import importlib.util, json, pathlib, tempfile, unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("review_server_v3", ROOT / "review_server_v3.py")
rv = importlib.util.module_from_spec(spec); spec.loader.exec_module(rv)


class ReviewV3Tests(unittest.TestCase):
    def make_root(self):
        td = tempfile.TemporaryDirectory(); root = pathlib.Path(td.name); final = root / "final"; final.mkdir()
        names = [
            "2026-08-08_09-00_CAM_A.mp4",
            "2026-08-08_09-00_CAM_B.mp4",
            "2026-08-08_09-00_CAM_C.mp4",
            "2026-08-08_09-00_CAM4.mp4",
            "2026-08-08_10-00_CAM_A.mp4",
            "2026-08-08_10-00_CAM_B.mp4",
            "2026-08-08_10-00_CAM_C.mp4",
            "2026-08-08_10-00_CAM4.mp4",
        ]
        for name in names: (final / name).write_bytes(b"x" * 100)
        # Deliberately wrong legacy metadata hour for first file: filename must win and conflict must surface.
        manifest = [{"time": "11:00", "file": str(final / names[0]), "flags": "OK", "duration": 3600, "stream": 1}]
        (root / "manifest.json").write_text(json.dumps(manifest))
        return td, root, final

    def test_filename_hour_wins_and_conflict_is_visible(self):
        td, root, _ = self.make_root()
        try:
            s = rv.Store(root)
            x = next(i for i in s.snapshot()["items"] if i["name"].endswith("CAM_A.mp4") and i["hour"] == "09-00")
            self.assertEqual(x["hour_source"], "filename")
            self.assertTrue(x["hour_conflict"])
            self.assertEqual(x["health"], "FAIL")
        finally: td.cleanup()

    def test_expected_count_mode_and_camera_labels(self):
        td, root, _ = self.make_root()
        try:
            snap = rv.Store(root).snapshot()
            self.assertEqual(snap["expected_per_hour"], 4)
            hour = next(h for h in snap["hours"] if h["hour"] == "10-00")
            self.assertEqual(hour["live_count"], 4)
            cams = {x["camera"] for x in snap["items"] if x["hour"] == "10-00" and x["exists"]}
            self.assertIn("CAM A", cams); self.assertIn("CAM 4", cams)
        finally: td.cleanup()

    def test_cleanup_requires_keep_and_blocks_hour_conflict(self):
        td, root, final = self.make_root()
        try:
            s = rv.Store(root, allow_delete=True)
            # 10:00 has no conflict but initially no keeper -> protected.
            p = s.cleanup_plan("10-00")
            self.assertEqual(p["count"], 0); self.assertTrue(p["blocked"])
            keep = "2026-08-08_10-00_CAM_B.mp4"
            s.set_keep(keep, True)
            p = s.cleanup_plan("10-00")
            self.assertEqual(p["keep_count"], 1); self.assertEqual(p["count"], 3)
            self.assertFalse(any(x["path"] == keep for x in p["accepted"]))
            # 09:00 remains blocked because filename/manifest conflict exists.
            s.set_keep("2026-08-08_09-00_CAM_B.mp4", True)
            p9 = s.cleanup_plan("09-00")
            self.assertEqual(p9["count"], 0)
            self.assertEqual(p9["blocked"][0]["reason"], "hour metadata conflict")
        finally: td.cleanup()

    def test_plan_changes_after_keep_change(self):
        td, root, _ = self.make_root()
        try:
            s = rv.Store(root, allow_delete=True)
            s.set_keep("2026-08-08_10-00_CAM_A.mp4", True)
            p1 = s.cleanup_plan("10-00")
            s.set_keep("2026-08-08_10-00_CAM_B.mp4", True)
            p2 = s.cleanup_plan("10-00")
            self.assertNotEqual(p1["plan_id"], p2["plan_id"])
            with self.assertRaises(RuntimeError): s.execute_cleanup(p1["plan_id"], "10-00")
        finally: td.cleanup()

    def test_ui_has_sticky_preview_and_hour_filters(self):
        html = (ROOT / "review_ui_v4.html").read_text()
        for needle in (
            'id="hour"', 'id="camera"', 'id="previewPane"', 'position:sticky',
            'id="probeHour"', 'id="cleanupHour"', 'id="cleanupAll"',
            'id="deepProbe"', 'data-r="2"', 'data-r="5"', '↶ 5s', '5s ↷',
        ):
            self.assertIn(needle, html)
        self.assertNotIn("scrollTo({top:0", html)


if __name__ == "__main__": unittest.main()
