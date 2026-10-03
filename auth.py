"""Jellyfin-backed authentication and in-memory Gateway sessions.

Jellyfin remains the source of truth for identities and administrator status.
Passwords are sent directly to Jellyfin and are never retained by the Gateway;
the browser receives only an opaque, HttpOnly session cookie.
"""
from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException, Request


SESSION_COOKIE = "gateway_session"

# Maximum credential field lengths accepted by the Gateway.  These bound the
# strings forwarded to Jellyfin so an unauthenticated caller cannot push
# arbitrarily large username/password values into the auth path (ISSUE 7).
# The HTTP route enforces the same limits (imported from here) BEFORE any
# limiter or Jellyfin call, and this method re-checks them as defense in
# depth so a future direct caller cannot bypass the route.
MAX_USERNAME_CHARS = 256
MAX_PASSWORD_CHARS = 4096


class AuthenticationError(Exception):
    """Credentials or an existing Jellyfin token are no longer valid."""


class JellyfinUnavailable(Exception):
    """Jellyfin could not be reached to authenticate or validate a user."""


@dataclass(frozen=True)
class GatewayUser:
    user_id: str
    username: str
    is_admin: bool


@dataclass
class GatewaySession:
    session_id: str
    user_id: str
    username: str
    jellyfin_token: str
    is_admin: bool
    device_id: str
    created_at: float
    last_validated: float
    last_seen: float = 0.0

    def user(self) -> GatewayUser:
        return GatewayUser(self.user_id, self.username, self.is_admin)


class JellyfinAuth:
    """Authenticate with Jellyfin and maintain process-local sessions."""

    def __init__(self, jellyfin_url: Callable[[], str], *, version: str,
                 validation_interval: float = 60.0,
                 max_session_age: float = 24 * 60 * 60,
                 idle_timeout: float = 60 * 60,
                 max_sessions_per_user: int = 10,
                 client: httpx.Client | None = None):
        self._jellyfin_url = jellyfin_url
        self.version = version
        self.validation_interval = validation_interval
        self.max_session_age = max_session_age
        self.idle_timeout = idle_timeout
        self.max_sessions_per_user = max_sessions_per_user
        self.http = client or httpx.Client(timeout=10, follow_redirects=False)
        self.sessions: dict[str, GatewaySession] = {}
        self.lock = threading.RLock()

    def _base_url(self) -> str:
        return self._jellyfin_url().rstrip("/")

    def _device_header(self, device_id: str, token: str | None = None) -> str:
        value = (f'MediaBrowser Client="Jellyfin Gateway", Device="Web", '
                 f'DeviceId="{device_id}", Version="{self.version}"')
        if token:
            value += f', Token="{token}"'
        return value

    @staticmethod
    def _user_from_payload(payload: dict) -> GatewayUser:
        policy = payload.get("Policy") or {}
        user_id = str(payload.get("Id") or "")
        username = str(payload.get("Name") or payload.get("Username") or "")
        disabled = bool(payload.get("IsDisabled") or policy.get("IsDisabled"))
        if not user_id or not username or disabled:
            raise AuthenticationError("Jellyfin account is disabled or unavailable")
        return GatewayUser(user_id, username, bool(policy.get("IsAdministrator")))

    def _authenticate_credentials(self, username: str, password: str,
                                  device_id: str) -> tuple[GatewayUser, str]:
        try:
            response = self.http.post(
                f"{self._base_url()}/Users/AuthenticateByName",
                json={"Username": username, "Pw": password},
                headers={"Authorization": self._device_header(device_id)},
            )
        except httpx.HTTPError as exc:
            raise JellyfinUnavailable("Jellyfin is unavailable") from exc
        if response.status_code in (400, 401, 403):
            raise AuthenticationError("Invalid Jellyfin username or password")
        if response.status_code >= 400:
            raise JellyfinUnavailable(f"Jellyfin authentication failed ({response.status_code})")
        try:
            payload = response.json()
            user = self._user_from_payload(payload.get("User") or {})
            token = str(payload.get("AccessToken") or "")
        except (ValueError, TypeError) as exc:
            raise JellyfinUnavailable("Jellyfin returned an invalid authentication response") from exc
        if not token:
            raise JellyfinUnavailable("Jellyfin did not return an access token")
        return user, token

    def login(self, username: str, password: str) -> tuple[str, GatewayUser]:
        # Type gate FIRST: non-string credentials must be rejected, never
        # coerced, and never reach .strip() (AttributeError) or the length
        # checks / _authenticate_credentials() (a short list has a len()).
        if not isinstance(username, str) or not isinstance(password, str):
            raise AuthenticationError("Invalid username or password")
        username = username.strip()
        if (not username or not password
                or len(username) > MAX_USERNAME_CHARS
                or len(password) > MAX_PASSWORD_CHARS):
            raise AuthenticationError("Invalid username or password")
        device_id = "gateway-" + secrets.token_urlsafe(18)
        user, token = self._authenticate_credentials(username, password, device_id)
        now = time.time()
        session_id = secrets.token_urlsafe(32)
        session = GatewaySession(session_id, user.user_id, user.username, token,
                                 user.is_admin, device_id, now, now, now)
        with self.lock:
            self.sessions[session_id] = session
            same_user = sorted(
                (s for s in self.sessions.values() if s.user_id == user.user_id),
                key=lambda s: s.created_at,
            )
            for old in same_user[:-self.max_sessions_per_user]:
                self.sessions.pop(old.session_id, None)
        return session_id, user

    def _fetch_current_user(self, session: GatewaySession) -> GatewayUser:
        try:
            response = self.http.get(
                f"{self._base_url()}/Users/Me",
                headers={
                    "X-Emby-Token": session.jellyfin_token,
                    "Authorization": self._device_header(session.device_id,
                                                          session.jellyfin_token),
                },
            )
        except httpx.HTTPError as exc:
            raise JellyfinUnavailable("Jellyfin is unavailable") from exc
        if response.status_code in (401, 403, 404):
            raise AuthenticationError("Jellyfin session is no longer valid")
        if response.status_code >= 400:
            raise JellyfinUnavailable(f"Jellyfin validation failed ({response.status_code})")
        try:
            user = self._user_from_payload(response.json())
        except (ValueError, TypeError) as exc:
            raise JellyfinUnavailable("Jellyfin returned an invalid user response") from exc
        if user.user_id != session.user_id:
            raise AuthenticationError("Jellyfin session user changed")
        return user

    def get_user(self, session_id: str | None, *, force_validate: bool = False) -> GatewayUser:
        if not session_id:
            raise AuthenticationError("Authentication required")
        with self.lock:
            session = self.sessions.get(session_id)
        if session is None:
            raise AuthenticationError("Authentication required")
        now = time.time()
        last_seen = session.last_seen or session.created_at
        if (now - session.created_at > self.max_session_age
                or now - last_seen > self.idle_timeout):
            with self.lock:
                self.sessions.pop(session_id, None)
            raise AuthenticationError("Gateway session expired")
        if force_validate or now - session.last_validated >= self.validation_interval:
            try:
                user = self._fetch_current_user(session)
            except AuthenticationError:
                with self.lock:
                    self.sessions.pop(session_id, None)
                raise
            with self.lock:
                current = self.sessions.get(session_id)
                if current is None:
                    raise AuthenticationError("Authentication required")
                current.username = user.username
                current.is_admin = user.is_admin
                current.last_validated = time.time()
                session = current
        with self.lock:
            current = self.sessions.get(session_id)
            if current is None:
                raise AuthenticationError("Authentication required")
            current.last_seen = time.time()
            session = current
        return session.user()

    def logout(self, session_id: str | None) -> None:
        with self.lock:
            session = self.sessions.pop(session_id, None) if session_id else None
        if session is None:
            return
        try:
            self.http.post(
                f"{self._base_url()}/Sessions/Logout",
                headers={
                    "X-Emby-Token": session.jellyfin_token,
                    "Authorization": self._device_header(session.device_id,
                                                          session.jellyfin_token),
                },
            )
        except httpx.HTTPError:
            # The local session is already gone; remote revocation is best-effort.
            pass

    def clear(self) -> None:
        """Forget all browser sessions, for example after changing Jellyfin URL."""
        with self.lock:
            self.sessions.clear()

    def close(self) -> None:
        self.clear()
        self.http.close()

    def require_user(self, request: Request) -> GatewayUser:
        try:
            return self.get_user(request.cookies.get(SESSION_COOKIE))
        except AuthenticationError as exc:
            raise HTTPException(401, str(exc))
        except JellyfinUnavailable as exc:
            raise HTTPException(503, str(exc))

    def require_admin(self, request: Request) -> GatewayUser:
        try:
            # Sensitive actions always re-check Jellyfin rather than trusting
            # the cached role from login or a previous ordinary request.
            user = self.get_user(request.cookies.get(SESSION_COOKIE), force_validate=True)
        except AuthenticationError as exc:
            raise HTTPException(401, str(exc))
        except JellyfinUnavailable as exc:
            raise HTTPException(503, str(exc))
        if not user.is_admin:
            raise HTTPException(403, "Administrator access required")
        return user

    @staticmethod
    def require_same_origin(request: Request) -> None:
        """Reject browser-initiated cross-origin state changes.

        Non-browser clients that send neither Origin/Referer nor Fetch Metadata
        remain usable. Browsers supply at least one of these signals for the
        requests used by this dashboard.
        """
        if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
            raise HTTPException(403, "Cross-origin request rejected")
        source = request.headers.get("origin") or request.headers.get("referer")
        if not source:
            return
        try:
            supplied = urlsplit(source)
            expected = urlsplit(str(request.base_url))
            supplied_port = supplied.port or (443 if supplied.scheme == "https" else 80)
            expected_port = expected.port or (443 if expected.scheme == "https" else 80)
            matches = (supplied.scheme.lower() == expected.scheme.lower()
                       and (supplied.hostname or "").lower() == (expected.hostname or "").lower()
                       and supplied_port == expected_port)
        except ValueError:
            matches = False
        if not matches:
            raise HTTPException(403, "Cross-origin request rejected")
