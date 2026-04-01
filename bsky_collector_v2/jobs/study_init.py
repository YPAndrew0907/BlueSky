from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from bsky_collector_v2.layout import Layout
from bsky_collector_v2.study import (
    FrozenStudyManifest,
    StudySampleFamily,
    build_study_id,
    ceil_to_window_utc,
    deterministic_seed,
    file_sha256,
    load_benchmark_summary,
    panel_membership_hash,
    panel_version_id_from_rows,
    read_panel_rows,
    write_panel_rows,
    write_study_manifest,
)
from bsky_collector_v2.time_utils import format_utc, now_utc

logger = logging.getLogger("bsky_collector_v2.job.study_init")


@dataclass(frozen=True)
class StudyInitConfig:
    sample_family: StudySampleFamily
    benchmark_path: Path
    source_panel_path: Path
    study_id: str | None
    viewer_modes: tuple[str, ...]
    posts_per_feed: int
    accept_language: str | None
    accept_labelers: str | None
    include_author_labels: bool
    vantage_id_unauth: str
    vantage_id_auth: str
    auto_core_size: bool
    requested_core_size: int | None
    auto_shard_count: bool
    requested_shard_count: int | None
    window_origin_utc: datetime | None
    study_group_id: str | None = None
    selection_strategy: str = "keep_input_order"
    feed_time_budget_s: float = 20.0
    max_attempts: int = 3


def _resolve_anchor(raw: datetime | None) -> datetime:
    anchor = raw if raw is not None else ceil_to_window_utc(now_utc(), window_minutes=5)
    return anchor.astimezone(UTC)


def run_study_init(*, layout: Layout, cfg: StudyInitConfig, dry_run: bool = False) -> dict[str, object]:
    benchmark = load_benchmark_summary(cfg.benchmark_path)
    safe_max_panel_size = int(benchmark.safe_max_panel_size)
    if safe_max_panel_size < 1:
        raise ValueError(
            "benchmark does not support any micro5 study on this machine/config; safe_max_panel_size < 1"
        )

    source_rows = read_panel_rows(cfg.source_panel_path)
    if not source_rows:
        raise ValueError(f"source panel is empty: {cfg.source_panel_path}")

    source_panel_hash = file_sha256(cfg.source_panel_path)
    if benchmark.panel_hash and benchmark.panel_hash != source_panel_hash:
        raise ValueError(
            "benchmark panel hash does not match the requested source panel; rerun study-benchmark for this panel"
        )

    if tuple(cfg.viewer_modes) and tuple(cfg.viewer_modes) != tuple(benchmark.viewer_modes):
        raise ValueError(
            f"requested viewer_modes {cfg.viewer_modes!r} do not match benchmark viewer_modes {benchmark.viewer_modes!r}"
        )
    if int(cfg.posts_per_feed) != int(benchmark.posts_per_feed):
        raise ValueError(
            f"requested posts_per_feed {cfg.posts_per_feed} does not match benchmark posts_per_feed {benchmark.posts_per_feed}"
        )

    created_at_utc = now_utc()
    study_id = str(cfg.study_id or build_study_id(sample_family=cfg.sample_family, created_at_utc=created_at_utc))
    anchor_start_utc = _resolve_anchor(cfg.window_origin_utc)
    study_dir = layout.study_dir(study_id)
    if study_dir.exists() and any(study_dir.iterdir()):
        raise ValueError(
            f"study_id {study_id!r} already exists at {study_dir}; frozen studies are immutable and must not be overwritten"
        )

    selected_rows = list(source_rows)
    selection_strategy = str(cfg.selection_strategy).strip() or "keep_input_order"
    panel_role = "core" if cfg.sample_family == "micro5_core_full" else "extended"
    shard_count: int | None = None
    shard_seed: int | None = None

    if cfg.sample_family == "micro5_core_full":
        if cfg.requested_core_size is not None and cfg.requested_core_size > 0:
            selected_rows = selected_rows[: int(cfg.requested_core_size)]
            selection_strategy = f"{selection_strategy}_truncated"
        if cfg.auto_core_size and len(selected_rows) > safe_max_panel_size:
            selected_rows = selected_rows[:safe_max_panel_size]
            selection_strategy = f"{selection_strategy}_auto_safe_max"
        if len(selected_rows) > safe_max_panel_size:
            raise ValueError(
                "requested core study exceeds 5-minute benchmark capacity; "
                f"requested={len(selected_rows)} safe_max_panel_size={safe_max_panel_size}"
            )
    else:
        if len(selected_rows) <= safe_max_panel_size and not cfg.requested_shard_count and not cfg.auto_shard_count:
            raise ValueError(
                "requested panel already fits within the benchmarked 5-minute budget; "
                "use micro5_core_full instead of micro5_extended_sharded"
            )
        if cfg.requested_shard_count is not None and cfg.requested_shard_count > 0:
            shard_count = int(cfg.requested_shard_count)
        elif cfg.auto_shard_count:
            shard_count = int(max(1, benchmark.required_shard_count))
        else:
            raise ValueError(
                "extended sharded studies require either --shard-count or --auto-shard-count; "
                "the command will not silently choose a shard count"
            )
        estimated_units = benchmark.estimated_full_sweep_requests
        estimated_duration_s = float(estimated_units) / max(float(benchmark.throughput_rps), 0.001)
        per_shard_s = estimated_duration_s / float(max(1, shard_count))
        if per_shard_s > benchmark.safe_window_budget_s:
            raise ValueError(
                "requested sharded study exceeds 5-minute benchmark capacity; "
                f"per_shard_duration_s={per_shard_s:.3f} safe_window_budget_s={benchmark.safe_window_budget_s:.3f}"
            )
        shard_seed = deterministic_seed(study_id, source_panel_hash, "micro5_extended_sharded")

    frozen_panel_hash: str
    frozen_panel_membership_hash: str
    frozen_panel_path = layout.study_panel_csv(study_id)
    benchmark_dest = layout.study_benchmark_json(study_id)
    manifest_path = layout.study_manifest_json(study_id)

    if not dry_run:
        write_panel_rows(frozen_panel_path, selected_rows)
        frozen_panel_hash = file_sha256(frozen_panel_path)
        frozen_panel_membership_hash = panel_membership_hash(selected_rows)
        try:
            os.chmod(frozen_panel_path, 0o444)
        except OSError:
            logger.debug("unable to chmod frozen panel read-only path=%s", str(frozen_panel_path))
        benchmark_dest.parent.mkdir(parents=True, exist_ok=True)
        benchmark_dest.write_text(
            json.dumps(benchmark.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    else:
        frozen_panel_hash = source_panel_hash
        frozen_panel_membership_hash = panel_membership_hash(selected_rows)

    sample_design = {
        "family": cfg.sample_family,
        "selection_strategy": selection_strategy,
        "source_panel_row_count": len(source_rows),
        "selected_panel_row_count": len(selected_rows),
        "safe_max_panel_size": safe_max_panel_size,
        "window_minutes": int(benchmark.window_minutes),
        "safety_margin": float(benchmark.safety_margin),
    }
    if shard_count is not None:
        sample_design["shard_count"] = shard_count
        sample_design["sharding_strategy"] = "stable_hash_modulo_rotation"

    manifest = FrozenStudyManifest(
        study_id=study_id,
        study_group_id=(str(cfg.study_group_id).strip() or None) if cfg.study_group_id else None,
        created_at_utc=format_utc(created_at_utc),
        window_anchor_start_utc=format_utc(anchor_start_utc),
        intended_window_minutes=int(benchmark.window_minutes),
        sample_family=cfg.sample_family,
        panel_role=panel_role,
        sample_design=sample_design,
        panel_path=str(frozen_panel_path),
        panel_hash=str(frozen_panel_hash),
        panel_version_id=panel_version_id_from_rows(selected_rows),
        panel_row_count=len(selected_rows),
        panel_membership_hash=str(frozen_panel_membership_hash),
        source_panel_path=str(cfg.source_panel_path),
        source_panel_hash=source_panel_hash,
        source_panel_version_id=panel_version_id_from_rows(source_rows),
        selection_strategy=selection_strategy,
        viewer_modes=tuple(str(mode) for mode in benchmark.viewer_modes or ()),
        accept_language=(str(cfg.accept_language).strip() or None) if cfg.accept_language else None,
        accept_labelers=(str(cfg.accept_labelers).strip() or None) if cfg.accept_labelers else None,
        include_author_labels=bool(cfg.include_author_labels),
        auth_vantage_ids={
            "unauth": str(cfg.vantage_id_unauth).strip() or "unauth",
            "auth": str(cfg.vantage_id_auth).strip() or "auth",
        },
        posts_per_feed=int(benchmark.posts_per_feed),
        rps=float(benchmark.rps),
        concurrency=int(benchmark.concurrency),
        feed_time_budget_s=float(cfg.feed_time_budget_s),
        max_attempts=int(cfg.max_attempts),
        benchmark_id=str(benchmark.benchmark_id),
        benchmark_path=str(benchmark_dest),
        benchmark_result=benchmark.to_dict(),
        shard_count=shard_count,
        shard_seed=shard_seed,
    )

    if not dry_run:
        write_study_manifest(manifest_path, manifest)

    return manifest.to_dict()
