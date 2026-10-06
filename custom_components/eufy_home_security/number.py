"""Writable range settings as number entities, and each camera's live-view zoom.

**Live view zoom** (one per zoom-capable camera with a live stream): the picture zoom of
its live view, 1x-12x. Home Assistant's own state, restored across restarts, like the
live-view preset. A change with no viewer sends nothing and the next view opens at that
zoom; with a view running it zooms the camera at once (``Station.async_set_zoom``). The
camera resets its zoom on every turn and every reopen, so the value follows the chosen
preset's stored zoom when the preset changes, and the view re-applies it at each open.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, override

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from eufy_home_security import MAX_ZOOM, MIN_ZOOM
from eufy_home_security.devices import SettingControl, SettingUnit

from . import detections, ptz, runtime
from .const import LIVE_ZOOM_KEY, LIVE_ZOOM_STEP
from .coordinator import StationCoordinator
from .entity import EufyDeviceEntity, EufyPushAvailability, EufySettingEntity
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
    """Add every writable range setting of every station and paired device."""
    del hass  # the coordinators carry everything this platform needs
    entities: list[NumberEntity] = [
        EufySettingNumber(coordinator, spec)
        for coordinator in entry.runtime_data.coordinators.values()
        for spec in setting_specs(coordinator.station)
        if spec.platform is Platform.NUMBER
    ]
    streams = runtime.streaming(entry)
    if streams is not None:
        entities.extend(
            EufyLiveZoomNumber(coordinator, device_sn)
            for coordinator in entry.runtime_data.coordinators.values()
            for device_sn in detections.paired_device_kinds(coordinator.station)
            if detections.has_zoom(device_sn) and streams.has_camera(device_sn)
        )
    async_add_entities(entities)


class EufySettingNumber(EufySettingEntity, NumberEntity):
    """A range setting: bounds, step, unit and slider or box from the library."""

    def __init__(self, coordinator: StationCoordinator, spec: SettingEntitySpec) -> None:
        super().__init__(coordinator, spec)
        setting = spec.setting
        if setting.minimum is not None:
            self._attr_native_min_value = float(setting.minimum)
        if setting.maximum is not None:
            self._attr_native_max_value = float(setting.maximum)
        self._attr_native_step = float(setting.step or 1)
        self._attr_native_unit_of_measurement = (
            setting.unit.value if setting.unit is not None and setting.unit.value else None
        )
        if setting.unit is SettingUnit.SECONDS:
            self._attr_device_class = NumberDeviceClass.DURATION
        # The library's control; a per-mode delay carries none and is a duration box.
        if setting.control is SettingControl.SLIDER:
            self._attr_mode = NumberMode.SLIDER
        elif setting.control is SettingControl.BOX or spec.translated:
            self._attr_mode = NumberMode.BOX
        else:
            self._attr_mode = NumberMode.SLIDER

    @property
    @override
    def native_value(self) -> int | float | None:
        """The decoded value as the library returns it; whole numbers stay ints."""
        value = self.setting_value
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        return value

    @override
    async def async_set_native_value(self, value: float) -> None:
        """Write the value; Home Assistant has range-checked it, the library checks the step."""
        await self.async_write_setting(int(value) if float(value).is_integer() else value)


class EufyLiveZoomNumber(EufyPushAvailability, EufyDeviceEntity, NumberEntity, RestoreEntity):
    """The picture zoom of a camera's live view; HA state, restored."""

    _attr_translation_key = LIVE_ZOOM_KEY
    _attr_native_min_value = MIN_ZOOM
    _attr_native_max_value = MAX_ZOOM
    _attr_native_step = LIVE_ZOOM_STEP
    _attr_native_unit_of_measurement = "×"
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator: StationCoordinator, device_sn: str) -> None:
        super().__init__(coordinator, device_sn, LIVE_ZOOM_KEY)
        self._serial = device_sn

    @property
    @override
    def native_value(self) -> float | None:
        """The zoom the running view shows, or the next view opens at."""
        streams = runtime.streaming(self.coordinator.config_entry)
        return None if streams is None else streams.live_zoom(self._serial)

    @override
    async def async_added_to_hass(self) -> None:
        """Restore the last zoom (no wake), and follow the manager's zoom changes."""
        await super().async_added_to_hass()
        streams = runtime.streaming(self.coordinator.config_entry)
        last = await self.async_get_last_state()
        if streams is not None and last is not None:
            try:
                zoom = float(last.state)
            except ValueError:
                pass
            else:
                if MIN_ZOOM <= zoom <= MAX_ZOOM:
                    streams.async_restore_live_zoom(self._serial, zoom)
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                detections.zoom_signal(self.coordinator.config_entry.entry_id, self._serial),
                self._async_on_zoom,
            )
        )

    @callback
    def _async_on_zoom(self) -> None:
        self.async_write_ha_state()

    @override
    async def async_set_native_value(self, value: float) -> None:
        """Store the zoom; with a view running, zoom the camera first.

        A zoom the camera does not take keeps the previous value and raises a
        translated error; Home Assistant has range-checked ``value``.
        """
        await ptz.async_set_live_zoom(self.coordinator, self._serial, value, self.name)
