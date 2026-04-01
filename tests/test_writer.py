from __future__ import annotations

import csv
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest


def test_csv_part_writer_survives_sigkill(tmp_path: Path) -> None:
    if not hasattr(signal, "SIGKILL"):
        pytest.skip("SIGKILL not available on this platform")

    out = tmp_path / "parts" / "feed_items_part_000.csv"
    out.parent.mkdir(parents=True, exist_ok=True)

    code = r"""
import time, sys
from pathlib import Path
from bsky_collector_v2.writers import CsvPartWriter

path = Path(sys.argv[1])
w = CsvPartWriter(path, fieldnames=["a","b"], flush_interval_s=0.1, fsync_interval_s=0.1)
i = 0
while True:
    w.write_rows([{"a": i, "b": "x"}])
    i += 1
    time.sleep(0.02)
"""
    p = subprocess.Popen([sys.executable, "-c", code, str(out)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(0.4)
        os.kill(p.pid, signal.SIGKILL)
        p.wait(timeout=5)
    finally:
        if p.poll() is None:
            p.kill()

    assert out.exists()
    # Must be parseable even if last line is partial.
    with out.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) >= 1
