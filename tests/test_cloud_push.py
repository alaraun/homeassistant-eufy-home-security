"""The cloud push option: its background start, its repair issue and its unload.

The library does not fake push, so each built client's ``async_start`` is wrapped:
a local start runs the real one against the fake station, and a push start is only
recorded, with ``push_running`` and ``push_error`` answered from the test's state.
``subscribe`` is mirrored so a test can emit a ``PushChanged`` the way the client's
bus does: to the subscriptions still active, which is what the client's close emits.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from conftest import (
    PUSHED_THUMB_PATH,
    PUSHED_THUMBNAIL,
    SYNTHETIC,
    add_entry,
    set_guard_mode,
    set_up_warm,
    setup_entry,
    wait_until,
)
from eufy_home_security import (
    CommunicationError,
    EufySecurity,
    EufySecurityError,
    LoginLimitedError,
    PushChanged,
)
from eufy_home_security.p2p import session as session_module
from eufy_home_security.testing import FakeStation
from homeassistant.components.alarm_control_panel import DOMAIN as ALARM_DOMAIN
from homeassistant.components.repairs import repairs_flow_manager
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.eufy_home_security import diagnostics, errors, runtime
from custom_components.eufy_home_security.const import (
    CONF_CLOUD_PUSH,
    CONF_SESSION_PROBE,
    DOMAIN,
    ISSUE_PUSH_NEEDED,
    ISSUE_PUSH_NOT_RUNNING,
)

# A HomeBase 2 with the synthetic station's tail, so its key derivation is the fake's.
HOMEBASE_2_SN = "T8010" + SYNTHETIC.station_sn[5:]


@pytest.fixture
def short_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fake HomeBase 2 answers no storage query: give up after a tenth of a second."""
    monkeypatch.setattr(session_module, "STORAGE_TIMEOUT", 0.1)
    monkeypatch.setattr(session_module, "SD_INFO_TIMEOUT", 0.1)


@pytest.fixture
async def fake_station(request: pytest.FixtureRequest) -> AsyncIterator[FakeStation]:
    """The conftest station, with the serial a test passes indirectly (default HomeBase 3)."""
    station = FakeStation(serial=getattr(request, "param", SYNTHETIC.station_sn))
    set_guard_mode(station, station.guard_mode)
    station.images[PUSHED_THUMB_PATH] = PUSHED_THUMBNAIL
    await station.start()
    yield station
    station.stop()


@dataclass
class PushHarness:
    """What the wrapped clients record, and the push state they report."""

    # (p2p, push, a panel entity existed, the calling task's name) per start.
    starts: list[tuple[bool, bool, bool, str]] = field(default_factory=list)
    running_after_start: bool = True
    error_after_start: EufySecurityError | None = None
    running: bool = False
    error: EufySecurityError | None = None
    # The client's subscriptions still active, mirrored.
    subscribers: list[Callable[[Any], None]] = field(default_factory=list)
    # When set, a push start waits on it: a start that takes its whole deadline.
    gate: asyncio.Event | None = None
    # "close", and "push start cancelled", in the order they happened.
    order: list[str] = field(default_factory=list)

    def emit(self, event: Any) -> None:
        """Deliver ``event`` as the client's bus does: to every active subscription."""
        for callback in list(self.subscribers):
            callback(event)


@pytest.fixture
def push(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    built_clients: list[EufySecurity],
) -> PushHarness:
    harness = PushHarness()
    real_start = EufySecurity.async_start
    real_subscribe = EufySecurity.subscribe
    real_close = EufySecurity.async_close

    async def start(self: EufySecurity, *, p2p: bool = True, push: bool = True) -> Any:
        task = asyncio.current_task()
        harness.starts.append(
            (
                p2p,
                push,
                bool(hass.states.async_entity_ids(ALARM_DOMAIN)),
                task.get_name() if task is not None else "",
            )
        )
        result = await real_start(self, p2p=p2p, push=False) if p2p else {}
        if push and harness.gate is not None:
            try:
                await harness.gate.wait()
            except asyncio.CancelledError:
                harness.order.append("push start cancelled")
                raise
        if push:
            harness.running = harness.running_after_start
            harness.error = None if harness.running else harness.error_after_start
            harness.emit(PushChanged(running=harness.running, error=harness.error))
        return result

    def subscribe(self: EufySecurity, callback: Callable[[Any], None]) -> Callable[[], None]:
        unsubscribe = real_subscribe(self, callback)
        harness.subscribers.append(callback)

        def _unsubscribe() -> None:
            if callback in harness.subscribers:
                harness.subscribers.remove(callback)
            unsubscribe()

        return _unsubscribe

    async def close(self: EufySecurity) -> None:
        harness.order.append("close")
        # The library's close reports a clean stop, PushChanged(False, None).
        if harness.running:
            harness.running = False
            harness.emit(PushChanged(running=False, error=None))
        await real_close(self)

    monkeypatch.setattr(EufySecurity, "async_start", start)
    monkeypatch.setattr(EufySecurity, "subscribe", subscribe)
    monkeypatch.setattr(EufySecurity, "async_close", close)
    monkeypatch.setattr(EufySecurity, "push_running", property(lambda _self: harness.running))
    monkeypatch.setattr(EufySecurity, "push_error", property(lambda _self: harness.error))
    return harness


def _issue(hass: HomeAssistant, entry: MockConfigEntry) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, f"{ISSUE_PUSH_NOT_RUNNING}_{entry.entry_id}")


async def _unload(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_with_the_option_off_only_the_local_sessions_start(
    hass: HomeAssistant,
    fake_station: FakeStation,
    push: PushHarness,
    seed_warm_cache: Callable[..., None],
) -> None:
    """Off by default: one start, local only, and no push issue, even one left before."""
    seed_warm_cache()
    entry = add_entry(hass)
    errors.sync_push_issue(hass, entry, running=False)
    assert _issue(hass, entry) is not None

    assert await setup_entry(hass, entry)

    assert [(p2p, push_) for p2p, push_, _, _ in push.starts] == [(True, False)]
    assert _issue(hass, entry) is None, "an issue left from an earlier setup was kept"
    push.emit(PushChanged(running=False, error=CommunicationError("synthetic")))
    assert _issue(hass, entry) is None, "push status raised an issue with the option off"

    await _unload(hass, entry)


async def test_with_the_option_on_push_starts_in_the_background_after_the_platforms(
    hass: HomeAssistant,
    fake_station: FakeStation,
    push: PushHarness,
    seed_warm_cache: Callable[..., None],
) -> None:
    """The local start first; then push alone, in a background task, once entities exist."""
    entry = await set_up_warm(hass, seed_warm_cache, options={CONF_CLOUD_PUSH: True})
    await wait_until(lambda: len(push.starts) == 2)

    (p2p_start, push_start) = push.starts
    assert p2p_start[:2] == (True, False)
    assert push_start[:2] == (False, True), "push did not start alone"
    assert push_start[2], "push started before the platforms were forwarded"
    assert push_start[3] == f"{DOMAIN} cloud push start"
    assert _issue(hass, entry) is None

    await _unload(hass, entry)


async def test_push_status_raises_and_withdraws_its_issue(
    hass: HomeAssistant,
    fake_station: FakeStation,
    push: PushHarness,
    seed_warm_cache: Callable[..., None],
) -> None:
    """Not running raises a warning naming only the account; running again withdraws it."""
    entry = await set_up_warm(hass, seed_warm_cache, options={CONF_CLOUD_PUSH: True})
    await wait_until(lambda: len(push.starts) == 2)

    push.emit(PushChanged(running=False, error=CommunicationError("synthetic")))
    issue = _issue(hass, entry)
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.is_fixable is False
    assert issue.translation_key == ISSUE_PUSH_NOT_RUNNING
    assert issue.translation_placeholders == {"account": errors.account_label(entry)}

    push.emit(PushChanged(running=True))
    assert _issue(hass, entry) is None

    # A cloud failure behind it arrives as its own CloudProblem; this one has no error.
    push.emit(PushChanged(running=False, error=None))
    assert _issue(hass, entry) is not None

    await _unload(hass, entry)


async def test_unload_leaves_no_push_issue(
    hass: HomeAssistant,
    fake_station: FakeStation,
    push: PushHarness,
    seed_warm_cache: Callable[..., None],
) -> None:
    """Unload deletes the issue, and the close's clean stop raises none."""
    entry = await set_up_warm(hass, seed_warm_cache, options={CONF_CLOUD_PUSH: True})
    await wait_until(lambda: len(push.starts) == 2)
    push.emit(PushChanged(running=False, error=CommunicationError("synthetic")))
    push.emit(PushChanged(running=True))
    running_before = push.running
    assert running_before

    await _unload(hass, entry)
    assert push.running is False, "the client's close did not run"
    assert _issue(hass, entry) is None, "the close's clean stop raised the issue"

    entry2 = await _set_up_again(hass, entry, push)
    push.emit(PushChanged(running=False, error=CommunicationError("synthetic")))
    assert _issue(hass, entry2) is not None
    await _unload(hass, entry2)
    assert _issue(hass, entry2) is None, "unload kept the issue"


async def _set_up_again(
    hass: HomeAssistant, entry: MockConfigEntry, push: PushHarness
) -> MockConfigEntry:
    starts = len(push.starts)
    assert await setup_entry(hass, entry)
    await wait_until(lambda: len(push.starts) == starts + 2)
    return entry


async def test_a_stop_while_home_assistant_stops_raises_no_issue(
    hass: HomeAssistant,
    fake_station: FakeStation,
    push: PushHarness,
    seed_warm_cache: Callable[..., None],
) -> None:
    """Shutdown does not unload the entry; the close it runs is not an outage."""
    entry = await set_up_warm(hass, seed_warm_cache, options={CONF_CLOUD_PUSH: True})
    await wait_until(lambda: len(push.starts) == 2)

    hass.set_state(CoreState.stopping)
    try:
        push.emit(PushChanged(running=False, error=None))
    finally:
        hass.set_state(CoreState.running)

    assert _issue(hass, entry) is None
    await _unload(hass, entry)


async def test_a_failed_first_push_start_is_retried_until_it_runs(
    hass: HomeAssistant,
    fake_station: FakeStation,
    push: PushHarness,
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The library does not retry a first start that failed; setup's task does."""
    monkeypatch.setattr(runtime, "PUSH_START_RETRY_MIN_SECONDS", 0.05)
    push.running_after_start = False
    push.error_after_start = CommunicationError("synthetic")

    entry = await set_up_warm(hass, seed_warm_cache, options={CONF_CLOUD_PUSH: True})
    await wait_until(lambda: len(push.starts) == 3)
    assert _issue(hass, entry) is not None

    push.running_after_start = True
    await wait_until(lambda: push.running)
    assert _issue(hass, entry) is None
    starts = len(push.starts)
    await asyncio.sleep(0.2)
    assert len(push.starts) == starts, "push was started again while it runs"

    await _unload(hass, entry)


async def test_a_push_start_held_off_by_eufy_waits_out_the_hold_off(
    hass: HomeAssistant,
    fake_station: FakeStation,
    push: PushHarness,
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rate limit's wait is honoured, and unload ends the waiting task."""
    monkeypatch.setattr(runtime, "PUSH_START_RETRY_MIN_SECONDS", 0.05)
    push.running_after_start = False
    push.error_after_start = LoginLimitedError("synthetic", retry_after=3600)

    entry = await set_up_warm(hass, seed_warm_cache, options={CONF_CLOUD_PUSH: True})
    await wait_until(lambda: len(push.starts) == 2)
    await asyncio.sleep(0.3)
    assert len(push.starts) == 2, "push was started again inside eufy's hold-off"

    await _unload(hass, entry)
    assert _issue(hass, entry) is None


async def test_unload_ends_a_push_start_before_the_client_closes(
    hass: HomeAssistant,
    fake_station: FakeStation,
    push: PushHarness,
    seed_warm_cache: Callable[..., None],
) -> None:
    """A start still running at unload is cancelled first, so no listener outlives the close."""
    push.gate = asyncio.Event()
    entry = await set_up_warm(hass, seed_warm_cache, options={CONF_CLOUD_PUSH: True})
    await wait_until(lambda: len(push.starts) == 2)

    await _unload(hass, entry)

    assert push.order == ["push start cancelled", "close"]


async def test_diagnostics_show_the_push_state_and_its_failure_type(
    hass: HomeAssistant,
    fake_station: FakeStation,
    push: PushHarness,
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whether push listens, and its last failure by type name, never its text."""
    monkeypatch.setattr(runtime, "PUSH_START_RETRY_MIN_SECONDS", 3600.0)
    push.running_after_start = False
    push.error_after_start = CommunicationError("synthetic text that must not appear")
    entry = await set_up_warm(hass, seed_warm_cache, options={CONF_CLOUD_PUSH: True})
    await wait_until(lambda: len(push.starts) == 2)

    data = await diagnostics.async_get_config_entry_diagnostics(hass, entry)

    assert data["push"] == {"running": False, "error": "CommunicationError"}
    assert "synthetic text" not in repr(data)
    await _unload(hass, entry)


def _push_needed(hass: HomeAssistant, entry: MockConfigEntry) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, f"{ISSUE_PUSH_NEEDED}_{entry.entry_id}")


async def test_a_homebase_3_with_push_off_raises_no_push_needed_issue(
    hass: HomeAssistant,
    fake_station: FakeStation,
    push: PushHarness,
    seed_warm_cache: Callable[..., None],
) -> None:
    """A station graded verified sends its detections locally: no issue."""
    entry = await set_up_warm(hass, seed_warm_cache)

    assert _push_needed(hass, entry) is None
    await _unload(hass, entry)


@pytest.mark.parametrize("fake_station", [HOMEBASE_2_SN], indirect=True)
@pytest.mark.usefixtures("short_storage")
async def test_a_homebase_2_with_push_off_raises_a_fixable_push_needed_issue(
    hass: HomeAssistant,
    fake_station: FakeStation,
    push: PushHarness,
    seed_warm_cache: Callable[..., None],
) -> None:
    """A station not graded verified, push off: one fixable warning naming the station."""
    entry = await set_up_warm(hass, seed_warm_cache)

    issue = _push_needed(hass, entry)
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.is_fixable is True
    assert issue.is_persistent is False
    assert issue.translation_key == ISSUE_PUSH_NEEDED
    assert issue.data == {"entry_id": entry.entry_id}
    placeholders = issue.translation_placeholders or {}
    assert placeholders["account"] == errors.account_label(entry)
    assert placeholders["stations"]
    assert HOMEBASE_2_SN not in placeholders["stations"]

    await _unload(hass, entry)
    assert _push_needed(hass, entry) is None, "the issue outlived the unload"


@pytest.mark.parametrize("fake_station", [HOMEBASE_2_SN], indirect=True)
@pytest.mark.usefixtures("short_storage")
async def test_a_homebase_2_with_push_on_raises_no_push_needed_issue(
    hass: HomeAssistant,
    fake_station: FakeStation,
    push: PushHarness,
    seed_warm_cache: Callable[..., None],
) -> None:
    """Push on covers the station: no issue."""
    entry = await set_up_warm(hass, seed_warm_cache, options={CONF_CLOUD_PUSH: True})
    await wait_until(lambda: len(push.starts) == 2)

    assert _push_needed(hass, entry) is None
    await _unload(hass, entry)


@pytest.mark.parametrize("fake_station", [HOMEBASE_2_SN], indirect=True)
@pytest.mark.usefixtures("short_storage")
async def test_the_push_needed_fix_switches_push_on_and_reloads(
    hass: HomeAssistant,
    fake_station: FakeStation,
    push: PushHarness,
    seed_warm_cache: Callable[..., None],
) -> None:
    """Confirming sets the option, keeps the others, reloads; push starts, the issue goes."""
    entry = await set_up_warm(hass, seed_warm_cache, options={CONF_SESSION_PROBE: False})
    issue = _push_needed(hass, entry)
    assert issue is not None
    assert await async_setup_component(hass, "repairs", {})
    manager = repairs_flow_manager(hass)
    assert manager is not None

    result = await manager.async_init(DOMAIN, data={"issue_id": issue.issue_id})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"
    assert result["description_placeholders"] == issue.translation_placeholders

    result = await manager.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_SESSION_PROBE: False, CONF_CLOUD_PUSH: True}
    assert entry.state is ConfigEntryState.LOADED
    await wait_until(lambda: any(push_ and not p2p for p2p, push_, _, _ in push.starts))
    assert _push_needed(hass, entry) is None
    await _unload(hass, entry)
