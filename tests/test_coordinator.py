"""Pushes, availability and start handling: the event router and the coordinator.

Every test runs the real library against the loopback ``FakeStation``: the pushes
below are real guard-mode reports and session closes on the wire, never events
built by hand, except the one ``ParamChanged`` whose point is that it is ignored.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from conftest import (
    add_entry,
    advance_to_poll,
    cloud_calls,
    entity_id_for,
    panel_entity_id,
    record_states,
    seed_setting,
    set_guard_mode,
    set_up_warm,
    setup_entry,
    state_of,
    wait_until,
)
from eufy_home_security import (
    CommunicationError,
    EufySecurity,
    Event,
    GuardMode,
    GuardModeChanged,
    ParamChanged,
    Station,
    StationState,
)
from eufy_home_security.testing import SYNTHETIC, FakeCloud, FakeStation
from homeassistant.components.alarm_control_panel import DOMAIN as ALARM_DOMAIN
from homeassistant.components.number.const import DOMAIN as NUMBER_DOMAIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, SERVICE_ALARM_ARM_HOME
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.eufy_home_security.const import CONNECTION_LOSS_GRACE_SECONDS

# The loggers whose lines a user's log would carry: the integration's and the library's.
_OUR_LOGGERS = ("custom_components.eufy_home_security", "eufy_home_security")

# A verified camera setting with a number entity: shows when a pushed dump arrives.
_RETRIGGER = "trigger_interval_time"


async def _settle(hass: HomeAssistant) -> None:
    """Give a report on the wire time to arrive, then let HA finish what it started."""
    await asyncio.sleep(0.3)
    await hass.async_block_till_done()


async def test_a_guard_mode_push_updates_the_panel_without_moving_the_poll(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pushes at +30 s show at once, and the poll still runs at +45 s, not later."""
    entry = await set_up_warm(hass, seed_warm_cache)
    anchor = dt_util.utcnow()
    entity_id = panel_entity_id(hass)
    station = entry.runtime_data.coordinators[SYNTHETIC.station_sn].station

    polls = 0
    real_update = station.async_update

    async def counting_update() -> StationState:
        nonlocal polls
        polls += 1
        return await real_update()

    monkeypatch.setattr(station, "async_update", counting_update)

    await advance_to_poll(hass, 30, anchor=anchor)
    set_guard_mode(fake_station, GuardMode.HOME)
    for _ in range(3):
        fake_station.send_alarm_mode(GuardMode.HOME)
    await wait_until(lambda: state_of(hass, entity_id) == "armed_home")
    assert polls == 0, "the panel changed by a poll, not by the push"

    # 42, not 44: Home Assistant floors a coordinator's due time to the second and
    # fires timers up to half a second early, so a sample one second short of the
    # interval can fire the poll once setup (the first storage read included) ran
    # into the next second.
    await advance_to_poll(hass, 42, anchor=anchor)
    assert polls == 0, "a poll ran before its 45 s interval"

    await advance_to_poll(hass, 46, anchor=anchor)
    await wait_until(lambda: polls >= 1)
    await hass.async_block_till_done()
    assert polls == 1, "the pushes postponed or multiplied the poll"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_push_of_the_mode_in_force_writes_no_state(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A report of the mode already shown notifies nobody; a different one does."""
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)
    coordinator = entry.runtime_data.coordinators[SYNTHETIC.station_sn]
    assert coordinator.data.guard_mode == GuardMode.AWAY
    states = record_states(hass, entity_id)

    fake_station.send_alarm_mode(GuardMode.AWAY)
    await _settle(hass)
    assert states == []

    # Not vacuous: a report of another mode on the same path is written at once.
    set_guard_mode(fake_station, GuardMode.HOME)
    fake_station.send_alarm_mode(GuardMode.HOME)
    await wait_until(lambda: states == ["armed_home"])

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_closed_session_that_reconnects_at_once_keeps_the_panel_available(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The station's CLOSE, then the library's reconnect within the grace: the panel
    never shows unavailable, and the reconnect reads the station again."""
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)
    states = record_states(hass, entity_id)
    station = entry.runtime_data.coordinators[SYNTHETIC.station_sn].station
    dumps = fake_station.param_queries

    fake_station.send_close()

    await wait_until(lambda: not station.connected, timeout=10)
    await wait_until(lambda: station.connected and fake_station.param_queries > dumps, timeout=30)
    await hass.async_block_till_done()
    assert "unavailable" not in states
    assert state_of(hass, entity_id) == "armed_away"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_param_change_does_not_touch_the_panel(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A parameter change is left to the poll."""
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)
    coordinator = entry.runtime_data.coordinators[SYNTHETIC.station_sn]
    before = coordinator.data
    states = record_states(hass, entity_id)

    entry.runtime_data.router.handle(
        ParamChanged(station_sn=SYNTHETIC.station_sn, channel=0, param_id=1101, old="87", new="80")
    )
    await hass.async_block_till_done()

    assert states == []
    assert coordinator.data is before

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_pushed_state_updates_setting_entities_without_moving_the_poll(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dump the station pushes shows at once, and the poll still runs at +45 s.

    The guard-mode push above proves the panel; this proves the settings, which come
    from the whole state rather than one field. The same timing is asserted, because
    the danger is the same: a station that pushes dumps steadily would postpone the
    45 s poll for as long as it kept pushing, if a push went through the coordinator's
    set-updated-data method instead of its listeners.
    """
    seed_setting(fake_station, _RETRIGGER, 30)
    entry = await set_up_warm(hass, seed_warm_cache)
    anchor = dt_util.utcnow()
    entity_id = entity_id_for(hass, NUMBER_DOMAIN, SYNTHETIC.camera_sn, _RETRIGGER)
    assert state_of(hass, entity_id) == "30"
    station = entry.runtime_data.coordinators[SYNTHETIC.station_sn].station

    polls = 0
    real_update = station.async_update

    async def counting_update() -> StationState:
        nonlocal polls
        polls += 1
        return await real_update()

    monkeypatch.setattr(station, "async_update", counting_update)

    await advance_to_poll(hass, 30, anchor=anchor)
    seed_setting(fake_station, _RETRIGGER, 45)
    fake_station.send_param_dump()
    await wait_until(lambda: state_of(hass, entity_id) == "45")
    assert polls == 0, "the setting changed by a poll, not by the pushed dump"

    # Well inside the interval: the anchor is taken after setup scheduled the poll, so
    # a sample at the edge has under a second of real margin (see `advance_to_poll`).
    # A push that rescheduled the poll fails the +46 s assertion below.
    await advance_to_poll(hass, 35, anchor=anchor)
    assert polls == 0, "a poll ran early, before its 45 s interval"

    await advance_to_poll(hass, 46, anchor=anchor)
    await wait_until(lambda: polls >= 1)
    await hass.async_block_till_done()
    assert polls == 1, "the pushed dump postponed or multiplied the poll"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_pushed_state_identical_to_the_shown_one_writes_no_state(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A dump reporting what is already shown notifies nobody; a changed one does."""
    seed_setting(fake_station, _RETRIGGER, 30)
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = entity_id_for(hass, NUMBER_DOMAIN, SYNTHETIC.camera_sn, _RETRIGGER)
    assert state_of(hass, entity_id) == "30"
    states = record_states(hass, entity_id)

    fake_station.send_param_dump()
    await _settle(hass)
    # The dump completes on its own timer when a block is late (PARAM_SETTLE is 1 s),
    # so a verdict taken any sooner could be a verdict on a dump still in flight.
    await asyncio.sleep(0.5)
    await hass.async_block_till_done()
    assert states == []

    # Not vacuous: a dump that does change the value is shown on the same path.
    seed_setting(fake_station, _RETRIGGER, 45)
    fake_station.send_param_dump()
    await wait_until(lambda: states == ["45"])

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_push_and_a_poll_in_either_order_leave_the_last_value(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Backstop: push, then poll, then push; the panel follows whichever came last."""
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)
    coordinator = entry.runtime_data.coordinators[SYNTHETIC.station_sn]

    set_guard_mode(fake_station, GuardMode.HOME)
    fake_station.send_alarm_mode(GuardMode.HOME)
    await wait_until(lambda: state_of(hass, entity_id) == "armed_home")

    set_guard_mode(fake_station, GuardMode.DISARMED)
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert state_of(hass, entity_id) == "disarmed"

    set_guard_mode(fake_station, GuardMode.AWAY)
    fake_station.send_alarm_mode(GuardMode.AWAY)
    await wait_until(lambda: state_of(hass, entity_id) == "armed_away")

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_the_report_of_our_own_arm_keeps_arming_until_the_call_returns(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The station's report of an arm in flight arrives mid-call and changes nothing shown."""
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)
    seen: list[Event] = []
    entry.async_on_unload(entry.runtime_data.eufy.subscribe(seen.append))
    states = record_states(hass, entity_id)

    await hass.services.async_call(
        ALARM_DOMAIN, SERVICE_ALARM_ARM_HOME, {ATTR_ENTITY_ID: entity_id}, blocking=True
    )
    await hass.async_block_till_done()

    # Not vacuous: the station did report the arm while the call was running.
    assert any(
        isinstance(event, GuardModeChanged) and event.mode == GuardMode.HOME for event in seen
    )
    assert states.count("arming") == 1, states
    assert states[states.index("arming") + 1] == "armed_home", states

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_setup_reads_each_started_station_once(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The start's ConnectionChanged(True) asks for no read the first refresh repeats."""
    reads = 0
    real_update = Station.async_update

    async def counting_update(self: Station) -> StationState:
        nonlocal reads
        reads += 1
        return await real_update(self)

    monkeypatch.setattr(Station, "async_update", counting_update)

    entry = await set_up_warm(hass, seed_warm_cache)
    await _settle(hass)

    assert entry.state is ConfigEntryState.LOADED
    assert state_of(hass, panel_entity_id(hass)) == "armed_away"
    assert reads == 1

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


def _integration_errors(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        f"{record.name}: {record.getMessage()}"
        for record in caplog.records
        if record.name.startswith("custom_components.eufy_home_security")
        and record.levelno >= logging.ERROR
    ]


async def test_a_station_whose_first_read_failed_on_a_live_session_is_read_again_at_setup(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A dropped start ConnectionChanged(True) is caught up, not left to the poll.

    A known entry is reloaded; the station starts and its session is up, but its
    first read fails. The panel must come back from one more read at the end of
    setup, with HA's clock never reaching the 45 s poll.
    """
    entry = await set_up_warm(hass, seed_warm_cache)
    reads = 0
    real_update = Station.async_update

    async def first_read_fails(self: Station) -> StationState:
        nonlocal reads
        reads += 1
        if reads == 1:
            raise CommunicationError("synthetic read timeout on a live session")
        return await real_update(self)

    monkeypatch.setattr(Station, "async_update", first_read_fails)
    caplog.set_level(logging.WARNING)

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    await wait_until(lambda: state_of(hass, panel_entity_id(hass)) == "armed_away")
    assert reads == 2
    warnings = [r.getMessage() for r in caplog.records if "did not come up" in r.getMessage()]
    assert len(warnings) == 1, warnings
    assert _integration_errors(caplog) == []
    assert [
        record.getMessage()
        for record in caplog.records
        if record.name.startswith(_OUR_LOGGERS) and SYNTHETIC.station_sn in record.getMessage()
    ] == []
    assert cloud_calls(fake_cloud) == []

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_station_that_came_up_and_dropped_during_setup_loads_unavailable(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A dropped start ConnectionChanged(False) is caught up at setup.

    The station answers its first read, then closes the session (and stays gone)
    before setup follows availability. It must load unavailable, not available
    with its last guard mode on a session that is down.
    """
    real_update = Station.async_update
    dropped = False

    async def read_then_drop(self: Station) -> StationState:
        nonlocal dropped
        state = await real_update(self)
        if not dropped:
            dropped = True
            fake_station.send_close()
            fake_station.stop()
            await wait_until(lambda: not self.connected)
        return state

    monkeypatch.setattr(Station, "async_update", read_then_drop)
    caplog.set_level(logging.WARNING)

    entry = await set_up_warm(hass, seed_warm_cache)

    assert dropped
    assert entry.state is ConfigEntryState.LOADED
    # At once: the library's first reconnect is 5 s away, and HA's poll 45 s.
    assert state_of(hass, panel_entity_id(hass)) == "unavailable"
    assert [
        record.getMessage()
        for record in caplog.records
        if record.name.startswith(_OUR_LOGGERS) and SYNTHETIC.station_sn in record.getMessage()
    ] == []
    assert cloud_calls(fake_cloud) == []

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_station_that_never_came_up_on_first_setup_is_retried_then_loads_unavailable(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    short_discovery: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A brand-new entry waits for its station once, then loads with the panel unavailable."""
    caplog.set_level(logging.DEBUG)
    seed_warm_cache()
    fake_station.stop()
    entry = add_entry(hass)

    await setup_entry(hass, entry)
    first_state = entry.state
    assert first_state is ConfigEntryState.SETUP_RETRY

    # HA's own retry (5 s plus jitter, ConfigEntry.async_setup), never a second
    # setup racing the pending retry timer. The retry's setup is not a task that
    # async_block_till_done waits for, and it spends real time on the short LAN
    # search, so wait for the state it ends in.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))
    await wait_until(lambda: entry.state is not ConfigEntryState.SETUP_RETRY, timeout=10)
    await wait_until(lambda: entry.state is not ConfigEntryState.SETUP_IN_PROGRESS, timeout=10)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert state_of(hass, panel_entity_id(hass)) == "unavailable"
    assert cloud_calls(fake_cloud) == []

    ours = [record for record in caplog.records if record.name.startswith(_OUR_LOGGERS)]
    assert any(record.name.startswith("eufy_home_security") for record in ours), (
        "no library log record was captured, so the serial check below is vacuous"
    )
    warnings = [
        record.getMessage()
        for record in ours
        if record.levelno == logging.WARNING and "did not come up" in record.getMessage()
    ]
    assert warnings, "no WARNING named the station that did not come up"
    # One WARNING per setup attempt, and no ERROR from the coordinator beside it.
    assert len(warnings) == 2, warnings
    integration_errors = [
        f"{record.name}: {record.getMessage()}"
        for record in ours
        if record.name.startswith("custom_components.eufy_home_security")
        and record.levelno >= logging.ERROR
    ]
    assert integration_errors == []
    leaking = [
        f"{record.name}: {record.getMessage()}"
        for record in ours
        if SYNTHETIC.station_sn in record.getMessage()
    ]
    assert leaking == []

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_an_idle_disconnect_is_not_an_outage_for_a_homebase(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """An idle disconnect does not mark the HomeBase unavailable."""
    from eufy_home_security import ConnectionChanged, DisconnectCause

    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = panel_entity_id(hass)

    entry.runtime_data.router.handle(
        ConnectionChanged(
            station_sn=SYNTHETIC.station_sn, connected=False, cause=DisconnectCause.IDLE
        )
    )
    await hass.async_block_till_done()

    coordinator = entry.runtime_data.coordinators[SYNTHETIC.station_sn]
    assert state_of(hass, entity_id) != "unavailable"
    assert coordinator.last_update_success is True

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_session_lost_while_home_assistant_stops_is_not_an_outage(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Stopping HA ends every session; the loss logs no ERROR and marks nothing."""
    from eufy_home_security import ConnectionChanged, DisconnectCause, StationUnreachableError
    from homeassistant.core import CoreState

    entry = await set_up_warm(hass, seed_warm_cache)
    coordinator = entry.runtime_data.coordinators[SYNTHETIC.station_sn]
    hass.set_state(CoreState.stopping)
    try:
        with caplog.at_level(logging.DEBUG):
            entry.runtime_data.router.handle(
                ConnectionChanged(
                    station_sn=SYNTHETIC.station_sn,
                    connected=False,
                    cause=DisconnectCause.STATION_CLOSED,
                    error=StationUnreachableError("the station closed the session"),
                )
            )
            await hass.async_block_till_done()
    finally:
        hass.set_state(CoreState.running)

    assert coordinator.last_update_success is True
    assert [
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("custom_components.eufy_home_security")
        and record.levelno >= logging.ERROR
    ] == []

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


def _lost(cause: Any) -> Any:
    from eufy_home_security import ConnectionChanged, StationUnreachableError

    return ConnectionChanged(
        station_sn=SYNTHETIC.station_sn,
        connected=False,
        cause=cause,
        error=StationUnreachableError("lost"),
    )


@pytest.mark.parametrize("cause", ["station_closed", "link_silent", "unreachable"])
async def test_a_session_lost_while_running_is_an_outage_after_the_grace(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    cause: str,
) -> None:
    """A lost session stays available through the grace, then shows unavailable."""
    from eufy_home_security import DisconnectCause

    entry = await set_up_warm(hass, seed_warm_cache)
    coordinator = entry.runtime_data.coordinators[SYNTHETIC.station_sn]
    fake_station.stop()

    entry.runtime_data.router.handle(_lost(DisconnectCause(cause)))
    await hass.async_block_till_done()
    in_grace = coordinator.last_update_success
    assert in_grace is True

    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=CONNECTION_LOSS_GRACE_SECONDS + 1)
    )
    await hass.async_block_till_done()

    assert coordinator.last_update_success is False
    assert state_of(hass, panel_entity_id(hass)) == "unavailable"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_session_back_within_the_grace_never_shows_unavailable(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A HomeBase drop that reconnects within seconds is not an outage: no ERROR, no
    unavailable state, and the reconnect still reads the station."""
    from eufy_home_security import ConnectionChanged, DisconnectCause

    entry = await set_up_warm(hass, seed_warm_cache)
    coordinator = entry.runtime_data.coordinators[SYNTHETIC.station_sn]
    states = record_states(hass, panel_entity_id(hass))
    dumps = fake_station.param_queries

    with caplog.at_level(logging.DEBUG):
        entry.runtime_data.router.handle(_lost(DisconnectCause.STATION_CLOSED))
        await hass.async_block_till_done()
        entry.runtime_data.router.handle(
            ConnectionChanged(station_sn=SYNTHETIC.station_sn, connected=True)
        )
        await hass.async_block_till_done()
        await wait_until(lambda: fake_station.param_queries > dumps, timeout=10)
        async_fire_time_changed(
            hass, dt_util.utcnow() + timedelta(seconds=CONNECTION_LOSS_GRACE_SECONDS + 1)
        )
        await hass.async_block_till_done()

    assert coordinator.last_update_success is True
    assert "unavailable" not in states
    assert [
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("custom_components.eufy_home_security")
        and record.levelno >= logging.ERROR
    ] == []

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
