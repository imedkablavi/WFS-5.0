#!/usr/bin/env python3
"""Entry point for WFS 0.5 Recovery Toolkit."""
from pathlib import Path
import argparse
_ROOT=Path(__file__).resolve().parent
_PARTS=sorted((_ROOT/"src").glob("wfs_recover_impl.part*"))
if not _PARTS: raise SystemExit("Missing implementation parts under src/")
_code="".join(p.read_text(encoding="utf-8") for p in _PARTS)
_entry='\n\nif __name__ == "__main__":\n    main()\n'
if _entry in _code: _code=_code.rsplit(_entry,1)[0]+"\n"
exec(compile(_code,str(_ROOT/"src"/"wfs_recover_impl.py"),"exec"),globals(),globals())
VERSION="0.8.0"; _old=build_parser

def _review(a):
    from review_server_v2 import serve
    serve(a.root,a.bind,a.port,a.allow_delete,a.token,Path(a.quarantine_dir) if a.quarantine_dir else None,a.delete_policy)

def build_parser():
    ap=_old(); sub=next((x for x in ap._actions if isinstance(x,argparse._SubParsersAction)),None)
    r=sub.add_parser("review",help="professional browser review console")
    r.add_argument("--root",required=True); r.add_argument("--bind",default="127.0.0.1"); r.add_argument("--port",type=int,default=8090)
    r.add_argument("--allow-delete",action="store_true"); r.add_argument("--token"); r.add_argument("--quarantine-dir")
    r.add_argument("--delete-policy",choices=("anything-except-keep","discard-only"),default="anything-except-keep")
    r.set_defaults(func=_review); return ap

if __name__=="__main__": main()
