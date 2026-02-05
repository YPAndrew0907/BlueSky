from __future__ import annotations

import logging

from bsky_fair_collect.config import AppConfig
from bsky_fair_collect.errors import record_error
from bsky_fair_collect.http_client import HttpClient, HttpError
from bsky_fair_collect.state import StateDB
from bsky_fair_collect.utils import utc_now_iso

logger = logging.getLogger("bsky_fair_collect.stage.popular")


def stage_collect_popular(cfg: AppConfig, state: StateDB, http: HttpClient) -> None:
    logger.info("stage=start name=collect_popular target=%s", cfg.run.n_popular)

    existing = _count_rows(state, "popular_feeds")
    if existing >= cfg.run.n_popular:
        logger.info("collect_popular skip existing=%s >= target=%s", existing, cfg.run.n_popular)
        return

    cursor = state.get_meta("popular_cursor")
    if state.get_meta("popular_cursor_done") == "1":
        cursor = None
    rank = _max_popularity_rank(state) + 1
    collected_at = utc_now_iso()
    attempts = 0

    while _count_rows(state, "popular_feeds") < cfg.run.n_popular:
        attempts += 1
        try:
            resp = http.xrpc_get(
                endpoint_name="app.bsky.unspecced.getPopularFeedGenerators",
                host=cfg.hosts.appview_host,
                method="app.bsky.unspecced.getPopularFeedGenerators",
                params={
                    "limit": cfg.run.popular_page_limit,
                    **({"cursor": cursor} if cursor else {}),
                },
                access_jwt=None,
            )
        except HttpError as err:
            record_error(
                state,
                stage="popular.getPopularFeedGenerators",
                key=str(cursor or ""),
                error_type="http_error",
                http_status=err.status_code,
                error_message=str(err),
            )
            return

        feeds = resp.get("feeds")
        if not isinstance(feeds, list):
            record_error(
                state,
                stage="popular.getPopularFeedGenerators",
                key=str(cursor or ""),
                error_type="bad_response",
                error_message="missing or invalid 'feeds' in response",
            )
            return

        inserted = 0
        for item in feeds:
            if not isinstance(item, dict):
                continue
            uri = item.get("uri")
            if not isinstance(uri, str) or not uri:
                continue
            if _popular_exists(state, uri):
                continue
            state.conn.execute(
                "INSERT OR IGNORE INTO popular_feeds(feed_uri, popularity_rank, collected_at_utc) VALUES (?, ?, ?)",
                (uri, rank, collected_at),
            )
            inserted += 1
            rank += 1

        state.conn.commit()

        if attempts % 10 == 0:
            logger.info(
                "collect_popular progress attempts=%s total=%s target=%s cursor=%s",
                attempts,
                _count_rows(state, "popular_feeds"),
                cfg.run.n_popular,
                cursor,
            )

        cursor = resp.get("cursor") if isinstance(resp.get("cursor"), str) else None
        if cursor:
            state.set_meta("popular_cursor", cursor)
        if inserted == 0 and not cursor:
            break

    if not cursor:
        state.set_meta("popular_cursor_done", "1")
    logger.info("stage=done name=collect_popular total=%s", _count_rows(state, "popular_feeds"))


def _count_rows(state: StateDB, table: str) -> int:
    row = state.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
    return int(row["n"]) if row is not None else 0


def _max_popularity_rank(state: StateDB) -> int:
    row = state.conn.execute("SELECT COALESCE(MAX(popularity_rank), 0) AS m FROM popular_feeds").fetchone()
    return int(row["m"]) if row is not None else 0


def _popular_exists(state: StateDB, feed_uri: str) -> bool:
    row = state.conn.execute("SELECT 1 FROM popular_feeds WHERE feed_uri = ? LIMIT 1", (feed_uri,)).fetchone()
    return row is not None
