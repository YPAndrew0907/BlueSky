from __future__ import annotations

import logging
from typing import Any

from bsky_fair_collect.config import AppConfig
from bsky_fair_collect.errors import record_error
from bsky_fair_collect.http_client import HttpClient, HttpError
from bsky_fair_collect.parse_utils import parse_at_uri, provider_bucket_from_service_did
from bsky_fair_collect.state import StateDB
from bsky_fair_collect.utils import utc_now_iso

logger = logging.getLogger("bsky_fair_collect.stage.index_generators")


def stage_index_feed_generators(cfg: AppConfig, state: StateDB, http: HttpClient) -> None:
    logger.info("stage=start name=index_feed_generators")

    cursor = state.get_meta("relay_cursor")
    processed_before = _count_rows(state, "actor_processed")
    feeds_before = _count_rows(state, "feed_generators")
    logger.info(
        "index_generators resume cursor=%s processed_actors=%s indexed_feeds=%s",
        cursor,
        processed_before,
        feeds_before,
    )

    page = 0
    processed_this_run = 0
    while True:
        page += 1
        try:
            resp = http.xrpc_get(
                endpoint_name="com.atproto.sync.listReposByCollection",
                host=cfg.hosts.relay_host,
                method="com.atproto.sync.listReposByCollection",
                params={
                    "collection": "app.bsky.feed.generator",
                    "limit": cfg.run.relay_page_limit,
                    **({"cursor": cursor} if cursor else {}),
                },
            )
        except HttpError as err:
            record_error(
                state,
                stage="index_generators.relay_listReposByCollection",
                key=str(cursor or ""),
                error_type="http_error",
                http_status=err.status_code,
                error_message=str(err),
            )
            raise

        repos = resp.get("repos")
        if not isinstance(repos, list):
            record_error(
                state,
                stage="index_generators.relay_listReposByCollection",
                key=str(cursor or ""),
                error_type="bad_response",
                error_message="missing or invalid 'repos' in response",
            )
            raise RuntimeError("relay response missing repos")

        cursor = resp.get("cursor") if isinstance(resp.get("cursor"), str) else None
        if cursor:
            state.set_meta("relay_cursor", cursor)

        new_actors = 0
        for repo in repos:
            actor_did = _safe_get_str(repo, "did")
            if not actor_did:
                continue
            if _is_actor_processed(state, actor_did):
                continue

            new_actors += 1
            _process_actor(cfg, state, http, actor_did)
            processed_this_run += 1
            if cfg.run.index_max_actors is not None and processed_this_run >= cfg.run.index_max_actors:
                logger.warning(
                    "index_generators early stop: processed_this_run=%s (index_max_actors=%s)",
                    processed_this_run,
                    cfg.run.index_max_actors,
                )
                logger.info(
                    "stage=partial name=index_feed_generators processed_actors=%s indexed_feeds=%s cursor=%s",
                    _count_rows(state, "actor_processed"),
                    _count_rows(state, "feed_generators"),
                    cursor,
                )
                return

        if page % 10 == 0:
            logger.info(
                "index_generators progress pages=%s new_actors=%s total_processed=%s total_feeds=%s cursor=%s",
                page,
                new_actors,
                _count_rows(state, "actor_processed"),
                _count_rows(state, "feed_generators"),
                cursor,
            )

        if not cursor:
            break

    state.set_meta("relay_cursor_done", "1")
    logger.info(
        "stage=done name=index_feed_generators processed_actors=%s indexed_feeds=%s",
        _count_rows(state, "actor_processed"),
        _count_rows(state, "feed_generators"),
    )


def _safe_get_str(obj: Any, key: str) -> str | None:
    if not isinstance(obj, dict):
        return None
    v = obj.get(key)
    if isinstance(v, str) and v:
        return v
    return None


def _count_rows(state: StateDB, table: str) -> int:
    row = state.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
    return int(row["n"]) if row is not None else 0


def _is_actor_processed(state: StateDB, actor_did: str) -> bool:
    row = state.conn.execute("SELECT 1 FROM actor_processed WHERE actor_did = ? LIMIT 1", (actor_did,)).fetchone()
    return row is not None


def _mark_actor_processed(state: StateDB, actor_did: str) -> None:
    state.conn.execute(
        "INSERT OR IGNORE INTO actor_processed(actor_did, processed_at_utc) VALUES (?, ?)",
        (actor_did, utc_now_iso()),
    )
    state.conn.commit()


def _process_actor(cfg: AppConfig, state: StateDB, http: HttpClient, actor_did: str) -> None:
    cursor: str | None = None
    page = 0
    while True:
        page += 1
        try:
            resp = http.xrpc_get(
                endpoint_name="app.bsky.feed.getActorFeeds",
                host=cfg.hosts.appview_host,
                method="app.bsky.feed.getActorFeeds",
                params={
                    "actor": actor_did,
                    "limit": cfg.run.actor_feeds_page_limit,
                    **({"cursor": cursor} if cursor else {}),
                },
            )
        except HttpError as err:
            record_error(
                state,
                stage="index_generators.appview_getActorFeeds",
                key=actor_did,
                error_type="http_error",
                http_status=err.status_code,
                error_message=str(err),
            )
            _mark_actor_processed(state, actor_did)
            return

        feeds = resp.get("feeds")
        if not isinstance(feeds, list):
            record_error(
                state,
                stage="index_generators.appview_getActorFeeds",
                key=actor_did,
                error_type="bad_response",
                error_message="missing or invalid 'feeds' in response",
            )
            _mark_actor_processed(state, actor_did)
            return

        _upsert_feeds_from_actor(state, actor_did, feeds)

        cursor = resp.get("cursor") if isinstance(resp.get("cursor"), str) else None
        if not cursor:
            break

    _mark_actor_processed(state, actor_did)


def _upsert_feeds_from_actor(state: StateDB, actor_did: str, feeds: list[object]) -> None:
    rows: list[tuple[object, ...]] = []
    for item in feeds:
        if not isinstance(item, dict):
            continue
        uri = item.get("uri")
        if not isinstance(uri, str) or not uri:
            continue
        try:
            at = parse_at_uri(uri)
        except ValueError:
            continue

        creator_did = actor_did
        creator = item.get("creator")
        if isinstance(creator, dict) and isinstance(creator.get("did"), str) and creator.get("did"):
            creator_did = str(creator.get("did"))

        service_did = item.get("did") if isinstance(item.get("did"), str) else None
        provider_bucket = provider_bucket_from_service_did(service_did)

        display_name = item.get("displayName") if isinstance(item.get("displayName"), str) else None
        description = item.get("description") if isinstance(item.get("description"), str) else None

        accepts_interaction = item.get("acceptsInteractions")
        if isinstance(accepts_interaction, bool):
            accepts_interaction_i = 1 if accepts_interaction else 0
        else:
            accepts_interaction_i = None

        content_mode = item.get("contentMode") if isinstance(item.get("contentMode"), str) else None
        indexed_at = item.get("indexedAt") if isinstance(item.get("indexedAt"), str) else None

        rows.append(
            (
                at.raw,
                creator_did,
                at.rkey,
                service_did,
                provider_bucket,
                display_name,
                description,
                accepts_interaction_i,
                content_mode,
                indexed_at,
            )
        )

    if not rows:
        return

    state.conn.executemany(
        """
        INSERT INTO feed_generators(
          feed_uri, creator_did, rkey, service_did, provider_bucket, display_name, description,
          accepts_interaction, content_mode, indexed_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(feed_uri) DO UPDATE SET
          creator_did = excluded.creator_did,
          rkey = excluded.rkey,
          service_did = excluded.service_did,
          provider_bucket = excluded.provider_bucket,
          display_name = COALESCE(excluded.display_name, feed_generators.display_name),
          description = COALESCE(excluded.description, feed_generators.description),
          accepts_interaction = COALESCE(excluded.accepts_interaction, feed_generators.accepts_interaction),
          content_mode = COALESCE(excluded.content_mode, feed_generators.content_mode),
          indexed_at = COALESCE(excluded.indexed_at, feed_generators.indexed_at)
        """,
        rows,
    )
    state.conn.commit()
