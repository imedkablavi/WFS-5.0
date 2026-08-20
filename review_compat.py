#!/usr/bin/env python3
"""Backward-compatibility layer for WFS Review Console.

Supports both the current recovery layout (video/ + dict manifest schema) and
legacy WFS recovery outputs (final/ + top-level list manifest schema).
"""
from __future__ import annotations

from pathlib import Path
import review_server as _base

VERSION = "0.7.1"
_BaseStore = _base.Store


class Store(_BaseStore):
    def __init__(self, root, allow_delete=False, quarantine=None):
        super().__init__(root, allow_delete=allow_delete, quarantine=quarantine)

        # Current toolkit writes video/. Historical recovery scripts used final/.
        current = self.root / "video"
        legacy = self.root / "final"
        if current.is_dir():
            self.video = current.resolve()
        elif legacy.is_dir():
            self.video = legacy.resolve()
        else:
            self.video = self.root

        if self.allow_delete and not self.video.is_dir():
            raise SystemExit(f"Recovered video directory not found: {self.video}")

    def manifest(self):
        obj = _base.load_json(self.root / "manifest.json", {})

        # Current schema: {"meta": {...}, "streams": [...]}
        if isinstance(obj, dict):
            meta = obj.get("meta", {})
            rows = obj.get("streams", [])
            if not isinstance(meta, dict):
                meta = {}
            if not isinstance(rows, list):
                rows = []
            return meta, rows

        # Legacy schema: manifest.json itself is the stream list.
        if isinstance(obj, list):
            rows = [x for x in obj if isinstance(x, dict)]
            return {
                "legacy_manifest": True,
                "legacy_schema": "top-level-list",
                "stream_count": len(rows),
            }, rows

        return {}, []

    def manifest_map(self):
        _, rows = self.manifest()
        out = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            value = None
            for key in ("output", "file", "path", "mp4", "filename"):
                if row.get(key):
                    value = row[key]
                    break
            if value:
                out[Path(str(value)).name] = row
        return out


def serve(root, bind="127.0.0.1", port=8090, allow_delete=False,
          token=None, quarantine=None):
    """Run the existing HTTP review server with the compatibility Store."""
    old_store = _base.Store
    old_version = getattr(_base, "VERSION", None)
    _base.Store = Store
    _base.VERSION = VERSION
    try:
        return _base.serve(
            root=root,
            bind=bind,
            port=port,
            allow_delete=allow_delete,
            token=token,
            quarantine=quarantine,
        )
    finally:
        _base.Store = old_store
        if old_version is not None:
            _base.VERSION = old_version
