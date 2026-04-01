from __future__ import annotations

import csv
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from bsky_collector_v2.fs_utils import ensure_dir


def _now_monotonic() -> float:
    return time.monotonic()


@dataclass
class CsvPartWriter:
    path: Path
    fieldnames: Sequence[str]
    flush_interval_s: float = 2.0
    fsync_interval_s: float = 10.0
    _file: Any = field(init=False, default=None)
    _writer: csv.DictWriter[str] = field(init=False)
    _rows_since_flush: int = field(init=False, default=0)
    _last_flush_s: float = field(init=False, default_factory=_now_monotonic)
    _last_fsync_s: float = field(init=False, default_factory=_now_monotonic)

    def __post_init__(self) -> None:
        ensure_dir(self.path.parent)
        file_exists = self.path.exists() and self.path.stat().st_size > 0
        self._file = open(self.path, "a", encoding="utf-8", newline="")  # noqa: SIM115
        self._writer = csv.DictWriter(self._file, fieldnames=list(self.fieldnames))
        if not file_exists:
            self._writer.writeheader()
            self.flush(force_fsync=True)

    def write_rows(self, rows: Iterable[Mapping[str, Any]]) -> int:
        count = 0
        for row in rows:
            self._writer.writerow({k: row.get(k) for k in self.fieldnames})
            count += 1
        self._rows_since_flush += count
        self._maybe_flush()
        return count

    def _maybe_flush(self) -> None:
        now = _now_monotonic()
        if (now - self._last_flush_s) >= self.flush_interval_s:
            self.flush(force_fsync=False)

    def flush(self, *, force_fsync: bool) -> None:
        self._file.flush()
        self._last_flush_s = _now_monotonic()
        if force_fsync or ((_now_monotonic() - self._last_fsync_s) >= self.fsync_interval_s):
            try:
                os.fsync(self._file.fileno())
            except OSError:
                # Best-effort; network volumes may not support fsync reliably.
                pass
            self._last_fsync_s = _now_monotonic()
        self._rows_since_flush = 0

    def close(self) -> None:
        try:
            self.flush(force_fsync=True)
        finally:
            self._file.close()

    def __enter__(self) -> "CsvPartWriter":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        self.close()


@dataclass
class JsonlWriter:
    path: Path
    flush_interval_s: float = 2.0
    fsync_interval_s: float = 10.0
    _file: Any = field(init=False, default=None)
    _last_flush_s: float = field(init=False, default_factory=_now_monotonic)
    _last_fsync_s: float = field(init=False, default_factory=_now_monotonic)

    def __post_init__(self) -> None:
        ensure_dir(self.path.parent)
        self._file = open(self.path, "a", encoding="utf-8", newline="\n")  # noqa: SIM115

    def write_obj(self, obj: Any) -> None:
        self._file.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self._maybe_flush()

    def write_many(self, objs: Iterable[Any]) -> int:
        count = 0
        for obj in objs:
            self.write_obj(obj)
            count += 1
        return count

    def _maybe_flush(self) -> None:
        now = _now_monotonic()
        if (now - self._last_flush_s) >= self.flush_interval_s:
            self.flush(force_fsync=False)

    def flush(self, *, force_fsync: bool) -> None:
        self._file.flush()
        self._last_flush_s = _now_monotonic()
        if force_fsync or ((_now_monotonic() - self._last_fsync_s) >= self.fsync_interval_s):
            try:
                os.fsync(self._file.fileno())
            except OSError:
                pass
            self._last_fsync_s = _now_monotonic()

    def close(self) -> None:
        try:
            self.flush(force_fsync=True)
        finally:
            self._file.close()

    def __enter__(self) -> "JsonlWriter":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        self.close()

