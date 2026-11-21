"""Camera lamps as lights (``Station.lights_for``).

The fixture camera is a T8160 behind a HomeBase 3: an enum brightness (Low, Medium, High
= 40, 70, 100 %). Made a T8170 there, it has a 1-100 % range and a lamp control enabled
by its Lighting setting; made a T84A1, its lamp switch is read-only.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import Any, Final

import pytest
from conftest import add_entry, entity_id_for, seed_setting, set_up_warm, setting_param, setup_entry
from conftest import state_of as _state_of
from eufy_home_security import DeviceTimeoutError, EufySecurity, Light, Station, entity_unique_id
from eufy_home_security.devices import Setting
from eufy_home_security.testing import SYNTHETIC, FakeCloud, FakeStation
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_MODE,
    ATTR_SUPPORTED_COLOR_MODES,
    ColorMode,
    LightEntity,
)
from homeassistant.components.light import DOMAIN as LIGHT_DOMAIN
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
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.util.color import value_to_brightness
from pytest_homeassistant.common import MockConfigEntry, async_capture_events

from custom_components.eufy_home_security.const import DOMAIN
from custom_components.eufy_home_security.settings import setting_specs

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
