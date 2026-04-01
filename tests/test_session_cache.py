from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from bsky_collector_v2.env import AuthEnv
from bsky_collector_v2.session import (
    SessionTokens,
    get_or_create_session,
    load_cached_session,
    save_cached_session,
    session_cache_path,
)


class _FakeHttp:
    def __init__(self, *, refresh_ok: bool) -> None:
        self.refresh_ok = refresh_ok
        self.calls: list[str] = []

    async def xrpc_post(self, *, endpoint: str, **_kwargs: Any):  # noqa: ANN401
        self.calls.append(endpoint)
        if endpoint == "com.atproto.server.refreshSession":
            if not self.refresh_ok:
                raise RuntimeError("refresh failed")
            data = {"accessJwt": "access-from-refresh", "refreshJwt": "refresh-from-refresh", "did": "did:plc:refresh"}
        else:
            data = {"accessJwt": "access-from-create", "refreshJwt": "refresh-from-create", "did": "did:plc:create"}

        class _Resp:
            def __init__(self, data: dict[str, Any]) -> None:
                self.data = data

        return _Resp(data)


def test_session_cache_round_trip(tmp_path: Path) -> None:
    env = AuthEnv(identifier="user.test", app_password="pw", pds_host="https://bsky.social")
    path = session_cache_path(control_root=tmp_path, env=env)
    tokens = SessionTokens(access_jwt="access", refresh_jwt="refresh", viewer_did="did:plc:test")
    save_cached_session(path, tokens)

    loaded = load_cached_session(path)
    assert loaded == tokens


def test_get_or_create_session_prefers_refresh(tmp_path: Path) -> None:
    env = AuthEnv(identifier="user.test", app_password="pw", pds_host="https://bsky.social")
    cache_path = session_cache_path(control_root=tmp_path, env=env)
    save_cached_session(cache_path, SessionTokens(access_jwt="old-access", refresh_jwt="old-refresh", viewer_did="did:plc:old"))

    http = _FakeHttp(refresh_ok=True)
    tokens = asyncio.run(get_or_create_session(http, env=env, cache_path=cache_path))

    assert tokens.access_jwt == "access-from-refresh"
    assert http.calls == ["com.atproto.server.refreshSession"]


def test_get_or_create_session_falls_back_to_create(tmp_path: Path) -> None:
    env = AuthEnv(identifier="user.test", app_password="pw", pds_host="https://bsky.social")
    cache_path = session_cache_path(control_root=tmp_path, env=env)
    save_cached_session(cache_path, SessionTokens(access_jwt="old-access", refresh_jwt="old-refresh", viewer_did="did:plc:old"))

    http = _FakeHttp(refresh_ok=False)
    tokens = asyncio.run(get_or_create_session(http, env=env, cache_path=cache_path))

    assert tokens.access_jwt == "access-from-create"
    assert http.calls == ["com.atproto.server.refreshSession", "com.atproto.server.createSession"]
