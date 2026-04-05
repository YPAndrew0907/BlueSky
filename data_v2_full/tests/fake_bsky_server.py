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


class FakeBskyServer:
    def __init__(self, *, feeds: dict[str, int], cfg: FakeBskyConfig | None = None) -> None:
        self._cfg = cfg or FakeBskyConfig()
        self._feeds = dict(feeds)  # feed_uri -> posts_count

        handler_cls = self._make_handler()
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def _make_handler(self):  # noqa: ANN201
        feeds = self._feeds
        cfg = self._cfg

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

                if parsed.path.endswith("/xrpc/app.bsky.unspecced.getPopularFeedGenerators"):
                    items = []
                    for i, uri in enumerate(sorted(feeds)):
                        creator_did = uri.removeprefix("at://").split("/")[0]
                        items.append(
                            {
                                "uri": uri,
                                "creator": {"did": creator_did},
                                "did": "did:web:example.com",
                                "likeCount": 1_000_000 - i,
                            }
                        )
                    self._send_json(200, {"feeds": items})
                    return

                if parsed.path.endswith("/xrpc/app.bsky.feed.getSuggestedFeeds"):
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
                    actors = [{"did": f"did:plc:suggest{i:03d}"} for i in range(10)]
                    self._send_json(200, {"actors": actors})
                    return

                if parsed.path.endswith("/xrpc/app.bsky.graph.getSuggestedFollowsByActor"):
                    actor = (qs.get("actor") or [""])[0]
                    is_fallback = actor.endswith("999")
                    suggestions = [{"did": f"did:plc:follow{j:03d}"} for j in range(5)]
                    self._send_json(200, {"suggestions": suggestions, "isFallback": is_fallback})
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

                if parsed.path.endswith("/xrpc/app.bsky.feed.getFeed"):
                    feed = (qs.get("feed") or [""])[0]
                    limit_s = (qs.get("limit") or ["50"])[0]
                    try:
                        limit = max(1, min(100, int(limit_s)))
                    except ValueError:
                        limit = 50

                    # Optional auth-required simulation.
                    if "authreq" in feed and not self.headers.get("Authorization"):
                        self._send_json(401, {"error": "AuthRequired", "message": "Authentication required"})
                        return

                    total = int(feeds.get(feed, 0))
                    out = []
                    for i in range(min(limit, total)):
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
                    self._send_json(
                        200,
                        {"feed": out},
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
