"""Image entities: a camera's latest detection image, and one per preset slot.

**Last event** (``image.<camera>_last_event``): the picture the camera entity shows, as
an image entity for ``picture-entity`` cards and automations. It follows the snapshot
manager's cache (``snapshots.py``): a detection's thumbnail then its HD frame, a
refreshed event, a live capture. The state is the time the image was stored; the
attributes are ``image_source`` and ``triggered_at`` as on the camera entity. Before
the first image the state is unknown. Serving it never fetches from the station.

**Preset images**: each entity shows the live view the camera took when last turned to
its slot, from the preset manager's cache (``presets.py``): a "Capture preset n" press
or the ``capture_preset`` action fills it. Before the first capture the entity has no
image and its state is unknown. A view serves what is cached and never waits on the
station.

Preset entities are built from the slots the station holds in its cache; nothing here
wakes a camera to learn them. A camera never read gets no preset image entity until its
first "Refresh presets" press (or any capture) reads the slots, at which point
``PresetsChanged`` adds them without a restart. A slot the latest read shows
disabled makes its entity unavailable; nothing is ever removed.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, override

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from eufy_home_security import redact_serial

from . import detections, presets, small_images, snapshots
from .const import ATTR_PRESET_INDEX, LAST_EVENT_IMAGE_KEY, PRESET_IMAGE_KEY
from .coordinator import StationCoordinator
from .entity import EufyDeviceEntity, EufyPushAvailability
from .presets import PresetManager
from .snapshots import SnapshotManager

if TYPE_CHECKING:
    from .runtime import EufyConfigEntry

_LOGGER = logging.getLogger(__name__)

# Images come from the managers' caches, so HA need not serialise entity updates.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EufyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a last-event image per camera, and a preset image per enabled cached slot of
    every pan/tilt camera."""
    manager = entry.runtime_data.presets
    stills = entry.runtime_data.snapshots
    async_add_entities(
        EufyLastEventImage(hass, coordinator, device_sn, stills)
        for coordinator in entry.runtime_data.coordinators.values()
        for device_sn, kind in detections.paired_device_kinds(coordinator.station).items()
        if detections.has_detection_entities(kind)
    )
    for coordinator in entry.runtime_data.coordinators.values():
        for device_sn in detections.paired_device_kinds(coordinator.station):
            if not detections.has_preset_entities(coordinator.station, device_sn):
                continue
            _add_camera_slots(hass, entry, coordinator, device_sn, manager, async_add_entities)


def _add_camera_slots(
    hass: HomeAssistant,
    entry: EufyConfigEntry,
    coordinator: StationCoordinator,
    device_sn: str,
    manager: PresetManager,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Bind one camera's slot adder, now and on every later slot change."""
    station = coordinator.station
    if station.presets(device_sn) is None:
        _LOGGER.debug(
            "Presets of %s unknown: press Refresh presets or capture a live image",
            redact_serial(device_sn),
        )

    def _add(index: int) -> None:
        async_add_entities([EufyPresetImage(hass, coordinator, device_sn, index, manager)])

    presets.async_add_slot_entities(hass, entry, station, device_sn, _add)


class EufyLastEventImage(
    EufyPushAvailability, EufyDeviceEntity, small_images.SmallImageSource, ImageEntity
):
    """The camera's shown detection image as an image entity; available while the
    camera is.

    Its ``entity_picture`` is the small copy (``small_images.py``); ``image_proxy``
    serves the full image.
    """

    _attr_translation_key = LAST_EVENT_IMAGE_KEY

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: StationCoordinator,
        device_sn: str,
        manager: SnapshotManager,
    ) -> None:
        super().__init__(coordinator, device_sn, LAST_EVENT_IMAGE_KEY)
        ImageEntity.__init__(self, hass)
        self._serial = device_sn
        self._manager = manager

    @override
    async def async_added_to_hass(self) -> None:
        """Follow the camera's image changes until removed."""
        await super().async_added_to_hass()
        self._attr_image_last_updated = self._manager.stored_time_for(self._serial)
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                snapshots.image_signal(self.coordinator.config_entry.entry_id, self._serial),
                self._async_on_image,
            )
        )

    @callback
    def _async_on_image(self) -> None:
        """A new image: stamp its store time and write the state, so the frontend
        fetches it."""
        self._attr_image_last_updated = self._manager.stored_time_for(self._serial)
        self.async_write_ha_state()

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """``image_source`` and ``triggered_at`` of the shown image; none before one."""
        return self._manager.shown_image_attributes(self._serial)

    @override
    async def async_image(self) -> bytes | None:
        """The cached JPEG; None before the first image. Never fetches."""
        return self._manager.image_for(self._serial)

    @property
    @override
    def entity_picture(self) -> str | None:
        """The small copy's URL at the current image's version, with the access token."""
        if self._attr_entity_picture is not None:
            return self._attr_entity_picture
        version = self.small_image_version
        if version is None:
            return None
        return small_images.small_image_url(self.entity_id, version, self.access_tokens[-1])

    @property
    @override
    def small_image_version(self) -> str | None:
        """The shown image's store time; None before one."""
        stored = self._manager.stored_time_for(self._serial)
        return None if stored is None else str(int(stored.timestamp() * 1000))

    @override
    async def async_full_image(self) -> bytes | None:
        """The cached JPEG, as ``async_image``."""
        return await self.async_image()

    @override
    async def async_small_image(self) -> bytes | None:
        """The small copy of the cached JPEG."""
        return await self._manager.small_image_for(self._serial)


class EufyPresetImage(
    EufyPushAvailability, EufyDeviceEntity, small_images.SmallImageSource, ImageEntity
):
    """The live view of one preset slot, as last captured; available while the slot is set.

    Its ``entity_picture`` is the small copy (``small_images.py``); ``image_proxy``
    serves the full image.
    """

    _attr_translation_key = PRESET_IMAGE_KEY

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: StationCoordinator,
        device_sn: str,
        index: int,
        manager: PresetManager,
    ) -> None:
        super().__init__(coordinator, device_sn, presets.preset_image_key(index))
        ImageEntity.__init__(self, hass)
        self._attr_translation_placeholders = {"index": str(index)}
        # The slot index only, never a path.
        self._attr_extra_state_attributes = {ATTR_PRESET_INDEX: index}
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
        """Follow this slot's image and the camera's slot changes until removed."""
        await super().async_added_to_hass()
        entry_id = self.coordinator.config_entry.entry_id
        self._take_image_time()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                presets.preset_image_signal(entry_id, self._serial, self._index),
                self._async_on_image,
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                presets.presets_signal(entry_id, self._serial),
                self._async_on_presets,
            )
        )

    def _take_image_time(self) -> datetime | None:
        """Stamp the entity with the cached image's time; None once the manager holds none."""
        cached = self._manager.image_for(self._serial, self._index)
        self._attr_image_last_updated = cached[1] if cached is not None else None
        return self._attr_image_last_updated

    @callback
    def _async_on_image(self) -> None:
        """A new or dropped image: stamp its time and write the state, so the frontend
        fetches it (or shows none)."""
        self._take_image_time()
        self.async_write_ha_state()

    @callback
    def _async_on_presets(self) -> None:
        """The slots changed: availability re-reads them."""
        self.async_write_ha_state()

    @override
    async def async_image(self) -> bytes | None:
        """The cached JPEG; None before the first capture. Never fetches."""
        cached = self._manager.image_for(self._serial, self._index)
        return cached[0] if cached is not None else None

    @property
    @override
    def entity_picture(self) -> str | None:
        """The small copy's URL at the current image's version, with the access token."""
        if self._attr_entity_picture is not None:
            return self._attr_entity_picture
        version = self.small_image_version
        if version is None:
            return None
        return small_images.small_image_url(self.entity_id, version, self.access_tokens[-1])

    @property
    @override
    def small_image_version(self) -> str | None:
        """The image's capture time; None before the first capture."""
        cached = self._manager.image_for(self._serial, self._index)
        return None if cached is None else str(int(cached[1].timestamp() * 1000))

    @override
    async def async_full_image(self) -> bytes | None:
        """The cached JPEG, as ``async_image``."""
        return await self.async_image()

    @override
    async def async_small_image(self) -> bytes | None:
        """The small copy of the cached JPEG."""
        return await self._manager.small_image_for(self._serial, self._index)
