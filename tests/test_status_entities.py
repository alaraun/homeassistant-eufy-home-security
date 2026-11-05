"""Setting entities by role, shown and readable: statuses on, diagnostics none.

An entry sensor's door, tamper and low-battery statuses are enabled binary sensors on
the device card; a status of a settings page is an enabled diagnostic entity; a
structured or JSON value is never a state.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable
from typing import Any, Final

import attr
import pytest
from conftest import add_entry, entity_id_for, seed_setting, setup_entry, state_of
from eufy_home_security import (
    EufySecurity,
    SettingRole,
    SettingShown,
    Station,
    entity_unique_id,
)
from eufy_home_security.devices import SettingControl, SettingKind
from eufy_home_security.devices.model_settings import Setting, bundled_codes, settings_of
from eufy_home_security.testing import SYNTHETIC, FakeCloud, FakeStation, camera_device
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.const import EntityCategory, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant.common import MockConfigEntry

from custom_components.eufy_home_security.const import ATTR_SECTION, ATTR_VALUE, DOMAIN
from custom_components.eufy_home_security.sensor import EufySettingSensor
from custom_components.eufy_home_security.settings import (
    MAX_STATE_LENGTH,
    STATUS_DEVICE_CLASSES,
    SettingEntitySpec,
    capped_state,
    is_setting_row,
    json_value,
    setting_platform,
    setting_specs,
)

# An entry sensor with door and low-battery statuses, and one with door and tamper.
DOOR_SN: Final = "T8900P0000000001"
TAMPER_SN: Final = "T90E0P0000000001"
_DOOR_CHANNEL: Final = 2
_TAMPER_CHANNEL: Final = 3
_STATION_CHANNEL: Final = 255
_DOOR_PARAM: Final = 1550
_LOW_POWER_PARAM: Final = 1552
_TAMPER_PARAM: Final = 61015
_BATTERY_PARAM: Final = 1101
# A string of the HomeBase the app offers as a control (read-only here) whose value is
# JSON (``Setting.structured``): a disabled diagnostic sensor.
_JSON_SETTING: Final = "arming_schedule_config"
# HomeBase statuses of an earlier library build that are diagnostic values now: bare
# codes on the device card and a structured value on a page.
_DIAGNOSTIC_NOW: Final = (
    ("setting_current_connection_type", None),
    ("device_wifi_signal_strength_level", None),
    ("arming_get_alarm_home_mode", EntityCategory.DIAGNOSTIC),
)


def _add_entry_sensors(station: FakeStation, cloud: FakeCloud) -> None:
    """Pair both entry sensors; call before ``seed_warm_cache``."""
    cloud.devices.append(
        camera_device(DOOR_SN, station_sn=station.serial, channel=_DOOR_CHANNEL, name="Door")
    )
    cloud.devices.append(
        camera_device(TAMPER_SN, station_sn=station.serial, channel=_TAMPER_CHANNEL, name="Window")
    )
    # A battery reading, so each sensor's block is in the dump without its statuses.
    station.params[_DOOR_CHANNEL] = {_BATTERY_PARAM: "90"}
    station.params[_TAMPER_CHANNEL] = {_BATTERY_PARAM: "90"}


async def _stop(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


# ── the rule ─────────────────────────────────────────────────────────────────


def _door() -> Setting:
    return settings_of("T8900")["sensor_door_status"]


def _switch() -> Setting:
    return next(
        s
        for s in settings_of("T8160").values()
        if s.writable and s.control is SettingControl.SWITCH
    )


_CARD = SettingShown.CARD
_PAGE = SettingShown.PAGE
_DIAG = EntityCategory.DIAGNOSTIC


@pytest.mark.parametrize(
    ("role", "shown", "readable", "platform", "category", "enabled"),
    [
        (SettingRole.STATUS, _CARD, True, Platform.BINARY_SENSOR, None, True),
        (SettingRole.STATUS, _PAGE, True, Platform.BINARY_SENSOR, _DIAG, True),
        (SettingRole.STATUS, _CARD, False, None, None, None),
        (SettingRole.STATUS, _PAGE, False, None, None, None),
        (SettingRole.SETTING, _CARD, True, Platform.BINARY_SENSOR, _DIAG, False),
        (SettingRole.SETTING, _PAGE, True, Platform.BINARY_SENSOR, _DIAG, False),
        (SettingRole.SETTING, None, True, Platform.BINARY_SENSOR, _DIAG, False),
        (SettingRole.SETTING, _CARD, False, None, None, None),
        (SettingRole.DIAGNOSTIC, _CARD, True, None, None, None),
        (SettingRole.DIAGNOSTIC, _PAGE, True, None, None, None),
        (SettingRole.DIAGNOSTIC, None, True, None, None, None),
        (SettingRole.DIAGNOSTIC, None, False, None, None, None),
    ],
)
def test_a_read_only_setting_maps_by_role_shown_and_readable(
    role: SettingRole,
    shown: SettingShown | None,
    readable: bool,
    platform: Platform | None,
    category: EntityCategory | None,
    enabled: bool | None,
) -> None:
    """Status on the card: on, no category; status on a page: on, diagnostic; a setting
    the library cannot write: off, diagnostic; a diagnostic value or unreadable: none."""
    setting = dataclasses.replace(_door(), role=role, shown=shown, readable=readable)
    assert setting_platform(setting) is platform
    if platform is None:
        return
    spec = SettingEntitySpec(platform=platform, setting=setting, device_sn=None)
    assert spec.entity_category is category
    assert spec.enabled_by_default is enabled


@pytest.mark.parametrize("role", list(SettingRole))
@pytest.mark.parametrize("shown", [_CARD, _PAGE, None])
def test_a_writable_setting_is_its_control_whatever_its_role(
    role: SettingRole, shown: SettingShown | None
) -> None:
    """Config category and on, from ``control``; role and shown do not move a control."""
    setting = dataclasses.replace(_switch(), role=role, shown=shown)
    spec = SettingEntitySpec(platform=Platform.SWITCH, setting=setting, device_sn=None)
    assert setting_platform(setting) is Platform.SWITCH
    assert spec.entity_category is EntityCategory.CONFIG
    assert spec.enabled_by_default is True


def test_a_string_status_on_the_card_is_an_enabled_sensor() -> None:
    setting = dataclasses.replace(
        _door(), kind=SettingKind.STRING, role=SettingRole.STATUS, shown=_CARD
    )
    spec = SettingEntitySpec(platform=Platform.SENSOR, setting=setting, device_sn=None)
    assert setting_platform(setting) is Platform.SENSOR
    assert spec.entity_category is None
    assert spec.enabled_by_default is True


@pytest.mark.parametrize(
    ("advanced", "variant", "in_use", "enabled"),
    [
        (True, False, None, False),
        (False, True, None, False),
        (False, True, True, True),
        (False, False, False, False),
    ],
)
def test_a_status_behind_a_warning_or_not_in_use_starts_off(
    advanced: bool, variant: bool, in_use: bool | None, enabled: bool
) -> None:
    setting = dataclasses.replace(_door(), advanced=advanced)
    spec = SettingEntitySpec(
        platform=Platform.BINARY_SENSOR,
        setting=setting,
        device_sn=None,
        variant=variant,
        in_use=in_use,
    )
    assert spec.enabled_by_default is enabled


def test_the_device_class_table_covers_the_bundled_bool_card_statuses() -> None:
    """Every readable bool STATUS + CARD key of the bundled data has a class, except a
    siren's ring state, which no binary-sensor class describes."""
    keys = {
        s.key
        for code in bundled_codes()
        for s in settings_of(code).values()
        if s.readable
        and not s.writable
        and s.kind is SettingKind.BOOL
        and s.role is SettingRole.STATUS
        and s.shown is SettingShown.CARD
    }
    assert keys == {*STATUS_DEVICE_CLASSES, "sensor_siren_ring_status"}
    assert STATUS_DEVICE_CLASSES == {
        "sensor_door_status": BinarySensorDeviceClass.DOOR,
        "sensor_disassemble_status": BinarySensorDeviceClass.TAMPER,
        "sensor_is_low_power": BinarySensorDeviceClass.BATTERY,
        "sensor_water_status": BinarySensorDeviceClass.MOISTURE,
        "sensor_temperature_low_status": BinarySensorDeviceClass.COLD,
    }


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"a": 1}, {"a": 1}),
        ([1, 2], [1, 2]),
        ('{"mode": 1}', {"mode": 1}),
        (' [1, "x"]', [1, "x"]),
        ("{not json", None),
        ('"text"', None),
        ("12", None),
        ("plain", None),
        (12, None),
        (True, None),
        (None, None),
    ],
)
def test_json_value_finds_objects_and_arrays_only(value: Any, expected: Any) -> None:
    assert json_value(value) == expected


@pytest.mark.parametrize("length", [0, MAX_STATE_LENGTH - 1, MAX_STATE_LENGTH])
def test_a_state_within_the_limit_is_kept_whole(length: int) -> None:
    assert capped_state("x" * length) == "x" * length


@pytest.mark.parametrize("length", [MAX_STATE_LENGTH + 1, 1000])
def test_a_longer_state_is_cut_to_the_limit_with_an_ellipsis(length: int) -> None:
    capped = capped_state("x" * length)
    assert len(capped) == MAX_STATE_LENGTH
    assert capped == "x" * (MAX_STATE_LENGTH - 1) + "\u2026"


def test_a_json_value_is_kept_out_of_the_recorder() -> None:
    assert {ATTR_VALUE, ATTR_SECTION} <= EufySettingSensor._unrecorded_attributes


# ── end to end ───────────────────────────────────────────────────────────────


async def test_entry_sensors_show_door_tamper_and_low_battery_on_the_device_card(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Enabled binary sensors without a category, with their device class; open is on."""
    _add_entry_sensors(fake_station, fake_cloud)
    fake_station.params[_DOOR_CHANNEL] = {_DOOR_PARAM: "1", _LOW_POWER_PARAM: "0"}
    fake_station.params[_TAMPER_CHANNEL] = {_DOOR_PARAM: "0", _TAMPER_PARAM: "1"}
    seed_warm_cache()
    entry = add_entry(hass)
    assert await setup_entry(hass, entry)
    registry = er.async_get(hass)

    for serial, key, device_class, state in (
        (DOOR_SN, "sensor_door_status", BinarySensorDeviceClass.DOOR, "on"),
        (DOOR_SN, "sensor_is_low_power", BinarySensorDeviceClass.BATTERY, "off"),
        (TAMPER_SN, "sensor_door_status", BinarySensorDeviceClass.DOOR, "off"),
        (TAMPER_SN, "sensor_disassemble_status", BinarySensorDeviceClass.TAMPER, "on"),
    ):
        entity_id = entity_id_for(hass, Platform.BINARY_SENSOR, serial, key)
        row = registry.async_get(entity_id)
        assert row is not None
        assert row.disabled_by is None, key
        assert row.entity_category is None, key
        assert row.original_device_class == device_class, key
        assert state_of(hass, entity_id) == state, (serial, key)
        state_obj = hass.states.get(entity_id)
        assert state_obj is not None
        assert state_obj.attributes.get("device_class") == device_class
    await _stop(hass, entry)


async def test_an_unreported_status_is_unknown_not_its_default(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The door's default is closed (False); never reported, it shows unknown."""
    assert settings_of("T8900")["sensor_door_status"].default is False
    _add_entry_sensors(fake_station, fake_cloud)
    seed_warm_cache()
    entry = add_entry(hass)
    assert await setup_entry(hass, entry)

    entity_id = entity_id_for(hass, Platform.BINARY_SENSOR, DOOR_SN, "sensor_door_status")
    assert state_of(hass, entity_id) == "unknown"
    await _stop(hass, entry)


async def test_diagnostic_values_get_no_entity(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A readable diagnostic value (firmware update in progress, capability data) builds
    no row; the entry sensor's statuses do."""
    _add_entry_sensors(fake_station, fake_cloud)
    seed_warm_cache()
    entry = add_entry(hass)
    assert await setup_entry(hass, entry)
    station = entry.runtime_data.coordinators[SYNTHETIC.station_sn].station
    diagnostic = [
        s for s in station.settings_for(DOOR_SN) if s.readable and s.role is SettingRole.DIAGNOSTIC
    ]
    assert {s.key for s in diagnostic} >= {"is_device_upgrade", "device_ability_data"}
    registry = er.async_get(hass)
    for setting in diagnostic:
        unique_id = entity_unique_id(DOOR_SN, setting.key)
        for domain in (Platform.SENSOR, Platform.BINARY_SENSOR):
            assert registry.async_get_entity_id(domain, DOMAIN, unique_id) is None, setting.key
    keys = {spec.key for spec in setting_specs(station) if spec.device_sn == DOOR_SN}
    assert keys == {"sensor_door_status", "sensor_is_low_power"}
    await _stop(hass, entry)


async def _enabled_station_sensor(
    hass: HomeAssistant, fake_station: FakeStation, key: str, raw: str
) -> tuple[MockConfigEntry, Setting, str]:
    """Set up with ``raw`` as the HomeBase's ``key``, its sensor row enabled."""
    seed_setting(fake_station, key, raw, channel=_STATION_CHANNEL)
    entry = add_entry(hass)
    assert await setup_entry(hass, entry)
    station = entry.runtime_data.coordinators[SYNTHETIC.station_sn].station
    setting = next(s for s in station.settings_for() if s.key == key)
    entity_id = entity_id_for(hass, Platform.SENSOR, SYNTHETIC.station_sn, key)
    er.async_get(hass).async_update_entity(entity_id, disabled_by=None)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    return entry, setting, entity_id


_DOCUMENT: Final = {"mode": 1, "devices": [{"channel": 0, "action": 3}]}


@pytest.mark.parametrize(
    ("key", "raw", "expected"),
    [
        ("ai_empower", "1", "1"),
        ("ai_empower", '[{"ch": 0, "on": 1}]', [{"ch": 0, "on": 1}]),
        (_JSON_SETTING, json.dumps(_DOCUMENT), _DOCUMENT),
        (_JSON_SETTING, "y" * 400, "y" * 400),
    ],
)
async def test_a_structured_value_is_never_the_state(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    key: str,
    raw: str,
    expected: Any,
) -> None:
    """A structured setting's value: state unknown, ``value`` the decoded JSON, or the
    raw value when it does not parse."""
    seed_warm_cache()
    entry, setting, entity_id = await _enabled_station_sensor(hass, fake_station, key, raw)
    assert setting.structured
    assert not setting.writable

    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "unknown"
    assert state.attributes[ATTR_VALUE] == expected
    await _stop(hass, entry)


async def test_a_json_text_of_another_setting_is_an_attribute_and_a_long_text_is_cut(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A setting not flagged structured: a JSON text is an attribute, state unknown; a
    text longer than the limit is cut to it."""
    key = "notification_alarm_delay_value"
    seed_warm_cache()
    entry, setting, entity_id = await _enabled_station_sensor(
        hass, fake_station, key, json.dumps(_DOCUMENT)
    )
    assert not setting.structured
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "unknown"
    assert state.attributes[ATTR_VALUE] == _DOCUMENT
    await _stop(hass, entry)

    seed_setting(fake_station, key, "y" * 400, channel=_STATION_CHANNEL)
    assert await setup_entry(hass, entry)
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "y" * (MAX_STATE_LENGTH - 1) + "\u2026"
    assert ATTR_VALUE not in state.attributes
    await _stop(hass, entry)


# ── existing installs ────────────────────────────────────────────────────────


def _status_row(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    serial: str,
    key: str,
    disabled_by: er.RegistryEntryDisabler | None,
) -> er.RegistryEntry:
    """A status row as an earlier release registered it: a disabled diagnostic one."""
    return er.async_get(hass).async_get_or_create(
        Platform.BINARY_SENSOR,
        DOMAIN,
        entity_unique_id(serial, key),
        config_entry=entry,
        entity_category=EntityCategory.DIAGNOSTIC,
        original_name=key,
        disabled_by=disabled_by,
    )


def _disabled_by(hass: HomeAssistant, row: er.RegistryEntry) -> er.RegistryEntryDisabler | None:
    current = er.async_get(hass).async_get(row.entity_id)
    assert current is not None
    return current.disabled_by


async def test_setup_enables_status_rows_this_integration_disabled_only(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Disabled by the integration: on after setup. Disabled by the user: stays off.
    Another entry's row is not touched."""
    _add_entry_sensors(fake_station, fake_cloud)
    seed_warm_cache()
    entry = add_entry(hass)
    other = add_entry(hass, email="other@example.com", unique_id="other@example.com")
    integration = er.RegistryEntryDisabler.INTEGRATION
    user = er.RegistryEntryDisabler.USER
    door = _status_row(hass, entry, DOOR_SN, "sensor_door_status", integration)
    battery = _status_row(hass, entry, DOOR_SN, "sensor_is_low_power", user)
    foreign = _status_row(hass, other, TAMPER_SN, "sensor_disassemble_status", integration)
    keypad = _status_row(
        hass, entry, SYNTHETIC.station_sn, "hb_keypad_is_password_set", integration
    )

    assert await setup_entry(hass, entry)

    assert _disabled_by(hass, door) is None
    assert _disabled_by(hass, battery) is user
    assert _disabled_by(hass, foreign) is integration
    # A status of the keypad's settings page: on too, in the diagnostic section.
    assert _disabled_by(hass, keypad) is None
    await _stop(hass, entry)


async def test_setup_leaves_a_status_off_by_default_and_controls_alone(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A status the app puts behind a warning stays off; a control's row is not this
    pass's to move."""
    real = Station.settings_for

    def settings_for(self: Station, device_sn: str | None = None) -> tuple[Setting, ...]:
        return tuple(
            dataclasses.replace(s, advanced=True) if s.key == "sensor_door_status" else s
            for s in real(self, device_sn)
        )

    monkeypatch.setattr(Station, "settings_for", settings_for)
    _add_entry_sensors(fake_station, fake_cloud)
    seed_warm_cache()
    entry = add_entry(hass)
    integration = er.RegistryEntryDisabler.INTEGRATION
    door = _status_row(hass, entry, DOOR_SN, "sensor_door_status", integration)
    battery = _status_row(hass, entry, DOOR_SN, "sensor_is_low_power", integration)
    switch = _switch()
    control = er.async_get(hass).async_get_or_create(
        Platform.SWITCH,
        DOMAIN,
        entity_unique_id(SYNTHETIC.camera_sn, switch.key),
        config_entry=entry,
        entity_category=EntityCategory.CONFIG,
        original_name=switch.name,
        disabled_by=integration,
    )

    assert await setup_entry(hass, entry)

    assert _disabled_by(hass, door) is integration
    assert _disabled_by(hass, battery) is None
    assert _disabled_by(hass, control) is integration
    await _stop(hass, entry)


async def test_status_rows_stay_off_under_disable_new_entities(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The entry's preference registers every row disabled by the integration: none moves."""
    _add_entry_sensors(fake_station, fake_cloud)
    seed_warm_cache()
    entry = add_entry(hass)
    hass.config_entries.async_update_entry(entry, pref_disable_new_entities=True)
    door = _status_row(
        hass, entry, DOOR_SN, "sensor_door_status", er.RegistryEntryDisabler.INTEGRATION
    )

    assert await setup_entry(hass, entry)

    assert _disabled_by(hass, door) is er.RegistryEntryDisabler.INTEGRATION
    await _stop(hass, entry)


async def test_status_rows_of_a_disabled_device_stay_off(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    _add_entry_sensors(fake_station, fake_cloud)
    seed_warm_cache()
    entry = add_entry(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, DOOR_SN)},
        disabled_by=dr.DeviceEntryDisabler.USER,
    )
    registry = er.async_get(hass)
    door = _status_row(
        hass, entry, DOOR_SN, "sensor_door_status", er.RegistryEntryDisabler.INTEGRATION
    )
    registry.async_update_entity(door.entity_id, device_id=device.id)

    assert await setup_entry(hass, entry)

    assert _disabled_by(hass, door) is er.RegistryEntryDisabler.INTEGRATION
    await _stop(hass, entry)


@pytest.mark.parametrize(
    ("serial", "key", "category", "device_class"),
    [
        # A key that is a diagnostic value: no row is built for it.
        (DOOR_SN, "is_device_upgrade", None, None),
        # A key the device does not have.
        (DOOR_SN, "sensor_disassemble_status", None, BinarySensorDeviceClass.TAMPER),
        (TAMPER_SN, "sensor_is_low_power", None, BinarySensorDeviceClass.BATTERY),
    ],
)
async def test_a_status_row_nothing_builds_is_forgotten(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    serial: str,
    key: str,
    category: EntityCategory | None,
    device_class: BinarySensorDeviceClass | None,
) -> None:
    """An enabled status row (no category, a status device class or none) of a served
    device is removed like any setting row; the built status row stays."""
    _add_entry_sensors(fake_station, fake_cloud)
    seed_warm_cache()
    entry = add_entry(hass)
    registry = er.async_get(hass)
    stale = registry.async_get_or_create(
        Platform.BINARY_SENSOR,
        DOMAIN,
        entity_unique_id(serial, key),
        config_entry=entry,
        entity_category=category,
        original_device_class=device_class,
        original_name=key,
    )
    assert is_setting_row(stale)

    assert await setup_entry(hass, entry)

    assert registry.async_get(stale.entity_id) is None
    assert registry.async_get_entity_id(
        Platform.BINARY_SENSOR, DOMAIN, entity_unique_id(serial, "sensor_door_status")
    )
    await _stop(hass, entry)


@pytest.mark.parametrize(("key", "category"), _DIAGNOSTIC_NOW)
async def test_an_enabled_status_row_whose_setting_is_diagnostic_now_is_forgotten(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    key: str,
    category: EntityCategory | None,
) -> None:
    """A HomeBase status sensor an earlier build created enabled is removed once the
    library makes its setting a diagnostic value; the station still lists the setting.
    A status still built (the keypad's password state) keeps its row."""
    seed_warm_cache()
    entry = add_entry(hass)
    registry = er.async_get(hass)
    stale = registry.async_get_or_create(
        Platform.SENSOR,
        DOMAIN,
        entity_unique_id(SYNTHETIC.station_sn, key),
        config_entry=entry,
        entity_category=category,
        original_name=key,
    )
    assert stale.disabled_by is None
    assert is_setting_row(stale)

    assert await setup_entry(hass, entry)

    station = entry.runtime_data.coordinators[SYNTHETIC.station_sn].station
    assert station.has_setting(key) is True
    setting = next(s for s in station.settings_for() if s.key == key)
    assert setting.readable
    assert setting.role is SettingRole.DIAGNOSTIC
    assert registry.async_get(stale.entity_id) is None
    assert registry.async_get_entity_id(
        Platform.BINARY_SENSOR,
        DOMAIN,
        entity_unique_id(SYNTHETIC.station_sn, "hb_keypad_is_password_set"),
    )
    await _stop(hass, entry)


async def test_a_row_with_a_translation_key_or_another_class_is_not_a_setting_row(
    hass: HomeAssistant,
) -> None:
    """The integration's own entities (translated names, motion class) are never matched."""
    base = er.async_get(hass).async_get_or_create(
        Platform.BINARY_SENSOR, DOMAIN, entity_unique_id(DOOR_SN, "x"), original_name="X"
    )
    assert is_setting_row(base)
    assert not is_setting_row(attr.evolve(base, translation_key="motion_detected"))
    assert not is_setting_row(
        attr.evolve(base, original_device_class=BinarySensorDeviceClass.MOTION)
    )
    assert not is_setting_row(attr.evolve(base, original_name=None))
    # A status class names a setting row on a binary sensor only: the integration's
    # battery sensor carries the same class name.
    battery = attr.evolve(base, original_device_class=BinarySensorDeviceClass.BATTERY)
    assert is_setting_row(battery)
    assert not is_setting_row(attr.evolve(battery, entity_id="sensor.x"))
