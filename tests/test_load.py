from __future__ import annotations

import csv
import json
import sys
import time
from pathlib import Path

from fake_bsky_server import FakeBskyServer


def _run(cmd: list[str], *, cwd: Path) -> tuple[int, str]:
    import subprocess

    p = subprocess.run(
        cmd,
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    return p.returncode, p.stdout


def test_load_snapshot_panel_50_feeds_both_modes(tmp_path: Path) -> None:
    out_base = tmp_path / "out"
    out_base.mkdir()
    date_str = "2026-02-13"
    hour_str = "2026-02-13T04:00:00Z"

    feed_uris = [f"at://did:plc:feed{i:03d}/app.bsky.feed.generator/main" for i in range(50)]
    (out_base / "panel").mkdir(parents=True, exist_ok=True)
    (out_base / "panel" / "panel_v1.csv").write_text(
        "feed_uri,bucket,unauth_skip,built_at_utc,panel_version_id\n"
        + "\n".join(f"{u},popular_by_likecount,0,2026-02-13T00:00:00Z,2026-02-13" for u in feed_uris)
        + "\n",
        encoding="utf-8",
    )

    env_path = tmp_path / "auth.env"
    with FakeBskyServer(feeds={u: 100 for u in feed_uris}) as srv:
        env_path.write_text(
            "\n".join(
                [
                    "BSKY_IDENTIFIER=test",
                    "BSKY_APP_PASSWORD=secret",
                    f"BSKY_PDS_HOST={srv.base_url}",
                    "",
                ]
            ),
            encoding="utf-8",
        )

        cmd = [
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
            "100",
            "--concurrency",
            "16",
            "--rps",
            "1000",
            "--snapshot-hour-utc",
            hour_str,
        ]
        t0 = time.monotonic()
        rc, out = _run(cmd, cwd=Path.cwd())
        dt = time.monotonic() - t0
        print(f"load_test runtime_s={dt:.3f} rc={rc}")
        assert rc == 0, out
        assert dt < 60.0

    hour_dir = out_base / "hourly" / date_str / "04"
    manifest = json.loads((hour_dir / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest.get("success") is True

    # Sanity: impressions roughly match 50 feeds * 2 modes * 100 posts.
    parts = hour_dir / "parts"
    total_rows = 0
    for path in parts.glob("feed_items_part_*.csv"):
        with path.open("r", encoding="utf-8", newline="") as f:
            total_rows += sum(1 for _ in csv.DictReader(f))
    assert total_rows >= 50 * 2 * 90

