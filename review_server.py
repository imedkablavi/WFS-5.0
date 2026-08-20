#!/usr/bin/env python3
"""Secure browser review console for WFS Recovery Toolkit outputs.

The server never opens the original WFS source. It only operates inside the
selected recovery output directory. Delete is disabled unless --allow-delete
is explicitly supplied. All review decisions/deletions are audit logged.
"""
from __future__ import annotations

import argparse, csv, io, json, mimetypes, os, secrets, shutil, socket, tempfile, threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

VERSION = "0.7.0"
VIDEO_SUFFIXES = {".mp4", ".avi", ".mkv", ".mov", ".hevc", ".h265"}
MAX_BODY = 1024 * 1024


def now():
    return datetime.now(timezone.utc).isoformat()


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def atomic_json(path, obj):
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, ensure_ascii=False)
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try: os.unlink(tmp)
        except FileNotFoundError: pass


class Store:
    def __init__(self, root, allow_delete=False, quarantine=None):
        self.root = Path(root).expanduser().resolve()
        if not self.root.is_dir():
            raise SystemExit(f"Recovery directory not found: {self.root}")
        v = self.root / "video"
        self.video = v.resolve() if v.is_dir() else self.root
        self.allow_delete = bool(allow_delete)
        self.quarantine = Path(quarantine).expanduser().resolve() if quarantine else None
        self.state_path = self.root / "review_state.json"
        self.audit_path = self.root / "review_audit.jsonl"
        self.lock = threading.RLock()
        self.state = load_json(self.state_path, {"version": 1, "items": {}})
        self.state.setdefault("items", {})
        if self.allow_delete and not os.access(self.video, os.W_OK):
            raise SystemExit(f"Delete mode requested but directory is not writable: {self.video}")
        if self.quarantine:
            self.quarantine.mkdir(parents=True, exist_ok=True)

    def manifest(self):
        obj = load_json(self.root / "manifest.json", {})
        return obj.get("meta", {}), obj.get("streams", [])

    def manifest_map(self):
        _, rows = self.manifest(); out = {}
        for r in rows if isinstance(rows, list) else []:
            if isinstance(r, dict) and r.get("output"):
                out[Path(str(r["output"])).name] = r
        return out

    def safe(self, rel, must_exist=True):
        if not isinstance(rel, str) or not rel or "\x00" in rel:
            raise ValueError("invalid path")
        p = Path(rel)
        if p.is_absolute() or ".." in p.parts:
            raise ValueError("path traversal rejected")
        target = (self.video / p).resolve(strict=False)
        try: target.relative_to(self.video)
        except ValueError: raise ValueError("path escapes video directory")
        if target.suffix.lower() not in VIDEO_SUFFIXES or target.is_symlink():
            raise ValueError("unsupported file target")
        if must_exist and (not target.exists() or not target.is_file()):
            raise FileNotFoundError(rel)
        return target

    def audit(self, action, **data):
        with self.audit_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"utc": now(), "action": action, **data}, ensure_ascii=False) + "\n")
            f.flush(); os.fsync(f.fileno())

    def save(self):
        self.state["updated_utc"] = now(); atomic_json(self.state_path, self.state)

    def snapshot(self):
        with self.lock:
            meta, _ = self.manifest(); mmap = self.manifest_map(); items = []; seen = set()
            for p in sorted(self.video.rglob("*")):
                if not p.is_file() or p.is_symlink() or p.suffix.lower() not in VIDEO_SUFFIXES: continue
                rel = p.relative_to(self.video).as_posix(); seen.add(rel); r = mmap.get(p.name, {}); st = self.state["items"].get(rel, {}); ps = p.stat()
                items.append({
                    "path": rel, "name": p.name, "exists": True, "size": ps.st_size,
                    "decision": st.get("decision", "unreviewed"), "note": st.get("note", ""), "deleted_at": st.get("deleted_at"),
                    "status": r.get("status", "UNTRACKED"), "segment": r.get("segment", ""), "slot": r.get("slot", ""),
                    "fps": r.get("fps_assumed", ""), "duration": r.get("mp4_duration", ""), "packets": r.get("packets", ""),
                    "coverage": r.get("coverage", ""), "strategy": r.get("strategy", ""), "reasons": r.get("reasons", []), "sha256": r.get("sha256", "")
                })
            for rel, st in self.state["items"].items():
                if rel in seen or st.get("decision") != "deleted": continue
                r = mmap.get(Path(rel).name, {})
                items.append({"path": rel, "name": Path(rel).name, "exists": False, "size": st.get("size_before", 0), "decision": "deleted", "note": st.get("note", ""), "deleted_at": st.get("deleted_at"), "status": r.get("status", "DELETED"), "segment": r.get("segment", ""), "slot": r.get("slot", ""), "fps": r.get("fps_assumed", ""), "duration": r.get("mp4_duration", ""), "packets": r.get("packets", ""), "coverage": r.get("coverage", ""), "strategy": r.get("strategy", ""), "reasons": r.get("reasons", []), "sha256": st.get("sha256") or r.get("sha256", "")})
            disk = shutil.disk_usage(self.root); counts = {}
            for x in items: counts[x["decision"]] = counts.get(x["decision"], 0) + 1
            return {"version": VERSION, "root": str(self.root), "video_root": str(self.video), "delete_enabled": self.allow_delete, "quarantine": str(self.quarantine) if self.quarantine else None, "manifest_meta": meta, "disk": {"total": disk.total, "used": disk.used, "free": disk.free}, "counts": counts, "items": items}

    def decision(self, rel, value, note="", client=None):
        if value not in {"unreviewed", "keep", "review", "discard"}: raise ValueError("invalid decision")
        t = self.safe(rel, False); rel = t.relative_to(self.video).as_posix()
        with self.lock:
            st = self.state["items"].setdefault(rel, {})
            if st.get("decision") == "deleted": raise ValueError("deleted item cannot be reclassified")
            st.update({"decision": value, "note": str(note or "")[:2000], "updated_utc": now()}); self.save(); self.audit("decision", path=rel, decision=value, note=st["note"], client=client)
        return st

    def delete(self, rel, expected_size=None, force=False, client=None):
        if not self.allow_delete: raise PermissionError("deletion disabled; restart with --allow-delete")
        t = self.safe(rel, True); rel = t.relative_to(self.video).as_posix()
        with self.lock:
            st = self.state["items"].setdefault(rel, {})
            if st.get("decision") == "keep" and not force: raise PermissionError("file is marked KEEP")
            size = t.stat().st_size
            if expected_size is not None and int(expected_size) != size: raise RuntimeError("file changed since page load")
            r = self.manifest_map().get(t.name, {}); known_hash = r.get("sha256") or st.get("sha256") or ""
            dest = None; mode = "permanent"
            if self.quarantine:
                dest = self.quarantine / self.root.name / rel; dest.parent.mkdir(parents=True, exist_ok=True)
                if dest.exists(): dest = dest.with_name(dest.stem + "-" + secrets.token_hex(4) + dest.suffix)
                shutil.move(str(t), str(dest)); mode = "quarantine"
            else: t.unlink()
            st.update({"decision": "deleted", "deleted_at": now(), "size_before": size, "sha256": known_hash, "delete_mode": mode, "destination": str(dest) if dest else None}); self.save(); self.audit("delete", path=rel, size=size, sha256=known_hash, mode=mode, destination=str(dest) if dest else None, client=client)
            return {"path": rel, "bytes_removed": size, "mode": mode}

    def csv_bytes(self):
        fields = ["path", "exists", "size", "decision", "note", "deleted_at", "status", "segment", "slot", "fps", "duration", "packets", "coverage", "strategy", "sha256", "reasons"]
        b = io.StringIO(); w = csv.DictWriter(b, fieldnames=fields); w.writeheader()
        for x in self.snapshot()["items"]:
            row = {k: x.get(k, "") for k in fields}
            if isinstance(row["reasons"], list): row["reasons"] = "; ".join(map(str, row["reasons"]))
            w.writerow(row)
        return b.getvalue().encode()


def handler_factory(store, token):
    class H(BaseHTTPRequestHandler):
        server_version = "WFSReview/0.7"
        def log_message(self, fmt, *args): print(f"{self.client_address[0]} - {fmt % args}")
        def secure(self):
            self.send_header("X-Content-Type-Options", "nosniff"); self.send_header("X-Frame-Options", "DENY"); self.send_header("Referrer-Policy", "no-referrer"); self.send_header("Cache-Control", "no-store"); self.send_header("Content-Security-Policy", "default-src 'self'; media-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'")
        def auth(self):
            q = parse_qs(urlparse(self.path).query); supplied = self.headers.get("X-WFS-Token") or (q.get("token") or [""])[0]
            return bool(supplied) and secrets.compare_digest(supplied, token)
        def deny(self):
            self.send_response(401); self.secure(); self.end_headers(); self.wfile.write(b"Unauthorized\n")
        def json(self, obj, code=200):
            raw = json.dumps(obj, ensure_ascii=False).encode(); self.send_response(code); self.secure(); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
        def read_json(self):
            n = int(self.headers.get("Content-Length", "0"));
            if n < 0 or n > MAX_BODY: raise ValueError("request too large")
            obj = json.loads(self.rfile.read(n).decode() or "{}")
            if not isinstance(obj, dict): raise ValueError("JSON object required")
            return obj
        def serve_file(self, p, head=False):
            if not p.exists() or not p.is_file() or p.is_symlink(): return self.send_error(404)
            size = p.stat().st_size; start, end, code = 0, size - 1, 200; rg = self.headers.get("Range")
            if rg and rg.startswith("bytes=") and size:
                try:
                    a,b = rg[6:].split(",",1)[0].split("-",1); start = int(a) if a else max(0, size-int(b)); end = int(b) if b and a else size-1
                    if start < 0 or end < start or start >= size: raise ValueError
                    end = min(end, size-1); code = 206
                except Exception:
                    self.send_response(416); self.send_header("Content-Range", f"bytes */{size}"); return self.end_headers()
            length = max(0, end-start+1); self.send_response(code); self.secure(); self.send_header("Content-Type", mimetypes.guess_type(p.name)[0] or "application/octet-stream"); self.send_header("Accept-Ranges", "bytes"); self.send_header("Content-Length", str(length));
            if code == 206: self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if head: return
            with p.open("rb") as f:
                f.seek(start); left = length
                while left:
                    data = f.read(min(left, 1024*1024))
                    if not data: break
                    self.wfile.write(data); left -= len(data)
        def do_HEAD(self):
            if not self.auth(): return self.deny()
            u = urlparse(self.path)
            if u.path.startswith("/media/"):
                try: return self.serve_file(store.safe(unquote(u.path[7:]), True), True)
                except Exception: return self.send_error(404)
            self.send_error(404)
        def do_GET(self):
            if not self.auth(): return self.deny()
            u = urlparse(self.path)
            if u.path in ("/", "/index.html"):
                raw = (Path(__file__).with_name("review_ui.html").read_text(encoding="utf-8").replace("__TOKEN__", json.dumps(token))).encode(); self.send_response(200); self.secure(); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(raw))); self.end_headers(); return self.wfile.write(raw)
            if u.path == "/api/items": return self.json(store.snapshot())
            if u.path == "/review.csv":
                raw = store.csv_bytes(); self.send_response(200); self.secure(); self.send_header("Content-Type", "text/csv; charset=utf-8"); self.send_header("Content-Disposition", 'attachment; filename="wfs-review.csv"'); self.send_header("Content-Length", str(len(raw))); self.end_headers(); return self.wfile.write(raw)
            if u.path.startswith("/media/"):
                try: return self.serve_file(store.safe(unquote(u.path[7:]), True))
                except Exception: return self.send_error(404)
            self.send_error(404)
        def do_POST(self):
            if not self.auth(): return self.deny()
            try:
                u = urlparse(self.path); o = self.read_json(); client = self.client_address[0]
                if u.path == "/api/decision": return self.json({"ok": True, "state": store.decision(o.get("path"), o.get("decision"), o.get("note", ""), client)})
                if u.path == "/api/delete": return self.json({"ok": True, **store.delete(o.get("path"), o.get("expected_size"), bool(o.get("force")), client)})
                self.send_error(404)
            except PermissionError as e: self.json({"error": str(e)}, 403)
            except FileNotFoundError as e: self.json({"error": str(e)}, 404)
            except (ValueError, RuntimeError) as e: self.json({"error": str(e)}, 409)
            except Exception as e: self.json({"error": f"server error: {e}"}, 500)
    return H


def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(("8.8.8.8", 80)); ip = s.getsockname()[0]; s.close(); return ip
    except Exception: return "127.0.0.1"


def serve(root, bind="127.0.0.1", port=8090, allow_delete=False, token=None, quarantine=None):
    store = Store(root, allow_delete, quarantine); token = token or secrets.token_urlsafe(24)
    if store.quarantine:
        try:
            if os.stat(store.video).st_dev == os.stat(store.quarantine).st_dev: print("WARNING: quarantine is on the same filesystem; it will not free space.")
        except OSError: pass
    server = ThreadingHTTPServer((bind, int(port)), handler_factory(store, token)); host = local_ip() if bind in {"0.0.0.0", "::"} else bind
    print(f"WFS Review Console {VERSION}\nRoot: {store.root}\nDelete mode: {'ENABLED' if allow_delete else 'read-only'}\nOpen: http://{host}:{port}/index.html?token={quote(token)}\nPress Ctrl+C to stop.")
    store.audit("serve_start", bind=bind, port=int(port), delete_enabled=bool(allow_delete))
    try: server.serve_forever()
    except KeyboardInterrupt: print("\nStopping review server.")
    finally: store.audit("serve_stop"); server.server_close()


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--root", required=True); ap.add_argument("--bind", default="127.0.0.1"); ap.add_argument("--port", type=int, default=8090); ap.add_argument("--allow-delete", action="store_true"); ap.add_argument("--token"); ap.add_argument("--quarantine-dir"); a = ap.parse_args(); serve(a.root, a.bind, a.port, a.allow_delete, a.token, a.quarantine_dir)

if __name__ == "__main__": main()
