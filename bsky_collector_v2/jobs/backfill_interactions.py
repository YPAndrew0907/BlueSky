from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable

from bsky_collector_v2.fs_utils import atomic_write_json, ensure_dir
from bsky_collector_v2.http_client import AsyncHttpClient, HttpRetryConfig, XrpcHosts
from bsky_collector_v2.instrumentation import enrich_manifest
from bsky_collector_v2.layout import Layout
from bsky_collector_v2.progress import ProgressReporter, ProgressState
from bsky_collector_v2.public_views import extract_post_record_features, flatten_profile_view_detailed
from bsky_collector_v2.request_provenance import JobRequestContextFactory, RequestProvenanceWriter
from bsky_collector_v2.state import ControlState
from bsky_collector_v2.time_utils import format_utc, now_utc, utc_date_str
from bsky_collector_v2.types import PostUri, RunId
from bsky_collector_v2.writers import CsvPartWriter

logger = logging.getLogger("bsky_collector_v2.job.backfill_interactions")

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
    "labelers_requested",
    "labelers_included",
    "captured_at_utc",
)

_SUMMARY_FIELDS: tuple[str, ...] = (
    "run_id",
    "vantage_id",
    "post_uri",
    "post_author_did",
    "likes_returned",
    "quotes_returned",
    "reposted_by_returned",
    "relationship_edges_returned",
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
    "captured_at_utc",
)

_RELATIONSHIP_FIELDS: tuple[str, ...] = (
    "run_id",
    "vantage_id",
    "post_uri",
    "post_author_did",
    "other_did",
    "following",
    "followed_by",
    "blocking",
    "blocked_by",
    "blocking_by_list_uri",
    "blocked_by_list_uri",
    "captured_at_utc",
)

_ACTOR_PROFILE_FIELDS: tuple[str, ...] = (
    "run_id",
    "vantage_id",
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
    "captured_at_utc",
)


@dataclass(frozen=True)
class BackfillInteractionsConfig:
    max_posts: int = 10_000
    batch_size: int = 25
    max_items_per_endpoint: int = 200
    seen_after_utc: str | None = None
    seen_before_utc: str | None = None
    include_hydrated: bool = False


def _as_list(data: dict[str, Any], *keys: str) -> list[dict[str, Any]]:
    for key in keys:
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _chunked(values: list[str], size: int) -> Iterable[list[str]]:
    for i in range(0, len(values), size):
        yield values[i : i + size]


def _selection_details(selected_rows: Iterable[Any]) -> dict[str, Any]:
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
        "selected_posts": len(rows),
        "selected_first_seen_min_utc": first_seen_values[0] if first_seen_values else None,
        "selected_first_seen_max_utc": first_seen_values[-1] if first_seen_values else None,
    }


def _normalized_max_items(value: int | None) -> int | None:
    if value is None:
        return None
    value = int(value)
    return None if value <= 0 else value


def _effective_max_items_detail(value: int | None) -> int | str:
    normalized = _normalized_max_items(value)
    return "uncapped" if normalized is None else int(normalized)


async def _fetch_paginated(
    *,
    http: AsyncHttpClient,
    endpoint: str,
    method: str,
    params: dict[str, Any],
    feed_uri: str,
    captured_at_utc: str,
    max_items: int | None,
    list_keys: tuple[str, ...],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    cursor: str | None = None
    remaining = _normalized_max_items(max_items)
    while remaining is None or len(out) < remaining:
        req_params = dict(params)
        req_params["limit"] = 100 if remaining is None else min(100, remaining - len(out))
        if cursor:
            req_params["cursor"] = cursor
        resp = await http.xrpc_get(
            endpoint=endpoint,
            host=http.hosts.appview_host,
            method=method,
            params=req_params,
            access_jwt=None,
            feed_uri=feed_uri,
            timestamp_utc=captured_at_utc,
        )
        items = _as_list(resp.data, *list_keys)
        if not items:
            break
        out.extend(items)
        cursor = resp.data.get("cursor") if isinstance(resp.data.get("cursor"), str) else None
        if not cursor:
            break
    return out if remaining is None else out[:remaining]


async def run_backfill_interactions(
    *,
    layout: Layout,
    hosts: XrpcHosts,
    run_id: RunId,
    rps: float,
    concurrency: int,
    dry_run: bool,
    cfg: BackfillInteractionsConfig | None = None,
    accept_language: str | None,
    accept_labelers: str | None,
    vantage_id: str,
) -> None:
    cfg = cfg or BackfillInteractionsConfig()
    date_str = utc_date_str(now_utc())
    out_dir = layout.interactions_day_dir(date_str)
    if dry_run:
        logger.info("dry_run=true: would backfill interactions out=%s", str(out_dir))
        return

    ensure_dir(out_dir)
    started_at_utc = format_utc(now_utc())
    manifest = {
        "run_id": str(run_id),
        "job_name": "backfill-interactions",
        "date_utc": date_str,
        "started_at_utc": started_at_utc,
        "params": {
            "date": date_str,
            "max_posts": cfg.max_posts,
            "batch_size": cfg.batch_size,
            "max_items_per_endpoint": cfg.max_items_per_endpoint,
            "seen_after_utc": cfg.seen_after_utc,
            "seen_before_utc": cfg.seen_before_utc,
            "include_hydrated": bool(cfg.include_hydrated),
            "accept_language": accept_language,
            "accept_labelers": accept_labelers,
            "vantage_id": str(vantage_id).strip() or "unauth",
        },
    }
    enrich_manifest(manifest, job_name="backfill-interactions", out_base=layout.out_base, params=manifest["params"])
    manifest["effective_limits"] = {"max_items_per_endpoint": _normalized_max_items(cfg.max_items_per_endpoint)}
    atomic_write_json(layout.interactions_manifest_json(date_str), manifest)

    progress_state = ProgressState(
        job_name="backfill-interactions",
        run_id=run_id,
        started_at_utc=started_at_utc,
        unit_label="posts",
    )
    progress_state.update_details(
        {
            "phase": "selecting_posts",
            "selection_order": "oldest_first",
            "effective_max_items_per_endpoint": _effective_max_items_detail(cfg.max_items_per_endpoint),
        }
    )
    progress_reporter = ProgressReporter(layout.interactions_progress_json(date_str), progress_state, write_interval_s=15.0)
    progress_reporter.start()
    http_stats_writer = CsvPartWriter(
        layout.interactions_http_stats_csv(date_str),
        fieldnames=["timestamp_utc", "endpoint", "status_code", "latency_ms", "attempt", "error_type", "feed_uri"],
    )
    vantage_value = str(vantage_id).strip() or "unauth"
    http = AsyncHttpClient(
        hosts=hosts,
        rps=rps,
        retry=HttpRetryConfig(max_retries=1),
        timeout_s=20.0,
        http_stats=http_stats_writer,
        progress=progress_state,
        accept_language=accept_language,
        accept_labelers=accept_labelers,
        request_provenance_writer=RequestProvenanceWriter(layout.interactions_request_provenance_csv(date_str)),
        request_context_factory=JobRequestContextFactory(
            run_id=str(run_id),
            job_name="backfill-interactions",
            sample_family=str(manifest.get("sample_family") or "interaction_backfill"),
            collection_params_hash=str(manifest.get("collection_params_hash") or ""),
            appview_host=hosts.appview_host,
            pds_host=hosts.pds_host,
            date_utc=date_str,
            viewer_mode="unauth",
            vantage_id=vantage_value,
        ),
    )

    post_view_writer = CsvPartWriter(out_dir / "post_views_part_000.csv", fieldnames=_POST_VIEW_FIELDS)
    summary_writer = CsvPartWriter(out_dir / "post_interaction_summary_part_000.csv", fieldnames=_SUMMARY_FIELDS)
    likes_writer = CsvPartWriter(out_dir / "post_likes_part_000.csv", fieldnames=_LIKE_FIELDS)
    quotes_writer = CsvPartWriter(out_dir / "post_quotes_part_000.csv", fieldnames=_QUOTE_FIELDS)
    reposted_by_writer = CsvPartWriter(out_dir / "post_reposted_by_part_000.csv", fieldnames=_REPOSTED_BY_FIELDS)
    relationships_writer = CsvPartWriter(out_dir / "relationship_edges_part_000.csv", fieldnames=_RELATIONSHIP_FIELDS)
    actor_profiles_writer = CsvPartWriter(out_dir / "interaction_actor_profiles_part_000.csv", fieldnames=_ACTOR_PROFILE_FIELDS)

    success = False
    processed_posts = 0

    try:
        with ControlState.open(layout.control_db_path) as control:
            control.start_run(
                run_id=run_id,
                job_name="backfill-interactions",
                started_at_utc=started_at_utc,
                params={
                    "date": date_str,
                    "max_posts": cfg.max_posts,
                    "batch_size": cfg.batch_size,
                    "max_items_per_endpoint": cfg.max_items_per_endpoint,
                    "seen_after_utc": cfg.seen_after_utc,
                    "seen_before_utc": cfg.seen_before_utc,
                    "include_hydrated": bool(cfg.include_hydrated),
                },
            )
            try:
                selected_rows = control.select_posts_to_backfill_rows(
                    limit=cfg.max_posts,
                    seen_after_utc=cfg.seen_after_utc,
                    seen_before_utc=cfg.seen_before_utc,
                    include_hydrated=bool(cfg.include_hydrated),
                )
                selection_details = _selection_details(selected_rows)
                manifest["selection"] = selection_details
                atomic_write_json(layout.interactions_manifest_json(date_str), manifest)
                progress_state.update_details({**selection_details, "phase": "hydrating_posts"})
                post_uris = [PostUri(str(row.post_uri)) for row in selected_rows]
                control.ensure_post_interaction_tasks(post_uris=post_uris, enqueued_at_utc=started_at_utc)
                control.commit()
                logger.info(
                    "backfill-interactions start posts=%s batch_size=%s first_seen_min=%s first_seen_max=%s",
                    len(post_uris),
                    cfg.batch_size,
                    selection_details.get("selected_first_seen_min_utc"),
                    selection_details.get("selected_first_seen_max_utc"),
                )
                progress_state.feeds_total = len(post_uris)

                all_actor_dids: set[str] = set()
                hydrated_successfully: list[PostUri] = []
                labelers_requested = accept_labelers

                for batch in _chunked([str(p) for p in post_uris], cfg.batch_size):
                    captured_at_utc = format_utc(now_utc())
                    resp = await http.xrpc_get(
                        endpoint="app.bsky.feed.getPosts",
                        host=http.hosts.appview_host,
                        method="app.bsky.feed.getPosts",
                        params={"uris": batch},
                        access_jwt=None,
                        feed_uri=",".join(batch[:3]),
                        timestamp_utc=captured_at_utc,
                    )
                    posts = _as_list(resp.data, "posts")
                    posts_by_uri = {
                        str(post.get("uri")): post for post in posts if isinstance(post.get("uri"), str) and post.get("uri")
                    }
                    labelers_included = resp.content_labelers

                    for post_uri in batch:
                        post = posts_by_uri.get(post_uri)
                        if not isinstance(post, dict):
                            continue
                        author = post.get("author") if isinstance(post.get("author"), dict) else {}
                        author_did = author.get("did") if isinstance(author.get("did"), str) else None
                        author_handle = author.get("handle") if isinstance(author.get("handle"), str) else None
                        post_features = extract_post_record_features(post)
                        post_view_writer.write_rows(
                            [
                                {
                                    "run_id": str(run_id),
                                    "vantage_id": vantage_value,
                                    "post_uri": post_uri,
                                    "post_cid": post.get("cid") if isinstance(post.get("cid"), str) else None,
                                    "author_did": author_did,
                                    "author_handle": author_handle,
                                    **post_features,
                                    "like_count": post.get("likeCount"),
                                    "repost_count": post.get("repostCount"),
                                    "reply_count": post.get("replyCount"),
                                    "quote_count": post.get("quoteCount"),
                                    "labelers_requested": labelers_requested,
                                    "labelers_included": labelers_included,
                                    "captured_at_utc": captured_at_utc,
                                }
                            ]
                        )

                        likes = await _fetch_paginated(
                            http=http,
                            endpoint="app.bsky.feed.getLikes",
                            method="app.bsky.feed.getLikes",
                            params={"uri": post_uri},
                            feed_uri=post_uri,
                            captured_at_utc=captured_at_utc,
                            max_items=cfg.max_items_per_endpoint,
                            list_keys=("likes",),
                        )
                        quotes = await _fetch_paginated(
                            http=http,
                            endpoint="app.bsky.feed.getQuotes",
                            method="app.bsky.feed.getQuotes",
                            params={"uri": post_uri},
                            feed_uri=post_uri,
                            captured_at_utc=captured_at_utc,
                            max_items=cfg.max_items_per_endpoint,
                            list_keys=("posts", "quotes"),
                        )
                        reposted_by = await _fetch_paginated(
                            http=http,
                            endpoint="app.bsky.feed.getRepostedBy",
                            method="app.bsky.feed.getRepostedBy",
                            params={"uri": post_uri},
                            feed_uri=post_uri,
                            captured_at_utc=captured_at_utc,
                            max_items=cfg.max_items_per_endpoint,
                            list_keys=("repostedBy", "actors"),
                        )

                        like_rows: list[dict[str, Any]] = []
                        quote_rows: list[dict[str, Any]] = []
                        repost_rows: list[dict[str, Any]] = []
                        relationship_rows: list[dict[str, Any]] = []
                        relationship_actor_dids: set[str] = set()

                        for like in likes:
                            actor = like.get("actor") if isinstance(like.get("actor"), dict) else {}
                            actor_did = actor.get("did") if isinstance(actor.get("did"), str) else None
                            if actor_did:
                                all_actor_dids.add(actor_did)
                                relationship_actor_dids.add(actor_did)
                            like_rows.append(
                                {
                                    "run_id": str(run_id),
                                    "vantage_id": vantage_value,
                                    "post_uri": post_uri,
                                    "post_author_did": author_did,
                                    "actor_did": actor_did,
                                    "actor_handle": actor.get("handle"),
                                    "actor_display_name": actor.get("displayName"),
                                    "created_at": like.get("createdAt"),
                                    "indexed_at": like.get("indexedAt"),
                                    "captured_at_utc": captured_at_utc,
                                }
                            )

                        for quote in quotes:
                            quote_author = quote.get("author") if isinstance(quote.get("author"), dict) else {}
                            quote_author_did = quote_author.get("did") if isinstance(quote_author.get("did"), str) else None
                            if quote_author_did:
                                all_actor_dids.add(quote_author_did)
                                relationship_actor_dids.add(quote_author_did)
                            quote_record = quote.get("record") if isinstance(quote.get("record"), dict) else {}
                            quote_rows.append(
                                {
                                    "run_id": str(run_id),
                                    "vantage_id": vantage_value,
                                    "post_uri": post_uri,
                                    "post_author_did": author_did,
                                    "quote_post_uri": quote.get("uri") if isinstance(quote.get("uri"), str) else None,
                                    "quote_post_cid": quote.get("cid") if isinstance(quote.get("cid"), str) else None,
                                    "quote_author_did": quote_author_did,
                                    "quote_author_handle": quote_author.get("handle"),
                                    "record_created_at": quote_record.get("createdAt") if isinstance(quote_record.get("createdAt"), str) else None,
                                    "indexed_at": quote.get("indexedAt") if isinstance(quote.get("indexedAt"), str) else None,
                                    "text": quote_record.get("text") if isinstance(quote_record.get("text"), str) else None,
                                    "like_count": quote.get("likeCount"),
                                    "repost_count": quote.get("repostCount"),
                                    "reply_count": quote.get("replyCount"),
                                    "quote_count": quote.get("quoteCount"),
                                    "captured_at_utc": captured_at_utc,
                                }
                            )

                        for actor in reposted_by:
                            actor_did = actor.get("did") if isinstance(actor.get("did"), str) else None
                            if actor_did:
                                all_actor_dids.add(actor_did)
                                relationship_actor_dids.add(actor_did)
                            repost_rows.append(
                                {
                                    "run_id": str(run_id),
                                    "vantage_id": vantage_value,
                                    "post_uri": post_uri,
                                    "post_author_did": author_did,
                                    "actor_did": actor_did,
                                    "actor_handle": actor.get("handle"),
                                    "actor_display_name": actor.get("displayName"),
                                    "captured_at_utc": captured_at_utc,
                                }
                            )

                        if author_did and relationship_actor_dids:
                            others_sorted = sorted(relationship_actor_dids)
                            for others_batch in _chunked(others_sorted, 30):
                                rel_resp = await http.xrpc_get(
                                    endpoint="app.bsky.graph.getRelationships",
                                    host=http.hosts.appview_host,
                                    method="app.bsky.graph.getRelationships",
                                    params={"actor": author_did, "others": others_batch},
                                    access_jwt=None,
                                    feed_uri=post_uri,
                                    timestamp_utc=captured_at_utc,
                                )
                                relationships = _as_list(rel_resp.data, "relationships")
                                for rel in relationships:
                                    relationship_rows.append(
                                        {
                                            "run_id": str(run_id),
                                            "vantage_id": vantage_value,
                                            "post_uri": post_uri,
                                            "post_author_did": author_did,
                                            "other_did": rel.get("did") if isinstance(rel.get("did"), str) else None,
                                            "following": rel.get("following"),
                                            "followed_by": rel.get("followedBy"),
                                            "blocking": rel.get("blocking"),
                                            "blocked_by": rel.get("blockedBy"),
                                            "blocking_by_list_uri": (
                                                rel.get("blockingByList", {}).get("uri")
                                                if isinstance(rel.get("blockingByList"), dict)
                                                else None
                                            ),
                                            "blocked_by_list_uri": (
                                                rel.get("blockedByList", {}).get("uri")
                                                if isinstance(rel.get("blockedByList"), dict)
                                                else None
                                            ),
                                            "captured_at_utc": captured_at_utc,
                                        }
                                    )

                        likes_writer.write_rows(like_rows)
                        quotes_writer.write_rows(quote_rows)
                        reposted_by_writer.write_rows(repost_rows)
                        relationships_writer.write_rows(relationship_rows)
                        summary_writer.write_rows(
                            [
                                {
                                    "run_id": str(run_id),
                                    "vantage_id": vantage_value,
                                    "post_uri": post_uri,
                                    "post_author_did": author_did,
                                    "likes_returned": len(like_rows),
                                    "quotes_returned": len(quote_rows),
                                    "reposted_by_returned": len(repost_rows),
                                    "relationship_edges_returned": len(relationship_rows),
                                    "captured_at_utc": captured_at_utc,
                                }
                            ]
                        )
                        hydrated_successfully.append(PostUri(post_uri))
                        processed_posts += 1
                        progress_state.feeds_done = processed_posts

                discovered_actor_dids = sorted(all_actor_dids)
                if discovered_actor_dids:
                    progress_state.set_detail("phase", "hydrating_actor_profiles")
                    control.upsert_author_registry_many(author_dids=discovered_actor_dids, seen_at_utc=format_utc(now_utc()))
                    control.commit()
                    for actor_batch in _chunked(discovered_actor_dids, 25):
                        captured_at_utc = format_utc(now_utc())
                        resp = await http.xrpc_get(
                            endpoint="app.bsky.actor.getProfiles",
                            host=http.hosts.appview_host,
                            method="app.bsky.actor.getProfiles",
                            params={"actors": actor_batch},
                            access_jwt=None,
                            feed_uri=None,
                            timestamp_utc=captured_at_utc,
                        )
                        profiles = _as_list(resp.data, "profiles")
                        rows = []
                        for profile in profiles:
                            did = profile.get("did") if isinstance(profile.get("did"), str) else None
                            if not did:
                                continue
                            rows.append(
                                {
                                    "run_id": str(run_id),
                                    "vantage_id": vantage_value,
                                    "actor_did": did,
                                    **flatten_profile_view_detailed(profile),
                                    "captured_at_utc": captured_at_utc,
                                }
                            )
                        actor_profiles_writer.write_rows(rows)

                post_view_writer.flush(force_fsync=False)
                summary_writer.flush(force_fsync=False)
                likes_writer.flush(force_fsync=False)
                quotes_writer.flush(force_fsync=False)
                reposted_by_writer.flush(force_fsync=False)
                relationships_writer.flush(force_fsync=False)
                actor_profiles_writer.flush(force_fsync=False)
                progress_state.set_detail("phase", "marking_hydrated")
                control.mark_posts_interactions_hydrated(post_uris=hydrated_successfully, hydrated_at_utc=format_utc(now_utc()))
                control.commit()
                success = True
                progress_state.set_detail("phase", "complete")
                logger.info(
                    "backfill-interactions done posts=%s actors=%s",
                    len(hydrated_successfully),
                    len(all_actor_dids),
                )
            finally:
                control.finish_run(run_id=run_id, finished_at_utc=format_utc(now_utc()), success=success)
    finally:
        post_view_writer.close()
        summary_writer.close()
        likes_writer.close()
        quotes_writer.close()
        reposted_by_writer.close()
        relationships_writer.close()
        actor_profiles_writer.close()
        if http.request_provenance_writer is not None:
            http.request_provenance_writer.close()
        http_stats_writer.close()
        progress_reporter.stop()
        manifest["finished_at_utc"] = format_utc(now_utc())
        manifest["success"] = bool(success)
        atomic_write_json(layout.interactions_manifest_json(date_str), manifest)
        await http.aclose()
