from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import threading
import time
import socket
from pathlib import Path

from bsky_collector_v2.layout import Layout
from bsky_collector_v2.state import ControlState
from bsky_collector_v2.state_writer import StateWriterConfig, run_state_writer
from bsky_collector_v2.time_utils import now_utc, utc_date_str
from bsky_collector_v2.types import PostUri
from fake_bsky_rq1_server import FakeBskyRq1Server


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


def _pick_free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_for_tcp(host: str, port: int, *, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError(f"state-writer tcp not reachable: {host}:{port}")


def test_backfill_interactions_treats_zero_limit_as_uncapped(tmp_path: Path) -> None:
    layout = Layout(tmp_path)
    post_uri = PostUri("at://did:plc:author000/app.bsky.feed.post/post000")
    with ControlState.open(layout.control_db_path) as control:
        control.upsert_post_registry_many(post_uris=[post_uri], seen_at_utc="2026-03-31T00:00:00Z")
        control.commit()

    with FakeBskyRq1Server() as server:
        cmd = [
            sys.executable,
            "-m",
            "bsky_collector_v2",
            "backfill-interactions",
            "--out-base",
            str(tmp_path),
            "--appview-host",
            server.base_url,
            "--pds-host",
            server.base_url,
            "--max-posts",
            "1",
            "--batch-size",
            "1",
            "--max-items-per-endpoint",
            "0",
        ]
        res = _run(cmd, cwd=Path.cwd())
        assert res.returncode == 0, res.stdout

    day_dir = tmp_path / "interactions" / utc_date_str(now_utc())
    summary_path = day_dir / "post_interaction_summary_part_000.csv"
    likes_path = day_dir / "post_likes_part_000.csv"
    quotes_path = day_dir / "post_quotes_part_000.csv"
    reposts_path = day_dir / "post_reposted_by_part_000.csv"
    progress_path = day_dir / "progress.json"
    manifest_path = day_dir / "run_manifest.json"

    with open(summary_path, "r", encoding="utf-8", newline="") as f:
        summary_rows = list(csv.DictReader(f))
    assert len(summary_rows) == 1
    assert summary_rows[0]["likes_returned"] == "2"
    assert summary_rows[0]["quotes_returned"] == "2"
    assert summary_rows[0]["reposted_by_returned"] == "2"
    assert summary_rows[0]["relationship_edges_returned"] == "6"

    for path in (likes_path, quotes_path, reposts_path):
        with open(path, "r", encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 2

    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    assert progress["unit_label"] == "posts"
    assert progress["feeds_total"] == 1
    assert progress["feeds_done"] == 1
    assert progress["details"]["effective_max_items_per_endpoint"] == "uncapped"
    assert progress["details"]["selected_first_seen_min_utc"] == "2026-03-31T00:00:00Z"
    assert progress["details"]["selected_first_seen_max_utc"] == "2026-03-31T00:00:00Z"
    assert progress["details"]["phase"] == "complete"

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["effective_limits"]["max_items_per_endpoint"] is None
    assert manifest["selection"]["selected_posts"] == 1
    assert manifest["selection"]["selected_first_seen_min_utc"] == "2026-03-31T00:00:00Z"
    assert manifest["selection"]["selected_first_seen_max_utc"] == "2026-03-31T00:00:00Z"


def test_backfill_interactions_runs_via_tcp_state_writer(tmp_path: Path) -> None:
    layout = Layout(tmp_path)
    post_uri = PostUri("at://did:plc:author000/app.bsky.feed.post/post000")
    with ControlState.open_local(layout.control_db_path) as control:
        control.upsert_post_registry_many(post_uris=[post_uri], seen_at_utc="2026-03-31T00:00:00Z")
        control.commit()

    host = "127.0.0.1"
    port = _pick_free_tcp_port()
    thread = threading.Thread(
        target=run_state_writer,
        kwargs={"cfg": StateWriterConfig(db_path=layout.control_db_path, tcp_host=host, tcp_port=port)},
        daemon=True,
    )
    thread.start()
    _wait_for_tcp(host, port)

    env = os.environ.copy()
    env["BSKY_STATE_WRITER_SOCKET"] = f"tcp://{host}:{port}"

    try:
        with FakeBskyRq1Server() as server:
            cmd = [
                sys.executable,
                "-m",
                "bsky_collector_v2",
                "backfill-interactions",
                "--out-base",
                str(tmp_path),
                "--appview-host",
                server.base_url,
                "--pds-host",
                server.base_url,
                "--max-posts",
                "1",
                "--batch-size",
                "1",
                "--max-items-per-endpoint",
                "1",
            ]
            res = _run(cmd, cwd=Path.cwd(), env=env)
            assert res.returncode == 0, res.stdout
    finally:
        if thread.is_alive():
            from bsky_collector_v2.state import RemoteControlState

            RemoteControlState(path=layout.control_db_path, tcp_host=host, tcp_port=port)._rpc("shutdown")
        thread.join(timeout=5)
        assert not thread.is_alive()

    day_dir = tmp_path / "interactions" / utc_date_str(now_utc())
    progress = json.loads((day_dir / "progress.json").read_text(encoding="utf-8"))
    manifest = json.loads((day_dir / "run_manifest.json").read_text(encoding="utf-8"))
    assert progress["feeds_done"] == 1
    assert progress["details"]["phase"] == "complete"
    assert manifest["selection"]["selected_posts"] == 1
