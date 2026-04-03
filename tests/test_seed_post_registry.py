from __future__ import annotations

import csv
import logging
import os
import socket
import time
from multiprocessing import Process
from pathlib import Path

import pytest

from bsky_collector_v2.jobs.seed_post_registry import SeedPostRegistryConfig, run_seed_post_registry
from bsky_collector_v2.layout import Layout
from bsky_collector_v2.state import ControlState, RemoteControlState
from bsky_collector_v2.state_writer import StateWriterConfig, run_state_writer


def _write_csv(path: Path, *, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = sorted({key for row in rows for key in row})
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


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


def test_seed_post_registry_scans_hourly_wide_micro_and_enqueues(tmp_path: Path) -> None:
    out_base = tmp_path
    layout = Layout(out_base)

    _write_csv(
        out_base / "hourly" / "2026-03-31" / "00" / "parts" / "feed_items_part_000.csv",
        rows=[
            {
                "post_uri": "at://did:plc:hourly/app.bsky.feed.post/one",
                "author_did": "did:plc:hourly",
                "captured_at_utc": "2026-03-31T00:00:10Z",
            }
        ],
    )
    _write_csv(
        out_base / "wide" / "2026-03-31" / "parts" / "posts_first_seen_part_000.csv",
        rows=[
            {
                "post_uri": "at://did:plc:wide/app.bsky.feed.post/two",
                "author_did": "did:plc:wide",
            }
        ],
    )
    _write_csv(
        out_base / "micro5" / "studyA" / "micro5_core_full" / "2026-03-31" / "00" / "05" / "parts" / "feed_items_part_000.csv",
        rows=[
            {
                "post_uri": "at://did:plc:micro/app.bsky.feed.post/three",
                "author_did": "did:plc:micro",
                "scheduled_window_start_utc": "2026-03-31T00:05:00Z",
            }
        ],
    )

    summary = run_seed_post_registry(
        layout=layout,
        run_id="seed-run-000",
        dry_run=False,
        cfg=SeedPostRegistryConfig(
            include_hourly=True,
            include_wide=True,
            include_micro5=True,
            include_posts_first_seen=True,
            enqueue_interactions=True,
            enqueue_rq1_factors=True,
            mark_first_written=True,
        ),
    )

    assert summary.files_scanned == 3
    assert summary.post_rows_processed == 3
    assert summary.post_registry_rows == 3
    assert summary.author_registry_rows == 3
    assert summary.files_by_family == {"hourly": 1, "wide": 1, "micro5": 1}
    assert summary.rows_by_family == {"hourly": 1, "wide": 1, "micro5": 1}

    with ControlState.open(layout.control_db_path) as control:
        post_rows = control.conn.execute(
            "SELECT post_uri, first_written FROM post_registry ORDER BY post_uri"
        ).fetchall()
        assert len(post_rows) == 3
        assert {int(row["first_written"]) for row in post_rows} == {1}

        interaction_rows = control.conn.execute(
            "SELECT post_uri, hydrated_count, last_hydrated_utc FROM post_interaction_registry ORDER BY post_uri"
        ).fetchall()
        assert len(interaction_rows) == 3
        assert {int(row["hydrated_count"]) for row in interaction_rows} == {0}
        assert {row["last_hydrated_utc"] for row in interaction_rows} == {None}

        rq1_rows = control.conn.execute(
            "SELECT post_uri, hydrated_count, last_hydrated_utc FROM post_rq1_factor_registry ORDER BY post_uri"
        ).fetchall()
        assert len(rq1_rows) == 3
        assert {int(row["hydrated_count"]) for row in rq1_rows} == {0}
        assert {row["last_hydrated_utc"] for row in rq1_rows} == {None}

        authors = control.conn.execute("SELECT author_did FROM author_registry ORDER BY author_did").fetchall()
        assert [str(row["author_did"]) for row in authors] == [
            "did:plc:hourly",
            "did:plc:micro",
            "did:plc:wide",
        ]


def test_seed_post_registry_uses_remote_state_writer_for_summary_counts(tmp_path: Path) -> None:
    out_base = tmp_path
    layout = Layout(out_base)
    _write_csv(
        out_base / "hourly" / "2026-03-31" / "00" / "parts" / "feed_items_part_000.csv",
        rows=[
            {
                "post_uri": "at://did:plc:remote/app.bsky.feed.post/one",
                "author_did": "did:plc:remote",
                "captured_at_utc": "2026-03-31T00:00:10Z",
            }
        ],
    )

    host = "127.0.0.1"
    port = _pick_free_tcp_port()
    proc = Process(
        target=run_state_writer,
        kwargs={"cfg": StateWriterConfig(db_path=layout.control_db_path, tcp_host=host, tcp_port=port)},
        daemon=True,
    )
    proc.start()
    _wait_for_tcp(host, port)

    old_socket_env = os.environ.get("BSKY_STATE_WRITER_SOCKET")
    os.environ["BSKY_STATE_WRITER_SOCKET"] = f"tcp://{host}:{port}"

    try:
        summary = run_seed_post_registry(
            layout=layout,
            run_id="seed-run-remote",
            dry_run=False,
            cfg=SeedPostRegistryConfig(
                include_hourly=True,
                include_wide=False,
                include_micro5=False,
                enqueue_interactions=True,
                enqueue_rq1_factors=True,
                mark_first_written=True,
            ),
        )
        assert summary.files_scanned == 1
        assert summary.post_registry_rows == 1
        assert summary.author_registry_rows == 1

        with ControlState.open_local(layout.control_db_path) as control:
            post_rows = control.conn.execute("SELECT post_uri FROM post_registry").fetchall()
            assert [str(row["post_uri"]) for row in post_rows] == ["at://did:plc:remote/app.bsky.feed.post/one"]
    finally:
        if old_socket_env is None:
            os.environ.pop("BSKY_STATE_WRITER_SOCKET", None)
        else:
            os.environ["BSKY_STATE_WRITER_SOCKET"] = old_socket_env

        try:
            RemoteControlState(path=layout.control_db_path, tcp_host=host, tcp_port=port)._rpc("shutdown")
        except Exception:  # noqa: BLE001
            pass
        proc.join(timeout=5)
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=5)


def test_seed_post_registry_raises_on_control_plane_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    layout = Layout(tmp_path)
    _write_csv(
        tmp_path / "hourly" / "2026-03-31" / "00" / "parts" / "feed_items_part_000.csv",
        rows=[
            {
                "post_uri": "at://did:plc:broken/app.bsky.feed.post/one",
                "author_did": "did:plc:broken",
                "captured_at_utc": "2026-03-31T00:00:10Z",
            }
        ],
    )

    def _flush_failure(**_kwargs: object) -> tuple[int, int]:
        raise ConnectionResetError(10054, "simulated writer reset")

    monkeypatch.setattr("bsky_collector_v2.jobs.seed_post_registry._flush_batch", _flush_failure)
    caplog.set_level(logging.ERROR, logger="bsky_collector_v2.job.seed_post_registry")

    with pytest.raises(ConnectionResetError):
        run_seed_post_registry(
            layout=layout,
            run_id="seed-run-control-error",
            dry_run=False,
            cfg=SeedPostRegistryConfig(
                include_hourly=True,
                include_wide=False,
                include_micro5=False,
                enqueue_interactions=True,
                enqueue_rq1_factors=True,
            ),
        )

    assert "seed-post-registry control-state update failed" in caplog.text
    assert "seed-post-registry source file read failed" not in caplog.text
