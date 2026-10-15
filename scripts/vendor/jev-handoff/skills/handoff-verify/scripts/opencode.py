"""OpenCode v2 session adapter for handoff-verify. Stdlib only, read-only, in memory: it reads ONE OpenCode v2 session (SQLite tables `session_v2` + `session_message`, opened with a `file:` URI
`mode=ro` and `PRAGMA query_only=ON`) and returns the Claude-shaped record stream the skill already parses (assistant `tool_use` / user `tool_result` / text records). It never executes or evaluates
anything it reads (no JavaScript, no patch, no command), writes no export, cache or intermediate file, and is reached only through an explicit selector:
  opencode:<session-id>                              the default database ~/.local/share/opencode/opencode.db
  opencode-db:<absolute-db-path>#<session-id>        a database at an absolute path (split at the FINAL `#`)
Canonical identity of a selector: `opencode-db:<realpath(db)>#<session-id>`.

Mapping (everything else is not part of the stream: reasoning, synthetic/system/idle/compaction/agent/model messages, child sessions, webfetch/... tools):
  tool `write`  -> Write{file_path, content}          tool `edit` -> Edit{file_path, old_string, new_string, replace_all}      tool `shell`/`bash` -> Bash{command}
  tool `patch`  -> one Write/Edit per section, ids `<call id>#<k>`: `*** Add File` -> Write; `*** Update File` -> Edit only when an earlier successful write/add/exact edit supplies the base and every hunk's old
                   block occurs exactly once, in order, without overlap; Delete, Move, non-exact/ambiguous/malformed hunks, unknown base, conflicting paths, concurrent mutation of the same path ->
                   `_unrecoverable` marker (reason) which discover.inventory / slice.reconstruct turn into a version `content not recoverable`
  tool `read` / `grep` / `glob` -> Read / Grep / Glob: ordinary EVIDENCE only when the call has valid top-level tool times (created and completed, no message-time fallback) and finished (completed or error): the recorded
                   input and output (sanitised: secrets redacted like prepare.py does), `is_error`, and a truncation marker when `state.metadata.truncated` is true (also on an errored call: both facts are kept). No structured `toolUseResult` is made, so discover.reads
                   lists them as incomplete: an OpenCode read result is never an edit base and never enters the per-path state of the adapter.
  Jev: a completed-or-failed direct `jev:jev_<tool>` / `jev_<tool>` -> `mcp__jev__jev_<tool>`; an `execute` call only under the single-inner-call rule (see `_jev_execute`), otherwise it is not in the stream.
Timing: a tool_use is placed at its block's TOP-LEVEL `time.created`, its tool_result at `time.completed` (never `state.time`, never `time.ran`); events are sorted by (timestamp, use before result, message seq,
content index, section index) because the version/Jev window checks use stream order. A call without valid timing gets no result (never a successful one).
Diagnostics: a SUCCESSFUL write/edit/patch call that cannot be placed (no valid top-level time.created/completed) or attributed (no path) is not an event, a time or a version, but it is returned by `diagnostics(selector)`
(`load(...)[1]`): {call_id, reason, path (realpath | None), seq, ci, sub, created, completed (the real top-level times when valid), kind}. versions.reconstruct_stream marks every version of an affected path (every path for
a pathless note) as `unpositioned`, which blocks the current-delivery certificate; failed operations are not diagnostics."""
import datetime, hashlib, json, os, re, sqlite3, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from urllib.parse import quote

SELECTORS = ("opencode:", "opencode-db:")
ID_RX = re.compile(r"^[A-Za-z0-9_.\-]+$")
JEV_DIRECT = re.compile(r"^(?:jev:)?(jev_[a-z0-9_]+)$")
JEV_INNER = re.compile(r"^jev\.(jev_[a-z0-9_]+)$")
HEADER = re.compile(r"^\*\*\* (Add File|Update File|Delete File|Move to): (.+)$")
EVIDENCE = {"read": "Read", "grep": "Grep", "glob": "Glob"}
TRUNCATED = "\n[output truncated: the recorded metadata marks this output as incomplete]"

class OpenCodeError(OSError):
    """The selector or the session cannot be used (unreadable, missing, malformed, not a v2 session). An OSError, so callers that treat an unreadable transcript file keep working."""

def default_db(): return os.path.expanduser("~/.local/share/opencode/opencode.db")

def is_selector(s):
    return isinstance(s, str) and s.startswith(SELECTORS)

def parse(s):
    """-> (db path, session id); OpenCodeError for anything that is not exactly one of the two selector forms."""
    if not is_selector(s): raise OpenCodeError("not an OpenCode selector: %r" % (s,))
    if s.startswith("opencode-db:"):
        rest = s[len("opencode-db:"):]; i = rest.rfind("#")
        if i < 0: raise OpenCodeError("opencode-db selector needs `#<session-id>`: %s" % s)
        db, sid = rest[:i], rest[i + 1:]
        if not os.path.isabs(db): raise OpenCodeError("opencode-db selector needs an ABSOLUTE database path: %s" % s)
    else: db, sid = default_db(), s[len("opencode:"):]
    if not sid or not ID_RX.match(sid) or sid in (".", ".."): raise OpenCodeError("invalid OpenCode session id in %r" % s)
    return db, sid

def canonical(s):
    db, sid = parse(s)
    return "opencode-db:%s#%s" % (os.path.realpath(db), sid)

def readonly_uri(db):
    return "file:%s?mode=ro" % quote(os.path.realpath(db))

def _connect(db):
    if not os.path.isfile(db): raise OpenCodeError("OpenCode database not found: %s" % db)
    try:
        con = sqlite3.connect(readonly_uri(db), uri=True); con.execute("PRAGMA query_only=ON"); return con
    except sqlite3.Error as e: raise OpenCodeError("OpenCode database cannot be opened read-only: %s (%s)" % (db, e))

_SCOPE = None   # while a snapshot scope is open, the stored rows of a session are read ONCE (identity and parsing of one candidate come from the same snapshot)

class snapshot_scope:
    """`with snapshot_scope():` -> each session is read from the database once inside the block (a memo that lives only as long as the block, never across candidates); nested scopes share the outer one."""
    def __enter__(self):
        global _SCOPE; self.prev = _SCOPE; _SCOPE = {} if self.prev is None else self.prev; return self
    def __exit__(self, *a):
        global _SCOPE; _SCOPE = self.prev

def _rows(s):
    """-> (session row, [(message id, type, seq, data) as stored]) of ONE session, read-only. The raw rows are the snapshot: the identity hashes them and `_read` parses them."""
    db, sid = parse(s); key = canonical(s) if _SCOPE is not None else None
    if key is not None and key in _SCOPE: return _SCOPE[key]
    con = _connect(db)
    try:
        try:
            row = con.execute("SELECT id, directory FROM session_v2 WHERE id = ?", (sid,)).fetchone()
            if row is None: raise OpenCodeError("OpenCode v2 session not found in %s: %s (legacy sessions are not supported)" % (db, sid))
            msgs = con.execute("SELECT id, type, seq, data FROM session_message WHERE session_id = ? ORDER BY seq, id", (sid,)).fetchall()
        except sqlite3.Error as e: raise OpenCodeError("unsupported OpenCode database (no v2 session tables?): %s (%s)" % (db, e))
    finally: con.close()
    snap = (dict(id=row[0], directory=row[1]), msgs)
    if key is not None: _SCOPE[key] = snap
    return snap

def _read(s):
    sess, rows = _rows(s); sid = sess["id"]
    out = []
    for mid, typ, seq, data in rows:
        try: d = json.loads(data)
        except (TypeError, ValueError): raise OpenCodeError("malformed message data in session %s (seq %s)" % (sid, seq))
        if not isinstance(d, dict): raise OpenCodeError("malformed message data in session %s (seq %s)" % (sid, seq))
        out.append((mid, typ, seq, d))
    return dict(sess), out

def exists(s):
    try: _read(s); return True
    except OSError: return False

def content_sha256(s):
    """sha256 of the stored CONTENT of the session (its row and its messages, as stored), read-only. The size, the mtime and the WAL of the database are not part of it: only what the skill reads decides the identity,
    a change of another session of the same database does not move it, and a same-size change with restored timestamps does."""
    sess, rows = _rows(s); h = hashlib.sha256()
    h.update(json.dumps([sess["id"], sess["directory"]], ensure_ascii=False).encode("utf-8"))
    for mid, typ, seq, data in rows:
        h.update(json.dumps([mid, typ, seq], ensure_ascii=False).encode("utf-8")); h.update(b"\x00"); h.update(data if isinstance(data, bytes) else str(data).encode("utf-8")); h.update(b"\x01")
    return h.hexdigest()

def fingerprint(s):
    """Identity of the stored session for memoization: ("sha256", canonical selector, sha256 of its stored content)."""
    return ("sha256", canonical(s), content_sha256(s))

def session_id(s): return parse(s)[1]

def _ms(v): return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None
def _iso(ms): return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
def _text_of(parts):
    return "".join(p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str)) if isinstance(parts, list) else ""

def _san(v):
    """Sanitise every string of a recorded value (secrets redacted exactly like prepare.py); other values are kept."""
    import sanitize
    if isinstance(v, str): return sanitize.sanitize(v)[0]
    if isinstance(v, dict): return {k: _san(x) for k, x in v.items()}
    if isinstance(v, list): return [_san(x) for x in v]
    return v

def _times(c):
    """(created, completed) as recorded, each only when valid; a completion before the creation is malformed."""
    cr, co = c["created"], c["completed"]
    return cr, (co if co is not None and (cr is None or co >= cr) else None)

def _diag(c, uid, reason, path, kind, sub=0):
    cr, co = _times(c)
    return dict(call_id=uid, reason=reason, path=path, seq=c["seq"], ci=c["ci"], sub=sub, created=cr, completed=co, kind=kind)

def _abs(p, cwd):
    if not isinstance(p, str) or not p: return None
    return os.path.normpath(p if os.path.isabs(p) else os.path.join(cwd, p))

def parse_patch(patch_text):
    """-> (sections | None, error). Strict grammar: `*** Begin Patch`, sections `*** Add File: P` (only `+` lines), `*** Update File: P` [`*** Move to: Q`] with `@@[ hint]` hunks of ` `/`-`/`+` lines,
    `*** Delete File: P`, `*** End Patch`. Anything else is malformed (never repaired). section = dict(kind add|update|delete, path, move_to, lines | hunks=[(old_lines, new_lines)])."""
    if not isinstance(patch_text, str): return None, "patchText is not a string"
    lines = patch_text.split("\n")
    while lines and lines[-1] == "": lines.pop()
    if len(lines) < 2 or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch": return None, "missing Begin/End Patch markers"
    body, secs, i = lines[1:-1], [], 0
    while i < len(body):
        m = HEADER.match(body[i])
        if not m or m.group(1) == "Move to": return None, "unexpected line %r" % body[i][:40]
        kind, path = {"Add File": "add", "Update File": "update", "Delete File": "delete"}[m.group(1)], m.group(2); i += 1; sec = dict(kind=kind, path=path, move_to=None)
        if kind == "update" and i < len(body) and body[i].startswith("*** Move to: "):
            mv = HEADER.match(body[i]); sec["move_to"] = mv.group(2) if mv else None; i += 1
            if not sec["move_to"]: return None, "bad Move to line"
        j = i
        while j < len(body) and not body[j].startswith("*** "): j += 1
        chunk = body[i:j]; i = j
        if kind == "delete":
            if chunk: return None, "Delete File section with a body"
        elif kind == "add":
            if any(not l.startswith("+") for l in chunk): return None, "Add File line without `+`"
            sec["lines"] = [l[1:] for l in chunk]
        else:
            hunks, cur = [], None
            for l in chunk:
                if l == "@@" or l.startswith("@@ "): cur = ([], []); hunks.append(cur)
                elif cur is None or l[:1] not in (" ", "-", "+"): return None, "malformed hunk line %r" % l[:40]
                elif l[0] == " ": cur[0].append(l[1:]); cur[1].append(l[1:])
                elif l[0] == "-": cur[0].append(l[1:])
                else: cur[1].append(l[1:])
            sec["hunks"] = hunks
        secs.append(sec)
    if not secs: return None, "no sections"
    return secs, None

def _unique_at(base, block, start):
    """Index of `block` in `base` at/after `start` when it occurs exactly once in the WHOLE base (overlapping occurrences count), else None."""
    i = base.find(block)
    return i if i >= start and base.find(block, i + 1) == -1 else None

def apply_hunks(base, hunks):
    """-> (old_span, new_span, why). Exact application: every hunk has old lines, its old block occurs exactly once in the base, hunks are in order and do not overlap."""
    if not hunks: return None, None, "Update File section without hunks"
    pos, spans = 0, []
    for old_lines, new_lines in hunks:
        if not old_lines: return None, None, "hunk without old lines (no exact anchor)"
        old = "".join(l + "\n" for l in old_lines); at = _unique_at(base, old, pos)
        if at is None: return None, None, "hunk is not exact (old block absent, ambiguous or out of order/overlapping)"
        spans.append((at, at + len(old), "".join(l + "\n" for l in new_lines))); pos = at + len(old)
    first, last = spans[0][0], spans[-1][1]; new = ""; cur = first
    for a, b, n in spans: new += base[cur:a] + n; cur = b
    return base[first:last], new, None

def _jev_execute(st):
    """The single-inner-call rule: the outer call completed, not truncated, no error flag; `metadata.toolCalls` has exactly one entry, `jev.jev_<tool>`, completed, with an object input; and the returned
    JSON object (priority: state.structured, state.result, then the concatenated state.content) has `tool` == the inner tool name. -> (tool, input, payload) | None. The code text is never inspected."""
    if not isinstance(st, dict) or st.get("status") != "completed": return None
    md = st.get("metadata")
    if not isinstance(md, dict) or md.get("truncated") is not False or md.get("error") not in (None, False): return None
    tc = md.get("toolCalls")
    if not isinstance(tc, list) or len(tc) != 1 or not isinstance(tc[0], dict): return None
    m = JEV_INNER.match(tc[0].get("tool") if isinstance(tc[0].get("tool"), str) else "")
    if not m or tc[0].get("status") != "completed" or not isinstance(tc[0].get("input"), dict): return None
    payload = st.get("structured") if isinstance(st.get("structured"), dict) else st.get("result") if isinstance(st.get("result"), dict) else None
    if payload is None:
        try: payload = json.loads(_text_of(st.get("content")))
        except ValueError: return None
    if not isinstance(payload, dict) or payload.get("tool") != m.group(1): return None
    return m.group(1), tc[0]["input"], payload

def load(s):
    """-> (records, notes). records = the Claude-shaped stream; notes = successful mutation calls that cannot be attributed to any path [{call_id, reason}]. OpenCodeError when the session cannot be read."""
    sess, msgs = _read(s); sid = sess["id"]
    cwd = next((d["previous"]["location"]["directory"] for _, t, _, d in msgs if t == "location-switched" and isinstance(d.get("previous"), dict) and isinstance(d["previous"].get("location"), dict)
                and isinstance(d["previous"]["location"].get("directory"), str)), None) or sess["directory"]
    texts, calls = [], []
    for mid, typ, seq, d in msgs:
        mt = _ms((d.get("time") or {}).get("created")) if isinstance(d.get("time"), dict) else None
        if typ == "location-switched":
            loc = d.get("location"); cwd = loc["directory"] if isinstance(loc, dict) and isinstance(loc.get("directory"), str) else cwd
        elif typ == "user" and isinstance(d.get("text"), str) and mt is not None: texts.append(dict(mid=mid, seq=seq, ci=0, role="user", text=d["text"], ts=mt, cwd=cwd))
        elif typ == "assistant" and isinstance(d.get("content"), list):
            for ci, b in enumerate(d["content"]):
                if not isinstance(b, dict): continue
                if b.get("type") == "text" and isinstance(b.get("text"), str) and mt is not None: texts.append(dict(mid=mid, seq=seq, ci=ci, role="assistant", text=b["text"], ts=mt, cwd=cwd))
                elif b.get("type") == "tool" and isinstance(b.get("id"), str) and isinstance(b.get("name"), str) and isinstance(b.get("state"), dict):
                    tm = b.get("time") if isinstance(b.get("time"), dict) else {}; cr, co = _ms(tm.get("created")), _ms(tm.get("completed"))
                    calls.append(dict(mid=mid, seq=seq, ci=ci, id=b["id"], name=b["name"], st=b["state"], cwd=cwd, created=cr, completed=co))
    seen, ops, notes = {}, [], []
    for c in calls:   # unique ids in stream order of the calls
        n = seen[c["id"]] = seen.get(c["id"], 0) + 1; c["uid"] = c["id"] if n == 1 else "%s~%d" % (c["id"], n)
    for c in calls:
        st, name = c["st"], c["name"]; status = st.get("status"); inp = st.get("input") if isinstance(st.get("input"), dict) else {}
        res_ok = c["created"] is not None and c["completed"] is not None and c["completed"] >= c["created"] and status in ("completed", "error")
        base = dict(call=c, use_ts=c["created"], res_ts=c["completed"] if res_ok else None, is_error=status != "completed", done=status == "completed", ok=status == "completed" and res_ok,
                    out=_text_of(st.get("content")) if status == "completed" else ((st.get("error") or {}).get("message") if isinstance(st.get("error"), dict) else None) or "error")
        if name in ("write", "edit"):
            paths = sorted({_abs(p, c["cwd"]) for p in (inp.get("filePath"), inp.get("path")) if isinstance(p, str) and p})
            if not paths:
                if base["done"]: notes.append(_diag(c, c["uid"], "%s without a path (target impact unknown)" % name, None, "pathless"))
                continue
            for k, p in enumerate(paths, 1): ops.append(dict(base, kind=name, path=p, uid=c["uid"], sub=k, args=inp, conflict=len(paths) > 1))
        elif name == "patch":
            if not base["done"]: continue   # a failed/unfinished patch creates no versions
            secs, err = parse_patch(inp.get("patchText")); k = [0]
            def add(kind, path, **kw): k[0] += 1; ops.append(dict(base, kind=kind, path=path, uid="%s#%d" % (c["uid"], k[0]), sub=k[0], **kw))
            if secs is None:
                text = inp.get("patchText") if isinstance(inp.get("patchText"), str) else ""
                paths = sorted({_abs(m.group(2), c["cwd"]) for l in text.split("\n") for m in [HEADER.match(l)] if m})
                if not paths: notes.append(_diag(c, c["uid"], "malformed patch without any path: %s (target impact unknown)" % err, None, "pathless"))
                for p in paths: add("patch-bad", p, reason="malformed patch (%s): content not recoverable" % err)
                continue
            for sec in secs:
                p = _abs(sec["path"], c["cwd"])
                if sec["move_to"]:
                    for q in dict.fromkeys([p, _abs(sec["move_to"], c["cwd"])]): add("patch-bad", q, reason="patch moves a file (Move to): content not recoverable")
                elif sec["kind"] == "delete": add("patch-bad", p, reason="patch deletes the file: content not recoverable")
                else: add("patch-" + sec["kind"], p, sec=sec)
        elif name in EVIDENCE:
            if res_ok: ops.append(dict(base, kind="evidence", uid=c["uid"], sub=0, args=inp))
        elif name in ("shell", "bash"):
            if isinstance(inp.get("command"), str): ops.append(dict(base, kind="bash", uid=c["uid"], sub=0, args=inp))
        elif JEV_DIRECT.match(name): ops.append(dict(base, kind="jev", uid=c["uid"], sub=0, tool=JEV_DIRECT.match(name).group(1), args=inp))
        elif name == "execute":
            r = _jev_execute(st)
            if r and res_ok: ops.append(dict(base, kind="jev", uid=c["uid"], sub=0, tool=r[0], args=r[1], out=json.dumps(r[2], ensure_ascii=False), is_error=False))
    _decide_mutations(ops)
    events = []
    for t in texts:
        rec = dict(type=t["role"], uuid=t["mid"] + (":%d" % t["ci"] if t["role"] == "assistant" else ""), timestamp=_iso(t["ts"]), cwd=t["cwd"], sessionId=sid, _adapter="opencode",
                   message=dict(role=t["role"], content=[dict(type="text", text=t["text"])]))
        events.append(((t["ts"], 0, t["seq"], t["ci"], 0), rec))
    for o in ops:
        c = o["call"]; name, inp = o["emit_name"], o["emit_input"]
        if o["use_ts"] is None: continue   # no valid top-level tool time.created: no positioned event (the message time is never a substitute)
        use = dict(type="assistant", uuid="%s:%d" % (c["mid"], c["ci"]) + ("#%d" % o["sub"] if o["sub"] else ""), timestamp=_iso(o["use_ts"]), cwd=c["cwd"], sessionId=sid, _adapter="opencode",
                   message=dict(role="assistant", content=[dict(type="tool_use", id=o["uid"], name=name, input=inp)]))
        events.append(((o["use_ts"], 0, c["seq"], c["ci"], o["sub"]), use))
        if o["res_ts"] is not None:
            events.append(((o["res_ts"], 1, c["seq"], c["ci"], o["sub"]), dict(type="user", uuid=use["uuid"] + ":result", timestamp=_iso(o["res_ts"]), cwd=c["cwd"], sessionId=sid, _adapter="opencode",
                           message=dict(role="user", content=[dict(type="tool_result", tool_use_id=o["uid"], content=o["out"], is_error=bool(o["is_error"]))]))))
    events.sort(key=lambda e: e[0])
    recs = []
    for i, (_, rec) in enumerate(events, 1): rec["_line"] = i; recs.append(rec)
    return recs, notes + [n for o in ops for n in o.get("notes", [])]

def records(s): return load(s)[0]

def diagnostics(s):
    """The successful mutations of the session that could not be placed or attributed (see the module docstring): [{call_id, reason, path, seq, ci, sub, created, completed, kind}]."""
    return load(s)[1]

def _skeleton(name, path):
    return dict(file_path=path, content=None) if name == "Write" else dict(file_path=path, old_string="", new_string="", replace_all=False)

def _plain(o):
    a = o["args"]
    if o["kind"] == "write": return dict(file_path=o["path"], content=a.get("content") if isinstance(a.get("content"), str) else None)
    return dict(file_path=o["path"], old_string=a.get("oldString") if isinstance(a.get("oldString"), str) else "", new_string=a.get("newString") if isinstance(a.get("newString"), str) else "", replace_all=a.get("replaceAll") is True)

def _decide_mutations(ops):
    """Fill emit_name/emit_input of every op. Mutations are walked in stream order with an adapter-local per-path content state (never OpenCode reads, never the disk)."""
    for o in ops:
        if o["kind"] == "bash": o["emit_name"], o["emit_input"] = "Bash", dict(command=o["args"]["command"])
        elif o["kind"] == "jev": o["emit_name"], o["emit_input"] = "mcp__jev__" + o["tool"], o["args"]
        elif o["kind"] == "evidence":   # read/grep/glob: ordinary, sanitised evidence; no structure, no state
            o["emit_name"] = EVIDENCE[o["call"]["name"]]; inp = _san(o["args"])
            if o["emit_name"] == "Read" and "filePath" in inp: inp["file_path"] = inp.pop("filePath")
            o["emit_input"] = inp; md = o["call"]["st"].get("metadata")
            o["out"] = _san(o["out"]) + (TRUNCATED if isinstance(md, dict) and md.get("truncated") is True else "")   # the marker is kept with the error too: a failed call can still have cut its output
    muts = [o for o in ops if o["kind"] not in ("bash", "jev", "evidence")]
    for o in muts: o["key"] = os.path.realpath(o["path"]); o["concurrent"] = False
    # successful mutations of the same path from DIFFERENT calls whose [created, completed] intervals overlap have no demonstrable order
    for a in (o for o in muts if o["ok"]):
        for b in (o for o in muts if o["ok"] and o["call"] is not a["call"] and o["key"] == a["key"]):
            if a["call"]["created"] < b["call"]["completed"] and b["call"]["created"] < a["call"]["completed"]: a["concurrent"] = True
    state = {}
    # a completed mutation without a valid top-level time.created has no position: the base of its path is unknown after every mutation that is not demonstrably later than its recorded completion
    blind = {}
    for o in muts:
        if o["done"] and o["use_ts"] is None: blind.setdefault(o["key"], []).append(o["call"]["completed"])
    def step(o):
        p, k, key = o["path"], o["kind"], o["key"]
        o["emit_name"] = "Write" if k in ("write", "patch-add") else "Edit"
        o["emit_input"] = _plain(o) if k in ("write", "edit") else _skeleton(o["emit_name"], p)
        def mark(why): o["emit_input"] = dict(_skeleton(o["emit_name"], p), _unrecoverable=why); state[key] = None
        if not o["done"]: return   # failed/unfinished: nothing changed
        if not o["ok"]:   # completed but without valid timing: no successful result, and the base of the path is no longer known
            state[key] = None; o.setdefault("notes", []).append(_diag(o["call"], o["uid"], "timing missing or malformed: no successful result", key, "untimed", o["sub"])); return
        if k == "patch-bad": mark(o["reason"])
        elif o.get("conflict"): mark("conflicting filePath/path values in the call: content not recoverable")
        elif o["concurrent"]: mark("concurrent mutation of the same path: no demonstrable order, content not recoverable")
        elif k in ("write", "patch-add"):
            content = o["args"].get("content") if k == "write" else "".join(l + "\n" for l in o["sec"]["lines"])
            if not isinstance(content, str): mark("write without string content: content not recoverable")
            else: o["emit_input"] = dict(file_path=p, content=content); state[key] = content
        elif k == "edit":
            old, new = o["args"].get("oldString"), o["args"].get("newString")
            if not isinstance(old, str) or not isinstance(new, str) or old == "": mark("edit without exact oldString/newString: content not recoverable"); return
            ra = o["args"].get("replaceAll") is True; o["emit_input"] = dict(file_path=p, old_string=old, new_string=new, replace_all=ra); cur = state.get(key)
            if cur is None or old not in cur or (cur.count(old) > 1 and not ra): state[key] = None
            else: state[key] = cur.replace(old, new) if ra else cur.replace(old, new, 1)
        else:   # patch-update
            cur = state.get(key)
            if cur is None: mark("patch Update File without a known base (no earlier successful write/add/exact edit in this session): content not recoverable"); return
            old_span, new_span, why = apply_hunks(cur, o["sec"]["hunks"])
            if why: mark(why + ": content not recoverable"); return
            o["emit_input"] = dict(file_path=p, old_string=old_span, new_string=new_span, replace_all=False); state[key] = cur.replace(old_span, new_span, 1)

    # an unpositioned (no valid time.created) mutation sorts first by (message seq, content index): its effect is only the conservative base reset above, never an emitted event. The base is restored by POSITIVE temporal
    # evidence only (the tool time of the mutation is strictly later than the valid completion of every uncertain one, exactly like versions._restores), never by the order of the messages
    for o in sorted(muts, key=lambda o: (o["use_ts"] if o["use_ts"] is not None else float("-inf"), o["call"]["seq"], o["call"]["ci"], o["sub"])):
        step(o)
        if o["key"] in blind and not all(isinstance(c, int) and o["use_ts"] is not None and o["use_ts"] > c for c in blind[o["key"]]): state[o["key"]] = None
