from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path


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


def merge_annotations(*, annotation_dir: Path, out_dir: Path) -> dict[str, object]:
    files = sorted(annotation_dir.glob("*_annotations.csv"))
    if not files:
        raise FileNotFoundError(f"no annotation files found in {annotation_dir}")

    all_rows: list[dict[str, str]] = []
    for path in files:
        all_rows.extend(_read_csv(path))

    _write_csv(
        out_dir / "annotations_merged.csv",
        fieldnames=list(all_rows[0].keys()),
        rows=all_rows,
    )

    by_topic_frame: Counter[tuple[str, str]] = Counter()
    by_topic_conf: Counter[tuple[str, str]] = Counter()
    event_examples: defaultdict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in all_rows:
        topic = row.get("topic", "")
        frame = row.get("frame_label", "")
        conf = row.get("label_confidence", "")
        event = row.get("event_guess", "")
        by_topic_frame[(topic, frame)] += 1
        by_topic_conf[(topic, conf)] += 1
        if len(event_examples[(topic, event)]) < 3:
            event_examples[(topic, event)].append(row)

    frame_rows = [
        {"topic": topic, "frame_label": frame, "count": count}
        for (topic, frame), count in sorted(by_topic_frame.items(), key=lambda item: (item[0][0], -item[1], item[0][1]))
    ]
    conf_rows = [
        {"topic": topic, "label_confidence": conf, "count": count}
        for (topic, conf), count in sorted(by_topic_conf.items(), key=lambda item: (item[0][0], -item[1], item[0][1]))
    ]
    event_rows: list[dict[str, object]] = []
    for (topic, event), rows in sorted(event_examples.items()):
        event_rows.append(
            {
                "topic": topic,
                "event_guess": event,
                "example_n": len(rows),
                "example_ids": "|".join(row.get("example_id", "") for row in rows),
                "example_text_1": rows[0].get("text", "")[:220] if len(rows) > 0 else "",
                "example_text_2": rows[1].get("text", "")[:220] if len(rows) > 1 else "",
                "example_text_3": rows[2].get("text", "")[:220] if len(rows) > 2 else "",
            }
        )

    _write_csv(out_dir / "frame_counts_by_topic.csv", fieldnames=["topic", "frame_label", "count"], rows=frame_rows)
    _write_csv(out_dir / "confidence_counts_by_topic.csv", fieldnames=["topic", "label_confidence", "count"], rows=conf_rows)
    _write_csv(
        out_dir / "event_examples_by_topic.csv",
        fieldnames=["topic", "event_guess", "example_n", "example_ids", "example_text_1", "example_text_2", "example_text_3"],
        rows=event_rows,
    )

    summary = {
        "annotation_dir": str(annotation_dir),
        "files": [str(path) for path in files],
        "annotation_rows": len(all_rows),
        "topics": sorted({row.get("topic", "") for row in all_rows}),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Merge topic annotation CSVs into a single demo dataset.")
    parser.add_argument("--annotation-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    summary = merge_annotations(annotation_dir=args.annotation_dir, out_dir=args.out_dir)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
