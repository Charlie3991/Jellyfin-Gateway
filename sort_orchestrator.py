"""Sort orchestration: qB metadata -> plan -> persistence -> execution
-> cleanup/finalization (ISSUE 6, phase 3).

Pure move out of gateway.py: the steps between a completed torrent and
its media files landing in the library - resolving content, building
and persisting the sort plan, executing it through sorter.py, finishing
(post-sort qBittorrent cleanup, Jellyfin refresh) and the manual/auto
sort entry points.  Magnet parsing and the add-transaction helpers are
deliberately not part of this module.
"""
import config
import re
import stat
import time
from dataclasses import dataclass
from pathlib import Path

import sorter
from jellyfin_client import jellyfin_refresh
from qb import QBClient
from state_store import (StateStore, StatePersistenceError,
                         cleanup_pending, legacy_incomplete, sort_terminal)

# states where a torrent with progress >= 1.0 counts as "complete" for sorting.
# stoppedUP = finished but stopped/paused (e.g. by the VPN guard); its files
# are already on disk, so sorting (a local move) is safe even while paused.
UPLOADING_STATES = {"uploading", "stalledUP", "pausedUP", "checkingUP", "queuedUP",
                    "completed", "forcedUP", "stoppedUP"}

# How often a still-pending cleanup may log a warning about repeated
# failed deletes (so a long qBittorrent outage does not flood the log).
CLEANUP_WARN_INTERVAL = 300.0

# How long the automatic watcher waits after a PARTIAL sort attempt before
# retrying it (a manual "Sort now" is never throttled by this).
SORT_RETRY_BACKOFF = 5.0


def is_complete(t: dict) -> bool:
    try:
        prog = float(t.get("progress", 0))
    except (TypeError, ValueError):
        return False
    return prog >= 0.999 and t.get("state") in UPLOADING_STATES



class UnsafeTorrentPath(ValueError):
    """qBittorrent metadata points outside the configured DOWNLOAD_DIR.

    qB's save_path and per-file entries are untrusted location HINTS, not
    authorization to touch arbitrary filesystem paths: the gateway may
    only read/move/copy/sweep content whose resolved source path is
    inside the managed download dir.
    """


def require_download_path(path, *, under_save=None, root=None) -> Path:
    """Resolve ``path`` and require it to stay inside DOWNLOAD_DIR.

    Returns the resolved path.  Raises UnsafeTorrentPath when it escapes
    the configured download dir (including through symlinks), or — when
    ``under_save`` is given — when it leaves that torrent's validated
    save directory.

    ``root`` lets a caller pin the check to the DOWNLOAD_DIR from its own
    config snapshot, so one logical operation validates every path
    against the same generation of the layout (ISSUE 5).
    """
    if root is None:
        root = Path(config.DOWNLOAD_DIR).resolve()
    else:
        root = Path(root).resolve()
    candidate = Path(path).resolve()
    if candidate != root and root not in candidate.parents:
        raise UnsafeTorrentPath(
            f"{candidate} is outside DOWNLOAD_DIR {root}")
    if under_save is not None:
        save_root = Path(under_save).resolve()
        if candidate != save_root and save_root not in candidate.parents:
            raise UnsafeTorrentPath(
                f"{candidate} is outside the torrent save dir {save_root}")
    return candidate


def _require_plan_destinations(plan: sorter.SortPlan, cfg) -> None:
    """ISSUE 11: every persisted destination must still be inside the
    CURRENT library root for the plan's kind.

    Plan destinations were computed from the library roots at planning
    time, and the roots are runtime-editable: a persisted plan resumed
    after a root change would place media outside the current library.
    Both ``plan.dest_dir`` and every op destination are checked.  The
    check is strict (a destination equal to the root itself is refused)
    and symlink-safe (``resolve()``): an apparently nested path through
    a symlink must not escape the root.  Raises UnsafeTorrentPath.
    """
    if plan.kind == "movie":
        label, root = "movie library", Path(cfg.MOVIES_DIR).resolve()
    elif plan.kind == "tv":
        label, root = "TV library", Path(cfg.TV_DIR).resolve()
    else:
        raise UnsafeTorrentPath(f"sort plan has unsupported kind {plan.kind!r}")

    def check(value, what):
        p = Path(value).resolve()
        if root not in p.parents:
            raise UnsafeTorrentPath(
                f"{what} {p} is outside current {label} {root}")

    if plan.dest_dir:
        check(plan.dest_dir, "sort plan destination")
    for i, op in enumerate(plan.ops):
        dst = op.get("dst")
        if not dst:
            raise UnsafeTorrentPath(
                f"sort plan op {i} has an empty destination path")
        check(dst, f"sort plan op {i} destination")


def _plan_is_pristine_for_replan(plan, rec: dict, cfg) -> bool:
    """ISSUE 11: can a stale-root plan be proven to have moved nothing?

    ``sort_completed == []`` alone is NOT proof: an op mutates the
    filesystem first and checkpoints afterwards; a crash in that gap
    leaves a durable empty list while a destination already exists.
    Require, in addition, that every old destination is genuinely
    absent (``lstat``: a broken symlink or a directory still counts as
    occupied) and that every source is still a regular file inside the
    current DOWNLOAD_DIR.  Any uncertainty answers False (fail closed).
    """
    if rec.get("sort_completed"):
        return False
    if rec.get("sort_swept") is not False:
        return False
    try:
        if plan.content_dir is not None:
            require_download_path(plan.content_dir, root=cfg.DOWNLOAD_DIR)
        for op in plan.ops:
            src = require_download_path(op.get("src") or "",
                                        root=cfg.DOWNLOAD_DIR)
            if not stat.S_ISREG(Path(src).stat().st_mode):
                return False
            try:
                Path(op["dst"]).lstat()
            except FileNotFoundError:
                continue
            except OSError:
                return False
            return False  # anything occupies the old destination
    except (UnsafeTorrentPath, OSError):
        return False
    return True


def _qb_rel_parts(raw) -> list[str]:
    """Parse and validate one qBittorrent file entry (relative to the
    torrent's save dir).  Returns its path components.

    Raises UnsafeTorrentPath for empty entries, drive/UNC-style paths,
    and any ".", ".." or empty component (traversal).
    """
    raw = str(raw).replace("\\", "/").strip()
    if not raw:
        raise UnsafeTorrentPath("empty qBittorrent file entry")
    parts = raw.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise UnsafeTorrentPath(
            f"unsafe qBittorrent file entry: {raw[:120]!r}")
    if re.fullmatch(r"[A-Za-z]:", parts[0]):
        raise UnsafeTorrentPath(
            f"unsafe qBittorrent file entry (drive): {raw[:120]!r}")
    return parts


@dataclass
class ResolvedContent:
    """Where a torrent's content lives and which files qB selected.

    selected_files semantics:
      None        qB file list could not be obtained
      set()       qB returned a valid list, but zero files are selected
      {paths...}  authoritative set of files qB is supposed to download
    file_count: the total number of files in the (valid) qB file list,
      before any priority/selected filtering.  None when the list could
      not be obtained.  A genuinely single-file torrent has file_count
      == 1; a multi-file torrent has file_count > 1, so a single
      selected non-video file in a multi-file torrent means the user
      deselected the main content (not a single-file exception).
    """
    content_dir: Path | None
    single_file: Path | None
    selected_files: set[Path] | None
    file_count: int | None = None


def resolve_content(qb: QBClient, t: dict, save: Path, root=None,
                    cfg=None) -> ResolvedContent:
    """Locate the torrent's actual content under its save dir.

    Returns a ResolvedContent with (content_dir, single_file) — exactly
    one set — plus the authoritative set of selected file paths.  When
    the file list can't be obtained, selected_files is None and the
    caller falls back to guessing from the torrent name.  When the file
    list is valid but nothing is selected, selected_files is the empty
    set (the caller must NOT fall back to a name guess).  Raises
    UnsafeTorrentPath when a qB file entry is malformed or escapes the
    save dir: an unsafe entry must never degrade into a name guess.
    """
    files = qb.files(t.get("hash") or "", cfg)
    if files is None:
        return ResolvedContent(None, None, None, None)
    # The total file count (before filtering) is what makes a torrent
    # "genuinely single-file".  A multi-file torrent where the user
    # deselects everything but one non-video file is NOT a single-file
    # exception.
    file_count = len(files)
    save_root = Path(save).resolve()
    # ``root`` pins the DOWNLOAD_DIR check to the planning operation's
    # snapshot so a mid-planning settings change cannot re-validate the
    # file list against a different generation of the layout (ISSUE 5).
    if root is None:
        root = Path(config.DOWNLOAD_DIR).resolve()
    else:
        root = Path(root).resolve()
    rels: list[list[str]] = []
    selected: set[Path] = set()
    for f in files:
        if not isinstance(f, dict):
            continue
        # qB 5.x authoritative selection flag: priority 0 = do not
        # download. The optional selected field is a defensive
        # compatibility fallback only.
        if f.get("priority") == 0:
            continue
        if f.get("selected") is False:
            continue
        raw = f.get("name") or f.get("path") or ""
        parts = _qb_rel_parts(raw)
        candidate = (save_root / Path(*parts)).resolve()
        if candidate != save_root and save_root not in candidate.parents:
            raise UnsafeTorrentPath(
                f"qBittorrent file entry escapes the save dir: {raw[:120]!r}")
        if candidate != root and root not in candidate.parents:
            raise UnsafeTorrentPath(
                f"qBittorrent file entry is outside DOWNLOAD_DIR: {raw[:120]!r}")
        rels.append(parts)
        selected.add(candidate)
    if not rels:
        return ResolvedContent(None, None, set(), file_count)
    tops: list[str] = []
    for parts in rels:
        if parts[0] not in tops:
            tops.append(parts[0])
    dirs = [d for d in (save_root / top for top in tops) if d.is_dir()]
    if len(dirs) == 1:
        return ResolvedContent(dirs[0], None, selected, file_count)
    if not dirs:
        root_files = [save_root / Path(*parts) for parts in rels
                      if len(parts) == 1]
        root_files = [p for p in root_files if p.is_file()]
        if len(root_files) == 1:
            return ResolvedContent(None, root_files[0], selected, file_count)
    return ResolvedContent(None, None, selected, file_count)


def _build_and_persist_sort_plan(qb: QBClient, store: StateStore, t: dict,
                                 rec: dict, kind: str, parsed: dict, cfg):
    """Plan + the pre-move durability barrier, in exactly one place.

    Shared by the fresh-plan branch and the stale-root re-plan branch
    (ISSUE 11) so both cross the same durability path: the plan is
    persisted BEFORE a single file moves, and a failed barrier leaves a
    retryable record with no transaction fields behind.

    Returns the plan, or None when the record was already handled.
    """
    plan = _build_sort_plan(qb, store, t, rec, kind, parsed, cfg)
    if plan is None:
        return None
    h = t["hash"]
    rec["sort_plan"] = plan.to_dict()
    rec["sort_completed"] = []
    rec["sort_swept"] = False
    # The plan is persisted BEFORE a single file moves: from here on,
    # a crash (at any op, in any phase) leaves a resumable record —
    # never a half-sorted mystery with no record of intent.  The
    # status returns to "sorting" (the plan phase is over); a "planning"
    # record found on disk therefore always means nothing was moved.
    rec["sort_status"] = "sorting"
    try:
        store.save(required=True)
    except StatePersistenceError as exc:
        # The durability barrier failed: the plan is in memory only, so
        # NOTHING may move.  Discard the unpersisted transaction fields
        # (a later watcher tick must not reuse an in-memory plan that
        # never crossed the barrier) and back off; the next attempt
        # re-plans from scratch.
        rec.pop("sort_plan", None)
        rec.pop("sort_completed", None)
        rec.pop("sort_swept", None)
        rec.update(sort_status="incomplete",
                   error=f"could not persist sort plan: {exc}"[:200],
                   sort_retry_after=time.time() + SORT_RETRY_BACKOFF)
        store.event("warn",
                    f"sort {h[:12]}: plan could not be persisted; "
                    f"will retry automatically",
                    f"sort {h[:12]}: will retry automatically")
        store.save()
        return None
    return plan


def do_sort(qb: QBClient, store: StateStore, t: dict, rec: dict, cfg=None):
    # One snapshot for the whole sort operation (planning + execution +
    # cleanup) unless the caller already pinned one (ISSUE 5).
    if cfg is None:
        cfg = config.snapshot()
    h = t["hash"]
    name = t.get("name") or rec.get("name") or ""
    parsed = rec.get("parsed") or sorter.parse_release(name)
    if rec.get("title") in (None, "") and parsed.get("title"):
        rec["title"] = parsed["title"]
        rec["parsed"] = parsed
    kind = rec.get("kind") or parsed.get("kind") or "movie"

    plan = rec.get("sort_plan")
    if plan is None:
        plan = _build_and_persist_sort_plan(qb, store, t, rec, kind,
                                            parsed, cfg)
        if plan is None:
            return  # a terminal/retryable record state was already saved
    else:
        plan = sorter.SortPlan.from_dict(plan)
        # ISSUE 11: the persisted destinations were computed from the
        # library roots AT PLANNING TIME, and the roots are runtime-
        # editable.  Re-confine every destination against the CURRENT
        # roots of the plan's kind before trusting the plan.
        try:
            _require_plan_destinations(plan, cfg)
        except UnsafeTorrentPath as exc:
            if _plan_is_pristine_for_replan(plan, rec, cfg):
                # The filesystem proves nothing was placed under the
                # stale root: discard the stale bookkeeping and rebuild
                # the plan under the current roots.  The shared helper
                # carries the same pre-move durability barrier.
                rec.pop("sort_plan", None)
                rec.pop("sort_completed", None)
                rec.pop("sort_swept", None)
                rec.pop("sort_retry_after", None)
                store.event("info",
                            f"sort {h[:12]}: library root changed before "
                            f"any file placement; rebuilding the plan "
                            f"for the current library",
                            f"sort {h[:12]}: stale-root plan rebuilt")
                plan = _build_and_persist_sort_plan(qb, store, t, rec,
                                                    kind, parsed, cfg)
                if plan is None:
                    return
            else:
                # Partial progress — or a filesystem state we cannot
                # prove untouched: re-planning could double-place or hit
                # missing sources.  Terminal failed, with the plan and
                # its bookkeeping retained for forensics and manual
                # recovery: restoring the old root makes the original
                # plan valid again.
                done = len(rec.get("sort_completed") or [])
                if done:
                    why = (f"{done} operation(s) durably recorded complete"
                           " and additional filesystem progress may exist")
                else:
                    why = ("no completed operations durably recorded, but "
                           "filesystem state cannot prove the old plan "
                           "untouched")
                # The full path detail goes to the (untruncated) activity
                # event below; the record error stays within its 200-char
                # budget so the verdict is never cut off.
                rec.update(sort_status="failed",
                           error=("persisted sort plan targets a previous "
                                  f"library root; {why}; administrator "
                                  f"intervention required")[:200])
                rec.pop("sort_retry_after", None)
                store.event("error",
                            f"sort {h[:12]}: stale plan destination ({exc}); "
                            f"{why}; administrator intervention required",
                            f"sort {h[:12]}: stale-root plan; intervention required")
                store.save()
                return
    _run_sort_plan(qb, store, h, rec, plan, cfg)


def _build_sort_plan(qb: QBClient, store: StateStore, t: dict, rec: dict,
                     kind: str, parsed: dict, cfg=None):
    """Planning phase: decide every destination WITHOUT touching the
    filesystem.

    Returns the SortPlan, or None when the record was left in a saved
    state instead:
      * "failed" (terminal) — content missing, or no recognizable video
      * "incomplete" (retryable) — planning itself was interrupted by an
        I/O error; nothing was moved, so a retry simply re-plans.
    """
    # One snapshot for the whole planning operation: the download dir,
    # the library dirs and the sweep policy come from the same generation
    # of the configuration (ISSUE 5).
    if cfg is None:
        cfg = config.snapshot()
    h = t["hash"]
    name = t.get("name") or rec.get("name") or ""
    raw_save = Path(t.get("save_path") or cfg.DOWNLOAD_DIR)
    try:
        save = require_download_path(raw_save, root=cfg.DOWNLOAD_DIR)
    except UnsafeTorrentPath:
        # The torrent's content lives outside the configured download dir:
        # the gateway must not read, move, or sweep it.  Terminal "failed"
        # (not retryable): automatic retries cannot fix a deterministic
        # path-policy violation — an admin moves the content or the
        # DOWNLOAD_DIR, then retries manually.
        rec.update(sort_status="failed",
                   error="torrent content is outside configured DOWNLOAD_DIR")
        store.event("error",
                    f"sort {h[:12]} refused: qBittorrent save_path "
                    f"{raw_save} is outside DOWNLOAD_DIR "
                    f"{Path(cfg.DOWNLOAD_DIR).resolve()}",
                    f"sort {h[:12]} refused: outside DOWNLOAD_DIR")
        store.save()
        return None
    # The folder qBittorrent creates is named after the torrent's *content*
    # name (info.name), which often differs from the torrent *name* shown in
    # the list (e.g. a magnet's "dn"). Ask qBittorrent where the files are.
    # An unsafe file entry must NOT degrade into the name fallback below.
    try:
        resolved = resolve_content(qb, t, save, root=cfg.DOWNLOAD_DIR, cfg=cfg)
    except UnsafeTorrentPath as exc:
        rec.update(sort_status="failed",
                   error=f"unsafe qBittorrent file path: {exc}"[:200])
        store.event("error", f"sort {h[:12]} refused: {exc}",
                    f"sort {h[:12]} refused: unsafe file path")
        store.save()
        return None
    content_dir = resolved.content_dir
    single_file = resolved.single_file
    selected_files = resolved.selected_files
    if selected_files == set():
        # qB returned a valid file list but the user selected NOTHING:
        # this is an authoritative "no content" result, not a file-list
        # failure.  Do not fall back to a name guess — terminal failed.
        rec.update(sort_status="failed",
                   error="no files are selected for download in qBittorrent")
        store.event("warn",
                    f"sort {h[:12]} skipped: no files selected in qBittorrent",
                    f"sort {h[:12]} skipped: no files selected")
        store.save()
        return None
    if content_dir is None and single_file is None:
        if selected_files is None:
            # no file list (qb offline / old version): guess from the name
            if name and (save / name).is_file():
                # single-file torrent: the video sits directly in the save dir
                single_file = save / name
            else:
                content_dir = save / name if name else save
                if not content_dir.exists() and name:
                    cand = cfg.DOWNLOAD_DIR / name
                    if cand.exists():
                        content_dir = cand
        else:
            # file list was obtained and files are selected, but no
            # content dir or single file could be resolved: treat the
            # selected files as the content.
            if len(selected_files) == 1:
                only = next(iter(selected_files))
                if only.is_file():
                    single_file = only
                elif only.is_dir():
                    content_dir = only
            else:
                # multiple selected files: use the save dir as content
                content_dir = save
    # The name also comes from qB metadata, so the final source still has
    # to be confined: a safe save + a "../outside" name is still unsafe.
    # A content dir that IS DOWNLOAD_DIR itself would make the final
    # sweep delete unrelated downloads, so reject it too.
    try:
        if single_file is not None:
            single_file = require_download_path(single_file, under_save=save,
                                                root=cfg.DOWNLOAD_DIR)
        elif content_dir is not None:
            content_dir = require_download_path(content_dir, under_save=save,
                                                root=cfg.DOWNLOAD_DIR)
            if (content_dir == Path(cfg.DOWNLOAD_DIR).resolve()
                    and cfg.SORT_DELETE_TORRENT):
                raise UnsafeTorrentPath(
                    f"content dir is DOWNLOAD_DIR itself; the source sweep "
                    f"would delete unrelated downloads")
    except UnsafeTorrentPath as exc:
        rec.update(sort_status="failed",
                   error=f"unsafe qBittorrent file path: {exc}"[:200])
        store.event("error", f"sort {h[:12]} refused: {exc}",
                    f"sort {h[:12]} refused: unsafe file path")
        store.save()
        return None
    if single_file is None and (content_dir is None or not content_dir.exists()):
        rec.update(sort_status="failed", error="content directory not found")
        store.event("error", f"sort {h[:12]}: content dir not found ({content_dir})",
                    f"sort {h[:12]}: content not found")
        store.save()
        return None
    # The selected files must contain at least one usable video, otherwise
    # the user deselected the main content (e.g. only a subtitle or NFO
    # remains selected).  This only applies to torrents that are genuinely
    # multi-file (file_count > 1): a single-file torrent whose video lacks
    # a standard extension must still sort.  A multi-file torrent where the
    # user deselects everything but one non-video file is NOT a single-file
    # exception.  Terminal failed: no selected video files.
    if selected_files is not None and resolved.file_count and \
            resolved.file_count > 1:
        selected_videos = [
            p for p in selected_files
            if p.suffix.lower() in sorter.VIDEO_EXTS
        ]
        if not selected_videos:
            rec.update(sort_status="failed",
                       error="no selected video files in qBittorrent")
            store.event("warn",
                        f"sort {h[:12]} skipped: no selected video files",
                        f"sort {h[:12]} skipped: no selected video files")
            store.save()
            return None
        # The selected videos must actually exist on disk: qB lists the
        # files it will download, but a selected video that is missing
        # (e.g. a partial download that was interrupted) must not produce
        # an empty plan that advances into sweep/finalization.
        missing_videos = [p for p in selected_videos if not p.is_file()]
        if missing_videos:
            names = ", ".join(Path(m).name for m in missing_videos[:3])
            rec.update(sort_status="failed",
                       error=f"selected video file is missing: {names}"[:200])
            store.event("warn",
                        f"sort {h[:12]} skipped: selected video missing "
                        f"({names})",
                        f"sort {h[:12]} skipped: selected video missing")
            store.save()
            return None

    rec["sort_status"] = "planning"
    store.save()
    try:
        plan = sorter.plan_content(
            kind, parsed, content_dir or save, cfg.MOVIES_DIR, cfg.TV_DIR,
            single_file=single_file,
            preserve_source=not cfg.SORT_DELETE_TORRENT,
            allowed_files=selected_files,
        )
    except Exception as e:
        # Interrupted BEFORE any move (no plan was saved): the record is
        # retryable and a retry re-plans from scratch.
        rec.update(sort_status="incomplete", error=str(e)[:200],
                   sort_retry_after=time.time() + SORT_RETRY_BACKOFF)
        store.event("warn", f"sort {h[:12]} interrupted while planning: {e} (will retry)",
                    f"sort {h[:12]} interrupted; will retry automatically")
        store.save()
        return None
    if not plan.ok:
        # plan_content() vetoes the plan itself (a movie with no
        # recognizable video, a single-file source that is already gone):
        # executing it would place ZERO files and still end "sorted" +
        # torrent cleanup.  Fail terminally instead — an admin can still
        # force a retry through the manual sort path.
        msg = "; ".join(plan.notes) or "sort plan contains no usable media"
        rec.update(sort_status="failed", error=msg[:200])
        store.event("error", f"sort {h[:12]} failed: {msg}",
                    f"sort {h[:12]} failed")
        store.save()
        return None
    return plan


def _run_sort_plan(qb: QBClient, store: StateStore, h: str, rec: dict,
                   plan: sorter.SortPlan, cfg=None):
    """Execution phase of a persisted plan.

    Runs the remaining ops one at a time, persisting progress after each
    (crash-resumable), sweeps the source dir LAST, and only then marks
    the record "sorted" (+ cleanup + Jellyfin refresh).  A failure at any
    op leaves the record "incomplete" with the plan and the completed-op
    list intact; the next attempt (automatic or manual) verifies what is
    already in place, skips it, and finishes the rest at the SAME planned
    destinations — never re-moving, never re-deduping.
    """
    # One snapshot for the whole execution: every preflight path check,
    # the sweep policy and the post-sort cleanup use the same generation
    # of the layout (ISSUE 5).
    if cfg is None:
        cfg = config.snapshot()
    layout = (cfg.DOWNLOAD_DIR, cfg.MOVIES_DIR, cfg.TV_DIR)
    completed = set(rec.get("sort_completed") or [])
    total = len(plan.ops)

    # Preflight — the execution firewall (ISSUE 11).  A persisted plan
    # is data, not trust: a hand-edited plan, a moved DOWNLOAD_DIR, or a
    # library root edited after the plan was written can point at
    # sources outside the download dir or destinations outside the
    # current library.  Validate every source AND destination against
    # this operation's pinned snapshot before execute_plan() gets a
    # chance to move/copy anything; a violation is terminal "failed"
    # with the plan retained for inspection and manual retry.  This
    # layer only fails closed — it never re-plans (that policy lives in
    # do_sort, the only caller with the full context).
    try:
        _require_plan_destinations(plan, cfg)
        if plan.content_dir is not None:
            require_download_path(plan.content_dir, root=cfg.DOWNLOAD_DIR)
        for op in plan.ops:
            src = op.get("src")
            if not src:
                raise UnsafeTorrentPath("sort plan contains an empty source path")
            require_download_path(src, root=cfg.DOWNLOAD_DIR)
    except UnsafeTorrentPath as exc:
        rec.update(sort_status="failed",
                   error=f"unsafe persisted sort plan: {exc}"[:200])
        store.event("error",
                    f"sort {h[:12]} refused: persisted plan path is unsafe: {exc}",
                    f"sort {h[:12]} refused: unsafe persisted plan")
        store.save()
        return

    def checkpoint(op_index: int, outcome: str) -> None:
        rec["sort_completed"] = sorted(completed)
        # A durability barrier: the next op must not start unless this
        # op's progress is on disk.  A failed write raises
        # StatePersistenceError and stops the sequence immediately.
        store.save(required=True)

    try:
        result = sorter.execute_plan(plan, completed, checkpoint)
    except StatePersistenceError as exc:
        # A checkpoint could not be made durable: STOP.  The plan and the
        # completed-op list are already on disk from the earlier successful
        # writes, so the next attempt resumes from there (completed ops are
        # verified and skipped).  Keep the plan — it is what makes the
        # recovery possible.
        rec.update(sort_status="incomplete",
                   error=f"sort paused because progress could not be "
                         f"persisted: {exc}"[:200],
                   sort_retry_after=time.time() + SORT_RETRY_BACKOFF)
        store.event("warn",
                    f"sort {h[:12]} paused: progress could not be persisted "
                    f"({len(completed)} of {total} file(s) in place); "
                    f"will retry automatically",
                    f"sort {h[:12]} paused; will retry automatically")
        store.save()
        return
    except sorter.DestinationConflictError as exc:
        # A planned destination is occupied by content we cannot prove
        # belongs to this sort: STOP, TERMINAL.  The existing library
        # content was NOT overwritten and the source was NOT swept.
        # Retrying the same collision every few seconds accomplishes
        # nothing and floods the activity log; an administrator inspects,
        # renames or removes the conflicting destination, then retries
        # manually (Sort now uses allow_failed=True).  The plan and the
        # completed-op list are retained so the manual retry resumes the
        # SAME plan at the SAME destinations.
        rec.update(sort_status="failed",
                   error=f"destination conflict: {exc}"[:200])
        rec.pop("sort_retry_after", None)
        store.event("error",
                    f"sort {h[:12]} stopped: {exc}; existing library content "
                    f"was NOT overwritten (torrent NOT deleted)",
                    f"sort {h[:12]} stopped: library destination conflict; "
                    f"administrator action required")
        store.save()
        return
    except Exception as e:
        rec.update(sort_status="incomplete", error=str(e)[:200],
                   sort_retry_after=time.time() + SORT_RETRY_BACKOFF)
        store.event("warn",
                    f"sort {h[:12]} incomplete: {len(completed)} of {total} "
                    f"file(s) in place, failed: {e} (will retry automatically)",
                    f"sort {h[:12]} interrupted after {len(completed)} of "
                    f"{total} file(s); will retry automatically")
        store.save()
        return

    if result["missing"]:
        names = ", ".join(Path(m).name for m in result["missing"][:3])
        more = f" (+{len(result['missing']) - 3} more)" if len(result["missing"]) > 3 else ""
        rec.update(sort_status="incomplete",
                   error=f"{len(result['missing'])} planned file(s) no longer in "
                         f"the source (e.g. {names}{more}); will retry automatically",
                   sort_retry_after=time.time() + SORT_RETRY_BACKOFF)
        store.event("warn",
                    f"sort {h[:12]} incomplete: {names}{more} vanished from the "
                    f"download dir (will retry automatically)",
                    f"sort {h[:12]} incomplete: {len(result['missing'])} file(s) "
                    f"missing from the download dir")
        store.save()
        return

    if result["vanished"]:
        # A file that WAS placed and verified is now gone from the library:
        # the "every planned file is in place" invariant is broken, so the
        # sort must NOT be declared sorted, the source must NOT be swept,
        # and the torrent must NOT be deleted.  The record stays retryable;
        # a later attempt finishes cleanly if the file reappears (or stays
        # incomplete until an admin intervenes).
        names = ", ".join(Path(v).name for v in result["vanished"][:3])
        rec.update(
            sort_status="incomplete",
            error=f"previously placed file(s) missing from library: {names}",
            sort_retry_after=time.time() + SORT_RETRY_BACKOFF,
        )
        store.event(
            "error",
            f"sort {h[:12]} incomplete: previously placed file(s) disappeared: {names}",
            f"sort {h[:12]} incomplete: previously placed file(s) disappeared",
        )
        store.save()
        return

    # Every planned file is now verified in place. The source sweep is the
    # LAST destructive operation — and only after it does the record become
    # "sorted" (which is also the gate for the qBittorrent delete and the
    # Jellyfin refresh).
    if plan.content_dir and not rec.get("sort_swept"):
        try:
            removed = sorter._sweep_dir(Path(plan.content_dir), layout=layout)
        except sorter.UnsafeSweepError as exc:
            # A destructive guard (library overlap, outside DOWNLOAD_DIR,
            # or DOWNLOAD_DIR itself) refused the sweep.  Never finalize
            # and never delete the torrent.  The refusal is deterministic
            # (a path policy, not a transient I/O error), so the record is
            # terminal "failed": the plan is kept, an admin fixes the
            # layout, and a manual retry re-verifies the placed files and
            # re-runs the (idempotent) sweep.
            rec.update(sort_status="failed",
                       error=f"source sweep refused: {exc}"[:200])
            rec.pop("sort_retry_after", None)
            store.event("error",
                        f"sort {h[:12]}: source sweep refused: {exc} "
                        f"(torrent NOT deleted)",
                        f"sort {h[:12]}: source sweep refused")
            store.save()
            return
        except sorter.SweepCleanupError as exc:
            # A safe sweep target could not be completely removed.
            # This is a transient I/O failure (permission denied, NFS error,
            # ENOTEMPTY, etc.), NOT a policy violation.  The files are in
            # the library, but source cleanup failed.  Mark the record
            # "incomplete" with a retry: the sweep is idempotent, so the
            # next attempt will re-verify the placed files and retry the
            # sweep.  Never finalize, never delete the torrent.
            rec.update(sort_status="incomplete",
                       error=f"source cleanup incomplete: {exc}"[:200],
                       sort_retry_after=time.time() + SORT_RETRY_BACKOFF)
            store.event("warn",
                        f"sort {h[:12]}: files are in the library but source "
                        f"cleanup failed: {exc}; will retry automatically "
                        f"(torrent NOT deleted)",
                        f"sort {h[:12]}: source cleanup incomplete; will retry automatically")
            store.save()
            return
        except Exception as exc:
            # A defensive catch for unexpected errors.  Treat as incomplete
            # and retryable: terminal should only mean you positively know
            # the sweep violates policy.  An unexpected failure should never
            # permit finalization, but neither should it permanently poison
            # the record based on uncertainty.
            rec.update(sort_status="incomplete",
                       error=f"source cleanup could not be completed: {exc}"[:200],
                       sort_retry_after=time.time() + SORT_RETRY_BACKOFF)
            store.event("warn",
                        f"sort {h[:12]}: source cleanup could not be completed: "
                        f"{exc}; will retry automatically (torrent NOT deleted)",
                        f"sort {h[:12]}: source cleanup failed; will retry automatically")
            store.save()
            return
        rec["sort_swept"] = True
        try:
            store.save(required=True)
        except StatePersistenceError as exc:
            # The sweep may already have happened, but the record that it
            # did is not durable: do NOT finalize (no "sorted", no qB
            # delete).  The sweep is idempotent, so the retry simply
            # re-runs it (a gone source dir sweeps nothing) and finishes.
            rec.update(sort_status="incomplete",
                       error=f"sort complete but sweep state could not be "
                             f"persisted: {exc}"[:200],
                       sort_retry_after=time.time() + SORT_RETRY_BACKOFF)
            store.event("warn",
                        f"sort {h[:12]}: files in place but sweep state could "
                        f"not be persisted; will finish later",
                        f"sort {h[:12]}: will finish later automatically")
            store.save()
            return
        if removed:
            store.event("info",
                        f"sort {h[:12]}: swept {len(removed)} leftover path(s) "
                        f"from the download dir")

    _finish_sort(qb, store, h, rec, plan, cfg)


def _finish_sort(qb: QBClient, store: StateStore, h: str, rec: dict,
                 plan: sorter.SortPlan, cfg=None) -> None:
    """The plan fully completed: mark sorted, then the SEPARATE cleanup step
    (qBittorrent delete) and the Jellyfin library refresh.  Neither runs
    after a partial sort — the torrent stays in qBittorrent and seeding
    until every planned file is safe in the library.
    """
    # One snapshot for the whole finalize step: the cleanup policy and
    # the refresh policy come from the same generation (ISSUE 5).
    if cfg is None:
        cfg = config.snapshot()
    rec.update(sort_status="sorted", dest=plan.dest_dir,
               sorted_at=time.time(), moved=len(plan.ops))
    rec.pop("error", None)
    rec.pop("sort_retry_after", None)
    if cfg.SORT_DELETE_TORRENT:
        # The files are already safe in the library. Deleting the torrent
        # is a SEPARATE cleanup step, tracked on its own (cleanup_status),
        # so a failed delete is retried on its own later — the filesystem
        # sort is never re-run.
        rec.update(cleanup_status="pending", cleanup_attempts=0)
    else:
        # Nothing to delete: the cleanup step is satisfied by definition.
        rec.update(cleanup_status="complete", cleaned_at=time.time())
    try:
        # Durability barrier: the "sorted" transition (and the pending
        # cleanup) must be on disk BEFORE the destructive qBittorrent
        # delete.  No best-effort save may run before this barrier: a
        # durable "sorted" WITHOUT the pending cleanup would be terminal
        # (sort_terminal) and the delete would never be retried.  Keep the
        # plan/completed fields until this save succeeds — destroying the
        # recovery information first would leave an unrecoverable record
        # if the process died mid-delete.
        store.save(required=True)
    except StatePersistenceError as exc:
        # Files are in place, but the record that they are is not durable:
        # do NOT delete from qB.  Revert to a retryable state, keep the
        # plan, and back off; the retry re-verifies the placed files,
        # re-sweeps (idempotent), and finalizes.
        rec.update(sort_status="incomplete",
                   error=f"sort complete but final state could not be "
                         f"persisted: {exc}"[:200],
                   sort_retry_after=time.time() + SORT_RETRY_BACKOFF)
        store.event("warn",
                    f"sort {h[:12]}: files in place but final state could "
                    f"not be persisted; will finish later",
                    f"sort {h[:12]}: will finish later automatically")
        store.save()
        return
    store.event("info", f"sorted “{plan.title}” → {plan.dest_dir} ({len(plan.ops)} file(s))",
                f"sorted “{plan.title}” → library ({len(plan.ops)} file(s))")
    rec.pop("sort_plan", None)
    rec.pop("sort_completed", None)
    rec.pop("sort_swept", None)
    if cfg.SORT_DELETE_TORRENT:
        _delete_torrent(qb, store, h, rec, cfg)
    if cfg.NOTIFY_JELLYFIN and cfg.JELLYFIN_API_KEY:
        result = jellyfin_refresh(cfg)
        if result.accepted:
            store.event("info",
                        f"Jellyfin library refresh requested after "
                        f"sorting {h[:12]}")
        else:
            store.event("warn",
                        f"Jellyfin refresh failed after sorting "
                        f"{h[:12]}: {result.error}")
    store.save()


def _delete_torrent(qb: QBClient, store: StateStore, h: str, rec: dict,
                    cfg=None) -> bool:
    """Ask qBittorrent to remove a torrent whose files are already sorted.

    Returns True once the cleanup is confirmed (the record becomes
    cleanup_status="complete"); False means the delete must be retried
    later and the record stays "pending". This never touches the media
    files — it is the cleanup half of a sort, not the sort itself.
    """
    r = qb.delete(h, delete_files=False, cfg=cfg)
    if r is not None and r.status_code in (200, 204, 404):
        # 404 = the torrent is already gone from qBittorrent (removed
        # manually, or the client lost it) — the cleanup goal is met
        # either way, which also makes a retry safe after a crash between
        # a successful delete and its persistence.
        rec.update(cleanup_status="complete", cleaned_at=time.time())
        if r.status_code == 404:
            store.event("info",
                        f"torrent {h[:12]} is no longer in qBittorrent; "
                        f"cleanup marked complete")
        else:
            store.event("info",
                        f"removed torrent {h[:12]} from qBittorrent (files already moved)")
        return True
    _cleanup_failed(store, h, rec,
                    "qBittorrent unreachable" if r is None
                    else f"qBittorrent returned HTTP {r.status_code}")
    return False


def retry_cleanup(qb: QBClient, store: StateStore, h: str, rec: dict,
                  cfg=None) -> None:
    """Watcher entry point: retry ONLY the qBittorrent delete for a record
    whose files are already sorted but whose cleanup is still pending (see
    cleanup_pending). The filesystem sort is never re-run.
    """
    _delete_torrent(qb, store, h, rec, cfg)
    store.save()


def _cleanup_failed(store: StateStore, h: str, rec: dict, detail: str) -> None:
    """Count a failed cleanup attempt and warn about it — but at most one
    warning per CLEANUP_WARN_INTERVAL, so a long qBittorrent outage does
    not flood the activity log with duplicate warnings.
    """
    now = time.time()
    rec["cleanup_attempts"] = int(rec.get("cleanup_attempts") or 0) + 1
    if now - float(rec.get("last_cleanup_warn_at") or 0) < CLEANUP_WARN_INTERVAL:
        return
    rec["last_cleanup_warn_at"] = now
    store.event("warn",
                f"cleanup {h[:12]} pending: could not remove torrent from "
                f"qBittorrent ({detail}); files are already in the library, "
                f"the delete will be retried")


def try_sort(qb: QBClient, store: StateStore, t: dict, rec: dict,
             allow_failed: bool = False, cfg=None) -> str:
    """Claim and run one sort, with one owner per torrent hash.

    allow_failed lets an explicit manual retry claim a genuinely failed
    record; the automatic watcher keeps the default (False) so real
    failures stay terminal for it.

    Returns ``sorted``/``failed`` after an attempt, ``busy`` when another
    caller owns it, or ``terminal`` when it was already handled.
    """
    h = t.get("hash") or ""
    lock = store.sort_lock(h)
    if not lock.acquire(blocking=False):
        return "busy"
    try:
        if not store.claim_sort(h, rec, allow_failed):
            return "terminal"
        do_sort(qb, store, t, rec, cfg)
        return rec.get("sort_status") or "failed"
    finally:
        lock.release()


# ---------------------------------------------------------------------------
# background threads
# ---------------------------------------------------------------------------

