from __future__ import annotations

from pathlib import Path

from bsky_collector_v2.fs_utils import atomic_write_json
from bsky_collector_v2.time_utils import format_utc, now_utc


def write_auth_preference_snapshot(
    path: Path,
    *,
    sample_family: str,
    vantage_id: str,
    viewer_did: str,
    identifier: str,
    pds_host: str,
    accept_language: str | None,
    accept_labelers: str | None,
    include_author_labels: bool | None,
    session_cache_path: Path | None,
) -> None:
    atomic_write_json(
        path,
        {
            "snapshot_version": "2026-03-16.1",
            "captured_at_utc": format_utc(now_utc()),
            "sample_family": sample_family,
            "viewer_mode": "auth",
            "vantage_id": vantage_id,
            "viewer_did": viewer_did,
            "identifier": identifier,
            "pds_host": pds_host,
            "accept_language": accept_language,
            "accept_labelers_requested": accept_labelers,
            "include_author_labels": include_author_labels,
            "session_cache_path": str(session_cache_path) if session_cache_path is not None else None,
            "note": "collector-visible auth preference snapshot; does not introspect server-side moderation preferences",
        },
    )
