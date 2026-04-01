#!/usr/bin/env python3
from __future__ import annotations

import argparse
import time
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _parse_hour(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.replace(minute=0, second=0, microsecond=0)


def _to_utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run(cmd: list[str], *, cwd: Path) -> None:
    print("\n$ " + " ".join(cmd))
    subprocess.run(cmd, cwd=str(cwd), check=True)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--project-root", default="/Volumes/T9/BlueSky")
    p.add_argument("--python", default=".venv/bin/python")
    p.add_argument("--out-base", default="/Volumes/T9/BlueSky/data_v2_full")
    p.add_argument("--auth-path", default="/Volumes/T9/BlueSky/auth.env")
    p.add_argument("--accept-language", default="en-US")
    p.add_argument("--vantage-id-unauth", default="unauth_enUS")
    p.add_argument("--vantage-id-auth", default="auth_enUS")
    p.add_argument("--viewer-modes", default="unauth,auth")
    p.add_argument("--hours", type=int, default=10)
    p.add_argument("--start-hour-utc", default="")

    p.add_argument("--snap-posts-per-feed", type=int, default=50)
    p.add_argument("--snap-concurrency", type=int, default=16)
    p.add_argument("--snap-rps", type=float, default=20)
    p.add_argument("--snap-time-budget-minutes", type=float, default=600)

    p.add_argument("--wide-feeds", type=int, default=10000)
    p.add_argument("--wide-posts-per-feed", type=int, default=10)
    p.add_argument("--wide-concurrency", type=int, default=8)
    p.add_argument("--wide-rps", type=float, default=20)
    p.add_argument("--wide-time-budget-minutes", type=float, default=600)
    p.add_argument("--skip-wide", action="store_true")

    p.add_argument("--hydrate-max-authors", type=int, default=50000)
    p.add_argument("--hydrate-batch-size", type=int, default=25)
    p.add_argument("--hydrate-concurrency", type=int, default=8)
    p.add_argument("--hydrate-rps", type=float, default=20)
    p.add_argument("--sleep-seconds", type=float, default=0.0)

    args = p.parse_args()

    if args.hours < 1:
        raise SystemExit("--hours must be >= 1")

    project_root = Path(args.project_root)
    python = str(project_root / args.python)
    out_base = args.out_base

    if args.start_hour_utc:
        start_hour = _parse_hour(args.start_hour_utc)
    else:
        now = datetime.now(timezone.utc)
        start_hour = now.replace(minute=0, second=0, microsecond=0)

    for i in range(args.hours):
        snap_hour = start_hour + timedelta(hours=i)
        snap_hour_str = _to_utc_iso(snap_hour)
        snap_next_str = _to_utc_iso(snap_hour + timedelta(hours=1))

        print(f"[Window {i+1}/{args.hours}] {snap_hour_str} -> {snap_next_str}")

        _run(
            [
                python,
                "-m",
                "bsky_collector_v2",
                "snapshot-panel",
                "--out-base",
                out_base,
                "--env-path",
                args.auth_path,
                "--accept-language",
                args.accept_language,
                "--vantage-id-unauth",
                args.vantage_id_unauth,
                "--vantage-id-auth",
                args.vantage_id_auth,
                "--viewer-modes",
                args.viewer_modes,
                "--posts-per-feed",
                str(args.snap_posts_per_feed),
                "--concurrency",
                str(args.snap_concurrency),
                "--rps",
                str(args.snap_rps),
                "--time-budget-minutes",
                str(args.snap_time_budget_minutes),
                "--snapshot-hour-utc",
                snap_hour_str,
            ],
            cwd=project_root,
        )

        if not args.skip_wide:
            _run(
                [
                    python,
                    "-m",
                    "bsky_collector_v2",
                    "wide-sweep",
                    "--out-base",
                    out_base,
                    "--env-path",
                    args.auth_path,
                    "--accept-language",
                    args.accept_language,
                    "--vantage-id-unauth",
                    args.vantage_id_unauth,
                    "--posts-per-feed",
                    str(args.wide_posts_per_feed),
                    "--n-feeds",
                    str(args.wide_feeds),
                    "--concurrency",
                    str(args.wide_concurrency),
                    "--rps",
                    str(args.wide_rps),
                    "--time-budget-minutes",
                    str(args.wide_time_budget_minutes),
                ],
                cwd=project_root,
            )

        _run(
            [
                python,
                "-m",
                "bsky_collector_v2",
                "hydrate-authors",
                "--out-base",
                out_base,
                "--env-path",
                args.auth_path,
                "--accept-language",
                args.accept_language,
                "--vantage-id-unauth",
                args.vantage_id_unauth,
                "--max-authors",
                str(args.hydrate_max_authors),
                "--batch-size",
                str(args.hydrate_batch_size),
                "--concurrency",
                str(args.hydrate_concurrency),
                "--rps",
                str(args.hydrate_rps),
                "--seen-after-utc",
                snap_hour_str,
                "--seen-before-utc",
                snap_next_str,
            ],
            cwd=project_root,
        )

        if args.sleep_seconds > 0 and i < args.hours - 1:
            time.sleep(args.sleep_seconds)


if __name__ == "__main__":
    main()
