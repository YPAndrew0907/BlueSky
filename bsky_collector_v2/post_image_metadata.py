from __future__ import annotations

import csv
import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Literal


ImageSourceKind = Literal["images_embed", "record_with_media_images", "external_thumb"]


@dataclass(frozen=True)
class ImageEntry:
    source_kind: ImageSourceKind
    position: int
    fullsize_url: str
    thumb_url: str
    alt_text: str
    about_text: str
    aspect_ratio_width: int | None
    aspect_ratio_height: int | None
    external_uri: str
    external_title: str
    external_description: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_kind": self.source_kind,
            "position": self.position,
            "fullsize_url": self.fullsize_url,
            "thumb_url": self.thumb_url,
            "alt_text": self.alt_text,
            "about_text": self.about_text,
            "aspect_ratio_width": self.aspect_ratio_width,
            "aspect_ratio_height": self.aspect_ratio_height,
            "external_uri": self.external_uri,
            "external_title": self.external_title,
            "external_description": self.external_description,
        }


@dataclass(frozen=True)
class PostImageMetadata:
    post_uri: str
    cid: str
    author_did: str
    author_handle: str
    author_display_name: str
    text: str
    record_created_at: str
    indexed_at: str
    embed_type: str
    media_embed_type: str
    labels: tuple[str, ...]
    image_count: int
    images: tuple[ImageEntry, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "post_uri": self.post_uri,
            "cid": self.cid,
            "author_did": self.author_did,
            "author_handle": self.author_handle,
            "author_display_name": self.author_display_name,
            "text": self.text,
            "record_created_at": self.record_created_at,
            "indexed_at": self.indexed_at,
            "embed_type": self.embed_type,
            "media_embed_type": self.media_embed_type,
            "labels": list(self.labels),
            "image_count": self.image_count,
            "images": [image.to_dict() for image in self.images],
        }


def make_get_posts_url(appview_host: str, uris: Iterable[str]) -> str:
    query = "&".join(
        "uris=" + urllib.parse.quote(post_uri, safe="")
        for post_uri in uris
    )
    return f"{appview_host.rstrip('/')}/xrpc/app.bsky.feed.getPosts?{query}"


def fetch_posts_batch(
    appview_host: str,
    uris: list[str],
    *,
    timeout_s: float = 60.0,
) -> list[dict[str, Any]]:
    if not uris:
        return []
    request_url = make_get_posts_url(appview_host, uris)
    request = urllib.request.Request(
        request_url,
        headers={"User-Agent": "BlueSky-PostImageMetadata/1.0"},
    )
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        payload = json.load(response)
    posts = payload.get("posts", [])
    if not isinstance(posts, list):
        raise ValueError("app.bsky.feed.getPosts returned non-list posts payload")
    return [post for post in posts if isinstance(post, dict)]


def load_study_sample_post_uris(
    root: Path,
    study_id: str,
    *,
    limit: int,
) -> list[str]:
    if limit <= 0:
        raise ValueError(f"limit must be positive: {limit}")
    study_root = root / "micro5" / study_id / "micro5_core_full"
    if not study_root.exists():
        raise FileNotFoundError(f"study root not found: {study_root}")

    seen: set[str] = set()
    sample: list[str] = []
    for path in sorted(study_root.rglob("parts/feed_items_part_000.csv")):
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            if "post_uri" not in (reader.fieldnames or []):
                raise ValueError(f"feed_items file missing post_uri column: {path}")
            for row in reader:
                post_uri = row.get("post_uri") or ""
                if not post_uri or post_uri in seen:
                    continue
                seen.add(post_uri)
                sample.append(post_uri)
                if len(sample) >= limit:
                    return sample
    return sample


def _extract_labels(post_payload: dict[str, Any]) -> tuple[str, ...]:
    labels = post_payload.get("labels", [])
    if not isinstance(labels, list):
        return ()
    values: list[str] = []
    for label in labels:
        if not isinstance(label, dict):
            continue
        value = label.get("val")
        if isinstance(value, str) and value:
            values.append(value)
    return tuple(values)


def _as_int(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    return None


def _about_text(*parts: str) -> str:
    cleaned = [part.strip() for part in parts if part and part.strip()]
    return " | ".join(cleaned)


def _extract_images_from_image_view(
    image_view: dict[str, Any],
    *,
    source_kind: ImageSourceKind,
    fallback_about_text: str,
    external_uri: str = "",
    external_title: str = "",
    external_description: str = "",
) -> tuple[ImageEntry, ...]:
    images = image_view.get("images", [])
    if not isinstance(images, list):
        return ()
    rows: list[ImageEntry] = []
    for position, image in enumerate(images):
        if not isinstance(image, dict):
            continue
        aspect_ratio = image.get("aspectRatio") or {}
        fullsize_url = str(image.get("fullsize") or "")
        thumb_url = str(image.get("thumb") or "")
        alt_text = str(image.get("alt") or "")
        rows.append(
            ImageEntry(
                source_kind=source_kind,
                position=position,
                fullsize_url=fullsize_url,
                thumb_url=thumb_url,
                alt_text=alt_text,
                about_text=_about_text(alt_text, fallback_about_text),
                aspect_ratio_width=_as_int(aspect_ratio.get("width")),
                aspect_ratio_height=_as_int(aspect_ratio.get("height")),
                external_uri=external_uri,
                external_title=external_title,
                external_description=external_description,
            )
        )
    return tuple(rows)


def extract_post_image_metadata(post_payload: dict[str, Any]) -> PostImageMetadata:
    author = post_payload.get("author") or {}
    record = post_payload.get("record") or {}
    embed = post_payload.get("embed") or {}
    post_text = str(record.get("text") or "")
    embed_type = str(embed.get("$type") or "none") if isinstance(embed, dict) else "none"
    media_embed_type = "none"
    images: tuple[ImageEntry, ...] = ()

    if embed_type == "app.bsky.embed.images#view" and isinstance(embed, dict):
        images = _extract_images_from_image_view(
            embed,
            source_kind="images_embed",
            fallback_about_text=post_text,
        )
        media_embed_type = embed_type
    elif embed_type == "app.bsky.embed.recordWithMedia#view" and isinstance(embed, dict):
        media = embed.get("media") or {}
        if isinstance(media, dict):
            media_embed_type = str(media.get("$type") or "none")
            if media_embed_type == "app.bsky.embed.images#view":
                images = _extract_images_from_image_view(
                    media,
                    source_kind="record_with_media_images",
                    fallback_about_text=post_text,
                )
    elif embed_type == "app.bsky.embed.external#view" and isinstance(embed, dict):
        external = embed.get("external") or {}
        if isinstance(external, dict):
            thumb_url = str(external.get("thumb") or "")
            if thumb_url:
                external_title = str(external.get("title") or "")
                external_description = str(external.get("description") or "")
                images = (
                    ImageEntry(
                        source_kind="external_thumb",
                        position=0,
                        fullsize_url="",
                        thumb_url=thumb_url,
                        alt_text="",
                        about_text=_about_text(
                            external_title,
                            external_description,
                            post_text,
                        ),
                        aspect_ratio_width=None,
                        aspect_ratio_height=None,
                        external_uri=str(external.get("uri") or ""),
                        external_title=external_title,
                        external_description=external_description,
                    ),
                )
                media_embed_type = embed_type

    return PostImageMetadata(
        post_uri=str(post_payload.get("uri") or ""),
        cid=str(post_payload.get("cid") or ""),
        author_did=str(author.get("did") or ""),
        author_handle=str(author.get("handle") or ""),
        author_display_name=str(author.get("displayName") or ""),
        text=post_text,
        record_created_at=str(record.get("createdAt") or ""),
        indexed_at=str(post_payload.get("indexedAt") or ""),
        embed_type=embed_type,
        media_embed_type=media_embed_type,
        labels=_extract_labels(post_payload),
        image_count=len(images),
        images=images,
    )
