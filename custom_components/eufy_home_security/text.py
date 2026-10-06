"""Writable string settings (``SettingControl.TEXT``) as text entities."""

from __future__ import annotations

from typing import TYPE_CHECKING, override

from homeassistant.components.text import TextEntity
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import EufySettingEntity
from .settings import setting_specs

if TYPE_CHECKING:
    from .runtime import EufyConfigEntry

# One write at a time within this platform; the library serialises the wire per station.
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EufyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add every writable string setting of every station and paired device."""
    del hass  # the coordinators carry everything this platform needs
    async_add_entities(
        EufySettingText(coordinator, spec)
        for coordinator in entry.runtime_data.coordinators.values()
        for spec in setting_specs(coordinator.station)
        if spec.platform is Platform.TEXT
    )


class EufySettingText(EufySettingEntity, TextEntity):
    """A string setting; the library validates and renders the write."""

    @property
    @override
    def native_value(self) -> str | None:
        """The reported string; None until the station reports it."""
        value = self.setting_value
        return value if isinstance(value, str) else None

    @override
    async def async_set_value(self, value: str) -> None:
        """Write the string."""
        await self.async_write_setting(value)
