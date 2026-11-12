"""Station recordings played straight to a viewer as fragmented-MP4 HLS.

A play is a websocket subscription (``station_recordings``, ``recordings/play``). The
library plays the recording on an extra session (``Station.async_play_recording``)
and writes its MPEG-TS into the play's remux thread (``recording_hls.TsRemuxer``),
which publishes fMP4 segments into the play's :class:`~.recording_hls.HlsPlaylist`.
:class:`RecordingHlsView` serves them under a capability token, as Home Assistant's
own HLS views do. Nothing is decoded, re-encoded or saved; Save is the separate
``recordings/fetch``.

- **Before the first segment** a library error answers the subscription with the code
  and message a fetch gives (``station_recordings.transfer_error``).
- **At the first segment** an empty result, then the first event ``{"url",
  "mime_type"}`` (the frontend's ``subscribeMessage`` drops a result's payload).
- **One end event** once the library has returned (or raised) and the remux has
  published its last segment, so the playlist already carries ``#EXT-X-ENDLIST``:
  ``{"end": "complete", "duration"}``, or ``{"end": "error", "code", "message"}``
  (``codec_change`` for a clip cut at a video-codec change). A play that ends without
  any segment is answered with the error instead.
- **After the end** the play is served until the viewer unsubscribes or nobody asked
  for it for ``PLAY_KEEP_SECONDS``, so the player can finish and seek back.
- **The viewer leaving** (unsubscribe, or the websocket closing) cancels the library
  call, which closes its extra session, stops the remux and frees every segment at
  once; the token answers 404 from then on.

Diagnostics count plays by outcome and the most bytes one play held; no serial, ref
or token is logged or reported.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, Final

from aiohttp import web
from homeassistant.components import websocket_api
from homeassistant.components.http import KEY_HASS, HomeAssistantView
from homeassistant.core import HomeAssistant, callback
from homeassistant.util.hass_dict import HassKey

from eufy_home_security import EufySecurityError, HistoryRecord, redact_serial

from .const import DOMAIN
from .recording_hls import (
    HLS_MIME_TYPE,
    INIT_MIME_TYPE,
    SEGMENT_MIME_TYPE,
    HlsPlaylist,
    RemuxError,
    RemuxResult,
    Segment,
    TsRemuxer,
)
from .station_recordings import (
    ERR_CODEC_CHANGE,
    ERR_FAILED,
    ERR_UNAVAILABLE,
    RecordingError,
    transfer_error,
)

if TYPE_CHECKING:
    from .runtime import EufyConfigEntry
    from .station_recordings import _Camera

_LOGGER = logging.getLogger(__name__)

# Seconds an ended play stays served without a request.
PLAY_KEEP_SECONDS: Final = 300.0
# Seconds a playlist request waits for the first segment.
PLAYLIST_WAIT_SECONDS: Final = 15.0
# Media one play holds; past it the oldest segments go.
PLAY_MAX_BYTES: Final = 96 * 1024 * 1024
# The library writes at the recording's own pace: a playlist growing faster than it
# plays makes Home Assistant's player catch up at up to twice the speed.
PLAY_REALTIME: Final = True
RECORDING_URL: Final = "/api/eufy_home_security/recording/{token}/{name}"
RECORDING_VIEW_NAME: Final = f"api:{DOMAIN}:recording_hls"
_PLAYLIST: Final = "playlist.m3u8"
_INIT: Final = re.compile(r"init(\d{1,6})\.mp4")
_SEGMENT: Final = re.compile(r"seg(\d{1,9})\.m4s")
# A token's file never changes; the token is freed, not reused.
_IMMUTABLE: Final = "max-age=3600, immutable"
_REMUX_THREAD: Final = f"{DOMAIN}_recording_remux"
# Seconds an unload waits for an aborted remux thread.
_JOIN_SECONDS: Final = 5.0

_COMPLETE: Final = "complete"
_FAILED: Final = "failed"
_PLAY_CODEC_CHANGE_MESSAGE: Final = (
    "This recording changes video format partway through, so it plays only up to there;"
    " play it in the eufy app"
)


@dataclass(frozen=True, slots=True)
class _End:
    """How a play ends: its counter and its error (code and message; None for
    complete)."""

    outcome: str
    code: str | None = None
    message: str | None = None

    def event(self, duration: float) -> dict[str, Any]:
        if self.code is None:
            return {"end": _COMPLETE, "duration": round(duration, 3)}
        return {"end": "error", "code": self.code, "message": self.message or ""}


_DONE: Final = _End(_COMPLETE)
_STOPPED: Final = _End(_FAILED, ERR_FAILED, "The play was stopped")
_NOT_PLAYED: Final = _End(_FAILED, ERR_FAILED, "The recording could not be played")


@dataclass(slots=True)
class PlayStats:
    """The entry's plays since setup, by outcome (``failed`` by error code), and the
    most bytes one play held."""

    started: int = 0
    complete: int = 0
    viewer_left: int = 0
    failed: dict[str, int] = field(default_factory=dict)
    bytes_held_peak: int = 0


@dataclass(slots=True)
class _Served:
    """Every entry's plays by token, and whether the view is registered."""

    plays: dict[str, _Play] = field(default_factory=dict)
    view_registered: bool = False


_SERVED: HassKey[_Served] = HassKey(f"{DOMAIN}_recording_plays")


def _served(hass: HomeAssistant) -> _Served:
    served = hass.data.get(_SERVED)
    if served is None:
        served = hass.data[_SERVED] = _Served()
    return served


class _Play:
    """One play: its library call, its remux thread, its playlist and subscription."""

    def __init__(
        self,
        camera: _Camera,
        row: HistoryRecord,
        connection: websocket_api.ActiveConnection,
        msg_id: int,
    ) -> None:
        self.token = secrets.token_hex(16)
        self.camera = camera
        self.row = row
        self.connection = connection
        self.msg_id = msg_id
        self.playlist = HlsPlaylist(PLAY_MAX_BYTES)
        self.task: asyncio.Task[None] | None = None
        # This module cancelled the library call.
        self.cancelled = False
        self.remuxer: TsRemuxer | None = None
        self.thread: threading.Thread | None = None
        self.remux_done = False
        self.remux_error: _End | None = None
        self.library_end: _End | None = None
        self.wrote = False
        self.answered = False
        self.ended = False
        self.freed = False
        # Set at the first segment, or when the play is freed.
        self.ready = asyncio.Event()
        self.last_request = 0.0
        self.keep_timer: asyncio.TimerHandle | None = None

    @property
    def url(self) -> str:
        return RECORDING_URL.format(token=self.token, name=_PLAYLIST)


class RecordingPlays:
    """One entry's recording plays: started by the websocket command, ended once each."""

    def __init__(self, hass: HomeAssistant, entry: EufyConfigEntry) -> None:
        self._hass = hass
        self._entry = entry
        self._served = _served(hass)
        if not self._served.view_registered:
            self._served.view_registered = True
            hass.http.register_view(RecordingHlsView())
        self._plays: set[_Play] = set()
        self._threads: set[threading.Thread] = set()
        self._stopped = False
        self.stats = PlayStats()

    def diagnostics(self) -> dict[str, Any]:
        """The counters and the plays running or kept; no ref or token."""
        return {
            "started": self.stats.started,
            "complete": self.stats.complete,
            "viewer_left": self.stats.viewer_left,
            "failed": dict(self.stats.failed),
            "active": len(self._plays),
            "bytes_held_peak": self.stats.bytes_held_peak,
        }

    async def async_play(
        self,
        connection: websocket_api.ActiveConnection,
        msg_id: int,
        camera: _Camera,
        row: HistoryRecord,
    ) -> None:
        """Play ``row`` for the subscription ``msg_id``; the answers follow from the
        library's writes and the remux (see the module docstring). Raises
        :class:`RecordingError` once the plays are stopped."""
        if self._stopped:
            raise RecordingError(ERR_UNAVAILABLE, "The camera's account is not loaded")
        play = _Play(camera, row, connection, msg_id)
        self._plays.add(play)
        self._served.plays[play.token] = play
        self.stats.started += 1
        # Registered before the first bytes, so a websocket that closes meanwhile
        # cancels the play too.
        connection.subscriptions[msg_id] = lambda: self._viewer_left(play)
        play.task = self._entry.async_create_background_task(
            self._hass, self._async_library(play), name=f"{DOMAIN} recording play"
        )

    async def _async_library(self, play: _Play) -> None:
        """Run the library's play into the remux; record how it ended."""
        camera = play.camera

        async def write(data: bytes) -> None:
            if play.remuxer is None:
                play.remuxer = self._start_remux(play)
            play.wrote = True
            play.remuxer.feed(data)

        try:
            clip = await camera.station.async_play_recording(
                play.row, write, realtime=PLAY_REALTIME
            )
        except asyncio.CancelledError:
            if not play.cancelled:
                # Cancelled from outside (Home Assistant stopping): nothing more comes.
                self._end(play, _STOPPED)
                self._free(play)
            raise
        except (EufySecurityError, TimeoutError) as err:
            _LOGGER.debug(
                "Recording play of %s ended by the library (%s)",
                redact_serial(camera.device_sn),
                type(err).__name__,
            )
            answer = transfer_error(camera, err)
            end = _End(_FAILED, answer.code, str(answer))
        except Exception:
            # Ends the play rather than leaving its subscription open.
            _LOGGER.exception("Recording play of %s failed", redact_serial(camera.device_sn))
            end = _NOT_PLAYED
        else:
            if not play.wrote:
                end = _End(_FAILED, ERR_UNAVAILABLE, "The HomeBase did not play it")
            elif clip.ended_early:
                end = _End(_FAILED, ERR_CODEC_CHANGE, _PLAY_CODEC_CHANGE_MESSAGE)
            else:
                end = _DONE
        play.library_end = end
        if play.remuxer is not None:
            play.remuxer.finish()
        self._settle(play)

    def _start_remux(self, play: _Play) -> TsRemuxer:
        """Start the play's remux thread; what it publishes comes back on the loop."""
        loop = self._hass.loop

        def post(target: Callable[..., None], *args: Any) -> None:
            # A closed loop: Home Assistant has stopped.
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(target, play, *args)

        remuxer = TsRemuxer(
            lambda index, data: post(self._on_init, index, data),
            lambda segment: post(self._on_segment, segment),
        )

        def run() -> None:
            result: RemuxResult | None = None
            error: Exception | None = None
            try:
                result = remuxer.run()
            except Exception as err:  # noqa: BLE001 - every failure ends the play
                error = err
            post(self._on_remux_done, result, error)

        thread = play.thread = threading.Thread(target=run, name=_REMUX_THREAD, daemon=True)
        self._threads.add(thread)
        thread.start()
        return remuxer

    @callback
    def _on_init(self, play: _Play, index: int, data: bytes) -> None:
        if not play.freed:
            play.playlist.add_init(index, data)

    @callback
    def _on_segment(self, play: _Play, segment: Segment) -> None:
        """List the segment; the first one answers the subscription with the URL."""
        if play.freed:
            return
        play.playlist.add_segment(segment)
        self.stats.bytes_held_peak = max(self.stats.bytes_held_peak, play.playlist.peak)
        play.ready.set()
        if not play.answered:
            play.answered = True
            play.connection.send_result(play.msg_id)
            play.connection.send_message(
                websocket_api.event_message(
                    play.msg_id, {"url": play.url, "mime_type": HLS_MIME_TYPE}
                )
            )

    @callback
    def _on_remux_done(
        self, play: _Play, result: RemuxResult | None, error: Exception | None
    ) -> None:
        play.remux_done = True
        if play.thread is not None:
            self._threads.discard(play.thread)
        if play.freed:
            return
        if result is not None:
            _LOGGER.debug(
                "Recording play repackaged: %d segment(s), %d init(s), %.1f s of media"
                " in %.2f s of CPU",
                result.segments,
                result.inits,
                result.media_seconds,
                result.cpu_seconds,
            )
        elif isinstance(error, RemuxError):
            _LOGGER.debug("Recording play could not be repackaged: %s", error)
        else:
            _LOGGER.error("Recording play could not be repackaged", exc_info=error)
        if error is not None:
            play.remux_error = _NOT_PLAYED
            self._cancel_library(play)
        play.playlist.end()
        self._settle(play)

    @callback
    def _settle(self, play: _Play) -> None:
        """End the play once its library call and its remux are both done, by the
        library's error first; a remux failure while the library plays ends it at once
        (a remux ends by itself only on an error)."""
        end = play.library_end
        if end is None:
            end = play.remux_error
            assert end is not None
        elif play.remuxer is not None and not play.remux_done:
            return
        elif end.code is None and play.remux_error is not None:
            end = play.remux_error
        self._end(play, end)

    @callback
    def _end(self, play: _Play, end: _End) -> None:
        """Count ``end`` and tell the viewer; a play never answered is freed, an
        answered one stays served until ``PLAY_KEEP_SECONDS`` after its last request."""
        play.ended = True
        if end.outcome == _COMPLETE:
            self.stats.complete += 1
        else:
            code = end.code or ERR_FAILED
            self.stats.failed[code] = self.stats.failed.get(code, 0) + 1
        if not play.answered:
            # An error answer ends a subscription that never started.
            play.connection.subscriptions.pop(play.msg_id, None)
            play.connection.send_error(play.msg_id, end.code or ERR_FAILED, end.message or "")
            self._free(play)
            return
        play.connection.send_message(
            websocket_api.event_message(play.msg_id, end.event(play.playlist.duration))
        )
        play.last_request = max(play.last_request, self._hass.loop.time())
        self._keep(play)

    @callback
    def _keep(self, play: _Play) -> None:
        """Free an ended play ``PLAY_KEEP_SECONDS`` after its last request."""
        play.keep_timer = None
        idle = self._hass.loop.time() - play.last_request
        if idle >= PLAY_KEEP_SECONDS:
            self._free(play)
            return
        play.keep_timer = self._hass.loop.call_later(PLAY_KEEP_SECONDS - idle, self._keep, play)

    @callback
    def _viewer_left(self, play: _Play) -> None:
        """Unsubscribe or a closed websocket: stop everything and free the play."""
        if not play.ended:
            play.ended = True
            self.stats.viewer_left += 1
        self._free(play)

    @callback
    def _free(self, play: _Play) -> None:
        """Stop the library call and the remux; let go of every byte and the token."""
        play.freed = True
        if play.keep_timer is not None:
            play.keep_timer.cancel()
            play.keep_timer = None
        self._cancel_library(play)
        if play.remuxer is not None:
            play.remuxer.abort()
        play.playlist.free()
        play.ready.set()
        self._served.plays.pop(play.token, None)
        self._plays.discard(play)

    @staticmethod
    def _cancel_library(play: _Play) -> None:
        if play.task is not None and not play.task.done():
            play.cancelled = True
            play.task.cancel()

    async def async_stop(self) -> None:
        """End every play still running (``failed``), free every play, and wait for
        the library calls and remux threads to stop. For the entry's unload."""
        self._stopped = True
        tasks = []
        for play in list(self._plays):
            if not play.ended:
                self._end(play, _STOPPED)
            if play.task is not None:
                tasks.append(play.task)
            self._free(play)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for thread in list(self._threads):
            await self._hass.async_add_executor_job(thread.join, _JOIN_SECONDS)
        self._threads.clear()


class RecordingHlsView(HomeAssistantView):
    """Serves the plays' playlists, inits and segments by capability token."""

    url = RECORDING_URL
    name = RECORDING_VIEW_NAME
    # The token is the credential, as for Home Assistant's own HLS views: a native
    # player sends no Authorization header.
    requires_auth = False

    async def get(self, request: web.Request, token: str, name: str) -> web.StreamResponse:
        """The playlist (waiting up to ``PLAYLIST_WAIT_SECONDS`` for the first
        segment), an init or a segment; 404 for an unknown or freed token or file."""
        play = _served(request.app[KEY_HASS]).plays.get(token)
        if play is None:
            return web.Response(status=HTTPStatus.NOT_FOUND)
        play.last_request = asyncio.get_running_loop().time()
        if name == _PLAYLIST:
            if not play.ready.is_set():
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(PLAYLIST_WAIT_SECONDS):
                        await play.ready.wait()
            if not play.playlist.segments:
                return web.Response(status=HTTPStatus.NOT_FOUND)
            return web.Response(
                body=play.playlist.render().encode(),
                content_type=HLS_MIME_TYPE,
                headers={"Cache-Control": "no-cache"},
            )
        body: bytes | None = None
        content_type = SEGMENT_MIME_TYPE
        if match := _INIT.fullmatch(name):
            body = play.playlist.init(int(match.group(1)))
            content_type = INIT_MIME_TYPE
        elif match := _SEGMENT.fullmatch(name):
            body = play.playlist.segment(int(match.group(1)))
        if body is None:
            return web.Response(status=HTTPStatus.NOT_FOUND)
        return web.Response(
            body=body, content_type=content_type, headers={"Cache-Control": _IMMUTABLE}
        )
