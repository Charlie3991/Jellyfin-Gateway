"""Add transaction: post-add verification and reconciliation (ISSUE 6,
phase 4).

Pure move out of gateway.py: the lifecycle of a gateway-initiated add
after qBittorrent reports success - confirming the torrent by
observation, rolling it back (with confirmed/unconfirmed semantics),
promoting the provisional record once verified, and reconciling pending
records in the background when qBittorrent recovers.
"""
import config
import time

from qb import QBClient
from state_store import StateStore

# Post-add verification: after qBittorrent reports a successful add, the
# gateway confirms by observation that the torrent now exists.  A success
# response alone is not proof — a 5.2 report can claim success for a
# torrent that never materialized (and a qB restart can lose one) — while
# every downstream step (guard start, StateStore record, audit) assumes
# the torrent exists.  Bounded attempts + delay let a slow qB index update
# settle without failing a real add (post-add verification).
ADD_VERIFY_ATTEMPTS = 5
ADD_VERIFY_DELAY = 0.2
# How often a still-pending add verification may log a warning about a
# stuck reconciliation (so a long qBittorrent or VPN outage does not
# flood the log) — same policy as CLEANUP_WARN_INTERVAL (ISSUE 1).
ADD_VERIFY_WARN_INTERVAL = 300.0


def _confirm_added_torrent(qb: QBClient, h: str, cfg=None) -> str:
    """Post-add verification: confirm by observation that h now exists.

    /torrents/add success is a CLAIM, not a guarantee: the qB 5.2 JSON
    report can name h as added even though the torrent never materialized
    (a failed index write, a restart in flight).  Poll torrent_present()
    up to ADD_VERIFY_ATTEMPTS times, ADD_VERIFY_DELAY apart, and
    classify the whole sequence:

      "present"   — some attempt positively saw h;
      "absent"    — EVERY attempt got a readable "no": absence was
                    positively established for the entire window;
      "uncertain" — no attempt saw h and at least one attempt was
                    unreadable: qB accepted the add and then went
                    unreadable, so h may exist (ISSUE 1).

    A single early "no" never dominates later unreadability: an
    add-then-outage sequence is "uncertain", not "absent" — the caller
    must not treat it as proof that nothing exists.  No deletion happens
    here; the caller decides what each classification means.
    """
    saw_unreadable = False
    for attempt in range(ADD_VERIFY_ATTEMPTS):
        if attempt:
            time.sleep(ADD_VERIFY_DELAY)
        present = qb.torrent_present(h, cfg=cfg)
        if present is True:
            return "present"
        if present is None:
            saw_unreadable = True
    return "uncertain" if saw_unreadable else "absent"


def _rollback_added_torrent(qb: QBClient, h: str, cfg=None) -> tuple[bool, str]:
    """Undo a just-accepted add and report whether the removal is CONFIRMED.

    Unlike _delete_torrent() (the post-sort cleanup state machine), this is
    the rollback half of an add transaction: it must distinguish "delete
    requested" from "delete verified", because the /api/add error message
    promises the freshly added torrent is no longer in qBittorrent.
    Returns (removed, why).
    """
    r = qb.delete(h, delete_files=False, cfg=cfg)
    if r is None:
        return False, f"qBittorrent unreachable: {qb.last_error}"
    if r.status_code == 404:
        return True, "torrent already absent"
    if r.status_code not in (200, 204):
        return False, f"delete returned HTTP {r.status_code}"
    torrents = qb.torrents_info(cfg)
    if torrents is None:
        return False, "delete accepted but removal could not be verified"
    if any(t.get("hash") == h for t in torrents):
        return False, "delete returned success but torrent is still present"
    return True, "torrent removed"


def _promote_verified_add(store: StateStore, h: str, rec: dict) -> None:
    """Finish an add transaction (ISSUE 1): h's presence — and, if owed,
    its start — is confirmed, so the provisional record becomes a normal
    gateway add record.  Failure-atomic under the store lock, with the
    same guarantee as _discard_pending_add(): the marker fields are only
    considered promoted once the barrier save succeeded; if it fails,
    the in-memory record is restored to its exact pending state and the
    error propagates.  A promoted record must exist on disk — otherwise
    the running process would stop reconciling a transaction the state
    file still says is unfinished (and a restart would reload it).
    """
    with store.lock:
        if store.torrents.get(h) is not rec:
            raise RuntimeError(
                f"pending add record {h[:12]} changed during promotion")
        if rec.get("add_verification") != "pending":
            raise RuntimeError(
                f"add record {h[:12]} is no longer pending")
        old_markers = {k: rec[k]
                       for k in ("add_verification", "add_start_pending",
                                 "add_verification_started_at",
                                 "add_verification_warn_at")
                       if k in rec}
        had_verified_at = rec.get("add_verified_at")
        for k in old_markers:
            del rec[k]
        rec["add_verified_at"] = time.time()
        try:
            store.save(required=True)
        except Exception:
            if had_verified_at is not None:
                rec["add_verified_at"] = had_verified_at
            else:
                rec.pop("add_verified_at", None)
            rec.update(old_markers)
            raise


def _discard_pending_add(store: StateStore, h: str) -> None:
    """Remove the provisional record of an add that provably never
    materialized (ISSUE 1).  Durable transition: if the barrier save
    fails, the in-memory entry is restored, so a pending add can never be
    lost from BOTH disk and memory.  Only removes while the record is
    still the pending-add record (its marker intact), so a record that
    was meanwhile promoted or reused is never deleted.
    """
    with store.lock:
        rec = store.torrents.get(h)
        if rec is None or rec.get("add_verification") != "pending":
            return
        try:
            del store.torrents[h]
            store.save(required=True)
        except Exception:
            store.torrents[h] = rec
            raise


def _pending_add_warn(store: StateStore, h: str, rec: dict, detail: str) -> None:
    """Rate-limited warning for a stuck pending add (ISSUE 1): at most one
    per ADD_VERIFY_WARN_INTERVAL, so a long qBittorrent or VPN outage
    does not flood the activity log (same policy as _cleanup_failed).
    The throttle timestamp is persisted so the limit survives a restart.
    """
    now = time.time()
    if now - float(rec.get("add_verification_warn_at") or 0) \
            < ADD_VERIFY_WARN_INTERVAL:
        return
    rec["add_verification_warn_at"] = now
    store.event("warn",
                f"pending add {h[:12]}: {detail}; the transaction will be "
                f"reconciled once qBittorrent responds again")
    store.save()


def reconcile_pending_add(qb: QBClient, store: StateStore, h: str,
                          rec: dict, by_hash: dict, cfg) -> str:
    """Watcher reconciliation of a provisional add record (ISSUE 1).

    Returns "promoted" (transaction finished), "pending" (still owed:
    qB unreadable, tunnel down, or start unconfirmed) or "discarded"
    (the torrent provably never materialized; the record was removed).

    Existence: h in the CURRENT snapshot means present — no re-query.
    A miss in one snapshot is NOT absence: the pending state exists
    precisely because visibility was ambiguous, so run the full bounded
    confirm; only five readable "no"s may discard the record.

    A pending add that still owes its start is promoted only AFTER that
    start is confirmed: presence alone is not a complete transaction.
    """
    if h in by_hash:
        verification = "present"
    else:
        verification = _confirm_added_torrent(qb, h, cfg)
    if verification == "absent":
        # Nothing to delete from qB: five readable answers proved it
        # was never there.
        _discard_pending_add(store, h)
        store.event(
            "warn",
            f"pending add {h[:12]} was confirmed absent from qBittorrent; "
            f"provisional record removed",
            "A pending torrent add did not materialize",
        )
        return "discarded"
    if verification == "uncertain":
        _pending_add_warn(store, h, rec,
                          "qBittorrent is still unreadable; the add could "
                          "not be verified")
        return "pending"
    owed_start = bool(rec.get("add_start_pending"))
    if owed_start:
        # The add was submitted stopped (VPN_GUARD): the start is owed
        # and must succeed before the transaction is complete.
        if cfg.VPN_GUARD:
            if config.tunnel_iface(cfg.VPN_GUARD_IFACES) is None:
                _pending_add_warn(store, h, rec,
                                  "the torrent exists but the VPN tunnel is "
                                  "down; start deferred until it is up")
                return "pending"
        if not qb.resume_hashes([h], cfg=cfg):
            _pending_add_warn(store, h, rec,
                              "the start request could not be confirmed; "
                              "will retry on the next pass")
            return "pending"
    _promote_verified_add(store, h, rec)
    store.event("info",
                f"pending add {h[:12]} was verified in qBittorrent"
                + (" and started" if owed_start else ""))
    return "promoted"


