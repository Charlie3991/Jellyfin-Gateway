#!/usr/bin/env python3
"""
qbittorrent-vpn-guard — standalone VPN guard for qBittorrent.

Pauses active qBittorrent torrents when the VPN tunnel interface disappears
and resumes only those torrents when it comes back. Works without the Gateway:
stdlib-only, talks to the qBittorrent WebUI over HTTP.

Fail-closed: if the tunnel is already down when the guard starts (or if
qBittorrent restarts and resurrects torrents while the VPN is down),
torrents are (re)paused.

The `Guard` class below is the single source of truth for this kill switch:
the Jellyfin Gateway (gateway.py) runs the exact same logic in-process, so
the behaviour is identical whether you use this service or the gateway.

Configuration (environment variables):
  QB_URL        qBittorrent WebUI base URL   (default http://127.0.0.1:8090)
  QB_USER       WebUI username               (default admin)
  QB_PASS       WebUI password               (required)
  TUNNEL_IFACES comma-separated fnmatch globs for the tunnel interface
                (default "nordlynx*,tun*,tap*")
  GUARD_POLL    seconds between checks       (default 5)

Run manually:  python3 vpn_guard.py [--once]
As a service:  see qbittorrent-vpn-guard.service (systemd user unit)
"""
import argparse
import fnmatch
import http.cookiejar
import json
import logging
import math
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

QB_URL = os.environ.get("QB_URL", "http://127.0.0.1:8090").rstrip("/")
QB_USER = os.environ.get("QB_USER", "admin").strip()
QB_PASS = os.environ.get("QB_PASS", "").strip()
IFACE_GLOBS = [g.strip() for g in
               os.environ.get("TUNNEL_IFACES", "nordlynx*,tun*,tap*").split(",") if g.strip()]
def _parse_guard_poll(raw):
    """Parse GUARD_POLL at import time WITHOUT crashing (ISSUE 8 parity).

    Returns a finite, > 0 float, or None when the value is missing,
    non-numeric, non-finite or <= 0.  main() turns None (or any other
    invalid global) into a clean exit-2 refusal instead of an import
    traceback or a pathological sleep(nan)/sleep(inf) loop.
    """
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v) or v <= 0:
        return None
    return v


POLL = _parse_guard_poll(os.environ.get("GUARD_POLL", "5"))
# where the guard remembers "I paused everything" so a restart/resume cycle
# (e.g. machine rebooted while the VPN was down) still resumes correctly
STATE_FILE = pathlib.Path(os.environ.get(
    "QB_GUARD_STATE_FILE",
    str(pathlib.Path.home() / ".local/state/qb-vpn-guard/state.json")))

log = logging.getLogger("vpn-guard")

# states where a torrent counts as "active" (not paused/stopped) for the
# fail-closed self-heal check
INACTIVE_STATES = {"pausedDL", "pausedUP", "stopped", "stoppedDL", "stoppedUP",
                   "error", "missing", "unknown"}


def _parse_qb_major(version: str | None) -> int | None:
    """Major version from a qBittorrent version string, or None.

    Accepts the documented "v5.2.3" form, a bare "5.0.0", or a bare major
    "6".  Malformed strings return None (treated as unknown, never as a
    supported version).
    """
    if not version:
        return None
    m = re.match(r"^v?(\d+)(?:\.|$)", version.strip())
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# tunnel detection
# ---------------------------------------------------------------------------

# Interface names that are NEVER a P2P VPN tunnel.  A TUNNEL_IFACES glob that
# matches any of these is too broad: it would let the guard treat a regular
# (usually always-up) interface as "the VPN" and silently defeat the kill
# switch.  (tailscale0 is an overlay, not a P2P VPN, per the default.)
# Duplicate of config.VPN_IFACE_SENTINELS (ISSUE 10 parity): this module is
# the standalone service and must stay self-contained — gateway.py imports
# it, so it must not import the gateway's config module back.
VPN_IFACE_SENTINELS = (
    "lo", "eth0", "enp1s0", "ens3", "wlan0", "wlp1s0",
    "docker0", "br0", "tailscale0",
    # more common non-VPN families (physical/bridge/virtual/wwan)
    "eno1", "enx001122334455", "bond0", "virbr0", "veth0",
    "cni0", "podman0", "wwan0",
)


def validate_vpn_iface_patterns(raw) -> list:
    """Validate TUNNEL_IFACES globs; return the cleaned pattern list.

    Duplicate of config.validate_vpn_iface_patterns() (ISSUE 10 parity): the
    same rules apply to the standalone service, so a too-broad pattern is
    rejected no matter which binary is running.  Raises ValueError if the
    list is empty or if any glob matches a sentinel interface that is
    definitely not a P2P VPN tunnel (a pattern such as '*' would otherwise
    match the always-up loopback and neuter the kill switch).
    """
    if isinstance(raw, str):
        pats = [p.strip() for p in raw.split(",") if p.strip()]
    else:
        pats = [str(p).strip() for p in raw if str(p).strip()]
    if not pats:
        raise ValueError(
            "TUNNEL_IFACES: give at least one glob (e.g. tun*,nordlynx*)")
    for pat in pats:
        for sentinel in VPN_IFACE_SENTINELS:
            if fnmatch.fnmatch(sentinel, pat):
                raise ValueError(
                    f"TUNNEL_IFACES: pattern '{pat}' is too broad; it "
                    f"matches non-VPN interface '{sentinel}'")
    return pats


def _parse_ip_link_line(line: str) -> tuple[str, set[str]] | None:
    """Parse one `ip -o link` line into (interface_name, interface_flags).

    Duplicate of config._parse_ip_link_line() (ISSUE 10 parity).  Returns
    None for malformed/unrecognized output.
    """
    parts = line.split(":", 2)
    if len(parts) < 3:
        return None

    name = parts[1].strip()
    details = parts[2]

    if not name:
        return None

    # iproute2 may display veth-style names as "eth0@if123".
    # "@if123" describes the peer, not the actual interface name.
    name = name.split("@", 1)[0]

    match = re.match(r"\s*<([^>]*)>", details)
    if not match:
        return None

    flags = {
        flag.strip()
        for flag in match.group(1).split(",")
        if flag.strip()
    }

    return name, flags


def tunnel_up() -> str | None:
    """Name of the first UP interface matching IFACE_GLOBS, else None.

    Parity with the gateway's config.tunnel_ifaces() (ISSUE 10): strict
    `ip -o link` parsing (malformed lines ignored, veth peer suffixes
    stripped), EXACT "UP" flag membership — not a substring over the raw
    line, so "LOWER_UP" alone or an incidental "UP" substring can never
    count as up — and fail-closed on any scan failure: a missing `ip`, a
    timeout, an exception, or a nonzero exit means the tunnel is UNKNOWN,
    which the guard treats as down (it pauses active torrents), never as
    up.  Note: IFACE_GLOBS here is the RAW env-derived list; the
    validator is applied in main() before this function is ever used.
    """
    try:
        result = subprocess.run(
            ["ip", "-o", "link"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return None

    if result.returncode != 0:
        return None

    for line in result.stdout.splitlines():
        parsed = _parse_ip_link_line(line)
        if parsed is None:
            continue
        name, flags = parsed
        if "UP" not in flags:
            continue
        if any(fnmatch.fnmatch(name, pat) for pat in IFACE_GLOBS):
            return name
    return None


# ---------------------------------------------------------------------------
# qBittorrent WebUI client (stdlib only)
# ---------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect.

    The gateway's QBClient sends requests with follow_redirects=False,
    so a qB URL that redirects (reverse proxy, wrong host) is treated
    as unreachable.  The standalone client must match: by default
    urllib follows 3xx even to another host, so a misconfigured QB_URL
    could send the session cookie — and, after a redirected login,
    the credentials themselves — to a different server.  Refusing the
    redirect lets the 3xx surface as an HTTPError in _req(), which
    treats it like any other non-401/403 failure: the target is
    offline.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class QB:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            _NoRedirect(), urllib.request.HTTPCookieProcessor(self.jar))

    def _req(self, method: str, path: str, data: bytes | None = None,
             allow_reauth: bool = True) -> urllib.response.addinfourl | None:
        req = urllib.request.Request(QB_URL + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            return self.opener.open(req, timeout=15)
        except urllib.error.HTTPError as e:
            if e.code not in (401, 403):
                return None
            # A 401/403 means the session cookie is missing/expired. Re-auth
            # at most ONCE per call: the login endpoint itself must be
            # non-reauthenticating, otherwise a rejected login recurses
            # forever (login -> _req 401 -> login -> ...).
            if not allow_reauth:
                return None
            self.jar.clear()
            if not self.login():
                return None
            try:
                return self.opener.open(req, timeout=15)
            except Exception:
                return None
        except Exception as e:
            safe_err = str(e).replace(QB_URL, _public_url(QB_URL))
            log.debug("request %s failed: %s", path, safe_err)
            return None

    def login(self) -> bool:
        data = urllib.parse.urlencode({"username": QB_USER, "password": QB_PASS}).encode()
        r = self._req("POST", "/api/v2/auth/login", data, allow_reauth=False)
        ok = r is not None and r.status in (200, 204) and r.read().decode(errors="replace").strip() != "Fails."
        if not ok:
            log.warning("qBittorrent login failed (%s)", _public_url(QB_URL))
        return ok

    def app_version(self) -> str | None:
        r = self._req("GET", "/api/v2/app/version")
        if r is None or r.status != 200:
            return None
        return r.read().decode(errors="replace").strip()

    def supports_guard(self) -> bool | None:
        """True if qBittorrent is exactly 5.x, False if a different major
        is reachable (or the version text is unparsable), None if the
        version could not be determined.  Matches gateway.QBClient
        semantics: the guard requires major 5 (not >= 5) — 4.x uses the
        old pause/resume endpoints and 6.x is untested, so an unconfirmed
        version must never authorize a stop/start call.
        """
        version = self.app_version()
        if version is None:
            return None
        return _parse_qb_major(version) == 5

    def online(self) -> bool:
        return self.app_version() is not None

    def _torrents_action(self, verb: str, hashes: str = "all") -> bool:
        # Never issue stop/start unless 5.x was JUST confirmed: a fresh
        # lookup per action, so a qB restart/reconnect to an unsupported
        # version is caught before the next torrent-control call.
        # The default "all" is qBittorrent's real all-torrents token (qB
        # 5.0-5.2); any other value is parsed as torrent IDs, so a
        # fabricated token would silently affect nothing (ISSUE 2).
        if self.supports_guard() is not True:
            return False
        # No eager login: _req() re-authenticates on demand when the cookie
        # is expired, so a valid session does not trigger a login attempt.
        r = self._req("POST", f"/api/v2/torrents/{verb}",
                      urllib.parse.urlencode({"hashes": hashes}).encode())
        # qB 5.0/5.1 answer a successful mutation with 200 "Ok."; 5.2 with
        # 204 No Content — both are success (ISSUE 1 fixed the gateway
        # client; this is the standalone's parity fix).
        return r is not None and r.status in (200, 204)

    def pause_all(self) -> bool:
        return self._torrents_action("stop")

    def resume_all(self) -> bool:
        return self._torrents_action("start")

    def pause_hashes(self, hashes: list[str]) -> bool:
        return not hashes or self._torrents_action("stop", "|".join(hashes))

    def resume_hashes(self, hashes: list[str]) -> bool:
        return not hashes or self._torrents_action("start", "|".join(hashes))

    def active_hashes(self) -> list[str] | None:
        r = self._req("GET", "/api/v2/torrents/info")
        if r is None or r.status != 200:
            return None
        try:
            torrents = json.loads(r.read().decode())
        except Exception:
            return None
        return [t["hash"] for t in torrents
                if t.get("hash") and (t.get("state") or "") not in INACTIVE_STATES]

    def any_active(self) -> bool | None:
        """True/False if we could check, None if qBittorrent unreachable."""
        hashes = self.active_hashes()
        return None if hashes is None else bool(hashes)


# ---------------------------------------------------------------------------
# URL sanitization (local, self-contained)
# ---------------------------------------------------------------------------

def _public_url(value: str) -> str:
    """Return a URL safe to expose in logs/events.
    
    Any userinfo is stripped. Malformed URLs are not echoed back.
    """
    from urllib.parse import urlsplit, urlunsplit
    
    value = str(value)
    
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "<invalid URL>"
    
    if not parsed.netloc:
        return "<invalid URL>" if "@" in value else value
    
    netloc = parsed.netloc.rsplit("@", 1)[-1]
    
    return urlunsplit((
        parsed.scheme,
        netloc,
        parsed.path,
        parsed.query,
        parsed.fragment,
    ))


# ---------------------------------------------------------------------------
# guard
# ---------------------------------------------------------------------------

_UNSET = object()   # sentinel: "no explicit tunnel was supplied"


class Guard:
    """Tunnel kill-switch for qBittorrent — shared by the standalone service
    and the Jellyfin Gateway (single source of truth for the pause/resume
    logic).

    Dependency-injected so the gateway can reuse it with its own client and
    config:
      qb         qB client; modern clients expose active_hashes(),
                 pause_hashes() and resume_hashes() so manual pauses survive
      tunnel_up  callable() -> tunnel interface name, or None if no tunnel
      state_file optional Path; remembers "we paused everything" across
                 restarts so a reboot while the VPN is down stays fail-closed
      url        label for log messages (the qBittorrent base URL)
      on_event   optional callable(level, msg) for the host to record events
    """

    def __init__(self, qb, tunnel_up, state_file=None, url="qBittorrent",
                 on_event=None):
        self.qb = qb
        self.tunnel_up = tunnel_up
        self.state_file = state_file
        self.url = url
        self.on_event = on_event
        self.prev_up: str | None = None   # None = not yet observed
        self.guard_paused = False         # we issued pause_all ourselves
        self.paused_hashes: list[str] | None = []
        self.last_event = ""
        self._last_offline_log = 0.0
        self._unknown_reported = False
        if state_file is not None:
            self._load_state()
            if self.guard_paused:
                self.last_event = "resuming pending (previous run paused torrents)"
                log.info("state file says previous run paused torrents — will resume when tunnel is up")

    def _load_state(self) -> None:
        """Load persisted guard state with strict validation.

        Invariant: guard state may be treated as empty only when there is
        definitively no prior state file. If a state file exists but cannot
        be read or validated, ownership is unknown: guard_paused=True,
        paused_hashes=None, and the guard must never auto-resume torrents
        from that state.
        """
        try:
            raw = self.state_file.read_text()
        except FileNotFoundError:
            # Legitimate first start: no prior state.
            return
        except Exception:
            # File exists but cannot be read: fail closed.
            self._mark_state_corrupt("unreadable")
            return
        try:
            state = json.loads(raw)
        except Exception:
            self._mark_state_corrupt("invalid JSON")
            return
        if not isinstance(state, dict):
            self._mark_state_corrupt("root is not a dictionary")
            return
        if not isinstance(state.get("guard_paused"), bool):
            self._mark_state_corrupt("guard_paused is not a boolean")
            return
        self.guard_paused = state["guard_paused"]
        if "paused_hashes" not in state:
            if self.guard_paused:
                self._mark_state_corrupt("paused_hashes missing while guard_paused")
            return
        saved = state["paused_hashes"]
        if not self.guard_paused:
            # When guard_paused is false, ownership must be empty.
            # Any non-empty paused_hashes is structurally inconsistent
            # and must fail closed.
            if saved is None:
                self._mark_state_corrupt("paused_hashes must be empty when guard_paused is false")
            elif not isinstance(saved, list) or saved:
                self._mark_state_corrupt("paused_hashes must be empty when guard_paused is false")
            else:
                self.paused_hashes = []
        else:
            if saved is None:
                self.paused_hashes = None   # unknown ownership marker
            elif isinstance(saved, list):
                self.paused_hashes = [str(h) for h in saved]
            else:
                self._mark_state_corrupt("paused_hashes has unexpected type")

    def _mark_state_corrupt(self, reason: str) -> None:
        """Mark the guard state as corrupt: fail closed with unknown ownership.

        The original file is left intact as evidence; a later normal
        _save_state() will replace it when an explicit guard operation
        requires persistence.
        """
        self.guard_paused = True
        self.paused_hashes = None
        log.error("guard state file is corrupt (%s) — ownership unknown, "
                  "automatic resume blocked; manual intervention required",
                  reason)

    def _save_state(self) -> bool:
        """Persist the current guard state.  Returns True when the state is
        durable, False when the write failed (the exception is logged).
        Callers decide what a failed save means for ownership — see
        _ownership_persistence_failed()."""
        if self.state_file is None:
            return True
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            # paused_hashes must persist the None (unknown ownership)
            # marker as JSON null — a list means exact ownership.
            self._atomic_json_write(self.state_file, {
                "guard_paused": self.guard_paused,
                "paused_hashes": self.paused_hashes,
            })
            return True
        except Exception:
            log.exception("could not save guard state")
            return False

    def _ownership_persistence_failed(self, reason: str) -> None:
        """Degrade to the unknown-ownership state after a failed state write.

        A pause or resume whose ownership record could not be made durable
        loses ownership certainty: the guard stays paused, exact ownership
        is discarded, and the single manual-intervention error event is
        emitted (routed to the host via on_event).  A best-effort re-save
        follows so a transient failure becomes a durable unknown-ownership
        barrier; if storage is still unavailable the in-memory state still
        fails closed.
        """
        self.guard_paused = True
        self.paused_hashes = None
        self.last_event = "resume blocked (guard state persistence failed)"
        if not self._unknown_reported:
            self._unknown_reported = True
            self._emit("error",
                       f"guard state persistence FAILED: {reason}; pause "
                       "ownership is unknown, automatic resume blocked; "
                       "manual intervention required")
        self._save_state()

    @staticmethod
    def _atomic_json_write(path, obj) -> None:
        """Atomically write JSON to path: temp file + fsync + rename.

        A failed write leaves the previous complete state file intact
        rather than truncating it into partial JSON. The temp file is
        cleaned up on exception.
        """
        import os
        import tempfile
        fd, tmp_path = tempfile.mkstemp(
            prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(obj, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
            # fsync the containing directory where supported
            try:
                dir_fd = os.open(str(path.parent), os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # logical event level -> logging method (keeps "warn" as the event/CSS
    # level the dashboard expects, while calling the non-deprecated
    # logging.Logger.warning() under the hood)
    _LOG_METHOD = {"info": "info", "warn": "warning", "error": "error"}

    def _emit(self, level: str, msg: str) -> None:
        getattr(log, self._LOG_METHOD.get(level, "info"))(msg)
        if self.on_event is not None:
            try:
                self.on_event(level, msg)
            except Exception:
                log.exception("guard on_event callback failed")

    def _qb(self, method_name, *args, cfg=None):
        """Call one method on the injected qB client, forwarding the host's
        config snapshot only when the host supplied one (ISSUE 5).  Clients
        that do not accept a snapshot (the standalone service's own QB
        client, which reads its env vars, and test stubs) keep working
        unchanged: with cfg=None the call is made exactly as before."""
        m = getattr(self.qb, method_name)
        if cfg is not None:
            return m(*args, cfg)
        return m(*args)

    def _pause_active(self, cfg=None) -> bool:
        """Pause active torrents and remember exactly which ones we changed.

        Ownership model for self.paused_hashes:
          [list]  — exact set of torrents the guard paused from an active
                    state; safe to auto-resume later
          None    — UNKNOWN: a pause_all() fallback ran while the active set
                    could not be read; the guard must NEVER auto-resume
                    (resume_all() could resurrect manually-paused torrents)
        """
        if hasattr(self.qb, "active_hashes") and hasattr(self.qb, "pause_hashes"):
            active = self._qb("active_hashes", cfg=cfg)
            if active is None:
                # Still fail closed, but ownership becomes UNKNOWN: we
                # paused everything without knowing which torrents were
                # active, so the guard must never auto-resume on restore.
                ok = self._qb("pause_all", cfg=cfg)
                if ok:
                    self.paused_hashes = None
                return ok
            ok = self._qb("pause_hashes", active, cfg=cfg)
            if ok and self.paused_hashes is not None:
                known = set(self.paused_hashes)
                known.update(active)
                self.paused_hashes = sorted(known)
            # if it was already None it stays None: unknown ∪ known = unknown
            return ok
        # Compatibility for injected clients implementing the old protocol
        # (no active_hashes): a global pause always means unknown ownership.
        ok = self._qb("pause_all", cfg=cfg)
        if ok:
            self.paused_hashes = None
        return ok

    def _resume_guarded(self, cfg=None) -> bool:
        """Release the guard's own pauses as a durability-ordered
        transaction.  Returns True only when the release completed AND the
        cleared state is durable.

        Central invariant: no /start request is ever sent while an exact
        auto-resume list is still authoritative on disk.  Before the resume
        the state is durably downgraded to the unknown-ownership barrier;
        if that barrier cannot be persisted, nothing is resumed.  After a
        successful resume the cleared state is persisted; if that write
        fails, the durable file still holds the safe pre-resume barrier, so
        a crash can never re-authorize the exact list.
        """
        if self.paused_hashes is None:
            # Unknown ownership: never auto-resume.  There is no algorithm
            # that can reconstruct the pre-pause_all() manual-pause set.
            return False
        if not hasattr(self.qb, "resume_hashes"):
            return False
        hashes = list(self.paused_hashes)
        # DURABILITY BARRIER: before /start can change qB state, make the
        # on-disk state incapable of authorizing an exact automatic resume
        # after a crash.
        self.guard_paused = True
        self.paused_hashes = None
        self._unknown_reported = False
        if not self._save_state():
            self._ownership_persistence_failed(
                "could not persist unknown-ownership barrier before resume")
            return False
        if not self._qb("resume_hashes", hashes, cfg=cfg):
            # The request outcome cannot safely restore exact ownership:
            # the barrier is already durable, so the state degrades to
            # unknown and the blocked release is reported once.
            self.report_unknown_ownership_once()
            return False
        self.guard_paused = False
        self.paused_hashes = []
        self._unknown_reported = False
        if not self._save_state():
            # The durable file still holds the pre-resume UNKNOWN barrier,
            # which is exactly what we want after a crash.
            self._ownership_persistence_failed(
                "torrents were resumed but the cleared guard state "
                "could not be persisted")
            return False
        return True

    def tick(self, *, tunnel=_UNSET, cfg=None) -> None:
        # An explicit ``tunnel`` (from the host's single config snapshot)
        # is used verbatim; otherwise the injected tunnel_up() callable is
        # consulted.  ``cfg`` is the host's single config snapshot: every
        # qB call of this tick (version gate, active-set read, pause/resume)
        # targets the SAME qB instance the tunnel decision was made from
        # (ISSUE 5).
        up = self.tunnel_up() if tunnel is _UNSET else tunnel

        if up is None:
            if not self.guard_paused:
                if self._pause_active(cfg):
                    self.guard_paused = True
                    if not self._save_state():
                        # The pause succeeded but its ownership record could
                        # not be made durable: ownership is now unknown and
                        # no automatic resume will ever be attempted.
                        self._ownership_persistence_failed(
                            "torrents were paused but their ownership record "
                            "could not be persisted")
                    elif self.prev_up is not None:
                        self.last_event = "tunnel lost"
                        self._emit("warn", "tunnel LOST — paused active torrents")
                    else:
                        self.last_event = "no tunnel at startup"
                        self._emit("warn", "no tunnel at startup — paused active torrents (fail-closed)")
            else:
                # fail-closed self-heal: e.g. qBittorrent restarted and
                # revived torrents while the VPN is down
                active = self._qb("any_active", cfg=cfg)
                if active:
                    if self._pause_active(cfg):
                        # Persist immediately: the self-heal may have
                        # changed the ownership state (exact -> None on a
                        # fallback pause, or an extended exact list), and a
                        # crash must not lose that change.
                        if not self._save_state():
                            self._ownership_persistence_failed(
                                "re-paused torrents but the updated ownership "
                                "record could not be persisted")
                        else:
                            self.last_event = "re-paused (torrents active while tunnel down)"
                            self._emit("warn", "tunnel down but torrents were active again — re-paused them")
        else:
            if self.guard_paused:
                if self._resume_guarded(cfg):
                    # _resume_guarded() owns the transaction: it set
                    # guard_paused=False, paused_hashes=[] and durably
                    # saved them — do not mutate or re-save here.
                    self.last_event = f"tunnel {up} restored"
                    self._emit("info", f"tunnel {up} RESTORED — resumed guard-paused torrents")
                else:
                    # If the release is blocked because ownership is unknown
                    # (or a durability barrier could not be written),
                    # surface it once; a plain qB-unreachable failure just
                    # keeps the pending state for the next tick.
                    self.report_unknown_ownership_once()
            elif self.prev_up is None:
                self.last_event = f"tunnel {up} up at startup"
                self._emit("info", f"tunnel {up} up at startup — left torrent states alone")
        self.prev_up = up

        if up is None and not self.guard_paused and not self._qb("online", cfg=cfg):
            now = time.time()
            if now - self._last_offline_log > 120:
                self._last_offline_log = now
                self._emit("warn", f"tunnel down AND qBittorrent unreachable ({self.url}) — will keep retrying")

    def observe(self, *, tunnel=_UNSET) -> str | None:
        """Re-detect the tunnel WITHOUT touching any torrent.

        Used by the gateway when the interface list changes while the guard
        is disabled, so the dashboard reflects the new globs immediately.
        An explicit ``tunnel`` (from the host's single config snapshot) is
        used verbatim; otherwise tunnel_up() is consulted.
        Returns the detected tunnel name, or None."""
        self.prev_up = self.tunnel_up() if tunnel is _UNSET else tunnel
        return self.prev_up

    @property
    def resume_ownership_unknown(self) -> bool:
        """True while a guard pause is pending AND its ownership is unknown.
        Unknown ownership arises when the exact list was never established
        (a pause_all() fallback ran with the active set unreadable) or was
        lost (a state-persistence failure, or a resume whose outcome was
        ambiguous).  This state can never be released automatically:
        resume_all() could resurrect manually-paused torrents, so hosts
        must not retry it in a loop and must surface it for manual
        intervention."""
        return self.guard_paused and self.paused_hashes is None

    def report_unknown_ownership_once(self) -> None:
        """Emit a single error event for the blocked unknown-ownership state.

        Hosts call this INSTEAD of retrying an impossible release; without
        the call the state simply stays persisted and silent."""
        if not self.resume_ownership_unknown or self._unknown_reported:
            return
        self._unknown_reported = True
        self.last_event = "resume blocked (pause ownership unknown)"
        self._emit("error",
                   "guard-paused torrents remain paused: pause ownership is "
                   "unknown, manual intervention required")

    def retry_pending_resume(self, cfg=None) -> bool:
        """Retry a pending release of guard-owned pauses.

        Never pauses anything, and never resumes anything except the hashes
        this guard itself recorded in paused_hashes.  The release is the
        durability-ordered transaction in _resume_guarded(): the unknown-
        ownership barrier must be on disk before any /start goes out.  Once
        the release succeeds, guard_paused is cleared and later calls are
        no-ops.  If the release cannot complete (qBittorrent unreachable, a
        rejected /start, or a persistence failure) the state degrades to
        unknown ownership: it is reported once and can never be released
        automatically — hosts must surface it for manual intervention
        instead of looping.
        Returns True when no guard-owned pauses remain pending, False when a
        release is still pending or impossible."""
        if not self.guard_paused:
            return True
        if self.paused_hashes is None:
            if not self._save_state():
                # State is already unknown in memory; surface the
                # persistence trouble (the helper's re-save is the
                # best-effort attempt at a durable barrier).
                self._ownership_persistence_failed(
                    "unknown pause ownership could not be persisted")
            self.report_unknown_ownership_once()
            return False
        if not self._resume_guarded(cfg):
            self.report_unknown_ownership_once()
            return False
        return True

    def disable(self, cfg=None) -> bool:
        """Turn the guard off.

        Resume ONLY the torrents this guard itself paused and clear that
        state; torrents paused for any other reason (e.g. manually) stay
        exactly as they are.  The release is the durability-ordered
        transaction in _resume_guarded(): if it cannot complete (qB
        unreachable, a rejected /start, or a persistence failure) the state
        degrades to unknown ownership and can never be released
        automatically.  Returns True when guard-owned pauses are released,
        False when a release is still pending or impossible."""
        if not self.guard_paused:
            self.paused_hashes = []
            self._save_state()
            self.last_event = "guard disabled"
            self._emit("info", "guard disabled — no guard-paused torrents to resume")
            return True
        if self.paused_hashes is None:
            # Unknown ownership: do NOT clear guard_paused (the state file
            # must keep recording the unresolved guard action) and do NOT
            # resume anything (resume_all() could resurrect manually-paused
            # torrents).
            self.last_event = "guard disabled (resume blocked: ownership unknown)"
            if not self._save_state():
                # Surface the persistence failure too, but remain unknown.
                self._ownership_persistence_failed(
                    "unknown pause ownership could not be persisted during "
                    "disable")
            self._emit("error",
                       "guard disabled but torrents cannot be safely "
                       "auto-resumed: the guard had to pause all torrents "
                       "without knowing which were already manually paused")
            return False
        if self.retry_pending_resume(cfg):
            self.last_event = "guard disabled"
            self._emit("info", "guard disabled — resumed guard-paused torrents")
            return True
        self.last_event = "guard disabled (resume failed)"
        self._emit("error",
                   "guard disabled but the pending resume FAILED — guard-paused "
                   "torrents stay paused; pause ownership is now unknown and "
                   "manual intervention is required")
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="run a single tick and exit")
    mode.add_argument("--disable", action="store_true",
                      help="release guard-paused torrents, keep retrying until "
                           "they are resumed, then exit (do not run the guard)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    # ISSUE 10 parity: the standalone service must apply the SAME
    # interface-glob validation the integrated gateway does.  A too-broad
    # pattern (e.g. '*' matching the always-up loopback) would silently
    # substitute another kill-switch policy, so it fails startup.
    # Deliberately NOT validated at module import: gateway.py imports
    # Guard from this module and never uses the standalone TUNNEL_IFACES,
    # so an invalid standalone env var must not make the gateway
    # unimportable.
    global IFACE_GLOBS, POLL
    try:
        IFACE_GLOBS = validate_vpn_iface_patterns(IFACE_GLOBS)
    except ValueError as exc:
        log.error("%s", exc)
        return 2
    # ISSUE 8 parity: GUARD_POLL must be a finite, > 0 number.  An invalid
    # value refuses startup (exit 2) instead of crashing at import or
    # running a sleep(nan)/sleep(inf) loop.  Re-validates the CURRENT
    # global (tests may monkeypatch it) before the loop uses it.
    try:
        POLL = float(POLL)
    except (TypeError, ValueError):
        log.error("GUARD_POLL %r is not a number — refusing to start",
                  os.environ.get("GUARD_POLL"))
        return 2
    if not math.isfinite(POLL) or POLL <= 0:
        log.error("GUARD_POLL %r must be a finite number greater than 0 — "
                  "refusing to start", os.environ.get("GUARD_POLL"))
        return 2
    # ISSUE 8 parity: the standalone QB_URL must not embed credentials
    # (QB_USER/QB_PASS are the intended credential fields) and must not
    # contain NUL/CR/LF.  Self-contained on purpose: this module never
    # imports config.py.
    if any(ch in QB_URL for ch in ("\x00", "\r", "\n")):
        log.error("QB_URL contains NUL/CR/LF — refusing to start")
        return 2
    try:
        _netloc = urllib.parse.urlsplit(QB_URL).netloc
    except ValueError:
        _netloc = ""
        if "://" in QB_URL:
            _netloc = re.split(r"[/?#]", QB_URL.split("://", 1)[1],
                               maxsplit=1)[0]
    if _netloc and "@" in _netloc:
        log.error("QB_URL embeds credentials — use QB_USER/QB_PASS instead; "
                  "refusing to start")
        return 2
    if not QB_PASS:
        log.error("QB_PASS is not set (env var or EnvironmentFile) — refusing to start")
        return 2

    QB_PUBLIC_URL = _public_url(QB_URL)
    qb = QB()
    g = Guard(qb, tunnel_up, STATE_FILE, url=QB_PUBLIC_URL)
    log.info("vpn-guard starting: qb=%s ifaces=%s poll=%.0fs", QB_PUBLIC_URL, ",".join(IFACE_GLOBS), POLL)

    def _incompatible() -> int:
        # A reachable qBittorrent that is NOT exactly 5.x: stop/start is
        # the 5.x protocol, so no guard action may run against it.
        log.error("unsupported qBittorrent %r; vpn-guard requires exactly 5.x",
                  qb.app_version())
        return 4

    if args.once:
        supported = qb.supports_guard()
        if supported is False:
            return _incompatible()
        if supported is None:
            log.error("qBittorrent version unavailable; no guard action performed")
            return 5
        g.tick()
        return 0
    if args.disable:
        # Stop enforcing the guard and release the exact torrents it paused.
        # If qBittorrent is unreachable the resume is retried (never paused,
        # never ticked) until it succeeds, then the process exits.  A
        # reachable non-5.x is never a drain candidate: stop/start would
        # speak the wrong protocol, so refuse and exit non-zero, leaving
        # the persisted paused ownership state intact.
        if g.resume_ownership_unknown:
            # A pause_all() fallback left the ownership unknown: the drain
            # could never terminate, and resuming everything could resurrect
            # manually-paused torrents.  Refuse immediately and exit
            # non-zero so the failure is visible; manual intervention is
            # required.  No version check is needed: no qB call will ever
            # be made from this state.
            g.disable()
            log.error("guard paused torrents via pause_all() when ownership could not "
                      "be determined; refusing to resume all because that could resume "
                      "manually-paused torrents")
            return 3
        supported = qb.supports_guard()
        while supported is None:
            log.warning("qBittorrent version unavailable — waiting for it to come up "
                        "(will not touch torrents)")
            time.sleep(POLL)
            supported = qb.supports_guard()
        if supported is False:
            return _incompatible()
        g.disable()
        if g.resume_ownership_unknown:
            # (Defensive: the state cannot change between the check above
            # and here, but keep the refusal path reachable.)
            log.error("guard paused torrents via pause_all() when ownership could not "
                      "be determined; refusing to resume all because that could resume "
                      "manually-paused torrents")
            return 3
        while g.guard_paused:
            log.info("guard-paused torrents not yet released — retrying in %.0fs", POLL)
            time.sleep(POLL)
            if qb.supports_guard() is False:
                return _incompatible()
            try:
                g.retry_pending_resume()
            except Exception:
                log.exception("pending resume retry failed")
            if g.resume_ownership_unknown:
                # The release attempt degraded ownership to unknown (a
                # rejected /start or a failed state write): retrying can
                # never release it, so refuse like the startup check.
                log.error("guard-paused torrents remain paused: pause ownership is "
                          "unknown, manual intervention required")
                return 3
        log.info("guard-paused torrents released — exiting")
        return 0
    last_unavail = 0.0
    while True:
        supported = qb.supports_guard()
        if supported is False:
            return _incompatible()
        if supported is True:
            try:
                g.tick()
            except Exception:
                log.exception("tick failed")
        else:
            # qBittorrent unreachable: do not touch torrents, keep retrying.
            now = time.time()
            if now - last_unavail > 120:
                last_unavail = now
                log.warning("qBittorrent version unavailable; not touching torrents, "
                            "will keep retrying")
        time.sleep(POLL)


if __name__ == "__main__":
    sys.exit(main())
