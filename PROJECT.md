# Project architecture and recovery model

## Purpose

WFS 0.5 Recovery Toolkit treats proprietary CCTV recovery as a reconstruction problem rather than ordinary filesystem undelete.

A DVR can record several cameras at the same time. Video fragments can be interleaved, metadata can be overwritten, timestamps can jump, and several cameras can use identical encoder settings. A normal file carver can find H.265 bytes without knowing which camera or timeline they belong to.

The project therefore separates:

1. source validation
2. recording-boundary discovery
3. multi-stream fragment reconstruction
4. HEVC extraction
5. technical validation
6. camera-identity review
7. reporting and reproducibility

## Source model

The source can be a RAW forensic image or a read-only Linux block device.

The program does not mount or repair the WFS source and opens it with `O_RDONLY`.

For a block device it checks the Linux read-only flag before processing.

For best forensic practice, create a clone/image first with a recovery-oriented imager such as GNU ddrescue and preserve its map file.

## Discovery

The current profile scans 2 MiB fragment boundaries for an `FD` recording-start record and decodes its WFS timestamp.

Starts with the same hour/minute are grouped into one recording boundary.

Camera count can be supplied explicitly or inferred from the statistical mode of start counts across the scanned timeline.

## Packet model

Current known record handling includes:

```text
00 00 01 FD
00 00 01 FE
00 00 01 FC
00 00 01 FA
00 00 01 F9
```

`FD` and `FE` use a 16-byte header in the current profile.

`FC`, `FA`, and `F9` use 8-byte headers with profile-specific length fields.

HEVC payload is extracted from video-bearing records.

## Padding

Long `00` or `FF` regions can indicate unused space at the end of a WFS fragment.

A very short run is not sufficient evidence because compressed video can naturally contain repeated bytes.

The parser therefore checks padding only at an expected record boundary and requires a longer run.

## Fragment continuation

A record can cross a physical WFS fragment boundary.

The recovery engine carries the incomplete tail and tests potential later fragments. A candidate is accepted only when the carried record can finish and the byte position immediately after it contains another valid WFS record or valid padding.

Physical distance is used only after structural validation.

## Multi-stream assignment

Camera candidates are advanced together.

A physical fragment is not allowed to belong to two candidate streams in the same reconstruction.

If several continuations are structurally valid, the step is recorded as ambiguous. A technically perfect-looking output with ambiguous reconstruction is not silently treated as unquestionable.

The current algorithm is bounded and conservative rather than a complete global graph solver.

## Planned graph solver

A future version should represent candidate continuations as a weighted graph.

Possible edge evidence:

- exact carried-packet completion
- record synchronization after the join
- HEVC NAL validity
- parameter-set compatibility
- timestamp clues
- channel metadata when available
- physical gap
- decoder continuity
- packet-rate consistency
- neighboring-segment visual continuity

The optimizer should solve camera paths jointly with mutual exclusion so one fragment cannot be reused by multiple cameras.

## Quality control

No single measurement determines success.

### Segment allocation coverage

The tool records what fraction of the physical segment span was assigned to reconstructed chains.

This is a **segment-level** measurement.

Low allocation coverage does not automatically mean every camera is damaged. One missing fragment may affect only one camera, so low segment coverage forces uncertainty but individual failure is determined using additional evidence.

### Packet rate

For an expected time interval:

```text
packet_rate = video_packet_count / expected_seconds
```

The nearest configured nominal rate is selected.

Defaults are `15,25`, but users can change them:

```bash
--fps-choices 10,12,15,20,25,30
```

This is still a heuristic. Some DVR formats may place more than one frame in a WFS record or split one frame across several logical video records.

### Duration

Recovered HEVC is remuxed with the selected nominal rate and measured with ffprobe.

Duration is useful but not sufficient: a mixed camera stream can still be exactly one hour long.

### Ambiguity

When multiple continuation fragments are structurally possible, the event is recorded.

Ambiguity forces human review even if duration and packet count look normal.

### Full decode

`--deep-verify` decodes the complete recovered MP4 with FFmpeg.

Actual decoder errors are treated more seriously than null-muxer/timestamp warnings.

A run without full decode cannot receive `PASS`.

### Scene sampling

Periodic dHash scene samples are used to detect large visual discontinuities.

This is only a heuristic. Lighting changes, headlights, night mode, camera motion, or a busy scene can create false positives.

## Failure modes

The project is designed to recognize or conservatively handle:

| Condition | Detection / behavior |
|---|---|
| long 00/FF padding | terminate only at record boundary with sufficiently long padding |
| false short padding pattern | do not treat a 4-byte run as terminal |
| missing fragment | unresolved chain, rate/duration mismatch, low allocation coverage |
| overwritten fragment | structural continuation failure or decode/QC failure |
| duplicate candidate | mutual exclusion prevents fragment reuse within one reconstruction |
| out-of-order fragment | adaptive forward search |
| stale data | structure/QC may reject; otherwise mark ambiguous/review |
| unreadable source sector | should be handled first by imaging/ddrescue |
| camera reboot | new recording boundary or discontinuity; review |
| recorder clock jump | implausible segment duration is refused automatically |
| non-hour boundary | next timestamp determines expected duration |
| last boundary without successor | configured with `--last-duration` |
| FPS change | choose from configured rates; abnormal rate becomes review/fail |
| identical codec signature | never use codec signature alone as physical camera ID |
| camera slot permutation | neutral candidate names |
| decoder corruption | FAIL when full-decode errors remain |
| FAT32 >4 GiB output | refuse final copy and preserve work candidate |
| command timeout | external tool timeout is converted to a controlled failure instead of crashing the complete run |

## Status policy

### PASS

Requires:

- no hard QC failure
- no reconstruction ambiguity
- no unresolved continuation
- acceptable packet-rate error
- acceptable duration error
- no scene-jump review flags
- complete decode verification performed
- no decoder errors

### REVIEW

Used when recovery produced a candidate but evidence is not sufficient for `PASS`.

Examples:

- full decode not run
- low segment allocation coverage
- ambiguous continuation
- scene discontinuity
- small timing/rate deviation

### FAIL

Used for strong structural or technical inconsistency.

Examples:

- large packet-rate mismatch
- large duration mismatch
- actual decoder errors
- remux failure
- invalid timeline
- final copy failure that cannot be safely resolved

## Recovery strategies

Three search windows are available:

```text
conservative
balanced
wide
```

`--auto-retry` evaluates several strategies and chooses the technically least-problematic reconstruction.

Wide search can find distant fragments but increases the space of plausible wrong continuations. It is therefore not automatically considered more reliable.

## Storage design

Temporary attempts are kept in the work directory, not the final output directory.

This avoids filling removable media with several copies of the same hour during auto-retry.

The tool estimates:

- final selected-output space
- peak scratch/work space

If output and work are on the same filesystem, the estimates are combined.

## Reproducibility

The final manifest records data such as:

- tool version
- source path and size
- optional source SHA-256
- requested date
- camera/start count
- configured FPS candidates
- recovery strategy
- start fragment
- number of assigned fragments
- allocation coverage
- ambiguity/unresolved counts
- WFS video packet count
- chosen rate
- MP4 duration
- decoder results
- scene hashes
- output SHA-256
- QC status and reasons

## Camera mapping roadmap

Physical camera identity should eventually support operator-provided anchors.

Example concept:

```json
{
  "Camera 4": [
    {"date":"2026-01-15","time":"10:00","candidate":4},
    {"date":"2026-01-15","time":"18:00","candidate":2}
  ]
}
```

The mapper could then use several visual anchors and continuity scores instead of trusting one reference frame.

Until then the core recovery tool intentionally preserves neutral candidate numbering.

## Additional WFS profiles

Long-term design should move vendor-specific values into profiles:

```text
profiles/
  wfs05_2m_hevc.json
  vendor_model_variant.json
```

Possible profile fields:

- fragment size
- packet sync
- packet types
- header layouts
- payload length offsets
- timestamp decoder
- channel-ID location
- codec
- padding rules
- nominal rates
- search windows

## Testing policy

Every release should pass:

```bash
python3 -m py_compile wfs_recover.py
python3 -m unittest discover -s tests -v
```

Higher-confidence releases should also run synthetic integration tests containing:

- multiple simultaneous camera streams
- real HEVC payload
- WFS records crossing fragment boundaries
- interleaving
- corrupted/missing fragments
- resume
- report generation
- deep verification

Synthetic tests cannot prove compatibility with unknown commercial WFS variants. They verify the recovery engine against the documented project profile.
