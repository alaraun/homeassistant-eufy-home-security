"""A station's storage record (disk and eMMC), read on its own slow schedule.

The record comes from ``Station.async_get_storage()`` and is the library's
``StorageInfo``: every figure and unit is the library's, and this module only
decides when to read it and which entities show it.

**Its own coordinator.** The record is not part of the parameter dump the station
coordinator polls every 45 s, so it gets a coordinator of its own, one per station,
with its own 30-minute timer. The two timers never touch each other: a storage read,
a storage failure or a pushed record changes nothing about the guard-mode poll, and a
dump changes nothing about the storage schedule.

**Pushes.** The station pushes a new record whenever a format finishes and whenever
another client reads it; the library announces each as ``StorageChanged``, and the
event router hands it to :meth:`StorageCoordinator.async_apply_storage`. Like the
station coordinator's pushes, that replaces ``data`` and notifies the listeners
without the coordinator's set-updated-data method, which would cancel and reschedule
this coordinator's timer on every push.

**Entities appear with the part they show.** Whether a station has a disk is known
only once a record arrives, and a station that was down at setup has none yet, so the
platforms add the disk entities when the first record naming a disk arrives, and the
eMMC entities likewise (:func:`async_add_part_entities`), rather than deciding once
at platform setup.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, override

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator

from eufy_home_security import (
    EufySecurityError,
    Station,
    StorageInfo,
    StorageMedium,
    entity_unique_id,
    redact_serial,
)

from . import errors
from .const import DOMAIN, STORAGE_POLL_INTERVAL_SECONDS

if TYPE_CHECKING:
    from .runtime import EufyConfigEntry

_LOGGER = logging.getLogger(__name__)


class StoragePart(StrEnum):
    """A part of the storage record that entities are built for.

    Each part is one of the record's media, and every medium has the library's one
    ``StorageMedium`` shape, so an entity description reads its figure off
    :meth:`medium` the same way for every part.
    """

    DISK = "disk"
    EMMC = "emmc"

    def medium(self, storage: StorageInfo | None) -> StorageMedium | None:
        """This part's medium in ``storage``; None when the record lacks it or is None."""
        if storage is None:
            return None
        if self is StoragePart.DISK:
            return storage.disk
        return storage.emmc

    def present(self, storage: StorageInfo | None) -> bool:
        """Whether ``storage`` reports this part."""
        return self.medium(storage) is not None


class StorageCoordinator(DataUpdateCoordinator[StorageInfo]):
    """A station's latest ``StorageInfo``, read every 30 minutes and on every push."""

    config_entry: EufyConfigEntry

    def __init__(self, hass: HomeAssistant, entry: EufyConfigEntry, station: Station) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            # Named in every failed-read log line: never the full serial.
            name=f"{DOMAIN} storage {redact_serial(station.serial)}",
            update_interval=timedelta(seconds=STORAGE_POLL_INTERVAL_SECONDS),
            # A read that returned the record already shown notifies no entity.
            always_update=False,
        )
        self.station = station

    @override
    async def _async_update_data(self) -> StorageInfo:
        """One storage read; every library error becomes ``UpdateFailed``.

        The coordinator logs the first failure once and the recovery once, as the
        station poll does, and marks only this coordinator's entities unavailable.
        """
        try:
            return await self.station.async_get_storage()
        except EufySecurityError as err:
            raise errors.update_failed(err) from err

    @callback
    def async_apply_storage(self, storage: StorageInfo) -> None:
        """Show a record the station pushed, without touching this coordinator's timer.

        A pushed record is an application-level answer from the station, so it also
        ends a failed read's unavailability. A record equal to the one shown, while
        shown as available, writes no entity state.
        """
        if self.last_update_success and storage == self.data:
            return
        self.data = storage
        self.last_update_success = True
        self.last_exception = None
        self.async_update_listeners()


@callback
def async_add_part_entities(
    entry: EufyConfigEntry,
    coordinator: StorageCoordinator,
    add_part: Callable[[StoragePart], None],
) -> None:
    """Call ``add_part`` once per part, as soon as a record reports that part.

    Checked now and on every record after, so a station that was down at setup gains
    its entities with its first record. A part that disappears later keeps its
    entities, which go unavailable. The listener also keeps the coordinator's timer
    running for as long as the entry is loaded.
    """
    added: set[StoragePart] = set()

    @callback
    def _check() -> None:
        data: StorageInfo | None = coordinator.data
        for part in StoragePart:
            if part not in added and part.present(data):
                added.add(part)
                add_part(part)

    _check()
    entry.async_on_unload(coordinator.async_add_listener(_check))


class EufyStorageEntity(CoordinatorEntity[StorageCoordinator]):
    """An entity of the station showing one part of its storage record."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: StorageCoordinator, part: StoragePart, key: str) -> None:
        super().__init__(coordinator)
        serial = coordinator.station.serial
        self._part = part
        self._attr_unique_id = entity_unique_id(serial, key)
        # Identifiers only: the station's device row has one writer, entry setup.
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, serial)})

    @property
    def storage(self) -> StorageInfo | None:
        """The latest record; None before the first one."""
        data: StorageInfo | None = self.coordinator.data
        return data

    @property
    @override
    def available(self) -> bool:
        """Available while the last read succeeded and the record reports this part."""
        return super().available and self._part.present(self.storage)
