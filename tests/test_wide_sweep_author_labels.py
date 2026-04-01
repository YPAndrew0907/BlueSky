from __future__ import annotations

import asyncio
import csv
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from bsky_collector_v2.jobs import wide_sweep as ws
from bsky_collector_v2.progress import ProgressState
from bsky_collector_v2.state import ControlState
from bsky_collector_v2.types import FeedUri, RunId
from bsky_collector_v2.writers import CsvPartWriter


@dataclass
class _FakeXrpcResp:
    data: dict[str, Any]
    content_labelers: str | None = None


class _FakeHttp:
    def __init__(self, resp: _FakeXrpcResp) -> None:
        self._resp = resp
        self.hosts = SimpleNamespace(appview_host="https://example.invalid")

    async def xrpc_get(self, **_kwargs: Any) -> _FakeXrpcResp:
        return self._resp


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def test_wide_sweep_sweep_one_feed_no_author_labels(tmp_path: Path) -> None:
    control = ControlState.open_local(tmp_path / "control_state.db")
    parts_dir = tmp_path / "parts"
    writers = ws.WorkerWriters(
        feed_items=CsvPartWriter(parts_dir / "feed_items_part_000.csv", fieldnames=ws._FEED_ITEMS_FIELDS),
        posts_first_seen=CsvPartWriter(
            parts_dir / "posts_first_seen_part_000.csv", fieldnames=ws._POSTS_FIRST_SEEN_FIELDS
        ),
        post_metrics=CsvPartWriter(parts_dir / "post_metrics_part_000.csv", fieldnames=ws._POST_METRICS_FIELDS),
        post_labels=CsvPartWriter(parts_dir / "post_labels_part_000.csv", fieldnames=ws._POST_LABEL_FIELDS),
    )
    try:
        post_uri = "at://did:plc:author/app.bsky.feed.post/1"
        resp = _FakeXrpcResp(
            data={
                "feed": [
                    {
                        "post": {
                            "uri": post_uri,
                            "cid": "bafyreiaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                            "author": {
                                "did": "did:plc:author",
                                "handle": "author.test",
                                "labels": [
                                    {
                                        "src": "did:plc:labeler1",
                                        "val": "impersonation",
                                        "neg": False,
                                        "uri": "at://did:plc:labeler1/app.bsky.labeler.service/1",
                                        "cts": "2026-02-22T00:00:00Z",
                                    }
                                ],
                            },
                            "record": {"text": "hello", "createdAt": "2026-02-22T00:00:00Z"},
                            "indexedAt": "2026-02-22T00:00:00Z",
                            "likeCount": 1,
                            "repostCount": 0,
                            "replyCount": 0,
                            "quoteCount": 0,
                            "labels": [
                                {
                                    "src": "did:plc:labeler2",
                                    "val": "porn",
                                    "neg": False,
                                    "uri": "at://did:plc:labeler2/app.bsky.labeler.service/2",
                                    "cts": "2026-02-22T00:00:00Z",
                                }
                            ],
                        }
                    }
                ],
                "cursor": None,
            },
            content_labelers="did:plc:labeler1,did:plc:labeler2",
        )
        http = _FakeHttp(resp)
        progress = ProgressState(job_name="wide-sweep", run_id=RunId("r1"), started_at_utc="2026-02-22T00:00:00Z")
        labelers_included: set[str] = set()

        asyncio.run(
            ws._sweep_one_feed(
                http=http,
                control=control,
                writers=writers,
                run_id=RunId("r1"),
                feed_uri=FeedUri("at://did:plc:feed/app.bsky.feed.generator/x"),
                include_author_labels=False,
                posts_per_feed=20,
                captured_at_utc="2026-02-22T05:06:10Z",
                progress=progress,
                vantage_id="unauth_enUS",
                labelers_included=labelers_included,
            )
        )

        # Feed items/metrics/labels may still be buffered; force a flush before reading.
        writers.feed_items.flush(force_fsync=True)
        writers.post_metrics.flush(force_fsync=True)
        writers.post_labels.flush(force_fsync=True)
        writers.posts_first_seen.flush(force_fsync=True)

        assert len(_read_csv_rows(parts_dir / "feed_items_part_000.csv")) == 1
        assert len(_read_csv_rows(parts_dir / "post_metrics_part_000.csv")) == 1
        # Only post-level label should be written when include_author_labels=False.
        assert len(_read_csv_rows(parts_dir / "post_labels_part_000.csv")) == 1
        assert len(_read_csv_rows(parts_dir / "posts_first_seen_part_000.csv")) == 1
    finally:
        ws._close_worker_writers(writers)
        control.close()


def test_wide_sweep_sweep_one_feed_with_author_labels(tmp_path: Path) -> None:
    control = ControlState.open_local(tmp_path / "control_state.db")
    parts_dir = tmp_path / "parts"
    writers = ws.WorkerWriters(
        feed_items=CsvPartWriter(parts_dir / "feed_items_part_000.csv", fieldnames=ws._FEED_ITEMS_FIELDS),
        posts_first_seen=CsvPartWriter(
            parts_dir / "posts_first_seen_part_000.csv", fieldnames=ws._POSTS_FIRST_SEEN_FIELDS
        ),
        post_metrics=CsvPartWriter(parts_dir / "post_metrics_part_000.csv", fieldnames=ws._POST_METRICS_FIELDS),
        post_labels=CsvPartWriter(parts_dir / "post_labels_part_000.csv", fieldnames=ws._POST_LABEL_FIELDS),
    )
    try:
        post_uri = "at://did:plc:author/app.bsky.feed.post/1"
        resp = _FakeXrpcResp(
            data={
                "feed": [
                    {
                        "post": {
                            "uri": post_uri,
                            "cid": "bafyreiaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                            "author": {
                                "did": "did:plc:author",
                                "handle": "author.test",
                                "labels": [
                                    {
                                        "src": "did:plc:labeler1",
                                        "val": "impersonation",
                                        "neg": False,
                                        "uri": "at://did:plc:labeler1/app.bsky.labeler.service/1",
                                        "cts": "2026-02-22T00:00:00Z",
                                    }
                                ],
                            },
                            "record": {"text": "hello", "createdAt": "2026-02-22T00:00:00Z"},
                            "indexedAt": "2026-02-22T00:00:00Z",
                            "likeCount": 1,
                            "repostCount": 0,
                            "replyCount": 0,
                            "quoteCount": 0,
                            "labels": [
                                {
                                    "src": "did:plc:labeler2",
                                    "val": "porn",
                                    "neg": False,
                                    "uri": "at://did:plc:labeler2/app.bsky.labeler.service/2",
                                    "cts": "2026-02-22T00:00:00Z",
                                }
                            ],
                        }
                    }
                ],
                "cursor": None,
            },
            content_labelers="did:plc:labeler1,did:plc:labeler2",
        )
        http = _FakeHttp(resp)
        progress = ProgressState(job_name="wide-sweep", run_id=RunId("r1"), started_at_utc="2026-02-22T00:00:00Z")
        labelers_included: set[str] = set()

        asyncio.run(
            ws._sweep_one_feed(
                http=http,
                control=control,
                writers=writers,
                run_id=RunId("r1"),
                feed_uri=FeedUri("at://did:plc:feed/app.bsky.feed.generator/x"),
                include_author_labels=True,
                posts_per_feed=20,
                captured_at_utc="2026-02-22T05:06:10Z",
                progress=progress,
                vantage_id="unauth_enUS",
                labelers_included=labelers_included,
            )
        )

        writers.feed_items.flush(force_fsync=True)
        writers.post_metrics.flush(force_fsync=True)
        writers.post_labels.flush(force_fsync=True)
        writers.posts_first_seen.flush(force_fsync=True)

        assert len(_read_csv_rows(parts_dir / "feed_items_part_000.csv")) == 1
        assert len(_read_csv_rows(parts_dir / "post_metrics_part_000.csv")) == 1
        # Post label + author label
        assert len(_read_csv_rows(parts_dir / "post_labels_part_000.csv")) == 2
        assert len(_read_csv_rows(parts_dir / "posts_first_seen_part_000.csv")) == 1
    finally:
        ws._close_worker_writers(writers)
        control.close()
