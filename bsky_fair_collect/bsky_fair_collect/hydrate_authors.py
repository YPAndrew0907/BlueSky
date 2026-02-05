from __future__ import annotations

import logging
from typing import Any

from bsky_fair_collect.config import AppConfig
from bsky_fair_collect.errors import record_error
from bsky_fair_collect.http_client import HttpClient, HttpError
from bsky_fair_collect.state import StateDB
from bsky_fair_collect.utils import utc_now_iso

logger = logging.getLogger("bsky_fair_collect.stage.hydrate_authors")


def stage_hydrate_authors(cfg: AppConfig, state: StateDB, http: HttpClient) -> None:
    logger.info("stage=start name=hydrate_authors")

    missing = _missing_author_dids(state)
    if not missing:
        logger.info("hydrate_authors: nothing to do (all authors hydrated)")
        return

    logger.info("hydrate_authors missing=%s batch_size=%s", len(missing), cfg.run.profiles_batch_size)

    collected_at = utc_now_iso()
    batch_size = cfg.run.profiles_batch_size

    for i in range(0, len(missing), batch_size):
        batch = missing[i : i + batch_size]
        _hydrate_batch(cfg, state, http, batch, collected_at_utc=collected_at)
        if (i // batch_size) % 100 == 0 and i > 0:
            logger.info(
                "hydrate_authors progress hydrated=%s/%s",
                _count_rows(state, "authors"),
                _count_distinct(state, "feed_items", "author_did"),
            )

    logger.info(
        "stage=done name=hydrate_authors hydrated=%s total_unique_authors=%s",
        _count_rows(state, "authors"),
        _count_distinct(state, "feed_items", "author_did"),
    )


def _missing_author_dids(state: StateDB) -> list[str]:
    rows = list(
        state.conn.execute(
            """
            SELECT DISTINCT fi.author_did AS did
            FROM feed_items fi
            LEFT JOIN authors a ON a.author_did = fi.author_did
            WHERE a.author_did IS NULL
            ORDER BY fi.author_did
            """
        )
    )
    return [str(r["did"]) for r in rows]


def _hydrate_batch(
    cfg: AppConfig,
    state: StateDB,
    http: HttpClient,
    dids: list[str],
    *,
    collected_at_utc: str,
) -> None:
    try:
        resp = http.xrpc_get(
            endpoint_name="app.bsky.actor.getProfiles",
            host=cfg.hosts.appview_host,
            method="app.bsky.actor.getProfiles",
            params={"actors": dids},
            access_jwt=None,
        )
    except HttpError as err:
        record_error(
            state,
            stage="authors.getProfiles",
            key=",".join(dids),
            error_type="http_error",
            http_status=err.status_code,
            error_message=str(err),
        )
        return

    profiles = resp.get("profiles")
    if not isinstance(profiles, list):
        record_error(
            state,
            stage="authors.getProfiles",
            key=",".join(dids),
            error_type="bad_response",
            error_message="missing or invalid 'profiles' in response",
        )
        return

    returned: set[str] = set()
    for p in profiles:
        if not isinstance(p, dict):
            continue
        did = p.get("did")
        if not isinstance(did, str) or not did:
            continue
        returned.add(did)
        handle = p.get("handle") if isinstance(p.get("handle"), str) else None
        display_name = p.get("displayName") if isinstance(p.get("displayName"), str) else None
        followers = p.get("followersCount") if isinstance(p.get("followersCount"), int) else None
        follows = p.get("followsCount") if isinstance(p.get("followsCount"), int) else None
        posts = p.get("postsCount") if isinstance(p.get("postsCount"), int) else None

        state.conn.execute(
            """
            INSERT OR IGNORE INTO authors(
              author_did, handle, display_name, followers_count, follows_count, posts_count, collected_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (did, handle, display_name, followers, follows, posts, collected_at_utc),
        )

    state.conn.commit()

    missing = [did for did in dids if did not in returned]
    if missing:
        record_error(
            state,
            stage="authors.getProfiles",
            key=",".join(missing),
            error_type="missing_profiles",
            error_message="some requested authors were not returned in profiles response",
        )


def _count_rows(state: StateDB, table: str) -> int:
    row = state.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
    return int(row["n"]) if row is not None else 0


def _count_distinct(state: StateDB, table: str, column: str) -> int:
    row = state.conn.execute(f"SELECT COUNT(DISTINCT {column}) AS n FROM {table}").fetchone()
    return int(row["n"]) if row is not None else 0
