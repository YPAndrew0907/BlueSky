from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from bsky_collector_v2.archive_scan import RunPartition, iter_branch_roots, iter_partitions


URL_RE = re.compile(r"https?://[^\s<>()\"']+")
HASHTAG_RE = re.compile(r"(?<!\w)#([a-z0-9_]{2,64})", flags=re.IGNORECASE)
MENTION_RE = re.compile(r"(?<!\w)@([a-z0-9._-]{2,128})", flags=re.IGNORECASE)
TOKEN_RE = re.compile(r"[a-z0-9]{3,}", flags=re.IGNORECASE)
STOPWORDS = {
    "about",
    "after",
    "again",
    "also",
    "because",
    "been",
    "being",
    "from",
    "have",
    "into",
    "just",
    "like",
    "more",
    "over",
    "some",
    "than",
    "that",
    "their",
    "them",
    "then",
    "there",
    "these",
    "they",
    "this",
    "those",
    "what",
    "when",
    "where",
    "which",
    "with",
    "would",
    "your",
    "while",
    "post",
    "posts",
    "bluesky",
}


@dataclass(frozen=True)
class IndexedPost:
    post_uri: str
    post_cid: str
    author_did: str
    author_handle: str
    branch: str
    surface: str
    date_utc: str
    hour_utc: str | None
    viewer_mode: str
    vantage_id: str
    feed_uri: str
    bucket: str
    record_created_at: str
    indexed_at: str
    text: str


@dataclass(frozen=True)
class EventAnchor:
    kind: str
    value: str
    window_start_utc: str

    @property
    def cluster_id(self) -> str:
        raw = f"{self.kind}|{self.value}|{self.window_start_utc}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


@dataclass
class ClusterAccumulator:
    cluster_id: str
    anchor_kind: str
    anchor_value: str
    window_start_utc: str
    post_uris: set[str] = field(default_factory=set)
    author_dids: set[str] = field(default_factory=set)
    matched_queries: Counter[str] = field(default_factory=Counter)
    example_texts: list[str] = field(default_factory=list)
    exposure_rows: int = 0
    auth_rows: int = 0
    unauth_rows: int = 0
    unique_feeds: set[str] = field(default_factory=set)
    bucket_counts: Counter[str] = field(default_factory=Counter)


def normalize_text(text: str) -> str:
    return " ".join(str(text).split()).strip()


def parse_utc(ts: str) -> datetime:
    if not ts:
        raise ValueError("empty timestamp")
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts).astimezone(UTC)


def floor_time(dt: datetime, hours: int) -> datetime:
    floored_hour = dt.hour - (dt.hour % hours)
    return dt.replace(hour=floored_hour, minute=0, second=0, microsecond=0)


def clean_url(raw_url: str) -> str:
    stripped = raw_url.rstrip(".,;:!?)]}\"'")
    try:
        parsed = urlparse(stripped)
    except ValueError:
        return stripped.lower()
    host = (parsed.netloc or "").lower()
    path = parsed.path or ""
    if not parsed.scheme:
        return stripped.lower()
    return f"{parsed.scheme.lower()}://{host}{path}"


def safe_url_domain(raw_url: str | None) -> str | None:
    if not raw_url:
        return None
    try:
        parsed = urlparse(raw_url)
    except ValueError:
        return None
    host = (parsed.netloc or "").lower()
    return host or None


def extract_urls(text: str) -> list[str]:
    return [clean_url(match.group(0)) for match in URL_RE.finditer(text)]


def extract_hashtags(text: str) -> list[str]:
    return [match.group(1).lower() for match in HASHTAG_RE.finditer(text)]


def extract_mentions(text: str) -> list[str]:
    return [match.group(1).lower() for match in MENTION_RE.finditer(text)]


def token_signature(text: str, *, limit: int = 5) -> str:
    tokens: list[str] = []
    for match in TOKEN_RE.finditer(text.lower()):
        token = match.group(0)
        if token in STOPWORDS:
            continue
        if token not in tokens:
            tokens.append(token)
        if len(tokens) >= limit:
            break
    return "|".join(tokens)


def build_event_anchor(
    *,
    text: str,
    record_created_at: str,
    indexed_at: str,
    time_window_hours: int,
) -> EventAnchor | None:
    canonical_text = normalize_text(text)
    if not canonical_text:
        return None

    urls = extract_urls(canonical_text)
    hashtags = extract_hashtags(canonical_text)
    signature = token_signature(canonical_text)
    timestamp = record_created_at or indexed_at
    if not timestamp:
        return None
    dt = floor_time(parse_utc(timestamp), time_window_hours)
    window_start = dt.isoformat().replace("+00:00", "Z")

    if urls:
        return EventAnchor(kind="url", value=urls[0], window_start_utc=window_start)
    if hashtags:
        top_tags = "|".join(sorted(dict.fromkeys(hashtags))[:3])
        return EventAnchor(kind="hashtags", value=top_tags, window_start_utc=window_start)
    if signature:
        return EventAnchor(kind="tokens", value=signature, window_start_utc=window_start)
    return None


def compile_regexes(patterns: list[str]) -> list[re.Pattern[str]]:
    return [re.compile(pattern, flags=re.IGNORECASE) for pattern in patterns]


def _connect_sqlite(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def _init_index_schema(conn: sqlite3.Connection) -> bool:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS posts (
          post_uri TEXT PRIMARY KEY,
          post_cid TEXT NOT NULL,
          author_did TEXT NOT NULL,
          author_handle TEXT,
          branch TEXT NOT NULL,
          surface TEXT NOT NULL,
          date_utc TEXT NOT NULL,
          hour_utc TEXT,
          viewer_mode TEXT,
          vantage_id TEXT,
          feed_uri TEXT,
          bucket TEXT,
          record_created_at TEXT,
          indexed_at TEXT NOT NULL,
          text TEXT NOT NULL,
          text_norm TEXT NOT NULL,
          primary_url TEXT,
          primary_domain TEXT,
          urls_json TEXT NOT NULL,
          hashtags_json TEXT NOT NULL,
          mentions_json TEXT NOT NULL,
          token_signature TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_posts_date ON posts(date_utc);
        CREATE INDEX IF NOT EXISTS idx_posts_domain ON posts(primary_domain);
        CREATE INDEX IF NOT EXISTS idx_posts_author ON posts(author_did);
        """
    )
    try:
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS posts_fts
            USING fts5(post_uri UNINDEXED, text, text_norm)
            """
        )
        return True
    except sqlite3.OperationalError:
        return False


def build_post_index(
    *,
    data_root: Path,
    out_db: Path,
    surfaces: list[str],
    start_date: str | None,
    end_date: str | None,
    include_labelerexp: bool,
    overwrite: bool,
) -> dict[str, object]:
    if out_db.exists():
        if not overwrite:
            raise FileExistsError(f"index already exists: {out_db}")
        out_db.unlink()
    out_db.parent.mkdir(parents=True, exist_ok=True)

    conn = _connect_sqlite(out_db)
    try:
        fts_enabled = _init_index_schema(conn)
        partitions: list[RunPartition] = []
        for branch, root in iter_branch_roots(data_root, include_labelerexp):
            partitions.extend(
                iter_partitions(
                    root=root,
                    branch=branch,
                    surfaces=surfaces,
                    start_date=start_date,
                    end_date=end_date,
                )
            )
        partitions.sort(key=lambda p: (p.branch, p.surface, p.date_utc, p.hour_utc or ""))

        posts_seen = 0
        inserted = 0
        by_branch: Counter[str] = Counter()
        by_surface: Counter[str] = Counter()
        by_date: Counter[str] = Counter()

        for partition in partitions:
            for csv_path in sorted(partition.parts_dir.glob("posts_first_seen_part_*.csv")):
                with csv_path.open("r", newline="", encoding="utf-8") as fh:
                    reader = csv.DictReader(fh)
                    rows_to_insert: list[tuple[object, ...]] = []
                    fts_rows: list[tuple[str, str, str]] = []
                    for row in reader:
                        posts_seen += 1
                        text = normalize_text(str(row.get("text", "")))
                        urls = extract_urls(text)
                        primary_url = urls[0] if urls else None
                        primary_domain = safe_url_domain(primary_url)
                        hashtags = extract_hashtags(text)
                        mentions = extract_mentions(text)
                        signature = token_signature(text)
                        rows_to_insert.append(
                            (
                                str(row.get("post_uri", "")),
                                str(row.get("post_cid", "")),
                                str(row.get("author_did", "")),
                                str(row.get("author_handle", "")),
                                partition.branch,
                                partition.surface,
                                partition.date_utc,
                                partition.hour_utc or "",
                                str(row.get("viewer_mode", "")),
                                str(row.get("vantage_id", "")),
                                str(row.get("feed_uri", "")),
                                str(row.get("bucket", "")),
                                str(row.get("record_created_at", "")),
                                str(row.get("indexed_at", "")),
                                text,
                                text.lower(),
                                primary_url,
                                primary_domain,
                                json.dumps(urls, ensure_ascii=False),
                                json.dumps(hashtags, ensure_ascii=False),
                                json.dumps(mentions, ensure_ascii=False),
                                signature,
                            )
                        )
                        if fts_enabled:
                            fts_rows.append((str(row.get("post_uri", "")), text, text.lower()))

                    before = conn.total_changes
                    conn.executemany(
                        """
                        INSERT OR IGNORE INTO posts(
                          post_uri, post_cid, author_did, author_handle, branch, surface,
                          date_utc, hour_utc, viewer_mode, vantage_id, feed_uri, bucket,
                          record_created_at, indexed_at, text, text_norm, primary_url,
                          primary_domain, urls_json, hashtags_json, mentions_json, token_signature
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        rows_to_insert,
                    )
                    inserted_now = conn.total_changes - before
                    inserted += inserted_now
                    if fts_enabled and inserted_now:
                        conn.executemany(
                            "INSERT OR IGNORE INTO posts_fts(post_uri, text, text_norm) VALUES (?, ?, ?)",
                            fts_rows,
                        )
                    by_branch[partition.branch] += inserted_now
                    by_surface[partition.surface] += inserted_now
                    by_date[partition.date_utc] += inserted_now
            conn.commit()

        summary = {
            "out_db": str(out_db),
            "fts_enabled": fts_enabled,
            "partitions_scanned": len(partitions),
            "posts_seen_rows": posts_seen,
            "posts_inserted": inserted,
            "by_branch": dict(by_branch),
            "by_surface": dict(by_surface),
            "by_date": dict(by_date),
        }
        return summary
    finally:
        conn.close()


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def cluster_topic_probe(
    *,
    probe_dir: Path,
    out_dir: Path,
    time_window_hours: int,
    min_cluster_size: int,
    allowed_anchor_kinds: list[str] | None,
    exclude_text_patterns: list[str],
) -> dict[str, object]:
    matched_posts_path = probe_dir / "matched_posts.csv"
    if not matched_posts_path.exists():
        raise FileNotFoundError(f"missing matched_posts.csv: {matched_posts_path}")
    matched_posts = _read_csv_rows(matched_posts_path)
    allowed_kinds = set(allowed_anchor_kinds or [])
    exclude_regexes = compile_regexes(exclude_text_patterns)

    membership_rows: list[dict[str, object]] = []
    cluster_map: dict[str, ClusterAccumulator] = {}
    post_to_cluster: dict[str, str] = {}
    excluded_posts = 0

    for row in matched_posts:
        text = str(row.get("text", ""))
        if any(regex.search(text) for regex in exclude_regexes):
            excluded_posts += 1
            continue
        anchor = build_event_anchor(
            text=text,
            record_created_at=str(row.get("record_created_at", "")),
            indexed_at=str(row.get("indexed_at", "")),
            time_window_hours=time_window_hours,
        )
        if anchor is None:
            continue
        if allowed_kinds and anchor.kind not in allowed_kinds:
            continue
        cluster_id = anchor.cluster_id
        post_uri = str(row.get("post_uri", ""))
        post_to_cluster[post_uri] = cluster_id
        cluster = cluster_map.setdefault(
            cluster_id,
            ClusterAccumulator(
                cluster_id=cluster_id,
                anchor_kind=anchor.kind,
                anchor_value=anchor.value,
                window_start_utc=anchor.window_start_utc,
            ),
        )
        cluster.post_uris.add(post_uri)
        cluster.author_dids.add(str(row.get("author_did", "")))
        for query in str(row.get("matched_queries", "")).split("|"):
            if query:
                cluster.matched_queries[query] += 1
        if len(cluster.example_texts) < 3:
            cluster.example_texts.append(normalize_text(str(row.get("text", "")))[:220])
        membership_rows.append(
            {
                "cluster_id": cluster_id,
                "anchor_kind": anchor.kind,
                "anchor_value": anchor.value,
                "window_start_utc": anchor.window_start_utc,
                "post_uri": post_uri,
                "author_did": str(row.get("author_did", "")),
                "author_handle": str(row.get("author_handle", "")),
                "record_created_at": str(row.get("record_created_at", "")),
                "indexed_at": str(row.get("indexed_at", "")),
                "matched_queries": str(row.get("matched_queries", "")),
                "text": str(row.get("text", "")),
            }
        )

    matched_feed_items_path = probe_dir / "matched_feed_items.csv"
    if matched_feed_items_path.exists():
        with matched_feed_items_path.open("r", newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                post_uri = str(row.get("post_uri", ""))
                cluster_id = post_to_cluster.get(post_uri)
                if cluster_id is None:
                    continue
                cluster = cluster_map[cluster_id]
                cluster.exposure_rows += 1
                viewer_mode = str(row.get("viewer_mode", ""))
                if viewer_mode == "auth":
                    cluster.auth_rows += 1
                elif viewer_mode == "unauth":
                    cluster.unauth_rows += 1
                cluster.unique_feeds.add(str(row.get("feed_uri", "")))
                cluster.bucket_counts[str(row.get("bucket", ""))] += 1

    cluster_rows: list[dict[str, object]] = []
    for cluster in sorted(cluster_map.values(), key=lambda c: (-len(c.post_uris), c.cluster_id)):
        if len(cluster.post_uris) < min_cluster_size:
            continue
        cluster_rows.append(
            {
                "cluster_id": cluster.cluster_id,
                "anchor_kind": cluster.anchor_kind,
                "anchor_value": cluster.anchor_value,
                "window_start_utc": cluster.window_start_utc,
                "post_n": len(cluster.post_uris),
                "author_n": len(cluster.author_dids),
                "exposure_rows": cluster.exposure_rows,
                "auth_rows": cluster.auth_rows,
                "unauth_rows": cluster.unauth_rows,
                "unique_feeds": len(cluster.unique_feeds),
                "matched_queries": "|".join(query for query, _ in cluster.matched_queries.most_common()),
                "bucket_counts_json": json.dumps(cluster.bucket_counts, ensure_ascii=False),
                "example_text_1": cluster.example_texts[0] if len(cluster.example_texts) > 0 else "",
                "example_text_2": cluster.example_texts[1] if len(cluster.example_texts) > 1 else "",
                "example_text_3": cluster.example_texts[2] if len(cluster.example_texts) > 2 else "",
            }
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    _write_rows(
        out_dir / "cluster_membership.csv",
        fieldnames=[
            "cluster_id",
            "anchor_kind",
            "anchor_value",
            "window_start_utc",
            "post_uri",
            "author_did",
            "author_handle",
            "record_created_at",
            "indexed_at",
            "matched_queries",
            "text",
        ],
        rows=membership_rows,
    )
    _write_rows(
        out_dir / "cluster_summary.csv",
        fieldnames=[
            "cluster_id",
            "anchor_kind",
            "anchor_value",
            "window_start_utc",
            "post_n",
            "author_n",
            "exposure_rows",
            "auth_rows",
            "unauth_rows",
            "unique_feeds",
            "matched_queries",
            "bucket_counts_json",
            "example_text_1",
            "example_text_2",
            "example_text_3",
        ],
        rows=cluster_rows,
    )
    summary = {
        "probe_dir": str(probe_dir),
        "time_window_hours": time_window_hours,
        "min_cluster_size": min_cluster_size,
        "allowed_anchor_kinds": sorted(allowed_kinds),
        "exclude_text_patterns": exclude_text_patterns,
        "matched_posts": len(matched_posts),
        "excluded_posts": excluded_posts,
        "clustered_posts": len(post_to_cluster),
        "clusters_total": len(cluster_map),
        "clusters_retained": len(cluster_rows),
        "top_clusters": [
            {
                "cluster_id": row["cluster_id"],
                "anchor_kind": row["anchor_kind"],
                "post_n": row["post_n"],
                "exposure_rows": row["exposure_rows"],
                "unique_feeds": row["unique_feeds"],
            }
            for row in cluster_rows[:25]
        ],
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def _write_rows(path: Path, *, fieldnames: list[str], rows: list[dict[str, object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Archive-wide content-bias utilities.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    build_index = subparsers.add_parser("build-post-index", help="Build a SQLite index from posts_first_seen parts.")
    build_index.add_argument("--data-root", type=Path, default=Path("/Volumes/T9/BlueSky/data_v2_full"))
    build_index.add_argument("--out-db", type=Path, required=True)
    build_index.add_argument("--surface", action="append", default=["hourly", "wide"])
    build_index.add_argument("--start-date", type=str, default=None)
    build_index.add_argument("--end-date", type=str, default=None)
    build_index.add_argument("--include-labelerexp", action="store_true")
    build_index.add_argument("--overwrite", action="store_true")

    cluster_probe = subparsers.add_parser("cluster-topic-probe", help="Cluster topic_probe outputs into event prototypes.")
    cluster_probe.add_argument("--probe-dir", type=Path, required=True)
    cluster_probe.add_argument("--out-dir", type=Path, required=True)
    cluster_probe.add_argument("--time-window-hours", type=int, default=12)
    cluster_probe.add_argument("--min-cluster-size", type=int, default=2)
    cluster_probe.add_argument(
        "--anchor-kind",
        action="append",
        default=[],
        help="Restrict clustering to specific anchor kinds: url, hashtags, or tokens.",
    )
    cluster_probe.add_argument(
        "--exclude-text-pattern",
        action="append",
        default=[],
        help="Case-insensitive regex used to drop known noisy matched_posts rows before clustering.",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "build-post-index":
        summary = build_post_index(
            data_root=args.data_root,
            out_db=args.out_db,
            surfaces=list(dict.fromkeys(args.surface)),
            start_date=args.start_date,
            end_date=args.end_date,
            include_labelerexp=bool(args.include_labelerexp),
            overwrite=bool(args.overwrite),
        )
    else:
        summary = cluster_topic_probe(
            probe_dir=args.probe_dir,
            out_dir=args.out_dir,
            time_window_hours=int(args.time_window_hours),
            min_cluster_size=int(args.min_cluster_size),
            allowed_anchor_kinds=list(args.anchor_kind),
            exclude_text_patterns=list(args.exclude_text_pattern),
        )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
