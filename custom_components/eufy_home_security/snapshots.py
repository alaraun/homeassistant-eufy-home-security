"""Each camera's latest still: the detection thumbnail, its 4K trigger frame, a live keyframe.

The library owns every eufy decision here. A detection's thumbnail comes from
``Station.async_event_thumbnail``: the ``thumb_path`` the push bound to the event, else
the thumbnail of the event's own history row, found by its ``record_id``. The history
lookup, the row's camera check and the still fetch are the library's; the integration
looks nothing up itself. The trigger frame of the event's own recording comes
from a short-lived second session that is always closed
(``Station.async_event_trigger_frame``). A crop is not a thumbnail source. This module
only decides what Home Assistant shows, and when it asks:

- **Tiers.** A detection's thumbnail is shown first, when the library finds one;
  the trigger frame of the same detection then replaces it, once HA's ffmpeg decoded
  it to JPEG. The Camera image option chooses the tiers: **hd**
  (the default) runs both, **thumbnail** only the thumbnail (no playback, no decoder),
  **hd_only** only the trigger frame (no thumbnail lookup, so no thumbnail retry). A
  detection the chosen mode cannot show is ignored before it is accepted. A standalone
  camera's detection is the exception below. Each tier
  fails on its own: a thumbnail not written to the history yet still leaves the
  trigger frame. A decode that fails keeps the thumbnail. A live keyframe is only
  asked for a camera that has no image at all, with the option on, at most once per
  cooldown per camera; any detection image replaces it.
- **Own camera only.** Everything is keyed by the camera's serial, and the
  router hands over only events of a camera paired to the station that delivered
  them. A result is stored for the camera whose event asked for it, never another.
- **Order.** An event older than the newest detection a camera accepted
  (shown, waiting, in flight, or owed its thumbnail retry) is ignored, and a result is
  stored only while no newer detection replaced its event. An unauthenticated (ECB)
  push never ranks ahead of the host clock, and never against an authenticated one: it
  has its own ordering mark, it is ignored while a genuine detection waits, is in
  flight or is owed its thumbnail retry, and a genuine detection always replaces
  whatever such a push showed or queued. A later copy of
  the same detection (an enrichment) never downgrades its own trigger frame to the
  thumbnail.
- **One later thumbnail.** A thumbnail the station has not written to its
  history yet is asked for once more, after a fixed delay, only when the same
  detection's trigger frame did not land either. The retry runs on the station's
  worker, fetches only the thumbnail, never repeats, never replaces the detection's
  own trigger frame, and is dropped for a newer detection and at unload.
- **A standalone camera's detection.** A battery camera without a HomeBase reports a
  detection by cloud push with no media path and lists no recordings, so there is no
  thumbnail to look up and no trigger frame. Its two tiers are:

  - **HD** (hd and hd_only): one live keyframe at the camera's full resolution
    (``Station.async_event_image`` with ``ImageSource.LIVE``, ``full_resolution``),
    taken at once while the detection has the camera awake, decoded like a trigger
    frame and shown as the detection's HD image (``image_source`` ``detection_live``)
    at the detection's time. A woken camera climbs to its full size first, so the
    library holds the stream until the size settles: about 17 s on a T8170 for
    2880x1616, one wake. A detection while that capture is queued joins it; a later
    detection time while it runs earns one more capture after it; a capture already
    taken or failed for a detection time is not repeated. A failed capture or decode
    keeps the thumbnail tier.
  - **Thumbnail** (hd and thumbnail): the detection's event still
    (``Station.async_event_image`` with ``ImageSource.THUMBNAIL``: the camera's newest
    still, returned only when its time matches the detection's), first asked a fixed
    delay after the detection, since the camera writes it seconds after its push. A
    still not written yet keeps the current image and is asked once more after a
    further delay, then given up; a later detection's still is given up at once. It
    never replaces the detection's HD image: then it goes to the event history only,
    beside the HD image, as kind ``<detection>_thumbnail``, since the two show
    different moments. A detection while that fetch is queued, running or owed joins
    it; a later detection time earns its own later attempt.

  The push pair of one detection captures and fetches once. An unauthenticated push
  never wakes a camera. Owed attempts are dropped at unload. The same ordering rules
  apply as for any detection.
- **One media operation per station at a time.** Each station has one worker
  that fetches its cameras' stills in turn, so Home Assistant never opens more than one
  short-lived session per station (the station ends sessions past about ten). A
  camera keeps only its newest pending event: a burst of detections costs one fetch.
- **On demand.** A camera's "Capture live image" button asks for one
  live keyframe (``Station.async_camera_image`` with ``ImageSource.LIVE``), which wakes a
  battery camera. A press only queues a job on the station's worker and returns; presses
  while that camera's capture is queued or running join it, and there is no cooldown.
  Presses and detections are ordered by a per-camera arrival counter, never by clocks: a
  detection accepted after a press replaces the pressed image, and a pressed image that
  lands after a newer detection's image is not shown. An older detection's owed retry
  never replaces a pressed image. It is independent of the automatic live keyframe:
  neither its option nor its cooldown applies. The "Refresh image" button
  works the same way, one refresh per camera queued or running, and shows the image of
  the camera's newest recorded event: the thumbnail in thumbnail mode, else the
  recording's trigger frame where the station offers one
  (``Station.image_sources``), never a thumbnail fallback behind a HomeBase. A
  standalone battery camera (no HomeBase) lists no recordings and offers only its
  newest event still, the thumbnail, so that is what hd and hd_only refresh there.
  It never wakes a camera paired to a HomeBase, which holds
  the history; reading a standalone camera's still is a command to it, which wakes
  it. With no such recording the current image stays. The camera then reports the
  recording's own start as its time. A live capture that meets a running preset
  capture (``presets.py``) is refused by the library (``DeviceBusyError``) before
  anything is sent; the button pre-checks it, and a race is a DEBUG line here.
- **Nothing on the view path.** ``image_for`` returns what is cached; a view never
  waits on the station. Memory serves every view; each stored still is also
  written to Home Assistant's cache directory and restored at the next setup with
  its tier, times and ordering mark (``still_cache.py``).

Media paths are never logged, stored or exposed as attributes: an ECB push's paths
come from anyone on the LAN, and the library already validated them. Each
decision is logged at DEBUG with redacted serials, sources, reasons, sizes and durations
only, never a path, record id or dedupe key.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util
from homeassistant.util.signal_type import SignalType

from eufy_home_security import (
    CameraImage,
    DeviceBusyError,
    EufySecurityError,
    ImageSource,
    LiveStreamLimitError,
    RecordNotFoundError,
    SecurityEvent,
    Station,
    UnsupportedError,
    redact_serial,
)
from eufy_home_security.station import FULL_RESOLUTION_TIMEOUT

from . import detections, errors, still_cache
from .const import (
    CAMERA_STILL_NAME,
    DEFAULT_CAMERA_IMAGE,
    DOMAIN,
    FFMPEG_DECODE_TIMEOUT_SECONDS,
    LIVE_SNAPSHOT_COOLDOWN_SECONDS,
    LIVE_SNAPSHOT_TIMEOUT_SECONDS,
    MAX_JPEG_BYTES,
    REFRESH_THUMBNAIL_TIMEOUT_SECONDS,
    REFRESH_TRIGGER_FRAME_TIMEOUT_SECONDS,
    STANDALONE_IMAGE_RETRY_DELAY_SECONDS,
    STANDALONE_THUMBNAIL_DELAY_SECONDS,
    THUMBNAIL_RETRY_DELAY_SECONDS,
    THUMBNAIL_TIMEOUT_SECONDS,
    TRIGGER_FRAME_TIMEOUT_SECONDS,
    CameraImageMode,
)

# Home Assistant's ffmpeg binary, resolved once at module import (in HA's import
# executor): ``homeassistant.components.ffmpeg`` imports ``haffmpeg``, which a host may
# lack, and a failed import is not cached, so a per-decode import would block the loop.
_ha_ffmpeg_binary: Callable[[HomeAssistant], str] | None
try:
    from homeassistant.components.ffmpeg import get_ffmpeg_manager
except ImportError:
    _ha_ffmpeg_binary = None
else:

    def _manager_binary(hass: HomeAssistant) -> str:
        return get_ffmpeg_manager(hass).binary

    _ha_ffmpeg_binary = _manager_binary

if TYPE_CHECKING:
    from .history import EventHistory
    from .runtime import EufyConfigEntry
    from .still_cache import StillCache

_LOGGER = logging.getLogger(__name__)

# The ffmpeg arguments after the binary: one Annex-B HEVC picture on stdin, one JPEG
# on stdout, nothing else written anywhere.
FFMPEG_ARGS: Final[tuple[str, ...]] = (
    "-hide_banner",
    "-loglevel",
    "error",
    "-f",
    "hevc",
    "-i",
    "pipe:0",
    "-frames:v",
    "1",
    "-c:v",
    "mjpeg",
    "-q:v",
    "2",
    "-f",
    "image2",
    "pipe:1",
)
_JPEG_MAGIC: Final = b"\xff\xd8"
# The bare binary name, used when Home Assistant's ffmpeg integration is not set up.
_DEFAULT_FFMPEG: Final = "ffmpeg"


class StillSource(StrEnum):
    """Where a camera's shown image came from; the camera's ``image_source`` attribute."""

    THUMBNAIL = "thumbnail"
    TRIGGER_FRAME = "trigger_frame"
    LIVE = "live"
    # A standalone camera's live keyframe taken for a detection: its HD tier.
    DETECTION_LIVE = "detection_live"


def image_signal(entry_id: str, device_sn: str) -> SignalType[()]:
    """The dispatcher signal telling one camera entity that its image changed.

    The name is never logged or stored.
    """
    return SignalType(f"{DOMAIN}_camera_image_{entry_id}_{device_sn}")


def ffmpeg_command(hass: HomeAssistant) -> list[str]:
    """The decoder command: Home Assistant's ffmpeg binary, else ``ffmpeg``.

    Nothing is imported here: the ffmpeg integration's module was resolved once, at
    this module's import. Its manager exists only once the ffmpeg integration has set
    up (``after_dependencies`` orders, never loads it), and ``get_ffmpeg_manager``
    raises ``ValueError`` before then. Either absence falls back to the bare name,
    whose absence in turn is a decode that fails: the thumbnail stays. Tests replace
    this module attribute.
    """
    if _ha_ffmpeg_binary is None:
        return [_DEFAULT_FFMPEG]
    try:
        return [_ha_ffmpeg_binary(hass)]
    except ValueError:
        return [_DEFAULT_FFMPEG]


async def async_open_media[T](
    station: Station,
    yield_media: Callable[[str], Awaitable[None]],
    call: Callable[[bool], Awaitable[T]],
) -> T:
    """Run ``call(wait)``, a library call that opens live video on ``station``.

    A standalone camera has one stream, so its live view ends first (``yield_media``)
    and the call runs once, ``wait=True``. On a HomeBase the call runs beside the live
    views, on an extra session, ``wait=False``; only past the session budget
    (``LiveStreamLimitError``) does the view holding the station session's slot end,
    and the call runs once more, ``wait=True``, on the freed slot. ``call`` passes
    ``wait`` to the library method. Any other error propagates.
    """
    if station.is_standalone:
        await yield_media(station.serial)
        return await call(True)
    try:
        return await call(False)
    except LiveStreamLimitError:
        _LOGGER.debug(
            "Live open on %s: session budget used up, ending the live view on the slot",
            redact_serial(station.serial),
        )
    await yield_media(station.serial)
    return await call(True)


async def async_hevc_to_jpeg(command: Sequence[str], hevc: bytes, timeout: float) -> bytes | None:
    """Decode one Annex-B HEVC keyframe to JPEG with ``command``; None when it cannot.

    An asyncio subprocess, never a blocking one. No decoder, a non-zero exit, a
    timeout, an output that is not a JPEG or is larger than ``MAX_JPEG_BYTES`` all
    return None; a cancelled decode never leaves the process running.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *command,
            *FFMPEG_ARGS,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        # Not found, not permitted, or not executable on this architecture.
        _LOGGER.debug("No usable ffmpeg to decode a camera keyframe")
        return None
    try:
        async with asyncio.timeout(timeout):
            out, _ = await proc.communicate(hevc)
    except TimeoutError:
        _LOGGER.debug("ffmpeg did not decode a camera keyframe within %s s", timeout)
        return None
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
    if proc.returncode != 0 or not out.startswith(_JPEG_MAGIC) or len(out) > MAX_JPEG_BYTES:
        _LOGGER.debug("ffmpeg could not decode a camera keyframe (exit %s)", proc.returncode)
        return None
    return out


def camera_image_mode(value: object) -> CameraImageMode:
    """The stored Camera image option as a mode; hd for None or an unknown value.

    A value from an older or newer version, or a hand edit, must never break setup.
    """
    if isinstance(value, str):
        with contextlib.suppress(ValueError):
            return CameraImageMode(value)
    return DEFAULT_CAMERA_IMAGE


def _monotonic() -> float:
    """The cooldown clock; tests replace this module attribute."""
    return time.monotonic()


def _utcnow() -> datetime:
    """The clock ``image_updated`` reads; tests replace this module attribute."""
    return dt_util.utcnow()


@dataclass(slots=True)
class _CameraStill:
    """What one camera shows and what it still waits for."""

    image: bytes | None = None
    source: StillSource | None = None
    # The shown detection's time (ms) and occurrence key; None for a live image.
    event_time_ms: int | None = None
    occurrence: str | None = None
    # Whether the shown image came from an authenticated event (a live one counts).
    shown_authenticated: bool = True
    pending: SecurityEvent | None = None
    pending_time_ms: int = 0
    # The newest authenticated detection this camera accepted, whether it is shown,
    # pending, in flight or owed a thumbnail retry: its ordering time and
    # occurrence key. An unauthenticated (ECB) push has its own mark, so a forgery
    # stamped "now" never ranks against a genuine detection still in transit.
    newest_ms: int | None = None
    newest_occurrence: str | None = None
    newest_unauthenticated_ms: int | None = None
    newest_unauthenticated_occurrence: str | None = None
    # Whether the worker is working an authenticated detection of this camera now.
    in_flight_authenticated: bool = False
    live_queued: bool = False
    last_live: float | None = None
    # The detection owed one later thumbnail attempt, its ordering time, the
    # occurrence whose single retry was already scheduled (never cleared, so a copy
    # of it never schedules another), and the timer's cancel handle.
    retry_event: SecurityEvent | None = None
    retry_time_ms: int = 0
    retry_occurrence: str | None = None
    cancel_retry: CALLBACK_TYPE | None = None
    # ``seq`` is a per-camera arrival counter, bumped at every accepted
    # detection and every press that is not ignored. Presses and detections are ordered
    # by it, never by clocks: a detection's time is the station's clock and a press is
    # the host's. The waiting detection's, the retry's and the shown image's counters,
    # whether the shown image came from a press, and the capture's coalescing state.
    seq: int = 0
    pending_seq: int = 0
    retry_seq: int = 0
    shown_seq: int = 0
    shown_on_demand: bool = False
    capture_queued: bool = False
    capture_seq: int = 0
    refresh_queued: bool = False
    refresh_seq: int = 0
    # A standalone camera's detection owed its images: the newest such detection, the
    # time its images must not predate, and its arrival counter.
    standalone_event: SecurityEvent | None = None
    standalone_time_ms: int = 0
    standalone_seq: int = 0
    # Its thumbnail: whether a fetch is queued or running, whether the time already
    # used its one later fetch, and the timer of the owed (first or later) fetch.
    standalone_queued: bool = False
    standalone_retried: bool = False
    cancel_standalone_thumbnail: CALLBACK_TYPE | None = None
    # Its live keyframe: whether a capture is queued, the detection time the running
    # one serves, whether a later detection is owed one more, and the newest
    # detection time a capture was taken (or failed) for.
    standalone_live_queued: bool = False
    standalone_live_running_ms: int | None = None
    standalone_live_again: bool = False
    standalone_live_tried_ms: int | None = None
    # The start of the recording a refreshed image came from; None for any other image.
    recorded_time: datetime | None = None
    # When the shown image was stored (UTC), strictly increasing per camera.
    stored_time: datetime | None = None


@dataclass(slots=True)
class _StationWorker:
    """One station's queue of cameras to fetch for, and the task working it."""

    station: Station
    queue: deque[tuple[str, str]]
    task: asyncio.Task[None] | None = None


# The smallest step ``image_updated`` shows (it is rendered in milliseconds).
_STORED_TIME_STEP: Final = timedelta(milliseconds=1)

_EVENT_JOB: Final = "event"
_LIVE_JOB: Final = "live"
_THUMBNAIL_RETRY_JOB: Final = "thumbnail_retry"
_CAPTURE_JOB: Final = "capture_live"
_REFRESH_JOB: Final = "refresh"
_STANDALONE_JOB: Final = "standalone_event"
_STANDALONE_LIVE_JOB: Final = "standalone_live"
# The library's RecordNotFoundError text for a standalone still the camera has not
# written yet (retry later), as against one a later detection replaced (give up).
_STILL_NOT_WRITTEN: Final = "not written yet"


class SnapshotManager:
    """The stills of one entry's cameras, fetched by one worker per station."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: EufyConfigEntry,
        *,
        live_snapshot: bool,
        camera_image: CameraImageMode,
        yield_media: Callable[[str], Awaitable[None]],
        cache: StillCache | None = None,
        history: EventHistory | None = None,
    ) -> None:
        """``yield_media`` ends the live view holding a station's media slot (``async_open_media``).

        A coroutine function taking a **station serial**. Injected so this module
        names no client type, never touches
        ``entry.runtime_data`` (which does not exist yet when a detection during setup
        queues the first job), and never imports ``streaming.py``: the dependency runs
        one way only. ``cache`` receives every stored still (``still_cache.py``), and so
        does ``history`` (``history.py``), with the still's own moment and kind.
        """
        self._hass = hass
        self._entry = entry
        self._cache = cache
        self._history = history
        self._live_snapshot = live_snapshot
        self._camera_image = camera_image
        self._yield_media = yield_media
        self._cameras: dict[str, _CameraStill] = {}
        self._workers: dict[str, _StationWorker] = {}
        self._stopped = False
        # Per camera serial, the last live keyframe skip reason logged (one line per reason).
        self._live_skip_logged: dict[str, str] = {}
        _LOGGER.debug(
            "Snapshot manager started: camera image %s, live snapshot %s",
            camera_image.value,
            live_snapshot,
        )

    @property
    def live_snapshot(self) -> bool:
        """Whether a camera with no image may ask for a live keyframe."""
        return self._live_snapshot

    @property
    def camera_image(self) -> CameraImageMode:
        """Which tiers a detection's stills use."""
        return self._camera_image

    def refresh_source_for(self, station: Station, device_sn: str) -> ImageSource:
        """What a "Refresh image" press shows for this camera.

        The thumbnail in thumbnail mode; otherwise the trigger frame when the station
        offers one for the camera (``Station.image_sources``), else the thumbnail: a
        standalone camera lists no recordings, so its newest event still is the only
        refreshed image, in hd and hd_only alike. A device the station cannot address
        falls back to the trigger frame, and the worker's fetch raises.
        """
        if self._camera_image is CameraImageMode.THUMBNAIL:
            return ImageSource.THUMBNAIL
        try:
            offered = station.image_sources(device_sn)
        except UnsupportedError:
            return ImageSource.TRIGGER_FRAME
        if ImageSource.TRIGGER_FRAME in offered:
            return ImageSource.TRIGGER_FRAME
        return ImageSource.THUMBNAIL

    @property
    def busy(self) -> bool:
        """Whether any station worker is fetching or has work queued."""
        return any(w.task is not None and not w.task.done() for w in self._workers.values())

    def image_for(self, device_sn: str) -> bytes | None:
        """The JPEG a camera shows now; None before it has one. Never fetches."""
        camera = self._cameras.get(device_sn)
        return camera.image if camera is not None else None

    async def small_image_for(self, device_sn: str) -> bytes | None:
        """The small copy of the JPEG a camera shows; None before one, without a cache,
        or when it does not decode. Never fetches from the station."""
        image = self.image_for(device_sn)
        if image is None or self._cache is None:
            return None
        return await self._cache.async_small(
            still_cache.still_key(device_sn, CAMERA_STILL_NAME), image
        )

    def source_for(self, device_sn: str) -> StillSource | None:
        """Where the camera's shown image came from; None before it has one."""
        camera = self._cameras.get(device_sn)
        return camera.source if camera is not None else None

    def event_time_for(self, device_sn: str) -> int | None:
        """The shown detection's own time in ms; None for a live image or none."""
        camera = self._cameras.get(device_sn)
        return camera.event_time_ms if camera is not None else None

    def recorded_time_for(self, device_sn: str) -> datetime | None:
        """The start of the recording a refreshed image shows; None for any other image."""
        camera = self._cameras.get(device_sn)
        return camera.recorded_time if camera is not None else None

    def stored_time_for(self, device_sn: str) -> datetime | None:
        """When the shown image was stored; differs for every stored image. None before one."""
        camera = self._cameras.get(device_sn)
        return camera.stored_time if camera is not None else None

    @callback
    def async_request_event(self, station: Station, event: SecurityEvent) -> None:
        """Fetch the stills of a camera's detection, replacing any older pending one.

        The router calls this only for a detection of a camera paired to ``station``
        that the library can find a still for (``detections.snapshot_wanted``).
        """
        device_sn = event.device_sn
        if device_sn is None:
            return
        if self._stopped:
            _ignored(device_sn, "snapshot manager stopped")
            return
        # Before any camera state or ordering mark: a detection the chosen mode cannot
        # show is never accepted. The same library attributes snapshot_wanted reads.
        if self._camera_image is CameraImageMode.HD_ONLY and event.video_path is None:
            _ignored(device_sn, "no recording, camera image is HD only")
            return
        if (
            self._camera_image is CameraImageMode.THUMBNAIL
            and event.record_id is None
            and event.thumb_path is None
        ):
            _ignored(device_sn, "no thumbnail to look up, camera image is thumbnail only")
            return
        camera = self._cameras.setdefault(device_sn, _CameraStill())
        when = event.event_time_ms if event.event_time_ms is not None else _host_now_ms()
        authenticated = event.authenticated
        key = event.dedupe_key
        if not authenticated:
            # An ECB push's claimed time is anyone's on the LAN: never let it rank ahead
            # of the host clock, or a future-dated forgery would hold every genuine
            # detection back until that time passes.
            when = min(when, _host_now_ms())
            # A genuine detection is in flight, waiting or owed its thumbnail retry: an
            # unauthenticated push never displaces or cancels that work.
            if camera.in_flight_authenticated:
                _ignored(
                    device_sn,
                    "unauthenticated push while a genuine detection is in flight",
                )
                return
            if camera.pending is not None and camera.pending.authenticated:
                _ignored(device_sn, "unauthenticated push while a genuine detection is waiting")
                return
            if camera.retry_event is not None and camera.retry_event.authenticated:
                _ignored(
                    device_sn,
                    "unauthenticated push while a genuine detection is owed its thumbnail retry",
                )
                return
        same_shown = key is not None and key == camera.occurrence
        if (
            not same_shown
            and camera.event_time_ms is not None
            and (camera.shown_authenticated or not authenticated)
            and when < camera.event_time_ms
        ):
            _ignored(device_sn, "older than the shown detection")
            return
        if (
            camera.pending is not None
            and (camera.pending.authenticated or not authenticated)
            and when < camera.pending_time_ms
        ):
            _ignored(device_sn, "older than the waiting detection")
            return
        same_newest = key is not None and key == camera.newest_occurrence
        if not same_newest and camera.newest_ms is not None and when < camera.newest_ms:
            # Older than a genuine detection already accepted: one in flight, or one
            # that showed nothing and may be owed a thumbnail retry.
            _ignored(device_sn, "older than a genuine detection already accepted")
            return
        if not authenticated:
            same_unauthenticated = (
                key is not None and key == camera.newest_unauthenticated_occurrence
            )
            newest_unauthenticated = camera.newest_unauthenticated_ms
            if (
                not same_unauthenticated
                and newest_unauthenticated is not None
                and when < newest_unauthenticated
            ):
                _ignored(device_sn, "older than an unauthenticated push already accepted")
                return
            if not same_unauthenticated:
                camera.newest_unauthenticated_occurrence = key
            camera.newest_unauthenticated_ms = (
                when if newest_unauthenticated is None else max(newest_unauthenticated, when)
            )
        else:
            # Only an authenticated detection moves the genuine mark.
            if not same_newest:
                camera.newest_occurrence = key
            camera.newest_ms = when if camera.newest_ms is None else max(camera.newest_ms, when)
        if key is None or key != camera.retry_occurrence:
            # A newer detection works for itself.
            self._cancel_retry(camera, device_sn, "newer detection")
        camera.seq += 1
        camera.pending_seq = camera.seq
        camera.pending = event
        camera.pending_time_ms = when
        _LOGGER.debug(
            "Snapshot request for %s accepted, authenticated %s",
            redact_serial(device_sn),
            authenticated,
        )
        self._enqueue(station, _EVENT_JOB, device_sn)

    @callback
    def async_request_standalone_event(self, station: Station, event: SecurityEvent) -> None:
        """The images of a standalone camera's detection.

        The router calls this only for a catalogued detection of a camera on a
        standalone ``station`` with no thumbnail to look up
        (``detections.standalone_image_wanted``). hd and hd_only queue a live keyframe at
        once; hd and thumbnail owe the camera's newest event still after
        ``STANDALONE_THUMBNAIL_DELAY_SECONDS``. Both wake the camera. A detection while
        either is queued, running or owed joins it and raises the time its image must
        not predate. An unauthenticated push never wakes a camera.
        """
        device_sn = event.device_sn
        if device_sn is None:
            return
        if self._stopped:
            _ignored(device_sn, "snapshot manager stopped")
            return
        if not event.authenticated:
            _ignored(device_sn, "an unauthenticated push never wakes a standalone camera")
            return
        camera = self._cameras.setdefault(device_sn, _CameraStill())
        when = event.event_time_ms if event.event_time_ms is not None else _host_now_ms()
        key = event.dedupe_key
        if (
            camera.event_time_ms is not None
            and camera.shown_authenticated
            and when <= camera.event_time_ms
        ):
            # The newest still is at least as new as this detection: a second push of a
            # shown detection, or an older one.
            _ignored(device_sn, "not newer than the shown detection")
            return
        same_newest = key is not None and key == camera.newest_occurrence
        if not same_newest and camera.newest_ms is not None and when < camera.newest_ms:
            _ignored(device_sn, "older than a genuine detection already accepted")
            return
        if not same_newest:
            camera.newest_occurrence = key
        camera.newest_ms = when if camera.newest_ms is None else max(camera.newest_ms, when)
        if key is None or key != camera.retry_occurrence:
            self._cancel_retry(camera, device_sn, "newer detection")
        camera.seq += 1
        camera.standalone_seq = camera.seq
        if camera.standalone_event is None or when > camera.standalone_time_ms:
            # A later detection time earns its own later attempt.
            camera.standalone_time_ms = when
            camera.standalone_retried = False
        camera.standalone_event = event
        if self._camera_image is not CameraImageMode.THUMBNAIL:
            self._want_standalone_live(station, camera, device_sn, when)
        if self._camera_image is not CameraImageMode.HD_ONLY:
            self._want_standalone_thumbnail(station, camera, device_sn)

    def _want_standalone_live(
        self, station: Station, camera: _CameraStill, device_sn: str, when: int
    ) -> None:
        """Queue a standalone camera's live keyframe for a detection, unless one serves it."""
        serial = redact_serial(device_sn)
        if camera.standalone_live_queued:
            _LOGGER.debug("Detection HD image for %s: joined the queued capture", serial)
            return
        running = camera.standalone_live_running_ms
        if running is not None:
            if when > running:
                camera.standalone_live_again = True
                _LOGGER.debug(
                    "Detection HD image for %s: a later detection, one more capture owed", serial
                )
            else:
                _LOGGER.debug("Detection HD image for %s: joined the running capture", serial)
            return
        tried = camera.standalone_live_tried_ms
        if tried is not None and when <= tried:
            _LOGGER.debug("Detection HD image for %s: already captured for this time", serial)
            return
        camera.standalone_live_queued = True
        _LOGGER.debug("Detection HD image for %s: capture queued, wakes the camera", serial)
        self._enqueue(station, _STANDALONE_LIVE_JOB, device_sn)

    def _want_standalone_thumbnail(
        self, station: Station, camera: _CameraStill, device_sn: str
    ) -> None:
        """Owe a standalone camera's newest event still after the first-attempt delay."""
        serial = redact_serial(device_sn)
        if camera.standalone_queued:
            _LOGGER.debug(
                "Standalone event image for %s: joined the queued or running fetch", serial
            )
            return
        if camera.cancel_standalone_thumbnail is not None:
            _LOGGER.debug("Standalone event image for %s: joined the owed fetch", serial)
            return
        self._schedule_standalone_thumbnail(
            station, camera, device_sn, STANDALONE_THUMBNAIL_DELAY_SECONDS
        )
        _LOGGER.debug(
            "Standalone event image for %s: fetching in %s s",
            serial,
            STANDALONE_THUMBNAIL_DELAY_SECONDS,
        )

    def _schedule_standalone_thumbnail(
        self, station: Station, camera: _CameraStill, device_sn: str, delay: float
    ) -> None:
        camera.cancel_standalone_thumbnail = async_call_later(
            self._hass,
            delay,
            functools.partial(self._async_standalone_thumbnail_due, station, device_sn),
        )

    @callback
    def async_request_live(self, station: Station, device_sn: str) -> None:
        """Ask once for a live keyframe of a camera with no image, within the cooldown."""
        if self._stopped:
            return
        if not self._live_snapshot:
            # Before any camera state is created: the option off keeps none.
            self._live_skipped(device_sn, "option off")
            return
        camera = self._cameras.setdefault(device_sn, _CameraStill())
        if camera.image is not None:
            self._live_skipped(device_sn, "camera has an image")
            return
        if camera.pending is not None or camera.standalone_event is not None:
            self._live_skipped(device_sn, "a detection waits")
            return
        if camera.live_queued:
            self._live_skipped(device_sn, "already queued")
            return
        now = _monotonic()
        if camera.last_live is not None and now - camera.last_live < LIVE_SNAPSHOT_COOLDOWN_SECONDS:
            self._live_skipped(
                device_sn,
                "cooldown",
                f", {LIVE_SNAPSHOT_COOLDOWN_SECONDS - (now - camera.last_live):.0f} s left",
            )
            return
        camera.last_live = now
        camera.live_queued = True
        self._live_skip_logged.pop(device_sn, None)
        _LOGGER.debug("Live keyframe for %s requested", redact_serial(device_sn))
        self._enqueue(station, _LIVE_JOB, device_sn)

    @callback
    def async_request_capture(self, station: Station, device_sn: str) -> None:
        """A "Capture live image" press: one live keyframe, coalesced, no cooldown.

        Only queues a job on the station's worker and returns. A press while this
        camera's capture is queued or running joins that job, and still counts as the
        newest press, so a detection accepted in between does not outrank it. Never
        reads or writes the automatic live keyframe's option, cooldown or flags.
        """
        serial = redact_serial(device_sn)
        if self._stopped:
            _LOGGER.debug(
                "Capture live image for %s pressed, ignored: snapshot manager stopped", serial
            )
            return
        camera = self._cameras.setdefault(device_sn, _CameraStill())
        camera.seq += 1
        camera.capture_seq = camera.seq
        if camera.capture_queued:
            _LOGGER.debug(
                "Capture live image for %s pressed, coalesced into the queued or running capture",
                serial,
            )
            return
        camera.capture_queued = True
        _LOGGER.debug("Capture live image for %s pressed, queued", serial)
        self._enqueue(station, _CAPTURE_JOB, device_sn)

    @callback
    def async_request_refresh(self, station: Station, device_sn: str) -> None:
        """A "Refresh image" press: the newest recording's image, coalesced, no cooldown.

        Only queues a job on the station's worker and returns, as a capture press does.
        """
        serial = redact_serial(device_sn)
        if self._stopped:
            _LOGGER.debug("Refresh image for %s pressed, ignored: snapshot manager stopped", serial)
            return
        camera = self._cameras.setdefault(device_sn, _CameraStill())
        camera.seq += 1
        camera.refresh_seq = camera.seq
        if camera.refresh_queued:
            _LOGGER.debug(
                "Refresh image for %s pressed, coalesced into the queued or running refresh",
                serial,
            )
            return
        camera.refresh_queued = True
        _LOGGER.debug(
            "Refresh image for %s pressed (%s), queued",
            serial,
            self.refresh_source_for(station, device_sn).value,
        )
        self._enqueue(station, _REFRESH_JOB, device_sn)

    def _live_skipped(self, device_sn: str, reason: str, detail: str = "") -> None:
        """Log a live keyframe skip once per camera per reason, never once per view."""
        if self._live_skip_logged.get(device_sn) == reason:
            return
        self._live_skip_logged[device_sn] = reason
        _LOGGER.debug(
            "Live keyframe for %s skipped: %s%s (logged once until it changes)",
            redact_serial(device_sn),
            reason,
            detail,
        )

    async def async_stop(self) -> None:
        """Stop every worker; a request after this is ignored."""
        self._stopped = True
        _LOGGER.debug(
            "Snapshot manager stopping: %d station worker(s) running, %d thumbnail retry(ies) owed",
            sum(1 for w in self._workers.values() if w.task is not None and not w.task.done()),
            sum(
                1
                for c in self._cameras.values()
                if c.retry_event is not None or c.cancel_retry is not None
            ),
        )
        for device_sn, camera in self._cameras.items():
            self._cancel_retry(camera, device_sn, "unload")
            self._drop_standalone(camera, device_sn, "unload")
        tasks = [w.task for w in self._workers.values() if w.task is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def _enqueue(self, station: Station, job: str, device_sn: str) -> None:
        worker = self._workers.get(station.serial)
        if worker is None:
            worker = _StationWorker(station, deque())
            self._workers[station.serial] = worker
        if (job, device_sn) not in worker.queue:
            worker.queue.append((job, device_sn))
        if worker.task is None or worker.task.done():
            worker.task = self._entry.async_create_background_task(
                self._hass,
                self._async_work(worker),
                name=f"{DOMAIN} camera stills {redact_serial(station.serial)}",
            )

    async def _async_work(self, worker: _StationWorker) -> None:
        """Work one station's queue until it is empty: one media operation at a time."""
        while worker.queue and not self._stopped:
            job, device_sn = worker.queue.popleft()
            _LOGGER.debug(
                "Worker for %s: %s job for %s, %d more queued",
                redact_serial(worker.station.serial),
                job,
                redact_serial(device_sn),
                len(worker.queue),
            )
            camera = self._cameras[device_sn]
            # A standalone camera carries one stream: its live view ends before any
            # job. A HomeBase job runs beside live views (async_open_media). A failed
            # yield still runs the job, whose own cleanup clears its queued flag.
            if worker.station.is_standalone:
                try:
                    await self._yield_media(worker.station.serial)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _LOGGER.exception("Unexpected error ending a live view before a camera still")
            try:
                if job == _EVENT_JOB:
                    event = camera.pending
                    when = camera.pending_time_ms
                    seq = camera.pending_seq
                    camera.pending = None
                    if event is not None:
                        camera.in_flight_authenticated = event.authenticated
                        try:
                            await self._async_event_stills(
                                worker.station, camera, device_sn, event, when, seq
                            )
                        finally:
                            camera.in_flight_authenticated = False
                elif job == _THUMBNAIL_RETRY_JOB:
                    event = camera.retry_event
                    when = camera.retry_time_ms
                    seq = camera.retry_seq
                    camera.retry_event = None
                    camera.retry_time_ms = 0
                    if event is not None:
                        camera.in_flight_authenticated = event.authenticated
                        try:
                            await self._async_thumbnail_retry(
                                worker.station, camera, device_sn, event, when, seq
                            )
                        finally:
                            camera.in_flight_authenticated = False
                elif job == _LIVE_JOB:
                    try:
                        await self._async_live(worker.station, camera, device_sn)
                    finally:
                        camera.live_queued = False
                elif job == _CAPTURE_JOB:
                    try:
                        await self._async_capture(worker.station, camera, device_sn)
                    finally:
                        camera.capture_queued = False
                elif job == _REFRESH_JOB:
                    try:
                        await self._async_refresh(worker.station, camera, device_sn)
                    finally:
                        camera.refresh_queued = False
                elif job == _STANDALONE_JOB:
                    camera.in_flight_authenticated = True
                    try:
                        await self._async_standalone_image(worker.station, camera, device_sn)
                    finally:
                        camera.in_flight_authenticated = False
                        camera.standalone_queued = False
                        _settle_standalone(camera)
                elif job == _STANDALONE_LIVE_JOB:
                    camera.standalone_live_queued = False
                    when = camera.standalone_time_ms
                    camera.standalone_live_running_ms = when
                    camera.in_flight_authenticated = True
                    try:
                        await self._async_standalone_live(worker.station, camera, device_sn, when)
                    finally:
                        camera.in_flight_authenticated = False
                        self._standalone_live_done(worker.station, camera, device_sn, when)
            except asyncio.CancelledError:
                raise
            except DeviceBusyError:
                # A race the button's pre-check missed: the library refused before
                # sending, so the camera keeps what it shows.
                _LOGGER.debug(
                    "A camera still of %s: camera busy with a preset capture, nothing sent",
                    redact_serial(device_sn),
                )
            except (EufySecurityError, TimeoutError) as err:
                # Keep what the camera shows; the next detection tries again.
                _LOGGER.debug(
                    "A camera still of %s could not be fetched (%s)",
                    redact_serial(device_sn),
                    errors.failure_reason(err),
                )
            except Exception:
                # One unexpected failure must not end the station's worker.
                _LOGGER.exception("Unexpected error fetching a camera still")

    async def _async_event_stills(
        self,
        station: Station,
        camera: _CameraStill,
        device_sn: str,
        event: SecurityEvent,
        when: int,
        seq: int,
    ) -> None:
        """The thumbnail, then the trigger frame, of one detection.

        ``when`` is the ordering time the manager gave the detection when it accepted
        it, and ``seq`` its arrival counter; neither is recomputed here. Each
        tier fails on its own.
        """
        occurrence = event.dedupe_key
        thumbnail_not_written = False
        serial = redact_serial(device_sn)
        if self._camera_image is CameraImageMode.HD_ONLY:
            # No thumbnail tier, so nothing is owed a retry either.
            _LOGGER.debug("Thumbnail of %s: skipped, camera image is HD only", serial)
        elif self._shows_trigger_frame_of(camera, occurrence):
            _LOGGER.debug("Thumbnail of %s: skipped, its trigger frame is already shown", serial)
        else:
            _LOGGER.debug("Thumbnail of %s: looking up", serial)
            start = time.monotonic()
            try:
                async with asyncio.timeout(THUMBNAIL_TIMEOUT_SECONDS):
                    still = await station.async_event_thumbnail(event)
            except UnsupportedError:
                _LOGGER.debug(
                    "Thumbnail of %s: none to look up, in %.1f s", serial, _elapsed(start)
                )
            except RecordNotFoundError as err:
                # Not written yet: the trigger frame is still fetched; a later attempt
                # is owed only if it does not land either.
                thumbnail_not_written = True
                _LOGGER.debug(
                    "Thumbnail of %s: not in the station's history yet (%s), in %.1f s",
                    serial,
                    type(err).__name__,
                    _elapsed(start),
                )
            except (EufySecurityError, TimeoutError, ValueError) as err:
                # ValueError: the library's impossible-day record_id, which a
                # LAN push can carry.
                _LOGGER.debug(
                    "Thumbnail of %s: failed (%s), in %.1f s",
                    serial,
                    errors.failure_reason(err),
                    _elapsed(start),
                )
            else:
                if not still.is_image:
                    _LOGGER.debug(
                        "Thumbnail of %s: not an image, ignored, in %.1f s",
                        serial,
                        _elapsed(start),
                    )
                elif not self._still_current(camera, occurrence, when, event.authenticated, seq):
                    _LOGGER.debug(
                        "Thumbnail of %s: superseded by a newer detection, in %.1f s",
                        serial,
                        _elapsed(start),
                    )
                else:
                    _LOGGER.debug(
                        "Thumbnail of %s: shown (%d bytes), in %.1f s",
                        serial,
                        len(still.data),
                        _elapsed(start),
                    )
                    self._store(
                        camera,
                        device_sn,
                        still.data,
                        StillSource.THUMBNAIL,
                        when,
                        occurrence,
                        authenticated=event.authenticated,
                        kind=_event_kind(event),
                        record_id=event.record_id,
                        seq=seq,
                    )
        if self._camera_image is CameraImageMode.THUMBNAIL:
            # No playback and no decoder; a thumbnail not written yet is owed its retry.
            _LOGGER.debug("Trigger frame of %s: skipped, camera image is thumbnail only", serial)
        elif event.video_path is None:
            _LOGGER.debug("Trigger frame of %s: skipped, no recording", serial)
        elif self._shows_trigger_frame_of(camera, occurrence):
            _LOGGER.debug("Trigger frame of %s: skipped, already shown", serial)
        elif not self._still_current(camera, occurrence, when, event.authenticated, seq):
            _LOGGER.debug("Trigger frame of %s: skipped, a newer detection replaced it", serial)
        elif self._newer_pending(camera, occurrence, when, event.authenticated):
            # A newer detection is waiting: spend the session on that one.
            _LOGGER.debug("Trigger frame of %s: skipped, a newer detection waits", serial)
            return
        else:
            _LOGGER.debug("Trigger frame of %s: fetching", serial)
            start = time.monotonic()
            try:
                async with asyncio.timeout(TRIGGER_FRAME_TIMEOUT_SECONDS):
                    hevc = await station.async_event_trigger_frame(event, trailing_frames=0)
            except (EufySecurityError, TimeoutError) as err:
                _LOGGER.debug(
                    "Trigger frame of %s: failed (%s), in %.1f s",
                    serial,
                    errors.failure_reason(err),
                    _elapsed(start),
                )
            else:
                _LOGGER.debug(
                    "Trigger frame of %s: fetched %d bytes in %.1f s, decode started",
                    serial,
                    len(hevc),
                    _elapsed(start),
                )
                start = time.monotonic()
                jpeg = await async_hevc_to_jpeg(
                    ffmpeg_command(self._hass), hevc, FFMPEG_DECODE_TIMEOUT_SECONDS
                )
                current = self._still_current(camera, occurrence, when, event.authenticated, seq)
                _log_decode(device_sn, jpeg, start, current=current)
                if jpeg is not None and current:
                    self._store(
                        camera,
                        device_sn,
                        jpeg,
                        StillSource.TRIGGER_FRAME,
                        when,
                        occurrence,
                        authenticated=event.authenticated,
                        kind=_event_kind(event),
                        record_id=event.record_id,
                        seq=seq,
                    )
        if (
            thumbnail_not_written
            and not self._shows_trigger_frame_of(camera, occurrence)
            and camera.pending is None
            and self._still_current(camera, occurrence, when, event.authenticated, seq)
            and (occurrence is None or camera.retry_occurrence != occurrence)
        ):
            self._schedule_thumbnail_retry(station, camera, device_sn, event, when, seq)

    def _schedule_thumbnail_retry(
        self,
        station: Station,
        camera: _CameraStill,
        device_sn: str,
        event: SecurityEvent,
        when: int,
        seq: int,
    ) -> None:
        """Owe the detection one later thumbnail attempt, after the retry delay."""
        self._cancel_retry(camera, device_sn, "rescheduled")
        camera.retry_event = event
        camera.retry_time_ms = when
        camera.retry_seq = seq
        camera.retry_occurrence = event.dedupe_key
        camera.cancel_retry = async_call_later(
            self._hass,
            THUMBNAIL_RETRY_DELAY_SECONDS,
            functools.partial(self._async_retry_due, station, device_sn),
        )
        _LOGGER.debug(
            "Thumbnail retry for %s scheduled in %s s",
            redact_serial(device_sn),
            THUMBNAIL_RETRY_DELAY_SECONDS,
        )

    @callback
    def _async_retry_due(self, station: Station, device_sn: str, _now: datetime) -> None:
        """The retry delay ran out: queue the thumbnail attempt on the station's worker."""
        camera = self._cameras.get(device_sn)
        if camera is None:
            return
        camera.cancel_retry = None
        if self._stopped or camera.retry_event is None:
            _LOGGER.debug("Thumbnail retry for %s due: nothing owed", redact_serial(device_sn))
            return
        if camera.pending is not None:
            # A detection is waiting; it fetches for itself.
            camera.retry_event = None
            _LOGGER.debug(
                "Thumbnail retry for %s due: dropped, a detection waits", redact_serial(device_sn)
            )
            return
        _LOGGER.debug("Thumbnail retry for %s due: queued", redact_serial(device_sn))
        self._enqueue(station, _THUMBNAIL_RETRY_JOB, device_sn)

    async def _async_thumbnail_retry(
        self,
        station: Station,
        camera: _CameraStill,
        device_sn: str,
        event: SecurityEvent,
        when: int,
        seq: int,
    ) -> None:
        """The one later thumbnail attempt of a detection; never rescheduled."""
        occurrence = event.dedupe_key
        serial = redact_serial(device_sn)
        if camera.shown_on_demand and seq <= camera.shown_seq:
            _LOGGER.debug("Retried thumbnail of %s: skipped, an on-demand image is newer", serial)
            return
        if self._shows_trigger_frame_of(camera, occurrence):
            _LOGGER.debug("Retried thumbnail of %s: skipped, its trigger frame is shown", serial)
            return
        if not self._still_current(camera, occurrence, when, event.authenticated, seq):
            _LOGGER.debug("Retried thumbnail of %s: skipped, a newer detection replaced it", serial)
            return
        if self._newer_pending(camera, occurrence, when, event.authenticated):
            _LOGGER.debug("Retried thumbnail of %s: skipped, a newer detection waits", serial)
            return
        _LOGGER.debug("Retried thumbnail of %s: looking up", serial)
        start = time.monotonic()
        try:
            async with asyncio.timeout(THUMBNAIL_TIMEOUT_SECONDS):
                still = await station.async_event_thumbnail(event)
        except RecordNotFoundError as err:
            _LOGGER.debug(
                "Retried thumbnail of %s: not in the station's history yet (%s), in %.1f s",
                serial,
                type(err).__name__,
                _elapsed(start),
            )
            return
        except (EufySecurityError, TimeoutError, ValueError) as err:
            _LOGGER.debug(
                "Retried thumbnail of %s: failed (%s), in %.1f s",
                serial,
                errors.failure_reason(err),
                _elapsed(start),
            )
            return
        if not still.is_image:
            _LOGGER.debug(
                "Retried thumbnail of %s: not an image, ignored, in %.1f s",
                serial,
                _elapsed(start),
            )
        elif not self._still_current(
            camera, occurrence, when, event.authenticated, seq
        ) or self._shows_trigger_frame_of(camera, occurrence):
            _LOGGER.debug(
                "Retried thumbnail of %s: superseded by a newer detection or its trigger frame,"
                " in %.1f s",
                serial,
                _elapsed(start),
            )
        else:
            _LOGGER.debug(
                "Retried thumbnail of %s: shown (%d bytes), in %.1f s",
                serial,
                len(still.data),
                _elapsed(start),
            )
            self._store(
                camera,
                device_sn,
                still.data,
                StillSource.THUMBNAIL,
                when,
                occurrence,
                authenticated=event.authenticated,
                kind=_event_kind(event),
                record_id=event.record_id,
                seq=seq,
            )

    @staticmethod
    def _cancel_retry(camera: _CameraStill, device_sn: str, reason: str) -> None:
        """Drop a camera's owed thumbnail attempt and its timer, if any; log only a real one."""
        if camera.retry_event is not None or camera.cancel_retry is not None:
            _LOGGER.debug("Thumbnail retry for %s cancelled: %s", redact_serial(device_sn), reason)
        if camera.cancel_retry is not None:
            camera.cancel_retry()
            camera.cancel_retry = None
        camera.retry_event = None

    async def _async_live(self, station: Station, camera: _CameraStill, device_sn: str) -> None:
        """One live keyframe for a camera that still has no image."""
        serial = redact_serial(device_sn)
        if camera.image is not None:
            _LOGGER.debug("Live keyframe of %s: skipped, camera got an image", serial)
            return
        _LOGGER.debug("Live keyframe of %s: fetching", serial)
        start = time.monotonic()

        async def fetch(wait: bool) -> bytes:
            async with asyncio.timeout(LIVE_SNAPSHOT_TIMEOUT_SECONDS):
                return await station.async_snapshot(device_sn, wait=wait)

        hevc = await async_open_media(station, self._yield_media, fetch)
        _LOGGER.debug(
            "Live keyframe of %s: fetched %d bytes in %.1f s, decode started",
            serial,
            len(hevc),
            _elapsed(start),
        )
        start = time.monotonic()
        jpeg = await async_hevc_to_jpeg(
            ffmpeg_command(self._hass), hevc, FFMPEG_DECODE_TIMEOUT_SECONDS
        )
        current = camera.image is None
        _log_decode(device_sn, jpeg, start, current=current)
        if jpeg is not None and current:
            self._store(camera, device_sn, jpeg, StillSource.LIVE, None, None, seq=camera.seq)

    async def _async_capture(self, station: Station, camera: _CameraStill, device_sn: str) -> None:
        """One pressed live keyframe; never an HEVC stored."""
        serial = redact_serial(device_sn)
        _LOGGER.debug("Live capture of %s: fetching", serial)
        start = time.monotonic()

        async def fetch(wait: bool) -> CameraImage:
            async with asyncio.timeout(LIVE_SNAPSHOT_TIMEOUT_SECONDS):
                return await station.async_camera_image(device_sn, ImageSource.LIVE, wait=wait)

        try:
            image = await async_open_media(station, self._yield_media, fetch)
        except DeviceBusyError:
            _LOGGER.debug(
                "Live capture of %s: camera busy with a preset capture, nothing sent, in %.1f s",
                serial,
                _elapsed(start),
            )
            return
        except (EufySecurityError, TimeoutError) as err:
            _LOGGER.debug(
                "Live capture of %s: failed (%s), in %.1f s",
                serial,
                errors.failure_reason(err),
                _elapsed(start),
            )
            return
        _LOGGER.debug(
            "Live capture of %s: fetched %d bytes in %.1f s, decode started",
            serial,
            len(image.data),
            _elapsed(start),
        )
        start = time.monotonic()
        jpeg = (
            image.data
            if image.is_jpeg
            else await async_hevc_to_jpeg(
                ffmpeg_command(self._hass), image.data, FFMPEG_DECODE_TIMEOUT_SECONDS
            )
        )
        # Read after the fetch: a press coalesced into this job moved it.
        seq = camera.capture_seq
        current = not self._stopped and camera.shown_seq <= seq
        _log_decode(device_sn, jpeg, start, current=current)
        if jpeg is not None and current:
            self._store(
                camera, device_sn, jpeg, StillSource.LIVE, None, None, seq=seq, on_demand=True
            )

    async def _async_refresh(self, station: Station, camera: _CameraStill, device_sn: str) -> None:
        """One refreshed image: the camera's newest recorded event, camera asleep.

        No thumbnail fallback in hd or hd_only: no usable recording keeps the
        image. The record id, its time text and its paths are never logged.
        """
        serial = redact_serial(device_sn)
        source = self.refresh_source_for(station, device_sn)
        _LOGGER.debug("Refresh of %s: looking up the newest %s", serial, source.value)
        start = time.monotonic()
        cap = (
            REFRESH_THUMBNAIL_TIMEOUT_SECONDS
            if source is ImageSource.THUMBNAIL
            else REFRESH_TRIGGER_FRAME_TIMEOUT_SECONDS
        )
        try:
            async with asyncio.timeout(cap):
                image = await station.async_camera_image(device_sn, source)
        except RecordNotFoundError:
            _LOGGER.debug(
                "Refresh of %s: no %s in the station's history, keeping the image, in %.1f s",
                serial,
                source.value,
                _elapsed(start),
            )
            return
        except (EufySecurityError, TimeoutError) as err:
            _LOGGER.debug(
                "Refresh of %s: failed (%s), in %.1f s",
                serial,
                errors.failure_reason(err),
                _elapsed(start),
            )
            return
        recorded_time = _station_local_time(image.recorded_at)
        still = StillSource(image.source.value)
        if image.is_jpeg:
            seq = camera.refresh_seq
            if self._stopped or camera.shown_seq > seq:
                _LOGGER.debug(
                    "Refresh of %s: superseded by a newer detection, in %.1f s",
                    serial,
                    _elapsed(start),
                )
                return
            _LOGGER.debug(
                "Refresh of %s: shown (%d bytes), in %.1f s",
                serial,
                len(image.data),
                _elapsed(start),
            )
            self._store(
                camera,
                device_sn,
                image.data,
                still,
                None,
                None,
                seq=seq,
                on_demand=True,
                recorded_time=recorded_time,
            )
            return
        _LOGGER.debug(
            "Refresh of %s: fetched %d bytes in %.1f s, decode started",
            serial,
            len(image.data),
            _elapsed(start),
        )
        start = time.monotonic()
        jpeg = await async_hevc_to_jpeg(
            ffmpeg_command(self._hass), image.data, FFMPEG_DECODE_TIMEOUT_SECONDS
        )
        seq = camera.refresh_seq
        current = not self._stopped and camera.shown_seq <= seq
        _log_decode(device_sn, jpeg, start, current=current)
        if jpeg is not None and current:
            self._store(
                camera,
                device_sn,
                jpeg,
                still,
                None,
                None,
                seq=seq,
                on_demand=True,
                recorded_time=recorded_time,
            )

    async def _async_standalone_image(
        self, station: Station, camera: _CameraStill, device_sn: str
    ) -> None:
        """A standalone camera's event still for its newest detection.

        The library returns the camera's newest still only when it is the detection's.
        One not written yet is asked once more after
        ``STANDALONE_IMAGE_RETRY_DELAY_SECONDS``, then given up; so is a still of an
        earlier detection when a later one joined during the fetch. A still of a
        detection whose HD image is shown never replaces it: it goes to the event
        history only, as kind ``<detection>_thumbnail``.
        """
        serial = redact_serial(device_sn)
        event = camera.standalone_event
        if event is None:
            return
        if not self._still_current(
            camera, event.dedupe_key, camera.standalone_time_ms, True, camera.standalone_seq
        ):
            _LOGGER.debug("Standalone event image of %s: skipped, a newer image is shown", serial)
            return
        asked_ms = camera.standalone_time_ms
        _LOGGER.debug("Standalone event image of %s: fetching, wakes the camera", serial)
        start = time.monotonic()
        try:
            async with asyncio.timeout(REFRESH_THUMBNAIL_TIMEOUT_SECONDS):
                image = await station.async_event_image(event, ImageSource.THUMBNAIL)
        except RecordNotFoundError as err:
            if _STILL_NOT_WRITTEN in str(err):
                self._retry_standalone_image(station, camera, device_sn, "not written yet", start)
                return
            _LOGGER.debug(
                "Standalone event image of %s: no still of this detection, gave up, keeping"
                " the image, in %.1f s",
                serial,
                _elapsed(start),
            )
            return
        except (EufySecurityError, TimeoutError) as err:
            _LOGGER.debug(
                "Standalone event image of %s: failed (%s), keeping the image, in %.1f s",
                serial,
                errors.failure_reason(err),
                _elapsed(start),
            )
            return
        if self._stopped:
            return
        event = camera.standalone_event
        if event is None:
            return
        when = camera.standalone_time_ms
        seq = camera.standalone_seq
        occurrence = event.dedupe_key
        if when > asked_ms:
            self._retry_standalone_image(
                station, camera, device_sn, "an earlier detection's still", start
            )
            return
        if _shows_standalone_hd(camera, when):
            _LOGGER.debug(
                "Standalone event image of %s: its HD image stays shown, still saved to the"
                " history (%d bytes), in %.1f s",
                serial,
                len(image.data),
                _elapsed(start),
            )
            if self._history is not None:
                self._history.async_save(
                    device_sn,
                    image.data,
                    dt_util.utc_from_timestamp(when / 1000),
                    f"{_event_kind(event)}_{StillSource.THUMBNAIL.value}",
                )
            return
        if not self._still_current(camera, occurrence, when, True, seq):
            _LOGGER.debug(
                "Standalone event image of %s: superseded by a newer image, in %.1f s",
                serial,
                _elapsed(start),
            )
            return
        _LOGGER.debug(
            "Standalone event image of %s: shown (%d bytes), in %.1f s",
            serial,
            len(image.data),
            _elapsed(start),
        )
        self._store(
            camera,
            device_sn,
            image.data,
            StillSource.THUMBNAIL,
            when,
            occurrence,
            kind=_event_kind(event),
            seq=seq,
        )

    def _retry_standalone_image(
        self, station: Station, camera: _CameraStill, device_sn: str, what: str, start: float
    ) -> None:
        """Ask a standalone camera's event still once more later, unless asked already."""
        serial = redact_serial(device_sn)
        if camera.standalone_retried:
            _LOGGER.debug(
                "Standalone event image of %s: %s again, gave up, keeping the image, in %.1f s",
                serial,
                what,
                _elapsed(start),
            )
            return
        camera.standalone_retried = True
        self._schedule_standalone_thumbnail(
            station, camera, device_sn, STANDALONE_IMAGE_RETRY_DELAY_SECONDS
        )
        _LOGGER.debug(
            "Standalone event image of %s: %s, keeping the image, fetching again in %s s,"
            " in %.1f s",
            serial,
            what,
            STANDALONE_IMAGE_RETRY_DELAY_SECONDS,
            _elapsed(start),
        )

    @callback
    def _async_standalone_thumbnail_due(
        self, station: Station, device_sn: str, _now: datetime
    ) -> None:
        """An owed fetch of a standalone camera's event still is due: queue it."""
        camera = self._cameras.get(device_sn)
        if camera is None:
            return
        camera.cancel_standalone_thumbnail = None
        if self._stopped or camera.standalone_event is None or camera.standalone_queued:
            return
        camera.standalone_queued = True
        _LOGGER.debug("Standalone event image for %s: fetch queued", redact_serial(device_sn))
        self._enqueue(station, _STANDALONE_JOB, device_sn)

    async def _async_standalone_live(
        self, station: Station, camera: _CameraStill, device_sn: str, when: int
    ) -> None:
        """One full-resolution live keyframe of a standalone camera for its detection.

        Shown as the detection's HD image at ``when`` when it decodes and no newer image
        replaced the detection. Any failure keeps the image; the thumbnail tier is
        unaffected.
        """
        serial = redact_serial(device_sn)
        event = camera.standalone_event
        if event is None:
            return
        seq = camera.standalone_seq
        occurrence = event.dedupe_key
        if _shows_standalone_hd(camera, when) or not self._still_current(
            camera, occurrence, when, True, seq
        ):
            _LOGGER.debug("Detection HD image of %s: skipped, a newer image is shown", serial)
            return
        _LOGGER.debug("Detection HD image of %s: capturing a full-resolution live keyframe", serial)
        start = time.monotonic()

        async def fetch(_wait: bool) -> CameraImage:
            # A standalone camera: async_open_media passes wait=True, what this call does.
            # The cap adds the library's longest hold for the full size to a live image's.
            async with asyncio.timeout(LIVE_SNAPSHOT_TIMEOUT_SECONDS + FULL_RESOLUTION_TIMEOUT):
                return await station.async_event_image(
                    event, ImageSource.LIVE, full_resolution=True
                )

        try:
            image = await async_open_media(station, self._yield_media, fetch)
        except DeviceBusyError:
            _LOGGER.debug(
                "Detection HD image of %s: camera busy with a preset capture, in %.1f s",
                serial,
                _elapsed(start),
            )
            return
        except (EufySecurityError, TimeoutError) as err:
            _LOGGER.debug(
                "Detection HD image of %s: capture failed (%s), in %.1f s",
                serial,
                errors.failure_reason(err),
                _elapsed(start),
            )
            return
        _LOGGER.debug(
            "Detection HD image of %s: fetched %s (%d bytes) in %.1f s, decode started",
            serial,
            _picture_size(image),
            len(image.data),
            _elapsed(start),
        )
        start = time.monotonic()
        jpeg = (
            image.data
            if image.is_jpeg
            else await async_hevc_to_jpeg(
                ffmpeg_command(self._hass), image.data, FFMPEG_DECODE_TIMEOUT_SECONDS
            )
        )
        current = (
            not self._stopped
            and not _shows_standalone_hd(camera, when)
            and self._still_current(camera, occurrence, when, True, seq)
        )
        _log_decode(device_sn, jpeg, start, current=current)
        if jpeg is None:
            return
        _LOGGER.debug(
            "Detection HD image of %s: %s, %.1f s after the detection",
            serial,
            _picture_size(image),
            (_host_now_ms() - when) / 1000,
        )
        if current:
            self._store(
                camera,
                device_sn,
                jpeg,
                StillSource.DETECTION_LIVE,
                when,
                occurrence,
                kind=_event_kind(event),
                seq=seq,
            )

    def _standalone_live_done(
        self, station: Station, camera: _CameraStill, device_sn: str, when: int
    ) -> None:
        """After a standalone camera's capture: queue the one owed, else settle."""
        tried = camera.standalone_live_tried_ms
        camera.standalone_live_tried_ms = when if tried is None else max(tried, when)
        camera.standalone_live_running_ms = None
        if camera.standalone_live_again and not self._stopped:
            camera.standalone_live_again = False
            camera.standalone_live_queued = True
            _LOGGER.debug(
                "Detection HD image for %s: capture queued for a later detection",
                redact_serial(device_sn),
            )
            self._enqueue(station, _STANDALONE_LIVE_JOB, device_sn)
            return
        camera.standalone_live_again = False
        _settle_standalone(camera)

    @staticmethod
    def _drop_standalone(camera: _CameraStill, device_sn: str, reason: str) -> None:
        """Drop a standalone camera's owed fetch, its timer and an owed capture, if any."""
        if camera.cancel_standalone_thumbnail is not None:
            _LOGGER.debug(
                "Standalone event image for %s: owed fetch cancelled, %s",
                redact_serial(device_sn),
                reason,
            )
            camera.cancel_standalone_thumbnail()
            camera.cancel_standalone_thumbnail = None
        camera.standalone_live_again = False
        camera.standalone_event = None

    @staticmethod
    def _shows_trigger_frame_of(camera: _CameraStill, occurrence: str | None) -> bool:
        """Whether the camera already shows the trigger frame of this occurrence."""
        return (
            occurrence is not None
            and camera.occurrence == occurrence
            and camera.source is StillSource.TRIGGER_FRAME
        )

    @staticmethod
    def _newer_pending(
        camera: _CameraStill, occurrence: str | None, when: int, authenticated: bool
    ) -> bool:
        """Whether another detection, no older than this one, waits for the worker.

        A genuine detection waiting always comes first for an unauthenticated one, and
        an unauthenticated one waiting never holds back a genuine one.
        """
        pending = camera.pending
        if pending is None:
            return False
        if pending.authenticated != authenticated:
            return pending.authenticated
        if camera.pending_time_ms < when:
            return False
        return occurrence is None or pending.dedupe_key != occurrence

    @staticmethod
    def _still_current(
        camera: _CameraStill, occurrence: str | None, when: int, authenticated: bool, seq: int
    ) -> bool:
        """Whether a result for the detection (``occurrence``, ``when``) may be shown.

        An unauthenticated result never lands while a genuine detection waits, and a
        genuine result always replaces an image an unauthenticated push brought. A
        pressed image is replaced only by a detection accepted after the press (its
        arrival counter ``seq`` is greater), an unauthenticated one included.
        """
        if not authenticated and camera.pending is not None and camera.pending.authenticated:
            return False
        if camera.shown_on_demand:
            return seq > camera.shown_seq
        if camera.image is None or camera.source is StillSource.LIVE:
            return True
        if occurrence is not None and occurrence == camera.occurrence:
            return True
        if authenticated and not camera.shown_authenticated:
            return True
        return camera.event_time_ms is None or when >= camera.event_time_ms

    def _store(
        self,
        camera: _CameraStill,
        device_sn: str,
        image: bytes,
        source: StillSource,
        when: int | None,
        occurrence: str | None,
        *,
        authenticated: bool = True,
        seq: int,
        on_demand: bool = False,
        recorded_time: datetime | None = None,
        kind: str | None = None,
        record_id: int | None = None,
    ) -> None:
        """Show ``image`` and hand it to the cache and the history.

        ``kind`` names a detection's class for the history; None is ``live`` for a live
        image and ``event`` for any other. ``record_id`` is the detection's history row,
        noted so its recording's video takes the still's name.
        """
        _LOGGER.debug(
            "Camera %s now shows its %s (%d bytes), was %s",
            redact_serial(device_sn),
            source.value,
            len(image),
            camera.source.value if camera.source is not None else None,
        )
        self._live_skip_logged.pop(device_sn, None)
        camera.image = image
        camera.source = source
        camera.event_time_ms = when
        camera.occurrence = occurrence
        camera.shown_authenticated = authenticated
        camera.shown_seq = seq
        camera.shown_on_demand = on_demand
        camera.recorded_time = recorded_time
        now = _utcnow()
        if camera.stored_time is not None and now <= camera.stored_time:
            # A wall clock stepped back, or two stores within one tick: still a new value.
            now = camera.stored_time + _STORED_TIME_STEP
        camera.stored_time = now
        if self._cache is not None:
            self._cache.async_save(
                still_cache.still_key(device_sn, CAMERA_STILL_NAME), image, _still_meta(camera)
            )
        if self._history is not None:
            self._history.async_save(
                device_sn,
                image,
                _still_moment(camera, now),
                _history_kind(source, kind),
                record_id=record_id,
            )
        async_dispatcher_send(self._hass, image_signal(self._entry.entry_id, device_sn))

    @callback
    def async_restore(self, stills: Mapping[str, tuple[bytes, Mapping[str, Any]]]) -> None:
        """Show the stills the cache kept, before any entity or detection reads them.

        A restored still ranks like the one it was: a detection older than its detection
        is still ignored. A still with metadata this module cannot read is skipped.
        """
        for key, (image, meta) in stills.items():
            device_sn, _, name = key.partition(".")
            if name != CAMERA_STILL_NAME or device_sn in self._cameras:
                continue
            camera = _restored_still(image, meta)
            if camera is None:
                _LOGGER.debug("Cached still of %s unreadable, skipped", redact_serial(device_sn))
                continue
            self._cameras[device_sn] = camera
            _LOGGER.debug(
                "Camera %s restored its %s (%d bytes) from the cache",
                redact_serial(device_sn),
                camera.source.value if camera.source is not None else None,
                len(image),
            )


def _shows_standalone_hd(camera: _CameraStill, when: int) -> bool:
    """Whether the camera shows a standalone detection's HD image no older than ``when``."""
    return (
        camera.source is StillSource.DETECTION_LIVE
        and camera.event_time_ms is not None
        and camera.event_time_ms >= when
    )


def _settle_standalone(camera: _CameraStill) -> None:
    """Forget a standalone camera's detection once no fetch or capture is owed for it."""
    if (
        not camera.standalone_queued
        and camera.cancel_standalone_thumbnail is None
        and not camera.standalone_live_queued
        and camera.standalone_live_running_ms is None
        and not camera.standalone_live_again
    ):
        camera.standalone_event = None


def _picture_size(image: CameraImage) -> str:
    """A live image's picture size for the log, as the library read it."""
    if image.width is None or image.height is None:
        return "size unknown"
    return f"{image.width}x{image.height}"


def _event_kind(event: SecurityEvent) -> str:
    """A detection's class for the history file name; ``event`` when it has none."""
    return detections.detection_event_type(event) or "event"


def _history_kind(source: StillSource, kind: str | None) -> str:
    if kind is not None:
        return kind
    return "live" if source is StillSource.LIVE else "event"


def _still_moment(camera: _CameraStill, stored: datetime) -> datetime:
    """The moment a still shows: the detection's time, the recording's start, else its arrival."""
    if camera.event_time_ms is not None:
        return dt_util.utc_from_timestamp(camera.event_time_ms / 1000)
    if camera.recorded_time is not None:
        return camera.recorded_time
    return stored


def _still_meta(camera: _CameraStill) -> dict[str, Any]:
    """What the cache keeps next to a camera's still to show and rank it again."""
    return {
        "source": camera.source.value if camera.source is not None else None,
        "event_time_ms": camera.event_time_ms,
        "occurrence": camera.occurrence,
        "authenticated": camera.shown_authenticated,
        "on_demand": camera.shown_on_demand,
        "recorded_time": _iso(camera.recorded_time),
        "stored_time": _iso(camera.stored_time),
    }


def _restored_still(image: bytes, meta: Mapping[str, Any]) -> _CameraStill | None:
    """A camera's still as the cache kept it; None when the metadata does not read."""
    try:
        source = StillSource(meta["source"])
        event_time_ms = meta.get("event_time_ms")
        occurrence = meta.get("occurrence")
        if event_time_ms is not None and not isinstance(event_time_ms, int):
            return None
        if occurrence is not None and not isinstance(occurrence, str):
            return None
        stored_time = _parse_iso(meta.get("stored_time"))
        recorded_time = _parse_iso(meta.get("recorded_time"))
    except KeyError, TypeError, ValueError:
        return None
    if stored_time is None:
        return None
    camera = _CameraStill(
        image=image,
        source=source,
        event_time_ms=event_time_ms,
        occurrence=occurrence,
        shown_authenticated=meta.get("authenticated") is not False,
        shown_on_demand=meta.get("on_demand") is True,
        recorded_time=recorded_time,
        stored_time=stored_time,
    )
    # The shown detection keeps its ordering mark, on the channel it came from.
    if camera.shown_authenticated:
        camera.newest_ms, camera.newest_occurrence = event_time_ms, occurrence
    else:
        camera.newest_unauthenticated_ms = event_time_ms
        camera.newest_unauthenticated_occurrence = occurrence
    return camera


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_iso(value: object) -> datetime | None:
    """An aware datetime from the cache's ISO text; None for None, raises for anything else."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(value)
    parsed = dt_util.parse_datetime(value)
    if parsed is None or parsed.tzinfo is None:
        raise ValueError(value)
    return parsed


def _log_decode(device_sn: str, jpeg: bytes | None, start: float, *, current: bool) -> None:
    """Log a keyframe decode's outcome and duration; a stale JPEG is named superseded."""
    serial = redact_serial(device_sn)
    if jpeg is None:
        _LOGGER.debug(
            "Decode for %s failed in %.1f s, keeping the current image", serial, _elapsed(start)
        )
    elif current:
        _LOGGER.debug(
            "Decode for %s ok, %d bytes of JPEG in %.1f s", serial, len(jpeg), _elapsed(start)
        )
    else:
        _LOGGER.debug(
            "Decode for %s ok, %d bytes of JPEG in %.1f s, superseded by a newer detection",
            serial,
            len(jpeg),
            _elapsed(start),
        )


def _ignored(device_sn: str, reason: str) -> None:
    """Log why a camera's detection request was ignored; the reason names no identifier."""
    _LOGGER.debug("Snapshot request for %s ignored: %s", redact_serial(device_sn), reason)


def _station_local_time(value: str | None) -> datetime | None:
    """A history record's ``start_time`` as an aware time; None when it does not parse.

    The station writes it in its own local time with no zone, and the library picks
    the history day by the host's local date (``Station._newest_camera_record``), so
    it is read in Home Assistant's configured zone. The value is never logged.
    """
    if value is None:
        return None
    try:
        parsed = dt_util.parse_datetime(value)
    except ValueError:
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_util.get_default_time_zone())
    return parsed


def _elapsed(start: float) -> float:
    """Seconds since ``start`` on the real monotonic clock, for a DEBUG duration."""
    return time.monotonic() - start


def _host_now_ms() -> int:
    """The host's time in ms, for a detection whose own time the library rejected."""
    return int(time.time() * 1000)
