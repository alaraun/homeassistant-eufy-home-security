"""Enum settings as select entities: library labels as options, values on the wire."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Final

import pytest
from conftest import (
    entity_id_for,
    record_states,
    seed_setting,
    set_up_warm,
    setting_param,
    state_of,
)
from eufy_home_security import EufySecurity
from eufy_home_security.devices.model_settings import settings_of
from eufy_home_security.devices.timezones import encode_zone
from eufy_home_security.testing import SYNTHETIC, FakeStation
from homeassistant.components.select import ATTR_OPTION, ATTR_OPTIONS, SERVICE_SELECT_OPTION
from homeassistant.components.select import DOMAIN as SELECT_DOMAIN
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE, STATE_UNKNOWN, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.eufy_home_security.const import DOMAIN

# On the camera, written as a 1350 sub-command whose payload names the channel.
_NIGHT_VISION: Final = "nightvision_type_new"
# On the camera, an ECB scalar: the fake answers a foreign account with -104.
_WATERMARK: Final = "watermark_set"
# On the station itself (channel 255).
_ALARM_TONE: Final = "alarm_tones_mode"
# Its wire values map to other public values (wire 2 = custom, 3).
_POWER_MODE: Final = "power_manager_mode"
# The station's time zone: IANA ids, sent as the device's "<POSIX rule>|1.<row>" form.
_TIMEZONE: Final = "timezone_set"

_CAMERA_CHANNEL: Final = 0
_STATION_CHANNEL: Final = 255
_NOT_APPLIED: Final = "setting_not_applied"


async def select_option(hass: HomeAssistant, entity_id: str, option: str) -> None:
    """Pick an option on a select entity and wait for it, as a dashboard does."""
    await hass.services.async_call(
        SELECT_DOMAIN,
        SERVICE_SELECT_OPTION,
        {ATTR_ENTITY_ID: entity_id, ATTR_OPTION: option},
        blocking=True,
    )


def _param(station: FakeStation, key: str, channel: int, model: str = "T8160") -> Any:
    """The raw value the fake station holds for a setting, or None."""
    return station.params.get(channel, {}).get(setting_param(key, model=model))


async def test_a_camera_select_shows_the_library_labels_and_writes_the_value(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Options are the library's labels in its order; a pick writes that label's value.

    Night vision's public values (100 Off, 102 Infrared, 103 Spotlights) map to wire codes
    0 / 1 / 2: the library's codec does it both ways.
    """
    seed_setting(fake_station, _NIGHT_VISION, 1)  # wire code of 102
    fake_station.reply_to_settings = True  # a 1350 write's result, as the station sends
    entry = await set_up_warm(hass, seed_warm_cache)

    entity_id = entity_id_for(hass, SELECT_DOMAIN, SYNTHETIC.camera_sn, _NIGHT_VISION)
    setting = settings_of("T8160")[_NIGHT_VISION]
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == setting.label(102)
    assert state.attributes[ATTR_OPTIONS] == [setting.label(v) for v in setting.values]

    shown = record_states(hass, entity_id)
    await select_option(hass, entity_id, str(setting.label(103)))
    await hass.async_block_till_done()

    assert _param(fake_station, _NIGHT_VISION, _CAMERA_CHANNEL) == "2"
    assert state_of(hass, entity_id) == setting.label(103)
    assert shown == [setting.label(103)]

    registered = er.async_get(hass).async_get(entity_id)
    assert registered is not None
    assert registered.entity_category is EntityCategory.CONFIG
    assert registered.translation_key is None
    assert registered.original_name == setting.name

    from custom_components.eufy_home_security import select

    assert select.PARALLEL_UPDATES == 1

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_mapped_enum_reads_and_writes_through_the_library_codec(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Wire 2 shows as custom recording (3), and picking it writes wire 2 again."""
    seed_setting(fake_station, _POWER_MODE, 1)
    entry = await set_up_warm(hass, seed_warm_cache)
    setting = settings_of("T8160")[_POWER_MODE]
    entity_id = entity_id_for(hass, SELECT_DOMAIN, SYNTHETIC.camera_sn, _POWER_MODE)
    assert state_of(hass, entity_id) == setting.label(1)

    await select_option(hass, entity_id, str(setting.label(3)))
    await hass.async_block_till_done()

    assert _param(fake_station, _POWER_MODE, _CAMERA_CHANNEL) == "2"
    assert state_of(hass, entity_id) == setting.label(3)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_station_select_is_read_and_written_on_the_station_block(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A station setting addresses no device: channel 255 for the read and the write."""
    seed_setting(fake_station, _ALARM_TONE, 1, channel=_STATION_CHANNEL)
    fake_station.reply_to_settings = True
    entry = await set_up_warm(hass, seed_warm_cache)
    setting = settings_of("T8030")[_ALARM_TONE]

    entity_id = entity_id_for(hass, SELECT_DOMAIN, SYNTHETIC.station_sn, _ALARM_TONE)
    assert state_of(hass, entity_id) == setting.label(1)

    await select_option(hass, entity_id, str(setting.label(2)))
    await hass.async_block_till_done()

    assert fake_station.received[-1]["mChannel"] == _STATION_CHANNEL
    assert state_of(hass, entity_id) == setting.label(2)
    assert _param(fake_station, _ALARM_TONE, _CAMERA_CHANNEL, model="T8030") is None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_the_time_zone_is_a_select_of_zone_ids_written_in_the_device_form(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The options are the library's IANA ids; the pick reaches the station in its own form."""
    seed_setting(fake_station, _TIMEZONE, encode_zone("Europe/Tallinn"), channel=_STATION_CHANNEL)
    fake_station.reply_to_settings = True
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = entity_id_for(hass, SELECT_DOMAIN, SYNTHETIC.station_sn, _TIMEZONE)
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "Europe/Tallinn"
    assert state.attributes[ATTR_OPTIONS] == list(settings_of("T8030")[_TIMEZONE].values)

    await select_option(hass, entity_id, "Europe/Helsinki")
    await hass.async_block_till_done()

    assert fake_station.string_commands_received[-1] == (
        1215,
        _STATION_CHANNEL,
        encode_zone("Europe/Helsinki"),
    )
    assert state_of(hass, entity_id) == "Europe/Helsinki"
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_select_write_the_station_rejects_keeps_the_last_option(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A rejected write raises the translated error and leaves the option where it was."""
    setting = settings_of("T8160")[_WATERMARK]
    seed_setting(fake_station, _WATERMARK, 1)
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = entity_id_for(hass, SELECT_DOMAIN, SYNTHETIC.camera_sn, _WATERMARK)
    assert state_of(hass, entity_id) == setting.label(1)

    fake_station.account_id = "another-account"  # the ECB result code is then -104
    shown = record_states(hass, entity_id)
    with pytest.raises(HomeAssistantError) as raised:
        await select_option(hass, entity_id, str(setting.label(2)))
    await hass.async_block_till_done()

    assert raised.value.translation_domain == DOMAIN
    assert raised.value.translation_key == _NOT_APPLIED
    placeholders = raised.value.translation_placeholders
    assert placeholders == {"target": setting.name}
    assert shown == []
    assert state_of(hass, entity_id) == setting.label(1)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_an_unknown_code_shows_no_option(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A wire code the setting has no value for shows unknown, never a guess."""
    seed_setting(fake_station, _POWER_MODE, 9)
    entry = await set_up_warm(hass, seed_warm_cache)

    entity_id = entity_id_for(hass, SELECT_DOMAIN, SYNTHETIC.camera_sn, _POWER_MODE)
    assert state_of(hass, entity_id) == STATE_UNKNOWN

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


# ── settings that apply only while another setting holds a value ─────────────

_CUSTOM_MODE_ONLY: Final[dict[str, str]] = {
    "video_clip_length": "number",
    "trigger_interval_time": "number",
    "motion_stop_end_early": "switch",
}


async def test_a_custom_mode_only_setting_is_unavailable_outside_the_custom_power_mode(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Clip length, trigger interval and end-early follow the camera's power mode.

    Outside custom recording (3) each is unavailable; picking it brings them back with
    an ``applies_when`` attribute, and leaving it takes them away again.
    """
    models = settings_of("T8160")
    for key in _CUSTOM_MODE_ONLY:
        assert models[key].applies_when == (_POWER_MODE, 3), key
    power = models[_POWER_MODE]
    seed_setting(fake_station, _POWER_MODE, 1)
    seed_setting(fake_station, "video_clip_length", 60)
    entry = await set_up_warm(hass, seed_warm_cache)
    power_id = entity_id_for(hass, SELECT_DOMAIN, SYNTHETIC.camera_sn, _POWER_MODE)
    gated = {
        key: entity_id_for(hass, platform, SYNTHETIC.camera_sn, key)
        for key, platform in _CUSTOM_MODE_ONLY.items()
    }
    night_id = entity_id_for(hass, SELECT_DOMAIN, SYNTHETIC.camera_sn, _NIGHT_VISION)

    for key, entity_id in gated.items():
        assert state_of(hass, entity_id) == STATE_UNAVAILABLE, key
    assert state_of(hass, night_id) != STATE_UNAVAILABLE
    # The controller names its dependents while they are unavailable (attributes dropped).
    power_state = hass.states.get(power_id)
    assert power_state is not None
    assert power_state.attributes["controls"] == dict.fromkeys(_CUSTOM_MODE_ONLY, power.label(3))

    await select_option(hass, power_id, str(power.label(3)))
    await hass.async_block_till_done()

    for key, entity_id in gated.items():
        state = hass.states.get(entity_id)
        assert state is not None and state.state != STATE_UNAVAILABLE, key
        assert state.attributes["applies_when"] == "power_manager_mode=3", key
        assert state.attributes["applies_when_label"] == power.label(3), key
    assert state_of(hass, gated["video_clip_length"]) == "60"
    night = hass.states.get(night_id)
    assert night is not None
    assert "applies_when" not in night.attributes

    await select_option(hass, power_id, str(power.label(0)))
    await hass.async_block_till_done()
    for key, entity_id in gated.items():
        assert state_of(hass, entity_id) == STATE_UNAVAILABLE, key

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_custom_mode_only_setting_stays_available_while_the_mode_is_unreported(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """An unreported power mode never takes a setting away: only a known other mode does."""
    seed_setting(fake_station, "video_clip_length", 60)
    entry = await set_up_warm(hass, seed_warm_cache)
    clip_id = entity_id_for(hass, "number", SYNTHETIC.camera_sn, "video_clip_length")
    assert state_of(hass, clip_id) == "60"
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
