from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Self
from urllib.parse import urlparse

import httpx

from bsky_collector_v2 import __version__
from bsky_collector_v2.progress import ProgressState
from bsky_collector_v2.request_provenance import (
    RequestContext,
    RequestProvenanceWriter,
    infer_depth_returned,
    response_hash_from_bytes,
)
from bsky_collector_v2.time_utils import format_utc, now_utc
from bsky_collector_v2.writers import CsvPartWriter

logger = logging.getLogger("bsky_collector_v2.http")

_ALLOWED_XRPC_POST_METHODS: frozenset[str] = frozenset(
    {
        "com.atproto.server.createSession",
        "com.atproto.server.refreshSession",
    }
)


class HttpError(RuntimeError):
    def __init__(
        self,
        *,
        endpoint: str,
        method: str,
        url: str,
        status_code: int | None,
        error_type: str,
        message: str,
    ) -> None:
        super().__init__(message)
        self.endpoint = endpoint
        self.method = method
        self.url = url
        self.status_code = status_code
        self.error_type = error_type


@dataclass(frozen=True)
class HttpRetryConfig:
    max_retries: int = 2
    base_backoff_s: float = 0.5
    max_backoff_s: float = 30.0


class AsyncRateLimiter:
    def __init__(self, rps: float) -> None:
        if rps <= 0:
            raise ValueError(f"rps must be > 0, got {rps}")
        self._base_rps = float(rps)
        self._lock = asyncio.Lock()
        self._min_interval_s = 1.0 / float(rps)
        self._next_allowed_s = time.monotonic()
        self._penalty_s = 0.0
        self._last_penalty_update_s = time.monotonic()

    def set_base_rps(self, rps: float) -> None:
        if rps <= 0:
            raise ValueError(f"rps must be > 0, got {rps}")
        self._base_rps = float(rps)
        self._min_interval_s = 1.0 / float(rps)

    def on_error(self) -> None:
        # Increase penalty delay (decays over time).
        self._decay_penalty()
        self._penalty_s = min(10.0, max(self._penalty_s, 0.25) * 1.5)

    def on_success(self) -> None:
        self._decay_penalty()

    def _decay_penalty(self) -> None:
        now = time.monotonic()
        dt = max(0.0, now - self._last_penalty_update_s)
        self._last_penalty_update_s = now
        if self._penalty_s <= 0:
            return
        # Exponential-ish decay: subtract 10% per second.
        decay = 0.9 ** dt
        self._penalty_s *= decay
        if self._penalty_s < 0.01:
            self._penalty_s = 0.0

    async def wait(self) -> None:
        async with self._lock:
            self._decay_penalty()
            now = time.monotonic()
            if now < self._next_allowed_s:
                await asyncio.sleep(self._next_allowed_s - now)
            # penalty is applied in addition to baseline pacing
            if self._penalty_s > 0:
                await asyncio.sleep(self._penalty_s)
            self._next_allowed_s = max(self._next_allowed_s + self._min_interval_s, time.monotonic())


def _xrpc_url(host: str, method: str) -> str:
    return host.rstrip("/") + "/xrpc/" + method


def _compute_backoff_s(cfg: HttpRetryConfig, attempt: int, *, retry_after_s: float | None) -> float:
    if retry_after_s is not None and retry_after_s > 0:
        return retry_after_s + random.uniform(0, 0.25)
    exp = cfg.base_backoff_s * (2**attempt)
    exp = min(exp, cfg.max_backoff_s)
    return exp + random.uniform(0, exp * 0.25)


def _parse_retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    if seconds < 0:
        return None
    return seconds


def _short_http_error(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        body = None
    if isinstance(body, dict):
        err = body.get("error")
        msg = body.get("message")
        if err or msg:
            return f"{err or 'error'}: {msg or ''}".strip()
    text = resp.text
    if len(text) > 200:
        text = text[:200] + "…"
    return f"http {resp.status_code}: {text}"


@dataclass(frozen=True)
class XrpcHosts:
    appview_host: str = "https://public.api.bsky.app"
    pds_host: str = "https://bsky.social"


@dataclass
class AsyncHttpClient:
    hosts: XrpcHosts
    rps: float
    retry: HttpRetryConfig
    timeout_s: float
    http_stats: CsvPartWriter | None
    progress: ProgressState | None = None
    user_agent: str = f"bsky_collector_v2/{__version__}"
    accept_language: str | None = None
    accept_labelers: str | None = None
    request_provenance_writer: RequestProvenanceWriter | None = None
    request_context_factory: Callable[..., RequestContext | None] | None = None

    def __post_init__(self) -> None:
        headers: dict[str, str] = {"User-Agent": self.user_agent}
        if isinstance(self.accept_language, str) and self.accept_language.strip():
            headers["Accept-Language"] = self.accept_language.strip()
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout_s),
            headers=headers,
            follow_redirects=True,
        )
        self._limiters: dict[str, AsyncRateLimiter] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    def _limiter_for_url(self, url: str) -> AsyncRateLimiter:
        host = urlparse(url).netloc
        limiter = self._limiters.get(host)
        if limiter is None:
            limiter = AsyncRateLimiter(rps=self.rps)
            self._limiters[host] = limiter
        return limiter

    def _record_http_stats(
        self,
        *,
        timestamp_utc: str,
        endpoint: str,
        status_code: int | None,
        latency_ms: float,
        attempt: int,
        error_type: str | None,
        feed_uri: str | None,
    ) -> None:
        if self.http_stats is not None:
            self.http_stats.write_rows(
                [
                    {
                        "timestamp_utc": timestamp_utc,
                        "endpoint": endpoint,
                        "status_code": status_code,
                        "latency_ms": round(latency_ms, 3),
                        "attempt": attempt,
                        "error_type": error_type,
                        "feed_uri": feed_uri,
                    }
                ]
            )
        if error_type and self.progress is not None:
            self.progress.incr_http_error(str(status_code) if status_code is not None else error_type)

    @dataclass(frozen=True)
    class XrpcResponse:
        data: dict[str, Any]
        content_labelers: str | None
        request_started_at_utc: str | None = None
        request_finished_at_utc: str | None = None
        request_order_in_window: int | None = None
        request_order_in_sweep: int | None = None
        http_status: int | None = None
        retry_count: int | None = None

        def get_str(self, key: str) -> str | None:
            value = self.data.get(key)
            if isinstance(value, str) and value:
                return value
            return None

        def get_dict(self, key: str) -> dict[str, Any] | None:
            value = self.data.get(key)
            if isinstance(value, dict):
                return value
            return None

        @classmethod
        def from_httpx(
            cls,
            *,
            data: dict[str, Any],
            resp: httpx.Response,
            request_context: RequestContext | None,
            request_started_at_utc: str,
            request_finished_at_utc: str,
            retry_count: int,
        ) -> Self:
            content_labelers = resp.headers.get("atproto-content-labelers")
            if isinstance(content_labelers, str) and content_labelers.strip():
                content_labelers = content_labelers.strip()
            else:
                content_labelers = None
            return cls(
                data=data,
                content_labelers=content_labelers,
                request_started_at_utc=request_started_at_utc,
                request_finished_at_utc=request_finished_at_utc,
                request_order_in_window=(request_context.request_order_in_window if request_context is not None else None),
                request_order_in_sweep=(request_context.request_order_in_sweep if request_context is not None else None),
                http_status=resp.status_code,
                retry_count=retry_count,
            )

    async def request_json(
        self,
        *,
        endpoint: str,
        method: str,
        url: str,
        params: Mapping[str, Any] | None,
        json_body: Mapping[str, Any] | None,
        headers: Mapping[str, str] | None,
        feed_uri: str | None,
        timestamp_utc: str,
        request_context: RequestContext | None,
    ) -> XrpcResponse:
        limiter = self._limiter_for_url(url)
        last_err: Exception | None = None

        for attempt in range(0, self.retry.max_retries + 1):
            await limiter.wait()
            start = time.perf_counter()
            request_started_at_utc = format_utc(now_utc())
            status_code: int | None = None
            error_type: str | None = None

            try:
                resp = await self._client.request(
                    method,
                    url,
                    params=dict(params) if params else None,
                    json=dict(json_body) if json_body else None,
                    headers=dict(headers) if headers else None,
                )
                status_code = resp.status_code
                latency_ms = (time.perf_counter() - start) * 1000.0

                if 200 <= resp.status_code < 300:
                    limiter.on_success()
                    try:
                        data = resp.json()
                    except json.JSONDecodeError as err:
                        error_type = "invalid_json"
                        self._record_http_stats(
                            timestamp_utc=timestamp_utc,
                            endpoint=endpoint,
                            status_code=status_code,
                            latency_ms=latency_ms,
                            attempt=attempt,
                            error_type=error_type,
                            feed_uri=feed_uri,
                        )
                        if request_context is not None and self.request_provenance_writer is not None:
                            self.request_provenance_writer.record(
                                context=request_context,
                                request_started_at_utc=request_started_at_utc,
                                request_finished_at_utc=format_utc(now_utc()),
                                cursor_out=None,
                                depth_returned=None,
                                http_status=status_code,
                                retry_count=attempt,
                                response_hash=response_hash_from_bytes(resp.content),
                                error_class=error_type,
                            )
                        raise HttpError(
                            endpoint=endpoint,
                            method=method,
                            url=str(resp.url),
                            status_code=resp.status_code,
                            error_type=error_type,
                            message=f"invalid json response: {err}",
                        ) from err

                    self._record_http_stats(
                        timestamp_utc=timestamp_utc,
                        endpoint=endpoint,
                        status_code=status_code,
                        latency_ms=latency_ms,
                        attempt=attempt,
                        error_type=None,
                        feed_uri=feed_uri,
                    )
                    request_finished_at_utc = format_utc(now_utc())
                    if request_context is not None and self.request_provenance_writer is not None:
                        self.request_provenance_writer.record(
                            context=request_context,
                            request_started_at_utc=request_started_at_utc,
                            request_finished_at_utc=request_finished_at_utc,
                            cursor_out=str(data.get("cursor")) if isinstance(data.get("cursor"), str) else None,
                            depth_returned=infer_depth_returned(data),
                            http_status=status_code,
                            retry_count=attempt,
                            response_hash=response_hash_from_bytes(resp.content),
                            error_class=None,
                        )
                    return AsyncHttpClient.XrpcResponse.from_httpx(
                        data=data,
                        resp=resp,
                        request_context=request_context,
                        request_started_at_utc=request_started_at_utc,
                        request_finished_at_utc=request_finished_at_utc,
                        retry_count=attempt,
                    )

                retryable = resp.status_code == 429 or 500 <= resp.status_code <= 599
                if retryable and attempt < self.retry.max_retries:
                    limiter.on_error()
                    error_type = f"http_{resp.status_code}"
                    self._record_http_stats(
                        timestamp_utc=timestamp_utc,
                        endpoint=endpoint,
                        status_code=status_code,
                        latency_ms=latency_ms,
                        attempt=attempt,
                        error_type=error_type,
                        feed_uri=feed_uri,
                    )
                    if request_context is not None and self.request_provenance_writer is not None:
                        self.request_provenance_writer.record(
                            context=request_context,
                            request_started_at_utc=request_started_at_utc,
                            request_finished_at_utc=format_utc(now_utc()),
                            cursor_out=None,
                            depth_returned=None,
                            http_status=status_code,
                            retry_count=attempt,
                            response_hash=response_hash_from_bytes(resp.content),
                            error_class=error_type,
                        )
                    retry_after_s = _parse_retry_after_seconds(resp.headers.get("Retry-After"))
                    backoff_s = _compute_backoff_s(self.retry, attempt, retry_after_s=retry_after_s)
                    await asyncio.sleep(backoff_s)
                    continue

                error_type = f"http_{resp.status_code}"
                self._record_http_stats(
                    timestamp_utc=timestamp_utc,
                    endpoint=endpoint,
                    status_code=status_code,
                    latency_ms=latency_ms,
                    attempt=attempt,
                    error_type=error_type,
                    feed_uri=feed_uri,
                )
                if request_context is not None and self.request_provenance_writer is not None:
                    self.request_provenance_writer.record(
                        context=request_context,
                        request_started_at_utc=request_started_at_utc,
                        request_finished_at_utc=format_utc(now_utc()),
                        cursor_out=None,
                        depth_returned=None,
                        http_status=status_code,
                        retry_count=attempt,
                        response_hash=response_hash_from_bytes(resp.content),
                        error_class=error_type,
                    )
                raise HttpError(
                    endpoint=endpoint,
                    method=method,
                    url=str(resp.url),
                    status_code=resp.status_code,
                    error_type=error_type,
                    message=_short_http_error(resp),
                )

            except (httpx.TimeoutException,) as err:
                limiter.on_error()
                last_err = err
                error_type = "timeout"
                latency_ms = (time.perf_counter() - start) * 1000.0
                self._record_http_stats(
                    timestamp_utc=timestamp_utc,
                    endpoint=endpoint,
                    status_code=status_code,
                    latency_ms=latency_ms,
                    attempt=attempt,
                    error_type=error_type,
                    feed_uri=feed_uri,
                )
                if request_context is not None and self.request_provenance_writer is not None:
                    self.request_provenance_writer.record(
                        context=request_context,
                        request_started_at_utc=request_started_at_utc,
                        request_finished_at_utc=format_utc(now_utc()),
                        cursor_out=None,
                        depth_returned=None,
                        http_status=status_code,
                        retry_count=attempt,
                        response_hash=None,
                        error_class=error_type,
                    )
                if attempt >= self.retry.max_retries:
                    break
                backoff_s = _compute_backoff_s(self.retry, attempt, retry_after_s=None)
                await asyncio.sleep(backoff_s)
                continue

            except (httpx.NetworkError,) as err:
                limiter.on_error()
                last_err = err
                error_type = "network"
                latency_ms = (time.perf_counter() - start) * 1000.0
                self._record_http_stats(
                    timestamp_utc=timestamp_utc,
                    endpoint=endpoint,
                    status_code=status_code,
                    latency_ms=latency_ms,
                    attempt=attempt,
                    error_type=error_type,
                    feed_uri=feed_uri,
                )
                if request_context is not None and self.request_provenance_writer is not None:
                    self.request_provenance_writer.record(
                        context=request_context,
                        request_started_at_utc=request_started_at_utc,
                        request_finished_at_utc=format_utc(now_utc()),
                        cursor_out=None,
                        depth_returned=None,
                        http_status=status_code,
                        retry_count=attempt,
                        response_hash=None,
                        error_class=error_type,
                    )
                if attempt >= self.retry.max_retries:
                    break
                backoff_s = _compute_backoff_s(self.retry, attempt, retry_after_s=None)
                await asyncio.sleep(backoff_s)
                continue

        raise HttpError(
            endpoint=endpoint,
            method=method,
            url=url,
            status_code=None,
            error_type="retries_exhausted",
            message=f"exhausted retries: {last_err!r}",
        )

    async def xrpc_get(
        self,
        *,
        endpoint: str,
        host: str,
        method: str,
        params: Mapping[str, Any] | None,
        access_jwt: str | None,
        feed_uri: str | None,
        timestamp_utc: str,
        request_context: RequestContext | None = None,
    ) -> XrpcResponse:
        if request_context is None and self.request_context_factory is not None:
            request_context = self.request_context_factory(
                endpoint=endpoint,
                host=host,
                method=method,
                params=params,
                json_body=None,
                access_jwt=access_jwt,
                feed_uri=feed_uri,
                timestamp_utc=timestamp_utc,
            )
        url = _xrpc_url(host, method)
        headers: dict[str, str] = {}
        if access_jwt:
            headers["Authorization"] = f"Bearer {access_jwt}"
        if isinstance(self.accept_labelers, str) and self.accept_labelers.strip():
            headers["atproto-accept-labelers"] = self.accept_labelers.strip()
        return await self.request_json(
            endpoint=endpoint,
            method="GET",
            url=url,
            params=params,
            json_body=None,
            headers=headers or None,
            feed_uri=feed_uri,
            timestamp_utc=timestamp_utc,
            request_context=request_context,
        )

    async def xrpc_post(
        self,
        *,
        endpoint: str,
        host: str,
        method: str,
        json_body: Mapping[str, Any] | None,
        access_jwt: str | None,
        timestamp_utc: str,
        request_context: RequestContext | None = None,
    ) -> XrpcResponse:
        if request_context is None and self.request_context_factory is not None:
            request_context = self.request_context_factory(
                endpoint=endpoint,
                host=host,
                method=method,
                params=None,
                json_body=json_body,
                access_jwt=access_jwt,
                feed_uri=None,
                timestamp_utc=timestamp_utc,
            )
        if method not in _ALLOWED_XRPC_POST_METHODS:
            raise ValueError(f"refusing to call non-read-only POST endpoint: {method}")
        url = _xrpc_url(host, method)
        headers: dict[str, str] = {}
        if access_jwt:
            headers["Authorization"] = f"Bearer {access_jwt}"
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        return await self.request_json(
            endpoint=endpoint,
            method="POST",
            url=url,
            params=None,
            json_body=json_body,
            headers=headers,
            feed_uri=None,
            timestamp_utc=timestamp_utc,
            request_context=request_context,
        )
