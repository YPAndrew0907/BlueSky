from __future__ import annotations

import argparse
import csv
import json
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


def apply_cluster_labels(*, annotation_dir: Path, out_dir: Path) -> dict[str, object]:
    candidate_path = annotation_dir / "annotation_candidates_all.csv"
    if not candidate_path.exists():
        raise FileNotFoundError(f"missing candidate table: {candidate_path}")
    candidate_rows = _read_csv(candidate_path)

    label_files = sorted(
        path
        for path in annotation_dir.glob("*_cluster_labels*.csv")
        if not path.name.startswith("._")
    )
    if not label_files:
        raise FileNotFoundError(f"no cluster label files found in {annotation_dir}")

    label_map: dict[tuple[str, str], dict[str, str]] = {}
    for path in label_files:
        for row in _read_csv(path):
            label_map[(row.get("topic", ""), row.get("cluster_id", ""))] = row

    applied_rows: list[dict[str, object]] = []
    applied = 0
    missing = 0
    for row in candidate_rows:
        key = (row.get("topic", ""), row.get("cluster_id", ""))
        label = label_map.get(key)
        merged = dict(row)
        if label is None:
            merged["event_guess"] = ""
            merged["frame_label"] = ""
            merged["label_confidence"] = ""
            merged["rationale_short"] = ""
            missing += 1
        else:
            merged["event_guess"] = label.get("event_guess", "")
            merged["frame_label"] = label.get("frame_label", "")
            merged["label_confidence"] = label.get("label_confidence", "")
            merged["rationale_short"] = label.get("rationale_short", "")
            applied += 1
        applied_rows.append(merged)

    out_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(
        out_dir / "annotation_demo_labeled_examples.csv",
        fieldnames=list(applied_rows[0].keys()),
        rows=applied_rows,
    )

    summary = {
        "annotation_dir": str(annotation_dir),
        "label_files": [str(path) for path in label_files],
        "candidate_rows": len(candidate_rows),
        "applied_rows": applied,
        "missing_rows": missing,
        "topics": sorted({row.get("topic", "") for row in candidate_rows}),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Apply cluster-level labels to candidate example rows.")
    parser.add_argument("--annotation-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    summary = apply_cluster_labels(annotation_dir=args.annotation_dir, out_dir=args.out_dir)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
