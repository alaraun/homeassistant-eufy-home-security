"""The guard-mode alarm panel: the mode mapping, arm and disarm, and the alarm lifecycle."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from collections.abc import Callable
from datetime import timedelta
from types import SimpleNamespace

import pytest
from conftest import (
    advance_to_poll,
    alarm_changed,
    entity_id_for,
    now_ms,
    panel_entity_id,
    record_states,
    set_guard_mode,
    set_up_warm,
    state_of,
    station_event,
    wait_until,
)
from eufy_home_security import (
    AlarmStopSource,
    CameraWakeError,
    CommandError,
    CommandNotAppliedError,
    CommandRejectedError,
    CommandUnsupportedError,
    CommunicationError,
    DeviceTimeoutError,
    EufySecurity,
    EufySecurityError,
    Event,
    EventSource,
    FrameCipher,
    GuardMode,
    GuardModeChanged,
    HandshakeError,
    KeyRejectedError,
    ProtocolError,
    PushMessageType,
    RateLimitedError,
    RefreshCooldownError,
    SecurityEvent,
    SessionReplacedError,
    StationStateChanged,
    StationUnreachableError,
)
from eufy_home_security.p2p import FrameType
from eufy_home_security.testing import SYNTHETIC, FakeCloud, FakeStation
from homeassistant.components.alarm_control_panel import ATTR_CODE_FORMAT
from homeassistant.components.alarm_control_panel import DOMAIN as ALARM_DOMAIN
from homeassistant.components.alarm_control_panel.const import (
    ATTR_CODE_ARM_REQUIRED,
    AlarmControlPanelEntityFeature,
    AlarmControlPanelState,
)
from homeassistant.components.event import DOMAIN as EVENT_DOMAIN
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_SUPPORTED_FEATURES,
    SERVICE_ALARM_ARM_AWAY,
    SERVICE_ALARM_ARM_HOME,
    SERVICE_ALARM_DISARM,
)
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_restore_state_shutdown_restart,
    mock_restore_cache_with_extra_data,
)

from custom_components.eufy_home_security import alarm_control_panel, detections, errors
from custom_components.eufy_home_security.const import (
    ALARM_EVENT_KEY,
    CONF_ALARM_TIMEOUT,
    DOMAIN,
    POLL_INTERVAL_SECONDS,
)
from custom_components.eufy_home_security.detections import ha_state

# The loggers whose lines a user's log would carry: the integration's and the library's.
_OUR_LOGGERS = ("custom_components.eufy_home_security", "eufy_home_security")

# The mode mapping, written out in full rather than derived from the function under test.
_EXPECTED: dict[GuardMode | int | None, AlarmControlPanelState | None] = {
    GuardMode.AWAY: AlarmControlPanelState.ARMED_AWAY,
    GuardMode.HOME: AlarmControlPanelState.ARMED_HOME,
    GuardMode.SCHEDULE: AlarmControlPanelState.ARMED_CUSTOM_BYPASS,
    GuardMode.CUSTOM_1: AlarmControlPanelState.ARMED_CUSTOM_BYPASS,
    GuardMode.CUSTOM_2: AlarmControlPanelState.ARMED_CUSTOM_BYPASS,
    GuardMode.CUSTOM_3: AlarmControlPanelState.ARMED_CUSTOM_BYPASS,
    GuardMode.OFF: AlarmControlPanelState.DISARMED,
    GuardMode.GEOFENCE: AlarmControlPanelState.ARMED_CUSTOM_BYPASS,
    GuardMode.DISARMED: AlarmControlPanelState.DISARMED,
    # A code the library does not know, and no read yet: unknown, never disarmed.
    99: None,
    None: None,
}


def test_the_table_covers_every_guard_mode() -> None:
    """A mode added to the library must be given a panel state here on purpose."""
    assert set(GuardMode) <= set(_EXPECTED)


@pytest.mark.parametrize(("mode", "expected"), list(_EXPECTED.items()), ids=str)
def test_guard_modes_map_to_alarm_states(
    mode: GuardMode | int | None, expected: AlarmControlPanelState | None
) -> None:
    assert ha_state(mode) == expected


async def test_the_panel_shows_the_station_guard_mode_on_the_poll(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A mode changed on the station itself reaches the panel on the next poll."""
    set_guard_mode(fake_station, GuardMode.HOME)
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)
    assert state_of(hass, entity_id) == "armed_home"

    set_guard_mode(fake_station, GuardMode.DISARMED)
    await advance_to_poll(hass, POLL_INTERVAL_SECONDS + 1)
    await wait_until(lambda: state_of(hass, entity_id) == "disarmed")

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_an_unknown_guard_mode_code_is_unknown_and_logged_once(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Code 99 shows as unknown, is warned about once, and no log line names the serial."""
    caplog.set_level(logging.DEBUG)
    set_guard_mode(fake_station, 99)
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)
    assert state_of(hass, entity_id) == "unknown"

    coordinator = entry.runtime_data.coordinators[SYNTHETIC.station_sn]
    for _ in range(2):
        before = coordinator.data
        await advance_to_poll(hass, POLL_INTERVAL_SECONDS + 1)
        # Each poll stores a fresh state object: proof the poll really ran.
        await wait_until(lambda before=before: coordinator.data is not before)
        # An unchanged poll notifies no entity (always_update=False), so force a
        # state write: the panel re-reads code 99 and must not warn again.
        coordinator.async_update_listeners()
        await hass.async_block_till_done()
        assert state_of(hass, entity_id) == "unknown"

    warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and "99" in record.getMessage()
    ]
    assert len(warnings) == 1, [record.getMessage() for record in warnings]
    assert entity_id in warnings[0].getMessage()

    # The integration's and the library's own log lines. The test
    # harness logs every hass_storage document it loads or writes in full, a
    # test-only line real Home Assistant never emits, so it is not in scope.
    ours = [record for record in caplog.records if record.name.startswith(_OUR_LOGGERS)]
    assert any(record.name.startswith("eufy_home_security") for record in ours), (
        "no library log record was captured, so the serial check below is vacuous"
    )
    leaking = [
        f"{record.name}: {record.getMessage()}"
        for record in ours
        if SYNTHETIC.station_sn in record.getMessage()
    ]
    assert leaking == []

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


# ── arm, arm home and disarm ─────────────────────────────────────────────────


async def call_panel(hass: HomeAssistant, service: str, entity_id: str) -> None:
    """Call an alarm panel service and wait for it, as a dashboard button does."""
    await hass.services.async_call(
        ALARM_DOMAIN, service, {ATTR_ENTITY_ID: entity_id}, blocking=True
    )


async def test_arm_home_disarm_and_arm_away_reach_the_station_and_show_the_applied_mode(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Each call changes the station's mode, and the panel shows the mode it applied."""
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)
    assert state_of(hass, entity_id) == "armed_away"

    for service, station_mode, shown in (
        (SERVICE_ALARM_ARM_HOME, 1, "armed_home"),
        (SERVICE_ALARM_DISARM, 63, "disarmed"),
        (SERVICE_ALARM_ARM_AWAY, 0, "armed_away"),
    ):
        await call_panel(hass, service, entity_id)
        assert fake_station.guard_mode == station_mode, service
        assert state_of(hass, entity_id) == shown, service

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_the_panel_shows_arming_or_disarming_until_the_applied_mode(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Arming, then armed home; disarming, then disarmed; nothing in between."""
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)

    states = record_states(hass, entity_id)
    await call_panel(hass, SERVICE_ALARM_ARM_HOME, entity_id)
    await hass.async_block_till_done()
    assert states == ["arming", "armed_home"]

    states.clear()
    await call_panel(hass, SERVICE_ALARM_DISARM, entity_id)
    await hass.async_block_till_done()
    assert states == ["disarming", "disarmed"]

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_arming_to_the_mode_in_force_shows_it_without_error(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The station is silent for the mode it is in; the library's read-back settles it."""
    assert fake_station.guard_mode == GuardMode.AWAY
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)

    await call_panel(hass, SERVICE_ALARM_ARM_AWAY, entity_id)

    assert state_of(hass, entity_id) == "armed_away"
    assert fake_station.guard_mode == GuardMode.AWAY

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_the_panel_offers_arm_away_and_arm_home_without_a_code(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Arm away and arm home, no custom bypass, and no code asked for."""
    entry = await set_up_warm(hass, seed_warm_cache)
    state = hass.states.get(panel_entity_id(hass))
    assert state is not None

    assert state.attributes[ATTR_SUPPORTED_FEATURES] == (
        AlarmControlPanelEntityFeature.ARM_AWAY | AlarmControlPanelEntityFeature.ARM_HOME
    )
    assert state.attributes[ATTR_CODE_ARM_REQUIRED] is False
    assert state.attributes[ATTR_CODE_FORMAT] is None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_two_arm_calls_together_end_at_the_last_completed_mode(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Backstop: the library serialises the two writes; no transitional state is left."""
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)

    await asyncio.gather(
        call_panel(hass, SERVICE_ALARM_ARM_HOME, entity_id),
        call_panel(hass, SERVICE_ALARM_ARM_AWAY, entity_id),
    )
    await hass.async_block_till_done()

    shown = state_of(hass, entity_id)
    assert shown not in ("arming", "disarming")
    expected = ha_state(GuardMode(fake_station.guard_mode))
    assert expected is not None
    assert shown == expected.value

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


# ── writes the station did not apply or could not receive ────────────────────

# Written out, not imported: the keys are the contract with the translations.
_NOT_APPLIED = "guard_mode_not_applied"
_UNREACHABLE = "station_unreachable"
_ON_DEMAND_UNREACHABLE = "on_demand_unreachable"
_KEY_REJECTED = "station_key_rejected"
_CLOUD_UNAVAILABLE = "cloud_unavailable"

# An owner account id the fake station does not hold, shaped like SYNTHETIC's.
_OTHER_ACCOUNT_ID = "fedcba9876543210fedcba9876543210fedcba98"


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (CommandNotAppliedError(1224), _NOT_APPLIED),
        (CommandRejectedError(1224, 6, "station applied OFF"), _NOT_APPLIED),
        # A -108 receipt: a rejection and an UnsupportedError at once.
        (CommandUnsupportedError(1224, -108), _NOT_APPLIED),
        # The station answered; the camera behind it did not wake.
        (CameraWakeError(1224, -204), _ON_DEMAND_UNREACHABLE),
        (StationUnreachableError("no reply to discovery"), _UNREACHABLE),
        (DeviceTimeoutError("command 1224 got no answer"), _UNREACHABLE),
        # What the reconnect before a write can raise.
        (ProtocolError("frame could not be decrypted"), _UNREACHABLE),
        (HandshakeError("session key not established"), _UNREACHABLE),
        (KeyRejectedError("key rejected"), _KEY_REJECTED),
        (RateLimitedError("throttled", retry_after=60.0), _CLOUD_UNAVAILABLE),
        (RefreshCooldownError("cooldown", retry_after=30.0), _CLOUD_UNAVAILABLE),
        # A kick-out in the chain names the session and Repairs, on
        # any station, instead of a cloud outage.
        (SessionReplacedError(), "session_replaced_see_repairs"),
    ],
    ids=lambda value: type(value).__name__ if isinstance(value, Exception) else value,
)
def test_arm_failures_map_to_translated_errors(error: EufySecurityError, key: str) -> None:
    """Each library error a write can meet becomes a translated error naming the mode."""
    translated = errors.arm_failed(error, GuardMode.AWAY)

    assert isinstance(translated, HomeAssistantError)
    assert translated.translation_domain == DOMAIN
    assert translated.translation_key == key
    assert translated.translation_placeholders == {"target": "away"}


def _expected_key(cause: BaseException | None) -> str:
    """The key for the library error a write actually raised, decided here, not by errors.py."""
    if isinstance(cause, CommandError):
        return _NOT_APPLIED
    assert isinstance(cause, CommunicationError), f"unexpected cause {cause!r}"
    return _UNREACHABLE


async def test_an_arm_the_station_did_not_apply_raises_and_keeps_the_last_mode(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The arm carries an owner id the station does not hold.

    The station drops such a command without a word, so the library waits its full
    command timeout (about 6 s) before it reads the mode back.
    """
    fake_cloud.owner_ids[fake_station.serial] = _OTHER_ACCOUNT_ID
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)
    assert state_of(hass, entity_id) == "armed_away"
    states = record_states(hass, entity_id)

    with pytest.raises(HomeAssistantError) as raised:
        await call_panel(hass, SERVICE_ALARM_ARM_HOME, entity_id)
    await hass.async_block_till_done()

    # Not vacuous: the arm reached the station, under the id it does not hold.
    arms = [obj for obj in fake_station.received if obj.get("cmd") == 1224]
    assert arms, "the arm never reached the station"
    assert {obj.get("account_id") for obj in arms} == {_OTHER_ACCOUNT_ID}

    assert raised.value.translation_key == _expected_key(raised.value.__cause__)
    assert raised.value.translation_placeholders == {"target": "home"}
    assert fake_station.guard_mode == GuardMode.AWAY
    assert state_of(hass, entity_id) == "armed_away"
    assert states == ["arming", "armed_away"]

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    ("error", "key"),
    [
        # A -108 receipt: a rejection and an UnsupportedError at once.
        (CommandUnsupportedError(1224, -108), _NOT_APPLIED),
        (CommandRejectedError(1224, 6, "station applied OFF"), _NOT_APPLIED),
        (StationUnreachableError("no reply to discovery"), _UNREACHABLE),
    ],
    ids=lambda value: type(value).__name__ if isinstance(value, Exception) else value,
)
async def test_an_arm_the_library_refuses_raises_a_translated_error(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    error: EufySecurityError,
    key: str,
) -> None:
    """A library error from the arm reaches the user translated; the panel keeps the last mode."""
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)
    station = entry.runtime_data.coordinators[SYNTHETIC.station_sn].station

    async def refused(mode: GuardMode) -> GuardMode:
        raise error

    monkeypatch.setattr(station, "async_set_guard_mode", refused)
    with pytest.raises(HomeAssistantError) as raised:
        await call_panel(hass, SERVICE_ALARM_ARM_HOME, entity_id)
    await hass.async_block_till_done()

    assert raised.value.translation_domain == DOMAIN
    assert raised.value.translation_key == key
    assert raised.value.translation_placeholders == {"target": "home"}
    assert raised.value.__cause__ is error
    assert state_of(hass, entity_id) == "armed_away"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_an_arm_on_a_stopped_station_raises_station_unreachable(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    short_discovery: None,
) -> None:
    """The station went silent while its session still looked up: the arm cannot land.

    Silent, not closed: a session the station closes is reported by the library,
    the panel goes unavailable, and Home Assistant never calls a service on an
    unavailable entity, so no arm is attempted at all.
    """
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)

    fake_station.stop()

    with pytest.raises(HomeAssistantError) as raised:
        await call_panel(hass, SERVICE_ALARM_ARM_HOME, entity_id)
    await hass.async_block_till_done()

    assert raised.value.translation_key == _UNREACHABLE
    assert isinstance(raised.value.__cause__, CommunicationError)
    assert state_of(hass, entity_id) not in ("arming", "disarming")
    assert fake_station.guard_mode == GuardMode.AWAY

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


# ── Schedule: the mode in force, and the selection beside it ──────────────────


def _select_schedule(station: FakeStation, slot_mode: GuardMode) -> None:
    """Put the fake station under Schedule with ``slot_mode`` in force, as the dump says."""
    set_guard_mode(station, GuardMode.SCHEDULE)
    station.schedule_mode = int(slot_mode)
    station.params[255][1151] = str(int(slot_mode))


async def test_under_schedule_the_panel_shows_the_slot_mode_and_says_schedule(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The state is the mode in force; ``selected_mode`` says a schedule drives it."""
    _select_schedule(fake_station, GuardMode.AWAY)
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)

    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "armed_away"
    assert state.attributes["selected_mode"] == "schedule"

    await _unload(hass, entry)


async def test_a_schedule_slot_boundary_moves_the_panel_once(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The station's report of the slot's mode moves the panel once, never via custom.

    The report (``0x047F``) carries the mode in force; the library keeps Schedule
    selected and emits one ``GuardModeChanged``, and the panel follows it.
    """
    _select_schedule(fake_station, GuardMode.AWAY)
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)
    states = record_states(hass, entity_id)

    fake_station.schedule_mode = int(GuardMode.HOME)
    fake_station.params[255][1151] = str(int(GuardMode.HOME))
    fake_station.send_alarm_mode(GuardMode.HOME)
    await wait_until(lambda: state_of(hass, entity_id) == "armed_home")
    await hass.async_block_till_done()

    assert set(states) == {"armed_home"}
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.attributes["selected_mode"] == "schedule"

    await _unload(hass, entry)


async def test_arming_away_from_schedule_leaves_the_schedule(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Arm away still works under Schedule and drops the selection attribute.

    Arming to Schedule is not offered: the panel has no custom bypass feature.
    """
    _select_schedule(fake_station, GuardMode.HOME)
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)
    assert state_of(hass, entity_id) == "armed_home"

    await call_panel(hass, SERVICE_ALARM_ARM_AWAY, entity_id)
    await hass.async_block_till_done()

    assert fake_station.guard_mode == GuardMode.AWAY
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "armed_away"
    assert "selected_mode" not in state.attributes
    assert not state.attributes[ATTR_SUPPORTED_FEATURES] & (
        AlarmControlPanelEntityFeature.ARM_CUSTOM_BYPASS
    )

    await _unload(hass, entry)


async def test_a_schedule_selection_without_its_slot_keeps_the_mode_in_force(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A Schedule selection that names no slot mode keeps the mode shown in force."""
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)
    coordinator = entry.runtime_data.coordinators[SYNTHETIC.station_sn]

    coordinator.async_apply_guard_mode(GuardMode.SCHEDULE)
    await hass.async_block_till_done()
    assert state_of(hass, entity_id) == "armed_away"
    await _handle(
        hass,
        entry,
        GuardModeChanged(
            station_sn=SYNTHETIC.station_sn,
            mode=GuardMode.SCHEDULE,
            active_mode=GuardMode.DISARMED,
            source=EventSource.CLOUD,
        ),
    )
    assert state_of(hass, entity_id) == "disarmed"

    await _unload(hass, entry)


# ── the alarm lifecycle ──────────────────────────────────────────────────────


async def test_lifecycle_alarm_changed_shows_triggered_until_its_end(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The library's alarm start shows triggered; its end the polled mode."""
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)
    assert state_of(hass, entity_id) == "armed_away"
    states = record_states(hass, entity_id)

    await _handle(hass, entry, alarm_changed())
    assert state_of(hass, entity_id) == "triggered"

    await _handle(hass, entry, alarm_changed(False, AlarmStopSource.APP))
    assert state_of(hass, entity_id) == "armed_away"
    assert states == ["triggered", "armed_away"]

    await _unload(hass, entry)


@pytest.mark.parametrize("cipher", [FrameCipher.GCM, None], ids=["gcm", "cloud"])
async def test_lifecycle_an_alarm_push_itself_never_raises_or_ends_triggered(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    cipher: FrameCipher | None,
) -> None:
    """The push's AlarmChanged moves the panel, so a push on both channels moves it once.

    The library emits the alarm push, then the ``AlarmChanged`` it makes of it; the
    panel follows only the second, whichever channel the push came on.
    """
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)
    states = record_states(hass, entity_id)

    await _handle(hass, entry, station_event(PushMessageType.ALARM, t_ms=now_ms(), cipher=cipher))
    assert state_of(hass, entity_id) == "armed_away"
    await _handle(hass, entry, alarm_changed())
    await _handle(
        hass,
        entry,
        station_event(PushMessageType.ALARM, t_ms=now_ms(), cipher=cipher, alarm_type=16),
    )
    assert state_of(hass, entity_id) == "triggered"
    await _handle(hass, entry, alarm_changed(False, AlarmStopSource.APP))
    assert states == ["triggered", "armed_away"]

    await _unload(hass, entry)


async def test_lifecycle_the_p2p_alarm_frames_drive_the_panel_without_push(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The station's tone frames reach the panel through the library, push off.

    A 30 s alarm on the camera's channel shows triggered; the app's stop on the
    station's channel ends it. A repeated start frame is no second transition.
    """
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)
    states = record_states(hass, entity_id)

    fake_station.send_alarm_frame(FrameType.ALARM_TONE_NOTIFY, 3, 30, channel=1)
    await wait_until(lambda: state_of(hass, entity_id) == "triggered")
    fake_station.send_alarm_frame(FrameType.ALARM_TONE_NOTIFY, 3, 30, channel=1)
    fake_station.send_alarm_frame(FrameType.ALARM_TONE_NOTIFY, 16, 0, channel=255)
    await wait_until(lambda: state_of(hass, entity_id) == "armed_away")
    await hass.async_block_till_done()
    assert states == ["triggered", "armed_away"]

    await _unload(hass, entry)


async def _handle(hass: HomeAssistant, entry: MockConfigEntry, event: Event) -> None:
    """Hand ``event`` to the entry's router, as the library would, and let it settle."""
    entry.runtime_data.router.handle(event)
    await hass.async_block_till_done()


async def _unload(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


def _changed_by(hass: HomeAssistant, entity_id: str) -> object:
    state = hass.states.get(entity_id)
    assert state is not None
    return state.attributes.get("changed_by")


async def test_lifecycle_an_entry_delay_shows_pending_for_its_delay(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """An authenticated delay shows pending for alarm_delay seconds from its own time."""
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)

    await _handle(
        hass, entry, station_event(PushMessageType.ALARM_DELAY, t_ms=now_ms(), alarm_delay=30)
    )
    assert state_of(hass, entity_id) == "pending"

    await advance_to_poll(hass, 29)
    assert state_of(hass, entity_id) == "pending"
    await advance_to_poll(hass, 31)
    assert state_of(hass, entity_id) == "armed_away"

    await _unload(hass, entry)


async def test_lifecycle_an_entry_delay_that_already_ran_out_shows_nothing(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A delay delivered after its own delay ran out shows no pending at all."""
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)
    states = record_states(hass, entity_id)

    await _handle(
        hass,
        entry,
        station_event(PushMessageType.ALARM_DELAY, t_ms=now_ms() - 40_000, alarm_delay=30),
    )
    assert state_of(hass, entity_id) == "armed_away"
    assert states == []

    await _unload(hass, entry)


async def test_lifecycle_an_entry_delay_of_zero_seconds_shows_nothing(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Pending for 0 seconds is no pending, not pending until the timeout."""
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)
    states = record_states(hass, entity_id)

    await _handle(
        hass, entry, station_event(PushMessageType.ALARM_DELAY, t_ms=now_ms(), alarm_delay=0)
    )
    assert state_of(hass, entity_id) == "armed_away"
    assert states == []

    await _unload(hass, entry)


async def test_lifecycle_a_trigger_during_the_entry_delay_escalates_and_a_delay_while_triggered_is_ignored(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Pending escalates to triggered, and a later delay never lowers it."""
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)

    await _handle(
        hass, entry, station_event(PushMessageType.ALARM_DELAY, t_ms=now_ms(), alarm_delay=30)
    )
    assert state_of(hass, entity_id) == "pending"
    await _handle(hass, entry, alarm_changed())
    assert state_of(hass, entity_id) == "triggered"
    await _handle(
        hass, entry, station_event(PushMessageType.ALARM_DELAY, t_ms=now_ms(), alarm_delay=30)
    )
    assert state_of(hass, entity_id) == "triggered"

    await advance_to_poll(hass, 31)
    assert state_of(hass, entity_id) == "triggered"

    await _unload(hass, entry)


async def test_lifecycle_an_entry_delay_older_than_the_last_stop_is_ignored(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A replayed older stop does not end pending, and a pre-stop delay cannot raise it."""
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)
    now = now_ms()

    await _handle(hass, entry, station_event(PushMessageType.ALARM_DELAY, t_ms=now, alarm_delay=30))
    assert state_of(hass, entity_id) == "pending"
    await _handle(hass, entry, station_event(PushMessageType.ALARM, t_ms=now - 5000, alarm_type=15))
    assert state_of(hass, entity_id) == "pending"
    await _handle(hass, entry, station_event(PushMessageType.ALARM, t_ms=now, alarm_type=16))
    assert state_of(hass, entity_id) == "armed_away"

    await _handle(
        hass,
        entry,
        station_event(PushMessageType.ALARM_DELAY, t_ms=now - 5000, alarm_delay=3600),
    )
    assert state_of(hass, entity_id) == "armed_away"
    # Equal seconds are accepted: showing an alarm is the fail-loud direction.
    await _handle(hass, entry, station_event(PushMessageType.ALARM_DELAY, t_ms=now, alarm_delay=30))
    assert state_of(hass, entity_id) == "pending"

    await _unload(hass, entry)


async def test_lifecycle_a_stop_without_an_event_time_clears_nothing(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A stop that cannot be ordered ends no entry delay and is not the last stop."""
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)
    now = now_ms()

    await _handle(hass, entry, station_event(PushMessageType.ALARM_DELAY, t_ms=now, alarm_delay=30))
    await _handle(hass, entry, station_event(PushMessageType.ALARM, t_ms=None, alarm_type=16))
    assert state_of(hass, entity_id) == "pending"

    # A later ordered stop still ends it.
    await _handle(hass, entry, station_event(PushMessageType.ALARM, t_ms=now, alarm_type=16))
    assert state_of(hass, entity_id) == "armed_away"

    await _unload(hass, entry)


async def test_lifecycle_a_replayed_stop_does_not_clear_an_alarm_with_no_event_time(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A timeless entry delay ends only on a stop newer than every stop seen."""
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)
    now = now_ms()
    stop = station_event(PushMessageType.ALARM, t_ms=now, alarm_type=16)

    await _handle(hass, entry, stop)
    assert state_of(hass, entity_id) == "armed_away"
    await _handle(hass, entry, station_event(PushMessageType.ALARM_DELAY, t_ms=None))
    assert state_of(hass, entity_id) == "pending"

    # The stop seen before the delay, replayed.
    await _handle(hass, entry, stop)
    assert state_of(hass, entity_id) == "pending"

    await _handle(hass, entry, station_event(PushMessageType.ALARM, t_ms=now + 2000, alarm_type=16))
    assert state_of(hass, entity_id) == "armed_away"

    await _unload(hass, entry)


async def test_lifecycle_a_replayed_disarm_push_does_not_clear_an_alarm_with_no_event_time(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A disarm push no newer than a disarm already seen is a replay."""
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)
    now = now_ms()
    disarm = station_event(PushMessageType.ARMING, t_ms=now, guard_mode=63, arming_user=2)

    await _handle(hass, entry, disarm)
    await _handle(hass, entry, station_event(PushMessageType.ALARM_DELAY, t_ms=None))
    assert state_of(hass, entity_id) == "pending"

    await _handle(hass, entry, disarm)
    assert state_of(hass, entity_id) == "pending"

    await _handle(
        hass,
        entry,
        station_event(PushMessageType.ARMING, t_ms=now + 2000, guard_mode=63, arming_user=1),
    )
    assert state_of(hass, entity_id) != "pending"
    assert _changed_by(hass, entity_id) == "Keypad"

    await _unload(hass, entry)


@pytest.mark.parametrize("evidence", ["stop", "disarm"])
async def test_lifecycle_evidence_stamped_ahead_of_the_host_clock_does_not_mute_a_later_entry_delay(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    evidence: str,
) -> None:
    """A station clock five minutes ahead, then set back, mutes no genuine delay.

    The library accepts event times up to ten minutes ahead of the host. An entry
    delay stamped at host time, the same second as the host clock when the evidence
    came, still shows pending.
    """
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)
    ahead = now_ms() + 300_000
    if evidence == "stop":
        early = station_event(PushMessageType.ALARM, t_ms=ahead, alarm_type=16)
    else:
        early = station_event(PushMessageType.ARMING, t_ms=ahead, guard_mode=63, arming_user=2)

    await _handle(hass, entry, early)
    await _handle(
        hass, entry, station_event(PushMessageType.ALARM_DELAY, t_ms=now_ms(), alarm_delay=30)
    )
    assert state_of(hass, entity_id) == "pending"

    await _unload(hass, entry)


async def test_lifecycle_a_replayed_stop_stamped_ahead_still_does_not_clear_a_timeless_delay(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Capping the floor for new delays leaves stops ordered in full.

    The stop is stamped five minutes ahead, so the floor that orders new delays holds
    the host time instead. Its replay is still no newer than the stop already seen.
    """
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)
    stop = station_event(PushMessageType.ALARM, t_ms=now_ms() + 300_000, alarm_type=16)

    await _handle(hass, entry, stop)
    await _handle(hass, entry, station_event(PushMessageType.ALARM_DELAY, t_ms=None))
    assert state_of(hass, entity_id) == "pending"

    await _handle(hass, entry, stop)
    assert state_of(hass, entity_id) == "pending"

    await _unload(hass, entry)


async def test_lifecycle_unauthenticated_station_evidence_never_changes_the_panel(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """ECB pushes reach the station signal and never touch the panel."""
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)
    alarm_event_id = entity_id_for(hass, EVENT_DOMAIN, SYNTHETIC.station_sn, ALARM_EVENT_KEY)
    signal = detections.station_signal(entry.entry_id, SYNTHETIC.station_sn)
    states = record_states(hass, entity_id)

    for event, event_type in (
        (
            station_event(PushMessageType.ALARM, t_ms=now_ms(), cipher=FrameCipher.ECB),
            "alarm_triggered",
        ),
        (
            station_event(
                PushMessageType.ALARM_DELAY, t_ms=now_ms(), cipher=FrameCipher.ECB, alarm_delay=30
            ),
            "alarm_delay",
        ),
    ):
        await _handle(hass, entry, event)
        assert state_of(hass, entity_id) == "armed_away", event_type
        assert _changed_by(hass, entity_id) is None
        # Not vacuous: the push really reached the station signal.
        alarm_event = hass.states.get(alarm_event_id)
        assert alarm_event is not None
        assert alarm_event.attributes["event_type"] == event_type
        assert alarm_event.attributes["authenticated"] is False

    # The router never sends an ECB arming push; dispatch it straight.
    async_dispatcher_send(
        hass,
        signal,
        station_event(
            PushMessageType.ARMING,
            t_ms=now_ms(),
            cipher=FrameCipher.ECB,
            guard_mode=0,
            arming_user=1,
        ),
    )
    await hass.async_block_till_done()
    assert state_of(hass, entity_id) == "armed_away"
    assert _changed_by(hass, entity_id) is None
    assert states == []

    await _handle(hass, entry, alarm_changed())
    assert state_of(hass, entity_id) == "triggered"
    # Nor an ECB stop, which the library withholds and the router drops.
    async_dispatcher_send(
        hass,
        signal,
        station_event(PushMessageType.ALARM, t_ms=now_ms(), cipher=FrameCipher.ECB, alarm_type=15),
    )
    await hass.async_block_till_done()
    assert state_of(hass, entity_id) == "triggered"

    await _unload(hass, entry)


async def test_lifecycle_the_alarm_timeout_falls_back_to_the_guard_mode_and_requests_a_refresh(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The poll never clears an alarm; the timeout does, with exactly one refresh."""
    entry = await set_up_warm(hass, seed_warm_cache, options={CONF_ALARM_TIMEOUT: 1})
    entity_id = panel_entity_id(hass)
    coordinator = entry.runtime_data.coordinators[SYNTHETIC.station_sn]
    requests: list[None] = []

    async def _count_refresh() -> None:
        requests.append(None)

    coordinator.async_request_refresh = _count_refresh

    await _handle(hass, entry, alarm_changed())
    assert state_of(hass, entity_id) == "triggered"

    await advance_to_poll(hass, 50)
    assert state_of(hass, entity_id) == "triggered"
    assert requests == []

    await advance_to_poll(hass, 61)
    assert state_of(hass, entity_id) == "armed_away"
    assert len(requests) == 1

    await _unload(hass, entry)


async def test_lifecycle_triggered_survives_an_entry_reload_until_its_own_timeout(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A reload mid-alarm keeps triggered, and the timeout keeps its first deadline."""
    entry = await set_up_warm(hass, seed_warm_cache, options={CONF_ALARM_TIMEOUT: 1})
    anchor = dt_util.utcnow()
    entity_id = panel_entity_id(hass)

    await _handle(hass, entry, alarm_changed())
    assert state_of(hass, entity_id) == "triggered"

    await advance_to_poll(hass, 30, anchor=anchor)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert state_of(hass, entity_id) == "triggered"

    # The restored alarm still orders a replayed disarm push against its own second.
    await _handle(
        hass,
        entry,
        station_event(
            PushMessageType.ARMING, t_ms=now_ms() - 120_000, guard_mode=63, arming_user=2
        ),
    )
    assert state_of(hass, entity_id) == "triggered"

    await advance_to_poll(hass, 62, anchor=anchor)
    await wait_until(lambda: state_of(hass, entity_id) == "armed_away")

    await _unload(hass, entry)


async def test_lifecycle_pending_survives_an_entry_reload_until_its_entry_delay_ends(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A reload during the entry delay keeps pending, ending when the delay does."""
    entry = await set_up_warm(hass, seed_warm_cache)
    anchor = dt_util.utcnow()
    entity_id = panel_entity_id(hass)

    await _handle(
        hass, entry, station_event(PushMessageType.ALARM_DELAY, t_ms=now_ms(), alarm_delay=30)
    )
    assert state_of(hass, entity_id) == "pending"

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert state_of(hass, entity_id) == "pending"

    await advance_to_poll(hass, 31, anchor=anchor)
    await wait_until(lambda: state_of(hass, entity_id) == "armed_away")

    await _unload(hass, entry)


async def test_lifecycle_triggered_is_not_restored_by_a_later_home_assistant_run(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Restore data outlives a restart, but a stop pushed while HA was down is lost.

    The alarm is saved to storage and loaded back, as a shutdown and the next start do,
    and the run's token is dropped, as a new process starts without it. The station is
    still armed away, so no poll would ever end a restored alarm.
    """
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)

    await _handle(hass, entry, alarm_changed())
    assert state_of(hass, entity_id) == "triggered"

    await _unload(hass, entry)
    await async_mock_restore_state_shutdown_restart(hass)
    hass.data.pop(f"{DOMAIN}_restore_run", None)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert state_of(hass, entity_id) == "armed_away"

    await _unload(hass, entry)


async def test_lifecycle_a_reload_that_lowers_the_alarm_timeout_shortens_a_restored_alarm(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A user who lowers the timeout of a stuck alarm gets the new timeout."""
    entry = await set_up_warm(hass, seed_warm_cache)
    anchor = dt_util.utcnow()
    entity_id = panel_entity_id(hass)

    await _handle(hass, entry, alarm_changed())
    assert state_of(hass, entity_id) == "triggered"

    hass.config_entries.async_update_entry(entry, options={CONF_ALARM_TIMEOUT: 1})
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert state_of(hass, entity_id) == "triggered"

    await advance_to_poll(hass, 62, anchor=anchor)
    await wait_until(lambda: state_of(hass, entity_id) == "armed_away")

    await _unload(hass, entry)


async def test_lifecycle_a_wall_clock_step_back_does_not_stretch_a_restored_alarm(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The panel's wall clock steps an hour back across the reload.

    The restored timeout still runs out at its first deadline, one minute in.
    """
    entry = await set_up_warm(hass, seed_warm_cache, options={CONF_ALARM_TIMEOUT: 1})
    anchor = dt_util.utcnow()
    entity_id = panel_entity_id(hass)

    await _handle(hass, entry, alarm_changed())
    assert state_of(hass, entity_id) == "triggered"

    real_utcnow = dt_util.utcnow
    with monkeypatch.context() as patch:
        patch.setattr(
            alarm_control_panel,
            "dt_util",
            SimpleNamespace(utcnow=lambda: real_utcnow() - timedelta(hours=1)),
        )
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
    assert state_of(hass, entity_id) == "triggered"

    await advance_to_poll(hass, 62, anchor=anchor)
    await wait_until(lambda: state_of(hass, entity_id) == "armed_away")

    await _unload(hass, entry)


async def test_lifecycle_a_restored_alarm_ends_on_a_setup_poll_showing_disarmed(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The station was disarmed during the reload; the reload's own poll says so.

    The alarm began armed away, so that poll confirms the disarm, and the panel never
    flaps triggered and then disarmed a poll later. The ended alarm still raises the
    ordering floor, so a replayed older entry delay stays refused.
    """
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)
    t_ms = now_ms()

    await _handle(hass, entry, alarm_changed())
    assert state_of(hass, entity_id) == "triggered"

    set_guard_mode(fake_station, GuardMode.DISARMED)
    states = record_states(hass, entity_id)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert state_of(hass, entity_id) == "disarmed"
    assert "triggered" not in states

    await _handle(
        hass,
        entry,
        station_event(PushMessageType.ALARM_DELAY, t_ms=t_ms - 120_000, alarm_delay=3600),
    )
    assert state_of(hass, entity_id) == "disarmed"

    await _unload(hass, entry)


# Restore data a reload in this run could hand back, by the fields it varies.
# Each deadline is seconds from the event loop's time now; "run" None drops the token.
_RESTORE_CASES: dict[str, tuple[dict[str, object], str]] = {
    "triggered": ({}, "triggered"),
    "pending": ({"lifecycle": "pending", "phase_ends_at": 20}, "pending"),
    "partial fields ignored": (
        {"alarm_second": "1", "floor_second": True, "mode_at_alarm": 9999},
        "triggered",
    ),
    "no run token": ({"run": None}, "armed_away"),
    "timeout ran out": ({"timeout_at": -1}, "armed_away"),
    "entry delay ran out": ({"lifecycle": "pending", "phase_ends_at": -1}, "armed_away"),
    "no timeout": ({"timeout_at": None}, "armed_away"),
    "timeout not a number": ({"timeout_at": True}, "armed_away"),
    "arming": ({"lifecycle": "arming"}, "armed_away"),
    "unknown lifecycle": ({"lifecycle": "bogus"}, "armed_away"),
    "lifecycle not a string": ({"lifecycle": 5}, "armed_away"),
}


@pytest.mark.parametrize(("changes", "expected"), _RESTORE_CASES.values(), ids=_RESTORE_CASES)
async def test_lifecycle_restore_data_is_taken_back_only_when_whole_and_live(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    changes: dict[str, object],
    expected: str,
) -> None:
    """Each branch that decides whether restore data brings an alarm back.

    The entry is set up once to learn the panel, unloaded, and set up again over
    restore data from this run. The first state written is checked, so a refused
    alarm is never shown, not even until an overdue timer ends it.
    """
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)
    await _unload(hass, entry)

    now = hass.loop.time()
    data: dict[str, object] = {
        "run": alarm_control_panel.restore_run_token(hass),
        "lifecycle": "triggered",
        "alarm_second": None,
        "floor_second": None,
        "alarm_floor_second": None,
        "mode_at_alarm": int(GuardMode.AWAY),
        "timeout_at": 60,
        "phase_ends_at": None,
    } | changes
    for key in ("timeout_at", "phase_ends_at"):
        offset = data[key]
        if isinstance(offset, int) and not isinstance(offset, bool):
            data[key] = now + offset
    mock_restore_cache_with_extra_data(hass, [(State(entity_id, "triggered"), data)])

    states = record_states(hass, entity_id)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert states[0] == expected, states

    await _unload(hass, entry)


async def test_lifecycle_a_restored_alarm_raised_while_disarmed_survives_a_disarmed_poll(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The mode the alarm began in survives the reload as a mode.

    The alarm was raised while disarmed, so the reload's disarmed poll proves nothing.
    """
    set_guard_mode(fake_station, GuardMode.DISARMED)
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)

    await _handle(hass, entry, alarm_changed())
    assert state_of(hass, entity_id) == "triggered"

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert state_of(hass, entity_id) == "triggered"

    await _unload(hass, entry)


def _mode_report(mode: GuardMode) -> GuardModeChanged:
    """The mode report the library emits after an arming push, in the same tick."""
    return GuardModeChanged(station_sn=SYNTHETIC.station_sn, mode=mode, source=EventSource.P2P)


def _arming_push(guard_mode: int, **fields: object) -> SecurityEvent:
    return station_event(PushMessageType.ARMING, t_ms=now_ms(), guard_mode=guard_mode, **fields)


async def test_lifecycle_a_poll_showing_disarmed_after_an_armed_start_is_a_confirmed_disarm(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The alarm began armed away, so a disarmed poll confirms a disarm."""
    entry = await set_up_warm(hass, seed_warm_cache)
    anchor = dt_util.utcnow()
    entity_id = panel_entity_id(hass)

    await _handle(hass, entry, alarm_changed())
    assert state_of(hass, entity_id) == "triggered"

    set_guard_mode(fake_station, GuardMode.DISARMED)
    await advance_to_poll(hass, 46, anchor=anchor)
    await wait_until(lambda: state_of(hass, entity_id) == "disarmed")

    await _unload(hass, entry)


async def test_lifecycle_a_disarmed_read_does_not_clear_an_alarm_raised_while_disarmed(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A disarmed read proves nothing about an alarm raised while disarmed."""
    set_guard_mode(fake_station, GuardMode.DISARMED)
    entry = await set_up_warm(hass, seed_warm_cache)
    anchor = dt_util.utcnow()
    entity_id = panel_entity_id(hass)
    coordinator = entry.runtime_data.coordinators[SYNTHETIC.station_sn]

    await _handle(hass, entry, alarm_changed())
    assert state_of(hass, entity_id) == "triggered"

    before = coordinator.data
    await advance_to_poll(hass, 46, anchor=anchor)
    # Each poll stores a fresh state object: proof the poll really ran.
    await wait_until(lambda: coordinator.data is not before)
    await hass.async_block_till_done()
    assert state_of(hass, entity_id) == "triggered"

    await _handle(hass, entry, alarm_changed(False))
    assert state_of(hass, entity_id) == "disarmed"

    await _unload(hass, entry)


async def test_lifecycle_the_disarm_service_clears_triggered(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Home Assistant's own disarm, confirmed by the station, ends the alarm."""
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)

    await _handle(hass, entry, alarm_changed())
    assert state_of(hass, entity_id) == "triggered"

    await call_panel(hass, SERVICE_ALARM_DISARM, entity_id)
    await hass.async_block_till_done()
    assert state_of(hass, entity_id) == "disarmed"

    await _unload(hass, entry)


async def test_lifecycle_a_delay_older_than_an_ended_alarm_cannot_raise_it_again(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """An alarm ended by a disarm leaves its second as the floor for replays."""
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)
    earlier = station_event(PushMessageType.ALARM_DELAY, t_ms=now_ms() - 120_000, alarm_delay=3600)

    await _handle(hass, entry, alarm_changed())
    assert state_of(hass, entity_id) == "triggered"
    await call_panel(hass, SERVICE_ALARM_DISARM, entity_id)
    await hass.async_block_till_done()
    assert state_of(hass, entity_id) == "disarmed"

    await _handle(hass, entry, earlier)
    assert state_of(hass, entity_id) == "disarmed"

    await _unload(hass, entry)


async def test_lifecycle_a_replayed_entry_delay_cannot_cut_pending_short(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """While pending, an older or timeless delay neither shortens nor cancels the delay."""
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)
    now = now_ms()

    await _handle(hass, entry, station_event(PushMessageType.ALARM_DELAY, t_ms=now, alarm_delay=30))
    assert state_of(hass, entity_id) == "pending"

    # An older delay with a shorter remaining time.
    await _handle(
        hass, entry, station_event(PushMessageType.ALARM_DELAY, t_ms=now - 5000, alarm_delay=10)
    )
    await advance_to_poll(hass, 6)
    assert state_of(hass, entity_id) == "pending"

    # A timeless delay naming no delay at all.
    await _handle(hass, entry, station_event(PushMessageType.ALARM_DELAY, t_ms=None))
    await advance_to_poll(hass, 31)
    assert state_of(hass, entity_id) == "armed_away"

    await _unload(hass, entry)


async def test_lifecycle_an_authenticated_disarming_push_clears_triggered(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The station's own authenticated report of a disarm ends the alarm."""
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)

    await _handle(hass, entry, alarm_changed())
    assert state_of(hass, entity_id) == "triggered"

    await _handle(hass, entry, _arming_push(63, arming_user=2))
    assert state_of(hass, entity_id) != "triggered"
    assert _changed_by(hass, entity_id) == "App"

    await _unload(hass, entry)


@pytest.mark.parametrize(
    ("offset_ms", "why"),
    [(-120_000, "older than the alarm"), (None, "with no event time")],
)
async def test_lifecycle_a_disarming_push_that_cannot_follow_the_alarm_clears_nothing(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    offset_ms: int | None,
    why: str,
) -> None:
    """A replayed or timeless disarm push neither ends nor attributes.

    The alarm is the library's ``AlarmChanged``, stamped with the host clock less
    ``ALARM_CHANGED_SKEW_SECONDS``: a push two minutes older cannot follow it.
    """
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)
    now = now_ms()

    await _handle(hass, entry, alarm_changed())
    assert state_of(hass, entity_id) == "triggered"

    t_ms = None if offset_ms is None else now + offset_ms
    await _handle(
        hass,
        entry,
        station_event(PushMessageType.ARMING, t_ms=t_ms, guard_mode=63, arming_user=2),
    )
    assert state_of(hass, entity_id) == "triggered", why
    assert _changed_by(hass, entity_id) is None, why

    # A disarm push ordered at or after the alarm still ends it.
    await _handle(
        hass, entry, station_event(PushMessageType.ARMING, t_ms=now, guard_mode=63, arming_user=2)
    )
    assert state_of(hass, entity_id) != "triggered"
    assert _changed_by(hass, entity_id) == "App"

    await _unload(hass, entry)


async def test_lifecycle_a_pushed_disarmed_read_does_not_clear_triggered_but_a_poll_does(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A pushed mode or state cannot be ordered against the alarm; only a poll confirms."""
    entry = await set_up_warm(hass, seed_warm_cache)
    anchor = dt_util.utcnow()
    entity_id = panel_entity_id(hass)
    coordinator = entry.runtime_data.coordinators[SYNTHETIC.station_sn]

    await _handle(hass, entry, alarm_changed())
    assert state_of(hass, entity_id) == "triggered"

    await _handle(hass, entry, _mode_report(GuardMode.DISARMED))
    assert state_of(hass, entity_id) == "triggered"
    state = dataclasses.replace(
        coordinator.data, guard_mode=GuardMode.DISARMED, active_mode=GuardMode.DISARMED
    )
    await _handle(hass, entry, StationStateChanged(station_sn=SYNTHETIC.station_sn, state=state))
    assert state_of(hass, entity_id) == "triggered"

    set_guard_mode(fake_station, GuardMode.DISARMED)
    await advance_to_poll(hass, 46, anchor=anchor)
    await wait_until(lambda: state_of(hass, entity_id) == "disarmed")

    await _unload(hass, entry)


async def test_lifecycle_an_arming_push_with_an_exit_delay_shows_arming_through_its_own_mode_report(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The push's own same-tick mode report cannot end the exit delay."""
    set_guard_mode(fake_station, GuardMode.DISARMED)
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)
    router = entry.runtime_data.router

    # One synchronous block, as the library's client delivers them.
    router.handle(_arming_push(0, alarm_delay=30, arming_user=1))
    router.handle(_mode_report(GuardMode.AWAY))
    await hass.async_block_till_done()
    assert state_of(hass, entity_id) == "arming"
    assert _changed_by(hass, entity_id) == "Keypad"

    await advance_to_poll(hass, 10)
    await _handle(hass, entry, _mode_report(GuardMode.AWAY))
    assert state_of(hass, entity_id) == "arming"

    await advance_to_poll(hass, 31)
    assert state_of(hass, entity_id) == "arming"
    await _handle(hass, entry, _mode_report(GuardMode.AWAY))
    assert state_of(hass, entity_id) == "armed_away"

    await _unload(hass, entry)


async def test_lifecycle_arming_ends_at_the_first_poll_after_the_exit_delay(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Once the exit delay has run out, the next poll ends arming."""
    set_guard_mode(fake_station, GuardMode.DISARMED)
    entry = await set_up_warm(hass, seed_warm_cache)
    anchor = dt_util.utcnow()
    entity_id = panel_entity_id(hass)
    router = entry.runtime_data.router

    router.handle(_arming_push(0, alarm_delay=30, arming_user=1))
    router.handle(_mode_report(GuardMode.AWAY))
    await hass.async_block_till_done()
    set_guard_mode(fake_station, GuardMode.AWAY)
    assert state_of(hass, entity_id) == "arming"

    await advance_to_poll(hass, 31)
    assert state_of(hass, entity_id) == "arming"

    await advance_to_poll(hass, 46, anchor=anchor)
    await wait_until(lambda: state_of(hass, entity_id) == "armed_away")

    await _unload(hass, entry)


async def test_lifecycle_a_disarmed_read_during_the_exit_delay_ends_arming(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A disarmed read ends arming before its exit delay runs out."""
    set_guard_mode(fake_station, GuardMode.DISARMED)
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)

    await _handle(hass, entry, _arming_push(0, alarm_delay=30, arming_user=1))
    assert state_of(hass, entity_id) == "arming"

    await _handle(hass, entry, _mode_report(GuardMode.DISARMED))
    assert state_of(hass, entity_id) == "disarmed"

    await _unload(hass, entry)


async def test_lifecycle_changed_by_is_the_arming_source_label(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """``changed_by`` names the authenticated source, never the sent name."""
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)

    for arming_user, label in ((1, "Keypad"), (5, "Key fob"), (2, "App"), (9, "App")):
        await _handle(hass, entry, _arming_push(0, arming_user=arming_user, user_name="Mallory"))
        state = hass.states.get(entity_id)
        assert state is not None
        assert state.state == "armed_away", arming_user
        assert state.attributes["changed_by"] == label, arming_user
        assert "Mallory" not in json.dumps(dict(state.attributes))

    await _handle(hass, entry, _arming_push(0, user_name="Mallory"))
    assert _changed_by(hass, entity_id) is None

    await _unload(hass, entry)


async def test_lifecycle_a_poll_showing_an_armed_mode_keeps_triggered(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The 45 s poll never clears an alarm on its own, and neither does a pushed armed mode."""
    entry = await set_up_warm(hass, seed_warm_cache)
    anchor = dt_util.utcnow()
    entity_id = panel_entity_id(hass)
    coordinator = entry.runtime_data.coordinators[SYNTHETIC.station_sn]

    await _handle(hass, entry, alarm_changed())
    before = coordinator.data
    await advance_to_poll(hass, 46, anchor=anchor)
    await wait_until(lambda: coordinator.data is not before)
    await hass.async_block_till_done()
    assert state_of(hass, entity_id) == "triggered"

    await _handle(hass, entry, _mode_report(GuardMode.HOME))
    assert state_of(hass, entity_id) == "triggered"

    await _unload(hass, entry)


async def test_lifecycle_a_trigger_during_the_exit_delay_replaces_arming(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A trigger ends arming, and the exit delay running out later changes nothing."""
    set_guard_mode(fake_station, GuardMode.DISARMED)
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)

    await _handle(hass, entry, _arming_push(0, alarm_delay=30, arming_user=1))
    assert state_of(hass, entity_id) == "arming"
    await _handle(hass, entry, alarm_changed())
    assert state_of(hass, entity_id) == "triggered"

    await advance_to_poll(hass, 31)
    await _handle(hass, entry, _mode_report(GuardMode.AWAY))
    assert state_of(hass, entity_id) == "triggered"

    await _unload(hass, entry)


async def test_lifecycle_the_alarm_timeout_ends_a_stuck_arming(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Arming outlasting the timeout ends there, with one refresh; the poll alone does not."""
    set_guard_mode(fake_station, GuardMode.DISARMED)
    entry = await set_up_warm(hass, seed_warm_cache, options={CONF_ALARM_TIMEOUT: 1})
    anchor = dt_util.utcnow()
    entity_id = panel_entity_id(hass)
    coordinator = entry.runtime_data.coordinators[SYNTHETIC.station_sn]
    requests: list[None] = []

    async def _count_refresh() -> None:
        requests.append(None)

    coordinator.async_request_refresh = _count_refresh

    # An exit delay longer than the one-minute timeout.
    await _handle(hass, entry, _arming_push(0, alarm_delay=120, arming_user=1))
    set_guard_mode(fake_station, GuardMode.AWAY)
    assert state_of(hass, entity_id) == "arming"

    await advance_to_poll(hass, 50, anchor=anchor)
    await wait_until(lambda: coordinator.data.guard_mode == GuardMode.AWAY)
    await hass.async_block_till_done()
    assert state_of(hass, entity_id) == "arming"
    assert requests == []

    await advance_to_poll(hass, 61)
    await wait_until(lambda: state_of(hass, entity_id) == "armed_away")
    assert len(requests) == 1

    await _unload(hass, entry)


async def test_lifecycle_a_pushed_station_state_is_a_mode_read_that_ends_arming(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A StationStateChanged after the exit delay ends arming, as a poll would."""
    set_guard_mode(fake_station, GuardMode.DISARMED)
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)
    coordinator = entry.runtime_data.coordinators[SYNTHETIC.station_sn]

    await _handle(hass, entry, _arming_push(0, alarm_delay=30, arming_user=1))
    await advance_to_poll(hass, 31)
    assert state_of(hass, entity_id) == "arming"

    state = dataclasses.replace(
        coordinator.data, guard_mode=GuardMode.AWAY, active_mode=GuardMode.AWAY
    )
    await _handle(hass, entry, StationStateChanged(station_sn=SYNTHETIC.station_sn, state=state))
    assert state_of(hass, entity_id) == "armed_away"

    await _unload(hass, entry)
