from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

from bsky_collector_v2.content_bias import build_post_index, cluster_topic_probe


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def test_build_post_index_inserts_posts(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    parts = data_root / "hourly" / "2026-03-01" / "00" / "parts"
    _write_csv(
        parts / "posts_first_seen_part_000.csv",
        [
            {
                "run_id": "r1",
                "snapshot_hour_utc": "2026-03-01T00:00:00Z",
                "captured_at_utc": "2026-03-01T00:10:00Z",
                "viewer_mode": "unauth",
                "vantage_id": "unauth_enUS",
                "feed_uri": "feed://one",
                "bucket": "popular_by_likecount",
                "post_uri": "post://one",
                "post_cid": "cid-1",
                "author_did": "did:author:1",
                "author_handle": "author1",
                "record_created_at": "2026-03-01T00:05:00Z",
                "indexed_at": "2026-03-01T00:05:10Z",
                "text": "Trump and Epstein are back in the news https://example.com/story",
            }
        ],
    )
    out_db = tmp_path / "post_index.sqlite"
    summary = build_post_index(
        data_root=data_root,
        out_db=out_db,
        surfaces=["hourly"],
        start_date="2026-03-01",
        end_date="2026-03-01",
        include_labelerexp=False,
        overwrite=False,
    )
    assert summary["posts_inserted"] == 1

    conn = sqlite3.connect(out_db)
    try:
        row = conn.execute(
            "SELECT primary_domain, token_signature, text_norm FROM posts WHERE post_uri='post://one'"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] == "example.com"
    assert "epstein" in row[1]
    assert row[2].startswith("trump and epstein")


def test_cluster_topic_probe_groups_same_url_posts(tmp_path: Path) -> None:
    probe_dir = tmp_path / "probe"
    _write_csv(
        probe_dir / "matched_posts.csv",
        [
            {
                "post_uri": "post://one",
                "post_cid": "cid-1",
                "author_did": "did:author:1",
                "author_handle": "author1",
                "record_created_at": "2026-03-01T00:05:00Z",
                "indexed_at": "2026-03-01T00:05:10Z",
                "text": "Story A https://example.com/story",
                "matched_queries": "epstein",
                "branch": "main",
                "surface": "hourly",
                "date_utc": "2026-03-01",
                "hour_utc": "00",
                "viewer_mode": "unauth",
                "vantage_id": "unauth_enUS",
                "feed_uri": "feed://one",
                "bucket": "popular_by_likecount",
            },
            {
                "post_uri": "post://two",
                "post_cid": "cid-2",
                "author_did": "did:author:2",
                "author_handle": "author2",
                "record_created_at": "2026-03-01T03:05:00Z",
                "indexed_at": "2026-03-01T03:05:10Z",
                "text": "Story B https://example.com/story",
                "matched_queries": "epstein",
                "branch": "main",
                "surface": "hourly",
                "date_utc": "2026-03-01",
                "hour_utc": "03",
                "viewer_mode": "auth",
                "vantage_id": "auth_enUS",
                "feed_uri": "feed://two",
                "bucket": "longtail_random",
            },
            {
                "post_uri": "post://three",
                "post_cid": "cid-3",
                "author_did": "did:author:3",
                "author_handle": "author3",
                "record_created_at": "2026-03-02T03:05:00Z",
                "indexed_at": "2026-03-02T03:05:10Z",
                "text": "Different event https://example.com/other",
                "matched_queries": "epstein",
                "branch": "main",
                "surface": "hourly",
                "date_utc": "2026-03-02",
                "hour_utc": "03",
                "viewer_mode": "auth",
                "vantage_id": "auth_enUS",
                "feed_uri": "feed://three",
                "bucket": "longtail_random",
            },
        ],
    )
    _write_csv(
        probe_dir / "matched_feed_items.csv",
        [
            {
                "branch": "main",
                "surface": "hourly",
                "date_utc": "2026-03-01",
                "hour_utc": "00",
                "run_id": "r1",
                "snapshot_hour_utc": "2026-03-01T00:00:00Z",
                "captured_at_utc": "2026-03-01T00:10:00Z",
                "viewer_mode": "unauth",
                "vantage_id": "unauth_enUS",
                "feed_uri": "feed://one",
                "bucket": "popular_by_likecount",
                "rank": "1",
                "post_uri": "post://one",
                "post_cid": "cid-1",
                "author_did": "did:author:1",
                "author_handle": "author1",
                "reason_type": "",
                "reason_actor_did": "",
            },
            {
                "branch": "main",
                "surface": "hourly",
                "date_utc": "2026-03-01",
                "hour_utc": "03",
                "run_id": "r2",
                "snapshot_hour_utc": "2026-03-01T03:00:00Z",
                "captured_at_utc": "2026-03-01T03:10:00Z",
                "viewer_mode": "auth",
                "vantage_id": "auth_enUS",
                "feed_uri": "feed://two",
                "bucket": "longtail_random",
                "rank": "2",
                "post_uri": "post://two",
                "post_cid": "cid-2",
                "author_did": "did:author:2",
                "author_handle": "author2",
                "reason_type": "",
                "reason_actor_did": "",
            },
        ],
    )

    summary = cluster_topic_probe(
        probe_dir=probe_dir,
        out_dir=tmp_path / "clusters",
        time_window_hours=12,
        min_cluster_size=2,
        allowed_anchor_kinds=["url"],
        exclude_text_patterns=[],
    )
    assert summary["clusters_retained"] == 1

    with (tmp_path / "clusters" / "cluster_summary.csv").open("r", newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 1
    assert rows[0]["post_n"] == "2"
    assert rows[0]["exposure_rows"] == "2"
    assert rows[0]["unique_feeds"] == "2"
