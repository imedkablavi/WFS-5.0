#!/usr/bin/env python3
"""WFS Review Console v0.9.

Master-detail review workflow for legacy/current recovered video folders.
Key design goals:
- filename-first hour grouping to avoid legacy metadata mixing hours;
- persistent select-to-KEEP workflow;
- deletion only from reviewed hours with at least one KEEP;
- preview/probe support without touching the original WFS source;
- structural conflicts are surfaced and protected from automatic cleanup.
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
from collections import Counter
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from review_compat import Store as CompatStore
from review_server import VIDEO_SUFFIXES, now

VERSION = "0.9.0"
COOKIE = "wfs_review"
MAX_BODY = 2 * 1024 * 1024
PROBE_TIMEOUT = 20
DEEP_SAMPLE_SECONDS = 2.0


# ---------- metadata normalization ----------

def _first(row, keys, default=""):
    row = row if isinstance(row, dict) else {}
    for key in keys:
        if row.get(key) not in (None, ""):
            return row[key]
    return default


def _hour_from_filename(name: str) -> str:
    """Prefer the explicit time embedded in recovered filenames.

    Examples supported:
      2026-08-08_09-00_CAM_A.mp4
      2026-08-08_0900_stream1.mp4
      09-00_candidate1.mp4
    """
    s = Path(str(name)).name
    patterns = (
        r"(?:19|20)\d{2}[-_]\d{2}[-_]\d{2}[_ -]+([01]\d|2[0-3])[-_:]?([0-5]\d)",
        r"(?:^|[_ -])([01]\d|2[0-3])[-_:]([0-5]\d)(?=[_ .-]|$)",
    )
    for pattern in patterns:
        m = re.search(pattern, s)
        if m:
            return f"{m.group(1)}-{m.group(2)}"
    return ""


def _hour_from_value(value) -> str:
    s = str(value or "")
    m = re.search(r"(?<!\d)([01]\d|2[0-3])[:\-]([0-5]\d)(?!\d)", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    m = re.search(r"(?<!\d)([01]\d|2[0-3])([0-5]\d)(?!\d)", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    return ""


def _camera_from_filename(name: str, fallback="") -> tuple[str, int]:
    s = Path(str(name)).stem
    # Explicit legacy labels first.
    m = re.search(r"(?:^|[_ -])CAM(?:ERA)?[_ -]?([A-D])(?:[_ .-]|$)", s, re.I)
    if m:
        letter = m.group(1).upper()
        return f"CAM {letter}", {"A": 1, "B": 2, "C": 3, "D": 4}[letter]
    m = re.search(r"(?:^|[_ -])CAM(?:ERA)?[_ -]?0*(\d+)(?:[_ .-]|$)", s, re.I)
    if m:
        n = int(m.group(1))
        return f"CAM {n}", n
    m = re.search(r"(?:candidate|stream|slot|channel|ch)[_ -]*0*(\d+)", s, re.I)
    if m:
        n = int(m.group(1))
        return f"Candidate {n}", n

    fb = str(fallback or "").strip()
    if fb:
        if re.fullmatch(r"[A-D]", fb, re.I):
            letter = fb.upper()
            return f"CAM {letter}", {"A": 1, "B": 2, "C": 3, "D": 4}[letter]
        try:
            n = int(float(fb))
            return f"Candidate {n}", n
        except Exception:
            return fb, 999
    return "Unknown", 999


def _reasons(value) -> list[str]:
    if value in (None, "", [], ()):
        return []
    if isinstance(value, list):
        return [str(x) for x in value]
    s = str(value).strip()
    if not s or s.upper() == "OK":
        return []
    if "," in s:
        return [x.strip() for x in s.split(",") if x.strip()]
    return [s]


def _qc_status(row) -> tuple[str, list[str]]:
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
    return status, reasons


def _normalize(row, name: str) -> dict:
    row = row if isinstance(row, dict) else {}
    file_hour = _hour_from_filename(name)
    meta_hour = _hour_from_value(_first(row, ("segment", "label", "time", "hour", "start_time"), ""))
    hour = file_hour or meta_hour
    hour_conflict = bool(file_hour and meta_hour and file_hour != meta_hour)
    slot = _first(row, ("slot", "stream", "candidate", "camera", "channel"), "")
    camera, camera_sort = _camera_from_filename(name, slot)
    status, reasons = _qc_status(row)

    duration = _first(row, ("mp4_duration", "duration", "dur"), "")
    try:
        duration_num = float(duration)
    except Exception:
        duration_num = None

    health = "PASS"
    health_reasons = []
    if status == "FAIL":
        health = "FAIL"; health_reasons.append("QC status FAIL")
    elif status in {"REVIEW", "UNTRACKED"}:
        health = "REVIEW"; health_reasons.append("QC needs review" if status == "REVIEW" else "No matching QC metadata")
    if hour_conflict:
        health = "FAIL"; health_reasons.append(f"hour conflict: filename {file_hour}, metadata {meta_hour}")
    if duration_num is not None and hour:
        if duration_num < 3300 or duration_num > 3900:
            if health != "FAIL": health = "REVIEW"
            health_reasons.append(f"unusual duration {duration_num:.1f}s")

    return dict(
        hour=hour,
        hour_file=file_hour,
        hour_meta=meta_hour,
        hour_source="filename" if file_hour else ("manifest" if meta_hour else "unknown"),
        hour_conflict=hour_conflict,
        camera=camera,
        camera_sort=camera_sort,
        status=status,
        reasons=reasons,
        fps=_first(row, ("fps_assumed", "fps", "frame_rate")),
        duration=duration,
        packets=_first(row, ("packets", "video_packets", "packet_count")),
        coverage=_first(row, ("coverage", "segment_coverage")),
        strategy=_first(row, ("strategy", "method")),
        sha256=_first(row, ("sha256", "hash", "checksum")),
        health=health,
        health_reasons=health_reasons,
    )


# ---------- store ----------

class Store(CompatStore):
    def __init__(self, root, allow_delete=False, quarantine=None):
        super().__init__(root, allow_delete=allow_delete, quarantine=quarantine)
        self.probe_path = self.root / "review_probe_cache.json"
        self.probe_lock = threading.RLock()
        self.probe_cache = self._load_probe_cache()
        if self.quarantine:
            try:
                self.quarantine.relative_to(self.video)
            except ValueError:
                pass
            else:
                raise SystemExit("quarantine directory cannot be inside recovered video directory")

    def _load_probe_cache(self):
        try:
            obj = json.loads(self.probe_path.read_text(encoding="utf-8"))
            return obj if isinstance(obj, dict) else {"items": {}}
        except Exception:
            return {"version": 1, "items": {}}

    def _save_probe_cache(self):
        fd, tmp = tempfile.mkstemp(prefix=self.probe_path.name + ".", dir=str(self.root))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.probe_cache, f, indent=2, ensure_ascii=False)
                f.flush(); os.fsync(f.fileno())
            os.replace(tmp, self.probe_path)
        finally:
            try: os.unlink(tmp)
            except FileNotFoundError: pass

    def _token(self, path: Path) -> str:
        st = path.stat()
        return f"{st.st_size}:{st.st_mtime_ns}:{getattr(st, 'st_ino', 0)}"

    def _probe_for(self, rel, token):
        p = self.probe_cache.get("items", {}).get(rel)
        return p if isinstance(p, dict) and p.get("file_token") == token else None

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
                n = _normalize(row, path.name)
                st = path.stat(); token = self._token(path); probe = self._probe_for(rel, token)
                items.append(dict(
                    path=rel, name=path.name, exists=True, size=st.st_size, file_token=token,
                    decision=state.get("decision", "unreviewed"), note=state.get("note", ""),
                    deleted_at=state.get("deleted_at"), manifest_matched=bool(row), probe=probe,
                    **n,
                ))

            for rel, state in self.state["items"].items():
                if rel in seen or state.get("decision") != "deleted":
                    continue
                row = mmap.get(Path(rel).name, {})
                n = _normalize(row, Path(rel).name)
                n["sha256"] = state.get("sha256") or n["sha256"]
                items.append(dict(
                    path=rel, name=Path(rel).name, exists=False, size=state.get("size_before", 0),
                    file_token=state.get("file_token_before", ""), decision="deleted",
                    note=state.get("note", ""), deleted_at=state.get("deleted_at"),
                    manifest_matched=bool(row), probe=None, **n,
                ))

            counts = Counter(x["decision"] for x in items)
            qc_counts = Counter(x["status"] for x in items)
            live = [x for x in items if x["exists"]]
            hours = self._hour_summary(items)
            expected = self._expected_per_hour(hours)
            for h in hours:
                h["expected"] = expected
                if expected and h["original_count"] != expected:
                    h["issues"].append(f"expected {expected} files, found {h['original_count']}")
                h["safe_grouping"] = not h["hour_conflicts"]

            disk = shutil.disk_usage(self.root)
            return dict(
                version=VERSION,
                root=str(self.root), video_root=str(self.video), delete_enabled=self.allow_delete,
                quarantine=str(self.quarantine) if self.quarantine else None,
                disk=dict(total=disk.total, used=disk.used, free=disk.free),
                video_bytes=sum(x["size"] for x in live),
                keep_bytes=sum(x["size"] for x in live if x["decision"] == "keep"),
                unselected_bytes=sum(x["size"] for x in live if x["decision"] != "keep"),
                counts=dict(counts), qc_counts=dict(qc_counts), expected_per_hour=expected,
                manifest_meta=meta, hours=hours, items=items,
            )

    def _hour_summary(self, items):
        groups = {}
        for item in items:
            hour = item.get("hour") or "UNASSIGNED"
            groups.setdefault(hour, []).append(item)
        result = []
        for hour, group in groups.items():
            live = [x for x in group if x["exists"]]
            original = list(group)
            labels = [x["camera"] for x in live if x.get("camera") and x["camera"] != "Unknown"]
            duplicates = sorted(k for k, v in Counter(labels).items() if v > 1)
            conflicts = [x["name"] for x in live if x.get("hour_conflict")]
            issues = []
            if hour == "UNASSIGNED": issues.append("hour could not be determined")
            if conflicts: issues.append("filename/metadata hour conflict")
            if duplicates: issues.append("duplicate camera labels: " + ", ".join(duplicates))
            keeper_probe_fail = [x["name"] for x in live if x["decision"] == "keep" and isinstance(x.get("probe"), dict) and x["probe"].get("status") == "FAIL"]
            if keeper_probe_fail: issues.append("KEEP file failed media probe")
            result.append(dict(
                hour=hour,
                live_count=len(live), original_count=len(original), deleted_count=len(original)-len(live),
                keep_count=sum(x["decision"] == "keep" for x in live),
                live_bytes=sum(x["size"] for x in live),
                keep_bytes=sum(x["size"] for x in live if x["decision"] == "keep"),
                cameras=sorted(set(labels)), duplicate_cameras=duplicates,
                hour_conflicts=conflicts, issues=issues, keeper_probe_fail=keeper_probe_fail,
            ))
        def key(h):
            return (1, 99, 99) if h["hour"] == "UNASSIGNED" else (0, int(h["hour"][:2]), int(h["hour"][3:5]))
        return sorted(result, key=key)

    @staticmethod
    def _expected_per_hour(hours):
        vals = [h["original_count"] for h in hours if h["hour"] != "UNASSIGNED" and h["original_count"] > 0]
        if not vals: return 0
        counts = Counter(vals)
        return sorted(counts.items(), key=lambda kv: (-kv[1], -kv[0]))[0][0]

    def set_keep(self, rel, keep, client=None):
        path = self.safe(rel, True); rel = path.relative_to(self.video).as_posix()
        with self.lock:
            state = self.state["items"].setdefault(rel, {})
            if state.get("decision") == "deleted": raise ValueError("deleted item cannot be selected")
            previous = state.get("decision", "unreviewed")
            state["decision"] = "keep" if keep else ("unreviewed" if previous == "keep" else previous)
            state["updated_utc"] = now(); self.save()
            self.audit("keep_selection", path=rel, selected=bool(keep), previous=previous, decision=state["decision"], client=client)
            return state

    def decision(self, rel, value, note="", client=None):
        self.safe(rel, True)
        return super().decision(rel, value, note, client)

    # ---------- media probing ----------
    def probe(self, rel, deep=False, client=None):
        path = self.safe(rel, True); rel = path.relative_to(self.video).as_posix(); token = self._token(path)
        result = self._run_probe(path, deep=bool(deep))
        result.update(file_token=token, path=rel, checked_utc=now(), deep=bool(deep))
        with self.probe_lock:
            self.probe_cache.setdefault("items", {})[rel] = result
            self.probe_cache["updated_utc"] = now(); self._save_probe_cache()
        self.audit("media_probe", path=rel, deep=bool(deep), status=result["status"], client=client)
        return result

    def probe_scope(self, hour=None, client=None):
        snap = self.snapshot(); targets = [x for x in snap["items"] if x["exists"] and (not hour or x.get("hour") == hour)]
        results = []
        for item in targets[:100]:
            try: results.append(self.probe(item["path"], deep=False, client=client))
            except Exception as exc: results.append(dict(path=item["path"], status="FAIL", error=str(exc)))
        return dict(count=len(results), results=results)

    def _run_probe(self, path: Path, deep=False):
        cmd = [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "format=duration,size,start_time:stream=codec_name,width,height,avg_frame_rate,r_frame_rate,duration",
            "-of", "json", str(path),
        ]
        try:
            cp = subprocess.run(cmd, capture_output=True, text=True, timeout=PROBE_TIMEOUT, check=False)
        except FileNotFoundError:
            return dict(status="FAIL", reasons=["ffprobe not installed"], error="ffprobe not installed")
        except subprocess.TimeoutExpired:
            return dict(status="FAIL", reasons=["ffprobe timeout"], error="ffprobe timeout")
        if cp.returncode != 0:
            err = (cp.stderr or cp.stdout or "ffprobe failed").strip()[:2000]
            return dict(status="FAIL", reasons=["ffprobe failed"], error=err)
        try: obj = json.loads(cp.stdout or "{}")
        except Exception: obj = {}
        streams = obj.get("streams") or []; fmt = obj.get("format") or {}
        if not streams:
            return dict(status="FAIL", reasons=["no video stream"], raw=obj)
        stream = streams[0]
        try: duration = float(fmt.get("duration") or stream.get("duration") or 0)
        except Exception: duration = 0.0
        reasons = []
        status = "PASS"
        if duration <= 0:
            status = "FAIL"; reasons.append("duration unavailable")
        elif duration < 3300 or duration > 3900:
            status = "REVIEW"; reasons.append(f"unusual duration {duration:.1f}s")
        result = dict(
            status=status, reasons=reasons, duration=duration,
            codec=stream.get("codec_name"), width=stream.get("width"), height=stream.get("height"),
            avg_frame_rate=stream.get("avg_frame_rate"), r_frame_rate=stream.get("r_frame_rate"),
            format_size=fmt.get("size"), start_time=fmt.get("start_time"),
        )
        if deep and duration > 0:
            samples = []
            targets = [("start", min(5.0, duration * 0.02)), ("middle", duration * 0.5), ("end", max(0.0, duration - 10.0))]
            for label, target in targets:
                samples.append(self._decode_sample(path, label, target))
            result["samples"] = samples
            failed = [x for x in samples if not x["ok"]]
            if failed:
                result["status"] = "REVIEW" if len(failed) < len(samples) else "FAIL"
                result["reasons"] = list(result["reasons"]) + ["decode sample failed: " + ", ".join(x["label"] for x in failed)]
        return result

    @staticmethod
    def _decode_sample(path: Path, label: str, target: float):
        cmd = [
            "ffmpeg", "-nostdin", "-v", "error", "-ss", f"{target:.3f}", "-i", str(path),
            "-map", "0:v:0", "-t", str(DEEP_SAMPLE_SECONDS), "-f", "null", "-",
        ]
        try:
            cp = subprocess.run(cmd, capture_output=True, text=True, timeout=PROBE_TIMEOUT, check=False)
            err = (cp.stderr or "").strip()[:1500]
            return dict(label=label, target=target, ok=cp.returncode == 0, error=err)
        except FileNotFoundError:
            return dict(label=label, target=target, ok=False, error="ffmpeg not installed")
        except subprocess.TimeoutExpired:
            return dict(label=label, target=target, ok=False, error="decode timeout")

    # ---------- deletion planning ----------
    def cleanup_plan(self, hour=None):
        snap = self.snapshot(); hours = {h["hour"]: h for h in snap["hours"]}
        live = [x for x in snap["items"] if x["exists"] and x.get("hour") != "UNASSIGNED"]
        if hour:
            live = [x for x in live if x.get("hour") == hour]

        groups = {}
        for item in live: groups.setdefault(item["hour"], []).append(item)
        accepted = []; blocked = []; keepers = []
        for h in sorted(groups):
            group = groups[h]; summary = hours.get(h, {})
            kept = [x for x in group if x["decision"] == "keep"]
            if not kept:
                blocked.append(dict(hour=h, reason="no KEEP selected", count=len(group))); continue
            if summary.get("hour_conflicts"):
                blocked.append(dict(hour=h, reason="hour metadata conflict", count=len(group))); continue
            if summary.get("keeper_probe_fail"):
                blocked.append(dict(hour=h, reason="selected KEEP failed media probe", count=len(group))); continue
            keepers.extend(kept)
            for item in group:
                if item["decision"] == "keep": continue
                accepted.append(dict(
                    path=item["path"], name=item["name"], hour=h, camera=item["camera"],
                    expected_size=item["size"], expected_token=item["file_token"],
                ))

        core = dict(
            scope_hour=hour or "ALL_REVIEWED",
            accepted=[{k: x[k] for k in ("path", "expected_size", "expected_token")} for x in accepted],
            keepers=sorted(x["path"] for x in keepers), blocked=blocked,
        )
        plan_id = hashlib.sha256(json.dumps(core, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return dict(
            plan_id=plan_id, scope_hour=hour or "ALL_REVIEWED", accepted=accepted,
            count=len(accepted), bytes=sum(x["expected_size"] for x in accepted),
            keep_count=len(keepers), keep_bytes=sum(x["size"] for x in keepers),
            blocked=blocked, unassigned=[x["path"] for x in snap["items"] if x["exists"] and not x.get("hour")],
        )

    def execute_cleanup(self, plan_id, hour=None, client=None):
        if not self.allow_delete: raise PermissionError("deletion disabled; restart with --allow-delete")
        current = self.cleanup_plan(hour)
        if not plan_id or not secrets.compare_digest(str(plan_id), current["plan_id"]):
            raise RuntimeError("cleanup plan changed; review the plan again")
        results = []; errors = []
        for entry in current["accepted"]:
            try:
                results.append(self._delete_checked(entry, client=client))
            except Exception as exc:
                errors.append(dict(path=entry["path"], error=str(exc)))
        removed = sum(x["bytes_removed"] for x in results)
        self.audit("cleanup_summary", scope_hour=hour or "ALL_REVIEWED", requested=len(current["accepted"]), deleted=len(results), failed=len(errors), bytes_removed=removed, client=client)
        return dict(requested=len(current["accepted"]), deleted=len(results), failed=len(errors), bytes_removed=removed, results=results, errors=errors)

    def _delete_checked(self, entry, client=None):
        path = self.safe(entry["path"], True); rel = path.relative_to(self.video).as_posix()
        state = self.state["items"].setdefault(rel, {})
        if state.get("decision") == "keep": raise PermissionError("file is selected KEEP")
        token = self._token(path)
        if token != entry.get("expected_token"): raise RuntimeError("file changed since cleanup plan")
        if path.stat().st_size != int(entry.get("expected_size")): raise RuntimeError("file size changed since cleanup plan")
        size = path.stat().st_size
        result = super().delete(rel, expected_size=size, force=False, client=client)
        state = self.state["items"].setdefault(rel, {}); state["file_token_before"] = token; self.save()
        return result


# ---------- HTTP ----------

def _cookie_value(header):
    try:
        c = cookies.SimpleCookie(); c.load(header or "")
        return c[COOKIE].value if COOKIE in c else ""
    except Exception:
        return ""


def _parse_range(header, size):
    if not header or not header.startswith("bytes=") or not size:
        return 0, max(0, size - 1), 200
    a, b = header[6:].split(",", 1)[0].strip().split("-", 1)
    if a:
        start = int(a); end = int(b) if b else size - 1
    else:
        n = int(b); start = max(0, size - n); end = size - 1
    if start < 0 or end < start or start >= size: raise ValueError
    return start, min(end, size - 1), 206


def handler_factory(store: Store, token: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "WFSReview/0.9"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            print(f"{self.client_address[0]} {self.command} {urlparse(self.path).path} - {fmt % args}")

        def sec(self):
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'self'; media-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")

        def auth(self):
            q = (parse_qs(urlparse(self.path).query).get("token") or [""])[0]
            h = self.headers.get("X-WFS-Token") or ""; c = _cookie_value(self.headers.get("Cookie")); supplied = q or h or c
            return ("query" if q else "header" if h else "cookie") if supplied and secrets.compare_digest(supplied, token) else ""

        def send_json(self, obj, code=200):
            raw = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code); self.sec(); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(raw))); self.end_headers()
            try: self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError): pass

        def deny(self):
            raw = b"Unauthorized\n"; self.send_response(401); self.sec(); self.send_header("Content-Length", str(len(raw))); self.end_headers()
            try: self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError): pass

        def body(self):
            n = int(self.headers.get("Content-Length", "0"))
            if n < 0 or n > MAX_BODY: raise ValueError("request too large")
            obj = json.loads(self.rfile.read(n).decode() or "{}")
            if not isinstance(obj, dict): raise ValueError("object required")
            return obj

        def media(self, path, head=False):
            size = path.stat().st_size
            try: start, end, code = _parse_range(self.headers.get("Range"), size)
            except Exception:
                self.send_response(416); self.send_header("Content-Range", f"bytes */{size}"); self.send_header("Content-Length", "0"); self.end_headers(); return
            length = max(0, end - start + 1)
            self.send_response(code); self.sec(); self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream"); self.send_header("Accept-Ranges", "bytes"); self.send_header("Content-Length", str(length))
            if code == 206: self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if head: return
            try:
                with path.open("rb") as f:
                    f.seek(start); left = length
                    while left:
                        data = f.read(min(left, 1024 * 1024))
                        if not data: break
                        self.wfile.write(data); left -= len(data)
            except (BrokenPipeError, ConnectionResetError): pass

        def do_HEAD(self):
            if not self.auth(): return self.deny()
            u = urlparse(self.path)
            if u.path.startswith("/media/"):
                try: return self.media(store.safe(unquote(u.path[7:]), True), True)
                except Exception: return self.send_error(404)
            self.send_error(404)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/favicon.ico": self.send_response(204); self.send_header("Content-Length", "0"); self.end_headers(); return
            source = self.auth()
            if not source: return self.deny()
            if u.path in ("/", "/index.html") and source == "query":
                c = cookies.SimpleCookie(); c[COOKIE] = token; c[COOKIE]["path"] = "/"; c[COOKIE]["httponly"] = True; c[COOKIE]["samesite"] = "Strict"
                self.send_response(303); self.sec(); self.send_header("Set-Cookie", c.output(header="").strip()); self.send_header("Location", "/"); self.send_header("Content-Length", "0"); self.end_headers(); return
            if u.path in ("/", "/index.html"):
                raw = Path(__file__).with_name("review_ui_v4.html").read_bytes(); self.send_response(200); self.sec(); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw); return
            if u.path == "/api/items": return self.send_json(store.snapshot())
            if u.path.startswith("/media/"):
                try: return self.media(store.safe(unquote(u.path[7:]), True))
                except Exception: return self.send_error(404)
            self.send_error(404)

        def do_POST(self):
            if not self.auth(): return self.deny()
            try:
                u = urlparse(self.path); obj = self.body(); client = self.client_address[0]
                if u.path == "/api/keep": return self.send_json(dict(ok=True, state=store.set_keep(obj.get("path"), bool(obj.get("keep")), client)))
                if u.path == "/api/decision": return self.send_json(dict(ok=True, state=store.decision(obj.get("path"), obj.get("decision"), obj.get("note", ""), client)))
                if u.path == "/api/probe": return self.send_json(dict(ok=True, probe=store.probe(obj.get("path"), bool(obj.get("deep")), client)))
                if u.path == "/api/probe-scope": return self.send_json(dict(ok=True, **store.probe_scope(obj.get("hour"), client)))
                if u.path == "/api/cleanup-plan": return self.send_json(dict(ok=True, plan=store.cleanup_plan(obj.get("hour"))))
                if u.path == "/api/cleanup":
                    result = store.execute_cleanup(obj.get("plan_id"), obj.get("hour"), client)
                    return self.send_json(dict(ok=result["failed"] == 0, **result), 207 if result["failed"] else 200)
                if u.path == "/api/sync": os.sync(); store.audit("sync_storage", client=client); return self.send_json(dict(ok=True))
                self.send_error(404)
            except PermissionError as exc: self.send_json(dict(error=str(exc)), 403)
            except FileNotFoundError as exc: self.send_json(dict(error=f"not found: {exc}"), 404)
            except (ValueError, RuntimeError) as exc: self.send_json(dict(error=str(exc)), 409)
            except Exception as exc: self.send_json(dict(error=f"server error: {exc}"), 500)

    return Handler


def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(("8.8.8.8", 80)); ip = s.getsockname()[0]; s.close(); return ip
    except Exception: return "127.0.0.1"


def serve(root, bind="127.0.0.1", port=8090, allow_delete=False, token=None, quarantine=None):
    store = Store(root, allow_delete=allow_delete, quarantine=quarantine)
    token = token or secrets.token_urlsafe(32)
    server = ThreadingHTTPServer((bind, int(port)), handler_factory(store, token))
    host = local_ip() if bind in {"0.0.0.0", "::"} else bind
    print(
        f"WFS Review Console {VERSION}\nRoot: {store.root}\nVideo: {store.video}\n"
        f"Delete: {'ENABLED' if allow_delete else 'read-only'}\n"
        f"Open: http://{host}:{port}/?token={quote(token)}\nPress Ctrl+C to stop."
    )
    store.audit("serve_start", bind=bind, port=int(port), delete_enabled=bool(allow_delete), version=VERSION)
    try: server.serve_forever()
    except KeyboardInterrupt: print("\nStopping review server.")
    finally: store.audit("serve_stop"); server.server_close()
