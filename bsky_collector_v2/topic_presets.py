from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TopicSpec:
    slug: str
    label: str
    include_queries: tuple[str, ...]
    exclude_queries: tuple[str, ...] = ()
    cluster_exclude_text_patterns: tuple[str, ...] = ()
    cluster_anchor_kinds: tuple[str, ...] = ("tokens",)
    notes: str = ""


POLITICS_V1: tuple[TopicSpec, ...] = (
    TopicSpec(
        slug="epstein",
        label="Epstein Files",
        include_queries=(r"\bepstein\b",),
        exclude_queries=(r"epsteinweb",),
        cluster_exclude_text_patterns=(),
        cluster_anchor_kinds=("tokens",),
        notes="Example topic; useful for stress-testing noisy retrieval and frame separation.",
    ),
    TopicSpec(
        slug="gaza",
        label="Gaza",
        include_queries=(r"\bgaza\b", r"\bpalestin(?:e|ian)\b"),
        exclude_queries=(),
        cluster_exclude_text_patterns=(),
        cluster_anchor_kinds=("tokens",),
        notes="International conflict topic with multiple recurring hashtags and link domains.",
    ),
    TopicSpec(
        slug="ukraine",
        label="Ukraine",
        include_queries=(r"\bukraine\b", r"\bzelensky(?:y|i)?\b"),
        exclude_queries=(),
        cluster_exclude_text_patterns=(),
        cluster_anchor_kinds=("tokens",),
        notes="War topic with newswire-heavy and commentary-heavy frames.",
    ),
    TopicSpec(
        slug="immigration",
        label="Immigration",
        include_queries=(r"\bimmigration\b", r"\bdeport(?:ation|ations|ed)?\b", r"\bice\b"),
        exclude_queries=(),
        cluster_exclude_text_patterns=(),
        cluster_anchor_kinds=("tokens",),
        notes="Useful for enforcement, humanitarian, and partisan governance frames.",
    ),
    TopicSpec(
        slug="abortion",
        label="Abortion",
        include_queries=(r"\babortion\b", r"\breproductive rights\b", r"\bpro[- ]?life\b", r"\bpro[- ]?choice\b"),
        exclude_queries=(),
        cluster_exclude_text_patterns=(),
        cluster_anchor_kinds=("tokens",),
        notes="Likely to produce legal, healthcare, and activist frames.",
    ),
    TopicSpec(
        slug="climate",
        label="Climate",
        include_queries=(r"\bclimate\b", r"\bclimate crisis\b", r"\bglobal warming\b"),
        exclude_queries=(r"\bclimate report\b", r"\biembot\b"),
        cluster_exclude_text_patterns=(r"\bclimate report\b", r"\biembot\b"),
        cluster_anchor_kinds=("tokens",),
        notes="Can mix policy, disaster, and scientific-reporting frames.",
    ),
    TopicSpec(
        slug="iran",
        label="Iran",
        include_queries=(r"\biran\b", r"\btehran\b"),
        exclude_queries=(),
        cluster_exclude_text_patterns=(),
        cluster_anchor_kinds=("tokens",),
        notes="Important because it co-occurs with several current U.S. conflict narratives in the archive.",
    ),
    TopicSpec(
        slug="tariffs",
        label="Tariffs and Trade",
        include_queries=(r"\btariff(?:s)?\b", r"\btrade war\b", r"\bimport taxes\b"),
        exclude_queries=(),
        cluster_exclude_text_patterns=(),
        cluster_anchor_kinds=("tokens",),
        notes="Economic-policy topic that may map onto inflation and business frames.",
    ),
)


TOPIC_PRESETS: dict[str, tuple[TopicSpec, ...]] = {
    "politics_v1": POLITICS_V1,
}


def get_topic_preset(name: str) -> tuple[TopicSpec, ...]:
    preset = TOPIC_PRESETS.get(name)
    if preset is None:
        available = ", ".join(sorted(TOPIC_PRESETS))
        raise ValueError(f"unknown topic preset: {name}. available={available}")
    return preset
