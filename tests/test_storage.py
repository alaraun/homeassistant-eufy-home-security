"""The station's storage record: disk and eMMC diagnostics on a schedule of their own.

Every figure comes from the library's ``StorageInfo``, which the fake station answers
and pushes with synthetic values (``FakeStation.storage``, ``send_storage``). The fake
record describes a 238475 MiB disk with 4000 + 9000 MiB of system areas and 1500 MiB
of recordings, at 38 °C, and a 16 GB eMMC 2 % worn.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any, Final

import pytest
from conftest import (
    advance_to_poll,
    entity_id_for,
    set_up_warm,
    state_of,
    wait_until,
)
from eufy_home_security import (
    ConnectionChanged,
    EufySecurity,
    StationState,
    StorageInfo,
    entity_unique_id,
)
from eufy_home_security.p2p import session as session_module
from eufy_home_security.testing import SYNTHETIC, FakeStation
from homeassistant.components.binary_sensor import DOMAIN as BINARY_SENSOR_DOMAIN
from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    EntityCategory,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.eufy_home_security.binary_sensor import STORAGE_BINARY_SENSORS
from custom_components.eufy_home_security.const import (
    DISK_FORMATTING_KEY,
    DISK_FREE_KEY,
    DISK_PROBLEM_KEY,
    DISK_SIZE_KEY,
    DISK_TEMPERATURE_KEY,
    DISK_USED_KEY,
    DISK_USED_PERCENT_KEY,
    DOMAIN,
    EMMC_FREE_KEY,
    EMMC_PROBLEM_KEY,
    EMMC_SIZE_KEY,
    EMMC_USED_KEY,
    EMMC_USED_SPACE_KEY,
    EMMC_WEAR_KEY,
    FIRMWARE_KEY,
    POLL_INTERVAL_SECONDS,
    STORAGE_POLL_INTERVAL_SECONDS,
)
from custom_components.eufy_home_security.sensor import STATION_DIAGNOSTICS, STORAGE_SENSORS
from custom_components.eufy_home_security.storage import StorageCoordinator, StoragePart

_DISK_SENSOR_KEYS: Final = (
    DISK_USED_KEY,
    DISK_FREE_KEY,
    DISK_SIZE_KEY,
    DISK_USED_PERCENT_KEY,
    DISK_TEMPERATURE_KEY,
)
_DISK_BINARY_KEYS: Final = (DISK_PROBLEM_KEY, DISK_FORMATTING_KEY)
_EMMC_SENSOR_KEYS: Final = (
    EMMC_USED_SPACE_KEY,
    EMMC_FREE_KEY,
    EMMC_SIZE_KEY,
    EMMC_WEAR_KEY,
)
_STORAGE_LOGGER: Final = "custom_components.eufy_home_security.storage"


def _registered(hass: HomeAssistant, domain: str, key: str) -> str | None:
    """The station's entity of ``key`` in ``domain``, or None when none is registered."""
    return er.async_get(hass).async_get_entity_id(
        domain, DOMAIN, entity_unique_id(SYNTHETIC.station_sn, key)
    )


def _enable_before_setup(hass: HomeAssistant, domain: str, key: str) -> None:
    """Register a station entity enabled, as a user who switched it on left it."""
    er.async_get(hass).async_get_or_create(
        domain, DOMAIN, entity_unique_id(SYNTHETIC.station_sn, key)
    )


def _storage(entry: MockConfigEntry) -> StorageCoordinator:
    storage: StorageCoordinator = entry.runtime_data.storage[SYNTHETIC.station_sn]
    return storage


async def _unload(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_disk_and_emmc_figures_with_their_units_and_defaults(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The library's GiB figures, °C and percentages, all diagnostic, read at setup.

    The disk's used, free, used percentage, temperature and problem, and the eMMC's
    used space, free, wear and problem, are on by default; both media's fixed sizes
    and the formatting flag are registered off.
    """
    entry = await set_up_warm(hass, seed_warm_cache)
    registry = er.async_get(hass)

    enabled = {
        (SENSOR_DOMAIN, DISK_USED_KEY): ("14.16", "GiB", "data_size"),
        (SENSOR_DOMAIN, DISK_FREE_KEY): ("218.73", "GiB", "data_size"),
        (SENSOR_DOMAIN, DISK_USED_PERCENT_KEY): ("6.1", "%", None),
        (SENSOR_DOMAIN, DISK_TEMPERATURE_KEY): ("38", "°C", "temperature"),
        (SENSOR_DOMAIN, EMMC_WEAR_KEY): ("2", "%", None),
        (SENSOR_DOMAIN, EMMC_USED_SPACE_KEY): ("2.93", "GiB", "data_size"),
        (SENSOR_DOMAIN, EMMC_FREE_KEY): ("12.7", "GiB", "data_size"),
        (BINARY_SENSOR_DOMAIN, DISK_PROBLEM_KEY): (STATE_OFF, None, "problem"),
        (BINARY_SENSOR_DOMAIN, EMMC_PROBLEM_KEY): (STATE_OFF, None, "problem"),
    }
    for (domain, key), (shown, unit, device_class) in enabled.items():
        entity_id = entity_id_for(hass, domain, SYNTHETIC.station_sn, key)
        state = hass.states.get(entity_id)
        assert state is not None, key
        assert state.state == shown, key
        assert state.attributes.get(ATTR_UNIT_OF_MEASUREMENT) == unit, key
        assert state.attributes.get(ATTR_DEVICE_CLASS) == device_class, key
        registered = registry.async_get(entity_id)
        assert registered is not None
        assert registered.entity_category is EntityCategory.DIAGNOSTIC, key
        assert registered.disabled_by is None, key

    for domain, key in (
        (SENSOR_DOMAIN, DISK_SIZE_KEY),
        (SENSOR_DOMAIN, EMMC_SIZE_KEY),
        (BINARY_SENSOR_DOMAIN, DISK_FORMATTING_KEY),
    ):
        entity_id = entity_id_for(hass, domain, SYNTHETIC.station_sn, key)
        registered = registry.async_get(entity_id)
        assert registered is not None
        assert registered.entity_category is EntityCategory.DIAGNOSTIC, key
        assert registered.disabled_by is er.RegistryEntryDisabler.INTEGRATION, key
        assert state_of(hass, entity_id) is None, key

    await _unload(hass, entry)


def test_the_storage_keys_are_pinned_per_part_and_collide_with_no_station_sensor() -> None:
    """Each part's key set is exactly what its real record carries, keys never change.

    Literal strings, not the constants, so a renamed constant fails here. No storage
    sensor may share a key with a station diagnostic: ``emmc_used`` is the dump
    sensor's, and the same unique id would drop one of the two entities.
    """

    def keys(descriptions: tuple[Any, ...], part: StoragePart) -> set[str]:
        return {d.key for d in descriptions if d.part is part}

    assert keys(STORAGE_SENSORS, StoragePart.DISK) == {
        "disk_used",
        "disk_free",
        "disk_size",
        "disk_used_percent",
        "disk_temperature",
    }
    assert keys(STORAGE_SENSORS, StoragePart.EMMC) == {
        "emmc_used_space",
        "emmc_free",
        "emmc_size",
        "emmc_wear",
    }
    assert keys(STORAGE_BINARY_SENSORS, StoragePart.DISK) == {"disk_problem", "disk_formatting"}
    assert keys(STORAGE_BINARY_SENSORS, StoragePart.EMMC) == {"emmc_problem"}
    for descriptions in (STORAGE_SENSORS, STORAGE_BINARY_SENSORS):
        all_keys = [d.key for d in descriptions]
        assert len(all_keys) == len(set(all_keys))
        for description in descriptions:
            assert description.translation_key == description.key
    assert not {d.key for d in STORAGE_SENSORS} & {d.key for d in STATION_DIAGNOSTICS}


@pytest.mark.parametrize(
    ("key", "shown"),
    [(DISK_SIZE_KEY, "232.89"), (EMMC_SIZE_KEY, "15.62")],
    ids=["disk", "emmc"],
)
async def test_the_size_reads_the_library_total_once_enabled(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    key: str,
    shown: str,
) -> None:
    """The total is the app's own figure, the medium's size in GiB."""
    _enable_before_setup(hass, SENSOR_DOMAIN, key)
    entry = await set_up_warm(hass, seed_warm_cache)

    entity_id = entity_id_for(hass, SENSOR_DOMAIN, SYNTHETIC.station_sn, key)
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == shown
    assert state.attributes[ATTR_UNIT_OF_MEASUREMENT] == "GiB"

    await _unload(hass, entry)


async def test_no_disk_means_no_disk_entities_until_a_record_reports_one(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A record without a disk makes only the eMMC entities; a pushed disk adds the rest."""
    disk = fake_station.storage.pop("hdd_info")
    entry = await set_up_warm(hass, seed_warm_cache)

    for key in _DISK_SENSOR_KEYS:
        assert _registered(hass, SENSOR_DOMAIN, key) is None, key
    for key in _DISK_BINARY_KEYS:
        assert _registered(hass, BINARY_SENSOR_DOMAIN, key) is None, key
    wear = entity_id_for(hass, SENSOR_DOMAIN, SYNTHETIC.station_sn, EMMC_WEAR_KEY)
    assert state_of(hass, wear) == "2"

    fake_station.storage["hdd_info"] = disk
    fake_station.send_storage()
    await wait_until(lambda: _registered(hass, SENSOR_DOMAIN, DISK_USED_KEY) is not None)
    await hass.async_block_till_done()
    used = entity_id_for(hass, SENSOR_DOMAIN, SYNTHETIC.station_sn, DISK_USED_KEY)
    assert state_of(hass, used) == "14.16"
    assert _registered(hass, BINARY_SENSOR_DOMAIN, DISK_PROBLEM_KEY) is not None

    await _unload(hass, entry)


async def test_a_pushed_record_updates_the_entities_without_a_read(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``StorageChanged`` is applied at once, and a failing disk turns the problem on."""
    entry = await set_up_warm(hass, seed_warm_cache)
    station = _storage(entry).station
    reads = 0
    real_read = station.async_get_storage

    async def counting_read(*, timeout: float = 10.0) -> StorageInfo:
        nonlocal reads
        reads += 1
        return await real_read(timeout=timeout)

    monkeypatch.setattr(station, "async_get_storage", counting_read)
    used = entity_id_for(hass, SENSOR_DOMAIN, SYNTHETIC.station_sn, DISK_USED_KEY)
    problem = entity_id_for(hass, BINARY_SENSOR_DOMAIN, SYNTHETIC.station_sn, DISK_PROBLEM_KEY)
    temperature = entity_id_for(hass, SENSOR_DOMAIN, SYNTHETIC.station_sn, DISK_TEMPERATURE_KEY)

    hdd = fake_station.storage["hdd_info"]
    hdd["video_used"] = 1500 + 10240
    hdd["cur_temperate"] = 45
    hdd["health"] = 3
    fake_station.send_storage()

    await wait_until(lambda: state_of(hass, used) == "24.16")
    assert state_of(hass, temperature) == "45"
    assert state_of(hass, problem) == STATE_ON
    emmc_problem = entity_id_for(hass, BINARY_SENSOR_DOMAIN, SYNTHETIC.station_sn, EMMC_PROBLEM_KEY)
    assert state_of(hass, emmc_problem) == STATE_OFF
    assert reads == 0

    await _unload(hass, entry)


async def test_an_unhealthy_emmc_turns_on_only_the_emmc_problem(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A pushed eMMC health code other than 0 leaves the disk's problem sensor off."""
    entry = await set_up_warm(hass, seed_warm_cache)
    emmc_problem = entity_id_for(hass, BINARY_SENSOR_DOMAIN, SYNTHETIC.station_sn, EMMC_PROBLEM_KEY)
    disk_problem = entity_id_for(hass, BINARY_SENSOR_DOMAIN, SYNTHETIC.station_sn, DISK_PROBLEM_KEY)
    assert state_of(hass, emmc_problem) == STATE_OFF

    fake_station.storage["emmc_info"]["health"] = 3
    fake_station.send_storage()
    await wait_until(lambda: state_of(hass, emmc_problem) == STATE_ON)
    assert state_of(hass, disk_problem) == STATE_OFF

    await _unload(hass, entry)


async def test_no_emmc_in_the_record_means_no_emmc_storage_entities(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Without ``emmc_info`` only the disk entities and the station's dump sensor exist."""
    fake_station.storage.pop("emmc_info")
    entry = await set_up_warm(hass, seed_warm_cache)

    for key in _EMMC_SENSOR_KEYS:
        assert _registered(hass, SENSOR_DOMAIN, key) is None, key
    assert _registered(hass, BINARY_SENSOR_DOMAIN, EMMC_PROBLEM_KEY) is None
    for key in _DISK_SENSOR_KEYS:
        assert _registered(hass, SENSOR_DOMAIN, key) is not None, key
    for key in _DISK_BINARY_KEYS:
        assert _registered(hass, BINARY_SENSOR_DOMAIN, key) is not None, key
    assert _registered(hass, SENSOR_DOMAIN, EMMC_USED_KEY) is not None

    await _unload(hass, entry)


async def test_the_formatting_sensor_follows_a_format_from_start_to_end(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """``parted_status`` 2 is on, the record the station pushes at the end is off."""
    _enable_before_setup(hass, BINARY_SENSOR_DOMAIN, DISK_FORMATTING_KEY)
    entry = await set_up_warm(hass, seed_warm_cache)
    formatting = entity_id_for(
        hass, BINARY_SENSOR_DOMAIN, SYNTHETIC.station_sn, DISK_FORMATTING_KEY
    )
    used = entity_id_for(hass, SENSOR_DOMAIN, SYNTHETIC.station_sn, DISK_USED_KEY)
    assert state_of(hass, formatting) == STATE_OFF

    hdd = fake_station.storage["hdd_info"]
    hdd["parted_status"] = 2
    fake_station.send_storage()
    await wait_until(lambda: state_of(hass, formatting) == STATE_ON)
    assert state_of(hass, used) == "14.16"

    hdd["parted_status"] = 1
    hdd["video_used"] = 0
    fake_station.send_storage()
    await wait_until(lambda: state_of(hass, formatting) == STATE_OFF)
    assert state_of(hass, used) == "12.7"

    await _unload(hass, entry)


async def test_a_failed_read_makes_only_the_storage_entities_unavailable_until_one_succeeds(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A rejected read: storage entities unavailable, one ERROR line, then recovery.

    The station's other entities stay as they were, and a second failure logs
    nothing more.
    """
    entry = await set_up_warm(hass, seed_warm_cache)
    storage = _storage(entry)
    used = entity_id_for(hass, SENSOR_DOMAIN, SYNTHETIC.station_sn, DISK_USED_KEY)
    wear = entity_id_for(hass, SENSOR_DOMAIN, SYNTHETIC.station_sn, EMMC_WEAR_KEY)
    problem = entity_id_for(hass, BINARY_SENSOR_DOMAIN, SYNTHETIC.station_sn, DISK_PROBLEM_KEY)
    firmware = entity_id_for(hass, SENSOR_DOMAIN, SYNTHETIC.station_sn, FIRMWARE_KEY)
    firmware_before = state_of(hass, firmware)
    caplog.clear()

    fake_station.storage_reply_code = 1
    await storage.async_refresh()
    await hass.async_block_till_done()
    await storage.async_refresh()
    await hass.async_block_till_done()

    for entity_id in (used, wear, problem):
        assert state_of(hass, entity_id) == STATE_UNAVAILABLE, entity_id
    assert state_of(hass, firmware) == firmware_before
    errors = [
        record
        for record in caplog.records
        if record.name == _STORAGE_LOGGER and record.levelno >= logging.WARNING
    ]
    assert len(errors) == 1
    assert SYNTHETIC.station_sn not in errors[0].getMessage()

    fake_station.storage_reply_code = 0
    await storage.async_refresh()
    await hass.async_block_till_done()
    assert state_of(hass, used) == "14.16"
    assert state_of(hass, wear) == "2"
    assert state_of(hass, problem) == STATE_OFF

    await _unload(hass, entry)


async def test_a_pushed_record_ends_a_failed_reads_unavailability(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A record the station pushes is an answer: the entities come back without a read."""
    entry = await set_up_warm(hass, seed_warm_cache)
    storage = _storage(entry)
    used = entity_id_for(hass, SENSOR_DOMAIN, SYNTHETIC.station_sn, DISK_USED_KEY)

    fake_station.storage_reply_code = 1
    await storage.async_refresh()
    await hass.async_block_till_done()
    assert state_of(hass, used) == STATE_UNAVAILABLE

    fake_station.storage_reply_code = 0
    fake_station.storage["hdd_info"]["video_used"] = 2524
    fake_station.send_storage()
    await wait_until(lambda: state_of(hass, used) == "15.16")

    await _unload(hass, entry)


async def test_a_reconnect_reads_a_failed_record_again_and_leaves_a_good_one(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A restored session reads the record only when the last read failed."""
    entry = await set_up_warm(hass, seed_warm_cache)
    storage = _storage(entry)
    router = entry.runtime_data.router
    station = storage.station
    reads = 0
    real_read = station.async_get_storage

    async def counting_read(*, timeout: float = 10.0) -> StorageInfo:
        nonlocal reads
        reads += 1
        return await real_read(timeout=timeout)

    monkeypatch.setattr(station, "async_get_storage", counting_read)
    used = entity_id_for(hass, SENSOR_DOMAIN, SYNTHETIC.station_sn, DISK_USED_KEY)
    restored = ConnectionChanged(station_sn=SYNTHETIC.station_sn, connected=True)

    router.handle(restored)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert reads == 0

    fake_station.storage_reply_code = 1
    await storage.async_refresh()
    assert state_of(hass, used) == STATE_UNAVAILABLE
    fake_station.storage_reply_code = 0
    router.handle(restored)
    await wait_until(lambda: state_of(hass, used) == "14.16")
    assert reads == 2

    await _unload(hass, entry)


async def test_the_storage_schedule_and_the_guard_poll_leave_each_other_alone(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Storage traffic moves no guard poll, and guard traffic moves no storage read.

    A pushed record and a storage read at +30 s leave the guard poll at +45 s; that
    poll leaves the storage read at +30 min, where it runs exactly once.
    """
    entry = await set_up_warm(hass, seed_warm_cache)
    anchor = dt_util.utcnow()
    storage = _storage(entry)
    station = storage.station
    assert storage.update_interval is not None
    assert storage.update_interval.total_seconds() == STORAGE_POLL_INTERVAL_SECONDS

    polls = 0
    real_update = station.async_update

    async def counting_update() -> StationState:
        nonlocal polls
        polls += 1
        return await real_update()

    reads = 0
    real_read = station.async_get_storage

    async def counting_read(*, timeout: float = 10.0) -> StorageInfo:
        nonlocal reads
        reads += 1
        return await real_read(timeout=timeout)

    monkeypatch.setattr(station, "async_update", counting_update)
    monkeypatch.setattr(station, "async_get_storage", counting_read)
    used = entity_id_for(hass, SENSOR_DOMAIN, SYNTHETIC.station_sn, DISK_USED_KEY)

    await advance_to_poll(hass, 30, anchor=anchor)
    fake_station.storage["hdd_info"]["video_used"] = 2524
    fake_station.send_storage()
    await wait_until(lambda: state_of(hass, used) == "15.16")
    assert polls == 0

    # Three seconds short: Home Assistant floors a coordinator's due time to the
    # second and fires timers half a second early, so one second short can fire.
    await advance_to_poll(hass, POLL_INTERVAL_SECONDS - 3, anchor=anchor)
    assert polls == 0, "storage traffic brought the guard poll forward"
    await advance_to_poll(hass, POLL_INTERVAL_SECONDS + 1, anchor=anchor)
    await wait_until(lambda: polls >= 1)
    await hass.async_block_till_done()
    assert polls == 1, "storage traffic postponed or multiplied the guard poll"
    assert reads == 0, "the guard poll read the storage record"

    await advance_to_poll(hass, STORAGE_POLL_INTERVAL_SECONDS - 5, anchor=anchor)
    assert reads == 0, "a storage read ran before its interval"
    await advance_to_poll(hass, STORAGE_POLL_INTERVAL_SECONDS + 5, anchor=anchor)
    await wait_until(lambda: reads >= 1)
    await hass.async_block_till_done()
    assert reads == 1, "the push or the guard poll postponed or multiplied the storage read"
    # The jump also fires the library's own session timers, which can leave a pushed
    # dump settling; let it settle rather than linger past the test.
    await asyncio.sleep(session_module.PARAM_SETTLE + 0.2)
    await hass.async_block_till_done()

    await _unload(hass, entry)
