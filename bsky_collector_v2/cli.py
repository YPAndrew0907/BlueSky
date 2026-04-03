from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence
from urllib.parse import urlparse

from bsky_collector_v2.fs_utils import ensure_dir, ensure_out_base, safe_cwd
from bsky_collector_v2.http_client import XrpcHosts
from bsky_collector_v2.layout import Layout
from bsky_collector_v2.logging_utils import LoggingPaths, add_run_log_file, configure_global_logging
from bsky_collector_v2.manifest import new_run_id
from bsky_collector_v2.study import (
    CORE_SAMPLE_FAMILY,
    ceil_to_window_utc,
    load_study_manifest,
    parse_utc_datetime,
    resolve_study_panel_path,
)
from bsky_collector_v2.time_utils import MicroWindow, SnapshotHour, floor_to_hour_utc, floor_to_window_utc, now_utc, utc_date_str

DEFAULT_OUT_BASE = Path("/Volumes/T9/BlueSky/data_v2_full")
DEFAULT_REPO_ROOT = Path(__file__).resolve().parent.parent
SAFE_RPS_MAX = 30.0
SAFE_CONCURRENCY_MAX = 24

logger = logging.getLogger("bsky_collector_v2")


@dataclass(frozen=True)
class GlobalArgs:
    out_base: Path
    env_path: Path | None
    log_level: str
    rps: float
    concurrency: int
    posts_per_feed: int
    time_budget_minutes: int
    feed_time_budget_s: float
    viewer_modes: tuple[str, ...]
    accept_language: str | None
    accept_labelers: str | None
    include_author_labels: bool
    vantage_id_unauth: str
    vantage_id_auth: str
    resume: bool
    dry_run: bool
    appview_host: str
    pds_host: str
    relay_host: str


def _repo_root() -> Path:
    return safe_cwd(fallback=DEFAULT_REPO_ROOT)


def _default_labelerexp_source_out_base() -> Path:
    env_value = os.environ.get("BSKY_LABELEREXP_SOURCE_OUT_BASE")
    if env_value:
        return Path(env_value)
    return _repo_root() / "data_v2_full"


def _sleep_until(target_utc) -> None:
    delay_s = (target_utc - now_utc()).total_seconds()
    if delay_s > 0:
        time.sleep(delay_s)


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--out-base", type=Path, default=DEFAULT_OUT_BASE)
    common.add_argument("--env-path", type=Path, default=None)
    common.add_argument("--log-level", choices=["info", "debug"], default="info")
    common.add_argument(
        "--appview-host",
        type=str,
        default=os.environ.get("BSKY_APPVIEW_HOST", "https://public.api.bsky.app"),
    )
    common.add_argument(
        "--pds-host",
        type=str,
        default=os.environ.get("BSKY_PDS_HOST", "https://bsky.social"),
    )
    common.add_argument(
        "--relay-host",
        type=str,
        default=os.environ.get("BSKY_RELAY_HOST", "https://bsky.network"),
    )
    common.add_argument("--rps", type=float, default=20.0)
    common.add_argument("--concurrency", type=int, default=16)
    common.add_argument("--posts-per-feed", type=int, default=50)
    common.add_argument("--time-budget-minutes", type=int, default=55)
    common.add_argument("--feed-time-budget-s", type=float, default=20.0)
    common.add_argument("--viewer-modes", type=str, default="unauth,auth")
    common.add_argument("--accept-language", type=str, default=os.environ.get("BSKY_ACCEPT_LANGUAGE"))
    common.add_argument("--accept-labelers", type=str, default=os.environ.get("BSKY_ACCEPT_LABELERS"))
    common.add_argument("--include-author-labels", action=argparse.BooleanOptionalAction, default=False)
    common.add_argument("--vantage-id-unauth", type=str, default=os.environ.get("BSKY_VANTAGE_ID_UNAUTH", "unauth"))
    common.add_argument("--vantage-id-auth", type=str, default=os.environ.get("BSKY_VANTAGE_ID_AUTH", "auth"))
    common.add_argument("--resume", action=argparse.BooleanOptionalAction, default=True)
    common.add_argument("--dry-run", action="store_true", default=False)

    parser = argparse.ArgumentParser(prog="python -m bsky_collector_v2", parents=[common])
    sub = parser.add_subparsers(dest="subcommand", required=True)

    sub.add_parser("healthcheck", parents=[common], add_help=True)
    sub.add_parser("refresh-discovery", parents=[common], add_help=True)
    sub.add_parser("index-feed-generators", parents=[common], add_help=True)

    build_panel = sub.add_parser("build-panel", parents=[common], add_help=True)
    build_panel.add_argument("--k1-popular", type=int, default=700)
    build_panel.add_argument("--k2-onboarding", type=int, default=300)
    build_panel.add_argument("--k3-suggested", type=int, default=300)
    build_panel.add_argument("--k4-longtail", type=int, default=200)

    labelerexp = sub.add_parser("build-labelerexp-panel", parents=[common], add_help=True)
    labelerexp.add_argument("--source-out-base", type=Path, default=_default_labelerexp_source_out_base())
    labelerexp.add_argument("--source-metadata-day", type=str, default=None)
    labelerexp.add_argument("--bucket", type=str, default="suggested")
    labelerexp.add_argument("--max-feeds", type=int, default=0)

    snapshot = sub.add_parser("snapshot-panel", parents=[common], add_help=True)
    snapshot.add_argument("--snapshot-hour-utc", type=str, default=None)

    wide = sub.add_parser("wide-sweep", parents=[common], add_help=True)
    wide.add_argument("--n-feeds", type=int, default=5000)

    authors = sub.add_parser("hydrate-authors", parents=[common], add_help=True)
    authors.add_argument("--max-authors", type=int, default=50000)
    authors.add_argument("--batch-size", type=int, default=25)
    authors.add_argument("--seen-after-utc", type=str, default=None)
    authors.add_argument("--seen-before-utc", type=str, default=None)

    feedgens = sub.add_parser("hydrate-feed-generators", parents=[common], add_help=True)
    feedgens.add_argument("--max-feeds", type=int, default=50000)
    feedgens.add_argument("--include-hydrated", action=argparse.BooleanOptionalAction, default=False)

    interactions = sub.add_parser("backfill-interactions", parents=[common], add_help=True)
    interactions.add_argument("--max-posts", type=int, default=10000)
    interactions.add_argument("--batch-size", type=int, default=25)
    interactions.add_argument("--max-items-per-endpoint", type=int, default=200)
    interactions.add_argument("--seen-after-utc", type=str, default=None)
    interactions.add_argument("--seen-before-utc", type=str, default=None)
    interactions.add_argument("--include-hydrated", action=argparse.BooleanOptionalAction, default=False)

    rq1 = sub.add_parser("backfill-rq1-factors", parents=[common], add_help=True)
    rq1.add_argument("--max-posts", type=int, default=10000)
    rq1.add_argument("--batch-size", type=int, default=25)
    rq1.add_argument("--max-items-per-endpoint", type=int, default=0)
    rq1.add_argument("--max-thread-depth", type=int, default=1000)
    rq1.add_argument("--max-thread-parent-height", type=int, default=1000)
    rq1.add_argument("--max-author-feed-items", type=int, default=0)
    rq1.add_argument("--max-followers-per-actor", type=int, default=0)
    rq1.add_argument("--max-follows-per-actor", type=int, default=0)
    rq1.add_argument("--max-follow-records-per-actor", type=int, default=0)
    rq1.add_argument("--max-actor-feeds-per-actor", type=int, default=0)
    rq1.add_argument("--max-lists-per-actor", type=int, default=0)
    rq1.add_argument("--max-list-members-per-list", type=int, default=0)
    rq1.add_argument("--max-starter-packs-per-actor", type=int, default=0)
    rq1.add_argument("--seen-after-utc", type=str, default=None)
    rq1.add_argument("--seen-before-utc", type=str, default=None)
    rq1.add_argument("--resolve-pds-endpoints", action=argparse.BooleanOptionalAction, default=True)
    rq1.add_argument("--follow-record-scope", type=str, default="seed+graph")
    rq1.add_argument("--shard-index", type=int, default=0)
    rq1.add_argument("--shard-count", type=int, default=1)
    rq1.add_argument("--include-hydrated", action=argparse.BooleanOptionalAction, default=False)

    seed = sub.add_parser("seed-post-registry", parents=[common], add_help=True)
    seed.add_argument("--include-hourly", action=argparse.BooleanOptionalAction, default=True)
    seed.add_argument("--include-wide", action=argparse.BooleanOptionalAction, default=True)
    seed.add_argument("--include-micro5", action=argparse.BooleanOptionalAction, default=True)
    seed.add_argument("--include-posts-first-seen", action=argparse.BooleanOptionalAction, default=True)
    seed.add_argument("--max-files", type=int, default=0)
    seed.add_argument("--max-rows", type=int, default=0)
    seed.add_argument("--enqueue-interactions", action=argparse.BooleanOptionalAction, default=True)
    seed.add_argument("--enqueue-rq1-factors", action=argparse.BooleanOptionalAction, default=True)
    seed.add_argument("--mark-first-written", action=argparse.BooleanOptionalAction, default=True)

    omnibus = sub.add_parser("collect-public-omnibus", parents=[common], add_help=True)
    omnibus.add_argument("--study-id", action="append", default=[])
    omnibus.add_argument("--all-studies", action=argparse.BooleanOptionalAction, default=True)
    omnibus.add_argument("--seed-registry", action=argparse.BooleanOptionalAction, default=True)
    omnibus.add_argument("--include-posts-first-seen", action=argparse.BooleanOptionalAction, default=True)
    omnibus.add_argument("--enqueue-interactions-from-seed", action=argparse.BooleanOptionalAction, default=True)
    omnibus.add_argument("--enqueue-rq1-factors-from-seed", action=argparse.BooleanOptionalAction, default=True)
    omnibus.add_argument("--run-index-feed-generators", action=argparse.BooleanOptionalAction, default=True)
    omnibus.add_argument("--run-refresh-discovery", action=argparse.BooleanOptionalAction, default=True)
    omnibus.add_argument("--run-build-panel", action=argparse.BooleanOptionalAction, default=True)
    omnibus.add_argument("--run-snapshot-panel", action=argparse.BooleanOptionalAction, default=True)
    omnibus.add_argument("--run-wide-sweep", action=argparse.BooleanOptionalAction, default=True)
    omnibus.add_argument("--run-hydrate-authors", action=argparse.BooleanOptionalAction, default=True)
    omnibus.add_argument("--run-hydrate-feed-generators", action=argparse.BooleanOptionalAction, default=True)
    omnibus.add_argument("--run-backfill-interactions", action=argparse.BooleanOptionalAction, default=True)
    omnibus.add_argument("--run-backfill-rq1-factors", action=argparse.BooleanOptionalAction, default=True)
    omnibus.add_argument("--run-micro-studies", action=argparse.BooleanOptionalAction, default=True)
    omnibus.add_argument("--seed-max-files", type=int, default=0)
    omnibus.add_argument("--seed-max-rows", type=int, default=0)
    omnibus.add_argument("--n-feeds-wide", type=int, default=5000)
    omnibus.add_argument("--max-authors", type=int, default=200000)
    omnibus.add_argument("--max-feed-generators", type=int, default=200000)
    omnibus.add_argument("--max-posts-interactions", type=int, default=200000)
    omnibus.add_argument("--max-posts-rq1", type=int, default=200000)
    omnibus.add_argument("--batch-size-interactions", type=int, default=25)
    omnibus.add_argument("--batch-size-rq1", type=int, default=25)
    omnibus.add_argument("--max-items-per-endpoint-interactions", type=int, default=0)
    omnibus.add_argument("--max-items-per-endpoint-rq1", type=int, default=0)
    omnibus.add_argument("--max-thread-depth", type=int, default=1000)
    omnibus.add_argument("--max-thread-parent-height", type=int, default=1000)
    omnibus.add_argument("--max-author-feed-items", type=int, default=0)
    omnibus.add_argument("--max-followers-per-actor", type=int, default=0)
    omnibus.add_argument("--max-follows-per-actor", type=int, default=0)
    omnibus.add_argument("--max-follow-records-per-actor", type=int, default=0)
    omnibus.add_argument("--max-actor-feeds-per-actor", type=int, default=0)
    omnibus.add_argument("--max-lists-per-actor", type=int, default=0)
    omnibus.add_argument("--max-list-members-per-list", type=int, default=0)
    omnibus.add_argument("--max-starter-packs-per-actor", type=int, default=0)
    omnibus.add_argument("--seen-after-utc", type=str, default=None)
    omnibus.add_argument("--seen-before-utc", type=str, default=None)
    omnibus.add_argument("--include-hydrated-interactions", action=argparse.BooleanOptionalAction, default=False)
    omnibus.add_argument("--include-hydrated-rq1", action=argparse.BooleanOptionalAction, default=False)
    omnibus.add_argument("--resolve-pds-endpoints", action=argparse.BooleanOptionalAction, default=True)
    omnibus.add_argument("--follow-record-scope", type=str, default="seed+graph")
    omnibus.add_argument("--shard-index", type=int, default=0)
    omnibus.add_argument("--shard-count", type=int, default=1)
    omnibus.add_argument("--panel-k1-popular", type=int, default=700)
    omnibus.add_argument("--panel-k2-onboarding", type=int, default=300)
    omnibus.add_argument("--panel-k3-suggested", type=int, default=300)
    omnibus.add_argument("--panel-k4-longtail", type=int, default=200)

    rq2 = sub.add_parser("rq2-pipeline", parents=[common], add_help=True)
    rq2.add_argument("--data-root", type=Path, default=None)
    rq2.add_argument("--out-dir", type=Path, required=True)
    rq2.add_argument("--annotation-dir", type=Path, default=None)
    rq2.add_argument("--preset", type=str, default="politics_v1")
    rq2.add_argument("--topic", action="append", default=[])
    rq2.add_argument("--surface", action="append", default=[])
    rq2.add_argument("--start-date", type=str, default=None)
    rq2.add_argument("--end-date", type=str, default=None)
    rq2.add_argument("--include-labelerexp", action=argparse.BooleanOptionalAction, default=False)
    rq2.add_argument("--run-topic-batch", action=argparse.BooleanOptionalAction, default=True)
    rq2.add_argument("--run-sampling", action=argparse.BooleanOptionalAction, default=True)
    rq2.add_argument("--run-label-application", action=argparse.BooleanOptionalAction, default=True)
    rq2.add_argument("--run-annotation-merge", action=argparse.BooleanOptionalAction, default=True)
    rq2.add_argument("--run-frame-table", action=argparse.BooleanOptionalAction, default=True)
    rq2.add_argument("--run-clustering", action=argparse.BooleanOptionalAction, default=True)
    rq2.add_argument("--cluster-anchor-kind", action="append", default=[])
    rq2.add_argument("--cluster-exclude-text-pattern", action="append", default=[])
    rq2.add_argument("--cluster-time-window-hours", type=int, default=12)
    rq2.add_argument("--cluster-min-size", type=int, default=2)
    rq2.add_argument("--max-clusters", type=int, default=25)
    rq2.add_argument("--per-cluster", type=int, default=4)

    state_writer = sub.add_parser("state-writer", parents=[common], add_help=True)
    default_socket_path = None if os.name == "nt" else Path("/tmp/bsky_state_writer.sock")
    state_writer.add_argument("--socket-path", type=Path, default=default_socket_path)
    state_writer.add_argument("--tcp", type=str, default=None)

    sub.add_parser("sync-effective-csv", parents=[common], add_help=True)
    sub.add_parser("backfill-run-artifacts", parents=[common], add_help=True)

    study_bench = sub.add_parser("study-benchmark", parents=[common], add_help=True)
    study_bench.add_argument("--panel-path", type=Path, default=None)
    study_bench.add_argument("--sample-size", type=int, default=200)
    study_bench.add_argument("--window-minutes", type=int, default=5)
    study_bench.add_argument("--safety-margin", type=float, default=0.85)

    study_init = sub.add_parser("study-init", parents=[common], add_help=True)
    study_init.add_argument("--benchmark-path", type=Path, required=True)
    study_init.add_argument("--source-panel-path", type=Path, default=None)
    study_init.add_argument("--sample-family", choices=["micro5_core_full", "micro5_extended_sharded"], required=True)
    study_init.add_argument("--auto-core-size", action=argparse.BooleanOptionalAction, default=False)
    study_init.add_argument("--core-panel-size", type=int, default=None)
    study_init.add_argument("--auto-shard-count", action=argparse.BooleanOptionalAction, default=False)
    study_init.add_argument("--shard-count", type=int, default=None)
    study_init.add_argument("--window-origin-utc", type=str, default=None)
    study_init.add_argument("--study-id", type=str, default=None)
    study_init.add_argument("--study-group-id", type=str, default=None)
    study_init.add_argument("--max-feeds", type=int, default=None)
    study_init.add_argument("--selection-strategy", type=str, default="keep_input_order")
    study_init.add_argument("--max-attempts", type=int, default=3)

    micro = sub.add_parser("micro-snapshot-study", parents=[common], add_help=True)
    micro.add_argument("--study-id", type=str, required=True)
    micro.add_argument("--scheduled-window-start-utc", type=str, default=None)
    micro.add_argument("--sleep-until-window", action="store_true")
    micro.add_argument("--sample-family", choices=["micro5_core_full", "micro5_extended_sharded"], default=None)
    micro.add_argument("--frozen-panel-path", type=Path, default=None)
    micro.add_argument("--public-only", action=argparse.BooleanOptionalAction, default=False)

    rq2_tables = sub.add_parser("rq2-generate-frame-tables", parents=[common], add_help=True)
    rq2_tables.add_argument("--batch-dir", type=Path, required=True)
    rq2_tables.add_argument("--label-rows-path", type=Path, required=True)
    rq2_tables.add_argument("--out-dir", type=Path, required=True)

    return parser


def _parse_global_args(ns: argparse.Namespace) -> GlobalArgs:
    env_path = ns.env_path
    if env_path is None:
        env_var = os.environ.get("BSKY_ENV_PATH")
        if env_var:
            env_path = Path(env_var)

    viewer_modes = tuple(mode.strip() for mode in str(ns.viewer_modes).split(",") if mode.strip())
    return GlobalArgs(
        out_base=ns.out_base,
        env_path=env_path,
        log_level=str(ns.log_level),
        rps=float(ns.rps),
        concurrency=int(ns.concurrency),
        posts_per_feed=int(ns.posts_per_feed),
        time_budget_minutes=int(ns.time_budget_minutes),
        feed_time_budget_s=float(ns.feed_time_budget_s),
        viewer_modes=viewer_modes,
        accept_language=(str(ns.accept_language).strip() if ns.accept_language else None),
        accept_labelers=(str(ns.accept_labelers).strip() if ns.accept_labelers else None),
        include_author_labels=bool(ns.include_author_labels),
        vantage_id_unauth=str(ns.vantage_id_unauth).strip() or "unauth",
        vantage_id_auth=str(ns.vantage_id_auth).strip() or "auth",
        resume=bool(ns.resume),
        dry_run=bool(ns.dry_run),
        appview_host=str(ns.appview_host),
        pds_host=str(ns.pds_host),
        relay_host=str(ns.relay_host),
    )


def _parse_tcp_target(raw: str) -> tuple[str, int]:
    raw_value = str(raw or "").strip()
    if not raw_value:
        raise ValueError("empty --tcp target")
    parsed = urlparse(raw_value if raw_value.startswith("tcp://") else "tcp://" + raw_value)
    host = parsed.hostname
    port = parsed.port
    if not host or port is None:
        raise ValueError(f"invalid --tcp target: {raw_value!r}")
    return str(host), int(port)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    g = _parse_global_args(args)

    try:
        check = ensure_out_base(g.out_base)
    except Exception as err:  # noqa: BLE001
        print(f"ERROR: {err}", file=sys.stderr)
        return 2

    layout = Layout(out_base=check.out_base)
    ensure_dir(layout.logs_root)
    ensure_dir(layout.control_root)
    configure_global_logging(
        log_level=g.log_level,
        paths=LoggingPaths(collector_log=layout.global_collector_log, errors_log=layout.global_errors_log),
    )

    logger.info("out_base ok path=%s mount=%s", str(check.out_base), str(check.mountpoint))
    logger.info("job start subcommand=%s dry_run=%s resume=%s", args.subcommand, g.dry_run, g.resume)
    if g.rps > SAFE_RPS_MAX or g.concurrency > SAFE_CONCURRENCY_MAX:
        logger.warning(
            "aggressive throttling config rps=%s concurrency=%s (recommended <= %.1f rps and <= %s concurrency)",
            g.rps,
            g.concurrency,
            SAFE_RPS_MAX,
            SAFE_CONCURRENCY_MAX,
        )

    hosts = XrpcHosts(appview_host=g.appview_host, pds_host=g.pds_host)
    repo_root = _repo_root()

    try:
        match args.subcommand:
            case "healthcheck":
                from bsky_collector_v2.jobs.healthcheck import run_healthcheck

                result = asyncio.run(
                    run_healthcheck(layout=layout, hosts=hosts, env_path=g.env_path, rps=g.rps, dry_run=g.dry_run)
                )
                logger.info(
                    "healthcheck result out_base_ok=%s control_db_ok=%s unauth_http_ok=%s auth_env_ok=%s auth_session_ok=%s",
                    result.out_base_ok,
                    result.control_db_ok,
                    result.unauth_http_ok,
                    result.auth_env_ok,
                    result.auth_session_ok,
                )
                return 0
            case "refresh-discovery":
                from bsky_collector_v2.jobs.refresh_discovery import run_refresh_discovery

                asyncio.run(
                    run_refresh_discovery(
                        layout=layout,
                        repo_root=repo_root,
                        run_id=new_run_id(),
                        hosts=hosts,
                        env_path=g.env_path,
                        viewer_modes=g.viewer_modes,
                        rps=g.rps,
                        concurrency=g.concurrency,
                        accept_language=g.accept_language,
                        accept_labelers=g.accept_labelers,
                        vantage_id_unauth=g.vantage_id_unauth,
                        vantage_id_auth=g.vantage_id_auth,
                        resume=g.resume,
                        dry_run=g.dry_run,
                    )
                )
                return 0
            case "index-feed-generators":
                from bsky_collector_v2.jobs.index_feed_generators import run_index_feed_generators

                date_str = utc_date_str(now_utc())
                with add_run_log_file(layout.feed_generators_index_log(date_str), log_level=g.log_level):
                    asyncio.run(
                        run_index_feed_generators(
                            layout=layout,
                            repo_root=repo_root,
                            hosts=hosts,
                            relay_host=g.relay_host,
                            env_path=g.env_path,
                            rps=g.rps,
                            time_budget_minutes=g.time_budget_minutes,
                            resume=g.resume,
                            dry_run=g.dry_run,
                            accept_language=g.accept_language,
                            accept_labelers=g.accept_labelers,
                            vantage_id=g.vantage_id_unauth,
                        )
                    )
                return 0
            case "build-panel":
                from bsky_collector_v2.jobs.build_panel import PanelBuildConfig, run_build_panel

                asyncio.run(
                    run_build_panel(
                        layout=layout,
                        run_id=new_run_id(),
                        hosts=hosts,
                        env_path=g.env_path,
                        rps=g.rps,
                        concurrency=g.concurrency,
                        dry_run=g.dry_run,
                        cfg=PanelBuildConfig(
                            k1_popular=int(getattr(args, "k1_popular")),
                            k2_onboarding=int(getattr(args, "k2_onboarding")),
                            k3_suggested=int(getattr(args, "k3_suggested")),
                            k4_longtail=int(getattr(args, "k4_longtail")),
                        ),
                    )
                )
                return 0
            case "build-labelerexp-panel":
                from bsky_collector_v2.jobs.build_labelerexp_panel import LabelerExpPanelConfig, run_build_labelerexp_panel

                max_feeds = int(getattr(args, "max_feeds") or 0)
                cfg = LabelerExpPanelConfig(
                    bucket=str(getattr(args, "bucket") or "suggested"),
                    max_feeds=(max_feeds if max_feeds > 0 else None),
                )
                run_build_labelerexp_panel(
                    layout=layout,
                    source_out_base=Path(getattr(args, "source_out_base")),
                    source_metadata_day=getattr(args, "source_metadata_day"),
                    dry_run=g.dry_run,
                    cfg=cfg,
                )
                return 0
            case "snapshot-panel":
                from bsky_collector_v2.jobs.snapshot_panel import run_snapshot_panel

                hour_dt = floor_to_hour_utc(now_utc())
                if args.snapshot_hour_utc:
                    hour_dt = floor_to_hour_utc(parse_utc_datetime(str(args.snapshot_hour_utc)))
                hour = SnapshotHour(hour_utc=hour_dt)
                with add_run_log_file(layout.hourly_snapshot_log(hour), log_level=g.log_level):
                    asyncio.run(
                        run_snapshot_panel(
                            layout=layout,
                            hosts=hosts,
                            env_path=g.env_path,
                            viewer_modes=g.viewer_modes,
                            posts_per_feed=g.posts_per_feed,
                            rps=g.rps,
                            concurrency=g.concurrency,
                            time_budget_minutes=g.time_budget_minutes,
                            feed_time_budget_s=g.feed_time_budget_s,
                            resume=g.resume,
                            dry_run=g.dry_run,
                            snapshot_hour_utc=hour_dt,
                            accept_language=g.accept_language,
                            accept_labelers=g.accept_labelers,
                            include_author_labels=g.include_author_labels,
                            vantage_id_unauth=g.vantage_id_unauth,
                            vantage_id_auth=g.vantage_id_auth,
                        )
                    )
                return 0
            case "wide-sweep":
                from bsky_collector_v2.jobs.wide_sweep import run_wide_sweep

                date_str = utc_date_str(now_utc())
                with add_run_log_file(layout.wide_log(date_str), log_level=g.log_level):
                    asyncio.run(
                        run_wide_sweep(
                            layout=layout,
                            hosts=hosts,
                            n_feeds=int(args.n_feeds),
                            posts_per_feed=g.posts_per_feed,
                            rps=g.rps,
                            concurrency=g.concurrency,
                            time_budget_minutes=g.time_budget_minutes,
                            feed_time_budget_s=g.feed_time_budget_s,
                            resume=g.resume,
                            dry_run=g.dry_run,
                            accept_language=g.accept_language,
                            accept_labelers=g.accept_labelers,
                            include_author_labels=g.include_author_labels,
                            vantage_id=g.vantage_id_unauth,
                        )
                    )
                return 0
            case "hydrate-authors":
                from bsky_collector_v2.jobs.hydrate_authors import HydrateAuthorsConfig, run_hydrate_authors

                asyncio.run(
                    run_hydrate_authors(
                        layout=layout,
                        hosts=hosts,
                        run_id=new_run_id(),
                        rps=g.rps,
                        concurrency=g.concurrency,
                        dry_run=g.dry_run,
                        cfg=HydrateAuthorsConfig(
                            batch_size=int(args.batch_size),
                            max_authors=int(args.max_authors),
                            seen_after_utc=args.seen_after_utc,
                            seen_before_utc=args.seen_before_utc,
                        ),
                        accept_language=g.accept_language,
                        accept_labelers=g.accept_labelers,
                        vantage_id=g.vantage_id_unauth,
                    )
                )
                return 0
            case "hydrate-feed-generators":
                from bsky_collector_v2.jobs.hydrate_feed_generators import HydrateFeedGeneratorsConfig, run_hydrate_feed_generators

                asyncio.run(
                    run_hydrate_feed_generators(
                        layout=layout,
                        hosts=hosts,
                        run_id=new_run_id(),
                        rps=g.rps,
                        concurrency=g.concurrency,
                        dry_run=g.dry_run,
                        cfg=HydrateFeedGeneratorsConfig(
                            max_feeds=int(args.max_feeds),
                            include_hydrated=bool(args.include_hydrated),
                        ),
                        accept_language=g.accept_language,
                        accept_labelers=g.accept_labelers,
                        vantage_id=g.vantage_id_unauth,
                    )
                )
                return 0
            case "state-writer":
                from bsky_collector_v2.state_writer import StateWriterConfig, run_state_writer

                if getattr(args, "tcp", None):
                    host, port = _parse_tcp_target(str(getattr(args, "tcp")))
                    cfg = StateWriterConfig(db_path=layout.control_db_path, tcp_host=host, tcp_port=port)
                else:
                    socket_path = getattr(args, "socket_path", None)
                    if socket_path is None:
                        raise ValueError("state-writer requires --tcp HOST:PORT on this platform (or pass --socket-path)")
                    cfg = StateWriterConfig(db_path=layout.control_db_path, socket_path=Path(socket_path))
                run_state_writer(cfg=cfg)
                return 0
            case "sync-effective-csv":
                from bsky_collector_v2.effective_csv import sync_effective_csv_full

                if g.dry_run:
                    logger.info("dry_run=true: would sync effective csv root=%s", str(layout.effective_csv_root))
                    return 0
                sync_effective_csv_full(layout)
                logger.info("effective csv synced root=%s", str(layout.effective_csv_root))
                return 0
            case "backfill-run-artifacts":
                from bsky_collector_v2.jobs.backfill_run_artifacts import run_backfill_run_artifacts

                summary = run_backfill_run_artifacts(layout=layout, dry_run=g.dry_run)
                logger.info("backfill run artifacts summary=%s", summary.to_dict())
                return 0
            case "seed-post-registry":
                from bsky_collector_v2.jobs.seed_post_registry import SeedPostRegistryConfig, run_seed_post_registry

                summary = run_seed_post_registry(
                    layout=layout,
                    run_id=new_run_id(),
                    dry_run=g.dry_run,
                    cfg=SeedPostRegistryConfig(
                        include_hourly=bool(args.include_hourly),
                        include_wide=bool(args.include_wide),
                        include_micro5=bool(args.include_micro5),
                        include_posts_first_seen=bool(args.include_posts_first_seen),
                        max_files=int(args.max_files),
                        max_rows=int(args.max_rows),
                        enqueue_interactions=bool(args.enqueue_interactions),
                        enqueue_rq1_factors=bool(args.enqueue_rq1_factors),
                        mark_first_written=bool(args.mark_first_written),
                    ),
                )
                logger.info("seed post registry summary=%s", summary.to_dict())
                return 0
            case "collect-public-omnibus":
                from bsky_collector_v2.jobs.public_omnibus import PublicOmnibusConfig, run_public_omnibus

                summary = asyncio.run(
                    run_public_omnibus(
                        layout=layout,
                        hosts=hosts,
                        relay_host=g.relay_host,
                        run_id=new_run_id(),
                        rps=g.rps,
                        concurrency=g.concurrency,
                        posts_per_feed=g.posts_per_feed,
                        time_budget_minutes=g.time_budget_minutes,
                        feed_time_budget_s=g.feed_time_budget_s,
                        dry_run=g.dry_run,
                        resume=g.resume,
                        accept_language=g.accept_language,
                        accept_labelers=g.accept_labelers,
                        include_author_labels=g.include_author_labels,
                        vantage_id_unauth=g.vantage_id_unauth,
                        cfg=PublicOmnibusConfig(
                            seed_registry=bool(args.seed_registry),
                            include_posts_first_seen=bool(args.include_posts_first_seen),
                            enqueue_interactions_from_seed=bool(args.enqueue_interactions_from_seed),
                            enqueue_rq1_factors_from_seed=bool(args.enqueue_rq1_factors_from_seed),
                            run_index_feed_generators=bool(args.run_index_feed_generators),
                            run_refresh_discovery=bool(args.run_refresh_discovery),
                            run_build_panel=bool(args.run_build_panel),
                            run_snapshot_panel=bool(args.run_snapshot_panel),
                            run_wide_sweep=bool(args.run_wide_sweep),
                            run_hydrate_authors=bool(args.run_hydrate_authors),
                            run_hydrate_feed_generators=bool(args.run_hydrate_feed_generators),
                            run_backfill_interactions=bool(args.run_backfill_interactions),
                            run_backfill_rq1_factors=bool(args.run_backfill_rq1_factors),
                            run_micro_studies=bool(args.run_micro_studies),
                            all_studies=bool(args.all_studies),
                            study_ids=tuple(str(item) for item in (args.study_id or []) if str(item).strip()),
                            seed_max_files=int(args.seed_max_files),
                            seed_max_rows=int(args.seed_max_rows),
                            n_feeds_wide=int(args.n_feeds_wide),
                            max_authors=int(args.max_authors),
                            max_feed_generators=int(args.max_feed_generators),
                            max_posts_interactions=int(args.max_posts_interactions),
                            max_posts_rq1=int(args.max_posts_rq1),
                            batch_size_interactions=int(args.batch_size_interactions),
                            batch_size_rq1=int(args.batch_size_rq1),
                            max_items_per_endpoint_interactions=int(args.max_items_per_endpoint_interactions),
                            max_items_per_endpoint_rq1=int(args.max_items_per_endpoint_rq1),
                            max_thread_depth=int(args.max_thread_depth),
                            max_thread_parent_height=int(args.max_thread_parent_height),
                            max_author_feed_items=int(args.max_author_feed_items),
                            max_followers_per_actor=int(args.max_followers_per_actor),
                            max_follows_per_actor=int(args.max_follows_per_actor),
                            max_follow_records_per_actor=int(args.max_follow_records_per_actor),
                            max_actor_feeds_per_actor=int(args.max_actor_feeds_per_actor),
                            max_lists_per_actor=int(args.max_lists_per_actor),
                            max_list_members_per_list=int(args.max_list_members_per_list),
                            max_starter_packs_per_actor=int(args.max_starter_packs_per_actor),
                            seen_after_utc=args.seen_after_utc,
                            seen_before_utc=args.seen_before_utc,
                            include_hydrated_interactions=bool(args.include_hydrated_interactions),
                            include_hydrated_rq1=bool(args.include_hydrated_rq1),
                            resolve_pds_endpoints=bool(args.resolve_pds_endpoints),
                            follow_record_scope=str(args.follow_record_scope),
                            shard_index=int(args.shard_index),
                            shard_count=int(args.shard_count),
                            panel_k1_popular=int(args.panel_k1_popular),
                            panel_k2_onboarding=int(args.panel_k2_onboarding),
                            panel_k3_suggested=int(args.panel_k3_suggested),
                            panel_k4_longtail=int(args.panel_k4_longtail),
                        ),
                    )
                )
                logger.info("public omnibus summary=%s", summary.to_dict())
                return 0
            case "rq2-pipeline":
                from bsky_collector_v2.rq2_pipeline import (
                    DEFAULT_CLUSTER_EXCLUDE_PATTERNS,
                    Rq2PipelineConfig,
                    run_rq2_pipeline,
                )

                raw_surfaces = [str(item).strip() for item in (args.surface or []) if str(item).strip()]
                raw_anchor_kinds = [
                    str(item).strip()
                    for item in (args.cluster_anchor_kind or [])
                    if str(item).strip()
                ]
                raw_exclude_patterns = [
                    str(item).strip()
                    for item in (args.cluster_exclude_text_pattern or [])
                    if str(item).strip()
                ]
                out_dir = Path(getattr(args, "out_dir"))
                annotation_dir = (
                    Path(getattr(args, "annotation_dir"))
                    if getattr(args, "annotation_dir", None)
                    else out_dir / "annotations"
                )
                summary = run_rq2_pipeline(
                    Rq2PipelineConfig(
                        data_root=Path(getattr(args, "data_root") or g.out_base),
                        out_dir=out_dir,
                        annotation_dir=annotation_dir,
                        preset=str(getattr(args, "preset")),
                        topics=tuple(
                            str(item).strip()
                            for item in (getattr(args, "topic", None) or [])
                            if str(item).strip()
                        ),
                        surfaces=tuple(dict.fromkeys(raw_surfaces or ["hourly", "wide"])),
                        start_date=getattr(args, "start_date", None),
                        end_date=getattr(args, "end_date", None),
                        include_labelerexp=bool(getattr(args, "include_labelerexp", False)),
                        run_topic_batch=bool(getattr(args, "run_topic_batch", True)),
                        run_sampling=bool(getattr(args, "run_sampling", True)),
                        run_label_application=bool(getattr(args, "run_label_application", True)),
                        run_annotation_merge=bool(getattr(args, "run_annotation_merge", True)),
                        run_frame_table=bool(getattr(args, "run_frame_table", True)),
                        run_clustering=bool(getattr(args, "run_clustering", True)),
                        cluster_anchor_kinds=tuple(dict.fromkeys(raw_anchor_kinds or ["tokens"])),
                        cluster_exclude_text_patterns=tuple(
                            dict.fromkeys(raw_exclude_patterns or list(DEFAULT_CLUSTER_EXCLUDE_PATTERNS))
                        ),
                        cluster_time_window_hours=int(getattr(args, "cluster_time_window_hours", 12)),
                        cluster_min_size=int(getattr(args, "cluster_min_size", 2)),
                        max_clusters=int(getattr(args, "max_clusters", 25)),
                        per_cluster=int(getattr(args, "per_cluster", 4)),
                    )
                )
                logger.info("rq2 pipeline summary=%s", summary)
                return 0
            case "backfill-interactions":
                from bsky_collector_v2.jobs.backfill_interactions import BackfillInteractionsConfig, run_backfill_interactions

                asyncio.run(
                    run_backfill_interactions(
                        layout=layout,
                        hosts=hosts,
                        run_id=new_run_id(),
                        rps=g.rps,
                        concurrency=g.concurrency,
                        dry_run=g.dry_run,
                        cfg=BackfillInteractionsConfig(
                            max_posts=int(args.max_posts),
                            batch_size=int(args.batch_size),
                            max_items_per_endpoint=int(args.max_items_per_endpoint),
                            seen_after_utc=args.seen_after_utc,
                            seen_before_utc=args.seen_before_utc,
                            include_hydrated=bool(args.include_hydrated),
                        ),
                        accept_language=g.accept_language,
                        accept_labelers=g.accept_labelers,
                        vantage_id=g.vantage_id_unauth,
                    )
                )
                return 0
            case "backfill-rq1-factors":
                from bsky_collector_v2.jobs.backfill_rq1_factors import BackfillRq1FactorsConfig, run_backfill_rq1_factors

                asyncio.run(
                    run_backfill_rq1_factors(
                        layout=layout,
                        hosts=hosts,
                        run_id=new_run_id(),
                        rps=g.rps,
                        concurrency=g.concurrency,
                        dry_run=g.dry_run,
                        cfg=BackfillRq1FactorsConfig(
                            max_posts=int(args.max_posts),
                            batch_size=int(args.batch_size),
                            max_items_per_endpoint=int(args.max_items_per_endpoint),
                            max_thread_depth=int(args.max_thread_depth),
                            max_thread_parent_height=int(args.max_thread_parent_height),
                            max_author_feed_items=int(args.max_author_feed_items),
                            max_followers_per_actor=int(args.max_followers_per_actor),
                            max_follows_per_actor=int(args.max_follows_per_actor),
                            max_follow_records_per_actor=int(args.max_follow_records_per_actor),
                            max_actor_feeds_per_actor=int(args.max_actor_feeds_per_actor),
                            max_lists_per_actor=int(args.max_lists_per_actor),
                            max_list_members_per_list=int(args.max_list_members_per_list),
                            max_starter_packs_per_actor=int(args.max_starter_packs_per_actor),
                            seen_after_utc=args.seen_after_utc,
                            seen_before_utc=args.seen_before_utc,
                            include_hydrated=bool(args.include_hydrated),
                            resolve_pds_endpoints=bool(args.resolve_pds_endpoints),
                            follow_record_scope=str(args.follow_record_scope),
                            shard_index=int(args.shard_index),
                            shard_count=int(args.shard_count),
                        ),
                        accept_language=g.accept_language,
                        accept_labelers=g.accept_labelers,
                        vantage_id=g.vantage_id_unauth,
                    )
                )
                return 0
            case "study-benchmark":
                from bsky_collector_v2.jobs.study_benchmark import StudyBenchmarkConfig, run_study_benchmark

                panel_path = Path(getattr(args, "panel_path") or layout.panel_active_csv)
                result = asyncio.run(
                    run_study_benchmark(
                        layout=layout,
                        hosts=hosts,
                        env_path=g.env_path,
                        cfg=StudyBenchmarkConfig(
                            panel_path=panel_path,
                            sample_size=int(getattr(args, "sample_size")),
                            viewer_modes=g.viewer_modes,
                            posts_per_feed=g.posts_per_feed,
                            concurrency=g.concurrency,
                            rps=g.rps,
                            safety_margin=float(getattr(args, "safety_margin")),
                            window_minutes=int(getattr(args, "window_minutes")),
                            accept_language=g.accept_language,
                            accept_labelers=g.accept_labelers,
                            include_author_labels=g.include_author_labels,
                            vantage_id_unauth=g.vantage_id_unauth,
                            vantage_id_auth=g.vantage_id_auth,
                        ),
                        dry_run=g.dry_run,
                    )
                )
                logger.info(
                    "study benchmark complete benchmark_id=%s panel_rows=%s safe_max_panel_size=%s estimated_full_sweep_duration_s=%s full_panel_feasible=%s",
                    result.benchmark_id,
                    result.panel_row_count,
                    result.safe_max_panel_size,
                    result.estimated_full_sweep_duration_s,
                    result.full_panel_feasible,
                )
                return 0
            case "study-init":
                from bsky_collector_v2.jobs.study_init import StudyInitConfig, run_study_init

                source_panel_path = Path(getattr(args, "source_panel_path") or layout.panel_active_csv)
                manifest = run_study_init(
                    layout=layout,
                    cfg=StudyInitConfig(
                        sample_family=str(getattr(args, "sample_family")),  # type: ignore[arg-type]
                        benchmark_path=Path(getattr(args, "benchmark_path")),
                        source_panel_path=source_panel_path,
                        study_id=getattr(args, "study_id"),
                        viewer_modes=g.viewer_modes,
                        posts_per_feed=g.posts_per_feed,
                        accept_language=g.accept_language,
                        accept_labelers=g.accept_labelers,
                        include_author_labels=g.include_author_labels,
                        vantage_id_unauth=g.vantage_id_unauth,
                        vantage_id_auth=g.vantage_id_auth,
                        auto_core_size=bool(getattr(args, "auto_core_size")),
                        requested_core_size=(
                            int(getattr(args, "core_panel_size"))
                            if getattr(args, "core_panel_size", None) is not None
                            else getattr(args, "max_feeds")
                        ),
                        auto_shard_count=bool(getattr(args, "auto_shard_count")),
                        requested_shard_count=(
                            int(getattr(args, "shard_count"))
                            if getattr(args, "shard_count", None) is not None
                            else None
                        ),
                        window_origin_utc=(
                            parse_utc_datetime(str(getattr(args, "window_origin_utc")))
                            if getattr(args, "window_origin_utc")
                            else None
                        ),
                        study_group_id=getattr(args, "study_group_id"),
                        selection_strategy=str(getattr(args, "selection_strategy") or "keep_input_order"),
                        feed_time_budget_s=g.feed_time_budget_s,
                        max_attempts=int(getattr(args, "max_attempts")),
                    ),
                    dry_run=g.dry_run,
                )
                logger.info(
                    "study initialized study_id=%s sample_family=%s panel_rows=%s panel_hash=%s",
                    manifest.get("study_id"),
                    manifest.get("sample_family"),
                    manifest.get("panel_row_count"),
                    manifest.get("panel_hash"),
                )
                return 0
            case "micro-snapshot-study":
                from bsky_collector_v2.jobs.micro_snapshot_study import run_micro_snapshot_study

                study_id = str(getattr(args, "study_id"))
                study_manifest = load_study_manifest(layout.study_manifest_json(study_id))
                resolved_panel_path = resolve_study_panel_path(
                    layout=layout,
                    study_id=study_id,
                    study_manifest=study_manifest,
                )
                study_manifest["panel_path"] = str(resolved_panel_path)
                sample_family = str(study_manifest.get("sample_family") or CORE_SAMPLE_FAMILY)
                requested_family = getattr(args, "sample_family", None)
                if requested_family and str(requested_family) != sample_family:
                    raise ValueError(f"study sample_family mismatch study={sample_family} requested={requested_family}")
                requested_panel = getattr(args, "frozen_panel_path", None)
                if requested_panel is not None and resolved_panel_path != Path(requested_panel):
                    raise ValueError(
                        f"study frozen panel mismatch study={resolved_panel_path} requested={requested_panel}"
                    )

                window_minutes = int(
                    study_manifest.get("intended_window_minutes")
                    or study_manifest.get("window_size_minutes")
                    or 5
                )
                explicit_window_start = (
                    parse_utc_datetime(str(getattr(args, "scheduled_window_start_utc")))
                    if getattr(args, "scheduled_window_start_utc", None)
                    else None
                )
                if explicit_window_start is not None:
                    scheduled_window_start_utc = floor_to_window_utc(explicit_window_start, window_minutes=window_minutes)
                elif bool(getattr(args, "sleep_until_window")):
                    scheduled_window_start_utc = ceil_to_window_utc(now_utc(), window_minutes=window_minutes)
                else:
                    scheduled_window_start_utc = floor_to_window_utc(now_utc(), window_minutes=window_minutes)
                if bool(getattr(args, "sleep_until_window")):
                    _sleep_until(scheduled_window_start_utc)

                micro_window = MicroWindow(start_utc=scheduled_window_start_utc, window_minutes=window_minutes)
                with add_run_log_file(
                    layout.micro5_snapshot_log(
                        study_id=study_id,
                        sample_family=sample_family,
                        window=micro_window,
                    ),
                    log_level=g.log_level,
                ):
                    asyncio.run(
                        run_micro_snapshot_study(
                            layout=layout,
                            hosts=hosts,
                            env_path=g.env_path,
                            study_id=study_id,
                            scheduled_window_start_utc=scheduled_window_start_utc,
                            rps=g.rps,
                            concurrency=g.concurrency,
                            feed_time_budget_s=g.feed_time_budget_s,
                            resume=g.resume,
                            dry_run=g.dry_run,
                            public_only=bool(getattr(args, "public_only", False)),
                        )
                    )
                return 0
            case "rq2-generate-frame-tables":
                from bsky_collector_v2.rq2_frame_tables import generate_frame_exposure_supply_tables

                summary = generate_frame_exposure_supply_tables(
                    batch_dir=Path(getattr(args, "batch_dir")),
                    label_rows_path=Path(getattr(args, "label_rows_path")),
                    out_dir=Path(getattr(args, "out_dir")),
                )
                logger.info("rq2 frame tables summary=%s", summary)
                return 0
            case _:
                logger.error("unknown subcommand: %s", args.subcommand)
                return 2
    except KeyboardInterrupt:
        logger.warning("job interrupted subcommand=%s", args.subcommand)
        return 130
    except Exception as err:  # noqa: BLE001
        logger.exception("job failed subcommand=%s err=%r", args.subcommand, err)
        return 1
