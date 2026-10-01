#!/usr/bin/env python3
"""Where do a council run's tokens go? (stdlib only; local by default; read-only; no model calls)

Usage:
  session_report.py --run-dir D [--projects DIR]                        # a run: local files only, nothing is started
  session_report.py --run-dir D --fetch-opencode [--oc scripts/oc.sh]   # also collect OpenCode sessions through oc.sh
  session_report.py --session FILE.jsonl [FILE.jsonl ...]               # any Claude Code transcript(s)

prompt_report.py measures the prompts the orchestrator writes. This measures what happens INSIDE
each member's session: every API call's usage as the provider reported it, and every item that
entered the context (council prompt, tool result, model output, injected context). The report opens
with a Coverage section: one line per session with its source and status (complete | PARTIAL |
unavailable | ambiguous | conflict | unknown (not fetched)). A missing value is unknown, never 0, and a
total with unknown components says how many. The provider counters are exact; only their split over
items uses sizes estimated from characters (--chars-per-token), normalised per call so the categories
add up to exactly what was attributed, and it is labelled 'heuristic estimate (character-based)'.

Claude sessions are read from <projects>/*/<session>.jsonl. OpenCode sessions show the totals recorded in
state.json; with --fetch-opencode their messages are read through `oc.sh api GET /api/session/<id>/message`
(paged, at most MAX_PAGES pages of PAGE_LIMIT messages, each oc.sh call bounded by OC_TIMEOUT_SECONDS). The
connection is pinned through OPENCODE_URL (a preset one, else the service.json state file), so oc.sh can
never start the service. Costs are only the recorded ones. Exit 0 whenever a report is printed (also a
PARTIAL one), 2 for a usage error or when no session can be used.
"""
import argparse
import collections
import glob
import importlib.util
import json
import math
import os
import re
import signal
import subprocess
import sys
import urllib.parse
from datetime import datetime


def ts(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def strlen(v):
    return len(v) if isinstance(v, str) else 0


def disp(v, pipes=True):
    """Text from a transcript, a state file or an adapter, made safe for one line of the report: control characters (C0,
    DEL, C1), U+2028/2029 and lone surrogates become \\xNN or \\uNNNN, a backslash is doubled (so a literal backslash-x1b and
    a real ESC never look alike) and, unless `pipes` is false, a pipe becomes \\| (it would end a table cell). The exact text
    stays in the transcript or the state file."""
    out = []
    for ch in v if isinstance(v, str) else str(v):
        cp = ord(ch)
        if 0xD800 <= cp <= 0xDFFF or cp in (0x2028, 0x2029):
            out.append("\\u%04x" % cp)
        elif cp < 0x20 or 0x7F <= cp <= 0x9F:
            out.append("\\x%02x" % cp)
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "|" and pipes:
            out.append("\\|")
        else:
            out.append(ch)
    return "".join(out)


def as_dict(v):
    return v if isinstance(v, dict) else {}


IMAGE_CHARS = 5100   # an image block is ~1,600 tokens; counted as characters at 3.2 chars/token


def text_len(content):
    if content is None:
        return 0
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(text_len(c) for c in content)
    if isinstance(content, dict):
        if content.get("type") == "image":
            return IMAGE_CHARS
        return text_len(content.get("text", content.get("content")))
    return len(str(content))


# Module constants, not options: the timeout applies to every oc.sh subprocess (status and each api call).
OC_TIMEOUT_SECONDS = 120
MAX_PAGES = 1000     # per session
PAGE_LIMIT = 200     # the server rejects a larger limit with HTTP 400 (measured)

# Claude JSONL line types the reader recognises; anything else is counted and listed (plan section 5).
LINE_TYPES = frozenset(("assistant", "attachment", "user", "last-prompt", "atis-latch", "mode", "ai-title",
                        "queue-operation", "cost-state"))
CACHE_PARTS = (("cw5m", "ephemeral_5m_input_tokens"), ("cw1h", "ephemeral_1h_input_tokens"))


class Session:
    def __init__(self, label, kind):
        self.label, self.kind = label, kind
        self.calls = []      # dicts: ts, model, inp, cr, cw, out (None = unknown), vis, unc, ...
        self.items = []      # (start_call, category, key, chars, call or None)
        self.segments = []   # index of first call of each council call (claude -p invocation)
        self.bad_lines = 0   # unparseable JSONL lines (the transcript is then PARTIAL)
        self.no_id = 0       # assistant lines without message.id, kept individually
        self.invalid_usage = 0
        self.usage_conflicts = 0
        self.repeated_blocks = 0
        self.compaction = 0
        self.other_types = collections.Counter()
        self.models = set()     # every message.model of every assistant line (None = missing), duplicates included
        self.odd = 0            # OpenCode message elements outside the measured flat schema (skipped, never guessed)
        self.seg_prompts = []   # first prompt text of each segment (None when unknown)
        self.member = None      # member id, for the byte-exact prompt attribution


class Gen:
    """One member generation (or one --session file): what the state recorded and how it was read."""

    def __init__(self, label, sid, kind, source):
        self.label, self.sid, self.kind, self.source = label, sid, kind, source
        self.tokens = self.cost = None    # state-recorded totals; None = unknown
        self.status, self.reason, self.detail = "complete", "", ""
        self.session = None
        self.identity = None              # Identity of this generation (None for --session files)
        self.member = None

    def status_text(self):
        return "%s (%s)" % (self.status, self.reason) if self.reason else self.status


def item_category(name):
    return "tool:" + disp(name or "?")


def new_call(o, m):
    return {"i": 0, "ts": ts(o.get("timestamp", "")), "inp": None, "cr": None, "cw": None,
            "out": None, "cw5m": None, "cw1h": None, "vis": 0, "unc": False, "conflicts": set()}


def merge_usage(s, call, usage):
    """Fold one line's usage into the call of its message.id. Missing components stay unknown; two
    different known values for one component make it unknown for this call (never a max or a guess)."""
    if usage is None:
        return
    if not isinstance(usage, dict):
        s.invalid_usage += 1
        return
    parts = (("inp", usage.get("input_tokens")), ("cr", usage.get("cache_read_input_tokens")),
             ("cw", usage.get("cache_creation_input_tokens")), ("out", usage.get("output_tokens")))
    split = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
    parts += tuple((key, split.get(name)) for key, name in CACHE_PARTS)
    for key, raw in parts:
        if raw is None or key in call["conflicts"]:
            continue
        v = num(raw)
        if v is None:
            s.invalid_usage += 1
        elif call[key] is None:
            call[key] = v
        elif call[key] != v:
            call["conflicts"].add(key)
            call[key] = None
            s.usage_conflicts += 1


def load_claude(path, label):
    s = Session(label, "claude")
    by_id, seen, tools = {}, {}, {}
    pending_prompt, pending_text = False, None
    with open(path, "rb") as fh:
        for raw in fh:
            if not raw.strip():
                continue
            try:
                o = json.loads(raw.decode("utf-8"))
            except ValueError:
                o = None
            if not isinstance(o, dict):
                s.bad_lines += 1
                continue
            t = o.get("type")
            if t == "compact_boundary" or (t == "system" and o.get("subtype") == "compact_boundary"):
                s.compaction += 1
                continue
            if not isinstance(t, str) or t not in LINE_TYPES:
                s.other_types[disp(t)] += 1
                continue
            m = o.get("message") if isinstance(o.get("message"), dict) else None
            if t == "assistant" and m is not None:
                s.models.add(m["model"] if isinstance(m.get("model"), str) else None)
                mid = m.get("id") if isinstance(m.get("id"), str) and m.get("id") else None
                if mid is None:
                    s.no_id += 1
                call = by_id.get(mid) if mid else None
                if call is None:
                    if pending_prompt or not s.calls:
                        s.segments.append(len(s.calls))
                        s.seg_prompts.append(pending_text if pending_prompt else None)
                        pending_prompt, pending_text = False, None
                    call = new_call(o, m)
                    call["i"] = len(s.calls)
                    s.calls.append(call)
                    if mid:
                        by_id[mid] = call
                merge_usage(s, call, m.get("usage"))
                start = call["i"] + 1
                blocks = seen.setdefault(mid or id(call), set())
                for b in m.get("content") if isinstance(m.get("content"), list) else []:
                    if not isinstance(b, dict):
                        continue
                    canon = json.dumps(b, sort_keys=True)
                    if canon in blocks:
                        s.repeated_blocks += 1
                        call["unc"] = True
                        continue
                    blocks.add(canon)
                    bt = b.get("type")
                    if bt == "tool_use":
                        if isinstance(b.get("id"), str):
                            tools[b["id"]] = (b.get("name"), as_dict(b.get("input")))
                        n_ = len(json.dumps(b.get("input") or {}))
                        call["vis"] += n_
                        s.items.append((start, "model output", "tool call", n_, call))
                    elif bt == "text":
                        call["vis"] += strlen(b.get("text"))
                        s.items.append((start, "model output", "text", strlen(b.get("text")), call))
                continue
            start = len(s.calls)
            if t == "user" and m:
                content = m.get("content")
                if isinstance(content, str):
                    s.items.append((start, "council prompt", "prompt", len(content), None))
                    if not o.get("isMeta") and not pending_prompt:
                        pending_text = content
                    pending_prompt = pending_prompt or not o.get("isMeta")
                    continue
                for b in content if isinstance(content, list) else []:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "tool_result":
                        name, inp = tools.get(b.get("tool_use_id") if isinstance(b.get("tool_use_id"), str) else None, ("?", {}))
                        key = disp(str(inp.get("file_path") or inp.get("path") or inp.get("pattern") or
                                       str(inp.get("command") or "")[:90] or inp.get("description") or ""), False)
                        s.items.append((start, item_category(name), key, text_len(b.get("content")), None))
                    elif b.get("type") == "text":
                        s.items.append((start, "council prompt", "prompt", strlen(b.get("text")), None))
                        if not o.get("isMeta") and not pending_prompt and isinstance(b.get("text"), str):
                            pending_text = b["text"]
                        pending_prompt = pending_prompt or not o.get("isMeta")
            elif t == "attachment":
                a = as_dict(o.get("attachment"))
                if a.get("type") != "hook_success":
                    s.items.append((start, "injected context", disp(a.get("type") or "?"),
                                    text_len(a.get("content")) or len(json.dumps(a)) // 4, None))
    return s


class OcError(Exception):
    pass


def run_oc(oc, args, env):
    """Run the adapter with an argument array and a bounded lifetime; (returncode, stdout, stderr)."""
    try:
        proc = subprocess.Popen([str(oc)] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                errors="replace", env=env, start_new_session=True)
    except OSError as e:
        raise OcError("cannot start the adapter %s: %s" % (oc, e))
    try:
        out, err = proc.communicate(timeout=OC_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)   # oc.sh api runs curl and jq children
        except OSError:
            proc.kill()
        try:
            proc.communicate(timeout=5)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            pass
        proc.wait()
        raise OcError("timeout after %ss" % OC_TIMEOUT_SECONDS)
    return proc.returncode, out, err


def connection_env(environ):
    """(env, None) for the child processes, or (None, reason) when the service state is unusable.
    A preset OPENCODE_URL is passed through and the state file is not read; otherwise the documented
    ${XDG_STATE_HOME:-~/.local/state}/opencode/service.json pins the connection, so oc.sh can never
    auto-start the service (scripts/oc.sh:81-98). An empty password removes an inherited one."""
    env = dict(environ)
    if environ.get("OPENCODE_URL"):
        return env, None
    base = environ.get("XDG_STATE_HOME") or os.path.join(environ.get("HOME") or os.path.expanduser("~"), ".local", "state")
    try:
        with open(os.path.join(base, "opencode", "service.json"), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None, "service state not found; not started"
    if not isinstance(data, dict):
        return None, "service state invalid; not started"
    if data.get("url") is None:
        return None, "service state not found; not started"
    password = data.get("password")
    if not isinstance(data["url"], str) or data["url"] == "" or (password is not None and not isinstance(password, str)):
        return None, "service state invalid; not started"
    try:                # a lone surrogate cannot be written as UTF-8: the child would get an invented byte, or none at all
        data["url"].encode("utf-8")
        (password or "").encode("utf-8")
    except UnicodeEncodeError:
        return None, "service state invalid (url or password is not valid UTF-8: a lone surrogate); not started"
    env["OPENCODE_URL"] = data["url"]
    if password:
        env["OPENCODE_PASSWORD"] = password
    else:
        env.pop("OPENCODE_PASSWORD", None)
    return env, None


def first_line(text):
    return disp((text.strip().splitlines() or [""])[0][:200])


def fetch_messages(oc, env, sid):
    """Follow the cursor pages of one session. Returns (messages, pages, partial reason or None); every
    way of stopping short has its own reason and keeps what was collected so far."""
    quoted = urllib.parse.quote(sid, safe="")
    path = "/api/session/%s/message?order=asc&limit=%d" % (quoted, PAGE_LIMIT)
    msgs, pages, seen_ids, seen_cursors = [], 0, set(), set()
    while True:
        if pages >= MAX_PAGES:
            return msgs, pages, "reached MAX_PAGES=%d" % MAX_PAGES
        pages += 1
        try:
            rc, out, err = run_oc(oc, ["api", "GET", path], env)
        except OcError as e:
            return msgs, pages, "page %d: %s" % (pages, e)
        if rc != 0:
            return msgs, pages, "page %d: adapter exit %d: %s" % (pages, rc, first_line(err))
        try:
            body = json.loads(out, strict=False)
        except ValueError:
            return msgs, pages, "page %d: invalid JSON" % pages
        if not isinstance(body, dict):
            return msgs, pages, "page %d: invalid response structure (the response is not an object)" % pages
        data, cursor = body.get("data"), body.get("cursor")
        if not isinstance(data, list):
            return msgs, pages, "page %d: invalid response structure (data is missing or not a list)" % pages
        cursor_ok = isinstance(cursor, dict) and "next" in cursor and (cursor["next"] is None or isinstance(cursor["next"], str))
        if not data:
            if "cursor" in body and not cursor_ok:
                return msgs, pages, "page %d: invalid response structure (malformed cursor)" % pages
            return msgs, pages, None
        ids = [m["id"] for m in data if isinstance(m, dict) and isinstance(m.get("id"), str) and m["id"]]
        repeated = bool(ids) and all(i in seen_ids for i in ids)
        seen_ids.update(ids)
        msgs.extend(data)
        if repeated:
            return msgs, pages, "page %d: page repeats messages already seen" % pages
        if not isinstance(cursor, dict):
            return msgs, pages, "page %d: nonempty page whose cursor is missing or not an object" % pages
        if not cursor_ok:
            return msgs, pages, "page %d: invalid response structure (malformed cursor)" % pages
        nxt = cursor.get("next")
        if nxt is None:
            return msgs, pages, None
        try:
            token = urllib.parse.quote(nxt, safe="")
        except UnicodeEncodeError:          # a lone surrogate cannot be sent and is never replaced
            return msgs, pages, "page %d: next cursor is not valid UTF-8 (a lone surrogate); not requested" % pages
        if nxt in seen_cursors:
            return msgs, pages, "page %d: repeated cursor" % pages
        seen_cursors.add(nxt)
        path = "/api/session/%s/message?cursor=%s" % (quoted, token)


KNOWN_MESSAGE_TYPES = ("user", "assistant", "idle", "synthetic")


def fold_message(dst, src, path, conflicted):
    """Fold a repeat of a message id into its first copy: a known value fills an unknown one; two
    different known values make that field unknown (no max, first or last). Returns the conflicts."""
    found = 0
    for key, new in src.items():
        here, old = path + (key,), dst.get(key)
        if here in conflicted:
            continue
        if isinstance(old, dict) and isinstance(new, dict):
            found += fold_message(old, new, here, conflicted)
        elif old is None:
            dst[key] = new
        elif new is not None and old != new:
            dst[key] = None
            conflicted.add(here)
            found += 1
    return found


def clean_messages(raw):
    """Deduplicate by id and set aside what the measured flat schema does not describe."""
    out, by_id, conflicted = [], {}, {}
    info, types = collections.Counter(), collections.Counter()
    for msg in raw:
        if not isinstance(msg, dict) or not isinstance(msg.get("type"), str) or "info" in msg or "parts" in msg:
            info["unrecognized"] += 1
        elif msg["type"] not in KNOWN_MESSAGE_TYPES:
            types[disp(msg["type"])] += 1
        elif not (isinstance(msg.get("id"), str) and msg["id"]):
            info["no_id"] += 1
            out.append(msg)
        elif msg["id"] in by_id:
            info["duplicate"] += 1
            info["conflict"] += fold_message(by_id[msg["id"]], msg, (), conflicted.setdefault(msg["id"], set()))
        else:
            by_id[msg["id"]] = msg
            out.append(msg)
    return out, info, types, conflicted


# Where each counter lives in a message, for the conflicts that fold_message found.
COUNTER_PATHS = {"inp": ("tokens", "input"), "out": ("tokens", "output"), "reasoning": ("tokens", "reasoning"),
                 "cr": ("tokens", "cache", "read"), "cw": ("tokens", "cache", "write")}


def flat_tool_part(part):
    """The measured flat tool part: .name (string), .state {input (object), content (string or list)}. The kit's
    .tool and .state.output forms are not part of it."""
    st = part.get("state")
    return (isinstance(part.get("name"), str) and isinstance(st, dict) and "tool" not in part and "output" not in st
            and (st.get("input") is None or isinstance(st["input"], dict))
            and (st.get("content") is None or isinstance(st["content"], (str, list))))


def opencode_session(label, msgs, conflicted=None):
    """The calls and items of the measured flat messages. An element outside that schema is skipped and
    counted in s.odd (the session is then PARTIAL): its size is unknown, never zero."""
    s = Session(label, "opencode")
    conflicted = conflicted or {}
    for msg in msgs:
        t = msg.get("type")
        if t == "user":
            text = msg.get("text")
            s.odd += text is not None and not isinstance(text, str)
            s.segments.append(len(s.calls))
            s.seg_prompts.append(text if isinstance(text, str) else None)
            s.items.append((len(s.calls), "council prompt", "prompt", strlen(text), None))
        elif t == "assistant":
            if not s.segments:   # calls before any user message form a segment of their own, as load_claude opens one
                s.segments.append(len(s.calls))
                s.seg_prompts.append(None)
            tokens, content = msg.get("tokens"), msg.get("content")
            tok = as_dict(tokens)
            cache = as_dict(tok.get("cache"))
            s.odd += (tokens is not None and not isinstance(tokens, dict)) + \
                (tok.get("cache") is not None and not isinstance(tok.get("cache"), dict)) + \
                (content is not None and not isinstance(content, list))
            call = {"i": len(s.calls), "ts": None, "inp": num(tok.get("input")), "cr": num(cache.get("read")),
                    "cw": num(cache.get("write")), "out": num(tok.get("output")), "reasoning": num(tok.get("reasoning")),
                    "cw5m": None, "cw1h": None, "vis": 0, "unc": False, "cost": num(msg.get("cost")),
                    "id": disp(msg["id"]) if isinstance(msg.get("id"), str) else msg.get("id")}
            paths = conflicted.get(msg["id"], ()) if isinstance(msg.get("id"), str) else ()
            call["conflicts"] = {k for k, at in COUNTER_PATHS.items() if any(at[:len(p)] == p for p in paths)}
            s.calls.append(call)
            start = len(s.calls)
            for part in content if isinstance(content, list) else []:
                ptype = part.get("type") if isinstance(part, dict) else None
                if ptype == "text" and isinstance(part.get("text"), str):
                    call["vis"] += len(part["text"])
                    s.items.append((start, "model output", "text", len(part["text"]), call))
                elif ptype == "tool" and flat_tool_part(part):
                    st = part["state"]
                    inp = as_dict(st.get("input"))
                    key = disp(str(inp.get("filePath") or inp.get("path") or inp.get("pattern") or str(inp.get("command") or "")[:90]), False)
                    call["vis"] += len(json.dumps(inp))
                    s.items.append((start, item_category(part["name"]), key, text_len(st.get("content")), None))
                elif ptype != "reasoning" or not isinstance(part, dict):
                    s.odd += 1
    return s


def complete(c):
    """A call takes part in the attribution only when its whole input side is known."""
    return c["inp"] is not None and c["cr"] is not None and c["cw"] is not None


def attributable(s):
    """The calls with a complete input side, and rank[i] = how many of them precede call i."""
    keep, rank = [], [0]
    for c in s.calls:
        if complete(c):
            keep.append(c)
        rank.append(len(keep))
    return keep, rank


def context_drops(keep):
    ctx = [c["inp"] + c["cr"] + c["cw"] for c in keep]
    return [k for k in range(1, len(ctx)) if ctx[k] < 0.7 * ctx[k - 1]]


def analyse(s, cpt):
    """Attribute every call's provider-measured context to the items live in it.

    ctx_k = new + cache read + cache write of call k (exact, from the provider). Live at call k: the
    harness prefix (ctx_0 minus the first prompt) plus every item added before k and not yet dropped
    by a context reset (ctx falls > 30%; cause unknown). Hidden reasoning (output tokens minus the
    visible text/tool calls) is an item too, estimated only when the output count is known. Each
    call's ctx_k is split over its live items by estimated size (scaled down when the estimates exceed
    it); what the transcript does not show is "unlogged". Calls whose input side is not fully known
    are excluded. The categories add up to exactly the measured total of the attributed calls."""
    keep, rank = attributable(s)
    n = len(keep)
    ctx = [c["inp"] + c["cr"] + c["cw"] for c in keep]
    items = [(rank[it[0]],) + tuple(it[1:]) for it in s.items]
    items = [it for it in items if it[0] < n]
    for k, c in enumerate(keep):
        if s.kind == "claude" and c["out"] is not None:
            hid = c["out"] - c["vis"] / cpt
            if hid > 0 and k + 1 < n:
                items.append((k + 1, "model output", "reasoning (estimated)", hid * cpt, c))
        elif s.kind == "opencode" and c.get("reasoning") and k + 1 < n:
            items.append((k + 1, "model output", "reasoning (reported)", c["reasoning"] * cpt, c))
    resets = context_drops(keep)
    def end_of(start):
        for r in resets:
            if r > start:
                return r
        return n
    harness = max(ctx[0] - sum(it[3] for it in items if it[0] == 0) / cpt, 0) if n else 0
    diff = [0.0] * (n + 1)
    for st, cat, key, chars, call in items:
        diff[st] += chars / cpt
        diff[end_of(st)] -= chars / cpt
    live, pref, unlogged = 0.0, [0.0], 0.0
    for k in range(n):
        live += diff[k]
        load = harness + live
        norm = min(1.0, ctx[k] / load) if load > 0 else 0.0
        unlogged += max(ctx[k] - load, 0)
        pref.append(pref[-1] + norm)
    by_cat = collections.Counter()
    by_key = collections.defaultdict(lambda: [0, 0, 0])   # times, chars, re-sent tokens
    bounds = sorted(set(rank[b] for b in s.segments) or {0}) + [n]
    def seg_end(start):   # first call of the next segment (one segment per prompt)
        for b in bounds:
            if b > start:
                return b
        return n
    inherited = 0.0       # re-sent by LATER segments of the same session (persistent --resume)
    for st, cat, key, chars, call in items:
        sent = chars / cpt * (pref[end_of(st)] - pref[st])
        inherited += sent - chars / cpt * (pref[min(end_of(st), seg_end(st))] - pref[st])
        label = cat + " \u2014 " + key if cat == "model output" else cat
        if call is not None and call["unc"]:
            label += " (attribution uncertain)"
        by_cat[label] += sent
        if cat.startswith("tool:"):
            v = by_key[(cat, key)]
            v[0] += 1; v[1] += chars; v[2] += sent
    by_cat["harness prefix (system prompt, tools, CLAUDE.md, MCP, skills)"] += harness * pref[n]
    if unlogged:
        by_cat["unlogged context (reminders, schemas loaded later, estimate error)"] += unlogged
    return sum(ctx), by_cat, by_key, harness, inherited


def fmt(x):
    x = float(x)
    for unit, div in (("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if abs(x) >= div:
            return "%.1f%s" % (x / div, unit)
    return "%d" % x


def num(v):
    """A recorded counter is known only when it is a finite, non-negative number (never a bool)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0 or not math.isfinite(v):
        return None
    return v


def counts_note(known, unknown, conflicting):
    """The account that accompanies a partial total: unknown and conflicting components are counted apart."""
    return "known components: %d; unknown components: %d%s" % (
        known, unknown, "; conflicting components: %d" % conflicting if conflicting else "")


def total(values, conflicting=0):
    """Sum of the known components with an explicit account of the unknown and the conflicting ones (never
    a silent 0); `conflicting` of the components that are None are conflicts and are not counted as unknown."""
    known = [v for v in values if v is not None]
    if not values:
        return "unknown"
    if not known:
        return "unknown (%s)" % counts_note(0, len(values) - conflicting, conflicting)
    tot = sum(known)
    if len(known) == len(values):
        return str(tot)
    return "%s (%s)" % (tot, counts_note(len(known), len(values) - len(known) - conflicting, conflicting))


def column(calls, key):
    """total() of one counter over the calls, with the conflicting components counted apart."""
    return total([c[key] for c in calls], sum(c[key] is None and key in c["conflicts"] for c in calls))


UNUSABLE_LINES = 50      # state entries named one per line in the Coverage section (all of them are counted)


def unusable_entries(state):
    """Where the state file has an entry that is not an object (a member, or a retired entry of a member): it can name no
    session, so its recorded totals are unknown and in no total."""
    found = []
    for i, m in enumerate(state.get("members", [])):
        if not isinstance(m, dict):
            found.append("members[%d]" % i)
            continue
        found += ["members[%d].retired[%d]" % (i, j) for j, r in enumerate(m.get("retired") or []) if not isinstance(r, dict)]
    return found


def print_coverage(gens, unusable=()):
    print("## Coverage\n")
    bad = [g for g in gens if g.status != "complete"]
    if bad or unusable:
        print("PARTIAL: %d session(s) not complete%s; totals are sums of known components only\n"
              % (len(bad), "; %d state entries unusable" % len(unusable) if unusable else ""))
    for g in gens:
        print("- %s: %s %s%s" % (g.label, g.source, g.status_text(), " \u2014 " + g.detail if g.detail else ""))
        if g.identity is not None:
            print("  identity: %s" % g.identity.text())
    for where in unusable[:UNUSABLE_LINES]:
        print("- state.json: %s is not an object (ignored; its recorded tokens and cost are unknown and are in no total)" % where)
    if len(unusable) > UNUSABLE_LINES:
        print("- state.json: %d more entries that are not objects (ignored, counted as unknown components)"
              % (len(unusable) - UNUSABLE_LINES))
    print()


# council.sh writes 0 as the session cost when the provider reported none (`// 0` at council.sh:1056 for
# OpenCode and council.sh:1076 for Claude), so a recorded 0 cannot be told from "no cost reported".
STATE_ZERO = "0 (state; the orchestrator also writes 0 when no cost was reported)"


def consumed_keys(kind):
    """The counters that make up what a call consumed: input side and output, plus the reported
    reasoning for OpenCode (the orchestrator counts it, council.sh), so it is included exactly once."""
    return ("inp", "cr", "cw", "out") + (("reasoning",) if kind == "opencode" else ())


def measured_tokens(g):
    """Provider-measured total of a session, or None when any component is unknown."""
    if not g.session or not g.session.calls:
        return None
    vals = [c[k] for c in g.session.calls for k in consumed_keys(g.session.kind)]
    return None if any(v is None for v in vals) else sum(vals)


def consumed(kind, calls):
    """[sum of the known counters, known, unknown, conflicting] over the consumed counters of `calls`;
    a conflicting component is not known either (never a max or a guess) and is counted only as conflicting."""
    tot = [0, 0, 0, 0]
    for c in calls:
        for k in consumed_keys(kind):
            if c.get(k) is None:
                tot[3 if k in c["conflicts"] else 2] += 1
            else:
                tot[0] += c[k]
                tot[1] += 1
    return tot


def qualified(tot):
    """A consumed total with its account of unknown and conflicting components (never a silent 0)."""
    value, known, unknown, conflicting = tot
    if not (known or unknown or conflicting):
        return "n/a (nothing counted)"
    if not known:
        return "unknown (%s)" % counts_note(0, unknown, conflicting)
    return str(value) if not (unknown or conflicting) else "%s (%s)" % (value, counts_note(known, unknown, conflicting))


def print_state_totals(gens):
    print("## State-recorded totals (orchestrator accounting; a separate view from the provider counters)\n")
    print("| session | recorded tokens | measured tokens | comparison | recorded cost |\n|---|---|---|---|---|")
    for g in gens:
        meas = measured_tokens(g)
        if g.tokens is None or meas is None:
            cmp_ = "not comparable (a total is unknown)"
        else:
            cmp_ = "equal" if g.tokens == meas else "differs (sources measure differently; not resolved)"
        cost = "unknown" if g.cost is None else STATE_ZERO if g.cost == 0 else g.cost
        print("| %s | %s | %s | %s | %s |" % (g.label, "unknown" if g.tokens is None else g.tokens,
                                             "unknown" if meas is None else meas, cmp_, cost))
    print()


def load_prompt_report():
    """prompt_report.py through importlib without leaving bytecode behind (as handoff_replay.py does);
    None when it cannot be loaded, in which case every phase is unknown."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompt_report.py")
    saved, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        spec = importlib.util.spec_from_file_location("prompt_report_for_session_report", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module if hasattr(module, "layout") else None
    except Exception:
        return None
    finally:
        sys.dont_write_bytecode = saved


def member_prompt_files(run, mid):
    """The member's OWN prompt files: '*-<id>.md', '*-<id>-retry.md' and 'handover-<id>-g<N>.md'."""
    out = []
    try:
        names = sorted(os.listdir(os.path.join(run, "prompts")))
    except OSError:
        return out
    for name in names:
        if re.fullmatch(r"handover-%s-g\d+\.md" % re.escape(str(mid)), name):
            kind = "handover"
        elif name.endswith("-%s-retry.md" % mid):
            kind = "retry"
        elif name.endswith("-%s.md" % mid):
            kind = "round"
        else:
            continue
        try:
            with open(os.path.join(run, "prompts", name), "rb") as fh:
                out.append({"name": name, "kind": kind, "data": fh.read()})
        except OSError:
            continue
    return out


def attribute_segment(s, j, run, pr, cache):
    """(task, round, phase, provenance) of segment j, only from a byte-for-byte match, with no
    normalization of any kind, to exactly one of the member's own prompt files; otherwise unknown."""
    unknown = ("unknown", "unknown", "unknown")
    if pr is None:
        return unknown + ("prompt_report unavailable",)
    if run is None:
        return unknown + ("no run dir (--session mode)",)
    if s.member is None:
        return unknown + ("no member id in state.json",)
    prompt = s.seg_prompts[j] if j < len(s.seg_prompts) else None
    if s.member not in cache:
        cache[s.member] = member_prompt_files(run, s.member)
    data = None
    if isinstance(prompt, str):
        try:
            data = prompt.encode("utf-8")
        except UnicodeEncodeError:               # a lone surrogate: there are no bytes to compare, and none are invented
            return unknown + ("first prompt is not valid UTF-8 (a lone surrogate); not compared with any prompt file",)
    hits = [f for f in cache[s.member] if data is not None and f["data"] == data]
    if len(hits) != 1:
        return unknown + ("several matches" if hits else "no match",)
    f = hits[0]
    prov = "prompts/" + disp(f["name"]) + (" (retry)" if f["kind"] == "retry" else "")
    if f["kind"] == "handover":
        return ("unknown", "unknown", "handover request", prov)
    try:
        info = pr.layout(f["data"])
    except Exception:
        info = None
    if info is None:
        return unknown + (prov + ("" if f["kind"] == "retry" else " (no task header)"),)
    m = re.search(r"\bround (\d+)", info[1])
    return (info[0], m.group(1) if m else "unknown", info[1], prov)


TOKEN_FIELDS = ("input", "cache_read", "cache_write", "output", "reasoning")


class Comp:
    """One recorded component (the tokens or the cost of one session id) and its fate."""

    def __init__(self, what, label, value, sid=None):
        self.what, self.labels, self.sid = what, [label], sid
        self.value = value                                   # the first known value (None = unknown)
        self.seen = [] if value is None else [(label, value)]
        self.conflict = None    # a text once known values disagree; the component is then excluded from every sum
        self.fill = None        # (known mapper components, unknown mapper fields) once completed from the mapper record

    @property
    def name(self):
        return "%s %s" % (self.labels[0], self.what)

    @property
    def known_count(self):
        """How many known components the value stands for: one recorded value, or the mapper fields that completed it."""
        if self.value is None or self.conflict:
            return 0
        return self.fill[0] if self.fill else 1

    def fold(self, label, value):
        """Another record of the same session id (a generation sharing it): an equal value adds nothing,
        a known value fills an unknown one, and two different known values are a conflict."""
        self.labels.append(label)
        if value is None:
            return
        self.seen.append((label, value))
        if self.value is None:
            self.value = value
        if len({v for _, v in self.seen}) > 1:
            self.conflict = "conflict (same session id recorded with different values: %s; neither preferred)" % (
                " vs ".join("%s %s" % kv for kv in self.seen))


def mapper_sessions(state):
    """(sentence, sessions): the map pre-pass records folded per unique session with the keys
    codemap_report.report_prepass uses ('task:<parent>' when a record has no session)."""
    if state.get("map_prepass_version") != 1:
        return "Map pre-pass: not enabled for this run.", {}
    records = state.get("map_prepasses")
    if not isinstance(records, dict) or not records:
        return "Map pre-pass: enabled; no task records yet (usage and coverage unknown).", {}
    sessions = {}
    for parent, record in sorted(records.items(), key=lambda kv: str(kv[0])):
        record = record if isinstance(record, dict) else {}
        usage = record.get("usage") if isinstance(record.get("usage"), dict) else {}
        sid = record.get("session")
        key = str(sid) if sid else "task:" + str(parent)
        ms = sessions.setdefault(key, {"parents": [], "nosession": not sid, "fields": {}, "conflicts": set(), "seen": {},
                                       "overlap": None, "filled": False, "excl": {"tokens": False, "cost": False},
                                       "label": disp(key)})
        ms["parents"].append(disp(parent))
        for field in TOKEN_FIELDS + ("cost",):
            v = num(usage.get(field))
            if v is None:
                continue
            if v not in ms["seen"].setdefault(field, []):
                ms["seen"][field].append(v)     # every distinct value, so a conflict can report them
            if field in ms["conflicts"]:
                continue
            if field not in ms["fields"]:
                ms["fields"][field] = v
            elif ms["fields"][field] != v:
                ms["conflicts"].add(field)
                del ms["fields"][field]
    return None, sessions


def member_components(state):
    """(token components, cost components): one of each per session id, so a session recorded by several
    generations counts once when the records are identical and is a conflict when they differ (a row
    without a session id stays apart)."""
    toks, costs, by_sid = [], [], {}
    for i, m in enumerate(state.get("members", [])):
        if not isinstance(m, dict):
            toks.append(Comp("tokens", "members[%d] (not an object)" % i, None))
            costs.append(Comp("cost", "members[%d] (not an object)" % i, None))
            continue
        for j, r in enumerate(m.get("retired") or []):           # an entry that is not an object is an unknown component
            if not isinstance(r, dict):
                toks.append(Comp("tokens", "members[%d].retired[%d] (not an object)" % (i, j), None))
                costs.append(Comp("cost", "members[%d].retired[%d] (not an object)" % (i, j), None))
        rows = [(r.get("gen"), r.get("session"), r.get("tokens"), r.get("cost")) for r in m.get("retired") or []
                if isinstance(r, dict)]
        rows.append((m.get("gen"), m.get("session"), m.get("session_tokens"), m.get("session_cost")))
        for gen, sid, tokens, cost in rows:
            label, key = disp("%s g%s" % (m.get("id"), gen)), str(sid) if sid else None
            if key in by_sid:
                by_sid[key][0].fold(label, num(tokens))
                by_sid[key][1].fold(label, num(cost))
                continue
            toks.append(Comp("tokens", label, num(tokens), key))
            costs.append(Comp("cost", label, num(cost), key))
            if key:
                by_sid[key] = (toks[-1], costs[-1])
    return toks, costs


def compare_overlaps(member_toks, member_costs, sessions):
    """R1: a mapper session that is also a member session is counted once, as member. A component that is
    unknown for the member is completed from the known mapper fields and labelled 'from mapper record'
    (the unknown fields stay unknown); when both sides are known and differ, the component is a conflict
    and neither side is preferred."""
    by_sid = {}
    for tc, cc in zip(member_toks, member_costs):
        if tc.sid:
            by_sid.setdefault(tc.sid, (tc, cc))
    for key, ms in sessions.items():
        if ms["nosession"] or key not in by_sid:
            continue
        tc, cc = by_sid[key]
        notes = []
        parts = [ms["fields"].get(f) for f in TOKEN_FIELDS]
        for label, comp, fields, mval, known in (
                ("tokens", tc, TOKEN_FIELDS, sum(parts) if None not in parts else None, None not in parts),
                ("cost", cc, ("cost",), ms["fields"].get("cost"), "cost" in ms["fields"])):
            clash = [f for f in fields if f in ms["conflicts"]]
            if clash:   # the mapper's own records contradict each other: that evidence must survive the overlap
                said = "mapper records disagree on " + ", ".join("%s: %s" % (f, " vs ".join(str(v) for v in ms["seen"][f]))
                                                                for f in clash)
                if comp.conflict is None:
                    comp.conflict = "conflict (%s; member %s; neither preferred)" % (said, "unknown" if comp.value is None else comp.value)
                else:
                    notes.append("%s: %s" % (label, said))
            elif comp.value is None and comp.conflict is None and ms["fields"].keys() & set(fields):
                have = [f for f in fields if f in ms["fields"]]
                missing = [f for f in fields if f not in ms["fields"]]
                # completed from the mapper record, counted once as member; a missing field stays unknown
                comp.value, comp.fill = sum(ms["fields"][f] for f in have), (len(have), missing)
                ms["filled"] = True
                notes.append("%s from mapper record" % label + (" (%s)" % counts_note(len(have), len(missing), 0) if missing else ""))
            elif comp.value is None or not known:
                notes.append("%s not comparable" % label)
            elif comp.value != mval and comp.conflict is None:
                comp.conflict = "conflict (member %s vs mapper %s; neither preferred)" % (comp.value, mval)
            ms["excl"][label] = True
        ms["overlap"] = "overlap (counted once as member)" + "".join(" \u2014 " + n for n in notes)


def listing(names):
    return "%d%s" % (len(names), " (%s)" % ", ".join(names) if names else "")


def detail_line(known, unknown, conflicts, overlaps):
    return "  known components: %d; unknown components: %s; conflicting components: %s; overlaps: %s" % (
        known, listing(unknown), listing(conflicts), "; ".join(overlaps) if overlaps else "none")


def print_run_totals(state):
    sentence, sessions = mapper_sessions(state)
    toks, costs = member_components(state)
    compare_overlaps(toks, costs, sessions)
    print("## Run totals \u2014 Member subtotal (state totals), map pre-pass usage as recorded (never fetched), "
          "RUN TOTAL (known components)\n")
    mt = [c.value for c in toks if c.value is not None and not c.conflict]
    mc = [c.value for c in costs if c.value is not None and not c.conflict]
    m_unknown = [c.name for c in toks + costs if c.value is None and not c.conflict]
    m_unknown += ["%s: mapper %s (from mapper record)" % (c.name, f) for c in toks + costs if c.fill for f in c.fill[1]]
    m_conflict = ["%s: %s" % (c.name, c.conflict) for c in toks + costs if c.conflict]
    shared = ["%s: same session id in %s (identical values counted once, differing values are conflicts)" % (
        disp(c.sid), " and ".join([", ".join(c.labels[:-1]), c.labels[-1]] if len(c.labels) > 2 else c.labels))
        for c in toks if len(c.labels) > 1]
    print("Member subtotal: {} known tokens; cost_usd={} known components".format(
        sum(mt) if mt else "unknown", sum(mc) if mc else "UNKNOWN"))
    filled = ["%s: %s" % (ms["label"], ms["overlap"]) for ms in sessions.values() if ms["filled"]]
    print(detail_line(sum(c.known_count for c in toks + costs), m_unknown, m_conflict, shared + filled))
    if sentence:
        print(sentence)
    kt, kc, p_unknown, p_conflict, overlaps = [], [], [], [], []
    if sessions:
        print("\nMap pre-pass records (usage as recorded in state.json; nothing is fetched):\n")
        print("| mapper session | input | cache_read | cache_write | output | reasoning | cost | tasks |")
        print("|---|---|---|---|---|---|---|---|")
        for key, ms in sessions.items():
            name = ms["label"] + (" (no session id recorded)" if ms["nosession"] else "")
            cells = ["conflict (%s)" % " vs ".join(str(v) for v in ms["seen"][f]) if f in ms["conflicts"]
                     else ms["fields"].get(f, "unknown") for f in TOKEN_FIELDS + ("cost",)]
            print("| %s | %s | %s |" % (name, " | ".join(str(c) for c in cells), ", ".join(ms["parents"])))
            if ms["overlap"]:
                overlaps.append("%s: %s" % (ms["label"], ms["overlap"]))
            for f in TOKEN_FIELDS + ("cost",):
                group = "cost" if f == "cost" else "tokens"
                if ms["excl"][group]:
                    continue
                if f in ms["conflicts"]:
                    p_conflict.append("%s %s" % (ms["label"], f))
                elif f in ms["fields"]:
                    (kc if f == "cost" else kt).append(ms["fields"][f])
                else:
                    p_unknown.append("%s %s" % (ms["label"], f))
        print()
        p_conflict += ["%s: %s" % (c.name, c.conflict) for c in toks + costs if c.conflict]
        miss_t = [ms["label"] for ms in sessions.values() if not ms["excl"]["tokens"]
                  and any(f not in ms["fields"] and f not in ms["conflicts"] for f in TOKEN_FIELDS)]
        miss_c = [ms["label"] for ms in sessions.values() if not ms["excl"]["cost"]
                  and "cost" not in ms["fields"] and "cost" not in ms["conflicts"]]
        print("Map pre-pass subtotal: {} known tokens across {} unique session(s); cost_usd={} known components; "
              "token usage unknown for {}; cost unknown for {}".format(
                  sum(kt) if kt else "unknown", len(sessions), sum(kc) if kc else "UNKNOWN",
                  ",".join(miss_t) or "none", ",".join(miss_c) or "none"))
        print(detail_line(len(kt) + len(kc), p_unknown, p_conflict, overlaps))
    run_t = (sum(mt) + sum(kt)) if mt or kt else "unknown"
    run_c = (sum(mc) + sum(kc)) if mc or kc else "UNKNOWN"
    print("RUN TOTAL (known components): {} known tokens; cost_usd={} known components".format(run_t, run_c))
    print(detail_line(sum(c.known_count for c in toks + costs) + len(kt) + len(kc), m_unknown + p_unknown,
                      m_conflict + [c for c in p_conflict if c not in m_conflict], shared + overlaps))
    print()


def report(gens, cpt, top, state=None, run=None):
    print("# Token report \u2014 member sessions\n")
    print_coverage(gens, unusable_entries(state) if state is not None else ())
    print_state_totals(gens)
    if state is not None:
        print_run_totals(state)
    for g in gens:
        if g.session:
            g.session.member = g.member
    sessions = [g.session for g in gens if g.session]
    pr, cache = load_prompt_report(), {}
    print("## Provider counters (as reported by the provider, from transcripts or fetched messages)\n")
    print("| session | kind | calls | council calls | input (new) | cache read | cache write | output | reasoning (reported) |")
    print("|---|---|---|---|---|---|---|---|---|")
    all_cat, all_key = collections.Counter(), collections.defaultdict(lambda: [0, 0, 0])
    tot_meas, tot_inh, rewrites, segs, n_keep, n_calls = 0, 0.0, [], [], 0, 0
    for s in sessions:
        cs = s.calls
        print("| %s | %s | %d | %d | %s | %s | %s | %s | %s |" % (
            s.label, s.kind, len(cs), len(s.segments), column(cs, "inp"), column(cs, "cr"), column(cs, "cw"),
            column(cs, "out"),
            column(cs, "reasoning") if s.kind == "opencode" else "n/a (Claude usage has no reasoning count)"))
        meas, by_cat, by_key, harness, inh = analyse(s, cpt)
        tot_meas += meas
        n_calls += len(cs)
        tot_inh += inh
        all_cat.update(by_cat)
        for k, v in by_key.items():
            for i in range(3):
                all_key[k][i] += v[i]
        keep, rank = attributable(s)
        n_keep += len(keep)
        starts = list(s.segments) or [0]
        for j, (a, b) in enumerate(zip(starts, starts[1:] + [len(cs)])):
            calls = cs[a:b]
            if calls:
                seg = [c for c in calls if complete(c)]
                seg_ctx = [c["inp"] + c["cr"] + c["cw"] for c in seg]
                segs.append((consumed(s.kind, calls), s.label, j + 1, len(calls), seg_ctx[0] if seg else None,
                             seg_ctx[-1] if seg else None, attribute_segment(s, j, run, pr, cache)))
        for c in keep:
            p = cs[c["i"] - 1] if c["i"] else None   # the immediately preceding call, attributable or not
            ctx = c["inp"] + c["cr"] + c["cw"]
            w = c["cw"]
            if ctx and w > 20000 and w > 0.5 * ctx:
                if p is None:
                    gap = "n/a (no earlier call)"
                elif c["ts"] and p["ts"]:
                    gap = "%.0f" % ((c["ts"] - p["ts"]) / 60)
                else:
                    gap = "n/a (a timestamp is missing)"
                rewrites.append((w, s.label, c["i"], gap))
        if harness:
            print("|  \u21b3 harness prefix per call \u2248 %s tokens (heuristic estimate) | | | | | | | |" % fmt(harness))
    for s in sessions:
        parts = ["%s=%s (%d call(s))" % (label, total(vals), len(vals)) for label, key in
                 (("5m", "cw5m"), ("1h", "cw1h")) for vals in [[c[key] for c in s.calls if c[key] is not None]] if vals]
        if parts:
            print("\ncache creation split recorded for %s (shown only where present; never added to the total): %s"
                  % (s.label, ", ".join(parts)))
    costed = [s for s in sessions if s.kind == "opencode" and s.calls]
    if costed:
        print("\n## OpenCode per-message costs (recorded per assistant message; never added to the state cumulative cost)\n")
        print("| session | message | cost |\n|---|---|---|")
        for s in costed:
            for c in s.calls[:top]:
                print("| %s | %s | %s |" % (s.label, c["id"] or "(no id)", "unknown" if c["cost"] is None else c["cost"]))
            if len(s.calls) > top:
                print("| %s | \u2026 %d more message(s) not listed (see --top) | |" % (s.label, len(s.calls) - top))
    expl = sum(all_cat.values())
    if n_keep:
        print("\nProvider-measured input side of the attribution-eligible calls (%d of %d call(s); new + cache read + "
              "cache write): **%s tokens**. The attribution below accounts for %s of it (%.0f%%). The Provider counters "
              "table above is the complete provider-counter view and keeps unknown components explicit."
              % (n_keep, n_calls, tot_meas, fmt(expl), 100 * expl / tot_meas if tot_meas else 0))
        print("Of it, %s tokens (%.0f%%) are re-sent by later council calls of the same session "
              "(heuristic estimate (character-based))." % (fmt(tot_inh), 100 * tot_inh / tot_meas if tot_meas else 0))
    else:
        print("\nProvider-measured input side: unknown \u2014 no call with a fully known input side was collected "
              "(attribution-eligible calls: 0 of %d); nothing is attributed." % n_calls)
    print("\n## Where the input tokens go \u2014 heuristic estimate (character-based; size x number of later calls that re-send it)\n")
    print("| category | tokens | share |\n|---|---|---|")
    for cat, v in all_cat.most_common():
        print("| %s | %s | %.1f%% |" % (cat, fmt(v), 100 * v / expl if expl else 0))
    print("\n## Top %d tool results by re-sent tokens \u2014 heuristic estimate (character-based)\n" % top)
    print("| tool | target | times | chars read | re-sent tokens |\n|---|---|---|---|---|")
    for (cat, key), v in sorted(all_key.items(), key=lambda kv: -kv[1][2])[:top]:
        print("| %s | `%s` | %d | %s | %s |" % (cat[5:], str(key).replace("|", "/")[:90], v[0], fmt(v[1]), fmt(v[2])))
    if segs:
        print("\n## Segments, most tokens first \u2014 heuristic estimate (character-based)\n")
        print("A segment starts at the first API call after a prompt; segments are not proven CLI invocations.\n")
        print("| session | segment # | turns (API calls) | context at start | context at end | tokens consumed | task | round | phase | provenance |")
        print("|---|---|---|---|---|---|---|---|---|---|")
        for tokc, lab, j, turns, c0, c1, at in sorted(segs, key=lambda x: -x[0][0])[:top]:
            print("| %s | %d | %d | %s | %s | %s | %s |" % (lab, j, turns, "unknown" if c0 is None else fmt(c0),
                                                          "unknown" if c1 is None else fmt(c1), qualified(tokc), " | ".join(at)))
    agg = collections.OrderedDict([(("unknown", "unknown", "unknown"), [0, [0, 0, 0, 0]])])
    for tokc, lab, j, turns, c0, c1, at in segs:
        row = agg.setdefault(at[:3], [0, [0, 0, 0, 0]])
        row[0] += 1
        row[1] = [x + y for x, y in zip(row[1], tokc)]
    print("\n## Task and phase attribution (byte-exact match with the member's own prompt files; otherwise unknown)\n")
    print("| task | round | phase | segments | tokens consumed |\n|---|---|---|---|---|")
    for key, (count, tokc) in sorted(agg.items(), key=lambda kv: -kv[1][1][0]):
        print("| %s | %d | %s |" % (" | ".join(key), count, qualified(tokc)))
    reread = [(k, v) for k, v in all_key.items() if k[0] in ("tool:Read", "tool:read") and v[0] > 1]
    if reread:
        extra = sum(v[1] * (v[0] - 1) / v[0] for k, v in reread) / cpt
        print("\nFiles read more than once: %d; repeated reads \u2248 %s tokens at first reading (before re-sending)."
              % (len(reread), fmt(extra)))
    if rewrites:
        print("\n## Large cache writes (> 20000 tokens and > 50% of the input side)\n")
        print("Call numbers start at 0 (the first call of the session), in call order; the context-drop diagnostic in the "
              "Coverage section uses the same numbers.\n")
        print("| tokens written | session | call | gap before (min) |\n|---|---|---|---|")
        for w, lab, i, gap in sorted(rewrites, reverse=True)[:top]:
            print("| %d | %s | %d | %s |" % (w, lab, i, gap))
        print("\nA gap or a large cache write does not by itself show the cause.")


def read_state(ap, run):
    if not os.path.isdir(run):
        ap.error("run dir not found or not a directory: %s" % run)
    path = os.path.join(run, "state.json")
    try:
        with open(path, encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, ValueError) as e:
        ap.error("state.json unreadable or invalid (%s): %s" % (path, e))
    if not isinstance(state, dict) or not isinstance(state.get("members"), list):
        ap.error("state.json invalid (%s): expected an object with a members list" % path)
    for i, m in enumerate(state["members"]):
        if not isinstance(m, dict):
            continue
        if m.get("id") is not None and not isinstance(m["id"], str):
            ap.error("state.json invalid (%s): member %d: id must be a string or null" % (path, i))
        if m.get("session") is not None and not isinstance(m["session"], str):
            ap.error("state.json invalid (%s): member %d: session must be a string or null" % (path, i))
        retired = m.get("retired")
        if retired is not None and not isinstance(retired, list):
            ap.error("state.json invalid (%s): member %d: retired must be a list or null" % (path, i))
        for j, r in enumerate(retired or []):
            if isinstance(r, dict) and r.get("session") is not None and not isinstance(r["session"], str):
                ap.error("state.json invalid (%s): member %d: retired entry %d: session must be a string or null" % (path, i, j))
    return state                                     # ids and names stay exactly as recorded: they are looked up, compared and requested as they are


KINDS = ("claude", "opencode")
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
SOURCE_ORDER = ("explicit", "replacement record", "derived (handover log)", "replacement log")


class Identity:
    """kind/model/effort of one generation and where the evidence came from (None = unknown)."""

    def __init__(self, kind=None, model=None, effort=None, source="", error=None):
        self.kind, self.model, self.effort, self.source, self.error = kind, model, effort, source, error

    def text(self):
        if self.error:
            return "ambiguous (%s)" % self.error
        return "%s %s effort %s (source: %s)" % (self.kind, disp(self.model or "unknown"), disp(self.effort or "unknown"), self.source)


def _valid(v):
    return isinstance(v, str) and v.strip() != ""


def merge_identity(cands, errors):
    """R6: every applicable candidate must agree; a field is known if any candidate states it."""
    if errors:
        return Identity(error="; ".join(errors))
    if not cands:
        return Identity(error="no identity evidence")
    for field in ("kind", "model", "effort"):
        if len({getattr(c, field) for c in cands if getattr(c, field) is not None}) > 1:
            names = [n for n in SOURCE_ORDER if any(c.source == n for c in cands)]
            return Identity(error="contradictory evidence: " + ", ".join(names))
    known = lambda field: next((getattr(c, field) for c in cands if getattr(c, field) is not None), None)
    source = " + ".join(n for n in SOURCE_ORDER if any(c.source == n for c in cands))
    if known("kind") is None:
        return Identity(error="identity evidence lacks a kind")
    return Identity(known("kind"), known("model"), known("effort"), source)


def resolve_generation(mid, gen, retired, current, log, memo, is_current=False):
    """Identity of generation `gen` of member `mid`: (a) state for the current one, else every
    applicable one of (b) explicit, (c) replacement record, (d) handover log, (e) replacement log."""
    if not is_current and (not isinstance(gen, int) or isinstance(gen, bool)):
        return Identity(error="generation number is missing or not an integer")
    key = "current" if is_current else gen
    if key in memo:
        return memo[key]
    if is_current:
        kind, model, effort = current.get("kind"), current.get("model"), current.get("effort")
        if kind not in KINDS or not _valid(model):
            ident = Identity(error="state kind/model missing or kind outside claude/opencode")
        else:
            ident = Identity(kind, model, effort if _valid(effort) else None, "state")
        memo[key] = ident
        return ident
    entry = retired.get(gen)
    if entry is None:
        memo[key] = Identity(error="generation %s has no record" % gen)
        return memo[key]
    sid = entry.get("session")
    cands, errors = [], []
    explicit = "kind" in entry or "effort" in entry
    if explicit or ("model" in entry and entry.get("reason") != "replaced"):
        kind, model, effort = entry.get("kind"), entry.get("model"), entry.get("effort")
        if (kind is not None and kind not in KINDS) or (model is not None and not _valid(model)) or \
                (effort is not None and not _valid(effort)):
            errors.append("explicit identity fields are malformed")
        else:
            cands.append(Identity(kind, model, effort, "explicit"))
    if entry.get("reason") == "replaced" and not explicit:   # the legacy 'kind model' record, not the explicit schema
        kind, _, model = entry.get("model").partition(" ") if isinstance(entry.get("model"), str) else ("", "", "")
        if kind in KINDS and model.strip():
            cands.append(Identity(kind, model.strip(), None, "replacement record"))
        else:
            errors.append("replacement record model is malformed")
    if "model" not in entry and "reason" not in entry and isinstance(sid, str):
        handover = re.compile(r"^\S+ member %s g%s: handover from %s$" % (re.escape(str(mid)), gen + 1, re.escape(sid)))
        if any(handover.match(l) for l in log) and not any("replaced %s with" % sid in l for l in log):
            nxt = resolve_generation(mid, gen + 1, retired, current, log, memo, is_current=(gen + 1 == current.get("gen")))
            if nxt.error:
                errors.append("handover target g%s is ambiguous" % (gen + 1))
            else:
                cands.append(Identity(nxt.kind, nxt.model, nxt.effort, "derived (handover log)"))
    created = re.compile(r"^\S+ member %s g%s: replaced \S+ with (.*)$" % (re.escape(str(mid)), gen))
    for line in log:
        m = created.match(line)
        if m:
            parts = re.match(r"^(\S+) (\S+)(?: \[([^\]]*)\])?$", m.group(1))
            if parts and parts.group(1) in KINDS:
                cands.append(Identity(parts.group(1), parts.group(2), parts.group(3) or None, "replacement log"))
            else:
                errors.append("replacement log line is malformed")
    memo[key] = merge_identity(cands, errors)
    return memo[key]


def shape_error(kind, sid):
    if kind == "opencode" and not sid.startswith("ses"):
        return "session id shape contradicts kind opencode: expected ses..."
    if kind == "claude" and not UUID_RE.match(sid):
        return "session id shape contradicts kind claude: expected a UUID"
    return None


def read_transcript(g, path):
    s = g.session = load_claude(path, g.label)
    keep, _ = attributable(s)
    parts = ["%d call(s)" % len(s.calls), "%d council call(s)" % len(s.segments),
             "%d unparseable line(s)" % s.bad_lines]
    if s.no_id:
        parts.append("%d assistant line(s) without message.id kept individually" % s.no_id)
    if len(keep) < len(s.calls):
        parts.append("%d call(s) with incomplete input-side usage excluded from attribution" % (len(s.calls) - len(keep)))
    if s.invalid_usage:
        parts.append("%d invalid usage value(s) treated as unknown" % s.invalid_usage)
    if s.usage_conflicts:
        parts.append("%d usage conflict(s) (component unknown for that call)" % s.usage_conflicts)
    if s.repeated_blocks:
        parts.append("%d repeated identical content block(s) under one message.id counted once "
                     "(attribution uncertain)" % s.repeated_blocks)
    if s.compaction:
        parts.append("compaction marker present (schema and token effect not interpreted): %d line(s)" % s.compaction)
    if s.other_types:
        parts.append("unlisted line types: " + ", ".join("%s x%d" % kv for kv in sorted(s.other_types.items())))
    drops = context_drops(keep)
    if drops:
        parts.append("%d context drop > 30%% (heuristic; cause unknown, compaction not proven) at call(s) %s"
                     % (len(drops), ", ".join(str(keep[k]["i"]) for k in drops)))
    g.detail = "; ".join(parts)
    if s.bad_lines:
        g.status = "PARTIAL"


def read_discovered(g, path):
    """Read a transcript found under --projects. A file that cannot be read (permissions, a directory
    with that name) is a secondary-source failure: the reason is returned and the run continues."""
    try:
        read_transcript(g, path)
    except OSError as e:
        g.session = None
        return "transcript unreadable: %s" % (e.strerror or type(e).__name__)
    return None


def all_models(s):
    """The one model named by every assistant line, or None (also when two lines of one message.id differ)."""
    model = next(iter(s.models)) if len(s.models) == 1 else None
    return model if _valid(model) else None


def fetch_pending(pending, oc):
    """--fetch-opencode: one pinned connection for status and every api call (plan section 3). A session id that
    cannot be written as UTF-8 (a lone surrogate) cannot be requested: it is never sent, never replaced, and its
    generation keeps its recorded totals."""
    requestable = []
    for g in pending:
        try:
            g.sid.encode("utf-8")
        except UnicodeEncodeError:
            g.source, g.status, g.detail = "fetched messages", "unavailable", "recorded totals kept"
            g.reason = "session id is not valid UTF-8 (a lone surrogate); not requested"
        else:
            requestable.append(g)
    pending = requestable
    if not pending:
        return
    env, reason = connection_env(os.environ)
    if env is None:
        for g in pending:
            g.source, g.status, g.reason, g.detail = "fetched messages", "unavailable", reason, "recorded totals kept"
        return
    try:
        rc, out, err = run_oc(oc, ["status"], env)
        failure = None if rc == 0 else "status exit %d: %s" % (rc, first_line(out + " " + err))
    except OcError as e:
        failure = str(e)
    if failure:
        for g in pending:
            g.source, g.status, g.reason = "fetched messages", "unavailable", "service not running or adapter failed; not started"
            g.detail = "%s; recorded totals kept" % failure
        return
    for g in pending:
        raw, pages, partial = fetch_messages(oc, env, g.sid)
        msgs, info, types, conflicted = clean_messages(raw)
        s = g.session = opencode_session(g.label, msgs, conflicted)
        g.source, g.status, g.reason = "fetched messages", "complete", ""
        parts = ["%d message(s) in %d page(s)" % (len(raw), pages)]
        if info["duplicate"]:
            parts.append("%d duplicate message(s) counted once" % info["duplicate"])
        if info["conflict"]:
            parts.append("%d conflicting duplicate field(s) treated as unknown" % info["conflict"])
        if info["no_id"]:
            parts.append("%d message(s) without id kept individually" % info["no_id"])
        if info["unrecognized"]:
            parts.append("%d message(s) with unrecognized schema (not interpreted)" % info["unrecognized"])
            g.status = "PARTIAL"
        if s.odd:
            parts.append("%d message element(s) with unrecognized schema (not interpreted)" % s.odd)
            g.status = "PARTIAL"
        if types:
            parts.append("unlisted message types: " + ", ".join("%s x%d" % kv for kv in sorted(types.items())))
        keep, _ = attributable(s)
        if len(keep) < len(s.calls):
            parts.append("%d call(s) with incomplete input-side usage excluded from attribution" % (len(s.calls) - len(keep)))
        if partial:
            g.status = "PARTIAL"
            parts.append("stopped: " + partial)
        g.detail = "; ".join(parts)


def transcript_hits(projects, sid):
    """The files `<projects>/<any project directory>/<sid>.jsonl`, looked up literally: only the project directory is a
    wildcard, so glob characters in `projects` or in the id are plain characters, and an id with a path separator
    names no file of that shape (it could only reach a deeper or a higher path)."""
    if os.sep in sid:
        return []
    return sorted(glob.glob(os.path.join(glob.escape(projects), "*", glob.escape(sid) + ".jsonl")))


def gens_from_run(state, projects, oc, fetch=False):
    out, seen, pending, shared = [], {}, [], []
    log = [l for l in state["log"] if isinstance(l, str)] if isinstance(state.get("log"), list) else []
    for m in state.get("members", []):
        if not isinstance(m, dict):
            continue
        records = [r for r in m.get("retired") or [] if isinstance(r, dict)]
        retired = {r["gen"]: r for r in records if isinstance(r.get("gen"), int) and not isinstance(r.get("gen"), bool)}
        entries = [(r.get("session"), r.get("gen"), r.get("tokens"), r.get("cost"), False) for r in records]
        entries.append((m.get("session"), m.get("gen"), m.get("session_tokens"), m.get("session_cost"), True))
        memo = {}
        for sid, gen, tokens, cost, is_current in entries:
            if not sid:
                continue
            ident = resolve_generation(m.get("id"), gen, retired, m, log, memo, is_current)
            if not ident.error:
                ident = Identity(ident.kind, ident.model, ident.effort, ident.source,
                                 shape_error(ident.kind, str(sid)))
            g = Gen(disp("%s g%s %s" % (m.get("id"), gen, sid)), sid, ident.kind, "local transcript")
            g.tokens, g.cost, g.identity, g.member = num(tokens), num(cost), ident, m.get("id")
            out.append(g)
            if sid in seen:
                first = seen[sid]
                reason = "same session id in %s and %s; read once" % (first.label.rsplit(" ", 1)[0], g.label.rsplit(" ", 1)[0])
                for x in (first, g):
                    x.status, x.reason = "conflict", reason
                shared.append((first, reason))
                continue
            seen[sid] = g
            hits = transcript_hits(projects, str(sid))
            if ident.error:
                g.source, g.status, g.reason = "state totals", "ambiguous", ident.error
                if len(hits) == 1:
                    failure = read_discovered(g, hits[0])
                    if failure:
                        g.detail = "%s; recorded totals kept" % failure
                    else:
                        g.source, g.reason = "local transcript", ""
                        g.status = "PARTIAL" if g.session.bad_lines else "complete"
                        g.identity = Identity("claude", all_models(g.session), None, "local transcript")
                continue
            if ident.kind == "opencode":
                g.source, g.status, g.reason = "state totals", "unknown (not fetched)", ""
                g.detail = "per-call breakdown unknown (not fetched; use --fetch-opencode)"
                pending.append(g)
            elif len(hits) > 1:
                g.status, g.reason = "ambiguous", "several transcripts with this session id; none read"
            elif hits:
                failure = read_discovered(g, hits[0])
                if failure:
                    g.status, g.reason = "unavailable", failure
            else:
                g.status, g.reason = "unavailable", "transcript not found under %s" % projects
    if fetch and pending:
        fetch_pending(pending, oc)
        for first, reason in shared:   # the outcome of the single fetch must not erase the conflict label
            if first.status != "conflict":
                outcome = first.status_text()
                first.status, first.reason = "conflict", reason
                first.detail = "; ".join(x for x in (first.detail, "outcome of the single read: " + outcome) if x)
    return out


def gens_from_files(paths):
    out = []
    for p in paths:
        g = Gen(disp(os.path.basename(p)), p, "claude", "file transcript")
        read_transcript(g, p)
        out.append(g)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0],
                                 epilog="Local by default: without --fetch-opencode no subprocess is started at all.")
    ap.add_argument("--run-dir", help="a council run directory (state.json, prompts/)")
    ap.add_argument("--session", nargs="+", help="Claude Code transcript file(s)")
    ap.add_argument("--projects", default=os.path.join(os.path.expanduser("~"), ".claude", "projects"),
                    help="where Claude transcripts live (default ~/.claude/projects)")
    ap.add_argument("--oc", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "oc.sh"),
                    help="the oc.sh adapter; only selects it, collection needs --fetch-opencode")
    ap.add_argument("--chars-per-token", type=float, default=3.2, help="finite and > 0 (default 3.2)")
    ap.add_argument("--top", type=int, default=15, help="rows per table, >= 1 (default 15)")
    ap.add_argument("--fetch-opencode", action="store_true",
                    help="collect OpenCode messages through oc.sh (requires --run-dir; never starts the service)")
    a = ap.parse_args(argv)
    try:                # the report is UTF-8 whatever the locale is; a lone surrogate is written as a visible \udXXX escape
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    except (AttributeError, ValueError, OSError):
        pass
    if (a.run_dir is None) == (a.session is None):
        ap.error("give exactly one of --run-dir or --session")
    if not math.isfinite(a.chars_per_token) or a.chars_per_token <= 0:
        ap.error("--chars-per-token must be a finite number > 0")
    if a.top < 1:
        ap.error("--top must be >= 1")
    if a.fetch_opencode and a.run_dir is None:
        ap.error("--fetch-opencode requires --run-dir")
    state = None
    if a.run_dir is not None:
        state = read_state(ap, a.run_dir)
        gens = gens_from_run(state, a.projects, a.oc, a.fetch_opencode)
    else:
        for p in a.session:
            try:
                with open(p, "rb"):
                    pass
            except OSError as e:
                ap.error("cannot read --session file %s: %s" % (p, e))
        gens = gens_from_files(a.session)
    if not any(g.session and g.session.calls or g.tokens is not None or g.cost is not None for g in gens):
        ap.error("no usable session: no generation with recorded totals or per-call data")
    report(gens, a.chars_per_token, a.top, state, a.run_dir)


if __name__ == "__main__":
    main()
