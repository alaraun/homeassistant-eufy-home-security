"""A stand-in for the ffmpeg binary, reached through a command prefix.

``hevc_to_jpeg`` takes a command prefix rather than a binary, which is what
lets a test substitute this script: ``FAKE_FFMPEG_PREFIX`` runs it with the
suite's own interpreter, and the prefix goes through the same ``shlex.split``
a real ``ffmpeg`` path does.

Two modes, chosen by the ``-i`` argument:

- ``-i pipe:0`` (a keyframe decode) reads all of stdin. Data starting with the
  Annex-B VPS start ``00 00 00 01 40 01`` can only have come out of a keyframe that
  really decrypted, so it answers with a minimal JPEG declaring 3840x2160 and exits
  0. Anything else exits 69, as a decoder given garbage fails.
- ``-i <file> ... <output>`` (a clip remux) writes :data:`REMUX_MAGIC` and then the
  input's bytes to the last argument and exits 0; an input that does not start with
  an MPEG-TS sync byte exits 69.

Environment knobs, read by the child process:

``FAKE_FFMPEG_SLEEP``  seconds to sleep before answering (timeout tests).
``FAKE_FFMPEG_FAIL``   when set, exit 1; a remux first writes a partial output.
``FAKE_FFMPEG_ARGV``   a file each run appends its arguments to, one JSON list a line.
"""

from __future__ import annotations

import json
import os
import shlex
import struct
import sys
import time
from pathlib import Path

__all__ = ["FAKE_FFMPEG_PREFIX", "REMUX_MAGIC", "main"]

FAKE_FFMPEG_PREFIX = f"{shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))}"
# What a remuxed file starts with, before the copied input bytes.
REMUX_MAGIC = b"FAKE-MP4\n"

_ANNEX_B_VPS = b"\x00\x00\x00\x01\x40\x01"
_SOF0_3840_2160 = b"\xff\xc0\x00\x11\x08" + struct.pack(">HH", 2160, 3840) + b"\x03" + b"\x00" * 9
_TS_SYNC = 0x47


def _sleep() -> None:
    if sleep := os.environ.get("FAKE_FFMPEG_SLEEP"):
        time.sleep(float(sleep))


def _remux(source: str, target: str) -> int:
    data = Path(source).read_bytes()
    _sleep()
    if os.environ.get("FAKE_FFMPEG_FAIL"):
        Path(target).write_bytes(REMUX_MAGIC)
        return 1
    if not data or data[0] != _TS_SYNC:
        return 69
    Path(target).write_bytes(REMUX_MAGIC + data)
    return 0


def main() -> int:
    args = sys.argv[1:]
    if log := os.environ.get("FAKE_FFMPEG_ARGV"):
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(args) + "\n")
    source = args[args.index("-i") + 1] if "-i" in args[:-1] else "pipe:0"
    if source != "pipe:0":
        return _remux(source, args[-1])
    data = sys.stdin.buffer.read()
    _sleep()
    if os.environ.get("FAKE_FFMPEG_FAIL"):
        return 1
    if not data.startswith(_ANNEX_B_VPS):
        return 69
    sys.stdout.buffer.write(b"\xff\xd8" + _SOF0_3840_2160 + b"\xff\xd9")
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
