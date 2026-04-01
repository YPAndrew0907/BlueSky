from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

from fake_bsky_server import FakeBskyServer


def _run(cmd: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )


def test_snapshot_panel_for_you_limit_one_only(tmp_path: Path) -> None:
    out_base = tmp_path / "out"
    out_base.mkdir()

    feed_uri = "at://did:plc:3guzzweuqraryl3rdkimjamk/app.bsky.feed.generator/for-you"

    panel_dir = out_base / "panel"
    panel_dir.mkdir(parents=True, exist_ok=True)
    (panel_dir / "panel_v1.csv").write_text(
        "\n".join(
            [
                "feed_uri,bucket,unauth_skip,built_at_utc,panel_version_id",
                f"{feed_uri},test,0,2026-02-13T00:00:00Z,2026-02-13",
                "",
            ]
        ),
        encoding="utf-8",
    )

    date_str = "2026-02-13"
    hour_str = "2026-02-13T01:00:00Z"

    with FakeBskyServer(feeds={feed_uri: 10}) as srv:
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
                "unauth",
                "--posts-per-feed",
                "5",
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

    hour_dir = out_base / "hourly" / date_str / "01"
    db_path = hour_dir / "snapshot_status.sqlite"
    assert db_path.exists()

    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status, attempts, last_error FROM feed_tasks WHERE feed_uri=? AND viewer_mode='unauth'",
            (feed_uri,),
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert str(row["status"]) == "success"
