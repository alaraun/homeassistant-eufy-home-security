"""The camera's ``record`` action: a live clip saved as an MP4 in the event history.

End to end on the library's loopback ``FakeStation``, which streams live frames at
40 ms of camera time each, much faster than real time; the clip's MPEG-TS is remuxed
by ``tests/fake_ffmpeg.py`` (conftest's autouse ``_fake_ffmpeg``). Library failures
are staged by replacing ``StreamBroadcast.async_capture`` with a plain function.
"""

from __future__ import annotations

import asyncio
import errno
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

import pytest
import voluptuous as vol
from conftest import (
    SYNTHETIC,
    entity_id_for,
    record_states,
    set_up_warm,
    state_of,
    wait_until,
)
from eufy_home_security import (
    CameraWakeError,
    CaptureStoppedError,
    ClipWriter,
    DeviceTimeoutError,
    EufySecurity,
    EufySecurityError,
    LiveStreamLimitError,
    MediaClip,
    StreamBroadcast,
    redact_serial,
)
from eufy_home_security.testing import FakeStation
from fake_ffmpeg import REMUX_MAGIC
from homeassistant.components.camera import DOMAIN as CAMERA_DOMAIN
from homeassistant.components.camera import CameraState
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.eufy_home_security import history
from custom_components.eufy_home_security.const import (
    ATTR_COMPLETE,
    ATTR_DURATION,
    ATTR_MEDIA_CONTENT_ID,
    CAMERA_KEY,
    CONF_EVENT_HISTORY_DAYS,
    CONF_RECORD_LENGTH,
    DEFAULT_RECORD_LENGTH_SECONDS,
    DOMAIN,
    SERVICE_RECORD,
    SERVICE_STOP_RECORDING,
)

CAMERA_NAME: Final = "Front"
# The integration's own logger.
LOGGER: Final = "custom_components.eufy_home_security"
TS_BYTES: Final = b"\x47" + b"\x00" * 187


def _camera(hass: HomeAssistant) -> str:
    return entity_id_for(hass, CAMERA_DOMAIN, SYNTHETIC.camera_sn, CAMERA_KEY)


async def _record(hass: HomeAssistant, **data: Any) -> dict[str, Any]:
    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_RECORD,
        {ATTR_ENTITY_ID: _camera(hass), **data},
        blocking=True,
        return_response=True,
    )
    assert isinstance(response, dict)
    result = response[_camera(hass)]
    assert isinstance(result, dict)
    return result


async def _stop(hass: HomeAssistant) -> None:
    await hass.services.async_call(
        DOMAIN, SERVICE_STOP_RECORDING, {ATTR_ENTITY_ID: _camera(hass)}, blocking=True
    )


def _videos(hass: HomeAssistant) -> list[Path]:
    root = history.history_dir(hass)
    return sorted(root.rglob("*.mp4")) if root.is_dir() else []


def _all_files(hass: HomeAssistant) -> list[Path]:
    root = history.history_dir(hass)
    return sorted(p for p in root.rglob("*") if p.is_file()) if root.is_dir() else []


def _stub_capture(
    monkeypatch: pytest.MonkeyPatch,
    *,
    error: EufySecurityError | None = None,
    ended_early: bool = False,
    gate: asyncio.Event | None = None,
) -> list[float]:
    """Replace the capture with one that writes two TS packets; returns the seconds asked."""
    asked: list[float] = []

    async def capture(
        self: StreamBroadcast,
        seconds: float,
        write: ClipWriter,
        *,
        start_timeout: float | None = None,
        stop: asyncio.Event | None = None,
    ) -> MediaClip:
        asked.append(seconds)
        if gate is not None:
            await gate.wait()
        if error is not None:
            raise error
        await write(TS_BYTES)
        await write(TS_BYTES)
        return MediaClip(
            video_frames=10,
            audio_frames=10,
            keyframes=3,
            bytes_written=2 * len(TS_BYTES),
            duration_s=seconds / 2 if ended_early else seconds,
            width=3840,
            height=2160,
            resizes=0,
            ended_early=ended_early,
        )

    monkeypatch.setattr(StreamBroadcast, "async_capture", capture)
    return asked


async def _unload(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_record_saves_a_live_clip_and_responds_with_its_media_id(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A real capture of the fake's live stream lands as ``<now>_<camera>_live.mp4``.

    The response names the file under Home Assistant's local media source, the clip's
    length by the camera's clock and whether it is whole; the camera's stream ends.
    """
    entry = await set_up_warm(hass, seed_warm_cache)

    result = await _record(hass, duration=5)

    (video,) = _videos(hass)
    assert video.parent == history.history_dir(hass) / CAMERA_NAME
    assert video.name.endswith(f"_{CAMERA_NAME}_live.mp4")
    assert video.read_bytes().startswith(REMUX_MAGIC + b"\x47")
    assert result[ATTR_MEDIA_CONTENT_ID] == (
        f"media-source://media_source/local/{DOMAIN}/{CAMERA_NAME}/{video.name}"
    )
    assert result[ATTR_COMPLETE] is True
    assert result[ATTR_DURATION] == pytest.approx(5, abs=0.1)
    assert set(result) == {ATTR_MEDIA_CONTENT_ID, ATTR_DURATION, ATTR_COMPLETE}
    assert [p for p in _all_files(hass) if p.name.startswith(".")] == []
    await wait_until(lambda: not fake_station.live_cameras)
    await _unload(hass, entry)


@pytest.mark.parametrize(
    ("options", "data", "expected"),
    [
        ({}, {}, DEFAULT_RECORD_LENGTH_SECONDS),
        ({CONF_RECORD_LENGTH: 12}, {}, 12),
        ({CONF_RECORD_LENGTH: 12}, {ATTR_DURATION: 7}, 7),
        ({}, {ATTR_DURATION: 5}, 5),
        ({}, {ATTR_DURATION: 300}, 300),
    ],
    ids=["default", "option", "override", "lowest", "highest"],
)
async def test_record_takes_the_duration_or_the_recording_length_option(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    options: dict[str, Any],
    data: dict[str, Any],
    expected: int,
) -> None:
    """``duration`` wins; without it the option; without that the 30 s default."""
    asked = _stub_capture(monkeypatch)
    entry = await set_up_warm(hass, seed_warm_cache, options=options)

    result = await _record(hass, **data)

    assert asked == [expected]
    assert result[ATTR_DURATION] == expected
    await _unload(hass, entry)


@pytest.mark.parametrize("duration", [4, 301, 0])
async def test_a_duration_outside_5_to_300_seconds_is_refused(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    duration: int,
) -> None:
    """The schema refuses it; nothing is captured."""
    asked = _stub_capture(monkeypatch)
    entry = await set_up_warm(hass, seed_warm_cache)

    with pytest.raises(vol.Invalid):
        await _record(hass, duration=duration)

    assert asked == []
    await _unload(hass, entry)


async def test_the_camera_shows_recording_while_it_records_and_refuses_a_second_clip(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """State ``recording`` for the clip's run, then back; a call meanwhile is refused."""
    gate = asyncio.Event()
    asked = _stub_capture(monkeypatch, gate=gate)
    entry = await set_up_warm(hass, seed_warm_cache)
    camera = _camera(hass)
    before = state_of(hass, camera)
    states = record_states(hass, camera)

    first = hass.async_create_task(_record(hass, duration=5))
    await wait_until(lambda: bool(asked))
    assert state_of(hass, camera) == CameraState.RECORDING

    with pytest.raises(ServiceValidationError) as refused:
        await _record(hass, duration=5)
    assert refused.value.translation_key == "recording_in_progress"
    assert len(asked) == 1

    gate.set()
    await first
    assert state_of(hass, camera) == before
    assert states == [CameraState.RECORDING, before]
    await _unload(hass, entry)


async def test_record_needs_the_event_history(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the history off nothing is captured and nothing is written."""
    asked = _stub_capture(monkeypatch)
    entry = await set_up_warm(hass, seed_warm_cache, options={CONF_EVENT_HISTORY_DAYS: 0})

    with pytest.raises(ServiceValidationError) as refused:
        await _record(hass)

    assert refused.value.translation_key == "recording_needs_history"
    assert asked == []
    assert not history.history_dir(hass).exists()
    await _unload(hass, entry)


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (LiveStreamLimitError("budget", limit=2), "live_stream_limit"),
        (CameraWakeError(1003, -204), "camera_unavailable"),
        (DeviceTimeoutError("no keyframe"), "camera_unavailable"),
    ],
    ids=["limit", "wake", "timeout"],
)
async def test_a_capture_the_camera_did_not_deliver_raises_a_translated_error(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    error: EufySecurityError,
    key: str,
) -> None:
    """Each library failure has its message; nothing is kept and the camera is idle again."""
    _stub_capture(monkeypatch, error=error)
    entry = await set_up_warm(hass, seed_warm_cache)
    before = state_of(hass, _camera(hass))

    with pytest.raises(HomeAssistantError) as raised:
        await _record(hass)

    assert raised.value.translation_key == key
    if key == "live_stream_limit":
        assert raised.value.translation_placeholders == {"limit": "2"}
    assert _all_files(hass) == []
    assert state_of(hass, _camera(hass)) == before
    await _unload(hass, entry)


async def test_a_clip_that_cannot_be_stored_raises_recording_failed(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed remux keeps nothing and says so; temp files go too."""
    _stub_capture(monkeypatch)
    monkeypatch.setenv("FAKE_FFMPEG_FAIL", "1")
    entry = await set_up_warm(hass, seed_warm_cache)

    with pytest.raises(HomeAssistantError) as raised:
        await _record(hass)

    assert raised.value.translation_key == "recording_failed"
    assert _all_files(hass) == []
    await _unload(hass, entry)


async def test_a_clip_that_cannot_be_written_logs_the_error_type_not_the_path(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An OSError's text carries the clip's path, and so the camera's name: not logged."""
    entry = await set_up_warm(hass, seed_warm_cache)
    clip = history.history_dir(hass) / CAMERA_NAME / f"clip_{CAMERA_NAME}_live.mp4"

    async def full_disk(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError(errno.ENOSPC, "No space left on device", str(clip))

    monkeypatch.setattr(history.EventHistory, "async_save_clip", full_disk)

    with (
        caplog.at_level(logging.WARNING, logger=LOGGER),
        pytest.raises(HomeAssistantError) as raised,
    ):
        await _record(hass)

    assert raised.value.translation_key == "recording_failed"
    (warning,) = [
        record.getMessage()
        for record in caplog.records
        if record.name.startswith(LOGGER) and record.levelno == logging.WARNING
    ]
    assert "OSError" in warning
    assert redact_serial(SYNTHETIC.camera_sn) in warning
    assert CAMERA_NAME not in warning
    assert str(history.history_dir(hass)) not in warning
    await _unload(hass, entry)


async def test_a_capture_that_ended_early_is_kept_as_incomplete(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stream that ended before the duration still saves what it got, ``complete`` False."""
    _stub_capture(monkeypatch, ended_early=True)
    entry = await set_up_warm(hass, seed_warm_cache)

    result = await _record(hass, duration=10)

    assert result[ATTR_COMPLETE] is False
    assert result[ATTR_DURATION] == 5
    assert len(_videos(hass)) == 1
    await _unload(hass, entry)


async def test_record_on_a_camera_without_a_live_stream_is_refused(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No broadcast, nothing to capture: refused before anything opens."""
    asked = _stub_capture(monkeypatch)
    entry = await set_up_warm(hass, seed_warm_cache)
    monkeypatch.setattr(type(entry.runtime_data.streaming), "broadcast", lambda _self, _sn: None)

    with pytest.raises(ServiceValidationError) as refused:
        await _record(hass)

    assert refused.value.translation_key == "recording_unsupported"
    assert asked == []
    await _unload(hass, entry)


async def test_stop_recording_ends_a_running_clip_and_keeps_it_whole(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A real capture of the fake's stream, stopped long before its length: the part
    recorded is saved, ``complete`` True, and the camera is idle again."""
    entry = await set_up_warm(hass, seed_warm_cache)
    camera = _camera(hass)
    before = state_of(hass, camera)

    running = hass.async_create_task(_record(hass, duration=300))
    await wait_until(lambda: state_of(hass, camera) == CameraState.RECORDING)
    await wait_until(lambda: bool(fake_station.live_cameras))
    await asyncio.sleep(1)
    await _stop(hass)
    result = await asyncio.wait_for(running, 30)

    assert result[ATTR_COMPLETE] is True
    assert 0 < result[ATTR_DURATION] < 60
    assert len(_videos(hass)) == 1
    assert state_of(hass, camera) == before
    await wait_until(lambda: not fake_station.live_cameras)
    await _unload(hass, entry)


async def test_stop_recording_with_no_clip_running_is_refused(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Nothing records: a validation error, nothing changes."""
    entry = await set_up_warm(hass, seed_warm_cache)

    with pytest.raises(ServiceValidationError) as refused:
        await _stop(hass)

    assert refused.value.translation_key == "recording_not_running"
    assert _all_files(hass) == []
    await _unload(hass, entry)


async def test_a_clip_stopped_before_its_first_picture_saves_nothing(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The library's ``CaptureStoppedError``: its own message, no file, the camera idle."""
    _stub_capture(monkeypatch, error=CaptureStoppedError("stopped before the first keyframe"))
    entry = await set_up_warm(hass, seed_warm_cache)
    before = state_of(hass, _camera(hass))

    with pytest.raises(HomeAssistantError) as raised:
        await _record(hass)

    assert raised.value.translation_key == "recording_stopped_empty"
    assert _all_files(hass) == []
    assert state_of(hass, _camera(hass)) == before
    await _unload(hass, entry)
