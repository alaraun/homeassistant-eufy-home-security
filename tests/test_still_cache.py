"""Stills kept on disk (``still_cache.py``): what a camera showed comes back after a restart.

End to end on the library's loopback ``FakeStation`` as ``tests/test_camera.py``; a
reload stands in for a Home Assistant restart, since memory is dropped and the entry id
kept. Each test has its own config directory (conftest's ``hass_config_dir``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import stat
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Final, cast

import pytest
from conftest import (
    SYNTHETIC,
    detection_event,
    entity_id_for,
    now_ms,
    set_up_warm,
    wait_until,
)
from eufy_home_security import DetectionType, EufySecurity, Station
from eufy_home_security.testing import FakeStation
from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN
from homeassistant.components.button import SERVICE_PRESS
from homeassistant.components.camera import DOMAIN as CAMERA_DOMAIN
from homeassistant.components.camera import async_get_image
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.eufy_home_security import still_cache
from custom_components.eufy_home_security.const import (
    ATTR_IMAGE_SOURCE,
    ATTR_IMAGE_UPDATED,
    ATTR_TRIGGERED_AT,
    CAMERA_KEY,
    CAPTURE_LIVE_IMAGE_KEY,
    DOMAIN,
    CameraImageMode,
)
from custom_components.eufy_home_security.snapshots import SnapshotManager

NEW_THUMBNAIL: Final = b"\xff\xd8NEW-THUMBNAIL\xff\xd9"
OLD_THUMBNAIL: Final = b"\xff\xd8OLD-THUMBNAIL\xff\xd9"
UNPAIRED_SN: Final = "T8160P2000099999"


def _manager(entry: MockConfigEntry) -> SnapshotManager:
    manager: SnapshotManager = entry.runtime_data.snapshots
    return manager


def _camera_id(hass: HomeAssistant) -> str:
    return entity_id_for(hass, CAMERA_DOMAIN, SYNTHETIC.camera_sn, CAMERA_KEY)


def _commands(station: FakeStation, cmd: int) -> int:
    return sum(1 for obj in station.received if obj.get("cmd") == cmd)


def _dir(hass: HomeAssistant, entry: MockConfigEntry) -> Path:
    return still_cache.cache_dir(hass, entry.entry_id)


def _camera_files(hass: HomeAssistant, entry: MockConfigEntry) -> tuple[Path, Path]:
    directory = _dir(hass, entry)
    key = f"{SYNTHETIC.camera_sn}.camera"
    return directory / f"{key}.jpg", directory / f"{key}.json"


async def _press_capture(hass: HomeAssistant) -> None:
    button = entity_id_for(hass, BUTTON_DOMAIN, SYNTHETIC.camera_sn, CAPTURE_LIVE_IMAGE_KEY)
    await hass.services.async_call(
        BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: button}, blocking=True
    )


async def _reload(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()


async def _unload(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def _show_thumbnail(
    hass: HomeAssistant, entry: MockConfigEntry, fake_station: FakeStation, t_ms: int
) -> None:
    """A detection at ``t_ms`` whose thumbnail the camera shows (no trigger frame)."""
    fake_station.images["/zx/new.jpg"] = NEW_THUMBNAIL
    entry.runtime_data.router.handle(
        detection_event(DetectionType.PERSON, t_ms=t_ms, thumb_path="/zx/new.jpg")
    )
    await wait_until(lambda: _manager(entry).image_for(SYNTHETIC.camera_sn) == NEW_THUMBNAIL)
    await wait_until(lambda: not _manager(entry).busy)
    await hass.async_block_till_done()


async def test_a_camera_still_and_its_attributes_come_back_after_a_restart(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The shown thumbnail, its tier, its detection time and image_updated, with no fetch."""
    entry = await set_up_warm(hass, seed_warm_cache)
    await _show_thumbnail(hass, entry, fake_station, now_ms())
    before = hass.states.get(_camera_id(hass))
    assert before is not None
    jpg, sidecar = _camera_files(hass, entry)
    await wait_until(jpg.exists)
    fetches = _commands(fake_station, 1308)

    await _reload(hass, entry)

    after = hass.states.get(_camera_id(hass))
    assert after is not None
    for name in (ATTR_IMAGE_SOURCE, ATTR_TRIGGERED_AT, ATTR_IMAGE_UPDATED):
        assert after.attributes[name] == before.attributes[name]
    assert (await async_get_image(hass, _camera_id(hass))).content == NEW_THUMBNAIL
    assert _commands(fake_station, 1308) == fetches
    # Private files; never a path or a record id in the metadata.
    for path in (jpg, sidecar):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "/zx/" not in sidecar.read_text()
    await _unload(hass, entry)


async def test_a_restored_still_keeps_its_rank_against_an_older_detection(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """After a restart a late, older detection is still not fetched."""
    entry = await set_up_warm(hass, seed_warm_cache)
    now = now_ms()
    await _show_thumbnail(hass, entry, fake_station, now)
    await wait_until(_camera_files(hass, entry)[0].exists)
    await _reload(hass, entry)
    fetches = _commands(fake_station, 1308)

    fake_station.images["/zx/old.jpg"] = OLD_THUMBNAIL
    entry.runtime_data.router.handle(
        detection_event(DetectionType.MOTION, t_ms=now - 60_000, thumb_path="/zx/old.jpg")
    )
    await asyncio.sleep(0.3)

    assert _commands(fake_station, 1308) == fetches
    assert _manager(entry).image_for(SYNTHETIC.camera_sn) == NEW_THUMBNAIL
    await _unload(hass, entry)


async def test_after_a_restart_and_a_capture_an_older_detection_is_still_ignored(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The restored detection keeps ranking after a pressed live image replaced it."""
    entry = await set_up_warm(hass, seed_warm_cache)
    now = now_ms()
    await _show_thumbnail(hass, entry, fake_station, now)
    await wait_until(_camera_files(hass, entry)[0].exists)
    await _reload(hass, entry)
    manager = _manager(entry)
    await _press_capture(hass)
    await wait_until(lambda: manager.source_for(SYNTHETIC.camera_sn) == "live")
    await wait_until(lambda: not manager.busy)
    fetches = _commands(fake_station, 1308)

    fake_station.images["/zx/old.jpg"] = OLD_THUMBNAIL
    entry.runtime_data.router.handle(
        detection_event(DetectionType.MOTION, t_ms=now - 60_000, thumb_path="/zx/old.jpg")
    )
    await asyncio.sleep(0.3)

    assert _commands(fake_station, 1308) == fetches
    assert manager.source_for(SYNTHETIC.camera_sn) == "live"
    await _unload(hass, entry)


async def test_a_pressed_live_image_comes_back_and_a_newer_detection_replaces_it(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A restored live capture shows as live; the next detection still replaces it."""
    entry = await set_up_warm(hass, seed_warm_cache)
    manager = _manager(entry)
    await _press_capture(hass)
    await wait_until(lambda: manager.source_for(SYNTHETIC.camera_sn) == "live")
    await wait_until(lambda: not manager.busy)
    live = manager.image_for(SYNTHETIC.camera_sn)
    await wait_until(_camera_files(hass, entry)[0].exists)

    await _reload(hass, entry)
    state = hass.states.get(_camera_id(hass))
    assert state is not None
    assert state.attributes[ATTR_IMAGE_SOURCE] == "live"
    assert ATTR_TRIGGERED_AT not in state.attributes
    assert _manager(entry).image_for(SYNTHETIC.camera_sn) == live

    await _show_thumbnail(hass, entry, fake_station, now_ms())
    await _unload(hass, entry)


async def test_every_new_still_overwrites_the_cached_one(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """One file pair per camera: the disk holds the still shown last."""
    entry = await set_up_warm(hass, seed_warm_cache)
    await _show_thumbnail(hass, entry, fake_station, now_ms())
    jpg, sidecar = _camera_files(hass, entry)
    await wait_until(lambda: jpg.exists() and jpg.read_bytes() == NEW_THUMBNAIL)

    fake_station.images["/zx/next.jpg"] = OLD_THUMBNAIL
    entry.runtime_data.router.handle(
        detection_event(DetectionType.MOTION, t_ms=now_ms() + 1_000, thumb_path="/zx/next.jpg")
    )
    await wait_until(lambda: jpg.read_bytes() == OLD_THUMBNAIL)
    await hass.async_block_till_done()
    assert json.loads(sidecar.read_text())["size"] == len(OLD_THUMBNAIL)
    assert sorted(p.name for p in _dir(hass, entry).iterdir()) == sorted([jpg.name, sidecar.name])
    await _unload(hass, entry)


@pytest.mark.parametrize(
    "damage",
    ["truncated_jpeg", "bad_json", "other_version", "no_sidecar"],
)
async def test_a_damaged_cache_entry_is_dropped_and_the_camera_starts_empty(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    damage: str,
) -> None:
    """A torn or foreign entry never shows: it is deleted and the camera has no still."""
    entry = await set_up_warm(hass, seed_warm_cache)
    await _show_thumbnail(hass, entry, fake_station, now_ms())
    jpg, sidecar = _camera_files(hass, entry)
    await wait_until(sidecar.exists)
    await _unload(hass, entry)
    if damage == "truncated_jpeg":
        jpg.write_bytes(NEW_THUMBNAIL[:-3])
    elif damage == "bad_json":
        sidecar.write_text("{")
    elif damage == "other_version":
        meta = json.loads(sidecar.read_text())
        sidecar.write_text(json.dumps({**meta, "version": 99}))
    else:
        sidecar.unlink()

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get(_camera_id(hass))
    assert state is not None
    assert ATTR_IMAGE_SOURCE not in state.attributes
    assert not jpg.exists()
    assert not sidecar.exists()
    await _unload(hass, entry)


async def test_stills_of_unpaired_devices_and_stray_temp_files_are_deleted_at_setup(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Only paired devices keep their stills; an interrupted write's temp file goes too."""
    entry = await set_up_warm(hass, seed_warm_cache)
    await _show_thumbnail(hass, entry, fake_station, now_ms())
    jpg, sidecar = _camera_files(hass, entry)
    await wait_until(sidecar.exists)
    await _unload(hass, entry)
    directory = _dir(hass, entry)
    (directory / f"{UNPAIRED_SN}.camera.jpg").write_bytes(OLD_THUMBNAIL)
    (directory / f"{UNPAIRED_SN}.camera.json").write_text(
        sidecar.read_text().replace(str(len(NEW_THUMBNAIL)), str(len(OLD_THUMBNAIL)))
    )
    (directory / ".tmp-left.tmp").write_bytes(b"x")

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert sorted(p.name for p in directory.iterdir()) == sorted([jpg.name, sidecar.name])
    await _unload(hass, entry)


async def test_removing_the_entry_deletes_its_stills(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The cache directory of a removed account is gone."""
    entry = await set_up_warm(hass, seed_warm_cache)
    await _show_thumbnail(hass, entry, fake_station, now_ms())
    await wait_until(_camera_files(hass, entry)[0].exists)

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert not _dir(hass, entry).exists()


async def test_a_failed_write_warns_once_and_the_still_is_still_shown(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A full or read-only disk costs the restart copy, never the shown still."""
    calls: list[str] = []

    def failing(*args: Any) -> None:
        calls.append(args[1])
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(still_cache, "_write", failing)
    entry = await set_up_warm(hass, seed_warm_cache)
    await _show_thumbnail(hass, entry, fake_station, now_ms())
    entry.runtime_data.router.handle(
        detection_event(DetectionType.MOTION, t_ms=now_ms() + 1_000, thumb_path="/zx/new.jpg")
    )
    await wait_until(lambda: len(calls) == 2)
    await hass.async_block_till_done()

    assert (await async_get_image(hass, _camera_id(hass))).content == NEW_THUMBNAIL
    warnings = [
        r for r in caplog.records if r.levelno == logging.WARNING and "cache the still" in r.message
    ]
    assert len(warnings) == 1
    assert SYNTHETIC.camera_sn not in warnings[0].getMessage()
    await _unload(hass, entry)


def test_a_key_with_a_path_character_is_never_written() -> None:
    """A serial or name outside [A-Za-z0-9_-] has no key, so nothing lands outside the dir."""
    assert still_cache.still_key("T8160P2000000001", "camera") == "T8160P2000000001.camera"
    for serial, name in (("../x", "camera"), ("T816/0", "camera"), ("T8160", "a.b"), ("", "x")):
        assert still_cache.still_key(serial, name) is None


async def test_a_standalone_capture_runs_even_when_ending_the_live_view_raised(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raising media yield neither kills the station's worker nor strands its capture."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    yields = 0

    async def yield_media(_serial: str) -> None:
        nonlocal yields
        yields += 1
        if yields == 1:
            raise RuntimeError("yield failed")

    manager = SnapshotManager(
        hass, entry, live_snapshot=False, camera_image=CameraImageMode.HD, yield_media=yield_media
    )
    captures: list[str] = []

    async def capture(_station: Station, _camera: Any, device_sn: str) -> None:
        captures.append(device_sn)

    monkeypatch.setattr(manager, "_async_capture", capture)
    station = cast(Station, SimpleNamespace(serial=SYNTHETIC.station_sn, is_standalone=True))

    manager.async_request_capture(station, SYNTHETIC.station_sn)
    await wait_until(lambda: not manager.busy)
    manager.async_request_capture(station, SYNTHETIC.station_sn)
    await wait_until(lambda: not manager.busy)

    assert yields == 2
    assert captures == [SYNTHETIC.station_sn, SYNTHETIC.station_sn]
    await manager.async_stop()
