"""Writable on/off settings, each member of a flags setting and each named bit of a
per-mode action mask, as switches.

``EufySettingSwitch`` writes the bool; the library renders it to the device's own code.
A bool that is one bit of a shared parameter (``Setting.bit``) is written the same way:
the library reads the mask fresh and moves only that bit.

``EufyFlagSwitch`` turns one member of a ``FLAGS`` setting (``detection_type_set``:
human, vehicle, pet, other motion) on or off with ``Station.async_set_flag``, which
reads the mask fresh and keeps every other bit.

``EufyModeActionSwitch`` writes ONE bit of a per-mode action mask and never a mask: the
bits without a name are mode bits a whole-mask write would clear. The library reads the
current mask, changes the bit and confirms the table by read-back.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, override

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import StationCoordinator
from .entity import EufySettingEntity
from .settings import SettingEntitySpec, setting_specs

if TYPE_CHECKING:
    from .runtime import EufyConfigEntry

# One write at a time within this platform; the library serialises the wire per station.
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EufyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add every writable on/off setting, one switch per flags member and one per named
    bit of every action mask."""
    del hass  # the coordinators carry everything this platform needs
    entities: list[SwitchEntity] = []
    for coordinator in entry.runtime_data.coordinators.values():
        for spec in setting_specs(coordinator.station):
            if spec.platform is not Platform.SWITCH:
                continue
            if spec.action is not None:
                entities.append(EufyModeActionSwitch(coordinator, spec))
            elif spec.member is not None:
                entities.append(EufyFlagSwitch(coordinator, spec))
            else:
                entities.append(EufySettingSwitch(coordinator, spec))
    async_add_entities(entities)


class EufySettingSwitch(EufySettingEntity, SwitchEntity):
    """A bool setting."""

    @property
    @override
    def is_on(self) -> bool | None:
        """The decoded value; None until the station reports it."""
        value = self.setting_value
        return value if isinstance(value, bool) else None

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Write True."""
        del kwargs  # a setting switch takes no service parameters
        await self.async_write_setting(True)

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Write False."""
        del kwargs  # a setting switch takes no service parameters
        await self.async_write_setting(False)


class EufyModeActionSwitch(EufySettingEntity, SwitchEntity):
    """One named bit of a per-mode action mask ("Away · camera siren").

    The bit names follow the eufy app's enum; the read-back confirms the whole mask,
    not each name.
    """

    @property
    @override
    def is_on(self) -> bool | None:
        """Whether this bit is set in the reported mask; None until it is reported."""
        mask, action, flag = self.setting_value, self._spec.action, self._spec.flag
        if not isinstance(mask, int) or isinstance(mask, bool) or action is None or flag is None:
            return None
        return bool(mask & action.flags[flag])

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Set this bit; every other bit stays as the station has it."""
        del kwargs  # a setting switch takes no service parameters
        await self.async_write_mode_action(True)

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Clear this bit; every other bit stays as the station has it."""
        del kwargs  # a setting switch takes no service parameters
        await self.async_write_mode_action(False)


class EufyFlagSwitch(EufySettingEntity, SwitchEntity):
    """One member of a ``FLAGS`` setting ("Detection types: Pet"), named by the app."""

    def __init__(self, coordinator: StationCoordinator, spec: SettingEntitySpec) -> None:
        super().__init__(coordinator, spec)
        member = spec.member
        if member is not None:
            self._attr_name = f"{spec.setting.name}: {spec.setting.flag_label(member)}"

    @property
    @override
    def is_on(self) -> bool | None:
        """Whether every bit of this member is set in the reported mask; None until reported."""
        mask, member = self.setting_value, self._spec.member
        if not isinstance(mask, int) or isinstance(mask, bool) or member is None:
            return None
        return member in self._spec.setting.decode_flags(mask)[0]

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Set this member; every other bit stays as the station has it."""
        del kwargs  # a setting switch takes no service parameters
        await self.async_write_flag(True)

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Clear this member; every other bit stays as the station has it."""
        del kwargs  # a setting switch takes no service parameters
        await self.async_write_flag(False)
