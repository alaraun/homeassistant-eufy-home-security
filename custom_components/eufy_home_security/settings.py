"""The setting-to-entity rule: which settings become entities, and on which platform.

Pure by design: nothing here reads Home Assistant state, a coordinator's data or a
eufy parameter id. The library lists each device's settings (``Station.settings_for``,
one ``Setting`` per vendor identifier); this module turns each into the platform it
belongs on, and it is the only place that decides it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from homeassistant.const import Platform

from eufy_home_security import GuardMode, Station
from eufy_home_security.devices import MODE_ACTION_FLAGS, Setting, SettingControl, SettingKind


@dataclass(frozen=True, slots=True)
class ModeAction:
    """A per-mode action mask: its guard mode and the bits the library names."""

    mode: GuardMode
    flags: Mapping[str, int]


def mode_action(setting: Setting) -> ModeAction | None:
    """The mode and named bits of a per-mode action mask (``camera_action_away``), else None."""
    for scope, flags in MODE_ACTION_FLAGS.items():
        prefix = f"{scope}_action_"
        if setting.key.startswith(prefix):
            try:
                return ModeAction(
                    mode=GuardMode.parse(setting.key.removeprefix(prefix)), flags=flags
                )
            except ValueError:
                return None
    return None


_DELAY_PREFIXES: tuple[str, ...] = ("alarm_delay_", "leaving_delay_")


@dataclass(frozen=True, slots=True)
class SettingEntitySpec:
    """One entity to build: a setting, the device it belongs to, and its platform."""

    platform: Platform
    setting: Setting
    device_sn: str | None
    """A paired device's serial; ``None`` is the station itself."""
    action: ModeAction | None = None
    """Set for one named bit of a per-mode action mask."""
    flag: str | None = None
    """The bit of ``action`` this entity switches, or the member of a ``FLAGS`` setting."""
    variant: bool = False
    """The library names another setting the app uses in this one's place (``variant_of``)."""

    @property
    def key(self) -> str:
        """The key of the unique id: the setting key, plus the flag for a mask bit."""
        if self.flag is None:
            return self.setting.key
        return f"{self.setting.key}_{self.flag}"

    @property
    def member(self) -> str | None:
        """The ``FLAGS`` member this switch turns on and off; None for any other entity."""
        return self.flag if self.action is None else None

    @property
    def translated(self) -> bool:
        """Whether the entity is named by a translation key of ours.

        Only the per-mode delays and action bits, whose keys are the library's own on
        every model. Every other setting is keyed by the vendor identifier and named
        from ``Setting.name``.
        """
        return self.action is not None or self.setting.key.startswith(_DELAY_PREFIXES)

    @property
    def writes_mode_table(self) -> bool:
        """Whether a write replaces a guard mode's whole table (refused as a sentence)."""
        return self.translated

    @property
    def is_control(self) -> bool:
        """Whether this entity changes the setting, rather than only showing its value."""
        return self.platform in _CONTROL_PLATFORMS

    @property
    def enabled_by_default(self) -> bool:
        """Controls are enabled, except the per-mode action bits; read-only values are off.

        Each camera or sensor carries five action masks (20 to 30 bits) whose names come
        from the eufy app; they, the read-only values and settings the app replaces with
        another (``Setting.variant_of``) stay one click away in the registry instead of
        crowding every device page.
        """
        return self.is_control and self.action is None and not self.variant


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


def setting_platform(setting: Setting) -> Platform | None:
    """The platform ``setting`` belongs on, or ``None`` for no entity.

    - A per-mode action mask: switches, one per named bit (see ``setting_specs``).
    - A per-mode delay: number.
    - A writable setting with a ``control``: its platform (switch, select, number, a
      switch per member for ``toggles``, text).
    - Otherwise, when readable: binary sensor for a bool, sensor for the rest.
    - Neither (a cloud-listed model, an app-local value): no entity, it never has a value.
    """
    if mode_action(setting) is not None:
        return Platform.SWITCH
    if setting.writable and setting.key.startswith(_DELAY_PREFIXES):
        return Platform.NUMBER
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
            if (action := mode_action(setting)) is not None:
                specs.extend(
                    SettingEntitySpec(
                        platform=platform,
                        setting=setting,
                        device_sn=device_sn,
                        action=action,
                        flag=flag,
                    )
                    for flag in action.flags
                )
            elif setting.writable and setting.control is SettingControl.TOGGLES:
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
