from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse


def _json_bytes(obj: Any) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


def _did_slug(did: str) -> str:
    return did.replace("did:plc:", "").replace("did:web:", "").replace(":", "-")


def _actor_profile(did: str, *, idx: int = 0, detailed: bool = True) -> dict[str, Any]:
    slug = _did_slug(did)
    base = {
        "did": did,
        "handle": f"{slug}.test",
        "displayName": slug.title(),
        "description": f"profile for {slug}",
        "avatar": f"https://img.test/{slug}.png",
        "associated": {"lists": 1, "feedgens": 1, "starterPacks": 1, "labeler": False},
        "indexedAt": "2026-03-31T00:00:00Z",
        "createdAt": "2025-01-01T00:00:00Z",
        "labels": [{"src": "did:plc:labeler000", "val": "safe"}],
        "verification": {"verifiedStatus": "none"},
        "status": {"status": "active"},
        "viewer": {"muted": False, "blockedBy": False},
    }
    if detailed:
        base.update(
            {
                "website": f"https://{slug}.example.com",
                "banner": f"https://img.test/{slug}-banner.png",
                "followersCount": 100 + idx,
                "followsCount": 50 + idx,
                "postsCount": 20 + idx,
                "joinedViaStarterPack": {
                    "uri": f"at://did:plc:starterowner/app.bsky.graph.starterpack/{slug}-pack",
                    "cid": f"cid-pack-{slug}",
                    "record": {"createdAt": "2025-01-02T00:00:00Z"},
                    "creator": {"did": "did:plc:starterowner", "handle": "starterowner.test", "displayName": "Starter Owner"},
                    "indexedAt": "2026-03-31T00:00:00Z",
                },
                "pinnedPost": {"uri": f"at://{did}/app.bsky.feed.post/pinned"},
            }
        )
    return base


def _post_view(uri: str, *, idx: int = 0, text: str | None = None, author_did: str | None = None) -> dict[str, Any]:
    author_did = author_did or uri.removeprefix("at://").split("/")[0]
    slug = _did_slug(author_did)
    text = text or f"post {idx} from {slug}"
    return {
        "uri": uri,
        "cid": f"cid-{slug}-{idx}",
        "author": {
            "did": author_did,
            "handle": f"{slug}.test",
            "displayName": slug.title(),
            "labels": [{"src": "did:plc:labeler000", "val": "!no-unauthenticated"}],
        },
        "record": {
            "text": text,
            "createdAt": f"2026-03-3{idx % 2}T00:00:0{idx % 10}Z",
            "langs": ["en", "zh"],
            "tags": ["rq1", "bluesky"],
            "facets": [
                {"features": [{"$type": "app.bsky.richtext.facet#mention"}]},
                {"features": [{"$type": "app.bsky.richtext.facet#link"}]},
                {"features": [{"$type": "app.bsky.richtext.facet#tag"}]},
            ],
            "labels": {"values": [{"val": "!no-unauthenticated"}]},
            "reply": {
                "root": {"uri": f"at://did:plc:threadroot/app.bsky.feed.post/root{idx}"},
                "parent": {"uri": f"at://did:plc:threadparent/app.bsky.feed.post/parent{idx}"},
            },
        },
        "embed": {
            "$type": "app.bsky.embed.recordWithMedia#view",
            "media": {
                "$type": "app.bsky.embed.external#view",
                "external": {"uri": "https://example.com/story"},
            },
        },
        "indexedAt": "2026-03-31T00:00:00Z",
        "replyCount": 3,
        "repostCount": 4,
        "likeCount": 5,
        "quoteCount": 2,
        "labels": [{"src": "did:plc:labeler000", "val": "!hide"}],
        "viewer": {"like": None},
        "threadgate": {"allow": ["followers"]},
        "debug": {"score": 0.42},
    }


def _feed_item(feed_uri: str, index: int) -> dict[str, Any]:
    author_did = f"did:plc:author{index:03d}"
    post_uri = f"at://{author_did}/app.bsky.feed.post/post{index:03d}"
    return {
        "post": _post_view(post_uri, idx=index, author_did=author_did),
        "reason": {
            "$type": "app.bsky.feed.defs#reasonRepost",
            "by": _actor_profile(f"did:plc:reposter{index:03d}", detailed=False),
            "uri": f"at://did:plc:reposter{index:03d}/app.bsky.feed.repost/repost{index:03d}",
            "cid": f"cid-repost-{index:03d}",
            "indexedAt": "2026-03-31T00:00:00Z",
        },
        "reply": {
            "root": {"uri": f"at://did:plc:root{index:03d}/app.bsky.feed.post/root{index:03d}"},
            "parent": {"uri": f"at://did:plc:parent{index:03d}/app.bsky.feed.post/parent{index:03d}"},
            "grandparentAuthor": {"did": f"did:plc:grand{index:03d}"},
        },
        "feedContext": f"context-{index}",
        "reqId": f"req-{index}",
    }


def _generator_view(feed_uri: str) -> dict[str, Any]:
    did = feed_uri.removeprefix("at://").split("/")[0]
    slug = _did_slug(did)
    return {
        "uri": feed_uri,
        "cid": f"cid-feed-{slug}",
        "did": "did:web:feeds.example.com",
        "creator": _actor_profile(did, detailed=False),
        "displayName": f"Feed {slug}",
        "description": f"generator for {slug}",
        "avatar": f"https://img.test/feed-{slug}.png",
        "likeCount": 77,
        "acceptsInteractions": True,
        "contentMode": "app.bsky.feed.defs#contentModeUnspecified",
        "indexedAt": "2026-03-31T00:00:00Z",
        "labels": [{"src": "did:plc:labeler000", "val": "safe"}],
    }


def _list_view(actor_did: str, suffix: str = "list0") -> dict[str, Any]:
    return {
        "uri": f"at://{actor_did}/app.bsky.graph.list/{suffix}",
        "cid": f"cid-{suffix}",
        "creator": _actor_profile(actor_did, detailed=False),
        "name": f"List {suffix}",
        "purpose": "app.bsky.graph.defs#curatelist",
        "description": f"desc {suffix}",
        "avatar": "https://img.test/list.png",
        "labels": [{"src": "did:plc:labeler000", "val": "safe"}],
        "viewer": {"muted": False},
        "indexedAt": "2026-03-31T00:00:00Z",
    }


def _starter_pack_view(actor_did: str, suffix: str = "pack0") -> dict[str, Any]:
    return {
        "uri": f"at://{actor_did}/app.bsky.graph.starterpack/{suffix}",
        "cid": f"cid-{suffix}",
        "record": {"createdAt": "2026-03-31T00:00:00Z"},
        "creator": _actor_profile(actor_did, detailed=False),
        "list": {"uri": f"at://{actor_did}/app.bsky.graph.list/{suffix}-list", "cid": f"cid-{suffix}-list", "name": "Starter List", "purpose": "app.bsky.graph.defs#referencelist"},
        "feeds": [{"uri": f"at://{actor_did}/app.bsky.feed.generator/{suffix}-feed"}],
        "joinedWeekCount": 10,
        "joinedAllTimeCount": 100,
        "labels": [{"src": "did:plc:labeler000", "val": "safe"}],
        "indexedAt": "2026-03-31T00:00:00Z",
    }


@dataclass(frozen=True)
class FakeBskyRq1Config:
    delay_by_path_s: dict[str, float] = field(default_factory=dict)


class FakeBskyRq1Server:
    def __init__(self, cfg: FakeBskyRq1Config | None = None) -> None:
        self.cfg = cfg or FakeBskyRq1Config()
        self.request_log: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        handler_cls = self._make_handler()
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def _make_handler(self):  # noqa: ANN201
        request_log = self.request_log
        lock = self._lock
        delay_by_path_s = dict(self.cfg.delay_by_path_s)

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: object) -> None:  # noqa: A003
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
                parsed = urlparse(self.path)
                qs = parse_qs(parsed.query)
                ts = time.monotonic()
                with lock:
                    request_log.append({"method": "GET", "path": parsed.path, "query": {k: list(v) for k, v in qs.items()}, "ts": ts})

                path = parsed.path
                delay_s = float(delay_by_path_s.get(path, 0.0) or 0.0)
                if delay_s > 0:
                    time.sleep(delay_s)
                if path.endswith("/xrpc/app.bsky.actor.getProfiles"):
                    actors = qs.get("actors") or []
                    self._send_json(200, {"profiles": [_actor_profile(did, idx=i) for i, did in enumerate(actors)]})
                    return
                if path.endswith("/xrpc/app.bsky.feed.getPosts"):
                    uris = qs.get("uris") or []
                    self._send_json(200, {"posts": [_post_view(uri, idx=i) for i, uri in enumerate(uris)]}, extra_headers={"atproto-content-labelers": "did:plc:labeler000"})
                    return
                if path.endswith("/xrpc/app.bsky.feed.getLikes"):
                    uri = (qs.get("uri") or [""])[0]
                    likes = []
                    for i in range(2):
                        likes.append({
                            "actor": _actor_profile(f"did:plc:liker{i:03d}", detailed=False),
                            "createdAt": f"2026-03-31T00:00:0{i}Z",
                            "indexedAt": f"2026-03-31T00:00:1{i}Z",
                        })
                    self._send_json(200, {"uri": uri, "likes": likes})
                    return
                if path.endswith("/xrpc/app.bsky.feed.getQuotes"):
                    uri = (qs.get("uri") or [""])[0]
                    quotes = []
                    for i in range(2):
                        q_author = f"did:plc:quoter{i:03d}"
                        q_uri = f"at://{q_author}/app.bsky.feed.post/quote{i:03d}"
                        quotes.append(_post_view(q_uri, idx=i, author_did=q_author, text=f"quote {i} for {uri}"))
                    self._send_json(200, {"posts": quotes})
                    return
                if path.endswith("/xrpc/app.bsky.feed.getRepostedBy"):
                    actors = [_actor_profile(f"did:plc:reposted{i:03d}", detailed=False) for i in range(2)]
                    self._send_json(200, {"repostedBy": actors})
                    return
                if path.endswith("/xrpc/app.bsky.feed.getPostThread"):
                    uri = (qs.get("uri") or [""])[0]
                    focus = _post_view(uri, idx=1)
                    parent_uri = focus["record"]["reply"]["parent"]["uri"]
                    reply_uri = f"at://did:plc:reply000/app.bsky.feed.post/reply000"
                    thread = {
                        "$type": "app.bsky.feed.defs#threadViewPost",
                        "post": focus,
                        "parent": {"$type": "app.bsky.feed.defs#threadViewPost", "post": _post_view(parent_uri, idx=2, author_did="did:plc:threadparent")},
                        "replies": [{"$type": "app.bsky.feed.defs#threadViewPost", "post": _post_view(reply_uri, idx=3, author_did="did:plc:reply000")}],
                    }
                    self._send_json(200, {"thread": thread})
                    return
                if path.endswith("/xrpc/app.bsky.graph.getFollowers"):
                    actor = (qs.get("actor") or [""])[0]
                    followers = [_actor_profile(f"did:plc:{_did_slug(actor)}-follower{i}", detailed=False) for i in range(2)]
                    self._send_json(200, {"subject": _actor_profile(actor, detailed=False), "followers": followers})
                    return
                if path.endswith("/xrpc/app.bsky.graph.getFollows"):
                    actor = (qs.get("actor") or [""])[0]
                    follows = [_actor_profile(f"did:plc:{_did_slug(actor)}-follow{i}", detailed=False) for i in range(2)]
                    self._send_json(200, {"subject": _actor_profile(actor, detailed=False), "follows": follows})
                    return
                if path.endswith("/xrpc/app.bsky.graph.getRelationships"):
                    actor = (qs.get("actor") or [""])[0]
                    others = qs.get("others") or []
                    rels = []
                    for idx, other in enumerate(others):
                        rels.append({
                            "did": other,
                            "following": f"at://{actor}/app.bsky.graph.follow/follow-{idx}",
                            "followedBy": f"at://{other}/app.bsky.graph.follow/followback-{idx}",
                            "blocking": None,
                            "blockedBy": None,
                            "blockingByList": None,
                            "blockedByList": None,
                        })
                    self._send_json(200, {"actor": actor, "relationships": rels})
                    return
                if path.endswith("/xrpc/app.bsky.feed.getAuthorFeed"):
                    actor = (qs.get("actor") or [""])[0]
                    items = [_feed_item(f"at://{actor}/app.bsky.feed.generator/feed0", i) for i in range(2)]
                    self._send_json(200, {"feed": items})
                    return
                if path.endswith("/xrpc/app.bsky.feed.getActorFeeds"):
                    actor = (qs.get("actor") or [""])[0]
                    feeds = [_generator_view(f"at://{actor}/app.bsky.feed.generator/feed{i}") for i in range(2)]
                    self._send_json(200, {"feeds": feeds})
                    return
                if path.endswith("/xrpc/app.bsky.graph.getLists"):
                    actor = (qs.get("actor") or [""])[0]
                    self._send_json(200, {"lists": [_list_view(actor, "list0")]})
                    return
                if path.endswith("/xrpc/app.bsky.graph.getList"):
                    list_uri = (qs.get("list") or [""])[0]
                    actor = list_uri.removeprefix("at://").split("/")[0] or "did:plc:listowner"
                    items = [{"uri": f"at://{actor}/app.bsky.graph.listitem/item{i}", "subject": _actor_profile(f"did:plc:listmember{i}", detailed=False)} for i in range(2)]
                    self._send_json(200, {"list": _list_view(actor, list_uri.split('/')[-1]), "items": items})
                    return
                if path.endswith("/xrpc/app.bsky.graph.getActorStarterPacks"):
                    actor = (qs.get("actor") or [""])[0]
                    self._send_json(200, {"starterPacks": [_starter_pack_view(actor, "pack0")]})
                    return
                if path.endswith("/xrpc/app.bsky.graph.getStarterPack"):
                    starter_pack_uri = (qs.get("starterPack") or [""])[0]
                    actor = starter_pack_uri.removeprefix("at://").split("/")[0] or "did:plc:starterowner"
                    self._send_json(200, {"starterPack": _starter_pack_view(actor, starter_pack_uri.split('/')[-1])})
                    return
                if path.endswith("/xrpc/app.bsky.feed.getFeedGenerator"):
                    feed_uri = (qs.get("feed") or [""])[0]
                    self._send_json(200, {"view": _generator_view(feed_uri), "isOnline": True, "isValid": True}, extra_headers={"atproto-content-labelers": "did:plc:labeler000"})
                    return
                if path.endswith("/xrpc/app.bsky.labeler.getServices"):
                    dids = qs.get("dids") or []
                    views = []
                    for did in dids:
                        views.append({
                            "uri": f"at://{did}/app.bsky.labeler.service/self",
                            "cid": f"cid-{_did_slug(did)}",
                            "creator": _actor_profile(did, detailed=False),
                            "policies": {"labelValues": ["safe", "!hide"]},
                            "indexedAt": "2026-03-31T00:00:00Z",
                            "labels": [{"src": did, "val": "safe"}],
                        })
                    self._send_json(200, {"views": views})
                    return
                if path.endswith("/xrpc/com.atproto.repo.describeRepo"):
                    repo = (qs.get("repo") or [""])[0]
                    self._send_json(200, {"handle": f"{_did_slug(repo)}.test", "did": repo, "didDoc": {"id": repo}, "collections": ["app.bsky.graph.follow", "app.bsky.feed.post"], "handleIsCorrect": True})
                    return
                if path.endswith("/xrpc/com.atproto.repo.listRecords"):
                    repo = (qs.get("repo") or [""])[0]
                    collection = (qs.get("collection") or [""])[0]
                    if collection == "app.bsky.graph.follow":
                        records = []
                        for i in range(2):
                            records.append({
                                "uri": f"at://{repo}/app.bsky.graph.follow/follow{i}",
                                "cid": f"cid-follow-{i}",
                                "value": {"$type": "app.bsky.graph.follow", "subject": f"did:plc:{_did_slug(repo)}-target{i}", "createdAt": f"2026-03-3{i}T00:00:00Z"},
                            })
                        self._send_json(200, {"records": records})
                        return
                    self._send_json(200, {"records": []})
                    return
                if path.endswith("/xrpc/com.atproto.server.createSession"):
                    self._send_json(200, {"accessJwt": "access", "refreshJwt": "refresh", "did": "did:plc:testviewer", "handle": "testviewer.test"})
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

    def __enter__(self) -> "FakeBskyRq1Server":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        self.close()
