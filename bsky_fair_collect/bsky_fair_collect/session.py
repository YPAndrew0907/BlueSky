from __future__ import annotations

import logging
from dataclasses import dataclass

from bsky_fair_collect.env import Credentials, load_credentials
from bsky_fair_collect.http_client import HttpClient, HttpError
from bsky_fair_collect.state import StateDB
from bsky_fair_collect.utils import utc_now_iso

logger = logging.getLogger("bsky_fair_collect.session")

_META_PDS = "session_pds"
_META_ACCESS = "session_access_jwt"
_META_REFRESH = "session_refresh_jwt"
_META_UPDATED = "session_updated_at_utc"


@dataclass(frozen=True)
class SessionTokens:
    pds: str
    access_jwt: str
    refresh_jwt: str | None


class SessionManager:
    def __init__(
        self,
        *,
        state: StateDB,
        http: HttpClient,
        creds: Credentials | None = None,
    ) -> None:
        self._state = state
        self._http = http
        self._creds = creds
        pds = (creds.pds if creds else (state.get_meta(_META_PDS) or "https://bsky.social")).rstrip("/")
        self._state.set_meta(_META_PDS, pds)
        if creds:
            if creds.access_jwt:
                self._state.set_meta(_META_ACCESS, creds.access_jwt)
            if creds.refresh_jwt:
                self._state.set_meta(_META_REFRESH, creds.refresh_jwt)

    @property
    def pds(self) -> str:
        return (self._state.get_meta(_META_PDS) or "https://bsky.social").rstrip("/")

    def get_access_jwt(self) -> str:
        access = self._state.get_meta(_META_ACCESS) or (self._creds.access_jwt if self._creds else None)
        if access:
            self._state.set_meta(_META_ACCESS, access)
            return access

        self._obtain_tokens()
        access2 = self._state.get_meta(_META_ACCESS)
        if not access2:
            raise RuntimeError("failed to obtain access token (no accessJwt available)")
        return access2

    def xrpc_get(
        self,
        *,
        endpoint_name: str,
        host: str,
        method: str,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        # One refresh attempt on expired token.
        for attempt in range(2):
            access = self.get_access_jwt()
            try:
                return self._http.xrpc_get(
                    endpoint_name=endpoint_name,
                    host=host,
                    method=method,
                    params=params,
                    access_jwt=access,
                )
            except HttpError as err:
                if attempt == 0 and _is_expired_token(err):
                    logger.warning("access token expired; refreshing session and retrying endpoint=%s", endpoint_name)
                    if self.refresh_or_create():
                        continue
                raise
        raise RuntimeError("unreachable")

    def refresh_or_create(self) -> bool:
        if self._try_refresh():
            return True
        return self._try_create_session()

    def _obtain_tokens(self) -> None:
        if self._try_refresh():
            return
        if self._try_create_session():
            return
        raise RuntimeError(
            "no valid session available: provide BLUESKY_REFRESH_JWT (preferred) or BSKY_HANDLE+BSKY_APP_PASSWORD"
        )

    def _try_refresh(self) -> bool:
        refresh = self._state.get_meta(_META_REFRESH) or (self._creds.refresh_jwt if self._creds else None)
        if not refresh:
            return False

        try:
            resp = self._http.xrpc_post(
                endpoint_name="com.atproto.server.refreshSession",
                host=self.pds,
                method="com.atproto.server.refreshSession",
                json_body=None,
                access_jwt=refresh,
            )
        except HttpError as err:
            logger.warning("refreshSession failed status=%s err=%s", err.status_code, str(err))
            return False

        access = resp.get("accessJwt")
        new_refresh = resp.get("refreshJwt")
        if not isinstance(access, str) or not access:
            return False
        refresh_out = new_refresh if isinstance(new_refresh, str) and new_refresh else None
        self._store_tokens(access_jwt=access, refresh_jwt=refresh_out)
        return True

    def _try_create_session(self) -> bool:
        creds = self._creds or load_credentials()
        if creds is None or not creds.handle or not creds.app_password:
            return False

        try:
            resp = self._http.xrpc_post(
                endpoint_name="com.atproto.server.createSession",
                host=creds.pds,
                method="com.atproto.server.createSession",
                json_body={"identifier": creds.handle, "password": creds.app_password},
                access_jwt=None,
            )
        except HttpError as err:
            logger.warning("createSession failed status=%s err=%s", err.status_code, str(err))
            return False

        access = resp.get("accessJwt")
        refresh = resp.get("refreshJwt")
        if not isinstance(access, str) or not access:
            return False
        refresh_out = refresh if isinstance(refresh, str) and refresh else None
        self._store_tokens(access_jwt=access, refresh_jwt=refresh_out)
        return True

    def _store_tokens(self, *, access_jwt: str, refresh_jwt: str | None) -> None:
        # Never log token values.
        self._state.set_meta(_META_ACCESS, access_jwt)
        if refresh_jwt:
            self._state.set_meta(_META_REFRESH, refresh_jwt)
        self._state.set_meta(_META_UPDATED, utc_now_iso())


def _is_expired_token(err: HttpError) -> bool:
    if err.status_code not in (400, 401):
        return False
    msg = str(err)
    return ("ExpiredToken" in msg) or ("Token has expired" in msg) or ("InvalidToken" in msg)
