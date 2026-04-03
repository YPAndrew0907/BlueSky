from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ClusterLabel:
    topic: str
    cluster_id: str
    event_guess: str
    frame_label: str
    label_confidence: str
    label_row_count: int


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _write_csv(path: Path, *, fieldnames: list[str], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _dominant_nonempty(rows: list[dict[str, str]], key: str) -> str:
    counts = Counter(str(row.get(key, "")).strip() for row in rows if str(row.get(key, "")).strip())
    if not counts:
        return ""
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return ranked[0][0]


def _resolve_cluster_labels(label_rows: list[dict[str, str]]) -> dict[tuple[str, str], ClusterLabel]:
    grouped: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in label_rows:
        topic = str(row.get("topic", "")).strip()
        cluster_id = str(row.get("cluster_id", "")).strip()
        if not topic or not cluster_id:
            continue
        grouped[(topic, cluster_id)].append(row)

    resolved: dict[tuple[str, str], ClusterLabel] = {}
    for key, rows in grouped.items():
        topic, cluster_id = key
        resolved[key] = ClusterLabel(
            topic=topic,
            cluster_id=cluster_id,
            event_guess=_dominant_nonempty(rows, "event_guess"),
            frame_label=_dominant_nonempty(rows, "frame_label"),
            label_confidence=_dominant_nonempty(rows, "label_confidence"),
            label_row_count=len(rows),
        )
    return resolved


def generate_frame_exposure_supply_tables(
    *,
    batch_dir: Path,
    label_rows_path: Path,
    out_dir: Path,
) -> dict[str, Any]:
    if not batch_dir.exists():
        raise FileNotFoundError(f"missing batch directory: {batch_dir}")
    if not label_rows_path.exists():
        raise FileNotFoundError(f"missing label rows: {label_rows_path}")

    label_rows = _read_csv(label_rows_path)
    cluster_labels = _resolve_cluster_labels(label_rows)
    if not cluster_labels:
        raise ValueError(f"no usable topic/cluster labels found in {label_rows_path}")

    cluster_members: dict[tuple[str, str], set[str]] = defaultdict(set)
    post_to_cluster: dict[tuple[str, str], str] = {}
    for topic_dir in sorted(path for path in batch_dir.iterdir() if path.is_dir()):
        membership_path = topic_dir / "clusters" / "cluster_membership.csv"
        if not membership_path.exists():
            continue
        topic = topic_dir.name
        for row in _read_csv(membership_path):
            cluster_id = str(row.get("cluster_id", "")).strip()
            post_uri = str(row.get("post_uri", "")).strip()
            if not cluster_id or not post_uri:
                continue
            cluster_members[(topic, cluster_id)].add(post_uri)
            post_to_cluster[(topic, post_uri)] = cluster_id

    labeled_clusters: dict[tuple[str, str], ClusterLabel] = {}
    for key, label in cluster_labels.items():
        if key in cluster_members:
            labeled_clusters[key] = label

    if not labeled_clusters:
        raise ValueError(f"no labeled clusters matched membership rows under {batch_dir}")

    cluster_rows: list[dict[str, object]] = []
    supply_by_frame: dict[tuple[str, str, str], dict[str, object]] = {}
    for key, label in sorted(labeled_clusters.items()):
        member_posts = cluster_members.get(key, set())
        supply_key = (label.topic, label.frame_label, label.event_guess)
        existing = supply_by_frame.get(supply_key)
        if existing is None:
            existing = {
                "topic": label.topic,
                "frame_label": label.frame_label,
                "event_guess": label.event_guess,
                "supply_clusters": 0,
                "supply_posts_set": set(),
                "label_row_count": 0,
            }
            supply_by_frame[supply_key] = existing
        existing["supply_clusters"] = int(existing["supply_clusters"]) + 1
        existing["label_row_count"] = int(existing["label_row_count"]) + int(label.label_row_count)
        supply_posts_set = existing["supply_posts_set"]
        assert isinstance(supply_posts_set, set)
        supply_posts_set.update(member_posts)

        cluster_rows.append(
            {
                "topic": label.topic,
                "cluster_id": label.cluster_id,
                "event_guess": label.event_guess,
                "frame_label": label.frame_label,
                "label_confidence": label.label_confidence,
                "label_row_count": label.label_row_count,
                "supply_posts": len(member_posts),
            }
        )

    exposure_group_sets: dict[tuple[str, str, str, str, str, str], dict[str, set[str]]] = {}
    overall_exposure_sets: dict[tuple[str, str, str], dict[str, set[str]]] = {}
    for topic_dir in sorted(path for path in batch_dir.iterdir() if path.is_dir()):
        topic = topic_dir.name
        matched_feed_items_path = topic_dir / "probe" / "matched_feed_items.csv"
        if not matched_feed_items_path.exists():
            continue
        for row in _read_csv(matched_feed_items_path):
            post_uri = str(row.get("post_uri", "")).strip()
            if not post_uri:
                continue
            cluster_id = post_to_cluster.get((topic, post_uri))
            if not cluster_id:
                continue
            label = labeled_clusters.get((topic, cluster_id))
            if label is None:
                continue
            viewer_mode = str(row.get("viewer_mode", "")).strip()
            surface = str(row.get("surface", "")).strip()
            bucket = str(row.get("bucket", "")).strip()
            group_key = (topic, label.frame_label, label.event_guess, viewer_mode, surface, bucket)
            group_sets = exposure_group_sets.setdefault(
                group_key,
                {"posts": set(), "feeds": set(), "clusters": set(), "rows": set()},
            )
            group_sets["posts"].add(post_uri)
            feed_uri = str(row.get("feed_uri", "")).strip()
            if feed_uri:
                group_sets["feeds"].add(feed_uri)
            group_sets["clusters"].add(cluster_id)
            row_fingerprint = json.dumps(
                {
                    "post_uri": post_uri,
                    "viewer_mode": viewer_mode,
                    "surface": surface,
                    "bucket": bucket,
                    "feed_uri": feed_uri,
                    "rank": str(row.get("rank", "")),
                    "captured_at_utc": str(row.get("captured_at_utc", "")),
                    "snapshot_hour_utc": str(row.get("snapshot_hour_utc", "")),
                    "hour_utc": str(row.get("hour_utc", "")),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            group_sets["rows"].add(row_fingerprint)

            overall_key = (topic, label.frame_label, label.event_guess)
            overall_sets = overall_exposure_sets.setdefault(
                overall_key,
                {"posts": set(), "clusters": set(), "rows": set()},
            )
            overall_sets["posts"].add(post_uri)
            overall_sets["clusters"].add(cluster_id)
            overall_sets["rows"].add(row_fingerprint)

    supply_rows: list[dict[str, object]] = []
    for value in supply_by_frame.values():
        supply_posts_set = value.pop("supply_posts_set")
        assert isinstance(supply_posts_set, set)
        supply_rows.append(
            {
                **value,
                "supply_posts": len(supply_posts_set),
            }
        )
    supply_rows.sort(key=lambda row: (str(row["topic"]), str(row["frame_label"]), str(row["event_guess"])))

    exposure_rows: list[dict[str, object]] = []
    for key, sets in sorted(exposure_group_sets.items()):
        topic, frame_label, event_guess, viewer_mode, surface, bucket = key
        exposure_rows.append(
            {
                "topic": topic,
                "frame_label": frame_label,
                "event_guess": event_guess,
                "viewer_mode": viewer_mode,
                "surface": surface,
                "bucket": bucket,
                "exposure_rows": len(sets["rows"]),
                "unique_exposed_posts": len(sets["posts"]),
                "unique_exposed_feeds": len(sets["feeds"]),
                "exposed_clusters": len(sets["clusters"]),
            }
        )

    overall_rows: list[dict[str, object]] = []
    for supply_row in supply_rows:
        overall_key = (
            str(supply_row["topic"]),
            str(supply_row["frame_label"]),
            str(supply_row["event_guess"]),
        )
        sets = overall_exposure_sets.get(overall_key, {"posts": set(), "clusters": set(), "rows": set()})
        supply_posts = int(supply_row["supply_posts"])
        exposure_count = len(sets["rows"])
        unique_exposed_posts = len(sets["posts"])
        overall_rows.append(
            {
                **supply_row,
                "exposure_rows": exposure_count,
                "unique_exposed_posts": unique_exposed_posts,
                "exposed_clusters": len(sets["clusters"]),
                "exposure_rows_per_supply_post": (
                    round(exposure_count / supply_posts, 6) if supply_posts > 0 else ""
                ),
                "exposed_post_share": (
                    round(unique_exposed_posts / supply_posts, 6) if supply_posts > 0 else ""
                ),
            }
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(
        out_dir / "cluster_label_summary.csv",
        fieldnames=[
            "topic",
            "cluster_id",
            "event_guess",
            "frame_label",
            "label_confidence",
            "label_row_count",
            "supply_posts",
        ],
        rows=cluster_rows,
    )
    _write_csv(
        out_dir / "frame_supply_by_topic.csv",
        fieldnames=[
            "topic",
            "frame_label",
            "event_guess",
            "supply_clusters",
            "supply_posts",
            "label_row_count",
        ],
        rows=supply_rows,
    )
    _write_csv(
        out_dir / "frame_exposure_by_topic.csv",
        fieldnames=[
            "topic",
            "frame_label",
            "event_guess",
            "viewer_mode",
            "surface",
            "bucket",
            "exposure_rows",
            "unique_exposed_posts",
            "unique_exposed_feeds",
            "exposed_clusters",
        ],
        rows=exposure_rows,
    )
    _write_csv(
        out_dir / "frame_overall_exposure_vs_supply.csv",
        fieldnames=[
            "topic",
            "frame_label",
            "event_guess",
            "supply_clusters",
            "supply_posts",
            "label_row_count",
            "exposure_rows",
            "unique_exposed_posts",
            "exposed_clusters",
            "exposure_rows_per_supply_post",
            "exposed_post_share",
        ],
        rows=overall_rows,
    )

    summary = {
        "batch_dir": str(batch_dir),
        "label_rows_path": str(label_rows_path),
        "labeled_clusters": len(labeled_clusters),
        "topics": sorted({label.topic for label in labeled_clusters.values()}),
        "frames": sorted({label.frame_label for label in labeled_clusters.values()}),
        "frame_supply_rows": len(supply_rows),
        "frame_exposure_rows": len(exposure_rows),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


__all__ = ["ClusterLabel", "generate_frame_exposure_supply_tables"]
