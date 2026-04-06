from __future__ import annotations

import hashlib
import json
import os
import socket
import sqlite3
import time
from collections.abc import Iterator as AbcIterator
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence
from urllib.parse import urlparse

from bsky_collector_v2.rq1_stages import (
    normalize_rq1_stage,
    rq1_stage_completion_columns,
    rq1_stage_pending_column,
    rq1_stage_prerequisite_clauses,
)
from bsky_collector_v2.types import FeedUri, PostUri, RunId, ViewerMode


_STATE_WRITER_SOCKET_ENV = "BSKY_STATE_WRITER_SOCKET"
_STATE_WRITER_RPC_TIMEOUT_ENV = "BSKY_STATE_WRITER_RPC_TIMEOUT_S"


def _state_writer_rpc_timeout_s() -> float:
    raw = str(os.getenv(_STATE_WRITER_RPC_TIMEOUT_ENV, "60")).strip()
    try:
        value = float(raw)
    except ValueError:
        return 60.0
    return max(1.0, value)


_STATE_WRITER_RPC_TIMEOUT_S = _state_writer_rpc_timeout_s()


@dataclass(frozen=True)
class SelectedPost:
    post_uri: str
    first_seen_utc: str


def coerce_selected_post_row(value: Any) -> SelectedPost:
    if isinstance(value, SelectedPost):
        return value
    if isinstance(value, dict):
        post_uri = str(value.get("post_uri") or "").strip()
        first_seen_utc = str(value.get("first_seen_utc") or "").strip()
        if not post_uri or not first_seen_utc:
            raise ValueError(f"invalid selected post row: {value!r}")
        return SelectedPost(post_uri=post_uri, first_seen_utc=first_seen_utc)
    raise TypeError(f"unsupported selected post row type: {type(value).__name__}")


def coerce_selected_post_rows(values: Iterable[Any]) -> list[SelectedPost]:
    return [coerce_selected_post_row(value) for value in values]


def _stable_shard(value: str, shard_count: int) -> int:
    digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()
    return int(digest[:16], 16) % max(1, int(shard_count))


def _connect_sqlite(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=60.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=60000;")
    conn.commit()
    return conn


def _is_locked_error(err: sqlite3.OperationalError) -> bool:
    msg = str(err).lower()
    return (
        "database is locked" in msg
        or "database table is locked" in msg
        or "database schema is locked" in msg
    )


def _with_locked_retry(fn: Any, *, attempts: int = 3, base_sleep_s: float = 0.2) -> Any:
    for attempt in range(max(1, int(attempts))):
        try:
            return fn()
        except sqlite3.OperationalError as err:
            if not _is_locked_error(err) or attempt >= (attempts - 1):
                raise
            time.sleep(base_sleep_s * (2**attempt))


def _fsync_file_and_dir(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)

    try:
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def _serialize_rpc_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, sqlite3.Row):
        return {str(k): _serialize_rpc_value(value[k]) for k in value.keys()}
    if not isinstance(value, type) and is_dataclass(value):
        return {str(k): _serialize_rpc_value(v) for k, v in asdict(value).items()}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _serialize_rpc_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_serialize_rpc_value(v) for v in value]
    if isinstance(value, AbcIterator):
        return [_serialize_rpc_value(v) for v in list(value)]
    return value


@dataclass(frozen=True)
class RemoteControlState:
    path: Path
    socket_path: Path | None = None
    tcp_host: str | None = None
    tcp_port: int | None = None

    @staticmethod
    def from_env(path: Path, raw: str) -> "RemoteControlState":
        raw = str(raw or "").strip()
        if not raw:
            raise ValueError("empty state-writer target")

        # Explicit schemes are preferred.
        if raw.startswith("tcp://"):
            u = urlparse(raw)
            host = u.hostname
            port = u.port
            if not host or port is None:
                raise ValueError(f"invalid tcp state-writer target: {raw!r}")
            return RemoteControlState(path=path, tcp_host=str(host), tcp_port=int(port))

        if raw.startswith("unix://"):
            unix_path = raw.removeprefix("unix://")
            if not unix_path:
                raise ValueError(f"invalid unix state-writer target: {raw!r}")
            return RemoteControlState(path=path, socket_path=Path(unix_path))

        # Back-compat: treat values with path separators as a unix socket path.
        if ("/" in raw) or ("\\" in raw):
            return RemoteControlState(path=path, socket_path=Path(raw))

        # Convenience: allow host:port without a scheme.
        u = urlparse("tcp://" + raw)
        host = u.hostname
        port = u.port
        if host and port is not None:
            return RemoteControlState(path=path, tcp_host=str(host), tcp_port=int(port))

        # Fallback: interpret as a unix socket path.
        return RemoteControlState(path=path, socket_path=Path(raw))

    def _rpc(self, method: str, *args: Any, **kwargs: Any) -> Any:
        req = {"method": str(method), "args": list(args), "kwargs": dict(kwargs)}
        payload = (json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8")
        timeout_s = _STATE_WRITER_RPC_TIMEOUT_S

        if self.socket_path is not None:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                conn.settimeout(timeout_s)
                conn.connect(str(self.socket_path))
                conn.sendall(payload)

                buf = bytearray()
                while True:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    buf.extend(chunk)
                    if b"\n" in chunk:
                        break
        else:
            host = str(self.tcp_host or "").strip()
            if not host or self.tcp_port is None:
                raise RuntimeError("state-writer tcp target not configured")
            with socket.create_connection((host, int(self.tcp_port)), timeout=timeout_s) as conn:
                conn.settimeout(timeout_s)
                conn.sendall(payload)
                buf = bytearray()
                while True:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    buf.extend(chunk)
                    if b"\n" in chunk:
                        break

        if not buf:
            raise RuntimeError(f"state-writer empty response method={method}")

        line = bytes(buf).split(b"\n", 1)[0]
        try:
            resp = json.loads(line.decode("utf-8"))
        except Exception as err:  # noqa: BLE001
            raise RuntimeError(f"state-writer invalid response method={method}: {line!r}") from err

        if not isinstance(resp, dict):
            raise RuntimeError(f"state-writer invalid response method={method}: {resp!r}")
        if bool(resp.get("ok")):
            return resp.get("result")

        err_type = resp.get("error_type")
        err_msg = resp.get("error")
        raise RuntimeError(f"state-writer method failed method={method} type={err_type} error={err_msg}")

    @property
    def conn(self) -> None:
        raise RuntimeError("RemoteControlState has no local sqlite connection; call ControlState methods only")

    def __getattr__(self, name: str) -> Any:
        if name in {"path", "socket_path", "_rpc", "conn"}:
            return object.__getattribute__(self, name)

        def _call(*args: Any, **kwargs: Any) -> Any:
            return self._rpc(name, *args, **kwargs)

        return _call

    def close(self) -> None:
        return None

    def __enter__(self) -> "RemoteControlState":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        self.close()


@dataclass(frozen=True)
class ControlState:
    path: Path
    conn: sqlite3.Connection

    @staticmethod
    def open(path: Path) -> "ControlState | RemoteControlState":
        socket_path_raw = str(os.environ.get(_STATE_WRITER_SOCKET_ENV, "")).strip()
        if socket_path_raw:
            return RemoteControlState.from_env(path=path, raw=socket_path_raw)
        return ControlState.open_local(path)

    @staticmethod
    def open_local(path: Path) -> "ControlState":
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = _connect_sqlite(path)
        state = ControlState(path=path, conn=conn)
        state.init_schema()
        return state

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "ControlState":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        self.close()

    def init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS feed_catalog (
              feed_uri TEXT PRIMARY KEY,
              creator_did TEXT,
              service_did TEXT,
              provider_domain TEXT,
              like_count_last INTEGER,
              discovered_from TEXT NOT NULL,
              first_seen_utc TEXT NOT NULL,
              last_seen_utc TEXT NOT NULL,
              last_hydrated_utc TEXT
            );

            CREATE TABLE IF NOT EXISTS post_registry (
              post_uri TEXT PRIMARY KEY,
              first_seen_utc TEXT NOT NULL,
              last_seen_utc TEXT NOT NULL,
              seen_count INTEGER NOT NULL,
              first_written INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS post_interaction_registry (
              post_uri TEXT PRIMARY KEY,
              first_enqueued_utc TEXT NOT NULL,
              last_hydrated_utc TEXT,
              hydrated_count INTEGER NOT NULL DEFAULT 0,
              FOREIGN KEY(post_uri) REFERENCES post_registry(post_uri) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS post_rq1_factor_registry (
              post_uri TEXT PRIMARY KEY,
              first_enqueued_utc TEXT NOT NULL,
              core_hydrated_at_utc TEXT,
              graph_hydrated_at_utc TEXT,
              repo_hydrated_at_utc TEXT,
              last_hydrated_utc TEXT,
              hydrated_count INTEGER NOT NULL DEFAULT 0,
              FOREIGN KEY(post_uri) REFERENCES post_registry(post_uri) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS queue_posts (
              post_uri TEXT PRIMARY KEY,
              first_seen_utc TEXT NOT NULL,
              priority INTEGER NOT NULL,
              status_likes TEXT,
              status_reposts TEXT,
              status_quotes TEXT,
              status_replies TEXT,
              last_error TEXT,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS runs (
              run_id TEXT PRIMARY KEY,
              job_name TEXT NOT NULL,
              started_at_utc TEXT NOT NULL,
              finished_at_utc TEXT,
              params_json TEXT NOT NULL,
              success INTEGER
            );

            CREATE TABLE IF NOT EXISTS author_registry (
              author_did TEXT PRIMARY KEY,
              first_seen_utc TEXT NOT NULL,
              last_seen_utc TEXT NOT NULL,
              seen_count INTEGER NOT NULL,
              last_hydrated_utc TEXT
            );

            CREATE TABLE IF NOT EXISTS wide_sweep_tasks (
              date_utc TEXT NOT NULL,
              feed_uri TEXT NOT NULL,
              status TEXT NOT NULL,
              attempts INTEGER NOT NULL,
              last_error TEXT,
              updated_at_utc TEXT NOT NULL,
              started_at_utc TEXT,
              finished_at_utc TEXT,
              PRIMARY KEY (date_utc, feed_uri)
            );

            -- Feed generator indexing state (daily, resumable, crash-safe).
            CREATE TABLE IF NOT EXISTS feed_generator_index_global (
              collection TEXT PRIMARY KEY,
              repo_source TEXT NOT NULL,
              repos_cursor TEXT,
              repos_done INTEGER NOT NULL,
              updated_at_utc TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS feed_generator_index_repo_tasks (
              collection TEXT NOT NULL,
              repo_did TEXT NOT NULL,
              status TEXT NOT NULL,
              cursor TEXT,
              attempts INTEGER NOT NULL,
              last_error TEXT,
              first_seen_utc TEXT NOT NULL,
              updated_at_utc TEXT NOT NULL,
              PRIMARY KEY (collection, repo_did)
            );

            CREATE INDEX IF NOT EXISTS idx_fg_index_repo_status
              ON feed_generator_index_repo_tasks(collection, status, attempts, updated_at_utc);

            CREATE TABLE IF NOT EXISTS feed_generator_index_parts (
              collection TEXT NOT NULL,
              date_utc TEXT NOT NULL,
              part_index INTEGER NOT NULL,
              status TEXT NOT NULL,
              started_at_utc TEXT NOT NULL,
              finished_at_utc TEXT,
              n_records INTEGER NOT NULL,
              last_error TEXT,
              PRIMARY KEY (collection, date_utc, part_index)
            );

            CREATE INDEX IF NOT EXISTS idx_post_registry_first_seen_post_uri
              ON post_registry(first_seen_utc, post_uri);

            CREATE INDEX IF NOT EXISTS idx_post_interaction_registry_last_hydrated_post_uri
              ON post_interaction_registry(last_hydrated_utc, post_uri);

            CREATE INDEX IF NOT EXISTS idx_post_rq1_factor_registry_last_hydrated_post_uri
              ON post_rq1_factor_registry(last_hydrated_utc, post_uri);

            """
        )
        self._migrate_post_rq1_factor_registry()
        self.conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_post_rq1_factor_registry_core_hydrated_post_uri
              ON post_rq1_factor_registry(core_hydrated_at_utc, post_uri);

            CREATE INDEX IF NOT EXISTS idx_post_rq1_factor_registry_graph_hydrated_post_uri
              ON post_rq1_factor_registry(graph_hydrated_at_utc, post_uri);

            CREATE INDEX IF NOT EXISTS idx_post_rq1_factor_registry_repo_hydrated_post_uri
              ON post_rq1_factor_registry(repo_hydrated_at_utc, post_uri);
            """
        )
        self.conn.commit()

    def _table_columns(self, table_name: str) -> set[str]:
        rows = self.conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        return {str(row["name"]) for row in rows}

    def _migrate_post_rq1_factor_registry(self) -> None:
        existing_columns = self._table_columns("post_rq1_factor_registry")
        expected_columns = {
            "core_hydrated_at_utc": "TEXT",
            "graph_hydrated_at_utc": "TEXT",
            "repo_hydrated_at_utc": "TEXT",
        }
        for column_name, column_type in expected_columns.items():
            if column_name in existing_columns:
                continue
            self.conn.execute(
                f"ALTER TABLE post_rq1_factor_registry ADD COLUMN {column_name} {column_type}"
            )
        self.conn.execute(
            """
            UPDATE post_rq1_factor_registry
            SET
              core_hydrated_at_utc = COALESCE(core_hydrated_at_utc, last_hydrated_utc),
              graph_hydrated_at_utc = COALESCE(graph_hydrated_at_utc, last_hydrated_utc),
              repo_hydrated_at_utc = COALESCE(repo_hydrated_at_utc, last_hydrated_utc)
            WHERE last_hydrated_utc IS NOT NULL
            """
        )

    def start_run(self, *, run_id: RunId, job_name: str, started_at_utc: str, params: dict[str, Any]) -> None:
        _with_locked_retry(
            lambda: self.conn.execute(
                """
                INSERT OR REPLACE INTO runs(run_id, job_name, started_at_utc, params_json, success)
                VALUES (?, ?, ?, ?, NULL)
                """,
                (str(run_id), job_name, started_at_utc, json.dumps(params, ensure_ascii=False, sort_keys=True)),
            )
        )
        _with_locked_retry(lambda: self.conn.commit())

    def finish_run(self, *, run_id: RunId, finished_at_utc: str, success: bool) -> None:
        _with_locked_retry(
            lambda: self.conn.execute(
                "UPDATE runs SET finished_at_utc=?, success=? WHERE run_id=?",
                (finished_at_utc, 1 if success else 0, str(run_id)),
            )
        )
        _with_locked_retry(lambda: self.conn.commit())

    def upsert_feed_catalog(
        self,
        *,
        feed_uri: FeedUri,
        creator_did: str | None,
        service_did: str | None,
        provider_domain: str | None,
        like_count_last: int | None,
        discovered_from: Sequence[str],
        seen_at_utc: str,
    ) -> None:
        # Merge discovered_from sources so repeated discovery runs accumulate provenance.
        merged_sources = set(str(s) for s in discovered_from if str(s))
        row = self.conn.execute(
            "SELECT discovered_from FROM feed_catalog WHERE feed_uri=?",
            (str(feed_uri),),
        ).fetchone()
        if row is not None:
            raw = row["discovered_from"]
            if isinstance(raw, str) and raw:
                try:
                    prev = json.loads(raw)
                except json.JSONDecodeError:
                    prev = None
                if isinstance(prev, list):
                    merged_sources.update(str(s) for s in prev if isinstance(s, str) and s)

        self.conn.execute(
            """
            INSERT INTO feed_catalog(
              feed_uri, creator_did, service_did, provider_domain,
              like_count_last, discovered_from, first_seen_utc, last_seen_utc, last_hydrated_utc
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(feed_uri) DO UPDATE SET
              creator_did = COALESCE(excluded.creator_did, feed_catalog.creator_did),
              service_did = COALESCE(excluded.service_did, feed_catalog.service_did),
              provider_domain = COALESCE(excluded.provider_domain, feed_catalog.provider_domain),
              like_count_last = COALESCE(excluded.like_count_last, feed_catalog.like_count_last),
              discovered_from = excluded.discovered_from,
              last_seen_utc = excluded.last_seen_utc
            """,
            (
                str(feed_uri),
                creator_did,
                service_did,
                provider_domain,
                like_count_last,
                json.dumps(sorted(merged_sources), ensure_ascii=False),
                seen_at_utc,
                seen_at_utc,
            ),
        )

    def commit(self) -> None:
        _with_locked_retry(lambda: self.conn.commit())

    def iter_feed_catalog(self) -> Iterator[sqlite3.Row]:
        cur = self.conn.execute(
            """
            SELECT feed_uri, creator_did, service_did, provider_domain, like_count_last,
                   discovered_from, first_seen_utc, last_seen_utc, last_hydrated_utc
            FROM feed_catalog
            ORDER BY (like_count_last IS NULL) ASC, like_count_last DESC, feed_uri
            """
        )
        yield from cur

    def select_feed_generators_to_hydrate(self, *, limit: int, include_hydrated: bool = False) -> list[str]:
        conditions = []
        if not include_hydrated:
            conditions.append("last_hydrated_utc IS NULL")
        where_sql = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        cur = self.conn.execute(
            f"""
            SELECT feed_uri
            FROM feed_catalog
            {where_sql}
            ORDER BY (like_count_last IS NULL) ASC, like_count_last DESC, first_seen_utc ASC, feed_uri ASC
            LIMIT ?
            """,
            (int(limit),),
        )
        return [str(r["feed_uri"]) for r in cur.fetchall()]

    def mark_feed_generators_hydrated(self, *, feed_uris: Sequence[FeedUri], hydrated_at_utc: str) -> None:
        if not feed_uris:
            return
        self.conn.executemany(
            "UPDATE feed_catalog SET last_hydrated_utc=? WHERE feed_uri=?",
            [(hydrated_at_utc, str(uri)) for uri in feed_uris],
        )

    def list_feed_catalog_uris(
        self,
        *,
        limit: int,
        after_feed_uri: str | None = None,
    ) -> list[str]:
        limit = int(limit)
        if limit <= 0:
            return []
        if after_feed_uri is not None and str(after_feed_uri):
            cur = self.conn.execute(
                """
                SELECT feed_uri
                FROM feed_catalog
                WHERE feed_uri > ?
                ORDER BY feed_uri ASC
                LIMIT ?
                """,
                (str(after_feed_uri), limit),
            )
        else:
            cur = self.conn.execute(
                """
                SELECT feed_uri
                FROM feed_catalog
                ORDER BY feed_uri ASC
                LIMIT ?
                """,
                (limit,),
            )
        return [str(r["feed_uri"]) for r in cur.fetchall()]

    def update_feed_catalog_like_counts(
        self,
        *,
        rows: Sequence[tuple[str, int | None]],
        hydrated_at_utc: str,
    ) -> int:
        if not rows:
            return 0
        hydrated_at_utc = str(hydrated_at_utc)
        self.conn.executemany(
            "UPDATE feed_catalog SET like_count_last=?, last_hydrated_utc=? WHERE feed_uri=?",
            [(like_count, hydrated_at_utc, str(feed_uri)) for feed_uri, like_count in rows],
        )
        _with_locked_retry(lambda: self.conn.commit())
        # sqlite3's rowcount behavior for executemany is not reliable; return attempted updates.
        return int(len(rows))

    def upsert_post_registry_many(self, *, post_uris: Sequence[PostUri], seen_at_utc: str) -> None:
        rows = [(str(p), seen_at_utc, seen_at_utc) for p in post_uris]
        self.conn.executemany(
            """
            INSERT INTO post_registry(post_uri, first_seen_utc, last_seen_utc, seen_count, first_written)
            VALUES (?, ?, ?, 1, 0)
            ON CONFLICT(post_uri) DO UPDATE SET
              last_seen_utc = excluded.last_seen_utc,
              seen_count = post_registry.seen_count + 1
            """,
            rows,
        )

    def count_post_registry_rows(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM post_registry").fetchone()
        return int(row["n"]) if row is not None else 0

    def select_not_written(self, *, post_uris: Sequence[PostUri]) -> list[PostUri]:
        if not post_uris:
            return []
        placeholders = ",".join("?" for _ in post_uris)
        cur = self.conn.execute(
            f"SELECT post_uri FROM post_registry WHERE post_uri IN ({placeholders}) AND first_written=0",
            tuple(str(p) for p in post_uris),
        )
        return [PostUri(str(r["post_uri"])) for r in cur.fetchall()]

    def mark_first_written(self, *, post_uris: Sequence[PostUri]) -> None:
        if not post_uris:
            return
        self.conn.executemany(
            "UPDATE post_registry SET first_written=1 WHERE post_uri=?",
            [(str(p),) for p in post_uris],
        )

    def ensure_post_interaction_tasks(self, *, post_uris: Sequence[PostUri], enqueued_at_utc: str) -> None:
        rows = [(str(p), enqueued_at_utc) for p in post_uris if str(p)]
        if not rows:
            return
        self.conn.executemany(
            """
            INSERT OR IGNORE INTO post_interaction_registry(post_uri, first_enqueued_utc, last_hydrated_utc, hydrated_count)
            VALUES (?, ?, NULL, 0)
            """,
            rows,
        )

    def select_posts_to_backfill_rows(
        self,
        *,
        limit: int,
        seen_after_utc: str | None = None,
        seen_before_utc: str | None = None,
        include_hydrated: bool = False,
    ) -> list[SelectedPost]:
        conditions = ["1=1"]
        args: list[Any] = []
        if seen_after_utc is not None:
            conditions.append("pr.first_seen_utc >= ?")
            args.append(seen_after_utc)
        if seen_before_utc is not None:
            conditions.append("pr.first_seen_utc < ?")
            args.append(seen_before_utc)
        if not include_hydrated:
            conditions.append("pir.last_hydrated_utc IS NULL")
        query = f"""
            SELECT pr.post_uri, pr.first_seen_utc
            FROM post_registry AS pr
            LEFT JOIN post_interaction_registry AS pir ON pir.post_uri = pr.post_uri
            WHERE {' AND '.join(conditions)}
            ORDER BY pr.first_seen_utc ASC, pr.post_uri ASC
            LIMIT ?
        """
        args.append(int(limit))
        cur = self.conn.execute(query, tuple(args))
        return [
            SelectedPost(post_uri=str(row["post_uri"]), first_seen_utc=str(row["first_seen_utc"]))
            for row in cur.fetchall()
        ]

    def select_posts_to_backfill(
        self,
        *,
        limit: int,
        seen_after_utc: str | None = None,
        seen_before_utc: str | None = None,
        include_hydrated: bool = False,
    ) -> list[str]:
        rows = self.select_posts_to_backfill_rows(
            limit=limit,
            seen_after_utc=seen_after_utc,
            seen_before_utc=seen_before_utc,
            include_hydrated=include_hydrated,
        )
        return [row.post_uri for row in rows]

    def mark_posts_interactions_hydrated(self, *, post_uris: Sequence[PostUri], hydrated_at_utc: str) -> None:
        rows = [(str(p), hydrated_at_utc) for p in post_uris if str(p)]
        if not rows:
            return
        self.conn.executemany(
            """
            INSERT INTO post_interaction_registry(post_uri, first_enqueued_utc, last_hydrated_utc, hydrated_count)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(post_uri) DO UPDATE SET
              last_hydrated_utc = excluded.last_hydrated_utc,
              hydrated_count = post_interaction_registry.hydrated_count + 1
            """,
            [(post_uri, hydrated_at_utc, hydrated_at_utc) for post_uri, hydrated_at_utc in rows],
        )

    def ensure_post_rq1_factor_tasks(self, *, post_uris: Sequence[PostUri], enqueued_at_utc: str) -> None:
        rows = [(str(p), enqueued_at_utc) for p in post_uris if str(p)]
        if not rows:
            return
        self.conn.executemany(
            """
            INSERT OR IGNORE INTO post_rq1_factor_registry(
              post_uri,
              first_enqueued_utc,
              core_hydrated_at_utc,
              graph_hydrated_at_utc,
              repo_hydrated_at_utc,
              last_hydrated_utc,
              hydrated_count
            )
            VALUES (?, ?, NULL, NULL, NULL, NULL, 0)
            """,
            rows,
        )

    def select_posts_to_backfill_rq1_rows(
        self,
        *,
        limit: int,
        seen_after_utc: str | None = None,
        seen_before_utc: str | None = None,
        include_hydrated: bool = False,
        stage: str = "all",
        shard_index: int = 0,
        shard_count: int = 1,
    ) -> list[SelectedPost]:
        limit = int(limit)
        if limit <= 0:
            return []
        stage = normalize_rq1_stage(stage)
        shard_count = max(1, int(shard_count))
        shard_index = int(shard_index)
        if shard_index < 0 or shard_index >= shard_count:
            raise ValueError(f"shard_index must be within [0,{shard_count}), got {shard_index}")

        conditions = ["1=1"]
        args: list[Any] = []
        if seen_after_utc is not None:
            conditions.append("pr.first_seen_utc >= ?")
            args.append(seen_after_utc)
        if seen_before_utc is not None:
            conditions.append("pr.first_seen_utc < ?")
            args.append(seen_before_utc)
        conditions.extend(rq1_stage_prerequisite_clauses(stage))
        if not include_hydrated:
            conditions.append(f"{rq1_stage_pending_column(stage)} IS NULL")

        query = f"""
            SELECT pr.post_uri, pr.first_seen_utc
            FROM post_registry AS pr
            LEFT JOIN post_rq1_factor_registry AS prr ON prr.post_uri = pr.post_uri
            WHERE {' AND '.join(conditions)}
            ORDER BY pr.first_seen_utc ASC, pr.post_uri ASC
        """

        if shard_count == 1:
            cur = self.conn.execute(f"{query}\nLIMIT ?", tuple([*args, limit]))
            return [
                SelectedPost(post_uri=str(row["post_uri"]), first_seen_utc=str(row["first_seen_utc"]))
                for row in cur.fetchall()
            ]

        cur = self.conn.execute(query, tuple(args))
        out: list[SelectedPost] = []
        for row in cur:
            post_uri = str(row["post_uri"])
            if _stable_shard(post_uri, shard_count) != shard_index:
                continue
            out.append(SelectedPost(post_uri=post_uri, first_seen_utc=str(row["first_seen_utc"])))
            if len(out) >= limit:
                break
        return out

    def select_posts_to_backfill_rq1(
        self,
        *,
        limit: int,
        seen_after_utc: str | None = None,
        seen_before_utc: str | None = None,
        include_hydrated: bool = False,
        stage: str = "all",
        shard_index: int = 0,
        shard_count: int = 1,
    ) -> list[str]:
        rows = self.select_posts_to_backfill_rq1_rows(
            limit=limit,
            seen_after_utc=seen_after_utc,
            seen_before_utc=seen_before_utc,
            include_hydrated=include_hydrated,
            stage=stage,
            shard_index=shard_index,
            shard_count=shard_count,
        )
        return [row.post_uri for row in rows]

    def mark_posts_rq1_factors_hydrated(
        self,
        *,
        post_uris: Sequence[PostUri],
        hydrated_at_utc: str,
        stage: str = "all",
    ) -> None:
        rows = [(str(p), hydrated_at_utc) for p in post_uris if str(p)]
        if not rows:
            return
        stage = normalize_rq1_stage(stage)
        completion_columns = rq1_stage_completion_columns(stage)
        insert_columns = ["post_uri", "first_enqueued_utc", "hydrated_count"]
        insert_values = ["?", "?", "1"]
        update_assignments = ["hydrated_count = post_rq1_factor_registry.hydrated_count + 1"]
        params: list[tuple[Any, ...]] = []
        for column_name in completion_columns:
            insert_columns.append(column_name)
            insert_values.append("?")
            update_assignments.append(f"{column_name} = excluded.{column_name}")
        sql = f"""
            INSERT INTO post_rq1_factor_registry({", ".join(insert_columns)})
            VALUES ({", ".join(insert_values)})
            ON CONFLICT(post_uri) DO UPDATE SET
              {", ".join(update_assignments)}
        """
        for post_uri, hydrated_value in rows:
            param_values: list[Any] = [post_uri, hydrated_value]
            param_values.extend([hydrated_value] * len(completion_columns))
            params.append(tuple(param_values))
        self.conn.executemany(
            sql,
            params,
        )

    def upsert_author_registry_many(self, *, author_dids: Sequence[str], seen_at_utc: str) -> None:
        rows = [(str(d), seen_at_utc, seen_at_utc) for d in author_dids if str(d)]
        if not rows:
            return
        self.conn.executemany(
            """
            INSERT INTO author_registry(author_did, first_seen_utc, last_seen_utc, seen_count, last_hydrated_utc)
            VALUES (?, ?, ?, 1, NULL)
            ON CONFLICT(author_did) DO UPDATE SET
              last_seen_utc = excluded.last_seen_utc,
              seen_count = author_registry.seen_count + 1
            """,
            rows,
        )

    def count_author_registry_rows(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM author_registry").fetchone()
        return int(row["n"]) if row is not None else 0

    def select_authors_to_hydrate(
        self,
        *,
        limit: int,
        seen_after_utc: str | None = None,
        seen_before_utc: str | None = None,
    ) -> list[str]:
        conditions = ["last_hydrated_utc IS NULL"]
        args: list[Any] = []

        if seen_after_utc is not None:
            conditions.append("first_seen_utc >= ?")
            args.append(seen_after_utc)
        if seen_before_utc is not None:
            conditions.append("first_seen_utc < ?")
            args.append(seen_before_utc)

        query = f"""
            SELECT author_did
            FROM author_registry
            WHERE {' AND '.join(conditions)}
            ORDER BY first_seen_utc ASC
            LIMIT ?
            """
        args.append(int(limit))

        cur = self.conn.execute(query, tuple(args))
        return [str(r["author_did"]) for r in cur.fetchall()]

    def mark_authors_hydrated(self, *, author_dids: Sequence[str], hydrated_at_utc: str) -> None:
        if not author_dids:
            return
        self.conn.executemany(
            "UPDATE author_registry SET last_hydrated_utc=? WHERE author_did=?",
            [(hydrated_at_utc, str(d)) for d in author_dids],
        )

    def ensure_wide_tasks(self, *, date_utc: str, feed_uris: Sequence[FeedUri], updated_at_utc: str) -> None:
        rows = [(date_utc, str(u), "pending", 0, None, updated_at_utc, None, None) for u in feed_uris]
        if not rows:
            return
        self.conn.executemany(
            """
            INSERT OR IGNORE INTO wide_sweep_tasks(
              date_utc, feed_uri, status, attempts, last_error, updated_at_utc, started_at_utc, finished_at_utc
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )

    def wide_pending_tasks(self, *, date_utc: str, max_attempts: int) -> list[tuple[FeedUri, int, str]]:
        cur = self.conn.execute(
            """
            SELECT feed_uri, attempts, status
            FROM wide_sweep_tasks
            WHERE date_utc=? AND status IN ('pending','failed') AND attempts < ?
            ORDER BY attempts ASC, feed_uri ASC
            """,
            (date_utc, int(max_attempts)),
        )
        return [(FeedUri(str(r["feed_uri"])), int(r["attempts"]), str(r["status"])) for r in cur.fetchall()]

    def mark_wide_in_progress(self, *, date_utc: str, feed_uri: FeedUri, started_at_utc: str) -> str | None:
        row = self.conn.execute(
            "SELECT status FROM wide_sweep_tasks WHERE date_utc=? AND feed_uri=?",
            (date_utc, str(feed_uri)),
        ).fetchone()
        prev = str(row["status"]) if row is not None else None
        self.conn.execute(
            """
            UPDATE wide_sweep_tasks
            SET status='in_progress', attempts=attempts+1, updated_at_utc=?, started_at_utc=?
            WHERE date_utc=? AND feed_uri=?
            """,
            (started_at_utc, started_at_utc, date_utc, str(feed_uri)),
        )
        return prev

    def mark_wide_done(
        self,
        *,
        date_utc: str,
        feed_uri: FeedUri,
        success: bool,
        finished_at_utc: str,
        last_error: str | None,
    ) -> None:
        status = "success" if success else "failed"
        self.conn.execute(
            """
            UPDATE wide_sweep_tasks
            SET status=?, updated_at_utc=?, finished_at_utc=?, last_error=?
            WHERE date_utc=? AND feed_uri=?
            """,
            (status, finished_at_utc, finished_at_utc, last_error, date_utc, str(feed_uri)),
        )

    def count_wide_by_status(self, *, date_utc: str) -> dict[str, int]:
        cur = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM wide_sweep_tasks WHERE date_utc=? GROUP BY status",
            (date_utc,),
        )
        return {str(r["status"]): int(r["n"]) for r in cur.fetchall()}

    # ---- Feed generator index state (global, resumable) ----

    def ensure_feed_generator_index_global(
        self,
        *,
        collection: str,
        repo_source: str,
        updated_at_utc: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT OR IGNORE INTO feed_generator_index_global(
              collection, repo_source, repos_cursor, repos_done, updated_at_utc
            )
            VALUES (?, ?, NULL, 0, ?)
            """,
            (str(collection), str(repo_source), str(updated_at_utc)),
        )

    def get_feed_generator_index_global(self, *, collection: str) -> sqlite3.Row | None:
        return self.conn.execute(
            """
            SELECT collection, repo_source, repos_cursor, repos_done, updated_at_utc
            FROM feed_generator_index_global
            WHERE collection=?
            """,
            (str(collection),),
        ).fetchone()

    def update_feed_generator_index_global(
        self,
        *,
        collection: str,
        repo_source: str,
        repos_cursor: str | None,
        repos_done: bool,
        updated_at_utc: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO feed_generator_index_global(
              collection, repo_source, repos_cursor, repos_done, updated_at_utc
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(collection) DO UPDATE SET
              repo_source = excluded.repo_source,
              repos_cursor = excluded.repos_cursor,
              repos_done = excluded.repos_done,
              updated_at_utc = excluded.updated_at_utc
            """,
            (
                str(collection),
                str(repo_source),
                repos_cursor,
                1 if repos_done else 0,
                str(updated_at_utc),
            ),
        )

    def reset_feed_generator_index_in_progress_to_pending(self, *, collection: str, updated_at_utc: str) -> int:
        cur = self.conn.execute(
            """
            UPDATE feed_generator_index_repo_tasks
            SET status='pending', updated_at_utc=?
            WHERE collection=? AND status='in_progress'
            """,
            (str(updated_at_utc), str(collection)),
        )
        return int(cur.rowcount)

    def ensure_feed_generator_index_repo_tasks(
        self,
        *,
        collection: str,
        repo_dids: Sequence[str],
        first_seen_utc: str,
        updated_at_utc: str,
    ) -> int:
        rows = [
            (str(collection), str(did), "pending", None, 0, None, str(first_seen_utc), str(updated_at_utc))
            for did in repo_dids
            if str(did)
        ]
        if not rows:
            return 0
        cur = self.conn.executemany(
            """
            INSERT OR IGNORE INTO feed_generator_index_repo_tasks(
              collection, repo_did, status, cursor, attempts, last_error, first_seen_utc, updated_at_utc
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        return int(cur.rowcount)

    def next_feed_generator_index_repo_tasks(
        self,
        *,
        collection: str,
        limit: int,
        max_attempts: int,
    ) -> list[tuple[str, str | None, int]]:
        cur = self.conn.execute(
            """
            SELECT repo_did, cursor, attempts
            FROM feed_generator_index_repo_tasks
            WHERE collection=? AND status IN ('pending','failed') AND attempts < ?
            ORDER BY attempts ASC, updated_at_utc ASC, repo_did ASC
            LIMIT ?
            """,
            (str(collection), int(max_attempts), int(limit)),
        )
        out: list[tuple[str, str | None, int]] = []
        for r in cur.fetchall():
            cursor_raw = r["cursor"]
            cursor = str(cursor_raw) if isinstance(cursor_raw, str) and cursor_raw else None
            out.append((str(r["repo_did"]), cursor, int(r["attempts"])))
        return out

    def count_feed_generator_index_repo_tasks_by_status(self, *, collection: str) -> dict[str, int]:
        cur = self.conn.execute(
            """
            SELECT status, COUNT(*) AS n
            FROM feed_generator_index_repo_tasks
            WHERE collection=?
            GROUP BY status
            """,
            (str(collection),),
        )
        return {str(r["status"]): int(r["n"]) for r in cur.fetchall()}

    def mark_feed_generator_index_repo_in_progress(
        self,
        *,
        collection: str,
        repo_did: str,
        started_at_utc: str,
    ) -> None:
        self.conn.execute(
            """
            UPDATE feed_generator_index_repo_tasks
            SET status='in_progress',
                attempts=attempts+1,
                updated_at_utc=?
            WHERE collection=? AND repo_did=?
            """,
            (str(started_at_utc), str(collection), str(repo_did)),
        )

    def mark_feed_generator_index_repo_done(
        self,
        *,
        collection: str,
        repo_did: str,
        status: str,
        cursor: str | None,
        updated_at_utc: str,
        last_error: str | None,
    ) -> None:
        self.conn.execute(
            """
            UPDATE feed_generator_index_repo_tasks
            SET status=?,
                cursor=?,
                last_error=?,
                updated_at_utc=?
            WHERE collection=? AND repo_did=?
            """,
            (str(status), cursor, last_error, str(updated_at_utc), str(collection), str(repo_did)),
        )

    def delete_feed_generator_index_repo_task(self, *, collection: str, repo_did: str) -> None:
        self.conn.execute(
            "DELETE FROM feed_generator_index_repo_tasks WHERE collection=? AND repo_did=?",
            (str(collection), str(repo_did)),
        )

    def next_feed_generator_index_part_index(self, *, collection: str, date_utc: str) -> int:
        row = self.conn.execute(
            """
            SELECT MAX(part_index) AS m
            FROM feed_generator_index_parts
            WHERE collection=? AND date_utc=?
            """,
            (str(collection), str(date_utc)),
        ).fetchone()
        if row is None:
            return 0
        m = row["m"]
        try:
            return int(m) + 1 if m is not None else 0
        except Exception:  # noqa: BLE001
            return 0

    def start_feed_generator_index_part(
        self,
        *,
        collection: str,
        date_utc: str,
        part_index: int,
        started_at_utc: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO feed_generator_index_parts(
              collection, date_utc, part_index, status, started_at_utc, finished_at_utc, n_records, last_error
            )
            VALUES (?, ?, ?, 'in_progress', ?, NULL, 0, NULL)
            """,
            (str(collection), str(date_utc), int(part_index), str(started_at_utc)),
        )

    def finish_feed_generator_index_part(
        self,
        *,
        collection: str,
        date_utc: str,
        part_index: int,
        success: bool,
        finished_at_utc: str,
        n_records: int,
        last_error: str | None,
    ) -> None:
        status = "success" if success else "failed"
        self.conn.execute(
            """
            UPDATE feed_generator_index_parts
            SET status=?,
                finished_at_utc=?,
                n_records=?,
                last_error=?
            WHERE collection=? AND date_utc=? AND part_index=?
            """,
            (
                str(status),
                str(finished_at_utc),
                int(n_records),
                last_error,
                str(collection),
                str(date_utc),
                int(part_index),
            ),
        )

    def iter_feed_generator_index_in_progress_parts(
        self, *, collection: str, date_utc: str
    ) -> Iterator[sqlite3.Row]:
        cur = self.conn.execute(
            """
            SELECT collection, date_utc, part_index, status, started_at_utc, finished_at_utc, n_records, last_error
            FROM feed_generator_index_parts
            WHERE collection=? AND date_utc=? AND status='in_progress'
            ORDER BY part_index ASC
            """,
            (str(collection), str(date_utc)),
        )
        yield from cur


def dispatch_state_rpc(state: ControlState, *, method: str, args: Sequence[Any], kwargs: dict[str, Any]) -> dict[str, Any]:
    if method == "ping":
        return {"ok": True, "result": {"status": "ok"}}
    if method == "shutdown":
        return {"ok": True, "result": {"shutdown": True}}
    if not method or method.startswith("_"):
        return {"ok": False, "error_type": "ValueError", "error": f"unsupported method: {method!r}"}

    fn = getattr(state, method, None)
    if fn is None or not callable(fn):
        return {"ok": False, "error_type": "AttributeError", "error": f"unknown method: {method}"}

    try:
        result = fn(*list(args), **dict(kwargs))
    except Exception as err:  # noqa: BLE001
        return {"ok": False, "error_type": type(err).__name__, "error": repr(err)}
    return {"ok": True, "result": _serialize_rpc_value(result)}


@dataclass(frozen=True)
class SnapshotStatusDB:
    path: Path
    conn: sqlite3.Connection

    @staticmethod
    def open(path: Path) -> "SnapshotStatusDB":
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = _connect_sqlite(path)
        db = SnapshotStatusDB(path=path, conn=conn)
        db.init_schema()
        _fsync_file_and_dir(path)
        return db

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "SnapshotStatusDB":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        self.close()

    def init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS feed_tasks (
              feed_uri TEXT NOT NULL,
              viewer_mode TEXT NOT NULL,
              task_order INTEGER,
              status TEXT NOT NULL,
              attempts INTEGER NOT NULL,
              last_error TEXT,
              updated_at_utc TEXT NOT NULL,
              started_at_utc TEXT,
              finished_at_utc TEXT,
              PRIMARY KEY (feed_uri, viewer_mode)
            );
            """
        )
        existing = {str(row["name"]) for row in self.conn.execute("PRAGMA table_info(feed_tasks)").fetchall()}
        if "task_order" not in existing:
            self.conn.execute("ALTER TABLE feed_tasks ADD COLUMN task_order INTEGER")
        self.conn.commit()

    def reset_in_progress_to_pending(self, *, updated_at_utc: str) -> int:
        cur = self.conn.execute(
            """
            UPDATE feed_tasks
            SET status='pending', updated_at_utc=?
            WHERE status='in_progress'
            """,
            (updated_at_utc,),
        )
        self.conn.commit()
        return int(cur.rowcount)

    def ensure_tasks(
        self,
        *,
        tasks: Sequence[tuple[FeedUri, ViewerMode]],
        updated_at_utc: str,
        task_order_by_task: dict[tuple[str, str], int] | None = None,
    ) -> None:
        rows: list[tuple[str, str, int | None, str, int, str | None, str, str | None, str | None]] = []
        for feed_uri, viewer_mode in tasks:
            task_key = (str(feed_uri), str(viewer_mode))
            rows.append(
                (
                    task_key[0],
                    task_key[1],
                    (task_order_by_task or {}).get(task_key),
                    "pending",
                    0,
                    None,
                    updated_at_utc,
                    None,
                    None,
                )
            )
        self.conn.executemany(
            """
            INSERT OR IGNORE INTO feed_tasks(
              feed_uri, viewer_mode, task_order, status, attempts, last_error, updated_at_utc, started_at_utc, finished_at_utc
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        if task_order_by_task:
            self.conn.executemany(
                """
                UPDATE feed_tasks
                SET task_order=COALESCE(task_order, ?)
                WHERE feed_uri=? AND viewer_mode=?
                """,
                [(order, feed_uri, viewer_mode) for (feed_uri, viewer_mode), order in task_order_by_task.items()],
            )
        self.conn.commit()

    def counts_by_status(self) -> dict[str, int]:
        cur = self.conn.execute("SELECT status, COUNT(*) AS n FROM feed_tasks GROUP BY status")
        return {str(r["status"]): int(r["n"]) for r in cur.fetchall()}

    def get_task_status(self, *, feed_uri: FeedUri, viewer_mode: ViewerMode) -> str | None:
        row = self.conn.execute(
            "SELECT status FROM feed_tasks WHERE feed_uri=? AND viewer_mode=?",
            (str(feed_uri), str(viewer_mode)),
        ).fetchone()
        if row is None:
            return None
        return str(row["status"])

    def pending_tasks(self, *, max_attempts: int) -> list[tuple[FeedUri, ViewerMode, int]]:
        cur = self.conn.execute(
            """
            SELECT feed_uri, viewer_mode, attempts
            FROM feed_tasks
            WHERE status IN ('pending', 'failed') AND attempts < ?
            ORDER BY
              attempts ASC,
              CASE WHEN task_order IS NULL THEN 1 ELSE 0 END ASC,
              task_order ASC,
              feed_uri ASC
            """,
            (max_attempts,),
        )
        return [(FeedUri(str(r["feed_uri"])), str(r["viewer_mode"]), int(r["attempts"])) for r in cur.fetchall()]

    def pending_tasks_with_order(self, *, max_attempts: int) -> list[tuple[FeedUri, ViewerMode, int, int | None]]:
        cur = self.conn.execute(
            """
            SELECT feed_uri, viewer_mode, attempts, task_order
            FROM feed_tasks
            WHERE status IN ('pending', 'failed') AND attempts < ?
            ORDER BY (task_order IS NULL) ASC, task_order ASC, attempts ASC, feed_uri ASC
            """,
            (max_attempts,),
        )
        return [
            (
                FeedUri(str(r["feed_uri"])),
                str(r["viewer_mode"]),
                int(r["attempts"]),
                (int(r["task_order"]) if r["task_order"] is not None else None),
            )
            for r in cur.fetchall()
        ]

    def mark_in_progress(self, *, feed_uri: FeedUri, viewer_mode: ViewerMode, started_at_utc: str) -> None:
        self.conn.execute(
            """
            UPDATE feed_tasks
            SET status='in_progress',
                attempts=attempts + 1,
                updated_at_utc=?,
                started_at_utc=?
            WHERE feed_uri=? AND viewer_mode=?
            """,
            (started_at_utc, started_at_utc, str(feed_uri), str(viewer_mode)),
        )
        self.conn.commit()

    def mark_done(
        self,
        *,
        feed_uri: FeedUri,
        viewer_mode: ViewerMode,
        success: bool,
        finished_at_utc: str,
        last_error: str | None,
    ) -> None:
        status = "success" if success else "failed"
        self.conn.execute(
            """
            UPDATE feed_tasks
            SET status=?,
                updated_at_utc=?,
                finished_at_utc=?,
                last_error=?
            WHERE feed_uri=? AND viewer_mode=?
            """,
            (status, finished_at_utc, finished_at_utc, last_error, str(feed_uri), str(viewer_mode)),
        )
        self.conn.commit()
