"""Each shown still on disk, so the next Home Assistant start shows it again.

Home Assistant's cache directory holds data an integration can regenerate: it is not in
backups (``hass.config.cache_path``). Every still the snapshot and preset managers show
is written there, one entry per key under ``.cache/eufy_home_security/<entry id>/``:
``<key>.jpg`` and a ``<key>.json`` sidecar with the still's metadata and the JPEG's size.
Memory stays the serving path; the disk is written through on every new still and read
once, at setup, before the platforms add their entities.

Every still also has a **small copy**, ``small/<key>.jpg``: at most ``SMALL_WIDTH``
pixels wide, for tiles, buttons and entity badges (``async_small``). It is rendered in
the executor from the still it belongs to whenever that still is written, so each new
still (a detection, a refresh, a capture, a preset capture) replaces it. The sidecar's
``small`` holds its size; a small copy that is missing or does not match is rendered
again at load, and one without its still is deleted. A still that does not decode as a
JPEG has none, and its full still is served instead.

- **Writes never block the loop and never reorder.** Each key has one writer task that
  writes in the executor and always writes the newest still queued for it; stills that
  arrive while a write runs coalesce to the last one.
- **All or nothing per still.** Every file is written by temp file and rename, the JPEG
  first and the sidecar last; a sidecar whose size does not match its JPEG, or that does
  not parse, is dropped with its JPEG at load, and so is a leftover temp file. A failed write logs
  one warning per key and keeps the still in memory.
- **Private.** The files are mode 0600. A key is ``<serial>.<name>``; a serial or name
  with any character outside ``[A-Za-z0-9_-]`` is never written.
- **Pruned.** At load, entries of serials the entry does not pair are deleted; removing
  the config entry deletes its directory. A still that stops showing what it names
  (a preset slot saved again) is deleted with its small copy (``async_forget``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import tempfile
from collections.abc import Collection
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.json import json_bytes
from PIL import Image, UnidentifiedImageError

from eufy_home_security import redact_serial

from .const import DOMAIN

if TYPE_CHECKING:
    from .runtime import EufyConfigEntry

_LOGGER = logging.getLogger(__name__)

# The sidecar layout; a sidecar of another version is dropped at load.
CACHE_VERSION: Final = 1
_SAFE: Final = re.compile(r"[A-Za-z0-9_-]+")
# A small copy's width in pixels: a 16:9 tile up to 240 CSS pixels wide on a 2x screen.
SMALL_WIDTH: Final = 480
SMALL_QUALITY: Final = 75
SMALL_FOLDER: Final = "small"
# The largest still a small copy is made of, in pixels (two 4K frames).
_SMALL_MAX_SOURCE_PIXELS: Final = 2 * 3840 * 2160


def render_small(image: bytes) -> bytes | None:
    """``image`` as a JPEG at most ``SMALL_WIDTH`` wide; None when it does not decode.

    Blocking: runs in the executor. The JPEG decoder scales by a power of two while
    decoding (``draft``), so a 4K frame is never decoded at full size.
    """
    try:
        with Image.open(BytesIO(image)) as source:
            if source.format != "JPEG" or source.width * source.height > _SMALL_MAX_SOURCE_PIXELS:
                return None
            width = min(SMALL_WIDTH, source.width)
            height = max(1, round(source.height * width / source.width))
            source.draft("RGB", (width, height))
            picture = source.convert("RGB")
            if picture.size != (width, height):
                picture = picture.resize((width, height), Image.Resampling.LANCZOS)
            out = BytesIO()
            picture.save(out, "JPEG", quality=SMALL_QUALITY, optimize=True)
    except OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError:
        return None
    return out.getvalue()


def cache_dir(hass: HomeAssistant, entry_id: str) -> Path:
    """The directory holding one config entry's stills."""
    return Path(hass.config.cache_path(DOMAIN, entry_id))


def still_key(device_sn: str, name: str) -> str | None:
    """The cache key of one still of a device; None when either part is not file-safe."""
    if _SAFE.fullmatch(device_sn) is None or _SAFE.fullmatch(name) is None:
        return None
    return f"{device_sn}.{name}"


async def async_remove_entry_cache(hass: HomeAssistant, entry_id: str) -> None:
    """Delete a removed config entry's stills."""
    await hass.async_add_executor_job(
        lambda: shutil.rmtree(cache_dir(hass, entry_id), ignore_errors=True)
    )


class StillCache:
    """One config entry's stills on disk: loaded once, written through per key."""

    def __init__(self, hass: HomeAssistant, entry: EufyConfigEntry) -> None:
        self._hass = hass
        self._entry = entry
        self._dir = cache_dir(hass, entry.entry_id)
        # Per key: the newest still not yet written (None: delete the key's files), and
        # the task writing that key.
        self._pending: dict[str, tuple[bytes, dict[str, Any]] | None] = {}
        self._writers: dict[str, asyncio.Task[None]] = {}
        # Keys whose last write failed, so a full disk logs one warning per key.
        self._failed: set[str] = set()
        # Per key: the still a small copy was made of (matched by identity) and the
        # copy, None when that still does not decode.
        self._small: dict[str, tuple[bytes, bytes | None]] = {}

    async def async_load(self, serials: Collection[str]) -> dict[str, tuple[bytes, dict[str, Any]]]:
        """Every valid cached still of ``serials``, by key; deletes the rest.

        The small copies come back with them, rendered again where missing or stale.
        """
        loaded = await self._hass.async_add_executor_job(_load, self._dir, frozenset(serials))
        entries: dict[str, tuple[bytes, dict[str, Any]]] = {}
        for key, (image, meta, small) in loaded.items():
            entries[key] = (image, meta)
            self._small[key] = (image, small)
        _LOGGER.debug(
            "Still cache: %d still(s) restored, %d with a small copy",
            len(entries),
            sum(1 for _, small in self._small.values() if small is not None),
        )
        return entries

    async def async_small(self, key: str | None, image: bytes) -> bytes | None:
        """The small copy of ``image``, the still now shown under ``key``.

        None when ``image`` does not decode as a JPEG or ``key`` is None. Served from
        memory when made of this very still, else rendered once in the executor; the
        writer then reuses it for the file.
        """
        if key is None:
            return None
        known = self._small.get(key)
        if known is not None and known[0] is image:
            return known[1]
        small = await self._hass.async_add_executor_job(render_small, image)
        self._small[key] = (image, small)
        return small

    @callback
    def async_save(self, key: str | None, image: bytes, meta: dict[str, Any]) -> None:
        """Queue ``image`` and its metadata for ``key``; a no-op for a None key."""
        if key is None:
            return
        self._pending[key] = (image, meta)
        self._start_writer(key)

    @callback
    def async_forget(self, key: str | None) -> None:
        """Drop the still under ``key`` and its small copy, from memory and disk."""
        if key is None:
            return
        self._small.pop(key, None)
        self._pending[key] = None
        self._start_writer(key)

    @callback
    def _start_writer(self, key: str) -> None:
        writer = self._writers.get(key)
        if writer is None or writer.done():
            # A tracked task, not a background one: unload and shutdown wait for it.
            self._writers[key] = self._entry.async_create_task(
                self._hass, self._async_write(key), name=f"{DOMAIN} still cache write"
            )

    async def _async_write(self, key: str) -> None:
        while key in self._pending:
            pending = self._pending.pop(key)
            if pending is None:
                await self._hass.async_add_executor_job(_remove, self._dir, key)
                continue
            image, meta = pending
            known = self._small.get(key)
            rendered = known[1] if known is not None and known[0] is image else _RENDER
            try:
                small = await self._hass.async_add_executor_job(
                    _write, self._dir, key, image, meta, rendered
                )
            except OSError as err:
                if key not in self._failed:
                    self._failed.add(key)
                    _LOGGER.warning(
                        "Could not cache the still of %s on disk: %s",
                        redact_serial(key.split(".", 1)[0]),
                        err,
                    )
                continue
            self._failed.discard(key)
            self._small[key] = (image, small)


# Passed to ``_write`` for a still whose small copy is not rendered yet.
_RENDER: Final = object()


def small_path(directory: Path, key: str) -> Path:
    """Where the small copy of the still under ``key`` is kept."""
    return directory / SMALL_FOLDER / f"{key}.jpg"


def _write(
    directory: Path, key: str, image: bytes, meta: dict[str, Any], small: object
) -> bytes | None:
    """Write a still, its small copy (``small``, or rendered here) and its sidecar.

    Returns the small copy; None when the still does not decode, and then no small
    file is left behind.
    """
    directory.mkdir(parents=True, exist_ok=True)
    copy = render_small(image) if small is _RENDER else small
    assert copy is None or isinstance(copy, bytes)
    _replace(directory / f"{key}.jpg", image)
    sidecar: dict[str, Any] = {**meta, "version": CACHE_VERSION, "size": len(image)}
    _write_small(directory, key, copy, sidecar)
    _replace(directory / f"{key}.json", json_bytes(sidecar))
    return copy


def _remove(directory: Path, key: str) -> None:
    """Delete the still under ``key``, its sidecar and its small copy; missing is fine."""
    for path in (directory / f"{key}.json", directory / f"{key}.jpg", small_path(directory, key)):
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)


def _write_small(directory: Path, key: str, copy: bytes | None, sidecar: dict[str, Any]) -> None:
    """Write or delete the small copy and record its size in ``sidecar``."""
    path = small_path(directory, key)
    if copy is None:
        sidecar.pop("small", None)
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(exist_ok=True)
    _replace(path, copy)
    sidecar["small"] = len(copy)


def _replace(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` by a 0600 temp file and a rename."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _load(
    directory: Path, serials: frozenset[str]
) -> dict[str, tuple[bytes, dict[str, Any], bytes | None]]:
    """Every valid still with its metadata and small copy, by key; deletes the rest."""
    if not directory.is_dir():
        return {}
    entries: dict[str, tuple[bytes, dict[str, Any], bytes | None]] = {}
    small_dir = directory / SMALL_FOLDER
    for folder in (directory, small_dir):
        if folder.is_dir():
            for stray in folder.glob(".*.tmp"):
                stray.unlink(missing_ok=True)
    keys = {
        path.stem
        for path in directory.iterdir()
        if path.is_file() and path.suffix in (".jpg", ".json")
    }
    for key in keys:
        jpg, sidecar = directory / f"{key}.jpg", directory / f"{key}.json"
        entry = _read(jpg, sidecar) if key.split(".", 1)[0] in serials else None
        if entry is None:
            jpg.unlink(missing_ok=True)
            sidecar.unlink(missing_ok=True)
            continue
        image, meta = entry
        entries[key] = (image, meta, _load_small(directory, key, image, meta))
    if small_dir.is_dir():
        for copy in small_dir.iterdir():
            if copy.stem not in entries or entries[copy.stem][2] is None:
                copy.unlink(missing_ok=True)
    return entries


def _load_small(directory: Path, key: str, image: bytes, meta: dict[str, Any]) -> bytes | None:
    """A loaded still's small copy: read when its size matches the sidecar, else
    rendered and written again. A failed rewrite still returns the rendered copy."""
    path = small_path(directory, key)
    with contextlib.suppress(OSError):
        stored = path.read_bytes()
        if meta.get("small") == len(stored):
            return stored
    copy = render_small(image)
    with contextlib.suppress(OSError):
        sidecar = {**meta}
        _write_small(directory, key, copy, sidecar)
        _replace(directory / f"{key}.json", json_bytes(sidecar))
    return copy


def _read(jpg: Path, sidecar: Path) -> tuple[bytes, dict[str, Any]] | None:
    try:
        meta = json.loads(sidecar.read_bytes())
        image = jpg.read_bytes()
    except OSError, ValueError:
        return None
    if (
        not isinstance(meta, dict)
        or meta.get("version") != CACHE_VERSION
        or meta.get("size") != len(image)
    ):
        return None
    return image, meta
