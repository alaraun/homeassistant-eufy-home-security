"""A station recording played straight to a viewer (``recording_plays.py``).

The websocket subscription ``eufy_home_security/recordings/play`` on Home Assistant's
test client, the library's ``FakeStation`` behind it, and Home Assistant's ``Stream``
replaced by :class:`FakeStream`, which records what the play does with it. Where a test
needs the stream worker's side, it reads the play's loopback URL itself through the real
view. ``Station.async_play_recording`` is scripted by :class:`Player` where a test needs
a library outcome at a chosen moment; one test plays through the real library.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final
from urllib.parse import urlparse

import pytest
from conftest import SYNTHETIC, add_entry, entity_id_for, setup_entry, wait_until
from eufy_home_security import (
    AccountRejectedError,
    ClipWriter,
    DeviceTimeoutError,
    EufySecurity,
    HistoryRecord,
    LiveStreamLimitError,
    MediaClip,
    Station,
)
from eufy_home_security.testing import FakeStation
from homeassistant.components.camera import DOMAIN as CAMERA_DOMAIN
from homeassistant.components.camera import DynamicStreamSettings
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.components.diagnostics import get_diagnostics_for_config_entry
from pytest_homeassistant.typing import (
    ClientSessionGenerator,
    MockHAClientWebSocket,
    WebSocketGenerator,
)

from custom_components.eufy_home_security import recording_plays, station_recordings, streaming
from custom_components.eufy_home_security.const import CAMERA_KEY

_DAY_FACTOR: Final = 100_000
_ROW_TIME: Final = "%Y-%m-%d %H:%M:%S"
# One MPEG-TS packet's worth of bytes, sync byte first.
TS_CHUNK: Final = b"\x47" + b"\x00" * 187
HLS_URL: Final = "/api/hls/test-token/master_playlist.m3u8"
# The playback command the station gets for a play, and the download command.
RECORD_VIEW: Final = 1025
RECORDING_COMMANDS: Final = (1024, RECORD_VIEW)


class FakeStream:
    """Stands in for Home Assistant's ``Stream``: records the source, provider, start and stops."""

    def __init__(self, source: str, options: Mapping[str, Any], label: str | None) -> None:
        self.source = source
        self.options = dict(options)
        self.label = label
        self.providers: list[str] = []
        self.started = False
        self.stops = 0
        # Held open by the test: ``start`` waits for it.
        self.start_gate: asyncio.Event | None = None
        self.starting = asyncio.Event()
        # Seconds ``stop`` takes, as joining the worker thread does.
        self.stop_delay = 0.0

    def add_provider(self, fmt: str, timeout: int | None = None) -> object:
        self.providers.append(fmt)
        return object()

    async def start(self) -> None:
        self.starting.set()
        if self.start_gate is not None:
            await self.start_gate.wait()
        self.started = True

    def endpoint_url(self, fmt: str) -> str:
        # As Home Assistant's: a stopped Stream has no outputs.
        if fmt not in self.providers:
            raise ValueError(f"Stream is not configured for format '{fmt}'")
        return HLS_URL

    async def stop(self) -> None:
        self.providers = []
        if self.stop_delay:
            await asyncio.sleep(self.stop_delay)
        self.stops += 1


@pytest.fixture
def start_gate() -> asyncio.Event | None:
    """The gate every FakeStream's start waits for; None: starts at once."""
    return None


@pytest.fixture
def streams(monkeypatch: pytest.MonkeyPatch, start_gate: asyncio.Event | None) -> list[FakeStream]:
    """Every Stream a play creates, in order."""
    made: list[FakeStream] = []

    def create(
        hass: HomeAssistant,
        source: str,
        options: Mapping[str, Any],
        dynamic: DynamicStreamSettings,
        stream_label: str | None = None,
    ) -> FakeStream:
        assert isinstance(dynamic, DynamicStreamSettings)
        stream = FakeStream(source, options, stream_label)
        stream.start_gate = start_gate
        made.append(stream)
        return stream

    monkeypatch.setattr(recording_plays, "create_stream", create)
    return made


@pytest.fixture
def short_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Stream outlives a play's end by a twentieth of a second."""
    monkeypatch.setattr(recording_plays, "PLAY_END_GRACE", 0.05)


@pytest.fixture
def short_attach(monkeypatch: pytest.MonkeyPatch) -> None:
    """A play nobody reads ends after a fifth of a second."""
    monkeypatch.setattr(recording_plays, "PLAY_ATTACH_TIMEOUT", 0.2)


@dataclass
class Player:
    """``Station.async_play_recording`` as the test scripts it.

    Writes ``chunks``; waits for ``release`` if given and writes ``then``; raises
    ``error``, or waits to be cancelled (``hold``), or returns a clip (``ended_early``
    as given).
    """

    chunks: list[bytes] = field(default_factory=lambda: [TS_CHUNK, TS_CHUNK])
    release: asyncio.Event | None = None
    then: list[bytes] = field(default_factory=list)
    error: Exception | None = None
    hold: bool = False
    ended_early: bool = False
    calls: list[tuple[int, bool, bool]] = field(default_factory=list)
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    returned: int = 0
    duration_s: float = 1.0

    def install(self, monkeypatch: pytest.MonkeyPatch) -> Player:
        async def play(
            station: Station,
            record: HistoryRecord,
            write: ClipWriter,
            *,
            wait: bool = True,
            realtime: bool = True,
        ) -> MediaClip:
            self.calls.append((record.record_id, wait, realtime))
            self.entered.set()
            try:
                for chunk in self.chunks:
                    await write(chunk)
                if self.release is not None:
                    await self.release.wait()
                for chunk in self.then:
                    await write(chunk)
                    await asyncio.sleep(0.02)
                if self.error is not None:
                    raise self.error
                if self.hold:
                    await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
            self.returned += 1
            return MediaClip(
                video_frames=1,
                audio_frames=0,
                keyframes=1,
                bytes_written=sum(len(c) for c in self.chunks),
                duration_s=self.duration_s,
                width=1,
                height=1,
                resizes=0,
                ended_early=self.ended_early,
            )

        monkeypatch.setattr(Station, "async_play_recording", play)
        return self


def _row(counter: int, *, ago: timedelta = timedelta(minutes=10)) -> dict[str, Any]:
    """A settled history row of the synthetic camera, recorded ``ago``."""
    start = (datetime.now().astimezone() - ago).replace(microsecond=0)
    return {
        "record_id": int(start.strftime("%Y%m%d")) * _DAY_FACTOR + counter,
        "device_sn": SYNTHETIC.camera_sn,
        "start_time": start.strftime(_ROW_TIME),
        "end_time": (start + timedelta(seconds=10)).strftime(_ROW_TIME),
        "storage_path": f"/zx/hdd_data0/Camera00/{counter}.zxvideo",
        "frame_num": 6,
    }


def _camera(hass: HomeAssistant) -> str:
    return entity_id_for(hass, CAMERA_DOMAIN, SYNTHETIC.camera_sn, CAMERA_KEY)


def _plays(entry: MockConfigEntry) -> recording_plays.RecordingPlays:
    manager = entry.runtime_data.station_recordings
    assert manager is not None
    return manager.plays


async def _set_up(
    hass: HomeAssistant, seed_warm_cache: Callable[..., None], fake_station: FakeStation
) -> MockConfigEntry:
    """The account set up with one settled recording on the station."""
    fake_station.rows = [_row(1)]
    seed_warm_cache()
    entry = add_entry(hass)
    assert await setup_entry(hass, entry)
    return entry


async def _ref(client: MockHAClientWebSocket, entity_id: str) -> str:
    await client.send_json_auto_id({"type": station_recordings.WS_LIST, "entity_id": entity_id})
    reply = await client.receive_json()
    ref: str = reply["result"]["recordings"][0]["ref"]
    return ref


async def _play(client: MockHAClientWebSocket, entity_id: str, ref: str) -> dict[str, Any]:
    """Subscribe a play; its answer: the error, or the empty result with the first
    event's payload under ``started`` (it follows the result at once)."""
    await client.send_json_auto_id(
        {"type": station_recordings.WS_PLAY, "entity_id": entity_id, "ref": ref}
    )
    reply = await _next(client)
    if reply["success"]:
        assert reply["result"] is None
        started = await _next(client)
        assert (started["id"], started["type"]) == (reply["id"], "event")
        reply["started"] = started["event"]
    return reply


async def _next(client: MockHAClientWebSocket) -> dict[str, Any]:
    reply: dict[str, Any] = await asyncio.wait_for(client.receive_json(), 5)
    return reply


async def _nothing_more(client: MockHAClientWebSocket) -> None:
    """The next message is the answer to a ping: no event came before it."""
    await client.send_json_auto_id({"type": "ping"})
    assert (await _next(client))["type"] == "pong"


def _source_path(stream: FakeStream) -> str:
    """The play's loopback URL as the test client asks it (path and secret query)."""
    parts = urlparse(stream.source)
    return f"{parts.path}?{parts.query}"


async def _unload(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


# ── a play from start to end ──────────────────────────────────────────────────


async def test_a_play_answers_the_hls_url_and_ends_complete_after_the_stream_stops(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    hass_client_no_auth: ClientSessionGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    streams: list[FakeStream],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Through the real library: one Stream on the loopback URL with the run secret, an
    HLS provider, the plays' one label, started; an empty result, then the HLS URL as
    the first event. The worker's GET gets the recording's MPEG-TS to its end; the one
    event ``complete`` with the played length comes at once, while the Stream still
    runs; a second GET is 410; after the grace the Stream is stopped and the id is
    unknown (404)."""
    # Long enough for the second GET to land inside the grace.
    monkeypatch.setattr(recording_plays, "PLAY_END_GRACE", 0.5)
    entry = await _set_up(hass, seed_warm_cache, fake_station)
    client = await hass_ws_client(hass)
    entity_id = _camera(hass)

    reply = await _play(client, entity_id, await _ref(client, entity_id))

    assert reply["success"], reply
    assert reply["started"] == {"url": HLS_URL, "mime_type": "application/vnd.apple.mpegurl"}
    (stream,) = streams
    source = urlparse(stream.source)
    assert (source.scheme, source.hostname, source.port) == (
        "http",
        "127.0.0.1",
        hass.http.server_port,
    )
    assert source.path.startswith("/api/eufy_home_security/stream/recording/")
    assert source.query == f"auth={streaming.stream_registry(hass).secret}"
    assert (stream.options, stream.providers, stream.started) == ({}, ["hls"], True)
    assert stream.label == "eufy_home_security_recording"

    http = await hass_client_no_auth()
    response = await http.get(_source_path(stream))
    assert response.status == 200
    assert response.content_type == "video/mp2t"
    body = await response.read()
    assert body[:1] == b"\x47"
    assert len(body) % 188 == 0
    event = await _next(client)
    assert (event["id"], event["type"], event["event"]["end"]) == (reply["id"], "event", "complete")
    assert event["event"]["duration"] > 0
    assert stream.stops == 0
    again = await http.get(_source_path(stream))
    assert again.status == 410

    await wait_until(lambda: stream.stops == 1)
    assert [o["cmd"] for o in fake_station.received if o.get("cmd") in RECORDING_COMMANDS] == [
        RECORD_VIEW
    ]
    await wait_until(lambda: _plays(entry).diagnostics()["active"] == 0)
    assert (await http.get(_source_path(stream))).status == 404
    await _nothing_more(client)
    assert _plays(entry).diagnostics() == {
        "started": 1,
        "complete": 1,
        "viewer_left": 0,
        "failed": {},
        "active": 0,
    }
    await _unload(hass, entry)


@pytest.mark.usefixtures("short_grace")
async def test_a_play_asks_the_library_to_wait_for_a_session_and_keep_the_recordings_pace(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    streams: list[FakeStream],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``wait`` and ``realtime`` on, for the listed row; a recording shorter than the
    answer's way out still gets its result and URL first, then ``complete`` with the
    clip's length, or the record's when the clip has none."""
    player = Player().install(monkeypatch)
    entry = await _set_up(hass, seed_warm_cache, fake_station)
    client = await hass_ws_client(hass)
    ref = await _ref(client, _camera(hass))

    reply = await _play(client, _camera(hass), ref)

    assert reply["success"], reply
    assert player.calls == [(fake_station.rows[0]["record_id"], True, True)]
    assert (await _next(client))["event"] == {"end": "complete", "duration": 1.0}
    player.duration_s = 0.0
    assert (await _play(client, _camera(hass), ref))["success"]
    assert (await _next(client))["event"] == {"end": "complete", "duration": 10.0}
    await _unload(hass, entry)


@pytest.mark.parametrize(
    ("error", "ended_early", "end"),
    [
        (None, False, "complete"),
        (DeviceTimeoutError("silent"), False, "unavailable"),
        (None, True, "codec_change"),
        (RuntimeError("unexpected"), False, "failed"),
    ],
    ids=["complete", "library-error", "codec-change", "unexpected-error"],
)
async def test_the_stream_outlives_the_recordings_end_by_the_grace(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    streams: list[FakeStream],
    monkeypatch: pytest.MonkeyPatch,
    error: Exception | None,
    ended_early: bool,
    end: str,
) -> None:
    """The end event goes out at once, while the Stream runs on; the Stream is stopped
    ``PLAY_END_GRACE`` after the library returns or raises, not before, so the player
    still fetches what arrived."""
    monkeypatch.setattr(recording_plays, "PLAY_END_GRACE", 0.4)
    release = asyncio.Event()
    Player(release=release, error=error, ended_early=ended_early).install(monkeypatch)
    entry = await _set_up(hass, seed_warm_cache, fake_station)
    client = await hass_ws_client(hass)
    assert (await _play(client, _camera(hass), await _ref(client, _camera(hass))))["success"]
    (stream,) = streams
    loop = asyncio.get_running_loop()

    release.set()
    ended = loop.time()
    event = await _next(client)

    assert (event["event"].get("code", "complete"), stream.stops) == (end, 0)
    assert loop.time() - ended < 0.4
    await wait_until(lambda: stream.stops == 1)
    assert loop.time() - ended >= 0.4
    await _unload(hass, entry)


# ── errors ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (LiveStreamLimitError("spent", limit=1), "busy"),
        (AccountRejectedError(1025, -104), "account_rejected"),
        (DeviceTimeoutError("no keyframe"), "unavailable"),
    ],
    ids=["busy", "account_rejected", "unavailable"],
)
async def test_an_error_before_the_first_bytes_is_the_fetch_answer(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    streams: list[FakeStream],
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    code: str,
) -> None:
    """The subscription is refused with the code and message a fetch gives for the same
    library error, no Stream is created, and the failure is counted by its code."""
    entry = await _set_up(hass, seed_warm_cache, fake_station)
    client = await hass_ws_client(hass)
    ref = await _ref(client, _camera(hass))

    async def failing(*_args: Any, **_kwargs: Any) -> MediaClip:
        raise error

    monkeypatch.setattr(Station, "async_download_recording", failing)
    await client.send_json_auto_id(
        {"type": station_recordings.WS_FETCH, "entity_id": _camera(hass), "ref": ref}
    )
    fetched = await _next(client)
    Player(chunks=[], error=error).install(monkeypatch)

    reply = await _play(client, _camera(hass), ref)

    assert not reply["success"], reply
    assert reply["error"] == fetched["error"]
    assert reply["error"]["code"] == code
    assert streams == []
    assert _plays(entry).diagnostics()["failed"] == {code: 1}
    await _nothing_more(client)
    await _unload(hass, entry)


async def test_a_busy_play_says_every_session_is_in_use(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    streams: list[FakeStream],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past the session budget: ``busy``, the HomeBase's sessions named."""
    Player(chunks=[], error=LiveStreamLimitError("spent", limit=2)).install(monkeypatch)
    entry = await _set_up(hass, seed_warm_cache, fake_station)
    client = await hass_ws_client(hass)

    reply = await _play(client, _camera(hass), await _ref(client, _camera(hass)))

    assert reply["error"] == {
        "code": "busy",
        "message": "Every session of the HomeBase is in use",
    }
    await _unload(hass, entry)


async def test_a_play_that_writes_nothing_answers_unavailable(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    streams: list[FakeStream],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A library call that returns without a byte is the station not playing it."""
    Player(chunks=[]).install(monkeypatch)
    entry = await _set_up(hass, seed_warm_cache, fake_station)
    client = await hass_ws_client(hass)

    reply = await _play(client, _camera(hass), await _ref(client, _camera(hass)))

    assert reply["error"]["code"] == "unavailable"
    assert streams == []
    await _unload(hass, entry)


async def test_a_play_while_the_station_is_away_is_unavailable(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    streams: list[FakeStream],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A listed recording of a station that has dropped is not asked for."""
    player = Player().install(monkeypatch)
    entry = await _set_up(hass, seed_warm_cache, fake_station)
    client = await hass_ws_client(hass)
    ref = await _ref(client, _camera(hass))
    monkeypatch.setattr(Station, "connected", property(lambda _self: False))

    reply = await _play(client, _camera(hass), ref)

    assert reply["error"] == {"code": "unavailable", "message": "The HomeBase is not connected"}
    assert player.calls == []
    monkeypatch.undo()
    await _unload(hass, entry)


async def test_a_request_the_station_cannot_play_is_refused_before_the_library(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    streams: list[FakeStream],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ref the library did not issue is ``not_found``, a recording still running
    ``not_settled``, another integration's entity ``not_found``; a key the command does
    not take is ``invalid_format``. None reaches the library."""
    player = Player().install(monkeypatch)
    entry = await _set_up(hass, seed_warm_cache, fake_station)
    fake_station.rows.append(_row(2, ago=timedelta(seconds=5)))
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": station_recordings.WS_LIST, "entity_id": _camera(hass)})
    listed = (await _next(client))["result"]["recordings"]
    running = next(r["ref"] for r in listed if not r["settled"])

    assert (await _play(client, _camera(hass), "v1.r.garbled"))["error"]["code"] == "not_found"
    assert (await _play(client, _camera(hass), running))["error"]["code"] == "not_settled"
    assert (await _play(client, "camera.other", running))["error"]["code"] == "not_found"
    await client.send_json_auto_id(
        {
            "type": station_recordings.WS_PLAY,
            "entity_id": _camera(hass),
            "ref": running,
            "save": True,
        }
    )
    assert (await _next(client))["error"]["code"] == "invalid_format"
    assert player.calls == []
    assert streams == []
    assert _plays(entry).diagnostics()["started"] == 0
    await _unload(hass, entry)


@pytest.mark.usefixtures("short_grace")
@pytest.mark.parametrize(
    ("error", "ended_early", "code"),
    [
        (DeviceTimeoutError("silent"), False, "unavailable"),
        (None, True, "codec_change"),
    ],
    ids=["library-error", "codec-change"],
)
async def test_an_end_after_the_first_bytes_is_one_error_event(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    hass_client_no_auth: ClientSessionGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    streams: list[FakeStream],
    monkeypatch: pytest.MonkeyPatch,
    error: Exception | None,
    ended_early: bool,
    code: str,
) -> None:
    """A library error after the result, or a clip cut at a codec change, ends the HTTP
    response after what arrived, sends one ``error`` event and stops the Stream."""
    release = asyncio.Event()
    Player(error=error, ended_early=ended_early, release=release).install(monkeypatch)
    entry = await _set_up(hass, seed_warm_cache, fake_station)
    client = await hass_ws_client(hass)
    assert (await _play(client, _camera(hass), await _ref(client, _camera(hass))))["success"]
    (stream,) = streams
    http = await hass_client_no_auth()
    response = await http.get(_source_path(stream))

    release.set()
    event = (await _next(client))["event"]

    assert await response.read() == TS_CHUNK * 2
    assert event["end"] == "error"
    assert event["code"] == code
    assert event["message"]
    await wait_until(lambda: stream.stops == 1)
    await _nothing_more(client)
    assert _plays(entry).diagnostics()["failed"] == {code: 1}
    await _unload(hass, entry)


async def test_a_play_without_home_assistants_stream_fails_and_stops_the_library(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Stream that cannot be created answers ``failed`` and cancels the play."""
    player = Player(hold=True).install(monkeypatch)

    def unavailable(*_args: Any, **_kwargs: Any) -> Any:
        raise HomeAssistantError("Stream integration is not set up.")

    monkeypatch.setattr(recording_plays, "create_stream", unavailable)
    entry = await _set_up(hass, seed_warm_cache, fake_station)
    client = await hass_ws_client(hass)

    reply = await _play(client, _camera(hass), await _ref(client, _camera(hass)))

    assert reply["error"]["code"] == "failed"
    await asyncio.wait_for(player.cancelled.wait(), 5)
    assert _plays(entry).diagnostics()["active"] == 0
    await _unload(hass, entry)


# ── the viewer leaves, or nobody reads ────────────────────────────────────────


async def test_unsubscribing_cancels_the_library_and_stops_the_stream_with_no_event(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    streams: list[FakeStream],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The card closes the player: the play is cancelled, the Stream stopped at once,
    no event; counted as the viewer leaving."""
    player = Player(hold=True).install(monkeypatch)
    entry = await _set_up(hass, seed_warm_cache, fake_station)
    client = await hass_ws_client(hass)
    reply = await _play(client, _camera(hass), await _ref(client, _camera(hass)))
    (stream,) = streams

    await client.send_json_auto_id({"type": "unsubscribe_events", "subscription": reply["id"]})

    assert (await _next(client))["success"]
    await asyncio.wait_for(player.cancelled.wait(), 5)
    await wait_until(lambda: stream.stops == 1)
    await _nothing_more(client)
    counts = _plays(entry).diagnostics()
    assert (counts["viewer_left"], counts["active"], counts["failed"]) == (1, 0, {})
    await _unload(hass, entry)


async def test_unsubscribing_during_the_grace_stops_the_stream_at_once(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    hass_client_no_auth: ClientSessionGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    streams: list[FakeStream],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The recording has ended (its event sent) and the Stream waits out its grace: the
    source is gone for a first reader too (410), and an unsubscribe stops the Stream
    now, with no further event."""
    monkeypatch.setattr(recording_plays, "PLAY_END_GRACE", 60.0)
    release = asyncio.Event()
    player = Player(release=release).install(monkeypatch)
    entry = await _set_up(hass, seed_warm_cache, fake_station)
    client = await hass_ws_client(hass)
    reply = await _play(client, _camera(hass), await _ref(client, _camera(hass)))
    (stream,) = streams
    release.set()
    assert (await _next(client))["event"]["end"] == "complete"
    await asyncio.sleep(0.1)
    assert (player.returned, stream.stops) == (1, 0)
    assert (await (await hass_client_no_auth()).get(_source_path(stream))).status == 410

    await client.send_json_auto_id({"type": "unsubscribe_events", "subscription": reply["id"]})

    assert (await _next(client))["success"]
    await wait_until(lambda: stream.stops == 1)
    await _nothing_more(client)
    await _unload(hass, entry)


@pytest.mark.parametrize(
    ("first_bytes", "how"),
    [(True, "close"), (False, "close"), (False, "unsubscribe")],
    ids=["playing-closed", "starting-closed", "starting-unsubscribed"],
)
async def test_a_viewer_that_leaves_cancels_its_play(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    streams: list[FakeStream],
    monkeypatch: pytest.MonkeyPatch,
    first_bytes: bool,
    how: str,
) -> None:
    """Home Assistant unsubscribes a closed connection: a play that has answered stops
    its Stream; one still waiting for its first bytes never creates one, and one
    unsubscribed before its result gets no answer after the unsubscribe's."""
    player = Player(chunks=[TS_CHUNK] if first_bytes else [], hold=True).install(monkeypatch)
    entry = await _set_up(hass, seed_warm_cache, fake_station)
    client = await hass_ws_client(hass)
    ref = await _ref(client, _camera(hass))
    request = {"type": station_recordings.WS_PLAY, "entity_id": _camera(hass), "ref": ref}
    await client.send_json_auto_id(request)
    if first_bytes:
        assert (await _next(client))["success"]
    else:
        await asyncio.wait_for(player.entered.wait(), 5)

    if how == "close":
        await client.close()
    else:
        await client.send_json_auto_id(
            {"type": "unsubscribe_events", "subscription": request["id"]}
        )
        assert (await _next(client))["success"]

    await asyncio.wait_for(player.cancelled.wait(), 5)
    await wait_until(lambda: _plays(entry).diagnostics()["active"] == 0)
    assert [s.stops for s in streams] == ([1] if first_bytes else [])
    assert _plays(entry).diagnostics()["viewer_left"] == 1
    if how == "unsubscribe":
        await _nothing_more(client)
    await _unload(hass, entry)


@pytest.mark.usefixtures("short_attach")
async def test_a_play_nobody_reads_ends_after_the_attach_timeout(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    streams: list[FakeStream],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Stream never reads the source: the play is cancelled, its Stream stopped,
    and the subscription ends ``failed``."""
    player = Player(hold=True).install(monkeypatch)
    entry = await _set_up(hass, seed_warm_cache, fake_station)
    client = await hass_ws_client(hass)
    assert (await _play(client, _camera(hass), await _ref(client, _camera(hass))))["success"]

    event = (await _next(client))["event"]

    assert (event["end"], event["code"]) == ("error", "failed")
    assert player.cancelled.is_set()
    await wait_until(lambda: streams[0].stops == 1)
    await _unload(hass, entry)


@pytest.mark.usefixtures("short_attach")
async def test_a_play_that_is_read_in_time_is_not_timed_out(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    hass_client_no_auth: ClientSessionGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    streams: list[FakeStream],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once the Stream reads the source, the attach timeout no longer ends the play."""
    player = Player(hold=True).install(monkeypatch)
    entry = await _set_up(hass, seed_warm_cache, fake_station)
    client = await hass_ws_client(hass)
    assert (await _play(client, _camera(hass), await _ref(client, _camera(hass))))["success"]
    http = await hass_client_no_auth()
    response = await http.get(_source_path(streams[0]))
    assert await response.content.readexactly(len(TS_CHUNK)) == TS_CHUNK
    # A second reader of a running play is refused.
    assert (await http.get(_source_path(streams[0]))).status == 410

    await asyncio.sleep(0.4)

    assert not player.cancelled.is_set()
    assert streams[0].stops == 0
    await _nothing_more(client)
    response.close()
    await _unload(hass, entry)


@pytest.mark.usefixtures("short_attach")
@pytest.mark.parametrize("start_gate", [asyncio.Event()])
async def test_a_source_read_while_the_stream_starts_is_not_timed_out(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    hass_client_no_auth: ClientSessionGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    streams: list[FakeStream],
    start_gate: asyncio.Event,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stream worker may read before ``start`` returns: no attach timeout then."""
    player = Player(hold=True).install(monkeypatch)
    entry = await _set_up(hass, seed_warm_cache, fake_station)
    client = await hass_ws_client(hass)
    ref = await _ref(client, _camera(hass))
    http = await hass_client_no_auth()
    await client.send_json_auto_id(
        {"type": station_recordings.WS_PLAY, "entity_id": _camera(hass), "ref": ref}
    )
    await wait_until(lambda: bool(streams) and streams[0].starting.is_set())
    response = await http.get(_source_path(streams[0]))
    assert await response.content.readexactly(len(TS_CHUNK)) == TS_CHUNK

    start_gate.set()
    assert (await _next(client))["success"]
    assert (await _next(client))["event"]["url"] == HLS_URL
    await asyncio.sleep(0.4)

    assert not player.cancelled.is_set()
    await _nothing_more(client)
    response.close()
    await _unload(hass, entry)


async def test_a_library_call_cancelled_by_home_assistant_ends_the_play(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    streams: list[FakeStream],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Home Assistant stopping cancels the entry's background tasks: the play ends
    ``failed`` and its Stream stops."""
    Player(hold=True).install(monkeypatch)
    entry = await _set_up(hass, seed_warm_cache, fake_station)
    client = await hass_ws_client(hass)
    assert (await _play(client, _camera(hass), await _ref(client, _camera(hass))))["success"]
    (library,) = [
        t for t in asyncio.all_tasks() if t.get_name() == "eufy_home_security recording play"
    ]

    library.cancel()
    event = (await _next(client))["event"]

    assert (event["end"], event["code"]) == ("error", "failed")
    await wait_until(lambda: streams[0].stops == 1)
    await _unload(hass, entry)


async def test_a_reader_that_goes_away_cancels_the_play(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    hass_client_no_auth: ClientSessionGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    streams: list[FakeStream],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Stream's worker drops the source mid-play: the library is cancelled, the
    Stream stopped, and the subscription ends ``failed``."""
    release = asyncio.Event()
    # Frames keep coming after the reader left, so the view notices at a write if the
    # server does not cancel it first.
    player = Player(chunks=[TS_CHUNK], release=release, then=[TS_CHUNK] * 50, hold=True)
    player.install(monkeypatch)
    entry = await _set_up(hass, seed_warm_cache, fake_station)
    client = await hass_ws_client(hass)
    assert (await _play(client, _camera(hass), await _ref(client, _camera(hass))))["success"]
    http = await hass_client_no_auth()
    response = await http.get(_source_path(streams[0]))
    assert await response.content.readexactly(len(TS_CHUNK)) == TS_CHUNK

    response.close()
    release.set()
    event = (await _next(client))["event"]

    assert player.cancelled.is_set()
    assert (event["end"], event["code"]) == ("error", "failed")
    await wait_until(lambda: streams[0].stops == 1)
    await _unload(hass, entry)


# ── unload, diagnostics ───────────────────────────────────────────────────────


@pytest.mark.parametrize("start_gate", [asyncio.Event()])
async def test_an_unload_while_the_stream_starts_answers_failed(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    streams: list[FakeStream],
    start_gate: asyncio.Event,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An end that comes while the Stream starts wins: the subscription gets the
    ``failed`` error, never a URL of a Stream already stopped."""
    player = Player(hold=True).install(monkeypatch)
    entry = await _set_up(hass, seed_warm_cache, fake_station)
    client = await hass_ws_client(hass)
    ref = await _ref(client, _camera(hass))
    await client.send_json_auto_id(
        {"type": station_recordings.WS_PLAY, "entity_id": _camera(hass), "ref": ref}
    )
    await wait_until(lambda: bool(streams) and streams[0].starting.is_set())

    unloading = hass.async_create_task(hass.config_entries.async_unload(entry.entry_id))
    await wait_until(player.cancelled.is_set)
    start_gate.set()
    await unloading

    reply = await _next(client)
    assert (reply["success"], reply["error"]["code"]) == (False, "failed")
    assert streams[0].stops == 1
    await _nothing_more(client)


async def test_an_unload_ends_every_play_and_stops_its_stream(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    streams: list[FakeStream],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A playing play and one in its grace (``complete`` sent) both end at once: the
    library cancelled, the playing one told ``failed``, each Stream stopped by the time
    the unload returns."""
    monkeypatch.setattr(recording_plays, "PLAY_END_GRACE", 60.0)
    player = Player(hold=True).install(monkeypatch)
    entry = await _set_up(hass, seed_warm_cache, fake_station)
    client = await hass_ws_client(hass)
    ref = await _ref(client, _camera(hass))
    playing = await _play(client, _camera(hass), ref)
    player.hold = False
    assert (await _play(client, _camera(hass), ref))["success"]
    assert (await _next(client))["event"]["end"] == "complete"
    await asyncio.sleep(0.1)
    assert [s.stops for s in streams] == [0, 0]
    for stream in streams:
        stream.stop_delay = 0.2

    assert await hass.config_entries.async_unload(entry.entry_id)

    # Every Stream has stopped by the time the unload returns.
    assert [s.stops for s in streams] == [1, 1]

    event = await _next(client)
    assert player.cancelled.is_set()
    assert event["id"] == playing["id"]
    assert (event["event"]["end"], event["event"]["code"]) == ("error", "failed")
    await _nothing_more(client)


def _worker_error(stream: FakeStream) -> None:
    """The line Home Assistant's stream worker logs when the play's source ends."""
    logging.getLogger(recording_plays.STREAM_LOGGER).error(
        "Error from stream worker: %s",
        f"Error demuxing stream (Input/output error, {stream.source})",
    )


def _logged(caplog: pytest.LogCaptureFixture) -> int:
    return sum(1 for r in caplog.records if r.name == recording_plays.STREAM_LOGGER)


async def test_the_worker_error_at_an_ended_plays_end_is_dropped_until_its_stream_stops(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    streams: list[FakeStream],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Once a play has ended complete, the stream worker's error naming it is dropped;
    the same line of a play still running, and another line naming the ended one, are
    logged. After the Stream stops the line is logged again, and the filter leaves with
    the last entry."""
    caplog.set_level(logging.ERROR, logger=recording_plays.STREAM_LOGGER)
    monkeypatch.setattr(recording_plays, "PLAY_END_GRACE", 60.0)
    release = asyncio.Event()
    player = Player(release=release).install(monkeypatch)
    entry = await _set_up(hass, seed_warm_cache, fake_station)
    client = await hass_ws_client(hass)
    ref = await _ref(client, _camera(hass))
    ended = await _play(client, _camera(hass), ref)
    release.set()
    assert (await _next(client))["event"]["end"] == "complete"
    player.release, player.hold = None, True
    assert (await _play(client, _camera(hass), ref))["success"]
    done, running = streams

    _worker_error(done)
    assert _logged(caplog) == 0
    _worker_error(running)
    logging.getLogger(recording_plays.STREAM_LOGGER).error("Stream %s stalled", done.source)
    assert _logged(caplog) == 2

    await client.send_json_auto_id({"type": "unsubscribe_events", "subscription": ended["id"]})
    assert (await _next(client))["success"]
    await wait_until(lambda: done.stops == 1)
    _worker_error(done)
    assert _logged(caplog) == 3

    await _unload(hass, entry)
    assert not logging.getLogger(recording_plays.STREAM_LOGGER).filters


def test_the_log_filter_stays_until_its_last_user_lets_go(hass: HomeAssistant) -> None:
    """Two entries share one filter; the first unload leaves it in place."""
    logger = logging.getLogger(recording_plays.STREAM_LOGGER)
    first = recording_plays._hold_filter(hass)
    second = recording_plays._hold_filter(hass)
    assert first is second
    assert logger.filters == [first]

    recording_plays._release_filter(hass, first)
    assert logger.filters == [first]
    recording_plays._release_filter(hass, second)
    assert logger.filters == []


async def test_diagnostics_count_the_plays_by_outcome(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    streams: list[FakeStream],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under ``station_recordings``: started, complete, viewer left, failed by code and
    the plays running; no ref or play id."""
    assert await async_setup_component(hass, "diagnostics", {})
    player = Player(hold=True).install(monkeypatch)
    entry = await _set_up(hass, seed_warm_cache, fake_station)
    client = await hass_ws_client(hass)
    ref = await _ref(client, _camera(hass))
    left = await _play(client, _camera(hass), ref)
    await client.send_json_auto_id({"type": "unsubscribe_events", "subscription": left["id"]})
    assert (await _next(client))["success"]
    assert (await _play(client, _camera(hass), ref))["success"]
    player.error, player.chunks = LiveStreamLimitError("spent", limit=1), []
    assert (await _play(client, _camera(hass), ref))["error"]["code"] == "busy"

    data = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    assert data["station_recordings"]["plays"] == {
        "started": 3,
        "complete": 0,
        "viewer_left": 1,
        "failed": {"busy": 1},
        "active": 1,
    }
    assert ref not in str(data)
    await _unload(hass, entry)
