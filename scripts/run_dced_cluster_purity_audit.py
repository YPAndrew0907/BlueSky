#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from run_dced_gap_metrics import SoftmaxModel, fit_context_softmax, weighted_mean
from run_dced_timing_upgrade import (
    build_observed_exposures,
    build_feed_cluster_seen_posts,
    build_riskset_rows,
    load_feed_posts,
    load_latest_author_snapshots,
    load_post_info,
    parse_timestamp,
)
from run_dced_trajectory_gap_metrics import (
    apply_strict_cohort,
    attach_early_trajectory_features,
    build_early_history,
    compute_context_gaps,
    compute_first_monitor_delay_minutes,
)


@dataclass(frozen=True)
class Config:
    root: Path
    study_id: str
    out_json: Path
    out_csv: Path
    max_age_hours: float
    riskset_mode: str
    early_window_hours: float
    availability_anchor: str
    max_first_monitor_delay_minutes: float | None
    strict_cohort_mode: str
    top_clusters: int
    top_contexts: int
    sample_posts_per_cluster: int


@dataclass(frozen=True)
class RawPostRecord:
    post_uri: str
    author_did: str
    author_handle: str
    record_created_at: str
    indexed_at: str
    raw_text: str
    first_seen_captured_at_utc: str
    first_seen_feed_uri: str
    first_seen_bucket: str


def parse_args() -> Config:
    parser = argparse.ArgumentParser(
        description=(
            "Build a top residual cluster purity audit artifact for the duplicate "
            "exposure study."
        )
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
            "dced_cluster_purity_audit_micro10_full_24h_1h_availability_strict20m.json"
        ),
        help="Path to write the audit JSON summary.",
    )
    parser.add_argument(
        "--out-csv",
        type=Path,
        default=Path(
            "/Volumes/T9/BlueSky/output/analysis_demo_20260320/"
            "dced_cluster_purity_audit_micro10_full_24h_1h_availability_strict20m.csv"
        ),
        help="Path to write the cluster/post audit CSV.",
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
        default="availability_time",
        help="Timestamp anchor for age / early-trajectory calculations.",
    )
    parser.add_argument(
        "--max-first-monitor-delay-minutes",
        type=float,
        default=20.0,
        help=(
            "Optional strict-cohort filter. Default matches the current best "
            "strict20m main specification."
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
    parser.add_argument(
        "--top-clusters",
        type=int,
        default=50,
        help="How many high-residual clusters to include in the audit artifact.",
    )
    parser.add_argument(
        "--top-contexts",
        type=int,
        default=100,
        help="How many high-residual contexts to include in the JSON summary.",
    )
    parser.add_argument(
        "--sample-posts-per-cluster",
        type=int,
        default=8,
        help="How many sample posts to emit per audited cluster in the CSV/JSON.",
    )
    args = parser.parse_args()
    return Config(
        root=args.root,
        study_id=args.study_id,
        out_json=args.out_json,
        out_csv=args.out_csv,
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
        top_clusters=int(args.top_clusters),
        top_contexts=int(args.top_contexts),
        sample_posts_per_cluster=int(args.sample_posts_per_cluster),
    )


def make_post_url(post_uri: str) -> str:
    prefix = "at://"
    if not post_uri.startswith(prefix):
        return ""
    parts = post_uri[len(prefix) :].split("/")
    if len(parts) < 3:
        return ""
    did = parts[0]
    rkey = parts[-1]
    return f"https://bsky.app/profile/{did}/post/{rkey}"


def load_raw_post_records(root: Path, target_post_uris: set[str]) -> dict[str, RawPostRecord]:
    records: dict[str, RawPostRecord] = {}
    if not target_post_uris:
        return records
    for path in sorted(root.rglob("posts_first_seen_part_*.csv")):
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                post_uri = row.get("post_uri") or ""
                if post_uri not in target_post_uris:
                    continue
                captured_at = row.get("captured_at_utc") or ""
                existing = records.get(post_uri)
                if existing is not None and captured_at >= existing.first_seen_captured_at_utc:
                    continue
                records[post_uri] = RawPostRecord(
                    post_uri=post_uri,
                    author_did=row.get("author_did") or "",
                    author_handle=row.get("author_handle") or "",
                    record_created_at=row.get("record_created_at") or "",
                    indexed_at=row.get("indexed_at") or "",
                    raw_text=row.get("text") or "",
                    first_seen_captured_at_utc=captured_at,
                    first_seen_feed_uri=row.get("feed_uri") or "",
                    first_seen_bucket=row.get("bucket") or "",
                )
    return records


def build_models(risk_df: pd.DataFrame) -> dict[str, SoftmaxModel]:
    timing_columns = [f"age_spline_{idx}" for idx in range(4)]
    author_columns = ["log10_followers", "log10_posts"]
    trajectory_columns = [
        "early_prior_exposure_log",
        "early_prior_appearances_log",
        "early_prior_best_rank_weight",
    ]
    return {
        "timing": fit_context_softmax(risk_df, timing_columns),
        "timing_author": fit_context_softmax(risk_df, timing_columns + author_columns),
        "timing_trajectory": fit_context_softmax(risk_df, timing_columns + trajectory_columns),
        "timing_author_trajectory": fit_context_softmax(
            risk_df,
            timing_columns + author_columns + trajectory_columns,
        ),
    }


def build_post_member_frame(
    risk_df: pd.DataFrame,
    first_monitor_delay_df: pd.DataFrame,
) -> pd.DataFrame:
    grouped = (
        risk_df.groupby(["cluster_text", "post_uri"], as_index=False)
        .agg(
            total_exposure=("y", "sum"),
            shown_context_count=("shown", "sum"),
            context_count=("context_id", "nunique"),
            mean_age_hours=("age_hours", "mean"),
            min_age_hours=("age_hours", "min"),
            max_age_hours=("age_hours", "max"),
        )
        .sort_values(
            ["cluster_text", "shown_context_count", "total_exposure", "post_uri"],
            ascending=[True, False, False, True],
        )
        .reset_index(drop=True)
    )
    return grouped.merge(first_monitor_delay_df, on="post_uri", how="left")


def summarize_cluster_contexts(
    context_gap_df: pd.DataFrame,
    risk_df: pd.DataFrame,
    post_info: dict[str, Any],
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for cluster_text, frame in context_gap_df.groupby("cluster_text"):
        risk_slice = risk_df[risk_df["cluster_text"] == cluster_text]
        if risk_slice.empty:
            continue
        worst_context = frame.sort_values(
            ["gap_timing_author_trajectory", "context_total_y"],
            ascending=[False, False],
        ).iloc[0]
        rows.append(
            {
                "cluster_text": cluster_text,
                "cluster_context_count": int(frame["context_id"].nunique()),
                "cluster_unique_posts": int(risk_slice["post_uri"].nunique()),
                "cluster_unique_authors": int(
                    len({post_info[post_uri].author_did for post_uri in risk_slice["post_uri"].unique()})
                ),
                "cluster_total_context_exposure": round(float(frame["context_total_y"].sum()), 6),
                "weighted_mean_gap_equal": round(weighted_mean(frame, "gap_equal"), 6),
                "weighted_mean_gap_timing_author_trajectory": round(
                    weighted_mean(frame, "gap_timing_author_trajectory"),
                    6,
                ),
                "max_gap_timing_author_trajectory": round(
                    float(frame["gap_timing_author_trajectory"].max()),
                    6,
                ),
                "mean_riskset_size": round(float(frame["riskset_size"].mean()), 4),
                "max_riskset_size": int(frame["riskset_size"].max()),
                "mean_shown_count": round(float(frame["shown_count"].mean()), 4),
                "mean_zero_share": round(
                    float(((frame["riskset_size"] - frame["shown_count"]) / frame["riskset_size"]).mean()),
                    6,
                ),
                "max_zero_share": round(
                    float(((frame["riskset_size"] - frame["shown_count"]) / frame["riskset_size"]).max()),
                    6,
                ),
                "worst_context_id": int(worst_context["context_id"]),
                "worst_context_gap_timing_author_trajectory": round(
                    float(worst_context["gap_timing_author_trajectory"]),
                    6,
                ),
                "worst_context_total_y": round(float(worst_context["context_total_y"]), 6),
                "worst_context_riskset_size": int(worst_context["riskset_size"]),
                "worst_context_shown_count": int(worst_context["shown_count"]),
                "worst_context_viewer_mode": str(worst_context["viewer_mode"]),
                "worst_context_bucket": str(worst_context["bucket"]),
                "worst_context_feed_uri": str(worst_context["feed_uri"]),
                "worst_context_window_start_utc": str(worst_context["scheduled_window_start_utc"]),
                "worst_context_window_end_utc": str(worst_context["scheduled_window_end_utc"]),
            }
        )
    return pd.DataFrame(rows).sort_values(
        [
            "weighted_mean_gap_timing_author_trajectory",
            "max_gap_timing_author_trajectory",
            "cluster_total_context_exposure",
        ],
        ascending=[False, False, False],
    ).reset_index(drop=True)


def build_top_context_records(
    context_gap_df: pd.DataFrame,
    risk_df: pd.DataFrame,
    raw_posts: dict[str, RawPostRecord],
    top_contexts: int,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    top_frame = context_gap_df.sort_values(
        ["gap_timing_author_trajectory", "context_total_y"],
        ascending=[False, False],
    ).head(top_contexts)
    for rank, (_, row) in enumerate(top_frame.iterrows(), start=1):
        context_rows = risk_df[risk_df["context_id"] == int(row["context_id"])].copy()
        context_rows = context_rows.sort_values(
            ["shown", "y", "post_uri"],
            ascending=[False, False, True],
        )
        post_samples: list[dict[str, Any]] = []
        for _, post_row in context_rows.head(8).iterrows():
            raw = raw_posts.get(str(post_row["post_uri"]))
            post_samples.append(
                {
                    "post_uri": str(post_row["post_uri"]),
                    "post_url": make_post_url(str(post_row["post_uri"])),
                    "author_handle": "" if raw is None else raw.author_handle,
                    "record_created_at": "" if raw is None else raw.record_created_at,
                    "shown": int(post_row["shown"]),
                    "context_exposure": round(float(post_row["y"]), 6),
                    "age_hours": round(float(post_row["age_hours"]), 6),
                    "raw_text": "" if raw is None else raw.raw_text,
                }
            )
        records.append(
            {
                "context_rank": rank,
                "context_id": int(row["context_id"]),
                "cluster_text": str(row["cluster_text"]),
                "viewer_mode": str(row["viewer_mode"]),
                "bucket": str(row["bucket"]),
                "feed_uri": str(row["feed_uri"]),
                "scheduled_window_start_utc": str(row["scheduled_window_start_utc"]),
                "scheduled_window_end_utc": str(row["scheduled_window_end_utc"]),
                "riskset_size": int(row["riskset_size"]),
                "shown_count": int(row["shown_count"]),
                "context_total_y": round(float(row["context_total_y"]), 6),
                "gap_equal": round(float(row["gap_equal"]), 6),
                "gap_timing_author_trajectory": round(
                    float(row["gap_timing_author_trajectory"]),
                    6,
                ),
                "sample_posts": post_samples,
            }
        )
    return records


def attach_context_metadata(risk_df: pd.DataFrame, context_gap_df: pd.DataFrame) -> pd.DataFrame:
    context_meta = (
        risk_df.groupby("context_id", as_index=False)
        .agg(
            scheduled_window_start_utc=("scheduled_window_start_utc", "first"),
            scheduled_window_end_utc=("scheduled_window_end_utc", "first"),
            viewer_mode_meta=("viewer_mode", "first"),
            vantage_id=("vantage_id", "first"),
            feed_uri=("feed_uri", "first"),
            bucket_meta=("bucket", "first"),
            cluster_text=("cluster_text", "first"),
        )
    )
    merged = context_gap_df.merge(context_meta, on="context_id", how="left")
    if "viewer_mode_meta" in merged.columns:
        merged["viewer_mode"] = merged["viewer_mode_meta"].combine_first(
            merged.get("viewer_mode")
        )
        merged = merged.drop(columns=["viewer_mode_meta"])
    if "bucket_meta" in merged.columns:
        merged["bucket"] = merged["bucket_meta"].combine_first(merged.get("bucket"))
        merged = merged.drop(columns=["bucket_meta"])
    return merged


def build_cluster_audit_rows(
    cluster_summary_df: pd.DataFrame,
    post_member_df: pd.DataFrame,
    raw_posts: dict[str, RawPostRecord],
    sample_posts_per_cluster: int,
) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    csv_rows: list[dict[str, Any]] = []
    json_clusters: list[dict[str, Any]] = []
    for cluster_rank, (_, cluster_row) in enumerate(cluster_summary_df.iterrows(), start=1):
        cluster_text = str(cluster_row["cluster_text"])
        post_frame = post_member_df[post_member_df["cluster_text"] == cluster_text].copy()
        post_frame = post_frame.sort_values(
            ["shown_context_count", "total_exposure", "post_uri"],
            ascending=[False, False, True],
        )
        sample_posts: list[dict[str, Any]] = []
        raw_texts: set[str] = set()
        author_handles: set[str] = set()
        for sample_rank, (_, post_row) in enumerate(
            post_frame.head(sample_posts_per_cluster).iterrows(),
            start=1,
        ):
            post_uri = str(post_row["post_uri"])
            raw = raw_posts.get(post_uri)
            raw_text = "" if raw is None else raw.raw_text
            if raw_text:
                raw_texts.add(raw_text)
            if raw is not None and raw.author_handle:
                author_handles.add(raw.author_handle)
            sample_post = {
                "sample_rank": sample_rank,
                "post_uri": post_uri,
                "post_url": make_post_url(post_uri),
                "author_did": "" if raw is None else raw.author_did,
                "author_handle": "" if raw is None else raw.author_handle,
                "record_created_at": "" if raw is None else raw.record_created_at,
                "indexed_at": "" if raw is None else raw.indexed_at,
                "first_seen_captured_at_utc": "" if raw is None else raw.first_seen_captured_at_utc,
                "first_seen_feed_uri": "" if raw is None else raw.first_seen_feed_uri,
                "first_seen_bucket": "" if raw is None else raw.first_seen_bucket,
                "raw_text": raw_text,
                "total_exposure": round(float(post_row["total_exposure"]), 6),
                "shown_context_count": int(post_row["shown_context_count"]),
                "context_count": int(post_row["context_count"]),
                "first_monitored_delay_minutes": (
                    None
                    if pd.isna(post_row["first_monitored_delay_minutes"])
                    else round(float(post_row["first_monitored_delay_minutes"]), 4)
                ),
                "mean_age_hours": round(float(post_row["mean_age_hours"]), 6),
                "min_age_hours": round(float(post_row["min_age_hours"]), 6),
                "max_age_hours": round(float(post_row["max_age_hours"]), 6),
            }
            sample_posts.append(sample_post)
            csv_rows.append(
                {
                    "cluster_rank": cluster_rank,
                    **cluster_row.to_dict(),
                    **sample_post,
                }
            )
        json_clusters.append(
            {
                "cluster_rank": cluster_rank,
                **cluster_row.to_dict(),
                "sample_author_handles": sorted(author_handles),
                "sample_raw_texts": sorted(raw_texts)[:5],
                "raw_text_variant_count_in_samples": len(raw_texts),
                "sample_posts": sample_posts,
            }
        )
    return pd.DataFrame(csv_rows), json_clusters


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
        needed_posts=set(risk_df["post_uri"].unique()),
        early_window_hours=config.early_window_hours,
        availability_anchor=config.availability_anchor,
    )
    risk_df = attach_early_trajectory_features(risk_df, history)
    models = build_models(risk_df)
    context_gap_df = compute_context_gaps(risk_df, models)
    if context_gap_df.empty:
        raise ValueError("no context gaps computed")
    context_gap_df = attach_context_metadata(risk_df, context_gap_df)

    cluster_summary_df = summarize_cluster_contexts(
        context_gap_df=context_gap_df,
        risk_df=risk_df,
        post_info=post_info,
    ).head(config.top_clusters)
    top_context_df = context_gap_df.sort_values(
        ["gap_timing_author_trajectory", "context_total_y"],
        ascending=[False, False],
    ).head(config.top_contexts)

    target_post_uris = set(
        risk_df[risk_df["cluster_text"].isin(cluster_summary_df["cluster_text"])]["post_uri"].unique()
    )
    target_post_uris.update(
        risk_df[risk_df["context_id"].isin(top_context_df["context_id"])]["post_uri"].unique()
    )
    raw_posts = load_raw_post_records(config.root, target_post_uris)
    post_member_df = build_post_member_frame(risk_df, first_monitor_delay_df)
    audit_csv_df, audit_clusters = build_cluster_audit_rows(
        cluster_summary_df=cluster_summary_df,
        post_member_df=post_member_df,
        raw_posts=raw_posts,
        sample_posts_per_cluster=config.sample_posts_per_cluster,
    )
    top_context_records = build_top_context_records(
        context_gap_df=context_gap_df,
        risk_df=risk_df,
        raw_posts=raw_posts,
        top_contexts=config.top_contexts,
    )

    config.out_json.parent.mkdir(parents=True, exist_ok=True)
    config.out_csv.parent.mkdir(parents=True, exist_ok=True)
    audit_csv_df.to_csv(config.out_csv, index=False)

    result = {
        "study_id": config.study_id,
        "max_age_hours": config.max_age_hours,
        "riskset_mode": config.riskset_mode,
        "early_window_hours": config.early_window_hours,
        "availability_anchor": config.availability_anchor,
        "strict_cohort_mode": config.strict_cohort_mode,
        "feed_posts": len(feed_posts),
        "valid_duplicate_clusters": len(valid_clusters),
        "riskset_contexts_ge2": int(context_gap_df["context_id"].nunique()),
        "riskset_rows": int(len(risk_df)),
        "riskset_zero_exposure_share": round(float((risk_df["shown"] == 0).mean()), 4),
        "strict_cohort_summary": cohort_summary,
        "model_columns": {name: list(model.columns) for name, model in models.items()},
        "model_params": {
            name: {
                column: round(value, 6)
                for column, value in zip(model.columns, model.params)
            }
            for name, model in models.items()
        },
        "gap_summary": {
            key: round(float(value), 4)
            for key, value in {
                "weighted_mean_gap_equal": weighted_mean(context_gap_df, "gap_equal"),
                "weighted_mean_gap_timing_author_trajectory": weighted_mean(
                    context_gap_df,
                    "gap_timing_author_trajectory",
                ),
            }.items()
        },
        "audit_counts": {
            "top_clusters": int(len(cluster_summary_df)),
            "top_contexts": int(len(top_context_records)),
            "sample_posts_per_cluster": config.sample_posts_per_cluster,
            "raw_post_records_loaded": int(len(raw_posts)),
        },
        "top_clusters_by_final_residual": audit_clusters,
        "top_contexts_by_final_residual": top_context_records,
    }
    config.out_json.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def main() -> None:
    config = parse_args()
    result = run(config)
    print(json.dumps(result["audit_counts"], indent=2))


if __name__ == "__main__":
    main()
