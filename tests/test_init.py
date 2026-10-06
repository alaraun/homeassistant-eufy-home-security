"""Adding an account and setting up its entry, end to end on the real library."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import logging
import re
from collections.abc import Callable
from typing import Any

import pytest
from conftest import (
    SENSOR_SN,
    add_entry,
    add_motion_sensor,
    alarm_changed,
    cloud_calls,
    set_up_warm,
    setup_entry,
    wait_until,
)
from eufy_home_security import (
    AuthenticationError,
    CloudProblem,
    EufySecurity,
    LoginLimitedError,
    RateLimitedError,
    RefreshCooldownError,
    SessionCache,
    SessionReplacedError,
    entity_unique_id,
)
from eufy_home_security.testing import (
    SYNTHETIC,
    FakeCloud,
    FakeStation,
    camera_device,
    station_device,
)
from homeassistant.components.alarm_control_panel import DOMAIN as ALARM_DOMAIN
from homeassistant.components.update import DOMAIN as UPDATE_DOMAIN
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    SOURCE_USER,
    ConfigEntry,
    ConfigEntryState,
)
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.eufy_home_security import async_unload_entry, runtime
from custom_components.eufy_home_security.config_flow import EufyHomeSecurityConfigFlow
from custom_components.eufy_home_security.const import (
    DOMAIN,
    EXC_AUTH_FAILED,
    GUARD_MODE_KEY,
)


def _panel_state(hass: HomeAssistant) -> str | None:
    """The state of the station's guard-mode panel, or None if it has none."""
    entity_id = er.async_get(hass).async_get_entity_id(
        ALARM_DOMAIN, DOMAIN, entity_unique_id(SYNTHETIC.station_sn, GUARD_MODE_KEY)
    )
    if entity_id is None:
        return None
    state = hass.states.get(entity_id)
    return state.state if state is not None else None


async def test_adding_the_account_shows_the_station_guard_mode(
    hass: HomeAssistant, fake_cloud: FakeCloud, built_clients: list[EufySecurity]
) -> None:
    """A cold add creates one account entry; setup reuses the flow's login and device list."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM

    typed_email = f"  {SYNTHETIC.email.upper()}  "
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: typed_email, CONF_PASSWORD: SYNTHETIC.password}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_EMAIL: SYNTHETIC.email}
    entry = result["result"]
    assert entry.unique_id == SYNTHETIC.email
    assert entry.state is ConfigEntryState.LOADED

    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, SYNTHETIC.station_sn), entry.entry_id
    )
    assert device is not None

    assert _panel_state(hass) == "armed_away"

    # One login and one device list for the whole add: the flow's, reused by setup.
    assert fake_cloud.calls.count("login") == 1
    assert fake_cloud.calls.count("devices") == 1
    assert len(built_clients) == 2

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_adding_the_same_account_again_aborts(
    hass: HomeAssistant, fake_cloud: FakeCloud, built_clients: list[EufySecurity]
) -> None:
    """A second add of an account already set up stops before any login."""
    add_entry(hass)

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_EMAIL: f" {SYNTHETIC.email.upper()} ", CONF_PASSWORD: SYNTHETIC.password},
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert built_clients == []
    assert cloud_calls(fake_cloud) == []
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_a_warm_start_makes_no_cloud_call(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A restart on the account's cached session reaches the station with no cloud request."""
    seed_warm_cache()
    entry = add_entry(hass)

    assert await setup_entry(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert _panel_state(hass) == "armed_away"
    assert cloud_calls(fake_cloud) == []

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def _add_account_through_the_flow(hass: HomeAssistant) -> str:
    """Run the user step on a cold store; returns the created entry's id."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: SYNTHETIC.email, CONF_PASSWORD: SYNTHETIC.password}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry_id: str = result["result"].entry_id
    return entry_id


def _assert_setup_handed_off_to_reauth(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Setup raised the translated ConfigEntryAuthFailed, and HA started reauth from it.

    SETUP_ERROR alone cannot tell ConfigEntryAuthFailed from a raw exception
    (config_entries.py sets it for both); the translation key HA keeps on the entry
    can. HA starts the flow through ``async_start_reauth_if_available``,
    which starts nothing for a config flow without
    ``async_step_reauth``. The flow defines that step, so this demands exactly one
    reauth flow for the entry unconditionally: a flow class that lost the step fails
    here instead of passing with zero flows.
    """
    assert hasattr(EufyHomeSecurityConfigFlow, "async_step_reauth")
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert entry.error_reason_translation_key == EXC_AUTH_FAILED
    reauth_flows = [
        flow
        for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        if flow["context"].get("source") == SOURCE_REAUTH
        and flow["context"].get("entry_id") == entry.entry_id
    ]
    assert len(reauth_flows) == 1


async def test_station_and_its_sub_devices_are_devices_under_the_station(
    hass: HomeAssistant,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The station and its camera are devices by their own serials; the camera hangs off it."""
    seed_warm_cache()
    entry = add_entry(hass)
    assert await setup_entry(hass, entry)

    registry = dr.async_get(hass)
    station = registry.async_get_device_by_identifier(
        (DOMAIN, SYNTHETIC.station_sn), entry.entry_id
    )
    camera = registry.async_get_device_by_identifier((DOMAIN, SYNTHETIC.camera_sn), entry.entry_id)
    assert station is not None
    assert camera is not None
    assert station.identifiers == {(DOMAIN, SYNTHETIC.station_sn)}
    assert camera.identifiers == {(DOMAIN, SYNTHETIC.camera_sn)}
    assert camera.via_device_id == station.id
    assert station.via_device_id is None
    assert entry.entry_id in station.config_entries
    assert entry.entry_id in camera.config_entries
    assert camera.serial_number == SYNTHETIC.camera_sn
    # The model from the library's catalogue, for the station and its camera alike.
    assert (station.manufacturer, station.model, station.model_id) == (
        "eufy",
        "HomeBase 3 (S380)",
        "T8030",
    )
    assert (camera.manufacturer, camera.model, camera.model_id) == (
        "eufy",
        "eufyCam 3 (S330)",
        "T8160",
    )

    panel = er.async_get(hass).async_get_entity_id(
        ALARM_DOMAIN, DOMAIN, entity_unique_id(SYNTHETIC.station_sn, GUARD_MODE_KEY)
    )
    assert panel is not None
    panel_entry = er.async_get(hass).async_get(panel)
    assert panel_entry is not None
    assert panel_entry.device_id == station.id

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_station_with_no_sub_devices_registers_only_itself(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Nothing paired: one device, its panel, and no error."""
    fake_cloud.devices = [station_device(fake_station.serial, did=str(fake_station.did))]
    seed_warm_cache()
    entry = add_entry(hass)

    with caplog.at_level(logging.WARNING):
        assert await setup_entry(hass, entry)

    # Station and paired-device rows only: the account's service device (the Refresh
    # device list button's) has an entry_type and is not a device.
    devices = [
        d
        for d in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
        if d.entry_type is None
    ]
    assert [d.identifiers for d in devices] == [{(DOMAIN, SYNTHETIC.station_sn)}]
    assert _panel_state(hass) == "armed_away"
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR] == []

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_entry_identity_is_the_account_and_ids_are_device_serials(
    hass: HomeAssistant,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The entry is the account; every device and entity id is a device's own serial."""
    seed_warm_cache()
    entry = add_entry(hass)
    assert await setup_entry(hass, entry)

    eufy = entry.runtime_data.eufy
    assert entry.unique_id == eufy.cache.account == SYNTHETIC.email

    serials = set()
    for station in eufy.stations.values():
        serials.add(station.device.device_sn)
        serials.update(sub.device_sn for sub in station.sub_devices)
    assert {SYNTHETIC.station_sn, SYNTHETIC.camera_sn} <= serials

    # Station and paired-device rows only: the account's service device is keyed by
    # the entry id, never the e-mail.
    devices = [
        d
        for d in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
        if d.entry_type is None
    ]
    identifiers = [ident for device in devices for ident in device.identifiers]
    assert len(devices) == 2
    assert all(domain == DOMAIN and value in serials for domain, value in identifiers)

    # Every entity but the account's Refresh device list button, keyed by entry id.
    account_button = f"{entry.entry_id}_refresh_device_list"
    unique_ids = [
        e.unique_id
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if e.unique_id != account_button
    ]
    assert unique_ids
    assert all(any(uid.startswith(f"{s}_") for s in serials) for uid in unique_ids)

    # No id is (station, channel): the station serial joined to a digit-only suffix.
    channel_id = re.compile(re.escape(SYNTHETIC.station_sn) + r"[_\-:.]\d+(?![A-Za-z_])")
    assert [v for _, v in identifiers if channel_id.search(v)] == []
    assert [uid for uid in unique_ids if channel_id.search(uid)] == []
    # The detector is not vacuous.
    assert channel_id.search(f"{SYNTHETIC.station_sn}_0")
    assert not channel_id.search(f"{SYNTHETIC.station_sn}_{GUARD_MODE_KEY}")

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_restarts_on_a_warm_cache_make_no_cloud_call(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Two reloads in a row, each LOADED, none reaching the cloud."""
    seed_warm_cache()
    entry = add_entry(hass)
    assert await setup_entry(hass, entry)
    assert entry.state is ConfigEntryState.LOADED

    for _ in range(2):
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED

    assert cloud_calls(fake_cloud) == []

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_cold_add_logs_in_once_across_setup_and_restarts(
    hass: HomeAssistant, fake_cloud: FakeCloud, built_clients: list[EufySecurity]
) -> None:
    """The flow's sign-in is the only one: its setup and two restarts reuse it."""
    entry_id = await _add_account_through_the_flow(hass)
    assert fake_cloud.calls.count("login") == 1
    assert fake_cloud.calls.count("devices") == 1

    for _ in range(2):
        assert await hass.config_entries.async_reload(entry_id)
        await hass.async_block_till_done()

    assert fake_cloud.calls.count("login") == 1
    assert fake_cloud.calls.count("devices") == 1
    assert len(built_clients) == 4  # the flow's, the setup's and one per restart

    assert await hass.config_entries.async_unload(entry_id)
    await hass.async_block_till_done()


async def test_setup_without_a_cached_password_starts_reauth_without_a_cloud_call(
    hass: HomeAssistant, fake_cloud: FakeCloud, built_clients: list[EufySecurity]
) -> None:
    """No account document at all: reauth, and not a single request to eufy."""
    entry = add_entry(hass)

    assert not await setup_entry(hass, entry)

    _assert_setup_handed_off_to_reauth(hass, entry)
    assert cloud_calls(fake_cloud) == []


async def test_setup_with_a_rejected_cached_password_starts_reauth(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """eufy rejects the cached password: exactly one sign-in attempt, then reauth."""
    seed_warm_cache()
    cache = SessionCache(runtime.cache_store(hass, SYNTHETIC.email), SYNTHETIC.email)
    await cache.async_load()
    assert cache.password is not None  # the rejection is of a password that was cached
    cache.section("cloud").clear()  # the session is gone, so a login is due
    await cache.async_save()
    fake_cloud.login_error = AuthenticationError("rejected")
    entry = add_entry(hass)

    assert not await setup_entry(hass, entry)

    _assert_setup_handed_off_to_reauth(hass, entry)
    assert cloud_calls(fake_cloud) == ["login"]
    # The library dropped the rejected password, so nothing can sign in with it again.
    cache = SessionCache(runtime.cache_store(hass, SYNTHETIC.email), SYNTHETIC.email)
    await cache.async_load()
    assert cache.password is None


def _login_limited_issue(hass: HomeAssistant, entry: MockConfigEntry) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, f"login_limited_{entry.entry_id}")


async def _set_up_with_login_error(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    seed_warm_cache: Callable[..., None],
    error: Exception,
) -> MockConfigEntry:
    """A warm account whose cloud session expired, set up while eufy answers the login with ``error``."""
    seed_warm_cache()
    cache = SessionCache(runtime.cache_store(hass, SYNTHETIC.email), SYNTHETIC.email)
    await cache.async_load()
    cache.section("cloud").clear()  # the session is gone, so a login is due
    await cache.async_save()
    fake_cloud.login_error = error  # type: ignore[assignment]
    entry = add_entry(hass)
    assert await setup_entry(hass, entry)
    return entry


@pytest.mark.parametrize(
    "error",
    [
        LoginLimitedError(retry_after=3600),
        # A plain request hold-off degrades exactly like a login limit.
        RateLimitedError("throttled", retry_after=3600.0, code=26145),
    ],
    ids=["login_limited", "rate_limited"],
)
async def test_a_login_limit_at_setup_raises_the_limited_issue_and_local_control_keeps_working(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    error: RateLimitedError,
) -> None:
    """A throttled login shows the wait as a repair and the panel keeps working from the cache."""
    entry = await _set_up_with_login_error(hass, fake_cloud, seed_warm_cache, error)

    assert entry.state is ConfigEntryState.LOADED
    issue = _login_limited_issue(hass, entry)
    assert issue is not None
    assert not issue.is_fixable
    assert issue.translation_placeholders is not None
    assert issue.translation_placeholders["minutes"] == "60"
    assert _panel_state(hass) == "armed_away"
    assert cloud_calls(fake_cloud) == ["login"]

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_the_limited_issue_clears_on_the_next_successful_login(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Once eufy lets the account sign in again, the next setup's login withdraws the issue."""
    entry = await _set_up_with_login_error(
        hass, fake_cloud, seed_warm_cache, LoginLimitedError(retry_after=1)
    )
    assert _login_limited_issue(hass, entry) is not None

    fake_cloud.login_error = None
    await asyncio.sleep(1.2)  # the library's hold-off runs on wall time
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert _login_limited_issue(hass, entry) is None
    # Two logins in all, well inside the library's own login budget.
    assert fake_cloud.calls.count("login") == 2

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.parametrize("held_off", [True, False], ids=["hold_off_running", "hold_off_over"])
async def test_a_cache_only_login_keeps_the_limited_issue_while_the_hold_off_runs(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    held_off: bool,
) -> None:
    """A warm-cache setup signs nobody in, so it proves nothing about the limit.

    The issue raised at runtime stays across a reload while the library still holds
    logins off; once the library holds nothing against a login, the same cache-only
    reload withdraws it. Neither setup reaches eufy.
    """
    seed_warm_cache()
    if held_off:
        cache = SessionCache(runtime.cache_store(hass, SYNTHETIC.email), SYNTHETIC.email)
        await cache.async_load()
        cache.hold_off("login", 3600)
        await cache.async_save()
    entry = add_entry(hass)
    assert await setup_entry(hass, entry)
    entry.runtime_data.router.handle(
        CloudProblem(error=LoginLimitedError(retry_after=3600), station_sn=SYNTHETIC.station_sn)
    )
    await hass.async_block_till_done()
    assert _login_limited_issue(hass, entry) is not None

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert (_login_limited_issue(hass, entry) is not None) is held_off
    assert cloud_calls(fake_cloud) == []

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    ("error", "issue_key", "minutes"),
    [
        (AuthenticationError("x"), None, None),
        (SessionReplacedError(), "session_replaced", None),
        (LoginLimitedError(retry_after=120), "login_limited", "2"),
        (RateLimitedError("t", retry_after=120.0, code=26145), "login_limited", "2"),
        (RefreshCooldownError("c", retry_after=60.0), None, None),
    ],
    ids=["authentication", "session_replaced", "login_limited", "rate_limited", "cooldown"],
)
async def test_background_cloud_problems_route_to_reauth_or_issues(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    error: Exception,
    issue_key: str | None,
    minutes: str | None,
) -> None:
    """A CloudProblem lands in reauth or the right repair, and never reaches eufy.

    The router is handed library event values directly; the entry itself runs on
    the library fakes, so this is not a replacement of the library.
    """
    reauth_requests: list[str] = []
    start_reauth = ConfigEntry.async_start_reauth_if_available

    def _record_reauth(
        self: ConfigEntry[Any], hass: HomeAssistant, *args: Any, **kwargs: Any
    ) -> None:
        reauth_requests.append(self.entry_id)
        start_reauth(self, hass, *args, **kwargs)

    seed_warm_cache()
    entry = add_entry(hass)
    assert await setup_entry(hass, entry)
    monkeypatch.setattr(ConfigEntry, "async_start_reauth_if_available", _record_reauth)

    entry.runtime_data.router.handle(CloudProblem(error=error, station_sn=SYNTHETIC.station_sn))  # type: ignore[arg-type]
    await hass.async_block_till_done()

    issues = {
        issue_id: issue
        for (domain, issue_id), issue in ir.async_get(hass).issues.items()
        if domain == DOMAIN
    }
    reauth_flows = [
        flow
        for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        if flow["context"].get("source") == SOURCE_REAUTH
    ]
    if isinstance(error, AuthenticationError):
        assert reauth_requests == [entry.entry_id]
        # The flow defines the reauth step, so exactly one flow, for this entry, is
        # demanded unconditionally: a flow class without the step starts none.
        assert hasattr(EufyHomeSecurityConfigFlow, "async_step_reauth")
        assert [flow["context"].get("entry_id") for flow in reauth_flows] == [entry.entry_id]
        assert issues == {}
    elif issue_key is None:
        assert reauth_requests == []
        assert reauth_flows == []
        assert issues == {}
    else:
        assert reauth_requests == []
        assert reauth_flows == []
        assert list(issues) == [f"{issue_key}_{entry.entry_id}"]
        issue = issues[f"{issue_key}_{entry.entry_id}"]
        assert issue.is_fixable is (issue_key == "session_replaced")
        if minutes is not None:
            assert issue.translation_placeholders is not None
            assert issue.translation_placeholders["minutes"] == minutes
    assert cloud_calls(fake_cloud) == []

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


# ── entry removal ─────────────────────────────────────────────────────────


async def test_removing_the_entry_forgets_secrets_but_keeps_the_hold_off_and_install_identity(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Remove-and-re-add must not reset eufy's sign-in hold-off."""
    seed_warm_cache()
    key = runtime.store_key(SYNTHETIC.email)
    before = copy.deepcopy(hass_storage[key]["data"])
    assert {"throttle", "openudid", "password", "cloud", "devices", "stations"} <= set(before)
    entry = add_entry(hass)
    assert await setup_entry(hass, entry)

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    after = hass_storage[key]["data"]
    assert {"throttle", "openudid"} <= set(after)
    assert after["openudid"] == before["openudid"]
    assert not {"password", "cloud", "devices", "stations"} & set(after)


# ── a changed device list rebuilds the entry's entities ───────


def _sensor_device(hass: HomeAssistant, entry: MockConfigEntry) -> dr.DeviceEntry | None:
    """The motion sensor's device row, found by its own serial."""
    return dr.async_get(hass).async_get_device_by_identifier((DOMAIN, SENSOR_SN), entry.entry_id)


async def test_a_device_list_change_reloads_the_entry_and_registers_the_new_device(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A device the account gained reaches Home Assistant as a device of its own.

    Entities are keyed by serial, so the entry is rebuilt rather than patched in
    place: a reload builds them from the new list.

    The refresh is the test's own call. Nothing in the integration schedules one,
    because a device-list refresh is a cloud call and eufy locks the account for 24 h
    after repeated sign-ins; what is proven here is the routing, not a trigger.
    """
    entry = await set_up_warm(hass, seed_warm_cache)
    before = len(built_clients)
    # Not vacuous: the sensor is absent until the account gains it.
    assert _sensor_device(hass, entry) is None

    add_motion_sensor(fake_station, fake_cloud)
    await entry.runtime_data.eufy.async_discover(refresh=True)
    await hass.async_block_till_done()
    await wait_until(
        lambda: len(built_clients) == before + 1 and entry.state is ConfigEntryState.LOADED,
        timeout=15,
    )

    assert _sensor_device(hass, entry) is not None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_device_list_refresh_that_changes_nothing_does_not_reload(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A refresh that finds the same devices emits nothing, so nothing is rebuilt.

    The library only reports a change, so a reload storm needs a list that keeps
    changing, not a refresh that keeps happening.
    """
    entry = await set_up_warm(hass, seed_warm_cache)
    before = len(built_clients)

    await entry.runtime_data.eufy.async_discover(refresh=True)
    await hass.async_block_till_done()
    # A reload is a task, so give one time to appear before saying none did.
    await asyncio.sleep(0.3)
    await hass.async_block_till_done()

    assert len(built_clients) == before
    assert entry.state is ConfigEntryState.LOADED

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


def _update_entities_by_device_serial(
    hass: HomeAssistant, entry: MockConfigEntry
) -> dict[str, str]:
    """Each device's serial mapped to the update entity registered against that device.

    Resolved through the device registry rather than through the entity's unique
    id, so the mapping asserted is the one a user sees on the device page. A second
    update entity for one device fails here rather than being folded into the map.
    """
    devices = dr.async_get(hass)
    found: dict[str, str] = {}
    for entity in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id):
        if entity.domain != UPDATE_DOMAIN:
            continue
        assert entity.device_id is not None, entity.entity_id
        device = devices.async_get(entity.device_id)
        assert isinstance(device, dr.DeviceEntry), entity.entity_id
        assert device.serial_number is not None, entity.entity_id
        assert device.serial_number not in found, f"two update entities on {entity.device_id}"
        found[device.serial_number] = entity.entity_id
    return found


async def test_a_reordered_cloud_device_list_keeps_every_entity_on_its_own_device(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The cloud listing the same devices in another order changes nothing.

    eufy's device list carries no order guarantee, and an entity keyed by position
    rather than by serial would change devices when the order changes.

    The refresh is the test's own call, as in the two tests above. Nothing in the
    integration refreshes the device list, because that is a cloud call and eufy
    locks the account for 24 h after repeated sign-ins.
    """
    add_motion_sensor(fake_station, fake_cloud)
    entry = await set_up_warm(hass, seed_warm_cache)

    before = _update_entities_by_device_serial(hass, entry)
    assert set(before) == {SYNTHETIC.station_sn, SYNTHETIC.camera_sn, SENSOR_SN}

    # The same devices and the same channels, listed the other way round.
    fake_cloud.devices = list(reversed(fake_cloud.devices))
    await entry.runtime_data.eufy.async_discover(refresh=True)
    await hass.async_block_till_done()
    # Not vacuous: the paired list really did come back reversed. Nothing was
    # added, removed or moved, so the library reports no change and schedules no
    # reload of its own.
    station = entry.runtime_data.coordinators[SYNTHETIC.station_sn].station
    assert [sub.device_sn for sub in station.sub_devices] == [SENSOR_SN, SYNTHETIC.camera_sn]

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    # The reload built its entities from the reordered list, not from a remembered
    # one: the refresh above wrote that list to the account cache.
    reloaded = entry.runtime_data.coordinators[SYNTHETIC.station_sn].station
    assert [sub.device_sn for sub in reloaded.sub_devices] == [SENSOR_SN, SYNTHETIC.camera_sn]

    # And every device still has exactly one update entity, the same one as before:
    # the ids are keyed by serial, so where a device sits in the list is immaterial.
    assert _update_entities_by_device_serial(hass, entry) == before

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_failed_platform_unload_leaves_the_entities_consuming_events(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """An unload that fails keeps the entities loaded, so events still reach them."""
    entry = await set_up_warm(hass, seed_warm_cache)
    real_unload_platforms = hass.config_entries.async_unload_platforms

    async def _refuse(*_: Any) -> bool:
        return False

    monkeypatch.setattr(hass.config_entries, "async_unload_platforms", _refuse)
    assert await async_unload_entry(hass, entry) is False
    monkeypatch.setattr(hass.config_entries, "async_unload_platforms", real_unload_platforms)

    entry.runtime_data.router.handle(alarm_changed())
    await hass.async_block_till_done()
    assert _panel_state(hass) == "triggered"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_latched_store_shows_the_session_replaced_issue_whatever_the_login_did(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A latched store shows the session_replaced issue whatever the login did."""
    seed_warm_cache()
    cache = SessionCache(runtime.cache_store(hass, SYNTHETIC.email), SYNTHETIC.email)
    await cache.async_load()
    cache.set_replaced()
    await cache.async_save()

    build = runtime.build_client

    async def _no_login(*args: Any, **kwargs: Any) -> None:
        return None

    def _mock_build_client(*args: Any, **kwargs: Any) -> EufySecurity:
        client = build(*args, **kwargs)
        monkeypatch.setattr(client, "async_login", _no_login)
        return client

    monkeypatch.setattr(runtime, "build_client", _mock_build_client)

    entry = add_entry(hass)
    assert await setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"session_replaced_{entry.entry_id}")
    assert issue is not None
    assert issue.is_fixable
    assert issue.data == {"entry_id": entry.entry_id}
    assert SYNTHETIC.email not in str(issue.translation_placeholders)

    assert cloud_calls(fake_cloud) == []

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_camera_of_an_uncatalogued_model_gets_its_model_id_and_no_name(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """model_id is the serial prefix even when the library does not know the model."""
    unknown = "T8999P2000011111"
    device = camera_device(unknown, station_sn=fake_station.serial, channel=1, name="Back")
    device["device_type"] = 999
    fake_cloud.devices.append(device)
    entry = await set_up_warm(hass, seed_warm_cache)

    camera = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, unknown), entry.entry_id)
    assert camera is not None
    assert (camera.manufacturer, camera.model, camera.model_id) == ("eufy", None, "T8999")

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_stop_during_setup_removes_the_stop_listener_without_an_error(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A setup cancelled after HA's stop event does not remove the fired listener again."""
    seed_warm_cache()
    entry = add_entry(hass)
    build = runtime.build_client  # the conftest replacement
    held = asyncio.Event()
    closed = asyncio.Event()

    def held_build(*args: Any, **kwargs: Any) -> EufySecurity:
        eufy = build(*args, **kwargs)
        real_close = eufy.async_close

        async def held_start(*_: Any, **__: Any) -> Any:
            held.set()
            await asyncio.Event().wait()

        async def noted_close() -> None:
            await real_close()
            closed.set()

        eufy.async_start = held_start  # type: ignore[method-assign]
        eufy.async_close = noted_close  # type: ignore[method-assign]
        return eufy

    monkeypatch.setattr(runtime, "build_client", held_build)
    setup = hass.async_create_task(hass.config_entries.async_setup(entry.entry_id))
    await wait_until(held.is_set)

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await wait_until(closed.is_set)
    setup.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await setup

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert "Unable to remove unknown job listener" not in caplog.text
