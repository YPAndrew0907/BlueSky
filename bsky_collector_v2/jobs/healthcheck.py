from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from bsky_collector_v2.env import AuthEnv, load_auth_env
from bsky_collector_v2.http_client import AsyncHttpClient, HttpRetryConfig, XrpcHosts
from bsky_collector_v2.layout import Layout
from bsky_collector_v2.session import get_or_create_session, session_cache_path
from bsky_collector_v2.state import ControlState
from bsky_collector_v2.time_utils import format_utc, now_utc

logger = logging.getLogger("bsky_collector_v2.job.healthcheck")


@dataclass(frozen=True)
class HealthcheckResult:
    out_base_ok: bool
    control_db_ok: bool
    unauth_http_ok: bool
    auth_env_ok: bool
    auth_session_ok: bool


async def run_healthcheck(
    *,
    layout: Layout,
    hosts: XrpcHosts,
    env_path: Path | None,
    rps: float,
    dry_run: bool,
) -> HealthcheckResult:
    auth_env: AuthEnv | None = None
    auth_env_ok = False
    if env_path is not None and env_path.exists():
        try:
            auth_env = load_auth_env(env_path)
            auth_env_ok = True
        except Exception as err:  # noqa: BLE001
            logger.warning("auth env invalid err=%r", err)
            auth_env_ok = False

    control_db_ok = False
    try:
        with ControlState.open_local(layout.control_db_path) as s:
            row = s.conn.execute("PRAGMA journal_mode;").fetchone()
            mode = str(row[0]).lower() if row is not None else ""
            if mode == "wal":
                control_db_ok = True
    except Exception as err:  # noqa: BLE001
        logger.error("control db open failed err=%r", err)
        control_db_ok = False

    if dry_run:
        logger.info("dry_run=true: skipping network probes")
        return HealthcheckResult(
            out_base_ok=True,
            control_db_ok=control_db_ok,
            unauth_http_ok=False,
            auth_env_ok=auth_env_ok,
            auth_session_ok=False,
        )

    http = AsyncHttpClient(
        hosts=hosts,
        rps=rps,
        retry=HttpRetryConfig(max_retries=1),
        timeout_s=10.0,
        http_stats=None,
        progress=None,
    )

    unauth_http_ok = False
    auth_session_ok = False
    ts = format_utc(now_utc())
    try:
        # Stable, tiny read-only endpoint.
        resp = await http.xrpc_get(
            endpoint="app.bsky.actor.getProfile.healthcheck",
            host=http.hosts.appview_host,
            method="app.bsky.actor.getProfile",
            params={"actor": "bsky.app"},
            access_jwt=None,
            feed_uri=None,
            timestamp_utc=ts,
        )
        unauth_http_ok = isinstance(resp.data, dict) and bool(resp.data)

        if auth_env is not None:
            try:
                _tokens = await get_or_create_session(
                    http,
                    env=auth_env,
                    cache_path=session_cache_path(control_root=layout.control_root, env=auth_env),
                )
                auth_session_ok = True
            except Exception as err:  # noqa: BLE001
                logger.warning("auth session probe failed err=%r", err)
                auth_session_ok = False
    finally:
        await http.aclose()

    return HealthcheckResult(
        out_base_ok=True,
        control_db_ok=control_db_ok,
        unauth_http_ok=unauth_http_ok,
        auth_env_ok=auth_env_ok,
        auth_session_ok=auth_session_ok,
    )
