"""An entry sensor's live door state and the alarm panel's entry delay.

The station's parameter notify moves the door entity at once, without a poll; the
station's alarm-delay frame shows the panel pending until the alarm, an end, a disarm
or the delay's own expiry.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, Final

import pytest
from conftest import (
    add_entry,
    advance_to_poll,
    alarm_changed,
    entity_id_for,
    now_ms,
    panel_entity_id,
    record_states,
    set_up_warm,
    setting_param,
    setup_entry,
    state_of,
    station_event,
    wait_until,
)
from eufy_home_security import (
    AlarmChanged,
    AlarmDelayChanged,
    EufySecurity,
    Event,
    EventSource,
    GuardMode,
    GuardModeChanged,
    GuardModeOrigin,
    PushMessageType,
    StationState,
)
from eufy_home_security.p2p import FrameType
from eufy_home_security.testing import SYNTHETIC, FakeCloud, FakeStation, camera_device
from homeassistant.components.alarm_control_panel import DOMAIN as ALARM_DOMAIN
from homeassistant.const import (
    ATTR_ENTITY_ID,
    EVENT_STATE_CHANGED,
    EVENT_STATE_REPORTED,
    SERVICE_ALARM_DISARM,
    Platform,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.util import dt as dt_util
from pytest_homeassistant.common import (
    MockConfigEntry,
    async_fire_time_changed_exact,
)

from custom_components.eufy_home_security import detections
from custom_components.eufy_home_security.const import (
    ATTR_DELAY_SECONDS,
    ATTR_DELAY_UNTIL,
    ATTR_VALUE_SOURCE,
)
from custom_components.eufy_home_security.coordinator import ModeRead

DOOR_SN: Final = "T8900P0000000001"
_DOOR_CHANNEL: Final = 2
_STATION_CHANNEL: Final = 255
_DOOR_PARAM: Final = 1550
_RSSI_PARAM: Final = 1141
# The HomeBase's notify of several parameters of one device at once.
_PARAM_LIST_CMD: Final = 1829
# An entry sensor's parameters as a HomeBase 3 lists them: battery, RSSI, door and
# three more, each value a string.
_SENSOR_PARAMS: Final = {
    1101: "100",
    1601: "0",
    _RSSI_PARAM: "-65",
    1243: "2",
    61016: "0",
    _DOOR_PARAM: "0",
}
# The alarm-delay frame's alarm type for a triggered sensor.
_SENSOR_TRIGGERED: Final = 6


def _add_door(station: FakeStation, cloud: FakeCloud) -> None:
    """Pair a closed entry sensor; call before ``seed_warm_cache``."""
    cloud.devices.append(
        camera_device(DOOR_SN, station_sn=station.serial, channel=_DOOR_CHANNEL, name="Door")
    )
    station.params[_DOOR_CHANNEL] = dict(_SENSOR_PARAMS)


def _door_list(door: str, rssi: str) -> dict[str, object]:
    """The HomeBase's parameter list notify about the sensor: every value a string."""
    values = {**_SENSOR_PARAMS, _RSSI_PARAM: rssi, _DOOR_PARAM: door}
    return {
        "cmd": _PARAM_LIST_CMD,
        "payload": {
            "params": [
                {"dev_type": _DOOR_CHANNEL, "param_type": param, "param_value": value}
                for param, value in values.items()
            ]
        },
    }


def _send_door(station: FakeStation, status: int, *, shape: str = "single") -> None:
    """The station's door notify about the entry sensor's channel.

    ``single``: the door value alone; ``list``: the parameter list, its RSSI moving
    with the door.
    """
    if shape == "list":
        obj = _door_list(str(status), "-61" if status else "-63")
    else:
        obj = {"cmd": _DOOR_PARAM, "payload": {"status": status}}
    station.send_json(FrameType.NOTIFY_PAYLOAD, obj, device_channel=_DOOR_CHANNEL)


async def _handle(hass: HomeAssistant, entry: MockConfigEntry, event: Event) -> None:
    """Hand ``event`` to the entry's router, as the library would, and let it settle."""
    entry.runtime_data.router.handle(event)
    await hass.async_block_till_done()


async def _unload(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def _at(hass: HomeAssistant, anchor: datetime, seconds: float) -> None:
    """Fire the timers due ``seconds`` after ``anchor``, to the microsecond."""
    async_fire_time_changed_exact(hass, anchor + timedelta(seconds=seconds))
    await hass.async_block_till_done()


# ── the door ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("shape", ["single", "list"])
async def test_a_door_notify_moves_the_door_entity_without_a_poll(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    shape: str,
) -> None:
    """Open, then closed, each from the station's notify alone."""
    _add_door(fake_station, fake_cloud)
    seed_warm_cache()
    entry = add_entry(hass)
    assert await setup_entry(hass, entry)
    entity_id = entity_id_for(hass, Platform.BINARY_SENSOR, DOOR_SN, "sensor_door_status")
    assert state_of(hass, entity_id) == "off"
    states = record_states(hass, entity_id)

    _send_door(fake_station, 1, shape=shape)
    await wait_until(lambda: state_of(hass, entity_id) == "on")
    _send_door(fake_station, 0, shape=shape)
    await wait_until(lambda: state_of(hass, entity_id) == "off")
    assert states == ["on", "off"]
    await _unload(hass, entry)


async def test_a_station_setting_from_a_notify_says_its_value_source_is_notify(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A station value a notify set shows at once, with ``value_source: notify``."""
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = entity_id_for(hass, Platform.NUMBER, SYNTHETIC.station_sn, "alarm_volume_value")
    assert _attributes(hass, entity_id)[ATTR_VALUE_SOURCE] == "app_default"

    param = setting_param("alarm_volume_value", model="T8030")
    fake_station.send_json(
        FrameType.NOTIFY_PAYLOAD,
        {"cmd": param, "payload": {"type": "updateCameraInfo", "value": 7}},
        device_channel=_STATION_CHANNEL,
    )
    await wait_until(lambda: _attributes(hass, entity_id)[ATTR_VALUE_SOURCE] == "notify")
    assert state_of(hass, entity_id) == "7"
    await _unload(hass, entry)


# ── the entry delay ──────────────────────────────────────────────────────────


def _delay(
    delaying: bool = True,
    seconds: int | None = 30,
    *,
    ago: float = 0.0,
    at: datetime | None = None,
) -> AlarmDelayChanged:
    """The library's ``AlarmDelayChanged`` of a HomeBase 3: the station's channel."""
    return AlarmDelayChanged(
        station_sn=SYNTHETIC.station_sn,
        delaying=delaying,
        channel=_STATION_CHANNEL,
        alarm_type=_SENSOR_TRIGGERED if delaying else None,
        seconds=seconds if delaying else None,
        at=(at or dt_util.utcnow()) - timedelta(seconds=ago),
    )


def _delay_push(at: datetime, *, skew: float = 0.0, seconds: int = 30) -> Event:
    """An authenticated cloud delay push, stamped ``skew`` seconds off ``at``."""
    t_ms = int((at.timestamp() + skew) * 1000)
    return station_event(PushMessageType.ALARM_DELAY, t_ms=t_ms, alarm_delay=seconds)


def _until(hass: HomeAssistant, entity_id: str) -> datetime:
    """The shown ``delay_until``."""
    until = dt_util.parse_datetime(str(_attributes(hass, entity_id)[ATTR_DELAY_UNTIL]))
    assert until is not None
    return until


def _record_writes(hass: HomeAssistant, entity_id: str) -> list[tuple[str, object]]:
    """Every state write of ``entity_id``, a repeat of the shown state included: its
    state and ``delay_until``."""
    writes: list[tuple[str, object]] = []

    @callback
    def _ours(data: Any) -> bool:
        return bool(data["entity_id"] == entity_id and data["new_state"] is not None)

    @callback
    def _on_write(event: Any) -> None:
        new = event.data["new_state"]
        writes.append((new.state, new.attributes.get(ATTR_DELAY_UNTIL)))

    hass.bus.async_listen(EVENT_STATE_CHANGED, _on_write, event_filter=_ours)
    hass.bus.async_listen(EVENT_STATE_REPORTED, _on_write, event_filter=_ours)
    return writes


def _attributes(hass: HomeAssistant, entity_id: str) -> dict[str, object]:
    state = hass.states.get(entity_id)
    assert state is not None
    return dict(state.attributes)


async def test_the_alarm_delay_frame_shows_pending_until_it_runs_out(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The station's frame shows pending with its seconds; no end frame comes.

    The countdown ends on its own 2 s after ``seconds``: back to the mode in force.
    """
    entry = await set_up_warm(hass, seed_warm_cache)
    anchor = dt_util.utcnow()
    entity_id = panel_entity_id(hass)
    assert state_of(hass, entity_id) == "armed_away"
    states = record_states(hass, entity_id)

    fake_station.send_alarm_frame(
        FrameType.ALARM_DELAY_NOTIFY, _SENSOR_TRIGGERED, 30, channel=_STATION_CHANNEL
    )
    await wait_until(lambda: state_of(hass, entity_id) == "pending")
    attributes = _attributes(hass, entity_id)
    assert attributes[ATTR_DELAY_SECONDS] == 30
    until = dt_util.parse_datetime(str(attributes[ATTR_DELAY_UNTIL]))
    assert until is not None
    assert abs((until - anchor).total_seconds() - 30) < 1

    await advance_to_poll(hass, 31, anchor=anchor)
    assert state_of(hass, entity_id) == "pending"
    await advance_to_poll(hass, 33, anchor=anchor)
    assert state_of(hass, entity_id) == "armed_away"
    assert ATTR_DELAY_UNTIL not in _attributes(hass, entity_id)
    assert ATTR_DELAY_SECONDS not in _attributes(hass, entity_id)
    assert states == ["pending", "armed_away"]
    await _unload(hass, entry)


@pytest.mark.parametrize("start", ["frame", "push"])
async def test_pending_outlasts_the_delay_by_two_seconds(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    start: str,
) -> None:
    """With no alarm, pending ends 2 s after ``seconds``; ``delay_until`` is at + seconds.

    The alarm the delay runs into comes a little after its seconds.
    """
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)
    anchor = dt_util.utcnow()
    states = record_states(hass, entity_id)

    await _handle(hass, entry, _delay(at=anchor) if start == "frame" else _delay_push(anchor))
    assert abs((_until(hass, entity_id) - anchor).total_seconds() - 30) < 1
    await _at(hass, anchor, 31.9)
    assert state_of(hass, entity_id) == "pending"
    await _at(hass, anchor, 32.1)
    assert state_of(hass, entity_id) == "armed_away"
    assert states == ["pending", "armed_away"]
    await _unload(hass, entry)


async def test_the_real_frame_order_runs_the_delay_into_the_alarm_with_no_armed_state(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A HomeBase 3's frames for a door opened in Away, with their timing.

    The parameter list (door open, RSSI moved), the delay on the station's channel
    25 ms later, a poll, the alarm tone on the sensor's channel 30.009 s after the
    delay, the tone's end and the door closing. The tone lands while pending.
    """
    _add_door(fake_station, fake_cloud)
    seed_warm_cache()
    entry = add_entry(hass)
    assert await setup_entry(hass, entry)
    panel = panel_entity_id(hass)
    door = entity_id_for(hass, Platform.BINARY_SENSOR, DOOR_SN, "sensor_door_status")
    coordinator = entry.runtime_data.coordinators[SYNTHETIC.station_sn]
    assert state_of(hass, panel) == "armed_away"
    panel_states = record_states(hass, panel)
    door_states = record_states(hass, door)

    _send_door(fake_station, 1, shape="list")
    await wait_until(lambda: state_of(hass, door) == "on")
    fake_station.send_alarm_frame(
        FrameType.ALARM_DELAY_NOTIFY, _SENSOR_TRIGGERED, 30, channel=_STATION_CHANNEL
    )
    await wait_until(lambda: state_of(hass, panel) == "pending")
    anchor = dt_util.utcnow()
    assert _attributes(hass, panel)[ATTR_DELAY_SECONDS] == 30

    await _at(hass, anchor, 6.4)
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert state_of(hass, panel) == "pending"
    await _at(hass, anchor, 30.009)
    assert state_of(hass, panel) == "pending"
    fake_station.send_alarm_frame(
        FrameType.ALARM_TONE_NOTIFY, _SENSOR_TRIGGERED, 30, channel=_DOOR_CHANNEL
    )
    await wait_until(lambda: state_of(hass, panel) == "triggered")
    await _at(hass, anchor, 60.012)
    fake_station.send_alarm_frame(FrameType.ALARM_TONE_NOTIFY, 0, 0, channel=_DOOR_CHANNEL)
    await wait_until(lambda: state_of(hass, panel) == "armed_away")
    await _at(hass, anchor, 72.125)
    _send_door(fake_station, 0, shape="list")
    await wait_until(lambda: state_of(hass, door) == "off")

    assert panel_states == ["pending", "triggered", "armed_away"]
    assert door_states == ["on", "off"]
    await _unload(hass, entry)


async def test_the_alarm_delay_end_frame_shows_the_armed_mode_again(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """An off type with no time left ends the delay: back to armed at once."""
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)

    fake_station.send_alarm_frame(
        FrameType.ALARM_DELAY_NOTIFY, _SENSOR_TRIGGERED, 30, channel=_STATION_CHANNEL
    )
    await wait_until(lambda: state_of(hass, entity_id) == "pending")
    fake_station.send_alarm_frame(FrameType.ALARM_DELAY_NOTIFY, 0, 0, channel=_STATION_CHANNEL)
    await wait_until(lambda: state_of(hass, entity_id) == "armed_away")
    await _unload(hass, entry)


async def test_the_alarm_ends_the_delay_and_the_expiry_never_lowers_triggered(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Pending escalates to triggered; the delay's own end time then changes nothing."""
    entry = await set_up_warm(hass, seed_warm_cache)
    anchor = dt_util.utcnow()
    entity_id = panel_entity_id(hass)

    await _handle(hass, entry, _delay())
    assert state_of(hass, entity_id) == "pending"
    await _handle(hass, entry, alarm_changed())
    assert state_of(hass, entity_id) == "triggered"
    assert ATTR_DELAY_UNTIL not in _attributes(hass, entity_id)
    assert ATTR_DELAY_SECONDS not in _attributes(hass, entity_id)

    await advance_to_poll(hass, 33, anchor=anchor)
    assert state_of(hass, entity_id) == "triggered"
    await _unload(hass, entry)


@pytest.mark.parametrize("delaying", [True, False], ids=["start", "end"])
async def test_a_delay_frame_while_triggered_changes_nothing(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    delaying: bool,
) -> None:
    """A sounding alarm is never lowered to pending or ended by a delay frame."""
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)
    await _handle(hass, entry, alarm_changed())
    states = record_states(hass, entity_id)

    await _handle(hass, entry, _delay(delaying))
    assert state_of(hass, entity_id) == "triggered"
    assert states == []
    await _unload(hass, entry)


@pytest.mark.parametrize(
    ("seconds", "ends", "writes"),
    [(45, 47, 2), (10, 32, 1)],
    ids=["later-end", "earlier-end"],
)
async def test_a_second_start_keeps_the_later_end(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    seconds: int,
    ends: int,
    writes: int,
) -> None:
    """Another start runs pending to the later of its end and the running one.

    A start that moves nothing shown writes no state.
    """
    entry = await set_up_warm(hass, seed_warm_cache)
    anchor = dt_util.utcnow()
    entity_id = panel_entity_id(hass)
    recorded = _record_writes(hass, entity_id)

    await _handle(hass, entry, _delay(seconds=30, at=anchor))
    await _handle(hass, entry, _delay(seconds=seconds, at=anchor))
    assert _attributes(hass, entity_id)[ATTR_DELAY_SECONDS] == max(30, seconds)
    assert len(recorded) == writes

    await _at(hass, anchor, ends - 0.1)
    assert state_of(hass, entity_id) == "pending"
    await _at(hass, anchor, ends + 0.1)
    assert state_of(hass, entity_id) == "armed_away"
    await _unload(hass, entry)


async def test_a_push_with_no_seconds_keeps_the_running_countdown(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The frame's countdown, then a delay push with no ``alarm_delay``: the countdown
    and its attributes stay, no state is written, and pending ends with the frame's."""
    entry = await set_up_warm(hass, seed_warm_cache)
    anchor = dt_util.utcnow()
    entity_id = panel_entity_id(hass)
    recorded = _record_writes(hass, entity_id)

    await _handle(hass, entry, _delay(at=anchor))
    shown = _attributes(hass, entity_id)
    await _handle(hass, entry, station_event(PushMessageType.ALARM_DELAY, t_ms=now_ms()))
    assert _attributes(hass, entity_id) == shown
    assert shown[ATTR_DELAY_SECONDS] == 30
    assert recorded == [("pending", shown[ATTR_DELAY_UNTIL])]

    await _at(hass, anchor, 31.9)
    assert state_of(hass, entity_id) == "pending"
    await _at(hass, anchor, 32.1)
    assert state_of(hass, entity_id) == "armed_away"
    await _unload(hass, entry)


@pytest.mark.parametrize("skew", [-0.999, -3.0])
async def test_a_push_stamped_before_the_frame_of_one_trigger_moves_nothing(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    skew: float,
) -> None:
    """The frame first, then the same trigger's push stamped by the station's clock.

    The push's earlier end neither shortens pending nor writes state.
    """
    entry = await set_up_warm(hass, seed_warm_cache)
    anchor = dt_util.utcnow()
    entity_id = panel_entity_id(hass)
    recorded = _record_writes(hass, entity_id)

    await _handle(hass, entry, _delay(at=anchor))
    until = _attributes(hass, entity_id)[ATTR_DELAY_UNTIL]
    await _handle(hass, entry, _delay_push(anchor, skew=skew))
    assert recorded == [("pending", until)]

    await _at(hass, anchor, 31.9)
    assert state_of(hass, entity_id) == "pending"
    await _at(hass, anchor, 32.1)
    assert state_of(hass, entity_id) == "armed_away"
    await _unload(hass, entry)


async def test_a_frame_after_the_push_of_one_trigger_moves_the_end_later(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A push stamped 3 s before the frame, then the frame: the frame's end is shown."""
    entry = await set_up_warm(hass, seed_warm_cache)
    anchor = dt_util.utcnow()
    entity_id = panel_entity_id(hass)
    recorded = _record_writes(hass, entity_id)

    await _handle(hass, entry, _delay_push(anchor, skew=-3.0))
    assert abs((_until(hass, entity_id) - anchor).total_seconds() - 27) < 1
    await _handle(hass, entry, _delay(at=anchor))
    assert abs((_until(hass, entity_id) - anchor).total_seconds() - 30) < 1
    assert [state for state, _ in recorded] == ["pending", "pending"]

    await _at(hass, anchor, 31.9)
    assert state_of(hass, entity_id) == "pending"
    await _at(hass, anchor, 32.1)
    assert state_of(hass, entity_id) == "armed_away"
    await _unload(hass, entry)


async def test_the_delay_counts_from_when_the_library_saw_the_frame(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A start handed on 10 s late shows the 20 s left; one already run out shows nothing."""
    entry = await set_up_warm(hass, seed_warm_cache)
    anchor = dt_util.utcnow()
    entity_id = panel_entity_id(hass)
    states = record_states(hass, entity_id)

    await _handle(hass, entry, _delay(seconds=30, ago=40))
    assert states == []
    await _handle(hass, entry, _delay(seconds=30, ago=10))
    assert state_of(hass, entity_id) == "pending"
    await advance_to_poll(hass, 21, anchor=anchor)
    assert state_of(hass, entity_id) == "pending"
    await advance_to_poll(hass, 23, anchor=anchor)
    assert state_of(hass, entity_id) == "armed_away"
    await _unload(hass, entry)


@pytest.mark.parametrize(("seconds", "ago"), [(None, 0.0), (0, -5.0)], ids=["none", "zero"])
async def test_a_start_without_time_left_shows_nothing(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    caplog: pytest.LogCaptureFixture,
    seconds: int | None,
    ago: float,
) -> None:
    """Pending lasts ``seconds``: none or zero is no entry delay, and no error.

    The zero start is stamped ahead of the clock, which ages nothing: exactly 0 s left.
    """
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)
    states = record_states(hass, entity_id)

    await _handle(hass, entry, _delay(seconds=seconds, ago=ago))
    assert states == []
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    await _unload(hass, entry)


async def test_an_end_with_nothing_pending_changes_nothing(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """An end frame on an armed panel writes no state."""
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)
    states = record_states(hass, entity_id)

    await _handle(hass, entry, _delay(False))
    assert states == []
    await _unload(hass, entry)


async def test_a_disarm_ends_the_entry_delay(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Home Assistant's disarm, confirmed by the station, ends pending and its countdown."""
    entry = await set_up_warm(hass, seed_warm_cache)
    anchor = dt_util.utcnow()
    entity_id = panel_entity_id(hass)

    await _handle(hass, entry, _delay())
    assert state_of(hass, entity_id) == "pending"
    await hass.services.async_call(
        ALARM_DOMAIN, SERVICE_ALARM_DISARM, {ATTR_ENTITY_ID: entity_id}, blocking=True
    )
    await hass.async_block_till_done()
    assert state_of(hass, entity_id) == "disarmed"
    assert ATTR_DELAY_UNTIL not in _attributes(hass, entity_id)
    states = record_states(hass, entity_id)
    await advance_to_poll(hass, 33, anchor=anchor)
    assert states == []
    await _unload(hass, entry)


@pytest.mark.parametrize("start", ["frame", "push"])
async def test_a_disarm_the_station_reports_live_ends_the_entry_delay_at_once(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    start: str,
) -> None:
    """A keypad or app disarm seen only as the station's own mode report ends pending."""
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)
    anchor = dt_util.utcnow()
    states = record_states(hass, entity_id)

    await _handle(hass, entry, _delay(at=anchor) if start == "frame" else _delay_push(anchor))
    await _at(hass, anchor, 5)
    fake_station.send_alarm_mode(int(GuardMode.DISARMED))
    await wait_until(lambda: state_of(hass, entity_id) == "disarmed")
    assert ATTR_DELAY_UNTIL not in _attributes(hass, entity_id)
    await _at(hass, anchor, 33)
    assert states == ["pending", "disarmed"]
    await _unload(hass, entry)


@pytest.mark.parametrize(
    ("source", "origin", "mode", "ended"),
    [
        (EventSource.P2P, GuardModeOrigin.REPORT, GuardMode.DISARMED, True),
        (EventSource.P2P, GuardModeOrigin.REPORT, GuardMode.OFF, True),
        (EventSource.P2P, GuardModeOrigin.REPORT, GuardMode.HOME, False),
        (EventSource.P2P, GuardModeOrigin.PUSH, GuardMode.DISARMED, False),
        (EventSource.CLOUD, GuardModeOrigin.REPORT, GuardMode.DISARMED, False),
        (EventSource.CLOUD, GuardModeOrigin.PUSH, GuardMode.DISARMED, False),
    ],
    ids=[
        "live-disarmed",
        "live-off",
        "live-armed",
        "p2p-push-disarmed",
        "cloud-disarmed",
        "cloud-push-disarmed",
    ],
)
async def test_only_a_live_disarmed_mode_report_ends_the_entry_delay(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    source: EventSource,
    origin: GuardModeOrigin,
    mode: GuardMode,
    ended: bool,
) -> None:
    """The station's own report over P2P ends pending; a push's mode (either channel) or a
    cloud report has no order, an armed mode is no disarm."""
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)

    await _handle(hass, entry, _delay())
    await _handle(
        hass,
        entry,
        GuardModeChanged(
            station_sn=SYNTHETIC.station_sn,
            mode=mode,
            active_mode=mode,
            source=source,
            origin=origin,
        ),
    )
    assert (state_of(hass, entity_id) != "pending") is ended
    await _unload(hass, entry)


@pytest.mark.parametrize(("skew", "ended"), [(-5.0, False), (5.0, True)], ids=["older", "newer"])
async def test_a_p2p_disarm_push_ends_the_entry_delay_only_when_ordered_after_it(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    skew: float,
    ended: bool,
) -> None:
    """An authenticated P2P disarm push and its mode, in the library's order: the push's
    event second decides, so one older than the delay leaves pending."""
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)
    anchor = dt_util.utcnow()
    await _handle(hass, entry, _delay_push(anchor))
    assert state_of(hass, entity_id) == "pending"

    t_ms = int((anchor.timestamp() + skew) * 1000)
    await _handle(
        hass,
        entry,
        station_event(PushMessageType.ARMING, t_ms=t_ms, guard_mode=int(GuardMode.DISARMED)),
    )
    await _handle(
        hass,
        entry,
        GuardModeChanged(
            station_sn=SYNTHETIC.station_sn,
            mode=GuardMode.DISARMED,
            active_mode=GuardMode.DISARMED,
            source=EventSource.P2P,
            origin=GuardModeOrigin.PUSH,
        ),
    )
    assert entry.runtime_data.coordinators[SYNTHETIC.station_sn].data.active_mode is (
        GuardMode.DISARMED
    )
    assert state_of(hass, entity_id) == ("disarmed" if ended else "pending")
    await _unload(hass, entry)


async def test_a_repeated_live_disarmed_report_ends_a_pushed_entry_delay(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A cloud disarmed report leaves a pushed delay pending; the station's own disarmed
    report, though it moves no mode, ends it."""
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)
    coordinator = entry.runtime_data.coordinators[SYNTHETIC.station_sn]
    await _handle(hass, entry, _delay_push(dt_util.utcnow()))
    assert state_of(hass, entity_id) == "pending"

    def disarmed(source: EventSource) -> GuardModeChanged:
        return GuardModeChanged(
            station_sn=SYNTHETIC.station_sn,
            mode=GuardMode.DISARMED,
            active_mode=GuardMode.DISARMED,
            source=source,
        )

    await _handle(hass, entry, disarmed(EventSource.CLOUD))
    assert coordinator.data.active_mode is GuardMode.DISARMED
    assert state_of(hass, entity_id) == "pending"

    await _handle(hass, entry, disarmed(EventSource.P2P))
    assert state_of(hass, entity_id) == "disarmed"
    await _unload(hass, entry)


@pytest.mark.parametrize("mode_at_start", [None, 9999], ids=["no mode", "unknown mode"])
@pytest.mark.parametrize(("start", "ended"), [("pending", True), ("triggered", False)])
async def test_a_disarmed_poll_ends_pending_begun_in_an_unknown_mode(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    mode_at_start: int | None,
    start: str,
    ended: bool,
) -> None:
    """Pending never begins in a known disarmed mode, so a disarmed poll ends it.

    Triggered can begin while disarmed: a poll ends it only when it began in a known
    armed mode. The poll's read is the only mode read, with no live report beside it.
    """
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)
    coordinator = entry.runtime_data.coordinators[SYNTHETIC.station_sn]
    coordinator.data = dataclasses.replace(coordinator.data, active_mode=mode_at_start)

    event = (
        station_event(PushMessageType.ALARM_DELAY, t_ms=now_ms())
        if start == "pending"
        else alarm_changed()
    )
    await _handle(hass, entry, event)
    assert state_of(hass, entity_id) == start

    polled = dataclasses.replace(
        coordinator.data, guard_mode=GuardMode.DISARMED, active_mode=GuardMode.DISARMED
    )

    async def _read() -> StationState:
        return polled

    monkeypatch.setattr(coordinator.station, "async_update", _read)
    reads: list[ModeRead] = []
    coordinator.async_add_mode_read_listener(reads.append)
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert reads == [ModeRead.POLL]
    assert state_of(hass, entity_id) == ("disarmed" if ended else start)
    await _unload(hass, entry)


# Each start of an entry delay, built at the time it is handed on.
_DELAY_STARTS: dict[str, Callable[[], Event]] = {
    "frame": _delay,
    "push": lambda: _delay_push(dt_util.utcnow()),
    "push with no seconds": lambda: station_event(PushMessageType.ALARM_DELAY, t_ms=now_ms()),
}


@pytest.mark.parametrize("start", _DELAY_STARTS.values(), ids=_DELAY_STARTS)
@pytest.mark.parametrize(
    ("mode", "shown", "expected"),
    [
        (GuardMode.DISARMED, "disarmed", []),
        (GuardMode.OFF, "disarmed", []),
        # Control: the same start on an armed panel shows pending.
        (GuardMode.HOME, "armed_home", ["pending"]),
    ],
    ids=["disarmed", "off", "armed control"],
)
async def test_a_delay_start_on_a_disarmed_panel_shows_nothing(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    caplog: pytest.LogCaptureFixture,
    mode: GuardMode,
    shown: str,
    expected: list[str],
    start: Callable[[], Event],
) -> None:
    """The station runs an entry delay only when armed: a start while disarmed is ignored."""
    caplog.set_level(logging.DEBUG)
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)
    fake_station.send_alarm_mode(int(mode))
    await wait_until(lambda: state_of(hass, entity_id) == shown)
    states = record_states(hass, entity_id)

    await _handle(hass, entry, start())
    assert states == expected
    assert ("entry delay start ignored" in caplog.text) == (not expected)
    await _unload(hass, entry)


async def test_the_entry_delay_survives_an_entry_reload_with_its_attributes(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A reload during the delay keeps pending and its seconds, ending when it runs out."""
    entry = await set_up_warm(hass, seed_warm_cache)
    anchor = dt_util.utcnow()
    entity_id = panel_entity_id(hass)

    await _handle(hass, entry, _delay())
    until = _attributes(hass, entity_id)[ATTR_DELAY_UNTIL]
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert state_of(hass, entity_id) == "pending"
    attributes = _attributes(hass, entity_id)
    assert attributes[ATTR_DELAY_SECONDS] == 30
    restored = dt_util.parse_datetime(str(attributes[ATTR_DELAY_UNTIL]))
    original = dt_util.parse_datetime(str(until))
    assert restored is not None
    assert original is not None
    assert abs((restored - original).total_seconds()) < 1

    await advance_to_poll(hass, 31, anchor=anchor)
    assert state_of(hass, entity_id) == "pending"
    await advance_to_poll(hass, 33, anchor=anchor)
    await wait_until(lambda: state_of(hass, entity_id) == "armed_away")
    await _unload(hass, entry)


async def test_a_cloud_delay_push_shows_the_same_attributes(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """An authenticated delay push's ``alarm_delay`` is the seconds; until is from its time."""
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)

    await _handle(
        hass,
        entry,
        station_event(PushMessageType.ALARM_DELAY, t_ms=now_ms() - 10_000, alarm_delay=30),
    )
    attributes = _attributes(hass, entity_id)
    assert attributes[ATTR_DELAY_SECONDS] == 30
    until = dt_util.parse_datetime(str(attributes[ATTR_DELAY_UNTIL]))
    assert until is not None
    assert abs((until - dt_util.utcnow()).total_seconds() - 20) < 2
    await _unload(hass, entry)


@pytest.mark.parametrize("case", ["other_station", "not_consuming"])
@pytest.mark.parametrize("kind", ["alarm", "delay"])
async def test_an_alarm_or_delay_reaches_only_its_station_while_the_router_consumes(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    case: str,
    kind: str,
) -> None:
    """One for a station this entry does not serve, or one after unload began, reaches
    no panel."""
    entry = await set_up_warm(hass, seed_warm_cache)
    signal = detections.alarm_signal if kind == "alarm" else detections.alarm_delay_signal
    other_sn = "T8030P0000000999"
    seen: list[AlarmChanged | AlarmDelayChanged] = []
    unsubs = [
        async_dispatcher_connect(hass, signal(entry.entry_id, serial), seen.append)
        for serial in (SYNTHETIC.station_sn, other_sn)
    ]
    router = entry.runtime_data.router
    event: AlarmChanged | AlarmDelayChanged = alarm_changed() if kind == "alarm" else _delay()
    if case == "not_consuming":
        router.async_stop_consuming()
        router.handle(event)
    else:
        router.handle(dataclasses.replace(event, station_sn=other_sn))
    await hass.async_block_till_done()
    assert seen == []

    # The control: the same event for this entry's station while it consumes arrives.
    if case == "not_consuming":
        router.async_start_consuming()
    router.handle(event)
    await hass.async_block_till_done()
    assert seen == [event]
    for unsub in unsubs:
        unsub()
    await _unload(hass, entry)


async def test_a_disarm_push_stamped_before_the_frame_ends_the_entry_delay(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The frame has no time of its own: a station disarm push a little behind it orders after.

    The station clock and the host's differ by seconds, as for ``AlarmChanged``.
    """
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)

    await _handle(hass, entry, _delay())
    await _handle(
        hass,
        entry,
        station_event(PushMessageType.ARMING, t_ms=now_ms() - 10_000, guard_mode=63, arming_user=2),
    )
    assert state_of(hass, entity_id) == "armed_away"
    await _unload(hass, entry)
