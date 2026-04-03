from __future__ import annotations

import csv
from pathlib import Path

from bsky_collector_v2.rq2_frame_tables import generate_frame_exposure_supply_tables
from bsky_collector_v2.rq2_pipeline import Rq2PipelineConfig, run_rq2_pipeline


def _write_csv(path: Path, *, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = sorted({key for row in rows for key in row})
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_generate_frame_exposure_supply_tables_builds_supply_and_exposure_outputs(tmp_path: Path) -> None:
    batch_dir = tmp_path / "topic_batch"
    topic_dir = batch_dir / "epstein"
    _write_csv(
        topic_dir / "clusters" / "cluster_membership.csv",
        rows=[
            {"cluster_id": "c1", "post_uri": "at://did:1/app.bsky.feed.post/one"},
            {"cluster_id": "c1", "post_uri": "at://did:2/app.bsky.feed.post/two"},
            {"cluster_id": "c2", "post_uri": "at://did:3/app.bsky.feed.post/three"},
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
            {
                "post_uri": "at://did:1/app.bsky.feed.post/one",
                "viewer_mode": "auth",
                "surface": "hourly",
                "bucket": "popular_by_likecount",
                "feed_uri": "at://feed/a",
                "rank": "2",
                "captured_at_utc": "2026-03-31T00:10:00Z",
            },
            {
                "post_uri": "at://did:2/app.bsky.feed.post/two",
                "viewer_mode": "unauth",
                "surface": "wide",
                "bucket": "longtail_random",
                "feed_uri": "at://feed/b",
                "rank": "7",
                "captured_at_utc": "2026-03-31T00:20:00Z",
            },
            {
                "post_uri": "at://did:3/app.bsky.feed.post/three",
                "viewer_mode": "unauth",
                "surface": "hourly",
                "bucket": "popular_by_likecount",
                "feed_uri": "at://feed/c",
                "rank": "4",
                "captured_at_utc": "2026-03-31T00:30:00Z",
            },
        ],
    )
    labels_path = tmp_path / "annotations_merged.csv"
    _write_csv(
        labels_path,
        rows=[
            {
                "topic": "epstein",
                "cluster_id": "c1",
                "frame_label": "supportive",
                "event_guess": "epstein-files",
                "label_confidence": "high",
            },
            {
                "topic": "epstein",
                "cluster_id": "c1",
                "frame_label": "supportive",
                "event_guess": "epstein-files",
                "label_confidence": "medium",
            },
            {
                "topic": "epstein",
                "cluster_id": "c2",
                "frame_label": "critical",
                "event_guess": "epstein-files",
                "label_confidence": "high",
            },
        ],
    )

    summary = generate_frame_exposure_supply_tables(
        batch_dir=batch_dir,
        label_rows_path=labels_path,
        out_dir=tmp_path / "frame_tables",
    )

    assert summary["labeled_clusters"] == 2
    overall_rows = _read_csv(tmp_path / "frame_tables" / "frame_overall_exposure_vs_supply.csv")
    assert overall_rows == [
        {
            "event_guess": "epstein-files",
            "exposed_clusters": "1",
            "exposed_post_share": "1.0",
            "exposure_rows": "1",
            "exposure_rows_per_supply_post": "1.0",
            "frame_label": "critical",
            "label_row_count": "1",
            "supply_clusters": "1",
            "supply_posts": "1",
            "topic": "epstein",
            "unique_exposed_posts": "1",
        },
        {
            "event_guess": "epstein-files",
            "exposed_clusters": "1",
            "exposed_post_share": "1.0",
            "exposure_rows": "3",
            "exposure_rows_per_supply_post": "1.5",
            "frame_label": "supportive",
            "label_row_count": "2",
            "supply_clusters": "1",
            "supply_posts": "2",
            "topic": "epstein",
            "unique_exposed_posts": "2",
        },
    ]

    cluster_rows = _read_csv(tmp_path / "frame_tables" / "cluster_label_summary.csv")
    assert cluster_rows[0]["frame_label"] == "supportive"
    assert cluster_rows[0]["label_confidence"] == "high"
    assert cluster_rows[0]["label_row_count"] == "2"


def test_run_rq2_pipeline_can_resume_from_existing_batch_and_annotations(tmp_path: Path) -> None:
    out_dir = tmp_path / "rq2"
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

    annotation_dir = out_dir / "annotations"
    _write_csv(
        annotation_dir / "coder1_annotations.csv",
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

    summary = run_rq2_pipeline(
        Rq2PipelineConfig(
            data_root=tmp_path / "data_v2_full",
            out_dir=out_dir,
            annotation_dir=annotation_dir,
            preset="politics_v1",
            topics=("epstein",),
            run_topic_batch=False,
            run_sampling=False,
            run_label_application=False,
            run_annotation_merge=True,
            run_frame_table=True,
        )
    )

    assert summary["steps"]["topic_batch"]["status"] == "skipped"
    assert summary["steps"]["annotation_merge"]["status"] == "completed"
    assert summary["steps"]["frame_tables"]["status"] == "completed"
    overall_rows = _read_csv(out_dir / "frame_tables" / "frame_overall_exposure_vs_supply.csv")
    assert len(overall_rows) == 1
    assert overall_rows[0]["frame_label"] == "supportive"
