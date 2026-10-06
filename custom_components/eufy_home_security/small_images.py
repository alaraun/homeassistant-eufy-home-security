"""Small copies of the camera and preset images, for tiles, buttons and entity badges.

Home Assistant keeps size variants of an uploaded picture next to the original and
serves them with long cache headers (``image_upload``); this does the same for the
integration's own stills. Every still has a small copy in the still cache
(``still_cache.py``), at most ``SMALL_WIDTH`` pixels wide, made when the still is
stored. It is served here:

    /api/eufy_home_security/image/<entity id>/small?v=<version>[&token=<access token>]

- **Authentication as Home Assistant's own image views.** A request with a valid login
  (``Authorization`` header, a signed path) or the entity's current access token, the
  one in its ``entity_picture``, is served; an unknown entity is 404 to a logged-in
  request and 401 to others; a wrong token is 401 with an ``Authorization`` header,
  else 403.
- **Cacheable by version.** ``v`` is the image's version (``SmallImageSource.small_image_version``:
  a camera's ``image_updated``, a preset image's state). When it matches the image now
  shown, the response may be kept for a year; any other ``v`` is served the current
  image uncached. A client that fetches by a stable URL with the ``Authorization``
  header (``hass.fetchWithAuth``) therefore loads each image once.
- **A still that does not decode** as a JPEG has no small copy; its full image is
  served instead. An entity with no image answers 404.
- **Camera entities** also follow Home Assistant's own sized request: a
  ``camera_proxy`` request whose ``width`` and ``height`` fit the small copy is
  answered with it (``EufyCamera.async_camera_image``).
"""

from __future__ import annotations

import collections
import logging
from abc import abstractmethod
from http import HTTPStatus
from typing import Final

from aiohttp import hdrs, web
from homeassistant.components.camera.const import DATA_COMPONENT as CAMERA_COMPONENT
from homeassistant.components.image.const import DATA_COMPONENT as IMAGE_COMPONENT
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.http import KEY_AUTHENTICATED, KEY_HASS, HomeAssistantView
from homeassistant.util.hass_dict import HassKey

from .const import DOMAIN
from .still_cache import SMALL_WIDTH

_LOGGER = logging.getLogger(__name__)

SMALL_IMAGE_URL: Final = "/api/" + DOMAIN + "/image/{entity_id}/small"
SMALL_IMAGE_VIEW_NAME: Final = f"api:{DOMAIN}:small_image"
# The largest sized camera_proxy request the small copy answers (16:9).
SMALL_HEIGHT: Final = SMALL_WIDTH * 9 // 16
# How long a response for the current version may be kept: a year, as a new still
# gets a new version and therefore a new URL.
CACHE_SECONDS: Final = 365 * 24 * 3600
_VIEW_REGISTERED: HassKey[bool] = HassKey(f"{DOMAIN}_small_image_view")


class SmallImageSource:
    """An entity whose shown image has a small copy; the view serves only these."""

    # Set by the Camera and ImageEntity base classes: the current and previous token.
    access_tokens: collections.deque[str]

    @property
    @abstractmethod
    def small_image_version(self) -> str | None:
        """The shown image's version; None before the entity has an image."""

    @abstractmethod
    async def async_full_image(self) -> bytes | None:
        """The shown image at full size; None before the entity has one."""

    @abstractmethod
    async def async_small_image(self) -> bytes | None:
        """The small copy of the shown image; None when it has none."""


def small_image_url(entity_id: str, version: str, token: str | None = None) -> str:
    """The path of an entity's small image at ``version``, with ``token`` when given."""
    path = SMALL_IMAGE_URL.format(entity_id=entity_id)
    query = f"v={version}" if token is None else f"v={version}&token={token}"
    return f"{path}?{query}"


def fits_small(width: int | None, height: int | None) -> bool:
    """Whether a sized image request is answered by the small copy."""
    return (
        width is not None
        and height is not None
        and 0 < width <= SMALL_WIDTH
        and 0 < height <= SMALL_HEIGHT
    )


class SmallImageView(HomeAssistantView):
    """Serves the small copy of one camera or preset image of this integration."""

    url = SMALL_IMAGE_URL
    name = SMALL_IMAGE_VIEW_NAME
    # An <img> cannot send the Authorization header, so the entity's access token is
    # accepted too, as Home Assistant's image_proxy and camera_proxy do.
    requires_auth = False

    async def get(self, request: web.Request, entity_id: str) -> web.Response:
        """Serve the small copy, or the full image when the still has none."""
        hass = request.app[KEY_HASS]
        entity = _find(hass, entity_id)
        if entity is None:
            raise web.HTTPNotFound if request[KEY_AUTHENTICATED] else web.HTTPUnauthorized
        if not (request[KEY_AUTHENTICATED] or request.query.get("token") in entity.access_tokens):
            if hdrs.AUTHORIZATION in request.headers:
                raise web.HTTPUnauthorized
            raise web.HTTPForbidden
        version = entity.small_image_version
        body = await entity.async_small_image()
        if body is None:
            body = await entity.async_full_image()
        if body is None or version is None:
            return web.Response(status=HTTPStatus.NOT_FOUND)
        current = request.query.get("v") == version
        return web.Response(
            body=body,
            content_type="image/jpeg",
            headers={
                hdrs.CACHE_CONTROL: (
                    f"private, max-age={CACHE_SECONDS}, immutable" if current else "no-cache"
                )
            },
        )


def _find(hass: HomeAssistant, entity_id: str) -> SmallImageSource | None:
    """The integration's camera or preset image entity ``entity_id``; None otherwise."""
    entity: object = None
    if entity_id.startswith("camera.") and (cameras := hass.data.get(CAMERA_COMPONENT)):
        entity = cameras.get_entity(entity_id)
    elif entity_id.startswith("image.") and (images := hass.data.get(IMAGE_COMPONENT)):
        entity = images.get_entity(entity_id)
    return entity if isinstance(entity, SmallImageSource) else None


@callback
def async_register_view(hass: HomeAssistant) -> None:
    """Register the view once per Home Assistant instance (views have no unregister)."""
    if hass.data.get(_VIEW_REGISTERED):
        return
    hass.data[_VIEW_REGISTERED] = True
    hass.http.register_view(SmallImageView())
    _LOGGER.debug("The small image view is registered for this Home Assistant instance")
