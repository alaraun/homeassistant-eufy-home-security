"""The recording of a detection whose push names none (``recording_match.py``).

End to end on the library's loopback ``FakeStation`` as a HomeBase 2: a cloud-push
detection names no recording, ``FakeStation.add_detection_recording`` adds the camera's
row, listed late, running on the host's clock, then settled. The library's re-list
interval and bound are shortened (read at call time), and the settle time the
integration passes is 1.1 s: rows keep whole seconds, so it must exceed 1 s.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest
from conftest import (
    SYNTHETIC,
    add_entry,
    detection_event,
    set_guard_mode,
    setup_entry,
    wait_until,
)
from eufy_home_security import (
    ClipWriter,
    DetectionType,
    DeviceTimeoutError,
    EufySecurity,
    EufySecurityError,
    FrameCipher,
    HistoryRecord,
    MediaClip,
    PushImage,
    PushMessageType,
    PushPictureSource,
    RecordNotFoundError,
    SecurityEvent,
    Station,
    UnsupportedError,
)
from eufy_home_security import station as station_module
from eufy_home_security.p2p import session as session_module
from eufy_home_security.testing import FakeStation
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant.common import MockConfigEntry, async_fire_time_changed

from custom_components.eufy_home_security import history, recording_match, recordings
from custom_components.eufy_home_security.const import (
    CONF_CAMERA_IMAGE,
    CONF_EVENT_VIDEOS,
    RECORDING_SYNC_FIRST_DELAY_SECONDS,
    RECORDING_SYNC_INTERVAL_SECONDS,
    CameraImageMode,
)
from custom_components.eufy_home_security.recording_match import RecordingMatcher
from custom_components.eufy_home_security.snapshots import SnapshotManager

HOMEBASE_2_SN: Final = "T8010" + SYNTHETIC.station_sn[5:]
OTHER_SN: Final = "T8160P2000099999"
QUIET: Final = 1.1
CLIP_SECONDS: Final = 2.0
VIDEOS_ON: Final = {CONF_EVENT_VIDEOS: True}


@pytest.fixture
async def fake_station(request: pytest.FixtureRequest) -> AsyncIterator[FakeStation]:
    """A started loopback station, a HomeBase 2 unless a test passes another serial."""
    station = FakeStation(serial=getattr(request, "param", HOMEBASE_2_SN))
    set_guard_mode(station, station.guard_mode)
    # A download plays this many frames: the fake's rows count 15 per second.
    station.recording_frames = round(CLIP_SECONDS * 15)
    await station.start()
    yield station
    station.stop()


@pytest.fixture(autouse=True)
def short_match(monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-list every 0.2 s, give up 5 s after a detection, settle 1.1 s after a row's end;
    the fake HomeBase 2 answers no storage query: give that up after 0.1 s."""
    monkeypatch.setattr(station_module, "RECORDING_MATCH_RETRY", 0.2)
    monkeypatch.setattr(station_module, "RECORDING_MATCH_GIVE_UP", 5.0)
    monkeypatch.setattr(recording_match, "RECORDING_QUIET", QUIET)
    monkeypatch.setattr(session_module, "STORAGE_TIMEOUT", 0.1)
    monkeypatch.setattr(session_module, "SD_INFO_TIMEOUT", 0.1)


def _start(seconds_ahead: float = 1.0) -> datetime:
    """A whole second ``seconds_ahead`` from now: rows keep no fractions."""
    return datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=seconds_ahead)


def _push(
    at: datetime,
    *,
    session_id: str = "session-1",
    station_sn: str = HOMEBASE_2_SN,
    cipher: FrameCipher | None = None,
    **fields: Any,
) -> SecurityEvent:
    """A HomeBase 2 cloud push of a motion detection at ``at``, naming no recording
    (``cipher`` set: the same detection as a P2P frame)."""
    return detection_event(
        DetectionType.MOTION,
        t_ms=round(at.timestamp() * 1000),
        station_sn=station_sn,
        msg_type=PushMessageType.SECURITY,
        cipher=cipher,
        session_id=session_id,
        **fields,
    )


async def _set_up(
    hass: HomeAssistant,
    seed_warm_cache: Callable[..., None],
    options: dict[str, Any],
    hass_storage: dict[str, Any] | None = None,
) -> MockConfigEntry:
    """Set up an entry; with ``hass_storage`` event videos were switched on an hour ago."""
    seed_warm_cache()
    entry = add_entry(hass, options=options)
    if hass_storage is not None:
        key = recordings.store_key(entry.entry_id)
        since = (dt_util.utcnow() - timedelta(hours=1)).isoformat()
        hass_storage[key] = {
            "version": 1,
            "minor_version": 2,
            "key": key,
            "data": {"records": {}, "since": since},
        }
    assert await setup_entry(hass, entry)
    return entry


def _snapshots(entry: MockConfigEntry) -> SnapshotManager:
    manager = entry.runtime_data.snapshots
    assert manager is not None
    return manager


def _matcher(entry: MockConfigEntry) -> RecordingMatcher:
    matcher = entry.runtime_data.recording_matches
    assert matcher is not None
    return matcher


def _count_frames(monkeypatch: pytest.MonkeyPatch) -> list[int | None]:
    """Record the record id of every trigger frame the station is asked for."""
    calls: list[int | None] = []
    real = Station.async_event_trigger_frame

    async def counting(self: Station, event: SecurityEvent, **kwargs: Any) -> bytes:
        calls.append(event.record_id)
        return await real(self, event, **kwargs)

    monkeypatch.setattr(Station, "async_event_trigger_frame", counting)
    return calls


def _count_thumbnails(monkeypatch: pytest.MonkeyPatch) -> list[int | None]:
    """Record the record id of every detection thumbnail the station is asked for."""
    calls: list[int | None] = []
    real = Station.async_event_thumbnail

    async def counting(self: Station, event: SecurityEvent, **kwargs: Any) -> Any:
        calls.append(event.record_id)
        return await real(self, event, **kwargs)

    monkeypatch.setattr(Station, "async_event_thumbnail", counting)
    return calls


def _fake_push_pictures(monkeypatch: pytest.MonkeyPatch) -> list[PushPictureSource]:
    """Answer every push picture fetch with a JPEG; returns the sources asked for."""
    asked: list[PushPictureSource] = []

    async def fetch(
        self: EufySecurity, event: SecurityEvent, source: PushPictureSource | None = None
    ) -> PushImage:
        assert source is not None
        asked.append(source)
        return PushImage(
            source=source,
            device_sn=event.device_sn,
            data=b"\xff\xd8PUSH-PICTURE\xff\xd9",
            content_type="image/jpeg",
        )

    monkeypatch.setattr(EufySecurity, "async_push_image", fetch)
    return asked


class _Gates:
    """Hold each detection's wait once the library has answered it, until released, so
    a test decides the order in which the waits finish (keyed by push session id)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.answered: set[str] = set()
        self._gates: dict[str, asyncio.Event] = {}
        real = Station.async_wait_recording_for_detection

        async def gated(
            station: Station, event: SecurityEvent, **kwargs: Any
        ) -> HistoryRecord | None:
            record = await real(station, event, **kwargs)
            name = event.session_id or ""
            self.answered.add(name)
            await self._gate(name).wait()
            return record

        monkeypatch.setattr(Station, "async_wait_recording_for_detection", gated)

    def _gate(self, name: str) -> asyncio.Event:
        return self._gates.setdefault(name, asyncio.Event())

    def release(self, *names: str) -> None:
        for name in names:
            self._gate(name).set()


async def _finish_in_order(entry: MockConfigEntry, gates: _Gates, order: tuple[str, ...]) -> None:
    """Let the held waits finish one by one in ``order``, each handled to the end."""
    matcher = _matcher(entry)
    snapshots = _snapshots(entry)
    await wait_until(lambda: gates.answered >= set(order), timeout=8)
    for count, name in enumerate(order, 1):
        gates.release(name)
        await wait_until(
            lambda count=count: matcher.waiting == len(order) - count and not snapshots.busy
        )


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


async def _settled(hass: HomeAssistant, entry: MockConfigEntry, timeout: float = 8.0) -> None:
    """Wait until every detection's wait, the stills worker and the copy are done."""
    matcher = _matcher(entry)
    snapshots = _snapshots(entry)
    recordings = entry.runtime_data.recordings
    await wait_until(
        lambda: (
            matcher.waiting == 0
            and not snapshots.busy
            and (recordings is None or not recordings.busy)
        ),
        timeout=timeout,
    )
    await hass.async_block_till_done()


def _videos(hass: HomeAssistant) -> list[Path]:
    root = history.history_dir(hass)
    return sorted(root.rglob("*.mp4")) if root.is_dir() else []


def _stills(hass: HomeAssistant) -> list[Path]:
    root = history.history_dir(hass)
    return sorted(root.rglob("*.jpg")) if root.is_dir() else []


async def _unload(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_homebase_2_detection_shows_its_trigger_frame_and_copies_its_clip_once(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not listed, running, settled: then the trigger frame of that row is shown at the
    detection's time and its clip is copied beside the still, once."""
    frames = _count_frames(monkeypatch)
    thumbnails = _count_thumbnails(monkeypatch)
    downloads = _count_downloads(monkeypatch)
    entry = await _set_up(hass, seed_warm_cache, VIDEOS_ON)
    started = _start()
    record_id = fake_station.add_detection_recording(
        SYNTHETIC.camera_sn, started, CLIP_SECONDS, listed_after=0.5
    )

    entry.runtime_data.router.handle(_push(started))
    assert _matcher(entry).waiting == 1
    await _settled(hass, entry)

    snapshots = _snapshots(entry)
    assert snapshots.source_for(SYNTHETIC.camera_sn) == "trigger_frame"
    assert snapshots.event_time_for(SYNTHETIC.camera_sn) == round(started.timestamp() * 1000)
    assert frames == [record_id]
    assert thumbnails == []  # no thumbnail step: a HomeBase 2 row's still cannot be looked up
    assert downloads == [record_id]
    (still,) = _stills(hass)
    (video,) = _videos(hass)
    assert video.stem == still.stem
    manager = entry.runtime_data.recordings
    assert manager is not None
    stats = manager.stats(HOMEBASE_2_SN)
    assert stats is not None
    assert stats["stored"] == 1
    await _unload(hass, entry)


async def test_two_detections_inside_one_clip_get_one_trigger_frame_and_one_copy(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second detection of the camera a second into the same clip is matched to the
    same row: the row's frame is fetched once and its clip copied once."""
    frames = _count_frames(monkeypatch)
    downloads = _count_downloads(monkeypatch)
    entry = await _set_up(hass, seed_warm_cache, VIDEOS_ON)
    started = _start()
    record_id = fake_station.add_detection_recording(SYNTHETIC.camera_sn, started, CLIP_SECONDS)

    entry.runtime_data.router.handle(_push(started))
    entry.runtime_data.router.handle(_push(started + timedelta(seconds=1), session_id="session-2"))
    assert _matcher(entry).waiting == 2
    await _settled(hass, entry)
    assert frames == [record_id]
    # A third, whose push comes after the frame is shown and is newer than it.
    entry.runtime_data.router.handle(
        _push(started + timedelta(milliseconds=1500), session_id="session-3")
    )
    await _settled(hass, entry)

    assert frames == [record_id]
    assert downloads == [record_id]
    assert len(_videos(hass)) == 1
    assert _snapshots(entry).source_for(SYNTHETIC.camera_sn) == "trigger_frame"
    await _unload(hass, entry)


async def test_no_row_of_the_camera_in_the_window_shows_and_copies_nothing(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Another camera's row at the detection's time and the camera's own clip from a
    minute before: the wait ends empty at the bound, with one DEBUG line."""
    monkeypatch.setattr(station_module, "RECORDING_MATCH_GIVE_UP", 1.5)
    frames = _count_frames(monkeypatch)
    downloads = _count_downloads(monkeypatch)
    entry = await _set_up(hass, seed_warm_cache, VIDEOS_ON)
    started = _start(0)
    fake_station.add_detection_recording(OTHER_SN, started, CLIP_SECONDS)
    fake_station.add_detection_recording(
        SYNTHETIC.camera_sn, started - timedelta(seconds=60), CLIP_SECONDS
    )
    caplog.set_level(logging.DEBUG, logger="custom_components.eufy_home_security")

    entry.runtime_data.router.handle(_push(started))
    await _settled(hass, entry)

    assert datetime.now(UTC) >= started + timedelta(seconds=1.5)
    assert frames == []
    assert downloads == []
    assert _snapshots(entry).image_for(SYNTHETIC.camera_sn) is None
    assert _videos(hass) == []
    assert "none matched by the bound" in caplog.text
    await _unload(hass, entry)


async def test_a_press_after_the_push_is_not_replaced_by_the_late_trigger_frame(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The detection's place in the camera's order is taken when its push arrives: a
    Capture live image press after it outranks the frame that comes once the clip has
    settled, so the frame is not even fetched."""
    frames = _count_frames(monkeypatch)
    entry = await _set_up(hass, seed_warm_cache, {})
    snapshots = _snapshots(entry)
    station = entry.runtime_data.coordinators[HOMEBASE_2_SN].station
    started = _start()
    fake_station.add_detection_recording(SYNTHETIC.camera_sn, started, CLIP_SECONDS)

    entry.runtime_data.router.handle(_push(started))
    snapshots.async_request_capture(station, SYNTHETIC.camera_sn)
    await wait_until(lambda: snapshots.source_for(SYNTHETIC.camera_sn) == "live")
    assert _matcher(entry).waiting == 1
    await _settled(hass, entry)

    assert snapshots.source_for(SYNTHETIC.camera_sn) == "live"
    assert frames == []
    await _unload(hass, entry)


async def test_a_press_before_the_push_is_replaced_by_its_trigger_frame(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A press made before the detection's push ranks below it: the frame replaces it."""
    entry = await _set_up(hass, seed_warm_cache, {})
    snapshots = _snapshots(entry)
    station = entry.runtime_data.coordinators[HOMEBASE_2_SN].station
    snapshots.async_request_capture(station, SYNTHETIC.camera_sn)
    await wait_until(lambda: snapshots.source_for(SYNTHETIC.camera_sn) == "live")
    started = _start()
    fake_station.add_detection_recording(SYNTHETIC.camera_sn, started, CLIP_SECONDS)

    entry.runtime_data.router.handle(_push(started))
    await _settled(hass, entry)

    assert snapshots.source_for(SYNTHETIC.camera_sn) == "trigger_frame"
    await _unload(hass, entry)


@pytest.mark.parametrize("mode", [CameraImageMode.HD, CameraImageMode.HD_ONLY])
async def test_with_event_videos_off_the_trigger_frame_shows_and_nothing_is_copied(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    mode: CameraImageMode,
) -> None:
    """hd and hd_only both show the matched recording's trigger frame; no copy."""
    downloads = _count_downloads(monkeypatch)
    entry = await _set_up(hass, seed_warm_cache, {CONF_CAMERA_IMAGE: mode.value})
    assert entry.runtime_data.recordings is None
    started = _start()
    fake_station.add_detection_recording(SYNTHETIC.camera_sn, started, CLIP_SECONDS)

    entry.runtime_data.router.handle(_push(started))
    await _settled(hass, entry)

    assert _snapshots(entry).source_for(SYNTHETIC.camera_sn) == "trigger_frame"
    assert downloads == []
    assert _videos(hass) == []
    await _unload(hass, entry)


async def test_thumbnail_mode_with_event_videos_off_looks_for_nothing(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """No trigger frame in thumbnail mode and no copy: no wait, no history query."""
    entry = await _set_up(
        hass, seed_warm_cache, {CONF_CAMERA_IMAGE: CameraImageMode.THUMBNAIL.value}
    )
    started = _start()
    fake_station.add_detection_recording(SYNTHETIC.camera_sn, started, CLIP_SECONDS)

    entry.runtime_data.router.handle(_push(started))

    assert _matcher(entry).waiting == 0
    await hass.async_block_till_done()
    assert fake_station.history_queries == []
    assert _snapshots(entry).image_for(SYNTHETIC.camera_sn) is None
    await _unload(hass, entry)


async def test_thumbnail_mode_with_event_videos_on_copies_the_clip_without_a_frame(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Thumbnail mode shows no trigger frame; the clip is still copied, named by its start."""
    frames = _count_frames(monkeypatch)
    downloads = _count_downloads(monkeypatch)
    entry = await _set_up(
        hass,
        seed_warm_cache,
        {CONF_CAMERA_IMAGE: CameraImageMode.THUMBNAIL.value, **VIDEOS_ON},
    )
    started = _start()
    record_id = fake_station.add_detection_recording(SYNTHETIC.camera_sn, started, CLIP_SECONDS)

    entry.runtime_data.router.handle(_push(started))
    await _settled(hass, entry)

    assert frames == []
    assert _snapshots(entry).image_for(SYNTHETIC.camera_sn) is None
    assert downloads == [record_id]
    (video,) = _videos(hass)
    assert video.stem.endswith("_event")
    await _unload(hass, entry)


async def test_a_recording_a_pass_stored_already_is_not_copied_again(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_storage: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A detection matched to a row the recording sync stored before gets its frame, and
    the copy finds the row stored: one download in all."""
    downloads = _count_downloads(monkeypatch)
    started = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=2)
    record_id = fake_station.add_detection_recording(SYNTHETIC.camera_sn, started, CLIP_SECONDS)
    entry = await _set_up(hass, seed_warm_cache, VIDEOS_ON, hass_storage)
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=RECORDING_SYNC_FIRST_DELAY_SECONDS + 1)
    )
    await hass.async_block_till_done()
    await wait_until(lambda: downloads == [record_id])
    await _settled(hass, entry)

    entry.runtime_data.router.handle(_push(started))
    await _settled(hass, entry)

    assert _snapshots(entry).source_for(SYNTHETIC.camera_sn) == "trigger_frame"
    assert downloads == [record_id]
    assert len(_videos(hass)) == 1
    await _unload(hass, entry)


async def test_a_push_picture_is_replaced_by_the_matched_recordings_trigger_frame(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A push naming a picture shows it at once; its recording is still looked for, and
    its trigger frame replaces the picture once the clip has settled."""
    asked = _fake_push_pictures(monkeypatch)
    entry = await _set_up(hass, seed_warm_cache, {})
    snapshots = _snapshots(entry)
    started = _start()
    fake_station.add_detection_recording(SYNTHETIC.camera_sn, started, CLIP_SECONDS)

    entry.runtime_data.router.handle(_push(started, pic_url="https://picture.example/event.jpg"))
    await wait_until(lambda: snapshots.source_for(SYNTHETIC.camera_sn) == "push_picture")
    await _settled(hass, entry)

    assert asked == [PushPictureSource.PIC_URL]
    assert snapshots.source_for(SYNTHETIC.camera_sn) == "trigger_frame"
    await _unload(hass, entry)


@pytest.mark.parametrize(
    ("outcome", "line"),
    [
        (None, "none matched by the bound"),
        (DeviceTimeoutError("no reply"), "no listing answered by the bound (DeviceTimeoutError)"),
        (UnsupportedError("no camera"), "not looked up (UnsupportedError)"),
        (RecordNotFoundError("another camera"), "not looked up (RecordNotFoundError)"),
    ],
    ids=["none", "communication", "unsupported", "record_not_found"],
)
async def test_a_wait_without_a_recording_is_one_debug_line(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    outcome: EufySecurityError | None,
    line: str,
) -> None:
    """Nothing found, no listing answered, or an event the library cannot match: one
    DEBUG line, no warning, no repair issue, no image."""

    async def wait(self: Station, event: SecurityEvent, **kwargs: Any) -> HistoryRecord | None:
        if outcome is not None:
            raise outcome
        return None

    monkeypatch.setattr(Station, "async_wait_recording_for_detection", wait)
    entry = await _set_up(hass, seed_warm_cache, VIDEOS_ON)
    issues = set(ir.async_get(hass).issues)
    caplog.set_level(logging.DEBUG, logger="custom_components.eufy_home_security")
    caplog.clear()

    entry.runtime_data.router.handle(_push(_start(0)))
    await _settled(hass, entry)

    ours = [r for r in caplog.records if r.name.startswith("custom_components.eufy_home_security")]
    assert [r.levelno for r in ours if r.levelno >= logging.INFO] == []
    assert sum(line in r.getMessage() for r in ours) == 1
    assert set(ir.async_get(hass).issues) == issues
    assert _snapshots(entry).image_for(SYNTHETIC.camera_sn) is None
    await _unload(hass, entry)


async def test_an_unexpected_error_of_a_wait_is_logged_once(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An error the library does not document ends the wait with one logged traceback."""

    async def wait(self: Station, event: SecurityEvent, **kwargs: Any) -> HistoryRecord | None:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(Station, "async_wait_recording_for_detection", wait)
    entry = await _set_up(hass, seed_warm_cache, {})

    entry.runtime_data.router.handle(_push(_start(0)))
    await _settled(hass, entry)

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR and r.exc_info]
    assert [r.name for r in errors] == ["custom_components.eufy_home_security.recording_match"]
    await _unload(hass, entry)


async def test_unload_during_a_wait_cancels_it_quietly(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A wait still listing at unload is cancelled with the entry: no task outlives it,
    nothing is logged above DEBUG, and the loop's exception handler sees nothing."""
    monkeypatch.setattr(station_module, "RECORDING_MATCH_GIVE_UP", 60.0)
    entry = await _set_up(hass, seed_warm_cache, VIDEOS_ON)
    matcher = _matcher(entry)
    seen: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(lambda _loop, context: seen.append(context))
    try:
        entry.runtime_data.router.handle(_push(_start(0)))
        (task,) = matcher._tasks
        await wait_until(lambda: len(fake_station.history_queries) >= 2)
        caplog.clear()

        await _unload(hass, entry)

        assert task.cancelled()
        assert matcher.waiting == 0
        for _ in range(3):
            await asyncio.sleep(0)
        assert seen == []
    finally:
        loop.set_exception_handler(None)
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


@pytest.mark.parametrize("fake_station", [SYNTHETIC.station_sn], indirect=True)
async def test_a_homebase_3_detection_naming_no_recording_is_not_matched(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A HomeBase 3 sends its catalogued cameras' detections over P2P with the recording
    named: a cloud push of one naming none starts no wait and lists nothing."""
    entry = await _set_up(hass, seed_warm_cache, VIDEOS_ON)
    station = entry.runtime_data.coordinators[SYNTHETIC.station_sn].station
    event = _push(_start(0), station_sn=SYNTHETIC.station_sn)
    assert not station.needs_recording_match(event)

    entry.runtime_data.router.handle(event)

    assert _matcher(entry).waiting == 0
    await hass.async_block_till_done()
    assert fake_station.history_queries == []
    await _unload(hass, entry)


async def test_a_matched_frames_waiter_is_released_when_replaced_or_at_unload(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """While the camera's worker is busy, a waiting matched frame that a newer detection
    replaces, and one still waiting at unload, both release their waiter: the copy after
    the frame never hangs."""
    held = asyncio.Event()
    entered = asyncio.Event()

    async def held_frame(self: Station, event: SecurityEvent, **kwargs: Any) -> bytes:
        entered.set()
        await held.wait()
        raise DeviceTimeoutError("held")

    monkeypatch.setattr(Station, "async_event_trigger_frame", held_frame)
    entry = await _set_up(hass, seed_warm_cache, {})
    snapshots = _snapshots(entry)
    station = entry.runtime_data.coordinators[HOMEBASE_2_SN].station
    now = _start(0)

    def named(seconds: int, session_id: str) -> SecurityEvent:
        return _push(
            now + timedelta(seconds=seconds),
            session_id=session_id,
            record_id=90000 + seconds,
            video_path=f"/media/mmcblk0p1/Camera00/2026010112000{seconds}.dat",
        )

    def slot() -> int:
        taken = snapshots.async_hold_slot(SYNTHETIC.camera_sn)
        assert taken is not None
        return taken

    first = snapshots.async_request_matched_frame(station, named(0, "a"), slot())
    await asyncio.wait_for(entered.wait(), 5)
    replaced = snapshots.async_request_matched_frame(station, named(1, "b"), slot())
    assert not replaced.done()
    snapshots.async_request_event(station, named(2, "c"))
    assert replaced.done()
    waiting = snapshots.async_request_matched_frame(station, named(3, "d"), slot())
    assert not waiting.done()

    await _unload(hass, entry)
    held.set()
    assert first.done()
    assert waiting.done()


async def test_a_pass_asked_during_a_matched_copy_runs_after_it(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 15-minute tick while a matched clip downloads is not lost: one pass follows
    the copy, and it copies nothing again."""
    released = asyncio.Event()
    started_download = asyncio.Event()
    calls: list[int] = []
    real = Station.async_download_recording

    async def slow(
        self: Station, record: HistoryRecord, write: ClipWriter, *, wait: bool = True
    ) -> MediaClip:
        calls.append(record.record_id)
        started_download.set()
        await released.wait()
        return await real(self, record, write, wait=wait)

    monkeypatch.setattr(Station, "async_download_recording", slow)
    entry = await _set_up(hass, seed_warm_cache, VIDEOS_ON)
    manager = entry.runtime_data.recordings
    assert manager is not None
    started = _start()
    record_id = fake_station.add_detection_recording(SYNTHETIC.camera_sn, started, CLIP_SECONDS)

    entry.runtime_data.router.handle(_push(started))
    await asyncio.wait_for(started_download.wait(), 8)
    before = manager.stats(HOMEBASE_2_SN)
    assert before is not None
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=RECORDING_SYNC_INTERVAL_SECONDS + 1)
    )
    await hass.async_block_till_done()
    released.set()
    await _settled(hass, entry)

    after = manager.stats(HOMEBASE_2_SN)
    assert after is not None
    assert after["passes"] == before["passes"] + 1
    assert calls == [record_id]
    await _unload(hass, entry)


async def test_the_15_minute_pass_after_a_matched_copy_copies_nothing_again(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_storage: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clip copied for its detection is stored for the sync too: its catch-up pass,
    which reads the row as settled, lists it and downloads nothing."""
    downloads = _count_downloads(monkeypatch)
    started = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=2)
    record_id = fake_station.add_detection_recording(SYNTHETIC.camera_sn, started, CLIP_SECONDS)
    entry = await _set_up(hass, seed_warm_cache, VIDEOS_ON, hass_storage)
    manager = entry.runtime_data.recordings
    assert manager is not None

    entry.runtime_data.router.handle(_push(started))
    await _settled(hass, entry)
    assert downloads == [record_id]
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=RECORDING_SYNC_INTERVAL_SECONDS + 1)
    )
    await hass.async_block_till_done()
    await wait_until(lambda: (manager.stats(HOMEBASE_2_SN) or {}).get("passes", 0) >= 1)
    await _settled(hass, entry)

    assert downloads == [record_id]
    assert len(_videos(hass)) == 1
    await _unload(hass, entry)


async def test_an_older_detection_whose_frame_is_not_shown_still_gets_its_copy(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_storage: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A newer detection's frame is shown first; the older one's matched frame is
    refused as older, and its own recording is still copied."""
    frames = _count_frames(monkeypatch)
    downloads = _count_downloads(monkeypatch)
    now = datetime.now(UTC).replace(microsecond=0)
    older = fake_station.add_detection_recording(
        SYNTHETIC.camera_sn, now - timedelta(seconds=60), CLIP_SECONDS
    )
    newer = fake_station.add_detection_recording(
        SYNTHETIC.camera_sn, now - timedelta(seconds=40), CLIP_SECONDS
    )
    entry = await _set_up(hass, seed_warm_cache, VIDEOS_ON, hass_storage)

    entry.runtime_data.router.handle(_push(now - timedelta(seconds=40), session_id="newer"))
    await _settled(hass, entry)
    entry.runtime_data.router.handle(_push(now - timedelta(seconds=60), session_id="older"))
    await _settled(hass, entry)

    assert frames == [newer]
    assert sorted(downloads) == sorted([newer, older])
    assert _snapshots(entry).event_time_for(SYNTHETIC.camera_sn) == round(
        (now - timedelta(seconds=40)).timestamp() * 1000
    )
    await _unload(hass, entry)


ORDERS: Final = [("d1", "d2"), ("d2", "d1")]


@pytest.mark.parametrize("order", ORDERS, ids=["d1_first", "d2_first"])
async def test_a_later_detection_with_a_picture_in_the_clip_ends_on_the_trigger_frame(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    order: tuple[str, str],
) -> None:
    """D1 names no picture, D2 a second later in the same clip names one and shows it:
    whichever wait finishes first, the clip's trigger frame ends up shown at the newest
    detection (D2), fetched once, and the clip is copied once."""
    _fake_push_pictures(monkeypatch)
    gates = _Gates(monkeypatch)
    frames = _count_frames(monkeypatch)
    downloads = _count_downloads(monkeypatch)
    entry = await _set_up(hass, seed_warm_cache, VIDEOS_ON)
    snapshots = _snapshots(entry)
    started = _start()
    record_id = fake_station.add_detection_recording(SYNTHETIC.camera_sn, started, CLIP_SECONDS)
    second = started + timedelta(seconds=1)

    entry.runtime_data.router.handle(_push(started, session_id="d1"))
    entry.runtime_data.router.handle(
        _push(second, session_id="d2", pic_url="https://picture.example/event.jpg")
    )
    await wait_until(lambda: snapshots.source_for(SYNTHETIC.camera_sn) == "push_picture")
    await _finish_in_order(entry, gates, order)
    await _settled(hass, entry)

    assert snapshots.source_for(SYNTHETIC.camera_sn) == "trigger_frame"
    assert snapshots.event_time_for(SYNTHETIC.camera_sn) == round(second.timestamp() * 1000)
    assert frames == [record_id]
    assert downloads == [record_id]
    await _unload(hass, entry)


@pytest.mark.parametrize("order", ORDERS, ids=["d1_first", "d2_first"])
async def test_a_press_between_two_detections_of_one_clip_is_replaced_by_its_frame(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    order: tuple[str, str],
) -> None:
    """D1, a press, then D2 in the same clip: D2 came after the press, so the clip's frame
    replaces the pressed image whichever wait finishes first, fetched once."""
    gates = _Gates(monkeypatch)
    frames = _count_frames(monkeypatch)
    entry = await _set_up(hass, seed_warm_cache, {})
    snapshots = _snapshots(entry)
    station = entry.runtime_data.coordinators[HOMEBASE_2_SN].station
    started = _start()
    record_id = fake_station.add_detection_recording(SYNTHETIC.camera_sn, started, CLIP_SECONDS)

    entry.runtime_data.router.handle(_push(started, session_id="d1"))
    snapshots.async_request_capture(station, SYNTHETIC.camera_sn)
    await wait_until(lambda: snapshots.source_for(SYNTHETIC.camera_sn) == "live")
    entry.runtime_data.router.handle(_push(started + timedelta(seconds=1), session_id="d2"))
    await _finish_in_order(entry, gates, order)
    await _settled(hass, entry)

    assert snapshots.source_for(SYNTHETIC.camera_sn) == "trigger_frame"
    assert frames == [record_id]
    await _unload(hass, entry)


async def test_two_waits_of_one_clip_finishing_together_fetch_its_frame_once(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both waits of one clip return in the same loop turn: one frame fetch, one copy."""
    gates = _Gates(monkeypatch)
    frames = _count_frames(monkeypatch)
    downloads = _count_downloads(monkeypatch)
    entry = await _set_up(hass, seed_warm_cache, VIDEOS_ON)
    started = _start()
    record_id = fake_station.add_detection_recording(SYNTHETIC.camera_sn, started, CLIP_SECONDS)

    entry.runtime_data.router.handle(_push(started, session_id="d1"))
    entry.runtime_data.router.handle(_push(started + timedelta(seconds=1), session_id="d2"))
    await wait_until(lambda: gates.answered >= {"d1", "d2"}, timeout=8)
    gates.release("d1", "d2")
    await _settled(hass, entry)

    assert _snapshots(entry).source_for(SYNTHETIC.camera_sn) == "trigger_frame"
    assert frames == [record_id]
    assert downloads == [record_id]
    await _unload(hass, entry)


async def test_an_enriching_copy_of_a_waiting_detection_starts_no_second_wait(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later copy of the same occurrence (same session) takes no slot of its own: a press
    made between the push and the copy stays shown, and only one wait runs."""
    frames = _count_frames(monkeypatch)
    entry = await _set_up(hass, seed_warm_cache, {})
    snapshots = _snapshots(entry)
    station = entry.runtime_data.coordinators[HOMEBASE_2_SN].station
    started = _start()
    fake_station.add_detection_recording(SYNTHETIC.camera_sn, started, CLIP_SECONDS)

    entry.runtime_data.router.handle(_push(started, session_id="d1"))
    snapshots.async_request_capture(station, SYNTHETIC.camera_sn)
    await wait_until(lambda: snapshots.source_for(SYNTHETIC.camera_sn) == "live")
    copy = _push(started, session_id="d1", enriches=True)
    assert copy.dedupe_key == _push(started, session_id="d1").dedupe_key
    entry.runtime_data.router.handle(copy)

    assert _matcher(entry).waiting == 1
    await _settled(hass, entry)
    assert snapshots.source_for(SYNTHETIC.camera_sn) == "live"
    assert frames == []
    await _unload(hass, entry)


async def test_an_unauthenticated_lan_frame_starts_no_wait(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A detection under the static ECB key, which anyone on the LAN can forge, lists no
    recordings and takes no slot; the same detection by cloud push does."""
    entry = await _set_up(hass, seed_warm_cache, VIDEOS_ON)
    started = _start(0)
    forged = _push(started, session_id="forged", cipher=FrameCipher.ECB)
    assert not forged.authenticated

    entry.runtime_data.router.handle(forged)
    assert _matcher(entry).waiting == 0
    entry.runtime_data.router.handle(_push(started, session_id="genuine"))
    assert _matcher(entry).waiting == 1
    await _unload(hass, entry)


async def test_a_frame_held_past_its_bound_does_not_hold_the_copy(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_storage: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A stills worker that does not get to the frame within the bound: the clip is
    copied anyway, with one DEBUG line."""
    monkeypatch.setattr(recording_match, "MATCHED_FRAME_WAIT_SECONDS", 0.2)
    held = asyncio.Event()

    async def held_frame(self: Station, event: SecurityEvent, **kwargs: Any) -> bytes:
        await held.wait()
        raise DeviceTimeoutError("held")

    monkeypatch.setattr(Station, "async_event_trigger_frame", held_frame)
    downloads = _count_downloads(monkeypatch)
    started = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=2)
    record_id = fake_station.add_detection_recording(SYNTHETIC.camera_sn, started, CLIP_SECONDS)
    entry = await _set_up(hass, seed_warm_cache, VIDEOS_ON, hass_storage)
    caplog.set_level(logging.DEBUG, logger="custom_components.eufy_home_security")

    entry.runtime_data.router.handle(_push(started))
    await wait_until(lambda: downloads == [record_id], timeout=8)
    held.set()
    await _settled(hass, entry)

    assert "trigger frame not done within" in caplog.text
    assert len(_videos(hass)) == 1
    await _unload(hass, entry)


async def test_a_match_during_a_running_copy_waits_for_it(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_storage: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second recording matched while the station's copy runs is downloaded after it:
    one download per station at a time."""
    released = asyncio.Event()
    active: list[int] = []
    most = [0]
    calls: list[int] = []
    real = Station.async_download_recording

    async def held(
        self: Station, record: HistoryRecord, write: ClipWriter, *, wait: bool = True
    ) -> MediaClip:
        calls.append(record.record_id)
        active.append(record.record_id)
        most[0] = max(most[0], len(active))
        try:
            await released.wait()
            return await real(self, record, write, wait=wait)
        finally:
            active.remove(record.record_id)

    monkeypatch.setattr(Station, "async_download_recording", held)
    now = datetime.now(UTC).replace(microsecond=0)
    older = fake_station.add_detection_recording(
        SYNTHETIC.camera_sn, now - timedelta(seconds=60), CLIP_SECONDS
    )
    newer = fake_station.add_detection_recording(
        SYNTHETIC.camera_sn, now - timedelta(seconds=40), CLIP_SECONDS
    )
    entry = await _set_up(hass, seed_warm_cache, VIDEOS_ON, hass_storage)

    entry.runtime_data.router.handle(_push(now - timedelta(seconds=60), session_id="older"))
    await wait_until(lambda: calls == [older], timeout=8)
    entry.runtime_data.router.handle(_push(now - timedelta(seconds=40), session_id="newer"))
    await wait_until(lambda: _matcher(entry).waiting == 0, timeout=8)
    await hass.async_block_till_done()
    released.set()
    await _settled(hass, entry)

    assert calls == [older, newer]
    assert most[0] == 1
    assert len(_videos(hass)) == 2
    await _unload(hass, entry)


async def test_a_later_detection_behind_a_held_frame_of_its_recording_goes_on(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D2's wait finds D1's frame request of the same clip still held past the bound: it
    stops waiting for it and goes on, and the clip is copied once."""
    monkeypatch.setattr(recording_match, "MATCHED_FRAME_WAIT_SECONDS", 0.2)
    held = asyncio.Event()
    entered = asyncio.Event()

    async def held_frame(self: Station, event: SecurityEvent, **kwargs: Any) -> bytes:
        entered.set()
        await held.wait()
        raise DeviceTimeoutError("held")

    monkeypatch.setattr(Station, "async_event_trigger_frame", held_frame)
    gates = _Gates(monkeypatch)
    downloads = _count_downloads(monkeypatch)
    entry = await _set_up(hass, seed_warm_cache, VIDEOS_ON)
    matcher = _matcher(entry)
    started = _start()
    record_id = fake_station.add_detection_recording(SYNTHETIC.camera_sn, started, CLIP_SECONDS)

    entry.runtime_data.router.handle(_push(started, session_id="d1"))
    entry.runtime_data.router.handle(_push(started + timedelta(seconds=1), session_id="d2"))
    await wait_until(lambda: gates.answered >= {"d1", "d2"}, timeout=8)
    gates.release("d1")
    await asyncio.wait_for(entered.wait(), 5)
    gates.release("d2")
    await wait_until(lambda: matcher.waiting == 0, timeout=3)
    assert not held.is_set()

    held.set()
    await _settled(hass, entry)
    assert downloads == [record_id]
    await _unload(hass, entry)
