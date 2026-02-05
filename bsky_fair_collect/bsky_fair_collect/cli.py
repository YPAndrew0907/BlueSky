from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from bsky_fair_collect.backfill import BackfillOverrides, backfill_and_export
from bsky_fair_collect.config import AppConfig, AuthMode, Hosts, OutputPaths, RunParams
from bsky_fair_collect.postprocess import run_postprocess
from bsky_fair_collect.run_all import run_all


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bsky_fair_collect", add_help=True)
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run-all", help="Run all collection stages end-to-end.")
    run.add_argument("--out-dir", type=Path, required=True)
    run.add_argument(
        "--auth-mode",
        choices=[m.value for m in AuthMode],
        default=AuthMode.UNAUTH.value,
    )
    run.add_argument("--appview-host", default=Hosts().appview_host)
    run.add_argument("--relay-host", default=Hosts().relay_host)
    run.add_argument("--rps", type=float, default=RunParams().rps)
    run.add_argument("--max-retries", type=int, default=RunParams().max_retries)
    run.add_argument("--posts-per-feed", type=int, default=RunParams().posts_per_feed)
    run.add_argument("--n-discovery", type=int, default=RunParams().n_discovery)
    run.add_argument("--n-popular", type=int, default=RunParams().n_popular)
    run.add_argument("--n-less-known", type=int, default=RunParams().n_less_known)
    run.add_argument(
        "--index-max-actors",
        type=int,
        default=None,
        help="Optional safety limit for Stage 1 (process at most N new actor DIDs).",
    )
    run.add_argument(
        "--starterpack-max-per-query",
        type=int,
        default=RunParams().starterpack_max_per_query,
        help="Max new starter packs to hydrate per search query.",
    )
    run.add_argument(
        "--starterpack-query-limit",
        type=int,
        default=None,
        help="Optional safety limit: only run the first N starterpack queries.",
    )
    run.add_argument(
        "--starterpack-actor-limit",
        type=int,
        default=None,
        help="Optional safety limit for Stage 2 (process at most N new starterpack creator DIDs).",
    )
    run.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Console log level (file log always includes DEBUG).",
    )

    backfill = sub.add_parser(
        "backfill",
        help="Backfill generator metadata for touched feeds and re-export OUT_DIR/csv/*. Useful post-run.",
    )
    backfill.add_argument("--out-dir", type=Path, required=True)
    backfill.add_argument(
        "--auth-mode",
        choices=[m.value for m in AuthMode],
        default=None,
        help="Override auth mode (default: read from OUT_DIR/csv/run_metadata.csv if present).",
    )
    backfill.add_argument("--appview-host", default=None, help="Override AppView host (default: from run_metadata.csv).")
    backfill.add_argument("--relay-host", default=None, help="Override Relay host (default: from run_metadata.csv).")
    backfill.add_argument("--rps", type=float, default=None, help="Override requests/second (default: from run_metadata.csv).")
    backfill.add_argument("--max-retries", type=int, default=None, help="Override max retries (default: from run_metadata.csv).")
    backfill.add_argument(
        "--allow-running",
        action="store_true",
        help="Allow backfill even if OUT_DIR/pid.txt looks alive (not recommended).",
    )
    backfill.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Console log level (file log always includes DEBUG).",
    )

    post = sub.add_parser(
        "postprocess",
        help="Create convenience joined tables under OUT_DIR/postprocess/ (does not modify OUT_DIR/csv/).",
    )
    post.add_argument("--out-dir", type=Path, required=True)
    post.add_argument(
        "--dest-dir",
        type=Path,
        default=None,
        help="Destination directory (default: OUT_DIR/postprocess).",
    )
    post.add_argument("--overwrite", action="store_true", help="Overwrite existing postprocess outputs.")
    post.add_argument("--zip", action="store_true", help="Also write OUT_DIR/postprocess/postprocess.zip.")
    post.add_argument(
        "--no-metrics",
        action="store_true",
        help="Skip H1–H6 derived metric tables (only write feeds_flat + impressions_flat).",
    )
    post.add_argument(
        "--labels-flat",
        action="store_true",
        help="Also write impression_labels_flat.csv.gz (one row per label assignment per ranked impression).",
    )
    post.add_argument(
        "--allow-running",
        action="store_true",
        help="Allow postprocess even if OUT_DIR/pid.txt looks alive (not recommended).",
    )
    post.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Console log level (file log always includes DEBUG).",
    )

    return p


def _configure_logging(*, out_dir: Path, console_level: str) -> None:
    logs_dir = out_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / "run.log"

    root = logging.getLogger()
    # Default to INFO for third-party libraries so we don't accidentally log extremely verbose internals
    # (e.g., httpcore wire-level debug) during long runs.
    root.setLevel(logging.INFO)

    fmt = logging.Formatter("%(asctime)sZ %(levelname)s %(name)s %(message)s")

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(getattr(logging, console_level))
    console_handler.setFormatter(fmt)

    # Replace any existing handlers.
    root.handlers = [file_handler, console_handler]

    # Our collector logs should be as detailed as possible in the file log.
    logging.getLogger("bsky_fair_collect").setLevel(logging.DEBUG)

    # Keep high-signal request logs, but suppress low-level transport debug.
    logging.getLogger("httpx").setLevel(logging.INFO)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)

    if args.command == "run-all":
        _configure_logging(out_dir=args.out_dir, console_level=args.log_level)

        outputs = OutputPaths.for_out_dir(args.out_dir)
        cfg = AppConfig(
            outputs=outputs,
            hosts=Hosts(appview_host=args.appview_host, relay_host=args.relay_host),
            auth_mode=AuthMode(args.auth_mode),
            run=RunParams(
                rps=args.rps,
                max_retries=args.max_retries,
                posts_per_feed=args.posts_per_feed,
                n_discovery=args.n_discovery,
                n_popular=args.n_popular,
                n_less_known=args.n_less_known,
                index_max_actors=args.index_max_actors,
                starterpack_max_per_query=args.starterpack_max_per_query,
                starterpack_query_limit=args.starterpack_query_limit,
                starterpack_actor_limit=args.starterpack_actor_limit,
            ),
        )
        run_all(cfg)
        return

    if args.command == "backfill":
        _configure_logging(out_dir=args.out_dir, console_level=args.log_level)
        overrides = BackfillOverrides(
            auth_mode=(AuthMode(args.auth_mode) if args.auth_mode is not None else None),
            appview_host=(str(args.appview_host) if args.appview_host else None),
            relay_host=(str(args.relay_host) if args.relay_host else None),
            rps=(float(args.rps) if args.rps is not None else None),
            max_retries=(int(args.max_retries) if args.max_retries is not None else None),
        )
        backfill_and_export(args.out_dir, overrides=overrides, allow_running=bool(args.allow_running))
        return

    if args.command == "postprocess":
        _configure_logging(out_dir=args.out_dir, console_level=args.log_level)
        run_postprocess(
            args.out_dir,
            dest_dir=(Path(args.dest_dir) if args.dest_dir is not None else None),
            overwrite=bool(args.overwrite),
            create_zip=bool(args.zip),
            include_metrics=not bool(args.no_metrics),
            include_impression_labels_flat=bool(args.labels_flat),
            allow_running=bool(args.allow_running),
        )
        return

    raise RuntimeError(f"unhandled command: {args.command}")
