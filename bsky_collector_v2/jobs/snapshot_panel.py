from __future__ import annotations

import asyncio
import csv
import logging
import platform
import random
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from bsky_collector_v2.env import AuthEnv, load_auth_env
from bsky_collector_v2.auth_snapshot import write_auth_preference_snapshot
from bsky_collector_v2.fs_utils import atomic_write_json, ensure_dir, safe_cwd
from bsky_collector_v2.http_client import AsyncHttpClient, HttpError, HttpRetryConfig, XrpcHosts
from bsky_collector_v2.instrumentation import enrich_manifest, read_panel_metadata
from bsky_collector_v2.layout import Layout
from bsky_collector_v2.progress import ProgressReporter, ProgressState
from bsky_collector_v2.quality import assess_snapshot_hour
from bsky_collector_v2.request_provenance import (
    RequestContext,
    RequestOrderTracker,
    RequestProvenanceWriter,
    classify_host_kind,
    max_request_order,
)
from bsky_collector_v2.session import get_or_create_session, is_auth_required_error, session_cache_path
from bsky_collector_v2.state import ControlState, SnapshotStatusDB
from bsky_collector_v2.time_utils import SnapshotHour, floor_to_hour_utc, format_utc, now_utc
from bsky_collector_v2.types import FeedUri, PostUri, RunId, ViewerMode
from bsky_collector_v2.writers import CsvPartWriter

logger = logging.getLogger("bsky_collector_v2.job.snapshot_panel")
REPO_ROOT_FALLBACK = Path(__file__).resolve().parents[2]


_FEED_ITEMS_FIELDS: tuple[str, ...] = (
    "run_id",
    "sample_family",
    "study_id",
    "panel_hash",
    "panel_version_id",
    "snapshot_hour_utc",
    "scheduled_window_start_utc",
    "scheduled_window_end_utc",
    "window_index",
    "window_minute",
    "window_minutes",
    "randomization_seed",
    "shard_id",
    "shard_count",
    "shard_membership_hash",
    "captured_at_utc",
    "request_order_in_window",
    "request_order_in_sweep",
    "viewer_mode",
    "vantage_id",
    "feed_uri",
    "bucket",
    "rank",
    "post_uri",
    "post_cid",
    "author_did",
    "author_handle",
    "reason_type",
    "reason_actor_did",
)

_POSTS_FIRST_SEEN_FIELDS: tuple[str, ...] = (
    "run_id",
    "sample_family",
    "study_id",
    "panel_hash",
    "panel_version_id",
    "snapshot_hour_utc",
    "scheduled_window_start_utc",
    "scheduled_window_end_utc",
    "window_index",
    "window_minute",
    "window_minutes",
    "randomization_seed",
    "shard_id",
    "shard_count",
    "shard_membership_hash",
    "captured_at_utc",
    "request_order_in_window",
    "request_order_in_sweep",
    "viewer_mode",
    "vantage_id",
    "feed_uri",
    "bucket",
    "post_uri",
    "post_cid",
    "author_did",
    "author_handle",
    "record_created_at",
    "indexed_at",
    "text",
)

_POST_METRICS_FIELDS: tuple[str, ...] = (
    "run_id",
    "sample_family",
    "study_id",
    "panel_hash",
    "panel_version_id",
    "snapshot_hour_utc",
    "scheduled_window_start_utc",
    "scheduled_window_end_utc",
    "window_index",
    "window_minute",
    "window_minutes",
    "randomization_seed",
    "shard_id",
    "shard_count",
    "shard_membership_hash",
    "captured_at_utc",
    "request_order_in_window",
    "request_order_in_sweep",
    "viewer_mode",
    "vantage_id",
    "post_uri",
    "like_count",
    "repost_count",
    "reply_count",
    "quote_count",
)

_POST_LABEL_FIELDS: tuple[str, ...] = (
    "run_id",
    "sample_family",
    "study_id",
    "panel_hash",
    "panel_version_id",
    "snapshot_hour_utc",
    "scheduled_window_start_utc",
    "scheduled_window_end_utc",
    "window_index",
    "window_minute",
    "window_minutes",
    "randomization_seed",
    "shard_id",
    "shard_count",
    "shard_membership_hash",
    "captured_at_utc",
    "request_order_in_window",
    "request_order_in_sweep",
    "viewer_mode",
    "vantage_id",
    "post_uri",
    "post_cid",
    "label_src",
    "label_val",
    "label_neg",
    "label_uri",
    "label_cts",
)


@dataclass(frozen=True)
class PanelFeed:
    feed_uri: FeedUri
    bucket: str
    unauth_skip: int


def _read_panel(path: Path) -> list[PanelFeed]:
    with open(path, "r", encoding="utf-8", newline="") as f:
        r = csv.DictReader(f)
        out: list[PanelFeed] = []
        for row in r:
            uri = (row.get("feed_uri") or "").strip()
            if not uri:
                continue
            bucket = (row.get("bucket") or "").strip() or "unknown"
            try:
                unauth_skip = int(row.get("unauth_skip") or 0)
            except ValueError:
                unauth_skip = 0
            out.append(PanelFeed(feed_uri=FeedUri(uri), bucket=bucket, unauth_skip=unauth_skip))
    return out


def _enabled_viewer_modes(requested: Iterable[str], *, auth_env: AuthEnv | None) -> list[ViewerMode]:
    modes: list[ViewerMode] = []
    for m in requested:
        if m == "unauth" and "unauth" not in modes:
            modes.append("unauth")
        if m == "auth":
            if auth_env is None:
                continue
            if "auth" not in modes:
                modes.append("auth")
    return modes


@dataclass(frozen=True)
class SnapshotRun:
    hour: SnapshotHour
    run_id: RunId
    manifest: dict[str, Any]


def _load_or_init_hour_manifest(
    *,
    layout: Layout,
    hour: SnapshotHour,
    resume: bool,
    started_at_utc: str,
    params: dict[str, Any],
    vantages: list[dict[str, Any]],
    panel_version_id: str | None,
    panel_hash: str | None,
) -> SnapshotRun:
    manifest_path = layout.hourly_manifest_json(hour)
    existing: dict[str, Any] | None = None
    if manifest_path.exists():
        import json

        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except Exception:  # noqa: BLE001
            existing = None

    run_id: str | None = None
    if resume and existing is not None:
        rid = existing.get("run_id")
        if isinstance(rid, str) and rid:
            run_id = rid

    # New run_id if not resuming.
    from bsky_collector_v2.manifest import git_sha as current_git_sha
    from bsky_collector_v2.manifest import new_run_id

    if not run_id:
        run_id = str(new_run_id())

    manifest: dict[str, Any] = dict(existing or {})
    # Preserve original started_at if present; otherwise use this attempt start.
    prior_started = manifest.get("started_at_utc")
    manifest_started = prior_started if isinstance(prior_started, str) and prior_started else started_at_utc

    # Keep any prior success/error/finished_at values (resume must not wipe history).
    manifest.update(
        {
            "run_id": run_id,
            "job_name": "snapshot-panel",
            "snapshot_hour_utc": hour.hour_iso_z,
            "started_at_utc": manifest_started,
            "git_sha": manifest.get("git_sha") or current_git_sha(safe_cwd(fallback=REPO_ROOT_FALLBACK)),
            "hostname": manifest.get("hostname") or platform.node(),
            "python": manifest.get("python") or platform.python_version(),
            "platform": manifest.get("platform") or platform.platform(),
            "params": dict(params),
            "vantages": list(vantages),
        }
    )
    enrich_manifest(
        manifest,
        job_name="snapshot-panel",
        out_base=layout.out_base,
        params=params,
        panel_version_id=panel_version_id,
        panel_hash=panel_hash,
    )
    atomic_write_json(manifest_path, manifest)
    return SnapshotRun(hour=hour, run_id=RunId(run_id), manifest=manifest)


class ContentLabelersTracker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.by_viewer_mode: dict[ViewerMode, set[str]] = {"unauth": set(), "auth": set()}

    def record(self, *, viewer_mode: ViewerMode, content_labelers: str | None) -> None:
        if not content_labelers:
            return
        parts = [p.strip() for p in str(content_labelers).split(",") if p.strip()]
        if not parts:
            return
        with self._lock:
            self.by_viewer_mode.setdefault(viewer_mode, set()).update(parts)


async def run_snapshot_panel(
    *,
    layout: Layout,
    hosts: XrpcHosts,
    env_path: Path | None,
    viewer_modes: tuple[str, ...],
    posts_per_feed: int,
    rps: float,
    concurrency: int,
    time_budget_minutes: int,
    feed_time_budget_s: float,
    resume: bool,
    dry_run: bool,
    snapshot_hour_utc: datetime | None,
    accept_language: str | None,
    accept_labelers: str | None,
    include_author_labels: bool,
    vantage_id_unauth: str,
    vantage_id_auth: str,
) -> None:
    hour_dt = floor_to_hour_utc(snapshot_hour_utc or now_utc())
    hour = SnapshotHour(hour_utc=hour_dt)

    if dry_run:
        logger.info("dry_run=true: would snapshot hour=%s out=%s", hour.hour_iso_z, str(layout.hourly_hour_dir(hour)))
        return

    panel_path = layout.panel_active_csv
    if not panel_path.exists():
        raise FileNotFoundError(f"missing panel_v1.csv: {panel_path}")

    auth_env: AuthEnv | None = None
    if env_path is not None and env_path.exists():
        try:
            auth_env = load_auth_env(env_path)
        except Exception as err:  # noqa: BLE001
            logger.warning("auth env invalid; auth mode disabled err=%r", err)
            auth_env = None

    enabled_modes = _enabled_viewer_modes(viewer_modes, auth_env=auth_env)
    if "auth" in viewer_modes and auth_env is None:
        logger.warning("auth viewer_mode requested but no auth env available; skipping auth mode")
    if not enabled_modes:
        raise RuntimeError("no viewer modes enabled")

    panel = _read_panel(panel_path)
    panel_metadata = read_panel_metadata(panel_path)
    logger.info("panel loaded feeds=%s path=%s", len(panel), str(panel_path))

    hour_dir = layout.hourly_hour_dir(hour)
    ensure_dir(layout.hourly_parts_dir(hour))
    ensure_dir(layout.hourly_logs_dir(hour))

    started_at_utc = format_utc(now_utc())
    vantage_by_mode: dict[ViewerMode, str] = {
        "unauth": str(vantage_id_unauth).strip() or "unauth",
        "auth": str(vantage_id_auth).strip() or "auth",
    }
    snap = _load_or_init_hour_manifest(
        layout=layout,
        hour=hour,
        resume=resume,
        started_at_utc=started_at_utc,
        params={
            "snapshot_hour_utc": hour.hour_iso_z,
            "viewer_modes": list(enabled_modes),
            "posts_per_feed": int(posts_per_feed),
            "rps": float(rps),
            "concurrency": int(concurrency),
            "time_budget_minutes": int(time_budget_minutes),
            "feed_time_budget_s": float(feed_time_budget_s),
            "resume": bool(resume),
            "accept_language": accept_language,
            "accept_labelers": accept_labelers,
            "include_author_labels": bool(include_author_labels),
        },
        vantages=[
            {
                "viewer_mode": mode,
                "vantage_id": vantage_by_mode[mode],
                "accept_language": accept_language,
                "labelers_requested": accept_labelers,
                "include_author_labels": bool(include_author_labels),
            }
            for mode in enabled_modes
        ],
        panel_version_id=panel_metadata.panel_version_id,
        panel_hash=panel_metadata.panel_hash,
    )
    run_id = snap.run_id
    sample_family = str(snap.manifest.get("sample_family") or "regular_hourly")
    collection_params_hash = str(snap.manifest.get("collection_params_hash") or "")
    progress = ProgressState(job_name="snapshot-panel", run_id=run_id, started_at_utc=started_at_utc)
    progress.rps_config = rps
    progress.concurrency = concurrency

    reporter = ProgressReporter(layout.hourly_progress_json(hour), progress)
    reporter.start()

    http_stats_writer = CsvPartWriter(
        layout.hourly_http_stats_csv(hour),
        fieldnames=["timestamp_utc", "endpoint", "status_code", "latency_ms", "attempt", "error_type", "feed_uri"],
    )
    request_provenance_writer = RequestProvenanceWriter(layout.hourly_request_provenance_csv(hour))
    http = AsyncHttpClient(
        hosts=hosts,
        rps=rps,
        retry=HttpRetryConfig(max_retries=1),
        timeout_s=20.0,
        http_stats=http_stats_writer,
        progress=progress,
        accept_language=accept_language,
        accept_labelers=accept_labelers,
        request_provenance_writer=request_provenance_writer,
    )
    labelers = ContentLabelersTracker()
    request_order_tracker = RequestOrderTracker(
        _value=max_request_order(layout.hourly_request_provenance_csv(hour), field_name="request_order_in_sweep")
    )

    success = False
    error: str | None = None
    try:
        with ControlState.open(layout.control_db_path) as control, SnapshotStatusDB.open(layout.hourly_status_sqlite(hour)) as status:
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
                        job_name="snapshot-panel",
                        sample_family=sample_family,
                        collection_params_hash=collection_params_hash,
                        panel_hash=panel_metadata.panel_hash,
                        panel_version_id=panel_metadata.panel_version_id,
                        captured_at_utc=started_at_utc,
                        snapshot_hour_utc=hour.hour_iso_z,
                        date_utc=hour.date_str,
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
                        job_name="snapshot-panel",
                        sample_family=sample_family,
                        collection_params_hash=collection_params_hash,
                        panel_hash=panel_metadata.panel_hash,
                        panel_version_id=panel_metadata.panel_version_id,
                        captured_at_utc=started_at_utc,
                        snapshot_hour_utc=hour.hour_iso_z,
                        date_utc=hour.date_str,
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
                    layout.hourly_auth_preference_snapshot_json(hour),
                    sample_family=sample_family,
                    vantage_id=vantage_by_mode["auth"],
                    viewer_did=tokens.viewer_did,
                    identifier=auth_env.identifier,
                    pds_host=auth_env.pds_host,
                    accept_language=accept_language,
                    accept_labelers=accept_labelers,
                    include_author_labels=bool(include_author_labels),
                    session_cache_path=auth_cache_path,
                )
            control.start_run(
                run_id=run_id,
                job_name="snapshot-panel",
                started_at_utc=started_at_utc,
                params={
                    "snapshot_hour_utc": hour.hour_iso_z,
                    "viewer_modes": list(enabled_modes),
                    "posts_per_feed": int(posts_per_feed),
                },
            )

            try:
                # Resume semantics.
                status.reset_in_progress_to_pending(updated_at_utc=started_at_utc)

                # Create tasks for enabled modes only.
                feed_uris_for_mode: dict[ViewerMode, list[PanelFeed]] = {}
                for mode in enabled_modes:
                    if mode == "unauth":
                        feed_uris_for_mode[mode] = [p for p in panel if p.unauth_skip == 0]
                    else:
                        feed_uris_for_mode[mode] = list(panel)

                all_tasks: list[tuple[PanelFeed, ViewerMode]] = []
                for mode, feeds in feed_uris_for_mode.items():
                    for p in feeds:
                        all_tasks.append((p, mode))

                status.ensure_tasks(
                    tasks=[(p.feed_uri, mode) for p, mode in all_tasks],
                    updated_at_utc=started_at_utc,
                )

                counts = status.counts_by_status()
                progress.feeds_total = sum(counts.values())
                progress.feeds_done = int(counts.get("success", 0))
                progress.feeds_failed = int(counts.get("failed", 0))

                budget_s = max(60.0, float(time_budget_minutes) * 60.0)
                stop_at = time.monotonic() + budget_s

                pending_tasks = list(status.pending_tasks(max_attempts=3))
                random.Random(f"{run_id}:{format_utc(now_utc())}").shuffle(pending_tasks)
                queue: asyncio.Queue[tuple[FeedUri, ViewerMode]] = asyncio.Queue()
                for feed_uri, mode, _attempts in pending_tasks:
                    queue.put_nowait((feed_uri, mode))

                pending_n = queue.qsize()
                if pending_n == 0:
                    logger.info("snapshot nothing to do hour=%s (all tasks complete)", hour.hour_iso_z)
                    success = True
                    return

                worker_n = max(1, min(concurrency, 64, pending_n))
                logger.info(
                    "snapshot start hour=%s modes=%s workers=%s tasks=%s",
                    hour.hour_iso_z,
                    enabled_modes,
                    worker_n,
                    pending_n,
                )

                bucket_by_feed = {p.feed_uri: p.bucket for p in panel}

                async def worker(worker_idx: int) -> None:
                    writers = _open_worker_writers(layout=layout, hour=hour, worker_idx=worker_idx)
                    try:
                        while True:
                            if time.monotonic() >= stop_at:
                                return
                            try:
                                feed_uri, mode = queue.get_nowait()
                            except asyncio.QueueEmpty:
                                return
                            captured_at_utc = format_utc(now_utc())
                            prev_status = status.get_task_status(feed_uri=feed_uri, viewer_mode=mode) or "pending"
                            status.mark_in_progress(feed_uri=feed_uri, viewer_mode=mode, started_at_utc=captured_at_utc)
                            ok = False
                            last_err: str | None = None
                            try:
                                feed_coro = _snapshot_one_feed(
                                    http=http,
                                    control=control,
                                    writers=writers,
                                    run_id=run_id,
                                    hour=hour,
                                    feed_uri=feed_uri,
                                    mode=mode,
                                    bucket=bucket_by_feed.get(feed_uri, "unknown"),
                                    posts_per_feed=posts_per_feed,
                                    access_jwt=(access_jwt if mode == "auth" else None),
                                    request_host=(
                                        auth_env.pds_host
                                        if (mode == "auth" and auth_env is not None)
                                        else http.hosts.appview_host
                                    ),
                                    host_kind=classify_host_kind(
                                        host=(
                                            auth_env.pds_host
                                            if (mode == "auth" and auth_env is not None)
                                            else http.hosts.appview_host
                                        ),
                                        appview_host=http.hosts.appview_host,
                                        pds_host=http.hosts.pds_host,
                                        access_jwt=(access_jwt if mode == "auth" else None),
                                    ),
                                    vantage_id=vantage_by_mode[mode],
                                    captured_at_utc=captured_at_utc,
                                    include_author_labels=bool(include_author_labels),
                                    labelers=labelers,
                                    progress=progress,
                                    sample_family=sample_family,
                                    study_id=None,
                                    panel_hash=panel_metadata.panel_hash,
                                    panel_version_id=panel_metadata.panel_version_id,
                                    scheduled_window_start_utc=None,
                                    scheduled_window_end_utc=None,
                                    window_index=None,
                                    window_minute=None,
                                    randomization_seed=None,
                                    shard_id=None,
                                    shard_count=None,
                                    collection_params_hash=collection_params_hash,
                                    request_order_tracker=request_order_tracker,
                                )
                                if feed_time_budget_s > 0:
                                    await asyncio.wait_for(feed_coro, timeout=float(feed_time_budget_s))
                                else:
                                    await feed_coro
                                ok = True
                            except Exception as err:  # noqa: BLE001
                                last_err = repr(err)
                            finally:
                                finished_at_utc = format_utc(now_utc())
                                status.mark_done(
                                    feed_uri=feed_uri,
                                    viewer_mode=mode,
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
                    for i in range(worker_n):
                        tg.create_task(worker(i))

                logger.info(
                    "snapshot done hour=%s done=%s failed=%s",
                    hour.hour_iso_z,
                    progress.feeds_done,
                    progress.feeds_failed,
                )
                success = True
            except Exception as err:  # noqa: BLE001
                error = repr(err)
                raise
            finally:
                control.finish_run(run_id=run_id, finished_at_utc=format_utc(now_utc()), success=success)
                try:
                    from bsky_collector_v2.manifest import finish_manifest

                    finish_manifest(
                        layout.hourly_manifest_json(hour),
                        finished_at_utc=now_utc(),
                        success=success,
                        error=error,
                        extra={
                            "labelers_included_by_viewer_mode": {
                                str(mode): sorted(included)
                                for mode, included in labelers.by_viewer_mode.items()
                                if included
                            }
                        },
                    )
                except Exception:  # noqa: BLE001
                    pass

    finally:
        await http.aclose()
        http_stats_writer.close()
        request_provenance_writer.close()
        reporter.stop()
        try:
            from bsky_collector_v2.effective_csv import refresh_key_views, sync_hour

            sync_hour(layout, hour=hour)
            refresh_key_views(layout)
        except Exception as err:  # noqa: BLE001
            logger.warning(
                "effective csv sync failed job=snapshot-panel hour=%s err=%r",
                hour.hour_iso_z,
                err,
            )
        try:
            atomic_write_json(layout.hourly_quality_report_json(hour), assess_snapshot_hour(layout, hour=hour))
        except Exception as err:  # noqa: BLE001
            logger.warning("quality report write failed job=snapshot-panel hour=%s err=%r", hour.hour_iso_z, err)


@dataclass
class WorkerWriters:
    feed_items: CsvPartWriter
    posts_first_seen: CsvPartWriter
    post_metrics: CsvPartWriter
    post_labels: CsvPartWriter


def _open_worker_writers_for_parts(*, parts: Path, worker_idx: int) -> WorkerWriters:
    return WorkerWriters(
        feed_items=CsvPartWriter(parts / f"feed_items_part_{worker_idx:03d}.csv", fieldnames=_FEED_ITEMS_FIELDS),
        posts_first_seen=CsvPartWriter(parts / f"posts_first_seen_part_{worker_idx:03d}.csv", fieldnames=_POSTS_FIRST_SEEN_FIELDS),
        post_labels=CsvPartWriter(parts / f"post_labels_part_{worker_idx:03d}.csv", fieldnames=_POST_LABEL_FIELDS),
        post_metrics=CsvPartWriter(parts / f"post_metrics_part_{worker_idx:03d}.csv", fieldnames=_POST_METRICS_FIELDS),
    )


def _open_worker_writers(*, layout: Layout, hour: SnapshotHour, worker_idx: int) -> WorkerWriters:
    return _open_worker_writers_for_parts(parts=layout.hourly_parts_dir(hour), worker_idx=worker_idx)


def _close_worker_writers(w: WorkerWriters) -> None:
    w.feed_items.close()
    w.posts_first_seen.close()
    w.post_metrics.close()
    w.post_labels.close()


def _bucket_for(panel: list[PanelFeed], feed_uri: FeedUri) -> str:
    for p in panel:
        if p.feed_uri == feed_uri:
            return p.bucket
    return "unknown"


async def _snapshot_one_feed(
    *,
    http: AsyncHttpClient,
    control: ControlState,
    writers: WorkerWriters,
    run_id: RunId,
    job_name: str = "snapshot-panel",
    hour: SnapshotHour,
    feed_uri: FeedUri,
    mode: ViewerMode,
    bucket: str,
    posts_per_feed: int,
    access_jwt: str | None,
    request_host: str,
    host_kind: str,
    vantage_id: str,
    captured_at_utc: str,
    include_author_labels: bool,
    labelers: ContentLabelersTracker,
    progress: ProgressState,
    sample_family: str | None = None,
    study_id: str | None = None,
    panel_hash: str | None = None,
    panel_version_id: str | None = None,
    scheduled_window_start_utc: str | None = None,
    scheduled_window_end_utc: str | None = None,
    window_index: int | None = None,
    window_minute: int | None = None,
    window_minutes: int | None = None,
    randomization_seed: str | None = None,
    shard_id: int | None = None,
    shard_count: int | None = None,
    shard_membership_hash: str | None = None,
    collection_params_hash: str | None = None,
    request_order_tracker: RequestOrderTracker | None = None,
    request_order_in_window: int | None = None,
) -> None:
    fetched = 0
    cursor: str | None = None
    rank = 0
    page_no = 0
    sample_family = str(sample_family or "regular_hourly")
    collection_params_hash = str(collection_params_hash or "")
    request_order_tracker = request_order_tracker or RequestOrderTracker()

    while fetched < posts_per_feed:
        limit = min(100, posts_per_feed - fetched)
        # Some key discovery feeds on Bluesky are currently extremely strict about `limit`.
        # In particular, the official "for-you" feed generator returns HTTP 400 when limit > 1
        # on the public AppView, and may return empty results for larger limits on other hosts.
        # Override to keep the snapshot resilient and capture at least the top result when available.
        if str(feed_uri) == "at://did:plc:3guzzweuqraryl3rdkimjamk/app.bsky.feed.generator/for-you":
            limit = 1
        request_order = request_order_tracker.next()
        window_request_order = int(request_order_in_window) if request_order_in_window is not None else request_order
        try:
            resp = await http.xrpc_get(
                endpoint="app.bsky.feed.getFeed",
                host=request_host,
                method="app.bsky.feed.getFeed",
                params={"feed": str(feed_uri), "limit": limit, **({"cursor": cursor} if cursor else {})},
                access_jwt=access_jwt,
                feed_uri=str(feed_uri),
                timestamp_utc=captured_at_utc,
                request_context=RequestContext(
                    run_id=str(run_id),
                    job_name=job_name,
                    sample_family=sample_family,
                    collection_params_hash=collection_params_hash,
                    study_id=study_id,
                    panel_hash=panel_hash,
                    panel_version_id=panel_version_id,
                    captured_at_utc=captured_at_utc,
                    snapshot_hour_utc=hour.hour_iso_z,
                    scheduled_window_start_utc=scheduled_window_start_utc,
                    scheduled_window_end_utc=scheduled_window_end_utc,
                    window_index=window_index,
                    window_minute=window_minute,
                    window_minutes=window_minutes,
                    randomization_seed=randomization_seed,
                    shard_id=shard_id,
                    shard_count=shard_count,
                    shard_membership_hash=shard_membership_hash,
                    date_utc=hour.date_str,
                    viewer_mode=mode,
                    vantage_id=vantage_id,
                    host_kind=host_kind,
                    host=request_host,
                    endpoint="app.bsky.feed.getFeed",
                    feed_uri=str(feed_uri),
                    page_no=page_no,
                    cursor_in=cursor,
                    depth_requested=limit,
                    request_order_in_run=request_order,
                    request_order_in_window=window_request_order,
                    request_order_in_sweep=request_order,
                ),
            )
        except HttpError as err:
            if mode == "unauth" and is_auth_required_error(err):
                # Treat as a clean skip for unauth mode.
                raise
            raise

        labelers.record(viewer_mode=mode, content_labelers=resp.content_labelers)
        items = resp.data.get("feed")
        if not isinstance(items, list):
            raise RuntimeError("getFeed response missing feed[]")
        if not items:
            break

        post_uris: list[PostUri] = []
        author_dids: list[str] = []
        feed_rows: list[dict[str, Any]] = []
        metrics_rows: list[dict[str, Any]] = []
        label_rows: list[dict[str, Any]] = []
        first_seen_rows: list[dict[str, Any]] = []

        for item in items:
            if not isinstance(item, dict):
                continue
            post = item.get("post")
            if not isinstance(post, dict):
                continue
            post_uri = post.get("uri")
            if not isinstance(post_uri, str) or not post_uri:
                continue
            post_cid = post.get("cid") if isinstance(post.get("cid"), str) else None
            author = post.get("author") if isinstance(post.get("author"), dict) else {}
            author_did = author.get("did") if isinstance(author.get("did"), str) else None
            author_handle = author.get("handle") if isinstance(author.get("handle"), str) else None
            if author_did:
                author_dids.append(author_did)

            reason = item.get("reason") if isinstance(item.get("reason"), dict) else None
            reason_type = reason.get("$type") if isinstance(reason, dict) and isinstance(reason.get("$type"), str) else None
            reason_actor_did = None
            if isinstance(reason, dict):
                by = reason.get("by")
                if isinstance(by, dict) and isinstance(by.get("did"), str):
                    reason_actor_did = str(by.get("did"))

            rank += 1
            row_common = {
                "run_id": str(run_id),
                "sample_family": sample_family,
                "study_id": study_id,
                "panel_hash": panel_hash,
                "panel_version_id": panel_version_id,
                "snapshot_hour_utc": hour.hour_iso_z,
                "scheduled_window_start_utc": scheduled_window_start_utc,
                "scheduled_window_end_utc": scheduled_window_end_utc,
                "window_index": window_index,
                "window_minute": window_minute,
                "window_minutes": window_minutes,
                "randomization_seed": randomization_seed,
                "shard_id": shard_id,
                "shard_count": shard_count,
                "shard_membership_hash": shard_membership_hash,
                "captured_at_utc": captured_at_utc,
                "request_order_in_window": window_request_order,
                "request_order_in_sweep": request_order,
                "viewer_mode": mode,
                "vantage_id": vantage_id,
            }
            feed_rows.append(
                {
                    **row_common,
                    "feed_uri": str(feed_uri),
                    "bucket": bucket,
                    "rank": rank,
                    "post_uri": post_uri,
                    "post_cid": post_cid,
                    "author_did": author_did,
                    "author_handle": author_handle,
                    "reason_type": reason_type,
                    "reason_actor_did": reason_actor_did,
                }
            )

            post_uris.append(PostUri(post_uri))
            metrics_rows.append(
                {
                    **row_common,
                    "post_uri": post_uri,
                    "like_count": post.get("likeCount"),
                    "repost_count": post.get("repostCount"),
                    "reply_count": post.get("replyCount"),
                    "quote_count": post.get("quoteCount"),
                }
            )

            labels = post.get("labels")
            if isinstance(labels, list):
                for lab in labels:
                    if not isinstance(lab, dict):
                        continue
                    label_rows.append(
                        {
                            **row_common,
                            "post_uri": post_uri,
                            "post_cid": post_cid,
                            "label_src": lab.get("src"),
                            "label_val": lab.get("val"),
                            "label_neg": 1 if lab.get("neg") is True else 0 if lab.get("neg") is False else None,
                            "label_uri": lab.get("uri"),
                            "label_cts": lab.get("cts"),
                        }
                    )

            if include_author_labels:
                author = post.get("author")
                if isinstance(author, dict):
                    author_labels = author.get("labels")
                    if isinstance(author_labels, list):
                        for lab in author_labels:
                            if not isinstance(lab, dict):
                                continue
                            label_rows.append(
                                {
                                    **row_common,
                                    "post_uri": post_uri,
                                    "post_cid": post_cid,
                                    "label_src": lab.get("src"),
                                    "label_val": lab.get("val"),
                                    "label_neg": 1 if lab.get("neg") is True else 0 if lab.get("neg") is False else None,
                                    "label_uri": lab.get("uri"),
                                    "label_cts": lab.get("cts"),
                                }
                            )

            record = post.get("record") if isinstance(post.get("record"), dict) else {}
            text = record.get("text") if isinstance(record.get("text"), str) else None
            record_created_at = record.get("createdAt") if isinstance(record.get("createdAt"), str) else None
            indexed_at = post.get("indexedAt") if isinstance(post.get("indexedAt"), str) else None

            first_seen_rows.append(
                {
                    **row_common,
                    "feed_uri": str(feed_uri),
                    "bucket": bucket,
                    "post_uri": post_uri,
                    "post_cid": post_cid,
                    "author_did": author_did,
                    "author_handle": author_handle,
                    "record_created_at": record_created_at,
                    "indexed_at": indexed_at,
                    "text": text,
                }
            )

        # Write impressions + metrics immediately.
        n_feed = writers.feed_items.write_rows(feed_rows)
        progress.add_rows("feed_items", n_feed)
        n_metrics = writers.post_metrics.write_rows(metrics_rows)
        progress.add_rows("post_metrics", n_metrics)
        n_labels = writers.post_labels.write_rows(label_rows)
        progress.add_rows("post_labels", n_labels)

        # Dedupe registry + write first-seen only once.
        control.upsert_post_registry_many(post_uris=post_uris, seen_at_utc=captured_at_utc)
        control.upsert_author_registry_many(author_dids=author_dids, seen_at_utc=captured_at_utc)
        not_written = control.select_not_written(post_uris=post_uris)
        not_written_set = set(str(p) for p in not_written)
        rows_to_write = [r for r in first_seen_rows if str(r.get("post_uri")) in not_written_set]
        n_first = writers.posts_first_seen.write_rows(rows_to_write)
        progress.add_rows("posts_first_seen", n_first)
        writers.posts_first_seen.flush(force_fsync=False)
        control.mark_first_written(post_uris=not_written)
        control.commit()

        fetched += len(post_uris)
        cursor = resp.data.get("cursor") if isinstance(resp.data.get("cursor"), str) else None
        page_no += 1
        if not cursor:
            break
