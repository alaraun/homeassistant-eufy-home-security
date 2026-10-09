"""A preset slot's picture handed over by the card, stored as a preset capture's.

One websocket command, registered once per Home Assistant instance:

- ``eufy_home_security/preset_image`` ``{entity_id, preset, image}`` keeps ``image`` (a
  base64 JPEG the card drew from the playing live video) as slot ``preset``'s image of
  the camera ``entity_id``, exactly where a "Capture preset n" image goes
  (``PresetManager.async_store_image``: memory, still cache and small copy, the event
  history file, the image entity). Responds ``{preset}``. Nothing reaches the camera
  and nothing is decoded here beyond the small copy, so a capture holding the camera
  does not refuse it.

Refused, with nothing stored: an entity that is not a camera of a loaded entry of this
integration (``not_found``, ``unavailable``), a model without presets
(``presets_unsupported``), a slot the last read does not show set (``preset_not_set``),
and an image that is no base64 JPEG or exceeds ``PRESET_IMAGE_MAX_BYTES``
(``preset_image_invalid``). Logs carry the redacted serial, the slot and byte counts,
never the image.
"""

from __future__ import annotations

import base64
import binascii
import logging
from typing import TYPE_CHECKING, Any, Final

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.components.camera import DOMAIN as CAMERA_DOMAIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.util.hass_dict import HassKey

from eufy_home_security import Station, entity_unique_id, redact_serial

from . import detections, errors, presets
from .const import ATTR_PRESET, CAMERA_KEY, DOMAIN, PRESET_MAX_INDEX

if TYPE_CHECKING:
    from .runtime import EufyConfigEntry

_LOGGER = logging.getLogger(__name__)

WS_PRESET_IMAGE: Final = f"{DOMAIN}/preset_image"
# The largest picture kept, decoded. Its base64 text (4/3 of it) plus the message stays
# under aiohttp's 4 MiB websocket message limit, past which the connection is closed.
PRESET_IMAGE_MAX_BYTES: Final = 2 * 1024 * 1024
_MAX_BASE64_CHARS: Final = 4 * -(-PRESET_IMAGE_MAX_BYTES // 3)
_JPEG_SOI: Final = b"\xff\xd8"
ATTR_IMAGE: Final = "image"

ERR_NOT_FOUND: Final = "not_found"
ERR_UNAVAILABLE: Final = "unavailable"

_REGISTERED: HassKey[bool] = HassKey(f"{DOMAIN}_preset_upload")


class _Refused(Exception):
    """A request answered with an error code and a plain message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _camera(hass: HomeAssistant, entity_id: str) -> tuple[EufyConfigEntry, Station, str]:
    """The loaded entry, the station and the serial of this integration's camera ``entity_id``."""
    registered = er.async_get(hass).async_get(entity_id)
    if (
        registered is None
        or registered.platform != DOMAIN
        or registered.domain != CAMERA_DOMAIN
        or registered.config_entry_id is None
    ):
        raise _Refused(ERR_NOT_FOUND, "Not a camera of this integration")
    entry: EufyConfigEntry | None = hass.config_entries.async_get_entry(registered.config_entry_id)
    if entry is None or entry.state is not ConfigEntryState.LOADED:
        raise _Refused(ERR_UNAVAILABLE, "The camera's account is not loaded")
    for coordinator in entry.runtime_data.coordinators.values():
        for device_sn in detections.paired_device_kinds(coordinator.station):
            if entity_unique_id(device_sn, CAMERA_KEY) == registered.unique_id:
                return entry, coordinator.station, device_sn
    raise _Refused(ERR_NOT_FOUND, "Not a camera of this integration")


def _jpeg(text: str) -> bytes:
    """The JPEG ``text`` carries as base64; ``preset_image_invalid`` for anything else."""
    max_mb = PRESET_IMAGE_MAX_BYTES // (1024 * 1024)
    if len(text) > _MAX_BASE64_CHARS:
        raise errors.preset_image_invalid(max_mb)
    try:
        data = base64.b64decode(text, validate=True)
    except binascii.Error as err:
        raise errors.preset_image_invalid(max_mb) from err
    if not data.startswith(_JPEG_SOI) or len(data) > PRESET_IMAGE_MAX_BYTES:
        raise errors.preset_image_invalid(max_mb)
    return data


@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_PRESET_IMAGE,
        vol.Required(ATTR_ENTITY_ID): cv.entity_id,
        vol.Required(ATTR_PRESET): vol.All(vol.Coerce(int), vol.Range(min=0, max=PRESET_MAX_INDEX)),
        # A plain type check: a refused value is never echoed into the log.
        vol.Required(ATTR_IMAGE): str,
    }
)
@websocket_api.async_response
async def _ws_preset_image(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Keep the card's picture as a set slot's preset image."""
    index: int = msg[ATTR_PRESET]
    try:
        entry, station, device_sn = _camera(hass, msg[ATTR_ENTITY_ID])
        serial = redact_serial(device_sn)
        if not detections.has_preset_entities(device_sn):
            raise errors.presets_unsupported()
        if index not in presets.enabled_indexes(station, device_sn):
            _LOGGER.debug("Picture for preset %d of %s refused: slot not set", index, serial)
            raise errors.preset_not_set(index)
        jpeg = _jpeg(msg[ATTR_IMAGE])
        if not await entry.runtime_data.presets.async_store_image(device_sn, index, jpeg):
            raise _Refused(ERR_UNAVAILABLE, "The camera's account is unloading")
    except _Refused as err:
        connection.send_error(msg["id"], err.code, str(err))
        return
    except HomeAssistantError as err:
        _LOGGER.debug(
            "Picture for preset %d refused: %s (%d base64 chars)",
            index,
            err.translation_key,
            len(msg[ATTR_IMAGE]),
        )
        connection.send_error(
            msg["id"],
            websocket_api.ERR_HOME_ASSISTANT_ERROR,
            str(err),
            translation_key=err.translation_key,
            translation_domain=err.translation_domain,
            translation_placeholders=err.translation_placeholders,
        )
        return
    connection.send_result(msg["id"], {ATTR_PRESET: index})


@callback
def async_setup(hass: HomeAssistant) -> None:
    """Register the command once per Home Assistant instance."""
    if hass.data.get(_REGISTERED):
        return
    hass.data[_REGISTERED] = True
    websocket_api.async_register_command(hass, _ws_preset_image)
