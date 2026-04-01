#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import datetime as dt
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import DefaultDict

from dateutil.parser import isoparse


UTC = dt.timezone.utc


@dataclass(frozen=True)
class SnapshotWindow:
    start: dt.datetime
    end: dt.datetime

    def contains(self, t: dt.datetime) -> bool:
        return self.start <= t <= self.end


def _parse_iso_hour(s: str) -> dt.datetime:
    # Accept "YYYY-MM-DDTHH", "YYYY-MM-DDTHH:MM:SSZ", etc.
    raw = s.strip()
    if len(raw) == 13 and raw[10] == "T":
        raw = raw + ":00:00Z"
    t = isoparse(raw)
    if t.tzinfo is None:
        # Treat naive as UTC
        t = t.replace(tzinfo=UTC)
    return t.astimezone(UTC)


def _hour_from_feed_items_path(path: Path) -> dt.datetime:
    # .../hourly/YYYY-MM-DD/HH/feed_items.csv
    hour_dir = path.parent.name
    day_dir = path.parent.parent.name
    return dt.datetime.fromisoformat(f"{day_dir}T{hour_dir}:00:00+00:00").astimezone(UTC)


def _iter_feed_items_files(timeseries_root: Path) -> list[Path]:
    files = list(timeseries_root.glob("*/*/feed_items.csv"))
    files.sort(key=lambda p: p.as_posix())
    return files


def _select_window(files: list[Path], *, start: dt.datetime | None, end: dt.datetime | None, last_days: int | None) -> SnapshotWindow:
    if not files:
        raise ValueError("No feed_items.csv files found under timeseries root")

    hours = [_hour_from_feed_items_path(p) for p in files]
    min_t = min(hours)
    max_t = max(hours)

    if end is None:
        end = max_t
    if start is None and last_days is not None:
        start = end - dt.timedelta(days=last_days)
    if start is None:
        start = min_t

    if start > end:
        raise ValueError(f"Bad window: start {start.isoformat()} > end {end.isoformat()}")

    return SnapshotWindow(start=start, end=end)


def compute_rank_marginals(
    *,
    timeseries_root: Path,
    feed_uri: str,
    viewer_mode: str,
    vantage_id: str,
    k: int,
    window: SnapshotWindow,
) -> tuple[int, Counter[int], Counter[str], dict[str, Counter[int]]]:
    """
    Returns:
      - n_snapshots (hours with at least one matching row)
      - counts_per_rank[j]
      - counts_per_post[post_uri]
      - counts_post_rank[post_uri][j]
    """
    if k <= 0:
        raise ValueError("k must be positive")

    files = _iter_feed_items_files(timeseries_root)

    counts_per_rank: Counter[int] = Counter()
    counts_per_post: Counter[str] = Counter()
    counts_post_rank: DefaultDict[str, Counter[int]] = defaultdict(Counter)

    n_snapshots = 0
    col_idx: dict[str, int] | None = None

    for file_path in files:
        hour = _hour_from_feed_items_path(file_path)
        if not window.contains(hour):
            continue

        found_any = False
        with file_path.open("r", encoding="utf-8", newline="") as f:
            header = f.readline().rstrip("\n")
            if not header:
                continue
            cols = header.split(",")
            if col_idx is None:
                col_idx = {c: i for i, c in enumerate(cols)}
                required = ["viewer_mode", "vantage_id", "feed_uri", "rank", "post_uri"]
                missing = [c for c in required if c not in col_idx]
                if missing:
                    raise ValueError(f"Missing expected columns in {file_path}: {missing}")

            # Fast path: substring check before splitting.
            for line in f:
                if feed_uri not in line:
                    continue
                fields = line.rstrip("\n").split(",")
                if fields[col_idx["feed_uri"]] != feed_uri:
                    continue
                if fields[col_idx["viewer_mode"]] != viewer_mode:
                    continue
                if fields[col_idx["vantage_id"]] != vantage_id:
                    continue

                found_any = True
                try:
                    rank = int(fields[col_idx["rank"]])
                except ValueError:
                    continue
                if rank < 1 or rank > k:
                    continue
                post_uri = fields[col_idx["post_uri"]]
                counts_per_rank[rank] += 1
                counts_per_post[post_uri] += 1
                counts_post_rank[post_uri][rank] += 1

        if found_any:
            n_snapshots += 1

    return n_snapshots, counts_per_rank, counts_per_post, dict(counts_post_rank)


def _write_wide_csv(
    out_path: Path,
    *,
    n_snapshots: int,
    counts_per_rank: Counter[int],
    counts_per_post: Counter[str],
    counts_post_rank: dict[str, Counter[int]],
    k: int,
    top_posts: list[str],
    include_other_row: bool,
) -> None:
    if n_snapshots <= 0:
        raise ValueError("No matching snapshots found (n_snapshots=0)")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    rank_cols = [f"p_rank_{j:02d}" for j in range(1, k + 1)]

    def p(count: int) -> float:
        return count / n_snapshots

    # Precompute per-rank totals (should be ~1.0 if every snapshot has all ranks).
    p_rank_present = {j: p(counts_per_rank.get(j, 0)) for j in range(1, k + 1)}

    with out_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "row_kind",
                "row_id",
                "p_in_topk",
                *rank_cols,
                *[f"rank_present_{j:02d}" for j in range(1, k + 1)],
                "n_snapshots",
            ]
        )

        # Normal rows
        for post_uri in top_posts:
            row = [
                "post",
                post_uri,
                f"{p(counts_per_post.get(post_uri, 0)):.6f}",
            ]
            for j in range(1, k + 1):
                row.append(f"{p(counts_post_rank.get(post_uri, Counter()).get(j, 0)):.6f}")
            for j in range(1, k + 1):
                row.append(f"{p_rank_present[j]:.6f}")
            row.append(str(n_snapshots))
            w.writerow(row)

        if include_other_row:
            row = ["other", "OTHER", ""]
            for j in range(1, k + 1):
                top_mass = sum(counts_post_rank.get(post, Counter()).get(j, 0) for post in top_posts)
                remaining = counts_per_rank.get(j, 0) - top_mass
                row.append(f"{p(max(remaining, 0)):.6f}")
            for j in range(1, k + 1):
                row.append(f"{p_rank_present[j]:.6f}")
            row.append(str(n_snapshots))
            w.writerow(row)


def main() -> None:
    ap = argparse.ArgumentParser(
        description=(
            "Compute empirical rank-marginals P_hat[p,j]=freq_t[rank_t(p)=j] for one feed+vantage over hourly snapshots."
        )
    )
    ap.add_argument("--timeseries-root", type=Path, default=Path("data_v2_full/effective_csv/timeseries/hourly"))
    ap.add_argument("--feed-uri", required=True)
    ap.add_argument("--viewer-mode", required=True, choices=["auth", "unauth"])
    ap.add_argument("--vantage-id", required=True)
    ap.add_argument("--k", type=int, default=13, help="Top-K ranks to include (default: 13).")
    ap.add_argument("--top-rows", type=int, default=12, help="How many top posts to include as rows (default: 12).")
    ap.add_argument("--include-other-row", action="store_true", help="Add an OTHER row per rank (fills remaining mass).")
    ap.add_argument("--start-hour", type=str, default=None, help="UTC hour (inclusive), e.g. 2026-02-25T05:00:00Z")
    ap.add_argument("--end-hour", type=str, default=None, help="UTC hour (inclusive), default: latest hour in archive")
    ap.add_argument("--last-days", type=int, default=None, help="If set, use [end-last_days, end] window")
    ap.add_argument("--out", type=Path, default=Path("_build/p_hat_matrix.csv"))
    args = ap.parse_args()

    start = _parse_iso_hour(args.start_hour) if args.start_hour else None
    end = _parse_iso_hour(args.end_hour) if args.end_hour else None

    window = _select_window(
        _iter_feed_items_files(args.timeseries_root),
        start=start,
        end=end,
        last_days=args.last_days,
    )

    n_snapshots, counts_per_rank, counts_per_post, counts_post_rank = compute_rank_marginals(
        timeseries_root=args.timeseries_root,
        feed_uri=args.feed_uri,
        viewer_mode=args.viewer_mode,
        vantage_id=args.vantage_id,
        k=args.k,
        window=window,
    )

    if n_snapshots <= 0:
        raise SystemExit(
            f"No snapshots matched (feed_uri={args.feed_uri!r}, viewer_mode={args.viewer_mode!r}, vantage_id={args.vantage_id!r})"
        )

    top_posts = [p for p, _ in counts_per_post.most_common(args.top_rows)]
    _write_wide_csv(
        args.out,
        n_snapshots=n_snapshots,
        counts_per_rank=counts_per_rank,
        counts_per_post=counts_per_post,
        counts_post_rank=counts_post_rank,
        k=args.k,
        top_posts=top_posts,
        include_other_row=bool(args.include_other_row),
    )

    start_s = window.start.strftime("%Y-%m-%dT%H:%MZ")
    end_s = window.end.strftime("%Y-%m-%dT%H:%MZ")
    print(
        "\n".join(
            [
                "OK: computed empirical rank marginals",
                f"  feed_uri      : {args.feed_uri}",
                f"  vantage       : viewer_mode={args.viewer_mode} vantage_id={args.vantage_id}",
                f"  window        : {start_s} .. {end_s}",
                f"  snapshots (T) : {n_snapshots}",
                f"  unique posts  : {len(counts_per_post)} (within top-{args.k})",
                f"  wrote         : {args.out}",
            ]
        )
    )


if __name__ == "__main__":
    main()

