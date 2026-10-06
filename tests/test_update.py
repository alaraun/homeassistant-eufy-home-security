"""Each device's firmware, as an update entity that never claims to be up to date.

Nothing tells this integration which firmware eufy has published, so every one of
these entities reports only what its device is running: ``latest_version`` stays
None, which leaves the state unknown, and no install is offered.

The entity is named "Firmware update" by its own translation key, apart from the
diagnostic firmware sensor on the same device, and publishes no entity picture.

The parameter id below is a fixture value only: it says what the fake station
reports, and the integration never names one.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

from conftest import SENSOR_SN, add_motion_sensor, entity_id_for, set_up_warm
from eufy_home_security import EufySecurity, entity_unique_id
from eufy_home_security.testing import SYNTHETIC, FakeCloud, FakeStation
from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.components.update import (
    ATTR_INSTALLED_VERSION,
    ATTR_LATEST_VERSION,
)
from homeassistant.components.update import DOMAIN as UPDATE_DOMAIN
from homeassistant.const import (
    ATTR_ENTITY_PICTURE,
    ATTR_SUPPORTED_FEATURES,
    STATE_UNKNOWN,
    EntityCategory,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.eufy_home_security.const import DOMAIN, FIRMWARE_KEY

# The parameter a device reports its own firmware under (fixture value only).
_FIRMWARE_PARAM: Final = 7013
_CAMERA_CHANNEL: Final = 0

# What the fake station's own dump says it is running.
_STATION_FIRMWARE: Final = "3.8.7.4"
# What the camera reports in its block, and what the cloud recorded for the
# T8910 motion sensor, which never reports one of its own.
_CAMERA_FIRMWARE: Final = "2.0.1"
_SENSOR_FIRMWARE: Final = "1.2.3"

# The update entity's own name key, and the document that has to hold a name for it.
_UPDATE_TRANSLATION_KEY: Final = "firmware_update"
_EN_PATH: Final = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "eufy_home_security"
    / "translations"
    / "en.json"
)


def _set_cloud_firmware(cloud: FakeCloud, serial: str, firmware: str) -> None:
    """Record a firmware version against one device of the fake cloud's list.

    Call before ``set_up_warm``: the warm cache copies the cloud's device list when
    it is seeded, so a later change is not in the list the entry is set up from.
    """
    for device in cloud.devices:
        if device["device_sn"] == serial:
            device["main_sw_version"] = firmware
            return
    raise AssertionError(f"{serial} is not in the fake cloud's device list")


def _update_entity_ids(hass: HomeAssistant, entry: MockConfigEntry) -> list[str]:
    """Every update-domain entity this entry has registered, sorted."""
    return sorted(
        registered.entity_id
        for registered in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if registered.domain == UPDATE_DOMAIN
    )


async def test_every_device_has_a_firmware_update_entity_with_unknown_state(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The station, the camera and the sensor each show their firmware.

    The state is unknown on purpose, and there is no install feature. Nothing
    reports which firmware is available, so "up to date" would be invented and an
    install button would promise something no local protocol can do.
    The installed version is the camera's own dump value, and for the
    T8910 the cloud's record, which is the only version it will ever have.

    Each carries its own translation key, so it does not read "Firmware" beside the
    firmware sensor, and the unique id ``entity_unique_id(serial, FIRMWARE_KEY)``.
    """
    add_motion_sensor(fake_station, fake_cloud)
    _set_cloud_firmware(fake_cloud, SENSOR_SN, _SENSOR_FIRMWARE)
    fake_station.params[_CAMERA_CHANNEL][_FIRMWARE_PARAM] = _CAMERA_FIRMWARE
    entry = await set_up_warm(hass, seed_warm_cache)

    registry = er.async_get(hass)
    for serial, installed in (
        (SYNTHETIC.station_sn, _STATION_FIRMWARE),
        (SYNTHETIC.camera_sn, _CAMERA_FIRMWARE),
        (SENSOR_SN, _SENSOR_FIRMWARE),
    ):
        entity_id = entity_id_for(hass, UPDATE_DOMAIN, serial, FIRMWARE_KEY)
        state = hass.states.get(entity_id)
        assert state is not None, serial
        assert state.attributes[ATTR_INSTALLED_VERSION] == installed, serial
        # None, so Home Assistant gives the entity no state at all.
        assert state.attributes[ATTR_LATEST_VERSION] is None, serial
        assert state.state == STATE_UNKNOWN, serial
        # No install: the feature is absent, which is also why the category
        # defaults to diagnostic.
        assert state.attributes[ATTR_SUPPORTED_FEATURES] == 0, serial
        # The integration's own icon, served from its brand/ directory.
        assert (
            state.attributes[ATTR_ENTITY_PICTURE] == f"/api/brands/integration/{DOMAIN}/icon.png"
        ), serial

        registered = registry.async_get(entity_id)
        assert registered is not None, serial
        assert registered.entity_category is EntityCategory.DIAGNOSTIC, serial
        # Its own name, rather than the one its device class would give it.
        assert registered.translation_key == _UPDATE_TRANSLATION_KEY, serial
        # The unique id is the serial and the firmware key.
        assert registered.unique_id == entity_unique_id(serial, FIRMWARE_KEY), serial

    # One key, two domains: the camera's firmware sensor and its firmware update
    # entity share a unique-id string without colliding, because unique ids are
    # scoped per platform domain.
    unique_id = entity_unique_id(SYNTHETIC.camera_sn, FIRMWARE_KEY)
    camera_sensor = registry.async_get_entity_id(SENSOR_DOMAIN, DOMAIN, unique_id)
    camera_update = registry.async_get_entity_id(UPDATE_DOMAIN, DOMAIN, unique_id)
    assert camera_sensor is not None
    assert camera_update is not None
    assert camera_sensor != camera_update

    # The station's and the camera's update entities are distinct: the key is the
    # same, the serial is not.
    station_update = entity_id_for(hass, UPDATE_DOMAIN, SYNTHETIC.station_sn, FIRMWARE_KEY)
    assert station_update != camera_update

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_the_firmware_sensor_and_the_update_entity_read_as_different_names(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """One device's firmware sensor and firmware update entity have different names.

    Each name is resolved as Home Assistant does: the registry row's translation key,
    looked up in the shipped English document. Without its own key the update entity
    would be named after its device class, "Firmware", the sensor's name.
    """
    entry = await set_up_warm(hass, seed_warm_cache)

    registry = er.async_get(hass)
    document: Any = json.loads(_EN_PATH.read_text())
    names: dict[str, str] = {}
    for domain in (SENSOR_DOMAIN, UPDATE_DOMAIN):
        entity_id = entity_id_for(hass, domain, SYNTHETIC.camera_sn, FIRMWARE_KEY)
        registered = registry.async_get(entity_id)
        assert registered is not None, domain
        translation_key = registered.translation_key
        assert translation_key is not None, (
            f"the {domain} firmware entity carries no translation key, so Home "
            f"Assistant names it from its device class"
        )
        name = document.get("entity", {}).get(domain, {}).get(translation_key, {}).get("name")
        assert isinstance(name, str) and name.strip(), (
            f"entity.{domain}.{translation_key}.name is missing from en.json, so this "
            f"entity reaches the user as a bare translation key"
        )
        names[domain] = name

    assert names[SENSOR_DOMAIN] != names[UPDATE_DOMAIN], (
        f"both firmware entities on one device resolve to {names[SENSOR_DOMAIN]!r}; "
        f"the user sees two rows with one name and nothing to tell them apart"
    )
    assert names[SENSOR_DOMAIN] == "Firmware"
    assert names[UPDATE_DOMAIN] == "Firmware update"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_device_with_no_firmware_anywhere_shows_no_installed_version(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A camera reporting no firmware, with none in the cloud either, claims none.

    The entity exists and shows no version rather than an invented one: the fake
    camera reports no firmware and the cloud's list holds none for it.
    """
    entry = await set_up_warm(hass, seed_warm_cache)

    entity_id = entity_id_for(hass, UPDATE_DOMAIN, SYNTHETIC.camera_sn, FIRMWARE_KEY)
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.attributes[ATTR_INSTALLED_VERSION] is None
    assert state.attributes[ATTR_LATEST_VERSION] is None
    assert state.state == STATE_UNKNOWN

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_update_entities_survive_a_reload_without_duplicates(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Reloading the entry leaves the same update entities, and no second set.

    The ids are keyed by each device's serial, so the entity a device gets does
    not depend on where it sits in the cloud's list or on how often the entry is
    loaded. A second set would show up here as a count that grew.
    """
    add_motion_sensor(fake_station, fake_cloud)
    entry = await set_up_warm(hass, seed_warm_cache)

    before = _update_entity_ids(hass, entry)
    # The station, the camera and the motion sensor: one each, and no more.
    assert len(before) == 3, before

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert _update_entity_ids(hass, entry) == before

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
