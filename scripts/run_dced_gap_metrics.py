#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy.optimize import minimize

from run_dced_timing_upgrade import (
    build_observed_exposures,
    build_pair_rows,
    build_riskset_rows,
    load_feed_posts,
    load_latest_author_snapshots,
    load_post_info,
)


@dataclass(frozen=True)
class Config:
    root: Path
    study_id: str
    out_json: Path
    out_csv: Path
    max_age_hours: float


@dataclass(frozen=True)
class SoftmaxModel:
    columns: tuple[str, ...]
    params: tuple[float, ...]


def parse_args() -> Config:
    parser = argparse.ArgumentParser(
        description="Compute DCED total-variation gap metrics over duplicate clusters."
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
            "dced_gap_metrics_micro10_full_24h.json"
        ),
        help="Path to write summary JSON.",
    )
    parser.add_argument(
        "--out-csv",
        type=Path,
        default=Path(
            "/Volumes/T9/BlueSky/output/analysis_demo_20260319/"
            "dced_gap_metrics_top_contexts_micro10_full_24h.csv"
        ),
        help="Path to write top residual contexts CSV.",
    )
    parser.add_argument(
        "--max-age-hours",
        type=float,
        default=24.0,
        help="Duplicate-local risk-set horizon in hours.",
    )
    args = parser.parse_args()
    return Config(
        root=args.root,
        study_id=args.study_id,
        out_json=args.out_json,
        out_csv=args.out_csv,
        max_age_hours=float(args.max_age_hours),
    )


def softmax(values: pd.Series) -> pd.Series:
    shifted = values - values.max()
    exps = shifted.map(math.exp)
    total = float(exps.sum())
    if total <= 0:
        return pd.Series([1.0 / len(values)] * len(values), index=values.index)
    return exps / total


def total_variation_distance(left: pd.Series, right: pd.Series) -> float:
    return 0.5 * float((left - right).abs().sum())


def fit_context_softmax(risk_df: pd.DataFrame, columns: list[str]) -> SoftmaxModel:
    contexts: list[tuple[np.ndarray, np.ndarray]] = []
    for _, frame in risk_df.groupby("context_id"):
        if len(frame) < 2:
            continue
        features = frame[columns].to_numpy(dtype=float)
        targets = frame["y"].to_numpy(dtype=float)
        contexts.append((features, targets))
    if not contexts:
        raise ValueError("no contexts available for softmax fit")

    def objective(beta: np.ndarray) -> tuple[float, np.ndarray]:
        loss = 0.0
        grad = np.zeros_like(beta)
        for features, targets in contexts:
            eta = features @ beta
            shifted = eta - eta.max()
            exp_shifted = np.exp(shifted)
            denom = float(exp_shifted.sum())
            probs = exp_shifted / denom
            total_y = float(targets.sum())
            loss += -(targets @ eta) + total_y * (float(np.log(denom)) + float(eta.max()))
            grad += features.T @ ((total_y * probs) - targets)
        l2 = 1e-6
        loss += l2 * float(beta @ beta)
        grad += 2.0 * l2 * beta
        return loss, grad

    beta0 = np.zeros(len(columns), dtype=float)
    result = minimize(
        fun=lambda beta: objective(beta)[0],
        x0=beta0,
        jac=lambda beta: objective(beta)[1],
        method="L-BFGS-B",
    )
    if not result.success:
        raise RuntimeError(f"softmax fit failed: {result.message}")
    return SoftmaxModel(columns=tuple(columns), params=tuple(float(x) for x in result.x))


def latent_score(
    frame: pd.DataFrame,
    model: SoftmaxModel,
) -> pd.Series:
    score = np.zeros(len(frame), dtype=float)
    for idx, column in enumerate(model.columns):
        score = score + model.params[idx] * frame[column].to_numpy(dtype=float)
    return pd.Series(score, index=frame.index, dtype=float)


def compute_cluster_gap(risk_df: pd.DataFrame) -> pd.DataFrame:
    observed = risk_df[risk_df["y"] > 0].copy()
    if observed.empty:
        raise ValueError("no observed risk-set rows with positive exposure")
    post_totals = (
        observed.groupby(["cluster_text", "post_uri"], as_index=False)["y"].sum()
        .rename(columns={"y": "E_i"})
    )
    cluster_rows: list[dict[str, Any]] = []
    for cluster_text, frame in post_totals.groupby("cluster_text"):
        total_exposure = float(frame["E_i"].sum())
        if total_exposure <= 0 or len(frame) < 2:
            continue
        shares = frame["E_i"] / total_exposure
        uniform = pd.Series([1.0 / len(frame)] * len(frame), index=frame.index)
        gap_equal = total_variation_distance(shares, uniform)
        top_share = float(shares.max())
        cluster_rows.append(
            {
                "cluster_text": cluster_text,
                "unique_posts": int(len(frame)),
                "total_exposure": total_exposure,
                "gap_equal": gap_equal,
                "top_share": top_share,
            }
        )
    return pd.DataFrame(cluster_rows)


def compute_context_gap(
    risk_df: pd.DataFrame,
    timing_model: SoftmaxModel,
    full_model: SoftmaxModel,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for context_id, frame in risk_df.groupby("context_id"):
        total_y = float(frame["y"].sum())
        if total_y <= 0 or len(frame) < 2:
            continue
        q = frame["y"] / total_y
        uniform = pd.Series([1.0 / len(frame)] * len(frame), index=frame.index)
        timing_pi = softmax(latent_score(frame, timing_model))
        full_pi = softmax(latent_score(frame, full_model))
        rows.append(
            {
                "context_id": int(context_id),
                "scheduled_window_start_utc": str(frame["scheduled_window_start_utc"].iloc[0]),
                "scheduled_window_end_utc": str(frame["scheduled_window_end_utc"].iloc[0]),
                "viewer_mode": str(frame["viewer_mode"].iloc[0]),
                "vantage_id": str(frame["vantage_id"].iloc[0]),
                "feed_uri": str(frame["feed_uri"].iloc[0]),
                "bucket": str(frame["bucket"].iloc[0]),
                "cluster_text": str(frame["cluster_text"].iloc[0]),
                "riskset_size": int(len(frame)),
                "context_total_y": total_y,
                "shown_count": int((frame["shown"] == 1).sum()),
                "zero_exposure_count": int((frame["shown"] == 0).sum()),
                "gap_equal": total_variation_distance(q, uniform),
                "gap_timing_residual": total_variation_distance(q, timing_pi),
                "gap_full_residual": total_variation_distance(q, full_pi),
            }
        )
    return pd.DataFrame(rows)


def weighted_mean(frame: pd.DataFrame, column: str) -> float:
    total_weight = float(frame["context_total_y"].sum())
    if total_weight <= 0:
        return float("nan")
    return float((frame[column] * frame["context_total_y"]).sum() / total_weight)


def summarize_context_gaps(context_gap_df: pd.DataFrame) -> dict[str, float]:
    gap_equal = weighted_mean(context_gap_df, "gap_equal")
    gap_timing = weighted_mean(context_gap_df, "gap_timing_residual")
    gap_full = weighted_mean(context_gap_df, "gap_full_residual")
    if gap_equal <= 0:
        explained_timing = float("nan")
        explained_full_extra = float("nan")
        unexplained = float("nan")
    else:
        explained_timing = (gap_equal - gap_timing) / gap_equal
        explained_full_extra = (gap_timing - gap_full) / gap_equal
        unexplained = gap_full / gap_equal
    return {
        "weighted_mean_gap_equal": round(gap_equal, 4),
        "weighted_mean_gap_timing_residual": round(gap_timing, 4),
        "weighted_mean_gap_full_residual": round(gap_full, 4),
        "timing_explained_share": round(explained_timing, 4),
        "author_extra_explained_share": round(explained_full_extra, 4),
        "unexplained_share": round(unexplained, 4),
    }


def summarize_cluster_gaps(cluster_gap_df: pd.DataFrame) -> dict[str, float]:
    return {
        "cluster_count": int(len(cluster_gap_df)),
        "mean_gap_equal": round(float(cluster_gap_df["gap_equal"].mean()), 4),
        "median_gap_equal": round(float(cluster_gap_df["gap_equal"].median()), 4),
        "mean_top_share": round(float(cluster_gap_df["top_share"].mean()), 4),
        "median_top_share": round(float(cluster_gap_df["top_share"].median()), 4),
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
    author_snapshots = load_latest_author_snapshots(
        config.root / "authors",
        needed_authors,
    )
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
    pair_df, _ = build_pair_rows(risk_df)

    timing_columns = [f"age_spline_{idx}" for idx in range(4)]
    full_columns = timing_columns + ["log10_followers", "log10_posts"]
    timing_model = fit_context_softmax(risk_df, timing_columns)
    full_model = fit_context_softmax(risk_df, full_columns)

    cluster_gap_df = compute_cluster_gap(risk_df)
    context_gap_df = compute_context_gap(
        risk_df=risk_df,
        timing_model=timing_model,
        full_model=full_model,
    )
    if context_gap_df.empty:
        raise ValueError("no context gaps computed")

    config.out_json.parent.mkdir(parents=True, exist_ok=True)
    config.out_csv.parent.mkdir(parents=True, exist_ok=True)

    top_contexts = context_gap_df.sort_values(
        ["gap_timing_residual", "context_total_y"],
        ascending=[False, False],
    ).head(50)
    top_contexts.to_csv(config.out_csv, index=False)

    cluster_top = cluster_gap_df.sort_values(
        ["gap_equal", "total_exposure"],
        ascending=[False, False],
    ).head(20)
    context_top = top_contexts[
        [
            "scheduled_window_start_utc",
            "viewer_mode",
            "bucket",
            "riskset_size",
            "shown_count",
            "zero_exposure_count",
            "context_total_y",
            "gap_equal",
            "gap_timing_residual",
            "gap_full_residual",
            "cluster_text",
        ]
    ]

    result = {
        "study_id": config.study_id,
        "max_age_hours": config.max_age_hours,
        "feed_posts": len(feed_posts),
        "valid_duplicate_clusters": len(valid_clusters),
        "riskset_contexts_ge2": int(risk_df["context_id"].nunique()),
        "riskset_rows": int(len(risk_df)),
        "pair_rows": int(len(pair_df)),
        "riskset_zero_exposure_share": round(float((risk_df["shown"] == 0).mean()), 4),
        "timing_model_params": {
            column: round(value, 6)
            for column, value in zip(timing_model.columns, timing_model.params)
        },
        "full_model_params": {
            column: round(value, 6)
            for column, value in zip(full_model.columns, full_model.params)
        },
        "cluster_gap_summary": summarize_cluster_gaps(cluster_gap_df),
        "context_gap_summary": summarize_context_gaps(context_gap_df),
        "top_clusters_by_gap_equal": cluster_top.to_dict("records"),
        "top_contexts_by_timing_residual": context_top.to_dict("records"),
    }
    config.out_json.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def main() -> None:
    config = parse_args()
    result = run(config)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
