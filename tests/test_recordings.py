"""Event videos (``recordings.py``): each HomeBase recording copied into the history.

End to end on the library's loopback ``FakeStation``: its ``rows`` are the station's
history, its recordings play ``recording_frames`` frames, and the clip's MPEG-TS is
remuxed by ``tests/fake_ffmpeg.py`` (conftest's autouse ``_fake_ffmpeg``). Passes are
started by moving Home Assistant's clock past the sync's timers. Unless a test says
otherwise the option was switched on an hour before setup (the Store's ``since``).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import time
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest
from conftest import (
    PUSHED_THUMB_PATH,
    SYNTHETIC,
    add_entry,
    detection_event,
    entity_id_for,
    setup_entry,
    wait_until,
)
from eufy_home_security import (
    ClipWriter,
    CommunicationError,
    DetectionType,
    DeviceTimeoutError,
    E2EKeyType,
    EndToEndEncryptedError,
    EufySecurity,
    HistoryRecord,
    MediaClip,
    Station,
    redact_serial,
)
from eufy_home_security.testing import FakeCloud, FakeStation, camera_device
from fake_ffmpeg import REMUX_MAGIC
from homeassistant.components.camera import DOMAIN as CAMERA_DOMAIN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant.typing import (
    ClientSessionGenerator,
    WebSocketGenerator,
)

from custom_components.eufy_home_security import errors, history, recordings, station_recordings
from custom_components.eufy_home_security.const import (
    CAMERA_KEY,
    CONF_EVENT_HISTORY_DAYS,
    CONF_EVENT_VIDEOS,
    DOMAIN,
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


_OTHER_STATION: Final = "T8030P2000099999"
_OTHER_CAMERA: Final = "T8113P2000099999"


def _seed_records(
    hass_storage: dict[str, Any],
    entry: MockConfigEntry,
    records: dict[str, dict[str, Any]],
    *,
    minor_version: int = 2,
) -> None:
    """Seed the entry's Store with ``records``, the option switched on an hour ago."""
    key = recordings.store_key(entry.entry_id)
    hass_storage[key] = {
        "version": 1,
        "minor_version": minor_version,
        "key": key,
        "data": {
            "records": records,
            "since": (dt_util.utcnow() - timedelta(hours=1)).isoformat(),
        },
    }


def _started(row: dict[str, Any]) -> datetime:
    started = HistoryRecord.from_row(row).started_at
    assert started is not None
    return started


def _record(station_sn: str, device_sn: str, started: datetime) -> dict[str, Any]:
    return {
        "file": "2026-01-01_00-00-00_Other_event.mp4",
        "folder": "Other",
        "station": station_sn,
        "device": device_sn,
        "started": started.isoformat(),
    }


async def test_a_record_of_another_station_or_start_under_the_same_id_is_not_stored(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_storage: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Record ids are per station: another station's record under the same id, and this
    station's record of an earlier recording under a reused id, leave the row to be
    downloaded; the record of the same recording does not."""
    downloads = _count_downloads(monkeypatch)
    now = datetime.now().astimezone()
    rows = [_row(n, started=now - timedelta(minutes=10 * n)) for n in (1, 2, 3)]
    fake_station.rows = rows
    starts = [_started(row) for row in rows]
    seed_warm_cache()
    entry = add_entry(hass, options=VIDEOS_ON)
    ours = SYNTHETIC.station_sn
    _seed_records(
        hass_storage,
        entry,
        {
            # Another station's recording under row 1's id.
            recordings.record_key(_OTHER_STATION, rows[0]["record_id"]): _record(
                _OTHER_STATION, _OTHER_CAMERA, starts[0]
            ),
            # An earlier recording of this station under row 2's id.
            recordings.record_key(ours, rows[1]["record_id"]): _record(
                ours, SYNTHETIC.camera_sn, starts[1] + timedelta(minutes=1)
            ),
            # Row 3 itself.
            recordings.record_key(ours, rows[2]["record_id"]): _record(
                ours, SYNTHETIC.camera_sn, starts[2]
            ),
        },
    )
    assert await setup_entry(hass, entry)

    await _first_pass(hass, entry)

    assert downloads == [rows[1]["record_id"], rows[0]["record_id"]]
    await _unload(hass, entry)
    records = hass_storage[recordings.store_key(entry.entry_id)]["data"]["records"]
    assert records[recordings.record_key(_OTHER_STATION, rows[0]["record_id"])]["station"] == (
        _OTHER_STATION
    )
    for row, started in zip(rows[:2], starts[:2], strict=True):
        record = records[recordings.record_key(ours, row["record_id"])]
        assert (record["device"], record["started"]) == (SYNTHETIC.camera_sn, started.isoformat())


async def test_a_store_keyed_by_record_id_alone_is_keyed_by_station_on_load(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_storage: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Minor version 1: each record moves under its noted station, so a recording stored
    before the change is not downloaded again; a key already its station's stays; a
    record naming no station, or keyed for another one, is dropped."""
    downloads = _count_downloads(monkeypatch)
    row = _row(4, started=datetime.now().astimezone() - timedelta(minutes=10))
    fake_station.rows = [row]
    started = _started(row)
    seed_warm_cache()
    entry = add_entry(hass, options=VIDEOS_ON)
    stationless = _record(SYNTHETIC.station_sn, SYNTHETIC.camera_sn, started)
    del stationless["station"]
    _seed_records(
        hass_storage,
        entry,
        {
            str(row["record_id"]): _record(SYNTHETIC.station_sn, SYNTHETIC.camera_sn, started),
            # Saved back as read by a release on minor 1: stays.
            recordings.record_key(SYNTHETIC.station_sn, 88): _record(
                SYNTHETIC.station_sn, SYNTHETIC.camera_sn, started
            ),
            # Another station's key under this station's record: dropped.
            recordings.record_key(_OTHER_STATION, 89): _record(
                SYNTHETIC.station_sn, SYNTHETIC.camera_sn, started
            ),
            "77": stationless,
            "not a record id": _record(SYNTHETIC.station_sn, SYNTHETIC.camera_sn, started),
        },
        minor_version=1,
    )
    assert await setup_entry(hass, entry)

    await _first_pass(hass, entry)

    assert downloads == []
    await _unload(hass, entry)
    document = hass_storage[recordings.store_key(entry.entry_id)]
    assert document["minor_version"] == 2
    assert sorted(document["data"]["records"]) == sorted(
        [
            recordings.record_key(SYNTHETIC.station_sn, row["record_id"]),
            recordings.record_key(SYNTHETIC.station_sn, 88),
        ]
    )


async def test_a_store_document_that_is_no_mapping_loads_empty(
    hass: HomeAssistant,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_storage: dict[str, Any],
) -> None:
    """A minor-1 document whose data is not a mapping: the entry still sets up."""
    seed_warm_cache()
    entry = add_entry(hass, options=VIDEOS_ON)
    key = recordings.store_key(entry.entry_id)
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": "garbled"}

    assert await setup_entry(hass, entry)

    assert _manager(entry).stats(SYNTHETIC.station_sn) is not None
    await _unload(hass, entry)


async def test_a_still_names_only_its_own_cameras_recording(
    hass: HomeAssistant,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Stills are noted per camera: another camera's still under the same record id does
    not name this camera's recording."""
    seed_warm_cache()
    entry = add_entry(hass, options=VIDEOS_ON)
    assert await setup_entry(hass, entry)
    events: history.EventHistory = entry.runtime_data.history
    moment = dt_util.utcnow()

    events.async_save(_OTHER_CAMERA, b"\xff\xd8", moment, "person", record_id=5)

    assert events.still_name(SYNTHETIC.camera_sn, 5) is None
    assert events.still_name(_OTHER_CAMERA, 5) == (dt_util.as_local(moment), "person")
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


@pytest.mark.parametrize(
    ("changes", "frame_num", "tries", "phrase"),
    [
        ({"ended_early": True}, 6, 1, "after 1 attempt (cut at a video-codec change)"),
        ({}, 40, RECORDING_MAX_ATTEMPTS, "after 3 attempts (incomplete: frames missing)"),
        (
            {"expected_frames": None, "end_frame": False},
            6,
            RECORDING_MAX_ATTEMPTS,
            "after 3 attempts (incomplete: the station never ended the download)",
        ),
    ],
    ids=["cut-at-codec-change", "short", "no-end-frame"],
)
async def test_a_recording_cut_at_a_codec_change_is_given_up_at_once(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_storage: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    changes: dict[str, Any],
    frame_num: int,
    tries: int,
    phrase: str,
) -> None:
    """A download cut at a codec change is final: not tried again, one warning naming
    why; a short one, or one the station never ended, is tried until the attempts run
    out, and the warning says which."""
    calls: list[int] = []
    real = Station.async_download_recording

    async def download(
        self: Station, record: HistoryRecord, write: ClipWriter, *, wait: bool = True
    ) -> MediaClip:
        calls.append(record.record_id)
        clip = await real(self, record, write, wait=wait)
        return dataclasses.replace(clip, **changes)

    monkeypatch.setattr(Station, "async_download_recording", download)
    fake_station.recording_frames = 6
    fake_station.rows = [
        _row(7, started=datetime.now().astimezone() - timedelta(minutes=10), frame_num=frame_num)
    ]
    entry = await _set_up(hass, seed_warm_cache, VIDEOS_ON, hass_storage)
    caplog.set_level(logging.WARNING)

    await _first_pass(hass, entry)
    for _ in range(RECORDING_MAX_ATTEMPTS + 1):
        await _pass(hass, entry, RECORDING_SYNC_INTERVAL_SECONDS + 1)

    assert len(calls) == tries
    (warning,) = [r for r in caplog.records if "could not be saved" in r.getMessage()]
    assert phrase in warning.getMessage()
    stats = _manager(entry).stats(SYNTHETIC.station_sn)
    assert stats is not None
    assert (stats["failed"], stats["given_up"], stats["kept"]) == (tries, 1, 0)
    assert _videos(hass) == []
    await _unload(hass, entry)


@pytest.mark.parametrize(
    ("ended_early", "frame_num", "code", "downloads"),
    [(True, 6, "codec_change", 0), (False, 40, "failed", 1)],
    ids=["cut-at-codec-change", "short"],
)
async def test_a_fetch_of_a_recording_the_sync_gave_up_at_a_codec_change_downloads_nothing(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_storage: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    ended_early: bool,
    frame_num: int,
    code: str,
    downloads: int,
) -> None:
    """The sync noted why it gave up: a request for a recording cut at a codec change
    is refused at once; one given up for another reason is downloaded again."""
    calls: list[int] = []
    real = Station.async_download_recording

    async def download(
        self: Station, record: HistoryRecord, write: ClipWriter, *, wait: bool = True
    ) -> MediaClip:
        calls.append(record.record_id)
        clip = await real(self, record, write, wait=wait)
        return dataclasses.replace(clip, ended_early=ended_early)

    monkeypatch.setattr(Station, "async_download_recording", download)
    fake_station.recording_frames = 6
    fake_station.rows = [
        _row(7, started=datetime.now().astimezone() - timedelta(minutes=10), frame_num=frame_num)
    ]
    entry = await _set_up(hass, seed_warm_cache, VIDEOS_ON, hass_storage)
    await _first_pass(hass, entry)
    for _ in range(RECORDING_MAX_ATTEMPTS):
        await _pass(hass, entry, RECORDING_SYNC_INTERVAL_SECONDS + 1)
    stats = _manager(entry).stats(SYNTHETIC.station_sn)
    assert stats is not None
    assert stats["given_up"] == 1
    synced = len(calls)

    client = await hass_ws_client(hass)
    camera = entity_id_for(hass, CAMERA_DOMAIN, SYNTHETIC.camera_sn, CAMERA_KEY)
    await client.send_json_auto_id({"type": station_recordings.WS_LIST, "entity_id": camera})
    listed = await client.receive_json()
    assert listed["success"], listed
    (ref,) = [r["ref"] for r in listed["result"]["recordings"]]
    await client.send_json_auto_id(
        {"type": station_recordings.WS_FETCH, "entity_id": camera, "ref": ref}
    )
    reply = await client.receive_json()

    assert not reply["success"], reply
    assert reply["error"]["code"] == code
    assert len(calls) - synced == downloads
    assert _videos(hass) == []
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
    failed = [r for r in caplog.records if "failed (TimeoutError)" in r.getMessage()]
    assert len(failed) == 1
    assert _videos(hass) == []

    hang = False
    await _pass(hass, entry, RECORDING_SYNC_INTERVAL_SECONDS + 1)

    assert len(_videos(hass)) == 1
    await _unload(hass, entry)


def _record_listings(monkeypatch: pytest.MonkeyPatch) -> list[tuple[date, int]]:
    """Record each sync listing's ``until`` day and ``days``; it never passes ``since``."""
    calls: list[tuple[date, int]] = []
    real = Station.async_list_recordings

    async def listing(self: Station, *args: Any, **kwargs: Any) -> list[HistoryRecord]:
        assert "since" not in kwargs
        calls.append((kwargs["until"], kwargs["days"]))
        return await real(self, *args, **kwargs)

    monkeypatch.setattr(Station, "async_list_recordings", listing)
    return calls


def _host_days(since: datetime) -> list[date]:
    """Every host-local day from ``since``'s to today."""
    day, today = since.astimezone().date(), datetime.now().astimezone().date()
    return [day + timedelta(days=n) for n in range((today - day).days + 1)]


async def test_a_backlog_is_listed_one_day_at_a_time_oldest_first(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_storage: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each listing asks one whole host-local day, oldest first, and that day's
    recordings are stored before the next day is asked, so no request covers the whole
    backlog; a row of the first day before the pass's start is not copied."""
    downloads = _count_downloads(monkeypatch)
    calls = _record_listings(monkeypatch)
    noon = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
    since = noon - timedelta(days=3)
    rows = [_row(n + 1, started=noon - timedelta(days=n) + timedelta(hours=1)) for n in (3, 2, 1)]
    rows.append(_row(9, started=datetime.now().astimezone() - timedelta(minutes=10)))
    before_start = _row(8, started=since - timedelta(hours=1))
    fake_station.rows = [*reversed(rows), before_start]
    entry = await _set_up(hass, seed_warm_cache, VIDEOS_ON, hass_storage, since=since)

    await _first_pass(hass, entry)

    days = _host_days(since)
    assert calls == [(day, (days[-1] - day).days + 1) for day in days]
    by_start = sorted(rows, key=_started)
    assert downloads == [row["record_id"] for row in by_start]
    await _unload(hass, entry)


async def test_a_day_that_never_answers_ends_the_pass_and_the_next_pass_starts_at_it(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_storage: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Days before the hung one are stored; the next pass lists from the last finished
    day on (not the days before it), and copies nothing twice."""
    monkeypatch.setattr(recordings, "LIST_TIMEOUT_SECONDS", 0.05)
    downloads = _count_downloads(monkeypatch)
    calls = _record_listings(monkeypatch)
    noon = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
    since = noon - timedelta(days=2)
    rows = [_row(n + 1, started=noon - timedelta(days=n) + timedelta(hours=1)) for n in (2, 1)]
    rows.append(_row(9, started=datetime.now().astimezone() - timedelta(minutes=10)))
    fake_station.rows = list(reversed(rows))
    hung = _host_days(since)[1]
    recorded = Station.async_list_recordings
    hang = True

    async def listing(self: Station, *args: Any, **kwargs: Any) -> list[HistoryRecord]:
        if hang and kwargs["until"] == hung:
            await asyncio.Event().wait()
        return await recorded(self, *args, **kwargs)

    monkeypatch.setattr(Station, "async_list_recordings", listing)
    entry = await _set_up(hass, seed_warm_cache, VIDEOS_ON, hass_storage, since=since)

    await _first_pass(hass, entry)

    stats = _manager(entry).stats(SYNTHETIC.station_sn)
    assert stats is not None
    assert stats["last_error"] == "TimeoutError"
    assert downloads == [rows[0]["record_id"]]
    # The hung day never reaches the recorder below the hang.
    assert [until for until, _days in calls] == _host_days(since)[:1]

    hang = False
    calls.clear()
    await _pass(hass, entry, RECORDING_SYNC_INTERVAL_SECONDS + 1)

    assert [until for until, _days in calls[:2]] == [_host_days(since)[0], hung]
    assert downloads == [row["record_id"] for row in sorted(rows, key=_started)]
    await _unload(hass, entry)


async def test_a_recording_still_running_on_an_earlier_day_keeps_the_next_pass_there(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_storage: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recording running on the first day keeps the next pass at that day, although a
    later day (with nothing) finished before the pass stopped."""
    monkeypatch.setattr(recordings, "LIST_TIMEOUT_SECONDS", 0.05)
    calls = _record_listings(monkeypatch)
    noon = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
    since = noon - timedelta(days=2)
    long_running = _row(1, started=since + timedelta(hours=1), length_s=3 * 86_400)
    fake_station.rows = [long_running]
    days = _host_days(since)
    recorded = Station.async_list_recordings
    hang = True

    async def listing(self: Station, *args: Any, **kwargs: Any) -> list[HistoryRecord]:
        if hang and kwargs["until"] == days[-1]:
            await asyncio.Event().wait()
        return await recorded(self, *args, **kwargs)

    monkeypatch.setattr(Station, "async_list_recordings", listing)
    entry = await _set_up(hass, seed_warm_cache, VIDEOS_ON, hass_storage, since=since)
    await _first_pass(hass, entry)
    assert [until for until, _days in calls] == days[:2]

    hang = False
    calls.clear()
    await _pass(hass, entry, RECORDING_SYNC_INTERVAL_SECONDS + 1)

    assert calls[0][0] == days[0]
    await _unload(hass, entry)


async def test_a_failed_day_after_a_running_recording_still_follows_it_up(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_storage: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recording on the first day ends in a minute, today's listing fails: a pass is
    still scheduled for when it has settled, not only the 15-minute catch-up."""
    noon = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
    since = noon - timedelta(days=1)
    started = since + timedelta(hours=1)
    ends_soon = int((datetime.now().astimezone() - started).total_seconds()) + 60
    fake_station.rows = [_row(1, started=started, length_s=ends_soon)]
    today = _host_days(since)[-1]
    real = Station.async_list_recordings

    async def listing(self: Station, *args: Any, **kwargs: Any) -> list[HistoryRecord]:
        if kwargs["until"] == today:
            raise DeviceTimeoutError("no answer")
        return await real(self, *args, **kwargs)

    monkeypatch.setattr(Station, "async_list_recordings", listing)
    entry = await _set_up(hass, seed_warm_cache, VIDEOS_ON, hass_storage, since=since)

    await _first_pass(hass, entry)

    sync = _manager(entry)._syncs[SYNTHETIC.station_sn]
    assert sync.soon_at is not None
    assert sync.soon_at - time.monotonic() < RECORDING_SYNC_INTERVAL_SECONDS
    await _unload(hass, entry)


@pytest.fixture
def host_utc(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The host's local zone is UTC for the test."""
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def _station_row(counter: int, local: datetime, offset: str) -> dict[str, Any]:
    """A row in the station's local time ``local`` (naive) with its ``time_zone``."""
    return {
        "record_id": int(local.strftime("%Y%m%d")) * _DAY_FACTOR + counter,
        "device_sn": SYNTHETIC.camera_sn,
        "start_time": local.strftime(_ROW_TIME),
        "end_time": (local + timedelta(seconds=10)).strftime(_ROW_TIME),
        "time_zone": offset,
        "storage_path": f"/zx/hdd_data0/Camera00/{counter}.zxvideo",
        "frame_num": 6,
    }


@pytest.mark.usefixtures("host_utc")
async def test_a_recording_early_in_a_station_day_ahead_of_the_host_is_copied(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_storage: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A station two hours ahead of the host: its day starts before host midnight, and a
    recording at 00:30 station time on an earlier day of the backlog is still copied."""
    downloads = _count_downloads(monkeypatch)
    midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    since = (datetime.now(UTC) - timedelta(days=3)).replace(
        hour=12, minute=0, second=0, microsecond=0
    )
    early = _station_row(1, midnight - timedelta(days=2) + timedelta(minutes=30), "+0200")
    later = _station_row(2, midnight - timedelta(days=1) + timedelta(hours=13), "+0200")
    fake_station.rows = [later, early]
    entry = await _set_up(hass, seed_warm_cache, VIDEOS_ON, hass_storage, since=since)

    await _first_pass(hass, entry)

    assert downloads == [early["record_id"], later["record_id"]]
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


# A history row's ``cipher_id`` for the user's PIN-protected key (type 4) with key id 10.
PIN_CIPHER_ID: Final = (4 << 24) | 10


async def test_a_pin_protected_recording_is_given_up_at_once_without_the_warning(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_storage: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The library refuses a recording under the PIN-protected key: tried once, never
    again, counted apart from the other give-ups, and no "could not be saved" warning;
    a plain recording beside it is stored."""
    downloads = _count_downloads(monkeypatch)
    now = datetime.now().astimezone()
    pin = _row(6, started=now - timedelta(minutes=20), cipher_id=PIN_CIPHER_ID)
    plain = _row(7, started=now - timedelta(minutes=10))
    fake_station.rows = [plain, pin]
    entry = await _set_up(hass, seed_warm_cache, VIDEOS_ON, hass_storage)
    caplog.set_level(logging.WARNING)

    await _first_pass(hass, entry)
    for _ in range(RECORDING_MAX_ATTEMPTS + 1):
        await _pass(hass, entry, RECORDING_SYNC_INTERVAL_SECONDS + 1)

    assert downloads == [pin["record_id"], plain["record_id"]]
    # Refused before anything was sent: the station served the plain one only.
    assert _manager(entry)._syncs[SYNTHETIC.station_sn].station.stats().recording_downloads == 1
    assert not [r for r in caplog.records if "could not be saved" in r.getMessage()]
    stats = _manager(entry).stats(SYNTHETIC.station_sn)
    assert stats is not None
    assert (stats["failed"], stats["given_up"], stats["pin_protected"], stats["retrying"]) == (
        1,
        0,
        1,
        0,
    )
    assert stats["last_error"] == "PinProtectedKeyError"
    assert len(_videos(hass)) == 1
    await _unload(hass, entry)


@pytest.mark.parametrize(
    ("error", "tries", "phrase"),
    [
        (
            CommunicationError("the cloud did not serve cipher 2"),
            RECORDING_MAX_ATTEMPTS,
            "after 3 attempts (CommunicationError)",
        ),
        (
            EndToEndEncryptedError("no usable key", key_type=E2EKeyType.CIPHER, key_id=2),
            RECORDING_MAX_ATTEMPTS,
            "after 3 attempts (EndToEndEncryptedError)",
        ),
    ],
    ids=["cloud-key-fetch-failed", "cloud-cipher-unusable"],
)
async def test_an_end_to_end_recording_without_the_pin_key_follows_the_usual_give_up(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_storage: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
    tries: int,
    phrase: str,
) -> None:
    """A failed cloud key fetch and a cloud cipher the library could not use (a key fetch
    still running, no usable key) are tried again until the attempts run out, end with
    the usual warning and count as given up, not as PIN-protected."""
    calls: list[int] = []

    async def failing(
        self: Station, record: HistoryRecord, write: ClipWriter, *, wait: bool = True
    ) -> MediaClip:
        calls.append(record.record_id)
        raise error

    monkeypatch.setattr(Station, "async_download_recording", failing)
    fake_station.rows = [_row(6, started=datetime.now().astimezone() - timedelta(minutes=10))]
    entry = await _set_up(hass, seed_warm_cache, VIDEOS_ON, hass_storage)
    caplog.set_level(logging.WARNING)

    await _first_pass(hass, entry)
    for _ in range(RECORDING_MAX_ATTEMPTS + 1):
        await _pass(hass, entry, RECORDING_SYNC_INTERVAL_SECONDS + 1)

    assert len(calls) == tries
    (warning,) = [r for r in caplog.records if "could not be saved" in r.getMessage()]
    assert phrase in warning.getMessage()
    stats = _manager(entry).stats(SYNTHETIC.station_sn)
    assert stats is not None
    assert (stats["failed"], stats["given_up"], stats["pin_protected"]) == (tries, 1, 0)
    await _unload(hass, entry)


# A second camera paired to the station, on channel 1.
_BACK_CAMERA: Final = "T8160P2000000002"


def _pin_issue(
    hass: HomeAssistant, entry: MockConfigEntry, device_sn: str
) -> tuple[dr.DeviceEntry, ir.IssueEntry | None]:
    """The camera's device and its PIN-protected-recordings issue, if raised."""
    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, device_sn), entry.entry_id)
    assert device is not None
    issue_id = errors.recordings_pin_protected_issue_id(entry.entry_id, device.id)
    return device, ir.async_get(hass).async_get_issue(DOMAIN, issue_id)


async def test_a_pin_protected_recording_raises_one_repair_per_camera_until_the_reload(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_storage: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each camera with a recording under the PIN-protected key gets its own warning
    repair naming it; a reload drops them, and the recordings given up before it raise
    nothing again: only a new one does."""
    fake_cloud.devices.append(
        camera_device(_BACK_CAMERA, station_sn=fake_station.serial, channel=1, name="Back *yard*")
    )
    fake_station.params[1] = {1101: "80", 1142: "-60"}
    downloads = _count_downloads(monkeypatch)
    now = datetime.now().astimezone()
    front = _row(6, started=now - timedelta(minutes=20), cipher_id=PIN_CIPHER_ID)
    back = {
        **_row(7, started=now - timedelta(minutes=15), cipher_id=PIN_CIPHER_ID),
        "device_sn": _BACK_CAMERA,
    }
    # Older than the PIN-protected one: storing it does not withdraw the repair.
    plain = _row(8, started=now - timedelta(minutes=25))
    fake_station.rows = [plain, back, front]
    entry = await _set_up(hass, seed_warm_cache, VIDEOS_ON, hass_storage)
    await _first_pass(hass, entry)

    for device_sn, name in ((SYNTHETIC.camera_sn, None), (_BACK_CAMERA, r"Back \*yard\*")):
        device, issue = _pin_issue(hass, entry, device_sn)
        assert issue is not None, device_sn
        assert issue.translation_key == "recordings_pin_protected"
        assert issue.severity is ir.IssueSeverity.WARNING
        assert not issue.is_fixable
        assert not issue.is_persistent
        label = errors.station_label(device)
        # Titles render as plain text, descriptions as markdown.
        assert issue.translation_placeholders == {
            "camera": label,
            "camera_markdown": name or errors.markdown_literal(label),
        }
    assert len(downloads) == 3

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await _first_pass(hass, entry)

    assert _pin_issue(hass, entry, SYNTHETIC.camera_sn)[1] is None
    assert _pin_issue(hass, entry, _BACK_CAMERA)[1] is None
    assert len(downloads) == 3

    fake_station.rows = [
        _row(9, started=now - timedelta(minutes=5), cipher_id=PIN_CIPHER_ID),
        *fake_station.rows,
    ]
    await _pass(hass, entry, RECORDING_SYNC_INTERVAL_SECONDS + 1)

    assert _pin_issue(hass, entry, SYNTHETIC.camera_sn)[1] is not None
    assert _pin_issue(hass, entry, _BACK_CAMERA)[1] is None
    await _unload(hass, entry)
    assert _pin_issue(hass, entry, SYNTHETIC.camera_sn)[1] is None


async def test_diagnostics_count_the_pin_protected_recordings_per_camera(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_storage: dict[str, Any],
) -> None:
    """The sync's diagnostics count each camera's recordings given up for the
    PIN-protected key, from the Store, so the count survives a reload. A camera no longer
    paired is named by its redacted serial; another station's, other give-ups and a
    record naming no camera do not count."""
    now = datetime.now().astimezone()
    fake_station.rows = [
        _row(6, started=now - timedelta(minutes=20), cipher_id=PIN_CIPHER_ID),
        _row(7, started=now - timedelta(minutes=15), cipher_id=PIN_CIPHER_ID),
        _row(8, started=now - timedelta(minutes=10)),
    ]
    entry = add_entry(hass, options=VIDEOS_ON)
    seed_warm_cache()
    seed_since(hass_storage, entry, dt_util.utcnow() - timedelta(hours=1))
    gone = {"file": None, "started": (now - timedelta(minutes=50)).isoformat()}
    hass_storage[recordings.store_key(entry.entry_id)]["data"]["records"] = {
        recordings.record_key(SYNTHETIC.station_sn, 1): {
            **gone,
            "station": SYNTHETIC.station_sn,
            "device": _OTHER_CAMERA,
            "pin_protected": True,
        },
        recordings.record_key(_OTHER_STATION, 2): {
            **gone,
            "station": _OTHER_STATION,
            "device": _OTHER_CAMERA,
            "pin_protected": True,
        },
        recordings.record_key(SYNTHETIC.station_sn, 3): {
            **gone,
            "station": SYNTHETIC.station_sn,
            "device": _OTHER_CAMERA,
        },
        recordings.record_key(SYNTHETIC.station_sn, 4): {
            **gone,
            "station": SYNTHETIC.station_sn,
            "pin_protected": True,
        },
    }
    assert await setup_entry(hass, entry)
    await _first_pass(hass, entry)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    data = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    sync = data["stations"][redact_serial(SYNTHETIC.station_sn)]["recording_sync"]
    assert sync["pin_protected_by_camera"] == {
        redact_serial(SYNTHETIC.camera_sn): 2,
        redact_serial(_OTHER_CAMERA): 1,
    }
    assert sync["pin_protected"] == 0
    await _unload(hass, entry)


async def test_a_pin_protected_refusal_after_a_failed_try_leaves_no_retry_owed(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_storage: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A first try that timed out owes a retry; the refusal for the PIN-protected key on
    the next one settles it: nothing is owed and no later pass tries it."""
    calls: list[int] = []
    real = Station.async_download_recording

    async def download(
        self: Station, record: HistoryRecord, write: ClipWriter, *, wait: bool = True
    ) -> MediaClip:
        calls.append(record.record_id)
        if len(calls) == 1:
            raise DeviceTimeoutError("no keyframe")
        return await real(self, record, write, wait=wait)

    monkeypatch.setattr(Station, "async_download_recording", download)
    fake_station.rows = [
        _row(
            6, started=datetime.now().astimezone() - timedelta(minutes=10), cipher_id=PIN_CIPHER_ID
        )
    ]
    entry = await _set_up(hass, seed_warm_cache, VIDEOS_ON, hass_storage)

    await _first_pass(hass, entry)
    stats = _manager(entry).stats(SYNTHETIC.station_sn)
    assert stats is not None
    assert stats["retrying"] == 1
    for _ in range(RECORDING_MAX_ATTEMPTS):
        await _pass(hass, entry, RECORDING_SYNC_INTERVAL_SECONDS + 1)

    assert len(calls) == 2
    stats = _manager(entry).stats(SYNTHETIC.station_sn)
    assert stats is not None
    assert (stats["retrying"], stats["given_up"], stats["pin_protected"]) == (0, 0, 1)
    await _unload(hass, entry)


async def test_no_pin_protected_repair_for_a_camera_without_a_device(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_storage: dict[str, Any],
) -> None:
    """A serial with no registered device raises and withdraws nothing (the issue is keyed
    by device)."""
    entry = await _set_up(hass, seed_warm_cache, VIDEOS_ON, hass_storage)

    errors.raise_recordings_pin_protected_issue(hass, entry, _OTHER_CAMERA)
    errors.delete_recordings_pin_protected_issue(hass, entry, _OTHER_CAMERA)

    assert not [
        issue_id
        for domain, issue_id in ir.async_get(hass).issues
        if domain == DOMAIN and issue_id.startswith("recordings_pin_protected")
    ]
    await _unload(hass, entry)


def test_the_pin_protected_repair_names_the_camera_plain_in_the_title() -> None:
    """The title takes the raw name (plain text), the description the escaped one."""
    strings = json.loads(
        (Path(recordings.__file__).parent / "strings.json").read_text(encoding="utf-8")
    )
    issue = strings["issues"]["recordings_pin_protected"]
    assert "{camera}" in issue["title"]
    assert "{camera_markdown}" not in issue["title"]
    assert "{camera_markdown}" in issue["description"]
    assert "{camera}" not in issue["description"]


async def test_a_newer_recording_stored_withdraws_the_cameras_pin_repair(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_storage: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recording that started after the camera's last PIN-protected one is stored: the
    encryption is off, so the camera's repair goes in that pass. An older one stored
    later (a retry) proves nothing and leaves it."""
    now = datetime.now().astimezone()
    older = _row(5, started=now - timedelta(minutes=30))
    pin = _row(6, started=now - timedelta(minutes=20), cipher_id=PIN_CIPHER_ID)
    newer = _row(7, started=now - timedelta(minutes=5))
    failed: list[int] = []
    real = Station.async_download_recording

    async def download(
        self: Station, record: HistoryRecord, write: ClipWriter, *, wait: bool = True
    ) -> MediaClip:
        if record.record_id == older["record_id"] and not failed:
            failed.append(record.record_id)
            raise DeviceTimeoutError("no keyframe")
        return await real(self, record, write, wait=wait)

    monkeypatch.setattr(Station, "async_download_recording", download)
    fake_station.rows = [pin, older]
    entry = await _set_up(hass, seed_warm_cache, VIDEOS_ON, hass_storage)

    await _first_pass(hass, entry)
    assert failed
    assert _pin_issue(hass, entry, SYNTHETIC.camera_sn)[1] is not None
    await _pass(hass, entry, RECORDING_SYNC_INTERVAL_SECONDS + 1)
    assert len(_videos(hass)) == 1
    assert _pin_issue(hass, entry, SYNTHETIC.camera_sn)[1] is not None

    fake_station.rows = [newer, pin, older]
    await _pass(hass, entry, RECORDING_SYNC_INTERVAL_SECONDS + 1)

    assert len(_videos(hass)) == 2
    assert _pin_issue(hass, entry, SYNTHETIC.camera_sn)[1] is None
    await _unload(hass, entry)


async def test_a_pin_protected_refusal_does_not_mute_the_cameras_warning(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_storage: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """After a PIN-protected recording of the camera, a later one that fails every try
    still logs the one "could not be saved" warning."""
    now = datetime.now().astimezone()
    pin = _row(6, started=now - timedelta(minutes=20), cipher_id=PIN_CIPHER_ID)
    broken = _row(7, started=now - timedelta(minutes=10))
    real = Station.async_download_recording

    async def download(
        self: Station, record: HistoryRecord, write: ClipWriter, *, wait: bool = True
    ) -> MediaClip:
        if record.record_id == broken["record_id"]:
            raise DeviceTimeoutError("no keyframe")
        return await real(self, record, write, wait=wait)

    monkeypatch.setattr(Station, "async_download_recording", download)
    fake_station.rows = [broken, pin]
    entry = await _set_up(hass, seed_warm_cache, VIDEOS_ON, hass_storage)
    caplog.set_level(logging.WARNING)

    await _first_pass(hass, entry)
    for _ in range(RECORDING_MAX_ATTEMPTS):
        await _pass(hass, entry, RECORDING_SYNC_INTERVAL_SECONDS + 1)

    (warning,) = [r for r in caplog.records if "could not be saved" in r.getMessage()]
    assert "after 3 attempts (DeviceTimeoutError)" in warning.getMessage()
    await _unload(hass, entry)


async def test_a_reload_drops_only_its_own_entrys_pin_repairs(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_storage: dict[str, Any],
) -> None:
    """The reload-scoped cleanup is keyed by the entry: another entry's issue stays."""
    entry = await _set_up(hass, seed_warm_cache, VIDEOS_ON, hass_storage)
    own = errors.recordings_pin_protected_issue_id(entry.entry_id, "device-a")
    other = errors.recordings_pin_protected_issue_id("other-entry", "device-b")
    for issue_id in (own, other):
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="recordings_pin_protected",
        )

    await _unload(hass, entry)

    assert ir.async_get(hass).async_get_issue(DOMAIN, own) is None
    assert ir.async_get(hass).async_get_issue(DOMAIN, other) is not None


async def test_an_older_pin_protected_retry_does_not_move_the_cameras_mark_back(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_storage: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An older PIN-protected recording given up on its retry after a newer one: a plain
    recording between the two, stored on its retry, leaves the repair (the newer one is
    still the latest encrypted)."""
    now = datetime.now().astimezone()
    old_pin = _row(5, started=now - timedelta(minutes=30), cipher_id=PIN_CIPHER_ID)
    between = _row(6, started=now - timedelta(minutes=25))
    new_pin = _row(7, started=now - timedelta(minutes=20), cipher_id=PIN_CIPHER_ID)
    first_tries: set[int] = set()
    real = Station.async_download_recording

    async def download(
        self: Station, record: HistoryRecord, write: ClipWriter, *, wait: bool = True
    ) -> MediaClip:
        if record.record_id in (old_pin["record_id"], between["record_id"]) and (
            record.record_id not in first_tries
        ):
            first_tries.add(record.record_id)
            raise DeviceTimeoutError("no keyframe")
        return await real(self, record, write, wait=wait)

    monkeypatch.setattr(Station, "async_download_recording", download)
    fake_station.rows = [new_pin, between, old_pin]
    entry = await _set_up(hass, seed_warm_cache, VIDEOS_ON, hass_storage)

    await _first_pass(hass, entry)
    await _pass(hass, entry, RECORDING_SYNC_INTERVAL_SECONDS + 1)

    stats = _manager(entry).stats(SYNTHETIC.station_sn)
    assert stats is not None
    assert (stats["pin_protected"], len(_videos(hass))) == (2, 1)
    assert _pin_issue(hass, entry, SYNTHETIC.camera_sn)[1] is not None
    await _unload(hass, entry)


async def test_another_cameras_newer_recording_leaves_the_pin_repair(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_storage: dict[str, Any],
) -> None:
    """Camera A's PIN-protected recording raises A's repair; a newer recording of camera B
    stored after it says nothing about A's encryption and leaves the repair. A newer one
    of A withdraws it."""
    fake_cloud.devices.append(
        camera_device(_BACK_CAMERA, station_sn=fake_station.serial, channel=1, name="Back")
    )
    fake_station.params[1] = {1101: "80", 1142: "-60"}
    now = datetime.now().astimezone()
    pin = _row(6, started=now - timedelta(minutes=20), cipher_id=PIN_CIPHER_ID)
    back = {**_row(7, started=now - timedelta(minutes=10)), "device_sn": _BACK_CAMERA}
    fake_station.rows = [back, pin]
    entry = await _set_up(hass, seed_warm_cache, VIDEOS_ON, hass_storage)

    await _first_pass(hass, entry)

    assert len(_videos(hass)) == 1
    assert _pin_issue(hass, entry, SYNTHETIC.camera_sn)[1] is not None

    fake_station.rows = [_row(8, started=now - timedelta(minutes=5)), back, pin]
    await _pass(hass, entry, RECORDING_SYNC_INTERVAL_SECONDS + 1)

    assert len(_videos(hass)) == 2
    assert _pin_issue(hass, entry, SYNTHETIC.camera_sn)[1] is None
    await _unload(hass, entry)
