"""The config entry's diagnostics download: session health that identifies nobody.

Built only from the library's redacted views and figures (cache summary,
``as_redacted_dict()``, ``redact_serial``, ``SessionStats``, the account report); the
account store, the entry's data and its title (the e-mail) are never read. Storage
media serials and labels, format request ids and a ``LanPath`` other than its warnings
are left out, and the payload goes through ``async_redact_data`` for device names.
The account report sends a few cloud requests on the held sessions, never a login.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, Final

from homeassistant.components.diagnostics import async_redact_data

from eufy_home_security import EufySecurityError, Station, StorageInfo, redact_serial

from . import detections

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .runtime import EufyConfigEntry, EufyRuntimeData

TO_REDACT: Final = frozenset({"name"})
# What a storage record carries that identifies a disk or a request, never downloaded.
_STORAGE_OMITTED: Final = ("format_transaction",)
_MEDIUM_OMITTED: Final = ("serial", "label")


def _storage_figures(storage: StorageInfo | None) -> dict[str, Any] | None:
    """The storage record's figures, without the fields that identify a medium."""
    if storage is None:
        return None
    figures = dataclasses.asdict(storage)
    for key in _STORAGE_OMITTED:
        figures.pop(key, None)
    for part in ("disk", "external", "emmc"):
        medium = figures.get(part)
        if medium is not None:
            for key in _MEDIUM_OMITTED:
                medium.pop(key, None)
    return figures


def _preset_figures(station: Station) -> dict[str, dict[str, Any] | None]:
    """Each pan/tilt camera's preset slots by redacted label: count and enabled indexes.

    None for a camera whose slots were never read (the station holds none, and
    nothing here reads them). Indexes and a count only: no zoom, no default flag.
    """
    figures: dict[str, dict[str, Any] | None] = {}
    for device in station.devices:
        serial = device.device_sn
        if not serial or not detections.has_preset_entities(serial):
            continue
        slots = station.presets(serial)
        figures[redact_serial(serial)] = (
            None
            if slots is None
            else {
                "count": len(slots),
                "enabled": sorted(slot.index for slot in slots if slot.enabled),
            }
        )
    return figures


def _station_labels(cached: Iterable[str], served: Iterable[str]) -> dict[str, str]:
    """Each serial's label, numbered ``#2``, ``#3`` … where two would collide.

    ``redact_serial`` keeps only a serial's model prefix and last four characters, so
    two stations of one model can share a label. The cache summary numbers its own
    labels over the cache's serials, sorted. The cached serials are labelled first,
    in that order and by that loop, so each gets the very label it has under
    ``cache.stations``, whatever order this entry serves them in. A served serial
    the cache does not hold is labelled after them, with a label no cached serial
    has, so it can never be taken for one.
    """
    ordered = sorted(set(cached))
    ordered += sorted(set(served).difference(ordered))
    labels: dict[str, str] = {}
    taken: set[str] = set()
    for serial in ordered:
        label, n = redact_serial(serial), 1
        while label in taken:
            n += 1
            label = f"{redact_serial(serial)}#{n}"
        taken.add(label)
        labels[serial] = label
    return labels


async def _account_report(data: EufyRuntimeData) -> dict[str, Any]:
    """The library's account report, or the type of the error that stopped it.

    The library's message text is never copied here.
    """
    try:
        report = await data.eufy.async_account_report()
    except EufySecurityError as err:
        return {"error": type(err).__name__}
    return report.as_dict()


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: EufyConfigEntry
) -> dict[str, Any]:
    """The entry's download: the library's cache view and each served station's health."""
    del hass
    data = entry.runtime_data
    eufy = data.eufy
    dedupe = eufy.deduplicator
    cache = await eufy.async_cache_summary()
    # Read with no await after the summary, so both label the same cached serials.
    labels = _station_labels(
        eufy.cache.station_serials(),
        (coordinator.station.serial for coordinator in data.coordinators.values()),
    )
    stations: dict[str, Any] = {}
    for coordinator in data.coordinators.values():
        station = coordinator.station
        stations[labels[station.serial]] = {
            "model": station.model.model if station.model is not None else None,
            "connected": station.connected,
            "last_error": (
                type(station.last_error).__name__ if station.last_error is not None else None
            ),
            "lan_path_warnings": [warning.value for warning in station.lan_path.warnings],
            "standalone": station.is_standalone,
            "on_demand": station.connects_on_demand,
            "sub_devices": [device.as_redacted_dict() for device in station.sub_devices],
            # The paired list, preceded by a standalone station's own device view.
            "devices": [device.as_redacted_dict() for device in station.devices],
            "max_sessions": station.max_sessions,
            "session": dataclasses.asdict(station.stats()),
            "events_received_by_cipher": data.router.events_received_by_cipher(station.serial),
            "storage": _storage_figures(station.storage),
            "presets": _preset_figures(station),
            "recording_sync": (
                data.recordings.stats(station.serial) if data.recordings is not None else None
            ),
        }
    # Asked after every station block, so the session figures above precede its requests.
    account_report = await _account_report(data)
    return async_redact_data(
        {
            "cache": cache,
            "stations": stations,
            "stations_served_elsewhere": [
                device.as_redacted_dict() for device in eufy.stations_served_elsewhere
            ],
            "skipped_devices": [dataclasses.asdict(device) for device in eufy.skipped_devices],
            "models": [
                {**dataclasses.asdict(status), "newer_vendor_data": status.newer_vendor_data}
                for status in eufy.model_status()
            ],
            "push": {
                "running": eufy.push_running,
                # The type only: the library's message text is never copied here.
                "error": type(eufy.push_error).__name__ if eufy.push_error is not None else None,
            },
            "deduplicator": (
                None
                if dedupe is None
                else {
                    "dropped_duplicates": dedupe.dropped_duplicates,
                    "dropped_repeats": dedupe.dropped_repeats,
                }
            ),
            "station_recordings": (
                data.station_recordings.diagnostics()
                if data.station_recordings is not None
                else None
            ),
            "options": dict(entry.options),
            "account_report": account_report,
        },
        TO_REDACT,
    )
