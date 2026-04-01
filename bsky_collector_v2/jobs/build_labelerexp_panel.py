from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from pathlib import Path

from bsky_collector_v2.fs_utils import ensure_dir
from bsky_collector_v2.layout import Layout
from bsky_collector_v2.time_utils import format_utc, now_utc, utc_date_str

logger = logging.getLogger("bsky_collector_v2.job.build_labelerexp_panel")


@dataclass(frozen=True)
class LabelerExpPanelConfig:
    bucket: str = "suggested"
    max_feeds: int | None = None


def _latest_metadata_day_with_suggested(layout: Layout) -> str:
    root = layout.metadata_root
    if not root.exists():
        raise FileNotFoundError(f"missing metadata root: {root}")

    candidates: list[str] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        name = child.name
        if len(name) != 10:
            continue
        if layout.suggested_feeds_csv(name).exists():
            candidates.append(name)

    if not candidates:
        raise FileNotFoundError(f"no metadata days with suggested_feeds.csv under: {root}")
    return sorted(candidates)[-1]


def _read_latest_suggested_snapshot(path: Path) -> tuple[str | None, list[str]]:
    """Return (captured_at_utc, feed_uris) for the latest snapshot in append-only suggested_feeds.csv."""
    latest_ts: str | None = None
    rows: list[tuple[int, str]] = []

    with open(path, "r", encoding="utf-8", newline="") as f:
        r = csv.DictReader(f)
        for row in r:
            ts = (row.get("captured_at_utc") or "").strip()
            uri = (row.get("feed_uri") or "").strip()
            pos_s = (row.get("position") or "").strip()
            if not ts or not uri:
                continue
            try:
                pos = int(pos_s)
            except ValueError:
                continue

            if latest_ts is None or ts > latest_ts:
                latest_ts = ts
                rows = []
            if ts == latest_ts:
                rows.append((pos, uri))

    rows.sort(key=lambda t: (t[0], t[1]))
    out: list[str] = []
    seen: set[str] = set()
    for _pos, uri in rows:
        if uri in seen:
            continue
        out.append(uri)
        seen.add(uri)
    return latest_ts, out


def _read_unauth_skip_map(panel_path: Path) -> dict[str, int]:
    if not panel_path.exists():
        return {}
    out: dict[str, int] = {}
    with open(panel_path, "r", encoding="utf-8", newline="") as f:
        r = csv.DictReader(f)
        for row in r:
            uri = (row.get("feed_uri") or "").strip()
            if not uri:
                continue
            try:
                unauth_skip = int(row.get("unauth_skip") or 0)
            except ValueError:
                unauth_skip = 0
            out[uri] = int(unauth_skip)
    return out


def _write_panel_csv(
    *,
    out_path: Path,
    selected: list[str],
    bucket: str,
    unauth_skip: dict[str, int],
    built_at_utc: str,
    panel_version_id: str,
) -> None:
    ensure_dir(out_path.parent)
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["feed_uri", "bucket", "unauth_skip", "built_at_utc", "panel_version_id"])
        for feed_uri in selected:
            w.writerow([feed_uri, bucket, int(unauth_skip.get(feed_uri, 0)), built_at_utc, panel_version_id])
        f.flush()


def run_build_labelerexp_panel(
    *,
    layout: Layout,
    source_out_base: Path,
    source_metadata_day: str | None,
    dry_run: bool,
    cfg: LabelerExpPanelConfig | None = None,
) -> None:
    """Build a small panel suitable for multi-labeler comparisons.

    Design goals:
    - Don't hit the network (reuse already-collected discovery surfaces from source_out_base).
    - Keep costs low: focus on discovery (suggested feeds) for strong comparability.
    """
    cfg = cfg or LabelerExpPanelConfig()
    source_layout = Layout(out_base=Path(source_out_base))

    if source_metadata_day:
        metadata_day = str(source_metadata_day).strip()
    else:
        metadata_day = _latest_metadata_day_with_suggested(source_layout)

    suggested_path = source_layout.suggested_feeds_csv(metadata_day)
    if not suggested_path.exists():
        raise FileNotFoundError(f"missing suggested_feeds.csv: {suggested_path}")

    captured_at_utc, feed_uris = _read_latest_suggested_snapshot(suggested_path)
    if not feed_uris:
        raise RuntimeError(f"no feeds found in suggested_feeds.csv: {suggested_path}")

    max_feeds = cfg.max_feeds
    if isinstance(max_feeds, int) and max_feeds > 0:
        feed_uris = feed_uris[: max_feeds]

    unauth_skip = _read_unauth_skip_map(source_layout.panel_active_csv)
    built_at_utc = format_utc(now_utc())
    panel_version_id = utc_date_str(now_utc())

    logger.info(
        "labelerexp panel build source_out_base=%s metadata_day=%s captured_at_utc=%s feeds=%s",
        str(source_out_base),
        metadata_day,
        captured_at_utc,
        len(feed_uris),
    )

    if dry_run:
        logger.info("dry_run=true: would write panel rows=%s out_base=%s", len(feed_uris), str(layout.out_base))
        return

    ensure_dir(layout.panel_versions_dir)
    ensure_dir(layout.panel_root)

    version_path = layout.panel_version_csv(panel_version_id)
    tmp_version = version_path.with_name(version_path.name + ".tmp")
    _write_panel_csv(
        out_path=tmp_version,
        selected=feed_uris,
        bucket=str(cfg.bucket or "suggested").strip() or "suggested",
        unauth_skip=unauth_skip,
        built_at_utc=built_at_utc,
        panel_version_id=panel_version_id,
    )
    tmp_version.replace(version_path)

    tmp_active = layout.panel_active_csv.with_name(layout.panel_active_csv.name + ".tmp")
    _write_panel_csv(
        out_path=tmp_active,
        selected=feed_uris,
        bucket=str(cfg.bucket or "suggested").strip() or "suggested",
        unauth_skip=unauth_skip,
        built_at_utc=built_at_utc,
        panel_version_id=panel_version_id,
    )
    tmp_active.replace(layout.panel_active_csv)

    logger.info("labelerexp panel written active=%s version=%s", str(layout.panel_active_csv), str(version_path))

    try:
        from bsky_collector_v2.effective_csv import refresh_key_views, sync_panel_day

        sync_panel_day(layout, date_yyyy_mm_dd=panel_version_id)
        refresh_key_views(layout)
    except Exception as err:  # noqa: BLE001
        logger.warning("effective csv sync failed job=build-labelerexp-panel date=%s err=%r", panel_version_id, err)

