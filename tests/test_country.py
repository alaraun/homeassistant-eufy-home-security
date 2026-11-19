"""The eufy login country and the pending-invitations repair.

eufy lists a device only to a login with the country it is held under, so the entry
logs in with a country the user picks (Home Assistant's by default) and Home
Assistant's time zone. An account that lists nothing is told about invitations it
has not accepted, and the repair's fix asks every login scope again (accepting from
the repair itself: ``test_invite_accept.py``).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any, Final

import pytest
from conftest import (
    add_entry,
    add_motion_sensor,
    cloud_calls,
    configure_options,
    entity_id_for,
    set_up_warm,
    setup_entry,
    wait_until,
)
from eufy_home_security import CommunicationError, EufySecurity
from eufy_home_security.testing import SYNTHETIC, FakeCloud, FakeStation
from homeassistant.components.repairs import RepairsFlowManager, repairs_flow_manager
from homeassistant.config_entries import SOURCE_USER, ConfigEntry, ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.components.diagnostics import (
    _get_diagnostics_for_config_entry,
)
from pytest_homeassistant.typing import ClientSessionGenerator

from custom_components.eufy_home_security import errors, runtime
from custom_components.eufy_home_security.const import (
    CONF_COUNTRY,
    CONF_EXTRA_COUNTRIES,
    CONF_SCAN_REGIONS,
    DOMAIN,
    OPTIONS_STEP_INIT,
    REFRESH_DEVICE_LIST_KEY,
)

# A pending home invitation as the cloud lists it; synthetic names.
_HOME_NAME: Final = "Synthetic Cottage"
_INVITER: Final = "synthetic-owner"
_HOUSE_INVITE: Final = {
    "id": 7,
    "house_id": "synthetic-house",
    "house_name": _HOME_NAME,
    "action_user_nick": _INVITER,
}


def _invites_issue(hass: HomeAssistant, entry: MockConfigEntry) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(
        DOMAIN, errors.pending_invites_issue_id(entry.entry_id)
    )


async def _unload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


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


async def test_setup_logs_in_with_home_assistants_country_by_default(
    hass: HomeAssistant,
    built_clients: list[EufySecurity],
    client_countries: list[str | list[str]],
    seed_warm_cache: Callable[..., None],
) -> None:
    """No option: Home Assistant's country; the option, when set, wins."""
    hass.config.country = "CH"
    entry = await set_up_warm(hass, seed_warm_cache)
    assert client_countries[-1] == "CH"
    await _unload(hass, entry)

    hass.config_entries.async_update_entry(entry, options={CONF_COUNTRY: "DE"})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert client_countries[-1] == "DE"
    await _unload(hass, entry)


def test_the_login_country_falls_back_to_home_assistants_then_to_none(
    hass: HomeAssistant,
) -> None:
    """An empty option means Home Assistant's country; neither set means the library's
    own choice (the host's IP country)."""
    hass.config.country = "EE"
    assert runtime.login_country(hass, {}) == "EE"
    assert runtime.login_country(hass, {CONF_COUNTRY: ""}) == "EE"
    assert runtime.login_country(hass, {CONF_COUNTRY: "CH"}) == "CH"
    hass.config.country = None
    assert runtime.login_country(hass, {}) == ""


def test_extra_countries_follow_the_login_country_without_duplicates(
    hass: HomeAssistant,
) -> None:
    """One country is a string; the login country comes first; repeats are dropped."""
    hass.config.country = "EE"
    assert runtime.login_countries(hass, {}) == "EE"
    assert runtime.login_countries(hass, {CONF_EXTRA_COUNTRIES: []}) == "EE"
    assert runtime.login_countries(hass, {CONF_EXTRA_COUNTRIES: ["CH", "EE", "CH"]}) == [
        "EE",
        "CH",
    ]
    assert runtime.login_countries(hass, {CONF_COUNTRY: "DE", CONF_EXTRA_COUNTRIES: ["CH"]}) == [
        "DE",
        "CH",
    ]
    hass.config.country = None
    assert runtime.login_countries(hass, {}) == ""
    assert runtime.login_countries(hass, {CONF_EXTRA_COUNTRIES: ["CH"]}) == "CH"


async def test_an_added_extra_country_signs_in_with_it_and_asks_every_scope_once(
    hass: HomeAssistant,
    built_clients: list[EufySecurity],
    client_countries: list[str | list[str]],
    seed_warm_cache: Callable[..., None],
    discover_calls: list[dict[str, Any]],
) -> None:
    """The extra country reaches the library after the login country; the reload rescans."""
    hass.config.country = "EE"
    entry = await set_up_warm(hass, seed_warm_cache)
    clients = len(built_clients)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await configure_options(hass, result["flow_id"], {CONF_EXTRA_COUNTRIES: ["CH"]})
    await hass.async_block_till_done()

    assert entry.options[CONF_EXTRA_COUNTRIES] == ["CH"]
    assert len(built_clients) == clients + 1, "the entry was not reloaded"
    assert client_countries[-1] == ["EE", "CH"]
    assert discover_calls[-1] == {"rescan_regions": True}
    await _unload(hass, entry)


async def test_the_user_step_signs_in_with_the_picked_country_and_keeps_it_as_an_option(
    hass: HomeAssistant,
    built_clients: list[EufySecurity],
    client_countries: list[str | list[str]],
) -> None:
    """The form suggests Home Assistant's country; a picked one is the entry's option."""
    hass.config.country = "EE"
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    schema = result["data_schema"]
    assert schema is not None
    (country_key,) = (key for key in schema.schema if key == CONF_COUNTRY)
    assert (country_key.description or {}).get("suggested_value") == "EE"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_EMAIL: SYNTHETIC.email, CONF_PASSWORD: SYNTHETIC.password, CONF_COUNTRY: "CH"},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert client_countries[0] == "CH"
    entry = result["result"]
    assert entry.options == {CONF_COUNTRY: "CH"}
    await _unload(hass, entry)


async def test_a_changed_login_country_reloads_and_asks_every_login_scope_once(
    hass: HomeAssistant,
    built_clients: list[EufySecurity],
    client_countries: list[str | list[str]],
    seed_warm_cache: Callable[..., None],
    discover_calls: list[dict[str, Any]],
) -> None:
    """Saving another country rescans at the reload's setup; saving Home Assistant's own
    country over an empty option changes nothing and reloads nothing."""
    hass.config.country = "EE"
    entry = await set_up_warm(hass, seed_warm_cache)
    assert discover_calls[-1] == {"rescan_regions": False}
    clients = len(built_clients)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == OPTIONS_STEP_INIT
    result = await configure_options(hass, result["flow_id"], {CONF_COUNTRY: "EE"})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert len(built_clients) == clients, "Home Assistant's own country is no change"

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await configure_options(hass, result["flow_id"], {CONF_COUNTRY: "CH"})
    await hass.async_block_till_done()

    assert len(built_clients) == clients + 1, "the entry was not reloaded"
    assert client_countries[-1] == "CH"
    assert discover_calls[-1] == {"rescan_regions": True}
    assert not runtime.take_rescan_at_setup(hass, entry.entry_id), "the rescan is used up"
    await _unload(hass, entry)


async def test_an_account_with_a_pending_invitation_gets_a_repair_naming_it(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    fake_cloud: FakeCloud,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """No device listed and an open home invitation: one fixable issue naming the home
    and the inviter, which is not persistent and so never in a diagnostics download."""
    fake_cloud.devices = []
    fake_cloud.house_invites = [dict(_HOUSE_INVITE)]
    entry = await set_up_warm(hass, seed_warm_cache)

    issue = _invites_issue(hass, entry)
    assert issue is not None
    assert issue.is_fixable is True
    assert issue.is_persistent is False
    assert issue.translation_placeholders is not None
    assert issue.translation_placeholders["invites"] == f"“{_HOME_NAME}” from {_INVITER}"
    payload = await _get_diagnostics_for_config_entry(hass, hass_client, entry)
    text = json.dumps(payload)
    assert errors.pending_invites_issue_id(entry.entry_id) in text, "the issue is listed"
    assert _HOME_NAME not in text
    assert _INVITER not in text

    await _unload(hass, entry)
    assert _invites_issue(hass, entry) is None


async def test_a_warm_start_with_devices_never_reads_its_invitations(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A served station on the cached list: no invitation read and no issue (a warm
    start makes no cloud call)."""
    fake_cloud.house_invites = [dict(_HOUSE_INVITE)]
    entry = await set_up_warm(hass, seed_warm_cache)

    assert _invites_issue(hass, entry) is None
    assert not [call for call in cloud_calls(fake_cloud) if "invites" in call]
    await _unload(hass, entry)


@pytest.mark.parametrize("pending", [True, False], ids=["invited", "none"])
async def test_a_setup_that_fetches_the_list_reads_the_invitations_with_devices(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    pending: bool,
) -> None:
    """A rescan setup (as after accepting) fetches the list: with a station served,
    the invitations are read too, and the repair names an open one."""
    entry = await set_up_warm(hass, seed_warm_cache)
    fake_cloud.house_invites = [dict(_HOUSE_INVITE)] if pending else []
    runtime.request_rescan_at_setup(hass, entry.entry_id)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert built_clients[-1].device_list_source == "fetched"
    assert fake_station.serial in entry.runtime_data.coordinators
    assert [call for call in cloud_calls(fake_cloud) if "invites" in call]
    issue = _invites_issue(hass, entry)
    if pending:
        assert issue is not None
        assert issue.translation_placeholders is not None
        assert issue.translation_placeholders["invites"] == f"“{_HOME_NAME}” from {_INVITER}"
    else:
        assert issue is None
    await _unload(hass, entry)


async def test_a_warm_reload_shows_the_invitations_last_read(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The issue is not persistent: a warm reload (no cloud call) shows again what the
    last read found."""
    entry = await set_up_warm(hass, seed_warm_cache)
    button = entity_id_for(hass, "button", entry.entry_id, REFRESH_DEVICE_LIST_KEY)
    fake_cloud.house_invites = [dict(_HOUSE_INVITE)]
    await hass.services.async_call("button", "press", {ATTR_ENTITY_ID: button}, blocking=True)
    await hass.async_block_till_done()
    assert _invites_issue(hass, entry) is not None
    reads = len([call for call in cloud_calls(fake_cloud) if "invites" in call])

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert len([call for call in cloud_calls(fake_cloud) if "invites" in call]) == reads
    issue = _invites_issue(hass, entry)
    assert issue is not None
    assert issue.translation_placeholders is not None
    assert issue.translation_placeholders["invites"] == f"“{_HOME_NAME}” from {_INVITER}"
    await _unload(hass, entry)


async def test_refresh_device_list_with_devices_names_a_new_invitation(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """With a station served and none added, a press reads the invitations again: one
    sent after setup shows, and goes once withdrawn; nothing reloads."""
    entry = await set_up_warm(hass, seed_warm_cache)
    assert _invites_issue(hass, entry) is None
    button = entity_id_for(hass, "button", entry.entry_id, REFRESH_DEVICE_LIST_KEY)

    fake_cloud.house_invites = [dict(_HOUSE_INVITE)]
    await hass.services.async_call("button", "press", {ATTR_ENTITY_ID: button}, blocking=True)
    await hass.async_block_till_done()
    assert _invites_issue(hass, entry) is not None
    assert not entry.runtime_data.router.reload_pending

    fake_cloud.house_invites = []
    await hass.services.async_call("button", "press", {ATTR_ENTITY_ID: button}, blocking=True)
    await hass.async_block_till_done()
    assert _invites_issue(hass, entry) is None
    assert fake_station.serial in entry.runtime_data.coordinators
    await _unload(hass, entry)


async def test_refresh_device_list_on_an_empty_account_names_a_new_invitation(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """An invitation sent after setup shows on the next press; withdrawn, the issue goes."""
    fake_cloud.devices = []
    entry = await set_up_warm(hass, seed_warm_cache)
    assert _invites_issue(hass, entry) is None
    button = entity_id_for(hass, "button", entry.entry_id, REFRESH_DEVICE_LIST_KEY)

    fake_cloud.house_invites = [dict(_HOUSE_INVITE)]
    await hass.services.async_call("button", "press", {ATTR_ENTITY_ID: button}, blocking=True)
    await hass.async_block_till_done()
    assert _invites_issue(hass, entry) is not None

    fake_cloud.house_invites = []
    await hass.services.async_call("button", "press", {ATTR_ENTITY_ID: button}, blocking=True)
    await hass.async_block_till_done()
    assert _invites_issue(hass, entry) is None
    await _unload(hass, entry)


async def _repairs(hass: HomeAssistant) -> RepairsFlowManager:
    assert await async_setup_component(hass, "repairs", {})
    manager = repairs_flow_manager(hass)
    assert manager is not None
    return manager


async def test_the_invitation_fix_rescans_and_sets_up_the_shared_station(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    discover_calls: list[dict[str, Any]],
) -> None:
    """Accepted in the eufy app, then confirmed: the reload asks every scope once, the
    station is served and the issue is gone."""
    listed = list(fake_cloud.devices)
    fake_cloud.devices = []
    fake_cloud.house_invites = [dict(_HOUSE_INVITE)]
    seed_warm_cache()
    entry = add_entry(hass)
    assert await setup_entry(hass, entry)
    manager = await _repairs(hass)
    issue_id = errors.pending_invites_issue_id(entry.entry_id)

    result = await manager.async_init(DOMAIN, data={"issue_id": issue_id})
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["pick", "confirm"]
    result = await manager.async_configure(result["flow_id"], {"next_step_id": "confirm"})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"
    placeholders = result["description_placeholders"]
    assert placeholders is not None
    assert placeholders["invites"] == f"“{_HOME_NAME}” from {_INVITER}"
    # The user accepts in the eufy app: the invitation leaves, the devices are listed.
    fake_cloud.house_invites = []
    fake_cloud.devices = listed
    result = await manager.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    assert discover_calls[-1] == {"rescan_regions": True}
    assert SYNTHETIC.station_sn in entry.runtime_data.eufy.stations
    assert _invites_issue(hass, entry) is None
    assert "login" not in fake_cloud.calls
    await _unload(hass, entry)


async def _press(hass: HomeAssistant, entry: ConfigEntry) -> None:
    button = entity_id_for(hass, "button", entry.entry_id, REFRESH_DEVICE_LIST_KEY)
    await hass.services.async_call("button", "press", {ATTR_ENTITY_ID: button}, blocking=True)
    await hass.async_block_till_done()


def _invite_reads(fake_cloud: FakeCloud) -> int:
    return len([call for call in cloud_calls(fake_cloud) if "invites" in call])


async def test_an_invitation_accepted_in_the_app_is_gone_after_the_press_that_lists_it(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Accepted in the eufy app, then Refresh device list: the press adds the station
    and reloads; the reload's setup reads the invitations, so the accepted one is not
    named again from the last read."""
    listed = list(fake_cloud.devices)
    fake_cloud.devices = []
    fake_cloud.house_invites = [dict(_HOUSE_INVITE)]
    # an account that lists nothing is asked on a press only with the region option
    entry = await set_up_warm(hass, seed_warm_cache, options={CONF_SCAN_REGIONS: True})
    assert _invites_issue(hass, entry) is not None

    fake_cloud.house_invites = []
    fake_cloud.devices = listed
    await _press(hass, entry)
    await wait_until(
        lambda: (
            entry.state.value == "loaded" and fake_station.serial in entry.runtime_data.coordinators
        )
    )
    await hass.async_block_till_done()

    assert _invites_issue(hass, entry) is None
    await _unload(hass, entry)


async def test_a_press_that_reloads_reads_the_invitations_once_at_the_reload(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A press whose fetch finds a new paired device (same stations, a reload pending)
    leaves the read to the reload's setup: one read in all, and the issue shows."""
    entry = await set_up_warm(hass, seed_warm_cache)
    first = len(built_clients)
    reads = _invite_reads(fake_cloud)
    add_motion_sensor(fake_station, fake_cloud)
    fake_cloud.house_invites = [dict(_HOUSE_INVITE)]
    button = entity_id_for(hass, "button", entry.entry_id, REFRESH_DEVICE_LIST_KEY)
    await hass.services.async_call("button", "press", {ATTR_ENTITY_ID: button}, blocking=True)
    await wait_until(lambda: len(built_clients) == first + 1 and entry.state.value == "loaded")
    await hass.async_block_till_done()

    # house and device invitations of eu and us, once: by the reload's setup only
    assert _invite_reads(fake_cloud) - reads == 4
    assert _invites_issue(hass, entry) is not None

    # the request is used up: a later warm reload reads nothing
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert _invite_reads(fake_cloud) - reads == 4
    assert _invites_issue(hass, entry) is not None
    await _unload(hass, entry)


async def test_a_failed_read_keeps_the_invitations_last_read(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A press whose invitation read fails leaves the issue, and a warm reload after
    it still names what the last readable answer listed."""
    entry = await set_up_warm(hass, seed_warm_cache)
    fake_cloud.house_invites = [dict(_HOUSE_INVITE)]
    await _press(hass, entry)
    assert _invites_issue(hass, entry) is not None

    async def refused(self: EufySecurity) -> None:
        raise CommunicationError("synthetic")

    monkeypatch.setattr(EufySecurity, "async_pending_invites", refused)
    await _press(hass, entry)
    assert _invites_issue(hass, entry) is not None
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    issue = _invites_issue(hass, entry)
    assert issue is not None
    assert issue.translation_placeholders is not None
    assert issue.translation_placeholders["invites"] == f"“{_HOME_NAME}” from {_INVITER}"
    await _unload(hass, entry)


async def test_a_requested_read_that_fails_names_no_earlier_invitation(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reload the user started drops the last read: when its own read fails, the
    issue is gone rather than naming an invitation that may be accepted by now."""
    entry = await set_up_warm(hass, seed_warm_cache)
    fake_cloud.house_invites = [dict(_HOUSE_INVITE)]
    await _press(hass, entry)
    assert _invites_issue(hass, entry) is not None

    async def refused(self: EufySecurity) -> None:
        raise CommunicationError("synthetic")

    monkeypatch.setattr(EufySecurity, "async_pending_invites", refused)
    runtime.request_invites_at_setup(hass, entry.entry_id)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert _invites_issue(hass, entry) is None
    await _unload(hass, entry)


async def test_finishing_the_fix_with_devices_reads_the_invitations_at_the_reload(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A station served and an invitation accepted in the eufy app, then confirmed in
    the fix: even when the reload's rescan falls back to the cached list, its setup
    reads the invitations and the accepted one is not named again."""
    entry = await set_up_warm(hass, seed_warm_cache)
    fake_cloud.house_invites = [dict(_HOUSE_INVITE)]
    await _press(hass, entry)
    manager = await _repairs(hass)
    issue_id = errors.pending_invites_issue_id(entry.entry_id)
    result = await manager.async_init(DOMAIN, data={"issue_id": issue_id})
    result = await manager.async_configure(result["flow_id"], {"next_step_id": "confirm"})
    assert result["step_id"] == "confirm"

    fake_cloud.house_invites = []
    fake_cloud.call_errors = [CommunicationError("synthetic")] * 2
    result = await manager.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    assert built_clients[-1].device_list_source != "fetched"
    assert _invites_issue(hass, entry) is None
    await _unload(hass, entry)


async def test_an_entry_unloaded_during_the_press_read_gets_no_issue(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The entry is unloaded while a press reads the invitations: the late answer
    raises no issue for an entry that is not loaded."""
    entry = await set_up_warm(hass, seed_warm_cache)
    entered = asyncio.Event()
    release = asyncio.Event()
    original = EufySecurity.async_pending_invites

    async def held(self: EufySecurity) -> list[Any]:
        entered.set()
        await release.wait()
        return await original(self)

    monkeypatch.setattr(EufySecurity, "async_pending_invites", held)
    fake_cloud.house_invites = [dict(_HOUSE_INVITE)]
    button = entity_id_for(hass, "button", entry.entry_id, REFRESH_DEVICE_LIST_KEY)
    entity = hass.data["entity_components"]["button"].get_entity(button)
    press = hass.async_create_task(entity.async_press())
    await entered.wait()
    unload = hass.async_create_task(hass.config_entries.async_unload(entry.entry_id))
    await wait_until(lambda: entry.state is not ConfigEntryState.LOADED)
    release.set()
    await press
    assert await unload
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED
    assert _invites_issue(hass, entry) is None


async def test_build_client_passes_the_country_and_home_assistants_time_zone(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one construction site hands the library the login country and the zone."""
    seen: dict[str, Any] = {}

    def recording(*args: Any, **kwargs: Any) -> object:
        del args
        seen.update(kwargs)
        return object()

    monkeypatch.setattr(runtime, "EufySecurity", recording)
    hass.config.time_zone = "Europe/Zurich"

    runtime.build_client(hass, SYNTHETIC.email, None, country="CH")

    assert seen["country"] == "CH"
    assert seen["timezone"] == "Europe/Zurich"
