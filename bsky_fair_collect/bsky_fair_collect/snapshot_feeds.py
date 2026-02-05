from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from bsky_fair_collect.config import AppConfig, AuthMode, ViewerMode
from bsky_fair_collect.errors import record_error, truncate_message
from bsky_fair_collect.http_client import HttpClient, HttpError
from bsky_fair_collect.parse_utils import domain_from_url, json_dumps_compact
from bsky_fair_collect.session import SessionManager
from bsky_fair_collect.state import StateDB
from bsky_fair_collect.utils import utc_now_iso

logger = logging.getLogger("bsky_fair_collect.stage.snapshot_feeds")


@dataclass(frozen=True)
class FeedPanelEntry:
    feed_uri: str
    feed_group: str


@dataclass(frozen=True)
class SnapshotResult:
    collected_at_utc: str
    requested_items: int
    returned_items: int
    pages_fetched: int
    items: list["FeedItemRow"]
    posts: list["PostRow"]
    labels: list["PostLabelRow"]


@dataclass(frozen=True)
class FeedItemRow:
    feed_uri: str
    feed_group: str
    viewer_mode: str
    collected_at_utc: str
    rank: int
    post_uri: str
    post_cid: str
    author_did: str
    author_handle: str | None
    reason_type: str | None
    reason_actor_did: str | None


@dataclass(frozen=True)
class PostRow:
    post_uri: str
    post_cid: str
    author_did: str
    author_handle: str | None
    record_created_at: str | None
    indexed_at: str | None
    text: str | None
    text_len: int
    is_reply: int
    reply_parent_uri: str | None
    reply_root_uri: str | None
    is_quote: int
    quoted_uri: str | None
    embed_type: str | None
    image_count: int
    external_uri: str | None
    external_domain: str | None
    facet_link_count: int
    link_domains_json: str
    mention_count: int
    hashtag_count: int
    like_count: int | None
    repost_count: int | None
    reply_count: int | None
    quote_count: int | None
    langs_json: str
    post_labels_json: str
    author_labels_json: str


@dataclass(frozen=True)
class PostLabelRow:
    post_uri: str
    post_cid: str
    feed_uri: str
    viewer_mode: str
    collected_at_utc: str
    label_src: str
    label_val: str
    label_neg: int | None
    label_uri: str


def stage_snapshot_feeds(
    cfg: AppConfig,
    state: StateDB,
    http: HttpClient,
    session: SessionManager | None,
    *,
    retry_failures: bool = True,
) -> None:
    logger.info(
        "stage=start name=snapshot_feeds posts_per_feed=%s auth_mode=%s",
        cfg.run.posts_per_feed,
        cfg.auth_mode.value,
    )
    state.set_meta("snapshots_started", "1")

    panel = _load_panel(state)
    if not panel:
        logger.warning("snapshot_feeds: feed_panel is empty; nothing to do")
        return

    viewer_modes = _viewer_modes(cfg, session)
    total_targets = len(panel) * len(viewer_modes)
    logger.info("snapshot_feeds targets feeds=%s viewer_modes=%s total=%s", len(panel), [vm.value for vm in viewer_modes], total_targets)

    completed = 0
    for entry in panel:
        for vm in viewer_modes:
            if _snapshot_already_success(state, entry.feed_uri, vm.value):
                completed += 1
                continue
            if not retry_failures and _snapshot_exists(state, entry.feed_uri, vm.value):
                completed += 1
                continue
            _snapshot_one(cfg, state, http, session, entry, vm)
            completed += 1
            if completed % 100 == 0:
                logger.info(
                    "snapshot_feeds progress completed=%s/%s success=%s",
                    completed,
                    total_targets,
                    _count_success(state),
                )

    logger.info(
        "stage=done name=snapshot_feeds success=%s attempted=%s",
        _count_success(state),
        total_targets,
    )


def _viewer_modes(cfg: AppConfig, session: SessionManager | None) -> list[ViewerMode]:
    if cfg.auth_mode == AuthMode.UNAUTH:
        return [ViewerMode.UNAUTH]
    if cfg.auth_mode == AuthMode.AUTH:
        if session is None:
            raise RuntimeError("auth_mode=auth but no session is available")
        session.get_access_jwt()
        return [ViewerMode.AUTH]
    if cfg.auth_mode == AuthMode.BOTH:
        if session is None:
            raise RuntimeError("auth_mode=both but no session is available")
        session.get_access_jwt()
        return [ViewerMode.UNAUTH, ViewerMode.AUTH]
    raise RuntimeError(f"unhandled auth_mode: {cfg.auth_mode}")


def _load_panel(state: StateDB) -> list[FeedPanelEntry]:
    out: list[FeedPanelEntry] = []
    for row in state.conn.execute(
        "SELECT feed_uri, feed_group FROM feed_panel ORDER BY feed_group, feed_uri"
    ):
        out.append(FeedPanelEntry(feed_uri=str(row["feed_uri"]), feed_group=str(row["feed_group"])))
    return out


def _snapshot_already_success(state: StateDB, feed_uri: str, viewer_mode: str) -> bool:
    row = state.conn.execute(
        "SELECT success FROM feed_snapshot_status WHERE feed_uri = ? AND viewer_mode = ?",
        (feed_uri, viewer_mode),
    ).fetchone()
    return row is not None and int(row["success"]) == 1


def _snapshot_exists(state: StateDB, feed_uri: str, viewer_mode: str) -> bool:
    row = state.conn.execute(
        "SELECT 1 FROM feed_snapshot_status WHERE feed_uri = ? AND viewer_mode = ?",
        (feed_uri, viewer_mode),
    ).fetchone()
    return row is not None


def _count_success(state: StateDB) -> int:
    row = state.conn.execute("SELECT COUNT(*) AS n FROM feed_snapshot_status WHERE success = 1").fetchone()
    return int(row["n"]) if row is not None else 0


def _snapshot_one(
    cfg: AppConfig,
    state: StateDB,
    http: HttpClient,
    session: SessionManager | None,
    entry: FeedPanelEntry,
    viewer_mode: ViewerMode,
) -> None:
    collected_at = utc_now_iso()
    use_session = viewer_mode == ViewerMode.AUTH

    try:
        snap = _fetch_feed_snapshot(
            cfg,
            http,
            feed_uri=entry.feed_uri,
            feed_group=entry.feed_group,
            viewer_mode=viewer_mode.value,
            session=session if use_session else None,
            collected_at_utc=collected_at,
        )
    except HttpError as err:
        record_error(
            state,
            stage="snapshot.getFeed",
            key=f"{entry.feed_uri}|{viewer_mode.value}",
            error_type="http_error",
            http_status=err.status_code,
            error_message=str(err),
        )
        _upsert_snapshot_status(
            state,
            feed_uri=entry.feed_uri,
            feed_group=entry.feed_group,
            viewer_mode=viewer_mode.value,
            collected_at_utc=collected_at,
            requested_items=cfg.run.posts_per_feed,
            returned_items=0,
            pages_fetched=0,
            success=0,
            http_status=err.status_code,
            error_type="http_error",
            error_message_short=truncate_message(str(err), max_len=200),
        )
        state.conn.commit()
        return
    except Exception as err:  # noqa: BLE001
        record_error(
            state,
            stage="snapshot.getFeed",
            key=f"{entry.feed_uri}|{viewer_mode.value}",
            error_type="exception",
            error_message=repr(err),
        )
        _upsert_snapshot_status(
            state,
            feed_uri=entry.feed_uri,
            feed_group=entry.feed_group,
            viewer_mode=viewer_mode.value,
            collected_at_utc=collected_at,
            requested_items=cfg.run.posts_per_feed,
            returned_items=0,
            pages_fetched=0,
            success=0,
            http_status=None,
            error_type="exception",
            error_message_short=truncate_message(repr(err), max_len=200),
        )
        state.conn.commit()
        return

    # Commit snapshot results atomically (within the current sqlite transaction).
    try:
        state.conn.execute(
            "DELETE FROM feed_items WHERE feed_uri = ? AND viewer_mode = ?",
            (entry.feed_uri, viewer_mode.value),
        )
        state.conn.execute(
            "DELETE FROM post_labels WHERE feed_uri = ? AND viewer_mode = ?",
            (entry.feed_uri, viewer_mode.value),
        )

        state.conn.executemany(
            """
            INSERT INTO feed_items(
              feed_uri, feed_group, viewer_mode, collected_at_utc, rank, post_uri, post_cid, author_did,
              author_handle, reason_type, reason_actor_did
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    r.feed_uri,
                    r.feed_group,
                    r.viewer_mode,
                    r.collected_at_utc,
                    r.rank,
                    r.post_uri,
                    r.post_cid,
                    r.author_did,
                    r.author_handle,
                    r.reason_type,
                    r.reason_actor_did,
                )
                for r in snap.items
            ],
        )

        state.conn.executemany(
            """
            INSERT OR IGNORE INTO posts(
              post_uri, post_cid, author_did, author_handle, record_created_at, indexed_at, text, text_len,
              is_reply, reply_parent_uri, reply_root_uri, is_quote, quoted_uri, embed_type, image_count,
              external_uri, external_domain, facet_link_count, link_domains_json, mention_count, hashtag_count,
              like_count, repost_count, reply_count, quote_count, langs_json, post_labels_json, author_labels_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    p.post_uri,
                    p.post_cid,
                    p.author_did,
                    p.author_handle,
                    p.record_created_at,
                    p.indexed_at,
                    p.text,
                    p.text_len,
                    p.is_reply,
                    p.reply_parent_uri,
                    p.reply_root_uri,
                    p.is_quote,
                    p.quoted_uri,
                    p.embed_type,
                    p.image_count,
                    p.external_uri,
                    p.external_domain,
                    p.facet_link_count,
                    p.link_domains_json,
                    p.mention_count,
                    p.hashtag_count,
                    p.like_count,
                    p.repost_count,
                    p.reply_count,
                    p.quote_count,
                    p.langs_json,
                    p.post_labels_json,
                    p.author_labels_json,
                )
                for p in snap.posts
            ],
        )

        state.conn.executemany(
            """
            INSERT OR IGNORE INTO post_labels(
              post_uri, post_cid, feed_uri, viewer_mode, collected_at_utc,
              label_src, label_val, label_neg, label_uri
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    l.post_uri,
                    l.post_cid,
                    l.feed_uri,
                    l.viewer_mode,
                    l.collected_at_utc,
                    l.label_src,
                    l.label_val,
                    l.label_neg,
                    l.label_uri,
                )
                for l in snap.labels
            ],
        )

        _upsert_snapshot_status(
            state,
            feed_uri=entry.feed_uri,
            feed_group=entry.feed_group,
            viewer_mode=viewer_mode.value,
            collected_at_utc=snap.collected_at_utc,
            requested_items=cfg.run.posts_per_feed,
            returned_items=snap.returned_items,
            pages_fetched=snap.pages_fetched,
            success=1,
            http_status=200,
            error_type=None,
            error_message_short=None,
        )

        state.conn.commit()
    except Exception:  # noqa: BLE001
        state.conn.rollback()
        raise


def _upsert_snapshot_status(
    state: StateDB,
    *,
    feed_uri: str,
    feed_group: str,
    viewer_mode: str,
    collected_at_utc: str,
    requested_items: int,
    returned_items: int,
    pages_fetched: int,
    success: int,
    http_status: int | None,
    error_type: str | None,
    error_message_short: str | None,
) -> None:
    state.conn.execute(
        """
        INSERT INTO feed_snapshot_status(
          feed_uri, viewer_mode, feed_group, collected_at_utc,
          requested_items, returned_items, pages_fetched, success, http_status, error_type, error_message_short
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(feed_uri, viewer_mode) DO UPDATE SET
          feed_group = excluded.feed_group,
          collected_at_utc = excluded.collected_at_utc,
          requested_items = excluded.requested_items,
          returned_items = excluded.returned_items,
          pages_fetched = excluded.pages_fetched,
          success = excluded.success,
          http_status = excluded.http_status,
          error_type = excluded.error_type,
          error_message_short = excluded.error_message_short
        """,
        (
            feed_uri,
            viewer_mode,
            feed_group,
            collected_at_utc,
            requested_items,
            returned_items,
            pages_fetched,
            success,
            http_status,
            error_type,
            error_message_short,
        ),
    )


def _fetch_feed_snapshot(
    cfg: AppConfig,
    http: HttpClient,
    *,
    feed_uri: str,
    feed_group: str,
    viewer_mode: str,
    session: SessionManager | None,
    collected_at_utc: str,
) -> SnapshotResult:
    cursor: str | None = None
    pages = 0
    items: list[FeedItemRow] = []
    posts_map: dict[tuple[str, str], PostRow] = {}
    labels: list[PostLabelRow] = []

    while len(items) < cfg.run.posts_per_feed:
        pages += 1
        params = {
            "feed": feed_uri,
            "limit": cfg.run.feed_page_limit,
            **({"cursor": cursor} if cursor else {}),
        }
        if viewer_mode == ViewerMode.AUTH.value:
            if session is None:
                raise RuntimeError("viewer_mode=auth but session is missing")
            resp = session.xrpc_get(
                endpoint_name="app.bsky.feed.getFeed",
                host=cfg.hosts.appview_host,
                method="app.bsky.feed.getFeed",
                params=params,
            )
        else:
            resp = http.xrpc_get(
                endpoint_name="app.bsky.feed.getFeed",
                host=cfg.hosts.appview_host,
                method="app.bsky.feed.getFeed",
                params=params,
                access_jwt=None,
            )

        feed = resp.get("feed")
        if not isinstance(feed, list):
            raise RuntimeError("getFeed response missing 'feed' list")

        for item in feed:
            if len(items) >= cfg.run.posts_per_feed:
                break
            parsed = _parse_feed_item(item)
            if parsed is None:
                continue
            post_uri, post_cid, author_did, author_handle, reason_type, reason_actor_did, post_row, post_labels = parsed
            rank = len(items) + 1
            items.append(
                FeedItemRow(
                    feed_uri=feed_uri,
                    feed_group=feed_group,
                    viewer_mode=viewer_mode,
                    collected_at_utc=collected_at_utc,
                    rank=rank,
                    post_uri=post_uri,
                    post_cid=post_cid,
                    author_did=author_did,
                    author_handle=author_handle,
                    reason_type=reason_type,
                    reason_actor_did=reason_actor_did,
                )
            )
            if post_row is not None:
                posts_map[(post_row.post_uri, post_row.post_cid)] = post_row
            for l in post_labels:
                labels.append(
                    PostLabelRow(
                        post_uri=l.post_uri,
                        post_cid=l.post_cid,
                        feed_uri=feed_uri,
                        viewer_mode=viewer_mode,
                        collected_at_utc=collected_at_utc,
                        label_src=l.label_src,
                        label_val=l.label_val,
                        label_neg=l.label_neg,
                        label_uri=l.label_uri,
                    )
                )

        cursor = resp.get("cursor") if isinstance(resp.get("cursor"), str) else None
        if not cursor or not feed:
            break

    return SnapshotResult(
        collected_at_utc=collected_at_utc,
        requested_items=cfg.run.posts_per_feed,
        returned_items=len(items),
        pages_fetched=pages,
        items=items,
        posts=list(posts_map.values()),
        labels=labels,
    )

def _parse_feed_item(
    item: Any,
) -> tuple[str, str, str, str | None, str | None, str | None, PostRow | None, list[PostLabelRow]] | None:
    if not isinstance(item, dict):
        return None
    post = item.get("post")
    if not isinstance(post, dict):
        return None

    post_uri = post.get("uri")
    post_cid = post.get("cid")
    if not isinstance(post_uri, str) or not isinstance(post_cid, str) or not post_uri or not post_cid:
        return None

    author = post.get("author")
    author_did = author.get("did") if isinstance(author, dict) else None
    if not isinstance(author_did, str) or not author_did:
        return None
    author_handle = author.get("handle") if isinstance(author, dict) and isinstance(author.get("handle"), str) else None

    reason = item.get("reason")
    reason_type = reason.get("$type") if isinstance(reason, dict) and isinstance(reason.get("$type"), str) else None
    reason_actor_did = None
    if isinstance(reason, dict):
        by = reason.get("by")
        if isinstance(by, dict) and isinstance(by.get("did"), str):
            reason_actor_did = str(by.get("did"))

    post_row = _parse_post_row(post)
    labels = _parse_post_labels(post, post_uri=post_uri, post_cid=post_cid)
    return post_uri, post_cid, author_did, author_handle, reason_type, reason_actor_did, post_row, labels


def _parse_post_row(post: dict[str, Any]) -> PostRow | None:
    post_uri = post.get("uri")
    post_cid = post.get("cid")
    if not isinstance(post_uri, str) or not isinstance(post_cid, str) or not post_uri or not post_cid:
        return None

    author = post.get("author")
    author_did = author.get("did") if isinstance(author, dict) else None
    if not isinstance(author_did, str) or not author_did:
        return None
    author_handle = author.get("handle") if isinstance(author, dict) and isinstance(author.get("handle"), str) else None

    record = post.get("record")
    if not isinstance(record, dict):
        record = {}
    text = record.get("text") if isinstance(record.get("text"), str) else None
    record_created_at = record.get("createdAt") if isinstance(record.get("createdAt"), str) else None

    reply = record.get("reply") if isinstance(record.get("reply"), dict) else None
    is_reply = 1 if reply else 0
    reply_parent_uri = None
    reply_root_uri = None
    if reply:
        parent = reply.get("parent")
        root = reply.get("root")
        if isinstance(parent, dict) and isinstance(parent.get("uri"), str):
            reply_parent_uri = str(parent.get("uri"))
        if isinstance(root, dict) and isinstance(root.get("uri"), str):
            reply_root_uri = str(root.get("uri"))

    embed = record.get("embed") if isinstance(record.get("embed"), dict) else None
    embed_type = embed.get("$type") if isinstance(embed, dict) and isinstance(embed.get("$type"), str) else None

    quoted_uri = _quoted_uri_from_embed(embed)
    is_quote = 1 if quoted_uri else 0

    image_count = _image_count_from_embed(embed)
    external_uri = _external_uri_from_embed(embed)
    external_domain = domain_from_url(external_uri)

    facets = record.get("facets") if isinstance(record.get("facets"), list) else []
    facet_link_count, link_domains, mention_count, hashtag_count = _parse_facets(facets)

    langs = record.get("langs") if isinstance(record.get("langs"), list) else []
    langs_json = json_dumps_compact(langs) if langs else "[]"

    post_labels = post.get("labels") if isinstance(post.get("labels"), list) else []
    post_labels_json = json_dumps_compact(post_labels) if post_labels else "[]"

    author_labels = author.get("labels") if isinstance(author, dict) and isinstance(author.get("labels"), list) else []
    author_labels_json = json_dumps_compact(author_labels) if author_labels else "[]"

    indexed_at = post.get("indexedAt") if isinstance(post.get("indexedAt"), str) else None

    like_count = post.get("likeCount") if isinstance(post.get("likeCount"), int) else None
    repost_count = post.get("repostCount") if isinstance(post.get("repostCount"), int) else None
    reply_count = post.get("replyCount") if isinstance(post.get("replyCount"), int) else None
    quote_count = post.get("quoteCount") if isinstance(post.get("quoteCount"), int) else None

    return PostRow(
        post_uri=post_uri,
        post_cid=post_cid,
        author_did=author_did,
        author_handle=author_handle,
        record_created_at=record_created_at,
        indexed_at=indexed_at,
        text=text,
        text_len=len(text) if text else 0,
        is_reply=is_reply,
        reply_parent_uri=reply_parent_uri,
        reply_root_uri=reply_root_uri,
        is_quote=is_quote,
        quoted_uri=quoted_uri,
        embed_type=embed_type,
        image_count=image_count,
        external_uri=external_uri,
        external_domain=external_domain,
        facet_link_count=facet_link_count,
        link_domains_json=json_dumps_compact(sorted(link_domains)) if link_domains else "[]",
        mention_count=mention_count,
        hashtag_count=hashtag_count,
        like_count=like_count,
        repost_count=repost_count,
        reply_count=reply_count,
        quote_count=quote_count,
        langs_json=langs_json,
        post_labels_json=post_labels_json,
        author_labels_json=author_labels_json,
    )


def _parse_post_labels(post: dict[str, Any], *, post_uri: str, post_cid: str) -> list[PostLabelRow]:
    labels = post.get("labels")
    if not isinstance(labels, list):
        return []
    out: list[PostLabelRow] = []
    for l in labels:
        if not isinstance(l, dict):
            continue
        src = l.get("src")
        val = l.get("val")
        if not isinstance(src, str) or not src or not isinstance(val, str) or not val:
            continue
        neg = l.get("neg")
        neg_i = (1 if neg else 0) if isinstance(neg, bool) else None
        uri = l.get("uri") if isinstance(l.get("uri"), str) else ""
        out.append(
            PostLabelRow(
                post_uri=post_uri,
                post_cid=post_cid,
                feed_uri="",  # filled by caller
                viewer_mode="",  # filled by caller
                collected_at_utc="",
                label_src=src,
                label_val=val,
                label_neg=neg_i,
                label_uri=uri,
            )
        )
    return out


def _parse_facets(facets: list[Any]) -> tuple[int, set[str], int, int]:
    facet_link_count = 0
    mention_count = 0
    hashtag_count = 0
    link_domains: set[str] = set()

    for f in facets:
        if not isinstance(f, dict):
            continue
        features = f.get("features")
        if not isinstance(features, list):
            continue
        for feat in features:
            if not isinstance(feat, dict):
                continue
            t = feat.get("$type") if isinstance(feat.get("$type"), str) else ""
            if t.endswith("#link") and isinstance(feat.get("uri"), str):
                facet_link_count += 1
                dom = domain_from_url(str(feat.get("uri")))
                if dom:
                    link_domains.add(dom)
            elif t.endswith("#mention"):
                mention_count += 1
            elif t.endswith("#tag"):
                hashtag_count += 1

    return facet_link_count, link_domains, mention_count, hashtag_count


def _quoted_uri_from_embed(embed: dict[str, Any] | None) -> str | None:
    if not embed:
        return None
    t = embed.get("$type") if isinstance(embed.get("$type"), str) else ""
    if t == "app.bsky.embed.record":
        rec = embed.get("record")
        if isinstance(rec, dict) and isinstance(rec.get("uri"), str):
            return str(rec.get("uri"))
    if t == "app.bsky.embed.recordWithMedia":
        rec = embed.get("record")
        if isinstance(rec, dict):
            rec2 = rec.get("record")
            if isinstance(rec2, dict) and isinstance(rec2.get("uri"), str):
                return str(rec2.get("uri"))
    return None


def _image_count_from_embed(embed: dict[str, Any] | None) -> int:
    if not embed:
        return 0
    t = embed.get("$type") if isinstance(embed.get("$type"), str) else ""
    if t == "app.bsky.embed.images":
        images = embed.get("images")
        return len(images) if isinstance(images, list) else 0
    if t == "app.bsky.embed.recordWithMedia":
        media = embed.get("media")
        if isinstance(media, dict):
            return _image_count_from_embed(media)
    return 0


def _external_uri_from_embed(embed: dict[str, Any] | None) -> str | None:
    if not embed:
        return None
    t = embed.get("$type") if isinstance(embed.get("$type"), str) else ""
    if t == "app.bsky.embed.external":
        ext = embed.get("external")
        if isinstance(ext, dict) and isinstance(ext.get("uri"), str):
            return str(ext.get("uri"))
    if t == "app.bsky.embed.recordWithMedia":
        media = embed.get("media")
        if isinstance(media, dict):
            return _external_uri_from_embed(media)
    return None
