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


def _write_csv(path: Path, *, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = sorted({key for row in rows for key in row})
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _seed_minimal_rq2_fixture(*, out_dir: Path, annotation_dir: Path) -> Path:
    batch_dir = out_dir / "topic_batch"
    topic_dir = batch_dir / "epstein"
    _write_csv(
        topic_dir / "clusters" / "cluster_membership.csv",
        rows=[
            {"cluster_id": "c1", "post_uri": "at://did:1/app.bsky.feed.post/one"},
        ],
    )
    _write_csv(
        topic_dir / "probe" / "matched_feed_items.csv",
        rows=[
            {
                "post_uri": "at://did:1/app.bsky.feed.post/one",
                "viewer_mode": "unauth",
                "surface": "hourly",
                "bucket": "popular_by_likecount",
                "feed_uri": "at://feed/a",
                "rank": "1",
                "captured_at_utc": "2026-03-31T00:00:00Z",
            },
        ],
    )
    labels_path = annotation_dir / "coder1_annotations.csv"
    _write_csv(
        labels_path,
        rows=[
            {
                "example_id": "epstein_001",
                "topic": "epstein",
                "cluster_id": "c1",
                "frame_label": "supportive",
                "event_guess": "epstein-files",
                "label_confidence": "high",
                "text": "Example row",
            }
        ],
    )
    return labels_path


def test_rq2_pipeline_cli_can_resume_from_existing_batch_and_annotations(tmp_path: Path) -> None:
    out_dir = tmp_path / "rq2"
    annotation_dir = out_dir / "annotations"
    data_root = tmp_path / "data_v2_full"
    data_root.mkdir(parents=True, exist_ok=True)
    _seed_minimal_rq2_fixture(out_dir=out_dir, annotation_dir=annotation_dir)

    res = _run(
        [
            sys.executable,
            "-m",
            "bsky_collector_v2",
            "rq2-pipeline",
            "--out-base",
            str(data_root),
            "--out-dir",
            str(out_dir),
            "--annotation-dir",
            str(annotation_dir),
            "--topic",
            "epstein",
            "--no-run-topic-batch",
            "--no-run-sampling",
            "--no-run-label-application",
            "--run-annotation-merge",
            "--run-frame-table",
        ],
        cwd=Path.cwd(),
    )

    assert res.returncode == 0, res.stdout
    summary_path = out_dir / "summary.json"
    assert summary_path.exists()
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["steps"]["topic_batch"]["status"] == "skipped"
    assert summary["steps"]["annotation_merge"]["status"] == "completed"
    assert summary["steps"]["frame_tables"]["status"] == "completed"
    assert (out_dir / "frame_tables" / "frame_overall_exposure_vs_supply.csv").exists()


def test_rq2_generate_frame_tables_cli_smoke(tmp_path: Path) -> None:
    out_dir = tmp_path / "rq2"
    annotation_dir = out_dir / "annotations"
    data_root = tmp_path / "data_v2_full"
    data_root.mkdir(parents=True, exist_ok=True)
    labels_path = _seed_minimal_rq2_fixture(out_dir=out_dir, annotation_dir=annotation_dir)
    frame_dir = out_dir / "frame_tables_cli"

    res = _run(
        [
            sys.executable,
            "-m",
            "bsky_collector_v2",
            "rq2-generate-frame-tables",
            "--out-base",
            str(data_root),
            "--batch-dir",
            str(out_dir / "topic_batch"),
            "--label-rows-path",
            str(labels_path),
            "--out-dir",
            str(frame_dir),
        ],
        cwd=Path.cwd(),
    )

    assert res.returncode == 0, res.stdout
    assert (frame_dir / "frame_overall_exposure_vs_supply.csv").exists()
