"""Charging, solar, power-manager, battery-low and station storage state.

Values come from the library's typed state. The parameter ids below are fixture
values only: they say what the fake station reports, and the integration never
names one. An entity exists once a state reports its field; a field that later reads
None shows as unknown, never off.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import Final

import pytest
from conftest import (
    SENSOR_SN,
    add_motion_sensor,
    advance_to_poll,
    entity_id_for,
    set_up_warm,
    state_of,
    wait_until,
)
from eufy_home_security import EufySecurity, Station, entity_unique_id
from eufy_home_security.devices import Setting
from eufy_home_security.testing import SYNTHETIC, FakeCloud, FakeStation
from homeassistant.components.binary_sensor import DOMAIN as BINARY_SENSOR_DOMAIN
from homeassistant.components.sensor import ATTR_STATE_CLASS
from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.components.update import DOMAIN as UPDATE_DOMAIN
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_OFF,
    STATE_ON,
    STATE_UNKNOWN,
    EntityCategory,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.eufy_home_security.const import (
    BATTERY_LOW_KEY,
    BATTERY_TEMPERATURE_KEY,
    CHARGING_KEY,
    DETECTED_EVENTS_KEY,
    DOMAIN,
    FIRMWARE_KEY,
    POLL_INTERVAL_SECONDS,
    RECORDED_EVENTS_KEY,
    SOLAR_CHARGING_KEY,
    SOLAR_INTENSITY_KEY,
    STORAGE_PROBLEM_KEY,
    WORKING_DAYS_KEY,
)

_STATION_BLOCK: Final = 255
_CAMERA_CHANNEL: Final = 0
_SENSOR_CHANNEL: Final = 1
_BATTERY_TEMPERATURE_PARAM: Final = 1138
_WORKING_DAYS_PARAM: Final = 1191
_DETECTED_EVENTS_PARAM: Final = 1192
_RECORDED_EVENTS_PARAM: Final = 1193
_SOLAR_INTENSITY_PARAM: Final = 1309
_SIREN_ACTION_AWAY_PARAM: Final = 1509
_SIREN_ACTION_HOME_PARAM: Final = 1510
_SENSOR_LOW_BATTERY_PARAM: Final = 1601
_POWER_SOURCE_PARAM: Final = 2111
_STORAGE_STATUS_PARAM: Final = 1135
_SUBSYSTEM_FIRMWARE_PARAM: Final = 5006

# Built-in solar (the library's declared code table).
_SOLAR_SOURCE: Final = "4"
# A code outside the library's table: charging (not 0 or 2), not solar.
_UNMAPPED_SOURCE: Final = "99"

_POWER_MANAGER: Final = {
    _BATTERY_TEMPERATURE_PARAM: "21",
    _WORKING_DAYS_PARAM: "12",
    _DETECTED_EVENTS_PARAM: "30",
    _RECORDED_EVENTS_PARAM: "25",
    _SOLAR_INTENSITY_PARAM: "6",
}


def _entity_id_or_none(hass: HomeAssistant, domain: str, serial: str, key: str) -> str | None:
    """The entity of ``key`` on ``serial``, or None when none was made."""
    return er.async_get(hass).async_get_entity_id(domain, DOMAIN, entity_unique_id(serial, key))


async def test_camera_power_state_is_shown_with_attributes(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Charging and solar charging from the power source, and the power-manager figures."""
    fake_station.params[_CAMERA_CHANNEL].update(
        {_POWER_SOURCE_PARAM: _SOLAR_SOURCE, **_POWER_MANAGER}
    )
    entry = await set_up_warm(hass, seed_warm_cache)
    camera = SYNTHETIC.camera_sn
    registry = er.async_get(hass)

    charging = hass.states.get(entity_id_for(hass, BINARY_SENSOR_DOMAIN, camera, CHARGING_KEY))
    assert charging is not None
    assert charging.state == STATE_ON
    assert charging.attributes[ATTR_DEVICE_CLASS] == "battery_charging"
    assert charging.attributes["solar_charging"] is True
    assert charging.attributes["power_source"] == 4

    solar = entity_id_for(hass, BINARY_SENSOR_DOMAIN, camera, SOLAR_CHARGING_KEY)
    assert state_of(hass, solar) == STATE_ON

    expected = {
        SOLAR_INTENSITY_KEY: ("6", None, None, "measurement"),
        BATTERY_TEMPERATURE_KEY: ("21", "temperature", "°C", "measurement"),
        WORKING_DAYS_KEY: ("12", "duration", "d", "measurement"),
        DETECTED_EVENTS_KEY: ("30", None, None, "total"),
        RECORDED_EVENTS_KEY: ("25", None, None, "total"),
    }
    for key, (value, device_class, unit, state_class) in expected.items():
        entity_id = entity_id_for(hass, SENSOR_DOMAIN, camera, key)
        state = hass.states.get(entity_id)
        assert state is not None, key
        assert state.state == value, key
        assert state.attributes.get(ATTR_DEVICE_CLASS) == device_class, key
        assert state.attributes.get(ATTR_UNIT_OF_MEASUREMENT) == unit, key
        assert state.attributes.get(ATTR_STATE_CLASS) == state_class, key
        registered = registry.async_get(entity_id)
        assert registered is not None
        assert registered.entity_category is EntityCategory.DIAGNOSTIC, key

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_an_unmapped_power_source_is_charging_not_solar_and_shown_raw(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A code outside the library's table follows the library: charging, not solar, raw."""
    fake_station.params[_CAMERA_CHANNEL][_POWER_SOURCE_PARAM] = _UNMAPPED_SOURCE
    entry = await set_up_warm(hass, seed_warm_cache)
    camera = SYNTHETIC.camera_sn

    charging = hass.states.get(entity_id_for(hass, BINARY_SENSOR_DOMAIN, camera, CHARGING_KEY))
    assert charging is not None
    assert charging.state == STATE_ON
    assert charging.attributes["power_source"] == 99
    assert charging.attributes["solar_charging"] is False
    solar = entity_id_for(hass, BINARY_SENSOR_DOMAIN, camera, SOLAR_CHARGING_KEY)
    assert state_of(hass, solar) == STATE_OFF

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_field_not_reported_has_no_entity_until_reported_then_reads_unknown(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """No entity while the field is unreported; added with its first report; unknown after.

    The fake camera's default block carries none of these fields, so a model without
    them gets no entities. Once added, an entity stays and a field that reads None is
    unknown, never off.
    """
    entry = await set_up_warm(hass, seed_warm_cache)
    anchor = dt_util.utcnow()
    camera = SYNTHETIC.camera_sn
    station = SYNTHETIC.station_sn

    assert _entity_id_or_none(hass, BINARY_SENSOR_DOMAIN, camera, CHARGING_KEY) is None
    assert _entity_id_or_none(hass, SENSOR_DOMAIN, camera, WORKING_DAYS_KEY) is None
    assert _entity_id_or_none(hass, BINARY_SENSOR_DOMAIN, station, STORAGE_PROBLEM_KEY) is None
    # The camera never reports the motion sensor's own low-battery flag.
    assert _entity_id_or_none(hass, BINARY_SENSOR_DOMAIN, camera, BATTERY_LOW_KEY) is None

    fake_station.params[_CAMERA_CHANNEL][_POWER_SOURCE_PARAM] = "0"
    fake_station.params[_CAMERA_CHANNEL][_WORKING_DAYS_PARAM] = "3"
    await advance_to_poll(hass, POLL_INTERVAL_SECONDS + 1, anchor=anchor)
    await wait_until(
        lambda: _entity_id_or_none(hass, BINARY_SENSOR_DOMAIN, camera, CHARGING_KEY) is not None,
        timeout=15,
    )
    await hass.async_block_till_done()
    charging = entity_id_for(hass, BINARY_SENSOR_DOMAIN, camera, CHARGING_KEY)
    working_days = entity_id_for(hass, SENSOR_DOMAIN, camera, WORKING_DAYS_KEY)
    assert state_of(hass, charging) == STATE_OFF
    assert state_of(hass, working_days) == "3"
    # Only the reported fields gained entities.
    assert _entity_id_or_none(hass, SENSOR_DOMAIN, camera, DETECTED_EVENTS_KEY) is None

    # The session keeps a parameter once reported, so a value the library cannot
    # parse (a negative count) is how the field turns None.
    fake_station.params[_CAMERA_CHANNEL][_POWER_SOURCE_PARAM] = "-1"
    fake_station.params[_CAMERA_CHANNEL][_WORKING_DAYS_PARAM] = "-1"
    await advance_to_poll(hass, 2 * POLL_INTERVAL_SECONDS + 2, anchor=anchor)
    await wait_until(lambda: state_of(hass, charging) == STATE_UNKNOWN, timeout=15)
    assert state_of(hass, working_days) == STATE_UNKNOWN

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_motion_sensor_battery_low(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The motion sensor's own low-battery flag is a diagnostic battery binary sensor."""
    add_motion_sensor(fake_station, fake_cloud)
    fake_station.params[_SENSOR_CHANNEL][_SENSOR_LOW_BATTERY_PARAM] = "1"
    entry = await set_up_warm(hass, seed_warm_cache)

    entity_id = entity_id_for(hass, BINARY_SENSOR_DOMAIN, SENSOR_SN, BATTERY_LOW_KEY)
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == STATE_ON
    assert state.attributes[ATTR_DEVICE_CLASS] == "battery"
    registered = er.async_get(hass).async_get(entity_id)
    assert registered is not None
    assert registered.entity_category is EntityCategory.DIAGNOSTIC
    # A charging source is a camera field: the sensor block has none.
    assert _entity_id_or_none(hass, BINARY_SENSOR_DOMAIN, SENSOR_SN, CHARGING_KEY) is None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_station_storage_problem_follows_storage_ok(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Off for a normal storage code, on for any other, with the raw code as attribute."""
    fake_station.params[_STATION_BLOCK][_STORAGE_STATUS_PARAM] = "0"
    entry = await set_up_warm(hass, seed_warm_cache)
    anchor = dt_util.utcnow()

    entity_id = entity_id_for(hass, BINARY_SENSOR_DOMAIN, SYNTHETIC.station_sn, STORAGE_PROBLEM_KEY)
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == STATE_OFF
    assert state.attributes[ATTR_DEVICE_CLASS] == "problem"
    assert state.attributes["storage_status"] == 0
    registered = er.async_get(hass).async_get(entity_id)
    assert registered is not None
    assert registered.entity_category is EntityCategory.DIAGNOSTIC

    fake_station.params[_STATION_BLOCK][_STORAGE_STATUS_PARAM] = "5"
    await advance_to_poll(hass, POLL_INTERVAL_SECONDS + 1, anchor=anchor)
    await wait_until(lambda: state_of(hass, entity_id) == STATE_ON, timeout=15)
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.attributes["storage_status"] == 5

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_subsystem_firmware_and_siren_actions_are_update_attributes(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The station's subsystem versions and a camera's siren actions, only when reported."""
    fake_station.params[_STATION_BLOCK][_SUBSYSTEM_FIRMWARE_PARAM] = "1.2.3"
    fake_station.params[_CAMERA_CHANNEL][_SIREN_ACTION_AWAY_PARAM] = "3"
    fake_station.params[_CAMERA_CHANNEL][_SIREN_ACTION_HOME_PARAM] = "0"
    add_motion_sensor(fake_station, fake_cloud)
    entry = await set_up_warm(hass, seed_warm_cache)

    station = hass.states.get(
        entity_id_for(hass, UPDATE_DOMAIN, SYNTHETIC.station_sn, FIRMWARE_KEY)
    )
    assert station is not None
    assert station.attributes["subsystem_firmware"] == {"5006": "1.2.3"}
    assert "siren_actions" not in station.attributes

    camera = hass.states.get(entity_id_for(hass, UPDATE_DOMAIN, SYNTHETIC.camera_sn, FIRMWARE_KEY))
    assert camera is not None
    assert camera.attributes["siren_actions"] == {"away": 3, "home": 0}

    sensor = hass.states.get(entity_id_for(hass, UPDATE_DOMAIN, SENSOR_SN, FIRMWARE_KEY))
    assert sensor is not None
    assert "siren_actions" not in sensor.attributes

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_readable_setting_of_the_same_key_keeps_its_unique_id(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A model setting keyed like a state field keeps the entity; no second one is made."""
    real = Station.settings_for

    def settings_for(self: Station, device_sn: str | None = None) -> tuple[Setting, ...]:
        return tuple(
            dataclasses.replace(s, readable=True) if s.key == WORKING_DAYS_KEY else s
            for s in real(self, device_sn)
        )

    monkeypatch.setattr(Station, "settings_for", settings_for)
    fake_station.params[_CAMERA_CHANNEL][_WORKING_DAYS_PARAM] = "3"
    entry = await set_up_warm(hass, seed_warm_cache)

    registry = er.async_get(hass)
    entity_id = entity_id_for(hass, SENSOR_DOMAIN, SYNTHETIC.camera_sn, WORKING_DAYS_KEY)
    registered = registry.async_get(entity_id)
    assert registered is not None
    # The setting sensor's registration: read-only settings are registered off.
    assert registered.disabled_by is er.RegistryEntryDisabler.INTEGRATION

    registry.async_update_entity(entity_id, disabled_by=None)
    caplog.clear()
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    # The setting sensor (no read path in this stub, so unknown), not a second entity.
    assert state_of(hass, entity_id) == STATE_UNKNOWN
    assert "does not generate unique IDs" not in caplog.text

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
