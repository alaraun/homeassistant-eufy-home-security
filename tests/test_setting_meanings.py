"""Device class and unit of a setting entity from the library's ``Setting.meaning``.

One table (``MEANING_TYPING``) types every setting entity: a duration in its unit, a
percentage, a temperature without a scale, a timestamp, a time of day, and the binary
statuses (door, tamper, battery, moisture, cold, sound). No setting key decides it.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Final

import pytest
from conftest import (
    add_entry,
    entity_id_for,
    seed_setting,
    set_up_warm,
    setup_entry,
    state_of,
    wait_until,
)
from eufy_home_security import EufySecurity, Station, entity_unique_id
from eufy_home_security.devices import SettingKind, SettingMeaning, SettingUnit
from eufy_home_security.devices.model_settings import (
    BINARY_MEANINGS,
    Setting,
    bundled_codes,
    settings_of,
)
from eufy_home_security.testing import SYNTHETIC, FakeCloud, FakeStation, camera_device
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.number import DEVICE_CLASS_UNITS as NUMBER_UNITS
from homeassistant.components.number import NumberDeviceClass
from homeassistant.components.sensor import DEVICE_CLASS_UNITS as SENSOR_UNITS
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    ATTR_FRIENDLY_NAME,
    ATTR_UNIT_OF_MEASUREMENT,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant.common import MockConfigEntry, async_capture_events

from custom_components.eufy_home_security import settings
from custom_components.eufy_home_security.const import DOMAIN
from custom_components.eufy_home_security.settings import (
    MEANING_TYPING,
    EntityTyping,
    entity_typing,
    numeric_value,
    setting_platform,
    setting_specs,
    timestamp_value,
)

# Devices paired to the fake HomeBase, one per meaning they carry.
WATER_SN: Final = "T8920P0000000001"
GLASS_SN: Final = "T90G0P0000000001"
SIREN_SN: Final = "T90R0P0000000001"
DOORBELL_SN: Final = "T8214P0000000001"
INDOOR_SN: Final = "T8352P0000000001"
SMOKE_SN: Final = "T90S0P0000000001"
_PAIRED: Final = (
    (WATER_SN, 1, "T8920"),
    (GLASS_SN, 2, "T90G0"),
    (SIREN_SN, 3, "T90R0"),
    (DOORBELL_SN, 4, "T8214"),
    (INDOOR_SN, 5, "T8352"),
    (SMOKE_SN, 6, "T90S0"),
)
_BATTERY_PARAM: Final = 1101
_VOLUME: Final = "speaker_volume"
_TEMPERATURE: Final = "device_indoor_temperatur_alarm_max_value"
# A device timestamp in milliseconds since the epoch, and the UTC datetime it stands for.
_STAMP_MS: Final = 1_700_000_000_123
_STAMP: Final = datetime(2023, 11, 14, 22, 13, 20, 123000, tzinfo=UTC)

_D = BinarySensorDeviceClass
_EN_PATH: Final = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "eufy_home_security"
    / "translations"
    / "en.json"
)


# ── the table ────────────────────────────────────────────────────────────────


def test_every_library_meaning_has_a_typing_rule() -> None:
    """A meaning the library adds fails here until the table types it."""
    assert set(MEANING_TYPING) == set(SettingMeaning)


# Binary meanings Home Assistant has no binary-sensor device class for.
_CLASSLESS: Final = frozenset({SettingMeaning.MUTED, SettingMeaning.TESTING})


def test_binary_meanings_type_binary_sensors_only() -> None:
    """A binary meaning gives a binary-sensor class and nothing else; a silenced alarm and
    a running test give none."""
    for meaning, rule in MEANING_TYPING.items():
        if meaning not in BINARY_MEANINGS:
            assert rule.binary_sensor is None, meaning
            continue
        assert (rule.binary_sensor is None) is (meaning in _CLASSLESS), meaning
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


_SMOKE_STATUS: Final = "sensor_smoke_status"
# The smoke sensor status's binary sensors: meaning -> device class.
_SMOKE_CLASSES: Final = {
    SettingMeaning.SMOKE: _D.SMOKE,
    SettingMeaning.LOW_BATTERY: _D.BATTERY,
    SettingMeaning.END_OF_LIFE: _D.PROBLEM,
    SettingMeaning.TESTING: None,
    SettingMeaning.MUTED: None,
    SettingMeaning.PROBLEM: _D.PROBLEM,
}


@pytest.mark.parametrize("code", ["T90S0", "T8931"])
def test_a_smoke_sensor_status_is_a_binary_sensor_per_member_meaning(code: str) -> None:
    """A read-only flags status with member meanings: a binary sensor, typed per meaning."""
    setting = _bundled(code, _SMOKE_STATUS)
    assert setting.kind is SettingKind.FLAGS
    assert not setting.writable
    assert setting_platform(setting) is Platform.BINARY_SENSOR
    assert set(setting.flag_meanings.values()) == set(_SMOKE_CLASSES)
    for meaning, device_class in _SMOKE_CLASSES.items():
        assert entity_typing(setting, meaning).binary_sensor == device_class, meaning
    # Its own meaning is none: the members carry them.
    assert entity_typing(setting) == EntityTyping()

    # One spec per distinct meaning: two members each mean smoke and testing.
    assert len(setting.flag_meanings) > len(_SMOKE_CLASSES)
    station = SimpleNamespace(
        sub_devices=[SimpleNamespace(device_sn=SMOKE_SN)],
        settings_for=lambda device_sn: list(settings_of(code).values()) if device_sn else [],
        lights_for=lambda device_sn: (),
    )
    specs = setting_specs(station)  # type: ignore[arg-type]
    keys = [spec.key for spec in specs]
    assert len(keys) == len(set(keys))
    smoke = [spec for spec in specs if spec.setting.key == _SMOKE_STATUS]
    assert [spec.meaning for spec in smoke] == list(dict.fromkeys(setting.flag_meanings.values()))
    assert len(smoke) == len(_SMOKE_CLASSES) == 6


def test_a_flags_status_without_member_meanings_stays_a_sensor() -> None:
    setting = dataclasses.replace(_bundled("T90S0", _SMOKE_STATUS), flag_meanings={})
    assert setting_platform(setting) is Platform.SENSOR


def test_every_bundled_member_meaning_has_a_name() -> None:
    """A member meaning names its binary sensor through a translation key."""
    names = json.loads(_EN_PATH.read_text())["entity"]["binary_sensor"]
    used = {
        meaning
        for code in bundled_codes()
        for setting in settings_of(code).values()
        for meaning in setting.flag_meanings.values()
    }
    assert used
    missing = sorted(m.value for m in used if f"meaning_{m.value}" not in names)
    assert not missing, missing


@pytest.mark.parametrize(
    ("code", "key", "platform", "device_class", "unit"),
    [
        ("T8160", "video_clip_length", Platform.NUMBER, NumberDeviceClass.DURATION, "s"),
        ("T8352", "speaker_volume", Platform.NUMBER, None, "%"),
        ("T8352", "device_indoor_temperatur_alarm_max_value", Platform.NUMBER, None, None),
        ("T8531", "video_clip_length", Platform.SENSOR, SensorDeviceClass.DURATION, "s"),
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
    """Home Assistant refuses a value in a unit its device class does not list; the bundled
    data gives none, on a sensor or a number."""
    seen: set[SettingMeaning] = set()
    for code in bundled_codes():
        for setting in settings_of(code).values():
            if setting_platform(setting) is None or setting.meaning is None:
                continue
            seen.add(setting.meaning)
            typing = entity_typing(setting)
            if typing.sensor in SENSOR_UNITS:
                assert typing.unit in SENSOR_UNITS[typing.sensor], (code, setting.key)
            if typing.number in NUMBER_UNITS:
                assert typing.unit in NUMBER_UNITS[typing.number], (code, setting.key)
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


def _replace_settings(monkeypatch: pytest.MonkeyPatch, changes: dict[str, dict[str, Any]]) -> None:
    """Serve each setting named in ``changes`` with those fields replaced, on every device."""
    real = Station.settings_for

    def settings_for(self: Station, device_sn: str | None = None) -> tuple[Setting, ...]:
        return tuple(
            dataclasses.replace(s, **changes[s.key]) if s.key in changes else s
            for s in real(self, device_sn)
        )

    monkeypatch.setattr(Station, "settings_for", settings_for)


# The doorbell's response timeout as a read-only value the library passes through as
# text (kind other, no control): a duration sensor whose state the glue converts.
_TIMEOUT: Final = "device_response_timeout_delay_time"
_AS_TEXT: Final[dict[str, Any]] = {
    "kind": SettingKind.OTHER,
    "control": None,
    "values": (),
    "labels": {},
    "default": None,
    "writable": False,
}


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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A duration sensor in seconds, a time of day as text, a percent number and a
    temperature number without a unit; no state class on any."""
    _replace_settings(monkeypatch, {_TIMEOUT: _AS_TEXT})
    _pair(fake_station, fake_cloud)
    _seed(fake_station, DOORBELL_SN, "device_response_timeout_delay_time", 15)
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
            "15",
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


@pytest.mark.parametrize(("raw", "expected"), [("30", "30"), ("soon", "unknown")])
async def test_a_duration_sensor_whose_value_is_not_a_number_is_unknown(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
    expected: str,
) -> None:
    """A text value the library passes through, typed as a duration in seconds."""
    key = "device_packages_uncollected_check_time"
    _replace_settings(
        monkeypatch, {key: {"meaning": SettingMeaning.DURATION, "unit": SettingUnit.SECONDS}}
    )
    _pair(fake_station, fake_cloud)
    _seed(fake_station, DOORBELL_SN, key, raw)
    entry = await set_up_warm(hass, seed_warm_cache)
    await _enable(hass, entry, [(Platform.SENSOR, DOORBELL_SN, key)])
    state = hass.states.get(entity_id_for(hass, Platform.SENSOR, DOORBELL_SN, key))
    assert state is not None
    assert state.attributes.get(ATTR_DEVICE_CLASS) == SensorDeviceClass.DURATION
    assert state.state == expected
    await _stop(hass, entry)


@pytest.mark.parametrize("platform", [Platform.NUMBER, Platform.SENSOR])
async def test_the_entity_unit_is_the_table_s_not_the_setting_s(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    platform: Platform,
) -> None:
    """A percentage whose setting has no unit is in %; a temperature whose setting has one
    has none. On a number, and on a sensor when the setting is read-only."""
    writable = platform is Platform.NUMBER
    _replace_settings(
        monkeypatch,
        {
            _VOLUME: {"unit": None, "writable": writable},
            _TEMPERATURE: {"unit": SettingUnit.SECONDS, "writable": writable},
        },
    )
    _pair(fake_station, fake_cloud)
    _seed(fake_station, INDOOR_SN, _VOLUME, 40)
    _seed(fake_station, INDOOR_SN, _TEMPERATURE, 83)
    entry = await set_up_warm(hass, seed_warm_cache)
    if platform is Platform.SENSOR:
        await _enable(
            hass, entry, [(platform, INDOOR_SN, _VOLUME), (platform, INDOOR_SN, _TEMPERATURE)]
        )
    for key, value, unit in ((_VOLUME, "40", "%"), (_TEMPERATURE, "83", None)):
        state = hass.states.get(entity_id_for(hass, platform, INDOOR_SN, key))
        assert state is not None, key
        assert state.state == value, key
        assert state.attributes.get(ATTR_UNIT_OF_MEASUREMENT) == unit, key
        assert state.attributes.get(ATTR_DEVICE_CLASS) is None, key
    await _stop(hass, entry)


@pytest.mark.parametrize(("raw", "expected"), [("40", "40"), ("abc", "unknown")])
async def test_a_read_only_percentage_without_a_class_is_a_number_or_unknown(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
    expected: str,
) -> None:
    """A unit makes the state a number even with no device class. The stand-in is a
    read-only setting the library passes through as text, typed as a percentage."""
    key = "device_packages_uncollected_check_time"
    _replace_settings(monkeypatch, {key: {"meaning": SettingMeaning.PERCENT, "unit": None}})
    _pair(fake_station, fake_cloud)
    _seed(fake_station, DOORBELL_SN, key, raw)
    entry = await set_up_warm(hass, seed_warm_cache)
    await _enable(hass, entry, [(Platform.SENSOR, DOORBELL_SN, key)])
    state = hass.states.get(entity_id_for(hass, Platform.SENSOR, DOORBELL_SN, key))
    assert state is not None
    assert state.attributes.get(ATTR_DEVICE_CLASS) is None
    assert state.attributes.get(ATTR_UNIT_OF_MEASUREMENT) == "%"
    assert state.state == expected
    await _stop(hass, entry)


# Smoke sensor status masks (members alarm 1, localTesting 2, remotingTesting 4, battery 8,
# trouble 16, muting 32, endoflife 64, highAlarm 128) and the meanings each sets.
_SMOKE_MASKS: Final[tuple[tuple[int, set[SettingMeaning]], ...]] = (
    (0, set()),
    (1, {SettingMeaning.SMOKE}),
    (128, {SettingMeaning.SMOKE}),
    (2, {SettingMeaning.TESTING}),
    (4 | 32, {SettingMeaning.TESTING, SettingMeaning.MUTED}),
    (8 | 16 | 64, {SettingMeaning.LOW_BATTERY, SettingMeaning.PROBLEM, SettingMeaning.END_OF_LIFE}),
    (129, {SettingMeaning.SMOKE}),
)


async def test_a_smoke_sensor_shows_a_binary_sensor_per_meaning_of_its_status(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Smoke Sensor E10: one enabled binary sensor per member meaning, on the device card,
    named by the meaning; unknown until a mask is reported, then each follows the dumps.
    No sensor shows the raw mask."""
    _pair(fake_station, fake_cloud)
    entry = await set_up_warm(hass, seed_warm_cache)
    registry = er.async_get(hass)
    assert (
        registry.async_get_entity_id(
            Platform.SENSOR, DOMAIN, entity_unique_id(SMOKE_SN, _SMOKE_STATUS)
        )
        is None
    )
    entity_ids: dict[SettingMeaning, str] = {}
    for meaning, device_class in _SMOKE_CLASSES.items():
        key = f"{_SMOKE_STATUS}_{meaning.value}"
        entity_id = entity_id_for(hass, Platform.BINARY_SENSOR, SMOKE_SN, key)
        row = registry.async_get(entity_id)
        assert row is not None
        assert (row.disabled_by, row.entity_category) == (None, None), meaning
        assert row.original_device_class == device_class, meaning
        assert row.translation_key == f"meaning_{meaning.value}", meaning
        assert state_of(hass, entity_id) == "unknown", meaning
        entity_ids[meaning] = entity_id
    smoke = hass.states.get(entity_ids[SettingMeaning.SMOKE])
    assert smoke is not None
    assert smoke.attributes[ATTR_FRIENDLY_NAME].endswith(" Smoke")

    for mask, on in _SMOKE_MASKS:
        _seed(fake_station, SMOKE_SN, _SMOKE_STATUS, mask)
        fake_station.send_param_dump()
        expected = {m: "on" if m in on else "off" for m in entity_ids}
        await wait_until(
            lambda expected=expected: (
                {m: state_of(hass, entity_id) for m, entity_id in entity_ids.items()} == expected
            )
        )
    await _stop(hass, entry)


async def test_a_meaning_the_library_no_longer_gives_loses_its_binary_sensor(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A meaning binary sensor of a status still listed, for a meaning the library no
    longer gives, is forgotten at setup; a meaning it gives keeps its row, as do rows
    under another setting's key and on another platform."""
    _pair(fake_station, fake_cloud)
    seed_warm_cache()
    entry = add_entry(hass)
    registry = er.async_get(hass)

    def seed(domain: str, key: str) -> er.RegistryEntry:
        # Named like a meaning row: by a translation key, no name.
        return registry.async_get_or_create(
            domain,
            DOMAIN,
            entity_unique_id(SMOKE_SN, key),
            config_entry=entry,
            translation_key=f"meaning_{key.rsplit('_', 1)[-1]}",
        )

    orphan = seed(Platform.BINARY_SENSOR, f"{_SMOKE_STATUS}_gas")
    live = seed(Platform.BINARY_SENSOR, f"{_SMOKE_STATUS}_{SettingMeaning.SMOKE.value}")
    other_key = seed(Platform.BINARY_SENSOR, "sensor_alarm_timeout_gas")
    other_platform = seed(Platform.SENSOR, f"{_SMOKE_STATUS}_gas")
    removed = async_capture_events(hass, er.EVENT_ENTITY_REGISTRY_UPDATED)

    assert await setup_entry(hass, entry)

    gone = {event.data["entity_id"] for event in removed if event.data["action"] == "remove"}
    assert gone == {orphan.entity_id}
    assert registry.async_get(orphan.entity_id) is None
    for kept in (live, other_key, other_platform):
        assert registry.async_get(kept.entity_id) is not None, kept.unique_id
    assert state_of(hass, live.entity_id) == "unknown"
    await _stop(hass, entry)
