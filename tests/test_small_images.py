"""Small copies of the camera and preset images (``small_images.py``, ``still_cache.py``).

End to end on the library's loopback ``FakeStation``. Stills are real JPEGs made here,
960x540 so a thumbnail fits one fake-station reply, and the small copy is really
decoded and scaled.
"""

from __future__ import annotations

from collections.abc import Callable
from io import BytesIO
from pathlib import Path
from typing import Final

import pytest
from conftest import (
    SYNTHETIC,
    detection_event,
    entity_id_for,
    now_ms,
    set_up_warm,
    wait_until,
)
from eufy_home_security import DetectionType, EufySecurity
from eufy_home_security.testing import FakeStation
from homeassistant.components.camera import DOMAIN as CAMERA_DOMAIN
from homeassistant.components.camera import async_get_image
from homeassistant.core import HomeAssistant
from PIL import Image
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.eufy_home_security import small_images, still_cache
from custom_components.eufy_home_security.const import CAMERA_KEY
from custom_components.eufy_home_security.snapshots import SnapshotManager

FULL_WIDTH: Final = 960
FULL_HEIGHT: Final = 540


def jpeg(width: int, height: int, colour: tuple[int, int, int] = (40, 90, 160)) -> bytes:
    """A real JPEG of ``width`` x ``height`` in one colour."""
    out = BytesIO()
    Image.new("RGB", (width, height), colour).save(out, "JPEG", quality=90)
    return out.getvalue()


def size_of(data: bytes) -> tuple[int, int]:
    with Image.open(BytesIO(data)) as picture:
        return picture.size


def _manager(entry: MockConfigEntry) -> SnapshotManager:
    manager: SnapshotManager = entry.runtime_data.snapshots
    return manager


def _camera_id(hass: HomeAssistant) -> str:
    return entity_id_for(hass, CAMERA_DOMAIN, SYNTHETIC.camera_sn, CAMERA_KEY)


def _small_file(hass: HomeAssistant, entry: MockConfigEntry) -> Path:
    key = f"{SYNTHETIC.camera_sn}.camera"
    return still_cache.small_path(still_cache.cache_dir(hass, entry.entry_id), key)


async def _show(
    hass: HomeAssistant, entry: MockConfigEntry, fake_station: FakeStation, image: bytes
) -> None:
    """A detection whose thumbnail ``image`` the camera then shows."""
    fake_station.images["/zx/new.jpg"] = image
    entry.runtime_data.router.handle(
        detection_event(DetectionType.PERSON, t_ms=now_ms(), thumb_path="/zx/new.jpg")
    )
    await wait_until(lambda: _manager(entry).image_for(SYNTHETIC.camera_sn) == image)
    await wait_until(lambda: not _manager(entry).busy)
    await hass.async_block_till_done()


def test_a_still_is_scaled_to_the_small_width_keeping_its_aspect() -> None:
    """A 16:9 frame becomes 480x270; a smaller one keeps its size; garbage has none."""
    assert size_of(still_cache.render_small(jpeg(3840, 2160)) or b"") == (480, 270)
    assert size_of(still_cache.render_small(jpeg(1920, 1080)) or b"") == (480, 270)
    assert size_of(still_cache.render_small(jpeg(320, 240)) or b"") == (320, 240)
    assert still_cache.render_small(b"\xff\xd8not a jpeg\xff\xd9") is None
    png = BytesIO()
    Image.new("RGB", (64, 64)).save(png, "PNG")
    assert still_cache.render_small(png.getvalue()) is None


async def test_a_new_still_gets_a_small_copy_on_disk_and_the_picture_points_at_it(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The small file follows each still; entity_picture carries its version and token."""
    entry = await set_up_warm(hass, seed_warm_cache)
    await _show(hass, entry, fake_station, jpeg(FULL_WIDTH, FULL_HEIGHT))
    small = _small_file(hass, entry)
    await wait_until(small.exists)
    first = small.read_bytes()
    assert size_of(first) == (480, 270)

    state = hass.states.get(_camera_id(hass))
    assert state is not None
    picture = state.attributes["entity_picture"]
    assert picture.startswith(f"/api/eufy_home_security/image/{_camera_id(hass)}/small?v=")
    assert f"token={state.attributes['access_token']}" in picture

    await _show(hass, entry, fake_station, jpeg(FULL_WIDTH, FULL_HEIGHT, (200, 30, 30)))
    await wait_until(lambda: small.read_bytes() != first)
    after = hass.states.get(_camera_id(hass))
    assert after is not None
    assert after.attributes["entity_picture"].split("&")[0] != picture.split("&")[0]
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_missing_small_copy_is_made_again_at_load(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A reload finds the small file gone, renders it again and writes it, with no fetch."""
    entry = await set_up_warm(hass, seed_warm_cache)
    await _show(hass, entry, fake_station, jpeg(FULL_WIDTH, FULL_HEIGHT))
    small = _small_file(hass, entry)
    await wait_until(small.exists)
    small.unlink()

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert size_of(small.read_bytes()) == (480, 270)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_the_small_image_view_serves_by_token_and_caches_the_current_version(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_client_no_auth: ClientSessionGenerator,
    hass_client: ClientSessionGenerator,
) -> None:
    """Token or login; a year's cache for the current version, none for another."""
    entry = await set_up_warm(hass, seed_warm_cache)
    await _show(hass, entry, fake_station, jpeg(FULL_WIDTH, FULL_HEIGHT))
    state = hass.states.get(_camera_id(hass))
    assert state is not None
    picture = state.attributes["entity_picture"]
    anonymous = await hass_client_no_auth()

    response = await anonymous.get(picture)
    assert response.status == 200
    assert response.headers["Content-Type"] == "image/jpeg"
    assert "max-age=31536000" in response.headers["Cache-Control"]
    assert size_of(await response.read()) == (480, 270)

    stale = picture.replace("?v=", "?v=1")
    response = await anonymous.get(stale)
    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-cache"

    path = picture.split("?")[0]
    assert (await anonymous.get(path)).status == 403
    assert (await anonymous.get(f"{path}?token=wrong")).status == 403
    logged_in = await hass_client()
    assert (await logged_in.get(path)).status == 200
    assert (await logged_in.get("/api/eufy_home_security/image/camera.nope/small")).status == 404
    assert (await anonymous.get("/api/eufy_home_security/image/camera.nope/small")).status == 401
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_sized_camera_proxy_request_is_answered_with_the_small_copy(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A tile-sized width/height gets the small copy; a larger one the full still."""
    entry = await set_up_warm(hass, seed_warm_cache)
    full = jpeg(FULL_WIDTH, FULL_HEIGHT)
    await _show(hass, entry, fake_station, full)

    small = await async_get_image(hass, _camera_id(hass), width=480, height=270)
    assert size_of(small.content) == (480, 270)
    large = await async_get_image(hass, _camera_id(hass), width=960, height=540)
    assert large.content == full
    assert small_images.fits_small(480, 270)
    assert not small_images.fits_small(481, 270)
    assert not small_images.fits_small(None, 270)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.parametrize("image", [b"\xff\xd8not a jpeg\xff\xd9"])
async def test_a_still_that_does_not_decode_serves_the_full_image(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_client: ClientSessionGenerator,
    image: bytes,
) -> None:
    """No small file is written, and the view answers with the still itself."""
    entry = await set_up_warm(hass, seed_warm_cache)
    await _show(hass, entry, fake_station, image)
    sidecar = still_cache.cache_dir(hass, entry.entry_id) / f"{SYNTHETIC.camera_sn}.camera.json"
    await wait_until(sidecar.exists)
    assert not _small_file(hass, entry).exists()

    client = await hass_client()
    response = await client.get(f"/api/eufy_home_security/image/{_camera_id(hass)}/small")
    assert response.status == 200
    assert await response.read() == image
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
