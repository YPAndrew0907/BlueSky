from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from bsky_collector_v2.appearance_file_index import iter_matching_feed_item_rows
from bsky_collector_v2.did_resolver import DidResolver
from bsky_collector_v2.fs_utils import atomic_write_json, ensure_dir
from bsky_collector_v2.http_client import AsyncHttpClient, HttpError, HttpRetryConfig, XrpcHosts
from bsky_collector_v2.instrumentation import enrich_manifest
from bsky_collector_v2.layout import Layout
from bsky_collector_v2.progress import ProgressReporter, ProgressState
from bsky_collector_v2.public_views import extract_post_record_features, flatten_generator_view, json_compact
from bsky_collector_v2.request_provenance import JobRequestContextFactory, RequestProvenanceWriter
from bsky_collector_v2.rq1_stages import (
    normalize_rq1_stage,
    rq1_stage_includes_graph,
    rq1_stage_includes_repo,
)
from bsky_collector_v2.rq1_factor_views import (
    extract_label_src_dids,
    flatten_actor_view,
    flatten_author_feed_item,
    flatten_follow_record,
    flatten_labeler_service_view,
    flatten_list_item,
    flatten_list_view,
    flatten_profile_view_detailed,
    flatten_repo_description,
    flatten_starter_pack_view,
    flatten_thread_node,
)
from bsky_collector_v2.rq1_stage_store import Rq1StageStore
from bsky_collector_v2.state import ControlState, coerce_selected_post_rows
from bsky_collector_v2.time_utils import format_utc, now_utc, utc_date_str
from bsky_collector_v2.types import PostUri, RunId
from bsky_collector_v2.writers import CsvPartWriter

logger = logging.getLogger("bsky_collector_v2.job.backfill_rq1_factors")

_APPEARANCE_FIELDS: tuple[str, ...] = (
    "run_id",
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
    "source_family",
    "source_path",
)

_POST_VIEW_FIELDS: tuple[str, ...] = (
    "run_id",
    "vantage_id",
    "post_uri",
    "post_cid",
    "author_did",
    "author_handle",
    "record_created_at",
    "indexed_at",
    "text",
    "is_reply",
    "is_quote",
    "reply_root_uri",
    "reply_parent_uri",
    "embed_type",
    "media_embed_type",
    "has_image",
    "has_video",
    "has_external",
    "has_record_embed",
    "external_uri",
    "external_domain",
    "lang_primary",
    "lang_count",
    "langs_json",
    "tag_count",
    "tags_json",
    "facets_count",
    "mention_count",
    "link_count",
    "hashtag_count",
    "self_label_values_json",
    "post_label_values_json",
    "author_label_values_json",
    "contains_no_unauthenticated",
    "contains_hide_like_label",
    "like_count",
    "repost_count",
    "reply_count",
    "quote_count",
    "labels_json",
    "record_json",
    "embed_json",
    "author_json",
    "viewer_json",
    "threadgate_json",
    "debug_json",
    "raw_json",
    "labelers_requested",
    "labelers_included",
    "captured_at_utc",
)

_SUMMARY_FIELDS: tuple[str, ...] = (
    "run_id",
    "vantage_id",
    "post_uri",
    "post_author_did",
    "appearance_rows_returned",
    "likes_returned",
    "quotes_returned",
    "reposted_by_returned",
    "thread_nodes_returned",
    "thread_edges_returned",
    "seed_relationship_edges_returned",
    "followers_edges_returned",
    "follows_edges_returned",
    "follow_records_returned",
    "captured_at_utc",
)

_LIKE_FIELDS: tuple[str, ...] = (
    "run_id",
    "vantage_id",
    "post_uri",
    "post_author_did",
    "actor_did",
    "actor_handle",
    "actor_display_name",
    "created_at",
    "indexed_at",
    "raw_json",
    "captured_at_utc",
)

_QUOTE_FIELDS: tuple[str, ...] = (
    "run_id",
    "vantage_id",
    "post_uri",
    "post_author_did",
    "quote_post_uri",
    "quote_post_cid",
    "quote_author_did",
    "quote_author_handle",
    "record_created_at",
    "indexed_at",
    "text",
    "like_count",
    "repost_count",
    "reply_count",
    "quote_count",
    "raw_json",
    "captured_at_utc",
)

_REPOSTED_BY_FIELDS: tuple[str, ...] = (
    "run_id",
    "vantage_id",
    "post_uri",
    "post_author_did",
    "actor_did",
    "actor_handle",
    "actor_display_name",
    "raw_json",
    "captured_at_utc",
)

_RELATIONSHIP_FIELDS: tuple[str, ...] = (
    "run_id",
    "vantage_id",
    "context_scope",
    "context_post_uri",
    "actor_did",
    "other_did",
    "following",
    "followed_by",
    "blocking",
    "blocked_by",
    "blocking_by_list_uri",
    "blocked_by_list_uri",
    "raw_json",
    "captured_at_utc",
)

_ACTOR_PROFILE_FIELDS: tuple[str, ...] = (
    "run_id",
    "vantage_id",
    "actor_scope",
    "actor_did",
    "handle",
    "display_name",
    "description",
    "website",
    "avatar",
    "banner",
    "followers_count",
    "follows_count",
    "posts_count",
    "associated_json",
    "joined_via_starter_pack_uri",
    "indexed_at",
    "created_at",
    "labels_json",
    "pinned_post_uri",
    "verification_json",
    "status_json",
    "raw_json",
    "captured_at_utc",
)

_THREAD_NODE_FIELDS: tuple[str, ...] = (
    "run_id",
    "vantage_id",
    "focus_post_uri",
    "relation_to_focus",
    "distance_to_focus",
    "node_type",
    "post_uri",
    "post_cid",
    "author_did",
    "author_handle",
    "text",
    "record_created_at",
    "indexed_at",
    "is_reply",
    "is_quote",
    "reply_root_uri",
    "reply_parent_uri",
    "embed_type",
    "media_embed_type",
    "has_image",
    "has_video",
    "has_external",
    "has_record_embed",
    "external_uri",
    "external_domain",
    "lang_primary",
    "lang_count",
    "langs_json",
    "tag_count",
    "tags_json",
    "facets_count",
    "mention_count",
    "link_count",
    "hashtag_count",
    "self_label_values_json",
    "post_label_values_json",
    "author_label_values_json",
    "contains_no_unauthenticated",
    "contains_hide_like_label",
    "like_count",
    "repost_count",
    "reply_count",
    "quote_count",
    "labels_json",
    "raw_json",
    "captured_at_utc",
)

_THREAD_EDGE_FIELDS: tuple[str, ...] = (
    "run_id",
    "vantage_id",
    "focus_post_uri",
    "relation_to_focus",
    "parent_post_uri",
    "child_post_uri",
    "captured_at_utc",
)

_FOLLOWERS_FIELDS: tuple[str, ...] = (
    "run_id",
    "vantage_id",
    "actor_scope",
    "actor_did",
    "follower_did",
    "follower_handle",
    "follower_display_name",
    "follower_description",
    "follower_avatar",
    "follower_associated_json",
    "follower_indexed_at",
    "follower_created_at",
    "follower_labels_json",
    "follower_viewer_muted",
    "follower_viewer_blocked_by",
    "raw_json",
    "captured_at_utc",
)

_FOLLOWS_FIELDS: tuple[str, ...] = (
    "run_id",
    "vantage_id",
    "actor_scope",
    "actor_did",
    "subject_did",
    "subject_handle",
    "subject_display_name",
    "subject_description",
    "subject_avatar",
    "subject_associated_json",
    "subject_indexed_at",
    "subject_created_at",
    "subject_labels_json",
    "subject_viewer_muted",
    "subject_viewer_blocked_by",
    "raw_json",
    "captured_at_utc",
)

_FOLLOW_RECORD_FIELDS: tuple[str, ...] = (
    "run_id",
    "vantage_id",
    "actor_scope",
    "repo_did",
    "resolved_pds_host",
    "record_uri",
    "record_cid",
    "subject_did",
    "created_at",
    "value_json",
    "captured_at_utc",
)

_REPO_DESCRIPTION_FIELDS: tuple[str, ...] = (
    "run_id",
    "vantage_id",
    "actor_scope",
    "resolved_pds_host",
    "did",
    "handle",
    "handle_is_correct",
    "collections_count",
    "collections_json",
    "raw_json",
    "captured_at_utc",
)

_AUTHOR_FEED_FIELDS: tuple[str, ...] = (
    "run_id",
    "vantage_id",
    "actor_did",
    "actor_scope",
    "post_uri",
    "post_cid",
    "post_author_did",
    "post_author_handle",
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
    "text",
    "record_created_at",
    "indexed_at",
    "is_reply",
    "is_quote",
    "embed_type",
    "media_embed_type",
    "has_image",
    "has_video",
    "has_external",
    "has_record_embed",
    "external_uri",
    "external_domain",
    "lang_primary",
    "lang_count",
    "langs_json",
    "tag_count",
    "tags_json",
    "facets_count",
    "mention_count",
    "link_count",
    "hashtag_count",
    "self_label_values_json",
    "post_label_values_json",
    "author_label_values_json",
    "contains_no_unauthenticated",
    "contains_hide_like_label",
    "like_count",
    "repost_count",
    "reply_count",
    "quote_count",
    "item_raw_json",
    "captured_at_utc",
)

_FEED_GENERATOR_FIELDS: tuple[str, ...] = (
    "run_id",
    "vantage_id",
    "source_scope",
    "source_actor_dids",
    "feed_uri",
    "feed_cid",
    "feed_did",
    "creator_did",
    "creator_handle",
    "creator_display_name",
    "display_name",
    "description",
    "avatar",
    "like_count",
    "accepts_interactions",
    "content_mode",
    "indexed_at",
    "labels_json",
    "is_online",
    "is_valid",
    "raw_json",
    "labelers_requested",
    "labelers_included",
    "captured_at_utc",
)

_ACTOR_LIST_FIELDS: tuple[str, ...] = (
    "run_id",
    "vantage_id",
    "actor_did",
    "actor_scope",
    "list_uri",
    "list_cid",
    "list_name",
    "purpose",
    "description",
    "avatar",
    "indexed_at",
    "creator_did",
    "creator_handle",
    "creator_display_name",
    "labels_json",
    "viewer_json",
    "raw_json",
    "captured_at_utc",
)

_LIST_MEMBER_FIELDS: tuple[str, ...] = (
    "run_id",
    "vantage_id",
    "actor_did",
    "actor_scope",
    "list_uri",
    "list_item_uri",
    "subject_did",
    "subject_handle",
    "subject_display_name",
    "subject_description",
    "subject_avatar",
    "subject_associated_json",
    "subject_indexed_at",
    "subject_created_at",
    "subject_labels_json",
    "subject_viewer_muted",
    "subject_viewer_blocked_by",
    "raw_json",
    "captured_at_utc",
)

_ACTOR_STARTER_PACK_FIELDS: tuple[str, ...] = (
    "run_id",
    "vantage_id",
    "actor_did",
    "actor_scope",
    "starter_pack_uri",
    "starter_pack_cid",
    "record_created_at",
    "indexed_at",
    "joined_week_count",
    "joined_all_time_count",
    "creator_did",
    "creator_handle",
    "creator_display_name",
    "list_uri",
    "labels_json",
    "raw_json",
    "captured_at_utc",
)

_STARTER_PACK_CONTENT_FIELDS: tuple[str, ...] = (
    "run_id",
    "vantage_id",
    "actor_did",
    "actor_scope",
    "starter_pack_uri",
    "relation_type",
    "slot_no",
    "feed_uri",
    "list_uri",
    "raw_json",
    "captured_at_utc",
)

_LABELER_SERVICE_FIELDS: tuple[str, ...] = (
    "run_id",
    "vantage_id",
    "labeler_did",
    "creator_did",
    "creator_handle",
    "creator_display_name",
    "indexed_at",
    "labels_json",
    "policies_json",
    "raw_json",
    "captured_at_utc",
)

_DID_RESOLUTION_FIELDS: tuple[str, ...] = (
    "run_id",
    "vantage_id",
    "did",
    "resolved_pds_host",
    "resolution_method",
    "did_doc_url",
    "service_endpoint",
    "error",
    "captured_at_utc",
)

_STAGE_FILE_SPECS: dict[str, tuple[str, tuple[str, ...]]] = {
    "appearances": ("post_surface_appearances_part_000.csv", _APPEARANCE_FIELDS),
    "post_views": ("post_views_part_000.csv", _POST_VIEW_FIELDS),
    "summary": ("post_rq1_summary_part_000.csv", _SUMMARY_FIELDS),
    "likes": ("post_likes_part_000.csv", _LIKE_FIELDS),
    "quotes": ("post_quotes_part_000.csv", _QUOTE_FIELDS),
    "reposted_by": ("post_reposted_by_part_000.csv", _REPOSTED_BY_FIELDS),
    "relationships": ("relationship_edges_part_000.csv", _RELATIONSHIP_FIELDS),
    "actor_profiles": ("actor_profiles_part_000.csv", _ACTOR_PROFILE_FIELDS),
    "thread_nodes": ("thread_nodes_part_000.csv", _THREAD_NODE_FIELDS),
    "thread_edges": ("thread_edges_part_000.csv", _THREAD_EDGE_FIELDS),
    "followers": ("followers_edges_part_000.csv", _FOLLOWERS_FIELDS),
    "follows": ("follows_edges_part_000.csv", _FOLLOWS_FIELDS),
    "follow_records": ("follow_records_part_000.csv", _FOLLOW_RECORD_FIELDS),
    "repo_desc": ("repo_descriptions_part_000.csv", _REPO_DESCRIPTION_FIELDS),
    "author_feed": ("author_feed_items_part_000.csv", _AUTHOR_FEED_FIELDS),
    "feed_generators": ("feed_generators_part_000.csv", _FEED_GENERATOR_FIELDS),
    "actor_lists": ("actor_lists_part_000.csv", _ACTOR_LIST_FIELDS),
    "list_members": ("list_members_part_000.csv", _LIST_MEMBER_FIELDS),
    "starter_packs": ("actor_starter_packs_part_000.csv", _ACTOR_STARTER_PACK_FIELDS),
    "starter_pack_contents": ("starter_pack_contents_part_000.csv", _STARTER_PACK_CONTENT_FIELDS),
    "labelers": ("labeler_services_part_000.csv", _LABELER_SERVICE_FIELDS),
    "dids": ("did_resolutions_part_000.csv", _DID_RESOLUTION_FIELDS),
}

_FOCUS_STAGE_NAMES: tuple[str, ...] = ("likes", "quotes", "reposted_by", "thread_nodes", "thread_edges")
_ACTOR_FEED_CATALOG_STAGE = "actor_feed_catalog"


@dataclass(frozen=True)
class BackfillRq1FactorsConfig:
    max_posts: int = 10_000
    batch_size: int = 25
    stage: str = "all"
    max_items_per_endpoint: int = 0
    max_thread_depth: int = 1000
    max_thread_parent_height: int = 1000
    max_author_feed_items: int = 0
    max_followers_per_actor: int = 0
    max_follows_per_actor: int = 0
    max_follow_records_per_actor: int = 0
    max_actor_feeds_per_actor: int = 0
    max_lists_per_actor: int = 0
    max_list_members_per_list: int = 0
    max_starter_packs_per_actor: int = 0
    seen_after_utc: str | None = None
    seen_before_utc: str | None = None
    resolve_pds_endpoints: bool = True
    follow_record_scope: str = "seed+graph"
    shard_index: int = 0
    shard_count: int = 1
    include_hydrated: bool = False


@dataclass(frozen=True)
class FocusPostResult:
    post_uri: str
    author_did: str
    likes_rows: list[dict[str, Any]]
    quote_rows: list[dict[str, Any]]
    reposted_rows: list[dict[str, Any]]
    thread_node_rows: list[dict[str, Any]]
    thread_edge_rows: list[dict[str, Any]]
    actor_scope_additions: dict[str, set[str]]
    labeler_dids: set[str]
    summary_counts: dict[str, int]


def _chunked(values: list[str], size: int) -> Iterable[list[str]]:
    size = max(1, int(size))
    for idx in range(0, len(values), size):
        yield values[idx : idx + size]


def _as_list(data: dict[str, Any], *keys: str) -> list[dict[str, Any]]:
    for key in keys:
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _opt_limit(value: int | None) -> int | None:
    if value is None:
        return None
    value = int(value)
    return value if value > 0 else None


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _has_thread_signal(post: dict[str, Any]) -> bool:
    record = post.get("record") if isinstance(post.get("record"), dict) else {}
    if isinstance(record.get("reply"), dict):
        return True
    for key in ("replyCount", "quoteCount"):
        value = _as_int(post.get(key))
        if value is not None and value > 0:
            return True
    return False


def _did_from_at_uri(uri: str | None) -> str | None:
    if not isinstance(uri, str) or not uri.startswith("at://"):
        return None
    did = uri.removeprefix("at://").split("/")[0].strip()
    return did or None


def _actor_scope_value(scope_map: dict[str, set[str]], did: str) -> str:
    return "|".join(sorted(scope_map.get(did, set())))


def _add_scope(scope_map: dict[str, set[str]], did: str | None, scope: str) -> None:
    did = str(did or "").strip()
    if not did:
        return
    scope_map.setdefault(did, set()).add(scope)


def _hash_to_shard(value: str, shard_count: int) -> int:
    digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()
    return int(digest[:16], 16) % max(1, int(shard_count))


async def _fetch_paginated(
    *,
    http: AsyncHttpClient,
    endpoint: str,
    method: str,
    params: dict[str, Any],
    feed_uri: str | None,
    captured_at_utc: str,
    max_items: int | None,
    list_keys: tuple[str, ...],
    allow_missing_actor_not_found: bool = False,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    cursor: str | None = None
    remaining = None if max_items is None else max(0, int(max_items))
    while remaining is None or remaining > 0:
        limit = 100 if remaining is None else min(100, remaining)
        req_params = dict(params)
        req_params["limit"] = limit
        if cursor:
            req_params["cursor"] = cursor
        try:
            resp = await http.xrpc_get(
                endpoint=endpoint,
                host=http.hosts.appview_host,
                method=method,
                params=req_params,
                access_jwt=None,
                feed_uri=feed_uri,
                timestamp_utc=captured_at_utc,
            )
        except HttpError as err:
            msg = str(err).lower()
            if allow_missing_actor_not_found and int(err.status_code or 0) == 400 and (
                "actor not found" in msg or "profile not found" in msg
            ):
                logger.warning(
                    "rq1 skip missing actor/profile endpoint=%s method=%s actor=%s err=%s",
                    endpoint,
                    method,
                    str(params.get("actor") or ""),
                    err,
                )
                return []
            raise
        items = _as_list(resp.data, *list_keys)
        if not items:
            break
        out.extend(items)
        if remaining is not None:
            remaining -= len(items)
            if remaining <= 0:
                break
        cursor = resp.data.get("cursor") if isinstance(resp.data.get("cursor"), str) else None
        if not cursor:
            break
    return out if max_items is None else out[: int(max_items)]


def _iter_selected_post_rows(control: ControlState, *, cfg: BackfillRq1FactorsConfig) -> list[Any]:
    return coerce_selected_post_rows(
        control.select_posts_to_backfill_rq1_rows(
            limit=int(cfg.max_posts),
            seen_after_utc=cfg.seen_after_utc,
            seen_before_utc=cfg.seen_before_utc,
            include_hydrated=bool(cfg.include_hydrated),
            stage=str(cfg.stage),
            shard_index=int(cfg.shard_index),
            shard_count=int(cfg.shard_count),
        )
    )


def _selection_details(selected_rows: Iterable[Any], *, rq1_stage: str) -> dict[str, Any]:
    rows = list(selected_rows)
    first_seen_values = sorted(
        {
            str(row.first_seen_utc).strip()
            for row in rows
            if getattr(row, "first_seen_utc", None)
        }
    )
    return {
        "selection_order": "oldest_first",
        "rq1_stage": rq1_stage,
        "selected_posts": len(rows),
        "selected_first_seen_min_utc": first_seen_values[0] if first_seen_values else None,
        "selected_first_seen_max_utc": first_seen_values[-1] if first_seen_values else None,
    }


def _appearance_row(row: dict[str, str], source_family: str, source_path: Path) -> dict[str, Any]:
    out = {field: row.get(field) for field in _APPEARANCE_FIELDS}
    out["source_family"] = source_family
    out["source_path"] = str(source_path)
    return out


def _iter_matching_appearances(layout: Layout, post_uris: set[str]) -> Iterable[dict[str, Any]]:
    for match in iter_matching_feed_item_rows(layout, post_uris):
        yield _appearance_row(match.row, match.source_family, match.source_path)


def _post_view_row(*, run_id: RunId, vantage_id: str, post: dict[str, Any], accept_labelers: str | None, labelers_included: str | None, captured_at_utc: str) -> dict[str, Any]:
    author = post.get("author") if isinstance(post.get("author"), dict) else {}
    row = {
        "run_id": str(run_id),
        "vantage_id": vantage_id,
        "post_uri": post.get("uri"),
        "post_cid": post.get("cid"),
        "author_did": author.get("did"),
        "author_handle": author.get("handle"),
        **extract_post_record_features(post),
        "like_count": post.get("likeCount"),
        "repost_count": post.get("repostCount"),
        "reply_count": post.get("replyCount"),
        "quote_count": post.get("quoteCount"),
        "labels_json": json_compact(post.get("labels")),
        "record_json": json_compact(post.get("record")),
        "embed_json": json_compact(post.get("embed")),
        "author_json": json_compact(author),
        "viewer_json": json_compact(post.get("viewer")),
        "threadgate_json": json_compact(post.get("threadgate")),
        "debug_json": json_compact(post.get("debug")),
        "raw_json": json_compact(post),
        "labelers_requested": accept_labelers,
        "labelers_included": labelers_included,
        "captured_at_utc": captured_at_utc,
    }
    return row


def _relationship_row(*, run_id: RunId, vantage_id: str, actor_did: str, context_scope: str, context_post_uri: str | None, rel: dict[str, Any], captured_at_utc: str) -> dict[str, Any]:
    blocking_by_list = rel.get("blockingByList")
    blocked_by_list = rel.get("blockedByList")
    return {
        "run_id": str(run_id),
        "vantage_id": vantage_id,
        "context_scope": context_scope,
        "context_post_uri": context_post_uri,
        "actor_did": actor_did,
        "other_did": rel.get("did"),
        "following": rel.get("following"),
        "followed_by": rel.get("followedBy"),
        "blocking": rel.get("blocking"),
        "blocked_by": rel.get("blockedBy"),
        "blocking_by_list_uri": blocking_by_list if isinstance(blocking_by_list, str) else None,
        "blocked_by_list_uri": blocked_by_list if isinstance(blocked_by_list, str) else None,
        "raw_json": json_compact(rel),
        "captured_at_utc": captured_at_utc,
    }


async def _get_profiles(http: AsyncHttpClient, actor_dids: list[str], captured_at_utc: str) -> list[dict[str, Any]]:
    profiles: list[dict[str, Any]] = []
    for batch in _chunked(sorted({did for did in actor_dids if did}), 25):
        resp = await http.xrpc_get(
            endpoint="app.bsky.actor.getProfiles",
            host=http.hosts.appview_host,
            method="app.bsky.actor.getProfiles",
            params={"actors": batch},
            access_jwt=None,
            feed_uri=None,
            timestamp_utc=captured_at_utc,
        )
        profiles.extend(_as_list(resp.data, "profiles"))
    return profiles


async def _get_relationships(http: AsyncHttpClient, actor_did: str, others: list[str], captured_at_utc: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    others = [did for did in sorted(set(others)) if did and did != actor_did]
    for batch in _chunked(others, 30):
        resp = await http.xrpc_get(
            endpoint="app.bsky.graph.getRelationships",
            host=http.hosts.appview_host,
            method="app.bsky.graph.getRelationships",
            params={"actor": actor_did, "others": batch},
            access_jwt=None,
            feed_uri=None,
            timestamp_utc=captured_at_utc,
        )
        rows.extend(_as_list(resp.data, "relationships"))
    return rows


async def _get_post_thread(http: AsyncHttpClient, post_uri: str, captured_at_utc: str, max_depth: int, max_parent_height: int) -> dict[str, Any] | None:
    if max_depth <= 0 and max_parent_height <= 0:
        return None
    resp = await http.xrpc_get(
        endpoint="app.bsky.feed.getPostThread",
        host=http.hosts.appview_host,
        method="app.bsky.feed.getPostThread",
        params={"uri": post_uri, "depth": max(0, int(max_depth)), "parentHeight": max(0, int(max_parent_height))},
        access_jwt=None,
        feed_uri=post_uri,
        timestamp_utc=captured_at_utc,
    )
    thread = resp.data.get("thread")
    return thread if isinstance(thread, dict) else None


def _thread_node_post_uri(node: dict[str, Any]) -> str | None:
    post = node.get("post") if isinstance(node.get("post"), dict) else {}
    value = post.get("uri") if isinstance(post.get("uri"), str) else None
    if value is None:
        return None
    value = value.strip()
    return value or None


def _thread_visit_key(node: dict[str, Any]) -> tuple[str, str | int]:
    post_uri = _thread_node_post_uri(node)
    if post_uri is not None:
        return ("post_uri", post_uri)
    return ("node_id", id(node))


def _walk_thread(
    *,
    focus_post_uri: str,
    node: dict[str, Any],
    relation_to_focus: str,
    distance: int,
    run_id: RunId,
    vantage_id: str,
    captured_at_utc: str,
    actor_scopes: dict[str, set[str]],
    labeler_dids: set[str],
    out_nodes: list[dict[str, Any]],
    out_edges: list[dict[str, Any]],
    seen_post_uris: set[str],
) -> None:
    stack: list[tuple[dict[str, Any], str, int]] = [(node, relation_to_focus, distance)]
    seen_node_keys: set[tuple[str, str | int]] = set()
    while stack:
        current_node, current_relation, current_distance = stack.pop()
        current_key = _thread_visit_key(current_node)
        if current_key in seen_node_keys:
            continue
        seen_node_keys.add(current_key)

        row = flatten_thread_node(
            focus_post_uri=focus_post_uri,
            relation_to_focus=current_relation,
            distance_to_focus=current_distance,
            node=current_node,
        )
        post_uri = str(row.get("post_uri") or "").strip() or None
        if post_uri and post_uri not in seen_post_uris:
            seen_post_uris.add(post_uri)
            out_nodes.append({"run_id": str(run_id), "vantage_id": vantage_id, **row, "captured_at_utc": captured_at_utc})
            post_obj = current_node.get("post") if isinstance(current_node.get("post"), dict) else {}
            author_did = str(row.get("author_did") or "").strip() or None
            _add_scope(actor_scopes, author_did, "thread_actor")
            author = post_obj.get("author") if isinstance(post_obj.get("author"), dict) else {}
            labeler_dids.update(extract_label_src_dids(post_obj.get("labels")))
            labeler_dids.update(extract_label_src_dids(author.get("labels")))

        parent = current_node.get("parent") if isinstance(current_node.get("parent"), dict) else None
        replies = current_node.get("replies") if isinstance(current_node.get("replies"), list) else []

        for reply in reversed(replies):
            if not isinstance(reply, dict):
                continue
            reply_uri = _thread_node_post_uri(reply)
            if post_uri and reply_uri:
                out_edges.append(
                    {
                        "run_id": str(run_id),
                        "vantage_id": vantage_id,
                        "focus_post_uri": focus_post_uri,
                        "relation_to_focus": "descendant_chain",
                        "parent_post_uri": post_uri,
                        "child_post_uri": reply_uri,
                        "captured_at_utc": captured_at_utc,
                    }
                )
            if _thread_visit_key(reply) not in seen_node_keys:
                stack.append((reply, "descendant", current_distance + 1))

        if parent is not None:
            parent_uri = _thread_node_post_uri(parent)
            if parent_uri and post_uri:
                out_edges.append(
                    {
                        "run_id": str(run_id),
                        "vantage_id": vantage_id,
                        "focus_post_uri": focus_post_uri,
                        "relation_to_focus": "ancestor_chain",
                        "parent_post_uri": parent_uri,
                        "child_post_uri": post_uri,
                        "captured_at_utc": captured_at_utc,
                    }
                )
            if _thread_visit_key(parent) not in seen_node_keys:
                stack.append((parent, "ancestor", current_distance + 1))


async def _resolve_pds(
    did_resolver: DidResolver,
    did: str,
    captured_at_utc: str,
    run_id: RunId,
    vantage_id: str,
) -> tuple[str | None, dict[str, Any]]:
    result = await did_resolver.resolve_pds_endpoint(did)
    row = {
        "run_id": str(run_id),
        "vantage_id": vantage_id,
        "did": did,
        "resolved_pds_host": result.pds_endpoint,
        "resolution_method": result.resolution_method,
        "did_doc_url": result.did_doc_url,
        "service_endpoint": result.service_endpoint,
        "error": result.error,
        "captured_at_utc": captured_at_utc,
    }
    return result.pds_endpoint, row


async def _process_focus_post(
    *,
    http: AsyncHttpClient,
    run_id: RunId,
    vantage_id: str,
    post: dict[str, Any],
    cfg: BackfillRq1FactorsConfig,
    max_items: int | None,
) -> FocusPostResult:
    captured_at_utc = format_utc(now_utc())
    post_uri = str(post.get("uri") or "").strip()
    author = post.get("author") if isinstance(post.get("author"), dict) else {}
    author_did = str(author.get("did") or "").strip()
    actor_scope_additions: dict[str, set[str]] = {}
    labelers: set[str] = set()
    _add_scope(actor_scope_additions, author_did, "post_author")
    labelers.update(extract_label_src_dids(post.get("labels")))
    labelers.update(extract_label_src_dids(author.get("labels")))

    likes_rows: list[dict[str, Any]] = []
    quote_rows: list[dict[str, Any]] = []
    reposted_rows: list[dict[str, Any]] = []
    thread_node_rows: list[dict[str, Any]] = []
    thread_edge_rows: list[dict[str, Any]] = []

    like_count = _as_int(post.get("likeCount"))
    if like_count is None or like_count > 0:
        likes = await _fetch_paginated(
            http=http,
            endpoint="app.bsky.feed.getLikes",
            method="app.bsky.feed.getLikes",
            params={"uri": post_uri},
            feed_uri=post_uri,
            captured_at_utc=captured_at_utc,
            max_items=max_items,
            list_keys=("likes",),
        )
        for item in likes:
            actor = item.get("actor") if isinstance(item.get("actor"), dict) else {}
            actor_did = actor.get("did")
            _add_scope(actor_scope_additions, actor_did, "liker")
            likes_rows.append(
                {
                    "run_id": str(run_id),
                    "vantage_id": vantage_id,
                    "post_uri": post_uri,
                    "post_author_did": author_did,
                    "actor_did": actor_did,
                    "actor_handle": actor.get("handle"),
                    "actor_display_name": actor.get("displayName"),
                    "created_at": item.get("createdAt"),
                    "indexed_at": item.get("indexedAt"),
                    "raw_json": json_compact(item),
                    "captured_at_utc": captured_at_utc,
                }
            )

    quote_count = _as_int(post.get("quoteCount"))
    if quote_count is None or quote_count > 0:
        quotes = await _fetch_paginated(
            http=http,
            endpoint="app.bsky.feed.getQuotes",
            method="app.bsky.feed.getQuotes",
            params={"uri": post_uri},
            feed_uri=post_uri,
            captured_at_utc=captured_at_utc,
            max_items=max_items,
            list_keys=("posts", "quotes"),
        )
        for quote in quotes:
            q_author = quote.get("author") if isinstance(quote.get("author"), dict) else {}
            q_author_did = q_author.get("did")
            _add_scope(actor_scope_additions, q_author_did, "quote_author")
            labelers.update(extract_label_src_dids(quote.get("labels")))
            labelers.update(extract_label_src_dids(q_author.get("labels")))
            quote_rows.append(
                {
                    "run_id": str(run_id),
                    "vantage_id": vantage_id,
                    "post_uri": post_uri,
                    "post_author_did": author_did,
                    "quote_post_uri": quote.get("uri"),
                    "quote_post_cid": quote.get("cid"),
                    "quote_author_did": q_author_did,
                    "quote_author_handle": q_author.get("handle"),
                    "record_created_at": quote.get("record", {}).get("createdAt") if isinstance(quote.get("record"), dict) else None,
                    "indexed_at": quote.get("indexedAt"),
                    "text": quote.get("record", {}).get("text") if isinstance(quote.get("record"), dict) else None,
                    "like_count": quote.get("likeCount"),
                    "repost_count": quote.get("repostCount"),
                    "reply_count": quote.get("replyCount"),
                    "quote_count": quote.get("quoteCount"),
                    "raw_json": json_compact(quote),
                    "captured_at_utc": captured_at_utc,
                }
            )

    repost_count = _as_int(post.get("repostCount"))
    if repost_count is None or repost_count > 0:
        reposted = await _fetch_paginated(
            http=http,
            endpoint="app.bsky.feed.getRepostedBy",
            method="app.bsky.feed.getRepostedBy",
            params={"uri": post_uri},
            feed_uri=post_uri,
            captured_at_utc=captured_at_utc,
            max_items=max_items,
            list_keys=("repostedBy",),
        )
        for actor in reposted:
            actor_did = actor.get("did")
            _add_scope(actor_scope_additions, actor_did, "reposter")
            reposted_rows.append(
                {
                    "run_id": str(run_id),
                    "vantage_id": vantage_id,
                    "post_uri": post_uri,
                    "post_author_did": author_did,
                    "actor_did": actor_did,
                    "actor_handle": actor.get("handle"),
                    "actor_display_name": actor.get("displayName"),
                    "raw_json": json_compact(actor),
                    "captured_at_utc": captured_at_utc,
                }
            )

    if _has_thread_signal(post):
        thread = await _get_post_thread(http, post_uri, captured_at_utc, int(cfg.max_thread_depth), int(cfg.max_thread_parent_height))
        if thread:
            _walk_thread(
                focus_post_uri=post_uri,
                node=thread,
                relation_to_focus="focus",
                distance=0,
                run_id=run_id,
                vantage_id=vantage_id,
                captured_at_utc=captured_at_utc,
                actor_scopes=actor_scope_additions,
                labeler_dids=labelers,
                out_nodes=thread_node_rows,
                out_edges=thread_edge_rows,
                seen_post_uris=set(),
            )

    return FocusPostResult(
        post_uri=post_uri,
        author_did=author_did,
        likes_rows=likes_rows,
        quote_rows=quote_rows,
        reposted_rows=reposted_rows,
        thread_node_rows=thread_node_rows,
        thread_edge_rows=thread_edge_rows,
        actor_scope_additions=actor_scope_additions,
        labeler_dids=labelers,
        summary_counts={
            "likes_returned": len(likes_rows),
            "quotes_returned": len(quote_rows),
            "reposted_by_returned": len(reposted_rows),
            "thread_nodes_returned": len(thread_node_rows),
            "thread_edges_returned": len(thread_edge_rows),
        },
    )


async def run_backfill_rq1_factors(
    *,
    layout: Layout,
    hosts: XrpcHosts,
    run_id: RunId,
    rps: float,
    concurrency: int,
    dry_run: bool,
    cfg: BackfillRq1FactorsConfig | None = None,
    accept_language: str | None,
    accept_labelers: str | None,
    vantage_id: str,
) -> None:
    cfg = cfg or BackfillRq1FactorsConfig()
    rq1_stage = normalize_rq1_stage(cfg.stage)
    run_graph_stage = rq1_stage_includes_graph(rq1_stage)
    run_repo_stage = rq1_stage_includes_repo(rq1_stage)
    shard_count = max(1, int(cfg.shard_count))
    shard_index = int(cfg.shard_index)
    if shard_index < 0 or shard_index >= shard_count:
        raise ValueError(f"shard_index must be within [0,{shard_count}), got {shard_index}")

    date_str = utc_date_str(now_utc())
    run_root = layout.out_base / "rq1_factors" / date_str / f"shard_{shard_index:03d}"
    if dry_run:
        logger.info("dry_run=true: would backfill rq1 factors out=%s", str(run_root))
        return
    ensure_dir(run_root)

    started_at_utc = format_utc(now_utc())
    manifest_path = run_root / "run_manifest.json"
    progress_path = run_root / "progress.json"
    http_stats_path = run_root / "http_stats.csv"
    provenance_path = run_root / "request_provenance.csv"

    manifest = {
        "run_id": str(run_id),
        "job_name": "backfill-rq1-factors",
        "date_utc": date_str,
        "started_at_utc": started_at_utc,
        "params": {
            "max_posts": cfg.max_posts,
            "batch_size": cfg.batch_size,
            "stage": rq1_stage,
            "max_items_per_endpoint": cfg.max_items_per_endpoint,
            "max_thread_depth": cfg.max_thread_depth,
            "max_thread_parent_height": cfg.max_thread_parent_height,
            "max_author_feed_items": cfg.max_author_feed_items,
            "max_followers_per_actor": cfg.max_followers_per_actor,
            "max_follows_per_actor": cfg.max_follows_per_actor,
            "max_follow_records_per_actor": cfg.max_follow_records_per_actor,
            "max_actor_feeds_per_actor": cfg.max_actor_feeds_per_actor,
            "max_lists_per_actor": cfg.max_lists_per_actor,
            "max_list_members_per_list": cfg.max_list_members_per_list,
            "max_starter_packs_per_actor": cfg.max_starter_packs_per_actor,
            "seen_after_utc": cfg.seen_after_utc,
            "seen_before_utc": cfg.seen_before_utc,
            "resolve_pds_endpoints": cfg.resolve_pds_endpoints,
            "follow_record_scope": cfg.follow_record_scope,
            "shard_index": shard_index,
            "shard_count": shard_count,
            "include_hydrated": bool(cfg.include_hydrated),
            "accept_language": accept_language,
            "accept_labelers": accept_labelers,
            "vantage_id": str(vantage_id).strip() or "unauth",
        },
    }
    enrich_manifest(manifest, job_name="backfill-rq1-factors", out_base=layout.out_base, params=manifest["params"])
    atomic_write_json(manifest_path, manifest)

    progress_state = ProgressState(
        job_name="backfill-rq1-factors",
        run_id=run_id,
        started_at_utc=started_at_utc,
        unit_label="posts",
    )
    progress_state.rps_config = rps
    progress_state.concurrency = concurrency
    progress_state.update_details({"phase": "selecting_posts", "selection_order": "oldest_first", "rq1_stage": rq1_stage})
    progress_reporter = ProgressReporter(progress_path, progress_state, write_interval_s=15.0)
    progress_reporter.start()

    http_stats_writer = CsvPartWriter(http_stats_path, fieldnames=["timestamp_utc", "endpoint", "status_code", "latency_ms", "attempt", "error_type", "feed_uri"])
    vantage_value = str(vantage_id).strip() or "unauth"
    request_context_factory = JobRequestContextFactory(
        run_id=str(run_id),
        job_name="backfill-rq1-factors",
        sample_family=str(manifest.get("sample_family") or "rq1_factor_backfill"),
        collection_params_hash=str(manifest.get("collection_params_hash") or ""),
        appview_host=hosts.appview_host,
        pds_host=hosts.pds_host,
        date_utc=date_str,
        viewer_mode="unauth",
        vantage_id=vantage_value,
        shard_id=shard_index,
        shard_count=shard_count,
    )
    http = AsyncHttpClient(
        hosts=hosts,
        rps=rps,
        retry=HttpRetryConfig(max_retries=1),
        timeout_s=30.0,
        http_stats=http_stats_writer,
        progress=progress_state,
        accept_language=accept_language,
        accept_labelers=accept_labelers,
        request_provenance_writer=RequestProvenanceWriter(provenance_path),
        request_context_factory=request_context_factory,
    )
    did_resolver = DidResolver(http=http)
    stage_store = Rq1StageStore.open(run_root / "rq1_stage_store.sqlite")

    def _materialize_stage_outputs() -> None:
        for stage_name, (file_name, fieldnames) in _STAGE_FILE_SPECS.items():
            stage_store.materialize_csv(stage_name=stage_name, path=run_root / file_name, fieldnames=fieldnames)

    def _stage_write(stage_name: str, entity_key: str, rows: list[dict[str, Any]]) -> None:
        stage_store.upsert_stage_rows(
            stage_name=stage_name,
            entity_key=entity_key,
            rows=rows,
            completed_at_utc=format_utc(now_utc()),
        )

    def _stage_write_grouped(stage_name: str, key_field: str, rows: list[dict[str, Any]]) -> None:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            entity_key = str(row.get(key_field) or "").strip()
            if not entity_key:
                continue
            grouped.setdefault(entity_key, []).append(dict(row))
        for entity_key, entity_rows in grouped.items():
            _stage_write(stage_name, entity_key, entity_rows)

    if stage_store.conn.execute("SELECT 1 FROM rq1_stage_registry LIMIT 1").fetchone() is not None:
        _materialize_stage_outputs()

    success = False
    actor_scopes: dict[str, set[str]] = {}
    labeler_dids: set[str] = set()
    feed_sources: dict[str, set[str]] = {}
    feed_source_actors: dict[str, set[str]] = {}
    list_uris_to_fetch: set[str] = set()
    starter_pack_uris: set[str] = set()
    graph_actor_dids: set[str] = set()
    hydrated_actor_profiles: set[str] = set()

    max_items = _opt_limit(cfg.max_items_per_endpoint)
    max_followers = _opt_limit(cfg.max_followers_per_actor)
    max_follows = _opt_limit(cfg.max_follows_per_actor)
    max_follow_records = _opt_limit(cfg.max_follow_records_per_actor)
    max_author_feed_items = _opt_limit(cfg.max_author_feed_items)
    max_actor_feeds = _opt_limit(cfg.max_actor_feeds_per_actor)
    max_lists = _opt_limit(cfg.max_lists_per_actor)
    max_list_members = _opt_limit(cfg.max_list_members_per_list)
    max_starter_packs = _opt_limit(cfg.max_starter_packs_per_actor)
    manifest["effective_limits"] = {
        "max_items_per_endpoint": max_items,
        "max_followers_per_actor": max_followers,
        "max_follows_per_actor": max_follows,
        "max_follow_records_per_actor": max_follow_records,
        "max_author_feed_items": max_author_feed_items,
        "max_actor_feeds_per_actor": max_actor_feeds,
        "max_lists_per_actor": max_lists,
        "max_list_members_per_list": max_list_members,
        "max_starter_packs_per_actor": max_starter_packs,
    }
    atomic_write_json(manifest_path, manifest)

    try:
        with ControlState.open(layout.control_db_path) as control:
            selected_rows = _iter_selected_post_rows(control, cfg=cfg)
            selection_details = _selection_details(selected_rows, rq1_stage=rq1_stage)
            manifest["selection"] = selection_details
            atomic_write_json(manifest_path, manifest)
            progress_state.update_details({**selection_details, "phase": "scanning_surface_appearances"})
            post_uris = [str(row.post_uri) for row in selected_rows]
            control.ensure_post_rq1_factor_tasks(post_uris=post_uris, enqueued_at_utc=started_at_utc)
            control.commit()
        progress_state.feeds_total = len(post_uris)
        logger.info(
            "backfill-rq1-factors start posts=%s batch_size=%s first_seen_min=%s first_seen_max=%s",
            len(post_uris),
            cfg.batch_size,
            selection_details.get("selected_first_seen_min_utc"),
            selection_details.get("selected_first_seen_max_utc"),
        )

        def _parse_json_object(raw: Any) -> dict[str, Any] | None:
            if raw is None:
                return None
            try:
                parsed = json.loads(str(raw))
            except Exception:  # noqa: BLE001
                return None
            return parsed if isinstance(parsed, dict) else None

        summary_counts: dict[str, dict[str, int]] = {
            uri: {
                "appearance_rows_returned": 0,
                "likes_returned": 0,
                "quotes_returned": 0,
                "reposted_by_returned": 0,
                "thread_nodes_returned": 0,
                "thread_edges_returned": 0,
                "seed_relationship_edges_returned": 0,
                "followers_edges_returned": 0,
                "follows_edges_returned": 0,
                "follow_records_returned": 0,
            }
            for uri in post_uris
        }
        appearance_count_by_post: dict[str, int] = {uri: 0 for uri in post_uris}
        author_to_posts: dict[str, set[str]] = {}
        post_author_by_post: dict[str, str] = {uri: "" for uri in post_uris}
        completed_focus_posts: list[str] = []
        hydrated_posts_by_uri: dict[str, dict[str, Any]] = {}

        def _apply_appearance_row(row: dict[str, Any]) -> None:
            post_uri = str(row.get("post_uri") or "").strip()
            if not post_uri:
                return
            appearance_count_by_post[post_uri] = appearance_count_by_post.get(post_uri, 0) + 1
            summary_counts[post_uri]["appearance_rows_returned"] = appearance_count_by_post.get(post_uri, 0)
            _add_scope(actor_scopes, row.get("author_did"), "surface_author")
            _add_scope(actor_scopes, row.get("reason_actor_did"), "surface_reason_actor")
            feed_uri = str(row.get("feed_uri") or "").strip()
            if feed_uri:
                feed_sources.setdefault(feed_uri, set()).add("surface")
                feed_source_actors.setdefault(feed_uri, set())
                creator_did = _did_from_at_uri(feed_uri)
                _add_scope(actor_scopes, creator_did, "feed_creator_repo")
                if creator_did:
                    feed_source_actors[feed_uri].add(creator_did)

        def _apply_post_view_row(row: dict[str, Any]) -> None:
            post_uri = str(row.get("post_uri") or "").strip()
            author_did = str(row.get("author_did") or "").strip()
            if post_uri:
                post_author_by_post[post_uri] = author_did
                if author_did:
                    author_to_posts.setdefault(author_did, set()).add(post_uri)
            _add_scope(actor_scopes, author_did, "post_author")
            post = _parse_json_object(row.get("raw_json"))
            if isinstance(post, dict) and post_uri:
                hydrated_posts_by_uri[post_uri] = post
                labeler_dids.update(extract_label_src_dids(post.get("labels")))
                author = post.get("author") if isinstance(post.get("author"), dict) else {}
                labeler_dids.update(extract_label_src_dids(author.get("labels")))

        def _apply_like_rows(rows: list[dict[str, Any]], *, post_uri: str) -> None:
            summary_counts[post_uri]["likes_returned"] += len(rows)
            for row in rows:
                _add_scope(actor_scopes, row.get("actor_did"), "liker")

        def _apply_quote_rows(rows: list[dict[str, Any]], *, post_uri: str) -> None:
            summary_counts[post_uri]["quotes_returned"] += len(rows)
            for row in rows:
                _add_scope(actor_scopes, row.get("quote_author_did"), "quote_author")
                quote_obj = _parse_json_object(row.get("raw_json"))
                if isinstance(quote_obj, dict):
                    labeler_dids.update(extract_label_src_dids(quote_obj.get("labels")))
                    q_author = quote_obj.get("author") if isinstance(quote_obj.get("author"), dict) else {}
                    labeler_dids.update(extract_label_src_dids(q_author.get("labels")))

        def _apply_reposted_rows(rows: list[dict[str, Any]], *, post_uri: str) -> None:
            summary_counts[post_uri]["reposted_by_returned"] += len(rows)
            for row in rows:
                _add_scope(actor_scopes, row.get("actor_did"), "reposter")

        def _apply_thread_rows(node_rows: list[dict[str, Any]], edge_rows: list[dict[str, Any]], *, post_uri: str) -> None:
            summary_counts[post_uri]["thread_nodes_returned"] += len(node_rows)
            summary_counts[post_uri]["thread_edges_returned"] += len(edge_rows)
            for row in node_rows:
                _add_scope(actor_scopes, row.get("author_did"), "thread_actor")
                node_obj = _parse_json_object(row.get("raw_json"))
                if isinstance(node_obj, dict):
                    post_obj = node_obj.get("post") if isinstance(node_obj.get("post"), dict) else {}
                    author = post_obj.get("author") if isinstance(post_obj.get("author"), dict) else {}
                    labeler_dids.update(extract_label_src_dids(post_obj.get("labels")))
                    labeler_dids.update(extract_label_src_dids(author.get("labels")))

        def _parse_json_value(raw: Any) -> Any:
            if raw is None or raw == "":
                return None
            try:
                return json.loads(str(raw))
            except Exception:  # noqa: BLE001
                return None

        def _apply_actor_profile_rows(_: str, rows: list[dict[str, Any]]) -> None:
            for row in rows:
                did = str(row.get("actor_did") or "").strip()
                if did:
                    hydrated_actor_profiles.add(did)
                labels = _parse_json_value(row.get("labels_json"))
                labeler_dids.update(extract_label_src_dids(labels))

        def _apply_relationship_rows(actor_did: str, rows: list[dict[str, Any]]) -> None:
            for row in rows:
                _add_scope(actor_scopes, row.get("other_did"), "relationship_counterparty")
            for uri in author_to_posts.get(actor_did, set()):
                summary_counts[uri]["seed_relationship_edges_returned"] += len(rows)

        def _apply_follower_rows(actor_did: str, rows: list[dict[str, Any]]) -> None:
            for row in rows:
                follower_did = str(row.get("follower_did") or "").strip()
                _add_scope(actor_scopes, follower_did, "follower_of_seed")
                if follower_did:
                    graph_actor_dids.add(follower_did)
                labels = _parse_json_value(row.get("follower_labels_json"))
                labeler_dids.update(extract_label_src_dids(labels))
            for uri in author_to_posts.get(actor_did, set()):
                summary_counts[uri]["followers_edges_returned"] += len(rows)

        def _apply_follow_rows(actor_did: str, rows: list[dict[str, Any]]) -> None:
            for row in rows:
                subject_did = str(row.get("subject_did") or "").strip()
                _add_scope(actor_scopes, subject_did, "followed_by_seed")
                if subject_did:
                    graph_actor_dids.add(subject_did)
                labels = _parse_json_value(row.get("subject_labels_json"))
                labeler_dids.update(extract_label_src_dids(labels))
            for uri in author_to_posts.get(actor_did, set()):
                summary_counts[uri]["follows_edges_returned"] += len(rows)

        def _apply_author_feed_rows(_: str, rows: list[dict[str, Any]]) -> None:
            for row in rows:
                _add_scope(actor_scopes, row.get("post_author_did"), "author_feed_post_author")
                _add_scope(actor_scopes, row.get("reason_actor_did"), "author_feed_reason_actor")
                item = _parse_json_object(row.get("item_raw_json"))
                if isinstance(item, dict):
                    post = item.get("post") if isinstance(item.get("post"), dict) else {}
                    author = post.get("author") if isinstance(post.get("author"), dict) else {}
                    labeler_dids.update(extract_label_src_dids(post.get("labels")))
                    labeler_dids.update(extract_label_src_dids(author.get("labels")))

        def _apply_actor_feed_catalog_rows(_: str, rows: list[dict[str, Any]]) -> None:
            for row in rows:
                actor_did = str(row.get("actor_did") or "").strip()
                feed_uri = str(row.get("feed_uri") or "").strip()
                if not feed_uri:
                    continue
                feed_sources.setdefault(feed_uri, set()).add("actor_feed_catalog")
                if actor_did:
                    feed_source_actors.setdefault(feed_uri, set()).add(actor_did)

        def _apply_actor_list_rows(_: str, rows: list[dict[str, Any]]) -> None:
            for row in rows:
                list_uri = str(row.get("list_uri") or "").strip()
                if list_uri:
                    list_uris_to_fetch.add(list_uri)
                _add_scope(actor_scopes, row.get("creator_did"), "list_creator")
                labeler_dids.update(extract_label_src_dids(_parse_json_value(row.get("labels_json"))))

        def _apply_starter_pack_rows(_: str, rows: list[dict[str, Any]]) -> None:
            for row in rows:
                starter_pack_uri = str(row.get("starter_pack_uri") or "").strip()
                if starter_pack_uri:
                    starter_pack_uris.add(starter_pack_uri)
                list_uri = str(row.get("list_uri") or "").strip()
                if list_uri:
                    list_uris_to_fetch.add(list_uri)
                _add_scope(actor_scopes, row.get("creator_did"), "starter_pack_creator")
                labeler_dids.update(extract_label_src_dids(_parse_json_value(row.get("labels_json"))))

        def _apply_list_member_rows(list_uri: str, rows: list[dict[str, Any]]) -> None:
            hydrated_list_uris.add(list_uri)
            for row in rows:
                _add_scope(actor_scopes, row.get("subject_did"), "list_member")
                labeler_dids.update(extract_label_src_dids(_parse_json_value(row.get("subject_labels_json"))))

        def _apply_starter_pack_content_rows(_: str, rows: list[dict[str, Any]]) -> None:
            for row in rows:
                actor_did = str(row.get("actor_did") or "").strip()
                feed_uri = str(row.get("feed_uri") or "").strip()
                list_uri = str(row.get("list_uri") or "").strip()
                if feed_uri:
                    feed_sources.setdefault(feed_uri, set()).add("starter_pack")
                    if actor_did:
                        feed_source_actors.setdefault(feed_uri, set()).add(actor_did)
                if list_uri:
                    list_uris_to_fetch.add(list_uri)

        def _apply_follow_record_rows(actor_did: str, rows: list[dict[str, Any]]) -> None:
            for row in rows:
                _add_scope(actor_scopes, row.get("subject_did"), "follow_record_subject")
            for uri in author_to_posts.get(actor_did, set()):
                summary_counts[uri]["follow_records_returned"] += len(rows)

        def _apply_feed_generator_rows(_: str, rows: list[dict[str, Any]]) -> None:
            for row in rows:
                _add_scope(actor_scopes, row.get("creator_did"), "feed_creator")
                raw = _parse_json_object(row.get("raw_json"))
                if isinstance(raw, dict):
                    view = raw.get("view") if isinstance(raw.get("view"), dict) else {}
                    labeler_dids.update(extract_label_src_dids(view.get("labels")))

        def _resolved_host_from_did_rows(rows: list[dict[str, Any]]) -> str | None:
            if not rows:
                return None
            host = str(rows[0].get("resolved_pds_host") or "").strip()
            return host or None

        def _replay_completed_stage(
            *,
            stage_name: str,
            entity_keys: Iterable[str],
            apply_rows: Callable[[str, list[dict[str, Any]]], None],
            progress_counter: str | None = None,
        ) -> list[str]:
            keys = [str(key).strip() for key in entity_keys if str(key).strip()]
            if not keys:
                return []
            missing = stage_store.filter_incomplete(stage_name=stage_name, entity_keys=keys)
            missing_set = set(missing)
            for entity_key in keys:
                if entity_key in missing_set:
                    continue
                rows = stage_store.stage_rows(stage_name=stage_name, entity_key=entity_key)
                apply_rows(entity_key, rows)
                if progress_counter is not None:
                    progress_state.add_rows(progress_counter, len(rows))
            return missing

        appearance_missing = stage_store.filter_incomplete(stage_name="appearances", entity_keys=post_uris)
        appearance_missing_set = set(appearance_missing)
        for post_uri in post_uris:
            if post_uri in appearance_missing_set:
                continue
            for row in stage_store.stage_rows(stage_name="appearances", entity_key=post_uri):
                _apply_appearance_row(row)

        grouped_appearances: dict[str, list[dict[str, Any]]] = {uri: [] for uri in appearance_missing}
        if appearance_missing_set:
            for appearance in _iter_matching_appearances(layout, appearance_missing_set):
                post_uri = str(appearance.get("post_uri") or "").strip()
                if post_uri:
                    grouped_appearances.setdefault(post_uri, []).append(appearance)
            for post_uri in appearance_missing:
                rows = grouped_appearances.get(post_uri, [])
                _stage_write("appearances", post_uri, rows)
                for row in rows:
                    _apply_appearance_row(row)
        progress_state.add_rows("surface_appearances", sum(appearance_count_by_post.values()))
        progress_state.update_details(
            {
                "phase": "hydrating_seed_posts",
                "surface_posts_touched": sum(1 for value in appearance_count_by_post.values() if value > 0),
            }
        )

        post_views_missing = stage_store.filter_incomplete(stage_name="post_views", entity_keys=post_uris)
        post_views_missing_set = set(post_views_missing)
        for post_uri in post_uris:
            if post_uri in post_views_missing_set:
                continue
            for row in stage_store.stage_rows(stage_name="post_views", entity_key=post_uri):
                _apply_post_view_row(row)
                progress_state.add_rows("post_views", 1)

        for batch in _chunked(post_views_missing, max(1, int(cfg.batch_size))):
            captured_at_utc = format_utc(now_utc())
            resp = await http.xrpc_get(
                endpoint="app.bsky.feed.getPosts",
                host=http.hosts.appview_host,
                method="app.bsky.feed.getPosts",
                params={"uris": batch},
                access_jwt=None,
                feed_uri=None,
                timestamp_utc=captured_at_utc,
            )
            labelers_included = resp.content_labelers
            returned_rows: dict[str, dict[str, Any]] = {}
            for post in _as_list(resp.data, "posts"):
                row = _post_view_row(
                    run_id=run_id,
                    vantage_id=vantage_value,
                    post=post,
                    accept_labelers=accept_labelers,
                    labelers_included=labelers_included,
                    captured_at_utc=captured_at_utc,
                )
                post_uri = str(row.get("post_uri") or "")
                if not post_uri:
                    continue
                returned_rows[post_uri] = row
                _stage_write("post_views", post_uri, [row])
                _apply_post_view_row(row)
                progress_state.add_rows("post_views", 1)
            for post_uri in batch:
                if post_uri not in returned_rows:
                    _stage_write("post_views", post_uri, [])

        task_concurrency = max(1, int(concurrency or 1))
        semaphore = asyncio.Semaphore(task_concurrency)

        async def _bounded_call(fn, /, *args, **kwargs):  # noqa: ANN001, ANN202
            async with semaphore:
                return await fn(*args, **kwargs)

        def _focus_complete(post_uri: str) -> bool:
            return all(stage_store.stage_complete(stage_name=stage_name, entity_key=post_uri) for stage_name in _FOCUS_STAGE_NAMES)

        focus_missing: list[str] = []
        for post_uri in post_uris:
            if _focus_complete(post_uri):
                like_rows = stage_store.stage_rows(stage_name="likes", entity_key=post_uri)
                quote_rows = stage_store.stage_rows(stage_name="quotes", entity_key=post_uri)
                reposted_rows = stage_store.stage_rows(stage_name="reposted_by", entity_key=post_uri)
                thread_node_rows = stage_store.stage_rows(stage_name="thread_nodes", entity_key=post_uri)
                thread_edge_rows = stage_store.stage_rows(stage_name="thread_edges", entity_key=post_uri)
                _apply_like_rows(like_rows, post_uri=post_uri)
                _apply_quote_rows(quote_rows, post_uri=post_uri)
                _apply_reposted_rows(reposted_rows, post_uri=post_uri)
                _apply_thread_rows(thread_node_rows, thread_edge_rows, post_uri=post_uri)
                completed_focus_posts.append(post_uri)
                progress_state.feeds_done += 1
                continue
            focus_missing.append(post_uri)

        async def _bounded_process(post: dict[str, Any]) -> FocusPostResult:
            return await _bounded_call(
                _process_focus_post,
                http=http,
                run_id=run_id,
                vantage_id=vantage_value,
                post=post,
                cfg=cfg,
                max_items=max_items,
            )

        tasks = [_bounded_process(hydrated_posts_by_uri[post_uri]) for post_uri in focus_missing if post_uri in hydrated_posts_by_uri]
        processed_focus_posts: set[str] = set()
        for result in await asyncio.gather(*tasks) if tasks else []:
            processed_focus_posts.add(result.post_uri)
            completed_focus_posts.append(result.post_uri)
            _stage_write("likes", result.post_uri, result.likes_rows)
            _stage_write("quotes", result.post_uri, result.quote_rows)
            _stage_write("reposted_by", result.post_uri, result.reposted_rows)
            _stage_write("thread_nodes", result.post_uri, result.thread_node_rows)
            _stage_write("thread_edges", result.post_uri, result.thread_edge_rows)
            for did, scopes in result.actor_scope_additions.items():
                actor_scopes.setdefault(did, set()).update(scopes)
            labeler_dids.update(result.labeler_dids)
            _apply_like_rows(result.likes_rows, post_uri=result.post_uri)
            _apply_quote_rows(result.quote_rows, post_uri=result.post_uri)
            _apply_reposted_rows(result.reposted_rows, post_uri=result.post_uri)
            _apply_thread_rows(result.thread_node_rows, result.thread_edge_rows, post_uri=result.post_uri)
            progress_state.add_rows("likes", len(result.likes_rows))
            progress_state.add_rows("quotes", len(result.quote_rows))
            progress_state.add_rows("reposted_by", len(result.reposted_rows))
            progress_state.add_rows("thread_nodes", len(result.thread_node_rows))
            progress_state.add_rows("thread_edges", len(result.thread_edge_rows))
            progress_state.feeds_done += 1

        for post_uri in focus_missing:
            if post_uri in processed_focus_posts:
                continue
            for stage_name in _FOCUS_STAGE_NAMES:
                _stage_write(stage_name, post_uri, [])
            completed_focus_posts.append(post_uri)
            progress_state.feeds_done += 1

        # Keep the seed-only actor lanes anchored on the selected post authors.
        # actor_scopes also accumulates likers, quoters, reposters, thread actors,
        # feed creators, and other discovered accounts; using the whole map here
        # causes relationship and graph hydration to fan out on popular posts.
        seed_actor_dids = (
            sorted(
                {
                    did
                    for did, scopes in actor_scopes.items()
                    if did and ("post_author" in scopes or "surface_author" in scopes)
                }
            )
            if run_graph_stage
            else []
        )

        async def _hydrate_actor_profiles(actor_dids: list[str]) -> None:
            missing_actor_dids = _replay_completed_stage(
                stage_name="actor_profiles",
                entity_keys=actor_dids,
                apply_rows=_apply_actor_profile_rows,
                progress_counter="actor_profiles",
            )
            if not missing_actor_dids:
                return
            profiles = await _get_profiles(http, missing_actor_dids, format_utc(now_utc()))
            returned_rows: dict[str, dict[str, Any]] = {}
            for profile in profiles:
                did = str(profile.get("did") or "").strip()
                if not did:
                    continue
                row = {
                    "run_id": str(run_id),
                    "vantage_id": vantage_value,
                    "actor_scope": _actor_scope_value(actor_scopes, did),
                    "actor_did": did,
                    **flatten_profile_view_detailed(profile),
                    "raw_json": json_compact(profile),
                    "captured_at_utc": format_utc(now_utc()),
                }
                returned_rows[did] = row
                _stage_write("actor_profiles", did, [row])
                _apply_actor_profile_rows(did, [row])
                progress_state.add_rows("actor_profiles", 1)
            for did in missing_actor_dids:
                if did not in returned_rows:
                    _stage_write("actor_profiles", did, [])

        if run_graph_stage and seed_actor_dids:
            progress_state.set_detail("phase", "hydrating_seed_actor_profiles")
            await _hydrate_actor_profiles(seed_actor_dids)

        task_concurrency = max(1, int(concurrency or 1))
        semaphore = asyncio.Semaphore(task_concurrency)

        async def _bounded_call(fn, /, *args, **kwargs):  # noqa: ANN001, ANN202
            async with semaphore:
                return await fn(*args, **kwargs)

        # Full pairwise public relationship sweep over the seed actor set.
        if run_graph_stage:
            progress_state.set_detail("phase", "hydrating_seed_relationships")

        async def _fetch_seed_actor_relationships(actor_did: str) -> dict[str, Any]:
            captured_at_utc = format_utc(now_utc())
            rels = await _get_relationships(http, actor_did, seed_actor_dids, captured_at_utc)
            rows = [
                _relationship_row(
                    run_id=run_id,
                    vantage_id=vantage_value,
                    actor_did=actor_did,
                    context_scope="seed_actor_pairwise",
                    context_post_uri=None,
                    rel=rel,
                    captured_at_utc=captured_at_utc,
                )
                for rel in rels
                if isinstance(rel, dict) and rel.get("did")
            ]
            return {
                "actor_did": actor_did,
                "rows": rows,
                "other_dids": [str(row.get("other_did") or "").strip() for row in rows],
            }

        rel_missing = (
            _replay_completed_stage(
                stage_name="relationships",
                entity_keys=seed_actor_dids,
                apply_rows=_apply_relationship_rows,
                progress_counter="relationships",
            )
            if run_graph_stage
            else []
        )
        rel_tasks = [_bounded_call(_fetch_seed_actor_relationships, actor_did) for actor_did in rel_missing]
        for result in await asyncio.gather(*rel_tasks) if rel_tasks else []:
            actor_did = str(result.get("actor_did") or "")
            rows = list(result.get("rows") or [])
            _stage_write("relationships", actor_did, rows)
            if rows:
                progress_state.add_rows("relationships", len(rows))
            _apply_relationship_rows(actor_did, rows)

        # First-hop graph for every seed actor.
        if run_graph_stage:
            progress_state.set_detail("phase", "hydrating_first_hop_graph")

        async def _fetch_seed_actor_graph(actor_did: str, *, need_followers: bool, need_follows: bool) -> dict[str, Any]:
            actor_scope_value = _actor_scope_value(actor_scopes, actor_did)
            scope_additions: dict[str, set[str]] = {}
            labelers: set[str] = set()
            discovered_graph_dids: set[str] = set()
            follower_rows: list[dict[str, Any]] = []
            follow_rows: list[dict[str, Any]] = []

            if need_followers and (max_followers is None or max_followers > 0):
                followers = await _fetch_paginated(
                    http=http,
                    endpoint="app.bsky.graph.getFollowers",
                    method="app.bsky.graph.getFollowers",
                    params={"actor": actor_did},
                    feed_uri=None,
                    captured_at_utc=format_utc(now_utc()),
                    max_items=max_followers,
                    list_keys=("followers",),
                    allow_missing_actor_not_found=True,
                )
                for follower in followers:
                    flat = flatten_actor_view(follower)
                    follower_did = flat.get("did")
                    _add_scope(scope_additions, follower_did, "follower_of_seed")
                    if follower_did:
                        discovered_graph_dids.add(str(follower_did))
                    labelers.update(extract_label_src_dids(follower.get("labels")))
                    follower_rows.append(
                        {
                            "run_id": str(run_id),
                            "vantage_id": vantage_value,
                            "actor_scope": actor_scope_value,
                            "actor_did": actor_did,
                            "follower_did": flat.get("did"),
                            "follower_handle": flat.get("handle"),
                            "follower_display_name": flat.get("display_name"),
                            "follower_description": flat.get("description"),
                            "follower_avatar": flat.get("avatar"),
                            "follower_associated_json": flat.get("associated_json"),
                            "follower_indexed_at": flat.get("indexed_at"),
                            "follower_created_at": flat.get("created_at"),
                            "follower_labels_json": flat.get("labels_json"),
                            "follower_viewer_muted": flat.get("viewer_muted"),
                            "follower_viewer_blocked_by": flat.get("viewer_blocked_by"),
                            "raw_json": json_compact(follower),
                            "captured_at_utc": format_utc(now_utc()),
                        }
                    )

            if need_follows and (max_follows is None or max_follows > 0):
                follows = await _fetch_paginated(
                    http=http,
                    endpoint="app.bsky.graph.getFollows",
                    method="app.bsky.graph.getFollows",
                    params={"actor": actor_did},
                    feed_uri=None,
                    captured_at_utc=format_utc(now_utc()),
                    max_items=max_follows,
                    list_keys=("follows",),
                    allow_missing_actor_not_found=True,
                )
                for subject in follows:
                    flat = flatten_actor_view(subject)
                    subject_did = flat.get("did")
                    _add_scope(scope_additions, subject_did, "followed_by_seed")
                    if subject_did:
                        discovered_graph_dids.add(str(subject_did))
                    labelers.update(extract_label_src_dids(subject.get("labels")))
                    follow_rows.append(
                        {
                            "run_id": str(run_id),
                            "vantage_id": vantage_value,
                            "actor_scope": actor_scope_value,
                            "actor_did": actor_did,
                            "subject_did": flat.get("did"),
                            "subject_handle": flat.get("handle"),
                            "subject_display_name": flat.get("display_name"),
                            "subject_description": flat.get("description"),
                            "subject_avatar": flat.get("avatar"),
                            "subject_associated_json": flat.get("associated_json"),
                            "subject_indexed_at": flat.get("indexed_at"),
                            "subject_created_at": flat.get("created_at"),
                            "subject_labels_json": flat.get("labels_json"),
                            "subject_viewer_muted": flat.get("viewer_muted"),
                            "subject_viewer_blocked_by": flat.get("viewer_blocked_by"),
                            "raw_json": json_compact(subject),
                            "captured_at_utc": format_utc(now_utc()),
                        }
                    )

            return {
                "actor_did": actor_did,
                "follower_rows": follower_rows,
                "follow_rows": follow_rows,
                "scope_additions": scope_additions,
                "labelers": labelers,
                "graph_actor_dids": discovered_graph_dids,
            }

        follower_missing = (
            set(
                _replay_completed_stage(
                    stage_name="followers",
                    entity_keys=seed_actor_dids,
                    apply_rows=_apply_follower_rows,
                    progress_counter="followers",
                )
            )
            if run_graph_stage
            else set()
        )
        follow_missing = (
            set(
                _replay_completed_stage(
                    stage_name="follows",
                    entity_keys=seed_actor_dids,
                    apply_rows=_apply_follow_rows,
                    progress_counter="follows",
                )
            )
            if run_graph_stage
            else set()
        )
        graph_tasks = [
            _bounded_call(
                _fetch_seed_actor_graph,
                actor_did,
                need_followers=actor_did in follower_missing,
                need_follows=actor_did in follow_missing,
            )
            for actor_did in seed_actor_dids
            if actor_did in follower_missing or actor_did in follow_missing
        ]
        for result in await asyncio.gather(*graph_tasks) if graph_tasks else []:
            actor_did = str(result.get("actor_did") or "")
            for did, scopes in (result.get("scope_additions") or {}).items():
                actor_scopes.setdefault(did, set()).update(scopes)
            labeler_dids.update(result.get("labelers") or set())
            graph_actor_dids.update(result.get("graph_actor_dids") or set())

            follower_rows = list(result.get("follower_rows") or [])
            if actor_did in follower_missing:
                _stage_write("followers", actor_did, follower_rows)
            if follower_rows:
                progress_state.add_rows("followers", len(follower_rows))
            _apply_follower_rows(actor_did, follower_rows)

            follow_rows = list(result.get("follow_rows") or [])
            if actor_did in follow_missing:
                _stage_write("follows", actor_did, follow_rows)
            if follow_rows:
                progress_state.add_rows("follows", len(follow_rows))
            _apply_follow_rows(actor_did, follow_rows)

        # Hydrate newly discovered graph actor profiles too, so the graph nodes carry covariates.
        graph_only_dids = sorted(graph_actor_dids - hydrated_actor_profiles) if run_graph_stage else []
        if run_graph_stage and graph_only_dids:
            progress_state.set_detail("phase", "hydrating_graph_actor_profiles")
            await _hydrate_actor_profiles(graph_only_dids)

        # Author history and curation surfaces for the seed actor universe.
        if run_repo_stage:
            progress_state.set_detail("phase", "hydrating_author_history_and_curation")

        async def _fetch_seed_actor_history(
            actor_did: str,
            *,
            need_author_feed: bool,
            need_actor_feeds: bool,
            need_actor_lists: bool,
            need_starter_packs: bool,
        ) -> dict[str, Any]:
            actor_scope_value = _actor_scope_value(actor_scopes, actor_did)
            scope_additions: dict[str, set[str]] = {}
            labelers: set[str] = set()
            author_feed_rows: list[dict[str, Any]] = []
            list_rows: list[dict[str, Any]] = []
            starter_pack_rows: list[dict[str, Any]] = []
            discovered_feed_uris: set[str] = set()
            discovered_list_uris: set[str] = set()
            discovered_starter_pack_uris: set[str] = set()

            if need_author_feed and (max_author_feed_items is None or max_author_feed_items > 0):
                items = await _fetch_paginated(
                    http=http,
                    endpoint="app.bsky.feed.getAuthorFeed",
                    method="app.bsky.feed.getAuthorFeed",
                    params={"actor": actor_did},
                    feed_uri=None,
                    captured_at_utc=format_utc(now_utc()),
                    max_items=max_author_feed_items,
                    list_keys=("feed",),
                    allow_missing_actor_not_found=True,
                )
                for item in items:
                    flat = flatten_author_feed_item(item)
                    _add_scope(scope_additions, flat.get("post_author_did"), "author_feed_post_author")
                    _add_scope(scope_additions, flat.get("reason_actor_did"), "author_feed_reason_actor")
                    post = item.get("post") if isinstance(item.get("post"), dict) else {}
                    author = post.get("author") if isinstance(post.get("author"), dict) else {}
                    labelers.update(extract_label_src_dids(post.get("labels")))
                    labelers.update(extract_label_src_dids(author.get("labels")))
                    author_feed_rows.append(
                        {
                            "run_id": str(run_id),
                            "vantage_id": vantage_value,
                            "actor_did": actor_did,
                            "actor_scope": actor_scope_value,
                            **flat,
                            "item_raw_json": json_compact(item),
                            "captured_at_utc": format_utc(now_utc()),
                        }
                    )

            if need_actor_feeds and (max_actor_feeds is None or max_actor_feeds > 0):
                feed_rows = await _fetch_paginated(
                    http=http,
                    endpoint="app.bsky.feed.getActorFeeds",
                    method="app.bsky.feed.getActorFeeds",
                    params={"actor": actor_did},
                    feed_uri=None,
                    captured_at_utc=format_utc(now_utc()),
                    max_items=max_actor_feeds,
                    list_keys=("feeds",),
                    allow_missing_actor_not_found=True,
                )
                for feed in feed_rows:
                    feed_uri = str(feed.get("uri") or "").strip()
                    if feed_uri:
                        discovered_feed_uris.add(feed_uri)
                    creator = feed.get("creator") if isinstance(feed.get("creator"), dict) else {}
                    _add_scope(scope_additions, creator.get("did"), "feed_creator")
                    labelers.update(extract_label_src_dids(feed.get("labels")))

            if need_actor_lists and (max_lists is None or max_lists > 0):
                lists = await _fetch_paginated(
                    http=http,
                    endpoint="app.bsky.graph.getLists",
                    method="app.bsky.graph.getLists",
                    params={"actor": actor_did},
                    feed_uri=None,
                    captured_at_utc=format_utc(now_utc()),
                    max_items=max_lists,
                    list_keys=("lists",),
                    allow_missing_actor_not_found=True,
                )
                for list_view in lists:
                    flat = flatten_list_view(list_view)
                    list_uri = str(flat.get("list_uri") or "").strip()
                    if list_uri:
                        discovered_list_uris.add(list_uri)
                    _add_scope(scope_additions, flat.get("creator_did"), "list_creator")
                    labelers.update(extract_label_src_dids(list_view.get("labels")))
                    list_rows.append(
                        {
                            "run_id": str(run_id),
                            "vantage_id": vantage_value,
                            "actor_did": actor_did,
                            "actor_scope": actor_scope_value,
                            **flat,
                            "raw_json": json_compact(list_view),
                            "captured_at_utc": format_utc(now_utc()),
                        }
                    )

            if need_starter_packs and (max_starter_packs is None or max_starter_packs > 0):
                packs = await _fetch_paginated(
                    http=http,
                    endpoint="app.bsky.graph.getActorStarterPacks",
                    method="app.bsky.graph.getActorStarterPacks",
                    params={"actor": actor_did},
                    feed_uri=None,
                    captured_at_utc=format_utc(now_utc()),
                    max_items=max_starter_packs,
                    list_keys=("starterPacks",),
                    allow_missing_actor_not_found=True,
                )
                for pack in packs:
                    flat = flatten_starter_pack_view(pack)
                    pack_uri = str(flat.get("starter_pack_uri") or "").strip()
                    if pack_uri:
                        discovered_starter_pack_uris.add(pack_uri)
                    list_uri = str(flat.get("list_uri") or "").strip()
                    if list_uri:
                        discovered_list_uris.add(list_uri)
                    _add_scope(scope_additions, flat.get("creator_did"), "starter_pack_creator")
                    labelers.update(extract_label_src_dids(pack.get("labels")))
                    starter_pack_rows.append(
                        {
                            "run_id": str(run_id),
                            "vantage_id": vantage_value,
                            "actor_did": actor_did,
                            "actor_scope": actor_scope_value,
                            **flat,
                            "raw_json": json_compact(pack),
                            "captured_at_utc": format_utc(now_utc()),
                        }
                    )

            return {
                "actor_did": actor_did,
                "author_feed_rows": author_feed_rows,
                "list_rows": list_rows,
                "starter_pack_rows": starter_pack_rows,
                "feed_uris": discovered_feed_uris,
                "list_uris": discovered_list_uris,
                "starter_pack_uris": discovered_starter_pack_uris,
                "scope_additions": scope_additions,
                "labelers": labelers,
            }

        author_feed_missing = (
            set(
                _replay_completed_stage(
                    stage_name="author_feed",
                    entity_keys=seed_actor_dids,
                    apply_rows=_apply_author_feed_rows,
                    progress_counter="author_feed_items",
                )
            )
            if run_repo_stage
            else set()
        )
        actor_feed_catalog_missing = (
            set(
                _replay_completed_stage(
                    stage_name=_ACTOR_FEED_CATALOG_STAGE,
                    entity_keys=seed_actor_dids,
                    apply_rows=_apply_actor_feed_catalog_rows,
                )
            )
            if run_repo_stage
            else set()
        )
        actor_lists_missing = (
            set(
                _replay_completed_stage(
                    stage_name="actor_lists",
                    entity_keys=seed_actor_dids,
                    apply_rows=_apply_actor_list_rows,
                    progress_counter="actor_lists",
                )
            )
            if run_repo_stage
            else set()
        )
        starter_packs_missing = (
            set(
                _replay_completed_stage(
                    stage_name="starter_packs",
                    entity_keys=seed_actor_dids,
                    apply_rows=_apply_starter_pack_rows,
                    progress_counter="starter_packs",
                )
            )
            if run_repo_stage
            else set()
        )
        history_tasks = [
            _bounded_call(
                _fetch_seed_actor_history,
                actor_did,
                need_author_feed=actor_did in author_feed_missing,
                need_actor_feeds=actor_did in actor_feed_catalog_missing,
                need_actor_lists=actor_did in actor_lists_missing,
                need_starter_packs=actor_did in starter_packs_missing,
            )
            for actor_did in seed_actor_dids
            if (
                actor_did in author_feed_missing
                or actor_did in actor_feed_catalog_missing
                or actor_did in actor_lists_missing
                or actor_did in starter_packs_missing
            )
        ]
        for result in await asyncio.gather(*history_tasks) if history_tasks else []:
            actor_did = str(result.get("actor_did") or "")
            for did, scopes in (result.get("scope_additions") or {}).items():
                actor_scopes.setdefault(did, set()).update(scopes)
            labeler_dids.update(result.get("labelers") or set())

            author_feed_rows = list(result.get("author_feed_rows") or [])
            if actor_did in author_feed_missing:
                _stage_write("author_feed", actor_did, author_feed_rows)
            if author_feed_rows:
                progress_state.add_rows("author_feed_items", len(author_feed_rows))
            _apply_author_feed_rows(actor_did, author_feed_rows)

            actor_feed_catalog_rows = [
                {"actor_did": actor_did, "feed_uri": feed_uri}
                for feed_uri in sorted(result.get("feed_uris") or set())
                if str(feed_uri).strip()
            ]
            if actor_did in actor_feed_catalog_missing:
                _stage_write(_ACTOR_FEED_CATALOG_STAGE, actor_did, actor_feed_catalog_rows)
            _apply_actor_feed_catalog_rows(actor_did, actor_feed_catalog_rows)

            list_rows = list(result.get("list_rows") or [])
            if actor_did in actor_lists_missing:
                _stage_write("actor_lists", actor_did, list_rows)
            if list_rows:
                progress_state.add_rows("actor_lists", len(list_rows))
            _apply_actor_list_rows(actor_did, list_rows)

            starter_pack_rows = list(result.get("starter_pack_rows") or [])
            if actor_did in starter_packs_missing:
                _stage_write("starter_packs", actor_did, starter_pack_rows)
            if starter_pack_rows:
                progress_state.add_rows("starter_packs", len(starter_pack_rows))
            _apply_starter_pack_rows(actor_did, starter_pack_rows)

        # Hydrate lists and starter packs in detail.
        if run_repo_stage:
            progress_state.set_detail("phase", "hydrating_lists_and_starter_packs")

        async def _hydrate_list_members(list_uri: str) -> dict[str, Any]:
            resp_items = await _fetch_paginated(
                http=http,
                endpoint="app.bsky.graph.getList",
                method="app.bsky.graph.getList",
                params={"list": list_uri},
                feed_uri=None,
                captured_at_utc=format_utc(now_utc()),
                max_items=max_list_members,
                list_keys=("items",),
            )
            list_actor_did = _did_from_at_uri(list_uri)
            list_scope = _actor_scope_value(actor_scopes, list_actor_did) if list_actor_did else ""
            rows: list[dict[str, Any]] = []
            scope_additions: dict[str, set[str]] = {}
            labelers: set[str] = set()
            for item in resp_items:
                flat = flatten_list_item(item)
                _add_scope(scope_additions, flat.get("subject_did"), "list_member")
                labelers.update(extract_label_src_dids(item.get("subject", {}).get("labels") if isinstance(item.get("subject"), dict) else None))
                rows.append(
                    {
                        "run_id": str(run_id),
                        "vantage_id": vantage_value,
                        "actor_did": list_actor_did,
                        "actor_scope": list_scope,
                        "list_uri": list_uri,
                        **flat,
                        "raw_json": json_compact(item),
                        "captured_at_utc": format_utc(now_utc()),
                    }
                )
            return {"rows": rows, "scope_additions": scope_additions, "labelers": labelers}

        hydrated_list_uris: set[str] = set()

        async def _run_list_hydration(list_uris: set[str]) -> None:
            pending = sorted(set(list_uris) - hydrated_list_uris)
            if not pending:
                return
            list_missing = _replay_completed_stage(
                stage_name="list_members",
                entity_keys=pending,
                apply_rows=_apply_list_member_rows,
                progress_counter="list_members",
            )
            list_tasks = [_bounded_call(_hydrate_list_members, list_uri) for list_uri in list_missing]
            gathered = await asyncio.gather(*list_tasks) if list_tasks else []
            for list_uri, result in zip(list_missing, gathered):
                for did, scopes in (result.get("scope_additions") or {}).items():
                    actor_scopes.setdefault(did, set()).update(scopes)
                labeler_dids.update(result.get("labelers") or set())
                rows = list(result.get("rows") or [])
                _stage_write("list_members", list_uri, rows)
                if rows:
                    progress_state.add_rows("list_members", len(rows))
                _apply_list_member_rows(list_uri, rows)
            hydrated_list_uris.update(pending)

        if run_repo_stage:
            await _run_list_hydration(list_uris_to_fetch)

        async def _hydrate_starter_pack(starter_pack_uri: str) -> dict[str, Any]:
            resp = await http.xrpc_get(
                endpoint="app.bsky.graph.getStarterPack",
                host=http.hosts.appview_host,
                method="app.bsky.graph.getStarterPack",
                params={"starterPack": starter_pack_uri},
                access_jwt=None,
                feed_uri=None,
                timestamp_utc=format_utc(now_utc()),
            )
            pack = resp.data.get("starterPack") if isinstance(resp.data.get("starterPack"), dict) else {}
            actor_did = _did_from_at_uri(starter_pack_uri)
            actor_scope_value = _actor_scope_value(actor_scopes, actor_did) if actor_did else ""
            feeds = pack.get("feeds") if isinstance(pack.get("feeds"), list) else []
            slot_rows: list[dict[str, Any]] = []
            discovered_feed_uris: set[str] = set()
            discovered_list_uris: set[str] = set()
            labelers: set[str] = set(extract_label_src_dids(pack.get("labels")))
            scope_additions: dict[str, set[str]] = {}
            for idx, feed in enumerate(feeds, start=1):
                if not isinstance(feed, dict):
                    continue
                feed_uri = str(feed.get("uri") or "").strip()
                if feed_uri:
                    discovered_feed_uris.add(feed_uri)
                slot_rows.append(
                    {
                        "run_id": str(run_id),
                        "vantage_id": vantage_value,
                        "actor_did": actor_did,
                        "actor_scope": actor_scope_value,
                        "starter_pack_uri": starter_pack_uri,
                        "relation_type": "feed",
                        "slot_no": idx,
                        "feed_uri": feed_uri,
                        "list_uri": None,
                        "raw_json": json_compact(feed),
                        "captured_at_utc": format_utc(now_utc()),
                    }
                )
            list_view = pack.get("list") if isinstance(pack.get("list"), dict) else {}
            if list_view.get("uri"):
                list_uri = str(list_view.get("uri"))
                discovered_list_uris.add(list_uri)
                slot_rows.append(
                    {
                        "run_id": str(run_id),
                        "vantage_id": vantage_value,
                        "actor_did": actor_did,
                        "actor_scope": actor_scope_value,
                        "starter_pack_uri": starter_pack_uri,
                        "relation_type": "list",
                        "slot_no": 1,
                        "feed_uri": None,
                        "list_uri": list_uri,
                        "raw_json": json_compact(list_view),
                        "captured_at_utc": format_utc(now_utc()),
                    }
                )
            creator = pack.get("creator") if isinstance(pack.get("creator"), dict) else {}
            _add_scope(scope_additions, creator.get("did"), "starter_pack_creator")
            return {
                "actor_did": actor_did,
                "slot_rows": slot_rows,
                "feed_uris": discovered_feed_uris,
                "list_uris": discovered_list_uris,
                "scope_additions": scope_additions,
                "labelers": labelers,
            }

        pending_starter_pack_uris = sorted(starter_pack_uris) if run_repo_stage else []
        starter_pack_missing = (
            _replay_completed_stage(
                stage_name="starter_pack_contents",
                entity_keys=pending_starter_pack_uris,
                apply_rows=_apply_starter_pack_content_rows,
                progress_counter="starter_pack_contents",
            )
            if run_repo_stage
            else []
        )
        starter_pack_tasks = [_bounded_call(_hydrate_starter_pack, starter_pack_uri) for starter_pack_uri in starter_pack_missing]
        gathered_starter_packs = await asyncio.gather(*starter_pack_tasks) if starter_pack_tasks else []
        for starter_pack_uri, result in zip(starter_pack_missing, gathered_starter_packs):
            actor_did = str(result.get("actor_did") or "")
            for did, scopes in (result.get("scope_additions") or {}).items():
                actor_scopes.setdefault(did, set()).update(scopes)
            labeler_dids.update(result.get("labelers") or set())
            slot_rows = list(result.get("slot_rows") or [])
            _stage_write("starter_pack_contents", starter_pack_uri, slot_rows)
            if slot_rows:
                progress_state.add_rows("starter_pack_contents", len(slot_rows))
            _apply_starter_pack_content_rows(starter_pack_uri, slot_rows)

        if run_repo_stage:
            await _run_list_hydration(list_uris_to_fetch)

        # Follow-record lane: resolve each repo to its PDS, describe it, then list app.bsky.graph.follow records.
        if run_repo_stage:
            progress_state.set_detail("phase", "hydrating_follow_records")
        follow_record_actors = set(seed_actor_dids) if run_repo_stage else set()
        if run_repo_stage and bool(cfg.resolve_pds_endpoints) and str(cfg.follow_record_scope).strip().lower() in {"seed+graph", "all", "seed_and_graph"}:
            follow_record_actors.update(graph_actor_dids)
        ordered_follow_record_actors = sorted(follow_record_actors)

        if run_repo_stage and bool(cfg.resolve_pds_endpoints):
            did_missing = set(
                _replay_completed_stage(
                    stage_name="dids",
                    entity_keys=ordered_follow_record_actors,
                    apply_rows=lambda _entity_key, _rows: None,
                )
            )
        else:
            did_missing = set()
        repo_desc_missing = (
            set(
                _replay_completed_stage(
                    stage_name="repo_desc",
                    entity_keys=ordered_follow_record_actors,
                    apply_rows=lambda _entity_key, _rows: None,
                    progress_counter="repo_descriptions",
                )
            )
            if run_repo_stage
            else set()
        )
        follow_records_missing = (
            set(
                _replay_completed_stage(
                    stage_name="follow_records",
                    entity_keys=ordered_follow_record_actors,
                    apply_rows=_apply_follow_record_rows,
                    progress_counter="follow_records",
                )
            )
            if run_repo_stage
            else set()
        )

        async def _fetch_follow_record_lane(
            actor_did: str,
            *,
            need_did: bool,
            need_repo_desc: bool,
            need_follow_records: bool,
        ) -> dict[str, Any]:
            resolved_host: str | None = None
            did_row: dict[str, Any] | None = None
            if bool(cfg.resolve_pds_endpoints):
                if need_did:
                    resolved_host, did_row = await _resolve_pds(did_resolver, actor_did, format_utc(now_utc()), run_id, vantage_value)
                else:
                    resolved_host = _resolved_host_from_did_rows(stage_store.stage_rows(stage_name="dids", entity_key=actor_did))
            host = resolved_host or hosts.pds_host
            actor_scope_value = _actor_scope_value(actor_scopes, actor_did)
            repo_desc_row: dict[str, Any] | None = None
            follow_rows: list[dict[str, Any]] = []
            scope_additions: dict[str, set[str]] = {}
            if need_repo_desc:
                try:
                    repo_desc_resp = await http.xrpc_get(
                        endpoint="com.atproto.repo.describeRepo",
                        host=host,
                        method="com.atproto.repo.describeRepo",
                        params={"repo": actor_did},
                        access_jwt=None,
                        feed_uri=None,
                        timestamp_utc=format_utc(now_utc()),
                    )
                    repo_desc = repo_desc_resp.data if isinstance(repo_desc_resp.data, dict) else {}
                    repo_desc_row = {
                        "run_id": str(run_id),
                        "vantage_id": vantage_value,
                        "actor_scope": actor_scope_value,
                        "resolved_pds_host": host,
                        **flatten_repo_description(repo_desc),
                        "raw_json": json_compact(repo_desc),
                        "captured_at_utc": format_utc(now_utc()),
                    }
                except Exception:
                    logger.exception("describeRepo failed repo=%s host=%s", actor_did, host)

            if need_follow_records:
                try:
                    records = await _fetch_paginated(
                        http=http,
                        endpoint="com.atproto.repo.listRecords",
                        method="com.atproto.repo.listRecords",
                        params={"repo": actor_did, "collection": "app.bsky.graph.follow"},
                        feed_uri=None,
                        captured_at_utc=format_utc(now_utc()),
                        max_items=max_follow_records,
                        list_keys=("records",),
                    ) if host == http.hosts.appview_host else None
                except Exception:
                    records = None
                if records is None:
                    records = []
                    try:
                        cursor: str | None = None
                        remaining = None if max_follow_records is None else max(0, int(max_follow_records))
                        while remaining is None or remaining > 0:
                            limit = 100 if remaining is None else min(100, remaining)
                            params = {"repo": actor_did, "collection": "app.bsky.graph.follow", "limit": limit}
                            if cursor:
                                params["cursor"] = cursor
                            resp = await http.xrpc_get(
                                endpoint="com.atproto.repo.listRecords",
                                host=host,
                                method="com.atproto.repo.listRecords",
                                params=params,
                                access_jwt=None,
                                feed_uri=None,
                                timestamp_utc=format_utc(now_utc()),
                            )
                            page = _as_list(resp.data, "records")
                            if not page:
                                break
                            records.extend(page)
                            if remaining is not None:
                                remaining -= len(page)
                                if remaining <= 0:
                                    break
                            cursor = resp.data.get("cursor") if isinstance(resp.data.get("cursor"), str) else None
                            if not cursor:
                                break
                    except Exception:
                        logger.exception("listRecords failed repo=%s host=%s", actor_did, host)
                        records = []
                for record in records:
                    flat = flatten_follow_record(record)
                    _add_scope(scope_additions, flat.get("subject_did"), "follow_record_subject")
                    follow_rows.append(
                        {
                            "run_id": str(run_id),
                            "vantage_id": vantage_value,
                            "actor_scope": actor_scope_value,
                            "repo_did": actor_did,
                            "resolved_pds_host": host,
                            **flat,
                            "captured_at_utc": format_utc(now_utc()),
                        }
                    )
            return {
                "actor_did": actor_did,
                "did_row": did_row,
                "repo_desc_row": repo_desc_row,
                "follow_rows": follow_rows,
                "scope_additions": scope_additions,
            }

        follow_record_tasks = [
            _bounded_call(
                _fetch_follow_record_lane,
                actor_did,
                need_did=actor_did in did_missing,
                need_repo_desc=actor_did in repo_desc_missing,
                need_follow_records=actor_did in follow_records_missing,
            )
            for actor_did in ordered_follow_record_actors
            if actor_did in did_missing or actor_did in repo_desc_missing or actor_did in follow_records_missing
        ]
        for result in await asyncio.gather(*follow_record_tasks) if follow_record_tasks else []:
            actor_did = str(result.get("actor_did") or "")
            did_row = result.get("did_row")
            if actor_did in did_missing and isinstance(did_row, dict):
                _stage_write("dids", actor_did, [did_row])
            repo_desc_row = result.get("repo_desc_row")
            if actor_did in repo_desc_missing and isinstance(repo_desc_row, dict):
                _stage_write("repo_desc", actor_did, [repo_desc_row])
            if isinstance(repo_desc_row, dict):
                progress_state.add_rows("repo_descriptions", 1)
            for did, scopes in (result.get("scope_additions") or {}).items():
                actor_scopes.setdefault(did, set()).update(scopes)
            follow_rows = list(result.get("follow_rows") or [])
            if actor_did in follow_records_missing:
                _stage_write("follow_records", actor_did, follow_rows)
            if follow_rows:
                progress_state.add_rows("follow_records", len(follow_rows))
            _apply_follow_record_rows(actor_did, follow_rows)

        # Final feed generator hydration after all discovery surfaces are scanned.
        if run_repo_stage:
            progress_state.set_detail("phase", "hydrating_feed_metadata")

        async def _hydrate_feed_generator(feed_uri: str) -> dict[str, Any] | None:
            captured_at_utc = format_utc(now_utc())
            try:
                resp = await http.xrpc_get(
                    endpoint="app.bsky.feed.getFeedGenerator",
                    host=http.hosts.appview_host,
                    method="app.bsky.feed.getFeedGenerator",
                    params={"feed": feed_uri},
                    access_jwt=None,
                    feed_uri=feed_uri,
                    timestamp_utc=captured_at_utc,
                )
            except Exception:
                logger.exception("getFeedGenerator failed feed=%s", feed_uri)
                return None
            view = resp.data.get("view") if isinstance(resp.data.get("view"), dict) else {}
            flat = flatten_generator_view(view)
            scope_additions: dict[str, set[str]] = {}
            _add_scope(scope_additions, flat.get("creator_did"), "feed_creator")
            labelers = set(extract_label_src_dids(view.get("labels")))
            row = {
                "run_id": str(run_id),
                "vantage_id": vantage_value,
                "source_scope": "|".join(sorted(feed_sources.get(feed_uri, set()))),
                "source_actor_dids": json_compact(sorted(a for a in feed_source_actors.get(feed_uri, set()) if a)),
                **flat,
                "is_online": 1 if resp.data.get("isOnline") is True else 0 if resp.data.get("isOnline") is False else None,
                "is_valid": 1 if resp.data.get("isValid") is True else 0 if resp.data.get("isValid") is False else None,
                "raw_json": json_compact(resp.data),
                "labelers_requested": accept_labelers,
                "labelers_included": resp.content_labelers,
                "captured_at_utc": captured_at_utc,
            }
            return {"row": row, "scope_additions": scope_additions, "labelers": labelers}

        ordered_feed_uris = sorted(feed_sources) if run_repo_stage else []
        feed_missing = (
            _replay_completed_stage(
                stage_name="feed_generators",
                entity_keys=ordered_feed_uris,
                apply_rows=_apply_feed_generator_rows,
                progress_counter="feed_generators",
            )
            if run_repo_stage
            else []
        )
        feed_tasks = [_bounded_call(_hydrate_feed_generator, feed_uri) for feed_uri in feed_missing]
        for result in await asyncio.gather(*feed_tasks) if feed_tasks else []:
            if not isinstance(result, dict):
                continue
            for did, scopes in (result.get("scope_additions") or {}).items():
                actor_scopes.setdefault(did, set()).update(scopes)
            labeler_dids.update(result.get("labelers") or set())
            row = result.get("row")
            if isinstance(row, dict):
                feed_uri = str(row.get("feed_uri") or "").strip()
                if feed_uri:
                    _stage_write("feed_generators", feed_uri, [row])
                    _apply_feed_generator_rows(feed_uri, [row])
                progress_state.add_rows("feed_generators", 1)

        # Final actor-profile sweep for actors discovered later in lists, follow records, and feed hydration.
        final_actor_dids = sorted(set(actor_scopes) - hydrated_actor_profiles) if run_repo_stage else []
        if run_repo_stage and final_actor_dids:
            progress_state.set_detail("phase", "hydrating_final_actor_profiles")
            await _hydrate_actor_profiles(final_actor_dids)

        # Labeler service metadata for every label source DID we saw.
        if run_repo_stage and labeler_dids:
            progress_state.set_detail("phase", "hydrating_labeler_services")

            async def _fetch_labeler_batch(batch: list[str]) -> list[dict[str, Any]]:
                try:
                    resp = await http.xrpc_get(
                        endpoint="app.bsky.labeler.getServices",
                        host=http.hosts.appview_host,
                        method="app.bsky.labeler.getServices",
                        params={"dids": batch, "detailed": True},
                        access_jwt=None,
                        feed_uri=None,
                        timestamp_utc=format_utc(now_utc()),
                    )
                except Exception:
                    logger.exception("getServices failed dids=%s", batch)
                    return []
                rows = []
                for view in _as_list(resp.data, "views"):
                    rows.append(
                        {
                            "run_id": str(run_id),
                            "vantage_id": vantage_value,
                            **flatten_labeler_service_view(view),
                            "raw_json": json_compact(view),
                            "captured_at_utc": format_utc(now_utc()),
                        }
                    )
                return rows

            ordered_labeler_dids = sorted(labeler_dids)
            labeler_missing = _replay_completed_stage(
                stage_name="labelers",
                entity_keys=ordered_labeler_dids,
                apply_rows=lambda _entity_key, _rows: None,
                progress_counter="labeler_services",
            )
            labeler_batches = list(_chunked(labeler_missing, 25))
            labeler_tasks = [_bounded_call(_fetch_labeler_batch, batch) for batch in labeler_batches]
            for batch, rows in zip(labeler_batches, await asyncio.gather(*labeler_tasks) if labeler_tasks else []):
                rows_by_did: dict[str, dict[str, Any]] = {}
                for row in rows:
                    did = str(row.get("labeler_did") or "").strip()
                    if did:
                        rows_by_did[did] = row
                for did in batch:
                    row = rows_by_did.get(did)
                    _stage_write("labelers", did, [row] if isinstance(row, dict) else [])
                if rows:
                    progress_state.add_rows("labeler_services", len(rows))

        # Summaries per focus post.
        progress_state.set_detail("phase", "writing_post_summaries")
        summary_rows = []
        for post_uri in post_uris:
            summary_rows.append(
                {
                    "run_id": str(run_id),
                    "vantage_id": vantage_value,
                    "post_uri": post_uri,
                    "post_author_did": post_author_by_post.get(post_uri),
                    **summary_counts.get(post_uri, {}),
                    "captured_at_utc": format_utc(now_utc()),
                }
            )
        if summary_rows:
            _stage_write_grouped("summary", "post_uri", summary_rows)
            progress_state.add_rows("post_summaries", len(summary_rows))

        if completed_focus_posts:
            progress_state.set_detail("phase", "marking_hydrated")
            with ControlState.open(layout.control_db_path) as control:
                control.mark_posts_rq1_factors_hydrated(
                    post_uris=[PostUri(uri) for uri in sorted(set(completed_focus_posts))],
                    hydrated_at_utc=format_utc(now_utc()),
                    stage=rq1_stage,
                )
                control.commit()

        success = True
        progress_state.set_detail("phase", "complete")
    finally:
        try:
            _materialize_stage_outputs()
        finally:
            stage_store.close()
            if http.request_provenance_writer is not None:
                http.request_provenance_writer.close()
            http_stats_writer.close()
            progress_reporter.stop()
            manifest["finished_at_utc"] = format_utc(now_utc())
            manifest["success"] = bool(success)
            atomic_write_json(manifest_path, manifest)
            await http.aclose()


__all__ = ["BackfillRq1FactorsConfig", "run_backfill_rq1_factors"]
