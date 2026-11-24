"""Bool settings and flags members as switches.

A bool switch writes True or False; the library renders the device's own code, so an
inverted setting (wire 0 = on) needs nothing here.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import Any, Final

import pytest
from conftest import (
    add_entry,
    entity_id_for,
    record_states,
    seed_setting,
    set_up_warm,
    setting_param,
    setup_entry,
    state_of,
)
from eufy_home_security import DeviceTimeoutError, EufySecurity, Station, entity_unique_id
from eufy_home_security.devices import Setting
from eufy_home_security.devices.model_settings import settings_of
from eufy_home_security.testing import SYNTHETIC, FakeStation
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import (
    ATTR_ASSUMED_STATE,
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    STATE_UNKNOWN,
    EntityCategory,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.eufy_home_security.const import DOMAIN

# Inverted: wire 0 is on (ECB).
_END_EARLY: Final = "motion_stop_end_early"
# The plain way round (ECB).
_STATUS_LED: Final = "led_on_off"
# Inverted, written as a 1350 sub-command.
_AUDIO_RECORDING: Final = "audio_recording_on_off"

_CAMERA_CHANNEL: Final = 0
_STATION_CHANNEL: Final = 255
_NOT_APPLIED: Final = "setting_not_applied"
_UNCONFIRMED: Final = "setting_unconfirmed"


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


@pytest.fixture
def write_only_led(monkeypatch: pytest.MonkeyPatch) -> None:
    """The status LED as a setting the library cannot read back (``readable`` False)."""
    real = Station.settings_for

    def settings_for(self: Station, device_sn: str | None = None) -> tuple[Setting, ...]:
        return tuple(
            dataclasses.replace(s, readable=False) if s.key == _STATUS_LED else s
            for s in real(self, device_sn)
        )

    monkeypatch.setattr(Station, "settings_for", settings_for)


@pytest.mark.usefixtures("write_only_led")
async def test_a_write_only_switch_is_assumed_and_shows_only_what_it_wrote(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A setting the library cannot read back is assumed state: unknown until written
    here, then the written value through later polls, and unknown again after a reload.
    A value in the station's dump is never shown for it."""
    seed_setting(fake_station, _STATUS_LED, 1)
    fake_station.reply_to_settings = True
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = _switch_id(hass, _STATUS_LED)
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == STATE_UNKNOWN
    assert state.attributes.get(ATTR_ASSUMED_STATE) is True

    await set_switch(hass, entity_id, False)
    await hass.async_block_till_done()
    assert _param(fake_station, _STATUS_LED) == "0"
    assert state_of(hass, entity_id) == STATE_OFF

    # The dump says on; the entity keeps the value it wrote.
    seed_setting(fake_station, _STATUS_LED, 1)
    await entry.runtime_data.coordinators[SYNTHETIC.station_sn].async_refresh()
    await hass.async_block_till_done()
    assert state_of(hass, entity_id) == STATE_OFF

    await set_switch(hass, entity_id, True)
    await hass.async_block_till_done()
    assert state_of(hass, entity_id) == STATE_ON

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == STATE_UNKNOWN
    assert state.attributes.get(ATTR_ASSUMED_STATE) is True

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.usefixtures("write_only_led")
async def test_a_write_only_switch_whose_write_timed_out_shows_unknown(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timed-out write may still apply: the earlier written value is dropped, and the
    switch stays unknown through later polls until it is written again."""
    seed_setting(fake_station, _STATUS_LED, 1)
    fake_station.reply_to_settings = True
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = _switch_id(hass, _STATUS_LED)
    coordinator = entry.runtime_data.coordinators[SYNTHETIC.station_sn]
    await set_switch(hass, entity_id, True)
    await hass.async_block_till_done()
    assert state_of(hass, entity_id) == STATE_ON

    async def timed_out(*args: object, **kwargs: object) -> None:
        raise DeviceTimeoutError("no answer")

    real_write = coordinator.station.async_set_setting
    monkeypatch.setattr(coordinator.station, "async_set_setting", timed_out)
    with pytest.raises(HomeAssistantError) as raised:
        await set_switch(hass, entity_id, False)
    assert raised.value.translation_key == _UNCONFIRMED
    assert state_of(hass, entity_id) == STATE_UNKNOWN

    # A dump that changes the station's state ends the unconfirmed write.
    seed_setting(fake_station, _STATUS_LED, 0)
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert state_of(hass, entity_id) == STATE_UNKNOWN

    monkeypatch.setattr(coordinator.station, "async_set_setting", real_write)
    await set_switch(hass, entity_id, False)
    await hass.async_block_till_done()
    assert state_of(hass, entity_id) == STATE_OFF

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


_ALARM_DELAY: Final = "notification_alarm_delay_value"


async def test_the_homebase_alarm_delay_is_a_switch_and_its_sensor_row_is_forgotten(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The alarm delay is the eufy app's on/off switch: an enabled config switch that
    writes 1 and 0. The sensor row an earlier release built under its unique id goes."""
    seed_setting(fake_station, _ALARM_DELAY, 0, channel=_STATION_CHANNEL)
    fake_station.reply_to_settings = True
    seed_warm_cache()
    entry = add_entry(hass)
    registry = er.async_get(hass)
    unique_id = entity_unique_id(SYNTHETIC.station_sn, _ALARM_DELAY)
    old = registry.async_get_or_create(
        Platform.SENSOR,
        DOMAIN,
        unique_id,
        config_entry=entry,
        entity_category=EntityCategory.DIAGNOSTIC,
        original_name=_ALARM_DELAY,
    )
    assert await setup_entry(hass, entry)

    assert registry.async_get(old.entity_id) is None
    entity_id = entity_id_for(hass, SWITCH_DOMAIN, SYNTHETIC.station_sn, _ALARM_DELAY)
    registered = registry.async_get(entity_id)
    assert registered is not None
    assert (registered.entity_category, registered.disabled_by) == (EntityCategory.CONFIG, None)
    assert state_of(hass, entity_id) == STATE_OFF

    await set_switch(hass, entity_id, True)
    await hass.async_block_till_done()
    sent = fake_station.received[-1]
    assert (sent["cmd"], sent["mChannel"], sent["payload"]) == (
        setting_param(_ALARM_DELAY, model="T8030"),
        _STATION_CHANNEL,
        {"notify_alarm_delay": 1},
    )
    assert state_of(hass, entity_id) == STATE_ON

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


async def test_a_switch_write_under_another_account_raises_the_account_hint(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A write the station refuses for the account (-104) raises the account hint and
    leaves the switch where it was."""
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
    assert raised.value.translation_key == "account_rejected"
    station = entry.runtime_data.coordinators[SYNTHETIC.station_sn].station
    # The camera's name in front: the station answers for it.
    camera = station.sub_device(SYNTHETIC.camera_sn).name
    assert raised.value.translation_placeholders == {
        "target": f"{camera} {settings_of('T8160')[_STATUS_LED].name}",
        "station": station.name,
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
