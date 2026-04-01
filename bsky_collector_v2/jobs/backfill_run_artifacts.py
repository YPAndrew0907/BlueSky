from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bsky_collector_v2.fs_utils import atomic_write_json
from bsky_collector_v2.instrumentation import enrich_manifest, read_panel_metadata
from bsky_collector_v2.layout import Layout
from bsky_collector_v2.quality import (
    assess_authors_day,
    assess_discovery_day,
    assess_feed_generator_index_day,
    assess_snapshot_hour,
    assess_wide_day,
)
from bsky_collector_v2.request_provenance import rewrite_request_provenance_csv
from bsky_collector_v2.time_utils import SnapshotHour, format_utc, now_utc


@dataclass
class BackfillSummary:
    out_base: str
    snapshot_manifests_updated: int = 0
    wide_manifests_updated: int = 0
    metadata_manifests_written: int = 0
    index_manifests_written: int = 0
    authors_manifests_written: int = 0
    snapshot_request_provenance_written: int = 0
    wide_request_provenance_written: int = 0
    index_request_provenance_written: int = 0
    authors_request_provenance_written: int = 0
    snapshot_quality_reports_written: int = 0
    wide_quality_reports_written: int = 0
    metadata_quality_reports_written: int = 0
    index_quality_reports_written: int = 0
    authors_quality_reports_written: int = 0
    nested_labelerexp_processed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at_utc": format_utc(now_utc()),
            "out_base": self.out_base,
            "snapshot_manifests_updated": self.snapshot_manifests_updated,
            "wide_manifests_updated": self.wide_manifests_updated,
            "metadata_manifests_written": self.metadata_manifests_written,
            "index_manifests_written": self.index_manifests_written,
            "authors_manifests_written": self.authors_manifests_written,
            "snapshot_request_provenance_written": self.snapshot_request_provenance_written,
            "wide_request_provenance_written": self.wide_request_provenance_written,
            "index_request_provenance_written": self.index_request_provenance_written,
            "authors_request_provenance_written": self.authors_request_provenance_written,
            "snapshot_quality_reports_written": self.snapshot_quality_reports_written,
            "wide_quality_reports_written": self.wide_quality_reports_written,
            "metadata_quality_reports_written": self.metadata_quality_reports_written,
            "index_quality_reports_written": self.index_quality_reports_written,
            "authors_quality_reports_written": self.authors_quality_reports_written,
            "nested_labelerexp_processed": self.nested_labelerexp_processed,
        }

    def absorb(self, other: "BackfillSummary") -> None:
        self.snapshot_manifests_updated += other.snapshot_manifests_updated
        self.wide_manifests_updated += other.wide_manifests_updated
        self.metadata_manifests_written += other.metadata_manifests_written
        self.index_manifests_written += other.index_manifests_written
        self.authors_manifests_written += other.authors_manifests_written
        self.snapshot_request_provenance_written += other.snapshot_request_provenance_written
        self.wide_request_provenance_written += other.wide_request_provenance_written
        self.index_request_provenance_written += other.index_request_provenance_written
        self.authors_request_provenance_written += other.authors_request_provenance_written
        self.snapshot_quality_reports_written += other.snapshot_quality_reports_written
        self.wide_quality_reports_written += other.wide_quality_reports_written
        self.metadata_quality_reports_written += other.metadata_quality_reports_written
        self.index_quality_reports_written += other.index_quality_reports_written
        self.authors_quality_reports_written += other.authors_quality_reports_written
        self.nested_labelerexp_processed = self.nested_labelerexp_processed or other.nested_labelerexp_processed


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(raw, dict):
        return raw
    return None


def _write_json_if_changed(path: Path, payload: dict[str, Any], *, dry_run: bool) -> bool:
    existing = _load_json(path)
    if existing == payload:
        return False
    if not dry_run:
        atomic_write_json(path, payload)
    return True


def _parse_snapshot_hour(*, date_str: str, hour_str: str) -> SnapshotHour:
    hour_dt = datetime.fromisoformat(f"{date_str}T{hour_str}:00:00+00:00").astimezone(UTC)
    return SnapshotHour(hour_utc=hour_dt)


def _iter_hour_dirs(layout: Layout) -> list[SnapshotHour]:
    out: list[SnapshotHour] = []
    root = layout.hourly_root
    if not root.exists():
        return out
    for date_dir in sorted(root.iterdir()):
        if not date_dir.is_dir():
            continue
        date_str = date_dir.name
        if len(date_str) != 10:
            continue
        for hour_dir in sorted(date_dir.iterdir()):
            if not hour_dir.is_dir():
                continue
            hour_str = hour_dir.name
            if len(hour_str) != 2:
                continue
            out.append(_parse_snapshot_hour(date_str=date_str, hour_str=hour_str))
    return out


def _iter_date_dirs(root: Path) -> list[str]:
    if not root.exists():
        return []
    return [child.name for child in sorted(root.iterdir()) if child.is_dir() and len(child.name) == 10]


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _csv_header_has_fields(path: Path, required: set[str]) -> bool:
    if not path.exists():
        return False
    try:
        with open(path, "r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            fieldnames = set(reader.fieldnames or [])
    except OSError:
        return False
    return required.issubset(fieldnames)


def _load_http_stats_by_key(path: Path) -> dict[tuple[str, str, str], dict[str, str]]:
    rows = _read_csv_rows(path)
    out: dict[tuple[str, str, str], dict[str, str]] = {}
    for row in rows:
        key = (
            str(row.get("timestamp_utc") or "").strip(),
            str(row.get("endpoint") or "").strip(),
            str(row.get("feed_uri") or "").strip(),
        )
        if not any(key):
            continue
        current = out.get(key)
        if current is None:
            out[key] = row
            continue
        try:
            current_attempt = int(current.get("attempt") or 0)
        except ValueError:
            current_attempt = 0
        try:
            row_attempt = int(row.get("attempt") or 0)
        except ValueError:
            row_attempt = 0
        if row_attempt >= current_attempt:
            out[key] = row
    return out


def _legacy_request_rows_from_feed_items(
    *,
    feed_item_paths: list[Path],
    http_stats_path: Path,
    job_name: str,
    sample_family: str,
    collection_params_hash: str,
    default_host_kind: str,
    depth_requested: int | None,
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    for path in feed_item_paths:
        for row in _read_csv_rows(path):
            snapshot_hour_utc = str(row.get("snapshot_hour_utc") or "").strip()
            captured_at_utc = str(row.get("captured_at_utc") or "").strip()
            viewer_mode = str(row.get("viewer_mode") or "").strip()
            vantage_id = str(row.get("vantage_id") or "").strip()
            feed_uri = str(row.get("feed_uri") or "").strip()
            if not captured_at_utc or not feed_uri:
                continue
            key = (snapshot_hour_utc, captured_at_utc, viewer_mode, vantage_id, feed_uri)
            group = groups.setdefault(
                key,
                {
                    "run_id": str(row.get("run_id") or "").strip(),
                    "snapshot_hour_utc": snapshot_hour_utc or None,
                    "date_utc": snapshot_hour_utc[:10] if snapshot_hour_utc else None,
                    "captured_at_utc": captured_at_utc,
                    "viewer_mode": viewer_mode or None,
                    "vantage_id": vantage_id or None,
                    "feed_uri": feed_uri,
                    "depth_returned": 0,
                },
            )
            try:
                rank = int(row.get("rank") or 0)
            except ValueError:
                rank = 0
            group["depth_returned"] = max(int(group["depth_returned"]), rank)

    http_stats = _load_http_stats_by_key(http_stats_path)
    out_rows: list[dict[str, Any]] = []
    sorted_groups = sorted(
        groups.items(),
        key=lambda item: (item[1]["captured_at_utc"], item[1]["viewer_mode"] or "", item[1]["feed_uri"]),
    )
    for order, (_key, group) in enumerate(sorted_groups, start=1):
        stats = http_stats.get((group["captured_at_utc"], "app.bsky.feed.getFeed", group["feed_uri"]))
        out_rows.append(
            {
                "run_id": group["run_id"],
                "job_name": job_name,
                "sample_family": sample_family,
                "collection_params_hash": collection_params_hash,
                "collector_version": None,
                "schema_version": None,
                "request_provenance_version": None,
                "provenance_source": "legacy_feed_items_plus_http_stats",
                "provenance_level": "partial",
                "partial_reason": "legacy backfill reconstructed from feed_items plus http_stats; finished_at, host, response_hash, and cursors are unavailable",
                "request_started_at_utc": group["captured_at_utc"],
                "request_finished_at_utc": None,
                "request_order_in_run": order,
                "request_order_in_sweep": order,
                "snapshot_hour_utc": group["snapshot_hour_utc"],
                "date_utc": group["date_utc"],
                "viewer_mode": group["viewer_mode"],
                "vantage_id": group["vantage_id"],
                "host_kind": default_host_kind if (group["viewer_mode"] != "auth") else "authenticated_pds_proxy",
                "host": None,
                "endpoint": "app.bsky.feed.getFeed",
                "feed_uri": group["feed_uri"],
                "page_no": 0,
                "cursor_in": None,
                "cursor_out": None,
                "depth_requested": depth_requested,
                "depth_returned": group["depth_returned"] or None,
                "http_status": stats.get("status_code") if stats else None,
                "retry_count": stats.get("attempt") if stats else None,
                "response_hash": None,
                "error_class": stats.get("error_type") if stats else None,
            }
        )
    return out_rows


def _backfill_snapshot_hour(layout: Layout, *, hour: SnapshotHour, dry_run: bool, summary: BackfillSummary) -> None:
    manifest_path = layout.hourly_manifest_json(hour)
    parts_dir = layout.hourly_parts_dir(hour)
    if not manifest_path.exists() and not parts_dir.exists():
        return

    manifest = _load_json(manifest_path) or {}
    params = manifest.get("params")
    if not isinstance(params, dict):
        params = {}
    panel_version_path = layout.panel_version_csv(hour.date_str)
    panel_metadata = read_panel_metadata(panel_version_path if panel_version_path.exists() else layout.panel_active_csv)
    manifest.setdefault("run_id", f"legacy_snapshot_{hour.date_str}_{hour.hour_str}")
    manifest.setdefault("job_name", "snapshot-panel")
    manifest.setdefault("snapshot_hour_utc", hour.hour_iso_z)
    manifest.setdefault("started_at_utc", hour.hour_iso_z)
    manifest["params"] = params
    enrich_manifest(
        manifest,
        job_name="snapshot-panel",
        out_base=layout.out_base,
        params=params,
        panel_version_id=panel_metadata.panel_version_id,
    )
    if _write_json_if_changed(manifest_path, manifest, dry_run=dry_run):
        summary.snapshot_manifests_updated += 1

    request_provenance_path = layout.hourly_request_provenance_csv(hour)
    if not _csv_header_has_fields(request_provenance_path, {"provenance_level", "partial_reason"}):
        rows = _legacy_request_rows_from_feed_items(
            feed_item_paths=sorted(parts_dir.glob("feed_items_part_*.csv")),
            http_stats_path=layout.hourly_http_stats_csv(hour),
            job_name="snapshot-panel",
            sample_family=str(manifest.get("sample_family") or "regular_hourly"),
            collection_params_hash=str(manifest.get("collection_params_hash") or ""),
            default_host_kind="public_appview",
            depth_requested=int(params.get("posts_per_feed")) if params.get("posts_per_feed") is not None else None,
        )
        if rows:
            if not dry_run:
                rewrite_request_provenance_csv(path=request_provenance_path, rows=rows)
            summary.snapshot_request_provenance_written += 1

    if not dry_run:
        atomic_write_json(layout.hourly_quality_report_json(hour), assess_snapshot_hour(layout, hour=hour))
    summary.snapshot_quality_reports_written += 1


def _backfill_wide_day(layout: Layout, *, date_yyyy_mm_dd: str, dry_run: bool, summary: BackfillSummary) -> None:
    manifest_path = layout.wide_manifest_json(date_yyyy_mm_dd)
    parts_dir = layout.wide_parts_dir(date_yyyy_mm_dd)
    if not manifest_path.exists() and not parts_dir.exists():
        return

    manifest = _load_json(manifest_path) or {}
    params = manifest.get("params")
    if not isinstance(params, dict):
        params = {}
    manifest.setdefault("run_id", f"legacy_wide_{date_yyyy_mm_dd}")
    manifest.setdefault("job_name", "wide-sweep")
    manifest.setdefault("date_utc", date_yyyy_mm_dd)
    manifest.setdefault("started_at_utc", f"{date_yyyy_mm_dd}T00:00:00Z")
    manifest["params"] = params
    enrich_manifest(
        manifest,
        job_name="wide-sweep",
        out_base=layout.out_base,
        params=params,
    )
    if _write_json_if_changed(manifest_path, manifest, dry_run=dry_run):
        summary.wide_manifests_updated += 1

    request_provenance_path = layout.wide_request_provenance_csv(date_yyyy_mm_dd)
    if not _csv_header_has_fields(request_provenance_path, {"provenance_level", "partial_reason"}):
        rows = _legacy_request_rows_from_feed_items(
            feed_item_paths=sorted(parts_dir.glob("feed_items_part_*.csv")),
            http_stats_path=layout.wide_http_stats_csv(date_yyyy_mm_dd),
            job_name="wide-sweep",
            sample_family=str(manifest.get("sample_family") or "wide"),
            collection_params_hash=str(manifest.get("collection_params_hash") or ""),
            default_host_kind="public_appview",
            depth_requested=int(params.get("posts_per_feed")) if params.get("posts_per_feed") is not None else None,
        )
        if rows:
            if not dry_run:
                rewrite_request_provenance_csv(path=request_provenance_path, rows=rows)
            summary.wide_request_provenance_written += 1

    if not dry_run:
        atomic_write_json(layout.wide_quality_report_json(date_yyyy_mm_dd), assess_wide_day(layout, date_yyyy_mm_dd=date_yyyy_mm_dd))
    summary.wide_quality_reports_written += 1


def _backfill_metadata_day(layout: Layout, *, date_yyyy_mm_dd: str, dry_run: bool, summary: BackfillSummary) -> None:
    manifest_path = layout.metadata_manifest_json(date_yyyy_mm_dd)
    status = _load_json(layout.metadata_discovery_status_json(date_yyyy_mm_dd)) or {}
    manifest = _load_json(manifest_path) or {}
    params = manifest.get("params")
    if not isinstance(params, dict):
        params = {}
    manifest.setdefault("run_id", str(status.get("run_id") or f"legacy_refresh_discovery_{date_yyyy_mm_dd}"))
    manifest.setdefault("job_name", "refresh-discovery")
    manifest.setdefault("date_utc", date_yyyy_mm_dd)
    manifest.setdefault("started_at_utc", str(status.get("started_at_utc") or f"{date_yyyy_mm_dd}T00:00:00Z"))
    manifest["params"] = params
    if "viewer_mode" in status:
        manifest["viewer_mode"] = status.get("viewer_mode")
    if "vantage_id" in status:
        manifest["vantage_id"] = status.get("vantage_id")
    if "finished_at_utc" in status:
        manifest["finished_at_utc"] = status.get("finished_at_utc")
    if "success" in status:
        manifest["success"] = status.get("success")
    enrich_manifest(
        manifest,
        job_name="refresh-discovery",
        out_base=layout.out_base,
        params=params,
    )
    if _write_json_if_changed(manifest_path, manifest, dry_run=dry_run):
        summary.metadata_manifests_written += 1

    if not dry_run:
        atomic_write_json(layout.metadata_quality_report_json(date_yyyy_mm_dd), assess_discovery_day(layout, date_yyyy_mm_dd=date_yyyy_mm_dd))
    summary.metadata_quality_reports_written += 1


def _legacy_request_rows_from_author_profiles(
    *,
    source_paths: list[Path],
    sample_family: str,
    collection_params_hash: str,
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for path in source_paths:
        for row in _read_csv_rows(path):
            run_id = str(row.get("run_id") or "").strip()
            captured_at_utc = str(row.get("captured_at_utc") or "").strip()
            vantage_id = str(row.get("vantage_id") or "").strip()
            if not run_id or not captured_at_utc:
                continue
            key = (run_id, captured_at_utc, vantage_id)
            group = groups.setdefault(
                key,
                {
                    "run_id": run_id,
                    "captured_at_utc": captured_at_utc,
                    "date_utc": captured_at_utc[:10],
                    "vantage_id": vantage_id or None,
                    "depth_returned": 0,
                },
            )
            group["depth_returned"] = int(group["depth_returned"]) + 1

    out_rows: list[dict[str, Any]] = []
    for order, (_key, group) in enumerate(sorted(groups.items(), key=lambda item: item[1]["captured_at_utc"]), start=1):
        out_rows.append(
            {
                "run_id": group["run_id"],
                "job_name": "hydrate-authors",
                "sample_family": sample_family,
                "collection_params_hash": collection_params_hash,
                "collector_version": None,
                "schema_version": None,
                "request_provenance_version": None,
                "provenance_source": "legacy_author_profiles",
                "provenance_level": "partial",
                "partial_reason": "legacy backfill reconstructed from author_profiles; host, finished_at, retries, and hashes are unavailable",
                "request_started_at_utc": group["captured_at_utc"],
                "request_finished_at_utc": None,
                "request_order_in_run": order,
                "request_order_in_sweep": order,
                "snapshot_hour_utc": None,
                "date_utc": group["date_utc"],
                "viewer_mode": "unauth",
                "vantage_id": group["vantage_id"],
                "host_kind": "public_appview",
                "host": None,
                "endpoint": "app.bsky.actor.getProfiles",
                "feed_uri": None,
                "page_no": 0,
                "cursor_in": None,
                "cursor_out": None,
                "depth_requested": None,
                "depth_returned": group["depth_returned"],
                "http_status": None,
                "retry_count": None,
                "response_hash": None,
                "error_class": None,
            }
        )
    return out_rows


def _legacy_request_rows_from_index_http_stats(
    *,
    http_stats_path: Path,
    sample_family: str,
    collection_params_hash: str,
    relay_host: str | None,
    records_host: str | None,
    run_id: str,
) -> list[dict[str, Any]]:
    rows = _read_csv_rows(http_stats_path)
    out_rows: list[dict[str, Any]] = []
    sorted_rows = sorted(rows, key=lambda row: (str(row.get("timestamp_utc") or ""), str(row.get("endpoint") or "")))
    for order, row in enumerate(sorted_rows, start=1):
        endpoint = str(row.get("endpoint") or "").strip()
        host_kind = "relay" if endpoint.startswith("com.atproto.sync.") else "pds_proxy"
        host = relay_host if host_kind == "relay" else records_host
        depth_requested = 500 if endpoint.startswith("com.atproto.sync.") else 100 if endpoint == "com.atproto.repo.listRecords" else None
        out_rows.append(
            {
                "run_id": run_id,
                "job_name": "index-feed-generators",
                "sample_family": sample_family,
                "collection_params_hash": collection_params_hash,
                "collector_version": None,
                "schema_version": None,
                "request_provenance_version": None,
                "provenance_source": "legacy_http_stats",
                "provenance_level": "partial",
                "partial_reason": "legacy backfill reconstructed from http_stats only; finished_at, hashes, cursors, and many hosts are unavailable",
                "request_started_at_utc": row.get("timestamp_utc"),
                "request_finished_at_utc": None,
                "request_order_in_run": order,
                "request_order_in_sweep": order,
                "snapshot_hour_utc": None,
                "date_utc": str(row.get("timestamp_utc") or "")[:10] or None,
                "viewer_mode": "auth" if endpoint == "com.atproto.repo.listRecords" else "unauth",
                "vantage_id": None,
                "host_kind": host_kind,
                "host": host,
                "endpoint": endpoint or None,
                "feed_uri": row.get("feed_uri"),
                "page_no": 0,
                "cursor_in": None,
                "cursor_out": None,
                "depth_requested": depth_requested,
                "depth_returned": None,
                "http_status": row.get("status_code"),
                "retry_count": row.get("attempt"),
                "response_hash": None,
                "error_class": row.get("error_type"),
            }
        )
    return out_rows


def _backfill_authors_day(layout: Layout, *, date_yyyy_mm_dd: str, dry_run: bool, summary: BackfillSummary) -> None:
    day_dir = layout.authors_day_dir(date_yyyy_mm_dd)
    source_paths = sorted(day_dir.glob("author_profiles_part_*.csv"))
    if not source_paths:
        return

    manifest_path = layout.authors_manifest_json(date_yyyy_mm_dd)
    manifest = _load_json(manifest_path) or {}
    sample_run_rows = _read_csv_rows(source_paths[0])
    author_rows_total = sum(len(_read_csv_rows(path)) for path in source_paths)
    run_id = str(sample_run_rows[0].get("run_id") or f"legacy_authors_{date_yyyy_mm_dd}") if sample_run_rows else f"legacy_authors_{date_yyyy_mm_dd}"
    params = manifest.get("params")
    if not isinstance(params, dict):
        params = {"date": date_yyyy_mm_dd}
    manifest.setdefault("run_id", run_id)
    manifest.setdefault("job_name", "hydrate-authors")
    manifest.setdefault("date_utc", date_yyyy_mm_dd)
    manifest.setdefault("started_at_utc", f"{date_yyyy_mm_dd}T00:00:00Z")
    manifest.setdefault("success", bool(author_rows_total > 0))
    manifest["params"] = params
    enrich_manifest(manifest, job_name="hydrate-authors", out_base=layout.out_base, params=params)
    if _write_json_if_changed(manifest_path, manifest, dry_run=dry_run):
        summary.authors_manifests_written += 1

    progress_path = layout.authors_progress_json(date_yyyy_mm_dd)
    progress_payload = {
        "job_name": "hydrate-authors",
        "run_id": run_id,
        "started_at_utc": manifest.get("started_at_utc"),
        "updated_at_utc": format_utc(now_utc()),
        "elapsed_s": 0.0,
        "feeds_total": author_rows_total,
        "feeds_done": author_rows_total,
        "feeds_failed": 0,
        "feeds_pending": 0,
        "rps_config": None,
        "concurrency": None,
        "http_errors_by_code": {},
        "rows_written": {"author_profiles": author_rows_total},
    }
    _write_json_if_changed(progress_path, progress_payload, dry_run=dry_run)

    request_provenance_path = layout.authors_request_provenance_csv(date_yyyy_mm_dd)
    if not _csv_header_has_fields(request_provenance_path, {"provenance_level", "partial_reason"}):
        rows = _legacy_request_rows_from_author_profiles(
            source_paths=source_paths,
            sample_family=str(manifest.get("sample_family") or "author_profile_hydration"),
            collection_params_hash=str(manifest.get("collection_params_hash") or ""),
        )
        if rows:
            if not dry_run:
                rewrite_request_provenance_csv(path=request_provenance_path, rows=rows)
            summary.authors_request_provenance_written += 1

    if not dry_run:
        atomic_write_json(layout.authors_quality_report_json(date_yyyy_mm_dd), assess_authors_day(layout, date_yyyy_mm_dd=date_yyyy_mm_dd))
    summary.authors_quality_reports_written += 1


def _backfill_index_day(layout: Layout, *, date_yyyy_mm_dd: str, dry_run: bool, summary: BackfillSummary) -> None:
    manifest_path = layout.feed_generators_index_manifest_json(date_yyyy_mm_dd)
    http_stats_path = layout.feed_generators_index_http_stats_csv(date_yyyy_mm_dd)
    if not manifest_path.exists() and not http_stats_path.exists():
        return

    manifest = _load_json(manifest_path) or {}
    params = manifest.get("params")
    if not isinstance(params, dict):
        params = {"date_utc": date_yyyy_mm_dd}
    manifest.setdefault("run_id", f"legacy_index_{date_yyyy_mm_dd}")
    manifest.setdefault("job_name", "index-feed-generators")
    manifest.setdefault("date_utc", date_yyyy_mm_dd)
    manifest.setdefault("started_at_utc", f"{date_yyyy_mm_dd}T00:00:00Z")
    manifest["params"] = params
    enrich_manifest(manifest, job_name="index-feed-generators", out_base=layout.out_base, params=params)
    if _write_json_if_changed(manifest_path, manifest, dry_run=dry_run):
        summary.index_manifests_written += 1

    request_provenance_path = layout.feed_generators_index_request_provenance_csv(date_yyyy_mm_dd)
    if not _csv_header_has_fields(request_provenance_path, {"provenance_level", "partial_reason"}):
        rows = _legacy_request_rows_from_index_http_stats(
            http_stats_path=http_stats_path,
            sample_family=str(manifest.get("sample_family") or "feed_generator_index"),
            collection_params_hash=str(manifest.get("collection_params_hash") or ""),
            relay_host=str(params.get("relay_host") or ""),
            records_host=str(params.get("records_host") or ""),
            run_id=str(manifest.get("run_id") or f"legacy_index_{date_yyyy_mm_dd}"),
        )
        if rows:
            if not dry_run:
                rewrite_request_provenance_csv(path=request_provenance_path, rows=rows)
            summary.index_request_provenance_written += 1

    if not dry_run:
        atomic_write_json(
            layout.feed_generators_index_quality_report_json(date_yyyy_mm_dd),
            assess_feed_generator_index_day(layout, date_yyyy_mm_dd=date_yyyy_mm_dd),
        )
    summary.index_quality_reports_written += 1


def run_backfill_run_artifacts(*, layout: Layout, dry_run: bool, include_nested_labelerexp: bool = True) -> BackfillSummary:
    summary = BackfillSummary(out_base=str(layout.out_base))

    for hour in _iter_hour_dirs(layout):
        _backfill_snapshot_hour(layout, hour=hour, dry_run=dry_run, summary=summary)

    for date_str in _iter_date_dirs(layout.wide_root):
        _backfill_wide_day(layout, date_yyyy_mm_dd=date_str, dry_run=dry_run, summary=summary)

    for date_str in _iter_date_dirs(layout.metadata_root):
        _backfill_metadata_day(layout, date_yyyy_mm_dd=date_str, dry_run=dry_run, summary=summary)

    for date_str in _iter_date_dirs(layout.authors_root):
        _backfill_authors_day(layout, date_yyyy_mm_dd=date_str, dry_run=dry_run, summary=summary)

    for date_str in _iter_date_dirs(layout.metadata_root):
        _backfill_index_day(layout, date_yyyy_mm_dd=date_str, dry_run=dry_run, summary=summary)

    nested_labelerexp = layout.out_base / "labelerexp"
    if include_nested_labelerexp and nested_labelerexp.exists() and nested_labelerexp.is_dir():
        summary.nested_labelerexp_processed = True
        nested_summary = run_backfill_run_artifacts(
            layout=Layout(out_base=nested_labelerexp),
            dry_run=dry_run,
            include_nested_labelerexp=False,
        )
        summary.absorb(nested_summary)

    if not dry_run:
        atomic_write_json(layout.control_root / "backfill_run_artifacts_summary.json", summary.to_dict())
    return summary
