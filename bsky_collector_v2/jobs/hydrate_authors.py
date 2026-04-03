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
from bsky_collector_v2.public_views import flatten_profile_view_detailed
from bsky_collector_v2.quality import assess_authors_day
from bsky_collector_v2.request_provenance import JobRequestContextFactory, RequestProvenanceWriter
from bsky_collector_v2.state import ControlState
from bsky_collector_v2.time_utils import format_utc, now_utc, utc_date_str
from bsky_collector_v2.types import RunId
from bsky_collector_v2.writers import CsvPartWriter

logger = logging.getLogger("bsky_collector_v2.job.hydrate_authors")


_AUTHOR_FIELDS: tuple[str, ...] = (
    "run_id",
    "vantage_id",
    "author_did",
    "handle",
    "display_name",
    "description",
    "website",
    "avatar",
    "banner",
    "followers_count",
    "follows_count",
    "posts_count",
    "associated_json",
    "joined_via_starter_pack_uri",
    "indexed_at",
    "created_at",
    "labels_json",
    "pinned_post_uri",
    "verification_json",
    "status_json",
    "captured_at_utc",
)


@dataclass(frozen=True)
class HydrateAuthorsConfig:
    batch_size: int = 25
    max_authors: int = 50_000
    seen_after_utc: str | None = None
    seen_before_utc: str | None = None


async def run_hydrate_authors(
    *,
    layout: Layout,
    hosts: XrpcHosts,
    run_id: RunId,
    rps: float,
    concurrency: int,
    dry_run: bool,
    cfg: HydrateAuthorsConfig | None = None,
    accept_language: str | None,
    accept_labelers: str | None,
    vantage_id: str,
) -> None:
    cfg = cfg or HydrateAuthorsConfig()
    date_str = utc_date_str(now_utc())
    out_dir = layout.authors_day_dir(date_str)
    out_csv = out_dir / "author_profiles_part_000.csv"
    if dry_run:
        logger.info("dry_run=true: would hydrate authors out=%s", str(out_csv))
        return

    ensure_dir(out_dir)

    started_at_utc = format_utc(now_utc())
    manifest = {
        "run_id": str(run_id),
        "job_name": "hydrate-authors",
        "date_utc": date_str,
        "started_at_utc": started_at_utc,
        "params": {
            "date": date_str,
            "seen_after_utc": cfg.seen_after_utc,
            "seen_before_utc": cfg.seen_before_utc,
            "max_authors": cfg.max_authors,
            "batch_size": cfg.batch_size,
            "accept_language": accept_language,
            "accept_labelers": accept_labelers,
            "vantage_id": str(vantage_id).strip() or "unauth",
        },
    }
    enrich_manifest(manifest, job_name="hydrate-authors", out_base=layout.out_base, params=manifest["params"])
    atomic_write_json(layout.authors_manifest_json(date_str), manifest)
    progress_state = ProgressState(job_name="hydrate-authors", run_id=run_id, started_at_utc=started_at_utc)
    progress_reporter = ProgressReporter(layout.authors_progress_json(date_str), progress_state, write_interval_s=15.0)
    progress_reporter.start()
    http_stats_writer = CsvPartWriter(
        layout.authors_http_stats_csv(date_str),
        fieldnames=["timestamp_utc", "endpoint", "status_code", "latency_ms", "attempt", "error_type", "feed_uri"],
    )
    http = AsyncHttpClient(
        hosts=hosts,
        rps=rps,
        retry=HttpRetryConfig(max_retries=1),
        timeout_s=20.0,
        http_stats=http_stats_writer,
        progress=progress_state,
        accept_language=accept_language,
        accept_labelers=accept_labelers,
        request_provenance_writer=RequestProvenanceWriter(layout.authors_request_provenance_csv(date_str)),
        request_context_factory=JobRequestContextFactory(
            run_id=str(run_id),
            job_name="hydrate-authors",
            sample_family=str(manifest.get("sample_family") or "author_profile_hydration"),
            collection_params_hash=str(manifest.get("collection_params_hash") or ""),
            appview_host=hosts.appview_host,
            pds_host=hosts.pds_host,
            date_utc=date_str,
            viewer_mode="unauth",
            vantage_id=str(vantage_id).strip() or "unauth",
        ),
    )

    writer = CsvPartWriter(out_csv, fieldnames=_AUTHOR_FIELDS)
    hydrated_total = 0
    success = False

    try:
        with ControlState.open(layout.control_db_path) as control:
            control.start_run(
                run_id=run_id,
                job_name="hydrate-authors",
                started_at_utc=started_at_utc,
                params={
                    "date": date_str,
                    "seen_after_utc": cfg.seen_after_utc,
                    "seen_before_utc": cfg.seen_before_utc,
                    "max_authors": cfg.max_authors,
                    "batch_size": cfg.batch_size,
                },
            )
            try:
                to_hydrate = control.select_authors_to_hydrate(
                    limit=cfg.max_authors,
                    seen_after_utc=cfg.seen_after_utc,
                    seen_before_utc=cfg.seen_before_utc,
                )
                logger.info("hydrate-authors start candidates=%s batch_size=%s", len(to_hydrate), cfg.batch_size)
                progress_state.feeds_total = len(to_hydrate)

                for i in range(0, len(to_hydrate), cfg.batch_size):
                    batch = to_hydrate[i : i + cfg.batch_size]
                    captured_at_utc = format_utc(now_utc())
                    resp = await http.xrpc_get(
                        endpoint="app.bsky.actor.getProfiles",
                        host=http.hosts.appview_host,
                        method="app.bsky.actor.getProfiles",
                        params={"actors": batch},
                        access_jwt=None,
                        feed_uri=None,
                        timestamp_utc=captured_at_utc,
                    )
                    profiles = resp.data.get("profiles")
                    if not isinstance(profiles, list):
                        continue

                    hydrated: list[str] = []
                    rows: list[dict[str, Any]] = []
                    for p in profiles:
                        if not isinstance(p, dict):
                            continue
                        did = p.get("did")
                        if not isinstance(did, str) or not did:
                            continue
                        hydrated.append(did)
                        rows.append(
                            {
                                "run_id": str(run_id),
                                "vantage_id": str(vantage_id).strip() or "unauth",
                                "author_did": did,
                                **flatten_profile_view_detailed(p),
                                "captured_at_utc": captured_at_utc,
                            }
                        )

                    writer.write_rows(rows)
                    writer.flush(force_fsync=False)
                    control.mark_authors_hydrated(author_dids=hydrated, hydrated_at_utc=captured_at_utc)
                    control.commit()
                    hydrated_total += len(hydrated)
                    progress_state.feeds_done = hydrated_total

                success = True
                logger.info("hydrate-authors done hydrated=%s", hydrated_total)
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
        atomic_write_json(layout.authors_manifest_json(date_str), manifest)
        await http.aclose()
        try:
            from bsky_collector_v2.effective_csv import refresh_key_views, sync_authors_day

            sync_authors_day(layout, date_yyyy_mm_dd=date_str)
            refresh_key_views(layout)
        except Exception as err:  # noqa: BLE001
            logger.warning("effective csv sync failed job=hydrate-authors date=%s err=%r", date_str, err)
        try:
            atomic_write_json(layout.authors_quality_report_json(date_str), assess_authors_day(layout, date_yyyy_mm_dd=date_str))
        except Exception as err:  # noqa: BLE001
            logger.warning("quality report write failed job=hydrate-authors date=%s err=%r", date_str, err)
