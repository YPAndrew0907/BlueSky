from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse


def _json_bytes(obj: Any) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


@dataclass(frozen=True)
class FakeBskyConfig:
    request_delay_s: float = 0.0
    fail_popular: bool = False
    fail_suggested_feeds: bool = False
    require_viewer_for_suggestions: bool = False
    feed_page_size: int | None = None


class FakeBskyServer:
    def __init__(self, *, feeds: dict[str, int], cfg: FakeBskyConfig | None = None) -> None:
        self._cfg = cfg or FakeBskyConfig()
        self._feeds = dict(feeds)  # feed_uri -> posts_count
        self.request_log: list[dict[str, Any]] = []
        self._request_log_lock = threading.Lock()

        handler_cls = self._make_handler()
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def _make_handler(self):  # noqa: ANN201
        feeds = self._feeds
        cfg = self._cfg
        request_log = self.request_log
        request_log_lock = self._request_log_lock

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: object) -> None:  # noqa: A003
                # Silence default request logging in tests.
                return

            def _send_json(self, code: int, obj: Any, *, extra_headers: dict[str, str] | None = None) -> None:
                payload = _json_bytes(obj)
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                if extra_headers:
                    for k, v in extra_headers.items():
                        self.send_header(str(k), str(v))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self) -> None:  # noqa: N802
                if cfg.request_delay_s:
                    time.sleep(cfg.request_delay_s)

                parsed = urlparse(self.path)
                qs = parse_qs(parsed.query)
                with request_log_lock:
                    request_log.append(
                        {
                            "method": "GET",
                            "path": parsed.path,
                            "query": {key: list(value) for key, value in qs.items()},
                            "authorization": bool(self.headers.get("Authorization")),
                        }
                    )

                if parsed.path.endswith("/xrpc/app.bsky.unspecced.getPopularFeedGenerators"):
                    if cfg.fail_popular:
                        self._send_json(500, {"error": "InternalError", "message": "boom"})
                        return

                    limit_s = (qs.get("limit") or ["100"])[0]
                    cursor_s = (qs.get("cursor") or ["0"])[0]
                    try:
                        limit = max(1, min(100, int(limit_s)))
                    except ValueError:
                        limit = 100
                    try:
                        offset = max(0, int(cursor_s))
                    except ValueError:
                        offset = 0

                    all_uris = sorted(feeds)
                    page = all_uris[offset : offset + limit]
                    items = []
                    for i, uri in enumerate(page):
                        creator_did = uri.removeprefix("at://").split("/")[0]
                        items.append(
                            {
                                "uri": uri,
                                "creator": {"did": creator_did},
                                "did": "did:web:example.com",
                                "likeCount": 1_000_000 - (offset + i),
                            }
                        )
                    out: dict[str, Any] = {"feeds": items}
                    if offset + limit < len(all_uris):
                        out["cursor"] = str(offset + limit)
                    self._send_json(200, out)
                    return

                if parsed.path.endswith("/xrpc/com.atproto.sync.listReposByCollection"):
                    collection = (qs.get("collection") or [""])[0].strip()
                    if collection != "app.bsky.feed.generator":
                        self._send_json(200, {"repos": []})
                        return

                    limit_s = (qs.get("limit") or ["500"])[0]
                    cursor_s = (qs.get("cursor") or ["0"])[0]
                    try:
                        limit = max(1, min(500, int(limit_s)))
                    except ValueError:
                        limit = 500
                    try:
                        offset = max(0, int(cursor_s))
                    except ValueError:
                        offset = 0

                    repo_dids = sorted({u.removeprefix("at://").split("/")[0] for u in feeds})
                    page = repo_dids[offset : offset + limit]
                    out: dict[str, Any] = {"repos": [{"did": did, "active": True} for did in page]}
                    if offset + limit < len(repo_dids):
                        out["cursor"] = str(offset + limit)
                    self._send_json(200, out)
                    return

                if parsed.path.endswith("/xrpc/com.atproto.sync.listRepos"):
                    limit_s = (qs.get("limit") or ["500"])[0]
                    cursor_s = (qs.get("cursor") or ["0"])[0]
                    try:
                        limit = max(1, min(500, int(limit_s)))
                    except ValueError:
                        limit = 500
                    try:
                        offset = max(0, int(cursor_s))
                    except ValueError:
                        offset = 0

                    repo_dids = sorted({u.removeprefix("at://").split("/")[0] for u in feeds})
                    page = repo_dids[offset : offset + limit]
                    out: dict[str, Any] = {"repos": [{"did": did, "active": True} for did in page]}
                    if offset + limit < len(repo_dids):
                        out["cursor"] = str(offset + limit)
                    self._send_json(200, out)
                    return

                if parsed.path.endswith("/xrpc/com.atproto.repo.listRecords"):
                    repo = (qs.get("repo") or [""])[0].strip()
                    collection = (qs.get("collection") or [""])[0].strip()
                    if collection != "app.bsky.feed.generator":
                        self._send_json(200, {"records": []})
                        return

                    limit_s = (qs.get("limit") or ["100"])[0]
                    cursor_s = (qs.get("cursor") or ["0"])[0]
                    try:
                        limit = max(1, min(100, int(limit_s)))
                    except ValueError:
                        limit = 100
                    try:
                        offset = max(0, int(cursor_s))
                    except ValueError:
                        offset = 0

                    repo_feed_uris = sorted(
                        [u for u in feeds if u.startswith(f"at://{repo}/app.bsky.feed.generator/")]
                    )
                    page = repo_feed_uris[offset : offset + limit]
                    records = []
                    for i, uri in enumerate(page):
                        records.append(
                            {
                                "uri": uri,
                                "cid": f"cid{offset+i:06d}",
                                "value": {
                                    "$type": "app.bsky.feed.generator",
                                    "did": "did:web:example.com",
                                    "displayName": uri.split("/")[-1],
                                },
                            }
                        )
                    out2: dict[str, Any] = {"records": records}
                    if offset + limit < len(repo_feed_uris):
                        out2["cursor"] = str(offset + limit)
                    self._send_json(200, out2)
                    return

                if parsed.path.endswith("/xrpc/app.bsky.feed.getSuggestedFeeds"):
                    if cfg.fail_suggested_feeds:
                        self._send_json(500, {"error": "InternalError", "message": "boom"})
                        return
                    items = []
                    for i, uri in enumerate(sorted(feeds)[:25]):
                        creator_did = uri.removeprefix("at://").split("/")[0]
                        items.append(
                            {
                                "uri": uri,
                                "creator": {"did": creator_did},
                                "did": "did:web:example.com",
                                "likeCount": 100_000 - i,
                            }
                        )
                    self._send_json(200, {"feeds": items})
                    return

                if parsed.path.endswith("/xrpc/app.bsky.actor.getSuggestions"):
                    if cfg.require_viewer_for_suggestions and not (qs.get("viewer") or [""])[0].strip():
                        self._send_json(400, {"error": "InvalidRequest", "message": "must pass viewer"})
                        return
                    actors = [{"did": f"did:plc:suggest{i:03d}"} for i in range(10)]
                    self._send_json(200, {"actors": actors})
                    return

                if parsed.path.endswith("/xrpc/app.bsky.graph.getSuggestedFollowsByActor"):
                    actor = (qs.get("actor") or [""])[0]
                    is_fallback = actor.endswith("999")
                    suggestions = [{"did": f"did:plc:follow{j:03d}"} for j in range(5)]
                    self._send_json(200, {"suggestions": suggestions, "isFallback": is_fallback})
                    return

                if parsed.path.endswith("/xrpc/app.bsky.unspecced.getOnboardingSuggestedStarterPacksSkeleton"):
                    packs = []
                    for i in range(2):
                        packs.append(f"at://did:plc:pack{i:03d}/app.bsky.graph.starterpack/pack{i:03d}")
                    self._send_json(200, {"starterPacks": packs})
                    return

                if parsed.path.endswith("/xrpc/app.bsky.unspecced.getOnboardingSuggestedStarterPacks"):
                    packs = []
                    for i in range(2):
                        packs.append(
                            {
                                "uri": f"at://did:plc:pack{i:03d}/app.bsky.graph.starterpack/pack{i:03d}",
                                "creator": {"did": f"did:plc:pack_creator{i:03d}"},
                                "joinedWeekCount": 10 + i,
                                "joinedAllTimeCount": 100 + i,
                            }
                        )
                    self._send_json(200, {"starterPacks": packs})
                    return

                if parsed.path.endswith("/xrpc/app.bsky.graph.getStarterPacks"):
                    uris = qs.get("uris") or []
                    out_packs = []
                    list_uri = "at://did:plc:list000/app.bsky.graph.list/list000"
                    feed_list = [{"uri": u} for u in sorted(feeds)[:3]]
                    for uri in uris:
                        out_packs.append(
                            {
                                "uri": uri,
                                "creator": {"did": "did:plc:pack_creator000"},
                                "feeds": feed_list,
                                "list": {"uri": list_uri},
                            }
                        )
                    self._send_json(200, {"starterPacks": out_packs})
                    return

                if parsed.path.endswith("/xrpc/app.bsky.graph.getStarterPack"):
                    pack_uri = (qs.get("starterPack") or [""])[0]
                    list_uri = "at://did:plc:list000/app.bsky.graph.list/list000"
                    feed_list = [{"uri": u} for u in sorted(feeds)[:3]]
                    self._send_json(
                        200,
                        {
                            "starterPack": {
                                "uri": pack_uri,
                                "creator": {"did": "did:plc:pack_creator000"},
                                "feeds": feed_list,
                                "list": {"uri": list_uri},
                            }
                        },
                    )
                    return

                if parsed.path.endswith("/xrpc/app.bsky.graph.getList"):
                    items = []
                    for i in range(7):
                        items.append({"subject": {"did": f"did:plc:starter{i:03d}"}})
                    self._send_json(200, {"items": items})
                    return

                if parsed.path.endswith("/xrpc/app.bsky.feed.getFeedGenerators"):
                    uris = qs.get("feeds") or []
                    items = []
                    for i, uri in enumerate(uris):
                        if uri not in feeds:
                            continue
                        creator_did = uri.removeprefix("at://").split("/")[0]
                        items.append(
                            {
                                "uri": uri,
                                "creator": {"did": creator_did},
                                "did": "did:web:example.com",
                                "likeCount": 100_000 - i,
                            }
                        )
                    self._send_json(200, {"feeds": items})
                    return

                if parsed.path.endswith("/xrpc/app.bsky.feed.getFeed"):
                    feed = (qs.get("feed") or [""])[0]
                    limit_s = (qs.get("limit") or ["50"])[0]
                    cursor_s = (qs.get("cursor") or ["0"])[0]
                    try:
                        limit = max(1, min(100, int(limit_s)))
                    except ValueError:
                        limit = 50
                    try:
                        cursor = max(0, int(cursor_s))
                    except ValueError:
                        cursor = 0

                    # Simulate real-world discovery feed quirks (e.g., the official "for-you" strict limit handling).
                    if feed == "at://did:plc:3guzzweuqraryl3rdkimjamk/app.bsky.feed.generator/for-you" and limit > 1:
                        self._send_json(400, {"error": "InvalidRequest", "message": "InvalidRequest"})
                        return

                    # Optional auth-required simulation.
                    if "authreq" in feed and not self.headers.get("Authorization"):
                        self._send_json(401, {"error": "AuthRequired", "message": "Authentication required"})
                        return

                    total = int(feeds.get(feed, 0))
                    page_limit = int(cfg.feed_page_size) if cfg.feed_page_size is not None else limit
                    page_limit = max(1, min(limit, page_limit))
                    out = []
                    start_idx = min(cursor, total)
                    end_idx = min(total, start_idx + page_limit)
                    for i in range(start_idx, end_idx):
                        author_did = f"did:plc:author{i%5:03d}"
                        post_uri = f"at://{author_did}/app.bsky.feed.post/post{i:06d}"
                        out.append(
                            {
                                "post": {
                                    "uri": post_uri,
                                    "cid": f"cid{i:06d}",
                                    "author": {"did": author_did, "handle": f"user{i%5:03d}.test"},
                                    "record": {"text": f"hello {i}", "createdAt": "2026-02-13T00:00:00Z"},
                                    "indexedAt": "2026-02-13T00:00:00Z",
                                    "likeCount": i,
                                    "repostCount": 0,
                                    "replyCount": 0,
                                    "quoteCount": 0,
                                }
                            }
                        )
                    payload: dict[str, Any] = {"feed": out}
                    if end_idx < total:
                        payload["cursor"] = str(end_idx)
                    self._send_json(
                        200,
                        payload,
                        extra_headers={"atproto-content-labelers": "did:plc:labeler000"},
                    )
                    return

                if parsed.path.endswith("/xrpc/app.bsky.actor.getProfile"):
                    self._send_json(200, {"did": "did:plc:bsky", "handle": "bsky.app"})
                    return

                if parsed.path.endswith("/xrpc/app.bsky.actor.getProfiles"):
                    actors = qs.get("actors") or []
                    profiles = []
                    for did in actors:
                        profiles.append(
                            {
                                "did": did,
                                "handle": did.replace("did:plc:", "") + ".test",
                                "displayName": "Test",
                                "followersCount": 1,
                                "followsCount": 2,
                                "postsCount": 3,
                            }
                        )
                    self._send_json(200, {"profiles": profiles})
                    return

                self._send_json(404, {"error": "NotFound", "message": "not found"})

            def do_POST(self) -> None:  # noqa: N802
                if cfg.request_delay_s:
                    time.sleep(cfg.request_delay_s)

                parsed = urlparse(self.path)
                with request_log_lock:
                    request_log.append(
                        {
                            "method": "POST",
                            "path": parsed.path,
                            "query": {},
                            "authorization": bool(self.headers.get("Authorization")),
                        }
                    )
                if parsed.path.endswith("/xrpc/com.atproto.server.createSession"):
                    self._send_json(
                        200,
                        {
                            "accessJwt": "access",
                            "refreshJwt": "refresh",
                            "did": "did:plc:testviewer",
                            "handle": "testviewer.test",
                        },
                    )
                    return
                self._send_json(404, {"error": "NotFound", "message": "not found"})

        return Handler

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address
        return f"http://{host}:{port}"

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def __enter__(self) -> "FakeBskyServer":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        self.close()
