"""Bool settings, flags members and the named bits of per-mode action masks as switches.

A bool switch writes True or False; the library renders the device's own code, so an
inverted setting (wire 0 = on) needs nothing here. An action-bit switch writes ONE bit
through the library, which reads the mask, keeps every other bit and confirms the
mode's table by read-back.
"""

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
from eufy_home_security import DeviceTimeoutError, EufySecurity, entity_unique_id
from eufy_home_security.devices import MODE_ACTION_FLAGS, Scope
from eufy_home_security.devices.model_settings import settings_of
from eufy_home_security.testing import SYNTHETIC, FakeStation
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    STATE_UNKNOWN,
    EntityCategory,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.eufy_home_security.const import DOMAIN

# Inverted: wire 0 is on (ECB).
_END_EARLY: Final = "motion_stop_end_early"
# The plain way round (ECB).
_STATUS_LED: Final = "led_on_off"
# Inverted, written as a 1350 sub-command.
_AUDIO_RECORDING: Final = "audio_recording_on_off"

_CAMERA_CHANNEL: Final = 0
_NOT_APPLIED: Final = "setting_not_applied"
_UNCONFIRMED: Final = "setting_unconfirmed"

_AWAY_ACTION: Final = "camera_action_away"
_ACTION_FLAGS: Final[dict[str, int]] = dict(MODE_ACTION_FLAGS[Scope.CAMERA])


async def set_switch(hass: HomeAssistant, entity_id: str, on: bool) -> None:
    """Turn a switch on or off and wait for it, as a dashboard control does."""
    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_ON if on else SERVICE_TURN_OFF,
        {ATTR_ENTITY_ID: entity_id},
        blocking=True,
    )


def _param(station: FakeStation, key: str) -> Any:
    """The raw value the fake station holds for a camera setting, or None."""
    return station.params.get(_CAMERA_CHANNEL, {}).get(setting_param(key))


def _switch_id(hass: HomeAssistant, key: str) -> str:
    """The synthetic camera's switch for ``key``."""
    return entity_id_for(hass, SWITCH_DOMAIN, SYNTHETIC.camera_sn, key)


async def _enable_action_switches(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Enable the Away action-bit switches (registered disabled) and reload the entry."""
    registry = er.async_get(hass)
    for flag in _ACTION_FLAGS:
        entity_id = _switch_id(hass, f"{_AWAY_ACTION}_{flag}")
        registry.async_update_entity(entity_id, disabled_by=None)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()


async def test_switches_follow_the_station_including_inverted_ones(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A switch shows the decoded bool, whichever way round the wire codes run."""
    seed_setting(fake_station, _END_EARLY, 0)
    seed_setting(fake_station, _STATUS_LED, 0)
    fake_station.reply_to_settings = True
    entry = await set_up_warm(hass, seed_warm_cache)

    end_early_id = _switch_id(hass, _END_EARLY)
    led_id = _switch_id(hass, _STATUS_LED)
    assert state_of(hass, end_early_id) == STATE_ON
    assert state_of(hass, led_id) == STATE_OFF

    await set_switch(hass, end_early_id, False)
    await hass.async_block_till_done()
    assert _param(fake_station, _END_EARLY) == "1"
    assert state_of(hass, end_early_id) == STATE_OFF

    await set_switch(hass, led_id, True)
    await hass.async_block_till_done()
    assert _param(fake_station, _STATUS_LED) == "1"
    assert state_of(hass, led_id) == STATE_ON

    registered = er.async_get(hass).async_get(led_id)
    assert registered is not None
    assert registered.entity_category is EntityCategory.CONFIG
    assert registered.original_name == settings_of("T8160")[_STATUS_LED].name

    from custom_components.eufy_home_security import switch

    assert switch.PARALLEL_UPDATES == 1

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_an_inverted_1350_switch_writes_the_wire_code_the_library_renders(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Record audio: wire 0 = on, sent as a 1350 sub-command carrying the channel."""
    seed_setting(fake_station, _AUDIO_RECORDING, 1)
    fake_station.reply_to_settings = True
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = _switch_id(hass, _AUDIO_RECORDING)
    assert state_of(hass, entity_id) == STATE_OFF

    await set_switch(hass, entity_id, True)
    await hass.async_block_till_done()
    assert _param(fake_station, _AUDIO_RECORDING) == "0"
    assert state_of(hass, entity_id) == STATE_ON

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_switch_write_the_station_rejects_raises_setting_not_applied(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A rejected write raises the translated error and leaves the switch where it was."""
    seed_setting(fake_station, _STATUS_LED, 0)
    entry = await set_up_warm(hass, seed_warm_cache)
    led_id = _switch_id(hass, _STATUS_LED)
    assert state_of(hass, led_id) == STATE_OFF

    fake_station.account_id = "another-account"  # the ECB result code is then -104
    shown = record_states(hass, led_id)
    with pytest.raises(HomeAssistantError) as raised:
        await set_switch(hass, led_id, True)
    await hass.async_block_till_done()

    assert raised.value.translation_domain == DOMAIN
    assert raised.value.translation_key == _NOT_APPLIED
    assert raised.value.translation_placeholders == {
        "target": settings_of("T8160")[_STATUS_LED].name
    }
    assert shown == []
    assert state_of(hass, led_id) == STATE_OFF

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


# ── flags members and shared bits ────────────────────────────────────────────

_DETECTION_TYPES: Final = "detection_type_set"
# human (bits 0-1), vehicle (2), pet (3), plus 0x30000 no member of this key names.
_DETECTION_MASK: Final = 196623
_IGNORE_SWITCH: Final = "notification_ignore_switch"
_STATION_CHANNEL: Final = 255


async def test_each_flags_member_is_a_switch_named_by_the_app(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A FLAGS setting is one enabled config switch per member; on = all its bits set."""
    seed_setting(fake_station, _DETECTION_TYPES, _DETECTION_MASK)
    entry = await set_up_warm(hass, seed_warm_cache)
    setting = settings_of("T8160")[_DETECTION_TYPES]
    registry = er.async_get(hass)

    expected = {"1": STATE_ON, "2": STATE_ON, "3": STATE_ON, "4": STATE_OFF}
    assert set(setting.flags) == set(expected)
    for member, shown in expected.items():
        entity_id = _switch_id(hass, f"{_DETECTION_TYPES}_{member}")
        assert state_of(hass, entity_id) == shown, member
        registered = registry.async_get(entity_id)
        assert registered is not None
        assert registered.disabled_by is None
        assert registered.entity_category is EntityCategory.CONFIG
        assert registered.original_name == f"{setting.name}: {setting.flag_label(member)}"
    # The mask itself is no entity.
    for platform in ("sensor", "select", "number"):
        unique_id = entity_unique_id(SYNTHETIC.camera_sn, _DETECTION_TYPES)
        assert registry.async_get_entity_id(platform, DOMAIN, unique_id) is None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_flags_member_switch_moves_its_bits_and_keeps_the_rest(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Pet off writes the mask without bit 3; the unnamed bits and the other members stay."""
    seed_setting(fake_station, _DETECTION_TYPES, _DETECTION_MASK)
    fake_station.reply_to_settings = True
    entry = await set_up_warm(hass, seed_warm_cache)
    pet_id = _switch_id(hass, f"{_DETECTION_TYPES}_3")
    human_id = _switch_id(hass, f"{_DETECTION_TYPES}_1")

    await set_switch(hass, pet_id, False)
    await hass.async_block_till_done()

    assert fake_station.received[-1]["payload"]["ai_detect_type"] == _DETECTION_MASK & ~8
    assert state_of(hass, pet_id) == STATE_OFF
    assert state_of(hass, human_id) == STATE_ON

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_flags_member_write_without_a_reported_mask_sends_nothing(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    short_readback: None,
) -> None:
    """No mask to start from: the library refuses, the error is translated, nothing is sent."""
    entry = await set_up_warm(hass, seed_warm_cache)
    pet_id = _switch_id(hass, f"{_DETECTION_TYPES}_3")
    assert state_of(hass, pet_id) == STATE_UNKNOWN
    param = setting_param(_DETECTION_TYPES)
    sent = len(fake_station.received)

    with pytest.raises(HomeAssistantError) as raised:
        await set_switch(hass, pet_id, True)

    assert raised.value.translation_key == _NOT_APPLIED
    assert all(r.get("cmd") != param for r in fake_station.received[sent:])

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_shared_bit_switch_keeps_the_other_bits_of_its_parameter(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """notification_ignore_switch is bit 256 of a mask whose other bits are the
    mode-switch notifications: on writes 208 | 256, off writes 208 again."""
    seed_setting(fake_station, _IGNORE_SWITCH, 208, channel=_STATION_CHANNEL)
    fake_station.reply_to_settings = True
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = entity_id_for(hass, SWITCH_DOMAIN, SYNTHETIC.station_sn, _IGNORE_SWITCH)
    assert settings_of("T8030")[_IGNORE_SWITCH].bit == 256
    assert state_of(hass, entity_id) == STATE_OFF

    await set_switch(hass, entity_id, True)
    await hass.async_block_till_done()
    assert fake_station.received[-1]["payload"] == {"arm_push_mode": 464}
    assert state_of(hass, entity_id) == STATE_ON

    # The fake does not store this command's value; the next fresh read must see it.
    seed_setting(fake_station, _IGNORE_SWITCH, 464, channel=_STATION_CHANNEL)
    await set_switch(hass, entity_id, False)
    await hass.async_block_till_done()
    assert fake_station.received[-1]["payload"] == {"arm_push_mode": 208}
    assert state_of(hass, entity_id) == STATE_OFF

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


# ── per-mode actions: one switch per (mode, flag) ────────────────────────────


async def test_mode_action_bits_are_config_switches_registered_disabled(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Each named bit of each action mask is a switch, translated, registered off."""
    entry = await set_up_warm(hass, seed_warm_cache)
    registry = er.async_get(hass)
    assert _ACTION_FLAGS
    for mode in ("home", "away", "custom_1", "custom_2", "custom_3"):
        for flag in _ACTION_FLAGS:
            key = f"camera_action_{mode}_{flag}"
            unique_id = entity_unique_id(SYNTHETIC.camera_sn, key)
            entity_id = registry.async_get_entity_id(SWITCH_DOMAIN, DOMAIN, unique_id)
            assert entity_id is not None, key
            registered = registry.async_get(entity_id)
            assert registered is not None
            assert registered.translation_key == key
            assert registered.disabled_by is er.RegistryEntryDisabler.INTEGRATION, key
            assert registered.entity_category is EntityCategory.CONFIG, key
    # The mask itself is never an entity.
    for platform in ("number", "sensor"):
        unique_id = entity_unique_id(SYNTHETIC.camera_sn, _AWAY_ACTION)
        assert registry.async_get_entity_id(platform, DOMAIN, unique_id) is None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_mode_action_switch_writes_one_bit_through_the_mode_table(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """ "Away: camera siren" sets one bit of the Away mask; every other bit is kept."""
    record = _ACTION_FLAGS["record"]
    notification = _ACTION_FLAGS["notification"]
    siren = _ACTION_FLAGS["camera_siren"]
    unnamed = 0x100  # a bit no flag names
    seed_setting(fake_station, _AWAY_ACTION, record | notification | unnamed)
    entry = await set_up_warm(hass, seed_warm_cache)
    await _enable_action_switches(hass, entry)

    siren_id = _switch_id(hass, f"{_AWAY_ACTION}_camera_siren")
    record_id = _switch_id(hass, f"{_AWAY_ACTION}_record")
    assert state_of(hass, siren_id) == STATE_OFF
    assert state_of(hass, record_id) == STATE_ON

    await set_switch(hass, siren_id, True)
    await hass.async_block_till_done()

    assert len(fake_station.mode_tables_received) == 1, "no mode table reached the station"
    assert _param(fake_station, _AWAY_ACTION) == str(record | notification | siren | unnamed)
    assert state_of(hass, siren_id) == STATE_ON
    assert state_of(hass, record_id) == STATE_ON

    await set_switch(hass, record_id, False)
    await hass.async_block_till_done()

    assert _param(fake_station, _AWAY_ACTION) == str(notification | siren | unnamed)
    assert state_of(hass, record_id) == STATE_OFF
    assert state_of(hass, siren_id) == STATE_ON

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_mode_action_write_that_times_out_shows_unknown_until_the_next_state(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timed-out action-bit write may still apply: unknown, then the next state's value."""
    record = _ACTION_FLAGS["record"]
    seed_setting(fake_station, _AWAY_ACTION, record)
    entry = await set_up_warm(hass, seed_warm_cache)
    await _enable_action_switches(hass, entry)
    siren_id = _switch_id(hass, f"{_AWAY_ACTION}_camera_siren")
    assert state_of(hass, siren_id) == STATE_OFF
    coordinator = entry.runtime_data.coordinators[SYNTHETIC.station_sn]

    async def timed_out(*args: object, **kwargs: object) -> int:
        raise DeviceTimeoutError("no answer")

    monkeypatch.setattr(coordinator.station, "async_set_mode_action", timed_out)
    with pytest.raises(HomeAssistantError) as raised:
        await set_switch(hass, siren_id, True)
    assert raised.value.translation_key == _UNCONFIRMED
    assert state_of(hass, siren_id) == STATE_UNKNOWN

    seed_setting(fake_station, _AWAY_ACTION, record | _ACTION_FLAGS["camera_siren"])
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert state_of(hass, siren_id) == STATE_ON

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_mode_action_switch_with_no_mask_reported_shows_unknown_and_sends_nothing(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    short_readback: None,
) -> None:
    """With no mask reported the bits say nothing, and a write is refused unsent."""
    entry = await set_up_warm(hass, seed_warm_cache)
    await _enable_action_switches(hass, entry)
    siren_id = _switch_id(hass, f"{_AWAY_ACTION}_camera_siren")
    assert state_of(hass, siren_id) == STATE_UNKNOWN

    with pytest.raises(HomeAssistantError) as raised:
        await set_switch(hass, siren_id, True)
    await hass.async_block_till_done()

    assert raised.value.translation_key == _NOT_APPLIED
    assert fake_station.mode_tables_received == []
    assert state_of(hass, siren_id) == STATE_UNKNOWN

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
