"""DEBUG trace of pushes and camera stills.

A live debug session must be able to follow each push and each camera still decision
from the integration's DEBUG log alone, and no line may carry an identifier: only
redacted serials, enum names, reasons, sizes and durations. Every test runs on the
library's ``FakeStation`` and ``tests/fake_ffmpeg.py``, like ``tests/test_camera.py``.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, Final

import pytest
from conftest import (
    PUSHED_THUMB_PATH,
    SYNTHETIC,
    detection_event,
    entity_id_for,
    now_ms,
    set_up_warm,
    wait_until,
)
from eufy_home_security import DetectionType, EufySecurity, FrameCipher, redact_serial
from eufy_home_security.testing import FakeStation
from homeassistant.components.camera import DOMAIN as CAMERA_DOMAIN
from homeassistant.components.camera import async_get_image
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.eufy_home_security import snapshots
from custom_components.eufy_home_security.const import (
    CAMERA_KEY,
    CONF_CAMERA_IMAGE,
    CONF_LIVE_SNAPSHOT,
    THUMBNAIL_RETRY_DELAY_SECONDS,
)
from custom_components.eufy_home_security.snapshots import SnapshotManager

# The start time of the synthetic camera's newest recording, for the Refresh image traces.
WANTED_START: Final = "2026-09-16 12:03:00"  # hygiene: ok (synthetic record data)
# A detection's event-database id: its day (YYYYMMDD) times 100000 plus a sequence.
RID: Final = 20260916 * 100_000 + 42
CLIP_PATH: Final = "/zx/clip.zxvideo"
# The thumbnail the station's history row for RID names, and the still it serves there.
ROW_THUMB_PATH: Final = "/zx/hdd_data0/Camera00/20260916/snapshort.jpg"
ROW_THUMBNAIL: Final = b"\xff\xd8ROW-THUMBNAIL\xff\xd9"
# A camera serial the fake station does not pair: synthetic, like test_camera's.
UNPAIRED_SN: Final = "T8160P2000000002"
_OURS: Final = "custom_components.eufy_home_security"


def _manager(entry: MockConfigEntry) -> SnapshotManager:
    manager: SnapshotManager = entry.runtime_data.snapshots
    return manager


def _commands(station: FakeStation, cmd: int) -> int:
    return sum(1 for obj in station.received if obj.get("cmd") == cmd)


def _ours(caplog: pytest.LogCaptureFixture) -> list[str]:
    """The text of every record the integration's own loggers wrote."""
    return [r.getMessage() for r in caplog.records if r.name.startswith(_OURS)]


def _no_errors(caplog: pytest.LogCaptureFixture) -> None:
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def _count(messages: list[str], *needles: str) -> int:
    return sum(1 for m in messages if all(needle in m for needle in needles))


async def _fire_retry_delay(hass: HomeAssistant, seconds: float) -> None:
    """Move Home Assistant's clock past the thumbnail retry's delay and let it run."""
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds + 1))
    await hass.async_block_till_done()


def _seed_row(fake_station: FakeStation) -> None:
    """Write the station's history row for RID and the thumbnail it names."""
    fake_station.rows = [
        {"record_id": RID, "device_sn": SYNTHETIC.camera_sn, "thumb_path": ROW_THUMB_PATH}
    ]
    fake_station.images[ROW_THUMB_PATH] = ROW_THUMBNAIL


async def _image(hass: HomeAssistant, entity_id: str) -> bytes | None:
    """What the camera serves to a view, None when it has no image."""
    try:
        return (await async_get_image(hass, entity_id)).content
    except HomeAssistantError:
        return None


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
    await wait_until(lambda: manager._cameras[SYNTHETIC.camera_sn].cancel_retry is not None)


def _assert_in_order(messages: list[str], needles: list[str]) -> None:
    """Each needle appears after the previous needle's match, on its line or a later one."""
    text = "\n".join(messages)
    position = 0
    for needle in needles:
        found = text.find(needle, position)
        assert found >= 0, f"{needle!r} not found in order in:\n{text}"
        position = found + len(needle)


def _assert_no_identifiers(messages: list[str], *extra: str) -> None:
    """No message carries a full serial, the DID, the account id, a path or an event repr."""
    forbidden = (
        SYNTHETIC.station_sn,
        SYNTHETIC.camera_sn,
        SYNTHETIC.did,
        SYNTHETIC.account_id,
        "/zx/",
        "SecurityEvent(",
        *extra,
    )
    for message in messages:
        for token in forbidden:
            assert token not in message, f"{token!r} logged in {message!r}"
    redacted = redact_serial(SYNTHETIC.camera_sn)
    assert any(redacted in message for message in messages), "no redacted camera serial logged"


def _delay_media_start(monkeypatch: pytest.MonkeyPatch, fake_station: FakeStation) -> None:
    """The fake station starts a stream it was asked for only half a second later."""
    original = fake_station._start_media

    def delayed(key_hex: str, **kwargs: Any) -> None:
        asyncio.get_running_loop().call_later(0.5, functools.partial(original, key_hex, **kwargs))

    monkeypatch.setattr(fake_station, "_start_media", delayed)


async def _unload(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_walk_past_is_traced_from_push_to_image(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A GCM detection whose row is not written yet: every decision up to the trigger frame."""
    caplog.set_level(logging.DEBUG)
    entry = await set_up_warm(hass, seed_warm_cache)
    manager = _manager(entry)

    entry.runtime_data.router.handle(
        detection_event(
            DetectionType.PERSON,
            t_ms=now_ms(),
            unique_id="occ-walk",
            record_id=RID,
            video_path=CLIP_PATH,
        )
    )
    await wait_until(lambda: manager.source_for(SYNTHETIC.camera_sn) == "trigger_frame")
    await wait_until(lambda: not manager.busy)

    messages = _ours(caplog)
    receipt = next(m for m in messages if "Push received for" in m)
    assert "gcm" in receipt and "PERSON" in receipt
    _assert_in_order(
        messages,
        [
            "Push received for",
            "Snapshot requested for",
            "accepted",
            "dispatched to its device entities",
        ],
    )
    # The worker starts eagerly, so its lines may precede the device dispatch.
    _assert_in_order(
        messages,
        [
            "accepted",
            "Worker for ",
            "Thumbnail of ",
            ": looking up",
            "Thumbnail of ",
            "not in the station's history yet",
            "Trigger frame of ",
            ": fetching",
            "decode started",
            "Decode for ",
            " ok",
            "now shows its trigger_frame",
        ],
    )
    worker = next(m for m in messages if "Worker for " in m)
    assert "event" in worker
    missing = next(m for m in messages if "not in the station's history yet" in m)
    assert " s" in missing
    assert _count(messages, "Thumbnail of ", ": looking up") == 1
    assert _count(messages, "Trigger frame of ", ": fetching") == 1
    assert _count(messages, "Decode for ", " ok") == 1

    # An enriching copy of the same detection: routed, requested, dropped, nothing fetched.
    caplog.clear()
    entry.runtime_data.router.handle(
        detection_event(
            DetectionType.PERSON,
            t_ms=manager.event_time_for(SYNTHETIC.camera_sn),
            unique_id="occ-walk",
            record_id=RID,
            video_path=CLIP_PATH,
            enriches=True,
        )
    )
    await wait_until(lambda: not manager.busy)
    await wait_until(lambda: _count(_ours(caplog), "Trigger frame of ", "already shown") == 1)
    messages = _ours(caplog)
    _assert_in_order(
        messages,
        [
            "enriches True",
            "Snapshot requested for",
            "accepted",
            "dropped: enriching copy of a delivered detection",
        ],
    )
    _assert_in_order(
        messages,
        [
            "accepted",
            "Thumbnail of ",
            "skipped, its trigger frame is already shown",
            "Trigger frame of ",
            "skipped, already shown",
        ],
    )
    assert _count(messages, "Trigger frame of ", ": skipped, already shown") == 1
    _assert_no_identifiers(messages, str(RID), "occ-walk", CLIP_PATH)
    _no_errors(caplog)
    await _unload(hass, entry)


async def test_every_routing_decision_names_its_reason(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Not a still to find, not paired, outside the consuming window: each on its own line."""
    caplog.set_level(logging.DEBUG)
    entry = await set_up_warm(hass, seed_warm_cache)
    router = entry.runtime_data.router

    router.handle(detection_event(DetectionType.CRYING, t_ms=now_ms(), unique_id="occ-cry"))
    router.handle(
        detection_event(
            DetectionType.PERSON,
            t_ms=now_ms(),
            device_sn=UNPAIRED_SN,
            unique_id="occ-unpaired",
            record_id=RID,
        )
    )
    await hass.async_block_till_done()
    messages = _ours(caplog)
    _assert_in_order(
        messages,
        [
            "No snapshot for ",
            "not a catalogued detection with a still to find",
            "fired as the fallback bus event: no entity consumes it",
            "No snapshot for ",
            "not paired to the delivering station",
            "fired as the fallback bus event: device not paired to the delivering station",
        ],
    )
    assert _count(messages, "Snapshot requested for") == 0

    caplog.clear()
    router.async_stop_consuming()
    router.handle(
        detection_event(DetectionType.PERSON, t_ms=now_ms(), unique_id="occ-late", record_id=RID)
    )
    messages = _ours(caplog)
    _assert_in_order(
        messages,
        ["Push received for", "fired as the fallback bus event: outside the consuming window"],
    )
    assert _count(messages, "Snapshot requested for") == 0
    _assert_no_identifiers(messages, str(RID), "occ-cry", "occ-unpaired", "occ-late", UNPAIRED_SN)
    _no_errors(caplog)
    router.async_start_consuming()
    await _unload(hass, entry)


async def test_a_thumbnail_retry_and_an_ignored_forged_push_are_traced(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A thumbnail retry scheduled, due, retried and shown; an ECB push ignored with its reason."""
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("FAKE_FFMPEG_FAIL", "1")
    entry = await set_up_warm(hass, seed_warm_cache)
    manager = _manager(entry)

    await _miss_without_trigger_frame(entry, fake_station)
    await asyncio.sleep(0.05)
    entry.runtime_data.router.handle(
        detection_event(
            DetectionType.PERSON,
            t_ms=now_ms(),
            cipher=FrameCipher.ECB,
            unique_id="occ-forged",
            record_id=RID + 1,
        )
    )
    await wait_until(lambda: not manager.busy)
    _seed_row(fake_station)
    await _fire_retry_delay(hass, THUMBNAIL_RETRY_DELAY_SECONDS)
    await wait_until(lambda: manager.source_for(SYNTHETIC.camera_sn) == "thumbnail")
    await wait_until(lambda: not manager.busy)

    messages = _ours(caplog)
    _assert_in_order(
        messages,
        [
            "not in the station's history yet",
            "Decode for ",
            "keeping the current image",
            "Thumbnail retry for ",
            "scheduled in 60 s",
            "ignored: unauthenticated push while a genuine detection is owed its thumbnail retry",
            "due: queued",
            "thumbnail_retry",
            "Retried thumbnail of ",
            ": looking up",
            "shown",
            "now shows its thumbnail",
        ],
    )
    assert _count(messages, "Decode for ", "failed") == 1
    _assert_no_identifiers(messages, str(RID), str(RID + 1), "occ-1", "occ-forged", CLIP_PATH)
    _no_errors(caplog)
    await _unload(hass, entry)


@pytest.mark.parametrize("cancel_by", ["newer_detection", "unload"])
async def test_a_cancelled_thumbnail_retry_and_the_stop_are_traced(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    cancel_by: str,
) -> None:
    """A newer detection or the unload cancels the owed retry, each with its reason."""
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("FAKE_FFMPEG_FAIL", "1")
    entry = await set_up_warm(hass, seed_warm_cache)
    manager = _manager(entry)
    await _miss_without_trigger_frame(entry, fake_station)

    if cancel_by == "newer_detection":
        entry.runtime_data.router.handle(
            detection_event(
                DetectionType.MOTION,
                t_ms=now_ms(),
                unique_id="occ-2",
                thumb_path=PUSHED_THUMB_PATH,
            )
        )
        await wait_until(lambda: manager.source_for(SYNTHETIC.camera_sn) == "thumbnail")
        await wait_until(lambda: not manager.busy)
        await _unload(hass, entry)
        needles = [
            "Thumbnail retry for ",
            "cancelled: newer detection",
            "Snapshot manager stopping: ",
        ]
    else:
        await _unload(hass, entry)
        needles = ["Snapshot manager stopping: ", "Thumbnail retry for ", "cancelled: unload"]

    messages = _ours(caplog)
    _assert_in_order(messages, needles)
    assert _count(messages, "Snapshot manager stopping: ") == 1
    assert _count(messages, "Thumbnail retry for ", "cancelled: ") == 1
    _assert_no_identifiers(messages, str(RID), "occ-1", "occ-2", CLIP_PATH)
    _no_errors(caplog)


async def test_a_live_keyframe_skip_is_logged_once_per_reason(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """With the option off, views of a camera with no image add one line, not one per view."""
    caplog.set_level(logging.DEBUG)
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = entity_id_for(hass, CAMERA_DOMAIN, SYNTHETIC.camera_sn, CAMERA_KEY)
    for _ in range(3):
        assert await _image(hass, entity_id) is None
    messages = _ours(caplog)
    assert _count(messages, "Live keyframe for ", "option off") == 1
    assert _commands(fake_station, 1003) == 0
    _assert_no_identifiers(messages)
    _no_errors(caplog)
    await _unload(hass, entry)


async def test_a_live_keyframe_request_and_its_cooldown_are_traced(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """With the option on: requested, fetched, decode failed, then one cooldown line."""
    caplog.set_level(logging.DEBUG)
    clock = time.monotonic()
    monkeypatch.setattr(snapshots, "_monotonic", lambda: clock)
    monkeypatch.setenv("FAKE_FFMPEG_FAIL", "1")
    entry = await set_up_warm(hass, seed_warm_cache, options={CONF_LIVE_SNAPSHOT: True})
    entity_id = entity_id_for(hass, CAMERA_DOMAIN, SYNTHETIC.camera_sn, CAMERA_KEY)
    manager = _manager(entry)

    assert await _image(hass, entity_id) is None
    await wait_until(lambda: _commands(fake_station, 1003) == 1)
    await wait_until(lambda: not manager.busy)
    for _ in range(3):
        assert await _image(hass, entity_id) is None

    messages = _ours(caplog)
    _assert_in_order(
        messages,
        [
            "Live keyframe for ",
            " requested",
            "Live keyframe of ",
            ": fetching",
            "decode started",
            "Decode for ",
            "failed",
        ],
    )
    assert _count(messages, "Live keyframe for ", "cooldown") == 1
    _assert_no_identifiers(messages)
    _no_errors(caplog)
    await _unload(hass, entry)


async def test_a_capture_press_its_coalesce_and_outcome_are_traced(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Queued, coalesced, fetched, decoded, shown; ignored after the stop."""
    caplog.set_level(logging.DEBUG)
    entry = await set_up_warm(hass, seed_warm_cache)
    manager = _manager(entry)
    station = entry.runtime_data.coordinators[SYNTHETIC.station_sn].station
    _delay_media_start(monkeypatch, fake_station)

    for _ in range(2):
        manager.async_request_capture(station, SYNTHETIC.camera_sn)
    await wait_until(lambda: manager.source_for(SYNTHETIC.camera_sn) == "live")
    await wait_until(lambda: not manager.busy)

    messages = _ours(caplog)
    # The worker starts eagerly, so its first lines precede the second press.
    _assert_in_order(
        messages,
        [
            "Capture live image for ",
            "pressed, queued",
            "capture_live job",
            "Live capture of ",
            ": fetching",
            "Capture live image for ",
            "pressed, coalesced into the queued or running capture",
            "Live capture of ",
            "decode started",
            "Decode for ",
            " ok",
            "now shows its live",
        ],
    )
    assert _count(messages, "Live capture of ", ": fetching") == 1

    await _unload(hass, entry)
    manager.async_request_capture(station, SYNTHETIC.camera_sn)
    messages = _ours(caplog)
    assert _count(messages, "Capture live image for ", "ignored: snapshot manager stopped") == 1
    _assert_no_identifiers(messages)
    _no_errors(caplog)


@pytest.mark.parametrize(
    ("mode", "skip", "reason"),
    [
        (
            "hd_only",
            "Thumbnail of ",
            "ignored: no recording, camera image is HD only",
        ),
        (
            "thumbnail",
            "Trigger frame of ",
            "ignored: no thumbnail to look up, camera image is thumbnail only",
        ),
    ],
)
async def test_the_camera_image_mode_decisions_are_traced(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    mode: str,
    skip: str,
    reason: str,
) -> None:
    """The mode at start, the tier it skips, the detection it cannot show."""
    caplog.set_level(logging.DEBUG)
    entry = await set_up_warm(hass, seed_warm_cache, options={CONF_CAMERA_IMAGE: mode})
    manager = _manager(entry)

    fake_station.push_camera_event()
    await wait_until(lambda: manager.image_for(SYNTHETIC.camera_sn) is not None)
    await wait_until(lambda: not manager.busy)
    fields = {"thumb_path": PUSHED_THUMB_PATH} if mode == "hd_only" else {"video_path": CLIP_PATH}
    entry.runtime_data.router.handle(
        detection_event(DetectionType.PERSON, t_ms=now_ms(), unique_id="occ-cannot", **fields)
    )
    await hass.async_block_till_done()

    messages = _ours(caplog)
    assert _count(messages, "Snapshot manager started: camera image " + mode) == 1
    skipped = "HD only" if mode == "hd_only" else "thumbnail only"
    assert _count(messages, skip, "skipped, camera image is " + skipped) == 1
    assert _count(messages, "Snapshot request for ", reason) == 1
    _assert_no_identifiers(messages, "occ-cannot", CLIP_PATH)
    _no_errors(caplog)
    await _unload(hass, entry)


def _seed_wanted_recording(fake_station: FakeStation) -> int:
    """Today's newest recorded event of the synthetic camera; returns its record id."""
    record_id = int(datetime.now().astimezone().date().strftime("%Y%m%d")) * 100_000 + 43
    fake_station.rows = [
        {
            "record_id": record_id,
            "device_sn": SYNTHETIC.camera_sn,
            "storage_path": "/zx/wanted.zxvideo",
            "thumb_path": "/zx/wanted.jpg",
            "start_time": WANTED_START,
        }
    ]
    return record_id


async def test_a_refresh_press_its_coalesce_and_outcome_are_traced(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Queued with its source, coalesced, fetched, decoded, shown."""
    caplog.set_level(logging.DEBUG)
    record_id = _seed_wanted_recording(fake_station)
    entry = await set_up_warm(hass, seed_warm_cache)
    manager = _manager(entry)
    station = entry.runtime_data.coordinators[SYNTHETIC.station_sn].station
    _delay_media_start(monkeypatch, fake_station)

    for _ in range(2):
        manager.async_request_refresh(station, SYNTHETIC.camera_sn)
    await wait_until(lambda: manager.source_for(SYNTHETIC.camera_sn) == "trigger_frame")
    await wait_until(lambda: not manager.busy)

    messages = _ours(caplog)
    # The worker starts eagerly, so its first lines precede the second press.
    _assert_in_order(
        messages,
        [
            "Refresh image for ",
            "pressed (trigger_frame), queued",
            "refresh job",
            "Refresh of ",
            ": looking up the newest trigger_frame",
            "Refresh image for ",
            "pressed, coalesced into the queued or running refresh",
            "Refresh of ",
            "decode started",
            "Decode for ",
            " ok",
            "now shows its trigger_frame",
        ],
    )
    assert _count(messages, "Refresh of ", "looking up") == 1

    await _unload(hass, entry)
    manager.async_request_refresh(station, SYNTHETIC.camera_sn)
    messages = _ours(caplog)
    assert _count(messages, "Refresh image for ", "ignored: snapshot manager stopped") == 1
    _assert_no_identifiers(messages, str(record_id), "12:03:00", "wanted")
    _no_errors(caplog)


async def test_a_refresh_with_no_recording_keeps_the_image_and_says_so(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """An empty history: one DEBUG line saying nothing was found and the image stays."""
    caplog.set_level(logging.DEBUG)
    entry = await set_up_warm(hass, seed_warm_cache)
    manager = _manager(entry)
    station = entry.runtime_data.coordinators[SYNTHETIC.station_sn].station

    manager.async_request_refresh(station, SYNTHETIC.camera_sn)
    await wait_until(lambda: not manager.busy)

    messages = _ours(caplog)
    assert (
        _count(
            messages,
            "Refresh of ",
            "no trigger_frame in the station's history, keeping the image, in ",
        )
        == 1
    )
    _assert_no_identifiers(messages)
    _no_errors(caplog)
    await _unload(hass, entry)
