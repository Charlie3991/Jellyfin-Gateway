"""Persistent gateway state: sort records, event log, record-state
semantics (ISSUE 6, phase 2).

Pure move out of gateway.py: the on-disk state file (records + activity
log), its validators, and the predicates that define the meaning of the
persisted sort_status / cleanup_status fields (including the
transition rule enforced by claim_sort()).  The watcher/orchestrator
modules consume these; nothing here imports the gateway.
"""
import json
import logging
import math
import os
import shutil
import tempfile
import threading
import time
import weakref
from pathlib import Path

import sorter

log = logging.getLogger("gateway")


def _artifact_presence(path: Path) -> str:
    """Tri-state probe for a state artifact: 'missing' | 'present' | 'error'.

    The loaders must distinguish "this file never existed" (a
    genuinely fresh install) from "this file exists but cannot be
    examined right now" (dying disk, stale handle, permission error).
    Path.exists() cannot make that call: on Python < 3.13 it swallows
    filesystem errors (an unstat-able file reads as ABSENT — a failing
    disk could masquerade as a fresh install), and on 3.13+ it raises.
    lstat() gives the explicit split: FileNotFoundError means
    genuinely absent; any other OSError means present but unexaminable
    ("error").  The same reason sorter._sweep_dir() avoids
    Path.exists().
    """
    try:
        path.lstat()
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "error"
    return "present"


# ---------------------------------------------------------------------------
# persistent state (sort records + event log)
# ---------------------------------------------------------------------------

class StatePersistenceError(RuntimeError):
    """A required (durability-barrier) state write failed.

    Raised only by save(required=True): the transaction state that a
    destructive sort phase depends on could not be made durable, so the
    phase must stop instead of continuing on an in-memory-only record.
    """


class StateCorruptionError(RuntimeError):
    """The persisted sort state could not be loaded in any safe form.

    Raised from StateStore.__init__ when a state artifact exists but
    neither the primary file nor the retained backup parses and
    validates.  The unusable artifact is PRESERVED IN PLACE — never
    moved, quarantined, or overwritten — so every restart fails closed
    the same way until an administrator explicitly repairs, replaces,
    or removes it.  Moving it aside would make the next restart look
    like a fresh install and start with an empty state, letting the
    watcher adopt already-sorted torrents as new work; an empty state
    is not an option for transaction state.
    """


# ISSUE 10: the persisted state is TRANSACTION state — it decides
# whether destructive/resumable filesystem work runs.  Anything that
# loads must pass structural validation; parseable-but-malformed is
# treated exactly like unparseable.
_SORT_STATUS_OK = ("planning", "sorting", "incomplete", "failed", "sorted")
_SORT_CORRUPT_MSG = ("persisted sort state is invalid; "
                     "administrator intervention required")
_CLEANUP_CORRUPT_MSG = ("persisted cleanup metadata is invalid; "
                        "administrator intervention required")
_ADD_VERIFY_CORRUPT_MSG = ("persisted add-verification state is invalid; "
                           "administrator intervention required")


def _finite_number(value):
    """True for an actual finite int/float (bool is not a number here)."""
    return (isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value))


def _validate_torrent_state(value):
    """Whole-file shape check for sort_state.json.

    Deliberately minimal and backward-compatible: old records and
    adopted records have evolved over time, and the goal is to prevent
    methods like rec.get() from crashing — not to schema-police history.
    Raises ValueError on any violation.
    """
    if not isinstance(value, dict):
        raise ValueError("sort state must be a JSON object")
    for h, rec in value.items():
        if not isinstance(h, str):
            raise ValueError("torrent-state keys must be strings")
        if not isinstance(rec, dict):
            raise ValueError(f"torrent record {h!r} must be an object")
    return value


def _validate_event_log(value):
    """Whole-file shape check for sort_log.json.

    /api/sortlog calls dict(event) on every entry, so each entry must be
    an object.  Raises ValueError on any violation.
    """
    if not isinstance(value, list):
        raise ValueError("activity log must be a JSON array")
    if not all(isinstance(x, dict) for x in value):
        raise ValueError("every activity-log entry must be an object")
    return value


def _validate_persisted_plan(plan):
    """Structural validation of a persisted sort plan (ISSUE 10).

    Establishes only that this is structurally an executable plan — the
    fields SortPlan.from_dict() and _run_sort_plan() rely on.  A plan
    that could never have executed (or whose bookkeeping is broken) must
    quarantine its record as terminal: it is never re-planned from
    scratch, because some ops may already have happened.  Destination-
    root confinement is deliberately NOT checked here (issue 11).
    Raises ValueError on any violation.

    PLAN_VERSION 2 (ISSUE 13): every op must carry its persisted source
    fingerprint (source_size int >= 0, source_sha256 64 lowercase hex
    chars) — it is the op's ownership proof for crash recovery, so it is
    required, never defaulted.  Older versions (e.g. 1) are unsupported
    transaction state and quarantine the record: a v1 plan that already
    ran some ops could not be proven safe to resume.
    """
    if not isinstance(plan, dict):
        raise ValueError("sort plan must be an object")
    version = plan.get("version", sorter.PLAN_VERSION)
    if (isinstance(version, bool) or not isinstance(version, int)
            or version != sorter.PLAN_VERSION):
        raise ValueError(f"unsupported sort plan version {version!r}")
    for field in ("kind", "title"):
        if field in plan and not isinstance(plan[field], str):
            raise ValueError(f"sort plan {field} must be a string")
    dest_dir = plan.get("dest_dir")
    if not isinstance(dest_dir, str) or not dest_dir:
        raise ValueError("sort plan dest_dir must be a non-empty string")
    content_dir = plan.get("content_dir")
    if content_dir is not None and not isinstance(content_dir, str):
        raise ValueError("sort plan content_dir must be null or a string")
    if not isinstance(plan.get("preserve_source", False), bool):
        raise ValueError("sort plan preserve_source must be a boolean")
    ops = plan.get("ops", [])
    if not isinstance(ops, list):
        raise ValueError("sort plan ops must be an array")
    for op in ops:
        if (not isinstance(op, dict)
                or not isinstance(op.get("src"), str) or not op.get("src")
                or not isinstance(op.get("dst"), str) or not op.get("dst")):
            raise ValueError("every plan op needs non-empty string src and dst")
        size = op.get("source_size")
        if (isinstance(size, bool) or not isinstance(size, int)
                or size < 0):
            raise ValueError(
                "every plan op needs an integer source_size >= 0")
        digest = op.get("source_sha256")
        if (not isinstance(digest, str) or len(digest) != 64
                or any(c not in "0123456789abcdef" for c in digest)):
            raise ValueError(
                "every plan op needs a 64-lowercase-hex-char source_sha256")
    if not isinstance(plan.get("notes", []), list):
        raise ValueError("sort plan notes must be an array")
    if not isinstance(plan.get("ok", True), bool):
        raise ValueError("sort plan ok must be a boolean")
    return plan


class StateStore:
    # A fully completed record (sorted + cleanup complete) is dropped from
    # the state file once it is this old: long enough for recent history
    # and debugging, short enough to bound steady-state growth.  The
    # activity log (capped at 300 entries) already keeps the recent
    # operational history, so dropping the record is not a data loss.
    PRUNE_AFTER_DAYS = 30
    # The watcher re-checks for prunable records at most this often.
    PRUNE_INTERVAL_SECONDS = 6 * 3600

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.data_dir / "sort_state.json"
        self.log_file = self.data_dir / "sort_log.json"
        self.lock = threading.RLock()
        # Per-torrent sort locks are WEAK references: an entry disappears
        # as soon as no thread holds or waits on its lock, so dead hashes
        # do not accumulate forever.  A caller holds the strong reference
        # in a local variable for the whole acquire/release window, which
        # is exactly the window in which another caller must observe the
        # SAME lock object.
        self._sort_locks: "weakref.WeakValueDictionary[str, threading.Lock]" = \
            weakref.WeakValueDictionary()
        self.torrents: dict[str, dict] = {}
        self.events: list[dict] = []
        self._last_prune = 0.0
        self._load()

    @staticmethod
    def _read_valid_json(path: Path, validate):
        """Read, parse, and structurally validate one JSON artifact.

        Returns the validated value; raises ValueError for a missing
        file, an unreadable file, bad JSON, or a wrong shape.  Callers
        decide how a failure is handled (fatal vs best-effort).
        """
        try:
            raw = path.read_text()
        except FileNotFoundError:
            raise ValueError(f"{path.name}: no such file") from None
        except OSError as exc:
            raise ValueError(f"{path.name}: unreadable ({exc})") from exc
        try:
            value = json.loads(raw)
        except Exception as exc:
            raise ValueError(f"{path.name}: invalid JSON ({exc})") from exc
        try:
            return validate(value)
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"{path.name}: invalid structure ({exc})") from exc

    @staticmethod
    def _restore_canonical_no_rotate(path: Path, value) -> None:
        """Write `value` to the canonical path WITHOUT rotating the
        existing file into .bak (ISSUE 10 recovery paths only).

        _atomic_json_write() first rotates the existing canonical file
        into its .bak — right for an ordinary new state generation,
        wrong for a recovery-from-backup: the .bak is the known-good
        copy being restored from, and rotating would poison it with
        whatever (possibly corrupt) content still sits at the canonical
        path.  Same atomic write-temp / fsync / replace / fsync-dir
        mechanics, minus the rotation.
        """
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.",
                                        dir=str(path.parent))
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(value, fh, indent=1)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, path)
            try:
                dir_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    @staticmethod
    def _quarantine_path(path: Path) -> Path:
        """A unique sidecar name for an unusable artifact.

        <name>.corrupt-<time_ns>-<n>, incrementing n until the name is
        unused: two corruptions in the same instant (or a stale
        sidecar) can never clobber each other.
        """
        stamp = time.time_ns()
        n = 0
        while True:
            cand = path.with_name(f"{path.name}.corrupt-{stamp}-{n}")
            try:
                if not cand.exists():
                    return cand
            except OSError:
                # the name cannot be verified at all (broken
                # filesystem).  Return it: the move/copy that follows
                # will either fail safely (caught by the caller) or
                # land harmlessly — but we must not spin forever
                # trying to prove uniqueness we cannot prove.
                return cand
            n += 1

    def _load_state_fail_closed(self) -> dict:
        """Load sort state: the transaction truth, so it fails CLOSED.

        Decision table (primary = sort_state.json, backup = its .bak):

          primary valid               -> use primary
          primary bad, backup valid   -> quarantine the primary, restore
                                         the backup to the canonical
                                         path, use it
          primary missing, backup     -> restore the backup to the
          valid                         canonical path, use it
          nothing usable              -> StateCorruptionError, EXCEPT a
                                         genuinely fresh install (BOTH
                                         artifacts positively confirmed
                                         absent via FileNotFoundError —
                                         a stat error on either is
                                         "unusable", never "absent")
                                         which starts with {}

        In the fatal case the unusable artifact is PRESERVED IN PLACE
        (plus a uniquely named forensic copy): moving it away would make
        the next restart look like a fresh install and start with {},
        letting the watcher adopt already-sorted torrents as new work.
        An empty state is not an option for transaction state.
        """
        primary = self.state_file
        backup = primary.with_suffix(primary.suffix + ".bak")

        def _try(path: Path):
            pres = _artifact_presence(path)
            if pres == "missing":
                return None, None  # never existed (distinct from unusable)
            if pres == "error":
                # exists but unstat-able: UNUSABLE, never "missing" —
                # uncertainty about existing state must not read as a
                # fresh install.
                return None, f"{path.name}: cannot examine artifact (stat failed)"
            try:
                return (self._read_valid_json(path, _validate_torrent_state),
                        None)
            except ValueError as exc:
                return None, str(exc)

        p_value, p_err = _try(primary)
        if p_value is not None:
            return p_value
        b_value, b_err = _try(backup)
        if b_value is not None:
            if p_err is not None:
                # corrupt primary + usable backup: quarantine the primary
                # (collision-proof sidecar name) and restore the backup.
                # A quarantine failure is FATAL: continuing would restore
                # over the corrupt primary while it is still the .bak
                # rotation source, destroying the only valid recovery
                # copy — the exact thing fail-closed loading must never
                # do.
                try:
                    quar = self._quarantine_path(primary)
                    os.replace(primary, quar)
                    log.warning("sort state %s unusable (%s); quarantined "
                                "to %s, restored from backup %s",
                                primary, p_err, quar, backup)
                except OSError as exc:
                    raise StateCorruptionError(
                        f"sort state backup is valid, but corrupt primary "
                        f"{primary} could not be preserved: {exc}") from exc
            else:
                log.warning("sort state %s is missing; restored from "
                            "backup %s", primary, backup)
            # restore the canonical primary immediately: the process must
            # never run on an in-memory-only backup value.  No .bak
            # rotation: the backup is the known-good source.
            self._restore_canonical_no_rotate(primary, b_value)
            return b_value

        # No usable artifact at all.
        if p_err is None and b_err is None:
            return {}  # genuinely fresh install: no state ever existed
        for art, err in ((primary, p_err), (backup, b_err)):
            if err is not None:
                try:
                    forens = self._quarantine_path(art)
                    shutil.copy2(art, forens)
                    log.error("unusable %s preserved in place (forensic "
                              "copy %s): %s", art, forens, err)
                except OSError as exc:
                    log.error("could not make a forensic copy of %s: %s",
                              art, exc)
        raise StateCorruptionError(
            f"sort state is unrecoverable: primary {primary}: {p_err}; "
            f"backup {backup}: {b_err}. The unusable file(s) were left in "
            f"place and are preserved; restore from a backup or repair the "
            f"file manually. Moving the state file aside yourself starts "
            f"the gateway with a FRESH state (already-sorted torrents may "
            f"be reprocessed).")

    def _load_events_best_effort(self) -> list:
        """Load the activity log: corruption here is NON-fatal.

        The log carries no transaction state — losing history never
        authorizes filesystem work — so an unusable log is quarantined
        (moved to a uniquely named sidecar) and the store continues with
        an empty log.  A usable backup is restored to the canonical log
        path, mirroring the state-file recovery.
        """
        primary = self.log_file
        backup = primary.with_suffix(primary.suffix + ".bak")

        def _try(path: Path):
            pres = _artifact_presence(path)
            if pres == "missing":
                return None, None
            if pres == "error":
                # unstat-able: degrade to an empty log INTENTIONALLY
                # (the log is non-transactional), never as "missing".
                return None, f"{path.name}: cannot examine artifact (stat failed)"
            try:
                return (self._read_valid_json(path, _validate_event_log),
                        None)
            except ValueError as exc:
                return None, str(exc)

        p_value, p_err = _try(primary)
        if p_value is not None:
            return p_value
        b_value, b_err = _try(backup)
        if b_value is not None:
            if p_err is not None:
                try:
                    quar = self._quarantine_path(primary)
                    os.replace(primary, quar)
                    log.warning("activity log %s unusable (%s); quarantined "
                                "to %s, restored from backup %s",
                                primary, p_err, quar, backup)
                except OSError as exc:
                    # non-transactional log: a failed quarantine is
                    # logged, not fatal — but the restore must still not
                    # rotate .bak (same backup-poisoning pattern).
                    log.error("could not quarantine unusable activity log "
                              "%s: %s", primary, exc)
            self._restore_canonical_no_rotate(primary, b_value)
            return b_value
        for art, err in ((primary, p_err), (backup, b_err)):
            if err is not None:
                try:
                    quar = self._quarantine_path(art)
                    os.replace(art, quar)
                    log.error("unusable activity log %s quarantined to %s",
                              art, quar)
                except OSError as exc:
                    log.error("could not quarantine unusable activity log "
                              "%s: %s", art, exc)
        return []

    def _load(self):
        self.torrents = self._load_state_fail_closed()
        self.events = self._load_events_best_effort()
        self._validate_loaded_records()
        self._recover_stale_sorting()
        self.prune_completed()

    def _validate_loaded_records(self) -> None:
        """Validate the execution-critical fields of every loaded record
        (ISSUE 10) — BEFORE stale-sort recovery, which itself reads
        sort_plan.

        A record whose transaction state cannot be trusted is
        quarantined LOGICALLY: terminal "failed" with state_corrupt=True
        (refused by claim_sort, even for a manual retry) and its
        persisted plan data kept untouched for inspection.  Disposable
        metadata is sanitized in place instead: it can be regenerated
        and is not authoritative filesystem state.
        """
        changed = False
        for rec in self.torrents.values():
            if self._sanitize_record(rec):
                changed = True
        if changed:
            log.warning("sanitized %d record(s) with invalid persisted "
                        "state at load; the corruption marker is durable",
                        sum(1 for r in self.torrents.values()
                            if r.get("state_corrupt")))
            self.save()

    @staticmethod
    def _sanitize_record(rec: dict) -> bool:
        """Validate/sanitize one loaded record's execution-critical
        fields.  Returns True when the record was changed.

        * an unusable persisted plan, broken plan bookkeeping
          (sort_completed / sort_swept), or an unknown sort_status
          quarantines the record logically: terminal "failed" +
          state_corrupt=True, with the plan data preserved untouched —
          it is never re-planned from scratch, because some ops may
          already have happened;
        * an unknown sort_status must not stay retryable, or the watcher
          would sort the record again;
        * a malformed cleanup_status on a SORted record is flagged for
          administrator attention (state_corrupt=True) but the record
          stays "sorted": already-sorted media must never become
          re-sortable; on a non-sorted record it is dead metadata and
          is dropped;
        * a malformed pending add-verification marker (ISSUE 1) quarantines
          the record: the watcher promotes/discards on it, so only the
          exact shape the add path writes (source "gateway", bool
          add_start_pending) is trusted;
        * disposable fields are dropped in place when malformed:
          sort_retry_after (a broken backoff must not delay retries),
          parsed (derived from the torrent name), cleanup_attempts,
          last_cleanup_warn_at.
        """
        changed = False
        corrupt_reason = None

        status = rec.get("sort_status")
        if status is not None and status not in _SORT_STATUS_OK:
            corrupt_reason = _SORT_CORRUPT_MSG

        # --- the persisted plan ---------------------------------------------
        plan = rec.get("sort_plan")
        plan_ok = None
        if plan is not None:
            try:
                _validate_persisted_plan(plan)
                plan_ok = plan
            except ValueError:
                if corrupt_reason is None:
                    corrupt_reason = _SORT_CORRUPT_MSG

        # --- plan bookkeeping -------------------------------------------------
        completed = rec.get("sort_completed")
        if completed is not None:
            if plan_ok is not None:
                ops = plan_ok.get("ops") or []
                if (not isinstance(completed, list)
                        or any(isinstance(x, bool) or not isinstance(x, int)
                               for x in completed)
                        or any(not (0 <= x < len(ops)) for x in completed)
                        or len(set(completed)) != len(completed)):
                    if corrupt_reason is None:
                        corrupt_reason = _SORT_CORRUPT_MSG
            else:
                # no usable plan: the bookkeeping is orphaned.  It is only
                # ever set together with the plan, so a non-empty list
                # without a plan is inconsistent transaction state.
                if not (isinstance(completed, list) and not completed):
                    if corrupt_reason is None:
                        corrupt_reason = _SORT_CORRUPT_MSG

        swept = rec.get("sort_swept")
        if swept is not None and not isinstance(swept, bool):
            # e.g. the string "false" is TRUTHY: a resume would skip the
            # final sweep.  Only an actual boolean is acceptable.
            if corrupt_reason is None:
                corrupt_reason = _SORT_CORRUPT_MSG

        # --- pending add-verification marker (ISSUE 1) ------------------------
        add_verification = rec.get("add_verification")
        if add_verification is not None:
            # Execution-critical transaction state: the watcher promotes or
            # discards on this marker, so a malformed one must not look
            # like a healthy pending add.  Only the exact marker the add
            # path writes is accepted: source "gateway" and a real bool
            # for the owed-start flag (a string "true" is NOT a bool).
            if (add_verification != "pending"
                    or rec.get("source") != "gateway"
                    or not isinstance(rec.get("add_start_pending"), bool)):
                if corrupt_reason is None:
                    corrupt_reason = _ADD_VERIFY_CORRUPT_MSG

        # --- disposable metadata ----------------------------------------------
        rta = rec.get("sort_retry_after")
        if rta is not None and not _finite_number(rta):
            rec.pop("sort_retry_after", None)
            changed = True
        parsed = rec.get("parsed")
        if parsed is not None and not isinstance(parsed, dict):
            rec.pop("parsed", None)
            changed = True
        attempts = rec.get("cleanup_attempts")
        if (attempts is not None
                and (isinstance(attempts, bool)
                     or not isinstance(attempts, int)
                     or attempts < 0)):
            rec.pop("cleanup_attempts", None)
            changed = True
        warn_at = rec.get("last_cleanup_warn_at")
        if warn_at is not None and not _finite_number(warn_at):
            rec.pop("last_cleanup_warn_at", None)
            changed = True

        # --- cleanup status -----------------------------------------------------
        cstatus = rec.get("cleanup_status")
        if cstatus is not None and cstatus not in ("pending", "complete"):
            if rec.get("sort_status") == "sorted":
                if corrupt_reason is None:
                    corrupt_reason = _CLEANUP_CORRUPT_MSG
            else:
                # cleanup only exists for sorted records; on anything else
                # it is dead metadata
                rec.pop("cleanup_status", None)
                changed = True

        # --- quarantine / flag ----------------------------------------------------
        if corrupt_reason is not None:
            # A SORted record whose only problem is malformed cleanup
            # metadata must STAY "sorted" (already-sorted media must
            # never become re-sortable): flag it for administrator
            # attention instead.  Every other corruption is terminal.
            keep_sorted = (rec.get("sort_status") == "sorted"
                           and corrupt_reason == _CLEANUP_CORRUPT_MSG)
            if not keep_sorted:
                rec["sort_status"] = "failed"
            rec["state_corrupt"] = True
            rec["error"] = corrupt_reason[:200]
            changed = True
        return changed

    def _recover_stale_sorting(self):
        """Recover records a dead process left mid-sort.

        A sort runs for seconds inside a single process, so a
        "sorting"/"planning" record found at load time can never be
        owned by a live worker: it belongs to a process that died
        mid-sort (crash, OOM kill, service restart).  Without this, the
        record is stuck forever — the watcher skips the torrent and
        manual sort answers 409.

        * "sorting" WITH a persisted plan: some files may already be in
          the library — keep the plan and mark the record "incomplete"
          so it is RESUMED (completed ops are verified and skipped),
          never re-planned or re-moved.
        * "sorting" without a plan, or "planning": the plan was never
          persisted, so nothing was moved — back the record to pending
          and the torrent is (re)sorted like any other once complete.
        """
        stale = [h for h, rec in self.torrents.items()
                 if rec.get("sort_status") in ("sorting", "planning")]
        for h in stale:
            rec = self.torrents[h]
            if rec.get("sort_status") == "sorting" and rec.get("sort_plan"):
                rec["sort_status"] = "incomplete"
                rec.setdefault("error",
                               "sort interrupted by a restart; will be resumed automatically")
                log.info("recovered interrupted sort %s: keeping %d-op plan "
                         "(%d op(s) already completed); will resume",
                         h[:12], len(rec["sort_plan"].get("ops") or []),
                         len(rec.get("sort_completed") or []))
            else:
                rec.pop("sort_status", None)
                rec.pop("error", None)
        if stale:
            log.info("recovered %d stale sort record(s) from a previous run: %s",
                     len(stale), ", ".join(h[:12] for h in stale))
            self.save()

    @staticmethod
    def _atomic_json_write(path: Path, value) -> None:
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        backup_tmp: Path | None = None
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(value, fh, indent=1)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            if path.exists():
                backup = path.with_suffix(path.suffix + ".bak")
                backup_tmp = Path(tmp_name + ".bak")
                shutil.copy2(path, backup_tmp)
                os.replace(backup_tmp, backup)
                backup_tmp = None
            os.replace(tmp_name, path)
            try:
                dir_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            if backup_tmp is not None:
                try:
                    backup_tmp.unlink()
                except OSError:
                    pass
            raise

    def save(self, *, required: bool = False) -> bool:
        """Persist state + activity log.

        required=True marks a durability barrier for a destructive sort
        phase: if the state file cannot be written, raise
        StatePersistenceError so the caller stops before doing anything
        that would outlive the record.  The default (required=False)
        keeps the historical best-effort behavior for status/log updates:
        failures are logged and reported as False.  A failure writing
        ONLY the activity log never fails a save — the transaction state
        is what matters.
        """
        with self.lock:
            try:
                self._atomic_json_write(self.state_file, self.torrents)
            except Exception as exc:
                log.exception("state save failed")
                if required:
                    raise StatePersistenceError(
                        f"could not persist sort state: {exc}") from exc
                return False
            try:
                self._atomic_json_write(self.log_file, self.events[-300:])
            except Exception:
                log.exception("activity log save failed")
            return True

    def record(self, h: str, **kw) -> dict:
        with self.lock:
            rec = self.torrents.setdefault(h, {})
            rec.update(kw)
            return rec

    def sort_lock(self, h: str) -> threading.Lock:
        with self.lock:
            return self._sort_locks.setdefault(h, threading.Lock())

    @staticmethod
    def _prunable(rec: dict, cutoff: float) -> bool:
        """True when a record is fully complete and old enough to drop.

        Only a record that is BOTH sorted and past its cleanup step is
        eligible.  Every other state is protected: pending, planning,
        sorting, incomplete, failed, and cleanup_status == "pending"
        (the qBittorrent delete is still outstanding).  A record with no
        completion timestamp has no known age, so it is kept (age is
        required, and "the torrent disappeared" is not an age).

        A provisional add record (add_verification == "pending", ISSUE 1)
        is protected explicitly: it is an OPEN transaction the watcher
        still reconciles, and a future refactor adding sort metadata to
        one of these records must not make it prunable.
        """
        if rec.get("add_verification") == "pending":
            return False
        if rec.get("sort_status") != "sorted":
            return False
        if rec.get("cleanup_status") == "pending":
            return False
        ts = rec.get("cleaned_at")
        if ts is None:
            ts = rec.get("sorted_at")
        if ts is None:
            return False
        try:
            return float(ts) <= cutoff
        except (TypeError, ValueError):
            return False

    def prune_completed(self, now: float | None = None) -> int:
        """Drop fully completed records older than PRUNE_AFTER_DAYS.

        Runs under the store lock.  A record whose per-hash sort lock is
        currently active (still present in the weak dictionary, i.e. a
        thread holds or waits on it) is never pruned, so pruning cannot
        race a running or waiting sort.  Returns the number of records
        dropped; performs one state save only when something changed.
        """
        if now is None:
            now = time.time()
        cutoff = now - self.PRUNE_AFTER_DAYS * 86400.0
        with self.lock:
            stale = [h for h, rec in self.torrents.items()
                     if self._prunable(rec, cutoff)
                     and h not in self._sort_locks]
            for h in stale:
                del self.torrents[h]
            if stale:
                log.info("pruned %d completed record(s) older than %d day(s)",
                         len(stale), self.PRUNE_AFTER_DAYS)
                self.save()
            self._last_prune = now
            return len(stale)

    def claim_sort(self, h: str, rec: dict, allow_failed: bool = False) -> bool:
        """Atomically transition a retryable record to sorting.

        allow_failed (the manual "Sort now" path only) also admits a
        genuinely "failed" record so an administrator can retry it after
        fixing the cause. "sorted" and "sorting" are never claimable: a
        retry must not re-sort a completed torrent or race an in-flight
        sort. A state_corrupt record is NEVER claimable — not even
        manually: its persisted transaction state is untrustworthy and
        must be repaired by an administrator, not re-executed (ISSUE
        10). The previous error is cleared on claim so a successful
        retry leaves no stale message behind.
        """
        with self.lock:
            if rec.get("state_corrupt"):
                return False
            if sort_terminal(rec) and not (
                    allow_failed and rec.get("sort_status") == "failed"):
                return False
            rec["sort_status"] = "sorting"
            rec.pop("error", None)
            self.save()
            return True

    def _save_events(self) -> bool:
        """Persist ONLY the activity log.  Event-only activity must not
        rewrite the full torrent state file (ISSUE 6)."""
        with self.lock:
            try:
                self._atomic_json_write(self.log_file, self.events[-300:])
                return True
            except Exception:
                log.exception("activity log save failed")
                return False

    def event(self, level: str, msg: str, public_msg: str | None = None):
        with self.lock:
            self.events.append({"ts": time.time(), "level": level, "msg": msg,
                                "public_msg": msg if public_msg is None else public_msg})
            self.events = self.events[-300:]
        self._save_events()



def legacy_incomplete(rec: dict) -> bool:
    """True for records poisoned by the pre-fix manual-sort bug: an
    *incomplete* manual sort attempt was persisted as a terminal
    "failed" state with the exact error "torrent is not complete yet".

    That was never a real sorting failure — the torrent simply had not
    finished downloading — so the record stays eligible for automatic
    sorting and is normalized back to pending. Genuine failures
    (content/filesystem/sorter errors) are intentionally untouched.
    """
    return (rec.get("sort_status") == "failed"
            and rec.get("error") == "torrent is not complete yet")


def sort_terminal(rec: dict) -> bool:
    """True when a record must not be sorted again: it was either
    genuinely sorted or genuinely failed with a real content,
    filesystem or sorter error. (A legacy "not complete yet" record is
    not terminal — it was never an actual sorting attempt.)

    This is the conservative rule used by the automatic watcher; an
    explicit manual retry may still claim a genuine failure via
    ``claim_sort(..., allow_failed=True)``.
    """
    if rec.get("sort_status") in ("sorted", "sorting"):
        return True
    return rec.get("sort_status") == "failed" and not legacy_incomplete(rec)


def cleanup_pending(rec: dict) -> bool:
    """True when the media files are already in the library but the
    qBittorrent delete has not been confirmed yet.

    File sorting and qBittorrent cleanup are separate states: such a
    record must never be re-sorted on the filesystem, only its
    qBittorrent cleanup (the delete) is retried.
    """
    return (rec.get("sort_status") == "sorted"
            and rec.get("cleanup_status") == "pending")

