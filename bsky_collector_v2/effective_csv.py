from __future__ import annotations

import csv
import logging
import os
import shutil
import tempfile
from pathlib import Path

from bsky_collector_v2.fs_utils import atomic_write_json, ensure_dir
from bsky_collector_v2.layout import Layout
from bsky_collector_v2.time_utils import SnapshotHour, format_utc, now_utc

logger = logging.getLogger("bsky_collector_v2.effective_csv")


_METADATA_CSV_NAMES: tuple[str, ...] = (
    "feed_catalog.csv",
    "starterpack_feeds.csv",
    "starterpack_accounts.csv",
    "suggested_feeds.csv",
    "suggested_accounts.csv",
    "suggested_follows_by_actor.csv",
)

# RQ1/RQ2 only require impression/exposure rows (feed_items). Keep the effective dataset minimal.
_TIMESERIES_PART_DATASETS: tuple[str, ...] = ("feed_items",)
_MICRO_TIMESERIES_PART_DATASETS: tuple[str, ...] = ("feed_items", "posts_first_seen", "post_metrics", "post_labels")


def sync_effective_csv_full(layout: Layout) -> None:
    """Rebuild effective CSV views from all currently available outputs."""
    _sync_all_metadata_days(layout)
    _sync_all_panel_versions(layout)
    _sync_all_hourly(layout)
    _sync_all_wide(layout)
    _sync_all_authors(layout)
    _sync_all_micro5(layout)
    refresh_key_views(layout)
    prune_to_rq_dataset(layout)


def sync_metadata_day(layout: Layout, *, date_yyyy_mm_dd: str) -> None:
    src_dir = layout.metadata_day(date_yyyy_mm_dd)
    if not src_dir.exists():
        return
    dest_dir = layout.effective_timeseries_root / "metadata" / date_yyyy_mm_dd
    ensure_dir(dest_dir)
    for file_name in _METADATA_CSV_NAMES:
        _copy_effective_csv(src=src_dir / file_name, dest=dest_dir / file_name)
    _prune_csv_dir(dest_dir, allowed_file_names=set(_METADATA_CSV_NAMES))


def sync_panel_day(layout: Layout, *, date_yyyy_mm_dd: str) -> None:
    # Keep a date-stamped panel snapshot for time-series use.
    version_src = layout.panel_version_csv(date_yyyy_mm_dd)
    version_dest = layout.effective_timeseries_root / "panel" / f"panel_v1_{date_yyyy_mm_dd}.csv"
    _copy_effective_csv(src=version_src, dest=version_dest)
    # Also keep active panel snapshot.
    active_dest = layout.effective_timeseries_root / "panel" / "panel_v1_active.csv"
    _copy_effective_csv(src=layout.panel_active_csv, dest=active_dest)


def sync_hour(layout: Layout, *, hour: SnapshotHour) -> None:
    parts_dir = layout.hourly_parts_dir(hour)
    if not parts_dir.exists():
        return
    dest_dir = layout.effective_timeseries_root / "hourly" / hour.date_str / hour.hour_str
    ensure_dir(dest_dir)
    for dataset_name in _TIMESERIES_PART_DATASETS:
        _merge_part_csvs(
            parts_dir=parts_dir,
            dataset_name=dataset_name,
            dest=dest_dir / f"{dataset_name}.csv",
        )
    _prune_csv_dir(dest_dir, allowed_file_names={f"{name}.csv" for name in _TIMESERIES_PART_DATASETS})


def sync_wide_day(layout: Layout, *, date_yyyy_mm_dd: str) -> None:
    parts_dir = layout.wide_parts_dir(date_yyyy_mm_dd)
    if not parts_dir.exists():
        return
    dest_dir = layout.effective_timeseries_root / "wide" / date_yyyy_mm_dd
    ensure_dir(dest_dir)
    for dataset_name in _TIMESERIES_PART_DATASETS:
        _merge_part_csvs(
            parts_dir=parts_dir,
            dataset_name=dataset_name,
            dest=dest_dir / f"{dataset_name}.csv",
        )
    _prune_csv_dir(dest_dir, allowed_file_names={f"{name}.csv" for name in _TIMESERIES_PART_DATASETS})


def sync_authors_day(layout: Layout, *, date_yyyy_mm_dd: str) -> None:
    src_dir = layout.authors_day_dir(date_yyyy_mm_dd)
    if not src_dir.exists():
        return
    dest_dir = layout.effective_timeseries_root / "authors" / date_yyyy_mm_dd
    ensure_dir(dest_dir)
    _merge_csv_files(
        source_paths=sorted(src_dir.glob("author_profiles_part_*.csv")),
        dest=dest_dir / "author_profiles.csv",
    )
    _prune_csv_dir(dest_dir, allowed_file_names={"author_profiles.csv"})


def sync_micro5_window(
    layout: Layout,
    *,
    study_id: str,
    sample_family: str,
    window=None,  # noqa: ANN001
    date_yyyy_mm_dd: str | None = None,
    hour_str: str | None = None,
    minute_str: str | None = None,
) -> None:
    parts_dir = layout.micro5_parts_dir(
        study_id=study_id,
        sample_family=sample_family,
        window=window,
        date_yyyy_mm_dd=date_yyyy_mm_dd,
        hour_str=hour_str,
        minute_str=minute_str,
    )
    if not parts_dir.exists():
        return
    dest_dir = layout.effective_micro5_window_dir(
        study_id=study_id,
        sample_family=sample_family,
        window=window,
        date_yyyy_mm_dd=date_yyyy_mm_dd,
        hour_str=hour_str,
        minute_str=minute_str,
    )
    ensure_dir(dest_dir)
    for dataset_name in _MICRO_TIMESERIES_PART_DATASETS:
        _merge_part_csvs(
            parts_dir=parts_dir,
            dataset_name=dataset_name,
            dest=dest_dir / f"{dataset_name}.csv",
        )
    _prune_csv_dir(dest_dir, allowed_file_names={f"{name}.csv" for name in _MICRO_TIMESERIES_PART_DATASETS})


def refresh_key_views(layout: Layout) -> None:
    """Refresh key latest CSV views under effective_csv/key/."""
    ensure_dir(layout.effective_key_root)
    key_sources: dict[str, str] = {}

    # Metadata latest.
    for file_name in _METADATA_CSV_NAMES:
        _promote_latest(
            layout=layout,
            source_glob=f"metadata/*/{file_name}",
            key_dest=layout.effective_key_root / "metadata" / file_name,
            source_key=f"metadata/{file_name}",
            key_sources=key_sources,
        )

    # Latest hourly and wide snapshots.
    for dataset_name in _TIMESERIES_PART_DATASETS:
        file_name = f"{dataset_name}.csv"
        _promote_latest(
            layout=layout,
            source_glob=f"hourly/*/*/{file_name}",
            key_dest=layout.effective_key_root / "hourly" / file_name,
            source_key=f"hourly/{file_name}",
            key_sources=key_sources,
        )
        _promote_latest(
            layout=layout,
            source_glob=f"wide/*/{file_name}",
            key_dest=layout.effective_key_root / "wide" / file_name,
            source_key=f"wide/{file_name}",
            key_sources=key_sources,
        )

    # Latest authors.
    _promote_latest(
        layout=layout,
        source_glob="authors/*/author_profiles.csv",
        key_dest=layout.effective_key_root / "authors" / "author_profiles.csv",
        source_key="authors/author_profiles.csv",
        key_sources=key_sources,
    )

    # Latest panel snapshot: prefer date-versioned panel, fall back to active.
    if not _promote_latest(
        layout=layout,
        source_glob="panel/panel_v1_*.csv",
        key_dest=layout.effective_key_root / "panel" / "panel_v1.csv",
        source_key="panel/panel_v1.csv",
        key_sources=key_sources,
    ):
        _promote_latest(
            layout=layout,
            source_glob="panel/panel_v1_active.csv",
            key_dest=layout.effective_key_root / "panel" / "panel_v1.csv",
            source_key="panel/panel_v1.csv",
            key_sources=key_sources,
        )

    atomic_write_json(
        layout.effective_key_sources_json,
        {
            "generated_at_utc": format_utc(now_utc()),
            "sources": key_sources,
        },
    )
    _prune_csv_dir(layout.effective_key_root / "metadata", allowed_file_names=set(_METADATA_CSV_NAMES))
    _prune_csv_dir(
        layout.effective_key_root / "hourly",
        allowed_file_names={f"{name}.csv" for name in _TIMESERIES_PART_DATASETS},
    )
    _prune_csv_dir(
        layout.effective_key_root / "wide",
        allowed_file_names={f"{name}.csv" for name in _TIMESERIES_PART_DATASETS},
    )
    _prune_csv_dir(layout.effective_key_root / "authors", allowed_file_names={"author_profiles.csv"})
    # key/panel contains a single file; keep it as-is.
    _remove_appledouble_sidecars(layout.effective_csv_root)


def _sync_all_metadata_days(layout: Layout) -> None:
    for date_str in _list_date_dirs(layout.metadata_root):
        sync_metadata_day(layout, date_yyyy_mm_dd=date_str)


def _sync_all_panel_versions(layout: Layout) -> None:
    versions_dir = layout.panel_versions_dir
    if versions_dir.exists():
        for path in sorted(versions_dir.glob("panel_v1_*.csv")):
            suffix = path.stem.removeprefix("panel_v1_")
            if _is_date_dir_name(suffix):
                sync_panel_day(layout, date_yyyy_mm_dd=suffix)
    # Keep active panel in sync even if no version files are present.
    _copy_effective_csv(
        src=layout.panel_active_csv,
        dest=layout.effective_timeseries_root / "panel" / "panel_v1_active.csv",
    )


def _sync_all_hourly(layout: Layout) -> None:
    root = layout.hourly_root
    if not root.exists():
        return
    for date_dir in sorted(root.iterdir()):
        if not date_dir.is_dir() or not _is_date_dir_name(date_dir.name):
            continue
        for hour_dir in sorted(date_dir.iterdir()):
            if not hour_dir.is_dir() or not _is_hour_dir_name(hour_dir.name):
                continue
            sync_hour_dir(layout, date_yyyy_mm_dd=date_dir.name, hour_str=hour_dir.name)


def _sync_all_wide(layout: Layout) -> None:
    for date_str in _list_date_dirs(layout.wide_root):
        sync_wide_day(layout, date_yyyy_mm_dd=date_str)


def _sync_all_authors(layout: Layout) -> None:
    for date_str in _list_date_dirs(layout.authors_root):
        sync_authors_day(layout, date_yyyy_mm_dd=date_str)


def _sync_all_micro5(layout: Layout) -> None:
    root = layout.micro5_root
    if not root.exists():
        return
    for study_dir in sorted(root.iterdir()):
        if not study_dir.is_dir():
            continue
        for family_dir in sorted(study_dir.iterdir()):
            if not family_dir.is_dir():
                continue
            for date_dir in sorted(family_dir.iterdir()):
                if not date_dir.is_dir() or not _is_date_dir_name(date_dir.name):
                    continue
                for hour_dir in sorted(date_dir.iterdir()):
                    if not hour_dir.is_dir() or not _is_hour_dir_name(hour_dir.name):
                        continue
                    for minute_dir in sorted(hour_dir.iterdir()):
                        if not minute_dir.is_dir() or not _is_hour_dir_name(minute_dir.name):
                            continue
                        sync_micro5_window(
                            layout,
                            study_id=study_dir.name,
                            sample_family=family_dir.name,
                            date_yyyy_mm_dd=date_dir.name,
                            hour_str=hour_dir.name,
                            minute_str=minute_dir.name,
                        )


def sync_hour_dir(layout: Layout, *, date_yyyy_mm_dd: str, hour_str: str) -> None:
    parts_dir = layout.hourly_root / date_yyyy_mm_dd / hour_str / "parts"
    if not parts_dir.exists():
        return
    dest_dir = layout.effective_timeseries_root / "hourly" / date_yyyy_mm_dd / hour_str
    ensure_dir(dest_dir)
    for dataset_name in _TIMESERIES_PART_DATASETS:
        _merge_part_csvs(
            parts_dir=parts_dir,
            dataset_name=dataset_name,
            dest=dest_dir / f"{dataset_name}.csv",
        )
    _prune_csv_dir(dest_dir, allowed_file_names={f"{name}.csv" for name in _TIMESERIES_PART_DATASETS})


def prune_to_rq_dataset(layout: Layout) -> None:
    """Remove effective CSV artifacts that are not needed for RQ1/RQ2 analysis."""
    root = layout.effective_timeseries_root
    if not root.exists():
        return

    allowed_metadata = set(_METADATA_CSV_NAMES)
    allowed_hourly = {f"{name}.csv" for name in _TIMESERIES_PART_DATASETS}
    allowed_wide = allowed_hourly

    # metadata/YYYY-MM-DD/*.csv
    meta_root = root / "metadata"
    if meta_root.exists():
        for day_dir in meta_root.iterdir():
            if day_dir.is_dir():
                _prune_csv_dir(day_dir, allowed_file_names=allowed_metadata)

    # hourly/YYYY-MM-DD/HH/*.csv
    hourly_root = root / "hourly"
    if hourly_root.exists():
        for date_dir in hourly_root.iterdir():
            if not date_dir.is_dir():
                continue
            for hour_dir in date_dir.iterdir():
                if hour_dir.is_dir():
                    _prune_csv_dir(hour_dir, allowed_file_names=allowed_hourly)

    # wide/YYYY-MM-DD/*.csv
    wide_root = root / "wide"
    if wide_root.exists():
        for day_dir in wide_root.iterdir():
            if day_dir.is_dir():
                _prune_csv_dir(day_dir, allowed_file_names=allowed_wide)


def _merge_part_csvs(*, parts_dir: Path, dataset_name: str, dest: Path) -> bool:
    return _merge_csv_files(
        source_paths=sorted(parts_dir.glob(f"{dataset_name}_part_*.csv")),
        dest=dest,
    )


def _merge_csv_files(*, source_paths: list[Path], dest: Path) -> bool:
    valid_paths = [p for p in source_paths if p.exists() and p.is_file()]
    if not valid_paths:
        _remove_if_exists(dest)
        return False

    ensure_dir(dest.parent)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="",
        prefix=dest.name + ".tmp.",
        dir=str(dest.parent),
        delete=False,
    ) as tmp:
        tmp_path = Path(tmp.name)
        writer: csv.DictWriter[str] | None = None
        fieldnames: list[str] | None = None
        rows_written = 0
        try:
            for src in valid_paths:
                with src.open("r", encoding="utf-8", newline="") as fh:
                    reader = csv.DictReader(fh)
                    src_fieldnames = list(reader.fieldnames or [])
                    if not src_fieldnames:
                        continue
                    if fieldnames is None:
                        fieldnames = src_fieldnames
                        writer = csv.DictWriter(tmp, fieldnames=fieldnames, extrasaction="ignore")
                        writer.writeheader()
                    assert writer is not None
                    assert fieldnames is not None
                    for row in reader:
                        if not _row_has_data(row):
                            continue
                        writer.writerow({name: row.get(name, "") for name in fieldnames})
                        rows_written += 1
        finally:
            tmp.flush()
            os.fsync(tmp.fileno())

    if rows_written <= 0:
        _remove_if_exists(tmp_path)
        _remove_if_exists(dest)
        return False

    os.replace(tmp_path, dest)
    return True


def _copy_effective_csv(*, src: Path, dest: Path) -> bool:
    if not src.exists() or not src.is_file():
        _remove_if_exists(dest)
        return False
    if not _csv_has_data_rows(src):
        _remove_if_exists(dest)
        return False
    _copy_file_content_atomic(src=src, dest=dest)
    return True


def _promote_latest(
    *,
    layout: Layout,
    source_glob: str,
    key_dest: Path,
    source_key: str,
    key_sources: dict[str, str],
) -> bool:
    candidates = sorted(layout.effective_timeseries_root.glob(source_glob))
    for src in reversed(candidates):
        if not src.is_file():
            continue
        if not _csv_has_data_rows(src):
            continue
        _copy_file_content_atomic(src=src, dest=key_dest)
        key_sources[source_key] = str(src.relative_to(layout.out_base))
        return True
    _remove_if_exists(key_dest)
    return False


def _copy_file_content_atomic(*, src: Path, dest: Path) -> None:
    ensure_dir(dest.parent)
    tmp_path: Path | None = None
    try:
        with src.open("rb") as src_fh, tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=dest.name + ".tmp.",
            dir=str(dest.parent),
            delete=False,
        ) as tmp:
            tmp_path = Path(tmp.name)
            shutil.copyfileobj(src_fh, tmp)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_path, dest)
    except Exception:  # noqa: BLE001
        if tmp_path is not None:
            _remove_if_exists(tmp_path)
        raise


def _csv_has_data_rows(path: Path) -> bool:
    if not path.exists() or not path.is_file():
        return False
    try:
        with path.open("r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            if not reader.fieldnames:
                return False
            for row in reader:
                if _row_has_data(row):
                    return True
    except OSError:
        return False
    return False


def _row_has_data(row: dict[str, object] | None) -> bool:
    if not row:
        return False
    for value in row.values():
        if value is None:
            continue
        if isinstance(value, str):
            if value.strip():
                return True
            continue
        return True
    return False


def _list_date_dirs(root: Path) -> list[str]:
    if not root.exists():
        return []
    out: list[str] = []
    for child in sorted(root.iterdir()):
        if child.is_dir() and _is_date_dir_name(child.name):
            out.append(child.name)
    return out


def _is_date_dir_name(name: str) -> bool:
    if not (len(name) == 10 and name[4] == "-" and name[7] == "-"):
        return False
    yyyy, mm, dd = name.split("-")
    return yyyy.isdigit() and mm.isdigit() and dd.isdigit()


def _is_hour_dir_name(name: str) -> bool:
    return len(name) == 2 and name.isdigit()


def _remove_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        logger.debug("could not remove stale path=%s", path)


def _remove_appledouble_sidecars(root: Path) -> None:
    if not root.exists():
        return
    for sidecar in root.rglob("._*"):
        if sidecar.is_file():
            _remove_if_exists(sidecar)


def _prune_csv_dir(dir_path: Path, *, allowed_file_names: set[str]) -> None:
    if not dir_path.exists() or not dir_path.is_dir():
        return
    for child in dir_path.iterdir():
        if not child.is_file():
            continue
        if child.name.startswith("._"):
            _remove_if_exists(child)
            continue
        if child.suffix.lower() != ".csv":
            continue
        if child.name not in allowed_file_names:
            _remove_if_exists(child)
