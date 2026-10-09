"""Devices the account's device list no longer names leave the registries.

At every setup and at each Refresh device list press, also from the cached list (the
library caches only a whole list). A device eufy lists but the client skips stays, and
a list naming no device removes nothing. The user may delete a device by hand only once
the list no longer names it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Final

import pytest
from conftest import add_entry, configure_options, entity_id_for, set_up_warm, setup_entry
from eufy_home_security import CommunicationError, EufySecurity, entity_unique_id, redact_serial
from eufy_home_security.testing import SYNTHETIC, FakeCloud
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.eufy_home_security import (
    async_remove_config_entry_device,
    runtime,
    stale_devices,
)
from custom_components.eufy_home_security.const import (
    CONF_EXTRA_COUNTRIES,
    DOMAIN,
    REFRESH_DEVICE_LIST_KEY,
)

# A standalone battery camera under the extra country: reached on demand, so setup
# opens no session to it. Synthetic, like every identity here.
CH_CAMERA_SN: Final = "T8170P0000000001"


def _ch_camera() -> dict[str, Any]:
    return {
        "device_sn": CH_CAMERA_SN,
        "device_type": 48,
        "device_name": "Solo",
        "parent_sn": CH_CAMERA_SN,
        "device_channel": 48,
        "p2p_did": SYNTHETIC.did,
        "local_ip": SYNTHETIC.station_ip,
        "params": [{"param_type": 1101, "param_value": "61", "update_time": 1.7e9}],
    }


@pytest.fixture
def ch_listed(fake_cloud: FakeCloud) -> None:
    """The extra country CH, homed on ``eu``, lists the standalone camera."""
    fake_cloud.country_regions["CH"] = "eu"
    fake_cloud.region_devices["eu:CH"] = [_ch_camera()]


def _device(hass: HomeAssistant, entry: ConfigEntry, serial: str) -> dr.DeviceEntry | None:
    return dr.async_get(hass).async_get_device_by_identifier((DOMAIN, serial), entry.entry_id)


def _add_stale_device(hass: HomeAssistant, entry: ConfigEntry) -> tuple[str, str]:
    """A device and an entity of the entry for a serial the cloud does not list."""
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, CH_CAMERA_SN)}, name="Solo"
    )
    entity = er.async_get(hass).async_get_or_create(
        "sensor",
        DOMAIN,
        entity_unique_id(CH_CAMERA_SN, "battery"),
        config_entry=entry,
        device_id=device.id,
    )
    return device.id, entity.entity_id


async def _set_up_with_ch(hass: HomeAssistant, seed_warm_cache: Callable[..., None]) -> ConfigEntry:
    """Set up with CH as an extra country, its devices listed by the setup's rescan."""
    hass.config.country = "EE"
    seed_warm_cache()
    entry = add_entry(hass, options={CONF_EXTRA_COUNTRIES: ["CH"]})
    runtime.request_rescan_at_setup(hass, entry.entry_id)
    assert await setup_entry(hass, entry)
    return entry


async def _press_refresh(hass: HomeAssistant, entry: ConfigEntry) -> None:
    button = entity_id_for(hass, "button", entry.entry_id, REFRESH_DEVICE_LIST_KEY)
    await hass.services.async_call("button", "press", {"entity_id": button}, blocking=True)
    await hass.async_block_till_done()


async def _unload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.usefixtures("ch_listed")
async def test_a_removed_sign_in_country_takes_its_devices_and_entities(
    hass: HomeAssistant,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The reload after an extra country is removed lists every scope again; the
    country's devices and their entities go, the rest stays."""
    entry = await _set_up_with_ch(hass, seed_warm_cache)
    ch_device = _device(hass, entry, CH_CAMERA_SN)
    assert ch_device is not None
    ch_entities = [
        e.entity_id for e in er.async_entries_for_device(er.async_get(hass), ch_device.id)
    ]
    assert ch_entities

    result = await hass.config_entries.options.async_init(entry.entry_id)
    await configure_options(hass, result["flow_id"], {CONF_EXTRA_COUNTRIES: []})
    await hass.async_block_till_done()

    assert entry.options[CONF_EXTRA_COUNTRIES] == []
    assert _device(hass, entry, CH_CAMERA_SN) is None
    assert not [e for e in ch_entities if er.async_get(hass).async_get(e) is not None]
    assert _device(hass, entry, SYNTHETIC.station_sn) is not None
    assert _device(hass, entry, SYNTHETIC.camera_sn) is not None
    assert _device(hass, entry, entry.entry_id) is not None
    await _unload(hass, entry)


@pytest.mark.parametrize("when", ["warm start", "failed rescan at start", "failed press"])
async def test_a_cached_device_list_also_removes_an_unlisted_device(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    when: str,
) -> None:
    """A list from the cache, at a start or after a refresh the cloud did not answer,
    removes a device it does not name and keeps the listed ones."""
    seed_warm_cache()
    entry = add_entry(hass)
    if when == "failed press":
        assert await setup_entry(hass, entry)
    device_id, entity_id = _add_stale_device(hass, entry)
    if when == "failed rescan at start":
        runtime.request_rescan_at_setup(hass, entry.entry_id)
    if when != "warm start":
        fake_cloud.call_errors = [CommunicationError("unreachable")]
    if when == "failed press":
        await _press_refresh(hass, entry)
    else:
        assert await setup_entry(hass, entry)

    assert not fake_cloud.call_errors, "the refresh never met the cloud failure"
    assert dr.async_get(hass).async_get(device_id) is None
    assert er.async_get(hass).async_get(entity_id) is None
    assert _device(hass, entry, SYNTHETIC.station_sn) is not None
    assert _device(hass, entry, SYNTHETIC.camera_sn) is not None
    await _unload(hass, entry)


async def test_a_press_removes_a_device_the_list_no_longer_names(
    hass: HomeAssistant,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A press the cloud answers removes the device and its entities at once."""
    entry = await set_up_warm(hass, seed_warm_cache)
    device_id, entity_id = _add_stale_device(hass, entry)

    await _press_refresh(hass, entry)

    assert dr.async_get(hass).async_get(device_id) is None
    assert er.async_get(hass).async_get(entity_id) is None
    assert _device(hass, entry, SYNTHETIC.station_sn) is not None
    assert _device(hass, entry, SYNTHETIC.camera_sn) is not None
    await _unload(hass, entry)


@pytest.mark.usefixtures("ch_listed")
async def test_a_station_that_left_the_list_goes_at_the_setup_after_a_press(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The client keeps the stations it built, so a station a press no longer finds goes
    at the next setup, which builds from the list that press cached."""
    entry = await _set_up_with_ch(hass, seed_warm_cache)
    fake_cloud.region_devices["eu:CH"] = []

    await _press_refresh(hass, entry)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert _device(hass, entry, CH_CAMERA_SN) is None
    assert _device(hass, entry, SYNTHETIC.station_sn) is not None
    await _unload(hass, entry)


async def test_only_a_device_the_list_no_longer_names_can_be_deleted(
    hass: HomeAssistant,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The station, its camera and the account device stay; an unlisted one may go."""
    entry = await set_up_warm(hass, seed_warm_cache)
    device_id, _entity_id = _add_stale_device(hass, entry)
    registry = dr.async_get(hass)

    for serial in (SYNTHETIC.station_sn, SYNTHETIC.camera_sn, entry.entry_id):
        listed = _device(hass, entry, serial)
        assert listed is not None
        assert not await async_remove_config_entry_device(hass, entry, listed)
    stale = registry.async_get(device_id)
    assert isinstance(stale, dr.DeviceEntry)
    assert await async_remove_config_entry_device(hass, entry, stale)

    await _unload(hass, entry)
    assert not await async_remove_config_entry_device(hass, entry, stale)


def test_a_device_under_a_station_served_elsewhere_is_kept(hass: HomeAssistant) -> None:
    """Another account serves the station, so the devices paired to it are unknown here."""
    entry = add_entry(hass)
    registry = dr.async_get(hass)
    station = registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, SYNTHETIC.station_sn)}
    )
    camera = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, SYNTHETIC.camera_sn)},
        via_device_id=station.id,
    )
    station_only = frozenset({SYNTHETIC.station_sn})

    elsewhere = runtime.ListedDevices(serials=station_only, elsewhere=station_only)
    assert not stale_devices.unlisted(registry, entry.entry_id, camera, elsewhere)
    served_here = runtime.ListedDevices(serials=station_only, elsewhere=frozenset())
    assert stale_devices.unlisted(registry, entry.entry_id, camera, served_here)


def test_a_device_the_list_names_but_the_client_skips_is_kept(hass: HomeAssistant) -> None:
    """A camera whose station is not on the list is still on eufy's list: kept."""
    entry = add_entry(hass)
    registry = dr.async_get(hass)
    camera = registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, CH_CAMERA_SN)}
    )
    station_only = frozenset({SYNTHETIC.station_sn})

    skipped = runtime.ListedDevices(
        serials=station_only,
        elsewhere=frozenset(),
        skipped=frozenset({redact_serial(CH_CAMERA_SN)}),
    )
    assert not stale_devices.unlisted(registry, entry.entry_id, camera, skipped)
    not_listed = runtime.ListedDevices(serials=station_only, elsewhere=frozenset())
    assert stale_devices.unlisted(registry, entry.entry_id, camera, not_listed)


def test_a_list_naming_no_device_removes_nothing(hass: HomeAssistant) -> None:
    """An empty list is never taken as every device gone."""
    entry = add_entry(hass)
    device_id, entity_id = _add_stale_device(hass, entry)

    stale_devices.async_remove_unlisted(
        hass, entry, runtime.ListedDevices(serials=frozenset(), elsewhere=frozenset())
    )

    assert dr.async_get(hass).async_get(device_id) is not None
    assert er.async_get(hass).async_get(entity_id) is not None
