"""Each HomeBase recording as an MP4 in the event history, beside its detection's still.

With the ``event_videos`` option on and the event history kept, every recording a
HomeBase holds for a paired camera is downloaded off the station's disk once it has
finished and stored by ``history.EventHistory.async_save_clip``:

- **Never wakes a camera.** The station plays the clip off its own disk
  (``Station.async_download_recording``) on an extra session from the live-stream
  budget, one download per station at a time. A standalone camera keeps no
  recordings on a station, so it has no sync.
- **When.** One pass per station once after setup, again a clip length plus the
  library's settle time after each detection push of that station (coalesced), and
  every 15 minutes as a catch-up. A pass that saw a recording still running comes
  back once it has settled.
- **Only upcoming.** The option syncs recordings that start after it was switched on:
  the first setup with it on marks that moment in the Store (``since``), and no pass
  lists before it. A setup with the option off drops the mark, so switching it on
  again marks a new moment. Older recordings are fetched one by one on request
  (``station_recordings.py``).
- **What.** A pass lists the station's recordings since the newest one stored (with a
  margin, so one left unsettled or failed is listed again), keeps the settled rows
  not stored yet, and downloads them oldest first, one at a time.
- **Names.** A recording whose detection's still was written takes that still's stamp
  and kind (the history notes them by ``record_id``), so a browser pairs the two by
  name; any other is named by the recording's start, kind ``event``.
- **Once.** Each stored ``record_id`` is kept in a ``Store`` per entry with its file
  name, pruned with the retention, so a restart downloads nothing again and a file
  the user deleted is not fetched back. A download that fails or comes back
  incomplete keeps nothing and is tried again on a later pass, up to three attempts.
- **One path.** The sync and a fetch on request store through
  :meth:`StoredRecordings.async_store`: same names, same Store, and a second request
  for a recording being downloaded joins that download.

Serials, paths and record ids are never logged; counts and redacted serials are.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from eufy_home_security import (
    ClipWriter,
    EufySecurityError,
    HistoryRecord,
    MediaClip,
    SecurityEvent,
    Station,
    redact_serial,
)
from eufy_home_security.station import RECORDING_QUIET

from . import errors
from .const import (
    DOMAIN,
    RECORDING_DEFAULT_CLIP_SECONDS,
    RECORDING_DOWNLOAD_TIMEOUT_SECONDS,
    RECORDING_MAX_ATTEMPTS,
    RECORDING_SYNC_FIRST_DELAY_SECONDS,
    RECORDING_SYNC_INTERVAL_SECONDS,
)
from .history import ClipStoreError, EventHistory, IncompleteClipError, SavedClip

if TYPE_CHECKING:
    from .runtime import EufyConfigEntry

_LOGGER = logging.getLogger(__name__)

_STORE_VERSION: Final = 1
# The kind of a recording whose detection's still is unknown.
_EVENT_KIND: Final = "event"
# Rows that started up to this long before the newest stored one are listed again.
_SINCE_MARGIN: Final = timedelta(minutes=30)
# The soonest a follow-up pass for a running recording is scheduled.
_MIN_FOLLOW_UP_SECONDS: Final = 5.0
# The camera setting that holds its recording length in seconds.
_CLIP_LENGTH_SETTING: Final = "video_clip_length"
# A HA-side cap on one listing or page (the library asks one history query per day and page).
LIST_TIMEOUT_SECONDS: Final = 60.0


def store_key(entry_id: str) -> str:
    """The storage key of one entry's stored recordings."""
    return f"{DOMAIN}.recordings.{entry_id}"


async def async_remove_store(hass: HomeAssistant, entry_id: str) -> None:
    """Delete one entry's stored-recordings document."""
    await Store[dict[str, Any]](hass, _STORE_VERSION, store_key(entry_id)).async_remove()


async def join[T](task: asyncio.Task[T], stopped: Exception) -> T:
    """``task``'s result, shielded from this waiter's cancellation.

    Raises ``stopped`` when ``task`` itself was cancelled (an unload) while this
    waiter was not, so a request waiting on it gets an answer.
    """
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            raise
        raise stopped from None


@dataclass(frozen=True, slots=True)
class StoredFile:
    """Where a stored recording's MP4 was written: its camera folder and file name.

    ``folder`` is None for a record that names none; the camera's current folder is
    meant then.
    """

    folder: str | None
    file: str


class StoredRecordings:
    """One entry's record of the recordings stored in the history, and the one way in.

    Per record id (as text): ``file`` (None: given up), ``folder``, ``station``,
    ``device`` and ``started``. ``since`` is the event-videos mark: the sync lists nothing before it.
    """

    def __init__(self, hass: HomeAssistant, entry: EufyConfigEntry, events: EventHistory) -> None:
        """Nothing is read until :meth:`async_load`."""
        self._hass = hass
        self._entry = entry
        self._history = events
        self._store: Store[dict[str, Any]] = Store(hass, _STORE_VERSION, store_key(entry.entry_id))
        self.records: dict[str, dict[str, Any]] = {}
        self._since: datetime | None = None
        self._running: dict[int, asyncio.Task[SavedClip]] = {}

    @property
    def history(self) -> EventHistory:
        """The history the recordings are stored in."""
        return self._history

    @property
    def since(self) -> datetime | None:
        """The event-videos mark; None while unset."""
        return self._since

    async def async_load(self) -> None:
        """Read the Store once; flush it again when the entry unloads."""
        stored = await self._store.async_load()
        if isinstance(stored, Mapping):
            records = stored.get("records")
            if isinstance(records, Mapping):
                self.records = {
                    str(key): dict(value)
                    for key, value in records.items()
                    if isinstance(value, Mapping)
                }
            self._since = _parse(stored.get("since"))
        self._entry.async_on_unload(self._async_flush)

    def mark_since(self, moment: datetime) -> None:
        """Set the event-videos mark unless one is set."""
        if self._since is None:
            self._since = moment
            self.save()

    def clear_since(self) -> None:
        """Drop the event-videos mark."""
        if self._since is not None:
            self._since = None
            self.save()

    def stored_file(self, record_id: int, device_sn: str | None = None) -> StoredFile | None:
        """Where ``record_id`` was stored; None when it is not, was given up, or (with
        ``device_sn``) is noted for another camera."""
        record = self.records.get(str(record_id))
        file = record.get("file") if record is not None else None
        if not isinstance(file, str) or not file:
            return None
        noted = record.get("device") if record is not None else None
        if device_sn is not None and noted is not None and noted != device_sn:
            return None
        folder = record.get("folder") if record is not None else None
        return StoredFile(folder if isinstance(folder, str) and folder else None, file)

    def path_of(self, stored: StoredFile, device_sn: str) -> Path | None:
        """The MP4 of ``stored``, a recording of ``device_sn``.

        None when a stored name is not one plain name (a separator, ``.`` or ``..``):
        the Store is not trusted to stay inside the history folder.
        """
        folder = stored.folder or self._history.camera_folder(device_sn)
        if not (_plain(folder) and _plain(stored.file)):
            _LOGGER.debug("A stored recording names a path outside the history folder; skipped")
            return None
        return self._history.root / folder / stored.file

    def is_kept(self, path: Path) -> bool:
        """Whether ``path`` is a file that resolves inside the history folder. Blocking."""
        if not path.is_file():
            return False
        if path.resolve().is_relative_to(self._history.root.resolve()):
            return True
        _LOGGER.debug("A stored recording resolves outside the history folder; skipped")
        return False

    def give_up(self, record_id: int, station_sn: str, started: datetime) -> None:
        """Note ``record_id`` as never to be synced again."""
        self.records[str(record_id)] = {
            "file": None,
            "station": station_sn,
            "started": started.isoformat(),
        }
        self.save()

    def prune(self) -> None:
        """Forget stored recordings whose day the retention no longer keeps."""
        if not self._history.enabled:
            return
        floor = dt_util.start_of_local_day(self._history.oldest_kept())
        before = len(self.records)
        self.records = {
            key: record
            for key, record in self.records.items()
            if (started := _parse(record.get("started"))) is not None and started >= floor
        }
        if len(self.records) != before:
            self.save()

    async def async_store(self, station: Station, row: HistoryRecord) -> SavedClip:
        """Download ``row``'s recording into the history and note it; joins a running one.

        Named after the still its detection wrote (stamp and kind), else after the
        recording's start, kind ``event``. Raises what the download and
        ``EventHistory.async_save_clip`` raise, ``TimeoutError`` past
        ``RECORDING_DOWNLOAD_TIMEOUT_SECONDS``, and ``ClipStoreError`` for a row
        without a start or a camera, or a download stopped by an unload. A cancelled
        waiter leaves the download running.
        """
        task = self._running.get(row.record_id)
        if task is None:
            task = self._entry.async_create_background_task(
                self._hass, self._async_download(station, row), name=f"{DOMAIN} recording download"
            )
            self._running[row.record_id] = task
            task.add_done_callback(lambda done: self._finished(row.record_id, done))
        return await join(task, ClipStoreError("the download was stopped"))

    def _finished(self, record_id: int, task: asyncio.Task[SavedClip]) -> None:
        """Forget a finished download; its outcome went to whoever waited for it."""
        self._running.pop(record_id, None)
        if not task.cancelled():
            task.exception()

    async def _async_download(self, station: Station, row: HistoryRecord) -> SavedClip:
        started = row.started_at
        device_sn = row.device_sn
        if started is None or device_sn is None:
            raise ClipStoreError("the recording names no start or camera")
        named = self._history.still_name(row.record_id)
        moment, kind = named if named is not None else (started, _EVENT_KIND)

        async def produce(write: ClipWriter) -> MediaClip:
            return await station.async_download_recording(row, write, wait=True)

        async with asyncio.timeout(RECORDING_DOWNLOAD_TIMEOUT_SECONDS):
            saved = await self._history.async_save_clip(
                device_sn, produce, kind=kind, moment=moment, require_complete=True
            )
        self.records[str(row.record_id)] = {
            "file": saved.path.name,
            "folder": saved.path.parent.name,
            "station": station.serial,
            "device": device_sn,
            "started": started.isoformat(),
        }
        self.save()
        return saved

    def save(self) -> None:
        """Write the Store a second from now."""
        self._store.async_delay_save(self._data, 1)

    def _data(self) -> dict[str, Any]:
        data: dict[str, Any] = {"records": dict(self.records)}
        if self._since is not None:
            data["since"] = self._since.isoformat()
        return data

    async def _async_flush(self) -> None:
        await self._store.async_save(self._data())


@dataclass(slots=True)
class SyncStats:
    """One station's sync counters since setup, for diagnostics."""

    passes: int = 0
    stored: int = 0
    failed: int = 0
    given_up: int = 0
    last_pass: str | None = None
    last_error: str | None = None


@dataclass(slots=True)
class RecordingSync:
    """One HomeBase's sync: its pass task, timers and attempt counts."""

    station: Station
    stats: SyncStats = field(default_factory=SyncStats)
    task: asyncio.Task[None] | None = None
    again: bool = False
    cancel_soon: CALLBACK_TYPE | None = None
    soon_at: float | None = None
    # Per record id, failed attempts and the recording's start, until stored or given up.
    attempts: dict[int, tuple[int, datetime]] = field(default_factory=dict)
    # Where the last good listing leaves the next one to start; None before one.
    watermark: datetime | None = None


class RecordingManager:
    """One entry's recording syncs, one per HomeBase, and the record of what is stored."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: EufyConfigEntry,
        stored: StoredRecordings,
        stations: Iterable[Station],
    ) -> None:
        """Syncs for every station that is not standalone; ``async_start`` starts them."""
        self._hass = hass
        self._entry = entry
        self._stored = stored
        self._history = stored.history
        self._syncs: dict[str, RecordingSync] = {
            station.serial: RecordingSync(station)
            for station in stations
            if not station.is_standalone
        }
        self._warned: set[str] = set()
        self._stopped = False

    @property
    def station_serials(self) -> frozenset[str]:
        """The stations this entry syncs."""
        return frozenset(self._syncs)

    @property
    def busy(self) -> bool:
        """Whether any station's pass is running."""
        return any(sync.task is not None and not sync.task.done() for sync in self._syncs.values())

    def stats(self, station_sn: str) -> dict[str, Any] | None:
        """One station's sync counters and stored count; None for a station not synced."""
        sync = self._syncs.get(station_sn)
        if sync is None:
            return None
        return {
            "passes": sync.stats.passes,
            "stored": sync.stats.stored,
            "failed": sync.stats.failed,
            "given_up": sync.stats.given_up,
            "retrying": len(sync.attempts),
            "kept": sum(
                1
                for record in self._stored.records.values()
                if record.get("station") == station_sn and record.get("file")
            ),
            "last_pass": sync.stats.last_pass,
            "last_error": sync.stats.last_error,
        }

    async def async_start(self) -> None:
        """Mark when the option came on, prune what is stored, arm each station's passes.

        The Store is loaded already (:meth:`StoredRecordings.async_load`).
        """
        self._stored.mark_since(dt_util.utcnow())
        self._stored.prune()
        self._entry.async_on_unload(self._async_stop)
        if not self._syncs:
            return
        self._entry.async_on_unload(
            async_track_time_interval(
                self._hass,
                self._async_tick,
                timedelta(seconds=RECORDING_SYNC_INTERVAL_SECONDS),
            )
        )
        for serial in self._syncs:
            self._async_schedule(serial, RECORDING_SYNC_FIRST_DELAY_SECONDS)
        _LOGGER.debug("Recording sync armed for %d HomeBase(s)", len(self._syncs))

    @callback
    def async_detection(self, station_sn: str, event: SecurityEvent) -> None:
        """A detection push of ``station_sn``: a pass once its recording should be done.

        The wait is the camera's clip length (its ``video_clip_length`` setting as last
        read, else a default) plus the library's settle time.
        """
        sync = self._syncs.get(station_sn)
        if sync is None:
            return
        if event.video_path is None and event.record_id is None:
            return
        self._async_schedule(
            station_sn, _clip_seconds(sync.station, event.device_sn) + RECORDING_QUIET
        )

    @callback
    def _async_tick(self, _now: datetime) -> None:
        self._stored.prune()
        for serial in self._syncs:
            self._async_run(serial)

    @callback
    def _async_schedule(self, station_sn: str, delay: float) -> None:
        """One pass of ``station_sn`` in ``delay`` seconds; an earlier timer stands."""
        sync = self._syncs[station_sn]
        due = time.monotonic() + delay
        if self._stopped or (sync.soon_at is not None and sync.soon_at <= due):
            return
        if sync.cancel_soon is not None:
            sync.cancel_soon()

        @callback
        def _fire(_now: datetime) -> None:
            sync.cancel_soon = None
            sync.soon_at = None
            self._async_run(station_sn)

        sync.soon_at = due
        sync.cancel_soon = async_call_later(self._hass, delay, _fire)

    @callback
    def _async_run(self, station_sn: str) -> None:
        """Start a pass of ``station_sn``; with one running, one more follows it."""
        sync = self._syncs[station_sn]
        if self._stopped:
            return
        if sync.task is not None and not sync.task.done():
            sync.again = True
            return
        sync.task = self._entry.async_create_background_task(
            self._hass, self._async_passes(sync), name=f"{DOMAIN} recording sync"
        )

    async def _async_passes(self, sync: RecordingSync) -> None:
        while True:
            sync.again = False
            await self._async_pass(sync)
            if not sync.again or self._stopped:
                return

    async def _async_pass(self, sync: RecordingSync) -> None:
        """List the station's recordings and store each settled one not stored yet."""
        station = sync.station
        serial = redact_serial(station.serial)
        if not station.connected:
            _LOGGER.debug("Recording sync of %s: skipped, the HomeBase is not connected", serial)
            return
        sync.stats.passes += 1
        sync.stats.last_pass = dt_util.utcnow().isoformat(timespec="seconds")
        since = self._since(sync)
        try:
            # Bounded as a request's listing is; a hung one ends the pass, the next retries.
            async with asyncio.timeout(LIST_TIMEOUT_SECONDS):
                rows = await station.async_list_recordings(
                    days=max(self._history.days, 1), since=since
                )
        except (EufySecurityError, TimeoutError) as err:
            sync.stats.last_error = type(err).__name__
            _LOGGER.debug(
                "Recording sync of %s: listing failed (%s)", serial, errors.failure_reason(err)
            )
            return
        now = dt_util.utcnow()
        wanted: list[HistoryRecord] = []
        running: list[HistoryRecord] = []
        for row in rows:
            if (
                not row.record_id
                or row.started_at is None
                or str(row.record_id) in self._stored.records
            ):
                continue
            if Station.recording_settled(row, now=now):
                wanted.append(row)
            else:
                running.append(row)
        wanted.sort(key=lambda row: row.started_at or now)
        _LOGGER.debug(
            "Recording sync of %s: %d listed, %d to store, %d still recording",
            serial,
            len(rows),
            len(wanted),
            len(running),
        )
        for row in wanted:
            if self._stopped:
                return
            await self._async_store(sync, row)
        sync.watermark = min(
            [now - _SINCE_MARGIN, *(row.started_at for row in running if row.started_at)]
        )
        if running:
            self._async_follow_up(station.serial, running)

    def _since(self, sync: RecordingSync) -> datetime:
        """Where a pass lists from, never before the oldest kept day or the option's mark.

        The later of the newest stored start (less a margin) and where the last good
        listing left off, moved back to any recording still owed a retry; the floor
        when neither is known.
        """
        floor = dt_util.start_of_local_day(self._history.oldest_kept())
        if (mark := self._stored.since) is not None:
            floor = max(floor, mark)
        marks = [
            started - _SINCE_MARGIN
            for record in self._stored.records.values()
            if record.get("station") == sync.station.serial
            and (started := _parse(record.get("started"))) is not None
        ]
        if sync.watermark is not None:
            marks.append(sync.watermark)
        if not marks:
            return floor
        since = max(marks)
        for _count, started in sync.attempts.values():
            since = min(since, started)
        return max(since, floor)

    async def _async_store(self, sync: RecordingSync, row: HistoryRecord) -> None:
        """Download one recording into the history; count a failure toward giving up."""
        if row.started_at is None or row.device_sn is None:
            return
        try:
            await self._stored.async_store(sync.station, row)
        except (
            EufySecurityError,
            IncompleteClipError,
            ClipStoreError,
            OSError,
            TimeoutError,
        ) as err:
            self._failed(sync, row, err)
            return
        sync.attempts.pop(row.record_id, None)
        sync.stats.stored += 1
        self._warned.discard(row.device_sn)

    def _failed(self, sync: RecordingSync, row: HistoryRecord, err: BaseException) -> None:
        started = row.started_at or dt_util.utcnow()
        count = sync.attempts.get(row.record_id, (0, started))[0] + 1
        sync.stats.failed += 1
        sync.stats.last_error = type(err).__name__
        camera = redact_serial(row.device_sn)
        reason = (
            "incomplete" if isinstance(err, IncompleteClipError) else errors.failure_reason(err)
        )
        _LOGGER.debug(
            "Recording sync: a recording of %s not stored (%s), attempt %d of %d",
            camera,
            reason,
            count,
            RECORDING_MAX_ATTEMPTS,
        )
        if count < RECORDING_MAX_ATTEMPTS:
            sync.attempts[row.record_id] = (count, started)
            return
        sync.attempts.pop(row.record_id, None)
        sync.stats.given_up += 1
        self._stored.give_up(row.record_id, sync.station.serial, started)
        if row.device_sn is not None and row.device_sn not in self._warned:
            self._warned.add(row.device_sn)
            _LOGGER.warning(
                "A recording of %s could not be saved to the event history after %d "
                "attempts (%s); it stays on the HomeBase",
                camera,
                RECORDING_MAX_ATTEMPTS,
                reason,
            )

    @callback
    def _async_follow_up(self, station_sn: str, running: list[HistoryRecord]) -> None:
        """Pass again once the earliest-ending running recording has settled."""
        now = dt_util.utcnow()
        ends = [row.ended_at for row in running if row.ended_at is not None]
        delay = (
            min((end - now).total_seconds() for end in ends) + RECORDING_QUIET
            if ends
            else RECORDING_DEFAULT_CLIP_SECONDS + RECORDING_QUIET
        )
        self._async_schedule(station_sn, max(delay, _MIN_FOLLOW_UP_SECONDS))

    async def _async_stop(self) -> None:
        """Cancel every timer and pass."""
        self._stopped = True
        for sync in self._syncs.values():
            if sync.cancel_soon is not None:
                sync.cancel_soon()
                sync.cancel_soon = None
            if sync.task is not None and not sync.task.done():
                sync.task.cancel()


def _clip_seconds(station: Station, device_sn: str | None) -> float:
    """The camera's clip length in seconds from the cached state; a default when unknown."""
    state = station.state
    if state is None or device_sn is None:
        return RECORDING_DEFAULT_CLIP_SECONDS
    try:
        value = state.setting(_CLIP_LENGTH_SETTING, device_sn=device_sn)
    except EufySecurityError, ValueError:
        return RECORDING_DEFAULT_CLIP_SECONDS
    if isinstance(value, int | float) and not isinstance(value, bool) and 0 < value <= 600:
        return float(value)
    return RECORDING_DEFAULT_CLIP_SECONDS


def _plain(name: str) -> bool:
    """Whether ``name`` is one path part: no separator, not ``.`` or ``..``, not empty."""
    return name not in {"", ".", ".."} and "/" not in name and "\\" not in name


def _parse(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt_util.parse_datetime(value)
    except ValueError:
        return None
    return parsed if parsed is not None and parsed.tzinfo is not None else None
