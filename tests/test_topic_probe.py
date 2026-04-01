from __future__ import annotations

import csv
import json
from pathlib import Path

from bsky_collector_v2.topic_probe import run_probe


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def test_run_probe_matches_posts_and_exposure(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    run_parts = data_root / "hourly" / "2026-03-01" / "00" / "parts"
    _write_csv(
        run_parts / "posts_first_seen_part_000.csv",
        [
            {
                "run_id": "r1",
                "snapshot_hour_utc": "2026-03-01T00:00:00Z",
                "captured_at_utc": "2026-03-01T00:10:00Z",
                "viewer_mode": "unauth",
                "vantage_id": "unauth_enUS",
                "feed_uri": "feed://one",
                "bucket": "popular_by_likecount",
                "post_uri": "post://epstein-1",
                "post_cid": "cid-1",
                "author_did": "did:author:1",
                "author_handle": "author1",
                "record_created_at": "2026-03-01T00:05:00Z",
                "indexed_at": "2026-03-01T00:05:10Z",
                "text": "Trump and Epstein are back in the news",
            },
            {
                "run_id": "r1",
                "snapshot_hour_utc": "2026-03-01T00:00:00Z",
                "captured_at_utc": "2026-03-01T00:10:00Z",
                "viewer_mode": "unauth",
                "vantage_id": "unauth_enUS",
                "feed_uri": "feed://one",
                "bucket": "popular_by_likecount",
                "post_uri": "post://other-1",
                "post_cid": "cid-2",
                "author_did": "did:author:2",
                "author_handle": "author2",
                "record_created_at": "2026-03-01T00:05:00Z",
                "indexed_at": "2026-03-01T00:05:10Z",
                "text": "sports only",
            },
        ],
    )
    _write_csv(
        run_parts / "feed_items_part_000.csv",
        [
            {
                "run_id": "r1",
                "snapshot_hour_utc": "2026-03-01T00:00:00Z",
                "captured_at_utc": "2026-03-01T00:10:00Z",
                "viewer_mode": "unauth",
                "vantage_id": "unauth_enUS",
                "feed_uri": "feed://one",
                "bucket": "popular_by_likecount",
                "rank": "1",
                "post_uri": "post://epstein-1",
                "post_cid": "cid-1",
                "author_did": "did:author:1",
                "author_handle": "author1",
                "reason_type": "",
                "reason_actor_did": "",
            },
            {
                "run_id": "r1",
                "snapshot_hour_utc": "2026-03-01T00:00:00Z",
                "captured_at_utc": "2026-03-01T00:10:00Z",
                "viewer_mode": "auth",
                "vantage_id": "auth_enUS",
                "feed_uri": "feed://two",
                "bucket": "longtail_random",
                "rank": "7",
                "post_uri": "post://epstein-1",
                "post_cid": "cid-1",
                "author_did": "did:author:1",
                "author_handle": "author1",
                "reason_type": "",
                "reason_actor_did": "",
            },
        ],
    )
    _write_csv(
        run_parts / "post_labels_part_000.csv",
        [
            {
                "run_id": "r1",
                "snapshot_hour_utc": "2026-03-01T00:00:00Z",
                "captured_at_utc": "2026-03-01T00:10:00Z",
                "viewer_mode": "auth",
                "vantage_id": "auth_enUS",
                "post_uri": "post://epstein-1",
                "post_cid": "cid-1",
                "label_src": "did:labeler:1",
                "label_val": "politics",
                "label_neg": "",
                "label_uri": "post://epstein-1",
                "label_cts": "2026-03-01T00:09:00Z",
            }
        ],
    )
    _write_csv(
        run_parts / "post_metrics_part_000.csv",
        [
            {
                "run_id": "r1",
                "snapshot_hour_utc": "2026-03-01T00:00:00Z",
                "captured_at_utc": "2026-03-01T00:10:00Z",
                "viewer_mode": "auth",
                "vantage_id": "auth_enUS",
                "post_uri": "post://epstein-1",
                "like_count": "10",
                "repost_count": "2",
                "reply_count": "1",
                "quote_count": "0",
            }
        ],
    )

    out_dir = tmp_path / "out"
    summary = run_probe(
        data_root=data_root,
        out_dir=out_dir,
        queries=["epstein", "trump"],
        exclude_queries=[],
        surfaces=["hourly"],
        start_date="2026-03-01",
        end_date="2026-03-01",
        include_labelerexp=False,
    )

    assert summary["matched_posts"] == 1
    assert summary["matched_feed_rows"] == 2
    assert summary["matched_label_rows"] == 1
    assert summary["matched_metrics_rows"] == 1
    assert summary["exposure_by_viewer"] == {"unauth": 1, "auth": 1}
    assert summary["exposure_by_bucket"] == {"popular_by_likecount": 1, "longtail_random": 1}

    summary_path = out_dir / "summary.json"
    assert json.loads(summary_path.read_text())["matched_posts"] == 1

    matched_posts_path = out_dir / "matched_posts.csv"
    with matched_posts_path.open("r", newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert rows[0]["post_uri"] == "post://epstein-1"
    assert rows[0]["matched_queries"] == "epstein|trump"
