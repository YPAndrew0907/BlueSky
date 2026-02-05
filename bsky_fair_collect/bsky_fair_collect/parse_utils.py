from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse


@dataclass(frozen=True)
class AtUri:
    raw: str
    did: str
    collection: str
    rkey: str


def parse_at_uri(uri: str) -> AtUri:
    # Expected: at://<did>/<collection>/<rkey>
    if not uri.startswith("at://"):
        raise ValueError(f"invalid at-uri (missing at://): {uri}")
    parts = uri[len("at://") :].split("/")
    if len(parts) < 3:
        raise ValueError(f"invalid at-uri (expected 3 parts): {uri}")
    did, collection, rkey = parts[0], parts[1], parts[2]
    if not did or not collection or not rkey:
        raise ValueError(f"invalid at-uri (empty parts): {uri}")
    return AtUri(raw=uri, did=did, collection=collection, rkey=rkey)


def provider_bucket_from_service_did(service_did: str | None) -> str:
    if not service_did:
        return "unknown"
    if service_did.startswith("did:web:"):
        return service_did[len("did:web:") :]
    return "plc_bucket"


def domain_from_url(url: str | None) -> str | None:
    if not url:
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    hostname = parsed.hostname
    if not hostname:
        return None
    return hostname.lower()


def json_dumps_compact(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

