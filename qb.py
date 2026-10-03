"""qBittorrent WebUI protocol client (ISSUE 6, phase 1).

Pure move out of gateway.py: the HTTP session, version handling and
mutation helpers for talking to qBittorrent.  The constants that govern
the gateway's *use* of the client (body caps, add-verification timing,
state sets) stay in their owning modules.
"""
import config
import httpx
import json
import re
import threading
import time

from service_utils import _safe_service_error
from vpn_guard import INACTIVE_STATES

# ---------------------------------------------------------------------------
# qBittorrent client
# ---------------------------------------------------------------------------

class QBUnsupportedVersion(RuntimeError):
    """A qBittorrent-only API call was attempted against a version the
    Gateway does not support (it requires qBittorrent 5.x)."""


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


def _qb_mutation_ok(r) -> bool:
    """Successful synchronous qBittorrent mutation (qB 5.0-5.2).

    qB 5.0/5.1 answer successful stop/start/delete/category/preference
    mutations with 200 "Ok."; 5.2 answers with 204 No Content.  Both are
    success — treating 204 as a failure makes a successful, safety-critical
    operation look failed.  Everything else is a failure: an httpx.Response
    is truthy even for HTTP 500, so the status must be mapped explicitly.
    The 404 "already absent" delete case is interpreted by the delete
    callers, and /torrents/add has separate version-dependent semantics.
    """
    return r is not None and r.status_code in (200, 204)


def _qb_add_accepted(r, expected_hash: str) -> tuple[bool, str]:
    """Classify a /torrents/add response (ISSUE 3).

    Real qB 5.x contracts (per the addAction() source, 5.0/5.1 vs 5.2):
      5.0/5.1: fresh add -> 200 body "Ok."; duplicate -> 200 body "Fails."
      5.2:     fresh add -> 200 JSON {success_count:1, pending_count:0,
               added_torrent_ids:[id]}; duplicate -> 409 body "Conflict"
      5.2:     202 Accepted = pending asynchronous add (pending_count > 0).
    accepted=True ONLY when THIS request positively proved that it added
    expected_hash — that proof is what the ownership-gated rollback below
    relies on.  A 202 or a mismatched 5.2 JSON never proves ownership, so
    it is never acceptable: an indeterminate add makes rollback
    ownership impossible.
    """
    if r is None:
        return False, "no response"
    if r.status_code == 200:
        text = r.text.strip()
        # qB 5.0 / 5.1: the gateway submits exactly ONE magnet or file,
        # so the body IS the answer for that item.
        if text == "Ok.":
            return True, "legacy"
        if text == "Fails.":
            return False, "failed"
        # qB 5.2: JSON report.
        try:
            data = r.json()
        except Exception:
            return False, "unexpected response"
        if not isinstance(data, dict):
            return False, "unexpected response"
        ids = {str(x).lower() for x in data.get("added_torrent_ids", [])}
        if (data.get("success_count") == 1
                and data.get("pending_count") == 0
                and expected_hash.lower() in ids):
            return True, "confirmed"
        return False, "torrent was not confirmed added"
    if r.status_code == 409:
        return False, "conflict"
    if r.status_code == 202:
        return False, "pending"
    return False, f"HTTP {r.status_code}"


class QBClient:
    def __init__(self):
        # follow_redirects=False: qB auth is session-cookie based, so a
        # 307/308 would forward the login form body and the session cookie
        # cross-origin. QB_URL must point at the final qB WebUI origin;
        # redirects are treated as unexpected qB responses. Matches the
        # Jellyfin client (plain httpx.get/post).
        self.http = httpx.Client(timeout=15, follow_redirects=False)
        # RLock: api() holds it across the WHOLE auth+request transaction
        # and _ensure() re-acquires it from the same thread (ISSUE 5).
        self.lock = threading.RLock()
        self.logged_in = False
        # Connectivity belongs to the qB instance that produced it: a
        # guard decision must not consume connectivity produced by
        # another generation of the configuration (ISSUE 5).  ONE
        # immutable (key, online) tuple: the reference is read and
        # assigned atomically, so the key and the flag always agree.
        self._online_state: tuple | None = None
        self.last_error = ""
        # The cached version and the login both belong to the qB instance
        # they were obtained from.  Keying them by (URL, user, pass) means
        # a snapshot for a NEW qB instance can never consume the version
        # discovered from, or the session cookie of, the previous one
        # (ISSUE 5).  The cache is ONE immutable tuple assigned atomically
        # under self.lock: no reader can ever see a version from one key
        # paired with a major or timestamp from another.
        self._app_version_cache: tuple | None = None
        self._login_key: tuple | None = None

    def _login(self, cfg=None) -> bool:
        # One qB snapshot supplies URL + user + pass together, so a
        # settings change can never pair the new URL with the old
        # credentials (ISSUE 5).
        if cfg is None:
            cfg = config.snapshot()
        if not cfg.QB_PASS:
            self.last_error = "QB_PASS not set"
            return False
        try:
            r = self.http.post(f"{cfg.QB_URL}/api/v2/auth/login",
                               data={"username": cfg.QB_USER, "password": cfg.QB_PASS})
            ok = r.status_code in (200, 204) and r.text.strip() != "Fails."
            self.last_error = "" if ok else f"login failed ({r.status_code})"
            return ok
        except Exception as e:
            self.last_error = _safe_service_error(e, cfg.QB_URL)
            return False

    def _ensure(self, cfg=None) -> bool:
        if cfg is None:
            cfg = config.snapshot()
        key = (cfg.QB_URL, cfg.QB_USER, cfg.QB_PASS)
        if self.logged_in and self._login_key == key:
            return True
        with self.lock:
            if self.logged_in and self._login_key == key:
                return True
            if self._login_key is not None and self._login_key != key:
                # The endpoint or credentials changed: the old session
                # cookie belongs to the previous qB instance and must not
                # be presented to the new one (ISSUE 5).
                self.http.cookies.clear()
            self.logged_in = self._login(cfg)
            if self.logged_in:
                self._login_key = key
        return self.logged_in

    def api(self, method: str, path: str, cfg=None, **kw) -> httpx.Response | None:
        # Every request runs against ONE config snapshot: the URL used
        # here is the same generation as the credentials used to
        # authenticate (ISSUE 5).
        if cfg is None:
            cfg = config.snapshot()
        # The lock covers the WHOLE auth + request transaction: while this
        # request is in flight, no other thread can re-login and swap the
        # shared cookie jar, so the request is always sent with the
        # session it authenticated with (ISSUE 5).
        with self.lock:
            if not self._ensure(cfg):
                self._online_state = None
                return None
            for attempt in (1, 2):
                try:
                    r = self.http.request(method, f"{cfg.QB_URL}{path}", **kw)
                except Exception as e:
                    self._online_state = None
                    self.logged_in = False
                    self.last_error = _safe_service_error(e, cfg.QB_URL)
                    return None
                if r.status_code in (401, 403) and attempt == 1:
                    self.logged_in = False
                    if not self._ensure(cfg):
                        self._online_state = None
                        return None
                    continue
                self._online_state = ((cfg.QB_URL, cfg.QB_USER, cfg.QB_PASS), True)
                self.last_error = ""
                return r
            self._online_state = None
            self.last_error = "login rejected"
            return None

    def reset(self):
        """Drop the cached login and version (call after QB_URL/QB_USER/
        QB_PASS changes)."""
        with self.lock:
            self.logged_in = False
            self._online_state = None
            self.last_error = ""
            self._app_version_cache = None
            self._login_key = None
            self.http.cookies.clear()

    def online(self, cfg=None) -> bool:
        """Whether qBittorrent was reachable on the last API call.

        When a snapshot is given, the answer is only True if the last
        successful call was made against the SAME qB instance
        (URL, user, pass): a guard decision must not consume
        connectivity produced by another generation of the
        configuration (ISSUE 5)."""
        # One atomic tuple read: the key and the flag always agree
        # (ISSUE 5).
        state = self._online_state
        if state is None:
            return False
        if cfg is None:
            return state[1]
        key = (cfg.QB_URL, cfg.QB_USER, cfg.QB_PASS)
        return state[1] if state[0] == key else False

    def any_active(self, cfg=None) -> bool | None:
        """True/False if we could check, None if qBittorrent is unreachable.
        "active" = not paused/stopped/error — used by the VPN guard's
        fail-closed self-heal (see vpn_guard.INACTIVE_STATES)."""
        torrents = self.torrents_info(cfg)
        if torrents is None:
            return None
        return any((t.get("state") or "") not in INACTIVE_STATES for t in torrents)

    # How long a cached version is trusted before a non-forced lookup
    # re-queries.  Short enough that a qB 5.x -> 4.x replacement is
    # detected within ~1 minute even if no /api/add occurs; long enough
    # that the guard's frequent polls do not hammer /app/version.
    VERSION_CACHE_TTL = 30.0

    def app_version(self, *, force: bool = False, cfg=None) -> str | None:
        """qBittorrent version string, or None if unreachable.

        A successful lookup is cached for VERSION_CACHE_TTL seconds (the
        guard polls frequently); a failed lookup is NOT cached, so a
        transient outage cannot pin the version to a stale value.
        force=True re-queries immediately; if the forced request FAILS it
        also invalidates any cached version, so trust is never retained
        after a failed re-check.

        The cache is keyed by (QB_URL, QB_USER, QB_PASS): a snapshot for a
        different qB instance never consumes a version discovered from
        another one (ISSUE 5).
        """
        if cfg is None:
            cfg = config.snapshot()
        key = (cfg.QB_URL, cfg.QB_USER, cfg.QB_PASS)
        if not force:
            # One immutable tuple: the read is atomic, so the key, the
            # version, the major and the timestamp always agree (ISSUE 5).
            cache = self._app_version_cache
            if cache is not None and cache[0] == key:
                if (cache[3] is None
                        or time.monotonic() - cache[3]
                        < self.VERSION_CACHE_TTL):
                    return cache[1]
        r = self.api("GET", "/api/v2/app/version", cfg=cfg)
        if r is None or r.status_code != 200:
            if force:
                # A failed forced re-check must clear trust, not keep it —
                # but only this key's trust: another key's cache is still
                # valid.
                with self.lock:
                    if self._app_version_cache is not None \
                            and self._app_version_cache[0] == key:
                        self._app_version_cache = None
            return None
        version = r.text.strip()
        with self.lock:
            self._app_version_cache = (key, version, _parse_qb_major(version),
                                       time.monotonic())
        return version

    def supports_gateway(self, *, force: bool = False, cfg=None) -> bool | None:
        """True if qBittorrent is exactly 5.x, False if a different major
        is reachable, None if the version could not be determined.

        The Gateway requires major 5 (not >= 5): a future qB 6 may make
        another incompatible API change and is unsupported until
        explicitly tested.
        """
        version = self.app_version(force=force, cfg=cfg)
        if version is None:
            return None
        # Derive the major from the returned LOCAL version, not from the
        # shared cache: another thread may have overwritten the cache for
        # a different key between the lookup and this check (ISSUE 5).
        return _parse_qb_major(version) == 5

    def transfer_info(self, cfg=None):
        r = self.api("GET", "/api/v2/transfer/info", cfg=cfg)
        return r.json() if r is not None and r.status_code == 200 else None

    def torrents_info(self, cfg=None):
        r = self.api("GET", "/api/v2/torrents/info", cfg=cfg)
        return r.json() if r is not None and r.status_code == 200 else None

    def torrent_present(self, h: str, cfg=None) -> bool | None:
        """True/False = qB answered whether h exists; None = unknown.

        /torrents/info?hashes=<id> is supported on qB 5.0, 5.1 and 5.2.
        Used as the add preflight (ISSUE 3): the gateway may only add a
        torrent whose absence it positively verified — that is what makes
        the ownership-gated rollback sound.  An unreadable answer is a
        failure, never an "absent" assumption."""
        r = self.api("GET", "/api/v2/torrents/info", params={"hashes": h},
                     cfg=cfg)
        if r is None or r.status_code != 200:
            return None
        try:
            items = r.json()
        except Exception:
            return None
        if not isinstance(items, list):
            return None
        return any(str(t.get("hash") or "").lower() == h.lower()
                   for t in items)

    def files(self, h: str, cfg=None):
        """Per-file list for one torrent. Entries carry the file path
        relative to the torrent's save dir (qBittorrent 5.x: in "name";
        some versions: in "path")."""
        r = self.api("GET", "/api/v2/torrents/files", params={"hash": h}, cfg=cfg)
        if r is not None and r.status_code == 200:
            try:
                return r.json()
            except Exception:
                return None
        return None

    def create_category(self, name: str, savepath: str, cfg=None):
        return self.api("POST", "/api/v2/torrents/createCategory",
                        data={"category": name, "savePath": savepath}, cfg=cfg)

    def edit_category(self, name: str, savepath: str, cfg=None):
        return self.api("POST", "/api/v2/torrents/editCategory",
                        data={"category": name, "savePath": savepath}, cfg=cfg)

    def ensure_category(self, name: str, savepath: str, cfg=None) -> bool:
        """Create a category, or update its path when it already exists."""
        r = self.create_category(name, savepath, cfg=cfg)
        if _qb_mutation_ok(r):
            return True
        # Real qB 5.x answers 409 Conflict when the category already
        # exists (never 400): fall through to a path update.
        if r is not None and r.status_code == 409:
            r = self.edit_category(name, savepath, cfg=cfg)
        return _qb_mutation_ok(r)

    def add(self, category: str, savepath: str, urls: str | None = None,
            torrent_file: tuple | None = None, stopped: bool = False, cfg=None):
        if cfg is None:
            cfg = config.snapshot()
        if stopped and self.supports_gateway(cfg=cfg) is not True:
            # The "stopped" add option is qBittorrent 5.x-only.  On 4.x it
            # is silently ignored, so a guarded add would start the
            # torrent immediately and the check/add race would not be
            # closed.  Refuse locally rather than send an unsafe request.
            raise QBUnsupportedVersion(
                "stopped add requires qBittorrent 5.x")
        fields = {"category": category, "savePath": savepath}
        if stopped:
            fields["stopped"] = "true"
        if urls:
            # Defense in depth: qBittorrent's 'urls' field is a
            # NEWLINE-SEPARATED URL list, but the gateway's add protocol
            # (preflight/ownership/start/rollback/record) is single-torrent.
            # A multi-URL value must never reach qB from this client, even
            # if a future caller bypasses the HTTP boundary validation.
            if "\r" in urls or "\n" in urls:
                raise ValueError("QBClient.add accepts exactly one URL")
            return self.api("POST", "/api/v2/torrents/add",
                            data={"urls": urls, **fields}, cfg=cfg)
        if torrent_file:
            return self.api("POST", "/api/v2/torrents/add",
                            data=fields,
                            files={"torrents": torrent_file}, cfg=cfg)
        raise ValueError("no urls or torrent_file")

    def app_preferences(self, cfg=None):
        r = self.api("GET", "/api/v2/app/preferences", cfg=cfg)
        return r.json() if r is not None and r.status_code == 200 else None

    def set_preference(self, key: str, value, cfg=None) -> bool:
        r = self.api("POST", "/api/v2/app/setPreferences",
                     data={"json": json.dumps({key: value})}, cfg=cfg)
        return _qb_mutation_ok(r)

    def set_preferences(self, values: dict, cfg=None) -> bool:
        r = self.api("POST", "/api/v2/app/setPreferences",
                     data={"json": json.dumps(values)}, cfg=cfg)
        return _qb_mutation_ok(r)

    def set_category(self, h: str, cat: str, cfg=None):
        return self.api("POST", "/api/v2/torrents/setCategory", data={"hashes": h, "category": cat}, cfg=cfg)

    def delete(self, h: str, delete_files: bool = False, cfg=None):
        return self.api("POST", "/api/v2/torrents/delete",
                        data={"hashes": h, "deleteFiles": "true" if delete_files else "false"},
                        cfg=cfg)

    def _torrent_control(self, verb: str, hashes: str, cfg=None) -> bool:
        """A stop/start mutation with a FRESH version gate.

        The version is re-queried with force=True BEFORE the mutation:
        the TTL cache must never authorize a stop/start, because a
        qBittorrent replaced or restarted to an unsupported version in
        the last 30 seconds must be detected before the NEXT
        torrent-control call, not 30 seconds later.  The gate and the
        POST run under the client lock (which api() already takes for
        the whole auth + request transaction, and which is reentrant),
        so no other thread's mutation or version lookup can interleave
        between the fresh check and the request.

        Truthy ONLY if the mutation actually succeeded: an
        httpx.Response is truthy even for HTTP 500, so the status is
        mapped explicitly — a failed stop must never look like a
        successful one.
        """
        if cfg is None:
            cfg = config.snapshot()
        with self.lock:
            if self.supports_gateway(force=True, cfg=cfg) is not True:
                # stop/start are the qB 5.x protocol; on an unsupported
                # version the guard must not treat a 4.x install as
                # guarded.
                return False
            r = self.api("POST", f"/api/v2/torrents/{verb}",
                         data={"hashes": hashes}, cfg=cfg)
        return _qb_mutation_ok(r)

    def pause_all(self, cfg=None) -> bool:
        # "all" is qBittorrent's real all-torrents token (qB 5.0-5.2);
        # any other value is parsed as torrent IDs, so a fabricated
        # token would silently pause nothing (ISSUE 2).
        return self._torrent_control("stop", "all", cfg)

    def resume_all(self, cfg=None) -> bool:
        return self._torrent_control("start", "all", cfg)

    def active_hashes(self, cfg=None) -> list[str] | None:
        torrents = self.torrents_info(cfg)
        if torrents is None:
            return None
        return [t["hash"] for t in torrents
                if t.get("hash") and (t.get("state") or "") not in INACTIVE_STATES]

    def pause_hashes(self, hashes: list[str], cfg=None) -> bool:
        if not hashes:
            return True
        return self._torrent_control("stop", "|".join(hashes), cfg)

    def resume_hashes(self, hashes: list[str], cfg=None) -> bool:
        if not hashes:
            return True
        return self._torrent_control("start", "|".join(hashes), cfg)

