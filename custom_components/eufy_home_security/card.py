"""The bundled Lovelace cards: served by the integration and registered as dashboard resources.

Each card file lives in ``frontend/`` beside this module and is served at its URL:
``CARD_URL`` (``/eufy_home_security/eufy-camera-card.js``) and ``HOMEBASE_CARD_URL``
(``/eufy_home_security/eufy-homebase-card.js``). In storage-mode resources (the default)
the integration keeps exactly one Lovelace resource per card, ``<url>?v=<hash>``, whose
query is the file's content hash, so every change to a file makes browsers fetch it
again. In YAML resource mode nothing is registered: the user adds each URL to
``lovelace: resources:`` once.

The paths are registered once per Home Assistant run (``async_setup``); the resources
are checked at start-up and again at every config entry setup, so a changed card file
takes effect on an entry reload. A failure is logged and never fails the integration.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from homeassistant.components.http import StaticPathConfig
from homeassistant.components.lovelace.const import LOVELACE_DATA
from homeassistant.components.lovelace.resources import ResourceStorageCollection
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, Event, HomeAssistant
from homeassistant.util.hass_dict import HassKey

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

_FRONTEND: Final = Path(__file__).parent / "frontend"
CARD_FILE: Final = _FRONTEND / "eufy-camera-card.js"
CARD_URL: Final = f"/{DOMAIN}/eufy-camera-card.js"
HOMEBASE_CARD_FILE: Final = _FRONTEND / "eufy-homebase-card.js"
HOMEBASE_CARD_URL: Final = f"/{DOMAIN}/eufy-homebase-card.js"
_RESOURCE_TYPE: Final = "module"
_SERVED: HassKey[bool] = HassKey(f"{DOMAIN}_card_served")


@dataclass(frozen=True, slots=True)
class BundledCard:
    """One card file, the URL it is served at, and how log lines name it."""

    file: Path
    url: str
    label: str


CARDS: Final = (
    BundledCard(CARD_FILE, CARD_URL, "camera card"),
    BundledCard(HOMEBASE_CARD_FILE, HOMEBASE_CARD_URL, "HomeBase card"),
)


def card_hash(path: Path = CARD_FILE) -> str:
    """The first 8 hex digits of a card file's SHA-256: the resource URL's cache key."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:8]


def _path_of(url: str) -> str:
    return url.split("?", 1)[0]


def _storage_resources(hass: HomeAssistant) -> ResourceStorageCollection | None:
    """The dashboard resources when they are in storage mode, else None."""
    lovelace = hass.data.get(LOVELACE_DATA)
    resources = None if lovelace is None else lovelace.resources
    return resources if isinstance(resources, ResourceStorageCollection) else None


async def async_setup_card(hass: HomeAssistant) -> None:
    """Serve the card files (once per run) and sync their resources once HA runs."""
    if not hass.data.get(_SERVED):
        try:
            await hass.http.async_register_static_paths(
                [StaticPathConfig(card.url, str(card.file), cache_headers=False) for card in CARDS]
            )
        except RuntimeError:
            _LOGGER.warning("Could not serve the bundled cards", exc_info=True)
            return
        hass.data[_SERVED] = True

    if hass.state is CoreState.running:
        await async_sync_card_resource(hass)
        return

    async def _on_started(_event: Event) -> None:
        await async_sync_card_resource(hass)

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _on_started)


async def async_sync_card_resource(hass: HomeAssistant) -> None:
    """Point each card's resource at its current file hash.

    Only storage-mode resources are written, and only when the URL differs. Every
    resource of a card past the first is deleted, so each element is defined once.
    """
    resources = _storage_resources(hass)
    if resources is None:
        _LOGGER.debug(
            "Lovelace resources are not in storage mode; add %s by hand",
            ", ".join(card.url for card in CARDS),
        )
        return
    for card in CARDS:
        try:
            url = f"{card.url}?v={await hass.async_add_executor_job(card_hash, card.file)}"
            await resources.async_get_info()  # loads the collection
            ours = [item for item in resources.async_items() if _path_of(item["url"]) == card.url]
            if not ours:
                await resources.async_create_item({"res_type": _RESOURCE_TYPE, "url": url})
                _LOGGER.info("Registered the %s resource %s", card.label, url)
                continue
            first, *extra = ours
            if first["url"] != url or first.get("type") != _RESOURCE_TYPE:
                await resources.async_update_item(
                    first["id"], {"res_type": _RESOURCE_TYPE, "url": url}
                )
                _LOGGER.info("%s resource %s -> %s", card.label.capitalize(), first["url"], url)
            for item in extra:
                await resources.async_delete_item(item["id"])
                _LOGGER.info("Removed the duplicate %s resource %s", card.label, item["url"])
        except Exception:  # an optional dashboard resource never fails the integration
            _LOGGER.warning("Could not register the %s resource", card.label, exc_info=True)


async def async_remove_card_resource(hass: HomeAssistant) -> None:
    """Delete every card's storage-mode resource; for the last config entry's removal.

    Without an entry the integration does not load, so the URLs would answer 404 on
    every dashboard load after the next restart.
    """
    resources = _storage_resources(hass)
    if resources is None:
        return
    urls = {card.url: card.label for card in CARDS}
    try:
        await resources.async_get_info()
        for item in list(resources.async_items()):
            if (label := urls.get(_path_of(item["url"]))) is not None:
                await resources.async_delete_item(item["id"])
                _LOGGER.info("Removed the %s resource %s", label, item["url"])
    except Exception:  # an optional dashboard resource never fails the removal
        _LOGGER.warning("Could not remove the card resources", exc_info=True)
