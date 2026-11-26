"""The recording of a detection whose push names none (a HomeBase 2's cloud push).

A HomeBase 2 reports its cameras' detections only by cloud push, and that push names
no recording, so neither the detection's trigger frame nor its clip can be fetched when
it arrives. The router hands such a detection here when it is authenticated and
``Station.needs_recording_match`` says so, and this module:

- **Waits per detection** in a background task of the entry, so the router never
  blocks: ``Station.async_wait_recording_for_detection`` lists the camera's recordings,
  matches the row by camera and time and returns it once settled, or None at its bound
  (180 s after the detection's time). Unload cancels every wait. A later copy of the
  same occurrence (an enriching push) starts no second wait and takes no slot.
- **Keeps the detection's place.** The camera's arrival slot is taken when the push
  arrives (``SnapshotManager.async_hold_slot``), so a Capture or Refresh press made
  after the push is not replaced by the later frame.
- **Trigger frame**, through the snapshot manager with the event naming the row
  (``Station.event_with_recording``), without the thumbnail step: hd and hd_only show
  it, thumbnail mode none. Detections matched to the same row (one ``ref``) share it:
  their frame requests run one at a time, and a detection with a later slot than the
  last request asks again with its own event and slot unless the camera already shows
  that row's frame. So the frame replaces whatever the camera showed since the first of
  those detections (a push picture, a press between them, another row's frame), except a
  press after the newest one, in either finish order; it is fetched once unless a newer
  image came in between. Shown at the detection whose request was accepted first: with
  nothing else shown, that can be the earlier one (the same image).
- **Copy**, with event videos on, once per row, through the recording sync's store after
  the frame request is done or dropped (or ``MATCHED_FRAME_WAIT_SECONDS`` passed), so the
  video takes the still's name. When the camera does not show the row's frame then and
  another wait of the camera still runs (which may still show it), the copy waits until
  no wait of the camera runs, then goes in the order deferred; a wait cancelled by an
  unload copies nothing. The 15-minute pass finds a copied row stored.

Nothing found, a station that never answered and an event the library cannot match are
DEBUG lines only. Serials are redacted; paths and record ids are never logged.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from homeassistant.core import HomeAssistant, callback

from eufy_home_security import (
    CommunicationError,
    EufySecurityError,
    HistoryRecord,
    SecurityEvent,
    Station,
    redact_serial,
)
from eufy_home_security.station import RECORDING_QUIET

from . import errors
from .const import DOMAIN, MATCHED_FRAME_WAIT_SECONDS

if TYPE_CHECKING:
    from .recordings import RecordingManager
    from .runtime import EufyConfigEntry
    from .snapshots import SnapshotManager

_LOGGER = logging.getLogger(__name__)

# Occurrences and recordings remembered; a detection's wait ends within minutes.
_KEPT: Final = 64


@dataclass(slots=True)
class _Matched:
    """A recording detections were matched to: its latest frame request and its copy."""

    slot: int | None = None
    frame: asyncio.Future[None] | None = None
    copied: bool = False


class RecordingMatcher:
    """One entry's waits for the recordings of detections that name none."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: EufyConfigEntry,
        snapshots: SnapshotManager | None,
        recordings: RecordingManager | None,
    ) -> None:
        """``recordings`` None: event videos are off, nothing is copied."""
        self._hass = hass
        self._entry = entry
        self._snapshots = snapshots
        self._recordings = recordings
        self._tasks: set[asyncio.Task[None]] = set()
        # Per camera serial, how many detections' waits are running, and the copies
        # left until none runs: (station serial, recording, its state), in that order.
        self._cameras: dict[str, int] = {}
        self._deferred: dict[str, list[tuple[str, HistoryRecord, _Matched]]] = {}
        # Occurrences (dedupe keys) a wait was started for.
        self._occurrences: OrderedDict[str, None] = OrderedDict()
        # Per station and recording ref, what was done for that recording.
        self._matched: OrderedDict[tuple[str, str], _Matched] = OrderedDict()

    @property
    def waiting(self) -> int:
        """How many detections' waits are running."""
        return len(self._tasks)

    @callback
    def async_detection(self, station: Station, event: SecurityEvent) -> None:
        """Wait for the recording of ``event``, an authenticated detection of a camera
        paired to ``station`` that ``station.needs_recording_match`` named."""
        device_sn = event.device_sn
        if device_sn is None:
            return
        serial = redact_serial(device_sn)
        occurrence = event.dedupe_key
        if occurrence is not None:
            if occurrence in self._occurrences:
                _LOGGER.debug(
                    "Recording of a detection of %s: a copy of a detection already waited for",
                    serial,
                )
                return
            _remember(self._occurrences, occurrence, None)
        slot = self._snapshots.async_hold_slot(device_sn) if self._snapshots is not None else None
        if slot is None and self._recordings is None:
            _LOGGER.debug(
                "Recording of a detection of %s: not looked for, no trigger frame shown"
                " and event videos off",
                serial,
            )
            return
        _LOGGER.debug("Recording of a detection of %s: waiting for it to settle", serial)
        task = self._entry.async_create_background_task(
            self._hass,
            self._async_run(station, event, device_sn, slot),
            name=f"{DOMAIN} recording of a detection",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _async_run(
        self, station: Station, event: SecurityEvent, device_sn: str, slot: int | None
    ) -> None:
        self._cameras[device_sn] = self._cameras.get(device_sn, 0) + 1
        cancelled = False
        try:
            await self._async_match(station, event, device_sn, slot)
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception:
            # Logged here: an entry background task's error is never retrieved.
            _LOGGER.exception("Unexpected error handling the recording of a detection")
        finally:
            if (left := self._cameras[device_sn] - 1) > 0:
                self._cameras[device_sn] = left
            else:
                del self._cameras[device_sn]
                deferred = self._deferred.pop(device_sn, [])
                if not cancelled:
                    # The camera's last overlapping wait ended: the deferred copies now,
                    # named by the still as it then stands. Never after an unload.
                    self._copy_deferred(device_sn, deferred)

    async def _async_match(
        self, station: Station, event: SecurityEvent, device_sn: str, slot: int | None
    ) -> None:
        serial = redact_serial(device_sn)
        try:
            record = await station.async_wait_recording_for_detection(event, quiet=RECORDING_QUIET)
        except CommunicationError as err:
            _LOGGER.debug(
                "Recording of a detection of %s: no listing answered by the bound (%s)",
                serial,
                errors.failure_reason(err),
            )
            return
        except EufySecurityError as err:
            _LOGGER.debug(
                "Recording of a detection of %s: not looked up (%s)",
                serial,
                errors.failure_reason(err),
            )
            return
        if record is None:
            _LOGGER.debug("Recording of a detection of %s: none matched by the bound", serial)
            return
        key = (station.serial, record.ref or str(record.record_id))
        matched = self._matched.get(key)
        if matched is None:
            matched = _remember(self._matched, key, _Matched())
        _LOGGER.debug("Recording of a detection of %s: settled", serial)
        await self._async_frame(station, event, device_sn, record, slot, matched)
        if self._recordings is None or matched.copied:
            return
        if (
            slot is not None
            and self._snapshots is not None
            and not self._snapshots.shows_frame_of(device_sn, record.record_id)
            and self._cameras.get(device_sn, 0) > 1
        ):
            # A later detection of the camera may still show this recording's frame:
            # copying now would name the video by the clip's start, not by that still.
            _LOGGER.debug(
                "Recording of a detection of %s: copy waits for the camera's other waits",
                serial,
            )
            self._deferred.setdefault(device_sn, []).append((station.serial, record, matched))
            return
        matched.copied = True
        self._recordings.async_matched(station.serial, record)

    def _copy_deferred(
        self, device_sn: str, deferred: list[tuple[str, HistoryRecord, _Matched]]
    ) -> None:
        """Hand each deferred copy not made meanwhile to the recording sync, in order."""
        if self._recordings is None:
            return
        for station_sn, record, matched in deferred:
            if matched.copied:
                continue
            _LOGGER.debug(
                "Recording of a detection of %s: deferred copy, the camera's waits are done",
                redact_serial(device_sn),
            )
            matched.copied = True
            self._recordings.async_matched(station_sn, record)

    async def _async_frame(
        self,
        station: Station,
        event: SecurityEvent,
        device_sn: str,
        record: HistoryRecord,
        slot: int | None,
        matched: _Matched,
    ) -> None:
        """Ask for ``record``'s trigger frame in the detection's slot, unless the last
        request of that recording had a later slot or the camera shows that frame; back
        once it is shown, skipped or failed, or after ``MATCHED_FRAME_WAIT_SECONDS``."""
        if slot is None or self._snapshots is None:
            return
        serial = redact_serial(device_sn)
        # One request per recording at a time: a later detection decides on its outcome.
        while (frame := matched.frame) is not None and not frame.done():
            await _bounded(frame, serial)
            if matched.frame is frame:
                break
        if matched.slot is not None:
            if matched.slot > slot:
                _LOGGER.debug(
                    "Recording of a detection of %s: its frame was asked for a later detection",
                    serial,
                )
                return
            if self._snapshots.shows_frame_of(device_sn, record.record_id):
                _LOGGER.debug(
                    "Recording of a detection of %s: the camera shows its trigger frame", serial
                )
                return
        try:
            named = station.event_with_recording(event, record)
        except ValueError:
            _LOGGER.debug(
                "Recording of a detection of %s: no trigger frame, the row names no recording",
                serial,
            )
            return
        matched.slot = slot
        matched.frame = self._snapshots.async_request_matched_frame(station, named, slot)
        await _bounded(matched.frame, serial)


async def _bounded(frame: asyncio.Future[None], serial: str) -> None:
    """Wait for a frame request's outcome, at most ``MATCHED_FRAME_WAIT_SECONDS``; the
    request itself is never cancelled (other detections may wait on it too)."""
    done, _ = await asyncio.wait((frame,), timeout=MATCHED_FRAME_WAIT_SECONDS)
    if not done:
        _LOGGER.debug(
            "Recording of a detection of %s: trigger frame not done within %s s, going on",
            serial,
            MATCHED_FRAME_WAIT_SECONDS,
        )


def _remember[K, V](kept: OrderedDict[K, V], key: K, value: V) -> V:
    """Add ``key`` to ``kept``, dropping the oldest past ``_KEPT``; returns ``value``."""
    kept[key] = value
    while len(kept) > _KEPT:
        kept.popitem(last=False)
    return value
