from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path


def _run(cmd: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )


def test_seed_post_registry_cli_smoke(tmp_path: Path) -> None:
    path = tmp_path / "hourly" / "2026-03-31" / "00" / "parts" / "feed_items_part_000.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["post_uri", "author_did", "captured_at_utc"])
        writer.writeheader()
        writer.writerow(
            {
                "post_uri": "at://did:plc:cli/app.bsky.feed.post/one",
                "author_did": "did:plc:cli",
                "captured_at_utc": "2026-03-31T00:00:00Z",
            }
        )

    res = _run(
        [
            sys.executable,
            "-m",
            "bsky_collector_v2",
            "seed-post-registry",
            "--out-base",
            str(tmp_path),
            "--include-posts-first-seen",
        ],
        cwd=Path.cwd(),
    )
    assert res.returncode == 0, res.stdout
    manifest_path = tmp_path / "control" / "seed_post_registry_last_run.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest.get("success") is True
    assert int(manifest["summary"]["post_registry_rows"]) == 1


def test_collect_public_omnibus_cli_dry_run_smoke(tmp_path: Path) -> None:
    res = _run(
        [
            sys.executable,
            "-m",
            "bsky_collector_v2",
            "collect-public-omnibus",
            "--out-base",
            str(tmp_path),
            "--dry-run",
            "--no-run-build-panel",
            "--no-run-snapshot-panel",
            "--no-run-wide-sweep",
            "--no-run-hydrate-authors",
            "--no-run-hydrate-feed-generators",
            "--no-run-backfill-interactions",
            "--no-run-backfill-rq1-factors",
            "--no-run-micro-studies",
        ],
        cwd=Path.cwd(),
    )
    assert res.returncode == 0, res.stdout
    manifest_path = tmp_path / "control" / "public_omnibus_last_run.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest.get("success") is True
    assert [item["step"] for item in manifest.get("step_results") or []] == [
        "seed-post-registry",
        "index-feed-generators",
        "refresh-discovery",
    ]
