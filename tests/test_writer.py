from __future__ import annotations

import csv
import errno
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from bsky_collector_v2.writers import CsvPartWriter, JsonlWriter


def _wait_for_parseable_csv_row(path: Path, *, timeout_s: float = 10.0) -> None:
    deadline = time.time() + timeout_s
    last_err: Exception | None = None
    while time.time() < deadline:
        if path.exists() and path.stat().st_size > 0:
            try:
                with path.open("r", encoding="utf-8", newline="") as f:
                    rows = list(csv.DictReader(f))
                if rows:
                    return
            except Exception as err:  # noqa: BLE001
                last_err = err
        time.sleep(0.05)
    if last_err is not None:
        raise last_err
    raise AssertionError(f"timed out waiting for parseable csv rows: {path}")



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
        _wait_for_parseable_csv_row(out)
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


def test_csv_part_writer_retries_transient_open_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out = tmp_path / "parts" / "feed_items_part_000.csv"
    original_open = os.open
    attempts = {"count": 0}

    def flaky_open(path, flags, mode=0o777):  # noqa: ANN001
        attempts["count"] += 1
        if attempts["count"] <= 2:
            raise OSError(errno.EBUSY, "simulated transient open failure")
        return original_open(path, flags, mode)

    monkeypatch.setattr("bsky_collector_v2.writers.os.open", flaky_open)

    writer = CsvPartWriter(out, fieldnames=["a", "b"], io_retry_initial_backoff_s=0.0)
    writer.write_rows([{"a": 1, "b": "x"}])
    writer.close()

    with out.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert rows == [{"a": "1", "b": "x"}]
    assert attempts["count"] >= 3


def test_csv_part_writer_retries_partial_write_then_transient_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = tmp_path / "parts" / "feed_items_part_000.csv"
    original_write = os.write
    state = {"call": 0}

    def flaky_write(fd, data):  # noqa: ANN001
        state["call"] += 1
        payload = bytes(data)
        if state["call"] == 2:
            first_chunk = payload[:5]
            return original_write(fd, first_chunk)
        if state["call"] == 3:
            raise OSError(errno.EIO, "simulated transient write failure")
        return original_write(fd, payload)

    monkeypatch.setattr("bsky_collector_v2.writers.os.write", flaky_write)

    with CsvPartWriter(out, fieldnames=["a", "b"], io_retry_initial_backoff_s=0.0) as writer:
        writer.write_rows([{"a": 123, "b": "alpha"}])

    with out.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert rows == [{"a": "123", "b": "alpha"}]
    assert state["call"] >= 4


def test_jsonl_writer_retries_transient_write_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out = tmp_path / "parts" / "objects.jsonl"
    original_write = os.write
    state = {"call": 0}

    def flaky_write(fd, data):  # noqa: ANN001
        state["call"] += 1
        if state["call"] == 1:
            raise OSError(errno.ETIMEDOUT, "simulated transient jsonl write failure")
        return original_write(fd, bytes(data))

    monkeypatch.setattr("bsky_collector_v2.writers.os.write", flaky_write)

    with JsonlWriter(out, io_retry_initial_backoff_s=0.0) as writer:
        writer.write_many([{"k": 1}, {"k": 2}])

    lines = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert lines == [{"k": 1}, {"k": 2}]
    assert state["call"] >= 2


def test_writer_raises_non_retryable_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out = tmp_path / "parts" / "feed_items_part_000.csv"

    def fatal_write(_fd, _data):  # noqa: ANN001
        raise OSError(errno.ENOSPC, "disk full")

    monkeypatch.setattr("bsky_collector_v2.writers.os.write", fatal_write)

    with pytest.raises(OSError, match="disk full"):
        with CsvPartWriter(out, fieldnames=["a", "b"], io_retry_initial_backoff_s=0.0) as writer:
            writer.write_rows([{"a": 1, "b": "x"}])


def test_writer_retries_transient_fsync_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out = tmp_path / "parts" / "feed_items_part_000.csv"
    original_fsync = os.fsync
    attempts = {"count": 0}

    def flaky_fsync(fd):  # noqa: ANN001
        attempts["count"] += 1
        if attempts["count"] <= 2:
            raise OSError(errno.EIO, "simulated transient fsync failure")
        return original_fsync(fd)

    monkeypatch.setattr("bsky_collector_v2.writers.os.fsync", flaky_fsync)

    with CsvPartWriter(out, fieldnames=["a", "b"], io_retry_initial_backoff_s=0.0) as writer:
        writer.write_rows([{"a": 1, "b": "x"}])
        writer.flush(force_fsync=True)

    with out.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert rows == [{"a": "1", "b": "x"}]
    assert attempts["count"] >= 3
