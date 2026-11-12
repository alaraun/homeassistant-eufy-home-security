"""The TS to fMP4 HLS repackaging of a recording play (``recording_hls.py``).

Real PyAV on TS made by ``recording_media.make_ts`` (H.264 or HEVC, silent AAC); each
segment is checked as a player reads it: with its own init, by its boxes.
"""

from __future__ import annotations

import io
import logging
import struct
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

import av
import pytest
from recording_media import Part, audio_only, make_ts

from custom_components.eufy_home_security import recording_hls
from custom_components.eufy_home_security.recording_hls import (
    HlsPlaylist,
    RemuxError,
    Segment,
    TsRemuxer,
)

FPS = 15
TICK = Fraction(1, 90000)


@dataclass
class Published:
    """What a remux published, in order."""

    inits: dict[int, bytes] = field(default_factory=dict)
    segments: list[Segment] = field(default_factory=list)
    order: list[str] = field(default_factory=list)

    def init(self, index: int, data: bytes) -> None:
        self.inits[index] = data
        self.order.append(f"init{index}")

    def segment(self, segment: Segment) -> None:
        self.segments.append(segment)
        self.order.append(f"seg{segment.sequence}")


def _remux(data: bytes, chunk: int = 7_520) -> tuple[Published, recording_hls.RemuxResult]:
    """Feed ``data`` in chunks, end the input, run the remux in this thread."""
    published = Published()
    remuxer = TsRemuxer(published.init, published.segment)
    for start in range(0, len(data), chunk):
        remuxer.feed(data[start : start + chunk])
    remuxer.finish()
    return published, remuxer.run()


def _boxes(data: bytes, start: int = 0, end: int | None = None) -> Iterator[tuple[bytes, int, int]]:
    index, end = start, len(data) if end is None else end
    while index + 8 <= end:
        size, kind = struct.unpack(">I4s", data[index : index + 8])
        yield kind, index, size
        index += size


def _tkhd_size(init: bytes) -> tuple[int, int]:
    """The first track's width and height from its ``tkhd`` box."""
    at = init.index(b"tkhd") - 4
    size = int.from_bytes(init[at : at + 4], "big")
    width, height = struct.unpack(">II", init[at + size - 8 : at + size])
    return width >> 16, height >> 16


@dataclass(frozen=True)
class _Decoded:
    sizes: set[tuple[int, int]]
    frames: int
    first_pts: Fraction
    audio_frames: int
    first_audio_pts: Fraction | None
    audio_config: bytes | None


def _decode(init: bytes, segment: Segment) -> _Decoded:
    """Decode a segment's video and audio with ``init`` as a player does."""
    container = av.open(io.BytesIO(init + segment.data), mode="r")
    video = container.streams.video[0]
    sizes: set[tuple[int, int]] = set()
    frames = 0
    first: Fraction | None = None
    for frame in container.decode(video):
        sizes.add((frame.width, frame.height))
        frames += 1
        if first is None and frame.pts is not None:
            first = frame.pts * video.time_base
    container.close()
    container = av.open(io.BytesIO(init + segment.data), mode="r")
    audio_frames = 0
    first_audio: Fraction | None = None
    config: bytes | None = None
    if container.streams.audio:
        sound = container.streams.audio[0]
        config = sound.codec_context.extradata
        for audio_frame in container.decode(sound):
            audio_frames += 1
            if first_audio is None and audio_frame.pts is not None:
                first_audio = audio_frame.pts * sound.time_base
    container.close()
    assert first is not None
    return _Decoded(sizes, frames, first, audio_frames, first_audio, config)


def _source_counts(data: bytes) -> tuple[int, int]:
    """Video packets of a TS, and its audio packets from the first video frame on
    (the encoder's priming frame before it has no place on the timeline)."""
    container = av.open(io.BytesIO(data), mode="r", format="mpegts")
    packets = [(p.stream.type, p.dts) for p in container.demux() if p.size and p.dts is not None]
    container.close()
    video = [dts for kind, dts in packets if kind == "video"]
    audio = [dts for kind, dts in packets if kind == "audio" and dts >= video[0]]
    return len(video), len(audio)


# ── segment cuts ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("gop", "seconds", "durations"),
    [
        # A keyframe every second: the one at 2.0 s is the first past 1.9 s and cuts.
        (15, 5.0, [2.0, 2.0, 1.0]),
        # Every 22 frames (1.467 s): the first at or past 1.9 s is at 2.933 s.
        (22, 5.0, [2.933, 2.067]),
        # Every 29 frames (1.933 s, a camera's interval just under 2 s): every keyframe cuts.
        (29, 5.0, [1.933, 1.933, 1.133]),
        # Shorter than one segment: the whole recording is the one segment.
        (15, 1.4, [1.4]),
    ],
)
def test_a_segment_is_cut_at_the_first_keyframe_1_9_seconds_in_and_the_tail_is_published(
    gop: int, seconds: float, durations: list[float]
) -> None:
    """Every frame of the TS is in some segment, the last ones included."""
    data = make_ts((Part(320, 240, seconds),), gop=gop)
    published, result = _remux(data)

    assert [round(s.duration, 3) for s in published.segments] == durations
    assert [s.sequence for s in published.segments] == list(range(len(durations)))
    assert result.segments == len(durations)
    video, _ = _source_counts(data)
    assert result.video_packets == video
    decoded = [_decode(published.inits[0], s) for s in published.segments]
    assert sum(d.frames for d in decoded) == video
    assert result.media_seconds == pytest.approx(seconds, abs=1e-6)


def test_every_segment_has_its_init_first_and_one_timeline_from_zero() -> None:
    """The init is published before the first segment; each segment's first frame is
    at the sum of the durations before it, the first at 0, its audio next to it; every
    audio frame decodes with the init's AudioSpecificConfig."""
    data = make_ts((Part(320, 240, 7.0),), gop=30)
    published, _ = _remux(data)

    assert published.order[:2] == ["init0", "seg0"]
    assert len(published.inits) == 1
    start = 0.0
    audio = 0
    for segment in published.segments:
        decoded = _decode(published.inits[0], segment)
        assert float(decoded.first_pts) == pytest.approx(start, abs=0.001)
        assert decoded.first_audio_pts is not None
        # Audio goes with the segment open when it arrives: up to two frames late.
        assert float(decoded.first_audio_pts) == pytest.approx(start, abs=0.13)
        assert decoded.audio_config == bytes.fromhex("1408")
        start += segment.duration
        audio += decoded.audio_frames
    assert audio == _source_counts(data)[1]


def test_frames_before_the_first_keyframe_are_left_out() -> None:
    """A TS that starts mid-picture-group starts its first segment at its keyframe."""
    data = make_ts((Part(320, 240, 3.0),), gop=15)
    container = av.open(io.BytesIO(data), mode="r", format="mpegts")
    buffer = io.BytesIO()
    out = av.open(buffer, mode="w", format="mpegts")
    streams = {s.index: out.add_stream_from_template(s) for s in container.streams}
    skipped = 0
    for packet in container.demux():
        if not packet.size:
            continue
        if packet.stream.type == "video" and skipped < 20:
            skipped += 1
            continue
        packet.stream = streams[packet.stream.index]
        out.mux(packet)
    out.close()
    container.close()

    published, result = _remux(buffer.getvalue())

    # 45 frames, 20 cut away, the next keyframe is frame 30.
    assert result.video_packets == 15
    assert [round(s.duration, 3) for s in published.segments] == [1.0]


class _EditedInput:
    """An input container whose video packets pass through ``edit(index, packet)``."""

    def __init__(self, container: Any, edit: Callable[[int, Any], None]) -> None:
        self._container = container
        self._edit = edit
        self.streams = container.streams

    def demux(self, *args: Any) -> Iterator[Any]:
        index = 0
        for packet in self._container.demux(*args):
            if packet.size and packet.stream.type == "video":
                self._edit(index, packet)
                index += 1
            yield packet

    def close(self) -> None:
        self._container.close()


def _library_style(index: int, packet: Any) -> None:
    """DTS one frame behind PTS, as the library muxes it."""
    packet.pts += 6000


def _lj_style(index: int, packet: Any) -> None:
    """The streaming demux of a first keyframe muxed without DTS: its DTS read as its
    PTS, the next frame's missing."""
    _library_style(index, packet)
    if index == 0:
        packet.dts = packet.pts
    elif index == 1:
        packet.dts = None


def _first_without_dts(index: int, packet: Any) -> None:
    _library_style(index, packet)
    if index == 0:
        packet.dts = None


def _repeated_dts(index: int, packet: Any) -> None:
    if index == 5:
        packet.dts -= 6000


def _pts_before_dts(index: int, packet: Any) -> None:
    if index == 5:
        packet.pts -= 1


def _leading_picture(index: int, packet: Any) -> None:
    """A frame decoded after the first keyframe but shown before it."""
    _library_style(index, packet)
    if index == 1:
        packet.pts -= 9000
        packet.dts = packet.pts - 2000


@pytest.mark.parametrize(
    ("edit", "kept", "first_pts"),
    [
        (_library_style, 45, 0.0),
        (_lj_style, 45, 0.0),
        (_first_without_dts, 45, 0.0),
        (_repeated_dts, 44, 0.0),
        (_pts_before_dts, 44, 0.0),
        (_leading_picture, 44, 0.0),
    ],
)
def test_a_frame_without_a_usable_dts_is_mended_or_left_out(
    monkeypatch: pytest.MonkeyPatch,
    edit: Callable[[int, Any], None],
    kept: int,
    first_pts: float,
) -> None:
    """The first keyframe is shown at 0 whatever its DTS; a missing DTS is put one frame
    before the PTS and after the previous DTS; a frame whose DTS does not advance or
    lies past its PTS is dropped, not fatal."""
    real_open = av.open

    def edited_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        container = real_open(file, *args, **kwargs)
        if isinstance(file, recording_hls._QueueReader):
            return _EditedInput(container, edit)
        return container

    monkeypatch.setattr(av, "open", edited_open)
    data = make_ts((Part(320, 240, 3.0),), gop=30)
    published, result = _remux(data)
    monkeypatch.undo()

    assert result.video_packets == kept
    decoded = [_decode(published.inits[0], s) for s in published.segments]
    assert sum(d.frames for d in decoded) == kept
    assert float(decoded[0].first_pts) == pytest.approx(first_pts, abs=0.001)
    # Segments span the shown pictures: first keyframe to the end of the last frame.
    assert sum(s.duration for s in published.segments) == pytest.approx(3.0, abs=0.001)


def test_with_b_frames_the_last_segment_ends_with_the_last_picture_shown() -> None:
    """The last frame decoded is not the last one shown; the tail still ends there."""
    data = make_ts((Part(320, 240, 3.0),), gop=15, bframes=2)
    published, result = _remux(data)

    assert sum(s.duration for s in published.segments) == pytest.approx(3.0, abs=0.001)
    assert result.video_packets == 45
    assert sum(_decode(published.inits[0], s).frames for s in published.segments) == 45


# ── codec tags and picture-format changes ─────────────────────────────────────


@pytest.mark.parametrize(("codec", "tag"), [("libx264", b"avc1"), ("libx265", b"hvc1")])
def test_hevc_is_tagged_hvc1_and_h264_keeps_avc1(codec: str, tag: bytes) -> None:
    published, _ = _remux(make_ts((Part(320, 240, 2.5),), codec=codec))

    assert tag in published.inits[0]
    assert (b"hev1" if tag == b"hvc1" else b"hvc1") not in published.inits[0]


def _pasp(init: bytes) -> tuple[int, int] | None:
    """The pixel aspect an init declares, None without a ``pasp`` box."""
    at = init.find(b"pasp")
    if at < 0:
        return None
    spacing: tuple[int, int] = struct.unpack(">II", init[at + 4 : at + 12])
    return spacing


@pytest.mark.parametrize("codec", ["libx264", "libx265"])
def test_a_picture_size_change_starts_a_new_init_at_its_keyframe(codec: str) -> None:
    """A change 1.467 s into a segment cuts it there; the new init has the new size in
    its ``tkhd`` and its own pixel aspect, and decodes every later frame at that size,
    still on one timeline."""
    data = make_ts((Part(320, 240, 3.5, sar=Fraction(4, 3)), Part(352, 288, 3.5)), codec=codec)
    published, result = _remux(data)

    assert published.order == ["init0", "seg0", "seg1", "init1", "seg2", "seg3"]
    assert [s.init for s in published.segments] == [0, 0, 1, 1]
    assert [round(s.duration, 3) for s in published.segments] == [2.0, 1.467, 2.0, 1.467]
    assert _tkhd_size(published.inits[0]) == (320, 240)
    assert _tkhd_size(published.inits[1]) == (352, 288)
    assert _pasp(published.inits[0]) == (4, 3)
    assert _pasp(published.inits[1]) is None
    assert result.inits == 2
    start = 0.0
    frames = 0
    for segment in published.segments:
        decoded = _decode(published.inits[segment.init], segment)
        assert decoded.sizes == {(320, 240) if segment.init == 0 else (352, 288)}
        assert float(decoded.first_pts) == pytest.approx(start, abs=0.001)
        start += segment.duration
        frames += decoded.frames
    assert frames == _source_counts(data)[0]


def test_sets_that_do_not_decode_are_kept_out_and_tried_again_at_the_next_keyframe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first decode of the new sets fails: no init for them yet; the next keyframe
    repeats them, decodes, and starts the new init there."""
    real = recording_hls._decoded_format
    calls: list[int] = []

    def failing_once(codec: str, extradata: bytes, keyframe: bytes) -> recording_hls._Format:
        calls.append(len(keyframe))
        if len(calls) == 1:
            raise ValueError("keyframe did not decode")
        return real(codec, extradata, keyframe)

    monkeypatch.setattr(recording_hls, "_decoded_format", failing_once)
    data = make_ts((Part(320, 240, 3.5), Part(352, 288, 3.5)))
    published, _ = _remux(data)

    assert len(calls) == 2
    # The failed change at 3.467 s cuts nothing; the retry at 4.467 s cuts and is init 1.
    assert [s.init for s in published.segments] == [0, 0, 1, 1]
    assert [round(s.duration, 3) for s in published.segments] == [2.0, 2.467, 2.0, 0.467]
    assert _tkhd_size(published.inits[1]) == (352, 288)


def test_keyframes_that_repeat_the_same_sets_start_no_new_init(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every keyframe carries the sets in-band; equal sets are no change (no decode)."""
    calls: list[str] = []

    def decoded(*args: object) -> None:
        calls.append("decode")

    monkeypatch.setattr(recording_hls, "_decoded_format", decoded)
    published, result = _remux(make_ts((Part(320, 240, 6.0),), codec="libx265"))

    assert result.inits == 1
    assert len(published.segments) == 3
    assert calls == []


# ── unreadable input and aborts ───────────────────────────────────────────────


def test_a_ts_without_video_is_a_remux_error() -> None:
    with pytest.raises(RemuxError, match="no video"):
        _remux(audio_only(make_ts((Part(320, 240, 2.0),))))


@pytest.mark.parametrize("data", [b"", b"not a transport stream" * 400])
def test_input_that_is_no_ts_is_a_remux_error(data: bytes) -> None:
    with pytest.raises(RemuxError):
        _remux(data)


def test_an_abort_before_any_input_returns_without_publishing() -> None:
    published = Published()
    remuxer = TsRemuxer(published.init, published.segment)
    remuxer.abort()

    result = remuxer.run()

    assert published.order == []
    assert result.segments == 0


def test_an_abort_while_a_segment_is_published_stops_it() -> None:
    published = Published()

    def init_then_abort(index: int, data: bytes) -> None:
        published.init(index, data)
        remuxer.abort()

    remuxer = TsRemuxer(init_then_abort, published.segment)
    remuxer.feed(make_ts((Part(320, 240, 1.4),)))
    remuxer.finish()

    remuxer.run()

    assert published.order == ["init0"]


def test_video_without_a_keyframe_is_a_remux_error() -> None:
    data = make_ts((Part(320, 240, 2.0),))
    container = av.open(io.BytesIO(data), mode="r", format="mpegts")
    buffer = io.BytesIO()
    out = av.open(buffer, mode="w", format="mpegts")
    streams = {s.index: out.add_stream_from_template(s) for s in container.streams}
    for packet in container.demux():
        if packet.size and not (packet.stream.type == "video" and packet.is_keyframe):
            packet.stream = streams[packet.stream.index]
            out.mux(packet)
    out.close()
    container.close()

    with pytest.raises(RemuxError, match="no video keyframe"):
        _remux(buffer.getvalue())


def test_the_input_reader_returns_at_most_what_is_asked() -> None:
    reader = recording_hls._QueueReader()
    reader.chunks.put(b"0123456789")
    reader.chunks.put(b"ab")
    reader.chunks.put(None)

    assert [reader.read(4), reader.read(4), reader.read(4), reader.read(-1), reader.read(4)] == [
        b"0123",
        b"4567",
        b"89",
        b"ab",
        b"",
    ]


def test_an_abort_midway_publishes_nothing_more() -> None:
    """Aborted after its first segment: the rest of the input is never published."""
    data = make_ts((Part(320, 240, 7.0),), gop=15)
    published = Published()

    def first_then_abort(segment: Segment) -> None:
        published.segment(segment)
        remuxer.abort()

    remuxer = TsRemuxer(published.init, first_then_abort)
    remuxer.feed(data)
    remuxer.finish()

    result = remuxer.run()

    assert published.order == ["init0", "seg0"]
    # It stopped reading after the keyframe that cut the segment.
    assert result.video_packets == 31


# ── parameter-set IDs ─────────────────────────────────────────────────────────


def _ue(value: int) -> str:
    code = bin(value + 1)[2:]
    return "0" * (len(code) - 1) + code


def _nal(header: bytes, bits: str) -> bytes:
    """A NAL unit from its header and RBSP bits: stop bit, padding, emulation
    prevention after two zero bytes."""
    bits += "1"
    bits += "0" * (-len(bits) % 8)
    rbsp = int(bits, 2).to_bytes(len(bits) // 8, "big")
    out = bytearray()
    zeros = 0
    for byte in rbsp:
        if zeros >= 2 and byte <= 3:
            out.append(3)
            zeros = 0
        out.append(byte)
        zeros = zeros + 1 if byte == 0 else 0
    return header + bytes(out)


def _hevc_sps(sps_id: int, sub_layers: int = 0, *, profile_flags: str = "") -> bytes:
    bits = "0000" + format(sub_layers, "03b") + "1" + "0" * 88 + format(93, "08b")
    flags = profile_flags or "11" * sub_layers
    bits += flags
    if sub_layers:
        bits += "00" * (8 - sub_layers)
    for index in range(sub_layers):
        profile, level = flags[2 * index], flags[2 * index + 1]
        bits += "0" * (88 * int(profile) + 8 * int(level))
    return _nal(bytes([33 << 1, 1]), bits + _ue(sps_id) + _ue(1))


@pytest.mark.parametrize(
    ("unit", "expected"),
    [
        (_nal(bytes([32 << 1, 1]), "0011" + "1" * 12), (32, 3)),
        (_hevc_sps(1), (33, 1)),
        (_hevc_sps(5, sub_layers=2), (33, 5)),
        (_hevc_sps(2, sub_layers=2, profile_flags="1001"), (33, 2)),
        (_nal(bytes([34 << 1, 1]), _ue(7) + _ue(0)), (34, 7)),
    ],
)
def test_hevc_parameter_set_ids_are_read_after_emulation_prevention(
    unit: bytes, expected: tuple[int, int]
) -> None:
    assert recording_hls.parameter_set_id(unit, hevc=True) == expected


@pytest.mark.parametrize(
    ("unit", "expected"),
    [
        (_nal(bytes([0x67]), format(66, "08b") + "0" * 16 + _ue(5) + "1"), (7, 5)),
        (_nal(bytes([0x68]), _ue(4) + _ue(2)), (8, 4)),
    ],
)
def test_h264_parameter_set_ids(unit: bytes, expected: tuple[int, int]) -> None:
    assert recording_hls.parameter_set_id(unit, hevc=False) == expected


def test_h264_sets_are_told_by_the_five_type_bits() -> None:
    """Type 24 (0x18) is no PPS although its low four bits are 8; a start code at the
    very end is no unit."""
    sps = _nal(bytes([0x67]), format(66, "08b") + "0" * 16 + _ue(0) + "1")
    other = _nal(bytes([0x78]), _ue(3))
    data = b"\x00\x00\x01" + sps + b"\x00\x00\x01" + other + b"\x00\x00\x01"

    assert recording_hls.parameter_sets(data, hevc=False) == {(7, 0): sps}


def test_a_frame_that_gives_no_picture_is_a_value_error() -> None:
    """A P-frame without its references decodes to nothing: no format is read."""
    data = make_ts((Part(320, 240, 1.0),))
    container = av.open(io.BytesIO(data), mode="r", format="mpegts")
    frames = [bytes(p) for p in container.demux(container.streams.video[0]) if p.size]
    container.close()
    extradata = recording_hls.annex_b(recording_hls.parameter_sets(frames[0], hevc=False))

    assert recording_hls._decoded_format("h264", extradata, frames[0]).width == 320
    with pytest.raises(ValueError, match="did not decode"):
        recording_hls._decoded_format("h264", extradata, frames[3])


@pytest.mark.parametrize(
    "unit",
    [
        # Cut inside the general profile, inside the sub-layer flags, inside the ue(v).
        bytes([33 << 1, 1]) + b"\x01\x00\x00\x03",
        _hevc_sps(1, sub_layers=2)[:14],
        bytes([34 << 1, 1]) + b"\x00",
    ],
)
def test_a_cut_short_parameter_set_is_a_value_error(unit: bytes) -> None:
    with pytest.raises(ValueError):
        recording_hls.parameter_set_id(unit, hevc=True)


def test_parameter_sets_are_keyed_by_type_and_id_and_unreadable_ones_skipped() -> None:
    vps = _nal(bytes([32 << 1, 1]), "0000" + "1" * 12)
    sps0, sps1 = _hevc_sps(0), _hevc_sps(1)
    pps = _nal(bytes([34 << 1, 1]), _ue(0) + _ue(1))
    broken = bytes([34 << 1, 1]) + b"\x00"
    slice_unit = bytes([20 << 1, 1]) + b"\xaf" * 10
    data = b"".join(b"\x00\x00\x00\x01" + u for u in (sps1, pps, vps, sps0, broken, slice_unit))

    found = recording_hls.parameter_sets(data, hevc=True)

    assert found == {(32, 0): vps, (33, 0): sps0, (33, 1): sps1, (34, 0): pps}
    start = b"\x00\x00\x00\x01"
    assert recording_hls.annex_b(found) == start + vps + start + sps0 + start + sps1 + start + pps


@pytest.mark.parametrize(
    ("profile", "rate", "channels", "expected"),
    [
        ("LC", 16000, 1, "1408"),
        ("LC", 44100, 2, "1210"),
        ("Main", 48000, 2, "0990"),
        (None, 16000, 1, "1408"),
        ("LC", 7000, 1, "17800dac08"),
    ],
)
def test_the_audio_specific_config_follows_the_adts_stream(
    profile: str | None, rate: int, channels: int, expected: str
) -> None:
    assert recording_hls.audio_specific_config(profile, rate, channels).hex() == expected


# ── the playlist ──────────────────────────────────────────────────────────────


def _segment(sequence: int, duration: float = 2.0, init: int = 0, size: int = 10) -> Segment:
    return Segment(sequence, init, duration, bytes(size))


def test_the_playlist_starts_at_the_beginning_and_ends_with_endlist() -> None:
    playlist = HlsPlaylist(max_bytes=1 << 20)
    playlist.add_init(0, b"init")
    for index, duration in enumerate((2.0, 3.2, 0.4)):
        playlist.add_segment(_segment(index, duration))

    assert playlist.render().splitlines() == [
        "#EXTM3U",
        "#EXT-X-VERSION:7",
        "#EXT-X-TARGETDURATION:4",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PLAYLIST-TYPE:EVENT",
        "#EXT-X-START:TIME-OFFSET=0,PRECISE=YES",
        "#EXT-X-INDEPENDENT-SEGMENTS",
        '#EXT-X-MAP:URI="init0.mp4"',
        "#EXTINF:2.000,",
        "seg0.m4s",
        "#EXTINF:3.200,",
        "seg1.m4s",
        "#EXTINF:0.400,",
        "seg2.m4s",
    ]
    playlist.end()
    assert playlist.render().splitlines()[-1] == "#EXT-X-ENDLIST"
    assert playlist.duration == pytest.approx(5.6)


@pytest.mark.parametrize(
    ("durations", "target"), [((), 1), ((0.3,), 1), ((2.0, 2.0004), 2), ((2.0006,), 3)]
)
def test_the_target_duration_is_the_longest_segment_rounded_up(
    durations: tuple[float, ...], target: int
) -> None:
    playlist = HlsPlaylist(max_bytes=1 << 20)
    for index, duration in enumerate(durations):
        playlist.add_segment(_segment(index, duration))

    assert f"#EXT-X-TARGETDURATION:{target}" in playlist.render().splitlines()


def test_a_new_init_is_a_discontinuity_with_its_own_map() -> None:
    playlist = HlsPlaylist(max_bytes=1 << 20)
    playlist.add_init(0, b"a")
    playlist.add_segment(_segment(0))
    playlist.add_segment(_segment(1))
    playlist.add_init(1, b"b")
    playlist.add_segment(_segment(2, init=1))

    lines = playlist.render().splitlines()

    assert lines[lines.index("seg1.m4s") + 1 :] == [
        "#EXT-X-DISCONTINUITY",
        '#EXT-X-MAP:URI="init1.mp4"',
        "#EXTINF:2.000,",
        "seg2.m4s",
    ]
    assert lines.count("#EXT-X-DISCONTINUITY") == 1
    assert not any(line.startswith("#EXT-X-DISCONTINUITY-SEQUENCE") for line in lines)
    assert (playlist.init(0), playlist.init(1), playlist.init(2)) == (b"a", b"b", None)


def test_past_the_memory_cap_the_oldest_segments_leave_and_the_window_slides(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The playlist drops its EVENT type, counts the media sequence and the
    discontinuities before its window, and frees an init no listed segment uses."""
    caplog.set_level(logging.DEBUG, logger=recording_hls.__name__)
    playlist = HlsPlaylist(max_bytes=400)
    playlist.add_init(0, bytes(20))
    for sequence in range(3):
        playlist.add_segment(_segment(sequence, size=90))
    playlist.add_init(1, bytes(20))
    # Exactly at the cap: nothing leaves.
    playlist.add_segment(_segment(3, init=1, size=90))
    assert (playlist.segments, playlist.dropped, playlist.held) == (4, 0, 400)
    assert "#EXT-X-PLAYLIST-TYPE:EVENT" in playlist.render()

    playlist.add_segment(_segment(4, init=1, size=90))

    assert (playlist.segments, playlist.dropped) == (4, 1)
    assert playlist.held == 400
    assert playlist.peak == 490
    assert playlist.segment(0) is None
    assert playlist.segment(1) == bytes(90)
    assert playlist.segment(5) is None
    lines = playlist.render().splitlines()
    assert "#EXT-X-PLAYLIST-TYPE:EVENT" not in lines
    assert "#EXT-X-MEDIA-SEQUENCE:1" in lines
    assert lines.count("#EXT-X-DISCONTINUITY") == 1

    # The window moves past the old init: it is freed and counted in the sequence.
    playlist.add_segment(_segment(5, init=1, size=90))
    playlist.add_segment(_segment(6, init=1, size=90))
    lines = playlist.render().splitlines()
    assert "#EXT-X-DISCONTINUITY-SEQUENCE:1" in lines
    assert "#EXT-X-DISCONTINUITY" not in lines
    assert lines[lines.index("#EXT-X-INDEPENDENT-SEGMENTS") + 1] == '#EXT-X-MAP:URI="init1.mp4"'
    assert playlist.init(0) is None
    assert playlist.held == 20 + 4 * 90
    dropped = [r for r in caplog.records if "oldest segments dropped" in r.getMessage()]
    assert [r.levelno for r in dropped] == [logging.DEBUG]


def test_the_newest_segment_stays_even_past_the_cap() -> None:
    playlist = HlsPlaylist(max_bytes=50)
    playlist.add_segment(_segment(0, size=40))
    playlist.add_segment(_segment(1, size=80))

    assert playlist.segments == 1
    assert playlist.segment(1) == bytes(80)


def test_free_lets_go_of_every_byte() -> None:
    playlist = HlsPlaylist(max_bytes=1 << 20)
    playlist.add_init(0, b"x")
    playlist.add_segment(_segment(0))

    playlist.free()

    assert (playlist.held, playlist.segments, playlist.init(0), playlist.segment(0)) == (
        0,
        0,
        None,
        None,
    )
    assert playlist.peak == 11
