"""Camera lamps as lights: one per lamp the library names (``Station.lights_for``) whose
switch is writable where the device is (``Light.on_off_writable``).

The light takes the unique id of the lamp's on/off setting (``<serial>_<on_off>``), and
sets the lamp's brightness where the library can write it there. Writes go through the
setting write path of :class:`~.entity.EufySettingEntity`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final, override

from homeassistant.components.light import ATTR_BRIGHTNESS, ColorMode, LightEntity
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util.color import brightness_to_value, value_to_brightness

from eufy_home_security import Light
from eufy_home_security.devices.lights import LOWEST_WRITTEN_BRIGHTNESS

from . import errors
from .coordinator import StationCoordinator
from .entity import EufySettingEntity
from .settings import SettingEntitySpec, setting_specs

if TYPE_CHECKING:
    from .runtime import EufyConfigEntry

# One write at a time within this platform; the library serialises the wire per station.
PARALLEL_UPDATES = 1

# Home Assistant's brightness 1-255 against the lamp's percent.
_PERCENT: Final = (1, 100)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EufyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a light per lamp the library lets this integration switch."""
    del hass  # the coordinators carry everything this platform needs
    async_add_entities(
        EufyLight(coordinator, spec, spec.light)
        for coordinator in entry.runtime_data.coordinators.values()
        for spec in setting_specs(coordinator.station)
        if spec.platform is Platform.LIGHT and spec.light is not None
    )


class EufyLight(EufySettingEntity, LightEntity):
    """A lamp: on and off, and its brightness where it is writable.

    Assumed state unless the switch is read from the param the station's light report
    updates (``Light.state_reported``): a lamp that goes dark by itself then shows off.
    """

    def __init__(
        self, coordinator: StationCoordinator, spec: SettingEntitySpec, light: Light
    ) -> None:
        super().__init__(coordinator, spec)
        self._light = light
        self._attr_name = light.name
        self._attr_assumed_state = not (light.state_reported and light.on_off_readable)
        brightness = light.brightness
        self._brightness = brightness if brightness is not None and brightness.writable else None
        mode = ColorMode.ONOFF if self._brightness is None else ColorMode.BRIGHTNESS
        self._attr_color_mode = mode
        self._attr_supported_color_modes = {mode}
        # The setting that enables the lamp's control in the eufy app, when the device has it.
        self._enabled_by = next(
            (
                setting
                for setting in coordinator.station.settings_for(spec.device_sn)
                if setting.key == light.enabled_by
            ),
            None,
        )

    @property
    @override
    def is_on(self) -> bool | None:
        """The on/off setting's value; None until known."""
        value = self.setting_value
        return value if isinstance(value, bool) else None

    @property
    @override
    def brightness(self) -> int | None:
        """The lamp's brightness on Home Assistant's 1-255 scale; None without a writable
        brightness or until its value is known."""
        brightness = self._brightness
        if brightness is None:
            return None
        percent = brightness.percent(self.value_of(brightness.key, readable=brightness.readable))
        return None if percent is None else value_to_brightness(_PERCENT, percent)

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Write the brightness when one is given, then on unless the lamp is on already.

        A brightness of 1 % or less turns the lamp off instead, as the eufy app does.
        """
        self._require_control_enabled()
        brightness = self._brightness
        if brightness is not None and ATTR_BRIGHTNESS in kwargs:
            percent = brightness_to_value(_PERCENT, kwargs[ATTR_BRIGHTNESS])
            if round(percent) < LOWEST_WRITTEN_BRIGHTNESS:
                await self.async_write_setting(False)
                return
            await self.async_write(
                brightness.key, brightness.value_for(percent), readable=brightness.readable
            )
            if self.is_on is True:
                return
        await self.async_write_setting(True)

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Write off."""
        del kwargs  # turning off takes no parameters
        self._require_control_enabled()
        await self.async_write_setting(False)

    def _require_control_enabled(self) -> None:
        """Refuse a switch while the setting that enables the lamp's control reads off;
        nothing is sent. Unknown or unreported allows it."""
        enabled_by = self._enabled_by
        if enabled_by is None:
            return
        if self.value_of(enabled_by.key, readable=enabled_by.readable) is False:
            raise errors.light_control_off(self._write_target(), enabled_by.name)
