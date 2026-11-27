"""Lock and unlock of a lock the library can send to (``EufySecurity.lock_control``).

The command goes through the library to the fake station's fake lock; the entity shows
the library's reading (``EufySecurity.lock_state``): locking or unlocking once the lock
took the command, then the state the lock reports, never the call's return. One command
per lock at a time; refusals send nothing.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, Final

import pytest
from conftest import entity_id_for, record_states, set_up_warm, state_of, wait_until
from eufy_home_security import (
    AccountRejectedError,
    AuthenticationError,
    CameraWakeError,
    CipherUnusableError,
    CloudError,
    CommandRejectedError,
    CommandUnsupportedError,
    CommunicationError,
    DeviceBusyError,
    DeviceTimeoutError,
    EufySecurityError,
    GuestAccessError,
    HandshakeError,
    KeyRejectedError,
    LockNotMovedError,
    LockState,
    MqttAccountRequiredError,
    RateLimitedError,
    SessionReplacedError,
    UnsupportedError,
)
from eufy_home_security.lock_commands import control as lock_commands_control
from eufy_home_security.lock_commands.control import (
    LOCK_ANSWER_TIMEOUT,
    LOCK_CONFIRM_TIMEOUT,
    LOCK_PUBLISH_TIMEOUT,
    NOT_SELECTED_FOR_MQTT,
)
from eufy_home_security.station import lock_control as lock_control_module
from eufy_home_security.testing import FakeCloud, FakeLock, FakeStation, camera_device
from homeassistant.components.lock import DOMAIN as LOCK_DOMAIN
from homeassistant.components.lock import LockState as HALockState
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import issue_registry as ir

from custom_components.eufy_home_security import errors, runtime
from custom_components.eufy_home_security import lock as lock_module
from custom_components.eufy_home_security.const import (
    DOMAIN,
    EXC_ACCOUNT_REJECTED,
    EXC_LOCK_CLOUD_UNAVAILABLE,
    EXC_LOCK_COMMAND_CANCELLED,
    EXC_LOCK_COMMAND_FAILED,
    EXC_LOCK_COMMAND_REJECTED,
    EXC_LOCK_COMMAND_WAITING,
    EXC_LOCK_DEVICE_BUSY,
    EXC_LOCK_GUEST,
    EXC_LOCK_KEY_REJECTED,
    EXC_LOCK_KEY_UNUSABLE,
    EXC_LOCK_MQTT_REQUIRED,
    EXC_LOCK_NOT_MOVED,
    EXC_LOCK_NOT_WRITABLE,
    EXC_LOCK_SESSION_REPLACED,
    EXC_LOCK_TIMEOUT,
    EXC_LOCK_UNREACHABLE,
    EXC_LOCK_UNSUPPORTED,
    LOCK_COMMAND_TIMEOUT_SECONDS,
    LOCK_KEY,
)

# A synthetic serial on a lock product whose command the library sends over the station
# (an ECIES-keyed JSON command), paired to the fake HomeBase.
LOCK_SN: Final = "T8520P0000000001"
LOCK_CHANNEL: Final = 1
# The lock-state parameter of a lock behind a HomeBase (library guide, Locks).
LOCK_STATE_PARAM: Final = 1912
LOCKED: Final = "4"
UNLOCKED: Final = "3"
# The member record the cloud lists for the account's own lock (member_type 2: owner).
OWNER: Final = {"admin_user_id": "0" * 40, "member_type": 2, "short_user_id": "0001"}

pytestmark = pytest.mark.usefixtures("built_clients")


@pytest.fixture
def lock_sn() -> str:
    """The lock's serial; a test parametrizes another product."""
    return LOCK_SN


@pytest.fixture
def fake_lock(lock_sn: str) -> FakeLock:
    """The lock on the fake HomeBase's channel 1, unlocked."""
    return FakeLock(serial=lock_sn, channel=LOCK_CHANNEL)


@pytest.fixture
def member() -> dict[str, Any]:
    """The lock's member record in the device list."""
    return dict(OWNER)


@pytest.fixture
def fake_cloud(fake_station: FakeStation, fake_lock: FakeLock, member: dict[str, Any]) -> FakeCloud:
    """The fake HomeBase with its camera and the lock, which serves its public keys."""
    cloud = FakeCloud.for_stations(fake_station)
    device = camera_device(fake_lock.serial, station_sn=fake_station.serial, channel=LOCK_CHANNEL)
    cloud.devices.append(device | {"member": member})
    cloud.lock_public_keys.update(fake_lock.public_keys())
    fake_station.locks[LOCK_CHANNEL] = fake_lock
    return cloud


@pytest.fixture
def short_lock_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Half-second answer and state waits; the library waits 20 s and 30 s. The state wait
    also bounds how long a failed command's move shows (one second here)."""
    monkeypatch.setattr(lock_control_module, "LOCK_ANSWER_TIMEOUT", 0.5)
    monkeypatch.setattr(lock_control_module, "LOCK_CONFIRM_TIMEOUT", 0.5)
    monkeypatch.setattr(lock_commands_control, "LOCK_CONFIRM_TIMEOUT", 1.0)


def _seed(station: FakeStation, lock: FakeLock, raw: str) -> None:
    lock.state_code = int(raw)
    station.params.setdefault(LOCK_CHANNEL, {})[LOCK_STATE_PARAM] = raw


def _lock_id(hass: HomeAssistant) -> str:
    return entity_id_for(hass, LOCK_DOMAIN, LOCK_SN, LOCK_KEY)


async def _call(hass: HomeAssistant, service: str) -> None:
    await hass.services.async_call(
        LOCK_DOMAIN, service, {ATTR_ENTITY_ID: _lock_id(hass)}, blocking=True
    )


def _shown(states: list[str]) -> list[str]:
    """The states in order, a repeat (an attribute change) left out."""
    return [state for index, state in enumerate(states) if not index or states[index - 1] != state]


def _start(hass: HomeAssistant, service: str) -> asyncio.Task[None]:
    """The service call as a task Home Assistant does not track (its wait is held)."""
    return asyncio.get_running_loop().create_task(_call(hass, service))


# (service, the state before, the state asked, the HA state while waiting, after)
_COMMANDS: Final = (
    ("lock", UNLOCKED, LOCKED, HALockState.LOCKING, HALockState.LOCKED),
    ("unlock", LOCKED, UNLOCKED, HALockState.UNLOCKING, HALockState.UNLOCKED),
)


@pytest.mark.parametrize(
    ("service", "before", "after", "waiting", "shown"), _COMMANDS, ids=["lock", "unlock"]
)
async def test_a_command_is_sent_and_shows_the_reported_state(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_lock: FakeLock,
    seed_warm_cache: Callable[..., None],
    service: str,
    before: str,
    after: str,
    waiting: str,
    shown: str,
) -> None:
    """The lock gets the asked action once; the entity shows the lock's report after (the
    fake lock reports at once, so the moving state between is covered by the held
    command below)."""
    _seed(fake_station, fake_lock, before)
    await set_up_warm(hass, seed_warm_cache)
    states = record_states(hass, _lock_id(hass))

    await _call(hass, service)

    assert [str(action.name).lower() for _, action, _ in fake_lock.received] == [service]
    assert _shown(states)[-1] == shown
    assert state_of(hass, _lock_id(hass)) == shown


async def _held_command(
    hass: HomeAssistant, fake_lock: FakeLock, service: str, waiting: str
) -> asyncio.Task[None]:
    """A command the lock took but has not reported yet: the entity shows ``waiting``."""
    fake_lock.report = False
    task = _start(hass, service)
    await wait_until(lambda: state_of(hass, _lock_id(hass)) == waiting)
    await wait_until(lambda: len(fake_lock.received) == 1)
    return task


@pytest.mark.parametrize(
    ("service", "before", "after", "waiting", "shown"), _COMMANDS, ids=["lock", "unlock"]
)
async def test_the_command_ends_only_on_the_locks_report(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_lock: FakeLock,
    seed_warm_cache: Callable[..., None],
    service: str,
    before: str,
    after: str,
    waiting: str,
    shown: str,
) -> None:
    """The lock took the command but has not reported: the call still waits and the
    entity shows the moving state; the report ends both."""
    _seed(fake_station, fake_lock, before)
    await set_up_warm(hass, seed_warm_cache)
    task = await _held_command(hass, fake_lock, service, waiting)
    await asyncio.sleep(0.1)
    assert not task.done()
    assert state_of(hass, _lock_id(hass)) == waiting

    fake_station.send_param_dump()  # the lock's report of the state it moved to
    async with asyncio.timeout(5):
        await task
    assert state_of(hass, _lock_id(hass)) == shown
    assert fake_station.params[LOCK_CHANNEL][LOCK_STATE_PARAM] == after


@pytest.mark.parametrize("second", ["lock", "unlock"])
async def test_a_second_command_while_one_waits_is_refused_and_sends_nothing(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_lock: FakeLock,
    seed_warm_cache: Callable[..., None],
    second: str,
) -> None:
    """Lock or unlock while a lock waits: refused with the translated message, nothing
    sent; the first command still ends on the lock's report."""
    _seed(fake_station, fake_lock, UNLOCKED)
    await set_up_warm(hass, seed_warm_cache)
    task = await _held_command(hass, fake_lock, "lock", HALockState.LOCKING)

    with pytest.raises(ServiceValidationError) as raised:
        async with asyncio.timeout(5):
            await _call(hass, second)

    assert raised.value.translation_domain == DOMAIN
    assert raised.value.translation_key == EXC_LOCK_COMMAND_WAITING
    assert len(fake_lock.received) == 1
    assert state_of(hass, _lock_id(hass)) == HALockState.LOCKING
    fake_station.send_param_dump()
    async with asyncio.timeout(5):
        await task
    assert state_of(hass, _lock_id(hass)) == HALockState.LOCKED


async def test_two_commands_at_once_send_one(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_lock: FakeLock,
    seed_warm_cache: Callable[..., None],
) -> None:
    """Two calls started together: one is sent, the other refused before sending."""
    _seed(fake_station, fake_lock, UNLOCKED)
    await set_up_warm(hass, seed_warm_cache)

    results = await asyncio.gather(
        _call(hass, "lock"), _call(hass, "unlock"), return_exceptions=True
    )

    refused = [r for r in results if isinstance(r, ServiceValidationError)]
    assert [r.translation_key for r in refused] == [EXC_LOCK_COMMAND_WAITING]
    assert len(fake_lock.received) == 1


async def test_a_refused_command_never_shows_a_move(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_lock: FakeLock,
    seed_warm_cache: Callable[..., None],
) -> None:
    """The lock refuses: the call raises the refusal with its code, and the entity keeps
    the state the lock last reported, with no locking in between (the lock never took the
    command)."""
    fake_lock.answer_code = 1
    _seed(fake_station, fake_lock, UNLOCKED)
    await set_up_warm(hass, seed_warm_cache)
    states = record_states(hass, _lock_id(hass))

    with pytest.raises(HomeAssistantError) as raised:
        await _call(hass, "lock")
    await hass.async_block_till_done()

    assert raised.value.translation_key == EXC_LOCK_COMMAND_REJECTED
    assert raised.value.translation_placeholders == {"code": "1"}
    assert HALockState.LOCKING not in states
    assert state_of(hass, _lock_id(hass)) == HALockState.UNLOCKED


@pytest.mark.usefixtures("short_lock_waits")
async def test_no_report_within_the_wait_raises_the_timeout_and_the_move_shows_on(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_lock: FakeLock,
    seed_warm_cache: Callable[..., None],
) -> None:
    """The lock took the command and never reports: the library's wait ends the call with
    the translated timeout. The library keeps the lock locking until its next report or
    the end of its state wait after the take; the entity shows that, then the last report
    again."""
    fake_lock.report = False
    _seed(fake_station, fake_lock, UNLOCKED)
    entry = await set_up_warm(hass, seed_warm_cache)
    states = record_states(hass, _lock_id(hass))

    with pytest.raises(HomeAssistantError) as raised:
        async with asyncio.timeout(5):
            await _call(hass, "lock")

    assert raised.value.translation_key == EXC_LOCK_TIMEOUT
    assert entry.runtime_data.eufy.lock_state(LOCK_SN).state is LockState.LOCKING
    assert state_of(hass, _lock_id(hass)) == HALockState.LOCKING
    await wait_until(lambda: state_of(hass, _lock_id(hass)) == HALockState.UNLOCKED)
    assert _shown(states) == [HALockState.LOCKING, HALockState.UNLOCKED]


async def test_a_jammed_report_raises_not_moved_and_shows_jammed(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_lock: FakeLock,
    seed_warm_cache: Callable[..., None],
) -> None:
    """The lock reports its bolt stopped between positions: not moved, with the state."""
    fake_lock.move_to = 5
    _seed(fake_station, fake_lock, UNLOCKED)
    await set_up_warm(hass, seed_warm_cache)

    with pytest.raises(HomeAssistantError) as raised:
        await _call(hass, "lock")

    assert raised.value.translation_key == EXC_LOCK_NOT_MOVED
    assert raised.value.translation_placeholders == {"state": "intermediate"}
    assert state_of(hass, _lock_id(hass)) == HALockState.JAMMED


async def test_the_state_follows_the_report_not_the_calls_return(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_lock: FakeLock,
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A call that returns locked with no report from the lock leaves the shown state."""
    _seed(fake_station, fake_lock, UNLOCKED)
    entry = await set_up_warm(hass, seed_warm_cache)

    async def returns_locked(device_sn: str, *, timeout: float | None = None) -> LockState:
        del device_sn, timeout
        return LockState.LOCKED

    monkeypatch.setattr(entry.runtime_data.eufy, "async_lock", returns_locked)
    await _call(hass, "lock")

    assert state_of(hass, _lock_id(hass)) == HALockState.UNLOCKED


@pytest.mark.parametrize("service", ["lock", "unlock"])
@pytest.mark.parametrize("member", [OWNER | {"member_type": 0}], ids=["guest"])
async def test_a_lock_the_library_cannot_send_to_refuses_with_its_reason(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_lock: FakeLock,
    seed_warm_cache: Callable[..., None],
    service: str,
) -> None:
    """Not writable (here: the account is a guest of the lock): the library's reason is
    in the refusal, nothing is sent and the state stays."""
    _seed(fake_station, fake_lock, LOCKED)
    entry = await set_up_warm(hass, seed_warm_cache)
    reason = entry.runtime_data.eufy.lock_control(LOCK_SN).reason
    assert reason

    with pytest.raises(ServiceValidationError) as raised:
        await _call(hass, service)

    assert raised.value.translation_key == EXC_LOCK_NOT_WRITABLE
    assert raised.value.translation_placeholders == {"reason": reason}
    assert fake_lock.received == []
    assert state_of(hass, _lock_id(hass)) == HALockState.LOCKED


# Each library error a command can end with, and the key and placeholders it maps to.
_ERRORS: Final = (
    (GuestAccessError("guest"), EXC_LOCK_GUEST, None, True),
    (MqttAccountRequiredError("mqtt"), EXC_LOCK_MQTT_REQUIRED, None, True),
    (UnsupportedError("no member record"), EXC_LOCK_UNSUPPORTED, None, True),
    (DeviceBusyError("busy"), EXC_LOCK_DEVICE_BUSY, None, True),
    (CommandRejectedError(1961, -151), EXC_LOCK_COMMAND_REJECTED, {"code": "-151"}, False),
    # Also an UnsupportedError, but the station's answer to a command that was sent.
    (CommandUnsupportedError(1940, -108), EXC_LOCK_COMMAND_REJECTED, {"code": "-108"}, False),
    (
        LockNotMovedError(1940, "stuck", state=LockState.JAMMED),
        EXC_LOCK_NOT_MOVED,
        {"state": "jammed"},
        False,
    ),
    (DeviceTimeoutError("no state"), EXC_LOCK_TIMEOUT, None, False),
    # The station's answer, but it could not wake the lock's device: not reached.
    (CameraWakeError(1940, -204), EXC_LOCK_UNREACHABLE, None, False),
    (CommunicationError("closed"), EXC_LOCK_UNREACHABLE, None, False),
    (EufySecurityError("other"), EXC_LOCK_COMMAND_FAILED, None, False),
)


@pytest.mark.parametrize(
    ("error", "key", "placeholders", "refused"),
    _ERRORS,
    ids=[type(row[0]).__name__ for row in _ERRORS],
)
async def test_each_library_error_maps_to_its_own_message(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_lock: FakeLock,
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    error: EufySecurityError,
    key: str,
    placeholders: dict[str, str] | None,
    refused: bool,
) -> None:
    """The library's error ends the call with its own translated message; a refusal
    before sending is a validation error; the state shows the last report again."""
    _seed(fake_station, fake_lock, LOCKED)
    entry = await set_up_warm(hass, seed_warm_cache)

    async def fails(device_sn: str, *, timeout: float | None = None) -> LockState:
        del device_sn, timeout
        raise error

    monkeypatch.setattr(entry.runtime_data.eufy, "async_unlock", fails)
    with pytest.raises(HomeAssistantError) as raised:
        await _call(hass, "unlock")

    assert raised.value.translation_domain == DOMAIN
    assert raised.value.translation_key == key
    assert raised.value.translation_placeholders == placeholders
    assert isinstance(raised.value, ServiceValidationError) is refused
    assert state_of(hass, _lock_id(hass)) == HALockState.LOCKED


async def test_an_unload_cancels_a_waiting_command_and_leaves_no_task(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_lock: FakeLock,
    seed_warm_cache: Callable[..., None],
) -> None:
    """Unloading while a command waits ends the call with the translated cancel and
    leaves no task of the command running."""
    _seed(fake_station, fake_lock, UNLOCKED)
    entry = await set_up_warm(hass, seed_warm_cache)
    task = await _held_command(hass, fake_lock, "lock", HALockState.LOCKING)
    commands = [t for t in asyncio.all_tasks() if t.get_name() == f"{LOCK_KEY} lock"]
    assert len(commands) == 1

    async with asyncio.timeout(5):
        assert await hass.config_entries.async_unload(entry.entry_id)
        with pytest.raises(HomeAssistantError) as raised:
            await task

    assert entry.state is ConfigEntryState.NOT_LOADED
    assert raised.value.translation_key == EXC_LOCK_COMMAND_CANCELLED
    assert commands[0].cancelled()
    assert not [t for t in asyncio.all_tasks() if t.get_name() == f"{LOCK_KEY} lock"]


async def test_a_lock_without_a_station_session_cannot_be_sent_to(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_lock: FakeLock,
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``lock_control`` raising for a lock the device list no longer names maps to the
    unsupported refusal; nothing is sent."""
    _seed(fake_station, fake_lock, LOCKED)
    entry = await set_up_warm(hass, seed_warm_cache)

    def not_listed(device_sn: str) -> Any:
        raise UnsupportedError(f"{device_sn} is not in the device list")

    monkeypatch.setattr(entry.runtime_data.eufy, "lock_control", not_listed)
    with pytest.raises(ServiceValidationError) as raised:
        await _call(hass, "unlock")

    assert raised.value.translation_key == EXC_LOCK_UNSUPPORTED
    assert fake_lock.received == []


async def test_an_account_without_an_mqtt_lock_builds_no_mqtt_account(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_lock: FakeLock,
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every lock of the entry takes its commands over a station: no MQTT connection."""
    built: list[object] = []
    monkeypatch.setattr(runtime, "EufyMqttAccount", built.append)
    _seed(fake_station, fake_lock, LOCKED)
    entry = await set_up_warm(hass, seed_warm_cache)

    assert entry.runtime_data.eufy.lock_control(LOCK_SN).path == "station"
    assert built == []


# A T8506 serial variant (7th character 8) whose route is MQTT, while eufy's MQTT
# connection does not serve it: the library cannot send its commands.
UNSERVED_MQTT_LOCK_SN: Final = "T8506P8000000001"


@pytest.mark.parametrize("lock_sn", [UNSERVED_MQTT_LOCK_SN])
async def test_a_lock_the_mqtt_connection_does_not_serve_is_read_only(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_lock: FakeLock,
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    lock_sn: str,
) -> None:
    """Its lock shows its state, refuses lock and unlock with the library's reason, and
    the entry builds no MQTT connection for it."""
    built: list[object] = []
    monkeypatch.setattr(runtime, "EufyMqttAccount", built.append)
    _seed(fake_station, fake_lock, LOCKED)
    entry = await set_up_warm(hass, seed_warm_cache)
    control = entry.runtime_data.eufy.lock_control(lock_sn)
    entity_id = entity_id_for(hass, LOCK_DOMAIN, lock_sn, LOCK_KEY)

    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            LOCK_DOMAIN, "unlock", {ATTR_ENTITY_ID: entity_id}, blocking=True
        )

    assert control.form == "mqtt"
    assert control.reason == NOT_SELECTED_FOR_MQTT
    assert raised.value.translation_key == EXC_LOCK_NOT_WRITABLE
    assert raised.value.translation_placeholders == {"reason": NOT_SELECTED_FOR_MQTT}
    assert built == []
    assert fake_lock.received == []
    assert state_of(hass, entity_id) == HALockState.LOCKED


# Errors whose remedy is the account's or the station's (a repair, a sign-in): the key, and
# what the failure routes to (None, a repair issue of the account, or the reauth flow).
_ROUTED: Final = (
    (SessionReplacedError("replaced"), EXC_LOCK_SESSION_REPLACED, "issue"),
    (RateLimitedError("held off", retry_after=600), EXC_LOCK_CLOUD_UNAVAILABLE, "issue"),
    (AuthenticationError("refused"), EXC_LOCK_CLOUD_UNAVAILABLE, "reauth"),
    (CloudError("down"), EXC_LOCK_CLOUD_UNAVAILABLE, None),
    (KeyRejectedError("rejected"), EXC_LOCK_KEY_REJECTED, None),
    (CipherUnusableError("unusable"), EXC_LOCK_KEY_UNUSABLE, None),
    (HandshakeError("no session key"), EXC_LOCK_UNREACHABLE, None),
)


@pytest.mark.parametrize(
    ("error", "key", "route"), _ROUTED, ids=[type(row[0]).__name__ for row in _ROUTED]
)
async def test_cloud_and_station_key_failures_point_at_their_remedy(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_lock: FakeLock,
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    error: EufySecurityError,
    key: str,
    route: str | None,
) -> None:
    """Each class gets its own message; a cloud refusal also goes to the account's repair
    issue or the reauth flow, as a failed device-list refresh does."""
    _seed(fake_station, fake_lock, LOCKED)
    entry = await set_up_warm(hass, seed_warm_cache)

    async def fails(device_sn: str, *, timeout: float | None = None) -> LockState:
        del device_sn, timeout
        raise error

    monkeypatch.setattr(entry.runtime_data.eufy, "async_unlock", fails)
    with pytest.raises(HomeAssistantError) as raised:
        await _call(hass, "unlock")

    assert raised.value.translation_key == key
    assert not isinstance(raised.value, ServiceValidationError)
    issues = {
        issue_id
        for (domain, issue_id) in ir.async_get(hass).issues
        if domain == DOMAIN and issue_id.startswith(("session_replaced", "login_limited"))
    }
    flows = [
        f
        for f in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        if f["context"].get("source") == "reauth"
    ]
    assert bool(issues) is (route == "issue")
    assert bool(flows) is (route == "reauth")


async def test_an_account_refusal_names_the_station_and_the_lock(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_lock: FakeLock,
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The station refusing the account (-104) shows the shared sharing hint, naming the
    station and the lock as Home Assistant names them."""
    _seed(fake_station, fake_lock, LOCKED)
    entry = await set_up_warm(hass, seed_warm_cache)

    async def fails(device_sn: str, *, timeout: float | None = None) -> LockState:
        del device_sn, timeout
        raise AccountRejectedError(1961, -104)

    monkeypatch.setattr(entry.runtime_data.eufy, "async_unlock", fails)
    with pytest.raises(HomeAssistantError) as raised:
        await _call(hass, "unlock")

    coordinator = entry.runtime_data.coordinators[fake_station.serial]
    assert raised.value.translation_key == EXC_ACCOUNT_REJECTED
    assert raised.value.translation_placeholders == {
        "station": errors.station_display_name(coordinator),
        "target": f"{errors.device_display_name(coordinator, LOCK_SN)} Lock",
    }


@pytest.mark.parametrize("service", ["lock", "unlock"])
async def test_each_command_hands_the_library_its_bound(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_lock: FakeLock,
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    service: str,
) -> None:
    """The library caps the whole call at ``LOCK_COMMAND_TIMEOUT_SECONDS``; its own answer
    and state waits keep their defaults within it."""
    _seed(fake_station, fake_lock, LOCKED if service == "unlock" else UNLOCKED)
    entry = await set_up_warm(hass, seed_warm_cache)
    calls: list[dict[str, Any]] = []

    async def records(device_sn: str, **kwargs: Any) -> LockState:
        del device_sn
        calls.append(kwargs)
        return LockState.LOCKED

    monkeypatch.setattr(entry.runtime_data.eufy, f"async_{service}", records)
    await _call(hass, service)

    assert calls == [{"timeout": LOCK_COMMAND_TIMEOUT_SECONDS}]


async def test_the_librarys_bound_ends_an_unanswered_command_as_a_timeout(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_lock: FakeLock,
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A command no one answers (the station sends no result), with the library's 20 s
    answer wait left as it is: the integration's bound (cut to 0.3 s here) ends the call as
    the lock's timeout, the last report stays shown and the next command is sent."""
    fake_lock.result = False
    _seed(fake_station, fake_lock, UNLOCKED)
    await set_up_warm(hass, seed_warm_cache)
    monkeypatch.setattr(lock_module, "LOCK_COMMAND_TIMEOUT_SECONDS", 0.3)

    for sent in (1, 2):
        with pytest.raises(HomeAssistantError) as raised:
            async with asyncio.timeout(5):
                await _call(hass, "lock")
        assert raised.value.translation_key == EXC_LOCK_TIMEOUT
        assert not isinstance(raised.value, ServiceValidationError)
        assert state_of(hass, _lock_id(hass)) == HALockState.UNLOCKED
        assert len(fake_lock.received) == sent


def test_the_command_bound_covers_the_librarys_waits_on_both_paths() -> None:
    """Over MQTT: the broker's take (a connect included), the lock's answer and its state;
    the station path's answer and state waits are within that, with room for its connect
    and the lock's key."""
    mqtt = LOCK_PUBLISH_TIMEOUT + LOCK_ANSWER_TIMEOUT + LOCK_CONFIRM_TIMEOUT
    assert LOCK_COMMAND_TIMEOUT_SECONDS > mqtt > LOCK_ANSWER_TIMEOUT + LOCK_CONFIRM_TIMEOUT


async def test_a_caller_that_stops_waiting_leaves_the_command_running(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_lock: FakeLock,
    seed_warm_cache: Callable[..., None],
) -> None:
    """The service call is cancelled (an automation stopped) while the lock has not
    reported: the command still waits, the lock still shows locking and a second command
    is refused; the lock's report then ends it."""
    _seed(fake_station, fake_lock, UNLOCKED)
    await set_up_warm(hass, seed_warm_cache)
    caller = await _held_command(hass, fake_lock, "lock", HALockState.LOCKING)

    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    await asyncio.sleep(0.05)

    assert state_of(hass, _lock_id(hass)) == HALockState.LOCKING
    assert [t for t in asyncio.all_tasks() if t.get_name() == f"{LOCK_KEY} lock"]
    with pytest.raises(ServiceValidationError):
        await _call(hass, "unlock")
    fake_station.send_param_dump()
    await wait_until(lambda: state_of(hass, _lock_id(hass)) == HALockState.LOCKED)
    assert len(fake_lock.received) == 1
