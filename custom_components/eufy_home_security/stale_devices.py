"""The devices of an entry that the account's device list no longer names.

Every eufy device is registered under ``(DOMAIN, serial)``; the account's own device
under ``(DOMAIN, entry_id)``, which is never one of them.
"""

from __future__ import annotations

import logging
from collections.abc import Set as AbstractSet

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr

from eufy_home_security import redact_serial

from .const import DOMAIN
from .runtime import EufyConfigEntry

_LOGGER = logging.getLogger(__name__)


def _serials(device: dr.DeviceEntry) -> set[str]:
    return {value for domain, value in device.identifiers if domain == DOMAIN}


@callback
def unlisted(entry_id: str, device: dr.DeviceEntry, listed: AbstractSet[str]) -> bool:
    """Whether ``device`` is an eufy device of the entry that ``listed`` does not name.

    Never the account device, a device without an identifier of this integration, or
    a device of another config entry (a device belongs to one entry).
    """
    serials = _serials(device)
    if not serials or entry_id in serials or serials & listed:
        return False
    return device.config_entry_id == entry_id


@callback
def async_remove_unlisted(
    hass: HomeAssistant, entry: EufyConfigEntry, listed: AbstractSet[str]
) -> None:
    """Remove each device of the entry that ``listed`` does not name, with its entities.

    A list that names no device removes nothing: an account never loses every device
    at once by intent, and a wrong sign-in country lists none.
    """
    if not listed:
        return
    registry = dr.async_get(hass)
    stale = [
        device
        for device in dr.async_entries_for_config_entry(registry, entry.entry_id)
        if unlisted(entry.entry_id, device, listed)
    ]
    for device in stale:
        _LOGGER.info(
            "Removing device %s: no longer on the eufy device list",
            ", ".join(redact_serial(serial) for serial in sorted(_serials(device))),
        )
        registry.async_remove_device(device.id)
