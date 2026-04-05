from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from fake_bsky_server import FakeBskyConfig, FakeBskyServer


def _run(cmd: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )


def _write_min_metadata(out_base: Path, *, date_str: str, feed_uris: list[str]) -> None:
    meta_day = out_base / "metadata" / date_str
    sources = meta_day / "discovery_sources"
    sources.mkdir(parents=True, exist_ok=True)

    # feed_catalog.csv must include like_count_last for popularity ranking.
    feed_catalog = meta_day / "feed_catalog.csv"
    feed_catalog.write_text(
        "feed_uri,creator_did,service_did,provider_domain,like_count_last,discovered_from_json,first_seen_utc,last_seen_utc,last_hydrated_utc\n"
        + "\n".join(
            f"{u},did:plc:test,,example.com,{1000-i},[\"test\"],2026-02-13T00:00:00Z,2026-02-13T00:00:00Z,\n"
            for i, u in enumerate(feed_uris)
        )
        + "\n",
        encoding="utf-8",
    )

    # starterpack_feeds.csv optional (keep header compatible).
    (meta_day / "starterpack_feeds.csv").write_text(
        "pack_uri,pack_creator,joinedWeekCount,joinedAllTimeCount,feed_uri,slot_index,captured_at_utc,source\n",
        encoding="utf-8",
    )

    # suggested_feeds.jsonl optional.
    (sources / "suggested_feeds.jsonl").write_text("", encoding="utf-8")


def test_smoke_build_panel_and_snapshot(tmp_path: Path) -> None:
    out_base = tmp_path / "out"
    out_base.mkdir()
    date_str = "2026-02-13"
    hour_str = "2026-02-13T01:00:00Z"

    feed_uris = [
        "at://did:plc:feed000/app.bsky.feed.generator/main",
        "at://did:plc:feed001/app.bsky.feed.generator/main",
        "at://did:plc:feed002/app.bsky.feed.generator/main",
    ]

    with FakeBskyServer(feeds={u: 10 for u in feed_uris}) as srv:
        _write_min_metadata(out_base, date_str=date_str, feed_uris=feed_uris)

        # build-panel
        res = _run(
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
            cwd=Path.cwd(),
        )
        assert res.returncode == 0, res.stdout
        panel_path = out_base / "panel" / "panel_v1.csv"
        assert panel_path.exists()

        # auth env for snapshot
        env_path = tmp_path / "auth.env"
        env_path.write_text(
            "\n".join(
                [
                    "BLUESKY_IDENTIFIER=test",
                    "BLUESKY_APP_PASSWORD=secret",
                    f"BLUESKY_PDS={srv.base_url}",
                    "",
                ]
            ),
            encoding="utf-8",
        )

        # snapshot-panel (both modes)
        res2 = _run(
            [
                sys.executable,
                "-m",
                "bsky_collector_v2",
                "snapshot-panel",
                "--out-base",
                str(out_base),
                "--env-path",
                str(env_path),
                "--appview-host",
                srv.base_url,
                "--pds-host",
                srv.base_url,
                "--viewer-modes",
                "unauth,auth",
                "--posts-per-feed",
                "5",
                "--concurrency",
                "2",
                "--rps",
                "1000",
                "--snapshot-hour-utc",
                hour_str,
            ],
            cwd=Path.cwd(),
        )
        assert res2.returncode == 0, res2.stdout

    hour_dir = out_base / "hourly" / date_str / "01"
    assert (hour_dir / "run_manifest.json").exists()
    assert (hour_dir / "progress.json").exists()
    assert (hour_dir / "http_stats.csv").exists()
    assert (hour_dir / "snapshot_status.sqlite").exists()

    parts = hour_dir / "parts"
    feed_items = sorted(parts.glob("feed_items_part_*.csv"))
    assert feed_items
    assert feed_items[0].stat().st_size > 0

    manifest = json.loads((hour_dir / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["job_name"] == "snapshot-panel"
    assert manifest.get("success") is True
    assert manifest.get("labelers_included_by_viewer_mode", {}).get("unauth") == ["did:plc:labeler000"]


def test_refresh_discovery_writes_metadata_files(tmp_path: Path) -> None:
    out_base = tmp_path / "out"
    out_base.mkdir()

    feed_uris = [
        "at://did:plc:feed000/app.bsky.feed.generator/main",
        "at://did:plc:feed001/app.bsky.feed.generator/main",
        "at://did:plc:feed002/app.bsky.feed.generator/main",
        "at://did:plc:feed003/app.bsky.feed.generator/main",
        "at://did:plc:feed004/app.bsky.feed.generator/main",
    ]

    with FakeBskyServer(feeds={u: 5 for u in feed_uris}) as srv:
        res = _run(
            [
                sys.executable,
                "-m",
                "bsky_collector_v2",
                "refresh-discovery",
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
            cwd=Path.cwd(),
        )
        assert res.returncode == 0, res.stdout

    meta_root = out_base / "metadata"
    day_dirs = sorted([p for p in meta_root.iterdir() if p.is_dir()])
    assert day_dirs
    day_dir = day_dirs[-1]

    assert (day_dir / "feed_catalog.csv").exists()
    assert (day_dir / "starterpack_feeds.csv").exists()
    assert (day_dir / "starterpack_accounts.csv").exists()
    assert (day_dir / "suggested_feeds.csv").exists()
    assert (day_dir / "suggested_accounts.csv").exists()
    assert (day_dir / "suggested_follows_by_actor.csv").exists()

    sources = day_dir / "discovery_sources"
    assert (sources / "popular_feed_generators.jsonl").exists()
    assert (sources / "suggested_feeds.jsonl").exists()
    assert (sources / "suggested_accounts.jsonl").exists()
    assert (sources / "suggested_follows_by_actor.jsonl").exists()
    assert (sources / "onboarding_suggested_starterpacks.jsonl").exists()


def test_disk_mount_failure_exits_before_writing(tmp_path: Path) -> None:
    out_base = tmp_path / "does_not_exist"
    res = _run(
        [sys.executable, "-m", "bsky_collector_v2", "healthcheck", "--out-base", str(out_base), "--dry-run"],
        cwd=Path.cwd(),
    )
    assert res.returncode != 0
    assert not out_base.exists()


def test_auth_missing_skips_auth_mode(tmp_path: Path) -> None:
    out_base = tmp_path / "out"
    out_base.mkdir()
    date_str = "2026-02-13"
    hour_str = "2026-02-13T02:00:00Z"
    feed_uris = ["at://did:plc:feed000/app.bsky.feed.generator/main"]

    (out_base / "panel").mkdir(parents=True, exist_ok=True)
    (out_base / "panel" / "panel_v1.csv").write_text(
        "feed_uri,bucket,unauth_skip,built_at_utc,panel_version_id\n"
        + f"{feed_uris[0]},popular_by_likecount,0,2026-02-13T00:00:00Z,2026-02-13\n",
        encoding="utf-8",
    )

    with FakeBskyServer(feeds={feed_uris[0]: 5}) as srv:
        res = _run(
            [
                sys.executable,
                "-m",
                "bsky_collector_v2",
                "snapshot-panel",
                "--out-base",
                str(out_base),
                "--appview-host",
                srv.base_url,
                "--pds-host",
                srv.base_url,
                "--viewer-modes",
                "unauth,auth",
                "--posts-per-feed",
                "3",
                "--concurrency",
                "1",
                "--rps",
                "1000",
                "--snapshot-hour-utc",
                hour_str,
            ],
            cwd=Path.cwd(),
        )
        assert res.returncode == 0, res.stdout
        assert "skipping auth mode" in res.stdout.lower()


@pytest.mark.skipif(sys.platform == "win32", reason="Uses SIGKILL")
def test_crash_resume_snapshot_panel(tmp_path: Path) -> None:
    out_base = tmp_path / "out"
    out_base.mkdir()
    date_str = "2026-02-13"
    hour_str = "2026-02-13T03:00:00Z"

    feed_uris = [f"at://did:plc:feed{i:03d}/app.bsky.feed.generator/main" for i in range(20)]
    (out_base / "panel").mkdir(parents=True, exist_ok=True)
    (out_base / "panel" / "panel_v1.csv").write_text(
        "feed_uri,bucket,unauth_skip,built_at_utc,panel_version_id\n"
        + "\n".join(
            f"{u},popular_by_likecount,0,2026-02-13T00:00:00Z,2026-02-13" for u in feed_uris
        )
        + "\n",
        encoding="utf-8",
    )

    with FakeBskyServer(feeds={u: 10 for u in feed_uris}, cfg=FakeBskyConfig(request_delay_s=0.2)) as srv:
        cmd = [
            sys.executable,
            "-m",
            "bsky_collector_v2",
            "snapshot-panel",
            "--out-base",
            str(out_base),
            "--appview-host",
            srv.base_url,
            "--pds-host",
            srv.base_url,
            "--viewer-modes",
            "unauth",
            "--posts-per-feed",
            "5",
            "--concurrency",
            "2",
            "--rps",
            "1000",
            "--snapshot-hour-utc",
            hour_str,
        ]
        p = subprocess.Popen(cmd, cwd=str(Path.cwd()), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            time.sleep(1.5)
            os.kill(p.pid, signal.SIGKILL)
        finally:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()

        hour_dir = out_base / "hourly" / date_str / "03"
        assert (hour_dir / "snapshot_status.sqlite").exists()

        import sqlite3

        conn = sqlite3.connect(str(hour_dir / "snapshot_status.sqlite"))
        try:
            row = conn.execute(
                "SELECT feed_uri, attempts FROM feed_tasks WHERE status='success' ORDER BY feed_uri LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        success_feed_uri, success_attempts = row[0], int(row[1])

        before_size = sum(f.stat().st_size for f in (hour_dir / "parts").glob("feed_items_part_*.csv"))

        # Resume same hour.
        res = _run(cmd + ["--resume"], cwd=Path.cwd())
        assert res.returncode == 0, res.stdout

        after_size = sum(f.stat().st_size for f in (hour_dir / "parts").glob("feed_items_part_*.csv"))
        assert after_size >= before_size

        conn2 = sqlite3.connect(str(hour_dir / "snapshot_status.sqlite"))
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
