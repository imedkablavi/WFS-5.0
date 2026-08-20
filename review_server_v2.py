#!/usr/bin/env python3
"""WFS Review Console v0.8: safer deletion and investigator-friendly playback."""
from __future__ import annotations
import json, mimetypes, os, re, secrets, shutil, socket
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse
from review_compat import Store as CompatStore
from review_server import VIDEO_SUFFIXES

VERSION="0.8.0"; MAX_BODY=2*1024*1024; COOKIE="wfs_review"; POLICIES={"anything-except-keep","discard-only"}

def _first(r, keys, default=""):
    for k in keys:
        if r.get(k) not in (None,""): return r[k]
    return default

def _segment(v):
    s=str(v or ""); m=re.search(r"(?<!\d)([01]\d|2[0-3])[:\-]([0-5]\d)(?!\d)",s)
    return f"{m.group(1)}-{m.group(2)}" if m else s

def _reasons(v):
    if v in (None,"",[],()): return []
    if isinstance(v,list): return [str(x) for x in v]
    s=str(v).strip()
    if not s or s.upper()=="OK": return []
    return [x.strip() for x in s.split(",")] if "," in s else [s]

def _norm(row,name):
    row=row if isinstance(row,dict) else {}
    seg=_segment(_first(row,("segment","label","time","hour","start_time"),name))
    slot=_first(row,("slot","stream","candidate","camera","channel"),"")
    if slot=="":
        m=re.search(r"(?:candidate|stream|slot|cam(?:era)?)[_\- ]*0*(\d+)",name,re.I); slot=m.group(1) if m else ""
    flags=_first(row,("reasons","flags","flag"),[]); rs=_reasons(flags)
    raw=str(_first(row,("status","qc_status","result"),"")).upper()
    status={"OK":"PASS","PASS":"PASS","PASSED":"PASS","WARN":"REVIEW","WARNING":"REVIEW","REVIEW":"REVIEW","FAIL":"FAIL","FAILED":"FAIL","ERROR":"FAIL"}.get(raw)
    if not status: status="REVIEW" if rs else ("PASS" if str(flags).upper()=="OK" else "UNTRACKED")
    return dict(status=status,segment=seg,slot=str(slot),fps=_first(row,("fps_assumed","fps","frame_rate")),duration=_first(row,("mp4_duration","duration","dur")),packets=_first(row,("packets","video_packets","packet_count")),coverage=_first(row,("coverage","segment_coverage")),strategy=_first(row,("strategy","method")),reasons=rs,sha256=_first(row,("sha256","hash","checksum")))

class Store(CompatStore):
    def __init__(self,root,allow_delete=False,quarantine=None,delete_policy="anything-except-keep"):
        super().__init__(root,allow_delete=allow_delete,quarantine=quarantine)
        if delete_policy not in POLICIES: raise SystemExit("invalid delete policy")
        self.delete_policy=delete_policy
        if self.quarantine:
            try: self.quarantine.relative_to(self.video)
            except ValueError: pass
            else: raise SystemExit("quarantine directory cannot be inside recovered video directory")
    def _token(self,p):
        s=p.stat(); return f"{s.st_size}:{s.st_mtime_ns}:{getattr(s,'st_ino',0)}"
    def snapshot(self):
        meta,rows=self.manifest(); mmap=self.manifest_map(); items=[]; seen=set()
        with self.lock:
            for p in sorted(self.video.rglob("*")):
                if not p.is_file() or p.is_symlink() or p.suffix.lower() not in VIDEO_SUFFIXES: continue
                rel=p.relative_to(self.video).as_posix(); seen.add(rel); st=self.state["items"].get(rel,{})
                row=mmap.get(p.name,{}); n=_norm(row,p.name); ps=p.stat()
                items.append(dict(path=rel,name=p.name,exists=True,size=ps.st_size,file_token=self._token(p),decision=st.get("decision","unreviewed"),note=st.get("note",""),deleted_at=st.get("deleted_at"),manifest_matched=bool(row),**n))
            for rel,st in self.state["items"].items():
                if rel in seen or st.get("decision")!="deleted": continue
                n=_norm(mmap.get(Path(rel).name,{}),Path(rel).name); n["sha256"]=st.get("sha256") or n["sha256"]
                items.append(dict(path=rel,name=Path(rel).name,exists=False,size=st.get("size_before",0),file_token=st.get("file_token_before",""),decision="deleted",note=st.get("note",""),deleted_at=st.get("deleted_at"),manifest_matched=bool(mmap.get(Path(rel).name)),**n))
            counts={}; qc={}; live=discard=0
            for x in items:
                counts[x["decision"]]=counts.get(x["decision"],0)+1; qc[x["status"]]=qc.get(x["status"],0)+1
                if x["exists"]:
                    live+=x["size"]; discard+=x["size"] if x["decision"]=="discard" else 0
            d=shutil.disk_usage(self.root); reviewed=sum(counts.get(x,0) for x in ("keep","review","discard","deleted"))
            return dict(version=VERSION,root=str(self.root),video_root=str(self.video),delete_enabled=self.allow_delete,delete_policy=self.delete_policy,quarantine=str(self.quarantine) if self.quarantine else None,manifest_meta=meta,disk=dict(total=d.total,used=d.used,free=d.free),video_bytes=live,discard_bytes=discard,progress=dict(reviewed=reviewed,total=len(items),percent=round(reviewed/max(1,len(items))*100,1)),counts=counts,qc_counts=qc,items=items)
    def decision(self,rel,value,note="",client=None):
        self.safe(rel,True); return super().decision(rel,value,note,client)
    def delete_plan(self,entries):
        out=[]; protected=[]; missing=[]; total=0
        for e in entries[:500] if isinstance(entries,list) else []:
            rel=e.get("path") if isinstance(e,dict) else e
            try: p=self.safe(rel,True)
            except FileNotFoundError: missing.append(str(rel)); continue
            st=self.state["items"].get(p.relative_to(self.video).as_posix(),{}); dec=st.get("decision","unreviewed")
            if dec=="keep" or (self.delete_policy=="discard-only" and dec!="discard"): protected.append(str(rel)); continue
            total+=p.stat().st_size; out.append(str(rel))
        return dict(accepted=out,count=len(out),bytes=total,protected=protected,missing=missing,policy=self.delete_policy)
    def delete(self,rel,expected_size=None,expected_token=None,force=False,client=None):
        if not self.allow_delete: raise PermissionError("deletion disabled; restart with --allow-delete")
        p=self.safe(rel,True); key=p.relative_to(self.video).as_posix(); st=self.state["items"].setdefault(key,{})
        dec=st.get("decision","unreviewed")
        if dec=="keep" and not force: raise PermissionError("file is marked KEEP")
        if self.delete_policy=="discard-only" and dec!="discard" and not force: raise PermissionError("mark file DISCARD before deleting")
        token=self._token(p)
        if expected_token and expected_token!=token: raise RuntimeError("file changed since page load; refresh first")
        if expected_size is not None and int(expected_size)!=p.stat().st_size: raise RuntimeError("file size changed; refresh first")
        size=p.stat().st_size; before=shutil.disk_usage(self.root).free
        result=super().delete(rel,expected_size=size,force=force,client=client)
        st=self.state["items"].setdefault(key,{}); st["file_token_before"]=token; self.save(); after=shutil.disk_usage(self.root).free
        result.update(free_before=before,free_after=after); return result
    def bulk_delete(self,entries,client=None):
        results=[]; errors=[]
        for e in entries[:500] if isinstance(entries,list) else []:
            if not isinstance(e,dict): errors.append(dict(path="",error="invalid entry")); continue
            try: results.append(self.delete(e.get("path"),e.get("expected_size"),e.get("expected_token"),False,client))
            except Exception as exc: errors.append(dict(path=str(e.get("path","")),error=str(exc)))
        self.audit("bulk_delete_summary",requested=len(entries),deleted=len(results),failed=len(errors),bytes_removed=sum(x["bytes_removed"] for x in results),client=client)
        return dict(requested=len(entries),deleted=len(results),failed=len(errors),bytes_removed=sum(x["bytes_removed"] for x in results),results=results,errors=errors)

def _cookie(h):
    try:
        c=cookies.SimpleCookie(); c.load(h or ""); return c[COOKIE].value if COOKIE in c else ""
    except Exception:return ""

def _range(h,size):
    if not h or not h.startswith("bytes=") or not size:return 0,max(0,size-1),200
    a,b=h[6:].split(",",1)[0].strip().split("-",1)
    if a:start=int(a); end=int(b) if b else size-1
    else:
        n=int(b); start=max(0,size-n); end=size-1
    if start<0 or end<start or start>=size:raise ValueError
    return start,min(end,size-1),206

def handler_factory(store,token):
    class H(BaseHTTPRequestHandler):
        server_version="WFSReview/0.8"; protocol_version="HTTP/1.1"
        def log_message(self,fmt,*args): print(f"{self.client_address[0]} {self.command} {urlparse(self.path).path} - {fmt%args}")
        def sec(self):
            self.send_header("X-Content-Type-Options","nosniff"); self.send_header("X-Frame-Options","DENY"); self.send_header("Referrer-Policy","no-referrer"); self.send_header("Cache-Control","no-store")
            self.send_header("Content-Security-Policy","default-src 'self'; media-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        def auth(self):
            q=(parse_qs(urlparse(self.path).query).get("token") or [""])[0]; h=self.headers.get("X-WFS-Token") or ""; c=_cookie(self.headers.get("Cookie")); s=q or h or c
            return ("query" if q else "header" if h else "cookie") if s and secrets.compare_digest(s,token) else ""
        def sendj(self,o,code=200):
            raw=json.dumps(o,ensure_ascii=False).encode(); self.send_response(code); self.sec(); self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(raw))); self.end_headers()
            try:self.wfile.write(raw)
            except (BrokenPipeError,ConnectionResetError):pass
        def deny(self):
            raw=b"Unauthorized\n"; self.send_response(401); self.sec(); self.send_header("Content-Length",str(len(raw))); self.end_headers(); self.wfile.write(raw)
        def body(self):
            n=int(self.headers.get("Content-Length","0"))
            if n<0 or n>MAX_BODY:raise ValueError("request too large")
            o=json.loads(self.rfile.read(n).decode() or "{}")
            if not isinstance(o,dict):raise ValueError("object required")
            return o
        def media(self,p,head=False):
            size=p.stat().st_size
            try:start,end,code=_range(self.headers.get("Range"),size)
            except Exception:self.send_response(416); self.send_header("Content-Range",f"bytes */{size}"); self.send_header("Content-Length","0"); self.end_headers(); return
            length=max(0,end-start+1); self.send_response(code); self.sec(); self.send_header("Content-Type",mimetypes.guess_type(p.name)[0] or "application/octet-stream"); self.send_header("Accept-Ranges","bytes"); self.send_header("Content-Length",str(length))
            if code==206:self.send_header("Content-Range",f"bytes {start}-{end}/{size}")
            self.end_headers()
            if head:return
            try:
                with p.open("rb") as f:
                    f.seek(start); left=length
                    while left:
                        b=f.read(min(left,1024*1024))
                        if not b:break
                        self.wfile.write(b); left-=len(b)
            except (BrokenPipeError,ConnectionResetError):pass
        def do_HEAD(self):
            if not self.auth():return self.deny()
            u=urlparse(self.path)
            if u.path.startswith("/media/"):
                try:return self.media(store.safe(unquote(u.path[7:]),True),True)
                except Exception:return self.send_error(404)
            self.send_error(404)
        def do_GET(self):
            u=urlparse(self.path)
            if u.path=="/favicon.ico":self.send_response(204); self.send_header("Content-Length","0"); self.end_headers(); return
            src=self.auth()
            if not src:return self.deny()
            if u.path in ("/","/index.html") and src=="query":
                c=cookies.SimpleCookie(); c[COOKIE]=token; c[COOKIE]["path"]="/"; c[COOKIE]["httponly"]=True; c[COOKIE]["samesite"]="Strict"
                self.send_response(303); self.sec(); self.send_header("Set-Cookie",c.output(header="").strip()); self.send_header("Location","/"); self.send_header("Content-Length","0"); self.end_headers(); return
            if u.path in ("/","/index.html"):
                raw=Path(__file__).with_name("review_ui_v2.html").read_bytes(); self.send_response(200); self.sec(); self.send_header("Content-Type","text/html; charset=utf-8"); self.send_header("Content-Length",str(len(raw))); self.end_headers(); self.wfile.write(raw); return
            if u.path=="/api/items":return self.sendj(store.snapshot())
            if u.path.startswith("/media/"):
                try:return self.media(store.safe(unquote(u.path[7:]),True))
                except Exception:return self.send_error(404)
            self.send_error(404)
        def do_POST(self):
            if not self.auth():return self.deny()
            try:
                u=urlparse(self.path); o=self.body(); client=self.client_address[0]
                if u.path=="/api/decision":return self.sendj(dict(ok=True,state=store.decision(o.get("path"),o.get("decision"),o.get("note",""),client)))
                if u.path=="/api/delete-plan":return self.sendj(dict(ok=True,plan=store.delete_plan(o.get("entries",[]))))
                if u.path=="/api/delete-bulk":
                    r=store.bulk_delete(o.get("entries",[]),client); return self.sendj(dict(ok=r["failed"]==0,**r),207 if r["failed"] else 200)
                if u.path=="/api/sync":os.sync(); store.audit("sync_storage",client=client); return self.sendj(dict(ok=True))
                self.send_error(404)
            except PermissionError as e:self.sendj(dict(error=str(e)),403)
            except FileNotFoundError as e:self.sendj(dict(error=f"not found: {e}"),404)
            except (ValueError,RuntimeError) as e:self.sendj(dict(error=str(e)),409)
            except Exception as e:self.sendj(dict(error=f"server error: {e}"),500)
    return H

def _ip():
    try:
        s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.connect(("8.8.8.8",80)); ip=s.getsockname()[0]; s.close(); return ip
    except Exception:return "127.0.0.1"

def serve(root,bind="127.0.0.1",port=8090,allow_delete=False,token=None,quarantine=None,delete_policy="anything-except-keep"):
    store=Store(root,allow_delete,quarantine,delete_policy); token=token or secrets.token_urlsafe(32)
    srv=ThreadingHTTPServer((bind,int(port)),handler_factory(store,token)); host=_ip() if bind in {"0.0.0.0","::"} else bind
    print(f"WFS Review Console {VERSION}\nRoot: {store.root}\nVideo: {store.video}\nDelete: {'ENABLED' if allow_delete else 'read-only'}{(' | '+delete_policy) if allow_delete else ''}\nOpen: http://{host}:{port}/?token={quote(token)}\nPress Ctrl+C to stop.")
    store.audit("serve_start",bind=bind,port=int(port),delete_enabled=bool(allow_delete),delete_policy=delete_policy)
    try:srv.serve_forever()
    except KeyboardInterrupt:print("\nStopping review server.")
    finally:store.audit("serve_stop"); srv.server_close()
