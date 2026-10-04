#!/usr/bin/env python3
"""Content-free council telemetry: the writer council.sh calls through one bash wrapper (tm_call).

Every record is one JSON line with the envelope schema/ev/host/run/inv/seq/ts and only the fields the
registry below allows for its event: indices, enums, counts, durations, versions and sanitized model or
effort names. No prompt, task text, post, note, path, project name, session id, host name or user name is
ever accepted. Unknown applicable values are null; inapplicable fields are omitted.

  init                                    -> "<host> <new run id>"
  begin   COMMON --command start|resume (--config FILE | --state FILE) --history full|partial|auto
          --gap true|false [k=v ...]      -> run_start plus one member event per member; prints the last seq
  event   COMMON [--state FILE] [--member I] [--task] [--mapper] [--mapper-usage] [--mapper-recover]
          [--launch-key] [--stale] [--evidence-stdin] [--mapdir DIR] [--no-response]
          [--no-captures] EV [k=v ...]    -> appends one event; prints its seq (nothing when there is nothing to close)
  nextseq --run DIR [--rid R --state FILE] --floor N
                                          -> the next free seq of the run (reserved in state.json when it exists)
  resume  --run DIR --rid NEW             -> "<run> <inv> <floor> auto|partial", stored in state.json first
Once state.json exists, it must carry this run's .telemetry {run, inv, seq} (else EINVAL), and every seq is
reserved there before its line is written.
  ship    --run DIR --host H --rid R --inv N --seq S --exit-code N [--status S | --state FILE]
                                          -> the unshipped records the schema accepts for run R (re-serialized;
                                             anything else is skipped and passed by the cursor) plus one
                                             run_summary, one write to the ledger
  COMMON = --run DIR --host H --rid R --inv N --floor N

On any failure: exit 3 and print only an errno name (EINVAL for an invalid event, ETIMEDOUT for a held
lock, EIO for a short write or anything unclassified) - never a path, a value or a traceback.
"""
import errno
import fcntl
import hashlib
import json
import math
import os
import re
import signal
import stat
import subprocess
import sys
import time
import uuid

sys.dont_write_bytecode = True

SCHEMA = 1
LOCK_TIMEOUT = 5.0
LOCK_POLL = 0.05
IOREG_TIMEOUT = 5.0
SALT = "council-telemetry-v1:"
EVENTS_FILE = "telemetry.jsonl"
CURSOR_FILE = "telemetry.shipped"
MODEL_RE = re.compile(r"[A-Za-z0-9._/:@~-]{1,100}")
VERSION_RE = re.compile(r"[0-9A-Za-z.+-]{1,40}")
HOST_RE = re.compile(r"[0-9a-f]{16}|unknown")
RUN_RE = re.compile(r"[0-9a-f]{32}")
UUID_RE = re.compile(r"[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}")
MAX_INT = 2 ** 53 - 1   # the largest integer every JSON reader keeps exactly; anything larger is invalid
# T4: fixed case-insensitive patterns, applied only to provider-origin evidence; only the class is kept.
QUOTA_RE = re.compile(r"quota|rate.?limit|exceeded your current", re.I)
AUTH_RE = re.compile(r"invalid authentication|unauthori[sz]ed|\b401\b|api key", re.I)
TOKEN_KEYS = ("input", "output", "cache_read", "cache_write", "reasoning", "total")
CLASSES = ("group_survived", "timeout", "launch_failed", "quota", "auth", "cli_error", "empty_output", "no_valid_json_tail")


class Invalid(Exception):
    """An event that does not fit the registry (EINVAL)."""


class Failure(Exception):
    """A write that did not happen; args[0] is the errno name to report."""


# ------------------------------------------------------------------ registry ----
def enum(*values):
    return ("enum", frozenset(values))


INT, NUM, BOOL, MODEL, VER, TOKENS = "int", "num", "bool", "model", "ver", "tokens"
STATUS = enum("created", "running", "questions", "failed", "done", "unknown")
STEP = enum("plan", "exec", "ratify", "handover", "map", "replace")
MAP_STAGE = enum("prepare", "session", "dispatch", "wait", "recover", "validate", "capture", "done")
MEMBER = {"member_index": INT, "kind": enum("opencode", "claude"), "mode": enum("read", "edit"), "executor": BOOL,
          "model": MODEL, "effort": MODEL, "gen": INT}
TASK = {"task_index": INT, "split_child": BOOL}
STEPS = {"step": STEP, "round": INT}
CALL = dict(MEMBER, **TASK, **STEPS, attempt=INT)
VERSIONS = {"skill_version": VER, "claude_version": VER, "opencode_version": VER}
COUNTS = {"members": INT, "max_rounds": INT, "tasks": INT}
EVENTS = {
    "run_start": dict(VERSIONS, **COUNTS, command=enum("start", "resume"), status=STATUS, timeout_s=NUM, max_turns=INT,
                      handover_at=NUM, style=enum("normal", "lite", "caveman", "ultra"), executor_present=BOOL,
                      map_code=BOOL, history=enum("full", "partial"), gap=BOOL),
    "member": dict(MEMBER, handover_at=NUM, handover_override=BOOL, style=enum("normal", "lite", "caveman", "ultra"),
                   style_override=BOOL),
    "run_end": dict(VERSIONS, **COUNTS, exit_code=INT, status=STATUS, task_index=INT, step=STEP, round=INT, duration_s=INT),
    "call_start": dict(CALL, launch_key=INT),
    "launched": dict(CALL, call_seq=INT, launch_key=INT),
    "call_end": dict(CALL, call_seq=INT, outcome=enum("ok", "failed", "killed"), failure_class=enum(*CLASSES),
                     duration_s=INT, provider_duration_s=NUM, exit_code=INT, wait_exit_code=INT, result_exit_code=INT,
                     tokens=TOKENS, cost_usd=NUM,
                     ctx_used=INT, ctx_limit=INT),
    "retry": dict(CALL, call_seq=INT, failure_class=enum(*CLASSES)),
    "timeout": dict(CALL, call_seq=INT, timeout_s=NUM),
    "kill_group": dict(CALL, call_seq=INT, reason=enum("timeout", "leftover", "launch_failed", "signal", "stale", "gate",
                                                       "handover", "cleanup"),
                       result=enum("stopped", "survived", "refused", "unknown")),
    "handover": dict(MEMBER, **TASK, **STEPS, handover_seq=INT, type=enum("threshold", "replace"), stage=enum("start", "end"),
                     ctx_used=INT, ctx_limit=INT, new_gen=INT, note_outcome=enum("ok", "failed", "killed")),
    "gate": {"member_index": INT, "handover_seq": INT, "wait_s": INT, "result": enum("released", "timeout", "disabled"),
             "interrupted": BOOL},
    "questions": dict(TASK, count=INT, source=enum("member", "mapper", "contract")),
    "votes": dict(TASK, **STEPS, vote_propose=INT, vote_agree=INT, vote_disagree=INT, vote_question=INT, vote_done=INT,
                  failed_members=INT, converted=INT, replay=BOOL),
    "outcome": dict(TASK, milestone=enum("consensus", "unresolved", "ratified", "unratified"), round=INT),
    "mapper_start": dict(TASK, kind=enum("opencode", "claude"), model=MODEL, effort=MODEL),
    "mapper": dict(TASK, op_seq=INT, mapper_seq=INT, kind=enum("opencode", "claude"), model=MODEL, effort=MODEL,
                   stage=MAP_STAGE, status=enum("complete", "partial", "unavailable", "aborted"), proposed_count=INT, validated_count=INT,
                   captured_count=INT, capture_total=INT, repairs_count=INT, repaired=BOOL, duration_s=INT,
                   tokens=TOKENS, cost_usd=NUM),
    "reused": dict(MEMBER, **TASK, **STEPS),
    "replace": dict(MEMBER, old_kind=enum("opencode", "claude"), old_model=MODEL, old_effort=MODEL),
    "stale_replay": dict(TASK, **STEPS, where=enum("in_run", "resume")),
    "mapper_review": dict(TASK, action=enum("keep", "split"), review_s=NUM),
    "split_failed": dict(TASK),
    "run_summary": dict(COUNTS, last_event_seq=INT, exit_code=INT, status=STATUS, history=enum("full", "partial"), gap=BOOL,
                        calls=INT, ok=INT, failed=INT, killed=INT, outcome_unknown=INT, incomplete=INT, retries=INT, timeouts=INT,
                        tokens_known=INT, tokens_unknown_calls=INT, cost_usd_known=NUM, cost_unknown_calls=INT,
                        handovers=INT, questions=INT, questions_unknown=INT, rounds_observed=INT, consensus=INT, unresolved=INT, ratified=INT,
                        unratified=INT),
}
ENVELOPE = ("schema", "ev", "host", "run", "inv", "seq", "ts")


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= MAX_INT


def check_value(kind, v):
    """True when v (already decoded) is a valid value of the registry type kind; None is always valid."""
    if v is None:
        return True
    if kind == INT:
        return is_int(v)
    if kind == NUM:   # an int is bounded before any float conversion: a 400-digit integer is invalid, never an overflow
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return False
        if isinstance(v, int):
            return 0 <= v <= MAX_INT
        return math.isfinite(v) and 0 <= v <= MAX_INT
    if kind == BOOL:
        return isinstance(v, bool)
    if kind == MODEL:
        return isinstance(v, str) and (v == "other" or MODEL_RE.fullmatch(v) is not None)
    if kind == VER:
        return isinstance(v, str) and VERSION_RE.fullmatch(v) is not None
    if kind == TOKENS:
        return isinstance(v, dict) and set(v) == set(TOKEN_KEYS) and all(x is None or is_int(x) for x in v.values())
    if isinstance(kind, tuple) and kind[0] == "enum":
        return isinstance(v, str) and v in kind[1]
    return False


def validate(rec):
    """Raises Invalid with a category: 'schema', 'event' or 'invalid'. Used by the writer and the report."""
    if not isinstance(rec, dict):
        raise Invalid("invalid")
    if type(rec.get("schema")) is not int or rec["schema"] != SCHEMA:
        raise Invalid("schema")
    fields = EVENTS.get(rec["ev"]) if isinstance(rec.get("ev"), str) else None
    if fields is None:
        raise Invalid("event")
    if not (isinstance(rec.get("host"), str) and HOST_RE.fullmatch(rec["host"])
            and isinstance(rec.get("run"), str) and RUN_RE.fullmatch(rec["run"])
            and is_int(rec.get("inv")) and rec["inv"] >= 1 and is_int(rec.get("seq")) and rec["seq"] >= 1
            and is_int(rec.get("ts"))):
        raise Invalid("invalid")
    for key, value in rec.items():
        if key in ENVELOPE:
            continue
        if key not in fields or not check_value(fields[key], value):
            raise Invalid("invalid")


def sanitize_model(v):
    if v is None:
        return None
    v = str(v)
    return v if MODEL_RE.fullmatch(v) else "other"


def sanitize_version(v):
    if v is None:
        return None
    v = str(v).strip()
    return v if VERSION_RE.fullmatch(v) else None


def coerce(kind, text):
    """One k=v value from the shell -> its typed value, or Invalid."""
    if text in ("", "null"):
        return None
    if kind == INT:
        if re.fullmatch(r"\d{1,16}", text) and int(text) <= MAX_INT:
            return int(text)
        raise Invalid(text)
    if kind == NUM:
        if re.fullmatch(r"\d{1,16}", text) and int(text) <= MAX_INT:
            return int(text)
        try:
            v = float(text)
        except ValueError:
            raise Invalid(text)
        if not math.isfinite(v) or v < 0 or v > MAX_INT:
            raise Invalid(text)
        return v
    if kind == BOOL:
        if text in ("true", "false"):
            return text == "true"
        raise Invalid(text)
    if kind == MODEL:
        return sanitize_model(text)
    if kind == VER:
        return sanitize_version(text)
    if isinstance(kind, tuple) and kind[0] == "enum":
        if text in kind[1]:
            return text
        raise Invalid(text)
    raise Invalid(text)


def parse_fields(ev, pairs):
    fields = EVENTS[ev]
    out, tokens = {}, None
    for pair in pairs:
        if "=" not in pair:
            raise Invalid(pair)
        key, text = pair.split("=", 1)
        if key.startswith("tokens."):
            sub = key[len("tokens."):]
            if "tokens" not in fields or sub not in TOKEN_KEYS:
                raise Invalid(key)
            tokens = tokens or {k: None for k in TOKEN_KEYS}
            tokens[sub] = coerce(INT, text)
            continue
        if key not in fields or fields[key] == TOKENS:
            raise Invalid(key)
        out[key] = coerce(fields[key], text)
    if tokens is not None:
        out["tokens"] = tokens
    return out


# ------------------------------------------------------------------- files ----
def errno_name(exc):
    return errno.errorcode.get(getattr(exc, "errno", None) or errno.EIO, "EIO")


def check_ancestors(path):
    """Refuse a path reached through a symlink anywhere on the way (ELOOP), whoever owns it: every directory
    component is inspected as written, never lexically normalized (a symlink followed by '..' is still traversed).
    The path itself is checked by its opener."""
    parts = (path if os.path.isabs(path) else os.path.join(os.getcwd(), path)).split(os.sep)
    cur = os.sep
    for part in parts[1:-1]:
        if part in ("", "."):
            continue
        cur = os.path.join(cur, part)
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            return
        if stat.S_ISLNK(st.st_mode):
            raise OSError(errno.ELOOP, "symlinked ancestor")


def tighten(fd_or_path, st, mode):
    """A telemetry-owned file or directory of ours loses every permission bit outside its required mode
    (0700/0600): group and other access is removed; an owner restriction the user set is kept."""
    cur = stat.S_IMODE(st.st_mode)
    if st.st_uid == os.geteuid() and cur & ~mode:
        (os.fchmod if isinstance(fd_or_path, int) else os.chmod)(fd_or_path, cur & mode)


def open_regular(path, flags, own=True):
    """Open a regular file without following a symlink anywhere on its path. A telemetry-owned file (own) is
    tightened to 0600; operational state (own=False) is only read and keeps its permissions."""
    check_ancestors(path)
    try:
        st = os.lstat(path)
        if stat.S_ISDIR(st.st_mode):
            raise OSError(errno.EISDIR, "a directory")
        if not stat.S_ISREG(st.st_mode) and not stat.S_ISLNK(st.st_mode):
            raise OSError(errno.EINVAL, "not a regular file")
    except FileNotFoundError:
        pass
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, 0o600)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(errno.EINVAL, "not a regular file")
        fcntl.fcntl(fd, fcntl.F_SETFL, fcntl.fcntl(fd, fcntl.F_GETFL) & ~os.O_NONBLOCK)
        if own:
            tighten(fd, st, 0o600)
    except BaseException:
        os.close(fd)
        raise
    return fd


def own_dir(path, parents=False):
    """A telemetry-owned directory: created 0700 when missing, tightened to 0700 when ours, never a symlink."""
    check_ancestors(os.path.join(path, "x"))
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        if parents:   # missing ancestors are telemetry-owned: each created 0700; existing ones are left alone
            missing, cur = [], os.path.dirname(path)
            while cur and not os.path.lexists(cur):
                missing.append(cur)
                cur = os.path.dirname(cur)
            for d in reversed(missing):
                try:
                    os.mkdir(d, 0o700)
                    os.chmod(d, 0o700)
                except FileExistsError:
                    pass
        try:
            os.mkdir(path, 0o700)
            os.chmod(path, 0o700)
        except FileExistsError:
            pass
        st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode):
        raise OSError(errno.ELOOP, "symlinked directory")
    if not stat.S_ISDIR(st.st_mode):
        raise OSError(errno.ENOTDIR, "not a directory")
    tighten(path, st, 0o700)


def lock(fd):
    deadline = time.monotonic() + LOCK_TIMEOUT
    while True:
        try:
            fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if exc.errno not in (errno.EAGAIN, errno.EACCES):
                raise
        if time.monotonic() >= deadline:
            raise Failure("ETIMEDOUT")
        time.sleep(LOCK_POLL)


def read_all(fd):
    size = os.fstat(fd).st_size
    chunks, off = [], 0
    while off < size:
        b = os.pread(fd, min(1 << 20, size - off), off)
        if not b:
            break
        chunks.append(b)
        off += len(b)
    return b"".join(chunks)


def unique_keys(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ValueError("duplicate key")
        out[k] = v
    return out


def reject_constant(name):
    raise ValueError("non-finite constant")


def loads(line):
    return json.loads(line, object_pairs_hook=unique_keys, parse_constant=reject_constant)


def parse_lines(data):
    """-> (valid records, their max seq). A damaged line is skipped: it is never a trusted seq source (each seq
    is reserved in state.json before its line is written)."""
    recs, top = [], 0
    for raw in data.split(b"\n"):
        if not raw.strip():
            continue
        try:
            rec = loads(raw.decode("utf-8"))
            validate(rec)
        except (ValueError, UnicodeDecodeError, RecursionError, OverflowError, TypeError, Invalid):
            continue
        recs.append(rec)
        top = max(top, rec["seq"])
    return recs, top


def dumps(rec):
    return json.dumps(rec, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def write_once(fd, data):
    n = os.write(fd, data)
    if n != len(data):
        raise Failure("EIO")


def separator(fd):
    size = os.fstat(fd).st_size
    return b"\n" if size and os.pread(fd, 1, size - 1) != b"\n" else b""


# ---------------------------------------------------------------- identity ----
def platform_uuid():
    try:
        p = subprocess.Popen(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"], stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return None
    try:
        out, _ = p.communicate(timeout=IOREG_TIMEOUT)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        p.communicate()
        return None
    if p.returncode != 0:
        return None
    m = re.search(rb'"IOPlatformUUID"\s*=\s*"([^"\n]*)"', out)
    if not m:
        return None
    value = m.group(1).decode("ascii", "replace").upper()
    return value if UUID_RE.fullmatch(value) else None


def host_seed():
    """The fallback identity: a random 128-bit seed in ~/.config/council-telemetry/host-seed (0600)."""
    try:
        base = os.path.join(os.path.expanduser("~"), ".config")
        check_ancestors(os.path.join(base, "x"))
        if not os.path.isdir(base):
            os.makedirs(base, 0o700, exist_ok=True)
        d = os.path.join(base, "council-telemetry")
        own_dir(d)
        f = os.path.join(d, "host-seed")
        for _ in range(2):
            try:
                fd = open_regular(f, os.O_RDONLY)
            except FileNotFoundError:
                value = os.urandom(16).hex()
                try:
                    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
                except FileExistsError:
                    continue
                try:
                    write_once(fd, (value + "\n").encode())
                finally:
                    os.close(fd)
                return value
            try:
                value = read_all(fd).decode("ascii", "replace").strip()
            finally:
                os.close(fd)
            return value if re.fullmatch(r"[0-9a-f]{32}", value) else None
    except (OSError, Failure):
        return None
    return None


def host_identity():
    secret = platform_uuid() or host_seed()
    if not secret:
        return "unknown"
    return hashlib.sha256((SALT + secret).encode()).hexdigest()[:16]


# ------------------------------------------------------------------- state ----
def load_json(path):
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def member_dims(state, i):
    try:
        m = state["members"][i]
    except (TypeError, KeyError, IndexError):
        return {"member_index": i}
    executor = (state.get("config") or {}).get("executor")
    return {"member_index": i, "kind": m.get("kind") if m.get("kind") in ("opencode", "claude") else None,
            "mode": m.get("mode") if m.get("mode") in ("read", "edit") else None,
            "executor": executor is not None and m.get("id") == executor,
            "model": sanitize_model(m.get("model")), "effort": sanitize_model(m.get("effort")),
            "gen": m.get("gen") if is_int(m.get("gen")) else None}


def task_dims(state):
    idx = (state or {}).get("task_idx")
    if not is_int(idx):
        return {"task_index": None}
    try:
        task = state["config"]["tasks"][idx]
        child = isinstance(task, dict) and task.get("map_parent") is not None
    except (TypeError, KeyError, IndexError):
        child = None
    return {"task_index": idx, "split_child": child}


def mapper_dims(state):
    mp = ((state or {}).get("config") or {}).get("map_prepass") or {}
    kind = mp.get("kind") or "opencode"
    return {"kind": kind if kind in ("opencode", "claude") else None,
            "model": sanitize_model(mp.get("model") or "google/gemini-3.8-flash"),
            "effort": sanitize_model(mp.get("effort") or "medium")}


def current_task_id(state):
    try:
        task = state["config"]["tasks"][state["task_idx"]]
        return task.get("map_parent") or task.get("id")
    except (TypeError, KeyError, IndexError, AttributeError):
        return None


def mapper_usage(state):
    """tokens/cost of the mapper session as the run already recorded it (one call per mapper session)."""
    tid = current_task_id(state)
    usage = (((state or {}).get("map_prepasses") or {}).get(tid) or {}).get("usage")
    if not isinstance(usage, dict):
        return {k: None for k in TOKEN_KEYS}, None
    tokens = {k: usage.get(k) if is_int(usage.get(k)) else None for k in ("input", "output", "cache_read", "cache_write", "reasoning")}
    tokens["total"] = sum(tokens.values()) if all(v is not None for v in tokens.values()) else None
    cost = usage.get("cost")
    cost = cost if check_value(NUM, cost) else None
    return tokens, cost


def map_status(v):
    return v if v in ("created", "running", "questions", "failed", "done") else "unknown"


PHASE_STEP = {"plan": "plan", "exec": "exec", "ratify": "ratify", "map": "map", "map_review": "map", "mapper_confirm": "map"}


def question_source(state):
    phase = state.get("phase")
    ids = [q.get("id") for q in (state.get("pending_questions") or []) if isinstance(q, dict)]
    if phase in ("mapper_confirm", "map_review"):
        return "mapper"
    if phase == "split_invalid" or "invalid-execution-phase" in ids:
        return "contract"
    return "member"


def mapper_counts(mapdir, max_bytes, no_response, no_captures):
    out = {"proposed_count": None, "validated_count": None, "captured_count": None, "capture_total": None,
           "repairs_count": None, "repaired": None}
    resp = os.path.join(mapdir, "response.txt")
    if not no_response and os.path.isfile(resp):
        out["proposed_count"] = proposed_count(resp, max_bytes)
    validation = load_json(os.path.join(mapdir, "validation.json"))
    result = validation.get("result") if isinstance(validation, dict) else None
    if isinstance(result, dict):
        if isinstance(result.get("candidates"), list):
            out["validated_count"] = len(result["candidates"])
        repairs = result.get("repairs") or []
        if isinstance(repairs, list):
            out["repairs_count"] = len(repairs)
            out["repaired"] = len(repairs) > 0
    caps = None if no_captures else load_json(os.path.join(mapdir, "capture-results.json"))
    if isinstance(caps, dict) and isinstance(caps.get("results"), list):
        out["capture_total"] = len(caps["results"])
        out["captured_count"] = sum(1 for r in caps["results"] if isinstance(r, dict) and r.get("status") == "ok")
    return out


def proposed_count(path, max_bytes):
    """Length of the mapper's candidates array before validation, decoded exactly as the validator decodes it
    (strict JSON, then its narrow syntax repair). Anything it cannot decode is unknown."""
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    try:
        import council_map_prepass as mp
        raw = open(path, "rb").read()
        if len(raw) > max_bytes or not raw.strip():
            return None
        stripped = raw.decode("utf-8").strip()
        if stripped.startswith("```json") and stripped.endswith("```"):
            stripped = stripped[len("```json"):-3].strip()
        try:
            obj = json.loads(stripped, object_pairs_hook=mp._unique_keys, parse_constant=mp._reject_constant)
        except json.JSONDecodeError:
            fixed, _ = mp._repair_json_syntax(stripped)
            obj = json.loads(fixed, object_pairs_hook=mp._unique_keys, parse_constant=mp._reject_constant)
    except Exception:
        return None
    cands = obj.get("candidates") if isinstance(obj, dict) else None
    return len(cands) if isinstance(cands, list) else None


# ---------------------------------------------------------- state metadata ----
def read_state(path):
    """-> (state, raw bytes) of state.json read without following a symlink, or (None, None) before it exists."""
    if not path:
        return None, None
    try:
        fd = open_regular(path, os.O_RDONLY, own=False)
    except FileNotFoundError:
        return None, None
    try:
        raw = read_all(fd)
    finally:
        os.close(fd)
    try:
        state = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise Invalid("state")
    if not isinstance(state, dict):
        raise Invalid("state")
    return state, raw


def trusted_meta(state, rid=None):
    """state.telemetry when it is exactly {run, inv, seq} with valid values (and of run rid), else None."""
    t = state.get("telemetry") if isinstance(state, dict) else None
    if not (isinstance(t, dict) and sorted(t) == ["inv", "run", "seq"] and isinstance(t["run"], str)
            and RUN_RE.fullmatch(t["run"]) and is_int(t["inv"]) and t["inv"] >= 1 and is_int(t["seq"])):
        return None
    if rid is not None and t["run"] != rid:
        return None
    return {"run": t["run"], "inv": t["inv"], "seq": t["seq"]}


def write_meta(path, raw, meta):
    """Replace state.json with .telemetry={run,inv,seq} set, every other byte formatted as the run's own jq
    writes leave it, through an exclusively created temporary file in the same directory."""
    p = subprocess.run(["jq", "--argjson", "t", dumps(meta), ".telemetry=$t"], input=raw, stdout=subprocess.PIPE,
                       stderr=subprocess.DEVNULL)
    if p.returncode != 0 or not p.stdout:
        raise Failure("EIO")
    check_ancestors(path)
    mode = stat.S_IMODE(os.lstat(path).st_mode)
    tmp = "%s.tm-%d-%s" % (path, os.getpid(), os.urandom(4).hex())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        try:
            os.fchmod(fd, mode)
            write_once(fd, p.stdout)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def state_for(opts):
    """The run's own state (and its trusted metadata) for an event; another run's or an untrusted one is EINVAL."""
    state, raw = read_state(opts.get("state"))
    if state is None:
        return None, None, None
    meta = trusted_meta(state, opts.get("rid"))
    if meta is None:
        raise Invalid("state")
    return state, raw, meta


def reserve(opts, raw, meta, seq):
    """Reserve seq in state.json before its line is published: a later invocation never reuses it."""
    if meta is not None and seq > meta["seq"]:
        write_meta(opts["state"], raw, dict(meta, seq=seq))


def launch_key(state, rid, opts):
    """An integer bound to one actual launch, from the operational evidence the run itself keeps: a member's
    recorded process group and its start time (inflight.pgid/started), or the mapper operation's session. Only a
    salted hash is recorded (never the pid, start time or session id); None when that evidence is missing."""
    if opts.get("mapper"):
        entry = ((state or {}).get("map_prepasses") or {}).get(current_task_id(state)) or {}
        session = entry.get("session") if isinstance(entry, dict) else None
        material = "map:%s" % session if isinstance(session, str) and session else None
    else:
        try:
            inflight = state["members"][int(opts["member"])]["inflight"]
            pgid, started = inflight["pgid"], inflight["started"]
        except (TypeError, KeyError, IndexError, ValueError):
            return None
        material = "member:%d:%s" % (pgid, started) if is_int(pgid) and isinstance(started, str) and started.strip() else None
    if material is None:
        return None
    return int(hashlib.sha256(("%s:%s" % (rid, material)).encode()).hexdigest()[:13], 16)


def stale_launch(mine, starts, ended, member, gen, key):
    """The one open call a leftover process found on resume belongs to, else None: its recorded launch key (and
    generation) must match exactly one launch of this member; time proximity alone never links."""
    if key is None:
        return None
    hits = [r for r in mine if r["ev"] == "launched" and r.get("launch_key") == key]
    if len(hits) != 1:
        return None
    h = hits[0]
    if h.get("member_index") != member or gen is None or h.get("gen") != gen or h.get("call_seq") not in starts \
            or h["call_seq"] in ended:
        return None
    return h["call_seq"]


def mapper_operation(mine, state, task_index, rid, opts):
    """-> (op_seq, dispatch seq) of the mapper operation whose dispatch recorded the state's session, else
    (None, None): a re-prepared operation (another session) links nothing."""
    key = launch_key(state, rid, opts)
    if key is None:
        return None, None
    calls = [s for s in mine if s["ev"] == "call_start" and s.get("step") == "map" and s.get("launch_key") == key]
    if len(calls) != 1 or calls[0].get("task_index") != task_index:
        return None, None
    d = calls[0]
    ops = [o["seq"] for o in mine if o["ev"] == "mapper_start" and o.get("task_index") == task_index
           and o["inv"] == d["inv"] and o["seq"] < d["seq"]]
    return (max(ops) if ops else None), d["seq"]


# ------------------------------------------------------------------ events ----
class Run:
    """The run's own event file, locked for the whole read-modify-append."""

    def __init__(self, run_dir, create=True):
        flags = os.O_RDWR | os.O_APPEND | (os.O_CREAT if create else 0)
        self.fd = open_regular(os.path.join(run_dir, EVENTS_FILE), flags)
        try:
            lock(self.fd)
            self.data = read_all(self.fd)
            self.records, self.top = parse_lines(self.data)
        except BaseException:
            os.close(self.fd)
            raise

    def close(self):
        os.close(self.fd)

    def mine(self, rid):
        return [r for r in self.records if r.get("run") == rid]

    def append(self, recs):
        for r in recs:
            validate(r)
        write_once(self.fd, separator(self.fd) + "".join(dumps(r) + "\n" for r in recs).encode())


def envelope(ev, opts, seq):
    return {"schema": SCHEMA, "ev": ev, "host": opts["host"], "run": opts["rid"], "inv": int(opts["inv"]),
            "seq": seq, "ts": int(time.time())}


def check_common(opts):
    for key in ("run", "host", "rid", "inv", "floor"):
        if key not in opts:
            raise Invalid(key)
    if not (HOST_RE.fullmatch(opts["host"]) and RUN_RE.fullmatch(opts["rid"]) and re.fullmatch(r"[1-9]\d*", opts["inv"])
            and re.fullmatch(r"\d+", opts["floor"])):
        raise Invalid("envelope")


def matches(rec, **kw):
    return all(rec.get(k) == v for k, v in kw.items())


def cmd_event(opts, pos):
    check_common(opts)
    if not pos or pos[0] not in EVENTS or pos[0] == "run_summary":
        raise Invalid("event")
    ev, fields = pos[0], parse_fields(pos[0], pos[1:])
    state, raw, meta = state_for(opts)
    allowed = EVENTS[ev]
    derived = {}
    if "member" in opts and state is not None:
        if not re.fullmatch(r"\d+", opts["member"]):
            raise Invalid("member")
        derived.update(member_dims(state, int(opts["member"])))
    elif "member" in opts:
        derived["member_index"] = coerce(INT, opts["member"])
    if opts.get("task") and state is not None:
        derived.update(task_dims(state))
    if opts.get("mapper") and state is not None:
        derived.update(mapper_dims(state))
    if ev == "questions" and state is not None:
        pending = state.get("pending_questions")
        derived["count"] = len(pending) if isinstance(pending, list) and state.get("status") == "questions" else None
        derived["source"] = question_source(state)
    if ev == "run_end" and state is not None:
        derived.update({"status": map_status(state.get("status")), "step": PHASE_STEP.get(state.get("phase")),
                        "round": state.get("round") if is_int(state.get("round")) else None})
        derived.update({k: v for k, v in task_dims(state).items() if k == "task_index"})
        cfg = state.get("config") or {}
        derived.update({"members": len(state.get("members") or []), "max_rounds": cfg.get("max_rounds") if is_int(cfg.get("max_rounds")) else None,
                        "tasks": len(cfg.get("tasks") or [])})
    if ev in ("run_end", "run_start"):
        derived["skill_version"] = skill_version()
    if opts.get("mapdir"):
        mp = ((state or {}).get("config") or {}).get("map_prepass") or {}
        max_bytes = mp.get("max_output_bytes") if is_int(mp.get("max_output_bytes")) else 65536
        derived.update(mapper_counts(opts["mapdir"], max_bytes, opts.get("no_response"), opts.get("no_captures")))
    if ev == "mapper" and state is not None:
        derived["tokens"], derived["cost_usd"] = mapper_usage(state)
        tid = current_task_id(state)
        entry = ((state.get("map_prepasses") or {}).get(tid) or {})
        if is_int(entry.get("started_at")) and is_int(entry.get("finished_at")) and entry["finished_at"] >= entry["started_at"]:
            derived["duration_s"] = entry["finished_at"] - entry["started_at"]
    if opts.get("mapper_usage") and state is not None:
        derived["tokens"], derived["cost_usd"] = mapper_usage(state)
    evidence = sys.stdin.read() if opts.get("evidence_stdin") else ""
    rec_fields = {k: v for k, v in derived.items() if k in allowed}
    rec_fields.update(fields)

    run = Run(opts["run"])
    try:
        mine = run.mine(opts["rid"])
        starts = {r["seq"]: r for r in mine if r["ev"] == "call_start"}
        ended = {r.get("call_seq") for r in mine if r["ev"] == "call_end"}
        if opts.get("launch_key") and "launch_key" in allowed:
            rec_fields["launch_key"] = launch_key(state, opts["rid"], opts)
        if opts.get("stale") and "call_seq" in allowed and "call_seq" not in fields:
            # a leftover process found on resume: linked only to the one launch its recorded launch key identifies
            rec_fields["call_seq"] = stale_launch(mine, starts, ended, rec_fields.get("member_index"), rec_fields.get("gen"),
                                                  launch_key(state, opts["rid"], opts) if opts.get("launch_key") else None)
        if opts.get("mapper_recover"):
            op, dispatch = mapper_operation(mine, state, rec_fields.get("task_index"), opts["rid"], opts)
            if ev in ("call_end", "timeout"):
                rec_fields["call_seq"] = dispatch
            elif ev == "mapper":
                rec_fields["op_seq"], rec_fields["mapper_seq"] = op, dispatch
        start = starts.get(rec_fields.get("call_seq"))
        if ev == "call_end":
            if start is None or rec_fields["call_seq"] in ended:
                return ""   # one terminal record per launch, never an end without its start
        if start is not None and "call_seq" in allowed:
            for k in CALL:
                if k in allowed and k in start:
                    rec_fields[k] = start[k]
            rec_fields.update(fields)
            if ev == "call_end":
                for k in CALL:
                    if k in start:
                        rec_fields[k] = start[k]
                if "duration_s" not in fields:
                    d = int(time.time()) - start["ts"]
                    rec_fields["duration_s"] = d if d >= 0 else None
        if ev in ("call_end", "retry") and rec_fields.get("failure_class") == "cli_error" and evidence:
            if QUOTA_RE.search(evidence):
                rec_fields["failure_class"] = "quota"
            elif AUTH_RE.search(evidence):
                rec_fields["failure_class"] = "auth"
        if ev == "call_start":   # members: per (member, step, round) over the whole run; the mapper: per task
            names = ("member_index", "step", "round") if rec_fields.get("member_index") is not None else ("member_index", "task_index", "step")
            key = {k: rec_fields.get(k) for k in names}
            prior = [s.get("attempt") or 0 for s in starts.values() if all(s.get(k) == v for k, v in key.items())]
            rec_fields["attempt"] = max(prior, default=0) + 1
        if ev == "votes":
            key = {k: rec_fields.get(k) for k in ("task_index", "step", "round")}
            rec_fields["replay"] = any(r["ev"] == "votes" and matches(r, **key) for r in mine)
        if ev == "mapper" and rec_fields.get("op_seq") is not None and any(
                r["ev"] == "mapper" and r.get("op_seq") == rec_fields["op_seq"] for r in mine):
            return ""   # one terminal record per mapper operation
        seq = max(run.top, int(opts["floor"]), meta["seq"] if meta else 0) + 1
        rec = envelope(ev, opts, seq)
        rec.update({k: v for k, v in rec_fields.items() if k in allowed})
        validate(rec)
        reserve(opts, raw, meta, seq)
        run.append([rec])
        return str(seq)
    finally:
        run.close()


def skill_version():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".claude-plugin", "plugin.json")
    data = load_json(path)
    v = sanitize_version(data.get("version")) if isinstance(data, dict) else None
    return v or "unknown"


def cmd_begin(opts, pos):
    check_common(opts)
    fields = parse_fields("run_start", pos)
    raw = meta = None
    if "config" in opts:
        cfg, members, status = load_json(opts["config"]) or {}, None, "created"
        members = [dict(m, gen=1) for m in (cfg.get("members") or []) if isinstance(m, dict)]
    else:
        state, raw, meta = state_for(opts)
        state = state or {}
        cfg, status = state.get("config") or {}, map_status(state.get("status"))
        members = [m for m in (state.get("members") or []) if isinstance(m, dict)]
    num = lambda v: v if check_value(NUM, v) else None
    council_ho = num(cfg.get("handover_at"))
    style = cfg.get("style") or "normal"
    start = {"command": opts.get("command"), "status": status, "skill_version": skill_version(),
             "claude_version": None, "opencode_version": None,   # unknown unless this invocation measured them
             "members": len(members), "max_rounds": cfg.get("max_rounds") if is_int(cfg.get("max_rounds")) else None,
             "tasks": len(cfg.get("tasks") or []), "timeout_s": num(cfg.get("timeout_s")),
             "max_turns": cfg.get("max_turns", 30) if is_int(cfg.get("max_turns", 30)) else None,
             "handover_at": council_ho, "style": style if style in ("normal", "lite", "caveman", "ultra") else None,
             "executor_present": cfg.get("executor") is not None, "map_code": cfg.get("map_code") is True,
             "gap": opts.get("gap") == "true"}
    start.update(fields)
    run = Run(opts["run"])
    try:
        history = opts.get("history")
        if history == "auto":
            first = [r for r in run.mine(opts["rid"]) if r["ev"] == "run_start"]
            history = first[0].get("history") if first and first[0].get("history") else "partial"
        start["history"] = history
        seq = max(run.top, int(opts["floor"]), meta["seq"] if meta else 0) + 1
        rec = envelope("run_start", opts, seq)
        rec.update(start)
        recs = [rec]
        executor = cfg.get("executor")
        for i, m in enumerate(members):
            seq += 1
            r = envelope("member", opts, seq)
            ho = num(m.get("handover_at", council_ho))
            mstyle = m.get("style") or style
            r.update({"member_index": i, "kind": m.get("kind") if m.get("kind") in ("opencode", "claude") else None,
                      "mode": m.get("mode") if m.get("mode") in ("read", "edit") else None,
                      "executor": executor is not None and m.get("id") == executor,
                      "model": sanitize_model(m.get("model")), "effort": sanitize_model(m.get("effort")),
                      "gen": m.get("gen") if is_int(m.get("gen")) else None, "handover_at": ho,
                      "handover_override": ho != council_ho,
                      "style": mstyle if mstyle in ("normal", "lite", "caveman", "ultra") else None,
                      "style_override": mstyle != style})
            recs.append(r)
        for r in recs:
            validate(r)
        reserve(opts, raw, meta, seq)
        run.append(recs)
        return str(seq)
    finally:
        run.close()


def cmd_nextseq(opts, pos):
    """The next free seq of the run, reserved in state.json when it exists (the run_summary's seq)."""
    if "run" not in opts or not re.fullmatch(r"\d{1,16}", opts.get("floor", "")) or \
            ("rid" in opts and not RUN_RE.fullmatch(opts["rid"])):
        raise Invalid("nextseq")
    state, raw, meta = state_for(opts)
    run = Run(opts["run"])
    try:
        seq = max(run.top, int(opts["floor"]), meta["seq"] if meta else 0) + 1
        reserve(opts, raw, meta, seq)
        return str(seq)
    finally:
        run.close()


def cmd_resume(opts, pos):
    """resume --run DIR --rid NEW -> "<run> <inv> <floor> auto|partial": the identity a resume continues. A trusted
    state.telemetry continues its run above every invocation and seq it or the run's own records show; anything
    else (legacy, opted out, damaged) starts recording NEW here. The result is stored in state.json first."""
    if "run" not in opts or not RUN_RE.fullmatch(opts.get("rid", "")):
        raise Invalid("resume")
    path = os.path.join(opts["run"], "state.json")
    state, raw = read_state(path)
    if state is None:
        raise OSError(errno.ENOENT, "no state")
    meta = trusted_meta(state)
    run = Run(opts["run"])
    try:
        if meta:
            mine = run.mine(meta["run"])
            new = {"run": meta["run"], "inv": max([meta["inv"]] + [r["inv"] for r in mine]) + 1,
                   "seq": max([meta["seq"]] + [r["seq"] for r in mine])}
            hist = "auto"
        else:
            new, hist = {"run": opts["rid"], "inv": 1, "seq": 0}, "partial"
        write_meta(path, raw, new)
    finally:
        run.close()
    return "%s %d %d %s" % (new["run"], new["inv"], new["seq"], hist)


# --------------------------------------------------------------------- ship ----
def summarize(records, rid):
    mine = [r for r in records if r.get("run") == rid]
    starts = {r["seq"]: r for r in mine if r["ev"] == "call_start"}
    ends = {r["call_seq"]: r for r in mine if r["ev"] == "call_end" and r.get("call_seq") in starts}
    outcome = lambda o: sum(1 for e in ends.values() if e.get("outcome") == o)
    incomplete = len(starts) - len(ends)
    totals = [(e.get("tokens") or {}).get("total") for e in ends.values()]
    costs = [e.get("cost_usd") for e in ends.values()]
    milestone = lambda m: sum(1 for r in mine if r["ev"] == "outcome" and r.get("milestone") == m)
    first = [r for r in mine if r["ev"] == "run_start"]
    latest = first[-1] if first else {}
    return {"members": latest.get("members"), "max_rounds": latest.get("max_rounds"), "tasks": latest.get("tasks"),
            "history": first[0].get("history") if first else None, "gap": latest.get("gap"),
            "calls": len(starts), "ok": outcome("ok"), "failed": outcome("failed"), "killed": outcome("killed"),
            "outcome_unknown": sum(1 for e in ends.values() if e.get("outcome") not in ("ok", "failed", "killed")),
            "incomplete": incomplete, "retries": sum(1 for r in mine if r["ev"] == "retry"),
            "timeouts": sum(1 for r in mine if r["ev"] == "timeout"),
            "tokens_known": sum(t for t in totals if t is not None),
            "tokens_unknown_calls": sum(1 for t in totals if t is None) + incomplete,
            "cost_usd_known": round(sum(c for c in costs if c is not None), 6),
            "cost_unknown_calls": sum(1 for c in costs if c is None) + incomplete,
            "handovers": sum(1 for r in mine if r["ev"] == "handover" and r.get("stage") == "start"),
            "questions": sum(r.get("count") or 0 for r in mine if r["ev"] == "questions"),   # the known counts
            "questions_unknown": sum(1 for r in mine if r["ev"] == "questions" and r.get("count") is None),
            "rounds_observed": len({(r.get("task_index"), r.get("step"), r.get("round")) for r in mine if r["ev"] == "votes"}),
            "consensus": milestone("consensus"), "unresolved": milestone("unresolved"),
            "ratified": milestone("ratified"), "unratified": milestone("unratified"),
            "last_event_seq": max((r["seq"] for r in mine), default=None)}


def ledger_dir():
    d = os.environ.get("COUNCIL_TELEMETRY_DIR") or ""
    return d if d else os.path.join(os.path.expanduser("~"), ".council-telemetry")


def cmd_ship(opts, pos):
    for key in ("run", "host", "rid", "inv", "seq", "exit_code"):
        if key not in opts:
            raise Invalid(key)
    if not (HOST_RE.fullmatch(opts["host"]) and RUN_RE.fullmatch(opts["rid"]) and re.fullmatch(r"[1-9]\d*", opts["inv"])
            and re.fullmatch(r"[1-9]\d*", opts["seq"]) and re.fullmatch(r"\d+", opts["exit_code"])):
        raise Invalid("ship")
    status = opts.get("status")
    if status is None:
        state, _, _ = state_for(opts)
        status = map_status(state.get("status")) if isinstance(state, dict) else None
    elif status not in STATUS[1]:
        raise Invalid("status")
    run_dir = opts["run"]
    try:
        run = Run(run_dir, create=False)
        data, records = run.data, run.records
        run.close()
    except FileNotFoundError:
        data, records = b"", []
    cursor_path = os.path.join(run_dir, CURSOR_FILE)
    cursor = 0
    check_ancestors(cursor_path)
    try:
        cst = os.lstat(cursor_path)
    except FileNotFoundError:
        cst = None
    if cst is not None and stat.S_ISLNK(cst.st_mode):   # refused before anything is read, exported or replaced
        raise OSError(errno.ELOOP, "symlinked cursor")
    if cst is not None and stat.S_ISDIR(cst.st_mode):
        raise OSError(errno.EISDIR, "cursor is a directory")
    if cst is not None and not stat.S_ISREG(cst.st_mode):
        raise OSError(errno.EINVAL, "cursor is not a regular file")
    cfd = open_regular(cursor_path, os.O_RDONLY) if cst is not None else None
    if cfd is not None:
        try:
            text = os.read(cfd, 64).decode("ascii", "replace").strip()
        finally:
            os.close(cfd)
        if re.fullmatch(r"\d{1,16}", text) and int(text) <= len(data):
            cursor = int(text)
    end = data.rfind(b"\n") + 1
    # only complete lines the schema accepts, of this run, re-serialized: nothing else is ever copied; the cursor
    # still passes the skipped lines, so they are not met again
    shipped = [r for r in parse_lines(data[cursor:end])[0] if r.get("run") == opts["rid"]] if end > cursor else []
    unshipped_len = end - cursor if end > cursor else 0
    summary = envelope("run_summary", opts, int(opts["seq"]))
    summary.update(summarize(records, opts["rid"]))
    summary.update({"exit_code": int(opts["exit_code"]), "status": status})
    validate(summary)
    batch = "".join(dumps(r) + "\n" for r in shipped + [summary]).encode()
    d = ledger_dir()
    own_dir(d, parents=True)
    fd = open_regular(os.path.join(d, opts["host"] + ".jsonl"), os.O_RDWR | os.O_APPEND | os.O_CREAT)
    try:
        lock(fd)
        write_once(fd, separator(fd) + batch)
    finally:
        os.close(fd)
    tmp = "%s.tmp-%d" % (cursor_path, os.getpid())
    # created exclusively before the cleanup scope: a name collision fails (EEXIST) and never removes what was there
    tfd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        try:
            write_once(tfd, str(cursor + unshipped_len).encode())
        finally:
            os.close(tfd)
        os.replace(tmp, cursor_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return ""


# --------------------------------------------------------------------- main ----
VALUE_OPTS = {"run", "host", "rid", "inv", "floor", "state", "member", "config", "command", "history", "gap", "seq",
              "exit-code", "status", "mapdir"}
FLAG_OPTS = {"task", "mapper", "stale", "launch-key", "mapper-recover", "mapper-usage", "evidence-stdin", "no-response", "no-captures"}


def parse_args(argv):
    opts, pos, i = {}, [], 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--"):
            name = a[2:]
            if name in FLAG_OPTS:
                opts[name.replace("-", "_")] = True
                i += 1
            elif name in VALUE_OPTS and i + 1 < len(argv):
                opts[name.replace("-", "_")] = argv[i + 1]
                i += 2
            else:
                raise Invalid(a)
        else:
            pos.append(a)
            i += 1
    return opts, pos


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        if not argv:
            raise Invalid("command")
        cmd, (opts, pos) = argv[0], parse_args(argv[1:])
        if cmd == "init":
            out = "%s %s" % (host_identity(), uuid.uuid4().hex)
        elif cmd == "begin":
            if opts.get("command") not in ("start", "resume") or opts.get("history") not in ("full", "partial", "auto") \
                    or opts.get("gap") not in ("true", "false") or ("config" in opts) == ("state" in opts):
                raise Invalid("begin")
            out = cmd_begin(opts, pos)
        elif cmd == "event":
            out = cmd_event(opts, pos)
        elif cmd == "nextseq":
            out = cmd_nextseq(opts, pos)
        elif cmd == "resume":
            out = cmd_resume(opts, pos)
        elif cmd == "ship":
            out = cmd_ship(opts, pos)
        else:
            raise Invalid("command")
    except Invalid:
        print("EINVAL")
        return 3
    except Failure as exc:
        print(exc.args[0])
        return 3
    except OSError as exc:
        print(errno_name(exc))
        return 3
    except Exception:
        print("EIO")
        return 3
    if out:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
