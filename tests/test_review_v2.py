import importlib.util, json, pathlib, tempfile, unittest
ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("review_server_v2", ROOT / "review_server_v2.py")
rv = importlib.util.module_from_spec(spec); spec.loader.exec_module(rv)


class Tests(unittest.TestCase):
    def root(self):
        td = tempfile.TemporaryDirectory()
        r = pathlib.Path(td.name)
        (r / "final").mkdir()
        files = []
        manifest = []
        for i, cam in enumerate(("A", "B", "C", "4"), 1):
            f = r / "final" / f"2026-08-08_11-00_CAM_{cam}.mp4"
            f.write_bytes(bytes([i]) * (100 + i))
            files.append(f)
            manifest.append({"time": "11:00", "stream": i, "flags": "OK", "file": str(f), "duration": 3600, "fps": 15})
        (r / "manifest.json").write_text(json.dumps(manifest))
        return td, r, files

    def test_legacy_metadata(self):
        td, r, files = self.root()
        try:
            s = rv.Store(r)
            x = s.snapshot()["items"][0]
            self.assertEqual(s.video, r / "final")
            self.assertEqual(x["segment"], "11-00")
            self.assertTrue(x["slot"])
        finally:
            td.cleanup()

    def test_keep_selection_persists(self):
        td, r, files = self.root()
        try:
            s = rv.Store(r)
            s.set_keep(files[1].name, True)
            items = {x["name"]: x for x in s.snapshot()["items"]}
            self.assertEqual(items[files[1].name]["decision"], "keep")
            s.set_keep(files[1].name, False)
            items = {x["name"]: x for x in s.snapshot()["items"]}
            self.assertNotEqual(items[files[1].name]["decision"], "keep")
        finally:
            td.cleanup()

    def test_cleanup_requires_keeper_by_default(self):
        td, r, files = self.root()
        try:
            s = rv.Store(r, True)
            p = s.delete_unselected_plan(False)
            self.assertEqual(p["count"], 0)
            self.assertIn("11-00", p["protected_segments"])
            s.set_keep(files[1].name, True)
            p = s.delete_unselected_plan(False)
            self.assertEqual(p["keep_count"], 1)
            self.assertEqual(p["count"], 3)
        finally:
            td.cleanup()

    def test_cleanup_deletes_only_unselected(self):
        td, r, files = self.root()
        try:
            s = rv.Store(r, True)
            s.set_keep(files[2].name, True)
            plan = s.delete_unselected_plan(False)
            out = s.execute_unselected_cleanup(plan["plan_id"], False)
            self.assertEqual(out["deleted"], 3)
            self.assertTrue(files[2].exists())
            self.assertEqual(sum(f.exists() for f in files), 1)
        finally:
            td.cleanup()

    def test_cleanup_plan_invalidates_after_selection_change(self):
        td, r, files = self.root()
        try:
            s = rv.Store(r, True)
            s.set_keep(files[0].name, True)
            plan = s.delete_unselected_plan(False)
            s.set_keep(files[1].name, True)
            with self.assertRaises(RuntimeError):
                s.execute_unselected_cleanup(plan["plan_id"], False)
        finally:
            td.cleanup()

    def test_identity_token_blocks_changed_file(self):
        td, r, files = self.root()
        try:
            s = rv.Store(r, True, delete_policy="anything-except-keep")
            tok = s._token(files[0])
            files[0].write_bytes(b"changed")
            with self.assertRaises(RuntimeError):
                s.delete(files[0].name, expected_token=tok)
        finally:
            td.cleanup()

    def test_direct_delete_disabled_in_selection_policy(self):
        td, r, files = self.root()
        try:
            s = rv.Store(r, True)
            with self.assertRaises(PermissionError):
                s.delete(files[0].name)
        finally:
            td.cleanup()

    def test_ui_player_controls_and_cleanup(self):
        h = (ROOT / "review_ui_v2.html").read_text()
        for needle in (
            'id="back"', 'id="fwd"', 'id="seekbar"',
            'data-r="0.5"', 'data-r="2"', 'data-r="5"',
            'id="fullscreen"', 'requestFullscreen', 'id="reloadPos"',
            "ArrowLeft", "ArrowRight", "delete-unselected",
            'id="playerKeep"',
        ):
            self.assertIn(needle, h)


if __name__ == "__main__":
    unittest.main()
