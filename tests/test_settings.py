"""The setting-to-entity rule and its end-to-end result on the library's fakes.

The first half is pure: ``settings.setting_platform`` against the library's bundled
model files. The second sets one entry up and reads the registry back.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

import pytest
from conftest import (
    SENSOR_SN,
    add_entry,
    add_motion_sensor,
    entity_id_for,
    seed_setting,
    set_up_warm,
    setup_entry,
    state_of,
)
from eufy_home_security import EufySecurity, Station, entity_unique_id
from eufy_home_security.devices import MODE_ACTION_FLAGS, Scope, SettingControl, SettingKind
from eufy_home_security.devices.model_settings import mode_table_settings, settings_of
from eufy_home_security.testing import SYNTHETIC, FakeCloud, FakeStation
from homeassistant.components.alarm_control_panel import DOMAIN as ALARM_DOMAIN
from homeassistant.components.camera import DOMAIN as CAMERA_DOMAIN
from homeassistant.const import EntityCategory, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.eufy_home_security.const import DOMAIN, FIRMWARE_KEY
from custom_components.eufy_home_security.settings import (
    mode_action,
    setting_platform,
    setting_specs,
)

_COMPONENT_ROOT: Final = (
    Path(__file__).resolve().parent.parent / "custom_components" / "eufy_home_security"
)
_STRINGS_PATH: Final = _COMPONENT_ROOT / "strings.json"
_EN_PATH: Final = _COMPONENT_ROOT / "translations" / "en.json"
_MODELS: Final = ("T8030", "T8160", "T8170", "T8910")
_MODE_TABLE: Final = mode_table_settings(Scope.CAMERA) + mode_table_settings(Scope.SENSOR)


# ── the platform rule ────────────────────────────────────────────────────────


_CONTROL_PLATFORMS: Final = {
    SettingControl.SWITCH: Platform.SWITCH,
    SettingControl.SELECT: Platform.SELECT,
    SettingControl.SLIDER: Platform.NUMBER,
    SettingControl.BOX: Platform.NUMBER,
    SettingControl.TOGGLES: Platform.SWITCH,
    SettingControl.TEXT: Platform.TEXT,
}


@pytest.mark.parametrize("model", _MODELS)
def test_every_bundled_setting_maps_to_the_platform_its_control_and_access_give(
    model: str,
) -> None:
    """Writable with a control: that control's platform; readable otherwise: sensor; neither: none."""
    for setting in settings_of(model).values():
        platform = setting_platform(setting)
        if setting.writable and setting.control is not None:
            assert platform is _CONTROL_PLATFORMS[setting.control], setting.key
        elif not setting.readable:
            assert platform is None, setting.key
        elif setting.kind is SettingKind.BOOL:
            assert platform is Platform.BINARY_SENSOR, setting.key
        else:
            assert platform is Platform.SENSOR, setting.key


def test_mode_action_masks_become_one_switch_per_named_bit() -> None:
    """A per-mode action mask is never a number: its named bits are switches."""
    masks = [s for s in _MODE_TABLE if s.key.split("_action_")[0] in MODE_ACTION_FLAGS]
    assert masks
    for setting in masks:
        action = mode_action(setting)
        assert action is not None, setting.key
        assert setting_platform(setting) is Platform.SWITCH
        scope = setting.key.split("_action_")[0]
        assert dict(action.flags) == dict(MODE_ACTION_FLAGS[Scope(scope)])
    delays = [s for s in _MODE_TABLE if "_delay_" in s.key]
    assert delays
    for setting in delays:
        assert mode_action(setting) is None
        assert setting_platform(setting) is Platform.NUMBER


def test_unique_id_keys_do_not_collide_within_a_platform() -> None:
    """One device's entities of one platform have distinct keys (largest set: a camera)."""
    for model in _MODELS:
        keys_by_platform: dict[Platform, list[str]] = {}
        settings = [*settings_of(model).values(), *mode_table_settings(Scope.CAMERA)]
        for setting in settings:
            platform = setting_platform(setting)
            if platform is None:
                continue
            action = mode_action(setting)
            keys = keys_by_platform.setdefault(platform, [])
            if action is not None:
                keys.extend(f"{setting.key}_{flag}" for flag in action.flags)
            else:
                keys.append(setting.key)
        for platform, keys in keys_by_platform.items():
            duplicates = sorted({key for key in keys if keys.count(key) > 1})
            assert not duplicates, (model, platform, duplicates)


@pytest.mark.parametrize("path", [_STRINGS_PATH, _EN_PATH], ids=lambda p: p.name)
def test_every_translated_setting_key_has_a_name(path: Path) -> None:
    """The per-mode delays and action bits have translated names; nothing else does."""
    document: Any = json.loads(path.read_text())
    missing: list[str] = []
    for setting in _MODE_TABLE:
        action = mode_action(setting)
        platform = str(setting_platform(setting))
        keys = (
            [f"{setting.key}_{flag}" for flag in action.flags]
            if action is not None
            else [setting.key]
        )
        for key in keys:
            name = document["entity"].get(platform, {}).get(key, {}).get("name")
            if not isinstance(name, str) or not name.strip():
                missing.append(f"entity.{platform}.{key}.name")
    assert not missing, missing


# ── end to end ───────────────────────────────────────────────────────────────


def _expected_keys(station: Station, device_sn: str | None) -> list[str]:
    """Every entity key one device contributes, in the library's order."""
    keys: list[str] = []
    for setting in station.settings_for(device_sn):
        if setting_platform(setting) is None:
            continue
        action = mode_action(setting)
        if action is not None:
            keys.extend(f"{setting.key}_{flag}" for flag in action.flags)
        elif setting.writable and setting.control is SettingControl.TOGGLES:
            keys.extend(f"{setting.key}_{member}" for member in setting.flags)
        else:
            keys.append(setting.key)
    return keys


async def test_setting_specs_yield_the_station_first_then_devices_in_paired_list_order(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Specs come station first, then one paired device at a time, in the library's order.

    Home Assistant hands out entity ids in the order entities are added, so a stable
    order keeps entity ids stable across restarts.
    """
    add_motion_sensor(fake_station, fake_cloud)
    entry = await set_up_warm(hass, seed_warm_cache)
    station = entry.runtime_data.coordinators[SYNTHETIC.station_sn].station

    paired: list[str | None] = [sub.device_sn for sub in station.sub_devices]
    assert paired == [SYNTHETIC.camera_sn, SENSOR_SN], paired
    assert all(_expected_keys(station, device_sn) for device_sn in (None, *paired))

    def expected(order: list[str | None]) -> list[tuple[str | None, str]]:
        return [(sn, key) for sn in order for key in _expected_keys(station, sn)]

    specs = setting_specs(station)
    assert [(spec.device_sn, spec.key) for spec in specs] == expected([None, *paired])

    station.sub_devices = tuple(reversed(station.sub_devices))
    reordered = setting_specs(station)
    assert [(spec.device_sn, spec.key) for spec in reordered] == expected(
        [None, SENSOR_SN, SYNTHETIC.camera_sn]
    )

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_controls_are_config_and_enabled_read_only_values_are_diagnostic_and_off(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Category and default enablement follow the platform; action bits and variants start off."""
    entry = await set_up_warm(hass, seed_warm_cache)
    registry = er.async_get(hass)
    station = entry.runtime_data.coordinators[SYNTHETIC.station_sn].station
    specs = setting_specs(station)
    assert {spec.platform for spec in specs} >= {
        Platform.SWITCH,
        Platform.SELECT,
        Platform.NUMBER,
        Platform.SENSOR,
    }
    for spec in specs:
        unique_id = entity_unique_id(spec.device_sn or station.serial, spec.key)
        entity_id = registry.async_get_entity_id(spec.platform, DOMAIN, unique_id)
        assert entity_id is not None, spec.key
        entity = registry.async_get(entity_id)
        assert entity is not None
        if spec.is_control:
            assert entity.entity_category is EntityCategory.CONFIG, spec.key
        else:
            assert entity.entity_category is EntityCategory.DIAGNOSTIC, spec.key
        if spec.is_control and spec.action is None and not spec.variant:
            assert entity.disabled_by is None, spec.key
        else:
            assert entity.disabled_by is er.RegistryEntryDisabler.INTEGRATION, spec.key

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_vendor_setting_is_named_by_the_library_and_a_delay_by_our_translation(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Vendor identifiers carry the library's name; per-mode keys carry a translation key."""
    seed_setting(fake_station, "trigger_interval_time", 30)
    entry = await set_up_warm(hass, seed_warm_cache)
    registry = er.async_get(hass)

    vendor = registry.async_get(
        entity_id_for(hass, Platform.NUMBER, SYNTHETIC.camera_sn, "trigger_interval_time")
    )
    assert vendor is not None
    assert vendor.translation_key is None
    assert vendor.original_name == settings_of("T8160")["trigger_interval_time"].name
    assert state_of(hass, vendor.entity_id) == "30"

    delay = registry.async_get(
        entity_id_for(hass, Platform.NUMBER, SYNTHETIC.camera_sn, "alarm_delay_away")
    )
    assert delay is not None
    assert delay.translation_key == "alarm_delay_away"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


_DEVICE_CLASS_NAMED: Final = frozenset({"battery", "signal_strength"})


async def test_every_registered_entity_has_a_name(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """No entity reaches a user as a bare key: a translation, a library name or a device class."""
    add_motion_sensor(fake_station, fake_cloud)
    entry = await set_up_warm(hass, seed_warm_cache)
    document: Any = json.loads(_EN_PATH.read_text())
    unnamed: list[str] = []
    for entity in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id):
        if entity.domain in (ALARM_DOMAIN, CAMERA_DOMAIN):
            continue
        if entity.translation_key is None:
            if (entity.original_name or "").strip():
                continue
            if entity.original_device_class not in _DEVICE_CLASS_NAMED:
                unnamed.append(entity.entity_id)
            continue
        name = document["entity"].get(entity.domain, {}).get(entity.translation_key, {}).get("name")
        if not isinstance(name, str) or not name.strip():
            unnamed.append(f"{entity.entity_id} ({entity.translation_key})")
    assert not unnamed, sorted(unnamed)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_setting_moved_to_another_platform_leaves_no_orphan_behind(
    hass: HomeAssistant,
    fake_station: FakeStation,
    seed_warm_cache: Callable[..., None],
    built_clients: list[EufySecurity],
) -> None:
    """A registry row on a platform the setting is not built on is forgotten.

    A non-setting entity sharing a key is kept, and so is another config entry's row.
    """
    seed_warm_cache()
    entry = add_entry(hass)
    registry = er.async_get(hass)
    assert await setup_entry(hass, entry)
    station = entry.runtime_data.coordinators[SYNTHETIC.station_sn].station
    specs = [spec for spec in setting_specs(station) if spec.device_sn == SYNTHETIC.camera_sn]
    assert specs
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    setting_domains = {
        Platform.SWITCH,
        Platform.SELECT,
        Platform.NUMBER,
        Platform.SENSOR,
        Platform.BINARY_SENSOR,
    }
    stale: list[tuple[str, str]] = []
    for spec in specs:
        unique_id = entity_unique_id(SYNTHETIC.camera_sn, spec.key)
        for other_platform in setting_domains - {spec.platform}:
            registry.async_get_or_create(other_platform, DOMAIN, unique_id, config_entry=entry)
            stale.append((other_platform, unique_id))
    firmware_uid = entity_unique_id(SYNTHETIC.camera_sn, FIRMWARE_KEY)
    kept_firmware = registry.async_get_or_create("update", DOMAIN, firmware_uid, config_entry=entry)
    other = add_entry(hass, email="other@example.com", unique_id="other@example.com")
    foreign_domain, foreign_uid = stale.pop()
    foreign_id = registry.async_get_entity_id(foreign_domain, DOMAIN, foreign_uid)
    assert foreign_id is not None
    registry.async_update_entity(foreign_id, config_entry_id=other.entry_id)

    assert await setup_entry(hass, entry)

    for domain, unique_id in stale:
        assert registry.async_get_entity_id(domain, DOMAIN, unique_id) is None, (domain, unique_id)
    for spec in specs:
        unique_id = entity_unique_id(SYNTHETIC.camera_sn, spec.key)
        assert registry.async_get_entity_id(spec.platform, DOMAIN, unique_id) is not None
    assert registry.async_get(kept_firmware.entity_id) is not None
    assert registry.async_get(foreign_id) is not None
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_variant_control_is_registered_disabled(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A setting with ``variant_of`` keeps its platform but is off by default; the one the
    app uses stays on, whichever key carries the suffix. A flags variant's member
    switches are all off by default."""
    models = settings_of("T8160")
    assert models["detection_type_set__v1"].variant_of == "detection_type_set"
    assert models["nightvision_type"].variant_of == "nightvision_type_new"
    entry = await set_up_warm(hass, seed_warm_cache)
    registry = er.async_get(hass)
    for domain, key, disabled in (
        ("switch", "detection_type_set_1", False),
        ("switch", "detection_type_set__v1_1", True),
        ("switch", "detection_type_set__v1_0", True),
        ("select", "nightvision_type_new", False),
        ("select", "nightvision_type", True),
    ):
        unique_id = entity_unique_id(SYNTHETIC.camera_sn, key)
        entity_id = registry.async_get_entity_id(domain, DOMAIN, unique_id)
        assert entity_id is not None, key
        registered = registry.async_get(entity_id)
        assert registered is not None
        assert (registered.disabled_by is er.RegistryEntryDisabler.INTEGRATION) is disabled, key
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
