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
from datetime import datetime
from pathlib import Path

import pytest

from bsky_collector_v2.layout import Layout
from bsky_collector_v2.quality import assess_micro5_window
from bsky_collector_v2.study import (
    StudyBenchmarkResult,
    compute_study_window,
    create_frozen_study,
    new_benchmark_id,
    panel_file_hash,
    read_panel_rows,
)
from fake_bsky_server import FakeBskyConfig, FakeBskyServer


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


def _write_panel(path: Path, feed_uris: list[str]) -> None:
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


def _benchmark(panel_path: Path, *, viewer_modes: tuple[str, ...] = ("unauth",), safe_max_panel_size: int = 100) -> StudyBenchmarkResult:
    rows = read_panel_rows(panel_path)
    return StudyBenchmarkResult(
        benchmark_id=new_benchmark_id(),
        benchmarked_at_utc="2026-03-17T00:00:00Z",
        panel_path=str(panel_path),
        panel_hash=panel_file_hash(panel_path),
        panel_version_id="2026-03-17",
        panel_row_count=len(rows),
        viewer_modes=viewer_modes,
        posts_per_feed=5,
        concurrency=4,
        rps=20.0,
        sample_size=min(10, len(rows)),
        measured_request_count=min(10, len(rows)),
        measured_success_count=min(10, len(rows)),
        measured_failure_count=0,
        measured_elapsed_s=1.0,
        throughput_rps=10.0,
        safety_margin=0.9,
        window_minutes=5,
        safe_window_budget_s=270.0,
        estimated_full_sweep_requests=len(rows),
        estimated_full_sweep_duration_s=(len(rows) / 10.0),
        safe_max_panel_size=safe_max_panel_size,
        required_shard_count=max(1, (len(rows) + max(1, safe_max_panel_size) - 1) // max(1, safe_max_panel_size)),
        full_panel_feasible=len(rows) <= safe_max_panel_size,
        dual_viewer_feasible=len(rows) <= safe_max_panel_size,
        full_depth_feasible=len(rows) <= safe_max_panel_size,
    )


def _create_study(out_base: Path, *, study_id: str, sample_family: str, feed_uris: list[str], benchmark: StudyBenchmarkResult) -> None:
    source_panel = out_base / "panel" / "panel_v1.csv"
    _write_panel(source_panel, feed_uris)
    create_frozen_study(
        study_root=Layout(out_base=out_base).study_dir(study_id),
        study_id=study_id,
        source_panel_path=source_panel,
        panel_rows=read_panel_rows(source_panel),
        benchmark=benchmark,
        sample_family=sample_family,  # type: ignore[arg-type]
        sample_design={"method": "runtime_fixture", "selected_panel_row_count": len(feed_uris)},
        viewer_modes=benchmark.viewer_modes,
        accept_language="en-US",
        accept_labelers=None,
        include_author_labels=False,
        vantage_id_unauth="unauth_test",
        vantage_id_auth="auth_test",
        posts_per_feed=benchmark.posts_per_feed,
        window_origin_utc=datetime.fromisoformat("2026-03-17T00:00:00+00:00"),
        intended_window_minutes=5,
        shard_count=(benchmark.required_shard_count if sample_family == "micro5_extended_sharded" else None),
        shard_seed=("12345" if sample_family == "micro5_extended_sharded" else None),
    )


def test_panel_hash_mismatch_causes_quarantine(tmp_path: Path) -> None:
    out_base = tmp_path / "out"
    study_id = "study_hash_runtime"
    feed_uris = ["at://did:plc:feed000/app.bsky.feed.generator/main"]
    source_panel = out_base / "panel" / "panel_v1.csv"
    _write_panel(source_panel, feed_uris)
    benchmark = _benchmark(source_panel)
    _create_study(out_base, study_id=study_id, sample_family="micro5_core_full", feed_uris=feed_uris, benchmark=benchmark)

    study_manifest = json.loads((Layout(out_base=out_base).study_manifest_json(study_id)).read_text(encoding="utf-8"))
    frozen_panel = Path(study_manifest["panel_path"])
    _write_panel(frozen_panel, ["at://did:plc:mutated/app.bsky.feed.generator/main"])

    res = _run(
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
            "2026-03-17T00:00:00Z",
        ],
        cwd=Path.cwd(),
    )
    assert res.returncode != 0

    layout = Layout(out_base=out_base)
    window = compute_study_window(
        window_origin_utc=datetime.fromisoformat("2026-03-17T00:00:00+00:00"),
        scheduled_window_start_utc=datetime.fromisoformat("2026-03-17T00:00:00+00:00"),
        window_minutes=5,
    )
    report = assess_micro5_window(layout, study_id=study_id, sample_family="micro5_core_full", window=window)
    codes = {issue["code"] for issue in report["issues"]}
    assert report["verdict"] == "quarantined"
    assert "panel_hash_mismatch" in codes


def test_request_order_reproducible_from_recorded_seed(tmp_path: Path) -> None:
    out_base = tmp_path / "out"
    study_id = "study_order_runtime"
    feed_uris = [f"at://did:plc:feed{i:03d}/app.bsky.feed.generator/main" for i in range(6)]
    source_panel = out_base / "panel" / "panel_v1.csv"
    _write_panel(source_panel, feed_uris)
    benchmark = _benchmark(source_panel, viewer_modes=("unauth",))
    _create_study(out_base, study_id=study_id, sample_family="micro5_core_full", feed_uris=feed_uris, benchmark=benchmark)

    with FakeBskyServer(feeds={uri: 5 for uri in feed_uris}) as srv:
        res = _run(
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
                "2026-03-17T00:00:00Z",
                "--appview-host",
                srv.base_url,
                "--pds-host",
                srv.base_url,
            ],
            cwd=Path.cwd(),
        )
        assert res.returncode == 0, res.stdout

    window_dir = out_base / "micro5" / study_id / "micro5_core_full" / "2026-03-17" / "00" / "00"
    manifest = json.loads((window_dir / "run_manifest.json").read_text(encoding="utf-8"))
    seed = str(manifest["randomization_seed"])
    with (window_dir / "request_provenance.csv").open("r", encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    first_by_feed: dict[str, int] = {}
    for row in rows:
        if row.get("endpoint") != "app.bsky.feed.getFeed":
            continue
        feed_uri = str(row.get("feed_uri") or "")
        if feed_uri and feed_uri not in first_by_feed:
            first_by_feed[feed_uri] = int(row["request_order_in_window"])
    observed = [feed_uri for feed_uri, _order in sorted(first_by_feed.items(), key=lambda item: item[1])]
    expected = list(feed_uris)
    random.Random(seed).shuffle(expected)
    assert observed == expected


@pytest.mark.skipif(sys.platform == "win32", reason="Uses SIGKILL")
def test_micro5_resume_survives_kill_restart(tmp_path: Path) -> None:
    out_base = tmp_path / "out"
    study_id = "study_resume_runtime"
    feed_uris = [f"at://did:plc:feed{i:03d}/app.bsky.feed.generator/main" for i in range(20)]
    source_panel = out_base / "panel" / "panel_v1.csv"
    _write_panel(source_panel, feed_uris)
    benchmark = _benchmark(source_panel, viewer_modes=("unauth",))
    _create_study(out_base, study_id=study_id, sample_family="micro5_core_full", feed_uris=feed_uris, benchmark=benchmark)

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
        "2026-03-17T00:00:00Z",
    ]

    with FakeBskyServer(feeds={uri: 10 for uri in feed_uris}, cfg=FakeBskyConfig(request_delay_s=0.2)) as srv:
        live_cmd = cmd + ["--appview-host", srv.base_url, "--pds-host", srv.base_url]
        proc = subprocess.Popen(live_cmd, cwd=str(Path.cwd()), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        window_dir = out_base / "micro5" / study_id / "micro5_core_full" / "2026-03-17" / "00" / "00"
        status_path = window_dir / "snapshot_status.sqlite"
        try:
            _wait_for_success_row(status_path)
            os.kill(proc.pid, signal.SIGKILL)
        finally:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

        before_size = sum(path.stat().st_size for path in (window_dir / "parts").glob("feed_items_part_*.csv"))
        resumed = _run(live_cmd + ["--resume"], cwd=Path.cwd())
        assert resumed.returncode == 0, resumed.stdout
        after_size = sum(path.stat().st_size for path in (window_dir / "parts").glob("feed_items_part_*.csv"))
        assert after_size >= before_size


def test_micro5_quality_flags_drift_and_overrun(tmp_path: Path) -> None:
    out_base = tmp_path / "out"
    layout = Layout(out_base=out_base)
    study_id = "study_quality_runtime"
    (layout.study_dir(study_id)).mkdir(parents=True, exist_ok=True)
    (layout.study_manifest_json(study_id)).write_text(json.dumps({"study_id": study_id, "panel_hash": "abc"}), encoding="utf-8")
    window_dir = out_base / "micro5" / study_id / "micro5_core_full" / "2026-03-17" / "00" / "00"
    (window_dir / "parts").mkdir(parents=True, exist_ok=True)
    (window_dir / "run_manifest.json").write_text(
        json.dumps(
            {
                "run_id": "r1",
                "study_id": study_id,
                "sample_family": "micro5_core_full",
                "collection_params_hash": "hash1",
                "panel_hash": "abc",
                "scheduled_window_start_utc": "2026-03-17T00:00:00Z",
                "scheduled_window_end_utc": "2026-03-17T00:05:00Z",
                "started_at_utc": "2026-03-17T00:01:40Z",
                "finished_at_utc": "2026-03-17T00:06:20Z",
                "window_minutes": 5,
                "window_index": 0,
                "window_minute": 0,
                "success": True,
                "params": {"viewer_modes": ["unauth"]},
            }
        ),
        encoding="utf-8",
    )
    (window_dir / "progress.json").write_text(json.dumps({"feeds_total": 10, "feeds_done": 9, "feeds_failed": 1}), encoding="utf-8")
    with (window_dir / "request_provenance.csv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["endpoint", "feed_uri", "viewer_mode", "request_started_at_utc"])
        writer.writeheader()
        for idx in range(9):
            writer.writerow(
                {
                    "endpoint": "app.bsky.feed.getFeed",
                    "feed_uri": f"at://feed/{idx}",
                    "viewer_mode": "unauth",
                    "request_started_at_utc": "2026-03-17T00:01:40Z",
                }
            )
    with (window_dir / "parts" / "feed_items_part_000.csv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["feed_uri", "rank"])
        writer.writeheader()
        writer.writerow({"feed_uri": "at://feed/0", "rank": "1"})
    conn = sqlite3.connect(str(window_dir / "snapshot_status.sqlite"))
    try:
        conn.execute("CREATE TABLE feed_tasks (feed_uri TEXT, viewer_mode TEXT, status TEXT, attempts INTEGER, task_order INTEGER)")
        for idx in range(10):
            conn.execute("INSERT INTO feed_tasks VALUES (?, 'unauth', 'success', 1, ?)", (f"at://feed/{idx}", idx + 1))
        conn.commit()
    finally:
        conn.close()

    window = compute_study_window(
        window_origin_utc=datetime.fromisoformat("2026-03-17T00:00:00+00:00"),
        scheduled_window_start_utc=datetime.fromisoformat("2026-03-17T00:00:00+00:00"),
        window_minutes=5,
    )
    report = assess_micro5_window(layout, study_id=study_id, sample_family="micro5_core_full", window=window)
    codes = {issue["code"] for issue in report["issues"]}
    assert report["verdict"] == "quarantined"
    assert "start_drift_exceeds_threshold" in codes
    assert "window_overrun_exceeds_threshold" in codes or "wall_clock_duration_exceeds_threshold" in codes


def test_readme_mentions_study_mode() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    assert "study-benchmark" in readme
    assert "study-init" in readme
    assert "collector_study_daemon.sh" in readme
