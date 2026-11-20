"""The Capture live image at full resolution option.

End to end on the library's loopback ``FakeStation``: a press goes through Home
Assistant's ``button.press`` service to the snapshot manager and
``Station.async_camera_image``. With the option on the library holds the stream until
the picture size settles (``full_resolution``), and the capture's own cap grows by the
library's longest hold for it; with it off the capture takes the first keyframe within
the plain live cap.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlparse

import eufy_home_security.station
import pytest
from conftest import (
    SYNTHETIC,
    configure_options,
    entity_id_for,
    set_up_warm,
    wait_until,
)
from eufy_home_security import EufySecurity, ImageSource, Station
from eufy_home_security.station import FULL_RESOLUTION_TIMEOUT
from eufy_home_security.testing import FakeStation
from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN
from homeassistant.components.button import SERVICE_PRESS
from homeassistant.components.camera import DOMAIN as CAMERA_DOMAIN
from homeassistant.components.camera import async_get_stream_source
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.typing import ClientSessionGenerator

from custom_components.eufy_home_security import snapshots
from custom_components.eufy_home_security.const import (
    CAMERA_KEY,
    CAPTURE_LIVE_IMAGE_KEY,
    CONF_FULL_RESOLUTION_CAPTURE,
    LIVE_SNAPSHOT_TIMEOUT_SECONDS,
    OPTIONS_SECTION_CAMERA_IMAGES,
    OPTIONS_STEP_INIT,
)

# A woken camera's resolution climb, as a T8170 streams it.
_CLIMB: Final = [(1280, 720), (1920, 1080), (2880, 1616)]
# The integration's own logger: size assertions read only its records.
_LOGGER: Final = "custom_components.eufy_home_security"
_TS_PACKET: Final = 188
_TS_SYNC: Final = 0x47


@pytest.fixture(autouse=True)
def short_size_settle(monkeypatch: pytest.MonkeyPatch) -> None:
    """A full-resolution live image waits this long for the picture size to hold."""
    monkeypatch.setattr(eufy_home_security.station, "SETTLE_STATION", 0.1)


def _record_live_calls(monkeypatch: pytest.MonkeyPatch, delay: float = 0.0) -> list[dict[str, Any]]:
    """Every live ``Station.async_camera_image`` call's keywords, from now on.

    The library runs after ``delay`` seconds, which count against the capture's cap.
    """
    calls: list[dict[str, Any]] = []
    original = Station.async_camera_image

    async def recording(self: Station, device_sn: str, source: Any, **kw: Any) -> Any:
        if source is ImageSource.LIVE:
            calls.append(kw)
        await asyncio.sleep(delay)
        return await original(self, device_sn, source, **kw)

    monkeypatch.setattr(Station, "async_camera_image", recording)
    return calls


async def _press(hass: HomeAssistant) -> None:
    button = entity_id_for(hass, BUTTON_DOMAIN, SYNTHETIC.camera_sn, CAPTURE_LIVE_IMAGE_KEY)
    await hass.services.async_call(
        BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: button}, blocking=True
    )


async def _capture(
    hass: HomeAssistant, entry: MockConfigEntry, calls: list[dict[str, Any]]
) -> None:
    """Press Capture live image and wait until its library call ran and the job ended."""
    manager = entry.runtime_data.snapshots
    before = len(calls)
    await _press(hass)
    await wait_until(lambda: len(calls) > before)
    await wait_until(lambda: not manager.busy, timeout=20)


def _fetched_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name.startswith(_LOGGER) and r.getMessage().startswith("Live capture of ")
    ]


async def _unload(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    ("options", "full"),
    [
        ({}, False),
        ({CONF_FULL_RESOLUTION_CAPTURE: False}, False),
        ({CONF_FULL_RESOLUTION_CAPTURE: True}, True),
    ],
    ids=["never_saved", "off", "on"],
)
async def test_a_capture_asks_for_full_resolution_only_with_the_option_on(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    caplog: pytest.LogCaptureFixture,
    options: dict[str, Any],
    full: bool,
) -> None:
    """Off (and never saved) takes the first keyframe; on waits out the climb and shows
    the largest size, and the log names the picture size."""
    caplog.set_level(logging.DEBUG, logger=_LOGGER)
    entry = await set_up_warm(hass, seed_warm_cache, options=options)
    manager = entry.runtime_data.snapshots
    calls = _record_live_calls(monkeypatch)
    fake_station.live_keyframe_sizes = list(_CLIMB)

    await _capture(hass, entry, calls)

    assert manager.source_for(SYNTHETIC.camera_sn) == "live"
    assert [call.get("full_resolution", False) for call in calls] == [full]
    fetched = [line for line in _fetched_lines(caplog) if "decode started" in line]
    assert len(fetched) == 1, fetched
    assert ("fetched 2880x1616" in fetched[0]) is full, fetched
    await _unload(hass, entry)


@pytest.mark.parametrize(
    ("full", "delay", "lands"),
    [
        (False, 0.6, False),
        (True, 0.6, True),
        (True, 2.2, False),
    ],
    ids=["off_past_the_live_cap", "on_within_the_longer_cap", "on_past_the_longer_cap"],
)
async def test_only_a_full_resolution_capture_gets_the_librarys_hold_on_top_of_the_live_cap(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    caplog: pytest.LogCaptureFixture,
    full: bool,
    delay: float,
    lands: bool,
) -> None:
    """A library call slower than the live cap fails off and lands on; past the live cap
    plus the library's hold it fails on too. A failure keeps no image."""
    caplog.set_level(logging.DEBUG, logger=_LOGGER)
    monkeypatch.setattr(snapshots, "LIVE_SNAPSHOT_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr(snapshots, "FULL_RESOLUTION_TIMEOUT", 1.5)
    entry = await set_up_warm(hass, seed_warm_cache, options={CONF_FULL_RESOLUTION_CAPTURE: full})
    manager = entry.runtime_data.snapshots
    calls = _record_live_calls(monkeypatch, delay=delay)

    await _capture(hass, entry, calls)

    assert (manager.source_for(SYNTHETIC.camera_sn) == "live") is lands
    failed = [line for line in _fetched_lines(caplog) if "failed (TimeoutError)" in line]
    assert bool(failed) is not lands, _fetched_lines(caplog)
    await _unload(hass, entry)


def test_the_capture_cap_adds_the_librarys_full_resolution_hold_only_when_asked() -> None:
    """At the real constants: the plain live cap, plus the library's longest hold."""
    assert snapshots.capture_timeout(False) == LIVE_SNAPSHOT_TIMEOUT_SECONDS == 30
    assert snapshots.capture_timeout(True) == LIVE_SNAPSHOT_TIMEOUT_SECONDS + (
        FULL_RESOLUTION_TIMEOUT
    )


def test_the_option_text_names_the_librarys_longest_hold() -> None:
    """The form and the usage guide say how much longer a capture may keep the camera
    awake: the library's bound, in whole seconds, in strings.json, en.json and usage.md."""
    component = Path(snapshots.__file__).parent
    bound = f"at most {FULL_RESOLUTION_TIMEOUT:g} s more"
    for path in (component / "strings.json", component / "translations" / "en.json"):
        step = json.loads(path.read_text())["options"]["step"][OPTIONS_STEP_INIT]
        texts = step["sections"][OPTIONS_SECTION_CAMERA_IMAGES]
        assert texts["data"][CONF_FULL_RESOLUTION_CAPTURE]
        description = texts["data_description"][CONF_FULL_RESOLUTION_CAPTURE]
        assert bound in description, path.name
    usage = (component.parents[1] / "docs" / "usage.md").read_text()
    row = next(
        line for line in usage.splitlines() if "Capture live image at full resolution" in line
    )
    assert bound in row


async def _stream_path(hass: HomeAssistant) -> str:
    """The camera's stream URL without its base; the test client adds its own."""
    url = await async_get_stream_source(
        hass, entity_id_for(hass, CAMERA_DOMAIN, SYNTHETIC.camera_sn, CAMERA_KEY)
    )
    assert url is not None
    parts = urlparse(url)
    return f"{parts.path}?{parts.query}"


async def _read_ts(response: Any, n: int = _TS_PACKET) -> bytes:
    async with asyncio.timeout(10):
        data: bytes = await response.content.readexactly(n)
    return data


async def test_a_full_resolution_capture_beside_a_live_view_leaves_the_view_streaming(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_client_no_auth: ClientSessionGenerator,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """On a HomeBase the capture opens its own stream on an extra session beside the
    view, holds it to the full size, and the view streams on with its slot kept."""
    caplog.set_level(logging.DEBUG, logger=_LOGGER)
    entry = await set_up_warm(hass, seed_warm_cache, options={CONF_FULL_RESOLUTION_CAPTURE: True})
    station: Station = entry.runtime_data.coordinators[SYNTHETIC.station_sn].station
    calls = _record_live_calls(monkeypatch)
    fake_station.live_keyframe_sizes = list(_CLIMB)
    client = await hass_client_no_auth()
    response = await client.get(await _stream_path(hass))
    assert (await _read_ts(response))[0] == _TS_SYNC
    assert station.media_slot_camera == SYNTHETIC.camera_sn

    await _capture(hass, entry, calls)

    assert entry.runtime_data.snapshots.source_for(SYNTHETIC.camera_sn) == "live"
    assert [call.get("full_resolution") for call in calls] == [True]
    assert station.stats().extra_live_sessions == 1
    assert station.media_slot_camera == SYNTHETIC.camera_sn
    assert (await _read_ts(response, _TS_PACKET * 4))[0] == _TS_SYNC
    assert any("fetched 2880x1616" in line for line in _fetched_lines(caplog))
    assert not any("aborting" in r.getMessage() for r in caplog.records)
    response.close()
    await _unload(hass, entry)


async def test_the_option_set_through_the_options_flow_applies_after_its_reload(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Saved as a bool; on after the reload asks for full resolution, off again does not."""
    entry = await set_up_warm(hass, seed_warm_cache)
    calls = _record_live_calls(monkeypatch)

    for value in (True, False):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await configure_options(
            hass, result["flow_id"], {CONF_FULL_RESOLUTION_CAPTURE: value}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        assert entry.options[CONF_FULL_RESOLUTION_CAPTURE] is value

        await _capture(hass, entry, calls)
        assert calls[-1].get("full_resolution", False) is value

    assert len(calls) == 2
    await _unload(hass, entry)
