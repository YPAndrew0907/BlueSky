from __future__ import annotations

from bsky_collector_v2.topic_presets import get_topic_preset


def test_politics_preset_contains_multiple_topics() -> None:
    topics = get_topic_preset("politics_v1")
    slugs = {topic.slug for topic in topics}
    assert "epstein" in slugs
    assert "gaza" in slugs
    assert "immigration" in slugs
