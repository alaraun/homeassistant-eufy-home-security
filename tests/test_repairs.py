"""Repair issues for a degraded eufy cloud and a rejected station key, on the real library.

Every test sets the entry up through Home Assistant on ``eufy_home_security.testing``
(a real ``EufySecurity`` wired to ``FakeCloud`` and a loopback ``FakeStation``) and
drives the fix flows through Home Assistant's own repairs flow manager. The cloud
fake's ``calls`` list is the proof of what reached eufy: nothing may log in until a
user confirms a fix.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from collections.abc import Callable
from datetime import timedelta
from typing import Any, Final

import pytest
from conftest import (
    add_entry,
    advance_to_poll,
    cloud_calls,
    entity_id_for,
    panel_entity_id,
    record_states,
    set_up_warm,
    setup_entry,
    state_of,
    wait_until,
)
from eufy_home_security import (
    AuthenticationError,
    CipherUnavailableError,
    CloudProblem,
    CommunicationError,
    ConnectionChanged,
    CredentialsRefreshed,
    DisconnectCause,
    EufySecurity,
    KeyRejectedError,
    LoginLimitedError,
    SessionCache,
    SessionReplacedError,
    redact_serial,
)
from eufy_home_security.testing import SYNTHETIC, FakeCloud, FakeStation
from homeassistant.components.alarm_control_panel import DOMAIN as ALARM_DOMAIN
from homeassistant.components.event import DOMAIN as EVENT_DOMAIN
from homeassistant.components.repairs import RepairsFlowManager, repairs_flow_manager
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.eufy_home_security import detections, errors, runtime
from custom_components.eufy_home_security.const import (
    CONF_SCAN_REGIONS,
    DETECTION_EVENT_KEY,
    DOMAIN,
    POLL_INTERVAL_SECONDS,
    REFRESH_DEVICE_LIST_KEY,
    STORAGE_POLL_INTERVAL_SECONDS,
)

_CIPHER_CALL = f"cipher:{redact_serial(SYNTHETIC.station_sn)}"
# The fake cloud's records of the two pending-invitation reads.
_INVITE_CALLS: Final = ("house_invites", "device_invites")
_NOTICE_KEYS = ("credentials_refreshed", "credentials_refreshed_login")
# The owner account a mismatched station stamps on its records: synthetic, 40 hex
# characters, and never the account id the library sends.
_OTHER_ACCOUNT_ID: Final = "f" * 40
_MISMATCH_KEY: Final = "account_id_mismatch"
# The integration's and the library's own loggers.
_OUR_LOGGERS: Final = ("custom_components.eufy_home_security", "eufy_home_security")
# The library's one WARNING per connection when a station stamps another account.
_MISMATCH_WARNING: Final = "stamps its records with another account id"
# The issue registry's storage document.
_ISSUE_REGISTRY_KEY: Final = "repairs.issue_registry"


def _cache(hass: HomeAssistant, email: str = SYNTHETIC.email) -> SessionCache:
    """A view of the account's store, as the entry's client reads it."""
    return SessionCache(runtime.cache_store(hass, email), email)


async def _set_replaced_latch(hass: HomeAssistant, email: str = SYNTHETIC.email) -> None:
    """Record in the store that another client's login ended the cached session."""
    cache = _cache(hass, email)
    await cache.async_load()
    cache.set_replaced()
    await cache.async_save()


def _session_replaced_issue(hass: HomeAssistant, entry: MockConfigEntry) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, f"session_replaced_{entry.entry_id}")


async def _repairs(hass: HomeAssistant) -> RepairsFlowManager:
    assert await async_setup_component(hass, "repairs", {})
    manager = repairs_flow_manager(hass)
    assert manager is not None
    return manager


async def _set_up_replaced(
    hass: HomeAssistant, seed_warm_cache: Callable[..., None]
) -> MockConfigEntry:
    """A warm account whose cached session another client has taken, set up."""
    seed_warm_cache()
    await _set_replaced_latch(hass)
    entry = add_entry(hass)
    assert await setup_entry(hass, entry)
    return entry


async def _unload(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    if entry.state is ConfigEntryState.LOADED:
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_a_replaced_session_at_setup_raises_a_fixable_issue_and_local_control_keeps_working(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A kick-out does not take the panel down, and nothing logs in to take the session back."""
    entry = await _set_up_replaced(hass, seed_warm_cache)

    assert entry.state is ConfigEntryState.LOADED
    issue = _session_replaced_issue(hass, entry)
    assert issue is not None
    assert issue.is_fixable
    assert issue.data == {"entry_id": entry.entry_id}
    # Repair text names the account without its e-mail address.
    assert SYNTHETIC.email not in str(issue.translation_placeholders)

    entity_id = panel_entity_id(hass)
    assert state_of(hass, entity_id) == "armed_away"
    await hass.services.async_call(
        ALARM_DOMAIN, "alarm_arm_home", {ATTR_ENTITY_ID: entity_id}, blocking=True
    )
    assert fake_station.guard_mode == 1
    assert cloud_calls(fake_cloud) == []

    await _unload(hass, entry)


async def test_the_session_replaced_fix_logs_in_only_after_the_user_confirms(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Opening the fix spends nothing; confirming it spends exactly one login, then reloads."""
    entry = await _set_up_replaced(hass, seed_warm_cache)
    manager = await _repairs(hass)
    issue_id = f"session_replaced_{entry.entry_id}"

    result = await manager.async_init(DOMAIN, data={"issue_id": issue_id})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"
    assert cloud_calls(fake_cloud) == []

    result = await manager.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert cloud_calls(fake_cloud) == ["login"]
    assert _session_replaced_issue(hass, entry) is None

    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert cloud_calls(fake_cloud) == ["login"]  # the reload reused the new session
    cache = _cache(hass)
    await cache.async_load()
    assert cache.replaced_at is None

    await _unload(hass, entry)


async def test_a_fix_whose_login_fails_keeps_the_issue(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A failed take-over aborts, leaves the repair open and never tries a second time."""
    entry = await _set_up_replaced(hass, seed_warm_cache)
    manager = await _repairs(hass)

    result = await manager.async_init(
        DOMAIN, data={"issue_id": f"session_replaced_{entry.entry_id}"}
    )
    assert result["type"] is FlowResultType.FORM
    fake_cloud.login_error = AuthenticationError("rejected")

    result = await manager.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "login_failed"
    assert _session_replaced_issue(hass, entry) is not None
    assert fake_cloud.calls.count("login") == 1

    await _unload(hass, entry)


async def test_a_degraded_login_on_a_cold_cache_is_not_ready(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
) -> None:
    """Kicked out with no cached device list: only then is the entry not ready, still cloud-free."""
    cache = _cache(hass)
    await cache.async_load()
    cache.set_password(SYNTHETIC.password)
    cache.set_replaced()
    await cache.async_save()
    entry = add_entry(hass)

    assert not await setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert _session_replaced_issue(hass, entry) is not None
    assert cloud_calls(fake_cloud) == []


# ── a station whose key keeps being rejected ─────────────────────────────────


@pytest.fixture
def rejected_key(fake_cloud: FakeCloud, fake_station: FakeStation) -> None:
    """Make the cloud, and so the warm cache seeded after this, hold a key the station rejects.

    The key of an unstarted fake is a valid key of the wrong station, so both the
    cached key and the one a refresh fetches are rejected.
    """
    fake_cloud.cipher_keys[fake_station.serial] = FakeStation().ecc_private_key_hex


def _domain_issues(hass: HomeAssistant) -> dict[str, ir.IssueEntry]:
    return {
        issue_id: issue
        for (domain, issue_id), issue in ir.async_get(hass).issues.items()
        if domain == DOMAIN
    }


def _key_rejected_issues(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, ir.IssueEntry]:
    prefix = f"key_rejected_{entry.entry_id}_"
    return {i: issue for i, issue in _domain_issues(hass).items() if i.startswith(prefix)}


def _notices(hass: HomeAssistant) -> list[ir.IssueEntry]:
    return [i for i in _domain_issues(hass).values() if i.translation_key in _NOTICE_KEYS]


async def _run_scheduled_retry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Let Home Assistant's own setup retry run, never a second setup beside its timer.

    The first retry waits 5 s plus jitter (ConfigEntry.async_setup). The retried
    setup spends real time on the loopback handshake, which async_block_till_done
    does not wait for, so wait on the entry state first.
    """
    assert entry.state is ConfigEntryState.SETUP_RETRY
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))
    await wait_until(lambda: entry.state is not ConfigEntryState.SETUP_RETRY, timeout=10)
    await wait_until(lambda: entry.state is not ConfigEntryState.SETUP_IN_PROGRESS, timeout=10)
    await hass.async_block_till_done()


async def _load_with_rejected_key(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """The first setup (not ready: its only station never came up), then HA's scheduled retry."""
    assert not await setup_entry(hass, entry)
    await _run_scheduled_retry(hass, entry)
    assert entry.state is ConfigEntryState.LOADED


async def test_a_rejected_key_raises_one_issue_per_station_and_no_repeated_cloud_call(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    rejected_key: None,
) -> None:
    """One re-fetch, one fixable issue, one notice; the retry spends no second fetch."""
    seed_warm_cache()
    entry = add_entry(hass)

    assert not await setup_entry(hass, entry)
    first_state = entry.state
    assert first_state is ConfigEntryState.SETUP_RETRY  # first-ever, none came up
    assert len(_key_rejected_issues(hass, entry)) == 1
    (issue,) = _key_rejected_issues(hass, entry).values()
    assert issue.is_fixable
    notices = _notices(hass)
    assert len(notices) == 1
    assert notices[0].is_persistent
    assert not notices[0].is_fixable
    assert fake_cloud.calls.count(_CIPHER_CALL) == 1

    await _run_scheduled_retry(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert state_of(hass, panel_entity_id(hass)) == "unavailable"
    assert len(_key_rejected_issues(hass, entry)) == 1
    assert fake_cloud.calls.count(_CIPHER_CALL) == 1

    await _unload(hass, entry)


async def test_a_key_rejection_seen_only_in_last_error_raises_the_issue(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    rejected_key: None,
) -> None:
    """With an empty start result, the station's own last error still names the rejection."""
    seed_warm_cache()
    entry = add_entry(hass)
    await _load_with_rejected_key(hass, entry)
    stations = entry.runtime_data.eufy.stations
    assert isinstance(stations[SYNTHETIC.station_sn].last_error, KeyRejectedError)
    cipher_calls = fake_cloud.calls.count(_CIPHER_CALL)

    # No await from here on: nothing else can raise the issue in between.
    (issue_id,) = _key_rejected_issues(hass, entry)
    ir.async_delete_issue(hass, DOMAIN, issue_id)
    assert _key_rejected_issues(hass, entry) == {}
    errors.raise_key_rejected_issues(hass, entry, stations.values(), {})

    assert list(_key_rejected_issues(hass, entry)) == [issue_id]
    assert fake_cloud.calls.count(_CIPHER_CALL) == cipher_calls

    await _unload(hass, entry)


async def test_the_key_rejected_fix_releases_the_latch_and_reloads(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    rejected_key: None,
) -> None:
    """Confirming the fix releases the station's latch through the loaded client and reloads."""
    seed_warm_cache()
    entry = add_entry(hass)
    await _load_with_rejected_key(hass, entry)
    cache = _cache(hass)
    await cache.async_load()
    latched_at = cache.key_refresh_outstanding(SYNTHETIC.station_sn)
    assert latched_at is not None
    manager = await _repairs(hass)
    (issue_id,) = _key_rejected_issues(hass, entry)

    result = await manager.async_init(DOMAIN, data={"issue_id": issue_id})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"
    result = await manager.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    assert _key_rejected_issues(hass, entry) == {}
    assert "login" not in fake_cloud.calls
    cache = _cache(hass)
    await cache.async_load()
    released = cache.key_refresh_outstanding(SYNTHETIC.station_sn)
    assert released is None or released > latched_at

    await _unload(hass, entry)


async def test_a_reload_with_an_accepted_cached_key_clears_the_issue_and_the_latch(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    rejected_key: None,
) -> None:
    """A station that rejected its key and then accepts the cached one (a library fix
    for the station's key format) recovers by a reload: connected, the key issue gone,
    the latch cleared, and no key fetched."""
    seed_warm_cache()
    entry = add_entry(hass)
    await _load_with_rejected_key(hass, entry)
    assert len(_key_rejected_issues(hass, entry)) == 1
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert len(_key_rejected_issues(hass, entry)) == 1, "an unload keeps the issue"
    # The cached key is now one the station accepts, under a set latch.
    fake_cloud.cipher_keys[fake_station.serial] = fake_station.ecc_private_key_hex
    seed_warm_cache()
    cache = _cache(hass)
    await cache.async_load()
    cache.note_key_refresh(SYNTHETIC.station_sn)
    await cache.async_save()
    cipher_calls = fake_cloud.calls.count(_CIPHER_CALL)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.eufy.stations[SYNTHETIC.station_sn].connected
    assert _key_rejected_issues(hass, entry) == {}
    assert fake_cloud.calls.count(_CIPHER_CALL) == cipher_calls
    cache = _cache(hass)
    await cache.async_load()
    assert cache.key_refresh_outstanding(SYNTHETIC.station_sn) is None

    await _unload(hass, entry)


async def test_a_reconnect_clears_the_station_key_issue_but_keeps_the_notice(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """ConnectionChanged(True) withdraws key_rejected; the credentials notice stays."""
    seed_warm_cache()
    entry = add_entry(hass)
    assert await setup_entry(hass, entry)
    stations = entry.runtime_data.eufy.stations
    # A connected station's last error is not a rejection.
    errors.raise_key_rejected_issues(hass, entry, stations.values(), {})
    assert _key_rejected_issues(hass, entry) == {}
    router = entry.runtime_data.router

    router.handle(
        ConnectionChanged(
            station_sn=SYNTHETIC.station_sn,
            connected=False,
            cause=DisconnectCause.KEY_REJECTED,
            error=KeyRejectedError("x"),
        )
    )
    assert len(_key_rejected_issues(hass, entry)) == 1
    router.handle(
        CredentialsRefreshed(
            station_sn=SYNTHETIC.station_sn, cipher=True, owner_id=False, login=True
        )
    )
    router.handle(ConnectionChanged(station_sn=SYNTHETIC.station_sn, connected=True))
    await hass.async_block_till_done()

    assert _key_rejected_issues(hass, entry) == {}
    assert [n.translation_key for n in _notices(hass)] == ["credentials_refreshed_login"]
    assert cloud_calls(fake_cloud) == []

    await _unload(hass, entry)


async def test_no_repair_issue_names_a_serial_or_an_owner_id(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    rejected_key: None,
) -> None:
    """Issue ids, data, placeholders and the integration's log carry no serial or secret."""
    caplog.set_level(logging.DEBUG, logger="custom_components.eufy_home_security")
    seed_warm_cache()
    entry = add_entry(hass)
    await _load_with_rejected_key(hass, entry)
    router = entry.runtime_data.router
    router.handle(CloudProblem(error=SessionReplacedError(), station_sn=SYNTHETIC.station_sn))
    router.handle(
        CloudProblem(error=LoginLimitedError(retry_after=120), station_sn=SYNTHETIC.station_sn)
    )
    await hass.async_block_till_done()

    issues = _domain_issues(hass)
    kinds = {issue.translation_key for issue in issues.values()}
    assert {"key_rejected", "session_replaced", "login_limited"} <= kinds
    assert kinds & set(_NOTICE_KEYS)
    secrets = (SYNTHETIC.station_sn, SYNTHETIC.account_id, SYNTHETIC.email, SYNTHETIC.password)
    for issue_id, issue in issues.items():
        for text in (issue_id, str(issue.data), str(issue.translation_placeholders)):
            for secret in secrets:
                assert secret not in text, (issue_id, text)

    own_lines = [
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("custom_components.eufy_home_security")
    ]
    assert own_lines, "the integration logged nothing; the log check would pass vacuously"
    for line in own_lines:
        for secret in secrets:
            assert secret not in line, line

    await _unload(hass, entry)


# ── a station stamping another owner account ─────────────────────────────────


def _mismatch_issues(hass: HomeAssistant) -> dict[str, ir.IssueEntry]:
    return {
        i: issue
        for i, issue in _domain_issues(hass).items()
        if issue.translation_key == _MISMATCH_KEY
    }


def _station_device(hass: HomeAssistant, entry: MockConfigEntry) -> dr.DeviceEntry:
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, SYNTHETIC.station_sn), entry.entry_id
    )
    assert device is not None
    return device


async def _push_mismatched(hass: HomeAssistant, fake_station: FakeStation) -> None:
    """Stamp the fake's records with another account and push one detection.

    Only after setup: the fake refuses commands carrying an account id other than its
    own, so a fake changed before setup would fail it.
    """
    fake_station.account_id = _OTHER_ACCOUNT_ID
    fake_station.push_camera_event()
    await wait_until(lambda: len(_mismatch_issues(hass)) == 1, timeout=10)


async def test_an_account_mismatch_raises_one_account_id_mismatch_issue(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """One non-fixable ERROR issue named by the station, and the detection still fires."""
    entry = await set_up_warm(hass, seed_warm_cache)
    detection_id = entity_id_for(hass, EVENT_DOMAIN, SYNTHETIC.camera_sn, DETECTION_EVENT_KEY)
    assert state_of(hass, detection_id) == STATE_UNKNOWN

    await _push_mismatched(hass, fake_station)

    device = _station_device(hass, entry)
    ((issue_id, issue),) = _mismatch_issues(hass).items()
    assert issue_id == errors.account_id_mismatch_issue_id(entry.entry_id, device.id)
    assert issue.is_fixable is False
    assert issue.is_persistent is False
    assert issue.severity is ir.IssueSeverity.ERROR
    assert issue.data is None
    assert issue.translation_placeholders == {"station": device.name_by_user or device.name}
    assert SYNTHETIC.station_sn not in issue_id
    assert _OTHER_ACCOUNT_ID not in issue_id
    await wait_until(lambda: state_of(hass, detection_id) != STATE_UNKNOWN)

    await _unload(hass, entry)


def _no_snapshot(*_args: object) -> bool:
    """In place of ``detections.snapshot_wanted``: no detection asks for a still."""
    return False


def _mismatch_warnings(caplog: pytest.LogCaptureFixture) -> int:
    """How many connections the library reported a stamped-account mismatch on."""
    return sum(
        1
        for record in caplog.records
        if record.name.startswith("eufy_home_security") and _MISMATCH_WARNING in record.getMessage()
    )


async def test_the_mismatched_account_id_is_in_no_log_issue_or_storage(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The stamped id reaches no integration log record, no issue and no storage."""
    caplog.set_level(logging.DEBUG)
    entry = await set_up_warm(hass, seed_warm_cache)
    await _push_mismatched(hass, fake_station)
    ((issue_id, issue),) = _mismatch_issues(hass).items()

    ours = [record for record in caplog.records if record.name.startswith(_OUR_LOGGERS)]
    assert any(record.name.startswith("eufy_home_security") for record in ours), (
        "no library log record was captured, so the log check below is vacuous"
    )
    assert _mismatch_warnings(caplog) == 1
    leaking = [
        f"{record.name}: {record.getMessage()}"
        for record in ours
        if _OTHER_ACCOUNT_ID in record.getMessage()
    ]
    assert leaking == []

    issue_text = str(dataclasses.asdict(issue))
    assert _OTHER_ACCOUNT_ID not in issue_text
    assert SYNTHETIC.account_id not in issue_text

    # The registry saves 10 s after a change while Home Assistant runs, else 180 s.
    await advance_to_poll(hass, 11)
    if _ISSUE_REGISTRY_KEY not in hass_storage:
        await advance_to_poll(hass, 181)
    stored_ids = [i["issue_id"] for i in hass_storage[_ISSUE_REGISTRY_KEY]["data"]["issues"]]
    assert issue_id in stored_ids
    assert _OTHER_ACCOUNT_ID not in json.dumps(hass_storage, default=str)

    await _unload(hass, entry)


async def test_the_mismatch_issue_survives_a_reconnect_and_a_second_push(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """One issue across a repeated push and a reconnect, which does not clear it."""
    # The pushes are camera detections: keep the camera's stills out. A trigger frame's
    # short-lived session is a connection of its own, which would report the mismatch
    # once more and hide whether the reconnected session did.
    monkeypatch.setattr(detections, "snapshot_wanted", _no_snapshot)
    caplog.set_level(logging.WARNING, logger="eufy_home_security")
    entry = await set_up_warm(hass, seed_warm_cache)
    deduplicator = built_clients[-1].deduplicator
    assert deduplicator is not None
    await _push_mismatched(hass, fake_station)
    ((issue_id, _),) = _mismatch_issues(hass).items()

    # The same connection: the library reports once, and the copy is a duplicate.
    fake_station.push_camera_event()
    await wait_until(lambda: deduplicator.dropped_duplicates == 1)
    await hass.async_block_till_done()
    assert list(_mismatch_issues(hass)) == [issue_id]

    station = entry.runtime_data.coordinators[SYNTHETIC.station_sn].station
    fake_station.send_close()
    # A reconnect within the loss grace shows nothing on the panel; wait on the session.
    await wait_until(lambda: not station.connected, timeout=10)
    await wait_until(lambda: station.connected, timeout=30)
    await hass.async_block_till_done()
    assert list(_mismatch_issues(hass)) == [issue_id]

    # A new connection reports the mismatch again: proof the second emit happened.
    fake_station.push_camera_event()
    await wait_until(lambda: _mismatch_warnings(caplog) == 2, timeout=10)
    await hass.async_block_till_done()
    assert list(_mismatch_issues(hass)) == [issue_id]

    await _unload(hass, entry)


async def test_the_mismatch_issue_is_deleted_when_the_entry_unloads(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Unloading deletes the issue, and a reload re-evaluates the station."""
    entry = await set_up_warm(hass, seed_warm_cache)
    await _push_mismatched(hass, fake_station)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert _mismatch_issues(hass) == {}

    fake_station.account_id = SYNTHETIC.account_id
    assert await setup_entry(hass, entry)
    detection_id = entity_id_for(hass, EVENT_DOMAIN, SYNTHETIC.camera_sn, DETECTION_EVENT_KEY)
    detection_states = record_states(hass, detection_id)
    fake_station.push_camera_event()
    # The detection arrived on the new session, so its stamp was checked.
    await wait_until(
        lambda: any(s not in (STATE_UNAVAILABLE, STATE_UNKNOWN) for s in detection_states),
        timeout=10,
    )
    await hass.async_block_till_done()
    assert _mismatch_issues(hass) == {}

    await _unload(hass, entry)


async def test_a_mismatch_issue_left_by_a_failed_setup_is_deleted_by_the_next_setup(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Each setup attempt re-evaluates, so a stale issue does not outlive it."""
    entry = await set_up_warm(hass, seed_warm_cache)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    # As an attempt that raised the issue and then failed, so never unloaded, leaves it.
    errors.raise_account_mismatch_issue(hass, entry, SYNTHETIC.station_sn)
    assert len(_mismatch_issues(hass)) == 1

    assert await setup_entry(hass, entry)
    assert _mismatch_issues(hass) == {}

    await _unload(hass, entry)


async def test_removing_an_entry_that_never_loaded_deletes_its_mismatch_issue(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Removal deletes the issue even when unload never ran for it."""
    entry = await set_up_warm(hass, seed_warm_cache)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    errors.raise_account_mismatch_issue(hass, entry, SYNTHETIC.station_sn)
    assert len(_mismatch_issues(hass)) == 1

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert _mismatch_issues(hass) == {}


async def test_a_fix_whose_login_fails_leaves_the_latch_set_so_the_next_setup_never_signs_in(
    hass: HomeAssistant,
    built_clients,
    seed_warm_cache,
    fake_cloud,
    fake_station,
) -> None:
    """The latch stays set after a failed fix, so the reload signs nothing in."""
    seed_warm_cache()
    await _set_replaced_latch(hass)

    cache = _cache(hass)
    await cache.async_load()
    for key in ("auth_token", "key_ident", "shared_key", "expires_at"):
        cache.section("cloud").pop(key, None)
    await cache.async_save()

    entry = add_entry(hass)
    assert await setup_entry(hass, entry)

    manager = await _repairs(hass)
    result = await manager.async_init(
        DOMAIN, data={"issue_id": f"session_replaced_{entry.entry_id}"}
    )

    fake_cloud.login_error = CommunicationError("down")
    result = await manager.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "login_failed"
    assert fake_cloud.calls.count("login") == 1

    fresh_cache = _cache(hass)
    await fresh_cache.async_load()
    assert fresh_cache.replaced_at is not None

    assert _session_replaced_issue(hass, entry) is not None

    fake_cloud.login_error = None
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert fake_cloud.calls.count("login") == 1
    assert _session_replaced_issue(hass, entry) is not None
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.fixture
def missing_cipher(fake_cloud: FakeCloud) -> None:
    """Make the cloud, and so the warm cache seeded after this, hold no station key.

    Every ``get_ciphers`` then gets the cloud's empty answer, as for an owner that
    holds no key for the cipher the station names.
    """
    fake_cloud.cipher_keys.clear()


def _cipher_issues(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, ir.IssueEntry]:
    prefix = f"cipher_unavailable_{entry.entry_id}_"
    return {i: issue for i, issue in _domain_issues(hass).items() if i.startswith(prefix)}


async def test_a_station_key_the_cloud_lacks_raises_one_issue_and_no_fetch_storm(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    fake_cloud: FakeCloud,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    missing_cipher: None,
) -> None:
    """One non-fixable issue naming the station and cipher; polls ask the cloud nothing more."""
    seed_warm_cache()
    entry = add_entry(hass)

    assert not await setup_entry(hass, entry)
    first_state = entry.state
    assert first_state is ConfigEntryState.SETUP_RETRY  # first-ever, none came up
    (issue,) = _cipher_issues(hass, entry).values()
    assert not issue.is_fixable
    assert issue.severity is ir.IssueSeverity.ERROR
    assert issue.translation_key == "cipher_unavailable"
    placeholders = issue.translation_placeholders or {}
    assert placeholders["cipher"] == str(fake_station.cipher_id)
    assert placeholders["station"]
    assert fake_cloud.calls.count(_CIPHER_CALL) == 1
    assert f"Station {redact_serial(SYNTHETIC.station_sn)} did not come up" in caplog.text

    # The retry builds a new client, which asks once more, then holds off.
    await _run_scheduled_retry(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert state_of(hass, panel_entity_id(hass)) == STATE_UNAVAILABLE
    asked = fake_cloud.calls.count(_CIPHER_CALL)
    anchor = dt_util.utcnow()
    for poll in (1, 2):
        await advance_to_poll(hass, poll * (POLL_INTERVAL_SECONDS + 1), anchor=anchor)
    await advance_to_poll(hass, STORAGE_POLL_INTERVAL_SECONDS + 1, anchor=anchor)
    assert fake_cloud.calls.count(_CIPHER_CALL) == asked
    assert len(_cipher_issues(hass, entry)) == 1
    assert "login" not in fake_cloud.calls

    await _unload(hass, entry)
    assert _cipher_issues(hass, entry) == {}


async def test_a_cipher_cloud_problem_raises_the_issue_and_a_reconnect_clears_it(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The background CloudProblem route raises it; ConnectionChanged(True) withdraws it."""
    entry = await set_up_warm(hass, seed_warm_cache)
    router = entry.runtime_data.router
    error = CipherUnavailableError(
        "no key", cipher_id=98, owner_source="member.admin_user_id", retry_after=3600.0
    )

    router.handle(CloudProblem(error=error, station_sn=SYNTHETIC.station_sn))
    router.handle(CloudProblem(error=error, station_sn=SYNTHETIC.station_sn))
    (issue,) = _cipher_issues(hass, entry).values()
    assert (issue.translation_placeholders or {})["cipher"] == "98"
    secrets = (SYNTHETIC.station_sn, SYNTHETIC.account_id, SYNTHETIC.email)
    for text in (*_cipher_issues(hass, entry), str(issue.translation_placeholders)):
        assert not any(secret in text for secret in secrets)

    router.handle(ConnectionChanged(station_sn=SYNTHETIC.station_sn, connected=True))
    assert _cipher_issues(hass, entry) == {}
    assert cloud_calls(fake_cloud) == []

    await _unload(hass, entry)


async def test_a_session_lost_for_an_unavailable_cipher_raises_the_issue(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """ConnectionChanged(CREDENTIALS_UNAVAILABLE) carries no error; the station's own names it."""
    entry = await set_up_warm(hass, seed_warm_cache)
    station = entry.runtime_data.eufy.stations[SYNTHETIC.station_sn]
    error = CipherUnavailableError(
        "no key", cipher_id=155, owner_source="member.admin_user_id", retry_after=3600.0
    )
    monkeypatch.setattr(type(station), "last_error", property(lambda _self: error))

    entry.runtime_data.router.handle(
        ConnectionChanged(
            station_sn=SYNTHETIC.station_sn,
            connected=False,
            cause=DisconnectCause.CREDENTIALS_UNAVAILABLE,
        )
    )
    (issue,) = _cipher_issues(hass, entry).values()
    assert (issue.translation_placeholders or {})["cipher"] == "155"

    monkeypatch.undo()
    await _unload(hass, entry)


async def test_a_cipher_issue_left_by_a_failed_setup_is_deleted_by_the_next_setup(
    hass: HomeAssistant,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The library asks again after a reload, so a stale issue does not outlive the attempt."""
    entry = await set_up_warm(hass, seed_warm_cache)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    errors.raise_cipher_unavailable_issue(hass, entry, SYNTHETIC.station_sn, 40)
    assert len(_cipher_issues(hass, entry)) == 1

    assert await setup_entry(hass, entry)
    assert _cipher_issues(hass, entry) == {}

    await _unload(hass, entry)


def _no_devices_issue(hass: HomeAssistant, entry: MockConfigEntry) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, errors.no_devices_issue_id(entry.entry_id))


async def _press_refresh_device_list(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    button = entity_id_for(hass, "button", entry.entry_id, REFRESH_DEVICE_LIST_KEY)
    await hass.services.async_call("button", "press", {ATTR_ENTITY_ID: button}, blocking=True)
    await hass.async_block_till_done()


async def test_an_account_listing_no_devices_in_any_region_raises_the_no_devices_issue(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Every region suspended: one non-fixable issue naming the regions; a press asks
    for no device list.

    Without the region option a region that listed no devices is never asked again,
    so the default Refresh device list sends no device-list request, only the
    pending-invitation reads; unload deletes the issue.
    """
    fake_cloud.devices = []
    entry = await set_up_warm(hass, seed_warm_cache)

    issue = _no_devices_issue(hass, entry)
    assert issue is not None
    assert issue.is_fixable is False
    assert issue.translation_key == "no_devices"
    assert issue.translation_placeholders is not None
    assert issue.translation_placeholders["regions"] == "eu, us"
    assert set(issue.translation_placeholders) == {"account", "regions"}

    before = len(cloud_calls(fake_cloud))
    await _press_refresh_device_list(hass, entry)
    pressed = cloud_calls(fake_cloud)[before:]
    assert [call for call in pressed if not call.startswith(_INVITE_CALLS)] == []
    assert pressed, "the press read the pending invitations"

    await _unload(hass, entry)
    assert _no_devices_issue(hass, entry) is None


async def test_an_account_with_devices_has_no_no_devices_issue(
    hass: HomeAssistant,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """One region listing the station is enough: the other region's suspension is no issue."""
    entry = await set_up_warm(hass, seed_warm_cache)

    regions = (await entry.runtime_data.eufy.async_cloud_status()).regions
    assert regions["us"].suspended is True
    assert regions["eu"].suspended is False
    assert _no_devices_issue(hass, entry) is None

    await _unload(hass, entry)


async def test_with_the_region_option_a_press_finds_a_station_homed_on_the_other_region(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The option makes a press ask every region; the station found reloads the entry and
    the no-devices issue goes. The station keeps the region that listed it."""
    listed = list(fake_cloud.devices)
    fake_cloud.devices = []
    seed_warm_cache()
    entry = add_entry(hass, options={CONF_SCAN_REGIONS: True})
    assert await setup_entry(hass, entry)
    assert _no_devices_issue(hass, entry) is not None
    assert not entry.runtime_data.coordinators

    fake_cloud.region_devices = {"us": listed}
    before = len(built_clients)
    await _press_refresh_device_list(hass, entry)
    await wait_until(
        lambda: len(built_clients) == before + 1 and entry.state is ConfigEntryState.LOADED,
        timeout=15,
    )

    assert "devices@us" in fake_cloud.calls
    assert _no_devices_issue(hass, entry) is None
    station = entry.runtime_data.coordinators[SYNTHETIC.station_sn].station
    assert station.device.region == "us"

    await _unload(hass, entry)
