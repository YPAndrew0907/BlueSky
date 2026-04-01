from __future__ import annotations

import csv
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bsky_collector_v2.jobs.micro_snapshot_study import _planned_tasks
from bsky_collector_v2.jobs.snapshot_panel import PanelFeed
from bsky_collector_v2.jobs.study_init import StudyInitConfig, run_study_init
from bsky_collector_v2.layout import Layout
from bsky_collector_v2.quality import assess_micro5_window
from bsky_collector_v2.state import SnapshotStatusDB
from bsky_collector_v2.study import (
    StudyBenchmarkResult,
    StudyWindow,
    ceil_to_window_utc,
    expected_snapshot_requests_for_panel,
    file_sha256,
    floor_to_window_utc,
    panel_membership_hash,
    parse_utc_datetime,
    read_panel_rows,
    save_benchmark_result,
    select_shard_rows,
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


def _write_panel_csv(path: Path, *, feed_uris: list[str], panel_version_id: str = "2026-03-17") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "feed_uri,bucket,unauth_skip,built_at_utc,panel_version_id\n"
        + "\n".join(
            f"{feed_uri},popular_by_likecount,0,2026-03-17T00:00:00Z,{panel_version_id}"
            for feed_uri in feed_uris
        )
        + "\n",
        encoding="utf-8",
    )


def _write_min_metadata(out_base: Path, *, date_str: str, feed_uris: list[str]) -> None:
    meta_day = out_base / "metadata" / date_str
    meta_day.mkdir(parents=True, exist_ok=True)
    (meta_day / "discovery_status.json").write_text(json.dumps({"success": True}), encoding="utf-8")
    (meta_day / "feed_catalog.csv").write_text(
        "feed_uri,creator_did,service_did,provider_domain,like_count_last,discovered_from_json,first_seen_utc,last_seen_utc,last_hydrated_utc\n"
        + "\n".join(
            f"{feed_uri},did:plc:test,,example.com,{1000-i},[\"test\"],2026-03-17T00:00:00Z,2026-03-17T00:00:00Z,"
            for i, feed_uri in enumerate(feed_uris)
        )
        + "\n",
        encoding="utf-8",
    )
    (meta_day / "starterpack_feeds.csv").write_text(
        "pack_uri,pack_creator,joinedWeekCount,joinedAllTimeCount,feed_uri,slot_index,captured_at_utc,source\n",
        encoding="utf-8",
    )
    (meta_day / "suggested_feeds.csv").write_text(
        "captured_at_utc,feed_uri,position\n"
        + "\n".join(f"2026-03-17T00:00:00Z,{feed_uri},{idx}" for idx, feed_uri in enumerate(feed_uris))
        + "\n",
        encoding="utf-8",
    )


def _write_benchmark(
    path: Path,
    *,
    panel_path: Path,
    viewer_modes: tuple[str, ...] = ("unauth",),
    posts_per_feed: int = 5,
    throughput_rps: float = 20.0,
    safe_max_panel_size: int | None = None,
    window_minutes: int = 5,
    safety_margin: float = 0.85,
) -> StudyBenchmarkResult:
    rows = read_panel_rows(panel_path)
    estimated_requests = expected_snapshot_requests_for_panel(
        panel_rows=rows,
        viewer_modes=viewer_modes,
        posts_per_feed=posts_per_feed,
        include_auth_session_setup=("auth" in viewer_modes),
    )
    estimated_duration = estimated_requests / max(throughput_rps, 0.001)
    safe_budget = window_minutes * 60.0 * safety_margin
    if safe_max_panel_size is None:
        safe_max_panel_size = len(rows)
    result = StudyBenchmarkResult(
        benchmark_id="bench_test",
        benchmarked_at_utc="2026-03-17T00:00:00Z",
        panel_path=str(panel_path),
        panel_hash=file_sha256(panel_path),
        panel_version_id="2026-03-17",
        panel_row_count=len(rows),
        viewer_modes=viewer_modes,
        posts_per_feed=posts_per_feed,
        concurrency=4,
        rps=1000.0,
        sample_size=min(5, len(rows)),
        measured_request_count=min(5, len(rows)),
        measured_success_count=min(5, len(rows)),
        measured_failure_count=0,
        measured_elapsed_s=round(min(5, len(rows)) / max(throughput_rps, 0.001), 3),
        throughput_rps=throughput_rps,
        safety_margin=safety_margin,
        window_minutes=window_minutes,
        safe_window_budget_s=safe_budget,
        estimated_full_sweep_requests=estimated_requests,
        estimated_full_sweep_duration_s=estimated_duration,
        safe_max_panel_size=safe_max_panel_size,
        required_shard_count=max(1, int((estimated_duration / max(safe_budget, 1.0)) + 0.9999)),
        full_panel_feasible=estimated_duration <= safe_budget,
        dual_viewer_feasible=estimated_duration <= safe_budget,
        full_depth_feasible=estimated_duration <= safe_budget,
    )
    save_benchmark_result(path, result)
    return result


def _init_study(
    *,
    layout: Layout,
    benchmark_path: Path,
    panel_path: Path,
    sample_family: str = "micro5_core_full",
    viewer_modes: tuple[str, ...] = ("unauth",),
    posts_per_feed: int = 5,
    auto_core_size: bool = False,
    requested_core_size: int | None = None,
    auto_shard_count: bool = False,
    requested_shard_count: int | None = None,
) -> dict[str, object]:
    return run_study_init(
        layout=layout,
        cfg=StudyInitConfig(
            sample_family=sample_family,  # type: ignore[arg-type]
            benchmark_path=benchmark_path,
            source_panel_path=panel_path,
            study_id=None,
            viewer_modes=viewer_modes,
            posts_per_feed=posts_per_feed,
            accept_language="en-US",
            accept_labelers=None,
            include_author_labels=False,
            vantage_id_unauth="unauth_test",
            vantage_id_auth="auth_test",
            auto_core_size=auto_core_size,
            requested_core_size=requested_core_size,
            auto_shard_count=auto_shard_count,
            requested_shard_count=requested_shard_count,
            window_origin_utc=parse_utc_datetime("2026-03-17T12:00:00Z"),
            selection_strategy="keep_input_order",
            feed_time_budget_s=20.0,
            max_attempts=3,
        ),
        dry_run=False,
    )


def test_frozen_study_panel_remains_unchanged_across_day_rollovers(tmp_path: Path) -> None:
    layout = Layout(out_base=tmp_path / "out")
    panel_path = layout.panel_active_csv
    _write_panel_csv(panel_path, feed_uris=[f"at://did:plc:day1{i}/app.bsky.feed.generator/main" for i in range(4)])
    benchmark_path = tmp_path / "bench.json"
    _write_benchmark(benchmark_path, panel_path=panel_path, safe_max_panel_size=4)

    manifest = _init_study(layout=layout, benchmark_path=benchmark_path, panel_path=panel_path)
    study_id = str(manifest["study_id"])
    frozen_panel = layout.study_panel_csv(study_id)
    before = frozen_panel.read_text(encoding="utf-8")

    _write_panel_csv(panel_path, feed_uris=[f"at://did:plc:day2{i}/app.bsky.feed.generator/main" for i in range(4)])

    after = frozen_panel.read_text(encoding="utf-8")
    assert before == after
    assert file_sha256(frozen_panel) == str(manifest["panel_hash"])


def test_runner_windows_align_to_five_minute_boundaries() -> None:
    base = parse_utc_datetime("2026-03-17T12:03:17Z")
    assert floor_to_window_utc(base, window_minutes=5) == parse_utc_datetime("2026-03-17T12:00:00Z")
    assert ceil_to_window_utc(base, window_minutes=5) == parse_utc_datetime("2026-03-17T12:05:00Z")
    starts = [
        floor_to_window_utc(parse_utc_datetime(f"2026-03-17T12:{minute:02d}:59Z"), window_minutes=5).minute
        for minute in range(0, 60, 5)
    ]
    assert starts == [0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55]


def test_panel_hash_mismatch_causes_quarantine_failure(tmp_path: Path) -> None:
    root = Path.cwd()
    layout = Layout(out_base=tmp_path / "out")
    panel_path = layout.panel_active_csv
    _write_panel_csv(panel_path, feed_uris=[f"at://did:plc:feed{i}/app.bsky.feed.generator/main" for i in range(4)])
    benchmark_path = tmp_path / "bench.json"
    _write_benchmark(benchmark_path, panel_path=panel_path, safe_max_panel_size=4)
    manifest = _init_study(layout=layout, benchmark_path=benchmark_path, panel_path=panel_path)
    study_id = str(manifest["study_id"])

    frozen_panel = layout.study_panel_csv(study_id)
    os.chmod(frozen_panel, 0o644)
    _write_panel_csv(frozen_panel, feed_uris=[f"at://did:plc:mutated{i}/app.bsky.feed.generator/main" for i in range(3)])

    result = _run(
        [
            sys.executable,
            "-m",
            "bsky_collector_v2",
            "micro-snapshot-study",
            "--out-base",
            str(layout.out_base),
            "--study-id",
            study_id,
            "--viewer-modes",
            "unauth",
            "--scheduled-window-start-utc",
            "2026-03-17T12:00:00Z",
        ],
        cwd=root,
    )
    assert result.returncode != 0

    quality_path = layout.micro5_quality_report_json(
        study_id=study_id,
        sample_family="micro5_core_full",
        window=type("W", (), {"date_str": "2026-03-17", "hour_str": "12", "minute_str": "00"})(),  # path shim
    )
    # Use the real assessor output path directly.
    quality_path = layout.out_base / "micro5" / study_id / "micro5_core_full" / "2026-03-17" / "12" / "00" / "quality_report.json"
    report = json.loads(quality_path.read_text(encoding="utf-8"))
    assert report["verdict"] == "quarantined"
    assert "panel_hash_mismatch" in {issue["code"] for issue in report["issues"]}


def test_benchmark_refuses_impossible_full_panel_micro5_configuration(tmp_path: Path) -> None:
    layout = Layout(out_base=tmp_path / "out")
    panel_path = layout.panel_active_csv
    _write_panel_csv(panel_path, feed_uris=[f"at://did:plc:feed{i}/app.bsky.feed.generator/main" for i in range(10)])
    benchmark_path = tmp_path / "bench.json"
    _write_benchmark(benchmark_path, panel_path=panel_path, safe_max_panel_size=3)

    with pytest.raises(ValueError, match="exceeds 5-minute benchmark capacity"):
        _init_study(layout=layout, benchmark_path=benchmark_path, panel_path=panel_path)


def test_deterministic_shard_membership_is_stable_for_same_study() -> None:
    rows = [
        {"feed_uri": f"at://did:plc:feed{i:03d}/app.bsky.feed.generator/main", "bucket": "popular_by_likecount", "unauth_skip": "0"}
        for i in range(12)
    ]
    shard_a = select_shard_rows(panel_rows=rows, shard_count=3, shard_id=1, shard_seed=12345)
    shard_b = select_shard_rows(panel_rows=rows, shard_count=3, shard_id=1, shard_seed=12345)
    assert [row["feed_uri"] for row in shard_a] == [row["feed_uri"] for row in shard_b]
    assert panel_membership_hash(shard_a) == panel_membership_hash(shard_b)


def test_request_order_is_randomized_but_reproducible_from_seed() -> None:
    feeds = [
        PanelFeed(
            feed_uri=f"at://did:plc:feed{i:03d}/app.bsky.feed.generator/main",  # type: ignore[arg-type]
            bucket="popular_by_likecount",
            unauth_skip=0,
        )
        for i in range(8)
    ]
    tasks_a = _planned_tasks(selected_feeds=feeds, viewer_modes=("unauth",), randomization_seed=123456)
    tasks_b = _planned_tasks(selected_feeds=feeds, viewer_modes=("unauth",), randomization_seed=123456)
    ordered_a = [str(task.feed_uri) for task in tasks_a]
    ordered_b = [str(task.feed_uri) for task in tasks_b]
    assert ordered_a == ordered_b
    assert ordered_a != [str(feed.feed_uri) for feed in feeds]
    assert [task.task_order for task in tasks_a] == list(range(1, len(tasks_a) + 1))


@pytest.mark.skipif(sys.platform == "win32", reason="Uses SIGKILL")
def test_micro5_incremental_writes_survive_kill_restart(tmp_path: Path) -> None:
    root = Path.cwd()
    out_base = tmp_path / "out"
    layout = Layout(out_base=out_base)
    panel_path = layout.panel_active_csv
    feed_uris = [f"at://did:plc:feed{i:03d}/app.bsky.feed.generator/main" for i in range(20)]
    _write_panel_csv(panel_path, feed_uris=feed_uris)
    benchmark_path = tmp_path / "bench.json"
    _write_benchmark(benchmark_path, panel_path=panel_path, viewer_modes=("unauth",), safe_max_panel_size=20)
    manifest = _init_study(
        layout=layout,
        benchmark_path=benchmark_path,
        panel_path=panel_path,
        viewer_modes=("unauth",),
    )
    study_id = str(manifest["study_id"])

    with FakeBskyServer(feeds={feed_uri: 10 for feed_uri in feed_uris}, cfg=FakeBskyConfig(request_delay_s=0.2)) as srv:
        cmd = [
            sys.executable,
            "-m",
            "bsky_collector_v2",
            "micro-snapshot-study",
            "--out-base",
            str(out_base),
            "--appview-host",
            srv.base_url,
            "--pds-host",
            srv.base_url,
            "--study-id",
            study_id,
            "--scheduled-window-start-utc",
            "2026-03-17T12:00:00Z",
            "--viewer-modes",
            "unauth",
        ]
        proc = subprocess.Popen(cmd, cwd=str(root), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            time.sleep(1.5)
            os.kill(proc.pid, signal.SIGKILL)
        finally:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

        window_dir = out_base / "micro5" / study_id / "micro5_core_full" / "2026-03-17" / "12" / "00"
        status_path = window_dir / "snapshot_status.sqlite"
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

        before_size = sum(path.stat().st_size for path in (window_dir / "parts").glob("feed_items_part_*.csv"))

        resumed = _run(cmd + ["--resume"], cwd=root)
        assert resumed.returncode == 0, resumed.stdout

        after_size = sum(path.stat().st_size for path in (window_dir / "parts").glob("feed_items_part_*.csv"))
        assert after_size >= before_size

        conn2 = sqlite3.connect(str(status_path))
        try:
            row2 = conn2.execute(
                "SELECT status, attempts FROM feed_tasks WHERE feed_uri=? AND viewer_mode='unauth'",
                (success_feed_uri,),
            ).fetchone()
        finally:
            conn2.close()
        assert row2 is not None
        assert row2[0] == "success"
        assert int(row2[1]) == success_attempts


def test_micro5_quality_report_flags_overrun_and_drift(tmp_path: Path) -> None:
    layout = Layout(out_base=tmp_path / "out")
    study_id = "study_quality"
    panel_path = layout.study_panel_csv(study_id)
    _write_panel_csv(panel_path, feed_uris=["at://did:plc:feed000/app.bsky.feed.generator/main"])
    study_manifest = {
        "study_id": study_id,
        "panel_hash": file_sha256(panel_path),
        "panel_path": str(panel_path),
        "sample_family": "micro5_core_full",
    }
    layout.study_manifest_json(study_id).parent.mkdir(parents=True, exist_ok=True)
    layout.study_manifest_json(study_id).write_text(json.dumps(study_manifest), encoding="utf-8")

    window = StudyWindow(
        scheduled_window_start_utc=parse_utc_datetime("2026-03-17T12:00:00Z"),
        scheduled_window_end_utc=parse_utc_datetime("2026-03-17T12:05:00Z"),
        window_minutes=5,
        window_index=0,
        window_minute=0,
    )
    micro_dir = layout.out_base / "micro5" / study_id / "micro5_core_full" / "2026-03-17" / "12" / "00"
    micro_dir.mkdir(parents=True, exist_ok=True)
    (micro_dir / "run_manifest.json").write_text(
        json.dumps(
            {
                "run_id": "r1",
                "job_name": "micro-snapshot-study",
                "study_id": study_id,
                "sample_family": "micro5_core_full",
                "collection_params_hash": "abc",
                "panel_hash": study_manifest["panel_hash"],
                "scheduled_window_start_utc": "2026-03-17T12:00:00Z",
                "scheduled_window_end_utc": "2026-03-17T12:05:00Z",
                "started_at_utc": "2026-03-17T12:02:00Z",
                "finished_at_utc": "2026-03-17T12:07:00Z",
                "success": True,
                "params": {"viewer_modes": ["unauth"]},
            }
        ),
        encoding="utf-8",
    )
    (micro_dir / "progress.json").write_text(
        json.dumps({"feeds_total": 1, "feeds_done": 1, "feeds_failed": 0}),
        encoding="utf-8",
    )
    with SnapshotStatusDB.open(micro_dir / "snapshot_status.sqlite") as status:
        status.ensure_tasks(tasks=[("at://did:plc:feed000/app.bsky.feed.generator/main", "unauth")], updated_at_utc="2026-03-17T12:00:00Z")
    (micro_dir / "parts").mkdir(exist_ok=True)
    (micro_dir / "parts" / "feed_items_part_000.csv").write_text(
        "run_id,feed_uri,viewer_mode,vantage_id,rank\nr1,at://did:plc:feed000/app.bsky.feed.generator/main,unauth,unauth_test,1\n",
        encoding="utf-8",
    )
    (micro_dir / "request_provenance.csv").write_text(
        "run_id,job_name,sample_family,collection_params_hash,request_started_at_utc,endpoint,feed_uri,viewer_mode\n"
        "r1,micro-snapshot-study,micro5_core_full,abc,2026-03-17T12:02:00Z,app.bsky.feed.getFeed,at://did:plc:feed000/app.bsky.feed.generator/main,unauth\n",
        encoding="utf-8",
    )

    report = assess_micro5_window(layout, study_id=study_id, sample_family="micro5_core_full", window=window)
    codes = {issue["code"] for issue in report["issues"]}
    assert report["verdict"] == "quarantined"
    assert "start_drift_exceeds_threshold" in codes
    assert "wall_clock_duration_exceeds_threshold" in codes
    assert "window_overrun_exceeds_threshold" in codes


def test_build_panel_does_not_mutate_active_study(tmp_path: Path) -> None:
    root = Path.cwd()
    out_base = tmp_path / "out"
    layout = Layout(out_base=out_base)
    original_feed_uris = [f"at://did:plc:orig{i}/app.bsky.feed.generator/main" for i in range(4)]
    _write_panel_csv(layout.panel_active_csv, feed_uris=original_feed_uris)
    benchmark_path = tmp_path / "bench.json"
    _write_benchmark(benchmark_path, panel_path=layout.panel_active_csv, safe_max_panel_size=4)
    manifest = _init_study(layout=layout, benchmark_path=benchmark_path, panel_path=layout.panel_active_csv)
    study_id = str(manifest["study_id"])
    frozen_panel = layout.study_panel_csv(study_id)
    frozen_hash_before = file_sha256(frozen_panel)

    new_feed_uris = [f"at://did:plc:new{i}/app.bsky.feed.generator/main" for i in range(6)]
    _write_min_metadata(out_base, date_str="2026-03-18", feed_uris=new_feed_uris)
    with FakeBskyServer(feeds={feed_uri: 5 for feed_uri in new_feed_uris}) as srv:
        result = _run(
            [
                sys.executable,
                "-m",
                "bsky_collector_v2",
                "build-panel",
                "--out-base",
                str(out_base),
                "--appview-host",
                srv.base_url,
                "--pds-host",
                srv.base_url,
                "--concurrency",
                "2",
                "--rps",
                "1000",
            ],
            cwd=root,
        )
        assert result.returncode == 0, result.stdout

    assert file_sha256(frozen_panel) == frozen_hash_before
    active_rows = read_panel_rows(layout.panel_active_csv)
    assert {row["feed_uri"] for row in active_rows} != set(original_feed_uris)
    assert {row["feed_uri"] for row in read_panel_rows(frozen_panel)} == set(original_feed_uris)


def test_readme_examples_run_conceptually_end_to_end() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    assert "study-benchmark" in readme
    assert "study-init" in readme
    assert "micro-snapshot-study" in readme
    assert "collector_study_daemon.sh" in readme
    assert "changing cron" in readme.lower()
    assert "every 5 minutes" in readme.lower()
