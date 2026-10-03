"""Jellyfin status / refresh client (ISSUE 6, phase 2.5).

Pure move out of gateway.py: reaching the configured Jellyfin server
(status probe, media refresh) plus the local-network helpers it uses.
"""
import config
import httpx
import socket
import subprocess
import time
from dataclasses import dataclass
from urllib.parse import urlparse

from service_utils import _safe_service_error

# Jellyfin
# ---------------------------------------------------------------------------

_SKIP_IFACE_KEYS = ("lo", "tailscale", "tun", "tap", "nordlynx", "nordvpn",
                    "docker", "br-", "veth", "virbr", "wg-")


def _local_ipv4() -> str:
    """Best-effort physical LAN IPv4 (skips loopback/tunnel/VPN ifaces)."""
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show"],
                             capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines():
            parts = line.split()  # ifindex ifname inet addr/prefix brd ...
            if len(parts) < 4:
                continue
            ifname = parts[1]
            if any(k in ifname for k in _SKIP_IFACE_KEYS):
                continue
            addr = parts[3].split("/")[0]
            if addr.startswith(("127.", "169.254.")):
                continue
            return addr
    except Exception:
        pass
    # Fallback: default-route source address (correct when no VPN tunnel up).
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 65530))  # no packet is actually sent
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        return ""


_TS_IP_CACHE: dict = {"ip": "", "ts": 0.0}


def _tailscale_ipv4() -> str:
    """This node's tailnet IPv4 (`tailscale ip -4`), cached for 30s."""
    now = time.time()
    if _TS_IP_CACHE["ip"] and now - _TS_IP_CACHE["ts"] < 30:
        return _TS_IP_CACHE["ip"]
    ip = ""
    try:
        ip = subprocess.run(["tailscale", "ip", "-4"], capture_output=True,
                            text=True, timeout=5).stdout.strip()
    except Exception:
        pass
    _TS_IP_CACHE.update(ip=ip, ts=now)
    return ip


def jellyfin_status(cfg=None) -> dict:
    # One snapshot: URL and API key come from the same generation
    # (ISSUE 5).
    if cfg is None:
        cfg = config.snapshot()
    st = {"online": False, "url": config.public_service_url(cfg.JELLYFIN_URL)}
    headers = {"X-Emby-Token": cfg.JELLYFIN_API_KEY} if cfg.JELLYFIN_API_KEY else {}
    try:
        # /system/info: 200 with key, 401 after wizard without key (still online), 404 = dead
        r = httpx.get(f"{cfg.JELLYFIN_URL}/system/info", timeout=3, headers=headers)
        st["online"] = r.status_code in (200, 401)
        if r.status_code == 200:
            d = r.json()
            st["version"] = d.get("Version")
            st["server_name"] = d.get("ServerName")
    except Exception as e:
        st["error"] = _safe_service_error(e, cfg.JELLYFIN_URL, limit=160)
        return st
    if st["online"]:
        try:
            r = httpx.get(f"{cfg.JELLYFIN_URL}/items",
                          params={"Recursive": "true", "Limit": "1"},
                          headers=headers, timeout=8)
            st["item_count"] = r.json().get("TotalRecordCount")
        except Exception:
            pass
        # Convenience links for a Jellyfin that runs on this very machine:
        # the LAN address and the tailnet address of the same server.
        ju = urlparse(cfg.JELLYFIN_URL)
        if (ju.hostname or "localhost") in ("localhost", "127.0.0.1", "::1"):
            port = ju.port or 8096
            lan, ts = _local_ipv4(), _tailscale_ipv4()
            if lan:
                st["lan_url"] = f"http://{lan}:{port}"
            if ts:
                st["tailscale_url"] = f"http://{ts}:{port}"
    return st


@dataclass
class JellyfinRefreshResult:
    """Concrete result of a Jellyfin /Library/Refresh request.

    accepted: True only when the HTTP request was made and Jellyfin
      returned a 2xx status.  The API call starts a library scan;
      acceptance is not the same as the scan having finished.
    status_code: the HTTP status code, or None when no request was made
      (no API key) or the connection failed.
    error: a human-readable reason when accepted is False.
    """
    accepted: bool
    status_code: int | None = None
    error: str | None = None


def jellyfin_refresh(cfg=None) -> JellyfinRefreshResult:
    """Ask a (keyed) Jellyfin to refresh its libraries.

    Always reports a concrete result.  Does NOT consult NOTIFY_JELLYFIN:
    that flag is the automatic-refresh policy (used by _finish_sort),
    not a gate on the low-level request.  The manual /api/refresh-jellyfin
    endpoint calls this directly so an administrator can trigger a
    refresh even when automatic refresh is disabled.
    """
    # One snapshot: URL and API key come from the same generation
    # (ISSUE 5).
    if cfg is None:
        cfg = config.snapshot()
    if not cfg.JELLYFIN_API_KEY:
        return JellyfinRefreshResult(
            False, error="Jellyfin API key is not configured")
    try:
        r = httpx.post(f"{cfg.JELLYFIN_URL}/Library/Refresh",
                       headers={"X-Emby-Token": cfg.JELLYFIN_API_KEY},
                       timeout=10)
    except Exception as exc:
        return JellyfinRefreshResult(
            False, error="Jellyfin unreachable: "
              + _safe_service_error(exc, cfg.JELLYFIN_URL))
    if 200 <= r.status_code < 300:
        return JellyfinRefreshResult(True, status_code=r.status_code)
    return JellyfinRefreshResult(
        False, status_code=r.status_code,
        error=f"Jellyfin returned HTTP {r.status_code}")


