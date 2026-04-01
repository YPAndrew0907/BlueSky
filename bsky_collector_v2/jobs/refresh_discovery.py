from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, TypeVar

from bsky_collector_v2.env import AuthEnv, load_auth_env
from bsky_collector_v2.auth_snapshot import write_auth_preference_snapshot
from bsky_collector_v2.fs_utils import atomic_write_json, ensure_dir
from bsky_collector_v2.http_client import AsyncHttpClient, HttpError, HttpRetryConfig, XrpcHosts
from bsky_collector_v2.instrumentation import enrich_manifest
from bsky_collector_v2.layout import Layout
from bsky_collector_v2.parse_utils import provider_domain_from_service_did
from bsky_collector_v2.progress import ProgressReporter, ProgressState
from bsky_collector_v2.quality import assess_discovery_day
from bsky_collector_v2.request_provenance import (
    JobRequestContextFactory,
    RequestContext,
    RequestOrderTracker,
    RequestProvenanceWriter,
)
from bsky_collector_v2.session import get_or_create_session, session_cache_path
from bsky_collector_v2.state import ControlState
from bsky_collector_v2.time_utils import format_utc, now_utc, utc_date_str
from bsky_collector_v2.types import FeedUri, RunId, ViewerMode
from bsky_collector_v2.writers import CsvPartWriter, JsonlWriter

logger = logging.getLogger("bsky_collector_v2.job.refresh_discovery")

T = TypeVar("T")

_SURFACE_NAMES: tuple[str, ...] = (
    "popular_feed_generators",
    "suggested_feeds",
    "suggested_accounts",
    "onboarding_suggested_starterpacks",
    "suggested_follows_by_actor",
    "feed_catalog_likecount_hydrate",
    "feed_catalog_export",
)


class DiscoverySurfaceSkip(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = str(reason)


@dataclass(frozen=True)
class DiscoveryOutputs:
    day_dir: Path
    sources_dir: Path
    popular_feed_generators_jsonl: Path
    suggested_feeds_jsonl: Path
    suggested_accounts_jsonl: Path
    suggested_follows_by_actor_jsonl: Path
    onboarding_suggested_starterpacks_jsonl: Path
    feed_catalog_csv: Path
    starterpack_feeds_csv: Path
    starterpack_accounts_csv: Path
    suggested_feeds_csv: Path
    suggested_accounts_csv: Path
    suggested_follows_by_actor_csv: Path


def _outputs(layout: Layout, date_yyyy_mm_dd: str) -> DiscoveryOutputs:
    day_dir = layout.metadata_day(date_yyyy_mm_dd)
    sources = layout.discovery_sources_dir(date_yyyy_mm_dd)
    return DiscoveryOutputs(
        day_dir=day_dir,
        sources_dir=sources,
        popular_feed_generators_jsonl=sources / "popular_feed_generators.jsonl",
        suggested_feeds_jsonl=sources / "suggested_feeds.jsonl",
        suggested_accounts_jsonl=sources / "suggested_accounts.jsonl",
        suggested_follows_by_actor_jsonl=sources / "suggested_follows_by_actor.jsonl",
        onboarding_suggested_starterpacks_jsonl=sources / "onboarding_suggested_starterpacks.jsonl",
        feed_catalog_csv=layout.feed_catalog_csv(date_yyyy_mm_dd),
        starterpack_feeds_csv=layout.starterpack_feeds_csv(date_yyyy_mm_dd),
        starterpack_accounts_csv=layout.starterpack_accounts_csv(date_yyyy_mm_dd),
        suggested_feeds_csv=layout.suggested_feeds_csv(date_yyyy_mm_dd),
        suggested_accounts_csv=layout.suggested_accounts_csv(date_yyyy_mm_dd),
        suggested_follows_by_actor_csv=layout.suggested_follows_by_actor_csv(date_yyyy_mm_dd),
    )


async def run_refresh_discovery(
    *,
    layout: Layout,
    repo_root: Path,
    run_id: RunId,
    hosts: XrpcHosts,
    env_path: Path | None,
    viewer_modes: tuple[str, ...],
    rps: float,
    concurrency: int,
    accept_language: str | None,
    accept_labelers: str | None,
    vantage_id_unauth: str,
    vantage_id_auth: str,
    resume: bool,
    dry_run: bool,
) -> None:
    date_str = utc_date_str(now_utc())
    out = _outputs(layout, date_str)
    if dry_run:
        logger.info("dry_run=true: would write metadata under %s", str(out.day_dir))
        return

    ensure_dir(out.sources_dir)

    started_at_utc = format_utc(now_utc())

    with ControlState.open(layout.control_db_path) as state:
        status_path = out.day_dir / "discovery_status.json"
        manifest_path = layout.metadata_manifest_json(date_str)
        existing_status: dict[str, Any] | None = None
        if resume and status_path.exists():
            try:
                loaded = json.loads(status_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    existing_status = loaded
            except Exception:  # noqa: BLE001
                existing_status = None

        # Prefer existing run_id when resuming (stable run_id across attempts).
        effective_run_id = run_id
        if resume and existing_status is not None:
            rid = existing_status.get("run_id")
            if isinstance(rid, str) and rid:
                effective_run_id = RunId(rid)

        manifest: dict[str, Any] = {}
        if resume and manifest_path.exists():
            try:
                loaded_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if isinstance(loaded_manifest, dict):
                    manifest = loaded_manifest
            except Exception:  # noqa: BLE001
                manifest = {}
        manifest_started_at = manifest.get("started_at_utc")
        if not isinstance(manifest_started_at, str) or not manifest_started_at:
            manifest_started_at = started_at_utc
        manifest.update(
            {
                "run_id": str(effective_run_id),
                "job_name": "refresh-discovery",
                "date_utc": date_str,
                "started_at_utc": manifest_started_at,
                "params": {
                    "viewer_modes": list(viewer_modes),
                    "rps": float(rps),
                    "concurrency": int(concurrency),
                    "accept_language": accept_language,
                    "accept_labelers": accept_labelers,
                    "vantage_id_unauth": str(vantage_id_unauth).strip() or "unauth",
                    "vantage_id_auth": str(vantage_id_auth).strip() or "auth",
                    "resume": bool(resume),
                },
            }
        )
        enrich_manifest(
            manifest,
            job_name="refresh-discovery",
            out_base=layout.out_base,
            params=manifest["params"],
        )
        atomic_write_json(manifest_path, manifest)

        state.start_run(run_id=effective_run_id, job_name="refresh-discovery", started_at_utc=started_at_utc, params={})
        success = False
        sample_family = str(manifest.get("sample_family") or "discovery_metadata")
        collection_params_hash = str(manifest.get("collection_params_hash") or "")

        auth_env: AuthEnv | None = None
        if env_path is not None and env_path.exists():
            try:
                auth_env = load_auth_env(env_path)
            except Exception as err:  # noqa: BLE001
                logger.warning("auth env invalid; auth mode disabled err=%r", err)
                auth_env = None

        enabled_modes: list[ViewerMode] = []
        if "unauth" in viewer_modes:
            enabled_modes.append("unauth")
        if "auth" in viewer_modes and auth_env is not None:
            enabled_modes.append("auth")
        if "auth" in viewer_modes and auth_env is None:
            logger.warning("auth viewer_mode requested but no auth env available; skipping auth mode")
        if not enabled_modes:
            raise RuntimeError("no viewer modes enabled")

        vantage_by_mode: dict[ViewerMode, str] = {
            "unauth": str(vantage_id_unauth).strip() or "unauth",
            "auth": str(vantage_id_auth).strip() or "auth",
        }

        # Touch JSONL outputs early so reruns always find expected files even if a surface is skipped/failed.
        for p in (
            out.popular_feed_generators_jsonl,
            out.suggested_feeds_jsonl,
            out.suggested_accounts_jsonl,
            out.suggested_follows_by_actor_jsonl,
            out.onboarding_suggested_starterpacks_jsonl,
        ):
            JsonlWriter(p).close()

        starterpack_feeds_writer: CsvPartWriter | None = None
        starterpack_feeds_writer = CsvPartWriter(
            out.starterpack_feeds_csv,
            fieldnames=[
                "pack_uri",
                "pack_creator",
                "joinedWeekCount",
                "joinedAllTimeCount",
                "feed_uri",
                "slot_index",
                "captured_at_utc",
                "vantage_id",
                "source",
            ],
            flush_interval_s=2.0,
            fsync_interval_s=10.0,
        )

        starterpack_accounts_writer: CsvPartWriter | None = None
        starterpack_accounts_writer = CsvPartWriter(
            out.starterpack_accounts_csv,
            fieldnames=[
                "pack_uri",
                "list_uri",
                "subject_did",
                "position",
                "captured_at_utc",
                "vantage_id",
                "source",
            ],
            flush_interval_s=2.0,
            fsync_interval_s=10.0,
        )

        suggested_feeds_writer: CsvPartWriter | None = None
        suggested_feeds_writer = CsvPartWriter(
            out.suggested_feeds_csv,
            fieldnames=["feed_uri", "position", "captured_at_utc", "vantage_id"],
            flush_interval_s=2.0,
            fsync_interval_s=10.0,
        )

        suggested_accounts_writer: CsvPartWriter | None = None
        suggested_accounts_writer = CsvPartWriter(
            out.suggested_accounts_csv,
            fieldnames=["actor_did", "position", "captured_at_utc", "vantage_id"],
            flush_interval_s=2.0,
            fsync_interval_s=10.0,
        )

        suggested_follows_writer: CsvPartWriter | None = None
        suggested_follows_writer = CsvPartWriter(
            out.suggested_follows_by_actor_csv,
            fieldnames=[
                "seed_actor_did",
                "suggested_did",
                "position",
                "isFallback",
                "captured_at_utc",
                "vantage_id",
            ],
            flush_interval_s=2.0,
            fsync_interval_s=10.0,
        )

        progress_state = ProgressState(job_name="refresh-discovery", run_id=effective_run_id, started_at_utc=started_at_utc)
        progress = ProgressReporter(path=out.day_dir / "progress.json", state=progress_state, write_interval_s=15.0)
        progress.start()

        status: dict[str, Any] = dict(existing_status or {})
        status.setdefault("job_name", "refresh-discovery")
        status.setdefault("date_utc", date_str)
        status["run_id"] = str(effective_run_id)
        status.setdefault("started_at_utc", started_at_utc)
        status["updated_at_utc"] = format_utc(now_utc())
        status.setdefault("surfaces", {})
        status.setdefault("viewer_mode", None)
        status.setdefault("vantage_id", None)
        atomic_write_json(status_path, status)

        def update_surface(
            surface: str,
            *,
            surface_status: str,
            started_at: str | None = None,
            finished_at: str | None = None,
            error: str | None = None,
            details: dict[str, Any] | None = None,
        ) -> None:
            surfaces = status.setdefault("surfaces", {})
            cur = dict(surfaces.get(surface) or {})
            cur["status"] = str(surface_status)
            if started_at:
                cur.setdefault("started_at_utc", str(started_at))
            if finished_at:
                cur["finished_at_utc"] = str(finished_at)
            if error:
                cur["error"] = str(error)
            if details:
                cur["details"] = dict(details)
            surfaces[surface] = cur
            status["updated_at_utc"] = format_utc(now_utc())
            atomic_write_json(status_path, status)

        def read_latest_seed_actor_dids(path: Path, *, limit: int) -> list[str]:
            if limit <= 0 or not path.exists():
                return []
            latest_ts: str | None = None
            latest_by_pos: dict[int, str] = {}
            seen: set[str] = set()
            import csv

            with open(path, "r", encoding="utf-8", newline="") as f:
                r = csv.DictReader(f)
                for row in r:
                    ts = (row.get("captured_at_utc") or "").strip()
                    did = (row.get("actor_did") or "").strip()
                    pos_s = (row.get("position") or "").strip()
                    if not ts or not did:
                        continue
                    try:
                        pos = int(pos_s)
                    except ValueError:
                        continue
                    if latest_ts is None or ts > latest_ts:
                        latest_ts = ts
                        latest_by_pos = {}
                        seen = set()
                    if ts != latest_ts:
                        continue
                    if did in seen:
                        continue
                    seen.add(did)
                    latest_by_pos[pos] = did
            return [did for _pos, did in sorted(latest_by_pos.items())][: int(limit)]

        def resume_seed_actor_dids(surface_status: dict[str, Any]) -> list[str]:
            details = surface_status.get("details")
            if isinstance(details, dict):
                dids = details.get("seed_actor_dids")
                if isinstance(dids, list):
                    out_dids = [str(d) for d in dids if isinstance(d, str) and d]
                    if out_dids:
                        return out_dids[:25]
            return read_latest_seed_actor_dids(out.suggested_accounts_csv, limit=25)

        async def run_surface(
            surface: str,
            coro: Awaitable[T],
            *,
            default: T,
            details_fn: Callable[[T], dict[str, Any]] | None = None,
            resume_value_from_status: Callable[[dict[str, Any]], T] | None = None,
            critical: bool = False,
        ) -> T:
            if surface not in _SURFACE_NAMES:
                raise ValueError(f"unknown discovery surface: {surface}")
            existing = (status.get("surfaces") or {}).get(surface)
            if not critical and resume and isinstance(existing, dict) and existing.get("status") == "success":
                logger.info("resume=true: skipping completed surface=%s", surface)
                # Avoid "coroutine was never awaited" warnings when resume short-circuits.
                try:
                    import inspect

                    if inspect.iscoroutine(coro):
                        coro.close()
                except Exception:  # noqa: BLE001
                    pass
                if resume_value_from_status is not None:
                    try:
                        return resume_value_from_status(existing)
                    except Exception:  # noqa: BLE001
                        return default
                return default

            started = format_utc(now_utc())
            update_surface(surface, surface_status="in_progress", started_at=started)
            try:
                result = await coro
            except DiscoverySurfaceSkip as err:
                finished = format_utc(now_utc())
                logger.warning("discovery surface skipped surface=%s reason=%s", surface, err.reason)
                update_surface(surface, surface_status="skipped", finished_at=finished, error=err.reason)
                if critical:
                    raise
                return default
            except Exception as err:  # noqa: BLE001
                finished = format_utc(now_utc())
                logger.warning("discovery surface failed surface=%s err=%r", surface, err)
                update_surface(surface, surface_status="failed", finished_at=finished, error=repr(err))
                if critical:
                    raise
                return default
            else:
                finished = format_utc(now_utc())
                details: dict[str, Any] | None = None
                if details_fn is not None:
                    try:
                        details = details_fn(result)
                    except Exception:  # noqa: BLE001
                        details = None
                update_surface(surface, surface_status="success", finished_at=finished, details=details)
                return result

        try:
            http = AsyncHttpClient(
                hosts=hosts,
                rps=rps,
                retry=HttpRetryConfig(max_retries=2),
                timeout_s=30.0,
                http_stats=None,
                progress=progress_state,
                accept_language=accept_language,
                accept_labelers=accept_labelers,
                request_provenance_writer=RequestProvenanceWriter(layout.metadata_request_provenance_csv(date_str)),
            )
            try:
                captured_at_utc = format_utc(now_utc())
                auth_access_jwt: str | None = None
                auth_viewer_did: str | None = None
                request_order_tracker = RequestOrderTracker()
                if "auth" in enabled_modes and auth_env is not None:
                    auth_cache_path = session_cache_path(control_root=layout.control_root, env=auth_env)
                    refresh_order = request_order_tracker.next()
                    create_order = request_order_tracker.next()
                    tokens = await get_or_create_session(
                        http,
                        env=auth_env,
                        cache_path=auth_cache_path,
                        refresh_request_context=RequestContext(
                            run_id=str(effective_run_id),
                            job_name="refresh-discovery",
                            sample_family=sample_family,
                            collection_params_hash=collection_params_hash,
                            date_utc=date_str,
                            viewer_mode="auth",
                            vantage_id=vantage_by_mode["auth"],
                            host_kind="pds_proxy",
                            host=auth_env.pds_host,
                            endpoint="com.atproto.server.refreshSession",
                            request_order_in_run=refresh_order,
                            request_order_in_sweep=refresh_order,
                        ),
                        create_request_context=RequestContext(
                            run_id=str(effective_run_id),
                            job_name="refresh-discovery",
                            sample_family=sample_family,
                            collection_params_hash=collection_params_hash,
                            date_utc=date_str,
                            viewer_mode="auth",
                            vantage_id=vantage_by_mode["auth"],
                            host_kind="pds_proxy",
                            host=auth_env.pds_host,
                            endpoint="com.atproto.server.createSession",
                            request_order_in_run=create_order,
                            request_order_in_sweep=create_order,
                        ),
                    )
                    auth_access_jwt = tokens.access_jwt
                    auth_viewer_did = tokens.viewer_did
                    write_auth_preference_snapshot(
                        layout.metadata_auth_preference_snapshot_json(date_str),
                        sample_family=sample_family,
                        vantage_id=vantage_by_mode["auth"],
                        viewer_did=tokens.viewer_did,
                        identifier=auth_env.identifier,
                        pds_host=auth_env.pds_host,
                        accept_language=accept_language,
                        accept_labelers=accept_labelers,
                        include_author_labels=False,
                        session_cache_path=auth_cache_path,
                    )

                primary_mode: ViewerMode = "auth" if ("auth" in enabled_modes and auth_access_jwt) else "unauth"
                primary_access_jwt = auth_access_jwt if primary_mode == "auth" else None
                primary_viewer_did = auth_viewer_did if primary_mode == "auth" else None
                primary_vantage_id = vantage_by_mode[primary_mode]
                http.request_context_factory = JobRequestContextFactory(
                    run_id=str(effective_run_id),
                    job_name="refresh-discovery",
                    sample_family=sample_family,
                    collection_params_hash=collection_params_hash,
                    appview_host=http.hosts.appview_host,
                    pds_host=http.hosts.pds_host,
                    date_utc=date_str,
                    viewer_mode=primary_mode,
                    vantage_id=primary_vantage_id,
                    _order_tracker=request_order_tracker,
                )
                status["viewer_mode"] = primary_mode
                status["vantage_id"] = primary_vantage_id
                atomic_write_json(status_path, status)
                manifest["viewer_mode"] = primary_mode
                manifest["vantage_id"] = primary_vantage_id
                atomic_write_json(manifest_path, manifest)

                popular_n = await run_surface(
                    "popular_feed_generators",
                    _collect_popular(
                        http=http,
                        state=state,
                        out_jsonl=out.popular_feed_generators_jsonl,
                        captured_at_utc=captured_at_utc,
                        access_jwt=primary_access_jwt,
                    ),
                    default=0,
                    details_fn=lambda n: {"feeds_collected": int(n)},
                )

                suggested_feeds_n = await run_surface(
                    "suggested_feeds",
                    _collect_suggested_feeds(
                        http=http,
                        state=state,
                        out_jsonl=out.suggested_feeds_jsonl,
                        out_csv=suggested_feeds_writer,
                        captured_at_utc=captured_at_utc,
                        vantage_id=primary_vantage_id,
                        access_jwt=primary_access_jwt,
                    ),
                    default=0,
                    details_fn=lambda n: {"feeds_collected": int(n)},
                )

                seed_dids = await run_surface(
                    "suggested_accounts",
                    _collect_suggested_accounts(
                        http=http,
                        out_jsonl=out.suggested_accounts_jsonl,
                        out_csv=suggested_accounts_writer,
                        captured_at_utc=captured_at_utc,
                        vantage_id=primary_vantage_id,
                        access_jwt=primary_access_jwt,
                        viewer_did=primary_viewer_did,
                    ),
                    default=[],
                    details_fn=lambda dids: {"actors_collected": len(dids), "seed_actor_dids": list(dids[:25])},
                    resume_value_from_status=resume_seed_actor_dids,
                )

                onboarding_n = await run_surface(
                    "onboarding_suggested_starterpacks",
                    _collect_onboarding_starterpacks(
                        http=http,
                        state=state,
                        out_jsonl=out.onboarding_suggested_starterpacks_jsonl,
                        starterpack_feeds=starterpack_feeds_writer,
                        starterpack_accounts=starterpack_accounts_writer,
                        captured_at_utc=captured_at_utc,
                        vantage_id=primary_vantage_id,
                        concurrency=concurrency,
                        access_jwt=primary_access_jwt,
                        viewer_did=primary_viewer_did,
                    ),
                    default=0,
                    details_fn=lambda n: {"packs_collected": int(n)},
                )

                # Suggested follows depends on having seed accounts. If empty (or surface skipped), record as skipped.
                if seed_dids:
                    seed_n = min(25, len(seed_dids))
                    follows_n = await run_surface(
                        "suggested_follows_by_actor",
                        _collect_suggested_follows_by_actor(
                            http=http,
                            out_jsonl=out.suggested_follows_by_actor_jsonl,
                            out_csv=suggested_follows_writer,
                            captured_at_utc=captured_at_utc,
                            vantage_id=primary_vantage_id,
                            seed_actor_dids=seed_dids[:25],
                            concurrency=concurrency,
                            access_jwt=primary_access_jwt,
                            viewer_did=primary_viewer_did,
                        ),
                        default=0,
                        details_fn=lambda n: {"seed_actors": int(seed_n), "rows": int(n)},
                    )
                else:
                    update_surface("suggested_follows_by_actor", surface_status="skipped", error="no_seed_accounts")

                await run_surface(
                    "feed_catalog_likecount_hydrate",
                    _hydrate_feed_catalog_like_counts(
                        http=http,
                        state=state,
                        captured_at_utc=captured_at_utc,
                        access_jwt=primary_access_jwt,
                        concurrency=concurrency,
                    ),
                    default={},
                    details_fn=lambda d: dict(d),
                )

                state.commit()
                # Always attempt export, even if some surfaces failed.
                async def export_feed_catalog() -> None:
                    _export_feed_catalog_csv(state=state, out_csv=out.feed_catalog_csv)

                await run_surface(
                    "feed_catalog_export",
                    export_feed_catalog(),
                    default=None,
                    critical=True,
                )
                logger.info("wrote feed_catalog.csv path=%s", str(out.feed_catalog_csv))
                success = True
            finally:
                if http.request_provenance_writer is not None:
                    http.request_provenance_writer.close()
                await http.aclose()

        finally:
            for w in (
                starterpack_feeds_writer,
                starterpack_accounts_writer,
                suggested_feeds_writer,
                suggested_accounts_writer,
                suggested_follows_writer,
            ):
                if w is not None:
                    w.close()
            progress.stop()
            status["finished_at_utc"] = format_utc(now_utc())
            status["success"] = bool(success)
            atomic_write_json(status_path, status)
            manifest["finished_at_utc"] = format_utc(now_utc())
            manifest["success"] = bool(success)
            atomic_write_json(manifest_path, manifest)
            try:
                from bsky_collector_v2.effective_csv import refresh_key_views, sync_metadata_day

                sync_metadata_day(layout, date_yyyy_mm_dd=date_str)
                refresh_key_views(layout)
            except Exception as err:  # noqa: BLE001
                logger.warning("effective csv sync failed job=refresh-discovery date=%s err=%r", date_str, err)
            try:
                atomic_write_json(layout.metadata_quality_report_json(date_str), assess_discovery_day(layout, date_yyyy_mm_dd=date_str))
            except Exception as err:  # noqa: BLE001
                logger.warning("quality report write failed job=refresh-discovery date=%s err=%r", date_str, err)
            state.finish_run(run_id=effective_run_id, finished_at_utc=format_utc(now_utc()), success=success)


async def _collect_popular(
    *,
    http: AsyncHttpClient,
    state: ControlState,
    out_jsonl: Path,
    captured_at_utc: str,
    access_jwt: str | None,
) -> int:
    cursor: str | None = None
    total = 0
    with JsonlWriter(out_jsonl) as w:
        while True:
            resp = await http.xrpc_get(
                endpoint="app.bsky.unspecced.getPopularFeedGenerators",
                host=http.hosts.appview_host,
                method="app.bsky.unspecced.getPopularFeedGenerators",
                params={"limit": 100, **({"cursor": cursor} if cursor else {})},
                access_jwt=access_jwt,
                feed_uri=None,
                timestamp_utc=captured_at_utc,
            )
            feeds = resp.data.get("feeds")
            if not isinstance(feeds, list):
                raise RuntimeError("popular endpoint missing feeds[]")

            for item in feeds:
                if not isinstance(item, dict):
                    continue
                w.write_obj({"captured_at_utc": captured_at_utc, "source": "popular", "item": item})
                feed_uri = item.get("uri")
                if not isinstance(feed_uri, str) or not feed_uri:
                    continue
                creator_did = _deep_get_str(item, ("creator", "did"))
                service_did = item.get("did") if isinstance(item.get("did"), str) else None
                like_count = item.get("likeCount") if isinstance(item.get("likeCount"), int) else None
                provider_domain = provider_domain_from_service_did(service_did)
                state.upsert_feed_catalog(
                    feed_uri=FeedUri(feed_uri),
                    creator_did=creator_did,
                    service_did=service_did,
                    provider_domain=provider_domain,
                    like_count_last=like_count,
                    discovered_from=["popular_feed_generators"],
                    seen_at_utc=captured_at_utc,
                )
                total += 1

            state.commit()
            cursor = resp.data.get("cursor") if isinstance(resp.data.get("cursor"), str) else None
            if not cursor:
                break

    logger.info("popular collected=%s", total)
    return total


async def _collect_suggested_feeds(
    *,
    http: AsyncHttpClient,
    state: ControlState,
    out_jsonl: Path,
    out_csv: CsvPartWriter | None,
    captured_at_utc: str,
    vantage_id: str,
    access_jwt: str | None,
) -> int:
    methods = (
        "app.bsky.feed.getSuggestedFeeds",
        "app.bsky.unspecced.getSuggestedFeeds",
    )
    cursor: str | None = None
    position = 0
    total = 0

    with JsonlWriter(out_jsonl) as w:
        while True:
            resp: AsyncHttpClient.XrpcResponse | None = None
            last_err: Exception | None = None
            for method in methods:
                try:
                    resp = await http.xrpc_get(
                        endpoint=method,
                        host=http.hosts.appview_host,
                        method=method,
                        params={"limit": 100, **({"cursor": cursor} if cursor else {})},
                        access_jwt=access_jwt,
                        feed_uri=None,
                        timestamp_utc=captured_at_utc,
                    )
                    break
                except Exception as err:  # noqa: BLE001
                    last_err = err
                    resp = None
                    continue
            if resp is None:
                if isinstance(last_err, HttpError) and last_err.status_code == 404:
                    raise DiscoverySurfaceSkip("suggested_feeds_not_supported") from last_err
                raise RuntimeError(f"suggested feeds unsupported or failed: {last_err!r}") from last_err

            feeds = resp.data.get("feeds")
            if not isinstance(feeds, list):
                feeds = resp.data.get("suggestions")
            if not isinstance(feeds, list):
                feeds = resp.data.get("suggested")
            if not isinstance(feeds, list):
                raise RuntimeError("suggested feeds response missing feeds[]")

            for item in feeds:
                pos = position
                position += 1
                if not isinstance(item, dict):
                    continue
                w.write_obj(
                    {
                        "captured_at_utc": captured_at_utc,
                        "source": "suggested_feeds",
                        "position": pos,
                        "item": item,
                        "vantage_id": vantage_id,
                    }
                )
                uri = _extract_feed_uri(item)
                if not uri:
                    continue
                if out_csv is not None:
                    out_csv.write_rows(
                        [{"feed_uri": uri, "position": pos, "captured_at_utc": captured_at_utc, "vantage_id": vantage_id}]
                    )
                creator_did = _deep_get_str(item, ("creator", "did"))
                service_did = item.get("did") if isinstance(item.get("did"), str) else None
                like_count = item.get("likeCount") if isinstance(item.get("likeCount"), int) else None
                provider_domain = provider_domain_from_service_did(service_did)
                state.upsert_feed_catalog(
                    feed_uri=FeedUri(uri),
                    creator_did=creator_did,
                    service_did=service_did,
                    provider_domain=provider_domain,
                    like_count_last=like_count,
                    discovered_from=["suggested_feeds"],
                    seen_at_utc=captured_at_utc,
                )
                total += 1

            state.commit()
            cursor = resp.data.get("cursor") if isinstance(resp.data.get("cursor"), str) else None
            if not cursor:
                break

    logger.info("suggested collected=%s", total)
    return total


async def _collect_suggested_accounts(
    *,
    http: AsyncHttpClient,
    out_jsonl: Path,
    out_csv: CsvPartWriter | None,
    captured_at_utc: str,
    vantage_id: str,
    access_jwt: str | None,
    viewer_did: str | None,
) -> list[str]:
    # Some AppView hosts (notably https://public.api.bsky.app) do not honor viewer context for
    # getSuggestions even when passing a `viewer` param. When authenticated, prefer the same
    # host we used for session creation (configured as pds_host) so auth is honored.
    request_host = http.hosts.pds_host if access_jwt else http.hosts.appview_host
    cursor: str | None = None
    position = 0
    out_dids: list[str] = []
    seen: set[str] = set()

    with JsonlWriter(out_jsonl) as w:
        while True:
            try:
                resp = await http.xrpc_get(
                    endpoint="app.bsky.actor.getSuggestions",
                    host=request_host,
                    method="app.bsky.actor.getSuggestions",
                    params={
                        "limit": 100,
                        **({"cursor": cursor} if cursor else {}),
                        **({"viewer": viewer_did} if viewer_did else {}),
                    },
                    access_jwt=access_jwt,
                    feed_uri=None,
                    timestamp_utc=captured_at_utc,
                )
            except HttpError as err:
                msg = str(err).lower()
                if viewer_did is None and ("must pass viewer" in msg or ("pass" in msg and "viewer" in msg)):
                    raise DiscoverySurfaceSkip("suggested_accounts_requires_viewer") from err
                raise

            actors = resp.data.get("actors")
            if not isinstance(actors, list):
                actors = resp.data.get("suggestions")
            if not isinstance(actors, list):
                actors = resp.data.get("suggested")
            if not isinstance(actors, list):
                logger.warning("suggested accounts response missing actors[]; skipping")
                return out_dids

            for item in actors:
                pos = position
                position += 1
                if not isinstance(item, dict):
                    continue
                w.write_obj(
                    {
                        "captured_at_utc": captured_at_utc,
                        "source": "suggested_accounts",
                        "position": pos,
                        "item": item,
                        "vantage_id": vantage_id,
                    }
                )
                did = item.get("did")
                if not isinstance(did, str) or not did:
                    continue
                if out_csv is not None:
                    out_csv.write_rows(
                        [{"actor_did": did, "position": pos, "captured_at_utc": captured_at_utc, "vantage_id": vantage_id}]
                    )
                if did not in seen:
                    seen.add(did)
                    out_dids.append(did)

            cursor = resp.data.get("cursor") if isinstance(resp.data.get("cursor"), str) else None
            if not cursor:
                break

    logger.info("suggested accounts collected=%s", len(out_dids))
    return out_dids


async def _collect_suggested_follows_by_actor(
    *,
    http: AsyncHttpClient,
    out_jsonl: Path,
    out_csv: CsvPartWriter | None,
    captured_at_utc: str,
    vantage_id: str,
    seed_actor_dids: list[str],
    concurrency: int,
    access_jwt: str | None,
    viewer_did: str | None,
) -> int:
    # Same reasoning as `_collect_suggested_accounts`: these graph suggestion endpoints require
    # auth and are unreliable via public AppView proxies.
    request_host = http.hosts.pds_host if access_jwt else http.hosts.appview_host
    seeds = [d for d in (str(x).strip() for x in seed_actor_dids) if d]
    if not seeds:
        # Still create the JSONL file to keep output layout stable.
        JsonlWriter(out_jsonl).close()
        return 0

    sem = asyncio.Semaphore(max(1, min(concurrency, 64)))
    rows_written = 0

    with JsonlWriter(out_jsonl) as w:
        async def fetch_one(seed_actor_did: str) -> None:
            nonlocal rows_written
            async with sem:
                cursor: str | None = None
                pos = 0
                while True:
                    try:
                        resp = await http.xrpc_get(
                            endpoint="app.bsky.graph.getSuggestedFollowsByActor",
                            host=request_host,
                            method="app.bsky.graph.getSuggestedFollowsByActor",
                            params={
                                "actor": seed_actor_did,
                                "limit": 100,
                                **({"cursor": cursor} if cursor else {}),
                                **({"viewer": viewer_did} if viewer_did else {}),
                            },
                            access_jwt=access_jwt,
                            feed_uri=None,
                            timestamp_utc=captured_at_utc,
                        )
                    except Exception as err:  # noqa: BLE001
                        logger.warning("suggested follows failed seed=%s err=%r", seed_actor_did, err)
                        return

                    is_fallback = resp.data.get("isFallback")
                    is_fallback_out = 1 if is_fallback is True else 0 if is_fallback is False else None

                    suggestions = resp.data.get("suggestions")
                    if not isinstance(suggestions, list):
                        suggestions = resp.data.get("actors")
                    if not isinstance(suggestions, list):
                        suggestions = resp.data.get("users")
                    if not isinstance(suggestions, list):
                        logger.warning("suggested follows response missing suggestions[] seed=%s", seed_actor_did)
                        return

                    for item in suggestions:
                        this_pos = pos
                        pos += 1
                        if not isinstance(item, dict):
                            continue
                        w.write_obj(
                            {
                                "captured_at_utc": captured_at_utc,
                                "source": "suggested_follows_by_actor",
                                "seed_actor_did": seed_actor_did,
                                "position": this_pos,
                                "isFallback": is_fallback,
                                "item": item,
                                "vantage_id": vantage_id,
                            }
                        )
                        did = item.get("did")
                        if not isinstance(did, str) or not did:
                            actor = item.get("actor")
                            if isinstance(actor, dict) and isinstance(actor.get("did"), str):
                                did = str(actor.get("did"))
                            else:
                                continue
                        if out_csv is not None:
                            rows_written_local = out_csv.write_rows(
                                [
                                    {
                                        "seed_actor_did": seed_actor_did,
                                        "suggested_did": did,
                                        "position": this_pos,
                                        "isFallback": is_fallback_out,
                                        "captured_at_utc": captured_at_utc,
                                        "vantage_id": vantage_id,
                                    }
                                ]
                            )
                            rows_written += int(rows_written_local)

                    cursor = resp.data.get("cursor") if isinstance(resp.data.get("cursor"), str) else None
                    if not cursor:
                        break

        async with asyncio.TaskGroup() as tg:
            for did in seeds:
                tg.create_task(fetch_one(did))
    return rows_written


async def _collect_onboarding_starterpacks(
    *,
    http: AsyncHttpClient,
    state: ControlState,
    out_jsonl: Path,
    starterpack_feeds: CsvPartWriter | None,
    starterpack_accounts: CsvPartWriter | None,
    captured_at_utc: str,
    vantage_id: str,
    concurrency: int,
    access_jwt: str | None,
    viewer_did: str | None,
) -> int:
    # Preferred path (Option B): request onboarding skeleton (URIs only), then hydrate via getStarterPacks/getStarterPack.
    skeleton_method = "app.bsky.unspecced.getOnboardingSuggestedStarterPacksSkeleton"
    view_methods = (
        "app.bsky.unspecced.getOnboardingSuggestedStarterPacks",
        "app.bsky.graph.getSuggestedStarterPacks",
    )
    suggest_hosts = [http.hosts.appview_host]
    if access_jwt and http.hosts.pds_host not in suggest_hosts:
        suggest_hosts.append(http.hosts.pds_host)

    pack_infos: list[dict[str, Any]] = []

    with JsonlWriter(out_jsonl) as w:
        resp: AsyncHttpClient.XrpcResponse | None = None
        used_method: str | None = None
        used_host: str | None = None

        skeleton_err: Exception | None = None
        for host in suggest_hosts:
            try:
                resp = await http.xrpc_get(
                    endpoint=skeleton_method,
                    host=host,
                    method=skeleton_method,
                    params={"limit": 25, **({"viewer": viewer_did} if viewer_did else {})},
                    access_jwt=access_jwt,
                    feed_uri=None,
                    timestamp_utc=captured_at_utc,
                )
                used_method = skeleton_method
                used_host = host
                break
            except Exception as err:  # noqa: BLE001
                skeleton_err = err
                resp = None
                continue
        if resp is None and skeleton_err is not None:
            logger.info("starterpacks skeleton unsupported; falling back err=%r", skeleton_err)

        if resp is None:
            last_err: Exception | None = None
            for host in suggest_hosts:
                for method in view_methods:
                    try:
                        resp = await http.xrpc_get(
                            endpoint=method,
                            host=host,
                            method=method,
                            params={"limit": 25, **({"viewer": viewer_did} if viewer_did else {})},
                            access_jwt=access_jwt,
                            feed_uri=None,
                            timestamp_utc=captured_at_utc,
                        )
                        used_method = method
                        used_host = host
                        break
                    except Exception as err:  # noqa: BLE001
                        last_err = err
                        resp = None
                        continue
                if resp is not None:
                    break
            if resp is None:
                if isinstance(last_err, HttpError) and last_err.status_code == 404:
                    raise DiscoverySurfaceSkip("onboarding_starterpacks_not_supported") from last_err
                raise RuntimeError(f"starterpacks suggested endpoint unsupported or failed: {last_err!r}") from last_err

        packs = (
            resp.data.get("starterPacks")
            or resp.data.get("packs")
            or resp.data.get("starterPackUris")
            or resp.data.get("uris")
        )
        if not isinstance(packs, list):
            raise RuntimeError(f"starterpacks response missing starterPacks[] method={used_method}")

        position = 0
        for item in packs:
            pos = position
            position += 1
            pack_uri = _extract_pack_uri(item)
            if not pack_uri:
                continue
            # Normalize to dict for downstream hydration.
            item_obj = dict(item) if isinstance(item, dict) else {"uri": pack_uri}
            w.write_obj(
                {
                    "captured_at_utc": captured_at_utc,
                    "source": "onboarding_suggested_starterpacks",
                    "method": used_method,
                    "host": used_host,
                    "position": pos,
                    "item": item_obj,
                    "vantage_id": vantage_id,
                }
            )
            pack_infos.append(item_obj)

    if not pack_infos:
        return 0

    # Attempt batch hydration via app.bsky.graph.getStarterPacks(uris=[...]).
    packs_by_uri: dict[str, dict[str, Any]] | None = None
    pack_uris = [u for u in (_extract_pack_uri(x) for x in pack_infos) if u]
    if pack_uris:
        try:
            packs_by_uri = await _fetch_starterpacks_by_uri(
                http=http,
                starterpack_uris=pack_uris,
                captured_at_utc=captured_at_utc,
                access_jwt=access_jwt,
                viewer_did=viewer_did,
            )
        except Exception as err:  # noqa: BLE001
            logger.info("getStarterPacks hydration unavailable; falling back to per-pack err=%r", err)
            packs_by_uri = None

    sem = asyncio.Semaphore(max(1, min(concurrency, 64)))

    async def hydrate_one(item: dict[str, Any]) -> None:
        pack_uri = _extract_pack_uri(item)
        if not pack_uri:
            return
        async with sem:
            try:
                if packs_by_uri and pack_uri in packs_by_uri:
                    pack_obj = packs_by_uri[pack_uri]
                    # getStarterPacks may return lightweight views without feeds/list populated; fall back to per-pack hydrate.
                    has_list = _extract_list_uri(pack_obj) is not None
                    has_feeds = bool(_extract_feed_uris(pack_obj))
                    if has_list and has_feeds:
                        await _hydrate_starterpack_obj(
                            http=http,
                            state=state,
                            starterpack_feeds=starterpack_feeds,
                            starterpack_accounts=starterpack_accounts,
                            pack_item=item,
                            pack_uri=pack_uri,
                            pack_obj=pack_obj,
                            captured_at_utc=captured_at_utc,
                            vantage_id=vantage_id,
                            source="onboarding_suggested_starterpacks",
                            access_jwt=access_jwt,
                        )
                    else:
                        await _hydrate_starterpack(
                            http=http,
                            state=state,
                            starterpack_feeds=starterpack_feeds,
                            starterpack_accounts=starterpack_accounts,
                            pack_item=item,
                            pack_uri=pack_uri,
                            captured_at_utc=captured_at_utc,
                            vantage_id=vantage_id,
                            source="onboarding_suggested_starterpacks",
                            access_jwt=access_jwt,
                        )
                else:
                    await _hydrate_starterpack(
                        http=http,
                        state=state,
                        starterpack_feeds=starterpack_feeds,
                        starterpack_accounts=starterpack_accounts,
                        pack_item=item,
                        pack_uri=pack_uri,
                        captured_at_utc=captured_at_utc,
                        vantage_id=vantage_id,
                        source="onboarding_suggested_starterpacks",
                        access_jwt=access_jwt,
                    )
            except Exception as err:  # noqa: BLE001
                logger.warning("starterpack hydrate failed uri=%s err=%r", pack_uri, err)

    async with asyncio.TaskGroup() as tg:
        for item in pack_infos:
            tg.create_task(hydrate_one(item))

    state.commit()
    return len(pack_infos)


async def _fetch_starterpacks_by_uri(
    *,
    http: AsyncHttpClient,
    starterpack_uris: list[str],
    captured_at_utc: str,
    access_jwt: str | None,
    viewer_did: str | None,
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    # Conservative batch size; the server may enforce small caps.
    batch_size = 25
    for i in range(0, len(starterpack_uris), batch_size):
        batch = [u for u in starterpack_uris[i : i + batch_size] if u]
        if not batch:
            continue
        resp = await http.xrpc_get(
            endpoint="app.bsky.graph.getStarterPacks",
            host=http.hosts.appview_host,
            method="app.bsky.graph.getStarterPacks",
            params={"uris": batch, **({"viewer": viewer_did} if viewer_did else {})},
            access_jwt=access_jwt,
            feed_uri=None,
            timestamp_utc=captured_at_utc,
        )
        packs = resp.data.get("starterPacks")
        if not isinstance(packs, list):
            packs = resp.data.get("packs")
        if not isinstance(packs, list):
            raise RuntimeError("getStarterPacks missing starterPacks[]")
        for item in packs:
            if not isinstance(item, dict):
                continue
            pack_obj = item.get("starterPack") if isinstance(item.get("starterPack"), dict) else item
            if not isinstance(pack_obj, dict):
                continue
            uri = _extract_pack_uri(pack_obj)
            if not uri:
                continue
            out[uri] = pack_obj
    return out


async def _hydrate_starterpack(
    *,
    http: AsyncHttpClient,
    state: ControlState,
    starterpack_feeds: CsvPartWriter | None,
    starterpack_accounts: CsvPartWriter | None,
    pack_item: dict[str, Any],
    pack_uri: str,
    captured_at_utc: str,
    vantage_id: str,
    source: str,
    access_jwt: str | None,
) -> None:
    resp = await http.xrpc_get(
        endpoint="app.bsky.graph.getStarterPack",
        host=http.hosts.appview_host,
        method="app.bsky.graph.getStarterPack",
        params={"starterPack": pack_uri},
        access_jwt=access_jwt,
        feed_uri=None,
        timestamp_utc=captured_at_utc,
    )
    pack_obj = resp.data.get("starterPack") if isinstance(resp.data.get("starterPack"), dict) else resp.data
    if not isinstance(pack_obj, dict):
        return
    await _hydrate_starterpack_obj(
        http=http,
        state=state,
        starterpack_feeds=starterpack_feeds,
        starterpack_accounts=starterpack_accounts,
        pack_item=pack_item,
        pack_uri=pack_uri,
        pack_obj=pack_obj,
        captured_at_utc=captured_at_utc,
        vantage_id=vantage_id,
        source=source,
        access_jwt=access_jwt,
    )


async def _hydrate_starterpack_obj(
    *,
    http: AsyncHttpClient,
    state: ControlState,
    starterpack_feeds: CsvPartWriter | None,
    starterpack_accounts: CsvPartWriter | None,
    pack_item: dict[str, Any],
    pack_uri: str,
    pack_obj: dict[str, Any],
    captured_at_utc: str,
    vantage_id: str,
    source: str,
    access_jwt: str | None,
) -> None:
    joined_week = pack_item.get("joinedWeekCount") if isinstance(pack_item.get("joinedWeekCount"), int) else None
    joined_all = pack_item.get("joinedAllTimeCount") if isinstance(pack_item.get("joinedAllTimeCount"), int) else None
    pack_creator = _deep_get_str(pack_obj, ("creator", "did")) or _deep_get_str(pack_item, ("creator", "did"))

    # Keep all feed URIs returned by upstream; downstream analysis can decide how many to use.
    feed_uris = _extract_feed_uris(pack_obj)
    if not feed_uris:
        logger.info("starterpack has no embedded feeds pack_uri=%s", pack_uri)
    for idx, feed_uri in enumerate(feed_uris):
        state.upsert_feed_catalog(
            feed_uri=FeedUri(feed_uri),
            creator_did=None,
            service_did=None,
            provider_domain=None,
            like_count_last=None,
            discovered_from=[source],
            seen_at_utc=captured_at_utc,
        )
        if starterpack_feeds is not None:
            starterpack_feeds.write_rows(
                [
                    {
                        "pack_uri": pack_uri,
                        "pack_creator": pack_creator,
                        "joinedWeekCount": joined_week,
                        "joinedAllTimeCount": joined_all,
                        "feed_uri": feed_uri,
                        "slot_index": idx,
                        "captured_at_utc": captured_at_utc,
                        "vantage_id": vantage_id,
                        "source": source,
                    }
                ]
            )

    if feed_uris:
        # Avoid holding a write transaction while awaiting list pagination requests.
        state.commit()

    list_uri = _extract_list_uri(pack_obj)
    if list_uri and starterpack_accounts is not None:
        cursor: str | None = None
        pos = 0
        while True:
            resp2 = await http.xrpc_get(
                endpoint="app.bsky.graph.getList",
                host=http.hosts.appview_host,
                method="app.bsky.graph.getList",
                params={"list": list_uri, "limit": 100, **({"cursor": cursor} if cursor else {})},
                access_jwt=access_jwt,
                feed_uri=None,
                timestamp_utc=captured_at_utc,
            )
            items = resp2.data.get("items")
            if not isinstance(items, list) or not items:
                break

            rows: list[dict[str, Any]] = []
            for it in items:
                if not isinstance(it, dict):
                    continue
                subject_did = _extract_subject_did(it)
                if not subject_did:
                    continue
                rows.append(
                    {
                        "pack_uri": pack_uri,
                        "list_uri": list_uri,
                        "subject_did": subject_did,
                        "position": pos,
                        "captured_at_utc": captured_at_utc,
                        "vantage_id": vantage_id,
                        "source": source,
                    }
                )
                pos += 1
            starterpack_accounts.write_rows(rows)

            cursor = resp2.data.get("cursor") if isinstance(resp2.data.get("cursor"), str) else None
            if not cursor:
                break


def _deep_get_str(obj: Any, path: Iterable[str]) -> str | None:
    cur: Any = obj
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    if isinstance(cur, str) and cur:
        return cur
    return None


def _extract_feed_uri(item: dict[str, Any]) -> str | None:
    uri = item.get("uri")
    if isinstance(uri, str) and uri:
        return uri
    feed = item.get("feed")
    if isinstance(feed, dict):
        uri2 = feed.get("uri")
        if isinstance(uri2, str) and uri2:
            return uri2
    return None


def _extract_pack_uri(item: Any) -> str | None:
    if isinstance(item, str) and item:
        return item
    if not isinstance(item, dict):
        return None
    uri = item.get("uri")
    if isinstance(uri, str) and uri:
        return uri
    starter_pack = item.get("starterPack")
    if isinstance(starter_pack, str) and starter_pack:
        return starter_pack
    if isinstance(starter_pack, dict):
        uri2 = starter_pack.get("uri")
        if isinstance(uri2, str) and uri2:
            return uri2
    return None


def _extract_list_uri(obj: Any) -> str | None:
    found: list[str] = []

    def visit(x: Any) -> None:
        if found:
            return
        if isinstance(x, str):
            if "/app.bsky.graph.list/" in x:
                found.append(x)
            return
        if isinstance(x, dict):
            uri = x.get("uri")
            if isinstance(uri, str) and "/app.bsky.graph.list/" in uri:
                found.append(uri)
                return
            for v in x.values():
                visit(v)
        elif isinstance(x, list):
            for v in x:
                visit(v)

    visit(obj)
    return found[0] if found else None


def _extract_subject_did(item: dict[str, Any]) -> str | None:
    subject = item.get("subject")
    if isinstance(subject, dict):
        did = subject.get("did")
        if isinstance(did, str) and did:
            return did
    if isinstance(item.get("subject_did"), str) and item.get("subject_did"):
        return str(item.get("subject_did"))
    return None


def _extract_feed_uris(obj: Any) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()

    def visit(x: Any) -> None:
        if isinstance(x, str):
            _maybe_add(x)
            return
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
        if uri in seen:
            return
        seen.add(uri)
        out.append(uri)

    visit(obj)
    return out


def _export_feed_catalog_csv(*, state: ControlState, out_csv: Path) -> None:
    tmp = out_csv.with_name(out_csv.name + ".tmp")
    ensure_dir(out_csv.parent)
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        import csv

        w = csv.writer(f)
        w.writerow(
            [
                "feed_uri",
                "creator_did",
                "service_did",
                "provider_domain",
                "like_count_last",
                "discovered_from_json",
                "first_seen_utc",
                "last_seen_utc",
                "last_hydrated_utc",
            ]
        )
        for row in state.iter_feed_catalog():
            w.writerow(
                [
                    row["feed_uri"],
                    row["creator_did"],
                    row["service_did"],
                    row["provider_domain"],
                    row["like_count_last"],
                    row["discovered_from"],
                    row["first_seen_utc"],
                    row["last_seen_utc"],
                    row["last_hydrated_utc"],
                ]
            )
        f.flush()

    tmp.replace(out_csv)


async def _hydrate_feed_catalog_like_counts(
    *,
    http: AsyncHttpClient,
    state: ControlState,
    captured_at_utc: str,
    access_jwt: str | None,
    concurrency: int,
) -> dict[str, Any]:
    """
    Populate feed_catalog.like_count_last for *all* known feed generators by calling
    app.bsky.feed.getFeedGenerators in batches.

    This fixes the "popular_by_likecount" sampling bias where likeCount is only known
    for the small discovery-returned subset.
    """

    sem = asyncio.Semaphore(max(1, min(int(concurrency), 32)))
    page_size = 5_000
    batch_size = 60  # adaptive splitting handles 414s

    api_calls = 0
    http_414_splits = 0
    batches_failed = 0
    feeds_total = 0
    feeds_updated = 0
    feeds_missing_in_response = 0

    async def fetch_like_map(feed_uris: list[str]) -> dict[str, int | None]:
        nonlocal api_calls, http_414_splits
        if not feed_uris:
            return {}
        try:
            api_calls += 1
            resp = await http.xrpc_get(
                endpoint="app.bsky.feed.getFeedGenerators",
                host=http.hosts.appview_host,
                method="app.bsky.feed.getFeedGenerators",
                params={"feeds": list(feed_uris)},
                access_jwt=access_jwt,
                feed_uri=None,
                timestamp_utc=captured_at_utc,
            )
        except HttpError as err:
            # Very long query strings can hit HTTP 414. Split and retry.
            if err.status_code == 414 and len(feed_uris) > 1:
                http_414_splits += 1
                mid = max(1, len(feed_uris) // 2)
                left = await fetch_like_map(feed_uris[:mid])
                right = await fetch_like_map(feed_uris[mid:])
                merged: dict[str, int | None] = {}
                merged.update(left)
                merged.update(right)
                return merged
            raise

        items = resp.data.get("feeds")
        out: dict[str, int | None] = {}
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                uri = item.get("uri")
                if not isinstance(uri, str) or not uri:
                    continue
                like = item.get("likeCount")
                if isinstance(like, int):
                    out[uri] = int(like)
                else:
                    out[uri] = None
        return out

    async def hydrate_batch(batch: list[str]) -> list[tuple[str, int | None]] | None:
        async with sem:
            try:
                like_by_uri = await fetch_like_map(batch)
            except Exception as err:  # noqa: BLE001
                logger.warning("likeCount hydrate failed batch=%s err=%r", len(batch), err)
                return None

        updates: list[tuple[str, int | None]] = []
        missing = 0
        for uri in batch:
            if uri not in like_by_uri:
                missing += 1
            updates.append((uri, like_by_uri.get(uri)))
        nonlocal feeds_missing_in_response
        feeds_missing_in_response += missing
        return updates

    after: str | None = None
    while True:
        page = state.list_feed_catalog_uris(limit=page_size, after_feed_uri=after)
        if not page:
            break
        after = page[-1]
        feeds_total += len(page)

        tasks: list[asyncio.Task[list[tuple[str, int | None]] | None]] = []
        for i in range(0, len(page), batch_size):
            batch = page[i : i + batch_size]
            tasks.append(asyncio.create_task(hydrate_batch(batch)))

        page_updates: list[tuple[str, int | None]] = []
        for t in asyncio.as_completed(tasks):
            updates = await t
            if updates is None:
                batches_failed += 1
                continue
            page_updates.extend(updates)

        if page_updates:
            state.update_feed_catalog_like_counts(rows=page_updates, hydrated_at_utc=captured_at_utc)
            feeds_updated += len(page_updates)

    return {
        "feeds_total": int(feeds_total),
        "feeds_updated": int(feeds_updated),
        "feeds_missing_in_response": int(feeds_missing_in_response),
        "batches_failed": int(batches_failed),
        "api_calls": int(api_calls),
        "http_414_splits": int(http_414_splits),
        "batch_size": int(batch_size),
        "page_size": int(page_size),
    }
