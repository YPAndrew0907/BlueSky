from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bsky_collector_v2.env import AuthEnv, load_auth_env
from bsky_collector_v2.auth_snapshot import write_auth_preference_snapshot
from bsky_collector_v2.fs_utils import atomic_write_json, ensure_dir
from bsky_collector_v2.http_client import AsyncHttpClient, HttpError, HttpRetryConfig, XrpcHosts
from bsky_collector_v2.layout import Layout
from bsky_collector_v2.manifest import git_sha
from bsky_collector_v2.progress import ProgressReporter, ProgressState
from bsky_collector_v2.quality import assess_feed_generator_index_day
from bsky_collector_v2.request_provenance import (
    JobRequestContextFactory,
    RequestContext,
    RequestOrderTracker,
    RequestProvenanceWriter,
)
from bsky_collector_v2.session import SessionTokens, get_or_create_session, session_cache_path
from bsky_collector_v2.state import ControlState
from bsky_collector_v2.time_utils import format_utc, now_utc, utc_date_str
from bsky_collector_v2.types import FeedUri, RunId
from bsky_collector_v2.writers import CsvPartWriter

logger = logging.getLogger("bsky_collector_v2.job.index_feed_generators")

FEED_GENERATOR_COLLECTION = "app.bsky.feed.generator"
PART_LIMIT = 100
REPOS_PAGE_LIMIT = 500
DEFAULT_RELAY_HOST = "https://bsky.network"


@dataclass(frozen=True)
class FeedGeneratorIndexConfig:
    relay_host: str
    records_host: str
    access_jwt: str | None


@dataclass(frozen=True)
class FeedGeneratorDayPaths:
    day_dir: Path
    parts_dir: Path
    logs_dir: Path
    manifest_path: Path
    progress_path: Path
    http_stats_path: Path
    request_provenance_path: Path


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:  # noqa: BLE001
        return int(default)


def _parse_repo_dids_from_list_repos_by_collection(data: dict[str, Any]) -> list[str]:
    repos = data.get("repos")
    out: list[str] = []
    if isinstance(repos, list):
        for item in repos:
            if isinstance(item, dict):
                did = item.get("did")
                if isinstance(did, str) and did:
                    out.append(did)
            elif isinstance(item, str) and item:
                out.append(item)
    # Some implementations may return `repoDids` or similar.
    alt = data.get("repoDids")
    if isinstance(alt, list):
        for did in alt:
            if isinstance(did, str) and did:
                out.append(did)
    return sorted(set(out))


def _parse_repo_dids_from_list_repos(data: dict[str, Any]) -> list[str]:
    repos = data.get("repos")
    out: list[str] = []
    if isinstance(repos, list):
        for item in repos:
            if isinstance(item, dict):
                did = item.get("did")
                if isinstance(did, str) and did:
                    out.append(did)
            elif isinstance(item, str) and item:
                out.append(item)
    return sorted(set(out))


def _parse_repo_source(value: Any, *, default_host: str) -> tuple[str, str]:
    """Return (host, mode) where mode is 'listReposByCollection' or 'listRepos'."""
    raw = str(value) if isinstance(value, str) and value else ""
    if "::" in raw:
        host, mode = raw.split("::", 1)
        host_out = host.strip() or default_host
        mode_out = mode.strip() or "listReposByCollection"
        return host_out, mode_out
    if raw:
        return raw, "listReposByCollection"
    return default_host, "listReposByCollection"


def _extract_record_feed_uri(record: dict[str, Any]) -> str | None:
    uri = record.get("uri")
    if isinstance(uri, str) and uri:
        return uri
    return None


def _extract_service_did(record: dict[str, Any]) -> str | None:
    value = record.get("value")
    if not isinstance(value, dict):
        return None
    did = value.get("did")
    if isinstance(did, str) and did:
        return did
    return None


def _provider_domain_from_service_did(service_did: str | None) -> str | None:
    if not service_did:
        return None
    if service_did.startswith("did:web:"):
        domain = service_did.removeprefix("did:web:").strip()
        return domain or None
    return None


def _manifest_recovery_path(path: Path) -> Path:
    try:
        out_base = path.parents[3]
        date_str = path.parent.parent.name
        safe_name = f"index-feed-generators_{date_str}_{path.name}"
        return out_base / "control" / "manifest_recovery" / safe_name
    except Exception:  # noqa: BLE001
        return path.with_name(f"{path.stem}.recovered{path.suffix}")


def _build_day_paths(day_dir: Path) -> FeedGeneratorDayPaths:
    return FeedGeneratorDayPaths(
        day_dir=day_dir,
        parts_dir=day_dir / "parts",
        logs_dir=day_dir / "logs",
        manifest_path=day_dir / "run_manifest.json",
        progress_path=day_dir / "progress.json",
        http_stats_path=day_dir / "http_stats.csv",
        request_provenance_path=day_dir / "request_provenance.csv",
    )


def _supports_atomic_writes(dir_path: Path) -> bool:
    ensure_dir(dir_path)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=".probe.",
            dir=str(dir_path),
            delete=False,
        ) as tmp:
            tmp.write("ok\n")
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_path = Path(tmp.name)
        return True
    except OSError as err:
        logger.warning("index output dir not writable for atomic writes dir=%s err=%r", str(dir_path), err)
        return False
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass


def _resolve_day_paths(layout: Layout, *, date_str: str) -> FeedGeneratorDayPaths:
    candidates = (
        layout.feed_generators_index_day_dir(date_str),
        layout.metadata_day(date_str) / "feed_generators_index_recovered",
        layout.control_root / "feed_generators_index_recovered" / date_str,
    )
    primary = candidates[0]
    for candidate in candidates:
        if _supports_atomic_writes(candidate):
            if candidate != primary:
                logger.warning(
                    "using recovered index day dir primary=%s recovered=%s",
                    str(primary),
                    str(candidate),
                )
            return _build_day_paths(candidate)
    raise OSError(f"no writable feed_generators_index day dir for date={date_str}")


def _load_manifest_with_recovery(path: Path) -> tuple[dict[str, Any] | None, Path | None]:
    candidates = (path, _manifest_recovery_path(path))
    for candidate in candidates:
        try:
            if not candidate.exists():
                continue
            loaded = json.loads(candidate.read_text(encoding="utf-8"))
        except OSError as err:
            logger.warning("manifest path unreadable path=%s err=%r", str(candidate), err)
            continue
        except Exception:  # noqa: BLE001
            continue
        if isinstance(loaded, dict):
            return loaded, candidate
    return None, None


def _write_manifest_with_recovery(path: Path, manifest: dict[str, Any]) -> Path:
    try:
        atomic_write_json(path, manifest)
        return path
    except OSError as err:
        fallback = _manifest_recovery_path(path)
        logger.warning(
            "manifest write failed path=%s err=%r; writing recovery manifest path=%s",
            str(path),
            err,
            str(fallback),
        )
        atomic_write_json(fallback, manifest)
        return fallback


def _load_or_init_day_manifest(
    *,
    manifest_path: Path,
    resume: bool,
    started_at_utc: str,
    repo_root: Path,
    params: dict[str, Any],
) -> RunId:
    existing, _ = _load_manifest_with_recovery(manifest_path)

    run_id: str | None = None
    if resume and existing is not None:
        rid = existing.get("run_id")
        if isinstance(rid, str) and rid:
            run_id = rid

    from bsky_collector_v2.manifest import new_run_id

    if not run_id:
        run_id = str(new_run_id())

    manifest: dict[str, Any] = dict(existing or {})
    prior_started = manifest.get("started_at_utc")
    manifest_started = prior_started if isinstance(prior_started, str) and prior_started else started_at_utc
    manifest.update(
        {
            "run_id": run_id,
            "job_name": "index-feed-generators",
            "date_utc": manifest.get("date_utc") or utc_date_str(now_utc()),
            "started_at_utc": manifest_started,
            "git_sha": manifest.get("git_sha") or git_sha(repo_root),
            "params": dict(params),
        }
    )
    _write_manifest_with_recovery(manifest_path, manifest)
    return RunId(run_id)


def _finish_day_manifest(
    path: Path,
    *,
    success: bool,
    error: str | None,
    extra: dict[str, Any] | None,
) -> None:
    existing, _ = _load_manifest_with_recovery(path)
    manifest: dict[str, Any] = dict(existing or {})
    manifest["finished_at_utc"] = format_utc(now_utc())
    manifest["success"] = bool(success)
    if error:
        manifest["error"] = str(error)
    if extra:
        manifest.update({str(k): v for k, v in extra.items()})
    _write_manifest_with_recovery(path, manifest)


def _part_path(parts_dir: Path, *, part_index: int) -> Path:
    return parts_dir / f"feed_generators_part_{int(part_index):06d}.jsonl"


def _read_part_meta(path: Path) -> dict[str, Any] | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            last: str | None = None
            for line in f:
                if line.strip():
                    last = line
    except OSError:
        return None
    if not last:
        return None
    try:
        obj = json.loads(last)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or obj.get("__meta__") is not True:
        return None
    return obj


def _upsert_feed_generator_records(
    *,
    state: ControlState,
    records: list[dict[str, Any]],
    discovered_at_utc: str,
) -> int:
    n = 0
    for rec in records:
        if not isinstance(rec, dict):
            continue
        feed_uri = _extract_record_feed_uri(rec)
        if not feed_uri:
            continue
        creator_did = feed_uri.removeprefix("at://").split("/")[0] if feed_uri.startswith("at://") else None
        service_did = _extract_service_did(rec)
        state.upsert_feed_catalog(
            feed_uri=FeedUri(feed_uri),
            creator_did=creator_did,
            service_did=service_did,
            provider_domain=_provider_domain_from_service_did(service_did),
            like_count_last=None,
            discovered_from=["feed_generators_index"],
            seen_at_utc=discovered_at_utc,
        )
        n += 1
    return n


def _counts_fg_repo_tasks(state: ControlState, *, collection: str) -> dict[str, int]:
    return state.count_feed_generator_index_repo_tasks_by_status(collection=collection)


def _write_checkpoint_json(path: Path, data: dict[str, Any]) -> None:
    atomic_write_json(path, data)


async def _build_index_cfg(
    *,
    http: AsyncHttpClient,
    hosts: XrpcHosts,
    relay_host: str,
    env_path: Path | None,
    control_root: Path,
    request_order_tracker: RequestOrderTracker,
    run_id: RunId,
    sample_family: str,
    collection_params_hash: str,
    date_str: str,
    vantage_id: str,
    auth_snapshot_path: Path,
) -> FeedGeneratorIndexConfig:
    auth_env: AuthEnv | None = None
    tokens: SessionTokens | None = None
    if env_path is not None and env_path.exists():
        try:
            auth_env = load_auth_env(env_path)
        except Exception as err:  # noqa: BLE001
            logger.warning("auth env invalid; sync index may fail env_path=%s err=%r", str(env_path), err)
            auth_env = None

    # The relay host is used to enumerate repos (`com.atproto.sync.*`). Default points at the public relay.
    # The records host is used to read arbitrary repos (`com.atproto.repo.listRecords`); `bsky.social` works today.
    relay_host = str(relay_host).strip() or DEFAULT_RELAY_HOST
    records_host = hosts.pds_host

    if auth_env is not None:
        # Optional: auth can increase reliability/limits on some deployments.
        try:
            session_env = AuthEnv(identifier=auth_env.identifier, app_password=auth_env.app_password, pds_host=records_host)
            refresh_order = request_order_tracker.next()
            create_order = request_order_tracker.next()
            tokens = await get_or_create_session(
                http,
                env=session_env,
                cache_path=session_cache_path(control_root=control_root, env=session_env),
                refresh_request_context=RequestContext(
                    run_id=str(run_id),
                    job_name="index-feed-generators",
                    sample_family=sample_family,
                    collection_params_hash=collection_params_hash,
                    date_utc=date_str,
                    viewer_mode="auth",
                    vantage_id=vantage_id,
                    host_kind="pds_proxy",
                    host=records_host,
                    endpoint="com.atproto.server.refreshSession",
                    request_order_in_run=refresh_order,
                    request_order_in_sweep=refresh_order,
                ),
                create_request_context=RequestContext(
                    run_id=str(run_id),
                    job_name="index-feed-generators",
                    sample_family=sample_family,
                    collection_params_hash=collection_params_hash,
                    date_utc=date_str,
                    viewer_mode="auth",
                    vantage_id=vantage_id,
                    host_kind="pds_proxy",
                    host=records_host,
                    endpoint="com.atproto.server.createSession",
                    request_order_in_run=create_order,
                    request_order_in_sweep=create_order,
                ),
            )
            write_auth_preference_snapshot(
                auth_snapshot_path,
                sample_family=sample_family,
                vantage_id=vantage_id,
                viewer_did=tokens.viewer_did,
                identifier=session_env.identifier,
                pds_host=session_env.pds_host,
                accept_language=None,
                accept_labelers=None,
                include_author_labels=None,
                session_cache_path=session_cache_path(control_root=control_root, env=session_env),
            )
        except Exception as err:  # noqa: BLE001
            logger.warning("auth session failed; proceeding unauth err=%r", err)
            tokens = None

    return FeedGeneratorIndexConfig(
        relay_host=relay_host,
        records_host=records_host,
        access_jwt=tokens.access_jwt if tokens else None,
    )


async def run_index_feed_generators(
    *,
    layout: Layout,
    repo_root: Path,
    hosts: XrpcHosts,
    relay_host: str,
    env_path: Path | None,
    rps: float,
    time_budget_minutes: int,
    resume: bool,
    dry_run: bool,
    accept_language: str | None,
    accept_labelers: str | None,
    vantage_id: str,
) -> None:
    date_str = utc_date_str(now_utc())
    day_paths = _resolve_day_paths(layout, date_str=date_str)
    if dry_run:
        logger.info("dry_run=true: would index feed generators date=%s out=%s", date_str, str(day_paths.day_dir))
        return

    ensure_dir(day_paths.parts_dir)
    ensure_dir(day_paths.logs_dir)

    started_at_utc = format_utc(now_utc())
    run_id = _load_or_init_day_manifest(
        manifest_path=day_paths.manifest_path,
        resume=resume,
        started_at_utc=started_at_utc,
        repo_root=repo_root,
        params={
            "date_utc": date_str,
            "collection": FEED_GENERATOR_COLLECTION,
            "rps": float(rps),
            "time_budget_minutes": int(time_budget_minutes),
            "resume": bool(resume),
            "accept_language": accept_language,
            "accept_labelers": accept_labelers,
            "vantage_id": str(vantage_id).strip() or "unauth",
            "relay_host": str(relay_host).strip() or DEFAULT_RELAY_HOST,
            "records_host": str(hosts.pds_host),
            "auth_env_path": str(env_path) if env_path else None,
        },
    )
    manifest = json.loads(day_paths.manifest_path.read_text(encoding="utf-8"))
    sample_family = str(manifest.get("sample_family") or "feed_generator_index")
    collection_params_hash = str(manifest.get("collection_params_hash") or "")
    request_order_tracker = RequestOrderTracker()

    progress = ProgressState(job_name="index-feed-generators", run_id=run_id, started_at_utc=started_at_utc)
    progress.rps_config = float(rps)
    progress.concurrency = 1
    reporter = ProgressReporter(day_paths.progress_path, progress, write_interval_s=15.0)
    reporter.start()

    http_stats_writer = CsvPartWriter(
        day_paths.http_stats_path,
        fieldnames=["timestamp_utc", "endpoint", "status_code", "latency_ms", "attempt", "error_type", "feed_uri"],
        flush_interval_s=2.0,
        fsync_interval_s=10.0,
    )

    http = AsyncHttpClient(
        hosts=hosts,
        rps=rps,
        retry=HttpRetryConfig(max_retries=2),
        timeout_s=30.0,
        http_stats=http_stats_writer,
        progress=progress,
        accept_language=accept_language,
        accept_labelers=accept_labelers,
        request_provenance_writer=RequestProvenanceWriter(day_paths.request_provenance_path),
    )

    success = False
    error: str | None = None
    cfg: FeedGeneratorIndexConfig | None = None
    try:
        cfg = await _build_index_cfg(
            http=http,
            hosts=hosts,
            relay_host=relay_host,
            env_path=env_path,
            control_root=layout.control_root,
            request_order_tracker=request_order_tracker,
            run_id=run_id,
            sample_family=sample_family,
            collection_params_hash=collection_params_hash,
            date_str=date_str,
            vantage_id=str(vantage_id).strip() or "unauth",
            auth_snapshot_path=layout.feed_generators_index_auth_preference_snapshot_json(date_str),
        )
        http.request_context_factory = JobRequestContextFactory(
            run_id=str(run_id),
            job_name="index-feed-generators",
            sample_family=sample_family,
            collection_params_hash=collection_params_hash,
            appview_host=http.hosts.appview_host,
            pds_host=http.hosts.pds_host,
            relay_host=cfg.relay_host,
            date_utc=date_str,
            viewer_mode=("auth" if cfg.access_jwt else "unauth"),
            vantage_id=str(vantage_id).strip() or "unauth",
            _order_tracker=request_order_tracker,
        )
        logger.info(
            "feed generator index hosts relay_host=%s records_host=%s auth=%s",
            cfg.relay_host,
            cfg.records_host,
            "yes" if cfg.access_jwt else "no",
        )

        checkpoint_path = layout.feed_generators_index_checkpoint_json
        last_checkpoint_write_s = time.monotonic()

        with ControlState.open(layout.control_db_path) as state:
            state.start_run(
                run_id=run_id,
                job_name="index-feed-generators",
                started_at_utc=started_at_utc,
                params={
                    "date_utc": date_str,
                    "collection": FEED_GENERATOR_COLLECTION,
                    "time_budget_minutes": int(time_budget_minutes),
                    "relay_host": cfg.relay_host,
                    "records_host": cfg.records_host,
                    "auth": bool(cfg.access_jwt),
                },
            )
            try:
                default_repo_source = f"{cfg.relay_host}::listReposByCollection"
                state.ensure_feed_generator_index_global(
                    collection=FEED_GENERATOR_COLLECTION,
                    repo_source=default_repo_source,
                    updated_at_utc=started_at_utc,
                )
                global_row0 = state.get_feed_generator_index_global(collection=FEED_GENERATOR_COLLECTION)
                repo_source0 = global_row0["repo_source"] if global_row0 is not None else None
                if not (isinstance(repo_source0, str) and "::" in repo_source0):
                    # Upgrade older state rows (pre repo_source mode tagging).
                    state.update_feed_generator_index_global(
                        collection=FEED_GENERATOR_COLLECTION,
                        repo_source=default_repo_source,
                        repos_cursor=None,
                        repos_done=False,
                        updated_at_utc=started_at_utc,
                    )
                    state.commit()

                reset_n = state.reset_feed_generator_index_in_progress_to_pending(
                    collection=FEED_GENERATOR_COLLECTION, updated_at_utc=started_at_utc
                )
                if reset_n:
                    logger.info("recovered repo tasks in_progress->pending n=%s", int(reset_n))

                # Repair any parts left in_progress (crash between file + DB updates).
                repaired_parts = 0
                for row in list(
                    state.iter_feed_generator_index_in_progress_parts(
                        collection=FEED_GENERATOR_COLLECTION, date_utc=date_str
                    )
                ):
                    part_index = _safe_int(row["part_index"])
                    path = _part_path(day_paths.parts_dir, part_index=part_index)
                    if not path.exists():
                        state.finish_feed_generator_index_part(
                            collection=FEED_GENERATOR_COLLECTION,
                            date_utc=date_str,
                            part_index=part_index,
                            success=False,
                            finished_at_utc=format_utc(now_utc()),
                            n_records=0,
                            last_error="missing_part_file_after_crash",
                        )
                        continue
                    meta = _read_part_meta(path)
                    if not meta:
                        state.finish_feed_generator_index_part(
                            collection=FEED_GENERATOR_COLLECTION,
                            date_utc=date_str,
                            part_index=part_index,
                            success=False,
                            finished_at_utc=format_utc(now_utc()),
                            n_records=0,
                            last_error="invalid_part_meta_after_crash",
                        )
                        continue

                    # Re-apply DB upserts and repo cursor updates from the part file (best-effort).
                    try:
                        repo_did = meta.get("repo_did")
                        if not isinstance(repo_did, str) or not repo_did:
                            raise RuntimeError("missing repo_did in part meta")
                        next_cursor_raw = meta.get("records_cursor_next")
                        next_cursor = (
                            str(next_cursor_raw) if isinstance(next_cursor_raw, str) and next_cursor_raw else None
                        )
                        # Re-read full file to upsert feed URIs (small during repair).
                        records: list[dict[str, Any]] = []
                        for line in path.read_text(encoding="utf-8").splitlines():
                            if not line.strip():
                                continue
                            obj = json.loads(line)
                            if isinstance(obj, dict) and obj.get("__meta__") is True:
                                continue
                            rec = obj.get("record") if isinstance(obj, dict) else None
                            if isinstance(rec, dict):
                                records.append(rec)
                        n_up = _upsert_feed_generator_records(state=state, records=records, discovered_at_utc=started_at_utc)
                        if next_cursor is None:
                            state.mark_feed_generator_index_repo_done(
                                collection=FEED_GENERATOR_COLLECTION,
                                repo_did=repo_did,
                                status="success",
                                cursor=None,
                                updated_at_utc=started_at_utc,
                                last_error=None,
                            )
                        else:
                            state.mark_feed_generator_index_repo_done(
                                collection=FEED_GENERATOR_COLLECTION,
                                repo_did=repo_did,
                                status="pending",
                                cursor=next_cursor,
                                updated_at_utc=started_at_utc,
                                last_error=None,
                            )
                        state.finish_feed_generator_index_part(
                            collection=FEED_GENERATOR_COLLECTION,
                            date_utc=date_str,
                            part_index=part_index,
                            success=True,
                            finished_at_utc=started_at_utc,
                            n_records=n_up,
                            last_error=None,
                        )
                        repaired_parts += 1
                    except Exception as err:  # noqa: BLE001
                        state.finish_feed_generator_index_part(
                            collection=FEED_GENERATOR_COLLECTION,
                            date_utc=date_str,
                            part_index=part_index,
                            success=False,
                            finished_at_utc=format_utc(now_utc()),
                            n_records=0,
                            last_error=f"repair_failed:{err!r}",
                        )
                if repaired_parts:
                    state.commit()
                    logger.info("repaired in_progress parts n=%s", int(repaired_parts))

                # Create a local part index counter for this day (append-only files).
                next_part_index = state.next_feed_generator_index_part_index(
                    collection=FEED_GENERATOR_COLLECTION, date_utc=date_str
                )

                deadline_s = time.monotonic() + max(1, int(time_budget_minutes)) * 60.0
                repos_pages = 0
                repos_enqueued = 0
                repos_processed = 0
                records_written = 0

                def maybe_write_checkpoint() -> None:
                    nonlocal last_checkpoint_write_s
                    now_s = time.monotonic()
                    if (now_s - last_checkpoint_write_s) < 15.0:
                        return
                    last_checkpoint_write_s = now_s
                    global_row = state.get_feed_generator_index_global(collection=FEED_GENERATOR_COLLECTION)
                    counts = _counts_fg_repo_tasks(state, collection=FEED_GENERATOR_COLLECTION)
                    _write_checkpoint_json(
                        checkpoint_path,
                        {
                            "updated_at_utc": format_utc(now_utc()),
                            "collection": FEED_GENERATOR_COLLECTION,
                            "relay_host": cfg.relay_host,
                            "records_host": cfg.records_host,
                            "repo_source": (global_row["repo_source"] if global_row is not None else None),
                            "repos_cursor": (global_row["repos_cursor"] if global_row is not None else None),
                            "repos_done": bool(global_row["repos_done"]) if global_row is not None else False,
                            "repo_task_counts": counts,
                            "parts_next_index": int(next_part_index),
                            "records_written_this_run": int(records_written),
                        },
                    )

                while time.monotonic() < (deadline_s - 2.0):
                    global_row = state.get_feed_generator_index_global(collection=FEED_GENERATOR_COLLECTION)
                    repos_done = bool(global_row["repos_done"]) if global_row is not None else False

                    # Prefer processing repo tasks; only enqueue more when we run out.
                    tasks = state.next_feed_generator_index_repo_tasks(
                        collection=FEED_GENERATOR_COLLECTION, limit=1, max_attempts=3
                    )
                    if not tasks:
                        if repos_done:
                            logger.info("index complete (no pending repo tasks)")
                            break

                        # Enqueue next page of repos that have the collection.
                        cursor_raw = global_row["repos_cursor"] if global_row is not None else None
                        repos_cursor = str(cursor_raw) if isinstance(cursor_raw, str) and cursor_raw else None
                        captured_at_utc = format_utc(now_utc())

                        repo_source = global_row["repo_source"] if global_row is not None else default_repo_source
                        listing_host, listing_mode = _parse_repo_source(repo_source, default_host=cfg.relay_host)

                        repo_dids: list[str] = []
                        next_cursor: str | None = None

                        if listing_mode == "listRepos":
                            params: dict[str, Any] = {"limit": REPOS_PAGE_LIMIT}
                            if repos_cursor:
                                params["cursor"] = repos_cursor
                            resp = await http.xrpc_get(
                                endpoint="com.atproto.sync.listRepos",
                                host=listing_host,
                                method="com.atproto.sync.listRepos",
                                params=params,
                                access_jwt=None,
                                feed_uri=None,
                                timestamp_utc=captured_at_utc,
                            )
                            repo_dids = _parse_repo_dids_from_list_repos(resp.data)
                            next_cursor = resp.get_str("cursor")
                        else:
                            params = {"collection": FEED_GENERATOR_COLLECTION, "limit": REPOS_PAGE_LIMIT}
                            if repos_cursor:
                                params["cursor"] = repos_cursor
                            try:
                                resp = await http.xrpc_get(
                                    endpoint="com.atproto.sync.listReposByCollection",
                                    host=listing_host,
                                    method="com.atproto.sync.listReposByCollection",
                                    params=params,
                                    access_jwt=None,
                                    feed_uri=None,
                                    timestamp_utc=captured_at_utc,
                                )
                            except HttpError as err:
                                msg = str(err).lower()
                                fallback = (
                                    err.status_code in (400, 404, 501)
                                    or "xrpcnotsupported" in msg
                                    or "failed to proxy" in msg
                                    or "method not implemented" in msg
                                )
                                if fallback:
                                    new_repo_source = f"{cfg.relay_host}::listRepos"
                                    logger.warning(
                                        "listReposByCollection unavailable; falling back to listRepos relay_host=%s err=%r",
                                        cfg.relay_host,
                                        err,
                                    )
                                    state.update_feed_generator_index_global(
                                        collection=FEED_GENERATOR_COLLECTION,
                                        repo_source=new_repo_source,
                                        repos_cursor=None,
                                        repos_done=False,
                                        updated_at_utc=captured_at_utc,
                                    )
                                    state.commit()
                                    maybe_write_checkpoint()
                                    continue
                                raise
                            repo_dids = _parse_repo_dids_from_list_repos_by_collection(resp.data)
                            next_cursor = resp.get_str("cursor")

                        inserted = state.ensure_feed_generator_index_repo_tasks(
                            collection=FEED_GENERATOR_COLLECTION,
                            repo_dids=repo_dids,
                            first_seen_utc=captured_at_utc,
                            updated_at_utc=captured_at_utc,
                        )
                        state.update_feed_generator_index_global(
                            collection=FEED_GENERATOR_COLLECTION,
                            repo_source=f"{listing_host}::{listing_mode}",
                            repos_cursor=next_cursor,
                            repos_done=(next_cursor is None),
                            updated_at_utc=captured_at_utc,
                        )
                        state.commit()

                        repos_pages += 1
                        repos_enqueued += int(inserted)
                        logger.info(
                            "enqueued repos page=%s mode=%s inserted=%s repos_returned=%s next_cursor=%s",
                            repos_pages,
                            listing_mode,
                            int(inserted),
                            len(repo_dids),
                            "none" if next_cursor is None else "set",
                        )
                        maybe_write_checkpoint()
                        continue

                    repo_did, records_cursor, _attempts = tasks[0]
                    captured_at_utc = format_utc(now_utc())

                    # Mark repo in progress first (so concurrent runs don't double-process).
                    state.mark_feed_generator_index_repo_in_progress(
                        collection=FEED_GENERATOR_COLLECTION,
                        repo_did=repo_did,
                        started_at_utc=captured_at_utc,
                    )
                    state.commit()

                    part_index: int | None = None
                    try:
                        params2: dict[str, Any] = {
                            "repo": repo_did,
                            "collection": FEED_GENERATOR_COLLECTION,
                            "limit": PART_LIMIT,
                        }
                        if records_cursor:
                            params2["cursor"] = records_cursor

                        resp2 = await http.xrpc_get(
                            endpoint="com.atproto.repo.listRecords",
                            host=cfg.records_host,
                            method="com.atproto.repo.listRecords",
                            params=params2,
                            access_jwt=cfg.access_jwt,
                            feed_uri=None,
                            timestamp_utc=captured_at_utc,
                        )
                        records = resp2.data.get("records")
                        if not isinstance(records, list):
                            raise RuntimeError("listRecords missing records[]")
                        next_records_cursor = resp2.get_str("cursor")
                        rec_dicts = [r for r in records if isinstance(r, dict)]

                        if not rec_dicts:
                            # No feed generators in this repo (at least at this cursor). Keep the queue bounded by
                            # deleting successful, empty tasks.
                            if next_records_cursor is not None:
                                state.mark_feed_generator_index_repo_done(
                                    collection=FEED_GENERATOR_COLLECTION,
                                    repo_did=repo_did,
                                    status="pending",
                                    cursor=next_records_cursor,
                                    updated_at_utc=captured_at_utc,
                                    last_error=None,
                                )
                            else:
                                state.delete_feed_generator_index_repo_task(
                                    collection=FEED_GENERATOR_COLLECTION, repo_did=repo_did
                                )
                            state.commit()
                            repos_processed += 1
                            maybe_write_checkpoint()
                            continue

                        part_index = int(next_part_index)
                        next_part_index += 1
                        state.start_feed_generator_index_part(
                            collection=FEED_GENERATOR_COLLECTION,
                            date_utc=date_str,
                            part_index=part_index,
                            started_at_utc=captured_at_utc,
                        )
                        state.commit()

                        part_path = _part_path(day_paths.parts_dir, part_index=part_index)
                        if part_path.exists():
                            raise RuntimeError(
                                f"refusing to overwrite existing part file: {part_path} (use --resume)"
                            )

                        # Write part file first; DB will be updated afterwards (repairable).
                        tmp_path = part_path.with_name(part_path.name + ".tmp")
                        payload_lines: list[str] = []
                        for pos, rec in enumerate(rec_dicts):
                            payload_lines.append(
                                json.dumps(
                                    {
                                        "captured_at_utc": captured_at_utc,
                                        "part_index": part_index,
                                        "repo_did": repo_did,
                                        "records_cursor_start": records_cursor,
                                        "position": pos,
                                        "record": rec,
                                    },
                                    ensure_ascii=False,
                                )
                            )
                        payload_lines.append(
                            json.dumps(
                                {
                                    "__meta__": True,
                                    "captured_at_utc": captured_at_utc,
                                    "part_index": part_index,
                                    "repo_did": repo_did,
                                    "records_cursor_start": records_cursor,
                                    "records_cursor_next": next_records_cursor,
                                    "n_records": len(rec_dicts),
                                },
                                ensure_ascii=False,
                            )
                        )
                        payload = "\n".join(payload_lines) + "\n"
                        with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
                            f.write(payload)
                            f.flush()
                            os.fsync(f.fileno())
                        os.replace(tmp_path, part_path)

                        n_up = _upsert_feed_generator_records(
                            state=state, records=rec_dicts, discovered_at_utc=captured_at_utc
                        )
                        if next_records_cursor is not None:
                            state.mark_feed_generator_index_repo_done(
                                collection=FEED_GENERATOR_COLLECTION,
                                repo_did=repo_did,
                                status="pending",
                                cursor=next_records_cursor,
                                updated_at_utc=captured_at_utc,
                                last_error=None,
                            )
                        else:
                            state.delete_feed_generator_index_repo_task(
                                collection=FEED_GENERATOR_COLLECTION, repo_did=repo_did
                            )
                        state.finish_feed_generator_index_part(
                            collection=FEED_GENERATOR_COLLECTION,
                            date_utc=date_str,
                            part_index=part_index,
                            success=True,
                            finished_at_utc=captured_at_utc,
                            n_records=n_up,
                            last_error=None,
                        )
                        state.commit()

                        repos_processed += 1
                        records_written += int(n_up)
                        progress.feeds_done += int(n_up)
                        progress.add_rows("feed_generators_index_records", int(n_up))
                        logger.info(
                            "indexed repo=%s part=%s records=%s next_cursor=%s",
                            repo_did,
                            part_index,
                            int(n_up),
                            "none" if next_records_cursor is None else "set",
                        )
                        maybe_write_checkpoint()

                    except HttpError as err:
                        state.mark_feed_generator_index_repo_done(
                            collection=FEED_GENERATOR_COLLECTION,
                            repo_did=repo_did,
                            status="failed",
                            cursor=records_cursor,
                            updated_at_utc=captured_at_utc,
                            last_error=repr(err),
                        )
                        if part_index is not None:
                            state.finish_feed_generator_index_part(
                                collection=FEED_GENERATOR_COLLECTION,
                                date_utc=date_str,
                                part_index=part_index,
                                success=False,
                                finished_at_utc=captured_at_utc,
                                n_records=0,
                                last_error=repr(err),
                            )
                        state.commit()
                        progress.feeds_failed += 1
                        logger.warning("listRecords failed repo=%s err=%r", repo_did, err)
                        maybe_write_checkpoint()
                        continue

                    except Exception as err:  # noqa: BLE001
                        state.mark_feed_generator_index_repo_done(
                            collection=FEED_GENERATOR_COLLECTION,
                            repo_did=repo_did,
                            status="failed",
                            cursor=records_cursor,
                            updated_at_utc=captured_at_utc,
                            last_error=repr(err),
                        )
                        if part_index is not None:
                            state.finish_feed_generator_index_part(
                                collection=FEED_GENERATOR_COLLECTION,
                                date_utc=date_str,
                                part_index=part_index,
                                success=False,
                                finished_at_utc=captured_at_utc,
                                n_records=0,
                                last_error=repr(err),
                            )
                        state.commit()
                        progress.feeds_failed += 1
                        logger.warning("repo processing failed repo=%s err=%r", repo_did, err)
                        maybe_write_checkpoint()
                        continue

                # Publish an updated feed_catalog.csv snapshot under today's metadata for downstream jobs.
                from bsky_collector_v2.jobs.refresh_discovery import _export_feed_catalog_csv

                _export_feed_catalog_csv(state=state, out_csv=layout.feed_catalog_csv(date_str))

                # Final checkpoint snapshot.
                global_row = state.get_feed_generator_index_global(collection=FEED_GENERATOR_COLLECTION)
                counts = _counts_fg_repo_tasks(state, collection=FEED_GENERATOR_COLLECTION)
                _write_checkpoint_json(
                    checkpoint_path,
                    {
                        "updated_at_utc": format_utc(now_utc()),
                        "collection": FEED_GENERATOR_COLLECTION,
                        "relay_host": cfg.relay_host,
                        "records_host": cfg.records_host,
                        "repo_source": (global_row["repo_source"] if global_row is not None else None),
                        "repos_cursor": (global_row["repos_cursor"] if global_row is not None else None),
                        "repos_done": bool(global_row["repos_done"]) if global_row is not None else False,
                        "repo_task_counts": counts,
                        "parts_next_index": int(next_part_index),
                        "records_written_this_run": int(records_written),
                    },
                )

                success = True
            finally:
                state.finish_run(run_id=run_id, finished_at_utc=format_utc(now_utc()), success=success)
    except Exception as err:  # noqa: BLE001
        error = repr(err)
        raise
    finally:
        try:
            await http.aclose()
        finally:
            if http.request_provenance_writer is not None:
                http.request_provenance_writer.close()
            http_stats_writer.close()
            reporter.stop()
            _finish_day_manifest(
                day_paths.manifest_path,
                success=success,
                error=error,
                extra={
                    "collection": FEED_GENERATOR_COLLECTION,
                    "relay_host": cfg.relay_host if cfg else None,
                    "records_host": cfg.records_host if cfg else None,
                },
            )
            try:
                atomic_write_json(
                    layout.feed_generators_index_quality_report_json(date_str),
                    assess_feed_generator_index_day(layout, date_yyyy_mm_dd=date_str),
                )
            except Exception as err:  # noqa: BLE001
                logger.warning("quality report write failed job=index-feed-generators date=%s err=%r", date_str, err)
