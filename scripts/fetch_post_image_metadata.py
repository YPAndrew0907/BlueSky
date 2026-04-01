#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bsky_collector_v2.post_image_metadata import (
    fetch_posts_batch,
    extract_post_image_metadata,
    load_study_sample_post_uris,
)


@dataclass(frozen=True)
class Config:
    root: Path
    study_id: str
    limit: int
    batch_size: int
    appview_host: str
    out_json: Path
    include_non_image_posts: bool
    timeout_s: float


def parse_args() -> Config:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch live post image metadata from Bluesky AppView for a small sample "
            "of post URIs in a micro study."
        )
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("/Volumes/T9/BlueSky/data_v2_full"),
    )
    parser.add_argument(
        "--study-id",
        default="micro10_full_live_20260319",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="How many unique post URIs to sample from the study feed_items archive.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=10,
        help="How many post URIs to fetch per app.bsky.feed.getPosts request.",
    )
    parser.add_argument(
        "--appview-host",
        default="https://public.api.bsky.app",
    )
    parser.add_argument(
        "--out-json",
        type=Path,
        default=Path("/Volumes/T9/BlueSky/output/post_image_metadata_sample.json"),
    )
    parser.add_argument(
        "--include-non-image-posts",
        action="store_true",
        help="Keep posts with zero extracted images in the output payload.",
    )
    parser.add_argument(
        "--timeout-s",
        type=float,
        default=60.0,
    )
    args = parser.parse_args()
    return Config(
        root=args.root,
        study_id=str(args.study_id),
        limit=int(args.limit),
        batch_size=int(args.batch_size),
        appview_host=str(args.appview_host),
        out_json=args.out_json,
        include_non_image_posts=bool(args.include_non_image_posts),
        timeout_s=float(args.timeout_s),
    )


def batched(items: list[str], size: int) -> list[list[str]]:
    if size <= 0:
        raise ValueError(f"batch_size must be positive: {size}")
    return [items[idx : idx + size] for idx in range(0, len(items), size)]


def run(config: Config) -> dict[str, Any]:
    sample_uris = load_study_sample_post_uris(
        config.root,
        config.study_id,
        limit=config.limit,
    )
    posts: list[dict[str, Any]] = []
    for batch in batched(sample_uris, config.batch_size):
        posts.extend(
            fetch_posts_batch(
                config.appview_host,
                batch,
                timeout_s=config.timeout_s,
            )
        )

    embed_type_counts: Counter[str] = Counter()
    records = []
    for post_payload in posts:
        metadata = extract_post_image_metadata(post_payload)
        embed_type_counts[metadata.embed_type] += 1
        if metadata.image_count == 0 and not config.include_non_image_posts:
            continue
        records.append(metadata.to_dict())

    summary = {
        "study_id": config.study_id,
        "sample_size_requested": config.limit,
        "sample_size_loaded": len(sample_uris),
        "sample_size_fetched": len(posts),
        "posts_with_extracted_images": sum(1 for row in records if row["image_count"] > 0),
        "embed_type_counts": dict(embed_type_counts),
        "records": records,
    }
    config.out_json.parent.mkdir(parents=True, exist_ok=True)
    config.out_json.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def main() -> None:
    config = parse_args()
    result = run(config)
    preview = {
        "study_id": result["study_id"],
        "sample_size_requested": result["sample_size_requested"],
        "sample_size_loaded": result["sample_size_loaded"],
        "sample_size_fetched": result["sample_size_fetched"],
        "posts_with_extracted_images": result["posts_with_extracted_images"],
        "embed_type_counts": result["embed_type_counts"],
        "preview_records": result["records"][:3],
    }
    print(json.dumps(preview, indent=2))


if __name__ == "__main__":
    main()
