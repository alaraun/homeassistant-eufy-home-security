"""The settings layout, HomeBase, mode-table and arming-group websocket commands, the
security-mode select and the clean-up of stale setting rows, on the real library with its
FakeCloud and FakeStation."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime, time
from typing import Any, Final

import pytest
from conftest import (
    SENSOR_SN,
    add_entry,
    add_motion_sensor,
    advance_to_poll,
    alarm_changed,
    entity_id_for,
    panel_entity_id,
    record_states,
    seed_setting,
    set_guard_mode,
    set_up_warm,
    setting_param,
    setup_entry,
    state_of,
    wait_until,
)
from eufy_home_security import (
    MODE_ACTION_ALARM_AFFECTING,
    MODE_DELAY_CHOICES,
    SETTING_SECTIONS,
    AlarmChanged,
    CloudApiError,
    EufySecurity,
    EufySecurityError,
    EventSource,
    GuardMode,
    ModeInUseError,
    ScheduleSlot,
    StationConfigChanged,
    StationFeature,
    Weekday,
    redact_serial,
    setting_section,
)
from eufy_home_security.devices import mode_action_label
from eufy_home_security.p2p import FrameType
from eufy_home_security.station import MIN_TRACKING_CAMERAS
from eufy_home_security.testing import (
    SYNTHETIC,
    FakeCloud,
    FakeStation,
    camera_device,
    mode_list_params,
)
from homeassistant.components.alarm_control_panel import DOMAIN as ALARM_DOMAIN
from homeassistant.components.number import DOMAIN as NUMBER_DOMAIN
from homeassistant.components.select import ATTR_OPTION, ATTR_OPTIONS, SERVICE_SELECT_OPTION
from homeassistant.components.select import DOMAIN as SELECT_DOMAIN
from homeassistant.const import ATTR_ENTITY_ID, EntityCategory, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.typing import (
    MockHAClientWebSocket,
    WebSocketGenerator,
)

from custom_components.eufy_home_security import detections, homebase, select, settings_layout
from custom_components.eufy_home_security.const import (
    ATTR_SECTION,
    ATTR_VALUE_SOURCE,
    DEFAULT_PRESET_KEY,
    DOMAIN,
    FIRMWARE_KEY,
    POLL_INTERVAL_SECONDS,
    SECURITY_MODE_KEY,
)
from custom_components.eufy_home_security.settings import is_setting_row, setting_specs

_HOUSE: Final = "house-1"
_GROUP: Final = "group-1"
_CAMERA_ACTION_AWAY: Final = "camera_action_away"
_CAMERA_CHANNEL: Final = 0
_ALARM_VOLUME: Final = "alarm_volume_value"


def _device_id(hass: HomeAssistant, serial: str) -> str:
    """The HA device id of ``serial`` (or of the account, by its entry id) in the one entry."""
    (entry,) = hass.config_entries.async_entries(DOMAIN)
    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, serial), entry.entry_id)
    assert device is not None
    return device.id


async def _ws(client: MockHAClientWebSocket, command: str, **fields: Any) -> dict[str, Any]:
    await client.send_json_auto_id({"type": command, **fields})
    reply: dict[str, Any] = await client.receive_json()
    return reply


async def _ok(client: MockHAClientWebSocket, command: str, **fields: Any) -> dict[str, Any]:
    reply = await _ws(client, command, **fields)
    assert reply["success"], reply
    result: dict[str, Any] = reply["result"]
    return result


def _no_serial(result: Any) -> None:
    """No serial of the fixture, plain or redacted, anywhere in ``result``."""
    text = json.dumps(result)
    for serial in (SYNTHETIC.station_sn, SYNTHETIC.camera_sn):
        assert serial not in text
        assert redact_serial(serial) not in text


def _member(serial: str, mode: int, title: str) -> dict[str, Any]:
    return {"device_sn": serial, "mode": title, "mode_id": mode}


def _house(cloud: FakeCloud, *groups: dict[str, Any]) -> None:
    """One house holding the account's devices, with ``groups`` as the cloud holds them."""
    cloud.houses = [{"house_id": _HOUSE, "house_name": "Home"}]
    cloud.house_devices = {_HOUSE: cloud.devices}
    cloud.arming_groups = {_HOUSE: list(groups)}


def _group(name: str = "Night", mode: int = 0, title: str = "Away") -> dict[str, Any]:
    return {
        "group_id": _GROUP,
        "group_name": name,
        "status": "0",
        "default": 1,
        "devices": [_member(SYNTHETIC.station_sn, mode, title)],
    }


# ── settings layout ──────────────────────────────────────────────────────────


async def test_the_layout_lists_a_cameras_setting_entities_by_library_section_in_order(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Sections in the library's order with its headings; entity ids in ``settings_for``
    order, disabled ones included, entities without a section left out. The camera's
    Modes section holds only per-mode settings, which have no entities: not listed."""
    entry = await set_up_warm(hass, seed_warm_cache)
    station = entry.runtime_data.coordinators[SYNTHETIC.station_sn].station
    camera_id = _device_id(hass, SYNTHETIC.camera_sn)
    client = await hass_ws_client(hass)

    result = await _ok(client, settings_layout.WS_SETTINGS_LAYOUT, device_id=camera_id)

    assert result["device_id"] == camera_id
    sections = result["sections"]
    known = [s.id for s in SETTING_SECTIONS]
    ids = [s["id"] for s in sections]
    assert ids == sorted(ids, key=known.index)
    assert "modes" not in ids
    for section in sections:
        info = setting_section(section["id"])
        assert (section["heading"], section["order"]) == (info.heading, info.order)
        assert section["alarm_affecting"] is info.alarm_affecting
        assert section["entity_ids"]
    registry = er.async_get(hass)
    by_section: dict[str, list[str | None]] = {}
    alarm_by_section: dict[str, list[str | None]] = {}
    for spec in setting_specs(station):
        if spec.device_sn == SYNTHETIC.camera_sn:
            entity_id = registry.async_get_entity_id(
                spec.platform, DOMAIN, f"{SYNTHETIC.camera_sn}_{spec.key}"
            )
            by_section.setdefault(spec.setting.section, []).append(entity_id)
            alarm_ids = alarm_by_section.setdefault(spec.setting.section, [])
            if spec.setting.alarm_affecting:
                alarm_ids.append(entity_id)
    assert {s["id"]: s["entity_ids"] for s in sections} == by_section
    assert {s["id"]: s["alarm_affecting_entity_ids"] for s in sections} == alarm_by_section
    listed = [entity_id for section in sections for entity_id in section["entity_ids"]]
    disabled = [
        e
        for e in listed
        if registry.async_get(e).disabled_by is not None  # type: ignore[union-attr]
    ]
    assert disabled, "a disabled setting entity is listed"
    camera_entity = entity_id_for(hass, "camera", SYNTHETIC.camera_sn, "camera")
    assert camera_entity not in listed
    _no_serial(result)
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_the_layout_of_the_homebase_has_its_own_settings_and_no_pan_tilt(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The station's layout holds the station's settings (the alarm volume in ``alarm``,
    which is alarm-affecting: the section and the entity are marked)."""
    entry = await set_up_warm(hass, seed_warm_cache)
    client = await hass_ws_client(hass)

    result = await _ok(
        client,
        settings_layout.WS_SETTINGS_LAYOUT,
        device_id=_device_id(hass, SYNTHETIC.station_sn),
    )

    sections = {s["id"]: s["entity_ids"] for s in result["sections"]}
    volume = entity_id_for(hass, NUMBER_DOMAIN, SYNTHETIC.station_sn, _ALARM_VOLUME)
    assert volume in sections["alarm"]
    alarm = next(s for s in result["sections"] if s["id"] == "alarm")
    assert alarm["alarm_affecting"] is True
    assert volume in alarm["alarm_affecting_entity_ids"]
    assert set(alarm["alarm_affecting_entity_ids"]) <= set(alarm["entity_ids"])
    assert "ptz" not in sections
    assert entity_id_for(hass, ALARM_DOMAIN, SYNTHETIC.station_sn, "guard_mode") not in {
        e for ids in sections.values() for e in ids
    }
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_the_layout_adds_the_integrations_preset_selects_to_pan_and_tilt(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A preset camera's default (and live-view) preset select are listed under ``ptz``."""
    monkeypatch.setattr(
        detections, "has_preset_entities", lambda _station, serial: serial == SYNTHETIC.camera_sn
    )
    entry = await set_up_warm(hass, seed_warm_cache)
    client = await hass_ws_client(hass)

    result = await _ok(
        client,
        settings_layout.WS_SETTINGS_LAYOUT,
        device_id=_device_id(hass, SYNTHETIC.camera_sn),
    )

    ptz = next(s for s in result["sections"] if s["id"] == "ptz")
    assert ptz["heading"] == setting_section("ptz").heading
    default = entity_id_for(hass, SELECT_DOMAIN, SYNTHETIC.camera_sn, DEFAULT_PRESET_KEY)
    assert default in ptz["entity_ids"]
    assert default not in ptz["alarm_affecting_entity_ids"]
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_the_layout_refuses_an_unknown_device_and_an_unloaded_account(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """``not_found`` for an unknown id and the account's own device; ``unavailable`` unloaded."""
    entry = await set_up_warm(hass, seed_warm_cache)
    client = await hass_ws_client(hass)
    account = _device_id(hass, entry.entry_id)
    camera = _device_id(hass, SYNTHETIC.camera_sn)

    for device_id in ("nope", account):
        reply = await _ws(client, settings_layout.WS_SETTINGS_LAYOUT, device_id=device_id)
        assert not reply["success"]
        assert reply["error"]["code"] == "not_found"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    reply = await _ws(client, settings_layout.WS_SETTINGS_LAYOUT, device_id=camera)
    assert reply["error"]["code"] == "unavailable"


# ── setting entity attributes ────────────────────────────────────────────────


async def test_a_setting_entity_names_its_section_and_value_source_kept_out_of_the_recorder(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """``section`` is the library's id; ``value_source`` the library's source (``p2p`` for
    a value in the dump, ``app_default`` for the station default); both unrecorded."""
    seed_setting(fake_station, "watermark_set", 1)
    entry = await set_up_warm(hass, seed_warm_cache)
    station = entry.runtime_data.coordinators[SYNTHETIC.station_sn].station

    watermark = hass.states.get(
        entity_id_for(hass, SELECT_DOMAIN, SYNTHETIC.camera_sn, "watermark_set")
    )
    assert watermark is not None
    expected = station.setting("watermark_set", device_sn=SYNTHETIC.camera_sn).section
    assert watermark.attributes[ATTR_SECTION] == expected
    assert watermark.attributes[ATTR_VALUE_SOURCE] == "p2p"
    assert watermark.state_info is not None
    assert {ATTR_SECTION, ATTR_VALUE_SOURCE} <= watermark.state_info["unrecorded_attributes"]

    volume = hass.states.get(
        entity_id_for(hass, NUMBER_DOMAIN, SYNTHETIC.station_sn, _ALARM_VOLUME)
    )
    assert volume is not None
    assert volume.attributes[ATTR_SECTION] == "alarm"
    assert volume.attributes[ATTR_VALUE_SOURCE] == "app_default"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_a_setting_without_a_value_has_no_value_source(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A camera setting its block does not report shows unknown and no ``value_source``."""
    entry = await set_up_warm(hass, seed_warm_cache)
    state = hass.states.get(
        entity_id_for(hass, SELECT_DOMAIN, SYNTHETIC.camera_sn, "watermark_set")
    )
    assert state is not None
    assert state.state == "unknown"
    assert ATTR_SECTION in state.attributes
    assert ATTR_VALUE_SOURCE not in state.attributes
    assert await hass.config_entries.async_unload(entry.entry_id)


# ── homebase ─────────────────────────────────────────────────────────────────


_NIGHT: Final = "Night watch"


async def _payload(client: MockHAClientWebSocket, hass: HomeAssistant) -> dict[str, Any]:
    return await _ok(client, homebase.WS_HOMEBASE, device_id=_device_id(hass, SYNTHETIC.station_sn))


async def test_the_homebase_payload_is_the_stations_configuration_by_device_id(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Defined modes with the library's names (a custom title included) and settable;
    support per feature with what the integration writes; flag labels from the library;
    the camera's mode table by HA device id; status; never a serial."""
    fake_station.set_mode_list({GuardMode.CUSTOM_1: _NIGHT})
    seed_setting(fake_station, _CAMERA_ACTION_AWAY, 9)  # record + notification
    seed_setting(fake_station, "alarm_delay_away", 30)
    entry = await set_up_warm(hass, seed_warm_cache)
    station = entry.runtime_data.coordinators[SYNTHETIC.station_sn].station
    client = await hass_ws_client(hass)
    station_id = _device_id(hass, SYNTHETIC.station_sn)
    camera_id = _device_id(hass, SYNTHETIC.camera_sn)

    result = await _payload(client, hass)

    config = station.config
    assert config is not None
    assert result["device_id"] == station_id
    assert result["revision"] == config.revision
    assert result["alarm_panel"] == panel_entity_id(hass)
    assert result["security_mode"] == entity_id_for(
        hass, SELECT_DOMAIN, SYNTHETIC.station_sn, SECURITY_MODE_KEY
    )
    assert (result["guard_mode"], result["effective_mode"]) == ("away", "away")
    assert result["connected"] is True
    assert result["alarm"] == {"triggered": False}
    assert result["modes"] == [
        {"id": "home", "name": "Home", "custom": False, "settable": True, "option": "Home"},
        {"id": "away", "name": "Away", "custom": False, "settable": True, "option": "Away"},
        {"id": "custom_1", "name": _NIGHT, "custom": True, "settable": True, "option": _NIGHT},
        {
            "id": "disarmed",
            "name": "Disarmed",
            "custom": False,
            "settable": True,
            "option": "Disarmed",
        },
    ]
    options = hass.states.get(
        entity_id_for(hass, SELECT_DOMAIN, SYNTHETIC.station_sn, SECURITY_MODE_KEY)
    )
    assert options is not None
    assert [m["option"] for m in result["modes"]] == options.attributes[ATTR_OPTIONS]
    support = result["support"]
    assert set(support) == {feature.value for feature in StationFeature}
    for feature in StationFeature:
        evidence = station.config_support(feature)
        assert support[feature.value]["grade"] == evidence.support.value
        assert support[feature.value]["note"] == (evidence.note or None)
        assert support[feature.value]["alarm_affecting"] is feature.alarm_affecting
    # Every feature has its write; the working mode's is its setting entity, which the
    # HomeBase 3 has.
    assert all(v["available"] for v in support.values())
    assert "working_mode" in config.setting_keys
    assert result["flags"]["camera"][0] == {
        "id": "record",
        "label": mode_action_label("record"),
        "alarm_affecting": False,
    }
    for flags in result["flags"].values():
        for flag in flags:
            assert flag["alarm_affecting"] is (flag["id"] in MODE_ACTION_ALARM_AFFECTING)
    assert any(f["alarm_affecting"] for f in result["flags"]["camera"])
    assert result["delay_choices"] == list(MODE_DELAY_CHOICES)
    (camera,) = result["devices"]
    assert camera == {
        "device_id": camera_id,
        "name": dr.async_get(hass).async_get(camera_id).name,  # type: ignore[union-attr]
        "kind": "camera",
        "model": camera["model"],
        "online": camera["online"],
        "battery": camera["battery"],
    }
    assert "flags" not in camera  # the camera kind's own list
    tables = result["mode_tables"]
    assert set(tables) == {"home", "away", "custom_1", "disarmed"}
    away = tables["away"]["devices"][camera_id]
    assert away["flags"]["record"] is True
    assert away["flags"]["notification"] is True
    assert away["flags"]["camera_siren"] is False
    assert (away["entry"], away["leaving"]) == (30, 0)
    assert tables["home"] is None
    assert result["schedule"] is None
    assert result["status"]["update"] == entity_id_for(
        hass, "update", SYNTHETIC.station_sn, FIRMWARE_KEY
    )
    assert set(result["status"]["storage"]) == {"disk", "emmc"}
    assert result["sources"]["modes"] == "p2p"
    assert result["unreadable"] == {}
    _no_serial(result)

    reply = await _ws(client, homebase.WS_HOMEBASE, device_id=camera_id)
    assert reply["error"]["code"] == "not_found"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_the_payload_shows_a_sounding_alarm_as_the_station_reports_it(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """``alarm.triggered`` is the station's ``alarming`` (its tone frames), not the panel:
    a panel turned triggered without the station sounding leaves it false. ``alarm.last``
    is the station's last transition (when seen, its own time or null), absent before."""
    entry = await set_up_warm(hass, seed_warm_cache)
    client = await hass_ws_client(hass)

    entry.runtime_data.router.handle(alarm_changed(alarming=True))
    await hass.async_block_till_done()
    assert state_of(hass, panel_entity_id(hass)) == "triggered"
    assert (await _payload(client, hass))["alarm"] == {"triggered": False}

    fake_station.send_alarm_frame(FrameType.ALARM_TONE_NOTIFY, 3, 30, channel=1)
    station = entry.runtime_data.coordinators[SYNTHETIC.station_sn].station
    await wait_until(lambda: station.alarming)
    last = station.last_alarm
    assert last is not None
    assert (await _payload(client, hass))["alarm"] == {
        "triggered": True,
        "last": {"at": last.at.isoformat(), "event_time": None},
    }

    seen = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    pushed = datetime(2026, 1, 2, 3, 4, 1, tzinfo=UTC)
    station.note_alarm(
        AlarmChanged(
            station_sn=SYNTHETIC.station_sn,
            alarming=False,
            source=EventSource.CLOUD,
            at=seen,
            event_time=pushed,
        )
    )
    assert (await _payload(client, hass))["alarm"] == {
        "triggered": False,
        "last": {"at": "2026-01-02T03:04:05+00:00", "event_time": "2026-01-02T03:04:01+00:00"},
    }
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_the_payload_carries_the_schedule_and_a_reported_off_that_cannot_be_set(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Schedule slots by weekday and ``HH:MM`` with mode ids; Off reported is a mode that
    is not settable; an entry sensor lists its own flags (no motion response)."""
    door_sn = "T8900P0000000001"
    fake_cloud.devices.append(
        camera_device(door_sn, station_sn=fake_station.serial, channel=2, name="Door")
    )
    fake_station.params[2] = {1101: "90"}
    fake_station.set_schedule([ScheduleSlot(Weekday.MONDAY, time(8), time(17, 30), GuardMode.HOME)])
    set_guard_mode(fake_station, int(GuardMode.OFF))
    entry = await set_up_warm(hass, seed_warm_cache)
    client = await hass_ws_client(hass)

    result = await _payload(client, hass)

    assert result["schedule"]["slots"] == [
        {"weekday": 1, "start": "08:00", "end": "17:30", "mode": "home"}
    ]
    modes = {m["id"]: m for m in result["modes"]}
    assert modes["schedule"] == {
        "id": "schedule",
        "name": "Schedule",
        "custom": False,
        "settable": True,
        "option": "Schedule",
    }
    # Off in force: shown by the select, so it has its option.
    assert modes["off"] == {
        "id": "off",
        "name": "Off",
        "custom": False,
        "settable": False,
        "option": "Off",
    }
    assert result["guard_mode"] == "off"
    door = next(d for d in result["devices"] if d["name"] == "Door")
    assert door["kind"] == "sensor"
    assert door["flags"] == ["station_alarm", "notification", "report_monitor_center"]
    assert door_sn not in json.dumps(result)
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_a_mode_list_only_the_cloud_holds_is_named_with_its_source(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A custom mode from the cloud's copy of the station's parameters is listed by its
    title; ``sources.modes`` names the cloud."""
    fake_cloud.set_station_params(
        fake_station.serial, mode_list_params({GuardMode.CUSTOM_2: "Garden"})
    )
    entry = await set_up_warm(hass, seed_warm_cache)
    client = await hass_ws_client(hass)

    result = await _payload(client, hass)

    assert {"id": "custom_2", "name": "Garden"}.items() <= next(
        m for m in result["modes"] if m["id"] == "custom_2"
    ).items()
    assert result["sources"]["modes"] == "cloud"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_a_mode_option_is_the_selects_own_for_a_name_two_modes_share(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A custom mode named like a built-in one keeps its name; its ``option`` is the
    select's distinct option string."""
    fake_station.set_mode_list({GuardMode.CUSTOM_1: "Away"})
    entry = await set_up_warm(hass, seed_warm_cache)
    client = await hass_ws_client(hass)

    result = await _payload(client, hass)

    custom = next(m for m in result["modes"] if m["id"] == "custom_1")
    assert (custom["name"], custom["option"]) == ("Away", "Away (Custom 1)")
    state = hass.states.get(_security_mode(hass))
    assert state is not None
    assert [m["option"] for m in result["modes"]] == state.attributes[ATTR_OPTIONS]
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    ("mode", "text"),
    [(GuardMode.CUSTOM_1, "custom_1"), (GuardMode.DISARMED, "disarmed"), (99, "code_99")],
)
def test_a_mode_id_reads_back_as_its_mode(mode: GuardMode | int, text: str) -> None:
    assert homebase.mode_id(mode) == text
    assert homebase.parse_mode_id(text) == mode


def test_a_code_id_of_a_named_mode_is_that_mode_and_junk_is_refused() -> None:
    assert homebase.parse_mode_id("code_3") is GuardMode.CUSTOM_1
    for junk in ("sometimes", "code_x", ""):
        with pytest.raises(ValueError):
            homebase.parse_mode_id(junk)


# ── homebase/subscribe ───────────────────────────────────────────────────────


async def _event(client: MockHAClientWebSocket) -> dict[str, Any]:
    async with asyncio.timeout(5):
        message = await client.receive_json()
    assert message["type"] == "event", message
    event: dict[str, Any] = message["event"]
    return event


async def test_a_subscription_sends_the_payload_now_and_after_a_config_change(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The first event is the current payload; a custom mode the station gains arrives
    in the next event, after the debounce."""
    entry = await set_up_warm(hass, seed_warm_cache)
    client = await hass_ws_client(hass)
    station_id = _device_id(hass, SYNTHETIC.station_sn)

    await client.send_json_auto_id(
        {"type": homebase.WS_HOMEBASE_SUBSCRIBE, "device_id": station_id}
    )
    reply = await client.receive_json()
    assert reply["success"], reply
    first = await _event(client)
    assert first["device_id"] == station_id
    assert [m["id"] for m in first["modes"]] == ["home", "away", "disarmed"]

    fake_station.set_mode_list({GuardMode.CUSTOM_1: _NIGHT})
    await advance_to_poll(hass, POLL_INTERVAL_SECONDS + 1)
    await advance_to_poll(hass, POLL_INTERVAL_SECONDS + 2)  # the debounce
    changed = await _event(client)
    assert {
        "id": "custom_1",
        "name": _NIGHT,
        "custom": True,
        "settable": True,
        "option": _NIGHT,
    } in changed["modes"]
    assert changed["revision"] != first["revision"]
    _no_serial(changed)
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_a_subscription_follows_the_entry_across_a_reload(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """After a reload the subscription sends the reloaded entry's payload at once, and
    follows its alarm panel."""
    entry = await set_up_warm(hass, seed_warm_cache)
    client = await hass_ws_client(hass)
    station_id = _device_id(hass, SYNTHETIC.station_sn)
    await client.send_json_auto_id(
        {"type": homebase.WS_HOMEBASE_SUBSCRIBE, "device_id": station_id}
    )
    assert (await client.receive_json())["success"]
    await _event(client)

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    again = await _event(client)
    assert again["device_id"] == station_id
    assert again["alarm"] == {"triggered": False}
    fake_station.send_alarm_frame(FrameType.ALARM_TONE_NOTIFY, 3, 30, channel=1)
    await wait_until(lambda: state_of(hass, panel_entity_id(hass)) == "triggered")
    await advance_to_poll(hass, 1)  # the debounce
    assert (await _event(client))["alarm"]["triggered"] is True
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_a_subscription_to_a_paired_device_is_refused(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    entry = await set_up_warm(hass, seed_warm_cache)
    client = await hass_ws_client(hass)
    reply = await _ws(
        client,
        homebase.WS_HOMEBASE_SUBSCRIBE,
        device_id=_device_id(hass, SYNTHETIC.camera_sn),
    )
    assert reply["error"]["code"] == "not_found"
    assert await hass.config_entries.async_unload(entry.entry_id)


# ── homebase/mode_table/set ──────────────────────────────────────────────────


async def _set_table(
    client: MockHAClientWebSocket, hass: HomeAssistant, revision: int, **fields: Any
) -> dict[str, Any]:
    camera_id = _device_id(hass, SYNTHETIC.camera_sn)
    request: dict[str, Any] = {
        "device_id": _device_id(hass, SYNTHETIC.station_sn),
        "mode": "away",
        "actions": {camera_id: {"camera_siren": True}},
        "expect_revision": revision,
    }
    request.update(fields)
    return await _ws(client, homebase.WS_MODE_TABLE_SET, **request)


@pytest.fixture
def away_table(fake_station: FakeStation) -> None:
    """The camera's Away table: record + notification, no delays."""
    seed_setting(fake_station, _CAMERA_ACTION_AWAY, 9)
    seed_setting(fake_station, "alarm_delay_away", 0)
    seed_setting(fake_station, "leaving_delay_away", 0)


@pytest.mark.usefixtures("away_table")
async def test_a_mode_table_write_is_one_table_and_answers_the_new_payload(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Actions and the entry delay by HA device id, sent as one Away table that keeps the
    mask's unnamed bits; the answer carries the table read back and a new revision."""
    unnamed = 0x100  # a mask bit no flag names
    seed_setting(fake_station, _CAMERA_ACTION_AWAY, 9 | unnamed)
    entry = await set_up_warm(hass, seed_warm_cache)
    client = await hass_ws_client(hass)
    camera_id = _device_id(hass, SYNTHETIC.camera_sn)
    before = await _payload(client, hass)

    reply = await _set_table(
        client,
        hass,
        before["revision"],
        entry_s=30,
        entry_devices={camera_id: True},
    )

    assert reply["success"], reply
    result = reply["result"]
    assert len(fake_station.mode_tables_received) == 1
    camera_params = fake_station.params[_CAMERA_CHANNEL]
    assert camera_params[setting_param(_CAMERA_ACTION_AWAY)] == str(9 | 2 | unnamed)
    assert camera_params[setting_param("alarm_delay_away")] == "30"
    away = result["mode_tables"]["away"]["devices"][camera_id]
    assert away["flags"]["camera_siren"] is True
    assert away["flags"]["record"] is True
    assert away["entry"] == 30
    assert result["revision"] != before["revision"]
    _no_serial(result)
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.usefixtures("away_table")
async def test_a_stale_revision_is_config_changed_and_sends_nothing(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    entry = await set_up_warm(hass, seed_warm_cache)
    client = await hass_ws_client(hass)
    revision = (await _payload(client, hass))["revision"]

    reply = await _set_table(client, hass, revision + 1)

    assert reply["error"]["code"] == "config_changed"
    assert fake_station.mode_tables_received == []
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.usefixtures("away_table")
async def test_a_table_the_station_does_not_apply_is_not_applied_named_without_serial(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    short_readback: None,
) -> None:
    """A read-back that does not show the write: ``not_applied`` with the library's
    message, the camera named by its HA name."""
    entry = await set_up_warm(hass, seed_warm_cache)
    client = await hass_ws_client(hass)
    revision = (await _payload(client, hass))["revision"]
    fake_station.apply_settings = False

    reply = await _set_table(client, hass, revision)

    assert reply["error"]["code"] == "not_applied"
    assert "camera_siren" in reply["error"]["message"]
    camera_name = dr.async_get(hass).async_get(_device_id(hass, SYNTHETIC.camera_sn)).name  # type: ignore[union-attr]
    assert camera_name in reply["error"]["message"]
    _no_serial(reply)
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.usefixtures("away_table")
async def test_a_table_two_devices_did_not_take_names_both_by_their_ha_names(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    short_readback: None,
) -> None:
    """``not_applied`` for a camera and a sensor: the library names both
    (``device_sns``), the message by their HA names, no serial."""
    add_motion_sensor(fake_station, fake_cloud)
    fake_station.params[1].update({1239: "8", 1167: "0", 1172: "0"})
    entry = await set_up_warm(hass, seed_warm_cache)
    client = await hass_ws_client(hass)
    camera_id = _device_id(hass, SYNTHETIC.camera_sn)
    sensor_id = _device_id(hass, SENSOR_SN)
    revision = (await _payload(client, hass))["revision"]
    fake_station.apply_settings = False

    reply = await _set_table(
        client,
        hass,
        revision,
        actions={camera_id: {"camera_siren": True}, sensor_id: {"motion_sensor_respond": False}},
    )

    assert reply["error"]["code"] == "not_applied"
    registry = dr.async_get(hass)
    for device_id in (camera_id, sensor_id):
        assert registry.async_get(device_id).name in reply["error"]["message"]  # type: ignore[union-attr]
    assert "a device" not in reply["error"]["message"]
    _no_serial(reply)
    assert SENSOR_SN not in json.dumps(reply)
    assert redact_serial(SENSOR_SN) not in json.dumps(reply)
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.usefixtures("away_table")
async def test_a_mode_table_write_refuses_bad_input_before_sending(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A device that is not paired to the station or an unknown mode: ``invalid_format``;
    a mode without a table (Schedule): ``not_supported``. Nothing sent."""
    entry = await set_up_warm(hass, seed_warm_cache)
    client = await hass_ws_client(hass)
    revision = (await _payload(client, hass))["revision"]
    station_id = _device_id(hass, SYNTHETIC.station_sn)

    not_paired = await _set_table(client, hass, revision, actions={station_id: {"record": True}})
    assert not_paired["error"]["code"] == "invalid_format"
    unknown_mode = await _set_table(client, hass, revision, mode="sometimes")
    assert unknown_mode["error"]["code"] == "invalid_format"
    no_table = await _set_table(client, hass, revision, mode="schedule")
    assert no_table["error"]["code"] == "not_supported"
    assert fake_station.mode_tables_received == []
    for reply in (not_paired, unknown_mode, no_table):
        _no_serial(reply)
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.usefixtures("away_table")
async def test_a_non_admin_cannot_write_a_mode_table(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    hass_read_only_access_token: str,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    entry = await set_up_warm(hass, seed_warm_cache)
    client = await hass_ws_client(hass, hass_read_only_access_token)
    revision = (await _payload(client, hass))["revision"]

    reply = await _set_table(client, hass, revision)

    assert reply["error"]["code"] == "unauthorized"
    assert fake_station.mode_tables_received == []
    assert await hass.config_entries.async_unload(entry.entry_id)


# ── mode-table flag semantics ────────────────────────────────────────────────


@pytest.mark.usefixtures("away_table")
async def test_a_motion_sensor_reacts_while_its_bit_is_clear_and_a_write_takes_logical_values(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """``motion_sensor_respond`` is on while bit 32 is clear (the library's flags); turning
    it off is a logical ``false``: the library sets the bit and clears the sensor's
    notification and HomeBase alarm in the same table."""
    add_motion_sensor(fake_station, fake_cloud)
    sensor_channel = 1
    fake_station.params[sensor_channel].update({1239: "8", 1167: "0", 1172: "0"})
    entry = await set_up_warm(hass, seed_warm_cache)
    client = await hass_ws_client(hass)
    sensor_id = _device_id(hass, SENSOR_SN)
    before = await _payload(client, hass)
    flags = before["mode_tables"]["away"]["devices"][sensor_id]["flags"]
    assert flags["motion_sensor_respond"] is True
    assert flags["notification"] is True

    reply = await _set_table(
        client,
        hass,
        before["revision"],
        actions={sensor_id: {"motion_sensor_respond": False}},
    )

    assert reply["success"], reply
    assert fake_station.params[sensor_channel][1239] == "32"
    after = reply["result"]["mode_tables"]["away"]["devices"][sensor_id]["flags"]
    assert after["motion_sensor_respond"] is False
    assert after["notification"] is False
    assert await hass.config_entries.async_unload(entry.entry_id)


# ── configuration writes, restart, alarm ─────────────────────────────────────


async def _write(
    client: MockHAClientWebSocket, hass: HomeAssistant, command: str, **fields: Any
) -> dict[str, Any]:
    """``command`` on the station with the current revision, unless ``fields`` names one."""
    fields.setdefault("expect_revision", (await _payload(client, hass))["revision"])
    return await _ws(client, command, device_id=_device_id(hass, SYNTHETIC.station_sn), **fields)


def _config_writes(fake_station: FakeStation, frame: int) -> list[dict[str, Any]]:
    return [
        json.loads(body) for ftype, body in fake_station.config_frames_received if ftype == frame
    ]


async def test_a_custom_mode_is_created_renamed_and_deleted(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Each write is the station's mode list (1256) and answers the payload with the mode
    and its select option."""
    fake_station.set_mode_list({GuardMode.CUSTOM_1: _NIGHT})
    entry = await set_up_warm(hass, seed_warm_cache)
    client = await hass_ws_client(hass)

    created = await _write(client, hass, homebase.WS_CUSTOM_MODE_CREATE, name="Garden")
    assert created["success"], created
    garden = next(m for m in created["result"]["modes"] if m["id"] == "custom_2")
    assert garden == {
        "id": "custom_2",
        "name": "Garden",
        "custom": True,
        "settable": True,
        "option": "Garden",
    }
    assert _config_writes(fake_station, 1256)[-1]["modes"][-1]["title"] == "Garden"

    renamed = await _write(
        client, hass, homebase.WS_CUSTOM_MODE_RENAME, mode="custom_1", name="Night"
    )
    assert renamed["success"], renamed
    names = {m["id"]: m["name"] for m in renamed["result"]["modes"]}
    assert names["custom_1"] == "Night"

    deleted = await _write(client, hass, homebase.WS_CUSTOM_MODE_DELETE, mode="custom_2")
    assert deleted["success"], deleted
    assert "custom_2" not in {m["id"] for m in deleted["result"]["modes"]}
    assert len(_config_writes(fake_station, 1256)) == 3
    _no_serial(deleted)
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_custom_mode_refusals_map_to_error_codes_and_send_nothing(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The current mode cannot be deleted (``mode_in_use``, the library's reason); a
    stale revision is ``config_changed``; the library's ``ValueError`` (an empty name, a
    built-in mode) is ``invalid_format`` with its text."""
    fake_station.set_mode_list({GuardMode.CUSTOM_1: _NIGHT})
    set_guard_mode(fake_station, int(GuardMode.CUSTOM_1))
    entry = await set_up_warm(hass, seed_warm_cache)
    client = await hass_ws_client(hass)
    revision = (await _payload(client, hass))["revision"]

    in_use = await _write(client, hass, homebase.WS_CUSTOM_MODE_DELETE, mode="custom_1")
    assert in_use["error"]["code"] == "mode_in_use"
    with pytest.raises(ModeInUseError) as raised:
        station = entry.runtime_data.coordinators[SYNTHETIC.station_sn].station
        await station.async_delete_custom_mode(GuardMode.CUSTOM_1)
    assert in_use["error"]["message"] == raised.value.reason

    stale = await _write(
        client, hass, homebase.WS_CUSTOM_MODE_CREATE, name="Garden", expect_revision=revision + 1
    )
    assert stale["error"]["code"] == "config_changed"

    empty = await _write(client, hass, homebase.WS_CUSTOM_MODE_CREATE, name=" ")
    assert empty["error"]["code"] == "invalid_format"
    with pytest.raises(ValueError) as invalid:
        await station.async_create_custom_mode(" ")
    assert empty["error"]["message"] == str(invalid.value)
    built_in = await _write(client, hass, homebase.WS_CUSTOM_MODE_RENAME, mode="home", name="X")
    assert built_in["error"]["code"] == "invalid_format"
    assert _config_writes(fake_station, 1256) == []
    for reply in (in_use, stale, empty, built_in):
        _no_serial(reply)
    await advance_to_poll(hass, 2)  # let a written mode's dump-settle timer fire
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_the_schedule_is_written_whole_with_its_default(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Slots by weekday, ``HH:MM`` and mode id become one 1254 write; ``default`` the mode
    outside them (1257); the answer shows both."""
    fake_station.set_mode_list({GuardMode.CUSTOM_1: _NIGHT})
    fake_station.set_schedule((), default=GuardMode.DISARMED)
    entry = await set_up_warm(hass, seed_warm_cache)
    client = await hass_ws_client(hass)
    slots = [
        {"weekday": 1, "start": "08:00", "end": "17:30", "mode": "home"},
        {"weekday": 6, "start": "22:00", "end": "24:00", "mode": "custom_1"},
    ]

    reply = await _write(client, hass, homebase.WS_SCHEDULE_SET, slots=slots, default="away")

    assert reply["success"], reply
    assert reply["result"]["schedule"] == {"slots": slots, "default": "away"}
    (written,) = _config_writes(fake_station, 1254)
    assert written["schedules"][0] == {
        "week": 1,
        "start_h": 8,
        "start_m": 0,
        "end_h": 17,
        "end_m": 30,
        "mode_id": 1,
    }
    # A slot to midnight is stored as the eufy app stores it.
    assert (written["schedules"][1]["end_h"], written["schedules"][1]["end_m"]) == (23, 59)
    assert fake_station.params[255][1257] == "0"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_a_slot_to_midnight_reads_as_24_00_and_an_unchanged_resend_sends_nothing(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The station's 23:59 end is midnight: ``24:00`` in the payload; the payload's
    slots sent back as they are write nothing and answer the same schedule."""
    fake_station.set_schedule(
        [ScheduleSlot(Weekday.TUESDAY, time(20), time(23, 59), GuardMode.AWAY)]
    )
    entry = await set_up_warm(hass, seed_warm_cache)
    client = await hass_ws_client(hass)

    schedule = (await _payload(client, hass))["schedule"]
    assert schedule["slots"] == [{"weekday": 2, "start": "20:00", "end": "24:00", "mode": "away"}]

    reply = await _write(client, hass, homebase.WS_SCHEDULE_SET, slots=schedule["slots"])

    assert reply["success"], reply
    assert reply["result"]["schedule"] == schedule
    assert _config_writes(fake_station, 1254) == []
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    ("slots", "match"),
    [
        (
            [
                {"weekday": 1, "start": "08:00", "end": "12:00", "mode": "home"},
                {"weekday": 1, "start": "11:00", "end": "13:00", "mode": "away"},
            ],
            "overlap",
        ),
        ([{"weekday": 1, "start": "12:00", "end": "08:00", "mode": "home"}], "start before"),
        ([{"weekday": 7, "start": "08:00", "end": "09:00", "mode": "home"}], "weekday"),
        ([{"weekday": 1, "start": "8:00", "end": "09:00", "mode": "home"}], "HH:MM"),
        ([{"weekday": 1, "start": "24:00", "end": "24:00", "mode": "home"}], "HH:MM"),
        ([{"weekday": 1, "start": "08:60", "end": "09:00", "mode": "home"}], "HH:MM"),
        ([{"weekday": 1, "start": "08:00", "end": "09:00", "mode": "sometimes"}], "sometimes"),
    ],
    ids=["overlap", "end-before-start", "weekday", "clock", "start-24", "minute-60", "mode"],
)
async def test_a_schedule_the_codec_refuses_is_invalid_format_with_its_text(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    slots: list[dict[str, Any]],
    match: str,
) -> None:
    entry = await set_up_warm(hass, seed_warm_cache)
    client = await hass_ws_client(hass)

    reply = await _write(client, hass, homebase.WS_SCHEDULE_SET, slots=slots)

    assert reply["error"]["code"] == "invalid_format"
    assert match in reply["error"]["message"]
    assert fake_station.config_frames_received == []
    assert await hass.config_entries.async_unload(entry.entry_id)


# A second camera of the fixture station, on its own channel.
_PORCH_SN: Final = "T8160P0000000002"
_PORCH_CHANNEL: Final = 2


def _add_camera(fake_station: FakeStation, fake_cloud: FakeCloud) -> None:
    """Pair a second camera (``_PORCH_SN``) to the fake station; before ``set_up_warm``."""
    fake_cloud.devices.append(
        camera_device(
            _PORCH_SN, station_sn=fake_station.serial, channel=_PORCH_CHANNEL, name="Porch"
        )
    )
    fake_station.params[_PORCH_CHANNEL] = {1101: "90"}


async def test_paired_devices_are_listed_by_name_whatever_the_cloud_order(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The payload's devices are sorted by name, so the card's drawing keeps its places
    when a device-list refresh reorders the cloud's list."""
    _add_camera(fake_station, fake_cloud)
    fake_cloud.devices.insert(0, fake_cloud.devices.pop())  # the cloud lists Porch first
    entry = await set_up_warm(hass, seed_warm_cache)
    client = await hass_ws_client(hass)

    names = [d["name"] for d in (await _payload(client, hass))["devices"]]

    assert len(names) >= 2
    assert names == sorted(names, key=str.casefold)
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_cross_camera_cameras_and_groups_are_written_by_device_id(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """``cameras: null`` chooses every camera (1072), ``groups`` replaces the groups
    (1073); the answer lists them by HA device id with the tracking time in seconds."""
    _add_camera(fake_station, fake_cloud)
    fake_station.reply_to_settings = True
    fake_station.params[255][1070] = "180"  # the wire value: seconds
    entry = await set_up_warm(hass, seed_warm_cache)
    client = await hass_ws_client(hass)
    camera_id = _device_id(hass, SYNTHETIC.camera_sn)
    porch_id = _device_id(hass, _PORCH_SN)

    reply = await _write(
        client, hass, homebase.WS_CROSS_CAMERA_SET, cameras=None, groups=[[camera_id]]
    )

    assert reply["success"], reply
    writes = [(c["cmd"], c["payload"]) for c in fake_station.received if c["cmd"] in (1072, 1073)]
    assert writes == [
        (1072, [SYNTHETIC.camera_sn, _PORCH_SN]),
        (1073, [{"value": [SYNTHETIC.camera_sn]}]),
    ]
    cross = reply["result"]["cross_camera"]
    assert cross["cameras"] == [camera_id, porch_id]
    assert cross["groups"] == [[camera_id]]
    assert (cross["continuous_time"], cross["continuous_time_s"]) == (1, 180)
    _no_serial(reply)
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_cross_camera_offers_the_eligible_devices_by_id_and_the_minimum(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """``eligible``: the library's eligible devices by HA device id in its order, a
    sensor not among them, a device HA does not know left out; ``min_cameras``: the
    library's minimum."""
    add_motion_sensor(fake_station, fake_cloud)
    _add_camera(fake_station, fake_cloud)
    entry = await set_up_warm(hass, seed_warm_cache)
    client = await hass_ws_client(hass)
    camera_id = _device_id(hass, SYNTHETIC.camera_sn)
    porch_id = _device_id(hass, _PORCH_SN)

    cross = (await _payload(client, hass))["cross_camera"]

    assert cross["eligible"] == [camera_id, porch_id]
    assert cross["min_cameras"] == MIN_TRACKING_CAMERAS == 2
    dr.async_get(hass).async_remove_device(porch_id)
    assert (await _payload(client, hass))["cross_camera"]["eligible"] == [camera_id]
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_a_tracking_choice_below_the_minimum_is_refused_with_the_librarys_text(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """One camera chosen: ``invalid_format`` with the library's text; every camera of a
    station with one: ``not_supported``, the station named by its HA name. Nothing sent."""
    entry = await set_up_warm(hass, seed_warm_cache)
    client = await hass_ws_client(hass)
    station = entry.runtime_data.coordinators[SYNTHETIC.station_sn].station
    camera_id = _device_id(hass, SYNTHETIC.camera_sn)
    station_name = dr.async_get(hass).async_get(_device_id(hass, SYNTHETIC.station_sn)).name  # type: ignore[union-attr]

    one = await _write(client, hass, homebase.WS_CROSS_CAMERA_SET, cameras=[camera_id])
    every = await _write(client, hass, homebase.WS_CROSS_CAMERA_SET, cameras=None)

    assert one["error"]["code"] == "invalid_format"
    with pytest.raises(ValueError) as refused:
        await station.async_set_cross_camera_cameras([SYNTHETIC.camera_sn])
    assert one["error"]["message"] == str(refused.value)
    assert every["error"] == {
        "code": "not_supported",
        "message": f"{station_name} has fewer than 2 cameras cross-camera tracking can follow",
    }
    assert [c for c in fake_station.received if c["cmd"] == 1072] == []
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_cross_camera_refuses_an_empty_request_and_a_foreign_device(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """No key or a device not paired: ``invalid_format``; a paired sensor: the library's
    ``not_supported``, the sensor and the station by their HA names. Nothing sent."""
    add_motion_sensor(fake_station, fake_cloud)
    entry = await set_up_warm(hass, seed_warm_cache)
    client = await hass_ws_client(hass)
    station_id = _device_id(hass, SYNTHETIC.station_sn)
    sensor_id = _device_id(hass, SENSOR_SN)
    camera_id = _device_id(hass, SYNTHETIC.camera_sn)
    registry = dr.async_get(hass)

    empty = await _write(client, hass, homebase.WS_CROSS_CAMERA_SET)
    foreign = await _write(client, hass, homebase.WS_CROSS_CAMERA_SET, cameras=[station_id])
    sensor = await _write(
        client, hass, homebase.WS_CROSS_CAMERA_SET, cameras=[sensor_id, camera_id]
    )

    assert empty["error"]["code"] == "invalid_format"
    assert foreign["error"]["code"] == "invalid_format"
    assert sensor["error"] == {
        "code": "not_supported",
        "message": f"{registry.async_get(sensor_id).name} is not a device paired to "  # type: ignore[union-attr]
        f"{registry.async_get(station_id).name} that cross-camera tracking can follow",  # type: ignore[union-attr]
    }
    assert [c for c in fake_station.received if c["cmd"] in (1072, 1073)] == []
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_restart_sends_the_stations_restart_and_answers_the_payload(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    entry = await set_up_warm(hass, seed_warm_cache)
    client = await hass_ws_client(hass)
    station_id = _device_id(hass, SYNTHETIC.station_sn)

    result = await _ok(client, homebase.WS_RESTART, device_id=station_id)

    assert result["device_id"] == station_id
    assert (1034, 255, 0) in fake_station.ecb_received
    assert await hass.config_entries.async_unload(entry.entry_id)


def _tone_files(fake_station: FakeStation) -> list[int]:
    return [c["payload"]["time_out"] for c in fake_station.received if c["cmd"] == 1201]


async def test_the_alarm_sounds_and_stops(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """``sound`` without seconds is the library's default (30 s); ``stop`` ends it. The
    station's tone frame confirms each, and the payload follows the station."""
    fake_station.tone_frames = True
    entry = await set_up_warm(hass, seed_warm_cache)
    client = await hass_ws_client(hass)
    station_id = _device_id(hass, SYNTHETIC.station_sn)
    station = entry.runtime_data.coordinators[SYNTHETIC.station_sn].station

    await _ok(client, homebase.WS_ALARM, device_id=station_id, action="sound")
    await wait_until(lambda: station.alarming)
    assert (await _payload(client, hass))["alarm"]["triggered"] is True
    await _ok(client, homebase.WS_ALARM, device_id=station_id, action="sound", seconds=30)
    stopped = await _ok(client, homebase.WS_ALARM, device_id=station_id, action="stop")

    assert _tone_files(fake_station) == [30, 30, 0]
    await wait_until(lambda: not station.alarming)
    assert stopped["device_id"] == station_id
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_alarm_seconds_the_library_refuses_are_invalid_format(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    entry = await set_up_warm(hass, seed_warm_cache)
    client = await hass_ws_client(hass)

    reply = await _ws(
        client,
        homebase.WS_ALARM,
        device_id=_device_id(hass, SYNTHETIC.station_sn),
        action="sound",
        seconds=60,
    )

    assert reply["error"]["code"] == "invalid_format"
    assert _tone_files(fake_station) == []
    assert await hass.config_entries.async_unload(entry.entry_id)


_WRITES: Final = [
    (homebase.WS_CUSTOM_MODE_CREATE, {"name": "Garden", "expect_revision": 1}),
    (homebase.WS_CUSTOM_MODE_RENAME, {"mode": "custom_1", "name": "X", "expect_revision": 1}),
    (homebase.WS_CUSTOM_MODE_DELETE, {"mode": "custom_1", "expect_revision": 1}),
    (homebase.WS_SCHEDULE_SET, {"slots": [], "expect_revision": 1}),
    (homebase.WS_CROSS_CAMERA_SET, {"cameras": None, "expect_revision": 1}),
    (homebase.WS_RESTART, {}),
    (homebase.WS_ALARM, {"action": "sound"}),
]
_WRITE_IDS: Final = ["create", "rename", "delete", "schedule", "cross_camera", "restart", "alarm"]


@pytest.mark.parametrize(
    ("command", "fields"),
    [
        (homebase.WS_CUSTOM_MODE_CREATE, {"name": "Garden"}),
        (homebase.WS_CUSTOM_MODE_RENAME, {"mode": "custom_1", "name": "X"}),
        (homebase.WS_CUSTOM_MODE_DELETE, {"mode": "custom_1"}),
        (
            homebase.WS_SCHEDULE_SET,
            {"slots": [{"weekday": 1, "start": "08:00", "end": "09:00", "mode": "home"}]},
        ),
        (homebase.WS_CROSS_CAMERA_SET, {"cameras": None}),
    ],
    ids=["create", "rename", "delete", "schedule", "cross_camera"],
)
async def test_a_stale_revision_refuses_a_configuration_write_unsent(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    command: str,
    fields: dict[str, Any],
) -> None:
    """``config_changed`` and nothing sent when ``expect_revision`` is not the station's."""
    _add_camera(fake_station, fake_cloud)  # every camera is a choice of two
    fake_station.set_mode_list({GuardMode.CUSTOM_1: _NIGHT})
    fake_station.reply_to_settings = True
    entry = await set_up_warm(hass, seed_warm_cache)
    client = await hass_ws_client(hass)
    revision = (await _payload(client, hass))["revision"]

    reply = await _write(client, hass, command, expect_revision=revision + 1, **fields)

    assert reply["error"]["code"] == "config_changed"
    assert fake_station.config_frames_received == []
    assert [c for c in fake_station.received if c["cmd"] in (1072, 1073)] == []
    _no_serial(reply)
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(("command", "fields"), _WRITES, ids=_WRITE_IDS)
async def test_a_non_admin_cannot_write_the_configuration_restart_or_sound(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    hass_read_only_access_token: str,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    command: str,
    fields: dict[str, Any],
) -> None:
    fake_station.set_mode_list({GuardMode.CUSTOM_1: _NIGHT})
    entry = await set_up_warm(hass, seed_warm_cache)
    client = await hass_ws_client(hass, hass_read_only_access_token)

    reply = await _ws(client, command, device_id=_device_id(hass, SYNTHETIC.station_sn), **fields)

    assert reply["error"]["code"] == "unauthorized"
    assert fake_station.config_frames_received == []
    assert fake_station.ecb_received == []
    assert [c for c in fake_station.received if c["cmd"] in (1072, 1201)] == []
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(("command", "fields"), _WRITES, ids=_WRITE_IDS)
async def test_a_write_to_an_unknown_device_or_a_paired_one_is_not_found(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    command: str,
    fields: dict[str, Any],
) -> None:
    entry = await set_up_warm(hass, seed_warm_cache)
    client = await hass_ws_client(hass)

    for device_id in ("nope", _device_id(hass, SYNTHETIC.camera_sn)):
        reply = await _ws(client, command, device_id=device_id, **fields)
        assert reply["error"]["code"] == "not_found"
    assert fake_station.ecb_received == []
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_an_error_names_its_device_by_the_ha_name(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every device of ``device_sns`` reads as its HA name, plain or redacted, even one the
    station no longer lists; another redacted serial as ``a device``; a refused command
    is ``home_assistant_error``."""
    entry = await set_up_warm(hass, seed_warm_cache)
    client = await hass_ws_client(hass)
    gone_sn = "T8160P0000009999"
    registry = dr.async_get(hass)
    registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, gone_sn)}, name="Garage"
    )
    camera = registry.async_get(_device_id(hass, SYNTHETIC.camera_sn))
    assert camera is not None
    station = entry.runtime_data.coordinators[SYNTHETIC.station_sn].station
    camera_sn = SYNTHETIC.camera_sn
    stranger = redact_serial("T8160P0000008888")

    async def refused(*_args: Any, **_kwargs: Any) -> None:
        raise EufySecurityError(
            f"{redact_serial(gone_sn)} and {redact_serial(camera_sn)} refused "
            f"({gone_sn}, {camera_sn}, {stranger})",
            device_sns=(gone_sn, camera_sn),
        )

    monkeypatch.setattr(station, "async_restart", refused)
    reply = await _ws(client, homebase.WS_RESTART, device_id=_device_id(hass, SYNTHETIC.station_sn))

    assert reply["error"] == {
        "code": "home_assistant_error",
        "message": f"Garage and {camera.name} refused (Garage, {camera.name}, a device)",
    }
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_a_rejected_restart_is_home_assistant_error_without_serial(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The station's own rejection (another account's command): the library's message,
    the station named by its HA name."""
    entry = await set_up_warm(hass, seed_warm_cache)
    client = await hass_ws_client(hass)
    station_id = _device_id(hass, SYNTHETIC.station_sn)
    fake_station.account_id = "another-account"

    reply = await _ws(client, homebase.WS_RESTART, device_id=station_id)

    assert reply["error"]["code"] == "home_assistant_error"
    _no_serial(reply)
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_a_config_change_rebuilds_the_coordinators_configuration(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """``StationConfigChanged`` makes the coordinator build the configuration again, even
    with its state unchanged; without one it is built once."""
    entry = await set_up_warm(hass, seed_warm_cache)
    coordinator = entry.runtime_data.coordinators[SYNTHETIC.station_sn]
    first = coordinator.station_config
    assert first is not None
    assert coordinator.station_config is first

    entry.runtime_data.router.handle(
        StationConfigChanged(
            station_sn=SYNTHETIC.station_sn, revision=first.revision, fields=frozenset({"modes"})
        )
    )

    assert coordinator.station_config is not first
    assert await hass.config_entries.async_unload(entry.entry_id)


# ── arming groups ────────────────────────────────────────────────────────────


async def test_the_group_list_maps_members_to_devices_and_offers_the_station(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Each member by HA device id, name and its station's mode name; an unknown member
    keeps no device id and the app's title; the station is a candidate with its settable
    modes by name."""
    unknown_sn = "T8030P2000099999"
    group = _group()
    group["devices"].append(_member(unknown_sn, 1, "Home"))
    _house(fake_cloud, group)
    entry = await set_up_warm(hass, seed_warm_cache)
    client = await hass_ws_client(hass)
    station_id = _device_id(hass, SYNTHETIC.station_sn)

    result = await _ok(client, homebase.WS_GROUPS_LIST, device_id=station_id)

    (listed,) = result["groups"]
    assert listed["group_id"] == _GROUP
    assert listed["name"] == "Night"
    assert listed["active"] is False
    assert listed["preset"] == "leaving"
    assert listed["members"][0] == {
        "device_id": station_id,
        "name": dr.async_get(hass).async_get(station_id).name,  # type: ignore[union-attr]
        "mode": "away",
        "label": "Away",
    }
    assert listed["members"][1] == {
        "device_id": None,
        "name": None,
        "mode": "home",
        "label": "Home",
    }
    (candidate,) = result["candidates"]
    assert candidate["device_id"] == station_id
    assert candidate["modes"] == [
        {"id": "home", "label": "Home"},
        {"id": "away", "label": "Away"},
        {"id": "disarmed", "label": "Disarmed"},
    ]
    text = json.dumps(result)
    assert unknown_sn not in text and redact_serial(unknown_sn) not in text
    _no_serial(result)
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_create_rename_and_delete_answer_the_new_listing(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """An admin creates a group of HA device ids, renames it and deletes it; each answer
    is the listing after the change."""
    _house(fake_cloud)
    entry = await set_up_warm(hass, seed_warm_cache)
    client = await hass_ws_client(hass)
    station_id = _device_id(hass, SYNTHETIC.station_sn)

    created = await _ok(
        client,
        homebase.WS_GROUPS_CREATE,
        device_id=station_id,
        name="Out",
        members=[{"device_id": station_id, "mode": "home"}],
    )
    (group,) = created["groups"]
    assert (group["name"], group["members"][0]["mode"]) == ("Out", "home")
    assert fake_cloud.arming_groups[_HOUSE][0]["devices"][0]["device_sn"] == SYNTHETIC.station_sn

    renamed = await _ok(
        client,
        homebase.WS_GROUPS_UPDATE,
        device_id=station_id,
        group_id=group["group_id"],
        name="Away all",
        members=[{"device_id": station_id, "mode": "away"}],
    )
    assert renamed["groups"][0]["name"] == "Away all"
    assert renamed["groups"][0]["members"][0]["mode"] == "away"

    deleted = await _ok(
        client, homebase.WS_GROUPS_DELETE, device_id=station_id, group_id=group["group_id"]
    )
    assert deleted["groups"] == []
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    ("command", "fields"),
    [
        (homebase.WS_GROUPS_CREATE, {"name": "Out", "members": []}),
        (homebase.WS_GROUPS_UPDATE, {"group_id": _GROUP, "name": "Out"}),
        (homebase.WS_GROUPS_DELETE, {"group_id": _GROUP}),
        (homebase.WS_GROUPS_ACTIVATE, {"group_id": _GROUP}),
    ],
    ids=["create", "update", "delete", "activate"],
)
async def test_a_non_admin_cannot_change_or_activate_a_group(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    hass_read_only_access_token: str,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    command: str,
    fields: dict[str, Any],
) -> None:
    """Writes need an admin: ``unauthorized``, and nothing reaches the cloud."""
    _house(fake_cloud, _group())
    entry = await set_up_warm(hass, seed_warm_cache)
    client = await hass_ws_client(hass, hass_read_only_access_token)
    station_id = _device_id(hass, SYNTHETIC.station_sn)

    reply = await _ws(client, command, device_id=station_id, **fields)

    assert reply["error"]["code"] == "unauthorized"
    assert fake_cloud.arming_requests == []
    listed = await _ok(client, homebase.WS_GROUPS_LIST, device_id=station_id)
    assert [g["group_id"] for g in listed["groups"]] == [_GROUP]
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_library_errors_map_to_websocket_error_codes(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """``ValueError`` -> ``invalid_format`` with the library's message, ``UnsupportedError``
    -> ``not_supported``, another library error -> ``home_assistant_error``; a member that
    is no station of the account -> ``invalid_format`` before any request."""
    _house(fake_cloud, _group())
    entry = await set_up_warm(hass, seed_warm_cache)
    client = await hass_ws_client(hass)
    station_id = _device_id(hass, SYNTHETIC.station_sn)
    camera_id = _device_id(hass, SYNTHETIC.camera_sn)

    bad_name = await _ws(
        client,
        homebase.WS_GROUPS_CREATE,
        device_id=station_id,
        name="a/b",
        members=[{"device_id": station_id, "mode": "away"}],
    )
    assert bad_name["error"]["code"] == "invalid_format"
    assert "special characters" in bad_name["error"]["message"]

    paired = await _ws(
        client,
        homebase.WS_GROUPS_CREATE,
        device_id=station_id,
        name="Out",
        members=[{"device_id": camera_id, "mode": "away"}],
    )
    assert paired["error"]["code"] == "invalid_format"

    unknown = await _ws(client, homebase.WS_GROUPS_DELETE, device_id=station_id, group_id="nope")
    assert unknown["error"]["code"] == "not_supported"

    fake_cloud.arming_errors["guard_list"] = CloudApiError(1, "refused", endpoint="guard_list")
    failed = await _ws(client, homebase.WS_GROUPS_LIST, device_id=station_id)
    assert failed["error"]["code"] == "home_assistant_error"

    not_station = await _ws(client, homebase.WS_GROUPS_LIST, device_id=camera_id)
    assert not_station["error"]["code"] == "not_found"
    for reply in (bad_name, paired, unknown, failed):
        _no_serial(reply)
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    ("target", "outcome", "guard_mode"),
    [(0, "already", 0), (1, "changed", 1)],
    ids=["away", "home"],
)
async def test_activation_answers_each_members_outcome_and_is_logged(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    target: int,
    outcome: str,
    guard_mode: int,
) -> None:
    """A member already in its mode is ``already`` (nothing sent); another is ``changed``
    and the station takes the mode. The cloud logs the activation, answered with an ISO time."""
    _house(fake_cloud, _group(mode=target, title="Away" if target == 0 else "Home"))
    entry = await set_up_warm(hass, seed_warm_cache)
    client = await hass_ws_client(hass)
    station_id = _device_id(hass, SYNTHETIC.station_sn)

    result = await _ok(client, homebase.WS_GROUPS_ACTIVATE, device_id=station_id, group_id=_GROUP)

    (member,) = result["results"]
    assert member["device_id"] == station_id
    assert member["mode"] == ("away" if target == 0 else "home")
    assert member["outcome"] == outcome
    assert member["error"] is None
    assert fake_station.guard_mode == guard_mode
    logs = await _ok(client, homebase.WS_GROUPS_LOGS, device_id=station_id, group_id=_GROUP)
    (log,) = logs["logs"]
    assert log["group_name"] == "Night"
    assert log["time"].endswith("+00:00")
    await advance_to_poll(hass, 2)  # let a written mode's dump-settle timer fire
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_a_member_this_account_does_not_serve_fails_and_is_named_without_its_serial(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A member with no local station is ``failed`` with the library's reason, its serial
    (redacted or not) replaced; the station's own member still gets its mode."""
    unknown_sn = "T8030P2000099999"
    group = _group(mode=1, title="Home")
    group["devices"].append(_member(unknown_sn, 0, "Away"))
    _house(fake_cloud, group)
    entry = await set_up_warm(hass, seed_warm_cache)
    client = await hass_ws_client(hass)
    station_id = _device_id(hass, SYNTHETIC.station_sn)

    result = await _ok(client, homebase.WS_GROUPS_ACTIVATE, device_id=station_id, group_id=_GROUP)

    own, other = result["results"]
    assert (own["device_id"], own["outcome"]) == (station_id, "changed")
    assert (other["device_id"], other["name"], other["outcome"]) == (None, None, "failed")
    assert other["error"] == "a device has no local station in this client"
    text = json.dumps(result)
    assert unknown_sn not in text and redact_serial(unknown_sn) not in text
    await advance_to_poll(hass, 2)  # let a written mode's dump-settle timer fire
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_a_failed_member_ha_knows_is_named_by_its_ha_name(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A member's error names the device the library error names (``device_sns``) by
    its HA name."""
    unknown_sn = "T8030P2000099999"
    group = _group(mode=1, title="Home")
    group["devices"].append(_member(unknown_sn, 0, "Away"))
    _house(fake_cloud, group)
    entry = await set_up_warm(hass, seed_warm_cache)
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, unknown_sn)}, name="Cabin"
    )
    client = await hass_ws_client(hass)
    station_id = _device_id(hass, SYNTHETIC.station_sn)

    result = await _ok(client, homebase.WS_GROUPS_ACTIVATE, device_id=station_id, group_id=_GROUP)

    other = result["results"][1]
    assert (other["name"], other["outcome"]) == ("Cabin", "failed")
    assert other["error"] == "Cabin has no local station in this client"
    await advance_to_poll(hass, 2)  # let a written mode's dump-settle timer fire
    assert await hass.config_entries.async_unload(entry.entry_id)


# ── security mode select ─────────────────────────────────────────────────────


def _security_mode(hass: HomeAssistant) -> str:
    return entity_id_for(hass, SELECT_DOMAIN, SYNTHETIC.station_sn, SECURITY_MODE_KEY)


async def _select(hass: HomeAssistant, entity_id: str, option: str) -> None:
    await hass.services.async_call(
        SELECT_DOMAIN,
        SERVICE_SELECT_OPTION,
        {ATTR_ENTITY_ID: entity_id, ATTR_OPTION: option},
        blocking=True,
    )


async def test_the_security_mode_select_sets_a_mode_by_name_and_follows_the_panel(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Options are the settable modes' names, a custom title included; a pick writes the
    mode's code; a mode set through the alarm panel shows here too."""
    fake_station.set_mode_list({GuardMode.CUSTOM_1: _NIGHT})
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = _security_mode(hass)
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "Away"
    assert state.attributes[ATTR_OPTIONS] == ["Home", "Away", _NIGHT, "Disarmed"]

    await _select(hass, entity_id, _NIGHT)
    assert fake_station.guard_mode == 3
    assert state_of(hass, entity_id) == _NIGHT

    await hass.services.async_call(
        ALARM_DOMAIN, "alarm_arm_away", {ATTR_ENTITY_ID: panel_entity_id(hass)}, blocking=True
    )
    assert state_of(hass, entity_id) == "Away"
    await advance_to_poll(hass, 2)  # let a written mode's dump-settle timer fire
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_a_reported_mode_that_cannot_be_set_is_shown_but_refused_on_write(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Off in force is an extra option by its name; picking it raises a translated
    validation error and sends nothing."""
    set_guard_mode(fake_station, int(GuardMode.OFF))
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = _security_mode(hass)
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "Off"
    assert state.attributes[ATTR_OPTIONS] == ["Home", "Away", "Disarmed", "Off"]
    with pytest.raises(ServiceValidationError) as raised:
        await _select(hass, entity_id, "Off")

    assert raised.value.translation_key == "security_mode_not_settable"
    assert fake_station.guard_mode == int(GuardMode.OFF)
    assert state_of(hass, entity_id) == "Off"
    assert await hass.config_entries.async_unload(entry.entry_id)


def _station_model(fake_cloud: FakeCloud, fake_station: FakeStation, model: str) -> None:
    """Give the fake station's device-list entry the product code ``model``."""
    station = next(d for d in fake_cloud.devices if d.get("device_sn") == fake_station.serial)
    station["device_new_pn"] = model


async def test_a_station_model_whose_guard_mode_the_library_refuses_is_reported_unsent(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The library refuses the guard mode of a model without app data before sending:
    the pick raises the translated ``guard_mode_not_supported`` once, nothing is sent
    and the select keeps the mode."""
    _station_model(fake_cloud, fake_station, "T8021")
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = _security_mode(hass)
    states = record_states(hass, entity_id)

    with pytest.raises(HomeAssistantError) as raised:
        await _select(hass, entity_id, "Home")
    await advance_to_poll(hass, POLL_INTERVAL_SECONDS + 1)

    assert raised.value.translation_key == "guard_mode_not_supported"
    station = entry.runtime_data.coordinators[fake_station.serial].station
    assert raised.value.translation_placeholders == {"target": "home", "station": station.name}
    assert [c for c in fake_station.received if c.get("cmd") == 1224] == []
    assert state_of(hass, entity_id) == "Away"
    assert states == []
    assert await hass.config_entries.async_unload(entry.entry_id)


def test_a_mode_name_another_mode_has_gets_its_label() -> None:
    """Two modes of one name stay two options."""

    class _Config:
        settable_modes = (GuardMode.HOME, GuardMode.CUSTOM_1)

        def mode_name(self, code: GuardMode | int) -> str:
            return "Home"

    choices = select.mode_choices(_Config(), GuardMode.HOME)  # type: ignore[arg-type]
    assert choices == {"Home": GuardMode.HOME, "Home (Custom 1)": GuardMode.CUSTOM_1}


# ── stale setting rows (a library release drops or moves settings) ───────────


# A paired device of a model the library has no settings list for.
_UNLISTED_SN: Final = "T9999P0000000001"


async def test_rows_of_settings_the_library_moved_or_dropped_are_forgotten(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    fake_cloud: FakeCloud,
    seed_warm_cache: Callable[..., None],
) -> None:
    """A setting turned read-only loses its switch row and gets a binary sensor; a
    ``toggles`` member and a whole setting the library no longer lists for the device
    (``has_setting`` False) lose their rows, a duration setting's too. A setting row of
    a device whose settings the library does not know (``has_setting`` None) stays, as
    do the integration's own rows (named by device class or translation key)."""
    fake_cloud.devices.append(
        camera_device(_UNLISTED_SN, station_sn=fake_station.serial, channel=3, name="Odd")
    )
    seed_warm_cache()
    entry = add_entry(hass)
    registry = er.async_get(hass)
    station_sn = SYNTHETIC.station_sn
    keypad = registry.async_get_or_create(
        Platform.SWITCH,
        DOMAIN,
        f"{station_sn}_hb_keypad_is_password_set",
        config_entry=entry,
        original_name="Keypad password set",
        entity_category=EntityCategory.CONFIG,
    )
    member = registry.async_get_or_create(
        Platform.SWITCH,
        DOMAIN,
        f"{station_sn}_switching_notification_9",
        config_entry=entry,
        # No name: the member rule alone recognises it.
        entity_category=EntityCategory.CONFIG,
    )
    dropped = registry.async_get_or_create(
        Platform.SELECT,
        DOMAIN,
        f"{station_sn}_set_alarm_type",
        config_entry=entry,
        original_name="Alarm type",
        entity_category=EntityCategory.CONFIG,
    )
    dropped_duration = registry.async_get_or_create(
        Platform.NUMBER,
        DOMAIN,
        f"{station_sn}_old_entry_delay",
        config_entry=entry,
        original_name="Entry delay",
        original_device_class="duration",
        entity_category=EntityCategory.CONFIG,
    )
    unknown_device = registry.async_get_or_create(
        Platform.SELECT,
        DOMAIN,
        f"{_UNLISTED_SN}_set_alarm_type",
        config_entry=entry,
        original_name="Alarm type",
        entity_category=EntityCategory.CONFIG,
    )
    own_device_class = registry.async_get_or_create(
        Platform.SENSOR,
        DOMAIN,
        f"{SYNTHETIC.camera_sn}_old_battery",
        config_entry=entry,
        original_name="Battery",
        original_device_class="battery",
        entity_category=EntityCategory.DIAGNOSTIC,
    )
    own_translated = registry.async_get_or_create(
        Platform.SENSOR,
        DOMAIN,
        f"{station_sn}_old_figure",
        config_entry=entry,
        original_name="Figure",
        translation_key="old_figure",
        entity_category=EntityCategory.DIAGNOSTIC,
    )

    assert await setup_entry(hass, entry)

    station = entry.runtime_data.coordinators[station_sn].station
    assert station.has_setting("set_alarm_type") is False
    assert station.has_setting("set_alarm_type", _UNLISTED_SN) is None
    for gone in (keypad, member, dropped, dropped_duration):
        assert registry.async_get(gone.entity_id) is None, gone.unique_id
    for kept in (own_device_class, own_translated, unknown_device):
        assert registry.async_get(kept.entity_id) is not None, kept.unique_id
    sensor = registry.async_get_entity_id(
        Platform.BINARY_SENSOR, DOMAIN, f"{station_sn}_hb_keypad_is_password_set"
    )
    assert sensor is not None
    assert registry.async_get_entity_id(
        Platform.SWITCH, DOMAIN, f"{station_sn}_switching_notification_0"
    )
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_only_setting_entities_carry_a_name_of_their_own(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The stale-row rule's premise: every row ``is_setting_row`` matches (a literal name,
    no translation key, no device class or a setting's) is a setting entity this setup
    builds."""
    entry = await set_up_warm(hass, seed_warm_cache)
    registry = er.async_get(hass)
    built = {
        f"{spec.device_sn or station.serial}_{spec.key}"
        for coordinator in entry.runtime_data.coordinators.values()
        for station in (coordinator.station,)
        for spec in setting_specs(station)
    }
    setting_domains = {"switch", "select", "number", "text", "sensor", "binary_sensor"}
    named = [
        row.unique_id
        for row in er.async_entries_for_config_entry(registry, entry.entry_id)
        if row.domain in setting_domains and is_setting_row(row)
    ]
    assert named
    assert set(named) <= built
    assert await hass.config_entries.async_unload(entry.entry_id)
