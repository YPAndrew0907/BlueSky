from __future__ import annotations

RQ1_STAGE_CORE = "core"
RQ1_STAGE_GRAPH = "graph"
RQ1_STAGE_REPO = "repo"
RQ1_STAGE_ALL = "all"

RQ1_STAGE_CHOICES: tuple[str, ...] = (
    RQ1_STAGE_CORE,
    RQ1_STAGE_GRAPH,
    RQ1_STAGE_REPO,
    RQ1_STAGE_ALL,
)


def normalize_rq1_stage(value: str | None, *, default: str = RQ1_STAGE_ALL) -> str:
    stage = str(value or default).strip().lower()
    if stage not in RQ1_STAGE_CHOICES:
        raise ValueError(f"invalid rq1 stage {value!r}; expected one of {RQ1_STAGE_CHOICES}")
    return stage


def rq1_stage_includes_graph(stage: str) -> bool:
    return normalize_rq1_stage(stage) in {RQ1_STAGE_GRAPH, RQ1_STAGE_REPO, RQ1_STAGE_ALL}


def rq1_stage_includes_repo(stage: str) -> bool:
    return normalize_rq1_stage(stage) in {RQ1_STAGE_REPO, RQ1_STAGE_ALL}


def rq1_stage_completion_columns(stage: str) -> tuple[str, ...]:
    normalized = normalize_rq1_stage(stage)
    if normalized == RQ1_STAGE_CORE:
        return ("core_hydrated_at_utc",)
    if normalized == RQ1_STAGE_GRAPH:
        return ("core_hydrated_at_utc", "graph_hydrated_at_utc")
    return ("core_hydrated_at_utc", "graph_hydrated_at_utc", "repo_hydrated_at_utc", "last_hydrated_utc")


def rq1_stage_prerequisite_clauses(stage: str) -> tuple[str, ...]:
    normalized = normalize_rq1_stage(stage)
    if normalized == RQ1_STAGE_GRAPH:
        return ("prr.core_hydrated_at_utc IS NOT NULL",)
    if normalized == RQ1_STAGE_REPO:
        return ("prr.graph_hydrated_at_utc IS NOT NULL",)
    return ()


def rq1_stage_pending_column(stage: str) -> str:
    normalized = normalize_rq1_stage(stage)
    if normalized == RQ1_STAGE_CORE:
        return "prr.core_hydrated_at_utc"
    if normalized == RQ1_STAGE_GRAPH:
        return "prr.graph_hydrated_at_utc"
    if normalized == RQ1_STAGE_REPO:
        return "prr.repo_hydrated_at_utc"
    return "prr.last_hydrated_utc"


__all__ = [
    "RQ1_STAGE_ALL",
    "RQ1_STAGE_CHOICES",
    "RQ1_STAGE_CORE",
    "RQ1_STAGE_GRAPH",
    "RQ1_STAGE_REPO",
    "normalize_rq1_stage",
    "rq1_stage_completion_columns",
    "rq1_stage_includes_graph",
    "rq1_stage_includes_repo",
    "rq1_stage_pending_column",
    "rq1_stage_prerequisite_clauses",
]
