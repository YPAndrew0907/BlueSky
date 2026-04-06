from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from bsky_collector_v2.http_client import XrpcHosts
from bsky_collector_v2.jobs.public_omnibus import PublicOmnibusConfig, run_public_omnibus
from bsky_collector_v2.layout import Layout


class _DummySummary:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = dict(payload)

    def to_dict(self) -> dict[str, Any]:
        return dict(self._payload)


def test_public_omnibus_orchestrates_public_only_pipeline(tmp_path: Path, monkeypatch) -> None:
    layout = Layout(tmp_path)
    study_dir = layout.study_dir("studyA")
    study_dir.mkdir(parents=True, exist_ok=True)
    layout.study_manifest_json("studyA").write_text(
        json.dumps(
            {
                "study_id": "studyA",
                "sample_family": "micro5_core_full",
                "viewer_modes": ["auth"],
                "intended_window_minutes": 5,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    calls: list[tuple[str, dict[str, Any]]] = []

    def _record(name: str, **kwargs: Any) -> None:
        calls.append((name, kwargs))

    def fake_seed_post_registry(**kwargs: Any) -> _DummySummary:
        _record("seed-post-registry", **kwargs)
        return _DummySummary({"files_scanned": 7})

    async def fake_index_feed_generators(**kwargs: Any) -> None:
        _record("index-feed-generators", **kwargs)

    async def fake_refresh_discovery(**kwargs: Any) -> None:
        _record("refresh-discovery", **kwargs)

    async def fake_build_panel(**kwargs: Any) -> None:
        _record("build-panel", **kwargs)

    async def fake_hydrate_feed_generators(**kwargs: Any) -> None:
        _record("hydrate-feed-generators", **kwargs)

    async def fake_snapshot_panel(**kwargs: Any) -> None:
        _record("snapshot-panel", **kwargs)

    async def fake_micro_snapshot_study(**kwargs: Any) -> None:
        _record("micro-snapshot-study", **kwargs)

    async def fake_wide_sweep(**kwargs: Any) -> None:
        _record("wide-sweep", **kwargs)

    async def fake_hydrate_authors(**kwargs: Any) -> None:
        _record("hydrate-authors", **kwargs)

    async def fake_backfill_interactions(**kwargs: Any) -> None:
        _record("backfill-interactions", **kwargs)

    async def fake_backfill_rq1_factors(**kwargs: Any) -> None:
        _record("backfill-rq1-factors", **kwargs)

    monkeypatch.setattr("bsky_collector_v2.jobs.public_omnibus.run_seed_post_registry", fake_seed_post_registry)
    monkeypatch.setattr("bsky_collector_v2.jobs.public_omnibus.run_index_feed_generators", fake_index_feed_generators)
    monkeypatch.setattr("bsky_collector_v2.jobs.public_omnibus.run_refresh_discovery", fake_refresh_discovery)
    monkeypatch.setattr("bsky_collector_v2.jobs.public_omnibus.run_build_panel", fake_build_panel)
    monkeypatch.setattr("bsky_collector_v2.jobs.public_omnibus.run_hydrate_feed_generators", fake_hydrate_feed_generators)
    monkeypatch.setattr("bsky_collector_v2.jobs.public_omnibus.run_snapshot_panel", fake_snapshot_panel)
    monkeypatch.setattr("bsky_collector_v2.jobs.public_omnibus.run_micro_snapshot_study", fake_micro_snapshot_study)
    monkeypatch.setattr("bsky_collector_v2.jobs.public_omnibus.run_wide_sweep", fake_wide_sweep)
    monkeypatch.setattr("bsky_collector_v2.jobs.public_omnibus.run_hydrate_authors", fake_hydrate_authors)
    monkeypatch.setattr("bsky_collector_v2.jobs.public_omnibus.run_backfill_interactions", fake_backfill_interactions)
    monkeypatch.setattr("bsky_collector_v2.jobs.public_omnibus.run_backfill_rq1_factors", fake_backfill_rq1_factors)

    summary = asyncio.run(
        run_public_omnibus(
            layout=layout,
            hosts=XrpcHosts(appview_host="https://public.example", pds_host="https://pds.example"),
            relay_host="https://relay.example",
            run_id="public-omnibus-run-000",
            rps=12.5,
            concurrency=9,
            posts_per_feed=33,
            time_budget_minutes=44,
            feed_time_budget_s=11.0,
            dry_run=False,
            resume=True,
            accept_language="en-US",
            accept_labelers="did:plc:labeler000",
            include_author_labels=True,
            vantage_id_unauth="unauth-public",
            cfg=PublicOmnibusConfig(),
        )
    )

    call_names = [name for name, _kwargs in calls]
    assert call_names == [
        "seed-post-registry",
        "index-feed-generators",
        "refresh-discovery",
        "build-panel",
        "hydrate-feed-generators",
        "snapshot-panel",
        "micro-snapshot-study",
        "wide-sweep",
        "hydrate-authors",
        "backfill-interactions",
        "backfill-rq1-factors",
    ]

    by_name = {name: kwargs for name, kwargs in calls}
    assert by_name["index-feed-generators"]["env_path"] is None
    assert by_name["index-feed-generators"]["relay_host"] == "https://relay.example"
    assert by_name["refresh-discovery"]["env_path"] is None
    assert by_name["refresh-discovery"]["viewer_modes"] == ("unauth",)
    assert by_name["refresh-discovery"]["vantage_id_auth"] == "auth-disabled"
    assert by_name["snapshot-panel"]["env_path"] is None
    assert by_name["snapshot-panel"]["viewer_modes"] == ("unauth",)
    assert by_name["snapshot-panel"]["vantage_id_auth"] == "auth-disabled"
    assert by_name["micro-snapshot-study"]["env_path"] is None
    assert by_name["micro-snapshot-study"]["public_only"] is True
    assert by_name["micro-snapshot-study"]["study_id"] == "studyA"
    assert by_name["wide-sweep"]["vantage_id"] == "unauth-public"
    assert by_name["hydrate-authors"]["cfg"].max_authors == 200_000
    assert by_name["backfill-interactions"]["cfg"].include_hydrated is False
    assert by_name["backfill-rq1-factors"]["cfg"].include_hydrated is False
    assert by_name["backfill-rq1-factors"]["cfg"].stage == "core"
    assert summary.success is True
    assert summary.studies_run == ["studyA"]
    assert [item["step"] for item in summary.step_results] == [
        "seed-post-registry",
        "index-feed-generators",
        "refresh-discovery",
        "build-panel",
        "hydrate-feed-generators",
        "snapshot-panel",
        "micro-snapshot-study:studyA",
        "wide-sweep",
        "hydrate-authors",
        "backfill-interactions",
        "backfill-rq1-factors",
    ]
