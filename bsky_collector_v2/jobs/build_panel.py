from __future__ import annotations

import asyncio
import csv
import logging
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from bsky_collector_v2.env import AuthEnv, load_auth_env
from bsky_collector_v2.fs_utils import ensure_dir
from bsky_collector_v2.http_client import AsyncHttpClient, HttpRetryConfig, HttpError, XrpcHosts
from bsky_collector_v2.layout import Layout
from bsky_collector_v2.session import get_or_create_session, is_auth_required_error, session_cache_path
from bsky_collector_v2.time_utils import format_utc, now_utc, utc_date_str
from bsky_collector_v2.types import FeedUri, RunId

logger = logging.getLogger("bsky_collector_v2.job.build_panel")


@dataclass(frozen=True)
class PanelBuildConfig:
    k1_popular: int = 700
    k2_onboarding: int = 300
    k3_suggested: int = 300
    k4_longtail: int = 200


@dataclass(frozen=True)
class FeedSignals:
    feed_uri: str
    like_count: int
    provider_domain: str | None
    onboarding_inclusion_count: int
    onboarding_join_weighted: int
    suggested_rank: int | None


def _latest_metadata_day(layout: Layout) -> str:
    root = layout.metadata_root
    if not root.exists():
        raise FileNotFoundError(f"missing metadata root: {root}")
    candidates: list[str] = []
    for child in root.iterdir():
        if child.is_dir() and len(child.name) == 10:
            candidates.append(child.name)
    if not candidates:
        raise FileNotFoundError(f"no metadata day directories under: {root}")
    return sorted(candidates)[-1]


def _read_feed_catalog(path: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    with open(path, "r", encoding="utf-8", newline="") as f:
        r = csv.DictReader(f)
        for row in r:
            uri = (row.get("feed_uri") or "").strip()
            if not uri:
                continue
            out[uri] = row
    return out


def _read_starterpack_stats(path: Path) -> tuple[dict[str, int], dict[str, int]]:
    inclusion: dict[str, set[str]] = {}
    weighted: dict[str, int] = {}
    if not path.exists():
        return {}, {}
    with open(path, "r", encoding="utf-8", newline="") as f:
        r = csv.DictReader(f)
        for row in r:
            feed_uri = (row.get("feed_uri") or "").strip()
            pack_uri = (row.get("pack_uri") or "").strip()
            if not feed_uri or not pack_uri:
                continue
            inclusion.setdefault(feed_uri, set()).add(pack_uri)
            joined_all = row.get("joinedAllTimeCount")
            try:
                joined_all_i = int(joined_all) if joined_all not in (None, "") else 0
            except ValueError:
                joined_all_i = 0
            weighted[feed_uri] = int(weighted.get(feed_uri, 0)) + joined_all_i
    inclusion_count = {k: len(v) for k, v in inclusion.items()}
    return inclusion_count, weighted


def _read_suggested_ranks(path: Path) -> dict[str, int]:
    if not path.exists():
        return {}
    # suggested_feeds.csv is append-only; prefer the latest captured_at_utc snapshot.
    latest_ts: str | None = None
    latest: dict[str, int] = {}
    with open(path, "r", encoding="utf-8", newline="") as f:
        r = csv.DictReader(f)
        for row in r:
            ts = (row.get("captured_at_utc") or "").strip()
            uri = (row.get("feed_uri") or "").strip()
            pos_s = (row.get("position") or "").strip()
            if not ts or not uri:
                continue
            try:
                pos = int(pos_s)
            except ValueError:
                continue
            if latest_ts is None or ts > latest_ts:
                latest_ts = ts
                latest = {}
            if ts == latest_ts and uri not in latest:
                latest[uri] = pos
    return latest


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except Exception:  # noqa: BLE001
        return 0


def _build_signals(
    *,
    feed_catalog: dict[str, dict[str, Any]],
    inclusion_count: dict[str, int],
    onboarding_weighted: dict[str, int],
    suggested_ranks: dict[str, int],
) -> list[FeedSignals]:
    out: list[FeedSignals] = []
    for feed_uri, row in feed_catalog.items():
        like_count = _to_int(row.get("like_count_last"))
        provider_domain = (row.get("provider_domain") or "").strip() or None
        out.append(
            FeedSignals(
                feed_uri=feed_uri,
                like_count=like_count,
                provider_domain=provider_domain,
                onboarding_inclusion_count=int(inclusion_count.get(feed_uri, 0)),
                onboarding_join_weighted=int(onboarding_weighted.get(feed_uri, 0)),
                suggested_rank=suggested_ranks.get(feed_uri),
            )
        )
    return out


def _select_panel(signals: list[FeedSignals], cfg: PanelBuildConfig, *, seed: int) -> list[tuple[str, str]]:
    """Return list of (feed_uri, bucket)."""
    by_uri = {s.feed_uri: s for s in signals}
    remaining: set[str] = set(by_uri.keys())
    selected: list[tuple[str, str]] = []
    target_total = cfg.k1_popular + cfg.k2_onboarding + cfg.k3_suggested + cfg.k4_longtail

    def take(uris: Iterable[str], bucket: str, k: int) -> int:
        taken = 0
        for uri in uris:
            if uri not in remaining:
                continue
            selected.append((uri, bucket))
            remaining.remove(uri)
            taken += 1
            if taken >= k:
                break
            if len(selected) >= target_total:
                break
        return taken

    popular_sorted = sorted(remaining, key=lambda u: (-by_uri[u].like_count, u))
    take(popular_sorted, "popular_by_likecount", cfg.k1_popular)

    onboarding_candidates = [
        u
        for u in remaining
        if by_uri[u].onboarding_inclusion_count > 0 or by_uri[u].onboarding_join_weighted > 0
    ]
    onboarding_sorted = sorted(
        onboarding_candidates,
        key=lambda u: (-by_uri[u].onboarding_join_weighted, -by_uri[u].onboarding_inclusion_count, u),
    )
    take(onboarding_sorted, "onboarding_surfaced", cfg.k2_onboarding)

    suggested_sorted = sorted(
        (u for u in remaining if by_uri[u].suggested_rank is not None),
        key=lambda u: (int(by_uri[u].suggested_rank or 0), u),
    )
    take(suggested_sorted, "suggested", cfg.k3_suggested)

    rng = random.Random(seed)
    longtail_candidates = sorted(remaining)
    rng.shuffle(longtail_candidates)
    # Longtail bucket is the "fill": guarantee target_total if catalog permits.
    needed = max(0, target_total - len(selected))
    take(longtail_candidates, "longtail_random", max(cfg.k4_longtail, needed))

    return selected


async def run_build_panel(
    *,
    layout: Layout,
    run_id: RunId,
    hosts: XrpcHosts,
    env_path: Path | None,
    rps: float,
    concurrency: int,
    dry_run: bool,
    cfg: PanelBuildConfig | None = None,
) -> None:
    cfg = cfg or PanelBuildConfig()
    date_str = utc_date_str(now_utc())
    metadata_day = _latest_metadata_day(layout)

    feed_catalog = _read_feed_catalog(layout.feed_catalog_csv(metadata_day))
    inclusion_count, onboarding_weighted = _read_starterpack_stats(layout.starterpack_feeds_csv(metadata_day))
    suggested_ranks = _read_suggested_ranks(layout.suggested_feeds_csv(metadata_day))

    signals = _build_signals(
        feed_catalog=feed_catalog,
        inclusion_count=inclusion_count,
        onboarding_weighted=onboarding_weighted,
        suggested_ranks=suggested_ranks,
    )
    panel_target = cfg.k1_popular + cfg.k2_onboarding + cfg.k3_suggested + cfg.k4_longtail
    panel_version_id = date_str
    seed = int.from_bytes(panel_version_id.encode("utf-8"), "little") & 0xFFFFFFFF
    selected = _select_panel(signals, cfg, seed=seed)

    if len(selected) < panel_target:
        logger.warning(
            "panel deficit selected=%s target=%s (catalog=%s)",
            len(selected),
            panel_target,
            len(signals),
        )

    built_at_utc = format_utc(now_utc())

    if dry_run:
        logger.info("dry_run=true: would write panel rows=%s", len(selected))
        return

    ensure_dir(layout.panel_versions_dir)
    ensure_dir(layout.panel_root)

    http = AsyncHttpClient(
        hosts=hosts,
        rps=rps,
        retry=HttpRetryConfig(max_retries=1),
        timeout_s=20.0,
        http_stats=None,
        progress=None,
    )

    auth_env: AuthEnv | None = None
    auth_access_jwt: str | None = None
    auth_host: str | None = None
    if env_path is not None and env_path.exists():
        try:
            auth_env = load_auth_env(env_path)
        except Exception as err:  # noqa: BLE001
            logger.warning("auth env invalid; probe unauth-only err=%r", err)
            auth_env = None

    try:
        if auth_env is not None:
            try:
                tokens = await get_or_create_session(
                    http,
                    env=auth_env,
                    cache_path=session_cache_path(control_root=layout.control_root, env=auth_env),
                )
                auth_access_jwt = tokens.access_jwt
                auth_host = auth_env.pds_host
            except Exception as err:  # noqa: BLE001
                logger.warning("auth session failed; probe unauth-only err=%r", err)
                auth_access_jwt = None
                auth_host = None

        unauth_skip = await _probe_auth_required(
            http=http,
            feed_uris=[FeedUri(uri) for uri, _bucket in selected],
            concurrency=concurrency,
            captured_at_utc=built_at_utc,
            auth_access_jwt=auth_access_jwt,
            auth_host=auth_host,
        )

        version_path = layout.panel_version_csv(panel_version_id)
        tmp_version = version_path.with_name(version_path.name + ".tmp")
        _write_panel_csv(
            out_path=tmp_version,
            selected=selected,
            unauth_skip=unauth_skip,
            built_at_utc=built_at_utc,
            panel_version_id=panel_version_id,
        )
        tmp_version.replace(version_path)

        tmp_active = layout.panel_active_csv.with_name(layout.panel_active_csv.name + ".tmp")
        _write_panel_csv(
            out_path=tmp_active,
            selected=selected,
            unauth_skip=unauth_skip,
            built_at_utc=built_at_utc,
            panel_version_id=panel_version_id,
        )
        tmp_active.replace(layout.panel_active_csv)
        logger.info("panel written active=%s version=%s", str(layout.panel_active_csv), str(version_path))
        try:
            from bsky_collector_v2.effective_csv import refresh_key_views, sync_panel_day

            sync_panel_day(layout, date_yyyy_mm_dd=panel_version_id)
            refresh_key_views(layout)
        except Exception as err:  # noqa: BLE001
            logger.warning("effective csv sync failed job=build-panel date=%s err=%r", panel_version_id, err)
    finally:
        await http.aclose()


async def _probe_auth_required(
    *,
    http: AsyncHttpClient,
    feed_uris: list[FeedUri],
    concurrency: int,
    captured_at_utc: str,
    auth_access_jwt: str | None,
    auth_host: str | None,
) -> dict[str, int]:
    sem = asyncio.Semaphore(max(1, min(concurrency, 64)))
    out: dict[str, int] = {}
    # Probe with >1 item to catch feeds that allow "limit=1" unauth but fail at normal snapshot limits.
    probe_limit = 2

    async def probe_one(feed_uri: FeedUri) -> None:
        async with sem:
            try:
                await http.xrpc_get(
                    endpoint="app.bsky.feed.getFeed.probe",
                    host=http.hosts.appview_host,
                    method="app.bsky.feed.getFeed",
                    params={"feed": str(feed_uri), "limit": probe_limit},
                    access_jwt=None,
                    feed_uri=str(feed_uri),
                    timestamp_utc=captured_at_utc,
                )
                out[str(feed_uri)] = 0
            except HttpError as err:
                if is_auth_required_error(err):
                    out[str(feed_uri)] = 1
                    return

                # Some feeds error on public AppView for non-auth reasons (e.g., personalized feeds).
                # If the same feed succeeds under auth on the user's PDS/AppView, mark unauth_skip=1.
                if auth_access_jwt and auth_host:
                    try:
                        await http.xrpc_get(
                            endpoint="app.bsky.feed.getFeed.probe.auth",
                            host=auth_host,
                            method="app.bsky.feed.getFeed",
                            params={"feed": str(feed_uri), "limit": probe_limit},
                            access_jwt=auth_access_jwt,
                            feed_uri=str(feed_uri),
                            timestamp_utc=captured_at_utc,
                        )
                        out[str(feed_uri)] = 1
                        return
                    except HttpError:
                        pass

                out[str(feed_uri)] = 0

    async with asyncio.TaskGroup() as tg:
        for uri in feed_uris:
            tg.create_task(probe_one(uri))

    return out


def _write_panel_csv(
    *,
    out_path: Path,
    selected: list[tuple[str, str]],
    unauth_skip: dict[str, int],
    built_at_utc: str,
    panel_version_id: str,
) -> None:
    ensure_dir(out_path.parent)
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["feed_uri", "bucket", "unauth_skip", "built_at_utc", "panel_version_id"])
        for feed_uri, bucket in selected:
            w.writerow([feed_uri, bucket, int(unauth_skip.get(feed_uri, 0)), built_at_utc, panel_version_id])
        f.flush()
