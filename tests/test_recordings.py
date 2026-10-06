"""Event videos (``recordings.py``): each HomeBase recording copied into the history.

End to end on the library's loopback ``FakeStation``: its ``rows`` are the station's
history, its recordings play ``recording_frames`` frames, and the clip's MPEG-TS is
remuxed by ``tests/fake_ffmpeg.py`` (conftest's autouse ``_fake_ffmpeg``). Passes are
started by moving Home Assistant's clock past the sync's timers. Unless a test says
otherwise the option was switched on an hour before setup (the Store's ``since``).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest
from conftest import (
    PUSHED_THUMB_PATH,
    SYNTHETIC,
    add_entry,
    detection_event,
    setup_entry,
    wait_until,
)
from eufy_home_security import (
    ClipWriter,
    DetectionType,
    DeviceTimeoutError,
    EufySecurity,
    HistoryRecord,
    MediaClip,
    Station,
    redact_serial,
)
from eufy_home_security.testing import FakeStation
from fake_ffmpeg import REMUX_MAGIC
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.eufy_home_security import history, recordings
from custom_components.eufy_home_security.const import (
    CONF_EVENT_HISTORY_DAYS,
    CONF_EVENT_VIDEOS,
    RECORDING_DEFAULT_CLIP_SECONDS,
    RECORDING_MAX_ATTEMPTS,
    RECORDING_SYNC_FIRST_DELAY_SECONDS,
    RECORDING_SYNC_INTERVAL_SECONDS,
)

CAMERA_NAME: Final = "Front"
# A history row's record id: its day (YYYYMMDD) times 100000 plus a sequence.
_DAY_FACTOR: Final = 100_000
_ROW_TIME: Final = "%Y-%m-%d %H:%M:%S"
VIDEOS_ON: Final = {CONF_EVENT_VIDEOS: True}


def _row(
    counter: int, *, started: datetime | None = None, length_s: int = 10, **extra: Any
) -> dict[str, Any]:
    """A history row of the synthetic camera with a recording, in the host's local time."""
    start = (started or datetime.now().astimezone() - timedelta(minutes=5)).replace(microsecond=0)
    return {
        "record_id": int(start.strftime("%Y%m%d")) * _DAY_FACTOR + counter,
        "device_sn": SYNTHETIC.camera_sn,
        "start_time": start.strftime(_ROW_TIME),
        "end_time": (start + timedelta(seconds=length_s)).strftime(_ROW_TIME),
        "storage_path": f"/zx/hdd_data0/Camera00/{counter}.zxvideo",
        "frame_num": 6,
        **extra,
    }


def _videos(hass: HomeAssistant) -> list[Path]:
    root = history.history_dir(hass)
    return sorted(root.rglob("*.mp4")) if root.is_dir() else []


def _manager(entry: MockConfigEntry) -> recordings.RecordingManager:
    manager = entry.runtime_data.recordings
    assert manager is not None
    return manager


def _passes(entry: MockConfigEntry) -> int:
    stats = _manager(entry).stats(SYNTHETIC.station_sn)
    assert stats is not None
    passes: int = stats["passes"]
    return passes


def seed_since(hass_storage: dict[str, Any], entry: MockConfigEntry, since: datetime) -> None:
    """Seed the entry's Store as the event-videos option switched on at ``since`` left it."""
    key = recordings.store_key(entry.entry_id)
    hass_storage[key] = {
        "version": 1,
        "minor_version": 1,
        "key": key,
        "data": {"records": {}, "since": since.isoformat()},
    }


async def _set_up(
    hass: HomeAssistant,
    seed_warm_cache: Callable[..., None],
    options: dict[str, Any],
    hass_storage: dict[str, Any] | None = None,
    *,
    since: datetime | None = None,
) -> MockConfigEntry:
    """Set up an entry; with ``hass_storage`` the option was switched on at ``since``
    (an hour ago by default)."""
    seed_warm_cache()
    entry = add_entry(hass, options=options)
    if hass_storage is not None:
        seed_since(hass_storage, entry, since or dt_util.utcnow() - timedelta(hours=1))
    assert await setup_entry(hass, entry)
    return entry


async def _pass(
    hass: HomeAssistant, entry: MockConfigEntry, seconds: float, passes: int = 1
) -> None:
    """Move the clock ``seconds`` on and wait for the station's sync to finish ``passes``."""
    stats = _manager(entry).stats(SYNTHETIC.station_sn)
    assert stats is not None
    before = stats["passes"]
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()

    def done() -> bool:
        now = _manager(entry).stats(SYNTHETIC.station_sn)
        return now is not None and now["passes"] >= before + passes and not _manager(entry).busy

    await wait_until(done)
    await hass.async_block_till_done()


async def _first_pass(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await _pass(hass, entry, RECORDING_SYNC_FIRST_DELAY_SECONDS + 1)


def _count_downloads(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Record the id of every recording the station is asked to download."""
    calls: list[int] = []
    real = Station.async_download_recording

    async def counting(
        self: Station, record: HistoryRecord, write: ClipWriter, *, wait: bool = True
    ) -> MediaClip:
        calls.append(record.record_id)
        return await real(self, record, write, wait=wait)

    monkeypatch.setattr(Station, "async_download_recording", counting)
    return calls


async def _unload(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_pass_stores_settled_recordings_and_skips_running_ones(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_storage: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A finished recording lands as ``<start>_<camera>_event.mp4``; one still recording waits.

    Nothing is listed before the first pass, so setup itself sends no history query.
    """
    downloads = _count_downloads(monkeypatch)
    settled = _row(1, started=datetime.now().astimezone() - timedelta(minutes=10))
    running = _row(2, started=datetime.now().astimezone() - timedelta(seconds=5))
    fake_station.rows = [running, settled]
    entry = await _set_up(hass, seed_warm_cache, VIDEOS_ON, hass_storage)
    assert fake_station.history_queries == []

    await _first_pass(hass, entry)

    assert downloads == [settled["record_id"]]
    (video,) = _videos(hass)
    started = HistoryRecord.from_row(settled).started_at
    assert started is not None
    assert video.name == (
        f"{dt_util.as_local(started).strftime('%Y-%m-%d_%H-%M-%S')}_{CAMERA_NAME}_event.mp4"
    )
    assert video.read_bytes().startswith(REMUX_MAGIC + b"\x47")
    stats = _manager(entry).stats(SYNTHETIC.station_sn)
    assert stats is not None
    assert (stats["stored"], stats["kept"], stats["failed"]) == (1, 1, 0)
    await _unload(hass, entry)


async def test_recordings_are_stored_oldest_first(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_storage: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rows are listed newest first; they are downloaded one at a time, oldest first."""
    downloads = _count_downloads(monkeypatch)
    now = datetime.now().astimezone()
    # The station numbers rows in order, so the newest has the highest id.
    rows = [_row(10 - n, started=now - timedelta(minutes=10 * n)) for n in (1, 2, 3)]
    fake_station.rows = rows
    entry = await _set_up(hass, seed_warm_cache, VIDEOS_ON, hass_storage)

    await _first_pass(hass, entry)

    assert downloads == [rows[2]["record_id"], rows[1]["record_id"], rows[0]["record_id"]]
    assert len(_videos(hass)) == 3
    await _unload(hass, entry)


async def test_a_recording_is_named_after_its_detections_still(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_storage: dict[str, Any],
) -> None:
    """The video takes the stamp and kind of the still its detection wrote, so they pair.

    The detection's own time differs from the recording's start by design here: the
    name follows the still, not the row.
    """
    row = _row(7, started=datetime.now().astimezone() - timedelta(minutes=10))
    fake_station.rows = [row]
    entry = await _set_up(hass, seed_warm_cache, VIDEOS_ON, hass_storage)
    t_ms = int((dt_util.utcnow() - timedelta(minutes=9, seconds=57)).timestamp() * 1000)
    entry.runtime_data.router.handle(
        detection_event(
            DetectionType.PERSON,
            t_ms=t_ms,
            thumb_path=PUSHED_THUMB_PATH,
            record_id=row["record_id"],
        )
    )
    snapshots = entry.runtime_data.snapshots
    await wait_until(lambda: snapshots.image_for(SYNTHETIC.camera_sn) is not None)
    await wait_until(lambda: not snapshots.busy)
    await hass.async_block_till_done()
    stills = sorted(history.history_dir(hass).rglob("*.jpg"))
    stamp = dt_util.as_local(dt_util.utc_from_timestamp(t_ms / 1000))
    assert [still.name for still in stills] == [
        f"{stamp.strftime('%Y-%m-%d_%H-%M-%S')}_{CAMERA_NAME}_person.jpg"
    ]

    await _first_pass(hass, entry)

    (video,) = _videos(hass)
    assert video.stem == stills[0].stem
    await _unload(hass, entry)


async def test_a_detection_schedules_one_pass_after_its_clip(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_storage: dict[str, Any],
) -> None:
    """Two detections close together cost one pass, a clip length plus the settle time later."""
    entry = await _set_up(hass, seed_warm_cache, VIDEOS_ON, hass_storage)
    await _first_pass(hass, entry)
    fake_station.rows = [_row(3, started=datetime.now().astimezone() - timedelta(minutes=10))]
    for offset in (2_000, 1_000):
        entry.runtime_data.router.handle(
            detection_event(
                DetectionType.PERSON,
                t_ms=int(dt_util.utcnow().timestamp() * 1000) - offset,
                thumb_path=PUSHED_THUMB_PATH,
                record_id=fake_station.rows[0]["record_id"],
            )
        )
    await hass.async_block_till_done()
    passes = _passes(entry)

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=20))
    await hass.async_block_till_done()
    assert _passes(entry) == passes

    await _pass(hass, entry, RECORDING_DEFAULT_CLIP_SECONDS + 31)

    assert _passes(entry) == passes + 1
    assert len(_videos(hass)) == 1
    await _unload(hass, entry)


async def test_stored_recordings_survive_a_restart_and_a_deleted_file_stays_deleted(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_storage: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stored record id is never downloaded again: not on the next pass, not after a
    reload, not when the user deleted its file."""
    downloads = _count_downloads(monkeypatch)
    fake_station.rows = [_row(4, started=datetime.now().astimezone() - timedelta(minutes=10))]
    entry = await _set_up(hass, seed_warm_cache, VIDEOS_ON, hass_storage)
    await _first_pass(hass, entry)
    assert len(downloads) == 1

    await _pass(hass, entry, RECORDING_SYNC_INTERVAL_SECONDS + 1)
    assert len(downloads) == 1

    (video,) = _videos(hass)
    video.unlink()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await _first_pass(hass, entry)

    assert len(downloads) == 1
    assert _videos(hass) == []
    await _unload(hass, entry)


async def test_a_failed_download_is_retried_three_times_then_given_up_with_one_warning(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_storage: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Each pass tries again until the third failure; then one warning per camera, which
    names no serial, however many of its recordings were given up."""
    calls: list[int] = []

    async def failing(
        self: Station, record: HistoryRecord, write: ClipWriter, *, wait: bool = True
    ) -> MediaClip:
        calls.append(record.record_id)
        raise DeviceTimeoutError("no keyframe")

    monkeypatch.setattr(Station, "async_download_recording", failing)
    now = datetime.now().astimezone()
    fake_station.rows = [
        _row(6, started=now - timedelta(minutes=10)),
        _row(5, started=now - timedelta(minutes=20)),
    ]
    entry = await _set_up(hass, seed_warm_cache, VIDEOS_ON, hass_storage)
    caplog.set_level(logging.WARNING)

    await _first_pass(hass, entry)
    for _ in range(RECORDING_MAX_ATTEMPTS + 1):
        await _pass(hass, entry, RECORDING_SYNC_INTERVAL_SECONDS + 1)

    assert len(calls) == 2 * RECORDING_MAX_ATTEMPTS
    warnings = [r for r in caplog.records if "could not be saved" in r.getMessage()]
    assert len(warnings) == 1
    assert SYNTHETIC.camera_sn not in warnings[0].getMessage()
    stats = _manager(entry).stats(SYNTHETIC.station_sn)
    assert stats is not None
    assert (stats["failed"], stats["given_up"], stats["kept"]) == (2 * RECORDING_MAX_ATTEMPTS, 2, 0)
    assert _videos(hass) == []
    assert not [p for p in history.history_dir(hass).rglob("*") if p.is_file()]
    await _unload(hass, entry)


async def test_an_incomplete_recording_keeps_nothing_and_is_retried(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_storage: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clip with fewer frames than its row counts is dropped; once whole, it is stored."""
    downloads = _count_downloads(monkeypatch)
    fake_station.recording_frames = 6
    row = _row(6, started=datetime.now().astimezone() - timedelta(minutes=10), frame_num=40)
    fake_station.rows = [row]
    entry = await _set_up(hass, seed_warm_cache, VIDEOS_ON, hass_storage)

    await _first_pass(hass, entry)
    assert len(downloads) == 1
    assert _videos(hass) == []

    fake_station.rows = [{**row, "frame_num": 6}]
    await _pass(hass, entry, RECORDING_SYNC_INTERVAL_SECONDS + 1)

    assert len(downloads) == 2
    assert len(_videos(hass)) == 1
    await _unload(hass, entry)


async def test_a_listing_that_never_answers_ends_the_pass_and_the_next_pass_lists_again(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_storage: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The sync bounds its listing as a request does; one line says why, and the next
    pass stores what the hung one missed."""
    monkeypatch.setattr(recordings, "LIST_TIMEOUT_SECONDS", 0.05)
    real = Station.async_list_recordings
    hang = True

    async def listing(self: Station, *args: Any, **kwargs: Any) -> list[HistoryRecord]:
        if hang:
            await asyncio.Event().wait()
        return await real(self, *args, **kwargs)

    monkeypatch.setattr(Station, "async_list_recordings", listing)
    fake_station.rows = [_row(3, started=datetime.now().astimezone() - timedelta(minutes=10))]
    entry = await _set_up(hass, seed_warm_cache, VIDEOS_ON, hass_storage)

    with caplog.at_level(logging.DEBUG, logger=recordings.__name__):
        await _first_pass(hass, entry)

    stats = _manager(entry).stats(SYNTHETIC.station_sn)
    assert stats is not None
    assert (stats["passes"], stats["last_error"]) == (1, "TimeoutError")
    failed = [r for r in caplog.records if "listing failed" in r.getMessage()]
    assert len(failed) == 1
    assert _videos(hass) == []

    hang = False
    await _pass(hass, entry, RECORDING_SYNC_INTERVAL_SECONDS + 1)

    assert len(_videos(hass)) == 1
    await _unload(hass, entry)


@pytest.mark.parametrize(
    "options",
    [{}, {CONF_EVENT_VIDEOS: False}, {CONF_EVENT_VIDEOS: True, CONF_EVENT_HISTORY_DAYS: 0}],
    ids=["default", "off", "history-off"],
)
async def test_no_sync_with_the_option_off_or_the_history_off(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_storage: dict[str, Any],
    options: dict[str, Any],
) -> None:
    """Off by default; on with no history days nothing is synced either."""
    fake_station.rows = [_row(8, started=datetime.now().astimezone() - timedelta(minutes=10))]
    entry = await _set_up(hass, seed_warm_cache, options)
    assert entry.runtime_data.recordings is None

    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=RECORDING_SYNC_INTERVAL_SECONDS + 1)
    )
    await hass.async_block_till_done()

    assert fake_station.history_queries == []
    assert _videos(hass) == []
    await _unload(hass, entry)


async def test_diagnostics_show_the_sync_counters_and_the_downloads(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_storage: dict[str, Any],
) -> None:
    """Each HomeBase carries its sync's counters; the session counts the download."""
    fake_station.rows = [_row(9, started=datetime.now().astimezone() - timedelta(minutes=10))]
    entry = await _set_up(hass, seed_warm_cache, VIDEOS_ON, hass_storage)
    await _first_pass(hass, entry)

    data = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    station = data["stations"][redact_serial(SYNTHETIC.station_sn)]
    sync = station["recording_sync"]
    assert (sync["passes"], sync["stored"], sync["kept"], sync["failed"]) == (1, 1, 1, 0)
    assert station["session"]["recording_downloads"] == 1
    await _unload(hass, entry)
