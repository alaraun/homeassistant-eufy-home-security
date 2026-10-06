"""Writable enum settings, and a pan/tilt camera's default and live-view preset, as selects.

An enum setting's options are its values shown by the library's labels; the picked
label is mapped back to its value before the write. A reported value outside the
setting's values shows no option.

**Default preset** (one per camera with ``Capability.PTZ_PRESETS``): the slot the
camera returns to on its own when idle. Options are the enabled slot indexes and the
value is ``Station.default_preset``, both from the cached slots (no wake); the
entity re-reads them on the camera's presets signal. Picking one calls
``Station.async_set_default_preset``, which turns the camera there and confirms by
read-back. A capture holding the camera refuses it before anything is sent.

**Live view preset** (one per pan/tilt camera with a live stream): the slot every
live view opens at, or ``camera_default``. Home Assistant's own state, restored across
restarts; it claims nothing about the camera. A change with no viewer sends nothing;
with a view running it turns the camera at once (``Station.async_goto_preset``) and
the stream keeps running. A chosen slot the camera no longer has shows as
``camera_default`` and opens the view there.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, override

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from eufy_home_security import (
    CommandRejectedError,
    CommandUnsupportedError,
    DeviceBusyError,
    EufySecurityError,
    UnsupportedError,
    redact_serial,
)

from . import detections, errors, presets, runtime
from .const import (
    DEFAULT_PRESET_KEY,
    LIVE_PRESET_CAMERA_DEFAULT,
    LIVE_PRESET_KEY,
)
from .coordinator import StationCoordinator
from .entity import EufyDeviceEntity, EufyPushAvailability, EufySettingEntity
from .settings import SettingEntitySpec, setting_specs

if TYPE_CHECKING:
    from .runtime import EufyConfigEntry

_LOGGER = logging.getLogger(__name__)

# The camera's refusal that asks for confirmation (the app's "set anyway?" dialog).
_NEEDS_CONFIRMATION_CODE = -502

# One write at a time within this platform; the library serialises the wire per station.
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EufyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add every writable enum setting of every station and paired device, and the default
    preset of every pan/tilt camera."""
    del hass  # the coordinators carry everything this platform needs
    entities: list[SelectEntity] = [
        EufySettingSelect(coordinator, spec)
        for coordinator in entry.runtime_data.coordinators.values()
        for spec in setting_specs(coordinator.station)
        if spec.platform is Platform.SELECT
    ]
    entities.extend(
        EufyDefaultPresetSelect(coordinator, device_sn)
        for coordinator in entry.runtime_data.coordinators.values()
        for device_sn in detections.paired_device_kinds(coordinator.station)
        if detections.has_preset_entities(device_sn)
    )
    streams = runtime.streaming(entry)
    if streams is not None:
        entities.extend(
            EufyLivePresetSelect(coordinator, device_sn)
            for coordinator in entry.runtime_data.coordinators.values()
            for device_sn in detections.paired_device_kinds(coordinator.station)
            if detections.has_preset_entities(device_sn) and streams.has_camera(device_sn)
        )
    async_add_entities(entities)


class EufyDefaultPresetSelect(EufyPushAvailability, EufyDeviceEntity, SelectEntity):
    """A pan/tilt camera's default preset: the slot it returns to when idle."""

    _attr_translation_key = DEFAULT_PRESET_KEY
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: StationCoordinator, device_sn: str) -> None:
        super().__init__(coordinator, device_sn, DEFAULT_PRESET_KEY)
        self._serial = device_sn

    @property
    @override
    def options(self) -> list[str]:
        """The enabled slot indexes of the last read, ascending; empty when never read."""
        return [
            str(i) for i in sorted(presets.enabled_indexes(self.coordinator.station, self._serial))
        ]

    @property
    @override
    def current_option(self) -> str | None:
        """The library's default slot from the last read; None when unknown or none is set."""
        index = self.coordinator.station.default_preset(self._serial)
        return None if index is None else str(index)

    @override
    async def async_added_to_hass(self) -> None:
        """Follow the camera's slot changes until removed: both properties re-read them."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                presets.presets_signal(self.coordinator.config_entry.entry_id, self._serial),
                self._async_on_presets,
            )
        )

    @callback
    def _async_on_presets(self) -> None:
        self.async_write_ha_state()

    @override
    async def async_select_option(self, option: str) -> None:
        """Make slot ``option`` the default; Home Assistant has checked it is an option.

        Wakes a battery camera and turns it to the slot. The value shown changes only
        with the library's read-back; any failure keeps it and raises a translated
        error. A -502 refusal is reported, never retried with ``confirm``.
        """
        station = self.coordinator.station
        serial = redact_serial(self._serial)
        index = int(option)
        try:
            station.channel_for(self._serial)
        except UnsupportedError as err:
            raise errors.setting_device_unavailable(self._write_target()) from err
        try:
            await station.async_set_default_preset(self._serial, index)
        except DeviceBusyError as err:
            _LOGGER.debug(
                "Default preset %d of %s refused: camera busy, nothing sent", index, serial
            )
            raise errors.capture_in_progress() from err
        except CommandUnsupportedError as err:
            # Before UnsupportedError: a -108 receipt is both, and was sent.
            _LOGGER.debug("Default preset %d of %s not handled by the station", index, serial)
            raise errors.ptz_command_not_handled() from err
        except UnsupportedError as err:
            _LOGGER.debug("Default preset %d of %s refused: slot not set", index, serial)
            raise errors.preset_not_set(index) from err
        except CommandRejectedError as err:
            _LOGGER.debug("Default preset %d of %s rejected with code %d", index, serial, err.code)
            if err.code == _NEEDS_CONFIRMATION_CODE:
                raise errors.default_preset_needs_confirmation() from err
            raise errors.setting_write_failed(
                err, self._write_target(), on_demand=station.connects_on_demand
            ) from err
        except EufySecurityError as err:
            _LOGGER.debug(
                "Default preset %d of %s failed: %s", index, serial, errors.failure_reason(err)
            )
            raise errors.setting_write_failed(
                err, self._write_target(), on_demand=station.connects_on_demand
            ) from err
        _LOGGER.debug("Default preset of %s is now %d (read back)", serial, index)
        self.async_write_ha_state()

    def _write_target(self) -> str:
        """How a failed write names this entity: its translated name, never a serial."""
        name = self.name
        return name if isinstance(name, str) else DEFAULT_PRESET_KEY


class EufyLivePresetSelect(EufyPushAvailability, EufyDeviceEntity, SelectEntity, RestoreEntity):
    """The preset a pan/tilt camera's live view opens at; HA state, restored."""

    _attr_translation_key = LIVE_PRESET_KEY

    def __init__(self, coordinator: StationCoordinator, device_sn: str) -> None:
        super().__init__(coordinator, device_sn, LIVE_PRESET_KEY)
        self._serial = device_sn

    @property
    @override
    def options(self) -> list[str]:
        """``camera_default``, then the enabled slot indexes of the last read."""
        return [LIVE_PRESET_CAMERA_DEFAULT] + [
            str(i) for i in sorted(presets.enabled_indexes(self.coordinator.station, self._serial))
        ]

    @property
    @override
    def current_option(self) -> str | None:
        """The slot the next view opens at; ``camera_default`` for none or a stale one."""
        streams = runtime.streaming(self.coordinator.config_entry)
        index = None if streams is None else streams.live_preset(self._serial)
        return LIVE_PRESET_CAMERA_DEFAULT if index is None else str(index)

    @override
    async def async_added_to_hass(self) -> None:
        """Restore the last choice (no wake), and follow the camera's slot changes."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        streams = runtime.streaming(self.coordinator.config_entry)
        if last is not None and last.state.isdigit() and streams is not None:
            streams.async_set_live_preset(self._serial, int(last.state))
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                presets.presets_signal(self.coordinator.config_entry.entry_id, self._serial),
                self._async_on_presets,
            )
        )

    @callback
    def _async_on_presets(self) -> None:
        self.async_write_ha_state()

    @override
    async def async_select_option(self, option: str) -> None:
        """Store the choice; with a view running, turn the camera there first.

        A turn that fails keeps the previous choice and raises a translated error.
        """
        streams = runtime.streaming(self.coordinator.config_entry)
        if streams is None:
            return
        station = self.coordinator.station
        serial = redact_serial(self._serial)
        index = None if option == LIVE_PRESET_CAMERA_DEFAULT else int(option)
        try:
            turned = await streams.async_turn_live_view(self._serial, index)
        except DeviceBusyError as err:
            _LOGGER.debug("Live view of %s not turned: camera busy, nothing sent", serial)
            raise errors.capture_in_progress() from err
        except CommandUnsupportedError as err:
            # Before UnsupportedError: a -108 receipt is both, and was sent.
            _LOGGER.debug("Live view of %s not turned: not handled by the station", serial)
            raise errors.ptz_command_not_handled() from err
        except UnsupportedError as err:
            _LOGGER.debug("Live view of %s not turned: slot %s not set", serial, option)
            raise errors.preset_not_set(
                index if index is not None else station.default_preset(self._serial) or 0
            ) from err
        except EufySecurityError as err:
            _LOGGER.debug("Live view of %s not turned: %s", serial, errors.failure_reason(err))
            name = self.name
            raise errors.setting_write_failed(
                err,
                name if isinstance(name, str) else LIVE_PRESET_KEY,
                on_demand=station.connects_on_demand,
            ) from err
        streams.async_choose_live_preset(self._serial, index)
        _LOGGER.debug(
            "Live view preset of %s is now %s%s",
            serial,
            option,
            " (the running view was turned)" if turned else "",
        )
        self.async_write_ha_state()


class EufySettingSelect(EufySettingEntity, SelectEntity):
    """An enum setting: its values, shown by the library's labels, in the library's order."""

    def __init__(self, coordinator: StationCoordinator, spec: SettingEntitySpec) -> None:
        super().__init__(coordinator, spec)
        self._attr_options = [self.option_label(value) for value in spec.setting.values]

    @property
    @override
    def current_option(self) -> str | None:
        """The label of the reported value; None when unknown or not one of the values."""
        value = self.setting_value
        if value is None or value not in self._spec.setting.values:
            return None
        return self.option_label(value)

    @override
    async def async_select_option(self, option: str) -> None:
        """Write the value the picked label stands for; Home Assistant has checked it."""
        await self.async_write_setting(self.option_value(option))
