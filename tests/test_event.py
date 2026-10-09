"""Detection events: what a camera detected, fired once per occurrence.

Events are tested end to end through the library's ``FakeStation``: a push
travels over loopback UDP through the real library, its de-duplicator and the
router, then the dispatcher, to the entity. Events the fake cannot push (a current
trigger time, a station message) are real library ``SecurityEvent`` instances handed
to the router, never a hand-written fake.

No test here uses ``freezer``: it freezes the monotonic clock the loopback sessions
depend on, and a FakeStation-backed entry hangs under it (measured).
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest
from conftest import (
    PUSHED_THUMB_PATH,
    SENSOR_SN,
    add_motion_sensor,
    alarm_changed,
    detection_event,
    entity_id_for,
    now_ms,
    record_states,
    set_up_warm,
    station_event,
    wait_until,
)
from eufy_home_security import (
    AlarmChanged,
    AlarmStopSource,
    ArmingSource,
    DetectionType,
    EufySecurity,
    FrameCipher,
    PushMessageType,
    SecurityEvent,
    entity_unique_id,
)
from eufy_home_security.devices import DeviceKind
from eufy_home_security.p2p import FrameType
from eufy_home_security.push.decode import decode_push
from eufy_home_security.testing import SYNTHETIC, FakeCloud, FakeStation
from homeassistant.components.binary_sensor import DOMAIN as BINARY_SENSOR_DOMAIN
from homeassistant.components.event import DOMAIN as EVENT_DOMAIN
from homeassistant.const import STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events

from custom_components.eufy_home_security import detections
from custom_components.eufy_home_security.const import (
    ALARM_EVENT_KEY,
    ARMING_EVENT_KEY,
    DETECTION_EVENT_KEY,
    DOMAIN,
    DOORBELL_EVENT_KEY,
    EVENT_EUFY_HOME_SECURITY,
    PERSON_DETECTED_KEY,
)

# The fake's trigger_time, 1_700_000_000_000 ms, as the detection's own time.
_FAKE_TRIGGERED_AT: Final = "2023-11-14T22:13:20.000+00:00"
_EVENT_TYPES: Final = ["motion", "person", "identified_person", "stranger", "pet", "vehicle"]
# Every attribute a detection event state may carry.
_ALLOWED_ATTRIBUTES: Final = frozenset(
    {"event_type", "event_types", "friendly_name", "triggered_at", "person_name"}
)
# The exact keys of the fallback bus event.
_FALLBACK_KEYS: Final = frozenset(
    {"device_id", "msg_type", "event_type", "triggered_at", "authenticated", "source"}
)
# The fake's trigger_time in epoch ms, for a router-fed copy of its push.
_FAKE_TRIGGER_MS: Final = 1_700_000_000_000
# A station serial with no coordinator in the entry.
_UNKNOWN_STATION_SN: Final = "T8030P0000000002"


def _detection_entity_id(hass: HomeAssistant) -> str:
    """The synthetic camera's detection event entity."""
    return entity_id_for(hass, EVENT_DOMAIN, SYNTHETIC.camera_sn, DETECTION_EVENT_KEY)


def _person_sensor_id(hass: HomeAssistant) -> str:
    """The synthetic camera's person sensor."""
    return entity_id_for(hass, BINARY_SENSOR_DOMAIN, SYNTHETIC.camera_sn, PERSON_DETECTED_KEY)


def _registry_id(hass: HomeAssistant, entry: MockConfigEntry, serial: str) -> str:
    """The device registry id of ``serial`` within ``entry``."""
    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, serial), entry.entry_id)
    assert device is not None
    return device.id


async def _handle(
    hass: HomeAssistant, entry: MockConfigEntry, event: SecurityEvent | AlarmChanged
) -> None:
    """Hand ``event`` to the entry's router, as the library would, and let it settle."""
    entry.runtime_data.router.handle(event)
    await hass.async_block_till_done()


async def _unload(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


def _fired(states: list[str]) -> list[str]:
    """The distinct detection events among ``states``, in order.

    Each fired event is a new timestamp state. The entity also goes unavailable and
    comes back with the same timestamp across a reconnect, which is not a new event.
    """
    fired: list[str] = []
    for state in states:
        if state not in (STATE_UNAVAILABLE, STATE_UNKNOWN) and state not in fired:
            fired.append(state)
    return fired


def test_every_event_type_a_mapping_fires_is_declared() -> None:
    """``_trigger_event`` raises on an undeclared type, so each tuple covers its mapping."""
    for names, mapping in (
        (detections.DETECTION_EVENT_TYPE_NAMES, detections.DETECTION_EVENT_TYPES),
        (detections.ALARM_EVENT_TYPE_NAMES, detections.ALARM_EVENT_TYPES),
        (detections.ARMING_EVENT_TYPE_NAMES, detections.ARMING_EVENT_TYPES),
    ):
        assert set(mapping.values()) == set(names)
        assert len(names) == len(set(names))
    assert list(detections.DETECTION_EVENT_TYPE_NAMES) == _EVENT_TYPES
    assert detections.ARMING_EVENT_TYPE_NAMES == (
        "armed_away",
        "armed_home",
        "disarmed",
        "armed_custom",
    )


def test_every_arming_source_has_a_label() -> None:
    """``changed_by`` indexes the labels by the library's source, so each must have one."""
    assert set(detections.ARMING_SOURCE_LABELS) == set(ArmingSource)


async def test_a_detection_push_fires_the_camera_detection_event(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A person the camera saw fires its detection event, at its own time."""
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = _detection_entity_id(hass)

    fake_station.push_camera_event(DetectionType.PERSON)
    await wait_until(
        lambda: (state := hass.states.get(entity_id)) is not None and state.state != STATE_UNKNOWN
    )

    state = hass.states.get(entity_id)
    assert state is not None
    assert state.attributes["event_type"] == "person"
    assert state.attributes["triggered_at"] == _FAKE_TRIGGERED_AT
    assert state.attributes["event_types"] == _EVENT_TYPES

    await _unload(hass, entry)


async def test_detection_event_attributes_are_allow_listed(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Media paths, serials, names and user names never reach the attributes."""
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = _detection_entity_id(hass)

    await _handle(
        hass,
        entry,
        detection_event(
            DetectionType.PERSON,
            t_ms=now_ms(),
            device_name="Front",
            person_name="Alice",
            thumb_path="/zx/thumb.jpg",
            video_path="/zx/clip.zxvideo",
            crop_path="/zx/crop.jpg",
            pic_url="https://example.com/pic.jpg",
            user_name="Bob",
        ),
    )

    state = hass.states.get(entity_id)
    assert state is not None
    assert state.attributes["event_type"] == "person"
    assert set(state.attributes) <= _ALLOWED_ATTRIBUTES
    # A person that is not an identified person carries no name.
    assert "person_name" not in state.attributes
    dumped = json.dumps(dict(state.attributes))
    for leak in ("/zx/", SYNTHETIC.camera_sn, SYNTHETIC.station_sn, "example.com", "Bob"):
        assert leak not in dumped

    await _unload(hass, entry)


async def test_an_identified_person_carries_only_its_name(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """An identified person's name is the one attribute beyond its time."""
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = _detection_entity_id(hass)
    t_ms = now_ms()

    await _handle(
        hass,
        entry,
        detection_event(DetectionType.IDENTITY_PERSON, t_ms=t_ms, person_name="Alice"),
    )

    state = hass.states.get(entity_id)
    assert state is not None
    assert {
        key: value
        for key, value in state.attributes.items()
        if key not in {"event_types", "friendly_name"}
    } == {
        "event_type": "identified_person",
        "triggered_at": detections.triggered_at(
            detection_event(DetectionType.IDENTITY_PERSON, t_ms=t_ms)
        ),
        "person_name": "Alice",
    }

    await _unload(hass, entry)


@pytest.mark.parametrize(
    "detection", [DetectionType.DOG, DetectionType.DOG_LICK, DetectionType.DOG_POOP]
)
async def test_dog_detections_fire_the_pet_event_type(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    detection: DetectionType,
) -> None:
    """Every dog detection fires ``pet``, so none falls to the fallback."""
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = _detection_entity_id(hass)
    events = async_capture_events(hass, EVENT_EUFY_HOME_SECURITY)

    await _handle(hass, entry, detection_event(detection, t_ms=now_ms()))

    state = hass.states.get(entity_id)
    assert state is not None
    assert state.attributes["event_type"] == "pet"
    assert events == []

    await _unload(hass, entry)


@dataclass(frozen=True, kw_only=True)
class _FallbackCase:
    """One push no entity consumes, and the device the fallback must name."""

    event: Callable[[], SecurityEvent]
    pair_sensor: bool = False
    # The serial whose registry id is expected, or None for no device at all.
    device_serial: str | None


_FALLBACK_CASES: Final = {
    # A T8910 has no detection entities: its motion push goes to the bus.
    "motion_sensor": _FallbackCase(
        event=lambda: detection_event(
            DetectionType.MOTION,
            t_ms=now_ms(),
            device_sn=SENSOR_SN,
            msg_type=PushMessageType.MOTION_SENSOR,
        ),
        pair_sensor=True,
        device_serial=SENSOR_SN,
    ),
    # A detection code the event entity does not declare.
    "uncatalogued_code": _FallbackCase(
        event=lambda: detection_event(DetectionType.CRYING, t_ms=now_ms()),
        device_serial=SYNTHETIC.camera_sn,
    ),
    # A serial the station does not pair names the station, never a device.
    "unpaired_device": _FallbackCase(
        event=lambda: detection_event(DetectionType.PERSON, t_ms=now_ms(), device_sn=SENSOR_SN),
        device_serial=SYNTHETIC.station_sn,
    ),
    # A station this entry does not hold: no device id at all.
    "unknown_station": _FallbackCase(
        event=lambda: detection_event(
            DetectionType.PERSON, t_ms=now_ms(), station_sn=_UNKNOWN_STATION_SN
        ),
        device_serial=None,
    ),
}


@pytest.mark.parametrize("case", list(_FALLBACK_CASES.values()), ids=list(_FALLBACK_CASES))
async def test_a_push_no_entity_consumes_fires_the_fallback_bus_event(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    case: _FallbackCase,
) -> None:
    """One bus event with the six keys and a registry id, and no entity event."""
    if case.pair_sensor:
        # Before setup: the warm cache copies the device list when it is seeded.
        add_motion_sensor(fake_station, fake_cloud)
    entry = await set_up_warm(hass, seed_warm_cache)
    detection_states = record_states(hass, _detection_entity_id(hass))
    events = async_capture_events(hass, EVENT_EUFY_HOME_SECURITY)
    event = case.event()

    await _handle(hass, entry, event)

    assert len(events) == 1
    data = events[0].data
    assert set(data) == _FALLBACK_KEYS
    expected_device_id = (
        None if case.device_serial is None else _registry_id(hass, entry, case.device_serial)
    )
    assert data["device_id"] == expected_device_id
    assert data["msg_type"] == event.msg_type
    assert data["event_type"] == event.event_type
    assert data["triggered_at"] == detections.triggered_at(event)
    assert data["authenticated"] is True
    assert data["source"] == "p2p"
    assert detection_states == []

    await _unload(hass, entry)


async def test_a_camera_doorbell_press_fires_the_fallback_end_to_end(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A doorbell press from a plain camera reaches the bus, not an entity."""
    entry = await set_up_warm(hass, seed_warm_cache)
    detection_states = record_states(hass, _detection_entity_id(hass))
    events = async_capture_events(hass, EVENT_EUFY_HOME_SECURITY)

    fake_station.push_camera_event(DetectionType.DOORBELL_PRESS)
    await wait_until(lambda: len(events) == 1)

    data = events[0].data
    assert set(data) == _FALLBACK_KEYS
    assert data["device_id"] == _registry_id(hass, entry, SYNTHETIC.camera_sn)
    assert data["msg_type"] == 18
    assert data["event_type"] == 3103
    assert data["triggered_at"] == _FAKE_TRIGGERED_AT
    assert data["authenticated"] is True
    assert data["source"] == "p2p"
    assert detection_states == []

    await _unload(hass, entry)


async def test_an_unauthenticated_alarm_stop_and_an_enriching_copy_fire_nothing(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A forgeable stop and a re-delivered occurrence leave no trace."""
    entry = await set_up_warm(hass, seed_warm_cache)
    detection_states = record_states(hass, _detection_entity_id(hass))
    events = async_capture_events(hass, EVENT_EUFY_HOME_SECURITY)

    stop = station_event(
        PushMessageType.ALARM, t_ms=now_ms(), cipher=FrameCipher.ECB, alarm_type=15
    )
    assert stop.alarm_phase is None  # the library withholds an unauthenticated stop
    await _handle(hass, entry, stop)
    await _handle(hass, entry, detection_event(DetectionType.PERSON, t_ms=now_ms(), enriches=True))

    assert events == []
    assert detection_states == []

    await _unload(hass, entry)


async def test_events_before_entities_listen_go_to_the_fallback(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Outside the consuming window a detection fires the fallback, not its entity."""
    entry = await set_up_warm(hass, seed_warm_cache)
    detection_states = record_states(hass, _detection_entity_id(hass))
    events = async_capture_events(hass, EVENT_EUFY_HOME_SECURITY)

    entry.runtime_data.router.async_stop_consuming()
    await _handle(hass, entry, detection_event(DetectionType.PERSON, t_ms=now_ms()))

    assert len(events) == 1
    assert events[0].data["device_id"] == _registry_id(hass, entry, SYNTHETIC.camera_sn)
    assert detection_states == []

    await _unload(hass, entry)


async def test_routing_events_logs_no_serial(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Routing a delivered push and a fallback writes no serial to the integration's logs."""
    caplog.set_level(logging.DEBUG)
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = _detection_entity_id(hass)
    events: list[Event] = async_capture_events(hass, EVENT_EUFY_HOME_SECURITY)

    fake_station.push_camera_event(DetectionType.PERSON)
    await wait_until(
        lambda: (state := hass.states.get(entity_id)) is not None and state.state != STATE_UNKNOWN
    )
    await _handle(hass, entry, detection_event(DetectionType.CRYING, t_ms=now_ms()))
    assert len(events) == 1

    await _unload(hass, entry)

    ours = [
        record
        for record in caplog.records
        if record.name.startswith("custom_components.eufy_home_security")
    ]
    for record in ours:
        message = record.getMessage()
        assert SYNTHETIC.station_sn not in message
        assert SYNTHETIC.camera_sn not in message
    # Non-vacuity: debug logging was really captured while the push travelled.
    assert any(record.name.startswith("eufy_home_security") for record in caplog.records)


def _alarm_entity_id(hass: HomeAssistant) -> str:
    """The synthetic station's alarm event entity."""
    return entity_id_for(hass, EVENT_DOMAIN, SYNTHETIC.station_sn, ALARM_EVENT_KEY)


async def test_the_p2p_alarm_frames_fire_the_station_alarm_event_once_per_transition(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The tone frames fire triggered and stopped through AlarmChanged, push off.

    A repeated start frame is no second transition; the app's stop on the station's
    channel names its source. No fallback bus event: the frames are not pushes.
    """
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = _alarm_entity_id(hass)
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == STATE_UNKNOWN
    states = record_states(hass, entity_id)
    events = async_capture_events(hass, EVENT_EUFY_HOME_SECURITY)

    fake_station.send_alarm_frame(FrameType.ALARM_TONE_NOTIFY, 3, 30, channel=1)
    await wait_until(lambda: len(states) == 1)
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.attributes["event_type"] == "alarm_triggered"
    assert state.attributes["authenticated"] is True
    assert "stop_source" not in state.attributes

    fake_station.send_alarm_frame(FrameType.ALARM_TONE_NOTIFY, 3, 30, channel=1)
    fake_station.send_alarm_frame(FrameType.ALARM_TONE_NOTIFY, 16, 0, channel=255)
    await wait_until(lambda: len(states) == 2)
    await hass.async_block_till_done()
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.attributes["event_type"] == "alarm_stopped"
    assert state.attributes["stop_source"] == "app"
    assert set(state.attributes) <= _ALARM_ATTRIBUTES
    assert len(states) == 2
    assert events == []

    await _unload(hass, entry)


@pytest.mark.parametrize("cipher", [FrameCipher.GCM, None], ids=["gcm", "cloud"])
async def test_an_alarm_push_and_its_alarm_changed_fire_the_alarm_event_once(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    cipher: FrameCipher | None,
) -> None:
    """An authenticated alarm push fires nothing itself; its AlarmChanged fires once.

    The library delivers the push and then the transition it makes of it, so with push
    on an alarm seen on both channels is still one triggered and one stopped.
    """
    entry = await set_up_warm(hass, seed_warm_cache)
    states = record_states(hass, _alarm_entity_id(hass))
    events = async_capture_events(hass, EVENT_EUFY_HOME_SECURITY)

    await _handle(hass, entry, station_event(PushMessageType.ALARM, t_ms=now_ms(), cipher=cipher))
    assert states == []
    await _handle(hass, entry, alarm_changed())
    await _handle(
        hass,
        entry,
        station_event(PushMessageType.ALARM, t_ms=now_ms(), cipher=cipher, alarm_type=16),
    )
    await _handle(hass, entry, alarm_changed(False, AlarmStopSource.APP))

    assert len(states) == 2
    state = hass.states.get(_alarm_entity_id(hass))
    assert state is not None
    assert state.attributes["event_type"] == "alarm_stopped"
    assert events == []

    await _unload(hass, entry)


async def test_events_during_a_failed_poll_still_fire_while_the_session_is_up(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Event entities follow the push session, not the poll's last result."""
    entry = await set_up_warm(hass, seed_warm_cache)
    coordinator = entry.runtime_data.coordinators[SYNTHETIC.station_sn]
    assert coordinator.station.connected
    coordinator.async_set_update_error(UpdateFailed("a poll timed out"))
    await hass.async_block_till_done()

    await _handle(hass, entry, detection_event(DetectionType.PERSON, t_ms=now_ms()))
    detection = hass.states.get(_detection_entity_id(hass))
    assert detection is not None
    assert detection.state != STATE_UNAVAILABLE
    assert detection.attributes["event_type"] == "person"

    await _handle(hass, entry, alarm_changed())
    alarm = hass.states.get(_alarm_entity_id(hass))
    assert alarm is not None
    assert alarm.state != STATE_UNAVAILABLE
    assert alarm.attributes["event_type"] == "alarm_triggered"

    await _unload(hass, entry)


# Every attribute a station alarm event state may carry.
_ALARM_ATTRIBUTES: Final = frozenset(
    {"event_type", "event_types", "friendly_name", "triggered_at", "authenticated", "stop_source"}
)
# Every attribute a station arming event state may carry.
_ARMING_ATTRIBUTES: Final = frozenset(
    {"event_type", "event_types", "friendly_name", "triggered_at", "changed_by"}
)


def _arming_entity_id(hass: HomeAssistant) -> str:
    """The synthetic station's arming event entity."""
    return entity_id_for(hass, EVENT_DOMAIN, SYNTHETIC.station_sn, ARMING_EVENT_KEY)


@pytest.mark.parametrize(
    ("event", "event_type"),
    [
        (alarm_changed, "alarm_triggered"),
        (lambda: alarm_changed(False, AlarmStopSource.APP), "alarm_stopped"),
        (lambda: alarm_changed(False), "alarm_stopped"),
        (
            lambda: station_event(PushMessageType.ALARM_DELAY, t_ms=now_ms(), alarm_delay=30),
            "alarm_delay",
        ),
        (
            lambda: station_event(
                PushMessageType.ALARM_DELAY, t_ms=now_ms(), cipher=None, alarm_delay=30
            ),
            "alarm_delay",
        ),
    ],
    ids=["alarm_start", "app_stop", "tone_end", "gcm_delay", "cloud_delay"],
)
async def test_alarm_phases_map_to_alarm_event_types(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    event: Callable[[], SecurityEvent | AlarmChanged],
    event_type: str,
) -> None:
    """Start, end and delay each fire their alarm event type, authenticated."""
    entry = await set_up_warm(hass, seed_warm_cache)
    events = async_capture_events(hass, EVENT_EUFY_HOME_SECURITY)

    await _handle(hass, entry, event())

    state = hass.states.get(_alarm_entity_id(hass))
    assert state is not None
    assert state.attributes["event_type"] == event_type
    assert state.attributes["authenticated"] is True
    assert events == []

    await _unload(hass, entry)


@pytest.mark.parametrize(
    ("event", "event_type"),
    [
        (
            lambda: station_event(PushMessageType.ALARM, t_ms=now_ms(), cipher=FrameCipher.ECB),
            "alarm_triggered",
        ),
        (
            lambda: station_event(
                PushMessageType.ALARM_DELAY, t_ms=now_ms(), cipher=FrameCipher.ECB, alarm_delay=30
            ),
            "alarm_delay",
        ),
    ],
    ids=["ecb_trigger", "ecb_delay"],
)
async def test_an_unauthenticated_alarm_trigger_or_delay_fires_the_alarm_event_marked_unauthenticated(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    event: Callable[[], SecurityEvent],
    event_type: str,
) -> None:
    """An ECB trigger or delay fires the alarm event, marked so automations can refuse it."""
    entry = await set_up_warm(hass, seed_warm_cache)
    events = async_capture_events(hass, EVENT_EUFY_HOME_SECURITY)

    await _handle(hass, entry, event())

    state = hass.states.get(_alarm_entity_id(hass))
    assert state is not None
    assert state.attributes["event_type"] == event_type
    assert state.attributes["authenticated"] is False
    assert events == []

    await _unload(hass, entry)


@pytest.mark.parametrize(
    ("guard_mode", "event_type"),
    [
        (0, "armed_away"),
        (1, "armed_home"),
        (63, "disarmed"),
        (6, "disarmed"),
        (2, "armed_custom"),
        (3, "armed_custom"),
        (47, "armed_custom"),
    ],
    ids=["away", "home", "disarmed", "off", "schedule", "custom_1", "geofence"],
)
async def test_arming_pushes_map_to_arming_event_types(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    guard_mode: int,
    event_type: str,
) -> None:
    """The arming event follows the panel's own guard-mode mapping."""
    entry = await set_up_warm(hass, seed_warm_cache)
    events = async_capture_events(hass, EVENT_EUFY_HOME_SECURITY)

    event = station_event(PushMessageType.ARMING, t_ms=now_ms(), guard_mode=guard_mode)
    await _handle(hass, entry, event)

    state = hass.states.get(_arming_entity_id(hass))
    assert state is not None
    assert state.attributes["event_type"] == event_type
    assert state.attributes["triggered_at"] == detections.triggered_at(event)
    assert events == []

    await _unload(hass, entry)


async def test_under_schedule_the_arming_event_names_the_slot_mode_in_force(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A slot's push (``arming`` 2, ``mode`` 1) fires what the panel shows."""
    entry = await set_up_warm(hass, seed_warm_cache)

    await _handle(
        hass,
        entry,
        station_event(PushMessageType.ARMING, t_ms=now_ms(), cipher=None, guard_mode=2, mode=1),
    )

    state = hass.states.get(_arming_entity_id(hass))
    assert state is not None
    assert state.attributes["event_type"] == "armed_home"

    await _unload(hass, entry)


@pytest.mark.parametrize(
    ("arming_user", "changed_by"),
    [(1, "Keypad"), (5, "Key fob"), (2, "App"), (9, "App"), (None, None)],
    ids=["keypad", "key_fob", "app", "other_code", "no_user"],
)
async def test_the_arming_event_names_who_armed_by_source_label_never_user_name(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    arming_user: int | None,
    changed_by: str | None,
) -> None:
    """``changed_by`` is the source label; a sender-supplied name never appears."""
    entry = await set_up_warm(hass, seed_warm_cache)

    await _handle(
        hass,
        entry,
        station_event(
            PushMessageType.ARMING,
            t_ms=now_ms(),
            guard_mode=0,
            arming_user=arming_user,
            user_name="Mallory",
        ),
    )

    state = hass.states.get(_arming_entity_id(hass))
    assert state is not None
    assert state.attributes["event_type"] == "armed_away"
    if changed_by is None:
        assert "changed_by" not in state.attributes
    else:
        assert state.attributes["changed_by"] == changed_by
    assert "Mallory" not in json.dumps(dict(state.attributes))

    await _unload(hass, entry)


async def test_station_event_attributes_are_allow_listed(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Station events carry only their allow-listed attributes, never a serial."""
    entry = await set_up_warm(hass, seed_warm_cache)
    noise = {
        "device_name": "Cellar Hub",
        "user_name": "Mallory",
        "thumb_path": "/zx/thumb.jpg",
        "pic_url": "https://example.com/pic.jpg",
    }

    await _handle(
        hass,
        entry,
        station_event(PushMessageType.ALARM_DELAY, t_ms=now_ms(), alarm_delay=30, **noise),
    )
    await _handle(
        hass,
        entry,
        station_event(PushMessageType.ARMING, t_ms=now_ms(), guard_mode=1, arming_user=2, **noise),
    )

    alarm = hass.states.get(_alarm_entity_id(hass))
    arming = hass.states.get(_arming_entity_id(hass))
    assert alarm is not None
    assert arming is not None
    assert alarm.attributes["event_type"] == "alarm_delay"
    assert arming.attributes["event_type"] == "armed_home"
    assert set(alarm.attributes) <= _ALARM_ATTRIBUTES
    assert "authenticated" in alarm.attributes
    assert set(arming.attributes) <= _ARMING_ATTRIBUTES
    for state in (alarm, arming):
        dumped = json.dumps(dict(state.attributes))
        for leak in (SYNTHETIC.station_sn, "Mallory", "Cellar Hub", "/zx/", "example.com"):
            assert leak not in dumped

    await _unload(hass, entry)


async def test_an_unauthenticated_arming_push_goes_to_the_fallback(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A forgeable ECB arming push never fires the arming event."""
    entry = await set_up_warm(hass, seed_warm_cache)
    arming_states = record_states(hass, _arming_entity_id(hass))
    events = async_capture_events(hass, EVENT_EUFY_HOME_SECURITY)

    await _handle(
        hass,
        entry,
        station_event(PushMessageType.ARMING, t_ms=now_ms(), cipher=FrameCipher.ECB, guard_mode=0),
    )

    assert arming_states == []
    assert len(events) == 1
    assert set(events[0].data) == _FALLBACK_KEYS
    assert events[0].data["authenticated"] is False
    assert events[0].data["device_id"] == _registry_id(hass, entry, SYNTHETIC.station_sn)

    await _unload(hass, entry)


@pytest.mark.parametrize("guard_mode", [None, 99], ids=["no_mode", "unknown_mode"])
async def test_an_arming_push_without_a_known_mode_goes_to_the_fallback(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    guard_mode: int | None,
) -> None:
    """An arming push naming no mode, or one eufy never defined, fires the fallback."""
    entry = await set_up_warm(hass, seed_warm_cache)
    arming_states = record_states(hass, _arming_entity_id(hass))
    events = async_capture_events(hass, EVENT_EUFY_HOME_SECURITY)

    await _handle(
        hass,
        entry,
        station_event(PushMessageType.ARMING, t_ms=now_ms(), guard_mode=guard_mode),
    )

    assert arming_states == []
    assert len(events) == 1
    assert events[0].data["authenticated"] is True

    await _unload(hass, entry)


async def test_a_cloud_arming_push_fires_the_arming_event(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A cloud arming push is authenticated and fires the arming event."""
    entry = await set_up_warm(hass, seed_warm_cache)
    events = async_capture_events(hass, EVENT_EUFY_HOME_SECURITY)

    await _handle(
        hass,
        entry,
        station_event(
            PushMessageType.ARMING, t_ms=now_ms(), cipher=None, guard_mode=1, arming_user=2
        ),
    )

    state = hass.states.get(_arming_entity_id(hass))
    assert state is not None
    assert state.attributes["event_type"] == "armed_home"
    assert state.attributes["changed_by"] == "App"
    assert events == []

    await _unload(hass, entry)


_EN_JSON: Final = (
    Path(__file__).parent.parent
    / "custom_components"
    / "eufy_home_security"
    / "translations"
    / "en.json"
)


def test_doorbell_selection_follows_the_library_device_kind() -> None:
    """Only a DOORBELL gets the ring, and it keeps its detections."""
    for kind in [*DeviceKind, None]:
        assert detections.has_doorbell_entity(kind) is (kind is DeviceKind.DOORBELL)
    press = detection_event(DetectionType.DOORBELL_PRESS, t_ms=now_ms())
    person = detection_event(DetectionType.PERSON, t_ms=now_ms())
    assert detections.device_event_consumed(DeviceKind.DOORBELL, press) is True
    assert detections.device_event_consumed(DeviceKind.CAMERA, press) is False
    assert detections.device_event_consumed(DeviceKind.DOORBELL, person) is True


async def test_a_doorbell_press_on_a_doorbell_fires_ring(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A catalogued doorbell's press fires its ring event end to end.

    ``SYNTHETIC`` has no doorbell serial and the fake station pushes for its synthetic
    camera, so the integration's own ``detections.device_kind`` lookup is replaced by
    one that calls that camera a doorbell. The library itself is never replaced.
    """
    original = detections.device_kind

    def doorbell_kind(serial: str) -> DeviceKind | None:
        if serial == SYNTHETIC.camera_sn:
            return DeviceKind.DOORBELL
        return original(serial)

    monkeypatch.setattr(detections, "device_kind", doorbell_kind)
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = entity_id_for(hass, EVENT_DOMAIN, SYNTHETIC.camera_sn, DOORBELL_EVENT_KEY)
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.attributes["device_class"] == "doorbell"
    assert state.attributes["event_types"] == ["ring"]
    detection_states = record_states(hass, _detection_entity_id(hass))
    events = async_capture_events(hass, EVENT_EUFY_HOME_SECURITY)

    fake_station.push_camera_event(DetectionType.DOORBELL_PRESS)
    await wait_until(
        lambda: (state := hass.states.get(entity_id)) is not None and state.state != STATE_UNKNOWN
    )
    await hass.async_block_till_done()

    state = hass.states.get(entity_id)
    assert state is not None
    assert state.attributes["event_type"] == "ring"
    assert state.attributes["triggered_at"] == _FAKE_TRIGGERED_AT
    assert detection_states == []
    assert events == []

    await _unload(hass, entry)


async def test_every_station_has_one_alarm_and_one_arming_event_entity(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """One alarm and one arming event per station, no doorbell unpatched."""
    entry = await set_up_warm(hass, seed_warm_cache)
    registry = er.async_get(hass)

    for key in (ALARM_EVENT_KEY, ARMING_EVENT_KEY):
        assert registry.async_get_entity_id(
            EVENT_DOMAIN, DOMAIN, entity_unique_id(SYNTHETIC.station_sn, key)
        )
    assert (
        registry.async_get_entity_id(
            EVENT_DOMAIN, DOMAIN, entity_unique_id(SYNTHETIC.camera_sn, DOORBELL_EVENT_KEY)
        )
        is None
    )

    translations = json.loads(_EN_JSON.read_text(encoding="utf-8"))["entity"]["event"]
    event_entries = [
        reg
        for reg in er.async_entries_for_config_entry(registry, entry.entry_id)
        if reg.domain == EVENT_DOMAIN
    ]
    assert {reg.translation_key for reg in event_entries} == {
        ALARM_EVENT_KEY,
        ARMING_EVENT_KEY,
        DETECTION_EVENT_KEY,
    }
    for reg in event_entries:
        assert reg.translation_key is not None
        name = translations[reg.translation_key]["name"]
        state = hass.states.get(reg.entity_id)
        assert state is not None
        assert state.attributes["friendly_name"].endswith(f" {name}")

    await _unload(hass, entry)


@pytest.mark.parametrize("kind", [*DeviceKind, None])
def test_device_event_consumption_follows_the_device_kind(kind: DeviceKind | None) -> None:
    """Only a camera or a doorbell consumes a detection; an unknown model does not."""
    event = detection_event(DetectionType.PERSON, t_ms=now_ms())
    assert detections.device_event_consumed(kind, event) is (
        kind in {DeviceKind.CAMERA, DeviceKind.DOORBELL}
    )


async def test_dedupe_the_same_push_twice_fires_one_event(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The library drops the second copy; the integration keeps no seen-set."""
    entry = await set_up_warm(hass, seed_warm_cache)
    detection_states = record_states(hass, _detection_entity_id(hass))
    deduplicator = built_clients[-1].deduplicator
    assert deduplicator is not None

    fake_station.push_camera_event(DetectionType.PERSON)
    await wait_until(lambda: len(detection_states) == 1)
    fake_station.push_camera_event(DetectionType.PERSON)
    # A positive signal that the second copy arrived and was processed.
    await wait_until(lambda: deduplicator.dropped_duplicates == 1)
    await hass.async_block_till_done()

    assert _fired(detection_states) == detection_states
    assert len(detection_states) == 1

    await _unload(hass, entry)


async def test_dedupe_the_same_push_after_a_reconnect_fires_no_second_event(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A copy re-delivered on a new session is still the same occurrence.

    The cross-channel FCM half is proven by the library's own ``EventDeduplicator``
    tests: these tests run ``push=False``.
    """
    entry = await set_up_warm(hass, seed_warm_cache)
    detection_states = record_states(hass, _detection_entity_id(hass))
    deduplicator = built_clients[-1].deduplicator
    assert deduplicator is not None

    fake_station.push_camera_event(DetectionType.PERSON)
    await wait_until(lambda: len(_fired(detection_states)) == 1)
    fake_station.push_camera_event(DetectionType.PERSON)
    await wait_until(lambda: deduplicator.dropped_duplicates == 1)

    station = entry.runtime_data.coordinators[SYNTHETIC.station_sn].station
    fake_station.send_close()
    # A reconnect within the loss grace shows nothing on the panel; wait on the session.
    await wait_until(lambda: not station.connected, timeout=10)
    await wait_until(lambda: station.connected, timeout=30)
    await hass.async_block_till_done()

    fake_station.push_camera_event(DetectionType.PERSON)
    await wait_until(lambda: deduplicator.dropped_duplicates == 2)
    await hass.async_block_till_done()

    assert len(_fired(detection_states)) == 1

    await _unload(hass, entry)


async def test_dedupe_an_ecb_copy_of_a_delivered_detection_fires_nothing_new(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The ECB copy of an occurrence delivered under GCM is a duplicate."""
    entry = await set_up_warm(hass, seed_warm_cache)
    detection_states = record_states(hass, _detection_entity_id(hass))
    deduplicator = built_clients[-1].deduplicator
    assert deduplicator is not None

    fake_station.push_camera_event(DetectionType.PERSON)
    await wait_until(lambda: len(detection_states) == 1)
    fake_station.push_camera_event(DetectionType.PERSON, cipher=FrameCipher.ECB)
    await wait_until(lambda: deduplicator.dropped_duplicates == 1)
    await hass.async_block_till_done()

    assert len(detection_states) == 1

    await _unload(hass, entry)


async def test_an_ecb_detection_of_a_new_occurrence_is_still_shown(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """An unauthenticated detection is still shown as a detection."""
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = _detection_entity_id(hass)
    detection_states = record_states(hass, entity_id)
    deduplicator = built_clients[-1].deduplicator
    assert deduplicator is not None

    fake_station.push_camera_event(DetectionType.PERSON)
    await wait_until(lambda: len(detection_states) == 1)
    fake_station.push_camera_event(DetectionType.PERSON, cipher=FrameCipher.ECB)
    await wait_until(lambda: deduplicator.dropped_duplicates == 1)

    fake_station.push_camera_event(DetectionType.MOTION, cipher=FrameCipher.ECB)
    await wait_until(lambda: len(detection_states) == 2)

    state = hass.states.get(entity_id)
    assert state is not None
    assert state.attributes["event_type"] == "motion"
    assert deduplicator.dropped_duplicates == 1

    await _unload(hass, entry)


async def test_dedupe_an_enriching_copy_fires_no_new_event(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A later copy that only adds media paths is not a new detection.

    Fed through the router: the library marks such a copy ``enriches=True``, and a
    FakeStation push cannot carry media its first copy lacked.
    """
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = _detection_entity_id(hass)
    detection_states = record_states(hass, entity_id)
    events = async_capture_events(hass, EVENT_EUFY_HOME_SECURITY)

    fake_station.push_camera_event(DetectionType.PERSON)
    await wait_until(lambda: len(detection_states) == 1)
    before = hass.states.get(entity_id)

    await _handle(
        hass,
        entry,
        detection_event(
            DetectionType.PERSON,
            t_ms=_FAKE_TRIGGER_MS,
            crop_path="/zx/crop.jpg",
            enriches=True,
        ),
    )

    assert len(detection_states) == 1
    assert hass.states.get(entity_id) == before
    assert events == []

    await _unload(hass, entry)


async def test_dedupe_a_p2p_and_a_cloud_copy_of_one_detection_fire_one_event(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The station's copy and the cloud's, same ``unique_id``, fire once.

    The cloud copy's outer time is about 3 s after the P2P copy's, so only the
    payload's ``unique_id`` joins them; the library de-duplicates on it and the
    integration keeps no seen-set of its own. There is no fake FCM listener, so the
    cloud copy is decoded from its data message and handed to the client's push
    callback, the path the listener takes.
    """
    entry = await set_up_warm(hass, seed_warm_cache)
    detection_states = record_states(hass, _detection_entity_id(hass))
    eufy = built_clients[-1]
    deduplicator = eufy.deduplicator
    assert deduplicator is not None
    t_ms = now_ms()
    inner = {
        "msg_type": 18,
        "event_type": int(DetectionType.PERSON),
        "device_sn": SYNTHETIC.camera_sn,
        "station_sn": SYNTHETIC.station_sn,
        "channel": 0,
        "trigger_time": t_ms,
        "unique_id": "0123456789abcdef0123456789abcdef",
        "record_id": 4242,
    }

    fake_station.send_json(
        FrameType.NOTIFY_PAYLOAD,
        {"cmd": 2037, "payload": json.dumps(inner)},
        cipher=FrameCipher.GCM,
    )
    await wait_until(lambda: len(detection_states) == 1)

    cloud = decode_push(
        {
            "station_sn": SYNTHETIC.station_sn,
            "device_sn": SYNTHETIC.camera_sn,
            "event_time": str(t_ms // 1000 + 3),
            "span_id": "synthetic-span",
            # FCM carries the station's payload base64-encoded.
            "payload": base64.b64encode(json.dumps(inner).encode()).decode(),
        }
    )
    assert cloud.dedupe_key == "unique:0123456789abcdef0123456789abcdef"
    eufy._on_push(cloud)  # the FCM listener's own entry point
    await wait_until(lambda: deduplicator.dropped_duplicates == 1)
    await hass.async_block_till_done()

    assert len(detection_states) == 1

    await _unload(hass, entry)


def _occurrence(t_ms: int, unique_id: str) -> dict[str, object]:
    """One person detection's inner payload, as both channels carry it."""
    return {
        "msg_type": 18,
        "event_type": int(DetectionType.PERSON),
        "device_sn": SYNTHETIC.camera_sn,
        "station_sn": SYNTHETIC.station_sn,
        "channel": 0,
        "trigger_time": t_ms,
        "unique_id": unique_id,
    }


def _send_p2p(fake_station: FakeStation, inner: dict[str, object], *, thumb: bool) -> None:
    """The station's own copy over P2P, with its bound thumbnail record when ``thumb``."""
    payload = dict(inner)
    if thumb:
        payload["rec_content"] = [
            {
                "device_sn": SYNTHETIC.camera_sn,
                "thumb_path": PUSHED_THUMB_PATH,
                "station_sn": SYNTHETIC.station_sn,
            }
        ]
    fake_station.send_json(
        FrameType.NOTIFY_PAYLOAD,
        {"cmd": 2037, "payload": json.dumps(payload)},
        cipher=FrameCipher.GCM,
    )


def _cloud_copy(inner: dict[str, object], *, span: str, clip: bool = False) -> SecurityEvent:
    """The cloud's copy, decoded from its FCM data message; ``clip`` adds its recording."""
    payload = dict(inner)
    if clip:
        payload["file_path"] = "/zx/Camera00/clip.zxvideo"
    trigger_ms = inner["trigger_time"]
    assert isinstance(trigger_ms, int)
    event = decode_push(
        {
            "station_sn": SYNTHETIC.station_sn,
            "device_sn": SYNTHETIC.camera_sn,
            "event_time": str(trigger_ms // 1000 + 3),
            "span_id": span,
            "payload": base64.b64encode(json.dumps(payload).encode()).decode(),
        }
    )
    assert event.frame_cipher is None
    return event


def _deliver_cloud(eufy: EufySecurity, entry: MockConfigEntry, event: SecurityEvent) -> bool:
    """A cloud copy as the client delivers one: through its de-duplicator, then to
    its subscriber. Whether the de-duplicator let it through."""
    deduplicator = eufy.deduplicator
    assert deduplicator is not None
    admitted = deduplicator.admit(event)
    if admitted is not None:
        entry.runtime_data.router.handle(admitted)
    return admitted is not None


async def test_dedupe_a_p2p_detection_then_its_cloud_copies_alert_once(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """P2P first: the cloud's plain copy is dropped, its copy with a new path enriches.

    One detection event and one off-to-on edge of the person sensor in all. The
    library de-duplicates on the payload's ``unique_id`` and marks the copy with the
    recording ``enriches``; the router fires nothing for it.
    """
    entry = await set_up_warm(hass, seed_warm_cache)
    eufy = built_clients[-1]
    detection_states = record_states(hass, _detection_entity_id(hass))
    person_states = record_states(hass, _person_sensor_id(hass))
    fallback = async_capture_events(hass, EVENT_EUFY_HOME_SECURITY)
    inner = _occurrence(now_ms(), "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")

    _send_p2p(fake_station, inner, thumb=True)
    await wait_until(lambda: len(detection_states) == 1)

    assert not _deliver_cloud(eufy, entry, _cloud_copy(inner, span="span-1"))
    assert _deliver_cloud(eufy, entry, _cloud_copy(inner, span="span-2", clip=True)), (
        "the copy with a recording was not admitted as an enrichment"
    )
    await hass.async_block_till_done()

    assert len(_fired(detection_states)) == 1
    assert person_states == [STATE_ON]
    assert fallback == []

    await _unload(hass, entry)


async def test_dedupe_a_cloud_detection_then_its_p2p_copy_alerts_once(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Cloud first: the station's copy with its thumbnail enriches, a bare one is dropped."""
    entry = await set_up_warm(hass, seed_warm_cache)
    eufy = built_clients[-1]
    deduplicator = eufy.deduplicator
    assert deduplicator is not None
    router = entry.runtime_data.router
    detection_states = record_states(hass, _detection_entity_id(hass))
    person_states = record_states(hass, _person_sensor_id(hass))
    fallback = async_capture_events(hass, EVENT_EUFY_HOME_SECURITY)
    inner = _occurrence(now_ms(), "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")

    assert _deliver_cloud(eufy, entry, _cloud_copy(inner, span="span-1"))
    await hass.async_block_till_done()
    assert len(_fired(detection_states)) == 1

    _send_p2p(fake_station, inner, thumb=True)
    await wait_until(lambda: router.events_received_by_cipher(SYNTHETIC.station_sn)["gcm"] == 1)
    _send_p2p(fake_station, inner, thumb=False)
    await wait_until(lambda: deduplicator.dropped_duplicates == 1)
    await hass.async_block_till_done()

    assert len(_fired(detection_states)) == 1
    assert person_states == [STATE_ON]
    assert fallback == []

    await _unload(hass, entry)
