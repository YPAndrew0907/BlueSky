#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Any

import pandas as pd
import statsmodels.api as sm

from run_dced_first_pass import (
    EPSILON,
    load_feed_posts,
    load_latest_author_snapshots,
    load_post_info,
    parse_timestamp,
)


csv.field_size_limit(2**31 - 1)


@dataclass(frozen=True)
class Config:
    root: Path
    study_id: str
    out_json: Path


@dataclass(frozen=True)
class ContextPost:
    post_uri: str
    author_did: str
    exposure: float
    age_hours: float
    log10_followers: float
    log10_posts: float


def parse_args() -> Config:
    parser = argparse.ArgumentParser(
        description=(
            "Run a factor-augmented first-pass duplicate-conditioned exposure model "
            "using rq1_factors joins plus the canonical micro5 study."
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
            "/Volumes/T9/BlueSky/output/analysis_demo_20260405_factor_augmented/"
            "dced_factor_augmented_first_pass_micro10_full.json"
        ),
        help="Path to write summary JSON.",
    )
    args = parser.parse_args()
    return Config(root=args.root, study_id=str(args.study_id), out_json=args.out_json)


def parse_json(value: str | None) -> Any:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def parse_optional_timestamp(value: str | None) -> Any:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return parse_timestamp(raw)
    except ValueError:
        return None


def log1p_float(value: Any) -> float:
    try:
        return math.log10(1.0 + max(0.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def bool_float(value: Any) -> float:
    if value in (1, True, "1", "true", "True"):
        return 1.0
    return 0.0


def count_json_list(value: str | None) -> float:
    parsed = parse_json(value)
    if isinstance(parsed, list):
        return float(len(parsed))
    return 0.0


def actor_numeric(actor: dict[str, float] | None, key: str) -> float:
    if actor is None:
        return 0.0
    return float(actor.get(key, 0.0))


def relation_direction(
    rel_map: dict[tuple[str, str], dict[str, float]], left_did: str, right_did: str
) -> dict[str, float]:
    left_to_right = rel_map.get((left_did, right_did))
    right_to_left = rel_map.get((right_did, left_did))

    left_follows_right = bool_float(left_to_right.get("following") if left_to_right else 0)
    right_follows_left = bool_float(right_to_left.get("following") if right_to_left else 0)
    left_blocks_right = bool_float(left_to_right.get("blocking") if left_to_right else 0)
    right_blocks_left = bool_float(right_to_left.get("blocking") if right_to_left else 0)
    rel_observed = 1.0 if left_to_right or right_to_left else 0.0

    return {
        "rq1_rel_observed_any": rel_observed,
        "rq1_rel_mutual_follow": 1.0
        if left_follows_right > 0 and right_follows_left > 0
        else 0.0,
        "rq1_rel_any_block": 1.0
        if left_blocks_right > 0 or right_blocks_left > 0
        else 0.0,
        "rq1_rel_follow_direction": left_follows_right - right_follows_left,
        "rq1_rel_block_direction": left_blocks_right - right_blocks_left,
    }


def build_context_posts(
    study_root: Path,
    post_info: dict[str, Any],
    valid_clusters: set[str],
    author_snapshots: dict[str, Any],
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
        author = author_snapshots.get(str(value["author_did"]))
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
                author_did=str(value["author_did"] or ""),
                exposure=float(value["exposure"]),
                age_hours=age_hours,
                log10_followers=math.log10(1 + int(getattr(author, "followers_count", 0))),
                log10_posts=math.log10(1 + int(getattr(author, "posts_count", 0))),
            )
        )
    return by_context


def load_rq1_actor_profiles(rq_root: Path) -> dict[str, dict[str, float]]:
    print("loading rq1 actor_profiles")
    latest: dict[str, tuple[str, dict[str, float]]] = {}
    for path in sorted(rq_root.rglob("actor_profiles_part_000.csv")):
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                actor_did = str(row.get("actor_did") or "").strip()
                if not actor_did:
                    continue
                captured = str(row.get("captured_at_utc") or "")
                existing = latest.get(actor_did)
                if existing is not None and captured <= existing[0]:
                    continue
                associated = parse_json(row.get("associated_json")) or {}
                verification = parse_json(row.get("verification_json")) or {}
                labels = parse_json(row.get("labels_json")) or []
                created_at = parse_optional_timestamp(row.get("created_at"))
                captured_at = parse_optional_timestamp(row.get("captured_at_utc"))
                account_age_days = 0.0
                if created_at is not None and captured_at is not None:
                    account_age_days = max(
                        0.0,
                        (captured_at - created_at).total_seconds() / 86400.0,
                    )
                latest[actor_did] = (
                    captured,
                    {
                        "rq1_log10_followers": log1p_float(row.get("followers_count")),
                        "rq1_log10_follows": log1p_float(row.get("follows_count")),
                        "rq1_log10_posts": log1p_float(row.get("posts_count")),
                        "rq1_desc_len_log": log1p_float(len(str(row.get("description") or ""))),
                        "rq1_label_count_log": log1p_float(len(labels) if isinstance(labels, list) else 0),
                        "rq1_account_age_days_log": log1p_float(account_age_days),
                        "rq1_has_website": 1.0 if str(row.get("website") or "").strip() else 0.0,
                        "rq1_has_avatar": 1.0 if str(row.get("avatar") or "").strip() else 0.0,
                        "rq1_has_banner": 1.0 if str(row.get("banner") or "").strip() else 0.0,
                        "rq1_joined_via_starter_pack": 1.0
                        if str(row.get("joined_via_starter_pack_uri") or "").strip()
                        else 0.0,
                        "rq1_verified_valid": 1.0
                        if verification.get("verifiedStatus") == "valid"
                        else 0.0,
                        "rq1_trusted_verifier": 1.0
                        if str(verification.get("trustedVerifierStatus") or "").strip()
                        not in {"", "none"}
                        else 0.0,
                        "rq1_associated_feedgens_log": log1p_float(associated.get("feedgens")),
                        "rq1_associated_lists_log": log1p_float(associated.get("lists")),
                        "rq1_associated_starterpacks_log": log1p_float(associated.get("starterPacks")),
                        "rq1_associated_labeler": 1.0
                        if associated.get("labeler") is True
                        else 0.0,
                    },
                )
    return {did: payload for did, (_, payload) in latest.items()}


def load_relationship_map(rq_root: Path) -> dict[tuple[str, str], dict[str, float]]:
    print("loading rq1 relationship_edges")
    rel_map: dict[tuple[str, str], dict[str, float]] = {}
    for path in sorted(rq_root.rglob("relationship_edges_part_000.csv")):
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                actor_did = str(row.get("actor_did") or "").strip()
                other_did = str(row.get("other_did") or "").strip()
                if not actor_did or not other_did:
                    continue
                key = (actor_did, other_did)
                bucket = rel_map.setdefault(
                    key,
                    {
                        "following": 0.0,
                        "followed_by": 0.0,
                        "blocking": 0.0,
                        "blocked_by": 0.0,
                    },
                )
                for field in ("following", "followed_by", "blocking", "blocked_by"):
                    bucket[field] = max(bucket[field], bool_float(row.get(field)))
    return rel_map


def load_sparse_actor_summaries(rq_root: Path) -> dict[str, dict[str, float]]:
    print("loading sparse rq1 actor summaries")
    data: dict[str, dict[str, float]] = defaultdict(dict)

    follow_subjects: dict[str, set[str]] = defaultdict(set)
    for path in sorted(rq_root.rglob("follow_records_part_000.csv")):
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                repo_did = str(row.get("repo_did") or "").strip()
                subject_did = str(row.get("subject_did") or "").strip()
                if not repo_did:
                    continue
                entry = data[repo_did]
                entry["rq1_follow_record_count"] = entry.get("rq1_follow_record_count", 0.0) + 1.0
                if subject_did:
                    follow_subjects[repo_did].add(subject_did)
    for did, subjects in follow_subjects.items():
        data[did]["rq1_follow_subject_count_log"] = log1p_float(len(subjects))

    for path in sorted(rq_root.rglob("repo_descriptions_part_000.csv")):
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                did = str(row.get("did") or "").strip()
                if not did:
                    continue
                entry = data[did]
                entry["rq1_repo_collections_count_log"] = log1p_float(
                    row.get("collections_count")
                )
                entry["rq1_repo_handle_correct"] = bool_float(
                    row.get("handle_is_correct")
                )

    for path in sorted(rq_root.rglob("actor_lists_part_000.csv")):
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                actor_did = str(row.get("actor_did") or "").strip()
                if not actor_did:
                    continue
                entry = data[actor_did]
                entry["rq1_actor_list_count"] = entry.get("rq1_actor_list_count", 0.0) + 1.0
                purpose = str(row.get("purpose") or "")
                if "modlist" in purpose:
                    entry["rq1_modlist_count"] = entry.get("rq1_modlist_count", 0.0) + 1.0

    for path in sorted(rq_root.rglob("actor_starter_packs_part_000.csv")):
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                actor_did = str(row.get("actor_did") or "").strip()
                if not actor_did:
                    continue
                entry = data[actor_did]
                entry["rq1_actor_starter_pack_count"] = entry.get(
                    "rq1_actor_starter_pack_count", 0.0
                ) + 1.0
                joined_all_time = log1p_float(row.get("joined_all_time_count"))
                entry["rq1_actor_starter_pack_joined_all_time_log"] = max(
                    entry.get("rq1_actor_starter_pack_joined_all_time_log", 0.0),
                    joined_all_time,
                )

    for path in sorted(rq_root.rglob("followers_edges_part_000.csv")):
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                actor_did = str(row.get("actor_did") or "").strip()
                if not actor_did:
                    continue
                entry = data[actor_did]
                entry["rq1_followers_edge_count"] = entry.get(
                    "rq1_followers_edge_count", 0.0
                ) + 1.0

    for path in sorted(rq_root.rglob("follows_edges_part_000.csv")):
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                actor_did = str(row.get("actor_did") or "").strip()
                if not actor_did:
                    continue
                entry = data[actor_did]
                entry["rq1_follows_edge_count"] = entry.get(
                    "rq1_follows_edge_count", 0.0
                ) + 1.0

    for path in sorted(rq_root.rglob("author_feed_items_part_000.csv")):
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                actor_did = str(row.get("actor_did") or "").strip()
                if not actor_did:
                    continue
                entry = data[actor_did]
                entry["rq1_author_feed_item_count"] = entry.get(
                    "rq1_author_feed_item_count", 0.0
                ) + 1.0

    compact: dict[str, dict[str, float]] = {}
    for did, entry in data.items():
        compact[did] = {
            "rq1_follow_record_count_log": log1p_float(entry.get("rq1_follow_record_count")),
            "rq1_follow_subject_count_log": float(entry.get("rq1_follow_subject_count_log", 0.0)),
            "rq1_repo_collections_count_log": float(entry.get("rq1_repo_collections_count_log", 0.0)),
            "rq1_repo_handle_correct": float(entry.get("rq1_repo_handle_correct", 0.0)),
            "rq1_actor_list_count_log": log1p_float(entry.get("rq1_actor_list_count")),
            "rq1_modlist_count_log": log1p_float(entry.get("rq1_modlist_count")),
            "rq1_actor_starter_pack_count_log": log1p_float(entry.get("rq1_actor_starter_pack_count")),
            "rq1_actor_starter_pack_joined_all_time_log": float(
                entry.get("rq1_actor_starter_pack_joined_all_time_log", 0.0)
            ),
            "rq1_followers_edge_count_log": log1p_float(entry.get("rq1_followers_edge_count")),
            "rq1_follows_edge_count_log": log1p_float(entry.get("rq1_follows_edge_count")),
            "rq1_author_feed_item_count_log": log1p_float(entry.get("rq1_author_feed_item_count")),
        }
    return compact


def load_post_feature_summaries(rq_root: Path) -> dict[str, dict[str, float]]:
    print("loading rq1 post-level summaries")
    data: dict[str, dict[str, float]] = defaultdict(dict)
    latest_post_view: dict[str, tuple[str, dict[str, float]]] = {}

    for path in sorted(rq_root.rglob("post_views_part_000.csv")):
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                post_uri = str(row.get("post_uri") or "").strip()
                if not post_uri:
                    continue
                captured = str(row.get("captured_at_utc") or "")
                existing = latest_post_view.get(post_uri)
                if existing is not None and captured <= existing[0]:
                    continue
                latest_post_view[post_uri] = (
                    captured,
                    {
                        "rq1_postview_like_count_log": log1p_float(row.get("like_count")),
                        "rq1_postview_repost_count_log": log1p_float(row.get("repost_count")),
                        "rq1_postview_reply_count_log": log1p_float(row.get("reply_count")),
                        "rq1_postview_quote_count_log": log1p_float(row.get("quote_count")),
                        "rq1_postview_has_image": bool_float(row.get("has_image")),
                        "rq1_postview_has_video": bool_float(row.get("has_video")),
                        "rq1_postview_has_external": bool_float(row.get("has_external")),
                        "rq1_postview_has_record_embed": bool_float(row.get("has_record_embed")),
                        "rq1_postview_lang_count_log": log1p_float(row.get("lang_count")),
                        "rq1_postview_tag_count_log": log1p_float(row.get("tag_count")),
                        "rq1_postview_link_count_log": log1p_float(row.get("link_count")),
                        "rq1_postview_label_count_log": log1p_float(
                            count_json_list(row.get("labels_json"))
                        ),
                    },
                )
    for post_uri, (_, payload) in latest_post_view.items():
        data[post_uri].update(payload)

    count_specs = [
        ("post_likes_part_000.csv", "rq1_like_edge_count_log"),
        ("post_quotes_part_000.csv", "rq1_quote_edge_count_log"),
        ("post_reposted_by_part_000.csv", "rq1_repost_edge_count_log"),
    ]
    for file_name, field_name in count_specs:
        counts: dict[str, int] = defaultdict(int)
        for path in sorted(rq_root.rglob(file_name)):
            with path.open(newline="", encoding="utf-8") as handle:
                for row in csv.DictReader(handle):
                    post_uri = str(row.get("post_uri") or "").strip()
                    if post_uri:
                        counts[post_uri] += 1
        for post_uri, count in counts.items():
            data[post_uri][field_name] = log1p_float(count)

    thread_stats: dict[str, dict[str, float]] = defaultdict(dict)
    for path in sorted(rq_root.rglob("thread_nodes_part_000.csv")):
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                focus_post_uri = str(row.get("focus_post_uri") or "").strip()
                if not focus_post_uri:
                    continue
                bucket = thread_stats[focus_post_uri]
                bucket["rq1_thread_node_count"] = bucket.get("rq1_thread_node_count", 0.0) + 1.0
                try:
                    distance = float(row.get("distance_to_focus") or 0.0)
                except ValueError:
                    distance = 0.0
                bucket["rq1_thread_max_distance"] = max(
                    bucket.get("rq1_thread_max_distance", 0.0),
                    distance,
                )
    for post_uri, stats in thread_stats.items():
        data[post_uri]["rq1_thread_node_count_log"] = log1p_float(
            stats.get("rq1_thread_node_count")
        )
        data[post_uri]["rq1_thread_max_distance"] = float(
            stats.get("rq1_thread_max_distance", 0.0)
        )

    return dict(data)


def fit_weighted_models(
    pairs: pd.DataFrame, outcome: str, specs: list[tuple[str, list[str]]]
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for viewer_mode in ("all", "unauth", "auth"):
        frame = pairs if viewer_mode == "all" else pairs[pairs["viewer_mode"] == viewer_mode]
        if len(frame) < 50:
            continue
        for name, columns in specs:
            usable_columns = [
                col
                for col in columns
                if col in frame.columns and frame[col].nunique(dropna=False) > 1
            ]
            if not usable_columns:
                continue
            design = sm.add_constant(frame[usable_columns], has_constant="add")
            model = sm.WLS(
                frame[outcome], design, weights=frame["context_total_y"]
            ).fit(cov_type="HC1")
            result[f"{viewer_mode}:{name}"] = {
                "n": int(len(frame)),
                "r2": round(float(model.rsquared), 4),
                "columns_used": list(model.params.index),
                "dropped_zero_variance": [
                    col for col in columns if col not in usable_columns
                ],
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
    rq_root = config.root / "rq1_factors"
    if not study_root.exists():
        raise FileNotFoundError(f"study root not found: {study_root}")
    if not rq_root.exists():
        raise FileNotFoundError(f"rq1_factors root not found: {rq_root}")

    print("loading canonical micro5 study")
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
    by_context = build_context_posts(
        study_root=study_root,
        post_info=post_info,
        valid_clusters=valid_clusters,
        author_snapshots=author_snapshots,
    )

    actor_profiles = load_rq1_actor_profiles(rq_root)
    relationship_map = load_relationship_map(rq_root)
    sparse_actor = load_sparse_actor_summaries(rq_root)
    post_features = load_post_feature_summaries(rq_root)

    print("building factor-augmented pair rows")
    pair_rows: list[dict[str, Any]] = []
    coverage = defaultdict(int)

    actor_diff_keys = [
        "rq1_log10_followers",
        "rq1_log10_follows",
        "rq1_log10_posts",
        "rq1_desc_len_log",
        "rq1_label_count_log",
        "rq1_account_age_days_log",
        "rq1_has_website",
        "rq1_has_avatar",
        "rq1_has_banner",
        "rq1_joined_via_starter_pack",
        "rq1_verified_valid",
        "rq1_trusted_verifier",
        "rq1_associated_feedgens_log",
        "rq1_associated_lists_log",
        "rq1_associated_starterpacks_log",
        "rq1_associated_labeler",
    ]
    sparse_diff_keys = [
        "rq1_follow_record_count_log",
        "rq1_follow_subject_count_log",
        "rq1_repo_collections_count_log",
        "rq1_repo_handle_correct",
        "rq1_actor_list_count_log",
        "rq1_modlist_count_log",
        "rq1_actor_starter_pack_count_log",
        "rq1_actor_starter_pack_joined_all_time_log",
        "rq1_followers_edge_count_log",
        "rq1_follows_edge_count_log",
        "rq1_author_feed_item_count_log",
    ]
    post_diff_keys = [
        "rq1_postview_like_count_log",
        "rq1_postview_repost_count_log",
        "rq1_postview_reply_count_log",
        "rq1_postview_quote_count_log",
        "rq1_postview_has_image",
        "rq1_postview_has_video",
        "rq1_postview_has_external",
        "rq1_postview_has_record_embed",
        "rq1_postview_lang_count_log",
        "rq1_postview_tag_count_log",
        "rq1_postview_link_count_log",
        "rq1_postview_label_count_log",
        "rq1_like_edge_count_log",
        "rq1_quote_edge_count_log",
        "rq1_repost_edge_count_log",
        "rq1_thread_node_count_log",
        "rq1_thread_max_distance",
    ]

    for context_key, rows in by_context.items():
        if len(rows) < 2:
            continue
        _, viewer_mode, _, _, bucket, _ = context_key
        total_y = sum(row.exposure for row in rows)
        ordered_rows = sorted(rows, key=lambda row: row.post_uri)
        for left, right in combinations(ordered_rows, 2):
            left_actor = actor_profiles.get(left.author_did)
            right_actor = actor_profiles.get(right.author_did)
            left_sparse = sparse_actor.get(left.author_did)
            right_sparse = sparse_actor.get(right.author_did)
            left_post = post_features.get(left.post_uri)
            right_post = post_features.get(right.post_uri)
            rel_features = relation_direction(
                relationship_map, left.author_did, right.author_did
            )

            pair_any_actor = 1.0 if left_actor or right_actor else 0.0
            pair_both_actor = 1.0 if left_actor and right_actor else 0.0
            pair_any_sparse = 1.0 if left_sparse or right_sparse else 0.0
            pair_any_post = 1.0 if left_post or right_post else 0.0
            pair_both_post = 1.0 if left_post and right_post else 0.0

            row = {
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
                "pair_any_rq1_actor_profile": pair_any_actor,
                "pair_both_rq1_actor_profile": pair_both_actor,
                "pair_any_rq1_sparse_actor": pair_any_sparse,
                "pair_any_rq1_post_feature": pair_any_post,
                "pair_both_rq1_post_feature": pair_both_post,
            }
            for key in actor_diff_keys:
                row[f"d_{key}"] = actor_numeric(left_actor, key) - actor_numeric(
                    right_actor, key
                )
            for key in sparse_diff_keys:
                row[f"d_{key}"] = actor_numeric(left_sparse, key) - actor_numeric(
                    right_sparse, key
                )
            for key in post_diff_keys:
                row[f"d_{key}"] = actor_numeric(left_post, key) - actor_numeric(
                    right_post, key
                )
            row.update(rel_features)
            pair_rows.append(row)

            if pair_any_actor > 0:
                coverage["pairs_any_actor_profile"] += 1
            if pair_both_actor > 0:
                coverage["pairs_both_actor_profile"] += 1
            if pair_any_sparse > 0:
                coverage["pairs_any_sparse_actor"] += 1
            if pair_any_post > 0:
                coverage["pairs_any_post_feature"] += 1
            if pair_both_post > 0:
                coverage["pairs_both_post_feature"] += 1
            if rel_features["rq1_rel_observed_any"] > 0:
                coverage["pairs_any_relationship"] += 1

    pairs = pd.DataFrame(pair_rows)
    pair_count = int(len(pairs))
    if pair_count == 0:
        raise ValueError("no pair rows constructed")

    specs = [
        ("timing_only", ["d_age_hours"]),
        ("timing_plus_old_author", ["d_age_hours", "d_log10_followers", "d_log10_posts"]),
        (
            "timing_plus_rq1_actor",
            [
                "d_age_hours",
                "d_log10_followers",
                "d_log10_posts",
                "pair_any_rq1_actor_profile",
                "pair_both_rq1_actor_profile",
            ]
            + [f"d_{key}" for key in actor_diff_keys],
        ),
        (
            "timing_plus_rq1_actor_relationship",
            [
                "d_age_hours",
                "d_log10_followers",
                "d_log10_posts",
                "pair_any_rq1_actor_profile",
                "pair_both_rq1_actor_profile",
            ]
            + [f"d_{key}" for key in actor_diff_keys]
            + [
                "rq1_rel_observed_any",
                "rq1_rel_mutual_follow",
                "rq1_rel_any_block",
                "rq1_rel_follow_direction",
                "rq1_rel_block_direction",
            ],
        ),
        (
            "timing_plus_all_new_factors_pre",
            [
                "d_age_hours",
                "d_log10_followers",
                "d_log10_posts",
                "pair_any_rq1_actor_profile",
                "pair_both_rq1_actor_profile",
                "pair_any_rq1_sparse_actor",
            ]
            + [f"d_{key}" for key in actor_diff_keys]
            + [f"d_{key}" for key in sparse_diff_keys]
            + [
                "rq1_rel_observed_any",
                "rq1_rel_mutual_follow",
                "rq1_rel_any_block",
                "rq1_rel_follow_direction",
                "rq1_rel_block_direction",
            ],
        ),
        (
            "timing_plus_all_new_factors_exploratory",
            [
                "d_age_hours",
                "d_log10_followers",
                "d_log10_posts",
                "pair_any_rq1_actor_profile",
                "pair_both_rq1_actor_profile",
                "pair_any_rq1_sparse_actor",
                "pair_any_rq1_post_feature",
                "pair_both_rq1_post_feature",
            ]
            + [f"d_{key}" for key in actor_diff_keys]
            + [f"d_{key}" for key in sparse_diff_keys]
            + [f"d_{key}" for key in post_diff_keys]
            + [
                "rq1_rel_observed_any",
                "rq1_rel_mutual_follow",
                "rq1_rel_any_block",
                "rq1_rel_follow_direction",
                "rq1_rel_block_direction",
            ],
        ),
    ]

    return {
        "study_id": config.study_id,
        "feed_posts": len(feed_posts),
        "post_info_posts": len(post_info),
        "valid_duplicate_clusters": len(valid_clusters),
        "author_profile_coverage_main": round(len(author_snapshots) / len(needed_authors), 4)
        if needed_authors
        else None,
        "rq1_overlap_summary": {
            "rq1_actor_profile_authors": len(actor_profiles),
            "rq1_relationship_pairs": len(relationship_map),
            "rq1_sparse_actor_rows": len(sparse_actor),
            "rq1_post_feature_posts": len(post_features),
        },
        "pair_rows": pair_count,
        "pair_factor_coverage": {
            "pairs_any_actor_profile": coverage["pairs_any_actor_profile"],
            "pairs_both_actor_profile": coverage["pairs_both_actor_profile"],
            "pairs_any_relationship": coverage["pairs_any_relationship"],
            "pairs_any_sparse_actor": coverage["pairs_any_sparse_actor"],
            "pairs_any_post_feature": coverage["pairs_any_post_feature"],
            "pairs_both_post_feature": coverage["pairs_both_post_feature"],
            "share_pairs_any_actor_profile": round(
                coverage["pairs_any_actor_profile"] / pair_count, 6
            ),
            "share_pairs_both_actor_profile": round(
                coverage["pairs_both_actor_profile"] / pair_count, 6
            ),
            "share_pairs_any_relationship": round(
                coverage["pairs_any_relationship"] / pair_count, 6
            ),
            "share_pairs_any_sparse_actor": round(
                coverage["pairs_any_sparse_actor"] / pair_count, 6
            ),
            "share_pairs_any_post_feature": round(
                coverage["pairs_any_post_feature"] / pair_count, 6
            ),
            "share_pairs_both_post_feature": round(
                coverage["pairs_both_post_feature"] / pair_count, 6
            ),
        },
        "share_gap_models": fit_weighted_models(pairs, "d_log_share", specs),
        "win_models": fit_weighted_models(pairs, "win_left", specs),
    }


def main() -> None:
    config = parse_args()
    result = run(config)
    config.out_json.parent.mkdir(parents=True, exist_ok=True)
    config.out_json.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
