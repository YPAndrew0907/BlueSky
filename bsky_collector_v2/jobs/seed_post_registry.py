from __future__ import annotations

import csv
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

from bsky_collector_v2.fs_utils import atomic_write_json, ensure_dir
from bsky_collector_v2.instrumentation import enrich_manifest
from bsky_collector_v2.layout import Layout
from bsky_collector_v2.state import ControlState
from bsky_collector_v2.time_utils import format_utc, now_utc
from bsky_collector_v2.types import PostUri, RunId

logger = logging.getLogger("bsky_collector_v2.job.seed_post_registry")


@dataclass(frozen=True)
class SeedPostRegistryConfig:
    include_hourly: bool = True
    include_wide: bool = True
    include_micro5: bool = True
    include_posts_first_seen: bool = False
    max_files: int = 0
    max_rows: int = 0
    enqueue_interactions: bool = False
    enqueue_rq1_factors: bool = False
    mark_first_written: bool = True


@dataclass(frozen=True)
class SeedPostRegistrySummary:
    files_scanned: int
    rows_scanned: int
    post_rows_processed: int
    actor_rows_processed: int
    post_registry_rows: int
    author_registry_rows: int
    files_by_family: dict[str, int] = field(default_factory=dict)
    rows_by_family: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "files_scanned": int(self.files_scanned),
            "rows_scanned": int(self.rows_scanned),
            "post_rows_processed": int(self.post_rows_processed),
            "actor_rows_processed": int(self.actor_rows_processed),
            "post_registry_rows": int(self.post_registry_rows),
            "author_registry_rows": int(self.author_registry_rows),
            "files_by_family": {str(k): int(v) for k, v in self.files_by_family.items()},
            "rows_by_family": {str(k): int(v) for k, v in self.rows_by_family.items()},
        }


def _is_iso_date(value: str) -> bool:
    return len(value) == 10 and value[4] == "-" and value[7] == "-" and value.replace("-", "").isdigit()


def _is_two_digit(value: str) -> bool:
    return len(value) == 2 and value.isdigit()


def _fallback_seen_at_from_path(path: Path, *, family: str) -> str:
    parts = list(path.parts)
    for idx, part in enumerate(parts):
        if not _is_iso_date(part):
            continue
        date_str = part
        next_parts = parts[idx + 1 : idx + 3]
        hour = next_parts[0] if len(next_parts) >= 1 and _is_two_digit(next_parts[0]) else "00"
        minute = next_parts[1] if len(next_parts) >= 2 and _is_two_digit(next_parts[1]) else None
        if family == "micro5" and minute is not None:
            return f"{date_str}T{hour}:{minute}:00Z"
        if family == "hourly":
            return f"{date_str}T{hour}:00:00Z"
        return f"{date_str}T00:00:00Z"
    return format_utc(now_utc())


def _row_seen_at(row: dict[str, Any], *, family: str, path: Path) -> str:
    for key in (
        "captured_at_utc",
        "first_seen_utc",
        "scheduled_window_start_utc",
        "snapshot_hour_utc",
        "post_indexed_at",
        "record_created_at",
    ):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return _fallback_seen_at_from_path(path, family=family)


def _actor_dids_from_row(row: dict[str, Any], *, post_uri: str | None) -> list[str]:
    out: list[str] = []
    for key in ("author_did", "reason_actor_did", "reply_grandparent_author_did"):
        value = str(row.get(key) or "").strip()
        if value:
            out.append(value)
    if post_uri and post_uri.startswith("at://"):
        did = post_uri.removeprefix("at://").split("/")[0].strip()
        if did:
            out.append(did)
    return out


def _iter_source_paths(layout: Layout, cfg: SeedPostRegistryConfig) -> Iterator[tuple[str, Path]]:
    roots: list[tuple[str, Path, bool]] = [
        ("hourly", layout.hourly_root, bool(cfg.include_hourly)),
        ("wide", layout.wide_root, bool(cfg.include_wide)),
        ("micro5", layout.micro5_root, bool(cfg.include_micro5)),
    ]
    patterns = ["feed_items_part_*.csv"]
    if cfg.include_posts_first_seen:
        patterns.append("posts_first_seen_part_*.csv")
    for family, root, enabled in roots:
        if not enabled or not root.exists():
            continue
        for pattern in patterns:
            for path in sorted(root.rglob(pattern)):
                yield family, path


def _flush_batch(
    *,
    control: ControlState,
    posts: list[PostUri],
    actor_dids: list[str],
    seen_at_utc: str,
    cfg: SeedPostRegistryConfig,
) -> tuple[int, int]:
    dedup_posts = [PostUri(uri) for uri in dict.fromkeys(str(p) for p in posts if str(p))]
    dedup_actors = list(dict.fromkeys(str(did) for did in actor_dids if str(did).strip()))
    if dedup_posts:
        control.upsert_post_registry_many(post_uris=dedup_posts, seen_at_utc=seen_at_utc)
        if cfg.mark_first_written:
            control.mark_first_written(post_uris=dedup_posts)
        if cfg.enqueue_interactions:
            control.ensure_post_interaction_tasks(post_uris=dedup_posts, enqueued_at_utc=seen_at_utc)
        if cfg.enqueue_rq1_factors:
            control.ensure_post_rq1_factor_tasks(post_uris=dedup_posts, enqueued_at_utc=seen_at_utc)
    if dedup_actors:
        control.upsert_author_registry_many(author_dids=dedup_actors, seen_at_utc=seen_at_utc)
    return len(dedup_posts), len(dedup_actors)


def run_seed_post_registry(
    *,
    layout: Layout,
    run_id: RunId,
    dry_run: bool,
    cfg: SeedPostRegistryConfig | None = None,
) -> SeedPostRegistrySummary:
    cfg = cfg or SeedPostRegistryConfig()
    started_at_utc = format_utc(now_utc())
    control_root = layout.control_root
    ensure_dir(control_root)
    manifest_path = control_root / "seed_post_registry_last_run.json"
    manifest = {
        "run_id": str(run_id),
        "job_name": "seed-post-registry",
        "started_at_utc": started_at_utc,
        "params": {
            "include_hourly": bool(cfg.include_hourly),
            "include_wide": bool(cfg.include_wide),
            "include_micro5": bool(cfg.include_micro5),
            "include_posts_first_seen": bool(cfg.include_posts_first_seen),
            "max_files": int(cfg.max_files),
            "max_rows": int(cfg.max_rows),
            "enqueue_interactions": bool(cfg.enqueue_interactions),
            "enqueue_rq1_factors": bool(cfg.enqueue_rq1_factors),
            "mark_first_written": bool(cfg.mark_first_written),
        },
    }
    enrich_manifest(manifest, job_name="seed-post-registry", out_base=layout.out_base, params=manifest["params"])
    atomic_write_json(manifest_path, manifest)

    files_scanned = 0
    rows_scanned = 0
    post_rows_processed = 0
    actor_rows_processed = 0
    files_by_family: dict[str, int] = defaultdict(int)
    rows_by_family: dict[str, int] = defaultdict(int)
    success = False

    paths = list(_iter_source_paths(layout, cfg))
    if cfg.max_files and int(cfg.max_files) > 0:
        paths = paths[: int(cfg.max_files)]

    logger.info(
        "seed-post-registry start files=%s include_hourly=%s include_wide=%s include_micro5=%s include_posts_first_seen=%s dry_run=%s",
        len(paths),
        cfg.include_hourly,
        cfg.include_wide,
        cfg.include_micro5,
        cfg.include_posts_first_seen,
        dry_run,
    )

    if dry_run:
        summary = SeedPostRegistrySummary(
            files_scanned=len(paths),
            rows_scanned=0,
            post_rows_processed=0,
            actor_rows_processed=0,
            post_registry_rows=0,
            author_registry_rows=0,
            files_by_family={family: sum(1 for fam, _path in paths if fam == family) for family in {fam for fam, _path in paths}},
            rows_by_family={},
        )
        manifest["finished_at_utc"] = format_utc(now_utc())
        manifest["success"] = True
        manifest["summary"] = summary.to_dict()
        atomic_write_json(manifest_path, manifest)
        return summary

    with ControlState.open(layout.control_db_path) as control:
        control.start_run(run_id=run_id, job_name="seed-post-registry", started_at_utc=started_at_utc, params=manifest["params"])
        try:
            for family, path in paths:
                if cfg.max_rows and rows_scanned >= int(cfg.max_rows):
                    break
                try:
                    with open(path, "r", encoding="utf-8", newline="") as f:
                        reader = csv.DictReader(f)
                        first_row = next(reader, None)
                        if first_row is None:
                            continue
                        seen_at_utc = _row_seen_at(first_row, family=family, path=path)
                        post_batch: list[PostUri] = []
                        actor_batch: list[str] = []

                        def process_row(row: dict[str, Any]) -> None:
                            nonlocal rows_scanned, post_rows_processed, actor_rows_processed
                            if cfg.max_rows and rows_scanned >= int(cfg.max_rows):
                                return
                            rows_scanned += 1
                            rows_by_family[family] += 1
                            post_uri = str(row.get("post_uri") or "").strip()
                            if post_uri:
                                post_batch.append(PostUri(post_uri))
                                post_rows_processed += 1
                            actor_dids = _actor_dids_from_row(row, post_uri=post_uri or None)
                            actor_batch.extend(actor_dids)
                            actor_rows_processed += len(actor_dids)
                            if len(post_batch) >= 5000 or len(actor_batch) >= 10000:
                                _flush_batch(control=control, posts=post_batch, actor_dids=actor_batch, seen_at_utc=seen_at_utc, cfg=cfg)
                                post_batch.clear()
                                actor_batch.clear()

                        process_row(first_row)
                        for row in reader:
                            process_row(row)
                            if cfg.max_rows and rows_scanned >= int(cfg.max_rows):
                                break
                        if post_batch or actor_batch:
                            _flush_batch(control=control, posts=post_batch, actor_dids=actor_batch, seen_at_utc=seen_at_utc, cfg=cfg)
                            post_batch.clear()
                            actor_batch.clear()
                    control.commit()
                    files_scanned += 1
                    files_by_family[family] += 1
                except OSError as err:
                    if isinstance(err, (ConnectionError, TimeoutError)):
                        logger.error("seed-post-registry control-state update failed path=%s err=%r", str(path), err)
                        raise
                    logger.warning("seed-post-registry source file read failed path=%s err=%r", str(path), err)
                    continue
            success = True
        finally:
            control.finish_run(run_id=run_id, finished_at_utc=format_utc(now_utc()), success=success)
            control.commit()
            post_registry_rows = int(control.count_post_registry_rows())
            author_registry_rows = int(control.count_author_registry_rows())

    summary = SeedPostRegistrySummary(
        files_scanned=files_scanned,
        rows_scanned=rows_scanned,
        post_rows_processed=post_rows_processed,
        actor_rows_processed=actor_rows_processed,
        post_registry_rows=post_registry_rows,
        author_registry_rows=author_registry_rows,
        files_by_family=dict(files_by_family),
        rows_by_family=dict(rows_by_family),
    )
    manifest["finished_at_utc"] = format_utc(now_utc())
    manifest["success"] = bool(success)
    manifest["summary"] = summary.to_dict()
    atomic_write_json(manifest_path, manifest)
    logger.info("seed-post-registry complete summary=%s", summary.to_dict())
    return summary


__all__ = ["SeedPostRegistryConfig", "SeedPostRegistrySummary", "run_seed_post_registry"]
