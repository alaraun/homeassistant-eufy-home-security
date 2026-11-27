"""Locks the library commands over eufy's MQTT broker: one MQTT account per entry.

The entry starts the library's ``EufyMqttAccount`` and hands it to the client
(``use_mqtt``) only when one of its locks takes its commands over MQTT, follows each
device-list refresh with it and stops it at unload. Without the library's ``mqtt``
extra such a lock refuses lock and unlock. The account's cloud errors reach the entry's
reauth or repair. The account is a recording stand-in here: the library's own client
needs ``aiomqtt``, which the test environment does not hold.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import AsyncIterator, Callable, Iterable
from typing import Any, Final

import pytest
from conftest import entity_id_for, set_up_warm, state_of
from eufy_home_security import (
    AuthenticationError,
    CloudError,
    CommunicationError,
    EufySecurity,
    EventSource,
    LockState,
    LockStateChanged,
    LockStateSource,
    MissingDependencyError,
    SecurityEvent,
    SessionReplacedError,
)
from eufy_home_security.cloud.models import CloudDevice
from eufy_home_security.lock_commands.actions import LockAction
from eufy_home_security.mqtt.account import EufyMqttAccount
from eufy_home_security.p2p.messages import STANDALONE_RECEIPT_LEN
from eufy_home_security.testing import SYNTHETIC, FakeCloud, FakeStation
from eufy_home_security.testing.mqtt import FakeMqttBroker, FakeMqttLock, make_mqtt_pki
from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN
from homeassistant.components.lock import DOMAIN as LOCK_DOMAIN
from homeassistant.components.lock import LockState as HALockState
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import issue_registry as ir

from custom_components.eufy_home_security import runtime
from custom_components.eufy_home_security.const import (
    DOMAIN,
    EXC_LOCK_MQTT_REQUIRED,
    EXC_LOCK_UNREACHABLE,
    LOCK_KEY,
    LOCK_MQTT_STOP_TIMEOUT_SECONDS,
    REFRESH_DEVICE_LIST_KEY,
)

# A synthetic video lock that is its own station: its commands go over MQTT.
MQTT_LOCK_SN: Final = "T8531P0000000001"
# The parameter block a standalone device labels with its device type (synthetic).
DEVICE_TYPE: Final = 51
# The lock-state parameter of a lock that is its own station (library guide, Locks).
STANDALONE_LOCK_STATE_PARAM: Final = 6000
# A lock push reporting a mechanical fault.
JAMMED_PUSH: Final = 517
OWNER: Final = {"admin_user_id": "0" * 40, "member_type": 2, "short_user_id": "0001"}

pytestmark = pytest.mark.usefixtures("built_clients")


class FakeMqttAccount:
    """Records what the integration and the client ask of the library's
    ``EufyMqttAccount``; each call named in ``failing`` raises the library's communication
    error. It serves no lock (``serves``), as while the account starts, and records every
    request it is asked to send."""

    built: list[FakeMqttAccount]
    failing: frozenset[str] = frozenset()

    def __init__(self, cloud: object) -> None:
        self.cloud = cloud
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.listeners: list[Callable[[Any], None]] = []
        self.cloud_listeners: list[Callable[[CloudError], None]] = []
        self.requests: list[str] = []
        self.updated_with: list[list[CloudDevice]] = []
        self.built.append(self)

    def add_listener(self, listener: Callable[[Any], None]) -> Callable[[], None]:
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)

    def add_cloud_error_listener(
        self, listener: Callable[[CloudError], None]
    ) -> Callable[[], None]:
        self.cloud_listeners.append(listener)
        return lambda: self.cloud_listeners.remove(listener)

    def serves(self, device_sn: str) -> bool:
        del device_sn
        return False

    async def async_request(self, device_sn: str, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        self.requests.append(device_sn)
        raise CommunicationError("not connected")

    async def async_start(self, devices: Iterable[CloudDevice], **_: Any) -> None:
        self.calls.append(("start", tuple(sorted(d.device_sn for d in devices))))
        if "start" in self.failing:
            raise CommunicationError("start failed")

    async def async_update_devices(self, devices: Iterable[CloudDevice]) -> None:
        listed = list(devices)
        self.updated_with.append(listed)
        self.calls.append(("update", tuple(sorted(d.device_sn for d in listed))))
        if "update" in self.failing:
            raise CommunicationError("update failed")

    async def async_stop(self) -> None:
        self.calls.append(("stop", ()))
        if "stop" in self.failing:
            raise CommunicationError("stop failed")


@pytest.fixture
def accounts(monkeypatch: pytest.MonkeyPatch) -> list[FakeMqttAccount]:
    """Every MQTT account the entry builds, in order."""
    built: list[FakeMqttAccount] = []
    monkeypatch.setattr(FakeMqttAccount, "built", built, raising=False)
    monkeypatch.setattr(runtime, "EufyMqttAccount", FakeMqttAccount)
    return built


@pytest.fixture
def handed(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Every account the entry hands the client with ``use_mqtt`` (None: taken back)."""
    calls: list[object] = []
    original = EufySecurity.use_mqtt

    def use_mqtt(self: EufySecurity, account: Any) -> None:
        calls.append(account)
        original(self, account)

    monkeypatch.setattr(EufySecurity, "use_mqtt", use_mqtt)
    return calls


@pytest.fixture
async def fake_station() -> AsyncIterator[FakeStation]:
    """The standalone video lock, locked. It connects on demand, so setup reads its state
    from the device list's copy."""
    station = FakeStation(
        serial=MQTT_LOCK_SN,
        cipher_id=98,
        receipt_len=STANDALONE_RECEIPT_LEN,
        guard_mode=1,
        params={DEVICE_TYPE: {1101: "61", STANDALONE_LOCK_STATE_PARAM: "4"}, 255: {1216: "Door"}},
    )
    station.sd_info = (0, 32000, 16000)  # answers setup's storage query at once
    await station.start()
    yield station
    station.stop()


@pytest.fixture
def fake_cloud(fake_station: FakeStation) -> FakeCloud:
    serial = fake_station.serial
    return FakeCloud(
        devices=[
            {
                "device_sn": serial,
                "device_type": DEVICE_TYPE,
                "device_name": "Door",
                "parent_sn": serial,
                "device_channel": DEVICE_TYPE,
                "p2p_did": SYNTHETIC.did,
                "local_ip": SYNTHETIC.station_ip,
                "member": dict(OWNER),
                "params": [
                    {"param_type": 1101, "param_value": "61", "update_time": 1.7e9},
                    {
                        "param_type": STANDALONE_LOCK_STATE_PARAM,
                        "param_value": "4",
                        "update_time": 1.7e9,
                    },
                ],
            }
        ],
        owner_ids={serial: fake_station.account_id},
        cipher_keys={serial: fake_station.ecc_private_key_hex},
    )


def _lock_id(hass: HomeAssistant) -> str:
    return entity_id_for(hass, LOCK_DOMAIN, MQTT_LOCK_SN, LOCK_KEY)


async def test_an_account_with_an_mqtt_lock_starts_one_mqtt_account(
    hass: HomeAssistant,
    seed_warm_cache: Callable[..., None],
    accounts: list[FakeMqttAccount],
    handed: list[object],
) -> None:
    """Setup builds one account on the client's cloud, hands it over and starts it with
    the device list."""
    entry = await set_up_warm(hass, seed_warm_cache)
    eufy = entry.runtime_data.eufy

    assert eufy.lock_control(MQTT_LOCK_SN).path == "mqtt"
    assert len(accounts) == 1
    assert accounts[0].cloud is eufy.cloud
    assert handed == [accounts[0]]
    assert accounts[0].calls == [("start", (MQTT_LOCK_SN,))]


async def test_a_device_list_refresh_updates_the_mqtt_account(
    hass: HomeAssistant,
    seed_warm_cache: Callable[..., None],
    accounts: list[FakeMqttAccount],
) -> None:
    """The account follows each refresh of the device list."""
    entry = await set_up_warm(hass, seed_warm_cache)

    button = entity_id_for(hass, BUTTON_DOMAIN, entry.entry_id, REFRESH_DEVICE_LIST_KEY)
    await hass.services.async_call(BUTTON_DOMAIN, "press", {ATTR_ENTITY_ID: button}, blocking=True)

    assert accounts[0].calls == [("start", (MQTT_LOCK_SN,)), ("update", (MQTT_LOCK_SN,))]


async def test_a_refresh_that_reloads_the_entry_leaves_the_mqtt_account_alone(
    hass: HomeAssistant,
    seed_warm_cache: Callable[..., None],
    accounts: list[FakeMqttAccount],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fetched list changed the paired devices (the router schedules a reload during
    the fetch): the old account is not updated, the reload builds the next one."""
    entry = await set_up_warm(hass, seed_warm_cache)
    eufy = entry.runtime_data.eufy
    router = entry.runtime_data.router
    fetch = eufy.async_discover

    async def fetch_that_changes_the_pairing(*args: Any, **kwargs: Any) -> Any:
        found = await fetch(*args, **kwargs)
        router.async_reload_soon("paired devices changed")
        return found

    monkeypatch.setattr(eufy, "async_discover", fetch_that_changes_the_pairing)
    button = entity_id_for(hass, BUTTON_DOMAIN, entry.entry_id, REFRESH_DEVICE_LIST_KEY)
    await hass.services.async_call(BUTTON_DOMAIN, "press", {ATTR_ENTITY_ID: button}, blocking=True)
    await hass.async_block_till_done()

    assert [tag for tag, _ in accounts[0].calls] == ["start", "stop"]
    assert len(accounts) == 2


async def test_an_unload_stops_the_mqtt_account(
    hass: HomeAssistant,
    seed_warm_cache: Callable[..., None],
    accounts: list[FakeMqttAccount],
) -> None:
    """The unload stops the account."""
    entry = await set_up_warm(hass, seed_warm_cache)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert accounts[0].calls[-1] == ("stop", ())


async def test_an_unload_stops_the_mqtt_account_before_the_client_closes(
    hass: HomeAssistant,
    seed_warm_cache: Callable[..., None],
    accounts: list[FakeMqttAccount],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The account's bounded stop comes first, then the client's close (which takes the
    account back and saves the account store), both inside the unload."""
    close = EufySecurity.async_close

    async def recorded_close(self: EufySecurity) -> None:
        accounts[0].calls.append(("close", ()))
        await close(self)

    monkeypatch.setattr(EufySecurity, "async_close", recorded_close)
    entry = await set_up_warm(hass, seed_warm_cache)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert [tag for tag, _ in accounts[0].calls][-2:] == ["stop", "close"]


async def test_without_the_mqtt_extra_the_lock_refuses_and_setup_goes_on(
    hass: HomeAssistant,
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    handed: list[object],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The library's MQTT client is not installed: one warning, the lock still shows its
    state, and lock and unlock refuse before anything is sent."""

    def missing(cloud: object) -> None:
        raise MissingDependencyError("the eufy MQTT client", extra="mqtt")

    monkeypatch.setattr(runtime, "EufyMqttAccount", missing)
    caplog.set_level(logging.WARNING)
    await set_up_warm(hass, seed_warm_cache)

    assert handed == []
    warnings = [
        r for r in caplog.records if r.name.startswith("custom_components") and "mqtt" in r.message
    ]
    assert len(warnings) == 1
    assert state_of(hass, _lock_id(hass)) == HALockState.LOCKED
    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            LOCK_DOMAIN, "unlock", {ATTR_ENTITY_ID: _lock_id(hass)}, blocking=True
        )
    assert raised.value.translation_key == EXC_LOCK_MQTT_REQUIRED


async def test_the_lock_shows_the_librarys_reading_not_the_events_state(
    hass: HomeAssistant,
    seed_warm_cache: Callable[..., None],
    accounts: list[FakeMqttAccount],
) -> None:
    """The entity shows ``EufySecurity.lock_state``, the library's one merged reading of
    every source: an event's own state is not kept beside it (here one naming no station
    while the library still reads locked), and a push the library takes shows."""
    entry = await set_up_warm(hass, seed_warm_cache)
    eufy = entry.runtime_data.eufy
    assert state_of(hass, _lock_id(hass)) == HALockState.LOCKED

    entry.runtime_data.router.handle(
        LockStateChanged(
            device_sn=MQTT_LOCK_SN,
            state=LockState.UNLOCKED,
            source=LockStateSource.LOCK,
            raw="3",
        )
    )
    await hass.async_block_till_done()
    assert eufy.lock_state(MQTT_LOCK_SN).state is LockState.LOCKED
    assert state_of(hass, _lock_id(hass)) == HALockState.LOCKED

    station = entry.runtime_data.coordinators[MQTT_LOCK_SN].station
    assert station.note_lock_push(
        SecurityEvent(
            source=EventSource.CLOUD,
            station_sn=MQTT_LOCK_SN,
            device_sn=MQTT_LOCK_SN,
            event_type=JAMMED_PUSH,
            frame_cipher=None,
        )
    )
    await hass.async_block_till_done()
    assert state_of(hass, _lock_id(hass)) == HALockState.JAMMED


async def test_mqtt_account_failures_are_logged_and_setup_goes_on(
    hass: HomeAssistant,
    seed_warm_cache: Callable[..., None],
    accounts: list[FakeMqttAccount],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A start, a device-list update and a stop that fail are logged by their error type;
    setup, the refresh and the unload complete."""
    monkeypatch.setattr(FakeMqttAccount, "failing", frozenset({"start", "update", "stop"}))
    caplog.set_level(logging.DEBUG, logger="custom_components.eufy_home_security")
    entry = await set_up_warm(hass, seed_warm_cache)
    button = entity_id_for(hass, BUTTON_DOMAIN, entry.entry_id, REFRESH_DEVICE_LIST_KEY)
    await hass.services.async_call(BUTTON_DOMAIN, "press", {ATTR_ENTITY_ID: button}, blocking=True)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert [tag for tag, _ in accounts[0].calls] == ["start", "update", "stop"]
    logged = [
        r.getMessage()
        for r in caplog.records
        if r.name == "custom_components.eufy_home_security.runtime" and "MQTT" in r.getMessage()
    ]
    assert logged == [
        "The MQTT connection for locks did not start: CommunicationError",
        "The MQTT connection for locks did not follow the device list: CommunicationError",
        "The MQTT connection for locks stopped with CommunicationError",
    ]


async def test_a_command_while_the_account_serves_no_client_is_unreachable(
    hass: HomeAssistant,
    seed_warm_cache: Callable[..., None],
    accounts: list[FakeMqttAccount],
) -> None:
    """The account is still starting (or its start failed): the library sends nothing and
    the call ends with the unreachable message, not a validation error."""
    entry = await set_up_warm(hass, seed_warm_cache)
    assert entry.runtime_data.eufy.lock_control(MQTT_LOCK_SN).writable

    with pytest.raises(HomeAssistantError) as raised:
        await hass.services.async_call(
            LOCK_DOMAIN, "unlock", {ATTR_ENTITY_ID: _lock_id(hass)}, blocking=True
        )

    assert raised.value.translation_key == EXC_LOCK_UNREACHABLE
    assert not isinstance(raised.value, ServiceValidationError)
    assert accounts[0].requests == []
    assert state_of(hass, _lock_id(hass)) == HALockState.LOCKED


@pytest.mark.parametrize(
    ("error", "route"),
    [(AuthenticationError("refused"), "reauth"), (SessionReplacedError("replaced"), "issue")],
    ids=["AuthenticationError", "SessionReplacedError"],
)
async def test_the_mqtt_accounts_cloud_errors_reach_reauth_or_the_repair(
    hass: HomeAssistant,
    seed_warm_cache: Callable[..., None],
    accounts: list[FakeMqttAccount],
    error: CloudError,
    route: str,
) -> None:
    """A cloud error of the account's certificate fetch arrives as the client's
    ``CloudProblem`` naming no station; it goes to reauth or the account's repair issue,
    as the other cloud problems do."""
    entry = await set_up_warm(hass, seed_warm_cache)
    assert len(accounts[0].cloud_listeners) == 1

    accounts[0].cloud_listeners[0](error)
    await hass.async_block_till_done()

    issues = [
        issue_id
        for (domain, issue_id) in ir.async_get(hass).issues
        if domain == DOMAIN and issue_id.startswith("session_replaced")
    ]
    flows = [
        flow
        for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        if flow["context"].get("source") == "reauth"
        and flow["context"].get("entry_id") == entry.entry_id
    ]
    assert bool(issues) is (route == "issue")
    assert bool(flows) is (route == "reauth")


async def test_a_stuck_mqtt_stop_does_not_hold_the_clients_close_on_reload(
    hass: HomeAssistant,
    seed_warm_cache: Callable[..., None],
    accounts: list[FakeMqttAccount],
    built_clients: list[EufySecurity],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An MQTT stop that does not end (a connect step the library still settles) is given
    up after its bound: the old client closes, and saves the account store, before the
    reload builds the new one, so two live clients never share the store."""
    release = asyncio.Event()

    async def stuck_stop(self: FakeMqttAccount) -> None:
        self.calls.append(("stop", ()))
        await release.wait()

    monkeypatch.setattr(FakeMqttAccount, "async_stop", stuck_stop)
    monkeypatch.setattr(runtime, "LOCK_MQTT_STOP_TIMEOUT_SECONDS", 0.2)
    closed_with: list[tuple[EufySecurity, int]] = []
    original_close = EufySecurity.async_close

    async def close(self: EufySecurity) -> None:
        closed_with.append((self, len(built_clients)))
        await original_close(self)

    monkeypatch.setattr(EufySecurity, "async_close", close)
    entry = await set_up_warm(hass, seed_warm_cache)
    old = entry.runtime_data.eufy

    try:
        async with asyncio.timeout(5):
            assert await hass.config_entries.async_reload(entry.entry_id)
    finally:
        release.set()

    assert accounts[0].calls[-1] == ("stop", ())
    assert closed_with[0] == (old, 1)
    assert entry.runtime_data.eufy is not old


# Home Assistant waits this long for an entry's unload tasks (config_entries).
HA_UNLOAD_TASK_WAIT_SECONDS: Final = 10


def test_the_mqtt_stop_bound_leaves_the_clients_close_room_in_the_unload_wait() -> None:
    """The stop's bound plus a few seconds for the client's close fit Home Assistant's
    unload wait."""
    assert 0 < LOCK_MQTT_STOP_TIMEOUT_SECONDS <= HA_UNLOAD_TASK_WAIT_SECONDS / 2


async def test_a_refresh_hands_the_account_the_new_device_list(
    hass: HomeAssistant,
    seed_warm_cache: Callable[..., None],
    fake_cloud: FakeCloud,
    accounts: list[FakeMqttAccount],
) -> None:
    """The account gets the list the refresh fetched, not the one before it."""
    entry = await set_up_warm(hass, seed_warm_cache)
    fake_cloud.devices[0]["device_name"] = "Back door"

    button = entity_id_for(hass, BUTTON_DOMAIN, entry.entry_id, REFRESH_DEVICE_LIST_KEY)
    await hass.services.async_call(BUTTON_DOMAIN, "press", {ATTR_ENTITY_ID: button}, blocking=True)

    assert [d.name for d in accounts[0].updated_with[0]] == ["Back door"]


async def test_an_unload_cancels_a_start_still_running(
    hass: HomeAssistant,
    seed_warm_cache: Callable[..., None],
    accounts: list[FakeMqttAccount],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The account's start is a task of the entry: an unload while it connects ends it."""
    started = asyncio.Event()
    ended: list[str] = []

    async def held_start(self: FakeMqttAccount, devices: Iterable[CloudDevice], **_: Any) -> None:
        del devices
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            ended.append("cancelled")
            raise

    monkeypatch.setattr(FakeMqttAccount, "async_start", held_start)
    entry = await set_up_warm(hass, seed_warm_cache)
    assert started.is_set()

    async with asyncio.timeout(5):
        assert await hass.config_entries.async_unload(entry.entry_id)

    assert ended == ["cancelled"]


async def test_an_unlock_over_the_broker_is_confirmed_by_the_locks_report(
    hass: HomeAssistant,
    seed_warm_cache: Callable[..., None],
    fake_cloud: FakeCloud,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the library's ``mqtt`` extra installed the entry builds the library's own
    account; it connects (here to a loopback broker with the fake cloud's certificate),
    the unlock goes over it and the lock's status report confirms it."""
    pki = make_mqtt_pki()
    fake_cloud.mqtt_answer = pki.register_answer()
    broker = FakeMqttBroker(pki)
    await broker.async_start()
    lock = FakeMqttLock(
        broker=broker,
        serial=MQTT_LOCK_SN,
        product_code="T8531",
        # The member record names no member user id: keyed by the account's own id.
        owner_user_id=SYNTHETIC.account_id,
        state_code=4,
    )
    lock.attach()
    monkeypatch.setattr(
        runtime,
        "EufyMqttAccount",
        functools.partial(EufyMqttAccount, port=broker.port, server_ca_pem=pki.ca_pem),
    )
    try:
        entry = await set_up_warm(hass, seed_warm_cache)
        account = entry.runtime_data.lock_mqtt
        assert isinstance(account, EufyMqttAccount)
        await account.async_wait_connected(5)
        await broker.wait_for(lambda: any(t.endswith("/res") for _, t, _ in broker.subscriptions))

        async with asyncio.timeout(10):
            await hass.services.async_call(
                LOCK_DOMAIN, "unlock", {ATTR_ENTITY_ID: _lock_id(hass)}, blocking=True
            )

        assert [action for action, _ in lock.received] == [LockAction.UNLOCK]
        assert lock.refused == []
        assert state_of(hass, _lock_id(hass)) == HALockState.UNLOCKED
        assert await hass.config_entries.async_unload(entry.entry_id)
    finally:
        await broker.async_stop()
