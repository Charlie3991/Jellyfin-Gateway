"""Environment-driven configuration for the Jellyfin Gateway.

Settings can also be changed at runtime (dashboard → Settings):
`apply_settings()` updates the in-memory values below *and* persists them
back to the `.env` file so they survive restarts.
"""
import fnmatch
import ipaddress
import logging
import math
import os
import re
import threading
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit
import shlex
import shutil
import tempfile
from pathlib import Path

log = logging.getLogger("config")

PROJECT_DIR = Path(__file__).resolve().parent
# Resolve this from the real process environment before reading the file.
ENV_FILE = Path(os.environ.get("GATEWAY_ENV_FILE", str(PROJECT_DIR / ".env")).strip())


def _load_dotenv() -> None:
    """Load .env as data, never as shell code.

    Existing process/systemd variables win.  ``shlex`` is used only as a
    quote parser; no expansion or command execution is possible.
    """
    try:
        lines = ENV_FILE.read_text().splitlines()
    except FileNotFoundError:
        return
    except OSError as exc:
        log.warning("could not read %s: %s", ENV_FILE, exc)
        return
    for line_no, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, raw = stripped.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            log.warning("ignoring invalid .env key on line %d", line_no)
            continue
        try:
            parsed = shlex.split(raw, comments=True, posix=True)
        except ValueError as exc:
            log.warning("ignoring malformed .env value on line %d: %s", line_no, exc)
            continue
        if len(parsed) > 1:
            log.warning("ignoring unquoted whitespace in .env value on line %d", line_no)
            continue
        os.environ.setdefault(key, parsed[0] if parsed else "")


_load_dotenv()


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


# ---------------------------------------------------------------------------
# Safety validators + one coercion path (ISSUE 8)
#
# These are defined BEFORE the module-level configuration below so
# startup and the Settings API share exactly ONE rule set: a value
# the API rejects must also be rejected when it arrives via .env,
# systemd or the process environment, with the same effective
# representation when it is accepted.
# ---------------------------------------------------------------------------
def _reject_line_controls(name: str, value: str) -> None:
    """Reject NUL, CR, and LF characters that could break line-oriented .env
    persistence or enable multi-line injection.
    """
    if any(ch in value for ch in ("\x00", "\r", "\n")):
        raise ValueError(
            f"{name}: line breaks and NUL bytes are not allowed"
        )


def _service_url_has_userinfo(value: str) -> bool:
    """True when a URL authority contains embedded credentials/userinfo."""
    value = str(value).strip()
    
    try:
        parsed = urlsplit(value)
    except ValueError:
        # urlsplit can fail on malformed authorities (notably malformed
        # bracketed IPv6). Still reject obvious userinfo rather than
        # treating a parse failure as proof that credentials are absent.
        if "://" in value:
            authority = value.split("://", 1)[1]
            authority = re.split(r"[/?#]", authority, maxsplit=1)[0]
            return "@" in authority
        return False
    
    # The @ separator is the important property here. This also rejects
    # username-only and empty-userinfo forms such as:
    #   http://user@host/
    #   http://:pass@host/
    #   http://@host/
    return bool(parsed.netloc and "@" in parsed.netloc)


def _coerce(name: str, typ: str, raw):
    """Validate + convert one raw value; raises ValueError with a clear msg."""
    if typ == "bool":
        if isinstance(raw, bool):
            return raw
        s = str(raw).strip().lower()
        if s in ("1", "true", "yes", "on"):
            return True
        if s in ("0", "false", "no", "off"):
            return False
        raise ValueError(f"{name}: expected true/false (or 1/0)")
    if typ == "int":
        try:
            v = int(str(raw).strip())
        except ValueError:
            raise ValueError(f"{name}: expected an integer")
        if name == "GATEWAY_PORT" and not (1 <= v <= 65535):
            raise ValueError("GATEWAY_PORT: must be between 1 and 65535")
        return v
    if typ == "float":
        try:
            v = float(str(raw).strip())
        except ValueError:
            raise ValueError(f"{name}: expected a number")
        if not math.isfinite(v) or v <= 0:
            raise ValueError(f"{name}: must be a finite number greater than 0")
        return v
    if typ == "dir":
        if raw is None:
            raise ValueError(f"{name}: must not be empty")
        v = str(raw).strip()
        if not v:
            raise ValueError(f"{name}: must not be empty")
        _reject_line_controls(name, v)
        return Path(v).expanduser().absolute()
    # str
    v = str(raw).strip()
    _reject_line_controls(name, v)
    if name in ("QB_URL", "JELLYFIN_URL"):
        if _service_url_has_userinfo(v):
            raise ValueError(
                f"{name}: embedded credentials are not allowed in service URLs"
            )
        v = v.rstrip("/")
    if name == "VPN_GUARD_IFACES":
        # Shared with the startup parse so a too-broad pattern (e.g. '*')
        # is rejected no matter how it is configured (ISSUE 10).
        return validate_vpn_iface_patterns(v)
    return v



def _startup_value(name: str, typ: str, default):
    """Materialize one runtime-editable setting at startup.

    ISSUE 8: the SAME _coerce() rules the Settings API applies to
    dashboard changes apply to .env / systemd EnvironmentFile / process
    environment values.  A value the API rejects must also be rejected
    at startup, and startup ABORTS (RuntimeError -> non-zero exit)
    instead of silently falling back to a default — an invalid
    configuration is safer than a guessed one.
    """
    raw = _env(name, str(default))
    try:
        return _coerce(name, typ, raw)
    except ValueError as exc:
        raise RuntimeError(f"invalid startup configuration: {exc}") from exc


def _resolve_nordvpn_bin(raw: str) -> str:
    """Validate and resolve NORDVPN_BIN once at startup.

    Local startup config only (`.env` / process env) — intentionally NOT
    part of CONFIG_FIELDS, so the dashboard Settings API can never point
    the gateway at an arbitrary executable.  The value must be a simple
    command name or filesystem path; it is resolved through PATH (or kept
    as an absolute path) and that exact file is what nordvpn.py execs.
    Malformed or missing values fall back to plain "nordvpn" (the VPN card
    then reports "not available" until the real CLI is on PATH).
    """
    if not re.fullmatch(r"[A-Za-z0-9._~/\-]+", raw or ""):
        log.warning("NORDVPN_BIN %r is not a simple command name or path; using 'nordvpn'", raw)
        return "nordvpn"
    resolved = shutil.which(raw)
    if resolved is None:
        log.warning("NORDVPN_BIN %r not found; VPN status/actions will report 'not available'", raw)
        return raw
    if os.path.basename(resolved) != "nordvpn":
        log.warning("NORDVPN_BIN resolves to %r (expected a file named 'nordvpn'); continuing", resolved)
    return resolved


DATA_DIR = _startup_value("GATEWAY_DATA_DIR", "dir", PROJECT_DIR / "data")
# where runtime settings changes are persisted (same file systemd/run.sh read)

# --- gateway web server ---
GATEWAY_HOST = _startup_value("GATEWAY_HOST", "str", "0.0.0.0")
GATEWAY_PORT = _startup_value("GATEWAY_PORT", "int", "8390")
# once everything is set up, hide the Settings panel from the dashboard
HIDE_SETTINGS = _startup_value("HIDE_SETTINGS", "bool", "0")


# --- trusted reverse proxies (ISSUE 8) ---
# Startup-only: deliberately absent from CONFIG_FIELDS, so the dashboard can
# never change which peers the gateway trusts to forward the real client
# address.  A comma-separated list of IPs or CIDRs.  When the socket peer is
# one of these, the effective client for the login rate limiter is taken from
# X-Forwarded-For; otherwise the header is ignored entirely.
def _parse_trusted_proxies(raw: str) -> list:
    nets = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            log.warning("ignoring malformed GATEWAY_TRUSTED_PROXIES entry %r", part)
    return nets


TRUSTED_PROXIES = _parse_trusted_proxies(_env("GATEWAY_TRUSTED_PROXIES", ""))


def _is_trusted_peer(ip: str) -> bool:
    if not TRUSTED_PROXIES:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in TRUSTED_PROXIES)


def effective_client_ip(peer: str, xff_header: str | None) -> str:
    """Effective client IP for the login rate limiter (ISSUE 8).

    If the socket peer is a configured trusted reverse proxy, the client is
    the rightmost untrusted address in the X-Forwarded-For chain.  Otherwise
    the forwarding header is ignored entirely and the socket peer is used.
    Malformed or absent forwarding data falls back to the socket peer, so a
    spoofed header from an untrusted peer can never change the limit key.
    """
    if not _is_trusted_peer(peer) or not xff_header:
        return peer
    chain = [p.strip() for p in xff_header.split(",") if p.strip()]
    if not chain:
        return peer
    for hop in reversed(chain):
        try:
            addr = ipaddress.ip_address(hop)
        except ValueError:
            return peer
        if not any(addr in net for net in TRUSTED_PROXIES):
            return hop
    return chain[0]

# --- qBittorrent ---
# _coerce() (the Settings API's validator) applies here too (ISSUE 8):
# embedded userinfo in a service URL and NUL/CR/LF are rejected at
# startup exactly as they are at runtime.
QB_URL = _startup_value("QB_URL", "str", "http://127.0.0.1:8090")
QB_USER = _startup_value("QB_USER", "str", "admin")
QB_PASS = _startup_value("QB_PASS", "str", "")

# --- Jellyfin ---
JELLYFIN_URL = _startup_value("JELLYFIN_URL", "str", "http://localhost:8096")
JELLYFIN_API_KEY = _startup_value("JELLYFIN_API_KEY", "str", "")

def validate_media_dirs(download_dir: Path, movies_dir: Path, tv_dir: Path) -> None:
    """Reject unsafe combinations of the three media directories.

    Shared by the Settings API (apply_settings) and startup (ISSUE 8),
    so an unsafe layout is rejected no matter how it is configured.
    Raises ValueError when two of the directories are identical, or when
    one is nested inside the other (in either direction).  Comparisons use
    resolved absolute paths, so relative paths, ".." components and
    symlinks cannot hide an overlap.  A rejected layout must never reach
    the .env file, the live configuration or qBittorrent categories.
    """
    paths = {
        "DOWNLOAD_DIR": Path(download_dir).resolve(),
        "MOVIES_DIR": Path(movies_dir).resolve(),
        "TV_DIR": Path(tv_dir).resolve(),
    }

    items = list(paths.items())

    for i, (name_a, path_a) in enumerate(items):
        for name_b, path_b in items[i + 1:]:
            if path_a == path_b:
                raise ValueError(f"{name_a} and {name_b} must not be the same directory")

            if path_a in path_b.parents or path_b in path_a.parents:
                raise ValueError(
                    f"{name_a} and {name_b} must not contain one another"
                )


# --- media layout ---
# MEDIA_ROOT is a convenience fallback: each of the three real locations can
# be overridden independently (e.g. to live on separate drives).  All four
# are coerced with the SAME "dir" rules as the Settings API (ISSUE 8):
# non-empty, no NUL/CR/LF, expanduser + absolute (so relative and ~
# paths get the same treatment as a dashboard change).
MEDIA_ROOT = _startup_value("MEDIA_ROOT", "dir", PROJECT_DIR / "media")
DOWNLOAD_DIR = _startup_value("DOWNLOAD_DIR", "dir", MEDIA_ROOT / "downloads")
MOVIES_DIR = _startup_value("MOVIES_DIR", "dir", MEDIA_ROOT / "Movies")
TV_DIR = _startup_value("TV_DIR", "dir", MEDIA_ROOT / "TV")
# The Settings API runs this same check in apply_settings(); startup runs
# it here too (ISSUE 8) so a hand-edited .env cannot produce an unsafe
# layout.  An overlap or nesting ABORTS startup — there is no fallback.
validate_media_dirs(DOWNLOAD_DIR, MOVIES_DIR, TV_DIR)


# --- sorting behaviour ---
# Booleans now use the SAME true/false/yes/no/on/off/1/0 set as the
# Settings API (ISSUE 8): "true" enables, "0" disables, anything else
# aborts startup instead of silently meaning False.
SORT_DELETE_TORRENT = _startup_value("SORT_DELETE_TORRENT", "bool", "1")
NOTIFY_JELLYFIN = _startup_value("NOTIFY_JELLYFIN", "bool", "1")
WATCH_POLL = _startup_value("WATCH_POLL", "float", "3")

# --- NordVPN / VPN guard ---
# Startup-only: deliberately absent from CONFIG_FIELDS (see _resolve_nordvpn_bin).
NORDVPN_BIN = _resolve_nordvpn_bin(_env("NORDVPN_BIN", "nordvpn"))
# VPN_GUARD=true is now ENABLED (same boolean set as the Settings API);
# previously only an exact "1" enabled it (ISSUE 8).
VPN_GUARD = _startup_value("VPN_GUARD", "bool", "1")
VPN_GUARD_POLL = _startup_value("VPN_GUARD_POLL", "float", "5")
# Interface names that are NEVER a P2P VPN tunnel.  A VPN_GUARD_IFACES glob
# that matches any of these is too broad: it would let the guard treat a
# regular (usually always-up) interface as "the VPN" and silently defeat the
# kill switch.  (tailscale0 is an overlay, not a P2P VPN, per the default.)
VPN_IFACE_SENTINELS = (
    "lo", "eth0", "enp1s0", "ens3", "wlan0", "wlp1s0",
    "docker0", "br0", "tailscale0",
    # more common non-VPN families (physical/bridge/virtual/wwan)
    "eno1", "enx001122334455", "bond0", "virbr0", "veth0",
    "cni0", "podman0", "wwan0",
)


def validate_vpn_iface_patterns(raw) -> list:
    """Validate VPN_GUARD_IFACES globs; return the cleaned pattern list.

    Shared by the startup parse (below) and the Settings API (_coerce) so a
    too-broad pattern is rejected no matter how it is configured (ISSUE 10).
    Raises ValueError if the list is empty or if any glob matches a sentinel
    interface that is definitely not a P2P VPN tunnel (a pattern such as '*'
    would otherwise match the always-up loopback and neuter the kill switch).
    """
    if isinstance(raw, str):
        pats = [p.strip() for p in raw.split(",") if p.strip()]
    else:
        pats = [str(p).strip() for p in raw if str(p).strip()]
    if not pats:
        raise ValueError(
            "VPN_GUARD_IFACES: give at least one glob (e.g. tun*,nordlynx*)")
    for pat in pats:
        for sentinel in VPN_IFACE_SENTINELS:
            if fnmatch.fnmatch(sentinel, pat):
                raise ValueError(
                    f"VPN_GUARD_IFACES: pattern '{pat}' is too broad; it "
                    f"matches non-VPN interface '{sentinel}'")
    return pats


# interface globs treated as "the VPN tunnel" (tailscale0 is an overlay, not a P2P VPN)
# A too-broad pattern (e.g. '*') fails startup rather than silently
# substituting another kill-switch policy (ISSUE 10).
# _coerce() routes VPN_GUARD_IFACES through validate_vpn_iface_patterns()
# (same as the Settings API) and returns the cleaned list; an invalid
# pattern now aborts startup via _startup_value (ISSUE 8).
VPN_GUARD_IFACES = _startup_value("VPN_GUARD_IFACES", "str",
                                  "tun*,tap*,nordlynx*")


def _parse_ip_link_line(line: str) -> tuple[str, set[str]] | None:
    """Parse one `ip -o link` line into (interface_name, interface_flags).
    
    Returns None for malformed/unrecognized output.
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


def tunnel_ifaces(patterns=None) -> list[str]:
    """All UP interface names matching the VPN_GUARD_IFACES globs, in
    `ip -o link` order.

    ``patterns`` lets a host pass the interface globs from a single
    config.snapshot() so one logical operation never mixes the guard flag
    with a different generation of the interface list (ISSUE 5).  Shared by
    tunnel_iface() and the /api/status matched_ifaces display so the two
    never disagree about which interfaces the globs match (ISSUE 10)."""
    if patterns is None:
        patterns = snapshot().VPN_GUARD_IFACES
    import subprocess
    try:
        result = subprocess.run(
            ["ip", "-o", "link"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return []

    if result.returncode != 0:
        return []

    out = []
    for line in result.stdout.splitlines():
        parsed = _parse_ip_link_line(line)
        if parsed is None:
            continue
        name, flags = parsed
        if "UP" not in flags:
            continue
        if any(fnmatch.fnmatch(name, pat) for pat in patterns):
            out.append(name)
    return out


def tunnel_iface(patterns=None) -> str | None:
    """Name of the first UP interface matching the VPN_GUARD_IFACES globs,
    else None.  See tunnel_ifaces() for the shared scan logic (ISSUE 10)."""
    matches = tunnel_ifaces(patterns)
    return matches[0] if matches else None


# ---------------------------------------------------------------------------
# runtime-editable settings (dashboard → Settings)
# ---------------------------------------------------------------------------

# name -> (type, secret, default). Drives validation, /api/config and the UI.
CONFIG_FIELDS = {
    "GATEWAY_HOST": ("str", False, "0.0.0.0"),
    "GATEWAY_PORT": ("int", False, "8390"),
    "HIDE_SETTINGS": ("bool", False, "0"),
    "QB_URL": ("str", False, "http://127.0.0.1:8090"),
    "QB_USER": ("str", False, "admin"),
    "QB_PASS": ("str", True, ""),
    "JELLYFIN_URL": ("str", False, "http://localhost:8096"),
    "JELLYFIN_API_KEY": ("str", True, ""),
    "DOWNLOAD_DIR": ("dir", False, str(DOWNLOAD_DIR)),
    "MOVIES_DIR": ("dir", False, str(MOVIES_DIR)),
    "TV_DIR": ("dir", False, str(TV_DIR)),
    "SORT_DELETE_TORRENT": ("bool", False, "1"),
    "NOTIFY_JELLYFIN": ("bool", False, "1"),
    "WATCH_POLL": ("float", False, "3"),
    "VPN_GUARD": ("bool", False, "1"),
    "VPN_GUARD_POLL": ("float", False, "5"),
    "VPN_GUARD_IFACES": ("str", False, "tun*,tap*,nordlynx*"),
    # NOTE: NORDVPN_BIN is intentionally absent here: it selects an
    # executable, so it is local startup config only, never dashboard-editable.
}
# keys that only take effect once the gateway process restarts
RESTART_KEYS = {"GATEWAY_HOST", "GATEWAY_PORT"}


# ---------------------------------------------------------------------------
# runtime-atomic configuration (ISSUE 5)
#
# apply_settings() used to publish new values by mutating the module
# globals one at a time; a concurrent thread could observe a mixed
# old/new combination (e.g. the new QB_URL with the old credentials).
# The lock below serializes the WHOLE settings transaction (validation,
# .env persistence, publication), and every runtime operation that needs
# several related values calls snapshot() once and uses that immutable
# snapshot for the whole operation.
# ---------------------------------------------------------------------------

_CONFIG_LOCK = threading.RLock()


@dataclass(frozen=True)
class ConfigSnapshot:
    """Immutable, coherent view of every runtime-editable setting.

    A single snapshot() call is the only way a runtime operation may
    combine two or more settings: each field was read while holding
    _CONFIG_LOCK, so no operation can mix generations of the config.
    """
    GATEWAY_HOST: str
    GATEWAY_PORT: int
    HIDE_SETTINGS: bool

    QB_URL: str
    QB_USER: str
    QB_PASS: str

    JELLYFIN_URL: str
    JELLYFIN_API_KEY: str

    DOWNLOAD_DIR: Path
    MOVIES_DIR: Path
    TV_DIR: Path

    SORT_DELETE_TORRENT: bool
    NOTIFY_JELLYFIN: bool
    WATCH_POLL: float

    VPN_GUARD: bool
    VPN_GUARD_POLL: float
    VPN_GUARD_IFACES: tuple[str, ...]


def snapshot() -> ConfigSnapshot:
    """One coherent, immutable configuration for a logical operation.

    Readers call this ONCE and use the returned object throughout; the
    lock is held only for the (cheap) field copies, never across network
    or filesystem work.  Direct module-global reads still work (tests
    monkeypatch them), but a single read cannot form a mixed profile and
    multi-field operations must not use them.
    """
    with _CONFIG_LOCK:
        return ConfigSnapshot(
            GATEWAY_HOST=GATEWAY_HOST,
            GATEWAY_PORT=GATEWAY_PORT,
            HIDE_SETTINGS=HIDE_SETTINGS,
            QB_URL=QB_URL,
            QB_USER=QB_USER,
            QB_PASS=QB_PASS,
            JELLYFIN_URL=JELLYFIN_URL,
            JELLYFIN_API_KEY=JELLYFIN_API_KEY,
            DOWNLOAD_DIR=DOWNLOAD_DIR,
            MOVIES_DIR=MOVIES_DIR,
            TV_DIR=TV_DIR,
            SORT_DELETE_TORRENT=SORT_DELETE_TORRENT,
            NOTIFY_JELLYFIN=NOTIFY_JELLYFIN,
            WATCH_POLL=WATCH_POLL,
            VPN_GUARD=VPN_GUARD,
            VPN_GUARD_POLL=VPN_GUARD_POLL,
            VPN_GUARD_IFACES=tuple(VPN_GUARD_IFACES),
        )


def public_service_url(value: str) -> str:
    """Return a URL safe to expose in API/UI responses.
    
    Any userinfo is stripped. Malformed URLs are not echoed back.
    """
    value = str(value)
    
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "<invalid URL>"
    
    if not parsed.netloc:
        # Existing configuration historically permits values that are not
        # fully validated URLs. Do not broaden ISSUE 19 into URL validation.
        # But if something suspicious contains '@', don't reflect it.
        return "<invalid URL>" if "@" in value else value
    
    netloc = parsed.netloc.rsplit("@", 1)[-1]
    
    return urlunsplit((
        parsed.scheme,
        netloc,
        parsed.path,
        parsed.query,
        parsed.fragment,
    ))


def current_values() -> dict:
    """Current effective values for the UI. Secrets are never returned:
    they report {"value": null, "set": <bool>} instead.

    Rendered from ONE snapshot so the Settings panel never shows a mixed
    old/new profile while a settings write is in flight (ISSUE 5)."""
    cfg = snapshot()
    out = {}
    for name, (typ, secret, _default) in CONFIG_FIELDS.items():
        v = getattr(cfg, name)
        if secret:
            out[name] = {"value": None, "set": bool(v), "type": typ}
        else:
            if isinstance(v, (list, tuple)):
                disp = ",".join(v)
            elif isinstance(v, Path):
                disp = str(v)
            elif name in ("QB_URL", "JELLYFIN_URL"):
                disp = public_service_url(v)
            else:
                disp = v
            out[name] = {"value": disp, "set": True, "type": typ}
    return out


def apply_settings(updates: dict) -> dict:
    """Validate, apply to the in-memory config and persist to ENV_FILE.

    All-or-nothing: any invalid key aborts the whole update.
    Empty/None values for secret fields mean "keep the current value".
    Returns {"applied": [...], "unchanged": [...], "kept": [...],
             "restart": [...], "persisted": <path>}.
    """
    if not isinstance(updates, dict):
        raise ValueError("payload must be a JSON object of settings")
    # The WHOLE transaction runs under _CONFIG_LOCK (ISSUE 5): validation
    # compares against a stable current configuration, .env persistence
    # and the live publication cannot interleave with each other, and a
    # snapshot() reader can never observe the one-at-a-time publication
    # window.  persist_env() intentionally stays INSIDE the lock: readers
    # keep the complete old configuration until persistence succeeds, and
    # two concurrent writes cannot race through independent
    # read-modify-write passes over the .env file.
    with _CONFIG_LOCK:
        plan, kept, unchanged, errors = {}, [], [], []
        for name, raw in updates.items():
            if name not in CONFIG_FIELDS:
                errors.append(f"unknown setting: {name}")
                continue
            typ, secret, _default = CONFIG_FIELDS[name]
            if secret and (raw is None or str(raw).strip() == ""):
                kept.append(name)
                continue
            try:
                val = _coerce(name, typ, raw)
            except ValueError as e:
                errors.append(str(e))
                continue
            if val == globals()[name]:
                unchanged.append(name)
            else:
                plan[name] = val
        if errors:
            raise ValueError("; ".join(errors))

        # Reject an unsafe media layout BEFORE persisting or applying
        # anything, so a rejected request leaves the .env file and the
        # live configuration completely unchanged.  Each dir's
        # prospective value is the proposed one if it is part of this
        # update, otherwise the current value.
        validate_media_dirs(
            plan.get("DOWNLOAD_DIR", DOWNLOAD_DIR),
            plan.get("MOVIES_DIR", MOVIES_DIR),
            plan.get("TV_DIR", TV_DIR),
        )

        if plan:
            # Persist first.  A disk/permission failure must leave the
            # live process completely unchanged.
            persist_env(plan)
            for name, val in plan.items():
                globals()[name] = val
        return {
            "applied": sorted(plan),
            "unchanged": sorted(unchanged),
            "kept": sorted(kept),
            "restart": sorted(k for k in plan if k in RESTART_KEYS),
            "persisted": str(ENV_FILE),
        }


# ---------------------------------------------------------------------------
# .env persistence
# ---------------------------------------------------------------------------

def _fmt_env_value(v: str) -> str:
    """Quote a value for systemd EnvironmentFile and our data-only parser."""
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _to_env_str(name: str, v) -> str:
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, Path):
        v = str(v)
    if isinstance(v, float) and v == int(v):
        return str(int(v))
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        v = ",".join(v)
    return _fmt_env_value(str(v))


def _read_env_file() -> list[tuple[str, str, str | None]]:
    """Parse ENV_FILE -> [(key, value, raw_line)] preserving order."""
    entries = []
    try:
        for line in ENV_FILE.read_text().splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, _, v = s.partition("=")
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                v = v[1:-1]
            entries.append((k.strip(), v, line))
    except FileNotFoundError:
        pass
    return entries


def persist_env(values: dict) -> None:
    """Write updated keys back to ENV_FILE, preserving comments, blank lines
    and any other (unknown) lines. New keys are appended at the end."""
    updated = {k: _to_env_str(k, v) for k, v in values.items()}
    out_lines: list[str] = []
    seen: set[str] = set()
    try:
        raw_lines = ENV_FILE.read_text().splitlines()
    except FileNotFoundError:
        raw_lines = []
    except OSError as e:
        raise ValueError(f"could not read {ENV_FILE}: {e}") from e
    for line in raw_lines:
        s = line.strip()
        key = None
        if s and not s.startswith("#") and "=" in s:
            key = s.split("=", 1)[0].strip()
        if key is not None and key in updated:
            out_lines.append(f"{key}={updated[key]}")
            seen.add(key)
        else:
            out_lines.append(line)
    missing = {k: v for k, v in updated.items() if k not in seen}
    if missing:
        out_lines.append("")
        out_lines.append("# (updated via dashboard settings)")
        out_lines.extend(f"{k}={v}" for k, v in missing.items())
    tmp_name = None
    try:
        ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{ENV_FILE.name}.",
                                        dir=str(ENV_FILE.parent))
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write("\n".join(out_lines) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, ENV_FILE)
        tmp_name = None
        os.chmod(ENV_FILE, 0o600)
        try:
            dir_fd = os.open(ENV_FILE.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    except OSError as e:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        raise ValueError(f"could not write {ENV_FILE}: {e}") from e
    log.info("settings persisted to %s: %s", ENV_FILE, ", ".join(sorted(updated)))
