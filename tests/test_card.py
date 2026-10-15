"""The bundled dashboard cards: served by the integration, one storage-mode resource per card
kept current."""

from __future__ import annotations

from collections.abc import Callable
from http import HTTPStatus
from pathlib import Path
from typing import Any

import pytest
from conftest import set_up_warm
from eufy_home_security.testing import FakeStation
from homeassistant.components.lovelace.const import LOVELACE_DATA
from homeassistant.components.lovelace.resources import ResourceStorageCollection
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.eufy_home_security import card
from custom_components.eufy_home_security.const import DOMAIN

_RESOURCES_KEY = "lovelace_resources"


def _stored_resources(urls: list[str]) -> dict[str, Any]:
    return {
        "version": 1,
        "minor_version": 1,
        "key": _RESOURCES_KEY,
        "data": {
            "items": [{"id": f"r{i}", "url": u, "type": "module"} for i, u in enumerate(urls)]
        },
    }


async def _resource_urls(hass: HomeAssistant) -> list[str]:
    resources = hass.data[LOVELACE_DATA].resources
    assert isinstance(resources, ResourceStorageCollection)
    await resources.async_get_info()
    return [item["url"] for item in resources.async_items()]


def _current() -> list[str]:
    """Each bundled card's resource URL at its file's hash, in ``card.CARDS`` order."""
    return [f"{c.url}?v={card.card_hash(c.file)}" for c in card.CARDS]


async def _set_up(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()


def test_the_bundled_cards_are_shipped_and_name_their_custom_elements() -> None:
    """Both files exist in the package and define the elements dashboards reference."""
    source = card.CARD_FILE.read_text(encoding="utf-8")
    assert "customElements.define('eufy-camera-card'" in source
    assert len(card.card_hash()) == 8
    homebase = card.HOMEBASE_CARD_FILE.read_text(encoding="utf-8")
    assert "customElements.define('eufy-homebase-card'" in homebase
    assert len(card.card_hash(card.HOMEBASE_CARD_FILE)) == 8
    assert [c.url for c in card.CARDS] == [card.CARD_URL, card.HOMEBASE_CARD_URL]


async def test_the_card_resource_is_created_with_the_file_hash(
    hass: HomeAssistant, hass_client: ClientSessionGenerator
) -> None:
    """No resource yet: one module resource per card at its URL, cache key = the file hash."""
    await _set_up(hass)

    assert await _resource_urls(hass) == _current()
    client = await hass_client()
    for bundled in card.CARDS:
        response = await client.get(bundled.url)
        assert response.status == HTTPStatus.OK
        assert await response.read() == bundled.file.read_bytes()


@pytest.mark.parametrize(
    "stored",
    [
        [f"{card.CARD_URL}?v=00000000"],
        [f"{card.CARD_URL}?v=00000000", f"{card.CARD_URL}?v=11111111"],
    ],
    ids=["stale-hash", "duplicate"],
)
async def test_an_existing_card_resource_is_repointed_and_duplicates_removed(
    hass: HomeAssistant, hass_storage: dict[str, Any], stored: list[str]
) -> None:
    """A stale resource becomes the current URL, a second one is deleted; others are left alone."""
    other = "/hacsfiles/other-card/other-card.js"
    hass_storage[_RESOURCES_KEY] = _stored_resources([other, *stored])

    await _set_up(hass)

    assert await _resource_urls(hass) == [other, *_current()]


async def test_a_current_resource_is_left_untouched(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Every card already at its current hash: no write."""
    hass_storage[_RESOURCES_KEY] = _stored_resources(_current())
    await _set_up(hass)
    resources = hass.data[LOVELACE_DATA].resources
    assert isinstance(resources, ResourceStorageCollection)
    assert [item["id"] for item in resources.async_items()] == ["r0", "r1"]
    assert await _resource_urls(hass) == _current()


async def test_yaml_resource_mode_registers_nothing_but_still_serves_the_card(
    hass: HomeAssistant, hass_client: ClientSessionGenerator
) -> None:
    """YAML resources are the user's file: nothing is written; the URLs still serve the cards."""
    assert await async_setup_component(hass, "lovelace", {"lovelace": {"resource_mode": "yaml"}})
    await _set_up(hass)

    assert await _resource_urls_yaml(hass) == []
    client = await hass_client()
    for bundled in card.CARDS:
        assert (await client.get(bundled.url)).status == HTTPStatus.OK


async def _resource_urls_yaml(hass: HomeAssistant) -> list[str]:
    return [item["url"] for item in hass.data[LOVELACE_DATA].resources.async_items()]


async def test_a_config_entry_setup_registers_the_card_once(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[Any],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Setting an entry up (and reloading it) leaves exactly one resource per card."""
    entry = await set_up_warm(hass, seed_warm_cache)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    urls = await _resource_urls(hass)
    for bundled, current in zip(card.CARDS, _current(), strict=True):
        assert [u for u in urls if u.split("?")[0] == bundled.url] == [current]
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_removing_the_last_entry_removes_the_card_resource(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[Any],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The last account's removal deletes both card resources; other resources stay."""
    other = "/hacsfiles/other-card/other-card.js"
    entry = await set_up_warm(hass, seed_warm_cache)
    resources = hass.data[LOVELACE_DATA].resources
    assert isinstance(resources, ResourceStorageCollection)
    await resources.async_create_item({"res_type": "module", "url": other})

    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert await _resource_urls(hass) == [other]


async def test_an_entry_reload_points_the_resource_at_a_changed_card_file(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[Any],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A card-only update needs no restart: the next entry setup syncs the new hash."""
    entry = await set_up_warm(hass, seed_warm_cache)

    original = card.card_hash

    def changed(path: Path = card.CARD_FILE) -> str:
        return "abcdef12" if path == card.HOMEBASE_CARD_FILE else original(path)

    monkeypatch.setattr(card, "card_hash", changed)

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    urls = await _resource_urls(hass)
    assert f"{card.HOMEBASE_CARD_URL}?v=abcdef12" in urls
    assert f"{card.CARD_URL}?v={original(card.CARD_FILE)}" in urls
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
