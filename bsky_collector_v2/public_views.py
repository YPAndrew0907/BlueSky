from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse


def json_compact(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    except Exception:  # noqa: BLE001
        return None


def _as_str(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _extract_label_values(labels: Any) -> list[str]:
    out: list[str] = []
    if not isinstance(labels, list):
        return out
    for lab in labels:
        if not isinstance(lab, dict):
            continue
        value = lab.get("val")
        if isinstance(value, str) and value:
            out.append(value)
    return out


def _extract_self_label_values(record_labels: Any) -> list[str]:
    if not isinstance(record_labels, dict):
        return []
    values = record_labels.get("values")
    if not isinstance(values, list):
        return []
    out: list[str] = []
    for item in values:
        if not isinstance(item, dict):
            continue
        value = item.get("val")
        if isinstance(value, str) and value:
            out.append(value)
    return out


def _extract_facets(record: dict[str, Any]) -> tuple[int, int, int, int]:
    facets = record.get("facets")
    if not isinstance(facets, list):
        return (0, 0, 0, 0)
    mention_count = 0
    link_count = 0
    hashtag_count = 0
    for facet in facets:
        if not isinstance(facet, dict):
            continue
        features = facet.get("features")
        if not isinstance(features, list):
            continue
        for feat in features:
            if not isinstance(feat, dict):
                continue
            ftype = feat.get("$type")
            if ftype == "app.bsky.richtext.facet#mention":
                mention_count += 1
            elif ftype == "app.bsky.richtext.facet#link":
                link_count += 1
            elif ftype == "app.bsky.richtext.facet#tag":
                hashtag_count += 1
    return (len(facets), mention_count, link_count, hashtag_count)


def _extract_external_info(embed: dict[str, Any]) -> tuple[str | None, str | None]:
    candidate_blocks: list[dict[str, Any]] = []
    if isinstance(embed.get("external"), dict):
        candidate_blocks.append(embed["external"])
    media = embed.get("media")
    if isinstance(media, dict) and isinstance(media.get("external"), dict):
        candidate_blocks.append(media["external"])
    for block in candidate_blocks:
        uri = _as_str(block.get("uri"))
        if uri:
            try:
                domain = urlparse(uri).netloc or None
            except Exception:  # noqa: BLE001
                domain = None
            return uri, domain
    return None, None


def extract_post_record_features(post: dict[str, Any]) -> dict[str, Any]:
    record = post.get("record") if isinstance(post.get("record"), dict) else {}
    embed = post.get("embed") if isinstance(post.get("embed"), dict) else {}
    embed_type = _as_str(embed.get("$type")) or "none"
    media = embed.get("media") if isinstance(embed.get("media"), dict) else {}
    media_embed_type = _as_str(media.get("$type")) or "none"

    has_image = embed_type == "app.bsky.embed.images#view" or media_embed_type == "app.bsky.embed.images#view"
    has_video = embed_type == "app.bsky.embed.video#view" or media_embed_type == "app.bsky.embed.video#view"
    has_external = embed_type == "app.bsky.embed.external#view" or media_embed_type == "app.bsky.embed.external#view"
    has_record_embed = embed_type in {"app.bsky.embed.record#view", "app.bsky.embed.recordWithMedia#view"}
    is_quote = embed_type in {"app.bsky.embed.record#view", "app.bsky.embed.recordWithMedia#view"}

    external_uri, external_domain = _extract_external_info(embed)

    reply_ref = record.get("reply") if isinstance(record.get("reply"), dict) else {}
    root = reply_ref.get("root") if isinstance(reply_ref.get("root"), dict) else {}
    parent = reply_ref.get("parent") if isinstance(reply_ref.get("parent"), dict) else {}
    reply_root_uri = _as_str(root.get("uri"))
    reply_parent_uri = _as_str(parent.get("uri"))
    is_reply = bool(reply_root_uri and reply_parent_uri)

    langs = record.get("langs") if isinstance(record.get("langs"), list) else []
    langs_clean = [str(lang) for lang in langs if isinstance(lang, str) and lang]

    tags = record.get("tags") if isinstance(record.get("tags"), list) else []
    tags_clean = [str(tag) for tag in tags if isinstance(tag, str) and tag]

    facets_count, mention_count, link_count, hashtag_count = _extract_facets(record)
    self_label_values = _extract_self_label_values(record.get("labels"))
    post_label_values = _extract_label_values(post.get("labels"))
    author = post.get("author") if isinstance(post.get("author"), dict) else {}
    author_label_values = _extract_label_values(author.get("labels"))
    combined_label_values = list(dict.fromkeys(post_label_values + author_label_values))

    contains_no_unauthenticated = 1 if "!no-unauthenticated" in combined_label_values else 0
    contains_hide_like_label = 1 if any(v.startswith("!hide") or v == "!hide" for v in combined_label_values) else 0

    return {
        "text": _as_str(record.get("text")),
        "record_created_at": _as_str(record.get("createdAt")),
        "indexed_at": _as_str(post.get("indexedAt")),
        "is_reply": 1 if is_reply else 0,
        "is_quote": 1 if is_quote else 0,
        "reply_root_uri": reply_root_uri,
        "reply_parent_uri": reply_parent_uri,
        "embed_type": embed_type,
        "media_embed_type": media_embed_type,
        "has_image": 1 if has_image else 0,
        "has_video": 1 if has_video else 0,
        "has_external": 1 if has_external else 0,
        "has_record_embed": 1 if has_record_embed else 0,
        "external_uri": external_uri,
        "external_domain": external_domain,
        "lang_primary": langs_clean[0] if langs_clean else None,
        "lang_count": len(langs_clean),
        "langs_json": json_compact(langs_clean),
        "tag_count": len(tags_clean),
        "tags_json": json_compact(tags_clean),
        "facets_count": facets_count,
        "mention_count": mention_count,
        "link_count": link_count,
        "hashtag_count": hashtag_count,
        "self_label_values_json": json_compact(self_label_values),
        "post_label_values_json": json_compact(post_label_values),
        "author_label_values_json": json_compact(author_label_values),
        "contains_no_unauthenticated": contains_no_unauthenticated,
        "contains_hide_like_label": contains_hide_like_label,
    }


def extract_feed_item_features(item: dict[str, Any]) -> dict[str, Any]:
    reason = item.get("reason") if isinstance(item.get("reason"), dict) else {}
    reason_by = reason.get("by") if isinstance(reason.get("by"), dict) else {}
    reply = item.get("reply") if isinstance(item.get("reply"), dict) else {}
    root = reply.get("root") if isinstance(reply.get("root"), dict) else {}
    parent = reply.get("parent") if isinstance(reply.get("parent"), dict) else {}
    grandparent_author = reply.get("grandparentAuthor") if isinstance(reply.get("grandparentAuthor"), dict) else {}
    return {
        "reason_type": _as_str(reason.get("$type")),
        "reason_actor_did": _as_str(reason_by.get("did")),
        "reason_actor_handle": _as_str(reason_by.get("handle")),
        "reason_repost_uri": _as_str(reason.get("uri")),
        "reason_repost_cid": _as_str(reason.get("cid")),
        "reason_repost_indexed_at": _as_str(reason.get("indexedAt")),
        "reply_root_uri": _as_str(root.get("uri")),
        "reply_parent_uri": _as_str(parent.get("uri")),
        "reply_grandparent_author_did": _as_str(grandparent_author.get("did")),
        "feed_context": _as_str(item.get("feedContext")),
        "req_id": _as_str(item.get("reqId")),
    }


def flatten_profile_view_detailed(profile: dict[str, Any]) -> dict[str, Any]:
    joined_via = profile.get("joinedViaStarterPack") if isinstance(profile.get("joinedViaStarterPack"), dict) else {}
    pinned_post = profile.get("pinnedPost") if isinstance(profile.get("pinnedPost"), dict) else {}
    return {
        "handle": _as_str(profile.get("handle")),
        "display_name": _as_str(profile.get("displayName")),
        "description": _as_str(profile.get("description")),
        "website": _as_str(profile.get("website")),
        "avatar": _as_str(profile.get("avatar")),
        "banner": _as_str(profile.get("banner")),
        "followers_count": profile.get("followersCount"),
        "follows_count": profile.get("followsCount"),
        "posts_count": profile.get("postsCount"),
        "associated_json": json_compact(profile.get("associated")),
        "joined_via_starter_pack_uri": _as_str(joined_via.get("uri")),
        "indexed_at": _as_str(profile.get("indexedAt")),
        "created_at": _as_str(profile.get("createdAt")),
        "labels_json": json_compact(profile.get("labels")),
        "pinned_post_uri": _as_str(pinned_post.get("uri")),
        "verification_json": json_compact(profile.get("verification")),
        "status_json": json_compact(profile.get("status")),
    }


def flatten_generator_view(generator: dict[str, Any]) -> dict[str, Any]:
    creator = generator.get("creator") if isinstance(generator.get("creator"), dict) else {}
    return {
        "feed_uri": _as_str(generator.get("uri")),
        "feed_cid": _as_str(generator.get("cid")),
        "feed_did": _as_str(generator.get("did")),
        "creator_did": _as_str(creator.get("did")),
        "creator_handle": _as_str(creator.get("handle")),
        "creator_display_name": _as_str(creator.get("displayName")),
        "display_name": _as_str(generator.get("displayName")),
        "description": _as_str(generator.get("description")),
        "avatar": _as_str(generator.get("avatar")),
        "like_count": generator.get("likeCount"),
        "accepts_interactions": 1 if generator.get("acceptsInteractions") is True else 0 if generator.get("acceptsInteractions") is False else None,
        "content_mode": _as_str(generator.get("contentMode")),
        "indexed_at": _as_str(generator.get("indexedAt")),
        "labels_json": json_compact(generator.get("labels")),
    }
