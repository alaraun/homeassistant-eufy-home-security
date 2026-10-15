"""The settings layout, HomeBase, mode-table and arming-group websocket commands, the
security-mode select and the per-mode entities' availability, on the real library with its
FakeCloud and FakeStation."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Callable
from typing import Any, Final

import pytest
from conftest import (
    add_entry,
    advance_to_poll,
    alarm_changed,
    define_custom_modes,
    entity_id_for,
    panel_entity_id,
    seed_setting,
    set_guard_mode,
    set_up_warm,
    setup_entry,
    state_of,
)
from eufy_home_security import (
    SETTING_SECTIONS,
    CloudApiError,
    EufySecurity,
    GuardMode,
    ModeInUseError,
    StationConfigChanged,
    StationFeature,
    redact_serial,
    setting_section,
)
from eufy_home_security.devices import mode_action_label
from eufy_home_security.testing import SYNTHETIC, FakeCloud, FakeStation, camera_device
from homeassistant.components.alarm_control_panel import DOMAIN as ALARM_DOMAIN
from homeassistant.components.number import DOMAIN as NUMBER_DOMAIN
from homeassistant.components.select import ATTR_OPTION, ATTR_OPTIONS, SERVICE_SELECT_OPTION
from homeassistant.components.select import DOMAIN as SELECT_DOMAIN
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE, EntityCategory, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
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
from custom_components.eufy_home_security.settings import setting_specs

_HOUSE: Final = "house-1"
_GROUP: Final = "group-1"
_CAMERA_ACTION_AWAY: Final = "camera_action_away"
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
    order, disabled ones (per-mode bits) included, entities without a section left out."""
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
    assert "modes" in ids
    for section in sections:
        info = setting_section(section["id"])
        assert (section["heading"], section["order"]) == (info.heading, info.order)
        assert section["entity_ids"]
    registry = er.async_get(hass)
    by_section: dict[str, list[str | None]] = {}
    for spec in setting_specs(station):
        if spec.device_sn == SYNTHETIC.camera_sn:
            by_section.setdefault(spec.setting.section, []).append(
                registry.async_get_entity_id(
                    spec.platform, DOMAIN, f"{SYNTHETIC.camera_sn}_{spec.key}"
                )
            )
    assert {s["id"]: s["entity_ids"] for s in sections} == by_section
    listed = [entity_id for section in sections for entity_id in section["entity_ids"]]
    mode_bit = entity_id_for(hass, SWITCH_DOMAIN, SYNTHETIC.camera_sn, "camera_action_away_record")
    assert registry.async_get(mode_bit).disabled_by is er.RegistryEntryDisabler.INTEGRATION  # type: ignore[union-attr]
    assert mode_bit in next(s for s in sections if s["id"] == "modes")["entity_ids"]
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
    """The station's layout holds the station's settings (the alarm volume in ``alarm``)."""
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
    define_custom_modes(fake_station, {3: _NIGHT})
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
        {"id": "home", "name": "Home", "custom": False, "settable": True},
        {"id": "away", "name": "Away", "custom": False, "settable": True},
        {"id": "custom_1", "name": _NIGHT, "custom": True, "settable": True},
        {"id": "disarmed", "name": "Disarmed", "custom": False, "settable": True},
    ]
    support = result["support"]
    assert set(support) == {feature.value for feature in StationFeature}
    for feature in StationFeature:
        evidence = station.config_support(feature)
        assert support[feature.value]["grade"] == evidence.support.value
        assert support[feature.value]["note"] == (evidence.note or None)
        assert support[feature.value]["alarm_affecting"] is feature.alarm_affecting
    available = {k for k, v in support.items() if v["available"]}
    assert {"guard_mode", "mode_tables", "mode_delays", "arming_groups"} <= available
    assert not available & {"custom_modes", "schedule", "cross_camera", "restart", "sound_alarm"}
    # Written by its setting entity; the HomeBase 3 has the setting.
    assert "working_mode" in config.setting_keys
    assert support["working_mode"]["available"] is True
    assert result["flags"]["camera"][0] == {"id": "record", "label": mode_action_label("record")}
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


async def test_the_payload_shows_a_sounding_alarm(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """``alarm.triggered`` follows the alarm panel's ``triggered``."""
    entry = await set_up_warm(hass, seed_warm_cache)
    client = await hass_ws_client(hass)
    router = entry.runtime_data.router

    router.handle(alarm_changed(alarming=True))
    await hass.async_block_till_done()

    assert (await _payload(client, hass))["alarm"] == {"triggered": True}
    assert await hass.config_entries.async_unload(entry.entry_id)


def _b64(body: object) -> str:
    return base64.b64encode(json.dumps(body, separators=(",", ":")).encode()).decode()


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
    fake_station.params[255][1254] = _b64(
        {
            "account_id": SYNTHETIC.account_id,
            "schedules": [
                {"week": 1, "start_h": 8, "start_m": 0, "end_h": 17, "end_m": 30, "mode_id": 1}
            ],
        }
    )
    set_guard_mode(fake_station, int(GuardMode.OFF))
    entry = await set_up_warm(hass, seed_warm_cache)
    client = await hass_ws_client(hass)

    result = await _payload(client, hass)

    assert result["schedule"]["slots"] == [
        {"weekday": 1, "start": "08:00", "end": "17:30", "mode": "home"}
    ]
    assert {"id": "schedule", "name": "Schedule", "custom": False, "settable": True} in result[
        "modes"
    ]
    assert {"id": "off", "name": "Off", "custom": False, "settable": False} in result["modes"]
    assert result["guard_mode"] == "off"
    door = next(d for d in result["devices"] if d["name"] == "Door")
    assert door["kind"] == "sensor"
    assert door["flags"] == ["station_alarm", "notification", "report_monitor_center"]
    assert door_sn not in json.dumps(result)
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

    define_custom_modes(fake_station, {3: _NIGHT})
    await advance_to_poll(hass, POLL_INTERVAL_SECONDS + 1)
    await advance_to_poll(hass, POLL_INTERVAL_SECONDS + 2)  # the debounce
    changed = await _event(client)
    assert {"id": "custom_1", "name": _NIGHT, "custom": True, "settable": True} in changed["modes"]
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
    entry.runtime_data.router.handle(alarm_changed(alarming=True))
    await advance_to_poll(hass, 1)  # the debounce
    assert (await _event(client))["alarm"] == {"triggered": True}
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
    """Actions and the entry delay by HA device id, sent as one Away table; the answer
    carries the table read back and a new revision."""
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
async def test_a_mode_in_use_is_refused_with_the_librarys_reason(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ModeInUseError`` -> ``mode_in_use`` with its reason (no table write raises it
    yet, so the library call is replaced for this one case)."""
    entry = await set_up_warm(hass, seed_warm_cache)
    client = await hass_ws_client(hass)
    revision = (await _payload(client, hass))["revision"]
    station = entry.runtime_data.coordinators[SYNTHETIC.station_sn].station

    async def in_use(*_args: Any, **_kwargs: Any) -> None:
        raise ModeInUseError("Away is the current mode")

    monkeypatch.setattr(station, "async_set_mode_actions", in_use)
    reply = await _set_table(client, hass, revision)

    assert reply["error"] == {"code": "mode_in_use", "message": "Away is the current mode"}
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


# ── per-mode entities follow the defined modes ───────────────────────────────


async def test_per_mode_entities_of_an_undefined_mode_are_unavailable_until_it_is_defined(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Custom 1 defined, Custom 2 not: Custom 2's delay and action switch are unavailable,
    Custom 1's are not; once the station lists Custom 2 they come back."""
    define_custom_modes(fake_station, {3: _NIGHT})
    for mode in ("custom_1", "custom_2"):
        seed_setting(fake_station, f"alarm_delay_{mode}", 0)
        seed_setting(fake_station, f"camera_action_{mode}", 9)
    entry = await set_up_warm(hass, seed_warm_cache)
    registry = er.async_get(hass)
    switch_2 = entity_id_for(
        hass, SWITCH_DOMAIN, SYNTHETIC.camera_sn, "camera_action_custom_2_record"
    )
    registry.async_update_entity(switch_2, disabled_by=None)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    delay_1 = entity_id_for(hass, NUMBER_DOMAIN, SYNTHETIC.camera_sn, "alarm_delay_custom_1")
    delay_2 = entity_id_for(hass, NUMBER_DOMAIN, SYNTHETIC.camera_sn, "alarm_delay_custom_2")

    assert state_of(hass, delay_1) == "0"
    assert state_of(hass, delay_2) == STATE_UNAVAILABLE
    assert state_of(hass, switch_2) == STATE_UNAVAILABLE

    define_custom_modes(fake_station, {3: _NIGHT, 4: "Late"})
    await advance_to_poll(hass, POLL_INTERVAL_SECONDS + 1)

    assert state_of(hass, delay_2) == "0"
    assert state_of(hass, switch_2) == "on"
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
    define_custom_modes(fake_station, {3: _NIGHT})
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


def test_a_mode_name_another_mode_has_gets_its_label() -> None:
    """Two modes of one name stay two options."""

    class _Config:
        settable_modes = (GuardMode.HOME, GuardMode.CUSTOM_1)

        def mode_name(self, code: GuardMode | int) -> str:
            return "Home"

    choices = select.mode_choices(_Config(), GuardMode.HOME)  # type: ignore[arg-type]
    assert choices == {"Home": GuardMode.HOME, "Home (Custom 1)": GuardMode.CUSTOM_1}


# ── stale setting rows (a library release drops or moves settings) ───────────


async def test_rows_of_settings_the_library_moved_or_dropped_are_forgotten(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A setting turned read-only loses its switch row and gets a binary sensor; a
    ``toggles`` member and a whole setting the library no longer lists lose their rows.
    The integration's own rows (named by device class or translation key) stay."""
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

    for gone in (keypad, member, dropped):
        assert registry.async_get(gone.entity_id) is None, gone.unique_id
    for kept in (own_device_class, own_translated):
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
    """The stale-row rule's premise: every row on a setting platform with a literal name and
    no translation key or device class is a setting entity this setup builds."""
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
        if row.domain in setting_domains
        and row.translation_key is None
        and row.original_device_class is None
        and row.original_name is not None
    ]
    assert named
    assert set(named) <= built
    assert await hass.config_entries.async_unload(entry.entry_id)
