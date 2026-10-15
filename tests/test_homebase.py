"""The settings layout, HomeBase and arming-group websocket commands, and the security-mode
select, on the real library with its FakeCloud and FakeStation."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Final

import pytest
from conftest import (
    add_entry,
    advance_to_poll,
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
    redact_serial,
    setting_section,
)
from eufy_home_security.testing import SYNTHETIC, FakeCloud, FakeStation
from homeassistant.components.alarm_control_panel import DOMAIN as ALARM_DOMAIN
from homeassistant.components.number import DOMAIN as NUMBER_DOMAIN
from homeassistant.components.select import ATTR_OPTION, ATTR_OPTIONS, SERVICE_SELECT_OPTION
from homeassistant.components.select import DOMAIN as SELECT_DOMAIN
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import ATTR_ENTITY_ID, EntityCategory, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.typing import (
    MockHAClientWebSocket,
    WebSocketGenerator,
)

from custom_components.eufy_home_security import detections, homebase, settings_layout
from custom_components.eufy_home_security.const import (
    ATTR_SECTION,
    ATTR_VALUE_SOURCE,
    DEFAULT_PRESET_KEY,
    DOMAIN,
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


async def test_the_homebase_answer_names_its_modes_flags_and_per_mode_entities(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Modes with a table in order with translated labels and ``reported``; the camera's
    action switches and delay numbers by mode; HA ids and names only, never a serial."""
    seed_setting(fake_station, _CAMERA_ACTION_AWAY, 9)
    entry = await set_up_warm(hass, seed_warm_cache)
    client = await hass_ws_client(hass)
    station_id = _device_id(hass, SYNTHETIC.station_sn)

    result = await _ok(client, homebase.WS_HOMEBASE, device_id=station_id)

    assert result["device_id"] == station_id
    assert result["alarm_panel"] == panel_entity_id(hass)
    assert result["security_mode"] == entity_id_for(
        hass, SELECT_DOMAIN, SYNTHETIC.station_sn, SECURITY_MODE_KEY
    )
    assert (result["guard_mode"], result["active_mode"]) == ("away", "away")
    assert result["connected"] is True
    assert result["arming_groups"] is True
    assert result["modes"] == [
        {"id": "away", "label": "Away", "reported": True},
        {"id": "home", "label": "Home", "reported": False},
        {"id": "custom_1", "label": "Custom 1", "reported": False},
        {"id": "custom_2", "label": "Custom 2", "reported": False},
        {"id": "custom_3", "label": "Custom 3", "reported": False},
    ]
    assert result["flags"]["camera"][0] == {"id": "record", "label": "Record"}
    assert {f["id"] for f in result["flags"]["sensor"]} >= {"notification", "station_alarm"}
    (camera,) = result["devices"]
    assert camera["device_id"] == _device_id(hass, SYNTHETIC.camera_sn)
    assert camera["kind"] == "camera"
    assert camera["name"]
    assert camera["actions"]["away"]["record"] == entity_id_for(
        hass, SWITCH_DOMAIN, SYNTHETIC.camera_sn, "camera_action_away_record"
    )
    assert set(camera["actions"]) == {"away", "home", "custom_1", "custom_2", "custom_3"}
    assert camera["delays"]["home"] == {
        "entry": entity_id_for(hass, NUMBER_DOMAIN, SYNTHETIC.camera_sn, "alarm_delay_home"),
        "leaving": entity_id_for(hass, NUMBER_DOMAIN, SYNTHETIC.camera_sn, "leaving_delay_home"),
    }
    _no_serial(result)

    reply = await _ws(client, homebase.WS_HOMEBASE, device_id=camera["device_id"])
    assert reply["error"]["code"] == "not_found"
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
    """Each member by HA device id, name and translated mode; an unknown member keeps no
    device id; the station is a candidate with the four settable modes."""
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
    assert [m["id"] for m in candidate["modes"]] == ["away", "home", "schedule", "disarmed"]
    assert candidate["modes"][2]["label"] == "Schedule"
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


async def test_the_security_mode_select_sets_a_mode_and_follows_the_panel(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Options Away, Home, Schedule, Disarmed; a pick writes the station's guard mode, and a
    mode set through the alarm panel shows here too."""
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = _security_mode(hass)
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "away"
    assert state.attributes[ATTR_OPTIONS] == ["away", "home", "schedule", "disarmed"]

    await _select(hass, entity_id, "home")
    assert fake_station.guard_mode == 1
    assert state_of(hass, entity_id) == "home"

    await hass.services.async_call(
        ALARM_DOMAIN, "alarm_arm_away", {ATTR_ENTITY_ID: panel_entity_id(hass)}, blocking=True
    )
    assert state_of(hass, entity_id) == "away"
    await advance_to_poll(hass, 2)  # let a written mode's dump-settle timer fire
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_a_custom_mode_is_shown_but_refused_on_write(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A custom mode in force is an extra option; picking it raises a translated
    validation error and sends nothing."""
    set_guard_mode(fake_station, 3)
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = _security_mode(hass)
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "custom_1"
    assert state.attributes[ATTR_OPTIONS][-1] == "custom_1"
    with pytest.raises(ServiceValidationError) as raised:
        await _select(hass, entity_id, "custom_1")

    assert raised.value.translation_key == "security_mode_not_settable"
    assert fake_station.guard_mode == 3
    assert state_of(hass, entity_id) == "custom_1"
    assert await hass.config_entries.async_unload(entry.entry_id)


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
