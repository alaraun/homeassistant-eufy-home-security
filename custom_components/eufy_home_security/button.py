"""Buttons: a camera's image on demand, presets, pan/tilt steps, and the device-list refresh.

Camera buttons (each camera with a camera entity):

- **Capture live image** takes one live keyframe; it always wakes a battery camera
  and ignores the live-snapshot option and its cooldown.
- **Refresh image** shows the camera's newest recorded event per the Camera image
  option (thumbnail, else the recording's trigger frame). It never wakes a camera
  paired to a HomeBase; a standalone battery camera is woken to read its newest still.

A press only queues the job on the station's media worker; the outcome is logged at
DEBUG, never raised, and presses of one kind join a queued or running job. A
"Capture live image" press while a preset capture holds the camera is refused with a
translated error (the library allows one capture per camera). The buttons follow the
station's session, never a running capture.

A pan/tilt camera also gets **Refresh presets** (one slot read per press, which wakes
a battery camera; setup never reads them) and one **Capture preset n** per slot the
last read showed set (unavailable while unset). A press for another slot while a
capture runs is a translated error, never a queue. With ``Capability.PTZ_CONTROL``:
**Pan left/right**, **Tilt up/down** (one step per press; the camera returns to its
default preset once idle) and, with presets, **Save current view** (stores the view
in the lowest free slot; a full camera refuses it).

**Refresh device list** sits on the account's service device and fetches eufy's
device list once per press (a warm start never does: eufy locks the account after
repeated sign-ins). A press during a refresh or a pending reload makes no call. A
changed station or paired-device list reloads the entry once; the devices the list no
longer names are removed. A failure raises a translated error and
goes to reauth or the account's repair issue.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, override

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from eufy_home_security import (
    EufySecurityError,
    PanTilt,
    Station,
    redact_serial,
)

from . import detections, errors, presets, ptz, runtime, stale_devices
from .const import (
    CAPTURE_LIVE_IMAGE_KEY,
    CAPTURE_PRESET_KEY,
    DOMAIN,
    PAN_LEFT_KEY,
    PAN_RIGHT_KEY,
    REFRESH_DEVICE_LIST_KEY,
    REFRESH_IMAGE_KEY,
    REFRESH_PRESETS_KEY,
    SAVE_VIEW_KEY,
    TILT_DOWN_KEY,
    TILT_UP_KEY,
)
from .coordinator import StationCoordinator
from .entity import EufyDeviceEntity, EufyPushAvailability
from .presets import PresetManager
from .snapshots import SnapshotManager

if TYPE_CHECKING:
    from .runtime import EufyConfigEntry

_LOGGER = logging.getLogger(__name__)

# A camera press only queues work on the snapshot manager, so HA need not serialise
# presses; the account button guards its own overlap.
PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class EufyCameraButtonDescription(ButtonEntityDescription):
    """A camera button: what a press asks the snapshot manager for."""

    press_fn: Callable[[SnapshotManager, Station, str], None]
    # Whether a press is refused while a preset capture holds the camera. Not
    # while the snapshot manager's own live capture does: such presses coalesce.
    busy_check: bool = False


BUTTONS: tuple[EufyCameraButtonDescription, ...] = (
    EufyCameraButtonDescription(
        key=CAPTURE_LIVE_IMAGE_KEY,
        translation_key=CAPTURE_LIVE_IMAGE_KEY,
        press_fn=SnapshotManager.async_request_capture,
        busy_check=True,
    ),
    EufyCameraButtonDescription(
        key=REFRESH_IMAGE_KEY,
        translation_key=REFRESH_IMAGE_KEY,
        press_fn=SnapshotManager.async_request_refresh,
    ),
)


# The pan/tilt step buttons: (unique-id and translation key, direction).
PAN_TILT_BUTTONS: tuple[tuple[str, PanTilt], ...] = (
    (PAN_LEFT_KEY, PanTilt.LEFT),
    (PAN_RIGHT_KEY, PanTilt.RIGHT),
    (TILT_UP_KEY, PanTilt.UP),
    (TILT_DOWN_KEY, PanTilt.DOWN),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EufyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the account's refresh button, the buttons of every camera, and the
    preset buttons of every pan/tilt camera, from the cached slots only."""
    manager = entry.runtime_data.snapshots
    preset_manager = entry.runtime_data.presets
    entities: list[ButtonEntity] = [EufyRefreshDeviceListButton(entry)]
    entities.extend(
        EufyCameraButton(coordinator, device_sn, manager, preset_manager, description)
        for coordinator in entry.runtime_data.coordinators.values()
        for device_sn, kind in detections.paired_device_kinds(coordinator.station).items()
        if detections.has_detection_entities(kind)
        for description in BUTTONS
    )
    entities.extend(
        EufyRefreshPresetsButton(coordinator, device_sn, preset_manager)
        for coordinator in entry.runtime_data.coordinators.values()
        for device_sn in detections.paired_device_kinds(coordinator.station)
        if detections.has_preset_entities(device_sn)
    )
    entities.extend(
        EufyPanTiltButton(coordinator, device_sn, key, direction)
        for coordinator in entry.runtime_data.coordinators.values()
        for device_sn in detections.paired_device_kinds(coordinator.station)
        if detections.has_pan_tilt_control(device_sn)
        for key, direction in PAN_TILT_BUTTONS
    )
    entities.extend(
        EufySaveViewButton(coordinator, device_sn, preset_manager)
        for coordinator in entry.runtime_data.coordinators.values()
        for device_sn in detections.paired_device_kinds(coordinator.station)
        if detections.has_pan_tilt_control(device_sn) and detections.has_preset_entities(device_sn)
    )
    async_add_entities(entities)
    for coordinator in entry.runtime_data.coordinators.values():
        for device_sn in detections.paired_device_kinds(coordinator.station):
            if detections.has_preset_entities(device_sn):
                _add_preset_capture_buttons(
                    hass, entry, coordinator, device_sn, preset_manager, async_add_entities
                )


def _add_preset_capture_buttons(
    hass: HomeAssistant,
    entry: EufyConfigEntry,
    coordinator: StationCoordinator,
    device_sn: str,
    manager: PresetManager,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Bind one camera's capture-button adder, now and on every later slot change."""

    def _add(index: int) -> None:
        async_add_entities([EufyPresetCaptureButton(coordinator, device_sn, index, manager)])

    presets.async_add_slot_entities(hass, entry, coordinator.station, device_sn, _add)


class EufyCameraButton(EufyPushAvailability, EufyDeviceEntity, ButtonEntity):
    """One camera button; available while its station's session is up."""

    entity_description: EufyCameraButtonDescription

    def __init__(
        self,
        coordinator: StationCoordinator,
        device_sn: str,
        manager: SnapshotManager,
        presets: PresetManager,
        description: EufyCameraButtonDescription,
    ) -> None:
        super().__init__(coordinator, device_sn, description.key)
        self.entity_description = description
        self._serial = device_sn
        self._manager = manager
        self._presets = presets

    @override
    async def async_press(self) -> None:
        """Queue the job and return; the station is never awaited here.

        A capture press while a preset capture holds the camera is refused before
        anything is queued: the library allows one capture per camera, and a
        queued live capture would only fail against it. The button stays available.
        The library's own ``is_capturing`` says nothing about which capture holds the
        camera (the snapshot manager's own live capture, whose presses coalesce, holds
        it too), so the preset manager is asked; the library's flag is traced.
        """
        station = self.coordinator.station
        if self.entity_description.busy_check:
            preset = self._presets.capturing_index(self._serial)
            if preset is not None:
                _LOGGER.debug(
                    "Capture live image for %s refused: camera busy with the capture of "
                    "preset %d (library capturing: %s), nothing sent",
                    redact_serial(self._serial),
                    preset,
                    station.is_capturing(self._serial),
                )
                raise errors.capture_in_progress()
        self.entity_description.press_fn(self._manager, station, self._serial)


class EufyRefreshPresetsButton(EufyPushAvailability, EufyDeviceEntity, ButtonEntity):
    """A pan/tilt camera's "Refresh presets": one slot read per press, which wakes it."""

    _attr_translation_key = REFRESH_PRESETS_KEY
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self, coordinator: StationCoordinator, device_sn: str, manager: PresetManager
    ) -> None:
        super().__init__(coordinator, device_sn, REFRESH_PRESETS_KEY)
        self._serial = device_sn
        self._manager = manager

    @override
    async def async_press(self) -> None:
        """Start the read and return; a running read is joined."""
        self._manager.async_request_refresh_presets(self.coordinator.station, self._serial)


class EufyPresetCaptureButton(EufyPushAvailability, EufyDeviceEntity, ButtonEntity):
    """One slot's "Capture preset n"; available while the slot is set in the latest read."""

    _attr_translation_key = CAPTURE_PRESET_KEY

    def __init__(
        self, coordinator: StationCoordinator, device_sn: str, index: int, manager: PresetManager
    ) -> None:
        super().__init__(coordinator, device_sn, presets.preset_capture_key(index))
        self._attr_translation_placeholders = {"index": str(index)}
        self._serial = device_sn
        self._index = index
        self._manager = manager

    @property
    @override
    def available(self) -> bool:
        """Available while the camera is, and the slot is set in the latest read."""
        return super().available and self._index in presets.enabled_indexes(
            self.coordinator.station, self._serial
        )

    @override
    async def async_added_to_hass(self) -> None:
        """Follow the camera's slot changes until removed: availability re-reads them."""
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
    async def async_press(self) -> None:
        """Start the capture and return at once; a busy camera or an unset slot raises."""
        self._manager.async_request_preset(self.coordinator.station, self._serial, self._index)


class EufyPanTiltButton(EufyPushAvailability, EufyDeviceEntity, ButtonEntity):
    """One pan/tilt step of a camera; the press returns once the camera has moved.

    The step is not lasting: the camera returns to its default preset once idle.
    """

    def __init__(
        self, coordinator: StationCoordinator, device_sn: str, key: str, direction: PanTilt
    ) -> None:
        super().__init__(coordinator, device_sn, key)
        self._attr_translation_key = key
        self._serial = device_sn
        self._direction = direction

    @override
    async def async_press(self) -> None:
        """Move one step; a capture holding the camera or a failed step raises."""
        await ptz.async_pan_tilt(self.coordinator, self._serial, self._direction, self.name)


class EufySaveViewButton(EufyPushAvailability, EufyDeviceEntity, ButtonEntity):
    """A pan/tilt camera's "Save current view": stores the view in the lowest free slot."""

    _attr_translation_key = SAVE_VIEW_KEY

    def __init__(
        self, coordinator: StationCoordinator, device_sn: str, manager: PresetManager
    ) -> None:
        super().__init__(coordinator, device_sn, SAVE_VIEW_KEY)
        self._serial = device_sn
        self._manager = manager

    @override
    async def async_press(self) -> None:
        """Save the view; a full camera, a capture holding it or a failed save raises."""
        await ptz.async_save_preset(self.coordinator, self._serial, self._manager, name=self.name)


class EufyRefreshDeviceListButton(ButtonEntity):
    """The account's "Refresh device list": one cloud device-list fetch per press."""

    _attr_has_entity_name = True
    _attr_translation_key = REFRESH_DEVICE_LIST_KEY
    _attr_entity_category = EntityCategory.CONFIG
    _attr_should_poll = False

    def __init__(self, entry: EufyConfigEntry) -> None:
        self._entry = entry
        # The entry id, never the entry's unique id: that is the e-mail address.
        self._attr_unique_id = f"{entry.entry_id}_{REFRESH_DEVICE_LIST_KEY}"
        # Identifiers only: setup registers the account device and owns its row.
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})
        self._refreshing = False

    @override
    async def async_press(self) -> None:
        """Fetch the device list once; reload the entry when the station list changed.

        The fetch asks the cloud regions that listed devices before, or every region
        when the entry's region option is on; a region that listed none is otherwise
        not asked. Never retried. A changed paired-device list reaches the router as
        ``DevicesChanged`` during the fetch, a station built or no longer listed as
        ``StationsChanged``, and the router schedules the reload; a station added is
        also found here by comparing the served stations with the client's. Every
        device of the entry the list no longer names is removed at once, the station
        itself included. Only registries are touched after the
        fetch when a reload follows, so the reload cannot unload this entity mid-press;
        with no station before or after, the pending invitations are read for their
        repair.
        """
        runtime_data = self._entry.runtime_data
        router = runtime_data.router
        if self._refreshing or router.reload_pending:
            _LOGGER.debug(
                "Device list refresh skipped: %s",
                "a refresh is already running"
                if self._refreshing
                else "an entry reload is pending",
            )
            return
        self._refreshing = True
        before = set(runtime_data.coordinators)
        _LOGGER.debug("Device list refresh requested (%d station(s) served)", len(before))
        try:
            await runtime_data.eufy.async_discover(refresh=True)
        except EufySecurityError as err:
            _LOGGER.debug("Device list refresh failed: %s", type(err).__name__)
            raise errors.device_list_refresh_failed(self.hass, self._entry, err) from err
        finally:
            self._refreshing = False
        stale_devices.async_remove_unlisted(
            self.hass, self._entry, runtime.listed_serials(runtime_data.eufy)
        )
        errors.sync_skipped_devices_issues(
            self.hass, self._entry, runtime_data.eufy.skipped_devices
        )
        after = set(runtime_data.eufy.stations)
        if after != before:
            _LOGGER.debug(
                "Device list refreshed: %d station(s) added, %d gone",
                len(after - before),
                len(before - after),
            )
            router.async_reload_soon("station list changed")
        else:
            _LOGGER.debug("Device list refreshed: no station added")
            if not after and not router.reload_pending:
                # Still nothing: name any invitation the account has not accepted.
                # No reload is pending, so this entity outlives the await.
                errors.sync_pending_invites_issue(
                    self.hass, self._entry, await runtime.async_pending_invites(runtime_data.eufy)
                )
