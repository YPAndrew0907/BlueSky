from __future__ import annotations

import sys

from bsky_collector_v2.cli import main


def _main() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    _main()

