"""Station recordings played straight to a viewer through Home Assistant's stream.

A play is a websocket subscription (``station_recordings``, ``recordings/play``). The
library plays the recording on an extra session at its own pace
(``Station.async_play_recording``) and writes its MPEG-TS into the play's queue; the
loopback view (``streaming.RecordingPlayView``) serves that queue once to a ``Stream``
of Home Assistant's stream component. Nothing is decoded, re-encoded or saved; Save is
the separate ``recordings/fetch``.

- **Before the first bytes** a library error answers the subscription with the code
  and message a fetch gives (``station_recordings.transfer_error``); no Stream exists.
- **At the first bytes** the Stream starts; an empty result, then the first event
  ``{"url", "mime_type"}`` (the frontend's ``subscribeMessage`` drops a result's
  payload).
- **One end event**, then the play is gone: ``{"end": "complete", "duration"}`` when
  the library returned, ``{"end": "error", "code", "message"}`` when it raised after the
  first bytes, the clip stopped at a codec change (``codec_change``), Home Assistant's
  stream never read the source within ``PLAY_ATTACH_TIMEOUT`` or stopped reading it,
  or the entry unloaded (``failed``). The event goes out at once; after the library's
  end the Stream lives ``PLAY_END_GRACE`` longer, so a player behind the live edge
  still fetches the last segment. Every end runs :meth:`RecordingPlays._async_end`.
- **The viewer leaving** (unsubscribe, or the websocket closing) cancels the library
  call, which closes its extra session, and stops the Stream at once; no event.
- The source URL answers one GET per play (410 after it): the stream worker asks a
  source that ended again after 10 s, and must not start a second play.
- Home Assistant's stream worker logs an ERROR at every end of its source; a filter on
  the plays' one stream logger (:class:`_EndedPlaysFilter`) drops that line for a play
  this integration ended on purpose, until its Stream has stopped.

Diagnostics count plays by outcome; no serial, ref or play id is logged or reported.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from homeassistant.components import websocket_api
from homeassistant.components.camera import DynamicStreamSettings
from homeassistant.components.stream import HLS_PROVIDER, Stream, create_stream
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util.hass_dict import HassKey

from eufy_home_security import EufySecurityError, HistoryRecord, MediaClip, redact_serial

from . import streaming
from .const import DOMAIN
from .station_recordings import ERR_CODEC_CHANGE, ERR_FAILED, ERR_UNAVAILABLE, transfer_error

if TYPE_CHECKING:
    from .runtime import EufyConfigEntry
    from .station_recordings import _Camera

_LOGGER = logging.getLogger(__name__)

# Seconds the Stream outlives the end of the recording, so a player behind the live
# edge still fetches the last segment. Under 10 s: the stream worker asks an ended
# source again 10 s after its end.
PLAY_END_GRACE: Final = 8.0
# Seconds a play waits for the Stream to read its source before it is stopped.
PLAY_ATTACH_TIMEOUT: Final = 15.0
HLS_MIME_TYPE: Final = "application/vnd.apple.mpegurl"
# One label for every play's Stream: the stream component keeps a Logger per label.
STREAM_LABEL: Final = f"{DOMAIN}_recording"
STREAM_LOGGER: Final = f"homeassistant.components.stream.stream.{STREAM_LABEL}"
# How the stream worker's error line starts (``Stream._run_worker``).
_WORKER_ERROR: Final = "Error from stream worker:"

_COMPLETE: Final = "complete"
_VIEWER_LEFT: Final = "viewer_left"
_FAILED: Final = "failed"
_PLAY_CODEC_CHANGE_MESSAGE: Final = (
    "This recording changes video format partway through, so it plays only up to there;"
    " play it in the eufy app"
)


@dataclass(frozen=True, slots=True)
class _End:
    """How a play ends: its counter, its error (code and message; None for complete or
    a viewer that left), whether the Stream outlives it by ``PLAY_END_GRACE``, whether
    the stream worker's error at the source's end is expected (``quiet``), and the
    played length for ``complete``."""

    outcome: str
    code: str | None = None
    message: str | None = None
    grace: bool = False
    quiet: bool = False
    duration: float | None = None

    def event(self) -> dict[str, Any]:
        if self.code is None:
            return {"end": _COMPLETE, "duration": self.duration}
        return {"end": "error", "code": self.code, "message": self.message or ""}


_LEFT: Final = _End(_VIEWER_LEFT, quiet=True)
_NOT_READ: Final = _End(_FAILED, ERR_FAILED, "Home Assistant's stream did not open the recording")
_READER_GONE: Final = _End(
    _FAILED, ERR_FAILED, "Home Assistant's stream stopped reading the recording"
)
_STOPPED: Final = _End(_FAILED, ERR_FAILED, "The play was stopped", quiet=True)


class _EndedPlaysFilter(logging.Filter):
    """Drops the stream worker's error line naming a play in ``ended``; passes the rest.

    A reworded line no longer matches and is logged again.
    """

    def __init__(self) -> None:
        super().__init__()
        self.ended: set[str] = set()
        self.users = 0

    def filter(self, record: logging.LogRecord) -> bool:
        if not self.ended:
            return True
        message = record.getMessage()
        return not (message.startswith(_WORKER_ERROR) and any(pid in message for pid in self.ended))


_FILTER: HassKey[_EndedPlaysFilter] = HassKey(f"{DOMAIN}_recording_play_filter")


def _hold_filter(hass: HomeAssistant) -> _EndedPlaysFilter:
    """The instance's filter, attached to the plays' stream logger by its first user."""
    found = hass.data.get(_FILTER)
    if found is None:
        found = hass.data[_FILTER] = _EndedPlaysFilter()
        logging.getLogger(STREAM_LOGGER).addFilter(found)
    found.users += 1
    return found


def _release_filter(hass: HomeAssistant, held: _EndedPlaysFilter) -> None:
    """Detach the filter when its last user lets go."""
    held.users -= 1
    if held.users <= 0:
        logging.getLogger(STREAM_LOGGER).removeFilter(held)
        hass.data.pop(_FILTER, None)


@dataclass(slots=True)
class PlayStats:
    """The entry's plays since setup, by outcome; ``failed`` by error code."""

    started: int = 0
    complete: int = 0
    viewer_left: int = 0
    failed: dict[str, int] = field(default_factory=dict)


class _Play:
    """One play: its library call, its byte queue, its Stream and its subscription.

    The ``streaming.RecordingSource`` the loopback view reads.
    """

    def __init__(
        self,
        plays: RecordingPlays,
        camera: _Camera,
        row: HistoryRecord,
        connection: websocket_api.ActiveConnection,
        msg_id: int,
    ) -> None:
        self.play_id = secrets.token_urlsafe(16)
        self.camera = camera
        self.row = row
        self.connection = connection
        self.msg_id = msg_id
        self._plays = plays
        self._chunks: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.first = asyncio.Event()
        self.task: asyncio.Task[None] | None = None
        self.stream: Stream | None = None
        self.answered = False
        self.claimed = False
        self.subscribed = True
        self.ending: _End | None = None
        # The library's end, held while the result is not yet sent.
        self.library_end: _End | None = None
        # Set to cut a running grace short.
        self.cut = asyncio.Event()
        self.attach_timer: asyncio.TimerHandle | None = None

    def ended(self) -> bool:
        """Whether the play is ending or has ended (a call: it changes across awaits)."""
        return self.ending is not None

    async def write(self, data: bytes) -> None:
        """The library's writer: queue the bytes for the view."""
        self._chunks.put_nowait(data)
        self.first.set()

    def close(self) -> None:
        """End the view's response after what is queued."""
        self._chunks.put_nowait(None)

    def claim(self) -> bool:
        if self.claimed or self.ending is not None:
            return False
        self.claimed = True
        if self.attach_timer is not None:
            self.attach_timer.cancel()
            self.attach_timer = None
        return True

    async def read(self) -> bytes | None:
        return await self._chunks.get()

    def reader_left(self) -> None:
        self._plays.end(self, _READER_GONE)


class RecordingPlays:
    """One entry's recording plays: started by the websocket command, ended once each."""

    def __init__(self, hass: HomeAssistant, entry: EufyConfigEntry) -> None:
        self._hass = hass
        self._entry = entry
        self._registry = streaming.stream_registry(hass)
        self._plays: dict[str, _Play] = {}
        self._ends: set[asyncio.Task[None]] = set()
        self._filter: _EndedPlaysFilter | None = _hold_filter(hass)
        self.stats = PlayStats()

    def diagnostics(self) -> dict[str, Any]:
        """The counters and the plays running; no ref or play id."""
        return {
            "started": self.stats.started,
            "complete": self.stats.complete,
            "viewer_left": self.stats.viewer_left,
            "failed": dict(self.stats.failed),
            "active": len(self._plays),
        }

    async def async_play(
        self,
        connection: websocket_api.ActiveConnection,
        msg_id: int,
        camera: _Camera,
        row: HistoryRecord,
    ) -> None:
        """Play ``row`` for the subscription ``msg_id``.

        Answers the HLS URL once the library has written its first bytes and the Stream
        has started, or the error that came first; the end event follows later.
        """
        play = _Play(self, camera, row, connection, msg_id)
        self._plays[play.play_id] = play
        self._registry.plays[play.play_id] = play
        self.stats.started += 1
        # Registered before the first bytes, so a websocket that closes meanwhile
        # cancels the play too.
        connection.subscriptions[msg_id] = lambda: self.end(play, _LEFT)
        task = play.task = self._entry.async_create_background_task(
            self._hass, self._async_library(play), name=f"{DOMAIN} recording play"
        )
        first = asyncio.ensure_future(play.first.wait())
        try:
            await asyncio.wait((task, first), return_when=asyncio.FIRST_COMPLETED)
        finally:
            first.cancel()
        # The library's call ends the play by itself unless its first bytes came.
        if play.ended():
            return
        try:
            stream = create_stream(
                self._hass,
                streaming.recording_url(self._hass, play.play_id),
                {},
                DynamicStreamSettings(),
                stream_label=STREAM_LABEL,
            )
        except HomeAssistantError:
            self.end(play, _End(_FAILED, ERR_FAILED, "Home Assistant's stream is not available"))
            return
        play.stream = stream
        stream.add_provider(HLS_PROVIDER)
        await stream.start()
        # An end that came meanwhile has stopped this Stream, or stops it.
        if play.ended():
            return
        play.answered = True
        connection.send_result(msg_id)
        connection.send_message(
            websocket_api.event_message(
                msg_id, {"url": stream.endpoint_url(HLS_PROVIDER), "mime_type": HLS_MIME_TYPE}
            )
        )
        if not play.claimed:
            play.attach_timer = self._hass.loop.call_later(
                PLAY_ATTACH_TIMEOUT, self.end, play, _NOT_READ
            )
        if play.library_end is not None:
            self.end(play, play.library_end)

    async def _async_library(self, play: _Play) -> None:
        """Run the library's play into ``play``'s queue; end the play by its outcome."""
        camera = play.camera
        try:
            clip = await camera.station.async_play_recording(play.row, play.write)
        except asyncio.CancelledError:
            # Cancelled by an end, or by the entry's unload.
            if play.ending is None:
                self.end(play, _STOPPED)
            raise
        except (EufySecurityError, TimeoutError) as err:
            _LOGGER.debug(
                "Recording play of %s ended by the library (%s)",
                redact_serial(camera.device_sn),
                type(err).__name__,
            )
            answer = transfer_error(camera, err)
            end = _End(_FAILED, answer.code, str(answer), grace=True)
        except Exception:
            # Ends the play rather than leaving its subscription open.
            _LOGGER.exception("Recording play of %s failed", redact_serial(camera.device_sn))
            end = _End(_FAILED, ERR_FAILED, "The recording could not be played", grace=True)
        else:
            if not play.first.is_set():
                end = _End(_FAILED, ERR_UNAVAILABLE, "The HomeBase did not play it")
            elif clip.ended_early:
                end = _End(
                    _FAILED, ERR_CODEC_CHANGE, _PLAY_CODEC_CHANGE_MESSAGE, grace=True, quiet=True
                )
            else:
                end = _End(_COMPLETE, grace=True, quiet=True, duration=_duration(clip, play.row))
        if play.first.is_set() and not play.answered:
            # Ended once the result is out (``async_play``).
            play.library_end = end
            return
        self.end(play, end)

    @callback
    def end(self, play: _Play, end: _End) -> None:
        """End ``play`` with ``end``; on a play already ending (its event sent), a
        viewer leaving or an unload cuts the grace short."""
        if play.ending is not None:
            if end is _LEFT or end is _STOPPED:
                play.cut.set()
            return
        play.ending = end
        if end is _LEFT:
            play.subscribed = False
        task = self._hass.async_create_background_task(
            self._async_end(play), name=f"{DOMAIN} recording play end", eager_start=True
        )
        self._ends.add(task)
        task.add_done_callback(self._ends.discard)

    async def _async_end(self, play: _Play) -> None:
        """The one end of every play.

        Stops the library call and timers and ends the view's response after the bytes
        already queued, counts the end and sends its event (or the error when no result
        was sent) at once, keeps the Stream for ``PLAY_END_GRACE`` when the end has one
        (unless cut short), stops the Stream, then forgets the play (a later GET is 404).
        Until then the stream worker's error at the source's end is dropped for a
        ``quiet`` end.
        """
        end = play.ending
        assert end is not None
        if play.attach_timer is not None:
            play.attach_timer.cancel()
            play.attach_timer = None
        if play.task is not None and not play.task.done():
            play.task.cancel()
        if end.quiet and self._filter is not None:
            self._filter.ended.add(play.play_id)
        play.close()
        self._count(end)
        if play.subscribed and play.answered:
            # The subscription stays until the client unsubscribes; the unsubscribe
            # then finds the play ended and does nothing.
            play.connection.send_message(websocket_api.event_message(play.msg_id, end.event()))
        elif play.subscribed:
            # An error answer ends a subscription that never started.
            play.connection.subscriptions.pop(play.msg_id, None)
            play.connection.send_error(
                play.msg_id, end.code or ERR_FAILED, end.message or "The play ended"
            )
        try:
            stream = play.stream
            if stream is not None and end.grace:
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(PLAY_END_GRACE):
                        await play.cut.wait()
            if stream is not None:
                await stream.stop()
        finally:
            self._plays.pop(play.play_id, None)
            self._registry.plays.pop(play.play_id, None)
            if self._filter is not None:
                self._filter.ended.discard(play.play_id)

    def _count(self, end: _End) -> None:
        if end.outcome == _COMPLETE:
            self.stats.complete += 1
        elif end.outcome == _VIEWER_LEFT:
            self.stats.viewer_left += 1
        else:
            code = end.code or ERR_FAILED
            self.stats.failed[code] = self.stats.failed.get(code, 0) + 1

    async def async_stop(self) -> None:
        """End every play (``failed``; one in its grace keeps its end), wait until each
        Stream has stopped, and let go of the log filter. For the entry's unload."""
        for play in list(self._plays.values()):
            self.end(play, _STOPPED)
        if self._ends:
            await asyncio.gather(*self._ends, return_exceptions=True)
        if self._filter is not None:
            _release_filter(self._hass, self._filter)
            self._filter = None


def _duration(clip: MediaClip, row: HistoryRecord) -> float | None:
    """The played length: the clip's, else the record's, else None."""
    return clip.duration_s if clip.duration_s > 0 else row.duration_s
