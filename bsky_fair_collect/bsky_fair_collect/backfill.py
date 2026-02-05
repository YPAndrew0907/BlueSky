from __future__ import annotations

import csv
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from bsky_fair_collect.config import AppConfig, AuthMode, Hosts, OutputPaths, RunParams
from bsky_fair_collect.env import load_credentials
from bsky_fair_collect.hydrate_feed_generators import backfill_feed_panel_metadata, stage_hydrate_feed_generators
from bsky_fair_collect.http_client import HttpClient, HttpRetryConfig
from bsky_fair_collect.session import SessionManager
from bsky_fair_collect.state import StateDB
from bsky_fair_collect.write_outputs import export_all_csvs

logger = logging.getLogger("bsky_fair_collect.backfill")


@dataclass(frozen=True)
class BackfillOverrides:
    auth_mode: AuthMode | None = None
    appview_host: str | None = None
    relay_host: str | None = None
    rps: float | None = None
    max_retries: int | None = None


def backfill_and_export(out_dir: Path, *, overrides: BackfillOverrides, allow_running: bool) -> None:
    cfg = _load_cfg_from_run_metadata(out_dir, overrides=overrides)
    state_db_path = out_dir / "state" / "state.db"

    with StateDB(state_db_path) as state:
        if not allow_running:
            _ensure_run_not_active(out_dir, state)

        http = HttpClient(
            state=state,
            rps=cfg.run.rps,
            retry=HttpRetryConfig(max_retries=cfg.run.max_retries),
        )
        try:
            session = _maybe_session(cfg, state, http)

            # 1) Hydrate provider/service DID/displayName for all touched feeds.
            stage_hydrate_feed_generators(cfg, state, http, session)
            backfill_feed_panel_metadata(state)

            # 2) Re-export CSVs so downstream analysis sees the backfilled metadata.
            export_all_csvs(cfg, state)
        finally:
            http.close()


def _maybe_session(cfg: AppConfig, state: StateDB, http: HttpClient) -> SessionManager | None:
    if cfg.auth_mode == AuthMode.UNAUTH:
        return None
    creds = load_credentials()
    if creds is None:
        raise RuntimeError("auth-mode requires credentials in /Users/yipengandrewwang/BlueSky/.env.local")
    session = SessionManager(state=state, http=http, creds=creds)
    session.get_access_jwt()
    return session


def _load_cfg_from_run_metadata(out_dir: Path, *, overrides: BackfillOverrides) -> AppConfig:
    outputs = OutputPaths.for_out_dir(out_dir)
    meta_path = outputs.csv_dir / "run_metadata.csv"

    hosts = Hosts()
    auth_mode = AuthMode.UNAUTH
    run = RunParams()

    if meta_path.exists():
        with meta_path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            row = next(reader, None)
        if row:
            hosts = Hosts(
                appview_host=str(row.get("appview_host") or hosts.appview_host),
                relay_host=str(row.get("relay_host") or hosts.relay_host),
            )
            try:
                auth_mode = AuthMode(str(row.get("auth_mode") or auth_mode.value))
            except ValueError:
                auth_mode = AuthMode.UNAUTH

            def _int(key: str, default: int) -> int:
                raw = row.get(key)
                try:
                    return int(raw) if raw is not None and str(raw) else default
                except ValueError:
                    return default

            def _float(key: str, default: float) -> float:
                raw = row.get(key)
                try:
                    return float(raw) if raw is not None and str(raw) else default
                except ValueError:
                    return default

            run = RunParams(
                rps=_float("rps", run.rps),
                max_retries=_int("max_retries", run.max_retries),
                posts_per_feed=_int("posts_per_feed", run.posts_per_feed),
                n_discovery=_int("n_discovery", run.n_discovery),
                n_popular=_int("n_popular", run.n_popular),
                n_less_known=_int("n_less_known", run.n_less_known),
            )

    # Apply overrides last.
    if overrides.appview_host is not None:
        hosts = Hosts(appview_host=overrides.appview_host, relay_host=hosts.relay_host)
    if overrides.relay_host is not None:
        hosts = Hosts(appview_host=hosts.appview_host, relay_host=overrides.relay_host)
    if overrides.auth_mode is not None:
        auth_mode = overrides.auth_mode
    if overrides.rps is not None:
        run = RunParams(
            rps=overrides.rps,
            max_retries=run.max_retries,
            posts_per_feed=run.posts_per_feed,
            n_discovery=run.n_discovery,
            n_popular=run.n_popular,
            n_less_known=run.n_less_known,
        )
    if overrides.max_retries is not None:
        run = RunParams(
            rps=run.rps,
            max_retries=overrides.max_retries,
            posts_per_feed=run.posts_per_feed,
            n_discovery=run.n_discovery,
            n_popular=run.n_popular,
            n_less_known=run.n_less_known,
        )

    return AppConfig(outputs=outputs, hosts=hosts, auth_mode=auth_mode, run=run)


def _ensure_run_not_active(out_dir: Path, state: StateDB) -> None:
    finished_at = state.get_meta("finished_at_utc")
    if finished_at:
        return

    pid_path = out_dir / "pid.txt"
    try:
        raw = pid_path.read_text("utf-8").strip()
    except FileNotFoundError:
        return
    if not raw:
        return
    try:
        pid = int(raw)
    except ValueError:
        return

    if _pid_alive(pid):
        raise RuntimeError(
            f"refusing to backfill while run appears active (pid={pid}). "
            "Wait for the collector to finish, or re-run with --allow-running."
        )


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True

