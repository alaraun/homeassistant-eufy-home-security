"""A station recording's MPEG-TS repackaged into fragmented-MP4 HLS, in memory.

Stream copy with PyAV (no decode, no re-encode, no disk): :class:`TsRemuxer` runs in a
thread of its own, reads the TS from a byte queue the library's writer fills, and
publishes an init section per picture format and one fMP4 segment per cut;
:class:`HlsPlaylist` holds them on the event loop and renders the media playlist.

- A segment is cut at the first video keyframe at least ``SEGMENT_SECONDS`` after its
  start, at a parameter-set change, and at the end of the input: nothing is held back.
- Every segment shares one timeline: timestamps are moved by one offset (the first
  keyframe's decode time goes to 0); fragments carry their own decode time.
- HEVC is tagged ``hvc1`` (Safari's requirement), which keeps the parameter sets in the
  init only: a change of the VPS/SPS/PPS (tracked by type and ID) starts a new init,
  a discontinuity in the playlist. H.264 stays ``avc1``.
- :class:`HlsPlaylist` keeps at most ``max_bytes`` of media; past it the oldest
  segments leave the playlist, which then is a sliding window.
"""

from __future__ import annotations

import io
import logging
import math
import queue
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Final, cast

import av
from av.audio.stream import AudioStream
from av.container import InputContainer, OutputContainer
from av.packet import Packet
from av.video.codeccontext import VideoCodecContext
from av.video.stream import VideoStream

_LOGGER = logging.getLogger(__name__)

# Shortest segment, seconds: a cut waits for the first keyframe at or past it.
SEGMENT_SECONDS: Final = 2.0
HLS_MIME_TYPE: Final = "application/vnd.apple.mpegurl"
INIT_MIME_TYPE: Final = "video/mp4"
SEGMENT_MIME_TYPE: Final = "video/iso.segment"

_MOVFLAGS: Final = (
    "frag_custom+empty_moov+default_base_moof+frag_discont"
    "+negative_cts_offsets+skip_sidx+skip_trailer"
)
_START_CODE: Final = b"\x00\x00\x01"
# NAL unit types of the parameter sets, per codec.
_HEVC_SETS: Final = frozenset({32, 33, 34})
_H264_SETS: Final = frozenset({7, 8})
# Audio packets kept while the first keyframe has not come.
_EARLY_AUDIO_MAX: Final = 64
# AAC audio object types of the ADTS profiles, and the sampling-frequency indexes.
_AAC_OBJECT_TYPES: Final = {"Main": 1, "LC": 2, "SSR": 3, "LTP": 4}
_AAC_RATES: Final = (96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000)
_AAC_RATES_LOW: Final = (12000, 11025, 8000, 7350)


class RemuxError(Exception):
    """The input cannot be repackaged (no video, unreadable TS)."""


@dataclass(frozen=True, slots=True)
class Segment:
    """One published fMP4 segment: ``moof`` + ``mdat`` boxes, read with init ``init``."""

    sequence: int
    init: int
    duration: float
    data: bytes


@dataclass(frozen=True, slots=True)
class RemuxResult:
    """What a finished remux produced; ``cpu_seconds`` is the thread's CPU time."""

    segments: int
    inits: int
    video_packets: int
    media_seconds: float
    cpu_seconds: float


# ── parameter sets ─────────────────────────────────────────────────────────────


def nal_units(data: bytes, types: frozenset[int], hevc: bool) -> list[bytes]:
    """The Annex B NAL units of ``data`` whose type is in ``types`` (header included)."""
    found: list[bytes] = []
    start = data.find(_START_CODE)
    while start >= 0:
        begin = start + 3
        start = data.find(_START_CODE, begin)
        if begin >= len(data):
            break
        kind = (data[begin] >> 1) & 0x3F if hevc else data[begin] & 0x1F
        if kind in types:
            end = len(data) if start < 0 else start
            # Zero bytes before the next start code belong to it (4-byte form).
            found.append(data[begin:end].rstrip(b"\x00"))
    return found


class _Bits:
    """An MSB-first bit reader over an RBSP; ValueError past its end."""

    def __init__(self, rbsp: bytes) -> None:
        self._data = rbsp
        self._pos = 0

    def u(self, count: int) -> int:
        value = 0
        for _ in range(count):
            byte = self._pos >> 3
            if byte >= len(self._data):
                raise ValueError("parameter set too short")
            value = (value << 1) | ((self._data[byte] >> (7 - (self._pos & 7))) & 1)
            self._pos += 1
        return value

    def ue(self) -> int:
        zeros = 0
        while self.u(1) == 0:
            zeros += 1
        return (1 << zeros) - 1 + self.u(zeros)


def _rbsp(payload: bytes) -> bytes:
    """``payload`` without emulation-prevention bytes."""
    return payload.replace(b"\x00\x00\x03", b"\x00\x00")


def parameter_set_id(unit: bytes, hevc: bool) -> tuple[int, int]:
    """``(NAL type, parameter-set ID)`` of a VPS/SPS/PPS unit; ValueError if unreadable."""
    if hevc:
        kind = (unit[0] >> 1) & 0x3F
        bits = _Bits(_rbsp(unit[2:66]))
        if kind == 32:
            return kind, bits.u(4)
        if kind == 34:
            return kind, bits.ue()
        bits.u(4)
        sub_layers = bits.u(3)
        bits.u(1)
        bits.u(88 + 8)
        flags = [(bits.u(1), bits.u(1)) for _ in range(sub_layers)]
        if sub_layers:
            bits.u(2 * (8 - sub_layers))
        for profile, level in flags:
            bits.u(88 * profile + 8 * level)
        return kind, bits.ue()
    kind = unit[0] & 0x1F
    bits = _Bits(_rbsp(unit[1:64]))
    if kind == 7:
        bits.u(24)
    return kind, bits.ue()


def parameter_sets(data: bytes, hevc: bool) -> dict[tuple[int, int], bytes]:
    """The parameter sets in ``data`` by ``(type, ID)``; a set whose ID does not parse
    is skipped."""
    found: dict[tuple[int, int], bytes] = {}
    for unit in nal_units(data, _HEVC_SETS if hevc else _H264_SETS, hevc):
        try:
            found[parameter_set_id(unit, hevc)] = unit
        except ValueError:
            continue
    return found


def annex_b(sets: dict[tuple[int, int], bytes]) -> bytes:
    """Extradata in Annex B form: the sets in key order (VPS, SPS, PPS by ID)."""
    return b"".join(b"\x00\x00\x00\x01" + sets[key] for key in sorted(sets))


@dataclass(frozen=True, slots=True)
class _Format:
    """A picture format; ``aspect`` 0 when the stream gives none."""

    width: int
    height: int
    aspect: Fraction


def _decoded_format(codec: str, extradata: bytes, keyframe: bytes) -> _Format:
    """The picture format of ``keyframe`` decoded with ``extradata``; ValueError or
    ``av.FFmpegError`` when it does not decode."""
    context = cast(VideoCodecContext, av.CodecContext.create(codec, "r"))
    context.extradata = extradata
    frames = list(context.decode(Packet(keyframe)))
    frames += context.decode(None)
    if not frames:
        raise ValueError("keyframe did not decode")
    aspect = context.sample_aspect_ratio
    return _Format(
        context.width,
        context.height,
        Fraction(aspect.numerator, aspect.denominator) if aspect is not None else Fraction(0),
    )


def audio_specific_config(profile: str | None, sample_rate: int, channels: int) -> bytes:
    """The two-byte AudioSpecificConfig an MP4 track of ADTS AAC needs."""
    object_type = _AAC_OBJECT_TYPES.get(profile or "", 2)
    rates = _AAC_RATES + _AAC_RATES_LOW
    rate = rates.index(sample_rate) if sample_rate in rates else 15
    if rate == 15:
        # An explicit 24-bit rate follows the escape index.
        value = (object_type << 35) | (15 << 31) | (sample_rate << 7) | (channels << 3)
        return value.to_bytes(5, "big")
    return ((object_type << 11) | (rate << 7) | (channels << 3)).to_bytes(2, "big")


# ── the remux thread ───────────────────────────────────────────────────────────


class _QueueReader:
    """A non-seekable file over a queue of byte chunks; None ends it."""

    def __init__(self) -> None:
        self.chunks: queue.SimpleQueue[bytes | None] = queue.SimpleQueue()
        self._buffer = b""
        self._eof = False

    def read(self, size: int = -1) -> bytes:
        while not self._buffer and not self._eof:
            chunk = self.chunks.get()
            if chunk is None:
                self._eof = True
            else:
                self._buffer = chunk
        if size < 0 or size >= len(self._buffer):
            data, self._buffer = self._buffer, b""
            return data
        data, self._buffer = self._buffer[:size], self._buffer[size:]
        return data


def _split_init(data: bytes) -> tuple[bytes, bytes]:
    """``data`` split before its first ``moof`` box: (init, media)."""
    index = 0
    while index + 8 <= len(data):
        size = int.from_bytes(data[index : index + 4], "big")
        if data[index + 4 : index + 8] == b"moof":
            return data[:index], data[index:]
        if size < 8:
            break
        index += size
    raise RemuxError("no fragment in the muxed segment")


class TsRemuxer:
    """Repackages one MPEG-TS into fMP4 segments, in the thread that calls :meth:`run`.

    :meth:`feed`, :meth:`finish` and :meth:`abort` are safe from any thread. ``on_init``
    gets ``(index, init bytes)`` before the first segment that uses it, ``on_segment``
    each :class:`Segment` in order; both are called from the remux thread.
    """

    def __init__(
        self,
        on_init: Callable[[int, bytes], None],
        on_segment: Callable[[Segment], None],
    ) -> None:
        self._on_init = on_init
        self._on_segment = on_segment
        self._reader = _QueueReader()
        self._aborted = threading.Event()
        self._input: InputContainer | None = None
        self._video: VideoStream | None = None
        self._audio: AudioStream | None = None
        self._hevc = False
        self._sets: dict[tuple[int, int], bytes] = {}
        self._format: _Format | None = None
        self._init = -1
        self._init_sent = -1
        self._sequence = 0
        self._origin: int | None = None
        self._out: OutputContainer | None = None
        self._out_video: VideoStream | None = None
        self._out_audio: AudioStream | None = None
        self._buffer: io.BytesIO | None = None
        self._segment_start = 0
        self._last_dts: int | None = None
        self._frame_ticks = 0
        self._frame_guess = 1
        self._early_audio: list[Packet[Any]] = []
        self._video_packets = 0
        self._media_ticks = 0
        self._time_base = Fraction(1, 90000)
        self._asc = b""
        self._adts: av.BitStreamFilterContext | None = None

    def feed(self, data: bytes) -> None:
        """Queue TS bytes."""
        self._reader.chunks.put(data)

    def finish(self) -> None:
        """End the input: the rest is repackaged and the last segment published."""
        self._reader.chunks.put(None)

    def abort(self) -> None:
        """Stop at once; nothing more is published."""
        self._aborted.set()
        self._reader.chunks.put(None)

    def run(self) -> RemuxResult:
        """Repackage until the input ends; raises :class:`RemuxError` on an input it
        cannot read (no video stream, no keyframe, not a TS)."""
        cpu = time.thread_time()
        try:
            self._run()
        finally:
            if self._out is not None:
                self._out.close()
            if self._input is not None:
                self._input.close()
        return RemuxResult(
            segments=self._sequence,
            inits=self._init + 1,
            video_packets=self._video_packets,
            media_seconds=float(self._media_ticks * self._time_base),
            cpu_seconds=time.thread_time() - cpu,
        )

    def _run(self) -> None:
        try:
            self._input = av.open(self._reader, mode="r", format="mpegts")
        except av.FFmpegError as err:
            if self._aborted.is_set():
                return
            raise RemuxError(f"unreadable input ({type(err).__name__})") from err
        if not self._input.streams.video:
            raise RemuxError("no video stream")
        self._video = self._input.streams.video[0]
        self._audio = self._input.streams.audio[0] if self._input.streams.audio else None
        if self._audio is not None:
            audio = self._audio.codec_context
            self._asc = audio_specific_config(
                audio.profile, audio.sample_rate, audio.layout.nb_channels
            )
            self._adts = av.BitStreamFilterContext("aac_adtstoasc", self._audio)
        assert self._video.time_base is not None
        base = self._video.time_base
        self._time_base = Fraction(base.numerator, base.denominator)
        self._hevc = self._video.codec_context.name == "hevc"
        rate = self._video.average_rate or self._video.guessed_rate
        if rate:
            self._frame_guess = max(1, round(1 / (rate * self._time_base)))
        extradata = self._video.codec_context.extradata
        if extradata:
            self._sets = parameter_sets(extradata, self._hevc)
        streams = [self._video] + ([self._audio] if self._audio is not None else [])
        try:
            for packet in self._input.demux(streams):
                if self._aborted.is_set():
                    return
                if packet.size == 0:
                    continue
                if packet.stream is self._video:
                    self._video_packet(packet)
                else:
                    self._audio_packet(packet)
        except av.FFmpegError as err:
            if self._aborted.is_set():
                return
            raise RemuxError(f"input broke off ({type(err).__name__})") from err
        if self._out is None:
            raise RemuxError("no video keyframe")
        self._close_segment(self._end_dts())

    def _end_dts(self) -> int:
        assert self._last_dts is not None
        return self._last_dts + self._frame_ticks

    def _video_packet(self, packet: Packet[Any]) -> None:
        if packet.pts is None:
            return
        if self._out is None and not packet.is_keyframe:
            return
        if packet.dts is None:
            # A frame demuxed without DTS: one frame before its PTS, after the last.
            packet.dts = packet.pts - (packet.duration or self._frame_guess)
            if self._last_dts is not None:
                packet.dts = max(packet.dts, self._origin_dts(self._last_dts) + 1)
        if self._origin is None:
            self._origin = packet.dts
        dts = packet.dts - self._origin
        pts = packet.pts - self._origin
        if dts < 0 or (self._last_dts is not None and dts <= self._last_dts) or pts < dts:
            return
        if packet.is_keyframe:
            self._keyframe(packet, dts)
        if self._last_dts is not None:
            self._frame_ticks = dts - self._last_dts
        elif packet.duration:
            self._frame_ticks = packet.duration
        self._last_dts = dts
        packet.dts = dts
        packet.pts = pts
        packet.stream = self._out_video
        assert self._out is not None
        self._out.mux(packet)
        self._video_packets += 1
        self._media_ticks = dts + self._frame_ticks
        if self._early_audio:
            for early in self._early_audio:
                self._audio_packet(early)
            self._early_audio = []

    def _origin_dts(self, dts: int) -> int:
        assert self._origin is not None
        return dts + self._origin

    def _keyframe(self, packet: Packet[Any], dts: int) -> None:
        """Cut before ``packet`` where due; a parameter-set change starts a new init."""
        new_format = self._new_format(bytes(packet))
        if self._out is None:
            if new_format is not None:
                self._format = new_format
            self._init = 0
            self._open_segment(dts)
            return
        due = (dts - self._segment_start) * self._time_base >= SEGMENT_SECONDS
        if new_format is None and not due:
            return
        self._close_segment(dts)
        if new_format is not None:
            self._format = new_format
            self._init += 1
        self._open_segment(dts)

    def _new_format(self, keyframe: bytes) -> _Format | None:
        """The format of a keyframe whose parameter sets differ from the current ones
        (committed here), else None; sets that do not decode are left out."""
        found = parameter_sets(keyframe, self._hevc)
        merged = self._sets | found
        if merged == self._sets:
            return None
        assert self._video is not None
        try:
            decoded = _decoded_format(self._video.codec_context.name, annex_b(merged), keyframe)
        except (ValueError, av.FFmpegError) as err:
            _LOGGER.debug("New parameter sets did not decode (%s); kept the current ones", err)
            return None
        self._sets = merged
        return decoded

    def _audio_packet(self, packet: Packet[Any]) -> None:
        if packet.dts is None or packet.pts is None:
            return
        if self._out is None:
            if len(self._early_audio) < _EARLY_AUDIO_MAX:
                self._early_audio.append(packet)
            return
        assert self._origin is not None and self._audio is not None
        shift = math.ceil(self._origin * self._time_base / self._audio.time_base)
        if packet.dts - shift < 0:
            return
        packet.dts -= shift
        packet.pts -= shift
        assert self._adts is not None
        for raw in self._adts.filter(packet):
            raw.stream = self._out_audio
            self._out.mux(raw)

    def _open_segment(self, dts: int) -> None:
        assert self._video is not None
        self._segment_start = dts
        self._buffer = io.BytesIO()
        self._out = av.open(
            self._buffer,
            mode="w",
            format="mp4",
            container_options={
                "movflags": _MOVFLAGS,
                "fragment_index": str(self._sequence + 1),
                "video_track_timescale": str(self._time_base.denominator),
            },
        )
        video = self._out.add_stream_from_template(self._video)
        context = video.codec_context
        if self._sets:
            context.extradata = annex_b(self._sets)
        if self._format is not None:
            context.width = self._format.width
            context.height = self._format.height
            context.sample_aspect_ratio = self._format.aspect
        if self._hevc:
            context.codec_tag = "hvc1"
        self._out_video = video
        self._out_audio = None
        if self._audio is not None:
            self._out_audio = self._out.add_stream_from_template(self._audio)
            self._out_audio.codec_context.extradata = self._asc

    def _close_segment(self, end_dts: int) -> None:
        assert self._out is not None and self._buffer is not None
        self._out.close()
        self._out = None
        init, media = _split_init(self._buffer.getvalue())
        self._buffer = None
        if self._init != self._init_sent:
            self._on_init(self._init, init)
            self._init_sent = self._init
        if self._aborted.is_set():
            return
        duration = float((end_dts - self._segment_start) * self._time_base)
        self._on_segment(Segment(self._sequence, self._init, duration, media))
        self._sequence += 1


# ── the playlist, on the event loop ────────────────────────────────────────────


class HlsPlaylist:
    """One play's published inits and segments and its media playlist.

    Holds at most ``max_bytes`` of segments: past it the oldest leave (never the
    newest), and the playlist stops calling itself an EVENT playlist.
    """

    def __init__(self, max_bytes: int) -> None:
        self._max_bytes = max_bytes
        self._inits: dict[int, bytes] = {}
        self._segments: deque[Segment] = deque()
        self.ended = False
        self.dropped = 0
        self.held = 0
        self.peak = 0
        self.duration = 0.0

    @property
    def segments(self) -> int:
        """Segments listed now."""
        return len(self._segments)

    def add_init(self, index: int, data: bytes) -> None:
        self._inits[index] = data
        self._count(len(data))

    def add_segment(self, segment: Segment) -> None:
        self._segments.append(segment)
        self.duration += segment.duration
        self._count(len(segment.data))
        while self.held > self._max_bytes and len(self._segments) > 1:
            gone = self._segments.popleft()
            self.held -= len(gone.data)
            if not self.dropped:
                _LOGGER.debug(
                    "Recording play past %d bytes: oldest segments dropped", self._max_bytes
                )
            self.dropped += 1
            if self._segments[0].init != gone.init:
                self.held -= len(self._inits.pop(gone.init, b""))

    def _count(self, size: int) -> None:
        self.held += size
        self.peak = max(self.peak, self.held)

    def end(self) -> None:
        """No more segments: the playlist gets its ``EXT-X-ENDLIST``."""
        self.ended = True

    def free(self) -> None:
        """Let go of every byte."""
        self._inits.clear()
        self._segments.clear()
        self.held = 0

    def init(self, index: int) -> bytes | None:
        return self._inits.get(index)

    def segment(self, sequence: int) -> bytes | None:
        if not self._segments:
            return None
        first = self._segments[0].sequence
        if not first <= sequence < first + len(self._segments):
            return None
        return self._segments[sequence - first].data

    def render(self) -> str:
        """The media playlist; URIs are relative to it."""
        segments = list(self._segments)
        target = max([1, *(math.ceil(round(s.duration, 3)) for s in segments)])
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:7",
            f"#EXT-X-TARGETDURATION:{target}",
            f"#EXT-X-MEDIA-SEQUENCE:{segments[0].sequence if segments else 0}",
        ]
        if segments and segments[0].init:
            # One discontinuity per init change before the window.
            lines.append(f"#EXT-X-DISCONTINUITY-SEQUENCE:{segments[0].init}")
        if not self.dropped:
            lines.append("#EXT-X-PLAYLIST-TYPE:EVENT")
        lines += ["#EXT-X-START:TIME-OFFSET=0,PRECISE=YES", "#EXT-X-INDEPENDENT-SEGMENTS"]
        previous: int | None = None
        for segment in segments:
            if segment.init != previous:
                if previous is not None:
                    lines.append("#EXT-X-DISCONTINUITY")
                lines.append(f'#EXT-X-MAP:URI="init{segment.init}.mp4"')
                previous = segment.init
            lines += [f"#EXTINF:{segment.duration:.3f},", f"seg{segment.sequence}.m4s"]
        if self.ended:
            lines.append("#EXT-X-ENDLIST")
        return "\n".join(lines) + "\n"
