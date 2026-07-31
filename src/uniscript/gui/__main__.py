"""Entry point: python -m uniscript.gui."""

from __future__ import annotations

import argparse
import sys

from ..cli import data_dir
from .server import serve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="uniscript-gui",
        description="The uniscript task catalogue in a browser, served only to this machine.",
    )
    parser.add_argument("--port", type=int, default=0, help="port to bind (default: random)")
    parser.add_argument(
        "--no-browser", action="store_true", help="print the address, do not open a browser"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="start with dry run switched on"
    )
    args = parser.parse_args(argv)
    return serve(
        port=args.port,
        open_browser=not args.no_browser,
        dry_run=args.dry_run,
        backup_root=data_dir() / "backups",
    )


if __name__ == "__main__":
    sys.exit(main())
