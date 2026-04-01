from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class OutBaseCheck:
    out_base: Path
    mountpoint: Path


def safe_cwd(*, fallback: Path) -> Path:
    """Return the current working directory or a stable fallback.

    Detached/remounted external volumes can leave long-running shells with a
    deleted cwd. Falling back keeps resumable jobs from crashing during parser
    setup or manifest writes.
    """
    try:
        return Path.cwd()
    except OSError:
        return fallback.resolve()


def _find_mountpoint(path: Path) -> Path:
    if not path.exists():
        raise FileNotFoundError(str(path))
    current = path.resolve()
    while True:
        if os.path.ismount(current):
            return current
        parent = current.parent
        if parent == current:
            return current
        current = parent


def ensure_out_base(out_base: Path) -> OutBaseCheck:
    """Fail fast if out_base is missing or not writable.

    Never silently falls back to other locations.
    """
    out_base = out_base.resolve()
    if not out_base.exists():
        raise FileNotFoundError(f"out_base does not exist: {out_base}")
    if not out_base.is_dir():
        raise NotADirectoryError(f"out_base is not a directory: {out_base}")
    mountpoint = _find_mountpoint(out_base)
    # Basic writability check (without writing elsewhere).
    if not os.access(out_base, os.W_OK):
        raise PermissionError(f"out_base is not writable: {out_base}")
    return OutBaseCheck(out_base=out_base, mountpoint=mountpoint)


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    ensure_dir(path.parent)
    payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    _atomic_write_text(path, payload)


def _atomic_write_text(path: Path, text: str) -> None:
    ensure_dir(path.parent)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        prefix=path.name + ".tmp.",
        dir=str(path.parent),
        delete=False,
    ) as tmp:
        tmp.write(text)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)
