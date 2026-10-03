#!/usr/bin/env python3
"""
Jellyfin Gateway — local dashboard for Jellyfin + qBittorrent + NordVPN.

- Status cards for Jellyfin (any reachable server), qBittorrent WebUI and NordVPN.
- Paste a magnet (or upload a .torrent) tagged Movie / TV.
- A background watcher sorts completed torrents into
    media/Movies/<Title (Year)>/   and   media/TV/<Show>/Season NN/
- A VPN guard pauses all torrents if the tunnel interface disappears.
"""
import base64
from collections import OrderedDict, deque
from dataclasses import dataclass
import fnmatch
import hashlib
import json
import logging
import math
import os
import re
import shutil
import socket
import stat
import subprocess
import tempfile
import threading
import time
import weakref
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from auth import (AuthenticationError, GatewayUser, JellyfinAuth,
                  JellyfinUnavailable, MAX_PASSWORD_CHARS,
                  MAX_USERNAME_CHARS, SESSION_COOKIE)
from service_utils import _safe_service_error
from qb import (QBClient, QBUnsupportedVersion, _parse_qb_major,
                 _qb_mutation_ok, _qb_add_accepted)
from state_store import (StateStore, StatePersistenceError,
                            StateCorruptionError, _SORT_STATUS_OK,
                            _SORT_CORRUPT_MSG, _CLEANUP_CORRUPT_MSG,
                            _ADD_VERIFY_CORRUPT_MSG, _finite_number,
                            _validate_torrent_state,
                            _validate_event_log,
                            _validate_persisted_plan,
                            legacy_incomplete, sort_terminal,
                            cleanup_pending)
from jellyfin_client import (_SKIP_IFACE_KEYS, _TS_IP_CACHE,
                              _local_ipv4, _tailscale_ipv4,
                              jellyfin_status, JellyfinRefreshResult,
                              jellyfin_refresh)
from watch import (GUARD_STATE, watch_loop, make_guard,
                    sync_guard_state, guard_loop, apply_guard_change)
from add_verify import (ADD_VERIFY_ATTEMPTS, ADD_VERIFY_DELAY,
                         ADD_VERIFY_WARN_INTERVAL,
                         _confirm_added_torrent,
                         _rollback_added_torrent,
                         _promote_verified_add,
                         _discard_pending_add, _pending_add_warn,
                         reconcile_pending_add)
from sort_orchestrator import (UPLOADING_STATES,
                                CLEANUP_WARN_INTERVAL,
                                SORT_RETRY_BACKOFF,
                                is_complete, UnsafeTorrentPath,
                                require_download_path,
                                _require_plan_destinations,
                                _plan_is_pristine_for_replan,
                                _qb_rel_parts, ResolvedContent,
                                resolve_content,
                                _build_and_persist_sort_plan,
                                do_sort, _build_sort_plan,
                                _run_sort_plan, _finish_sort,
                                _delete_torrent, retry_cleanup,
                                _cleanup_failed, try_sort)
import config
import nordvpn
import sorter
from vpn_guard import Guard, INACTIVE_STATES   # shared kill-switch logic

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("gateway")

VERSION = "1.0.0"
START_TIME = time.time()
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "object-src 'none'"
)
# Dashboard-specific state buckets.  These are separate from
# UPLOADING_STATES, which defines sorting eligibility (progress >= 1.0).
# A stoppedUP torrent is not actively seeding, but its payload is
# complete and can still be eligible for local sorting.
DASH_SEEDING_STATES = {"uploading", "forcedUP"}
DASH_DOWNLOADING_STATES = {"downloading", "forcedDL", "metaDL", "forcedMetaDL"}
DASH_STOPPED_STATES = {
    "stoppedUP", "stoppedDL",
    "pausedUP", "pausedDL",  # legacy qB 4.x compatibility
}

MAX_TORRENT_BYTES = 4 * 1024 * 1024

# ISSUE 9: the settings whose effective change means the one-time qB 5.x
# initialization must run again.  Correctness does NOT depend on this set:
# the derived init key inside ensure_qb_init() (the exact values the
# successful writes used) is the invalidation mechanism.  This set only
# decides whether the Settings route triggers the retry immediately
# instead of waiting for the next watcher poll.
QB_INIT_FIELDS = {"QB_URL", "QB_USER", "QB_PASS", "DOWNLOAD_DIR"}
# Whole POST /api/add body cap: the 4 MiB torrent file plus the multipart
# envelope (boundaries, per-part headers) and the small kind field.  The
# ASGI middleware enforces this BEFORE Starlette buffers the body, so an
# oversized upload is rejected at the transport edge instead of after the
# whole multipart has already been received (ISSUE 9).
MAX_ADD_BODY_BYTES = MAX_TORRENT_BYTES + 64 * 1024
# Whole POST /api/auth/login body cap: two bounded credential strings
# (username <= 256 chars, password <= 4096 chars) plus JSON object overhead.
# 16 KiB is far more than a real login needs while sharply limiting what an
# unauthenticated caller can make the server allocate for the body BEFORE
# Starlette ever parses it (ISSUE 7).  Enforced by the same per-route _BodyCap
# middleware as /api/add.
MAX_LOGIN_BODY_BYTES = 16 * 1024
# Whole POST /api/vpn body cap: {"action": "connect"|"disconnect"} is a
# dozen or so bytes; 4 KiB is generous while keeping an oversized JSON
# body out of the request buffer BEFORE auth runs (the admin JSON routes
# have no other transport-level bound, so an unauthenticated caller could
# otherwise make the server buffer an arbitrarily large body first).
MAX_VPN_BODY_BYTES = 4 * 1024
# Whole POST /api/config body cap: the settings dict is a few dozen keys
# of short strings; 64 KiB is generous while bounding what an
# unauthenticated caller can make the server allocate for the body.
MAX_CONFIG_BODY_BYTES = 64 * 1024
# A magnet link is a few hundred bytes in practice; 16 KiB is generous and
# bounds the urlparse()/parse_qs() work on the form field (ISSUE 9).
MAX_MAGNET_LEN = 16 * 1024
# A magnet query string has a handful of fields (xt, dn, tr, sd, ...); 128
# bounds parse_qs() so a pathologically long &-list is rejected (ISSUE 9).
MAX_MAGNET_FIELDS = 128
# How long to wait for background threads to stop during shutdown.
# QBClient already has a 15-second HTTP timeout, so this gives an
# ordinary in-flight qB request time to finish without allowing
# shutdown to hang forever.
SHUTDOWN_JOIN_TIMEOUT = 25.0



# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# sorting helpers
# ---------------------------------------------------------------------------

def _decode_btih(value: str) -> str | None:
    """v1 info hash (20 bytes) from a btih: topic -> 40 lowercase hex, or None.

    Accepts the 40-hex form and the 32-char base32 form (BEP 9).
    """
    s = value.strip().lower()
    try:
        if len(s) == 40:
            return bytes.fromhex(s).hex()
        if len(s) == 32:
            return base64.b32decode(s.upper() + "=" * (-len(s) % 8)).hex()
    except Exception:
        return None
    return None


def _decode_btmh(value: str) -> str | None:
    """v2 info hash (32 bytes) from a btmh: topic -> 64 lowercase hex, or None.

    The canonical BEP 9 form is a hex-encoded multihash: 1220 + 64 hex
    (0x12 = SHA-256 tag, 0x20 = 32-byte length). The whole multihash is
    validated as bytes: wrong length, non-hex input, or a tag other than
    1220 is rejected. The bare 64-hex digest is also accepted.
    """
    s = value.strip().lower()
    try:
        if len(s) == 68:
            raw = bytes.fromhex(s)
            if len(raw) == 34 and raw[:2] == b"\x12\x20":
                return raw[2:].hex()
            return None
        if len(s) == 64:
            return bytes.fromhex(s).hex()
    except Exception:
        return None
    return None


def _magnet_query(magnet: str) -> dict:
    """parse_qs() of a magnet's query string with a bounded field count.

    Both magnet-parsing paths share this helper so the field cap cannot
    diverge.  Raises ValueError when the query has more than
    MAX_MAGNET_FIELDS fields; callers surface that as a 400.
    """
    return parse_qs(urlparse(magnet).query, max_num_fields=MAX_MAGNET_FIELDS)


def magnet_hashes(magnet: str) -> dict | None:
    """Extract the info hashes from a magnet link.

    Returns {"v1": 40hex|None, "v2": 64hex|None, "hash": 40hex,
    "version": "v1"|"v2"|"hybrid"} or None when no usable hash is present.
    `hash` is the qBittorrent TorrentID: the v1 hash for v1-only torrents,
    and the first 40 hex chars of the v2 hash for v2/hybrid (qB/libtorrent
    use the truncated v2 as the torrent key whenever a v2 hash exists).
    A magnet may carry several xt= topics; a hybrid carries both
    urn:btih: and urn:btmh:. Conflicting duplicates are rejected.
    """
    try:
        q = _magnet_query(magnet)
    except Exception:
        return None
    v1s = {_decode_btih(x.split("btih:", 1)[1])
           for x in q.get("xt", []) if x.startswith("urn:btih:")}
    v2s = {_decode_btmh(x.split("btmh:", 1)[1])
           for x in q.get("xt", []) if x.startswith("urn:btmh:")}
    v1s.discard(None)
    v2s.discard(None)
    if len(v1s) > 1 or len(v2s) > 1:
        return None   # conflicting duplicates: do not guess
    v1 = v1s.pop() if v1s else None
    v2 = v2s.pop() if v2s else None
    if v1 and v2:
        return {"v1": v1, "v2": v2, "hash": v2[:40], "version": "hybrid"}
    if v2:
        return {"v1": None, "v2": v2, "hash": v2[:40], "version": "v2"}
    if v1:
        return {"v1": v1, "v2": None, "hash": v1, "version": "v1"}
    return None


def magnet_hash(magnet: str) -> str | None:
    """qBittorrent TorrentID for a magnet (compatibility wrapper)."""
    info = magnet_hashes(magnet)
    return info["hash"] if info else None


def qb_vpn_binding_status(prefs: dict | None, tunnel_iface: str | None,
                          patterns=None) -> dict:
    """Describe whether qB is restricted to the configured VPN interface.

    ``patterns`` lets the caller pass the interface globs from its own
    config snapshot so the binding decision and the tunnel detection use
    the same generation of the configuration (ISSUE 5)."""
    if patterns is None:
        patterns = config.snapshot().VPN_GUARD_IFACES
    if prefs is None:
        return {"checked": False, "safe": False, "interface": None,
                "vpn_interface": tunnel_iface}
    bound = str(prefs.get("current_network_interface") or "").strip()
    matches_vpn = bool(bound) and any(
        fnmatch.fnmatch(bound, pattern) for pattern in patterns)
    safe = matches_vpn and (tunnel_iface is None or bound == tunnel_iface)
    return {
        "checked": True,
        "safe": safe,
        "interface": bound or None,
        "vpn_interface": tunnel_iface,
    }


# ---------------------------------------------------------------------------
# login rate limiting (ISSUE 8)
# ---------------------------------------------------------------------------

def _login_account_key(username: str) -> str:
    """Fixed-size (64-hex-char) limiter key for an account (ISSUE 7).

    The per-account limiter tables are bounded in the NUMBER of keys but
    were not bounded in the SIZE of each key: an unauthenticated attacker
    could stuff arbitrarily long strings into the key space.  The key is
    the SHA-256 hex digest of the normalized username (strip + casefold,
    the same normalization the limiter has always compared on, so
    'Alice' and 'aLiCe' share one key), always exactly 64 ASCII chars.
    surrogatepass keeps any (invalid) code point JSON could hand us
    hashable instead of raising.  Applied INSIDE LoginRateLimiter, so no
    caller — route, test, or future code — can ever store a raw username
    in the tables.
    """
    normalized = username.strip().casefold()
    return hashlib.sha256(normalized.encode("utf-8", "surrogatepass")).hexdigest()


class LoginRateLimiter:
    """Bounded sliding-window limiter for failed logins.

    Two failure tables and two in-flight reservation tables, all under one
    lock:
      - per effective client IP: 5 attempts within 60s -> 429
      - per account: 10 attempts within 10 min -> 429.  Accounts are keyed
        by a fixed-size SHA-256 digest of the normalized username
        (_login_account_key), never by the raw (unbounded) string
        (ISSUE 7), so the tables are bounded in key COUNT and in key SIZE.
    An "attempt" is a recorded failure OR an outstanding in-flight
    reservation, so concurrent requests cannot all pass the check before any
    failure is recorded: reserve() atomically checks BOTH tables (failures
    plus reservations) and, if allowed, holds one in-flight slot for each
    key until the attempt resolves.  Every table is hard-capped at MAX_KEYS
    keys (LRU eviction) and self-prunes by TTL, so the structure is bounded
    in memory and time even under a scan of one-shot source addresses.
    """
    IP_WINDOW = 60.0
    IP_LIMIT = 5
    ACCT_WINDOW = 600.0
    ACCT_LIMIT = 10
    MAX_KEYS = 4096
    # a reservation lives at most this long; authn.login's HTTP timeout is
    # 10s, so a leaked reservation is reclaimed long before it can matter.
    RESERVATION_TTL = 60.0

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.ip_failures: "OrderedDict[str, deque]" = OrderedDict()
        self.acct_failures: "OrderedDict[str, deque]" = OrderedDict()
        self.ip_res: "OrderedDict[str, deque]" = OrderedDict()
        self.acct_res: "OrderedDict[str, deque]" = OrderedDict()

    def _prune(self, table, window: float, now: float) -> None:
        for key in list(table):
            dq = table[key]
            while dq and now - dq[0] > window:
                dq.popleft()
            if not dq:
                del table[key]
        while len(table) > self.MAX_KEYS:
            table.popitem(last=False)

    def _total(self, failures, res, key) -> int:
        return len(failures.get(key, ())) + len(res.get(key, ()))

    def _prune_all(self, now: float) -> None:
        self._prune(self.ip_failures, self.IP_WINDOW, now)
        self._prune(self.acct_failures, self.ACCT_WINDOW, now)
        self._prune(self.ip_res, self.RESERVATION_TTL, now)
        self._prune(self.acct_res, self.RESERVATION_TTL, now)

    def limited(self, ip: str, account: str) -> tuple[bool, bool]:
        """Pure peek (no reservation) for tests/introspection:
        (ip_limited, account_limited), counting failures + reservations.
        The account argument is any username form; it is normalized to the
        fixed-size key before the lookup (ISSUE 7)."""
        account = _login_account_key(account)
        now = time.time()
        with self.lock:
            self._prune_all(now)
            return (self._total(self.ip_failures, self.ip_res, ip) >= self.IP_LIMIT,
                    self._total(self.acct_failures, self.acct_res, account) >= self.ACCT_LIMIT)

    def reserve(self, ip: str, account: str) -> bool:
        """Atomically check both limits (failures + outstanding reservations)
        and, if allowed, hold one in-flight slot for each key.  Returns True
        if the attempt may proceed, False if rate-limited (nothing held).
        The account argument is normalized to its fixed-size key first
        (ISSUE 7)."""
        account = _login_account_key(account)
        now = time.time()
        with self.lock:
            self._prune_all(now)
            if (self._total(self.ip_failures, self.ip_res, ip) >= self.IP_LIMIT
                    or self._total(self.acct_failures, self.acct_res, account) >= self.ACCT_LIMIT):
                return False
            for res, key in ((self.ip_res, ip), (self.acct_res, account)):
                res.setdefault(key, deque()).append(now)
                res.move_to_end(key)
            return True

    def _release_res(self, res, key) -> None:
        dq = res.get(key)
        if dq:
            dq.pop()  # release the most recently reserved slot for this key
            if not dq:
                del res[key]

    def commit_failure(self, ip: str, account: str) -> None:
        """An in-flight attempt failed authentication: convert its reserved
        slot into a timestamped failure in both tables.  The account
        argument is normalized to its fixed-size key first (ISSUE 7)."""
        account = _login_account_key(account)
        now = time.time()
        with self.lock:
            self._release_res(self.ip_res, ip)
            self._release_res(self.acct_res, account)
            self.ip_failures.setdefault(ip, deque()).append(now)
            self.ip_failures.move_to_end(ip)
            self.acct_failures.setdefault(account, deque()).append(now)
            self.acct_failures.move_to_end(account)
            while len(self.ip_failures) > self.MAX_KEYS:
                self.ip_failures.popitem(last=False)
            while len(self.acct_failures) > self.MAX_KEYS:
                self.acct_failures.popitem(last=False)

    def release(self, ip: str, account: str) -> None:
        """An in-flight attempt resolved without a failed authentication
        (503, or success): drop its reserved slots, record no failure.
        The account argument is normalized to its fixed-size key first
        (ISSUE 7)."""
        account = _login_account_key(account)
        with self.lock:
            self._release_res(self.ip_res, ip)
            self._release_res(self.acct_res, account)

    def clear(self, ip: str, account: str) -> None:
        """A successful login: wipe the historical failure buckets for this
        IP and account (its own reservation is dropped via release).  The
        account argument is normalized to its fixed-size key first
        (ISSUE 7)."""
        account = _login_account_key(account)
        with self.lock:
            self.ip_failures.pop(ip, None)
            self.acct_failures.pop(account, None)


# ---------------------------------------------------------------------------
# app
# ---------------------------------------------------------------------------

class _BodyCap:
    """Pure ASGI middleware that caps request bodies per (method, path).

    It peeks the body by calling receive() itself and buffering
    http.request chunks until the body ends or the accumulated bytes
    exceed the route's cap.  On overflow it answers 413 immediately and
    then drains the remaining body so the connection closes cleanly; the
    application is never invoked, so Starlette never buffers or spools an
    oversized upload (ISSUE 9: POST /api/add; ISSUE 7: POST
    /api/auth/login — an UNAUTHENTICATED route, where the body cap is the
    only transport-level bound).  Under the cap the app is invoked with a
    replacement receive() that replays the buffered body as a single
    http.request message.  A declared Content-Length already over the cap
    is rejected up front (still draining the body).  Routes not in the
    caps map pass through untouched.
    """
    def __init__(self, app, caps):
        self.app = app
        # {(method, path): max raw bytes}; anything else passes through.
        self.caps = {(m, p): int(c) for (m, p), c in caps.items()}
        # The most bytes ever held in the peek buffer at once.  For any
        # single request it must never exceed that route's cap + 1: on
        # overflow the offending chunk is NOT copied into the buffer, only
        # its length is observed (ISSUE 9).
        self.max_buffered = 0

    def _cap_for(self, scope):
        if scope.get("type") != "http":
            return None
        return self.caps.get((scope.get("method"), scope.get("path")))

    async def __call__(self, scope, receive, send):
        cap = self._cap_for(scope)
        if cap is None:
            await self.app(scope, receive, send)
            return
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        declared = headers.get(b"content-length")
        try:
            declared_len = int(declared) if declared is not None else None
        except (TypeError, ValueError):
            declared_len = None
        buf = bytearray()
        over = declared_len is not None and declared_len > cap
        if not over:
            while True:
                message = await receive()
                mtype = message.get("type")
                if mtype == "http.disconnect":
                    return
                if mtype != "http.request":
                    continue
                chunk = message.get("body", b"")
                if len(buf) + len(chunk) > cap:
                    # Overflow: do NOT copy the (possibly huge) chunk into
                    # the buffer.  The length alone proves the cap is
                    # exceeded; the body is drained after the 413.
                    over = True
                    break
                buf.extend(chunk)
                if len(buf) > self.max_buffered:
                    self.max_buffered = len(buf)
                if not message.get("more_body", False):
                    break
        if over:
            # Reject FIRST, before reading any further body bytes: an
            # attacker who exceeded the cap must not be able to hold the
            # request open by trickling the remainder slowly (ISSUE 9).
            body = b"request body too large"
            await send({"type": "http.response.start", "status": 413,
                        "headers": [[b"content-type", b"text/plain; charset=utf-8"],
                                    [b"content-length", str(len(body)).encode()]]})
            await send({"type": "http.response.body", "body": body})
            # Now drain the rest of the body so the client can finish
            # sending and the connection closes cleanly.
            while True:
                message = await receive()
                if message.get("type") == "http.disconnect":
                    break
                if (message.get("type") == "http.request"
                        and not message.get("more_body", False)):
                    break
            return
        body = bytes(buf)
        state = {"replayed": False}

        async def replay_receive():
            if not state["replayed"]:
                state["replayed"] = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay_receive, send)


def create_app() -> FastAPI:
    from contextlib import asynccontextmanager

    qb = QBClient()
    store = StateStore(config.DATA_DIR)
    authn = JellyfinAuth(lambda: config.JELLYFIN_URL, version=VERSION)
    login_limiter = LoginRateLimiter()
    # The VPN guard is created HERE (not inside lifespan) so the background
    # loop and the Settings API share the SAME Guard object and the lock
    # that serializes every action on it (ticks, live enable/disable,
    # interface re-evaluation) — a settings change can never race a tick.
    guard = make_guard(qb, store)
    guard_tick_lock = threading.Lock()

    # Idempotent qB 5.x one-time setup: create the categories and enable
    # category paths in manual mode.  Runs the first time runtime
    # compatibility is confirmed True, whether that is at startup or
    # later (e.g. qB was offline or 4.x at startup and is now 5.x).
    #
    # ISSUE 9: the completion marker used to be a bare boolean, so an
    # init that STARTED on one configuration (old endpoint/credentials/
    # download dir) could finish AFTER an admin changed those settings
    # and stamp done=True — permanently claiming a configuration that
    # was never initialized (the flag only cleared on the NEXT settings
    # change).  Now:
    #   * the state stores the derived KEY of the exact snapshot the
    #     successful writes used (endpoint + credentials + download dir);
    #   * the fast path matches only when the CURRENT snapshot has the
    #     same key, so any effective change re-runs init on the new
    #     snapshot — a stale in-flight completion can only store its own
    #     (older) key and can never claim a newer configuration;
    #   * a dedicated lock serializes the startup, watcher and
    #     Settings-route invocations, so two mutation sequences never
    #     overlap and a blocked Settings call re-snapshots the NEW
    #     configuration when it finally runs (the snapshot is taken
    #     INSIDE the lock, after the in-flight init has finished).
    def _qb_init_key(cfg):
        # The effective inputs this init depends on.  MOVIES_DIR/TV_DIR,
        # VPN and polling settings are deliberately excluded: they do
        # not change what these writes do.
        return (cfg.QB_URL, cfg.QB_USER, cfg.QB_PASS, str(cfg.DOWNLOAD_DIR))

    _qb_init_state = {"key": None}
    _qb_init_lock = threading.Lock()

    def ensure_qb_init() -> None:
        with _qb_init_lock:
            cfg = config.snapshot()
            key = _qb_init_key(cfg)
            if _qb_init_state["key"] == key:
                return
            # One snapshot for the whole init: the pass gate, the version
            # check and BOTH category setups use the same generation of
            # the qB endpoint, credentials and download dir, so the movies
            # and tv categories can never be configured on different qB
            # instances (ISSUE 5).
            if not cfg.QB_PASS:
                return
            if qb.supports_gateway(cfg=cfg) is not True:
                return
            # Store the key only after a fully successful init, so a
            # transient failure is retried on a later poll (and a qB swap
            # re-inits: its key differs from the stored one).
            if not qb.ensure_category("movies", str(cfg.DOWNLOAD_DIR), cfg=cfg):
                return
            if not qb.ensure_category("tv", str(cfg.DOWNLOAD_DIR), cfg=cfg):
                return
            # Without this, qb (manual mode) ignores category save paths and
            # dumps everything into its global default download dir.
            prefs = qb.app_preferences(cfg)
            if prefs is None:
                return
            if prefs.get("use_category_paths_in_manual_mode") is False:
                if not qb.set_preference("use_category_paths_in_manual_mode",
                                         True, cfg=cfg):
                    return
                log.info("qBittorrent: enabled use_category_paths_in_manual_mode")
            # Publish exactly the configuration these writes used.
            _qb_init_state["key"] = key

    @asynccontextmanager
    async def lifespan(app):
        for d in (config.DOWNLOAD_DIR, config.MOVIES_DIR, config.TV_DIR, config.DATA_DIR):
            d.mkdir(parents=True, exist_ok=True)
        if config.QB_PASS:
            supported = qb.supports_gateway()
            if supported is False:
                log.error("Unsupported qBittorrent %s; Jellyfin Gateway requires "
                          "qBittorrent 5.x", qb.app_version())
                if config.VPN_GUARD:
                    raise RuntimeError(
                        f"VPN guard requires qBittorrent 5.x; detected "
                        f"{qb.app_version()}")
                # degraded mode: background threads still run (so a later
                # upgrade to 5.x is picked up), but their runtime gates
                # keep them from doing any qB work until 5.x is
                # confirmed; no category mutations yet; /api/add is
                # gated; the dashboard shows the incompatibility
        else:
            supported = None
        ensure_qb_init()
        stop = threading.Event()
        if config.QB_PASS and config.VPN_GUARD:
            # immediate fail-closed check at startup (README: "no tunnel at
            # startup -> torrents stay paused") so the dashboard reflects
            # reality before the first request instead of after one poll
            # (the loop thread is not started yet, but take the lock anyway
            # so the init path uses the same serialization as everything else)
            try:
                with guard_tick_lock:
                    guard.tick()
            except Exception:
                log.exception("guard init tick failed")
        sync_guard_state(guard)
        tw = threading.Thread(target=watch_loop,
                      args=(qb, store, stop, ensure_qb_init), daemon=True)
        tg = threading.Thread(target=guard_loop, args=(guard, qb, stop, guard_tick_lock), daemon=True)
        tw.start()
        tg.start()
        try:
            yield
        finally:
            stop.set()
            tw.join(timeout=SHUTDOWN_JOIN_TIMEOUT)
            tg.join(timeout=SHUTDOWN_JOIN_TIMEOUT)
            if not tw.is_alive() and not tg.is_alive():
                qb.http.close()
            else:
                log.warning("background threads did not stop cleanly; leaving qB HTTP client open")
            authn.close()

    app = FastAPI(title="Jellyfin Gateway", version=VERSION, lifespan=lifespan)
    app.state.auth = authn
    app.state.store = store
    app.state.qb = qb
    # the login rate limiter is exposed for tests and introspection
    app.state.login_limiter = login_limiter
    # the VPN guard (and the lock serializing every action on it) is
    # exposed for tests and introspection
    app.state.guard = guard
    app.state.guard_tick_lock = guard_tick_lock
    # the qB 5.x one-time init (ISSUE 9): the callable, its lock and the
    # keyed completion state are exposed for deterministic tests
    app.state.ensure_qb_init = ensure_qb_init
    app.state.qb_init_lock = _qb_init_lock
    app.state.qb_init_state = _qb_init_state
    web_dir = Path(__file__).resolve().parent / "web"
    web = web_dir / "index.html"

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    @app.get("/", response_class=HTMLResponse)
    def index():
        return web.read_text()

    @app.get("/static/app.js", response_class=FileResponse)
    def app_javascript():
        return FileResponse(web_dir / "app.js", media_type="text/javascript")

    # --- Jellyfin identity + opaque Gateway session cookie ---

    @app.post("/api/auth/login", dependencies=[Depends(authn.require_same_origin)])
    def api_auth_login(payload: dict, request: Request, response: Response):
        peer = request.client.host if request.client else "unknown"
        # Effective client for the IP limit: behind a configured trusted
        # reverse proxy the real client comes from X-Forwarded-For, otherwise
        # the socket peer is used and the header is ignored (ISSUE 8).
        client_key = config.effective_client_ip(peer, request.headers.get("x-forwarded-for"))
        # Credential fields: only real JSON strings are accepted — numbers,
        # objects and arrays must not be silently coerced into credential
        # strings (ISSUE 7).  Both are length-capped by the auth.py
        # constants: the raw body is already capped at
        # MAX_LOGIN_BODY_BYTES at the transport edge by _BodyCap, and these
        # field caps bound what can reach the limiter and Jellyfin (they
        # are re-checked in authn.login() for direct callers too).
        raw_username = payload.get("username")
        raw_password = payload.get("password")
        username = raw_username.strip() if isinstance(raw_username, str) else ""
        password = raw_password if isinstance(raw_password, str) else ""
        # Atomically check both limits (failures + in-flight reservations)
        # and, if allowed, hold one in-flight slot for each key so concurrent
        # requests cannot all pass the check before any failure is recorded.
        # The limiter keys the account by a fixed-size digest — never the
        # raw, unbounded username (ISSUE 7).
        if not login_limiter.reserve(client_key, username):
            raise HTTPException(429, "Too many failed login attempts; retry later")
        # Field validation happens AFTER reserve(): an intentionally
        # oversized or mistyped credential must consume the IP/account
        # budgets instead of being a free path around the limiter
        # (ISSUE 7).  Every rejection below shares ONE generic message: no
        # individual limit (missing, wrong type, too long) may be
        # distinguishable, and the Jellyfin error text is never echoed.
        if (not username or not password
                or len(username) > MAX_USERNAME_CHARS
                or len(password) > MAX_PASSWORD_CHARS):
            login_limiter.commit_failure(client_key, username)
            raise HTTPException(401, "Invalid username or password")
        try:
            session_id, user = authn.login(username, password)
        except AuthenticationError:
            login_limiter.commit_failure(client_key, username)
            raise HTTPException(401, "Invalid username or password")
        except JellyfinUnavailable as e:
            # 503 does NOT consume either rate-limit budget.
            login_limiter.release(client_key, username)
            raise HTTPException(503, str(e))
        response.set_cookie(
            SESSION_COOKIE, session_id, httponly=True, samesite="strict",
            secure=request.url.scheme == "https", path="/",
        )
        login_limiter.release(client_key, username)
        login_limiter.clear(client_key, username)
        return {"authenticated": True, "username": user.username,
                "user_id": user.user_id, "is_admin": user.is_admin}

    @app.post("/api/auth/logout", dependencies=[Depends(authn.require_same_origin)])
    def api_auth_logout(request: Request, response: Response):
        authn.logout(request.cookies.get(SESSION_COOKIE))
        response.delete_cookie(SESSION_COOKIE, path="/", samesite="strict",
                               secure=request.url.scheme == "https")
        return {"ok": True}

    @app.get("/api/auth/me")
    def api_auth_me(user: GatewayUser = Depends(authn.require_user)):
        return {"authenticated": True, "username": user.username,
                "user_id": user.user_id, "is_admin": user.is_admin}

    @app.get("/api/status")
    def api_status(user: GatewayUser = Depends(authn.require_user)):
        # One snapshot for the whole status page: the qB endpoint, the
        # media layout, the cleanup policy and the interface globs are
        # all from the same generation of the configuration (ISSUE 5).
        st_cfg = config.snapshot()
        qb_status = {"online": False, "logged_in": False, "url": config.public_service_url(st_cfg.QB_URL),
                     "required_version": "5.x", "supported": None}
        if st_cfg.QB_PASS:
            ver = qb.app_version(cfg=st_cfg)
            if ver:
                supported = qb.supports_gateway(cfg=st_cfg)
                qb_status.update(online=True, logged_in=True, version=ver,
                                 supported=supported)
                if supported is False:
                    qb_status["error"] = (
                        f"Unsupported qBittorrent {ver}; version 5.x is required")
                ti = qb.transfer_info(st_cfg)
                if ti:
                    qb_status.update(dl_speed=ti.get("dl_info_speed"),
                                     up_speed=ti.get("up_info_speed"),
                                     dht_nodes=ti.get("dht_nodes"),
                                     connection=ti.get("connection_status"))
                torrents = qb.torrents_info(st_cfg) or []
                counts = {"total": len(torrents), "downloading": 0, "seeding": 0,
                          "stopped": 0, "other": 0, "pending_sort": 0,
                          "paused": 0}
                for t in torrents:
                    st = t.get("state") or ""
                    if st in DASH_SEEDING_STATES:
                        counts["seeding"] += 1
                    elif st in DASH_DOWNLOADING_STATES:
                        counts["downloading"] += 1
                    elif st in DASH_STOPPED_STATES:
                        counts["stopped"] += 1
                    else:
                        counts["other"] += 1
                    rec = store.torrents.get(t.get("hash"), {})
                    if (t.get("category") or "").lower() in ("movies", "tv") \
                            and is_complete(t) and not sort_terminal(rec):
                        counts["pending_sort"] += 1
                counts["paused"] = counts["stopped"]
                qb_status["torrents"] = counts
                if user.is_admin:
                    tunnel_iface = config.tunnel_iface(st_cfg.VPN_GUARD_IFACES)
                    qb_status["vpn_binding"] = qb_vpn_binding_status(
                        qb.app_preferences(st_cfg), tunnel_iface,
                        patterns=st_cfg.VPN_GUARD_IFACES)
        else:
            qb_status["error"] = "QB_PASS not set"
        gateway_status = {"version": VERSION, "uptime_s": int(time.time() - START_TIME)}
        if user.is_admin:
            gateway_status.update(
                movies_dir=str(st_cfg.MOVIES_DIR),
                tv_dir=str(st_cfg.TV_DIR),
                downloads_dir=str(st_cfg.DOWNLOAD_DIR),
                sort_delete_torrent=st_cfg.SORT_DELETE_TORRENT,
            )
        guard = dict(GUARD_STATE)
        if user.is_admin:
            # Show exactly which interfaces the configured globs match, so a
            # too-broad pattern visibly produces e.g. lo, eth0, wlan0
            # instead of silently treating the first match as the VPN
            # (ISSUE 10).  Same snapshot as the rest of the status page.
            guard["matched_ifaces"] = config.tunnel_ifaces(
                st_cfg.VPN_GUARD_IFACES)
        return {
            "jellyfin": jellyfin_status(st_cfg),
            "qbittorrent": qb_status,
            "nordvpn": nordvpn.status(),
            "guard": guard,
            "gateway": gateway_status,
        }

    @app.get("/api/torrents")
    def api_torrents(user: GatewayUser = Depends(authn.require_user)):
        out = []
        for t in (qb.torrents_info() or []):
            rec = store.torrents.get(t.get("hash"), {})
            item = {
                "hash": t.get("hash"), "name": t.get("name"),
                "category": t.get("category"), "size": t.get("size"),
                "progress": t.get("progress"), "state": t.get("state"),
                "dlspeed": t.get("dlspeed"), "upspeed": t.get("upspeed"),
                "sort_status": rec.get("sort_status"),
                "cleanup_status": rec.get("cleanup_status"),
                "state_corrupt": bool(rec.get("state_corrupt")),
                "error": (rec.get("error") if user.is_admin else
                          ("sorting failed" if rec.get("error") else None)),
                "title": rec.get("title"),
                "added_by": rec.get("added_by"),
            }
            if user.is_admin:
                item["dest"] = rec.get("dest")
            out.append(item)
        return out

    @app.post("/api/add", dependencies=[Depends(authn.require_same_origin)])
    async def api_add(magnet: str | None = Form(None), kind: str = Form("auto"),
                      torrent_file: UploadFile | None = File(None),
                      user: GatewayUser = Depends(authn.require_user)):
        if not magnet and torrent_file is None:
            raise HTTPException(400, "provide a magnet link or a .torrent file")

        # Fail fast on magnet input BEFORE any qB work (ISSUE 9): bound the
        # length and the query field count so a pathologically large magnet
        # cannot force expensive urlparse()/parse_qs() work or memory.  The
        # bounded query is parsed once here and reused by the add flow.
        #
        # Single-torrent invariant: qBittorrent's 'urls' field is a
        # NEWLINE-SEPARATED list of URLs, but the whole add flow (preflight,
        # ownership, start, rollback, StateStore record, audit attribution)
        # is designed for exactly one torrent.  Any ASCII control character
        # (C0 + DEL) in the field can smuggle a second URL past that
        # protocol, so the field is normalized once (stripped) and rejected
        # outright if it still contains a control; only the normalized value
        # is parsed below and forwarded to qB.
        magnet_value = magnet.strip() if magnet else ""
        if magnet and len(magnet) > MAX_MAGNET_LEN:
            raise HTTPException(413, "magnet link exceeds the 16 KiB limit")
        if magnet and any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in magnet):
            raise HTTPException(
                400, "magnet link contains invalid control characters")
        is_magnet = bool(magnet_value) and magnet_value.lower().startswith("magnet:")
        if is_magnet:
            try:
                q = _magnet_query(magnet_value)
            except ValueError:
                raise HTTPException(400, "magnet contains too many query parameters")
            dn = (q.get("dn") or [""])[0]
        else:
            q, dn = None, None

        # One snapshot for the WHOLE add flow: the version gate, the guard
        # flag, the interface globs and the download dir all come from the
        # same generation of the configuration, and the snapshot is passed
        # through to every qB call of the flow (ISSUE 5).
        add_cfg = config.snapshot()
        # Hard gate: verify qBittorrent is 5.x BEFORE adding anything.
        # The whole add flow (the "stopped" option, the stop/start protocol,
        # the check/add race) depends on the qB 5.x API, so an unsupported
        # version must never reach /torrents/add.  force=True re-queries so
        # a qB that was replaced/restarted is detected (adds are infrequent).
        supported = await run_in_threadpool(qb.supports_gateway, force=True,
                                            cfg=add_cfg)
        if supported is None:
            raise HTTPException(502, f"qBittorrent unreachable: {qb.last_error}")
        if not supported:
            raise HTTPException(
                503,
                f"Unsupported qBittorrent {qb.app_version(cfg=add_cfg)}; "
                "Jellyfin Gateway requires qBittorrent 5.x")
        # Do not rely on the background poll: a torrent added after its last
        # pause_all() could otherwise transfer for an entire poll interval.
        if (add_cfg.VPN_GUARD
                and await run_in_threadpool(config.tunnel_iface,
                                            add_cfg.VPN_GUARD_IFACES) is None):
            raise HTTPException(503, "VPN tunnel is down; torrent was not added")

        if is_magnet:
            name = dn
            parsed = sorter.parse_release(name) if name else {"title": "", "year": None,
                                                              "season": None, "episode": None,
                                                              "kind": "movie"}
            add_kw = {"urls": magnet_value}
            mh = magnet_hashes(magnet_value)
            if not mh:
                raise HTTPException(400, "could not extract info-hash from magnet")
            h = mh["hash"]
            infohash_v1, infohash_v2, bt_version = mh["v1"], mh["v2"], mh["version"]
        elif torrent_file is not None:
            data = await torrent_file.read(MAX_TORRENT_BYTES + 1)
            if len(data) > MAX_TORRENT_BYTES:
                raise HTTPException(413, "torrent file exceeds the 4 MiB upload limit")
            try:
                info = sorter.torrent_info(data)
            except Exception as e:
                raise HTTPException(400, f"invalid .torrent file: {e}")
            name = info["name"]
            parsed = sorter.parse_release(name)
            add_kw = {"torrent_file": (torrent_file.filename or "upload.torrent",
                                       data, "application/x-bittorrent")}
            h = info["hash"]
            if not h:
                raise HTTPException(400, "invalid .torrent file: info hash not found")
            infohash_v1, infohash_v2, bt_version = (
                info["infohash_v1"], info["infohash_v2"], info["version"])
        else:
            raise HTTPException(400, "magnet must be a magnet: URI or include a .torrent file")

        k = (kind or "auto").lower()
        if k not in ("auto", "movie", "tv"):
            raise HTTPException(400, "kind must be auto, movie or tv")
        auto_note = ""
        if k == "auto":
            k = parsed.get("kind") or "movie"
            auto_note = "auto-detected" if name else "no name yet — assumed movie"

        cat = "movies" if k == "movie" else "tv"
        add_cfg.DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

        def add_to_qb():
            qb.ensure_category(cat, str(add_cfg.DOWNLOAD_DIR), cfg=add_cfg)
            return qb.add(category=cat, savepath=str(add_cfg.DOWNLOAD_DIR),
                          stopped=add_cfg.VPN_GUARD, cfg=add_cfg, **add_kw)

        # Preflight (ISSUE 3): the add response alone cannot be trusted to
        # detect duplicates across qB 5.x (5.0/5.1 answer a duplicate with
        # 200 "Fails.", 5.2 with 409 Conflict).  The gateway may only add a
        # torrent whose ABSENCE it positively verified: that is what makes
        # the ownership-gated rollback below sound.  Fail closed when the
        # preflight itself is unreadable.
        present = await run_in_threadpool(qb.torrent_present, h, cfg=add_cfg)
        if present is None:
            raise HTTPException(
                502,
                "could not verify whether torrent already exists in qBittorrent")
        if present:
            raise HTTPException(409, "torrent already exists in qBittorrent")

        r = await run_in_threadpool(add_to_qb)
        if r is None:
            raise HTTPException(502, f"qBittorrent unreachable: {qb.last_error}")
        # ISSUE 3: only a response that PROVES this request added h grants
        # ownership.  "Fails." (5.0/5.1) and 409 (5.2) mean a duplicate
        # won the race; a 202 or a mismatched 5.2 JSON means "not this
        # torrent".  None of them may ever lead to a rollback delete.
        add_owned = False
        accepted, why = _qb_add_accepted(r, h)
        if not accepted:
            if why in ("failed", "conflict"):
                raise HTTPException(
                    409, "torrent already exists or was not added")
            if why == "pending":
                raise HTTPException(
                    502,
                    "qBittorrent accepted the torrent asynchronously; "
                    "pending adds are not supported by the gateway")
            raise HTTPException(400, f"qBittorrent rejected the torrent: {why}")
        # Post-add transaction (ISSUE 1): ownership is proven, so persist
        # the PROVISIONAL record now — before verification — with a
        # durability barrier.  Every later state becomes recoverable: a
        # crash between acceptance and confirmation (or an accepted add
        # whose verification goes unreadable) leaves a pending record the
        # watcher reconciles, instead of an accepted torrent the gateway
        # forgot.
        pending_rec = store.record(
            h,
            kind=k,
            name=name,
            title=parsed.get("title"),
            parsed=parsed,
            added=time.time(),
            source="gateway",
            added_by=user.username,
            added_by_id=user.user_id,
            infohash_v1=infohash_v1, infohash_v2=infohash_v2,
            bittorrent_version=bt_version,
            add_verification="pending",
            add_start_pending=bool(add_cfg.VPN_GUARD),
            add_verification_started_at=time.time(),
        )
        store.save(required=True)
        # Confirm by observation that h actually exists BEFORE the
        # response grants ownership — everything below (guard start,
        # promotion, audit attribution) assumes the torrent is in
        # qBittorrent.
        verification = await run_in_threadpool(
            _confirm_added_torrent, qb, h, add_cfg)
        if verification == "absent":
            # Every attempt positively said "no": nothing materialized,
            # there is nothing to delete, and the provisional record is
            # removed durably.
            _discard_pending_add(store, h)
            raise HTTPException(
                502,
                "qBittorrent did not confirm the torrent was added; "
                "nothing was recorded")
        if verification == "uncertain":
            # qB accepted the add but then went unreadable: the torrent
            # may exist (stopped, under the guard).  Keep the provisional
            # record — the watcher reconciles it — and tell the client
            # the gateway has taken ownership of finishing the
            # transaction.
            store.event(
                "warn",
                f"add {h[:12]} was accepted by qBittorrent but could not be "
                f"verified; reconciliation pending",
                "Torrent add verification is pending",
            )
            return JSONResponse(
                status_code=202,
                content={"ok": True, "hash": h, "kind": k,
                         "verification": "pending"})
        add_owned = True

        started = True
        if add_cfg.VPN_GUARD:
            # Close the check/add race by adding stopped, checking the tunnel
            # again, and only then starting this exact torrent.
            if await run_in_threadpool(config.tunnel_iface,
                                       add_cfg.VPN_GUARD_IFACES) is None:
                # Ownership gate (ISSUE 3): rollback may only delete h if
                # the add response positively proved THIS request added it.
                # Not-accepted responses raise above, so add_owned is always
                # True here; the gate makes the invariant explicit and keeps
                # a future refactor from deleting a foreign torrent.
                if add_owned:
                    removed, why = await run_in_threadpool(
                        _rollback_added_torrent, qb, h, add_cfg)
                    if removed:
                        # The rollback is CONFIRMED: the torrent is gone,
                        # so the provisional transaction record is too.
                        # (Unconfirmed rollback keeps it — that is exactly
                        # the state the watcher reconciles, ISSUE 1.)
                        _discard_pending_add(store, h)
                        raise HTTPException(
                            503,
                            "VPN tunnel dropped while adding; add was rolled back")
                    store.event(
                        "error",
                        f"{user.username} add {h[:12]} failed and rollback "
                        f"could not be confirmed ({why}); torrent may remain "
                        f"stopped in qBittorrent",
                        "Torrent add failed; administrator intervention may be required",
                    )
                    raise HTTPException(
                        503,
                        "VPN tunnel dropped while adding and rollback could not "
                        "be confirmed; torrent may remain stopped in qBittorrent")
                raise HTTPException(
                    503,
                    "VPN tunnel dropped while adding; torrent ownership is "
                    "unknown and nothing was deleted")
            started = await run_in_threadpool(qb.resume_hashes, [h], cfg=add_cfg)
            if not started:
                # A failed /start does not prove the torrent stayed stopped
                # (qB may have applied it before the response was lost), so
                # only the confirmed-removed case may claim "rolled back".
                if add_owned:
                    # Ownership gate (ISSUE 3), same rule as above.
                    removed, why = await run_in_threadpool(
                        _rollback_added_torrent, qb, h, add_cfg)
                    if removed:
                        # Confirmed-removed: nothing left to reconcile.
                        _discard_pending_add(store, h)
                        raise HTTPException(
                            502,
                            "qBittorrent did not confirm that the torrent started; "
                            "add was rolled back")
                    store.event(
                        "error",
                        f"{user.username} add {h[:12]} failed and rollback "
                        f"could not be confirmed ({why}); torrent may remain in "
                        f"qBittorrent and its running/stopped state is unknown",
                        "Torrent add failed; administrator intervention may be required",
                    )
                    raise HTTPException(
                        502,
                        "qBittorrent did not confirm that the torrent started "
                        "and rollback could not be confirmed; torrent may remain "
                        "in qBittorrent and its running/stopped state is unknown")
                raise HTTPException(
                    502,
                    "qBittorrent did not confirm that the torrent started; "
                    "torrent ownership is unknown and nothing was deleted")

        # The start requirement is satisfied: finish the transaction by
        # promoting the provisional record (durable barrier save — the
        # record must be on disk before the response claims success).
        _promote_verified_add(store, h, pending_rec)
        note = f" ({auto_note})" if auto_note else ""
        store.event("info", f"{user.username} added “{name or magnet_value[:60]}” as {k}{note}")
        result = {"ok": True, "hash": h, "kind": k, "parsed": parsed, "name": name,
                  "note": auto_note, "category": cat, "added_by": user.username,
                  "started": started}
        if user.is_admin:
            result["save_path"] = str(add_cfg.DOWNLOAD_DIR)
        return result

    @app.post("/api/torrents/{h}/sort", dependencies=[Depends(authn.require_same_origin)])
    def api_sort(h: str, user: GatewayUser = Depends(authn.require_admin)):
        # One snapshot for the whole manual-sort request: the qB lookup,
        # the disappeared-torrent fallback save path and the sort
        # operation itself use the same generation (ISSUE 5).
        sort_cfg = config.snapshot()
        torrents = qb.torrents_info(sort_cfg)
        t = next((x for x in (torrents or []) if x.get("hash") == h), None)
        rec = store.torrents.get(h)
        if rec is not None and rec.get("state_corrupt"):
            # ISSUE 10: the persisted transaction state is untrustworthy.
            # Neither an automatic resume nor a manual retry may execute
            # it; an administrator repairs the state file.
            raise HTTPException(
                409, "persisted sort state is corrupt; administrator "
                     "repair required")
        if t is None:
            # The torrent is gone from qBittorrent. A FRESH sort is
            # impossible without it, but an interrupted one (a persisted
            # plan) can still be finished: the resume is pure local file
            # work, and the finish step treats an absent delete as
            # already satisfied.
            if not (rec is not None
                    and (rec.get("sort_status") == "incomplete"
                         or rec.get("sort_plan"))):
                raise HTTPException(404, "torrent not found in qBittorrent")
            t = {"hash": h, "name": rec.get("name") or "",
                 "save_path": str(sort_cfg.DOWNLOAD_DIR)}
        if rec is None:
            name = t.get("name") or ""
            parsed = sorter.parse_release(name)
            rec = store.record(h, kind="tv" if (t.get("category") or "").lower() == "tv" else "movie",
                               name=name, title=parsed.get("title"), parsed=parsed,
                               added=time.time(), source="manual")
        if rec.get("sort_status") == "sorted":
            raise HTTPException(409, "already sorted")
        if rec.get("sort_status") in ("sorting", "planning"):
            raise HTTPException(409, "sort already in progress")
        if not rec.get("sort_plan") and not is_complete(t):
            # Not a sorting failure: the torrent simply isn't ready yet.
            # Leave the persistent record untouched (no "sorting", no
            # "failed") so the background watcher can still auto-sort it
            # once the download completes. (A saved plan does not depend
            # on the download state: its paths are absolute and its
            # progress is recorded op-by-op.)
            raise HTTPException(409, "torrent is not complete yet")
        # Explicit manual retry: allow_failed=True lets the admin recover
        # a genuinely failed sort after fixing the cause. The automatic
        # watcher keeps the default (False), so real failures stay
        # terminal for it.
        outcome = try_sort(qb, store, t, rec, allow_failed=True, cfg=sort_cfg)
        if outcome in ("busy", "terminal") \
                and rec.get("sort_status") in ("sorting", "planning"):
            raise HTTPException(409, "sort already in progress")
        return {"ok": rec.get("sort_status") == "sorted", "status": rec.get("sort_status"),
                "cleanup_status": rec.get("cleanup_status"),
                "dest": rec.get("dest"), "error": rec.get("error")}

    @app.get("/api/sortlog")
    def api_sortlog(user: GatewayUser = Depends(authn.require_user)):
        events = list(reversed(store.events))
        if user.is_admin:
            out = []
            for event in events:
                item = dict(event)
                item.pop("public_msg", None)
                out.append(item)
            return out
        public = []
        for event in events:
            item = dict(event)
            msg = item.pop("public_msg", None)
            if msg is None:
                # Backward-compatible redaction for logs written by an older
                # Gateway version, before public_msg was stored explicitly.
                msg = str(item.get("msg") or "")
                if " → /" in msg:
                    msg = msg.split(" → /", 1)[0] + " → library"
                elif re.search(r"(^|[\s(])/(?!/)", msg):
                    msg = "Gateway activity recorded (details available to an administrator)"
            item["msg"] = msg
            public.append(item)
        return public

    @app.post("/api/vpn", dependencies=[Depends(authn.require_same_origin)])
    async def api_vpn(payload: dict, user: GatewayUser = Depends(authn.require_admin)):
        action = (payload.get("action") or "").lower()
        if action == "connect":
            return await run_in_threadpool(nordvpn.connect)
        if action == "disconnect":
            return await run_in_threadpool(nordvpn.disconnect)
        raise HTTPException(400, "action must be connect or disconnect")

    @app.post("/api/qbittorrent/bind-vpn",
              dependencies=[Depends(authn.require_same_origin)])
    def api_qb_bind_vpn(user: GatewayUser = Depends(authn.require_admin)):
        # One snapshot: tunnel detection and the binding check use the
        # same interface globs (ISSUE 5).
        bind_cfg = config.snapshot()
        tunnel_iface = config.tunnel_iface(bind_cfg.VPN_GUARD_IFACES)
        if tunnel_iface is None:
            raise HTTPException(409, "Connect the VPN before binding qBittorrent")
        if not qb.set_preferences({
                "current_network_interface": tunnel_iface,
                "current_interface_address": "",
        }, cfg=bind_cfg):
            detail = qb.last_error or "setPreferences returned an error"
            raise HTTPException(502, f"qBittorrent rejected the VPN binding: {detail}")
        binding = qb_vpn_binding_status(qb.app_preferences(bind_cfg), tunnel_iface,
                                        patterns=bind_cfg.VPN_GUARD_IFACES)
        if not binding["checked"] or not binding["safe"]:
            raise HTTPException(502, "qBittorrent did not retain the VPN interface binding")
        store.event("info", f"qBittorrent bound to VPN interface {tunnel_iface}")
        return {"ok": True, "vpn_binding": binding}

    @app.post("/api/refresh-jellyfin", dependencies=[Depends(authn.require_same_origin)])
    def api_refresh(user: GatewayUser = Depends(authn.require_admin)):
        # One snapshot: the API-key gate and the refresh request use the
        # same generation of the Jellyfin URL and key (ISSUE 5).
        ref_cfg = config.snapshot()
        # The manual button explicitly attempts a refresh even when
        # NOTIFY_JELLYFIN is False: that toggle means "automatically
        # notify after sorting", not "disable the administrator's manual
        # refresh button".
        if not ref_cfg.JELLYFIN_API_KEY:
            raise HTTPException(409, "Jellyfin API key is not configured")
        result = jellyfin_refresh(ref_cfg)
        if not result.accepted:
            raise HTTPException(502, result.error
                                or "Jellyfin rejected the refresh request")
        store.event("info", "manual Jellyfin library refresh requested")
        return {"ok": True, "accepted": True, "notified": True,
                "status_code": result.status_code}

    # --- settings (dashboard "Settings" panel) ---

    @app.get("/api/config")
    def api_config_get(user: GatewayUser = Depends(authn.require_admin)):
        return config.current_values()

    @app.post("/api/config", dependencies=[Depends(authn.require_same_origin)])
    def api_config_post(payload: dict, user: GatewayUser = Depends(authn.require_admin)):
        try:
            res = config.apply_settings(payload or {})
        except ValueError as e:
            raise HTTPException(400, str(e))
        changed = set(res["applied"])
        if changed & {"QB_URL", "QB_USER", "QB_PASS"}:
            qb.reset()
        # NOTE (ISSUE 9): no manual invalidation here.  The derived init
        # key (the exact values a successful init used) IS the
        # invalidation mechanism — if any of those values changed, the
        # current snapshot's key no longer matches the stored one and
        # the next ensure_qb_init() runs for the new configuration.
        if changed & {"VPN_GUARD", "VPN_GUARD_IFACES"}:
            # Live toggle semantics (issue #8): do NOT just flip the
            # dashboard flag — immediately run the guard action, serialized
            # with the guard thread so it cannot race a regular tick.
            # The settings change itself is already persisted; if the
            # immediate reaction fails (should be near-impossible), log it
            # and let the guard loop catch up on its next tick, exactly like
            # a failed regular tick.
            try:
                apply_guard_change(guard, guard_tick_lock, changed)
            except Exception:
                log.exception("immediate guard reaction failed; the guard loop will catch up on its next tick")
        if "JELLYFIN_URL" in changed:
            # Tokens are scoped to the server that issued them. Keep the
            # current request valid, but require everybody to log in again.
            authn.clear()
        if "MEDIA_ROOT" in changed or changed & {"MOVIES_DIR", "TV_DIR",
                                                 "DOWNLOAD_DIR"}:
            # One snapshot: all three dirs are created from the same
            # generation of the layout (ISSUE 5).
            media_cfg = config.snapshot()
            for d in (media_cfg.DOWNLOAD_DIR, media_cfg.MOVIES_DIR,
                      media_cfg.TV_DIR):
                d.mkdir(parents=True, exist_ok=True)
        # Note: category re-pointing after a DOWNLOAD_DIR change is
        # handled by the keyed ensure_qb_init() (its derived key no
        # longer matches the current snapshot), so it never writes to an
        # unsupported qB instance.
        # Trigger the (keyed, locked) qB 5.x init immediately for any
        # qB-affecting change so the categories point at the new
        # endpoint/directory without waiting for the next watcher poll.
        if changed & QB_INIT_FIELDS:
            ensure_qb_init()
        if changed:
            store.event("info", "settings updated: " + ", ".join(res["applied"]))
        return res

    # --- folder picker (used by the Settings "Browse…" buttons) ---

    @app.get("/api/browse")
    def api_browse(path: str = "/", user: GatewayUser = Depends(authn.require_admin)):
        p = Path(path).expanduser()
        if not p.exists():
            raise HTTPException(404, f"no such path: {path}")
        if not p.is_dir():
            raise HTTPException(400, "not a directory")
        try:
            dirs = [{"name": d.name, "path": str(d)}
                    for d in sorted(p.iterdir(), key=lambda x: x.name.lower())
                    if d.is_dir()]
        except PermissionError:
            raise HTTPException(403, f"permission denied: {path}")
        quick, seen = [], set()
        candidates = ["/", str(Path.home())]
        for base in ("/media", "/mnt"):
            try:
                candidates.extend(str(d) for d in sorted(Path(base).iterdir()) if d.is_dir())
            except OSError:
                pass
        for q in candidates:
            if q not in seen:
                seen.add(q)
                quick.append(q)
        return {"path": str(p),
                "parent": None if str(p) == "/" else str(p.parent),
                "home": str(Path.home()),
                "quick": quick,
                "dirs": dirs}

    # Outermost middleware: cap request bodies BEFORE Starlette buffers
    # them, so oversized bodies are rejected at the transport edge
    # (ISSUE 9: /api/add; ISSUE 7: /api/auth/login, an unauthenticated
    # route; /api/vpn and /api/config: the admin JSON routes, where the
    # body cap is the only bound on what an unauthenticated caller can
    # make the server buffer before auth rejects the request).
    # add_middleware() inserts at the front, so registering it last makes
    # it the first to run.
    app.add_middleware(_BodyCap, caps={
        ("POST", "/api/add"): MAX_ADD_BODY_BYTES,
        ("POST", "/api/auth/login"): MAX_LOGIN_BODY_BYTES,
        ("POST", "/api/vpn"): MAX_VPN_BODY_BYTES,
        ("POST", "/api/config"): MAX_CONFIG_BODY_BYTES,
    })

    return app


# --- process identity ---------------------------------------------------
# The gateway should be easy to spot in ps/htop/top/systemctl.  "jellyfingateway"
# is exactly 15 chars, the kernel's comm field limit on Linux.
PROCESS_TITLE = "jellyfingateway"


def set_process_title(name: str = PROCESS_TITLE) -> None:
    """Make the running gateway show up as "jellyfingateway" in ps/htop/top.

    Two independent parts, each best-effort (neither may break startup):
      * the command line (ps "args" column): rewritten via setproctitle;
      * the kernel task name (ps "comm" column, the Command column of
        htop/top): written to /proc/self/comm (Linux only, <= 15 chars).
    """
    try:
        import setproctitle
    except ImportError:
        log.warning("setproctitle is not installed; the ps 'args' column "
                    "will still show the interpreter")
    else:
        try:
            setproctitle.setproctitle(name)
        except Exception as exc:  # pragma: no cover - platform dependent
            log.warning("could not rewrite the process command line: %s", exc)
    try:
        with open("/proc/self/comm", "w") as f:
            f.write(name)
    except OSError:
        pass  # not Linux


app = create_app()


if __name__ == "__main__":
    set_process_title()
    import uvicorn
    uvicorn.run(app, host=config.GATEWAY_HOST, port=config.GATEWAY_PORT, log_level="info")
