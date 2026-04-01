from bsky_collector_v2.post_image_metadata import extract_post_image_metadata


def test_extract_native_image_embed_metadata() -> None:
    payload = {
        "uri": "at://example/post/1",
        "cid": "cid-1",
        "author": {
            "did": "did:example:alice",
            "handle": "alice.example.com",
            "displayName": "Alice",
        },
        "record": {
            "text": "look at this cat",
            "createdAt": "2026-03-30T12:00:00Z",
        },
        "indexedAt": "2026-03-30T12:00:05Z",
        "embed": {
            "$type": "app.bsky.embed.images#view",
            "images": [
                {
                    "thumb": "https://cdn.example/thumb.jpg",
                    "fullsize": "https://cdn.example/full.jpg",
                    "alt": "orange cat sleeping on a chair",
                    "aspectRatio": {"width": 1200, "height": 900},
                }
            ],
        },
        "labels": [{"val": "safe"}],
    }

    metadata = extract_post_image_metadata(payload)

    assert metadata.embed_type == "app.bsky.embed.images#view"
    assert metadata.image_count == 1
    assert metadata.labels == ("safe",)
    assert metadata.images[0].fullsize_url == "https://cdn.example/full.jpg"
    assert metadata.images[0].thumb_url == "https://cdn.example/thumb.jpg"
    assert metadata.images[0].alt_text == "orange cat sleeping on a chair"
    assert metadata.images[0].about_text == "orange cat sleeping on a chair | look at this cat"
    assert metadata.images[0].aspect_ratio_width == 1200
    assert metadata.images[0].aspect_ratio_height == 900


def test_extract_external_thumb_metadata() -> None:
    payload = {
        "uri": "at://example/post/2",
        "cid": "cid-2",
        "author": {
            "did": "did:example:bob",
            "handle": "bob.example.com",
            "displayName": "Bob",
        },
        "record": {
            "text": "interesting link",
            "createdAt": "2026-03-30T12:10:00Z",
        },
        "indexedAt": "2026-03-30T12:10:05Z",
        "embed": {
            "$type": "app.bsky.embed.external#view",
            "external": {
                "uri": "https://example.com/story",
                "title": "Story title",
                "description": "Story description",
                "thumb": "https://cdn.example/story-thumb.jpg",
            },
        },
        "labels": [],
    }

    metadata = extract_post_image_metadata(payload)

    assert metadata.embed_type == "app.bsky.embed.external#view"
    assert metadata.image_count == 1
    assert metadata.images[0].source_kind == "external_thumb"
    assert metadata.images[0].thumb_url == "https://cdn.example/story-thumb.jpg"
    assert metadata.images[0].external_uri == "https://example.com/story"
    assert metadata.images[0].about_text == "Story title | Story description | interesting link"
