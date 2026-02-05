from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass
from typing import Any

import httpx

from bsky_fair_collect.state import StateDB

logger = logging.getLogger("bsky_fair_collect.http")

_ALLOWED_POST_METHODS: frozenset[str] = frozenset(
    {
        # Session endpoints are required for authenticated read-only viewer context.
        "com.atproto.server.createSession",
        "com.atproto.server.refreshSession",
    }
)


class HttpError(RuntimeError):
    def __init__(
        self,
        *,
        endpoint_name: str,
        method: str,
        url: str,
        status_code: int | None,
        message: str,
    ) -> None:
        super().__init__(message)
        self.endpoint_name = endpoint_name
        self.method = method
        self.url = url
        self.status_code = status_code


class RateLimiter:
    def __init__(self, rps: float) -> None:
        if rps <= 0:
            raise ValueError(f"rps must be > 0, got {rps}")
        self._min_interval = 1.0 / rps
        self._next_allowed = time.monotonic()

    def wait(self) -> None:
        now = time.monotonic()
        if now < self._next_allowed:
            time.sleep(self._next_allowed - now)
        self._next_allowed = max(self._next_allowed + self._min_interval, time.monotonic())


@dataclass(frozen=True)
class HttpRetryConfig:
    max_retries: int
    base_backoff_s: float = 0.5
    max_backoff_s: float = 30.0


_LATENCY_BINS_MS: tuple[float, ...] = (
    50,
    100,
    200,
    400,
    800,
    1500,
    3000,
    6000,
    12000,
    30000,
)


def _bin_upper_ms(latency_ms: float) -> float:
    for upper in _LATENCY_BINS_MS:
        if latency_ms <= upper:
            return float(upper)
    return float(_LATENCY_BINS_MS[-1])


class HttpClient:
    def __init__(
        self,
        *,
        state: StateDB,
        rps: float,
        retry: HttpRetryConfig,
        timeout_s: float = 30.0,
        user_agent: str = "bsky_fair_collect/0.1.0",
    ) -> None:
        self._state = state
        self._limiter = RateLimiter(rps=rps)
        self._retry = retry
        self._client = httpx.Client(
            timeout=httpx.Timeout(timeout_s),
            headers={"User-Agent": user_agent},
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def _record_stats(self, *, endpoint_name: str, latency_ms: float, status_code: int | None) -> None:
        rate_limited = 1 if status_code == 429 else 0
        success = 1 if (status_code is not None and 200 <= status_code < 300) else 0
        bin_upper = _bin_upper_ms(latency_ms)

        c = self._state.conn
        c.execute(
            """
            INSERT INTO http_stats_endpoint(endpoint_name, request_count, success_count, rate_limited_count, total_latency_ms)
            VALUES (?, 1, ?, ?, ?)
            ON CONFLICT(endpoint_name) DO UPDATE SET
              request_count = request_count + 1,
              success_count = success_count + excluded.success_count,
              rate_limited_count = rate_limited_count + excluded.rate_limited_count,
              total_latency_ms = total_latency_ms + excluded.total_latency_ms
            """,
            (endpoint_name, success, rate_limited, float(latency_ms)),
        )
        c.execute(
            """
            INSERT INTO http_latency_hist(endpoint_name, bin_upper_ms, count)
            VALUES (?, ?, 1)
            ON CONFLICT(endpoint_name, bin_upper_ms) DO UPDATE SET count = count + 1
            """,
            (endpoint_name, bin_upper),
        )

    def request_json(
        self,
        *,
        endpoint_name: str,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        attempt = 0
        last_err: Exception | None = None

        while attempt <= self._retry.max_retries:
            self._limiter.wait()
            start = time.perf_counter()
            status_code: int | None = None

            try:
                resp = self._client.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers=headers,
                )
                status_code = resp.status_code
                latency_ms = (time.perf_counter() - start) * 1000.0
                self._record_stats(endpoint_name=endpoint_name, latency_ms=latency_ms, status_code=status_code)

                if 200 <= resp.status_code < 300:
                    try:
                        return resp.json()
                    except json.JSONDecodeError as err:
                        raise HttpError(
                            endpoint_name=endpoint_name,
                            method=method,
                            url=str(resp.url),
                            status_code=resp.status_code,
                            message=f"invalid json response: {err}",
                        ) from err

                if resp.status_code in (429,) or 500 <= resp.status_code <= 599:
                    retry_after_s = _parse_retry_after_seconds(resp.headers.get("Retry-After"))
                    backoff_s = _compute_backoff_s(self._retry, attempt, retry_after_s=retry_after_s)
                    logger.warning(
                        "retryable http status endpoint=%s status=%s attempt=%s backoff_s=%.2f url=%s",
                        endpoint_name,
                        resp.status_code,
                        attempt,
                        backoff_s,
                        str(resp.url),
                    )
                    time.sleep(backoff_s)
                    attempt += 1
                    continue

                raise HttpError(
                    endpoint_name=endpoint_name,
                    method=method,
                    url=str(resp.url),
                    status_code=resp.status_code,
                    message=_short_http_error(resp),
                )

            except (httpx.TimeoutException, httpx.NetworkError) as err:
                latency_ms = (time.perf_counter() - start) * 1000.0
                self._record_stats(endpoint_name=endpoint_name, latency_ms=latency_ms, status_code=status_code)
                last_err = err
                backoff_s = _compute_backoff_s(self._retry, attempt, retry_after_s=None)
                logger.warning(
                    "network error endpoint=%s attempt=%s backoff_s=%.2f err=%s url=%s",
                    endpoint_name,
                    attempt,
                    backoff_s,
                    repr(err),
                    url,
                )
                time.sleep(backoff_s)
                attempt += 1
                continue

        raise HttpError(
            endpoint_name=endpoint_name,
            method=method,
            url=url,
            status_code=None,
            message=f"exhausted retries: {last_err!r}",
        )

    def xrpc_get(
        self,
        *,
        endpoint_name: str,
        host: str,
        method: str,
        params: dict[str, Any] | None = None,
        access_jwt: str | None = None,
    ) -> dict[str, Any]:
        url = _xrpc_url(host, method)
        headers = {"Authorization": f"Bearer {access_jwt}"} if access_jwt else None
        return self.request_json(
            endpoint_name=endpoint_name,
            method="GET",
            url=url,
            params=params,
            headers=headers,
        )

    def xrpc_post(
        self,
        *,
        endpoint_name: str,
        host: str,
        method: str,
        json_body: dict[str, Any] | None,
        access_jwt: str | None = None,
    ) -> dict[str, Any]:
        if method not in _ALLOWED_POST_METHODS:
            raise ValueError(f"refusing to call non-read-only POST endpoint: {method}")
        url = _xrpc_url(host, method)
        headers: dict[str, str] = {}
        if access_jwt:
            headers["Authorization"] = f"Bearer {access_jwt}"
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        return self.request_json(
            endpoint_name=endpoint_name,
            method="POST",
            url=url,
            json_body=json_body,
            headers=headers,
        )


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


def _compute_backoff_s(cfg: HttpRetryConfig, attempt: int, *, retry_after_s: float | None) -> float:
    if retry_after_s is not None and retry_after_s > 0:
        # Keep a small jitter so many clients don't synchronize.
        return retry_after_s + random.uniform(0, 0.25)
    exp = cfg.base_backoff_s * (2**attempt)
    exp = min(exp, cfg.max_backoff_s)
    return exp + random.uniform(0, exp * 0.25)


def _short_http_error(resp: httpx.Response) -> str:
    body = None
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


def _xrpc_url(host: str, method: str) -> str:
    return host.rstrip("/") + "/xrpc/" + method
