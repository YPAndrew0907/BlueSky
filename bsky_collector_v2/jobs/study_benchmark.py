from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass
from pathlib import Path

from bsky_collector_v2.env import AuthEnv, load_auth_env
from bsky_collector_v2.http_client import AsyncHttpClient, HttpError, HttpRetryConfig, XrpcHosts
from bsky_collector_v2.layout import Layout
from bsky_collector_v2.session import get_or_create_session, session_cache_path
from bsky_collector_v2.study import (
    StudyBenchmarkResult,
    deterministic_seed,
    effective_request_limit,
    expected_snapshot_requests_for_panel,
    file_sha256,
    panel_version_id_from_rows,
    read_panel_rows,
    save_benchmark_summary,
)
from bsky_collector_v2.time_utils import format_utc, now_utc

logger = logging.getLogger("bsky_collector_v2.job.study_benchmark")


@dataclass(frozen=True)
class StudyBenchmarkConfig:
    panel_path: Path
    sample_size: int
    viewer_modes: tuple[str, ...]
    posts_per_feed: int
    concurrency: int
    rps: float
    safety_margin: float
    window_minutes: int
    accept_language: str | None = None
    accept_labelers: str | None = None
    include_author_labels: bool = False
    vantage_id_unauth: str = "unauth"
    vantage_id_auth: str = "auth"


BenchmarkConfig = StudyBenchmarkConfig


@dataclass(frozen=True)
class _BenchmarkTask:
    feed_uri: str
    viewer_mode: str
    host: str
    access_jwt: str | None


def _enabled_viewer_modes(requested: tuple[str, ...], *, auth_env: AuthEnv | None) -> tuple[str, ...]:
    enabled: list[str] = []
    for mode in requested:
        if mode == "unauth" and "unauth" not in enabled:
            enabled.append("unauth")
        if mode == "auth":
            if auth_env is None:
                raise RuntimeError("auth viewer mode requested for benchmark but auth env is unavailable")
            if "auth" not in enabled:
                enabled.append("auth")
    if not enabled:
        raise RuntimeError("benchmark has no enabled viewer modes")
    return tuple(enabled)


def _sample_panel_rows(rows: list[dict[str, str]], *, sample_size: int, panel_hash: str) -> list[dict[str, str]]:
    if sample_size <= 0 or sample_size >= len(rows):
        return list(rows)
    seed = deterministic_seed(panel_hash, "study-benchmark-sample", sample_size)
    decorated: list[tuple[int, str, dict[str, str]]] = []
    for row in rows:
        feed_uri = str(row.get("feed_uri") or "").strip()
        decorated.append((deterministic_seed(seed, feed_uri), feed_uri, row))
    decorated.sort(key=lambda item: (item[0], item[1]))
    return [row for _sort_key, _feed_uri, row in decorated[:sample_size]]


async def run_study_benchmark(
    *,
    layout: Layout,
    hosts: XrpcHosts,
    env_path: Path | None,
    cfg: StudyBenchmarkConfig,
    dry_run: bool = False,
) -> StudyBenchmarkResult:
    panel_rows = read_panel_rows(cfg.panel_path)
    if not panel_rows:
        raise RuntimeError(f"benchmark panel is empty: {cfg.panel_path}")

    panel_hash = file_sha256(cfg.panel_path)
    panel_version_id = panel_version_id_from_rows(panel_rows)

    auth_env: AuthEnv | None = None
    if "auth" in cfg.viewer_modes:
        if env_path is None or not env_path.exists():
            raise RuntimeError("auth viewer mode requested for benchmark but --env-path is missing")
        auth_env = load_auth_env(env_path)
    enabled_modes = _enabled_viewer_modes(cfg.viewer_modes, auth_env=auth_env)

    benchmark_id = f"bench_{format_utc(now_utc()).replace(':', '').replace('-', '')}"
    benchmarked_at_utc = format_utc(now_utc())

    if dry_run:
        result = StudyBenchmarkResult(
            benchmark_id=benchmark_id,
            benchmarked_at_utc=benchmarked_at_utc,
            panel_path=str(cfg.panel_path),
            panel_hash=panel_hash,
            panel_version_id=panel_version_id,
            panel_row_count=len(panel_rows),
            viewer_modes=enabled_modes,
            posts_per_feed=cfg.posts_per_feed,
            concurrency=cfg.concurrency,
            rps=cfg.rps,
            sample_size=min(cfg.sample_size, len(panel_rows)),
            measured_request_count=0,
            measured_success_count=0,
            measured_failure_count=0,
            measured_elapsed_s=0.0,
            throughput_rps=0.0,
            safety_margin=cfg.safety_margin,
            window_minutes=cfg.window_minutes,
            safe_window_budget_s=float(cfg.window_minutes * 60) * float(cfg.safety_margin),
            estimated_full_sweep_requests=expected_snapshot_requests_for_panel(
                panel_rows=panel_rows,
                viewer_modes=enabled_modes,
                posts_per_feed=cfg.posts_per_feed,
                include_auth_session_setup=("auth" in enabled_modes),
            ),
            estimated_full_sweep_duration_s=float("inf"),
            safe_max_panel_size=0,
            required_shard_count=max(1, len(panel_rows)),
            full_panel_feasible=False,
            dual_viewer_feasible=False,
            full_depth_feasible=False,
        )
        save_benchmark_summary(layout.benchmark_result_json(result.benchmark_id), result)
        return result

    sample_rows = _sample_panel_rows(panel_rows, sample_size=cfg.sample_size, panel_hash=panel_hash)

    tasks: list[_BenchmarkTask] = []
    for row in sample_rows:
        feed_uri = str(row.get("feed_uri") or "").strip()
        if not feed_uri:
            continue
        unauth_skip = str(row.get("unauth_skip") or "0").strip() == "1"
        if "unauth" in enabled_modes and not unauth_skip:
            tasks.append(
                _BenchmarkTask(
                    feed_uri=feed_uri,
                    viewer_mode="unauth",
                    host=hosts.appview_host,
                    access_jwt=None,
                )
            )
        if "auth" in enabled_modes:
            tasks.append(
                _BenchmarkTask(
                    feed_uri=feed_uri,
                    viewer_mode="auth",
                    host=(auth_env.pds_host if auth_env is not None else hosts.pds_host),
                    access_jwt=None,
                )
            )

    http = AsyncHttpClient(
        hosts=hosts,
        rps=cfg.rps,
        retry=HttpRetryConfig(max_retries=1),
        timeout_s=20.0,
        http_stats=None,
        progress=None,
        accept_language=cfg.accept_language,
        accept_labelers=cfg.accept_labelers,
    )
    auth_access_jwt: str | None = None
    try:
        if auth_env is not None:
            tokens = await get_or_create_session(
                http,
                env=auth_env,
                cache_path=session_cache_path(control_root=layout.control_root, env=auth_env),
            )
            auth_access_jwt = tokens.access_jwt
            tasks = [
                _BenchmarkTask(
                    feed_uri=task.feed_uri,
                    viewer_mode=task.viewer_mode,
                    host=task.host,
                    access_jwt=(auth_access_jwt if task.viewer_mode == "auth" else None),
                )
                for task in tasks
            ]

        sem = asyncio.Semaphore(max(1, min(cfg.concurrency, len(tasks) or 1)))
        measured_request_count = 0
        measured_success_count = 0
        measured_failure_count = 0
        started = asyncio.get_running_loop().time()

        async def run_one(task: _BenchmarkTask) -> None:
            nonlocal measured_request_count, measured_success_count, measured_failure_count
            async with sem:
                try:
                    await http.xrpc_get(
                        endpoint="app.bsky.feed.getFeed",
                        host=task.host,
                        method="app.bsky.feed.getFeed",
                        params={
                            "feed": task.feed_uri,
                            "limit": effective_request_limit(
                                feed_uri=task.feed_uri,
                                posts_per_feed=cfg.posts_per_feed,
                            ),
                        },
                        access_jwt=task.access_jwt,
                        feed_uri=task.feed_uri,
                        timestamp_utc=format_utc(now_utc()),
                    )
                    measured_request_count += 1
                    measured_success_count += 1
                except HttpError as err:
                    logger.warning(
                        "study benchmark request failed feed=%s viewer_mode=%s err=%r",
                        task.feed_uri,
                        task.viewer_mode,
                        err,
                    )
                    measured_request_count += 1
                    measured_failure_count += 1

        async with asyncio.TaskGroup() as tg:
            for task in tasks:
                tg.create_task(run_one(task))

        measured_elapsed_s = max(0.001, asyncio.get_running_loop().time() - started)
    finally:
        await http.aclose()

    throughput_rps = measured_success_count / measured_elapsed_s if measured_elapsed_s > 0 else 0.0
    safe_window_budget_s = float(cfg.window_minutes * 60) * float(cfg.safety_margin)
    estimated_full_sweep_requests = expected_snapshot_requests_for_panel(
        panel_rows=panel_rows,
        viewer_modes=enabled_modes,
        posts_per_feed=cfg.posts_per_feed,
        include_auth_session_setup=("auth" in enabled_modes),
    )
    estimated_full_sweep_duration_s = (
        round(estimated_full_sweep_requests / throughput_rps, 3)
        if throughput_rps > 0
        else float("inf")
    )

    auth_setup_requests = 2 if "auth" in enabled_modes else 0
    request_units_without_bootstrap = expected_snapshot_requests_for_panel(
        panel_rows=panel_rows,
        viewer_modes=enabled_modes,
        posts_per_feed=cfg.posts_per_feed,
        include_auth_session_setup=False,
    )
    avg_requests_per_row = max(0.000001, request_units_without_bootstrap / float(max(1, len(panel_rows))))
    safe_request_budget = max(0.0, (safe_window_budget_s * throughput_rps) - auth_setup_requests)
    safe_max_panel_size = int(max(0, math.floor(safe_request_budget / avg_requests_per_row)))
    required_shard_count = max(
        1,
        int(math.ceil(estimated_full_sweep_duration_s / max(safe_window_budget_s, 1.0))),
    )

    dual_viewer_requests = expected_snapshot_requests_for_panel(
        panel_rows=panel_rows,
        viewer_modes=("unauth", "auth"),
        posts_per_feed=cfg.posts_per_feed,
        include_auth_session_setup=True,
    )
    dual_viewer_feasible = bool(
        auth_env is not None
        and throughput_rps > 0
        and (dual_viewer_requests / throughput_rps) <= safe_window_budget_s
    )

    result = StudyBenchmarkResult(
        benchmark_id=benchmark_id,
        benchmarked_at_utc=benchmarked_at_utc,
        panel_path=str(cfg.panel_path),
        panel_hash=panel_hash,
        panel_version_id=panel_version_id,
        panel_row_count=len(panel_rows),
        viewer_modes=enabled_modes,
        posts_per_feed=cfg.posts_per_feed,
        concurrency=cfg.concurrency,
        rps=cfg.rps,
        sample_size=len(sample_rows),
        measured_request_count=measured_request_count,
        measured_success_count=measured_success_count,
        measured_failure_count=measured_failure_count,
        measured_elapsed_s=round(measured_elapsed_s, 3),
        throughput_rps=round(throughput_rps, 6),
        safety_margin=cfg.safety_margin,
        window_minutes=cfg.window_minutes,
        safe_window_budget_s=round(safe_window_budget_s, 3),
        estimated_full_sweep_requests=estimated_full_sweep_requests,
        estimated_full_sweep_duration_s=estimated_full_sweep_duration_s,
        safe_max_panel_size=safe_max_panel_size,
        required_shard_count=required_shard_count,
        full_panel_feasible=bool(throughput_rps > 0 and estimated_full_sweep_duration_s <= safe_window_budget_s),
        dual_viewer_feasible=dual_viewer_feasible,
        full_depth_feasible=bool(throughput_rps > 0 and estimated_full_sweep_duration_s <= safe_window_budget_s),
    )
    save_benchmark_summary(layout.benchmark_result_json(result.benchmark_id), result)
    return result
