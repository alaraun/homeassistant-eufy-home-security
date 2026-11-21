"""Device class and unit of a setting entity from the library's ``Setting.meaning``.

One table (``MEANING_TYPING``) types every setting entity: a duration in its unit, a
percentage, a temperature without a scale, a timestamp, a time of day, and the binary
statuses (door, tamper, battery, moisture, cold, sound). No setting key decides it.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Final

import pytest
from conftest import entity_id_for, seed_setting, set_up_warm, state_of
from eufy_home_security import EufySecurity
from eufy_home_security.devices import SettingMeaning, SettingUnit
from eufy_home_security.devices.model_settings import (
    BINARY_MEANINGS,
    Setting,
    bundled_codes,
    settings_of,
)
from eufy_home_security.testing import SYNTHETIC, FakeCloud, FakeStation, camera_device
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.number import NumberDeviceClass
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import ATTR_DEVICE_CLASS, ATTR_UNIT_OF_MEASUREMENT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant.common import MockConfigEntry

from custom_components.eufy_home_security import settings
from custom_components.eufy_home_security.settings import (
    MEANING_TYPING,
    EntityTyping,
    entity_typing,
    numeric_value,
    setting_platform,
    timestamp_value,
)

# Devices paired to the fake HomeBase, one per meaning they carry.
WATER_SN: Final = "T8920P0000000001"
GLASS_SN: Final = "T90G0P0000000001"
SIREN_SN: Final = "T90R0P0000000001"
DOORBELL_SN: Final = "T8214P0000000001"
INDOOR_SN: Final = "T8352P0000000001"
_PAIRED: Final = (
    (WATER_SN, 1, "T8920"),
    (GLASS_SN, 2, "T90G0"),
    (SIREN_SN, 3, "T90R0"),
    (DOORBELL_SN, 4, "T8214"),
    (INDOOR_SN, 5, "T8352"),
)
_BATTERY_PARAM: Final = 1101
# A device timestamp in milliseconds since the epoch, and the UTC datetime it stands for.
_STAMP_MS: Final = 1_700_000_000_123
_STAMP: Final = datetime(2023, 11, 14, 22, 13, 20, 123000, tzinfo=UTC)

_D = BinarySensorDeviceClass


# ── the table ────────────────────────────────────────────────────────────────


def test_every_library_meaning_has_a_typing_rule() -> None:
    """A meaning the library adds fails here until the table types it."""
    assert set(MEANING_TYPING) == set(SettingMeaning)


def test_binary_meanings_type_binary_sensors_only() -> None:
    for meaning, rule in MEANING_TYPING.items():
        binary = meaning in BINARY_MEANINGS
        assert (rule.binary_sensor is not None) is binary, meaning
        if binary:
            assert (rule.sensor, rule.number, rule.unit, rule.unit_from_setting) == (
                None,
                None,
                None,
                False,
            ), meaning


def test_a_meaning_the_table_does_not_list_keeps_the_setting_unit_and_no_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The documented default for a meaning newer than the table: untyped."""
    monkeypatch.setattr(settings, "MEANING_TYPING", {})
    assert entity_typing(_bundled("T8160", "video_clip_length")) == EntityTyping(unit="s")


def _bundled(code: str, key: str) -> Setting:
    return settings_of(code)[key]


@pytest.mark.parametrize(
    ("code", "key", "platform", "device_class", "unit"),
    [
        ("T8160", "video_clip_length", Platform.NUMBER, NumberDeviceClass.DURATION, "s"),
        ("T8352", "speaker_volume", Platform.NUMBER, None, "%"),
        ("T8352", "device_indoor_temperatur_alarm_max_value", Platform.NUMBER, None, None),
        (
            "T8214",
            "device_response_timeout_delay_time",
            Platform.SENSOR,
            SensorDeviceClass.DURATION,
            "s",
        ),
        (
            "T90G0",
            "sensor_grass_last_break_time",
            Platform.SENSOR,
            SensorDeviceClass.TIMESTAMP,
            None,
        ),
        ("T8214", "device_packages_uncollected_check_time", Platform.SENSOR, None, None),
        ("T8900", "sensor_door_status", Platform.BINARY_SENSOR, _D.DOOR, None),
        ("T90E0", "sensor_disassemble_status", Platform.BINARY_SENSOR, _D.TAMPER, None),
        ("T8900", "sensor_is_low_power", Platform.BINARY_SENSOR, _D.BATTERY, None),
        ("T8920", "sensor_water_status", Platform.BINARY_SENSOR, _D.MOISTURE, None),
        ("T8920", "sensor_temperature_low_status", Platform.BINARY_SENSOR, _D.COLD, None),
        ("T90R0", "sensor_siren_ring_status", Platform.BINARY_SENSOR, _D.SOUND, None),
        ("T8030", "hb_keypad_is_password_set", Platform.BINARY_SENSOR, None, None),
    ],
)
def test_bundled_settings_are_typed_by_their_meaning(
    code: str, key: str, platform: Platform, device_class: str | None, unit: str | None
) -> None:
    setting = _bundled(code, key)
    assert setting_platform(setting) is platform
    typing = entity_typing(setting)
    by_platform = {
        Platform.SENSOR: typing.sensor,
        Platform.NUMBER: typing.number,
        Platform.BINARY_SENSOR: typing.binary_sensor,
    }
    assert by_platform[platform] == device_class
    assert typing.unit == unit


def test_a_status_without_a_meaning_has_no_class() -> None:
    setting = _bundled("T8030", "hb_keypad_is_password_set")
    assert setting.meaning is None
    assert entity_typing(setting) == EntityTyping()


@pytest.mark.parametrize(
    ("unit", "expected"),
    [
        (
            SettingUnit.MILLISECONDS,
            EntityTyping(SensorDeviceClass.DURATION, NumberDeviceClass.DURATION, unit="ms"),
        ),
        (
            SettingUnit.DAYS,
            EntityTyping(SensorDeviceClass.DURATION, NumberDeviceClass.DURATION, unit="d"),
        ),
        (None, EntityTyping()),
        (SettingUnit.NONE, EntityTyping()),
    ],
)
def test_a_duration_takes_the_setting_unit_and_without_one_no_class(
    unit: SettingUnit | None, expected: EntityTyping
) -> None:
    setting = dataclasses.replace(_bundled("T8160", "video_clip_length"), unit=unit)
    assert setting.meaning is SettingMeaning.DURATION
    assert entity_typing(setting) == expected


def test_a_percentage_is_in_percent_whatever_the_setting_unit() -> None:
    setting = dataclasses.replace(_bundled("T8352", "speaker_volume"), unit=None)
    assert entity_typing(setting) == EntityTyping(unit="%")


def test_no_meaning_keeps_the_setting_unit_without_a_class() -> None:
    setting = dataclasses.replace(_bundled("T8160", "video_clip_length"), meaning=None)
    assert entity_typing(setting) == EntityTyping(unit="s")


def test_every_bundled_typed_entity_has_a_unit_its_class_accepts() -> None:
    """Home Assistant refuses a duration in a unit it cannot convert; the bundled data
    gives none."""
    seen: set[SettingMeaning] = set()
    for code in bundled_codes():
        for setting in settings_of(code).values():
            if setting_platform(setting) is None or setting.meaning is None:
                continue
            seen.add(setting.meaning)
            typing = entity_typing(setting)
            if typing.sensor is SensorDeviceClass.DURATION:
                assert typing.unit in {"s", "ms", "d"}, (code, setting.key)
    assert seen >= {SettingMeaning.DURATION, SettingMeaning.TIMESTAMP}


# ── values ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (str(_STAMP_MS), _STAMP),
        (f" {_STAMP_MS} ", _STAMP),
        (_STAMP_MS, _STAMP),
        (1, datetime(1970, 1, 1, 0, 0, 0, 1000, tzinfo=UTC)),
        ("0", None),
        (0, None),
        ("", None),
        ("-5", None),
        ("12:30", None),
        (True, None),
        (1.5, None),
        (None, None),
        (10**30, None),
    ],
)
def test_a_timestamp_is_an_aware_datetime_and_zero_or_empty_is_none(
    value: Any, expected: datetime | None
) -> None:
    result = timestamp_value(value)
    assert result == expected
    if result is not None:
        assert result.tzinfo is UTC


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (30, 30),
        (2.5, 2.5),
        ("30", 30),
        (" 2.5 ", 2.5),
        ("nan", None),
        (float("inf"), None),
        ("thirty", None),
        ("", None),
        (True, None),
        (None, None),
    ],
)
def test_a_value_with_a_unit_is_a_finite_number_or_none(value: Any, expected: Any) -> None:
    result = numeric_value(value)
    assert result == expected
    assert type(result) is type(expected)


# ── end to end ───────────────────────────────────────────────────────────────


def _pair(station: FakeStation, cloud: FakeCloud) -> None:
    """Pair one device per meaning; call before ``set_up_warm``."""
    for serial, channel, _model in _PAIRED:
        cloud.devices.append(
            camera_device(serial, station_sn=station.serial, channel=channel, name=serial[:5])
        )
        station.params[channel] = {_BATTERY_PARAM: "90"}


def _seed(station: FakeStation, serial: str, key: str, raw: int | str) -> None:
    channel, model = next((c, m) for s, c, m in _PAIRED if s == serial)
    seed_setting(station, key, raw, channel=channel, model=model)


async def _enable(
    hass: HomeAssistant, entry: MockConfigEntry, rows: list[tuple[str, str, str]]
) -> None:
    """Enable each (domain, serial, key) row, then reload once."""
    registry = er.async_get(hass)
    for domain, serial, key in rows:
        registry.async_update_entity(entity_id_for(hass, domain, serial, key), disabled_by=None)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()


async def _stop(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_binary_statuses_carry_their_meaning_class(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Moisture, cold, tamper and sound from the meaning; a status without one has none."""
    _pair(fake_station, fake_cloud)
    _seed(fake_station, WATER_SN, "sensor_water_status", 1)
    _seed(fake_station, SIREN_SN, "sensor_siren_ring_status", 1)
    entry = await set_up_warm(hass, seed_warm_cache)
    registry = er.async_get(hass)
    for serial, key, device_class in (
        (WATER_SN, "sensor_water_status", _D.MOISTURE),
        (WATER_SN, "sensor_temperature_low_status", _D.COLD),
        (GLASS_SN, "sensor_disassemble_status", _D.TAMPER),
        (SIREN_SN, "sensor_siren_ring_status", _D.SOUND),
        (SYNTHETIC.station_sn, "hb_keypad_is_password_set", None),
    ):
        entity_id = entity_id_for(hass, Platform.BINARY_SENSOR, serial, key)
        row = registry.async_get(entity_id)
        assert row is not None
        assert row.original_device_class == device_class, key
        state = hass.states.get(entity_id)
        assert state is not None, key
        assert state.attributes.get(ATTR_DEVICE_CLASS) == device_class, key
    assert (
        state_of(
            hass, entity_id_for(hass, Platform.BINARY_SENSOR, SIREN_SN, "sensor_siren_ring_status")
        )
        == "on"
    )
    await _stop(hass, entry)


@pytest.mark.parametrize(
    ("raw", "expected"),
    # Home Assistant writes a timestamp state in whole seconds.
    [(str(_STAMP_MS), "2023-11-14T22:13:20+00:00"), ("0", "unknown"), ("", "unknown")],
)
async def test_a_timestamp_sensor_shows_the_time_and_zero_as_unknown(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    raw: str,
    expected: str,
) -> None:
    key = "sensor_grass_last_break_time"
    _pair(fake_station, fake_cloud)
    _seed(fake_station, GLASS_SN, key, raw)
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = entity_id_for(hass, Platform.SENSOR, GLASS_SN, key)
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.attributes[ATTR_DEVICE_CLASS] == SensorDeviceClass.TIMESTAMP
    assert ATTR_UNIT_OF_MEASUREMENT not in state.attributes
    assert state.state == expected
    await _stop(hass, entry)


async def test_value_sensors_and_numbers_carry_their_meaning_class_and_unit(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A duration sensor in seconds, a time of day as text, a percent number and a
    temperature number without a unit; no state class on any."""
    _pair(fake_station, fake_cloud)
    _seed(fake_station, DOORBELL_SN, "device_response_timeout_delay_time", 30)
    _seed(fake_station, DOORBELL_SN, "device_packages_uncollected_check_time", "20:00")
    _seed(fake_station, INDOOR_SN, "speaker_volume", 40)
    _seed(fake_station, INDOOR_SN, "device_indoor_temperatur_alarm_max_value", 83)
    entry = await set_up_warm(hass, seed_warm_cache)
    await _enable(
        hass,
        entry,
        [
            (Platform.SENSOR, DOORBELL_SN, "device_response_timeout_delay_time"),
            (Platform.SENSOR, DOORBELL_SN, "device_packages_uncollected_check_time"),
        ],
    )
    for domain, serial, key, value, device_class, unit in (
        (
            Platform.SENSOR,
            DOORBELL_SN,
            "device_response_timeout_delay_time",
            "30",
            SensorDeviceClass.DURATION,
            "s",
        ),
        (
            Platform.SENSOR,
            DOORBELL_SN,
            "device_packages_uncollected_check_time",
            "20:00",
            None,
            None,
        ),
        (Platform.NUMBER, INDOOR_SN, "speaker_volume", "40", None, "%"),
        (Platform.NUMBER, INDOOR_SN, "device_indoor_temperatur_alarm_max_value", "83", None, None),
    ):
        state = hass.states.get(entity_id_for(hass, domain, serial, key))
        assert state is not None, key
        assert state.state == value, key
        assert state.attributes.get(ATTR_DEVICE_CLASS) == device_class, key
        assert state.attributes.get(ATTR_UNIT_OF_MEASUREMENT) == unit, key
        assert "state_class" not in state.attributes, key
    await _stop(hass, entry)


async def test_a_duration_sensor_whose_value_is_not_a_number_is_unknown(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    key = "device_response_timeout_delay_time"
    _pair(fake_station, fake_cloud)
    _seed(fake_station, DOORBELL_SN, key, "soon")
    entry = await set_up_warm(hass, seed_warm_cache)
    await _enable(hass, entry, [(Platform.SENSOR, DOORBELL_SN, key)])
    assert state_of(hass, entity_id_for(hass, Platform.SENSOR, DOORBELL_SN, key)) == "unknown"
    await _stop(hass, entry)
