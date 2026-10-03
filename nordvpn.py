"""NordVPN status/actions via the `nordvpn` CLI."""
import re
import shutil
import subprocess
import threading
import time
from typing import Optional

import config

STATUS_CACHE_TTL = 5.0
# 10 minutes: subscription state changes rarely and the account CLI call
# can take 25s, so it must not re-run on every 5s status refresh (ISSUE 7).
ACCOUNT_CACHE_TTL = 600.0

# Status cache: {ts, value}.  _cache_lock guards fast reads/writes/
# invalidation only; it is NEVER held across a subprocess.
_cache = {"ts": 0.0, "value": None}
_cache_lock = threading.Lock()
# Single-flight: at most one thread runs `nordvpn status` at a time;
# concurrent callers block here, then re-check the cache.
_status_refresh_lock = threading.Lock()

# Account cache: same single-flight pattern with its own locks and a
# much longer TTL.  Both successful and failed parses are cached so a
# broken account command cannot re-run the 25s CLI every 5s.
_account_cache = {"ts": 0.0, "value": None}
_account_cache_lock = threading.Lock()
_account_refresh_lock = threading.Lock()


def available() -> bool:
    return shutil.which(config.NORDVPN_BIN) is not None


def _run(*args, timeout=15) -> tuple[int, str]:
    # config.NORDVPN_BIN is validated and resolved to a concrete executable
    # at startup (config._resolve_nordvpn_bin) and is never dashboard-editable.
    try:
        p = subprocess.run([config.NORDVPN_BIN, *args], capture_output=True,
                           text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return 1, str(e)


def _status_fresh() -> Optional[dict]:
    with _cache_lock:
        if _cache["value"] is not None and time.time() - _cache["ts"] < STATUS_CACHE_TTL:
            return _cache["value"]
    return None


def _fetch_status() -> dict:
    rc, out = _run("status")
    connected = "Status: Connected" in out
    server = None
    if connected:
        m = re.search(r"Server:\s*(.+?)(?:\n|$)", out)
        if m:
            server = m.group(1).strip()
            country = m.group(1).strip().split(" (")[-1].rstrip(")")
            m2 = re.match(r"(.+?)\s*\(([^)]+)\)", server)
            if m2:
                server, country = m2.group(1).strip(), m2.group(2).strip()
            else:
                country = ""
    else:
        country = ""

    return {
        "available": True,
        "connected": connected,
        "server": server,
        "country": country,
        "tunnel_iface": config.tunnel_iface(),
        "raw": out.strip().splitlines()[:3],
        "account": _account(),
    }


def status() -> dict:
    """Current VPN status (cached 5s: the CLI is slow-ish).

    Single-flight (ISSUE 7): the freshness check is a fast path under
    _cache_lock; a stale cache makes this thread block on
    _status_refresh_lock and RE-CHECK the cache before running the CLI,
    so a refresh that started ahead of it is reused instead of
    duplicated.
    """
    cached = _status_fresh()
    if cached is not None:
        return cached
    if not available():
        return {"available": False, "connected": False,
                "error": f"{config.NORDVPN_BIN} not found"}
    with _status_refresh_lock:
        cached = _status_fresh()
        if cached is not None:
            return cached
        value = _fetch_status()
        with _cache_lock:
            _cache["ts"] = time.time()
            _cache["value"] = value
        return value


def _account_fresh() -> Optional[dict]:
    with _account_cache_lock:
        if (_account_cache["value"] is not None
                and time.time() - _account_cache["ts"] < ACCOUNT_CACHE_TTL):
            return _account_cache["value"]
    return None


def _fetch_account() -> dict:
    rc, out = _run("account", "status", timeout=25)
    acct = {"active": None, "expiry": None}
    if rc == 0:
        if re.search(r"Subscription:\s*active", out, re.I):
            acct["active"] = True
        elif re.search(r"Subscription:\s*inactive", out, re.I):
            acct["active"] = False
        m = re.search(r"Subscription expiry date:\s*(.+?)(?:\n|$)", out)
        if m:
            acct["expiry"] = m.group(1).strip()
    return acct


def _account() -> dict:
    """Account info, cached for ACCOUNT_CACHE_TTL; single-flight.

    The same double-check pattern as status(): a waiting caller re-checks
    the account cache after the refresh lock and only runs the CLI if it
    is still stale.
    """
    cached = _account_fresh()
    if cached is not None:
        return cached
    with _account_refresh_lock:
        cached = _account_fresh()
        if cached is not None:
            return cached
        value = _fetch_account()
        with _account_cache_lock:
            _account_cache["ts"] = time.time()
            _account_cache["value"] = value
        return value


def _invalidate_status() -> None:
    # All status-cache mutations go through _cache_lock (ISSUE 7).
    # The account cache is untouched: connecting/disconnecting does not
    # change subscription state.
    with _cache_lock:
        _cache["ts"] = 0.0
        _cache["value"] = None


def _invalidate_after_action() -> None:
    """Invalidate the status cache after connect()/disconnect().

    The action's own CLI runs WITHOUT the refresh lock, but the
    invalidation first acquires _status_refresh_lock (ISSUE 7): an
    in-flight status() refresh is therefore guaranteed to finish and
    publish BEFORE we clear the cache, so it cannot republish its
    pre-action result with a fresh timestamp afterwards.
    """
    with _status_refresh_lock:
        _invalidate_status()


def connect() -> dict:
    if not available():
        return {"ok": False, "error": f"{config.NORDVPN_BIN} not found"}
    rc, out = _run("connect", timeout=90)
    _invalidate_after_action()
    return {"ok": rc == 0, "output": out.strip()[:500]}


def disconnect() -> dict:
    if not available():
        return {"ok": False, "error": f"{config.NORDVPN_BIN} not found"}
    rc, out = _run("disconnect", timeout=60)
    _invalidate_after_action()
    return {"ok": rc == 0, "output": out.strip()[:500]}
