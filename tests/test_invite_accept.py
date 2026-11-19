"""Accepting a pending eufy invitation from the pending-invitations repair.

The fix reads the invitations afresh, offers to accept one here (a code field only
for an invitation that needs the inviter's code) or to look again after an accept in
the eufy app, and reloads the entry with one rescan once something was accepted.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Final

import pytest
from conftest import set_up_warm
from eufy_home_security import (
    CloudApiError,
    CommunicationError,
    EufySecurity,
    EufySecurityError,
    InviteCodeError,
    NoCachedSessionError,
    SessionReplacedError,
)
from eufy_home_security.cloud.crypto import invite_seal
from eufy_home_security.testing import SYNTHETIC, FakeCloud, FakeStation
from homeassistant.components.repairs import RepairsFlowManager, repairs_flow_manager
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, UnknownFlow
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.components.diagnostics import get_diagnostics_for_config_entry
from pytest_homeassistant.typing import ClientSessionGenerator

from custom_components.eufy_home_security import errors, runtime
from custom_components.eufy_home_security.const import DOMAIN
from custom_components.eufy_home_security.repairs import PendingInvitesFix

# Synthetic invitations as the cloud lists them.
_HOME_NAME: Final = "Synthetic Cottage"
_INVITER: Final = "synthetic-owner"
_CODE: Final = "synthetic-code-4711"
_HOUSE_INVITE: Final = {
    "id": 7,
    "house_id": "synthetic-house",
    "house_name": _HOME_NAME,
    "action_user_nick": _INVITER,
}
_SHARED_CAMERA: Final = "T8170P0000001111"
_SHARED_DOORBELL: Final = "T8170P0000002222"


def _device_invite(
    invite_id: int,
    serial: str,
    *,
    owner: str = "synthetic-owner-id",
    code: str | None = None,
    category: str | None = None,
) -> dict[str, Any]:
    """One ``get_invites`` entry; ``code`` seals it as an inviter's code does."""
    entry: dict[str, Any] = {
        "id": invite_id,
        "device_sn": serial,
        "product_code": serial[:5],
        "action_user_nick": _INVITER,
        "user_id": owner,
        "sub_sns": "",
    }
    if code is not None:
        entry["encrypted_data"] = invite_seal(code, "synthetic")
    if category is not None:
        entry["category"] = category
    return entry


@pytest.fixture
def discover_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """The keyword arguments of every ``EufySecurity.async_discover`` call, in order."""
    calls: list[dict[str, Any]] = []
    original = EufySecurity.async_discover

    async def recording(self: EufySecurity, **kwargs: Any) -> Any:
        calls.append(kwargs)
        return await original(self, **kwargs)

    monkeypatch.setattr(EufySecurity, "async_discover", recording)
    return calls


async def _set_up_invited(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    seed_warm_cache: Callable[..., None],
    *,
    house: list[dict[str, Any]] | None = None,
    devices: list[dict[str, Any]] | None = None,
) -> tuple[MockConfigEntry, list[dict[str, Any]]]:
    """An entry whose account lists nothing and has the given invitations open; also
    the device list the cloud gives once they are accepted."""
    listed = list(fake_cloud.devices)
    fake_cloud.devices = []
    fake_cloud.house_invites = list(house or [])
    fake_cloud.device_invites = list(devices or [])
    entry = await set_up_warm(hass, seed_warm_cache)
    assert _issue(hass, entry) is not None
    return entry, listed


def _issue(hass: HomeAssistant, entry: MockConfigEntry) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(
        DOMAIN, errors.pending_invites_issue_id(entry.entry_id)
    )


def _accepts(cloud: FakeCloud) -> list[str]:
    return [call for call in cloud.calls if call.startswith("accept_")]


def _fields(result: Mapping[str, Any]) -> set[str]:
    schema = result["data_schema"]
    assert schema is not None
    return {str(key) for key in schema.schema}


async def _open_fix(hass: HomeAssistant, entry: MockConfigEntry) -> tuple[RepairsFlowManager, str]:
    """The repair's fix, opened and moved past its menu to accept here."""
    assert await async_setup_component(hass, "repairs", {})
    manager = repairs_flow_manager(hass)
    assert manager is not None
    result = await manager.async_init(
        DOMAIN, data={"issue_id": errors.pending_invites_issue_id(entry.entry_id)}
    )
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "init"
    assert result["menu_options"] == ["pick", "confirm"]
    return manager, result["flow_id"]


async def test_a_home_invitation_without_a_code_is_accepted_and_its_station_set_up(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    discover_calls: list[dict[str, Any]],
) -> None:
    """One invitation: no picker, no code field; the accept names the home, the reload
    asks every scope once, and the shared station is served with the issue gone."""
    entry, listed = await _set_up_invited(
        hass, fake_cloud, seed_warm_cache, house=[dict(_HOUSE_INVITE)]
    )
    manager, flow_id = await _open_fix(hass, entry)

    result = await manager.async_configure(flow_id, {"next_step_id": "pick"})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "accept"
    assert _fields(result) == set()
    placeholders = result["description_placeholders"]
    assert placeholders is not None
    assert placeholders["invitation"] == f"“{_HOME_NAME}” from {_INVITER}"
    clients = len(built_clients)

    fake_cloud.devices = listed
    result = await manager.async_configure(flow_id, {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    assert _accepts(fake_cloud) == ["accept_house_invite"]
    (body,) = fake_cloud.invite_accepts
    assert (body["house_id"], body["invite_id"]) == ("synthetic-house", 7)
    assert len(built_clients) == clients + 1, "reloaded once"
    assert discover_calls[-1] == {"rescan_regions": True}
    assert SYNTHETIC.station_sn in entry.runtime_data.eufy.stations
    assert _issue(hass, entry) is None
    assert "login" not in fake_cloud.calls


async def test_a_coded_invitation_keeps_the_form_open_on_a_wrong_or_missing_code(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    discover_calls: list[dict[str, Any]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The code field shows; a wrong or empty code is a form error and sends nothing;
    the right one accepts the device with its owner, and is neither sent nor logged."""
    caplog.set_level(logging.DEBUG)
    entry, listed = await _set_up_invited(
        hass, fake_cloud, seed_warm_cache, devices=[_device_invite(11, _SHARED_CAMERA, code=_CODE)]
    )
    manager, flow_id = await _open_fix(hass, entry)
    result = await manager.async_configure(flow_id, {"next_step_id": "pick"})
    assert result["step_id"] == "accept"
    assert _fields(result) == {"code"}
    schema = result["data_schema"]
    assert schema is not None
    (code_selector,) = schema.schema.values()
    assert code_selector.config["type"] == "password"

    result = await manager.async_configure(flow_id, {"code": "not-the-code"})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"code": "wrong_code"}
    result = await manager.async_configure(flow_id, {"code": ""})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"code": "code_missing"}
    assert fake_cloud.invite_accepts == []
    assert _accepts(fake_cloud) == []

    fake_cloud.devices = listed
    result = await manager.async_configure(flow_id, {"code": _CODE})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    (body,) = fake_cloud.invite_accepts
    (accepted,) = body["invites"]
    assert accepted["device_sn"] == _SHARED_CAMERA
    assert accepted["invite_id"] == 11
    assert accepted["owner_user_id"] == "synthetic-owner-id"
    assert _CODE not in json.dumps(body)
    assert discover_calls[-1] == {"rescan_regions": True}
    assert SYNTHETIC.station_sn in entry.runtime_data.eufy.stations
    for private in (_CODE, _INVITER, _SHARED_CAMERA):
        assert private not in caplog.text


async def test_an_appliance_invitation_is_left_to_the_eufy_app(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A home-appliance category aborts with the eufy-app text; nothing sent, no reload,
    the issue stays."""
    entry, _ = await _set_up_invited(
        hass,
        fake_cloud,
        seed_warm_cache,
        devices=[_device_invite(12, _SHARED_CAMERA, category="eufy_home")],
    )
    manager, flow_id = await _open_fix(hass, entry)
    await manager.async_configure(flow_id, {"next_step_id": "pick"})
    clients = len(built_clients)

    result = await manager.async_configure(flow_id, {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "accept_in_app"
    await hass.async_block_till_done()

    assert _accepts(fake_cloud) == []
    assert len(built_clients) == clients
    assert _issue(hass, entry) is not None


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (NoCachedSessionError("synthetic-detail"), "session_lapsed"),
        (CloudApiError(1, "synthetic-detail"), "accept_failed"),
        (CommunicationError("synthetic-detail"), "accept_failed"),
    ],
)
async def test_a_failed_accept_aborts_with_its_reason_and_reloads_nothing(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error: EufySecurityError,
    reason: str,
) -> None:
    """A lapsed session gets its own text; a cloud refusal or an unreachable cloud the
    generic one. The issue stays, the entry is not reloaded, and the library's message
    text is not logged."""
    entry, _ = await _set_up_invited(hass, fake_cloud, seed_warm_cache, house=[dict(_HOUSE_INVITE)])

    async def failing(*args: Any, **kwargs: Any) -> None:
        raise error

    monkeypatch.setattr(entry.runtime_data.eufy, "async_accept_invite", failing)
    manager, flow_id = await _open_fix(hass, entry)
    await manager.async_configure(flow_id, {"next_step_id": "pick"})
    clients = len(built_clients)

    result = await manager.async_configure(flow_id, {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == reason
    await hass.async_block_till_done()
    assert len(built_clients) == clients
    assert _issue(hass, entry) is not None
    assert "synthetic-detail" not in caplog.text


async def test_a_replaced_session_aborts_and_raises_its_own_repair(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Another client ended the session: its own text and the session-replaced repair,
    whose fix signs in again; no reload."""
    entry, _ = await _set_up_invited(hass, fake_cloud, seed_warm_cache, house=[dict(_HOUSE_INVITE)])
    manager, flow_id = await _open_fix(hass, entry)
    await manager.async_configure(flow_id, {"next_step_id": "pick"})
    clients = len(built_clients)

    fake_cloud.call_errors = [SessionReplacedError("synthetic")]
    result = await manager.async_configure(flow_id, {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "session_replaced"
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(
        DOMAIN, errors.session_replaced_issue_id(entry.entry_id)
    )
    assert len(built_clients) == clients
    assert _issue(hass, entry) is not None


async def test_a_cloud_refusal_of_the_accept_aborts_and_keeps_the_issue(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The cloud answers the accept with an error code: the generic text."""
    entry, _ = await _set_up_invited(hass, fake_cloud, seed_warm_cache, house=[dict(_HOUSE_INVITE)])
    manager, flow_id = await _open_fix(hass, entry)
    await manager.async_configure(flow_id, {"next_step_id": "pick"})

    fake_cloud.call_errors = [CloudApiError(1, "synthetic")]
    result = await manager.async_configure(flow_id, {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "accept_failed"
    assert _accepts(fake_cloud) == ["accept_house_invite"]
    assert fake_cloud.house_invites, "still pending"
    assert _issue(hass, entry) is not None


async def test_two_invitations_are_picked_one_at_a_time_then_finished(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    discover_calls: list[dict[str, Any]],
) -> None:
    """Two devices of one model differ by their redacted serials; after one accept the
    other stays open, and Finish reloads once with a rescan."""
    entry, _ = await _set_up_invited(
        hass,
        fake_cloud,
        seed_warm_cache,
        devices=[
            _device_invite(21, _SHARED_CAMERA, owner="synthetic-owner-a"),
            _device_invite(22, _SHARED_DOORBELL, owner="synthetic-owner-b"),
        ],
    )
    manager, flow_id = await _open_fix(hass, entry)
    result = await manager.async_configure(flow_id, {"next_step_id": "pick"})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "pick"
    schema = result["data_schema"]
    assert schema is not None
    (selector,) = schema.schema.values()
    assert selector.config["mode"] == "list"
    labels = [option["label"] for option in selector.config["options"]]
    assert labels == [
        f"SoloCam S340 from {_INVITER} (T8170***1111)",
        f"SoloCam S340 from {_INVITER} (T8170***2222)",
    ]

    result = await manager.async_configure(flow_id, {"invitation": "1"})
    assert result["step_id"] == "accept"
    placeholders = result["description_placeholders"]
    assert placeholders is not None
    assert placeholders["invitation"] == f"SoloCam S340 from {_INVITER} (T8170***2222)"
    result = await manager.async_configure(flow_id, {})
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "accepted"
    assert result["menu_options"] == ["pick", "finish"]
    placeholders = result["description_placeholders"]
    assert placeholders is not None
    assert placeholders["invites"] == f"SoloCam S340 from {_INVITER}"
    assert [accept["invites"][0]["invite_id"] for accept in fake_cloud.invite_accepts] == [22]
    clients = len(built_clients)

    result = await manager.async_configure(flow_id, {"next_step_id": "finish"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert len(built_clients) == clients + 1, "reloaded once"
    assert discover_calls[-1] == {"rescan_regions": True}


async def test_closing_the_fix_after_an_accept_still_reloads(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    discover_calls: list[dict[str, Any]],
) -> None:
    """Closed on the "accepted" menu: the entry reloads once with a rescan anyway."""
    entry, _ = await _set_up_invited(
        hass,
        fake_cloud,
        seed_warm_cache,
        devices=[
            _device_invite(31, _SHARED_CAMERA, owner="synthetic-owner-a"),
            _device_invite(32, _SHARED_DOORBELL, owner="synthetic-owner-b"),
        ],
    )
    manager, flow_id = await _open_fix(hass, entry)
    await manager.async_configure(flow_id, {"next_step_id": "pick"})
    await manager.async_configure(flow_id, {"invitation": "0"})
    result = await manager.async_configure(flow_id, {})
    assert result["step_id"] == "accepted"
    clients = len(built_clients)

    manager.async_abort(flow_id)
    await hass.async_block_till_done()
    assert len(built_clients) == clients + 1, "reloaded once"
    assert discover_calls[-1] == {"rescan_regions": True}


async def test_accept_another_takes_the_last_one_without_a_picker_then_finishes(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    discover_calls: list[dict[str, Any]],
) -> None:
    """ "Accept another" with one left opens its accept form; accepting it finishes with
    one reload."""
    entry, _ = await _set_up_invited(
        hass,
        fake_cloud,
        seed_warm_cache,
        devices=[
            _device_invite(81, _SHARED_CAMERA, owner="synthetic-owner-a"),
            _device_invite(82, _SHARED_DOORBELL, owner="synthetic-owner-b"),
        ],
    )
    manager, flow_id = await _open_fix(hass, entry)
    await manager.async_configure(flow_id, {"next_step_id": "pick"})
    await manager.async_configure(flow_id, {"invitation": "0"})
    result = await manager.async_configure(flow_id, {})
    assert result["step_id"] == "accepted"

    result = await manager.async_configure(flow_id, {"next_step_id": "pick"})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "accept"
    placeholders = result["description_placeholders"]
    assert placeholders is not None
    assert placeholders["invitation"] == f"SoloCam S340 from {_INVITER} (T8170***2222)"
    clients = len(built_clients)
    result = await manager.async_configure(flow_id, {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    assert [accept["invites"][0]["invite_id"] for accept in fake_cloud.invite_accepts] == [81, 82]
    assert len(built_clients) == clients + 1, "reloaded once"
    assert discover_calls[-1] == {"rescan_regions": True}


async def test_closing_the_fix_while_an_accept_runs_still_reloads(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    discover_calls: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dialog closed before the accept answers: the accept, once it succeeds, reloads."""
    entry, _ = await _set_up_invited(hass, fake_cloud, seed_warm_cache, house=[dict(_HOUSE_INVITE)])
    manager, flow_id = await _open_fix(hass, entry)
    await manager.async_configure(flow_id, {"next_step_id": "pick"})
    eufy = entry.runtime_data.eufy
    original = eufy.async_accept_invite

    async def closed_meanwhile(invite: Any, *, code: str | None = None) -> None:
        manager.async_abort(flow_id)
        await original(invite, code=code)

    monkeypatch.setattr(eufy, "async_accept_invite", closed_meanwhile)
    clients = len(built_clients)
    with pytest.raises(UnknownFlow):
        await manager.async_configure(flow_id, {})
    await hass.async_block_till_done()

    assert _accepts(fake_cloud) == ["accept_house_invite"]
    assert len(built_clients) == clients + 1, "reloaded once"
    assert discover_calls[-1] == {"rescan_regions": True}


async def test_closing_during_a_later_accept_reloads_once_after_it(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    discover_calls: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One accepted, the dialog closed during the second: no reload while that accept
    runs on the client, one reload after it, asking every scope."""
    entry, _ = await _set_up_invited(
        hass,
        fake_cloud,
        seed_warm_cache,
        devices=[
            _device_invite(101, _SHARED_CAMERA, owner="synthetic-owner-a"),
            _device_invite(102, _SHARED_DOORBELL, owner="synthetic-owner-b"),
        ],
    )
    manager, flow_id = await _open_fix(hass, entry)
    await manager.async_configure(flow_id, {"next_step_id": "pick"})
    await manager.async_configure(flow_id, {"invitation": "0"})
    await manager.async_configure(flow_id, {})
    result = await manager.async_configure(flow_id, {"next_step_id": "pick"})
    assert result["step_id"] == "accept"
    eufy = entry.runtime_data.eufy
    original = eufy.async_accept_invite
    clients = len(built_clients)
    during: list[int] = []

    async def closed_meanwhile(invite: Any, *, code: str | None = None) -> None:
        manager.async_abort(flow_id)
        await asyncio.sleep(0.05)
        during.append(len(built_clients))
        await original(invite, code=code)

    monkeypatch.setattr(eufy, "async_accept_invite", closed_meanwhile)
    discovered = len(discover_calls)
    with pytest.raises(UnknownFlow):
        await manager.async_configure(flow_id, {})
    await hass.async_block_till_done()

    assert during == [clients], "no reload while the accept runs"
    assert [accept["invites"][0]["invite_id"] for accept in fake_cloud.invite_accepts] == [101, 102]
    assert len(built_clients) == clients + 1, "reloaded once"
    assert discover_calls[discovered:] == [{"rescan_regions": True}]


@pytest.mark.parametrize(
    ("earlier_accept", "error", "reloads"),
    [
        (False, CloudApiError(1, "synthetic"), 0),
        (True, CloudApiError(1, "synthetic"), 1),
        (True, InviteCodeError("synthetic", missing=False), 1),
    ],
)
async def test_closing_during_a_failing_accept_reloads_only_for_an_earlier_one(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    discover_calls: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    earlier_accept: bool,
    error: EufySecurityError,
    reloads: int,
) -> None:
    """Closed while an accept runs that then fails: one reload after it when an earlier
    accept succeeded, none otherwise; never while it runs."""
    entry, _ = await _set_up_invited(
        hass,
        fake_cloud,
        seed_warm_cache,
        devices=[
            _device_invite(111, _SHARED_CAMERA, owner="synthetic-owner-a"),
            _device_invite(112, _SHARED_DOORBELL, owner="synthetic-owner-b"),
        ],
    )
    manager, flow_id = await _open_fix(hass, entry)
    await manager.async_configure(flow_id, {"next_step_id": "pick"})
    if earlier_accept:
        await manager.async_configure(flow_id, {"invitation": "0"})
        await manager.async_configure(flow_id, {})
        result = await manager.async_configure(flow_id, {"next_step_id": "pick"})
    else:
        result = await manager.async_configure(flow_id, {"invitation": "1"})
    assert result["step_id"] == "accept"
    eufy = entry.runtime_data.eufy
    clients = len(built_clients)
    during: list[int] = []

    async def closed_then_failing(invite: Any, *, code: str | None = None) -> None:
        manager.async_abort(flow_id)
        await asyncio.sleep(0.05)
        during.append(len(built_clients))
        raise error

    monkeypatch.setattr(eufy, "async_accept_invite", closed_then_failing)
    discovered = len(discover_calls)
    # The dialog is gone: an abort result goes nowhere, a form result raises.
    with contextlib.suppress(UnknownFlow):
        await manager.async_configure(flow_id, {})
    await hass.async_block_till_done()

    assert during == [clients], "no reload while the accept runs"
    assert len(built_clients) == clients + reloads
    assert discover_calls[discovered:] == [{"rescan_regions": True}] * reloads
    assert runtime.take_rescan_at_setup(hass, entry.entry_id) is False


async def test_no_reload_after_an_accept_while_home_assistant_stops(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Closed after an accept while Home Assistant stops: nothing scheduled."""
    entry = MockConfigEntry(domain=DOMAIN, title="synthetic")
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    scheduled: list[str] = []
    monkeypatch.setattr(hass.config_entries, "async_schedule_reload", scheduled.append)
    flow = PendingInvitesFix(entry.entry_id)
    flow.hass = hass
    flow._accepted = True

    hass.set_state(CoreState.stopping)
    try:
        flow.async_remove()
    finally:
        hass.set_state(CoreState.running)

    assert scheduled == []


@pytest.mark.parametrize(
    ("state", "reloads"),
    [
        (ConfigEntryState.LOADED, True),
        (ConfigEntryState.SETUP_RETRY, True),
        (ConfigEntryState.SETUP_IN_PROGRESS, True),
        (ConfigEntryState.NOT_LOADED, False),
        (ConfigEntryState.SETUP_ERROR, False),
        (ConfigEntryState.UNLOAD_IN_PROGRESS, False),
    ],
)
async def test_the_reload_after_an_accept_follows_the_entry_state(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    state: ConfigEntryState,
    reloads: bool,
) -> None:
    """Every state asks every scope at the next setup; only a running entry, one being
    set up or one waiting to retry is reloaded."""
    entry = MockConfigEntry(domain=DOMAIN, title="synthetic")
    entry.add_to_hass(hass)
    entry.mock_state(hass, state)
    scheduled: list[str] = []
    monkeypatch.setattr(hass.config_entries, "async_schedule_reload", scheduled.append)
    flow = PendingInvitesFix(entry.entry_id)
    flow.hass = hass
    flow._accepted = True

    flow.async_remove()

    assert scheduled == ([entry.entry_id] if reloads else [])
    assert runtime.take_rescan_at_setup(hass, entry.entry_id)


def test_every_step_and_reason_of_the_fix_has_its_text() -> None:
    """Each step id, menu option, error and abort reason the fix uses is in the strings."""
    source = inspect.getsource(PendingInvitesFix)
    strings = json.loads(
        (Path(errors.__file__).parent / "strings.json").read_text(encoding="utf-8")
    )["issues"]["pending_invites"]["fix_flow"]
    steps = set(re.findall(r'step_id="(\w+)"', source))
    reasons = set(re.findall(r'reason="(\w+)"', source))
    menu = {
        option
        for listed in re.findall(r"menu_options=\[([^\]]*)\]", source)
        for option in re.findall(r'"(\w+)"', listed)
    }
    form_errors = set(re.findall(r'"(\w+)" if err\.missing else "(\w+)"', source)[0])

    assert steps == {"init", "pick", "accept", "accepted", "confirm"}
    assert steps <= set(strings["step"])
    shown = {
        option for step in ("init", "accepted") for option in strings["step"][step]["menu_options"]
    }
    assert menu == shown
    assert reasons == set(strings["abort"])
    assert form_errors == set(strings["error"])


async def test_an_entry_unloaded_after_an_accept_stays_unloaded_and_rescans_at_its_setup(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The user unloads the entry with the fix open: the next accept aborts, nothing
    sets the entry up again, and its next setup asks every scope."""
    entry, _ = await _set_up_invited(
        hass,
        fake_cloud,
        seed_warm_cache,
        devices=[
            _device_invite(91, _SHARED_CAMERA, owner="synthetic-owner-a"),
            _device_invite(92, _SHARED_DOORBELL, owner="synthetic-owner-b"),
        ],
    )
    manager, flow_id = await _open_fix(hass, entry)
    await manager.async_configure(flow_id, {"next_step_id": "pick"})
    await manager.async_configure(flow_id, {"invitation": "0"})
    await manager.async_configure(flow_id, {})
    result = await manager.async_configure(flow_id, {"next_step_id": "pick"})
    assert result["step_id"] == "accept"
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    clients = len(built_clients)

    result = await manager.async_configure(flow_id, {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "entry_not_loaded"
    await hass.async_block_till_done()

    assert _accepts(fake_cloud) == ["accept_device_invite"]
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert len(built_clients) == clients
    assert runtime.take_rescan_at_setup(hass, entry.entry_id)


async def test_closing_the_fix_after_its_entry_was_removed_reloads_nothing(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An accept, then the entry removed while the fix is open: closing it is quiet."""
    entry, _ = await _set_up_invited(
        hass,
        fake_cloud,
        seed_warm_cache,
        devices=[
            _device_invite(51, _SHARED_CAMERA, owner="synthetic-owner-a"),
            _device_invite(52, _SHARED_DOORBELL, owner="synthetic-owner-b"),
        ],
    )
    manager, flow_id = await _open_fix(hass, entry)
    await manager.async_configure(flow_id, {"next_step_id": "pick"})
    await manager.async_configure(flow_id, {"invitation": "0"})
    result = await manager.async_configure(flow_id, {})
    assert result["step_id"] == "accepted"
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    manager.async_abort(flow_id)
    await hass.async_block_till_done()
    assert "Error removing" not in caplog.text


async def test_closing_the_fix_without_an_accept_reloads_nothing(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Opened and closed on the menu: no reload."""
    entry, _ = await _set_up_invited(hass, fake_cloud, seed_warm_cache, house=[dict(_HOUSE_INVITE)])
    manager, flow_id = await _open_fix(hass, entry)
    clients = len(built_clients)

    manager.async_abort(flow_id)
    await hass.async_block_till_done()
    assert len(built_clients) == clients


async def test_an_invitation_accepted_meanwhile_opens_on_looking_again(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Nothing open when the fix starts: straight to the confirmation, naming what the
    issue was raised for."""
    entry, _ = await _set_up_invited(hass, fake_cloud, seed_warm_cache, house=[dict(_HOUSE_INVITE)])
    fake_cloud.house_invites = []
    assert await async_setup_component(hass, "repairs", {})
    manager = repairs_flow_manager(hass)
    assert manager is not None

    result = await manager.async_init(
        DOMAIN, data={"issue_id": errors.pending_invites_issue_id(entry.entry_id)}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"
    placeholders = result["description_placeholders"]
    assert placeholders is not None
    assert placeholders["invites"] == f"“{_HOME_NAME}” from {_INVITER}"


async def test_diagnostics_say_whether_an_invitation_needs_a_code_and_nothing_private(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The account report's invitation carries ``needs_code``; no inviter, no sealed code."""
    entry, _ = await _set_up_invited(
        hass, fake_cloud, seed_warm_cache, devices=[_device_invite(41, _SHARED_CAMERA, code=_CODE)]
    )
    payload = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    assert isinstance(payload, dict)
    (invite,) = payload["account_report"]["invites"]
    assert invite["needs_code"] is True
    text = json.dumps(payload)
    assert _INVITER not in text
    assert fake_cloud.device_invites[0]["encrypted_data"] not in text
