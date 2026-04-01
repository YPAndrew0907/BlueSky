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
import patsy
import statsmodels.api as sm


URL_RE = re.compile(r"https?://\S+")
EPSILON = 1e-6


@dataclass(frozen=True)
class Config:
    root: Path
    study_id: str
    out_json: Path
    max_age_hours: float
    riskset_mode: str


@dataclass(frozen=True)
class PostInfo:
    cluster_text: str
    author_did: str
    record_created_at: str
    indexed_at: str


@dataclass(frozen=True)
class AuthorSnapshot:
    captured_at_utc: str
    followers_count: int
    follows_count: int
    posts_count: int


def parse_args() -> Config:
    parser = argparse.ArgumentParser(
        description="Run a duplicate-conditioned exposure timing-upgrade model."
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
            "dced_timing_upgrade_micro10_full_24h.json"
        ),
        help="Path to write summary JSON.",
    )
    parser.add_argument(
        "--max-age-hours",
        type=float,
        default=24.0,
        help="Duplicate-local risk-set horizon in hours.",
    )
    parser.add_argument(
        "--riskset-mode",
        choices=("age_window", "ever_seen_in_feed"),
        default="age_window",
        help=(
            "Risk-set construction rule. "
            "'age_window' keeps all duplicate-cluster posts created within max-age-hours. "
            "'ever_seen_in_feed' keeps only duplicate-cluster posts ever observed in the "
            "same feed, ignoring max-age-hours except for future posts."
        ),
    )
    args = parser.parse_args()
    return Config(
        root=args.root,
        study_id=args.study_id,
        out_json=args.out_json,
        max_age_hours=float(args.max_age_hours),
        riskset_mode=str(args.riskset_mode),
    )


def normalize_text(text: str) -> str:
    lowered = text.lower()
    without_urls = URL_RE.sub(" ", lowered)
    return " ".join(without_urls.split())


def parse_timestamp(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def parse_optional_timestamp(value: str) -> datetime | None:
    raw = value.strip()
    if not raw:
        return None
    try:
        return parse_timestamp(raw)
    except ValueError:
        return None


def availability_time(info: PostInfo) -> datetime | None:
    created_at = parse_optional_timestamp(info.record_created_at)
    indexed_at = parse_optional_timestamp(info.indexed_at)
    if created_at is None:
        return indexed_at
    if indexed_at is None:
        return created_at
    return max(created_at, indexed_at)


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
                    indexed_at=row.get("indexed_at") or "",
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


def build_observed_exposures(
    study_root: Path,
    post_info: dict[str, PostInfo],
    valid_clusters: set[str],
) -> dict[tuple[str, str, str, str, str, str, str], dict[str, Any]]:
    observed: dict[tuple[str, str, str, str, str, str, str], dict[str, Any]] = (
        defaultdict(lambda: {"exposure": 0.0})
    )
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
                )
                context = observed[key]
                context["post_exposure"] = context.get("post_exposure", {})
                context["post_exposure"][post_uri] = context["post_exposure"].get(
                    post_uri, 0.0
                ) + 1.0 / math.log2(1 + int(row["rank"]))
    return observed


def build_feed_cluster_seen_posts(
    observed_contexts: dict[tuple[str, str, str, str, str, str, str], dict[str, Any]],
) -> dict[tuple[str, str], set[str]]:
    feed_cluster_seen: dict[tuple[str, str], set[str]] = defaultdict(set)
    for context_key, context in observed_contexts.items():
        _, _, _, _, feed_uri, _, cluster_text = context_key
        for post_uri in context.get("post_exposure", {}):
            feed_cluster_seen[(feed_uri, cluster_text)].add(post_uri)
    return feed_cluster_seen


def build_riskset_rows(
    observed_contexts: dict[tuple[str, str, str, str, str, str, str], dict[str, Any]],
    cluster_posts: dict[str, set[str]],
    post_info: dict[str, PostInfo],
    author_snapshots: dict[str, AuthorSnapshot],
    max_age_hours: float,
    riskset_mode: str = "age_window",
    feed_cluster_seen_posts: dict[tuple[str, str], set[str]] | None = None,
) -> pd.DataFrame:
    cluster_members: dict[str, list[tuple[str, PostInfo]]] = defaultdict(list)
    for post_uri, info in post_info.items():
        if post_uri in cluster_posts[info.cluster_text]:
            cluster_members[info.cluster_text].append((post_uri, info))

    risk_rows: list[dict[str, Any]] = []
    context_id = 0
    for context_key, context in observed_contexts.items():
        (
            scheduled_window_start_utc,
            scheduled_window_end_utc,
            viewer_mode,
            vantage_id,
            feed_uri,
            bucket,
            cluster_text,
        ) = context_key
        window_end = parse_timestamp(scheduled_window_end_utc)
        seen_in_feed = (
            feed_cluster_seen_posts.get((feed_uri, cluster_text), set())
            if feed_cluster_seen_posts is not None
            else set()
        )
        eligible_rows: list[dict[str, Any]] = []
        for post_uri, info in cluster_members[cluster_text]:
            try:
                created_at = parse_timestamp(info.record_created_at)
            except ValueError:
                continue
            age_hours = (window_end - created_at).total_seconds() / 3600.0
            if age_hours < 0:
                continue
            if riskset_mode == "age_window":
                if age_hours > max_age_hours:
                    continue
            elif riskset_mode == "ever_seen_in_feed":
                if post_uri not in seen_in_feed:
                    continue
            else:
                raise ValueError(f"unsupported riskset_mode: {riskset_mode}")
            author = author_snapshots.get(
                info.author_did,
                AuthorSnapshot(
                    captured_at_utc="",
                    followers_count=0,
                    follows_count=0,
                    posts_count=0,
                ),
            )
            eligible_rows.append(
                {
                    "context_id": context_id,
                    "scheduled_window_start_utc": scheduled_window_start_utc,
                    "scheduled_window_end_utc": scheduled_window_end_utc,
                    "viewer_mode": viewer_mode,
                    "vantage_id": vantage_id,
                    "feed_uri": feed_uri,
                    "bucket": bucket,
                    "cluster_text": cluster_text,
                    "post_uri": post_uri,
                    "age_hours": age_hours,
                    "age_log_hours": math.log1p(age_hours),
                    "y": context.get("post_exposure", {}).get(post_uri, 0.0),
                    "shown": 1 if post_uri in context.get("post_exposure", {}) else 0,
                    "log10_followers": math.log10(1 + author.followers_count),
                    "log10_follows": math.log10(1 + author.follows_count),
                    "log10_posts": math.log10(1 + author.posts_count),
                }
            )
        if len(eligible_rows) < 2:
            continue
        risk_rows.extend(eligible_rows)
        context_id += 1

    if not risk_rows:
        raise ValueError("no risk-set rows found")

    risk_df = pd.DataFrame(risk_rows)
    spline_basis = patsy.dmatrix(
        "bs(age_log_hours, df=4, degree=3, include_intercept=False)",
        risk_df,
        return_type="dataframe",
    )
    spline_basis.columns = [f"age_spline_{idx}" for idx in range(spline_basis.shape[1])]
    return pd.concat(
        [risk_df.reset_index(drop=True), spline_basis.reset_index(drop=True)], axis=1
    )


def build_pair_rows(risk_df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    pair_rows: list[dict[str, Any]] = []
    summary: dict[tuple[str, str], dict[str, int]] = defaultdict(
        lambda: {"pairs": 0, "fresher_wins": 0, "higher_followers_wins": 0}
    )
    for context_id, frame in risk_df.groupby("context_id"):
        rows = frame.sort_values("post_uri").to_dict("records")
        total_y = float(frame["y"].sum())
        viewer_mode = str(frame["viewer_mode"].iloc[0])
        bucket = str(frame["bucket"].iloc[0])
        for left, right in combinations(rows, 2):
            row = {
                "viewer_mode": viewer_mode,
                "bucket": bucket,
                "context_total_y": total_y,
                "d_log_share": math.log((left["y"] + EPSILON) / (right["y"] + EPSILON)),
                "win_left": 1 if left["y"] > right["y"] else 0,
                "d_age_hours": left["age_hours"] - right["age_hours"],
                "d_age_log_hours": left["age_log_hours"] - right["age_log_hours"],
                "d_log10_followers": left["log10_followers"] - right["log10_followers"],
                "d_log10_posts": left["log10_posts"] - right["log10_posts"],
            }
            for idx in range(4):
                row[f"d_age_spline_{idx}"] = (
                    left[f"age_spline_{idx}"] - right[f"age_spline_{idx}"]
                )
            pair_rows.append(row)
            winner, loser = (left, right) if left["y"] > right["y"] else (right, left)
            for key in (
                ("all", "all"),
                (viewer_mode, "all"),
                ("all", bucket),
                (viewer_mode, bucket),
            ):
                summary[key]["pairs"] += 1
                if winner["age_hours"] < loser["age_hours"]:
                    summary[key]["fresher_wins"] += 1
                if winner["log10_followers"] > loser["log10_followers"]:
                    summary[key]["higher_followers_wins"] += 1
    return pd.DataFrame(pair_rows), {
        f"{viewer}|{bucket}": {
            "pairs": values["pairs"],
            "fresher_wins_rate": round(values["fresher_wins"] / values["pairs"], 4),
            "higher_followers_wins_rate": round(
                values["higher_followers_wins"] / values["pairs"], 4
            ),
        }
        for (viewer, bucket), values in sorted(summary.items())
        if values["pairs"] > 0
    }


def fit_models(pairs: pd.DataFrame, outcome: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    specs = [
        ("linear_timing", ["d_age_log_hours"]),
        (
            "spline_timing",
            [f"d_age_spline_{idx}" for idx in range(4)],
        ),
        (
            "spline_plus_author",
            [f"d_age_spline_{idx}" for idx in range(4)]
            + ["d_log10_followers", "d_log10_posts"],
        ),
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


def describe_riskset_definition(max_age_hours: float, riskset_mode: str) -> str:
    if riskset_mode == "age_window":
        return (
            "same duplicate cluster, same feed/viewer/window, "
            "eligible if created_at <= window_end and age <= max_age_hours"
        )
    if riskset_mode == "ever_seen_in_feed":
        return (
            "same duplicate cluster, same feed/viewer/window, "
            "eligible if created_at <= window_end and post was ever observed in the same feed"
        )
    raise ValueError(f"unsupported riskset_mode: {riskset_mode}")


def summarize_top_clusters(risk_df: pd.DataFrame) -> list[dict[str, Any]]:
    grouped = (
        risk_df.groupby("cluster_text")
        .agg(
            unique_posts=("post_uri", "nunique"),
            contexts_ge2=("context_id", "nunique"),
            rows=("post_uri", "size"),
            shown_rows=("shown", "sum"),
            total_y=("y", "sum"),
        )
        .reset_index()
    )
    grouped = grouped.sort_values(["rows", "contexts_ge2"], ascending=False).head(10)
    return [
        {
            "cluster_text": str(row["cluster_text"])[:180],
            "unique_posts": int(row["unique_posts"]),
            "contexts_ge2": int(row["contexts_ge2"]),
            "riskset_rows": int(row["rows"]),
            "shown_rows": int(row["shown_rows"]),
            "total_context_exposure": round(float(row["total_y"]), 4),
        }
        for _, row in grouped.iterrows()
    ]


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
    observed_contexts = build_observed_exposures(
        study_root=study_root,
        post_info=post_info,
        valid_clusters=valid_clusters,
    )
    feed_cluster_seen_posts = build_feed_cluster_seen_posts(observed_contexts)
    risk_df = build_riskset_rows(
        observed_contexts=observed_contexts,
        cluster_posts=cluster_posts,
        post_info=post_info,
        author_snapshots=author_snapshots,
        max_age_hours=config.max_age_hours,
        riskset_mode=config.riskset_mode,
        feed_cluster_seen_posts=feed_cluster_seen_posts,
    )
    pair_df, win_rate_summary = build_pair_rows(risk_df)

    return {
        "study_id": config.study_id,
        "max_age_hours": config.max_age_hours,
        "riskset_mode": config.riskset_mode,
        "riskset_definition": describe_riskset_definition(
            config.max_age_hours,
            config.riskset_mode,
        ),
        "feed_posts": len(feed_posts),
        "text_coverage_ratio": round(len(post_info) / len(feed_posts), 4)
        if feed_posts
        else None,
        "valid_duplicate_clusters": len(valid_clusters),
        "author_profile_coverage": round(len(author_snapshots) / len(needed_authors), 4)
        if needed_authors
        else None,
        "observed_duplicate_contexts": len(observed_contexts),
        "riskset_contexts_ge2": int(risk_df["context_id"].nunique()),
        "riskset_rows": int(len(risk_df)),
        "riskset_zero_exposure_share": round(float((risk_df["shown"] == 0).mean()), 4),
        "pair_rows": int(len(pair_df)),
        "win_rate_summary": win_rate_summary,
        "share_gap_models": fit_models(pair_df, "d_log_share"),
        "win_models": fit_models(pair_df, "win_left"),
        "top_clusters_by_riskset_rows": summarize_top_clusters(risk_df),
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
