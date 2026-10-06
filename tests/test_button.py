"""Camera buttons: an image on demand, without waiting for a detection.

End to end on the library's loopback ``FakeStation``, like ``tests/test_camera.py``: a
press goes through Home Assistant's ``button.press`` service to the snapshot manager,
the station's media worker and ``Station.async_camera_image``, and the HEVC decode runs
``tests/fake_ffmpeg.py`` (conftest's autouse ``_fake_ffmpeg``). A press only queues
work: presses of one kind coalesce while that camera's job is queued or running, there
is no cooldown, and presses are ordered against detections by arrival.
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
    PUSHED_THUMBNAIL,
    SYNTHETIC,
    add_entry,
    detection_event,
    entity_id_for,
    now_ms,
    panel_entity_id,
    record_states,
    set_up_warm,
    setup_entry,
    state_of,
    wait_until,
)
from eufy_home_security import DetectionType, EufySecurity, Station, entity_unique_id
from eufy_home_security.testing import FakeCloud, FakeStation, camera_device
from homeassistant.components.alarm_control_panel import DOMAIN as ALARM_DOMAIN
from homeassistant.components.alarm_control_panel import SERVICE_ALARM_ARM_HOME
from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN
from homeassistant.components.button import SERVICE_PRESS
from homeassistant.components.camera import DOMAIN as CAMERA_DOMAIN
from homeassistant.components.camera import async_get_image
from homeassistant.const import ATTR_ENTITY_ID, EVENT_STATE_CHANGED, STATE_UNAVAILABLE
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.eufy_home_security import snapshots
from custom_components.eufy_home_security.const import (
    ATTR_IMAGE_SOURCE,
    ATTR_IMAGE_UPDATED,
    ATTR_TRIGGERED_AT,
    CAMERA_KEY,
    CAPTURE_LIVE_IMAGE_KEY,
    CONF_CAMERA_IMAGE,
    CONNECTION_LOSS_GRACE_SECONDS,
    DOMAIN,
    REFRESH_IMAGE_KEY,
    THUMBNAIL_RETRY_DELAY_SECONDS,
)
from custom_components.eufy_home_security.snapshots import SnapshotManager

# What fake_ffmpeg.py answers for a keyframe that decrypted: a JPEG declaring 3840x2160.
FOUR_K_JPEG_PREFIX: Final = b"\xff\xd8\xff\xc0\x00\x11\x08\x08\x70\x0f\x00"
# A detection's event-database id: its day (YYYYMMDD) times 100000 plus a sequence.
RID: Final = 20260916 * 100_000 + 42
CLIP_PATH: Final = "/zx/clip.zxvideo"
# The thumbnail the station's history row for RID names, and the still it serves there.
ROW_THUMB_PATH: Final = "/zx/hdd_data0/Camera00/20260916/snapshort.jpg"
ROW_THUMBNAIL: Final = b"\xff\xd8ROW-THUMBNAIL\xff\xd9"
# A second camera on the fake station, on channel 1: synthetic, like test_camera's.
OTHER_CAMERA_SN: Final = "T8160P2000000002"
# The newest recorded event of the synthetic camera the Refresh image tests seed, as the
# library's own async_camera_image tests shape it (tests/test_station.py).
WANTED_CLIP: Final = "/zx/wanted.zxvideo"
WANTED_THUMB: Final = "/zx/wanted.jpg"
WANTED_THUMBNAIL: Final = b"\xff\xd8WANTED-THUMBNAIL\xff\xd9"
WANTED_START: Final = "2026-09-16 12:03:00"
# How long the fake station holds back a stream it was asked to open.
MEDIA_START_DELAY: Final = 0.5


def _manager(entry: MockConfigEntry) -> SnapshotManager:
    manager: SnapshotManager = entry.runtime_data.snapshots
    return manager


def _station(entry: MockConfigEntry) -> Station:
    station: Station = entry.runtime_data.coordinators[SYNTHETIC.station_sn].station
    return station


def _camera_id(hass: HomeAssistant) -> str:
    return entity_id_for(hass, CAMERA_DOMAIN, SYNTHETIC.camera_sn, CAMERA_KEY)


def _button_id(hass: HomeAssistant, key: str) -> str:
    return entity_id_for(hass, BUTTON_DOMAIN, SYNTHETIC.camera_sn, key)


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


def _record_attribute(hass: HomeAssistant, entity_id: str, name: str) -> list[str]:
    """Every value of attribute ``name`` the entity is written with from now on, in order."""
    values: list[str] = []

    @callback
    def _on_change(event: Event[Any]) -> None:
        new = event.data["new_state"]
        if event.data[ATTR_ENTITY_ID] == entity_id and new is not None and name in new.attributes:
            values.append(new.attributes[name])

    hass.bus.async_listen(EVENT_STATE_CHANGED, _on_change)
    return values


async def _image(hass: HomeAssistant, entity_id: str) -> bytes | None:
    """What the camera serves to a view, None when it has no image."""
    try:
        return (await async_get_image(hass, entity_id)).content
    except HomeAssistantError:
        return None


async def _unload(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def _press(hass: HomeAssistant, entity_id: str) -> float:
    """Press a button as an automation does; returns how long the service call took."""
    started = time.monotonic()
    await hass.services.async_call(
        BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: entity_id}, blocking=True
    )
    return time.monotonic() - started


def _delay_media_start(
    monkeypatch: pytest.MonkeyPatch, fake_station: FakeStation, seconds: float = MEDIA_START_DELAY
) -> None:
    """The fake station starts a stream it was asked for only after ``seconds``."""
    original = fake_station._start_media

    def delayed(key_hex: str, **kwargs: Any) -> None:
        asyncio.get_running_loop().call_later(
            seconds, functools.partial(original, key_hex, **kwargs)
        )

    monkeypatch.setattr(fake_station, "_start_media", delayed)


async def _fire_retry_delay(hass: HomeAssistant, seconds: float) -> None:
    """Move Home Assistant's clock past the thumbnail retry's delay and let it run."""
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds + 1))
    await hass.async_block_till_done()


def _seed_row(fake_station: FakeStation) -> None:
    """The station's history row for RID, and its thumbnail."""
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
    await wait_until(lambda: manager._cameras[SYNTHETIC.camera_sn].cancel_retry is not None)


# ── Capture live image ─────────────────────────────────────────────────────────


async def test_a_capture_press_shows_a_live_image_with_the_option_off_and_has_no_cooldown(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """One live open per press, the live option off; a second press right after works too."""
    entry = await set_up_warm(hass, seed_warm_cache)
    manager = _manager(entry)
    camera_id = _camera_id(hass)
    button_id = _button_id(hass, CAPTURE_LIVE_IMAGE_KEY)
    assert not manager.live_snapshot

    await _press(hass, button_id)
    await wait_until(lambda: manager.source_for(SYNTHETIC.camera_sn) == "live")
    await wait_until(lambda: not manager.busy)
    image = await _image(hass, camera_id)
    assert image is not None and image.startswith(FOUR_K_JPEG_PREFIX)
    state = hass.states.get(camera_id)
    assert state is not None
    assert state.attributes[ATTR_IMAGE_SOURCE] == "live"
    assert ATTR_TRIGGERED_AT not in state.attributes
    assert _commands(fake_station, 1003) == 1

    # No cooldown: the next press after the job ended opens the camera again at once.
    await _press(hass, button_id)
    await wait_until(lambda: _commands(fake_station, 1003) == 2)
    await wait_until(lambda: not manager.busy)
    assert manager.source_for(SYNTHETIC.camera_sn) == "live"
    await _unload(hass, entry)


async def test_capture_presses_while_one_is_running_coalesce_and_never_wait(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Three presses during a slow live open: one open, and each press returns at once."""
    entry = await set_up_warm(hass, seed_warm_cache)
    manager = _manager(entry)
    button_id = _button_id(hass, CAPTURE_LIVE_IMAGE_KEY)
    _delay_media_start(monkeypatch, fake_station)

    for _ in range(3):
        elapsed = await _press(hass, button_id)
        assert elapsed < 1.0, f"a press waited {elapsed:.1f} s on the station"
    assert manager.busy, "the capture had already ended: the test proved nothing"
    await wait_until(lambda: manager.source_for(SYNTHETIC.camera_sn) == "live")
    await wait_until(lambda: not manager.busy)
    assert _commands(fake_station, 1003) == 1
    await _unload(hass, entry)


async def test_a_detection_accepted_after_a_press_replaces_the_pressed_image(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Press, then a detection while the capture is in flight: live, then the thumbnail."""
    entry = await set_up_warm(hass, seed_warm_cache)
    manager = _manager(entry)
    camera_id = _camera_id(hass)
    sources = _record_sources(hass, camera_id)
    _delay_media_start(monkeypatch, fake_station)

    await _press(hass, _button_id(hass, CAPTURE_LIVE_IMAGE_KEY))
    await wait_until(lambda: _commands(fake_station, 1003) == 1)
    entry.runtime_data.router.handle(
        detection_event(DetectionType.MOTION, t_ms=now_ms(), thumb_path=PUSHED_THUMB_PATH)
    )
    await wait_until(lambda: sources == ["live", "thumbnail"])
    await wait_until(lambda: not manager.busy)
    assert sources == ["live", "thumbnail"]
    assert await _image(hass, camera_id) == PUSHED_THUMBNAIL
    await _unload(hass, entry)


async def test_an_older_detections_owed_retry_never_replaces_a_pressed_image(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A detection owed its thumbnail retry, then a press: the retry fetches and shows nothing."""
    monkeypatch.setenv("FAKE_FFMPEG_FAIL", "1")
    entry = await set_up_warm(hass, seed_warm_cache)
    manager = _manager(entry)
    camera_id = _camera_id(hass)
    await _miss_without_trigger_frame(entry, fake_station)
    monkeypatch.delenv("FAKE_FFMPEG_FAIL")

    await _press(hass, _button_id(hass, CAPTURE_LIVE_IMAGE_KEY))
    await wait_until(lambda: manager.source_for(SYNTHETIC.camera_sn) == "live")
    await wait_until(lambda: not manager.busy)
    pressed = await _image(hass, camera_id)
    assert pressed is not None
    _seed_row(fake_station)

    await _fire_retry_delay(hass, THUMBNAIL_RETRY_DELAY_SECONDS)
    await asyncio.sleep(0.3)
    await wait_until(lambda: not manager.busy)
    assert manager.source_for(SYNTHETIC.camera_sn) == "live"
    assert await _image(hass, camera_id) == pressed
    assert len(fake_station.history_queries) == 1, "the retry looked the thumbnail up"
    await _unload(hass, entry)


async def test_the_capture_button_follows_the_session_and_a_press_after_unload_sends_nothing(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Unavailable while the station's session is down; the manager ignores a later request."""
    entry = await set_up_warm(hass, seed_warm_cache)
    manager = _manager(entry)
    station = _station(entry)
    button_id = _button_id(hass, CAPTURE_LIVE_IMAGE_KEY)
    button_states = record_states(hass, button_id)

    fake_station.send_close()
    fake_station.stop()
    await wait_until(lambda: not station.connected, timeout=10)
    await hass.async_block_till_done()
    assert STATE_UNAVAILABLE not in button_states
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=CONNECTION_LOSS_GRACE_SECONDS + 1)
    )
    await wait_until(lambda: STATE_UNAVAILABLE in button_states, timeout=10)

    await _unload(hass, entry)
    opens = _commands(fake_station, 1003)
    manager.async_request_capture(station, SYNTHETIC.camera_sn)
    await asyncio.sleep(0.2)
    assert not manager.busy
    assert _commands(fake_station, 1003) == opens


# ── Refresh image ──────────────────────────────────────────────────────────────


def _today_record_id(n: int) -> int:
    """A history record id of today, the day the library's newest-record walk starts on."""
    return int(datetime.now().astimezone().date().strftime("%Y%m%d")) * 100_000 + n


WANTED_RID_N: Final = 43


def _seed_recordings(fake_station: FakeStation, *, with_recording: bool = True) -> int:
    """A newer row of an unpaired camera, then the synthetic camera's newest event."""
    wanted: dict[str, Any] = {
        "record_id": _today_record_id(WANTED_RID_N),
        "device_sn": SYNTHETIC.camera_sn,
        "thumb_path": WANTED_THUMB,
        "start_time": WANTED_START,
    }
    if with_recording:
        wanted["storage_path"] = WANTED_CLIP
    fake_station.rows = [
        {
            "record_id": _today_record_id(45),
            "device_sn": "T8160P2000099999",
            "storage_path": "/zx/other.zxvideo",
            "thumb_path": "/zx/other.jpg",
            "start_time": "2026-09-16 12:05:00",
        },
        wanted,
    ]
    fake_station.images[WANTED_THUMB] = WANTED_THUMBNAIL
    fake_station.images["/zx/other.jpg"] = b"\xff\xd8OTHER\xff\xd9"
    return int(wanted["record_id"])


def _count_decoder_calls(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Count every decoder command asked for from now on; the decode itself still runs."""
    calls: list[int] = []
    inner = snapshots.ffmpeg_command

    def counting(hass: HomeAssistant) -> list[str]:
        calls.append(1)
        return inner(hass)

    monkeypatch.setattr(snapshots, "ffmpeg_command", counting)
    return calls


def _errors(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno >= logging.ERROR]


async def test_each_camera_gets_both_buttons_and_the_station_none(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Two buttons per camera-entity device: capture_live_image and refresh_image."""
    fake_cloud.devices.append(
        camera_device(OTHER_CAMERA_SN, station_sn=fake_station.serial, channel=1, name="Back")
    )
    fake_station.params[1] = {1101: "80", 1142: "-60"}
    entry = await set_up_warm(hass, seed_warm_cache)

    # Camera buttons only: the account's Refresh device list button is not one.
    buttons = [
        entity
        for entity in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if entity.domain == BUTTON_DOMAIN
        and (entity.unique_id or "").endswith((CAPTURE_LIVE_IMAGE_KEY, REFRESH_IMAGE_KEY))
    ]
    assert len(buttons) == 4
    for serial in (SYNTHETIC.camera_sn, OTHER_CAMERA_SN):
        for key in (CAPTURE_LIVE_IMAGE_KEY, REFRESH_IMAGE_KEY):
            entity_id_for(hass, BUTTON_DOMAIN, serial, key)
    assert all(SYNTHETIC.station_sn not in (b.unique_id or "") for b in buttons)
    await _unload(hass, entry)


async def test_a_new_install_names_the_buttons_event_image_and_live_image(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The no-wake button says "event", the waking one says "live".

    The translation keys are ``refresh_image`` and ``capture_live_image``. Home Assistant
    builds a new install's entity_id from the English name, so a fresh refresh button ends in
    ``_refresh_event_image``.
    """
    entry = await set_up_warm(hass, seed_warm_cache)
    registry = er.async_get(hass)

    refresh_id = _button_id(hass, REFRESH_IMAGE_KEY)
    capture_id = _button_id(hass, CAPTURE_LIVE_IMAGE_KEY)
    refresh_state = hass.states.get(refresh_id)
    capture_state = hass.states.get(capture_id)
    assert refresh_state is not None
    assert capture_state is not None
    assert refresh_state.attributes["friendly_name"].endswith(" Refresh event image")
    assert capture_state.attributes["friendly_name"].endswith(" Capture live image")
    refresh_entry = registry.async_get(refresh_id)
    capture_entry = registry.async_get(capture_id)
    assert refresh_entry is not None
    assert capture_entry is not None
    assert refresh_entry.translation_key == REFRESH_IMAGE_KEY
    assert capture_entry.translation_key == CAPTURE_LIVE_IMAGE_KEY
    assert refresh_id.endswith("_refresh_event_image")
    assert capture_id.endswith("_capture_live_image")
    await _unload(hass, entry)


async def test_a_registered_refresh_button_keeps_its_entity_id(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A button registered as "Refresh image" keeps its entity_id.

    The registry finds the entry by unique id and renames an entity_id only when asked
    to, so automations on ``button.garden_cam_refresh_image`` keep working; only the
    name follows the translation.
    """
    seed_warm_cache()
    entry = add_entry(hass)
    registry = er.async_get(hass)
    old = registry.async_get_or_create(
        BUTTON_DOMAIN,
        DOMAIN,
        entity_unique_id(SYNTHETIC.camera_sn, REFRESH_IMAGE_KEY),
        config_entry=entry,
        suggested_object_id="garden_cam_refresh_image",
        original_name="Refresh image",
        translation_key=REFRESH_IMAGE_KEY,
        has_entity_name=True,
    )
    assert old.entity_id == "button.garden_cam_refresh_image"
    assert await setup_entry(hass, entry)

    entity_id = entity_id_for(hass, BUTTON_DOMAIN, SYNTHETIC.camera_sn, REFRESH_IMAGE_KEY)
    assert entity_id == "button.garden_cam_refresh_image"
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.attributes["friendly_name"].endswith(" Refresh event image")
    registered = registry.async_get(entity_id)
    assert registered is not None
    assert registered.original_name == "Refresh event image"
    await _unload(hass, entry)


async def test_a_refresh_in_hd_shows_the_newest_recordings_trigger_frame_and_its_time(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """HD: the newest recording of this camera, played, never a live open; its own time."""
    record_id = _seed_recordings(fake_station)
    entry = await set_up_warm(hass, seed_warm_cache)
    manager = _manager(entry)
    camera_id = _camera_id(hass)

    await _press(hass, _button_id(hass, REFRESH_IMAGE_KEY))
    await wait_until(lambda: manager.source_for(SYNTHETIC.camera_sn) == "trigger_frame")
    await wait_until(lambda: not manager.busy)

    image = await _image(hass, camera_id)
    assert image is not None and image.startswith(FOUR_K_JPEG_PREFIX)
    assert _commands(fake_station, 1025) == 1
    assert _commands(fake_station, 1003) == 0, "a refresh woke the camera"
    assert _commands(fake_station, 1308) == 0
    state = hass.states.get(camera_id)
    assert state is not None
    assert state.attributes[ATTR_IMAGE_SOURCE] == "trigger_frame"
    expected = dt_util.as_utc(
        datetime(2026, 9, 16, 12, 3, tzinfo=dt_util.get_default_time_zone())
    ).isoformat(timespec="milliseconds")
    assert state.attributes[ATTR_TRIGGERED_AT] == expected
    for value in state.attributes.values():
        assert "/zx/" not in str(value)
        assert str(record_id) not in str(value)
    await _unload(hass, entry)


async def test_a_refresh_in_thumbnail_mode_shows_the_newest_thumbnail_without_decoding(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Fast thumbnail only: the newest thumbnail as it is; no playback, no live open, no decode."""
    _seed_recordings(fake_station)
    entry = await set_up_warm(hass, seed_warm_cache, options={CONF_CAMERA_IMAGE: "thumbnail"})
    manager = _manager(entry)
    decodes = _count_decoder_calls(monkeypatch)

    await _press(hass, _button_id(hass, REFRESH_IMAGE_KEY))
    await wait_until(lambda: manager.source_for(SYNTHETIC.camera_sn) == "thumbnail")
    await wait_until(lambda: not manager.busy)

    assert await _image(hass, _camera_id(hass)) == WANTED_THUMBNAIL
    assert _commands(fake_station, 1025) == 0
    assert _commands(fake_station, 1003) == 0
    assert not decodes
    await _unload(hass, entry)


async def test_a_refresh_in_hd_with_no_recording_keeps_the_image_and_never_falls_back(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Only a thumbnail row in the window; the pressed live image stays, no thumbnail."""
    _seed_recordings(fake_station, with_recording=False)
    entry = await set_up_warm(hass, seed_warm_cache)
    manager = _manager(entry)
    camera_id = _camera_id(hass)

    await _press(hass, _button_id(hass, CAPTURE_LIVE_IMAGE_KEY))
    await wait_until(lambda: manager.source_for(SYNTHETIC.camera_sn) == "live")
    await wait_until(lambda: not manager.busy)
    live = await _image(hass, camera_id)
    queries = len(fake_station.history_queries)

    await _press(hass, _button_id(hass, REFRESH_IMAGE_KEY))
    await wait_until(lambda: len(fake_station.history_queries) > queries)
    await wait_until(lambda: not manager.busy)

    assert manager.source_for(SYNTHETIC.camera_sn) == "live"
    assert await _image(hass, camera_id) == live
    assert _commands(fake_station, 1308) == 0, "hd fell back to the thumbnail"
    assert _commands(fake_station, 1025) == 0
    assert not _errors(caplog)
    await _unload(hass, entry)


async def test_a_refresh_with_no_history_keeps_no_image_and_logs_no_error(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """No recorded event at all: the history is walked, and the camera still has no image."""
    entry = await set_up_warm(hass, seed_warm_cache)
    manager = _manager(entry)

    await _press(hass, _button_id(hass, REFRESH_IMAGE_KEY))
    await wait_until(lambda: len(fake_station.history_queries) > 0)
    await wait_until(lambda: not manager.busy)

    assert manager.image_for(SYNTHETIC.camera_sn) is None
    assert await _image(hass, _camera_id(hass)) is None
    assert not _errors(caplog)
    await _unload(hass, entry)


async def test_refresh_presses_while_one_is_running_coalesce(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Three presses during a slow still fetch: one history query, one still fetch."""
    _seed_recordings(fake_station)
    fake_station.image_reply_delay[WANTED_THUMB] = 0.5
    entry = await set_up_warm(hass, seed_warm_cache, options={CONF_CAMERA_IMAGE: "thumbnail"})
    manager = _manager(entry)
    button_id = _button_id(hass, REFRESH_IMAGE_KEY)

    for _ in range(3):
        elapsed = await _press(hass, button_id)
        assert elapsed < 1.0, f"a press waited {elapsed:.1f} s on the station"
    await wait_until(lambda: manager.source_for(SYNTHETIC.camera_sn) == "thumbnail")
    await wait_until(lambda: not manager.busy)
    assert _commands(fake_station, 1308) == 1
    assert len(fake_station.history_queries) == 1
    await _unload(hass, entry)


async def test_every_stored_still_writes_a_new_camera_state_with_its_own_image_updated(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Two captures and two refreshes of the same event: four states, four image_updated."""
    _seed_recordings(fake_station)
    entry = await set_up_warm(hass, seed_warm_cache, options={CONF_CAMERA_IMAGE: "thumbnail"})
    manager = _manager(entry)
    camera_id = _camera_id(hass)
    before = hass.states.get(camera_id)
    assert before is not None
    assert ATTR_IMAGE_UPDATED not in before.attributes
    assert ATTR_IMAGE_SOURCE not in before.attributes
    stamps = _record_attribute(hass, camera_id, ATTR_IMAGE_UPDATED)

    for key, opens in ((CAPTURE_LIVE_IMAGE_KEY, 1003), (REFRESH_IMAGE_KEY, 1308)):
        for n in (1, 2):
            await _press(hass, _button_id(hass, key))
            await wait_until(lambda n=n, opens=opens: _commands(fake_station, opens) == n)
            await wait_until(lambda: not manager.busy)
    await wait_until(lambda: len(stamps) == 4)

    # The refreshes showed the same event: only image_updated tells them apart.
    state = hass.states.get(camera_id)
    assert state is not None
    assert state.attributes[ATTR_IMAGE_SOURCE] == "thumbnail"
    assert len(set(stamps)) == 4
    assert stamps == sorted(stamps)
    assert all(dt_util.parse_datetime(stamp) is not None for stamp in stamps)
    await _unload(hass, entry)


async def test_image_updated_advances_when_the_clock_does_not(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Two stills stored at one frozen instant still get two different image_updated."""
    frozen = dt_util.utcnow()
    monkeypatch.setattr(snapshots, "_utcnow", lambda: frozen)
    entry = await set_up_warm(hass, seed_warm_cache)
    manager = _manager(entry)
    stamps = _record_attribute(hass, _camera_id(hass), ATTR_IMAGE_UPDATED)

    for n in (1, 2):
        await _press(hass, _button_id(hass, CAPTURE_LIVE_IMAGE_KEY))
        await wait_until(lambda n=n: _commands(fake_station, 1003) == n)
        await wait_until(lambda: not manager.busy)
    await wait_until(lambda: len(stamps) == 2)
    assert stamps[0] < stamps[1]
    await _unload(hass, entry)


async def test_a_detection_accepted_during_a_refresh_replaces_the_refreshed_image(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Refresh in flight, then a detection: the detection's thumbnail is what stays."""
    _seed_recordings(fake_station)
    fake_station.image_reply_delay[WANTED_THUMB] = 0.5
    entry = await set_up_warm(hass, seed_warm_cache, options={CONF_CAMERA_IMAGE: "thumbnail"})
    manager = _manager(entry)
    camera_id = _camera_id(hass)

    await _press(hass, _button_id(hass, REFRESH_IMAGE_KEY))
    await wait_until(lambda: _commands(fake_station, 1308) == 1)
    entry.runtime_data.router.handle(
        detection_event(DetectionType.MOTION, t_ms=now_ms(), thumb_path=PUSHED_THUMB_PATH)
    )
    await wait_until(lambda: _commands(fake_station, 1308) == 2)
    await wait_until(lambda: not manager.busy)
    assert await _image(hass, camera_id) == PUSHED_THUMBNAIL
    await _unload(hass, entry)


async def test_an_arm_while_an_hd_refresh_is_in_flight_is_not_delayed(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The recording open is received but no frame follows; arm home still lands at once."""
    _seed_recordings(fake_station)
    entry = await set_up_warm(hass, seed_warm_cache)
    manager = _manager(entry)

    def swallow(*_args: object, **_kwargs: object) -> None:
        """In place of the fake station's stream start: an open that sends no frame."""

    monkeypatch.setattr(fake_station, "_start_media", swallow)

    await _press(hass, _button_id(hass, REFRESH_IMAGE_KEY))
    await wait_until(lambda: _commands(fake_station, 1025) == 1)
    assert manager.busy

    started = time.monotonic()
    await hass.services.async_call(
        ALARM_DOMAIN,
        SERVICE_ALARM_ARM_HOME,
        {ATTR_ENTITY_ID: panel_entity_id(hass)},
        blocking=True,
    )
    elapsed = time.monotonic() - started

    assert elapsed < 2.0, f"arming waited {elapsed:.1f} s behind the HD refresh"
    assert fake_station.guard_mode == 1
    assert state_of(hass, panel_entity_id(hass)) == "armed_home"
    assert manager.busy, "the refresh had already ended: the test proved nothing"
    assert manager.image_for(SYNTHETIC.camera_sn) is None
    await _unload(hass, entry)


async def test_a_camera_without_presets_gets_no_preset_entities(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A camera without presets gets no preset entities and refuses the service."""
    entry = await set_up_warm(hass, seed_warm_cache)
    registry = er.async_get(hass)
    assert not any(
        entity.unique_id.endswith("refresh_presets") or "_preset_" in entity.unique_id
        for entity in registry.entities.values()
        if entity.platform == DOMAIN
    )
    assert hass.states.async_entity_ids("image") == []
    camera_id = _camera_id(hass)
    with pytest.raises(ServiceValidationError) as exc:
        await hass.services.async_call(
            DOMAIN, "capture_preset", {"entity_id": camera_id, "preset": 0}, blocking=True
        )
    assert exc.value.translation_key == "presets_unsupported"
    await _unload(hass, entry)


@pytest.mark.parametrize(
    ("action", "data"),
    [
        ("pan_tilt", {"direction": "left"}),
        ("goto_preset", {"preset": 0}),
        ("zoom", {"direction": "in"}),
        ("save_preset", {}),
        ("delete_preset", {"preset": 0}),
    ],
)
async def test_a_camera_without_pan_tilt_gets_no_zoom_and_refuses_the_ptz_actions(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    action: str,
    data: dict[str, Any],
) -> None:
    """A camera without pan/tilt gets no zoom or save-view entity; each PTZ action is refused."""
    entry = await set_up_warm(hass, seed_warm_cache)
    assert not any(
        entity.unique_id.endswith(("_live_zoom", "_save_view"))
        for entity in er.async_get(hass).entities.values()
        if entity.platform == DOMAIN
    )
    with pytest.raises(ServiceValidationError) as exc:
        await hass.services.async_call(
            DOMAIN, action, {"entity_id": _camera_id(hass), **data}, blocking=True
        )
    assert exc.value.translation_key == "pan_tilt_unsupported"
    await _unload(hass, entry)
