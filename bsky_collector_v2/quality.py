from __future__ import annotations

import csv
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from bsky_collector_v2.instrumentation import QUALITY_REPORT_VERSION, infer_sample_family
from bsky_collector_v2.layout import Layout
from bsky_collector_v2.study import file_sha256
from bsky_collector_v2.time_utils import MicroWindow, SnapshotHour, format_utc, now_utc

_HARD_THRESHOLDS: dict[str, dict[str, float]] = {
    "snapshot-panel": {
        "min_success_count": 1.0,
        "max_failure_ratio": 0.10,
        "min_request_provenance_completeness": 0.95,
    },
    "wide-sweep": {
        "min_success_count": 1.0,
        "max_failure_ratio": 0.15,
        "min_request_provenance_completeness": 0.95,
    },
    "index-feed-generators": {
        "min_success_count": 1.0,
        "max_failure_ratio": 0.25,
        "min_request_provenance_completeness": 0.90,
    },
}

_MICRO_WINDOW_BASE_THRESHOLDS: dict[str, float] = {
    "start_drift_warn_s": 30.0,
    "start_drift_quarantine_s": 90.0,
    "wall_clock_warn_s": 270.0,
    "wall_clock_quarantine_s": 360.0,
    "max_failure_ratio": 0.05,
    "min_request_provenance_completeness": 0.99,
    "max_next_window_overrun_s": 60.0,
}


def _micro_window_thresholds(*, window_minutes: int) -> dict[str, float]:
    window_s = max(60.0, float(window_minutes) * 60.0)
    base_window_s = 300.0
    scale = window_s / base_window_s
    return {
        "start_drift_warn_s": _MICRO_WINDOW_BASE_THRESHOLDS["start_drift_warn_s"] * scale,
        "start_drift_quarantine_s": _MICRO_WINDOW_BASE_THRESHOLDS["start_drift_quarantine_s"] * scale,
        "wall_clock_warn_s": _MICRO_WINDOW_BASE_THRESHOLDS["wall_clock_warn_s"] * scale,
        "wall_clock_quarantine_s": _MICRO_WINDOW_BASE_THRESHOLDS["wall_clock_quarantine_s"] * scale,
        "max_failure_ratio": _MICRO_WINDOW_BASE_THRESHOLDS["max_failure_ratio"],
        "min_request_provenance_completeness": _MICRO_WINDOW_BASE_THRESHOLDS["min_request_provenance_completeness"],
        "max_next_window_overrun_s": _MICRO_WINDOW_BASE_THRESHOLDS["max_next_window_overrun_s"] * scale,
    }


@dataclass(frozen=True)
class QualityIssue:
    severity: str
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"severity": self.severity, "code": self.code, "message": self.message}


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


def _count_csv_rows(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        with open(path, "r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            return sum(1 for row in reader if any(str(value or "").strip() for value in row.values()))
    except OSError:
        return 0


def _count_matching_parts(parts_dir: Path, prefix: str) -> int:
    if not parts_dir.exists():
        return 0
    total = 0
    for path in sorted(parts_dir.glob(f"{prefix}_part_*.csv")):
        total += _count_csv_rows(path)
    return total


def _count_expected_requests_from_feed_items(parts_dir: Path) -> int:
    if not parts_dir.exists():
        return 0
    keys: set[tuple[str, str, str, str, str]] = set()
    for path in sorted(parts_dir.glob("feed_items_part_*.csv")):
        try:
            with open(path, "r", encoding="utf-8", newline="") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    key = (
                        str(row.get("snapshot_hour_utc") or "").strip(),
                        str(row.get("captured_at_utc") or "").strip(),
                        str(row.get("viewer_mode") or "").strip(),
                        str(row.get("vantage_id") or "").strip(),
                        str(row.get("feed_uri") or "").strip(),
                    )
                    if key[-1]:
                        keys.add(key)
        except OSError:
            continue
    return int(len(keys))


def _count_http_stat_rows(path: Path) -> int:
    return _count_csv_rows(path)


def _count_unique_micro_success_requests(parts_dir: Path) -> int:
    if not parts_dir.exists():
        return 0
    keys: set[tuple[str, str, str]] = set()
    for path in sorted(parts_dir.glob("feed_items_part_*.csv")):
        try:
            with open(path, "r", encoding="utf-8", newline="") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    feed_uri = str(row.get("feed_uri") or "").strip()
                    viewer_mode = str(row.get("viewer_mode") or "").strip()
                    page_no = str(row.get("page_no") or "0").strip() or "0"
                    if not feed_uri:
                        continue
                    keys.add((feed_uri, viewer_mode, page_no))
        except OSError:
            continue
    return len(keys)


def _count_unique_request_tasks(path: Path) -> int:
    if not path.exists():
        return 0
    keys: set[tuple[str, str]] = set()
    with open(path, "r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            endpoint = str(row.get("endpoint") or "").strip()
            feed_uri = str(row.get("feed_uri") or "").strip()
            viewer_mode = str(row.get("viewer_mode") or "").strip()
            if endpoint != "app.bsky.feed.getFeed" or not feed_uri:
                continue
            keys.add((feed_uri, viewer_mode))
    return len(keys)


def _sqlite_feed_task_counts(path: Path) -> tuple[int, int]:
    if not path.exists():
        return 0, 0
    conn = sqlite3.connect(str(path))
    try:
        total_row = conn.execute("SELECT COUNT(*) FROM feed_tasks").fetchone()
        failed_row = conn.execute("SELECT COUNT(*) FROM feed_tasks WHERE status='failed'").fetchone()
        total = int(total_row[0]) if total_row is not None else 0
        failed = int(failed_row[0]) if failed_row is not None else 0
        return total, failed
    finally:
        conn.close()


def _sqlite_feed_task_count(path: Path) -> int:
    total, _failed = _sqlite_feed_task_counts(path)
    return total


def _min_request_started_at(path: Path) -> str | None:
    if not path.exists():
        return None
    earliest: str | None = None
    with open(path, "r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            value = str(row.get("request_started_at_utc") or "").strip()
            if not value:
                continue
            if earliest is None or value < earliest:
                earliest = value
    return earliest


def _parse_iso8601(value: str | None) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _snapshot_drift_minutes(*, started_at_utc: str | None, snapshot_hour_utc: str | None) -> float | None:
    started = _parse_iso8601(started_at_utc)
    snapshot = _parse_iso8601(snapshot_hour_utc)
    if started is None or snapshot is None:
        return None
    return round((started - snapshot).total_seconds() / 60.0, 3)


def _append_issue(issues: list[QualityIssue], *, severity: str, code: str, message: str) -> None:
    issues.append(QualityIssue(severity=severity, code=code, message=message))


def _verdict_for(issues: list[QualityIssue]) -> str:
    return "quarantined" if any(issue.severity == "error" for issue in issues) else "promoted"


def _apply_hard_thresholds(
    *,
    issues: list[QualityIssue],
    job_name: str,
    success_count: int | None,
    total_count: int | None,
    actual_request_count: int,
    expected_request_count: int,
) -> tuple[float | None, float | None]:
    thresholds = _HARD_THRESHOLDS.get(job_name, {})
    failure_ratio: float | None = None
    provenance_completeness: float | None = None

    if success_count is not None and float(success_count) < thresholds.get("min_success_count", 0.0):
        _append_issue(
            issues,
            severity="error",
            code="min_success_count_not_met",
            message=f"success count {success_count} is below minimum {int(thresholds['min_success_count'])}",
        )

    if total_count is not None and total_count > 0:
        failure_count = max(0, int(total_count) - int(success_count or 0))
        failure_ratio = round(failure_count / float(total_count), 6)
        if failure_ratio > thresholds.get("max_failure_ratio", 1.0):
            _append_issue(
                issues,
                severity="error",
                code="failure_ratio_exceeds_threshold",
                message=f"failure ratio {failure_ratio:.3f} exceeds max {thresholds['max_failure_ratio']:.3f}",
            )

    if expected_request_count > 0:
        provenance_completeness = round(actual_request_count / float(expected_request_count), 6)
        if provenance_completeness < thresholds.get("min_request_provenance_completeness", 0.0):
            _append_issue(
                issues,
                severity="error",
                code="request_provenance_completeness_below_threshold",
                message=(
                    f"request provenance completeness {provenance_completeness:.3f} "
                    f"is below minimum {thresholds['min_request_provenance_completeness']:.3f}"
                ),
            )

    return failure_ratio, provenance_completeness


def _resolve_metadata_day(layout: Layout, *, date_yyyy_mm_dd: str, sample_family: str | None) -> tuple[Path, bool, bool, str]:
    candidates: list[Layout] = [layout]
    if sample_family == "experimental_labelerexp_hourly" and layout.out_base.name.lower() == "labelerexp":
        candidates.append(Layout(out_base=layout.out_base.parent))

    chosen = candidates[0]
    for candidate in candidates:
        if candidate.feed_catalog_csv(date_yyyy_mm_dd).exists() or candidate.metadata_discovery_status_json(date_yyyy_mm_dd).exists():
            chosen = candidate
            break

    day_dir = chosen.metadata_day(date_yyyy_mm_dd)
    return (
        day_dir,
        chosen.metadata_discovery_status_json(date_yyyy_mm_dd).exists(),
        chosen.feed_catalog_csv(date_yyyy_mm_dd).exists(),
        str(chosen.out_base),
    )


def assess_snapshot_hour(layout: Layout, *, hour: SnapshotHour) -> dict[str, Any]:
    manifest = _load_json(layout.hourly_manifest_json(hour))
    progress = _load_json(layout.hourly_progress_json(hour))
    request_rows = _count_csv_rows(layout.hourly_request_provenance_csv(hour))
    parts_dir = layout.hourly_parts_dir(hour)
    expected_request_rows = _count_expected_requests_from_feed_items(parts_dir)
    feed_item_rows = _count_matching_parts(parts_dir, "feed_items")
    post_label_rows = _count_matching_parts(parts_dir, "post_labels")
    issues: list[QualityIssue] = []

    if manifest is None:
        _append_issue(issues, severity="error", code="manifest_missing", message="run_manifest.json is missing")
        manifest = {}
    sample_family = str(manifest.get("sample_family") or "regular_hourly")
    metadata_day, discovery_status_exists, feed_catalog_exists, metadata_source_out_base = _resolve_metadata_day(
        layout,
        date_yyyy_mm_dd=hour.date_str,
        sample_family=sample_family,
    )
    if progress is None:
        _append_issue(issues, severity="error", code="progress_missing", message="progress.json is missing")
        progress = {}
    if not feed_item_rows:
        _append_issue(issues, severity="error", code="header_only_output", message="feed_items parts have no data rows")
    if not feed_catalog_exists:
        _append_issue(issues, severity="error", code="same_day_feed_catalog_missing", message="same-day feed_catalog.csv is missing")
    if not discovery_status_exists:
        _append_issue(
            issues,
            severity="error",
            code="same_day_metadata_status_missing",
            message="same-day discovery_status.json is missing",
        )
    if not request_rows:
        _append_issue(
            issues,
            severity="warning",
            code="request_provenance_missing",
            message="request_provenance.csv is missing or empty",
        )

    params = manifest.get("params")
    if not isinstance(params, dict):
        _append_issue(issues, severity="error", code="params_missing", message="manifest params are missing")
        params = {}

    for key in ("posts_per_feed", "time_budget_minutes", "feed_time_budget_s", "accept_language"):
        if params.get(key) in (None, ""):
            _append_issue(issues, severity="warning", code=f"param_{key}_missing", message=f"manifest param {key} is missing")

    if "collection_params_hash" not in manifest:
        _append_issue(
            issues,
            severity="error",
            code="collection_params_hash_missing",
            message="manifest collection_params_hash is missing",
        )
    if "sample_family" not in manifest:
        _append_issue(issues, severity="error", code="sample_family_missing", message="manifest sample_family is missing")
    if manifest.get("success") is not True:
        _append_issue(issues, severity="error", code="run_not_successful", message="manifest success is not true")

    drift_minutes = _snapshot_drift_minutes(
        started_at_utc=manifest.get("started_at_utc"),
        snapshot_hour_utc=manifest.get("snapshot_hour_utc"),
    )
    if drift_minutes is not None:
        if drift_minutes > 45.0:
            _append_issue(
                issues,
                severity="error",
                code="timestamp_drift_exceeds_threshold",
                message=f"snapshot start drift is {drift_minutes:.1f} minutes",
            )
        elif drift_minutes > 30.0:
            _append_issue(
                issues,
                severity="warning",
                code="timestamp_drift_warning",
                message=f"snapshot start drift is {drift_minutes:.1f} minutes",
            )

    failure_ratio, provenance_completeness = _apply_hard_thresholds(
        issues=issues,
        job_name="snapshot-panel",
        success_count=progress.get("feeds_done"),
        total_count=progress.get("feeds_total"),
        actual_request_count=request_rows,
        expected_request_count=expected_request_rows,
    )

    return {
        "quality_report_version": QUALITY_REPORT_VERSION,
        "generated_at_utc": format_utc(now_utc()),
        "job_name": "snapshot-panel",
        "sample_family": manifest.get("sample_family")
        or infer_sample_family(job_name="snapshot-panel", out_base=layout.out_base, accept_labelers=params.get("accept_labelers")),
        "scope": {"snapshot_hour_utc": hour.hour_iso_z, "date_utc": hour.date_str},
        "run_id": manifest.get("run_id"),
        "collection_params_hash": manifest.get("collection_params_hash"),
        "verdict": _verdict_for(issues),
        "issues": [issue.to_dict() for issue in issues],
        "metrics": {
            "feed_item_rows": feed_item_rows,
            "post_label_rows": post_label_rows,
            "request_provenance_rows": request_rows,
            "expected_request_rows": expected_request_rows,
            "request_provenance_completeness": provenance_completeness,
            "drift_minutes": drift_minutes,
            "failure_ratio": failure_ratio,
            "same_day_feed_catalog_exists": feed_catalog_exists,
            "same_day_discovery_status_exists": discovery_status_exists,
            "metadata_source_out_base": metadata_source_out_base,
            "feeds_done": progress.get("feeds_done"),
            "feeds_failed": progress.get("feeds_failed"),
        },
    }


def assess_micro5_window(
    layout: Layout,
    *,
    study_id: str,
    sample_family: str,
    window=None,  # noqa: ANN001
    date_yyyy_mm_dd: str | None = None,
    hour_str: str | None = None,
    minute_str: str | None = None,
) -> dict[str, Any]:
    if window is not None:
        if hasattr(window, "date_str") and hasattr(window, "hour_str") and hasattr(window, "minute_str"):
            date_yyyy_mm_dd = str(window.date_str)
            hour_str = str(window.hour_str)
            minute_str = str(window.minute_str)
        else:
            start_utc = getattr(window, "scheduled_window_start_utc", None) or getattr(window, "start_utc", None)
            if start_utc is None:
                raise ValueError("window object does not expose scheduled_window_start_utc or start_utc")
            date_yyyy_mm_dd = start_utc.date().isoformat()
            hour_str = f"{start_utc.hour:02d}"
            minute_str = f"{start_utc.minute:02d}"
    if date_yyyy_mm_dd is None or hour_str is None or minute_str is None:
        raise ValueError("assess_micro5_window requires window=... or explicit date/hour/minute components")

    manifest_path = layout.micro5_manifest_json(
        study_id=study_id,
        sample_family=sample_family,
        date_yyyy_mm_dd=date_yyyy_mm_dd,
        hour_str=hour_str,
        minute_str=minute_str,
    )
    progress_path = layout.micro5_progress_json(
        study_id=study_id,
        sample_family=sample_family,
        date_yyyy_mm_dd=date_yyyy_mm_dd,
        hour_str=hour_str,
        minute_str=minute_str,
    )
    request_path = layout.micro5_request_provenance_csv(
        study_id=study_id,
        sample_family=sample_family,
        date_yyyy_mm_dd=date_yyyy_mm_dd,
        hour_str=hour_str,
        minute_str=minute_str,
    )
    status_path = layout.micro5_status_sqlite(
        study_id=study_id,
        sample_family=sample_family,
        date_yyyy_mm_dd=date_yyyy_mm_dd,
        hour_str=hour_str,
        minute_str=minute_str,
    )
    parts_dir = layout.micro5_parts_dir(
        study_id=study_id,
        sample_family=sample_family,
        date_yyyy_mm_dd=date_yyyy_mm_dd,
        hour_str=hour_str,
        minute_str=minute_str,
    )
    auth_snapshot_path = layout.micro5_auth_preference_snapshot_json(
        study_id=study_id,
        sample_family=sample_family,
        date_yyyy_mm_dd=date_yyyy_mm_dd,
        hour_str=hour_str,
        minute_str=minute_str,
    )

    manifest = _load_json(manifest_path)
    progress = _load_json(progress_path)
    study_manifest = _load_json(layout.study_manifest_json(study_id))
    issues: list[QualityIssue] = []

    if manifest is None:
        _append_issue(issues, severity="error", code="manifest_missing", message="run_manifest.json is missing")
        manifest = {}
    if progress is None:
        _append_issue(issues, severity="error", code="progress_missing", message="progress.json is missing")
        progress = {}
    if study_manifest is None:
        _append_issue(issues, severity="error", code="study_manifest_missing", message="study manifest is missing")
        study_manifest = {}
    window_minutes = int(manifest.get("window_minutes") or manifest.get("params", {}).get("window_minutes") or 5)
    thresholds = _micro_window_thresholds(window_minutes=window_minutes)

    if manifest.get("study_id") in (None, ""):
        _append_issue(issues, severity="error", code="study_id_missing", message="run manifest study_id is missing")

    scheduled_start_raw = str(manifest.get("scheduled_window_start_utc") or "").strip()
    scheduled_end_raw = str(manifest.get("scheduled_window_end_utc") or "").strip()
    if not scheduled_start_raw:
        _append_issue(issues, severity="error", code="scheduled_window_start_utc_missing", message="scheduled_window_start_utc is missing")
    if not scheduled_end_raw:
        _append_issue(issues, severity="error", code="scheduled_window_end_utc_missing", message="scheduled_window_end_utc is missing")

    run_panel_hash = str(manifest.get("panel_hash") or "").strip()
    study_panel_hash = str(study_manifest.get("panel_hash") or "").strip()
    panel_path_raw = str(study_manifest.get("panel_path") or "").strip()
    live_panel_hash = None
    if panel_path_raw:
        panel_path = Path(panel_path_raw)
        if panel_path.exists():
            live_panel_hash = file_sha256(panel_path)
    if not run_panel_hash or not study_panel_hash:
        _append_issue(issues, severity="error", code="panel_hash_missing", message="panel hash is missing from run or study manifest")
    elif run_panel_hash != study_panel_hash:
        _append_issue(
            issues,
            severity="error",
            code="panel_hash_mismatch",
            message=f"run panel hash {run_panel_hash} does not match study panel hash {study_panel_hash}",
        )
    if live_panel_hash is not None and study_panel_hash and live_panel_hash != study_panel_hash:
        _append_issue(
            issues,
            severity="error",
            code="panel_hash_mismatch",
            message=f"frozen panel file hash {live_panel_hash} does not match study panel hash {study_panel_hash}",
        )

    feed_item_rows = _count_matching_parts(parts_dir, "feed_items")
    if feed_item_rows <= 0:
        _append_issue(issues, severity="error", code="header_only_output", message="feed_items parts have no data rows")

    request_rows = _count_csv_rows(request_path)
    if request_rows <= 0:
        _append_issue(issues, severity="error", code="request_provenance_missing", message="request_provenance.csv is missing or empty")

    total_task_count, failed_task_count = _sqlite_feed_task_counts(status_path)
    successful_request_rows = _count_unique_micro_success_requests(parts_dir)
    params = manifest.get("params")
    if not isinstance(params, dict):
        params = {}
    requested_viewer_modes = tuple(str(mode) for mode in params.get("viewer_modes") or ())
    auth_setup_requests = 2 if "auth" in requested_viewer_modes else 0
    expected_request_rows = max(total_task_count, successful_request_rows + failed_task_count) + auth_setup_requests
    provenance_completeness: float | None = None
    if expected_request_rows > 0:
        provenance_completeness = round(request_rows / float(expected_request_rows), 6)
        if provenance_completeness < thresholds["min_request_provenance_completeness"]:
            _append_issue(
                issues,
                severity="error",
                code="request_provenance_completeness_below_threshold",
                message=(
                    f"request provenance completeness {provenance_completeness:.3f} "
                    f"is below minimum {thresholds['min_request_provenance_completeness']:.3f}"
                ),
            )

    actual_start = _min_request_started_at(request_path) or manifest.get("started_at_utc")
    actual_start_dt = _parse_iso8601(actual_start)
    scheduled_start_dt = _parse_iso8601(scheduled_start_raw)
    scheduled_end_dt = _parse_iso8601(scheduled_end_raw)
    finished_dt = _parse_iso8601(str(manifest.get("finished_at_utc") or ""))

    start_drift_s: float | None = None
    if actual_start_dt is not None and scheduled_start_dt is not None:
        start_drift_s = round((actual_start_dt - scheduled_start_dt).total_seconds(), 3)
        if start_drift_s > thresholds["start_drift_quarantine_s"]:
            _append_issue(
                issues,
                severity="error",
                code="start_drift_exceeds_threshold",
                message=f"window start drift is {start_drift_s:.1f}s",
            )
        elif start_drift_s > thresholds["start_drift_warn_s"]:
            _append_issue(
                issues,
                severity="warning",
                code="start_drift_warning",
                message=f"window start drift is {start_drift_s:.1f}s",
            )

    wall_clock_duration_s: float | None = None
    finish_overrun_s: float | None = None
    if scheduled_start_dt is not None and finished_dt is not None:
        wall_clock_duration_s = round((finished_dt - scheduled_start_dt).total_seconds(), 3)
        if wall_clock_duration_s > thresholds["wall_clock_quarantine_s"]:
            _append_issue(
                issues,
                severity="error",
                code="wall_clock_duration_exceeds_threshold",
                message=f"wall-clock duration is {wall_clock_duration_s:.1f}s",
            )
        elif wall_clock_duration_s > thresholds["wall_clock_warn_s"]:
            _append_issue(
                issues,
                severity="warning",
                code="wall_clock_duration_warning",
                message=f"wall-clock duration is {wall_clock_duration_s:.1f}s",
            )
    if scheduled_end_dt is not None and finished_dt is not None:
        finish_overrun_s = round(max(0.0, (finished_dt - scheduled_end_dt).total_seconds()), 3)
        if finish_overrun_s > thresholds["max_next_window_overrun_s"]:
            _append_issue(
                issues,
                severity="error",
                code="window_overrun_exceeds_threshold",
                message=f"window overran the scheduled end by {finish_overrun_s:.1f}s",
            )

    feeds_total = progress.get("feeds_total")
    feeds_done = progress.get("feeds_done")
    failure_ratio: float | None = None
    if isinstance(feeds_total, int) and feeds_total > 0:
        feeds_failed = int(progress.get("feeds_failed") or max(0, feeds_total - int(feeds_done or 0)))
        failure_ratio = round(feeds_failed / float(feeds_total), 6)
        if failure_ratio > thresholds["max_failure_ratio"]:
            _append_issue(
                issues,
                severity="error",
                code="failure_ratio_exceeds_threshold",
                message=f"failure ratio {failure_ratio:.3f} exceeds max {thresholds['max_failure_ratio']:.3f}",
            )
        pending_tasks = max(0, feeds_total - int(progress.get("feeds_done") or 0) - int(progress.get("feeds_failed") or 0))
        if pending_tasks > 0:
            _append_issue(
                issues,
                severity="error",
                code="pending_tasks_remaining",
                message=f"{pending_tasks} task(s) remained pending when the window finished",
            )

    if "auth" in requested_viewer_modes and not auth_snapshot_path.exists():
        _append_issue(
            issues,
            severity="error",
            code="auth_snapshot_missing",
            message="auth_preference_snapshot.json is missing for an auth study window",
        )

    run_sample_family = str(manifest.get("sample_family") or sample_family)
    if run_sample_family == "micro5_extended_sharded":
        for key in ("shard_id", "shard_count", "shard_membership_hash"):
            if manifest.get(key) in (None, ""):
                _append_issue(issues, severity="error", code=f"{key}_missing", message=f"{key} is missing for sharded study output")

    if manifest.get("success") is not True:
        _append_issue(issues, severity="error", code="run_not_successful", message="manifest success is not true")

    return {
        "quality_report_version": QUALITY_REPORT_VERSION,
        "generated_at_utc": format_utc(now_utc()),
        "job_name": "micro-snapshot-study",
        "study_id": study_id,
        "sample_family": run_sample_family,
        "scope": {
            "scheduled_window_start_utc": scheduled_start_raw,
            "scheduled_window_end_utc": scheduled_end_raw,
            "window_index": manifest.get("window_index"),
            "window_minute": manifest.get("window_minute"),
        },
        "run_id": manifest.get("run_id"),
        "collection_params_hash": manifest.get("collection_params_hash"),
        "thresholds": dict(thresholds),
        "verdict": _verdict_for(issues),
        "issues": [issue.to_dict() for issue in issues],
        "metrics": {
            "feed_item_rows": feed_item_rows,
            "request_provenance_rows": request_rows,
            "successful_request_rows": successful_request_rows,
            "expected_request_rows": expected_request_rows,
            "request_provenance_completeness": provenance_completeness,
            "start_drift_s": start_drift_s,
            "wall_clock_duration_s": wall_clock_duration_s,
            "finish_overrun_s": finish_overrun_s,
            "failure_ratio": failure_ratio,
            "auth_snapshot_present": auth_snapshot_path.exists(),
            "panel_hash_match": bool(
                run_panel_hash
                and study_panel_hash
                and run_panel_hash == study_panel_hash
                and (live_panel_hash is None or live_panel_hash == study_panel_hash)
            ),
            "total_task_count": total_task_count,
            "failed_task_count": failed_task_count,
            "feeds_done": progress.get("feeds_done"),
            "feeds_failed": progress.get("feeds_failed"),
        },
    }


def assess_wide_day(layout: Layout, *, date_yyyy_mm_dd: str) -> dict[str, Any]:
    manifest = _load_json(layout.wide_manifest_json(date_yyyy_mm_dd))
    progress = _load_json(layout.wide_progress_json(date_yyyy_mm_dd))
    request_rows = _count_csv_rows(layout.wide_request_provenance_csv(date_yyyy_mm_dd))
    parts_dir = layout.wide_parts_dir(date_yyyy_mm_dd)
    expected_request_rows = _count_expected_requests_from_feed_items(parts_dir)
    feed_item_rows = _count_matching_parts(parts_dir, "feed_items")
    issues: list[QualityIssue] = []

    if manifest is None:
        _append_issue(issues, severity="error", code="manifest_missing", message="run_manifest.json is missing")
        manifest = {}
    sample_family = str(manifest.get("sample_family") or "wide")
    _metadata_day, discovery_status_exists, feed_catalog_exists, metadata_source_out_base = _resolve_metadata_day(
        layout,
        date_yyyy_mm_dd=date_yyyy_mm_dd,
        sample_family=sample_family,
    )
    if progress is None:
        _append_issue(issues, severity="error", code="progress_missing", message="progress.json is missing")
        progress = {}
    if not feed_item_rows:
        _append_issue(issues, severity="error", code="header_only_output", message="feed_items parts have no data rows")
    if not feed_catalog_exists:
        _append_issue(issues, severity="error", code="same_day_feed_catalog_missing", message="same-day feed_catalog.csv is missing")
    if not discovery_status_exists:
        _append_issue(
            issues,
            severity="error",
            code="same_day_metadata_status_missing",
            message="same-day discovery_status.json is missing",
        )
    if not request_rows:
        _append_issue(
            issues,
            severity="warning",
            code="request_provenance_missing",
            message="request_provenance.csv is missing or empty",
        )

    params = manifest.get("params")
    if not isinstance(params, dict):
        _append_issue(issues, severity="error", code="params_missing", message="manifest params are missing")
        params = {}

    if "collection_params_hash" not in manifest:
        _append_issue(
            issues,
            severity="error",
            code="collection_params_hash_missing",
            message="manifest collection_params_hash is missing",
        )
    if "sample_family" not in manifest:
        _append_issue(issues, severity="error", code="sample_family_missing", message="manifest sample_family is missing")
    if manifest.get("success") is not True:
        _append_issue(issues, severity="error", code="run_not_successful", message="manifest success is not true")

    failure_ratio, provenance_completeness = _apply_hard_thresholds(
        issues=issues,
        job_name="wide-sweep",
        success_count=progress.get("feeds_done"),
        total_count=progress.get("feeds_total"),
        actual_request_count=request_rows,
        expected_request_count=expected_request_rows,
    )

    return {
        "quality_report_version": QUALITY_REPORT_VERSION,
        "generated_at_utc": format_utc(now_utc()),
        "job_name": "wide-sweep",
        "sample_family": manifest.get("sample_family")
        or infer_sample_family(job_name="wide-sweep", out_base=layout.out_base, accept_labelers=params.get("accept_labelers")),
        "scope": {"date_utc": date_yyyy_mm_dd},
        "run_id": manifest.get("run_id"),
        "collection_params_hash": manifest.get("collection_params_hash"),
        "verdict": _verdict_for(issues),
        "issues": [issue.to_dict() for issue in issues],
        "metrics": {
            "feed_item_rows": feed_item_rows,
            "request_provenance_rows": request_rows,
            "expected_request_rows": expected_request_rows,
            "request_provenance_completeness": provenance_completeness,
            "failure_ratio": failure_ratio,
            "same_day_feed_catalog_exists": feed_catalog_exists,
            "same_day_discovery_status_exists": discovery_status_exists,
            "metadata_source_out_base": metadata_source_out_base,
            "feeds_done": progress.get("feeds_done"),
            "feeds_failed": progress.get("feeds_failed"),
        },
    }


def assess_discovery_day(layout: Layout, *, date_yyyy_mm_dd: str) -> dict[str, Any]:
    status = _load_json(layout.metadata_discovery_status_json(date_yyyy_mm_dd))
    manifest = _load_json(layout.metadata_manifest_json(date_yyyy_mm_dd))
    feed_catalog_rows = _count_csv_rows(layout.feed_catalog_csv(date_yyyy_mm_dd))
    issues: list[QualityIssue] = []

    if manifest is None:
        _append_issue(issues, severity="error", code="manifest_missing", message="run_manifest.json is missing")
        manifest = {}
    if status is None:
        _append_issue(issues, severity="error", code="discovery_status_missing", message="discovery_status.json is missing")
        status = {}
    if feed_catalog_rows <= 0:
        _append_issue(issues, severity="error", code="feed_catalog_missing", message="feed_catalog.csv is missing or empty")
    if status.get("success") is not True:
        _append_issue(
            issues,
            severity="warning",
            code="metadata_not_marked_successful",
            message="discovery status success is not true",
        )

    surfaces = status.get("surfaces")
    if isinstance(surfaces, dict):
        failed_surfaces = sorted(surface for surface, row in surfaces.items() if isinstance(row, dict) and row.get("status") == "failed")
        if failed_surfaces:
            _append_issue(
                issues,
                severity="warning",
                code="metadata_surface_failures",
                message=f"failed discovery surfaces: {', '.join(failed_surfaces)}",
            )

    return {
        "quality_report_version": QUALITY_REPORT_VERSION,
        "generated_at_utc": format_utc(now_utc()),
        "job_name": "refresh-discovery",
        "sample_family": manifest.get("sample_family")
        or infer_sample_family(job_name="refresh-discovery", out_base=layout.out_base),
        "scope": {"date_utc": date_yyyy_mm_dd},
        "run_id": manifest.get("run_id") or status.get("run_id"),
        "collection_params_hash": manifest.get("collection_params_hash"),
        "verdict": _verdict_for(issues),
        "issues": [issue.to_dict() for issue in issues],
        "metrics": {
            "feed_catalog_rows": feed_catalog_rows,
            "surface_count": len(surfaces) if isinstance(surfaces, dict) else 0,
        },
    }


def assess_authors_day(layout: Layout, *, date_yyyy_mm_dd: str) -> dict[str, Any]:
    manifest = _load_json(layout.authors_manifest_json(date_yyyy_mm_dd))
    progress = _load_json(layout.authors_progress_json(date_yyyy_mm_dd))
    request_rows = _count_csv_rows(layout.authors_request_provenance_csv(date_yyyy_mm_dd))
    author_rows = _count_csv_rows(layout.effective_timeseries_root / "authors" / date_yyyy_mm_dd / "author_profiles.csv")
    if author_rows <= 0:
        author_rows = _count_csv_rows(layout.authors_day_dir(date_yyyy_mm_dd) / "author_profiles_part_000.csv")
    issues: list[QualityIssue] = []

    if manifest is None:
        _append_issue(issues, severity="error", code="manifest_missing", message="run_manifest.json is missing")
        manifest = {}
    if progress is None:
        _append_issue(issues, severity="warning", code="progress_missing", message="progress.json is missing")
        progress = {}
    if author_rows <= 0:
        _append_issue(issues, severity="error", code="header_only_output", message="author profile output is missing or empty")
    if request_rows <= 0:
        _append_issue(issues, severity="warning", code="request_provenance_missing", message="request_provenance.csv is missing or empty")

    params = manifest.get("params")
    if not isinstance(params, dict):
        params = {}

    return {
        "quality_report_version": QUALITY_REPORT_VERSION,
        "generated_at_utc": format_utc(now_utc()),
        "job_name": "hydrate-authors",
        "sample_family": manifest.get("sample_family")
        or infer_sample_family(job_name="hydrate-authors", out_base=layout.out_base, accept_labelers=params.get("accept_labelers")),
        "scope": {"date_utc": date_yyyy_mm_dd},
        "run_id": manifest.get("run_id"),
        "collection_params_hash": manifest.get("collection_params_hash"),
        "verdict": _verdict_for(issues),
        "issues": [issue.to_dict() for issue in issues],
        "metrics": {
            "author_rows": author_rows,
            "request_provenance_rows": request_rows,
            "feeds_done": progress.get("feeds_done"),
            "feeds_failed": progress.get("feeds_failed"),
        },
    }


def assess_feed_generator_index_day(layout: Layout, *, date_yyyy_mm_dd: str) -> dict[str, Any]:
    manifest = _load_json(layout.feed_generators_index_manifest_json(date_yyyy_mm_dd))
    progress = _load_json(layout.feed_generators_index_progress_json(date_yyyy_mm_dd))
    request_rows = _count_csv_rows(layout.feed_generators_index_request_provenance_csv(date_yyyy_mm_dd))
    expected_request_rows = _count_http_stat_rows(layout.feed_generators_index_http_stats_csv(date_yyyy_mm_dd))
    parts_dir = layout.feed_generators_index_parts_dir(date_yyyy_mm_dd)
    part_rows = 0
    if parts_dir.exists():
        for path in sorted(parts_dir.glob("feed_generators_part_*.jsonl")):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    part_rows += sum(1 for line in fh if line.strip() and "\"__meta__\": true" not in line)
            except OSError:
                continue
    issues: list[QualityIssue] = []

    if manifest is None:
        _append_issue(issues, severity="error", code="manifest_missing", message="run_manifest.json is missing")
        manifest = {}
    if progress is None:
        _append_issue(issues, severity="warning", code="progress_missing", message="progress.json is missing")
        progress = {}
    if part_rows <= 0:
        _append_issue(issues, severity="warning", code="no_part_records", message="no feed generator records were written")
    if request_rows <= 0:
        _append_issue(issues, severity="warning", code="request_provenance_missing", message="request_provenance.csv is missing or empty")

    params = manifest.get("params")
    if not isinstance(params, dict):
        params = {}

    failure_ratio, provenance_completeness = _apply_hard_thresholds(
        issues=issues,
        job_name="index-feed-generators",
        success_count=progress.get("feeds_done"),
        total_count=progress.get("feeds_total"),
        actual_request_count=request_rows,
        expected_request_count=expected_request_rows,
    )

    return {
        "quality_report_version": QUALITY_REPORT_VERSION,
        "generated_at_utc": format_utc(now_utc()),
        "job_name": "index-feed-generators",
        "sample_family": manifest.get("sample_family")
        or infer_sample_family(job_name="index-feed-generators", out_base=layout.out_base, accept_labelers=params.get("accept_labelers")),
        "scope": {"date_utc": date_yyyy_mm_dd},
        "run_id": manifest.get("run_id"),
        "collection_params_hash": manifest.get("collection_params_hash"),
        "verdict": _verdict_for(issues),
        "issues": [issue.to_dict() for issue in issues],
        "metrics": {
            "part_rows": part_rows,
            "request_provenance_rows": request_rows,
            "expected_request_rows": expected_request_rows,
            "request_provenance_completeness": provenance_completeness,
            "failure_ratio": failure_ratio,
            "feeds_done": progress.get("feeds_done"),
            "feeds_failed": progress.get("feeds_failed"),
        },
    }
