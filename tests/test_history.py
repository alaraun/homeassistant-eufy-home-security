"""The event history (``history.py``): every shown still as a dated, named file in media.

End to end on the library's loopback ``FakeStation`` for the write path, plus the
pure helpers (names, pruning, the container mount check) on their own. Each test has
its own media folder (conftest's ``_media_dir_per_test``).
"""

from __future__ import annotations

import json
import logging
import os
import stat
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest
from conftest import (
    SYNTHETIC,
    detection_event,
    entity_id_for,
    now_ms,
    set_up_warm,
    wait_until,
)
from eufy_home_security import (
    ClipWriter,
    DetectionType,
    DeviceTimeoutError,
    EufySecurity,
    MediaClip,
)
from eufy_home_security.testing import FakeCloud, FakeStation, camera_device
from fake_ffmpeg import REMUX_MAGIC
from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN
from homeassistant.components.button import SERVICE_PRESS
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.eufy_home_security import errors, history
from custom_components.eufy_home_security.const import (
    CAPTURE_LIVE_IMAGE_KEY,
    CONF_EVENT_HISTORY_DAYS,
    DOMAIN,
    ISSUE_MEDIA_NOT_PERSISTENT,
)
from custom_components.eufy_home_security.snapshots import SnapshotManager

THUMBNAIL: Final = b"\xff\xd8PERSON-THUMBNAIL\xff\xd9"
CAMERA_NAME: Final = "Front"
# Paired beside the synthetic camera: one whose name cleans to the same folder, one not.
TWIN_CAMERA_SN: Final = "T8160P2000000002"
OTHER_CAMERA_SN: Final = "T8160P2000000003"


def _manager(entry: MockConfigEntry) -> SnapshotManager:
    manager: SnapshotManager = entry.runtime_data.snapshots
    return manager


def _files(hass: HomeAssistant) -> list[Path]:
    root = history.history_dir(hass)
    return sorted(path for path in root.rglob("*") if path.is_file()) if root.is_dir() else []


async def _unload(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def _detect(
    hass: HomeAssistant, entry: MockConfigEntry, fake_station: FakeStation, t_ms: int
) -> None:
    fake_station.images["/zx/person.jpg"] = THUMBNAIL
    entry.runtime_data.router.handle(
        detection_event(DetectionType.PERSON, t_ms=t_ms, thumb_path="/zx/person.jpg")
    )
    await wait_until(lambda: _manager(entry).image_for(SYNTHETIC.camera_sn) is not None)
    await wait_until(lambda: not _manager(entry).busy)
    await hass.async_block_till_done()


def test_a_file_name_carries_date_time_camera_name_and_kind() -> None:
    """``<camera>/<date>_<time>_<camera>_<kind>.jpg``, with the name made file-safe."""
    moment = datetime(2026, 9, 29, 8, 35, 59, tzinfo=dt_util.get_default_time_zone())
    path = history.history_path(Path("/m"), "Back yard / Gate", moment, "person")
    assert path == Path("/m/Back_yard_Gate/2026-09-29_08-35-59_Back_yard_Gate_person.jpg")
    assert history.safe_name("Õue kaamera") == "Õue_kaamera"
    assert history.safe_name("../..") == "camera"
    assert history.safe_name(None) == "camera"
    assert len(history.safe_name("x" * 200)) == 60


async def test_a_detection_is_saved_under_the_camera_name_at_its_own_local_time(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The file is named by the detection's time in HA's zone and its class; readable 0644."""
    entry = await set_up_warm(hass, seed_warm_cache)
    t_ms = now_ms() - 5_000
    await _detect(hass, entry, fake_station, t_ms)
    moment = dt_util.as_local(dt_util.utc_from_timestamp(t_ms / 1000))
    expected = history.history_path(history.history_dir(hass), CAMERA_NAME, moment, "person")

    await wait_until(expected.exists)

    assert expected.read_bytes() == _manager(entry).image_for(SYNTHETIC.camera_sn)
    assert f"_{CAMERA_NAME}_person.jpg" in expected.name
    assert stat.S_IMODE(expected.stat().st_mode) == 0o644
    assert not [path for path in _files(hass) if path.name.endswith(".tmp")]
    await _unload(hass, entry)


async def test_a_renamed_camera_is_saved_under_its_new_name(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The name a user gave the device in Home Assistant is the one in the path."""
    entry = await set_up_warm(hass, seed_warm_cache)
    registry = dr.async_get(hass)
    device = registry.async_get_device_by_identifier((DOMAIN, SYNTHETIC.camera_sn), entry.entry_id)
    assert device is not None
    registry.async_update_device(device.id, name_by_user="Garage door")

    await _detect(hass, entry, fake_station, now_ms())
    await wait_until(lambda: bool(_files(hass)))

    assert all(
        path.parent.name == "Garage_door" and "_Garage_door_" in path.name for path in _files(hass)
    )
    await _unload(hass, entry)


async def test_a_live_capture_is_saved_as_live(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A pressed live image is kept too, with ``live`` as its kind."""
    entry = await set_up_warm(hass, seed_warm_cache)
    button = entity_id_for(hass, BUTTON_DOMAIN, SYNTHETIC.camera_sn, CAPTURE_LIVE_IMAGE_KEY)
    await hass.services.async_call(
        BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: button}, blocking=True
    )
    await wait_until(lambda: any(p.name.endswith(f"_{CAMERA_NAME}_live.jpg") for p in _files(hass)))
    await _unload(hass, entry)


async def test_zero_days_saves_nothing(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """With the history off no file and no folder is written."""
    entry = await set_up_warm(hass, seed_warm_cache, options={CONF_EVENT_HISTORY_DAYS: 0})
    await _detect(hass, entry, fake_station, now_ms())
    await hass.async_block_till_done()

    assert not history.history_dir(hass).exists()
    await _unload(hass, entry)


def test_prune_removes_only_expired_history_files(tmp_path: Path) -> None:
    """Files dated before the cut-off by their name go, and stale temp files; the rest stays."""
    camera = tmp_path / "Front"
    camera.mkdir()
    old = camera / "2026-09-20_10-00-00_Front_person.jpg"
    kept = camera / "2026-09-28_10-00-00_Front_person.jpg"
    stale_tmp = camera / ".abc.tmp"
    fresh_tmp = camera / ".def.tmp"
    stranger = camera / "2026-09-19 holiday.jpg"  # hygiene: ok (a user file, not history)
    for path in (old, kept, stale_tmp, fresh_tmp, stranger):
        path.write_bytes(b"x")
    stale = datetime(2026, 9, 20, 12, tzinfo=dt_util.get_default_time_zone()).timestamp()
    os.utime(stale_tmp, (stale, stale))
    (camera / "2026-09-19").mkdir()  # hygiene: ok (a user folder, not history)
    lone = tmp_path / "Gone"
    lone.mkdir()
    (lone / "2026-09-01_10-00-00_Gone_motion.jpg").write_bytes(b"x")

    removed = history.prune(tmp_path, date(2026, 9, 22))

    assert removed == 3
    assert sorted(path.name for path in camera.iterdir()) == sorted(
        [kept.name, fresh_tmp.name, stranger.name, "2026-09-19"]  # hygiene: ok (user folder)
    )
    assert not lone.exists()


async def test_setup_prunes_days_beyond_the_kept_ones(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Keeping 2 days: a file dated 1 day back stays, one dated 2 days back goes."""
    today = dt_util.now().date()
    root = history.history_dir(hass) / CAMERA_NAME
    root.mkdir(parents=True)
    files = {
        offset: root
        / f"{today.fromordinal(today.toordinal() - offset).isoformat()}_10-00-00_{CAMERA_NAME}_motion.jpg"
        for offset in (1, 2)
    }
    for path in files.values():
        path.write_bytes(b"x")

    entry = await set_up_warm(hass, seed_warm_cache, options={CONF_EVENT_HISTORY_DAYS: 2})
    await wait_until(lambda: not files[2].exists())

    assert files[1].exists()
    await _unload(hass, entry)


async def test_a_failed_write_warns_once_and_the_camera_still_shows_it(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unwritable media folder costs the file, never the shown still."""
    history.history_dir(hass).parent.mkdir(parents=True, exist_ok=True)
    history.history_dir(hass).write_bytes(b"not a folder")
    entry = await set_up_warm(hass, seed_warm_cache)
    caplog.set_level(logging.WARNING)

    await _detect(hass, entry, fake_station, now_ms() - 2_000)
    await _detect(hass, entry, fake_station, now_ms())
    await hass.async_block_till_done()

    warnings = [r for r in caplog.records if "event history" in r.getMessage()]
    assert len(warnings) == 1
    assert SYNTHETIC.camera_sn not in warnings[0].getMessage()
    assert _manager(entry).image_for(SYNTHETIC.camera_sn) is not None
    await _unload(hass, entry)


def test_prune_skips_a_temp_file_that_vanishes_and_goes_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A temp file renamed away by a running write is skipped; the rest is still pruned."""
    camera = tmp_path / "Front"
    camera.mkdir()
    vanishing = camera / ".gone.tmp"
    old = camera / "2026-09-20_10-00-00_Front_person.jpg"
    for path in (vanishing, old):
        path.write_bytes(b"x")
    real_stat = Path.stat

    def stat(self: Path, **kwargs: Any) -> os.stat_result:
        if self.name == vanishing.name:
            raise FileNotFoundError(self)
        return real_stat(self, **kwargs)

    monkeypatch.setattr(Path, "stat", stat)

    assert history.prune(tmp_path, date(2026, 9, 22)) == 1
    assert not old.exists()


def _mountinfo(tmp_path: Path, name: str, *points: str) -> Path:
    lines = [f"{i} 1 0:{i} / {point} rw - ext4 /dev/x rw" for i, point in enumerate(points)]
    info = tmp_path / name
    info.write_text("\n".join(lines) + "\n")
    return info


def test_media_is_persistent_only_on_its_own_mount_in_a_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In a container ``/media`` must be a mount; the root file system does not count."""
    monkeypatch.setattr(history, "is_docker_env", lambda: True)
    media = Path("/media/eufy_home_security")
    bare = _mountinfo(tmp_path, "bare", "/", "/config", "/proc")
    assert not history.media_is_persistent(media, bare)
    mounted = _mountinfo(tmp_path, "mounted", "/", "/config", "/media")
    assert history.media_is_persistent(media, mounted)
    under_config = Path("/config/media/eufy_home_security")
    assert history.media_is_persistent(under_config, bare)
    spaced = _mountinfo(tmp_path, "spaced", "/", "/my\\040media")
    assert history.media_is_persistent(Path("/my media/x"), spaced)
    assert not history.media_is_persistent(Path("/mediax/y"), mounted)
    assert history.media_is_persistent(media, tmp_path / "missing")

    monkeypatch.setattr(history, "is_docker_env", lambda: False)
    assert history.media_is_persistent(media, bare)


async def test_an_unmounted_media_folder_raises_an_issue_naming_it(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The repair issue names the media folder; turning the history off withdraws it."""
    monkeypatch.setattr(history, "media_is_persistent", lambda _path: False)
    entry = await set_up_warm(hass, seed_warm_cache)
    issue_id = errors.media_not_persistent_issue_id(entry.entry_id)

    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.translation_key == ISSUE_MEDIA_NOT_PERSISTENT
    assert issue.translation_placeholders == {"path": str(history.media_dir(hass))}

    hass.config_entries.async_update_entry(entry, options={CONF_EVENT_HISTORY_DAYS: 0})
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
    await _unload(hass, entry)


async def test_a_mounted_media_folder_raises_no_issue(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Outside a container, or with ``/media`` mounted, there is nothing to repair."""
    monkeypatch.setattr(history, "media_is_persistent", lambda _path: True)
    entry = await set_up_warm(hass, seed_warm_cache)
    issue_id = errors.media_not_persistent_issue_id(entry.entry_id)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
    await _unload(hass, entry)


# A clip's bytes as the library writes them: MPEG-TS packets start with 0x47.
TS_BYTES: Final = b"\x47" + b"\x00" * 187


def _clip(**fields: Any) -> MediaClip:
    values: dict[str, Any] = {
        "video_frames": 6,
        "audio_frames": 6,
        "keyframes": 2,
        "bytes_written": 2 * len(TS_BYTES),
        "duration_s": 0.2,
        "width": 3840,
        "height": 2160,
        "resizes": 0,
    }
    values.update(fields)
    return MediaClip(**values)


def _producer(clip: MediaClip, chunks: int = 2) -> Callable[[ClipWriter], Awaitable[MediaClip]]:
    async def produce(write: ClipWriter) -> MediaClip:
        for _ in range(chunks):
            await write(TS_BYTES)
        return clip

    return produce


async def _failing_producer(write: ClipWriter) -> MediaClip:
    await write(TS_BYTES)
    raise DeviceTimeoutError("no keyframe")


def _history(entry: MockConfigEntry) -> history.EventHistory:
    events: history.EventHistory = entry.runtime_data.history
    return events


async def test_a_clip_is_remuxed_by_stream_copy_and_renamed_into_place(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The TS goes to a hidden part file, ffmpeg copies it into an MP4, 0644, no temp left.

    The MP4 is named by the moment given, in HA's zone, beside the stills, and its media
    id is the one Home Assistant's local media source serves.
    """
    argv_log = tmp_path / "argv.jsonl"
    monkeypatch.setenv("FAKE_FFMPEG_ARGV", str(argv_log))
    entry = await set_up_warm(hass, seed_warm_cache)
    moment = dt_util.utcnow().replace(microsecond=0)

    saved = await _history(entry).async_save_clip(
        SYNTHETIC.camera_sn, _producer(_clip()), kind="person", moment=moment
    )

    expected = history.history_path(
        history.history_dir(hass), CAMERA_NAME, dt_util.as_local(moment), "person", ".mp4"
    )
    assert saved.path == expected
    assert expected.read_bytes() == REMUX_MAGIC + TS_BYTES * 2
    assert stat.S_IMODE(expected.stat().st_mode) == 0o644
    assert [path.name for path in _files(hass)] == [expected.name]
    (args,) = [json.loads(line) for line in argv_log.read_text().splitlines()]
    assert args[:5] == ["-hide_banner", "-loglevel", "error", "-y", "-i"]
    assert args[5].endswith(".ts.part") and Path(args[5]).parent == expected.parent
    assert args[6:-1] == [
        "-map", "0", "-c", "copy", "-tag:v", "hvc1", "-movflags", "+faststart", "-f", "mp4",
    ]  # fmt: skip
    assert "-c:v" not in args, "a clip is never encoded"
    assert history.media_content_id(hass, expected) == (
        f"media-source://media_source/local/{DOMAIN}/{CAMERA_NAME}/{expected.name}"
    )
    await _unload(hass, entry)


async def test_a_clip_without_a_moment_is_named_by_its_own_start(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """With no moment the clip's ``started_at`` names it."""
    entry = await set_up_warm(hass, seed_warm_cache)
    started = datetime(2026, 9, 29, 6, 1, 2, tzinfo=dt_util.UTC)

    saved = await _history(entry).async_save_clip(
        SYNTHETIC.camera_sn, _producer(_clip(started_at=started)), kind="live"
    )

    assert saved.path.name == (
        f"{dt_util.as_local(started).strftime('%Y-%m-%d_%H-%M-%S')}_{CAMERA_NAME}_live.mp4"
    )
    await _unload(hass, entry)


@pytest.mark.parametrize("failure", ["ffmpeg", "producer", "incomplete"])
async def test_a_failed_clip_leaves_no_file(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """A failed remux, a failed download or an incomplete clip keeps nothing, temp files included."""
    entry = await set_up_warm(hass, seed_warm_cache)
    produce = _producer(_clip())
    expected: type[BaseException] = history.ClipStoreError
    if failure == "ffmpeg":
        monkeypatch.setenv("FAKE_FFMPEG_FAIL", "1")
    elif failure == "producer":
        produce = _failing_producer
        expected = DeviceTimeoutError
    else:
        produce = _producer(_clip(expected_frames=40))
        expected = history.IncompleteClipError

    with pytest.raises(expected):
        await _history(entry).async_save_clip(
            SYNTHETIC.camera_sn, produce, kind="person", require_complete=True
        )

    assert _files(hass) == []
    await _unload(hass, entry)


async def test_a_clip_with_the_history_off_is_refused(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """With the history off nothing is written, not even a temp file."""
    entry = await set_up_warm(hass, seed_warm_cache, options={CONF_EVENT_HISTORY_DAYS: 0})
    with pytest.raises(history.ClipStoreError):
        await _history(entry).async_save_clip(SYNTHETIC.camera_sn, _producer(_clip()), kind="live")
    assert not history.history_dir(hass).exists()
    await _unload(hass, entry)


def test_prune_removes_expired_videos_and_stale_clip_parts(tmp_path: Path) -> None:
    """Dated ``.mp4`` files go like stills; ``.part`` files go once over a day old."""
    camera = tmp_path / "Front"
    camera.mkdir()
    old_video = camera / "2026-09-20_10-00-00_Front_person.mp4"
    kept_video = camera / "2026-09-28_10-00-00_Front_person.mp4"
    stale_part = camera / ".abc.ts.part"
    fresh_part = camera / ".def.mp4.part"
    foreign = camera / "2026-09-01 trip.mp4"  # hygiene: ok (a user file, not history)
    for path in (old_video, kept_video, stale_part, fresh_part, foreign):
        path.write_bytes(b"x")
    stale = (dt_util.utcnow() - timedelta(days=2)).timestamp()
    os.utime(stale_part, (stale, stale))

    removed = history.prune(tmp_path, date(2026, 9, 22))

    assert removed == 2
    assert sorted(path.name for path in camera.iterdir()) == sorted(
        [kept_video.name, fresh_part.name, foreign.name]
    )


async def _set_up_with_a_twin(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    seed_warm_cache: Callable[..., None],
) -> MockConfigEntry:
    """The synthetic camera, a twin whose name cleans to the same folder, and a third."""
    fake_cloud.devices.append(
        camera_device(TWIN_CAMERA_SN, station_sn=fake_station.serial, channel=1, name="Front!")
    )
    fake_cloud.devices.append(
        camera_device(OTHER_CAMERA_SN, station_sn=fake_station.serial, channel=2, name="Back")
    )
    return await set_up_warm(hass, seed_warm_cache)


async def test_stills_of_cameras_whose_names_collide_land_in_folders_with_serial_tails(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Colliding cameras get ``<name>_<last 4 of serial>``; a camera with no collision does not."""
    entry = await _set_up_with_a_twin(hass, fake_station, fake_cloud, seed_warm_cache)
    moment = dt_util.utcnow().replace(microsecond=0)
    serials = (SYNTHETIC.camera_sn, TWIN_CAMERA_SN, OTHER_CAMERA_SN)
    for serial in serials:
        _history(entry).async_save(serial, serial.encode(), moment, "person")
    root = history.history_dir(hass)
    local = dt_util.as_local(moment)
    expected = {
        serial: history.history_path(root, folder, local, "person")
        for serial, folder in (
            (SYNTHETIC.camera_sn, f"{CAMERA_NAME}_{SYNTHETIC.camera_sn[-4:]}"),
            (TWIN_CAMERA_SN, f"{CAMERA_NAME}_{TWIN_CAMERA_SN[-4:]}"),
            (OTHER_CAMERA_SN, "Back"),
        )
    }

    await wait_until(lambda: len(_files(hass)) == len(serials))

    assert _files(hass) == sorted(expected.values())
    for serial, path in expected.items():
        assert path.read_bytes() == serial.encode()
    await _unload(hass, entry)


async def test_a_clip_of_a_camera_whose_name_collides_lands_in_its_tailed_folder(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A clip takes the same ``<name>_<last 4 of serial>`` folder and file name as a still."""
    entry = await _set_up_with_a_twin(hass, fake_station, fake_cloud, seed_warm_cache)
    moment = dt_util.utcnow().replace(microsecond=0)

    saved = await _history(entry).async_save_clip(
        TWIN_CAMERA_SN, _producer(_clip()), kind="person", moment=moment
    )

    folder = f"{CAMERA_NAME}_{TWIN_CAMERA_SN[-4:]}"
    assert saved.path == history.history_path(
        history.history_dir(hass), folder, dt_util.as_local(moment), "person", ".mp4"
    )
    assert [path.parent.name for path in _files(hass)] == [folder]
    await _unload(hass, entry)
