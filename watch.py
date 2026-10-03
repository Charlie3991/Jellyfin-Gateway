"""Background loops: the torrent watcher and VPN-guard integration
(ISSUE 6, phase 5).

Pure move out of gateway.py: the periodic watcher (auto-sort of
completed torrents, post-sort cleanup retries, pending-add
reconciliation) and the glue that keeps the VPN guard's paused state in
sync with the tunnel (make_guard / sync_guard_state / guard_loop /
apply_guard_change), plus the GUARD_STATE presentation dict they
maintain.
"""
import config
import logging
import threading
import time
from pathlib import Path

import sorter
from add_verify import reconcile_pending_add
from qb import QBClient
from sort_orchestrator import (is_complete, retry_cleanup, try_sort)
from state_store import (StateStore, cleanup_pending, legacy_incomplete,
                         sort_terminal)
from vpn_guard import Guard

log = logging.getLogger("gateway")

GUARD_STATE = {"enabled": config.VPN_GUARD, "tunnel": None, "paused": False,
               "last_event": "", "compatible": False}


def watch_loop(qb: QBClient, store: StateStore, stop: threading.Event,
               on_supported=None):
    while not stop.is_set():
        # One snapshot per iteration: the pass gate, the poll interval and
        # the download dir used for the disappeared-torrent resume all
        # come from the same generation of the configuration, and the
        # snapshot is passed through to every qB call of this tick
        # (ISSUE 5).
        cfg = config.snapshot()
        # Opportunistic retention: re-check for prunable completed
        # records at most once per PRUNE_INTERVAL_SECONDS (ISSUE 6).
        # The startup prune already ran in StateStore._load(); this
        # bounds growth for a long-running process.
        if time.time() - store._last_prune >= store.PRUNE_INTERVAL_SECONDS:
            store.prune_completed()
        try:
            if cfg.QB_PASS:
                # Runtime compatibility gate: never adopt/sort/cleanup on a
                # qBittorrent we do not support.  The version is cached (with
                # a short TTL), so this does not hit /app/version every poll;
                # if qB was offline at startup and later appears as 4.x, we
                # simply do no qB work until it is 5.x.
                if qb.supports_gateway(cfg=cfg) is not True:
                    stop.wait(cfg.WATCH_POLL)
                    continue
                if on_supported is not None:
                    on_supported()
                torrents = qb.torrents_info(cfg)
                if torrents is not None:
                    by_hash = {t.get("hash"): t for t in torrents if t.get("hash")}
                    # Reconcile provisional adds (ISSUE 1): the record
                    # exists because qB accepted the add but verification
                    # was ambiguous.  A quarantined record is left for the
                    # administrator; a pending add that still owes its
                    # start is skipped below, so it cannot fall through to
                    # adoption or sorting in this tick.
                    for h, rec in list(store.torrents.items()):
                        if rec.get("add_verification") != "pending":
                            continue
                        if rec.get("state_corrupt"):
                            continue
                        reconcile_pending_add(qb, store, h, rec, by_hash, cfg)
                    # A pending cleanup whose torrent is no longer in
                    # qBittorrent (removed manually, or the client lost it)
                    # is already satisfied: there is no delete to retry.
                    for h, rec in list(store.torrents.items()):
                        if cleanup_pending(rec) and h not in by_hash:
                            rec.update(cleanup_status="complete",
                                       cleaned_at=time.time())
                            store.event("info",
                                        f"torrent {h[:12]} is no longer in "
                                        f"qBittorrent; cleanup marked complete")
                            store.save()
                    # A torrent that disappeared from qBittorrent while its
                    # sort was interrupted: the files can still be finished
                    # locally (the delete is already satisfied — there is
                    # nothing left to remove), so resume the saved plan.
                    for h, rec in list(store.torrents.items()):
                        if h in by_hash:
                            continue
                        if rec.get("sort_status") != "incomplete" \
                                or not rec.get("sort_plan"):
                            continue
                        if time.time() < float(rec.get("sort_retry_after") or 0):
                            continue
                        try_sort(qb, store,
                                 {"hash": h, "name": rec.get("name") or "",
                                  "save_path": str(cfg.DOWNLOAD_DIR)},
                                 rec, allow_failed=True, cfg=cfg)
                    for t in torrents:
                        h = t.get("hash")
                        if not h:
                            continue
                        cat = (t.get("category") or "").lower()
                        if cat not in ("movies", "tv"):
                            continue
                        name = t.get("name") or ""
                        rec = store.torrents.get(h)
                        if rec is not None and \
                                rec.get("add_verification") == "pending":
                            # A pending add (ISSUE 1) is not yet a tracked
                            # torrent: it must not be adopted, title-
                            # refreshed or sorted until reconciliation
                            # promotes it.
                            continue
                        if rec is None:
                            parsed = sorter.parse_release(name)
                            rec = store.record(h, kind="tv" if cat == "tv" else "movie",
                                               name=name, title=parsed.get("title"),
                                               parsed=parsed, added=time.time(),
                                               source="adopted")
                        elif rec.get("title") in (None, "") and name:
                            parsed = sorter.parse_release(name)
                            rec.update(title=parsed.get("title"), parsed=parsed)
                            store.save()
                        if legacy_incomplete(rec):
                            # Recover records poisoned by the old bug so
                            # they can still be auto-sorted once complete
                            # instead of being skipped forever.
                            rec.pop("sort_status", None)
                            rec.pop("error", None)
                            store.save()
                        elif sort_terminal(rec):
                            # Files are already in the library; if the
                            # qBittorrent delete failed, retry ONLY the
                            # delete — never the filesystem sort.
                            if cleanup_pending(rec):
                                retry_cleanup(qb, store, h, rec, cfg)
                            continue
                        elif (rec.get("sort_status") == "incomplete"
                              and time.time() < float(rec.get("sort_retry_after") or 0)):
                            # A partial sort was just attempted: give the
                            # cause (disk filling up, FS reconnecting, ...)
                            # a moment before the automatic retry.
                            continue
                        if is_complete(t):
                            try_sort(qb, store, t, rec, cfg=cfg)
        except Exception:
            log.exception("watch tick failed")
        stop.wait(cfg.WATCH_POLL)


def make_guard(qb: QBClient, store: StateStore) -> Guard:
    """Build the shared VPN guard — the SAME implementation the standalone
    vpn_guard.py service uses (single source of truth for the kill switch).

    Fail-closed: it pauses ALL torrents whenever the tunnel is absent —
    including at startup — and re-pauses them if they become active again
    while the tunnel is still down. State is persisted so a reboot while the
    VPN is down stays fail-closed (and resumes once the tunnel returns)."""
    return Guard(
        qb,
        lambda: config.tunnel_iface(),
        state_file=Path(config.DATA_DIR) / "vpn_guard.json",
        url=config.public_service_url(config.QB_URL),
        on_event=lambda lvl, msg: store.event(lvl, "VPN guard: " + msg),
    )


def sync_guard_state(g: Guard, enabled: bool | None = None) -> None:
    """Mirror the shared guard's state into the dashboard-visible dict.

    ``enabled`` lets the caller pass the VPN_GUARD value from its own
    config snapshot so the dashboard flag and the guard decision come
    from the same generation (ISSUE 5)."""
    GUARD_STATE["enabled"] = config.VPN_GUARD if enabled is None else enabled
    GUARD_STATE["tunnel"] = g.prev_up
    GUARD_STATE["paused"] = g.guard_paused
    GUARD_STATE["last_event"] = g.last_event


def guard_loop(g: Guard, qb: QBClient, stop: threading.Event,
               tick_lock: threading.Lock) -> None:
    while not stop.is_set():
        # One snapshot per iteration: the poll interval, the pass gate,
        # the enabled flag and the interface globs all come from the same
        # generation of the configuration (ISSUE 5).
        cfg = config.snapshot()
        if stop.wait(cfg.VPN_GUARD_POLL):
            break
        try:
            # The enabled-check and the tick happen under the SAME lock the
            # Settings API uses, so a live disable can never be followed by
            # one last stale tick re-pausing the torrents that disable()
            # just resumed.
            with tick_lock:
                supported = qb.supports_gateway(cfg=cfg) if cfg.QB_PASS else None
                GUARD_STATE["compatible"] = supported is True
                if cfg.VPN_GUARD:
                    # Runtime compatibility gate: never tick (pause/resume)
                    # against a qBittorrent we do not support, and never
                    # report torrents as successfully guarded in that state.
                    # The Guard's ownership state is preserved; only the
                    # "currently guarding" presentation is suppressed.
                    if supported is not True:
                        sync_guard_state(g, cfg.VPN_GUARD)
                        continue
                    g.tick(tunnel=config.tunnel_iface(cfg.VPN_GUARD_IFACES),
                           cfg=cfg)
                elif g.guard_paused:
                    # Guard is disabled but a resume of guard-paused
                    # torrents is still pending (qBittorrent was
                    # unreachable when disable() ran). tick() must NOT run
                    # here — it can pause based on tunnel state. Only
                    # release the exact hashes this guard paused.
                    if g.resume_ownership_unknown:
                        # A fallback pause_all() left the ownership unknown:
                        # retrying cannot release it safely, so do not hammer
                        # retry_pending_resume() every poll. Leave the
                        # persisted state intact, surface the blocked state
                        # once; manual intervention is required.
                        g.report_unknown_ownership_once()
                    else:
                        g.retry_pending_resume(cfg)
                sync_guard_state(g, cfg.VPN_GUARD)
        except Exception:
            log.exception("guard tick failed")


def apply_guard_change(g: Guard, tick_lock: threading.Lock, changed: set[str]) -> None:
    """React IMMEDIATELY to a VPN_GUARD / VPN_GUARD_IFACES settings change,
    serialized with the guard thread (same lock as its regular tick, so a
    settings change can never race a tick):

      guard enabled (just turned on, or the interface list changed)
          -> immediate fail-closed tick(): if no tunnel matches the
             (possibly new) interface globs, pause the active torrents NOW
             instead of waiting for the next poll.
      guard disabled (just turned off)
          -> resume only the torrents the guard itself paused and clear
             that state (Guard.disable()).
      interface list changed while the guard is disabled
          -> re-evaluate the tunnel for the dashboard only
             (Guard.observe()); a disabled guard never touches torrents.

    `changed` is the set of setting names actually changed by this request
    (config values are already updated by the time this runs)."""
    # One snapshot: the enabled flag and the interface globs used for the
    # immediate reaction come from the same generation (ISSUE 5).
    cfg = config.snapshot()
    with tick_lock:
        if cfg.VPN_GUARD:
            g.tick(tunnel=config.tunnel_iface(cfg.VPN_GUARD_IFACES), cfg=cfg)
        elif "VPN_GUARD" in changed:
            g.disable(cfg=cfg)
        else:
            g.observe(tunnel=config.tunnel_iface(cfg.VPN_GUARD_IFACES))
        sync_guard_state(g, cfg.VPN_GUARD)


