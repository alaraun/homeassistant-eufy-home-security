"""The setting-to-entity rule: which settings become entities, and on which platform.

Pure by design: nothing here reads Home Assistant state, a coordinator's data or a
eufy parameter id. The library lists each device's settings (``Station.settings_for``,
one ``Setting`` per vendor identifier); this module turns each into the platform it
belongs on, and it is the only place that decides it.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any, Final

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.number import NumberDeviceClass
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import PERCENTAGE, EntityCategory, Platform
from homeassistant.helpers import entity_registry as er

from eufy_home_security import (
    MODES_SECTION,
    Light,
    Station,
    UnsupportedError,
    entity_unique_id,
)
from eufy_home_security.devices import (
    MODE_ACTION_FLAGS,
    Scope,
    Setting,
    SettingControl,
    SettingKind,
    SettingMeaning,
    SettingRole,
    SettingShown,
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


@dataclass(frozen=True, slots=True)
class EntityTyping:
    """The device class per platform and the unit of a setting entity.

    In :data:`MEANING_TYPING` a rule: ``unit_from_setting`` takes ``Setting.unit``
    instead of ``unit``. From :func:`entity_typing` the result for one setting.
    """

    sensor: SensorDeviceClass | None = None
    number: NumberDeviceClass | None = None
    binary_sensor: BinarySensorDeviceClass | None = None
    unit: str | None = None
    unit_from_setting: bool = False


# How each ``Setting.meaning`` types its entity; the only place a setting gets a device
# class or unit. A temperature's scale (C or F) is not in the model data: no class, no unit.
MEANING_TYPING: Final[Mapping[SettingMeaning, EntityTyping]] = MappingProxyType(
    {
        SettingMeaning.DURATION: EntityTyping(
            sensor=SensorDeviceClass.DURATION,
            number=NumberDeviceClass.DURATION,
            unit_from_setting=True,
        ),
        SettingMeaning.PERCENT: EntityTyping(unit=PERCENTAGE),
        SettingMeaning.TEMPERATURE: EntityTyping(),
        SettingMeaning.TIMESTAMP: EntityTyping(sensor=SensorDeviceClass.TIMESTAMP),
        SettingMeaning.TIME_OF_DAY: EntityTyping(),
        SettingMeaning.OPENING: EntityTyping(binary_sensor=BinarySensorDeviceClass.DOOR),
        SettingMeaning.TAMPER: EntityTyping(binary_sensor=BinarySensorDeviceClass.TAMPER),
        SettingMeaning.LOW_BATTERY: EntityTyping(binary_sensor=BinarySensorDeviceClass.BATTERY),
        SettingMeaning.MOISTURE: EntityTyping(binary_sensor=BinarySensorDeviceClass.MOISTURE),
        SettingMeaning.COLD: EntityTyping(binary_sensor=BinarySensorDeviceClass.COLD),
        SettingMeaning.SIREN_RINGING: EntityTyping(binary_sensor=BinarySensorDeviceClass.SOUND),
    }
)
# No meaning, or one the table does not list: no device class, the setting's own unit.
UNTYPED: Final = EntityTyping(unit_from_setting=True)


def entity_typing(setting: Setting) -> EntityTyping:
    """The device classes and unit of ``setting``'s entity, from its meaning and unit.

    A class whose unit comes from the setting (a duration) is dropped when the setting
    has no unit: Home Assistant converts such a value by its unit.
    """
    rule = UNTYPED if setting.meaning is None else MEANING_TYPING.get(setting.meaning, UNTYPED)
    if not rule.unit_from_setting:
        return rule
    unit = setting.unit.value if setting.unit is not None and setting.unit.value else None
    if unit is None:
        return EntityTyping()
    return replace(rule, unit=unit, unit_from_setting=False)


_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)


def timestamp_value(value: object) -> datetime | None:
    """A ``TIMESTAMP`` value (integer milliseconds since the epoch, UTC, as digits or an
    int) as an aware datetime; None for 0 (none recorded), empty or anything else."""
    if isinstance(value, str):
        try:
            value = int(value.strip())
        except ValueError:
            return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    try:
        return _EPOCH + timedelta(milliseconds=value)
    except OverflowError:
        return None


def numeric_value(value: object) -> int | float | None:
    """``value`` as a finite number (an int or float, or text holding one); None otherwise."""
    if isinstance(value, str):
        text = value.strip()
        for parse in (int, float):
            try:
                value = parse(text)
                break
            except ValueError:
                continue
        else:
            return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return value if math.isfinite(value) else None


# The device classes a setting entity carries (the table's, an enum sensor); the
# integration's own entities named by a device class (battery, signal) have none of these.
_SETTING_DEVICE_CLASSES: frozenset[str] = frozenset(
    {SensorDeviceClass.ENUM}
    | {c for t in MEANING_TYPING.values() for c in (t.sensor, t.number) if c is not None}
)
# A status's binary-sensor classes; the battery sensor's class has the same name.
_STATUS_BINARY_CLASSES: frozenset[str] = frozenset(
    t.binary_sensor for t in MEANING_TYPING.values() if t.binary_sensor is not None
)

# Home Assistant's limit on a state's length.
MAX_STATE_LENGTH: Final = 255


def is_setting_row(row: er.RegistryEntry) -> bool:
    """Whether a registry row looks like a setting entity's: named by the library
    (``Setting.name``: a literal name, no translation key), a config or diagnostic
    category or none (a status on the device card), and no device class or one a
    setting entity carries (a status's on a binary sensor only)."""
    device_class = row.original_device_class
    return (
        row.translation_key is None
        and row.original_name is not None
        and row.entity_category in (EntityCategory.CONFIG, EntityCategory.DIAGNOSTIC, None)
        and (
            device_class is None
            or device_class in _SETTING_DEVICE_CLASSES
            or (row.domain == Platform.BINARY_SENSOR and device_class in _STATUS_BINARY_CLASSES)
        )
    )


def json_value(value: object) -> dict[str, Any] | list[Any] | None:
    """``value`` as a JSON object or array: a dict or list as is, a str parsed when it
    holds one; None for anything else."""
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str) and value.lstrip()[:1] in ("{", "["):
        try:
            parsed: dict[str, Any] | list[Any] = json.loads(value)
        except ValueError:
            return None
        return parsed
    return None


# A structured value's members that locate the house (the library masks the same keys in
# its logs), compared with case, ``_`` and ``-`` ignored.
_LOCATION_MEMBERS: frozenset[str] = frozenset(
    {
        "address",
        "houseaddress",
        "location",
        "latitude",
        "longitude",
        "longtitude",
        "lat",
        "lng",
        "lon",
    }
)


def without_location(value: object) -> object:
    """``value`` with every object member that locates the house left out, at any depth."""
    if isinstance(value, dict):
        return {
            key: without_location(member)
            for key, member in value.items()
            if str(key).lower().replace("_", "").replace("-", "") not in _LOCATION_MEMBERS
        }
    if isinstance(value, list):
        return [without_location(item) for item in value]
    return value


def capped_state(value: str) -> str:
    """``value`` cut to :data:`MAX_STATE_LENGTH` characters, ending in an ellipsis when cut."""
    if len(value) <= MAX_STATE_LENGTH:
        return value
    return f"{value[: MAX_STATE_LENGTH - 1]}\u2026"


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
    in_use: bool | None = None
    """For a member of a variant group: whether the eufy app uses it for this device
    (``Station.setting_in_use``); None outside a group or when the library cannot tell."""
    grouped: bool = False
    """A member of a variant group: a variant, or a primary the device has variants of."""
    light: Light | None = None
    """The lamp a ``light`` entity of this on/off setting switches; None for any other."""
    covered: bool = False
    """A brightness the device's light entity sets: registered disabled."""

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
    def is_status(self) -> bool:
        """Whether this entity shows a status the eufy app shows (``SettingRole.STATUS``)."""
        return not self.is_control and self.setting.role is SettingRole.STATUS

    @property
    def entity_category(self) -> EntityCategory | None:
        """None for a light or a status on the device card, config for another control,
        diagnostic else."""
        if self.platform is Platform.LIGHT:
            return None
        if self.is_control:
            return EntityCategory.CONFIG
        if self.is_status and self.setting.shown is SettingShown.CARD:
            return None
        return EntityCategory.DIAGNOSTIC

    @property
    def enabled_by_default(self) -> bool:
        """Controls and statuses are enabled; other read-only values are off.

        Of a variant group (``Setting.variant_of``) only the member the app uses for the
        device is on; while the library cannot tell, the primary. A setting the library
        cannot write, the other members and settings the app puts behind a warning
        (``Setting.advanced``) stay one click away in the registry instead of crowding
        every device page. So does a brightness the device's light entity sets (``covered``).
        """
        if self.covered:
            return False
        in_use = self.in_use if self.in_use is not None else not self.variant
        return (self.is_control or self.is_status) and in_use and not self.setting.advanced

    @property
    def group(self) -> str:
        """The key of the setting's variant group: its primary's key."""
        return self.setting.variant_of or self.setting.key


_CONTROL_PLATFORMS: frozenset[Platform] = frozenset(
    {Platform.LIGHT, Platform.NUMBER, Platform.SELECT, Platform.SWITCH, Platform.TEXT}
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
      switch per member for ``toggles``, text), unless its value is structured
      (``Setting.structured``): no control, it is treated as read-only below.
    - A diagnostic value (``SettingRole.DIAGNOSTIC``): none; the app shows it nowhere, or
      a device field already gives it.
    - Otherwise, when readable: binary sensor for a bool, sensor for the rest.
    - Neither (a cloud-listed model, an app-local value): no entity, it never has a value.
    """
    if is_per_mode(setting):
        return None
    if setting.writable and setting.control is not None and not setting.structured:
        return _CONTROLS[setting.control]
    if not setting.readable or setting.role is SettingRole.DIAGNOSTIC:
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

    A lamp's on/off setting that is writable where the device is (``Station.lights_for``,
    ``Light.on_off_writable``) is a light, and the brightness that light sets is
    ``covered``; a lamp whose switch is read-only there keeps its sensor.
    """
    specs: list[SettingEntitySpec] = []
    targets: list[str | None] = [None]
    targets.extend(sub.device_sn for sub in station.sub_devices if sub.device_sn)
    for device_sn in targets:
        settings = station.settings_for(device_sn)
        lights = {light.on_off: light for light in station.lights_for(device_sn)}
        lights = {key: light for key, light in lights.items() if light.on_off_writable}
        covered = {
            light.brightness.key
            for light in lights.values()
            if light.brightness is not None and light.brightness.writable
        }
        primaries = {s.variant_of for s in settings if s.variant_of is not None}
        for setting in settings:
            light = lights.get(setting.key)
            platform = Platform.LIGHT if light is not None else setting_platform(setting)
            if platform is None:
                continue
            grouped = setting.variant_of is not None or setting.key in primaries
            in_use = member_in_use(station, setting.key, device_sn) if grouped else None
            if platform is Platform.SWITCH and setting.control is SettingControl.TOGGLES:
                specs.extend(
                    SettingEntitySpec(
                        platform=platform,
                        setting=setting,
                        device_sn=device_sn,
                        flag=member,
                        variant=setting.variant_of is not None,
                        in_use=in_use,
                        grouped=grouped,
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
                        in_use=in_use,
                        grouped=grouped,
                        light=light,
                        covered=setting.key in covered,
                    )
                )
    return specs


def member_in_use(station: Station, key: str, device_sn: str | None) -> bool | None:
    """Whether the app uses variant-group member ``key`` for the device; None when the
    library cannot tell or refuses the question."""
    try:
        return station.setting_in_use(key, device_sn)
    except UnsupportedError:
        return None
