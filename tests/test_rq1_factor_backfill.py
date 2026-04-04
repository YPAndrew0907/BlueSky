from __future__ import annotations

import csv
import json
import subprocess
import time
import sys
from pathlib import Path
from typing import Any

from bsky_collector_v2.jobs.backfill_rq1_factors import _walk_thread
from bsky_collector_v2.layout import Layout
from bsky_collector_v2.state import ControlState
from bsky_collector_v2.time_utils import now_utc, utc_date_str
from fake_bsky_rq1_server import FakeBskyRq1Config, FakeBskyRq1Server


def _run(cmd: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )


def _seed_posts_and_appearances(out_base: Path, post_uris: list[str]) -> None:
    layout = Layout(out_base)
    layout.control_root.mkdir(parents=True, exist_ok=True)
    with ControlState.open(layout.control_db_path) as control:
        control.upsert_post_registry_many(post_uris=post_uris, seen_at_utc="2026-03-31T00:00:00Z")
        control.commit()

    hourly_parts = out_base / "hourly" / "2026-03-31" / "00" / "parts"
    hourly_parts.mkdir(parents=True, exist_ok=True)
    wide_parts = out_base / "wide" / "2026-03-31" / "parts"
    wide_parts.mkdir(parents=True, exist_ok=True)
    micro_parts = out_base / "micro5" / "studyA" / "micro5_core_full" / "2026-03-31" / "00" / "05" / "parts"
    micro_parts.mkdir(parents=True, exist_ok=True)

    header = [
        "sample_family",
        "study_id",
        "panel_hash",
        "panel_version_id",
        "snapshot_hour_utc",
        "scheduled_window_start_utc",
        "scheduled_window_end_utc",
        "window_index",
        "window_minute",
        "window_minutes",
        "randomization_seed",
        "shard_id",
        "shard_count",
        "shard_membership_hash",
        "captured_at_utc",
        "request_order_in_window",
        "request_order_in_sweep",
        "viewer_mode",
        "vantage_id",
        "surface_type",
        "surface_id",
        "labelers_requested",
        "labelers_included",
        "feed_uri",
        "bucket",
        "page_no",
        "cursor_in",
        "cursor_out",
        "slot_no",
        "rank",
        "rank_approx",
        "post_uri",
        "post_cid",
        "post_indexed_at",
        "author_did",
        "author_handle",
        "reason_type",
        "reason_actor_did",
        "reason_actor_handle",
        "reason_repost_uri",
        "reason_repost_cid",
        "reason_repost_indexed_at",
        "reply_root_uri",
        "reply_parent_uri",
        "reply_grandparent_author_did",
        "feed_context",
        "req_id",
    ]
    rows = []
    for idx, post_uri in enumerate(post_uris):
        author_did = post_uri.removeprefix("at://").split("/")[0]
        rows.append(
            {
                "sample_family": "test_family",
                "study_id": "study0",
                "panel_hash": "panelhash",
                "panel_version_id": "panelv1",
                "snapshot_hour_utc": "2026-03-31T00:00:00Z",
                "scheduled_window_start_utc": "2026-03-31T00:00:00Z",
                "scheduled_window_end_utc": "2026-03-31T00:05:00Z",
                "window_index": str(idx),
                "window_minute": "0",
                "window_minutes": "5",
                "randomization_seed": "7",
                "shard_id": "0",
                "shard_count": "1",
                "shard_membership_hash": "hash",
                "captured_at_utc": "2026-03-31T00:00:30Z",
                "request_order_in_window": str(idx + 1),
                "request_order_in_sweep": str(idx + 1),
                "viewer_mode": "unauth",
                "vantage_id": "unauth",
                "surface_type": "feed",
                "surface_id": "surface0",
                "labelers_requested": "did:plc:labeler000",
                "labelers_included": "did:plc:labeler000",
                "feed_uri": f"at://did:plc:feedowner{idx}/app.bsky.feed.generator/feed{idx}",
                "bucket": "popular",
                "page_no": "1",
                "cursor_in": "",
                "cursor_out": "",
                "slot_no": str(idx + 1),
                "rank": str(idx + 1),
                "rank_approx": str(idx + 1),
                "post_uri": post_uri,
                "post_cid": f"cid-{idx}",
                "post_indexed_at": "2026-03-31T00:00:00Z",
                "author_did": author_did,
                "author_handle": f"author{idx}.test",
                "reason_type": "app.bsky.feed.defs#reasonRepost",
                "reason_actor_did": f"did:plc:reasonactor{idx}",
                "reason_actor_handle": f"reasonactor{idx}.test",
                "reason_repost_uri": f"at://did:plc:reasonactor{idx}/app.bsky.feed.repost/repost{idx}",
                "reason_repost_cid": f"repost-cid-{idx}",
                "reason_repost_indexed_at": "2026-03-31T00:00:00Z",
                "reply_root_uri": f"at://did:plc:root{idx}/app.bsky.feed.post/root{idx}",
                "reply_parent_uri": f"at://did:plc:parent{idx}/app.bsky.feed.post/parent{idx}",
                "reply_grandparent_author_did": f"did:plc:grand{idx}",
                "feed_context": f"ctx-{idx}",
                "req_id": f"req-{idx}",
            }
        )

    for path in (
        hourly_parts / "feed_items_part_000.csv",
        wide_parts / "feed_items_part_000.csv",
        micro_parts / "feed_items_part_000.csv",
    ):
        with open(path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=header)
            writer.writeheader()
            writer.writerows(rows)


def _thread_node(uri: str) -> dict[str, Any]:
    author_did = uri.removeprefix("at://").split("/")[0]
    slug = author_did.replace("did:plc:", "")
    return {
        "$type": "app.bsky.feed.defs#threadViewPost",
        "post": {
            "uri": uri,
            "cid": f"cid-{slug}",
            "author": {
                "did": author_did,
                "handle": f"{slug}.test",
            },
            "record": {
                "text": f"post for {slug}",
                "createdAt": "2026-03-31T00:00:00Z",
            },
            "indexedAt": "2026-03-31T00:00:00Z",
        },
    }


def _walk_thread_rows(node: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    focus_post_uri = str(node["post"]["uri"])
    out_nodes: list[dict[str, Any]] = []
    out_edges: list[dict[str, Any]] = []
    _walk_thread(
        focus_post_uri=focus_post_uri,
        node=node,
        relation_to_focus="focus",
        distance=0,
        run_id="rq1-walk-test",
        vantage_id="unauth",
        captured_at_utc="2026-03-31T00:00:00Z",
        actor_scopes={},
        labeler_dids=set(),
        out_nodes=out_nodes,
        out_edges=out_edges,
        seen_post_uris=set(),
    )
    return out_nodes, out_edges


def test_walk_thread_handles_deep_parent_chain() -> None:
    focus_uri = "at://did:plc:focus/app.bsky.feed.post/focus"
    focus = _thread_node(focus_uri)
    current = focus
    for idx in range(1200):
        parent = _thread_node(f"at://did:plc:parent{idx:04d}/app.bsky.feed.post/parent{idx:04d}")
        current["parent"] = parent
        current = parent

    out_nodes, out_edges = _walk_thread_rows(focus)

    assert len(out_nodes) == 1201
    assert len(out_edges) == 1200
    assert max(int(row["distance_to_focus"]) for row in out_nodes) == 1200
    assert {row["post_uri"] for row in out_nodes} == {focus_uri} | {
        f"at://did:plc:parent{idx:04d}/app.bsky.feed.post/parent{idx:04d}" for idx in range(1200)
    }


def test_walk_thread_handles_revisited_focus_without_infinite_recursion() -> None:
    focus_uri = "at://did:plc:focus/app.bsky.feed.post/focus"
    parent_uri = "at://did:plc:parent/app.bsky.feed.post/parent"
    focus = _thread_node(focus_uri)
    parent = _thread_node(parent_uri)
    focus["parent"] = parent
    parent["replies"] = [_thread_node(focus_uri)]

    out_nodes, out_edges = _walk_thread_rows(focus)

    assert [row["post_uri"] for row in out_nodes].count(focus_uri) == 1
    assert [row["post_uri"] for row in out_nodes].count(parent_uri) == 1
    assert any(
        edge["parent_post_uri"] == parent_uri and edge["child_post_uri"] == focus_uri
        for edge in out_edges
    )


def test_backfill_rq1_factors_full_integration(tmp_path: Path) -> None:
    post_uris = [
        "at://did:plc:author000/app.bsky.feed.post/post000",
        "at://did:plc:author001/app.bsky.feed.post/post001",
    ]
    _seed_posts_and_appearances(tmp_path, post_uris)

    with FakeBskyRq1Server() as server:
        cmd = [
            sys.executable,
            "-m",
            "bsky_collector_v2",
            "backfill-rq1-factors",
            "--out-base",
            str(tmp_path),
            "--appview-host",
            server.base_url,
            "--pds-host",
            server.base_url,
            "--relay-host",
            server.base_url,
            "--max-posts",
            "2",
            "--batch-size",
            "2",
            "--max-items-per-endpoint",
            "2",
            "--max-author-feed-items",
            "2",
            "--max-followers-per-actor",
            "2",
            "--max-follows-per-actor",
            "2",
            "--max-follow-records-per-actor",
            "2",
            "--max-actor-feeds-per-actor",
            "2",
            "--max-lists-per-actor",
            "1",
            "--max-list-members-per-list",
            "2",
            "--max-starter-packs-per-actor",
            "1",
            "--no-resolve-pds-endpoints",
        ]
        res = _run(cmd, cwd=Path.cwd())
        assert res.returncode == 0, res.stdout

        run_root = tmp_path / "rq1_factors" / utc_date_str(now_utc()) / "shard_000"
        assert run_root.exists(), sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))

        expected_files = [
            "post_surface_appearances_part_000.csv",
            "post_views_part_000.csv",
            "post_likes_part_000.csv",
            "post_quotes_part_000.csv",
            "post_reposted_by_part_000.csv",
            "relationship_edges_part_000.csv",
            "actor_profiles_part_000.csv",
            "thread_nodes_part_000.csv",
            "thread_edges_part_000.csv",
            "followers_edges_part_000.csv",
            "follows_edges_part_000.csv",
            "follow_records_part_000.csv",
            "repo_descriptions_part_000.csv",
            "author_feed_items_part_000.csv",
            "feed_generators_part_000.csv",
            "actor_lists_part_000.csv",
            "list_members_part_000.csv",
            "actor_starter_packs_part_000.csv",
            "starter_pack_contents_part_000.csv",
            "labeler_services_part_000.csv",
            "post_rq1_summary_part_000.csv",
        ]
        for name in expected_files:
            path = run_root / name
            assert path.exists(), name
            with open(path, "r", encoding="utf-8", newline="") as f:
                rows = list(csv.DictReader(f))
            assert rows, name

        progress = json.loads((run_root / "progress.json").read_text(encoding="utf-8"))
        assert progress["unit_label"] == "posts"
        assert progress["feeds_total"] == 2
        assert progress["feeds_done"] == 2
        assert progress["details"]["selection_order"] == "oldest_first"
        assert progress["details"]["selected_first_seen_min_utc"] == "2026-03-31T00:00:00Z"
        assert progress["details"]["selected_first_seen_max_utc"] == "2026-03-31T00:00:00Z"
        assert progress["details"]["phase"] == "complete"

        manifest = json.loads((run_root / "run_manifest.json").read_text(encoding="utf-8"))
        assert manifest["selection"]["selected_posts"] == 2
        assert manifest["selection"]["selected_first_seen_min_utc"] == "2026-03-31T00:00:00Z"
        assert manifest["selection"]["selected_first_seen_max_utc"] == "2026-03-31T00:00:00Z"
        assert manifest["effective_limits"]["max_items_per_endpoint"] == 2
        assert manifest["effective_limits"]["max_author_feed_items"] == 2
        assert manifest["effective_limits"]["max_lists_per_actor"] == 1

        with open(run_root / "post_views_part_000.csv", "r", encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 2
        assert rows[0]["raw_json"]
        assert rows[0]["record_json"]
        assert rows[0]["embed_json"]
        assert rows[0]["contains_no_unauthenticated"] == "1"
        assert rows[0]["contains_hide_like_label"] == "1"

        with open(run_root / "post_surface_appearances_part_000.csv", "r", encoding="utf-8", newline="") as f:
            appearance_rows = list(csv.DictReader(f))
        assert len(appearance_rows) >= 3
        assert {r["source_family"] for r in appearance_rows} == {"hourly", "wide", "micro5"}

        with open(run_root / "list_members_part_000.csv", "r", encoding="utf-8", newline="") as f:
            list_member_rows = list(csv.DictReader(f))
        assert any(str(r.get("list_uri") or "").endswith("pack0-list") for r in list_member_rows)

        first_post_view_count = len(rows)
        first_surface_count = len(appearance_rows)

        res2 = _run(cmd, cwd=Path.cwd())
        assert res2.returncode == 0, res2.stdout

        with open(run_root / "post_views_part_000.csv", "r", encoding="utf-8", newline="") as f:
            rows_after = list(csv.DictReader(f))
        with open(run_root / "post_surface_appearances_part_000.csv", "r", encoding="utf-8", newline="") as f:
            appearance_rows_after = list(csv.DictReader(f))
        assert len(rows_after) == first_post_view_count
        assert len(appearance_rows_after) == first_surface_count

        called_paths = {entry["path"] for entry in server.request_log}
        for required in {
            "/xrpc/app.bsky.feed.getPosts",
            "/xrpc/app.bsky.feed.getLikes",
            "/xrpc/app.bsky.feed.getQuotes",
            "/xrpc/app.bsky.feed.getRepostedBy",
            "/xrpc/app.bsky.feed.getPostThread",
            "/xrpc/app.bsky.graph.getFollowers",
            "/xrpc/app.bsky.graph.getFollows",
            "/xrpc/app.bsky.graph.getRelationships",
            "/xrpc/com.atproto.repo.describeRepo",
            "/xrpc/com.atproto.repo.listRecords",
            "/xrpc/app.bsky.feed.getAuthorFeed",
            "/xrpc/app.bsky.feed.getActorFeeds",
            "/xrpc/app.bsky.graph.getLists",
            "/xrpc/app.bsky.graph.getList",
            "/xrpc/app.bsky.graph.getActorStarterPacks",
            "/xrpc/app.bsky.graph.getStarterPack",
            "/xrpc/app.bsky.feed.getFeedGenerator",
            "/xrpc/app.bsky.labeler.getServices",
        }:
            assert required in called_paths, required


def test_backfill_rq1_factors_actor_phase_uses_concurrency(tmp_path: Path) -> None:
    post_uris = [
        "at://did:plc:author000/app.bsky.feed.post/post000",
        "at://did:plc:author001/app.bsky.feed.post/post001",
        "at://did:plc:author002/app.bsky.feed.post/post002",
        "at://did:plc:author003/app.bsky.feed.post/post003",
    ]

    delayed_paths = {
        "/xrpc/app.bsky.graph.getRelationships": 0.04,
        "/xrpc/app.bsky.graph.getFollowers": 0.04,
        "/xrpc/app.bsky.graph.getFollows": 0.04,
        "/xrpc/app.bsky.feed.getAuthorFeed": 0.04,
        "/xrpc/app.bsky.feed.getActorFeeds": 0.04,
        "/xrpc/app.bsky.graph.getLists": 0.04,
        "/xrpc/app.bsky.graph.getActorStarterPacks": 0.04,
        "/xrpc/com.atproto.repo.describeRepo": 0.04,
        "/xrpc/com.atproto.repo.listRecords": 0.04,
        "/xrpc/app.bsky.feed.getFeedGenerator": 0.04,
        "/xrpc/app.bsky.graph.getList": 0.04,
        "/xrpc/app.bsky.graph.getStarterPack": 0.04,
    }

    def _cmd(out_base: Path, concurrency: int, server_base_url: str) -> list[str]:
        return [
            sys.executable,
            "-m",
            "bsky_collector_v2",
            "backfill-rq1-factors",
            "--out-base",
            str(out_base),
            "--appview-host",
            server_base_url,
            "--pds-host",
            server_base_url,
            "--relay-host",
            server_base_url,
            "--rps",
            "500",
            "--concurrency",
            str(concurrency),
            "--max-posts",
            "4",
            "--batch-size",
            "4",
            "--max-items-per-endpoint",
            "2",
            "--max-author-feed-items",
            "2",
            "--max-followers-per-actor",
            "2",
            "--max-follows-per-actor",
            "2",
            "--max-follow-records-per-actor",
            "2",
            "--max-actor-feeds-per-actor",
            "2",
            "--max-lists-per-actor",
            "1",
            "--max-list-members-per-list",
            "2",
            "--max-starter-packs-per-actor",
            "1",
            "--no-resolve-pds-endpoints",
        ]

    out_base_slow = tmp_path / "slow"
    out_base_fast = tmp_path / "fast"
    _seed_posts_and_appearances(out_base_slow, post_uris)
    _seed_posts_and_appearances(out_base_fast, post_uris)

    with FakeBskyRq1Server(FakeBskyRq1Config(delay_by_path_s=delayed_paths)) as slow_server:
        t0 = time.monotonic()
        slow = _run(_cmd(out_base_slow, 1, slow_server.base_url), cwd=Path.cwd())
        slow_elapsed = time.monotonic() - t0
        assert slow.returncode == 0, slow.stdout

    with FakeBskyRq1Server(FakeBskyRq1Config(delay_by_path_s=delayed_paths)) as fast_server:
        t0 = time.monotonic()
        fast = _run(_cmd(out_base_fast, 8, fast_server.base_url), cwd=Path.cwd())
        fast_elapsed = time.monotonic() - t0
        assert fast.returncode == 0, fast.stdout

    assert fast_elapsed < slow_elapsed * 0.8, (slow_elapsed, fast_elapsed)
