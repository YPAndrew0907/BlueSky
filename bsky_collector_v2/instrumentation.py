from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, MutableMapping

from bsky_collector_v2 import __version__
from bsky_collector_v2.study import file_sha256, read_panel_rows

SCHEMA_VERSION = "2026-03-17.1"
REQUEST_PROVENANCE_VERSION = "2026-03-17.1"
QUALITY_REPORT_VERSION = "2026-03-17.1"


@dataclass(frozen=True)
class PanelMetadata:
    panel_version_id: str | None
    row_count: int
    panel_hash: str | None


def _normalize_for_json(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): _normalize_for_json(v) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple, set)):
        return [_normalize_for_json(v) for v in value]
    return repr(value)


def stable_json_dumps(value: Any) -> str:
    return json.dumps(_normalize_for_json(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def collection_params_hash(params: Mapping[str, Any]) -> str:
    payload = stable_json_dumps(dict(params))
    import hashlib

    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def read_panel_metadata(panel_csv: Path) -> PanelMetadata:
    if not panel_csv.exists():
        return PanelMetadata(panel_version_id=None, row_count=0, panel_hash=None)

    rows = read_panel_rows(panel_csv)
    version_ids = sorted(
        {
            str(row.get("panel_version_id") or "").strip()
            for row in rows
            if str(row.get("panel_version_id") or "").strip()
        }
    )
    chosen_version_id = sorted(version_ids)[-1] if version_ids else None
    return PanelMetadata(
        panel_version_id=chosen_version_id,
        row_count=len(rows),
        panel_hash=file_sha256(panel_csv),
    )


def infer_sample_family(
    *,
    job_name: str,
    out_base: Path,
    accept_labelers: str | None = None,
) -> str:
    parts_lower = {part.lower() for part in out_base.parts}
    is_labelerexp = "labelerexp" in parts_lower
    has_explicit_labelers = isinstance(accept_labelers, str) and bool(accept_labelers.strip())

    if job_name == "snapshot-panel":
        if is_labelerexp or has_explicit_labelers:
            return "experimental_labelerexp_hourly"
        return "regular_hourly"
    if job_name == "wide-sweep":
        return "wide"
    if job_name == "refresh-discovery":
        return "discovery_metadata"
    if job_name == "index-feed-generators":
        return "feed_generator_index"
    if job_name == "hydrate-authors":
        return "author_profile_hydration"
    if job_name == "build-panel":
        return "panel_construction"
    if job_name == "build-labelerexp-panel":
        return "experimental_panel_construction"
    return job_name.replace("_", "-")


def enrich_manifest(
    manifest: MutableMapping[str, Any],
    *,
    job_name: str,
    out_base: Path,
    params: Mapping[str, Any],
    panel_version_id: str | None = None,
    panel_hash: str | None = None,
    sample_family_override: str | None = None,
    study_id: str | None = None,
) -> MutableMapping[str, Any]:
    params_dict = dict(params)
    accept_labelers_raw = params_dict.get("accept_labelers")
    accept_labelers = str(accept_labelers_raw).strip() if isinstance(accept_labelers_raw, str) else None
    manifest["sample_family"] = str(sample_family_override or infer_sample_family(
        job_name=job_name,
        out_base=out_base,
        accept_labelers=accept_labelers,
    ))
    manifest["collection_params_hash"] = collection_params_hash(params_dict)
    manifest["collector_version"] = __version__
    manifest["schema_version"] = SCHEMA_VERSION
    manifest["request_provenance_version"] = REQUEST_PROVENANCE_VERSION
    manifest["quality_report_version"] = QUALITY_REPORT_VERSION
    if panel_version_id is not None:
        manifest["panel_version_id"] = panel_version_id
    if panel_hash is not None:
        manifest["panel_hash"] = panel_hash
    if study_id is not None:
        manifest["study_id"] = study_id
    if manifest["sample_family"] == "experimental_labelerexp_hourly":
        manifest["metadata_source_out_base"] = str(out_base.parent)
    return manifest
