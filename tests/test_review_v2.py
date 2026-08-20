import importlib.util
import json
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("review_server_v2", ROOT / "review_server_v2.py")
rv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rv)


class Tests(unittest.TestCase):
    def root(self, hours=("11-00", "12-00"), per_hour=4):
        td = tempfile.TemporaryDirectory()
        root = pathlib.Path(td.name)
        (root / "final").mkdir()
        rows = []
        files = {}
        for hour in hours:
            for stream in range(1, per_hour + 1):
                f = root / "final" / f"2026-08-08_{hour}_stream{stream}_OK.mp4"
                f.write_bytes((f"{hour}-{stream}".encode()) * 20)
                files[(hour, stream)] = f
                rows.append({
                    "time": hour.replace("-", ":"),
                    "stream": stream,
                    "flags": "OK",
                    "file": str(f),
                    "duration": 3600.0,
                    "fps": 15,
                })
        (root / "manifest.json").write_text(json.dumps(rows))
        return td, root, files

    def test_legacy_metadata(self):
        td, root, files = self.root(hours=("11-00",), per_hour=1)
        try:
            s = rv.Store(root)
            x = s.snapshot()["items"][0]
            self.assertEqual(s.video, root / "final")
            self.assertEqual(x["status"], "PASS")
            self.assertEqual(x["segment"], "11-00")
            self.assertEqual(x["slot"], "1")
        finally:
            td.cleanup()

    def test_checkbox_keep_persists_and_unselects(self):
        td, root, files = self.root(hours=("11-00",), per_hour=1)
        try:
            s = rv.Store(root)
            name = files[("11-00", 1)].name
            s.set_keep(name, True)
            self.assertEqual(s.snapshot()["items"][0]["decision"], "keep")
            s.set_keep(name, False)
            self.assertEqual(s.snapshot()["items"][0]["decision"], "unreviewed")
        finally:
            td.cleanup()

    def test_plan_deletes_only_unselected_in_hours_with_keeper(self):
        td, root, files = self.root()
        try:
            s = rv.Store(root, True)
            s.set_keep(files[("11-00", 2)].name, True)
            plan = s.delete_unselected_plan(False)
            accepted = {x["path"] for x in plan["accepted"]}
            self.assertEqual(plan["keep_count"], 1)
            self.assertEqual(plan["count"], 3)
            self.assertIn("12-00", plan["protected_segments"])
            self.assertNotIn(files[("11-00", 2)].name, accepted)
            self.assertIn(files[("11-00", 1)].name, accepted)
        finally:
            td.cleanup()

    def test_empty_hour_requires_explicit_opt_in(self):
        td, root, files = self.root()
        try:
            s = rv.Store(root, True)
            s.set_keep(files[("11-00", 2)].name, True)
            safe = s.delete_unselected_plan(False)
            wide = s.delete_unselected_plan(True)
            self.assertEqual(safe["count"], 3)
            self.assertEqual(wide["count"], 7)
            self.assertEqual(wide["protected_segments"], [])
        finally:
            td.cleanup()

    def test_plan_token_detects_selection_change(self):
        td, root, files = self.root(hours=("11-00",), per_hour=4)
        try:
            s = rv.Store(root, True)
            s.set_keep(files[("11-00", 2)].name, True)
            plan = s.delete_unselected_plan(False)
            s.set_keep(files[("11-00", 3)].name, True)
            with self.assertRaises(RuntimeError):
                s.execute_unselected_cleanup(plan["plan_id"], False)
        finally:
            td.cleanup()

    def test_execute_cleanup_preserves_keep(self):
        td, root, files = self.root(hours=("11-00",), per_hour=4)
        try:
            s = rv.Store(root, True)
            keep = files[("11-00", 2)]
            s.set_keep(keep.name, True)
            plan = s.delete_unselected_plan(False)
            out = s.execute_unselected_cleanup(plan["plan_id"], False)
            self.assertEqual(out["deleted"], 3)
            self.assertTrue(keep.exists())
            self.assertEqual(sum(1 for p in (root / "final").glob("*.mp4")), 1)
        finally:
            td.cleanup()

    def test_identity_token(self):
        td, root, files = self.root(hours=("11-00",), per_hour=2)
        try:
            s = rv.Store(root, True)
            s.set_keep(files[("11-00", 2)].name, True)
            plan = s.delete_unselected_plan(False)
            victim = files[("11-00", 1)]
            victim.write_bytes(b"changed")
            with self.assertRaises(RuntimeError):
                s.execute_unselected_cleanup(plan["plan_id"], False)
        finally:
            td.cleanup()

    def test_ui_controls_and_select_keep_workflow(self):
        html = (ROOT / "review_ui_v2.html").read_text()
        for value in (
            'id="back"', 'id="fwd"', 'data-r="2"', 'data-r="5"',
            "ArrowLeft", "ArrowRight", 'id="cleanup"',
            "/api/keep", "/api/delete-unselected-plan",
            "/api/delete-unselected", "KEEP this video",
        ):
            self.assertIn(value, html)


if __name__ == "__main__":
    unittest.main()
