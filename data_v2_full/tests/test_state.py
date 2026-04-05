from __future__ import annotations

from pathlib import Path

from bsky_collector_v2.state import ControlState
from bsky_collector_v2.types import PostUri
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

