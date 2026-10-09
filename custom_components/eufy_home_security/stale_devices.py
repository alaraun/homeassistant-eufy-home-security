"""The devices of an entry that the account's device list no longer names.

Every eufy device is registered under ``(DOMAIN, serial)``; the account's own device
under ``(DOMAIN, entry_id)``, which is never one of them.
"""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr

from eufy_home_security import redact_serial

from .const import DOMAIN
from .runtime import EufyConfigEntry, ListedDevices

_LOGGER = logging.getLogger(__name__)


def _serials(device: dr.DeviceEntry | dr.ChildDeviceEntry) -> set[str]:
    return {value for domain, value in device.identifiers if domain == DOMAIN}


@callback
def unlisted(
    registry: dr.DeviceRegistry, entry_id: str, device: dr.DeviceEntry, listed: ListedDevices
) -> bool:
    """Whether ``device`` is an eufy device of the entry that ``listed`` does not name.

    Never the account device, a device without an identifier of this integration, a
    device the list names but the client skipped (matched by redacted serial), or a
    device under a station another account serves (its paired devices are unknown).
    """
    serials = _serials(device)
    if not serials or entry_id in serials or serials & listed.serials:
        return False
    if any(redact_serial(serial) in listed.skipped for serial in serials):
        return False
    parent = registry.async_get(device.via_device_id) if device.via_device_id else None
    return parent is None or not _serials(parent) & listed.elsewhere


@callback
def async_remove_unlisted(
    hass: HomeAssistant, entry: EufyConfigEntry, listed: ListedDevices
) -> None:
    """Remove each device of the entry that ``listed`` does not name, with its entities.

    A list that names no device removes nothing: an account never loses every device
    at once by intent, and a wrong sign-in country lists none.
    """
    if not listed.serials:
        return
    registry = dr.async_get(hass)
    stale = [
        device
        for device in dr.async_entries_for_config_entry(registry, entry.entry_id)
        if unlisted(registry, entry.entry_id, device, listed)
    ]
    for device in stale:
        _LOGGER.info(
            "Removing device %s: no longer on the eufy device list",
            ", ".join(redact_serial(serial) for serial in sorted(_serials(device))),
        )
        registry.async_remove_device(device.id)
