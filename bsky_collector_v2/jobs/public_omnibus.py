from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from bsky_collector_v2.fs_utils import atomic_write_json, ensure_dir, safe_cwd
from bsky_collector_v2.http_client import XrpcHosts
from bsky_collector_v2.instrumentation import enrich_manifest
from bsky_collector_v2.jobs.backfill_interactions import BackfillInteractionsConfig, run_backfill_interactions
from bsky_collector_v2.jobs.backfill_rq1_factors import BackfillRq1FactorsConfig, run_backfill_rq1_factors
from bsky_collector_v2.jobs.build_panel import PanelBuildConfig, run_build_panel
from bsky_collector_v2.jobs.hydrate_authors import HydrateAuthorsConfig, run_hydrate_authors
from bsky_collector_v2.jobs.hydrate_feed_generators import HydrateFeedGeneratorsConfig, run_hydrate_feed_generators
from bsky_collector_v2.jobs.index_feed_generators import run_index_feed_generators
from bsky_collector_v2.jobs.micro_snapshot_study import run_micro_snapshot_study
from bsky_collector_v2.jobs.refresh_discovery import run_refresh_discovery
from bsky_collector_v2.jobs.seed_post_registry import SeedPostRegistryConfig, run_seed_post_registry
from bsky_collector_v2.jobs.snapshot_panel import run_snapshot_panel
from bsky_collector_v2.jobs.wide_sweep import run_wide_sweep
from bsky_collector_v2.layout import Layout
from bsky_collector_v2.manifest import new_run_id
from bsky_collector_v2.study import floor_to_window_utc, load_study_manifest
from bsky_collector_v2.time_utils import format_utc, now_utc
from bsky_collector_v2.types import RunId

logger = logging.getLogger("bsky_collector_v2.job.public_omnibus")
REPO_ROOT_FALLBACK = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class PublicOmnibusConfig:
    seed_registry: bool = True
    include_posts_first_seen: bool = True
    enqueue_interactions_from_seed: bool = True
    enqueue_rq1_factors_from_seed: bool = True
    run_index_feed_generators: bool = True
    run_refresh_discovery: bool = True
    run_build_panel: bool = True
    run_snapshot_panel: bool = True
    run_wide_sweep: bool = True
    run_hydrate_authors: bool = True
    run_hydrate_feed_generators: bool = True
    run_backfill_interactions: bool = True
    run_backfill_rq1_factors: bool = True
    run_micro_studies: bool = True
    all_studies: bool = True
    study_ids: tuple[str, ...] = ()
    seed_max_files: int = 0
    seed_max_rows: int = 0
    snapshot_hour_utc: datetime | None = None
    micro_window_start_utc: datetime | None = None
    n_feeds_wide: int = 5000
    max_authors: int = 200_000
    max_feed_generators: int = 200_000
    max_posts_interactions: int = 200_000
    max_posts_rq1: int = 200_000
    batch_size_interactions: int = 25
    batch_size_rq1: int = 25
    max_items_per_endpoint_interactions: int = 0
    max_items_per_endpoint_rq1: int = 0
    max_thread_depth: int = 1000
    max_thread_parent_height: int = 1000
    max_author_feed_items: int = 0
    max_followers_per_actor: int = 0
    max_follows_per_actor: int = 0
    max_follow_records_per_actor: int = 0
    max_actor_feeds_per_actor: int = 0
    max_lists_per_actor: int = 0
    max_list_members_per_list: int = 0
    max_starter_packs_per_actor: int = 0
    seen_after_utc: str | None = None
    seen_before_utc: str | None = None
    include_hydrated_interactions: bool = False
    include_hydrated_rq1: bool = False
    resolve_pds_endpoints: bool = True
    follow_record_scope: str = "seed+graph"
    shard_index: int = 0
    shard_count: int = 1
    panel_k1_popular: int = 700
    panel_k2_onboarding: int = 300
    panel_k3_suggested: int = 300
    panel_k4_longtail: int = 200


@dataclass(frozen=True)
class PublicOmnibusSummary:
    run_id: str
    started_at_utc: str
    finished_at_utc: str
    success: bool
    step_results: list[dict[str, Any]] = field(default_factory=list)
    studies_run: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": str(self.run_id),
            "started_at_utc": str(self.started_at_utc),
            "finished_at_utc": str(self.finished_at_utc),
            "success": bool(self.success),
            "step_results": [dict(item) for item in self.step_results],
            "studies_run": [str(item) for item in self.studies_run],
        }


def _manifest_path(layout: Layout) -> Path:
    return layout.control_root / "public_omnibus_last_run.json"


def _resolve_study_ids(layout: Layout, cfg: PublicOmnibusConfig) -> list[str]:
    requested = [str(study_id).strip() for study_id in cfg.study_ids if str(study_id).strip()]
    discovered: list[str] = []
    if cfg.all_studies and layout.studies_root.exists():
        for manifest_path in sorted(layout.studies_root.glob("*/study_manifest.json")):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            study_id = str(manifest.get("study_id") or manifest_path.parent.name).strip()
            if study_id:
                discovered.append(study_id)
    return list(dict.fromkeys(requested + discovered))


def _resolve_micro_window_start(*, layout: Layout, study_id: str, explicit: datetime | None) -> datetime:
    if explicit is not None:
        return explicit
    manifest = load_study_manifest(layout.study_manifest_json(study_id))
    window_minutes = int(manifest.get("intended_window_minutes") or manifest.get("window_size_minutes") or 5)
    return floor_to_window_utc(now_utc(), window_minutes=window_minutes)


async def run_public_omnibus(
    *,
    layout: Layout,
    hosts: XrpcHosts,
    relay_host: str,
    run_id: RunId,
    rps: float,
    concurrency: int,
    posts_per_feed: int,
    time_budget_minutes: int,
    feed_time_budget_s: float,
    dry_run: bool,
    resume: bool,
    accept_language: str | None,
    accept_labelers: str | None,
    include_author_labels: bool,
    vantage_id_unauth: str,
    cfg: PublicOmnibusConfig | None = None,
) -> PublicOmnibusSummary:
    cfg = cfg or PublicOmnibusConfig()
    repo_root = safe_cwd(fallback=REPO_ROOT_FALLBACK)
    started_at_utc = format_utc(now_utc())
    ensure_dir(layout.control_root)
    manifest_path = _manifest_path(layout)
    public_viewer_modes = ("unauth",)
    step_results: list[dict[str, Any]] = []
    studies_run: list[str] = []
    success = False

    manifest: dict[str, Any] = {
        "run_id": str(run_id),
        "job_name": "collect-public-omnibus",
        "started_at_utc": started_at_utc,
        "params": {
            "seed_registry": bool(cfg.seed_registry),
            "include_posts_first_seen": bool(cfg.include_posts_first_seen),
            "enqueue_interactions_from_seed": bool(cfg.enqueue_interactions_from_seed),
            "enqueue_rq1_factors_from_seed": bool(cfg.enqueue_rq1_factors_from_seed),
            "run_index_feed_generators": bool(cfg.run_index_feed_generators),
            "run_refresh_discovery": bool(cfg.run_refresh_discovery),
            "run_build_panel": bool(cfg.run_build_panel),
            "run_snapshot_panel": bool(cfg.run_snapshot_panel),
            "run_wide_sweep": bool(cfg.run_wide_sweep),
            "run_hydrate_authors": bool(cfg.run_hydrate_authors),
            "run_hydrate_feed_generators": bool(cfg.run_hydrate_feed_generators),
            "run_backfill_interactions": bool(cfg.run_backfill_interactions),
            "run_backfill_rq1_factors": bool(cfg.run_backfill_rq1_factors),
            "run_micro_studies": bool(cfg.run_micro_studies),
            "all_studies": bool(cfg.all_studies),
            "study_ids": list(cfg.study_ids),
            "seed_max_files": int(cfg.seed_max_files),
            "seed_max_rows": int(cfg.seed_max_rows),
            "n_feeds_wide": int(cfg.n_feeds_wide),
            "max_authors": int(cfg.max_authors),
            "max_feed_generators": int(cfg.max_feed_generators),
            "max_posts_interactions": int(cfg.max_posts_interactions),
            "max_posts_rq1": int(cfg.max_posts_rq1),
            "public_viewer_modes": list(public_viewer_modes),
            "accept_language": accept_language,
            "accept_labelers": accept_labelers,
            "include_author_labels": bool(include_author_labels),
            "vantage_id": str(vantage_id_unauth).strip() or "unauth",
            "relay_host": str(relay_host),
            "rps": float(rps),
            "concurrency": int(concurrency),
            "posts_per_feed": int(posts_per_feed),
            "time_budget_minutes": int(time_budget_minutes),
            "feed_time_budget_s": float(feed_time_budget_s),
            "resume": bool(resume),
            "dry_run": bool(dry_run),
            "public_only": True,
        },
    }
    enrich_manifest(manifest, job_name="collect-public-omnibus", out_base=layout.out_base, params=manifest["params"])
    atomic_write_json(manifest_path, manifest)

    async def _run_step(step_name: str, coro_factory, *, extra: dict[str, Any] | None = None) -> None:
        logger.info("public-omnibus step start step=%s", step_name)
        await coro_factory()
        result = {"step": step_name, "status": "ok"}
        if extra:
            result.update(extra)
        step_results.append(result)
        manifest["step_results"] = step_results
        atomic_write_json(manifest_path, manifest)
        logger.info("public-omnibus step complete step=%s", step_name)

    try:
        if cfg.seed_registry:
            seed_summary = run_seed_post_registry(
                layout=layout,
                run_id=new_run_id(),
                dry_run=dry_run,
                cfg=SeedPostRegistryConfig(
                    include_hourly=True,
                    include_wide=True,
                    include_micro5=True,
                    include_posts_first_seen=bool(cfg.include_posts_first_seen),
                    max_files=int(cfg.seed_max_files),
                    max_rows=int(cfg.seed_max_rows),
                    enqueue_interactions=bool(cfg.enqueue_interactions_from_seed),
                    enqueue_rq1_factors=bool(cfg.enqueue_rq1_factors_from_seed),
                    mark_first_written=True,
                ),
            )
            step_results.append({"step": "seed-post-registry", "status": "ok", "summary": seed_summary.to_dict()})
            manifest["step_results"] = step_results
            atomic_write_json(manifest_path, manifest)

        if cfg.run_index_feed_generators:
            await _run_step(
                "index-feed-generators",
                lambda: run_index_feed_generators(
                    layout=layout,
                    repo_root=repo_root,
                    hosts=hosts,
                    relay_host=str(relay_host),
                    env_path=None,
                    rps=rps,
                    time_budget_minutes=time_budget_minutes,
                    resume=resume,
                    dry_run=dry_run,
                    accept_language=accept_language,
                    accept_labelers=accept_labelers,
                    vantage_id=str(vantage_id_unauth).strip() or "unauth",
                ),
            )

        if cfg.run_refresh_discovery:
            await _run_step(
                "refresh-discovery",
                lambda: run_refresh_discovery(
                    layout=layout,
                    repo_root=repo_root,
                    run_id=new_run_id(),
                    hosts=hosts,
                    env_path=None,
                    viewer_modes=public_viewer_modes,
                    rps=rps,
                    concurrency=concurrency,
                    accept_language=accept_language,
                    accept_labelers=accept_labelers,
                    vantage_id_unauth=str(vantage_id_unauth).strip() or "unauth",
                    vantage_id_auth="auth-disabled",
                    resume=resume,
                    dry_run=dry_run,
                ),
            )

        if cfg.run_build_panel:
            await _run_step(
                "build-panel",
                lambda: run_build_panel(
                    layout=layout,
                    run_id=new_run_id(),
                    hosts=hosts,
                    env_path=None,
                    rps=rps,
                    concurrency=concurrency,
                    dry_run=dry_run,
                    cfg=PanelBuildConfig(
                        k1_popular=int(cfg.panel_k1_popular),
                        k2_onboarding=int(cfg.panel_k2_onboarding),
                        k3_suggested=int(cfg.panel_k3_suggested),
                        k4_longtail=int(cfg.panel_k4_longtail),
                    ),
                ),
            )

        if cfg.run_hydrate_feed_generators:
            await _run_step(
                "hydrate-feed-generators",
                lambda: run_hydrate_feed_generators(
                    layout=layout,
                    hosts=hosts,
                    run_id=new_run_id(),
                    rps=rps,
                    concurrency=concurrency,
                    dry_run=dry_run,
                    cfg=HydrateFeedGeneratorsConfig(
                        max_feeds=int(cfg.max_feed_generators),
                        include_hydrated=False,
                    ),
                    accept_language=accept_language,
                    accept_labelers=accept_labelers,
                    vantage_id=str(vantage_id_unauth).strip() or "unauth",
                ),
            )

        if cfg.run_snapshot_panel:
            await _run_step(
                "snapshot-panel",
                lambda: run_snapshot_panel(
                    layout=layout,
                    hosts=hosts,
                    env_path=None,
                    viewer_modes=public_viewer_modes,
                    posts_per_feed=posts_per_feed,
                    rps=rps,
                    concurrency=concurrency,
                    time_budget_minutes=time_budget_minutes,
                    feed_time_budget_s=feed_time_budget_s,
                    resume=resume,
                    dry_run=dry_run,
                    snapshot_hour_utc=cfg.snapshot_hour_utc,
                    accept_language=accept_language,
                    accept_labelers=accept_labelers,
                    include_author_labels=include_author_labels,
                    vantage_id_unauth=str(vantage_id_unauth).strip() or "unauth",
                    vantage_id_auth="auth-disabled",
                ),
            )

        if cfg.run_micro_studies:
            for study_id in _resolve_study_ids(layout, cfg):
                micro_window_start = _resolve_micro_window_start(
                    layout=layout,
                    study_id=study_id,
                    explicit=cfg.micro_window_start_utc,
                )
                await _run_step(
                    f"micro-snapshot-study:{study_id}",
                    lambda study_id=study_id, micro_window_start=micro_window_start: run_micro_snapshot_study(
                        layout=layout,
                        hosts=hosts,
                        env_path=None,
                        study_id=study_id,
                        scheduled_window_start_utc=micro_window_start,
                        rps=rps,
                        concurrency=concurrency,
                        feed_time_budget_s=feed_time_budget_s,
                        resume=resume,
                        dry_run=dry_run,
                        public_only=True,
                    ),
                    extra={"study_id": study_id, "window_start_utc": format_utc(micro_window_start)},
                )
                studies_run.append(study_id)

        if cfg.run_wide_sweep:
            await _run_step(
                "wide-sweep",
                lambda: run_wide_sweep(
                    layout=layout,
                    hosts=hosts,
                    n_feeds=int(cfg.n_feeds_wide),
                    posts_per_feed=posts_per_feed,
                    rps=rps,
                    concurrency=concurrency,
                    time_budget_minutes=time_budget_minutes,
                    feed_time_budget_s=feed_time_budget_s,
                    resume=resume,
                    dry_run=dry_run,
                    accept_language=accept_language,
                    accept_labelers=accept_labelers,
                    include_author_labels=include_author_labels,
                    vantage_id=str(vantage_id_unauth).strip() or "unauth",
                ),
            )

        if cfg.run_hydrate_authors:
            await _run_step(
                "hydrate-authors",
                lambda: run_hydrate_authors(
                    layout=layout,
                    hosts=hosts,
                    run_id=new_run_id(),
                    rps=rps,
                    concurrency=concurrency,
                    dry_run=dry_run,
                    cfg=HydrateAuthorsConfig(
                        batch_size=25,
                        max_authors=int(cfg.max_authors),
                        seen_after_utc=cfg.seen_after_utc,
                        seen_before_utc=cfg.seen_before_utc,
                    ),
                    accept_language=accept_language,
                    accept_labelers=accept_labelers,
                    vantage_id=str(vantage_id_unauth).strip() or "unauth",
                ),
            )

        if cfg.run_backfill_interactions:
            await _run_step(
                "backfill-interactions",
                lambda: run_backfill_interactions(
                    layout=layout,
                    hosts=hosts,
                    run_id=new_run_id(),
                    rps=rps,
                    concurrency=concurrency,
                    dry_run=dry_run,
                    cfg=BackfillInteractionsConfig(
                        max_posts=int(cfg.max_posts_interactions),
                        batch_size=int(cfg.batch_size_interactions),
                        max_items_per_endpoint=int(cfg.max_items_per_endpoint_interactions),
                        seen_after_utc=cfg.seen_after_utc,
                        seen_before_utc=cfg.seen_before_utc,
                        include_hydrated=bool(cfg.include_hydrated_interactions),
                    ),
                    accept_language=accept_language,
                    accept_labelers=accept_labelers,
                    vantage_id=str(vantage_id_unauth).strip() or "unauth",
                ),
            )

        if cfg.run_backfill_rq1_factors:
            await _run_step(
                "backfill-rq1-factors",
                lambda: run_backfill_rq1_factors(
                    layout=layout,
                    hosts=hosts,
                    run_id=new_run_id(),
                    rps=rps,
                    concurrency=concurrency,
                    dry_run=dry_run,
                    cfg=BackfillRq1FactorsConfig(
                        max_posts=int(cfg.max_posts_rq1),
                        batch_size=int(cfg.batch_size_rq1),
                        max_items_per_endpoint=int(cfg.max_items_per_endpoint_rq1),
                        max_thread_depth=int(cfg.max_thread_depth),
                        max_thread_parent_height=int(cfg.max_thread_parent_height),
                        max_author_feed_items=int(cfg.max_author_feed_items),
                        max_followers_per_actor=int(cfg.max_followers_per_actor),
                        max_follows_per_actor=int(cfg.max_follows_per_actor),
                        max_follow_records_per_actor=int(cfg.max_follow_records_per_actor),
                        max_actor_feeds_per_actor=int(cfg.max_actor_feeds_per_actor),
                        max_lists_per_actor=int(cfg.max_lists_per_actor),
                        max_list_members_per_list=int(cfg.max_list_members_per_list),
                        max_starter_packs_per_actor=int(cfg.max_starter_packs_per_actor),
                        seen_after_utc=cfg.seen_after_utc,
                        seen_before_utc=cfg.seen_before_utc,
                        include_hydrated=bool(cfg.include_hydrated_rq1),
                        resolve_pds_endpoints=bool(cfg.resolve_pds_endpoints),
                        follow_record_scope=str(cfg.follow_record_scope),
                        shard_index=int(cfg.shard_index),
                        shard_count=int(cfg.shard_count),
                    ),
                    accept_language=accept_language,
                    accept_labelers=accept_labelers,
                    vantage_id=str(vantage_id_unauth).strip() or "unauth",
                ),
            )

        success = True
    finally:
        finished_at_utc = format_utc(now_utc())
        manifest["finished_at_utc"] = finished_at_utc
        manifest["success"] = bool(success)
        manifest["step_results"] = step_results
        manifest["studies_run"] = studies_run
        atomic_write_json(manifest_path, manifest)

    summary = PublicOmnibusSummary(
        run_id=str(run_id),
        started_at_utc=started_at_utc,
        finished_at_utc=finished_at_utc,
        success=bool(success),
        step_results=step_results,
        studies_run=studies_run,
    )
    logger.info("public-omnibus complete summary=%s", summary.to_dict())
    return summary


__all__ = ["PublicOmnibusConfig", "PublicOmnibusSummary", "run_public_omnibus"]
