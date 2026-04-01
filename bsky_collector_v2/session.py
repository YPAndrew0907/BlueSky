from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from dataclasses import dataclass

from bsky_collector_v2.env import AuthEnv
from bsky_collector_v2.http_client import AsyncHttpClient, HttpError
from bsky_collector_v2.request_provenance import RequestContext
from bsky_collector_v2.fs_utils import atomic_write_json
from bsky_collector_v2.time_utils import format_utc, now_utc

logger = logging.getLogger("bsky_collector_v2.session")


@dataclass(frozen=True)
class SessionTokens:
    access_jwt: str
    refresh_jwt: str | None
    viewer_did: str


def session_cache_path(*, control_root: Path, env: AuthEnv) -> Path:
    key = hashlib.sha256(f"{env.identifier}|{env.pds_host}".encode("utf-8")).hexdigest()[:16]
    return control_root / "auth_sessions" / f"{key}.json"


def load_cached_session(path: Path) -> SessionTokens | None:
    if not path.exists():
        return None
    try:
        import json

        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(raw, dict):
        return None
    access = raw.get("access_jwt")
    refresh = raw.get("refresh_jwt")
    viewer_did = raw.get("viewer_did")
    if not isinstance(access, str) or not access:
        return None
    if not isinstance(viewer_did, str) or not viewer_did:
        return None
    refresh_out = refresh if isinstance(refresh, str) and refresh else None
    return SessionTokens(access_jwt=access, refresh_jwt=refresh_out, viewer_did=viewer_did)


def save_cached_session(path: Path, tokens: SessionTokens) -> None:
    atomic_write_json(
        path,
        {
            "access_jwt": tokens.access_jwt,
            "refresh_jwt": tokens.refresh_jwt,
            "viewer_did": tokens.viewer_did,
            "saved_at_utc": format_utc(now_utc()),
        },
    )


async def create_session(
    http: AsyncHttpClient,
    *,
    env: AuthEnv,
    request_context: RequestContext | None = None,
) -> SessionTokens:
    # Never log token values.
    ts = format_utc(now_utc())
    resp = await http.xrpc_post(
        endpoint="com.atproto.server.createSession",
        host=env.pds_host,
        method="com.atproto.server.createSession",
        json_body={"identifier": env.identifier, "password": env.app_password},
        access_jwt=None,
        timestamp_utc=ts,
        request_context=request_context,
    )
    access = resp.data.get("accessJwt")
    refresh = resp.data.get("refreshJwt")
    viewer_did = resp.data.get("did")
    if not isinstance(access, str) or not access:
        raise RuntimeError("createSession did not return accessJwt")
    if not isinstance(viewer_did, str) or not viewer_did:
        raise RuntimeError("createSession did not return did")
    refresh_out = refresh if isinstance(refresh, str) and refresh else None
    return SessionTokens(access_jwt=access, refresh_jwt=refresh_out, viewer_did=viewer_did)


async def refresh_session(
    http: AsyncHttpClient,
    *,
    pds_host: str,
    refresh_jwt: str,
    request_context: RequestContext | None = None,
) -> SessionTokens:
    ts = format_utc(now_utc())
    resp = await http.xrpc_post(
        endpoint="com.atproto.server.refreshSession",
        host=pds_host,
        method="com.atproto.server.refreshSession",
        json_body=None,
        access_jwt=refresh_jwt,
        timestamp_utc=ts,
        request_context=request_context,
    )
    access = resp.data.get("accessJwt")
    refresh = resp.data.get("refreshJwt")
    viewer_did = resp.data.get("did")
    if not isinstance(access, str) or not access:
        raise RuntimeError("refreshSession did not return accessJwt")
    if not isinstance(viewer_did, str) or not viewer_did:
        raise RuntimeError("refreshSession did not return did")
    refresh_out = refresh if isinstance(refresh, str) and refresh else None
    return SessionTokens(access_jwt=access, refresh_jwt=refresh_out, viewer_did=viewer_did)


async def get_or_create_session(
    http: AsyncHttpClient,
    *,
    env: AuthEnv,
    cache_path: Path,
    refresh_request_context: RequestContext | None = None,
    create_request_context: RequestContext | None = None,
) -> SessionTokens:
    cached = load_cached_session(cache_path)
    if cached is not None and cached.refresh_jwt:
        try:
            refreshed = await refresh_session(
                http,
                pds_host=env.pds_host,
                refresh_jwt=cached.refresh_jwt,
                request_context=refresh_request_context,
            )
            save_cached_session(cache_path, refreshed)
            return refreshed
        except Exception as err:  # noqa: BLE001
            logger.warning("cached session refresh failed; falling back to createSession err=%r", err)
    elif cached is not None:
        return cached

    created = await create_session(http, env=env, request_context=create_request_context)
    save_cached_session(cache_path, created)
    return created


def is_auth_required_error(err: HttpError) -> bool:
    if err.status_code in (401, 403):
        return True
    msg = str(err).lower()
    return ("auth" in msg and "required" in msg) or ("authentication" in msg and "required" in msg)
