from __future__ import annotations

import csv
import hashlib
import os
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from bsky_collector_v2 import __version__
from bsky_collector_v2.fs_utils import ensure_dir
from bsky_collector_v2.instrumentation import REQUEST_PROVENANCE_VERSION, SCHEMA_VERSION
from bsky_collector_v2.writers import CsvPartWriter

REQUEST_PROVENANCE_FIELDS: tuple[str, ...] = (
    "run_id",
    "job_name",
    "sample_family",
    "collection_params_hash",
    "study_id",
    "panel_hash",
    "panel_version_id",
    "collector_version",
    "schema_version",
    "request_provenance_version",
    "provenance_source",
    "provenance_level",
    "partial_reason",
    "captured_at_utc",
    "request_started_at_utc",
    "request_finished_at_utc",
    "request_order_in_run",
    "request_order_in_window",
    "request_order_in_sweep",
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
    "date_utc",
    "viewer_mode",
    "vantage_id",
    "host_kind",
    "host",
    "endpoint",
    "feed_uri",
    "page_no",
    "cursor_in",
    "cursor_out",
    "depth_requested",
    "depth_returned",
    "http_status",
    "retry_count",
    "response_hash",
    "error_class",
)

_DEPTH_KEYS: tuple[str, ...] = (
    "feed",
    "feeds",
    "profiles",
    "actors",
    "starterPacks",
    "suggestions",
    "records",
    "repos",
)


@dataclass(frozen=True)
class RequestContext:
    run_id: str
    job_name: str
    sample_family: str
    collection_params_hash: str
    host_kind: str
    host: str
    endpoint: str
    study_id: str | None = None
    panel_hash: str | None = None
    panel_version_id: str | None = None
    provenance_source: str = "live_request"
    provenance_level: str = "complete"
    partial_reason: str | None = None
    captured_at_utc: str | None = None
    snapshot_hour_utc: str | None = None
    scheduled_window_start_utc: str | None = None
    scheduled_window_end_utc: str | None = None
    window_index: int | None = None
    window_minute: int | None = None
    window_minutes: int | None = None
    randomization_seed: str | None = None
    shard_id: str | int | None = None
    shard_count: int | None = None
    shard_membership_hash: str | None = None
    date_utc: str | None = None
    viewer_mode: str | None = None
    vantage_id: str | None = None
    feed_uri: str | None = None
    page_no: int | None = None
    cursor_in: str | None = None
    depth_requested: int | None = None
    request_order_in_run: int | None = None
    request_order_in_window: int | None = None
    request_order_in_sweep: int | None = None


@dataclass
class RequestOrderTracker:
    _value: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def next(self) -> int:
        with self._lock:
            self._value += 1
            return self._value

    @property
    def current(self) -> int:
        with self._lock:
            return self._value


def classify_host_kind(
    *,
    host: str,
    appview_host: str,
    pds_host: str,
    relay_host: str | None = None,
    access_jwt: str | None = None,
) -> str:
    normalized = str(host).rstrip("/")
    if relay_host and normalized == str(relay_host).rstrip("/"):
        return "relay"
    if normalized == str(appview_host).rstrip("/"):
        return "public_appview"
    if normalized == str(pds_host).rstrip("/"):
        if access_jwt:
            return "authenticated_pds_proxy"
        return "pds_proxy"
    return "unknown_host"


def response_hash_from_bytes(content: bytes | None) -> str | None:
    if not content:
        return None
    return hashlib.sha256(content).hexdigest()[:16]


def infer_depth_returned(data: dict[str, Any]) -> int | None:
    for key in _DEPTH_KEYS:
        value = data.get(key)
        if isinstance(value, list):
            return int(len(value))
    return None


def infer_depth_requested(*, params: Mapping[str, Any] | None, json_body: Mapping[str, Any] | None) -> int | None:
    if params is not None:
        limit = params.get("limit")
        if isinstance(limit, int):
            return int(limit)
        if isinstance(limit, str):
            try:
                return int(limit)
            except ValueError:
                pass
        for key in ("actors", "uris"):
            value = params.get(key)
            if isinstance(value, list):
                return int(len(value))
    if json_body is not None:
        for key in ("actors", "uris"):
            value = json_body.get(key)
            if isinstance(value, list):
                return int(len(value))
    return None


@dataclass
class JobRequestContextFactory:
    run_id: str
    job_name: str
    sample_family: str
    collection_params_hash: str
    appview_host: str
    pds_host: str
    study_id: str | None = None
    panel_hash: str | None = None
    panel_version_id: str | None = None
    relay_host: str | None = None
    date_utc: str | None = None
    snapshot_hour_utc: str | None = None
    scheduled_window_start_utc: str | None = None
    scheduled_window_end_utc: str | None = None
    window_index: int | None = None
    window_minute: int | None = None
    window_minutes: int | None = None
    randomization_seed: str | None = None
    shard_id: str | int | None = None
    shard_count: int | None = None
    shard_membership_hash: str | None = None
    viewer_mode: str | None = None
    vantage_id: str | None = None
    _order_tracker: RequestOrderTracker = field(default_factory=RequestOrderTracker)
    _page_counters: dict[tuple[str, str], int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __call__(
        self,
        *,
        endpoint: str,
        host: str,
        method: str,
        params: Mapping[str, Any] | None,
        json_body: Mapping[str, Any] | None,
        access_jwt: str | None,
        feed_uri: str | None,
        timestamp_utc: str,
    ) -> RequestContext:
        order = self._order_tracker.next()
        cursor_in: str | None = None
        if params is not None:
            raw_cursor = params.get("cursor")
            if isinstance(raw_cursor, str) and raw_cursor:
                cursor_in = raw_cursor
        page_key = (endpoint, feed_uri or "")
        with self._lock:
            page_no = int(self._page_counters.get(page_key, 0))
            self._page_counters[page_key] = page_no + 1
        return RequestContext(
            run_id=self.run_id,
            job_name=self.job_name,
            sample_family=self.sample_family,
            collection_params_hash=self.collection_params_hash,
            study_id=self.study_id,
            panel_hash=self.panel_hash,
            panel_version_id=self.panel_version_id,
            captured_at_utc=timestamp_utc,
            snapshot_hour_utc=self.snapshot_hour_utc,
            scheduled_window_start_utc=self.scheduled_window_start_utc,
            scheduled_window_end_utc=self.scheduled_window_end_utc,
            window_index=self.window_index,
            window_minute=self.window_minute,
            window_minutes=self.window_minutes,
            randomization_seed=self.randomization_seed,
            shard_id=self.shard_id,
            shard_count=self.shard_count,
            shard_membership_hash=self.shard_membership_hash,
            date_utc=self.date_utc,
            viewer_mode=self.viewer_mode,
            vantage_id=self.vantage_id,
            host_kind=classify_host_kind(
                host=host,
                appview_host=self.appview_host,
                pds_host=self.pds_host,
                relay_host=self.relay_host,
                access_jwt=access_jwt,
            ),
            host=host,
            endpoint=endpoint,
            feed_uri=feed_uri,
            page_no=page_no,
            cursor_in=cursor_in,
            depth_requested=infer_depth_requested(params=params, json_body=json_body),
            request_order_in_run=order,
            request_order_in_window=order,
            request_order_in_sweep=order,
        )


@dataclass
class RequestProvenanceWriter:
    path: Path
    flush_interval_s: float = 2.0
    fsync_interval_s: float = 10.0

    def __post_init__(self) -> None:
        self._writer = CsvPartWriter(
            self.path,
            fieldnames=REQUEST_PROVENANCE_FIELDS,
            flush_interval_s=self.flush_interval_s,
            fsync_interval_s=self.fsync_interval_s,
        )

    def record(
        self,
        *,
        context: RequestContext,
        request_started_at_utc: str,
        request_finished_at_utc: str | None,
        cursor_out: str | None,
        depth_returned: int | None,
        http_status: int | None,
        retry_count: int,
        response_hash: str | None,
        error_class: str | None,
    ) -> None:
        self._writer.write_rows(
            [
                {
                    "run_id": context.run_id,
                    "job_name": context.job_name,
                    "sample_family": context.sample_family,
                    "collection_params_hash": context.collection_params_hash,
                    "study_id": context.study_id,
                    "panel_hash": context.panel_hash,
                    "panel_version_id": context.panel_version_id,
                    "collector_version": __version__,
                    "schema_version": SCHEMA_VERSION,
                    "request_provenance_version": REQUEST_PROVENANCE_VERSION,
                    "provenance_source": context.provenance_source,
                    "provenance_level": context.provenance_level,
                    "partial_reason": context.partial_reason,
                    "captured_at_utc": context.captured_at_utc,
                    "request_started_at_utc": request_started_at_utc,
                    "request_finished_at_utc": request_finished_at_utc,
                    "request_order_in_run": context.request_order_in_run,
                    "request_order_in_window": context.request_order_in_window,
                    "request_order_in_sweep": context.request_order_in_sweep,
                    "snapshot_hour_utc": context.snapshot_hour_utc,
                    "scheduled_window_start_utc": context.scheduled_window_start_utc,
                    "scheduled_window_end_utc": context.scheduled_window_end_utc,
                    "window_index": context.window_index,
                    "window_minute": context.window_minute,
                    "window_minutes": context.window_minutes,
                    "randomization_seed": context.randomization_seed,
                    "shard_id": context.shard_id,
                    "shard_count": context.shard_count,
                    "shard_membership_hash": context.shard_membership_hash,
                    "date_utc": context.date_utc,
                    "viewer_mode": context.viewer_mode,
                    "vantage_id": context.vantage_id,
                    "host_kind": context.host_kind,
                    "host": context.host,
                    "endpoint": context.endpoint,
                    "feed_uri": context.feed_uri,
                    "page_no": context.page_no,
                    "cursor_in": context.cursor_in,
                    "cursor_out": cursor_out,
                    "depth_requested": context.depth_requested,
                    "depth_returned": depth_returned,
                    "http_status": http_status,
                    "retry_count": retry_count,
                    "response_hash": response_hash,
                    "error_class": error_class,
                }
            ]
        )

    def close(self) -> None:
        self._writer.close()


def rewrite_request_provenance_csv(*, path: Path, rows: list[dict[str, Any]]) -> None:
    ensure_dir(path.parent)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="",
        prefix=path.name + ".tmp.",
        dir=str(path.parent),
        delete=False,
    ) as tmp:
        tmp_path = Path(tmp.name)
        writer = csv.DictWriter(tmp, fieldnames=list(REQUEST_PROVENANCE_FIELDS))
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name) for name in REQUEST_PROVENANCE_FIELDS})
        tmp.flush()
        os.fsync(tmp.fileno())
    os.replace(tmp_path, path)


def max_request_order(path: Path, *, field_name: str = "request_order_in_sweep") -> int:
    if not path.exists():
        return 0
    max_order = 0
    with open(path, "r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            try:
                max_order = max(max_order, int(row.get(field_name) or 0))
            except (TypeError, ValueError):
                continue
    return max_order
