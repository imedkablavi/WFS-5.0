# WFS 0.5 Recovery Toolkit

Linux-first, conservative tooling for recovering CCTV/DVR/NVR video from compatible **WFS 0.5** storage or forensic images.

The project is intended for data-recovery technicians, investigators, repair shops, CCTV owners, researchers, and anyone who is authorized to recover recordings from their own WFS-based media.

It is **not tied to one DVR model, one disk, one date, or one recovery case**.

> WFS is proprietary and vendor implementations differ. This project uses a tested recovery profile and heuristics; it cannot guarantee compatibility with every WFS implementation or reconstruct bytes that were physically overwritten.

## Safety first

The recovery engine opens the source read-only.

Recommended workflow:

1. Do not format, initialize, repair, mount read/write, or run `fsck` on the original DVR disk.
2. Prefer a forensic image or GNU `ddrescue` clone.
3. Keep the original source read-only.
4. Write recovered files to another disk.
5. Preserve the manifest and hashes produced by the tool.

If a Linux block device is used directly, the program checks `/sys/class/block/<device>/ro` and refuses an unconfirmed writable device unless the operator explicitly overrides that protection.

## Current WFS profile

The current parser is designed around a WFS 0.5 layout with:

- 2 MiB fragments
- record sync `00 00 01`
- record types including `FD`, `FE`, `FC`, `FA`, and `F9`
- WFS-style timestamps in `FD`/`FE`
- HEVC/H.265 video payloads
- fragmented and interleaved camera streams
- nominal packet/frame-rate choices configurable from the command line

These are profile assumptions, not a universal specification of every WFS product.

## Requirements

- Linux
- Python 3.10+
- FFmpeg
- ffprobe

Ubuntu/Debian:

```bash
sudo apt update
sudo apt install -y python3 ffmpeg
```

No third-party Python packages are required.

## Install

```bash
git clone https://github.com/imedkablavi/WFS-5.0.git
cd WFS-5.0

python3 -m py_compile wfs_recover.py
python3 -m unittest discover -s tests -v
python3 wfs_recover.py --help
```

## Scan a source

```bash
python3 wfs_recover.py scan \
  --raw /path/to/source.raw \
  --date 2026-01-15 \
  --out ./scan-output
```

The scanner reports recording boundaries, start fragments, and an inferred camera/start count.

## Recover recordings

```bash
python3 -u wfs_recover.py recover \
  --raw /path/to/source.raw \
  --date 2026-01-15 \
  --out ./recovered \
  --work ./work \
  --auto-retry \
  --deep-verify \
  --thumbs
```

Important options:

```text
--cameras auto
--fps-choices 15,25
--segments 09-00,10-00,11-00
--strategy conservative|balanced|wide
--auto-retry
--deep-verify
--resume
--hash-source
--last-duration 3600
--keep-attempts
```

Example for a recorder using 12/15/20/25 fps candidates:

```bash
python3 -u wfs_recover.py recover \
  --raw /path/to/source.raw \
  --date 2026-01-15 \
  --out ./recovered \
  --work ./work \
  --fps-choices 12,15,20,25 \
  --auto-retry \
  --deep-verify
```

## Why `--deep-verify` matters

The tool intentionally does **not** award `PASS` when full decode verification was skipped.

Without `--deep-verify`, a technically promising result is marked `REVIEW` with:

```text
deep_verify_not_run
```

This fail-closed behavior is deliberate.

## Recovery statuses

### PASS

The candidate passed the configured structural, packet-rate, duration, ambiguity, scene, and full-decode checks.

`PASS` means the reconstruction passed technical QC. It does **not** prove the physical camera number.

### REVIEW

A playable candidate was produced but one or more checks are ambiguous, incomplete, or were not run.

### FAIL

The reconstruction has strong evidence of being incomplete or invalid, such as a large packet-rate mismatch, duration mismatch, or decoder failure.

## Camera identity

The program writes neutral names:

```text
candidate1
candidate2
candidate3
candidate4
```

It does not assume that slot order equals physical camera order.

This is important because:

- camera order can change between recording boundaries
- several cameras can share identical VPS/SPS/PPS
- bitrate and frame rate are not reliable camera IDs
- a single visual fingerprint can be misleading

Physical camera names should be assigned only after multiple visual anchors or reliable channel metadata confirm identity.

## Reconstruction strategy

For each recording boundary the program:

1. initializes all camera candidates together
2. evaluates structurally valid continuation fragments
3. prevents one physical fragment from being assigned to two streams in the same reconstruction
4. tries configurable search radii
5. reconstructs HEVC
6. remuxes to MP4
7. checks packet rate and duration
8. optionally performs a complete FFmpeg decode
9. samples scenes for abrupt continuity changes
10. selects the least-problematic strategy
11. exports manifests, hashes, thumbnails, logs, and HTML

The current engine uses conservative multi-stream assignment. A future major version is planned to use a larger weighted graph/beam-search solver across ambiguous fragment paths.

## Why no single metric is trusted

A correct duration does not prove a correct camera chain.

A correct packet count does not prove ordering.

Identical VPS/SPS/PPS can be shared by multiple cameras.

Physical proximity alone can select the wrong continuation.

For that reason the program combines several independent signals and marks uncertainty instead of silently guessing.

## Output

```text
recovered/
├── video/
│   ├── 2026-01-15_09-00_candidate1.mp4
│   └── ...
├── thumbs/
├── logs/
├── manifest.json
├── manifest.csv
└── index.html
```

Rebuild the HTML report:

```bash
python3 wfs_recover.py report --out ./recovered
```

Hash any file:

```bash
python3 wfs_recover.py hash /path/to/file
```

## Resume

Interrupted jobs can be resumed:

```bash
python3 -u wfs_recover.py recover \
  --raw /path/to/source.raw \
  --date 2026-01-15 \
  --out ./recovered \
  --work ./work \
  --auto-retry \
  --deep-verify \
  --resume
```

Existing completed candidate outputs are skipped.

## FAT32 / USB media

FAT32/vfat has an individual file-size limit of about 4 GiB.

The tool detects FAT32/vfat output. If a recovered file is already too large to copy safely, the final copy is refused and the recovered candidate is preserved in the work area instead of silently losing it.

For DVR recovery, shorter time segments are usually safer when the destination is FAT32.

## Important limitations

The software cannot reconstruct information that no longer exists.

Examples:

- overwritten WFS descriptors
- overwritten video fragments
- unreadable sectors absent from the forensic image
- vendor-specific fields not implemented by the current profile
- a continuation that is genuinely indistinguishable after metadata loss

The software also cannot guarantee that a technically valid candidate belongs to a particular physical camera unless sufficient channel or visual evidence exists.

## Testing

The repository includes unit tests for parser/QC logic.

The current release was also exercised against synthetic interleaved WFS-style images with:

- four simultaneous streams
- fragmented WFS records crossing 2 MiB boundaries
- valid HEVC/H.265 payloads
- 15 fps packet counts
- interleaved physical fragments
- a deliberately missing/corrupted continuation fragment
- scan, recover, deep verification, report, hash, and resume workflows

A clean synthetic image produced four `PASS` candidates. The deliberately damaged image produced a `FAIL` for the truncated stream and `REVIEW` for affected neighboring uncertainty rather than silently reporting success.

Real-world WFS devices can still differ, so test on an image before relying on results.

## Legal / ethical use

Use this software only on media you own or are authorized to examine.

It is intended for legitimate data recovery, incident response, repair, research, and forensic analysis.

See [PROJECT.md](PROJECT.md) for architecture, failure modes, and the roadmap.
