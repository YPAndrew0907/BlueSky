from __future__ import annotations

from typing import Any

from bsky_collector_v2.public_views import (
    extract_feed_item_features,
    extract_post_record_features,
    flatten_generator_view,
    flatten_profile_view_detailed,
    json_compact,
)


def _as_str(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def extract_label_src_dids(labels: Any) -> set[str]:
    out: set[str] = set()
    if not isinstance(labels, list):
        return out
    for item in labels:
        if not isinstance(item, dict):
            continue
        src = item.get("src")
        if isinstance(src, str) and src:
            out.add(src)
    return out


def flatten_actor_view(actor: dict[str, Any]) -> dict[str, Any]:
    viewer = actor.get("viewer") if isinstance(actor.get("viewer"), dict) else {}
    return {
        "did": _as_str(actor.get("did")),
        "handle": _as_str(actor.get("handle")),
        "display_name": _as_str(actor.get("displayName")),
        "description": _as_str(actor.get("description")),
        "avatar": _as_str(actor.get("avatar")),
        "associated_json": json_compact(actor.get("associated")),
        "indexed_at": _as_str(actor.get("indexedAt")),
        "created_at": _as_str(actor.get("createdAt")),
        "labels_json": json_compact(actor.get("labels")),
        "viewer_muted": 1 if viewer.get("muted") is True else 0 if viewer.get("muted") is False else None,
        "viewer_blocked_by": 1 if viewer.get("blockedBy") is True else 0 if viewer.get("blockedBy") is False else None,
    }


def flatten_follow_record(record: dict[str, Any]) -> dict[str, Any]:
    value = record.get("value") if isinstance(record.get("value"), dict) else {}
    return {
        "record_uri": _as_str(record.get("uri")),
        "record_cid": _as_str(record.get("cid")),
        "subject_did": _as_str(value.get("subject")),
        "created_at": _as_str(value.get("createdAt")),
        "value_json": json_compact(value),
    }


def flatten_repo_description(desc: dict[str, Any]) -> dict[str, Any]:
    collections = desc.get("collections") if isinstance(desc.get("collections"), list) else []
    collections_clean = [str(v) for v in collections if isinstance(v, str) and v]
    return {
        "did": _as_str(desc.get("did")),
        "handle": _as_str(desc.get("handle")),
        "handle_is_correct": 1 if desc.get("handleIsCorrect") is True else 0 if desc.get("handleIsCorrect") is False else None,
        "collections_count": len(collections_clean),
        "collections_json": json_compact(collections_clean),
    }


def flatten_author_feed_item(item: dict[str, Any]) -> dict[str, Any]:
    post = item.get("post") if isinstance(item.get("post"), dict) else {}
    author = post.get("author") if isinstance(post.get("author"), dict) else {}
    return {
        "post_uri": _as_str(post.get("uri")),
        "post_cid": _as_str(post.get("cid")),
        "post_author_did": _as_str(author.get("did")),
        "post_author_handle": _as_str(author.get("handle")),
        **extract_feed_item_features(item),
        **extract_post_record_features(post),
    }


def flatten_thread_node(*, focus_post_uri: str, relation_to_focus: str, distance_to_focus: int, node: dict[str, Any]) -> dict[str, Any]:
    node_type = _as_str(node.get("$type")) or "threadNode"
    post = node.get("post") if isinstance(node.get("post"), dict) else {}
    author = post.get("author") if isinstance(post.get("author"), dict) else {}
    out = {
        "focus_post_uri": focus_post_uri,
        "relation_to_focus": relation_to_focus,
        "distance_to_focus": distance_to_focus,
        "node_type": node_type,
        "post_uri": _as_str(post.get("uri")),
        "post_cid": _as_str(post.get("cid")),
        "author_did": _as_str(author.get("did")),
        "author_handle": _as_str(author.get("handle")),
        "labels_json": json_compact(post.get("labels")),
        "raw_json": json_compact(node),
    }
    if post:
        out.update(extract_post_record_features(post))
    else:
        out.update({
            "text": None,
            "record_created_at": None,
            "indexed_at": None,
            "is_reply": None,
            "is_quote": None,
            "reply_root_uri": None,
            "reply_parent_uri": None,
            "embed_type": None,
            "media_embed_type": None,
            "has_image": None,
            "has_video": None,
            "has_external": None,
            "has_record_embed": None,
            "external_uri": None,
            "external_domain": None,
            "lang_primary": None,
            "lang_count": None,
            "langs_json": None,
            "tag_count": None,
            "tags_json": None,
            "facets_count": None,
            "mention_count": None,
            "link_count": None,
            "hashtag_count": None,
            "self_label_values_json": None,
            "post_label_values_json": None,
            "author_label_values_json": None,
            "contains_no_unauthenticated": None,
            "contains_hide_like_label": None,
        })
        out.update({
            "like_count": None,
            "repost_count": None,
            "reply_count": None,
            "quote_count": None,
        })
    out.setdefault("like_count", post.get("likeCount"))
    out.setdefault("repost_count", post.get("repostCount"))
    out.setdefault("reply_count", post.get("replyCount"))
    out.setdefault("quote_count", post.get("quoteCount"))
    return out


def flatten_list_view(view: dict[str, Any]) -> dict[str, Any]:
    creator = view.get("creator") if isinstance(view.get("creator"), dict) else {}
    return {
        "list_uri": _as_str(view.get("uri")),
        "list_cid": _as_str(view.get("cid")),
        "list_name": _as_str(view.get("name")),
        "purpose": _as_str(view.get("purpose")),
        "description": _as_str(view.get("description")),
        "avatar": _as_str(view.get("avatar")),
        "indexed_at": _as_str(view.get("indexedAt")),
        "creator_did": _as_str(creator.get("did")),
        "creator_handle": _as_str(creator.get("handle")),
        "creator_display_name": _as_str(creator.get("displayName")),
        "labels_json": json_compact(view.get("labels")),
        "viewer_json": json_compact(view.get("viewer")),
    }


def flatten_list_item(item: dict[str, Any]) -> dict[str, Any]:
    subject = item.get("subject") if isinstance(item.get("subject"), dict) else {}
    actor = flatten_actor_view(subject)
    return {
        "list_item_uri": _as_str(item.get("uri")),
        "subject_did": actor.get("did"),
        "subject_handle": actor.get("handle"),
        "subject_display_name": actor.get("display_name"),
        "subject_description": actor.get("description"),
        "subject_avatar": actor.get("avatar"),
        "subject_associated_json": actor.get("associated_json"),
        "subject_indexed_at": actor.get("indexed_at"),
        "subject_created_at": actor.get("created_at"),
        "subject_labels_json": actor.get("labels_json"),
        "subject_viewer_muted": actor.get("viewer_muted"),
        "subject_viewer_blocked_by": actor.get("viewer_blocked_by"),
    }


def flatten_starter_pack_view(view: dict[str, Any]) -> dict[str, Any]:
    creator = view.get("creator") if isinstance(view.get("creator"), dict) else {}
    record = view.get("record") if isinstance(view.get("record"), dict) else {}
    list_view = view.get("list") if isinstance(view.get("list"), dict) else {}
    return {
        "starter_pack_uri": _as_str(view.get("uri")),
        "starter_pack_cid": _as_str(view.get("cid")),
        "record_created_at": _as_str(record.get("createdAt")),
        "indexed_at": _as_str(view.get("indexedAt")),
        "joined_week_count": view.get("joinedWeekCount"),
        "joined_all_time_count": view.get("joinedAllTimeCount"),
        "creator_did": _as_str(creator.get("did")),
        "creator_handle": _as_str(creator.get("handle")),
        "creator_display_name": _as_str(creator.get("displayName")),
        "list_uri": _as_str(list_view.get("uri")),
        "labels_json": json_compact(view.get("labels")),
    }


def flatten_labeler_service_view(view: dict[str, Any]) -> dict[str, Any]:
    creator = view.get("creator") if isinstance(view.get("creator"), dict) else {}
    uri = _as_str(view.get("uri"))
    labeler_did = None
    if isinstance(uri, str) and uri.startswith("at://"):
        labeler_did = uri.removeprefix("at://").split("/")[0] or None
    return {
        "labeler_did": labeler_did or _as_str(creator.get("did")),
        "creator_did": _as_str(creator.get("did")),
        "creator_handle": _as_str(creator.get("handle")),
        "creator_display_name": _as_str(creator.get("displayName")),
        "indexed_at": _as_str(view.get("indexedAt")),
        "labels_json": json_compact(view.get("labels")),
        "policies_json": json_compact(view.get("policies")),
    }


__all__ = [
    "extract_label_src_dids",
    "flatten_actor_view",
    "flatten_author_feed_item",
    "flatten_follow_record",
    "flatten_generator_view",
    "flatten_labeler_service_view",
    "flatten_list_item",
    "flatten_list_view",
    "flatten_profile_view_detailed",
    "flatten_repo_description",
    "flatten_starter_pack_view",
    "flatten_thread_node",
]
