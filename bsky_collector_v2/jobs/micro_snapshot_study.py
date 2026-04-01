from __future__ import annotations

import asyncio
import logging
import platform
import random
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bsky_collector_v2.auth_snapshot import write_auth_preference_snapshot
from bsky_collector_v2.env import AuthEnv, load_auth_env
from bsky_collector_v2.fs_utils import atomic_write_json, ensure_dir, safe_cwd
from bsky_collector_v2.http_client import AsyncHttpClient, HttpRetryConfig, XrpcHosts
from bsky_collector_v2.instrumentation import enrich_manifest
from bsky_collector_v2.jobs.snapshot_panel import (
    ContentLabelersTracker,
    PanelFeed,
    _close_worker_writers,
    _open_worker_writers_for_parts,
    _snapshot_one_feed,
)
from bsky_collector_v2.layout import Layout
from bsky_collector_v2.manifest import finish_manifest, git_sha, new_run_id
from bsky_collector_v2.progress import ProgressReporter, ProgressState
from bsky_collector_v2.quality import assess_micro5_window
from bsky_collector_v2.request_provenance import (
    RequestContext,
    RequestOrderTracker,
    RequestProvenanceWriter,
    classify_host_kind,
    max_request_order,
)
from bsky_collector_v2.session import get_or_create_session, session_cache_path
from bsky_collector_v2.state import ControlState, SnapshotStatusDB
from bsky_collector_v2.study import (
    MICRO5_WINDOW_MINUTES,
    StudyWindow,
    compute_study_window,
    deterministic_randomization_seed,
    load_study_manifest,
    panel_file_hash,
    parse_utc_datetime,
    read_study_panel_rows,
    resolve_study_panel_path,
    shard_membership_hash,
    shard_rows,
)
from bsky_collector_v2.time_utils import MicroWindow, SnapshotHour, floor_to_hour_utc, format_utc, now_utc
from bsky_collector_v2.types import FeedUri, RunId, ViewerMode
from bsky_collector_v2.writers import CsvPartWriter

logger = logging.getLogger("bsky_collector_v2.job.micro_snapshot_study")
REPO_ROOT_FALLBACK = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class PlannedTask:
    feed_uri: FeedUri
    viewer_mode: ViewerMode
    bucket: str
    unauth_skip: int
    task_order: int


def _planned_tasks(
    *,
    selected_feeds: list[PanelFeed],
    viewer_modes: tuple[ViewerMode, ...] | tuple[str, ...],
    randomization_seed: int | str,
) -> list[PlannedTask]:
    tasks: list[PlannedTask] = []
    for mode in viewer_modes:
        for feed in selected_feeds:
            if str(mode) == "unauth" and int(feed.unauth_skip) != 0:
                continue
            tasks.append(
                PlannedTask(
                    feed_uri=feed.feed_uri,
                    viewer_mode=str(mode),
                    bucket=feed.bucket,
                    unauth_skip=int(feed.unauth_skip),
                    task_order=0,
                )
            )
    rng = random.Random(randomization_seed)
    shuffled = list(tasks)
    rng.shuffle(shuffled)
    return [
        PlannedTask(
            feed_uri=task.feed_uri,
            viewer_mode=task.viewer_mode,
            bucket=task.bucket,
            unauth_skip=task.unauth_skip,
            task_order=idx,
        )
        for idx, task in enumerate(shuffled, start=1)
    ]


@dataclass(frozen=True)
class MicroWindowPaths:
    study_id: str
    sample_family: str
    date_yyyy_mm_dd: str
    hour_str: str
    minute_str: str
    window_dir: Path
    manifest_path: Path
    progress_path: Path
    http_stats_path: Path
    request_provenance_path: Path
    quality_report_path: Path
    auth_preference_snapshot_path: Path
    status_sqlite_path: Path
    parts_dir: Path
    logs_dir: Path


def _window_paths(*, layout: Layout, study_id: str, sample_family: str, window: StudyWindow) -> MicroWindowPaths:
    micro_window = MicroWindow(start_utc=window.scheduled_window_start_utc, window_minutes=window.window_minutes)
    return MicroWindowPaths(
        study_id=study_id,
        sample_family=sample_family,
        date_yyyy_mm_dd=micro_window.date_str,
        hour_str=micro_window.hour_str,
        minute_str=micro_window.minute_str,
        window_dir=layout.micro5_window_dir(study_id=study_id, sample_family=sample_family, window=micro_window),
        manifest_path=layout.micro5_manifest_json(study_id=study_id, sample_family=sample_family, window=micro_window),
        progress_path=layout.micro5_progress_json(study_id=study_id, sample_family=sample_family, window=micro_window),
        http_stats_path=layout.micro5_http_stats_csv(study_id=study_id, sample_family=sample_family, window=micro_window),
        request_provenance_path=layout.micro5_request_provenance_csv(study_id=study_id, sample_family=sample_family, window=micro_window),
        quality_report_path=layout.micro5_quality_report_json(study_id=study_id, sample_family=sample_family, window=micro_window),
        auth_preference_snapshot_path=layout.micro5_auth_preference_snapshot_json(study_id=study_id, sample_family=sample_family, window=micro_window),
        status_sqlite_path=layout.micro5_status_sqlite(study_id=study_id, sample_family=sample_family, window=micro_window),
        parts_dir=layout.micro5_parts_dir(study_id=study_id, sample_family=sample_family, window=micro_window),
        logs_dir=layout.micro5_logs_dir(study_id=study_id, sample_family=sample_family, window=micro_window),
    )


def _enabled_viewer_modes(requested: tuple[str, ...], *, auth_env: AuthEnv | None) -> list[ViewerMode]:
    modes: list[ViewerMode] = []
    for mode in requested:
        if mode == "unauth" and "unauth" not in modes:
            modes.append("unauth")
        if mode == "auth":
            if auth_env is None:
                raise RuntimeError("study requests auth viewer mode but auth env is unavailable")
            if "auth" not in modes:
                modes.append("auth")
    if not modes:
        raise RuntimeError("micro study has no enabled viewer modes")
    return modes


def _load_or_init_manifest(
    *,
    paths: MicroWindowPaths,
    layout: Layout,
    study_manifest: dict[str, Any],
    window: StudyWindow,
    started_at_utc: str,
    params: dict[str, Any],
    vantages: list[dict[str, Any]],
    resume: bool,
    panel_hash: str,
    panel_version_id: str | None,
    shard_id: str | None,
    shard_count: int | None,
    shard_membership_hash_value: str | None,
    randomization_seed: str,
) -> dict[str, Any]:
    existing: dict[str, Any] | None = None
    if paths.manifest_path.exists():
        import json

        loaded = json.loads(paths.manifest_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            existing = loaded

    run_id = None
    if resume and existing is not None:
        candidate = existing.get("run_id")
        if isinstance(candidate, str) and candidate:
            run_id = candidate
    if run_id is None:
        run_id = str(new_run_id())

    manifest: dict[str, Any] = dict(existing or {})
    prior_started = manifest.get("started_at_utc")
    manifest_started = prior_started if isinstance(prior_started, str) and prior_started else started_at_utc
    manifest.update(
        {
            "run_id": run_id,
            "job_name": "micro-snapshot-study",
            "study_id": study_manifest["study_id"],
            "started_at_utc": manifest_started,
            "git_sha": manifest.get("git_sha") or git_sha(safe_cwd(fallback=REPO_ROOT_FALLBACK)),
            "hostname": manifest.get("hostname") or platform.node(),
            "python": manifest.get("python") or platform.python_version(),
            "platform": manifest.get("platform") or platform.platform(),
            "panel_path": str(study_manifest["panel_path"]),
            "panel_hash": panel_hash,
            "panel_version_id": panel_version_id,
            "scheduled_window_start_utc": format_utc(window.scheduled_window_start_utc),
            "scheduled_window_end_utc": format_utc(window.scheduled_window_end_utc),
            "window_minutes": window.window_minutes,
            "window_index": window.window_index,
            "window_minute": window.window_minute,
            "randomization_seed": randomization_seed,
            "shard_id": shard_id,
            "shard_count": shard_count,
            "shard_membership_hash": shard_membership_hash_value,
            "sample_design": study_manifest.get("sample_design"),
            "benchmark_summary": study_manifest.get("benchmark_result"),
            "params": dict(params),
            "vantages": list(vantages),
        }
    )
    enrich_manifest(
        manifest,
        job_name="micro-snapshot-study",
        out_base=layout.out_base,
        params=params,
        panel_version_id=panel_version_id,
        panel_hash=panel_hash,
        sample_family_override=str(study_manifest["sample_family"]),
        study_id=str(study_manifest["study_id"]),
    )
    atomic_write_json(paths.manifest_path, manifest)
    return manifest


def _write_quarantine_artifacts(
    *,
    paths: MicroWindowPaths,
    manifest: dict[str, Any],
) -> None:
    ensure_dir(paths.parts_dir)
    ensure_dir(paths.logs_dir)
    atomic_write_json(paths.manifest_path, manifest)
    atomic_write_json(
        paths.progress_path,
        {
            "job_name": "micro-snapshot-study",
            "run_id": manifest.get("run_id"),
            "feeds_total": 0,
            "feeds_done": 0,
            "feeds_failed": 0,
        },
    )


async def run_micro_snapshot_study(
    *,
    layout: Layout,
    hosts: XrpcHosts,
    env_path: Path | None,
    study_id: str,
    scheduled_window_start_utc: datetime,
    rps: float,
    concurrency: int,
    feed_time_budget_s: float,
    resume: bool,
    dry_run: bool,
) -> None:
    study_manifest = load_study_manifest(layout.study_manifest_json(study_id))
    sample_family = str(study_manifest["sample_family"])
    study_panel_path = resolve_study_panel_path(layout=layout, study_id=study_id, study_manifest=study_manifest)
    study_manifest["panel_path"] = str(study_panel_path)
    if not study_panel_path.exists():
        raise FileNotFoundError(f"study panel is missing: {study_panel_path}")

    window_origin_utc = parse_utc_datetime(str(study_manifest["window_anchor_start_utc"]))
    window = compute_study_window(
        window_origin_utc=window_origin_utc,
        scheduled_window_start_utc=scheduled_window_start_utc.astimezone(UTC),
        window_minutes=int(study_manifest.get("intended_window_minutes") or MICRO5_WINDOW_MINUTES),
    )
    paths = _window_paths(layout=layout, study_id=study_id, sample_family=sample_family, window=window)
    ensure_dir(paths.parts_dir)
    ensure_dir(paths.logs_dir)

    panel_hash = panel_file_hash(study_panel_path)
    if panel_hash != str(study_manifest["panel_hash"]):
        manifest = {
            "run_id": str(new_run_id()),
            "job_name": "micro-snapshot-study",
            "study_id": study_id,
            "sample_family": sample_family,
            "started_at_utc": format_utc(now_utc()),
            "finished_at_utc": format_utc(now_utc()),
            "success": False,
            "error": f"panel hash mismatch current={panel_hash} expected={study_manifest['panel_hash']}",
            "panel_hash": panel_hash,
            "expected_panel_hash": study_manifest["panel_hash"],
            "scheduled_window_start_utc": format_utc(window.scheduled_window_start_utc),
            "scheduled_window_end_utc": format_utc(window.scheduled_window_end_utc),
            "window_minutes": window.window_minutes,
            "window_index": window.window_index,
            "window_minute": window.window_minute,
            "shard_id": None,
            "shard_count": study_manifest.get("shard_count"),
        }
        _write_quarantine_artifacts(paths=paths, manifest=manifest)
        atomic_write_json(
            paths.quality_report_path,
            assess_micro5_window(
                layout,
                study_id=study_id,
                sample_family=sample_family,
                window=window,
            ),
        )
        raise RuntimeError(f"study panel hash mismatch for {study_id}")

    if dry_run:
        logger.info(
            "dry_run=true: would run micro window study=%s sample_family=%s window_start=%s",
            study_id,
            sample_family,
            format_utc(window.scheduled_window_start_utc),
        )
        return

    panel_rows = read_study_panel_rows(study_panel_path)
    shard_count = int(study_manifest.get("shard_count") or 0) or None
    shard_seed = study_manifest.get("shard_seed")
    shard_index: int | None = None
    shard_id: str | None = None
    shard_membership_hash_value: str | None = None
    if sample_family == "micro5_extended_sharded":
        if shard_count is None or shard_count <= 0:
            raise RuntimeError("extended sharded study is missing shard_count")
        shards = shard_rows(panel_rows, shard_count=shard_count, shard_seed=(shard_seed or study_id))
        shard_index = int(window.window_index % shard_count)
        panel_rows = list(shards.get(shard_index, []))
        shard_id = f"shard-{shard_index + 1:02d}-of-{shard_count:02d}"
        shard_membership_hash_value = shard_membership_hash(panel_rows)

    selected_feeds = [
        PanelFeed(
            feed_uri=FeedUri(row.feed_uri),
            bucket=row.bucket,
            unauth_skip=row.unauth_skip,
        )
        for row in panel_rows
    ]
    panel_version_id = str(study_manifest.get("panel_version_id") or "").strip() or None

    auth_env: AuthEnv | None = None
    if env_path is not None and env_path.exists():
        auth_env = load_auth_env(env_path)
    enabled_modes = _enabled_viewer_modes(tuple(str(mode) for mode in study_manifest.get("viewer_modes") or ()), auth_env=auth_env)

    started_at_utc = format_utc(now_utc())
    randomization_seed = deterministic_randomization_seed(
        study_id=study_id,
        scheduled_window_start_utc=format_utc(window.scheduled_window_start_utc),
        shard_id=shard_index,
    )
    auth_vantage_ids = study_manifest.get("auth_vantage_ids") if isinstance(study_manifest.get("auth_vantage_ids"), dict) else {}
    vantage_by_mode: dict[ViewerMode, str] = {
        "unauth": str(auth_vantage_ids.get("unauth") or "unauth"),
        "auth": str(auth_vantage_ids.get("auth") or "auth"),
    }
    params = {
        "study_id": study_id,
        "scheduled_window_start_utc": format_utc(window.scheduled_window_start_utc),
        "scheduled_window_end_utc": format_utc(window.scheduled_window_end_utc),
        "window_minutes": window.window_minutes,
        "window_index": window.window_index,
        "window_minute": window.window_minute,
        "viewer_modes": list(enabled_modes),
        "posts_per_feed": int(study_manifest.get("posts_per_feed") or 0),
        "rps": float(rps),
        "concurrency": int(concurrency),
        "feed_time_budget_s": float(feed_time_budget_s),
        "accept_language": study_manifest.get("accept_language"),
        "accept_labelers": study_manifest.get("accept_labelers"),
        "include_author_labels": bool(study_manifest.get("include_author_labels")),
        "panel_path": str(study_panel_path),
        "panel_hash": panel_hash,
        "panel_version_id": panel_version_id,
        "randomization_seed": randomization_seed,
        "shard_id": shard_id,
        "shard_count": shard_count,
        "shard_membership_hash": shard_membership_hash_value,
    }
    manifest = _load_or_init_manifest(
        paths=paths,
        layout=layout,
        study_manifest=study_manifest,
        window=window,
        started_at_utc=started_at_utc,
        params=params,
        vantages=[
            {
                "viewer_mode": mode,
                "vantage_id": vantage_by_mode[mode],
                "accept_language": study_manifest.get("accept_language"),
                "labelers_requested": study_manifest.get("accept_labelers"),
                "include_author_labels": bool(study_manifest.get("include_author_labels")),
            }
            for mode in enabled_modes
        ],
        resume=resume,
        panel_hash=panel_hash,
        panel_version_id=panel_version_id,
        shard_id=shard_id,
        shard_count=shard_count,
        shard_membership_hash_value=shard_membership_hash_value,
        randomization_seed=randomization_seed,
    )
    run_id = RunId(str(manifest["run_id"]))

    progress = ProgressState(job_name="micro-snapshot-study", run_id=run_id, started_at_utc=started_at_utc)
    progress.rps_config = rps
    progress.concurrency = concurrency
    reporter = ProgressReporter(paths.progress_path, progress)
    reporter.start()
    http_stats_writer = CsvPartWriter(
        paths.http_stats_path,
        fieldnames=["timestamp_utc", "endpoint", "status_code", "latency_ms", "attempt", "error_type", "feed_uri"],
    )
    request_provenance_writer = RequestProvenanceWriter(paths.request_provenance_path)
    request_order_tracker = RequestOrderTracker(
        _value=max_request_order(paths.request_provenance_path, field_name="request_order_in_sweep")
    )
    http = AsyncHttpClient(
        hosts=hosts,
        rps=rps,
        retry=HttpRetryConfig(max_retries=1),
        timeout_s=20.0,
        http_stats=http_stats_writer,
        progress=progress,
        accept_language=str(study_manifest.get("accept_language") or "").strip() or None,
        accept_labelers=str(study_manifest.get("accept_labelers") or "").strip() or None,
        request_provenance_writer=request_provenance_writer,
    )
    labelers = ContentLabelersTracker()

    success = False
    error: str | None = None
    try:
        with ControlState.open(layout.control_db_path) as control, SnapshotStatusDB.open(paths.status_sqlite_path) as status:
            access_jwt: str | None = None
            if "auth" in enabled_modes and auth_env is not None:
                auth_host_kind = classify_host_kind(
                    host=auth_env.pds_host,
                    appview_host=http.hosts.appview_host,
                    pds_host=http.hosts.pds_host,
                    access_jwt=None,
                )
                auth_cache_path = session_cache_path(control_root=layout.control_root, env=auth_env)
                refresh_order = request_order_tracker.next()
                create_order = request_order_tracker.next()
                tokens = await get_or_create_session(
                    http,
                    env=auth_env,
                    cache_path=auth_cache_path,
                    refresh_request_context=RequestContext(
                        run_id=str(run_id),
                        job_name="micro-snapshot-study",
                        sample_family=sample_family,
                        collection_params_hash=str(manifest.get("collection_params_hash") or ""),
                        study_id=study_id,
                        panel_hash=panel_hash,
                        panel_version_id=panel_version_id,
                        captured_at_utc=started_at_utc,
                        scheduled_window_start_utc=format_utc(window.scheduled_window_start_utc),
                        scheduled_window_end_utc=format_utc(window.scheduled_window_end_utc),
                        window_index=window.window_index,
                        window_minute=window.window_minute,
                        window_minutes=window.window_minutes,
                        randomization_seed=randomization_seed,
                        shard_id=shard_id,
                        shard_count=shard_count,
                        shard_membership_hash=shard_membership_hash_value,
                        date_utc=window.scheduled_window_start_utc.strftime("%Y-%m-%d"),
                        viewer_mode="auth",
                        vantage_id=vantage_by_mode["auth"],
                        host_kind=auth_host_kind,
                        host=auth_env.pds_host,
                        endpoint="com.atproto.server.refreshSession",
                        request_order_in_run=refresh_order,
                        request_order_in_window=refresh_order,
                        request_order_in_sweep=refresh_order,
                    ),
                    create_request_context=RequestContext(
                        run_id=str(run_id),
                        job_name="micro-snapshot-study",
                        sample_family=sample_family,
                        collection_params_hash=str(manifest.get("collection_params_hash") or ""),
                        study_id=study_id,
                        panel_hash=panel_hash,
                        panel_version_id=panel_version_id,
                        captured_at_utc=started_at_utc,
                        scheduled_window_start_utc=format_utc(window.scheduled_window_start_utc),
                        scheduled_window_end_utc=format_utc(window.scheduled_window_end_utc),
                        window_index=window.window_index,
                        window_minute=window.window_minute,
                        window_minutes=window.window_minutes,
                        randomization_seed=randomization_seed,
                        shard_id=shard_id,
                        shard_count=shard_count,
                        shard_membership_hash=shard_membership_hash_value,
                        date_utc=window.scheduled_window_start_utc.strftime("%Y-%m-%d"),
                        viewer_mode="auth",
                        vantage_id=vantage_by_mode["auth"],
                        host_kind=auth_host_kind,
                        host=auth_env.pds_host,
                        endpoint="com.atproto.server.createSession",
                        request_order_in_run=create_order,
                        request_order_in_window=create_order,
                        request_order_in_sweep=create_order,
                    ),
                )
                access_jwt = tokens.access_jwt
                write_auth_preference_snapshot(
                    paths.auth_preference_snapshot_path,
                    sample_family=sample_family,
                    vantage_id=vantage_by_mode["auth"],
                    viewer_did=tokens.viewer_did,
                    identifier=auth_env.identifier,
                    pds_host=auth_env.pds_host,
                    accept_language=study_manifest.get("accept_language"),
                    accept_labelers=study_manifest.get("accept_labelers"),
                    include_author_labels=bool(study_manifest.get("include_author_labels")),
                    session_cache_path=auth_cache_path,
                )

            control.start_run(
                run_id=run_id,
                job_name="micro-snapshot-study",
                started_at_utc=started_at_utc,
                params=params,
            )
            try:
                status.reset_in_progress_to_pending(updated_at_utc=started_at_utc)
                all_tasks: list[tuple[FeedUri, ViewerMode]] = []
                for mode in enabled_modes:
                    for feed in selected_feeds:
                        if mode == "unauth" and feed.unauth_skip != 0:
                            continue
                        all_tasks.append((feed.feed_uri, mode))
                progress.feeds_total = len(all_tasks)
                ordered_tasks = _planned_tasks(
                    selected_feeds=selected_feeds,
                    viewer_modes=tuple(enabled_modes),
                    randomization_seed=randomization_seed,
                )
                task_order_by_task = {
                    (str(task.feed_uri), str(task.viewer_mode)): int(task.task_order)
                    for task in ordered_tasks
                }
                status.ensure_tasks(
                    tasks=[(task.feed_uri, task.viewer_mode) for task in ordered_tasks],
                    updated_at_utc=started_at_utc,
                    task_order_by_task=task_order_by_task,
                )
                pending_lookup = {
                    (str(feed_uri), str(viewer_mode))
                    for feed_uri, viewer_mode, _attempts, _task_order in status.pending_tasks_with_order(
                        max_attempts=int(study_manifest.get("max_attempts") or 3)
                    )
                }
                queue: asyncio.Queue[PlannedTask] = asyncio.Queue()
                for task in ordered_tasks:
                    if (str(task.feed_uri), str(task.viewer_mode)) in pending_lookup:
                        queue.put_nowait(task)

                if queue.qsize() == 0:
                    success = True
                    return

                stop_at = time.monotonic() + (int(study_manifest.get("intended_window_minutes") or 5) * 60.0)
                worker_n = max(1, min(int(study_manifest.get("concurrency") or 1), queue.qsize()))

                async def worker(worker_idx: int) -> None:
                    writers = _open_worker_writers_for_parts(parts=paths.parts_dir, worker_idx=worker_idx)
                    try:
                        while True:
                            if time.monotonic() >= stop_at:
                                return
                            try:
                                task = queue.get_nowait()
                            except asyncio.QueueEmpty:
                                return
                            captured_at_utc = format_utc(now_utc())
                            prev_status = status.get_task_status(
                                feed_uri=FeedUri(str(task.feed_uri)),
                                viewer_mode=task.viewer_mode,
                            ) or "pending"
                            status.mark_in_progress(
                                feed_uri=FeedUri(str(task.feed_uri)),
                                viewer_mode=task.viewer_mode,
                                started_at_utc=captured_at_utc,
                            )
                            ok = False
                            last_err: str | None = None
                            try:
                                coro = _snapshot_one_feed(
                                    http=http,
                                    control=control,
                                    writers=writers,
                                    run_id=run_id,
                                    job_name="micro-snapshot-study",
                                    hour=SnapshotHour(hour_utc=floor_to_hour_utc(window.scheduled_window_start_utc)),
                                    feed_uri=task.feed_uri,
                                    mode=task.viewer_mode,
                                    bucket=task.bucket,
                                    posts_per_feed=int(study_manifest.get("posts_per_feed") or 0),
                                    access_jwt=(access_jwt if task.viewer_mode == "auth" else None),
                                    request_host=(
                                        auth_env.pds_host
                                        if (task.viewer_mode == "auth" and auth_env is not None)
                                        else http.hosts.appview_host
                                    ),
                                    host_kind=classify_host_kind(
                                        host=(
                                            auth_env.pds_host
                                            if (task.viewer_mode == "auth" and auth_env is not None)
                                            else http.hosts.appview_host
                                        ),
                                        appview_host=http.hosts.appview_host,
                                        pds_host=http.hosts.pds_host,
                                        access_jwt=(access_jwt if task.viewer_mode == "auth" else None),
                                    ),
                                    vantage_id=vantage_by_mode[task.viewer_mode],
                                    captured_at_utc=captured_at_utc,
                                    include_author_labels=bool(study_manifest.get("include_author_labels")),
                                    labelers=labelers,
                                    progress=progress,
                                    sample_family=sample_family,
                                    study_id=study_id,
                                    panel_hash=panel_hash,
                                    panel_version_id=panel_version_id,
                                    scheduled_window_start_utc=format_utc(window.scheduled_window_start_utc),
                                    scheduled_window_end_utc=format_utc(window.scheduled_window_end_utc),
                                    window_index=window.window_index,
                                    window_minute=window.window_minute,
                                    window_minutes=window.window_minutes,
                                    randomization_seed=randomization_seed,
                                    shard_id=shard_id,
                                    shard_count=shard_count,
                                    shard_membership_hash=shard_membership_hash_value,
                                    collection_params_hash=str(manifest.get("collection_params_hash") or ""),
                                    request_order_tracker=request_order_tracker,
                                    request_order_in_window=int(task.task_order),
                                )
                                if feed_time_budget_s > 0:
                                    await asyncio.wait_for(coro, timeout=float(feed_time_budget_s))
                                else:
                                    await coro
                                ok = True
                            except Exception as err:  # noqa: BLE001
                                last_err = repr(err)
                            finally:
                                finished_at_utc = format_utc(now_utc())
                                status.mark_done(
                                    feed_uri=FeedUri(str(task.feed_uri)),
                                    viewer_mode=task.viewer_mode,
                                    success=ok,
                                    finished_at_utc=finished_at_utc,
                                    last_error=last_err,
                                )
                                with progress.lock:
                                    if ok:
                                        if prev_status == "failed":
                                            progress.feeds_failed = max(0, progress.feeds_failed - 1)
                                        progress.feeds_done += 1
                                    else:
                                        if prev_status == "pending":
                                            progress.feeds_failed += 1
                                queue.task_done()
                    finally:
                        _close_worker_writers(writers)

                async with asyncio.TaskGroup() as tg:
                    for idx in range(worker_n):
                        tg.create_task(worker(idx))
                success = True
            except Exception as err:  # noqa: BLE001
                error = repr(err)
                raise
            finally:
                control.finish_run(run_id=run_id, finished_at_utc=format_utc(now_utc()), success=success)
                finish_manifest(
                    paths.manifest_path,
                    finished_at_utc=now_utc(),
                    success=success,
                    error=error,
                    extra={
                        "labelers_included_by_viewer_mode": {
                            str(mode): sorted(values)
                            for mode, values in labelers.by_viewer_mode.items()
                            if values
                        }
                    },
                )
    finally:
        await http.aclose()
        http_stats_writer.close()
        request_provenance_writer.close()
        reporter.stop()
        try:
            from bsky_collector_v2.effective_csv import sync_micro5_window

            sync_micro5_window(layout, study_id=study_id, sample_family=sample_family, window=window)
        except Exception as err:  # noqa: BLE001
            logger.warning(
                "effective csv sync failed job=micro-snapshot-study study=%s sample_family=%s window_start=%s err=%r",
                study_id,
                sample_family,
                format_utc(window.scheduled_window_start_utc),
                err,
            )
        atomic_write_json(
            paths.quality_report_path,
            assess_micro5_window(
                layout,
                study_id=study_id,
                sample_family=sample_family,
                window=window,
            ),
        )
