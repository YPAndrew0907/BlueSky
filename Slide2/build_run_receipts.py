#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from PIL import Image, ImageDraw, ImageFont


FONT_SANS = Path("/System/Library/Fonts/SFNS.ttf")
FONT_MONO = Path("/System/Library/Fonts/SFNSMono.ttf")

RTL_MARKS_RE = re.compile(r"[\u202A-\u202E\u2066-\u2069]")
ASCII_RE = re.compile(r"^[\x20-\x7E]+$")


COLORS = {
    "ink": "0B0B0F",
    "muted": "4B5563",
    "muted2": "9CA3AF",
    "card": "FFFFFF",
    "shadow": "000000",
    "cyan": "06B6D4",
    "purple": "7C3AED",
    "pink": "DB2777",
    "amber": "D97706",
    "green": "16A34A",
    "slate": "334155",
}


def _clean_text(s: str) -> str:
    return RTL_MARKS_RE.sub("", s).strip()


def _truncate_middle(s: str, max_len: int) -> str:
    s = _clean_text(s)
    if len(s) <= max_len:
        return s
    if max_len < 8:
        return s[:max_len]
    keep_head = (max_len - 3) // 2
    keep_tail = max_len - 3 - keep_head
    return f"{s[:keep_head]}...{s[-keep_tail:]}"


def _compact_did(did: str) -> str:
    did = _clean_text(did)
    if not did.startswith("did:"):
        return _truncate_middle(did, 24)
    if len(did) <= 24:
        return did
    return f"{did[:12]}...{did[-6:]}"


def _compact_cid(cid: str) -> str:
    cid = _clean_text(cid)
    return _truncate_middle(cid, 26)


def _compact_at_uri(uri: str) -> str:
    uri = _clean_text(uri)
    idx = uri.rfind("/app.bsky.")
    if idx != -1:
        return f"...{uri[idx:]}"
    return _truncate_middle(uri, 42)


def _compact_sha256(h: str) -> str:
    h = _clean_text(h)
    if len(h) <= 18:
        return h
    return f"{h[:10]}...{h[-10:]}"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _iter_csv_rows(path: Path) -> Iterator[dict[str, str]]:
    with path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        yield from reader


def _iter_csv_gz_rows(path: Path) -> Iterator[dict[str, str]]:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        yield from reader


def _read_single_row_csv(path: Path) -> dict[str, str]:
    with path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return next(reader)


def _find_row(path: Path, *, key: str, value: str) -> dict[str, str]:
    for row in _iter_csv_rows(path):
        if row.get(key) == value:
            return row
    raise RuntimeError(f"Row not found in {path.name}: {key}={value!r}")


def _find_row_gz(path: Path, *, predicate) -> dict[str, str]:
    for row in _iter_csv_gz_rows(path):
        if predicate(row):
            return row
    raise RuntimeError(f"Row not found in {path.name} (predicate)")


def _find_rows_gz(path: Path, *, predicate, limit: int) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for row in _iter_csv_gz_rows(path):
        if predicate(row):
            out.append(row)
            if len(out) >= limit:
                break
    return out


@dataclass(frozen=True)
class RunPaths:
    run_dir: Path
    csv_dir: Path
    manifest_dir: Path
    logs_dir: Path
    receipts_dir: Path


def _paths(run_dir: Path) -> RunPaths:
    return RunPaths(
        run_dir=run_dir,
        csv_dir=run_dir / "02_csv_exports",
        manifest_dir=run_dir / "05_manifest",
        logs_dir=run_dir / "04_logs",
        receipts_dir=run_dir / "06_figures_preview" / "receipts",
    )


def render_kv_card(
    *,
    out_path: Path,
    title: str,
    items: Sequence[tuple[str, str]],
    accent_hex: str,
    width_px: int = 1400,
    height_px: int = 340,
) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)

    img = Image.new("RGBA", (width_px, height_px), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    pad = 34
    radius = 32
    border = 4

    # Shadow
    shadow = Image.new("RGBA", (width_px, height_px), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.rounded_rectangle(
        (12, 14, width_px - 4, height_px - 4),
        radius=radius,
        fill=f"#{COLORS['shadow']}22",
        outline=None,
    )
    img = Image.alpha_composite(img, shadow)
    draw = ImageDraw.Draw(img)

    # Card
    draw.rounded_rectangle(
        (0, 0, width_px - 1, height_px - 1),
        radius=radius,
        fill=f"#{COLORS['card']}",
        outline=f"#{accent_hex}",
        width=border,
    )

    title_font = ImageFont.truetype(str(FONT_SANS), 30)
    label_font = ImageFont.truetype(str(FONT_SANS), 16)
    value_font = ImageFont.truetype(str(FONT_SANS), 19)
    value_mono = ImageFont.truetype(str(FONT_MONO), 18)

    draw.text((pad, 20), _clean_text(title), fill=f"#{COLORS['ink']}", font=title_font)

    cols = max(2, min(4, len(items)))
    rows = (len(items) + cols - 1) // cols
    col_w = (width_px - 2 * pad) / cols
    base_y = 88
    row_h = 78 if rows <= 2 else 70

    for idx, (k, v) in enumerate(items):
        r = idx // cols
        c = idx % cols
        x = int(pad + c * col_w)
        y = int(base_y + r * row_h)

        draw.text((x, y), _clean_text(k), fill=f"#{COLORS['muted']}", font=label_font)

        vv = _clean_text(v)
        is_mono = any(t in vv for t in ("did:", "at://", "bafy", "sha256", "/app.bsky.", "..."))
        vf = value_mono if is_mono else value_font
        draw.text((x, y + 22), vv, fill=f"#{COLORS['ink']}", font=vf)

    cap = "Receipt (truncated)"
    cap_font = ImageFont.truetype(str(FONT_SANS), 13)
    cap_w = draw.textlength(cap, font=cap_font)
    draw.text((width_px - pad - cap_w, height_px - 28), cap, fill=f"#{COLORS['muted2']}", font=cap_font)

    img.save(out_path)


def render_list_card(
    *,
    out_path: Path,
    title: str,
    lines: Sequence[str],
    accent_hex: str,
    width_px: int = 1400,
    height_px: int = 420,
    max_lines: int = 10,
) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)

    img = Image.new("RGBA", (width_px, height_px), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    pad = 34
    radius = 32
    border = 4

    shadow = Image.new("RGBA", (width_px, height_px), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.rounded_rectangle(
        (12, 14, width_px - 4, height_px - 4),
        radius=radius,
        fill=f"#{COLORS['shadow']}22",
        outline=None,
    )
    img = Image.alpha_composite(img, shadow)
    draw = ImageDraw.Draw(img)

    draw.rounded_rectangle(
        (0, 0, width_px - 1, height_px - 1),
        radius=radius,
        fill=f"#{COLORS['card']}",
        outline=f"#{accent_hex}",
        width=border,
    )

    title_font = ImageFont.truetype(str(FONT_SANS), 30)
    mono = ImageFont.truetype(str(FONT_MONO), 18)
    draw.text((pad, 20), _clean_text(title), fill=f"#{COLORS['ink']}", font=title_font)

    y = 92
    line_h = 28
    for line in lines[:max_lines]:
        draw.text((pad, y), _clean_text(line), fill=f"#{COLORS['ink']}", font=mono)
        y += line_h
        if y > height_px - 44:
            break

    cap = "Excerpt"
    cap_font = ImageFont.truetype(str(FONT_SANS), 13)
    cap_w = draw.textlength(cap, font=cap_font)
    draw.text((width_px - pad - cap_w, height_px - 28), cap, fill=f"#{COLORS['muted2']}", font=cap_font)

    img.save(out_path)


def pick_named_starterpack_with_inclusion(paths: RunPaths) -> tuple[dict[str, str], dict[str, str]]:
    name_map: dict[str, dict[str, str]] = {}
    for row in _iter_csv_rows(paths.csv_dir / "starterpacks.csv"):
        name = _clean_text(row.get("name", ""))
        if not name:
            continue
        name_map[row["starterpack_uri"]] = row

    for inc in _iter_csv_rows(paths.csv_dir / "starterpack_feeds.csv"):
        sp_uri = inc.get("starterpack_uri", "")
        sp = name_map.get(sp_uri)
        if not sp:
            continue
        name = _clean_text(sp.get("name", ""))
        if not ASCII_RE.match(name):
            continue
        return sp, inc

    raise RuntimeError("No suitable ASCII-named starter pack with feed inclusion found.")


def _safe_first_row(path: Path) -> dict[str, str]:
    with path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        try:
            return next(reader)
        except StopIteration as err:
            raise RuntimeError(f"Empty CSV: {path.name}") from err


@dataclass(frozen=True)
class LabeledImpression:
    feed_uri: str
    viewer_mode: str
    rank: str
    post_uri: str
    post_cid: str
    author_did: str
    collected_at_utc: str


def _pick_labeled_impression(paths: RunPaths) -> LabeledImpression | None:
    labels_path = paths.csv_dir / "post_labels.csv.gz"
    if not labels_path.exists():
        return None

    for label_row in _iter_csv_gz_rows(labels_path):
        feed_uri = _clean_text(label_row.get("feed_uri", ""))
        viewer_mode = _clean_text(label_row.get("viewer_mode", ""))
        post_uri = _clean_text(label_row.get("post_uri", ""))
        post_cid = _clean_text(label_row.get("post_cid", ""))
        if not (feed_uri and viewer_mode and post_uri and post_cid):
            continue

        try:
            impression_row = _find_row_gz(
                paths.csv_dir / "feed_items.csv.gz",
                predicate=lambda r: r.get("feed_uri") == feed_uri
                and r.get("viewer_mode") == viewer_mode
                and r.get("post_uri") == post_uri
                and r.get("post_cid") == post_cid,
            )
        except RuntimeError:
            continue

        author_did = _clean_text(impression_row.get("author_did", ""))
        if not author_did:
            continue

        return LabeledImpression(
            feed_uri=feed_uri,
            viewer_mode=viewer_mode,
            rank=_clean_text(impression_row.get("rank", "")),
            post_uri=post_uri,
            post_cid=post_cid,
            author_did=author_did,
            collected_at_utc=_clean_text(impression_row.get("collected_at_utc", "")),
        )

    return None


def build_receipts(*, run_dir: Path, skip_zip_receipt: bool) -> dict[str, Path]:
    p = _paths(run_dir)
    p.receipts_dir.mkdir(parents=True, exist_ok=True)

    # --- Deterministic exemplar anchored on the first popular feed row ---
    popular_first = _safe_first_row(p.csv_dir / "popular_feeds.csv")
    exemplar_feed_uri = popular_first["feed_uri"]

    feed_idx = _find_row(p.csv_dir / "feed_generators_index.csv", key="feed_uri", value=exemplar_feed_uri)
    panel_row = _find_row(p.csv_dir / "feed_panel.csv", key="feed_uri", value=exemplar_feed_uri)

    # Prefer an exemplar which is guaranteed to have label rows, so the deck can
    # show a concrete labels example (instead of “no label rows…”).
    labeled = _pick_labeled_impression(p)
    if labeled is None:
        impression_row = _find_row_gz(
            p.csv_dir / "feed_items.csv.gz",
            predicate=lambda r: r.get("feed_uri") == exemplar_feed_uri
            and r.get("viewer_mode") == "unauth"
            and r.get("rank") == "1",
        )
        label_feed_uri = exemplar_feed_uri
        label_viewer_mode = "unauth"
        post_uri = impression_row["post_uri"]
        post_cid = impression_row["post_cid"]
        author_did = impression_row["author_did"]
    else:
        impression_row = _find_row_gz(
            p.csv_dir / "feed_items.csv.gz",
            predicate=lambda r: r.get("feed_uri") == labeled.feed_uri
            and r.get("viewer_mode") == labeled.viewer_mode
            and r.get("post_uri") == labeled.post_uri
            and r.get("post_cid") == labeled.post_cid,
        )
        label_feed_uri = labeled.feed_uri
        label_viewer_mode = labeled.viewer_mode
        post_uri = labeled.post_uri
        post_cid = labeled.post_cid
        author_did = labeled.author_did

    post_row = _find_row_gz(
        p.csv_dir / "posts.csv.gz",
        predicate=lambda r: r.get("post_uri") == post_uri and r.get("post_cid") == post_cid,
    )
    label_rows = _find_rows_gz(
        p.csv_dir / "post_labels.csv.gz",
        predicate=lambda r: r.get("feed_uri") == label_feed_uri
        and r.get("viewer_mode") == label_viewer_mode
        and r.get("post_uri") == post_uri
        and r.get("post_cid") == post_cid,
        limit=12,
    )
    author_row = _find_row_gz(
        p.csv_dir / "authors.csv.gz",
        predicate=lambda r: r.get("author_did") == author_did,
    )

    starterpack_row, starterpack_inc = pick_named_starterpack_with_inclusion(p)

    run_meta = _read_single_row_csv(p.manifest_dir / "run_metadata.csv")
    run_summ = _read_single_row_csv(p.manifest_dir / "run_summary.csv")

    out: dict[str, Path] = {}

    # Run metadata receipt
    out_path = p.receipts_dir / "run_metadata_receipt.png"
    render_kv_card(
        out_path=out_path,
        title="run_metadata.csv (run window + params)",
        accent_hex=COLORS["slate"],
        height_px=360,
        items=(
            ("run_id", _truncate_middle(run_meta["run_id"], 22)),
            ("window", f"{run_meta['started_at_utc']} -> {run_meta['finished_at_utc']}"),
            ("auth_mode", run_meta["auth_mode"]),
            ("posts_per_feed", run_meta["posts_per_feed"]),
            (
                "targets",
                f"discovery={run_meta['n_discovery']} popular={run_meta['n_popular']} less_known={run_meta['n_less_known']}",
            ),
        ),
    )
    out["run_metadata_receipt"] = out_path

    # Run summary receipt (coverage only; no interpretation)
    out_path = p.receipts_dir / "run_summary_receipt.png"
    render_kv_card(
        out_path=out_path,
        title="run_summary.csv (coverage)",
        accent_hex=COLORS["slate"],
        height_px=420,
        items=(
            ("feed_generators_indexed", run_summ["num_feed_generators_indexed"]),
            ("starterpacks_seen", run_summ["num_starterpacks_seen"]),
            ("popular_feeds_seen", run_summ["num_popular_feeds_seen"]),
            ("feeds_panel", run_summ["num_feeds_panel"]),
            ("snapshots_success", run_summ["num_feeds_snapshotted_success"]),
            ("feed_items", run_summ["num_feed_items"]),
            ("unique_posts", run_summ["num_unique_posts"]),
            ("unique_authors", run_summ["num_unique_authors"]),
        ),
    )
    out["run_summary_receipt"] = out_path

    # Feed index row
    out_path = p.receipts_dir / "feed_index_row.png"
    render_kv_card(
        out_path=out_path,
        title="feed_generators_index.csv (example row)",
        accent_hex=COLORS["cyan"],
        height_px=320,
        items=(
            ("display_name", _truncate_middle(feed_idx.get("display_name", "") or "(no name)", 34)),
            ("provider_bucket", _truncate_middle(feed_idx.get("provider_bucket", "") or "(unknown)", 34)),
            ("feed_uri", _compact_at_uri(exemplar_feed_uri)),
        ),
    )
    out["feed_index_row"] = out_path

    # Starter pack row (metadata)
    out_path = p.receipts_dir / "starterpack_row.png"
    render_kv_card(
        out_path=out_path,
        title="starterpacks.csv (metadata)",
        accent_hex=COLORS["pink"],
        height_px=320,
        items=(
            ("name", _truncate_middle(starterpack_row.get("name", "") or "(no name)", 44)),
            ("creator_did", _compact_did(starterpack_row.get("creator_did", ""))),
            ("starterpack_uri", _compact_at_uri(starterpack_row.get("starterpack_uri", ""))),
        ),
    )
    out["starterpack_row"] = out_path

    # Starter pack inclusion row
    out_path = p.receipts_dir / "starterpack_inclusion_row.png"
    render_kv_card(
        out_path=out_path,
        title="starterpack_feeds.csv (inclusion)",
        accent_hex=COLORS["pink"],
        height_px=280,
        items=(
            ("starterpack_uri", _compact_at_uri(starterpack_inc.get("starterpack_uri", ""))),
            ("feed_uri", _compact_at_uri(starterpack_inc.get("feed_uri", ""))),
        ),
    )
    out["starterpack_inclusion_row"] = out_path

    # Popular row
    out_path = p.receipts_dir / "popular_row.png"
    render_kv_card(
        out_path=out_path,
        title="popular_feeds.csv (first row)",
        accent_hex=COLORS["amber"],
        height_px=280,
        items=(
            ("rank", popular_first.get("popularity_rank", "")),
            ("feed_uri", _compact_at_uri(exemplar_feed_uri)),
            ("collected_at_utc", popular_first.get("collected_at_utc", "")),
        ),
    )
    out["popular_row"] = out_path

    # Panel row
    out_path = p.receipts_dir / "panel_row.png"
    render_kv_card(
        out_path=out_path,
        title="feed_panel.csv (chosen feed)",
        accent_hex=COLORS["cyan"],
        height_px=340,
        items=(
            ("display_name", _truncate_middle(panel_row.get("display_name", "") or "(no name)", 34)),
            ("feed_group", panel_row.get("feed_group", "")),
            ("selection_reason", _truncate_middle(panel_row.get("selection_reason", ""), 34)),
            ("feed_uri", _compact_at_uri(exemplar_feed_uri)),
        ),
    )
    out["panel_row"] = out_path

    # Impression row (example; prefer one which has labels)
    out_path = p.receipts_dir / "impression_row.png"
    render_kv_card(
        out_path=out_path,
        title="feed_items.csv.gz (labeled impression)",
        accent_hex=COLORS["purple"],
        height_px=360,
        items=(
            ("feed_uri", _compact_at_uri(_clean_text(impression_row.get("feed_uri", "")))),
            ("viewer_mode", impression_row.get("viewer_mode", "")),
            ("rank", impression_row.get("rank", "")),
            ("post_uri", _compact_at_uri(post_uri)),
            ("post_cid", _compact_cid(post_cid)),
            ("author_did", _compact_did(author_did)),
        ),
    )
    out["impression_row"] = out_path

    # Post row (metadata only; no text)
    out_path = p.receipts_dir / "post_row.png"
    embed_short = _clean_text(post_row.get("embed_type", "")).replace("app.bsky.embed.", "") or "(none)"
    out_external = _clean_text(post_row.get("external_domain", "")) or "(none)"
    out_langs = _truncate_middle(_clean_text(post_row.get("langs_json", "")) or "[]", 42)
    render_kv_card(
        out_path=out_path,
        title="posts.csv.gz (metadata only)",
        accent_hex=COLORS["purple"],
        height_px=420,
        items=(
            ("post_uri", _compact_at_uri(post_uri)),
            ("post_cid", _compact_cid(post_cid)),
            ("author_did", _compact_did(post_row.get("author_did", author_did))),
            ("created_at", _clean_text(post_row.get("record_created_at", "")) or "(missing)"),
            ("embed", embed_short),
            ("external_domain", out_external),
            ("langs_json", out_langs),
        ),
    )
    out["post_row"] = out_path

    # Labels rows
    out_path = p.receipts_dir / "post_labels_row.png"
    if label_rows:
        lines = []
        for r in label_rows[:10]:
            src = _clean_text(r.get("label_src", ""))
            val = _clean_text(r.get("label_val", ""))
            neg = _clean_text(r.get("label_neg", ""))
            uri = _clean_text(r.get("label_uri", ""))
            suffix_parts: list[str] = []
            if neg:
                suffix_parts.append(f"neg={neg}")
            if uri and uri != post_uri:
                suffix_parts.append(f"uri={_truncate_middle(uri, 32)}")
            suffix = f" ({' '.join(suffix_parts)})" if suffix_parts else ""
            lines.append(f"- {src}:{val}{suffix}")
    else:
        lines = ["(no label rows for this impression)"]
    render_list_card(
        out_path=out_path,
        title="post_labels.csv.gz (labels excerpt)",
        accent_hex=COLORS["purple"],
        lines=lines,
        height_px=420,
        max_lines=11,
    )
    out["post_labels_row"] = out_path

    # Author row (no follower counts)
    out_path = p.receipts_dir / "author_row.png"
    render_kv_card(
        out_path=out_path,
        title="authors.csv.gz (profile; no counts)",
        accent_hex=COLORS["green"],
        height_px=320,
        items=(
            ("handle", _truncate_middle(_clean_text(author_row.get("handle", "")) or "(missing)", 36)),
            ("display_name", _truncate_middle(_clean_text(author_row.get("display_name", "")) or "(none)", 36)),
            ("author_did", _compact_did(author_did)),
        ),
    )
    out["author_row"] = out_path

    # Manifest excerpt
    want_files = {
        "feed_panel.csv",
        "feed_items.csv.gz",
        "posts.csv.gz",
        "authors.csv.gz",
        "run_metadata.csv",
        "run_summary.csv",
        "validation_report.csv",
    }
    manifest_rows = [r for r in _iter_csv_rows(p.manifest_dir / "manifest.csv") if r.get("file_name") in want_files]
    manifest_rows.sort(key=lambda r: r.get("file_name", ""))
    manifest_lines = [f"{r['file_name']}: sha256 {_compact_sha256(r['sha256'])}" for r in manifest_rows]
    out_path = p.receipts_dir / "manifest_excerpt.png"
    render_list_card(
        out_path=out_path,
        title="manifest.csv (files + hashes)",
        accent_hex=COLORS["slate"],
        lines=manifest_lines,
        height_px=420,
        max_lines=12,
    )
    out["manifest_excerpt"] = out_path

    # Data dictionary excerpt
    dd_keep = [
        ("feed_panel.csv", "feed_uri"),
        ("feed_panel.csv", "feed_group"),
        ("feed_items.csv.gz", "feed_uri"),
        ("feed_items.csv.gz", "viewer_mode"),
        ("feed_items.csv.gz", "rank"),
        ("feed_items.csv.gz", "post_uri"),
        ("feed_items.csv.gz", "post_cid"),
        ("posts.csv.gz", "author_did"),
    ]
    dd_rows = list(_iter_csv_rows(p.manifest_dir / "data_dictionary.csv"))
    dd_map: dict[tuple[str, str], dict[str, str]] = {(r.get("file_name", ""), r.get("column_name", "")): r for r in dd_rows}
    dd_lines: list[str] = []
    for file_name, col in dd_keep:
        r = dd_map.get((file_name, col))
        if not r:
            continue
        desc = _truncate_middle(_clean_text(r.get("description", "")), 72)
        dd_lines.append(f"{file_name}:{col} - {desc}")
    out_path = p.receipts_dir / "data_dictionary_excerpt.png"
    render_list_card(
        out_path=out_path,
        title="data_dictionary.csv (selected columns)",
        accent_hex=COLORS["slate"],
        lines=dd_lines or ["(missing rows)"],
        height_px=460,
        max_lines=12,
    )
    out["data_dictionary_excerpt"] = out_path

    # Validation report receipt (PASS-only excerpt)
    val_rows = list(_iter_csv_rows(p.manifest_dir / "validation_report.csv"))
    val_rows.sort(key=lambda r: r.get("check_name", ""))
    val_lines = [f"{r['status']}: {r['check_name']}" for r in val_rows[:12]]
    out_path = p.receipts_dir / "validation_report_receipt.png"
    render_list_card(
        out_path=out_path,
        title="validation_report.csv (excerpt)",
        accent_hex=COLORS["green"],
        lines=val_lines,
        height_px=420,
        max_lines=12,
    )
    out["validation_report_receipt"] = out_path

    # Folder tree receipt (00–07)
    top = p.run_dir.name
    tree_lines = [f"{top}/"]
    want = [
        "README_RUN.md",
        "state.db",
        "00_notes/",
        "01_state_db/state.db",
        "02_csv_exports/",
        "03_postprocess_metrics/",
        "04_logs/run.log",
        "05_manifest/",
        "06_figures_preview/receipts/",
        "07_archive_zip/",
    ]
    for w in want:
        tree_lines.append(f"  - {w}")
    out_path = p.receipts_dir / "folder_tree_receipt.png"
    render_list_card(
        out_path=out_path,
        title="Run folder map (expected structure)",
        accent_hex=COLORS["slate"],
        lines=tree_lines,
        height_px=420,
        max_lines=14,
    )
    out["folder_tree_receipt"] = out_path

    # Log excerpt receipt
    log_path = p.logs_dir / "run.log"
    log_lines: list[str] = []
    if log_path.exists():
        with log_path.open("r", encoding="utf-8", errors="replace") as f:
            for _ in range(12):
                line = f.readline()
                if not line:
                    break
                line = line.rstrip("\n")
                # Keep only a compact excerpt; avoid giant URLs.
                line = re.sub(r"(https?://\\S+)", lambda m: _truncate_middle(m.group(1), 80), line)
                log_lines.append(_truncate_middle(line, 120))
    else:
        log_lines = ["(missing run.log)"]
    out_path = p.receipts_dir / "log_excerpt_receipt.png"
    render_list_card(
        out_path=out_path,
        title="04_logs/run.log (excerpt)",
        accent_hex=COLORS["slate"],
        lines=log_lines,
        height_px=460,
        max_lines=12,
    )
    out["log_excerpt_receipt"] = out_path

    # Zip receipt (optional; avoids recursion by defaulting to “skip” until zip exists)
    run_id = _clean_text(run_meta.get("run_id", ""))
    zip_name = f"Bluesky_Run_{p.run_dir.name.replace('data_run_', '')}_{run_id}.zip"
    zip_path = p.run_dir / "07_archive_zip" / zip_name
    out_path = p.receipts_dir / "zip_receipt.png"
    if skip_zip_receipt:
        # Create a placeholder that is still visually useful for the deck.
        render_kv_card(
            out_path=out_path,
            title="Run archive (zip)",
            accent_hex=COLORS["slate"],
            height_px=260,
            items=(
                ("zip", zip_name),
                ("sha256", "(generate after zip exists)"),
            ),
        )
    else:
        if not zip_path.exists():
            raise RuntimeError(f"Missing run zip (create it first): {zip_path}")
        render_kv_card(
            out_path=out_path,
            title="Run archive (zip)",
            accent_hex=COLORS["slate"],
            height_px=260,
            items=(
                ("zip", zip_path.name),
                ("sha256", _compact_sha256(_sha256_file(zip_path))),
            ),
        )
    out["zip_receipt"] = out_path

    return out


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Build Slide2 PNG receipts from a run folder (no results).")
    p.add_argument("--run-dir", type=Path, required=True)
    p.add_argument(
        "--skip-zip-receipt",
        action="store_true",
        help="Render a placeholder zip receipt (useful before the zip exists).",
    )
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    run_dir = args.run_dir.resolve()
    if not run_dir.exists():
        raise SystemExit(f"Missing --run-dir: {run_dir}")

    # Ensure deterministic layout regardless of locale.
    os.environ.setdefault("TZ", "UTC")

    build_receipts(run_dir=run_dir, skip_zip_receipt=bool(args.skip_zip_receipt))
    print(f"OK: receipts written to {run_dir / '06_figures_preview' / 'receipts'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
