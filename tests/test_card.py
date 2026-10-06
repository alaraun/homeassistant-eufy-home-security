"""The bundled dashboard card: served by the integration, one storage-mode resource kept current."""

from __future__ import annotations

from collections.abc import Callable
from http import HTTPStatus
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


async def _set_up(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()


def test_the_bundled_card_is_shipped_and_names_its_custom_element() -> None:
    """The file exists in the package and defines the element dashboards reference."""
    source = card.CARD_FILE.read_text(encoding="utf-8")
    assert "customElements.define('eufy-camera-card'" in source
    assert len(card.card_hash()) == 8


async def test_the_card_resource_is_created_with_the_file_hash(
    hass: HomeAssistant, hass_client: ClientSessionGenerator
) -> None:
    """No resource yet: one module resource at the bundled URL, cache key = the file hash."""
    await _set_up(hass)

    assert await _resource_urls(hass) == [f"{card.CARD_URL}?v={card.card_hash()}"]
    client = await hass_client()
    response = await client.get(card.CARD_URL)
    assert response.status == HTTPStatus.OK
    assert await response.read() == card.CARD_FILE.read_bytes()


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

    assert await _resource_urls(hass) == [other, f"{card.CARD_URL}?v={card.card_hash()}"]


async def test_a_current_resource_is_left_untouched(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Already at the current hash: no write."""
    url = f"{card.CARD_URL}?v={card.card_hash()}"
    hass_storage[_RESOURCES_KEY] = _stored_resources([url])
    await _set_up(hass)
    resources = hass.data[LOVELACE_DATA].resources
    assert isinstance(resources, ResourceStorageCollection)
    assert [item["id"] for item in resources.async_items()] == ["r0"]
    assert await _resource_urls(hass) == [url]


async def test_yaml_resource_mode_registers_nothing_but_still_serves_the_card(
    hass: HomeAssistant, hass_client: ClientSessionGenerator
) -> None:
    """YAML resources are the user's file: nothing is written; the URL still serves the card."""
    assert await async_setup_component(hass, "lovelace", {"lovelace": {"resource_mode": "yaml"}})
    await _set_up(hass)

    assert await _resource_urls_yaml(hass) == []
    client = await hass_client()
    assert (await client.get(card.CARD_URL)).status == HTTPStatus.OK


async def _resource_urls_yaml(hass: HomeAssistant) -> list[str]:
    return [item["url"] for item in hass.data[LOVELACE_DATA].resources.async_items()]


async def test_a_config_entry_setup_registers_the_card_once(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[Any],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Setting an entry up (and reloading it) leaves exactly one card resource."""
    entry = await set_up_warm(hass, seed_warm_cache)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    urls = await _resource_urls(hass)
    assert [u for u in urls if u.split("?")[0] == card.CARD_URL] == [
        f"{card.CARD_URL}?v={card.card_hash()}"
    ]
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_removing_the_last_entry_removes_the_card_resource(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[Any],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The last account's removal deletes the resource; other resources stay."""
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
    monkeypatch.setattr(card, "card_hash", lambda: "abcdef12")

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert f"{card.CARD_URL}?v=abcdef12" in await _resource_urls(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
