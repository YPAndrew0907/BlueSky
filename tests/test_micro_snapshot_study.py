from __future__ import annotations

import csv
import json
import os
import random
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from fake_bsky_server import FakeBskyConfig, FakeBskyServer

from bsky_collector_v2.layout import Layout
from bsky_collector_v2.quality import assess_micro5_window
from bsky_collector_v2.state import SnapshotStatusDB
from bsky_collector_v2.study import (
    build_benchmark_result,
    compute_study_window,
    create_frozen_study,
    parse_utc_datetime,
    read_panel_rows,
    resolve_study_panel_path,
)
from bsky_collector_v2.time_utils import MicroWindow
from bsky_collector_v2.types import FeedUri


def _run(cmd: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )




def _wait_for_success_row(status_path: Path, *, timeout_s: float = 20.0) -> None:
    deadline = time.time() + timeout_s
    last_err: Exception | None = None
    while time.time() < deadline:
        if status_path.exists():
            try:
                conn = sqlite3.connect(str(status_path))
                try:
                    row = conn.execute(
                        "SELECT 1 FROM feed_tasks WHERE status='success' LIMIT 1"
                    ).fetchone()
                finally:
                    conn.close()
                if row is not None:
                    return
            except Exception as err:  # noqa: BLE001
                last_err = err
        time.sleep(0.05)
    if last_err is not None:
        raise last_err
    raise AssertionError(f"timed out waiting for successful task in {status_path}")


def _write_panel_csv(path: Path, feed_uris: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "feed_uri,bucket,unauth_skip,built_at_utc,panel_version_id\n"
        + "\n".join(
            f"{feed_uri},popular_by_likecount,0,2026-03-17T00:00:00Z,2026-03-17"
            for feed_uri in feed_uris
        )
        + "\n",
        encoding="utf-8",
    )


def _create_feasible_study(
    out_base: Path,
    *,
    study_id: str,
    feed_uris: list[str],
    sample_family: str = "micro5_core_full",
    viewer_modes: tuple[str, ...] = ("unauth",),
    posts_per_feed: int = 5,
    shard_count: int | None = None,
) -> dict[str, object]:
    layout = Layout(out_base=out_base)
    _write_panel_csv(layout.panel_active_csv, feed_uris)
    panel_rows = read_panel_rows(layout.panel_active_csv)
    benchmark = build_benchmark_result(
        panel_path=layout.panel_active_csv,
        panel_rows=panel_rows,
        viewer_modes=viewer_modes,
        posts_per_feed=posts_per_feed,
        concurrency=4,
        rps=1000.0,
        sample_size=min(10, len(panel_rows)),
        measured_request_count=max(1, len(panel_rows) * max(1, len(viewer_modes))),
        measured_success_count=max(1, len(panel_rows) * max(1, len(viewer_modes))),
        measured_failure_count=0,
        measured_elapsed_s=1.0,
        safety_margin=0.9,
        window_minutes=5,
    )
    return create_frozen_study(
        study_root=layout.study_dir(study_id),
        study_id=study_id,
        source_panel_path=layout.panel_active_csv,
        panel_rows=panel_rows,
        benchmark=benchmark,
        sample_family=sample_family,  # type: ignore[arg-type]
        sample_design={"method": "test_fixture", "feed_time_budget_s": 20.0, "max_attempts": 3},
        viewer_modes=viewer_modes,
        accept_language="en-US",
        accept_labelers=None,
        include_author_labels=False,
        vantage_id_unauth="unauth_test",
        vantage_id_auth="auth_test",
        posts_per_feed=posts_per_feed,
        window_origin_utc=parse_utc_datetime("2026-03-17T00:00:00Z"),
        intended_window_minutes=5,
        shard_count=shard_count,
        shard_seed=("12345" if shard_count is not None else None),
    )


@pytest.mark.skipif(sys.platform == "win32", reason="Uses SIGKILL")
def test_micro_incremental_writes_survive_kill_restart(tmp_path: Path) -> None:
    out_base = tmp_path / "out"
    out_base.mkdir()
    study_id = "study_micro_resume"
    feed_uris = [f"at://did:plc:feed{i:03d}/app.bsky.feed.generator/main" for i in range(20)]
    manifest = _create_feasible_study(out_base, study_id=study_id, feed_uris=feed_uris)
    sample_family = str(manifest["sample_family"])
    window_start = "2026-03-17T00:00:00Z"
    micro_window = MicroWindow(start_utc=parse_utc_datetime(window_start), window_minutes=5)
    layout = Layout(out_base=out_base)

    with FakeBskyServer(feeds={uri: 10 for uri in feed_uris}, cfg=FakeBskyConfig(request_delay_s=0.2)) as srv:
        cmd = [
            sys.executable,
            "-m",
            "bsky_collector_v2",
            "micro-snapshot-study",
            "--out-base",
            str(out_base),
            "--study-id",
            study_id,
            "--scheduled-window-start-utc",
            window_start,
            "--viewer-modes",
            "unauth",
            "--appview-host",
            srv.base_url,
            "--pds-host",
            srv.base_url,
        ]
        proc = subprocess.Popen(cmd, cwd=str(Path.cwd()), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        status_path = layout.micro5_status_sqlite(study_id=study_id, sample_family=sample_family, window=micro_window)
        try:
            _wait_for_success_row(status_path)
            os.kill(proc.pid, signal.SIGKILL)
        finally:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

        assert status_path.exists()
        conn = sqlite3.connect(str(status_path))
        try:
            row = conn.execute(
                "SELECT feed_uri, attempts FROM feed_tasks WHERE status='success' ORDER BY feed_uri LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        success_feed_uri, success_attempts = row[0], int(row[1])

        parts_dir = layout.micro5_parts_dir(study_id=study_id, sample_family=sample_family, window=micro_window)
        before_size = sum(path.stat().st_size for path in parts_dir.glob("feed_items_part_*.csv"))

        resumed = _run(cmd + ["--resume"], cwd=Path.cwd())
        assert resumed.returncode == 0, resumed.stdout

    after_size = sum(path.stat().st_size for path in parts_dir.glob("feed_items_part_*.csv"))
    assert after_size >= before_size

    conn = sqlite3.connect(str(status_path))
    try:
        row2 = conn.execute(
            "SELECT status, attempts FROM feed_tasks WHERE feed_uri=? AND viewer_mode='unauth'",
            (success_feed_uri,),
        ).fetchone()
    finally:
        conn.close()
    assert row2 is not None
    assert row2[0] == "success"
    assert int(row2[1]) == success_attempts


def test_micro_quarantines_on_panel_hash_mismatch(tmp_path: Path) -> None:
    out_base = tmp_path / "out"
    out_base.mkdir()
    study_id = "study_panel_mismatch"
    feed_uris = [f"at://did:plc:feed{i:03d}/app.bsky.feed.generator/main" for i in range(6)]
    manifest = _create_feasible_study(out_base, study_id=study_id, feed_uris=feed_uris)
    sample_family = str(manifest["sample_family"])
    layout = Layout(out_base=out_base)
    frozen_panel_path = Path(str(manifest["panel_path"]))
    frozen_panel_path.write_text(
        frozen_panel_path.read_text(encoding="utf-8") + "at://did:plc:mutated/app.bsky.feed.generator/main,extra,0,2026-03-17T00:00:00Z,2026-03-17\n",
        encoding="utf-8",
    )

    window_start = "2026-03-17T00:00:00Z"
    run = _run(
        [
            sys.executable,
            "-m",
            "bsky_collector_v2",
            "micro-snapshot-study",
            "--out-base",
            str(out_base),
            "--study-id",
            study_id,
            "--scheduled-window-start-utc",
            window_start,
            "--viewer-modes",
            "unauth",
        ],
        cwd=Path.cwd(),
    )
    assert run.returncode != 0

    micro_window = MicroWindow(start_utc=parse_utc_datetime(window_start), window_minutes=5)
    quality = json.loads(
        layout.micro5_quality_report_json(study_id=study_id, sample_family=sample_family, window=micro_window).read_text(
            encoding="utf-8"
        )
    )
    codes = {issue["code"] for issue in quality["issues"]}
    assert quality["verdict"] == "quarantined"
    assert "panel_hash_mismatch" in codes


def test_micro_resolves_local_study_panel_when_manifest_path_is_from_other_machine(tmp_path: Path) -> None:
    out_base = tmp_path / "out"
    out_base.mkdir()
    study_id = "study_cross_platform_panel"
    feed_uris = [f"at://did:plc:feed{i:03d}/app.bsky.feed.generator/main" for i in range(4)]
    manifest = _create_feasible_study(out_base, study_id=study_id, feed_uris=feed_uris)
    layout = Layout(out_base=out_base)
    local_panel = layout.study_panel_csv(study_id)

    stale_manifest = dict(manifest)
    stale_manifest["panel_path"] = f"/Volumes/T9/BlueSky/data_v2_full/studies/{study_id}/panel/frozen_panel.csv"

    resolved = resolve_study_panel_path(layout=layout, study_id=study_id, study_manifest=stale_manifest)

    assert resolved == local_panel
    assert resolved.exists()


def test_micro_request_order_randomized_but_reproducible_from_seed(tmp_path: Path) -> None:
    out_base = tmp_path / "out"
    out_base.mkdir()
    study_id = "study_request_order"
    feed_uris = [f"at://did:plc:feed{i:03d}/app.bsky.feed.generator/main" for i in range(6)]
    manifest = _create_feasible_study(out_base, study_id=study_id, feed_uris=feed_uris)
    sample_family = str(manifest["sample_family"])
    layout = Layout(out_base=out_base)
    window_start = "2026-03-17T00:00:00Z"
    micro_window = MicroWindow(start_utc=parse_utc_datetime(window_start), window_minutes=5)

    with FakeBskyServer(feeds={uri: 5 for uri in feed_uris}) as srv:
        run = _run(
            [
                sys.executable,
                "-m",
                "bsky_collector_v2",
                "micro-snapshot-study",
                "--out-base",
                str(out_base),
                "--study-id",
                study_id,
                "--scheduled-window-start-utc",
                window_start,
                "--viewer-modes",
                "unauth",
                "--appview-host",
                srv.base_url,
                "--pds-host",
                srv.base_url,
            ],
            cwd=Path.cwd(),
        )
        assert run.returncode == 0, run.stdout

    manifest_path = layout.micro5_manifest_json(study_id=study_id, sample_family=sample_family, window=micro_window)
    run_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    provenance_path = layout.micro5_request_provenance_csv(study_id=study_id, sample_family=sample_family, window=micro_window)
    with provenance_path.open("r", encoding="utf-8", newline="") as fh:
        rows = [
            row
            for row in csv.DictReader(fh)
            if row.get("endpoint") == "app.bsky.feed.getFeed" and row.get("feed_uri")
        ]
    actual_order = [row["feed_uri"] for row in sorted(rows, key=lambda row: int(row["request_order_in_window"]))]
    expected_order = [str(row.get("feed_uri") or "") for row in read_panel_rows(Path(str(manifest["panel_path"])))]
    random.Random(str(run_manifest["randomization_seed"])).shuffle(expected_order)
    assert actual_order == expected_order
    assert actual_order != sorted(actual_order)


def test_micro_writes_effective_csv_exports(tmp_path: Path) -> None:
    out_base = tmp_path / "out"
    out_base.mkdir()
    study_id = "study_effective_exports"
    feed_uris = [f"at://did:plc:feed{i:03d}/app.bsky.feed.generator/main" for i in range(3)]
    manifest = _create_feasible_study(out_base, study_id=study_id, feed_uris=feed_uris)
    sample_family = str(manifest["sample_family"])
    layout = Layout(out_base=out_base)
    window_start = "2026-03-17T00:00:00Z"
    micro_window = MicroWindow(start_utc=parse_utc_datetime(window_start), window_minutes=5)

    with FakeBskyServer(feeds={uri: 3 for uri in feed_uris}) as srv:
        run = _run(
            [
                sys.executable,
                "-m",
                "bsky_collector_v2",
                "micro-snapshot-study",
                "--out-base",
                str(out_base),
                "--study-id",
                study_id,
                "--scheduled-window-start-utc",
                window_start,
                "--viewer-modes",
                "unauth",
                "--appview-host",
                srv.base_url,
                "--pds-host",
                srv.base_url,
            ],
            cwd=Path.cwd(),
        )
        assert run.returncode == 0, run.stdout

    effective_dir = layout.effective_micro5_window_dir(study_id=study_id, sample_family=sample_family, window=micro_window)
    assert (effective_dir / "feed_items.csv").exists()
    assert (effective_dir / "posts_first_seen.csv").exists()
    with (effective_dir / "feed_items.csv").open("r", encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert rows


def test_micro_quality_report_flags_drift_overrun_and_auth_snapshot_gaps(tmp_path: Path) -> None:
    out_base = tmp_path / "out"
    out_base.mkdir()
    layout = Layout(out_base=out_base)
    study_id = "study_quality"
    sample_family = "micro5_extended_sharded"
    feed_uris = [f"at://did:plc:feed{i:03d}/app.bsky.feed.generator/main" for i in range(10)]
    manifest = _create_feasible_study(
        out_base,
        study_id=study_id,
        feed_uris=feed_uris,
        sample_family=sample_family,
        viewer_modes=("auth",),
        shard_count=3,
    )
    window_start_dt = parse_utc_datetime("2026-03-17T00:00:00Z")
    window = compute_study_window(
        window_origin_utc=window_start_dt,
        scheduled_window_start_utc=window_start_dt,
        window_minutes=5,
    )
    micro_window = MicroWindow(start_utc=window_start_dt, window_minutes=5)
    run_dir = layout.micro5_window_dir(study_id=study_id, sample_family=sample_family, window=micro_window)
    run_dir.mkdir(parents=True, exist_ok=True)

    (layout.study_manifest_json(study_id)).write_text(json.dumps(manifest), encoding="utf-8")
    (layout.micro5_manifest_json(study_id=study_id, sample_family=sample_family, window=micro_window)).write_text(
        json.dumps(
            {
                "run_id": "micro-run-1",
                "job_name": "micro-snapshot-study",
                "study_id": study_id,
                "sample_family": sample_family,
                "collection_params_hash": "abc123",
                "panel_hash": manifest["panel_hash"],
                "scheduled_window_start_utc": "2026-03-17T00:00:00Z",
                "scheduled_window_end_utc": "2026-03-17T00:05:00Z",
                "window_minutes": 5,
                "window_index": 0,
                "window_minute": 0,
                "started_at_utc": "2026-03-17T00:02:00Z",
                "finished_at_utc": "2026-03-17T00:07:00Z",
                "success": True,
                "shard_id": "shard-01-of-03",
                "shard_count": 3,
                "shard_membership_hash": "deadbeef",
                "params": {"viewer_modes": ["auth"]},
            }
        ),
        encoding="utf-8",
    )
    (layout.micro5_progress_json(study_id=study_id, sample_family=sample_family, window=micro_window)).write_text(
        json.dumps({"feeds_total": 10, "feeds_done": 8, "feeds_failed": 2}),
        encoding="utf-8",
    )
    with layout.micro5_request_provenance_csv(study_id=study_id, sample_family=sample_family, window=micro_window).open(
        "w", encoding="utf-8", newline=""
    ) as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "run_id",
                "endpoint",
                "feed_uri",
                "viewer_mode",
                "request_started_at_utc",
                "request_order_in_window",
            ],
        )
        writer.writeheader()
        for idx in range(8):
            writer.writerow(
                {
                    "run_id": "micro-run-1",
                    "endpoint": "app.bsky.feed.getFeed",
                    "feed_uri": feed_uris[idx],
                    "viewer_mode": "auth",
                    "request_started_at_utc": "2026-03-17T00:02:00Z",
                    "request_order_in_window": str(idx + 1),
                }
            )

    with SnapshotStatusDB.open(layout.micro5_status_sqlite(study_id=study_id, sample_family=sample_family, window=micro_window)) as status:
        status.ensure_tasks(
            tasks=[(FeedUri(feed_uri), "auth") for feed_uri in feed_uris],
            updated_at_utc="2026-03-17T00:00:00Z",
        )

    report = assess_micro5_window(layout, study_id=study_id, sample_family=sample_family, window=window)
    codes = {issue["code"] for issue in report["issues"]}
    assert report["verdict"] == "quarantined"
    assert "start_drift_exceeds_threshold" in codes
    assert "wall_clock_duration_exceeds_threshold" in codes
    assert "window_overrun_exceeds_threshold" in codes
    assert "failure_ratio_exceeds_threshold" in codes
    assert "request_provenance_completeness_below_threshold" in codes
    assert "auth_snapshot_missing" in codes
