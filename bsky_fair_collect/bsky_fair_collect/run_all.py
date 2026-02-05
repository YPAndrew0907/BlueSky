from __future__ import annotations

import logging
import uuid
from pathlib import Path

from bsky_fair_collect.config import AppConfig, AuthMode
from bsky_fair_collect.env import load_credentials
from bsky_fair_collect.http_client import HttpClient, HttpRetryConfig
from bsky_fair_collect.session import SessionManager
from bsky_fair_collect.state import StateDB
from bsky_fair_collect.utils import ensure_dir, utc_now_iso

logger = logging.getLogger("bsky_fair_collect.run_all")


def _ensure_outputs(cfg: AppConfig) -> None:
    ensure_dir(cfg.outputs.out_dir)
    ensure_dir(cfg.outputs.csv_dir)
    ensure_dir(cfg.outputs.raw_dir)
    ensure_dir(cfg.outputs.logs_dir)
    ensure_dir(cfg.outputs.state_dir)


def _ensure_run_identity(state: StateDB) -> tuple[str, str]:
    run_id = state.get_meta("run_id")
    started_at = state.get_meta("started_at_utc")
    if run_id and started_at:
        return run_id, started_at

    run_id = uuid.uuid4().hex
    started_at = utc_now_iso()
    state.set_meta("run_id", run_id)
    state.set_meta("started_at_utc", started_at)
    return run_id, started_at


def _state_db_path(out_dir: Path) -> Path:
    return out_dir / "state" / "state.db"


def run_all(cfg: AppConfig) -> None:
    _ensure_outputs(cfg)

    with StateDB(_state_db_path(cfg.outputs.out_dir)) as state:
        run_id, started_at = _ensure_run_identity(state)
        logger.info("run_id=%s started_at_utc=%s", run_id, started_at)
        # If we're resuming after a failed/partial run, clear any stale finished_at marker so monitors
        # and exports reflect that the run is active again.
        state.set_meta("finished_at_utc", "")

        http = HttpClient(
            state=state,
            rps=cfg.run.rps,
            retry=HttpRetryConfig(max_retries=cfg.run.max_retries),
        )
        try:
            creds = load_credentials()
            session = SessionManager(state=state, http=http, creds=creds) if creds else None
            if cfg.auth_mode != AuthMode.UNAUTH:
                if session is None:
                    raise RuntimeError("auth-mode requires credentials in /Users/yipengandrewwang/BlueSky/.env.local")
                # Ensure we have a working access token (refresh/create session if needed).
                session.get_access_jwt()

            # Stage orchestration is implemented in the stage modules.
            from bsky_fair_collect.write_outputs import write_run_metadata

            write_run_metadata(cfg, state, finished_at_utc="")

            from bsky_fair_collect.collect_index_generators import stage_index_feed_generators
            from bsky_fair_collect.collect_popular import stage_collect_popular
            from bsky_fair_collect.collect_starterpacks import stage_collect_starterpacks
            from bsky_fair_collect.hydrate_feed_generators import stage_hydrate_feed_generators
            from bsky_fair_collect.build_panel import stage_build_feed_panel
            from bsky_fair_collect.snapshot_feeds import stage_snapshot_feeds
            from bsky_fair_collect.hydrate_authors import stage_hydrate_authors
            from bsky_fair_collect.validate import (
                remediate_authors_once,
                remediate_snapshots_once,
                upsert_checks,
                validate_all,
                validate_author_hydration,
                validate_feed_generator_index,
                validate_feed_panel,
                validate_popular,
                validate_snapshots,
                validate_starterpacks,
            )
            from bsky_fair_collect.write_outputs import (
                export_all_csvs,
                finalize_run_metadata,
            )

            try:
                # Core stages with validation + a single remediation loop per stage.
                stage_index_feed_generators(cfg, state, http)
                checks = validate_feed_generator_index(state)
                upsert_checks(state, checks)
                if any(c.status == "FAIL" for c in checks):
                    logger.warning("validation failed after index_generators; retrying stage once")
                    stage_index_feed_generators(cfg, state, http)
                    checks = validate_feed_generator_index(state)
                    upsert_checks(state, checks)
                    if any(c.status == "FAIL" for c in checks):
                        raise RuntimeError("index_generators validations failed (see validation_report.csv)")

                stage_collect_starterpacks(cfg, state, http, session)
                checks = validate_starterpacks(state)
                upsert_checks(state, checks)
                if any(c.status == "FAIL" for c in checks):
                    logger.warning("validation failed after starterpacks; retrying stage once")
                    stage_collect_starterpacks(cfg, state, http, session)
                    checks = validate_starterpacks(state)
                    upsert_checks(state, checks)
                    if any(c.status == "FAIL" for c in checks):
                        raise RuntimeError("starterpacks validations failed (see validation_report.csv)")

                stage_collect_popular(cfg, state, http)
                checks = validate_popular(cfg, state)
                upsert_checks(state, checks)
                if any(c.status == "FAIL" for c in checks):
                    logger.warning("validation failed after popular; retrying stage once")
                    stage_collect_popular(cfg, state, http)
                    checks = validate_popular(cfg, state)
                    upsert_checks(state, checks)
                    if any(c.status == "FAIL" for c in checks):
                        raise RuntimeError("popular validations failed (see validation_report.csv)")

                # Ensure discovery/popular feeds have generator metadata (provider/service DID) even if the
                # relay-index scan has not reached them yet.
                stage_hydrate_feed_generators(cfg, state, http, session)

                stage_build_feed_panel(cfg, state)
                checks = validate_feed_panel(cfg, state)
                upsert_checks(state, checks)
                if any(c.status == "FAIL" for c in checks):
                    logger.warning("validation failed after feed_panel; retrying upstream stages once")
                    stage_index_feed_generators(cfg, state, http)
                    stage_collect_starterpacks(cfg, state, http, session)
                    stage_collect_popular(cfg, state, http)
                    stage_build_feed_panel(cfg, state)
                    checks = validate_feed_panel(cfg, state)
                    upsert_checks(state, checks)
                    if any(c.status == "FAIL" for c in checks):
                        raise RuntimeError("feed_panel validations failed (see validation_report.csv)")

                # Snapshot each feed once per viewer mode. If resuming and a feed already has a
                # snapshot_status row (success or failure), skip it here; remediation handles retries/replacement.
                stage_snapshot_feeds(cfg, state, http, session, retry_failures=False)
                checks = validate_snapshots(cfg, state)
                upsert_checks(state, checks)
                if any(c.status == "FAIL" for c in checks):
                    logger.warning("validation failed after snapshots; running one remediation loop")
                    remediate_snapshots_once(cfg, state, session)
                    checks = validate_snapshots(cfg, state)
                    upsert_checks(state, checks)
                    if any(c.status == "FAIL" for c in checks):
                        raise RuntimeError("snapshot validations failed (see validation_report.csv)")

                stage_hydrate_authors(cfg, state, http)
                checks = validate_author_hydration(state)
                upsert_checks(state, checks)
                if any(c.status == "FAIL" for c in checks):
                    logger.warning("validation failed after author hydration; running one remediation loop")
                    remediate_authors_once(cfg, state)
                    checks = validate_author_hydration(state)
                    upsert_checks(state, checks)
                    if any(c.status == "FAIL" for c in checks):
                        raise RuntimeError("author hydration validations failed (see validation_report.csv)")

                # Final cross-stage validation gate.
                validate_all(cfg, state)

                export_all_csvs(cfg, state)
                finalize_run_metadata(cfg, state)

            except Exception:
                logger.exception("run_all failed; exporting partial outputs for debugging")
                export_all_csvs(cfg, state)
                finalize_run_metadata(cfg, state)
                raise

        finally:
            http.close()
