#!/usr/bin/env python3
"""End-to-end synthetic integration test for the published WFS recovery entry point.

Creates four interleaved WFS-style HEVC streams, verifies clean recovery, then
corrupts one continuation fragment and verifies the tool fails closed instead
of silently reporting all streams PASS.
"""

import json
import random
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

FRAG = 2 * 1024 * 1024
SYNC = b"\x00\x00\x01"
ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "wfs_recover.py"


def run(cmd, timeout=300):
    return subprocess.run(
        [str(x) for x in cmd],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        text=True,
    )


def encode_ts(year, month, day, hour, minute, second):
    return (
        ((year - 2000) & 0x3F) << 26
        | (month & 0x0F) << 22
        | (day & 0x1F) << 17
        | (hour & 0x1F) << 12
        | (minute & 0x3F) << 6
        | (second & 0x3F)
    )


def split_sizes(length, count, seed):
    rnd = random.Random(seed)
    weights = [0.7 + rnd.random() * 0.6 for _ in range(count)]
    total = sum(weights)
    sizes = [max(1, int(length * w / total)) for w in weights]
    diff = length - sum(sizes)
    i = 0
    while diff:
        j = i % count
        if diff > 0:
            sizes[j] += 1
            diff -= 1
        elif sizes[j] > 1:
            sizes[j] -= 1
            diff += 1
        i += 1
    return sizes


def make_stream(payload, ts, seed, packet_count=900):
    sizes = split_sizes(len(payload), packet_count, seed)
    out = bytearray()
    off = 0
    for i, size in enumerate(sizes):
        chunk = payload[off:off + size]
        off += size
        if i == 0:
            header = (
                SYNC
                + b"\xfd"
                + b"\x04\x0f\xa0\xb4"
                + struct.pack("<I", ts)
                + struct.pack("<I", len(chunk))
            )
        else:
            header = SYNC + b"\xfc" + struct.pack("<I", len(chunk))
        out += header + chunk
    assert off == len(payload)
    return bytes(out)


def fragment_stream(data):
    frags = [data[i:i + FRAG] for i in range(0, len(data), FRAG)]
    frags[-1] += b"\xff" * (FRAG - len(frags[-1]))
    assert all(len(x) == FRAG for x in frags)
    return frags


def make_next_start(payload, ts):
    sample = payload[:50000]
    data = (
        SYNC
        + b"\xfd"
        + b"\x04\x0f\xa0\xb4"
        + struct.pack("<I", ts)
        + struct.pack("<I", len(sample))
        + sample
    )
    return data + b"\xff" * (FRAG - len(data))


def build_image(path, payload):
    ts0 = encode_ts(2026, 1, 15, 0, 0, 0)
    streams = [
        fragment_stream(make_stream(payload, ts0, 100 + cam))
        for cam in range(4)
    ]
    levels = max(len(x) for x in streams)
    physical = []
    for level in range(levels):
        for cam in range(4):
            if level < len(streams[cam]):
                physical.append(streams[cam][level])

    boundary_index = len(physical)
    ts1 = encode_ts(2026, 1, 15, 0, 1, 0)
    for _ in range(4):
        physical.append(make_next_start(payload, ts1))

    path.write_bytes(b"".join(physical))
    return boundary_index


def recover(raw, out, work, deep=True):
    cmd = [
        sys.executable,
        TOOL,
        "recover",
        "--raw", raw,
        "--date", "2026-01-15",
        "--out", out,
        "--work", work,
        "--segments", "00-00",
        "--strategy", "balanced",
        "--ignore-space",
    ]
    if deep:
        cmd.append("--deep-verify")
    result = run(cmd, timeout=600)
    if result.returncode != 0:
        raise RuntimeError(
            f"recovery failed rc={result.returncode}\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
    return json.loads((Path(out) / "manifest.json").read_text())


def main():
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise SystemExit("ffmpeg/ffprobe are required")

    with tempfile.TemporaryDirectory(prefix="wfs-integration-") as td:
        td = Path(td)
        hevc = td / "source.h265"
        gen = run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=15",
            "-t", "60",
            "-c:v", "libx265", "-preset", "ultrafast",
            "-x265-params", "log-level=error:keyint=30:min-keyint=30:scenecut=0",
            "-crf", "20", "-an", "-f", "hevc", hevc,
        ], timeout=180)
        if gen.returncode != 0:
            raise RuntimeError(f"HEVC generation failed:\n{gen.stderr}")

        raw = td / "clean.raw"
        boundary = build_image(raw, hevc.read_bytes())

        manifest = recover(raw, td / "clean-out", td / "clean-work", deep=True)
        rows = manifest["streams"]
        statuses = [r.get("status") for r in rows]
        if len(rows) != 4 or statuses != ["PASS"] * 4:
            raise AssertionError(f"clean recovery did not PASS all four streams: {statuses}")

        # Corrupt one continuation fragment inside the first recording span.
        damaged = td / "damaged.raw"
        data = bytearray(raw.read_bytes())
        corrupt_frag = 4
        if corrupt_frag >= boundary:
            raise AssertionError("synthetic layout did not create a continuation fragment")
        data[corrupt_frag * FRAG:(corrupt_frag + 1) * FRAG] = b"\x00" * FRAG
        damaged.write_bytes(data)

        damaged_manifest = recover(
            damaged, td / "damaged-out", td / "damaged-work", deep=True
        )
        damaged_statuses = [r.get("status") for r in damaged_manifest["streams"]]
        if "FAIL" not in damaged_statuses:
            raise AssertionError(
                f"damaged recovery failed to produce a FAIL status: {damaged_statuses}"
            )
        if damaged_statuses == ["PASS"] * 4:
            raise AssertionError("damaged image was incorrectly reported as fully PASS")

        print("Synthetic integration test PASS")
        print("clean statuses:", statuses)
        print("damaged statuses:", damaged_statuses)


if __name__ == "__main__":
    main()
