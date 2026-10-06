"""Binary sensors: a read-only bool setting, and each camera's detection sensors.

Detections are router-fed real library events unless a test says end to end; the
hold runs from the detection's own time.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import Any, Final

import pytest
from conftest import (
    SENSOR_SN,
    add_motion_sensor,
    advance_to_poll,
    detection_event,
    entity_id_for,
    now_ms,
    record_states,
    seed_setting,
    set_up_warm,
    state_of,
    wait_until,
)
from eufy_home_security import (
    DetectionType,
    EufySecurity,
    FrameCipher,
    Station,
    entity_unique_id,
)
from eufy_home_security.devices import Setting
from eufy_home_security.testing import SYNTHETIC, FakeCloud, FakeStation
from homeassistant.components.binary_sensor import DOMAIN as BINARY_SENSOR_DOMAIN
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.event import DOMAIN as EVENT_DOMAIN
from homeassistant.const import STATE_OFF, STATE_ON, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.eufy_home_security import detections
from custom_components.eufy_home_security.const import (
    CONF_DETECTION_HOLD,
    DETECTION_EVENT_KEY,
    DOMAIN,
)


async def test_a_read_only_bool_setting_is_a_disabled_diagnostic_binary_sensor(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A readable bool with no write is a binary sensor showing the decoded value."""
    real = Station.settings_for

    def settings_for(self: Station, device_sn: str | None = None) -> tuple[Setting, ...]:
        return tuple(
            dataclasses.replace(s, writable=False) if s.key == "led_on_off" else s
            for s in real(self, device_sn)
        )

    monkeypatch.setattr(Station, "settings_for", settings_for)
    seed_setting(fake_station, "led_on_off", 1)
    entry = await set_up_warm(hass, seed_warm_cache)
    registry = er.async_get(hass)
    entity_id = entity_id_for(hass, BINARY_SENSOR_DOMAIN, SYNTHETIC.camera_sn, "led_on_off")
    registered = registry.async_get(entity_id)
    assert registered is not None
    assert registered.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert registered.entity_category is EntityCategory.DIAGNOSTIC
    registry.async_update_entity(entity_id, disabled_by=None)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert state_of(hass, entity_id) == STATE_ON

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


# The four detection sensors of a camera, in the order they are added.
_DETECTION_KEYS: Final = ("motion_detected", "person_detected", "pet_detected", "vehicle_detected")
_DETECTION_DEVICE_CLASSES: Final[dict[str, BinarySensorDeviceClass | None]] = {
    "motion_detected": BinarySensorDeviceClass.MOTION,
    "person_detected": None,
    "pet_detected": None,
    "vehicle_detected": None,
}


def _detection_entity_id(hass: HomeAssistant, key: str) -> str:
    """The synthetic camera's detection binary sensor of ``key``."""
    return entity_id_for(hass, BINARY_SENSOR_DOMAIN, SYNTHETIC.camera_sn, key)


def _detection_state(hass: HomeAssistant, key: str) -> str | None:
    """The state of the synthetic camera's detection binary sensor of ``key``."""
    return state_of(hass, _detection_entity_id(hass, key))


async def _handle_detection(
    hass: HomeAssistant, entry: MockConfigEntry, detection: DetectionType, **kwargs: Any
) -> None:
    """Hand a real library detection to the entry's router, and let it settle.

    Router-fed because FakeStation's trigger time is years in the past, so its
    pushes are always late under the hold.
    """
    entry.runtime_data.router.handle(detection_event(detection, **kwargs))
    await hass.async_block_till_done()


async def test_a_person_detection_turns_on_person_detected_and_off_after_the_hold(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A current person detection lights only the person sensor, for the hold.

    The station announces a detection and never its end, so the sensor turns itself
    off on a timer. The default hold is 10 s: on at +5 s, off at +11 s.
    """
    entry = await set_up_warm(hass, seed_warm_cache)
    for key in _DETECTION_KEYS:
        assert _detection_state(hass, key) == STATE_OFF, key

    await _handle_detection(hass, entry, DetectionType.PERSON, t_ms=now_ms())

    assert _detection_state(hass, "person_detected") == STATE_ON
    for key in ("motion_detected", "pet_detected", "vehicle_detected"):
        assert _detection_state(hass, key) == STATE_OFF, key

    await advance_to_poll(hass, 5)
    assert _detection_state(hass, "person_detected") == STATE_ON
    await advance_to_poll(hass, 11)
    assert _detection_state(hass, "person_detected") == STATE_OFF

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


def test_hold_remaining_seconds_counts_from_the_event_time() -> None:
    """The hold runs from the detection's own time, never longer than the hold.

    A missing time is held from arrival; a time ahead of the clock ages nothing, so a
    forged future time cannot extend the hold.
    """
    now = 1_800_000_000_000.0
    assert detections.hold_remaining_seconds(None, 10, now) == 10
    assert detections.hold_remaining_seconds(int(now) - 8000, 10, now) == 2
    assert detections.hold_remaining_seconds(int(now) - 12000, 10, now) <= 0
    assert detections.hold_remaining_seconds(int(now) + 60000, 10, now) == 10


# Every mapped detection and the one sensor it turns on.
_DETECTION_CLASS_CASES: Final = (
    (DetectionType.MOTION, "motion_detected"),
    (DetectionType.PERSON, "person_detected"),
    (DetectionType.IDENTITY_PERSON, "person_detected"),
    (DetectionType.STRANGER_PERSON, "person_detected"),
    (DetectionType.PET, "pet_detected"),
    (DetectionType.DOG, "pet_detected"),
    (DetectionType.DOG_LICK, "pet_detected"),
    (DetectionType.DOG_POOP, "pet_detected"),
    (DetectionType.VEHICLE, "vehicle_detected"),
)


async def _unload(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_each_camera_has_four_detection_binary_sensors_and_the_motion_sensor_none(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A camera has motion, person, pet and vehicle sensors; the T8910 and station none.

    They are primary readings, not diagnostics, each with its own entity id.
    """
    add_motion_sensor(fake_station, fake_cloud)
    entry = await set_up_warm(hass, seed_warm_cache)
    registry = er.async_get(hass)

    entity_ids = set()
    for key in _DETECTION_KEYS:
        entity_id = _detection_entity_id(hass, key)
        registered = registry.async_get(entity_id)
        assert registered is not None, key
        # Only motion is motion; a person, pet or vehicle has no device class.
        assert registered.original_device_class == _DETECTION_DEVICE_CLASSES[key], key
        assert registered.entity_category is None, key
        entity_ids.add(entity_id)
    assert len(entity_ids) == len(_DETECTION_KEYS)

    for serial in (SENSOR_SN, SYNTHETIC.station_sn):
        for key in _DETECTION_KEYS:
            assert (
                registry.async_get_entity_id(
                    BINARY_SENSOR_DOMAIN, DOMAIN, entity_unique_id(serial, key)
                )
                is None
            ), (serial, key)

    await _unload(hass, entry)


async def test_a_detection_during_a_failed_poll_is_shown_while_the_session_is_up(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Detection sensors follow the push session, not the poll's last result."""
    entry = await set_up_warm(hass, seed_warm_cache)
    coordinator = entry.runtime_data.coordinators[SYNTHETIC.station_sn]
    assert coordinator.station.connected

    coordinator.async_set_update_error(UpdateFailed("a poll timed out"))
    await hass.async_block_till_done()
    assert _detection_state(hass, "person_detected") == STATE_OFF

    await _handle_detection(hass, entry, DetectionType.PERSON, t_ms=now_ms())
    assert _detection_state(hass, "person_detected") == STATE_ON

    await _unload(hass, entry)


@pytest.mark.parametrize(
    ("detection", "expected"),
    _DETECTION_CLASS_CASES,
    ids=[detection.name for detection, _ in _DETECTION_CLASS_CASES],
)
async def test_a_detection_turns_on_only_its_own_class(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    detection: DetectionType,
    expected: str,
) -> None:
    """Each detection turns on exactly the sensor of its class, and nothing else."""
    entry = await set_up_warm(hass, seed_warm_cache)

    await _handle_detection(hass, entry, detection, t_ms=now_ms())

    for key in _DETECTION_KEYS:
        assert _detection_state(hass, key) == (STATE_ON if key == expected else STATE_OFF), key

    await _unload(hass, entry)


async def test_the_detection_hold_is_measured_from_the_event_time(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """An event 8 s old under a 10 s hold is on at +1 s and off at +3 s."""
    entry = await set_up_warm(hass, seed_warm_cache)

    await _handle_detection(hass, entry, DetectionType.PERSON, t_ms=now_ms() - 8000)
    assert _detection_state(hass, "person_detected") == STATE_ON

    await advance_to_poll(hass, 1)
    assert _detection_state(hass, "person_detected") == STATE_ON
    await advance_to_poll(hass, 3)
    assert _detection_state(hass, "person_detected") == STATE_OFF

    await _unload(hass, entry)


async def test_a_late_detection_fires_its_event_but_leaves_the_detection_sensor_off(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A push whose hold ran out before it arrived fires its event and lights nothing.

    End to end through FakeStation, whose trigger time is in 2023.
    """
    entry = await set_up_warm(hass, seed_warm_cache)
    person_id = _detection_entity_id(hass, "person_detected")
    person_states = record_states(hass, person_id)
    event_states = record_states(
        hass, entity_id_for(hass, EVENT_DOMAIN, SYNTHETIC.camera_sn, DETECTION_EVENT_KEY)
    )

    fake_station.push_camera_event(DetectionType.PERSON)
    await wait_until(lambda: len(event_states) == 1)
    await hass.async_block_till_done()

    assert person_states == []
    assert state_of(hass, person_id) == STATE_OFF

    await _unload(hass, entry)


async def test_a_newer_detection_extends_the_detection_hold(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A newer detection during the hold moves the off edge to its own time plus the hold."""
    entry = await set_up_warm(hass, seed_warm_cache)

    await _handle_detection(hass, entry, DetectionType.PERSON, t_ms=now_ms() - 8000)
    await _handle_detection(hass, entry, DetectionType.PERSON, t_ms=now_ms())

    await advance_to_poll(hass, 3)
    assert _detection_state(hass, "person_detected") == STATE_ON
    await advance_to_poll(hass, 11)
    assert _detection_state(hass, "person_detected") == STATE_OFF

    await _unload(hass, entry)


async def test_an_older_detection_never_shortens_the_detection_hold(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """An older detection arriving after a newer one leaves the later off edge in place."""
    entry = await set_up_warm(hass, seed_warm_cache)

    await _handle_detection(hass, entry, DetectionType.PERSON, t_ms=now_ms())
    await _handle_detection(hass, entry, DetectionType.PERSON, t_ms=now_ms() - 8000)

    await advance_to_poll(hass, 3)
    assert _detection_state(hass, "person_detected") == STATE_ON
    await advance_to_poll(hass, 11)
    assert _detection_state(hass, "person_detected") == STATE_OFF

    await _unload(hass, entry)


async def test_two_detections_in_the_same_second_keep_one_detection_hold(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Same class, same time: on once, one off edge. Different classes: both on."""
    entry = await set_up_warm(hass, seed_warm_cache)
    person_states = record_states(hass, _detection_entity_id(hass, "person_detected"))
    t_ms = now_ms()

    await _handle_detection(hass, entry, DetectionType.PERSON, t_ms=t_ms)
    await _handle_detection(hass, entry, DetectionType.PERSON, t_ms=t_ms)
    assert person_states == [STATE_ON]

    await advance_to_poll(hass, 11)
    assert person_states == [STATE_ON, STATE_OFF]

    t_ms = now_ms()
    await _handle_detection(hass, entry, DetectionType.PERSON, t_ms=t_ms)
    await _handle_detection(hass, entry, DetectionType.MOTION, t_ms=t_ms)
    assert _detection_state(hass, "person_detected") == STATE_ON
    assert _detection_state(hass, "motion_detected") == STATE_ON

    await _unload(hass, entry)


async def test_a_detection_without_an_event_time_is_held_from_arrival(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A detection with no usable time is held from its arrival."""
    entry = await set_up_warm(hass, seed_warm_cache)

    await _handle_detection(hass, entry, DetectionType.PERSON, t_ms=None)
    assert _detection_state(hass, "person_detected") == STATE_ON

    await advance_to_poll(hass, 11)
    assert _detection_state(hass, "person_detected") == STATE_OFF

    await _unload(hass, entry)


async def test_a_detection_time_ahead_of_the_clock_holds_no_longer_than_the_hold(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A time a minute ahead of the clock is held for the hold and no longer."""
    entry = await set_up_warm(hass, seed_warm_cache)

    await _handle_detection(hass, entry, DetectionType.PERSON, t_ms=now_ms() + 60000)
    assert _detection_state(hass, "person_detected") == STATE_ON

    await advance_to_poll(hass, 11)
    assert _detection_state(hass, "person_detected") == STATE_OFF

    await _unload(hass, entry)


async def test_an_ecb_detection_turns_its_detection_sensor_on(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A detection delivered AES-ECB is shown like a GCM one."""
    entry = await set_up_warm(hass, seed_warm_cache)

    await _handle_detection(
        hass, entry, DetectionType.PERSON, t_ms=now_ms(), cipher=FrameCipher.ECB
    )
    assert _detection_state(hass, "person_detected") == STATE_ON

    await _unload(hass, entry)


async def test_an_enriching_copy_does_not_restart_the_detection_hold(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A copy that only adds media is not a new detection: the hold ends as first set.

    The router drops enriching copies before dispatch, so the sensor never sees one.
    """
    entry = await set_up_warm(hass, seed_warm_cache)

    await _handle_detection(hass, entry, DetectionType.PERSON, t_ms=now_ms() - 8000)
    assert _detection_state(hass, "person_detected") == STATE_ON
    await _handle_detection(hass, entry, DetectionType.PERSON, t_ms=now_ms(), enriches=True)

    await advance_to_poll(hass, 3)
    assert _detection_state(hass, "person_detected") == STATE_OFF

    await _unload(hass, entry)


async def test_the_detection_hold_option_reaches_the_sensors_after_reload(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A saved 30 s hold is what the sensors use: on at +11 s, off at +31 s.

    The hold is read once at platform setup, so an entry set up with the option is
    the state the options flow's reload leaves behind.
    """
    entry = await set_up_warm(hass, seed_warm_cache, options={CONF_DETECTION_HOLD: 30})

    await _handle_detection(hass, entry, DetectionType.PERSON, t_ms=now_ms())
    assert _detection_state(hass, "person_detected") == STATE_ON

    await advance_to_poll(hass, 11)
    assert _detection_state(hass, "person_detected") == STATE_ON
    await advance_to_poll(hass, 31)
    assert _detection_state(hass, "person_detected") == STATE_OFF

    await _unload(hass, entry)
