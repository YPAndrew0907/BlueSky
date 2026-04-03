from __future__ import annotations

import asyncio
import csv
import json
import logging
import platform
import random
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bsky_collector_v2.fs_utils import atomic_write_json, ensure_dir, safe_cwd
from bsky_collector_v2.http_client import AsyncHttpClient, HttpError, HttpRetryConfig, XrpcHosts
from bsky_collector_v2.instrumentation import enrich_manifest
from bsky_collector_v2.layout import Layout
from bsky_collector_v2.progress import ProgressReporter, ProgressState
from bsky_collector_v2.public_views import extract_feed_item_features, extract_post_record_features
from bsky_collector_v2.quality import assess_wide_day
from bsky_collector_v2.request_provenance import (
    RequestContext,
    RequestOrderTracker,
    RequestProvenanceWriter,
    classify_host_kind,
)
from bsky_collector_v2.session import is_auth_required_error
from bsky_collector_v2.state import ControlState
from bsky_collector_v2.time_utils import floor_to_hour_utc, format_utc, now_utc, utc_date_str
from bsky_collector_v2.types import FeedUri, PostUri, RunId
from bsky_collector_v2.writers import CsvPartWriter

logger = logging.getLogger("bsky_collector_v2.job.wide_sweep")
REPO_ROOT_FALLBACK = Path(__file__).resolve().parents[2]


_FEED_ITEMS_FIELDS: tuple[str, ...] = (
    "run_id",
    "snapshot_hour_utc",
    "captured_at_utc",
    "viewer_mode",
    "vantage_id",
    "surface_type",
    "surface_id",
    "labelers_requested",
    "labelers_included",
    "feed_uri",
    "bucket",
    "page_no",
    "cursor_in",
    "cursor_out",
    "slot_no",
    "rank",
    "rank_approx",
    "post_uri",
    "post_cid",
    "post_indexed_at",
    "author_did",
    "author_handle",
    "reason_type",
    "reason_actor_did",
    "reason_actor_handle",
    "reason_repost_uri",
    "reason_repost_cid",
    "reason_repost_indexed_at",
    "reply_root_uri",
    "reply_parent_uri",
    "reply_grandparent_author_did",
    "feed_context",
    "req_id",
)


_POSTS_FIRST_SEEN_FIELDS: tuple[str, ...] = (
    "run_id",
    "snapshot_hour_utc",
    "captured_at_utc",
    "viewer_mode",
    "vantage_id",
    "surface_type",
    "surface_id",
    "labelers_requested",
    "labelers_included",
    "feed_uri",
    "bucket",
    "post_uri",
    "post_cid",
    "author_did",
    "author_handle",
    "record_created_at",
    "indexed_at",
    "text",
    "is_reply",
    "is_quote",
    "reply_root_uri",
    "reply_parent_uri",
    "embed_type",
    "media_embed_type",
    "has_image",
    "has_video",
    "has_external",
    "has_record_embed",
    "external_uri",
    "external_domain",
    "lang_primary",
    "lang_count",
    "langs_json",
    "tag_count",
    "tags_json",
    "facets_count",
    "mention_count",
    "link_count",
    "hashtag_count",
    "self_label_values_json",
    "post_label_values_json",
    "author_label_values_json",
    "contains_no_unauthenticated",
    "contains_hide_like_label",
)


_POST_METRICS_FIELDS: tuple[str, ...] = (
    "run_id",
    "snapshot_hour_utc",
    "captured_at_utc",
    "viewer_mode",
    "vantage_id",
    "labelers_requested",
    "labelers_included",
    "post_uri",
    "like_count",
    "repost_count",
    "reply_count",
    "quote_count",
)


_POST_LABEL_FIELDS: tuple[str, ...] = (
    "run_id",
    "snapshot_hour_utc",
    "captured_at_utc",
    "viewer_mode",
    "vantage_id",
    "labelers_requested",
    "labelers_included",
    "post_uri",
    "post_cid",
    "label_target",
    "label_src",
    "label_val",
    "label_neg",
    "label_uri",
    "label_cts",
)



@dataclass
class WorkerWriters:
    feed_items: CsvPartWriter
    posts_first_seen: CsvPartWriter
    post_metrics: CsvPartWriter
    post_labels: CsvPartWriter


def _read_panel_feed_uris(panel_path: Path) -> set[str]:
    if not panel_path.exists():
        return set()
    out: set[str] = set()
    with open(panel_path, "r", encoding="utf-8", newline="") as f:
        r = csv.DictReader(f)
        for row in r:
            uri = (row.get("feed_uri") or "").strip()
            if uri:
                out.add(uri)
    return out


def _load_or_init_manifest(
    *,
    manifest_path: Path,
    out_base: Path,
    date_str: str,
    resume: bool,
    started_at_utc: str,
    params: dict[str, Any],
) -> RunId:
    existing: dict[str, Any] | None = None
    if manifest_path.exists():
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

    from bsky_collector_v2.manifest import git_sha as current_git_sha
    from bsky_collector_v2.manifest import new_run_id

    if not run_id:
        run_id = str(new_run_id())

    manifest: dict[str, Any] = dict(existing or {})
    prior_started = manifest.get("started_at_utc")
    manifest_started = prior_started if isinstance(prior_started, str) and prior_started else started_at_utc
    manifest.update(
        {
            "run_id": run_id,
            "job_name": "wide-sweep",
            "date_utc": date_str,
            "started_at_utc": manifest_started,
            "git_sha": manifest.get("git_sha") or current_git_sha(safe_cwd(fallback=REPO_ROOT_FALLBACK)),
            "hostname": manifest.get("hostname") or platform.node(),
            "python": manifest.get("python") or platform.python_version(),
            "platform": manifest.get("platform") or platform.platform(),
            "params": dict(params),
        }
    )
    enrich_manifest(
        manifest,
        job_name="wide-sweep",
        out_base=out_base,
        params=params,
    )
    atomic_write_json(manifest_path, manifest)
    return RunId(run_id)


def _open_worker_writers(*, parts_dir: Path, worker_idx: int) -> WorkerWriters:
    return WorkerWriters(
        feed_items=CsvPartWriter(parts_dir / f"feed_items_part_{worker_idx:03d}.csv", fieldnames=_FEED_ITEMS_FIELDS),
        posts_first_seen=CsvPartWriter(
            parts_dir / f"posts_first_seen_part_{worker_idx:03d}.csv", fieldnames=_POSTS_FIRST_SEEN_FIELDS
        ),
        post_metrics=CsvPartWriter(parts_dir / f"post_metrics_part_{worker_idx:03d}.csv", fieldnames=_POST_METRICS_FIELDS),
        post_labels=CsvPartWriter(parts_dir / f"post_labels_part_{worker_idx:03d}.csv", fieldnames=_POST_LABEL_FIELDS),
    )


def _close_worker_writers(w: WorkerWriters) -> None:
    w.feed_items.close()
    w.posts_first_seen.close()
    w.post_metrics.close()
    w.post_labels.close()


async def run_wide_sweep(
    *,
    layout: Layout,
    hosts: XrpcHosts,
    n_feeds: int,
    posts_per_feed: int,
    rps: float,
    concurrency: int,
    time_budget_minutes: int,
    feed_time_budget_s: float,
    resume: bool,
    dry_run: bool,
    accept_language: str | None,
    accept_labelers: str | None,
    include_author_labels: bool,
    vantage_id: str,
) -> None:
    date_str = utc_date_str(now_utc())
    if dry_run:
        out_dir = layout.wide_day_dir(date_str)
        logger.info("dry_run=true: would wide-sweep date=%s out=%s", date_str, str(out_dir))
        return

    vantage_id = str(vantage_id).strip() or "unauth"

    out_dir = layout.wide_day_dir(date_str)
    parts_dir = layout.wide_parts_dir(date_str)
    ensure_dir(parts_dir)

    started_at_utc = format_utc(now_utc())
    run_id = _load_or_init_manifest(
        manifest_path=layout.wide_manifest_json(date_str),
        out_base=layout.out_base,
        date_str=date_str,
        resume=resume,
        started_at_utc=started_at_utc,
        params={
            "date_utc": date_str,
            "n_feeds": int(n_feeds),
            "posts_per_feed": int(posts_per_feed),
            "rps": float(rps),
            "concurrency": int(concurrency),
            "time_budget_minutes": int(time_budget_minutes),
            "feed_time_budget_s": float(feed_time_budget_s),
            "resume": bool(resume),
            "accept_language": accept_language,
            "accept_labelers": accept_labelers,
            "include_author_labels": bool(include_author_labels),
            "vantage_id": vantage_id,
        },
    )
    manifest = json.loads(layout.wide_manifest_json(date_str).read_text(encoding="utf-8"))
    sample_family = str(manifest.get("sample_family") or "wide")
    collection_params_hash = str(manifest.get("collection_params_hash") or "")

    progress = ProgressState(job_name="wide-sweep", run_id=run_id, started_at_utc=started_at_utc)
    progress.rps_config = rps
    progress.concurrency = concurrency

    reporter = ProgressReporter(layout.wide_progress_json(date_str), progress)
    reporter.start()

    http_stats_writer = CsvPartWriter(
        layout.wide_http_stats_csv(date_str),
        fieldnames=["timestamp_utc", "endpoint", "status_code", "latency_ms", "attempt", "error_type", "feed_uri"],
    )
    request_provenance_writer = RequestProvenanceWriter(layout.wide_request_provenance_csv(date_str))
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

    panel_feed_uris = _read_panel_feed_uris(layout.panel_active_csv)
    labelers_included: set[str] = set()
    request_order_tracker = RequestOrderTracker()

    try:
        with ControlState.open(layout.control_db_path) as control:
            control.start_run(
                run_id=run_id,
                job_name="wide-sweep",
                started_at_utc=started_at_utc,
                params={"date": date_str},
            )
            success = False
            error: str | None = None
            try:
                # Build deterministic feed sample from current catalog (excluding panel).
                candidates: list[str] = []
                for row in control.iter_feed_catalog():
                    uri = row["feed_uri"]
                    if isinstance(uri, str) and uri and uri not in panel_feed_uris:
                        candidates.append(uri)
                if not candidates:
                    raise RuntimeError("feed_catalog is empty; run refresh-discovery first")

                candidates = sorted(set(candidates))
                seed = int.from_bytes(date_str.encode("utf-8"), "little") & 0xFFFFFFFF
                rng = random.Random(seed)
                if len(candidates) > n_feeds:
                    sample = rng.sample(candidates, n_feeds)
                else:
                    sample = candidates

                sample_feed_uris = [FeedUri(u) for u in sorted(sample)]
                control.ensure_wide_tasks(date_utc=date_str, feed_uris=sample_feed_uris, updated_at_utc=started_at_utc)
                control.commit()

                counts = control.count_wide_by_status(date_utc=date_str)
                progress.feeds_total = sum(counts.values())
                progress.feeds_done = int(counts.get("success", 0))
                progress.feeds_failed = int(counts.get("failed", 0))

                queue: asyncio.Queue[FeedUri] = asyncio.Queue()
                pending_tasks = list(control.wide_pending_tasks(date_utc=date_str, max_attempts=3))
                random.Random(f"{run_id}:{format_utc(now_utc())}").shuffle(pending_tasks)
                for feed_uri, _attempts, _status in pending_tasks:
                    queue.put_nowait(feed_uri)

                budget_s = max(60.0, float(time_budget_minutes) * 60.0)
                stop_at = time.monotonic() + budget_s

                pending_n = queue.qsize()
                if pending_n == 0:
                    logger.info("wide-sweep nothing to do date=%s (all tasks complete)", date_str)
                    success = True
                    return

                worker_n = max(1, min(concurrency, 64, pending_n))
                logger.info("wide-sweep start date=%s workers=%s tasks=%s", date_str, worker_n, pending_n)

                async def worker(worker_idx: int) -> None:
                    writers = _open_worker_writers(parts_dir=parts_dir, worker_idx=worker_idx)
                    try:
                        while True:
                            if time.monotonic() >= stop_at:
                                return
                            try:
                                feed_uri = queue.get_nowait()
                            except asyncio.QueueEmpty:
                                return
                            captured_at_utc = format_utc(now_utc())
                            prev = (
                                control.mark_wide_in_progress(
                                    date_utc=date_str, feed_uri=feed_uri, started_at_utc=captured_at_utc
                                )
                                or "pending"
                            )
                            # Release DB write lock before network I/O to reduce cross-job contention.
                            control.commit()
                            ok = False
                            last_err: str | None = None
                            try:
                                feed_coro = _sweep_one_feed(
                                    http=http,
                                    control=control,
                                    writers=writers,
                                    run_id=run_id,
                                    feed_uri=feed_uri,
                                    include_author_labels=include_author_labels,
                                    posts_per_feed=posts_per_feed,
                                    captured_at_utc=captured_at_utc,
                                    progress=progress,
                                    vantage_id=vantage_id,
                                    labelers_included=labelers_included,
                                    sample_family=sample_family,
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
                                control.mark_wide_done(
                                    date_utc=date_str,
                                    feed_uri=feed_uri,
                                    success=ok,
                                    finished_at_utc=finished_at_utc,
                                    last_error=last_err,
                                )
                                with progress.lock:
                                    if ok:
                                        if prev == "failed":
                                            progress.feeds_failed = max(0, progress.feeds_failed - 1)
                                        progress.feeds_done += 1
                                    else:
                                        if prev == "pending":
                                            progress.feeds_failed += 1
                                control.commit()
                            queue.task_done()
                    finally:
                        _close_worker_writers(writers)

                async with asyncio.TaskGroup() as tg:
                    for i in range(worker_n):
                        tg.create_task(worker(i))

                logger.info(
                    "wide-sweep done date=%s done=%s failed=%s",
                    date_str,
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
                        layout.wide_manifest_json(date_str),
                        finished_at_utc=now_utc(),
                        success=success,
                        error=error,
                        extra={"labelers_included": sorted(labelers_included)},
                    )
                except Exception:  # noqa: BLE001
                    pass

    finally:
        await http.aclose()
        http_stats_writer.close()
        request_provenance_writer.close()
        reporter.stop()
        try:
            from bsky_collector_v2.effective_csv import refresh_key_views, sync_wide_day

            sync_wide_day(layout, date_yyyy_mm_dd=date_str)
            refresh_key_views(layout)
        except Exception as err:  # noqa: BLE001
            logger.warning("effective csv sync failed job=wide-sweep date=%s err=%r", date_str, err)
        try:
            atomic_write_json(layout.wide_quality_report_json(date_str), assess_wide_day(layout, date_yyyy_mm_dd=date_str))
        except Exception as err:  # noqa: BLE001
            logger.warning("quality report write failed job=wide-sweep date=%s err=%r", date_str, err)


async def _sweep_one_feed(
    *,
    http: AsyncHttpClient,
    control: ControlState,
    writers: WorkerWriters,
    run_id: RunId,
    feed_uri: FeedUri,
    include_author_labels: bool,
    posts_per_feed: int,
    captured_at_utc: str,
    progress: ProgressState,
    vantage_id: str,
    labelers_included: set[str],
    sample_family: str | None = None,
    collection_params_hash: str | None = None,
    request_order_tracker: RequestOrderTracker | None = None,
) -> None:
    fetched = 0
    cursor: str | None = None
    rank = 0
    bucket = "wide_sweep"
    viewer_mode = "unauth"
    vantage_id = str(vantage_id).strip() or viewer_mode
    sample_family = str(sample_family or "wide")
    collection_params_hash = str(collection_params_hash or "")
    request_order_tracker = request_order_tracker or RequestOrderTracker()
    host_kind = classify_host_kind(
        host=http.hosts.appview_host,
        appview_host=http.hosts.appview_host,
        pds_host=getattr(http.hosts, "pds_host", http.hosts.appview_host),
        access_jwt=None,
    )

    snapshot_hour_utc = format_utc(floor_to_hour_utc(datetime.fromisoformat(captured_at_utc.replace("Z", "+00:00"))))
    page_no = 0

    while fetched < posts_per_feed:
        limit = min(100, posts_per_feed - fetched)
        request_order = request_order_tracker.next()
        resp = await http.xrpc_get(
            endpoint="app.bsky.feed.getFeed",
            host=http.hosts.appview_host,
            method="app.bsky.feed.getFeed",
            params={"feed": str(feed_uri), "limit": limit, **({"cursor": cursor} if cursor else {})},
            access_jwt=None,
            feed_uri=str(feed_uri),
            timestamp_utc=captured_at_utc,
            request_context=RequestContext(
                run_id=str(run_id),
                job_name="wide-sweep",
                sample_family=sample_family,
                collection_params_hash=collection_params_hash,
                snapshot_hour_utc=snapshot_hour_utc,
                date_utc=snapshot_hour_utc[:10],
                viewer_mode=viewer_mode,
                vantage_id=vantage_id,
                host_kind=host_kind,
                host=http.hosts.appview_host,
                endpoint="app.bsky.feed.getFeed",
                feed_uri=str(feed_uri),
                page_no=page_no,
                cursor_in=cursor,
                depth_requested=limit,
                request_order_in_run=request_order,
                request_order_in_sweep=request_order,
            ),
        )

        if resp.content_labelers:
            parts = [p.strip() for p in str(resp.content_labelers).split(",") if p.strip()]
            if parts:
                labelers_included.update(parts)

        items = resp.data.get("feed")
        if not isinstance(items, list) or not items:
            break

        post_uris: list[PostUri] = []
        author_dids: list[str] = []
        feed_rows: list[dict[str, Any]] = []
        metrics_rows: list[dict[str, Any]] = []
        label_rows: list[dict[str, Any]] = []
        first_seen_rows: list[dict[str, Any]] = []
        _accept_labelers = getattr(http, "accept_labelers", None)
        labelers_requested = _accept_labelers.strip() if isinstance(_accept_labelers, str) and _accept_labelers.strip() else None
        labelers_included_value = resp.content_labelers
        cursor_out = resp.data.get("cursor") if isinstance(resp.data.get("cursor"), str) else None

        for item_idx, item in enumerate(items, start=1):
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

            rank += 1
            row_common = {
                "run_id": str(run_id),
                "snapshot_hour_utc": snapshot_hour_utc,
                "captured_at_utc": captured_at_utc,
                "viewer_mode": viewer_mode,
                "vantage_id": vantage_id,
                "surface_type": "feed",
                "surface_id": str(feed_uri),
                "labelers_requested": labelers_requested,
                "labelers_included": labelers_included_value,
            }
            feed_features = extract_feed_item_features(item)
            post_features = extract_post_record_features(post)
            feed_rows.append(
                {
                    **row_common,
                    "feed_uri": str(feed_uri),
                    "bucket": bucket,
                    "page_no": page_no,
                    "cursor_in": cursor,
                    "cursor_out": cursor_out,
                    "slot_no": rank,
                    "rank": rank,
                    "rank_approx": rank,
                    "post_uri": post_uri,
                    "post_cid": post_cid,
                    "post_indexed_at": post_features.get("indexed_at"),
                    "author_did": author_did,
                    "author_handle": author_handle,
                    **feed_features,
                }
            )

            post_uris.append(PostUri(post_uri))
            metrics_rows.append(
                {
                    **{k: v for k, v in row_common.items() if k not in {"surface_type", "surface_id"}},
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
                            **{k: v for k, v in row_common.items() if k not in {"surface_type", "surface_id"}},
                            "post_uri": post_uri,
                            "post_cid": post_cid,
                            "label_target": "post",
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
                                    **{k: v for k, v in row_common.items() if k not in {"surface_type", "surface_id"}},
                                    "post_uri": post_uri,
                                    "post_cid": post_cid,
                                    "label_target": "author",
                                    "label_src": lab.get("src"),
                                    "label_val": lab.get("val"),
                                    "label_neg": 1 if lab.get("neg") is True else 0 if lab.get("neg") is False else None,
                                    "label_uri": lab.get("uri"),
                                    "label_cts": lab.get("cts"),
                                }
                            )

            first_seen_rows.append(
                {
                    **row_common,
                    "feed_uri": str(feed_uri),
                    "bucket": bucket,
                    "post_uri": post_uri,
                    "post_cid": post_cid,
                    "author_did": author_did,
                    "author_handle": author_handle,
                    **post_features,
                }
            )

        n_feed = writers.feed_items.write_rows(feed_rows)
        progress.add_rows("feed_items", n_feed)
        n_metrics = writers.post_metrics.write_rows(metrics_rows)
        progress.add_rows("post_metrics", n_metrics)
        n_labels = writers.post_labels.write_rows(label_rows)
        progress.add_rows("post_labels", n_labels)

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
