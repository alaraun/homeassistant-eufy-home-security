"""An entry sensor's live door state and the alarm panel's entry delay.

The station's parameter notify moves the door entity at once, without a poll; the
station's alarm-delay frame shows the panel pending until the alarm, an end, a disarm
or the delay's own expiry.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Callable, Iterator
from datetime import timedelta
from typing import Final

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
    AlarmDelayChanged,
    EufySecurity,
    Event,
    ParamChanged,
    PushMessageType,
    Station,
    ValueSource,
)
from eufy_home_security.p2p import FrameType
from eufy_home_security.testing import SYNTHETIC, FakeCloud, FakeStation, camera_device
from homeassistant.components.alarm_control_panel import DOMAIN as ALARM_DOMAIN
from homeassistant.const import ATTR_ENTITY_ID, SERVICE_ALARM_DISARM, Platform
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.eufy_home_security.const import (
    ATTR_DELAY_SECONDS,
    ATTR_DELAY_UNTIL,
    ATTR_VALUE_SOURCE,
)

DOOR_SN: Final = "T8900P0000000001"
_DOOR_CHANNEL: Final = 2
_STATION_CHANNEL: Final = 255
_SERIALLESS_CHANNEL: Final = 4
_DOOR_PARAM: Final = 1550
_BATTERY_PARAM: Final = 1101
# The alarm-delay frame's alarm type for a triggered sensor.
_SENSOR_TRIGGERED: Final = 6


def _add_door(station: FakeStation, cloud: FakeCloud) -> None:
    """Pair a closed entry sensor; call before ``seed_warm_cache``."""
    cloud.devices.append(
        camera_device(DOOR_SN, station_sn=station.serial, channel=_DOOR_CHANNEL, name="Door")
    )
    station.params[_DOOR_CHANNEL] = {_BATTERY_PARAM: "90", _DOOR_PARAM: "0"}


def _send_door(station: FakeStation, status: int) -> None:
    """The station's door notify about the entry sensor's channel."""
    station.send_json(
        FrameType.NOTIFY_PAYLOAD,
        {"cmd": _DOOR_PARAM, "payload": {"status": status}},
        device_channel=_DOOR_CHANNEL,
    )


async def _handle(hass: HomeAssistant, entry: MockConfigEntry, event: Event) -> None:
    """Hand ``event`` to the entry's router, as the library would, and let it settle."""
    entry.runtime_data.router.handle(event)
    await hass.async_block_till_done()


async def _unload(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


# ── the door ─────────────────────────────────────────────────────────────────


@pytest.fixture
def quiet_dumps(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[bool]]:
    """Hold back ``StationStateChanged`` while the yielded flag is True.

    Patched before setup: the station binds its dump listener when it starts.
    """
    quiet = [False]
    original = Station._on_dump_complete

    def _on_dump_complete(self: Station) -> None:
        if not quiet[0]:
            original(self)

    monkeypatch.setattr(Station, "_on_dump_complete", _on_dump_complete)
    yield quiet


async def test_a_door_notify_moves_the_door_entity_without_a_poll(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Open, then closed, each from the station's notify alone."""
    _add_door(fake_station, fake_cloud)
    seed_warm_cache()
    entry = add_entry(hass)
    assert await setup_entry(hass, entry)
    entity_id = entity_id_for(hass, Platform.BINARY_SENSOR, DOOR_SN, "sensor_door_status")
    assert state_of(hass, entity_id) == "off"
    states = record_states(hass, entity_id)

    _send_door(fake_station, 1)
    await wait_until(lambda: state_of(hass, entity_id) == "on")
    _send_door(fake_station, 0)
    await wait_until(lambda: state_of(hass, entity_id) == "off")
    assert states == ["on", "off"]
    await _unload(hass, entry)


async def test_the_notify_itself_refreshes_the_door_before_the_station_state(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    quiet_dumps: list[bool],
) -> None:
    """With no ``StationStateChanged`` following, the ``ParamChanged`` moves the door."""
    _add_door(fake_station, fake_cloud)
    seed_warm_cache()
    entry = add_entry(hass)
    assert await setup_entry(hass, entry)
    entity_id = entity_id_for(hass, Platform.BINARY_SENSOR, DOOR_SN, "sensor_door_status")
    assert state_of(hass, entity_id) == "off"

    quiet_dumps[0] = True
    _send_door(fake_station, 1)
    await wait_until(lambda: state_of(hass, entity_id) == "on")
    quiet_dumps[0] = False
    await _unload(hass, entry)


@pytest.mark.parametrize(
    ("source", "channel", "station_sn", "refreshed"),
    [
        (ValueSource.NOTIFY, _DOOR_CHANNEL, SYNTHETIC.station_sn, True),
        (ValueSource.NOTIFY, _STATION_CHANNEL, SYNTHETIC.station_sn, True),
        (ValueSource.P2P, _DOOR_CHANNEL, SYNTHETIC.station_sn, False),
        (ValueSource.CLOUD, _DOOR_CHANNEL, SYNTHETIC.station_sn, False),
        (ValueSource.NOTIFY, 9, SYNTHETIC.station_sn, False),
        (ValueSource.NOTIFY, _SERIALLESS_CHANNEL, SYNTHETIC.station_sn, False),
        (ValueSource.NOTIFY, _DOOR_CHANNEL, "T8030P0000000999", False),
    ],
    ids=[
        "notify-device",
        "notify-station",
        "p2p",
        "cloud",
        "unserved-channel",
        "serialless-slot",
        "other-station",
    ],
)
async def test_only_a_notify_about_a_served_device_refreshes_from_the_station_state(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    source: ValueSource,
    channel: int,
    station_sn: str,
    refreshed: bool,
) -> None:
    """A NOTIFY about the station or a device it serves shows ``station.state``.

    Other sources wait for ``StationStateChanged`` or the poll; a channel no served
    device is on and a station this entry does not serve change nothing.
    """
    _add_door(fake_station, fake_cloud)
    # A slot the station reports that the cloud list does not name: no serial.
    fake_station.params[_SERIALLESS_CHANNEL] = {_BATTERY_PARAM: "80"}
    seed_warm_cache()
    entry = add_entry(hass)
    assert await setup_entry(hass, entry)
    coordinator = entry.runtime_data.coordinators[SYNTHETIC.station_sn]
    entity_id = entity_id_for(hass, Platform.BINARY_SENSOR, DOOR_SN, "sensor_door_status")
    stale = coordinator.data
    _send_door(fake_station, 1)
    await wait_until(lambda: state_of(hass, entity_id) == "on")
    # The coordinator still shows the closed door the station has since replaced.
    coordinator.data = stale
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    assert state_of(hass, entity_id) == "off"

    await _handle(
        hass,
        entry,
        ParamChanged(
            station_sn=station_sn,
            channel=channel,
            param_id=_DOOR_PARAM,
            old="0",
            new="1",
            source=source,
        ),
    )
    assert state_of(hass, entity_id) == ("on" if refreshed else "off")
    await _unload(hass, entry)


async def test_a_notify_before_the_station_has_a_state_changes_nothing(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no merged state to show, the shown one stays."""
    _add_door(fake_station, fake_cloud)
    seed_warm_cache()
    entry = add_entry(hass)
    assert await setup_entry(hass, entry)
    coordinator = entry.runtime_data.coordinators[SYNTHETIC.station_sn]
    shown = coordinator.data
    monkeypatch.setattr(Station, "state", property(lambda _self: None))

    await _handle(
        hass,
        entry,
        ParamChanged(
            station_sn=SYNTHETIC.station_sn,
            channel=_DOOR_CHANNEL,
            param_id=_DOOR_PARAM,
            old="0",
            new="1",
            source=ValueSource.NOTIFY,
        ),
    )
    assert coordinator.data is shown
    monkeypatch.undo()
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
    delaying: bool = True, seconds: int | None = 30, *, ago: float = 0.0
) -> AlarmDelayChanged:
    """The library's ``AlarmDelayChanged`` of a HomeBase 3: the station's channel."""
    return AlarmDelayChanged(
        station_sn=SYNTHETIC.station_sn,
        delaying=delaying,
        channel=_STATION_CHANNEL,
        alarm_type=_SENSOR_TRIGGERED if delaying else None,
        seconds=seconds if delaying else None,
        at=dt_util.utcnow() - timedelta(seconds=ago),
    )


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

    The countdown ends on its own after ``seconds``: back to the mode in force.
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
    assert abs((until - anchor).total_seconds() - 30) < 2

    await advance_to_poll(hass, 29, anchor=anchor)
    assert state_of(hass, entity_id) == "pending"
    await advance_to_poll(hass, 31, anchor=anchor)
    assert state_of(hass, entity_id) == "armed_away"
    assert ATTR_DELAY_UNTIL not in _attributes(hass, entity_id)
    assert ATTR_DELAY_SECONDS not in _attributes(hass, entity_id)
    assert states == ["pending", "armed_away"]
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

    await advance_to_poll(hass, 31, anchor=anchor)
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


async def test_a_second_start_refreshes_the_entry_delay(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Another trigger's start restarts the countdown from its own seconds."""
    entry = await set_up_warm(hass, seed_warm_cache)
    anchor = dt_util.utcnow()
    entity_id = panel_entity_id(hass)

    await _handle(hass, entry, _delay(seconds=30))
    await advance_to_poll(hass, 20, anchor=anchor)
    assert state_of(hass, entity_id) == "pending"
    await _handle(hass, entry, _delay(seconds=45))
    assert _attributes(hass, entity_id)[ATTR_DELAY_SECONDS] == 45

    await advance_to_poll(hass, 31, anchor=anchor)
    assert state_of(hass, entity_id) == "pending"
    await advance_to_poll(hass, 44, anchor=anchor)
    assert state_of(hass, entity_id) == "pending"
    await advance_to_poll(hass, 47, anchor=anchor)
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
    await advance_to_poll(hass, 19, anchor=anchor)
    assert state_of(hass, entity_id) == "pending"
    await advance_to_poll(hass, 21, anchor=anchor)
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
    await advance_to_poll(hass, 31, anchor=anchor)
    assert states == []
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
    assert abs((restored - original).total_seconds()) < 2

    await advance_to_poll(hass, 31, anchor=anchor)
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


async def test_a_delay_for_another_station_reaches_no_panel(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The router hands a delay only to the station it names."""
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)
    states = record_states(hass, entity_id)

    await _handle(hass, entry, dataclasses.replace(_delay(), station_sn="T8030P0000000999"))
    assert states == []
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
