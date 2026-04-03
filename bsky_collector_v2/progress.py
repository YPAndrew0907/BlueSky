from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from bsky_collector_v2.fs_utils import atomic_write_json
from bsky_collector_v2.time_utils import format_utc, now_utc
from bsky_collector_v2.types import RunId


def _now_s() -> float:
    return time.monotonic()


@dataclass
class ProgressState:
    job_name: str
    run_id: RunId
    started_at_utc: str
    start_monotonic_s: float = field(default_factory=_now_s)
    last_write_monotonic_s: float = field(default_factory=_now_s)

    unit_label: str = "feeds"
    feeds_total: int | None = None
    feeds_done: int = 0
    feeds_failed: int = 0

    rps_config: float | None = None
    concurrency: int | None = None

    http_errors_by_code: dict[str, int] = field(default_factory=dict)
    rows_written: dict[str, int] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)

    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def to_dict(self) -> dict[str, Any]:
        elapsed_s = max(0.0, _now_s() - self.start_monotonic_s)
        with self.lock:
            feeds_pending = None
            if self.feeds_total is not None:
                feeds_pending = max(0, self.feeds_total - self.feeds_done - self.feeds_failed)
            return {
                "job_name": self.job_name,
                "run_id": str(self.run_id),
                "started_at_utc": self.started_at_utc,
                "updated_at_utc": format_utc(now_utc()),
                "elapsed_s": round(elapsed_s, 3),
                "unit_label": self.unit_label,
                "feeds_total": self.feeds_total,
                "feeds_done": self.feeds_done,
                "feeds_failed": self.feeds_failed,
                "feeds_pending": feeds_pending,
                "rps_config": self.rps_config,
                "concurrency": self.concurrency,
                "http_errors_by_code": dict(self.http_errors_by_code),
                "rows_written": dict(self.rows_written),
                "details": dict(self.details),
            }

    def incr_http_error(self, code: str) -> None:
        with self.lock:
            self.http_errors_by_code[code] = int(self.http_errors_by_code.get(code, 0)) + 1

    def add_rows(self, key: str, n: int) -> None:
        if n <= 0:
            return
        with self.lock:
            self.rows_written[key] = int(self.rows_written.get(key, 0)) + int(n)

    def set_detail(self, key: str, value: Any) -> None:
        with self.lock:
            if value is None:
                self.details.pop(key, None)
            else:
                self.details[key] = value

    def update_details(self, details: Mapping[str, Any]) -> None:
        with self.lock:
            for key, value in details.items():
                if value is None:
                    self.details.pop(str(key), None)
                else:
                    self.details[str(key)] = value


@dataclass
class ProgressReporter:
    path: Path
    state: ProgressState
    write_interval_s: float = 30.0
    _stop: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _thread: threading.Thread = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._thread = threading.Thread(target=self._run_loop, name="progress-writer", daemon=True)

    def start(self) -> None:
        self._write_now()
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5.0)
        self._write_now()

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            time.sleep(self.write_interval_s)
            if self._stop.is_set():
                break
            self._write_now()

    def _write_now(self) -> None:
        atomic_write_json(self.path, self.state.to_dict())


def read_progress(path: Path) -> dict[str, Any] | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None
