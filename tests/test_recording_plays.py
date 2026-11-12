"""A station recording played straight to a viewer (``recording_plays.py``).

The websocket subscription ``eufy_home_security/recordings/play`` on Home Assistant's
test client, the library's ``FakeStation`` behind it, and the real remux and HLS view.
``Station.async_play_recording`` is scripted by :class:`Player`, which writes real
MPEG-TS made by ``recording_media.make_ts``; one test plays through the real library.
"""

from __future__ import annotations

import asyncio
import io
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

import av
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
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.components.diagnostics import get_diagnostics_for_config_entry
from pytest_homeassistant.typing import (
    ClientSessionGenerator,
    MockHAClientWebSocket,
    WebSocketGenerator,
)
from recording_media import Part, audio_only, make_ts, split_at

from custom_components.eufy_home_security import recording_hls, recording_plays, station_recordings
from custom_components.eufy_home_security.const import CAMERA_KEY

_DAY_FACTOR: Final = 100_000
_ROW_TIME: Final = "%Y-%m-%d %H:%M:%S"
_URL: Final = re.compile(r"/api/eufy_home_security/recording/([0-9a-f]{32})/playlist\.m3u8")
HLS_MIME: Final = "application/vnd.apple.mpegurl"
# The playback command the station gets for a play, and the download command.
RECORD_VIEW: Final = 1025
RECORDING_COMMANDS: Final = (1024, RECORD_VIEW)


def _clip(seconds: float = 5.0, **kwargs: Any) -> bytes:
    """A recording's TS: 320x240 H.264 at 15 fps, a keyframe every second, AAC."""
    return make_ts((Part(320, 240, seconds),), **kwargs)


def _halves(data: bytes, seconds: float) -> tuple[list[bytes], list[bytes]]:
    """``data`` cut at the first frame ``seconds`` in, each part in 18.8 kB writes."""
    cut = split_at(data, seconds)
    return _writes(data[:cut]), _writes(data[cut:])


def _writes(data: bytes, size: int = 18_800) -> list[bytes]:
    return [data[i : i + size] for i in range(0, len(data), size)]


@dataclass
class Player:
    """``Station.async_play_recording`` as the test scripts it.

    Writes ``chunks``; waits for ``release`` if given and writes ``then``; raises
    ``error``, or waits to be cancelled (``hold``), or returns a clip (``ended_early``
    as given).
    """

    chunks: list[bytes] = field(default_factory=lambda: _writes(_clip()))
    release: asyncio.Event | None = None
    then: list[bytes] = field(default_factory=list)
    error: Exception | None = None
    hold: bool = False
    ended_early: bool = False
    calls: list[tuple[int, bool, bool]] = field(default_factory=list)
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    returned: int = 0

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
                bytes_written=sum(len(c) for c in self.chunks + self.then),
                duration_s=1.0,
                width=320,
                height=240,
                resizes=0,
                ended_early=self.ended_early,
            )

        monkeypatch.setattr(Station, "async_play_recording", play)
        return self


@pytest.fixture
def short_keep(monkeypatch: pytest.MonkeyPatch) -> None:
    """An ended play is kept 0.3 s after its last request."""
    monkeypatch.setattr(recording_plays, "PLAY_KEEP_SECONDS", 0.3)


@pytest.fixture
def short_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """A playlist request waits 0.2 s for the first segment."""
    monkeypatch.setattr(recording_plays, "PLAYLIST_WAIT_SECONDS", 0.2)


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


async def _subscribe(client: MockHAClientWebSocket, entity_id: str, ref: str) -> int:
    """Send a play request; its message id."""
    request: dict[str, Any] = {
        "type": station_recordings.WS_PLAY,
        "entity_id": entity_id,
        "ref": ref,
    }
    await client.send_json_auto_id(request)
    msg_id: int = request["id"]
    return msg_id


async def _play(client: MockHAClientWebSocket, entity_id: str, ref: str) -> dict[str, Any]:
    """Subscribe a play; its answer: the error, or the empty result with the first
    event's payload under ``started`` (it follows the result at once)."""
    await _subscribe(client, entity_id, ref)
    return await _answer(client)


async def _answer(client: MockHAClientWebSocket) -> dict[str, Any]:
    reply = await _next(client)
    if reply["success"]:
        assert reply["result"] is None
        started = await _next(client)
        assert (started["id"], started["type"]) == (reply["id"], "event")
        reply["started"] = started["event"]
    return reply


async def _next(client: MockHAClientWebSocket) -> dict[str, Any]:
    reply: dict[str, Any] = await asyncio.wait_for(client.receive_json(), 10)
    return reply


async def _nothing_more(client: MockHAClientWebSocket) -> None:
    """The next message is the answer to a ping: no event came before it."""
    await client.send_json_auto_id({"type": "ping"})
    assert (await _next(client))["type"] == "pong"


def _base(url: str) -> str:
    return url.rsplit("/", 1)[0]


async def _get(http: Any, url: str) -> tuple[int, bytes, Any]:
    response = await http.get(url)
    return response.status, await response.read(), response.headers


async def _fetch_all(http: Any, url: str) -> tuple[str, dict[str, bytes]]:
    """The playlist and every file it names."""
    status, body, _ = await _get(http, url)
    assert status == 200
    text = body.decode()
    files: dict[str, bytes] = {}
    for name in re.findall(r"(init\d+\.mp4|seg\d+\.m4s)", text):
        status, files[name], _ = await _get(http, f"{_base(url)}/{name}")
        assert status == 200, name
    return text, files


def _frames(text: str, files: dict[str, bytes]) -> int:
    """Video frames the playlist's segments decode to, each with its own init."""
    init = b""
    frames = 0
    for line in text.splitlines():
        if match := re.match(r'#EXT-X-MAP:URI="(init\d+\.mp4)"', line):
            init = files[match.group(1)]
        elif line.startswith("seg"):
            container = av.open(io.BytesIO(init + files[line]), mode="r")
            frames += sum(1 for _ in container.decode(container.streams.video[0]))
            container.close()
    return frames


def _video_frames(data: bytes) -> int:
    container = av.open(io.BytesIO(data), mode="r", format="mpegts")
    count = sum(1 for p in container.demux(container.streams.video[0]) if p.size)
    container.close()
    return count


async def _unload(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


def _remux_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == recording_plays._REMUX_THREAD]


# ── a play from start to end ──────────────────────────────────────────────────


async def test_the_url_comes_with_the_first_segment_and_complete_after_the_last(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    hass_client_no_auth: ClientSessionGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under 2 s of media there is no segment and no answer; past the first cut the
    empty result and the playlist URL come; ``complete`` with the played length comes
    only when the remux has published the tail, and the playlist then ends with
    ``#EXT-X-ENDLIST`` and decodes to every frame, without any credential."""
    data = _clip(5.0)
    head, tail = _halves(data, 1.5)
    release = asyncio.Event()
    Player(chunks=head, release=release, then=tail).install(monkeypatch)
    # The remux holds its end until the test lets it go.
    remux_end = threading.Event()
    real_run = recording_hls.TsRemuxer.run

    def held_run(self: recording_hls.TsRemuxer) -> recording_hls.RemuxResult:
        result = real_run(self)
        remux_end.wait(10)
        return result

    monkeypatch.setattr(recording_hls.TsRemuxer, "run", held_run)
    entry = await _set_up(hass, seed_warm_cache, fake_station)
    client = await hass_ws_client(hass)
    msg_id = await _subscribe(client, _camera(hass), await _ref(client, _camera(hass)))
    await asyncio.sleep(0.3)
    await _nothing_more(client)

    release.set()
    reply = await _answer(client)

    assert reply["id"] == msg_id
    assert reply["started"]["mime_type"] == HLS_MIME
    url = reply["started"]["url"]
    assert _URL.fullmatch(url)
    http = await hass_client_no_auth()
    await asyncio.sleep(0.3)
    await _nothing_more(client)
    status, body, headers = await _get(http, url)
    assert (status, headers["Content-Type"], headers["Cache-Control"]) == (
        200,
        HLS_MIME,
        "no-cache",
    )
    assert "#EXT-X-ENDLIST" not in body.decode()

    remux_end.set()
    event = await _next(client)

    assert (event["id"], event["type"]) == (msg_id, "event")
    assert event["event"] == {"end": "complete", "duration": 5.0}
    text, files = await _fetch_all(http, url)
    assert text.splitlines()[-1] == "#EXT-X-ENDLIST"
    assert "#EXT-X-START:TIME-OFFSET=0,PRECISE=YES" in text
    assert re.findall(r"#EXTINF:([\d.]+),", text) == ["2.000", "2.000", "1.000"]
    assert _frames(text, files) == _video_frames(data)
    _, _, init_headers = await _get(http, f"{_base(url)}/init0.mp4")
    _, _, seg_headers = await _get(http, f"{_base(url)}/seg0.m4s")
    assert (init_headers["Content-Type"], seg_headers["Content-Type"]) == (
        "video/mp4",
        "video/iso.segment",
    )
    assert "immutable" in seg_headers["Cache-Control"]
    counts = _plays(entry).diagnostics()
    assert (counts["started"], counts["complete"], counts["active"]) == (1, 1, 1)
    assert _plays(entry)._threads == set()
    assert counts["bytes_held_peak"] == sum(len(b) for b in files.values())
    await _nothing_more(client)
    await _unload(hass, entry)
    assert _remux_threads() == []


async def test_a_play_asks_the_library_to_wait_for_a_session_and_keep_the_recordings_pace(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``wait`` and ``realtime`` on, for the listed row; a clip shorter than one
    segment still plays: one segment, then ``complete``."""
    player = Player(chunks=_writes(_clip(1.4))).install(monkeypatch)
    entry = await _set_up(hass, seed_warm_cache, fake_station)
    client = await hass_ws_client(hass)

    reply = await _play(client, _camera(hass), await _ref(client, _camera(hass)))

    assert reply["success"], reply
    assert player.calls == [(fake_station.rows[0]["record_id"], True, True)]
    assert (await _next(client))["event"] == {"end": "complete", "duration": 1.4}
    await _unload(hass, entry)


async def test_the_real_library_plays_through_the_fake_station(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The station gets the playback command, never the download one. The fake
    station's frames are no real video, so the remux refuses them: ``failed``."""
    entry = await _set_up(hass, seed_warm_cache, fake_station)
    client = await hass_ws_client(hass)

    reply = await _play(client, _camera(hass), await _ref(client, _camera(hass)))

    assert reply["error"] == {"code": "failed", "message": "The recording could not be played"}
    assert [o["cmd"] for o in fake_station.received if o.get("cmd") in RECORDING_COMMANDS] == [
        RECORD_VIEW
    ]
    await wait_until(lambda: _remux_threads() == [])
    counts = _plays(entry).diagnostics()
    assert (counts["started"], counts["failed"], counts["active"]) == (1, {"failed": 1}, 0)
    await _unload(hass, entry)


async def test_a_size_change_is_served_as_a_second_init_after_a_discontinuity(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    hass_client_no_auth: ClientSessionGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = make_ts((Part(320, 240, 3.5), Part(352, 288, 3.5)), codec="libx265")
    Player(chunks=_writes(data)).install(monkeypatch)
    entry = await _set_up(hass, seed_warm_cache, fake_station)
    client = await hass_ws_client(hass)
    reply = await _play(client, _camera(hass), await _ref(client, _camera(hass)))
    assert (await _next(client))["event"]["end"] == "complete"

    text, files = await _fetch_all(await hass_client_no_auth(), reply["started"]["url"])

    assert text.count("#EXT-X-DISCONTINUITY\n") == 1
    assert sorted(n for n in files if n.startswith("init")) == ["init0.mp4", "init1.mp4"]
    assert _frames(text, files) == _video_frames(data)
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
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    code: str,
) -> None:
    """The subscription is refused with the code and message a fetch gives for the same
    library error, no remux thread starts, and the failure is counted by its code."""
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
    assert _remux_threads() == []
    counts = _plays(entry).diagnostics()
    assert (counts["failed"], counts["active"]) == ({code: 1}, 0)
    await _nothing_more(client)
    await _unload(hass, entry)


async def test_a_busy_play_says_every_session_is_in_use(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A library call that returns without a byte is the station not playing it."""
    Player(chunks=[]).install(monkeypatch)
    entry = await _set_up(hass, seed_warm_cache, fake_station)
    client = await hass_ws_client(hass)

    reply = await _play(client, _camera(hass), await _ref(client, _camera(hass)))

    assert reply["error"] == {"code": "unavailable", "message": "The HomeBase did not play it"}
    assert _plays(entry).diagnostics()["active"] == 0
    # The refused subscription is gone: unsubscribing it finds nothing.
    await client.send_json_auto_id({"type": "unsubscribe_events", "subscription": reply["id"]})
    assert (await _next(client))["error"]["code"] == "not_found"
    await _unload(hass, entry)


@pytest.mark.parametrize(
    ("chunks", "error"),
    [
        ([b"\x47" + bytes(187)] * 40, None),
        ([b"not a transport stream" * 1000], None),
        ([b"\x47" + bytes(187)] * 40, DeviceTimeoutError("silent")),
    ],
    ids=["no-video", "no-ts", "no-video-then-library-error"],
)
async def test_a_play_with_nothing_to_show_is_refused(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    chunks: list[bytes],
    error: Exception | None,
) -> None:
    """Bytes without a video segment: ``failed`` (or the library's own error when it
    raised), never a URL; the remux thread is gone."""
    Player(chunks=chunks, error=error).install(monkeypatch)
    entry = await _set_up(hass, seed_warm_cache, fake_station)
    client = await hass_ws_client(hass)

    reply = await _play(client, _camera(hass), await _ref(client, _camera(hass)))

    assert reply["error"]["code"] == ("unavailable" if error else "failed")
    await wait_until(lambda: _remux_threads() == [])
    await _nothing_more(client)
    await _unload(hass, entry)


async def test_input_the_remux_cannot_read_stops_the_library(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The remux gives up (a TS without video) while the library still plays: the
    library is cancelled and the subscription refused ``failed``."""
    player = Player(chunks=[audio_only(_clip(2.0))], hold=True).install(monkeypatch)
    entry = await _set_up(hass, seed_warm_cache, fake_station)
    client = await hass_ws_client(hass)

    reply = await _play(client, _camera(hass), await _ref(client, _camera(hass)))

    assert reply["error"] == {"code": "failed", "message": "The recording could not be played"}
    await asyncio.wait_for(player.cancelled.wait(), 5)
    assert _plays(entry).diagnostics()["failed"] == {"failed": 1}
    await _unload(hass, entry)


async def test_a_play_while_the_station_is_away_is_unavailable(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
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
    refused = await _play(client, _camera(hass), running)
    assert refused["error"]["code"] == "not_settled"
    # The refused subscription is gone: unsubscribing it finds nothing.
    await client.send_json_auto_id({"type": "unsubscribe_events", "subscription": refused["id"]})
    assert (await _next(client))["error"]["code"] == "not_found"
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
    assert _plays(entry).diagnostics()["started"] == 0
    await _unload(hass, entry)


@pytest.mark.parametrize(
    ("error", "ended_early", "code"),
    [
        (DeviceTimeoutError("silent"), False, "unavailable"),
        (None, True, "codec_change"),
        (RuntimeError("unexpected"), False, "failed"),
    ],
    ids=["library-error", "codec-change", "unexpected-error"],
)
async def test_an_end_after_the_first_segment_is_one_error_event_after_the_tail(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    hass_client_no_auth: ClientSessionGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    error: Exception | None,
    ended_early: bool,
    code: str,
) -> None:
    """A library error after the URL, or a clip cut at a codec change, publishes what
    arrived (its tail included, with ``#EXT-X-ENDLIST``) and sends one ``error`` event;
    the play stays served."""
    data = _clip(5.0)
    head, _ = _halves(data, 3.5)
    release = asyncio.Event()
    Player(chunks=head, error=error, ended_early=ended_early, release=release).install(monkeypatch)
    entry = await _set_up(hass, seed_warm_cache, fake_station)
    client = await hass_ws_client(hass)
    reply = await _play(client, _camera(hass), await _ref(client, _camera(hass)))
    assert reply["success"]

    release.set()
    event = (await _next(client))["event"]

    assert (event["end"], event["code"]) == ("error", code)
    assert event["message"]
    text, _ = await _fetch_all(await hass_client_no_auth(), reply["started"]["url"])
    assert text.splitlines()[-1] == "#EXT-X-ENDLIST"
    # Cut before the frame at 3.533 s.
    assert re.findall(r"#EXTINF:([\d.]+),", text) == ["2.000", "1.533"]
    await _nothing_more(client)
    assert _plays(entry).diagnostics()["failed"] == {code: 1}
    await _unload(hass, entry)


async def test_a_remux_failure_after_the_url_stops_the_library_with_an_error_event(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The remux breaks off after its first segment while the library still plays: the
    library is cancelled (its session freed) and the viewer gets ``failed``."""
    player = Player(hold=True).install(monkeypatch)
    real_close = recording_hls.TsRemuxer._close_segment
    closes: list[int] = []

    def close_then_break(self: recording_hls.TsRemuxer, end_dts: int) -> None:
        closes.append(end_dts)
        if len(closes) > 1:
            raise recording_hls.RemuxError("broke off")
        real_close(self, end_dts)

    monkeypatch.setattr(recording_hls.TsRemuxer, "_close_segment", close_then_break)
    entry = await _set_up(hass, seed_warm_cache, fake_station)
    client = await hass_ws_client(hass)
    assert (await _play(client, _camera(hass), await _ref(client, _camera(hass))))["success"]

    event = (await _next(client))["event"]

    assert (event["end"], event["code"]) == ("error", "failed")
    await asyncio.wait_for(player.cancelled.wait(), 5)
    await _nothing_more(client)
    await _unload(hass, entry)


async def test_what_the_remux_posts_after_the_play_is_freed_is_dropped(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An init, a segment or the remux's end queued on the event loop before the viewer
    left neither answers the gone subscription nor holds bytes."""
    head, _ = _halves(_clip(5.0), 1.0)
    Player(chunks=head, hold=True).install(monkeypatch)
    entry = await _set_up(hass, seed_warm_cache, fake_station)
    client = await hass_ws_client(hass)
    msg_id = await _subscribe(client, _camera(hass), await _ref(client, _camera(hass)))
    await wait_until(lambda: bool(recording_plays._served(hass).plays))
    (play,) = recording_plays._served(hass).plays.values()
    await client.send_json_auto_id({"type": "unsubscribe_events", "subscription": msg_id})
    assert (await _next(client))["success"]
    plays = _plays(entry)
    await wait_until(lambda: plays._threads == set())

    plays._on_init(play, 0, b"init")
    plays._on_segment(play, recording_hls.Segment(0, 0, 2.0, b"segment"))
    plays._on_remux_done(play, None, recording_hls.RemuxError("aborted"))

    await _nothing_more(client)
    assert (play.playlist.held, play.playlist.segments, play.answered) == (0, 0, False)
    assert plays.diagnostics()["failed"] == {}
    await _unload(hass, entry)


# ── the viewer leaves; the ended play is kept ─────────────────────────────────


@pytest.mark.parametrize(
    ("answered", "how"),
    [(True, "close"), (True, "unsubscribe"), (False, "close"), (False, "unsubscribe")],
    ids=["playing-closed", "playing-unsubscribed", "starting-closed", "starting-unsubscribed"],
)
async def test_a_viewer_that_leaves_stops_the_play_and_frees_it_at_once(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    hass_client_no_auth: ClientSessionGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    answered: bool,
    how: str,
) -> None:
    """Unsubscribing or closing the websocket cancels the library and the remux, the
    token answers 404 at once, no event follows; counted as the viewer leaving."""
    head, _ = _halves(_clip(5.0), 3.5 if answered else 1.0)
    player = Player(chunks=head, hold=True).install(monkeypatch)
    entry = await _set_up(hass, seed_warm_cache, fake_station)
    client = await hass_ws_client(hass)
    msg_id = await _subscribe(client, _camera(hass), await _ref(client, _camera(hass)))
    url = None
    if answered:
        url = (await _answer(client))["started"]["url"]
    else:
        await asyncio.wait_for(player.entered.wait(), 5)
    (play,) = recording_plays._served(hass).plays.values()

    if how == "close":
        await client.close()
    else:
        await client.send_json_auto_id({"type": "unsubscribe_events", "subscription": msg_id})
        assert (await _next(client))["success"]

    await asyncio.wait_for(player.cancelled.wait(), 5)
    counts = _plays(entry).diagnostics()
    assert (counts["viewer_left"], counts["active"], counts["failed"]) == (1, 0, {})
    assert recording_plays._served(hass).plays == {}
    assert play.playlist.held == 0
    if url is not None:
        assert (await (await hass_client_no_auth()).get(url)).status == 404
    await wait_until(lambda: _remux_threads() == [])
    if how == "unsubscribe":
        await _nothing_more(client)
    await _unload(hass, entry)


@dataclass
class HeldLookup:
    """``StationRecordings.async_playable_row`` finds the row, then holds it until
    ``release`` is set."""

    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> HeldLookup:
        real = station_recordings.StationRecordings.async_playable_row

        async def held(
            recordings: station_recordings.StationRecordings, camera: Any, ref: str
        ) -> HistoryRecord:
            row = await real(recordings, camera, ref)
            self.entered.set()
            await self.release.wait()
            return row

        monkeypatch.setattr(station_recordings.StationRecordings, "async_playable_row", held)
        return self


async def _play_handlers_done() -> None:
    """Wait for every running ``recordings/play`` handler to return."""
    handlers = [t for t in asyncio.all_tasks() if t.get_name() == "websocket_api.async:_ws_play"]
    assert handlers
    await asyncio.wait_for(asyncio.gather(*handlers), 5)


@pytest.mark.parametrize("how", ["close", "unsubscribe", "stay"])
async def test_a_viewer_gone_while_the_recording_is_looked_up_starts_no_play(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    how: str,
) -> None:
    """Closing the websocket or unsubscribing while the row is looked up: the library is
    never asked and nothing stays served; a viewer that stays gets the play."""
    player = Player(chunks=[], hold=True).install(monkeypatch)
    lookup = HeldLookup().install(monkeypatch)
    entry = await _set_up(hass, seed_warm_cache, fake_station)
    client = await hass_ws_client(hass)
    msg_id = await _subscribe(client, _camera(hass), await _ref(client, _camera(hass)))
    await asyncio.wait_for(lookup.entered.wait(), 5)

    if how == "close":
        await client.close()
        await asyncio.sleep(0.1)
    elif how == "unsubscribe":
        await client.send_json_auto_id({"type": "unsubscribe_events", "subscription": msg_id})
        assert (await _next(client))["success"]
    lookup.release.set()
    await _play_handlers_done()

    if how == "stay":
        await asyncio.wait_for(player.entered.wait(), 5)
        assert _plays(entry).diagnostics()["active"] == 1
    else:
        assert not player.entered.is_set()
        assert _plays(entry).diagnostics() | {"bytes_held_peak": 0} == {
            "started": 0,
            "complete": 0,
            "viewer_left": 0,
            "failed": {},
            "active": 0,
            "bytes_held_peak": 0,
        }
        assert recording_plays._served(hass).plays == {}
    if how == "unsubscribe":
        await _nothing_more(client)
    await _unload(hass, entry)


async def test_an_unload_while_the_recording_is_looked_up_refuses_the_play(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The entry unloads while the row is looked up: the play is refused
    ``unavailable`` and the library is never asked."""
    player = Player(chunks=[], hold=True).install(monkeypatch)
    lookup = HeldLookup().install(monkeypatch)
    entry = await _set_up(hass, seed_warm_cache, fake_station)
    client = await hass_ws_client(hass)
    msg_id = await _subscribe(client, _camera(hass), await _ref(client, _camera(hass)))
    await asyncio.wait_for(lookup.entered.wait(), 5)
    plays = _plays(entry)

    await _unload(hass, entry)
    lookup.release.set()
    reply = await _next(client)

    assert (reply["id"], reply["error"]["code"]) == (msg_id, "unavailable")
    assert not player.entered.is_set()
    assert plays.diagnostics()["active"] == 0
    assert recording_plays._served(hass).plays == {}


@pytest.mark.usefixtures("short_keep")
async def test_an_ended_play_is_kept_until_nobody_asked_for_the_keep_time(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    hass_client_no_auth: ClientSessionGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every request restarts the keep time; idle for ``PLAY_KEEP_SECONDS`` the play is
    freed (404) with no further event."""
    Player().install(monkeypatch)
    entry = await _set_up(hass, seed_warm_cache, fake_station)
    client = await hass_ws_client(hass)
    reply = await _play(client, _camera(hass), await _ref(client, _camera(hass)))
    assert (await _next(client))["event"]["end"] == "complete"
    http = await hass_client_no_auth()
    loop = asyncio.get_running_loop()
    url = reply["started"]["url"]

    for _ in range(3):
        await asyncio.sleep(0.2)
        assert (await http.get(url)).status == 200
    asked = loop.time()
    await wait_until(lambda: _plays(entry).diagnostics()["active"] == 0)

    # Freed when the keep time since the last request is up, not a keep time later.
    assert 0.3 <= loop.time() - asked < 0.45
    assert (await http.get(url)).status == 404
    await _nothing_more(client)
    await _unload(hass, entry)


async def test_unsubscribing_after_the_end_frees_the_kept_play_at_once(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    hass_client_no_auth: ClientSessionGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Player().install(monkeypatch)
    entry = await _set_up(hass, seed_warm_cache, fake_station)
    client = await hass_ws_client(hass)
    reply = await _play(client, _camera(hass), await _ref(client, _camera(hass)))
    assert (await _next(client))["event"]["end"] == "complete"
    http = await hass_client_no_auth()
    assert (await http.get(reply["started"]["url"])).status == 200

    await client.send_json_auto_id({"type": "unsubscribe_events", "subscription": reply["id"]})

    assert (await _next(client))["success"]
    assert (await http.get(reply["started"]["url"])).status == 404
    counts = _plays(entry).diagnostics()
    assert (counts["complete"], counts["viewer_left"], counts["active"]) == (1, 0, 0)
    await _unload(hass, entry)


# ── the HLS view ──────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("short_wait")
async def test_a_playlist_asked_before_the_first_segment_waits_for_it(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    hass_client_no_auth: ClientSessionGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Asked early (a native player given the URL ahead), the playlist answers when the
    first segment comes, or 404 after ``PLAYLIST_WAIT_SECONDS``; a play freed while a
    request waits answers it 404 at once."""
    head, tail = _halves(_clip(5.0), 1.0)
    release = asyncio.Event()
    Player(chunks=head, release=release, then=tail, hold=True).install(monkeypatch)
    entry = await _set_up(hass, seed_warm_cache, fake_station)
    client = await hass_ws_client(hass)
    msg_id = await _subscribe(client, _camera(hass), await _ref(client, _camera(hass)))
    await wait_until(lambda: _plays(entry).diagnostics()["active"] == 1)
    (token,) = recording_plays._served(hass).plays
    url = recording_plays.RECORDING_URL.format(token=token, name="playlist.m3u8")
    http = await hass_client_no_auth()
    loop = asyncio.get_running_loop()

    asked = loop.time()
    assert (await http.get(url)).status == 404
    assert loop.time() - asked >= 0.2

    monkeypatch.setattr(recording_plays, "PLAYLIST_WAIT_SECONDS", 10.0)
    waiting = hass.async_create_task(http.get(url))
    await asyncio.sleep(0.1)
    release.set()
    response = await asyncio.wait_for(waiting, 5)
    assert response.status == 200
    assert "seg0.m4s" in await response.text()
    assert (await _answer(client))["started"]["url"] == url

    await client.send_json_auto_id({"type": "unsubscribe_events", "subscription": msg_id})
    assert (await _next(client))["success"]
    await _unload(hass, entry)


async def test_a_wait_ends_with_404_when_the_play_is_freed(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    hass_client_no_auth: ClientSessionGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    head, _ = _halves(_clip(5.0), 1.0)
    Player(chunks=head, hold=True).install(monkeypatch)
    entry = await _set_up(hass, seed_warm_cache, fake_station)
    client = await hass_ws_client(hass)
    msg_id = await _subscribe(client, _camera(hass), await _ref(client, _camera(hass)))
    await wait_until(lambda: _plays(entry).diagnostics()["active"] == 1)
    (token,) = recording_plays._served(hass).plays
    http = await hass_client_no_auth()
    waiting = hass.async_create_task(
        http.get(recording_plays.RECORDING_URL.format(token=token, name="playlist.m3u8"))
    )
    await asyncio.sleep(0.1)
    loop = asyncio.get_running_loop()
    left = loop.time()

    await client.send_json_auto_id({"type": "unsubscribe_events", "subscription": msg_id})

    assert (await asyncio.wait_for(waiting, 5)).status == 404
    assert loop.time() - left < 1.0
    await _unload(hass, entry)


async def test_the_view_answers_404_for_unknown_tokens_and_files(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    hass_client_no_auth: ClientSessionGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Player().install(monkeypatch)
    entry = await _set_up(hass, seed_warm_cache, fake_station)
    client = await hass_ws_client(hass)
    reply = await _play(client, _camera(hass), await _ref(client, _camera(hass)))
    assert (await _next(client))["event"]["end"] == "complete"
    http = await hass_client_no_auth()
    base = _base(reply["started"]["url"])

    statuses = [
        (await http.get(f"{base}/{name}")).status
        for name in ("init0.mp4", "seg2.m4s", "init1.mp4", "seg3.m4s", "seg0.mp4", "x.m3u8")
    ]
    unknown = (await http.get(f"/api/eufy_home_security/recording/{'0' * 32}/seg0.m4s")).status

    assert statuses == [200, 200, 404, 404, 404, 404]
    assert unknown == 404
    await _unload(hass, entry)


async def test_past_the_memory_cap_the_playlist_slides(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    hass_client_no_auth: ClientSessionGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A play holds at most ``PLAY_MAX_BYTES``: the oldest segments leave the
    playlist, the newest stays, and the peak is in the diagnostics."""
    monkeypatch.setattr(recording_plays, "PLAY_MAX_BYTES", 40_000)
    Player(chunks=_writes(_clip(9.0))).install(monkeypatch)
    entry = await _set_up(hass, seed_warm_cache, fake_station)
    client = await hass_ws_client(hass)
    reply = await _play(client, _camera(hass), await _ref(client, _camera(hass)))
    assert (await _next(client))["event"] == {"end": "complete", "duration": 9.0}

    text, files = await _fetch_all(await hass_client_no_auth(), reply["started"]["url"])

    assert "#EXT-X-PLAYLIST-TYPE:EVENT" not in text
    assert "#EXT-X-MEDIA-SEQUENCE:0" not in text
    assert "seg4.m4s" in files
    held = sum(len(b) for b in files.values())
    assert held <= 40_000
    assert _plays(entry).diagnostics()["bytes_held_peak"] > 40_000
    await _unload(hass, entry)


# ── unload, Home Assistant stopping, diagnostics ──────────────────────────────


async def test_an_unload_ends_every_play_and_waits_for_the_remux(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    hass_client_no_auth: ClientSessionGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A playing play is told ``failed``, one still starting is refused ``failed``, a
    kept one gets nothing more; the library is cancelled, every token answers 404 and
    no remux thread is left when the unload returns."""
    head, _ = _halves(_clip(5.0), 3.5)
    player = Player(chunks=head, hold=True).install(monkeypatch)
    entry = await _set_up(hass, seed_warm_cache, fake_station)
    client = await hass_ws_client(hass)
    ref = await _ref(client, _camera(hass))
    playing = await _play(client, _camera(hass), ref)
    player.hold = False
    player.chunks = _writes(_clip(1.4))
    kept = await _play(client, _camera(hass), ref)
    assert (await _next(client))["event"]["end"] == "complete"
    player.chunks, player.hold = [], True
    starting = await _subscribe(client, _camera(hass), ref)
    await wait_until(lambda: _plays(entry).diagnostics()["active"] == 3)

    assert await hass.config_entries.async_unload(entry.entry_id)

    assert _remux_threads() == []
    assert player.cancelled.is_set()
    answers = {}
    for _ in range(2):
        message = await _next(client)
        answers[message["id"]] = message
    assert answers[playing["id"]]["event"]["code"] == "failed"
    assert answers[starting]["error"]["code"] == "failed"
    await _nothing_more(client)
    http = await hass_client_no_auth()
    for reply in (playing, kept):
        assert (await http.get(reply["started"]["url"])).status == 404


async def test_an_unload_waits_for_the_library_and_a_slow_remux_thread(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The unload returns only once every library call is done and every remux thread,
    even one slow to stop, has ended."""
    real_run = recording_hls.TsRemuxer.run

    def slow_run(self: recording_hls.TsRemuxer) -> recording_hls.RemuxResult:
        result = real_run(self)
        time.sleep(0.3)
        return result

    monkeypatch.setattr(recording_hls.TsRemuxer, "run", slow_run)
    head, _ = _halves(_clip(5.0), 3.5)
    player = Player(chunks=head, hold=True).install(monkeypatch)
    entry = await _set_up(hass, seed_warm_cache, fake_station)
    client = await hass_ws_client(hass)
    ref = await _ref(client, _camera(hass))
    assert (await _play(client, _camera(hass), ref))["success"]
    player.chunks = []
    await _subscribe(client, _camera(hass), ref)
    await wait_until(lambda: _plays(entry).diagnostics()["active"] == 2)
    libraries = [
        t for t in asyncio.all_tasks() if t.get_name() == "eufy_home_security recording play"
    ]

    assert await hass.config_entries.async_unload(entry.entry_id)

    assert len(libraries) == 2
    assert all(t.done() for t in libraries)
    assert _remux_threads() == []


async def test_stopping_the_plays_returns_once_the_library_calls_are_done(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A play still waiting for its first bytes has no remux thread to wait for; its
    library call is still over when ``async_stop`` returns, before the client closes."""
    player = Player(chunks=[], hold=True).install(monkeypatch)
    entry = await _set_up(hass, seed_warm_cache, fake_station)
    client = await hass_ws_client(hass)
    await _subscribe(client, _camera(hass), await _ref(client, _camera(hass)))
    await asyncio.wait_for(player.entered.wait(), 5)
    (library,) = [
        t for t in asyncio.all_tasks() if t.get_name() == "eufy_home_security recording play"
    ]

    await _plays(entry).async_stop()

    assert library.done()
    assert player.cancelled.is_set()
    await _unload(hass, entry)


def test_a_second_entry_registers_no_second_view(hass: HomeAssistant) -> None:
    """The view is registered once per Home Assistant instance."""
    entry = MockConfigEntry(domain="eufy_home_security")
    registered: list[object] = []

    class Http:
        def register_view(self, view: object) -> None:
            registered.append(view)

    hass.http = Http()  # type: ignore[assignment]
    recording_plays.RecordingPlays(hass, entry)
    recording_plays.RecordingPlays(hass, entry)

    assert len(registered) == 1
    assert isinstance(registered[0], recording_plays.RecordingHlsView)


async def test_a_library_call_cancelled_by_home_assistant_ends_the_play(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Home Assistant stopping cancels the entry's background tasks: the play ends
    ``failed``."""
    head, _ = _halves(_clip(5.0), 3.5)
    Player(chunks=head, hold=True).install(monkeypatch)
    entry = await _set_up(hass, seed_warm_cache, fake_station)
    client = await hass_ws_client(hass)
    assert (await _play(client, _camera(hass), await _ref(client, _camera(hass))))["success"]
    (library,) = [
        t for t in asyncio.all_tasks() if t.get_name() == "eufy_home_security recording play"
    ]

    library.cancel()
    event = (await _next(client))["event"]

    assert (event["end"], event["code"]) == ("error", "failed")
    await _unload(hass, entry)


async def test_diagnostics_count_the_plays_by_outcome(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under ``station_recordings``: started, complete, viewer left, failed by code, the
    plays served and the most bytes one held; no ref or token."""
    assert await async_setup_component(hass, "diagnostics", {})
    head, _ = _halves(_clip(5.0), 3.5)
    player = Player(chunks=head, hold=True).install(monkeypatch)
    entry = await _set_up(hass, seed_warm_cache, fake_station)
    client = await hass_ws_client(hass)
    ref = await _ref(client, _camera(hass))
    left = await _play(client, _camera(hass), ref)
    await client.send_json_auto_id({"type": "unsubscribe_events", "subscription": left["id"]})
    assert (await _next(client))["success"]
    running = await _play(client, _camera(hass), ref)
    player.hold, player.chunks = False, _writes(_clip(1.4))
    assert (await _play(client, _camera(hass), ref))["success"]
    assert (await _next(client))["event"]["end"] == "complete"
    player.error, player.chunks = LiveStreamLimitError("spent", limit=1), []
    assert (await _play(client, _camera(hass), ref))["error"]["code"] == "busy"

    data = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    plays = data["station_recordings"]["plays"]
    assert plays == {
        "started": 4,
        "complete": 1,
        "viewer_left": 1,
        "failed": {"busy": 1},
        "active": 2,
        "bytes_held_peak": plays["bytes_held_peak"],
    }
    assert plays["bytes_held_peak"] > 0
    assert ref not in str(data)
    assert _URL.fullmatch(running["started"]["url"])
    token = _URL.fullmatch(running["started"]["url"]).group(1)  # type: ignore[union-attr]
    assert token not in str(data)
    await _unload(hass, entry)
