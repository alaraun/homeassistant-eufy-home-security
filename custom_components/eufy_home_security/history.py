"""Every stored still as a browsable file history in Home Assistant's media folder.

Files land under the ``local`` media directory (``/media`` in a container, else
``<config>/media``), so **Media > My media** browses them without any extra code:

    eufy_home_security/<camera name>/<YYYY-MM-DD_HH-MM-SS>_<camera name>_<kind>.jpg

``<camera name>`` is the device's name in Home Assistant (a user's rename included) made
file-safe, with ``_<last 4 of serial>`` added when two of the entry's cameras clean to the
same name; the date and time are the still's own moment in Home Assistant's time zone: a
detection's own time, a refreshed recording's start, a live capture's arrival. ``<kind>``
is the detection (``person``, ``motion``, ...), ``event`` for a refreshed or unclassified
event, ``live`` or ``preset_<n>``. A detection's trigger frame overwrites its thumbnail.

Videos sit beside the stills under the same name with ``.mp4``: a HomeBase recording
(``recordings.py``) takes the stamp and kind of its detection's still, so a browser
pairs the two by name; a live clip of the ``record`` action is ``live``.

- **Writes never block the loop.** One executor write at a time per file; stills that
  arrive during a write coalesce to the newest. Files are written by temp file and
  rename, mode 0644, so a failed write never leaves a torn JPEG.
- **Videos are never encoded.** A clip's MPEG-TS goes to a hidden ``.part`` file in the
  camera folder, Home Assistant's ffmpeg copies its streams into an MP4 (``-c copy``,
  bounded), and the MP4 is renamed into place. Any failure removes both temp files.
- **Retention.** History files dated before the kept days are deleted at setup and
  once a day, by the date in their name, and ``.part`` files left over a day; a camera
  folder left empty is removed. Other files are never touched.
- **Persistence.** In a container ``/media`` is only kept when it is mounted from the
  host. ``media_is_persistent`` reads the container's mount table, and setup raises a
  repair issue naming the path when history is on and nothing is mounted there.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import tempfile
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import IO, TYPE_CHECKING, Final

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util
from homeassistant.util.package import is_docker_env

from eufy_home_security import ClipWriter, MediaClip, redact_serial

from . import snapshots
from .const import CLIP_REMUX_TIMEOUT_SECONDS, DOMAIN

if TYPE_CHECKING:
    from .runtime import EufyConfigEntry

_LOGGER = logging.getLogger(__name__)

# The folder under the media directory that holds every camera's history.
HISTORY_FOLDER: Final = DOMAIN
STILL_SUFFIX: Final = ".jpg"
CLIP_SUFFIX: Final = ".mp4"
_PRUNE_INTERVAL: Final = timedelta(days=1)
_FILE: Final = re.compile(r"(\d{4}-\d{2}-\d{2})_\d{2}-\d{2}-\d{2}_.+\.(?:jpg|mp4)")
_UNSAFE: Final = re.compile(r"[^\w-]+")
_NAME_MAX: Final = 60
_FALLBACK_NAME: Final = "camera"
# A clip's temp files: the MPEG-TS as it arrives and the MP4 before its rename.
_PART_SUFFIX: Final = ".part"
_STALE_PART: Final = timedelta(days=1)
# ffmpeg around the input and output paths: copy every stream into an MP4, never
# encode. ``hvc1`` makes HEVC playable in Safari and Home Assistant's media browser.
REMUX_ARGS_BEFORE: Final[tuple[str, ...]] = ("-hide_banner", "-loglevel", "error", "-y", "-i")
REMUX_ARGS_AFTER: Final[tuple[str, ...]] = (
    "-map",
    "0",
    "-c",
    "copy",
    "-tag:v",
    "hvc1",
    "-movflags",
    "+faststart",
    "-f",
    "mp4",
)
# How many detections' still names are kept for pairing a recording with its still.
_NOTES_MAX: Final = 2000


class ClipStoreError(Exception):
    """A clip arrived but could not be stored: no ffmpeg, a failed remux or a timeout."""


class IncompleteClipError(Exception):
    """A clip that had to be complete was not; nothing was kept."""

    def __init__(self, clip: MediaClip) -> None:
        super().__init__("the clip is not complete")
        self.clip = clip


@dataclass(frozen=True, slots=True)
class SavedClip:
    """A clip stored in the history: its MP4 and what the library wrote."""

    path: Path
    clip: MediaClip


def media_dir(hass: HomeAssistant) -> Path:
    """Home Assistant's ``local`` media directory, else the first one configured."""
    dirs = hass.config.media_dirs
    return Path(dirs.get("local") or next(iter(dirs.values()), hass.config.path("media")))


def history_dir(hass: HomeAssistant) -> Path:
    """The folder holding every camera's history."""
    return media_dir(hass) / HISTORY_FOLDER


def media_content_id(hass: HomeAssistant, path: Path) -> str:
    """The ``media-source://`` id Home Assistant's local media source serves ``path`` under."""
    dirs = hass.config.media_dirs
    source = "local" if "local" in dirs else next(iter(dirs), "local")
    relative = path.relative_to(media_dir(hass)).as_posix()
    return f"media-source://media_source/{source}/{relative}"


def safe_name(name: str | None) -> str:
    """``name`` as one file-name part: letters, digits, ``_`` and ``-`` only."""
    cleaned = _UNSAFE.sub("_", name or "").strip("_")[:_NAME_MAX].strip("_")
    return cleaned or _FALLBACK_NAME


def history_path(
    root: Path, camera_name: str, moment: datetime, kind: str, suffix: str = STILL_SUFFIX
) -> Path:
    """Where the file of ``camera_name`` at ``moment`` (local time) of ``kind`` is written."""
    camera = safe_name(camera_name)
    stamp = moment.strftime("%Y-%m-%d_%H-%M-%S")
    return root / camera / f"{stamp}_{camera}_{safe_name(kind)}{suffix}"


def media_is_persistent(path: Path, mountinfo: Path = Path("/proc/self/mountinfo")) -> bool:
    """Whether files under ``path`` outlive the container.

    Outside a container every path is. In one, ``path`` must lie on a mount other than
    the container's root file system; an unreadable mount table counts as persistent,
    so no false warning is raised.
    """
    if not is_docker_env():
        return True
    try:
        lines = mountinfo.read_text().splitlines()
    except OSError:
        return True
    target = os.path.normpath(path)
    best = ""
    for line in lines:
        fields = line.split()
        if len(fields) < 5:
            continue
        point = _unescape(fields[4])
        if (target == point or target.startswith(point.rstrip("/") + "/")) and len(point) > len(
            best
        ):
            best = point
    return best not in ("", "/")


def _unescape(field: str) -> str:
    """A mountinfo field with its octal escapes (``\\040`` for a space) decoded."""
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)


def camera_name(hass: HomeAssistant, entry_id: str, device_sn: str) -> str:
    """The camera's name as Home Assistant shows it; a fixed word when it has none."""
    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, device_sn), entry_id)
    if device is None:
        return _FALLBACK_NAME
    return device.name_by_user or device.name or _FALLBACK_NAME


class EventHistory:
    """One config entry's still and clip files, written through and pruned by age."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: EufyConfigEntry,
        *,
        days: int,
        cameras: Iterable[str] = (),
    ) -> None:
        """``days`` is how many days are kept; 0 turns the history off.

        ``cameras`` are the serials of the entry's cameras, the set :meth:`camera_folder`
        checks for colliding names.
        """
        self._hass = hass
        self._entry = entry
        self._days = days
        self._cameras = frozenset(cameras)
        self._root = history_dir(hass)
        self._pending: dict[Path, bytes] = {}
        self._writers: dict[Path, asyncio.Task[None]] = {}
        self._failed: set[str] = set()
        # Per detection record id, the stamp and kind its still was written under.
        self._still_names: dict[int, tuple[datetime, str]] = {}

    @property
    def enabled(self) -> bool:
        """Whether stills are written at all."""
        return self._days > 0

    @property
    def days(self) -> int:
        """How many days are kept; 0 when the history is off."""
        return self._days

    @property
    def root(self) -> Path:
        """The folder the history is written to."""
        return self._root

    def camera_folder(self, device_sn: str) -> str:
        """The camera's folder and file-name part: its file-safe name, plus
        ``_<last 4 of serial>`` when another of the entry's cameras cleans to the same name."""
        entry_id = self._entry.entry_id
        folder = safe_name(camera_name(self._hass, entry_id, device_sn))
        if not any(
            serial != device_sn and safe_name(camera_name(self._hass, entry_id, serial)) == folder
            for serial in self._cameras
        ):
            return folder
        tail = safe_name(device_sn[-4:])
        return f"{folder[: _NAME_MAX - len(tail) - 1].rstrip('_')}_{tail}"

    def oldest_kept(self) -> date:
        """The first day the retention keeps."""
        return dt_util.now().date() - timedelta(days=max(self._days, 1) - 1)

    @callback
    def async_start(self) -> None:
        """Prune once now and then daily, until the entry unloads."""
        if not self.enabled:
            return
        self._async_schedule_prune()
        self._entry.async_on_unload(
            async_track_time_interval(
                self._hass, lambda _now: self._async_schedule_prune(), _PRUNE_INTERVAL
            )
        )

    @callback
    def async_save(
        self,
        device_sn: str,
        image: bytes,
        moment: datetime,
        kind: str,
        *,
        record_id: int | None = None,
    ) -> None:
        """Queue ``image`` as the camera's still of ``kind`` at ``moment``.

        ``record_id`` (a detection's history row) is noted with the still's stamp and
        kind, so that recording's video gets the same name (:meth:`still_name`).
        """
        if not self.enabled:
            return
        local = dt_util.as_local(moment)
        if record_id is not None:
            self._still_names.pop(record_id, None)
            self._still_names[record_id] = (local, kind)
            while len(self._still_names) > _NOTES_MAX:
                del self._still_names[next(iter(self._still_names))]
        path = history_path(self._root, self.camera_folder(device_sn), local, kind)
        self._pending[path] = image
        writer = self._writers.get(path)
        if writer is None or writer.done():
            self._writers[path] = self._entry.async_create_task(
                self._hass, self._async_write(path, device_sn), name=f"{DOMAIN} history write"
            )

    def still_name(self, record_id: int) -> tuple[datetime, str] | None:
        """The local stamp and kind of the still written for ``record_id``; None if none."""
        return self._still_names.get(record_id)

    async def async_save_clip(
        self,
        device_sn: str,
        produce: Callable[[ClipWriter], Awaitable[MediaClip]],
        *,
        kind: str,
        moment: datetime | None = None,
        require_complete: bool = False,
    ) -> SavedClip:
        """Store the clip ``produce(write)`` writes as the camera's MP4 of ``kind``.

        ``produce`` is a library call (a recording download or a live capture) that
        writes MPEG-TS through ``write``. The TS lands in a hidden ``.part`` file in
        the camera folder, ffmpeg copies it into an MP4 (no encode), and the MP4 is
        renamed to ``<stamp>_<camera>_<kind>.mp4``, mode 0644. ``moment`` names it;
        None takes the clip's own ``started_at``, else now.

        Raises what ``produce`` raises, :class:`IncompleteClipError` for a clip that
        ``require_complete`` and is not, :class:`ClipStoreError` for a failed remux,
        ``OSError`` for the media folder. Nothing is left behind on any failure.
        """
        if not self.enabled:
            raise ClipStoreError("the event history is off")
        name = self.camera_folder(device_sn)
        folder = self._root / name
        ts_path, handle = await self._hass.async_add_executor_job(_open_part, folder, ".ts")
        mp4_tmp = ts_path.with_name(
            ts_path.name.removesuffix(f".ts{_PART_SUFFIX}") + f"{CLIP_SUFFIX}{_PART_SUFFIX}"
        )
        try:
            try:

                async def write(chunk: bytes) -> None:
                    await self._hass.async_add_executor_job(handle.write, chunk)

                clip = await produce(write)
            finally:
                await self._hass.async_add_executor_job(handle.close)
            if require_complete and not clip.complete:
                raise IncompleteClipError(clip)
            when = moment or clip.started_at or dt_util.utcnow()
            path = history_path(self._root, name, dt_util.as_local(when), kind, CLIP_SUFFIX)
            await _async_remux(snapshots.ffmpeg_command(self._hass), ts_path, mp4_tmp)
            await self._hass.async_add_executor_job(_publish, mp4_tmp, path)
        except BaseException:
            await asyncio.shield(self._hass.async_add_executor_job(_remove, ts_path, mp4_tmp))
            raise
        await self._hass.async_add_executor_job(_remove, ts_path)
        _LOGGER.debug(
            "Event history: %s clip of %s stored, %d frames, %.1f s, complete %s",
            kind,
            redact_serial(device_sn),
            clip.video_frames,
            clip.duration_s,
            clip.complete,
        )
        return SavedClip(path, clip)

    async def _async_write(self, path: Path, device_sn: str) -> None:
        try:
            while (image := self._pending.pop(path, None)) is not None:
                try:
                    await self._hass.async_add_executor_job(_write, path, image)
                except OSError as err:
                    if device_sn not in self._failed:
                        self._failed.add(device_sn)
                        _LOGGER.warning(
                            "Could not write the event history of %s under %s: %s",
                            redact_serial(device_sn),
                            self._root,
                            err,
                        )
                    continue
                self._failed.discard(device_sn)
        finally:
            self._writers.pop(path, None)

    @callback
    def _async_schedule_prune(self) -> None:
        oldest = dt_util.now().date() - timedelta(days=self._days - 1)
        self._entry.async_create_background_task(
            self._hass, self._async_prune(oldest), name=f"{DOMAIN} history prune"
        )

    async def _async_prune(self, oldest: date) -> None:
        try:
            await self._hass.async_add_executor_job(prune, self._root, oldest)
        except OSError as err:
            _LOGGER.warning("Could not prune the event history under %s: %s", self._root, err)


def _expired(item: Path, oldest: date) -> bool:
    """Whether ``item`` is a history file dated before ``oldest``, or a stale temp file."""
    if match := _FILE.fullmatch(item.name):
        try:
            return date.fromisoformat(match.group(1)) < oldest
        except ValueError:
            return False
    temp = item.name.endswith(".tmp")
    if not item.name.startswith(".") or not (temp or item.name.endswith(_PART_SUFFIX)):
        return False
    try:
        changed = dt_util.utc_from_timestamp(item.stat().st_mtime)
    except OSError:
        # Renamed or removed by a running write since the folder was listed.
        return False
    if temp:
        return dt_util.as_local(changed).date() < oldest
    return dt_util.utcnow() - changed > _STALE_PART


def _open_part(folder: Path, suffix: str) -> tuple[Path, IO[bytes]]:
    """A new hidden temp file ``.<random><suffix>.part`` in ``folder``, open for writing."""
    folder.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".", suffix=f"{suffix}{_PART_SUFFIX}")
    return Path(tmp), os.fdopen(fd, "wb")


def _publish(tmp: Path, path: Path) -> None:
    """Make ``tmp`` readable (0644) and rename it to ``path``."""
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def _remove(*paths: Path | None) -> None:
    for path in paths:
        if path is not None:
            with contextlib.suppress(OSError):
                path.unlink()


async def _async_remux(command: Sequence[str], source: Path, target: Path) -> None:
    """Copy the streams of the MPEG-TS ``source`` into the MP4 ``target`` with ffmpeg.

    An asyncio subprocess, bounded by ``CLIP_REMUX_TIMEOUT_SECONDS``; never an encode.
    Raises :class:`ClipStoreError` when ffmpeg is missing, fails or overruns; a
    cancelled remux never leaves the process running.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *command,
            *REMUX_ARGS_BEFORE,
            str(source),
            *REMUX_ARGS_AFTER,
            str(target),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as err:
        raise ClipStoreError("no usable ffmpeg to store a clip") from err
    try:
        async with asyncio.timeout(CLIP_REMUX_TIMEOUT_SECONDS):
            _, stderr = await proc.communicate()
    except TimeoutError as err:
        raise ClipStoreError(
            f"ffmpeg did not finish within {CLIP_REMUX_TIMEOUT_SECONDS} s"
        ) from err
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
    if proc.returncode != 0:
        message = stderr.decode(errors="replace").strip().splitlines()[-1:] or [""]
        raise ClipStoreError(f"ffmpeg exit {proc.returncode}: {message[0][:200]}")


def _write(path: Path, image: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(image)
            os.fchmod(handle.fileno(), 0o644)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def prune(root: Path, oldest: date) -> int:
    """Delete history files dated before ``oldest``; returns how many.

    Only ``<stamp>_<name>_<kind>.jpg|.mp4`` files go, by the date in their name, temp
    files last changed before ``oldest`` and clip ``.part`` files over a day old; a
    camera folder is removed once empty.
    """
    removed = 0
    if not root.is_dir():
        return 0
    for camera in root.iterdir():
        if not camera.is_dir():
            continue
        for item in camera.iterdir():
            if item.is_file() and _expired(item, oldest):
                item.unlink(missing_ok=True)
                removed += 1
        with contextlib.suppress(OSError):
            camera.rmdir()
    if removed:
        _LOGGER.debug("Event history: %d file(s) older than %s removed", removed, oldest)
    return removed
