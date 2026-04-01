from __future__ import annotations

import platform
import socket
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from bsky_collector_v2.fs_utils import atomic_write_json
from bsky_collector_v2.time_utils import format_utc, now_utc
from bsky_collector_v2.types import RunId


def new_run_id() -> RunId:
    return RunId(uuid.uuid4().hex)


def _git_sha(repo_root: Path) -> str | None:
    head = repo_root / ".git" / "HEAD"
    if not head.exists():
        return None
    try:
        head_text = head.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if head_text.startswith("ref: "):
        ref = head_text.removeprefix("ref: ").strip()
        ref_path = repo_root / ".git" / ref
        try:
            return ref_path.read_text(encoding="utf-8").strip()[:40] or None
        except OSError:
            return None
    return head_text[:40] or None


def git_sha(repo_root: Path) -> str | None:
    return _git_sha(repo_root)


def hostname() -> str:
    return socket.gethostname()


@dataclass(frozen=True)
class RunManifest:
    run_id: RunId
    job_name: str
    started_at_utc: str
    params: dict[str, Any]
    git_sha: str | None
    hostname: str
    python: str
    platform: str

    @staticmethod
    def start(*, job_name: str, params: dict[str, Any], repo_root: Path) -> "RunManifest":
        run_id = new_run_id()
        started_at = now_utc()
        return RunManifest(
            run_id=run_id,
            job_name=job_name,
            started_at_utc=format_utc(started_at),
            params=params,
            git_sha=_git_sha(repo_root),
            hostname=hostname(),
            python=platform.python_version(),
            platform=platform.platform(),
        )

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["run_id"] = str(self.run_id)
        return d

    def write(self, path: Path) -> None:
        atomic_write_json(path, self.to_dict())


def finish_manifest(
    path: Path,
    *,
    finished_at_utc: datetime,
    success: bool,
    error: str | None,
    extra: dict[str, Any] | None = None,
) -> None:
    try:
        current = path.read_text(encoding="utf-8")
    except OSError as err:
        raise RuntimeError(f"cannot read run manifest to finish: {path}: {err}") from err
    # Small file; safe to parse fully.
    import json  # local import to keep module deps minimal

    try:
        data = json.loads(current)
    except json.JSONDecodeError as err:
        raise RuntimeError(f"invalid run manifest json: {path}: {err}") from err
    data["finished_at_utc"] = format_utc(finished_at_utc)
    data["success"] = bool(success)
    if error:
        data["error"] = str(error)
    if extra:
        data.update({str(k): v for k, v in extra.items()})
    atomic_write_json(path, data)
