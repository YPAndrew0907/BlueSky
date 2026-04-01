#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _parse_hour(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def _to_utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_minutes(raw: str, *, default_minute: int | None) -> list[int]:
    if raw.strip():
        out = []
        for part in raw.split(","):
            minute = int(part.strip())
            if minute < 0 or minute > 59:
                raise ValueError(f"invalid micro-sweep minute: {minute}")
            out.append(minute)
        return sorted(set(out))
    if default_minute is not None:
        return [int(default_minute)]
    return [0]


def _run(cmd: list[str], *, cwd: Path) -> None:
    print("\n$ " + " ".join(cmd))
    subprocess.run(cmd, cwd=str(cwd), check=True)


def _sleep_until(target_utc: datetime) -> None:
    delay = (target_utc - datetime.now(timezone.utc)).total_seconds()
    if delay > 0:
        time.sleep(delay)


def _default_start_hour(*, fixed_start_minute: int | None) -> datetime:
    now = datetime.now(timezone.utc)
    hour = now.replace(minute=0, second=0, microsecond=0)
    if fixed_start_minute is not None and now.minute > fixed_start_minute:
        hour += timedelta(hours=1)
    return hour


def _snapshot_out_base(*, base_out_base: str, minute: int, minutes: list[int], template: str | None) -> str:
    if len(minutes) == 1:
        return base_out_base
    if template:
        return template.format(minute=f"{minute:02d}")
    return f"{base_out_base}_m{minute:02d}"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--project-root", default="/Volumes/T9/BlueSky")
    p.add_argument("--python", default=".venv/bin/python")
    p.add_argument("--out-base", default="/Volumes/T9/BlueSky/data_v2_full")
    p.add_argument("--micro-out-base-template", default="")
    p.add_argument("--auth-path", default="/Volumes/T9/BlueSky/auth.env")
    p.add_argument("--accept-language", default="en-US")
    p.add_argument("--vantage-id-unauth", default="unauth_enUS")
    p.add_argument("--vantage-id-auth", default="auth_enUS")
    p.add_argument("--viewer-modes", default="unauth,auth")
    p.add_argument("--hours", type=int, default=10)
    p.add_argument("--start-hour-utc", default="")
    p.add_argument("--fixed-start-minute", type=int, default=None)
    p.add_argument("--micro-sweep-minutes", default="")
    p.add_argument("--sleep-until-window", action="store_true")

    p.add_argument("--snap-posts-per-feed", type=int, default=50)
    p.add_argument("--snap-concurrency", type=int, default=16)
    p.add_argument("--snap-rps", type=float, default=20)
    p.add_argument("--snap-time-budget-minutes", type=float, default=55)

    p.add_argument("--wide-feeds", type=int, default=10000)
    p.add_argument("--wide-posts-per-feed", type=int, default=10)
    p.add_argument("--wide-concurrency", type=int, default=8)
    p.add_argument("--wide-rps", type=float, default=20)
    p.add_argument("--wide-time-budget-minutes", type=float, default=55)
    p.add_argument("--skip-wide", action="store_true")

    p.add_argument("--hydrate-max-authors", type=int, default=50000)
    p.add_argument("--hydrate-batch-size", type=int, default=25)
    p.add_argument("--hydrate-concurrency", type=int, default=8)
    p.add_argument("--hydrate-rps", type=float, default=20)
    p.add_argument("--skip-hydrate", action="store_true")
    p.add_argument("--sleep-seconds", type=float, default=0.0)

    args = p.parse_args()

    if args.hours < 1:
        raise SystemExit("--hours must be >= 1")
    if args.fixed_start_minute is not None and not 0 <= args.fixed_start_minute <= 59:
        raise SystemExit("--fixed-start-minute must be within [0, 59]")

    minutes = _parse_minutes(args.micro_sweep_minutes, default_minute=args.fixed_start_minute)
    project_root = Path(args.project_root)
    python = str(project_root / args.python)
    template = args.micro_out_base_template.strip() or None

    if args.start_hour_utc:
        start_hour = _parse_hour(args.start_hour_utc)
    else:
        start_hour = _default_start_hour(fixed_start_minute=args.fixed_start_minute)

    for i in range(args.hours):
        base_hour = start_hour + timedelta(hours=i)
        snap_hour_str = _to_utc_iso(base_hour)
        snap_next_str = _to_utc_iso(base_hour + timedelta(hours=1))
        print(f"[Strict Window {i + 1}/{args.hours}] base_hour={snap_hour_str} micro_minutes={minutes}")

        for minute in minutes:
            target_dt = base_hour + timedelta(minutes=minute)
            if args.sleep_until_window:
                _sleep_until(target_dt)
            snapshot_out_base = _snapshot_out_base(
                base_out_base=args.out_base,
                minute=minute,
                minutes=minutes,
                template=template,
            )
            _run(
                [
                    python,
                    "-m",
                    "bsky_collector_v2",
                    "snapshot-panel",
                    "--out-base",
                    snapshot_out_base,
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

        if not args.skip-wide:
            _run(
                [
                    python,
                    "-m",
                    "bsky_collector_v2",
                    "wide-sweep",
                    "--out-base",
                    args.out_base,
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

        if not args.skip_hydrate:
            _run(
                [
                    python,
                    "-m",
                    "bsky_collector_v2",
                    "hydrate-authors",
                    "--out-base",
                    args.out_base,
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
