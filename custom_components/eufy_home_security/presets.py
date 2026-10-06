"""A pan/tilt camera's preset slots and the image of each one.

The library owns the presets: it reads the slots (``Station.async_refresh_presets``),
turns the camera to one and takes that view's live keyframe
(``Station.async_preset_image``), keeps them in the session cache, re-reads them
whenever the camera is awake for an image, and emits ``PresetsChanged`` when they
differ. This module only decides what Home Assistant shows, and when it asks:

- **Slots from the cache, never a wake at setup.** Entities are built from
  ``Station.presets(device_sn)``, which does no I/O: the slots in memory, else the
  session cache, else None for a camera never read. A camera never read gets its
  "Refresh presets" button only; the first press of it, or any live or preset
  capture (the library re-reads while the camera is awake), populates the slots once,
  ``PresetsChanged`` reaches the router, and the platforms add the slot entities
  without a restart. From then on every restart has them from the cache.
- **One capture per camera, decided by the library.** ``Station.async_preset_image``
  holds the camera; a call for the same preset joins the running one and any other
  capture raises ``DeviceBusyError`` before anything is sent. A press asks the same
  questions first (``Station.is_capturing``), so a busy camera is a translated toast
  raised synchronously from the press, nothing is sent, and no entity ever goes
  unavailable for it. Presses are never queued: a queued second preset would turn
  the camera later, unasked. A same-preset press joins the running capture.
- **A press returns at once.** The capture itself runs as a background task under a
  HA-side cap, decodes the keyframe with Home Assistant's ffmpeg, stores the JPEG in
  memory and tells the preset's image entity through the dispatcher. A failure keeps
  the previous image, is logged at DEBUG only, and is never retried.
- **Refresh presets recaptures.** A "Refresh presets" press reads the slots, then
  captures every enabled slot in index order, one at a time, each exactly as its
  "Capture preset n" press would. The camera stays awake for the whole run (one turn
  and settle per slot) and returns to its default preset once idle. While the run
  captures a slot, that slot counts as the running capture: a press for it joins, a
  press for another slot or a live capture is refused. A capture already running when
  the run reaches its captures is waited for first.
- **Small copies.** Every stored image also has a small copy in the still cache
  (``small_image_for``), which the image entity serves as its entity picture.
- **A saved slot forgets its image.** Saving a view into a slot (``ptz.py``) replaces
  what the slot holds, so its image, small copy and cached file are dropped at once
  and its image entity shows no image (state unknown) until the next "Capture preset
  n" or "Refresh presets". A deleted slot's image goes the same way. Nothing is
  captured by itself: a capture would turn the camera and end a running live view.
- **Entities are never deleted.** A slot the latest read shows disabled makes its
  entities unavailable while they stay in the registry; a slot that appears gains
  its entities. Unique ids are keyed by the slot index the library reports, never
  renumbered.
- **Nothing on the view path.** ``image_for`` returns what is cached; a view never
  waits on the station. Memory serves every view; each captured image is also
  written to Home Assistant's cache directory and restored, with its time, at the
  next setup (``still_cache.py``).

Every decision is logged at DEBUG with redacted serials, slot indexes, counts,
exception type names, byte counts and durations only; never a path, a record id or
an account id. Nothing here signs in.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.util import dt as dt_util
from homeassistant.util.signal_type import SignalType

from eufy_home_security import (
    CameraImage,
    DeviceBusyError,
    EufySecurityError,
    PresetsChanged,
    Station,
    redact_serial,
)

from . import detections, errors, snapshots, still_cache
from .const import (
    DOMAIN,
    FFMPEG_DECODE_TIMEOUT_SECONDS,
    PRESET_CAPTURE_TIMEOUT_SECONDS,
    PRESET_REFRESH_TIMEOUT_SECONDS,
)

if TYPE_CHECKING:
    from .history import EventHistory
    from .runtime import EufyConfigEntry
    from .still_cache import StillCache

_LOGGER = logging.getLogger(__name__)


def preset_image_key(index: int) -> str:
    """The unique-id key of slot ``index``'s image entity, by the library's slot index."""
    return f"preset_{index}_image"


def preset_still_name(index: int) -> str:
    """The still-cache name of slot ``index``'s image."""
    return f"preset_{index}"


def _preset_index(name: str) -> int | None:
    """The slot index of a still-cache name; None for any other name."""
    prefix, _, index = name.partition("_")
    return int(index) if prefix == "preset" and index.isdigit() else None


def preset_capture_key(index: int) -> str:
    """The unique-id key of slot ``index``'s capture button, by the library's slot index."""
    return f"preset_{index}_capture"


def presets_signal(entry_id: str, device_sn: str) -> SignalType[()]:
    """The dispatcher signal telling a camera's preset entities that its slots changed.

    The name is never logged or stored.
    """
    return SignalType(f"{DOMAIN}_presets_{entry_id}_{device_sn}")


def preset_image_signal(entry_id: str, device_sn: str, index: int) -> SignalType[()]:
    """The dispatcher signal telling one preset image entity that it has a new image."""
    return SignalType(f"{DOMAIN}_preset_image_{entry_id}_{device_sn}_{index}")


def enabled_indexes(station: Station, device_sn: str) -> frozenset[int]:
    """The indexes of the camera's enabled slots as last read; empty when never read.

    Reads the library's own dataclass fields only, and does no I/O.
    """
    known = station.presets(device_sn)
    if known is None:
        return frozenset()
    return frozenset(slot.index for slot in known if slot.enabled)


@callback
def async_add_slot_entities(
    hass: HomeAssistant,
    entry: EufyConfigEntry,
    station: Station,
    device_sn: str,
    add_slot: Callable[[int], None],
) -> None:
    """Call ``add_slot`` once per enabled slot, now and whenever the slots change.

    Checked now from the cached slots (no wake) and on every ``PresetsChanged``
    after, so a camera read for the first time gains its entities without a
    restart. A slot that is later disabled keeps its entities, which go unavailable;
    nothing is ever removed.
    """
    added: set[int] = set()

    @callback
    def _check() -> None:
        enabled = enabled_indexes(station, device_sn)
        new = sorted(enabled - added)
        for index in new:
            added.add(index)
            add_slot(index)
        _LOGGER.debug(
            "Preset entities of %s: added slots %s, %d now unavailable",
            redact_serial(device_sn),
            new,
            len(added - enabled),
        )

    _check()
    entry.async_on_unload(
        async_dispatcher_connect(hass, presets_signal(entry.entry_id, device_sn), _check)
    )


class PresetManager:
    """The preset images of one entry's pan/tilt cameras, one capture per camera."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: EufyConfigEntry,
        *,
        yield_media: Callable[[str], Awaitable[None]],
        cache: StillCache | None = None,
        history: EventHistory | None = None,
    ) -> None:
        """``yield_media`` ends the live view holding a station's media slot (``async_open_media``).

        A coroutine function taking a **station serial**. Injected so this module never
        reads ``entry.runtime_data`` (absent while setup runs) and never imports
        ``streaming.py``: the dependency runs one way only.
        """
        self._hass = hass
        self._entry = entry
        self._yield_media = yield_media
        self._cache = cache
        self._history = history
        # Per (camera serial, slot index): the JPEG shown and when it was taken.
        self._images: dict[tuple[str, int], tuple[bytes, datetime]] = {}
        # Per camera serial: the one running preset job and the slot it captures.
        self._captures: dict[str, tuple[int, asyncio.Task[None]]] = {}
        # Per camera serial: the one running slot read.
        self._refreshes: dict[str, asyncio.Task[None]] = {}
        self._stopped = False
        _LOGGER.debug("Preset manager started")

    @callback
    def async_restore(self, stills: Mapping[str, tuple[bytes, Mapping[str, Any]]]) -> None:
        """Show the preset images the cache kept, before the image entities read them."""
        for key, (image, meta) in stills.items():
            device_sn, _, name = key.partition(".")
            index = _preset_index(name)
            taken = dt_util.parse_datetime(str(meta.get("taken", "")))
            if index is None or taken is None or taken.tzinfo is None:
                continue
            self._images.setdefault((device_sn, index), (image, taken))

    def image_for(self, device_sn: str, index: int) -> tuple[bytes, datetime] | None:
        """The JPEG a preset image shows and when it was taken; None before one. Never fetches."""
        return self._images.get((device_sn, index))

    async def small_image_for(self, device_sn: str, index: int) -> bytes | None:
        """The small copy of the image a preset shows; None before one or when it does
        not decode. Never fetches from the station."""
        cached = self._images.get((device_sn, index))
        if cached is None or self._cache is None:
            return None
        return await self._cache.async_small(
            still_cache.still_key(device_sn, preset_still_name(index)), cached[0]
        )

    @callback
    def async_forget_image(self, device_sn: str, index: int) -> None:
        """Drop slot ``index``'s image: the slot no longer holds the view it shows.

        Memory, the cached file and its small copy go; the image entity is told, so
        its state turns unknown and its picture URL goes. The history keeps its file.
        """
        known = self._images.pop((device_sn, index), None)
        if self._cache is not None:
            self._cache.async_forget(still_cache.still_key(device_sn, preset_still_name(index)))
        _LOGGER.debug(
            "Preset %d of %s: image dropped (%s)",
            index,
            redact_serial(device_sn),
            "had one" if known is not None else "had none",
        )
        async_dispatcher_send(
            self._hass, preset_image_signal(self._entry.entry_id, device_sn, index)
        )

    def capturing_index(self, device_sn: str) -> int | None:
        """The slot this manager's running preset job of the camera captures; None for none."""
        running = self._captures.get(device_sn)
        return running[0] if running is not None else None

    @callback
    def async_request_preset(self, station: Station, device_sn: str, index: int) -> None:
        """A "Capture preset n" press or the capture_preset action: returns at once or raises.

        Nothing is sent from here. A camera the library holds for another capture
        raises the busy toast, a slot the last read showed unset raises
        ``preset_not_set``, a model without presets ``presets_unsupported``
        (reachable from the action only). A press for the slot already being captured
        joins that capture. Otherwise the capture starts as a background task.
        """
        serial = redact_serial(device_sn)
        if self._stopped:
            _LOGGER.debug(
                "Capture preset %d of %s pressed, ignored: preset manager stopped", index, serial
            )
            return
        if not detections.has_preset_entities(device_sn):
            _LOGGER.debug(
                "Capture preset %d of %s refused: the model has no presets", index, serial
            )
            raise errors.presets_unsupported()
        known = station.presets(device_sn)
        if known is not None and index not in enabled_indexes(station, device_sn):
            _LOGGER.debug(
                "Capture preset %d of %s refused: slot not set, nothing sent", index, serial
            )
            raise errors.preset_not_set(index)
        if self.capturing_index(device_sn) == index:
            _LOGGER.debug(
                "Capture preset %d of %s pressed, joined the running capture of preset %d",
                index,
                serial,
                index,
            )
            return
        if station.is_capturing(device_sn):
            busy = self.capturing_index(device_sn)
            _LOGGER.debug(
                "Capture preset %d of %s refused: camera busy (%s), nothing sent",
                index,
                serial,
                busy if busy is not None else "live",
            )
            raise errors.capture_in_progress()
        _LOGGER.debug("Capture preset %d of %s accepted", index, serial)
        task = self._entry.async_create_background_task(
            self._hass,
            self._async_capture(station, device_sn, index),
            name=f"{DOMAIN} preset image {serial} {index}",
        )
        self._captures[device_sn] = (index, task)

    async def _async_capture(self, station: Station, device_sn: str, index: int) -> None:
        """One preset image: turn, settle, keyframe, decode, store, tell the entity."""
        try:
            await self._async_capture_inner(station, device_sn, index)
        except asyncio.CancelledError:
            raise
        except Exception:
            # One unexpected failure must never escape a background task.
            _LOGGER.exception("Unexpected error taking a preset image")
        finally:
            running = self._captures.get(device_sn)
            if running is not None and running[1] is asyncio.current_task():
                del self._captures[device_sn]

    async def _async_capture_inner(self, station: Station, device_sn: str, index: int) -> None:
        serial = redact_serial(device_sn)
        start = time.monotonic()

        async def fetch(wait: bool) -> CameraImage:
            # The library's default settle: eufy timing stays there.
            async with asyncio.timeout(PRESET_CAPTURE_TIMEOUT_SECONDS):
                return await station.async_preset_image(device_sn, index, wait=wait)

        try:
            image = await snapshots.async_open_media(station, self._yield_media, fetch)
        except DeviceBusyError:
            _LOGGER.debug(
                "Preset %d of %s: busy after all (race), nothing sent, in %.1f s",
                index,
                serial,
                _elapsed(start),
            )
            return
        except (EufySecurityError, TimeoutError) as err:
            _LOGGER.debug(
                "Preset %d of %s: failed (%s) in %.1f s, keeping the previous image",
                index,
                serial,
                errors.failure_reason(err),
                _elapsed(start),
            )
            return
        _LOGGER.debug(
            "Preset %d of %s: fetched %d bytes in %.1f s, decode started",
            index,
            serial,
            len(image.data),
            _elapsed(start),
        )
        start = time.monotonic()
        # Through the snapshots module attributes: the tests replace ffmpeg_command there.
        jpeg = (
            image.data
            if image.is_jpeg
            else await snapshots.async_hevc_to_jpeg(
                snapshots.ffmpeg_command(self._hass), image.data, FFMPEG_DECODE_TIMEOUT_SECONDS
            )
        )
        if jpeg is None:
            _LOGGER.debug(
                "Preset %d of %s: decode failed in %.1f s, keeping the previous image",
                index,
                serial,
                _elapsed(start),
            )
            return
        if self._stopped:
            _LOGGER.debug(
                "Preset %d of %s: decode ok, %d bytes in %.1f s, superseded by stop",
                index,
                serial,
                len(jpeg),
                _elapsed(start),
            )
            return
        previous = self._images.get((device_sn, index))
        taken = dt_util.utcnow()
        self._images[(device_sn, index)] = (jpeg, taken)
        if self._cache is not None:
            key = still_cache.still_key(device_sn, preset_still_name(index))
            self._cache.async_save(key, jpeg, {"taken": taken.isoformat()})
            # Rendered before the entity is told, so its first picture request is served
            # from memory; the writer reuses it for the file.
            await self._cache.async_small(key, jpeg)
        if self._history is not None:
            self._history.async_save(device_sn, jpeg, taken, preset_still_name(index))
        _LOGGER.debug(
            "Preset %d of %s now shows %d bytes (was %s), decoded in %.1f s",
            index,
            serial,
            len(jpeg),
            len(previous[0]) if previous is not None else None,
            _elapsed(start),
        )
        async_dispatcher_send(
            self._hass, preset_image_signal(self._entry.entry_id, device_sn, index)
        )

    @callback
    def async_request_refresh_presets(self, station: Station, device_sn: str) -> None:
        """A "Refresh presets" press: one slot read, then one capture per enabled slot.

        Wakes a battery camera. Returns at once. A press while this camera's refresh
        runs joins it. The entities of new slots are added by the ``PresetsChanged``
        path, not here; a failed read captures nothing, and nothing is retried.
        """
        serial = redact_serial(device_sn)
        if self._stopped:
            _LOGGER.debug("Refresh presets of %s pressed, ignored: preset manager stopped", serial)
            return
        running = self._refreshes.get(device_sn)
        if running is not None and not running.done():
            _LOGGER.debug("Refresh presets of %s pressed, joined the running read", serial)
            return
        _LOGGER.debug(
            "Refresh presets of %s pressed: reading slots and recapturing them "
            "(wakes a battery camera)",
            serial,
        )
        self._refreshes[device_sn] = self._entry.async_create_background_task(
            self._hass,
            self._async_refresh(station, device_sn),
            name=f"{DOMAIN} preset slots {serial}",
        )

    async def _async_refresh(self, station: Station, device_sn: str) -> None:
        serial = redact_serial(device_sn)
        start = time.monotonic()
        try:
            async with asyncio.timeout(PRESET_REFRESH_TIMEOUT_SECONDS):
                slots = await station.async_refresh_presets(device_sn)
        except (EufySecurityError, TimeoutError) as err:
            _LOGGER.debug(
                "Refresh presets of %s: failed (%s) in %.1f s",
                serial,
                errors.failure_reason(err),
                _elapsed(start),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("Unexpected error reading preset slots")
        else:
            enabled = sorted(slot.index for slot in slots if slot.enabled)
            _LOGGER.debug(
                "Refresh presets of %s: read %d slots, enabled %s, in %.1f s",
                serial,
                len(slots),
                enabled,
                _elapsed(start),
            )
            await self._async_recapture(station, device_sn, enabled)
        finally:
            if self._refreshes.get(device_sn) is asyncio.current_task():
                del self._refreshes[device_sn]

    async def _async_recapture(self, station: Station, device_sn: str, indexes: list[int]) -> None:
        """Capture each slot of ``indexes`` in turn, as the running capture of the camera."""
        this = asyncio.current_task()
        assert this is not None
        for index in indexes:
            running = self._captures.get(device_sn)
            if running is not None and running[1] is not this and not running[1].done():
                _LOGGER.debug(
                    "Refresh presets of %s: waiting for the running capture of preset %d",
                    redact_serial(device_sn),
                    running[0],
                )
                await asyncio.wait([running[1]])
            if self._stopped:
                return
            self._captures[device_sn] = (index, this)
            try:
                await self._async_capture_inner(station, device_sn, index)
            except asyncio.CancelledError:
                raise
            except Exception:
                # One slot's unexpected failure never stops the others.
                _LOGGER.exception("Unexpected error taking a preset image")
            finally:
                if self._captures.get(device_sn) == (index, this):
                    del self._captures[device_sn]

    @callback
    def async_apply_presets(self, event: PresetsChanged, *, dispatch: bool) -> None:
        """The slots of a camera changed: tell its platforms, which re-read the station.

        The station already holds the new slots when the event arrives, so nothing
        is stored here. Outside the consuming window nothing is dispatched: no
        entity listens yet, and setup re-reads ``Station.presets`` anyway.
        """
        _LOGGER.debug(
            "Presets of %s changed: %d slots, enabled %s%s",
            redact_serial(event.device_sn),
            len(event.presets),
            sorted(slot.index for slot in event.presets if slot.enabled),
            "" if dispatch else " (not dispatched: no entity listening)",
        )
        if dispatch:
            async_dispatcher_send(self._hass, presets_signal(self._entry.entry_id, event.device_sn))

    async def async_stop(self) -> None:
        """Cancel every running capture and read; a request after this is ignored."""
        self._stopped = True
        _LOGGER.debug(
            "Preset manager stopping: %d capture(s) and %d slot read(s) running",
            sum(1 for _, task in self._captures.values() if not task.done()),
            sum(1 for task in self._refreshes.values() if not task.done()),
        )
        tasks = [task for _, task in self._captures.values()]
        tasks.extend(self._refreshes.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task


def _elapsed(start: float) -> float:
    """Seconds since ``start`` on the real monotonic clock, for a DEBUG duration."""
    return time.monotonic() - start
