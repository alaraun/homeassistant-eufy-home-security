"""Tests for the device list refresh button."""

import asyncio

from conftest import (
    SENSOR_SN,
    add_motion_sensor,
    cloud_calls,
    entity_id_for,
    set_up_warm,
    state_of,
    wait_until,
)
from eufy_home_security import (
    AuthenticationError,
    DevicesChanged,
    LoginLimitedError,
    RateLimitedError,
    SessionReplacedError,
)
from eufy_home_security.testing import SYNTHETIC
from homeassistant.const import STATE_UNAVAILABLE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.eufy_home_security import errors
from custom_components.eufy_home_security.const import DOMAIN, REFRESH_DEVICE_LIST_KEY


async def test_the_account_device_carries_the_refresh_button(
    hass: HomeAssistant, fake_station, built_clients, seed_warm_cache
):
    """The account service device carries the Refresh device list button, a config entity."""
    entry = await set_up_warm(hass, seed_warm_cache)
    dev_reg = dr.async_get(hass)
    devices = dr.async_entries_for_config_entry(dev_reg, entry.entry_id)

    account_dev = next(d for d in devices if d.entry_type == dr.DeviceEntryType.SERVICE)
    assert account_dev.identifiers == {(DOMAIN, entry.entry_id)}
    assert account_dev.name == "eufy account"
    assert account_dev.manufacturer == "eufy"

    ent_reg = er.async_get(hass)
    btn_id = entity_id_for(hass, "button", entry.entry_id, REFRESH_DEVICE_LIST_KEY)
    assert btn_id == "button.eufy_account_refresh_device_list"

    btn_ent = ent_reg.async_get(btn_id)
    assert btn_ent is not None
    assert btn_ent.device_id == account_dev.id
    assert btn_ent.entity_category == EntityCategory.CONFIG

    assert state_of(hass, btn_id) != STATE_UNAVAILABLE

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_press_that_changes_nothing_fetches_once_and_does_not_reload(
    hass: HomeAssistant, fake_cloud, built_clients, fake_station, seed_warm_cache
):
    """A press fetches the device list once; with nothing changed the entry is not reloaded."""
    entry = await set_up_warm(hass, seed_warm_cache)
    n = len(cloud_calls(fake_cloud))
    before = len(built_clients)

    btn_id = entity_id_for(hass, "button", entry.entry_id, REFRESH_DEVICE_LIST_KEY)
    await hass.services.async_call("button", "press", {"entity_id": btn_id}, blocking=True)
    await asyncio.sleep(0.3)
    await hass.async_block_till_done()

    assert cloud_calls(fake_cloud)[n:] == ["devices"]
    assert len(built_clients) == before
    assert entry.state.value == "loaded"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_press_that_finds_a_new_paired_device_reloads_once(
    hass: HomeAssistant, fake_cloud, fake_station, built_clients, seed_warm_cache
):
    """A press that finds a new paired device reloads the entry once and registers it."""
    entry = await set_up_warm(hass, seed_warm_cache)
    before = len(built_clients)

    add_motion_sensor(fake_station, fake_cloud)

    btn_id = entity_id_for(hass, "button", entry.entry_id, REFRESH_DEVICE_LIST_KEY)
    await hass.services.async_call("button", "press", {"entity_id": btn_id}, blocking=True)

    await wait_until(lambda: len(built_clients) == before + 1 and entry.state.value == "loaded")
    await asyncio.sleep(0.3)
    await hass.async_block_till_done()

    assert len(built_clients) == before + 1

    dev_reg = dr.async_get(hass)
    assert dev_reg.async_get_device_by_identifier((DOMAIN, SENSOR_SN), entry.entry_id) is not None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_press_while_a_reload_is_pending_makes_no_cloud_call(
    hass: HomeAssistant, fake_cloud, fake_station, built_clients, seed_warm_cache
):
    """A press while an entry reload is pending is skipped without a cloud call."""
    entry = await set_up_warm(hass, seed_warm_cache)
    n = len(cloud_calls(fake_cloud))

    entry.runtime_data.router.async_reload_soon("test")

    btn_id = entity_id_for(hass, "button", entry.entry_id, REFRESH_DEVICE_LIST_KEY)
    btn_entity = hass.data["entity_components"]["button"].get_entity(btn_id)

    await btn_entity.async_press()
    await hass.async_block_till_done()

    assert "devices" not in cloud_calls(fake_cloud)[n:]

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_two_device_list_changes_schedule_one_reload(
    hass: HomeAssistant, fake_station, built_clients, seed_warm_cache
):
    """Two device list changes before the reload starts reload the entry once."""
    entry = await set_up_warm(hass, seed_warm_cache)
    before = len(built_clients)
    router = entry.runtime_data.router

    router.handle(DevicesChanged(station_sn=SYNTHETIC.station_sn, added=(SENSOR_SN,)))
    router.handle(DevicesChanged(station_sn=SYNTHETIC.station_sn, added=(SENSOR_SN,)))

    assert router.reload_pending is True
    assert len(built_clients) == before

    await wait_until(lambda: len(built_clients) == before + 1 and entry.state.value == "loaded")
    await asyncio.sleep(0.3)
    await hass.async_block_till_done()

    assert len(built_clients) == before + 1
    assert entry.state.value == "loaded"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_refresh_failures_route_to_reauth_or_repairs(
    hass: HomeAssistant, fake_station, built_clients, seed_warm_cache
):
    """Every refresh failure is a translated error; an authentication failure starts reauth."""
    entry = await set_up_warm(hass, seed_warm_cache)

    err_session = SessionReplacedError("x")
    ha_err = errors.device_list_refresh_failed(hass, entry, err_session)
    assert isinstance(ha_err, HomeAssistantError)
    assert ha_err.translation_key == "device_list_refresh_failed"

    err_auth = AuthenticationError("x")
    ha_err = errors.device_list_refresh_failed(hass, entry, err_auth)
    assert isinstance(ha_err, HomeAssistantError)
    assert ha_err.translation_key == "device_list_refresh_failed"

    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(flows) == 1
    assert flows[0]["context"].get("source") == "reauth"

    err_rate = RateLimitedError("x")
    ha_err = errors.device_list_refresh_failed(hass, entry, err_rate)
    assert isinstance(ha_err, HomeAssistantError)
    assert ha_err.translation_key == "device_list_refresh_failed"

    err_login = LoginLimitedError("x")
    ha_err = errors.device_list_refresh_failed(hass, entry, err_login)
    assert isinstance(ha_err, HomeAssistantError)
    assert ha_err.translation_key == "device_list_refresh_failed"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
