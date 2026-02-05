from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SqlitePaths:
    db_path: Path


class StateDB:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("StateDB not connected")
        return self._conn

    def connect(self) -> None:
        self._conn = sqlite3.connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._conn.execute("PRAGMA temp_store=MEMORY;")
        self._conn.execute("PRAGMA cache_size=-200000;")  # ~200MB
        self._conn.commit()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "StateDB":
        self.connect()
        self.init_schema()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        self.close()

    def init_schema(self) -> None:
        c = self.conn
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS errors (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              stage TEXT NOT NULL,
              key TEXT NOT NULL,
              error_type TEXT NOT NULL,
              http_status INTEGER,
              error_message TEXT NOT NULL,
              when_utc TEXT NOT NULL,
              retry_count INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS http_stats_endpoint (
              endpoint_name TEXT PRIMARY KEY,
              request_count INTEGER NOT NULL,
              success_count INTEGER NOT NULL,
              rate_limited_count INTEGER NOT NULL,
              total_latency_ms REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS http_latency_hist (
              endpoint_name TEXT NOT NULL,
              bin_upper_ms REAL NOT NULL,
              count INTEGER NOT NULL,
              PRIMARY KEY (endpoint_name, bin_upper_ms)
            );

            CREATE TABLE IF NOT EXISTS actor_processed (
              actor_did TEXT PRIMARY KEY,
              processed_at_utc TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS starterpack_actor_processed (
              actor_did TEXT PRIMARY KEY,
              processed_at_utc TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS feed_generators (
              feed_uri TEXT PRIMARY KEY,
              creator_did TEXT NOT NULL,
              rkey TEXT NOT NULL,
              service_did TEXT,
              provider_bucket TEXT NOT NULL,
              display_name TEXT,
              description TEXT,
              accepts_interaction INTEGER,
              content_mode TEXT,
              indexed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS starterpacks (
              starterpack_uri TEXT PRIMARY KEY,
              creator_did TEXT,
              name TEXT,
              description TEXT,
              collected_at_utc TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS starterpack_feeds (
              starterpack_uri TEXT NOT NULL,
              slot_index INTEGER NOT NULL,
              feed_uri TEXT NOT NULL,
              PRIMARY KEY (starterpack_uri, slot_index)
            );

            CREATE TABLE IF NOT EXISTS popular_feeds (
              feed_uri TEXT PRIMARY KEY,
              popularity_rank INTEGER NOT NULL,
              collected_at_utc TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS feed_panel (
              feed_uri TEXT PRIMARY KEY,
              feed_group TEXT NOT NULL,
              selection_reason TEXT NOT NULL,
              provider_bucket TEXT,
              service_did TEXT,
              creator_did TEXT,
              display_name TEXT,
              inclusion_count INTEGER,
              popularity_rank INTEGER
            );

            CREATE TABLE IF NOT EXISTS feed_snapshot_status (
              feed_uri TEXT NOT NULL,
              viewer_mode TEXT NOT NULL,
              feed_group TEXT NOT NULL,
              collected_at_utc TEXT NOT NULL,
              requested_items INTEGER NOT NULL,
              returned_items INTEGER NOT NULL,
              pages_fetched INTEGER NOT NULL,
              success INTEGER NOT NULL,
              http_status INTEGER,
              error_type TEXT,
              error_message_short TEXT,
              PRIMARY KEY (feed_uri, viewer_mode)
            );

            CREATE TABLE IF NOT EXISTS feed_items (
              feed_uri TEXT NOT NULL,
              feed_group TEXT NOT NULL,
              viewer_mode TEXT NOT NULL,
              collected_at_utc TEXT NOT NULL,
              rank INTEGER NOT NULL,
              post_uri TEXT NOT NULL,
              post_cid TEXT NOT NULL,
              author_did TEXT NOT NULL,
              author_handle TEXT,
              reason_type TEXT,
              reason_actor_did TEXT,
              PRIMARY KEY (feed_uri, viewer_mode, rank)
            );

            CREATE TABLE IF NOT EXISTS posts (
              post_uri TEXT NOT NULL,
              post_cid TEXT NOT NULL,
              author_did TEXT NOT NULL,
              author_handle TEXT,
              record_created_at TEXT,
              indexed_at TEXT,
              text TEXT,
              text_len INTEGER,
              is_reply INTEGER,
              reply_parent_uri TEXT,
              reply_root_uri TEXT,
              is_quote INTEGER,
              quoted_uri TEXT,
              embed_type TEXT,
              image_count INTEGER,
              external_uri TEXT,
              external_domain TEXT,
              facet_link_count INTEGER,
              link_domains_json TEXT,
              mention_count INTEGER,
              hashtag_count INTEGER,
              like_count INTEGER,
              repost_count INTEGER,
              reply_count INTEGER,
              quote_count INTEGER,
              langs_json TEXT,
              post_labels_json TEXT,
              author_labels_json TEXT,
              PRIMARY KEY (post_uri, post_cid)
            );

            CREATE TABLE IF NOT EXISTS post_labels (
              post_uri TEXT NOT NULL,
              post_cid TEXT NOT NULL,
              feed_uri TEXT NOT NULL,
              viewer_mode TEXT NOT NULL,
              collected_at_utc TEXT NOT NULL,
              label_src TEXT NOT NULL,
              label_val TEXT NOT NULL,
              label_neg INTEGER,
              label_uri TEXT,
              PRIMARY KEY (post_uri, post_cid, feed_uri, viewer_mode, label_src, label_val, label_uri)
            );

            CREATE TABLE IF NOT EXISTS authors (
              author_did TEXT PRIMARY KEY,
              handle TEXT,
              display_name TEXT,
              followers_count INTEGER,
              follows_count INTEGER,
              posts_count INTEGER,
              collected_at_utc TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS validations (
              check_name TEXT PRIMARY KEY,
              status TEXT NOT NULL,
              observed_value TEXT,
              expected_threshold TEXT,
              notes TEXT
            );
            """
        )
        c.commit()

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        return str(row["value"])

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()
