"""Camera lamps as lights (``Station.lights_for``).

The fixture camera is a T8160 behind a HomeBase 3: an enum brightness (Low, Medium, High
= 40, 70, 100 %). Made a T8170 there, it has a 1-100 % range and a lamp control enabled
by its Lighting setting; made a T84A1, its lamp switch is read-only.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import Callable
from typing import Any, Final

import pytest
from conftest import add_entry, entity_id_for, seed_setting, set_up_warm, setting_param, setup_entry
from conftest import state_of as _state_of
from eufy_home_security import (
    DeviceTimeoutError,
    EufySecurity,
    Light,
    Station,
    entity_unique_id,
    redact_serial,
)
from eufy_home_security.devices import Setting
from eufy_home_security.testing import SYNTHETIC, FakeCloud, FakeStation
from homeassistant.components.homeassistant.exposed_entities import (
    async_expose_entity,
    async_should_expose,
)
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_MODE,
    ATTR_SUPPORTED_COLOR_MODES,
    ColorMode,
    LightEntity,
)
from homeassistant.components.light import DOMAIN as LIGHT_DOMAIN
from homeassistant.components.switch_as_x import light as switch_as_x_light
from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.const import (
    ATTR_ASSUMED_STATE,
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    STATE_UNKNOWN,
    Platform,
)
from homeassistant.core import Event, HomeAssistant, callback, split_entity_id
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import label_registry as lr
from homeassistant.helpers.event import async_track_entity_registry_updated_event
from homeassistant.setup import async_setup_component
from homeassistant.util.color import value_to_brightness
from pytest_homeassistant.common import MockConfigEntry, async_capture_events

from custom_components.eufy_home_security.const import DOMAIN
from custom_components.eufy_home_security.settings import SettingEntitySpec, setting_specs

_CAMERA: Final = SYNTHETIC.camera_sn
_SWITCH: Final = "spotlight_switch"
_BRIGHTNESS: Final = "spotlight_brightness_level"
_LIGHTING: Final = "spotlight_status"
_CHANNEL: Final = 0
_UNCONFIRMED: Final = "setting_unconfirmed"
_CONTROL_OFF: Final = "light_control_off"


def _as_model(cloud: FakeCloud, code: str) -> None:
    """Make the fixture camera a ``code`` before the warm cache copies the device list."""
    for device in cloud.devices:
        if device.get("device_sn") == _CAMERA:
            device["device_new_pn"] = code


@pytest.fixture
def writes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    """Every ``(key, value)`` the integration asks the library to write, in order."""
    sent: list[tuple[str, Any]] = []
    real = Station.async_set_setting

    async def recording(self: Station, key: str, value: Any, **kwargs: Any) -> None:
        sent.append((key, value))
        await real(self, key, value, **kwargs)

    monkeypatch.setattr(Station, "async_set_setting", recording)
    return sent


def _light_id(hass: HomeAssistant) -> str:
    return entity_id_for(hass, LIGHT_DOMAIN, _CAMERA, _SWITCH)


def _param(station: FakeStation, key: str, model: str = "T8160") -> Any:
    """The raw value the fake station holds for a camera setting, or None."""
    return station.params.get(_CHANNEL, {}).get(setting_param(key, model=model))


async def _turn(hass: HomeAssistant, entity_id: str, on: bool, **data: Any) -> None:
    await hass.services.async_call(
        LIGHT_DOMAIN,
        SERVICE_TURN_ON if on else SERVICE_TURN_OFF,
        {ATTR_ENTITY_ID: entity_id, **data},
        blocking=True,
    )
    await hass.async_block_till_done()


async def _unload(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


def _replace_lights(monkeypatch: pytest.MonkeyPatch, **changes: Any) -> None:
    """Change every light the library lists, as a model with those fields would have."""
    real = Station.lights_for

    def lights_for(self: Station, device_sn: str | None = None) -> tuple[Light, ...]:
        return tuple(dataclasses.replace(light, **changes) for light in real(self, device_sn))

    monkeypatch.setattr(Station, "lights_for", lights_for)


def _replace_setting(monkeypatch: pytest.MonkeyPatch, key: str, **changes: Any) -> None:
    """Change setting ``key`` as the library lists it, for the lights as well."""
    real = Station.settings_for

    def settings_for(self: Station, device_sn: str | None = None) -> tuple[Setting, ...]:
        return tuple(
            dataclasses.replace(s, **changes) if s.key == key else s for s in real(self, device_sn)
        )

    monkeypatch.setattr(Station, "settings_for", settings_for)


# ── what is built ───────────────────────────────────────────────────────────


async def test_the_lamp_is_a_light_with_the_switchs_unique_id_and_its_brightness(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """One light, on the camera only, named by the library, enabled and without a category,
    reported (not assumed). The switch is gone; the brightness select stays, registered
    disabled."""
    seed_setting(fake_station, _SWITCH, 1)
    seed_setting(fake_station, _BRIGHTNESS, 70)
    entry = await set_up_warm(hass, seed_warm_cache)
    registry = er.async_get(hass)

    entity_id = _light_id(hass)
    lights = [
        row
        for row in er.async_entries_for_config_entry(registry, entry.entry_id)
        if row.domain == LIGHT_DOMAIN
    ]
    assert [row.entity_id for row in lights] == [entity_id]
    row = lights[0]
    assert row.unique_id == entity_unique_id(_CAMERA, _SWITCH)
    assert row.original_name == "Light"
    assert row.entity_category is None
    assert row.disabled_by is None
    assert row.translation_key is None
    unique_id = entity_unique_id(_CAMERA, _SWITCH)
    assert registry.async_get_entity_id(Platform.SWITCH, DOMAIN, unique_id) is None
    select = registry.async_get(entity_id_for(hass, Platform.SELECT, _CAMERA, _BRIGHTNESS))
    assert select is not None
    assert select.disabled_by is er.RegistryEntryDisabler.INTEGRATION

    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == STATE_ON
    assert state.attributes[ATTR_COLOR_MODE] == ColorMode.BRIGHTNESS
    assert state.attributes[ATTR_SUPPORTED_COLOR_MODES] == [ColorMode.BRIGHTNESS]
    assert state.attributes[ATTR_BRIGHTNESS] == value_to_brightness((1, 100), 70)
    assert not state.attributes.get(ATTR_ASSUMED_STATE)
    await _unload(hass, entry)


async def test_a_lamp_whose_switch_is_read_only_here_gets_no_light(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A T84A1 behind a HomeBase reads its lamp but cannot switch it: the read-only binary
    sensor stays, and its brightness number stays enabled."""
    _as_model(fake_cloud, "T84A1")
    entry = await set_up_warm(hass, seed_warm_cache)
    station = entry.runtime_data.coordinators[SYNTHETIC.station_sn].station
    (lamp,) = station.lights_for(_CAMERA)
    assert not lamp.on_off_writable
    registry = er.async_get(hass)
    unique_id = entity_unique_id(_CAMERA, _SWITCH)

    assert registry.async_get_entity_id(LIGHT_DOMAIN, DOMAIN, unique_id) is None
    assert registry.async_get_entity_id(Platform.BINARY_SENSOR, DOMAIN, unique_id) is not None
    number = registry.async_get(entity_id_for(hass, Platform.NUMBER, _CAMERA, _BRIGHTNESS))
    assert number is not None
    assert number.disabled_by is None
    await _unload(hass, entry)


async def test_a_station_without_a_lamp_gets_no_light(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The HomeBase has no lamp: the only light spec is its camera's."""
    entry = await set_up_warm(hass, seed_warm_cache)
    station = entry.runtime_data.coordinators[SYNTHETIC.station_sn].station
    assert station.lights_for(None) == ()
    lights = [s for s in setting_specs(station) if s.platform is Platform.LIGHT]
    assert [(s.device_sn, s.key) for s in lights] == [(_CAMERA, _SWITCH)]
    await _unload(hass, entry)


async def test_without_a_writable_brightness_the_light_is_on_off_only(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    writes: list[tuple[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A brightness the library cannot write here: on/off only, the brightness keeps its own
    read-only entity, and a turn-on writes only the switch."""
    _replace_setting(monkeypatch, _BRIGHTNESS, writable=False)
    seed_setting(fake_station, _BRIGHTNESS, 70)
    fake_station.reply_to_settings = True
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = _light_id(hass)
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.attributes[ATTR_SUPPORTED_COLOR_MODES] == [ColorMode.ONOFF]
    assert state.attributes.get(ATTR_BRIGHTNESS) is None
    registry = er.async_get(hass)
    unique_id = entity_unique_id(_CAMERA, _BRIGHTNESS)
    assert registry.async_get_entity_id(Platform.SELECT, DOMAIN, unique_id) is None
    assert registry.async_get_entity_id(Platform.SENSOR, DOMAIN, unique_id) is not None
    station = entry.runtime_data.coordinators[SYNTHETIC.station_sn].station
    specs = [s for s in setting_specs(station) if s.device_sn == _CAMERA]
    assert [s.covered for s in specs if s.key == _BRIGHTNESS] == [False]

    await _turn(hass, entity_id, True)
    assert writes == [(_SWITCH, True)]
    assert _state_of(hass, entity_id) == STATE_ON
    await _unload(hass, entry)


# ── the switch row moves to the light domain ─────────────────────────────────


async def test_the_lamps_switch_row_is_removed_once_and_nothing_else(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """An install from before lights: the lamp's switch row is removed at setup, only that
    row, and not again at the next start; the brightness select the user has enabled stays
    enabled."""
    seed_warm_cache()
    entry = add_entry(hass)
    registry = er.async_get(hass)
    old = registry.async_get_or_create(
        Platform.SWITCH, DOMAIN, entity_unique_id(_CAMERA, _SWITCH), config_entry=entry
    )
    brightness = registry.async_get_or_create(
        Platform.SELECT, DOMAIN, entity_unique_id(_CAMERA, _BRIGHTNESS), config_entry=entry
    )
    events = async_capture_events(hass, er.EVENT_ENTITY_REGISTRY_UPDATED)

    assert await setup_entry(hass, entry)
    removed = [e.data["entity_id"] for e in events if e.data["action"] == "remove"]
    assert removed == [old.entity_id]
    assert registry.async_get(old.entity_id) is None
    assert hass.states.get(_light_id(hass)) is not None
    kept = registry.async_get(brightness.entity_id)
    assert kept is not None
    assert kept.disabled_by is None

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert [e.data["entity_id"] for e in events if e.data["action"] == "remove"] == removed
    await _unload(hass, entry)


async def test_a_light_row_is_removed_when_the_lamps_switch_is_read_only_here(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A light row of a lamp that is no longer switchable here goes; its sensor is built."""
    _as_model(fake_cloud, "T84A1")
    seed_warm_cache()
    entry = add_entry(hass)
    registry = er.async_get(hass)
    unique_id = entity_unique_id(_CAMERA, _SWITCH)
    old = registry.async_get_or_create(LIGHT_DOMAIN, DOMAIN, unique_id, config_entry=entry)

    assert await setup_entry(hass, entry)
    assert registry.async_get(old.entity_id) is None
    assert registry.async_get_entity_id(Platform.BINARY_SENSOR, DOMAIN, unique_id) is not None
    await _unload(hass, entry)


# ── the switch row's customisations carry over to the light row ──────────────


def _camera_device(hass: HomeAssistant, entry: MockConfigEntry) -> dr.DeviceEntry:
    """The camera's device as setup registers it (the fake cloud names it Front)."""
    return dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, _CAMERA)}, name="Front"
    )


def _old_switch(
    hass: HomeAssistant, entry: MockConfigEntry, name: str = "Spotlight"
) -> er.RegistryEntry:
    """The lamp's switch row of an install from before lights, named ``name`` by the
    integration: ``switch.front_<name>``."""
    return er.async_get(hass).async_get_or_create(
        Platform.SWITCH,
        DOMAIN,
        entity_unique_id(_CAMERA, _SWITCH),
        config_entry=entry,
        device_id=_camera_device(hass, entry).id,
        has_entity_name=True,
        object_id_base=name,
        original_name=name,
    )


def _moved_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "custom_components.eufy_home_security" and "moved to" in r.getMessage()
    ]


async def test_the_switch_rows_customisations_carry_over_to_the_light_row(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Object id, name, icon, area, labels, aliases and the user's hidden and disabled marks
    move to the light row; the switch row is removed."""
    object_id = "garden_lamp"
    seed_warm_cache()
    entry = add_entry(hass)
    registry = er.async_get(hass)
    area = ar.async_get(hass).async_create("Garden")
    label = lr.async_get(hass).async_create("Outdoor")
    old = _old_switch(hass, entry)
    assert old.entity_id == "switch.front_spotlight"
    old = registry.async_update_entity(
        old.entity_id,
        new_entity_id=f"switch.{object_id}",
        name="Garden lamp",
        icon="mdi:lamp",
        area_id=area.id,
        labels={label.label_id},
        aliases=["yard light"],
        hidden_by=er.RegistryEntryHider.USER,
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    caplog.set_level(logging.INFO, logger="custom_components.eufy_home_security")

    assert await setup_entry(hass, entry)
    assert registry.async_get(old.entity_id) is None
    light = registry.async_get(_light_id(hass))
    assert light is not None
    assert light.entity_id == f"light.{object_id}"
    assert light.config_entry_id == entry.entry_id
    assert light.name == "Garden lamp"
    assert light.icon == "mdi:lamp"
    assert light.area_id == area.id
    assert light.labels == {label.label_id}
    assert light.aliases == ["yard light"]
    assert light.hidden_by is er.RegistryEntryHider.USER
    assert light.disabled_by is er.RegistryEntryDisabler.USER
    assert light.original_name == "Light"
    assert light.device_id == old.device_id
    (line,) = _moved_lines(caplog)
    assert old.entity_id in line
    assert f"light.{object_id}" in line
    assert redact_serial(_CAMERA) in line
    assert _CAMERA not in line
    await _unload(hass, entry)


async def test_a_switch_row_as_home_assistant_named_it_carries_its_object_id(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``switch.front_spotlight`` as Home Assistant named it, in an area set later: the light
    is ``light.front_spotlight`` (not the ``light.front_light`` its own name gives), enabled
    and shown, in that area; the integration's own disabled and hidden marks of the switch
    do not carry over."""
    seed_warm_cache()
    entry = add_entry(hass)
    registry = er.async_get(hass)
    area = ar.async_get(hass).async_create("Garden")
    old = _old_switch(hass, entry)
    registry.async_update_entity(
        old.entity_id,
        area_id=area.id,
        disabled_by=er.RegistryEntryDisabler.INTEGRATION,
        hidden_by=er.RegistryEntryHider.INTEGRATION,
    )
    caplog.set_level(logging.INFO, logger="custom_components.eufy_home_security")

    assert await setup_entry(hass, entry)
    assert registry.async_get(old.entity_id) is None
    light = registry.async_get(_light_id(hass))
    assert light is not None
    assert light.entity_id == "light.front_spotlight"
    assert light.area_id == area.id
    assert light.name is None
    assert light.disabled_by is None
    assert light.hidden_by is None
    assert hass.states.get(light.entity_id) is not None
    assert len(_moved_lines(caplog)) == 1
    await _unload(hass, entry)


async def test_a_taken_light_id_gets_home_assistants_suffix(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Another integration's ``light.garden_lamp``: the moved light is ``light.garden_lamp_2``
    and the INFO line names that id."""
    seed_warm_cache()
    entry = add_entry(hass)
    registry = er.async_get(hass)
    taken = registry.async_get_or_create(
        LIGHT_DOMAIN, "other", "lamp", suggested_object_id="garden_lamp"
    )
    assert taken.entity_id == "light.garden_lamp"
    old = _old_switch(hass, entry)
    registry.async_update_entity(old.entity_id, new_entity_id="switch.garden_lamp")
    caplog.set_level(logging.INFO, logger="custom_components.eufy_home_security")

    assert await setup_entry(hass, entry)
    assert _light_id(hass) == "light.garden_lamp_2"
    assert registry.async_get(taken.entity_id) is not None
    (line,) = _moved_lines(caplog)
    assert line.endswith(
        "switch.garden_lamp moved to light.garden_lamp_2 with its name, area, "
        "labels, visibility and voice assistant exposure"
    )
    await _unload(hass, entry)


@pytest.mark.parametrize(
    ("switch_disabled_by", "light_disabled_by"),
    [
        (None, None),
        (er.RegistryEntryDisabler.INTEGRATION, er.RegistryEntryDisabler.INTEGRATION),
        (er.RegistryEntryDisabler.USER, er.RegistryEntryDisabler.USER),
    ],
)
async def test_a_moved_light_with_a_disabled_default_keeps_the_switchs_enabled_state(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    switch_disabled_by: er.RegistryEntryDisabler | None,
    light_disabled_by: er.RegistryEntryDisabler | None,
) -> None:
    """A light registered disabled by default: an enabled switch row gives an enabled light
    row, a user-disabled one a user-disabled one; one the integration disabled gives the
    light's own default, disabled by the integration."""
    monkeypatch.setattr(SettingEntitySpec, "enabled_by_default", property(lambda _: False))
    seed_warm_cache()
    entry = add_entry(hass)
    old = _old_switch(hass, entry)
    er.async_get(hass).async_update_entity(old.entity_id, disabled_by=switch_disabled_by)

    assert await setup_entry(hass, entry)
    light = er.async_get(hass).async_get(_light_id(hass))
    assert light is not None
    assert light.disabled_by is light_disabled_by
    await _unload(hass, entry)


@pytest.mark.parametrize(
    ("switch_disabled_by", "light_disabled_by"),
    [
        (None, None),
        (er.RegistryEntryDisabler.INTEGRATION, er.RegistryEntryDisabler.INTEGRATION),
    ],
)
async def test_the_entrys_disable_new_preference_leaves_an_enabled_switchs_light_enabled(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    switch_disabled_by: er.RegistryEntryDisabler | None,
    light_disabled_by: er.RegistryEntryDisabler | None,
) -> None:
    """With the entry's preference to disable new entities, an enabled switch row still gives
    an enabled light row; one the integration disabled gives a light disabled as a new
    entity of the entry is."""
    seed_warm_cache()
    entry = add_entry(hass)
    hass.config_entries.async_update_entry(entry, pref_disable_new_entities=True)
    old = _old_switch(hass, entry)
    er.async_get(hass).async_update_entity(old.entity_id, disabled_by=switch_disabled_by)

    assert await setup_entry(hass, entry)
    light = er.async_get(hass).async_get(_light_id(hass))
    assert light is not None
    assert light.disabled_by is light_disabled_by
    await _unload(hass, entry)


_ASSISTANTS: Final = ("conversation", "cloud.alexa", "cloud.google_assistant")


async def test_the_switchs_voice_assistant_exposure_carries_over(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A switch the user hid from Assist and exposed to Alexa and Google Assistant: the light
    is hidden from Assist (exposed by default) and exposed to both (not exposed by
    default)."""
    assert await async_setup_component(hass, "homeassistant", {})
    seed_warm_cache()
    entry = add_entry(hass)
    old = _old_switch(hass, entry)
    async_expose_entity(hass, "conversation", old.entity_id, False)
    async_expose_entity(hass, "cloud.alexa", old.entity_id, True)
    async_expose_entity(hass, "cloud.google_assistant", old.entity_id, True)

    assert await setup_entry(hass, entry)
    light_id = _light_id(hass)
    assert async_should_expose(hass, "conversation", light_id) is False
    assert async_should_expose(hass, "cloud.alexa", light_id) is True
    assert async_should_expose(hass, "cloud.google_assistant", light_id) is True
    await _unload(hass, entry)


async def test_a_switch_without_stored_exposure_gives_the_light_none(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """No exposure stored for the switch: none is stored for the light, so each assistant
    decides as for any new light (Assist exposes it, Alexa does not)."""
    assert await async_setup_component(hass, "homeassistant", {})
    seed_warm_cache()
    entry = add_entry(hass)
    _old_switch(hass, entry)

    assert await setup_entry(hass, entry)
    light_id = _light_id(hass)
    light = er.async_get(hass).async_get(light_id)
    assert light is not None
    assert not set(_ASSISTANTS) & set(light.options)
    assert async_should_expose(hass, "conversation", light_id) is True
    assert async_should_expose(hass, "cloud.alexa", light_id) is False
    await _unload(hass, entry)


async def _switch_as_x(
    hass: HomeAssistant, switch: str, target_domain: str = LIGHT_DOMAIN
) -> MockConfigEntry:
    """A loaded Switch as X helper making ``switch`` (entity id or registry id) a
    ``target_domain`` entity."""
    helper = MockConfigEntry(
        domain="switch_as_x",
        options={"entity_id": switch, "target_domain": target_domain, "invert": False},
        title="Light",
        version=1,
        minor_version=3,
    )
    helper.add_to_hass(hass)
    assert await hass.config_entries.async_setup(helper.entry_id)
    await hass.async_block_till_done()
    return helper


def _wrapper(hass: HomeAssistant, helper: MockConfigEntry) -> er.RegistryEntry:
    """The one entity of Switch as X helper ``helper``."""
    (wrapper,) = er.async_entries_for_config_entry(er.async_get(hass), helper.entry_id)
    return wrapper


def _errors(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]


def _taken_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "custom_components.eufy_home_security" and "took the place" in r.getMessage()
    ]


@pytest.mark.parametrize("by_registry_id", [False, True])
@pytest.mark.parametrize("helper_loaded", [True, False])
async def test_a_switch_as_x_helpers_exposure_carries_over_instead_of_the_switchs(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    by_registry_id: bool,
    helper_loaded: bool,
) -> None:
    """A Switch as X light wrapping the switch (named by entity id or registry id) holds the
    user's exposure and leaves the switch unexposed: the light gets that helper's exposure,
    not another helper's. A loaded helper is removed by Home Assistant with its switch and
    the light takes its id; one not loaded stays as it was and the light takes the next
    free id."""
    assert await async_setup_component(hass, "homeassistant", {})
    seed_warm_cache()
    entry = add_entry(hass)
    registry = er.async_get(hass)
    old = _old_switch(hass, entry)
    async_expose_entity(hass, "conversation", old.entity_id, False)
    async_expose_entity(hass, "cloud.alexa", old.entity_id, True)
    async_expose_entity(hass, "cloud.google_assistant", old.entity_id, True)
    await _switch_as_x(hass, "switch.other")
    helper = await _switch_as_x(hass, old.id if by_registry_id else old.entity_id)
    wrapper = _wrapper(hass, helper)
    assert wrapper.entity_id == "light.front_spotlight"
    assert async_should_expose(hass, "cloud.alexa", old.entity_id) is False
    if not helper_loaded:
        assert await hass.config_entries.async_unload(helper.entry_id)
        await hass.async_block_till_done()
        unloaded = registry.async_get(wrapper.entity_id)
        assert unloaded is not None
        wrapper = unloaded
    options = dict(helper.options)

    assert await setup_entry(hass, entry)
    light_id = _light_id(hass)
    assert light_id == ("light.front_spotlight" if helper_loaded else "light.front_spotlight_2")
    assert async_should_expose(hass, "conversation", light_id) is False
    assert async_should_expose(hass, "cloud.alexa", light_id) is True
    assert async_should_expose(hass, "cloud.google_assistant", light_id) is True
    if helper_loaded:
        assert hass.config_entries.async_get_entry(helper.entry_id) is None
        assert not er.async_entries_for_config_entry(registry, helper.entry_id)
    else:
        assert dict(helper.options) == options
        assert hass.config_entries.async_get_entry(helper.entry_id) is helper
        assert registry.async_get(wrapper.entity_id) == wrapper
    await _unload(hass, entry)


@pytest.mark.parametrize("by_registry_id", [False, True])
async def test_the_light_takes_the_place_of_a_loaded_switch_as_x_light(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    writes: list[tuple[str, Any]],
    caplog: pytest.LogCaptureFixture,
    by_registry_id: bool,
) -> None:
    """The helper's light is what the user's automations name: once Home Assistant removes
    the helper, the light holds its id with its name, icon, area, labels, aliases, hidden
    mark and exposure instead of the switch's; a call to that id switches the lamp; the
    other helper stays; a reload changes nothing."""
    assert await async_setup_component(hass, "homeassistant", {})
    seed_setting(fake_station, _SWITCH, 0)
    fake_station.reply_to_settings = True
    seed_warm_cache()
    entry = add_entry(hass)
    registry = er.async_get(hass)
    shed = ar.async_get(hass).async_create("Shed")
    garden = ar.async_get(hass).async_create("Garden")
    outdoor = lr.async_get(hass).async_create("Outdoor")
    old = _old_switch(hass, entry)
    old = registry.async_update_entity(
        old.entity_id, name="Switch name", icon="mdi:toggle-switch", area_id=shed.id
    )
    other = await _switch_as_x(hass, "switch.other")
    helper = await _switch_as_x(hass, old.id if by_registry_id else old.entity_id)
    wrapper = _wrapper(hass, helper)
    async_expose_entity(hass, "cloud.alexa", wrapper.entity_id, True)
    wrapper = registry.async_update_entity(
        wrapper.entity_id,
        name="Porch lamp",
        icon="mdi:lamp",
        area_id=garden.id,
        labels={outdoor.label_id},
        aliases=["yard light"],
        hidden_by=er.RegistryEntryHider.USER,
    )
    assert wrapper.entity_id == "light.front_spotlight"
    caplog.set_level(logging.INFO, logger="custom_components.eufy_home_security")
    events = async_capture_events(hass, er.EVENT_ENTITY_REGISTRY_UPDATED)

    assert await setup_entry(hass, entry)
    assert hass.config_entries.async_get_entry(helper.entry_id) is None
    assert hass.config_entries.async_get_entry(other.entry_id) is other
    light = registry.async_get(wrapper.entity_id)
    assert light is not None
    assert light.platform == DOMAIN
    assert light.config_entry_id == entry.entry_id
    assert light.unique_id == entity_unique_id(_CAMERA, _SWITCH)
    assert _light_id(hass) == wrapper.entity_id
    assert light.name == "Porch lamp"
    assert light.icon == "mdi:lamp"
    assert light.area_id == garden.id
    assert light.labels == {outdoor.label_id}
    assert light.aliases == ["yard light"]
    assert light.hidden_by is er.RegistryEntryHider.USER
    assert light.disabled_by is None
    assert light.device_id == old.device_id
    assert async_should_expose(hass, "cloud.alexa", wrapper.entity_id) is True
    assert registry.async_get("light.front_spotlight_2") is None
    (moved,) = _moved_lines(caplog)
    assert f"{old.entity_id} moved to light.front_spotlight_2 with" in moved
    (line,) = _taken_lines(caplog)
    assert redact_serial(_CAMERA) in line
    assert _CAMERA not in line
    assert "light.front_spotlight_2 took the place of the removed Switch as X light " in line
    assert f"Switch as X light {wrapper.entity_id} with" in line

    await _turn(hass, wrapper.entity_id, True)
    assert writes == [(_SWITCH, True)]
    assert _state_of(hass, wrapper.entity_id) == STATE_ON

    seen = len(events)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert [e.data for e in events[seen:] if e.data["action"] != "update"] == []
    assert _light_id(hass) == wrapper.entity_id
    assert len(_taken_lines(caplog)) == 1
    assert _errors(caplog) == []
    await _unload(hass, entry)


@pytest.mark.parametrize(
    ("wrapper_disabled_by", "light_disabled_by"),
    [
        (er.RegistryEntryDisabler.USER, er.RegistryEntryDisabler.USER),
        (er.RegistryEntryDisabler.INTEGRATION, None),
    ],
)
async def test_only_the_users_marks_of_the_switch_as_x_light_carry_over(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    wrapper_disabled_by: er.RegistryEntryDisabler,
    light_disabled_by: er.RegistryEntryDisabler | None,
) -> None:
    """The helper's light disabled and hidden, over a switch the user hid: the light takes
    its id, disabled only when the user disabled the helper's light, and shown (neither
    the switch's mark nor one the user did not set carries over)."""
    seed_warm_cache()
    entry = add_entry(hass)
    registry = er.async_get(hass)
    old = _old_switch(hass, entry)
    helper = await _switch_as_x(hass, old.entity_id)
    registry.async_update_entity(old.entity_id, hidden_by=er.RegistryEntryHider.USER)
    wrapper = registry.async_update_entity(
        _wrapper(hass, helper).entity_id,
        disabled_by=wrapper_disabled_by,
        hidden_by=er.RegistryEntryHider.INTEGRATION,
    )

    assert await setup_entry(hass, entry)
    light = registry.async_get(wrapper.entity_id)
    assert light is not None
    assert light.platform == DOMAIN
    assert light.disabled_by is light_disabled_by
    assert light.hidden_by is None
    await _unload(hass, entry)


@pytest.mark.parametrize("by_registry_id", [False, True])
async def test_a_not_loaded_switch_as_x_light_keeps_its_id_also_when_deleted_later(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    caplog: pytest.LogCaptureFixture,
    by_registry_id: bool,
) -> None:
    """A helper that is not loaded stays: the light keeps the next free id and the switch's
    name, also after the user deletes the helper."""
    seed_warm_cache()
    entry = add_entry(hass)
    registry = er.async_get(hass)
    old = _old_switch(hass, entry)
    registry.async_update_entity(old.entity_id, name="Switch name")
    helper = await _switch_as_x(hass, old.id if by_registry_id else old.entity_id)
    wrapper = registry.async_update_entity(_wrapper(hass, helper).entity_id, name="Porch lamp")
    assert await hass.config_entries.async_unload(helper.entry_id)
    await hass.async_block_till_done()
    caplog.set_level(logging.INFO, logger="custom_components.eufy_home_security")

    assert await setup_entry(hass, entry)
    assert hass.config_entries.async_get_entry(helper.entry_id) == helper
    assert _light_id(hass) == "light.front_spotlight_2"
    await hass.config_entries.async_remove(helper.entry_id)
    await hass.async_block_till_done()
    assert registry.async_get(wrapper.entity_id) is None
    light = registry.async_get(_light_id(hass))
    assert light is not None
    assert light.entity_id == "light.front_spotlight_2"
    assert light.name == "Switch name"
    assert _taken_lines(caplog) == []
    await _unload(hass, entry)


async def test_a_switch_as_x_entity_of_another_domain_gives_no_place_to_take(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A loaded helper making the switch a fan: Home Assistant removes it, and the light keeps
    the switch's id and name, with no error logged."""
    assert await async_setup_component(hass, "homeassistant", {})
    seed_warm_cache()
    entry = add_entry(hass)
    registry = er.async_get(hass)
    old = _old_switch(hass, entry)
    registry.async_update_entity(old.entity_id, name="Switch name")
    helper = await _switch_as_x(hass, old.entity_id, Platform.FAN)
    wrapper = registry.async_update_entity(_wrapper(hass, helper).entity_id, name="Fan name")
    assert wrapper.entity_id == "fan.front_spotlight"
    caplog.set_level(logging.INFO, logger="custom_components.eufy_home_security")

    assert await setup_entry(hass, entry)
    assert hass.config_entries.async_get_entry(helper.entry_id) is None
    light = registry.async_get(_light_id(hass))
    assert light is not None
    assert light.entity_id == "light.front_spotlight"
    assert light.name == "Switch name"
    assert _taken_lines(caplog) == []
    assert _errors(caplog) == []
    await _unload(hass, entry)


@pytest.mark.parametrize(
    ("meanwhile", "taken"),
    [
        ("helper light updated", "light.front_spotlight"),
        ("helper light renamed", "light.porch"),
        ("area and label deleted", "light.front_spotlight"),
        ("entry unloaded", None),
        ("light removed", None),
    ],
)
async def test_the_place_is_taken_when_home_assistant_removes_the_helper(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    caplog: pytest.LogCaptureFixture,
    meanwhile: str,
    taken: str | None,
) -> None:
    """The helper's removal held past setup: the light keeps the next free id until Home
    Assistant removes the helper's light. What the user changes on that light in the
    meantime is taken, a new entity id too (also when a row takes the old id and goes
    again before); an area or label deleted in the meantime is not; an entry unloaded or a
    light row removed in the meantime takes no place."""
    assert await async_setup_component(hass, "homeassistant", {})
    seed_warm_cache()
    entry = add_entry(hass)
    registry = er.async_get(hass)
    garden = ar.async_get(hass).async_create("Garden")
    outdoor = lr.async_get(hass).async_create("Outdoor")
    old = _old_switch(hass, entry)
    helper = await _switch_as_x(hass, old.entity_id)
    wrapper = registry.async_update_entity(
        _wrapper(hass, helper).entity_id,
        name="Porch lamp",
        area_id=garden.id,
        labels={outdoor.label_id},
    )

    async with helper.setup_lock:
        assert await hass.config_entries.async_setup(entry.entry_id)
        for _ in range(5):
            await asyncio.sleep(0)
        assert hass.config_entries.async_get_entry(helper.entry_id) == helper
        assert _light_id(hass) == "light.front_spotlight_2"
        if meanwhile == "helper light updated":
            registry.async_update_entity(wrapper.entity_id, name="Yard lamp", icon="mdi:lamp")
        elif meanwhile == "helper light renamed":
            registry.async_update_entity(
                wrapper.entity_id, new_entity_id="light.porch", name="Yard lamp", icon="mdi:lamp"
            )
            other = registry.async_get_or_create(
                LIGHT_DOMAIN, "other", "lamp", suggested_object_id="front_spotlight"
            )
            assert other.entity_id == wrapper.entity_id
            registry.async_remove(other.entity_id)
        elif meanwhile == "area and label deleted":
            ar.async_get(hass).async_delete(garden.id)
            lr.async_get(hass).async_delete(outdoor.label_id)
        elif meanwhile == "entry unloaded":
            assert await hass.config_entries.async_unload(entry.entry_id)
        else:
            registry.async_remove("light.front_spotlight_2")
    await hass.async_block_till_done()

    assert hass.config_entries.async_get_entry(helper.entry_id) is None
    if taken is None:
        assert registry.async_get(wrapper.entity_id) is None
    else:
        held = registry.async_get(taken)
        assert held is not None
        assert held.platform == DOMAIN
        assert _light_id(hass) == taken
        if meanwhile == "area and label deleted":
            assert (held.name, held.icon) == ("Porch lamp", None)
            assert (held.area_id, held.labels) == (None, set())
        else:
            assert (held.name, held.icon) == ("Yard lamp", "mdi:lamp")
            assert (held.area_id, held.labels) == (garden.id, {outdoor.label_id})
    assert _errors(caplog) == []
    if entry.state is ConfigEntryState.LOADED:
        await _unload(hass, entry)


async def test_the_light_keeps_its_id_once_it_has_taken_the_place(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """After the light took the helper light's id, the user renames it, and another row is
    registered at the old id and removed: the light keeps its new id."""
    assert await async_setup_component(hass, "homeassistant", {})
    seed_warm_cache()
    entry = add_entry(hass)
    registry = er.async_get(hass)
    old = _old_switch(hass, entry)
    helper = await _switch_as_x(hass, old.entity_id)
    target = _wrapper(hass, helper).entity_id
    assert await setup_entry(hass, entry)
    assert _light_id(hass) == target

    registry.async_update_entity(target, new_entity_id="light.porch")
    other = registry.async_get_or_create(
        LIGHT_DOMAIN, "other", "lamp", suggested_object_id=split_entity_id(target)[1]
    )
    assert other.entity_id == target
    registry.async_remove(target)
    await hass.async_block_till_done()
    assert _light_id(hass) == "light.porch"
    await _unload(hass, entry)


async def test_the_place_of_a_switch_as_x_light_being_set_up_is_taken(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A helper still being set up when the switch is removed: Home Assistant removes it once
    its setup ends, and the light takes its light's id and name."""
    assert await async_setup_component(hass, "homeassistant", {})
    seed_warm_cache()
    entry = add_entry(hass)
    registry = er.async_get(hass)
    old = _old_switch(hass, entry)
    helper = await _switch_as_x(hass, old.entity_id)
    wrapper = registry.async_update_entity(_wrapper(hass, helper).entity_id, name="Porch lamp")
    assert await hass.config_entries.async_unload(helper.entry_id)
    await hass.async_block_till_done()
    release = asyncio.Event()
    real = switch_as_x_light.async_setup_entry

    async def held(*args: Any) -> None:
        await release.wait()
        await real(*args)

    monkeypatch.setattr(switch_as_x_light, "async_setup_entry", held)
    # Not a tracked task: Home Assistant's removal of the helper waits for this setup.
    setting_up = asyncio.get_running_loop().create_task(
        hass.config_entries.async_setup(helper.entry_id)
    )
    for _ in range(20):
        await asyncio.sleep(0)
    assert helper.state is ConfigEntryState.SETUP_IN_PROGRESS

    assert await hass.config_entries.async_setup(entry.entry_id)
    for _ in range(5):
        await asyncio.sleep(0)
    assert _light_id(hass) == "light.front_spotlight_2"
    release.set()
    assert await setting_up
    await hass.async_block_till_done()

    assert hass.config_entries.async_get_entry(helper.entry_id) is None
    light = registry.async_get(wrapper.entity_id)
    assert light is not None
    assert light.platform == DOMAIN
    assert light.name == "Porch lamp"
    await _unload(hass, entry)


@pytest.mark.parametrize("second", [LIGHT_DOMAIN, Platform.LOCK])
async def test_a_switch_as_x_light_is_preferred_to_another_domains_helper(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    second: str,
) -> None:
    """A fan helper made first and a second helper both wrap the switch, each with its own
    exposure. A light helper wins: the light takes its id, name and exposure. A lock helper
    does not: the light keeps the switch's id and gets the first helper's exposure. Home
    Assistant removes both helpers."""
    assert await async_setup_component(hass, "homeassistant", {})
    seed_warm_cache()
    entry = add_entry(hass)
    registry = er.async_get(hass)
    old = _old_switch(hass, entry)
    fan = await _switch_as_x(hass, old.entity_id, Platform.FAN)
    async_expose_entity(hass, "cloud.alexa", _wrapper(hass, fan).entity_id, second == Platform.LOCK)
    helper = await _switch_as_x(hass, old.entity_id, second)
    wrapper = registry.async_update_entity(_wrapper(hass, helper).entity_id, name="Porch lamp")
    async_expose_entity(hass, "cloud.alexa", wrapper.entity_id, second == LIGHT_DOMAIN)

    assert await setup_entry(hass, entry)
    assert hass.config_entries.async_get_entry(fan.entry_id) is None
    assert hass.config_entries.async_get_entry(helper.entry_id) is None
    light = registry.async_get(_light_id(hass))
    assert light is not None
    if second == LIGHT_DOMAIN:
        assert light.entity_id == wrapper.entity_id == "light.front_spotlight"
        assert light.name == "Porch lamp"
    else:
        assert light.entity_id == "light.front_spotlight"
        assert light.name is None
    assert async_should_expose(hass, "cloud.alexa", light.entity_id) is True
    await _unload(hass, entry)


@pytest.mark.parametrize("by", ["state", "row"])
async def test_an_id_taken_when_the_helper_goes_is_left_alone(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    caplog: pytest.LogCaptureFixture,
    by: str,
) -> None:
    """Another state or registry row holding the helper light's id the moment it is removed:
    the light keeps the next free id, with no error logged, also once that row is gone."""
    assert await async_setup_component(hass, "homeassistant", {})
    seed_warm_cache()
    entry = add_entry(hass)
    registry = er.async_get(hass)
    old = _old_switch(hass, entry)
    helper = await _switch_as_x(hass, old.entity_id)
    target = _wrapper(hass, helper).entity_id

    @callback
    def take(event: Event[er.EventEntityRegistryUpdatedData]) -> None:
        if event.data["action"] != "remove":
            return
        if by == "state":
            hass.states.async_set(target, STATE_ON)
        else:
            registry.async_get_or_create(
                LIGHT_DOMAIN, "other", "lamp", suggested_object_id=split_entity_id(target)[1]
            )

    unsubscribe = async_track_entity_registry_updated_event(hass, target, take)
    assert await setup_entry(hass, entry)
    unsubscribe()
    assert hass.config_entries.async_get_entry(helper.entry_id) is None
    assert _light_id(hass) == "light.front_spotlight_2"
    if by == "row":
        registry.async_remove(target)
        await hass.async_block_till_done()
        assert _light_id(hass) == "light.front_spotlight_2"
    assert _errors(caplog) == []
    await _unload(hass, entry)


async def test_an_area_and_label_deleted_as_the_helper_goes_are_left_out(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The helper light's area and label deleted by another listener of its removal: the
    light takes its id and name without them, with no error logged."""
    assert await async_setup_component(hass, "homeassistant", {})
    seed_warm_cache()
    entry = add_entry(hass)
    registry = er.async_get(hass)
    garden = ar.async_get(hass).async_create("Garden")
    outdoor = lr.async_get(hass).async_create("Outdoor")
    old = _old_switch(hass, entry)
    helper = await _switch_as_x(hass, old.entity_id)
    wrapper = registry.async_update_entity(
        _wrapper(hass, helper).entity_id,
        name="Porch lamp",
        area_id=garden.id,
        labels={outdoor.label_id},
    )

    @callback
    def delete(event: Event[er.EventEntityRegistryUpdatedData]) -> None:
        if event.data["action"] == "remove":
            ar.async_get(hass).async_delete(garden.id)
            lr.async_get(hass).async_delete(outdoor.label_id)

    unsubscribe = async_track_entity_registry_updated_event(hass, wrapper.entity_id, delete)
    assert await setup_entry(hass, entry)
    unsubscribe()
    light = registry.async_get(wrapper.entity_id)
    assert light is not None
    assert light.platform == DOMAIN
    assert light.name == "Porch lamp"
    assert light.area_id is None
    assert light.labels == set()
    assert _errors(caplog) == []
    await _unload(hass, entry)


async def test_a_read_only_lamps_switch_row_gives_no_light_row(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A lamp whose switch is read-only here builds no light: its switch row moves nowhere,
    no light row is registered, and its sensor is built."""
    _as_model(fake_cloud, "T84A1")
    seed_warm_cache()
    entry = add_entry(hass)
    registry = er.async_get(hass)
    _old_switch(hass, entry)
    created = async_capture_events(hass, er.EVENT_ENTITY_REGISTRY_UPDATED)
    caplog.set_level(logging.INFO, logger="custom_components.eufy_home_security")

    assert await setup_entry(hass, entry)
    unique_id = entity_unique_id(_CAMERA, _SWITCH)
    assert registry.async_get_entity_id(Platform.BINARY_SENSOR, DOMAIN, unique_id) is not None
    assert not [
        e
        for e in created
        if e.data["action"] == "create" and e.data["entity_id"].startswith("light.")
    ]
    assert _moved_lines(caplog) == []
    await _unload(hass, entry)


async def test_another_entrys_switch_row_is_not_moved(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A switch row of the lamp's unique id held by another entry stays as it is, and the
    light row takes none of its settings."""
    seed_warm_cache()
    entry = add_entry(hass)
    other = MockConfigEntry(
        domain=DOMAIN, title="other", unique_id="other", disabled_by=ConfigEntryDisabler.USER
    )
    other.add_to_hass(hass)
    registry = er.async_get(hass)
    foreign = registry.async_get_or_create(
        Platform.SWITCH, DOMAIN, entity_unique_id(_CAMERA, _SWITCH), config_entry=other
    )
    foreign = registry.async_update_entity(foreign.entity_id, name="Other", icon="mdi:lamp")
    caplog.set_level(logging.INFO, logger="custom_components.eufy_home_security")

    assert await setup_entry(hass, entry)
    kept = registry.async_get(foreign.entity_id)
    assert kept is not None
    assert kept.config_entry_id == other.entry_id
    light = registry.async_get(_light_id(hass))
    assert light is not None
    assert light.name is None
    assert light.icon is None
    assert _moved_lines(caplog) == []
    await _unload(hass, entry)


async def test_an_existing_light_row_is_left_as_it_is(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A light row already there keeps its own settings; the switch row next to it is only
    removed."""
    seed_warm_cache()
    entry = add_entry(hass)
    registry = er.async_get(hass)
    old = _old_switch(hass, entry)
    registry.async_update_entity(old.entity_id, name="Switch name", icon="mdi:lamp")
    light = registry.async_get_or_create(
        LIGHT_DOMAIN,
        DOMAIN,
        entity_unique_id(_CAMERA, _SWITCH),
        config_entry=entry,
        suggested_object_id="porch",
    )
    registry.async_update_entity(light.entity_id, name="Kept")
    caplog.set_level(logging.INFO, logger="custom_components.eufy_home_security")

    assert await setup_entry(hass, entry)
    assert registry.async_get(old.entity_id) is None
    kept = registry.async_get(light.entity_id)
    assert kept is not None
    assert kept.entity_id == "light.porch"
    assert kept.name == "Kept"
    assert kept.icon is None
    assert _moved_lines(caplog) == []
    await _unload(hass, entry)


# ── writes ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("brightness", "level", "raw"),
    [
        # 40 %: Low; the lower level on a tie between Low and Medium
        (102, 0, "40"),
        (140, 0, "40"),
        (141, 1, "70"),
        (191, 1, "70"),
        (255, 2, "100"),
        # 2 %: the lowest the app writes, the nearest level
        (4, 0, "40"),
    ],
)
async def test_turning_an_off_lamp_on_at_a_brightness_writes_the_level_then_on(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    writes: list[tuple[str, Any]],
    brightness: int,
    level: int,
    raw: str,
) -> None:
    """Brightness first (the nearest of the lamp's levels), then on; both reach the camera."""
    seed_setting(fake_station, _SWITCH, 0)
    seed_setting(fake_station, _BRIGHTNESS, 100 if level != 2 else 40)
    fake_station.reply_to_settings = True
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = _light_id(hass)
    assert _state_of(hass, entity_id) == STATE_OFF

    await _turn(hass, entity_id, True, **{ATTR_BRIGHTNESS: brightness})

    assert writes == [(_BRIGHTNESS, level), (_SWITCH, True)]
    assert (_param(fake_station, _BRIGHTNESS), _param(fake_station, _SWITCH)) == (raw, "1")
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == STATE_ON
    assert state.attributes[ATTR_BRIGHTNESS] == value_to_brightness((1, 100), int(raw))
    await _unload(hass, entry)


async def test_a_brightness_for_a_lamp_that_is_on_writes_only_the_brightness(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    writes: list[tuple[str, Any]],
) -> None:
    seed_setting(fake_station, _SWITCH, 1)
    seed_setting(fake_station, _BRIGHTNESS, 40)
    fake_station.reply_to_settings = True
    entry = await set_up_warm(hass, seed_warm_cache)

    await _turn(hass, _light_id(hass), True, **{ATTR_BRIGHTNESS: 255})
    assert writes == [(_BRIGHTNESS, 2)]
    await _unload(hass, entry)


@pytest.mark.parametrize("brightness", [1, 3])
async def test_a_brightness_of_one_percent_or_less_turns_the_lamp_off(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    writes: list[tuple[str, Any]],
    brightness: int,
) -> None:
    """As the eufy app does: no brightness written, the lamp turned off."""
    seed_setting(fake_station, _SWITCH, 1)
    fake_station.reply_to_settings = True
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = _light_id(hass)

    await _turn(hass, entity_id, True, **{ATTR_BRIGHTNESS: brightness})
    assert writes == [(_SWITCH, False)]
    assert _param(fake_station, _SWITCH) == "0"
    assert _state_of(hass, entity_id) == STATE_OFF
    await _unload(hass, entry)


async def test_on_and_off_without_a_brightness_write_only_the_switch(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    writes: list[tuple[str, Any]],
) -> None:
    seed_setting(fake_station, _SWITCH, 0)
    fake_station.reply_to_settings = True
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = _light_id(hass)

    await _turn(hass, entity_id, True)
    assert _param(fake_station, _SWITCH) == "1"
    assert _state_of(hass, entity_id) == STATE_ON
    await _turn(hass, entity_id, False)
    assert _param(fake_station, _SWITCH) == "0"
    assert _state_of(hass, entity_id) == STATE_OFF
    assert writes == [(_SWITCH, True), (_SWITCH, False)]
    await _unload(hass, entry)


@pytest.mark.parametrize(
    ("brightness", "value"),
    [(128, 50), (255, 100), (5, 2), (4, 2), (1, None)],
)
async def test_a_range_brightness_writes_the_percent_from_two(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    writes: list[tuple[str, Any]],
    brightness: int,
    value: int | None,
) -> None:
    """A T8170's 1-100 % range: the percent itself, at least 2; 1 % or less turns it off.
    The light shows the percent the camera reports."""
    _as_model(fake_cloud, "T8170")
    seed_setting(fake_station, _SWITCH, 0, model="T8170")
    seed_setting(fake_station, _BRIGHTNESS, 30, model="T8170")
    fake_station.reply_to_settings = True
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = _light_id(hass)
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.attributes[ATTR_SUPPORTED_COLOR_MODES] == [ColorMode.BRIGHTNESS]

    await _turn(hass, entity_id, True, **{ATTR_BRIGHTNESS: brightness})

    if value is None:
        assert writes == [(_SWITCH, False)]
        assert _state_of(hass, entity_id) == STATE_OFF
    else:
        assert writes == [(_BRIGHTNESS, value), (_SWITCH, True)]
        assert _param(fake_station, _BRIGHTNESS, "T8170") == str(value)
        state = hass.states.get(entity_id)
        assert state is not None
        assert state.state == STATE_ON
        assert state.attributes[ATTR_BRIGHTNESS] == value_to_brightness((1, 100), value)
    await _unload(hass, entry)


# ── the lamp control's enabling setting ─────────────────────────────────────


@pytest.mark.parametrize("on", [True, False])
async def test_a_switch_is_refused_while_lighting_is_off(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    writes: list[tuple[str, Any]],
    on: bool,
) -> None:
    """The eufy app disables the lamp's control while Lighting is off: nothing is sent and
    the error names both settings; the light stays shown."""
    _as_model(fake_cloud, "T8170")
    seed_setting(fake_station, _SWITCH, 0 if on else 1, model="T8170")
    seed_setting(fake_station, _LIGHTING, 0, model="T8170")
    fake_station.reply_to_settings = True
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = _light_id(hass)
    before = _state_of(hass, entity_id)
    sent = len(fake_station.received)

    with pytest.raises(ServiceValidationError) as raised:
        await _turn(hass, entity_id, on, **({ATTR_BRIGHTNESS: 128} if on else {}))

    assert raised.value.translation_domain == DOMAIN
    assert raised.value.translation_key == _CONTROL_OFF
    assert raised.value.translation_placeholders == {"target": "Light", "setting": "Lighting"}
    assert writes == []
    assert len(fake_station.received) == sent
    assert _state_of(hass, entity_id) == before
    await _unload(hass, entry)


@pytest.mark.parametrize("lighting", [1, None])
async def test_a_switch_is_sent_while_lighting_is_on_or_unknown(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    writes: list[tuple[str, Any]],
    lighting: int | None,
) -> None:
    _as_model(fake_cloud, "T8170")
    seed_setting(fake_station, _SWITCH, 0, model="T8170")
    if lighting is not None:
        seed_setting(fake_station, _LIGHTING, lighting, model="T8170")
    fake_station.reply_to_settings = True
    entry = await set_up_warm(hass, seed_warm_cache)

    await _turn(hass, _light_id(hass), True)
    assert writes == [(_SWITCH, True)]
    await _unload(hass, entry)


# ── assumed state ───────────────────────────────────────────────────────────


async def test_a_lamp_the_station_does_not_report_is_assumed(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A switch read from another param (``state_reported`` False) is shown as read, as
    an assumed state."""
    _replace_lights(monkeypatch, state_reported=False)
    seed_setting(fake_station, _SWITCH, 1)
    entry = await set_up_warm(hass, seed_warm_cache)
    state = hass.states.get(_light_id(hass))
    assert state is not None
    assert state.state == STATE_ON
    assert state.attributes.get(ATTR_ASSUMED_STATE) is True
    await _unload(hass, entry)


async def test_a_write_only_lamp_shows_what_it_wrote_and_unknown_after_a_timeout(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A switch the library cannot read: assumed, unknown until written here, then the
    written value through polls; a write that timed out shows unknown again."""
    _replace_setting(monkeypatch, _SWITCH, readable=False)
    seed_setting(fake_station, _SWITCH, 1)
    fake_station.reply_to_settings = True
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = _light_id(hass)
    coordinator = entry.runtime_data.coordinators[SYNTHETIC.station_sn]
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == STATE_UNKNOWN
    assert state.attributes.get(ATTR_ASSUMED_STATE) is True

    await _turn(hass, entity_id, False)
    assert _state_of(hass, entity_id) == STATE_OFF
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert _state_of(hass, entity_id) == STATE_OFF

    async def timed_out(*args: object, **kwargs: object) -> None:
        raise DeviceTimeoutError("no answer")

    monkeypatch.setattr(coordinator.station, "async_set_setting", timed_out)
    with pytest.raises(HomeAssistantError) as raised:
        await _turn(hass, entity_id, True)
    assert raised.value.translation_key == _UNCONFIRMED
    assert _state_of(hass, entity_id) == STATE_UNKNOWN
    # A dump that changes the station's state settles the write; the value written
    # before it is no longer known to hold.
    assert _param(fake_station, _SWITCH) == "0"
    seed_setting(fake_station, _SWITCH, 1)
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert _state_of(hass, entity_id) == STATE_UNKNOWN
    await _unload(hass, entry)


async def test_a_write_only_brightness_and_switch_each_show_what_was_written(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    writes: list[tuple[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neither readable: a turn-on at a brightness writes both (the lamp is not known to be
    on), and the light shows each written value."""
    _replace_setting(monkeypatch, _SWITCH, readable=False)
    _replace_setting(monkeypatch, _BRIGHTNESS, readable=False)
    seed_setting(fake_station, _BRIGHTNESS, 40)
    fake_station.reply_to_settings = True
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = _light_id(hass)
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == STATE_UNKNOWN
    assert state.attributes.get(ATTR_BRIGHTNESS) is None

    await _turn(hass, entity_id, True, **{ATTR_BRIGHTNESS: 255})

    assert writes == [(_BRIGHTNESS, 2), (_SWITCH, True)]
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == STATE_ON
    assert state.attributes[ATTR_BRIGHTNESS] == 255
    # A dump with another level leaves the written brightness shown.
    seed_setting(fake_station, _BRIGHTNESS, 40)
    await entry.runtime_data.coordinators[SYNTHETIC.station_sn].async_refresh()
    await hass.async_block_till_done()
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.attributes[ATTR_BRIGHTNESS] == 255
    await _unload(hass, entry)


async def test_a_timed_out_switch_write_keeps_the_written_brightness(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neither readable: a turn-on whose brightness write lands and whose switch write
    times out leaves the switch unknown and the written brightness shown after a dump."""
    _replace_setting(monkeypatch, _SWITCH, readable=False)
    _replace_setting(monkeypatch, _BRIGHTNESS, readable=False)
    fake_station.reply_to_settings = True
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = _light_id(hass)
    coordinator = entry.runtime_data.coordinators[SYNTHETIC.station_sn]
    real = coordinator.station.async_set_setting

    async def switch_times_out(key: str, value: Any, **kwargs: Any) -> None:
        if key == _SWITCH:
            raise DeviceTimeoutError("no answer")
        await real(key, value, **kwargs)

    monkeypatch.setattr(coordinator.station, "async_set_setting", switch_times_out)
    with pytest.raises(HomeAssistantError) as raised:
        await _turn(hass, entity_id, True, **{ATTR_BRIGHTNESS: 255})
    assert raised.value.translation_key == _UNCONFIRMED
    assert _param(fake_station, _BRIGHTNESS) == "100"

    # A dump that changes the station's state settles the write.
    seed_setting(fake_station, _BRIGHTNESS, 40)
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    light = hass.data["entity_components"][LIGHT_DOMAIN].get_entity(entity_id)
    assert isinstance(light, LightEntity)
    assert light.is_on is None
    assert light.brightness == 255
    await _unload(hass, entry)


async def test_a_write_only_brightness_of_a_readable_switch_shows_what_was_written(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The written value is kept by the written setting's readability, not the switch's."""
    _replace_setting(monkeypatch, _BRIGHTNESS, readable=False)
    seed_setting(fake_station, _SWITCH, 0)
    fake_station.reply_to_settings = True
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = _light_id(hass)

    await _turn(hass, entity_id, True, **{ATTR_BRIGHTNESS: 255})

    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == STATE_ON
    assert state.attributes[ATTR_BRIGHTNESS] == 255
    await _unload(hass, entry)
