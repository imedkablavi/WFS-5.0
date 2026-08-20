import importlib.util
import json
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('review_compat', ROOT/'review_compat.py')
rc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rc)


class LegacyReviewTests(unittest.TestCase):
    def test_legacy_list_manifest_and_final_directory(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            final = root / 'final'
            final.mkdir()
            video = final / '2026-08-08_09-00_stream1.mp4'
            video.write_bytes(b'x' * 256)
            (root / 'manifest.json').write_text(json.dumps([
                {
                    'segment': '09-00',
                    'slot': 1,
                    'status': 'OK',
                    'output': str(video),
                }
            ]), encoding='utf-8')

            store = rc.Store(root)
            self.assertEqual(store.video, final.resolve())
            meta, rows = store.manifest()
            self.assertTrue(meta['legacy_manifest'])
            self.assertEqual(len(rows), 1)
            snap = store.snapshot()
            self.assertEqual(len(snap['items']), 1)
            self.assertEqual(snap['items'][0]['name'], video.name)
            self.assertEqual(snap['items'][0]['status'], 'OK')

    def test_current_manifest_still_supported(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            video_dir = root / 'video'
            video_dir.mkdir()
            video = video_dir / 'candidate1.mp4'
            video.write_bytes(b'x')
            (root / 'manifest.json').write_text(json.dumps({
                'meta': {'date': '2026-08-08'},
                'streams': [{'output': str(video), 'status': 'PASS'}],
            }), encoding='utf-8')

            store = rc.Store(root)
            self.assertEqual(store.video, video_dir.resolve())
            snap = store.snapshot()
            self.assertEqual(snap['items'][0]['status'], 'PASS')


if __name__ == '__main__':
    unittest.main()
