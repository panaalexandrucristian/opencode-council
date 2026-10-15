#!/usr/bin/env python3
"""opencode_source.py: one OpenCode session (SQLite, tables session_v2 + session_message of OpenCode 2.0.x) as the Claude-shaped text that compact_context.py already processes.
Used by make_context.py (--opencode DB [--session ID], --current, which also searches the OpenCode database, and --list-opencode, which lists the sessions: list_sessions / session_line). Stdlib only; the database is opened read-only (file: URI, mode=ro, PRAGMA query_only) and never written.

Every record is  [uuid role] text  with a uuid5 of the OpenCode message id and an index, so REC / kind_of / compact_text / the round trip work unchanged:
  user text and assistant text parts          whole, one record each
  a tool call                                 assistant record  [tool_use NAME] key: value lines  (raw strings: no JSON escaping; nested values as path[0].sub: value)
  its result                                  user record  [tool_result] text  /  [tool_result ERROR] type: message  /  (no output; status ...), then a last line  (exit N, truncated)  from state.metadata only
  an image / file block of a result           its own user record  [tool_result file] mime: / name: / uri:  (NOT sanitized; make_context forces it into a blob, whole)
  system, synthetic                           [opencode system] / [opencode synthetic] text, whole
  compaction                                  [opencode compaction summary] summary, whole; [opencode compaction recent] recent, forced into a blob
  idle, model/agent/location-switched, ...    one short line  [opencode <kind>] ...
  NOT rendered: reasoning text and reasoningEncryptedContent (dropped on purpose), JSON overhead, tokens, snapshots, times, providerState, the rest of state.metadata.
Everything but the file records passes through handoff-verify's sanitize.sanitize, exactly as its prepare.py does for the Claude records.

  opencode_source.py --selftest"""
import collections
import datetime
import importlib.util
import json
import os
import sqlite3
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import compact_context as cc

NS = uuid.UUID("0f1c8a52-6d0e-4c1b-9a77-5b3e2d41c0a9")   # fixed, so the same session always gives the same uuids
FIRST_CALLS = 4
EVENT_MAX = 300
LEGACY = "legacy OpenCode sessions are not supported (this database or session exists only in the legacy tables session/message/part; only session_v2/session_message are read)"


class OpenCodeError(Exception):
    """A message for the user: the database or the session cannot be used."""


# ---- the database --------------------------------------------------------------------------------------------------------------------------------
def default_db():
    """$XDG_DATA_HOME/opencode/opencode.db, else ~/.local/share/opencode/opencode.db (read now, so a test can set HOME and XDG_DATA_HOME)."""
    xdg = os.environ.get("XDG_DATA_HOME")
    return os.path.join(xdg if xdg else os.path.join(os.path.expanduser("~"), ".local", "share"), "opencode", "opencode.db")


def connect(path):
    """Read-only connection (mode=ro in the URI, and query_only). OpenCodeError when the file cannot be opened or is not a database."""
    uri = "file:" + os.path.abspath(path).replace("%", "%25").replace("?", "%3F").replace("#", "%23") + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        conn.execute("PRAGMA query_only=ON")
        conn.execute("select count(*) from sqlite_master").fetchone()   # a file that is not a database fails here
    except sqlite3.Error as e:
        raise OpenCodeError("the OpenCode database %s cannot be read (%s)" % (path, e))
    return conn


def tables(conn):
    return {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}


def session_ids(conn):
    """The ids in session_v2, oldest first. [] when the table is missing."""
    if "session_v2" not in tables(conn): return []
    return [r[0] for r in conn.execute("select id from session_v2 order by time_created, id")]


def legacy_has(conn, sid=None):
    """True when the legacy table `session` holds rows (or the row `sid`)."""
    if "session" not in tables(conn): return False
    try:
        if sid is None: return conn.execute("select count(*) from session").fetchone()[0] > 0
        return conn.execute("select count(*) from session where id = ?", (sid,)).fetchone()[0] > 0
    except sqlite3.Error:
        return False


def pick_session(conn, session=None, limit=15):
    """-> the session id to convert. Zero sessions, several without `session`, an unknown id and a legacy-only database or id are errors. The error for several lists the `limit` newest in the format of --list-opencode."""
    ids = session_ids(conn)
    if session is not None:
        if session in ids: return session
        if legacy_has(conn, session): raise OpenCodeError(LEGACY)
        raise OpenCodeError("--session %s: no such session in session_v2 (%d sessions in this database)" % (session, len(ids)))
    if len(ids) == 1: return ids[0]
    if not ids:
        if legacy_has(conn): raise OpenCodeError(LEGACY)
        raise OpenCodeError("no session in session_v2 of this database")
    rows, total = list_sessions(conn, None, limit)
    raise OpenCodeError("%d sessions in this database; pick one with --session ID (newest first):\n%s\n%s" % (len(ids), "\n".join(session_line(r) for r in rows), summary_line(len(rows), total)))


# ---- the list of sessions (--list-opencode): metadata and a message COUNT only, never the content of a message -----------------------------------------
LIST_LIMIT = 15
TITLE_MAX = 80


def tilde(path, home=None):
    """`path` with the home folder shown as ~ (only at a path boundary: /home/u2 is not under /home/u)."""
    home = os.path.expanduser("~") if home is None else home
    if not path or not home or home == "~": return path
    h = home.rstrip("/")
    if path == home or path == h: return "~"
    if path.startswith(h + "/"): return "~" + path[len(h):]
    return path


def title_text(title):
    """Every whitespace run (newlines too) becomes one space; over TITLE_MAX characters -> the first TITLE_MAX-1 and '…'; empty or NULL -> '(untitled)'."""
    t = " ".join(("" if title is None else str(title)).split())
    if not t: return "(untitled)"
    return t if len(t) <= TITLE_MAX else t[:TITLE_MAX - 1] + "…"


def time_text(ms):
    """Local time YYYY-MM-DD HH:MM of a time in milliseconds."""
    try: return datetime.datetime.fromtimestamp(ms // 1000).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError): return "????-??-?? ??:??"


def session_line(row, home=None):
    """One line:  <id> | <YYYY-MM-DD HH:MM> | <N> msgs | <directory, home as ~, '-' when empty> | <title>   (row as returned by list_sessions)."""
    d = " ".join(("" if row["directory"] is None else str(row["directory"])).splitlines())   # a line break in a folder name must not split the line
    return " | ".join((row["id"], time_text(row["time"]), "%d msgs" % row["count"], tilde(d, home) if d else "-", title_text(row["title"])))


def summary_line(shown, total, match=None, db=None):
    """The last line of a list: 'shown N of M sessions[ matching "TEXT"][; database: PATH]' (M = the sessions that matched)."""
    return "shown %d of %d sessions%s%s" % (shown, total, "" if match is None else ' matching "%s"' % match, "" if db is None else "; database: %s" % db)


def list_sessions(conn, match=None, limit=LIST_LIMIT):
    """-> (rows, matched_total): the `limit` newest sessions of session_v2 (time_updated, else time_created, desc; ties by id) that match, as dicts id/title/directory/time/count, and how many matched in all.
    `match`: case-insensitive (casefold) substring of the id, the title, the directory or its ~ form; None or '' keeps all. Reads the columns id, title, directory, time_updated, time_created (a missing one counts as NULL) and
    the NUMBER of rows of session_message per session; message data is never selected. OpenCodeError: no session_v2 (or none and only legacy rows), or the legacy rows only."""
    tb = tables(conn)
    if "session_v2" not in tb:
        raise OpenCodeError(LEGACY if legacy_has(conn) else "no session_v2 table in this database")
    cols = {r[1] for r in conn.execute("pragma table_info(session_v2)")}
    col = lambda c: c if c in cols else "NULL"
    try:
        raw = conn.execute("select id, %s, %s, coalesce(%s, %s, 0) from session_v2" % (col("title"), col("directory"), col("time_updated"), col("time_created"))).fetchall()
        counts = {}
        if "session_message" in tb:
            try: counts = dict(conn.execute("select session_id, count(*) from session_message group by session_id").fetchall())
            except sqlite3.Error: counts = {}   # a table of another shape: no counts
    except sqlite3.Error as e:
        raise OpenCodeError("the session list of the OpenCode database cannot be read (%s)" % e)
    if not raw and legacy_has(conn): raise OpenCodeError(LEGACY)
    def ms(v):
        try: return int(v)
        except (TypeError, ValueError, OverflowError): return 0
    rows = [{"id": str(r[0]), "title": r[1], "directory": r[2], "time": ms(r[3]), "count": counts.get(r[0], 0)} for r in raw]
    if match:
        needle = match.casefold()
        def hit(r):
            d = "" if r["directory"] is None else str(r["directory"]); t = "" if r["title"] is None else str(r["title"])
            return any(needle in s.casefold() for s in (r["id"], t, " ".join(t.split()), d, tilde(d)))
        rows = [r for r in rows if hit(r)]
    rows.sort(key=lambda r: (-r["time"], r["id"]))
    return rows[:limit], len(rows)


def find_current(marker, db, max_age=1800, now=None):
    """The ids of the sessions with a message row created in the last `max_age` seconds whose data holds `marker`. A missing file is skipped (-> []); a database without session_message gives []; an existing file that
    cannot be read raises OpenCodeError (the caller names --no-opencode). time_created is in milliseconds."""
    if not os.path.exists(db): return []
    conn = connect(db)
    try:
        if "session_message" not in tables(conn): return []
        cutoff = int(((time.time() if now is None else now) - max_age) * 1000)
        try:
            return sorted(r[0] for r in conn.execute("select distinct session_id from session_message where time_created >= ? and instr(data, ?) > 0", (cutoff, marker)))
        except sqlite3.Error as e:
            raise OpenCodeError("the OpenCode database %s cannot be searched (%s)" % (db, e))
    finally:
        conn.close()


# ---- the sanitizer of handoff-verify -------------------------------------------------------------------------------------------------------------
def _hv_scripts():
    """handoff-verify's scripts (the same lookup as prepare_all.py, which cannot be imported: it runs the whole of prepare.py): HV_SCRIPTS, else the sibling skill, else the newest installed copy of the jev plugin."""
    import glob
    cands = [os.environ.get("HV_SCRIPTS"), os.path.join(HERE, "..", "..", "handoff-verify", "scripts")]
    cands += sorted(glob.glob(os.path.expanduser("~/.claude/plugins/cache/*/jev/*/skills/handoff-verify/scripts")), reverse=True)
    for c in cands:
        if c and os.path.exists(os.path.join(c, "sanitize.py")): return os.path.abspath(c)
    sys.exit("handoff-verify's scripts (sanitize.py) not found: set HV_SCRIPTS to their folder")


_SANITIZE = []


def sanitize(text):
    """-> the text with the demonstrable secrets redacted (handoff-verify's sanitize.sanitize; the module is loaded from its file, so nothing else of that folder enters sys.path)."""
    if not _SANITIZE:
        spec = importlib.util.spec_from_file_location("hv_sanitize", os.path.join(_hv_scripts(), "sanitize.py"))
        mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); _SANITIZE.append(mod.sanitize)
    return _SANITIZE[0](text)[0]


# ---- rendering -----------------------------------------------------------------------------------------------------------------------------------
class Rec:
    """One rendered record: text (final, as written to the chunk), kind, the whole strings it was rendered from (`needs`), whether the sanitizer changed it, and for a file block its uri."""
    __slots__ = ("uuid", "role", "text", "needs", "changed", "uri")

    def __init__(self, uid, role, text, needs, changed, uri=None):
        self.uuid, self.role, self.text, self.needs, self.changed, self.uri = uid, role, text, needs, changed, uri


class Rendered:
    def __init__(self):
        self.recs = []; self.readonly = set(); self.forced = {}; self.first_calls = []; self.original = 0
        self.n_compaction_recent = 0; self.n_files = 0; self.dropped_reasoning = 0; self.dropped_encrypted = 0; self.sanitized = 0; self.skipped = collections.Counter()

    @property
    def text(self): return "".join(r.text for r in self.recs)


class Lit(str):
    """The text '{}' or '[]' standing for an empty container: not a string of the source."""


def flat(v, path=""):
    """-> [(path, leaf)]: nested dicts as path.sub, lists as path[0]; an empty container is the leaf '{}' / '[]'."""
    if isinstance(v, dict):
        if not v: return [(path, Lit("{}"))] if path else []
        return [p for k, x in v.items() for p in flat(x, "%s.%s" % (path, k) if path else str(k))]
    if isinstance(v, list):
        if not v: return [(path, Lit("[]"))]
        return [p for i, x in enumerate(v) for p in flat(x, "%s[%d]" % (path, i))]
    return [(path, v)]


def leaf_text(v): return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)


def input_lines(inp):
    """-> (text, [whole strings]) of a tool call input: 'key: value' lines with the raw strings (a multi-line value stays multi-line)."""
    pairs = flat(inp)
    lines = [("%s: %s" % (p, leaf_text(v))) if p else leaf_text(v) for p, v in pairs]
    return "\n".join(lines), [v for _, v in pairs if isinstance(v, str) and not isinstance(v, Lit)]


def event_line(kind, j):
    """One short line for a row that is not a message: its scalar fields (times left out), at most EVENT_MAX characters."""
    body = ", ".join("%s=%s" % (p, leaf_text(v)) for p, v in flat({k: x for k, x in j.items() if k != "time"}) if v not in ("", None)) if isinstance(j, dict) else leaf_text(j)
    body = body.replace("\n", "⏎")
    return "[opencode %s] %s" % (kind, body if len(body) <= EVENT_MAX else body[:EVENT_MAX] + "…")


def result_suffix(name, meta):
    """The only facts kept from state.metadata: a shell tool's exit code and the truncated flag; another tool only the truncated flag."""
    if not isinstance(meta, dict): return ""
    trunc = meta.get("truncated") is True
    if name.lower() in ("shell", "bash") and "exit" in meta: return "(exit %s%s)" % (leaf_text(meta["exit"]), ", truncated" if trunc else "")
    return "(truncated)" if trunc else ""


def render(conn, sid):
    """-> Rendered for the session `sid`: the records in seq order, the uuids of the read-only calls, the forced records {uuid: marker label}, the first tool calls (for the parent handoff), the original size."""
    R = Rendered()
    R.original = conn.execute("select coalesce(sum(length(data)), 0) from session_message where session_id = ?", (sid,)).fetchone()[0]
    count = collections.Counter()

    def add(mid, role, body, needs=(), sanitized=True, uri=None):
        n = count[mid]; count[mid] += 1
        uid = str(uuid.uuid5(NS, "%s/%s/%d" % (sid, mid, n)))
        raw = "[%s %s] %s\n" % (uid, role, body)
        text = sanitize(raw) if sanitized else raw
        changed = text != raw
        R.sanitized += changed
        R.recs.append(Rec(uid, role, text, [s for s in needs if s], changed, uri))
        return uid

    for mid, typ, data in conn.execute("select id, type, data from session_message where session_id = ? order by seq, rowid", (sid,)):
        try: j = json.loads(data)
        except (TypeError, ValueError): j = None
        if not isinstance(j, dict):
            add(mid, "user", "[opencode %s] (the data of this row is not a JSON object)" % typ); R.skipped["unreadable"] += 1; continue
        if typ == "user":
            t = j.get("text") if isinstance(j.get("text"), str) else ""
            add(mid, "user", t, [t])
        elif typ in ("system", "synthetic"):
            t = j.get("text") if isinstance(j.get("text"), str) else ""
            add(mid, "user", "[opencode %s] %s" % (typ, t))
        elif typ == "compaction":
            add(mid, "user", "[opencode compaction summary] %s" % (j["summary"] if isinstance(j.get("summary"), str) else ""))
            if isinstance(j.get("recent"), str) and j["recent"]:
                uid = add(mid, "user", "[opencode compaction recent] %s" % j["recent"]); R.forced[uid] = "[opencode compaction recent]"; R.n_compaction_recent += 1
        elif typ == "assistant":
            for part in j.get("content") or []:
                pt = part.get("type") if isinstance(part, dict) else None
                if pt == "text":
                    t = part.get("text") if isinstance(part.get("text"), str) else ""
                    add(mid, "assistant", t, [t])
                elif pt == "reasoning":
                    R.dropped_reasoning += len(part.get("text") or "")
                    st = part.get("state")
                    R.dropped_encrypted += len((st or {}).get("reasoningEncryptedContent") or "") if isinstance(st, dict) else 0
                elif pt == "tool":
                    render_tool(R, add, mid, part)
                else:
                    add(mid, "user", "[opencode assistant content %s omitted]" % (pt,)); R.skipped["content:%s" % (pt,)] += 1
            if isinstance(j.get("error"), dict):   # the message itself failed (for example an interrupted step)
                add(mid, "assistant", "[opencode assistant error] %s: %s" % (leaf_text(j["error"].get("type")), leaf_text(j["error"].get("message"))))
        else:   # idle, model-switched, agent-switched, location-switched, and any type this version does not know
            add(mid, "user", event_line(typ, j))
    return R


def render_tool(R, add, mid, part):
    name = part.get("name") if isinstance(part.get("name"), str) else "unknown"
    state = part.get("state") if isinstance(part.get("state"), dict) else {}
    inp = state.get("input")
    body, strings = input_lines(inp)
    uid = add(mid, "assistant", ("[tool_use %s] %s" % (name, body)) if body else "[tool_use %s]" % name, strings)
    if len(R.first_calls) < FIRST_CALLS: R.first_calls.append((name, inp))
    if name.lower() in ("read", "grep", "glob"): R.readonly.add(uid)
    elif name.lower() in ("shell", "bash") and isinstance(inp, dict) and isinstance(inp.get("command"), str) and cc.readonly_command(inp["command"]): R.readonly.add(uid)
    status = state.get("status"); err = state.get("error")
    texts, files = [], []
    for blk in state.get("content") or []:
        if not isinstance(blk, dict): continue
        if blk.get("type") == "text":
            if isinstance(blk.get("text"), str) and blk["text"]: texts.append(blk["text"])
        elif blk.get("type") == "file": files.append(blk)
        else: texts.append(json.dumps(blk, ensure_ascii=False))
    out = "\n".join(texts); suffix = result_suffix(name, state.get("metadata"))
    if isinstance(err, dict) or status == "error":
        head = "[tool_result ERROR] %s: %s" % (leaf_text(err.get("type")), leaf_text(err.get("message"))) if isinstance(err, dict) else "[tool_result ERROR] (no error recorded; status %s)" % status
        res = head + ("\n" + out if out else "")
    elif status != "completed":
        res = "[tool_result] (no result recorded; status %s)" % leaf_text(status) + ("\n" + out if out else "")
    elif out: res = "[tool_result] " + out
    elif not files or suffix: res = "[tool_result] (no output; status completed)"
    else: res = None
    if res is not None: add(mid, "user", res + ("\n" + suffix if suffix else ""))
    for blk in files:
        uri = blk["uri"] if isinstance(blk.get("uri"), str) else ""
        mime = leaf_text(blk.get("mime", "")); fname = leaf_text(blk.get("name", ""))
        fid = add(mid, "user", "[tool_result file]\nmime: %s\nname: %s\nuri: %s" % (mime, fname, uri), sanitized=False, uri=uri)
        R.forced[fid] = "[file %s %s, %d chars]" % (mime[:40], fname[:60], len(uri)); R.n_files += 1


def find_parents(first_calls, pattern):
    """The handoff files (paths matching `pattern`) that a `read` among the first tool calls opened, in order, without duplicates."""
    found = []
    for name, inp in first_calls:
        p = inp.get("path") if isinstance(inp, dict) and name.lower() == "read" else None
        if isinstance(p, str) and pattern.search(p) and p not in found: found.append(p)
    return found


# ---- the self-check ------------------------------------------------------------------------------------------------------------------------------
def selfcheck(R, expanded, blob_texts):
    """After the round trip: every record sits in the expanded output where it was rendered. A record that the sanitizer left unchanged holds every whole string it was rendered from (user text, assistant text part,
    tool-input string) at least as often as the source has it; records the sanitizer changed are not string-checked (its rules depend on the context, e.g. an input key `token`). For a file block the ORIGINAL uri
    from the database occurs unchanged in its place and in a blob. -> a list of problems (empty = fine)."""
    bad, pos = [], 0
    for r in R.recs:
        seg = expanded[pos:pos + len(r.text)]; pos += len(r.text)
        if seg != r.text: bad.append("record %s is not where it was rendered" % r.uuid); continue
        if r.uri is not None:
            if r.uri not in seg: bad.append("record %s: the uri of the file block is not unchanged in the output" % r.uuid)
            if r.text not in blob_texts: bad.append("record %s: the file block is not whole in a blob" % r.uuid)
        elif not r.changed:
            for s, k in collections.Counter(r.needs).items():
                if seg.count(s) < k: bad.append("record %s: a string of the source (%d chars) occurs %d times instead of %d" % (r.uuid, len(s), seg.count(s), k))
    if pos != len(expanded): bad.append("the output is %d characters longer than the rendered records" % (len(expanded) - pos))
    return bad[:5]


# ---- self test -----------------------------------------------------------------------------------------------------------------------------------
def make_db(path, sessions, legacy=None, now_ms=None, meta=None):
    """A test database: `sessions` = {session id: [(type, data dict, age in seconds)]} (rows in the given order, seq 1, 2, ...); `legacy` = ids to put in a legacy `session` table. Times are in milliseconds.
    `meta` = {session id: {title, directory, time_updated, parent_id, fork_session_id}} (a missing key is NULL): only then session_v2 gets those columns; without it the table has id and time_created only."""
    now = int(time.time() * 1000) if now_ms is None else now_ms
    c = sqlite3.connect(path)
    c.execute("create table session_v2(id TEXT, time_created INT%s)" % ("" if meta is None else ", title TEXT, directory TEXT, time_updated INT, parent_id TEXT, fork_session_id TEXT"))
    c.execute("create table session_message(id TEXT, session_id TEXT, type TEXT, seq INT, time_created INT, time_updated INT, data TEXT)")
    for i, (sid, rows) in enumerate(sessions.items()):
        if meta is None: c.execute("insert into session_v2 values (?, ?)", (sid, now - 1000 * 1000 + i))
        else:
            m = meta.get(sid, {}); c.execute("insert into session_v2 values (?, ?, ?, ?, ?, ?, ?)", (sid, now - 1000 * 1000 + i) + tuple(m.get(k) for k in ("title", "directory", "time_updated", "parent_id", "fork_session_id")))
        for seq, (typ, data, age) in enumerate(rows, 1):
            c.execute("insert into session_message values (?, ?, ?, ?, ?, ?, ?)", ("msg_%s_%d" % (sid[-6:], seq), sid, typ, seq, now - int(age * 1000), now - int(age * 1000), json.dumps(data, ensure_ascii=False)))
    if legacy:
        c.execute("create table session(id TEXT)"); c.execute("create table message(id TEXT)"); c.execute("create table part(id TEXT)")
        for sid in legacy: c.execute("insert into session values (?)", (sid,))
    c.commit(); c.close()
    return path


def synthetic_rows(secret="API_TOKEN=abcd1234efgh5678", uri_tail="aB3dE5fG7hJ9kL1mN2pQ4rS6tU8vW0xY" * 8):
    """One session with every message type and every content kind (used by the tests of make_context.py too)."""
    uri = "data:image/png;base64,iVBORw0KGgo" + uri_tail + "=="
    tool = lambda name, inp, status="completed", content=None, error=None, meta=None, **kw: {"type": "tool", "name": name, "executed": False, "state": dict({"status": status, "input": inp}, **({"content": content} if content is not None else {}),
                                                                                                                                                           **({"error": error} if error else {}), **({"metadata": meta} if meta else {})), **kw}
    txt = lambda s: [{"type": "text", "text": s}]
    return [
        ("user", {"time": {"created": 1}, "text": "Salut! Fă un plan pentru fișierul „raport” – cu diacritice: ăâîșț, 日本語 și emoji 🙂."}, 90),
        ("system", {"description": "Instructions updated: core/x", "text": "SYSTEM TEXT one two three"}, 89),
        ("synthetic", {"text": "SYNTHETIC TEXT four five"}, 88),
        ("model-switched", {"time": {"created": 1}, "model": {"id": "m-two", "providerID": "prov"}}, 87),
        ("agent-switched", {"agent": "build"}, 86),
        ("location-switched", {"location": "/work/dir"}, 85),
        ("assistant", {"agent": "build", "model": {"id": "m-two", "providerID": "prov", "variant": "high"}, "cost": 0, "tokens": {"input": 1}, "finish": "tool-calls", "snapshot": "abc", "time": {"created": 2}, "providerState": {}, "content": [
            {"type": "reasoning", "text": "REASONING SECRET WORDS", "state": {"itemId": "i1", "reasoningEncryptedContent": "ENCRYPTEDBLOB" * 50}},
            {"type": "text", "text": "I will look at the files first.", "state": {"itemId": "i2", "phase": "commentary"}},
            tool("shell", {"command": "ls -la /work/dir/with/a/rather/long/path/name/to/pass/the/preview/threshold/of/the/marker", "workdir": "/work/dir", "timeout": 5000}, content=txt("total 0\nfile-a\nfile-b"), meta={"exit": 0, "truncated": False, "shellID": "s1"}),
            tool("shell", {"command": "git commit -m 'x'"}, content=txt("[main abc] x"), meta={"exit": 1, "truncated": True}),
            tool("read", {"path": "/work/dir/with/a/rather/long/path/name/to/pass/the/preview/threshold/of/the/marker/big.txt", "offset": 1, "limit": 20}, content=txt("1\tline one\n2\tline two\n" + "payload " * 80), meta={"truncated": True}),
            tool("patch", {"patchText": "*** Begin Patch\n*** Update File: a.txt\n@@\n-old line\n+new line é\n*** End Patch"}, content=txt("patched"), meta={"files": ["a.txt"], "truncated": False}),
            tool("custom", {"nested": {"list": ["alpha", {"deep": "beta"}], "n": 3, "flag": True, "empty": {}}, "token": "s3cretValue99xyz"}, content=txt("ok " + secret + " and more text after it"), meta={"truncated": False}),
            tool("patch", {"patchText": "*** Begin Patch\n*** End Patch"}, status="error", error={"type": "tool.execution", "message": "patch verification failed: no lines"}),
            tool("grep", {"pattern": "needle", "path": "/work/dir/with/a/rather/long/path/name/to/pass/the/preview/threshold/of/the/marker"}, content=[], meta={"matches": 0, "truncated": False}),
            tool("webfetch", {"url": "https://example.com/x"}, status="running"),
            tool("read", {"path": "/work/dir/with/a/rather/long/path/name/to/pass/the/preview/threshold/of/the/marker/shot.png"}, content=[{"type": "text", "text": "Image read successfully. Token=TOKENabcdefgh12345 near the image"}, {"type": "file", "mime": "image/png", "name": "shots/one.png", "uri": uri}], meta={"truncated": False}),
        ]}, 80),
        ("compaction", {"time": {"created": 3}, "status": "done", "reason": "overflow", "model": {"id": "m"}, "providerState": {}, "summary": "COMPACTION SUMMARY: goal and decisions so far", "recent": "RECENT TEXT " + "of the recent turns " * 30, "cost": 0, "tokens": {}}, 70),
        ("assistant", {"agent": "build", "model": {"id": "m-two"}, "cost": 0, "tokens": {}, "finish": "error", "providerState": {}, "error": {"type": "aborted", "message": "Step interrupted"}, "content": [{"type": "text", "text": "Final answer text."}]}, 60),
        ("idle", {"time": {"created": 4}, "outcome": "succeeded"}, 50),
    ]


def selftest():
    import io
    import shutil
    import tempfile
    n = 0
    d = tempfile.mkdtemp()
    try:
        # the default path: XDG_DATA_HOME first, then HOME
        keep = {k: os.environ.get(k) for k in ("XDG_DATA_HOME", "HOME")}
        try:
            os.environ["XDG_DATA_HOME"] = "/x/xdg"; os.environ["HOME"] = "/x/home"
            assert default_db() == "/x/xdg/opencode/opencode.db"
            os.environ["XDG_DATA_HOME"] = ""
            assert default_db() == "/x/home/.local/share/opencode/opencode.db"
            del os.environ["XDG_DATA_HOME"]
            assert default_db() == "/x/home/.local/share/opencode/opencode.db"; n += 3
        finally:
            for k, v in keep.items():
                if v is None: os.environ.pop(k, None)
                else: os.environ[k] = v
        # the flattening of an input
        text, strings = input_lines({"command": "ls", "n": 3, "t": True, "x": None, "o": {"a": ["p", {"q": "r"}], "e": {}, "l": []}, "ml": "one\ntwo"})
        assert text == "command: ls\nn: 3\nt: true\nx: null\no.a[0]: p\no.a[1].q: r\no.e: {}\no.l: []\nml: one\ntwo" and strings == ["ls", "p", "r", "one\ntwo"], text; n += 1
        assert input_lines(None) == ("null", []) and input_lines({}) == ("", []); n += 1
        # the suffix: shell exit always, truncated only when true; another tool only truncated; nothing else of the metadata
        for name, meta, want in (("shell", {"exit": 0, "truncated": False}, "(exit 0)"), ("shell", {"exit": 1, "truncated": True, "shellID": "s"}, "(exit 1, truncated)"), ("bash", {"exit": 2}, "(exit 2)"),
                                 ("read", {"truncated": True}, "(truncated)"), ("read", {"truncated": False}, ""), ("grep", {"matches": 3}, ""), ("shell", {"truncated": True}, "(truncated)"), ("shell", None, ""), ("patch", {"files": ["a"]}, "")):
            assert result_suffix(name, meta) == want, (name, meta, want); n += 1
        # a session with every message type and content kind
        db = make_db(os.path.join(d, "one.sqlite"), {"ses_0123456789abcdefAAAAAA": synthetic_rows()})
        conn = connect(db); sid = pick_session(conn); R = render(conn, sid)
        assert sid == "ses_0123456789abcdefAAAAAA" and R.original == conn.execute("select sum(length(data)) from session_message").fetchone()[0]; n += 1
        t = R.text
        for must in ("Fă un plan pentru fișierul „raport” – cu diacritice: ăâîșț, 日本語 și emoji 🙂.", "[opencode system] SYSTEM TEXT one two three", "[opencode synthetic] SYNTHETIC TEXT four five", "[opencode model-switched] model.id=m-two, model.providerID=prov",
                     "[opencode agent-switched] agent=build", "[opencode location-switched] location=/work/dir", "I will look at the files first.", "[tool_use shell] command: ls -la /work/dir/with/a/rather/long/path/name/to/pass/the/preview/threshold/of/the/marker\nworkdir: /work/dir\ntimeout: 5000",
                     "[tool_result] total 0\nfile-a\nfile-b\n(exit 0)", "(exit 1, truncated)", "[tool_use read] path: /work/dir/with/a/rather/long/path/name/to/pass/the/preview/threshold/of/the/marker/big.txt\noffset: 1\nlimit: 20", "(truncated)", "patchText: *** Begin Patch\n*** Update File: a.txt\n@@\n-old line\n+new line é\n*** End Patch",
                     "nested.list[0]: alpha\nnested.list[1].deep: beta\nnested.n: 3\nnested.flag: true\nnested.empty: {}", "[tool_result ERROR] tool.execution: patch verification failed: no lines", "[tool_result] (no output; status completed)",
                     "[tool_result] (no result recorded; status running)", "[opencode compaction summary] COMPACTION SUMMARY", "[opencode compaction recent] RECENT TEXT", "[opencode assistant error] aborted: Step interrupted", "Final answer text.",
                     "[opencode idle] outcome=succeeded", "[tool_result file]\nmime: image/png\nname: shots/one.png\nuri: data:image/png;base64,iVBORw0KGgoaB3dE5fG7hJ9kL1m"):
            assert must in t, must; n += 1
        assert "REASONING SECRET" not in t and "ENCRYPTEDBLOB" not in t and R.dropped_reasoning == len("REASONING SECRET WORDS") and R.dropped_encrypted == len("ENCRYPTEDBLOB") * 50; n += 3
        assert "shellID" not in t and "snapshot" not in t and "providerState" not in t and "cost" not in t; n += 1
        assert "API_TOKEN=abcd1234efgh5678" not in t and "[REDACTED:" in t and "s3cretValue99xyz" not in t and "TOKENabcdefgh12345" not in t; n += 1   # the sanitizer ran on a text output and on a tool input
        files = [r for r in R.recs if r.uri is not None]
        assert sanitize(files[0].uri) != files[0].uri; n += 1   # the sanitizer WOULD change this uri (so leaving it alone is a decision the test proves)
        assert len(files) == 1 and files[0].uri in t and files[0].text.count("[REDACTED") == 0 and R.n_files == 1 and R.n_compaction_recent == 1 and len(R.forced) == 2; n += 1   # the image is not sanitized
        assert R.sanitized >= 2; n += 1
        ro = {r.uuid for r in R.recs if r.uuid in R.readonly}
        tu = lambda s: next(r.uuid for r in R.recs if s in r.text.split("\n")[0] and "[tool_use" in r.text)
        assert tu("[tool_use shell] command: ls") in ro and tu("[tool_use read] path: /work/dir/with/a/rather/long/path/name/to/pass/the/preview/threshold/of/the/marker/big.txt") in ro and tu("[tool_use grep]") in ro; n += 3
        assert tu("[tool_use shell] command: git commit") not in ro and tu("[tool_use patch]") not in ro and tu("[tool_use custom]") not in ro and tu("[tool_use webfetch]") not in ro; n += 4
        assert [c[0] for c in R.first_calls] == ["shell", "shell", "read", "patch"]; n += 1
        import re
        hp = re.compile(r"/handoff-[0-9a-f]{8}-\d{8}-[^/\s\"]+(?:\.md|/HANDOFF\.md)$")
        assert find_parents([("shell", {"command": "x"}), ("read", {"path": "/tmp/handoff-0123abcd-20260101-aa/HANDOFF.md"}), ("read", {"path": "/tmp/handoff-0123abcd-20260101-aa/HANDOFF.md"}), ("read", {"path": "/tmp/other.md"}), ("Read", {"file_path": "/tmp/handoff-0123abcd-20260101-bb.md"}), ("read", {"path": 3})], hp) == ["/tmp/handoff-0123abcd-20260101-aa/HANDOFF.md"]; n += 1
        # uuids are valid for the record parser and stable
        assert all(cc.REC.match(r.text) for r in R.recs) and R.recs[0].uuid == render(conn, sid).recs[0].uuid and len({r.uuid for r in R.recs}) == len(R.recs); n += 1
        # the self-check: a good output passes; a lost string, a changed uri and a wrong order are found; a record the sanitizer changed is not string-checked
        blobs = {r.text for r in R.recs if r.uuid in R.forced}
        assert selfcheck(R, t, blobs) == []; n += 1
        assert selfcheck(R, t.replace("Final answer text.", "Final answer tex"), blobs) and selfcheck(R, t.replace(files[0].uri, files[0].uri[:-6] + "X" + files[0].uri[-5:]), blobs) and selfcheck(R, t, set()) and selfcheck(R, t + "x", blobs); n += 4
        changed = [r for r in R.recs if r.changed and r.needs]
        assert changed and selfcheck(R, t, blobs) == []; n += 1   # the tool input {"token": ...} record is changed by the sanitizer and does not fail
        # sessions: one, several, unknown, none, legacy
        conn.close()
        two = make_db(os.path.join(d, "two.sqlite"), {"ses_aaaaaaaaaaaaAAAAAAAA": [("user", {"text": "a"}, 5)], "ses_bbbbbbbbbbbbBBBBBBBB": [("user", {"text": "b"}, 5)]})
        c2 = connect(two)
        for sess, want in ((None, "2 sessions"), ("ses_nope", "no such session")):
            try: pick_session(c2, sess); raise SystemExit("not refused: %r" % sess)
            except OpenCodeError as e: assert want in str(e), str(e); n += 1
        assert pick_session(c2, "ses_bbbbbbbbbbbbBBBBBBBB") == "ses_bbbbbbbbbbbbBBBBBBBB"; n += 1
        msg = str(_catch(lambda: pick_session(c2)))   # the error lists the sessions as lines of --list-opencode, newest first (make_db gives the later session the later time_created)
        assert msg.startswith("2 sessions in this database; pick one with --session ID (newest first):\n") and msg.index("ses_bbbbbbbbbbbbBBBBBBBB | ") < msg.index("ses_aaaaaaaaaaaaAAAAAAAA | ") and msg.endswith("\nshown 2 of 2 sessions") and "database:" not in msg and "| 1 msgs |" in msg; n += 1
        c2.close()
        empty = make_db(os.path.join(d, "empty.sqlite"), {}); leg = make_db(os.path.join(d, "legacy.sqlite"), {}, legacy=["ses_old1"]); mix = make_db(os.path.join(d, "mix.sqlite"), {"ses_new": [("user", {"text": "n"}, 5)]}, legacy=["ses_old1"])
        for path, sess, want in ((empty, None, "no session in session_v2"), (leg, None, "legacy"), (leg, "ses_old1", "legacy"), (mix, "ses_old1", "legacy"), (leg, "ses_zzz", "no such session")):
            c = connect(path)
            try: pick_session(c, sess); raise SystemExit("not refused: %s %s" % (path, sess))
            except OpenCodeError as e: assert want in str(e), (path, sess, str(e)); n += 1
            c.close()
        # read-only: a write through the connection fails
        c = connect(two)
        try: c.execute("insert into session_v2 values ('x', 1)"); raise SystemExit("a write succeeded on a read-only connection")
        except sqlite3.Error: n += 1
        c.close()
        # the search for a marker: ids of the sessions with a recent row that holds it; times in milliseconds
        mk = "handoff-marker-1-aaaaaaaaaaaa"
        db3 = make_db(os.path.join(d, "find.sqlite"), {"ses_one": [("user", {"text": "x"}, 5), ("assistant", {"content": [{"type": "tool", "name": "shell", "state": {"status": "completed", "input": {"command": "echo " + mk}}}]}, 3)],
                                                      "ses_two": [("user", {"text": "no marker here"}, 3)], "ses_old": [("user", {"text": mk}, 7200)]})
        assert find_current(mk, db3) == ["ses_one"] and find_current("handoff-marker-9-bbbbbbbbbbbb", db3) == [] and find_current(mk, db3, max_age=10 ** 6) == ["ses_old", "ses_one"]; n += 3
        assert find_current(mk, os.path.join(d, "missing.sqlite")) == [] and find_current(mk, leg) == []; n += 2
        junk = os.path.join(d, "junk.sqlite"); open(junk, "wb").write(b"this is not a database " * 100)
        try: find_current(mk, junk); raise SystemExit("an unreadable database was not reported")
        except OpenCodeError as e: assert "cannot be read" in str(e) or "cannot be searched" in str(e); n += 1
        n += selftest_list(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("opencode_source selftest OK: %d cases (rendering of every message type and content kind, the sanitizer on texts and inputs but not on the image, the read-only set, the self-check, session picking, legacy, the marker search, the session list)" % n)


def selftest_list(d):
    """The pure part of --list-opencode (the command line is tested in make_context.py) -> the number of cases."""
    n = 0
    T = 1700000000000   # 2023-11-14 22:13:20 UTC
    # ~ only at a path boundary
    for path, home, want in (("/h/u", "/h/u", "~"), ("/h/u/x/y", "/h/u", "~/x/y"), ("/h/u2", "/h/u", "/h/u2"), ("/h/ux/y", "/h/u", "/h/ux/y"), ("/other", "/h/u", "/other"), ("/h/u/x", "/h/u/", "~/x"), ("/x", "/", "~/x"), ("", "/h/u", ""), ("/h/u", "~", "/h/u"), ("/h/u", "", "/h/u")):
        assert tilde(path, home) == want, (path, home, tilde(path, home)); n += 1
    # the title: whitespace runs, 80 characters, empty
    for title, want in (("a  b\n\n c\td", "a b c d"), ("A" * 80, "A" * 80), ("A" * 81, "A" * 79 + "…"), ("A" * 120, "A" * 79 + "…"), ("", "(untitled)"), (None, "(untitled)"), (" \n ", "(untitled)"), ("ș" * 90, "ș" * 79 + "…")):
        assert title_text(title) == want and len(title_text(title)) <= 80, title; n += 1
    # the line, in UTC (TZ is read by the C library: tzset), and the listing of a database with metadata
    keep_tz = os.environ.get("TZ"); keep_home = os.environ.get("HOME"); os.environ["HOME"] = "/h/u"   # list_sessions reads the home folder when it matches the ~ form
    def set_tz(v):
        if v is None: os.environ.pop("TZ", None)
        else: os.environ["TZ"] = v
        if hasattr(time, "tzset"): time.tzset()
    try:
        set_tz("UTC")
        if hasattr(time, "tzset"):
            row = {"id": "ses_x1", "title": "Council B g1 (openai/gpt-6-astra)", "directory": "/h/u/proj", "time": T, "count": 2}
            assert session_line(row, "/h/u") == "ses_x1 | 2023-11-14 22:13 | 2 msgs | ~/proj | Council B g1 (openai/gpt-6-astra)"; n += 1
            assert session_line(dict(row, directory="", title="", count=0), "/h/u") == "ses_x1 | 2023-11-14 22:13 | 0 msgs | - | (untitled)" and session_line(dict(row, directory=None, title=None), "/h/u").count(" | ") == 4; n += 2
            assert session_line(dict(row, directory="/a\n/b"), "/h/u") == "ses_x1 | 2023-11-14 22:13 | 2 msgs | /a /b | Council B g1 (openai/gpt-6-astra)"; n += 1   # one line per session
            set_tz("JST-9"); assert time_text(T) == "2023-11-15 07:13"; n += 1   # local time, not UTC
        assert time_text(10 ** 30) == "????-??-?? ??:??"; n += 1
        assert summary_line(2, 5) == "shown 2 of 5 sessions" and summary_line(0, 0, "zz", "/p/db") == 'shown 0 of 0 sessions matching "zz"; database: /p/db' and summary_line(1, 1, None, "/p/db") == "shown 1 of 1 sessions; database: /p/db"; n += 3
        meta = {"ses_a1": {"title": "Alpha", "directory": "/h/u/a", "time_updated": T + 10}, "ses_a2": {"title": "Beta (fork #1)", "directory": "/srv/B", "time_updated": T + 5}, "ses_a3": {"title": "Gamma", "directory": None, "time_updated": T + 10},
                "ses_a4": {"title": "Șantier\nBucurești", "directory": "/h/u/ședință", "time_updated": None}}
        rows_ = [("user", {"text": "SECRET CONTENT"}, 1)]
        db = make_db(os.path.join(d, "list.sqlite"), {k: rows_ * (i + 1) for i, k in enumerate(meta)}, meta=meta)   # time_created grows with the order of the dict: a4 is the newest by creation
        conn = connect(db)
        ids = lambda *a, **k: [r["id"] for r in list_sessions(conn, *a, **k)[0]]
        assert ids() == ["ses_a4", "ses_a1", "ses_a3", "ses_a2"], ids(); n += 1   # a4: time_updated NULL -> time_created (about now, newer than T); a1 before a3 on the same time (id); a2 last
        assert [r["count"] for r in list_sessions(conn)[0]] == [4, 1, 3, 2]; n += 1
        assert list_sessions(conn, None, 2)[1] == 4 and ids(None, 2) == ["ses_a4", "ses_a1"] and ids(None, 1) == ["ses_a4"]; n += 2
        for m, want in (("ALPHA", ["ses_a1"]), ("ses_a3", ["ses_a3"]), ("fork #1", ["ses_a2"]), ("/srv/b", ["ses_a2"]), ("~/a", ["ses_a1"]), ("/h/u/a", ["ses_a1"]), ("ȘANTIER", ["ses_a4"]), ("șantier bucurești", ["ses_a4"]), ("ședință", ["ses_a4"]), ("nothing", []),
                        ("", ["ses_a4", "ses_a1", "ses_a3", "ses_a2"]), (None, ["ses_a4", "ses_a1", "ses_a3", "ses_a2"]), ("ses_a", ["ses_a4", "ses_a1", "ses_a3", "ses_a2"])):
            assert ids(m) == want and list_sessions(conn, m)[1] == len(want), (m, ids(m)); n += 1
        assert ids("ses_a", 2) == ["ses_a4", "ses_a1"] and list_sessions(conn, "ses_a", 2)[1] == 4; n += 1   # the match is applied before the limit
        # the content of a message is never read: a denied read of session_message.data changes nothing (and the denial bites)
        deny = lambda action, a1, a2, dbn, src: 1 if (action == 20 and a1 == "session_message" and a2 == "data") else 0   # SQLITE_READ = 20, SQLITE_DENY = 1
        conn.set_authorizer(deny)
        try: conn.execute("select data from session_message").fetchall(); raise SystemExit("the authorizer does not deny the read of data")
        except sqlite3.DatabaseError: n += 1
        assert ids() == ["ses_a4", "ses_a1", "ses_a3", "ses_a2"] and [r["count"] for r in list_sessions(conn)[0]] == [4, 1, 3, 2] and ids("alpha") == ["ses_a1"]; n += 1
        conn.set_authorizer(None); conn.close()
        # garbage message data: still listed with the right counts
        c = sqlite3.connect(db); c.execute("update session_message set data = ?", (b"\xff\x00\xfe not json {{",)); c.commit(); c.close()
        conn = connect(db); assert [r["count"] for r in list_sessions(conn)[0]] == [4, 1, 3, 2]; conn.close(); n += 1
    finally:
        set_tz(keep_tz)
        if keep_home is None: os.environ.pop("HOME", None)
        else: os.environ["HOME"] = keep_home
    # a minimal schema (make_db without meta), no session_message table, no rows, legacy, no session_v2
    mini = make_db(os.path.join(d, "mini.sqlite"), {"ses_m1": [("user", {"text": "x"}, 1)], "ses_m2": []})
    conn = connect(mini); rows, total = list_sessions(conn)
    assert total == 2 and [(r["id"], r["count"], r["title"], r["directory"]) for r in rows] == [("ses_m2", 0, None, None), ("ses_m1", 1, None, None)]; n += 1   # time_created only: the later one first
    assert session_line(rows[0], "/h").endswith(" | 0 msgs | - | (untitled)") and session_line(rows[1], "/h").endswith(" | 1 msgs | - | (untitled)"); n += 2
    conn.close()
    raw = lambda name, *stmts: (lambda c: ([c.execute(x) for x in stmts], c.commit(), c.close()))(sqlite3.connect(os.path.join(d, name))) and os.path.join(d, name)
    nomsg = raw("nomsg.sqlite", "create table session_v2(id TEXT, time_created INT)", "insert into session_v2 values ('ses_n1', 5)")
    conn = connect(nomsg); assert [(r["id"], r["count"]) for r in list_sessions(conn)[0]] == [("ses_n1", 0)]; conn.close(); n += 1
    empty = make_db(os.path.join(d, "emptyl.sqlite"), {}, meta={}); leg_empty = make_db(os.path.join(d, "legl.sqlite"), {}, legacy=["ses_old1"])
    leg_only = raw("legonly.sqlite", "create table session(id TEXT)", "insert into session values ('ses_old1')")
    other = raw("other.sqlite", "create table unrelated(x INT)"); leg_norows = raw("legnorows.sqlite", "create table session(id TEXT)")
    conn = connect(empty); assert list_sessions(conn) == ([], 0) and list_sessions(conn, "x") == ([], 0); conn.close(); n += 1
    for path, want in ((leg_empty, LEGACY), (leg_only, LEGACY), (other, "no session_v2 table in this database"), (leg_norows, "no session_v2 table in this database")):
        conn = connect(path)
        try: list_sessions(conn); raise SystemExit("not refused: %s" % path)
        except OpenCodeError as e: assert str(e) == want, (path, str(e)); n += 1
        conn.close()
    return n


def _catch(fn):
    try: fn()
    except OpenCodeError as e: return e
    raise SystemExit("no error raised")


if __name__ == "__main__":
    if "--selftest" in sys.argv: selftest()
    else: print(__doc__); sys.exit(2)
