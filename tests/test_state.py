from __future__ import annotations

import sqlite3
from pathlib import Path

from bsky_collector_v2.state import ControlState, SelectedPost, coerce_selected_post_rows
from bsky_collector_v2.types import FeedUri, PostUri
from bsky_collector_v2.time_utils import format_utc, now_utc


def test_control_state_wal_and_post_registry(tmp_path: Path) -> None:
    db_path = tmp_path / "control" / "control_state.db"
    ts = format_utc(now_utc())

    with ControlState.open(db_path) as s:
        row = s.conn.execute("PRAGMA journal_mode;").fetchone()
        assert row is not None
        assert str(row[0]).lower() == "wal"

        posts = [PostUri("at://did:plc:abc/app.bsky.feed.post/1"), PostUri("at://did:plc:abc/app.bsky.feed.post/1")]
        s.upsert_post_registry_many(post_uris=posts, seen_at_utc=ts)
        s.commit()

        row2 = s.conn.execute(
            "SELECT seen_count, first_written FROM post_registry WHERE post_uri=?",
            (str(posts[0]),),
        ).fetchone()
        assert row2 is not None
        assert int(row2["seen_count"]) == 2
        assert int(row2["first_written"]) == 0

        not_written = s.select_not_written(post_uris=[posts[0]])
        assert not_written == [posts[0]]

        s.mark_first_written(post_uris=not_written)
        s.commit()

        not_written2 = s.select_not_written(post_uris=[posts[0]])
        assert not_written2 == []




def test_control_state_interaction_and_feed_hydration_helpers(tmp_path: Path) -> None:
    db_path = tmp_path / "control" / "control_state.db"
    ts = format_utc(now_utc())
    post = PostUri("at://did:plc:abc/app.bsky.feed.post/2")

    with ControlState.open(db_path) as s:
        s.upsert_post_registry_many(post_uris=[post], seen_at_utc=ts)
        s.ensure_post_interaction_tasks(post_uris=[post], enqueued_at_utc=ts)
        s.upsert_feed_catalog(
            feed_uri=FeedUri("at://did:plc:feed/app.bsky.feed.generator/test"),
            creator_did="did:plc:feed",
            service_did="did:web:example.com",
            provider_domain="example.com",
            like_count_last=1,
            discovered_from=["test"],
            seen_at_utc=ts,
        )
        s.commit()

        selected_post_rows = s.select_posts_to_backfill_rows(limit=10)
        assert [(row.post_uri, row.first_seen_utc) for row in selected_post_rows] == [(str(post), ts)]

        selected_posts = s.select_posts_to_backfill(limit=10)
        assert selected_posts == [str(post)]

        s.mark_posts_interactions_hydrated(post_uris=[post], hydrated_at_utc=ts)
        s.commit()
        selected_posts_after = s.select_posts_to_backfill(limit=10)
        assert selected_posts_after == []

        selected_feeds = s.select_feed_generators_to_hydrate(limit=10)
        assert selected_feeds == ["at://did:plc:feed/app.bsky.feed.generator/test"]

        s.mark_feed_generators_hydrated(
            feed_uris=[FeedUri("at://did:plc:feed/app.bsky.feed.generator/test")],
            hydrated_at_utc=ts,
        )
        s.commit()
        selected_feeds_after = s.select_feed_generators_to_hydrate(limit=10)
        assert selected_feeds_after == []


def test_control_state_rq1_hydration_helpers(tmp_path: Path) -> None:
    db_path = tmp_path / "control" / "control_state.db"
    post_a = PostUri("at://did:plc:aaa/app.bsky.feed.post/a")
    post_b = PostUri("at://did:plc:bbb/app.bsky.feed.post/b")
    post_c = PostUri("at://did:plc:ccc/app.bsky.feed.post/c")

    with ControlState.open(db_path) as s:
        s.upsert_post_registry_many(post_uris=[post_a], seen_at_utc="2026-03-01T00:00:00Z")
        s.upsert_post_registry_many(post_uris=[post_b], seen_at_utc="2026-03-02T00:00:00Z")
        s.upsert_post_registry_many(post_uris=[post_c], seen_at_utc="2026-03-03T00:00:00Z")
        s.ensure_post_rq1_factor_tasks(
            post_uris=[post_a, post_b, post_c],
            enqueued_at_utc="2026-03-03T01:00:00Z",
        )
        s.commit()

        selected_rows = s.select_posts_to_backfill_rq1_rows(limit=10)
        assert [(row.post_uri, row.first_seen_utc) for row in selected_rows] == [
            (str(post_a), "2026-03-01T00:00:00Z"),
            (str(post_b), "2026-03-02T00:00:00Z"),
            (str(post_c), "2026-03-03T00:00:00Z"),
        ]

        selected = s.select_posts_to_backfill_rq1(limit=10)
        assert selected == [str(post_a), str(post_b), str(post_c)]

        s.mark_posts_rq1_factors_hydrated(
            post_uris=[post_b],
            hydrated_at_utc="2026-03-04T00:00:00Z",
            stage="core",
        )
        s.commit()

        selected_after = s.select_posts_to_backfill_rq1(limit=10)
        assert selected_after == [str(post_a), str(post_b), str(post_c)]

        selected_after_core = s.select_posts_to_backfill_rq1(limit=10, stage="core")
        assert selected_after_core == [str(post_a), str(post_c)]

        selected_graph = s.select_posts_to_backfill_rq1(limit=10, stage="graph")
        assert selected_graph == [str(post_b)]

        s.mark_posts_rq1_factors_hydrated(
            post_uris=[post_b],
            hydrated_at_utc="2026-03-05T00:00:00Z",
            stage="graph",
        )
        s.commit()

        selected_repo = s.select_posts_to_backfill_rq1(limit=10, stage="repo")
        assert selected_repo == [str(post_b)]

        s.mark_posts_rq1_factors_hydrated(
            post_uris=[post_b],
            hydrated_at_utc="2026-03-06T00:00:00Z",
            stage="repo",
        )
        s.commit()

        selected_after = s.select_posts_to_backfill_rq1(limit=10)
        assert selected_after == [str(post_a), str(post_c)]

        selected_with_hydrated = s.select_posts_to_backfill_rq1(limit=10, include_hydrated=True)
        assert selected_with_hydrated == [str(post_a), str(post_b), str(post_c)]

        stage_row = s.conn.execute(
            """
            SELECT core_hydrated_at_utc, graph_hydrated_at_utc, repo_hydrated_at_utc, last_hydrated_utc
            FROM post_rq1_factor_registry
            WHERE post_uri=?
            """,
            (str(post_b),),
        ).fetchone()
        assert stage_row is not None
        assert str(stage_row["core_hydrated_at_utc"]) == "2026-03-06T00:00:00Z"
        assert str(stage_row["graph_hydrated_at_utc"]) == "2026-03-06T00:00:00Z"
        assert str(stage_row["repo_hydrated_at_utc"]) == "2026-03-06T00:00:00Z"
        assert str(stage_row["last_hydrated_utc"]) == "2026-03-06T00:00:00Z"

        shard0 = set(s.select_posts_to_backfill_rq1(limit=10, include_hydrated=True, shard_index=0, shard_count=2))
        shard1 = set(s.select_posts_to_backfill_rq1(limit=10, include_hydrated=True, shard_index=1, shard_count=2))
        assert shard0.isdisjoint(shard1)
        assert shard0 | shard1 == {str(post_a), str(post_b), str(post_c)}


def test_control_state_backfill_indexes_exist(tmp_path: Path) -> None:
    db_path = tmp_path / "control" / "control_state.db"

    with ControlState.open(db_path) as s:
        post_registry_indexes = {
            str(row[1]) for row in s.conn.execute("PRAGMA index_list(post_registry)").fetchall()
        }
        interaction_indexes = {
            str(row[1]) for row in s.conn.execute("PRAGMA index_list(post_interaction_registry)").fetchall()
        }
        rq1_indexes = {
            str(row[1]) for row in s.conn.execute("PRAGMA index_list(post_rq1_factor_registry)").fetchall()
        }

    assert "idx_post_registry_first_seen_post_uri" in post_registry_indexes
    assert "idx_post_interaction_registry_last_hydrated_post_uri" in interaction_indexes
    assert "idx_post_rq1_factor_registry_last_hydrated_post_uri" in rq1_indexes
    assert "idx_post_rq1_factor_registry_core_hydrated_post_uri" in rq1_indexes
    assert "idx_post_rq1_factor_registry_graph_hydrated_post_uri" in rq1_indexes
    assert "idx_post_rq1_factor_registry_repo_hydrated_post_uri" in rq1_indexes


def test_control_state_rq1_stage_columns_backfill_from_legacy_last_hydrated(tmp_path: Path) -> None:
    db_path = tmp_path / "control" / "control_state.db"
    post_uri = "at://did:plc:legacy/app.bsky.feed.post/one"

    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as raw_conn:
        raw_conn.executescript(
            """
            CREATE TABLE post_registry (
              post_uri TEXT PRIMARY KEY,
              first_seen_utc TEXT NOT NULL,
              last_seen_utc TEXT NOT NULL,
              seen_count INTEGER NOT NULL,
              first_written INTEGER NOT NULL
            );

            CREATE TABLE post_rq1_factor_registry (
              post_uri TEXT PRIMARY KEY,
              first_enqueued_utc TEXT NOT NULL,
              last_hydrated_utc TEXT,
              hydrated_count INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        raw_conn.execute(
            """
            INSERT INTO post_registry(post_uri, first_seen_utc, last_seen_utc, seen_count, first_written)
            VALUES (?, ?, ?, 1, 0)
            """,
            (post_uri, "2026-03-01T00:00:00Z", "2026-03-01T00:00:00Z"),
        )
        raw_conn.execute(
            """
            INSERT INTO post_rq1_factor_registry(post_uri, first_enqueued_utc, last_hydrated_utc, hydrated_count)
            VALUES (?, ?, ?, 1)
            """,
            (post_uri, "2026-03-01T00:00:00Z", "2026-03-02T00:00:00Z"),
        )
        raw_conn.commit()

    with ControlState.open(db_path) as s2:
        row = s2.conn.execute(
            """
            SELECT core_hydrated_at_utc, graph_hydrated_at_utc, repo_hydrated_at_utc, last_hydrated_utc
            FROM post_rq1_factor_registry
            WHERE post_uri=?
            """,
            (post_uri,),
        ).fetchone()
        assert row is not None
        assert str(row["core_hydrated_at_utc"]) == "2026-03-02T00:00:00Z"
        assert str(row["graph_hydrated_at_utc"]) == "2026-03-02T00:00:00Z"
        assert str(row["repo_hydrated_at_utc"]) == "2026-03-02T00:00:00Z"
        assert str(row["last_hydrated_utc"]) == "2026-03-02T00:00:00Z"


def test_coerce_selected_post_rows_accepts_rpc_dicts() -> None:
    rows = coerce_selected_post_rows(
        [
            {"post_uri": "at://did:plc:aaa/app.bsky.feed.post/a", "first_seen_utc": "2026-03-01T00:00:00Z"},
            SelectedPost(
                post_uri="at://did:plc:bbb/app.bsky.feed.post/b",
                first_seen_utc="2026-03-02T00:00:00Z",
            ),
        ]
    )

    assert rows == [
        SelectedPost(post_uri="at://did:plc:aaa/app.bsky.feed.post/a", first_seen_utc="2026-03-01T00:00:00Z"),
        SelectedPost(post_uri="at://did:plc:bbb/app.bsky.feed.post/b", first_seen_utc="2026-03-02T00:00:00Z"),
    ]
