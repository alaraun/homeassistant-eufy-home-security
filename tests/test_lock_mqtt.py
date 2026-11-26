"""Locks the library commands over eufy's MQTT broker: one MQTT account per entry.

The entry starts the library's ``EufyMqttAccount`` and hands it to the client
(``use_mqtt``) only when one of its locks takes its commands over MQTT, follows each
device-list refresh with it and stops it at unload. Without the library's ``mqtt``
extra such a lock refuses lock and unlock. The account is a recording stand-in here:
the library's own client needs ``aiomqtt``, which the test environment does not hold.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import AsyncIterator, Callable, Iterable
from typing import Any, Final

import pytest
from conftest import entity_id_for, set_up_warm, state_of
from eufy_home_security import (
    CommunicationError,
    EufySecurity,
    EventSource,
    LockState,
    LockStateChanged,
    LockStateSource,
    MissingDependencyError,
    SecurityEvent,
)
from eufy_home_security.cloud.models import CloudDevice
from eufy_home_security.p2p.messages import STANDALONE_RECEIPT_LEN
from eufy_home_security.testing import SYNTHETIC, FakeCloud, FakeStation
from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN
from homeassistant.components.lock import DOMAIN as LOCK_DOMAIN
from homeassistant.components.lock import LockState as HALockState
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from custom_components.eufy_home_security import runtime
from custom_components.eufy_home_security.const import (
    EXC_LOCK_MQTT_REQUIRED,
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
    """Records what the integration asks of the library's ``EufyMqttAccount``; each call
    named in ``failing`` raises the library's communication error."""

    built: list[FakeMqttAccount]
    failing: frozenset[str] = frozenset()

    def __init__(self, cloud: object) -> None:
        self.cloud = cloud
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.listeners: list[Callable[[Any], None]] = []
        self.built.append(self)

    def add_listener(self, listener: Callable[[Any], None]) -> Callable[[], None]:
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)

    async def async_start(self, devices: Iterable[CloudDevice], **_: Any) -> None:
        self.calls.append(("start", tuple(sorted(d.device_sn for d in devices))))
        if "start" in self.failing:
            raise CommunicationError("start failed")

    async def async_update_devices(self, devices: Iterable[CloudDevice]) -> None:
        self.calls.append(("update", tuple(sorted(d.device_sn for d in devices))))
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


async def test_a_lock_report_over_mqtt_shows_until_the_station_reports_again(
    hass: HomeAssistant,
    fake_station: FakeStation,
    seed_warm_cache: Callable[..., None],
    accounts: list[FakeMqttAccount],
) -> None:
    """The lock's own report over MQTT (no station) sets the shown state; the next change
    the station path reports (here a cloud push naming the station) replaces it."""
    entry = await set_up_warm(hass, seed_warm_cache)
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
    assert state_of(hass, _lock_id(hass)) == HALockState.UNLOCKED

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


# A lock push reporting an unlock.
UNLOCK_PUSH: Final = 257


def _lock_push(event_type: int) -> SecurityEvent:
    return SecurityEvent(
        source=EventSource.CLOUD,
        station_sn=MQTT_LOCK_SN,
        device_sn=MQTT_LOCK_SN,
        event_type=event_type,
        frame_cipher=None,
    )


def _mqtt_report(state: LockState) -> LockStateChanged:
    return LockStateChanged(device_sn=MQTT_LOCK_SN, state=state, source=LockStateSource.LOCK)


async def test_a_newer_push_replaces_the_mqtt_report_even_repeating_the_station_state(
    hass: HomeAssistant,
    seed_warm_cache: Callable[..., None],
    accounts: list[FakeMqttAccount],
) -> None:
    """Push unlocked, then an MQTT report locked, then a push unlocked again: the station's
    state does not change (no LockStateChanged), yet the newest report shows. The push is
    delivered as the client does: its event first, then the station takes it."""
    entry = await set_up_warm(hass, seed_warm_cache)
    router = entry.runtime_data.router
    station = entry.runtime_data.coordinators[MQTT_LOCK_SN].station
    assert station.note_lock_push(_lock_push(UNLOCK_PUSH))
    await hass.async_block_till_done()
    assert state_of(hass, _lock_id(hass)) == HALockState.UNLOCKED

    router.handle(_mqtt_report(LockState.LOCKED))
    await hass.async_block_till_done()
    assert state_of(hass, _lock_id(hass)) == HALockState.LOCKED

    push = _lock_push(UNLOCK_PUSH)
    router.handle(push)
    assert station.note_lock_push(push)
    await hass.async_block_till_done()

    assert station.lock_state(MQTT_LOCK_SN).state is LockState.UNLOCKED
    assert state_of(hass, _lock_id(hass)) == HALockState.UNLOCKED


async def test_a_push_the_station_drops_keeps_the_newer_mqtt_report(
    hass: HomeAssistant,
    seed_warm_cache: Callable[..., None],
    accounts: list[FakeMqttAccount],
) -> None:
    """A push about the lock that the station does not take (older than its state) is no
    newer report: the MQTT report stays shown."""
    entry = await set_up_warm(hass, seed_warm_cache)
    router = entry.runtime_data.router
    station = entry.runtime_data.coordinators[MQTT_LOCK_SN].station
    router.handle(_mqtt_report(LockState.LOCKED))
    await hass.async_block_till_done()

    stale = dataclasses.replace(_lock_push(UNLOCK_PUSH), event_time_ms=1)
    router.handle(stale)
    assert not station.note_lock_push(stale)
    await hass.async_block_till_done()

    assert state_of(hass, _lock_id(hass)) == HALockState.LOCKED


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
