from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bsky_collector_v2.fs_utils import atomic_write_json, ensure_dir
from bsky_collector_v2.http_client import AsyncHttpClient, HttpRetryConfig, XrpcHosts
from bsky_collector_v2.instrumentation import enrich_manifest
from bsky_collector_v2.layout import Layout
from bsky_collector_v2.progress import ProgressReporter, ProgressState
from bsky_collector_v2.public_views import flatten_generator_view
from bsky_collector_v2.request_provenance import JobRequestContextFactory, RequestProvenanceWriter
from bsky_collector_v2.state import ControlState
from bsky_collector_v2.time_utils import format_utc, now_utc, utc_date_str
from bsky_collector_v2.types import FeedUri, RunId
from bsky_collector_v2.writers import CsvPartWriter

logger = logging.getLogger("bsky_collector_v2.job.hydrate_feed_generators")


_FEED_GENERATOR_FIELDS: tuple[str, ...] = (
    "run_id",
    "vantage_id",
    "feed_uri",
    "feed_cid",
    "feed_did",
    "creator_did",
    "creator_handle",
    "creator_display_name",
    "display_name",
    "description",
    "avatar",
    "like_count",
    "accepts_interactions",
    "content_mode",
    "indexed_at",
    "labels_json",
    "is_online",
    "is_valid",
    "labelers_requested",
    "labelers_included",
    "captured_at_utc",
)


@dataclass(frozen=True)
class HydrateFeedGeneratorsConfig:
    max_feeds: int = 50_000
    include_hydrated: bool = False


async def run_hydrate_feed_generators(
    *,
    layout: Layout,
    hosts: XrpcHosts,
    run_id: RunId,
    rps: float,
    concurrency: int,
    dry_run: bool,
    cfg: HydrateFeedGeneratorsConfig | None = None,
    accept_language: str | None,
    accept_labelers: str | None,
    vantage_id: str,
) -> None:
    cfg = cfg or HydrateFeedGeneratorsConfig()
    date_str = utc_date_str(now_utc())
    out_dir = layout.feed_generators_day_dir(date_str)
    out_csv = out_dir / "feed_generator_profiles_part_000.csv"
    if dry_run:
        logger.info("dry_run=true: would hydrate feed generators out=%s", str(out_csv))
        return

    ensure_dir(out_dir)

    started_at_utc = format_utc(now_utc())
    manifest = {
        "run_id": str(run_id),
        "job_name": "hydrate-feed-generators",
        "date_utc": date_str,
        "started_at_utc": started_at_utc,
        "params": {
            "date": date_str,
            "max_feeds": cfg.max_feeds,
            "include_hydrated": bool(cfg.include_hydrated),
            "accept_language": accept_language,
            "accept_labelers": accept_labelers,
            "vantage_id": str(vantage_id).strip() or "unauth",
        },
    }
    enrich_manifest(manifest, job_name="hydrate-feed-generators", out_base=layout.out_base, params=manifest["params"])
    atomic_write_json(layout.feed_generators_manifest_json(date_str), manifest)

    progress_state = ProgressState(job_name="hydrate-feed-generators", run_id=run_id, started_at_utc=started_at_utc)
    progress_reporter = ProgressReporter(layout.feed_generators_progress_json(date_str), progress_state, write_interval_s=15.0)
    progress_reporter.start()
    http_stats_writer = CsvPartWriter(
        layout.feed_generators_http_stats_csv(date_str),
        fieldnames=["timestamp_utc", "endpoint", "status_code", "latency_ms", "attempt", "error_type", "feed_uri"],
    )
    vantage_value = str(vantage_id).strip() or "unauth"
    http = AsyncHttpClient(
        hosts=hosts,
        rps=rps,
        retry=HttpRetryConfig(max_retries=1),
        timeout_s=20.0,
        http_stats=http_stats_writer,
        progress=progress_state,
        accept_language=accept_language,
        accept_labelers=accept_labelers,
        request_provenance_writer=RequestProvenanceWriter(layout.feed_generators_request_provenance_csv(date_str)),
        request_context_factory=JobRequestContextFactory(
            run_id=str(run_id),
            job_name="hydrate-feed-generators",
            sample_family=str(manifest.get("sample_family") or "feed_generator_hydration"),
            collection_params_hash=str(manifest.get("collection_params_hash") or ""),
            appview_host=hosts.appview_host,
            pds_host=hosts.pds_host,
            date_utc=date_str,
            viewer_mode="unauth",
            vantage_id=vantage_value,
        ),
    )

    writer = CsvPartWriter(out_csv, fieldnames=_FEED_GENERATOR_FIELDS)
    hydrated_total = 0
    success = False

    try:
        with ControlState.open(layout.control_db_path) as control:
            control.start_run(
                run_id=run_id,
                job_name="hydrate-feed-generators",
                started_at_utc=started_at_utc,
                params={
                    "date": date_str,
                    "max_feeds": cfg.max_feeds,
                    "include_hydrated": bool(cfg.include_hydrated),
                },
            )
            try:
                to_hydrate = control.select_feed_generators_to_hydrate(
                    limit=cfg.max_feeds,
                    include_hydrated=bool(cfg.include_hydrated),
                )
                logger.info("hydrate-feed-generators start candidates=%s", len(to_hydrate))
                progress_state.feeds_total = len(to_hydrate)

                for feed_uri in to_hydrate:
                    captured_at_utc = format_utc(now_utc())
                    resp = await http.xrpc_get(
                        endpoint="app.bsky.feed.getFeedGenerator",
                        host=http.hosts.appview_host,
                        method="app.bsky.feed.getFeedGenerator",
                        params={"feed": str(feed_uri)},
                        access_jwt=None,
                        feed_uri=str(feed_uri),
                        timestamp_utc=captured_at_utc,
                    )
                    generator = resp.data.get("view") if isinstance(resp.data.get("view"), dict) else {}
                    row = {
                        "run_id": str(run_id),
                        "vantage_id": vantage_value,
                        **flatten_generator_view(generator),
                        "is_online": 1 if resp.data.get("isOnline") is True else 0 if resp.data.get("isOnline") is False else None,
                        "is_valid": 1 if resp.data.get("isValid") is True else 0 if resp.data.get("isValid") is False else None,
                        "labelers_requested": accept_labelers,
                        "labelers_included": resp.content_labelers,
                        "captured_at_utc": captured_at_utc,
                    }
                    writer.write_rows([row])
                    writer.flush(force_fsync=False)
                    control.mark_feed_generators_hydrated(feed_uris=[FeedUri(str(feed_uri))], hydrated_at_utc=captured_at_utc)
                    control.commit()
                    hydrated_total += 1
                    progress_state.feeds_done = hydrated_total

                success = True
                logger.info("hydrate-feed-generators done hydrated=%s", hydrated_total)
            finally:
                control.finish_run(run_id=run_id, finished_at_utc=format_utc(now_utc()), success=success)
    finally:
        writer.close()
        if http.request_provenance_writer is not None:
            http.request_provenance_writer.close()
        http_stats_writer.close()
        progress_reporter.stop()
        manifest["finished_at_utc"] = format_utc(now_utc())
        manifest["success"] = bool(success)
        atomic_write_json(layout.feed_generators_manifest_json(date_str), manifest)
        await http.aclose()
