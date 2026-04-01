from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TopicSamplingConfig:
    topic: str
    cluster_summary: Path
    cluster_membership: Path
    max_clusters: int
    per_cluster: int
    exclude_anchor_prefixes: tuple[str, ...] = ()


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


def build_candidate_pool(*, configs: list[TopicSamplingConfig], out_dir: Path) -> dict[str, int]:
    out_dir.mkdir(parents=True, exist_ok=True)
    all_rows: list[dict[str, object]] = []
    counts: dict[str, int] = {}

    for config in configs:
        summary_rows = _read_csv(config.cluster_summary)
        membership_rows = _read_csv(config.cluster_membership)
        chosen_clusters: list[dict[str, str]] = []
        for row in summary_rows:
            anchor_value = row.get("anchor_value", "")
            if any(anchor_value.startswith(prefix) for prefix in config.exclude_anchor_prefixes):
                continue
            if not row.get("example_text_1", "").strip():
                continue
            chosen_clusters.append(row)
            if len(chosen_clusters) >= config.max_clusters:
                break
        cluster_lookup = {
            row["cluster_id"]: {
                "summary": row,
                "members": [],
            }
            for row in chosen_clusters
        }
        for row in membership_rows:
            cluster_id = row.get("cluster_id", "")
            if cluster_id not in cluster_lookup:
                continue
            bucket = cluster_lookup[cluster_id]["members"]
            if len(bucket) < config.per_cluster:
                bucket.append(row)

        out_rows: list[dict[str, object]] = []
        example_counter = 1
        for cluster_rank, cluster in enumerate(chosen_clusters, start=1):
            cluster_id = cluster["cluster_id"]
            for member_rank, member in enumerate(cluster_lookup[cluster_id]["members"], start=1):
                out_rows.append(
                    {
                        "example_id": f"{config.topic}_{example_counter:03d}",
                        "topic": config.topic,
                        "cluster_rank": cluster_rank,
                        "member_rank": member_rank,
                        "cluster_id": cluster_id,
                        "anchor_value": cluster.get("anchor_value", ""),
                        "cluster_post_n": cluster.get("post_n", ""),
                        "cluster_exposure_rows": cluster.get("exposure_rows", ""),
                        "post_uri": member.get("post_uri", ""),
                        "author_handle": member.get("author_handle", ""),
                        "record_created_at": member.get("record_created_at", ""),
                        "matched_queries": member.get("matched_queries", ""),
                        "text": member.get("text", ""),
                    }
                )
                example_counter += 1
        counts[config.topic] = len(out_rows)
        _write_csv(
            out_dir / f"{config.topic}_candidate_examples.csv",
            fieldnames=list(out_rows[0].keys()) if out_rows else [
                "example_id",
                "topic",
                "cluster_rank",
                "member_rank",
                "cluster_id",
                "anchor_value",
                "cluster_post_n",
                "cluster_exposure_rows",
                "post_uri",
                "author_handle",
                "record_created_at",
                "matched_queries",
                "text",
            ],
            rows=out_rows,
        )
        all_rows.extend(out_rows)

    if all_rows:
        _write_csv(
            out_dir / "annotation_candidates_all.csv",
            fieldnames=list(all_rows[0].keys()),
            rows=all_rows,
        )
    return counts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sample annotation candidates from topic cluster outputs.")
    parser.add_argument("--batch-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--topic", action="append", required=True)
    parser.add_argument("--max-clusters", type=int, default=25)
    parser.add_argument("--per-cluster", type=int, default=4)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    configs = [
        TopicSamplingConfig(
            topic=topic,
            cluster_summary=args.batch_dir / topic / "clusters" / "cluster_summary.csv",
            cluster_membership=args.batch_dir / topic / "clusters" / "cluster_membership.csv",
            max_clusters=int(args.max_clusters),
            per_cluster=int(args.per_cluster),
        )
        for topic in args.topic
    ]
    counts = build_candidate_pool(configs=configs, out_dir=args.out_dir)
    for topic, count in counts.items():
        print(topic, count)


if __name__ == "__main__":
    main()
