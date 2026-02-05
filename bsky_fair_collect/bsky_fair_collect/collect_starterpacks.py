from __future__ import annotations

import logging
from typing import Any

from bsky_fair_collect.config import AppConfig
from bsky_fair_collect.errors import record_error
from bsky_fair_collect.http_client import HttpClient, HttpError
from bsky_fair_collect.parse_utils import parse_at_uri
from bsky_fair_collect.session import SessionManager
from bsky_fair_collect.state import StateDB
from bsky_fair_collect.utils import utc_now_iso

logger = logging.getLogger("bsky_fair_collect.stage.starterpacks")

_META_RELAY_CURSOR = "starterpacks_relay_cursor"
_META_RELAY_CURSOR_DONE = "starterpacks_relay_cursor_done"


def stage_collect_starterpacks(
    cfg: AppConfig,
    state: StateDB,
    http: HttpClient,
    session: SessionManager | None,
) -> None:
    logger.info("stage=start name=collect_starterpacks")

    total_before = _count_rows(state, "starterpacks")
    logger.info("starterpacks resume existing=%s", total_before)

    # Strategy A: legacy searchStarterPacks query strategy (may not be supported on some AppView deployments).
    if _supports_search_starterpacks(cfg, http):
        queries = cfg.run.starterpack_queries
        if cfg.run.starterpack_query_limit is not None:
            queries = queries[: cfg.run.starterpack_query_limit]

        for q in queries:
            _collect_query(cfg, state, http, session, q)
    else:
        logger.warning("starterpacks searchStarterPacks unsupported; skipping query strategy")

    # Strategy B (preferred): enumerate starter pack creators via relay and fetch packs per actor.
    _collect_via_relay(cfg, state, http)

    logger.info(
        "stage=done name=collect_starterpacks packs=%s feeds=%s",
        _count_rows(state, "starterpacks"),
        _count_distinct(state, "starterpack_feeds", "feed_uri"),
    )


def _supports_search_starterpacks(cfg: AppConfig, http: HttpClient) -> bool:
    try:
        http.xrpc_get(
            endpoint_name="app.bsky.graph.searchStarterPacks.probe",
            host=cfg.hosts.appview_host,
            method="app.bsky.graph.searchStarterPacks",
            params={"q": "a", "limit": 1},
            access_jwt=None,
        )
        return True
    except HttpError as err:
        if err.status_code == 404:
            return False
        # If the method exists but the probe failed for another reason, keep the legacy path enabled.
        return True


def _collect_via_relay(cfg: AppConfig, state: StateDB, http: HttpClient) -> None:
    cursor = state.get_meta(_META_RELAY_CURSOR)
    processed_before = _count_rows(state, "starterpack_actor_processed")
    packs_before = _count_rows(state, "starterpacks")
    logger.info(
        "starterpacks relay-enum resume cursor=%s processed_actors=%s packs=%s",
        cursor,
        processed_before,
        packs_before,
    )

    page = 0
    processed_this_run = 0
    while True:
        page += 1
        try:
            resp = http.xrpc_get(
                endpoint_name="starterpacks.relay_listReposByCollection",
                host=cfg.hosts.relay_host,
                method="com.atproto.sync.listReposByCollection",
                params={
                    "collection": "app.bsky.graph.starterpack",
                    "limit": cfg.run.relay_page_limit,
                    **({"cursor": cursor} if cursor else {}),
                },
                access_jwt=None,
            )
        except HttpError as err:
            record_error(
                state,
                stage="starterpacks.relay_listReposByCollection",
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
                stage="starterpacks.relay_listReposByCollection",
                key=str(cursor or ""),
                error_type="bad_response",
                error_message="missing or invalid 'repos' in response",
            )
            raise RuntimeError("relay response missing repos")

        cursor = resp.get("cursor") if isinstance(resp.get("cursor"), str) else None
        if cursor:
            state.set_meta(_META_RELAY_CURSOR, cursor)

        new_actors = 0
        for repo in repos:
            actor_did = _safe_get_str(repo, "did")
            if not actor_did:
                continue
            if _is_starterpack_actor_processed(state, actor_did):
                continue

            new_actors += 1
            _process_starterpack_actor(cfg, state, http, actor_did)
            processed_this_run += 1

            if cfg.run.starterpack_actor_limit is not None and processed_this_run >= cfg.run.starterpack_actor_limit:
                logger.warning(
                    "starterpacks early stop: processed_this_run=%s (starterpack_actor_limit=%s)",
                    processed_this_run,
                    cfg.run.starterpack_actor_limit,
                )
                logger.info(
                    "stage=partial name=collect_starterpacks processed_actors=%s packs=%s cursor=%s",
                    _count_rows(state, "starterpack_actor_processed"),
                    _count_rows(state, "starterpacks"),
                    cursor,
                )
                return

        if page % 10 == 0:
            logger.info(
                "starterpacks relay-enum progress pages=%s new_actors=%s processed_actors=%s packs=%s cursor=%s",
                page,
                new_actors,
                _count_rows(state, "starterpack_actor_processed"),
                _count_rows(state, "starterpacks"),
                cursor,
            )

        if not cursor:
            break

    state.set_meta(_META_RELAY_CURSOR_DONE, "1")


def _safe_get_str(obj: Any, key: str) -> str | None:
    if not isinstance(obj, dict):
        return None
    v = obj.get(key)
    if isinstance(v, str) and v:
        return v
    return None


def _is_starterpack_actor_processed(state: StateDB, actor_did: str) -> bool:
    row = state.conn.execute(
        "SELECT 1 FROM starterpack_actor_processed WHERE actor_did = ? LIMIT 1",
        (actor_did,),
    ).fetchone()
    return row is not None


def _mark_starterpack_actor_processed(state: StateDB, actor_did: str) -> None:
    state.conn.execute(
        "INSERT OR IGNORE INTO starterpack_actor_processed(actor_did, processed_at_utc) VALUES (?, ?)",
        (actor_did, utc_now_iso()),
    )
    state.conn.commit()


def _process_starterpack_actor(cfg: AppConfig, state: StateDB, http: HttpClient, actor_did: str) -> None:
    cursor: str | None = None
    page = 0
    while True:
        page += 1
        try:
            resp = http.xrpc_get(
                endpoint_name="app.bsky.graph.getActorStarterPacks",
                host=cfg.hosts.appview_host,
                method="app.bsky.graph.getActorStarterPacks",
                params={
                    "actor": actor_did,
                    "limit": 100,
                    **({"cursor": cursor} if cursor else {}),
                },
                access_jwt=None,
            )
        except HttpError as err:
            record_error(
                state,
                stage="starterpacks.appview_getActorStarterPacks",
                key=actor_did,
                error_type="http_error",
                http_status=err.status_code,
                error_message=str(err),
            )
            _mark_starterpack_actor_processed(state, actor_did)
            return

        packs = resp.get("starterPacks")
        if not isinstance(packs, list):
            record_error(
                state,
                stage="starterpacks.appview_getActorStarterPacks",
                key=actor_did,
                error_type="bad_response",
                error_message="missing or invalid 'starterPacks' in response",
            )
            _mark_starterpack_actor_processed(state, actor_did)
            return

        for pack in packs:
            uri = _extract_starterpack_uri(pack)
            if not uri:
                continue
            if _starterpack_exists(state, uri):
                continue
            _hydrate_pack(cfg, state, http, session=None, starterpack_uri=uri)

        cursor = resp.get("cursor") if isinstance(resp.get("cursor"), str) else None
        if not cursor:
            break

    _mark_starterpack_actor_processed(state, actor_did)


def _collect_query(cfg: AppConfig, state: StateDB, http: HttpClient, session: SessionManager | None, query: str) -> None:
    cursor: str | None = None
    collected_new = 0
    seen_this_query = 0

    while True:
        try:
            resp = http.xrpc_get(
                endpoint_name="app.bsky.graph.searchStarterPacks",
                host=cfg.hosts.appview_host,
                method="app.bsky.graph.searchStarterPacks",
                params={
                    "q": query,
                    "limit": 100,
                    **({"cursor": cursor} if cursor else {}),
                },
                access_jwt=None,
            )
        except HttpError as err:
            # Some deployments do not expose this method on public AppView hosts.
            if err.status_code == 404:
                record_error(
                    state,
                    stage="starterpacks.searchStarterPacks",
                    key=query,
                    error_type="xrpc_not_supported",
                    http_status=err.status_code,
                    error_message=str(err),
                )
                return
            else:
                record_error(
                    state,
                    stage="starterpacks.searchStarterPacks",
                    key=query,
                    error_type="http_error",
                    http_status=err.status_code,
                    error_message=str(err),
                )
                return

        packs = resp.get("starterPacks")
        if not isinstance(packs, list):
            record_error(
                state,
                stage="starterpacks.searchStarterPacks",
                key=query,
                error_type="bad_response",
                error_message="missing or invalid 'starterPacks' in response",
            )
            return

        for pack in packs:
            uri = _extract_starterpack_uri(pack)
            if not uri:
                continue
            seen_this_query += 1
            if _starterpack_exists(state, uri):
                continue

            collected_new += 1
            _hydrate_pack(cfg, state, http, session, uri)
            if collected_new >= cfg.run.starterpack_max_per_query:
                break

        if collected_new >= cfg.run.starterpack_max_per_query:
            break

        cursor = resp.get("cursor") if isinstance(resp.get("cursor"), str) else None
        if not cursor:
            break

    if collected_new:
        logger.info("starterpacks query=%s new=%s seen=%s", query, collected_new, seen_this_query)


def _extract_starterpack_uri(obj: Any) -> str | None:
    if not isinstance(obj, dict):
        return None
    uri = obj.get("uri")
    if isinstance(uri, str) and uri:
        return uri
    starter_pack = obj.get("starterPack")
    if isinstance(starter_pack, dict):
        uri2 = starter_pack.get("uri")
        if isinstance(uri2, str) and uri2:
            return uri2
    return None


def _starterpack_exists(state: StateDB, starterpack_uri: str) -> bool:
    row = state.conn.execute(
        "SELECT 1 FROM starterpacks WHERE starterpack_uri = ? LIMIT 1",
        (starterpack_uri,),
    ).fetchone()
    return row is not None


def _hydrate_pack(
    cfg: AppConfig,
    state: StateDB,
    http: HttpClient,
    session: SessionManager | None,
    starterpack_uri: str,
) -> None:
    try:
        resp = http.xrpc_get(
            endpoint_name="app.bsky.graph.getStarterPack",
            host=cfg.hosts.appview_host,
            method="app.bsky.graph.getStarterPack",
            params={"starterPack": starterpack_uri},
            access_jwt=None,
        )
    except HttpError as err:
        if err.status_code == 404 and session is not None:
            try:
                resp = session.xrpc_get(
                    endpoint_name="app.bsky.graph.getStarterPack.fallback_bsky_social",
                    host="https://bsky.social",
                    method="app.bsky.graph.getStarterPack",
                    params={"starterPack": starterpack_uri},
                )
            except HttpError as err2:
                record_error(
                    state,
                    stage="starterpacks.getStarterPack",
                    key=starterpack_uri,
                    error_type="http_error",
                    http_status=err2.status_code,
                    error_message=str(err2),
                )
                return
        else:
            record_error(
                state,
                stage="starterpacks.getStarterPack",
                key=starterpack_uri,
                error_type="http_error",
                http_status=err.status_code,
                error_message=str(err),
            )
            return

    pack_obj = resp.get("starterPack") if isinstance(resp.get("starterPack"), dict) else resp
    if not isinstance(pack_obj, dict):
        record_error(
            state,
            stage="starterpacks.getStarterPack",
            key=starterpack_uri,
            error_type="bad_response",
            error_message="missing or invalid starterPack payload",
        )
        return

    creator_did = _deep_get_str(pack_obj, ("creator", "did"))
    # The authoritative fields are on starterPack.record for getStarterPack responses.
    name = _first_str(_deep_get_str(pack_obj, ("record", "name")), pack_obj.get("name"), pack_obj.get("displayName"))
    description = _first_str(
        _deep_get_str(pack_obj, ("record", "description")),
        pack_obj.get("description"),
        pack_obj.get("about"),
    )
    collected_at = utc_now_iso()

    state.conn.execute(
        """
        INSERT OR IGNORE INTO starterpacks(starterpack_uri, creator_did, name, description, collected_at_utc)
        VALUES (?, ?, ?, ?, ?)
        """,
        (starterpack_uri, creator_did, name, description, collected_at),
    )

    feed_uris = _extract_feed_uris(pack_obj)
    for idx, feed_uri in enumerate(feed_uris):
        state.conn.execute(
            "INSERT OR IGNORE INTO starterpack_feeds(starterpack_uri, slot_index, feed_uri) VALUES (?, ?, ?)",
            (starterpack_uri, idx, feed_uri),
        )

    state.conn.commit()


def _extract_feed_uris(obj: Any) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()

    def visit(x: Any) -> None:
        if isinstance(x, dict):
            uri = x.get("uri")
            if isinstance(uri, str):
                _maybe_add(uri)
            for v in x.values():
                visit(v)
        elif isinstance(x, list):
            for v in x:
                visit(v)

    def _maybe_add(uri: str) -> None:
        if "/app.bsky.feed.generator/" not in uri:
            return
        try:
            at = parse_at_uri(uri)
        except ValueError:
            return
        if at.collection != "app.bsky.feed.generator":
            return
        if uri in seen:
            return
        seen.add(uri)
        out.append(uri)

    visit(obj)
    return out


def _deep_get_str(obj: Any, path: tuple[str, ...]) -> str | None:
    cur: Any = obj
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    if isinstance(cur, str) and cur:
        return cur
    return None


def _first_str(*values: Any) -> str | None:
    for v in values:
        if isinstance(v, str) and v:
            return v
    return None


def _count_rows(state: StateDB, table: str) -> int:
    row = state.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
    return int(row["n"]) if row is not None else 0


def _count_distinct(state: StateDB, table: str, column: str) -> int:
    row = state.conn.execute(f"SELECT COUNT(DISTINCT {column}) AS n FROM {table}").fetchone()
    return int(row["n"]) if row is not None else 0
