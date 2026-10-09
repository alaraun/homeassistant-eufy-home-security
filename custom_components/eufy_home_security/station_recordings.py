"""A HomeBase's own recordings of a camera, listed and fetched on request, for the card.

Two websocket commands and one view, registered once per Home Assistant instance:

- ``eufy_home_security/recordings`` ``{entity_id, days?, limit?, before?, day?}`` lists the
  camera's recordings on the station's disk, newest first: start, end, length, size,
  whether it has settled, the kind of its detection's still when known, the media id
  of its MP4 once stored, and a signed URL of its thumbnail. ``days`` is the window
  back from today (default the entry's history days, 1-30). With ``limit`` or
  ``before`` the answer is one page of ``Station.async_list_recordings``: up to
  ``limit`` rows (default 10) older than the cursor ``before``, the library asking the
  days from today (or the cursor's day) backwards until the page is full, ``PAGE_DAYS``
  days at most. A full page says ``more`` (the next one may be empty) and ``next``, an
  opaque text cursor for it; so does a short page with days of the window left (``next``
  then names the day before the ones it asked); any other short page ends the window.
  ``day`` (``YYYY-MM-DD``) pages only that day's recordings: the window reaches back to
  it (up to ``MAX_LIST_DAYS``; an older day lists nothing). Without ``limit``,
  ``before`` or ``day``, the whole window in one answer (``more`` False). A listing or
  page is reused for ``LIST_CACHE_SECONDS`` per camera and arguments, and identical
  queries in flight share one. A standalone camera keeps no recordings on a station:
  ``supported`` False, nothing is queried.
- ``eufy_home_security/recordings/fetch`` ``{entity_id, record_id}`` stores one
  recording in the event history exactly as the event-videos sync does
  (``recordings.StoredRecordings``: same name, same Store, so the sync never fetches
  it again and the retention covers it) and returns its ``media_content_id`` and a
  signed ``url`` from the media source, playable by a ``<video>``. A stored one whose
  file still exists returns at once. The station plays it off its disk: no camera is
  woken.
- ``/api/eufy_home_security/recording_thumb/<entity id>/<record id>`` serves a
  recording's still off the station's disk as ``image/jpeg``, behind Home Assistant's
  auth (the listing hands out signed paths), kept in a small in-memory LRU; 404 for a
  still that is obfuscated or absent.

Errors carry a code: ``not_found``, ``not_settled``, ``unavailable``, ``busy`` (the
station's session budget is spent) and ``failed``. Serials, paths and record ids are
never logged.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import voluptuous as vol
from aiohttp import hdrs, web
from homeassistant.components import websocket_api
from homeassistant.components.camera import DOMAIN as CAMERA_DOMAIN
from homeassistant.components.http.auth import async_sign_path
from homeassistant.components.media_player.browse_media import async_process_play_media_url
from homeassistant.components.media_source import Unresolvable, async_resolve_media
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.http import KEY_HASS, HomeAssistantView
from homeassistant.util import dt as dt_util
from homeassistant.util.hass_dict import HassKey

from eufy_home_security import (
    EufySecurityError,
    HistoryRecord,
    LiveStreamLimitError,
    Station,
    UnsupportedError,
    entity_unique_id,
    redact_serial,
)

from . import detections, history
from .const import (
    CAMERA_KEY,
    CONF_EVENT_HISTORY_DAYS,
    DEFAULT_EVENT_HISTORY_DAYS,
    DOMAIN,
)
from .history import ClipStoreError, IncompleteClipError
from .recordings import LIST_TIMEOUT_SECONDS, join

if TYPE_CHECKING:
    from .recordings import StoredRecordings
    from .runtime import EufyConfigEntry

_LOGGER = logging.getLogger(__name__)

WS_LIST: Final = f"{DOMAIN}/recordings"
WS_FETCH: Final = f"{DOMAIN}/recordings/fetch"
THUMB_URL: Final = "/api/" + DOMAIN + "/recording_thumb/{entity_id}/{record_id}"
THUMB_VIEW_NAME: Final = f"api:{DOMAIN}:recording_thumb"
# How long a listing or page is reused per camera and arguments.
LIST_CACHE_SECONDS: Final = 60.0
# The day counts a listing takes; the default is the entry's history days within these.
MIN_LIST_DAYS: Final = 1
MAX_LIST_DAYS: Final = 30
# Rows per page of a paged listing: the default and the bounds of ``limit``.
PAGE_LIMIT: Final = 10
MIN_PAGE_LIMIT: Final = 1
MAX_PAGE_LIMIT: Final = 50
# The days one page asks at most (one station query each).
PAGE_DAYS: Final = 7
# A page cursor naming a day: ``YYYYMMDD``; a record id is that day times
# ``_RECORD_DAY_FACTOR`` plus a sequence.
_DAY_CURSOR_FORMAT: Final = "%Y%m%d"
_DAY_CURSOR_LEN: Final = 8
_RECORD_DAY_FACTOR: Final = 100_000
# How long a thumbnail's signed path is valid.
THUMB_SIGN_SECONDS: Final = 3600
# The thumbnail LRU: at most this many stills, and none larger than this.
THUMB_CACHE_ITEMS: Final = 50
THUMB_CACHE_MAX_BYTES: Final = 200 * 1024
# How long a browser may keep a thumbnail: a recording's still does not change.
THUMB_MAX_AGE_SECONDS: Final = 3600

ERR_NOT_FOUND: Final = "not_found"
ERR_NOT_SETTLED: Final = "not_settled"
ERR_UNAVAILABLE: Final = "unavailable"
ERR_BUSY: Final = "busy"
ERR_FAILED: Final = "failed"

_REGISTERED: HassKey[bool] = HassKey(f"{DOMAIN}_station_recordings")


def _clock() -> float:
    """The clock the listing cache ages by."""
    return time.monotonic()


def _cursor(value: Any) -> int | date:
    """A page cursor as ``next`` gave it: a record id, or a day as ``YYYYMMDD``."""
    text = str(value)
    if not (text.isascii() and text.isdigit()):
        raise vol.Invalid("expected the cursor a page's next named")
    if len(text) != _DAY_CURSOR_LEN:
        return int(text)
    try:
        return _day_of(text)
    except ValueError as err:
        raise vol.Invalid("expected the cursor a page's next named") from err


def _day_of(text: str) -> date:
    """The day ``YYYYMMDD`` names; ValueError for none."""
    if len(text) != _DAY_CURSOR_LEN:
        raise ValueError(text)
    return date(int(text[:4]), int(text[4:6]), int(text[6:]))


def _cursor_text(cursor: int | date) -> str:
    return cursor.strftime(_DAY_CURSOR_FORMAT) if isinstance(cursor, date) else str(cursor)


def _page_days(days: int, before: int | date | None, day: date | None) -> tuple[date, date, date]:
    """A page's first day of the window, the day it starts at and the oldest day it asks."""
    today = datetime.now().astimezone().date()
    window_first = today - timedelta(days=days - 1)
    if isinstance(before, int):
        try:
            start = _day_of(str(before // _RECORD_DAY_FACTOR))
        except ValueError:
            start = today  # the library refuses the cursor
    else:
        start = before or day or today
    start = min(start, today)
    return window_first, start, max(window_first, start - timedelta(days=PAGE_DAYS - 1))


# A listing's arguments: camera serial, days, limit, before, day (all None: the whole window).
type _ListKey = tuple[str, int, int | None, int | date | None, date | None]


@dataclass(frozen=True, slots=True)
class Page:
    """One page of a camera's recordings, newest first; ``next`` is its last record id,
    or the day before the ones it asked."""

    rows: list[HistoryRecord]
    more: bool
    next: int | date | None


class RecordingError(Exception):
    """A request that cannot be served; ``code`` is the websocket error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(slots=True)
class RequestStats:
    """The entry's listing, fetch and thumbnail counters since setup, for diagnostics."""

    list_calls: int = 0
    list_queries: int = 0
    list_failed: int = 0
    fetch_calls: int = 0
    fetch_downloads: int = 0
    fetch_failed: int = 0
    thumb_calls: int = 0
    thumb_fetches: int = 0


@dataclass(frozen=True, slots=True)
class _Camera:
    """A camera entity of this integration, resolved to its entry, station and serial."""

    entity_id: str
    entry: EufyConfigEntry
    station: Station
    device_sn: str
    recordings: StationRecordings


class StationRecordings:
    """One entry's listings and fetches of its cameras' recordings on their stations."""

    def __init__(self, hass: HomeAssistant, stored: StoredRecordings) -> None:
        """``stored`` is the record of what the history holds, shared with the sync."""
        self._hass = hass
        self._stored = stored
        self._listings: dict[_ListKey, tuple[float, list[HistoryRecord]]] = {}
        self._listing: dict[_ListKey, asyncio.Task[list[HistoryRecord]]] = {}
        self._thumbs: OrderedDict[tuple[str, int], bytes] = OrderedDict()
        self.stats = RequestStats()

    def diagnostics(self) -> dict[str, Any]:
        """The counters and the cache sizes; no serial, path or record id."""
        return {
            **asdict(self.stats),
            "cached_listings": len(self._listings),
            "cached_thumbs": len(self._thumbs),
        }

    async def async_list(self, camera: _Camera, days: int) -> list[HistoryRecord]:
        """The camera's recordings over ``days``, newest first."""
        return await self._async_listing((camera.device_sn, days, None, None, None), camera)

    async def async_page(
        self,
        camera: _Camera,
        *,
        days: int,
        limit: int,
        before: int | date | None,
        day: date | None = None,
    ) -> Page:
        """Up to ``limit`` of the camera's recordings older than ``before``.

        ``before`` is a record id or a day (its recordings and older ones). One
        ``Station.async_list_recordings`` call within the window of ``days``, asking
        ``PAGE_DAYS`` days at most; with ``day`` only that day's recordings (the host's
        local day, as the station keeps). A full page may hold more after it (the
        library cannot tell before asking); a short one ends the window unless days of
        it are left.
        """
        rows = await self._async_listing((camera.device_sn, days, limit, before, day), camera)
        if len(rows) >= limit:
            return Page(rows, True, rows[-1].record_id)
        window_first, _start, oldest = _page_days(days, before, day)
        if oldest > window_first:
            return Page(rows, True, oldest - timedelta(days=1))
        return Page(rows, False, None)

    async def _async_listing(self, key: _ListKey, camera: _Camera) -> list[HistoryRecord]:
        """The listing ``key`` names; cached and coalesced."""
        cached = self._listings.get(key)
        if cached is not None and _clock() - cached[0] < LIST_CACHE_SECONDS:
            return cached[1]
        task = self._listing.get(key)
        if task is None:
            task = camera.entry.async_create_background_task(
                self._hass, self._async_query(key, camera), name=f"{DOMAIN} recordings list"
            )
            self._listing[key] = task
            task.add_done_callback(lambda _done: self._listing.pop(key, None))
        return await join(task, RecordingError(ERR_UNAVAILABLE, "The listing was stopped"))

    async def _async_query(self, key: _ListKey, camera: _Camera) -> list[HistoryRecord]:
        _sn, days, limit, before, day = key
        since: datetime | None = None
        until = day
        cursor = before if isinstance(before, int) else None
        if limit is not None:
            # A page: the walk starts at its day and stops before the day before its oldest
            _first, start, oldest = _page_days(days, before, day)
            since = datetime.combine(oldest, dt_time.min).astimezone()
            if isinstance(before, date):
                until = start
        station = camera.station
        if not station.connected:
            raise RecordingError(ERR_UNAVAILABLE, "The HomeBase is not connected")
        self.stats.list_queries += 1
        try:
            async with asyncio.timeout(LIST_TIMEOUT_SECONDS):
                rows = await station.async_list_recordings(
                    camera.device_sn,
                    days=days,
                    limit=limit,
                    before=cursor,
                    until=until,
                    since=since,
                )
        except ValueError as err:
            # Raised before anything is sent: a cursor that names no recording's day.
            raise RecordingError(
                websocket_api.ERR_INVALID_FORMAT, "The cursor names no recording"
            ) from err
        except (EufySecurityError, TimeoutError) as err:
            self.stats.list_failed += 1
            _LOGGER.debug(
                "Recordings of %s: listing failed (%s)",
                redact_serial(camera.device_sn),
                type(err).__name__,
            )
            raise RecordingError(ERR_UNAVAILABLE, "The HomeBase did not answer") from err
        now = _clock()
        for old in [k for k, (at, _) in self._listings.items() if now - at >= LIST_CACHE_SECONDS]:
            del self._listings[old]
        self._listings[key] = (now, rows)
        return rows

    async def async_rows_json(
        self, camera: _Camera, rows: list[HistoryRecord]
    ) -> list[dict[str, Any]]:
        """The listing's rows as the websocket result carries them."""
        now = dt_util.utcnow()
        stored = {
            row.record_id: found
            for row in rows
            if (found := self._stored.stored_file(row.record_id, camera.device_sn)) is not None
        }
        paths = {
            record_id: path
            for record_id, found in stored.items()
            if (path := self._stored.path_of(found, camera.device_sn)) is not None
        }
        existing = await self._hass.async_add_executor_job(
            _existing, self._stored, list(paths.values())
        )
        result: list[dict[str, Any]] = []
        for row in rows:
            named = self._stored.history.still_name(row.record_id)
            path = paths.get(row.record_id)
            result.append(
                {
                    "record_id": row.record_id,
                    "started_at": _iso(row.started_at),
                    "ended_at": _iso(row.ended_at),
                    "duration_s": row.duration_s,
                    "size_bytes": row.size_bytes,
                    "settled": Station.recording_settled(row, now=now),
                    "kind": named[1] if named is not None else None,
                    "media_content_id": (
                        history.media_content_id(self._hass, path)
                        if path is not None and path in existing
                        else None
                    ),
                    "thumb_url": (
                        async_sign_path(
                            self._hass,
                            THUMB_URL.format(entity_id=camera.entity_id, record_id=row.record_id),
                            timedelta(seconds=THUMB_SIGN_SECONDS),
                        )
                        if row.thumb_path
                        else None
                    ),
                }
            )
        return result

    async def async_row(self, camera: _Camera, record_id: int) -> HistoryRecord:
        """The camera's recording ``record_id``: from a fresh listing, else one query."""
        now = _clock()
        for (device_sn, *_args), (at, rows) in self._listings.items():
            if device_sn != camera.device_sn or now - at >= LIST_CACHE_SECONDS:
                continue
            for row in rows:
                if row.record_id == record_id:
                    return row
        if not camera.station.connected:
            raise RecordingError(ERR_UNAVAILABLE, "The HomeBase is not connected")
        try:
            found = await camera.station.async_history_record(record_id)
        except UnsupportedError as err:
            raise RecordingError(ERR_NOT_FOUND, "No such recording of this camera") from err
        except (EufySecurityError, TimeoutError) as err:
            raise RecordingError(ERR_UNAVAILABLE, "The HomeBase did not answer") from err
        if found is None or found.device_sn != camera.device_sn or found.video_path is None:
            raise RecordingError(ERR_NOT_FOUND, "No such recording of this camera")
        return found

    async def async_fetch(self, camera: _Camera, record_id: int) -> dict[str, str]:
        """Store the recording in the history unless it is; its media id and a play URL."""
        self.stats.fetch_calls += 1
        stored = self._stored.stored_file(record_id, camera.device_sn)
        path = self._stored.path_of(stored, camera.device_sn) if stored is not None else None
        if path is not None and await self._hass.async_add_executor_job(self._stored.is_kept, path):
            return await self._async_playable(path)
        row = await self.async_row(camera, record_id)
        if not Station.recording_settled(row):
            raise RecordingError(ERR_NOT_SETTLED, "The recording is still running")
        if not self._stored.history.enabled:
            raise RecordingError(ERR_FAILED, "Event history is off, so nothing can be saved")
        if not camera.station.connected:
            raise RecordingError(ERR_UNAVAILABLE, "The HomeBase is not connected")
        self.stats.fetch_downloads += 1
        try:
            saved = await self._stored.async_store(camera.station, row)
        except LiveStreamLimitError as err:
            self.stats.fetch_failed += 1
            raise RecordingError(ERR_BUSY, "Every session of the HomeBase is in use") from err
        # Before OSError: TimeoutError is one.
        except (EufySecurityError, TimeoutError) as err:
            self.stats.fetch_failed += 1
            _LOGGER.debug(
                "Recording of %s not downloaded (%s)",
                redact_serial(camera.device_sn),
                type(err).__name__,
            )
            raise RecordingError(ERR_UNAVAILABLE, "The HomeBase did not play it") from err
        except (IncompleteClipError, ClipStoreError, OSError) as err:
            self.stats.fetch_failed += 1
            _LOGGER.debug(
                "Recording of %s not stored (%s)",
                redact_serial(camera.device_sn),
                type(err).__name__,
            )
            raise RecordingError(ERR_FAILED, "The recording could not be saved") from err
        return await self._async_playable(saved.path)

    async def _async_playable(self, path: Path) -> dict[str, str]:
        content_id = history.media_content_id(self._hass, path)
        try:
            media = await async_resolve_media(self._hass, content_id, None)
        except Unresolvable as err:
            raise RecordingError(ERR_FAILED, "The media library cannot serve the file") from err
        return {
            "media_content_id": content_id,
            "url": async_process_play_media_url(self._hass, media.url, allow_relative_url=True),
        }

    async def async_thumbnail(self, camera: _Camera, record_id: int) -> bytes | None:
        """The recording's still as a JPEG; None when it has none or it is no image."""
        self.stats.thumb_calls += 1
        key = (camera.device_sn, record_id)
        if (cached := self._thumbs.get(key)) is not None:
            self._thumbs.move_to_end(key)
            return cached
        row = await self.async_row(camera, record_id)
        if not row.thumb_path:
            return None
        if not camera.station.connected:
            raise RecordingError(ERR_UNAVAILABLE, "The HomeBase is not connected")
        self.stats.thumb_fetches += 1
        try:
            still = await camera.station.async_fetch_still(row.thumb_path)
        except (EufySecurityError, TimeoutError) as err:
            raise RecordingError(ERR_UNAVAILABLE, "The HomeBase did not send it") from err
        if not still.is_image:
            return None
        if len(still.data) <= THUMB_CACHE_MAX_BYTES:
            self._thumbs[key] = still.data
            while len(self._thumbs) > THUMB_CACHE_ITEMS:
                self._thumbs.popitem(last=False)
        return still.data


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _existing(stored: StoredRecordings, paths: list[Path]) -> set[Path]:
    return {path for path in paths if stored.is_kept(path)}


def _camera(hass: HomeAssistant, entity_id: str) -> _Camera:
    """The camera entity ``entity_id`` of a loaded entry of this integration.

    Raises ``not_found`` for any other entity and ``unavailable`` while its entry is
    not loaded.
    """
    registered = er.async_get(hass).async_get(entity_id)
    if (
        registered is None
        or registered.platform != DOMAIN
        or registered.domain != CAMERA_DOMAIN
        or registered.config_entry_id is None
    ):
        raise RecordingError(ERR_NOT_FOUND, "Not a camera of this integration")
    entry = hass.config_entries.async_get_entry(registered.config_entry_id)
    if entry is None or entry.state is not ConfigEntryState.LOADED:
        raise RecordingError(ERR_UNAVAILABLE, "The camera's account is not loaded")
    data = entry.runtime_data
    recordings = getattr(data, "station_recordings", None)
    if recordings is None:
        raise RecordingError(ERR_UNAVAILABLE, "The camera's account is not loaded")
    for coordinator in data.coordinators.values():
        station = coordinator.station
        for device_sn in detections.paired_device_kinds(station):
            if entity_unique_id(device_sn, CAMERA_KEY) == registered.unique_id:
                return _Camera(entity_id, entry, station, device_sn, recordings)
    raise RecordingError(ERR_NOT_FOUND, "Not a camera of this integration")


def _days_to(day: date) -> int:
    """The window from today back to ``day``, within the listing bounds."""
    back = (datetime.now().astimezone().date() - day).days + 1
    return min(max(back, MIN_LIST_DAYS), MAX_LIST_DAYS)


def _default_days(entry: EufyConfigEntry) -> int:
    value = entry.options.get(CONF_EVENT_HISTORY_DAYS, DEFAULT_EVENT_HISTORY_DAYS)
    days = value if isinstance(value, int) and not isinstance(value, bool) else 1
    return min(max(days, MIN_LIST_DAYS), MAX_LIST_DAYS)


@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_LIST,
        vol.Required(ATTR_ENTITY_ID): cv.entity_id,
        vol.Optional("days"): vol.All(
            vol.Coerce(int), vol.Range(min=MIN_LIST_DAYS, max=MAX_LIST_DAYS)
        ),
        vol.Optional("limit"): vol.All(
            vol.Coerce(int), vol.Range(min=MIN_PAGE_LIMIT, max=MAX_PAGE_LIMIT)
        ),
        vol.Optional("before"): vol.Any(None, _cursor),
        vol.Optional("day"): cv.date,
    }
)
@websocket_api.async_response
async def _ws_list(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """List a camera's recordings on its station: the whole window, or one page."""
    try:
        camera = _camera(hass, msg[ATTR_ENTITY_ID])
        camera.recordings.stats.list_calls += 1
        if camera.station.is_standalone:
            connection.send_result(msg["id"], {"supported": False, "recordings": []})
            return
        days = msg.get("days") or _default_days(camera.entry)
        if (day := msg.get("day")) is not None:
            # A later day than today is today (the window and the day filter both count from it)
            day = min(day, datetime.now().astimezone().date())
            days = _days_to(day)
        if "limit" in msg or "before" in msg or day is not None:
            page = await camera.recordings.async_page(
                camera,
                days=days,
                limit=msg.get("limit", PAGE_LIMIT),
                before=msg.get("before"),
                day=day,
            )
        else:
            page = Page(await camera.recordings.async_list(camera, days), False, None)
        result = await camera.recordings.async_rows_json(camera, page.rows)
    except RecordingError as err:
        connection.send_error(msg["id"], err.code, str(err))
        return
    connection.send_result(
        msg["id"],
        {
            "supported": True,
            "recordings": result,
            "more": page.more,
            "next": _cursor_text(page.next) if page.next is not None else None,
        },
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_FETCH,
        vol.Required(ATTR_ENTITY_ID): cv.entity_id,
        vol.Required("record_id"): cv.positive_int,
    }
)
@websocket_api.async_response
async def _ws_fetch(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Store one recording in the history; respond with its media id and play URL."""
    try:
        camera = _camera(hass, msg[ATTR_ENTITY_ID])
        if camera.station.is_standalone:
            raise RecordingError(ERR_NOT_FOUND, "A standalone camera keeps no recordings")
        result = await camera.recordings.async_fetch(camera, msg["record_id"])
    except RecordingError as err:
        connection.send_error(msg["id"], err.code, str(err))
        return
    connection.send_result(msg["id"], result)


class RecordingThumbView(HomeAssistantView):
    """Serves a recording's still off the station's disk."""

    url = THUMB_URL
    name = THUMB_VIEW_NAME
    # An <img> sends no Authorization header: the listing hands out signed paths.
    requires_auth = True

    async def get(self, request: web.Request, entity_id: str, record_id: str) -> web.Response:
        """The still as ``image/jpeg``; 404 for none, 503 while the station is away."""
        hass = request.app[KEY_HASS]
        try:
            number = int(record_id)
        except ValueError:
            return web.Response(status=HTTPStatus.NOT_FOUND)
        try:
            camera = _camera(hass, entity_id)
            if camera.station.is_standalone:
                return web.Response(status=HTTPStatus.NOT_FOUND)
            body = await camera.recordings.async_thumbnail(camera, number)
        except RecordingError as err:
            status = (
                HTTPStatus.NOT_FOUND
                if err.code == ERR_NOT_FOUND
                else HTTPStatus.SERVICE_UNAVAILABLE
            )
            return web.Response(status=status)
        if body is None:
            return web.Response(status=HTTPStatus.NOT_FOUND)
        return web.Response(
            body=body,
            content_type="image/jpeg",
            headers={hdrs.CACHE_CONTROL: f"private, max-age={THUMB_MAX_AGE_SECONDS}"},
        )


@callback
def async_setup(hass: HomeAssistant) -> None:
    """Register the commands and the view once per Home Assistant instance."""
    if hass.data.get(_REGISTERED):
        return
    hass.data[_REGISTERED] = True
    websocket_api.async_register_command(hass, _ws_list)
    websocket_api.async_register_command(hass, _ws_fetch)
    hass.http.register_view(RecordingThumbView())
