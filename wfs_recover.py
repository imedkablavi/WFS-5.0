#!/usr/bin/env python3
"""Entry point for WFS 0.5 Recovery Toolkit.

The recovery implementation is stored in ordered source parts under src/.
This wrapper also integrates the recovered-video review console.
"""
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

VERSION = "0.7.1"
_original_build_parser = build_parser


def _cmd_review(args):
    from review_compat import serve
    serve(
        root=args.root,
        bind=args.bind,
        port=args.port,
        allow_delete=args.allow_delete,
        token=args.token,
        quarantine=Path(args.quarantine_dir) if args.quarantine_dir else None,
    )


def build_parser():
    ap = _original_build_parser()
    sub = next(
        (a for a in ap._actions if isinstance(a, argparse._SubParsersAction)),
        None,
    )
    if sub is None:
        raise RuntimeError("Unable to extend WFS CLI: subparser action not found")

    review = sub.add_parser(
        "review",
        help="secure browser review console for recovered videos",
    )
    review.add_argument("--root", required=True, help="recovery output directory")
    review.add_argument("--bind", default="127.0.0.1")
    review.add_argument("--port", type=int, default=8090)
    review.add_argument(
        "--allow-delete",
        action="store_true",
        help="enable deletion buttons; disabled by default",
    )
    review.add_argument("--token", help="fixed session token; random by default")
    review.add_argument(
        "--quarantine-dir",
        help="move removed videos here instead of permanent deletion",
    )
    review.set_defaults(func=_cmd_review)
    return ap


if __name__ == "__main__":
    main()
