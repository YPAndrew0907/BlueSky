from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urlparse

from bsky_collector_v2.time_utils import format_utc, now_utc


@dataclass(frozen=True)
class DidResolutionResult:
    did: str
    pds_endpoint: str | None
    resolution_method: str
    did_doc_url: str | None
    service_endpoint: str | None
    raw_doc: dict[str, Any] | None = None
    error: str | None = None


@dataclass
class DidResolver:
    http: Any
    plc_directory_base: str = "https://plc.directory"
    _cache: dict[str, DidResolutionResult] = field(default_factory=dict)

    async def resolve_pds_endpoint(self, did: str) -> DidResolutionResult:
        did = str(did or "").strip()
        if not did:
            return DidResolutionResult(
                did="",
                pds_endpoint=None,
                resolution_method="invalid_empty_did",
                did_doc_url=None,
                service_endpoint=None,
                raw_doc=None,
                error="empty did",
            )
        cached = self._cache.get(did)
        if cached is not None:
            return cached
        if did.startswith("did:plc:"):
            result = await self._resolve_plc(did)
        elif did.startswith("did:web:"):
            result = await self._resolve_web(did)
        else:
            result = DidResolutionResult(
                did=did,
                pds_endpoint=None,
                resolution_method="unsupported_did_method",
                did_doc_url=None,
                service_endpoint=None,
                raw_doc=None,
                error=f"unsupported DID method for {did}",
            )
        self._cache[did] = result
        return result

    async def _resolve_plc(self, did: str) -> DidResolutionResult:
        url = self.plc_directory_base.rstrip("/") + "/" + did
        return await self._resolve_document(did=did, url=url, method="plc_directory")

    async def _resolve_web(self, did: str) -> DidResolutionResult:
        url = _did_web_document_url(did)
        return await self._resolve_document(did=did, url=url, method="did_web")

    async def _resolve_document(self, *, did: str, url: str, method: str) -> DidResolutionResult:
        timestamp = format_utc(now_utc())
        try:
            resp = await self.http.request_json(
                endpoint="did.resolve",
                method="GET",
                url=url,
                params=None,
                json_body=None,
                headers=None,
                feed_uri=None,
                timestamp_utc=timestamp,
                request_context=None,
            )
            data = resp.data if isinstance(resp.data, dict) else {}
            endpoint = _extract_atproto_pds_endpoint(data)
            return DidResolutionResult(
                did=did,
                pds_endpoint=endpoint,
                resolution_method=method,
                did_doc_url=url,
                service_endpoint=endpoint,
                raw_doc=data,
                error=None if endpoint else "missing atproto PDS service endpoint",
            )
        except Exception as err:  # noqa: BLE001
            return DidResolutionResult(
                did=did,
                pds_endpoint=None,
                resolution_method=method,
                did_doc_url=url,
                service_endpoint=None,
                raw_doc=None,
                error=str(err),
            )


def _did_web_document_url(did: str) -> str:
    payload = did.removeprefix("did:web:")
    parts = [unquote(part) for part in payload.split(":") if part]
    if not parts:
        raise ValueError(f"invalid did:web {did}")
    domain = parts[0]
    if len(parts) == 1:
        return f"https://{domain}/.well-known/did.json"
    return f"https://{domain}/{'/'.join(parts[1:])}/did.json"


def _extract_atproto_pds_endpoint(doc: Any) -> str | None:
    if not isinstance(doc, dict):
        return None
    service_entries = doc.get("service")
    if isinstance(service_entries, dict):
        service_entries = [service_entries]
    if not isinstance(service_entries, list):
        return None
    for entry in service_entries:
        if not isinstance(entry, dict):
            continue
        service_type = entry.get("type")
        service_id = entry.get("id")
        endpoint = entry.get("serviceEndpoint")
        if service_type == "AtprotoPersonalDataServer" or service_id in {"#atproto_pds", "atproto_pds"}:
            if isinstance(endpoint, str) and endpoint:
                return endpoint.rstrip("/")
    return None


__all__ = ["DidResolutionResult", "DidResolver"]
