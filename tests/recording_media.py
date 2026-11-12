"""MPEG-TS test media made with PyAV: H.264 or HEVC video with silent AAC.

``make_ts`` encodes each part at its own picture size and joins them into one TS on one
timeline, the way a camera that changes resolution mid-recording streams: every
keyframe carries its parameter sets in-band.
"""

from __future__ import annotations

import functools
import io
from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import cast

import av
import numpy as np
from av.audio.stream import AudioStream
from av.video.stream import VideoStream

TICKS: Fraction = Fraction(1, 90000)


@dataclass(frozen=True, slots=True)
class Part:
    """``seconds`` of video at ``width`` x ``height``; ``sar`` its pixel aspect."""

    width: int
    height: int
    seconds: float
    sar: Fraction | None = None


def _encode_part(
    part: Part, *, codec: str, fps: int, gop: int, audio: bool, first_frame: int, bframes: int
) -> bytes:
    """One part as its own TS, frames numbered from ``first_frame`` (moving content)."""
    buffer = io.BytesIO()
    out = av.open(buffer, mode="w", format="mpegts")
    video = cast(VideoStream, out.add_stream(codec, rate=fps))
    video.width = part.width
    video.height = part.height
    video.pix_fmt = "yuv420p"
    context = video.codec_context
    context.gop_size = gop
    context.max_b_frames = bframes
    params = "repeat-headers=1"
    if codec == "libx265":
        params += f":log-level=error:keyint={gop}:min-keyint={gop}:scenecut=0"
        context.options = {"preset": "ultrafast", "x265-params": params}
    else:
        context.options = {"preset": "ultrafast", "x264-params": params + ":scenecut=0"}
    if part.sar is not None:
        context.sample_aspect_ratio = part.sar
    sound = None
    if audio:
        sound = cast(AudioStream, out.add_stream("aac", rate=16000))
        sound.layout = "mono"
    frames = round(part.seconds * fps)
    samples = 0
    for index in range(frames):
        number = first_frame + index
        luma = np.full((part.height, part.width), 64, dtype=np.uint8)
        column = (number * 7) % part.width
        luma[:, column : column + 8] = 235
        luma[(number * 3) % part.height, :] = 16
        chroma = np.full((part.height // 2, part.width), 128, dtype=np.uint8)
        frame = av.VideoFrame.from_ndarray(np.vstack([luma, chroma]), format="yuv420p")
        frame.pts = index
        frame.time_base = Fraction(1, fps)
        for packet in video.encode(frame):
            out.mux(packet)
        if sound is not None:
            while samples < (index + 1) * 16000 // fps:
                silence = av.AudioFrame.from_ndarray(
                    np.zeros((1, 1024), dtype=np.float32), format="fltp", layout="mono"
                )
                silence.sample_rate = 16000
                silence.pts = samples
                silence.time_base = Fraction(1, 16000)
                samples += 1024
                for packet in sound.encode(silence):
                    out.mux(packet)
    for packet in video.encode(None):
        out.mux(packet)
    if sound is not None:
        for packet in sound.encode(None):
            out.mux(packet)
    out.close()
    return buffer.getvalue()


@functools.cache
def make_ts(
    parts: Sequence[Part] = (Part(320, 240, 5.0),),
    *,
    codec: str = "libx264",
    fps: int = 15,
    gop: int = 15,
    audio: bool = True,
    start: int = 900_000,
    bframes: int = 0,
) -> bytes:
    """The parts joined into one TS whose first video DTS is ``start`` ticks (90 kHz);
    ``bframes`` B-frames between references (H.264)."""
    out_buffer = io.BytesIO()
    out = av.open(out_buffer, mode="w", format="mpegts")
    template_video = template_audio = None
    out_video = out_audio = None
    offset = start
    frame = 0
    last: dict[str, int] = {}
    for part in parts:
        data = _encode_part(
            part, codec=codec, fps=fps, gop=gop, audio=audio, first_frame=frame, bframes=bframes
        )
        frame += round(part.seconds * fps)
        source = av.open(io.BytesIO(data), mode="r", format="mpegts")
        if template_video is None:
            template_video = source.streams.video[0]
            out_video = out.add_stream_from_template(template_video)
            if source.streams.audio:
                template_audio = source.streams.audio[0]
                out_audio = out.add_stream_from_template(template_audio)
        packets = [p for p in source.demux() if p.size and p.dts is not None]
        video_packets = [p for p in packets if p.stream.type == "video"]
        first = video_packets[0].dts
        assert first is not None and video_packets[-1].dts is not None
        length = video_packets[-1].dts + (video_packets[-1].duration or 6000) - first
        for packet in packets:
            assert packet.pts is not None and packet.dts is not None
            kind = packet.stream.type
            packet.pts += offset - first
            packet.dts += offset - first
            if packet.dts <= last.get(kind, -1):
                # Audio of the previous part reaching past its video.
                continue
            last[kind] = packet.dts
            target = out_video if kind == "video" else out_audio
            assert target is not None
            packet.stream = target
            out.mux(packet)
        offset += length
        source.close()
    out.close()
    return out_buffer.getvalue()


def split_at(data: bytes, seconds: float) -> int:
    """The byte offset of the first video frame ``seconds`` or more into ``data``."""
    container = av.open(io.BytesIO(data), mode="r", format="mpegts")
    first: int | None = None
    offset = len(data)
    for packet in container.demux(container.streams.video[0]):
        if not packet.size or packet.pts is None or packet.pos is None:
            continue
        first = packet.pts if first is None else first
        if (packet.pts - first) * TICKS >= seconds:
            offset = packet.pos
            break
    container.close()
    return offset


def audio_only(data: bytes) -> bytes:
    """The audio of a TS, as a TS of its own."""
    source = av.open(io.BytesIO(data), mode="r", format="mpegts")
    buffer = io.BytesIO()
    out = av.open(buffer, mode="w", format="mpegts")
    audio = out.add_stream_from_template(source.streams.audio[0])
    for packet in source.demux(source.streams.audio[0]):
        if packet.size:
            packet.stream = audio
            out.mux(packet)
    out.close()
    source.close()
    return buffer.getvalue()
