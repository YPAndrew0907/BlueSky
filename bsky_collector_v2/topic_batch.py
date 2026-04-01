from __future__ import annotations

import argparse
import csv
import json
from dataclasses import asdict
from pathlib import Path

from bsky_collector_v2.content_bias import cluster_topic_probe
from bsky_collector_v2.topic_presets import TopicSpec, get_topic_preset
from bsky_collector_v2.topic_probe import run_probe


def _write_rows(path: Path, *, fieldnames: list[str], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def run_topic_batch(
    *,
    data_root: Path,
    out_dir: Path,
    topics: list[TopicSpec],
    surfaces: list[str],
    start_date: str | None,
    end_date: str | None,
    include_labelerexp: bool,
    run_clustering: bool,
    cluster_anchor_kinds: list[str],
    cluster_exclude_text_patterns: list[str],
    cluster_time_window_hours: int,
    cluster_min_size: int,
) -> dict[str, object]:
    out_dir.mkdir(parents=True, exist_ok=True)
    topic_rows: list[dict[str, object]] = []
    manifest_rows: list[dict[str, object]] = []
    markdown_lines: list[str] = [
        "# Topic Batch Report",
        "",
        f"- Date range: `{start_date}` to `{end_date}`",
        f"- Surfaces: `{', '.join(surfaces)}`",
        f"- Include `labelerexp`: `{include_labelerexp}`",
        "",
    ]

    for topic in topics:
        topic_dir = out_dir / topic.slug
        probe_dir = topic_dir / "probe"
        probe_summary = run_probe(
            data_root=data_root,
            out_dir=probe_dir,
            queries=list(topic.include_queries),
            exclude_queries=list(topic.exclude_queries),
            surfaces=surfaces,
            start_date=start_date,
            end_date=end_date,
            include_labelerexp=include_labelerexp,
        )

        cluster_summary: dict[str, object] | None = None
        if run_clustering:
            cluster_dir = topic_dir / "clusters"
            topic_cluster_anchor_kinds = list(topic.cluster_anchor_kinds) or cluster_anchor_kinds
            topic_cluster_exclude_patterns = list(dict.fromkeys([
                *cluster_exclude_text_patterns,
                *topic.cluster_exclude_text_patterns,
            ]))
            cluster_summary = cluster_topic_probe(
                probe_dir=probe_dir,
                out_dir=cluster_dir,
                time_window_hours=cluster_time_window_hours,
                min_cluster_size=cluster_min_size,
                allowed_anchor_kinds=topic_cluster_anchor_kinds,
                exclude_text_patterns=topic_cluster_exclude_patterns,
            )

        row = {
            "slug": topic.slug,
            "label": topic.label,
            "matched_posts": probe_summary["matched_posts"],
            "matched_feed_rows": probe_summary["matched_feed_rows"],
            "matched_label_rows": probe_summary["matched_label_rows"],
            "matched_metrics_rows": probe_summary["matched_metrics_rows"],
            "unique_feeds_seen": probe_summary["unique_feeds_seen"],
            "unique_snapshots_seen": probe_summary["unique_snapshots_seen"],
            "auth_exposure_rows": probe_summary["exposure_by_viewer"].get("auth", 0),
            "unauth_exposure_rows": probe_summary["exposure_by_viewer"].get("unauth", 0),
            "popular_by_likecount_rows": probe_summary["exposure_by_bucket"].get("popular_by_likecount", 0),
            "longtail_random_rows": probe_summary["exposure_by_bucket"].get("longtail_random", 0),
            "wide_sweep_rows": probe_summary["exposure_by_bucket"].get("wide_sweep", 0),
            "probe_dir": str(probe_dir),
            "clusters_retained": cluster_summary["clusters_retained"] if cluster_summary else "",
            "clustered_posts": cluster_summary["clustered_posts"] if cluster_summary else "",
            "cluster_dir": str(topic_dir / "clusters") if cluster_summary else "",
            "notes": topic.notes,
        }
        topic_rows.append(row)
        manifest_rows.append(
            {
                **asdict(topic),
                "probe_summary_path": str(probe_dir / "summary.json"),
                "cluster_summary_path": str((topic_dir / "clusters" / "summary.json")) if cluster_summary else "",
            }
        )
        markdown_lines.extend(
            [
                f"## {topic.label}",
                "",
                f"- `matched_posts`: {probe_summary['matched_posts']}",
                f"- `matched_feed_rows`: {probe_summary['matched_feed_rows']}",
                f"- `unique_feeds_seen`: {probe_summary['unique_feeds_seen']}",
                f"- `auth_exposure_rows`: {probe_summary['exposure_by_viewer'].get('auth', 0)}",
                f"- `unauth_exposure_rows`: {probe_summary['exposure_by_viewer'].get('unauth', 0)}",
                f"- `clusters_retained`: {cluster_summary['clusters_retained'] if cluster_summary else 0}",
                f"- Notes: {topic.notes}",
                "",
            ]
        )
        if cluster_summary:
            cluster_csv = topic_dir / "clusters" / "cluster_summary.csv"
            if cluster_csv.exists():
                with cluster_csv.open("r", newline="", encoding="utf-8") as fh:
                    reader = csv.DictReader(fh)
                    shown = 0
                    for cluster_row in reader:
                        example = str(cluster_row.get("example_text_1", "")).strip()
                        if not example:
                            continue
                        markdown_lines.append(
                            f"- Cluster `{cluster_row['anchor_value']}`: post_n={cluster_row['post_n']}, exposure_rows={cluster_row['exposure_rows']}, feeds={cluster_row['unique_feeds']}"
                        )
                        markdown_lines.append(f"  Example: {example[:220]}")
                        shown += 1
                        if shown >= 3:
                            break
                markdown_lines.append("")

    _write_rows(
        out_dir / "topic_batch_summary.csv",
        fieldnames=[
            "slug",
            "label",
            "matched_posts",
            "matched_feed_rows",
            "matched_label_rows",
            "matched_metrics_rows",
            "unique_feeds_seen",
            "unique_snapshots_seen",
            "auth_exposure_rows",
            "unauth_exposure_rows",
            "popular_by_likecount_rows",
            "longtail_random_rows",
            "wide_sweep_rows",
            "probe_dir",
            "clusters_retained",
            "clustered_posts",
            "cluster_dir",
            "notes",
        ],
        rows=topic_rows,
    )
    _write_rows(
        out_dir / "topic_manifest.csv",
        fieldnames=[
            "slug",
            "label",
            "include_queries",
            "exclude_queries",
            "notes",
            "probe_summary_path",
            "cluster_summary_path",
        ],
        rows=[
            {
                **row,
                "include_queries": "|".join(row["include_queries"]),
                "exclude_queries": "|".join(row["exclude_queries"]),
            }
            for row in manifest_rows
        ],
    )

    summary = {
        "topics": [topic.slug for topic in topics],
        "start_date": start_date,
        "end_date": end_date,
        "surfaces": surfaces,
        "include_labelerexp": include_labelerexp,
        "run_clustering": run_clustering,
        "cluster_anchor_kinds": cluster_anchor_kinds,
        "cluster_exclude_text_patterns": cluster_exclude_text_patterns,
        "topic_count": len(topics),
        "batch_dir": str(out_dir),
        "totals": {
            "matched_posts": sum(int(row["matched_posts"]) for row in topic_rows),
            "matched_feed_rows": sum(int(row["matched_feed_rows"]) for row in topic_rows),
            "auth_exposure_rows": sum(int(row["auth_exposure_rows"]) for row in topic_rows),
            "unauth_exposure_rows": sum(int(row["unauth_exposure_rows"]) for row in topic_rows),
        },
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out_dir / "topic_batch_report.md").write_text("\n".join(markdown_lines) + "\n", encoding="utf-8")
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Batch topic probing and event clustering over Bluesky archive data.")
    parser.add_argument("--data-root", type=Path, default=Path("/Volumes/T9/BlueSky/data_v2_full"))
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--preset", type=str, default="politics_v1")
    parser.add_argument("--topic", action="append", default=[], help="Optional slug filter within the preset.")
    parser.add_argument("--surface", action="append", default=["hourly", "wide"])
    parser.add_argument("--start-date", type=str, default=None)
    parser.add_argument("--end-date", type=str, default=None)
    parser.add_argument("--include-labelerexp", action="store_true")
    parser.add_argument("--skip-clustering", action="store_true")
    parser.add_argument("--cluster-anchor-kind", action="append", default=["tokens"])
    parser.add_argument(
        "--cluster-exclude-text-pattern",
        action="append",
        default=[r"^\s*Bluesky.?s Top 10 Trending Words", r"^\s*Here are the #Top10 trending hashtags on #Bluesky"],
    )
    parser.add_argument("--cluster-time-window-hours", type=int, default=12)
    parser.add_argument("--cluster-min-size", type=int, default=2)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    topics = list(get_topic_preset(args.preset))
    if args.topic:
        topic_filter = set(args.topic)
        topics = [topic for topic in topics if topic.slug in topic_filter]
    if not topics:
        raise ValueError("no topics selected")

    summary = run_topic_batch(
        data_root=args.data_root,
        out_dir=args.out_dir,
        topics=topics,
        surfaces=list(dict.fromkeys(args.surface)),
        start_date=args.start_date,
        end_date=args.end_date,
        include_labelerexp=bool(args.include_labelerexp),
        run_clustering=not bool(args.skip_clustering),
        cluster_anchor_kinds=list(dict.fromkeys(args.cluster_anchor_kind)),
        cluster_exclude_text_patterns=list(dict.fromkeys(args.cluster_exclude_text_pattern)),
        cluster_time_window_hours=int(args.cluster_time_window_hours),
        cluster_min_size=int(args.cluster_min_size),
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
