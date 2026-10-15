"""The setting-to-entity rule: which settings become entities, and on which platform.

Pure by design: nothing here reads Home Assistant state, a coordinator's data or a
eufy parameter id. The library lists each device's settings (``Station.settings_for``,
one ``Setting`` per vendor identifier); this module turns each into the platform it
belongs on, and it is the only place that decides it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from homeassistant.components.number import NumberDeviceClass
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import EntityCategory, Platform
from homeassistant.helpers import entity_registry as er

from eufy_home_security import MODES_SECTION, Station, entity_unique_id
from eufy_home_security.devices import (
    MODE_ACTION_FLAGS,
    Scope,
    Setting,
    SettingControl,
    SettingKind,
)
from eufy_home_security.devices.model_settings import PARENTLESS_ONLY, mode_table_settings

# The per-mode delays every paired camera or sensor carries, one per guard mode.
_DELAY_KEYS: frozenset[str] = frozenset(s.key for s in mode_table_settings(Scope.SUB_DEVICE))
# The per-mode delays and action masks: the station's mode table, which the HomeBase card
# shows and writes (``homebase/mode_table/set``). None of them is an entity.
_PER_MODE_KEYS: frozenset[str] = frozenset(
    s.key for scope in Scope for s in mode_table_settings(scope)
)
# The keys of the per-mode entities earlier releases built: a number (a sensor when
# read-only) per delay and a switch per named bit of an action mask.
_PER_MODE_ENTITY_KEYS: frozenset[str] = _DELAY_KEYS | frozenset(
    f"{s.key}_{flag}"
    for scope, flags in MODE_ACTION_FLAGS.items()
    for s in mode_table_settings(scope)
    if s.key not in _DELAY_KEYS
    for flag in flags
)


def is_per_mode(setting: Setting) -> bool:
    """Whether ``setting`` is a per-mode delay or action mask, edited on the HomeBase card."""
    return setting.section == MODES_SECTION and setting.key in _PER_MODE_KEYS


def is_per_mode_entity(unique_id: str) -> bool:
    """Whether ``unique_id`` (``<serial>_<key>``) is a per-mode delay or action-bit entity."""
    return unique_id.partition("_")[2] in _PER_MODE_ENTITY_KEYS


# The device classes a setting entity carries (a seconds range, an enum sensor); the
# integration's own entities named by a device class (battery, signal) have none of these.
_SETTING_DEVICE_CLASSES: frozenset[str] = frozenset(
    {NumberDeviceClass.DURATION, SensorDeviceClass.DURATION, SensorDeviceClass.ENUM}
)


def is_setting_row(row: er.RegistryEntry) -> bool:
    """Whether a registry row looks like a setting entity's: named by the library
    (``Setting.name``: a literal name, no translation key), a config or diagnostic
    category, and no device class or one a setting entity carries."""
    return (
        row.translation_key is None
        and row.original_name is not None
        and row.entity_category in (EntityCategory.CONFIG, EntityCategory.DIAGNOSTIC)
        and (
            row.original_device_class is None
            or row.original_device_class in _SETTING_DEVICE_CLASSES
        )
    )


@dataclass(frozen=True, slots=True)
class SettingEntitySpec:
    """One entity to build: a setting, the device it belongs to, and its platform."""

    platform: Platform
    setting: Setting
    device_sn: str | None
    """A paired device's serial; ``None`` is the station itself."""
    flag: str | None = None
    """The member of a ``FLAGS`` setting this switch turns on and off."""
    variant: bool = False
    """The library names another setting the app uses in this one's place (``variant_of``)."""

    @property
    def key(self) -> str:
        """The key of the unique id: the setting key, plus the member for a ``FLAGS`` switch."""
        if self.flag is None:
            return self.setting.key
        return f"{self.setting.key}_{self.flag}"

    @property
    def member(self) -> str | None:
        """The ``FLAGS`` member this switch turns on and off; None for any other entity."""
        return self.flag

    @property
    def is_control(self) -> bool:
        """Whether this entity changes the setting, rather than only showing its value."""
        return self.platform in _CONTROL_PLATFORMS

    @property
    def enabled_by_default(self) -> bool:
        """Controls are enabled; read-only values are off.

        Read-only values, settings the app replaces with another (``Setting.variant_of``)
        and settings the app puts behind a warning (``Setting.advanced``) stay one click
        away in the registry instead of crowding every device page.
        """
        return self.is_control and not self.variant and not self.setting.advanced


_CONTROL_PLATFORMS: frozenset[Platform] = frozenset(
    {Platform.NUMBER, Platform.SELECT, Platform.SWITCH, Platform.TEXT}
)
_CONTROLS: Mapping[SettingControl, Platform] = {
    SettingControl.SWITCH: Platform.SWITCH,
    SettingControl.SELECT: Platform.SELECT,
    SettingControl.SLIDER: Platform.NUMBER,
    SettingControl.BOX: Platform.NUMBER,
    SettingControl.TOGGLES: Platform.SWITCH,
    SettingControl.TEXT: Platform.TEXT,
}


def offered_only_without_parent(unique_id: str, device_sn: str) -> bool:
    """Whether ``unique_id`` is device ``device_sn``'s entity of a setting the library lists
    only for a device without a parent station (``PARENTLESS_ONLY``); a ``toggles``
    member's entity counts."""
    for key in PARENTLESS_ONLY:
        base = entity_unique_id(device_sn, key)
        if unique_id == base or unique_id.startswith(f"{base}_"):
            return True
    return False


def setting_platform(setting: Setting) -> Platform | None:
    """The platform ``setting`` belongs on, or ``None`` for no entity.

    - A per-mode delay or action mask: none, the HomeBase card edits it (``is_per_mode``).
    - A writable setting with a ``control``: its platform (switch, select, number, a
      switch per member for ``toggles``, text).
    - Otherwise, when readable: binary sensor for a bool, sensor for the rest.
    - Neither (a cloud-listed model, an app-local value): no entity, it never has a value.
    """
    if is_per_mode(setting):
        return None
    if setting.writable and setting.control is not None:
        return _CONTROLS[setting.control]
    if not setting.readable:
        return None
    if setting.kind is SettingKind.BOOL:
        return Platform.BINARY_SENSOR
    return Platform.SENSOR


def setting_specs(station: Station) -> list[SettingEntitySpec]:
    """Every setting entity of a station and of its paired devices, in the library's order.

    The devices are the cloud's paired list, the set ``_async_register_devices``
    registers; a paired device with no serial has no identity and is skipped. The
    parameter dump is never enumerated: a camera offline at setup still gets its
    entities, and a setting the station does not report shows as unknown.
    """
    specs: list[SettingEntitySpec] = []
    targets: list[str | None] = [None]
    targets.extend(sub.device_sn for sub in station.sub_devices if sub.device_sn)
    for device_sn in targets:
        for setting in station.settings_for(device_sn):
            platform = setting_platform(setting)
            if platform is None:
                continue
            if setting.writable and setting.control is SettingControl.TOGGLES:
                specs.extend(
                    SettingEntitySpec(
                        platform=platform,
                        setting=setting,
                        device_sn=device_sn,
                        flag=member,
                        variant=setting.variant_of is not None,
                    )
                    for member in setting.flags
                )
            else:
                specs.append(
                    SettingEntitySpec(
                        platform=platform,
                        setting=setting,
                        device_sn=device_sn,
                        variant=setting.variant_of is not None,
                    )
                )
    return specs
