from __future__ import annotations

from bsky_collector_v2.jobs.build_panel import FeedSignals, PanelBuildConfig, _select_panel


def test_panel_builder_deterministic_and_fills() -> None:
    cfg = PanelBuildConfig()
    total = cfg.k1_popular + cfg.k2_onboarding + cfg.k3_suggested + cfg.k4_longtail

    signals: list[FeedSignals] = []
    for i in range(2000):
        feed_uri = f"at://did:plc:feed{i:04d}/app.bsky.feed.generator/main"
        signals.append(
            FeedSignals(
                feed_uri=feed_uri,
                like_count=2000 - i,
                provider_domain="example.com",
                onboarding_inclusion_count=(i % 7),
                onboarding_join_weighted=(i % 13) * 10,
                suggested_rank=i if i < 500 else None,
            )
        )

    out1 = _select_panel(signals, cfg, seed=123)
    out2 = _select_panel(signals, cfg, seed=123)
    assert out1 == out2
    assert len(out1) == total
    assert len({u for u, _b in out1}) == total

    bucket_counts: dict[str, int] = {}
    for _u, b in out1:
        bucket_counts[b] = bucket_counts.get(b, 0) + 1
    assert bucket_counts["popular_by_likecount"] == cfg.k1_popular
    assert bucket_counts["onboarding_surfaced"] == cfg.k2_onboarding
    assert bucket_counts.get("suggested", 0) <= cfg.k3_suggested
    assert bucket_counts["longtail_random"] >= cfg.k4_longtail
    assert sum(bucket_counts.values()) == total


def test_panel_builder_no_onboarding_bucket_when_inputs_empty() -> None:
    cfg = PanelBuildConfig(k1_popular=10, k2_onboarding=5, k3_suggested=0, k4_longtail=0)
    total = cfg.k1_popular + cfg.k2_onboarding + cfg.k3_suggested + cfg.k4_longtail

    signals: list[FeedSignals] = []
    for i in range(50):
        feed_uri = f"at://did:plc:feed{i:04d}/app.bsky.feed.generator/main"
        signals.append(
            FeedSignals(
                feed_uri=feed_uri,
                like_count=50 - i,
                provider_domain="example.com",
                onboarding_inclusion_count=0,
                onboarding_join_weighted=0,
                suggested_rank=None,
            )
        )

    out = _select_panel(signals, cfg, seed=123)
    assert len(out) == total
    assert len({u for u, _b in out}) == total
    assert all(bucket != "onboarding_surfaced" for _u, bucket in out)
