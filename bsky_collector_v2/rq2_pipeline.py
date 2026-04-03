from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bsky_collector_v2.annotation_merge import merge_annotations
from bsky_collector_v2.annotation_sampling import TopicSamplingConfig, build_candidate_pool
from bsky_collector_v2.cluster_label_apply import apply_cluster_labels
from bsky_collector_v2.rq2_frame_tables import generate_frame_exposure_supply_tables
from bsky_collector_v2.topic_batch import run_topic_batch
from bsky_collector_v2.topic_presets import TopicSpec, get_topic_preset

DEFAULT_CLUSTER_EXCLUDE_PATTERNS: tuple[str, ...] = (
    r"^\s*Bluesky.?s Top 10 Trending Words",
    r"^\s*Here are the #Top10 trending hashtags on #Bluesky",
)


@dataclass(frozen=True)
class Rq2PipelineConfig:
    data_root: Path
    out_dir: Path
    annotation_dir: Path
    preset: str = "politics_v1"
    topics: tuple[str, ...] = ()
    surfaces: tuple[str, ...] = ("hourly", "wide")
    start_date: str | None = None
    end_date: str | None = None
    include_labelerexp: bool = False
    run_topic_batch: bool = True
    run_sampling: bool = True
    run_label_application: bool = True
    run_annotation_merge: bool = True
    run_frame_table: bool = True
    run_clustering: bool = True
    cluster_anchor_kinds: tuple[str, ...] = ("tokens",)
    cluster_exclude_text_patterns: tuple[str, ...] = DEFAULT_CLUSTER_EXCLUDE_PATTERNS
    cluster_time_window_hours: int = 12
    cluster_min_size: int = 2
    max_clusters: int = 25
    per_cluster: int = 4


def _resolve_topics(cfg: Rq2PipelineConfig) -> list[TopicSpec]:
    topics = list(get_topic_preset(cfg.preset))
    if cfg.topics:
        selected = set(cfg.topics)
        topics = [topic for topic in topics if topic.slug in selected]
    if not topics:
        raise ValueError(f"no topics selected for preset={cfg.preset}")
    return topics


def _cluster_label_files(annotation_dir: Path) -> list[Path]:
    return sorted(
        path
        for path in annotation_dir.glob("*_cluster_labels*.csv")
        if path.is_file() and not path.name.startswith("._")
    )


def _annotation_files(annotation_dir: Path) -> list[Path]:
    return sorted(path for path in annotation_dir.glob("*_annotations.csv") if path.is_file())


def _step(status: str, **extra: Any) -> dict[str, Any]:
    payload = {"status": status}
    payload.update(extra)
    return payload


def run_rq2_pipeline(cfg: Rq2PipelineConfig) -> dict[str, Any]:
    topics = _resolve_topics(cfg)
    out_dir = cfg.out_dir
    annotation_dir = cfg.annotation_dir
    batch_dir = out_dir / "topic_batch"
    applied_dir = out_dir / "label_application"
    merged_dir = out_dir / "merged_annotations"
    frame_dir = out_dir / "frame_tables"

    out_dir.mkdir(parents=True, exist_ok=True)
    annotation_dir.mkdir(parents=True, exist_ok=True)

    summary: dict[str, Any] = {
        "preset": cfg.preset,
        "topics": [topic.slug for topic in topics],
        "surfaces": list(cfg.surfaces),
        "start_date": cfg.start_date,
        "end_date": cfg.end_date,
        "include_labelerexp": cfg.include_labelerexp,
        "batch_dir": str(batch_dir),
        "annotation_dir": str(annotation_dir),
        "steps": {},
    }

    if cfg.run_topic_batch:
        topic_batch_summary = run_topic_batch(
            data_root=cfg.data_root,
            out_dir=batch_dir,
            topics=topics,
            surfaces=list(cfg.surfaces),
            start_date=cfg.start_date,
            end_date=cfg.end_date,
            include_labelerexp=cfg.include_labelerexp,
            run_clustering=cfg.run_clustering,
            cluster_anchor_kinds=list(cfg.cluster_anchor_kinds),
            cluster_exclude_text_patterns=list(cfg.cluster_exclude_text_patterns),
            cluster_time_window_hours=int(cfg.cluster_time_window_hours),
            cluster_min_size=int(cfg.cluster_min_size),
        )
        summary["steps"]["topic_batch"] = _step("completed", summary=topic_batch_summary)
    else:
        summary["steps"]["topic_batch"] = _step("skipped", reason="run_topic_batch=false")

    if cfg.run_sampling:
        candidate_counts = build_candidate_pool(
            configs=[
                TopicSamplingConfig(
                    topic=topic.slug,
                    cluster_summary=batch_dir / topic.slug / "clusters" / "cluster_summary.csv",
                    cluster_membership=batch_dir / topic.slug / "clusters" / "cluster_membership.csv",
                    max_clusters=int(cfg.max_clusters),
                    per_cluster=int(cfg.per_cluster),
                )
                for topic in topics
            ],
            out_dir=annotation_dir,
        )
        summary["steps"]["annotation_sampling"] = _step("completed", candidate_counts=candidate_counts)
    else:
        summary["steps"]["annotation_sampling"] = _step("skipped", reason="run_sampling=false")

    if cfg.run_label_application:
        label_files = _cluster_label_files(annotation_dir)
        if label_files and (annotation_dir / "annotation_candidates_all.csv").exists():
            apply_summary = apply_cluster_labels(annotation_dir=annotation_dir, out_dir=applied_dir)
            summary["steps"]["cluster_label_apply"] = _step("completed", summary=apply_summary)
        else:
            summary["steps"]["cluster_label_apply"] = _step(
                "skipped",
                reason="missing annotation_candidates_all.csv or *_cluster_labels*.csv",
            )
    else:
        summary["steps"]["cluster_label_apply"] = _step("skipped", reason="run_label_application=false")

    if cfg.run_annotation_merge:
        annotation_files = _annotation_files(annotation_dir)
        if annotation_files:
            merge_summary = merge_annotations(annotation_dir=annotation_dir, out_dir=merged_dir)
            summary["steps"]["annotation_merge"] = _step("completed", summary=merge_summary)
        else:
            summary["steps"]["annotation_merge"] = _step("skipped", reason="no *_annotations.csv files found")
    else:
        summary["steps"]["annotation_merge"] = _step("skipped", reason="run_annotation_merge=false")

    label_rows_path: Path | None = None
    merged_rows = merged_dir / "annotations_merged.csv"
    applied_rows = applied_dir / "annotation_demo_labeled_examples.csv"
    if merged_rows.exists():
        label_rows_path = merged_rows
    elif applied_rows.exists():
        label_rows_path = applied_rows

    if cfg.run_frame_table:
        if label_rows_path is None:
            summary["steps"]["frame_tables"] = _step(
                "skipped",
                reason="no merged annotations or applied label rows available",
            )
        else:
            frame_summary = generate_frame_exposure_supply_tables(
                batch_dir=batch_dir,
                label_rows_path=label_rows_path,
                out_dir=frame_dir,
            )
            summary["steps"]["frame_tables"] = _step(
                "completed",
                label_rows_path=str(label_rows_path),
                summary=frame_summary,
            )
    else:
        summary["steps"]["frame_tables"] = _step("skipped", reason="run_frame_table=false")

    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the canonical RQ2 topic/frame workflow.")
    parser.add_argument("--data-root", type=Path, default=Path("/Volumes/T9/BlueSky/data_v2_full"))
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--annotation-dir", type=Path, default=None)
    parser.add_argument("--preset", type=str, default="politics_v1")
    parser.add_argument("--topic", action="append", default=[])
    parser.add_argument("--surface", action="append", default=[])
    parser.add_argument("--start-date", type=str, default=None)
    parser.add_argument("--end-date", type=str, default=None)
    parser.add_argument("--include-labelerexp", action="store_true")
    parser.add_argument("--run-topic-batch", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--run-sampling", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--run-label-application", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--run-annotation-merge", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--run-frame-table", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--run-clustering", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--cluster-anchor-kind", action="append", default=[])
    parser.add_argument("--cluster-exclude-text-pattern", action="append", default=[])
    parser.add_argument("--cluster-time-window-hours", type=int, default=12)
    parser.add_argument("--cluster-min-size", type=int, default=2)
    parser.add_argument("--max-clusters", type=int, default=25)
    parser.add_argument("--per-cluster", type=int, default=4)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    out_dir = Path(args.out_dir)
    annotation_dir = Path(args.annotation_dir) if args.annotation_dir else (out_dir / "annotations")
    summary = run_rq2_pipeline(
        Rq2PipelineConfig(
            data_root=Path(args.data_root),
            out_dir=out_dir,
            annotation_dir=annotation_dir,
            preset=str(args.preset),
            topics=tuple(str(item) for item in args.topic),
            surfaces=tuple(dict.fromkeys(str(item) for item in (args.surface or ["hourly", "wide"]))),
            start_date=args.start_date,
            end_date=args.end_date,
            include_labelerexp=bool(args.include_labelerexp),
            run_topic_batch=bool(args.run_topic_batch),
            run_sampling=bool(args.run_sampling),
            run_label_application=bool(args.run_label_application),
            run_annotation_merge=bool(args.run_annotation_merge),
            run_frame_table=bool(args.run_frame_table),
            run_clustering=bool(args.run_clustering),
            cluster_anchor_kinds=tuple(dict.fromkeys(str(item) for item in (args.cluster_anchor_kind or ["tokens"]))),
            cluster_exclude_text_patterns=tuple(
                dict.fromkeys(
                    str(item) for item in (args.cluster_exclude_text_pattern or list(DEFAULT_CLUSTER_EXCLUDE_PATTERNS))
                )
            ),
            cluster_time_window_hours=int(args.cluster_time_window_hours),
            cluster_min_size=int(args.cluster_min_size),
            max_clusters=int(args.max_clusters),
            per_cluster=int(args.per_cluster),
        )
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
