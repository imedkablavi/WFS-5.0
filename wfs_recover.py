#!/usr/bin/env python3
"""Entry point for WFS 0.5 Recovery Toolkit.

The implementation is stored in ordered source parts under src/ so the project
can be distributed through environments with per-file payload limits.
The parts are concatenated in memory and executed in this module namespace.
"""
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_PARTS = sorted((_ROOT / "src").glob("wfs_recover_impl.part*"))
if not _PARTS:
    raise SystemExit("Missing implementation parts under src/")
_code = "".join(p.read_text(encoding="utf-8") for p in _PARTS)
exec(compile(_code, str(_ROOT / "src" / "wfs_recover_impl.py"), "exec"), globals(), globals())
