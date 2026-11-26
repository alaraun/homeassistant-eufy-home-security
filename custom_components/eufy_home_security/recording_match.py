"""The recording of a detection whose push names none (a HomeBase 2's cloud push).

A HomeBase 2 reports its cameras' detections only by cloud push, and that push names
no recording, so neither the detection's trigger frame nor its clip can be fetched when
it arrives. The router hands such a detection here when ``Station.needs_recording_match``
says so (the only gate), and this module:

- **Waits per detection** in a background task of the entry, so the router never
  blocks: ``Station.async_wait_recording_for_detection`` lists the camera's recordings,
  matches the row by camera and time and returns it once settled, or None at its bound
  (180 s after the detection). Unload cancels every wait.
- **Keeps the detection's place.** The camera's arrival slot is taken when the push
  arrives (``SnapshotManager.async_hold_slot``), so a Capture or Refresh press made
  after the push is not replaced by the later frame.
- **Once per recording.** Detections matched to the same row (one ``ref``) get one
  trigger frame and one copy: the first match does both, the others nothing.
- **Trigger frame**, through the snapshot manager with the event naming the row
  (``Station.event_with_recording``), without the thumbnail step: hd and hd_only show
  it, thumbnail mode none.
- **Copy**, with event videos on, through the recording sync's store once the frame is
  done or dropped, so the video takes the still's name; the 15-minute pass finds it
  stored.

Nothing found, a station that never answered and an event the library cannot match are
DEBUG lines only. Serials are redacted; paths and record ids are never logged.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
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
from .const import DOMAIN

if TYPE_CHECKING:
    from .recordings import RecordingManager
    from .runtime import EufyConfigEntry
    from .snapshots import SnapshotManager

_LOGGER = logging.getLogger(__name__)

# Recordings remembered as handled; a detection's wait ends within minutes.
_HANDLED_KEPT: Final = 64


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
        # Per station and recording ref, the recordings a detection was matched to.
        self._handled: OrderedDict[tuple[str, str], None] = OrderedDict()

    @property
    def waiting(self) -> int:
        """How many detections' waits are running."""
        return len(self._tasks)

    @callback
    def async_detection(self, station: Station, event: SecurityEvent) -> None:
        """Wait for the recording of ``event``, a detection of a camera paired to
        ``station`` that ``station.needs_recording_match`` named."""
        device_sn = event.device_sn
        if device_sn is None:
            return
        serial = redact_serial(device_sn)
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
        try:
            await self._async_match(station, event, device_sn, slot)
        except Exception:
            # Logged here: an entry background task's error is never retrieved.
            _LOGGER.exception("Unexpected error handling the recording of a detection")

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
        if key in self._handled:
            _LOGGER.debug(
                "Recording of a detection of %s: the recording of an earlier detection,"
                " already handled",
                serial,
            )
            return
        self._handled[key] = None
        while len(self._handled) > _HANDLED_KEPT:
            self._handled.popitem(last=False)
        _LOGGER.debug("Recording of a detection of %s: settled", serial)
        await self._async_frame(station, event, record, slot)
        if self._recordings is not None:
            self._recordings.async_matched(station.serial, record)

    async def _async_frame(
        self, station: Station, event: SecurityEvent, record: HistoryRecord, slot: int | None
    ) -> None:
        """The trigger frame of ``record`` in the detection's slot; back once it is shown,
        skipped or failed."""
        if slot is None or self._snapshots is None:
            return
        try:
            named = station.event_with_recording(event, record)
        except ValueError:
            _LOGGER.debug(
                "Recording of a detection of %s: no trigger frame, the row names no recording",
                redact_serial(event.device_sn),
            )
            return
        await self._snapshots.async_request_matched_frame(station, named, slot)
