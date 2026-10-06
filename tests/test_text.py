"""String settings as text entities."""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

import pytest
from conftest import entity_id_for, seed_setting, set_up_warm, state_of
from eufy_home_security import EufySecurity
from eufy_home_security.devices.model_settings import settings_of
from eufy_home_security.testing import SYNTHETIC, FakeStation
from eufy_home_security.testing.cloud import FakeCloud
from homeassistant.components.text import DOMAIN as TEXT_DOMAIN
from homeassistant.components.text.const import ATTR_VALUE, SERVICE_SET_VALUE
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNKNOWN, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er

from custom_components.eufy_home_security.const import DOMAIN

# A string setting of the HomeBase 1 (T8001), which the fake station is made to be.
_MODEL: Final = "T8001"
_TONES: Final = "alarm_tones_mode"
_STATION_CHANNEL: Final = 255


def _as_homebase_1(cloud: FakeCloud) -> None:
    """Make the fake station a T8001 before the warm cache copies the device list."""
    for device in cloud.devices:
        if device.get("device_sn") == SYNTHETIC.station_sn:
            device["device_new_pn"] = _MODEL


async def test_a_string_setting_is_a_text_entity_that_writes_the_string(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The reported string is the state; a set sends it and the entity shows it."""
    _as_homebase_1(fake_cloud)
    seed_setting(fake_station, _TONES, "tone-a", channel=_STATION_CHANNEL, model=_MODEL)
    fake_station.reply_to_settings = True
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = entity_id_for(hass, TEXT_DOMAIN, SYNTHETIC.station_sn, _TONES)
    assert state_of(hass, entity_id) == "tone-a"
    registered = er.async_get(hass).async_get(entity_id)
    assert registered is not None
    assert registered.entity_category is EntityCategory.CONFIG
    assert registered.original_name == settings_of(_MODEL)[_TONES].name

    sent = len(fake_station.received)
    await hass.services.async_call(
        TEXT_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: entity_id, ATTR_VALUE: "tone-b"},
        blocking=True,
    )
    await hass.async_block_till_done()

    # The frame's shape is the library's model data; one write frame went out.
    assert len(fake_station.received) == sent + 1
    assert fake_station.received[-1]["mChannel"] == _STATION_CHANNEL
    assert state_of(hass, entity_id) == "tone-b"

    from custom_components.eufy_home_security import text

    assert text.PARALLEL_UPDATES == 1

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_an_unreported_string_setting_shows_unknown(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Nothing in the dump: unknown, not an empty string."""
    _as_homebase_1(fake_cloud)
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = entity_id_for(hass, TEXT_DOMAIN, SYNTHETIC.station_sn, _TONES)
    assert state_of(hass, entity_id) == STATE_UNKNOWN
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_string_the_library_refuses_is_a_validation_error(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A string the library refuses (``ValueError``) is a validation error; the state stays."""
    _as_homebase_1(fake_cloud)
    seed_setting(fake_station, _TONES, "tone-a", channel=_STATION_CHANNEL, model=_MODEL)
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = entity_id_for(hass, TEXT_DOMAIN, SYNTHETIC.station_sn, _TONES)
    station = entry.runtime_data.coordinators[SYNTHETIC.station_sn].station

    async def refused(*args: object, **kwargs: object) -> None:
        raise ValueError(f"{_TONES}: 'tone-z' is not one of its ids")

    monkeypatch.setattr(station, "async_set_setting", refused)
    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            TEXT_DOMAIN,
            SERVICE_SET_VALUE,
            {ATTR_ENTITY_ID: entity_id, ATTR_VALUE: "tone-z"},
            blocking=True,
        )
    await hass.async_block_till_done()

    assert raised.value.translation_domain == DOMAIN
    assert raised.value.translation_key == "setting_value_invalid"
    assert raised.value.translation_placeholders == {"target": settings_of(_MODEL)[_TONES].name}
    assert state_of(hass, entity_id) == "tone-a"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
