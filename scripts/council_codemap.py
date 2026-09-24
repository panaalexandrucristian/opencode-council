#!/usr/bin/env python3
"""Standalone code map for a council run (stdlib only; no model calls; no network).

Usage:
  council_codemap.py ingest --run-dir RUN --capture-json FILE
  council_codemap.py ingest --run-dir RUN --task T --step S --mode stage --member ID --generation N --post-json PATH [--launch-id LID]
  council_codemap.py ingest --run-dir RUN --task T --step S --mode publish
  council_codemap.py validate --run-dir RUN --task T --step S --mode begin [--raw]
  council_codemap.py validate --run-dir RUN --task T --step S --mode end
  council_codemap.py validate --run-dir RUN --task T --step S --mode resume
  council_codemap.py lookup --run-dir RUN [--task T --step S | --snapshot ID]   (JSON request on stdin)
  council_codemap.py locator --run-dir RUN --task T --step S

Exit codes: 0 successful processing (structured source/record failures, including a "stale"
guard outcome, are reported inside the result JSON, not as a failing exit code); 1 operational
failure (storage broken/unreadable); 2 invalid CLI arguments or invalid lookup request.

Storage under RUN/codemap/: index.json (current published index; schema_version 1) with
index-snapshots/<sha256>.json + index-head.json (the only thing a lost index.json is ever recovered
from), snapshots/<sha256>.json (immutable validation records), sources/<sha256>.json (immutable,
content-addressed, labelled-base64 full file bytes, one per distinct captured version) with
sources/<sha256>.missing.json tombstones for objects recorded as lost, versions/<sha256>/ (which
root+path+resolved_path actually HELD those bytes — what a historical claim must bind against),
events/ and attempts/<id>/ (a private staging and replay-archive area — this is the map's own
storage, not a generic object store). The run state's own config.dir resolved to its real directory
is the sole permitted source root; a --dir argument is only ever an assertion that must match it
exactly, never a substitute for it. Absolute input paths, path traversal, embedded NULs and
resolved symlink escapes are rejected on the caller's ORIGINAL spelling, before normalisation;
symlinks that resolve to a target inside the root are permitted. Each capture performs two complete
reads, each fenced by before/after descriptor-identity and resolved-path checks and hashed in its
own right: changes are detected by full content, never by size or mtime.

The freshness/consensus guard is round-level and conservative: at "validate --mode begin" the set
of every source version exposed as current in the frozen view is recorded (root, display path,
resolved path, full-file digest). "validate --mode end" (before publication) and "validate --mode
resume" (before reusing checkpointed posts) re-capture and compare those exact sources. A confirmed
whole-file change, deletion, or symlink retarget/escape blocks the decision: the attempt is marked
"stale" (preserved, never reused) and the caller (council.sh) checkpoints via the ordinary exit 2
path and reruns the same round from a fresh attempt, without consuming the round budget, rotating
the proposer, or rewriting archived vote text. If the guard itself cannot be evaluated (storage
broken, a capture unreadable/unstable), the attempt is "pending_verification": votes are preserved,
not discarded, and are not called stale merely because the map failed.

Round 1 of every task (including later tasks in the same run) is served a raw-only immutable
projection: source identities, exact byte ranges, hashes, evidence and freshness only — every
reader identity, inspection label, author conclusion, claim-attached symbol and unbound author
report is removed, and symbol lookup returns an explicit "unavailable_in_this_view" diagnostic.
Later rounds see the full index as published by completed earlier steps only: a step's newly
accepted claims are staged privately and are never exposed to a member still owed a response in
that same step, and are only merged into the shared index after every post in the step is accepted
and the freshness guard passes.
"""
import argparse
import base64
import binascii
import errno
import hashlib
import json
import os
import stat
import sys
import time
from pathlib import Path

SCHEMA_VERSION = 1
PAGE_SIZE = 32


# --------------------------------------------------------------------- canon ----
def canonical_json(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False, allow_nan=False).encode("utf-8")


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


# ------------------------------------------------------------------ atomic io ----
def atomic_write(path, data):
    """temp file in the destination dir, flush+fsync, then atomic rename; fsync the dir too."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / (".tmp-{}-{}".format(os.getpid(), path.name))
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp), str(path))
    except BaseException:
        try:
            os.unlink(str(tmp))
        except OSError:
            pass
        raise
    try:
        dfd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass  # not supported on every platform/filesystem; best-effort


def clean_stale_tmp(directory):
    """Remove abandoned temp files, only within the helper's own namespace."""
    directory = Path(directory)
    if not directory.is_dir():
        return
    for p in directory.iterdir():
        if p.name.startswith(".tmp-") and p.is_file():
            try:
                p.unlink()
            except OSError:
                pass


def write_content_addressed(path, data):
    """Content-addressed file: never overwritten with different content; verify on reuse."""
    path = Path(path)
    if path.exists():
        existing = path.read_bytes()
        if existing != data:
            raise CodemapError("content-addressed object changed identity: {}".format(path))
        return
    atomic_write(path, data)


# -------------------------------------------------------------------- errors ----
class CodemapError(Exception):
    """Operational failure (storage broken/unreadable) -> exit 1."""


class RequestError(Exception):
    """Invalid CLI/request input -> exit 2."""


class CaptureError(Exception):
    """A failed capture still did real work. bytes_read/bytes_hashed carry the bytes this attempt
    genuinely read from the descriptor and genuinely fed to sha256 before it failed, so a rejected
    read is never accounted for as zero work — the counters describe effort, not success."""
    def __init__(self, reason, message, bytes_read=0, bytes_hashed=0):
        super().__init__(message)
        self.reason = reason
        self.bytes_read = bytes_read
        self.bytes_hashed = bytes_hashed


# --------------------------------------------------------------- path safety ----
def run_state_root(run_dir):
    """The run's own config.dir, realpath'd — the single authoritative root. Returns None only
    when the run directory has no state.json at all (a bare store used by the test suite)."""
    p = Path(run_dir) / "state.json"
    if not p.exists():
        return None
    try:
        st = json.loads(p.read_bytes())
    except (OSError, ValueError) as exc:
        raise CodemapError("cannot read run state {}: {}".format(p, exc))
    d = (st.get("config") or {}).get("dir")
    if not isinstance(d, str) or not d:
        return None
    return os.path.realpath(d)


def resolve_root(run_dir, dir_arg):
    """The root is the run state's own config.dir, realpath'd — full stop. There is no fallback to
    an orchestrator-supplied --dir: a run whose state does not name a usable config.dir has no
    authoritative root, and accepting an arbitrary directory would let the caller redefine what the
    map is about. --dir may still be passed, but only as an assertion that must match exactly."""
    state_root = run_state_root(run_dir)
    if state_root is None:
        raise RequestError("the run state at {}/state.json does not name a usable config.dir — the "
                           "source root comes from the run's own state only; an arbitrary --dir is "
                           "never accepted as a substitute".format(run_dir))
    if dir_arg is not None and os.path.realpath(dir_arg) != state_root:
        raise RequestError("--dir resolves to {} but this run's config.dir is {} — the root is "
                           "taken from the run state, never from an arbitrary --dir".format(
                               os.path.realpath(dir_arg), state_root))
    return state_root


def _no_assumptions_relpath(rel_path):
    """Validate the ORIGINAL spelling the caller supplied, before any normalisation: normalising
    first would silently turn "/b" into "b" and "a//../b" into something the caller never wrote.
    An embedded NUL is rejected here too, so it becomes an attributed per-item diagnostic instead
    of a ValueError from os.path that aborts a whole batch of independent reports."""
    if not isinstance(rel_path, str) or not rel_path:
        raise CaptureError("invalid_path", "path must be a non-empty string: {!r}".format(rel_path))
    if "\x00" in rel_path:
        raise CaptureError("invalid_path", "path contains an embedded NUL byte: {!r}".format(rel_path))
    if rel_path.startswith("/") or (os.name == "nt" and os.path.splitdrive(rel_path)[0]):
        raise CaptureError("absolute_path", "absolute input paths are not permitted: {}".format(rel_path))
    parts = rel_path.split("/")
    if any(p == ".." for p in parts):
        raise CaptureError("traversal", "path traversal outside the root is not permitted: {}".format(rel_path))
    if not [p for p in parts if p not in ("", ".")]:
        raise CaptureError("invalid_path", "empty path: {!r}".format(rel_path))
    return parts


def validated_display_path(rel_path):
    """The single entry point from untrusted input to a canonical display path: validate the
    original spelling first, then canonicalise "./a.py" and "a.py" to the same identity key."""
    _no_assumptions_relpath(rel_path)
    return normalize_display_path(rel_path)


def safe_resolve(root, rel_path):
    """Resolve rel_path under root; reject traversal/absolute/escape. Symlinks inside root are OK."""
    _no_assumptions_relpath(rel_path)
    target = os.path.join(root, rel_path)
    resolved = os.path.realpath(target)
    root_with_sep = root if root.endswith(os.sep) else root + os.sep
    if resolved != root and not resolved.startswith(root_with_sep):
        raise CaptureError("escape", "resolved path escapes the root: {}".format(rel_path))
    return resolved


def normalize_display_path(rel_path):
    """Canonical spelling of an already-validated (non-traversal, non-absolute) relative path, so
    "a.py" and "./a.py" produce the same identity/location key instead of silently fragmenting
    the index. Called only through validated_display_path / after _no_assumptions_relpath."""
    parts = [p for p in rel_path.split("/") if p not in ("", ".")]
    return "/".join(parts)


def _open_norace(root, rel_path):
    """Root-anchored fd opening: walk directory components with O_NOFOLLOW from a root fd,
    then open the final resolved target. Raises CaptureError on any non-regular/escaped node.
    O_NONBLOCK on the final open prevents an indefinite hang if the target is a FIFO with no
    writer (capture_once's immediate S_ISREG check then rejects it as not_regular)."""
    resolved = safe_resolve(root, rel_path)
    rel_from_root = os.path.relpath(resolved, root)
    if rel_from_root == os.curdir:
        raise CaptureError("not_regular", "path resolves to the root itself: {}".format(rel_path))
    parts = rel_from_root.split(os.sep)
    dir_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        for comp in parts[:-1]:
            try:
                next_fd = os.open(comp, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW, dir_fd=dir_fd)
            except OSError as exc:
                # Only a DEMONSTRATED identity/path change may invalidate votes. A missing parent
                # means the target is gone (a real change); a parent that became a symlink or a
                # non-directory is a real retarget. But EACCES/EPERM/EIO say "we could not look",
                # not "it changed": classifying those as a confirmed change would let a chmod on a
                # parent directory silently discard a whole round's votes.
                if exc.errno == errno.ENOENT:
                    raise CaptureError("missing", "no such directory component: {}".format(rel_path))
                if exc.errno == errno.ELOOP:
                    raise CaptureError("retargeted", "a parent component became a symlink: {}".format(rel_path))
                if exc.errno == errno.ENOTDIR:
                    raise CaptureError("not_regular", "a parent component is no longer a directory: {}".format(rel_path))
                raise CaptureError("unreadable", "cannot traverse {}: {}".format(rel_path, exc))
            os.close(dir_fd)
            dir_fd = next_fd
        try:
            # O_NOFOLLOW on the FINAL component too: parts come from the already-resolved real
            # path, so a symlink there means the tree was retargeted between resolution and open.
            # O_NONBLOCK keeps a FIFO from blocking the open (capture_once then rejects it).
            file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=dir_fd)
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                raise CaptureError("missing", "no such file: {}".format(rel_path))
            if exc.errno in (errno.ELOOP, getattr(errno, "EMLINK", errno.ELOOP)):
                raise CaptureError("retargeted", "final component became a symlink during capture: {}".format(rel_path))
            raise CaptureError("unreadable", "cannot open {}: {}".format(rel_path, exc))
        return file_fd, resolved
    finally:
        os.close(dir_fd)


class OneRead:
    """One complete read and everything that must hold around it for the bytes to mean anything."""
    def __init__(self, data, sha256, st_before, st_after, resolved):
        self.data = data
        self.sha256 = sha256
        self.st_before = st_before
        self.st_after = st_after
        self.resolved = resolved

    def identity(self):
        return (self.st_before.st_dev, self.st_before.st_ino)


def capture_once(root, rel_path):
    """A single complete read, fenced by descriptor-identity and resolved-path checks on BOTH
    sides, and hashed in its own right.

      fstat(fd) -> S_ISREG -> read to EOF -> fstat(fd) again -> re-resolve the path -> sha256

    The after-read fstat is what catches a file replaced *during* the read (same fd, new inode
    behind the name, or a size/mtime that moved under us); the re-resolve catches a symlink swapped
    mid-read. Each read gets its own hash, so bounded_capture's stability comparison is a
    digest-vs-digest comparison and the hashing work reported is the work actually done.

    A raw OSError anywhere in the operation (a transient I/O error mid-read, a failure re-resolving
    the path, a failure closing the descriptor after the read) becomes a retry-worthy CaptureError
    so one bad file cannot abort a batch of otherwise-independent captures — and so no raw OSError
    ever leaves the operation carrying no accounting at all.

    Whatever the verdict, the work done is reported: bytes are counted as each chunk comes off the
    descriptor (so a partial read that then fails still counts what it read), and a read that
    reached EOF is hashed BEFORE the stability checks run, so a completed-then-rejected read
    reports its hashing too. The counters belong to this operation, not to the individual raise
    sites: every failure leaves through one boundary that attaches them, so a rejection raised
    deep inside a helper that knows nothing about this attempt (safe_resolve's "escape", for
    instance) reports the same real work as a locally constructed one. Nothing that did not
    happen is ever counted — a read aborted by an I/O error is never hashed and never reports
    hashed bytes."""
    counters = {"read": 0, "hashed": 0}
    try:
        return _capture_once_inner(root, rel_path, counters)
    except CaptureError as exc:
        # The single accounting boundary: assign (never add) this operation's own totals, so a
        # failure from any post-read path — identity, mutation, re-resolution, escape — carries
        # exactly the bytes this attempt really read and really hashed, counted once.
        exc.bytes_read = counters["read"]
        exc.bytes_hashed = counters["hashed"]
        raise
    except OSError as exc:
        # An operational filesystem failure ANYWHERE in the operation — including one raised by
        # descriptor cleanup (os.close) after the read finished, which no inner handler can see
        # because it happens in the `finally` that ends them. It is "we could not complete the
        # read", never a demonstrated change, so it takes the existing retry-worthy `unreadable`
        # semantics and the same single assignment of this operation's real counters. Hashing that
        # never ran is not counted: counters["hashed"] is still 0 unless the hash actually happened.
        raise CaptureError("unreadable", "I/O error capturing {}: {}".format(rel_path, exc),
                           bytes_read=counters["read"], bytes_hashed=counters["hashed"])


def _capture_once_inner(root, rel_path, counters):
    """capture_once's body. It updates `counters` as the work actually happens and never has to
    attach them to an exception itself — capture_once does that for every failure path."""
    fd, resolved = _open_norace(root, rel_path)
    try:
        try:
            st_before = os.fstat(fd)
            if not stat.S_ISREG(st_before.st_mode):
                raise CaptureError("not_regular", "not a regular file: {}".format(rel_path))
            chunks = []
            while True:
                chunk = os.read(fd, 1 << 20)
                if not chunk:
                    break
                chunks.append(chunk)
                counters["read"] += len(chunk)
            data = b"".join(chunks)
            st_after = os.fstat(fd)
        except OSError as exc:
            raise CaptureError("unreadable", "I/O error reading {}: {}".format(rel_path, exc))
    finally:
        os.close(fd)
    # The read completed: hash it now, so the hashing is accounted for even if the checks below
    # reject the bytes. The digest is also what makes the rejection reportable at all.
    digest = sha256_hex(data)
    counters["hashed"] = len(data)
    if (st_before.st_dev, st_before.st_ino) != (st_after.st_dev, st_after.st_ino):
        raise CaptureError("retargeted", "file identity changed during the read: {}".format(rel_path))
    if (st_after.st_size, st_after.st_mtime_ns) != (st_before.st_size, st_before.st_mtime_ns) \
            or st_after.st_size != len(data):
        raise CaptureError("mutated_during_read",
                           "the file was modified while it was being read: {}".format(rel_path))
    try:
        # A post-read filesystem failure here is "we could not look again", never a demonstrated
        # change: it goes through the ordinary retry-worthy unreadable semantics, with this
        # operation's counters attached by the boundary above.
        resolved_after = safe_resolve(root, rel_path)
    except OSError as exc:
        raise CaptureError("unreadable", "cannot re-resolve {}: {}".format(rel_path, exc))
    if resolved_after != resolved:
        raise CaptureError("retargeted", "path retargeted during capture: {}".format(rel_path))
    return OneRead(data, digest, st_before, st_after, resolved)


class CaptureResult:
    def __init__(self, status, resolved_path=None, data=None, sha256=None, size=None,
                 bytes_read=0, bytes_hashed=0, reason=None, attempts=0, identity=None):
        self.status = status  # "ok" | "unstable" | "error"
        self.resolved_path = resolved_path
        self.data = data
        self.sha256 = sha256
        self.size = size
        self.bytes_read = bytes_read      # every byte actually read, including repeated/discarded reads
        self.bytes_hashed = bytes_hashed  # only the bytes actually fed to sha256
        self.reason = reason
        self.attempts = attempts
        self.file_identity = identity


# Definite, deterministic outcomes: retrying cannot change the verdict, so they short-circuit
# immediately as "error" rather than being retried into a wishy-washy "unstable" classification.
# "unreadable" (permission/transient I/O) and "retargeted" (symlink swapped mid-attempt) are
# retry-worthy: if they persist across both attempts the source is genuinely "unstable".
IMMEDIATE_REASONS = ("escape", "traversal", "absolute_path", "invalid_path", "not_regular", "missing")


def bounded_capture(root, rel_path):
    """At most two attempts; each attempt performs TWO complete reads, each with its own full-file
    hash and its own before/after identity and resolved-path checks. The version is accepted only
    when both reads agree on digest, inode and resolved path — detection is by content, never by
    size/mtime alone. bytes_read and bytes_hashed count the work genuinely performed, including
    the partial and completed-then-rejected reads of attempts that then failed."""
    bytes_read = 0
    bytes_hashed = 0
    last_reason = None
    used = 0
    for _attempt in (1, 2):
        used = _attempt
        reads = []
        failed = None
        for _which in (1, 2):
            try:
                r = capture_once(root, rel_path)
            except CaptureError as exc:
                # a failed read still did work: count what it actually read and actually hashed
                bytes_read += exc.bytes_read
                bytes_hashed += exc.bytes_hashed
                failed = exc
                break
            # both the read and its hash actually happened; account for both, every time
            bytes_read += len(r.data)
            bytes_hashed += len(r.data)
            reads.append(r)
        if failed is not None:
            if failed.reason in IMMEDIATE_REASONS:
                return CaptureResult("error", reason=failed.reason, bytes_read=bytes_read,
                                     bytes_hashed=bytes_hashed, attempts=used)
            last_reason = failed.reason
            continue
        first, second = reads
        if first.sha256 == second.sha256 and first.resolved == second.resolved \
                and first.identity() == second.identity():
            return CaptureResult("ok", resolved_path=first.resolved, data=first.data,
                                 sha256=first.sha256, size=len(first.data),
                                 bytes_read=bytes_read, bytes_hashed=bytes_hashed, attempts=used,
                                 identity=first.identity())
        last_reason = "inconsistent"
    return CaptureResult("unstable", reason=last_reason or "unstable", bytes_read=bytes_read,
                         bytes_hashed=bytes_hashed, attempts=used)


# --------------------------------------------------------------------- lines ----
def line_spans(data):
    """LF-delimited spans, CRLF and all original bytes retained; a final LF terminates the
    preceding line without a phantom extra line. Empty data has zero spans."""
    spans = []
    start = 0
    for i, b in enumerate(data):
        if b == 0x0A:
            spans.append((start, i + 1))
            start = i + 1
    if start < len(data):
        spans.append((start, len(data)))
    return spans


def is_strict_utf8(data):
    try:
        data.decode("utf-8", errors="strict")
        return True
    except UnicodeDecodeError:
        return False


def is_text_safe(data):
    """Strict UTF-8 AND free of embedded NUL bytes. A NUL is technically valid UTF-8 but is
    fragile downstream (shell ENVIRON, C-string APIs, terminal rendering) — treat any
    NUL-containing source the same as binary: always labelled base64, never line-addressed."""
    return is_strict_utf8(data) and b"\x00" not in data


def line_range_to_bytes(data, first, last):
    """Exactly two non-bool ints, 1 <= first <= last <= line count; no clamping."""
    if isinstance(first, bool) or isinstance(last, bool) or not isinstance(first, int) or not isinstance(last, int):
        raise ValueError("line range must be exactly two integers (booleans excluded)")
    if not is_text_safe(data):
        raise ValueError("line reporting requires a strictly UTF-8-decodable, NUL-free source")
    spans = line_spans(data)
    if not spans:
        raise ValueError("empty file has no valid inclusive line pair")
    if not (1 <= first <= last <= len(spans)):
        raise ValueError("line range out of bounds: [{},{}] of {} lines".format(first, last, len(spans)))
    return spans[first - 1][0], spans[last - 1][1]


def excerpt_encoding(data, start, end):
    """Text (exact JSON string, strict UTF-8, NUL-free) if the whole source and the slice both
    qualify; otherwise labelled base64 (never lossy replacement, never a raw embedded NUL)."""
    if not is_text_safe(data):
        return "base64", base64.b64encode(data[start:end]).decode("ascii")
    piece = data[start:end]
    if not is_text_safe(piece):
        return "base64", base64.b64encode(piece).decode("ascii")
    return "utf8", piece.decode("utf-8")


# -------------------------------------------------------------------- identity ----
def compute_entry_id(root, path, resolved_path, source_sha256, byte_range):
    obj = {"root": root, "path": path, "resolved_path": resolved_path,
           "source_sha256": source_sha256, "byte_range": list(byte_range)}
    return sha256_hex(canonical_json(obj))


def compute_snapshot_id(snapshot_without_id):
    return sha256_hex(canonical_json(snapshot_without_id))


# ---------------------------------------------------------------------- store ----
class Store:
    def __init__(self, run_dir):
        self.run = Path(run_dir)
        self.base = self.run / "codemap"
        self.sources_dir = self.base / "sources"
        self.snapshots_dir = self.base / "snapshots"
        self.attempts_dir = self.base / "attempts"
        self.events_dir = self.base / "events"
        self.versions_dir = self.base / "versions"
        self.index_snapshots_dir = self.base / "index-snapshots"
        self.index_path = self.base / "index.json"
        self.index_head_path = self.base / "index-head.json"

    def ensure_dirs(self):
        for d in (self.base, self.sources_dir, self.snapshots_dir, self.attempts_dir,
                  self.events_dir, self.versions_dir, self.index_snapshots_dir):
            d.mkdir(parents=True, exist_ok=True)
        clean_stale_tmp(self.base)
        clean_stale_tmp(self.sources_dir)
        clean_stale_tmp(self.snapshots_dir)

    # ---- source-version identity: which (root, path, resolved_path) ever HELD these bytes -------
    def version_record_id(self, root, path, resolved_path, sha256):
        return sha256_hex(canonical_json({"root": root, "path": path,
                                          "resolved_path": resolved_path, "sha256": sha256}))

    def record_source_version(self, root, path, resolved_path, sha256, size):
        """A source object proves only that SOME file once held these bytes. A version record
        proves that THIS identity held them, and is what a historical claim must bind against."""
        self.ensure_dirs()
        rid = self.version_record_id(root, path, resolved_path, sha256)
        d = self.versions_dir / sha256
        d.mkdir(parents=True, exist_ok=True)
        rec = {"schema_version": SCHEMA_VERSION, "version_id": rid, "root": root, "path": path,
               "resolved_path": resolved_path, "sha256": sha256, "size": size}
        write_content_addressed(d / (rid + ".json"), canonical_json(rec))
        return rec

    def find_source_versions(self, root, path, sha256):
        """Every recorded version of (root, path) that held exactly these bytes. Independent of
        whether the live path is readable today — a deleted file's history is still history."""
        d = self.versions_dir / sha256
        if not d.is_dir():
            return []
        out = []
        for f in sorted(d.glob("*.json")):
            try:
                rec = json.loads(f.read_bytes())
            except (OSError, ValueError) as exc:
                raise CodemapError("corrupt source-version record {}: {}".format(f, exc))
            if rec.get("root") == root and rec.get("path") == path and rec.get("sha256") == sha256:
                out.append(rec)
        return out

    # ---- tombstones: a digest whose object was lost must never be "restored" from today's bytes -
    def tombstone_path(self, sha256):
        return self.sources_dir / (sha256 + ".missing.json")

    def record_missing_source(self, sha256, reason):
        self.ensure_dirs()
        atomic_write(self.tombstone_path(sha256),
                     canonical_json({"schema_version": SCHEMA_VERSION, "sha256": sha256,
                                     "reason": reason, "recorded_at": int(time.time())}))

    def load_index(self):
        if not self.index_path.exists():
            recovered = self._recover_index()
            if recovered is not None:
                return recovered
            if self._has_prior_activity():
                raise CodemapError("index.json is missing and no verified index snapshot is recorded in "
                                    "index-head.json, but this run has prior code-map activity — treating "
                                    "this as storage corruption rather than silently starting over")
            return empty_index()
        try:
            obj = json.loads(self.index_path.read_bytes())
        except (OSError, ValueError) as exc:
            raise CodemapError("cannot read index.json: {}".format(exc))
        if not isinstance(obj, dict) or obj.get("schema_version") != SCHEMA_VERSION:
            raise CodemapError("index.json is not a schema_version {} index".format(SCHEMA_VERSION))
        for key, default in (("sources", {}), ("entries", {}), ("locations", {}),
                             ("unbound", []), ("diagnostics", []), ("ingested_keys", [])):
            obj.setdefault(key, default() if callable(default) else default)
        return obj

    def _has_prior_activity(self):
        """True if this run's code map has ever done anything, even though index.json itself is
        absent right now — the signal that a missing index.json is data loss, not a fresh run."""
        if (self.base / "seq.json").exists():
            return True
        for d in (self.sources_dir, self.snapshots_dir, self.attempts_dir, self.events_dir):
            if d.is_dir() and any(d.iterdir()):
                return True
        return False

    def _recover_index(self):
        """Recovery from an explicitly recorded, digest-verified index snapshot — and from nothing
        else. Never from posts, never from today's source bytes: an index rebuilt from the current
        filesystem would silently re-date history it cannot actually attest to."""
        if not self.index_head_path.exists():
            return None
        try:
            head = json.loads(self.index_head_path.read_bytes())
        except (OSError, ValueError) as exc:
            raise CodemapError("corrupt index head record {}: {}".format(self.index_head_path, exc))
        snap_id = (head or {}).get("index_snapshot_id")
        if not isinstance(snap_id, str) or len(snap_id) != 64:
            raise CodemapError("index head record names no usable index snapshot id")
        path = self.index_snapshots_dir / (snap_id + ".json")
        if not path.exists():
            raise CodemapError("index.json is missing and the recorded index snapshot {} is absent "
                               "too — authoritative history cannot be recovered".format(snap_id))
        raw = path.read_bytes()
        if sha256_hex(raw) != snap_id:
            raise CodemapError("recorded index snapshot {} does not match its own digest "
                               "(storage corrupted or tampered)".format(snap_id))
        try:
            obj = json.loads(raw)
        except ValueError as exc:
            raise CodemapError("corrupt index snapshot {}: {}".format(snap_id, exc))
        atomic_write(self.index_path, raw)
        return obj

    def publish_index(self, index_obj):
        """Every published index is also written as an immutable, content-addressed snapshot, and
        index-head.json records which one is authoritative — that pair is what makes a lost
        index.json recoverable without inventing history."""
        self.ensure_dirs()
        body = canonical_json(index_obj)
        snap_id = sha256_hex(body)
        write_content_addressed(self.index_snapshots_dir / (snap_id + ".json"), body)
        atomic_write(self.index_path, body)
        atomic_write(self.index_head_path,
                     canonical_json({"schema_version": SCHEMA_VERSION, "index_snapshot_id": snap_id,
                                     "recorded_at": int(time.time())}))
        return snap_id

    def write_source(self, sha256, data):
        """Every full source object is stored as labelled base64 — text and binary alike — so the
        stored bytes are exact and encoding-independent. text_safe records whether the bytes are
        strictly UTF-8 and NUL-free (i.e. line-addressable), which is source metadata, not the
        storage encoding.

        A digest already tombstoned as lost is never written again: re-creating it from today's
        bytes would quietly resurrect an object publication has already reported as missing
        evidence, turning a recorded data loss into a fake intact history."""
        self.ensure_dirs()
        if self.tombstone_path(sha256).exists():
            raise CodemapError("source object {} was recorded as lost; it is never recreated from "
                               "later bytes under its old digest".format(sha256))
        obj = {"schema_version": SCHEMA_VERSION, "sha256": sha256, "size": len(data),
               "encoding": "base64", "text_safe": is_text_safe(data),
               "data": base64.b64encode(data).decode("ascii")}
        write_content_addressed(self.sources_dir / (sha256 + ".json"), canonical_json(obj))

    def read_source(self, sha256):
        path = self.sources_dir / (sha256 + ".json")
        if not path.exists():
            return None
        try:
            obj = json.loads(path.read_bytes())
        except (OSError, ValueError) as exc:
            raise CodemapError("corrupt source object {}: {}".format(sha256, exc))
        if not isinstance(obj, dict) or obj.get("schema_version") != SCHEMA_VERSION \
                or not isinstance(obj.get("size"), int):
            raise CodemapError("source object {} does not match the stored-object schema".format(sha256))
        try:
            if obj.get("encoding") == "utf8":   # objects written by an earlier schema revision
                data = obj["text"].encode("utf-8")
            elif obj.get("encoding") == "base64":
                data = base64.b64decode(obj["data"], validate=True)
            else:
                raise CodemapError("source object {} has an unknown encoding: {}".format(sha256, obj.get("encoding")))
        except (KeyError, TypeError, ValueError, binascii.Error) as exc:
            raise CodemapError("corrupt source object schema {}: {}".format(sha256, exc))
        if sha256_hex(data) != sha256:
            raise CodemapError("source object content does not match its filename digest: {}".format(sha256))
        if obj["size"] != len(data):
            raise CodemapError("source object {} records size {} but holds {} bytes".format(
                sha256, obj["size"], len(data)))
        return data

    def write_snapshot(self, snapshot_without_id):
        self.ensure_dirs()
        snap_id = compute_snapshot_id(snapshot_without_id)
        path = self.snapshots_dir / (snap_id + ".json")
        write_content_addressed(path, canonical_json(snapshot_without_id))
        return snap_id

    def read_snapshot(self, snap_id):
        """Snapshots are addressed by the SHA-256 of their exact canonical bytes; that digest is
        re-verified on every read, so a corrupted or substituted snapshot is an explicit error
        rather than silently authoritative history."""
        path = self.snapshots_dir / (snap_id + ".json")
        if not path.exists():
            return None
        try:
            raw = path.read_bytes()
            obj = json.loads(raw)
        except (OSError, ValueError) as exc:
            raise CodemapError("corrupt snapshot {}: {}".format(snap_id, exc))
        if sha256_hex(raw) != snap_id or compute_snapshot_id(obj) != snap_id:
            raise CodemapError("snapshot {} does not match its own content digest "
                               "(storage corrupted or tampered)".format(snap_id))
        return obj

    def record_event(self, event):
        """Durable, separately addressed validation/capture record. Snapshots are content-addressed
        (two identical validation outcomes are the same snapshot); events are NOT — each carries its
        own monotonic id, so repeated identical validations stay distinct, countable events."""
        seq = self.next_seq()
        ev = dict(event, event_seq=seq, event_id="e{}".format(seq), recorded_at=int(time.time()))
        self.events_dir.mkdir(parents=True, exist_ok=True)
        atomic_write(self.events_dir / "e{:08d}.json".format(seq), canonical_json(ev))
        return ev

    def read_events(self):
        if not self.events_dir.is_dir():
            return []
        out = []
        for f in sorted(self.events_dir.glob("e*.json")):
            try:
                out.append(json.loads(f.read_bytes()))
            except (OSError, ValueError) as exc:
                raise CodemapError("corrupt event record {}: {}".format(f, exc))
        return out

    def attempt_dir(self, attempt_id):
        return self.attempts_dir / attempt_id

    def registry_path(self):
        return self.attempts_dir / "registry.json"

    def load_registry(self):
        p = self.registry_path()
        if not p.exists():
            return {}
        try:
            return json.loads(p.read_bytes())
        except (OSError, ValueError) as exc:
            raise CodemapError("corrupt attempt registry {}: {} (refusing to silently reset it and "
                                "risk reusing an attempt identity)".format(p, exc))

    def save_registry(self, reg):
        self.ensure_dirs()
        atomic_write(self.registry_path(), canonical_json(reg))

    def next_seq(self):
        p = self.base / "seq.json"
        n = 0
        if p.exists():
            try:
                n = json.loads(p.read_bytes()).get("seq", 0)
            except (OSError, ValueError) as exc:
                raise CodemapError("corrupt sequence counter {}: {} (refusing to silently reset it and "
                                    "risk reusing an attempt identity)".format(p, exc))
        n += 1
        self.ensure_dirs()
        atomic_write(p, canonical_json({"seq": n}))
        return n


def empty_index():
    return {"schema_version": SCHEMA_VERSION, "sources": {}, "entries": {}, "locations": {},
            "unbound": [], "diagnostics": [], "latest_snapshot_id": None,
            # every (accepted event, array position) already merged — publication is exactly-once
            "ingested_keys": []}


# --------------------------------------------------------------------- views ----
def raw_projection(index_obj):
    """Round-1 view: identities/ranges/hashes/evidence/freshness only; strip claims."""
    entries = {}
    for eid, e in index_obj["entries"].items():
        entries[eid] = {
            "schema_version": e["schema_version"], "entry_id": e["entry_id"], "root": e["root"],
            "path": e["path"], "resolved_path": e["resolved_path"], "source_sha256": e["source_sha256"],
            "byte_range": e["byte_range"], "excerpt_sha256": e["excerpt_sha256"], "lines": e["lines"],
            "freshness": e["freshness"], "reports": [],
        }
    return {"schema_version": SCHEMA_VERSION, "sources": dict(index_obj["sources"]),
            "entries": entries, "locations": dict(index_obj["locations"]),
            "unbound": [], "diagnostics": [], "raw_only": True}


def full_view(index_obj):
    return {"schema_version": SCHEMA_VERSION, "sources": dict(index_obj["sources"]),
            "entries": {k: dict(v) for k, v in index_obj["entries"].items()},
            "locations": dict(index_obj["locations"]), "unbound": list(index_obj["unbound"]),
            "diagnostics": list(index_obj["diagnostics"]), "raw_only": False}


def recompute_current_at_snapshot(view, locations):
    for e in view["entries"].values():
        loc = locations.get(e["path"])
        current = bool(loc and loc.get("status") == "current"
                       and loc.get("resolved_path") == e["resolved_path"]
                       and loc.get("source_sha256") == e["source_sha256"])
        e["freshness"] = {
            "snapshot_id": (loc or {}).get("snapshot_id"),
            "current_at_snapshot": current,
            "status": "current" if current else ("unverified" if loc is None else "not_current"),
        }


# ----------------------------------------------------------------- attempts ----
def attempt_key(task, step):
    return json.dumps([task, step])


def new_attempt_id(task, step, seq):
    return "{}-{}-a{}".format(task, step, seq)


def load_meta(store, attempt_id):
    p = store.attempt_dir(attempt_id) / "meta.json"
    if not p.exists():
        raise RequestError("no such attempt: {}".format(attempt_id))
    return json.loads(p.read_bytes())


def save_meta(store, attempt_id, meta):
    atomic_write(store.attempt_dir(attempt_id) / "meta.json", canonical_json(meta))


def current_attempt_id(store, task, step):
    reg = store.load_registry()
    aid = reg.get(attempt_key(task, step))
    if not aid:
        raise RequestError("no open attempt for task={} step={} (call validate --mode begin first)".format(task, step))
    return aid


def load_view_verified(store, attempt_id, meta):
    """Load an attempt's frozen view and verify its bytes still match the digest recorded at
    validate --mode begin — so a corrupted/tampered view.json is caught, not silently served."""
    view_path = store.attempt_dir(attempt_id) / "view.json"
    view = json.loads(view_path.read_bytes())
    if sha256_hex(canonical_json(view)) != meta["view_snapshot_id"]:
        raise CodemapError("view.json for attempt {} does not match its recorded snapshot_id "
                            "(storage corrupted or tampered)".format(attempt_id))
    return view


def find_attempt_by_view_snapshot(store, snapshot_id):
    """Scan attempts for the one whose frozen view carries this exact snapshot_id. Each attempt's
    view is a private, immutable per-step artifact — this is the only way a bare --snapshot lookup
    (no --task/--step) can resolve to the correct one instead of silently falling back to the live,
    fully-attributed index (which would defeat round-1's raw-only isolation guarantee)."""
    if not store.attempts_dir.is_dir():
        return None, None
    for d in sorted(store.attempts_dir.iterdir()):
        meta_path = d / "meta.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_bytes())
        except (OSError, ValueError):
            continue
        if meta.get("view_snapshot_id") == snapshot_id:
            return meta.get("attempt_id", d.name), meta
    return None, None


def read_orchestration_pending(run_dir):
    """state.codemap_pending — the orchestrator's own record of the attempt it is owed a decision
    for. It is the authority on identity; the task/step registry is a convenience index."""
    p = Path(run_dir) / "state.json"
    if not p.exists():
        return None
    try:
        st = json.loads(p.read_bytes())
    except (OSError, ValueError) as exc:
        raise CodemapError("cannot read run state {}: {}".format(p, exc))
    pend = st.get("codemap_pending")
    return pend if isinstance(pend, dict) and pend.get("task") and pend.get("step") else None


def check_against_orchestration_state(run_dir, task, step, attempt_id, meta):
    """Never trust the registry alone: if orchestration state says this task/step is owed a
    decision, the attempt, frozen view and guard being operated on must be exactly the ones it
    recorded. A registry entry silently repointed at a fresh attempt (with a fresh, possibly empty
    guard) is precisely how a changed source could slip past the freshness check."""
    pend = read_orchestration_pending(run_dir)
    if pend is None or pend.get("task") != task or pend.get("step") != step:
        return
    for field, recorded in (("attempt", pend.get("attempt")),
                            ("view_snapshot_id", pend.get("view_snapshot_id")),
                            ("guard_snapshot_id", pend.get("guard_snapshot_id"))):
        if not recorded:
            continue
        actual = attempt_id if field == "attempt" else meta.get(field)
        if actual != recorded:
            raise CodemapError(
                "orchestration state records {}={} for task={} step={}, but the attempt being "
                "operated on has {}={} — refusing to verify a different attempt/view/guard than the "
                "one this run is owed a decision for".format(field, recorded, task, step, field, actual))


# ------------------------------------------------------------------- guard ----
def capture_guard_list(root, view):
    """Every source version exposed as current in the frozen view: a superset of anything a
    vote in this attempt could rely on, since active lookup cannot return anything else."""
    seen = {}
    for e in view["entries"].values():
        if e["freshness"].get("current_at_snapshot"):
            seen[e["path"]] = {"root": root, "path": e["path"], "resolved_path": e["resolved_path"],
                                "source_sha256": e["source_sha256"]}
    return list(seen.values())


def refresh_locations(root, index_obj):
    """Re-capture every known display path so 'current' reflects the real filesystem right now,
    not merely what the last publish recorded. This is what makes a freshly frozen guard list an
    honest baseline: current/changed/missing/unreadable/unstable, per distinct source location.
    Returns (stats, results) — results in the same shape as verify_guard's, so this refresh
    can be recorded as its own durable validation event."""
    stats = new_stats()
    results = []
    for path, loc in list(index_obj.get("locations", {}).items()):
        cap = bounded_capture(root, path)
        account(stats, cap)
        if cap.status == "ok":
            status = "current" if (cap.resolved_path == loc.get("resolved_path")
                                    and cap.sha256 == loc.get("source_sha256")) else "changed"
            index_obj["locations"][path] = {"resolved_path": cap.resolved_path, "source_sha256": cap.sha256,
                                             "status": status}
            results.append({"path": path, "status": status, "resolved_path": cap.resolved_path,
                             "source_sha256": cap.sha256})
        elif cap.status == "error" and cap.reason == "missing":
            index_obj["locations"][path] = dict(loc, status="missing")
            results.append({"path": path, "status": "missing"})
        elif cap.status == "error":
            index_obj["locations"][path] = dict(loc, status="changed")
            results.append({"path": path, "status": "changed", "reason": cap.reason})
        else:
            status = "unreadable" if cap.reason != "inconsistent" else "unstable"
            index_obj["locations"][path] = dict(loc, status=status)
            results.append({"path": path, "status": status, "reason": cap.reason})
    return stats, results


def new_stats():
    return {"bytes_read": 0, "bytes_hashed": 0, "captures": 0, "capture_failures": 0,
            "capture_unstable": 0, "read_attempts": 0}


def account(stats, cap):
    """Truthful per-capture accounting: bytes actually read vs bytes actually hashed, and the
    real failure/instability counts — never inferred later from the published index."""
    stats["bytes_read"] += cap.bytes_read
    stats["bytes_hashed"] += cap.bytes_hashed
    stats["captures"] += 1
    stats["read_attempts"] += cap.attempts
    if cap.status == "error":
        stats["capture_failures"] += 1
    elif cap.status == "unstable":
        stats["capture_unstable"] += 1
    return cap


def merge_stats(into, other):
    for k, v in other.items():
        into[k] = into.get(k, 0) + v
    return into


# A guarded source that now resolves outside the root (or through a path component that can no
# longer be traversed) is a CONFIRMED change of the thing the votes relied on — a retargeted
# symlink — not an "we could not check" observation. Classifying it as unreadable would let a
# retarget silently pass the guard as pending_verification.
CONFIRMED_CHANGE_REASONS = ("escape", "traversal", "absolute_path", "invalid_path", "not_regular", "missing")


def verify_guard(root, guard_list):
    """Re-capture each guarded source; compare identity and full content, not the index hash."""
    results = []
    stats = new_stats()
    status = "current"
    for g in guard_list:
        cap = bounded_capture(root, g["path"])
        account(stats, cap)
        if cap.status == "ok":
            if cap.resolved_path == g["resolved_path"] and cap.sha256 == g["source_sha256"]:
                results.append({"path": g["path"], "status": "current", "resolved_path": cap.resolved_path,
                                 "source_sha256": cap.sha256})
            else:
                results.append({"path": g["path"], "status": "changed", "resolved_path": cap.resolved_path,
                                 "source_sha256": cap.sha256})
                status = "changed"
        elif cap.status == "unstable":
            results.append({"path": g["path"], "status": "unstable", "reason": cap.reason})
            if status == "current":
                status = "pending_verification"
        else:
            if cap.reason == "missing":
                results.append({"path": g["path"], "status": "missing"})
                status = "changed"
            elif cap.reason in CONFIRMED_CHANGE_REASONS:
                results.append({"path": g["path"], "status": "changed", "reason": cap.reason})
                status = "changed"
            else:
                results.append({"path": g["path"], "status": "unreadable", "reason": cap.reason})
                if status == "current":
                    status = "pending_verification"
    return status, results, stats


# ------------------------------------------------------------------ ingest -----
CODE_READS_KEYS = {"path", "lines", "symbol", "conclusion", "observed_sha256", "inspection"}


def validate_code_reads_item(item):
    """Return (ok, reason). Malformed keys/items are diagnostics, never a vote/retry trigger."""
    if not isinstance(item, dict):
        return False, "item is not an object"
    if not isinstance(item.get("path"), str) or not item["path"]:
        return False, "missing or invalid path"
    extra = set(item.keys()) - CODE_READS_KEYS
    if extra:
        return False, "unknown keys: {}".format(sorted(extra))
    if "lines" in item:
        # An explicit null is an invalid array, NOT a silently omitted field: a member who writes
        # "lines": null has stated something the schema does not allow, and guessing "they meant a
        # whole-file claim" would invent a claim they never made.
        lines = item["lines"]
        if not (isinstance(lines, list) and len(lines) == 2):
            return False, "lines must be exactly two integers (an explicit null is invalid)"
        if not all(isinstance(x, int) and not isinstance(x, bool) for x in lines):
            return False, "lines must be exactly two integers (booleans excluded)"
    for key in ("symbol", "conclusion", "inspection", "observed_sha256"):
        # For these optional string fields an explicit null carries no claim and is accepted as
        # equivalent to omitting the key; any other non-string type is rejected.
        if key in item and item[key] is not None and not isinstance(item[key], str):
            return False, "{} must be a string".format(key)
    if isinstance(item.get("observed_sha256"), str):
        h = item["observed_sha256"]
        if len(h) != 64 or any(c not in "0123456789abcdef" for c in h.lower()):
            return False, "observed_sha256 must be a 64-character hex sha256"
    return True, None


def launch_record_path(store, attempt_id, launch_id):
    """One immutable record per genuinely new launch. The member's FIRST launch in an attempt is
    stored under its own id (which is the member id), a later one — a replacement, a handover, a
    re-dispatched retry — under its own launch id. A record is never rewritten, so an earlier
    launch can never be relabelled by a later one."""
    return store.attempt_dir(attempt_id) / "launches" / (launch_id + ".json")


def launch_records_for_member(store, attempt_id, member):
    """Every launch record this attempt holds for one member, in file-name order. This is how
    "was this member launched more than once here?" is answered — by the orchestration-owned
    records themselves, never by a naming convention."""
    d = store.attempt_dir(attempt_id) / "launches"
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.json")):
        try:
            rec = json.loads(p.read_bytes())
        except (OSError, ValueError) as exc:
            raise CodemapError("corrupt launch record {}: {}".format(p, exc))
        if not isinstance(rec, dict):
            raise CodemapError("corrupt launch record {}: not an object".format(p))
        if rec.get("member") == member:
            out.append(rec)
    return out


def select_launch_record(store, attempt_id, member, launch_id=None):
    """Resolve the ONE launch that can authenticate a reply, returning (record, reason).

    With an explicit launch_id this reads THAT record and nothing else: a selector naming a launch
    the orchestrator never recorded must never fall back to some other record of the same member,
    because that is exactly how a replacement's reply would be authenticated as the session it
    replaced. reason is "missing" when it is not there.

    Without a selector the member-level record is read — but ONLY while that is genuinely
    unambiguous. If this attempt holds more than one launch record for the member and nothing
    identifies which one produced the reply, no record is returned and reason is "ambiguous": the
    caller's own generation is not evidence, and picking the first (or the member-level) record
    would authenticate a replacement's reply as the session it replaced. The single-launch case —
    every caller that predates launch ids — is unchanged.

    A record whose own scope disagrees with what is being staged — another member, another
    attempt, another launch id — authenticates nothing and is reported as absent."""
    if launch_id is None and len(launch_records_for_member(store, attempt_id, member)) > 1:
        return None, "ambiguous"
    p = launch_record_path(store, attempt_id, launch_id if launch_id is not None else member)
    if not p.exists():
        return None, "missing"
    try:
        rec = json.loads(p.read_bytes())
    except (OSError, ValueError) as exc:
        raise CodemapError("corrupt launch record {}: {}".format(p, exc))
    if not isinstance(rec, dict):
        raise CodemapError("corrupt launch record {}: not an object".format(p))
    if rec.get("member") != member:
        return None, "missing"
    for field, expected in (("attempt", attempt_id),
                            ("launch_id", launch_id if launch_id is not None else None)):
        recorded = rec.get(field)
        if expected is not None and recorded is not None and recorded != expected:
            return None, "missing"
    return rec, None


def read_launch_record(store, attempt_id, member, launch_id=None):
    """select_launch_record's record alone, for callers that only need "may this be attributed?"."""
    return select_launch_record(store, attempt_id, member, launch_id)[0]


def launch_record_scope_ok(rec, task, step):
    """A launch record that names its own task/step must agree with the step being staged."""
    for field, expected in (("task", task), ("step", step)):
        if rec.get(field) is not None and rec.get(field) != expected:
            return False
    return True


def accepted_event_id(attempt_id, member, generation, accepted_sha256, launch_id=None):
    """The full identity of one accepted event. Two authors whose tails happen to be byte-identical
    are still two events; two launches of the same member — even at the same generation — are two
    events; the same launch restaged twice is still one."""
    key = [attempt_id, member, generation, accepted_sha256]
    if launch_id is not None:
        key.append(launch_id)
    return sha256_hex(canonical_json(key))


def staged_payload_path(store, attempt_id, member, event_id):
    return store.attempt_dir(attempt_id) / "staged" / "{}-{}.json".format(member, event_id)


def stage_accepted_post(store, root, task, step, attempt_id, member, generation, post_json_path, seq,
                        launch_id=None):
    post_json_path = Path(post_json_path)
    if not post_json_path.exists():
        raise RequestError("post-json not found: {}".format(post_json_path))
    raw = post_json_path.read_bytes()
    accepted_sha256 = sha256_hex(raw)
    # Attribution is orchestration-owned and recorded BEFORE the member was launched; staging only
    # confirms it, against the record for the launch that actually produced this reply. A handover
    # or replacement that launched the member again inside the same attempt has its own record, so
    # neither the old reply nor the new one is ever relabelled as the other.
    launch, launch_reason = select_launch_record(store, attempt_id, member, launch_id)
    if launch is not None and not launch_record_scope_ok(launch, task, step):
        launch, launch_reason = None, "missing"
    launch_diagnostics = []
    attributed = True
    if launch is None:
        # The caller's own word is not evidence of who produced this reply. Without the
        # orchestration-owned pre-launch record there is nothing to authenticate it against, so the
        # event is preserved in full but stays UNATTRIBUTED: nothing it claims becomes a bound,
        # attributed finding that a later round could rely on.
        attributed = False
        generation = None
        if launch_reason == "ambiguous":
            # The member was launched more than once in this attempt and nothing says which of
            # those launches produced these bytes. Preserved in full, authenticated by nothing.
            launch_diagnostics.append({"code": "ambiguous_launch_records_for_member", "member": member,
                                       "reason": "this attempt holds more than one launch record for "
                                                 "this member and no launch selector identifies which "
                                                 "one produced this reply; the supplied generation is "
                                                 "not accepted as authentication and every report in "
                                                 "this event stays unbound"})
        elif launch_id is not None:
            launch_diagnostics.append({"code": "no_launch_record_for_launch", "member": member,
                                       "reason": "no orchestration-owned record exists for launch {} of "
                                                 "this member in this attempt; no other launch record "
                                                 "may stand in for it, so the supplied generation is not "
                                                 "accepted as authentication and every report in this "
                                                 "event stays unbound".format(launch_id)})
        else:
            launch_diagnostics.append({"code": "no_prelaunch_launch_record", "member": member,
                                       "reason": "no orchestration-owned launch record exists for this "
                                                 "member in this attempt; the supplied generation is not "
                                                 "accepted as authentication and every report in this "
                                                 "event stays unbound"})
    else:
        if launch.get("generation") != generation:
            launch_diagnostics.append({"code": "generation_mismatch_with_launch_record",
                                       "member": member, "reason": "supplied generation {} != recorded "
                                       "launch generation {}".format(generation, launch.get("generation"))})
        # The authenticated generation is the launched one, taken from the orchestration-owned
        # record — never the member's current state and never the member-authored tail.
        generation = launch.get("generation")
    event_id = accepted_event_id(attempt_id, member, generation, accepted_sha256, launch_id)
    payload_path = staged_payload_path(store, attempt_id, member, event_id)

    attribution = {"member": member, "generation": generation, "task": task, "step": step,
                   "attempt": attempt_id, "accepted_sha256": accepted_sha256,
                   "event_id": event_id, "attributed": attributed,
                   "launch_id": (launch or {}).get("launch_id", launch_id if launch else None),
                   "launched_at": (launch or {}).get("launched_at"),
                   "launch_seq": (launch or {}).get("launch_seq")}

    # Acceptance identity and the tail bytes themselves are durable BEFORE any optional report is
    # looked at, so a single malformed code_reads item can never erase the fact that this event
    # was accepted, nor the bytes it was accepted as.
    archive = store.attempt_dir(attempt_id) / "accepted"
    archive.mkdir(parents=True, exist_ok=True)
    write_content_addressed(archive / (accepted_sha256 + ".json"), raw)
    atomic_write(archive / (event_id + ".meta.json"), canonical_json(attribution))
    # The orchestration-owned association from the LIVE post file to the one accepted event it was
    # accepted as. Member plus tail digest is NOT an identity: two launches of the same member can
    # produce byte-identical tails, and then "the first metadata record that matches" is a coin
    # toss that can archive a generation-2 reply as generation 1. This record names the exact
    # event_id and launch, so archival resolves rather than guesses. It is keyed by the live post's
    # own file name and rewritten whenever that file is accepted again, so it always describes the
    # bytes currently on disk; a reused checkpoint post is never re-staged, so its original
    # association is left exactly as it was.
    assoc = {"post": post_json_path.name, "member": member, "task": task, "step": step,
             "attempt": attempt_id, "accepted_sha256": accepted_sha256, "event_id": event_id,
             "generation": generation, "attributed": attributed,
             "launch_id": attribution["launch_id"], "launch_seq": attribution["launch_seq"]}
    assoc_dir = store.attempt_dir(attempt_id) / "post-assoc"
    assoc_dir.mkdir(parents=True, exist_ok=True)
    atomic_write(assoc_dir / (post_json_path.name + ".assoc.json"), canonical_json(assoc))

    if payload_path.exists():
        # This exact accepted event was already staged. Its binding decisions, ranges, conclusions,
        # diagnostics and accounting are frozen as of that moment — re-ingesting it must NOT
        # recapture the file and must NOT let a since-changed source turn an unmatched report into
        # a bound claim. The re-ingestion is recorded as its own (logical, zero-work) event.
        try:
            existing = json.loads(payload_path.read_bytes())
        except (OSError, ValueError) as exc:
            raise CodemapError("corrupt staged payload {}: {}".format(payload_path, exc))
        store.record_event({"kind": "stage_reingest", "attempt": attempt_id, "task": task, "step": step,
                            "member": member, "generation": generation, "event_id": event_id,
                            "accepted_sha256": accepted_sha256, "stats": new_stats(),
                            "note": "already-staged accepted event; original staging preserved verbatim"})
        return existing

    try:
        post = json.loads(raw)
    except ValueError as exc:
        raise RequestError("post-json is not valid JSON: {}".format(exc))
    diagnostics = list(launch_diagnostics)
    items = []
    if "code_reads" not in post:
        pass                                   # omitted entirely == unknown coverage
    elif post["code_reads"] is None:
        # An explicit null is a stated value the schema does not allow, not an omitted key.
        diagnostics.append({"code": "code_reads_null", "position": None, "original": None,
                            "reason": "code_reads is explicitly null; omit the key entirely to mean "
                                      "unknown coverage"})
    elif not isinstance(post["code_reads"], list):
        diagnostics.append({"code": "code_reads_not_array", "position": None,
                            "original": post["code_reads"], "reason": "code_reads must be an array"})
    else:
        for pos, item in enumerate(post["code_reads"]):
            ok, reason = validate_code_reads_item(item)
            if ok:
                items.append((pos, item))
            else:
                diagnostics.append({"code": "malformed_code_reads_item", "position": pos, "reason": reason,
                                     "original": item})

    staged_entries = {}
    staged_unbound = []
    staged_locations = {}
    stats = new_stats()
    for position, item in items:
        base_attr = dict(attribution, position=position,
                         report_key=sha256_hex(canonical_json([event_id, position])))
        try:
            display_path = validated_display_path(item["path"])
        except CaptureError as exc:
            # Per item, with full attribution: one unusable path (absolute, traversal, embedded
            # NUL) must never abort the valid reports sitting next to it in the same array.
            diagnostics.append({"code": "invalid_report_path", "position": position,
                                "reason": "{}:{}".format(exc.reason, exc), "original": item,
                                "attribution": base_attr})
            continue
        cap = account(stats, bounded_capture(root, display_path))
        observed = item.get("observed_sha256")
        lines = item.get("lines")
        historical = False
        hist_rec = None
        if observed is not None and len(observed) == 64 and (cap.status != "ok" or observed != cap.sha256):
            # The claim may describe a version this member saw earlier that is no longer live. It
            # may be bound ONLY against a recorded version of THIS root+path that actually held
            # those bytes — the mere existence of sources/<digest>.json proves that some file
            # somewhere held them, which authenticates nothing about this path. Version records are
            # kept independently of the live file, so a deleted source still has a usable history.
            versions = store.find_source_versions(root, display_path, observed)
            if versions:
                hist_data = store.read_source(observed)
                if hist_data is not None:
                    hist_rec = versions[0]
                    historical = True
        if cap.status != "ok" and not historical:
            staged_unbound.append({"reason": "capture_failed:{}".format(cap.reason or cap.status),
                                    "attribution": base_attr, "original": item})
            continue
        if cap.status == "ok":
            # The captured bytes are persisted immediately, at the moment they are known to match
            # the claim's basis — never re-derived from a since-changed live file at publish time —
            # and the identity that held them is recorded alongside, so a LATER historical claim
            # about this same path can be authenticated.
            store.write_source(cap.sha256, cap.data)
            store.record_source_version(root, display_path, cap.resolved_path, cap.sha256, cap.size)
        if historical:
            use_data = store.read_source(observed)
            use_sha = observed
            use_resolved = hist_rec["resolved_path"]     # the path as it resolved THEN, not today
        else:
            use_data, use_sha, use_resolved = cap.data, cap.sha256, cap.resolved_path
        binary_line_claim = False
        try:
            if lines is not None:
                start, end = line_range_to_bytes(use_data, lines[0], lines[1])
                line_pair = [lines[0], lines[1]]
            else:
                start, end = 0, len(use_data)
                line_pair = None
        except ValueError as exc:
            if lines is not None and not is_text_safe(use_data):
                # Not a bad line range — line semantics simply do not apply to binary/NUL data.
                # Preserve the valid whole-file capture as an entry, but the line claim itself
                # stays unbound (never silently reinterpreted as a whole-file report).
                start, end, line_pair = 0, len(use_data), None
                binary_line_claim = True
            else:
                staged_unbound.append({"reason": "invalid_range:{}".format(exc), "attribution": base_attr,
                                        "original": item})
                continue
        byte_range = [start, end]
        entry_id = compute_entry_id(root, display_path, use_resolved, use_sha, byte_range)
        excerpt_sha = sha256_hex(use_data[start:end])
        entry_shell = {
            "schema_version": SCHEMA_VERSION, "entry_id": entry_id, "root": root, "path": display_path,
            "resolved_path": use_resolved, "source_sha256": use_sha, "byte_range": byte_range,
            "excerpt_sha256": excerpt_sha, "lines": line_pair,
        }
        def hold(reason):
            staged_unbound.append({"reason": reason, "attribution": base_attr, "original": item,
                                    "entry_id": entry_id})
            staged_entries.setdefault(entry_id, {"shell": entry_shell, "reports": [],
                                                  "historical": historical})
        if not attributed:
            hold("unattributed_accepted_event")
        elif binary_line_claim:
            hold("line_range_not_applicable_to_binary_source")
        elif observed is None:
            hold("no_digest")
        elif use_sha != observed:
            hold("digest_mismatch")
        else:
            report = {"reader": base_attr, "inspection": item.get("inspection"), "symbol": item.get("symbol"),
                      "conclusion": item.get("conclusion"), "observed_sha256": observed}
            staged_entries.setdefault(entry_id, {"shell": entry_shell, "reports": [],
                                                  "historical": historical})
            staged_entries[entry_id]["reports"].append(report)
        if cap.status == "ok":
            # The location always reflects TODAY's live capture, never a historical version an entry
            # may have bound against — otherwise a historical bind would mislabel the display path's
            # current content and corrupt the freshness guard's baseline.
            staged_locations[display_path] = {"resolved_path": cap.resolved_path,
                                               "source_sha256": cap.sha256, "status": "current"}
    payload = {"member": member, "generation": generation, "accepted_sha256": accepted_sha256,
               "event_id": event_id, "attributed": attributed,
               "attribution": attribution, "diagnostics": diagnostics, "entries": staged_entries,
               "unbound": staged_unbound, "locations": staged_locations, "stats": stats}
    payload_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(payload_path, canonical_json(payload))
    store.record_event({"kind": "stage", "attempt": attempt_id, "task": task, "step": step,
                        "member": member, "generation": generation, "event_id": event_id,
                        "accepted_sha256": accepted_sha256, "stats": stats,
                        "read_reports": len(items),
                        "entries": len(staged_entries), "unbound": len(staged_unbound),
                        "diagnostics": len(diagnostics)})
    return payload


def merge_staged_into_index(store, attempt_id, validation_snapshot_id=None, verified_sources=None,
                            validation_results=None):
    """Merge staged entries/unbound/diagnostics/locations into the main index. The source bytes
    themselves are never re-derived here: stage_accepted_post already persisted them at the moment
    of capture, so publication only ever verifies and references already-durable evidence — it
    never trusts (or re-reads) the live file, which could have changed since staging."""
    index_obj = store.load_index()
    adir = store.attempt_dir(attempt_id) / "staged"
    if not adir.exists():
        return index_obj
    source_meta = {}
    done = set(index_obj.get("ingested_keys", []))
    newly_done = []

    def once(key):
        """True the first time this (accepted event, position) key is seen — across processes and
        across republications, because the accepted set is part of the published index itself."""
        if key in done:
            return False
        done.add(key)
        newly_done.append(key)
        return True

    # Only a source version this validation actually re-captured AND found unchanged may be
    # labelled by its snapshot — and only if the FULL identity matches: same root, same display
    # path, same resolved path and the same content digest. A path match alone is not enough: the
    # validation verified a specific version of that path, so another version staged at the same
    # path (older or newer bytes, or the same name resolving elsewhere) was not covered by it, and
    # borrowing its id would fabricate a freshness attestation the run never made.
    verified_ids = set()
    for v in (verified_sources or []):
        verified_ids.add((v.get("root"), v.get("path"), v.get("resolved_path"), v.get("source_sha256")))
    # The end validation is the authoritative statement about what each guarded display path holds
    # now. Staged observations were made earlier, while the step was still running, so they must
    # never overwrite it.
    validated_locs = {}
    for r in (validation_results or []):
        if not r.get("path"):
            continue
        validated_locs[r["path"]] = {"resolved_path": r.get("resolved_path"),
                                     "source_sha256": r.get("source_sha256"),
                                     "status": r.get("status"), "snapshot_id": validation_snapshot_id}

    for f in sorted(adir.glob("*.json")):
        payload = json.loads(f.read_bytes())
        attribution = payload.get("attribution") or {"member": payload.get("member"),
                                                     "generation": payload.get("generation"),
                                                     "accepted_sha256": payload.get("accepted_sha256")}
        # Every key below carries the FULL accepted-event identity (attempt+member+generation+tail
        # digest). Keying on the tail digest alone would let two authors who posted byte-identical
        # tails silently suppress each other's diagnostics.
        event_id = payload.get("event_id") or accepted_event_id(
            attempt_id, attribution.get("member"), attribution.get("generation"),
            attribution.get("accepted_sha256"))
        for entry_id, entry in payload["entries"].items():
            shell = entry["shell"]
            sha = shell["source_sha256"]
            if sha not in source_meta:
                try:
                    data = store.read_source(sha)
                except CodemapError as exc:
                    data = None
                    reason = str(exc)
                else:
                    reason = "source object absent at publication"
                if data is None:
                    # Staging always wrote this object; a missing or corrupt object here means the
                    # store lost/damaged data between staging and publication. Never publish a
                    # reference to evidence that cannot actually be produced, and never try to
                    # "repair" the digest from today's file bytes — the tombstone makes that
                    # refusal permanent.
                    store.record_missing_source(sha, reason)
                    if once("missing-source:{}:{}".format(event_id, entry_id)):
                        index_obj["diagnostics"].append({"code": "missing_staged_source", "entry_id": entry_id,
                                                         "source_sha256": sha, "reason": reason,
                                                         "attribution": attribution})
                    continue
                source_meta[sha] = {"size": len(data), "storage_encoding": "base64",
                                    "text_safe": is_text_safe(data)}
            new_reports = [r for r in entry["reports"] if once(r["reader"]["report_key"])]
            # A historical binding describes a version that is by definition NOT what the path
            # holds now; labelling it "current" would be a false freshness claim.
            historical = bool(entry.get("historical"))
            in_guard = (shell["root"], shell["path"], shell["resolved_path"], sha) in verified_ids
            status = "historical" if historical else ("current" if in_guard else "unverified")
            freshness = {"snapshot_id": validation_snapshot_id if in_guard else None,
                         "current_at_snapshot": status == "current", "status": status}
            existing = index_obj["entries"].get(entry_id)
            if existing is None:
                index_obj["entries"][entry_id] = dict(shell, reports=new_reports, freshness=freshness)
            else:
                existing["reports"].extend(new_reports)
                if in_guard:
                    existing["freshness"] = freshness
            index_obj["sources"][sha] = source_meta[sha]
        for u in payload["unbound"]:
            if once("unbound:" + u["attribution"]["report_key"]):
                index_obj["unbound"].append(u)
        for pos, d in enumerate(payload["diagnostics"]):
            if once("diag:{}:{}".format(event_id, pos)):
                index_obj["diagnostics"].append(dict(d, attribution=attribution))
        for path, loc in payload["locations"].items():
            if path in validated_locs:
                continue   # the end validation covered it; its verdict is written below
            index_obj["locations"][path] = dict(loc, snapshot_id=None)
    # Final word: every path this validation actually examined gets ITS result, whatever a staged
    # observation from earlier in the step happened to see.
    for path, loc in validated_locs.items():
        index_obj["locations"][path] = dict(loc)
    index_obj["ingested_keys"] = sorted(done)
    index_obj["latest_snapshot_id"] = validation_snapshot_id or index_obj.get("latest_snapshot_id")
    return index_obj


# ------------------------------------------------------------------- lookup ----
def _entry_public(e, include_evidence, include_conclusions, store, raw_only):
    out = {"entry_id": e["entry_id"], "root": e["root"], "path": e["path"], "resolved_path": e["resolved_path"],
           "source_sha256": e["source_sha256"], "byte_range": e["byte_range"], "excerpt_sha256": e["excerpt_sha256"],
           "lines": e["lines"], "freshness": e["freshness"]}
    if not raw_only:
        reports = []
        for r in e["reports"]:
            rr = {"reader": r["reader"], "symbol": r.get("symbol"), "observed_sha256": r.get("observed_sha256"),
                  "inspection": r.get("inspection")}
            if include_conclusions:
                rr["conclusion"] = r.get("conclusion")
            reports.append(rr)
        out["reports"] = reports
    else:
        out["reports"] = []
    if include_evidence:
        try:
            data = store.read_source(e["source_sha256"])
        except CodemapError as exc:
            # A corrupt object is this ENTRY's failure, reported structurally; it must not abort
            # the other entries in the same batched request.
            out["evidence_error"] = "corrupt_source_object"
            out["evidence_error_detail"] = str(exc)
            return out
        if data is None:
            out["evidence_error"] = "missing_source_object"
        else:
            start, end = e["byte_range"]
            encoding, excerpt = excerpt_encoding(data, start, end)
            if sha256_hex(data[start:end]) != e["excerpt_sha256"]:
                out["evidence_error"] = "excerpt_digest_mismatch"
            else:
                out["evidence"] = {"encoding": encoding, "content": excerpt}
    return out


LOOKUP_REQUEST_FIELDS = {"snapshot_id", "entry_ids", "paths", "symbols", "include_evidence",
                         "include_conclusions", "cursor"}


def validate_lookup_request(request):
    """Every field is resolved and type-checked BEFORE any view is selected, so an invalid request
    is reported as invalid input rather than as a view-selection outcome. An explicit null is an
    invalid value for a typed field, not an omitted field: a request that states a type it does
    not mean is rejected rather than silently reinterpreted."""
    if not isinstance(request, dict):
        raise RequestError("request must be a JSON object")
    extra = set(request.keys()) - LOOKUP_REQUEST_FIELDS
    if extra:
        raise RequestError("unknown request fields: {}".format(sorted(extra)))
    for key in ("entry_ids", "paths", "symbols"):
        if key in request:
            if not isinstance(request[key], list) or not all(isinstance(x, str) for x in request[key]):
                raise RequestError("{} must be an array of strings (an explicit null is invalid)".format(key))
    for key in ("include_evidence", "include_conclusions"):
        if key in request and not isinstance(request[key], bool):
            raise RequestError("{} must be a boolean (an explicit null is invalid)".format(key))
    if "snapshot_id" in request and not isinstance(request["snapshot_id"], str):
        raise RequestError("snapshot_id must be a string (an explicit null is invalid)")
    if "cursor" in request and not isinstance(request["cursor"], str):
        raise RequestError("cursor must be a string (an explicit null is invalid)")
    return request


def do_lookup(store, view, view_snapshot_id, request):
    validate_lookup_request(request)
    if "snapshot_id" in request and request["snapshot_id"] != view_snapshot_id:
        return {"snapshot_id": view_snapshot_id, "error": "unavailable_for_this_step",
                "entries": [], "missing": [], "next_cursor": None}
    include_evidence = bool(request.get("include_evidence", False))
    include_conclusions = bool(request.get("include_conclusions", False))
    entry_ids = request.get("entry_ids")
    paths = request.get("paths")
    symbols = request.get("symbols")

    normalized = {"entry_ids": sorted(entry_ids) if entry_ids else None,
                  "paths": sorted(paths) if paths else None,
                  "symbols": sorted(symbols) if symbols else None,
                  "include_evidence": include_evidence, "include_conclusions": include_conclusions}
    request_hash = sha256_hex(canonical_json(normalized))

    offset = 0
    if "cursor" in request:
        cursor = request["cursor"]
        try:
            token = json.loads(base64.b64decode(cursor.encode("ascii")).decode("utf-8"))
        except Exception:
            raise RequestError("invalid cursor")
        if not isinstance(token, dict) or token.get("snapshot_id") != view_snapshot_id \
                or token.get("request_hash") != request_hash:
            raise RequestError("cursor does not match this snapshot/request (a cursor is not reinterpreted after publication)")
        off = token.get("offset")
        if isinstance(off, bool) or not isinstance(off, int) or off < 0:
            raise RequestError("cursor offset must be a non-negative integer")
        offset = off

    missing = []
    symbol_unavailable = view.get("raw_only") and symbols
    matched = []
    if not entry_ids and not paths and not symbols:
        matched = list(view["entries"].values())
    else:
        by_id = {v: True for v in (entry_ids or [])}
        by_path = {v: True for v in (paths or [])}
        by_symbol = set(symbols or [])
        seen_ids, seen_paths, seen_symbols = set(), set(), set()
        for e in view["entries"].values():
            hit = False
            if e["entry_id"] in by_id:
                hit = True
                seen_ids.add(e["entry_id"])
            if e["path"] in by_path:
                hit = True
                seen_paths.add(e["path"])
            if not view.get("raw_only") and by_symbol:
                for r in e["reports"]:
                    if r.get("symbol") in by_symbol:
                        hit = True
                        seen_symbols.add(r.get("symbol"))
            if hit:
                matched.append(e)
        for v in (entry_ids or []):
            if v not in seen_ids:
                missing.append(v)
        for v in (paths or []):
            if v not in seen_paths:
                missing.append(v)
        if symbols and not view.get("raw_only"):
            for v in symbols:
                if v not in seen_symbols:
                    missing.append(v)

    matched.sort(key=lambda e: (e["root"], e["path"], e["source_sha256"], e["byte_range"][0], e["entry_id"]))
    page = matched[offset:offset + PAGE_SIZE]
    next_offset = offset + PAGE_SIZE
    next_cursor = None
    if next_offset < len(matched):
        next_cursor = base64.b64encode(canonical_json({"snapshot_id": view_snapshot_id,
                                                        "request_hash": request_hash,
                                                        "offset": next_offset})).decode("ascii")

    entries_out = [_entry_public(e, include_evidence, include_conclusions, store, bool(view.get("raw_only")))
                   for e in page]
    result = {"snapshot_id": view_snapshot_id, "entries": entries_out, "missing": missing,
              "next_cursor": next_cursor}
    if symbol_unavailable:
        result["diagnostic"] = "unavailable_in_this_view: symbol lookup is not available in the round-1 raw-only view"
    return result


LOCATOR_INSTRUCTION = (
    "The code map locates evidence and records attributed findings. A reader name, inspection "
    "label or digest is not verification by you. Inspect the raw evidence supporting any "
    "directory fact you rely on. Reuse its collection when the scope and source version match; "
    "inspect the source yourself when evidence is missing, changed, unclear or disputed. Absence "
    "from this map means unknown coverage. Request needed entries together; do not reconstruct "
    "missing text from a hash."
)

PREPASS_INSTRUCTION = (
    "This is a shared, incomplete navigation aid, not an exhaustive scope boundary or verified interpretation. "
    "You can access the entire map, including other subtasks' regions. Inspect raw evidence for each directory "
    "fact you rely on. Independently check the task's named locations and acceptance surfaces, and follow relevant "
    "dependencies beyond suggested ranges. Missing, stale, unclear, or disputed evidence requires source inspection. "
    "A reader name, inspection label, hash, or another agent's conclusion is not your verification. Absence means "
    "UNKNOWN COVERAGE. Reporting additional reads in code_reads is optional. Produce your own complete first-round proposal."
)


def render_locator(view_path, snapshot_id, num_sources, run_dir_arg, prepass=False, coverage_path=None):
    example = {"paths": ["example/path.py"], "include_evidence": True}
    example_json = json.dumps(example)
    self_path = str(Path(__file__).resolve())
    cmd = "printf %s {} | python3 {} lookup --run-dir {} --snapshot {}".format(
        _shell_quote(example_json), _shell_quote(self_path),
        _shell_quote(str(run_dir_arg)), _shell_quote(snapshot_id))
    lines = [
        "=== CODE MAP ===",
        "view: {}".format(view_path),
        "snapshot_id: {}".format(snapshot_id),
        "indexed source versions: {}".format(num_sources),
        "example lookup: {}".format(cmd),
        LOCATOR_INSTRUCTION,
        *(["Map pre-pass coverage (coverage remains unknown; artifact is informational): {}".format(coverage_path),
           PREPASS_INSTRUCTION] if prepass else []),
        "Current-source use is limited to entries marked current_at_snapshot in this step's view; "
        "other evidence is historical or unavailable.",
        "=== END CODE MAP ===",
    ]
    return "\n".join(lines) + "\n"


def _shell_quote(s):
    if not s:
        return "''"
    safe = all(c.isalnum() or c in "@%_-+=:,./" for c in s)
    return s if safe else "'" + s.replace("'", "'\"'\"'") + "'"


# --------------------------------------------------------------------- CLI -----
def cmd_validate(args):
    store = Store(args.run_dir)
    store.ensure_dirs()
    root = resolve_root(args.run_dir, args.dir)
    if args.mode == "begin":
        index_obj = store.load_index()
        refresh_stats, refresh_results = refresh_locations(root, index_obj)
        refresh_snapshot_id = store.write_snapshot({"schema_version": SCHEMA_VERSION, "attempt": None,
                                                    "mode": "begin", "guard_status": None,
                                                    "sources": refresh_results})
        for r in refresh_results:
            if r["path"] in index_obj["locations"]:
                index_obj["locations"][r["path"]]["snapshot_id"] = refresh_snapshot_id
        store.publish_index(index_obj)
        store.record_event({"kind": "validate", "mode": "begin", "attempt": None, "task": args.task,
                            "step": args.step, "snapshot_id": refresh_snapshot_id, "guard_status": None,
                            "sources": refresh_results, "stats": refresh_stats})
        view = raw_projection(index_obj) if args.raw else full_view(index_obj)
        recompute_current_at_snapshot(view, index_obj["locations"])
        guard_list = capture_guard_list(root, view)
        seq = store.next_seq()
        attempt_id = new_attempt_id(args.task, args.step, seq)
        adir = store.attempt_dir(attempt_id)
        adir.mkdir(parents=True, exist_ok=True)
        view_snapshot_id = sha256_hex(canonical_json(view))
        atomic_write(adir / "view.json", canonical_json(view))
        atomic_write(adir / "guard.json", canonical_json(guard_list))
        meta = {"attempt_id": attempt_id, "task": args.task, "step": args.step, "raw": bool(args.raw),
                "status": "open", "view_snapshot_id": view_snapshot_id, "seq": seq, "root": root,
                "guard_snapshot_id": sha256_hex(canonical_json(guard_list)),
                "validation_snapshot_id": None, "step_complete": False}
        save_meta(store, attempt_id, meta)
        reg = store.load_registry()
        reg[attempt_key(args.task, args.step)] = attempt_id
        store.save_registry(reg)
        print(json.dumps({"status": "ok", "attempt": attempt_id, "snapshot_id": view_snapshot_id,
                          "guard_snapshot_id": meta["guard_snapshot_id"],
                          "guard_sources": len(guard_list),
                          # the FULL identity of every exposed version — root, display path,
                          # resolved path and digest — so the obligation recorded in orchestration
                          # state is reconstructible even if every derived map file is lost
                          "exposed_sources": [{"root": g["root"], "path": g["path"],
                                               "resolved_path": g["resolved_path"],
                                               "source_sha256": g["source_sha256"]}
                                              for g in guard_list]}))
        return 0
    attempt_id = current_attempt_id(store, args.task, args.step)
    meta = load_meta(store, attempt_id)
    check_against_orchestration_state(args.run_dir, args.task, args.step, attempt_id, meta)
    if root is not None and meta.get("root") is not None and root != meta["root"]:
        raise RequestError("--dir resolves to {} but attempt {} was opened against {} — an attempt's "
                            "root cannot change mid-lifecycle".format(root, attempt_id, meta["root"]))
    guard_path = store.attempt_dir(attempt_id) / "guard.json"
    if not guard_path.exists():
        raise CodemapError("guard.json missing for open attempt {} — a derived file was lost; "
                            "this is an evidence/verification failure, not an empty (trivially passing) "
                            "guard".format(attempt_id))
    try:
        guard_list = json.loads(guard_path.read_bytes())
    except (OSError, ValueError) as exc:
        raise CodemapError("corrupt guard.json for attempt {}: {}".format(attempt_id, exc))
    if meta.get("guard_snapshot_id") and sha256_hex(canonical_json(guard_list)) != meta["guard_snapshot_id"]:
        raise CodemapError("guard.json for attempt {} no longer matches the guard identity recorded "
                           "when the attempt was opened".format(attempt_id))
    was_stale = meta.get("status") == "stale"
    status, results, stats = verify_guard(root, guard_list)
    if was_stale:
        status = "changed"
    snap = {"schema_version": SCHEMA_VERSION, "attempt": attempt_id, "mode": args.mode,
            "guard_status": status, "terminal_stale": was_stale, "sources": results}
    snapshot_id = store.write_snapshot(snap)
    # The snapshot is content-addressed (identical outcomes share one immutable record); the event
    # is not, so two identical re-validations remain two countable events.
    store.record_event({"kind": "validate", "mode": args.mode, "attempt": attempt_id,
                        "task": args.task, "step": args.step, "snapshot_id": snapshot_id,
                        "guard_status": status, "sources": results, "stats": stats})
    if was_stale:
        # Stale is TERMINAL. A guarded source that changed during the round invalidated this
        # attempt's votes at the moment it changed; a later re-check that happens to find the
        # original bytes back in place (restored, reverted, or coincidentally identical) does not
        # un-invalidate them, because nothing can establish what the members actually read in
        # between. The attempt is archived and replayed, never revived.
        status = "changed"
        for r in results:
            r.setdefault("note", "attempt already stale; verdict is terminal")
        meta["status"] = "stale"
        if args.mode in ("end", "resume"):
            meta["validation_snapshot_id"] = snapshot_id
        save_meta(store, attempt_id, meta)
    elif args.mode in ("end", "resume"):
        meta["validation_snapshot_id"] = snapshot_id
        if args.mode == "end" and status == "current":
            # The completed-step barrier publication requires. It records that the step reached its
            # end AND that its guard was actually verified there — an end validation that could not
            # verify the guard (pending_verification) records no barrier, so a later unchanged
            # re-check cannot publish on the strength of a barrier that was never truly reached.
            meta["step_complete"] = True
        if status == "current":
            meta["status"] = "guard_ok" if meta.get("status") != "published" else meta["status"]
        elif status == "changed":
            meta["status"] = "stale"
        else:
            meta["status"] = "pending_verification"
        # What this validation actually VERIFIED — full identities, and only the sources it found
        # unchanged. A changed/missing/unreadable result verifies nothing, so it grants no entry a
        # current label; the full result list is kept separately as the authoritative location
        # state, which a staged observation from earlier in the step may not overwrite.
        meta["verified_sources"] = [{"root": root, "path": r["path"], "resolved_path": r.get("resolved_path"),
                                     "source_sha256": r.get("source_sha256")}
                                    for r in results if r.get("status") == "current" and r.get("path")]
        meta["validation_results"] = results
        # No path-only "validated_paths" field survives: it is exactly the shape of record that
        # invited a path match to be mistaken for coverage of a version.
        save_meta(store, attempt_id, meta)
    print(json.dumps({"status": "ok", "attempt": attempt_id, "validation_snapshot": snapshot_id,
                      "guard_status": status, "sources": results, "terminal_stale": was_stale,
                      "bytes_read": stats["bytes_read"], "bytes_hashed": stats["bytes_hashed"]}))
    return 0


def cmd_ingest(args):
    store = Store(args.run_dir)
    store.ensure_dirs()
    if args.capture_json:
        root = resolve_root(args.run_dir, args.dir)
        try:
            # an unparsable request file is INVALID INPUT (exit 2), not a broken store (exit 1)
            items = json.loads(Path(args.capture_json).read_bytes())
        except ValueError as exc:
            raise RequestError("--capture-json is not valid JSON: {}".format(exc))
        except OSError as exc:
            raise RequestError("--capture-json cannot be read: {}".format(exc))
        if not isinstance(items, list):
            raise RequestError("--capture-json must contain a JSON array")
        index_obj = store.load_index()
        results = []
        stats = new_stats()
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                raise RequestError("each capture item needs a string path")
            try:
                display_path = validated_display_path(item["path"])
            except CaptureError as exc:
                # per item: one unusable path never aborts its valid neighbours
                index_obj["diagnostics"].append({"code": "invalid_capture_path", "path": item["path"],
                                                  "reason": "{}:{}".format(exc.reason, exc)})
                results.append({"path": item["path"], "status": "error", "reason": exc.reason})
                continue
            cap = account(stats, bounded_capture(root, display_path))
            if cap.status != "ok":
                index_obj["diagnostics"].append({"code": "capture_failed", "path": display_path,
                                                  "reason": cap.reason or cap.status})
                results.append({"path": display_path, "status": cap.status, "reason": cap.reason})
                continue
            byte_range = item.get("byte_range")
            line_range = item.get("lines")
            if args.capture_lines and line_range is not None and byte_range is not None:
                index_obj["diagnostics"].append({"code": "conflicting_capture_ranges", "path": display_path,
                                                  "reason": "lines and byte_range cannot both be supplied"})
                results.append({"path": display_path, "status": "error", "reason": "conflicting_ranges"})
                continue
            if line_range is not None and not args.capture_lines:
                index_obj["diagnostics"].append({"code": "line_capture_disabled", "path": display_path,
                                                  "reason": "line selectors require --capture-lines"})
                results.append({"path": display_path, "status": "error", "reason": "line_capture_disabled"})
                continue
            if args.capture_lines and line_range is not None:
                try:
                    if not (isinstance(line_range, list) and len(line_range) == 2):
                        raise ValueError("lines must be a two-item inclusive [first,last] pair")
                    byte_range = list(line_range_to_bytes(cap.data, line_range[0], line_range[1]))
                except (ValueError, TypeError) as exc:
                    index_obj["diagnostics"].append({"code": "invalid_capture_lines", "path": display_path,
                                                      "reason": str(exc)})
                    results.append({"path": display_path, "status": "error", "reason": str(exc)})
                    continue
            if byte_range is None:
                byte_range = [0, len(cap.data)]
            if not (isinstance(byte_range, list) and len(byte_range) == 2
                    and all(isinstance(x, int) and not isinstance(x, bool) for x in byte_range)
                    and 0 <= byte_range[0] <= byte_range[1] <= len(cap.data)):
                if args.capture_lines:
                    index_obj["diagnostics"].append({"code": "invalid_capture_range", "path": display_path,
                                                      "reason": "invalid byte_range"})
                    results.append({"path": display_path, "status": "error", "reason": "invalid_byte_range"})
                    continue
                raise RequestError("invalid byte_range for {}".format(display_path))
            entry_id = compute_entry_id(root, display_path, cap.resolved_path, cap.sha256, byte_range)
            excerpt = cap.data[byte_range[0]:byte_range[1]]
            lines = None
            if is_text_safe(cap.data):
                spans = line_spans(cap.data)
                for i, (s, e) in enumerate(spans):
                    if s == byte_range[0]:
                        for j in range(i, len(spans)):
                            if spans[j][1] == byte_range[1]:
                                lines = [i + 1, j + 1]
                                break
                        break
            shell = {"schema_version": SCHEMA_VERSION, "entry_id": entry_id, "root": root, "path": display_path,
                     "resolved_path": cap.resolved_path, "source_sha256": cap.sha256, "byte_range": byte_range,
                     "excerpt_sha256": sha256_hex(excerpt), "lines": lines}
            existing = index_obj["entries"].get(entry_id)
            if existing is None:
                index_obj["entries"][entry_id] = dict(
                    shell, reports=[],
                    freshness={"snapshot_id": None, "current_at_snapshot": True, "status": "captured"})
            index_obj["sources"].setdefault(cap.sha256, {"size": cap.size, "storage_encoding": "base64",
                                                         "text_safe": is_text_safe(cap.data)})
            index_obj["locations"][display_path] = {"resolved_path": cap.resolved_path,
                                                      "source_sha256": cap.sha256, "status": "current"}
            store.write_source(cap.sha256, cap.data)
            store.record_source_version(root, display_path, cap.resolved_path, cap.sha256, cap.size)
            results.append({"path": display_path, "status": "ok", "entry_id": entry_id})
        store.publish_index(index_obj)
        store.record_event({"kind": "capture", "mode": "capture_json", "attempt": None,
                            "sources": results, "stats": stats})
        print(json.dumps({"status": "ok", "results": results,
                          "bytes_read": stats["bytes_read"], "bytes_hashed": stats["bytes_hashed"]}))
        return 0
    if args.mode == "stage":
        root = resolve_root(args.run_dir, args.dir)
        attempt_id = current_attempt_id(store, args.task, args.step)
        attempt_meta = load_meta(store, attempt_id)
        check_against_orchestration_state(args.run_dir, args.task, args.step, attempt_id, attempt_meta)
        if attempt_meta.get("root") is not None and root != attempt_meta["root"]:
            raise RequestError("--dir resolves to {} but attempt {} was opened against {} — an attempt's "
                                "root cannot change mid-lifecycle".format(root, attempt_id, attempt_meta["root"]))
        payload = stage_accepted_post(store, root, args.task, args.step, attempt_id, args.member,
                                       args.generation, args.post_json, store.next_seq(),
                                       launch_id=args.launch_id or None)
        print(json.dumps({"status": "ok", "attempt": attempt_id, "member": args.member,
                           "entries": len(payload["entries"]), "unbound": len(payload["unbound"]),
                           "diagnostics": len(payload["diagnostics"])}))
        return 0
    if args.mode == "publish":
        attempt_id = current_attempt_id(store, args.task, args.step)
        meta = load_meta(store, attempt_id)
        check_against_orchestration_state(args.run_dir, args.task, args.step, attempt_id, meta)
        if meta.get("status") == "stale":
            print(json.dumps({"status": "stale", "attempt": attempt_id}))
            return 0
        if meta.get("status") == "published":
            # Idempotent: republishing the same accepted attempt must never append its staged
            # reports a second time (a resume/retry that reaches this call again is a no-op).
            index_obj = store.load_index()
            print(json.dumps({"status": "already_published", "attempt": attempt_id,
                               "entries": len(index_obj["entries"])}))
            return 0
        if meta.get("status") != "guard_ok" or not meta.get("step_complete"):
            # open / pending_verification / a step whose end-of-step barrier was never recorded:
            # the obligation is unresolved, so nothing is published and nothing staged is lost.
            print(json.dumps({"status": "not_publishable", "attempt": attempt_id,
                              "attempt_status": meta.get("status"),
                              "step_complete": bool(meta.get("step_complete")),
                              "reason": "publication requires the recorded completed-step barrier and a "
                                        "passing end-of-step freshness verification"}))
            return 0
        merged = merge_staged_into_index(store, attempt_id, meta.get("validation_snapshot_id"),
                                          meta.get("verified_sources"), meta.get("validation_results"))
        # index.json is written atomically (temp + fsync + rename), so publication is all-or-nothing;
        # a crash before the meta update simply replays into an exactly-once no-op via ingested_keys.
        store.publish_index(merged)
        meta["status"] = "published"
        save_meta(store, attempt_id, meta)
        store.record_event({"kind": "publish", "attempt": attempt_id, "task": args.task, "step": args.step,
                            "snapshot_id": meta.get("validation_snapshot_id"),
                            "entries": len(merged["entries"]), "ingested_keys": len(merged["ingested_keys"])})
        print(json.dumps({"status": "published", "attempt": attempt_id,
                           "entries": len(merged["entries"])}))
        return 0
    raise RequestError("unknown ingest mode: {}".format(args.mode))


def active_attempt(store, run_dir):
    """The attempt this run is currently owed a decision for, taken from orchestration state
    (state.codemap_pending), never from the caller's arguments. While one is active it is the ONLY
    view any lookup route may serve."""
    pend = read_orchestration_pending(run_dir)
    if pend is None:
        return None, None
    attempt_id = pend.get("attempt")
    if not attempt_id:
        reg = store.load_registry()
        attempt_id = reg.get(attempt_key(pend["task"], pend["step"]))
    if not attempt_id:
        raise CodemapError("orchestration state names a pending attempt for task={} step={} but no "
                           "such attempt exists in the registry".format(pend["task"], pend["step"]))
    meta = load_meta(store, attempt_id)
    # The view and guard this lookup is about to serve must be the ones the run is owed a decision
    # for, not whatever the registry happens to point at now.
    check_against_orchestration_state(run_dir, pend["task"], pend["step"], attempt_id, meta)
    return attempt_id, meta


def unavailable(snapshot_id, error):
    return {"snapshot_id": snapshot_id, "error": error, "entries": [], "missing": [], "next_cursor": None}


def cmd_lookup(args):
    store = Store(args.run_dir)
    try:
        request = json.loads(sys.stdin.read())
    except ValueError as exc:
        raise RequestError("stdin is not valid JSON: {}".format(exc))
    # The request is fully resolved and validated before any view is chosen, so "your request is
    # malformed" is never disguised as "that view is unavailable".
    validate_lookup_request(request)
    # Precedence, most specific first: --snapshot names a view outright; --task/--step name one
    # too (and a conflicting request.snapshot_id against it is then a mismatch WITHIN that view,
    # reported as unavailable_for_this_step by do_lookup); otherwise the request's own snapshot_id
    # selects the view, which is what makes a bare `lookup --run-dir RUN` workable.
    wanted_snapshot = args.snapshot
    if not wanted_snapshot and not (args.task and args.step):
        wanted_snapshot = request.get("snapshot_id")
    act_id, act_meta = active_attempt(store, args.run_dir)
    if act_id is not None:
        # One authoritative frozen view. Naming another snapshot, or another task/step, cannot
        # widen what this step may read — in particular it cannot escape round 1's raw-only view.
        if wanted_snapshot and wanted_snapshot != act_meta["view_snapshot_id"]:
            historical = False
            try:
                state = json.loads((Path(args.run_dir) / "state.json").read_text(encoding="utf-8"))
                task = next((t for t in state.get("config", {}).get("tasks", []) if t.get("id") == act_meta["task"]), {})
                parent = task.get("map_parent", act_meta["task"])
                record = state.get("map_prepasses", {}).get(parent, {})
                historical = (state.get("phase") in ("exec", "ratify")
                              and wanted_snapshot in (record.get("seed_snapshot_id"),
                                                      record.get("historical_snapshot_id")))
            except (OSError, ValueError, TypeError):
                pass
            if historical:
                seed_path = store.base / "seed-views" / (wanted_snapshot + ".json")
                try:
                    view = json.loads(seed_path.read_bytes())
                except (OSError, ValueError):
                    print(json.dumps(unavailable(wanted_snapshot, "unavailable_snapshot")))
                    return 0
                if sha256_hex(canonical_json(view)) != wanted_snapshot or view.get("raw_only") is not True:
                    print(json.dumps(unavailable(wanted_snapshot, "unavailable_snapshot")))
                    return 0
                print(json.dumps(do_lookup(store, view, wanted_snapshot, request)))
                return 0
            print(json.dumps(unavailable(act_meta["view_snapshot_id"], "unavailable_for_this_step")))
            return 0
        if (args.task or args.step) and (args.task != act_meta["task"] or args.step != act_meta["step"]):
            print(json.dumps(unavailable(act_meta["view_snapshot_id"], "unavailable_for_this_step")))
            return 0
        view = load_view_verified(store, act_id, act_meta)
        print(json.dumps(do_lookup(store, view, act_meta["view_snapshot_id"], request)))
        return 0
    if wanted_snapshot:
        # A bare snapshot (flag or request field) must resolve to the EXACT frozen per-attempt view
        # it names (whatever raw_only-ness that attempt had) — never silently substitute the live,
        # fully-attributed index, which would bypass round-1's raw-only isolation via the locator's
        # own advertised command.
        attempt_id, meta = find_attempt_by_view_snapshot(store, wanted_snapshot)
        if attempt_id is None:
            seed_path = store.base / "seed-views" / (wanted_snapshot + ".json")
            try:
                raw = seed_path.read_bytes()
                view = json.loads(raw)
            except (OSError, ValueError):
                print(json.dumps(unavailable(wanted_snapshot, "unavailable_snapshot")))
                return 0
            if sha256_hex(canonical_json(view)) != wanted_snapshot or view.get("raw_only") is not True:
                print(json.dumps(unavailable(wanted_snapshot, "unavailable_snapshot")))
                return 0
            print(json.dumps(do_lookup(store, view, wanted_snapshot, request)))
            return 0
        view = load_view_verified(store, attempt_id, meta)
        print(json.dumps(do_lookup(store, view, meta["view_snapshot_id"], request)))
        return 0
    if not (args.task and args.step):
        raise RequestError("no view selected: with no active attempt in the run state, lookup needs "
                           "--snapshot, a snapshot_id in the request, or --task with --step")
    attempt_id = current_attempt_id(store, args.task, args.step)
    meta = load_meta(store, attempt_id)
    view = load_view_verified(store, attempt_id, meta)
    print(json.dumps(do_lookup(store, view, meta["view_snapshot_id"], request)))
    return 0


def cmd_locator(args):
    store = Store(args.run_dir)
    attempt_id = current_attempt_id(store, args.task, args.step)
    meta = load_meta(store, attempt_id)
    view_path = store.attempt_dir(attempt_id) / "view.json"
    view = load_view_verified(store, attempt_id, meta)
    prepass = False
    coverage_path = None
    try:
        state = json.loads((Path(args.run_dir) / "state.json").read_text(encoding="utf-8"))
        prepass = state.get("map_prepass_version") == 1 and state.get("config", {}).get("map_code") is True
        task = next((t for t in state.get("config", {}).get("tasks", []) if t.get("id") == args.task), {})
        parent = task.get("map_parent", args.task)
        record = state.get("map_prepasses", {}).get(parent, {})
        coverage_path = (record.get("artifacts") or {}).get("coverage")
    except (OSError, ValueError, TypeError):
        pass
    print(render_locator(str(view_path), meta["view_snapshot_id"], len(view["sources"]), args.run_dir,
                         prepass=prepass, coverage_path=coverage_path or "unavailable"))
    return 0


def cmd_seed_locator(args):
    """Create a durable, raw-only locator over the complete pre-pass seed map."""
    store = Store(args.run_dir)
    store.ensure_dirs()
    view = raw_projection(store.load_index())
    sid = sha256_hex(canonical_json(view))
    d = store.base / "seed-views"
    d.mkdir(parents=True, exist_ok=True)
    write_content_addressed(d / (sid + ".json"), canonical_json(view))
    print(render_locator(str(d / (sid + ".json")), sid, len(view["sources"]), args.run_dir,
                         prepass=True, coverage_path=args.coverage or "unavailable"))
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="council_codemap.py", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="command", required=True)

    i = sub.add_parser("ingest")
    i.add_argument("--run-dir", required=True)
    i.add_argument("--dir", help="config.dir (real project root); required unless staging only")
    i.add_argument("--capture-json")
    i.add_argument("--capture-lines", action="store_true",
                   help="interpret per-item lines selectors against the exact captured bytes")
    i.add_argument("--task")
    i.add_argument("--step")
    i.add_argument("--mode", choices=["stage", "publish"])
    i.add_argument("--member")
    i.add_argument("--generation", type=int)
    i.add_argument("--launch-id", help="the orchestration-owned launch that produced this reply")
    i.add_argument("--post-json")

    v = sub.add_parser("validate")
    v.add_argument("--run-dir", required=True)
    v.add_argument("--dir")
    v.add_argument("--task", required=True)
    v.add_argument("--step", required=True)
    v.add_argument("--mode", required=True, choices=["begin", "end", "resume"])
    v.add_argument("--raw", action="store_true")

    lo = sub.add_parser("lookup")
    lo.add_argument("--run-dir", required=True)
    lo.add_argument("--task")
    lo.add_argument("--step")
    lo.add_argument("--snapshot")

    lc = sub.add_parser("locator")
    lc.add_argument("--run-dir", required=True)
    lc.add_argument("--task", required=True)
    lc.add_argument("--step", required=True)

    seedloc = sub.add_parser("seed-locator")
    seedloc.add_argument("--run-dir", required=True)
    seedloc.add_argument("--coverage")

    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "ingest":
            if not args.capture_json:
                if not args.mode:
                    raise RequestError("ingest needs --capture-json or --mode stage|publish")
                if not args.task or not args.step:
                    raise RequestError("ingest --mode {} needs --task and --step".format(args.mode))
                if args.mode == "stage" and (not args.member or args.generation is None or not args.post_json):
                    raise RequestError("ingest --mode stage needs --member --generation --post-json")
                if args.mode == "stage" and not args.dir:
                    raise RequestError("ingest --mode stage needs --dir")
            elif not args.dir:
                raise RequestError("ingest --capture-json needs --dir")
            return cmd_ingest(args)
        if args.command == "validate":
            if args.mode == "begin" and not args.dir:
                pass  # dir optional at begin if index has no entries yet; guard list would be empty
            if args.mode in ("end", "resume") and not args.dir:
                raise RequestError("validate --mode {} needs --dir".format(args.mode))
            return cmd_validate(args)
        if args.command == "lookup":
            # --run-dir alone is the normal call: the view comes from the run's active attempt, or
            # from a snapshot_id inside the request. The flags are optional narrowing, not a
            # precondition the caller has to know how to satisfy.
            return cmd_lookup(args)
        if args.command == "locator":
            return cmd_locator(args)
        if args.command == "seed-locator":
            return cmd_seed_locator(args)
        raise RequestError("unknown command")
    except RequestError as exc:
        print("council_codemap: {}".format(exc), file=sys.stderr)
        return 2
    except CodemapError as exc:
        print("council_codemap: {}".format(exc), file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print("council_codemap: {}".format(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
