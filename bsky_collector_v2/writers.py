from __future__ import annotations

import csv
import errno
import io
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from bsky_collector_v2.fs_utils import ensure_dir

logger = logging.getLogger("bsky_collector_v2.writers")

_TRANSIENT_ERRNOS = {
    code
    for code in (
        getattr(errno, "EAGAIN", None),
        getattr(errno, "EBUSY", None),
        getattr(errno, "EINTR", None),
        getattr(errno, "EIO", None),
        getattr(errno, "ENETRESET", None),
        getattr(errno, "ENOTCONN", None),
        getattr(errno, "ESTALE", None),
        getattr(errno, "ETIMEDOUT", None),
        getattr(errno, "EWOULDBLOCK", None),
    )
    if isinstance(code, int)
}

_TRANSIENT_WINERRORS = {
    21,
    59,
    64,
    121,
    995,
    1231,
    1232,
}


def _now_monotonic() -> float:
    return time.monotonic()


def _retry_delay_s(*, attempt: int, initial_s: float, max_s: float) -> float:
    if initial_s <= 0:
        return 0.0
    growth = initial_s * (2 ** max(attempt - 1, 0))
    return min(growth, max_s) if max_s > 0 else growth


def _is_retryable_io_error(err: OSError) -> bool:
    if isinstance(err, (BlockingIOError, InterruptedError)):
        return True
    eno = getattr(err, "errno", None)
    if isinstance(eno, int) and eno in _TRANSIENT_ERRNOS:
        return True
    winerror = getattr(err, "winerror", None)
    if isinstance(winerror, int) and winerror in _TRANSIENT_WINERRORS:
        return True
    return False


@dataclass
class _RawAppendSink:
    path: Path
    io_retry_attempts: int = 6
    io_retry_initial_backoff_s: float = 0.05
    io_retry_max_backoff_s: float = 0.5
    _fd: int | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        ensure_dir(self.path.parent)
        self._open_with_retry()

    def _sleep_before_retry(self, *, attempt: int) -> None:
        delay_s = _retry_delay_s(
            attempt=attempt,
            initial_s=self.io_retry_initial_backoff_s,
            max_s=self.io_retry_max_backoff_s,
        )
        if delay_s > 0:
            time.sleep(delay_s)

    def _log_retry(self, *, op: str, attempt: int, err: OSError) -> None:
        logger.warning(
            "writer transient io error path=%s op=%s attempt=%s/%s errno=%s winerror=%s err=%r",
            str(self.path),
            op,
            attempt,
            self.io_retry_attempts,
            getattr(err, "errno", None),
            getattr(err, "winerror", None),
            err,
        )

    def _should_retry(self, *, err: OSError, attempt: int) -> bool:
        return attempt < self.io_retry_attempts and _is_retryable_io_error(err)

    def _open_fd_once(self) -> int:
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        return os.open(self.path, flags, 0o666)

    def _open_with_retry(self) -> None:
        last_err: OSError | None = None
        for attempt in range(1, self.io_retry_attempts + 1):
            try:
                self._fd = self._open_fd_once()
                return
            except OSError as err:
                last_err = err
                if not self._should_retry(err=err, attempt=attempt):
                    raise
                self._log_retry(op="open", attempt=attempt, err=err)
                self._sleep_before_retry(attempt=attempt)
        assert last_err is not None
        raise last_err

    def _ensure_open(self) -> int:
        if self._fd is None:
            self._open_with_retry()
        assert self._fd is not None
        return self._fd

    def _close_fd(self) -> None:
        if self._fd is None:
            return
        try:
            os.close(self._fd)
        finally:
            self._fd = None

    def _reopen_with_retry(self) -> None:
        self._close_fd()
        self._open_with_retry()

    def write_bytes(self, payload: bytes) -> None:
        if not payload:
            return
        view = memoryview(payload)
        offset = 0
        attempt = 0
        while offset < len(view):
            try:
                written = os.write(self._ensure_open(), view[offset:])
                if written <= 0:
                    raise OSError(errno.EIO, "os.write returned no bytes")
                offset += written
            except OSError as err:
                attempt += 1
                if not self._should_retry(err=err, attempt=attempt):
                    raise
                self._log_retry(op="write", attempt=attempt, err=err)
                self._reopen_with_retry()
                self._sleep_before_retry(attempt=attempt)

    def fsync_best_effort(self) -> None:
        attempt = 0
        while True:
            try:
                os.fsync(self._ensure_open())
                return
            except OSError as err:
                attempt += 1
                if self._should_retry(err=err, attempt=attempt):
                    self._log_retry(op="fsync", attempt=attempt, err=err)
                    self._reopen_with_retry()
                    self._sleep_before_retry(attempt=attempt)
                    continue
                logger.warning(
                    "writer best-effort fsync failed path=%s errno=%s winerror=%s err=%r",
                    str(self.path),
                    getattr(err, "errno", None),
                    getattr(err, "winerror", None),
                    err,
                )
                return

    def close(self) -> None:
        self._close_fd()


@dataclass
class CsvPartWriter:
    path: Path
    fieldnames: Sequence[str]
    flush_interval_s: float = 2.0
    fsync_interval_s: float = 10.0
    io_retry_attempts: int = 6
    io_retry_initial_backoff_s: float = 0.05
    io_retry_max_backoff_s: float = 0.5
    _fieldnames_list: list[str] = field(init=False)
    _sink: _RawAppendSink = field(init=False)
    _rows_since_flush: int = field(init=False, default=0)
    _last_flush_s: float = field(init=False, default_factory=_now_monotonic)
    _last_fsync_s: float = field(init=False, default_factory=_now_monotonic)

    def __post_init__(self) -> None:
        ensure_dir(self.path.parent)
        self._fieldnames_list = list(self.fieldnames)
        file_exists = self.path.exists() and self.path.stat().st_size > 0
        self._sink = _RawAppendSink(
            self.path,
            io_retry_attempts=self.io_retry_attempts,
            io_retry_initial_backoff_s=self.io_retry_initial_backoff_s,
            io_retry_max_backoff_s=self.io_retry_max_backoff_s,
        )
        if not file_exists:
            self._sink.write_bytes(self._serialize_header())
            self.flush(force_fsync=True)

    def _serialize_header(self) -> bytes:
        buf = io.StringIO(newline="")
        writer = csv.DictWriter(buf, fieldnames=self._fieldnames_list)
        writer.writeheader()
        return buf.getvalue().encode("utf-8")

    def _serialize_rows(self, rows: Iterable[Mapping[str, Any]]) -> tuple[bytes, int]:
        buf = io.StringIO(newline="")
        writer = csv.DictWriter(buf, fieldnames=self._fieldnames_list)
        count = 0
        for row in rows:
            writer.writerow({k: row.get(k) for k in self._fieldnames_list})
            count += 1
        return buf.getvalue().encode("utf-8"), count

    def write_rows(self, rows: Iterable[Mapping[str, Any]]) -> int:
        payload, count = self._serialize_rows(rows)
        if count <= 0:
            return 0
        self._sink.write_bytes(payload)
        self._rows_since_flush += count
        self._maybe_flush()
        return count

    def _maybe_flush(self) -> None:
        now = _now_monotonic()
        if (now - self._last_flush_s) >= self.flush_interval_s:
            self.flush(force_fsync=False)

    def flush(self, *, force_fsync: bool) -> None:
        self._last_flush_s = _now_monotonic()
        if force_fsync or ((_now_monotonic() - self._last_fsync_s) >= self.fsync_interval_s):
            self._sink.fsync_best_effort()
            self._last_fsync_s = _now_monotonic()
        self._rows_since_flush = 0

    def close(self) -> None:
        try:
            self.flush(force_fsync=True)
        finally:
            self._sink.close()

    def __enter__(self) -> "CsvPartWriter":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        self.close()


@dataclass
class JsonlWriter:
    path: Path
    flush_interval_s: float = 2.0
    fsync_interval_s: float = 10.0
    io_retry_attempts: int = 6
    io_retry_initial_backoff_s: float = 0.05
    io_retry_max_backoff_s: float = 0.5
    _sink: _RawAppendSink = field(init=False)
    _last_flush_s: float = field(init=False, default_factory=_now_monotonic)
    _last_fsync_s: float = field(init=False, default_factory=_now_monotonic)

    def __post_init__(self) -> None:
        ensure_dir(self.path.parent)
        self._sink = _RawAppendSink(
            self.path,
            io_retry_attempts=self.io_retry_attempts,
            io_retry_initial_backoff_s=self.io_retry_initial_backoff_s,
            io_retry_max_backoff_s=self.io_retry_max_backoff_s,
        )

    def write_obj(self, obj: Any) -> None:
        self._sink.write_bytes((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
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
        self._last_flush_s = _now_monotonic()
        if force_fsync or ((_now_monotonic() - self._last_fsync_s) >= self.fsync_interval_s):
            self._sink.fsync_best_effort()
            self._last_fsync_s = _now_monotonic()

    def close(self) -> None:
        try:
            self.flush(force_fsync=True)
        finally:
            self._sink.close()

    def __enter__(self) -> "JsonlWriter":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        self.close()
