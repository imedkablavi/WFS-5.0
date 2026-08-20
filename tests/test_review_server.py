import importlib.util, json, pathlib, tempfile, threading, unittest, urllib.request, urllib.error
from http.server import ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('review_server', ROOT/'review_server.py')
rv = importlib.util.module_from_spec(spec); spec.loader.exec_module(rv)

class ReviewTests(unittest.TestCase):
    def make_root(self):
        td=tempfile.TemporaryDirectory(); root=pathlib.Path(td.name); (root/'video').mkdir(); f=root/'video'/'2026-01-01_09-00_candidate1.mp4'; f.write_bytes(b'x'*1024)
        (root/'manifest.json').write_text(json.dumps({'meta':{'date':'2026-01-01'},'streams':[{'segment':'09-00','slot':1,'status':'PASS','output':str(f),'sha256':'abc','mp4_duration':3600.0,'fps_assumed':15,'packets':54000,'coverage':1.0,'reasons':[]}]}))
        return td,root,f
    def test_path_traversal_rejected(self):
        td,root,_=self.make_root()
        try:
            with self.assertRaises(ValueError): rv.Store(root).safe('../outside.mp4',False)
        finally: td.cleanup()
    def test_delete_disabled_default(self):
        td,root,f=self.make_root()
        try:
            with self.assertRaises(PermissionError): rv.Store(root).delete(f.name)
        finally: td.cleanup()
    def test_keep_blocks_delete_and_audit_survives(self):
        td,root,f=self.make_root()
        try:
            s=rv.Store(root,True); s.decision(f.name,'keep')
            with self.assertRaises(PermissionError): s.delete(f.name)
            s.delete(f.name,expected_size=1024,force=True)
            self.assertFalse(f.exists()); self.assertTrue((root/'manifest.json').exists()); self.assertTrue((root/'review_audit.jsonl').exists()); self.assertEqual(s.snapshot()['items'][0]['decision'],'deleted')
        finally: td.cleanup()
    def test_range_and_token(self):
        td,root,f=self.make_root()
        try:
            s=rv.Store(root); token='test-token'; httpd=ThreadingHTTPServer(('127.0.0.1',0),rv.handler_factory(s,token)); th=threading.Thread(target=httpd.serve_forever,daemon=True); th.start(); port=httpd.server_address[1]
            req=urllib.request.Request(f'http://127.0.0.1:{port}/media/{f.name}?token={token}',headers={'Range':'bytes=10-19'})
            with urllib.request.urlopen(req,timeout=5) as r: self.assertEqual(r.status,206); self.assertEqual(len(r.read()),10)
            with self.assertRaises(urllib.error.HTTPError) as cm: urllib.request.urlopen(f'http://127.0.0.1:{port}/api/items',timeout=5)
            self.assertEqual(cm.exception.code,401); httpd.shutdown(); httpd.server_close()
        finally: td.cleanup()

if __name__=='__main__': unittest.main()
