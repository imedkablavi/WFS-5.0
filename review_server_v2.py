#!/usr/bin/env python3
"""WFS Review Console v0.8: persistent KEEP selection and safe cleanup."""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import secrets
import shutil
import socket
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from review_compat import Store as CompatStore
from review_server import VIDEO_SUFFIXES

VERSION = "0.8.0"
MAX_BODY = 2 * 1024 * 1024
COOKIE = "wfs_review"
POLICIES = {"selection-only", "anything-except-keep", "discard-only"}


def _first(row, keys, default=""):
    for key in keys:
        if row.get(key) not in (None, ""):
            return row[key]
    return default


def _segment(value):
    s = str(value or "")
    m = re.search(r"(?<!\d)([01]\d|2[0-3])[:\-]([0-5]\d)(?!\d)", s)
    return f"{m.group(1)}-{m.group(2)}" if m else s


def _reasons(value):
    if value in (None, "", [], ()):
        return []
    if isinstance(value, list):
        return [str(x) for x in value]
    s = str(value).strip()
    if not s or s.upper() == "OK":
        return []
    return [x.strip() for x in s.split(",")] if "," in s else [s]


def _norm(row, name):
    row = row if isinstance(row, dict) else {}
    seg = _segment(_first(row, ("segment", "label", "time", "hour", "start_time"), name))
    slot = _first(row, ("slot", "stream", "candidate", "camera", "channel"), "")
    if slot == "":
        m = re.search(r"(?:candidate|stream|slot|cam(?:era)?)[_\- ]*0*(\d+)", name, re.I)
        slot = m.group(1) if m else ""
    flags = _first(row, ("reasons", "flags", "flag"), [])
    reasons = _reasons(flags)
    raw = str(_first(row, ("status", "qc_status", "result"), "")).upper()
    status = {
        "OK": "PASS", "PASS": "PASS", "PASSED": "PASS",
        "WARN": "REVIEW", "WARNING": "REVIEW", "REVIEW": "REVIEW",
        "FAIL": "FAIL", "FAILED": "FAIL", "ERROR": "FAIL",
    }.get(raw)
    if not status:
        status = "REVIEW" if reasons else ("PASS" if str(flags).upper() == "OK" else "UNTRACKED")
    return dict(
        status=status,
        segment=seg,
        slot=str(slot),
        fps=_first(row, ("fps_assumed", "fps", "frame_rate")),
        duration=_first(row, ("mp4_duration", "duration", "dur")),
        packets=_first(row, ("packets", "video_packets", "packet_count")),
        coverage=_first(row, ("coverage", "segment_coverage")),
        strategy=_first(row, ("strategy", "method")),
        reasons=reasons,
        sha256=_first(row, ("sha256", "hash", "checksum")),
    )


class Store(CompatStore):
    """Compatibility-aware review store with persistent KEEP selections.

    A checked video is stored as decision=keep. Cleanup candidates are computed
    server-side from all live videos. By default, a segment/hour is protected
    unless it contains at least one KEEP selection.
    """

    def __init__(self, root, allow_delete=False, quarantine=None, delete_policy="selection-only"):
        super().__init__(root, allow_delete=allow_delete, quarantine=quarantine)
        if delete_policy not in POLICIES:
            raise SystemExit("invalid delete policy")
        self.delete_policy = delete_policy
        if self.quarantine:
            try:
                self.quarantine.relative_to(self.video)
            except ValueError:
                pass
            else:
                raise SystemExit("quarantine directory cannot be inside recovered video directory")

    def _token(self, path):
        st = path.stat()
        return f"{st.st_size}:{st.st_mtime_ns}:{getattr(st, 'st_ino', 0)}"

    def snapshot(self):
        meta, _ = self.manifest()
        mmap = self.manifest_map()
        items = []
        seen = set()
        with self.lock:
            for path in sorted(self.video.rglob("*")):
                if not path.is_file() or path.is_symlink() or path.suffix.lower() not in VIDEO_SUFFIXES:
                    continue
                rel = path.relative_to(self.video).as_posix()
                seen.add(rel)
                state = self.state["items"].get(rel, {})
                row = mmap.get(path.name, {})
                normalized = _norm(row, path.name)
                st = path.stat()
                items.append(dict(
                    path=rel,
                    name=path.name,
                    exists=True,
                    size=st.st_size,
                    file_token=self._token(path),
                    decision=state.get("decision", "unreviewed"),
                    note=state.get("note", ""),
                    deleted_at=state.get("deleted_at"),
                    manifest_matched=bool(row),
                    **normalized,
                ))

            for rel, state in self.state["items"].items():
                if rel in seen or state.get("decision") != "deleted":
                    continue
                row = mmap.get(Path(rel).name, {})
                normalized = _norm(row, Path(rel).name)
                normalized["sha256"] = state.get("sha256") or normalized["sha256"]
                items.append(dict(
                    path=rel,
                    name=Path(rel).name,
                    exists=False,
                    size=state.get("size_before", 0),
                    file_token=state.get("file_token_before", ""),
                    decision="deleted",
                    note=state.get("note", ""),
                    deleted_at=state.get("deleted_at"),
                    manifest_matched=bool(row),
                    **normalized,
                ))

            counts = {}
            qc_counts = {}
            live_bytes = keep_bytes = unselected_bytes = 0
            for item in items:
                counts[item["decision"]] = counts.get(item["decision"], 0) + 1
                qc_counts[item["status"]] = qc_counts.get(item["status"], 0) + 1
                if item["exists"]:
                    live_bytes += item["size"]
                    if item["decision"] == "keep":
                        keep_bytes += item["size"]
                    else:
                        unselected_bytes += item["size"]

            disk = shutil.disk_usage(self.root)
            live_items = [x for x in items if x["exists"]]
            reviewed = sum(1 for x in live_items if x["decision"] in {"keep", "review", "discard"})
            return dict(
                version=VERSION,
                root=str(self.root),
                video_root=str(self.video),
                delete_enabled=self.allow_delete,
                delete_policy=self.delete_policy,
                quarantine=str(self.quarantine) if self.quarantine else None,
                manifest_meta=meta,
                disk=dict(total=disk.total, used=disk.used, free=disk.free),
                video_bytes=live_bytes,
                keep_bytes=keep_bytes,
                unselected_bytes=unselected_bytes,
                progress=dict(reviewed=reviewed, total=len(live_items), percent=round(reviewed / max(1, len(live_items)) * 100, 1)),
                counts=counts,
                qc_counts=qc_counts,
                items=items,
            )

    def _now(self):
        from review_server import now
        return now()

    def set_keep(self, rel, keep, client=None):
        path = self.safe(rel, True)
        rel = path.relative_to(self.video).as_posix()
        with self.lock:
            state = self.state["items"].setdefault(rel, {})
            previous = state.get("decision", "unreviewed")
            if previous == "deleted":
                raise ValueError("deleted item cannot be selected")
            new_value = "keep" if keep else ("unreviewed" if previous == "keep" else previous)
            state.update({"decision": new_value, "updated_utc": self._now()})
            self.save()
            self.audit("keep_selection", path=rel, selected=bool(keep), previous=previous, decision=new_value, client=client)
            return state

    def decision(self, rel, value, note="", client=None):
        self.safe(rel, True)
        return super().decision(rel, value, note, client)

    def _live_inventory(self):
        return [x for x in self.snapshot()["items"] if x["exists"]]

    def delete_unselected_plan(self, include_empty_segments=False):
        live = self._live_inventory()
        groups = {}
        unassigned = []
        for item in live:
            seg = str(item.get("segment") or "").strip()
            if not seg or not re.fullmatch(r"(?:[01]\d|2[0-3])-[0-5]\d", seg):
                unassigned.append(item)
                continue
            groups.setdefault(seg, []).append(item)

        accepted = []
        protected_segments = []
        segment_summaries = []
        keepers = []
        for seg in sorted(groups):
            group = groups[seg]
            kept = [x for x in group if x["decision"] == "keep"]
            not_kept = [x for x in group if x["decision"] != "keep"]
            keepers.extend(kept)
            if not kept and not include_empty_segments:
                protected_segments.append(seg)
                segment_summaries.append(dict(segment=seg, keep=0, delete=0, protected=len(not_kept), bytes=0, reason="no_keeper_selected"))
                continue
            for item in not_kept:
                accepted.append(dict(path=item["path"], expected_size=item["size"], expected_token=item["file_token"], segment=seg, name=item["name"]))
            segment_summaries.append(dict(
                segment=seg,
                keep=len(kept),
                delete=len(not_kept),
                protected=0,
                bytes=sum(x["size"] for x in not_kept),
                reason="complete_segment_delete" if not kept else "keep_selection",
            ))

        plan_core = {
            "include_empty_segments": bool(include_empty_segments),
            "accepted": [{"path": x["path"], "expected_size": x["expected_size"], "expected_token": x["expected_token"]} for x in accepted],
            "keepers": sorted(x["path"] for x in keepers),
            "protected_segments": protected_segments,
            "unassigned": sorted(x["path"] for x in unassigned),
        }
        plan_id = hashlib.sha256(json.dumps(plan_core, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return dict(
            plan_id=plan_id,
            include_empty_segments=bool(include_empty_segments),
            accepted=accepted,
            count=len(accepted),
            bytes=sum(x["expected_size"] for x in accepted),
            keep_count=len(keepers),
            keep_bytes=sum(x["size"] for x in keepers),
            protected_segments=protected_segments,
            protected_count=sum(len(groups[s]) for s in protected_segments),
            unassigned=[x["path"] for x in unassigned],
            segments=segment_summaries,
            policy="select-to-keep",
        )

    def delete(self, rel, expected_size=None, expected_token=None, force=False, client=None, selection_cleanup=False):
        if not self.allow_delete:
            raise PermissionError("deletion disabled; restart with --allow-delete")
        path = self.safe(rel, True)
        key = path.relative_to(self.video).as_posix()
        state = self.state["items"].setdefault(key, {})
        decision = state.get("decision", "unreviewed")
        if decision == "keep":
            raise PermissionError("file is selected KEEP")
        if self.delete_policy == "selection-only" and not selection_cleanup:
            raise PermissionError("use Delete unselected; direct deletion is disabled")
        if self.delete_policy == "discard-only" and decision != "discard" and not selection_cleanup:
            raise PermissionError("mark file DISCARD before deleting")

        token = self._token(path)
        if expected_token and expected_token != token:
            raise RuntimeError("file changed since cleanup plan; refresh and review again")
        if expected_size is not None and int(expected_size) != path.stat().st_size:
            raise RuntimeError("file size changed since cleanup plan; refresh and review again")

        size = path.stat().st_size
        free_before = shutil.disk_usage(self.root).free
        result = super().delete(rel, expected_size=size, force=False, client=client)
        state = self.state["items"].setdefault(key, {})
        state["file_token_before"] = token
        self.save()
        free_after = shutil.disk_usage(self.root).free
        result.update(free_before=free_before, free_after=free_after)
        return result

    def execute_unselected_cleanup(self, plan_id, include_empty_segments=False, client=None):
        if not self.allow_delete:
            raise PermissionError("deletion disabled; restart with --allow-delete")
        current = self.delete_unselected_plan(include_empty_segments)
        if not plan_id or not secrets.compare_digest(str(plan_id), current["plan_id"]):
            raise RuntimeError("cleanup plan changed; refresh the plan before deleting")

        results = []
        errors = []
        for entry in current["accepted"]:
            try:
                results.append(self.delete(
                    entry["path"],
                    expected_size=entry["expected_size"],
                    expected_token=entry["expected_token"],
                    client=client,
                    selection_cleanup=True,
                ))
            except Exception as exc:
                errors.append(dict(path=entry["path"], error=str(exc)))

        removed = sum(x["bytes_removed"] for x in results)
        self.audit(
            "delete_unselected_summary",
            plan_id=current["plan_id"],
            include_empty_segments=bool(include_empty_segments),
            requested=len(current["accepted"]),
            deleted=len(results),
            failed=len(errors),
            bytes_removed=removed,
            protected_segments=current["protected_segments"],
            client=client,
        )
        return dict(requested=len(current["accepted"]), deleted=len(results), failed=len(errors), bytes_removed=removed, results=results, errors=errors)


def _cookie(header):
    try:
        parsed = cookies.SimpleCookie(); parsed.load(header or "")
        return parsed[COOKIE].value if COOKIE in parsed else ""
    except Exception:
        return ""


def _range(header, size):
    if not header or not header.startswith("bytes=") or not size:
        return 0, max(0, size - 1), 200
    a, b = header[6:].split(",", 1)[0].strip().split("-", 1)
    if a:
        start = int(a); end = int(b) if b else size - 1
    else:
        n = int(b); start = max(0, size - n); end = size - 1
    if start < 0 or end < start or start >= size:
        raise ValueError
    return start, min(end, size - 1), 206


def handler_factory(store, token):
    class Handler(BaseHTTPRequestHandler):
        server_version = "WFSReview/0.8"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            print(f"{self.client_address[0]} {self.command} {urlparse(self.path).path} - {fmt % args}")

        def security_headers(self):
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'self'; media-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")

        def auth(self):
            query = (parse_qs(urlparse(self.path).query).get("token") or [""])[0]
            header = self.headers.get("X-WFS-Token") or ""
            cookie = _cookie(self.headers.get("Cookie"))
            supplied = query or header or cookie
            if supplied and secrets.compare_digest(supplied, token):
                return "query" if query else "header" if header else "cookie"
            return ""

        def send_json(self, obj, code=200):
            raw = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code); self.security_headers(); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(raw))); self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def deny(self):
            raw = b"Unauthorized\n"
            self.send_response(401); self.security_headers(); self.send_header("Content-Length", str(len(raw))); self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def body(self):
            n = int(self.headers.get("Content-Length", "0"))
            if n < 0 or n > MAX_BODY:
                raise ValueError("request too large")
            obj = json.loads(self.rfile.read(n).decode() or "{}")
            if not isinstance(obj, dict):
                raise ValueError("object required")
            return obj

        def media(self, path, head=False):
            size = path.stat().st_size
            try:
                start, end, code = _range(self.headers.get("Range"), size)
            except Exception:
                self.send_response(416); self.send_header("Content-Range", f"bytes */{size}"); self.send_header("Content-Length", "0"); self.end_headers(); return
            length = max(0, end - start + 1)
            self.send_response(code); self.security_headers(); self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream"); self.send_header("Accept-Ranges", "bytes"); self.send_header("Content-Length", str(length))
            if code == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if head:
                return
            try:
                with path.open("rb") as f:
                    f.seek(start); left = length
                    while left:
                        data = f.read(min(left, 1024 * 1024))
                        if not data:
                            break
                        self.wfile.write(data); left -= len(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_HEAD(self):
            if not self.auth():
                return self.deny()
            url = urlparse(self.path)
            if url.path.startswith("/media/"):
                try:
                    return self.media(store.safe(unquote(url.path[7:]), True), True)
                except Exception:
                    return self.send_error(404)
            self.send_error(404)

        def do_GET(self):
            url = urlparse(self.path)
            if url.path == "/favicon.ico":
                self.send_response(204); self.send_header("Content-Length", "0"); self.end_headers(); return
            source = self.auth()
            if not source:
                return self.deny()
            if url.path in ("/", "/index.html") and source == "query":
                cookie = cookies.SimpleCookie(); cookie[COOKIE] = token; cookie[COOKIE]["path"] = "/"; cookie[COOKIE]["httponly"] = True; cookie[COOKIE]["samesite"] = "Strict"
                self.send_response(303); self.security_headers(); self.send_header("Set-Cookie", cookie.output(header="").strip()); self.send_header("Location", "/"); self.send_header("Content-Length", "0"); self.end_headers(); return
            if url.path in ("/", "/index.html"):
                raw = Path(__file__).with_name("review_ui_v2.html").read_bytes()
                self.send_response(200); self.security_headers(); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw); return
            if url.path == "/api/items":
                return self.send_json(store.snapshot())
            if url.path.startswith("/media/"):
                try:
                    return self.media(store.safe(unquote(url.path[7:]), True))
                except Exception:
                    return self.send_error(404)
            self.send_error(404)

        def do_POST(self):
            if not self.auth():
                return self.deny()
            try:
                url = urlparse(self.path); obj = self.body(); client = self.client_address[0]
                if url.path == "/api/keep":
                    return self.send_json(dict(ok=True, state=store.set_keep(obj.get("path"), bool(obj.get("keep")), client)))
                if url.path == "/api/decision":
                    return self.send_json(dict(ok=True, state=store.decision(obj.get("path"), obj.get("decision"), obj.get("note", ""), client)))
                if url.path == "/api/delete-unselected-plan":
                    return self.send_json(dict(ok=True, plan=store.delete_unselected_plan(bool(obj.get("include_empty_segments")))))
                if url.path == "/api/delete-unselected":
                    result = store.execute_unselected_cleanup(obj.get("plan_id"), bool(obj.get("include_empty_segments")), client)
                    return self.send_json(dict(ok=result["failed"] == 0, **result), 207 if result["failed"] else 200)
                if url.path == "/api/sync":
                    os.sync(); store.audit("sync_storage", client=client); return self.send_json(dict(ok=True))
                self.send_error(404)
            except PermissionError as exc:
                self.send_json(dict(error=str(exc)), 403)
            except FileNotFoundError as exc:
                self.send_json(dict(error=f"not found: {exc}"), 404)
            except (ValueError, RuntimeError) as exc:
                self.send_json(dict(error=str(exc)), 409)
            except Exception as exc:
                self.send_json(dict(error=f"server error: {exc}"), 500)

    return Handler


def _ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(("8.8.8.8", 80)); ip = s.getsockname()[0]; s.close(); return ip
    except Exception:
        return "127.0.0.1"


def serve(root, bind="127.0.0.1", port=8090, allow_delete=False, token=None, quarantine=None, delete_policy="selection-only"):
    store = Store(root, allow_delete, quarantine, delete_policy)
    token = token or secrets.token_urlsafe(32)
    server = ThreadingHTTPServer((bind, int(port)), handler_factory(store, token))
    host = _ip() if bind in {"0.0.0.0", "::"} else bind
    print(
        f"WFS Review Console {VERSION}\n"
        f"Root: {store.root}\n"
        f"Video: {store.video}\n"
        f"Delete: {'ENABLED' if allow_delete else 'read-only'}{(' | ' + delete_policy) if allow_delete else ''}\n"
        f"Open: http://{host}:{port}/?token={quote(token)}\n"
        "Press Ctrl+C to stop."
    )
    store.audit("serve_start", bind=bind, port=int(port), delete_enabled=bool(allow_delete), delete_policy=delete_policy)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping review server.")
    finally:
        store.audit("serve_stop"); server.server_close()
