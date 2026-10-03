"""Release-name parsing and file sorting for completed torrents."""
from __future__ import annotations

import contextlib
import ctypes
import errno
import hashlib
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import config

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".flv", ".webm", ".wmv", ".mpg", ".m2ts"}
SIDE_EXTS = {".srt", ".ass", ".ssa", ".vtt", ".m4a", ".mka", ".idx", ".sub"}

TECH_TERMS = {
    "1080p", "720p", "480p", "2160p", "1080i", "4k", "8k", "uhd", "hd",
    "web", "webrip", "web-dl", "webdl", "hdtv", "dvd", "bd", "brrip",
    "blu-ray", "bluray", "bdrip", "x264", "x265", "h264", "h265", "hevc",
    "avc", "aac", "ac3", "eac3", "dts", "atmos", "ddp", "dd+", "xvid",
    "divx", "remux", "proper", "repack", "internal", "limited", "multi",
    "subbed", "dubbed", "10bit", "10-bit", "8bit", "hdr", "hdr10", "hdr10+",
    "dolby", "vision", "remastered", "extended", "theatrical", "unrated",
    "remaster", "remastered", "imax", "cinema", "cine", "pal", "ntsc",
    "complete", "season", "s01", "s02", "s03", "s04", "s05", "s06", "s07",
    "s08", "s09", "s10", "s11", "s12", "s13", "s14", "s15", "s16", "s17",
    "s18", "s19", "s20", "ep", "episode", "min", "hmm", "gb", "mb",
}

SEASON_EP = re.compile(r"^s?(\d{1,2})[e.](\d{1,3})$", re.I)
XXYY = re.compile(r"^(\d{1,2})x(\d{2,3})$", re.I)
SEASON_ONLY = re.compile(r"^s(\d{1,2})$", re.I)
SEASON_NAME = re.compile(
    r"(?:season[\s._-]*|(?<![A-Za-z])s[\s._-]?)(\d{1,2})\b", re.I)
EMBEDDED_SEASON_EP = re.compile(
    r"(?<![A-Za-z0-9])s(\d{1,2})e(\d{1,3})(?!\d)", re.I)
EMBEDDED_XXYY = re.compile(r"(?<!\d)(\d{1,2})x(\d{2,3})(?!\d)", re.I)
YEAR = re.compile(r"^(19|20)\d{2}$")


def parse_release(name: str) -> dict:
    """Extract title / year / season-episode from a torrent release name.

    Returns {"title", "year", "season", "episode", "kind"}.
    kind is "tv" when a season/episode marker was found, else "movie".
    """
    name = re.sub(
        r"\.(?:mkv|mp4|avi|m4v|mov|ts|m2ts|flv|webm|wmv|mpg|mp3|flac|aac|ogg|opus)$",
        "", name, flags=re.I)
    tokens = [t for t in re.split(r"[.\-_()\[\]{}!+]+| +", name) if t]
    title_tokens: list[str] = []
    year: Optional[int] = None
    season: Optional[int] = None
    episode: Optional[int] = None

    for tok in tokens:
        tl = tok.lower()
        m = SEASON_EP.match(tl) or XXYY.match(tl)
        if m and len(title_tokens) >= 1:
            season = int(m.group(1))
            episode = int(m.group(2))
            break
        sm = SEASON_ONLY.match(tl)
        if sm and len(title_tokens) >= 1:
            season = int(sm.group(1))
            break
        if YEAR.match(tok) and year is None:
            year = int(tok)
            continue
        if tl in TECH_TERMS:
            break
        title_tokens.append(tok)
        if len(title_tokens) >= 12:
            break

    title = " ".join(title_tokens).strip()
    kind = "tv" if season is not None else "movie"
    return {"title": title, "year": year, "season": season,
            "episode": episode, "kind": kind}


def parse_episode_from_filename(fname: str) -> tuple[Optional[int], Optional[int]]:
    """(season, episode) embedded in a single file name, else (None, None)."""
    name = Path(fname).stem
    m = EMBEDDED_SEASON_EP.search(name)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = EMBEDDED_XXYY.search(name)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None, None


def season_from_name(name: str) -> Optional[int]:
    """Season number implied by a folder name like 'Season 01', 'Season01',
    'S01' or 'the.show.s01'."""
    m = SEASON_NAME.search(name)
    if m:
        return int(m.group(1))
    return None


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip()
    name = re.sub(r"\s+", " ", name)
    return name[:180] or "untitled"


def title_folder(title: str, year: Optional[int]) -> str:
    base = sanitize_filename(title)
    return f"{base} ({year})" if year else base


# ---------------------------------------------------------------------------
# .torrent bdecode (minimal, for reading info.name / info hash)
# ---------------------------------------------------------------------------

def bdecode(blob: bytes, *, max_depth: int = 100, max_items: int = 100_000):
    """Decode bounded bencode.

    Torrent uploads are untrusted.  The limits prevent deeply nested or
    extremely broad metadata from exhausting the Python stack or CPU.
    """
    items = 0

    def _read(pos: int, depth: int = 0):
        nonlocal items
        if depth > max_depth:
            raise ValueError("bencode nesting is too deep")
        if pos >= len(blob):
            raise ValueError("truncated bencode")
        items += 1
        if items > max_items:
            raise ValueError("bencode contains too many values")
        c = blob[pos:pos + 1]
        if c == b"d":
            pos += 1
            d = {}
            while pos < len(blob) and blob[pos:pos + 1] != b"e":
                k, pos = _read(pos, depth + 1)
                if not isinstance(k, bytes):
                    raise ValueError("bencode dictionary key is not a string")
                v, pos = _read(pos, depth + 1)
                d[k] = v
            if pos >= len(blob):
                raise ValueError("unterminated bencode dictionary")
            return d, pos + 1
        if c == b"l":
            pos += 1
            l = []
            while pos < len(blob) and blob[pos:pos + 1] != b"e":
                v, pos = _read(pos, depth + 1)
                l.append(v)
            if pos >= len(blob):
                raise ValueError("unterminated bencode list")
            return l, pos + 1
        if c.isdigit():
            m = re.match(rb"\d+", blob[pos:])
            if m is None or pos + len(m.group(0)) >= len(blob) \
                    or blob[pos + len(m.group(0)):pos + len(m.group(0)) + 1] != b":":
                raise ValueError(f"bad bencode string at {pos}")
            n = int(m.group(0))
            pos += len(m.group(0)) + 1
            if n < 0 or pos + n > len(blob):
                raise ValueError("truncated bencode string")
            return blob[pos:pos + n], pos + n
        if c == b"i":
            m = re.match(rb"i(\-?\d+)e", blob[pos:])
            if m is None:
                raise ValueError(f"bad bencode integer at {pos}")
            return int(m.group(1)), pos + m.end()
        raise ValueError(f"bad bencode at {pos}")

    val, _ = _read(0)
    return val


def _top_level_value_span(blob: bytes, wanted: bytes) -> tuple[int, int]:
    """Return the raw byte span (start, end) of `wanted` in the top-level
    bencoded dict.

    Walks the top-level dictionary structurally so an embedded "4:info"
    inside a string value, file path, or nested dict can never be mistaken
    for the info key. The BitTorrent info hash is defined over the EXACT
    raw info-dict bytes, so we record the span rather than re-bencoding.
    """
    if not blob or blob[:1] != b"d":
        raise ValueError("torrent metadata must be a dictionary")
    pos = 1
    found = None
    while True:
        if pos >= len(blob):
            raise ValueError("truncated top-level dictionary")
        if blob[pos:pos + 1] == b"e":
            pos += 1
            break
        key, pos = _bdecode_at(blob, pos)
        if not isinstance(key, bytes):
            raise ValueError("bencode dictionary key is not a byte string")
        value_start = pos
        _, value_end = _bdecode_at(blob, pos)
        pos = value_end
        if key == wanted:
            if found is not None:
                raise ValueError("duplicate info dictionary")
            found = (value_start, value_end)
    if pos != len(blob):
        raise ValueError("trailing data after torrent metadata")
    if found is None:
        raise ValueError("torrent metadata has no info dictionary")
    return found


def torrent_info(data: bytes) -> dict:
    """Return the name and info hashes from a raw .torrent blob.

    Returns {"name", "hash", "infohash_v1", "infohash_v2", "version"}.
    Both digests are computed from the SAME raw (canonical) info-dict
    bytes: v1 = SHA-1, v2 = SHA-256 (BEP 52). `hash` is the
    qBittorrent TorrentID: the v1 hash for v1-only torrents, and the
    first 40 hex chars of the v2 hash for v2/hybrid torrents (qB and
    libtorrent use the truncated v2 as the torrent key whenever a v2
    hash exists). A digest is only reported when its protocol is actually
    present in the info dict, so a pure-v2 torrent never gets a
    (meaningless) SHA-1.
    """
    meta = bdecode(data)
    if not isinstance(meta, dict):
        raise ValueError("torrent metadata must be a dictionary")
    info = meta.get(b"info")
    if not isinstance(info, dict):
        raise ValueError("torrent info must be a dictionary")

    def s(x):
        return x.decode("utf-8", "replace") if isinstance(x, bytes) else str(x)

    name = s(info.get(b"name") or info.get("name") or meta.get(b"name") or "unknown")
    v1 = ""
    v2 = ""
    version = ""
    start, end = _top_level_value_span(data, b"info")
    raw_info = data[start:end]
    if raw_info:
        # v1 structures: pieces + (length or files). v2 structures:
        # "meta version" == 2 + "file tree".
        has_v1 = (b"pieces" in info and (b"length" in info or b"files" in info))
        # BEP 52: meta version is the integer 2 and file tree is a nested
        # dict; bdecode returns Python int for i2e and dict for d...e
        has_v2 = (info.get(b"meta version") == 2
                  and isinstance(info.get(b"file tree"), dict))
        if has_v1:
            v1 = hashlib.sha1(raw_info).hexdigest()
        if has_v2:
            v2 = hashlib.sha256(raw_info).hexdigest()
        if has_v1 and has_v2:
            version = "hybrid"
        elif has_v2:
            version = "v2"
        elif has_v1:
            version = "v1"
    h = v2[:40] if v2 else v1
    return {"name": name, "hash": h,
            "infohash_v1": v1, "infohash_v2": v2, "version": version}


def _bdecode_at(blob: bytes, pos: int, depth: int = 0):
    """Parse a bencoded value starting at pos; return (value, end_pos)."""
    if depth > 100 or pos >= len(blob):
        raise ValueError("invalid or excessively nested bencode")
    c = blob[pos:pos + 1]
    if c == b"d":
        pos += 1
        d = {}
        while blob[pos:pos + 1] != b"e":
            k, pos = _bdecode_at(blob, pos, depth + 1)
            v, pos = _bdecode_at(blob, pos, depth + 1)
            d[k] = v
        return d, pos + 1
    if c == b"l":
        pos += 1
        l = []
        while blob[pos:pos + 1] != b"e":
            v, pos = _bdecode_at(blob, pos, depth + 1)
            l.append(v)
        return l, pos + 1
    if c == b"i":
        m = re.match(rb"i(\-?\d+)e", blob[pos:])
        return int(m.group(1)), pos + m.end()
    if c.isdigit():
        m = re.match(rb"\d+", blob[pos:])
        n = int(m.group(0))
        pos += len(m.group(0)) + 1
        return blob[pos:pos + n], pos + n
    raise ValueError(f"bad bencode at {pos}")


# ---------------------------------------------------------------------------
# sorting
# ---------------------------------------------------------------------------

@dataclass
class SortResult:
    kind: str
    title: str
    dest_dir: str
    moved: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    ok: bool = True


def _unique_dest(dest: Path) -> Path:
    """If dest exists, append ' (n)' until free (unless identical file)."""
    if not dest.exists():
        return dest
    if dest.is_file():
        p = dest
        n = 1
        while p.exists() and p.is_file():
            p = dest.with_name(f"{dest.stem} ({n}){dest.suffix}")
            n += 1
        return p
    return dest  # directory: merge


def _file_exists(p: Path) -> bool:
    """True only for a real file; I/O errors count as "absent"."""
    try:
        return Path(p).is_file()
    except OSError:
        return False


def _file_allowed(p: Path, allowed_files: set[Path] | None) -> bool:
    """True when p is in the selected-file allowlist (or no allowlist)."""
    return allowed_files is None or p.resolve() in allowed_files


def _video_files(root: Path, allowed_files: set[Path] | None = None) -> list[Path]:
    files = []
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS \
                and _file_allowed(p, allowed_files):
            files.append(p)
    return files


# Subfolder names (case-insensitive) that commonly hold a video's external
# subtitles/audio next to the video file itself.
SUBTITLE_SUBDIRS = ("subs", "subtitles")


def _is_sidecar(p: Path, video_stem: str) -> bool:
    """True if p is a sidecar belonging to a video whose stem is video_stem.

    A sidecar matches when its extension is a sidecar extension and its stem
    is either exactly the video stem or starts with the video stem followed
    by a dot.  The dot boundary keeps e.g. "Episode2.srt" from matching
    "Episode.mkv", while language / hearing-impaired tags such as
    "Episode.en.srt", "Episode.forced.srt", "Episode.eng.sdh.ass" all match.
    """
    if not p.is_file():
        return False
    if p.suffix.lower() not in SIDE_EXTS:
        return False
    return p.stem == video_stem or p.stem.startswith(video_stem + ".")


def _sidecars_for(video: Path, allowed_files: set[Path] | None = None) -> list[Path]:
    """Sidecar files for a video.

    Looks in the video's own directory and inside any common subtitle
    subfolder (Subs/, Subtitles/) sitting next to it, so tagged subtitles
    (language / forced / SDH) and subtitles kept in a subfolder are treated
    as content and are not left behind to be swept.  When an allowlist is
    given, only sidecars in it are returned (deselected sidecars are left
    in place and swept as junk).
    """
    out: list[Path] = []
    video_stem = video.stem
    try:
        entries = list(video.parent.iterdir())
    except OSError:
        return out
    for p in entries:
        if p.is_file():
            if _is_sidecar(p, video_stem) and _file_allowed(p, allowed_files):
                out.append(p)
        elif p.name.lower() in SUBTITLE_SUBDIRS:
            try:
                sub_entries = list(p.iterdir())
            except OSError:
                continue
            for q in sub_entries:
                if _is_sidecar(q, video_stem) and _file_allowed(q, allowed_files):
                    out.append(q)
    return out


def _place(src: Path, dest_dir: Path, preserve_source: bool = False,
           name: Optional[str] = None) -> Path:
    """Move a file, or hardlink/copy it when qB must keep seeding it.

    name overrides the destination file name (used to keep a sidecar
    attached when its video had to be dedupe-renamed).
    """
    if name is None:
        name = src.name
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = _unique_dest(dest_dir / name)
    if not preserve_source:
        shutil.move(str(src), str(dest))
        return dest
    try:
        os.link(src, dest)
    except OSError:
        shutil.copy2(src, dest)
    return dest


def _place_video(video: Path, dest_dir: Path, preserve_source: bool = False) -> list[Path]:
    """Place a video plus its sidecars into dest_dir.

    Uses the normal dedupe rules; if the video was renamed because the
    name was taken, each sidecar is renamed to the new stem so it stays
    attached to its video.
    """
    placed = [_place(video, dest_dir, preserve_source)]
    video_stem = video.stem
    for sc in _sidecars_for(video):
        name = sc.name
        if placed[0].name != video.name:
            # Video was dedupe-renamed: re-root the sidecar at the new stem
            # while keeping whatever tag follows the original stem (e.g.
            # ".en", ".eng.forced", ".sdh") so the language/track survives.
            tag = sc.stem[len(video_stem):]
            name = f"{placed[0].stem}{tag}{sc.suffix}"
        placed.append(_place(sc, dest_dir, preserve_source, name=name))
    return placed


def _merge_into(src: Path, dest: Path, preserve_source: bool = False) -> list[Path]:
    """Merge src's contents into the existing dest directory, file-by-file.

    Videos (with their sidecars) use the normal dedupe rules; any other
    file is placed with the same rules.  Returns the destination paths
    in placement order.  The source tree is left untouched by this
    function — the caller removes it once everything is placed.
    """
    placed: list[Path] = []
    seen: set[Path] = set()
    for v in _video_files(src):
        placed.extend(_place_video(v, dest, preserve_source))
        seen.update(_sidecars_for(v))
        seen.add(v)
    for f in sorted(src.rglob("*")):
        if f.is_file() and f not in seen and f.suffix.lower() not in VIDEO_EXTS:
            placed.append(_place(f, dest, preserve_source))
    return placed


def _place_tree(src: Path, dest: Path, preserve_source: bool = False) -> list[Path]:
    """Place src's tree at dest.

    Fresh dest: the whole tree is moved (or hardlinked/copied) at once.
    Existing dest directory: the contents are merged file-by-file into
    it (dedupe rules apply) and, in move mode, the source tree is swept
    only after every file has been placed successfully.
    Returns the list of placed destination paths.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.is_file():
        dest = _unique_dest(dest)
    if dest.exists():
        placed = _merge_into(src, dest, preserve_source)
        if not preserve_source:
            _sweep_dir(src)
        return placed
    if not preserve_source:
        shutil.move(str(src), str(dest))
        return [dest]

    def link_or_copy(source, target):
        try:
            os.link(source, target)
        except OSError:
            shutil.copy2(source, target)
        return target

    shutil.copytree(src, dest, copy_function=link_or_copy)
    return [dest]


class UnsafeSweepError(RuntimeError):
    """The requested sweep target violates the configured path safety policy."""
    pass


class SweepCleanupError(RuntimeError):
    """A safe sweep target could not be completely removed."""

    def __init__(self, path: Path, cause: Exception):
        self.path = Path(path)
        self.cause = cause
        super().__init__(f"could not remove {self.path}: {cause}")


def _assert_safe_sweep(source: Path, layout: tuple | None = None) -> None:
    """Refuse to sweep a source tree that is not inside DOWNLOAD_DIR or
    that overlaps a configured library.

    Defense in depth: the planner already confines sources to the managed
    download dir, but a persisted plan survives across restarts, configs
    can change between planning and execution, and a future regression in
    the planner must not be able to delete arbitrary trees.  No source
    cleanup may ever touch a path outside DOWNLOAD_DIR, DOWNLOAD_DIR
    itself, MOVIES_DIR, or TV_DIR.

    ``layout`` pins the check to the (DOWNLOAD_DIR, MOVIES_DIR, TV_DIR)
    triple from the executing operation's config snapshot, so the sweep
    is validated against the SAME generation of the layout that planned
    the move (ISSUE 5).  When omitted, one fresh snapshot is taken.
    """
    if layout is None:
        cfg = config.snapshot()
        layout = (cfg.DOWNLOAD_DIR, cfg.MOVIES_DIR, cfg.TV_DIR)
    download_dir, movies_dir, tv_dir = layout
    src = Path(source).resolve()
    root = Path(download_dir).resolve()
    if src == root:
        raise UnsafeSweepError(
            f"refusing unsafe cleanup of {src}: is DOWNLOAD_DIR itself")
    if root not in src.parents:
        raise UnsafeSweepError(
            f"refusing unsafe cleanup of {src}: outside DOWNLOAD_DIR {root}")
    for library in (movies_dir, tv_dir):
        lib = Path(library).resolve()
        if src == lib or lib in src.parents or src in lib.parents:
            raise UnsafeSweepError(
                f"refusing unsafe cleanup of {src}: overlaps library {lib}")


def _sweep_dir(root: Path, layout: tuple | None = None) -> list[str]:
    """Delete everything left under root (files, then dirs), then root itself.

    Called only AFTER a successful sort, on the torrent's own content
    folder: whatever remains is non-content junk (posters, samples, .nfo)
    that would otherwise keep the download dir non-empty.
    Refuses to run when the root overlaps a configured library (see
    _assert_safe_sweep).  Returns the list of removed paths (including the
    root dir).
    
    Raises:
        UnsafeSweepError: if the root violates the path safety policy.
        SweepCleanupError: if a safe root could not be completely removed.

    ``layout`` pins the safety check to the (DOWNLOAD_DIR, MOVIES_DIR,
    TV_DIR) triple from the executing operation's config snapshot (ISSUE
    5); when omitted, one fresh snapshot is taken.
    """
    _assert_safe_sweep(root, layout=layout)
    root = Path(root)
    removed: list[str] = []

    # Use lstat() to distinguish "missing" from "inaccessible".
    # Path.exists() returns False for any OSError on Python 3.14+,
    # which would incorrectly treat an inaccessible path as "already gone".
    try:
        root.lstat()
    except FileNotFoundError:
        return removed
    except OSError as exc:
        raise SweepCleanupError(root, exc) from exc

    try:
        paths = sorted(
            root.rglob("*"),
            key=lambda p: len(p.parts),
            reverse=True,
        )
    except FileNotFoundError as exc:
        # If another actor removed the whole root, cleanup is already done.
        try:
            root.lstat()
        except FileNotFoundError:
            return removed
        except OSError as stat_exc:
            raise SweepCleanupError(root, stat_exc) from stat_exc
        # Root itself still exists, so this was not an "already gone root".
        raise SweepCleanupError(root, exc) from exc
    except OSError as exc:
        raise SweepCleanupError(root, exc) from exc

    for q in paths:
        try:
            if q.is_symlink() or not q.is_dir():
                q.unlink()
            else:
                q.rmdir()
            removed.append(str(q))

        except FileNotFoundError:
            # Harmless race: another process already removed it.
            continue

        except OSError as exc:
            raise SweepCleanupError(q, exc) from exc

    try:
        root.rmdir()
        removed.append(str(root))
    except FileNotFoundError:
        # Root disappeared after its children were removed.
        pass
    except OSError as exc:
        raise SweepCleanupError(root, exc) from exc

    return removed


# ---------------------------------------------------------------------------
# sort plans: build once, persist, execute op-by-op (crash-resumable)
#
# A SortPlan fixes EVERY destination (including dedupe names) before any
# filesystem modification happens.  The gateway persists the plan and the
# list of completed op indices in the sort state record, so an interrupted
# sort (disk full, permission error, FS disconnect, restart) can be resumed:
# completed ops are verified and skipped, the rest run against the SAME
# planned paths — no duplicates, no "(1)" re-renames, no second moves.
# ---------------------------------------------------------------------------

PLAN_VERSION = 2


@dataclass
class SortPlan:
    """A complete, pre-computed sort: every file move, no side effects.

    ``ops`` are plain dicts (JSON-friendly for the state record):
    ``{"src": str, "dst": str, "source_size": int, "source_sha256": str}``
    — PLAN_VERSION 2 persists the source's size and sha256 with every op
    (captured at plan time), because that fingerprint is the only proof
    that a destination found after a crash holds the planned content.
    ``content_dir`` is the source folder swept as the LAST destructive
    step, after every op has been verified in place; it is None when
    there is nothing to sweep (preserve_source or single-file content
    sitting in the shared save dir).
    """
    kind: str
    title: str
    dest_dir: str
    content_dir: Optional[str]
    preserve_source: bool
    ops: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    ok: bool = True

    def to_dict(self) -> dict:
        return {
            "version": PLAN_VERSION,
            "kind": self.kind,
            "title": self.title,
            "dest_dir": self.dest_dir,
            "content_dir": self.content_dir,
            "preserve_source": self.preserve_source,
            "ops": self.ops,
            "notes": self.notes,
            "ok": self.ok,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SortPlan":
        return cls(
            kind=d.get("kind", ""),
            title=d.get("title", "Unknown"),
            dest_dir=d.get("dest_dir", ""),
            content_dir=d.get("content_dir"),
            preserve_source=bool(d.get("preserve_source", False)),
            ops=list(d.get("ops") or []),
            notes=list(d.get("notes") or []),
            ok=bool(d.get("ok", True)),
        )


class SortPlanError(Exception):
    """I/O failure while executing one op of a persisted plan."""

    def __init__(self, op: dict, cause: Exception):
        self.op = op
        self.cause = cause
        super().__init__(
            f"could not place {Path(op['src']).name} -> {op['dst']}: {cause}")


class DestinationConflictError(SortPlanError):
    """A planned destination is occupied by content we cannot prove
    belongs to this op; it must never be overwritten.

    The op's persisted source fingerprint (PLAN_VERSION 2) is the only
    ownership proof: a destination whose bytes do not match it — or
    cannot be verified at all — is treated as foreign library content.
    The sort stops, the record becomes terminal "failed", and an
    administrator resolves the conflict before a manual retry.
    """

    def __init__(self, op: dict, cause: Exception):
        self.op = op
        self.cause = cause
        Exception.__init__(
            self,
            f"destination conflict for {Path(op['src']).name}: planned "
            f"destination already exists with different content: {op['dst']}")


class _Planner:
    """Accumulates plan ops with per-destination-dir name reservations.

    Dedupe names are decided HERE, at plan time: against the files that
    already exist on disk AND against names this same plan has reserved.
    Once a plan exists its destinations never change, so a retry can never
    dedupe-rename a file the plan already placed.
    """

    def __init__(self, preserve_source: bool,
                 allowed_files: Optional[set[Path]] = None):
        self.preserve_source = preserve_source
        self.allowed_files = allowed_files
        self.ops: list = []
        self.notes: list = []
        self._reserved: dict = {}

    def _file_taken(self, dest_dir: Path, name: str) -> bool:
        if name in self._reserved.get(dest_dir, ()):
            return True
        return _file_exists(dest_dir / name)

    def _name_for(self, dest_dir: Path, name: str) -> str:
        """Unique file name in dest_dir (disk + this plan's reservations)."""
        if not self._file_taken(dest_dir, name):
            return name
        stem, suffix = Path(name).stem, Path(name).suffix
        n = 1
        while True:
            cand = f"{stem} ({n}){suffix}"
            if not self._file_taken(dest_dir, cand):
                return cand
            n += 1

    def _dir_for(self, parent: Path, name: str) -> Path:
        """Directory path under parent that won't clobber an existing file."""
        cand = parent / name
        if not cand.exists():
            return cand
        if cand.is_file():
            stem, suffix = Path(name).stem, Path(name).suffix
            n = 1
            while True:
                p = parent / f"{stem} ({n}){suffix}"
                if not p.exists() or not p.is_file():
                    return p
                n += 1
        return cand  # directory: merge

    def add_file(self, src: Path, dest_dir: Path, name: Optional[str] = None) -> Path:
        src, dest_dir = Path(src), Path(dest_dir)
        if name is None:
            name = src.name
        name = self._name_for(dest_dir, name)
        dst = dest_dir / name
        # PLAN_VERSION 2: capture the source's identity NOW, before any
        # filesystem mutation, so a crash between the move and the
        # checkpoint can be proven (or refused) by the destination's
        # bytes later.  An unreadable source fails the PLAN (the content
        # cannot be sorted as-is); it never becomes a silent unknown.
        size, digest = _source_fingerprint(src)
        self.ops.append({"src": str(src), "dst": str(dst),
                         "source_size": size, "source_sha256": digest})
        self._reserved.setdefault(dest_dir, set()).add(name)
        return dst

    def add_video(self, video: Path, dest_dir: Path) -> Path:
        """Plan a video plus its sidecars; sidecars follow a dedupe rename."""
        placed = self.add_file(video, dest_dir)
        video_stem = video.stem
        for sc in _sidecars_for(video, self.allowed_files):
            name = sc.name
            if placed.name != video.name:
                # Video was dedupe-renamed: re-root the sidecar at the new
                # stem, keeping the language/track tag (".en", ".sdh", ...).
                tag = sc.stem[len(video_stem):]
                name = f"{placed.stem}{tag}{sc.suffix}"
            self.add_file(sc, dest_dir, name=name)
        return placed

    def add_tree(self, src: Path, dest: Path) -> None:
        """Plan every file under src, preserving relative paths below dest.

        In selective mode (allowed_files set) only files in the allowlist
        are planned; a whole-tree add is never used for a folder that
        contains deselected files.
        """
        src, dest = Path(src), Path(dest)
        for f in sorted(src.rglob("*")):
            if f.is_file() and _file_allowed(f, self.allowed_files):
                rel = f.relative_to(src)
                self.add_file(f, dest / rel.parent, name=rel.name)


def _plan_merge_into(planner: _Planner, src: Path, dest: Path) -> None:
    """Plan a file-by-file merge of src into an existing dest directory."""
    seen: set = set()
    for v in _video_files(src, planner.allowed_files):
        planner.add_video(v, dest)
        seen.update(_sidecars_for(v, planner.allowed_files))
        seen.add(v)
    for f in sorted(src.rglob("*")):
        if f.is_file() and f not in seen and f.suffix.lower() not in VIDEO_EXTS \
                and _file_allowed(f, planner.allowed_files):
            planner.add_file(f, dest)


def plan_content(kind: str, parsed: dict, content_dir: Path,
                 movies_root: Path, tv_root: Path,
                 single_file: Optional[Path] = None,
                 preserve_source: bool = False,
                 allowed_files: Optional[set[Path]] = None) -> SortPlan:
    """Build the complete sort plan WITHOUT touching the filesystem.

    Same destination rules as the legacy one-shot sort: movie = the big
    video(s) + sidecars into Movies/<Title (Year)>; TV = whole season
    folders fresh or merged, per-episode scatter otherwise, unrecognizable
    content stashed.  Every destination — including any " (n)" dedupe
    suffix — is fixed here so retries are idempotent by construction.

    When allowed_files is given (the qB selected-file set), only files in
    that set are planned; deselected files are left in place and swept as
    junk.  A whole-tree add_tree is never used in selective mode because
    it would reintroduce deselected files.
    """
    content_dir = Path(content_dir)
    title = parsed.get("title") or content_dir.name or "Unknown"
    planner = _Planner(preserve_source, allowed_files=allowed_files)
    plan = SortPlan(kind=kind, title=title, dest_dir="", content_dir=None,
                    preserve_source=preserve_source)
    plan.ops = planner.ops
    plan.notes = planner.notes

    if single_file is not None:
        v = Path(single_file)
        if not _file_exists(v):
            plan.ok = False
            plan.notes.append(f"single file missing: {v}")
            return plan
        if kind == "movie":
            dest_dir = Path(movies_root) / title_folder(title, parsed.get("year"))
            planner.add_video(v, dest_dir)
            plan.dest_dir = str(dest_dir)
        else:
            show_dir = Path(tv_root) / title_folder(title, parsed.get("year"))
            s, e = parse_episode_from_filename(v.name)
            if s is None:
                s = season_from_name(v.name) or parsed.get("season")
            dest_dir = show_dir / f"Season {s:02d}" if s is not None else show_dir
            planner.add_video(v, dest_dir)
            plan.dest_dir = str(show_dir)
        # the file sits in the shared save dir: never sweep that
        return plan

    # If the torrent was one folder, descend into it.
    entries = list(content_dir.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        content_dir_for_scan = entries[0]
    else:
        content_dir_for_scan = content_dir

    if kind == "movie":
        dest_dir = Path(movies_root) / title_folder(title, parsed.get("year"))
        videos = _video_files(content_dir_for_scan, allowed_files)
        if not videos:
            plan.ok = False
            plan.notes.append("no video files found")
            return plan
        biggest = max(videos, key=lambda p: p.stat().st_size)
        to_move = [v for v in videos if v.stat().st_size >= 0.5 * biggest.stat().st_size]
        for v in to_move:
            planner.add_video(v, dest_dir)
        plan.dest_dir = str(dest_dir)
        plan.content_dir = str(content_dir) if not preserve_source else None
        return plan

    # ---- TV ----
    show_dir = Path(tv_root) / title_folder(title, parsed.get("year"))
    # whole-folder season move?
    folder = content_dir_for_scan
    folder_season = (season_from_name(folder.name)
                     if folder.name != content_dir.name else None)
    if len(entries) == 1 and entries[0].is_dir():
        sample = list(folder.rglob("*"))[:5]
        eps_in_folder = [p for p in sample if p.is_file()
                         and p.suffix.lower() in VIDEO_EXTS
                         and _file_allowed(p, allowed_files)
                         and parse_episode_from_filename(p.name)[0] is not None]
        if folder_season is not None and (not eps_in_folder or all(
                parse_episode_from_filename(p.name)[0] == folder_season
                for p in eps_in_folder)):
            dest_dir = planner._dir_for(show_dir, f"Season {folder_season:02d}")
            if dest_dir.exists():
                _plan_merge_into(planner, folder, dest_dir)
            elif allowed_files is None:
                # whole-tree move is only safe when nothing was
                # deselected: an allowlist means some files must be
                # left behind, so plan the selected files individually
                planner.add_tree(folder, dest_dir)
            else:
                # selective mode: plan each selected video (with its
                # sidecars) individually, never the whole folder.  The
                # sidecars are handled by add_video, so no separate
                # non-video pass is needed (it would double-plan them).
                for v in _video_files(folder, allowed_files):
                    planner.add_video(v, dest_dir)
            # In selective mode a zero-op plan is not a valid "sorted"
            # result: qB listed selected files but none of them are
            # usable video, so the sort must fail, not advance into
            # sweep/finalization with an empty plan.
            if allowed_files is not None and not planner.ops:
                plan.ok = False
                plan.notes.append("no selected media files found")
                return plan
            plan.dest_dir = str(dest_dir)
            plan.content_dir = str(content_dir) if not preserve_source else None
            return plan

    # per-episode scatter
    base_season = parsed.get("season")
    moved_any = False
    for p in sorted(content_dir_for_scan.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in VIDEO_EXTS:
            continue
        if not _file_allowed(p, allowed_files):
            continue
        s, e = parse_episode_from_filename(p.name)
        if s is None and base_season is not None:
            s = base_season
        if s is not None:
            dest_dir = show_dir / f"Season {s:02d}"
        else:
            dest_dir = show_dir  # unsorted: land at show root
        planner.add_video(p, dest_dir)
        moved_any = True
    if moved_any:
        plan.dest_dir = str(show_dir)
        plan.content_dir = str(content_dir) if not preserve_source else None
        return plan

    # no recognizable video: stash the whole content folder under the show
    dest = planner._dir_for(show_dir, sanitize_filename(folder.name))
    if content_dir_for_scan != content_dir:
        planner.add_tree(content_dir_for_scan, dest)
    else:
        for e in sorted(content_dir.iterdir(), key=lambda x: x.name.lower()):
            if e.is_dir():
                planner.add_tree(e, planner._dir_for(dest, e.name))
            else:
                if _file_allowed(e, allowed_files):
                    planner.add_file(e, dest)
    # In selective mode a zero-op plan is not a valid result: qB listed
    # selected files but none of them could be planned, so the sort must
    # fail, not advance into sweep/finalization with an empty plan.
    if allowed_files is not None and not planner.ops:
        plan.ok = False
        plan.notes.append("no selected media files found")
        return plan
    plan.dest_dir = str(dest)
    plan.content_dir = str(content_dir) if not preserve_source else None
    plan.notes.append("no video files recognized; moved content as-is")
    return plan


# ---------------------------------------------------------------------------
# plan execution: verify, skip, or move — one op at a time
# ---------------------------------------------------------------------------

def _source_fingerprint(path: Path) -> tuple[int, str]:
    """(size, sha256-hex) of a file, streaming in 1 MiB chunks.

    Captured by the planner at plan time (PLAN_VERSION 2) and persisted
    with every op: it is the op's ownership proof.  After a crash in the
    "moved-but-not-checkpointed" window the source is gone, so the
    destination may be trusted only if it matches these exact bytes.
    Raises OSError when the file cannot be read (plan time: the plan
    cannot be built; execution time: the check fails closed).
    """
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        while chunk := fh.read(1024 * 1024):
            size += len(chunk)
            h.update(chunk)
    return size, h.hexdigest()


def _op_fingerprint_ok(op: dict, path: Path) -> bool:
    """True iff `path` holds exactly the bytes the plan was built from.

    The op's persisted source_size/source_sha256 are the only proof that
    a file is "the planned content": a missing or malformed fingerprint,
    a size mismatch, or any I/O error while hashing answers False (fail
    closed) — an op may only ever trust the exact bytes it was planned
    against.
    """
    size = op.get("source_size")
    digest = op.get("source_sha256")
    if (isinstance(size, bool) or not isinstance(size, int) or size < 0
            or not isinstance(digest, str) or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)):
        return False
    try:
        if path.stat().st_size != size:
            return False  # a different size is a conflict without hashing
        return _source_fingerprint(path)[1] == digest
    except OSError:
        return False


def _source_file_state(path: Path) -> str:
    """Classify the op's source path for crash-recovery decisions.

    "regular"  — a regular file
    "missing"  — a genuine FileNotFoundError: the path is absent
    "error"    — the state cannot be determined (I/O, permission, stale
                 network-filesystem handle, ...): must fail closed,
                 never be read as "absent"
    "other"    — exists but is not a regular file
    """
    try:
        st = path.stat()  # follows symlinks, matching old is_file() behavior
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "error"
    return "regular" if stat.S_ISREG(st.st_mode) else "other"


def op_status(op: dict) -> str:
    """Where does this op currently stand on disk?

    ``op`` is the persisted plan op (PLAN_VERSION 2): ``src``/``dst``
    plus the ``source_size``/``source_sha256`` fingerprint captured at
    plan time.

    "in-place"  — the destination is already the content (op done)
    "pending"   — the destination is free AND the source still holds
                  the exact planned bytes; the op still needs to run
    "conflict"  — the destination is occupied by content the fingerprint
                  cannot prove belongs to this op, the source changed
                  since planning (stale source), or the source state is
                  uncertain; nothing may be overwritten
    "missing"   — neither side exists: the source vanished without the
                  op ever completing (data loss; the plan cannot finish)

    Both sides are probed with stat()/lstat(): a broken symlink, a
    directory, or any other non-regular file occupying the destination
    is a conflict, even though Path.exists()/is_file() would report it
    as absent.  A GONE source with a regular destination is "in-place"
    ONLY when the destination matches the persisted fingerprint: the
    plan has no other ownership marker, and "some file is there" is not
    proof it is ours (ISSUE 13).  Uncertainty on EITHER side (an I/O
    error while checking, a changed source, a missing fingerprint) is a
    conflict: it must never authorize replacing existing content or
    trusting an unverified destination.
    """
    src, dst = Path(op["src"]), Path(op["dst"])
    src_state = _source_file_state(src)
    try:
        dst_stat = dst.lstat()
    except FileNotFoundError:
        dst_stat = None
    except OSError:
        # The destination state cannot be determined: fail closed.
        return "conflict"

    if dst_stat is None:
        if src_state == "regular":
            # The op will move the source; only the EXACT bytes the plan
            # was built from may go into the library.  A source that was
            # replaced or edited since planning is a stale-source
            # conflict, not a pending op.
            return "pending" if _op_fingerprint_ok(op, src) else "conflict"
        if src_state == "missing":
            return "missing"
        # error or other: the source state is uncertain, fail closed.
        return "conflict"
    if not stat.S_ISREG(dst_stat.st_mode):
        # symlink (even broken), directory, FIFO, socket, device: the
        # planned destination must be a regular file this op placed.
        return "conflict"

    if src_state == "missing":
        # The crash-recovery window: the move happened, the checkpoint
        # did not.  The source is gone, so the persisted fingerprint is
        # the ONLY ownership proof left — the destination must match it
        # byte-for-byte, or the occupant is foreign library content.
        return "in-place" if _op_fingerprint_ok(op, dst) else "conflict"
    if src_state != "regular":
        # error or other: the source state is uncertain, so the
        # destination cannot be trusted as "already the content".
        return "conflict"

    # BOTH exist: normally an interrupted cross-filesystem move/copy.
    # samefile() cheaply recognizes hardlinks / the same inode.
    try:
        if os.path.samefile(src, dst):
            return "in-place"
    except OSError:
        return "conflict"

    # Different inodes: the destination must hold the exact bytes the
    # plan was built from (size first, then the full hash), and the
    # source must still be those same bytes.  A "stale partial copy"
    # with different content is foreign, not ours — after a crash,
    # another application or person can legitimately occupy this
    # pathname, and the fingerprint is the only ownership marker.
    if not _op_fingerprint_ok(op, dst):
        return "conflict"
    if not _op_fingerprint_ok(op, src):
        return "conflict"
    return "in-place"


# ---------------------------------------------------------------------------
# atomic no-clobber placement (TOCTOU between the op_status() check and
# the write: another process may create the pathname in that window)
# ---------------------------------------------------------------------------

AT_FDCWD = -100          # linux: "use the current working directory"
RENAME_NOREPLACE = 1     # renameat2(2): fail with EEXIST instead of replacing


def _load_renameat2():
    """glibc renameat2(2) via ctypes, or None when unavailable.

    renameat2(RENAME_NOREPLACE) is the only same-filesystem rename that
    REFUSES to replace an existing pathname: os.rename()/shutil.move()
    replace silently, which is how a pathname created after our lexists()
    check would lose its content.
    """
    try:
        import ctypes.util
        name = ctypes.util.find_library("c") or "libc.so.6"
        libc = ctypes.CDLL(name, use_errno=True)
        fn = libc.renameat2
        fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                       ctypes.c_char_p, ctypes.c_uint]
        fn.restype = ctypes.c_int
        return fn
    except Exception:
        return None


_RENAMEAT2 = _load_renameat2()


def _rename_noreplace(src: Path, dst: Path) -> None:
    """Atomic same-filesystem rename that never replaces dst.

    Raises FileExistsError when dst was created after our check (the
    caller turns it into DestinationConflictError), OSError(EXDEV) when
    the filesystems differ, OSError(ENOSYS) when renameat2 is missing.
    """
    if _RENAMEAT2 is None:
        raise OSError(errno.ENOSYS, "renameat2 unavailable")
    rc = _RENAMEAT2(AT_FDCWD, os.fsencode(src), AT_FDCWD,
                    os.fsencode(dst), RENAME_NOREPLACE)
    if rc != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))


def _same_device(src: Path, dst: Path) -> bool | None:
    """True/False when both devices are stat-able; None when unknown
    (an I/O error must not disable the atomic-rename path)."""
    try:
        return src.stat().st_dev == dst.parent.stat().st_dev
    except OSError:
        return None


def _publish_cross_device(op: dict, src: Path, dst: Path,
                          preserve_source: bool) -> None:
    """Cross-filesystem place without ever clobbering dst.

    The content is written to a PRIVATE mkstemp name in the destination
    directory (nothing to overwrite), then os.link() publishes that temp
    file onto the planned pathname: the link is atomic and REFUSES an
    occupied pathname, so a file created in the check/write window wins
    and the op fails as a conflict instead of replacing it.  The source
    is consumed only after the publish succeeded (move); the temp file
    is removed on every path.
    """
    fd, tmp_name = tempfile.mkstemp(prefix=".jg-sort-", dir=str(dst.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as out:
            with open(src, "rb") as fin:
                shutil.copyfileobj(fin, out)
        shutil.copystat(src, tmp)
        try:
            os.link(tmp, dst)
        except FileExistsError as exc:
            raise DestinationConflictError(
                op, RuntimeError(f"destination already exists: {dst}")) from exc
        if not preserve_source:
            os.unlink(src)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    with contextlib.suppress(OSError):
        tmp.unlink()


def perform_op(op: dict, preserve_source: bool) -> Path:
    """Execute one planned move (move, or hardlink/copy when preserving).

    Never overwrites an occupied destination, even when the pathname
    appears AFTER the op_status()/lexists() check (TOCTOU): every
    placement is an atomic no-clobber publish (renameat2(RENAME_NOREPLACE)
    same-filesystem, os.link() otherwise), and a race is a conflict,
    not an overwrite.
    """
    src, dst = Path(op["src"]), Path(op["dst"])
    dst.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(dst):
        # A directory, a file, or a symlink squatting on the planned
        # path: re-placing would nest into it, replace it, or follow it.
        raise DestinationConflictError(
            op, RuntimeError(f"destination already exists: {dst}"))
    if not preserve_source:
        same_dev = _same_device(src, dst)
        if same_dev is not False:
            # Same filesystem (or undecidable): prefer the atomic
            # no-clobber rename.
            try:
                _rename_noreplace(src, dst)
                return dst
            except FileExistsError as exc:
                raise DestinationConflictError(
                    op, RuntimeError(f"destination already exists: {dst}")) from exc
            except OSError as exc:
                if exc.errno != errno.EXDEV:
                    # renameat2 unusable on this platform: os.link() is
                    # still an atomic no-clobber publish; consume the
                    # source only after the link wins.
                    try:
                        os.link(src, dst)
                    except FileExistsError as e2:
                        raise DestinationConflictError(
                            op, RuntimeError(
                                f"destination already exists: {dst}")) from e2
                    except OSError:
                        pass  # link unusable too: cross-device publish
                    else:
                        os.unlink(src)
                        return dst
        _publish_cross_device(op, src, dst, preserve_source=False)
        return dst
    try:
        os.link(src, dst)
    except FileExistsError as exc:
        # The pathname appeared after the check above: it is
        # occupied, not a hardlink failure.
        raise DestinationConflictError(
            op, RuntimeError(f"destination already exists: {dst}")) from exc
    except OSError:
        if os.path.lexists(dst):
            raise DestinationConflictError(
                op, RuntimeError(f"destination already exists: {dst}"))
        # hardlink unavailable (cross-filesystem): private copy +
        # atomic link publish.
        _publish_cross_device(op, src, dst, preserve_source=True)
    return dst


def execute_plan(plan: SortPlan, completed: set, checkpoint=None) -> dict:
    """Run the remaining ops of a persisted plan, one at a time.

    Every op is verified against the filesystem before it is trusted:
    an op whose destination is already in place is SKIPPED — never
    re-moved, never dedupe-renamed.  ``completed`` (a set of op indices)
    is extended in place and ``checkpoint(i, outcome)`` is called after
    each resolved op so the caller can persist progress (crash-resumable).

    Returns {"moved", "skipped", "vanished", "missing"}:
      * "missing"  — ops whose source is gone and never completed (the
        plan cannot fully complete; keep the record resumable, retrying
        picks up the file if it reappears)
      * "vanished" — ops that WERE completed earlier but whose destination
        has since been removed from the library (the plan can no longer be
        trusted as complete: the caller must stop and keep the record
        retryable, never finish/sweep/delete)
    Raises SortPlanError on the first I/O failure of a pending op, and
    DestinationConflictError when a planned destination is occupied by
    content that cannot be proven to belong to this op (the caller must
    stop TERMINALLY — retrying the same collision is pointless).
    """
    moved = skipped = 0
    vanished: list = []
    missing: list = []
    for i, op in enumerate(plan.ops):
        status = op_status(op)
        if status == "conflict":
            # The destination holds content we cannot prove belongs to
            # this op (or the source no longer matches the plan): it is
            # not added to completed and no checkpoint records it — the
            # plan stays where it stopped, the source and the library
            # entry are both untouched.
            raise DestinationConflictError(
                op,
                RuntimeError(
                    f"destination cannot be proven to hold the planned "
                    f"content: {op['dst']}"))
        elif status == "pending":
            try:
                perform_op(op, plan.preserve_source)
            except DestinationConflictError:
                raise
            except SortPlanError:
                raise
            except OSError as e:
                raise SortPlanError(op, e) from e
            outcome = "moved"
            moved += 1
        elif status == "in-place":
            outcome = "skipped"
            skipped += 1
        else:  # missing
            outcome = "vanished" if i in completed else "missing"
            (vanished if outcome == "vanished" else missing).append(op["dst"])
        if outcome != "missing":
            completed.add(i)
        if checkpoint is not None:
            checkpoint(i, outcome)
    return {"moved": moved, "skipped": skipped,
            "vanished": vanished, "missing": missing}


def sort_content(kind: str, parsed: dict, content_dir: Path,
                 movies_root: Path, tv_root: Path,
                 single_file: Optional[Path] = None,
                 preserve_source: bool = False) -> SortResult:
    """One-shot convenience: build the plan and execute it immediately.

    content_dir is the directory that contains the torrent's content
    (a single folder for multi-file torrents, or the files directly).
    single_file is set for single-file torrents: the video itself, sitting
    directly in the (shared) save dir; that dir is NOT scanned.
    preserve_source hardlinks (or copies) instead of moving so qBittorrent
    can continue seeding from its original paths.

    The gateway's production path instead persists the plan between
    attempts (see gateway.do_sort) so an interrupted sort is resumable;
    this wrapper keeps the historical call shape for single-go callers.
    """
    content_dir = Path(content_dir)
    title = parsed.get("title") or content_dir.name or "Unknown"
    plan = plan_content(kind, parsed, content_dir, movies_root, tv_root,
                        single_file=single_file, preserve_source=preserve_source)
    res = SortResult(kind=kind, title=title, dest_dir=plan.dest_dir, ok=plan.ok)
    if not plan.ok:
        res.notes.extend(plan.notes)
        return res
    completed: set = set()
    result = execute_plan(plan, completed)
    if result["missing"]:
        # The source is gone without the op having completed: stop before
        # the sweep — deleting leftover download paths while a planned file
        # is lost would turn a recoverable problem into a lost one.
        res.ok = False
        res.notes.append(
            f"{len(result['missing'])} planned file(s) vanished from the source")
        return res
    if result["vanished"]:
        res.notes.append(
            f"{len(result['vanished'])} previously placed file(s) "
            "vanished from the library")
    if not preserve_source and plan.content_dir:
        removed = _sweep_dir(Path(plan.content_dir))
        if removed:
            res.notes.append(f"swept {len(removed)} leftover path(s) from download dir")
    res.moved = [op["dst"] for op in plan.ops]
    for note in plan.notes:
        if note not in res.notes:
            res.notes.append(note)
    return res

