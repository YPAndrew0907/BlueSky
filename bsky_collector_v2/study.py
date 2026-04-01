from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from bsky_collector_v2.fs_utils import atomic_write_json, ensure_dir
from bsky_collector_v2.layout import Layout
from bsky_collector_v2.time_utils import format_utc, now_utc

FOR_YOU_FEED_URI = "at://did:plc:3guzzweuqraryl3rdkimjamk/app.bsky.feed.generator/for-you"
MICRO5_WINDOW_MINUTES = 5
CORE_SAMPLE_FAMILY = "micro5_core_full"
EXTENDED_SAMPLE_FAMILY = "micro5_extended_sharded"

StudySampleFamily = Literal["micro5_core_full", "micro5_extended_sharded"]
StudyPanelRow = dict[str, str]


def parse_utc_datetime(value: str) -> datetime:
    raw = str(value).strip()
    if raw.endswith("Z"):
        raw = raw.removesuffix("Z") + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def floor_to_window_utc(dt: datetime, *, window_minutes: int) -> datetime:
    if window_minutes <= 0 or 60 % window_minutes != 0:
        raise ValueError(f"window_minutes must divide 60, got {window_minutes}")
    dt_utc = dt.astimezone(UTC)
    floored_minute = (dt_utc.minute // window_minutes) * window_minutes
    return dt_utc.replace(minute=floored_minute, second=0, microsecond=0)


def ceil_to_window_utc(dt: datetime, *, window_minutes: int) -> datetime:
    floored = floor_to_window_utc(dt, window_minutes=window_minutes)
    aligned = dt.astimezone(UTC).replace(second=0, microsecond=0)
    if floored == aligned:
        return floored
    return floored + timedelta(minutes=window_minutes)


def window_end_utc(window_start_utc: datetime, *, window_minutes: int) -> datetime:
    return window_start_utc.astimezone(UTC) + timedelta(minutes=window_minutes)


def validate_window_start(window_start_utc: datetime, *, window_minutes: int) -> None:
    dt = window_start_utc.astimezone(UTC)
    if dt.second != 0 or dt.microsecond != 0:
        raise ValueError(f"window start must be on an exact minute, got {format_utc(dt)}")
    if dt.minute % window_minutes != 0:
        raise ValueError(
            f"window start minute must be a multiple of {window_minutes}, got minute={dt.minute}"
        )


def compute_window_index(
    *,
    anchor_start_utc: datetime,
    scheduled_window_start_utc: datetime,
    window_minutes: int,
) -> int:
    validate_window_start(anchor_start_utc, window_minutes=window_minutes)
    validate_window_start(scheduled_window_start_utc, window_minutes=window_minutes)
    delta_s = (scheduled_window_start_utc.astimezone(UTC) - anchor_start_utc.astimezone(UTC)).total_seconds()
    window_s = window_minutes * 60
    if delta_s < 0:
        raise ValueError("scheduled window is earlier than the study anchor")
    if delta_s % window_s != 0:
        raise ValueError("scheduled window is not aligned to the study anchor")
    return int(delta_s // window_s)


@dataclass(frozen=True)
class StudyWindow:
    scheduled_window_start_utc: datetime
    scheduled_window_end_utc: datetime
    window_minutes: int
    window_index: int
    window_minute: int

    @property
    def date_str(self) -> str:
        return self.scheduled_window_start_utc.astimezone(UTC).date().isoformat()

    @property
    def hour_str(self) -> str:
        return f"{self.scheduled_window_start_utc.astimezone(UTC).hour:02d}"

    @property
    def minute_str(self) -> str:
        return f"{self.scheduled_window_start_utc.astimezone(UTC).minute:02d}"

    @property
    def start_iso_z(self) -> str:
        return format_utc(self.scheduled_window_start_utc)

    @property
    def end_iso_z(self) -> str:
        return format_utc(self.scheduled_window_end_utc)


def compute_study_window(
    *,
    window_origin_utc: datetime,
    scheduled_window_start_utc: datetime,
    window_minutes: int = MICRO5_WINDOW_MINUTES,
) -> StudyWindow:
    anchor = floor_to_window_utc(window_origin_utc, window_minutes=window_minutes)
    start = floor_to_window_utc(scheduled_window_start_utc, window_minutes=window_minutes)
    return StudyWindow(
        scheduled_window_start_utc=start,
        scheduled_window_end_utc=window_end_utc(start, window_minutes=window_minutes),
        window_minutes=int(window_minutes),
        window_index=compute_window_index(
            anchor_start_utc=anchor,
            scheduled_window_start_utc=start,
            window_minutes=window_minutes,
        ),
        window_minute=int(start.minute),
    )


def deterministic_seed(*parts: object) -> int:
    payload = "||".join(str(part) for part in parts)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return int(digest[:16], 16)


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def panel_file_hash(path: Path) -> str:
    return file_sha256(path)


def panel_membership_hash(rows: list[dict[str, str]]) -> str:
    payload = "\n".join(
        str(row.get("feed_uri") or "").strip()
        for row in rows
        if str(row.get("feed_uri") or "").strip()
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def shard_membership_hash(rows: list[dict[str, str]]) -> str:
    return panel_membership_hash(rows)


def read_panel_rows(panel_path: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    with panel_path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            feed_uri = str(row.get("feed_uri") or "").strip()
            if not feed_uri:
                continue
            rows.append({str(key): str(value or "") for key, value in row.items() if key is not None})
    return rows


def read_study_panel_rows(panel_path: Path) -> list[dict[str, str]]:
    return read_panel_rows(panel_path)


@dataclass(frozen=True)
class StudyPanelRow:
    feed_uri: str
    bucket: str
    unauth_skip: int
    built_at_utc: str | None
    panel_version_id: str | None

    def to_dict(self) -> dict[str, str]:
        return {
            "feed_uri": self.feed_uri,
            "bucket": self.bucket,
            "unauth_skip": str(self.unauth_skip),
            "built_at_utc": str(self.built_at_utc or ""),
            "panel_version_id": str(self.panel_version_id or ""),
        }


def read_study_panel_rows(panel_path: Path) -> list[StudyPanelRow]:
    return [
        StudyPanelRow(
            feed_uri=str(row.get("feed_uri") or "").strip(),
            bucket=str(row.get("bucket") or "").strip() or "unknown",
            unauth_skip=int(str(row.get("unauth_skip") or "0").strip() or 0),
            built_at_utc=(str(row.get("built_at_utc") or "").strip() or None),
            panel_version_id=(str(row.get("panel_version_id") or "").strip() or None),
        )
        for row in read_panel_rows(panel_path)
        if str(row.get("feed_uri") or "").strip()
    ]


def panel_version_id_from_rows(rows: list[dict[str, str]]) -> str | None:
    version_ids = sorted(
        {
            str(row.get("panel_version_id") or "").strip()
            for row in rows
            if str(row.get("panel_version_id") or "").strip()
        }
    )
    return version_ids[-1] if version_ids else None


def write_panel_rows(panel_path: Path, rows: list[dict[str, str]] | list[StudyPanelRow]) -> None:
    if not rows:
        raise ValueError("cannot freeze an empty panel")
    ensure_dir(panel_path.parent)
    normalized_rows: list[dict[str, str]] = []
    for row in rows:
        if hasattr(row, "to_dict"):
            normalized_rows.append(getattr(row, "to_dict")())
        else:
            normalized_rows.append(dict(row))
    fieldnames = list(normalized_rows[0].keys())
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="",
        prefix=panel_path.name + ".tmp.",
        dir=str(panel_path.parent),
        delete=False,
    ) as tmp:
        tmp_path = Path(tmp.name)
        writer = csv.DictWriter(tmp, fieldnames=fieldnames)
        writer.writeheader()
        for row in normalized_rows:
            writer.writerow({name: row.get(name, "") for name in fieldnames})
        tmp.flush()
        os.fsync(tmp.fileno())
    os.replace(tmp_path, panel_path)


def write_study_panel_rows(panel_path: Path, rows: list[StudyPanelRow]) -> None:
    write_panel_rows(panel_path, [row.to_dict() for row in rows])


def effective_request_limit(*, feed_uri: str, posts_per_feed: int) -> int:
    if str(feed_uri) == FOR_YOU_FEED_URI:
        return 1
    return max(1, min(100, int(posts_per_feed)))


def requests_per_feed(*, feed_uri: str, posts_per_feed: int) -> int:
    posts = max(0, int(posts_per_feed))
    if posts == 0:
        return 0
    limit = effective_request_limit(feed_uri=feed_uri, posts_per_feed=posts)
    return int(math.ceil(posts / float(limit)))


def expected_snapshot_requests_for_panel(
    *,
    panel_rows: list[dict[str, str]],
    viewer_modes: tuple[str, ...],
    posts_per_feed: int,
    include_auth_session_setup: bool,
) -> int:
    total = 0
    for row in panel_rows:
        feed_uri = str(row.get("feed_uri") or "").strip()
        if not feed_uri:
            continue
        unauth_skip = str(row.get("unauth_skip") or "0").strip() == "1"
        per_feed_requests = requests_per_feed(feed_uri=feed_uri, posts_per_feed=posts_per_feed)
        if "unauth" in viewer_modes and not unauth_skip:
            total += per_feed_requests
        if "auth" in viewer_modes:
            total += per_feed_requests
    if include_auth_session_setup and "auth" in viewer_modes:
        total += 2
    return total


def select_shard_rows(
    *,
    panel_rows: list[dict[str, str]],
    shard_count: int,
    shard_id: int,
    shard_seed: int,
) -> list[dict[str, str]]:
    if shard_count < 1:
        raise ValueError(f"shard_count must be >= 1, got {shard_count}")
    if shard_id < 0 or shard_id >= shard_count:
        raise ValueError(f"shard_id must be within [0, {shard_count}), got {shard_id}")
    keyed_rows: list[tuple[str, str, dict[str, str]]] = []
    for row in panel_rows:
        feed_uri = str(row.get("feed_uri") or "").strip()
        if not feed_uri:
            continue
        sort_key = hashlib.sha256(f"{shard_seed}:{feed_uri}".encode("utf-8")).hexdigest()
        keyed_rows.append((sort_key, feed_uri, row))
    keyed_rows.sort(key=lambda item: (item[0], item[1]))
    return [row for idx, (_sort_key, _feed_uri, row) in enumerate(keyed_rows) if idx % shard_count == shard_id]


def shard_rows(
    panel_rows: list[dict[str, str]],
    *,
    shard_count: int,
    shard_seed: int | str,
) -> dict[int, list[dict[str, str]]]:
    normalized_seed = deterministic_seed(shard_seed)
    return {
        shard_id: select_shard_rows(
            panel_rows=panel_rows,
            shard_count=int(shard_count),
            shard_id=shard_id,
            shard_seed=normalized_seed,
        )
        for shard_id in range(int(shard_count))
    }


def deterministic_randomization_seed(*, study_id: str, scheduled_window_start_utc: str, shard_id: int | None) -> str:
    shard_token = "all" if shard_id is None else f"shard-{int(shard_id)}"
    payload = f"{study_id}||{scheduled_window_start_utc}||{shard_token}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def shard_rows(rows: list[StudyPanelRow], *, shard_count: int, shard_seed: str | int) -> dict[int, list[StudyPanelRow]]:
    shards: dict[int, list[StudyPanelRow]] = {idx: [] for idx in range(int(shard_count))}
    for row in rows:
        shard_id = int(hashlib.sha256(f"{shard_seed}:{row.feed_uri}".encode("utf-8")).hexdigest()[:16], 16) % int(shard_count)
        shards[shard_id].append(row)
    return shards


def shard_membership_hash(rows: list[StudyPanelRow]) -> str:
    digest = hashlib.sha256()
    for feed_uri in sorted(row.feed_uri for row in rows):
        digest.update(feed_uri.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def build_study_id(*, sample_family: str, created_at_utc: datetime | None = None) -> str:
    created = (created_at_utc or now_utc()).astimezone(UTC)
    compact = created.strftime("%Y%m%dT%H%M%SZ")
    suffix = uuid.uuid4().hex[:8]
    family = str(sample_family).replace("-", "_")
    return f"{family}_{compact}_{suffix}"


def new_study_id(*, sample_family: str = "study", created_at_utc: datetime | None = None) -> str:
    return build_study_id(sample_family=sample_family, created_at_utc=created_at_utc)


def new_benchmark_id(*, created_at_utc: datetime | None = None) -> str:
    return build_study_id(sample_family="benchmark", created_at_utc=created_at_utc).removeprefix("benchmark_")


@dataclass(frozen=True)
class StudyBenchmarkResult:
    benchmark_id: str
    benchmarked_at_utc: str
    panel_path: str
    panel_hash: str
    panel_version_id: str | None
    panel_row_count: int
    viewer_modes: tuple[str, ...]
    posts_per_feed: int
    concurrency: int
    rps: float
    sample_size: int
    measured_request_count: int
    measured_success_count: int
    measured_failure_count: int
    measured_elapsed_s: float
    throughput_rps: float
    safety_margin: float
    window_minutes: int
    safe_window_budget_s: float
    estimated_full_sweep_requests: int
    estimated_full_sweep_duration_s: float
    safe_max_panel_size: int
    required_shard_count: int
    full_panel_feasible: bool
    dual_viewer_feasible: bool
    full_depth_feasible: bool

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["viewer_modes"] = list(self.viewer_modes)
        return payload


@dataclass(frozen=True)
class FrozenStudyManifest:
    study_id: str
    study_group_id: str | None
    created_at_utc: str
    window_anchor_start_utc: str
    intended_window_minutes: int
    sample_family: StudySampleFamily
    panel_role: str
    sample_design: dict[str, Any]
    panel_path: str
    panel_hash: str
    panel_version_id: str | None
    panel_row_count: int
    panel_membership_hash: str
    source_panel_path: str
    source_panel_hash: str
    source_panel_version_id: str | None
    selection_strategy: str
    viewer_modes: tuple[str, ...]
    accept_language: str | None
    accept_labelers: str | None
    include_author_labels: bool
    auth_vantage_ids: dict[str, str]
    posts_per_feed: int
    rps: float
    concurrency: int
    feed_time_budget_s: float
    max_attempts: int
    benchmark_id: str
    benchmark_path: str
    benchmark_result: dict[str, Any]
    shard_count: int | None = None
    shard_seed: int | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["viewer_modes"] = list(self.viewer_modes)
        return payload


@dataclass(frozen=True)
class StudyWindow:
    scheduled_window_start_utc: datetime
    scheduled_window_end_utc: datetime
    window_minutes: int
    window_index: int
    window_minute: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "scheduled_window_start_utc": format_utc(self.scheduled_window_start_utc),
            "scheduled_window_end_utc": format_utc(self.scheduled_window_end_utc),
            "window_minutes": int(self.window_minutes),
            "window_index": int(self.window_index),
            "window_minute": int(self.window_minute),
        }


def write_study_manifest(path: Path, manifest: FrozenStudyManifest) -> None:
    atomic_write_json(path, manifest.to_dict())


def load_study_manifest(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"invalid study manifest: {path}")
    return raw


def resolve_study_panel_path(*, layout: Layout, study_id: str, study_manifest: dict[str, Any]) -> Path:
    raw_path = str(study_manifest.get("panel_path") or "").strip()
    candidates: list[Path] = []
    if raw_path:
        declared_path = Path(raw_path)
        candidates.append(declared_path)

        normalized = raw_path.replace("\\", "/")
        remapped_roots: tuple[tuple[str, Path], ...] = (
            ("/data_v2_full/studies/", layout.out_base / "studies"),
            ("/studies/", layout.studies_root),
        )
        for marker, local_root in remapped_roots:
            if marker not in normalized:
                continue
            suffix = normalized.split(marker, 1)[1].strip("/")
            if not suffix:
                continue
            candidates.append(local_root / Path(*PurePosixPath(suffix).parts))

    candidates.append(layout.study_panel_csv(study_id))

    seen: set[str] = set()
    deduped_candidates: list[Path] = []
    for candidate in candidates:
        candidate_key = str(candidate)
        if candidate_key in seen:
            continue
        seen.add(candidate_key)
        deduped_candidates.append(candidate)

    for candidate in deduped_candidates:
        if candidate.exists():
            return candidate

    return layout.study_panel_csv(study_id)


def load_benchmark_result(path: Path) -> StudyBenchmarkResult:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"invalid benchmark result: {path}")
    viewer_modes = tuple(str(mode) for mode in raw.get("viewer_modes") or ())
    return StudyBenchmarkResult(
        benchmark_id=str(raw["benchmark_id"]),
        benchmarked_at_utc=str(raw["benchmarked_at_utc"]),
        panel_path=str(raw["panel_path"]),
        panel_hash=str(raw["panel_hash"]),
        panel_version_id=(str(raw["panel_version_id"]) if raw.get("panel_version_id") else None),
        panel_row_count=int(raw["panel_row_count"]),
        viewer_modes=viewer_modes,
        posts_per_feed=int(raw["posts_per_feed"]),
        concurrency=int(raw["concurrency"]),
        rps=float(raw["rps"]),
        sample_size=int(raw["sample_size"]),
        measured_request_count=int(raw["measured_request_count"]),
        measured_success_count=int(raw["measured_success_count"]),
        measured_failure_count=int(raw["measured_failure_count"]),
        measured_elapsed_s=float(raw["measured_elapsed_s"]),
        throughput_rps=float(raw["throughput_rps"]),
        safety_margin=float(raw["safety_margin"]),
        window_minutes=int(raw["window_minutes"]),
        safe_window_budget_s=float(raw["safe_window_budget_s"]),
        estimated_full_sweep_requests=int(raw["estimated_full_sweep_requests"]),
        estimated_full_sweep_duration_s=float(raw["estimated_full_sweep_duration_s"]),
        safe_max_panel_size=int(raw["safe_max_panel_size"]),
        required_shard_count=int(raw.get("required_shard_count") or 1),
        full_panel_feasible=bool(raw["full_panel_feasible"]),
        dual_viewer_feasible=bool(raw["dual_viewer_feasible"]),
        full_depth_feasible=bool(raw["full_depth_feasible"]),
    )


def save_benchmark_result(path: Path, result: StudyBenchmarkResult) -> None:
    atomic_write_json(path, result.to_dict())


def resolve_window_start(
    *,
    now_dt: datetime,
    sleep_until_window: bool,
    explicit_window_start_utc: datetime | None,
    window_minutes: int = 5,
) -> datetime:
    if explicit_window_start_utc is not None:
        return floor_to_window_utc(explicit_window_start_utc, window_minutes=window_minutes)
    if sleep_until_window:
        return ceil_to_window_utc(now_dt, window_minutes=window_minutes)
    return floor_to_window_utc(now_dt, window_minutes=window_minutes)


def compute_study_window(
    *,
    window_origin_utc: datetime,
    scheduled_window_start_utc: datetime,
    window_minutes: int,
) -> StudyWindow:
    window_index = compute_window_index(
        anchor_start_utc=floor_to_window_utc(window_origin_utc, window_minutes=window_minutes),
        scheduled_window_start_utc=floor_to_window_utc(scheduled_window_start_utc, window_minutes=window_minutes),
        window_minutes=window_minutes,
    )
    start = floor_to_window_utc(scheduled_window_start_utc, window_minutes=window_minutes)
    return StudyWindow(
        scheduled_window_start_utc=start,
        scheduled_window_end_utc=window_end_utc(start, window_minutes=window_minutes),
        window_minutes=int(window_minutes),
        window_index=int(window_index),
        window_minute=int(start.minute),
    )


def deterministic_randomization_seed(*, study_id: str, scheduled_window_start_utc: str, shard_id: int | None) -> str:
    return hashlib.sha256(
        f"{study_id}|{scheduled_window_start_utc}|{shard_id if shard_id is not None else 'all'}".encode("utf-8")
    ).hexdigest()[:24]


BenchmarkSummary = StudyBenchmarkResult


def load_benchmark_summary(path: Path) -> StudyBenchmarkResult:
    return load_benchmark_result(path)


def save_benchmark_summary(path: Path, result: StudyBenchmarkResult) -> None:
    save_benchmark_result(path, result)


def request_pages_for_row(row: dict[str, str], posts_per_feed: int) -> int:
    return requests_per_feed(feed_uri=str(row.get("feed_uri") or ""), posts_per_feed=posts_per_feed)


def total_request_units_for_panel(
    *,
    panel_rows: list[dict[str, str]],
    viewer_modes: tuple[str, ...],
    posts_per_feed: int,
    include_auth_bootstrap: bool = True,
) -> int:
    return expected_snapshot_requests_for_panel(
        panel_rows=panel_rows,
        viewer_modes=viewer_modes,
        posts_per_feed=posts_per_feed,
        include_auth_session_setup=include_auth_bootstrap,
    )


def build_benchmark_result(
    *,
    panel_path: Path,
    panel_rows: list[dict[str, str]],
    viewer_modes: tuple[str, ...],
    posts_per_feed: int,
    concurrency: int,
    rps: float,
    sample_size: int,
    measured_request_count: int,
    measured_success_count: int,
    measured_failure_count: int,
    measured_elapsed_s: float,
    safety_margin: float,
    window_minutes: int,
) -> StudyBenchmarkResult:
    panel_hash = file_sha256(panel_path)
    panel_version_id = panel_version_id_from_rows(panel_rows)
    throughput_rps = float(measured_success_count) / max(float(measured_elapsed_s), 0.001)
    estimated_full_sweep_requests = total_request_units_for_panel(
        panel_rows=panel_rows,
        viewer_modes=viewer_modes,
        posts_per_feed=posts_per_feed,
        include_auth_bootstrap=True,
    )
    estimated_full_sweep_duration_s = float(estimated_full_sweep_requests) / max(throughput_rps, 0.001)
    safe_window_budget_s = float(window_minutes) * 60.0 * float(safety_margin)
    per_row_units = float(estimated_full_sweep_requests) / max(len(panel_rows), 1)
    safe_request_budget = max(0.0, (safe_window_budget_s * throughput_rps) - (2.0 if "auth" in viewer_modes else 0.0))
    safe_max_panel_size = int(math.floor(safe_request_budget / max(per_row_units, 0.000001)))
    if panel_rows:
        safe_max_panel_size = max(1, min(len(panel_rows), safe_max_panel_size))
    required_shard_count = max(1, int(math.ceil(estimated_full_sweep_duration_s / max(safe_window_budget_s, 1.0))))
    feasible = bool(estimated_full_sweep_duration_s <= safe_window_budget_s)
    return StudyBenchmarkResult(
        benchmark_id=build_study_id(sample_family="benchmark").removeprefix("benchmark_"),
        benchmarked_at_utc=format_utc(now_utc()),
        panel_path=str(panel_path),
        panel_hash=panel_hash,
        panel_version_id=panel_version_id,
        panel_row_count=len(panel_rows),
        viewer_modes=viewer_modes,
        posts_per_feed=int(posts_per_feed),
        concurrency=int(concurrency),
        rps=float(rps),
        sample_size=int(sample_size),
        measured_request_count=int(measured_request_count),
        measured_success_count=int(measured_success_count),
        measured_failure_count=int(measured_failure_count),
        measured_elapsed_s=float(round(measured_elapsed_s, 6)),
        throughput_rps=float(round(throughput_rps, 6)),
        safety_margin=float(safety_margin),
        window_minutes=int(window_minutes),
        safe_window_budget_s=float(round(safe_window_budget_s, 6)),
        estimated_full_sweep_requests=int(estimated_full_sweep_requests),
        estimated_full_sweep_duration_s=float(round(estimated_full_sweep_duration_s, 6)),
        safe_max_panel_size=int(safe_max_panel_size),
        required_shard_count=int(required_shard_count),
        full_panel_feasible=feasible,
        dual_viewer_feasible=feasible,
        full_depth_feasible=feasible,
    )


build_benchmark_summary = build_benchmark_result


@dataclass(frozen=True)
class StudyPlan:
    panel_rows: list[dict[str, str]]
    panel_role: str
    selection_strategy: str
    sample_design: dict[str, Any]
    shard_count: int | None
    shard_seed: int | None
    estimated_request_units: int
    estimated_duration_s: float


def plan_frozen_study(
    *,
    source_rows: list[dict[str, str]],
    sample_family: StudySampleFamily,
    benchmark_result: StudyBenchmarkResult,
    selection_seed: int,
    auto_core_size: bool,
    requested_core_size: int | None,
    auto_shard_count: bool,
    requested_shard_count: int | None,
) -> StudyPlan:
    rows = list(source_rows)
    if sample_family == "micro5_core_full":
        target_size = int(requested_core_size or len(rows))
        if target_size > len(rows):
            target_size = len(rows)
        if target_size > benchmark_result.safe_max_panel_size:
            if not auto_core_size:
                raise ValueError(
                    "requested core panel does not fit the benchmarked 5-minute budget; "
                    f"requested={target_size} safe_max_panel_size={benchmark_result.safe_max_panel_size}"
                )
            target_size = benchmark_result.safe_max_panel_size
        planned_rows = list(rows[:target_size])
        estimated_units = total_request_units_for_panel(
            panel_rows=planned_rows,
            viewer_modes=benchmark_result.viewer_modes,
            posts_per_feed=benchmark_result.posts_per_feed,
            include_auth_bootstrap=True,
        )
        estimated_duration_s = float(estimated_units) / max(benchmark_result.throughput_rps, 0.001)
        if estimated_duration_s > benchmark_result.safe_window_budget_s:
            raise ValueError(
                "core panel still exceeds the benchmarked 5-minute budget after sizing; "
                f"estimated_duration_s={estimated_duration_s:.3f} safe_window_budget_s={benchmark_result.safe_window_budget_s:.3f}"
            )
        sample_design = {
            "method": "frozen_prefix_sample" if target_size < len(rows) else "frozen_full_panel",
            "source_panel_size": len(rows),
            "target_panel_size": len(planned_rows),
            "selection_seed": selection_seed,
        }
        return StudyPlan(
            panel_rows=planned_rows,
            panel_role="core",
            selection_strategy=str(sample_design["method"]),
            sample_design=sample_design,
            shard_count=None,
            shard_seed=None,
            estimated_request_units=estimated_units,
            estimated_duration_s=estimated_duration_s,
        )

    shard_count = int(requested_shard_count or 0)
    if shard_count <= 0:
        if not auto_shard_count:
            raise ValueError("extended sharded studies require --shard-count or --auto-shard-count")
        shard_count = int(max(1, benchmark_result.required_shard_count))
    estimated_units = total_request_units_for_panel(
        panel_rows=rows,
        viewer_modes=benchmark_result.viewer_modes,
        posts_per_feed=benchmark_result.posts_per_feed,
        include_auth_bootstrap=True,
    )
    estimated_duration_s = float(estimated_units) / max(benchmark_result.throughput_rps, 0.001)
    per_shard_s = estimated_duration_s / float(shard_count)
    if per_shard_s > benchmark_result.safe_window_budget_s:
        raise ValueError(
            "requested shard_count does not fit the benchmarked 5-minute budget; "
            f"per_shard_duration_s={per_shard_s:.3f} safe_window_budget_s={benchmark_result.safe_window_budget_s:.3f} "
            f"required_shard_count={benchmark_result.required_shard_count}"
        )
    sample_design = {
        "method": "frozen_full_panel_with_rotation",
        "source_panel_size": len(rows),
        "target_panel_size": len(rows),
        "selection_seed": selection_seed,
    }
    return StudyPlan(
        panel_rows=rows,
        panel_role="extended",
        selection_strategy=str(sample_design["method"]),
        sample_design=sample_design,
        shard_count=shard_count,
        shard_seed=selection_seed,
        estimated_request_units=estimated_units,
        estimated_duration_s=estimated_duration_s,
    )


def plan_study(
    *,
    panel_rows: list[dict[str, str]],
    benchmark: StudyBenchmarkResult,
    sample_family: StudySampleFamily,
    auto_core_size: bool,
    auto_shard_count: bool,
    requested_core_size: int | None = None,
    requested_shard_count: int | None = None,
    selection_seed: str | int = 0,
) -> StudyPlan:
    seed_int = int(selection_seed) if isinstance(selection_seed, int) else deterministic_seed(selection_seed)
    return plan_frozen_study(
        source_rows=panel_rows,
        sample_family=sample_family,
        benchmark_result=benchmark,
        selection_seed=seed_int,
        auto_core_size=auto_core_size,
        requested_core_size=requested_core_size,
        auto_shard_count=auto_shard_count,
        requested_shard_count=requested_shard_count,
    )


@dataclass(frozen=True)
class StudyWindow:
    scheduled_window_start_utc: datetime
    scheduled_window_end_utc: datetime
    window_minutes: int
    window_index: int
    window_minute: int


def compute_study_window(
    *,
    window_origin_utc: datetime,
    scheduled_window_start_utc: datetime,
    window_minutes: int = MICRO5_WINDOW_MINUTES,
) -> StudyWindow:
    anchor = floor_to_window_utc(window_origin_utc, window_minutes=window_minutes)
    start = floor_to_window_utc(scheduled_window_start_utc, window_minutes=window_minutes)
    index = compute_window_index(
        anchor_start_utc=anchor,
        scheduled_window_start_utc=start,
        window_minutes=window_minutes,
    )
    return StudyWindow(
        scheduled_window_start_utc=start,
        scheduled_window_end_utc=window_end_utc(start, window_minutes=window_minutes),
        window_minutes=window_minutes,
        window_index=index,
        window_minute=int(start.minute),
    )


def resolve_window_start(
    *,
    now_dt: datetime,
    sleep_until_window: bool,
    explicit_window_start_utc: datetime | None,
    window_minutes: int = MICRO5_WINDOW_MINUTES,
) -> datetime:
    if explicit_window_start_utc is not None:
        return floor_to_window_utc(explicit_window_start_utc, window_minutes=window_minutes)
    if sleep_until_window:
        return ceil_to_window_utc(now_dt, window_minutes=window_minutes)
    return floor_to_window_utc(now_dt, window_minutes=window_minutes)


def deterministic_randomization_seed(*, study_id: str, scheduled_window_start_utc: str, shard_id: int | None) -> str:
    return hashlib.sha256(
        f"{study_id}||{scheduled_window_start_utc}||{shard_id if shard_id is not None else 'all'}".encode("utf-8")
    ).hexdigest()[:24]


@dataclass(frozen=True)
class StudyPanelRow:
    feed_uri: str
    bucket: str
    unauth_skip: int
    built_at_utc: str | None
    panel_version_id: str | None


def read_study_panel_rows(panel_path: Path) -> list[StudyPanelRow]:
    out: list[StudyPanelRow] = []
    for row in read_panel_rows(panel_path):
        out.append(
            StudyPanelRow(
                feed_uri=str(row.get("feed_uri") or "").strip(),
                bucket=str(row.get("bucket") or "").strip() or "unknown",
                unauth_skip=int(str(row.get("unauth_skip") or "0") or 0),
                built_at_utc=str(row.get("built_at_utc") or "").strip() or None,
                panel_version_id=str(row.get("panel_version_id") or "").strip() or None,
            )
        )
    return out


panel_file_hash = file_sha256


def shard_rows(
    panel_rows: list[StudyPanelRow],
    *,
    shard_count: int,
    shard_seed: str | int,
) -> dict[int, list[StudyPanelRow]]:
    shards: dict[int, list[StudyPanelRow]] = {idx: [] for idx in range(int(shard_count))}
    seed_int = int(shard_seed) if isinstance(shard_seed, int) else deterministic_seed(shard_seed)
    for row in panel_rows:
        shard_id = deterministic_seed(seed_int, row.feed_uri) % int(shard_count)
        shards[shard_id].append(row)
    return shards


def shard_membership_hash(rows: list[StudyPanelRow]) -> str:
    payload = "\n".join(sorted(str(row.feed_uri) for row in rows if str(row.feed_uri)))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def create_frozen_study_manifest(
    *,
    study_id: str,
    study_group_id: str | None,
    panel_path: Path,
    source_panel_path: Path,
    benchmark_path: Path,
    benchmark_result: StudyBenchmarkResult,
    panel_rows: list[dict[str, str]],
    sample_family: StudySampleFamily,
    panel_role: str,
    sample_design: dict[str, Any],
    selection_strategy: str,
    viewer_modes: tuple[str, ...],
    accept_language: str | None,
    accept_labelers: str | None,
    include_author_labels: bool,
    auth_vantage_ids: dict[str, str],
    posts_per_feed: int,
    rps: float,
    concurrency: int,
    feed_time_budget_s: float,
    max_attempts: int,
    window_anchor_start_utc: datetime,
    intended_window_minutes: int,
    shard_count: int | None,
    shard_seed: int | None,
) -> FrozenStudyManifest:
    panel_hash = file_sha256(panel_path)
    source_panel_hash = file_sha256(source_panel_path)
    return FrozenStudyManifest(
        study_id=study_id,
        study_group_id=study_group_id,
        created_at_utc=format_utc(now_utc()),
        window_anchor_start_utc=format_utc(window_anchor_start_utc),
        intended_window_minutes=int(intended_window_minutes),
        sample_family=sample_family,
        panel_role=panel_role,
        sample_design=sample_design,
        panel_path=str(panel_path),
        panel_hash=panel_hash,
        panel_version_id=panel_version_id_from_rows(panel_rows),
        panel_row_count=len(panel_rows),
        panel_membership_hash=panel_membership_hash(panel_rows),
        source_panel_path=str(source_panel_path),
        source_panel_hash=source_panel_hash,
        source_panel_version_id=panel_version_id_from_rows(read_panel_rows(source_panel_path)),
        selection_strategy=selection_strategy,
        viewer_modes=viewer_modes,
        accept_language=accept_language,
        accept_labelers=accept_labelers,
        include_author_labels=bool(include_author_labels),
        auth_vantage_ids=dict(auth_vantage_ids),
        posts_per_feed=int(posts_per_feed),
        rps=float(rps),
        concurrency=int(concurrency),
        feed_time_budget_s=float(feed_time_budget_s),
        max_attempts=int(max_attempts),
        benchmark_id=benchmark_result.benchmark_id,
        benchmark_path=str(benchmark_path),
        benchmark_result=benchmark_result.to_dict(),
        shard_count=shard_count,
        shard_seed=shard_seed,
    )


def create_frozen_study(
    *,
    study_root: Path,
    study_id: str,
    source_panel_path: Path,
    panel_rows: list[dict[str, str]],
    benchmark: StudyBenchmarkResult,
    sample_family: StudySampleFamily,
    sample_design: dict[str, Any],
    viewer_modes: tuple[str, ...],
    accept_language: str | None,
    accept_labelers: str | None,
    include_author_labels: bool,
    vantage_id_unauth: str,
    vantage_id_auth: str,
    posts_per_feed: int,
    window_origin_utc: datetime,
    intended_window_minutes: int,
    shard_count: int | None,
    shard_seed: str | None,
) -> dict[str, Any]:
    if study_root.exists():
        raise FileExistsError(f"study already exists: {study_root}")
    ensure_dir(study_root)
    panel_path = study_root / "panel" / "frozen_panel.csv"
    write_panel_rows(panel_path, panel_rows)
    benchmark_path = study_root / "benchmark_result.json"
    save_benchmark_result(benchmark_path, benchmark)
    manifest = create_frozen_study_manifest(
        study_id=study_id,
        study_group_id=None,
        panel_path=panel_path,
        source_panel_path=source_panel_path,
        benchmark_path=benchmark_path,
        benchmark_result=benchmark,
        panel_rows=panel_rows,
        sample_family=sample_family,
        panel_role=("core" if sample_family == "micro5_core_full" else "extended"),
        sample_design=sample_design,
        selection_strategy=str(sample_design.get("method") or sample_design.get("selection_strategy") or "frozen_panel"),
        viewer_modes=viewer_modes,
        accept_language=accept_language,
        accept_labelers=accept_labelers,
        include_author_labels=include_author_labels,
        auth_vantage_ids={
            "unauth": str(vantage_id_unauth).strip() or "unauth",
            "auth": str(vantage_id_auth).strip() or "auth",
        },
        posts_per_feed=posts_per_feed,
        rps=benchmark.rps,
        concurrency=benchmark.concurrency,
        feed_time_budget_s=float(sample_design.get("feed_time_budget_s") or 20.0),
        max_attempts=int(sample_design.get("max_attempts") or 3),
        window_anchor_start_utc=window_origin_utc,
        intended_window_minutes=intended_window_minutes,
        shard_count=shard_count,
        shard_seed=(int(shard_seed) if isinstance(shard_seed, str) and shard_seed.isdigit() else shard_seed),
    )
    write_study_manifest(study_root / "study_manifest.json", manifest)
    return manifest.to_dict()
