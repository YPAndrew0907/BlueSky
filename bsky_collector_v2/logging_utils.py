from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from bsky_collector_v2.fs_utils import ensure_dir


class _MaxLevelFilter(logging.Filter):
    def __init__(self, max_level: int) -> None:
        super().__init__()
        self._max_level = max_level

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        return record.levelno <= self._max_level


@dataclass(frozen=True)
class LoggingPaths:
    collector_log: Path
    errors_log: Path


def configure_global_logging(*, log_level: str, paths: LoggingPaths) -> None:
    ensure_dir(paths.collector_log.parent)
    ensure_dir(paths.errors_log.parent)

    level = logging.DEBUG if log_level == "debug" else logging.INFO

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)

    fmt = logging.Formatter(
        fmt="%(asctime)sZ %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    fmt.converter = time.gmtime

    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(fmt)
    root.addHandler(console)

    collector = logging.FileHandler(paths.collector_log, mode="a", encoding="utf-8", delay=False)
    collector.setLevel(level)
    collector.setFormatter(fmt)
    root.addHandler(collector)

    errors = logging.FileHandler(paths.errors_log, mode="a", encoding="utf-8", delay=False)
    errors.setLevel(logging.WARNING)
    errors.setFormatter(fmt)
    root.addHandler(errors)

    # Silence extremely chatty third-party loggers by default. We record per-request metrics
    # to `http_stats.csv`, so request-by-request logging adds noise + overhead in production runs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


@contextmanager
def add_run_log_file(path: Path, *, log_level: str) -> Iterator[None]:
    ensure_dir(path.parent)
    level = logging.DEBUG if log_level == "debug" else logging.INFO
    handler = logging.FileHandler(path, mode="a", encoding="utf-8", delay=False)
    handler.setLevel(level)
    fmt = logging.Formatter(
        fmt="%(asctime)sZ %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    fmt.converter = time.gmtime
    handler.setFormatter(fmt)
    logging.getLogger().addHandler(handler)
    try:
        yield
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()
