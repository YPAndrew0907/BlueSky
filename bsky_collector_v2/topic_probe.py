from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Pattern

from bsky_collector_v2.archive_scan import (
    SURFACE_HOURLY,
    SURFACE_WIDE,
    RunPartition,
    iter_branch_roots,
    iter_partitions,
)


@dataclass(frozen=True)
class CandidatePost:
    post_uri: str
    post_cid: str
    author_did: str
    author_handle: str
    record_created_at: str
    indexed_at: str
    text: str
    matched_queries: tuple[str, ...]
    branch: str
    surface: str
    date_utc: str
    hour_utc: str | None
    viewer_mode: str
    vantage_id: str
    feed_uri: str
    bucket: str

    def to_csv_row(self) -> dict[str, str]:
        row = asdict(self)
        row["matched_queries"] = "|".join(self.matched_queries)
        row["hour_utc"] = self.hour_utc or ""
        return row


@dataclass
class MutablePostExposure:
    exposure_rows: int = 0
    auth_rows: int = 0
    unauth_rows: int = 0
    unique_feeds: set[str] = field(default_factory=set)
    unique_snapshots: set[str] = field(default_factory=set)
    min_rank: int | None = None
    max_rank: int | None = None

    def update(self, *, viewer_mode: str, feed_uri: str, snapshot_key: str, rank: str) -> None:
        self.exposure_rows += 1
        if viewer_mode == "auth":
            self.auth_rows += 1
        elif viewer_mode == "unauth":
            self.unauth_rows += 1
        self.unique_feeds.add(feed_uri)
        self.unique_snapshots.add(snapshot_key)
        try:
            rank_int = int(rank)
        except (TypeError, ValueError):
            return
        if self.min_rank is None or rank_int < self.min_rank:
            self.min_rank = rank_int
        if self.max_rank is None or rank_int > self.max_rank:
            self.max_rank = rank_int


@dataclass
class MutableFeedExposure:
    exposure_rows: int = 0
    unique_posts: set[str] = field(default_factory=set)
    auth_rows: int = 0
    unauth_rows: int = 0

    def update(self, *, post_uri: str, viewer_mode: str) -> None:
        self.exposure_rows += 1
        self.unique_posts.add(post_uri)
        if viewer_mode == "auth":
            self.auth_rows += 1
        elif viewer_mode == "unauth":
            self.unauth_rows += 1


def _compile_queries(queries: list[str], *, required: bool) -> list[Pattern[str]]:
    if not queries:
        if required:
            raise ValueError("at least one --query is required")
        return []
    return [re.compile(q, flags=re.IGNORECASE) for q in queries]


def _matches(
    text: str,
    include_patterns: list[Pattern[str]],
    exclude_patterns: list[Pattern[str]],
) -> tuple[str, ...]:
    if not text:
        return ()
    if any(pattern.search(text) for pattern in exclude_patterns):
        return ()
    matched = [pattern.pattern for pattern in include_patterns if pattern.search(text)]
    return tuple(matched)


def _scan_posts(
    *,
    partitions: list[RunPartition],
    include_patterns: list[Pattern[str]],
    exclude_patterns: list[Pattern[str]],
) -> tuple[dict[str, CandidatePost], Counter[str], Counter[str], Counter[str], Counter[str]]:
    matched_posts: dict[str, CandidatePost] = {}
    matched_query_counts: Counter[str] = Counter()
    matched_posts_by_branch: Counter[str] = Counter()
    matched_posts_by_surface: Counter[str] = Counter()
    matched_posts_by_date: Counter[str] = Counter()
    for partition in partitions:
        for csv_path in sorted(partition.parts_dir.glob("posts_first_seen_part_*.csv")):
            with csv_path.open("r", newline="", encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    matched_queries = _matches(
                        str(row.get("text", "")),
                        include_patterns=include_patterns,
                        exclude_patterns=exclude_patterns,
                    )
                    if not matched_queries:
                        continue
                    post_uri = str(row.get("post_uri", ""))
                    if not post_uri:
                        continue
                    for query in matched_queries:
                        matched_query_counts[query] += 1
                    if post_uri in matched_posts:
                        continue
                    matched_posts[post_uri] = CandidatePost(
                        post_uri=post_uri,
                        post_cid=str(row.get("post_cid", "")),
                        author_did=str(row.get("author_did", "")),
                        author_handle=str(row.get("author_handle", "")),
                        record_created_at=str(row.get("record_created_at", "")),
                        indexed_at=str(row.get("indexed_at", "")),
                        text=str(row.get("text", "")),
                        matched_queries=matched_queries,
                        branch=partition.branch,
                        surface=partition.surface,
                        date_utc=partition.date_utc,
                        hour_utc=partition.hour_utc,
                        viewer_mode=str(row.get("viewer_mode", "")),
                        vantage_id=str(row.get("vantage_id", "")),
                        feed_uri=str(row.get("feed_uri", "")),
                        bucket=str(row.get("bucket", "")),
                    )
                    matched_posts_by_branch[partition.branch] += 1
                    matched_posts_by_surface[partition.surface] += 1
                    matched_posts_by_date[partition.date_utc] += 1
    return (
        matched_posts,
        matched_query_counts,
        matched_posts_by_branch,
        matched_posts_by_surface,
        matched_posts_by_date,
    )


def _write_csv(path: Path, *, fieldnames: list[str], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def run_probe(
    *,
    data_root: Path,
    out_dir: Path,
    queries: list[str],
    exclude_queries: list[str],
    surfaces: list[str],
    start_date: str | None,
    end_date: str | None,
    include_labelerexp: bool,
) -> dict[str, object]:
    include_patterns = _compile_queries(queries, required=True)
    exclude_patterns = _compile_queries(exclude_queries, required=False)
    partitions: list[RunPartition] = []
    for branch, root in iter_branch_roots(data_root, include_labelerexp):
        partitions.extend(
            iter_partitions(
                root=root,
                branch=branch,
                surfaces=surfaces,
                start_date=start_date,
                end_date=end_date,
            )
        )
    partitions.sort(key=lambda p: (p.branch, p.surface, p.date_utc, p.hour_utc or ""))

    (
        matched_posts,
        matched_query_counts,
        matched_posts_by_branch,
        matched_posts_by_surface,
        matched_posts_by_date,
    ) = _scan_posts(
        partitions=partitions,
        include_patterns=include_patterns,
        exclude_patterns=exclude_patterns,
    )
    matched_post_uris = set(matched_posts)

    out_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(
        out_dir / "matched_posts.csv",
        fieldnames=[
            "post_uri",
            "post_cid",
            "author_did",
            "author_handle",
            "record_created_at",
            "indexed_at",
            "text",
            "matched_queries",
            "branch",
            "surface",
            "date_utc",
            "hour_utc",
            "viewer_mode",
            "vantage_id",
            "feed_uri",
            "bucket",
        ],
        rows=[post.to_csv_row() for post in matched_posts.values()],
    )

    matched_feed_handle = (out_dir / "matched_feed_items.csv").open("w", newline="", encoding="utf-8")
    matched_label_handle = (out_dir / "matched_post_labels.csv").open("w", newline="", encoding="utf-8")
    matched_metrics_handle = (out_dir / "matched_post_metrics.csv").open("w", newline="", encoding="utf-8")
    matched_feed_writer = csv.DictWriter(
        matched_feed_handle,
        fieldnames=[
            "branch",
            "surface",
            "date_utc",
            "hour_utc",
            "run_id",
            "snapshot_hour_utc",
            "captured_at_utc",
            "viewer_mode",
            "vantage_id",
            "feed_uri",
            "bucket",
            "rank",
            "post_uri",
            "post_cid",
            "author_did",
            "author_handle",
            "reason_type",
            "reason_actor_did",
        ],
    )
    matched_feed_writer.writeheader()

    matched_label_writer = csv.DictWriter(
        matched_label_handle,
        fieldnames=[
            "branch",
            "surface",
            "date_utc",
            "hour_utc",
            "run_id",
            "snapshot_hour_utc",
            "captured_at_utc",
            "viewer_mode",
            "vantage_id",
            "post_uri",
            "post_cid",
            "label_src",
            "label_val",
            "label_neg",
            "label_uri",
            "label_cts",
        ],
    )
    matched_label_writer.writeheader()

    matched_metrics_writer = csv.DictWriter(
        matched_metrics_handle,
        fieldnames=[
            "branch",
            "surface",
            "date_utc",
            "hour_utc",
            "run_id",
            "snapshot_hour_utc",
            "captured_at_utc",
            "viewer_mode",
            "vantage_id",
            "post_uri",
            "like_count",
            "repost_count",
            "reply_count",
            "quote_count",
        ],
    )
    matched_metrics_writer.writeheader()

    exposure_by_viewer: Counter[str] = Counter()
    exposure_by_bucket: Counter[str] = Counter()
    exposure_by_branch: Counter[str] = Counter()
    exposure_by_surface: Counter[str] = Counter()
    exposure_by_feed: Counter[str] = Counter()
    exposure_by_date: Counter[str] = Counter()
    label_value_counts: Counter[str] = Counter()
    label_source_counts: Counter[str] = Counter()
    matched_posts_with_labels: set[str] = set()
    unique_feeds_seen: set[str] = set()
    unique_snapshots_seen: set[str] = set()
    post_exposure: dict[str, MutablePostExposure] = defaultdict(MutablePostExposure)
    feed_exposure: dict[str, MutableFeedExposure] = defaultdict(MutableFeedExposure)
    matched_feed_rows = 0
    matched_label_rows = 0
    matched_metrics_rows = 0

    try:
        for partition in partitions:
            snapshot_key = partition.snapshot_key
            for csv_path in sorted(partition.parts_dir.glob("feed_items_part_*.csv")):
                with csv_path.open("r", newline="", encoding="utf-8") as fh:
                    reader = csv.DictReader(fh)
                    for row in reader:
                        post_uri = str(row.get("post_uri", ""))
                        if post_uri not in matched_post_uris:
                            continue
                        matched_feed_rows += 1
                        out_row = {
                            "branch": partition.branch,
                            "surface": partition.surface,
                            "date_utc": partition.date_utc,
                            "hour_utc": partition.hour_utc or "",
                            **row,
                        }
                        matched_feed_writer.writerow(out_row)
                        viewer_mode = str(row.get("viewer_mode", ""))
                        bucket = str(row.get("bucket", ""))
                        feed_uri = str(row.get("feed_uri", ""))
                        exposure_by_viewer[viewer_mode] += 1
                        exposure_by_bucket[bucket] += 1
                        exposure_by_branch[partition.branch] += 1
                        exposure_by_surface[partition.surface] += 1
                        exposure_by_feed[feed_uri] += 1
                        exposure_by_date[partition.date_utc] += 1
                        unique_feeds_seen.add(feed_uri)
                        unique_snapshots_seen.add(snapshot_key)
                        post_exposure[post_uri].update(
                            viewer_mode=viewer_mode,
                            feed_uri=feed_uri,
                            snapshot_key=snapshot_key,
                            rank=str(row.get("rank", "")),
                        )
                        feed_exposure[feed_uri].update(post_uri=post_uri, viewer_mode=viewer_mode)

            for csv_path in sorted(partition.parts_dir.glob("post_labels_part_*.csv")):
                with csv_path.open("r", newline="", encoding="utf-8") as fh:
                    reader = csv.DictReader(fh)
                    for row in reader:
                        post_uri = str(row.get("post_uri", ""))
                        if post_uri not in matched_post_uris:
                            continue
                        matched_label_rows += 1
                        matched_label_writer.writerow(
                            {
                                "branch": partition.branch,
                                "surface": partition.surface,
                                "date_utc": partition.date_utc,
                                "hour_utc": partition.hour_utc or "",
                                **row,
                            }
                        )
                        label_value_counts[str(row.get("label_val", ""))] += 1
                        label_source_counts[str(row.get("label_src", ""))] += 1
                        matched_posts_with_labels.add(post_uri)

            for csv_path in sorted(partition.parts_dir.glob("post_metrics_part_*.csv")):
                with csv_path.open("r", newline="", encoding="utf-8") as fh:
                    reader = csv.DictReader(fh)
                    for row in reader:
                        post_uri = str(row.get("post_uri", ""))
                        if post_uri not in matched_post_uris:
                            continue
                        matched_metrics_rows += 1
                        matched_metrics_writer.writerow(
                            {
                                "branch": partition.branch,
                                "surface": partition.surface,
                                "date_utc": partition.date_utc,
                                "hour_utc": partition.hour_utc or "",
                                **row,
                            }
                        )
    finally:
        matched_feed_handle.close()
        matched_label_handle.close()
        matched_metrics_handle.close()

    _write_csv(
        out_dir / "post_exposure_summary.csv",
        fieldnames=[
            "post_uri",
            "exposure_rows",
            "auth_rows",
            "unauth_rows",
            "unique_feeds",
            "unique_snapshots",
            "min_rank",
            "max_rank",
        ],
        rows=[
            {
                "post_uri": post_uri,
                "exposure_rows": summary.exposure_rows,
                "auth_rows": summary.auth_rows,
                "unauth_rows": summary.unauth_rows,
                "unique_feeds": len(summary.unique_feeds),
                "unique_snapshots": len(summary.unique_snapshots),
                "min_rank": summary.min_rank if summary.min_rank is not None else "",
                "max_rank": summary.max_rank if summary.max_rank is not None else "",
            }
            for post_uri, summary in sorted(
                post_exposure.items(),
                key=lambda item: (-item[1].exposure_rows, item[0]),
            )
        ],
    )

    _write_csv(
        out_dir / "feed_exposure_summary.csv",
        fieldnames=[
            "feed_uri",
            "exposure_rows",
            "unique_posts",
            "auth_rows",
            "unauth_rows",
        ],
        rows=[
            {
                "feed_uri": feed_uri,
                "exposure_rows": summary.exposure_rows,
                "unique_posts": len(summary.unique_posts),
                "auth_rows": summary.auth_rows,
                "unauth_rows": summary.unauth_rows,
            }
            for feed_uri, summary in sorted(
                feed_exposure.items(),
                key=lambda item: (-item[1].exposure_rows, item[0]),
            )
        ],
    )

    _write_csv(
        out_dir / "label_summary.csv",
        fieldnames=["kind", "value", "rows"],
        rows=[
            {"kind": "label_val", "value": key, "rows": value}
            for key, value in label_value_counts.most_common()
        ]
        + [
            {"kind": "label_src", "value": key, "rows": value}
            for key, value in label_source_counts.most_common()
        ],
    )

    summary = {
        "queries": queries,
        "exclude_queries": exclude_queries,
        "start_date": start_date,
        "end_date": end_date,
        "include_labelerexp": include_labelerexp,
        "surfaces": surfaces,
        "partitions_scanned": len(partitions),
        "matched_posts": len(matched_posts),
        "matched_posts_by_branch": dict(matched_posts_by_branch),
        "matched_posts_by_surface": dict(matched_posts_by_surface),
        "matched_posts_by_date": dict(matched_posts_by_date),
        "matched_query_hits": dict(matched_query_counts),
        "matched_feed_rows": matched_feed_rows,
        "matched_label_rows": matched_label_rows,
        "matched_metrics_rows": matched_metrics_rows,
        "matched_posts_with_labels": len(matched_posts_with_labels),
        "unique_feeds_seen": len(unique_feeds_seen),
        "unique_snapshots_seen": len(unique_snapshots_seen),
        "exposure_by_viewer": dict(exposure_by_viewer),
        "exposure_by_bucket": dict(exposure_by_bucket),
        "exposure_by_branch": dict(exposure_by_branch),
        "exposure_by_surface": dict(exposure_by_surface),
        "exposure_by_date": dict(exposure_by_date),
        "top_feeds_by_exposure": exposure_by_feed.most_common(25),
        "top_label_values": label_value_counts.most_common(25),
        "top_label_sources": label_source_counts.most_common(25),
    }
    summary_path = out_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Keyword-based topic exposure probe over Bluesky collector outputs."
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=Path("/Volumes/T9/BlueSky/data_v2_full"),
        help="Root directory containing hourly/ wide/ metadata outputs.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        required=True,
        help="Directory for matched posts, matched exposure rows, and JSON summary.",
    )
    parser.add_argument(
        "--query",
        action="append",
        required=True,
        help="Case-insensitive regex query. Pass multiple times to OR multiple topic patterns.",
    )
    parser.add_argument(
        "--exclude-query",
        action="append",
        default=[],
        help="Case-insensitive regex to drop noisy text matches after inclusion.",
    )
    parser.add_argument(
        "--surface",
        action="append",
        default=[SURFACE_HOURLY, SURFACE_WIDE],
        help="Surface(s) to scan. Repeatable. Defaults to hourly + wide.",
    )
    parser.add_argument("--start-date", type=str, default=None, help="Inclusive YYYY-MM-DD lower bound.")
    parser.add_argument("--end-date", type=str, default=None, help="Inclusive YYYY-MM-DD upper bound.")
    parser.add_argument(
        "--include-labelerexp",
        action="store_true",
        help="Also scan data_root/labelerexp with the same surfaces/date filters.",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    surfaces = list(dict.fromkeys(args.surface))
    summary = run_probe(
        data_root=args.data_root,
        out_dir=args.out_dir,
        queries=list(args.query),
        exclude_queries=list(args.exclude_query),
        surfaces=surfaces,
        start_date=args.start_date,
        end_date=args.end_date,
        include_labelerexp=bool(args.include_labelerexp),
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
