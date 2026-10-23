"""Sirens: a camera's own alarm and the HomeBase siren, where the eufy app offers them.

A camera siren sends the library's device alarm (30 s, as the eufy app); the library
reports no state for it, so the entity assumes on after a confirmed start and off 30 s
later or after a stop. The HomeBase siren shows ``Station.alarming``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, Final, cast

import pytest
from conftest import (
    SENSOR_SN,
    add_motion_sensor,
    entity_id_for,
    record_states,
    set_up_warm,
    state_of,
    wait_until,
)
from eufy_home_security import (
    CameraWakeError,
    CommandNotAppliedError,
    CommandOutcome,
    CommandRejectedError,
    CommandUnsupportedError,
    CommunicationError,
    DeviceAlarm,
    DeviceTimeoutError,
    EufySecurity,
    EufySecurityError,
    KeyRejectedError,
    RateLimitedError,
    SessionReplacedError,
    Station,
    StationFeature,
    UnsupportedError,
    entity_unique_id,
)
from eufy_home_security.devices import Evidence, Support
from eufy_home_security.p2p import FrameType
from eufy_home_security.p2p.messages import STANDALONE_RECEIPT_LEN
from eufy_home_security.testing import SYNTHETIC, FakeCloud, FakeStation
from homeassistant.components.siren import (
    ATTR_AVAILABLE_TONES,
    ATTR_DURATION,
    ATTR_TONE,
    SirenEntityFeature,
)
from homeassistant.components.siren import DOMAIN as SIREN_DOMAIN
from homeassistant.const import (
    ATTR_ASSUMED_STATE,
    ATTR_ENTITY_ID,
    ATTR_SUPPORTED_FEATURES,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.eufy_home_security import errors, siren
from custom_components.eufy_home_security.const import DOMAIN, SIREN_KEY

_SET_TONE_FILE: Final = 1201
_SET_DEVS_TONE_FILE: Final = 1202
# The device-alarm types the T8160 on a HomeBase 3 is sent: sound, sound and light.
_SOUND_TYPE: Final = 10
_SOUND_AND_LIGHT_TYPE: Final = 23

_DEVICE_SIREN_FEATURES: Final = (
    SirenEntityFeature.TURN_ON | SirenEntityFeature.TURN_OFF | SirenEntityFeature.TONES
)
_STATION_SIREN_FEATURES: Final = (
    SirenEntityFeature.TURN_ON | SirenEntityFeature.TURN_OFF | SirenEntityFeature.DURATION
)

# A T8170 standalone battery camera (its own station).
STANDALONE_SN: Final = "T8170P0000000001"


def _camera_siren(hass: HomeAssistant) -> str:
    return entity_id_for(hass, SIREN_DOMAIN, SYNTHETIC.camera_sn, SIREN_KEY)


def _station_siren(hass: HomeAssistant) -> str:
    return entity_id_for(hass, SIREN_DOMAIN, SYNTHETIC.station_sn, SIREN_KEY)


def _siren_ids(hass: HomeAssistant) -> set[str]:
    """Every siren unique id the registry holds for this integration."""
    return {
        entry.unique_id
        for entry in er.async_get(hass).entities.values()
        if entry.domain == SIREN_DOMAIN and entry.platform == DOMAIN
    }


def _alarm_payloads(station: FakeStation, command: int) -> list[dict[str, Any]]:
    """The payloads of every ``command`` the fake station received, in order."""
    return [
        dict(obj["payload"])
        for obj in station.received
        if obj.get("cmd") == command and isinstance(obj.get("payload"), dict)
    ]


async def _turn_on(hass: HomeAssistant, entity_id: str, **data: Any) -> None:
    await hass.services.async_call(
        SIREN_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: entity_id, **data}, blocking=True
    )
    await hass.async_block_till_done()


async def _turn_off(hass: HomeAssistant, entity_id: str) -> None:
    await hass.services.async_call(
        SIREN_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: entity_id}, blocking=True
    )
    await hass.async_block_till_done()


async def _after(hass: HomeAssistant, seconds: float) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


async def _unload(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


def _sound_alarm_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Grade ``StationFeature.SOUND_ALARM`` unknown on every station."""
    real = Station.config_support

    def config_support(self: Station, feature: StationFeature) -> Evidence:
        if feature is StationFeature.SOUND_ALARM:
            return Evidence(Support.UNKNOWN, "graded unknown by the test")
        return real(self, feature)

    monkeypatch.setattr(Station, "config_support", config_support)


# ── which devices get a siren ─────────────────────────────────────────────────


async def test_a_camera_and_the_homebase_get_sirens_and_a_motion_sensor_none(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The camera's siren offers its model's alarms; the HomeBase's offers a duration."""
    add_motion_sensor(fake_station, fake_cloud)
    entry = await set_up_warm(hass, seed_warm_cache)

    assert _siren_ids(hass) == {
        entity_unique_id(SYNTHETIC.camera_sn, SIREN_KEY),
        entity_unique_id(SYNTHETIC.station_sn, SIREN_KEY),
    }
    assert entity_unique_id(SENSOR_SN, SIREN_KEY) not in _siren_ids(hass)

    camera = hass.states.get(_camera_siren(hass))
    assert camera is not None
    assert camera.state == STATE_OFF
    assert camera.attributes[ATTR_SUPPORTED_FEATURES] == _DEVICE_SIREN_FEATURES
    assert camera.attributes[ATTR_AVAILABLE_TONES] == ["sound", "sound_and_light"]
    assert camera.attributes[ATTR_ASSUMED_STATE] is True

    station = hass.states.get(_station_siren(hass))
    assert station is not None
    assert station.state == STATE_OFF
    assert station.attributes[ATTR_SUPPORTED_FEATURES] == _STATION_SIREN_FEATURES
    assert ATTR_AVAILABLE_TONES not in station.attributes
    assert ATTR_ASSUMED_STATE not in station.attributes

    registry = er.async_get(hass)
    for entity_id in (_camera_siren(hass), _station_siren(hass)):
        registered = registry.async_get(entity_id)
        assert registered is not None
        assert registered.entity_category is None
        assert registered.translation_key == SIREN_KEY
        assert registered.has_entity_name

    await _unload(hass, entry)


async def test_no_homebase_siren_where_the_library_does_not_sound_the_station(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """``StationFeature.SOUND_ALARM`` graded unknown: the camera keeps its siren."""
    _sound_alarm_unknown(monkeypatch)
    entry = await set_up_warm(hass, seed_warm_cache)

    assert _siren_ids(hass) == {entity_unique_id(SYNTHETIC.camera_sn, SIREN_KEY)}

    await _unload(hass, entry)


def test_a_station_never_gets_a_device_siren_for_its_own_serial() -> None:
    """The library sends a HomeBase's own device alarm as the station siren; that is the
    station siren entity's job, so a station that is not standalone gets none for it,
    even where its own serial is listed among its devices. A paired device that is no
    camera gets none either, whatever the library offers it."""
    own = SimpleNamespace(device_sn=SYNTHETIC.camera_sn)
    paired = SimpleNamespace(device_sn="T8160P0000000001")
    sensor = SimpleNamespace(device_sn="T870GP0000000001")
    station = SimpleNamespace(
        serial=SYNTHETIC.camera_sn,
        is_standalone=False,
        devices=(own, paired, sensor),
        device_alarms=lambda serial: (DeviceAlarm.SOUND,),
    )

    assert siren.device_siren_alarms(cast(Station, station)) == {
        "T8160P0000000001": (DeviceAlarm.SOUND,)
    }

    station.is_standalone = True
    assert set(siren.device_siren_alarms(cast(Station, station))) == {
        SYNTHETIC.camera_sn,
        "T8160P0000000001",
    }


# ── a camera siren ────────────────────────────────────────────────────────────


async def test_a_camera_siren_sounds_the_default_alarm_and_reads_off_after_30_s(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """No tone: the app's preselected alarm; on once confirmed, off 30 s later, unsent."""
    fake_station.siren_frames = True
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = _camera_siren(hass)
    states = record_states(hass, entity_id)

    await _turn_on(hass, entity_id)

    assert [(p["type"], p["time_out"]) for p in _alarm_payloads(fake_station, 1202)] == [
        (_SOUND_TYPE, 30)
    ]
    assert state_of(hass, entity_id) == STATE_ON
    await _after(hass, 29)
    assert state_of(hass, entity_id) == STATE_ON
    await _after(hass, 31)
    assert state_of(hass, entity_id) == STATE_OFF
    assert states == [STATE_ON, STATE_OFF]
    # The off after 30 s is the alarm's own end: nothing more was sent.
    assert len(_alarm_payloads(fake_station, _SET_DEVS_TONE_FILE)) == 1

    await _unload(hass, entry)


async def test_a_tone_sounds_that_alarm_and_the_stop_names_it(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A tone picks the alarm; turning off stops that one and cancels the 30 s off."""
    fake_station.siren_frames = True
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = _camera_siren(hass)
    states = record_states(hass, entity_id)

    await _turn_on(hass, entity_id, **{ATTR_TONE: "sound_and_light"})
    assert state_of(hass, entity_id) == STATE_ON
    await _turn_off(hass, entity_id)
    assert state_of(hass, entity_id) == STATE_OFF

    assert [
        (p["type"], p["time_out"]) for p in _alarm_payloads(fake_station, _SET_DEVS_TONE_FILE)
    ] == [(_SOUND_AND_LIGHT_TYPE, 30), (_SOUND_AND_LIGHT_TYPE, 0)]

    # A second start: the first start's timer no longer runs, the second one does.
    await _turn_on(hass, entity_id)
    await _after(hass, 31)
    assert states == [STATE_ON, STATE_OFF, STATE_ON, STATE_OFF]

    await _unload(hass, entry)


async def test_a_new_start_restarts_the_30_s(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A start while on counts its length from the latest start, not the first."""
    monkeypatch.setattr(siren, "DEVICE_SIREN_SECONDS", 0.5)
    fake_station.siren_frames = True
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = _camera_siren(hass)
    states = record_states(hass, entity_id)

    await _turn_on(hass, entity_id)
    await asyncio.sleep(0.3)
    await _turn_on(hass, entity_id)
    await asyncio.sleep(0.3)
    # Past the first start's length: still on.
    assert state_of(hass, entity_id) == STATE_ON
    await wait_until(lambda: state_of(hass, entity_id) == STATE_OFF)
    assert states == [STATE_ON, STATE_OFF]

    await _unload(hass, entry)


async def test_a_stop_the_device_does_not_confirm_reads_as_off(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A siren that already went silent leaves a stop unconfirmed: off, no error."""
    fake_station.siren_frames = True
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = _camera_siren(hass)
    await _turn_on(hass, entity_id)
    assert state_of(hass, entity_id) == STATE_ON

    async def not_confirmed(self: Station, *args: Any, **kwargs: Any) -> CommandOutcome:
        raise CommandNotAppliedError(_SET_DEVS_TONE_FILE)

    monkeypatch.setattr(Station, "async_stop_device_alarm", not_confirmed)
    await _turn_off(hass, entity_id)

    assert state_of(hass, entity_id) == STATE_OFF

    await _unload(hass, entry)


async def test_a_stop_the_device_refuses_raises_and_keeps_on(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Any other stop failure is the device's answer: raised, the siren still on."""
    fake_station.siren_frames = True
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = _camera_siren(hass)
    await _turn_on(hass, entity_id)

    fake_station.siren_frames = False
    fake_station.reply_to_settings = True
    fake_station.reply_codes[_SET_DEVS_TONE_FILE] = 6
    with pytest.raises(HomeAssistantError) as raised:
        await _turn_off(hass, entity_id)

    assert raised.value.translation_key == "siren_rejected"
    assert state_of(hass, entity_id) == STATE_ON

    await _unload(hass, entry)


async def test_a_start_the_device_refuses_raises_and_stays_off(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A refused start is raised as a translated error; nothing is assumed on."""
    fake_station.reply_to_settings = True
    fake_station.reply_codes[_SET_DEVS_TONE_FILE] = 6
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = _camera_siren(hass)
    states = record_states(hass, entity_id)

    with pytest.raises(HomeAssistantError) as raised:
        await _turn_on(hass, entity_id)

    assert _alarm_payloads(fake_station, _SET_DEVS_TONE_FILE), "the start never reached the station"
    assert raised.value.translation_domain == DOMAIN
    assert raised.value.translation_key == "siren_rejected"
    assert raised.value.translation_placeholders == {"target": "Siren"}
    assert state_of(hass, entity_id) == STATE_OFF
    assert STATE_ON not in states

    await _unload(hass, entry)


async def test_an_unconfirmed_start_raises_and_stays_off(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A start acknowledged but never confirmed is not shown as on."""
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = _camera_siren(hass)
    sent: list[DeviceAlarm | int | None] = []

    async def not_confirmed(
        self: Station, device_sn: str, alarm: DeviceAlarm | int | None = None, **kwargs: Any
    ) -> CommandOutcome:
        sent.append(alarm)
        raise CommandNotAppliedError(_SET_DEVS_TONE_FILE)

    monkeypatch.setattr(Station, "async_trigger_device_alarm", not_confirmed)
    with pytest.raises(HomeAssistantError) as raised:
        await _turn_on(hass, entity_id, **{ATTR_TONE: "sound"})

    assert sent == [DeviceAlarm.SOUND]
    assert raised.value.translation_key == "siren_not_confirmed"
    assert state_of(hass, entity_id) == STATE_OFF

    await _unload(hass, entry)


async def test_unloading_with_a_camera_siren_on_leaves_no_timer(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The 30 s off is cancelled with the entity (the harness fails a lingering timer)."""
    fake_station.siren_frames = True
    entry = await set_up_warm(hass, seed_warm_cache)
    await _turn_on(hass, _camera_siren(hass))

    await _unload(hass, entry)
    await _after(hass, 31)


# ── the HomeBase siren ────────────────────────────────────────────────────────


async def test_the_homebase_siren_follows_the_station_alarm(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """On and off follow the station's alarm transitions, whoever started them."""
    fake_station.tone_frames = True
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = _station_siren(hass)

    await _turn_on(hass, entity_id)
    await wait_until(lambda: state_of(hass, entity_id) == STATE_ON)
    await _turn_off(hass, entity_id)
    await wait_until(lambda: state_of(hass, entity_id) == STATE_OFF)
    assert [p["time_out"] for p in _alarm_payloads(fake_station, _SET_TONE_FILE)] == [30, 0]

    # An alarm the station raised itself, and the app's stop.
    fake_station.send_alarm_frame(FrameType.ALARM_TONE_NOTIFY, 3, 30, channel=1)
    await wait_until(lambda: state_of(hass, entity_id) == STATE_ON)
    fake_station.send_alarm_frame(FrameType.ALARM_TONE_NOTIFY, 16, 0, channel=255)
    await wait_until(lambda: state_of(hass, entity_id) == STATE_OFF)

    await _unload(hass, entry)


async def test_the_homebase_siren_duration_rounds_up_and_refuses_above_the_most(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A duration is sent as the next allowed length; one above the longest sends nothing."""
    fake_station.tone_frames = True
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = _station_siren(hass)

    with pytest.raises(ServiceValidationError) as raised:
        await _turn_on(hass, entity_id, **{ATTR_DURATION: 31})
    assert raised.value.translation_key == "siren_duration_invalid"
    assert raised.value.translation_placeholders == {"max": "30"}
    assert _alarm_payloads(fake_station, _SET_TONE_FILE) == []

    await _turn_on(hass, entity_id, **{ATTR_DURATION: 5})
    await _turn_on(hass, entity_id, **{ATTR_DURATION: 30})
    assert [p["time_out"] for p in _alarm_payloads(fake_station, _SET_TONE_FILE)] == [30, 30]

    await _unload(hass, entry)


@pytest.mark.parametrize(
    ("duration", "seconds"),
    [(None, 30), (0, 10), (5, 10), (10, 10), (11, 30), (30, 30), (31, 60), (60, 60)],
)
def test_a_duration_maps_to_the_shortest_allowed_length_at_or_above_it(
    duration: int | None, seconds: int
) -> None:
    assert siren.station_alarm_seconds(duration, (10, 30, 60)) == seconds


def test_a_duration_above_the_longest_allowed_length_is_refused() -> None:
    with pytest.raises(ServiceValidationError) as raised:
        siren.station_alarm_seconds(61, (10, 30, 60))
    assert raised.value.translation_placeholders == {"max": "60"}


async def test_a_homebase_siren_failure_raises_a_translated_error(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = _station_siren(hass)

    async def unreachable(self: Station, *args: Any, **kwargs: Any) -> CommandOutcome:
        raise DeviceTimeoutError("command 1201 got no answer")

    monkeypatch.setattr(Station, "async_trigger_alarm", unreachable)
    monkeypatch.setattr(Station, "async_stop_alarm", unreachable)
    for call in (_turn_on, _turn_off):
        with pytest.raises(HomeAssistantError) as raised:
            await call(hass, entity_id)
        assert raised.value.translation_key == "station_unreachable"
    assert state_of(hass, entity_id) == STATE_OFF

    await _unload(hass, entry)


# ── error mapping ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("error", "on_demand", "key"),
    [
        (CommandNotAppliedError(_SET_DEVS_TONE_FILE), False, "siren_not_confirmed"),
        (CommandRejectedError(_SET_DEVS_TONE_FILE, 6, "refused"), False, "siren_rejected"),
        # A -108 receipt: a rejection and an UnsupportedError at once.
        (CommandUnsupportedError(_SET_DEVS_TONE_FILE, -108), False, "siren_rejected"),
        (CameraWakeError(_SET_DEVS_TONE_FILE, -204), False, "on_demand_unreachable"),
        (UnsupportedError("not offered here"), False, "siren_unsupported"),
        (DeviceTimeoutError("no answer"), False, "station_unreachable"),
        (DeviceTimeoutError("no answer"), True, "on_demand_unreachable"),
        (CommunicationError("socket closed"), False, "station_unreachable"),
        (KeyRejectedError("key rejected"), False, "station_key_rejected"),
        (RateLimitedError("throttled", retry_after=60.0), False, "cloud_unavailable"),
        (SessionReplacedError(), False, "session_replaced_see_repairs"),
    ],
    ids=lambda value: type(value).__name__ if isinstance(value, Exception) else str(value),
)
def test_siren_failures_map_to_translated_errors(
    error: EufySecurityError, on_demand: bool, key: str
) -> None:
    """Each library error a siren command can meet names the siren, never library text."""
    translated = errors.siren_failed(error, "Siren", on_demand=on_demand)

    assert isinstance(translated, HomeAssistantError)
    assert translated.translation_domain == DOMAIN
    assert translated.translation_key == key
    assert translated.translation_placeholders == {"target": "Siren"}


# ── a standalone camera ───────────────────────────────────────────────────────


def _standalone_entry() -> dict[str, Any]:
    return {
        "device_sn": STANDALONE_SN,
        "device_type": 48,
        "device_name": "Solo",
        "parent_sn": STANDALONE_SN,
        "device_channel": 48,
        "p2p_did": SYNTHETIC.did,
        "local_ip": SYNTHETIC.station_ip,
        "params": [
            {"param_type": 1101, "param_value": "61", "update_time": 1.7e9},
            {"param_type": 1224, "param_value": "1", "update_time": 1.7e9},
        ],
    }


class TestStandaloneCamera:
    """A battery camera that is its own station: its siren is a device siren."""

    @pytest.fixture
    async def fake_station(self) -> Any:
        station = FakeStation(
            serial=STANDALONE_SN,
            cipher_id=98,
            receipt_len=STANDALONE_RECEIPT_LEN,
            guard_mode=1,
            params={
                48: {1224: "1", 1101: "61", 1142: "-27", 1131: "1"},
                255: {1216: "Solo"},
            },
        )
        await station.start()
        yield station
        station.stop()

    @pytest.fixture
    def fake_cloud(self, fake_station: FakeStation) -> FakeCloud:
        return FakeCloud(
            devices=[_standalone_entry()],
            owner_ids={STANDALONE_SN: fake_station.account_id},
            cipher_keys={STANDALONE_SN: fake_station.ecc_private_key_hex},
        )

    async def test_its_own_serial_gets_a_device_siren_and_no_station_siren(
        self,
        hass: HomeAssistant,
        monkeypatch: pytest.MonkeyPatch,
        fake_station: FakeStation,
        built_clients: list[EufySecurity],
        seed_warm_cache: Callable[..., None],
    ) -> None:
        """Its alarms as the app offers a camera without a HomeBase; no station siren,
        even where its model would sound one."""
        real = Station.config_support

        def config_support(self: Station, feature: StationFeature) -> Evidence:
            if feature is StationFeature.SOUND_ALARM:
                return Evidence(Support.DECLARED, "graded declared by the test")
            return real(self, feature)

        monkeypatch.setattr(Station, "config_support", config_support)
        fake_station.tone_frames = True
        entry = await set_up_warm(hass, seed_warm_cache)

        assert _siren_ids(hass) == {entity_unique_id(STANDALONE_SN, SIREN_KEY)}
        entity_id = entity_id_for(hass, SIREN_DOMAIN, STANDALONE_SN, SIREN_KEY)
        state = hass.states.get(entity_id)
        assert state is not None
        assert state.attributes[ATTR_SUPPORTED_FEATURES] == _DEVICE_SIREN_FEATURES
        assert state.attributes[ATTR_AVAILABLE_TONES] == ["sound", "light", "sound_and_light"]

        await _turn_on(hass, entity_id, **{ATTR_TONE: "light"})
        assert state_of(hass, entity_id) == STATE_ON
        assert [(p["type"], p["time_out"]) for p in _alarm_payloads(fake_station, 1201)] == [
            (22, 30)
        ]

        await _unload(hass, entry)
