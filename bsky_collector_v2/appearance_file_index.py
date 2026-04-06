from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from bsky_collector_v2.fs_utils import ensure_dir
from bsky_collector_v2.layout import Layout
from bsky_collector_v2.state import _connect_sqlite
from bsky_collector_v2.time_utils import format_utc, now_utc

logger = logging.getLogger("bsky_collector_v2.appearance_file_index")

_INDEX_FILE_NAME = "rq1_appearance_file_index.sqlite"
_POST_FILE_BATCH_SIZE = 1000
_QUERY_BATCH_SIZE = 500


@dataclass(frozen=True)
class AppearanceMatch:
    row: dict[str, str]
    source_family: str
    source_path: Path


@dataclass(frozen=True)
class _AppearanceSource:
    path: Path
    source_family: str


def _chunked(values: Sequence[str], size: int) -> Iterator[list[str]]:
    size = max(1, int(size))
    for idx in range(0, len(values), size):
        yield list(values[idx : idx + size])


def _index_path(layout: Layout) -> Path:
    return layout.control_root / _INDEX_FILE_NAME


def _iter_effective_sources(layout: Layout) -> Iterator[_AppearanceSource]:
    hourly_root = layout.effective_timeseries_root / "hourly"
    if hourly_root.exists():
        for path in sorted(hourly_root.glob("*/*/feed_items.csv")):
            if path.is_file():
                yield _AppearanceSource(path=path, source_family="hourly")

    wide_root = layout.effective_timeseries_root / "wide"
    if wide_root.exists():
        for path in sorted(wide_root.glob("*/feed_items.csv")):
            if path.is_file():
                yield _AppearanceSource(path=path, source_family="wide")

    if layout.effective_micro5_root.exists():
        for path in sorted(layout.effective_micro5_root.rglob("feed_items.csv")):
            if path.is_file():
                yield _AppearanceSource(path=path, source_family="micro5")


def _effective_counterpart_for_raw(layout: Layout, source: _AppearanceSource) -> Path | None:
    path = source.path
    if source.source_family == "hourly":
        try:
            rel = path.relative_to(layout.hourly_root)
        except ValueError:
            return None
        if len(rel.parts) != 4 or rel.parts[2] != "parts":
            return None
        return layout.effective_timeseries_root / "hourly" / rel.parts[0] / rel.parts[1] / "feed_items.csv"

    if source.source_family == "wide":
        try:
            rel = path.relative_to(layout.wide_root)
        except ValueError:
            return None
        if len(rel.parts) != 3 or rel.parts[1] != "parts":
            return None
        return layout.effective_timeseries_root / "wide" / rel.parts[0] / "feed_items.csv"

    if source.source_family == "micro5":
        try:
            rel = path.relative_to(layout.micro5_root)
        except ValueError:
            return None
        if len(rel.parts) != 7 or rel.parts[5] != "parts":
            return None
        study_id, sample_family, date_str, hour_str, minute_str = rel.parts[:5]
        return (
            layout.effective_micro5_root
            / study_id
            / sample_family
            / date_str
            / hour_str
            / minute_str
            / "feed_items.csv"
        )
    return None


def _iter_raw_sources(layout: Layout) -> Iterator[_AppearanceSource]:
    if layout.hourly_root.exists():
        for path in sorted(layout.hourly_root.rglob("feed_items_part_*.csv")):
            if path.is_file():
                yield _AppearanceSource(path=path, source_family="hourly")

    if layout.wide_root.exists():
        for path in sorted(layout.wide_root.rglob("feed_items_part_*.csv")):
            if path.is_file():
                yield _AppearanceSource(path=path, source_family="wide")

    if layout.micro5_root.exists():
        for path in sorted(layout.micro5_root.rglob("feed_items_part_*.csv")):
            if path.is_file():
                yield _AppearanceSource(path=path, source_family="micro5")


def _iter_candidate_sources(layout: Layout) -> list[_AppearanceSource]:
    sources: dict[str, _AppearanceSource] = {
        str(source.path): source for source in _iter_effective_sources(layout)
    }
    for source in _iter_raw_sources(layout):
        effective_path = _effective_counterpart_for_raw(layout, source)
        if effective_path is not None and effective_path.exists():
            continue
        sources[str(source.path)] = source
    return [sources[key] for key in sorted(sources)]


class AppearanceFileIndex:
    def __init__(self, path: Path) -> None:
        ensure_dir(path.parent)
        self.path = path
        self.conn = _connect_sqlite(path)
        self._init_schema()

    def close(self) -> None:
        try:
            self.conn.commit()
        finally:
            self.conn.close()

    def __enter__(self) -> "AppearanceFileIndex":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        self.close()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS appearance_source_files (
              source_id INTEGER PRIMARY KEY,
              source_path TEXT NOT NULL UNIQUE,
              source_family TEXT NOT NULL,
              file_size INTEGER NOT NULL,
              mtime_ns INTEGER NOT NULL,
              indexed_at_utc TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS appearance_post_file_map (
              post_uri TEXT NOT NULL,
              source_id INTEGER NOT NULL,
              PRIMARY KEY(post_uri, source_id),
              FOREIGN KEY(source_id)
                REFERENCES appearance_source_files(source_id)
                ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_appearance_post_file_map_post_uri
            ON appearance_post_file_map(post_uri, source_id);
            """
        )
        self.conn.commit()

    def sync(self, layout: Layout) -> tuple[int, int, int]:
        candidate_sources = _iter_candidate_sources(layout)
        candidate_by_path = {str(source.path): source for source in candidate_sources}
        rows = self.conn.execute(
            "SELECT source_id, source_path FROM appearance_source_files"
        ).fetchall()

        removed = 0
        stale_source_ids = [
            int(row["source_id"])
            for row in rows
            if str(row["source_path"]) not in candidate_by_path
        ]
        if stale_source_ids:
            self.conn.executemany(
                "DELETE FROM appearance_source_files WHERE source_id=?",
                [(source_id,) for source_id in stale_source_ids],
            )
            self.conn.commit()
            removed = len(stale_source_ids)

        refreshed = 0
        for source in candidate_sources:
            stat = source.path.stat()
            row = self.conn.execute(
                """
                SELECT source_id, file_size, mtime_ns
                FROM appearance_source_files
                WHERE source_path=?
                """,
                (str(source.path),),
            ).fetchone()
            if (
                row is not None
                and int(row["file_size"]) == int(stat.st_size)
                and int(row["mtime_ns"]) == int(stat.st_mtime_ns)
            ):
                continue
            self._reindex_source(source, file_size=int(stat.st_size), mtime_ns=int(stat.st_mtime_ns))
            refreshed += 1
        return len(candidate_sources), refreshed, removed

    def _reindex_source(self, source: _AppearanceSource, *, file_size: int, mtime_ns: int) -> None:
        indexed_at_utc = format_utc(now_utc())
        source_path = str(source.path)
        with source.path.open("r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                row = self.conn.execute(
                    "SELECT source_id FROM appearance_source_files WHERE source_path=?",
                    (source_path,),
                ).fetchone()
                if row is None:
                    cur = self.conn.execute(
                        """
                        INSERT INTO appearance_source_files(
                          source_path,
                          source_family,
                          file_size,
                          mtime_ns,
                          indexed_at_utc
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (source_path, source.source_family, file_size, mtime_ns, indexed_at_utc),
                    )
                    source_id = int(cur.lastrowid)
                else:
                    source_id = int(row["source_id"])
                    self.conn.execute(
                        """
                        UPDATE appearance_source_files
                        SET source_family=?, file_size=?, mtime_ns=?, indexed_at_utc=?
                        WHERE source_id=?
                        """,
                        (source.source_family, file_size, mtime_ns, indexed_at_utc, source_id),
                    )

                self.conn.execute(
                    "DELETE FROM appearance_post_file_map WHERE source_id=?",
                    (source_id,),
                )

                batch: list[tuple[str, int]] = []
                for raw_row in reader:
                    post_uri = str(raw_row.get("post_uri") or "").strip()
                    if not post_uri:
                        continue
                    batch.append((post_uri, source_id))
                    if len(batch) >= _POST_FILE_BATCH_SIZE:
                        self.conn.executemany(
                            """
                            INSERT OR IGNORE INTO appearance_post_file_map(post_uri, source_id)
                            VALUES (?, ?)
                            """,
                            batch,
                        )
                        batch.clear()
                if batch:
                    self.conn.executemany(
                        """
                        INSERT OR IGNORE INTO appearance_post_file_map(post_uri, source_id)
                        VALUES (?, ?)
                        """,
                        batch,
                    )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def iter_matches(self, post_uris: Sequence[str]) -> Iterator[AppearanceMatch]:
        targets = sorted({str(post_uri).strip() for post_uri in post_uris if str(post_uri).strip()})
        if not targets:
            return

        source_by_path: dict[str, _AppearanceSource] = {}
        for batch in _chunked(targets, _QUERY_BATCH_SIZE):
            placeholders = ",".join("?" for _ in batch)
            rows = self.conn.execute(
                f"""
                SELECT DISTINCT sf.source_path, sf.source_family
                FROM appearance_post_file_map AS pf
                JOIN appearance_source_files AS sf ON sf.source_id = pf.source_id
                WHERE pf.post_uri IN ({placeholders})
                ORDER BY sf.source_path ASC
                """,
                tuple(batch),
            ).fetchall()
            for row in rows:
                source_path = str(row["source_path"])
                source_by_path[source_path] = _AppearanceSource(
                    path=Path(source_path),
                    source_family=str(row["source_family"]),
                )

        target_set = set(targets)
        for source_path in sorted(source_by_path):
            source = source_by_path[source_path]
            try:
                with source.path.open("r", encoding="utf-8", newline="") as fh:
                    reader = csv.DictReader(fh)
                    for raw_row in reader:
                        post_uri = str(raw_row.get("post_uri") or "").strip()
                        if post_uri and post_uri in target_set:
                            yield AppearanceMatch(
                                row={str(k): str(v) if v is not None else "" for k, v in raw_row.items()},
                                source_family=source.source_family,
                                source_path=source.path,
                            )
            except OSError:
                logger.warning("appearance source disappeared during lookup path=%s", str(source.path))


def iter_matching_feed_item_rows(layout: Layout, post_uris: Iterable[str]) -> Iterator[AppearanceMatch]:
    targets = sorted({str(post_uri).strip() for post_uri in post_uris if str(post_uri).strip()})
    if not targets:
        return
    with AppearanceFileIndex(_index_path(layout)) as index:
        files_seen, refreshed, removed = index.sync(layout)
        if refreshed > 0 or removed > 0:
            logger.info(
                "appearance index sync files=%s refreshed=%s removed=%s path=%s",
                files_seen,
                refreshed,
                removed,
                str(index.path),
            )
        yield from index.iter_matches(targets)


__all__ = ["AppearanceFileIndex", "AppearanceMatch", "iter_matching_feed_item_rows"]
