"""Camera snapshots: the detection thumbnail, the 4K trigger frame, the live keyframe.

End to end on the library's loopback ``FakeStation``: it serves stills from its
``images``, plays recordings on a short-lived second session with an encrypted
keyframe, and streams live. Events with a current time, a second camera or no
thumbnail are fed to the router as real library ``SecurityEvent``s.
The HEVC decode runs a real subprocess, ``tests/fake_ffmpeg.py``, in place of HA's
ffmpeg binary (conftest's autouse ``_fake_ffmpeg``): it answers a 3840x2160 JPEG
header for data that really decrypted to Annex-B HEVC, and fails on demand.
"""

from __future__ import annotations

import asyncio
import builtins
import logging
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any, Final

import pytest
from conftest import (
    PUSHED_THUMB_PATH,
    PUSHED_THUMBNAIL,
    SYNTHETIC,
    detection_event,
    entity_id_for,
    now_ms,
    panel_entity_id,
    set_up_warm,
    state_of,
    wait_until,
)
from eufy_home_security import DetectionType, EufySecurity, FrameCipher, Station
from eufy_home_security.testing import FakeCloud, FakeStation, camera_device
from homeassistant.components.alarm_control_panel import DOMAIN as ALARM_DOMAIN
from homeassistant.components.alarm_control_panel import SERVICE_ALARM_ARM_HOME
from homeassistant.components.camera import DOMAIN as CAMERA_DOMAIN
from homeassistant.components.camera import async_get_image
from homeassistant.const import ATTR_ENTITY_ID, EVENT_STATE_CHANGED
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.eufy_home_security import snapshots
from custom_components.eufy_home_security.const import (
    ATTR_IMAGE_SOURCE,
    CAMERA_KEY,
    CONF_CAMERA_IMAGE,
    CONF_LIVE_SNAPSHOT,
    LIVE_SNAPSHOT_COOLDOWN_SECONDS,
    THUMBNAIL_RETRY_DELAY_SECONDS,
)
from custom_components.eufy_home_security.snapshots import SnapshotManager

# The real decoder command, taken before conftest's autouse fixture replaces it.
REAL_FFMPEG_COMMAND: Final = snapshots.ffmpeg_command
# A second camera on the fake station, on channel 1: synthetic, like SENSOR_SN.
OTHER_CAMERA_SN: Final = "T8160P2000000002"
# The fixture station already serves this thumbnail for the path its push names.
THUMB_PATH: Final = PUSHED_THUMB_PATH
CLIP_PATH: Final = "/zx/clip.zxvideo"
THUMBNAIL: Final = PUSHED_THUMBNAIL
OTHER_THUMBNAIL: Final = b"\xff\xd8OTHER-CAMERA\xff\xd9"
# What fake_ffmpeg.py answers for a keyframe that decrypted: a JPEG declaring 3840x2160.
FOUR_K_JPEG_PREFIX: Final = b"\xff\xd8\xff\xc0\x00\x11\x08\x08\x70\x0f\x00"
# A detection's event-database id: its day (YYYYMMDD) times 100000 plus a sequence.
RID: Final = 20260916 * 100_000 + 42
# The same shape with an impossible day (month 13, day 99): a forged LAN push can carry it.
INVALID_RID: Final = 2026139900001
# The thumbnail the station's history row for RID names, and the still it serves there.
ROW_THUMB_PATH: Final = "/zx/hdd_data0/Camera00/20260916/snapshort.jpg"
ROW_THUMBNAIL: Final = b"\xff\xd8ROW-THUMBNAIL\xff\xd9"


def _swallow_media(*_args: object, **_kwargs: object) -> None:
    """In place of the fake station's stream start: it receives an open and sends no frame."""


def _manager(entry: MockConfigEntry) -> SnapshotManager:
    manager: SnapshotManager = entry.runtime_data.snapshots
    return manager


def _station(entry: MockConfigEntry) -> Station:
    station: Station = entry.runtime_data.coordinators[SYNTHETIC.station_sn].station
    return station


def _camera_id(hass: HomeAssistant, serial: str = SYNTHETIC.camera_sn) -> str:
    return entity_id_for(hass, CAMERA_DOMAIN, serial, CAMERA_KEY)


def _commands(station: FakeStation, cmd: int) -> int:
    return sum(1 for obj in station.received if obj.get("cmd") == cmd)


def _record_sources(hass: HomeAssistant, entity_id: str) -> list[str]:
    """Every ``image_source`` the camera is written with from now on, in order."""
    sources: list[str] = []

    @callback
    def _on_change(event: Event[Any]) -> None:
        new = event.data["new_state"]
        if event.data[ATTR_ENTITY_ID] != entity_id or new is None:
            return
        source = new.attributes.get(ATTR_IMAGE_SOURCE)
        if source is not None and (not sources or sources[-1] != source):
            sources.append(source)

    hass.bus.async_listen(EVENT_STATE_CHANGED, _on_change)
    return sources


async def _image(hass: HomeAssistant, entity_id: str) -> bytes | None:
    """What the camera serves to a view, None when it has no image."""
    try:
        return (await async_get_image(hass, entity_id)).content
    except HomeAssistantError:
        return None


async def _unload(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def _set_up_with_other_camera(
    hass: HomeAssistant, fake_station: FakeStation, fake_cloud: FakeCloud, seed: Callable[..., None]
) -> MockConfigEntry:
    """Pair a second camera before the warm cache copies the cloud's device list."""
    fake_cloud.devices.append(
        camera_device(OTHER_CAMERA_SN, station_sn=fake_station.serial, channel=1, name="Back")
    )
    fake_station.params[1] = {1101: "80", 1142: "-60"}
    return await set_up_warm(hass, seed)


# ── the thumbnail of the camera's own latest detection ────────────────────────


async def test_each_camera_gets_one_camera_entity_with_no_image_before_a_detection(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """One camera entity per camera; none for the station; no image and no fetch at first."""
    entry = await _set_up_with_other_camera(hass, fake_station, fake_cloud, seed_warm_cache)
    for serial in (SYNTHETIC.camera_sn, OTHER_CAMERA_SN):
        entity_id = _camera_id(hass, serial)
        assert state_of(hass, entity_id) == "idle"
        assert await _image(hass, entity_id) is None
    assert len(hass.states.async_entity_ids(CAMERA_DOMAIN)) == 2
    # The option is off: a view of a camera with no image asks the station for nothing.
    await asyncio.sleep(0.2)
    assert _commands(fake_station, 1003) == 0
    assert _commands(fake_station, 1308) == 0
    await _unload(hass, entry)


async def test_a_pushed_detection_shows_its_thumbnail_then_its_trigger_frame(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A P2P detection: the thumbnail first, then the 4K trigger frame of its recording."""
    fake_station.images[THUMB_PATH] = THUMBNAIL
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = _camera_id(hass)
    sources = _record_sources(hass, entity_id)
    closes_before = fake_station.client_closes

    fake_station.push_camera_event()
    await wait_until(lambda: sources == ["thumbnail", "trigger_frame"])
    await wait_until(lambda: not _manager(entry).busy)

    image = await _image(hass, entity_id)
    assert image is not None and image.startswith(FOUR_K_JPEG_PREFIX)
    # Home Assistant's own Camera default serves the JPEG as a JPEG.
    assert (await async_get_image(hass, entity_id)).content_type == "image/jpeg"
    state = hass.states.get(entity_id)
    assert state is not None
    # The detection's own time; never a path, a serial or the device name.
    assert state.attributes["triggered_at"].startswith("2023-11-14T22:13:20")
    assert all("/zx/" not in str(value) for value in state.attributes.values())
    assert _commands(fake_station, 1308) == 1
    assert _commands(fake_station, 1025) == 1
    # The recording played on a short-lived session that was closed again.
    await wait_until(lambda: fake_station.client_closes == closes_before + 1)
    assert _station(entry).connected
    await _unload(hass, entry)


async def test_a_detection_never_shows_on_another_camera(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The back camera's detection is the back camera's; unpaired or foreign ones show nowhere."""
    fake_station.images["/zx/other.jpg"] = OTHER_THUMBNAIL
    fake_station.images["/zx/foreign.jpg"] = THUMBNAIL
    entry = await _set_up_with_other_camera(hass, fake_station, fake_cloud, seed_warm_cache)
    router = entry.runtime_data.router

    router.handle(
        detection_event(
            DetectionType.PERSON,
            t_ms=now_ms(),
            device_sn=OTHER_CAMERA_SN,
            thumb_path="/zx/other.jpg",
        )
    )
    await wait_until(lambda: _manager(entry).image_for(OTHER_CAMERA_SN) is not None)
    assert await _image(hass, _camera_id(hass, OTHER_CAMERA_SN)) == OTHER_THUMBNAIL
    assert await _image(hass, _camera_id(hass)) is None

    # A serial this station does not pair, and a detection another station delivered.
    router.handle(
        detection_event(
            DetectionType.PERSON,
            t_ms=now_ms(),
            device_sn="T8160P2000000003",
            thumb_path="/zx/foreign.jpg",
        )
    )
    router.handle(
        detection_event(
            DetectionType.PERSON,
            t_ms=now_ms(),
            station_sn="T8030P2000000009",
            thumb_path="/zx/foreign.jpg",
        )
    )
    await asyncio.sleep(0.3)
    assert _commands(fake_station, 1308) == 1
    assert await _image(hass, _camera_id(hass)) is None
    assert await _image(hass, _camera_id(hass, OTHER_CAMERA_SN)) == OTHER_THUMBNAIL
    await _unload(hass, entry)


async def test_an_obfuscated_thumbnail_is_never_served(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A still the library labels as not an image (V1 obfuscation) leaves the camera empty."""
    fake_station.images[THUMB_PATH] = b"eufysecurity" + bytes(300)
    entry = await set_up_warm(hass, seed_warm_cache)

    entry.runtime_data.router.handle(
        detection_event(DetectionType.PERSON, t_ms=now_ms(), thumb_path=THUMB_PATH)
    )
    await wait_until(lambda: _commands(fake_station, 1308) == 1)
    await wait_until(lambda: not _manager(entry).busy)
    assert await _image(hass, _camera_id(hass)) is None
    await _unload(hass, entry)


async def test_an_older_detection_never_replaces_a_newer_still(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A detection delivered late, older than the one shown, is not even fetched."""
    fake_station.images["/zx/new.jpg"] = THUMBNAIL
    fake_station.images["/zx/old.jpg"] = OTHER_THUMBNAIL
    entry = await set_up_warm(hass, seed_warm_cache)
    router = entry.runtime_data.router
    now = now_ms()

    router.handle(detection_event(DetectionType.PERSON, t_ms=now, thumb_path="/zx/new.jpg"))
    await wait_until(lambda: _manager(entry).image_for(SYNTHETIC.camera_sn) == THUMBNAIL)
    router.handle(
        detection_event(DetectionType.MOTION, t_ms=now - 60_000, thumb_path="/zx/old.jpg")
    )
    await asyncio.sleep(0.3)

    assert _commands(fake_station, 1308) == 1
    assert await _image(hass, _camera_id(hass)) == THUMBNAIL
    await _unload(hass, entry)


async def test_a_future_dated_unauthenticated_push_never_blocks_later_detections(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """An ECB push claiming a time 5 minutes ahead ranks at its arrival, not its claim."""
    fake_station.images["/zx/forged.jpg"] = OTHER_THUMBNAIL
    fake_station.images["/zx/real.jpg"] = THUMBNAIL
    entry = await set_up_warm(hass, seed_warm_cache)
    manager = _manager(entry)
    router = entry.runtime_data.router

    router.handle(
        detection_event(
            DetectionType.PERSON,
            t_ms=now_ms() + 300_000,
            cipher=FrameCipher.ECB,
            unique_id="occ-forged",
            thumb_path="/zx/forged.jpg",
        )
    )
    await wait_until(lambda: manager.image_for(SYNTHETIC.camera_sn) == OTHER_THUMBNAIL)
    await wait_until(lambda: not manager.busy)
    shown = manager.event_time_for(SYNTHETIC.camera_sn)
    assert shown is not None and shown <= now_ms(), "a claimed future time was shown"

    await asyncio.sleep(0.01)
    real_ms = now_ms()
    router.handle(
        detection_event(
            DetectionType.PERSON, t_ms=real_ms, unique_id="occ-real", thumb_path="/zx/real.jpg"
        )
    )
    await wait_until(lambda: manager.image_for(SYNTHETIC.camera_sn) == THUMBNAIL)
    assert manager.event_time_for(SYNTHETIC.camera_sn) == real_ms
    await _unload(hass, entry)


async def test_an_older_detection_never_preempts_a_newer_one_in_flight(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """An older detection delivered while a newer one's thumbnail is in flight is ignored.

    The newer detection keeps its own trigger frame; the older one is not fetched at all.
    """
    fake_station.images["/zx/new.jpg"] = THUMBNAIL
    fake_station.images["/zx/old.jpg"] = OTHER_THUMBNAIL
    fake_station.image_reply_delay["/zx/new.jpg"] = 0.5
    entry = await set_up_warm(hass, seed_warm_cache)
    manager = _manager(entry)
    router = entry.runtime_data.router
    now = now_ms()

    router.handle(
        detection_event(
            DetectionType.PERSON,
            t_ms=now,
            unique_id="occ-new",
            thumb_path="/zx/new.jpg",
            video_path=CLIP_PATH,
        )
    )
    await wait_until(lambda: _commands(fake_station, 1308) == 1)
    router.handle(
        detection_event(
            DetectionType.MOTION,
            t_ms=now - 60_000,
            unique_id="occ-old",
            thumb_path="/zx/old.jpg",
            video_path=CLIP_PATH,
        )
    )
    await wait_until(lambda: manager.source_for(SYNTHETIC.camera_sn) == "trigger_frame")
    await wait_until(lambda: not manager.busy)
    await hass.async_block_till_done()

    assert _commands(fake_station, 1308) == 1, "the older detection's thumbnail was fetched"
    assert _commands(fake_station, 1025) == 1
    assert manager.event_time_for(SYNTHETIC.camera_sn) == now
    await _unload(hass, entry)


async def test_an_older_detection_never_cancels_a_newer_ones_owed_thumbnail_retry(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The newer detection showed nothing and is owed a retry: an older one changes nothing."""
    monkeypatch.setenv("FAKE_FFMPEG_FAIL", "1")
    fake_station.images["/zx/old.jpg"] = OTHER_THUMBNAIL
    entry = await set_up_warm(hass, seed_warm_cache)
    manager = _manager(entry)

    await _miss_without_trigger_frame(entry, fake_station)
    assert manager.image_for(SYNTHETIC.camera_sn) is None
    entry.runtime_data.router.handle(
        detection_event(
            DetectionType.MOTION,
            t_ms=now_ms() - 60_000,
            unique_id="occ-old",
            thumb_path="/zx/old.jpg",
            video_path=CLIP_PATH,
        )
    )
    await wait_until(lambda: not manager.busy)
    await hass.async_block_till_done()
    assert _commands(fake_station, 1308) == 0, "the older detection was fetched"
    assert _commands(fake_station, 1025) == 1
    assert await _image(hass, _camera_id(hass)) is None

    # The newer detection's retry survived: it now finds the written row.
    _seed_row(fake_station)
    await _fire_retry_delay(hass, THUMBNAIL_RETRY_DELAY_SECONDS)
    await wait_until(lambda: len(fake_station.history_queries) == 2)
    await wait_until(lambda: manager.image_for(SYNTHETIC.camera_sn) == ROW_THUMBNAIL)
    await _unload(hass, entry)


@pytest.mark.parametrize("with_recording", [True, False])
async def test_a_detection_shows_the_thumbnail_of_its_history_row(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    with_recording: bool,
) -> None:
    """A push with only a record_id: the library finds the thumbnail in its history row."""
    fake_station.rows = [
        {"record_id": RID, "device_sn": SYNTHETIC.camera_sn, "thumb_path": ROW_THUMB_PATH}
    ]
    fake_station.images[ROW_THUMB_PATH] = ROW_THUMBNAIL
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = _camera_id(hass)
    sources = _record_sources(hass, entity_id)
    fields: dict[str, Any] = {"record_id": RID}
    if with_recording:
        fields["video_path"] = CLIP_PATH

    entry.runtime_data.router.handle(detection_event(DetectionType.PERSON, t_ms=now_ms(), **fields))
    expected = ["thumbnail", "trigger_frame"] if with_recording else ["thumbnail"]
    await wait_until(lambda: sources == expected)
    await wait_until(lambda: not _manager(entry).busy)

    assert sources == expected
    assert _commands(fake_station, 1308) == 1
    if with_recording:
        assert _commands(fake_station, 1025) == 1
    else:
        assert await _image(hass, entity_id) == ROW_THUMBNAIL
        assert _commands(fake_station, 1025) == 0
    assert len(fake_station.history_queries) == 1
    state = hass.states.get(entity_id)
    assert state is not None
    assert all(
        "/zx/" not in str(value) and str(RID) not in str(value)
        for value in state.attributes.values()
    )
    await _unload(hass, entry)


async def test_another_cameras_row_is_never_shown(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The history row for the record_id is the back camera's: no still, on either camera."""
    fake_station.rows = [
        {"record_id": RID, "device_sn": OTHER_CAMERA_SN, "thumb_path": ROW_THUMB_PATH}
    ]
    fake_station.images[ROW_THUMB_PATH] = ROW_THUMBNAIL
    entry = await _set_up_with_other_camera(hass, fake_station, fake_cloud, seed_warm_cache)

    entry.runtime_data.router.handle(
        detection_event(DetectionType.PERSON, t_ms=now_ms(), record_id=RID)
    )
    await wait_until(lambda: len(fake_station.history_queries) == 1)
    await wait_until(lambda: not _manager(entry).busy)

    assert _commands(fake_station, 1308) == 0
    assert await _image(hass, _camera_id(hass)) is None
    assert await _image(hass, _camera_id(hass, OTHER_CAMERA_SN)) is None
    await _unload(hass, entry)


async def test_a_thumbnail_not_yet_written_still_reaches_the_trigger_frame(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Right after a live detection its history row is not written: the trigger frame still shows."""
    entry = await set_up_warm(hass, seed_warm_cache)
    manager = _manager(entry)

    entry.runtime_data.router.handle(
        detection_event(DetectionType.PERSON, t_ms=now_ms(), record_id=RID, video_path=CLIP_PATH)
    )
    await wait_until(lambda: manager.source_for(SYNTHETIC.camera_sn) == "trigger_frame")
    await wait_until(lambda: not manager.busy)

    image = await _image(hass, _camera_id(hass))
    assert image is not None and image.startswith(FOUR_K_JPEG_PREFIX)
    assert len(fake_station.history_queries) == 1
    assert _commands(fake_station, 1308) == 0
    assert _commands(fake_station, 1025) == 1
    await _unload(hass, entry)


async def test_a_thumbnail_past_its_safety_bound_still_reaches_the_trigger_frame(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The thumbnail tier has its own outer bound: a stalled one does not hold the worker."""
    monkeypatch.setattr(snapshots, "THUMBNAIL_TIMEOUT_SECONDS", 0.2)
    fake_station.images[THUMB_PATH] = THUMBNAIL
    # Shorter than the library's own 12 s still timeout, longer than the wait below.
    reply_delay = 2.5
    fake_station.image_reply_delay[THUMB_PATH] = reply_delay
    entry = await set_up_warm(hass, seed_warm_cache)
    manager = _manager(entry)
    loop = asyncio.get_running_loop()

    sent_at = loop.time()
    entry.runtime_data.router.handle(
        detection_event(
            DetectionType.PERSON, t_ms=now_ms(), thumb_path=THUMB_PATH, video_path=CLIP_PATH
        )
    )
    await wait_until(
        lambda: manager.source_for(SYNTHETIC.camera_sn) == "trigger_frame", timeout=1.5
    )
    assert _commands(fake_station, 1308) == 1
    assert _commands(fake_station, 1025) == 1
    # Let the station's late reply go out, so no timer outlives the test; it changes nothing.
    await asyncio.sleep(max(0.0, sent_at + reply_delay + 0.3 - loop.time()))
    await wait_until(lambda: not manager.busy)
    assert manager.source_for(SYNTHETIC.camera_sn) == "trigger_frame"
    await _unload(hass, entry)


async def test_an_invalid_record_id_logs_no_error_and_still_reaches_the_trigger_frame(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """An impossible-day record_id: no error, no id in the log, the trigger frame shows."""
    caplog.set_level(logging.DEBUG)
    entry = await set_up_warm(hass, seed_warm_cache)
    manager = _manager(entry)

    entry.runtime_data.router.handle(
        detection_event(
            DetectionType.PERSON, t_ms=now_ms(), record_id=INVALID_RID, video_path=CLIP_PATH
        )
    )
    await wait_until(lambda: manager.source_for(SYNTHETIC.camera_sn) == "trigger_frame")
    await wait_until(lambda: not manager.busy)

    ours = [r for r in caplog.records if r.name.startswith("custom_components.eufy_home_security")]
    assert not [r for r in ours if r.levelno >= logging.ERROR]
    assert all(str(INVALID_RID) not in r.getMessage() for r in ours)
    assert len(fake_station.history_queries) == 0
    await _unload(hass, entry)


# ── one later thumbnail attempt when the row was not written and no trigger frame landed ───────


async def _fire_retry_delay(hass: HomeAssistant, seconds: float) -> None:
    """Move Home Assistant's clock past the thumbnail retry's delay and let it run."""
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds + 1))
    await hass.async_block_till_done()


def _seed_row(fake_station: FakeStation) -> None:
    """The station has now written the history row for RID, and its thumbnail."""
    fake_station.rows = [
        {"record_id": RID, "device_sn": SYNTHETIC.camera_sn, "thumb_path": ROW_THUMB_PATH}
    ]
    fake_station.images[ROW_THUMB_PATH] = ROW_THUMBNAIL


async def _miss_without_trigger_frame(entry: MockConfigEntry, fake_station: FakeStation) -> None:
    """occ-1 with a record_id not written yet and a trigger frame that does not decode."""
    manager = _manager(entry)
    entry.runtime_data.router.handle(
        detection_event(
            DetectionType.PERSON,
            t_ms=now_ms(),
            unique_id="occ-1",
            record_id=RID,
            video_path=CLIP_PATH,
        )
    )
    await wait_until(
        lambda: (
            len(fake_station.history_queries) == 1
            and _commands(fake_station, 1025) == 1
            and not manager.busy
        )
    )


@pytest.mark.parametrize("row_written", [True, False])
async def test_a_thumbnail_not_yet_written_gets_one_retry_when_the_trigger_frame_did_not_land(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    row_written: bool,
) -> None:
    """One deferred thumbnail lookup, never a second, never a trigger-frame replay."""
    monkeypatch.setenv("FAKE_FFMPEG_FAIL", "1")
    entry = await set_up_warm(hass, seed_warm_cache)
    manager = _manager(entry)
    entity_id = _camera_id(hass)

    await _miss_without_trigger_frame(entry, fake_station)
    assert await _image(hass, entity_id) is None
    if row_written:
        _seed_row(fake_station)

    await _fire_retry_delay(hass, THUMBNAIL_RETRY_DELAY_SECONDS)
    await wait_until(lambda: len(fake_station.history_queries) == 2)
    await wait_until(lambda: not manager.busy)
    if row_written:
        assert manager.source_for(SYNTHETIC.camera_sn) == "thumbnail"
        assert await _image(hass, entity_id) == ROW_THUMBNAIL
    else:
        assert await _image(hass, entity_id) is None

    for _ in range(2):
        await _fire_retry_delay(hass, THUMBNAIL_RETRY_DELAY_SECONDS)
    await asyncio.sleep(0.3)
    assert len(fake_station.history_queries) == 2, "the thumbnail was retried more than once"
    assert _commands(fake_station, 1025) == 1, "the retry replayed the trigger frame"
    await _unload(hass, entry)


async def test_no_thumbnail_retry_once_the_trigger_frame_landed(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The detection's own trigger frame is shown: no later thumbnail lookup."""
    entry = await set_up_warm(hass, seed_warm_cache)
    manager = _manager(entry)

    entry.runtime_data.router.handle(
        detection_event(
            DetectionType.PERSON,
            t_ms=now_ms(),
            unique_id="occ-1",
            record_id=RID,
            video_path=CLIP_PATH,
        )
    )
    await wait_until(lambda: manager.source_for(SYNTHETIC.camera_sn) == "trigger_frame")
    await wait_until(lambda: not manager.busy)
    _seed_row(fake_station)

    await _fire_retry_delay(hass, THUMBNAIL_RETRY_DELAY_SECONDS)
    await asyncio.sleep(0.3)
    assert len(fake_station.history_queries) == 1
    assert manager.source_for(SYNTHETIC.camera_sn) == "trigger_frame"
    await _unload(hass, entry)


@pytest.mark.parametrize("cancel_by", ["newer_detection", "unload"])
async def test_a_pending_thumbnail_retry_is_dropped_for_a_newer_detection_or_at_unload(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    cancel_by: str,
) -> None:
    """A newer detection or an unload drops the scheduled retry; no timer lingers."""
    monkeypatch.setenv("FAKE_FFMPEG_FAIL", "1")
    entry = await set_up_warm(hass, seed_warm_cache)
    manager = _manager(entry)

    await _miss_without_trigger_frame(entry, fake_station)
    _seed_row(fake_station)
    camera = manager._cameras[SYNTHETIC.camera_sn]
    # The retry is owed and its timer is set: the drop below is observable.
    owed, timer = camera.retry_event, camera.cancel_retry
    assert owed is not None
    assert timer is not None

    if cancel_by == "newer_detection":
        entry.runtime_data.router.handle(
            detection_event(
                DetectionType.MOTION, t_ms=now_ms(), unique_id="occ-2", thumb_path=THUMB_PATH
            )
        )
        # Dropped at once, when the newer detection is accepted, not when the timer fires.
        assert camera.retry_event is None, "the newer detection did not drop the owed retry"
        assert camera.cancel_retry is None, "the newer detection left the retry timer set"
        await wait_until(lambda: manager.image_for(SYNTHETIC.camera_sn) == THUMBNAIL)
        await wait_until(lambda: not manager.busy)
        await _fire_retry_delay(hass, THUMBNAIL_RETRY_DELAY_SECONDS)
        await wait_until(lambda: not manager.busy)
        assert len(fake_station.history_queries) == 1
        assert await _image(hass, _camera_id(hass)) == THUMBNAIL
        await _unload(hass, entry)
    else:
        await _unload(hass, entry)
        assert camera.retry_event is None, "unload did not drop the owed retry"
        assert camera.cancel_retry is None, "unload left the retry timer set"
        await _fire_retry_delay(hass, THUMBNAIL_RETRY_DELAY_SECONDS)
        assert len(fake_station.history_queries) == 1


# A record_id whose history row the station never wrote: a forged push can carry it.
UNWRITTEN_RID: Final = RID + 1


@pytest.mark.parametrize("junk_record_id", [UNWRITTEN_RID, INVALID_RID])
async def test_a_forged_push_that_fetched_nothing_never_blocks_a_genuine_detection_in_transit(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    junk_record_id: int,
) -> None:
    """A genuine detection triggered before an ECB push arrived still shows.

    A real push reaches the host seconds after its trigger time, so a forged ECB push
    stamped "now" lands between a genuine detection's trigger and its delivery. The
    forged push fetches nothing (its row is unwritten or its id impossible), yet it
    must not count as a newer detection against the authenticated one.
    """
    fake_station.images["/zx/real.jpg"] = THUMBNAIL
    entry = await set_up_warm(hass, seed_warm_cache)
    manager = _manager(entry)
    router = entry.runtime_data.router

    genuine_trigger = now_ms()
    await asyncio.sleep(0.05)
    router.handle(
        detection_event(
            DetectionType.PERSON,
            t_ms=now_ms(),
            cipher=FrameCipher.ECB,
            unique_id="occ-forged",
            record_id=junk_record_id,
        )
    )
    await wait_until(lambda: not manager.busy)
    await hass.async_block_till_done()
    assert manager.image_for(SYNTHETIC.camera_sn) is None

    router.handle(
        detection_event(
            DetectionType.PERSON,
            t_ms=genuine_trigger,
            unique_id="occ-real",
            thumb_path="/zx/real.jpg",
        )
    )
    await wait_until(lambda: manager.image_for(SYNTHETIC.camera_sn) == THUMBNAIL)
    assert manager.event_time_for(SYNTHETIC.camera_sn) == genuine_trigger
    await wait_until(lambda: not manager.busy)
    await _unload(hass, entry)


async def test_a_forged_push_never_cancels_a_genuine_detections_owed_thumbnail_retry(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """An ECB push with an unwritten record_id leaves a genuine detection's retry owed."""
    monkeypatch.setenv("FAKE_FFMPEG_FAIL", "1")
    entry = await set_up_warm(hass, seed_warm_cache)
    manager = _manager(entry)

    await _miss_without_trigger_frame(entry, fake_station)
    camera = manager._cameras[SYNTHETIC.camera_sn]
    assert camera.retry_event is not None and camera.retry_event.authenticated
    await asyncio.sleep(0.05)
    entry.runtime_data.router.handle(
        detection_event(
            DetectionType.PERSON,
            t_ms=now_ms(),
            cipher=FrameCipher.ECB,
            unique_id="occ-forged",
            record_id=UNWRITTEN_RID,
        )
    )
    assert camera.retry_event is not None, "the forged push dropped the owed retry"
    assert camera.cancel_retry is not None, "the forged push cancelled the retry timer"
    await wait_until(lambda: not manager.busy)
    await hass.async_block_till_done()

    _seed_row(fake_station)
    await _fire_retry_delay(hass, THUMBNAIL_RETRY_DELAY_SECONDS)
    await wait_until(lambda: manager.image_for(SYNTHETIC.camera_sn) == ROW_THUMBNAIL)
    assert manager.source_for(SYNTHETIC.camera_sn) == "thumbnail"
    await wait_until(lambda: not manager.busy)
    await _unload(hass, entry)


async def test_a_thumbnail_retry_never_downgrades_its_own_trigger_frame(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """An enriching copy lands the trigger frame after the retry was scheduled: no lookup."""
    monkeypatch.setenv("FAKE_FFMPEG_FAIL", "1")
    entry = await set_up_warm(hass, seed_warm_cache)
    manager = _manager(entry)
    t = now_ms()

    entry.runtime_data.router.handle(
        detection_event(
            DetectionType.PERSON, t_ms=t, unique_id="occ-1", record_id=RID, video_path=CLIP_PATH
        )
    )
    await wait_until(
        lambda: (
            len(fake_station.history_queries) == 1
            and _commands(fake_station, 1025) == 1
            and not manager.busy
        )
    )
    monkeypatch.delenv("FAKE_FFMPEG_FAIL")
    entry.runtime_data.router.handle(
        detection_event(
            DetectionType.PERSON,
            t_ms=t,
            unique_id="occ-1",
            record_id=RID,
            video_path=CLIP_PATH,
            enriches=True,
        )
    )
    await wait_until(lambda: manager.source_for(SYNTHETIC.camera_sn) == "trigger_frame")
    await wait_until(lambda: not manager.busy)
    assert len(fake_station.history_queries) == 2

    _seed_row(fake_station)
    await _fire_retry_delay(hass, THUMBNAIL_RETRY_DELAY_SECONDS)
    await asyncio.sleep(0.3)
    assert len(fake_station.history_queries) == 2
    assert manager.source_for(SYNTHETIC.camera_sn) == "trigger_frame"
    await _unload(hass, entry)


# ── the 4K trigger frame, and the thumbnail kept when decoding fails ──────────


async def test_the_decoder_command_imports_nothing_on_the_event_loop(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ffmpeg integration is resolved at module import, never per decode.

    With the ffmpeg integration not set up (and ``haffmpeg`` not installed),
    the bare binary name is the command, and asking for it imports nothing.
    """
    real_import = builtins.__import__

    def _no_ffmpeg_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith("homeassistant.components.ffmpeg"):
            raise AssertionError("ffmpeg_command imported a module on the event loop")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_ffmpeg_import)
    for _ in range(2):
        assert REAL_FFMPEG_COMMAND(hass) == ["ffmpeg"]


async def test_a_detection_without_a_thumbnail_shows_its_trigger_frame(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Most pushes carry no thumbnail: the trigger frame covers them."""
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = _camera_id(hass)

    entry.runtime_data.router.handle(
        detection_event(DetectionType.PERSON, t_ms=now_ms(), video_path=CLIP_PATH)
    )
    await wait_until(lambda: _manager(entry).source_for(SYNTHETIC.camera_sn) is not None)

    image = await _image(hass, entity_id)
    assert image is not None and image.startswith(FOUR_K_JPEG_PREFIX)
    state = hass.states.get(entity_id)
    assert state is not None and state.attributes[ATTR_IMAGE_SOURCE] == "trigger_frame"
    assert _commands(fake_station, 1308) == 0
    await _unload(hass, entry)


@pytest.mark.parametrize("failure", ["decoder_fails", "no_decoder", "decoder_times_out"])
async def test_a_trigger_frame_that_does_not_decode_keeps_the_thumbnail(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    failure: str,
) -> None:
    """ffmpeg failing, missing or too slow: the thumbnail stays; the raw HEVC is never served."""
    if failure == "decoder_fails":
        monkeypatch.setenv("FAKE_FFMPEG_FAIL", "1")
    elif failure == "no_decoder":
        monkeypatch.setattr(snapshots, "ffmpeg_command", lambda _hass: ["/nonexistent/ffmpeg"])
    else:
        monkeypatch.setenv("FAKE_FFMPEG_SLEEP", "5")
        monkeypatch.setattr(snapshots, "FFMPEG_DECODE_TIMEOUT_SECONDS", 0.3)
    fake_station.images[THUMB_PATH] = THUMBNAIL
    entry = await set_up_warm(hass, seed_warm_cache)

    fake_station.push_camera_event()
    await wait_until(lambda: _commands(fake_station, 1025) == 1)
    await wait_until(lambda: not _manager(entry).busy)

    assert await _image(hass, _camera_id(hass)) == THUMBNAIL
    assert _manager(entry).source_for(SYNTHETIC.camera_sn) == "thumbnail"
    await _unload(hass, entry)


async def test_a_later_copy_of_a_detection_never_downgrades_its_trigger_frame(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """An enriching copy with the thumbnail arrives after the 4K frame: nothing is fetched."""
    fake_station.images[THUMB_PATH] = THUMBNAIL
    entry = await set_up_warm(hass, seed_warm_cache)
    router = entry.runtime_data.router
    t = now_ms()

    router.handle(
        detection_event(DetectionType.PERSON, t_ms=t, unique_id="occ-1", video_path=CLIP_PATH)
    )
    await wait_until(lambda: _manager(entry).source_for(SYNTHETIC.camera_sn) == "trigger_frame")
    router.handle(
        detection_event(
            DetectionType.PERSON,
            t_ms=t,
            unique_id="occ-1",
            video_path=CLIP_PATH,
            thumb_path=THUMB_PATH,
            enriches=True,
        )
    )
    await asyncio.sleep(0.3)

    assert _commands(fake_station, 1308) == 0
    assert _commands(fake_station, 1025) == 1
    assert _manager(entry).source_for(SYNTHETIC.camera_sn) == "trigger_frame"
    await _unload(hass, entry)


async def test_one_media_operation_per_station_at_a_time(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Two cameras detecting at once: their fetches run one after the other."""
    fake_station.images[THUMB_PATH] = THUMBNAIL
    fake_station.images["/zx/other.jpg"] = OTHER_THUMBNAIL
    entry = await _set_up_with_other_camera(hass, fake_station, fake_cloud, seed_warm_cache)
    station = _station(entry)
    running = 0
    most = 0

    def counting(method: Callable[..., Any]) -> Callable[..., Any]:
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            nonlocal running, most
            running += 1
            most = max(most, running)
            try:
                return await method(*args, **kwargs)
            finally:
                running -= 1

        return wrapper

    # The library fetches the still inside async_event_thumbnail: wrap only the outer call.
    monkeypatch.setattr(station, "async_event_thumbnail", counting(station.async_event_thumbnail))
    monkeypatch.setattr(
        station, "async_event_trigger_frame", counting(station.async_event_trigger_frame)
    )
    router = entry.runtime_data.router
    t = now_ms()
    router.handle(
        detection_event(DetectionType.PERSON, t_ms=t, thumb_path=THUMB_PATH, video_path=CLIP_PATH)
    )
    router.handle(
        detection_event(
            DetectionType.PET,
            t_ms=t,
            device_sn=OTHER_CAMERA_SN,
            thumb_path="/zx/other.jpg",
            video_path=CLIP_PATH,
        )
    )
    manager = _manager(entry)
    await wait_until(
        lambda: (
            manager.source_for(SYNTHETIC.camera_sn) == "trigger_frame"
            and manager.source_for(OTHER_CAMERA_SN) == "trigger_frame"
        ),
        timeout=10,
    )
    assert most == 1
    assert _commands(fake_station, 1308) == 2
    assert _commands(fake_station, 1025) == 2
    await _unload(hass, entry)


# ── the opt-in live keyframe, once per cooldown, never delaying an arm ─────────


async def test_with_the_option_on_a_camera_without_an_image_shows_a_live_keyframe_once_per_cooldown(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A view schedules one live keyframe; more views within the cooldown ask nothing more."""
    clock = time.monotonic()
    monkeypatch.setattr(snapshots, "_monotonic", lambda: clock)
    monkeypatch.setenv("FAKE_FFMPEG_FAIL", "1")
    entry = await set_up_warm(hass, seed_warm_cache, options={CONF_LIVE_SNAPSHOT: True})
    entity_id = _camera_id(hass)
    manager = _manager(entry)

    # A view never waits on the station: no image now, one live open in the background.
    assert await _image(hass, entity_id) is None
    await wait_until(lambda: _commands(fake_station, 1003) == 1)
    await wait_until(lambda: not manager.busy)
    for _ in range(3):
        assert await _image(hass, entity_id) is None
    await asyncio.sleep(0.2)
    assert _commands(fake_station, 1003) == 1, "a live keyframe was asked twice in one cooldown"

    clock += LIVE_SNAPSHOT_COOLDOWN_SECONDS + 1
    monkeypatch.delenv("FAKE_FFMPEG_FAIL")
    assert await _image(hass, entity_id) is None
    await wait_until(lambda: manager.source_for(SYNTHETIC.camera_sn) == "live")
    image = await _image(hass, entity_id)
    assert image is not None and image.startswith(FOUR_K_JPEG_PREFIX)
    assert _commands(fake_station, 1003) == 2

    # A detection image replaces the live one, and the camera asks for no more live.
    fake_station.images[THUMB_PATH] = THUMBNAIL
    entry.runtime_data.router.handle(
        detection_event(DetectionType.MOTION, t_ms=now_ms(), thumb_path=THUMB_PATH)
    )
    await wait_until(lambda: manager.source_for(SYNTHETIC.camera_sn) == "thumbnail")
    clock += LIVE_SNAPSHOT_COOLDOWN_SECONDS + 1
    assert await _image(hass, entity_id) == THUMBNAIL
    await asyncio.sleep(0.2)
    assert _commands(fake_station, 1003) == 2
    await _unload(hass, entry)


async def test_an_arm_while_a_trigger_frame_is_in_flight_is_not_delayed(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The trigger frame's short-lived session hangs in its handshake; arm home still lands at once."""
    entry = await set_up_warm(hass, seed_warm_cache)
    manager = _manager(entry)
    conn_inits = fake_station.conn_inits
    # The station's own session is up; the short-lived one never gets its key.
    fake_station.answer_conn_init = False

    entry.runtime_data.router.handle(
        detection_event(DetectionType.PERSON, t_ms=now_ms(), video_path=CLIP_PATH)
    )
    await wait_until(lambda: fake_station.conn_inits > conn_inits)
    assert manager.busy

    started = time.monotonic()
    await hass.services.async_call(
        ALARM_DOMAIN,
        SERVICE_ALARM_ARM_HOME,
        {ATTR_ENTITY_ID: panel_entity_id(hass)},
        blocking=True,
    )
    elapsed = time.monotonic() - started

    assert fake_station.guard_mode == 1
    assert state_of(hass, panel_entity_id(hass)) == "armed_home"
    assert elapsed < 2.0, f"arming waited {elapsed:.1f} s behind the trigger frame"
    assert manager.busy, "the trigger frame had already ended: the test proved nothing"
    assert manager.image_for(SYNTHETIC.camera_sn) is None
    await _unload(hass, entry)


async def test_an_arm_while_a_live_keyframe_is_in_flight_is_not_delayed(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The live open is received but no frame follows; arm home still lands at once."""
    entry = await set_up_warm(hass, seed_warm_cache, options={CONF_LIVE_SNAPSHOT: True})
    manager = _manager(entry)
    monkeypatch.setattr(fake_station, "_start_media", _swallow_media)

    assert await _image(hass, _camera_id(hass)) is None
    await wait_until(lambda: _commands(fake_station, 1003) == 1)
    assert manager.busy

    started = time.monotonic()
    await hass.services.async_call(
        ALARM_DOMAIN,
        SERVICE_ALARM_ARM_HOME,
        {ATTR_ENTITY_ID: panel_entity_id(hass)},
        blocking=True,
    )
    elapsed = time.monotonic() - started

    assert elapsed < 2.0, f"arming waited {elapsed:.1f} s behind the live keyframe"
    assert fake_station.guard_mode == 1
    assert state_of(hass, panel_entity_id(hass)) == "armed_home"
    assert manager.busy, "the live keyframe had already ended: the test proved nothing"
    assert manager.image_for(SYNTHETIC.camera_sn) is None
    await _unload(hass, entry)


async def test_unload_stops_a_fetch_in_flight(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Unloading while a trigger frame hangs ends the worker; a later request is ignored."""
    entry = await set_up_warm(hass, seed_warm_cache)
    manager = _manager(entry)
    station = _station(entry)
    fake_station.answer_conn_init = False
    entry.runtime_data.router.handle(
        detection_event(DetectionType.PERSON, t_ms=now_ms(), video_path=CLIP_PATH)
    )
    await wait_until(lambda: manager.busy)

    await _unload(hass, entry)
    assert not manager.busy
    manager.async_request_event(
        station, detection_event(DetectionType.PERSON, t_ms=now_ms(), video_path=CLIP_PATH)
    )
    assert not manager.busy


# ── the Camera image option gates the detection tiers ──────────────────────────


def _count_decoder_calls(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Count every decoder command asked for from now on; the decode itself still runs."""
    calls: list[int] = []
    inner = snapshots.ffmpeg_command

    def counting(hass: HomeAssistant) -> list[str]:
        calls.append(1)
        return inner(hass)

    monkeypatch.setattr(snapshots, "ffmpeg_command", counting)
    return calls


async def test_thumbnail_mode_shows_only_the_thumbnail_and_never_opens_the_recording(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Fast thumbnail only: a pushed detection shows its thumbnail; no 1025, no decoder."""
    entry = await set_up_warm(hass, seed_warm_cache, options={CONF_CAMERA_IMAGE: "thumbnail"})
    manager = _manager(entry)
    entity_id = _camera_id(hass)
    sources = _record_sources(hass, entity_id)
    decodes = _count_decoder_calls(monkeypatch)

    fake_station.push_camera_event()
    await wait_until(lambda: sources == ["thumbnail"])
    await wait_until(lambda: not manager.busy)
    await asyncio.sleep(0.3)

    assert sources == ["thumbnail"]
    assert await _image(hass, entity_id) == THUMBNAIL
    assert _commands(fake_station, 1025) == 0, "thumbnail mode opened the recording"
    assert not decodes, "thumbnail mode ran the decoder"
    await _unload(hass, entry)


async def test_thumbnail_mode_retries_a_thumbnail_not_yet_written_once(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """There is no trigger frame to land, so a thumbnail not written yet gets its one retry."""
    entry = await set_up_warm(hass, seed_warm_cache, options={CONF_CAMERA_IMAGE: "thumbnail"})
    manager = _manager(entry)
    entity_id = _camera_id(hass)

    entry.runtime_data.router.handle(
        detection_event(
            DetectionType.PERSON,
            t_ms=now_ms(),
            unique_id="occ-1",
            record_id=RID,
            video_path=CLIP_PATH,
        )
    )
    await wait_until(lambda: len(fake_station.history_queries) == 1 and not manager.busy)
    await wait_until(lambda: manager._cameras[SYNTHETIC.camera_sn].cancel_retry is not None)
    assert _commands(fake_station, 1025) == 0
    _seed_row(fake_station)

    await _fire_retry_delay(hass, THUMBNAIL_RETRY_DELAY_SECONDS)
    await wait_until(lambda: manager.source_for(SYNTHETIC.camera_sn) == "thumbnail")
    await wait_until(lambda: not manager.busy)
    assert await _image(hass, entity_id) == ROW_THUMBNAIL
    assert len(fake_station.history_queries) == 2

    for _ in range(2):
        await _fire_retry_delay(hass, THUMBNAIL_RETRY_DELAY_SECONDS)
    await asyncio.sleep(0.3)
    assert len(fake_station.history_queries) == 2, "the thumbnail was retried more than once"
    assert _commands(fake_station, 1025) == 0
    await _unload(hass, entry)


async def test_hd_only_mode_shows_only_the_trigger_frame_and_never_looks_a_thumbnail_up(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """HD only: no still fetch, no history query, no retry; just the trigger frame."""
    entry = await set_up_warm(hass, seed_warm_cache, options={CONF_CAMERA_IMAGE: "hd_only"})
    manager = _manager(entry)
    entity_id = _camera_id(hass)
    sources = _record_sources(hass, entity_id)

    fake_station.push_camera_event()
    await wait_until(lambda: sources == ["trigger_frame"])
    await wait_until(lambda: not manager.busy)
    image = await _image(hass, entity_id)
    assert image is not None and image.startswith(FOUR_K_JPEG_PREFIX)
    assert _commands(fake_station, 1308) == 0
    assert fake_station.history_queries == []
    assert manager._cameras[SYNTHETIC.camera_sn].cancel_retry is None

    received = len(fake_station.received)
    await _fire_retry_delay(hass, THUMBNAIL_RETRY_DELAY_SECONDS)
    await asyncio.sleep(0.3)
    assert [
        obj.get("cmd")
        for obj in fake_station.received[received:]
        if obj.get("cmd") in (1308, 1025, 1306)
    ] == []
    assert sources == ["trigger_frame"]
    await _unload(hass, entry)


@pytest.mark.parametrize(
    ("mode", "fields"),
    [
        ("hd_only", {"thumb_path": THUMB_PATH, "record_id": RID}),
        ("thumbnail", {"video_path": CLIP_PATH}),
    ],
)
async def test_a_detection_the_mode_cannot_show_is_ignored_before_it_is_accepted(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    mode: str,
    fields: dict[str, Any],
) -> None:
    """No recording in HD only, nothing to look a thumbnail up by in thumbnail only: no fetch."""
    entry = await set_up_warm(hass, seed_warm_cache, options={CONF_CAMERA_IMAGE: mode})
    manager = _manager(entry)

    entry.runtime_data.router.handle(
        detection_event(DetectionType.PERSON, t_ms=now_ms(), unique_id="occ-1", **fields)
    )
    await asyncio.sleep(0.3)
    assert not manager.busy
    assert manager.image_for(SYNTHETIC.camera_sn) is None
    assert _commands(fake_station, 1308) == 0
    assert _commands(fake_station, 1025) == 0
    assert fake_station.history_queries == []
    await _unload(hass, entry)


async def test_a_homebase_detection_without_a_media_path_fetches_nothing(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Only a standalone camera is woken for its newest still; a HomeBase camera's
    detection with nothing to look up asks the station for nothing."""
    entry = await set_up_warm(hass, seed_warm_cache)
    manager = _manager(entry)
    entry.runtime_data.router.handle(
        detection_event(DetectionType.PERSON, t_ms=now_ms(), cipher=None, unique_id="occ-1")
    )
    await asyncio.sleep(0.3)
    assert not manager.busy
    assert manager.image_for(SYNTHETIC.camera_sn) is None
    assert fake_station.event_count_queries == 0
    assert fake_station.history_queries == []
    await _unload(hass, entry)


async def test_an_unknown_stored_camera_image_sets_up_as_hd(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A tampered or downgraded stored value never breaks setup: hd, the default."""
    fake_station.images[THUMB_PATH] = THUMBNAIL
    entry = await set_up_warm(hass, seed_warm_cache, options={CONF_CAMERA_IMAGE: "bogus"})
    entity_id = _camera_id(hass)
    sources = _record_sources(hass, entity_id)
    assert _manager(entry).camera_image == "hd"

    fake_station.push_camera_event()
    await wait_until(lambda: sources == ["thumbnail", "trigger_frame"])
    await wait_until(lambda: not _manager(entry).busy)
    await _unload(hass, entry)
