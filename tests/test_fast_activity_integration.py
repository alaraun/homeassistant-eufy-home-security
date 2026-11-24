"""Actual coordinator scheduling and HA options for fast activity polling.

Uses the synthetic loopback FakeStation; no physical cameras or cloud accounts.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

import pytest
from conftest import advance_to_poll, configure_options, set_up_warm, wait_until
from eufy_home_security import EufySecurity, StationState
from eufy_home_security.testing import SYNTHETIC, FakeStation
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.util import dt as dt_util

from custom_components.eufy_home_security.const import (
    CONF_FAST_ACTIVITY_POLL,
    CONF_FAST_ACTIVITY_POLL_INTERVAL,
)


async def test_fast_interval_drives_one_station_coordinator_timer(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One scheduled read after ten seconds, none before it."""
    entry = await set_up_warm(
        hass,
        seed_warm_cache,
        options={
            CONF_FAST_ACTIVITY_POLL: True,
            CONF_FAST_ACTIVITY_POLL_INTERVAL: 10,
        },
    )
    coordinator = entry.runtime_data.coordinators[SYNTHETIC.station_sn]
    assert not coordinator.station.connects_on_demand
    assert coordinator.update_interval == timedelta(seconds=10)

    station = coordinator.station
    actual_update = station.async_update
    polls = 0

    async def counted_update() -> StationState:
        nonlocal polls
        polls += 1
        return await actual_update()

    monkeypatch.setattr(station, "async_update", counted_update)
    anchor = dt_util.utcnow()

    await advance_to_poll(hass, 6, anchor=anchor)
    assert polls == 0, "fast station poll ran before its interval"

    await advance_to_poll(hass, 13, anchor=anchor)
    await wait_until(lambda: polls >= 1)
    await hass.async_block_till_done()
    assert polls == 1, "one due timer should perform one station refresh"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_options_enable_and_disable_fast_poll_with_reload(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """One saved option controls the coordinator; disabling restores 45 s."""
    entry = await set_up_warm(hass, seed_warm_cache)

    def interval() -> timedelta:
        return entry.runtime_data.coordinators[SYNTHETIC.station_sn].update_interval

    assert interval() == timedelta(seconds=45)
    initial_clients = len(built_clients)

    async def save(enabled: bool) -> None:
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] is FlowResultType.FORM
        saved = await configure_options(
            hass,
            result["flow_id"],
            {
                CONF_FAST_ACTIVITY_POLL: enabled,
                CONF_FAST_ACTIVITY_POLL_INTERVAL: 6,
            },
        )
        assert saved["type"] is FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()

    await save(True)
    assert entry.options[CONF_FAST_ACTIVITY_POLL] is True
    assert entry.options[CONF_FAST_ACTIVITY_POLL_INTERVAL] == 6
    assert interval() == timedelta(seconds=6)
    assert len(built_clients) == initial_clients + 1

    await save(False)
    assert dict(entry.options)[CONF_FAST_ACTIVITY_POLL] is False
    assert entry.options[CONF_FAST_ACTIVITY_POLL_INTERVAL] == 6
    assert interval() == timedelta(seconds=45)
    assert len(built_clients) == initial_clients + 2

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_fast_poll_interval_outside_configured_range_is_rejected(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Invalid intervals never save an options entry."""
    entry = await set_up_warm(hass, seed_warm_cache)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM

    for seconds in (0, 45):
        with pytest.raises(InvalidData):
            await configure_options(
                hass,
                result["flow_id"],
                {
                    CONF_FAST_ACTIVITY_POLL: True,
                    CONF_FAST_ACTIVITY_POLL_INTERVAL: seconds,
                },
            )
        assert entry.options == {}

    hass.config_entries.options.async_abort(result["flow_id"])
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
