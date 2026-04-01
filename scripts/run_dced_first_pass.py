#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path
from typing import Any

import pandas as pd
import statsmodels.api as sm


URL_RE = re.compile(r"https?://\S+")
EPSILON = 1e-6


@dataclass(frozen=True)
class Config:
    root: Path
    study_id: str
    out_json: Path


@dataclass(frozen=True)
class PostInfo:
    cluster_text: str
    author_did: str
    record_created_at: str


@dataclass(frozen=True)
class AuthorSnapshot:
    captured_at_utc: str
    followers_count: int
    follows_count: int
    posts_count: int


@dataclass(frozen=True)
class ContextPost:
    post_uri: str
    exposure: float
    age_hours: float
    log10_followers: float
    log10_follows: float
    log10_posts: float


def parse_args() -> Config:
    parser = argparse.ArgumentParser(
        description="Run a first-pass duplicate-conditioned exposure model."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("/Volumes/T9/BlueSky/data_v2_full"),
        help="Collector out-base root.",
    )
    parser.add_argument(
        "--study-id",
        default="micro10_full_live_20260319",
        help="Study id under data_v2_full/micro5.",
    )
    parser.add_argument(
        "--out-json",
        type=Path,
        default=Path(
            "/Volumes/T9/BlueSky/output/analysis_demo_20260319/"
            "dced_first_pass_micro10_full.json"
        ),
        help="Path to write summary JSON.",
    )
    args = parser.parse_args()
    return Config(root=args.root, study_id=args.study_id, out_json=args.out_json)


def normalize_text(text: str) -> str:
    lowered = text.lower()
    without_urls = URL_RE.sub(" ", lowered)
    return " ".join(without_urls.split())


def parse_timestamp(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def load_feed_posts(study_root: Path) -> set[str]:
    post_uris: set[str] = set()
    for path in sorted(study_root.rglob("parts/feed_items_part_000.csv")):
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                post_uris.add(row["post_uri"])
    return post_uris


def load_post_info(
    root: Path, feed_posts: set[str]
) -> tuple[dict[str, PostInfo], dict[str, set[str]], dict[str, set[str]]]:
    post_info: dict[str, PostInfo] = {}
    cluster_posts: dict[str, set[str]] = defaultdict(set)
    cluster_authors: dict[str, set[str]] = defaultdict(set)
    for path in sorted(root.rglob("posts_first_seen_part_*.csv")):
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                post_uri = row["post_uri"]
                if post_uri not in feed_posts or post_uri in post_info:
                    continue
                cluster_text = normalize_text(row.get("text") or "")
                if not cluster_text:
                    continue
                author_did = row.get("author_did") or ""
                info = PostInfo(
                    cluster_text=cluster_text,
                    author_did=author_did,
                    record_created_at=row.get("record_created_at") or "",
                )
                post_info[post_uri] = info
                cluster_posts[cluster_text].add(post_uri)
                if author_did:
                    cluster_authors[cluster_text].add(author_did)
    return post_info, cluster_posts, cluster_authors


def load_latest_author_snapshots(
    authors_root: Path, needed_authors: set[str]
) -> dict[str, AuthorSnapshot]:
    snapshots: dict[str, AuthorSnapshot] = {}
    for path in sorted(authors_root.rglob("author_profiles_part_000.csv")):
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                author_did = row["author_did"]
                if author_did not in needed_authors:
                    continue
                captured_at_utc = row.get("captured_at_utc") or ""
                existing = snapshots.get(author_did)
                if existing is not None and captured_at_utc <= existing.captured_at_utc:
                    continue
                snapshots[author_did] = AuthorSnapshot(
                    captured_at_utc=captured_at_utc,
                    followers_count=int(row.get("followers_count") or 0),
                    follows_count=int(row.get("follows_count") or 0),
                    posts_count=int(row.get("posts_count") or 0),
                )
    return snapshots


def build_context_posts(
    study_root: Path,
    post_info: dict[str, PostInfo],
    valid_clusters: set[str],
    author_snapshots: dict[str, AuthorSnapshot],
) -> dict[tuple[str, str, str, str, str, str], list[ContextPost]]:
    raw_agg: dict[
        tuple[str, str, str, str, str, str, str, str], dict[str, Any]
    ] = defaultdict(lambda: {"exposure": 0.0})
    for path in sorted(study_root.rglob("parts/feed_items_part_000.csv")):
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                post_uri = row["post_uri"]
                info = post_info.get(post_uri)
                if info is None or info.cluster_text not in valid_clusters:
                    continue
                key = (
                    row["scheduled_window_start_utc"],
                    row["scheduled_window_end_utc"],
                    row["viewer_mode"],
                    row["vantage_id"],
                    row["feed_uri"],
                    row["bucket"],
                    info.cluster_text,
                    post_uri,
                )
                raw_agg[key]["exposure"] += 1.0 / math.log2(1 + int(row["rank"]))
                raw_agg[key]["author_did"] = info.author_did
                raw_agg[key]["record_created_at"] = info.record_created_at

    by_context: dict[tuple[str, str, str, str, str, str], list[ContextPost]] = (
        defaultdict(list)
    )
    for key, value in raw_agg.items():
        (
            scheduled_window_start_utc,
            scheduled_window_end_utc,
            viewer_mode,
            vantage_id,
            feed_uri,
            bucket,
            cluster_text,
            post_uri,
        ) = key
        try:
            age_hours = (
                parse_timestamp(scheduled_window_end_utc)
                - parse_timestamp(str(value["record_created_at"]))
            ).total_seconds() / 3600.0
        except ValueError:
            continue
        author = author_snapshots.get(
            str(value["author_did"]),
            AuthorSnapshot(
                captured_at_utc="",
                followers_count=0,
                follows_count=0,
                posts_count=0,
            ),
        )
        context_key = (
            scheduled_window_start_utc,
            viewer_mode,
            vantage_id,
            feed_uri,
            bucket,
            cluster_text,
        )
        by_context[context_key].append(
            ContextPost(
                post_uri=post_uri,
                exposure=float(value["exposure"]),
                age_hours=age_hours,
                log10_followers=math.log10(1 + author.followers_count),
                log10_follows=math.log10(1 + author.follows_count),
                log10_posts=math.log10(1 + author.posts_count),
            )
        )
    return by_context


def fit_weighted_models(
    pairs: pd.DataFrame, outcome: str
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    specs = [
        ("timing_only", ["d_age_hours"]),
        ("timing_plus_author", ["d_age_hours", "d_log10_followers", "d_log10_posts"]),
    ]
    for viewer_mode in ("all", "unauth", "auth"):
        frame = pairs if viewer_mode == "all" else pairs[pairs["viewer_mode"] == viewer_mode]
        if len(frame) < 50:
            continue
        for name, columns in specs:
            design = sm.add_constant(frame[columns])
            model = sm.WLS(
                frame[outcome], design, weights=frame["context_total_y"]
            ).fit(cov_type="HC1")
            result[f"{viewer_mode}:{name}"] = {
                "n": int(len(frame)),
                "r2": round(float(model.rsquared), 4),
                "params": {
                    key: round(float(value), 6) for key, value in model.params.items()
                },
                "pvalues": {
                    key: round(float(value), 6) for key, value in model.pvalues.items()
                },
            }
    return result


def run(config: Config) -> dict[str, Any]:
    study_root = config.root / "micro5" / config.study_id / "micro5_core_full"
    if not study_root.exists():
        raise FileNotFoundError(f"study root not found: {study_root}")

    feed_posts = load_feed_posts(study_root)
    post_info, cluster_posts, cluster_authors = load_post_info(config.root, feed_posts)
    valid_clusters = {
        cluster_text
        for cluster_text, posts in cluster_posts.items()
        if len(posts) >= 2 and len(cluster_authors[cluster_text]) >= 2
    }
    needed_authors = {
        info.author_did
        for info in post_info.values()
        if info.cluster_text in valid_clusters and info.author_did
    }
    author_snapshots = load_latest_author_snapshots(
        config.root / "authors", needed_authors
    )
    by_context = build_context_posts(
        study_root=study_root,
        post_info=post_info,
        valid_clusters=valid_clusters,
        author_snapshots=author_snapshots,
    )

    pair_rows: list[dict[str, Any]] = []
    summary: dict[tuple[str, str], dict[str, int]] = defaultdict(
        lambda: {"pairs": 0, "fresher_wins": 0, "higher_followers_wins": 0}
    )
    cluster_contexts: dict[str, int] = defaultdict(int)
    cluster_pairs: dict[str, int] = defaultdict(int)
    cluster_exposure: dict[str, float] = defaultdict(float)

    contexts_ge2 = 0
    for context_key, rows in by_context.items():
        if len(rows) < 2:
            continue
        contexts_ge2 += 1
        _, viewer_mode, _, _, bucket, cluster_text = context_key
        total_y = sum(row.exposure for row in rows)
        cluster_contexts[cluster_text] += 1
        cluster_pairs[cluster_text] += math.comb(len(rows), 2)
        cluster_exposure[cluster_text] += total_y
        ordered_rows = sorted(rows, key=lambda row: row.post_uri)
        for left, right in combinations(ordered_rows, 2):
            pair_rows.append(
                {
                    "viewer_mode": viewer_mode,
                    "bucket": bucket,
                    "d_log_share": math.log(
                        (left.exposure + EPSILON) / (right.exposure + EPSILON)
                    ),
                    "win_left": 1 if left.exposure > right.exposure else 0,
                    "d_age_hours": left.age_hours - right.age_hours,
                    "d_log10_followers": left.log10_followers - right.log10_followers,
                    "d_log10_posts": left.log10_posts - right.log10_posts,
                    "context_total_y": total_y,
                }
            )
            winner, loser = (
                (left, right) if left.exposure > right.exposure else (right, left)
            )
            for key in (
                ("all", "all"),
                (viewer_mode, "all"),
                ("all", bucket),
                (viewer_mode, bucket),
            ):
                summary[key]["pairs"] += 1
                if winner.age_hours < loser.age_hours:
                    summary[key]["fresher_wins"] += 1
                if winner.log10_followers > loser.log10_followers:
                    summary[key]["higher_followers_wins"] += 1

    pairs = pd.DataFrame(pair_rows)
    top_clusters = sorted(
        valid_clusters,
        key=lambda cluster_text: (cluster_pairs[cluster_text], cluster_contexts[cluster_text]),
        reverse=True,
    )[:10]

    return {
        "study_id": config.study_id,
        "feed_posts": len(feed_posts),
        "text_coverage_ratio": round(len(post_info) / len(feed_posts), 4)
        if feed_posts
        else None,
        "valid_duplicate_clusters": len(valid_clusters),
        "author_profile_coverage": round(len(author_snapshots) / len(needed_authors), 4)
        if needed_authors
        else None,
        "contexts_ge2": contexts_ge2,
        "pair_rows": int(len(pairs)),
        "win_rate_summary": {
            f"{viewer}|{bucket}": {
                "pairs": values["pairs"],
                "fresher_wins_rate": round(
                    values["fresher_wins"] / values["pairs"], 4
                ),
                "higher_followers_wins_rate": round(
                    values["higher_followers_wins"] / values["pairs"], 4
                ),
            }
            for (viewer, bucket), values in sorted(summary.items())
            if values["pairs"] > 0
        },
        "share_gap_models": fit_weighted_models(pairs, "d_log_share"),
        "win_models": fit_weighted_models(pairs, "win_left"),
        "top_clusters_by_pair_rows": [
            {
                "cluster_text": cluster_text[:180],
                "unique_posts": len(cluster_posts[cluster_text]),
                "unique_authors": len(cluster_authors[cluster_text]),
                "contexts_ge2": cluster_contexts[cluster_text],
                "pair_rows": cluster_pairs[cluster_text],
                "total_context_exposure": round(cluster_exposure[cluster_text], 4),
            }
            for cluster_text in top_clusters
        ],
    }


def main() -> None:
    config = parse_args()
    result = run(config)
    config.out_json.parent.mkdir(parents=True, exist_ok=True)
    config.out_json.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
