"""Which device a message that names the station is about.

A HomeBase answers for the cameras paired to it, so its refusal names the HomeBase;
the target then carries the camera's name ("Home Base refused the change to Front
Status LED"), as Home Assistant names the entity. An entity of the station itself (a
HomeBase's own setting, a standalone camera's) is named without the station's name again.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Final

import pytest
from conftest import entity_id_for, seed_setting, set_up_warm, state_of
from eufy_home_security import (
    AccountRejectedError,
    ClipWriter,
    CommandNotAppliedError,
    CommandRejectedError,
    CommandUnsupportedError,
    EufySecurity,
    EufySecurityError,
    KeyRejectedError,
    MediaClip,
    Station,
    StationUnreachableError,
    StreamBroadcast,
)
from eufy_home_security.devices.model_settings import settings_of
from eufy_home_security.p2p.messages import STANDALONE_RECEIPT_LEN
from eufy_home_security.testing import SYNTHETIC, FakeCloud, FakeStation
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant.common import MockConfigEntry

from custom_components.eufy_home_security import errors
from custom_components.eufy_home_security.const import (
    CAMERA_KEY,
    DEFAULT_PRESET_KEY,
    DOMAIN,
    LIVE_PRESET_KEY,
    LIVE_ZOOM_KEY,
    PAN_LEFT_KEY,
    SAVE_VIEW_KEY,
    SERVICE_GOTO_PRESET,
    SERVICE_RECORD,
    SIREN_KEY,
)
from custom_components.eufy_home_security.streaming import StreamManager

_STRINGS: Final = json.loads(
    (Path(__file__).parents[1] / "custom_components" / DOMAIN / "strings.json").read_text(
        encoding="utf-8"
    )
)
_STATION: Final = "Home Base"
_CAMERA: Final = "Front"
_CAMERA_MODEL: Final = "T8160"
# The station's answers that name it, one per setting-write key.
_REFUSALS: Final = (
    (CommandRejectedError(1350, 6, "refused"), "setting_rejected"),
    (CommandUnsupportedError(1350, -108), "setting_command_unsupported"),
    (CommandNotAppliedError(1350), "setting_not_applied"),
    (AccountRejectedError(1350, -104), "account_rejected"),
)
_REFUSAL_IDS: Final = [key for _, key in _REFUSALS]


def _message(err: HomeAssistantError) -> str:
    """The English message ``err`` shows, rendered from strings.json."""
    assert err.translation_key is not None
    template: str = _STRINGS["exceptions"][err.translation_key]["message"]
    return template.format(**(err.translation_placeholders or {}))


def _fail(monkeypatch: pytest.MonkeyPatch, owner: type, method: str, error: Exception) -> None:
    async def failing(*args: Any, **kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(owner, method, failing)


async def _unload(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


def _original_name(hass: HomeAssistant, entity_id: str) -> str:
    registered = er.async_get(hass).async_get(entity_id)
    assert registered is not None and registered.original_name
    return registered.original_name


def _entity(hass: HomeAssistant, entity_id: str) -> Any:
    """The platform's entity object behind ``entity_id``."""
    entity = hass.data[entity_id.split(".", 1)[0]].get_entity(entity_id)
    assert entity is not None
    return entity


def _device(hass: HomeAssistant, entry: MockConfigEntry, serial: str) -> dr.DeviceEntry:
    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, serial), entry.entry_id)
    assert device is not None
    return device


# ── setting entities of a camera behind a HomeBase ───────────────────────────

# (platform, setting key, entity key, seeded value, service, service data, library method);
# None for a select's data: any other option.
type _Write = tuple[str, str, str, int, str, dict[str, Any] | None, str]
_CAMERA_SETTINGS: Final[tuple[_Write, ...]] = (
    ("switch", "led_on_off", "led_on_off", 0, "turn_on", {}, "async_set_setting"),
    (
        "switch",
        "detection_type_set",
        "detection_type_set_3",
        196623,
        "turn_off",
        {},
        "async_set_flag",
    ),
    (
        "number",
        "trigger_interval_time",
        "trigger_interval_time",
        30,
        "set_value",
        {"value": 45},
        "async_set_setting",
    ),
    ("select", "watermark_set", "watermark_set", 1, "select_option", None, "async_set_setting"),
    ("light", "spotlight_switch", "spotlight_switch", 1, "turn_off", {}, "async_set_setting"),
)


@pytest.mark.parametrize(("error", "key"), _REFUSALS, ids=_REFUSAL_IDS)
@pytest.mark.parametrize(
    ("platform", "setting", "entity_key", "seeded", "service", "data", "method"),
    _CAMERA_SETTINGS,
    ids=[f"{row[0]}-{row[2]}" for row in _CAMERA_SETTINGS],
)
async def test_a_refusal_for_a_camera_names_the_camera_and_the_station(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    error: EufySecurityError,
    key: str,
    platform: str,
    setting: str,
    entity_key: str,
    seeded: Any,
    service: str,
    data: dict[str, Any] | None,
    method: str,
) -> None:
    """The station's answer about a paired camera's setting names the station, and the
    setting as Home Assistant names its entity: the camera's name, then the setting's."""
    seed_setting(fake_station, setting, seeded)
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = entity_id_for(hass, platform, SYNTHETIC.camera_sn, entity_key)
    name = _original_name(hass, entity_id)
    if data is None:  # a select: any other option
        options = hass.states.get(entity_id).attributes["options"]  # type: ignore[union-attr]
        data = {"option": next(o for o in options if o != state_of(hass, entity_id))}
    _fail(monkeypatch, Station, method, error)

    with pytest.raises(HomeAssistantError) as raised:
        await hass.services.async_call(
            platform, service, {ATTR_ENTITY_ID: entity_id, **data}, blocking=True
        )

    assert raised.value.translation_key == key
    assert raised.value.translation_placeholders == {
        "target": f"{_CAMERA} {name}",
        "station": _STATION,
    }
    assert f"{_STATION} " in _message(raised.value)
    assert f" {_CAMERA} {name}" in _message(raised.value)
    await _unload(hass, entry)


async def test_the_issue_example_reads_as_the_station_refusing_the_camera_light(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both devices by the names the user gave them, in the sentence the user reads."""
    seed_setting(fake_station, "spotlight_switch", 0)
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = entity_id_for(hass, "light", SYNTHETIC.camera_sn, "spotlight_switch")
    registry = dr.async_get(hass)
    registry.async_update_device(
        _device(hass, entry, SYNTHETIC.station_sn).id, name_by_user="Hallway Hub"
    )
    registry.async_update_device(
        _device(hass, entry, SYNTHETIC.camera_sn).id, name_by_user="Garage Cam"
    )
    _fail(monkeypatch, Station, "async_set_setting", CommandUnsupportedError(1350, -108))

    with pytest.raises(HomeAssistantError) as raised:
        await hass.services.async_call(
            "light", "turn_on", {ATTR_ENTITY_ID: entity_id}, blocking=True
        )

    assert _message(raised.value).startswith(
        "Hallway Hub answered that the device does not support changing Garage Cam Light."
    )
    await _unload(hass, entry)


async def test_a_camera_without_a_registered_name_is_named_by_its_eufy_name(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A device row with no name: the camera's name from the eufy device list."""
    seed_setting(fake_station, "led_on_off", 0)
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = entity_id_for(hass, "switch", SYNTHETIC.camera_sn, "led_on_off")
    name = _original_name(hass, entity_id)
    dr.async_get(hass).async_update_device(
        _device(hass, entry, SYNTHETIC.camera_sn).id, name="", name_by_user=None
    )
    _fail(monkeypatch, Station, "async_set_setting", CommandRejectedError(1350, 6, "refused"))

    with pytest.raises(HomeAssistantError) as raised:
        await hass.services.async_call(
            "switch", "turn_on", {ATTR_ENTITY_ID: entity_id}, blocking=True
        )

    assert raised.value.translation_placeholders == {
        "target": f"{_CAMERA} {name}",
        "station": _STATION,
    }
    await _unload(hass, entry)


async def test_a_message_that_does_not_name_the_station_keeps_the_setting_name(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the station's own answers carry the camera's name; the others are unchanged."""
    seed_setting(fake_station, "led_on_off", 0)
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = entity_id_for(hass, "switch", SYNTHETIC.camera_sn, "led_on_off")
    _fail(monkeypatch, Station, "async_set_setting", StationUnreachableError("no reply"))

    with pytest.raises(HomeAssistantError) as raised:
        await hass.services.async_call(
            "switch", "turn_on", {ATTR_ENTITY_ID: entity_id}, blocking=True
        )

    assert raised.value.translation_key == "station_unreachable"
    assert raised.value.translation_placeholders == {
        "target": settings_of(_CAMERA_MODEL)["led_on_off"].name
    }
    await _unload(hass, entry)


# ── setting entities of the HomeBase itself ──────────────────────────────────


@pytest.mark.parametrize(("error", "key"), _REFUSALS, ids=_REFUSAL_IDS)
async def test_a_refusal_for_the_station_own_setting_names_it_once(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    error: EufySecurityError,
    key: str,
) -> None:
    """A HomeBase's own number: the station's name, then the setting's alone."""
    seed_setting(fake_station, "alarm_volume_value", 10, channel=255)
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = entity_id_for(hass, "number", SYNTHETIC.station_sn, "alarm_volume_value")
    name = _original_name(hass, entity_id)
    _fail(monkeypatch, Station, "async_set_setting", error)

    with pytest.raises(HomeAssistantError) as raised:
        await hass.services.async_call(
            "number", "set_value", {ATTR_ENTITY_ID: entity_id, "value": 20}, blocking=True
        )

    assert raised.value.translation_key == key
    assert raised.value.translation_placeholders == {"target": name, "station": _STATION}
    assert _STATION not in raised.value.translation_placeholders["target"]
    await _unload(hass, entry)


# ── sirens and the camera's record action behind a HomeBase ─────────────────


async def test_a_camera_siren_refusal_names_the_camera_and_the_station(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = entity_id_for(hass, "siren", SYNTHETIC.camera_sn, SIREN_KEY)
    _fail(monkeypatch, Station, "async_trigger_device_alarm", AccountRejectedError(1202, -104))

    with pytest.raises(HomeAssistantError) as raised:
        await hass.services.async_call(
            "siren", "turn_on", {ATTR_ENTITY_ID: entity_id}, blocking=True
        )

    assert raised.value.translation_key == "siren_account_rejected"
    assert raised.value.translation_placeholders == {
        "target": f"{_CAMERA} Siren",
        "station": _STATION,
    }
    await _unload(hass, entry)


async def test_the_station_siren_refusal_names_the_station_once(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The account refusal names the station's own siren without the station again; the
    station's other answers keep the siren's shown name."""
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = entity_id_for(hass, "siren", SYNTHETIC.station_sn, SIREN_KEY)
    _fail(monkeypatch, Station, "async_trigger_alarm", AccountRejectedError(1201, -104))

    with pytest.raises(HomeAssistantError) as raised:
        await hass.services.async_call(
            "siren", "turn_on", {ATTR_ENTITY_ID: entity_id}, blocking=True
        )

    assert raised.value.translation_key == "siren_account_rejected"
    assert raised.value.translation_placeholders == {"target": "Siren", "station": _STATION}

    _fail(monkeypatch, Station, "async_trigger_alarm", CommandRejectedError(1201, 6, "x"))
    with pytest.raises(HomeAssistantError) as raised:
        await hass.services.async_call(
            "siren", "turn_on", {ATTR_ENTITY_ID: entity_id}, blocking=True
        )
    assert raised.value.translation_key == "siren_rejected"
    assert raised.value.translation_placeholders == {"target": f"{_STATION} Siren"}
    await _unload(hass, entry)


def _refused_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    async def capture(
        self: StreamBroadcast,
        seconds: float,
        write: ClipWriter,
        *,
        start_timeout: float | None = None,
        stop: Any = None,
    ) -> MediaClip:
        raise AccountRejectedError(1003, -104)

    monkeypatch.setattr(StreamBroadcast, "async_capture", capture)


async def _record(hass: HomeAssistant, serial: str) -> None:
    await hass.services.async_call(
        DOMAIN,
        SERVICE_RECORD,
        {ATTR_ENTITY_ID: entity_id_for(hass, "camera", serial, CAMERA_KEY)},
        blocking=True,
        return_response=True,
    )


async def test_a_refused_recording_names_the_camera_and_the_station(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _refused_capture(monkeypatch)
    entry = await set_up_warm(hass, seed_warm_cache)
    dr.async_get(hass).async_update_device(
        _device(hass, entry, SYNTHETIC.camera_sn).id, name_by_user="Garage Cam"
    )

    with pytest.raises(HomeAssistantError) as raised:
        await _record(hass, SYNTHETIC.camera_sn)

    assert raised.value.translation_key == "account_rejected"
    assert raised.value.translation_placeholders == {"target": "Garage Cam", "station": _STATION}
    await _unload(hass, entry)


# ── pan/tilt and presets of a camera behind a HomeBase ───────────────────────

# An eufyCam S4: it pans, tilts and zooms behind a HomeBase.
_PTZ_MODEL: Final = "T8172"


def _as_ptz_camera(cloud: FakeCloud) -> None:
    for device in cloud.devices:
        if device.get("device_sn") == SYNTHETIC.camera_sn:
            device["device_new_pn"] = _PTZ_MODEL


_ACCOUNT: Final = AccountRejectedError(6033, -104)


@pytest.mark.parametrize(
    ("entity_key", "call", "error", "key"),
    [
        (PAN_LEFT_KEY, "press", _ACCOUNT, "account_rejected"),
        (SAVE_VIEW_KEY, "press", _ACCOUNT, "account_rejected"),
        (DEFAULT_PRESET_KEY, "select", _ACCOUNT, "account_rejected"),
        (DEFAULT_PRESET_KEY, "select", CommandNotAppliedError(1350), "setting_not_applied"),
        (LIVE_PRESET_KEY, "select", _ACCOUNT, "account_rejected"),
        (LIVE_ZOOM_KEY, "zoom", _ACCOUNT, "account_rejected"),
        (CAMERA_KEY, SERVICE_GOTO_PRESET, _ACCOUNT, "account_rejected"),
    ],
    ids=lambda value: value if isinstance(value, str) else type(value).__name__,
)
async def test_a_refused_camera_command_names_the_camera_and_the_station(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    entity_key: str,
    call: str,
    error: EufySecurityError,
    key: str,
) -> None:
    """Pan/tilt, preset and zoom refusals by the station name the camera with the
    control (or the action) and the station."""
    _as_ptz_camera(fake_cloud)
    for owner, method in (
        (Station, "async_pan_tilt"),
        (Station, "async_store_preset"),
        (Station, "async_set_default_preset"),
        (StreamManager, "async_turn_live_view"),
        (StreamManager, "async_set_live_zoom"),
        (StreamManager, "async_goto_preset"),
    ):
        _fail(monkeypatch, owner, method, error)
    entry = await set_up_warm(hass, seed_warm_cache)
    platform = {"press": "button", "select": "select", "zoom": "number"}.get(call, "camera")
    entity_id = entity_id_for(hass, platform, SYNTHETIC.camera_sn, entity_key)
    entity = _entity(hass, entity_id)
    expected = f"{_CAMERA} {call}" if platform == "camera" else f"{_CAMERA} {entity.name}"

    with pytest.raises(HomeAssistantError) as raised:
        if call == "press":
            await entity.async_press()
        elif call == "select":
            await entity.async_select_option("1")
        elif call == "zoom":
            await entity.async_set_native_value(2.0)
        else:
            await hass.services.async_call(
                DOMAIN, call, {ATTR_ENTITY_ID: entity_id, "preset": 1}, blocking=True
            )

    assert raised.value.translation_key == key
    assert raised.value.translation_placeholders == {"target": expected, "station": _STATION}
    await _unload(hass, entry)


# ── naming rules, on a stand-in coordinator ──────────────────────────────────


def _coordinator(hass: HomeAssistant, entry: MockConfigEntry, devices: dict[str, str]) -> Any:
    """What the naming helpers read: the station's serial and eufy device names."""
    station = SimpleNamespace(
        serial=SYNTHETIC.station_sn,
        devices=tuple(SimpleNamespace(device_sn=sn, name=n) for sn, n in devices.items()),
    )
    return SimpleNamespace(hass=hass, config_entry=entry, station=station)


@pytest.mark.parametrize(
    ("registered", "by_user", "eufy", "expected"),
    [
        # The user's name for the device, over its own.
        ("Front", "Garage Cam", "Front", "Garage Cam Light"),
        ("Front", None, "Eufy front", "Front Light"),
        # A row without a name, or no row: the eufy device list's name.
        ("", None, "Eufy front", "Eufy front Light"),
        (None, None, "Eufy front", "Eufy front Light"),
        # No name anywhere: the setting's alone.
        ("", None, "", "Light"),
        (None, None, None, "Light"),
    ],
)
async def test_a_paired_device_is_named_as_home_assistant_names_it(
    hass: HomeAssistant,
    registered: str | None,
    by_user: str | None,
    eufy: str | None,
    expected: str,
) -> None:
    """``registered`` is the device row's name (None: no row), ``eufy`` the device list's."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    if registered is not None:
        devices = dr.async_get(hass)
        row = devices.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(DOMAIN, SYNTHETIC.camera_sn)}
        )
        devices.async_update_device(row.id, name=registered, name_by_user=by_user)
    coordinator = _coordinator(hass, entry, {} if eufy is None else {SYNTHETIC.camera_sn: eufy})

    assert errors.station_named_target(coordinator, SYNTHETIC.camera_sn, "Light") == expected


@pytest.mark.parametrize("device_sn", [None, SYNTHETIC.station_sn], ids=["none", "own serial"])
async def test_the_station_itself_is_not_named_again(
    hass: HomeAssistant, device_sn: str | None
) -> None:
    """The station's own entity, keyed by no serial or by its own: the target alone,
    whatever its registered and eufy names."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, SYNTHETIC.station_sn)},
        name=_STATION,
    )
    coordinator = _coordinator(hass, entry, {SYNTHETIC.station_sn: _STATION})

    assert errors.device_display_name(coordinator, device_sn) == ""
    assert errors.station_named_target(coordinator, device_sn, "Light") == "Light"


def test_only_the_station_named_messages_take_the_station_target() -> None:
    """Every setting-write outcome: the station's answers name ``station_target``, the
    others ``target``."""
    for error, key in (
        *_REFUSALS,
        (StationUnreachableError("x"), "station_unreachable"),
        (KeyRejectedError("x"), "station_key_rejected"),
    ):
        translated = errors.setting_write_failed(
            error, "Light", station_name=_STATION, station_target="Front Light"
        )
        assert translated.translation_key == key
        named = key in _REFUSAL_IDS
        assert translated.translation_placeholders == (
            {"target": "Front Light", "station": _STATION} if named else {"target": "Light"}
        ), key


# ── a standalone camera: the station is the camera ───────────────────────────

_SOLO_SN: Final = "T8170P0000000001"
_SOLO: Final = "Solo"


class TestStandaloneCamera:
    """A camera that is its own station names itself once."""

    @pytest.fixture
    async def fake_station(self) -> Any:
        station = FakeStation(
            serial=_SOLO_SN,
            cipher_id=98,
            receipt_len=STANDALONE_RECEIPT_LEN,
            guard_mode=1,
            params={
                48: {1224: "1", 1101: "61", 1142: "-27", 1131: "1"},
                255: {1216: _SOLO},
            },
        )
        await station.start()
        yield station
        station.stop()

    @pytest.fixture
    def fake_cloud(self, fake_station: FakeStation) -> FakeCloud:
        return FakeCloud(
            devices=[
                {
                    "device_sn": _SOLO_SN,
                    "device_type": 48,
                    "device_name": _SOLO,
                    "parent_sn": _SOLO_SN,
                    "device_channel": 48,
                    "p2p_did": SYNTHETIC.did,
                    "local_ip": SYNTHETIC.station_ip,
                    "params": [{"param_type": 1224, "param_value": "1", "update_time": 1.7e9}],
                }
            ],
            owner_ids={_SOLO_SN: fake_station.account_id},
            cipher_keys={_SOLO_SN: fake_station.ecc_private_key_hex},
        )

    @pytest.mark.parametrize(("error", "key"), _REFUSALS, ids=_REFUSAL_IDS)
    async def test_its_setting_refusal_names_it_once(
        self,
        hass: HomeAssistant,
        fake_station: FakeStation,
        built_clients: list[EufySecurity],
        seed_warm_cache: Callable[..., None],
        monkeypatch: pytest.MonkeyPatch,
        error: EufySecurityError,
        key: str,
    ) -> None:
        entry = await set_up_warm(hass, seed_warm_cache)
        entity_id = entity_id_for(hass, "switch", _SOLO_SN, "led_on_off")
        name = _original_name(hass, entity_id)
        _fail(monkeypatch, Station, "async_set_setting", error)

        with pytest.raises(HomeAssistantError) as raised:
            await hass.services.async_call(
                "switch", "turn_on", {ATTR_ENTITY_ID: entity_id}, blocking=True
            )

        assert raised.value.translation_key == key
        assert raised.value.translation_placeholders == {"target": name, "station": _SOLO}
        await _unload(hass, entry)

    async def test_its_siren_refusal_names_it_once(
        self,
        hass: HomeAssistant,
        fake_station: FakeStation,
        built_clients: list[EufySecurity],
        seed_warm_cache: Callable[..., None],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        entry = await set_up_warm(hass, seed_warm_cache)
        entity_id = entity_id_for(hass, "siren", _SOLO_SN, SIREN_KEY)
        _fail(monkeypatch, Station, "async_trigger_device_alarm", AccountRejectedError(1202, -104))

        with pytest.raises(HomeAssistantError) as raised:
            await hass.services.async_call(
                "siren", "turn_on", {ATTR_ENTITY_ID: entity_id}, blocking=True
            )

        assert raised.value.translation_placeholders == {"target": "Siren", "station": _SOLO}
        await _unload(hass, entry)

    async def test_its_refused_recording_names_the_action(
        self,
        hass: HomeAssistant,
        fake_station: FakeStation,
        built_clients: list[EufySecurity],
        seed_warm_cache: Callable[..., None],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _refused_capture(monkeypatch)
        entry = await set_up_warm(hass, seed_warm_cache)

        with pytest.raises(HomeAssistantError) as raised:
            await _record(hass, _SOLO_SN)

        assert raised.value.translation_key == "account_rejected"
        assert raised.value.translation_placeholders == {
            "target": SERVICE_RECORD,
            "station": _SOLO,
        }
        await _unload(hass, entry)

    async def test_its_pan_tilt_refusal_names_it_once(
        self,
        hass: HomeAssistant,
        fake_station: FakeStation,
        built_clients: list[EufySecurity],
        seed_warm_cache: Callable[..., None],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _fail(monkeypatch, Station, "async_pan_tilt", AccountRejectedError(6033, -104))
        entry = await set_up_warm(hass, seed_warm_cache)
        entity_id = entity_id_for(hass, "button", _SOLO_SN, PAN_LEFT_KEY)

        with pytest.raises(HomeAssistantError) as raised:
            await hass.services.async_call(
                "button", "press", {ATTR_ENTITY_ID: entity_id}, blocking=True
            )

        assert raised.value.translation_placeholders == {"target": "Pan left", "station": _SOLO}
        await _unload(hass, entry)
