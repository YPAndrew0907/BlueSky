#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from run_dced_gap_metrics import (
    SoftmaxModel,
    fit_context_softmax,
    latent_score,
    softmax,
    total_variation_distance,
    weighted_mean,
)
from run_dced_timing_upgrade import (
    build_observed_exposures,
    build_feed_cluster_seen_posts,
    build_riskset_rows,
    describe_riskset_definition,
    availability_time,
    load_feed_posts,
    load_latest_author_snapshots,
    load_post_info,
    parse_timestamp,
)


@dataclass(frozen=True)
class Config:
    root: Path
    study_id: str
    out_json: Path
    max_age_hours: float
    riskset_mode: str
    early_window_hours: float
    availability_anchor: str
    max_first_monitor_delay_minutes: float | None
    strict_cohort_mode: str


def parse_args() -> Config:
    parser = argparse.ArgumentParser(
        description="Run a DCED gap decomposition with early-trajectory covariates."
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
            "/Volumes/T9/BlueSky/output/analysis_demo_20260320/"
            "dced_trajectory_gap_metrics_micro10_full_24h_1h.json"
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
    parser.add_argument(
        "--early-window-hours",
        type=float,
        default=1.0,
        help="How much of a post's earliest monitored life counts as early trajectory.",
    )
    parser.add_argument(
        "--availability-anchor",
        choices=("record_created_at", "availability_time"),
        default="record_created_at",
        help="Timestamp anchor for age / early-trajectory calculations.",
    )
    parser.add_argument(
        "--max-first-monitor-delay-minutes",
        type=float,
        default=None,
        help=(
            "Optional strict-cohort filter. When set, keep only posts first monitored "
            "within this many minutes of the chosen availability anchor."
        ),
    )
    parser.add_argument(
        "--strict-cohort-mode",
        choices=("row", "context"),
        default="row",
        help=(
            "How to apply the strict first-monitor-delay filter. "
            "'row' keeps only qualifying post rows. "
            "'context' keeps only intact contexts where every post row qualifies."
        ),
    )
    args = parser.parse_args()
    return Config(
        root=args.root,
        study_id=args.study_id,
        out_json=args.out_json,
        max_age_hours=float(args.max_age_hours),
        riskset_mode=str(args.riskset_mode),
        early_window_hours=float(args.early_window_hours),
        availability_anchor=str(args.availability_anchor),
        max_first_monitor_delay_minutes=(
            None
            if args.max_first_monitor_delay_minutes is None
            else float(args.max_first_monitor_delay_minutes)
        ),
        strict_cohort_mode=str(args.strict_cohort_mode),
    )


def post_anchor_time(info: Any, availability_anchor: str) -> datetime | None:
    if availability_anchor == "availability_time":
        return availability_time(info)
    raw = str(getattr(info, "record_created_at", "") or "").strip()
    if not raw:
        return None
    try:
        return parse_timestamp(raw)
    except ValueError:
        return None


def build_early_history(
    study_root: Path,
    post_info: dict[str, Any],
    needed_posts: set[str],
    early_window_hours: float,
    availability_anchor: str,
) -> pd.DataFrame:
    per_window: dict[tuple[str, str], dict[str, float]] = {}
    for path in sorted(study_root.rglob("parts/feed_items_part_000.csv")):
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                post_uri = row["post_uri"]
                if post_uri not in needed_posts:
                    continue
                info = post_info.get(post_uri)
                if info is None:
                    continue
                anchor_time = post_anchor_time(info, availability_anchor)
                if anchor_time is None:
                    continue
                try:
                    captured_at = parse_timestamp(row["captured_at_utc"])
                except ValueError:
                    continue
                age_hours = (captured_at - anchor_time).total_seconds() / 3600.0
                if age_hours < 0 or age_hours > early_window_hours:
                    continue
                key = (post_uri, row["scheduled_window_start_utc"])
                bucket = per_window.setdefault(
                    key,
                    {
                        "exposure": 0.0,
                        "appearances": 0.0,
                        "best_rank_weight": 0.0,
                    },
                )
                weight = 1.0 / math.log2(1 + int(row["rank"]))
                bucket["exposure"] += weight
                bucket["appearances"] += 1.0
                bucket["best_rank_weight"] = max(bucket["best_rank_weight"], weight)
    rows: list[dict[str, Any]] = []
    for (post_uri, scheduled_window_start_utc), metrics in per_window.items():
        rows.append(
            {
                "post_uri": post_uri,
                "window_start_dt": parse_timestamp(scheduled_window_start_utc),
                "early_exposure_in_window": metrics["exposure"],
                "early_appearances_in_window": metrics["appearances"],
                "early_best_rank_weight_in_window": metrics["best_rank_weight"],
            }
        )
    if not rows:
        raise ValueError("no early trajectory rows found")
    history = pd.DataFrame(rows).sort_values(["post_uri", "window_start_dt"]).reset_index(
        drop=True
    )
    history["cum_early_exposure"] = history.groupby("post_uri")[
        "early_exposure_in_window"
    ].cumsum()
    history["cum_early_appearances"] = history.groupby("post_uri")[
        "early_appearances_in_window"
    ].cumsum()
    history["cum_early_best_rank_weight"] = history.groupby("post_uri")[
        "early_best_rank_weight_in_window"
    ].cummax()
    history["cum_early_windows"] = history.groupby("post_uri").cumcount() + 1
    return history


def attach_early_trajectory_features(
    risk_df: pd.DataFrame,
    history: pd.DataFrame,
) -> pd.DataFrame:
    left = risk_df.copy().reset_index(drop=False).rename(columns={"index": "_row_id"})
    left["window_start_dt"] = pd.to_datetime(left["scheduled_window_start_utc"], utc=True)
    right = history.copy()
    right["window_start_dt"] = pd.to_datetime(right["window_start_dt"], utc=True)

    left = left.sort_values(["window_start_dt", "post_uri"]).reset_index(drop=True)
    right = right.sort_values(["window_start_dt", "post_uri"]).reset_index(drop=True)

    merged = pd.merge_asof(
        left,
        right,
        on="window_start_dt",
        by="post_uri",
        direction="backward",
        allow_exact_matches=False,
    )
    for raw_col, out_col in (
        ("cum_early_exposure", "early_prior_exposure_log"),
        ("cum_early_appearances", "early_prior_appearances_log"),
        ("cum_early_windows", "early_prior_windows_log"),
    ):
        merged[out_col] = merged[raw_col].fillna(0.0).map(lambda x: math.log1p(float(x)))
    merged["early_prior_best_rank_weight"] = merged["cum_early_best_rank_weight"].fillna(0.0)
    merged["has_early_trajectory"] = (merged["cum_early_windows"].fillna(0.0) > 0).astype(int)
    restored = merged.sort_values("_row_id").drop(columns=["_row_id", "window_start_dt"])
    return restored.reset_index(drop=True)


def compute_first_monitor_delay_minutes(
    study_root: Path,
    post_info: dict[str, Any],
    needed_posts: set[str],
    availability_anchor: str,
) -> pd.DataFrame:
    first_capture: dict[str, datetime] = {}
    for path in sorted(study_root.rglob("parts/feed_items_part_000.csv")):
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                post_uri = row["post_uri"]
                if post_uri not in needed_posts:
                    continue
                try:
                    captured_at = parse_timestamp(row["captured_at_utc"])
                except ValueError:
                    continue
                existing = first_capture.get(post_uri)
                if existing is None or captured_at < existing:
                    first_capture[post_uri] = captured_at

    rows: list[dict[str, Any]] = []
    for post_uri in needed_posts:
        info = post_info.get(post_uri)
        if info is None:
            continue
        anchor_time = post_anchor_time(info, availability_anchor)
        first_captured_at = first_capture.get(post_uri)
        if anchor_time is None or first_captured_at is None:
            continue
        delay_minutes = max(
            0.0,
            (first_captured_at - anchor_time).total_seconds() / 60.0,
        )
        rows.append(
            {
                "post_uri": post_uri,
                "first_monitored_delay_minutes": delay_minutes,
            }
        )
    if not rows:
        raise ValueError("no first-monitor delay rows found")
    return pd.DataFrame(rows)


def apply_strict_cohort(
    risk_df: pd.DataFrame,
    first_monitor_delay_df: pd.DataFrame,
    max_first_monitor_delay_minutes: float | None,
    strict_cohort_mode: str,
) -> tuple[pd.DataFrame, dict[str, float | int | None]]:
    merged = risk_df.merge(first_monitor_delay_df, on="post_uri", how="left")
    post_count = int(merged["post_uri"].nunique())
    row_count = int(len(merged))
    context_count = int(merged["context_id"].nunique())
    total_y = float(merged["y"].sum())
    summary: dict[str, float | int | None] = {
        "posts_with_delay_measurement": int(
            first_monitor_delay_df["post_uri"].nunique()
        ),
        "posts_before_filter": post_count,
        "rows_before_filter": row_count,
        "contexts_before_filter": context_count,
        "exposure_before_filter": round(total_y, 6),
        "max_first_monitor_delay_minutes": max_first_monitor_delay_minutes,
        "strict_cohort_mode": strict_cohort_mode,
    }
    if max_first_monitor_delay_minutes is None:
        summary.update(
            {
                "posts_after_filter": post_count,
                "rows_after_filter": row_count,
                "contexts_after_filter": context_count,
                "exposure_after_filter": round(total_y, 6),
                "post_retention_share": 1.0,
                "row_retention_share": 1.0,
                "context_retention_share": 1.0,
                "exposure_retention_share": 1.0,
            }
        )
        return merged, summary

    qualifies = (
        merged["first_monitored_delay_minutes"].notna()
        & (merged["first_monitored_delay_minutes"] <= max_first_monitor_delay_minutes)
    )
    merged["_qualifies_strict_cohort"] = qualifies
    if strict_cohort_mode == "row":
        filtered = merged[merged["_qualifies_strict_cohort"]].copy()
    elif strict_cohort_mode == "context":
        context_ok = merged.groupby("context_id")["_qualifies_strict_cohort"].transform("all")
        filtered = merged[context_ok].copy()
    else:
        raise ValueError(f"unsupported strict_cohort_mode: {strict_cohort_mode}")
    filtered = filtered.drop(columns=["_qualifies_strict_cohort"])
    filtered_posts = int(filtered["post_uri"].nunique())
    filtered_rows = int(len(filtered))
    filtered_contexts = int(filtered["context_id"].nunique())
    filtered_y = float(filtered["y"].sum())
    summary.update(
        {
            "posts_after_filter": filtered_posts,
            "rows_after_filter": filtered_rows,
            "contexts_after_filter": filtered_contexts,
            "exposure_after_filter": round(filtered_y, 6),
            "post_retention_share": round(filtered_posts / post_count, 4)
            if post_count
            else None,
            "row_retention_share": round(filtered_rows / row_count, 4)
            if row_count
            else None,
            "context_retention_share": round(filtered_contexts / context_count, 4)
            if context_count
            else None,
            "exposure_retention_share": round(filtered_y / total_y, 4)
            if total_y > 0
            else None,
        }
    )
    return filtered, summary


def compute_context_gaps(
    risk_df: pd.DataFrame,
    models: dict[str, SoftmaxModel],
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for context_id, frame in risk_df.groupby("context_id"):
        total_y = float(frame["y"].sum())
        if total_y <= 0 or len(frame) < 2:
            continue
        q = frame["y"] / total_y
        uniform = pd.Series([1.0 / len(frame)] * len(frame), index=frame.index)
        row: dict[str, Any] = {
            "context_id": int(context_id),
            "context_total_y": total_y,
            "viewer_mode": str(frame["viewer_mode"].iloc[0]),
            "bucket": str(frame["bucket"].iloc[0]),
            "riskset_size": int(len(frame)),
            "shown_count": int((frame["shown"] == 1).sum()),
            "gap_equal": total_variation_distance(q, uniform),
        }
        for name, model in models.items():
            pi = softmax(latent_score(frame, model))
            row[f"gap_{name}"] = total_variation_distance(q, pi)
        rows.append(row)
    return pd.DataFrame(rows)


def summarize_gap_table(context_gap_df: pd.DataFrame) -> dict[str, float]:
    gap_equal = weighted_mean(context_gap_df, "gap_equal")
    gap_timing = weighted_mean(context_gap_df, "gap_timing")
    gap_timing_author = weighted_mean(context_gap_df, "gap_timing_author")
    gap_timing_trajectory = weighted_mean(context_gap_df, "gap_timing_trajectory")
    gap_timing_author_trajectory = weighted_mean(
        context_gap_df,
        "gap_timing_author_trajectory",
    )
    return {
        "weighted_mean_gap_equal": round(gap_equal, 4),
        "weighted_mean_gap_timing": round(gap_timing, 4),
        "weighted_mean_gap_timing_author": round(gap_timing_author, 4),
        "weighted_mean_gap_timing_trajectory": round(gap_timing_trajectory, 4),
        "weighted_mean_gap_timing_author_trajectory": round(
            gap_timing_author_trajectory,
            4,
        ),
        "timing_explained_share": round((gap_equal - gap_timing) / gap_equal, 4),
        "author_extra_after_timing_share": round(
            (gap_timing - gap_timing_author) / gap_equal,
            4,
        ),
        "trajectory_extra_after_timing_share": round(
            (gap_timing - gap_timing_trajectory) / gap_equal,
            4,
        ),
        "trajectory_extra_after_timing_author_share": round(
            (gap_timing_author - gap_timing_author_trajectory) / gap_equal,
            4,
        ),
        "final_unexplained_share": round(gap_timing_author_trajectory / gap_equal, 4),
    }


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
    author_snapshots = load_latest_author_snapshots(config.root / "authors", needed_authors)
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
    needed_posts = set(risk_df["post_uri"].unique())
    first_monitor_delay_df = compute_first_monitor_delay_minutes(
        study_root=study_root,
        post_info=post_info,
        needed_posts=needed_posts,
        availability_anchor=config.availability_anchor,
    )
    risk_df, cohort_summary = apply_strict_cohort(
        risk_df=risk_df,
        first_monitor_delay_df=first_monitor_delay_df,
        max_first_monitor_delay_minutes=config.max_first_monitor_delay_minutes,
        strict_cohort_mode=config.strict_cohort_mode,
    )
    if risk_df.empty:
        raise ValueError("strict cohort removed all rows")
    history = build_early_history(
        study_root=study_root,
        post_info=post_info,
        needed_posts=needed_posts,
        early_window_hours=config.early_window_hours,
        availability_anchor=config.availability_anchor,
    )
    risk_df = attach_early_trajectory_features(risk_df, history)

    timing_columns = [f"age_spline_{idx}" for idx in range(4)]
    author_columns = ["log10_followers", "log10_posts"]
    trajectory_columns = [
        "early_prior_exposure_log",
        "early_prior_appearances_log",
        "early_prior_best_rank_weight",
    ]
    models = {
        "timing": fit_context_softmax(risk_df, timing_columns),
        "timing_author": fit_context_softmax(risk_df, timing_columns + author_columns),
        "timing_trajectory": fit_context_softmax(
            risk_df,
            timing_columns + trajectory_columns,
        ),
        "timing_author_trajectory": fit_context_softmax(
            risk_df,
            timing_columns + author_columns + trajectory_columns,
        ),
    }
    context_gap_df = compute_context_gaps(risk_df, models)
    cohort_summary["contexts_after_filter_ge2"] = int(context_gap_df["context_id"].nunique())

    config.out_json.parent.mkdir(parents=True, exist_ok=True)
    result = {
        "study_id": config.study_id,
        "max_age_hours": config.max_age_hours,
        "riskset_mode": config.riskset_mode,
        "riskset_definition": describe_riskset_definition(
            config.max_age_hours,
            config.riskset_mode,
        ),
        "early_window_hours": config.early_window_hours,
        "availability_anchor": config.availability_anchor,
        "strict_cohort_mode": config.strict_cohort_mode,
        "feed_posts": len(feed_posts),
        "valid_duplicate_clusters": len(valid_clusters),
        "riskset_contexts_ge2": int(context_gap_df["context_id"].nunique()),
        "riskset_rows": int(len(risk_df)),
        "riskset_zero_exposure_share": round(float((risk_df["shown"] == 0).mean()), 4),
        "strict_cohort_summary": cohort_summary,
        "trajectory_feature_coverage": {
            "has_any_early_trajectory_share": round(
                float(risk_df["has_early_trajectory"].mean()),
                4,
            ),
            "nonzero_early_exposure_share": round(
                float((risk_df["early_prior_exposure_log"] > 0).mean()),
                4,
            ),
            "nonzero_early_best_rank_share": round(
                float((risk_df["early_prior_best_rank_weight"] > 0).mean()),
                4,
            ),
        },
        "model_columns": {
            name: list(model.columns) for name, model in models.items()
        },
        "model_params": {
            name: {
                column: round(value, 6)
                for column, value in zip(model.columns, model.params)
            }
            for name, model in models.items()
        },
        "gap_summary": summarize_gap_table(context_gap_df),
    }
    config.out_json.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def main() -> None:
    config = parse_args()
    result = run(config)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
