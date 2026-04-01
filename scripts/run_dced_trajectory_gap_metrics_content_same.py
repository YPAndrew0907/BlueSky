#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from run_dced_gap_metrics import SoftmaxModel, fit_context_softmax
from run_dced_timing_upgrade import (
    PostInfo,
    availability_time,
    build_feed_cluster_seen_posts,
    build_observed_exposures,
    build_riskset_rows,
    describe_riskset_definition,
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
    signature_cache_json: Path
    max_age_hours: float
    riskset_mode: str
    early_window_hours: float
    availability_anchor: str
    max_first_monitor_delay_minutes: float | None
    strict_cohort_mode: str
    appview_host: str
    batch_size: int
    request_pause_seconds: float


@dataclass(frozen=True)
class ContentSignature:
    top_embed_type: str
    media_embed_type: str
    external_uri: str
    record_uri: str
    media_signature: tuple[str, ...]

    def as_key(self) -> str:
        payload = {
            "top_embed_type": self.top_embed_type,
            "media_embed_type": self.media_embed_type,
            "external_uri": self.external_uri,
            "record_uri": self.record_uri,
            "media_signature": list(self.media_signature),
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    def short_label(self) -> str:
        digest = hashlib.sha1(self.as_key().encode("utf-8")).hexdigest()[:12]
        return (
            f"{self.top_embed_type}|{self.media_embed_type}|"
            f"{digest}"
        )


@dataclass(frozen=True)
class AnalysisSummary:
    valid_duplicate_clusters: int
    riskset_contexts_ge2: int
    riskset_rows: int
    riskset_zero_exposure_share: float
    strict_cohort_summary: dict[str, Any]
    trajectory_feature_coverage: dict[str, float | None]
    model_columns: dict[str, list[str]]
    model_params: dict[str, dict[str, float]]
    gap_summary: dict[str, float]


def parse_args() -> Config:
    parser = argparse.ArgumentParser(
        description=(
            "Run DCED gap decomposition using a stricter duplicate definition: "
            "same text + same embed type + same media signature + same external URL "
            "+ same quoted target."
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
            "/Volumes/T9/BlueSky/output/analysis_demo_20260321/"
            "dced_trajectory_gap_metrics_micro10_full_24h_1h_"
            "availability_strict20m_context_ever_seen_content_same.json"
        ),
        help="Path to write summary JSON.",
    )
    parser.add_argument(
        "--signature-cache-json",
        type=Path,
        default=Path(
            "/Volumes/T9/BlueSky/output/analysis_demo_20260321/"
            "content_signature_cache_micro10_full_live_20260319.json"
        ),
        help="Path to read/write cached live content signatures.",
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
        default="ever_seen_in_feed",
        help=(
            "Risk-set construction rule. Default uses the stricter ever-seen-in-feed mode."
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
        help="Strict-cohort delay cutoff in minutes.",
    )
    parser.add_argument(
        "--strict-cohort-mode",
        choices=("row", "context"),
        default="context",
        help="How to apply the strict first-monitor-delay filter.",
    )
    parser.add_argument(
        "--appview-host",
        default="https://public.api.bsky.app",
        help="AppView host for app.bsky.feed.getPosts.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=25,
        help="How many post URIs to fetch per getPosts call.",
    )
    parser.add_argument(
        "--request-pause-seconds",
        type=float,
        default=0.0,
        help="Optional pause between live getPosts requests.",
    )
    args = parser.parse_args()
    return Config(
        root=args.root,
        study_id=str(args.study_id),
        out_json=args.out_json,
        signature_cache_json=args.signature_cache_json,
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
        appview_host=str(args.appview_host),
        batch_size=int(args.batch_size),
        request_pause_seconds=float(args.request_pause_seconds),
    )


def batched(items: list[str], size: int) -> list[list[str]]:
    return [items[idx : idx + size] for idx in range(0, len(items), size)]


def make_request_url(appview_host: str, uris: list[str]) -> str:
    query = "&".join(
        "uris=" + urllib.parse.quote(post_uri, safe="")
        for post_uri in uris
    )
    return f"{appview_host.rstrip('/')}/xrpc/app.bsky.feed.getPosts?{query}"


def fetch_posts_batch(
    appview_host: str,
    uris: list[str],
) -> dict[str, dict[str, Any]]:
    if not uris:
        return {}
    request_url = make_request_url(appview_host, uris)
    request = urllib.request.Request(
        request_url,
        headers={"User-Agent": "BlueSky-DCED-ContentSame/1.0"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.load(response)
    return {
        str(post.get("uri")): post
        for post in payload.get("posts", [])
        if isinstance(post, dict) and isinstance(post.get("uri"), str)
    }


def image_urls(node: dict[str, Any]) -> tuple[str, ...]:
    urls: list[str] = []
    for image in node.get("images", []):
        if not isinstance(image, dict):
            continue
        url = image.get("fullsize") or image.get("thumb")
        if isinstance(url, str) and url:
            urls.append(url)
    return tuple(urls)


def build_content_signature(post_payload: dict[str, Any]) -> ContentSignature:
    embed = post_payload.get("embed") or {}
    if not isinstance(embed, dict):
        return ContentSignature("none", "none", "", "", ())

    top_embed_type = str(embed.get("$type") or "none")
    media_embed_type = "none"
    external_uri = ""
    record_uri = ""
    media_signature: tuple[str, ...] = ()

    if top_embed_type == "app.bsky.embed.images#view":
        media_embed_type = "app.bsky.embed.images#view"
        media_signature = image_urls(embed)
    elif top_embed_type == "app.bsky.embed.external#view":
        media_embed_type = "app.bsky.embed.external#view"
        external = embed.get("external") or {}
        if isinstance(external, dict):
            external_uri = str(external.get("uri") or "")
            media_signature = (external_uri,) if external_uri else ()
    elif top_embed_type == "app.bsky.embed.record#view":
        media_embed_type = "app.bsky.embed.record#view"
        record = embed.get("record") or {}
        if isinstance(record, dict):
            record_uri = str(record.get("uri") or "")
    elif top_embed_type == "app.bsky.embed.recordWithMedia#view":
        record = embed.get("record") or {}
        if isinstance(record, dict):
            record_view = record.get("record") or {}
            if isinstance(record_view, dict):
                record_uri = str(record_view.get("uri") or "")
        media = embed.get("media") or {}
        if isinstance(media, dict):
            media_embed_type = str(media.get("$type") or "none")
            if media_embed_type == "app.bsky.embed.images#view":
                media_signature = image_urls(media)
            elif media_embed_type == "app.bsky.embed.video#view":
                playlist = str(media.get("playlist") or "")
                cid = str(media.get("cid") or "")
                media_signature = tuple(
                    value for value in (playlist, cid) if value
                )
            elif media_embed_type == "app.bsky.embed.external#view":
                external = media.get("external") or {}
                if isinstance(external, dict):
                    external_uri = str(external.get("uri") or "")
                    media_signature = (external_uri,) if external_uri else ()
            else:
                media_signature = ()
    elif top_embed_type == "app.bsky.embed.video#view":
        media_embed_type = "app.bsky.embed.video#view"
        playlist = str(embed.get("playlist") or "")
        cid = str(embed.get("cid") or "")
        media_signature = tuple(value for value in (playlist, cid) if value)

    return ContentSignature(
        top_embed_type=top_embed_type,
        media_embed_type=media_embed_type,
        external_uri=external_uri,
        record_uri=record_uri,
        media_signature=media_signature,
    )


def load_signature_cache(path: Path) -> dict[str, dict[str, Any] | None]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as err:
        raise ValueError(f"invalid signature cache JSON: {path}") from err
    if not isinstance(payload, dict):
        raise ValueError(f"signature cache must be an object: {path}")
    return {
        str(post_uri): value if isinstance(value, dict) or value is None else None
        for post_uri, value in payload.items()
    }


def save_signature_cache(
    path: Path,
    cache_payload: dict[str, dict[str, Any] | None],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache_payload, indent=2), encoding="utf-8")


def fetch_content_signatures(
    post_uris: set[str],
    appview_host: str,
    cache_path: Path,
    batch_size: int,
    request_pause_seconds: float,
) -> tuple[dict[str, ContentSignature], dict[str, int | float]]:
    cache = load_signature_cache(cache_path)
    signature_cache_payload = dict(cache)
    found: dict[str, ContentSignature] = {}
    missing: set[str] = set()

    uncached = sorted(post_uri for post_uri in post_uris if post_uri not in cache)
    for batch in batched(uncached, batch_size):
        batch_payload: dict[str, dict[str, Any]] = {}
        attempts = 0
        while attempts < 3:
            attempts += 1
            try:
                batch_payload = fetch_posts_batch(appview_host, batch)
                break
            except Exception:
                if attempts >= 3:
                    batch_payload = {}
                    break
                time.sleep(1.0 * attempts)
        for post_uri in batch:
            payload = batch_payload.get(post_uri)
            if payload is None:
                signature_cache_payload[post_uri] = None
                missing.add(post_uri)
                continue
            signature = build_content_signature(payload)
            signature_cache_payload[post_uri] = {
                "top_embed_type": signature.top_embed_type,
                "media_embed_type": signature.media_embed_type,
                "external_uri": signature.external_uri,
                "record_uri": signature.record_uri,
                "media_signature": list(signature.media_signature),
            }
            found[post_uri] = signature
        if request_pause_seconds > 0:
            time.sleep(request_pause_seconds)

    for post_uri in post_uris:
        cached = signature_cache_payload.get(post_uri)
        if cached is None:
            missing.add(post_uri)
            continue
        if not isinstance(cached, dict):
            missing.add(post_uri)
            continue
        found[post_uri] = ContentSignature(
            top_embed_type=str(cached.get("top_embed_type") or "none"),
            media_embed_type=str(cached.get("media_embed_type") or "none"),
            external_uri=str(cached.get("external_uri") or ""),
            record_uri=str(cached.get("record_uri") or ""),
            media_signature=tuple(str(item) for item in cached.get("media_signature", [])),
        )
    save_signature_cache(cache_path, signature_cache_payload)
    summary = {
        "candidate_posts_for_live_signature": len(post_uris),
        "fetched_signature_posts": len(found),
        "missing_signature_posts": len(missing),
        "signature_coverage_share": round(len(found) / len(post_uris), 4)
        if post_uris
        else None,
        "signature_cache_path": str(cache_path),
    }
    return found, summary


def refine_post_info_by_signature(
    post_info: dict[str, PostInfo],
    candidate_post_uris: set[str],
    signatures: dict[str, ContentSignature],
) -> tuple[dict[str, PostInfo], dict[str, set[str]], dict[str, set[str]], dict[str, int | float]]:
    refined_post_info: dict[str, PostInfo] = {}
    refined_cluster_posts: dict[str, set[str]] = {}
    refined_cluster_authors: dict[str, set[str]] = {}

    for post_uri in candidate_post_uris:
        info = post_info.get(post_uri)
        signature = signatures.get(post_uri)
        if info is None or signature is None:
            continue
        refined_cluster_text = f"{info.cluster_text} || {signature.short_label()}"
        refined_post_info[post_uri] = PostInfo(
            cluster_text=refined_cluster_text,
            author_did=info.author_did,
            record_created_at=info.record_created_at,
            indexed_at=info.indexed_at,
        )
        refined_cluster_posts.setdefault(refined_cluster_text, set()).add(post_uri)
        if info.author_did:
            refined_cluster_authors.setdefault(refined_cluster_text, set()).add(
                info.author_did
            )
        else:
            refined_cluster_authors.setdefault(refined_cluster_text, set())

    valid_clusters = {
        cluster_text
        for cluster_text, posts in refined_cluster_posts.items()
        if len(posts) >= 2 and len(refined_cluster_authors.get(cluster_text, set())) >= 2
    }
    refined_needed_posts = {
        post_uri
        for post_uri, info in refined_post_info.items()
        if info.cluster_text in valid_clusters
    }
    refined_summary = {
        "refined_posts_with_signature": len(refined_post_info),
        "refined_clusters_total": len(refined_cluster_posts),
        "refined_valid_duplicate_clusters": len(valid_clusters),
        "refined_posts_in_valid_clusters": len(refined_needed_posts),
    }
    return (
        refined_post_info,
        refined_cluster_posts,
        refined_cluster_authors,
        refined_summary,
    )


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


def weighted_gap_summary(context_gap_df: pd.DataFrame) -> dict[str, float]:
    if context_gap_df.empty:
        raise ValueError("context gap table is empty")
    weight_total = float(context_gap_df["context_total_y"].sum())
    if weight_total <= 0:
        raise ValueError("context gap table has non-positive total weight")

    summary = {
        "weighted_mean_gap_equal": float(
            (context_gap_df["gap_equal"] * context_gap_df["context_total_y"]).sum()
            / weight_total
        ),
        "weighted_mean_gap_timing": float(
            (context_gap_df["gap_timing"] * context_gap_df["context_total_y"]).sum()
            / weight_total
        ),
        "weighted_mean_gap_timing_author": float(
            (
                context_gap_df["gap_timing_author"]
                * context_gap_df["context_total_y"]
            ).sum()
            / weight_total
        ),
        "weighted_mean_gap_timing_trajectory": float(
            (
                context_gap_df["gap_timing_trajectory"]
                * context_gap_df["context_total_y"]
            ).sum()
            / weight_total
        ),
        "weighted_mean_gap_timing_author_trajectory": float(
            (
                context_gap_df["gap_timing_author_trajectory"]
                * context_gap_df["context_total_y"]
            ).sum()
            / weight_total
        ),
    }

    gap_equal = summary["weighted_mean_gap_equal"]
    if gap_equal <= 0:
        raise ValueError("equal-allocation gap must be positive")
    summary.update(
        {
            "timing_explained_share": (summary["weighted_mean_gap_equal"] - summary["weighted_mean_gap_timing"]) / gap_equal,
            "author_extra_after_timing_share": (
                summary["weighted_mean_gap_timing"] - summary["weighted_mean_gap_timing_author"]
            ) / gap_equal,
            "trajectory_extra_after_timing_share": (
                summary["weighted_mean_gap_timing"] - summary["weighted_mean_gap_timing_trajectory"]
            ) / gap_equal,
            "trajectory_extra_after_timing_author_share": (
                summary["weighted_mean_gap_timing_author"]
                - summary["weighted_mean_gap_timing_author_trajectory"]
            ) / gap_equal,
            "final_unexplained_share": (
                summary["weighted_mean_gap_timing_author_trajectory"] / gap_equal
            ),
        }
    )
    return {key: round(value, 4) for key, value in summary.items()}


def analyze_duplicate_spec(
    *,
    study_root: Path,
    root: Path,
    feed_posts: pd.DataFrame,
    post_info: dict[str, PostInfo],
    cluster_posts: dict[str, set[str]],
    cluster_authors: dict[str, set[str]],
    config: Config,
) -> AnalysisSummary:
    valid_clusters = {
        cluster_text
        for cluster_text, posts in cluster_posts.items()
        if len(posts) >= 2 and len(cluster_authors.get(cluster_text, set())) >= 2
    }
    needed_authors = {
        info.author_did
        for info in post_info.values()
        if info.cluster_text in valid_clusters and info.author_did
    }
    author_snapshots = load_latest_author_snapshots(root / "authors", needed_authors)
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
    if risk_df.empty:
        raise ValueError("risk set construction produced no rows")
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
    cohort_summary["contexts_after_filter_ge2"] = int(context_gap_df["context_id"].nunique())
    return AnalysisSummary(
        valid_duplicate_clusters=len(valid_clusters),
        riskset_contexts_ge2=int(context_gap_df["context_id"].nunique()),
        riskset_rows=int(len(risk_df)),
        riskset_zero_exposure_share=round(float((risk_df["shown"] == 0).mean()), 4),
        strict_cohort_summary=cohort_summary,
        trajectory_feature_coverage={
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
        model_columns={name: list(model.columns) for name, model in models.items()},
        model_params={
            name: {
                column: round(value, 6)
                for column, value in zip(model.columns, model.params)
            }
            for name, model in models.items()
        },
        gap_summary=weighted_gap_summary(context_gap_df),
    )


def run(config: Config) -> dict[str, Any]:
    study_root = config.root / "micro5" / config.study_id / "micro5_core_full"
    if not study_root.exists():
        raise FileNotFoundError(f"study root not found: {study_root}")

    feed_posts = load_feed_posts(study_root)
    base_post_info, base_cluster_posts, base_cluster_authors = load_post_info(
        config.root, feed_posts
    )
    base_valid_clusters = {
        cluster_text
        for cluster_text, posts in base_cluster_posts.items()
        if len(posts) >= 2 and len(base_cluster_authors[cluster_text]) >= 2
    }
    candidate_post_uris = {
        post_uri
        for post_uri, info in base_post_info.items()
        if info.cluster_text in base_valid_clusters
    }
    baseline_summary = analyze_duplicate_spec(
        study_root=study_root,
        root=config.root,
        feed_posts=feed_posts,
        post_info=base_post_info,
        cluster_posts=base_cluster_posts,
        cluster_authors=base_cluster_authors,
        config=config,
    )

    signatures, signature_summary = fetch_content_signatures(
        post_uris=candidate_post_uris,
        appview_host=config.appview_host,
        cache_path=config.signature_cache_json,
        batch_size=config.batch_size,
        request_pause_seconds=config.request_pause_seconds,
    )
    (
        post_info,
        cluster_posts,
        cluster_authors,
        refined_summary,
    ) = refine_post_info_by_signature(
        post_info=base_post_info,
        candidate_post_uris=candidate_post_uris,
        signatures=signatures,
    )
    content_same_summary = analyze_duplicate_spec(
        study_root=study_root,
        root=config.root,
        feed_posts=feed_posts,
        post_info=post_info,
        cluster_posts=cluster_posts,
        cluster_authors=cluster_authors,
        config=config,
    )

    config.out_json.parent.mkdir(parents=True, exist_ok=True)
    result = {
        "study_id": config.study_id,
        "content_signature_mode": "live_embed_strict_same",
        "content_signature_definition": (
            "same normalized text + same top embed type + same nested media embed type "
            "+ same external URL + same quoted/embedded record target + same media signature"
        ),
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
        "base_valid_duplicate_clusters": len(base_valid_clusters),
        "same_snapshot_baseline": {
            "valid_duplicate_clusters": baseline_summary.valid_duplicate_clusters,
            "riskset_contexts_ge2": baseline_summary.riskset_contexts_ge2,
            "riskset_rows": baseline_summary.riskset_rows,
            "riskset_zero_exposure_share": baseline_summary.riskset_zero_exposure_share,
            "strict_cohort_summary": baseline_summary.strict_cohort_summary,
            "trajectory_feature_coverage": baseline_summary.trajectory_feature_coverage,
            "model_columns": baseline_summary.model_columns,
            "model_params": baseline_summary.model_params,
            "gap_summary": baseline_summary.gap_summary,
        },
        "live_signature_summary": signature_summary,
        "refined_duplicate_summary": refined_summary,
        "valid_duplicate_clusters": content_same_summary.valid_duplicate_clusters,
        "riskset_contexts_ge2": content_same_summary.riskset_contexts_ge2,
        "riskset_rows": content_same_summary.riskset_rows,
        "riskset_zero_exposure_share": content_same_summary.riskset_zero_exposure_share,
        "strict_cohort_summary": content_same_summary.strict_cohort_summary,
        "trajectory_feature_coverage": content_same_summary.trajectory_feature_coverage,
        "model_columns": content_same_summary.model_columns,
        "model_params": content_same_summary.model_params,
        "gap_summary": content_same_summary.gap_summary,
    }
    config.out_json.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def main() -> None:
    config = parse_args()
    result = run(config)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
