"""Live video streaming: the camera's MPEG-TS over HTTP.

End to end on the library's loopback ``FakeStation``, which serves a live stream from
its own media path, through ``streaming.EufyStreamView``. Every test here proves the
camera is opened by a real HTTP GET and by nothing else: setup opens nothing,
``stream_source()`` opens nothing however often it is called, two viewers of one camera
cost one camera open, and the last viewer leaving releases it.

The library is never mocked. Streams use the library's own defaults, so a stream starts
at the first keyframe and a test pays no settle window.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlparse

import av
import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request, unused_port
from conftest import (
    PUSHED_THUMB_PATH,
    PUSHED_THUMBNAIL,
    SENSOR_SN,
    SYNTHETIC,
    add_entry,
    detection_event,
    entity_id_for,
    now_ms,
    set_up_warm,
    setup_entry,
    state_of,
    wait_until,
)
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from eufy_home_security import (
    MIN_STATION_SESSIONS,
    DetectionType,
    EufySecurity,
    Station,
    redact_serial,
)
from eufy_home_security.devices.model_settings import settings_of
from eufy_home_security.p2p import session as session_module
from eufy_home_security.testing import FakeCloud, FakeStation, camera_device
from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN
from homeassistant.components.button import SERVICE_PRESS
from homeassistant.components.camera import DOMAIN as CAMERA_DOMAIN
from homeassistant.components.camera import CameraEntityFeature, async_get_stream_source
from homeassistant.components.camera.helper import get_camera_from_entity_id
from homeassistant.components.number import ATTR_VALUE, SERVICE_SET_VALUE
from homeassistant.components.number import DOMAIN as NUMBER_DOMAIN
from homeassistant.components.select import ATTR_OPTION, SERVICE_SELECT_OPTION
from homeassistant.components.select import DOMAIN as SELECT_DOMAIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.eufy_home_security import detections, streaming
from custom_components.eufy_home_security.const import (
    CAMERA_KEY,
    CAPTURE_LIVE_IMAGE_KEY,
    CONF_LIVE_SNAPSHOT,
    CONF_STATION_SESSIONS,
)

# A second camera on the fake station, on channel 1: synthetic, like SENSOR_SN.
OTHER_CAMERA_SN: Final = "T8160P2000000002"
# A recording the fake station plays for any path: the detection's trigger frame.
CLIP_PATH: Final = "/zx/clip.zxvideo"
# A pan/tilt camera paired to the fake station on channel 1; synthetic. No HomeBase
# camera with presets exists to model it on, so it borrows the T8170's profile.
PTZ_CAMERA_SN: Final = "T8170P2000000003"
# The live-open command, and its stop.
LIVE_OPEN: Final = 1003
LIVE_STOP: Final = 1004
# One MPEG-TS packet, whose first byte is the sync byte.
TS_PACKET: Final = 188
TS_SYNC: Final = 0x47
# The integration's own logger: every assertion about records reads only this one.
LOGGER: Final = "custom_components.eufy_home_security"
# The debug line of a live view yielding the media slot, after the camera.
YIELD_REASON: Final = "aborting, a media operation needs the station's one media slot"
# The host a test TLS certificate names: Home Assistant's domain, never 127.0.0.1.
TLS_HOST: Final = "ha.example.test"


def _camera_id(hass: HomeAssistant, serial: str = SYNTHETIC.camera_sn) -> str:
    return entity_id_for(hass, CAMERA_DOMAIN, serial, CAMERA_KEY)


def _commands(station: FakeStation, cmd: int) -> int:
    return sum(1 for obj in station.received if obj.get("cmd") == cmd)


async def _stream_path(hass: HomeAssistant, entity_id: str) -> str:
    """The camera's stream URL without its base (path and secret query).

    The test client prepends its own base and connects from 127.0.0.1.
    """
    url = await async_get_stream_source(hass, entity_id)
    assert url is not None
    parts = urlparse(url)
    return f"{parts.path}?{parts.query}"


class _Peer:
    """A request transport whose socket peer is ``address``."""

    def __init__(self, address: str) -> None:
        self._address = address

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        return (self._address, 40000) if name == "peername" else default

    def is_closing(self) -> bool:
        return False


async def _read_ts(response: Any, n: int = TS_PACKET) -> bytes:
    async with asyncio.timeout(10):
        data: bytes = await response.content.readexactly(n)
    return data


async def _unload(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def _image(hass: HomeAssistant, entity_id: str) -> bytes | None:
    """What the camera serves to a view, None when it has no image."""
    from homeassistant.components.camera import async_get_image

    try:
        return (await async_get_image(hass, entity_id)).content
    except HomeAssistantError:
        return None


async def _set_up_with_other_camera(
    hass: HomeAssistant, fake_station: FakeStation, fake_cloud: FakeCloud, seed: Callable[..., None]
) -> MockConfigEntry:
    """Pair a second camera before the warm cache copies the cloud's device list."""
    fake_cloud.devices.append(
        camera_device(OTHER_CAMERA_SN, station_sn=fake_station.serial, channel=1, name="Back")
    )
    fake_station.params[1] = {1101: "80", 1142: "-60"}
    return await set_up_warm(hass, seed)


def _stream_routes(hass: HomeAssistant) -> int:
    """How many routes of the running app serve the stream view's path."""
    return sum(
        1
        for resource in hass.http.app.router.resources()
        if str(resource.canonical).startswith("/api/eufy_home_security/stream/")
    )


def _errors(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name.startswith(LOGGER) and record.levelno >= logging.ERROR
    ]


# ── setup and stream_source() open nothing ────────────────────────────────────


async def test_setup_opens_no_live_stream(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Building a broadcast per camera costs no session, no wake and no task."""
    entry = await set_up_warm(hass, seed_warm_cache)
    await asyncio.sleep(0.3)
    await hass.async_block_till_done()

    assert fake_station.live_opens == []
    assert _commands(fake_station, LIVE_OPEN) == 0
    assert fake_station.streaming is False
    # The manifest's dependencies: ["http"] loaded the component before entry setup.
    assert hass.http is not None

    state = hass.states.get(_camera_id(hass))
    assert state is not None
    assert state.attributes["supported_features"] & CameraEntityFeature.STREAM

    await _unload(hass, entry)


async def test_stream_source_returns_a_stable_url_without_opening_anything(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Home Assistant asks on every offer and every recording, so it must open nothing."""
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = _camera_id(hass)

    urls = [await async_get_stream_source(hass, entity_id) for _ in range(5)]

    assert len(set(urls)) == 1
    url = urls[0]
    assert url is not None
    assert url.startswith("http://127.0.0.1:")
    assert str(hass.http.server_port) in url
    assert SYNTHETIC.camera_sn in url
    assert f"?auth={streaming.stream_registry(hass).secret}" in url

    await asyncio.sleep(0.3)
    await hass.async_block_till_done()
    assert _commands(fake_station, LIVE_OPEN) == 0
    assert fake_station.live_opens == []

    await _unload(hass, entry)


@pytest.mark.parametrize(("use_ssl", "scheme"), [(False, "http"), (True, "https")])
async def test_the_stream_url_speaks_tls_when_home_assistants_server_does(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    use_ssl: bool,
    scheme: str,
) -> None:
    """With ``ssl_certificate`` set, HA's one port speaks only TLS; the URL stays loopback."""
    entry = await set_up_warm(hass, seed_warm_cache)
    assert hass.config.api is not None
    monkeypatch.setattr(hass.config.api, "use_ssl", use_ssl)

    url = await async_get_stream_source(hass, _camera_id(hass))

    assert url is not None
    parts = urlparse(url)
    assert (parts.scheme, parts.hostname, parts.port) == (
        scheme,
        "127.0.0.1",
        hass.http.server_port,
    )
    await _unload(hass, entry)


@pytest.mark.parametrize(("use_ssl", "options"), [(False, {}), (True, {"tls_verify": "0"})])
async def test_the_stream_worker_does_not_verify_the_loopback_certificate(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    use_ssl: bool,
    options: dict[str, str],
) -> None:
    """HA's certificate names its domain, not 127.0.0.1; FFmpeg 9 verifies by default."""
    entry = await set_up_warm(hass, seed_warm_cache)
    assert hass.config.api is not None
    monkeypatch.setattr(hass.config.api, "use_ssl", use_ssl)
    camera = get_camera_from_entity_id(hass, _camera_id(hass))

    stream = await camera.async_create_stream()

    assert stream is not None
    assert stream.pyav_options == options
    await _unload(hass, entry)


def _server_tls(tmp_path: Path) -> ssl.SSLContext:
    """A TLS server context with a self-signed certificate for a host other than 127.0.0.1."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, TLS_HOST)])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(TLS_HOST)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_file, key_file = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(cert_file, key_file)
    return context


def _first_packet_size(url: str, options: dict[str, str]) -> int:
    """Open ``url`` with PyAV as HA's stream worker does; the first packet's size.

    The smallest probe, so the open returns at the first video packet instead of
    waiting for the silent audio track.
    """
    probe = {**options, "probesize": "32", "analyzeduration": "0"}
    with av.open(url, options=probe, timeout=10) as container:
        for packet in container.demux():
            if packet.size:
                return packet.size
    return 0


async def test_the_stream_worker_reads_the_view_over_tls_with_a_certificate_for_another_host(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """PyAV, with the options the stream worker gets, reads the view over TLS; the same
    open with verification on is refused, so the option is what makes it work."""
    entry = await set_up_warm(hass, seed_warm_cache)
    context = await hass.async_add_executor_job(_server_tls, tmp_path)
    app = web.Application()
    streaming.EufyStreamView(streaming.stream_registry(hass)).register(hass, app, app.router)
    runner = web.AppRunner(app)
    await runner.setup()
    port = unused_port()
    site = web.TCPSite(runner, "127.0.0.1", port, ssl_context=context)
    await site.start()
    assert hass.config.api is not None
    monkeypatch.setattr(hass.config.api, "use_ssl", True)
    monkeypatch.setattr(hass.http, "server_port", port)
    stream = await get_camera_from_entity_id(hass, _camera_id(hass)).async_create_stream()
    assert stream is not None

    try:
        with pytest.raises(av.error.FFmpegError, match="verification"):
            await hass.async_add_executor_job(
                _first_packet_size, stream.source, {**stream.pyav_options, "tls_verify": "1"}
            )
        assert fake_station.live_opens == []
        size = await hass.async_add_executor_job(
            _first_packet_size, stream.source, stream.pyav_options
        )
    finally:
        await runner.cleanup()

    assert size > 0
    assert len(fake_station.live_opens) == 1
    await _unload(hass, entry)


# ── one camera open per camera, however many viewers ──────────────────────────


async def test_two_viewers_of_one_camera_cost_one_camera_open(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """The headline: the library fans one camera stream to every viewer."""
    entry = await set_up_warm(hass, seed_warm_cache)
    client = await hass_client_no_auth()
    path = await _stream_path(hass, _camera_id(hass))

    r1 = await client.get(path)
    r2 = await client.get(path)

    assert r1.status == 200
    assert r2.status == 200
    assert r1.headers["Content-Type"] == "video/mp2t"
    assert r2.headers["Content-Type"] == "video/mp2t"

    await wait_until(lambda: fake_station.media_frames_sent > 0, timeout=20)
    assert (await _read_ts(r1))[0] == TS_SYNC
    assert (await _read_ts(r2))[0] == TS_SYNC

    assert len(fake_station.live_opens) == 1
    assert _commands(fake_station, LIVE_OPEN) == 1
    assert fake_station.streaming is True

    r1.close()
    r2.close()
    await _unload(hass, entry)


async def test_two_cameras_of_one_homebase_stream_at_once(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """Each camera has its own URL and channel, and the second streams beside the first.

    The library opens the second camera on an extra session within the default
    session budget, so the first view keeps running.
    """
    entry = await _set_up_with_other_camera(hass, fake_station, fake_cloud, seed_warm_cache)
    client = await hass_client_no_auth()

    first = await _stream_path(hass, _camera_id(hass))
    second = await _stream_path(hass, _camera_id(hass, OTHER_CAMERA_SN))
    assert first != second
    assert SYNTHETIC.camera_sn in first
    assert OTHER_CAMERA_SN in second

    r1 = await client.get(first)
    assert r1.headers["Content-Type"] == "video/mp2t"
    assert (await _read_ts(r1))[0] == TS_SYNC
    # The first camera is on channel 0, resolved from its serial inside the library.
    assert fake_station.live_opens == [0]

    r2 = await client.get(second)
    assert r2.headers["Content-Type"] == "video/mp2t"
    assert (await _read_ts(r2))[0] == TS_SYNC
    assert sorted(fake_station.live_cameras) == [0, 1]
    assert (await _read_ts(r1))[0] == TS_SYNC

    r1.close()
    r2.close()
    await _unload(hass, entry)


async def test_a_live_view_past_the_session_budget_answers_503_with_no_error(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_client_no_auth: ClientSessionGenerator,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A budget of 2 allows one live view: a second camera waits, then gets a 503.

    One INFO line names the limit; nothing is logged as an error, and the first view
    keeps streaming.
    """
    # The open's wait for a free session is the library's first-frame timeout.
    monkeypatch.setattr(session_module, "MEDIA_LIVE_FIRST_FRAME_TIMEOUT", 1.0)
    fake_cloud.devices.append(
        camera_device(OTHER_CAMERA_SN, station_sn=fake_station.serial, channel=1, name="Back")
    )
    fake_station.params[1] = {1101: "80", 1142: "-60"}
    entry = await set_up_warm(
        hass, seed_warm_cache, options={CONF_STATION_SESSIONS: MIN_STATION_SESSIONS}
    )
    client = await hass_client_no_auth()

    r1 = await client.get(await _stream_path(hass, _camera_id(hass)))
    assert (await _read_ts(r1))[0] == TS_SYNC

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        r2 = await client.get(await _stream_path(hass, _camera_id(hass, OTHER_CAMERA_SN)))

    assert r2.status == 503
    assert await r2.read() == b""
    assert fake_station.live_cameras == [0]
    assert (await _read_ts(r1))[0] == TS_SYNC
    assert _errors(caplog) == []
    limit_lines = [
        record
        for record in caplog.records
        if record.name.startswith(LOGGER) and "sessions per HomeBase" in record.getMessage()
    ]
    assert len(limit_lines) == 1
    assert limit_lines[0].levelno == logging.INFO
    assert limit_lines[0].exc_info is None
    assert "allows 1 live stream(s)" in limit_lines[0].getMessage()

    r1.close()
    r2.close()
    await _unload(hass, entry)


async def test_raising_the_session_budget_serves_a_waiting_live_view(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """The option reaches the running station: a view waiting for a session opens at once."""
    fake_cloud.devices.append(
        camera_device(OTHER_CAMERA_SN, station_sn=fake_station.serial, channel=1, name="Back")
    )
    fake_station.params[1] = {1101: "80", 1142: "-60"}
    entry = await set_up_warm(
        hass, seed_warm_cache, options={CONF_STATION_SESSIONS: MIN_STATION_SESSIONS}
    )
    clients_before = len(built_clients)
    client = await hass_client_no_auth()

    r1 = await client.get(await _stream_path(hass, _camera_id(hass)))
    assert (await _read_ts(r1))[0] == TS_SYNC
    opening = asyncio.create_task(
        client.get(await _stream_path(hass, _camera_id(hass, OTHER_CAMERA_SN)))
    )
    await asyncio.sleep(0.3)
    assert not opening.done()

    flow = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        flow["flow_id"], {CONF_STATION_SESSIONS: MIN_STATION_SESSIONS + 1}
    )
    async with asyncio.timeout(10):
        r2 = await opening
    assert r2.status == 200
    assert (await _read_ts(r2))[0] == TS_SYNC
    assert sorted(fake_station.live_cameras) == [0, 1]
    assert len(built_clients) == clients_before, "the budget change reloaded the entry"

    r1.close()
    r2.close()
    await _unload(hass, entry)


async def test_the_last_viewer_leaving_releases_the_camera_and_a_later_view_reopens_it(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """The camera is released by the last viewer, and the broadcast object is reused."""
    entry = await set_up_warm(hass, seed_warm_cache)
    client = await hass_client_no_auth()
    path = await _stream_path(hass, _camera_id(hass))

    r1 = await client.get(path)
    r2 = await client.get(path)
    await wait_until(lambda: fake_station.streaming, timeout=20)
    assert len(fake_station.live_opens) == 1

    r1.close()
    await asyncio.sleep(0.2)
    assert fake_station.streaming is True
    assert len(fake_station.live_opens) == 1

    r2.close()
    await wait_until(lambda: not fake_station.streaming, timeout=20)
    assert _commands(fake_station, LIVE_STOP) >= 1

    r3 = await client.get(path)
    await wait_until(lambda: fake_station.streaming, timeout=20)
    # The same broadcast object reopened: a second 1003, not a discarded object.
    assert len(fake_station.live_opens) == 2

    r3.close()
    await _unload(hass, entry)


# ── a still ends a live view only at the session budget ───────────────────────


async def _press_capture(hass: HomeAssistant, serial: str) -> None:
    button = entity_id_for(hass, BUTTON_DOMAIN, serial, CAPTURE_LIVE_IMAGE_KEY)
    await hass.services.async_call(
        BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: button}, blocking=True
    )


async def _streams_on(response: Any) -> None:
    """The view delivers more data: it was not ended."""
    assert (await _read_ts(response, TS_PACKET * 4))[0] == TS_SYNC


async def test_a_detection_still_leaves_the_live_view_running(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_client_no_auth: ClientSessionGenerator,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A HomeBase detection still ends no live view, even with the session budget used up.

    Its thumbnail and trigger frame are no live open: the trigger frame's session is
    held back from the live streams' budget.
    """
    fake_station.images[PUSHED_THUMB_PATH] = PUSHED_THUMBNAIL
    entry = await set_up_warm(
        hass, seed_warm_cache, options={CONF_STATION_SESSIONS: MIN_STATION_SESSIONS}
    )
    client = await hass_client_no_auth()
    entity_id = _camera_id(hass)
    response = await client.get(await _stream_path(hass, entity_id))
    assert (await _read_ts(response))[0] == TS_SYNC

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        entry.runtime_data.router.handle(
            detection_event(
                DetectionType.PERSON,
                t_ms=now_ms(),
                thumb_path=PUSHED_THUMB_PATH,
                video_path=CLIP_PATH,
            )
        )
        await wait_until(
            lambda: entry.runtime_data.snapshots.source_for(SYNTHETIC.camera_sn) == "trigger_frame",
            timeout=30,
        )

    assert await _image(hass, entity_id) is not None
    await _streams_on(response)
    assert fake_station.live_cameras == [0]
    assert not any("aborting" in r.getMessage() for r in caplog.records)
    assert _errors(caplog) == []

    response.close()
    await _unload(hass, entry)


async def test_a_live_capture_beside_a_live_view_takes_an_extra_session(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_client_no_auth: ClientSessionGenerator,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A live still of the camera being viewed runs beside the view, which streams on."""
    entry = await set_up_warm(hass, seed_warm_cache)
    station = entry.runtime_data.coordinators[SYNTHETIC.station_sn].station
    client = await hass_client_no_auth()
    response = await client.get(await _stream_path(hass, _camera_id(hass)))
    assert (await _read_ts(response))[0] == TS_SYNC
    assert station.media_slot_camera == SYNTHETIC.camera_sn

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        await _press_capture(hass, SYNTHETIC.camera_sn)
        await wait_until(
            lambda: entry.runtime_data.snapshots.source_for(SYNTHETIC.camera_sn) == "live",
            timeout=30,
        )

    assert station.stats().extra_live_sessions == 1
    assert station.media_slot_camera == SYNTHETIC.camera_sn
    await _streams_on(response)
    assert not any("aborting" in r.getMessage() for r in caplog.records)
    assert _errors(caplog) == []

    response.close()
    await _unload(hass, entry)


async def test_at_the_session_budget_a_live_keyframe_ends_the_view_on_the_slot_at_once(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_client_no_auth: ClientSessionGenerator,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """With one live stream allowed, the automatic live keyframe ends the view and lands.

    The first open is refused at once (``LiveStreamLimitError``), so the view ends
    without waiting out the library's first-frame timeout, and the keyframe takes the
    freed slot.
    """
    entry = await set_up_warm(
        hass,
        seed_warm_cache,
        options={CONF_STATION_SESSIONS: MIN_STATION_SESSIONS, CONF_LIVE_SNAPSHOT: True},
    )
    client = await hass_client_no_auth()
    entity_id = _camera_id(hass)
    response = await client.get(await _stream_path(hass, entity_id))
    assert (await _read_ts(response))[0] == TS_SYNC

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        assert await _image(hass, entity_id) is None  # asks for the live keyframe
        await wait_until(
            lambda: entry.runtime_data.snapshots.source_for(SYNTHETIC.camera_sn) == "live",
            timeout=10,
        )

    await response.content.read()
    assert response.status == 200
    aborted = [r.getMessage() for r in caplog.records if "aborting" in r.getMessage()]
    assert aborted == [f"Live stream of {redact_serial(SYNTHETIC.camera_sn)}: {YIELD_REASON}"]
    assert _errors(caplog) == []

    response.close()
    await _unload(hass, entry)


async def test_at_the_session_budget_a_live_capture_ends_only_the_view_holding_the_slot(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_client_no_auth: ClientSessionGenerator,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two cameras of one HomeBase live, the budget used up: a capture of the second ends
    the first's view, which holds the slot; the second's view on an extra session runs on.
    """
    fake_cloud.devices.append(
        camera_device(OTHER_CAMERA_SN, station_sn=fake_station.serial, channel=1, name="Back")
    )
    fake_station.params[1] = {1101: "80", 1142: "-60"}
    entry = await set_up_warm(
        hass, seed_warm_cache, options={CONF_STATION_SESSIONS: MIN_STATION_SESSIONS + 1}
    )
    station = entry.runtime_data.coordinators[SYNTHETIC.station_sn].station
    client = await hass_client_no_auth()
    r1 = await client.get(await _stream_path(hass, _camera_id(hass)))
    assert (await _read_ts(r1))[0] == TS_SYNC
    r2 = await client.get(await _stream_path(hass, _camera_id(hass, OTHER_CAMERA_SN)))
    assert (await _read_ts(r2))[0] == TS_SYNC
    assert sorted(fake_station.live_cameras) == [0, 1]
    assert station.media_slot_camera == SYNTHETIC.camera_sn

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        await _press_capture(hass, OTHER_CAMERA_SN)
        await wait_until(
            lambda: entry.runtime_data.snapshots.source_for(OTHER_CAMERA_SN) == "live",
            timeout=30,
        )

    await r1.content.read()
    assert r1.status == 200
    await _streams_on(r2)
    assert fake_station.live_cameras == [1]
    aborted = [r.getMessage() for r in caplog.records if "aborting" in r.getMessage()]
    assert aborted == [f"Live stream of {redact_serial(SYNTHETIC.camera_sn)}: {YIELD_REASON}"]
    assert _errors(caplog) == []

    r1.close()
    r2.close()
    await _unload(hass, entry)


def _record_wait(monkeypatch: pytest.MonkeyPatch, method: str, calls: list[bool | None]) -> None:
    """Wrap the public ``Station`` ``method`` to record each call's ``wait`` keyword."""
    orig = getattr(Station, method)

    async def recording(self: Station, *args: Any, **kw: Any) -> Any:
        calls.append(kw.get("wait"))
        return await orig(self, *args, **kw)

    monkeypatch.setattr(Station, method, recording)


async def test_at_the_session_budget_a_live_capture_frees_the_slot_without_waiting(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_client_no_auth: ClientSessionGenerator,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With one live stream allowed, a press first asks without waiting, is refused at
    once, ends the view on the slot and asks again waiting: no first-frame timeout passes.
    """
    calls: list[bool | None] = []
    _record_wait(monkeypatch, "async_camera_image", calls)
    entry = await set_up_warm(
        hass, seed_warm_cache, options={CONF_STATION_SESSIONS: MIN_STATION_SESSIONS}
    )
    client = await hass_client_no_auth()
    response = await client.get(await _stream_path(hass, _camera_id(hass)))
    assert (await _read_ts(response))[0] == TS_SYNC

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        start = time.monotonic()
        await _press_capture(hass, SYNTHETIC.camera_sn)
        await wait_until(
            lambda: entry.runtime_data.snapshots.source_for(SYNTHETIC.camera_sn) == "live",
            timeout=30,
        )
        elapsed = time.monotonic() - start

    assert calls == [False, True]
    assert elapsed < session_module.MEDIA_LIVE_FIRST_FRAME_TIMEOUT / 4
    await response.content.read()
    assert response.status == 200
    assert _errors(caplog) == []

    response.close()
    await _unload(hass, entry)


async def _set_up_with_ptz_camera(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    seed: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    **options: Any,
) -> tuple[MockConfigEntry, Station]:
    """Pair the pan/tilt camera, set up, read its slots; captures settle in 0.2 s."""
    orig = Station.async_preset_image
    monkeypatch.setattr(
        Station,
        "async_preset_image",
        lambda self, sn, preset, **kw: orig(self, sn, preset, **{**kw, "settle": 0.2}),
    )
    fake_cloud.devices.append(
        camera_device(PTZ_CAMERA_SN, station_sn=fake_station.serial, channel=1, name="Turret")
    )
    fake_station.params[1] = {1101: "80", 1142: "-60"}
    entry = await set_up_warm(hass, seed, options=options or None)
    station = entry.runtime_data.coordinators[SYNTHETIC.station_sn].station
    await station.async_refresh_presets(PTZ_CAMERA_SN)
    return entry, station


@pytest.mark.parametrize("budget_full", [False, True], ids=["beside", "at_budget"])
async def test_a_preset_capture_on_a_homebase_ends_its_view_only_at_the_session_budget(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_client_no_auth: ClientSessionGenerator,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    budget_full: bool,
) -> None:
    """A pan/tilt camera paired to a HomeBase, its own view running: the preset capture
    runs beside the view on an extra session, or, with the budget used up, ends the view
    and takes the freed slot.
    """
    options = {CONF_STATION_SESSIONS: MIN_STATION_SESSIONS} if budget_full else {}
    entry, station = await _set_up_with_ptz_camera(
        hass, fake_station, fake_cloud, seed_warm_cache, monkeypatch, **options
    )
    client = await hass_client_no_auth()
    response = await client.get(await _stream_path(hass, _camera_id(hass, PTZ_CAMERA_SN)))
    assert (await _read_ts(response))[0] == TS_SYNC
    assert station.media_slot_camera == PTZ_CAMERA_SN

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        entry.runtime_data.presets.async_request_preset(station, PTZ_CAMERA_SN, 1)
        await wait_until(
            lambda: entry.runtime_data.presets.image_for(PTZ_CAMERA_SN, 1) is not None,
            timeout=30,
        )

    aborted = [r.getMessage() for r in caplog.records if "aborting" in r.getMessage()]
    if budget_full:
        await response.content.read()
        assert response.status == 200
        assert aborted == [f"Live stream of {redact_serial(PTZ_CAMERA_SN)}: {YIELD_REASON}"]
    else:
        assert station.stats().extra_live_sessions == 1
        await _streams_on(response)
        assert aborted == []
    assert _errors(caplog) == []

    response.close()
    await _unload(hass, entry)


async def test_at_the_session_budget_a_preset_capture_frees_the_slot_without_waiting(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_client_no_auth: ClientSessionGenerator,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With one live stream allowed, a preset capture first asks without waiting, is
    refused at once, ends the view on the slot and asks again waiting.
    """
    entry, station = await _set_up_with_ptz_camera(
        hass,
        fake_station,
        fake_cloud,
        seed_warm_cache,
        monkeypatch,
        **{CONF_STATION_SESSIONS: MIN_STATION_SESSIONS},
    )
    calls: list[bool | None] = []
    _record_wait(monkeypatch, "async_preset_image", calls)
    client = await hass_client_no_auth()
    response = await client.get(await _stream_path(hass, _camera_id(hass, PTZ_CAMERA_SN)))
    assert (await _read_ts(response))[0] == TS_SYNC

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        start = time.monotonic()
        entry.runtime_data.presets.async_request_preset(station, PTZ_CAMERA_SN, 1)
        await wait_until(
            lambda: entry.runtime_data.presets.image_for(PTZ_CAMERA_SN, 1) is not None,
            timeout=30,
        )
        elapsed = time.monotonic() - start

    assert calls == [False, True]
    assert elapsed < session_module.MEDIA_LIVE_FIRST_FRAME_TIMEOUT / 4
    await response.content.read()
    assert response.status == 200
    assert _errors(caplog) == []

    response.close()
    await _unload(hass, entry)


async def test_a_pan_tilt_camera_paired_to_a_homebase_keeps_its_zoom_and_zooms_its_view(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_client_no_auth: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A T8170 behind a HomeBase gets the live-view zoom, and a zoom during its view
    reaches the camera: the HomeBase relays it, rather than answering -108.
    """
    fake_station.relayed_channels = {1}
    entry, _ = await _set_up_with_ptz_camera(
        hass, fake_station, fake_cloud, seed_warm_cache, monkeypatch
    )
    zoom_id = entity_id_for(hass, NUMBER_DOMAIN, PTZ_CAMERA_SN, "live_zoom")
    client = await hass_client_no_auth()
    response = await client.get(await _stream_path(hass, _camera_id(hass, PTZ_CAMERA_SN)))
    assert (await _read_ts(response))[0] == TS_SYNC

    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: zoom_id, ATTR_VALUE: 4},
        blocking=True,
    )

    assert fake_station.zoom_writes == [4.0]
    assert float(state_of(hass, zoom_id) or 0) == 4.0

    response.close()
    await _unload(hass, entry)


async def test_a_detection_still_ends_no_live_view_when_none_holds_the_media_slot(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_client_no_auth: ClientSessionGenerator,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A live view on an extra session keeps streaming once the slot's own view has ended."""
    fake_station.images[PUSHED_THUMB_PATH] = PUSHED_THUMBNAIL
    entry = await _set_up_with_other_camera(hass, fake_station, fake_cloud, seed_warm_cache)
    station = entry.runtime_data.coordinators[SYNTHETIC.station_sn].station
    client = await hass_client_no_auth()
    r1 = await client.get(await _stream_path(hass, _camera_id(hass)))
    assert (await _read_ts(r1))[0] == TS_SYNC
    r2 = await client.get(await _stream_path(hass, _camera_id(hass, OTHER_CAMERA_SN)))
    assert (await _read_ts(r2))[0] == TS_SYNC
    r1.close()
    await wait_until(lambda: fake_station.live_cameras == [1], timeout=20)
    await wait_until(lambda: station.media_slot_camera is None, timeout=20)

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        entry.runtime_data.router.handle(
            detection_event(
                DetectionType.PERSON,
                t_ms=now_ms(),
                device_sn=OTHER_CAMERA_SN,
                thumb_path=PUSHED_THUMB_PATH,
            )
        )
        await wait_until(
            lambda: entry.runtime_data.snapshots.image_for(OTHER_CAMERA_SN) is not None,
            timeout=30,
        )

    assert (await _read_ts(r2))[0] == TS_SYNC
    assert fake_station.live_cameras == [1]
    assert not any("aborting" in r.getMessage() for r in caplog.records)
    assert _errors(caplog) == []

    r2.close()
    await _unload(hass, entry)


# ── the view's own surface ────────────────────────────────────────────────────


async def test_an_unknown_serial_is_404_and_opens_nothing(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """Only a serial currently in the registry is served at all."""
    entry = await set_up_warm(hass, seed_warm_cache)
    client = await hass_client_no_auth()

    for serial in (SENSOR_SN, OTHER_CAMERA_SN):
        parts = urlparse(streaming.stream_url(hass, serial))
        response = await client.get(f"{parts.path}?{parts.query}")
        assert response.status == 404

    await asyncio.sleep(0.2)
    assert fake_station.live_opens == []
    assert _commands(fake_station, LIVE_OPEN) == 0

    await _unload(hass, entry)


async def test_the_view_is_registered_once_per_instance_not_once_per_entry(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """register_view has no unregister, so a second account must not add a route."""
    entry = await set_up_warm(hass, seed_warm_cache)
    assert _stream_routes(hass) == 1

    other_email = "second@example.com"
    seed_warm_cache(email=other_email)
    second = add_entry(hass, email=other_email)
    assert await setup_entry(hass, second)

    assert _stream_routes(hass) == 1
    assert entry.state is ConfigEntryState.LOADED
    assert second.state is ConfigEntryState.LOADED

    client = await hass_client_no_auth()
    response = await client.get(await _stream_path(hass, _camera_id(hass)))
    assert response.status == 200

    response.close()
    await _unload(hass, second)
    await _unload(hass, entry)


async def test_unload_closes_every_stream_and_leaves_nothing_running(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """Unload releases an attached viewer and drops the serial from the registry."""
    entry = await set_up_warm(hass, seed_warm_cache)
    client = await hass_client_no_auth()
    path = await _stream_path(hass, _camera_id(hass))

    response = await client.get(path)
    await wait_until(lambda: fake_station.streaming, timeout=20)

    await _unload(hass, entry)

    # The attached viewer's response ended, rather than hanging on a dead stream.
    await response.content.read()
    assert response.status == 200
    assert fake_station.streaming is False

    after = await client.get(path)
    assert after.status == 404

    response.close()
    after.close()


# ── who may read a stream: HA's own consumers only ──────────────────────────


@pytest.mark.parametrize(
    ("query", "headers"),
    [
        ("", {}),
        ("?auth=wrong", {}),
        (None, {"X-Real-IP": "203.0.113.7"}),
        (None, {"Forwarded": "for=203.0.113.7"}),
    ],
    ids=["no-secret", "wrong-secret", "x-real-ip", "forwarded"],
)
async def test_a_request_without_the_secret_or_through_a_proxy_is_403_and_opens_nothing(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_client_no_auth: ClientSessionGenerator,
    query: str | None,
    headers: dict[str, str],
) -> None:
    """A real camera serial is not enough: the secret and a direct local peer are needed."""
    entry = await set_up_warm(hass, seed_warm_cache)
    client = await hass_client_no_auth()
    path = await _stream_path(hass, _camera_id(hass))
    if query is not None:
        path = path.partition("?")[0] + query

    response = await client.get(path, headers=headers)

    assert response.status == 403
    assert await response.read() == b""
    await asyncio.sleep(0.2)
    assert fake_station.live_opens == []
    assert _commands(fake_station, LIVE_OPEN) == 0

    response.close()
    await _unload(hass, entry)


@pytest.mark.parametrize("peer", ["192.0.2.10", "10.0.0.1", "2001:db8::1", "::ffff:192.0.2.10"])
async def test_a_peer_that_is_not_this_host_is_403_even_with_the_secret(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    peer: str,
) -> None:
    """The socket peer decides, so a leaked URL is useless from another machine."""
    entry = await set_up_warm(hass, seed_warm_cache)
    parts = urlparse(await async_get_stream_source(hass, _camera_id(hass)) or "")
    view = streaming.EufyStreamView(streaming.stream_registry(hass))

    request = make_mocked_request(
        "GET", f"{parts.path}?{parts.query}", transport=_Peer(peer), app=hass.http.app
    )
    response = await view.get(request, SYNTHETIC.camera_sn)

    assert response.status == 403
    await asyncio.sleep(0.2)
    assert fake_station.live_opens == []

    await _unload(hass, entry)


@pytest.mark.parametrize("peer", ["127.0.0.1", "::1", "::ffff:127.0.0.1"])
def test_a_loopback_peer_with_the_secret_is_allowed(hass: HomeAssistant, peer: str) -> None:
    """Every loopback spelling counts as this host."""
    registry = streaming.stream_registry(hass)
    view = streaming.EufyStreamView(registry)
    request = make_mocked_request(
        "GET", f"/api/eufy_home_security/stream/x?auth={registry.secret}", transport=_Peer(peer)
    )
    assert view._refusal(request) is None


def test_the_secret_differs_per_instance() -> None:
    """A fresh registry (a Home Assistant restart) mints a new secret."""
    first, second = streaming.StreamRegistry(), streaming.StreamRegistry()
    assert first.secret != second.secret
    assert len(first.secret) >= 32


@pytest.mark.parametrize("streaming_yet", [False, True], ids=["opening", "writing"])
async def test_a_cancelled_view_releases_the_camera_and_stays_cancelled(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    streaming_yet: bool,
) -> None:
    """A cancel (HA stopping, the handler torn down), while the camera opens or while
    chunks flow, unsubscribes and propagates; no status is sent for an open."""
    opening = asyncio.Event()
    if not streaming_yet:

        async def never_opens(*_args: Any) -> Any:
            opening.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(streaming.StreamManager, "_async_open", never_opens)
    entry = await set_up_warm(hass, seed_warm_cache)
    parts = urlparse(await async_get_stream_source(hass, _camera_id(hass)) or "")
    request = make_mocked_request(
        "GET", f"{parts.path}?{parts.query}", transport=_Peer("127.0.0.1")
    )
    registry = streaming.stream_registry(hass)
    broadcast = registry.broadcasts[SYNTHETIC.camera_sn]
    task = asyncio.create_task(streaming.EufyStreamView(registry).get(request, SYNTHETIC.camera_sn))
    writer: Any = request.writer
    if streaming_yet:
        await wait_until(lambda: writer.write.call_count > 0, timeout=20)
    else:
        await wait_until(opening.is_set, timeout=20)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    await wait_until(lambda: broadcast.subscribers == 0, timeout=20)
    if not streaming_yet:
        assert writer.write_headers.call_count == 0
    await _unload(hass, entry)


# ── what the logs carry, a clean end, and the capability gate ─────────────────


async def test_no_stream_log_line_carries_a_raw_serial_or_the_url(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_client_no_auth: ClientSessionGenerator,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The serial in the path is a scoped exception; the logs carry neither it nor the URL."""
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        entry = await set_up_warm(hass, seed_warm_cache)
        client = await hass_client_no_auth()
        path = await _stream_path(hass, _camera_id(hass))
        r1 = await client.get(path)
        r2 = await client.get(path)
        await wait_until(lambda: fake_station.media_frames_sent > 0, timeout=20)
        r1.close()
        r2.close()
        await _unload(hass, entry)

    ours = [record for record in caplog.records if record.name.startswith(LOGGER)]
    secret = streaming.stream_registry(hass).secret
    for record in ours:
        message = record.getMessage()
        assert SYNTHETIC.camera_sn not in message
        assert SYNTHETIC.station_sn not in message
        assert secret not in message
    assert "http://127.0.0.1" not in caplog.text
    # Non-vacuity: the redacted serial really does appear.
    assert any(redact_serial(SYNTHETIC.camera_sn) in record.getMessage() for record in ours)


async def test_a_stream_that_ends_by_itself_is_a_clean_end_not_an_error(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_client_no_auth: ClientSessionGenerator,
    caplog: pytest.LogCaptureFixture,
    short_media_idle: None,
) -> None:
    """A stream that simply stops is never a 500, never an error, never reopened."""
    fake_station.live_ends_unpinged_after = 0.05
    entry = await set_up_warm(hass, seed_warm_cache)
    client = await hass_client_no_auth()
    path = await _stream_path(hass, _camera_id(hass))

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        opening = asyncio.create_task(client.get(path))
        await wait_until(lambda: fake_station.live_opens != [], timeout=20)
        opens = len(fake_station.live_opens)
        await wait_until(lambda: not fake_station.streaming, timeout=20)
        response = await opening
        await response.content.read()
        await asyncio.sleep(0.3)

    assert response.status == 200
    loud = [
        record.getMessage()
        for record in caplog.records
        if record.name.startswith(LOGGER) and record.levelno >= logging.WARNING
    ]
    assert loud == []
    assert any("ended after" in record.getMessage() for record in caplog.records)
    # No auto-restart: nothing reopened the camera after the stream ended.
    assert len(fake_station.live_opens) == opens

    response.close()
    await _unload(hass, entry)


def test_a_model_without_the_live_stream_capability_advertises_no_stream() -> None:
    """The gate is the library's catalog, never a serial prefix."""
    assert detections.has_live_stream(SYNTHETIC.camera_sn) is True
    assert detections.has_live_stream(SENSOR_SN) is False
    assert detections.has_live_stream(SYNTHETIC.station_sn) is False


# ── a picture-changing setting restarts the view ──────────────────────────────


async def test_changing_the_streaming_quality_ends_a_running_live_view(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_client_no_auth: ClientSessionGenerator,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A streaming-quality write ends the running view; nothing reopens the camera."""
    entry = await set_up_warm(hass, seed_warm_cache)
    client = await hass_client_no_auth()
    path = await _stream_path(hass, _camera_id(hass))

    response = await client.get(path)
    await wait_until(lambda: fake_station.streaming, timeout=20)
    opens = len(fake_station.live_opens)

    quality = entity_id_for(hass, SELECT_DOMAIN, SYNTHETIC.camera_sn, "live_streaming_resolution")
    high = settings_of("T8160")["live_streaming_resolution"].label(10)
    assert high is not None
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        await hass.services.async_call(
            SELECT_DOMAIN,
            SERVICE_SELECT_OPTION,
            {ATTR_ENTITY_ID: quality, ATTR_OPTION: high},
            blocking=True,
        )
        await wait_until(lambda: not fake_station.streaming, timeout=20)

    # The write landed, and the view ended because of it — not by coincidence.
    quality_state = hass.states.get(quality)
    assert quality_state is not None
    assert quality_state.state == high
    assert any(
        "a setting changed the picture the camera sends" in record.getMessage()
        for record in caplog.records
    )
    # The viewer saw a normal end: reading to EOF raises nothing, and nothing is loud.
    await response.content.read()
    assert response.status == 200
    assert _errors(caplog) == []
    # Nothing reopened the camera by itself; the next viewer does that.
    assert len(fake_station.live_opens) == opens

    response.close()
    await _unload(hass, entry)


async def test_an_unrelated_setting_leaves_a_running_live_view_alone(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """Only a setting that moves the picture costs a watching user their stream."""
    entry = await set_up_warm(hass, seed_warm_cache)
    client = await hass_client_no_auth()
    path = await _stream_path(hass, _camera_id(hass))

    response = await client.get(path)
    await wait_until(lambda: fake_station.streaming, timeout=20)

    night = entity_id_for(hass, SELECT_DOMAIN, SYNTHETIC.camera_sn, "nightvision_type_new")
    fake_station.reply_to_settings = True
    await hass.services.async_call(
        SELECT_DOMAIN,
        SERVICE_SELECT_OPTION,
        {ATTR_ENTITY_ID: night, ATTR_OPTION: "Off"},
        blocking=True,
    )
    await asyncio.sleep(0.3)

    night_state = hass.states.get(night)
    assert night_state is not None
    assert night_state.state == "Off"
    assert fake_station.streaming is True  # the view is untouched

    response.close()
    await _unload(hass, entry)


# ── a HomeBase camera that cannot be woken ────────────────────────────────────


async def test_a_camera_the_homebase_cannot_wake_answers_503_and_is_not_asked_again(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    hass_client_no_auth: ClientSessionGenerator,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """-204 on the live open: HTTP 503, no ERROR, the station stays available, and the
    library's wake backoff refuses the next open without sending."""
    fake_station.live_open_receipt_code = -204
    entry = await set_up_warm(hass, seed_warm_cache)
    client = await hass_client_no_auth()
    path = await _stream_path(hass, _camera_id(hass))
    coordinator = entry.runtime_data.coordinators[SYNTHETIC.station_sn]

    with caplog.at_level(logging.DEBUG):
        first = await client.get(path)
        second = await client.get(path)

    assert first.status == 503
    assert second.status == 503
    assert await first.read() == b""
    assert len(fake_station.live_opens) == 1
    assert coordinator.last_update_success is True
    assert _errors(caplog) == []
    assert any("did not wake" in record.getMessage() for record in caplog.records)

    first.close()
    second.close()
    await _unload(hass, entry)
