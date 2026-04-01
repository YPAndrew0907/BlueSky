#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from run_dced_gap_metrics import SoftmaxModel, latent_score, softmax, weighted_mean
from run_dced_timing_upgrade import (
    build_observed_exposures,
    build_riskset_rows,
    load_feed_posts,
    load_latest_author_snapshots,
    load_post_info,
)
from run_dced_trajectory_gap_metrics import (
    attach_early_trajectory_features,
    build_early_history,
)


@dataclass(frozen=True)
class Config:
    root: Path
    study_id: str
    model_json: Path
    out_json: Path
    max_age_hours: float
    early_window_hours: float


@dataclass(frozen=True)
class Decomposition:
    total: float
    gatekeeping: float
    in_ranking: float


def parse_args() -> Config:
    parser = argparse.ArgumentParser(
        description="Audit whether DCED residuals are mostly gatekeeping or in-ranking."
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
        "--model-json",
        type=Path,
        default=Path(
            "/Volumes/T9/BlueSky/output/analysis_demo_20260320/"
            "dced_trajectory_gap_metrics_micro10_full_24h_1h.json"
        ),
        help="Trajectory-gap result JSON with stored model parameters.",
    )
    parser.add_argument(
        "--out-json",
        type=Path,
        default=Path(
            "/Volumes/T9/BlueSky/output/analysis_demo_20260320/"
            "dced_gatekeeping_audit_micro10_full_24h_1h.json"
        ),
        help="Path to write audit JSON.",
    )
    parser.add_argument(
        "--max-age-hours",
        type=float,
        default=24.0,
        help="Duplicate-local risk-set horizon in hours.",
    )
    parser.add_argument(
        "--early-window-hours",
        type=float,
        default=1.0,
        help="Earliest monitored lifetime used for trajectory features.",
    )
    args = parser.parse_args()
    return Config(
        root=args.root,
        study_id=args.study_id,
        model_json=args.model_json,
        out_json=args.out_json,
        max_age_hours=float(args.max_age_hours),
        early_window_hours=float(args.early_window_hours),
    )


def model_from_result(result: dict[str, Any], name: str) -> SoftmaxModel:
    params = result["model_params"][name]
    return SoftmaxModel(
        columns=tuple(params.keys()),
        params=tuple(float(value) for value in params.values()),
    )


def decompose_tv(q: pd.Series, pi: pd.Series, shown: pd.Series) -> Decomposition:
    shown_mask = shown.astype(bool)
    hidden_mask = ~shown_mask
    gatekeeping = 0.5 * float(pi[hidden_mask].sum())
    in_ranking = 0.5 * float((q[shown_mask] - pi[shown_mask]).abs().sum())
    return Decomposition(
        total=gatekeeping + in_ranking,
        gatekeeping=gatekeeping,
        in_ranking=in_ranking,
    )


def build_risk_dataframe(config: Config) -> pd.DataFrame:
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
    risk_df = build_riskset_rows(
        observed_contexts=observed_contexts,
        cluster_posts=cluster_posts,
        post_info=post_info,
        author_snapshots=author_snapshots,
        max_age_hours=config.max_age_hours,
    )
    needed_posts = set(risk_df["post_uri"].unique())
    history = build_early_history(
        study_root=study_root,
        post_info=post_info,
        needed_posts=needed_posts,
        early_window_hours=config.early_window_hours,
    )
    return attach_early_trajectory_features(risk_df, history)


def compute_context_rows(
    risk_df: pd.DataFrame,
    timing_model: SoftmaxModel,
    final_model: SoftmaxModel,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for context_id, frame in risk_df.groupby("context_id"):
        total_y = float(frame["y"].sum())
        if total_y <= 0 or len(frame) < 2:
            continue
        q = frame["y"] / total_y
        uniform = pd.Series([1.0 / len(frame)] * len(frame), index=frame.index)
        shown = frame["shown"]
        timing_pi = softmax(latent_score(frame, timing_model))
        final_pi = softmax(latent_score(frame, final_model))
        raw = decompose_tv(q, uniform, shown)
        timing = decompose_tv(q, timing_pi, shown)
        final = decompose_tv(q, final_pi, shown)
        riskset_size = int(len(frame))
        shown_count = int(shown.sum())
        zero_exposure_count = riskset_size - shown_count
        rows.append(
            {
                "context_id": int(context_id),
                "viewer_mode": str(frame["viewer_mode"].iloc[0]),
                "bucket": str(frame["bucket"].iloc[0]),
                "cluster_text": str(frame["cluster_text"].iloc[0])[:180],
                "riskset_size": riskset_size,
                "shown_count": shown_count,
                "zero_exposure_count": zero_exposure_count,
                "zero_share": zero_exposure_count / riskset_size,
                "context_total_y": total_y,
                "raw_total": raw.total,
                "raw_gatekeeping": raw.gatekeeping,
                "raw_in_ranking": raw.in_ranking,
                "timing_total": timing.total,
                "timing_gatekeeping": timing.gatekeeping,
                "timing_in_ranking": timing.in_ranking,
                "final_total": final.total,
                "final_gatekeeping": final.gatekeeping,
                "final_in_ranking": final.in_ranking,
            }
        )
    return pd.DataFrame(rows)


def weighted_ratio(frame: pd.DataFrame, numerator: str, denominator: str) -> float:
    denom = weighted_mean(frame, denominator)
    if denom <= 0:
        return float("nan")
    return weighted_mean(frame, numerator) / denom


def summarize_slice(frame: pd.DataFrame, label: str) -> dict[str, Any]:
    if frame.empty:
        return {"label": label, "n": 0}
    total_weight = float(frame["context_total_y"].sum())
    return {
        "label": label,
        "n": int(len(frame)),
        "weight_share": round(total_weight, 4),
        "weighted_zero_share": round(weighted_mean(frame, "zero_share"), 4),
        "raw_total": round(weighted_mean(frame, "raw_total"), 4),
        "raw_gatekeeping": round(weighted_mean(frame, "raw_gatekeeping"), 4),
        "raw_in_ranking": round(weighted_mean(frame, "raw_in_ranking"), 4),
        "raw_gatekeeping_share": round(
            weighted_ratio(frame, "raw_gatekeeping", "raw_total"),
            4,
        ),
        "timing_total": round(weighted_mean(frame, "timing_total"), 4),
        "timing_gatekeeping": round(weighted_mean(frame, "timing_gatekeeping"), 4),
        "timing_in_ranking": round(weighted_mean(frame, "timing_in_ranking"), 4),
        "timing_gatekeeping_share": round(
            weighted_ratio(frame, "timing_gatekeeping", "timing_total"),
            4,
        ),
        "final_total": round(weighted_mean(frame, "final_total"), 4),
        "final_gatekeeping": round(weighted_mean(frame, "final_gatekeeping"), 4),
        "final_in_ranking": round(weighted_mean(frame, "final_in_ranking"), 4),
        "final_gatekeeping_share": round(
            weighted_ratio(frame, "final_gatekeeping", "final_total"),
            4,
        ),
    }


def normalize_weight_shares(summary: dict[str, Any], overall_weight: float) -> dict[str, Any]:
    if "weight_share" in summary and overall_weight > 0:
        summary["weight_share"] = round(float(summary["weight_share"]) / overall_weight, 4)
    return summary


def run(config: Config) -> dict[str, Any]:
    result = json.loads(config.model_json.read_text(encoding="utf-8"))
    timing_model = model_from_result(result, "timing")
    final_model = model_from_result(result, "timing_author_trajectory")
    risk_df = build_risk_dataframe(config)
    context_df = compute_context_rows(risk_df, timing_model, final_model)
    if context_df.empty:
        raise ValueError("no context rows computed")

    overall_weight = float(context_df["context_total_y"].sum())
    slices = {
        "overall": context_df,
        "single_winner": context_df[context_df["shown_count"] == 1],
        "multi_shown": context_df[context_df["shown_count"] >= 2],
        "all_but_one_zero": context_df[
            context_df["zero_exposure_count"] == (context_df["riskset_size"] - 1)
        ],
        "high_zero_share": context_df[context_df["zero_share"] >= 0.75],
        "low_zero_share": context_df[context_df["zero_share"] < 0.75],
        "large_riskset": context_df[context_df["riskset_size"] >= 10],
        "small_riskset": context_df[context_df["riskset_size"] < 10],
    }

    by_viewer = [
        normalize_weight_shares(summarize_slice(frame, f"viewer={viewer_mode}"), overall_weight)
        for viewer_mode, frame in context_df.groupby("viewer_mode")
    ]
    by_bucket = [
        normalize_weight_shares(summarize_slice(frame, f"bucket={bucket}"), overall_weight)
        for bucket, frame in context_df.groupby("bucket")
    ]

    summary = {
        key: normalize_weight_shares(summarize_slice(frame, key), overall_weight)
        for key, frame in slices.items()
    }
    summary["by_viewer"] = by_viewer
    summary["by_bucket"] = by_bucket
    summary["top_final_gatekeeping_contexts"] = context_df.sort_values(
        ["final_gatekeeping", "context_total_y"],
        ascending=[False, False],
    ).head(20).to_dict("records")
    summary["top_final_in_ranking_contexts"] = context_df.sort_values(
        ["final_in_ranking", "context_total_y"],
        ascending=[False, False],
    ).head(20).to_dict("records")

    payload = {
        "study_id": config.study_id,
        "max_age_hours": config.max_age_hours,
        "early_window_hours": config.early_window_hours,
        "source_model_json": str(config.model_json),
        "context_count": int(len(context_df)),
        "summary": summary,
    }
    config.out_json.parent.mkdir(parents=True, exist_ok=True)
    config.out_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def main() -> None:
    config = parse_args()
    payload = run(config)
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
