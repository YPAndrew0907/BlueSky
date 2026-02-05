from __future__ import annotations

import csv
import gzip
import logging
import math
import os
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from bsky_fair_collect.state import StateDB
from bsky_fair_collect.utils import ensure_dir, utc_now_iso

logger = logging.getLogger("bsky_fair_collect.postprocess")


@dataclass(frozen=True)
class PostprocessResult:
    dest_dir: Path
    feeds_flat_path: Path
    impressions_flat_path: Path
    zip_path: Path | None


def run_postprocess(
    out_dir: Path,
    *,
    dest_dir: Path | None = None,
    overwrite: bool = False,
    create_zip: bool = False,
    include_metrics: bool = True,
    include_impression_labels_flat: bool = False,
    allow_running: bool = False,
) -> PostprocessResult:
    """
    Create a small number of convenience "joined" tables for analysis.

    This is intentionally *separate* from the core pipeline outputs in OUT_DIR/csv/ so we don't
    violate the "exact deliverables" contract. By default we write to OUT_DIR/postprocess/.
    """
    state_db_path = out_dir / "state" / "state.db"
    if not state_db_path.exists():
        raise FileNotFoundError(f"missing state db: {state_db_path}")

    resolved_dest = dest_dir if dest_dir is not None else (out_dir / "postprocess")
    ensure_dir(resolved_dest)

    feeds_flat_path = resolved_dest / "feeds_flat.csv"
    impressions_flat_path = resolved_dest / "impressions_flat.csv.gz"
    impression_labels_flat_path = resolved_dest / "impression_labels_flat.csv.gz"

    h1_path = resolved_dest / "h1_discovery_concentration.csv"
    h2_path = resolved_dest / "h2_provider_leverage.csv"
    h3_path = resolved_dest / "h3_feed_exposure_concentration.csv"
    h4_path = resolved_dest / "h4_feed_overlap_summary.csv"
    h5_path = resolved_dest / "h5_exposure_vs_author_size.csv"
    h6_path = resolved_dest / "h6_feed_label_risk.csv"
    h6_labels_path = resolved_dest / "h6_label_value_counts.csv"

    with StateDB(state_db_path) as state:
        _ensure_run_not_active(out_dir, state, allow_running=allow_running)
        _atomic_write(
            feeds_flat_path,
            overwrite=overwrite,
            write_fn=lambda p: _write_csv(p, fieldnames=_FEEDS_FLAT_FIELDS, rows=_iter_feeds_flat_rows(state)),
        )
        _atomic_write(
            impressions_flat_path,
            overwrite=overwrite,
            write_fn=lambda p: _write_csv_gz(
                p, fieldnames=_IMPRESSIONS_FLAT_FIELDS, rows=_iter_impressions_flat_rows(state)
            ),
        )

        if include_impression_labels_flat:
            _atomic_write(
                impression_labels_flat_path,
                overwrite=overwrite,
                write_fn=lambda p: _write_csv_gz(
                    p,
                    fieldnames=_IMPRESSION_LABELS_FLAT_FIELDS,
                    rows=_iter_impression_labels_flat_rows(state),
                ),
            )

        if include_metrics:
            _atomic_write(
                h1_path,
                overwrite=overwrite,
                write_fn=lambda p: _write_csv(
                    p, fieldnames=_H1_FIELDS, rows=_iter_h1_discovery_concentration_rows(state)
                ),
            )
            _atomic_write(
                h2_path,
                overwrite=overwrite,
                write_fn=lambda p: _write_csv(p, fieldnames=_H2_FIELDS, rows=_iter_h2_provider_leverage_rows(state)),
            )
            _atomic_write(
                h3_path,
                overwrite=overwrite,
                write_fn=lambda p: _write_csv(
                    p,
                    fieldnames=_H3_FIELDS,
                    rows=_iter_h3_feed_exposure_concentration_rows(state),
                ),
            )
            _atomic_write(
                h4_path,
                overwrite=overwrite,
                write_fn=lambda p: _write_csv(p, fieldnames=_H4_FIELDS, rows=_iter_h4_feed_overlap_summary_rows(state)),
            )
            _atomic_write(
                h5_path,
                overwrite=overwrite,
                write_fn=lambda p: _write_csv(p, fieldnames=_H5_FIELDS, rows=_iter_h5_exposure_vs_author_size_rows(state)),
            )
            _atomic_write(
                h6_path,
                overwrite=overwrite,
                write_fn=lambda p: _write_csv(p, fieldnames=_H6_FIELDS, rows=_iter_h6_feed_label_risk_rows(state)),
            )
            _atomic_write(
                h6_labels_path,
                overwrite=overwrite,
                write_fn=lambda p: _write_csv(p, fieldnames=_H6_LABEL_FIELDS, rows=_iter_h6_label_value_counts_rows(state)),
            )

    zip_path: Path | None = None
    if create_zip:
        zip_path = resolved_dest / "postprocess.zip"
        to_zip: list[Path] = [feeds_flat_path, impressions_flat_path]
        if include_impression_labels_flat:
            to_zip.append(impression_labels_flat_path)
        if include_metrics:
            to_zip.extend([h1_path, h2_path, h3_path, h4_path, h5_path, h6_path, h6_labels_path])
        # Always refresh the zip when requested so it reflects the current output set.
        _write_zip(zip_path, files=to_zip, overwrite=True)

    return PostprocessResult(
        dest_dir=resolved_dest,
        feeds_flat_path=feeds_flat_path,
        impressions_flat_path=impressions_flat_path,
        zip_path=zip_path,
    )


def _ensure_run_not_active(out_dir: Path, state: StateDB, *, allow_running: bool) -> None:
    if allow_running:
        return

    finished_at = state.get_meta("finished_at_utc")
    if finished_at:
        return

    pid_path = out_dir / "pid.txt"
    try:
        raw = pid_path.read_text("utf-8").strip()
    except FileNotFoundError:
        raw = ""
    if not raw:
        return
    try:
        pid = int(raw)
    except ValueError:
        return

    if _pid_alive(pid):
        raise RuntimeError(
            f"refusing to postprocess while run appears active (pid={pid}). "
            "Wait for the collector to finish, or re-run with --allow-running."
        )


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _atomic_write(path: Path, *, overwrite: bool, write_fn: Callable[[Path], None]) -> None:
    if path.exists() and not overwrite:
        logger.info("postprocess skip exists=%s", path)
        return
    tmp = path.with_name(path.name + ".tmp")
    try:
        write_fn(tmp)
        tmp.replace(path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _write_csv(path: Path, *, fieldnames: list[str], rows: Iterable[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow(row)


def _write_csv_gz(path: Path, *, fieldnames: list[str], rows: Iterable[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow(row)


def _write_zip(path: Path, *, files: list[Path], overwrite: bool) -> None:
    if path.exists() and not overwrite:
        logger.info("postprocess zip skip exists=%s", path)
        return
    tmp = path.with_name(path.name + ".tmp")
    try:
        with zipfile.ZipFile(tmp, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("postprocess_created_at_utc.txt", utc_now_iso() + "\n")
            for p in files:
                zf.write(p, arcname=p.name)
        tmp.replace(path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


_FEEDS_FLAT_FIELDS = [
    "feed_uri",
    "feed_group",
    "selection_reason",
    "provider_bucket",
    "service_did",
    "creator_did",
    "display_name",
    "description",
    "content_mode",
    "accepts_interaction",
    "indexed_at",
    "inclusion_count",
    "slot_count",
    "inclusion_rank",
    "popularity_rank",
    "snapshot_success_unauth",
    "returned_items_unauth",
    "snapshot_success_auth",
    "returned_items_auth",
]


def _iter_feeds_flat_rows(state: StateDB) -> Iterable[dict[str, object]]:
    query = """
    WITH discovery AS (
      SELECT
        feed_uri,
        COUNT(DISTINCT starterpack_uri) AS inclusion_count,
        COUNT(*) AS slot_count
      FROM starterpack_feeds
      GROUP BY feed_uri
    ),
    discovery_ranked AS (
      SELECT
        feed_uri,
        inclusion_count,
        slot_count,
        ROW_NUMBER() OVER (ORDER BY slot_count DESC, feed_uri) AS inclusion_rank
      FROM discovery
    ),
    snapshots AS (
      SELECT
        feed_uri,
        MAX(CASE WHEN viewer_mode = 'unauth' THEN success END) AS snapshot_success_unauth,
        MAX(CASE WHEN viewer_mode = 'unauth' THEN returned_items END) AS returned_items_unauth,
        MAX(CASE WHEN viewer_mode = 'auth' THEN success END) AS snapshot_success_auth,
        MAX(CASE WHEN viewer_mode = 'auth' THEN returned_items END) AS returned_items_auth
      FROM feed_snapshot_status
      GROUP BY feed_uri
    )
    SELECT
      p.feed_uri,
      p.feed_group,
      p.selection_reason,
      COALESCE(p.provider_bucket, g.provider_bucket) AS provider_bucket,
      COALESCE(p.service_did, g.service_did) AS service_did,
      COALESCE(p.creator_did, g.creator_did) AS creator_did,
      COALESCE(p.display_name, g.display_name) AS display_name,
      g.description AS description,
      g.content_mode AS content_mode,
      g.accepts_interaction AS accepts_interaction,
      g.indexed_at AS indexed_at,
      d.inclusion_count AS inclusion_count,
      d.slot_count AS slot_count,
      d.inclusion_rank AS inclusion_rank,
      pop.popularity_rank AS popularity_rank,
      s.snapshot_success_unauth AS snapshot_success_unauth,
      s.returned_items_unauth AS returned_items_unauth,
      s.snapshot_success_auth AS snapshot_success_auth,
      s.returned_items_auth AS returned_items_auth
    FROM feed_panel p
    LEFT JOIN feed_generators g ON g.feed_uri = p.feed_uri
    LEFT JOIN discovery_ranked d ON d.feed_uri = p.feed_uri
    LEFT JOIN popular_feeds pop ON pop.feed_uri = p.feed_uri
    LEFT JOIN snapshots s ON s.feed_uri = p.feed_uri
    ORDER BY p.feed_group, p.feed_uri
    """
    for row in state.conn.execute(query):
        yield dict(row)


_IMPRESSIONS_FLAT_FIELDS = [
    "snapshot_id",
    "feed_uri",
    "feed_group",
    "viewer_mode",
    "collected_at_utc",
    "rank",
    "exposure_weight",
    "post_uri",
    "post_cid",
    "author_did",
    "author_handle",
    "reason_type",
    "reason_actor_did",
    "provider_bucket",
    "service_did",
    "feed_display_name",
    "record_created_at",
    "indexed_at",
    "text_len",
    "is_reply",
    "reply_parent_uri",
    "reply_root_uri",
    "is_quote",
    "quoted_uri",
    "embed_type",
    "image_count",
    "external_uri",
    "external_domain",
    "facet_link_count",
    "link_domains_json",
    "mention_count",
    "hashtag_count",
    "like_count",
    "repost_count",
    "reply_count",
    "quote_count",
    "langs_json",
    "post_labels_json",
    "author_labels_json",
    "author_handle_profile",
    "author_display_name_profile",
    "followers_count",
    "follows_count",
    "posts_count",
]


_IMPRESSION_LABELS_FLAT_FIELDS = [
    "snapshot_id",
    "feed_uri",
    "feed_group",
    "viewer_mode",
    "collected_at_utc",
    "rank",
    "post_uri",
    "post_cid",
    "author_did",
    "author_handle",
    "label_src",
    "label_val",
    "label_neg",
    "label_uri",
]


def _iter_impressions_flat_rows(state: StateDB) -> Iterable[dict[str, object]]:
    query = """
    SELECT
      fi.feed_uri AS feed_uri,
      fi.feed_group AS feed_group,
      fi.viewer_mode AS viewer_mode,
      fi.collected_at_utc AS collected_at_utc,
      fi.rank AS rank,
      fi.post_uri AS post_uri,
      fi.post_cid AS post_cid,
      fi.author_did AS author_did,
      fi.author_handle AS author_handle,
      fi.reason_type AS reason_type,
      fi.reason_actor_did AS reason_actor_did,
      COALESCE(pnl.provider_bucket, gen.provider_bucket) AS provider_bucket,
      COALESCE(pnl.service_did, gen.service_did) AS service_did,
      COALESCE(pnl.display_name, gen.display_name) AS feed_display_name,
      post.record_created_at AS record_created_at,
      post.indexed_at AS indexed_at,
      post.text_len AS text_len,
      post.is_reply AS is_reply,
      post.reply_parent_uri AS reply_parent_uri,
      post.reply_root_uri AS reply_root_uri,
      post.is_quote AS is_quote,
      post.quoted_uri AS quoted_uri,
      COALESCE(post.embed_type, 'none') AS embed_type,
      post.image_count AS image_count,
      post.external_uri AS external_uri,
      post.external_domain AS external_domain,
      post.facet_link_count AS facet_link_count,
      post.link_domains_json AS link_domains_json,
      post.mention_count AS mention_count,
      post.hashtag_count AS hashtag_count,
      post.like_count AS like_count,
      post.repost_count AS repost_count,
      post.reply_count AS reply_count,
      post.quote_count AS quote_count,
      post.langs_json AS langs_json,
      post.post_labels_json AS post_labels_json,
      post.author_labels_json AS author_labels_json,
      a.handle AS author_handle_profile,
      a.display_name AS author_display_name_profile,
      a.followers_count AS followers_count,
      a.follows_count AS follows_count,
      a.posts_count AS posts_count
    FROM feed_items fi
    INNER JOIN feed_panel pnl ON pnl.feed_uri = fi.feed_uri
    LEFT JOIN feed_generators gen ON gen.feed_uri = fi.feed_uri
    LEFT JOIN posts post ON post.post_uri = fi.post_uri AND post.post_cid = fi.post_cid
    LEFT JOIN authors a ON a.author_did = fi.author_did
    ORDER BY fi.feed_uri, fi.viewer_mode, fi.rank
    """
    for row in state.conn.execute(query):
        rank_raw = row["rank"]
        try:
            rank = int(rank_raw) if rank_raw is not None else 0
        except ValueError:
            rank = 0

        collected_at = str(row["collected_at_utc"] or "")
        feed_uri = str(row["feed_uri"] or "")
        viewer_mode = str(row["viewer_mode"] or "")
        snapshot_id = f"{feed_uri}|{viewer_mode}|{collected_at}"

        exposure_weight = ""
        if rank > 0:
            exposure_weight = f"{(1.0 / float(rank)):.8f}"

        out = dict(row)
        out["snapshot_id"] = snapshot_id
        out["exposure_weight"] = exposure_weight
        yield out


def _iter_impression_labels_flat_rows(state: StateDB) -> Iterable[dict[str, object]]:
    """
    Long "label facts" table joined to rank (one row per label assignment per impression).
    """
    query = """
    SELECT
      fi.feed_uri AS feed_uri,
      fi.feed_group AS feed_group,
      fi.viewer_mode AS viewer_mode,
      fi.collected_at_utc AS collected_at_utc,
      fi.rank AS rank,
      fi.post_uri AS post_uri,
      fi.post_cid AS post_cid,
      fi.author_did AS author_did,
      fi.author_handle AS author_handle,
      l.label_src AS label_src,
      l.label_val AS label_val,
      l.label_neg AS label_neg,
      l.label_uri AS label_uri
    FROM post_labels l
    INNER JOIN feed_items fi
      ON fi.feed_uri = l.feed_uri
     AND fi.viewer_mode = l.viewer_mode
     AND fi.collected_at_utc = l.collected_at_utc
     AND fi.post_uri = l.post_uri
     AND fi.post_cid = l.post_cid
    ORDER BY fi.feed_uri, fi.viewer_mode, fi.rank, l.label_src, l.label_val
    """
    for row in state.conn.execute(query):
        collected_at = str(row["collected_at_utc"] or "")
        feed_uri = str(row["feed_uri"] or "")
        viewer_mode = str(row["viewer_mode"] or "")
        snapshot_id = f"{feed_uri}|{viewer_mode}|{collected_at}"
        out = dict(row)
        out["snapshot_id"] = snapshot_id
        yield out


def _gini(values: list[float]) -> float:
    """
    Gini coefficient for non-negative values.

    Returns 0.0 for empty/all-zero inputs.
    """
    if not values:
        return 0.0
    vals = [v for v in values if v > 0.0]
    if not vals:
        return 0.0
    vals.sort()
    n = len(vals)
    total = sum(vals)
    if total <= 0.0 or n <= 1:
        return 0.0
    # (2 * sum(i * x_i) / (n * sum x)) - (n + 1) / n
    weighted = 0.0
    for i, x in enumerate(vals, start=1):
        weighted += float(i) * x
    return (2.0 * weighted) / (float(n) * total) - (float(n) + 1.0) / float(n)


def _hhi_shares(counts: list[int]) -> float:
    total = sum(counts)
    if total <= 0:
        return 0.0
    return sum((c / total) ** 2 for c in counts if c > 0)


def _quantile_threshold(values: list[int], q: float) -> int:
    if not values:
        return 0
    if q <= 0.0:
        return min(values)
    if q >= 1.0:
        return max(values)
    values_sorted = sorted(values)
    n = len(values_sorted)
    # Nearest-rank definition.
    idx = max(0, min(n - 1, int(math.ceil(q * n) - 1)))
    return int(values_sorted[idx])


_H1_FIELDS = [
    "level",  # feed|provider|summary
    "feed_uri",
    "provider_bucket",
    "slot_count",
    "share",
    "metric_name",
    "metric_value",
    "population",
]


def _iter_h1_discovery_concentration_rows(state: StateDB) -> Iterable[dict[str, object]]:
    total_slots_row = state.conn.execute("SELECT COUNT(*) AS n FROM starterpack_feeds").fetchone()
    total_slots = int(total_slots_row["n"]) if total_slots_row is not None else 0

    feed_counts: list[int] = []
    provider_counts: list[int] = []

    # Feed-level (with provider_bucket).
    for row in state.conn.execute(
        """
        SELECT
          sf.feed_uri AS feed_uri,
          COALESCE(g.provider_bucket, 'unknown') AS provider_bucket,
          COUNT(*) AS slot_count
        FROM starterpack_feeds sf
        LEFT JOIN feed_generators g ON g.feed_uri = sf.feed_uri
        GROUP BY sf.feed_uri, provider_bucket
        ORDER BY slot_count DESC, sf.feed_uri
        """
    ):
        slot_count = int(row["slot_count"])
        feed_counts.append(slot_count)
        share = (slot_count / total_slots) if total_slots > 0 else 0.0
        yield {
            "level": "feed",
            "feed_uri": row["feed_uri"],
            "provider_bucket": row["provider_bucket"],
            "slot_count": slot_count,
            "share": f"{share:.8f}",
            "metric_name": "",
            "metric_value": "",
            "population": "",
        }

    # Provider-level.
    for row in state.conn.execute(
        """
        SELECT
          COALESCE(g.provider_bucket, 'unknown') AS provider_bucket,
          COUNT(*) AS slot_count
        FROM starterpack_feeds sf
        LEFT JOIN feed_generators g ON g.feed_uri = sf.feed_uri
        GROUP BY provider_bucket
        ORDER BY slot_count DESC, provider_bucket
        """
    ):
        slot_count = int(row["slot_count"])
        provider_counts.append(slot_count)
        share = (slot_count / total_slots) if total_slots > 0 else 0.0
        yield {
            "level": "provider",
            "feed_uri": "",
            "provider_bucket": row["provider_bucket"],
            "slot_count": slot_count,
            "share": f"{share:.8f}",
            "metric_name": "",
            "metric_value": "",
            "population": "",
        }

    # Summary inequality metrics.
    yield {
        "level": "summary",
        "feed_uri": "",
        "provider_bucket": "",
        "slot_count": "",
        "share": "",
        "metric_name": "hhi_feed",
        "metric_value": f"{_hhi_shares(feed_counts):.8f}",
        "population": "feed",
    }
    yield {
        "level": "summary",
        "feed_uri": "",
        "provider_bucket": "",
        "slot_count": "",
        "share": "",
        "metric_name": "gini_feed",
        "metric_value": f"{_gini([float(c) for c in feed_counts]):.8f}",
        "population": "feed",
    }
    yield {
        "level": "summary",
        "feed_uri": "",
        "provider_bucket": "",
        "slot_count": "",
        "share": "",
        "metric_name": "hhi_provider",
        "metric_value": f"{_hhi_shares(provider_counts):.8f}",
        "population": "provider",
    }
    yield {
        "level": "summary",
        "feed_uri": "",
        "provider_bucket": "",
        "slot_count": "",
        "share": "",
        "metric_name": "gini_provider",
        "metric_value": f"{_gini([float(c) for c in provider_counts]):.8f}",
        "population": "provider",
    }


_H2_FIELDS = [
    "provider_bucket",
    "hosted_feed_count_api",
    "discovery_slot_count",
    "hosting_share",
    "discovery_share",
    "leverage_ratio",
]


def _iter_h2_provider_leverage_rows(state: StateDB) -> Iterable[dict[str, object]]:
    total_feeds_row = state.conn.execute("SELECT COUNT(*) AS n FROM feed_generators").fetchone()
    total_feeds = int(total_feeds_row["n"]) if total_feeds_row is not None else 0
    total_slots_row = state.conn.execute("SELECT COUNT(*) AS n FROM starterpack_feeds").fetchone()
    total_slots = int(total_slots_row["n"]) if total_slots_row is not None else 0

    hosted = {
        str(r["provider_bucket"]): int(r["n"])
        for r in state.conn.execute("SELECT provider_bucket, COUNT(*) AS n FROM feed_generators GROUP BY provider_bucket")
    }
    discovery = {
        str(r["provider_bucket"]): int(r["n"])
        for r in state.conn.execute(
            """
            SELECT COALESCE(g.provider_bucket, 'unknown') AS provider_bucket, COUNT(*) AS n
            FROM starterpack_feeds sf
            LEFT JOIN feed_generators g ON g.feed_uri = sf.feed_uri
            GROUP BY provider_bucket
            """
        )
    }

    all_buckets = sorted(set(hosted) | set(discovery))
    for bucket in all_buckets:
        hosted_n = int(hosted.get(bucket, 0))
        slots_n = int(discovery.get(bucket, 0))
        hosting_share = (hosted_n / total_feeds) if total_feeds > 0 else 0.0
        discovery_share = (slots_n / total_slots) if total_slots > 0 else 0.0
        leverage_ratio = ""
        if hosting_share > 0:
            leverage_ratio = f"{(discovery_share / hosting_share):.6f}"
        yield {
            "provider_bucket": bucket,
            "hosted_feed_count_api": hosted_n,
            "discovery_slot_count": slots_n,
            "hosting_share": f"{hosting_share:.8f}",
            "discovery_share": f"{discovery_share:.8f}",
            "leverage_ratio": leverage_ratio,
        }


_H3_FIELDS = [
    "snapshot_id",
    "feed_uri",
    "feed_group",
    "viewer_mode",
    "collected_at_utc",
    "provider_bucket",
    "num_items",
    "num_unique_authors",
    "total_exposure_weight",
    "gini_exposure",
    "hhi_exposure",
    "top10_share",
]


def _iter_h3_feed_exposure_concentration_rows(state: StateDB) -> Iterable[dict[str, object]]:
    query = """
    SELECT
      fi.feed_uri AS feed_uri,
      fi.feed_group AS feed_group,
      fi.viewer_mode AS viewer_mode,
      fi.collected_at_utc AS collected_at_utc,
      COALESCE(p.provider_bucket, 'unknown') AS provider_bucket,
      fi.author_did AS author_did,
      COUNT(*) AS n_items_author,
      SUM(1.0 / fi.rank) AS exposure_w
    FROM feed_items fi
    LEFT JOIN feed_panel p ON p.feed_uri = fi.feed_uri
    GROUP BY fi.feed_uri, fi.feed_group, fi.viewer_mode, fi.collected_at_utc, provider_bucket, fi.author_did
    ORDER BY fi.feed_uri, fi.viewer_mode, fi.collected_at_utc
    """

    cur_snapshot: tuple[str, str, str] | None = None  # (feed_uri, viewer_mode, collected_at_utc)
    cur_feed_group = ""
    cur_provider = ""
    weights: list[float] = []
    total_items = 0

    def _flush() -> dict[str, object] | None:
        nonlocal weights, total_items, cur_snapshot, cur_feed_group, cur_provider
        if cur_snapshot is None:
            return None
        feed_uri, viewer_mode, collected_at = cur_snapshot
        snapshot_id = f"{feed_uri}|{viewer_mode}|{collected_at}"
        total_w = sum(weights)
        n_authors = len(weights)
        if total_w <= 0.0 or total_items <= 0 or n_authors <= 0:
            row = {
                "snapshot_id": snapshot_id,
                "feed_uri": feed_uri,
                "feed_group": cur_feed_group,
                "viewer_mode": viewer_mode,
                "collected_at_utc": collected_at,
                "provider_bucket": cur_provider,
                "num_items": total_items,
                "num_unique_authors": n_authors,
                "total_exposure_weight": f"{total_w:.8f}",
                "gini_exposure": "",
                "hhi_exposure": "",
                "top10_share": "",
            }
        else:
            shares = [(w / total_w) for w in weights]
            hhi = sum(s * s for s in shares)
            topk = sorted(weights, reverse=True)[: min(10, len(weights))]
            top10_share = (sum(topk) / total_w) if total_w > 0.0 else 0.0
            row = {
                "snapshot_id": snapshot_id,
                "feed_uri": feed_uri,
                "feed_group": cur_feed_group,
                "viewer_mode": viewer_mode,
                "collected_at_utc": collected_at,
                "provider_bucket": cur_provider,
                "num_items": total_items,
                "num_unique_authors": n_authors,
                "total_exposure_weight": f"{total_w:.8f}",
                "gini_exposure": f"{_gini(weights):.8f}",
                "hhi_exposure": f"{hhi:.8f}",
                "top10_share": f"{top10_share:.8f}",
            }
        weights = []
        total_items = 0
        cur_snapshot = None
        cur_feed_group = ""
        cur_provider = ""
        return row

    for row in state.conn.execute(query):
        feed_uri = str(row["feed_uri"])
        viewer_mode = str(row["viewer_mode"])
        collected_at = str(row["collected_at_utc"])
        key = (feed_uri, viewer_mode, collected_at)
        if cur_snapshot is None:
            cur_snapshot = key
            cur_feed_group = str(row["feed_group"])
            cur_provider = str(row["provider_bucket"])
        elif key != cur_snapshot:
            flushed = _flush()
            if flushed is not None:
                yield flushed
            cur_snapshot = key
            cur_feed_group = str(row["feed_group"])
            cur_provider = str(row["provider_bucket"])

        total_items += int(row["n_items_author"])
        weights.append(float(row["exposure_w"] or 0.0))

    flushed = _flush()
    if flushed is not None:
        yield flushed


_H4_FIELDS = [
    "feed_group",
    "viewer_mode",
    "top_k",
    "num_feeds",
    "pairs_sampled",
    "avg_jaccard",
    "median_jaccard",
    "p90_jaccard",
    "global_winner_authors_for_50pct_exposure",
]


def _iter_h4_feed_overlap_summary_rows(state: StateDB) -> Iterable[dict[str, object]]:
    run_id = state.get_meta("run_id") or ""
    try:
        seed = int(run_id[:8], 16)
    except ValueError:
        seed = 0

    # Build per-snapshot top-K author sets using author exposure weights.
    top_k = 20
    snap_to_group: dict[str, str] = {}
    snap_to_mode: dict[str, str] = {}
    snap_to_top: dict[str, set[str]] = {}

    # Re-use the same grouped-by-author query from H3.
    query = """
    SELECT
      fi.feed_uri AS feed_uri,
      fi.feed_group AS feed_group,
      fi.viewer_mode AS viewer_mode,
      fi.collected_at_utc AS collected_at_utc,
      fi.author_did AS author_did,
      SUM(1.0 / fi.rank) AS exposure_w
    FROM feed_items fi
    GROUP BY fi.feed_uri, fi.feed_group, fi.viewer_mode, fi.collected_at_utc, fi.author_did
    ORDER BY fi.feed_uri, fi.viewer_mode, fi.collected_at_utc
    """

    cur_snapshot: tuple[str, str, str] | None = None
    cur_group = ""
    cur_mode = ""
    author_weights: list[tuple[str, float]] = []

    def _flush_top() -> None:
        nonlocal cur_snapshot, cur_group, cur_mode, author_weights
        if cur_snapshot is None:
            return
        feed_uri, viewer_mode, collected_at = cur_snapshot
        snapshot_id = f"{feed_uri}|{viewer_mode}|{collected_at}"
        snap_to_group[snapshot_id] = cur_group
        snap_to_mode[snapshot_id] = cur_mode
        author_weights.sort(key=lambda t: (-t[1], t[0]))
        top_authors = {a for a, _ in author_weights[: min(top_k, len(author_weights))]}
        snap_to_top[snapshot_id] = top_authors
        cur_snapshot = None
        cur_group = ""
        cur_mode = ""
        author_weights = []

    for row in state.conn.execute(query):
        feed_uri = str(row["feed_uri"])
        viewer_mode = str(row["viewer_mode"])
        collected_at = str(row["collected_at_utc"])
        key = (feed_uri, viewer_mode, collected_at)
        if cur_snapshot is None:
            cur_snapshot = key
            cur_group = str(row["feed_group"])
            cur_mode = viewer_mode
        elif key != cur_snapshot:
            _flush_top()
            cur_snapshot = key
            cur_group = str(row["feed_group"])
            cur_mode = viewer_mode

        author_weights.append((str(row["author_did"]), float(row["exposure_w"] or 0.0)))

    _flush_top()

    # Global winner set size (50% exposure) per (feed_group, viewer_mode).
    global_winners: dict[tuple[str, str], int] = {}
    for row in state.conn.execute(
        """
        SELECT feed_group, viewer_mode, author_did, SUM(1.0 / rank) AS w
        FROM feed_items
        GROUP BY feed_group, viewer_mode, author_did
        """
    ):
        key = (str(row["feed_group"]), str(row["viewer_mode"]))
        global_winners.setdefault(key, 0)  # placeholder to create key
    # Build weights per group/mode.
    weights_by_group: dict[tuple[str, str], list[float]] = {}
    for row in state.conn.execute(
        """
        SELECT feed_group, viewer_mode, author_did, SUM(1.0 / rank) AS w
        FROM feed_items
        GROUP BY feed_group, viewer_mode, author_did
        """
    ):
        key = (str(row["feed_group"]), str(row["viewer_mode"]))
        weights_by_group.setdefault(key, []).append(float(row["w"] or 0.0))
    for key, ws in weights_by_group.items():
        total = sum(ws)
        if total <= 0.0:
            global_winners[key] = 0
            continue
        ws_sorted = sorted(ws, reverse=True)
        target = 0.5 * total
        acc = 0.0
        n = 0
        for w in ws_sorted:
            acc += w
            n += 1
            if acc >= target:
                break
        global_winners[key] = n

    # Jaccard overlap sampling within each feed_group+viewer_mode.
    by_group_mode: dict[tuple[str, str], list[str]] = {}
    for snapshot_id, group in snap_to_group.items():
        mode = snap_to_mode.get(snapshot_id, "")
        by_group_mode.setdefault((group, mode), []).append(snapshot_id)

    import random

    rng = random.Random(seed)
    sample_pairs = 2000
    for (group, mode), snaps in sorted(by_group_mode.items()):
        n_feeds = len(snaps)
        if n_feeds < 2:
            yield {
                "feed_group": group,
                "viewer_mode": mode,
                "top_k": top_k,
                "num_feeds": n_feeds,
                "pairs_sampled": 0,
                "avg_jaccard": "",
                "median_jaccard": "",
                "p90_jaccard": "",
                "global_winner_authors_for_50pct_exposure": global_winners.get((group, mode), 0),
            }
            continue

        pairs: list[float] = []
        for _ in range(min(sample_pairs, n_feeds * (n_feeds - 1) // 2)):
            a, b = rng.sample(snaps, 2)
            sa = snap_to_top.get(a, set())
            sb = snap_to_top.get(b, set())
            if not sa and not sb:
                pairs.append(0.0)
                continue
            inter = len(sa & sb)
            union = len(sa | sb)
            pairs.append((inter / union) if union > 0 else 0.0)

        pairs.sort()
        avg = sum(pairs) / len(pairs) if pairs else 0.0
        median = pairs[len(pairs) // 2] if pairs else 0.0
        p90 = pairs[int(0.9 * (len(pairs) - 1))] if pairs else 0.0
        yield {
            "feed_group": group,
            "viewer_mode": mode,
            "top_k": top_k,
            "num_feeds": n_feeds,
            "pairs_sampled": len(pairs),
            "avg_jaccard": f"{avg:.8f}",
            "median_jaccard": f"{median:.8f}",
            "p90_jaccard": f"{p90:.8f}",
            "global_winner_authors_for_50pct_exposure": global_winners.get((group, mode), 0),
        }


_H5_FIELDS = [
    "feed_group",
    "follower_bucket",
    "follower_p90",
    "follower_p99",
    "n_authors",
    "exposure_weight_sum",
    "exposure_share",
]


def _iter_h5_exposure_vs_author_size_rows(state: StateDB) -> Iterable[dict[str, object]]:
    # Compute follower thresholds from hydrated authors.
    follower_counts: list[int] = []
    author_followers: dict[str, int | None] = {}
    for row in state.conn.execute("SELECT author_did, followers_count FROM authors"):
        did = str(row["author_did"])
        fc = row["followers_count"]
        fc_i = int(fc) if isinstance(fc, int) else None
        author_followers[did] = fc_i
        if fc_i is not None:
            follower_counts.append(fc_i)

    p90 = _quantile_threshold(follower_counts, 0.90)
    p99 = _quantile_threshold(follower_counts, 0.99)

    # Exposure by (feed_group, author).
    exposure_by_group_bucket: dict[tuple[str, str], float] = {}
    authors_by_group_bucket: dict[tuple[str, str], set[str]] = {}
    total_by_group: dict[str, float] = {}
    for row in state.conn.execute(
        """
        SELECT feed_group, author_did, SUM(1.0 / rank) AS w
        FROM feed_items
        GROUP BY feed_group, author_did
        """
    ):
        group = str(row["feed_group"])
        did = str(row["author_did"])
        w = float(row["w"] or 0.0)
        total_by_group[group] = total_by_group.get(group, 0.0) + w

        fc = author_followers.get(did)
        if fc is None:
            bucket = "missing"
        elif fc >= p99:
            bucket = "top1"
        elif fc >= p90:
            bucket = "top10"
        else:
            bucket = "bottom90"

        key = (group, bucket)
        exposure_by_group_bucket[key] = exposure_by_group_bucket.get(key, 0.0) + w
        authors_by_group_bucket.setdefault(key, set()).add(did)

    for group in sorted(total_by_group.keys()):
        total = total_by_group.get(group, 0.0)
        for bucket in ["top1", "top10", "bottom90", "missing"]:
            key = (group, bucket)
            w = exposure_by_group_bucket.get(key, 0.0)
            share = (w / total) if total > 0.0 else 0.0
            yield {
                "feed_group": group,
                "follower_bucket": bucket,
                "follower_p90": p90,
                "follower_p99": p99,
                "n_authors": len(authors_by_group_bucket.get(key, set())),
                "exposure_weight_sum": f"{w:.8f}",
                "exposure_share": f"{share:.8f}",
            }


_H6_FIELDS = [
    "snapshot_id",
    "feed_uri",
    "feed_group",
    "viewer_mode",
    "collected_at_utc",
    "provider_bucket",
    "total_items",
    "labeled_items_any",
    "label_rate_any",
    "label_rate_top10",
    "label_rate_top50",
]


def _iter_h6_feed_label_risk_rows(state: StateDB) -> Iterable[dict[str, object]]:
    query = """
    WITH label_posts AS (
      SELECT DISTINCT feed_uri, viewer_mode, collected_at_utc, post_uri, post_cid
      FROM post_labels
    ),
    joined AS (
      SELECT
        fi.feed_uri AS feed_uri,
        fi.feed_group AS feed_group,
        fi.viewer_mode AS viewer_mode,
        fi.collected_at_utc AS collected_at_utc,
        COALESCE(p.provider_bucket, 'unknown') AS provider_bucket,
        fi.rank AS rank,
        CASE WHEN lp.post_uri IS NOT NULL THEN 1 ELSE 0 END AS has_label
      FROM feed_items fi
      LEFT JOIN label_posts lp
        ON lp.feed_uri = fi.feed_uri
       AND lp.viewer_mode = fi.viewer_mode
       AND lp.collected_at_utc = fi.collected_at_utc
       AND lp.post_uri = fi.post_uri
       AND lp.post_cid = fi.post_cid
      LEFT JOIN feed_panel p ON p.feed_uri = fi.feed_uri
    )
    SELECT
      feed_uri,
      feed_group,
      viewer_mode,
      collected_at_utc,
      provider_bucket,
      COUNT(*) AS total_items,
      SUM(has_label) AS labeled_items_any,
      SUM(CASE WHEN rank <= 10 THEN has_label ELSE 0 END) AS labeled_top10,
      SUM(CASE WHEN rank <= 10 THEN 1 ELSE 0 END) AS denom_top10,
      SUM(CASE WHEN rank <= 50 THEN has_label ELSE 0 END) AS labeled_top50,
      SUM(CASE WHEN rank <= 50 THEN 1 ELSE 0 END) AS denom_top50
    FROM joined
    GROUP BY feed_uri, feed_group, viewer_mode, collected_at_utc, provider_bucket
    ORDER BY feed_group, feed_uri, viewer_mode
    """
    for row in state.conn.execute(query):
        feed_uri = str(row["feed_uri"])
        viewer_mode = str(row["viewer_mode"])
        collected_at = str(row["collected_at_utc"])
        snapshot_id = f"{feed_uri}|{viewer_mode}|{collected_at}"

        total_items = int(row["total_items"] or 0)
        labeled_any = int(row["labeled_items_any"] or 0)
        denom_top10 = int(row["denom_top10"] or 0)
        denom_top50 = int(row["denom_top50"] or 0)

        label_rate_any = (labeled_any / total_items) if total_items > 0 else 0.0
        label_rate_top10 = (int(row["labeled_top10"] or 0) / denom_top10) if denom_top10 > 0 else 0.0
        label_rate_top50 = (int(row["labeled_top50"] or 0) / denom_top50) if denom_top50 > 0 else 0.0

        yield {
            "snapshot_id": snapshot_id,
            "feed_uri": feed_uri,
            "feed_group": str(row["feed_group"]),
            "viewer_mode": viewer_mode,
            "collected_at_utc": collected_at,
            "provider_bucket": str(row["provider_bucket"]),
            "total_items": total_items,
            "labeled_items_any": labeled_any,
            "label_rate_any": f"{label_rate_any:.8f}",
            "label_rate_top10": f"{label_rate_top10:.8f}",
            "label_rate_top50": f"{label_rate_top50:.8f}",
        }


_H6_LABEL_FIELDS = [
    "feed_group",
    "label_val",
    "label_count",
    "label_share_within_group",
]


def _iter_h6_label_value_counts_rows(state: StateDB) -> Iterable[dict[str, object]]:
    # Count label assignments by feed_group.
    totals: dict[str, int] = {}
    for row in state.conn.execute(
        """
        SELECT COALESCE(p.feed_group, 'unknown') AS feed_group, COUNT(*) AS n
        FROM post_labels l
        LEFT JOIN feed_panel p ON p.feed_uri = l.feed_uri
        GROUP BY feed_group
        """
    ):
        totals[str(row["feed_group"])] = int(row["n"])

    for row in state.conn.execute(
        """
        SELECT
          COALESCE(p.feed_group, 'unknown') AS feed_group,
          l.label_val AS label_val,
          COUNT(*) AS n
        FROM post_labels l
        LEFT JOIN feed_panel p ON p.feed_uri = l.feed_uri
        GROUP BY feed_group, l.label_val
        ORDER BY feed_group, n DESC, l.label_val
        """
    ):
        group = str(row["feed_group"])
        n = int(row["n"])
        total = totals.get(group, 0)
        share = (n / total) if total > 0 else 0.0
        yield {
            "feed_group": group,
            "label_val": str(row["label_val"]),
            "label_count": n,
            "label_share_within_group": f"{share:.8f}",
        }
