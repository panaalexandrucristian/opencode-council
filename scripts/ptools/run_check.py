#!/usr/bin/env python3
"""Run a build/test command with the full log on disk and only a short, capped summary on stdout (stdlib only, POSIX only).

Usage:  run_check.py --log-dir DIR [--fallback-dir DIR] [--reports GLOB ...] [--max-bytes 4000] [--timeout SECONDS]
                     -- <command...>

Agents run every build/test through this wrapper instead of calling gradle/pytest/npm directly, so a 10 MB log never enters
the context (where it would be re-sent on every later call of the session). The command's OS-merged stdout+stderr is copied
byte for byte into <log-dir>/<stem>.log (no byte is dropped silently: storage errors block the command, size limits open
continuation files, anything else is reported as capture: INCOMPLETE). stdout gets at most --max-bytes bytes, always ending
with an `omitted:` line: the status and exit code, the log path, the capture verdict, the failing tests found in JUnit XML
reports (Gradle, Maven, pytest --junitxml, jest-junit, gotestsum, cargo nextest, ...) and in strict unittest/pytest text
lines (labelled text-heuristic) with the start of each failure, or, when none is identified, the error lines and the last
lines of the log. Every identified failure is complete in <stem>.failures.jsonl. The exit code is the command's own
(128+n for a signal, 124 for --timeout, 125/126/127 when the command could not be started). Read more of the log only by
line range (`sed -n 'A,Bp' LOG`; lines end only at LF bytes).

Unlike the other ptools this one executes a command and writes files: use a --log-dir outside the project.
"""
import argparse
import array
import base64
import codecs
import collections
import copy
import datetime
import errno
import fnmatch
import hashlib
import json
import math
import os
import re
import resource
import select
import signal
import stat
import subprocess
import sys
import time
import xml.parsers.expat as expat

ERR = re.compile(rb"(error|fail|failed|exception|panic|fatal|e: )", re.I)
SGR_PARAMS = 256          # the longest parameter part of a colour sequence that is recognised (real ones are a few dozen bytes)
SGR_TEXT = re.compile(r"\x1b\[[0-9;:]{0,%d}m" % SGR_PARAMS)
SGR = re.compile(rb"\x1b\[[0-9;:]{0,%d}m" % SGR_PARAMS)   # the only terminal sequence removed (from a copy of a line) before a line is matched
LINE_EXAMINE = 65536      # bytes examined per line; a longer line is counted as partially examined
TAIL_LINES = 40
ERR_LINES = 20
TAIL_PREVIEW = 4096
SHOWN_LINE_BYTES = 240


VALUE_OPTIONS = ("--log-dir", "--fallback-dir", "--reports", "--max-bytes", "--timeout")


def _max_bytes_arg(text):
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("must be an integer of at least 512")
    if value < 512:
        raise argparse.ArgumentTypeError("must be an integer of at least 512")
    return value


def _timeout_arg(text):
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a finite number > 0")
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("must be a finite number > 0")
    return value


def parse_arguments(argv):
    """Returns (namespace, command). Usage errors exit 2 through argparse (message on stderr)."""
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n")[0], allow_abbrev=False, usage="%(prog)s --log-dir DIR [options] -- COMMAND [ARG...]",
        epilog="Exit code: the command's own; 128+n killed by signal n; 124 --timeout; 130/143/129 the wrapper got "
               "SIGINT/SIGTERM/SIGHUP; 125 a log artifact cannot be created; 126 not executable; 127 not found; 2 usage error "
               "(stderr only, nothing created, the command is not run). The command receives RUN_CHECK_REPORT_DIR, a private "
               "directory for JUnit XML written by it (e.g. sh -c 'pytest --junitxml=\"$RUN_CHECK_REPORT_DIR/r.xml\"').")
    ap.add_argument("--log-dir", required=True,
                    help="directory for the log and every artifact (required, created if missing; use one outside the project)")
    ap.add_argument("--fallback-dir",
                    help="optional directory for continuation files (and the worker's files if --log-dir stops accepting them)")
    ap.add_argument("--reports", action="append", default=[],
                    help="JUnit XML glob, e.g. '**/build/test-results/**/*.xml' (repeatable, relative to the cwd)")
    ap.add_argument("--max-bytes", type=_max_bytes_arg, default=4000,
                    help="ceiling of the whole stdout in bytes (default 4000, at least 512; refused below the computed minimum)")
    ap.add_argument("--timeout", type=_timeout_arg,
                    help="seconds (finite, > 0) before the command's process group is stopped (default: no limit)")
    argv = list(argv)
    if "--" in argv:
        split = argv.index("--")
        options, command = argv[:split], argv[split + 1:]
    else:
        options, command = argv, None
    # a value that starts with '-' (for example --timeout -inf) is glued so argparse does not read it as an option
    glued = []
    for token in options:
        if glued and glued[-1] in VALUE_OPTIONS:
            glued[-1] += "=" + token
        else:
            glued.append(token)
    a, extra = ap.parse_known_args(glued)
    if command is None or extra:
        ap.error("the literal -- must precede the command" + (" (unrecognized arguments: %s)" % " ".join(extra) if extra else ""))
    if not command:
        ap.error("give the command after --")
    return a, command


DOT = " · "


def esc(text, path=False):
    """Escape for the summary: invalid UTF-8 bytes (lone surrogates from surrogateescape), C0 controls except TAB,
    DEL, C1, U+2028/2029 and other lone surrogates become \\xNN or \\uNNNN, and every literal backslash is doubled, so
    a literal backslash-x1b and a real ESC byte never look alike (`path` is kept for the callers that name a path)."""
    out = []
    for ch in text:
        cp = ord(ch)
        if 0xDC80 <= cp <= 0xDCFF:
            out.append("\\x%02x" % (cp - 0xDC00))
        elif 0xD800 <= cp <= 0xDFFF:
            out.append("\\u%04x" % cp)
        elif cp in (0x2028, 0x2029):
            out.append("\\u%04x" % cp)
        elif (cp < 0x20 and cp != 0x09) or 0x7F <= cp <= 0x9F:
            out.append("\\x%02x" % cp)
        elif ch == "\\":
            out.append("\\\\")
        else:
            out.append(ch)
    return "".join(out)


def raw_bytes(text):
    """The UTF-8 bytes of text that was decoded from the log with surrogateescape: an invalid byte becomes that very byte
    again (never '?'), so that lengths and cuts count the bytes of the log."""
    try:
        return text.encode("utf-8", "surrogateescape")
    except UnicodeEncodeError:                 # a lone surrogate that is no escaped byte
        return text.encode("utf-8", "replace")


def errno_name(exc):
    return errno.errorcode.get(getattr(exc, "errno", None) or 0, "EIO")


def first_line(status, code, elapsed, lines, nbytes, markers=()):
    return DOT.join([status, "exit code %d" % code] + list(markers) +
                    ["%.1fs" % elapsed, "%s log lines" % lines, "%s log bytes" % nbytes])


class LogAnalyzer:
    """One streaming pass over the written stream: lines end ONLY at byte 0x0A (str.splitlines is never used). Besides the
    line counts and the tail it finds the failure blocks of the log (their line and byte ranges) and hands every finding to
    `hit(kind, info)`; it keeps no text of a block, only offsets, so its memory is bounded by the lines it examines."""

    # the identity, then the suffix unittest itself prints for a subTest: one space and text in () or [] (` (i=0)`, ` [why]`,
    # ` [why] (s='a (b) c')`, ` (<subtest>)`)
    UNITTEST = re.compile(rb"^(FAIL|ERROR): (\S+) \(([^()\s]+)\)( [(\[].*[)\]])?$")
    PYTEST = re.compile(rb"^FAILED (\S+::\S.*?)( - .*)?$")
    UNITTEST_RESULT = re.compile(rb"^FAILED \((.*)\)$")
    PYTEST_RESULT = re.compile(rb"^=+ ")
    PYTEST_COUNT = re.compile(rb"\b(\d+) (?:failed|errors?)\b")     # 'N failed' and 'N error(s)' of a result line
    COUNT = re.compile(r"(failures|errors)=(\d+)")
    EQUALS = re.compile(rb"^={3,}$")                                # ends a unittest block
    DASHES = re.compile(rb"^-{3,}$")                                # the underline, or the rule in front of 'Ran N tests'
    RAN = re.compile(rb"^Ran [0-9]+ tests? in ")
    FAILURES = re.compile(rb"^=+ FAILURES =+$")                     # opens the section of the pytest failure blocks
    BANNER = re.compile(rb"^=+ .+ =+$")                             # any pytest section banner: ends a block
    PY_HEADER = re.compile(rb"^_+ (\S.*?) _+$")                     # opens a pytest failure block
    TRACEBACK = b"Traceback (most recent call last):"

    def __init__(self, hit=None):
        self.hit = hit                                       # called with each recognised failure: hit(kind, info)
        self.prefix_end = False                              # the stream ends here only because it is a prefix (pending, unwritten)
        self.runner_failed = 0                               # failing tests the runners themselves reported
        self.line_count = 0
        self.partial_lines = 0
        self.tail = collections.deque(maxlen=TAIL_LINES)     # (line number, preview bytes, true length, is ERR line)
        self.err_lines = []                                  # first ERR_LINES: (line number, preview bytes, true length)
        self.err_total = 0
        self._cur = bytearray()
        self._cur_len = 0
        self._offset = 0                                     # stream offset of the start of the line being read
        self._ut = None                                      # the open unittest block
        self._py = None                                      # the open pytest failure block
        self._py_section = False                             # inside the FAILURES section

    def feed(self, chunk):
        pieces = chunk.split(b"\n")
        for piece in pieces[:-1]:
            self._piece(piece)
            self._end_line(True)
        self._piece(pieces[-1])

    def finish(self):
        if self._cur_len:
            self._end_line(False)
        ended = "prefix_end" if self.prefix_end else "eof"
        self._close_unittest(ended, self.line_count, self._offset)
        self._close_pytest(ended, self.line_count, self._offset)
        if self.hit:
            self.hit("end", None)

    @staticmethod
    def examine(raw):
        """The copy of a line that is matched: one trailing CR and every SGR colour sequence removed (the line itself is kept)."""
        examined = raw[:-1] if raw.endswith(b"\r") else raw
        return SGR.sub(b"", examined) if b"\x1b" in examined else examined

    @classmethod
    def pytest_failed(cls, examined):
        """(classname, name, file, nodeid, message) of a pytest 'FAILED nodeid[ - message]' line, else None."""
        match = cls.PYTEST.match(examined)
        if not match:
            return None
        nodeid = match.group(1).decode("utf-8", "surrogateescape")
        classname, name, file = pytest_identity(nodeid)
        message = (match.group(2) or b"")[3:].decode("utf-8", "surrogateescape")
        return classname, name, file, nodeid, message

    def _recognise(self, number, s, start, end, lf, cr):
        """Anchored whole-line patterns only: unittest FAIL/ERROR, pytest FAILED, the runners' own failure counts and the
        boundaries of the failure blocks. `s` is the examined copy; `end` is the offset of the line terminator."""
        after = end + (1 if lf else 0)
        header = self.UNITTEST.match(s) if s.startswith((b"FAIL: ", b"ERROR: ")) else None
        if s.startswith(b"FAILED "):
            if s.startswith(b"FAILED ("):
                match = self.UNITTEST_RESULT.match(s)
                if match:
                    self.runner_failed += sum(int(n) for _, n in self.COUNT.findall(match.group(1).decode("ascii", "replace")))
            elif self.hit:
                fields = self.pytest_failed(s)
                if fields:
                    self.hit("pytest_failed", {"number": number, "start": start, "length": end - start, "fields": fields})
        elif s[:1] == b"=" and self.PYTEST_RESULT.match(s):
            self.runner_failed += sum(int(n) for n in self.PYTEST_COUNT.findall(s))
        if not self.hit:
            return
        if self._ut is not None:
            self._unittest_line(number, s, start, end, after, cr, header)
        if self._ut is None and header is not None:
            self._open_unittest(number, s, start, after, header)
        self._pytest_line(number, s, start, after)

    def _long_line(self, number, start, end, first):
        """A line beyond the examination limit is text of the open block; it can neither start nor end one, but its first
        byte still tells whether it is indented (a frame) or the start of the exception report."""
        if self.hit and self._ut is not None:
            self._unittest_line(number, None, start, end, end + 1, False, None, first)

    # ---- unittest blocks -----------------------------------------------------------------------------------------------

    def _open_unittest(self, number, s, start, after, header):
        suffix = header.group(4)
        classname, _, name = header.group(3).decode("utf-8", "surrogateescape").rpartition(".")
        self._ut = {"kind": header.group(1).decode("ascii"), "classname": classname, "name": name,
                    "subtest": suffix[1:].decode("utf-8", "surrogateescape") if suffix else None,
                    "header": s[:240].decode("utf-8", "surrogateescape"), "first": number, "start": start,
                    "body_start": after, "body_start_line": number + 1, "underlined": False, "dash": None,
                    "tb": 0, "has_tb": False, "msg_start": None, "first_nb": None, "nb_end": None}

    def _unittest_line(self, number, s, start, end, after, cr, header, first=b""):
        ut = self._ut
        dash = ut["dash"]
        if dash is not None:                        # a dash-only line waits for the line after it: 'Ran N tests in' ends the block
            ut["dash"] = None
            if s is not None and self.RAN.match(s):
                self._close_unittest("summary", dash[0] - 1, dash[1])
                return
            self._unittest_body(ut, dash[4], dash[1], dash[2], dash[3])
        if s is None:
            self._unittest_body(ut, None, start, end, cr, first)
        elif self.EQUALS.match(s):
            self._close_unittest("separator", number - 1, start)
        elif header is not None:
            self._close_unittest("next_header", number - 1, start)
        elif self.DASHES.match(s):
            if not ut["underlined"] and number == ut["first"] + 1:
                ut["underlined"] = True             # the header's underline belongs to the block, not to its body text
                ut["body_start"], ut["body_start_line"] = after, number + 1
            else:
                ut["dash"] = (number, start, end, cr, s)
        else:
            self._unittest_body(ut, s, start, end, cr)

    def _unittest_body(self, ut, s, start, end, cr, first=b""):
        """A text line of the block: the last exception report starts at the first column-0 line after the frames of the
        last traceback; blank lines around the message are not part of it."""
        if s is None or s.strip():
            if ut["first_nb"] is None:
                ut["first_nb"] = start
            ut["nb_end"] = end - (1 if cr else 0)
        lead = first if s is None else s[:1]
        if s is not None and s.startswith(self.TRACEBACK):
            ut["tb"], ut["has_tb"], ut["msg_start"] = 1, True, None
        elif ut["tb"] == 1 and lead and lead not in b" \t":
            ut["tb"], ut["msg_start"] = 2, start

    def _close_unittest(self, ended_by, last_line, end_off):
        ut, self._ut = self._ut, None
        if ut is None:
            return
        if ut["dash"] is not None:                  # the stream ended right after a dash-only line: it is text of the block
            dash = ut["dash"]
            ut["dash"] = None
            self._unittest_body(ut, dash[4], dash[1], dash[2], dash[3])
        if ut["has_tb"]:
            msg_start, source = ut["msg_start"], "traceback"
        elif ut["underlined"]:
            msg_start, source = ut["first_nb"], "block_text"
        else:
            msg_start, source = None, "none"          # no underline: the boundary of the message is not certain
        msg_end = ut["nb_end"]
        if msg_start is None or msg_end is None or msg_end <= msg_start:
            msg_start = msg_end = None
            source = "none"
        self.hit("unittest", {"kind": ut["kind"], "classname": ut["classname"], "name": ut["name"], "subtest": ut["subtest"],
                              "header": ut["header"], "first_line": ut["first"], "last_line": last_line, "start": ut["start"],
                              "end": end_off, "body_start": ut["body_start"], "body_start_line": ut["body_start_line"],
                              "message_range": None if msg_start is None else (msg_start, msg_end), "message_from": source,
                              "ended_by": ended_by})

    # ---- pytest failure blocks -----------------------------------------------------------------------------------------

    def _pytest_line(self, number, s, start, after):
        first = s[:1]
        if first not in (b"=", b"_"):
            return
        if self._py_section:
            if first == b"=" and (self.FAILURES.match(s) or self.BANNER.match(s)):
                self._close_pytest("section_end", number - 1, start)
                self._py_section = bool(self.FAILURES.match(s))
            elif first == b"_":
                match = self.PY_HEADER.match(s)
                if match:
                    self._close_pytest("next_header", number - 1, start)
                    self._py = {"name": match.group(1).decode("utf-8", "surrogateescape"), "first": number, "start": start,
                                "body_start": after}
        elif first == b"=" and self.FAILURES.match(s):
            self._py_section = True

    def _close_pytest(self, ended_by, last_line, end_off):
        py, self._py = self._py, None
        if py is not None:
            self.hit("pytest_block", {"header": py["name"], "first_line": py["first"], "last_line": last_line,
                                      "start": py["start"], "end": end_off, "body_start": py["body_start"],
                                      "body_start_line": py["first"] + 1, "ended_by": ended_by})

    def _piece(self, piece):
        self._cur_len += len(piece)
        room = LINE_EXAMINE - len(self._cur)
        if room > 0:
            self._cur += piece[:room]

    def _end_line(self, lf):
        self.line_count += 1
        number, line, true_len = self.line_count, bytes(self._cur), self._cur_len
        start = self._offset
        self._offset = start + true_len + (1 if lf else 0)
        self._cur = bytearray()
        self._cur_len = 0
        if true_len > LINE_EXAMINE:
            self.partial_lines += 1
            self._long_line(number, start, start + true_len, line[:1])
        else:
            self._recognise(number, self.examine(line), start, start + true_len, lf, line.endswith(b"\r"))
        is_err = bool(ERR.search(line))
        self.tail.append((number, line[:TAIL_PREVIEW], true_len, is_err))
        if is_err:
            self.err_total += 1
            if len(self.err_lines) < ERR_LINES:
                self.err_lines.append((number, line[:TAIL_PREVIEW], true_len))


REASON_BYTES = 80


ARGV_BYTES = 160


def bounded(text, limit=REASON_BYTES, path=False):
    """Escaped text of at most `limit` bytes: a longer text is cut inside no code point and marked [...]."""
    text = esc(text, path=path)
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    end = limit - len("[...]")
    while end > 0 and (data[end] & 0xC0) == 0x80:
        end -= 1
    return data[:end].decode("utf-8") + "[...]"


ERRNO_WIDTH = max(len(name) for name in errno.errorcode.values())
WIDE = "9" * 20                      # the widest counter any summary line has to budget for
SAME_FS = {True: "yes", False: "no", None: "unknown"}


def line_log(path):
    return "log: " + esc(path, path=True)


def line_capture_verified(nbytes, sha):
    return "capture: VERIFIED %s bytes, sha256 %s (fsync + read-back)" % (nbytes, sha)


def line_capture_incomplete(reason, written, received, unwritten, first_unwritten, sha_written, sha_stream):
    where = " (stream offsets %s..%s)" % (first_unwritten, received - 1 if isinstance(received, int) else received) \
        if unwritten else ""
    return ("capture: INCOMPLETE (%s): %s of %s stream bytes written, %s not written%s; sha256 written %s, stream %s"
            % (bounded(reason), written, received, unwritten, where, sha_written, sha_stream))


def line_capture_pending(reason, nbytes, sha, readback):
    return "capture: PENDING (%s): %s bytes so far, sha256 so far %s, read-back %s" % (bounded(reason), nbytes, sha, readback)


def line_pending(pid, nbytes, failures, summary, complete, fallback=None):
    where = ("; if the log directory stops accepting them the same file names are used in the fallback directory %s"
             % esc(fallback, path=True)) if fallback else ""
    return ("CAPTURE_PENDING: a detached process still holds the output pipe; background worker pid %s keeps capturing; "
            "counts below cover only the first %s bytes; final counts and additional failing tests are unknown; "
            "at EOF it writes %s, %s and, last, %s%s"
            % (pid, nbytes, esc(failures, path=True), esc(summary, path=True), esc(complete, path=True), where))


def line_continued(path, start, nbytes, cause, same_fs):
    return ("log continued: %s (from stream offset %s, %s bytes, after %s; same filesystem as the log: %s); "
            "read the parts in order" % (esc(path, path=True), start, nbytes, cause, SAME_FS[same_fs]))


def line_storage(episodes, retries, seconds, errno_text, offset, unwritten, gave_up, fallback_error):
    failed = "; fallback directory failed: %s" % fallback_error if fallback_error else ""
    if gave_up:
        return ("storage: STALLED_ON_STORAGE %s stall(s), %s retries, gave up after %ss: %s bytes not written%s"
                % (episodes, retries, seconds, unwritten, failed))
    return ("storage: STALLED_ON_STORAGE %s stall(s), %s retries, %ss total, last %s at offset %s; "
            "command output was blocked, not dropped%s" % (episodes, retries, seconds, errno_text, offset, failed))


def line_index(path, error=None):
    if error:
        return "failures index: INCOMPLETE (%s): %s" % (bounded(error), esc(path, path=True))
    return "failures index: " + esc(path, path=True)


DEDUP_LINE = "dedup capped: later duplicates may be listed twice"


def line_cap(count, kind, source):
    return "reports not examined: %s+ (%s %s)" % (count, kind, esc(source, path=True))


def line_reports(changed, produced, stale, rewritten, removed, errors):
    again = " (%s rewritten with identical content, failures NOT listed)" % rewritten if rewritten else ""
    return ("reports: %s changed during this run (concurrent writers not excluded), %s produced by this invocation, "
            "stale %s%s, removed %s, errors %s" % (changed, produced, stale, again, removed, errors))


def line_failing(total, junit, text, pending=False):
    return "failing tests: %s identified%s (junit %s, text-heuristic %s)" % (total, " so far" if pending else "", junit,
                                                                            text)


def line_omitted(k, total, d, w, h, x, lines, c, p):
    """The always-last omission line; a zero count is not mentioned (the worst case passes WIDE for every count)."""
    parts = []
    if k:
        parts.append("%s of %s failing tests not listed" % (k, total))
    if d:
        parts.append("%s of %s failure details not shown" % (d, total))
    if w:
        parts.append("%s warning lines not shown" % w)
    if h:
        parts.append("%s more error lines not shown" % h)
    if x:
        parts.append("%s of %s log lines not shown" % (x, lines))
    if c:
        parts.append("%s lines or messages cut" % c)
    if p:
        parts.append("%s lines only partially examined" % p)
    return "omitted: " + ("; ".join(parts) if parts else "nothing")


class Facts:
    """Everything the summary can show, collected after the analysis; render_summary turns it into the final bytes."""

    def __init__(self):
        self.first = ""                # line 1
        self.log = ""                  # 'log: ...'
        self.capture = ""              # 'capture: ...'
        self.continued = []            # one line per continuation segment
        self.storage = None            # 'storage: ...' or None
        self.pending = None            # 'CAPTURE_PENDING: ...' or None
        self.index = ""                # 'failures index: ...'
        self.caps = []                 # 'reports not examined: ...' lines
        self.dedup = False             # the 'dedup capped: ...' line
        self.warnings = []             # WARNING lines, optional
        self.warnings_dropped = 0      # warnings that were not even kept (they are counted in the omission line)
        self.reports = None            # 'reports: ...' or None
        self.failing = None            # 'failing tests: ...' / 'failing tests not identified' (None: the command did not run)
        self.failing_total = 0
        self.identities = []           # per failing test that can be listed: (identity line, detail lines or None, cut count, shown log lines, shown error lines)
        self.line_count = 0
        self.partial = 0
        self.err_total = 0
        self.err_rows = []             # first ERR lines: (line number, text, cut)
        self.tail_rows = []            # last lines: (line number, text, cut, is ERR line)
        self.unwritten = None          # (bytes not written, total lines, [(text, cut)]) or None


def pytest_identity(nodeid):
    """(classname, name, file) of a pytest node id such as a/b/test_x.py::C::t[p]."""
    head, bracket, rest = nodeid.partition("[")
    parts = head.split("::")
    module = parts[0][:-3] if parts[0].endswith(".py") else parts[0]
    return ".".join([module.replace("/", ".")] + parts[1:-1]), parts[-1] + bracket + rest, parts[0]


def cut_line(preview, true_len, limit=SHOWN_LINE_BYTES):
    """Shown text of one log line, cut at `limit` bytes without splitting a UTF-8 sequence; returns (text, was_cut)."""
    if true_len <= limit:
        return esc(preview[:true_len].decode("utf-8", "surrogateescape")), False
    end = limit
    while end > 0 and (preview[end] & 0xC0) == 0x80:
        end -= 1
    return esc(preview[:end].decode("utf-8", "surrogateescape")) + "[...+%d bytes]" % (true_len - end), True


def unwritten_rows(capture):
    """The last lines of the bytes that never reached a segment: (bytes not written, total lines, rows)."""
    rows = capture.tail.split(b"\n")
    if rows and rows[-1] == b"":
        rows.pop()
    total = len(rows)
    return capture.unwritten, total, [cut_line(row[:TAIL_PREVIEW], len(row)) for row in rows[-TAIL_LINES:]]


class Selection:
    """Which optional pieces are shown; the text and the omission line follow from it."""

    def __init__(self, facts):
        self.f = facts
        self.warn = []                 # indexes of the warning lines shown
        self.ident = 0                 # listed failing tests (a prefix of the identities)
        self.marker = False            # the '[... M more failing tests not listed ...]' line
        self.details = []              # listed tests whose detail block is shown
        self.err = 0                   # first ERR rows shown
        self.tail = 0                  # last rows shown (a suffix)
        self.unw = 0                   # unwritten-tail rows shown (a suffix)

    def shown_rows(self):
        f = self.f
        return f.err_rows[:self.err], f.tail_rows[len(f.tail_rows) - self.tail:]

    def omitted(self):
        f = self.f
        err_rows, tail_rows = self.shown_rows()
        numbers = {row[0] for row in err_rows} | {row[0] for row in tail_rows}
        err_shown = {row[0] for row in err_rows} | {row[0] for row in tail_rows if row[3]}
        for i in self.details:                             # the log lines an excerpt shows are shown lines, not counted twice
            numbers |= f.identities[i][3]
            err_shown |= f.identities[i][4]
        cut = sum(1 for row in err_rows if row[2]) + sum(1 for row in tail_rows if row[2])
        if f.unwritten:
            cut += sum(1 for row in f.unwritten[2][len(f.unwritten[2]) - self.unw:] if row[1])
        cut += sum(f.identities[i][2] for i in self.details)
        return line_omitted(f.failing_total - self.ident, f.failing_total, f.failing_total - len(self.details),
                            len(f.warnings) - len(self.warn) + f.warnings_dropped, f.err_total - len(err_shown),
                            f.line_count - len(numbers), f.line_count, cut, f.partial)

    def lines(self):
        f = self.f
        out = [f.first, f.log, f.capture] + f.continued
        if f.storage:
            out.append(f.storage)
        if f.pending:
            out.append(f.pending)
        if f.index:
            out.append(f.index)
        out += f.caps
        if f.dedup:
            out.append(DEDUP_LINE)
        out += [f.warnings[i] for i in self.warn]
        if f.reports:
            out.append(f.reports)
        if f.failing is not None:
            out.append(f.failing)
        out += [f.identities[i][0] for i in range(self.ident)]
        if self.marker:
            out.append("[... %d more failing tests not listed; full list in the failures index]"
                       % (f.failing_total - self.ident))
        for i in self.details:
            out += f.identities[i][1]
        err_rows, tail_rows = self.shown_rows()
        if err_rows:
            out.append("error lines (%d of %d):" % (len(err_rows), f.err_total))
            out += ["%d: %s" % (row[0], row[1]) for row in err_rows]
        if tail_rows:
            out.append("last lines (%d of %d):" % (len(tail_rows), f.line_count))
            out += ["%d: %s" % (row[0], row[1]) for row in tail_rows]
        if f.unwritten and self.unw:
            rows = f.unwritten[2][len(f.unwritten[2]) - self.unw:]
            out.append("unwritten tail (last %d of %d lines; %d bytes not written):" % (len(rows), f.unwritten[1],
                                                                                        f.unwritten[0]))
            out += [row[0] for row in rows]
        out.append(self.omitted())
        return out

    def text(self):
        return "\n".join(self.lines()).encode("utf-8") + b"\n"


def render_summary(facts, cap):
    """The final stdout bytes: the mandatory lines, then optional pieces added greedily in priority order (warnings,
    listed failing tests with their marker, detail blocks, error rows, last rows, unwritten tail) while the whole text,
    its final newline and the exact omission line still fit in `cap` bytes."""
    sel = Selection(facts)
    if len(sel.text()) > cap:
        raise ValueError("the mandatory summary lines need %d bytes, --max-bytes is %d" % (len(sel.text()), cap))

    def fits():
        return len(sel.text()) <= cap

    for i in range(len(facts.warnings)):
        sel.warn.append(i)
        if not fits():
            sel.warn.pop()
    def fits_identities(count):
        sel.ident = count
        sel.marker = count < facts.failing_total
        return fits()

    # the listed identities are a prefix; every extra row adds more bytes than the marker and the omission line can lose,
    # so the largest prefix that fits is found by bisection (the full list is tried first: it needs no marker)
    if not fits_identities(len(facts.identities)):
        low, high = 0, len(facts.identities) - 1
        while low < high:
            mid = (low + high + 1) // 2
            if fits_identities(mid):
                low = mid
            else:
                high = mid - 1
        sel.ident = low
    sel.marker = sel.ident < facts.failing_total
    if not fits():
        sel.marker = False
    for i in range(sel.ident):
        if facts.identities[i][1] is None:
            continue                     # only the first MAX_LISTED failing tests keep a detail block
        sel.details.append(i)
        if not fits():
            sel.details.pop()
    for attr, count in (("err", len(facts.err_rows)), ("tail", len(facts.tail_rows)),
                        ("unw", len(facts.unwritten[2]) if facts.unwritten else 0)):
        while getattr(sel, attr) < count:
            setattr(sel, attr, getattr(sel, attr) + 1)
            if not fits():
                setattr(sel, attr, getattr(sel, attr) - 1)
                break
    return sel.text()


def _write(fd, view):
    """The single seam every log write goes through. Writes all of `view` (partial writes are continued, EINTR is
    retried) and returns its length; on another error the OSError carries `.written`, the bytes that did get out."""
    view = memoryview(view)
    done = 0
    while done < len(view):
        try:
            done += os.write(fd, view[done:])
        except InterruptedError:
            continue
        except OSError as exc:
            exc.written = done
            raise
    return done


def _fsync(fd):
    """The seam of every fsync of a log segment."""
    os.fsync(fd)


def _read_back(paths):
    """The seam of the read-back: (bytes read, sha256 hex) of the files read in order."""
    digest = hashlib.sha256()
    total = 0
    for path in paths:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                digest.update(chunk)
                total += len(chunk)
    return total, digest.hexdigest()


MAX_SEGMENTS = 4
TAIL_BYTES = 65536
TICK = 0.1                    # longest wait of the select loop
RETRY_INITIAL = 0.05          # first backoff after a failed write; doubles up to RETRY_MAX
RETRY_MAX = 1.0
TERM_GRACE_SECONDS = 5        # SIGTERM to the group, then this long before SIGKILL
KILL_GRACE_SECONDS = 2        # after SIGKILL, how long to wait for the group to vanish
DRAIN_SECONDS = 2             # after the group is gone, how long to wait for EOF on the pipe
MAX_STALL_SECONDS = 120       # a continuous storage stall longer than this makes the capture INCOMPLETE
STALL_FALLBACK_SECONDS = 10   # after this long a stalled log continues in --fallback-dir


class Segment:
    def __init__(self, path, fd, start, cause=None, same_fs=None):
        self.path = path
        self.fd = fd
        self.start = start
        self.cause = cause
        self.same_fs = same_fs
        self.bytes = 0


class Capture:
    """Lossless copy of the command's output into the log (and continuation segments) with exact accounting:
    received = written + unwritten + len(pending)."""

    def __init__(self, log_path, log_fd, stem, log_dir, fallback_dir=None, deadline=None):
        self.stem = stem
        self.deadline = deadline           # absolute monotonic deadline launch + --timeout, or None
        self.log_dir = log_dir
        self.fallback_dir = fallback_dir
        self.segments = [Segment(log_path, log_fd, 0, same_fs=True)]
        self.received = 0
        self.written = 0
        self.pending = b""
        self.unwritten = 0
        self.unwritten_start = None
        self.tail = b""                    # the last TAIL_BYTES of the bytes that were not written
        self.incomplete = None             # reason once the log stopped being a complete copy
        self.stall_start = None            # monotonic time the current storage stall began
        self.stall_next = 0.0              # earliest monotonic time of the next write retry
        self.stall_delay = 0.0
        self.stall_episodes = 0
        self.stall_retries = 0
        self.stall_total = 0.0
        self.stall_errno = None
        self.stall_offset = None
        self.stall_gave_up = None          # seconds the stall lasted when the capture gave up
        self.stall_fallback_done = False
        self.fallback_error = None
        self.sha_stream = hashlib.sha256()
        self.sha_written = hashlib.sha256()
        self.verified = None

    def view(self):
        """A frozen copy of the accounting (hash states copied) for the analysis of exactly the bytes written so far."""
        frozen = copy.copy(self)
        frozen.sha_stream = self.sha_stream.copy()
        frozen.sha_written = self.sha_written.copy()
        frozen.segments = [copy.copy(segment) for segment in self.segments]
        return frozen

    def pump(self, fd, tick):
        """One turn of the select loop; returns True at EOF. While a write is failing the pipe is NOT read
        (backpressure: the command blocks on the full pipe instead of losing output) and the write is retried
        with an exponential backoff."""
        now = time.monotonic()
        if self.pending:
            if self.stall_next > now:
                time.sleep(min(tick, self.stall_next - now))
                return False
            self._flush()
            return False
        if not select.select([fd], [], [], tick)[0]:
            return False
        data = os.read(fd, 65536)
        if not data:
            return True
        self.accept(data)
        return False

    def accept(self, data):
        self.received += len(data)
        self.sha_stream.update(data)
        if self.incomplete:
            self._drop(data)
            return
        self.pending += data
        self._flush()

    def _drop(self, data):
        self.unwritten += len(data)
        self.tail = (self.tail + data)[-TAIL_BYTES:]

    def _give_up(self, reason):
        self.incomplete = reason
        self.unwritten_start = self.written
        data, self.pending = self.pending, b""
        self._drop(data)

    def _commit(self, segment, count):
        chunk, self.pending = self.pending[:count], self.pending[count:]
        segment.bytes += count
        self.written += count
        self.sha_written.update(chunk)

    def _flush(self):
        while self.pending:
            if self.stall_start is not None:
                self.stall_retries += 1
            segment = self.segments[-1]
            try:
                _write(segment.fd, self.pending)
            except OSError as exc:
                progress = getattr(exc, "written", 0)
                self._commit(segment, progress)
                if progress > 0:
                    self._end_stall()          # confirmed progress ends the continuous stall; a new failure starts a new one
                if exc.errno == errno.EFBIG:
                    try:
                        opened = self._open_segment(errno_name(exc))
                    except OSError as seg_exc:
                        self._give_up("cannot create continuation segment: %s" % errno_name(seg_exc))
                        return
                    if not opened:
                        self._give_up("no segment slot left after %s" % errno_name(exc))
                        return
                    continue
                self._stall(exc)
                return
            self._commit(segment, len(self.pending))
            self._end_stall()

    def _stall(self, exc):
        now = time.monotonic()
        if self.stall_start is None:
            self.stall_start = now
            self.stall_episodes += 1
            self.stall_delay = RETRY_INITIAL
        else:
            self.stall_delay = min(self.stall_delay * 2, RETRY_MAX)
        self.stall_errno = errno_name(exc)
        self.stall_offset = self.written
        self.stall_next = now + self.stall_delay
        if self.deadline is not None and now < self.deadline:
            self.stall_next = min(self.stall_next, self.deadline)      # the retry at the deadline fails and gives up
        expired = now >= self.deadline if self.deadline is not None else now - self.stall_start >= MAX_STALL_SECONDS
        if expired:
            self.stall_gave_up = now - self.stall_start
            self._give_up("write stalled on %s for %.1fs" % (self.stall_errno, self.stall_gave_up))
            self._end_stall()
        elif self.fallback_dir and not self.stall_fallback_done and now - self.stall_start >= STALL_FALLBACK_SECONDS:
            try:
                if self._open_segment(self.stall_errno, self.fallback_dir):
                    self.stall_fallback_done = True
                    self.stall_next = now
            except OSError as exc:
                self.fallback_error = errno_name(exc)

    def _end_stall(self):
        if self.stall_start is not None:
            self.stall_total += time.monotonic() - self.stall_start
            self.stall_start = None
            self.stall_fallback_done = False

    def _open_segment(self, cause, directory=None):
        number = len(self.segments) + 1
        if number > MAX_SEGMENTS:
            return False
        directory = directory or self.fallback_dir or self.log_dir
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "%s.log.%d" % (self.stem, number))
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        segment = Segment(path, fd, self.written, cause)
        self.segments.append(segment)             # from here on the segment owns the descriptor
        try:
            segment.same_fs = os.stat(directory).st_dev == os.stat(self.log_dir).st_dev
        except OSError:
            try:                                  # the paths cannot be inspected: ask the descriptors this capture owns
                segment.same_fs = os.fstat(fd).st_dev == os.fstat(self.segments[0].fd).st_dev
            except OSError:
                pass                              # the relation stays unknown; the usable segment is kept
        return True

    def finish(self):
        for segment in self.segments:
            try:
                _fsync(segment.fd)
            except OSError as exc:
                self.incomplete = self.incomplete or "fsync failed: %s" % errno_name(exc)
            os.close(segment.fd)
        try:
            back, digest = _read_back([segment.path for segment in self.segments])
        except OSError as exc:
            self.verified = False
            self.incomplete = self.incomplete or "read-back failed: %s" % errno_name(exc)
            return
        self.verified = back == self.written and digest == self.sha_written.hexdigest()
        if not self.verified:
            self.incomplete = self.incomplete or "read-back mismatch"


class Index:
    """The failures index: JSON Lines, ensure_ascii, exclusive create. A write error marks it INCOMPLETE and never
    touches the raw log or the exit code. The file always ends on a complete record: a record that could only be written
    in part is cut off again (ftruncate), and when even that fails `torn` says a partial last record remains. The records
    that could not be written are counted; a missing 'end' record proves an incomplete file."""

    def __init__(self, path, error=None):
        self.path = path
        self.error = error             # errno name of the first failure, None while every record was written
        self.size = 0                  # bytes of complete records in the file
        self.written = 0               # complete records in the file
        self.dropped = 0               # records that could not be written
        self.torn = False              # a partial record could not be cut off
        self.created = error is None   # the file exists (possibly INCOMPLETE)
        self.fd = None if error else os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)

    def emit(self, record):
        if self.error is not None or self.fd is None:
            self.dropped += self.error is not None
            return
        data = (json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")
        done = 0
        try:
            while done < len(data):
                done += os.write(self.fd, data[done:])
        except OSError as exc:
            self.error = errno_name(exc)
            self.dropped += 1
            if done:
                try:
                    os.ftruncate(self.fd, self.size)
                except OSError:
                    self.torn = True
            return
        self.size += len(data)
        self.written += 1

    def copy_records(self, path, start, end):
        """Writes again, one record at a time, the complete records in bytes [start, end) of another index file."""
        left = end - start
        copied = 0
        try:
            with open(path, "rb") as fh:
                fh.seek(start)
                while left > 0:
                    limit = min(left, REPORT_CHUNK)              # never past the saved range, never more than one chunk
                    line = fh.readline(limit)
                    if not line:
                        raise EOFError("the file ends %d bytes before the end of the saved range" % left)
                    if not line.endswith(b"\n"):
                        why = ("has no line feed before the file ends" if len(line) < limit else
                               "extends beyond the saved range" if limit == left else
                               "is longer than %d bytes" % REPORT_CHUNK)
                        raise ValueError("the record at byte %d %s" % (end - left, why))
                    record = json.loads(line)
                    if not isinstance(record, dict):
                        raise ValueError("the record at byte %d is not a JSON object" % (end - left))
                    left -= len(line)
                    self.emit(record)
                    copied += 1
        except (OSError, ValueError, EOFError, RecursionError) as exc:
            self.emit({"type": "coverage", "kind": "records_not_copied", "from": path, "range": [start, end],
                       "copied_records": copied, "reason": "%s: %s" % (type(exc).__name__, bounded(str(exc)))})

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError as exc:
                self.error = self.error or errno_name(exc)
            self.fd = None

    def reason(self):
        """Why the index is INCOMPLETE, with the counts; None when every record was written."""
        if self.error is None:
            return None
        return "%s; %d records written, %d not written%s" % (self.error, self.written, self.dropped,
                                                             "; last record torn" if self.torn else "")


def group_alive(pgid):
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Supervisor:
    """Runs the command's process group to its end while the capture drains the pipe: timeout and wrapper signals
    end in SIGTERM to the group, TERM_GRACE_SECONDS later SIGKILL; leftovers of a normal exit get the same treatment."""

    def __init__(self, proc, capture, deadline, signals):
        self.proc = proc
        self.capture = capture
        self.pgid = proc.pid
        self.deadline = deadline
        self.signals = signals                 # filled by the flag-only handlers
        self.reason = None                     # None | "timeout" | "signal"
        self.leftover = None                   # None | "SIGTERM" | "SIGKILL" | "survived"
        self.eof = False
        self.pending = False                   # a detached process still holds the pipe after the drain time

    def _kill_group(self, sig):
        try:
            os.killpg(self.pgid, sig)
        except ProcessLookupError:
            pass

    def run(self):
        fd = self.proc.stdout.fileno()
        term_at = kill_at = drain_from = None
        while True:
            now = time.monotonic()
            rc = self.proc.poll()
            if term_at is None:
                if self.signals:
                    self.reason = "signal"
                elif rc is None and self.deadline is not None and now >= self.deadline:
                    self.reason = "timeout"
                elif rc is not None and group_alive(self.pgid):
                    self.leftover = "SIGTERM"
                if self.reason or self.leftover:
                    self._kill_group(signal.SIGTERM)
                    term_at = now
            group = group_alive(self.pgid) if (rc is not None or term_at is not None) else True
            if term_at is not None and group:
                if kill_at is None and now - term_at >= TERM_GRACE_SECONDS:
                    self._kill_group(signal.SIGKILL)
                    kill_at = now
                    if self.leftover:
                        self.leftover = "SIGKILL"
                elif kill_at is not None and now - kill_at >= KILL_GRACE_SECONDS:
                    if self.leftover:
                        self.leftover = "survived"
                    group = False
            if not self.eof:
                self.eof = self.capture.pump(fd, TICK)
            elif rc is None or group:
                time.sleep(TICK)
            if rc is not None and not group:
                if self.eof:
                    return
                if drain_from is None or self.capture.stall_start is not None:
                    drain_from = time.monotonic()      # a stalled sink is resolved before the drain time counts
                elif time.monotonic() - drain_from >= DRAIN_SECONDS:
                    self.pending = True
                    return


MAX_REPORT_PATHS = 10000          # distinct realpaths retained in one discovery pass
MAX_UNEXAMINED_COUNT = 1000000    # candidate occurrences counted beyond that cap, per source and pass
MAX_MARKUP_TOKEN_BYTES = 8388608  # longest XML markup token the pre-scan lets through
DEDUP_CAP = 200000                # identity digests kept for de-duplication
OVERFLOW_BYTES = 65536            # the fixed-size filter of the pytest names that were seen beyond DEDUP_CAP
MAX_LISTED = 200                  # failing tests that keep an excerpt for the summary
ENDED_BY = ("separator", "summary", "next_header", "section_end", "eof", "prefix_end")     # how a failure block can end
MESSAGE_CHARS = 300
BODY_LINES = 12
BODY_RECORD_CHARS = 16384         # a body record holds at most this many characters (never more than 64 KiB of UTF-8)
REPORT_CHUNK = 65536


class Identity:
    """One failing test as the summary lists it: its number, identity and source (a row of the identity list)."""

    def __init__(self, number, classname, name, file, source, subtest=None):
        self.number = number
        self.classname, self.name, self.file = classname, name, file
        self.source = source
        self.subtest = subtest
        self.also_in_log = False

    def identity(self):
        text = "%s.%s" % (self.classname or "?", self.name or "?")
        return text + (" " + self.subtest if self.subtest else "") + (" (%s)" % self.file if self.file else "")

    def label(self):
        return self.source + (", also in log" if self.also_in_log else "")

    def row_bytes(self):
        """A lower bound of the bytes of its row: the number, a space, the identity, ' [' + source + ']' and the line feed."""
        return len(self.identity().encode("utf-8", "replace")) + len(self.source) + len(str(self.number)) + 6


class Excerpt(Identity):
    """The bounded part of one failing test that the summary can show (the complete data is in the failures index)."""

    def __init__(self, number, classname, name, file, source, message, subtest=None, total=None):
        super().__init__(number, classname, name, file, source, subtest)
        self.log_line = None
        self.first_line = None         # the log line number of the first body line, for a failure block of the log
        self.message = message[:MESSAGE_CHARS]
        self.message_cut = max(0, (len(message) if total is None else total) - MESSAGE_CHARS)
        self.lines = []                # up to BODY_LINES non-blank body lines: (text, true length in UTF-8 bytes, log line)
        self.more = False              # non-blank body lines beyond BODY_LINES exist
        self._cur = ""
        self._cur_len = 0
        self._seen = 0                 # body lines read, blank ones included
        self._done = False

    def feed(self, text):
        if self._done:
            return
        pieces = text.split("\n")
        for k, piece in enumerate(pieces):
            self._cur_len += len(raw_bytes(piece))
            room = 1024 - len(self._cur)
            if room > 0:
                self._cur += piece[:room]
            if k < len(pieces) - 1:
                self._end_line()
                if self._done:
                    return

    def close(self):
        if not self._done:
            self._end_line()

    def _end_line(self):
        line, length = self._cur, self._cur_len
        self._cur, self._cur_len = "", 0
        number = None if self.first_line is None else self.first_line + self._seen
        self._seen += 1
        if not line.strip():
            return
        if len(self.lines) < BODY_LINES:
            self.lines.append((line, length, number))
        else:
            self.more = True
            self._done = True


class Warnings:
    """The WARNING lines that could still fit in the summary (`budget` = --max-bytes); every other one is only counted."""

    def __init__(self, budget):
        self.budget = budget
        self.kept = []
        self.bytes = 0
        self.dropped = 0

    def add(self, text):
        size = len(text.encode("utf-8", "replace")) + 1
        if self.bytes + size <= self.budget:
            self.kept.append(text)
            self.bytes += size
        else:
            self.dropped += 1

    def extend(self, other):
        for text in other.kept:
            self.add(text)
        self.dropped += other.dropped


def read_range(capture, start, end):
    """The bytes [start, end) of the written stream, in chunks of at most REPORT_CHUNK bytes (bounded memory); the exact
    `bytes` of every segment count, as in read_stream. A segment that is shorter than recorded raises OSError."""
    offset = 0
    for segment in capture.segments:
        seg_end = offset + segment.bytes
        if seg_end > start and offset < end:
            low, high = max(start, offset), min(end, seg_end)
            with open(segment.path, "rb") as fh:
                fh.seek(low - offset)
                left = high - low
                while left > 0:
                    chunk = fh.read(min(REPORT_CHUNK, left))
                    if not chunk:
                        raise OSError(errno.EIO, "the log segment ends %d bytes before its recorded end" % left)
                    left -= len(chunk)
                    yield chunk
        offset = seg_end
        if offset >= end:
            break


class Chunker:
    """Ordered records of at most BODY_RECORD_CHARS characters from a stream of text (bounded memory)."""

    def __init__(self, emit, first_seq=0):
        self.emit = emit
        self.seq = first_seq
        self.parts = []
        self.size = 0
        self.total = 0                 # characters fed
        self.count = 0                 # records emitted

    def feed(self, text):
        self.total += len(text)
        while text:
            part = text[:BODY_RECORD_CHARS - self.size]
            self.parts.append(part)
            self.size += len(part)
            text = text[len(part):]
            if self.size >= BODY_RECORD_CHARS:
                self.flush()

    def flush(self):
        if self.parts:
            self.emit(self.seq, "".join(self.parts))
            self.seq += 1
            self.count += 1
            self.parts, self.size = [], 0


class MessageStream:
    """The text of one failure message, streamed. `first()` is the message field of the failure record (its first
    BODY_RECORD_CHARS characters); the rest goes to `emit(seq, text)` as ordered continuation chunks from seq 1.
    mode 'region' takes every character; mode 'eline' takes the pytest E lines ('E' and up to three spaces removed), joined
    by LF. SGR sequences and the CR of a CRLF terminator are removed; invalid UTF-8 stays reversible (surrogateescape)."""

    TAIL = re.compile(r"\x1b(?:\[[0-9;:]{0,%d})?$|\r$" % SGR_PARAMS)     # a sequence or a CRLF that may continue in the next chunk (bounded)

    def __init__(self, mode, emit):
        self.mode = mode
        self.decoder = codecs.getincrementaldecoder("utf-8")("surrogateescape")
        self.carry = ""
        self.head = []
        self.head_size = 0
        self.rest = Chunker(emit, 1)
        self.total = 0                 # characters of the whole message
        self.kind = None               # eline: None (undecided), "E" (an E line is being taken), "skip"
        self.pre = ""
        self.taken = 0                 # E lines taken

    def feed(self, data, final=False):
        text = self.carry + self.decoder.decode(data, final)
        self.carry = ""
        if not final:
            tail = self.TAIL.search(text)
            if tail:
                self.carry, text = text[tail.start():], text[:tail.start()]
        text = SGR_TEXT.sub("", text).replace("\r\n", "\n")
        if self.mode == "region":
            self._out(text)
            return
        pieces = text.split("\n")
        for k, piece in enumerate(pieces):
            if self.kind == "E":
                self._out(piece)
            elif self.kind is None:
                self.pre += piece
                if len(self.pre) >= 4:
                    self._decide(False)
            if k < len(pieces) - 1 or final:                     # the line ends here
                if self.kind is None:
                    self._decide(True)
                self.kind, self.pre = None, ""

    def _decide(self, at_end):
        pre, self.pre = self.pre, ""
        if pre[:1] == "E" and (pre[1:2] == " " or (at_end and len(pre) == 1)):
            if self.taken:
                self._out("\n")
            self.taken += 1
            self.kind = "E"
            self._out(pre[1:4].lstrip(" ") + pre[4:])
        else:
            self.kind = "skip"

    def _out(self, text):
        self.total += len(text)
        if self.head_size < BODY_RECORD_CHARS:
            take = text[:BODY_RECORD_CHARS - self.head_size]
            self.head.append(take)
            self.head_size += len(take)
            text = text[len(take):]
        if text:
            self.rest.feed(text)

    def finish(self):
        self.feed(b"", True)
        self.rest.flush()

    def first(self):
        return "".join(self.head)


class BlockRead:
    """What streaming one failure block into the index left behind."""

    def __init__(self):
        self.chunks = 0                # body records emitted
        self.chars = 0                 # characters of the body
        self.message = ""              # the first chunk of the message
        self.message_chars = 0
        self.message_more = 0          # continuation chunk records
        self.excerpt = None            # (non-blank body lines, more) for the summary
        self.error = None              # why the block could not be read completely


class Sink:
    """Failing tests as they are found: identity de-duplication, the failures index records, the bounded excerpts, and the
    failure blocks of the log (streamed into the index, then attached to the failure they belong to)."""

    def __init__(self, index, budget):
        self.index = index
        self.budget = budget           # --max-bytes: nothing that cannot fit in the summary is kept in memory
        self.stream = None             # the capture whose written stream the blocks are read from
        self.seen = set()
        self.capped = False
        self.count = self.junit = self.text = self.duplicates = 0
        self.rows = []                 # Identity (an Excerpt for the first MAX_LISTED) of each failing test, in order
        self.rows_bytes = 0
        self.rows_open = True          # false once a row cannot fit: no later row can be listed either (a prefix)
        self.junit_by_key = {}         # digest(classname, name) -> [(file or None, failure number)] for text matching
        self.occurrences = {}          # digest of a text subTest identity -> how many times it was seen (at most DEDUP_CAP keys)
        self.occurrences_capped = False
        self.subtest_observations = 0  # subTest failures of the log that are observations of a JUnit failure
        self.subtest_cases = set()     # the JUnit failures that carry at least one of them
        self.observed = set()          # the JUnit failures that the log corroborates (a text observation was merged into them)
        self.ambiguous = 0
        self.blocks = 0                # failure blocks numbered so far (unittest and pytest, in the order they end)
        self.unmatched = 0             # pytest blocks that no failure took
        # pytest blocks and FAILED lines wait for each other until the end of the log, as fixed-size records (no text, no dict)
        self.py_blocks = array.array("Q")    # per block six numbers: number, first line, last line, start, end, body start
        self.py_ended = bytearray()          # per block: how it ended (an index into ENDED_BY)
        self.py_keys = []                    # per block: its name key (the header text is read again from the log when needed)
        self.py_block_keys = {}              # name key -> index of the block, or the list of indexes when several blocks share it
        self.py_failed = array.array("Q")    # per FAILED line three numbers: line number, byte offset, length
        self.py_failed_keys = []             # per FAILED line: its name key
        self.assoc_capped = False
        self.py_overflow = None              # bit filter of the names of blocks and FAILED lines beyond the cap (fixed size)
        self.warned = Warnings(budget)

    def warn(self, text):
        """Keeps the warning while all kept warnings could still fit in the summary; otherwise it is only counted."""
        self.warned.add(text)

    @staticmethod
    def digest(*parts):
        """A fixed-size key of the parts as a JSON array: no choice of text in one part can move a boundary between parts."""
        return hashlib.sha256(json.dumps(parts, ensure_ascii=True).encode("ascii")).digest()[:16]

    def failure(self, source, classname, name, file, origin, message, kind, nodeid=None, subtest=None, fields=None,
                total=None):
        """Registers one observed failing test; returns (number, excerpt or None), or None for a duplicate. Every subTest
        failure of the log is an occurrence of its own (its number among those with the same identity and suffix)."""
        parts = (source, classname or "", name or "", file or "")            # an identity is kept per source
        occurrence = None
        if subtest is not None:
            occurrence = self.occurrence(self.digest(*parts, subtest))
            if occurrence is not None:
                parts += (subtest, str(occurrence))
        ident = self.digest(*parts)
        if not self.capped and not (subtest is not None and occurrence is None):     # an untracked subTest failure is never merged
            if ident in self.seen:
                self.duplicates += 1
                record = {"type": "duplicate", "classname": classname, "name": name, "file": file, "subtest": subtest}
                record.update(origin)
                if fields:                       # a text failure: the message of its own block, in the shape of a failure record
                    record["message"] = message
                    record.update(fields)
                self.index.emit(record)
                return None
            if len(self.seen) >= DEDUP_CAP:
                self.capped = True
                self.index.emit({"type": "coverage", "kind": "dedup_capped", "cap": DEDUP_CAP, "at_failure": self.count + 1})
            else:
                self.seen.add(ident)
        self.count += 1
        if source == "junit":
            self.junit += 1
        else:
            self.text += 1
        record = {"type": "failure", "id": self.count, "source": source, "kind": kind, "classname": classname,
                  "name": name, "file": file, "nodeid": nodeid, "subtest": subtest, "occurrence": occurrence,
                  "message": message}
        record.update(origin)
        record.update(fields or {})
        self.index.emit(record)
        if source == "junit" and not self.capped:
            self.junit_by_key.setdefault(self.digest(classname or "", name or ""), []).append((file, self.count))
        excerpt = row = None
        if self.count <= MAX_LISTED:
            shown = message if source == "junit" else message.lstrip()      # a pytest E line keeps its source indentation
            excerpt = row = Excerpt(self.count, classname, name, file, source, shown, subtest,
                                    None if total is None else total - (len(message) - len(shown)))
            if "log_lines" in origin:
                excerpt.log_line = origin["log_lines"][0]
        elif self.rows_open:
            row = Identity(self.count, classname, name, file, source, subtest)
        if row is not None and self.rows_open:
            if self.rows_bytes + row.row_bytes() > self.budget:
                self.rows_open = False
            else:
                self.rows_bytes += row.row_bytes()
                self.rows.append(row)
        return self.count, excerpt

    def occurrence(self, key):
        """How many times the subTest failure `key` was seen, this one included. The table holds at most DEDUP_CAP keys: a
        key beyond them has no number (None; the failure is still counted and indexed, and never merged with another), and
        one coverage record and one warning say so."""
        seen = self.occurrences.get(key)
        if seen is None and len(self.occurrences) >= DEDUP_CAP:
            if not self.occurrences_capped:
                self.occurrences_capped = True
                self.index.emit({"type": "coverage", "kind": "occurrences_capped", "cap": DEDUP_CAP})
                self.warn("WARNING: more than %d distinct subTest identities: later ones carry no occurrence number" % DEDUP_CAP)
            return None
        self.occurrences[key] = (seen or 0) + 1
        return self.occurrences[key]

    def report_mismatch(self, path, suite, declared, listed):
        """A testsuite says it has more failures or errors than it lists: nothing is invented, the difference is disclosed."""
        self.index.emit({"type": "coverage", "kind": "report_declared_mismatch", "report": path, "testsuite": suite,
                         "declared": declared, "listed": dict(listed)})
        self.warn("WARNING: REPORT_MISMATCH: %s: testsuite %s declares failures=%d errors=%d, lists failures=%d errors=%d"
                  % (esc(path, path=True), esc(suite or "?"), declared["failures"], declared["errors"], listed["failures"],
                     listed["errors"]))

    # ---- failures of the log -------------------------------------------------------------------------------------------

    def text_hit(self, kind, info):
        """A finding of the log analysis: a finished unittest block, a pytest failure block, a pytest FAILED line, or the
        end of the stream (which settles the pytest blocks and FAILED lines against each other)."""
        if kind == "unittest":
            self._unittest_block(info)
        elif kind == "pytest_block":
            self._pytest_block(info)
        elif kind == "pytest_failed":
            self._pytest_failed(info)
        elif kind == "end":
            self._pytest_settle()

    def _emit_block(self, block, runner, info, read, outcome, failure=None, failed_line=None):
        self.index.emit({"type": "text_block", "block": block, "runner": runner, "header": info["header"],
                         "log_lines": [info["first_line"], info["last_line"]], "bytes": [info["start"], info["end"]],
                         "ended_by": info["ended_by"], "body_chunks": read.chunks, "body_chars": read.chars,
                         "outcome": outcome, "failure": failure, "failed_line": failed_line, "read_error": read.error})
        if read.error:
            self.index.emit({"type": "coverage", "kind": "text_block_unread", "block": block, "reason": read.error,
                             "log_lines": [info["first_line"], info["last_line"]]})
            self.warn("WARNING: the failure block at log lines %d-%d could not be read completely: %s"
                      % (info["first_line"], info["last_line"], bounded(read.error)))

    def read_block(self, block, info, mode):
        """Streams the bytes of one failure block from the written stream into ordered `body` records and, for mode
        'region' (a byte range of the block is the message) or 'eline' (the pytest E lines), the message into a chunk
        stream. Nothing but one chunk per stream is held in memory. An I/O error is kept in `error`, never raised."""
        result = BlockRead()
        body = Chunker(lambda seq, text: self.index.emit({"type": "body", "block": block, "seq": seq, "text": text}))
        decoder = codecs.getincrementaldecoder("utf-8")("surrogateescape")
        excerpt = Excerpt(0, None, None, None, "text-heuristic", "")
        excerpt.first_line = info["body_start_line"]
        excerpt_decoder = codecs.getincrementaldecoder("utf-8")("surrogateescape")
        span = info.get("message_range") if mode == "region" else (info["body_start"], info["end"]) if mode == "eline" else None
        message = None
        if span:
            message = MessageStream(mode, lambda seq, text: self.index.emit({"type": "message", "block": block, "seq": seq,
                                                                              "text": text}))
        position = info["start"]
        try:
            for chunk in read_range(self.stream, info["start"], info["end"]):
                body.feed(decoder.decode(chunk))
                low = info["body_start"] - position
                if not excerpt._done and low < len(chunk):
                    excerpt.feed(excerpt_decoder.decode(chunk[max(low, 0):]))
                if message:
                    a, b = max(span[0] - position, 0), min(span[1] - position, len(chunk))
                    if a < b:
                        message.feed(chunk[a:b])
                position += len(chunk)
        except OSError as exc:
            result.error = "%s: %s" % (errno_name(exc), exc.strerror or exc)
        body.feed(decoder.decode(b"", True))
        body.flush()
        excerpt.feed(excerpt_decoder.decode(b"", True))
        excerpt.close()
        result.chunks, result.chars = body.count, body.total
        result.excerpt = (excerpt.lines, excerpt.more)
        if message and not result.error:
            message.finish()
            result.message, result.message_chars, result.message_more = message.first(), message.total, message.rest.count
        return result

    def _unittest_block(self, info):
        self.blocks += 1
        block = self.blocks
        read = self.read_block(block, info, "region")
        source = info["message_from"] if read.message_chars else "none"
        origin = {"log_lines": [info["first_line"], info["last_line"]], "block": block, "summary_line": None}
        fields = {"message_from": source, "message_chars": read.message_chars, "message_more": read.message_more}
        outcome, number = self._text_failure(info["classname"], info["name"], None, None, read.message, origin,
                                             info["subtest"], fields, read)
        self._emit_block(block, "unittest", info, read, outcome, number)

    def _text_failure(self, classname, name, file, nodeid, message, origin, subtest, fields, read):
        """A failure of the log: merged into the ONE JUnit failure with exactly the same classname and name (and the same
        file when both carry one); otherwise a failing test of its own. Returns (outcome, failure number or None)."""
        matches = [] if self.capped else [entry for entry in self.junit_by_key.get(self.digest(classname, name), ())
                                          if file is None or entry[0] is None or entry[0] == file]
        if len(matches) == 1:
            junit_id = matches[0][1]
            self.observed.add(junit_id)
            occurrence = None
            if subtest is not None:
                occurrence = self.occurrence(self.digest("junit-observation", str(junit_id), subtest))
                self.subtest_observations += 1
                self.subtest_cases.add(junit_id)
            record = {"type": "observation", "id": junit_id, "source": "text-heuristic", "classname": classname,
                      "name": name, "file": file, "nodeid": nodeid, "subtest": subtest, "occurrence": occurrence}
            record.update(origin)
            record["message"] = message          # the text of its own block, never the JUnit message: the head of the message
            record.update(fields)                # and how to rebuild the rest (message_chars, message_more, the `message` records)
            self.index.emit(record)
            if junit_id <= len(self.rows):
                self.rows[junit_id - 1].also_in_log = True
            return "observation", junit_id
        if len(matches) > 1:
            self.ambiguous += 1
            self.index.emit({"type": "coverage", "kind": "ambiguous_text_match", "classname": classname, "name": name,
                             "file": file, "matches": len(matches), "log_lines": [origin["log_lines"][0]] * 2})
            self.warn("WARNING: the log line %d matches %d JUnit failures (%s.%s); kept as a separate failing test"
                      % (origin["log_lines"][0], len(matches), esc(classname or "?"), esc(name or "?")))
        handle = self.failure("text-heuristic", classname, name, file, origin, message, "text", nodeid, subtest, fields,
                              fields["message_chars"])
        if handle is None:
            return "duplicate", None
        number, excerpt = handle
        if excerpt is not None and read is not None and read.excerpt:
            excerpt.lines, excerpt.more = read.excerpt
        return "failure", number

    # ---- pytest: blocks first, FAILED lines later, settled against each other at the end of the stream ------------------

    @staticmethod
    def pytest_key(nodeid):
        """The name a FAILURES block header carries for a node id: what follows the first '::', with the structural '::'
        turned into '.', and everything from the first '[' (the parameters) kept exactly."""
        head, bracket, rest = nodeid.partition("[")
        return ".".join(head.split("::")[1:]) + bracket + rest

    def _pytest_block(self, info):
        self.blocks += 1
        block = self.blocks
        key = self.digest("pytest-block", info["header"])
        if len(self.py_keys) >= DEDUP_CAP:
            self._association_capped()
            self._overflow_add(key)
            self._unmatched(block, info, self.read_block(block, info, None), "block_cap")
            return
        at = len(self.py_keys)
        known = self.py_block_keys.get(key)
        if known is None:
            self.py_block_keys[key] = at
        elif isinstance(known, int):
            self.py_block_keys[key] = [known, at]
        else:
            known.append(at)
        self.py_blocks.extend((block, info["first_line"], info["last_line"], info["start"], info["end"], info["body_start"]))
        self.py_ended.append(ENDED_BY.index(info["ended_by"]))
        self.py_keys.append(key)

    def _stored_block(self, at):
        """(block number, info) of a pytest block that was kept as six numbers: its header is read again from the log."""
        number, first, last, start, end, body_start = self.py_blocks[6 * at:6 * at + 6]
        info = {"first_line": first, "last_line": last, "start": start, "end": end, "body_start": body_start,
                "body_start_line": first + 1, "ended_by": ENDED_BY[self.py_ended[at]], "header": ""}
        try:
            head = b"".join(read_range(self.stream, start, min(end, start + LINE_EXAMINE + 2))).split(b"\n", 1)[0]
            match = LogAnalyzer.PY_HEADER.match(LogAnalyzer.examine(head))
            info["header"] = match.group(1).decode("utf-8", "surrogateescape") if match else ""
        except OSError:
            pass                                   # the block itself is read (and any error disclosed) when its body is streamed
        return number, info

    def _pytest_failed(self, info):
        key = self.digest("pytest-block", self.pytest_key(info["fields"][3]))
        if len(self.py_failed_keys) >= DEDUP_CAP:
            self._association_capped()
            self._overflow_add(key)
            self._pytest_failure(info["number"], info["fields"], None)
            return
        self.py_failed.extend((info["number"], info["start"], info["length"]))
        self.py_failed_keys.append(key)

    def _overflow_bits(self, key):
        bits = OVERFLOW_BYTES * 8
        return (int.from_bytes(key[:4], "big") % bits, int.from_bytes(key[4:8], "big") % bits)

    def _overflow_add(self, key):
        """A name seen beyond the cap is remembered in a filter of fixed size: a name that may also occur beyond the cap is
        never attached (a false positive of the filter costs an association, it can never attach a wrong one)."""
        if self.py_overflow is None:
            self.py_overflow = bytearray(OVERFLOW_BYTES)
        for bit in self._overflow_bits(key):
            self.py_overflow[bit >> 3] |= 1 << (bit & 7)

    def _overflowed(self, key):
        return self.py_overflow is not None and all(self.py_overflow[bit >> 3] & (1 << (bit & 7))
                                                   for bit in self._overflow_bits(key))

    def _association_capped(self):
        if not self.assoc_capped:
            self.assoc_capped = True
            self.index.emit({"type": "coverage", "kind": "text_block_association_capped", "cap": DEDUP_CAP})
            self.warn("WARNING: more than %d pytest failure blocks or FAILED lines: later ones are not matched to each other"
                      % DEDUP_CAP)

    def _pytest_settle(self):
        failed_keys = collections.Counter(self.py_failed_keys)
        lines_by_key = {}
        for at, key in enumerate(self.py_failed_keys):
            if key in self.py_block_keys and len(lines_by_key.setdefault(key, [])) < 20:      # only a block can name them
                lines_by_key[key].append(self.py_failed[3 * at])
        taken = set()
        ambiguous = 0
        for at, key in enumerate(self.py_failed_keys):
            number, start, length = self.py_failed[3 * at:3 * at + 3]
            known = self.py_block_keys.get(key)
            indexes = () if known is None else [known] if isinstance(known, int) else known
            block = None
            beyond = self._overflowed(key)             # the name may also belong to a block or a FAILED line beyond the cap
            if len(indexes) == 1 and failed_keys[key] == 1 and not beyond:
                block = indexes[0]
                taken.add(block)
            elif indexes:
                ambiguous += 1
                record = {"type": "coverage", "kind": "text_block_ambiguous", "reason": "ambiguous_name",
                          "failed_line": number, "blocks": [self.py_blocks[6 * i] for i in indexes[:20]],
                          "block_count": len(indexes), "failed_lines_with_this_name": failed_keys[key]}
                if beyond:
                    record["beyond_cap"] = True
                self.index.emit(record)
            try:
                fields = LogAnalyzer.pytest_failed(LogAnalyzer.examine(b"".join(read_range(self.stream, start, start + length))))
            except OSError as exc:
                fields = None
                self.index.emit({"type": "coverage", "kind": "text_failure_unread", "log_line": number,
                                 "reason": "%s: %s" % (errno_name(exc), exc.strerror or exc)})
                self.warn("WARNING: the FAILED line %d could not be read again (%s); it is not listed" % (number, errno_name(exc)))
            if fields:
                self._pytest_failure(number, fields, self._stored_block(block) if block is not None else None)
        never = named = 0
        for position, key in enumerate(self.py_keys):
            if position in taken:
                continue
            block, info = self._stored_block(position)
            reason = "ambiguous_name" if key in failed_keys or self._overflowed(key) else "no_failed_line"
            never += reason == "no_failed_line"
            named += reason == "ambiguous_name"
            self._unmatched(block, info, self.read_block(block, info, None), reason, lines_by_key.get(key, ()))
        self.py_blocks, self.py_ended, self.py_keys, self.py_block_keys = array.array("Q"), bytearray(), [], {}
        self.py_failed, self.py_failed_keys, self.py_overflow = array.array("Q"), [], None
        if never or named:
            self.warn("WARNING: %d pytest failure blocks were not attached to a failing test (%d without a FAILED line, %d whose "
                      "name is ambiguous); their complete text is in the failures index as text_block records"
                      % (never + named, never, named))
        if ambiguous:
            self.warn("WARNING: %d pytest FAILED lines share their name with another FAILED line or with several blocks; no "
                      "failure block is attached to them" % ambiguous)

    def _unmatched(self, block, info, read, reason, failed_lines=()):
        self.unmatched += 1
        self.index.emit({"type": "coverage", "kind": "text_block_unmatched", "reason": reason, "block": block,
                         "header": info["header"], "log_lines": [info["first_line"], info["last_line"]],
                         "failed_lines": list(failed_lines)})
        self._emit_block(block, "pytest", info, read, "unmatched")

    def _pytest_failure(self, number, fields, pending):
        """One FAILED line, with its failure block when exactly one block and no other FAILED line has its name."""
        classname, name, file, nodeid, summary = fields
        if pending is None:
            origin = {"log_lines": [number, number], "block": None, "summary_line": number}
            self._text_failure(classname, name, file, nodeid, summary, origin, None,
                               {"message_from": "short_summary" if summary else "none", "message_chars": len(summary),
                                "message_more": 0}, None)
            return
        block, info = pending
        read = self.read_block(block, info, "eline")
        message, chars, more, source = read.message, read.message_chars, read.message_more, "e_lines"
        if not chars:
            message, chars, more, source = summary, len(summary), 0, "short_summary" if summary else "none"
        origin = {"log_lines": [info["first_line"], info["last_line"]], "block": block, "summary_line": number}
        outcome, target = self._text_failure(classname, name, file, nodeid, message, origin, None,
                                             {"message_from": source, "message_chars": chars, "message_more": more}, read)
        self._emit_block(block, "pytest", info, read, outcome, target, number)


class NotJunit(Exception):
    pass


class JunitReader:
    """Expat handlers for one report. A testcase with any failure or error child is ONE failing test; the failure body is
    streamed to the index in ordered records and never accumulated. A testsuite that declares more failures or errors than
    its own testcases list is reported (REPORT_MISMATCH)."""

    def __init__(self, sink, path):
        self.sink = sink
        self.path = path
        self.origin = {"report": path}
        self.depth = 0
        self.case = None
        self.node = None
        self.suites = []               # the open testsuites: name, declared and listed counts, whether it has nested suites
        self.testcases = self.skipped = self.failing = 0

    @staticmethod
    def declared(attrs, key):
        """A count a testsuite declares about itself, or None when it does not (or does not give a number)."""
        try:
            value = int(attrs.get(key))
        except (TypeError, ValueError):
            return None
        return value if value >= 0 else None

    def start(self, name, attrs):
        local = name.rsplit(" ", 1)[-1]
        if self.depth == 0 and local not in ("testsuites", "testsuite"):
            raise NotJunit(local)
        self.depth += 1
        if local == "testsuite" and self.case is None:
            if self.suites:
                self.suites[-1]["nested"] = True
            self.suites.append({"name": attrs.get("name"), "depth": self.depth, "nested": False, "listed": {"failures": 0, "errors": 0},
                                "declared": {"failures": self.declared(attrs, "failures"), "errors": self.declared(attrs, "errors")}})
        elif local == "testcase" and self.case is None:
            self.testcases += 1
            self.case = {"depth": self.depth, "classname": attrs.get("classname"), "name": attrs.get("name"),
                         "file": attrs.get("file"), "decided": False, "handle": None, "nodes": 0, "kinds": set()}
        elif self.case is not None and self.depth == self.case["depth"] + 1:
            if local in ("failure", "error"):
                self._begin_node(local, attrs.get("message") or "", attrs.get("type"))
            elif local == "skipped":
                self.skipped += 1

    def _begin_node(self, kind, message, error_type=None):
        case = self.case
        if kind not in case["kinds"] and self.suites:
            case["kinds"].add(kind)
            self.suites[-1]["listed"][kind + "s"] += 1
        if not case["decided"]:
            case["decided"] = True
            self.failing += 1
            case["handle"] = self.sink.failure("junit", case["classname"], case["name"], case["file"], self.origin,
                                               message, kind, fields={"error_type": error_type})
        handle = case["handle"]
        index = case["nodes"]
        case["nodes"] += 1
        self.node = {"depth": self.depth, "index": index, "seq": 0, "buffer": [], "size": 0}
        if handle is not None and index > 0:
            self.sink.index.emit({"type": "node", "id": handle[0], "node": index, "kind": kind, "message": message,
                                  "error_type": error_type})

    def chars(self, data):
        node = self.node
        if node is None or self.case["handle"] is None:
            return
        number, excerpt = self.case["handle"]
        if excerpt is not None and node["index"] == 0:
            excerpt.feed(data)
        while data:
            part = data[:BODY_RECORD_CHARS - node["size"]]
            node["buffer"].append(part)
            node["size"] += len(part)
            data = data[len(part):]
            if node["size"] >= BODY_RECORD_CHARS:
                self._flush(node)

    def _flush(self, node):
        if node["buffer"]:
            self.sink.index.emit({"type": "body", "id": self.case["handle"][0], "node": node["index"],
                                  "seq": node["seq"], "text": "".join(node["buffer"])})
            node["seq"] += 1
            node["buffer"], node["size"] = [], 0

    def abort(self):
        """The report ended early: flush the body that was being streamed."""
        if self.node is not None and self.case is not None and self.case["handle"] is not None:
            self._flush(self.node)
            if self.node["index"] == 0 and self.case["handle"][1] is not None:
                self.case["handle"][1].close()
        self.node = None

    def end(self, name):
        if self.node is not None and self.depth == self.node["depth"]:
            if self.case["handle"] is not None:
                self._flush(self.node)
                if self.node["index"] == 0 and self.case["handle"][1] is not None:
                    self.case["handle"][1].close()
            self.node = None
        elif self.case is not None and self.depth == self.case["depth"]:
            self.case = None
        elif self.case is None and self.suites and self.depth == self.suites[-1]["depth"]:
            suite = self.suites.pop()
            declared = {key: value or 0 for key, value in suite["declared"].items()}
            if not suite["nested"] and any(declared[key] > suite["listed"][key] for key in declared):
                self.sink.report_mismatch(self.path, suite["name"], declared, suite["listed"])
        self.depth -= 1


class MarkupScanner:
    """Byte pre-scan in front of expat. Every markup token (start, end and empty-element tags with their quoted
    attributes, comments, processing instructions, declarations including a DOCTYPE with its internal subset) has to fit
    in `limit` bytes; character data and CDATA content stream through unlimited. feed() returns the bytes expat may see
    now: text and complete tokens. The bytes of a token in progress are held back, so an over-limit token is never
    given to the parser; `over` then holds the zero-based offset of its opening '<'."""

    _FULL_TAG = re.compile(rb"""<[^<>"'!?]?[^<>"']*(?:(?:"[^"]*"|'[^']*')[^<>"']*)*>""")
    _QUOTE_OR_GT = re.compile(rb"""[>"']""")

    def __init__(self, limit):
        self.limit = limit
        self.state = "text"
        self.consumed = 0
        self.held = bytearray()
        self.start = 0
        self.over = None
        self.quote = None
        self.dstate = "outer"
        self.sub = bytearray()
        self.tail = b""

    def feed(self, chunk):
        if self.over is not None:
            return b""
        out = []
        base = self.consumed
        self.consumed += len(chunk)
        i, n = 0, len(chunk)
        while i < n and self.over is None:
            if self.state == "text":
                j = chunk.find(b"<", i)
                if j < 0:
                    out.append(chunk[i:])
                    break
                if j > i:
                    out.append(chunk[i:j])
                self.start = base + j
                i = self._begin(chunk, j, out)
            elif self.state == "cdata":
                i = self._cdata(chunk, i, out)
            else:
                i = self._continue(chunk, i, out)
        return b"".join(out)

    def finish(self):
        """Bytes of a token still open at the end of the file (at most `limit`); expat reports them as malformed."""
        held, self.held = bytes(self.held), bytearray()
        return held if self.over is None else b""

    def _too_long(self):
        if len(self.held) > self.limit:
            self.over = self.start
            self.held = bytearray()
            return True
        return False

    def _begin(self, chunk, j, out):
        """A '<' at chunk[j]: complete tokens inside this chunk are handled at once, the rest is held."""
        nxt = chunk[j + 1:j + 2]
        if nxt not in (b"!", b"?"):
            match = self._FULL_TAG.match(chunk, j)
            if match:
                if match.end() - j > self.limit:
                    self.over = self.start
                else:
                    out.append(chunk[j:match.end()])
                return match.end()
        elif nxt == b"?":
            k = chunk.find(b"?>", j + 2)
            if k >= 0:
                if k + 2 - j > self.limit:
                    self.over = self.start
                else:
                    out.append(chunk[j:k + 2])
                return k + 2
        elif chunk.startswith(b"<!--", j):
            k = chunk.find(b"-->", j + 4)
            if k >= 0:
                if k + 3 - j > self.limit:
                    self.over = self.start
                else:
                    out.append(chunk[j:k + 3])
                return k + 3
        self.held = bytearray(b"<")
        self.state = "open"
        return j + 1

    def _release(self, out):
        out.append(bytes(self.held))
        self.held = bytearray()
        self.state = "text"

    def _continue(self, chunk, i, out):
        state = self.state
        if state == "open":
            return self._open(chunk, i, out)
        if state == "tag":
            return self._tag(chunk, i, out)
        if state == "comment":
            return self._until(chunk, i, out, b"-->", 4)
        if state == "pi":
            return self._until(chunk, i, out, b"?>", 2)
        return self._decl(chunk, i, out)

    def _open(self, chunk, i, out):
        n = len(chunk)
        while i < n:
            self.held.append(chunk[i])
            i += 1
            head = bytes(self.held)
            if head[1:2] == b"?":
                self.state = "pi"
            elif head[1:2] == b"!":
                if head == b"<![CDATA[":
                    self._release(out)
                    self.state, self.tail = "cdata", b""
                elif head.startswith(b"<!--"):
                    self.state = "comment"
                elif b"<!--".startswith(head) or b"<![CDATA[".startswith(head):
                    continue
                else:
                    self.state, self.dstate, self.quote = "decl", "outer", None
            else:
                self.state, self.quote = "tag", None
            return i
        return i

    def _tag(self, chunk, i, out):
        n = len(chunk)
        while i < n:
            if self.quote:
                k = chunk.find(self.quote, i)
                if k < 0:
                    self.held += chunk[i:]
                    i = n
                else:
                    self.held += chunk[i:k + 1]
                    i, self.quote = k + 1, None
            else:
                match = self._QUOTE_OR_GT.search(chunk, i)
                if not match:
                    self.held += chunk[i:]
                    i = n
                else:
                    self.held += chunk[i:match.end()]
                    i = match.end()
                    if match.group() == b">":
                        if not self._too_long():
                            self._release(out)
                        return i
                    self.quote = match.group()
            if self._too_long():
                return n
        return i

    def _until(self, chunk, i, out, terminator, first):
        """Comment or processing instruction: held until `terminator` (searched from `first` bytes into the token)."""
        before = len(self.held)
        self.held += chunk[i:]
        k = self.held.find(terminator, max(first, before - len(terminator) + 1))
        if k < 0:
            self._too_long()
            return len(chunk)
        end = k + len(terminator)
        used = len(chunk) - (len(self.held) - end)
        del self.held[end:]
        if not self._too_long():
            self._release(out)
        return used

    def _cdata(self, chunk, i, out):
        data = self.tail + chunk[i:]
        k = data.find(b"]]>")
        if k < 0:
            out.append(chunk[i:])
            self.tail = data[-2:]
            return len(chunk)
        end = k + 3 - len(self.tail)
        out.append(chunk[i:i + end])
        self.state, self.tail = "text", b""
        return i + end

    def _decl(self, chunk, i, out):
        """A declaration such as <!DOCTYPE ... [ internal subset ]>: quotes, the subset brackets and the comments,
        processing instructions and declarations inside the subset are tracked byte by byte (rare and bounded)."""
        n = len(chunk)
        while i < n:
            c = chunk[i]
            self.held.append(c)
            i += 1
            if len(self.held) > self.limit:
                self.over = self.start
                self.held = bytearray()
                return n
            d = self.dstate
            if self.quote:
                if c == self.quote:
                    self.quote = None
            elif d == "outer":
                if c in (0x22, 0x27):
                    self.quote = c
                elif c == 0x5B:
                    self.dstate = "subset"
                elif c == 0x3E:
                    self._release(out)
                    return i
            elif d == "subset":
                if c == 0x5D:
                    self.dstate = "outer"
                elif c == 0x3C:
                    self.dstate, self.sub = "sub-open", bytearray(b"<")
            elif d == "sub-open":
                self.sub.append(c)
                head = bytes(self.sub)
                if head[1:2] == b"?":
                    self.dstate = "sub-pi"
                elif head.startswith(b"<!--"):
                    self.dstate = "sub-comment"
                elif head[1:2] == b"!" and not (b"<!--".startswith(head)):
                    self.dstate = "sub-decl"
                elif head[1:2] not in (b"!", b""):
                    self.dstate = "subset"
            elif d == "sub-comment":
                self.sub.append(c)
                if bytes(self.sub[-3:]) == b"-->" and len(self.sub) >= 7:
                    self.dstate = "subset"
            elif d == "sub-pi":
                self.sub.append(c)
                if bytes(self.sub[-2:]) == b"?>" and len(self.sub) >= 4:
                    self.dstate = "subset"
            elif d == "sub-decl":
                if c in (0x22, 0x27):
                    self.quote = c
                elif c == 0x3E:
                    self.dstate = "subset"
        return i


SAFE_ENCODING = re.compile(r"^(utf-?8|us-ascii|ascii|iso-8859-(?:[1-9]|1[0-6])|latin-?1|windows-125[0-8])$", re.I)
DECLARED_ENCODING = re.compile(rb"""^(?:\xef\xbb\xbf)?<\?xml[^>]*?encoding\s*=\s*["']([A-Za-z0-9._-]+)["']""")


def encoding_problem(head):
    """Why the markup pre-scan cannot read this report, or None; only ASCII-compatible encodings are scanned as bytes."""
    if head[:2] in (b"\xff\xfe", b"\xfe\xff") or head[:4] in (b"\x00\x00\xfe\xff", b"\xff\xfe\x00\x00") or \
            head[:2] in (b"<\x00", b"\x00<") or head[:4] in (b"<\x00\x00\x00", b"\x00\x00\x00<"):
        return "encoding not supported by the markup pre-scan (UTF-16/UTF-32)"
    match = DECLARED_ENCODING.match(head)
    if match:
        declared = match.group(1).decode("ascii")
        try:
            codecs.lookup(declared)
        except LookupError:
            return "encoding not supported by the markup pre-scan (declared %s)" % declared
        if not SAFE_ENCODING.match(declared):
            return "encoding not supported by the markup pre-scan (declared %s)" % declared
    return None


def parse_report(path, sink):
    """Parses one report with expat in chunks behind the markup pre-scan; returns (error text or None, reader).
    Failures found before an error stay."""
    reader = JunitReader(sink, path)
    parser = expat.ParserCreate(namespace_separator=" ")
    parser.buffer_text = False
    parser.StartElementHandler = reader.start
    parser.EndElementHandler = reader.end
    parser.CharacterDataHandler = reader.chars
    scanner = MarkupScanner(MAX_MARKUP_TOKEN_BYTES)
    error = None
    try:
        with open(path, "rb") as fh:
            chunk = fh.read(REPORT_CHUNK)
            error = encoding_problem(chunk[:512])
            while chunk and error is None:
                safe = scanner.feed(chunk)
                if safe:
                    parser.Parse(safe, False)
                if scanner.over is not None:
                    error = "markup token over %d bytes at byte offset %d" % (scanner.limit, scanner.over)
                    break
                chunk = fh.read(REPORT_CHUNK)
            else:
                if error is None:
                    rest = scanner.finish()
                    if rest:
                        parser.Parse(rest, False)
                    parser.Parse(b"", True)
    except OSError as exc:
        error = errno_name(exc)
    except expat.ExpatError as exc:
        error = "malformed XML: %s at line %d, column %d" % (expat.ErrorString(exc.code), exc.lineno, exc.offset)
    except NotJunit as exc:
        error = "not a JUnit report (root element %s)" % exc
    except (LookupError, UnicodeError, ValueError) as exc:
        error = "codec error: %s: %s" % (type(exc).__name__, exc)
    reader.abort()
    return error, reader


class ReportStats:
    def __init__(self):
        self.changed = self.produced = self.stale = self.rewritten = self.removed = self.errors = 0
        self.private_files = 0
        self.cap_lines = []


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(REPORT_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_state(path):
    """(size, mtime_ns, sha256 hex) of one file; raises OSError."""
    info = os.stat(path)
    return info.st_size, info.st_mtime_ns, sha256_file(path)


def _scandir(path):
    """The seam of every directory listing of the report discovery."""
    return os.scandir(path)


GLOB_MAGIC = re.compile(r"[*?[]")


def _hidden(name):
    return name[:1] == "."


class GlobWalk:
    """glob.iglob(pattern, recursive=True) (wildcards never match a leading dot unless the pattern component starts with
    one, `**` matches zero or more directories and follows symlinks) that does not swallow errors: every listing or
    inspection that fails for another reason than 'no such file or directory' or 'not a directory' goes to
    on_error(path, errno name). The expansion is lazy from the file system to the caller: a directory is read entry by
    entry (in the order the file system lists it) and a candidate is yielded as soon as it matches, so a caller that stops
    reads no more; a symlink back to a directory that is being walked is not entered again."""

    def __init__(self, on_error):
        self.on_error = on_error

    def fail(self, path, exc):
        self.on_error(path, errno_name(exc))

    def lexists(self, path):
        try:
            os.lstat(path)
        except (FileNotFoundError, NotADirectoryError, ValueError):
            return False
        except OSError as exc:
            self.fail(path, exc)
            return False
        return True

    def isdir(self, path):
        try:
            return stat.S_ISDIR(os.stat(path).st_mode)
        except (FileNotFoundError, NotADirectoryError, ValueError):
            return False
        except OSError as exc:
            self.fail(path, exc)
            return False

    def entries(self, dirname, dironly):
        """(name, is a directory, is a symlink or None when unknown) of the entries of a directory, one at a time and in the
        order the file system lists them (nothing is collected); only its directories when `dironly`."""
        try:
            with _scandir(dirname or os.curdir) as scan:
                for entry in scan:
                    path = os.path.join(dirname, entry.name) if dirname else entry.name
                    try:
                        is_dir = entry.is_dir()
                    except OSError as exc:
                        self.fail(path, exc)
                        if dironly:
                            continue
                        is_dir = False
                    if is_dir or not dironly:
                        try:
                            link = entry.is_symlink()
                        except OSError as exc:
                            self.fail(path, exc)
                            link = None            # unknown: a directory of unknown kind is walked as if it were a symlink
                        yield entry.name, is_dir, link
        except (FileNotFoundError, NotADirectoryError):
            pass
        except OSError as exc:
            self.fail(dirname or os.curdir, exc)

    def iglob(self, pathname, dironly=False):
        dirname, basename = os.path.split(pathname)
        if not GLOB_MAGIC.search(pathname):
            if basename:
                if self.lexists(pathname):
                    yield pathname
            elif self.isdir(dirname):
                yield pathname
            return
        if not dirname:
            yield from (self.glob2 if basename == "**" else self.glob1)("", basename, dironly)
            return
        dirs = self.iglob(dirname, True) if dirname != pathname and GLOB_MAGIC.search(dirname) else [dirname]
        if GLOB_MAGIC.search(basename):
            in_dir = self.glob2 if basename == "**" else self.glob1
        else:
            in_dir = self.glob0
        for directory in dirs:
            for name in in_dir(directory, basename, dironly):
                yield os.path.join(directory, name)

    def glob0(self, dirname, basename, dironly):
        if basename:
            return [basename] if self.lexists(os.path.join(dirname, basename) if dirname else basename) else []
        return [basename] if self.isdir(dirname) else []

    def glob1(self, dirname, pattern, dironly):
        match = re.compile(fnmatch.translate(pattern)).match
        hidden_too = _hidden(pattern)
        for name, _, _ in self.entries(dirname, dironly):
            if (hidden_too or not _hidden(name)) and match(name):
                yield name

    def glob2(self, dirname, pattern, dironly):
        if not dirname or self.isdir(dirname):
            yield ""
        real = os.path.realpath(dirname or os.curdir)
        yield from self.walk(dirname, real, dironly, frozenset((real,)))

    def walk(self, dirname, real, dironly, stack):
        for name, is_dir, link in self.entries(dirname, dironly):
            if _hidden(name):
                continue
            yield name
            if not is_dir:
                continue
            path = os.path.join(dirname, name) if dirname else name
            target = os.path.realpath(path) if link is not False else os.path.join(real, name)
            if target in stack:
                continue
            for sub in self.walk(path, target, dironly, stack | {target}):
                yield os.path.join(name, sub)


def expand_glob(pattern, on_error):
    """The paths a --reports glob matches, like glob.iglob(pattern, recursive=True) but with every failure reported."""
    for path in GlobWalk(on_error).iglob(pattern):
        if path:
            yield path


class Discovery:
    """One bounded discovery pass: the retained realpaths (each with the sources that found it), the number of candidate
    occurrences beyond the cap per source, and the number of candidates per source. The failures of a listing or an
    inspection are handed to `report(source, absolute path, errno name)` one at a time and are not kept: only their count,
    the sources that had one and (up to MAX_REPORT_PATHS of them) a digest each, to count a repeated failure once."""

    def __init__(self, report=None):
        self.found = {}
        self.unexamined = {}
        self.candidates = {}
        self.report = report
        self.error_count = 0
        self.failed_sources = set()     # never bounded by the retention below: they decide what is provenance-unknown
        self.dedup_capped = False       # more than MAX_REPORT_PATHS distinct failures: later repeats are counted again
        self._failed = set()

    def fail(self, source, path, code):
        self.failed_sources.add(source)
        path = os.path.abspath(path)
        key = Sink.digest(source, path, code)
        if key in self._failed:
            return
        if len(self._failed) < MAX_REPORT_PATHS:
            self._failed.add(key)
        else:
            self.dedup_capped = True
        self.error_count += 1
        if self.report is not None:
            self.report(source, path, code)

    def offer(self, source, real):
        """Registers a candidate; returns False when the discovery of this source has to stop."""
        self.candidates[source] = self.candidates.get(source, 0) + 1
        sources = self.found.get(real)
        if sources is not None:
            sources.add(source)
            return True
        if len(self.found) < MAX_REPORT_PATHS:
            self.found[real] = {source}
            return True
        count = self.unexamined.get(source, 0) + 1
        self.unexamined[source] = count
        return count < MAX_UNEXAMINED_COUNT


def walk_private(directory, offer, fail):
    """The *.xml regular files below the private report directory; symlinks are not followed and a directory that cannot
    be listed goes to `fail(path, errno name)` while the rest is still read."""
    root = os.path.realpath(directory)

    def walk(path):
        try:
            with _scandir(path) as entries:
                for entry in entries:
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                    except OSError as exc:
                        fail(entry.path, errno_name(exc))          # this entry only: the ones after it are still examined
                        continue
                    if is_dir:
                        if not walk(entry.path):
                            return False
                        continue
                    try:
                        is_file = entry.is_file(follow_symlinks=False)
                    except OSError as exc:
                        fail(entry.path, errno_name(exc))
                        continue
                    if is_file and entry.name.endswith(".xml"):
                        real = os.path.realpath(entry.path)
                        if real.startswith(root + os.sep) and not offer(real):
                            return False
        except OSError as exc:
            fail(os.path.realpath(path), errno_name(exc))
        return True
    walk(root)


def discover(discovery, patterns, private_dir=None):
    if private_dir is not None:
        source = ("dir", private_dir)
        walk_private(private_dir, lambda real: discovery.offer(source, real),
                     lambda path, code: discovery.fail(private_dir, path, code))
    for pattern in dict.fromkeys(patterns):
        source = ("glob", pattern)
        walker = GlobWalk(lambda path, code, pattern=pattern: discovery.fail(pattern, path, code))
        hits = walker.iglob(pattern)
        try:
            for hit in hits:
                if not hit or walker.isdir(hit):
                    continue
                if not discovery.offer(source, os.path.realpath(hit)):
                    break
        finally:
            hits.close()                # a source that stops reads no more and closes the directories it holds open


class Snapshot:
    """The reports that matched --reports before the command started: realpath -> (size, mtime_ns, sha256) or, when the
    snapshot of that path failed, the errno name. The failures of the discovery go to `index` as they happen (the byte range
    of those records is `span`); `warnings` keeps the lines that could still fit in the summary and counts the rest."""

    def __init__(self, patterns, index, budget):
        self.state = {}
        self.capped = set()            # sources whose pre-launch discovery was cut short by the cap or by a failure
        self.index = index
        self.warnings = Warnings(budget)
        self.discovery = Discovery(self.record)
        self.observed = 0              # diagnostics of the pre-launch discovery, whether or not the index could write them
        self.unpersisted = 0           # of those, the ones the index did not take (a write error is sticky: the rest of them)
        self.write_error = None        # the failure of the index at the first diagnostic it did not take
        start = index.size
        if patterns:
            discover(self.discovery, patterns)
            self.capped = {source for source, count in self.discovery.unexamined.items() if count}
            self.capped |= {("glob", pattern) for pattern in self.discovery.failed_sources}
            for real in sorted(self.discovery.found):
                try:
                    self.state[real] = file_state(real)
                except OSError as exc:
                    self.state[real] = errno_name(exc)
        self.span = (start, index.size)

    def record(self, source, path, code):
        self.warnings.add("WARNING: PROVENANCE_ERROR: %s: %s" % (esc(path, path=True), code))
        written = self.index.written
        self.index.emit({"type": "coverage", "kind": "discovery_error", "source": source, "path": path, "pass": "pre_launch",
                         "errno": code})
        self.observed += 1
        if self.index.written == written:
            self.unpersisted += 1
            self.write_error = self.write_error or self.index.error or "unknown"


def absence(path):
    """'absent' when the path is established not to exist, None when it exists, else the errno name of the failed inspection."""
    try:
        os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return "absent"
    except OSError as exc:
        return errno_name(exc)
    return None


def process_reports(a, private_dir, snapshot, sink):
    stats = ReportStats()
    sink.warned.extend(snapshot.warnings)              # the failures of the pre-launch pass were recorded when they happened
    stats.errors += snapshot.discovery.error_count
    if sink.index is not snapshot.index:               # the worker's own index: bring them over from the foreground one
        sink.index.copy_records(snapshot.index.path, *snapshot.span)
        if snapshot.unpersisted:
            sink.index.emit({"type": "coverage", "kind": "records_not_copied", "from": snapshot.index.path,
                             "range": list(snapshot.span), "expected_records": snapshot.observed,
                             "persisted_records": snapshot.observed - snapshot.unpersisted,
                             "missing_records": snapshot.unpersisted,
                             "reason": "%d of %d pre-launch discovery diagnostics were never written to the foreground index: %s"
                                       % (snapshot.unpersisted, snapshot.observed, snapshot.write_error)})

    def record(source, path, code):
        stats.errors += 1
        sink.warn("WARNING: REPORT_ERROR: %s: %s" % (esc(path, path=True), code))
        sink.index.emit({"type": "coverage", "kind": "discovery_error", "source": source, "path": path, "pass": "post_run",
                         "errno": code})
    current = Discovery(record)
    discover(current, a.reports, private_dir)
    private_real = os.path.join(os.path.realpath(private_dir), "")
    for label, discovery in (("pre_launch", snapshot.discovery), ("post_run", current)):
        if discovery.dedup_capped:
            sink.warn("WARNING: more than %d distinct discovery failures in the %s pass; a failure that repeats after that "
                      "is counted again in the errors" % (MAX_REPORT_PATHS, label.replace("_", "-")))
            sink.index.emit({"type": "coverage", "kind": "discovery_errors_dedup_capped", "pass": label,
                             "cap": MAX_REPORT_PATHS})
    for pattern in dict.fromkeys(a.reports):
        if not current.candidates.get(("glob", pattern)):
            sink.warn("WARNING: no report matches --reports glob %s" % esc(pattern, path=True))
    for real in sorted(current.found):
        label, parse, counter, reason = None, False, None, None
        if real.startswith(private_real):
            label, parse, counter = "produced by this invocation", True, "produced"
            stats.private_files += 1
        else:
            before = snapshot.state.get(real)
            if before is None and any(source in snapshot.capped for source in current.found[real]):
                stats.errors += 1
                reason = "pre-launch discovery incomplete; current-run provenance unknown"
                sink.warn("WARNING: PROVENANCE_ERROR: %s: %s" % (esc(real, path=True), reason))
                label = "provenance unknown"
            elif before is None:
                label, parse, counter = "changed during this run; concurrent writers not excluded", True, "changed"
            elif isinstance(before, str):
                stats.errors += 1
                reason = before
                sink.warn("WARNING: PROVENANCE_ERROR: %s: %s" % (esc(real, path=True), before))
                label = "provenance unknown"
            else:
                try:
                    now = file_state(real)
                except OSError as exc:
                    stats.errors += 1
                    reason = errno_name(exc)
                    sink.warn("WARNING: PROVENANCE_ERROR: %s: %s" % (esc(real, path=True), reason))
                    label = "provenance unknown"
                else:
                    if now[2] != before[2]:
                        label, parse, counter = "changed during this run; concurrent writers not excluded", True, "changed"
                    elif now[1] != before[1]:
                        label = "stale, rewritten with identical content, failures NOT listed"
                        stats.stale += 1
                        stats.rewritten += 1
                    else:
                        label = "stale, ignored"
                        stats.stale += 1
        error, reader = parse_report(real, sink) if parse else (None, None)
        status = "stale" if not parse and label != "provenance unknown" else "unknown" if not parse else \
            (("partial" if reader.failing else "error") if error else "ok")
        sink.index.emit({"type": "report", "path": real, "label": label, "status": status, "error": error or reason,
                         "testcases": reader.testcases if reader else None, "failing": reader.failing if reader else None,
                         "skipped": reader.skipped if reader else None})
        if error:
            stats.errors += 1
            kept = " (partial: %d failures kept)" % reader.failing if reader.failing else ""
            sink.warn("WARNING: REPORT_ERROR: %s: %s%s" % (esc(real, path=True), bounded(error), kept))
        elif counter:
            setattr(stats, counter, getattr(stats, counter) + 1)      # a failed report is never counted as current
    for real, before in sorted(snapshot.state.items()):
        if isinstance(before, str) and real not in current.found:
            # the pre-launch snapshot failed and the path was not met again (deleted, or beyond the post-run cap): the
            # error is kept, and the file is never counted as changed, produced, stale or removed
            stats.errors += 1
            sink.warn("WARNING: PROVENANCE_ERROR: %s: %s" % (esc(real, path=True), before))
            sink.index.emit({"type": "report", "path": real, "label": "provenance unknown", "status": "unknown",
                             "error": before, "testcases": None, "failing": None, "skipped": None})
        elif isinstance(before, tuple) and real not in current.found:
            gone = absence(real)
            if gone == "absent":
                stats.removed += 1
                sink.index.emit({"type": "report", "path": real, "label": "removed during run", "status": "removed",
                                 "error": None, "testcases": None, "failing": None, "skipped": None})
            elif gone is not None:
                # only an established absence is a removal: the path cannot be inspected, so that is an explicit error
                stats.errors += 1
                sink.warn("WARNING: PROVENANCE_ERROR: %s: %s" % (esc(real, path=True), gone))
                sink.index.emit({"type": "report", "path": real, "label": "provenance unknown", "status": "unknown",
                                 "error": gone, "testcases": None, "failing": None, "skipped": None})
    sources = [("glob", pattern) for pattern in dict.fromkeys(a.reports)] + [("dir", private_dir)]
    for source in sources:
        before, after = snapshot.discovery.unexamined.get(source, 0), current.unexamined.get(source, 0)
        if before + after:
            kind = "glob" if source[0] == "glob" else "report dir"
            stats.cap_lines.append(line_cap(before + after, kind, source[1]))
            sink.index.emit({"type": "coverage", "kind": "reports_not_examined", "source_kind": source[0],
                             "source": source[1], "passes": {"pre_launch": before, "post_run": after},
                             "total": before + after})
    return stats


def check_writable_dir(directory):
    """Create the directory like --log-dir and prove it is writable with a probe file that is removed again."""
    os.makedirs(directory, exist_ok=True)
    probe = os.path.join(directory, ".run_check-probe-%d" % os.getpid())
    os.close(os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    os.unlink(probe)


def _stem_datetime():
    return datetime.datetime.now()


def log_slug(argv0):
    slug = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(argv0))[:32]
    return slug or "cmd"


def reserve_log(log_dir, slug):
    """Create <log-dir>/<stem>.log exclusively; returns (stem, path, fd). Collisions get the suffix -1 ... -99."""
    now = _stem_datetime()
    base = "%s-%06d-%d-%s" % (now.strftime("%Y%m%d-%H%M%S"), now.microsecond, os.getpid(), slug)
    for n in range(100):
        stem = base if n == 0 else "%s-%d" % (base, n)
        path = os.path.join(log_dir, stem + ".log")
        try:
            return stem, path, os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        except FileExistsError:
            continue
    raise FileExistsError(errno.EEXIST, "no free log name", base)


TYPE_BYTES = 40


def line_summary_error(exc):
    return "SUMMARY ERROR: %s: %s" % (bounded(type(exc).__name__, TYPE_BYTES), bounded(str(exc)))


def line_omitted_emergency(failing, details, warnings, lines):
    return ("omitted: summary not produced; %s failing tests not listed; %s failure details not shown; "
            "%s warning lines not shown; %s log lines not shown; read the log and the failures index"
            % (failing, details, warnings, lines))


def render_emergency(first, log_line, exc, failing, warnings, lines):
    """What is printed when the summary step itself failed: line 1, the log line, the error, and the omission line."""
    def n(total):
        return "unknown" if total is None else str(total)
    text = [first, log_line, line_summary_error(exc), line_omitted_emergency(n(failing), n(failing), n(warnings), n(lines))]
    return "\n".join(text).encode("utf-8") + b"\n"


def worst_stem(slug):
    """The longest stem reserve_log can produce for this slug (10-digit pid, collision suffix -99)."""
    return "20260930-113133-999999-9999999999-%s-99" % slug


def minimum_bytes(a, cmd):
    """Bytes of the mandatory summary lines in their worst case (every optional line present, 20-digit counters,
    the real absolute paths, 80-byte reasons); --max-bytes below this is refused before anything is created."""
    log_dir = os.path.abspath(a.log_dir)
    other = os.path.abspath(a.fallback_dir) if a.fallback_dir else log_dir
    stem = worst_stem(log_slug(cmd[0]))
    log = os.path.join(log_dir, stem + ".log")
    sha = "f" * 64
    reason = "r" * REASON_BYTES
    errno_worst = "E" * ERRNO_WIDTH
    lines = [
        first_line("TIMEOUT (TIMED OUT after %gs, command process group terminated)" % 1.7976931348623157e308, 255,
                   float(WIDE), WIDE, WIDE, ["CAPTURE_PENDING", "CAPTURE_INCOMPLETE"]),
        line_log(log),
        line_capture_incomplete(reason, WIDE, WIDE, WIDE, WIDE, sha, sha),
    ]
    for number in range(2, MAX_SEGMENTS + 1):
        lines.append(line_continued(os.path.join(other, "%s.log.%d" % (stem, number)), WIDE, WIDE, errno_worst, None))
    lines.append(line_storage(WIDE, WIDE, WIDE + ".9", errno_worst, WIDE, WIDE, True, errno_worst))
    sidecars = max(log_dir, other, key=len)
    lines.append(line_pending(WIDE, WIDE, os.path.join(log_dir, stem + ".final-failures.jsonl"),
                              os.path.join(log_dir, stem + ".final-summary.txt"), os.path.join(log_dir, stem + ".complete.json"),
                              os.path.abspath(a.fallback_dir) if a.fallback_dir else None))
    lines.append(line_index(os.path.join(sidecars, stem + ".final-failures.jsonl"), "r" * REASON_BYTES))
    for pattern in dict.fromkeys(a.reports):
        lines.append(line_cap(WIDE, "glob", pattern))
    lines.append(line_cap(WIDE, "report dir", os.path.join(log_dir, stem + ".reports")))
    lines.append(DEDUP_LINE)
    lines.append(line_reports(WIDE, WIDE, WIDE, WIDE, WIDE, WIDE))
    lines.append(line_failing(WIDE, WIDE, WIDE, pending=True))
    lines.append(line_omitted(WIDE, WIDE, WIDE, WIDE, WIDE, WIDE, WIDE, WIDE, WIDE))
    size = lambda rows: sum(len(line.encode("utf-8")) + 1 for line in rows)
    not_run = [first_line("WRAPPER_ERROR (cannot create log: %s (%s): %s)" % ("s" * 60, errno_worst, "p" * ARGV_BYTES), 255,
                          float(WIDE), 0, 0),
               lines[1], "capture: none (command did not run)",
               line_index(os.path.join(log_dir, stem + ".failures.jsonl"), "r" * REASON_BYTES), "omitted: nothing"]
    emergency = [lines[0], lines[1], line_summary_error(type("E" * TYPE_BYTES, (Exception,), {})("r" * REASON_BYTES)),
                 line_omitted_emergency(WIDE, WIDE, WIDE, WIDE)]
    return max(size(lines), size(emergency), size(not_run))


def main(argv=None, out=None):
    a, cmd = parse_arguments(sys.argv[1:] if argv is None else argv)
    needed = minimum_bytes(a, cmd)
    if a.max_bytes < needed:
        sys.stderr.write("run_check.py: error: --max-bytes %d is below the minimum of %d bytes that the mandatory summary "
                         "lines need for this log directory; use --max-bytes %d or more, or a shorter --log-dir path\n"
                         % (a.max_bytes, needed, needed))
        return 2
    watched = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGXFSZ)
    saved = {sig: signal.getsignal(sig) for sig in watched}
    limit = resource.getrlimit(resource.RLIMIT_FSIZE)
    try:
        return run(a, cmd, out)
    finally:
        for sig, handler in saved.items():
            signal.signal(sig, handler)
        try:
            resource.setrlimit(resource.RLIMIT_FSIZE, limit)
        except (ValueError, OSError):
            pass


def run(a, cmd, out):
    t0 = time.time()
    log = log_fd = reports_dir = None
    made = False
    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)      # a file-size error must surface as EFBIG, not kill the wrapper
    try:
        os.makedirs(a.log_dir, exist_ok=True)
        if a.fallback_dir:
            check_writable_dir(a.fallback_dir)
        stem, log, log_fd = reserve_log(a.log_dir, log_slug(cmd[0]))
        log = os.path.abspath(log)           # every persisted path is absolute
        reports_dir = os.path.abspath(os.path.join(a.log_dir, stem + ".reports"))
        os.mkdir(reports_dir, 0o700)
        made = True
        index = Index(os.path.abspath(os.path.join(a.log_dir, stem + ".failures.jsonl")))
        index.emit({"type": "header", "version": 1, "argv": cmd, "log": os.path.abspath(log), "cwd": os.getcwd(),
                    "max_bytes": a.max_bytes, "timeout": a.timeout, "reports": a.reports,
                    "fallback_dir": os.path.abspath(a.fallback_dir) if a.fallback_dir else None})
    except OSError as exc:
        if log_fd is not None:
            os.close(log_fd)
        if made:
            try:
                os.rmdir(reports_dir)
            except OSError:
                pass
        reason = "%s (%s): %s" % (os.strerror(exc.errno or 0), errno_name(exc),
                                 bounded(str(exc.filename or ""), ARGV_BYTES, path=True))
        out_write(out, render_not_run(a, "WRAPPER_ERROR (cannot create log: %s)" % reason, 125, t0,
                                      line_log(os.path.abspath(log)) if log else "log: none (not created)"))
        return 125
    snapshot = Snapshot(a.reports, index, a.max_bytes)
    signals = []
    old_handlers = {sig: signal.signal(sig, lambda number, frame: signals.append(number))
                    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                bufsize=0, env=dict(os.environ, RUN_CHECK_REPORT_DIR=reports_dir),
                                start_new_session=True, restore_signals=True)
        launched = time.monotonic()
    except OSError as exc:
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
        os.close(log_fd)
        os.rmdir(reports_dir)
        if isinstance(exc, FileNotFoundError):
            code, reason = 127, "command not found: %s" % bounded(cmd[0], ARGV_BYTES, path=True)
        else:
            code, reason = 126, "command not executable: %s (%s)" % (bounded(cmd[0], ARGV_BYTES, path=True),
                                                                     errno_name(exc))
        index.emit({"type": "end", "failing_tests": 0, "ran": False})
        index.close()
        out_write(out, render_not_run(a, "WRAPPER_ERROR (%s)" % reason, code, t0, line_log(os.path.abspath(log)),
                                      line_index(index.path, index.reason())))
        return code
    # the command was spawned with the limits of the caller; only the wrapper's own soft limit is raised, before the first write
    soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
    rlimit_error = None
    if soft != hard:
        try:
            resource.setrlimit(resource.RLIMIT_FSIZE, (hard, hard))
        except (ValueError, OSError) as exc:
            rlimit_error = errno_name(exc) if isinstance(exc, OSError) else "EINVAL"
    deadline = launched + a.timeout if a.timeout else None
    capture = Capture(log, log_fd, stem, os.path.abspath(a.log_dir),
                      os.path.abspath(a.fallback_dir) if a.fallback_dir else None, deadline)
    supervisor = Supervisor(proc, capture, deadline, signals)
    supervisor.run()
    for sig, handler in old_handlers.items():
        signal.signal(sig, handler)
    rc = proc.wait()
    status = "COMMAND_EXIT %d" % rc
    if supervisor.reason == "timeout":
        status, rc = "TIMEOUT (TIMED OUT after %gs, command process group terminated)" % a.timeout, 124
    elif supervisor.reason == "signal":
        status = "SIGNAL %d (%s) wrapper interrupted, command process group terminated" % (
            signals[0], signal.Signals(signals[0]).name)
        rc = 128 + signals[0]
    elif rc < 0:
        status = "SIGNAL %d (%s)" % (-rc, signal.Signals(-rc).name)
        rc = 128 - rc
    ctx = Ctx(a=a, cmd=cmd, stem=stem, log_path=os.path.abspath(log), log_dir=os.path.abspath(a.log_dir),
              fallback_dir=os.path.abspath(a.fallback_dir) if a.fallback_dir else None, reports_dir=reports_dir,
              snapshot=snapshot, t0=t0, status=status, rc=rc, leftover=supervisor.leftover, rlimit_error=rlimit_error)
    if supervisor.pending:
        data = hand_off(ctx, capture, proc, index)
    else:
        proc.stdout.close()
        capture.finish()
        emit_capture(index, capture)
        data = safe_summary(ctx, capture, index, "normal")
    out_write(out, data)
    return rc


def render_not_run(a, status, code, t0, log_line, index_line=""):
    """The summary of a run whose command never started (exit 125, 126, 127)."""
    f = Facts()
    f.first = first_line(status, code, time.time() - t0, 0, 0)
    f.log = log_line
    f.capture = "capture: none (command did not run)"
    f.index = index_line
    return render_summary(f, a.max_bytes)


class Ctx:
    """What the analysis and the summary need to know about one invocation (shared by the foreground and a worker)."""

    def __init__(self, **fields):
        self.__dict__.update(fields)

    def base(self):
        return os.path.join(self.log_dir, self.stem)

    def artifact_paths(self, suffix):
        """Where an artifact is created: next to the log, then (only for the worker's sidecars) in --fallback-dir."""
        paths = [self.base() + suffix]
        if self.fallback_dir:
            paths.append(os.path.join(self.fallback_dir, self.stem + suffix))
        return paths


def emit_capture(index, capture, pending=False, readback=None):
    """The 'capture' record (and the unwritten tail) of the failures index."""
    if readback is None:
        readback = "OK" if capture.verified else "MISMATCH"
    index.emit({"type": "capture", "segments": [{"path": s.path, "start": s.start, "bytes": s.bytes, "cause": s.cause,
                                                 "same_filesystem": s.same_fs} for s in capture.segments],
                "received": capture.received, "written": capture.written, "unwritten": capture.unwritten,
                "unwritten_start": capture.unwritten_start, "sha256_stream": capture.sha_stream.hexdigest(),
                "sha256_written": capture.sha_written.hexdigest(), "readback": readback,
                "incomplete": capture.incomplete, "pending": pending,
                "stalls": {"episodes": capture.stall_episodes, "retries": capture.stall_retries,
                           "total_seconds": round(capture.stall_total, 3), "last_errno": capture.stall_errno,
                           "offset": capture.stall_offset, "gave_up_after": capture.stall_gave_up,
                           "fallback_error": capture.fallback_error}})
    if capture.unwritten:
        index.emit({"type": "unwritten_tail", "offset": capture.unwritten_start + capture.unwritten - len(capture.tail),
                    "bytes_b64": base64.b64encode(capture.tail).decode("ascii")})


def summary_markers(capture, mode):
    return ({"pending": ["CAPTURE_PENDING"], "final": ["FINAL"]}.get(mode, [])
            + (["CAPTURE_INCOMPLETE"] if capture.incomplete else []))


def safe_summary(ctx, capture, index, mode, worker_pid=None, readback=None):
    """The summary bytes; if producing them fails the exit code is untouched and an emergency summary is returned."""
    known = {"failing": None, "warnings": None, "lines": None}      # totals established before a failure, else unknown
    try:
        return summarize(ctx, capture, index, mode, known, worker_pid, readback)
    except Exception as exc:
        index.close()            # the analysis did not finish: the file stays without an end record
        first = first_line(ctx.status, ctx.rc, time.time() - ctx.t0,
                           known["lines"] if known["lines"] is not None else "unknown", capture.written,
                           summary_markers(capture, mode))
        return render_emergency(first, line_log(ctx.log_path), exc, known["failing"], known["warnings"], known["lines"])


def detail_block(excerpt):
    """(identity line, detail lines, cut count, log lines shown, shown lines that are error lines) of one listed failing test;
    a row without an excerpt has no detail lines. The body lines of a failure block of the log carry their log line number."""
    number = excerpt.number
    line = "#%d %s [%s]" % (number, esc(excerpt.identity()), excerpt.label())
    if not isinstance(excerpt, Excerpt):
        return line, None, 0, set(), set()
    message = esc(excerpt.message) or ("log line %d" % excerpt.log_line if excerpt.log_line else "")
    cut = 0
    if excerpt.message_cut:
        message += "[...+%d characters]" % excerpt.message_cut
        cut += 1
    detail = ["detail #%d:%s" % (number, " " + message if message else "")]
    numbers, errors = set(), set()
    for text, length, log_line in excerpt.lines:
        raw = raw_bytes(text)[:TAIL_PREVIEW]
        shown, was_cut = cut_line(raw, length)
        detail.append("    " + ("%d: " % log_line if log_line is not None else "") + shown)
        if log_line is not None:
            numbers.add(log_line)
            if ERR.search(raw):
                errors.add(log_line)
        cut += was_cut
    if excerpt.more:
        cut += 1
    return line, detail, cut, numbers, errors


class ShortRead(Exception):
    """A log segment holds fewer bytes than the capture recorded for it (it changed after it was verified)."""

    def __init__(self, segment, read):
        self.segment, self.recorded, self.read = segment.path, segment.bytes, read
        super().__init__("the log ends %d bytes before its recorded length (%d of %d bytes of segment %s)"
                         % (segment.bytes - read, read, segment.bytes, segment.path))


def read_stream(capture):
    """The written stream: exactly `bytes` bytes of every segment, in order, in chunks. A segment that ends early raises
    ShortRead: the analysis must never take a shorter log for a complete one."""
    for segment in capture.segments:
        left = segment.bytes
        with open(segment.path, "rb") as fh:
            while left > 0:
                chunk = fh.read(min(REPORT_CHUNK, left))
                if not chunk:
                    raise ShortRead(segment, segment.bytes - left)
                left -= len(chunk)
                yield chunk


def summarize(ctx, capture, index, mode, known, worker_pid=None, readback=None):
    """Analyse the written stream and render the summary bytes; `known` receives each total as soon as it is established.
    mode: 'normal', 'pending' (the foreground summary of a handed-off capture) or 'final' (the worker's summary)."""
    a = ctx.a
    rc = ctx.rc
    sink = Sink(index, a.max_bytes)
    sink.stream = capture
    stats = process_reports(a, ctx.reports_dir, ctx.snapshot, sink)
    if mode != "pending":
        try:
            os.rmdir(ctx.reports_dir)
        except OSError:
            pass
    analyzer = LogAnalyzer(sink.text_hit)
    analyzer.prefix_end = mode == "pending" or bool(capture.unwritten)     # the stream goes on (or was lost) after the last byte
    try:
        for chunk in read_stream(capture):
            analyzer.feed(chunk)
    except (OSError, ShortRead) as exc:
        # what was verified earlier stays in the index as it was said; this says that the log is no longer what it described
        capture.incomplete = capture.incomplete or "analysis could not read the log: %s" % bounded(str(exc))
        index.emit({"type": "coverage", "kind": "analysis_read_failed", "reason": str(exc),
                    "segment": getattr(exc, "segment", None), "recorded_bytes": getattr(exc, "recorded", None),
                    "read_bytes": getattr(exc, "read", None)})
        raise
    analyzer.finish()
    known["lines"] = analyzer.line_count
    f = Facts()
    f.first = first_line(ctx.status, rc, time.time() - ctx.t0, analyzer.line_count, capture.written,
                         summary_markers(capture, mode))
    f.log = line_log(ctx.log_path)
    sha_written = capture.sha_written.hexdigest()
    if capture.incomplete:
        f.capture = line_capture_incomplete(capture.incomplete, capture.written, capture.received, capture.unwritten,
                                            capture.unwritten_start, sha_written, capture.sha_stream.hexdigest())
    elif mode == "pending":
        f.capture = line_capture_pending("pipe still held by a detached process", capture.written, sha_written, readback)
    else:
        f.capture = line_capture_verified(capture.written, sha_written)
    for segment in capture.segments[1:]:
        f.continued.append(line_continued(segment.path, segment.start, segment.bytes, segment.cause, segment.same_fs))
    if capture.stall_gave_up is not None or capture.stall_episodes:
        f.storage = line_storage(capture.stall_episodes, capture.stall_retries,
                                 "%.1f" % (capture.stall_gave_up if capture.stall_gave_up is not None else capture.stall_total),
                                 capture.stall_errno, capture.stall_offset, capture.unwritten,
                                 capture.stall_gave_up is not None, capture.fallback_error)
    if mode == "pending":
        base = ctx.base()
        f.pending = line_pending(worker_pid, capture.written, base + ".final-failures.jsonl", base + ".final-summary.txt",
                                 base + ".complete.json", ctx.fallback_dir)
    if ctx.leftover == "survived":
        f.warnings.append("WARNING: leftover processes in the command group survived SIGKILL after command exit")
    elif ctx.leftover:
        f.warnings.append("WARNING: leftover processes in the command group were terminated (%s) after command exit"
                          % ctx.leftover)
    if ctx.rlimit_error:
        f.warnings.append("WARNING: the wrapper's file size limit could not be raised (%s); "
                          "a log file larger than the soft limit continues in a new segment" % ctx.rlimit_error)
    if capture.unwritten:
        f.warnings.append("WARNING: analysis covers only the first %d of %d stream bytes (the contiguous written prefix)"
                          % (capture.written, capture.received))
    f.line_count, f.partial, f.err_total = analyzer.line_count, analyzer.partial_lines, analyzer.err_total
    f.warnings += sink.warned.kept
    f.warnings_dropped = sink.warned.dropped
    f.caps = stats.cap_lines
    f.dedup = sink.capped
    if rc == 0 and sink.junit:
        f.warnings.append("WARNING: command exited 0 but reports list %d failing tests" % sink.junit)
    if rc == 0 and sink.text:
        f.warnings.append("WARNING: command exited 0 but the log lists %d text-heuristic failing tests" % sink.text)
    extra = max(0, sink.subtest_observations - len(sink.subtest_cases))         # each subTest failure counts as one
    identified = sink.count + extra
    log_identified = sink.text + len(sink.observed) + extra                    # what the log itself accounts for
    if analyzer.runner_failed > identified:
        f.warnings.append("WARNING: runner reported %d failing tests, identified %d" % (analyzer.runner_failed, identified))
    elif analyzer.runner_failed > log_identified and sink.junit > len(sink.observed):
        f.warnings.append("WARNING: runner reported %d failing tests, the log identifies %d; %d JUnit failing tests are not "
                          "corroborated by the log" % (analyzer.runner_failed, log_identified, sink.junit - len(sink.observed)))
    if a.reports or stats.private_files or stats.errors:
        f.reports = line_reports(stats.changed, stats.produced, stats.stale, stats.rewritten, stats.removed, stats.errors)
    f.failing_total = sink.count
    if sink.count:
        f.failing = line_failing(sink.count, sink.junit, sink.text, pending=mode == "pending")
        for row in sink.rows:
            f.identities.append(detail_block(row))
    elif mode == "pending":
        f.failing = "failing tests: none identified so far"
    elif rc and analyzer.line_count:
        f.failing = "failing tests not identified"
        f.err_rows = [(number,) + cut_line(preview, true_len) for number, preview, true_len in analyzer.err_lines]
        if not capture.unwritten:
            f.tail_rows = [(number,) + cut_line(preview, true_len) + (is_err,)
                           for number, preview, true_len, is_err in analyzer.tail]
    else:
        f.failing = "failing tests: none identified"
    if capture.unwritten:
        f.unwritten = unwritten_rows(capture)
    known["failing"] = sink.count
    known["warnings"] = len(f.warnings) + f.warnings_dropped
    end = {"type": "end", "failing_tests": sink.count, "junit": sink.junit, "text_heuristic": sink.text,
           "duplicates": sink.duplicates, "log_lines": analyzer.line_count, "text_blocks": sink.blocks,
           "unmatched_blocks": sink.unmatched}
    if mode == "pending":
        end.update(pending=True, final_index=ctx.base() + ".final-failures.jsonl")
    index.emit(end)
    index.close()
    f.index = line_index(index.path, index.reason())
    return render_summary(f, a.max_bytes)


def hand_off(ctx, capture, proc, index):
    """A detached process still holds the output pipe after the command group ended: freeze the accounting, verify what
    is on disk, start a background worker that finishes the capture, and summarise the prefix."""
    view = capture.view()
    for segment in capture.segments:
        try:
            _fsync(segment.fd)
        except OSError as exc:
            view.incomplete = view.incomplete or "fsync failed: %s" % errno_name(exc)
    readback = "OK"
    try:
        total, digest = _read_back([segment.path for segment in capture.segments])
        if total != view.written or digest != view.sha_written.hexdigest():
            readback = "MISMATCH"
    except OSError:
        readback = "MISMATCH"
    view.verified = readback == "OK"
    worker_pid, fork_error = None, None
    try:
        pid = os.fork()
    except OSError as exc:
        fork_error = errno_name(exc)
    else:
        if pid == 0:
            run_worker(ctx, capture, proc)
        worker_pid = pid
    proc.stdout.close()
    for segment in capture.segments:
        os.close(segment.fd)
    if fork_error:
        view.incomplete = view.incomplete or "handoff failed: %s" % fork_error
    mode = "normal" if fork_error else "pending"
    emit_capture(index, view, pending=mode == "pending", readback=readback)
    return safe_summary(ctx, view, index, mode, worker_pid, readback)


def close_inherited(keep):
    limit = min(resource.getrlimit(resource.RLIMIT_NOFILE)[0], 65536)
    previous = 0
    for fd in sorted(keep):
        if fd > previous:
            os.closerange(previous, fd)
        previous = fd + 1
    os.closerange(previous, limit)


def write_exclusive(paths, data):
    """Creates the first usable path exclusively and writes `data` to it; returns (path or None, error text or None).
    A file that could only be written in part is removed again (a torn sidecar or summary must not exist) and the next
    path is tried; when nothing can be written the artifact is missing, never torn."""
    error = None
    for path in paths:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        except OSError as exc:
            error = errno_name(exc)
            continue
        failed = None
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        except OSError as exc:
            failed = errno_name(exc)
        finally:
            os.close(fd)
        if failed is None:
            return path, None
        error = failed
        try:
            os.unlink(path)
        except OSError:
            error += "; a torn file remains at %s" % path
    return None, error


def run_worker(ctx, capture, proc):
    """The background worker (a forked copy of the wrapper): detach, keep draining the pipe with the same capture engine
    until EOF, then write the final index, the final summary and, last, the completion sidecar."""
    code = 1
    try:
        os.setsid()
        null = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(null, fd)
        pipe = proc.stdout.fileno()
        close_inherited({0, 1, 2, pipe} | {segment.fd for segment in capture.segments})
        for sig in (signal.SIGHUP, signal.SIGINT, signal.SIGPIPE):
            signal.signal(sig, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        while not capture.pump(pipe, TICK):
            pass
        capture.finish()
        finish_worker(ctx, capture)
        code = 0
    except BaseException:
        code = 1
    finally:
        os._exit(code)


def finish_worker(ctx, capture):
    final = None
    error = None
    for path in ctx.artifact_paths(".final-failures.jsonl"):
        try:
            final = Index(path)
            break
        except OSError as exc:
            error = errno_name(exc)
    if final is None:
        final = Index(ctx.artifact_paths(".final-failures.jsonl")[0], error=error)
    final.emit({"type": "header", "version": 1, "final": True, "argv": ctx.cmd, "log": ctx.log_path, "cwd": os.getcwd(),
                "max_bytes": ctx.a.max_bytes, "timeout": ctx.a.timeout, "reports": ctx.a.reports,
                "fallback_dir": ctx.fallback_dir})
    emit_capture(final, capture)
    data = safe_summary(ctx, capture, final, "final")
    summary_path, summary_error = write_exclusive(ctx.artifact_paths(".final-summary.txt"), data)
    complete = {"version": 1, "status": ctx.status, "exit_code": ctx.rc,
                "complete": capture.incomplete is None and capture.verified is True,
                "total_bytes": capture.received, "written_bytes": capture.written,
                "sha256_stream": capture.sha_stream.hexdigest(), "sha256_written": capture.sha_written.hexdigest(),
                "readback": "OK" if capture.verified else "MISMATCH", "incomplete": capture.incomplete,
                "segments": [{"path": s.path, "start": s.start, "bytes": s.bytes, "same_filesystem": s.same_fs}
                             for s in capture.segments],
                "unwritten": capture.unwritten,
                "stalls": {"episodes": capture.stall_episodes, "retries": capture.stall_retries,
                           "total_seconds": round(capture.stall_total, 3), "last_errno": capture.stall_errno,
                           "gave_up_after": capture.stall_gave_up},
                "artifacts": {"final_failures": final.path if final.created else None,
                              "final_summary": summary_path, "complete_json": None},
                "outcomes": {"final_failures": "written" if final.error is None else "error: %s" % final.reason(),
                             "final_summary": "written" if summary_error is None else "error: %s" % summary_error}}
    for path in ctx.artifact_paths(".complete.json"):         # the record names the path it is written to
        complete["artifacts"]["complete_json"] = path
        written, _ = write_exclusive([path], (json.dumps(complete, ensure_ascii=True, indent=1) + "\n").encode("ascii"))
        if written:
            break


RESULT_PREFIXES = (b"log: ", b"capture: ", b"CAPTURE_PENDING:", b"failures index: ", b"SUMMARY ERROR: ")


def result_lines(data):
    """The lines of a summary that state what the run produced: line 1 (status and exit code), the log, capture, pending and
    failures index lines and, when the summary itself failed, the error line. They are already escaped and bounded."""
    lines = data.split(b"\n")
    return [lines[0]] + [line for line in lines[1:] if line.startswith(RESULT_PREFIXES)]


def out_write(out, data):
    """Write the summary bytes to `out` (a binary file object) or to the binary buffer of stdout. When stdout cannot take
    them (closed, a broken pipe, a full device) the exit code stays the command's own and the bytes that are still buffered
    are dropped, so that nothing fails a second time when the interpreter flushes stdout at shutdown. stderr is then the
    caller's only result: one line says that the summary was not delivered, followed by the result lines of the summary
    itself (status and exit code, log, capture, pending, failures index), which say what was and was not produced."""
    try:
        if out is None:
            out = getattr(sys.stdout, "buffer", None)
            if out is None:
                sys.stdout.write(data.decode("utf-8", "replace"))
                sys.stdout.flush()
                return
        out.write(data)
        out.flush()
    except (OSError, ValueError, AttributeError) as exc:       # AttributeError: no stdout at all (descriptor 1 was closed)
        try:
            null = os.open(os.devnull, os.O_WRONLY)
            os.dup2(null, sys.stdout.fileno())
            os.close(null)
        except (OSError, ValueError, AttributeError):
            pass
        try:
            message = ("run_check.py: the summary could not be written to stdout (%s: %s); its result lines follow\n"
                       % (type(exc).__name__, exc)).encode("utf-8", "replace")
            message += b"".join(line + b"\n" for line in result_lines(data))
            stderr = getattr(sys.stderr, "buffer", None)
            if stderr is None:
                sys.stderr.write(message.decode("utf-8", "replace"))
                sys.stderr.flush()
            else:
                stderr.write(message)
                stderr.flush()
        except (OSError, ValueError, AttributeError):
            pass


if __name__ == "__main__":
    sys.exit(main())
