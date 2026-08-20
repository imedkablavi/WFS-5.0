#!/usr/bin/env python3
"""Entry point for WFS 0.5 Recovery Toolkit."""
from pathlib import Path
import argparse

_ROOT = Path(__file__).resolve().parent
_PARTS = sorted((_ROOT / "src").glob("wfs_recover_impl.part*"))
if not _PARTS:
    raise SystemExit("Missing implementation parts under src/")

_code = "".join(p.read_text(encoding="utf-8") for p in _PARTS)
_entry = '\n\nif __name__ == "__main__":\n    main()\n'
if _entry in _code:
    _code = _code.rsplit(_entry, 1)[0] + "\n"

exec(compile(_code, str(_ROOT / "src" / "wfs_recover_impl.py"), "exec"), globals(), globals())

VERSION = "0.9.0"
_original_build_parser = build_parser


def _review(args):
    from review_server_v3 import serve
    serve(
        args.root,
        args.bind,
        args.port,
        args.allow_delete,
        args.token,
        Path(args.quarantine_dir) if args.quarantine_dir else None,
    )


def build_parser():
    ap = _original_build_parser()
    sub = next((x for x in ap._actions if isinstance(x, argparse._SubParsersAction)), None)
    if sub is None:
        raise RuntimeError("Unable to extend WFS CLI: subparser action not found")

    review = sub.add_parser("review", help="hour-safe browser review console")
    review.add_argument("--root", required=True)
    review.add_argument("--bind", default="127.0.0.1")
    review.add_argument("--port", type=int, default=8090)
    review.add_argument("--allow-delete", action="store_true")
    review.add_argument("--token")
    review.add_argument("--quarantine-dir")
    review.set_defaults(func=_review)
    return ap


if __name__ == "__main__":
    main()
