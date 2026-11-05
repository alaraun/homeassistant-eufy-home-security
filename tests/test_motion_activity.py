"""Native counter-based motion-activity events through the real HA event router.

These use the integration's synthetic FakeStation and fake cloud fixtures; no
real credentials, stations, cloud requests or Home Assistant installations.
The library observer itself is tested separately in the library checkout.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from conftest import entity_id_for, set_up_warm
from eufy_home_security import (
    EufySecurity,
    MotionDetected,
    MotionRecorded,
)
from eufy_home_security.models import PARAM_DETECTED_EVENTS, PARAM_RECORDED_EVENTS
from eufy_home_security.testing import SYNTHETIC, FakeStation
from homeassistant.components.event import DOMAIN as EVENT_DOMAIN
from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import HomeAssistant


def _entity_id(hass: HomeAssistant, key: str) -> str:
    return entity_id_for(hass, EVENT_DOMAIN, SYNTHETIC.camera_sn, key)


async def test_activity_events_register_beside_original_detection(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """All three event entities exist on the same synthetic camera."""
    entry = await set_up_warm(hass, seed_warm_cache)
    for key in ("detection", "motion_detected", "motion_recorded"):
        entity_id = _entity_id(hass, key)
        state = hass.states.get(entity_id)
        assert state is not None
        assert state.state == STATE_UNKNOWN
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_aggregate_counter_motion_does_not_fire_the_original_detection(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """One +3 observation is one motion event with count=3, not 3 fabricated times."""
    entry = await set_up_warm(hass, seed_warm_cache)
    motion_id = _entity_id(hass, "motion_detected")
    detection_id = _entity_id(hass, "detection")
    recorded_id = _entity_id(hass, "motion_recorded")
    before_detection = hass.states.get(detection_id)
    assert before_detection is not None
    observed_at = datetime.now(UTC)
    entry.runtime_data.router.handle(
        MotionDetected(
            station_sn=SYNTHETIC.station_sn,
            device_sn=SYNTHETIC.camera_sn,
            source="counter",
            count=3,
            observed_at=observed_at,
        )
    )
    await hass.async_block_till_done()
    state = hass.states.get(motion_id)
    assert state is not None
    assert state.state != STATE_UNKNOWN
    assert state.attributes["event_type"] == "motion"
    assert state.attributes["source"] == "counter"
    assert state.attributes["count"] == 3
    assert state.attributes["observed_at"] == observed_at.isoformat()
    original = hass.states.get(detection_id)
    assert original is not None
    assert original.state == before_detection.state
    assert original.attributes == before_detection.attributes
    recorded = hass.states.get(recorded_id)
    assert recorded is not None
    assert recorded.state == STATE_UNKNOWN
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_push_motion_has_one_count_and_does_not_fire_recorded(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    entry = await set_up_warm(hass, seed_warm_cache)
    entry.runtime_data.router.handle(
        MotionDetected(
            station_sn=SYNTHETIC.station_sn,
            device_sn=SYNTHETIC.camera_sn,
            source="push",
            count=1,
            observed_at=datetime.now(UTC),
        )
    )
    await hass.async_block_till_done()
    state = hass.states.get(_entity_id(hass, "motion_detected"))
    assert state is not None
    assert state.attributes["source"] == "push"
    assert state.attributes["count"] == 1
    recorded = hass.states.get(_entity_id(hass, "motion_recorded"))
    assert recorded is not None
    assert recorded.state == STATE_UNKNOWN
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_recording_counter_event_is_aggregate_and_does_not_query_history(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """One +3 recording-counter observation is one event, not three video IDs."""
    entry = await set_up_warm(hass, seed_warm_cache)
    observed_at = datetime.now(UTC)
    entry.runtime_data.router.handle(
        MotionRecorded(
            station_sn=SYNTHETIC.station_sn,
            device_sn=SYNTHETIC.camera_sn,
            count=3,
            observed_at=observed_at,
        )
    )
    await hass.async_block_till_done()
    state = hass.states.get(_entity_id(hass, "motion_recorded"))
    assert state is not None
    assert state.state != STATE_UNKNOWN
    assert state.attributes["event_type"] == "recorded"
    assert state.attributes["source"] == "counter"
    assert state.attributes["count"] == 3
    assert state.attributes["observed_at"] == observed_at.isoformat()
    assert "record_id" not in state.attributes
    assert "record_ref" not in state.attributes
    assert "started_at" not in state.attributes
    assert "ended_at" not in state.attributes
    motion = hass.states.get(_entity_id(hass, "motion_detected"))
    assert motion is not None
    assert motion.state == STATE_UNKNOWN
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_activity_wrong_station_or_unpaired_camera_is_ignored(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The event router only routes pairs of station and registered camera."""
    entry = await set_up_warm(hass, seed_warm_cache)
    for station_sn, device_sn in (
        ("T8030P0000000002", SYNTHETIC.camera_sn),
        (SYNTHETIC.station_sn, "T8160P0000000002"),
    ):
        entry.runtime_data.router.handle(
            MotionDetected(
                station_sn=station_sn,
                device_sn=device_sn,
                source="counter",
                count=5,
                observed_at=datetime.now(UTC),
            )
        )
    await hass.async_block_till_done()
    motion = hass.states.get(_entity_id(hass, "motion_detected"))
    assert motion is not None
    assert motion.state == STATE_UNKNOWN
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_parameter_counters_reach_native_ha_events_without_detection_push(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Real fake-P2P parameter dumps propagate through library, router and HA.

    This deliberately sends no detection push: activity counters can be useful when
    a HomeBase security mode suppresses notification delivery.
    """
    fake_station.params[0][PARAM_DETECTED_EVENTS] = "100"
    fake_station.params[0][PARAM_RECORDED_EVENTS] = "50"
    entry = await set_up_warm(hass, seed_warm_cache)
    station = entry.runtime_data.coordinators[SYNTHETIC.station_sn].station
    await station.async_update()  # establish both baselines, never fire on startup
    await hass.async_block_till_done()
    detected_id = _entity_id(hass, "motion_detected")
    recorded_id = _entity_id(hass, "motion_recorded")
    detection_id = _entity_id(hass, "detection")
    baseline_detection = hass.states.get(detection_id)
    assert baseline_detection is not None
    initial_detected = hass.states.get(detected_id)
    initial_recorded = hass.states.get(recorded_id)
    assert initial_detected is not None and initial_detected.state == STATE_UNKNOWN
    assert initial_recorded is not None and initial_recorded.state == STATE_UNKNOWN

    fake_station.params[0][PARAM_DETECTED_EVENTS] = "103"
    await station.async_update()
    await hass.async_block_till_done()
    detected = hass.states.get(detected_id)
    assert detected is not None
    assert detected.attributes["event_type"] == "motion"
    assert detected.attributes["count"] == 3
    assert detected.attributes["source"] == "counter"
    recorded = hass.states.get(recorded_id)
    assert recorded is not None
    assert recorded.state == STATE_UNKNOWN
    unchanged_detection = hass.states.get(detection_id)
    assert unchanged_detection is not None
    assert unchanged_detection.state == baseline_detection.state
    assert unchanged_detection.attributes == baseline_detection.attributes

    fake_station.params[0][PARAM_RECORDED_EVENTS] = "52"
    await station.async_update()
    await hass.async_block_till_done()
    recorded = hass.states.get(recorded_id)
    assert recorded is not None
    assert recorded.attributes["event_type"] == "recorded"
    assert recorded.attributes["source"] == "counter"
    assert recorded.attributes["count"] == 2
    await hass.config_entries.async_unload(entry.entry_id)
