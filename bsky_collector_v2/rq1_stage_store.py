from __future__ import annotations

import csv
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from bsky_collector_v2.fs_utils import ensure_dir
from bsky_collector_v2.state import _connect_sqlite, _fsync_file_and_dir, _is_locked_error


@dataclass
class Rq1StageStore:
    path: Path
    conn: sqlite3.Connection

    @classmethod
    def open(cls, path: Path) -> "Rq1StageStore":
        ensure_dir(path.parent)
        conn = _connect_sqlite(path)
        store = cls(path=path, conn=conn)
        store._init_schema()
        return store

    def close(self) -> None:
        try:
            self.conn.commit()
        finally:
            self.conn.close()

    def __enter__(self) -> "Rq1StageStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        self.close()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS rq1_stage_registry (
              stage_name TEXT NOT NULL,
              entity_key TEXT NOT NULL,
              completed_at_utc TEXT NOT NULL,
              row_count INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY(stage_name, entity_key)
            );

            CREATE TABLE IF NOT EXISTS rq1_stage_rows (
              stage_name TEXT NOT NULL,
              entity_key TEXT NOT NULL,
              row_index INTEGER NOT NULL,
              row_json TEXT NOT NULL,
              PRIMARY KEY(stage_name, entity_key, row_index),
              FOREIGN KEY(stage_name, entity_key)
                REFERENCES rq1_stage_registry(stage_name, entity_key)
                ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_rq1_stage_rows_stage
            ON rq1_stage_rows(stage_name, entity_key, row_index);
            """
        )
        self.conn.commit()

    def stage_complete(self, *, stage_name: str, entity_key: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM rq1_stage_registry WHERE stage_name=? AND entity_key=?",
            (str(stage_name), str(entity_key)),
        ).fetchone()
        return row is not None

    def filter_incomplete(self, *, stage_name: str, entity_keys: Sequence[str]) -> list[str]:
        keys = [str(v) for v in entity_keys if str(v)]
        if not keys:
            return []
        placeholders = ",".join("?" for _ in keys)
        rows = self.conn.execute(
            f"SELECT entity_key FROM rq1_stage_registry WHERE stage_name=? AND entity_key IN ({placeholders})",
            tuple([str(stage_name), *keys]),
        ).fetchall()
        completed = {str(r["entity_key"]) for r in rows}
        return [key for key in keys if key not in completed]

    def stage_rows(self, *, stage_name: str, entity_key: str) -> list[dict[str, Any]]:
        cur = self.conn.execute(
            """
            SELECT row_json
            FROM rq1_stage_rows
            WHERE stage_name=? AND entity_key=?
            ORDER BY row_index ASC
            """,
            (str(stage_name), str(entity_key)),
        )
        out: list[dict[str, Any]] = []
        for row in cur.fetchall():
            try:
                parsed = json.loads(str(row["row_json"]))
            except Exception:  # noqa: BLE001
                continue
            if isinstance(parsed, dict):
                out.append(parsed)
        return out

    def upsert_stage_rows(
        self,
        *,
        stage_name: str,
        entity_key: str,
        rows: Sequence[Mapping[str, Any]],
        completed_at_utc: str,
    ) -> None:
        stage_name = str(stage_name)
        entity_key = str(entity_key)
        payloads = [json.dumps(dict(row), ensure_ascii=False, sort_keys=True) for row in rows]
        while True:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                self.conn.execute(
                    """
                    INSERT INTO rq1_stage_registry(stage_name, entity_key, completed_at_utc, row_count)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(stage_name, entity_key) DO UPDATE SET
                      completed_at_utc=excluded.completed_at_utc,
                      row_count=excluded.row_count
                    """,
                    (stage_name, entity_key, str(completed_at_utc), len(payloads)),
                )
                self.conn.execute(
                    "DELETE FROM rq1_stage_rows WHERE stage_name=? AND entity_key=?",
                    (stage_name, entity_key),
                )
                if payloads:
                    self.conn.executemany(
                        """
                        INSERT INTO rq1_stage_rows(stage_name, entity_key, row_index, row_json)
                        VALUES (?, ?, ?, ?)
                        """,
                        [(stage_name, entity_key, idx, payload) for idx, payload in enumerate(payloads)],
                    )
                self.conn.commit()
                break
            except sqlite3.OperationalError as err:
                self.conn.rollback()
                if _is_locked_error(err):
                    continue
                raise
        _fsync_file_and_dir(self.path)

    def materialize_csv(self, *, stage_name: str, path: Path, fieldnames: Sequence[str]) -> int:
        ensure_dir(path.parent)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        count = 0
        with open(tmp_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(fieldnames))
            writer.writeheader()
            cur = self.conn.execute(
                """
                SELECT row_json
                FROM rq1_stage_rows
                WHERE stage_name=?
                ORDER BY entity_key ASC, row_index ASC
                """,
                (str(stage_name),),
            )
            for row in cur.fetchall():
                parsed = json.loads(str(row["row_json"]))
                if not isinstance(parsed, dict):
                    continue
                writer.writerow({k: parsed.get(k) for k in fieldnames})
                count += 1
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp_path, path)
        _fsync_file_and_dir(path)
        return count


__all__ = ["Rq1StageStore"]
