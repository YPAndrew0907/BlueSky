from __future__ import annotations

import logging
from dataclasses import dataclass

from bsky_fair_collect.config import AppConfig, AuthMode
from bsky_fair_collect.errors import record_error
from bsky_fair_collect.http_client import HttpClient, HttpError
from bsky_fair_collect.parse_utils import parse_at_uri, provider_bucket_from_service_did
from bsky_fair_collect.session import SessionManager
from bsky_fair_collect.state import StateDB

logger = logging.getLogger("bsky_fair_collect.stage.hydrate_feed_generators")


@dataclass(frozen=True)
class FeedGeneratorView:
    feed_uri: str
    creator_did: str
    rkey: str
    service_did: str | None
    provider_bucket: str
    display_name: str | None
    description: str | None
    accepts_interaction: int | None
    content_mode: str | None
    indexed_at: str | None


def stage_hydrate_feed_generators(cfg: AppConfig, state: StateDB, http: HttpClient, session: SessionManager | None) -> None:
    """
    Hydrate feed generator metadata (service DID / provider bucket / displayName) for feeds we *touch*
    via starterpacks / popular / panel, even if the relay-index scan hasn't reached them yet.

    This prevents discovery/popular feeds from ending up with provider_bucket='unknown', which would
    break provider-leverage (H2) analyses.
    """

    logger.info("stage=start name=hydrate_feed_generators")

    targets = _list_feed_uris_needing_hydration(state)
    logger.info("hydrate_feed_generators targets=%s", len(targets))
    if not targets:
        logger.info("stage=done name=hydrate_feed_generators hydrated=0")
        return

    batch_size = 25
    hydrated = 0
    for i in range(0, len(targets), batch_size):
        batch = targets[i : i + batch_size]
        views = _fetch_feed_generators_views(cfg, state, http, session, batch)
        if not views:
            continue
        _upsert_feed_generators(state, views)
        hydrated += len(views)
        if hydrated % 500 == 0:
            logger.info("hydrate_feed_generators progress hydrated=%s/%s", hydrated, len(targets))

    logger.info("stage=done name=hydrate_feed_generators hydrated=%s", hydrated)


def backfill_feed_panel_metadata(state: StateDB) -> int:
    """
    Fill missing/unknown feed_panel metadata from feed_generators.

    This is safe to run after snapshots are in progress or after a run finishes; it does not change
    the feed selection or grouping, only metadata columns.
    """
    before = int(
        state.conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM feed_panel
            WHERE provider_bucket IS NULL OR provider_bucket = 'unknown'
               OR service_did IS NULL OR creator_did IS NULL OR display_name IS NULL
            """
        ).fetchone()["n"]
    )

    state.conn.execute(
        """
        UPDATE feed_panel
        SET
          provider_bucket = COALESCE(
            (SELECT provider_bucket FROM feed_generators g WHERE g.feed_uri = feed_panel.feed_uri),
            provider_bucket
          ),
          service_did = COALESCE(
            (SELECT service_did FROM feed_generators g WHERE g.feed_uri = feed_panel.feed_uri),
            service_did
          ),
          creator_did = COALESCE(
            (SELECT creator_did FROM feed_generators g WHERE g.feed_uri = feed_panel.feed_uri),
            creator_did
          ),
          display_name = COALESCE(
            (SELECT display_name FROM feed_generators g WHERE g.feed_uri = feed_panel.feed_uri),
            display_name
          )
        WHERE (provider_bucket IS NULL OR provider_bucket = 'unknown'
               OR service_did IS NULL OR creator_did IS NULL OR display_name IS NULL)
          AND EXISTS (SELECT 1 FROM feed_generators g WHERE g.feed_uri = feed_panel.feed_uri)
        """
    )
    state.conn.commit()

    after = int(
        state.conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM feed_panel
            WHERE provider_bucket IS NULL OR provider_bucket = 'unknown'
               OR service_did IS NULL OR creator_did IS NULL OR display_name IS NULL
            """
        ).fetchone()["n"]
    )
    fixed = max(0, before - after)
    logger.info("feed_panel metadata backfill fixed=%s remaining_missing=%s", fixed, after)
    return fixed


def _list_feed_uris_needing_hydration(state: StateDB) -> list[str]:
    # We only hydrate feeds that the pipeline explicitly touches, which is bounded by the number
    # of unique feeds in starterpacks/popular/panel (not the entire universe).
    rows = state.conn.execute(
        """
        WITH touched AS (
          SELECT DISTINCT feed_uri FROM starterpack_feeds
          UNION
          SELECT DISTINCT feed_uri FROM popular_feeds
          UNION
          SELECT DISTINCT feed_uri FROM feed_panel
        )
        SELECT t.feed_uri
        FROM touched t
        LEFT JOIN feed_generators g ON g.feed_uri = t.feed_uri
        WHERE g.feed_uri IS NULL
           OR g.service_did IS NULL
           OR g.provider_bucket = 'unknown'
           OR g.display_name IS NULL
        ORDER BY t.feed_uri
        """
    ).fetchall()
    return [str(r["feed_uri"]) for r in rows]


def _fetch_feed_generators_views(
    cfg: AppConfig,
    state: StateDB,
    http: HttpClient,
    session: SessionManager | None,
    feed_uris: list[str],
) -> list[FeedGeneratorView]:
    if not feed_uris:
        return []

    try:
        access_jwt = None
        if session is not None and cfg.auth_mode != AuthMode.UNAUTH:
            access_jwt = session.get_access_jwt()
        resp = http.xrpc_get(
            endpoint_name="app.bsky.feed.getFeedGenerators",
            host=cfg.hosts.appview_host,
            method="app.bsky.feed.getFeedGenerators",
            params={"feeds": feed_uris},
            access_jwt=access_jwt,
        )
    except HttpError as err:
        record_error(
            state=state,
            stage="hydrate_feed_generators.getFeedGenerators",
            key=f"batch_size={len(feed_uris)}",
            error_type="http_error",
            http_status=err.status_code,
            error_message=str(err),
        )
        # Try to salvage by hydrating individually.
        if len(feed_uris) <= 1:
            return []
        out: list[FeedGeneratorView] = []
        for uri in feed_uris:
            out.extend(_fetch_feed_generators_views(cfg, state, http, session, [uri]))
        return out

    feeds = resp.get("feeds")
    if not isinstance(feeds, list):
        record_error(
            state=state,
            stage="hydrate_feed_generators.getFeedGenerators",
            key=f"batch_size={len(feed_uris)}",
            error_type="bad_response",
            error_message="missing or invalid 'feeds' list in response",
        )
        return []

    out: list[FeedGeneratorView] = []
    returned: set[str] = set()
    for item in feeds:
        if not isinstance(item, dict):
            continue
        uri = item.get("uri")
        if not isinstance(uri, str) or not uri:
            continue
        returned.add(uri)
        try:
            at = parse_at_uri(uri)
        except ValueError:
            continue

        creator_did = at.did
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

        out.append(
            FeedGeneratorView(
                feed_uri=at.raw,
                creator_did=creator_did,
                rkey=at.rkey,
                service_did=service_did,
                provider_bucket=provider_bucket,
                display_name=display_name,
                description=description,
                accepts_interaction=accepts_interaction_i,
                content_mode=content_mode,
                indexed_at=indexed_at,
            )
        )

    missing = sorted(set(feed_uris) - returned)
    for uri in missing:
        record_error(
            state=state,
            stage="hydrate_feed_generators.getFeedGenerators",
            key=uri,
            error_type="missing_in_response",
            error_message="feed not returned by getFeedGenerators",
        )

    return out


def _upsert_feed_generators(state: StateDB, rows: list[FeedGeneratorView]) -> None:
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
          service_did = COALESCE(excluded.service_did, feed_generators.service_did),
          provider_bucket = CASE
            WHEN feed_generators.provider_bucket = 'unknown' THEN excluded.provider_bucket
            WHEN feed_generators.provider_bucket = 'plc_bucket' AND excluded.provider_bucket != 'plc_bucket' THEN excluded.provider_bucket
            ELSE feed_generators.provider_bucket
          END,
          display_name = COALESCE(excluded.display_name, feed_generators.display_name),
          description = COALESCE(excluded.description, feed_generators.description),
          accepts_interaction = COALESCE(excluded.accepts_interaction, feed_generators.accepts_interaction),
          content_mode = COALESCE(excluded.content_mode, feed_generators.content_mode),
          indexed_at = COALESCE(excluded.indexed_at, feed_generators.indexed_at)
        """,
        [
            (
                r.feed_uri,
                r.creator_did,
                r.rkey,
                r.service_did,
                r.provider_bucket,
                r.display_name,
                r.description,
                r.accepts_interaction,
                r.content_mode,
                r.indexed_at,
            )
            for r in rows
        ],
    )
    state.conn.commit()
