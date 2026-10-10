"""Range settings as numbers: the decoded value on the entity, and writes through the library."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from typing import Final

import pytest
from conftest import (
    _STATION_BLOCK,
    entity_id_for,
    record_states,
    seed_setting,
    set_up_warm,
    setting_param,
    state_of,
)
from eufy_home_security import (
    CameraWakeError,
    CommandNotAppliedError,
    CommandRejectedError,
    CommandUnsupportedError,
    DeviceTimeoutError,
    EufySecurity,
    EufySecurityError,
    HandshakeError,
    KeyRejectedError,
    ProtocolError,
    RateLimitedError,
    RefreshCooldownError,
    SessionReplacedError,
    StationUnreachableError,
    entity_unique_id,
)
from eufy_home_security.devices.model_settings import settings_of
from eufy_home_security.testing import SYNTHETIC, FakeStation
from homeassistant.components.number.const import (
    ATTR_MAX,
    ATTR_MIN,
    ATTR_STEP,
    ATTR_VALUE,
    SERVICE_SET_VALUE,
    NumberEntityCapabilityAttribute,
)
from homeassistant.components.number.const import DOMAIN as NUMBER_DOMAIN
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    ATTR_ENTITY_ID,
    ATTR_UNIT_OF_MEASUREMENT,
    EntityCategory,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.eufy_home_security import errors, number
from custom_components.eufy_home_security.const import DOMAIN

# A range setting of the synthetic camera (T8160): 5-60 s, written as an ECB scalar.
_RETRIGGER = "trigger_interval_time"
# The synthetic camera's slot in the fake station's dump.
_CAMERA_CHANNEL = 0

# Written out, not imported: the keys are the contract with the translations.
_NOT_APPLIED = "setting_not_applied"
_UNREACHABLE = "station_unreachable"
_ON_DEMAND_UNREACHABLE = "on_demand_unreachable"
_KEY_REJECTED = "station_key_rejected"
_CLOUD_UNAVAILABLE = "cloud_unavailable"
_DEVICE_UNAVAILABLE = "setting_device_unavailable"
_UNCONFIRMED = "setting_unconfirmed"
_VALUE_INVALID = "setting_value_invalid"


async def set_number(hass: HomeAssistant, entity_id: str, value: float) -> None:
    """Set a number entity and wait for it, as a dashboard control does."""
    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: entity_id, ATTR_VALUE: value},
        blocking=True,
    )


async def test_a_camera_number_shows_the_station_value_and_writes_it(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The dump's value is shown; a write lands on the station and shows once, at its end."""
    seed_setting(fake_station, _RETRIGGER, 30)
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = entity_id_for(hass, NUMBER_DOMAIN, SYNTHETIC.camera_sn, _RETRIGGER)
    assert state_of(hass, entity_id) == "30"

    states = record_states(hass, entity_id)
    await set_number(hass, entity_id, 45)
    await hass.async_block_till_done()

    assert fake_station.params[_CAMERA_CHANNEL][setting_param(_RETRIGGER)] == "45"
    assert state_of(hass, entity_id) == "45"
    assert states == ["45"]

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


# ── writes the station did not apply or could not receive ────────────────────


async def test_a_number_write_the_station_rejects_raises_setting_not_applied(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A rejected write raises the translated error naming the entity, and keeps the value."""
    seed_setting(fake_station, _RETRIGGER, 30)
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = entity_id_for(hass, NUMBER_DOMAIN, SYNTHETIC.camera_sn, _RETRIGGER)
    assert state_of(hass, entity_id) == "30"

    fake_station.account_id = "another-account"  # the ECB result code is then -104
    with pytest.raises(HomeAssistantError) as raised:
        await set_number(hass, entity_id, 45)
    await hass.async_block_till_done()

    assert raised.value.translation_domain == DOMAIN
    assert raised.value.translation_key == _NOT_APPLIED
    placeholders = raised.value.translation_placeholders
    assert placeholders is not None
    assert set(placeholders) == {"target"}
    assert placeholders["target"] == settings_of("T8160")[_RETRIGGER].name
    assert SYNTHETIC.camera_sn not in placeholders["target"]
    assert state_of(hass, entity_id) == "30"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_number_write_that_times_out_shows_unknown_until_the_next_state(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timed-out write may still apply: unknown, a translated error, then the next dump."""
    seed_setting(fake_station, _RETRIGGER, 30)
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = entity_id_for(hass, NUMBER_DOMAIN, SYNTHETIC.camera_sn, _RETRIGGER)
    station = entry.runtime_data.coordinators[SYNTHETIC.station_sn].station

    async def timed_out(*args: object, **kwargs: object) -> None:
        raise DeviceTimeoutError("no answer")

    monkeypatch.setattr(station, "async_set_setting", timed_out)
    with pytest.raises(HomeAssistantError) as raised:
        await set_number(hass, entity_id, 45)
    assert raised.value.translation_key == _UNCONFIRMED
    assert state_of(hass, entity_id) == "unknown"

    seed_setting(fake_station, _RETRIGGER, 45)
    await entry.runtime_data.coordinators[SYNTHETIC.station_sn].async_refresh()
    await hass.async_block_till_done()
    assert state_of(hass, entity_id) == "45"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_number_off_its_step_is_refused_as_a_validation_error(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Home Assistant checks only the range; the library's step refusal is translated, unsent."""
    seed_setting(fake_station, _RETRIGGER, 30)
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = entity_id_for(hass, NUMBER_DOMAIN, SYNTHETIC.camera_sn, _RETRIGGER)
    sent = len(fake_station.received)

    with pytest.raises(ServiceValidationError) as raised:
        await set_number(hass, entity_id, 45.5)
    await hass.async_block_till_done()

    assert raised.value.translation_domain == DOMAIN
    assert raised.value.translation_key == _VALUE_INVALID
    assert raised.value.translation_placeholders == {
        "target": settings_of("T8160")[_RETRIGGER].name
    }
    assert isinstance(raised.value.__cause__, ValueError)
    assert len(fake_station.received) == sent
    assert state_of(hass, entity_id) == "30"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (CommandNotAppliedError(1250), _NOT_APPLIED),
        (CommandRejectedError(1250, 6, "station reports another value"), _NOT_APPLIED),
        # A -108 receipt: a rejection and an UnsupportedError at once.
        (CommandUnsupportedError(1250, -108), _NOT_APPLIED),
        # The station answered; the camera behind it did not wake.
        (CameraWakeError(1250, -204), _ON_DEMAND_UNREACHABLE),
        (StationUnreachableError("no reply to discovery"), _UNREACHABLE),
        (DeviceTimeoutError("command 1250 got no answer"), _UNREACHABLE),
        # What the reconnect before a write can raise.
        (ProtocolError("frame could not be decrypted"), _UNREACHABLE),
        (HandshakeError("session key not established"), _UNREACHABLE),
        (KeyRejectedError("key rejected"), _KEY_REJECTED),
        (RateLimitedError("throttled", retry_after=60.0), _CLOUD_UNAVAILABLE),
        (RefreshCooldownError("cooldown", retry_after=30.0), _CLOUD_UNAVAILABLE),
        # A kick-out in the chain names the session and Repairs, on
        # any station, instead of a cloud outage.
        (SessionReplacedError(), "session_replaced_see_repairs"),
    ],
    ids=lambda value: type(value).__name__ if isinstance(value, Exception) else value,
)
def test_setting_write_failures_map_to_translated_errors(
    error: EufySecurityError, key: str
) -> None:
    """Each library error a setting write can meet becomes a translated error.

    The same twelve errors the arm path maps, so the two write paths cannot drift.
    """
    translated = errors.setting_write_failed(error, "Clip length")

    assert isinstance(translated, HomeAssistantError)
    assert translated.translation_domain == DOMAIN
    assert translated.translation_key == key
    assert translated.translation_placeholders == {"target": "Clip length"}


async def test_a_write_to_a_device_with_no_channel_names_it_instead_of_raising(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A paired device with no channel refuses the write as a translated error, unsent.

    Such a device keeps its entities (the catalog resolves them from the serial), so
    the control can be pressed; ``channel_for``'s ``UnsupportedError`` must not reach
    the user as an unhandled exception.
    """
    seed_setting(fake_station, _RETRIGGER, 30)
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = entity_id_for(hass, NUMBER_DOMAIN, SYNTHETIC.camera_sn, _RETRIGGER)
    assert state_of(hass, entity_id) == "30"

    station = entry.runtime_data.coordinators[SYNTHETIC.station_sn].station
    station.sub_devices = tuple(
        replace(device, channel=None) if device.device_sn == SYNTHETIC.camera_sn else device
        for device in station.sub_devices
    )

    with pytest.raises(HomeAssistantError) as raised:
        await set_number(hass, entity_id, 45)
    await hass.async_block_till_done()

    assert raised.value.translation_domain == DOMAIN
    assert raised.value.translation_key == _DEVICE_UNAVAILABLE
    placeholders = raised.value.translation_placeholders
    assert placeholders is not None
    # The entity's translated name, and nothing else: no serial.
    assert set(placeholders) == {"target"}
    assert placeholders["target"] == settings_of("T8160")[_RETRIGGER].name

    # Refused before anything was sent, so the station still holds what it held.
    assert fake_station.params[_CAMERA_CHANNEL][setting_param(_RETRIGGER)] == "30"
    assert state_of(hass, entity_id) == "30"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


# ── which numbers exist ──────────────────────────────────────────────────────

# The T8160's writable range settings, and the station's (T8030).
_CAMERA_NUMBERS: Final = frozenset(
    {
        "trigger_interval_time",
        "video_clip_length",
        # hb_connect_nas_storage_type: offered only without a parent (PARENTLESS_ONLY)
        "detection_sensitivity",
    }
)
_STATION_NUMBERS: Final = frozenset({"alarm_volume_value", "prompt_volume_value"})
_DELAY_NUMBERS: Final = frozenset(
    f"{kind}_{mode}"
    for kind in ("alarm_delay", "leaving_delay")
    for mode in ("home", "away", "custom_1", "custom_2", "custom_3")
)


def _number_unique_ids(hass: HomeAssistant, entry: MockConfigEntry) -> set[str]:
    """The unique id of every number entity this entry registered."""
    return {
        entity.unique_id
        for entity in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if entity.entity_id.split(".", 1)[0] == NUMBER_DOMAIN
    }


async def test_numbers_exist_for_every_writable_range_setting(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Every writable range setting and every per-mode delay is a number; a mask never is."""
    entry = await set_up_warm(hass, seed_warm_cache)

    expected = {
        *(entity_unique_id(SYNTHETIC.camera_sn, key) for key in _CAMERA_NUMBERS | _DELAY_NUMBERS),
        *(entity_unique_id(SYNTHETIC.station_sn, key) for key in _STATION_NUMBERS),
    }
    assert _number_unique_ids(hass, entry) == expected

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


# ── modes, units, ranges and the write path's edges ──────────────────────────

# A 5-120 range with room either side of its bounds for the boundary probe.
_CLIP_LENGTH: Final = "video_clip_length"
# A unitless 1-26 range on the station: the slider case, left unseeded for unknown.
_ALARM_VOLUME: Final = "alarm_volume_value"


async def test_numbers_take_bounds_step_and_mode_from_the_library(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Bounds, step and slider or box are the library's; seconds make a duration.

    The clip length is a slider in seconds (the app's control); a per-mode delay carries
    no control and is a duration box.

    The state is the library's own integer: "60", not "60.0".
    """
    seed_setting(fake_station, _CLIP_LENGTH, 60)
    seed_setting(fake_station, "alarm_delay_away", 30)
    seed_setting(fake_station, _ALARM_VOLUME, 10, channel=_STATION_BLOCK)
    entry = await set_up_warm(hass, seed_warm_cache)

    clip_id = entity_id_for(hass, NUMBER_DOMAIN, SYNTHETIC.camera_sn, _CLIP_LENGTH)
    clip = hass.states.get(clip_id)
    assert clip is not None
    assert clip.state == "60"
    assert clip.attributes[ATTR_MIN] == 5
    assert clip.attributes[ATTR_MAX] == 120
    assert clip.attributes[ATTR_STEP] == 1
    assert clip.attributes[NumberEntityCapabilityAttribute.MODE] == "slider"
    assert clip.attributes[ATTR_UNIT_OF_MEASUREMENT] == "s"
    assert clip.attributes[ATTR_DEVICE_CLASS] == "duration"

    volume_id = entity_id_for(hass, NUMBER_DOMAIN, SYNTHETIC.station_sn, _ALARM_VOLUME)
    volume = hass.states.get(volume_id)
    assert volume is not None
    assert volume.state == "10"
    assert volume.attributes[NumberEntityCapabilityAttribute.MODE] == "slider"
    assert ATTR_UNIT_OF_MEASUREMENT not in volume.attributes

    delay_id = entity_id_for(hass, NUMBER_DOMAIN, SYNTHETIC.camera_sn, "alarm_delay_away")
    delay = hass.states.get(delay_id)
    assert delay is not None
    assert delay.state == "30"
    assert delay.attributes[ATTR_UNIT_OF_MEASUREMENT] == "s"
    assert delay.attributes[ATTR_DEVICE_CLASS] == "duration"
    assert delay.attributes[NumberEntityCapabilityAttribute.MODE] == "box"
    assert delay.attributes[ATTR_MAX] == 300

    registered = er.async_get(hass).async_get(clip_id)
    assert registered is not None
    assert registered.entity_category is EntityCategory.CONFIG

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_number_out_of_range_is_refused_before_reaching_the_station(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A value outside the library's range never becomes a command.

    Home Assistant checks the bounds before the entity runs, so a refused value costs
    the station nothing: no frame is sent at all. The bounds themselves are inclusive,
    so both ends are writable.
    """
    seed_setting(fake_station, _CLIP_LENGTH, 60)
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = entity_id_for(hass, NUMBER_DOMAIN, SYNTHETIC.camera_sn, _CLIP_LENGTH)
    command_id = setting_param(_CLIP_LENGTH)

    for refused in (4, 121):
        with pytest.raises(ServiceValidationError):
            await set_number(hass, entity_id, refused)
    await hass.async_block_till_done()

    assert not [received for received in fake_station.ecb_received if received[0] == command_id]
    assert state_of(hass, entity_id) == "60"

    for accepted in (5, 120):
        await set_number(hass, entity_id, accepted)
        await hass.async_block_till_done()
        assert fake_station.params[_CAMERA_CHANNEL][command_id] == str(accepted), accepted
        assert state_of(hass, entity_id) == str(accepted), accepted

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_number_write_shows_the_value_once_the_write_returned(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A successful write shows its value once; a rejected one shows nothing new."""
    seed_setting(fake_station, _CLIP_LENGTH, 30)
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = entity_id_for(hass, NUMBER_DOMAIN, SYNTHETIC.camera_sn, _CLIP_LENGTH)

    applied = record_states(hass, entity_id)
    await set_number(hass, entity_id, 45)
    await hass.async_block_till_done()

    assert applied == ["45"]
    assert state_of(hass, entity_id) == "45"

    refused = record_states(hass, entity_id)
    fake_station.account_id = "another-account"
    with pytest.raises(HomeAssistantError):
        await set_number(hass, entity_id, 50)
    await hass.async_block_till_done()

    assert refused == []
    assert state_of(hass, entity_id) == "45"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_two_number_writes_in_a_row_end_at_the_last_value(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Two writes at once leave the entity showing what the station holds.

    ``PARALLEL_UPDATES = 1`` makes Home Assistant run them one after the other, so the
    last write wins and no intermediate value is left behind.
    """
    seed_setting(fake_station, _CLIP_LENGTH, 30)
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = entity_id_for(hass, NUMBER_DOMAIN, SYNTHETIC.camera_sn, _CLIP_LENGTH)

    await asyncio.gather(
        set_number(hass, entity_id, 20),
        set_number(hass, entity_id, 40),
    )
    await hass.async_block_till_done()

    command_id = setting_param(_CLIP_LENGTH)
    shown = state_of(hass, entity_id)
    assert shown == fake_station.params[_CAMERA_CHANNEL][command_id]
    assert shown in ("20", "40")
    assert number.PARALLEL_UPDATES == 1

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_writing_a_number_the_value_it_already_holds_leaves_it_at_that_value(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Writing the value already in force is sent and changes nothing shown.

    A number write has no short circuit (``async_set_setting`` sends whatever the
    station reports), so the write goes out and the entity ends where it started.
    """
    seed_setting(fake_station, _CLIP_LENGTH, 30)
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = entity_id_for(hass, NUMBER_DOMAIN, SYNTHETIC.camera_sn, _CLIP_LENGTH)
    command_id = setting_param(_CLIP_LENGTH)
    assert state_of(hass, entity_id) == "30"

    shown = record_states(hass, entity_id)
    await set_number(hass, entity_id, 30)
    await hass.async_block_till_done()

    # Sent rather than skipped: exactly one frame, carrying the value asked for.
    written = [received for received in fake_station.ecb_received if received[0] == command_id]
    assert [received[-1] for received in written] == [30]
    assert fake_station.params[_CAMERA_CHANNEL][command_id] == "30"
    assert state_of(hass, entity_id) == "30"
    assert shown == []

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_two_number_writes_of_the_same_value_end_at_that_value(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Repeating one write sends it twice, raises nothing and shows the value once."""
    seed_setting(fake_station, _CLIP_LENGTH, 30)
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = entity_id_for(hass, NUMBER_DOMAIN, SYNTHETIC.camera_sn, _CLIP_LENGTH)
    command_id = setting_param(_CLIP_LENGTH)

    shown = record_states(hass, entity_id)
    await set_number(hass, entity_id, 45)
    await set_number(hass, entity_id, 45)
    await hass.async_block_till_done()

    # Both writes went out; neither raised.
    written = [received for received in fake_station.ecb_received if received[0] == command_id]
    assert [received[-1] for received in written] == [45, 45]
    assert fake_station.params[_CAMERA_CHANNEL][command_id] == "45"
    assert state_of(hass, entity_id) == "45"
    assert shown == ["45"]

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_number_the_station_does_not_report_shows_unknown(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A setting the dump does not carry shows unknown, never a default."""
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = entity_id_for(hass, NUMBER_DOMAIN, SYNTHETIC.station_sn, _ALARM_VOLUME)

    assert state_of(hass, entity_id) == "unknown"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


# ── the per-mode delays: one value per mode, written as the mode's table ─────

_ALARM_DELAY_AWAY: Final = "alarm_delay_away"
_CAMERA_ACTION_AWAY: Final = "camera_action_away"
_MODE_TABLE_REFUSED: Final = "setting_mode_table_refused"


async def test_an_alarm_delay_number_writes_through_the_mode_table_with_read_back(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """ "Entry delay (Away)" is a box in whole seconds, written as Away's whole table.

    The library sends the mode's table (``SET_ALL_ACTION``) and confirms it by read-back.
    """
    seed_setting(fake_station, _ALARM_DELAY_AWAY, 0)
    # The table the write replaces carries every device's action for the mode.
    seed_setting(fake_station, _CAMERA_ACTION_AWAY, 9)
    entry = await set_up_warm(hass, seed_warm_cache)

    entity_id = entity_id_for(hass, NUMBER_DOMAIN, SYNTHETIC.camera_sn, _ALARM_DELAY_AWAY)
    registered = er.async_get(hass).async_get(entity_id)
    assert registered is not None
    assert registered.disabled_by is None
    assert registered.entity_category is EntityCategory.CONFIG
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "0"
    assert state.attributes[NumberEntityCapabilityAttribute.MODE] == "box"
    assert state.attributes[ATTR_UNIT_OF_MEASUREMENT] == "s"

    await set_number(hass, entity_id, 30)
    await hass.async_block_till_done()

    assert len(fake_station.mode_tables_received) == 1, "no mode table reached the station"
    command_id = setting_param(_ALARM_DELAY_AWAY)
    assert fake_station.params[_CAMERA_CHANNEL][command_id] == "30"
    assert state_of(hass, entity_id) == "30"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_delay_write_the_mode_table_cannot_carry_is_refused_as_a_sentence(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A table the library will not write back unchanged is a translated refusal.

    The camera reports no Away action, so the library cannot rebuild Away's table
    and refuses before sending anything (``UnsupportedError``). That is a state of
    the HomeBase a user can meet by pressing the control, so it must arrive as a
    translated message naming the entity, with nothing sent and the value unchanged.
    """
    seed_setting(fake_station, _ALARM_DELAY_AWAY, 0)
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = entity_id_for(hass, NUMBER_DOMAIN, SYNTHETIC.camera_sn, _ALARM_DELAY_AWAY)

    with pytest.raises(HomeAssistantError) as caught:
        await set_number(hass, entity_id, 30)
    await hass.async_block_till_done()

    assert caught.value.translation_key == _MODE_TABLE_REFUSED
    assert caught.value.translation_domain == DOMAIN
    assert fake_station.mode_tables_received == []
    assert state_of(hass, entity_id) == "0"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


_MODE_ACTION: Final[dict[str, str]] = {
    "home": "camera_action_home",
    "away": "camera_action_away",
    "custom_1": "camera_action_custom_1",
    "custom_2": "camera_action_custom_2",
    "custom_3": "camera_action_custom_3",
}


@pytest.mark.parametrize("key", sorted(_DELAY_NUMBERS))
async def test_every_per_mode_delay_is_an_enabled_number_written_as_its_table(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    key: str,
) -> None:
    """Each delay is an enabled config number with a translated name; a write is a table."""
    mode = key.split("_delay_", 1)[1]
    seed_setting(fake_station, key, 0)
    seed_setting(fake_station, _MODE_ACTION[mode], 9)
    entry = await set_up_warm(hass, seed_warm_cache)
    registry = er.async_get(hass)
    entity_id = entity_id_for(hass, NUMBER_DOMAIN, SYNTHETIC.camera_sn, key)
    registered = registry.async_get(entity_id)
    assert registered is not None
    assert registered.translation_key == key
    assert registered.disabled_by is None
    assert registered.entity_category is EntityCategory.CONFIG

    await set_number(hass, entity_id, 45)
    await hass.async_block_till_done()
    assert len(fake_station.mode_tables_received) == 1
    assert fake_station.params[_CAMERA_CHANNEL][setting_param(key)] == "45"
    assert state_of(hass, entity_id) == "45"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
