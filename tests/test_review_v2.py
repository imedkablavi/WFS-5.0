import importlib.util,json,pathlib,tempfile,unittest
ROOT=pathlib.Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("review_server_v2",ROOT/"review_server_v2.py"); rv=importlib.util.module_from_spec(spec); spec.loader.exec_module(rv)

class Tests(unittest.TestCase):
 def root(self):
  td=tempfile.TemporaryDirectory(); r=pathlib.Path(td.name); (r/"final").mkdir(); f=r/"final"/"2026-08-08_11-00_stream4_REVIEW.mp4"; f.write_bytes(b"x"*100)
  (r/"manifest.json").write_text(json.dumps([{"time":"11:00","stream":4,"flags":"DURATION_MISMATCH","file":str(f),"duration":3441.1,"fps":15}]))
  return td,r,f
 def test_legacy_metadata(self):
  td,r,f=self.root()
  try:
   s=rv.Store(r); x=s.snapshot()["items"][0]; self.assertEqual(s.video,r/"final"); self.assertEqual(x["status"],"REVIEW"); self.assertEqual(x["segment"],"11-00"); self.assertEqual(x["slot"],"4")
  finally:td.cleanup()
 def test_delete_plan_keep_and_discard_policy(self):
  td,r,f=self.root()
  try:
   s=rv.Store(r,True,delete_policy="discard-only"); s.decision(f.name,"keep"); self.assertEqual(s.delete_plan([{"path":f.name}])["count"],0)
   s.decision(f.name,"discard"); self.assertEqual(s.delete_plan([{"path":f.name}])["count"],1)
  finally:td.cleanup()
 def test_identity_token(self):
  td,r,f=self.root()
  try:
   s=rv.Store(r,True); tok=s._token(f)
   with self.assertRaises(RuntimeError): s.delete(f.name,expected_token="bad")
   s.delete(f.name,expected_token=tok,expected_size=100); self.assertFalse(f.exists())
  finally:td.cleanup()
 def test_ui_controls(self):
  h=(ROOT/"review_ui_v2.html").read_text()
  for x in ('id="back"','id="fwd"','data-r="2"','data-r="5"',"ArrowLeft","ArrowRight","delete-bulk"): self.assertIn(x,h)

if __name__=="__main__":unittest.main()
