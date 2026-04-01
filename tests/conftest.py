from __future__ import annotations

import sys
from pathlib import Path


def pytest_configure() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    tests_dir = Path(__file__).resolve().parent
    for p in (repo_root, tests_dir):
        s = str(p)
        if s not in sys.path:
            sys.path.insert(0, s)

