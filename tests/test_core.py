import importlib.util
import pathlib
import struct
import sys
import unittest
from datetime import datetime

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("wfs_recover", ROOT / "wfs_recover.py")
w = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = w
SPEC.loader.exec_module(w)


def encode_ts(year, month, day, hour, minute, second):
    return (
        ((year - 2000) & 0x3F) << 26
        | (month & 0x0F) << 22
        | (day & 0x1F) << 17
        | (hour & 0x1F) << 12
        | (minute & 0x3F) << 6
        | (second & 0x3F)
    )


class CoreTests(unittest.TestCase):
    def test_timestamp_roundtrip(self):
        value = encode_ts(2026, 8, 20, 19, 42, 17)
        self.assertEqual(w.decode_ts(value), datetime(2026, 8, 20, 19, 42, 17))

    def test_packet_info_fc(self):
        payload = b"abc123"
        packet = b"\x00\x00\x01\xfc" + struct.pack("<I", len(payload)) + payload
        status, typ, hdr, size, total = w.packet_info(packet, 0)
        self.assertEqual(status, "ok")
        self.assertEqual(typ, 0xFC)
        self.assertEqual(hdr, 8)
        self.assertEqual(size, len(payload))
        self.assertEqual(total, len(packet))

    def test_padding_requires_long_run(self):
        short = b"\xff\xff\xff\xff" + b"\x01" * 100
        self.assertFalse(w.padding_here(short, 0))
        long_pad = b"\xff" * 128
        self.assertTrue(w.padding_here(long_pad, 0))

    def test_camera_count_mode(self):
        segments = [
            {"frags": [1,2,3,4]},
            {"frags": [5,6,7,8]},
            {"frags": [9,10,11,12]},
            {"frags": [13,14,15]},
        ]
        self.assertEqual(w.infer_camera_count(segments), 4)

    def test_fps_choices(self):
        fps, observed, err = w.choose_fps(900, 60, (12, 15, 25))
        self.assertEqual(fps, 15.0)
        self.assertEqual(observed, 15.0)
        self.assertEqual(err, 0.0)

    def test_pass_requires_deep_verify(self):
        status, reasons, _, _ = w.classify_qc(
            expected=3600,
            coverage=1.0,
            packets=54000,
            fps=15,
            duration=3600,
            ambiguity=0,
            unresolved=0,
            decode_errors=0,
            decode_warnings=0,
            deep_verified=False,
            scene_jumps=0,
        )
        self.assertEqual(status, "REVIEW")
        self.assertIn("deep_verify_not_run", reasons)

    def test_clean_qc_pass(self):
        status, reasons, _, _ = w.classify_qc(
            expected=3600,
            coverage=1.0,
            packets=54000,
            fps=15,
            duration=3600,
            ambiguity=0,
            unresolved=0,
            decode_errors=0,
            decode_warnings=0,
            deep_verified=True,
            scene_jumps=0,
        )
        self.assertEqual(status, "PASS")
        self.assertEqual(reasons, [])

    def test_low_segment_coverage_does_not_fail_every_stream(self):
        status, reasons, _, _ = w.classify_qc(
            expected=3600,
            coverage=0.90,
            packets=54000,
            fps=15,
            duration=3600,
            ambiguity=0,
            unresolved=0,
            decode_errors=0,
            decode_warnings=0,
            deep_verified=True,
            scene_jumps=0,
        )
        self.assertEqual(status, "REVIEW")
        self.assertTrue(any("segment_coverage" in x for x in reasons))

    def test_large_rate_mismatch_fails(self):
        status, reasons, _, _ = w.classify_qc(
            expected=3600,
            coverage=1.0,
            packets=40000,
            fps=15,
            duration=3600,
            ambiguity=0,
            unresolved=0,
            decode_errors=0,
            decode_warnings=0,
            deep_verified=True,
            scene_jumps=0,
        )
        self.assertEqual(status, "FAIL")
        self.assertTrue(any("rate_error" in x for x in reasons))


if __name__ == "__main__":
    unittest.main()
