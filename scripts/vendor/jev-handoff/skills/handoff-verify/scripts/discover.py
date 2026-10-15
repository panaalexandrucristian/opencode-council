"""Session discovery and Write/Edit inventory from Claude Code JSONL transcripts. Pure parsing; never executes anything."""
import glob, hashlib, json, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import jevref as J, opencode as O

NAME_RX = re.compile(r"(?i)(^HANDOVER|^HANDOFF|^CONTINUE-HERE|handoff|handover)")
BASH_WRITE = re.compile(r"(?:>>?|\btee\s+(?:-a\s+)?)\s*([\"']?)([^\s\"'|;&<>]+\.(?:md|txt|markdown))\1")
TEXT_EXT = (".md", ".markdown", ".txt")

def projects_dir():
    return os.environ.get("CLAUDE_PROJECTS_DIR", os.path.expanduser("~/.claude/projects"))

def is_selector(s):
    """An explicit OpenCode selector (`opencode:<id>` / `opencode-db:<abs db>#<id>`); never a guess, never a filesystem path."""
    return O.is_selector(s)

def source_exists(p):
    """The source (a JSONL file or an OpenCode selector) is readable now."""
    return O.exists(p) if O.is_selector(p) else os.path.isfile(p)

def canon(p):
    """Canonical identity of a source: realpath of a file, `opencode-db:<realpath(db)>#<id>` of a selector (an unusable selector stays as written)."""
    if not O.is_selector(p): return os.path.realpath(p)
    try: return O.canonical(p)
    except OSError: return p

def fingerprint(p):
    """(identity, mtime, size...) of a source for memoization; OSError when it cannot be read."""
    if O.is_selector(p): return O.fingerprint(p)
    return (os.path.realpath(p), os.path.getmtime(p), os.path.getsize(p))

class fingerprint_scope:
    """`with fingerprint_scope():` -> a SNAPSHOT: inside the block each transcript file is read once (`jevref.read_snapshot`) and its digest (`content_fingerprint`), its records (`load_jsonl`) and its Jev calls (`jevref.load_calls`) all come from
    that capture, and an OpenCode session is read once (identity and parsing from one snapshot). Every load still returns its own mutable copy; nested blocks share the capture; it ends with the outermost block, never across candidates."""
    def __enter__(self):
        self.files = J.file_snapshots(); self.files.__enter__(); self.snap = O.snapshot_scope(); self.snap.__enter__(); return self
    def __exit__(self, *a):
        try: self.snap.__exit__(*a)
        finally: self.files.__exit__(*a)

def content_fingerprint(p):
    """Content-backed identity of a source for memoization: ("sha256", canonical path, sha256 of the bytes) of a file, ("sha256", canonical selector, sha256 of the stored session content) of an OpenCode selector (read-only).
    mtime and size alone are never the identity of a source: a same-size edit with restored timestamps must invalidate. Inside a snapshot scope the digest is the one of the captured bytes the records are parsed from. OSError when it cannot be read."""
    if O.is_selector(p): return O.fingerprint(p)
    if J._SNAP is not None:
        real, sha, _ = J.read_snapshot(p); return ("sha256", real, sha)
    real = os.path.realpath(p)
    h = hashlib.sha256()
    with open(real, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""): h.update(chunk)
    return ("sha256", real, h.hexdigest())

def session_label(p):
    """Session id used to name run directories: the JSONL stem, or the OpenCode session id."""
    return O.session_id(p) if O.is_selector(p) else os.path.splitext(os.path.basename(p))[0]

def load_jsonl(path):
    if O.is_selector(path): return O.records(path)   # explicit OpenCode selector: normalized in memory, nothing written
    out = []
    for i, l in enumerate(J.text_lines(J.read_snapshot(path)[2])):   # the captured bytes of a snapshot scope, else the file as it is; always parsed afresh: the caller owns the records
        l = l.strip()
        if not l: continue
        try: d = json.loads(l)
        except Exception: continue
        d["_line"] = i + 1
        out.append(d)
    return out

def resolve_session_info(arg=None, cwd=None, window_s=120):
    """Returns (path, how, ambiguous). how: path | id | env:CLAUDE_SESSION_ID | heuristic_most_recent | none.
    Without an explicit path/id the current session is NOT demonstrated: the most recent jsonl of cwd's project dir is only a heuristic, and it is flagged ambiguous
    when another jsonl of the same project was modified within `window_s` seconds of it (concurrent sessions)."""
    if O.is_selector(arg):   # explicit OpenCode selector: canonical identity, never guessed; existence is checked by the caller (source_exists)
        try: return O.canonical(arg), "opencode", False
        except OSError: return None, "opencode", False
    if arg and arg.endswith(".jsonl") and os.path.isfile(arg): return os.path.abspath(arg), "path", False
    arg = arg or os.environ.get("CLAUDE_SESSION_ID")
    if arg:
        m = glob.glob(os.path.join(projects_dir(), "*", arg + ".jsonl"))
        return (m[0] if m else None), ("id" if arg != os.environ.get("CLAUDE_SESSION_ID") else "env:CLAUDE_SESSION_ID"), False
    slug = re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(cwd or os.getcwd()))
    cands = glob.glob(os.path.join(projects_dir(), slug, "*.jsonl"))
    if not cands: return None, "none", False
    best = max(cands, key=os.path.getmtime)
    return best, "heuristic_most_recent", any(c != best and os.path.getmtime(best) - os.path.getmtime(c) <= window_s for c in cands)

def resolve_session(arg=None, cwd=None):
    """arg: .jsonl path | session id | None (=current: env CLAUDE_SESSION_ID else most recent jsonl of cwd's project dir, see resolve_session_info). Returns path or None."""
    return resolve_session_info(arg, cwd)[0]

def subagent_files(session_path):
    if O.is_selector(session_path): return []   # child sessions of an OpenCode session are not followed
    d = os.path.splitext(session_path)[0] + os.sep + "subagents"
    return sorted(glob.glob(os.path.join(d, "*.jsonl")))

def _results(records):
    res = {}
    for d in records:
        if d.get("type") == "user" and isinstance(d.get("message", {}).get("content"), list):
            for c in d["message"]["content"]:
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    res[c["tool_use_id"]] = {"is_error": bool(c.get("is_error")), "content": c.get("content"), "uuid": d.get("uuid"), "line": d["_line"], "timestamp": d.get("timestamp")}
    return res

def inventory(path, source="session"):
    """Successful Write/Edit calls (success = correlated tool_result without is_error), plus failed ones flagged, plus Bash-only candidates."""
    recs = load_jsonl(path); res = _results(recs); upos, rpos = J.timeline(recs)
    items, bash, idx = [], [], 0
    for d in recs:
        if d.get("type") != "assistant": continue
        for c in d.get("message", {}).get("content", []):
            if not isinstance(c, dict) or c.get("type") != "tool_use": continue
            idx += 1
            meta = {"tool_use_id": c["id"], "uuid": d.get("uuid"), "timestamp": d.get("timestamp"), "cwd": d.get("cwd"),
                    "line": d["_line"], "index": idx, "pos": upos.get(c["id"]), "result_pos": rpos.get(c["id"]), "source": source, "file": path}
            if c["name"] in ("Write", "Edit") and "file_path" in c["input"]:
                r = res.get(c["id"])
                meta.update(op=c["name"], path=c["input"]["file_path"], result_found=r is not None,
                            success=bool(r) and not r["is_error"], result_uuid=(r or {}).get("uuid"), result_timestamp=(r or {}).get("timestamp"))
                if d.get("_adapter") == "opencode" and isinstance(c["input"].get("_unrecoverable"), str): meta["unrecoverable"] = c["input"]["_unrecoverable"]   # adapter-only marker (never read from a Claude record)
                if c["name"] == "Write": meta["content"] = c["input"].get("content")
                else: meta.update(old_string=c["input"].get("old_string"), new_string=c["input"].get("new_string"),
                                  replace_all=bool(c["input"].get("replace_all")))
                items.append(meta)
            elif c["name"] == "Bash":
                cmd = c["input"].get("command", "")
                for m in BASH_WRITE.finditer(cmd):
                    bash.append({**meta, "op": "bash-redirect", "path": m.group(2), "linked": False})
    return items, bash

def diagnostics(path):
    """Successful mutations of an OpenCode session that the adapter could not place or attribute (no valid top-level tool time, or no path): [{call_id, reason, path (realpath | None), seq, ci, sub, created, completed, kind}].
    A Claude transcript has none ([]). They are never events, times or versions: versions.reconstruct_stream marks every version of an affected path with them."""
    return O.diagnostics(path) if O.is_selector(path) else []

def real_of(path, cwd=None):
    """Canonical path of a recorded path: a relative one is resolved against the cwd recorded for the call. Without a recorded absolute cwd a relative path has no demonstrated place: None (the cwd of THIS process is
    never substituted); None is also the answer for anything that is not a non-empty string."""
    if not isinstance(path, str) or not path: return None
    if not os.path.isabs(path):
        if not (isinstance(cwd, str) and os.path.isabs(cwd)): return None
        path = os.path.join(cwd, path)
    return os.path.realpath(path)

def reads(path):
    """Read tool calls of a transcript whose content is DEMONSTRABLY the complete file: the call has no offset/limit, the result is not an error and the recorded structured result
    (`toolUseResult` of a session log, `tool_use_result` of a stream-json) is a text file with startLine 1 and numLines == totalLines; the content comes from that structure (the
    `cat -n` text cannot tell whether the file ended with a newline, so a Read without the structure is NOT a base). -> [{index (tool_use ordinal, as in inventory), pos, result_pos (common timeline of
    jevref.timeline), path, cwd, content, complete, file}]; an OpenCode read has no structure, so it is listed as incomplete: evidence, never a base."""
    recs = load_jsonl(path); out, idx, uses = [], 0, {}
    upos, rpos = J.timeline(recs)
    for d in recs:
        msg = d.get("message") if isinstance(d.get("message"), dict) else {}; cont = msg.get("content")
        if d.get("type") == "assistant" and isinstance(cont, list):
            for c in cont:
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    idx += 1
                    if c.get("name") == "Read" and isinstance(c.get("input"), dict): uses[c["id"]] = (idx, c["input"], d.get("cwd"))
        elif d.get("type") == "user" and isinstance(cont, list):
            sr = d.get("toolUseResult", d.get("tool_use_result"))
            for c in cont:
                if not (isinstance(c, dict) and c.get("type") == "tool_result" and c.get("tool_use_id") in uses): continue
                i, inp, cwd = uses[c["tool_use_id"]]
                f = sr.get("file") if isinstance(sr, dict) else None
                fp = inp.get("file_path"); sp = f.get("filePath") if isinstance(f, dict) else None   # both resolved in the cwd recorded for the call: a relative call path with an absolute structured path is the same file
                complete = (isinstance(fp, str) and not c.get("is_error") and "offset" not in inp and "limit" not in inp and isinstance(sr, dict) and sr.get("type") == "text" and isinstance(f, dict)
                            and isinstance(f.get("content"), str) and f.get("startLine") == 1 and isinstance(f.get("numLines"), int) and f.get("numLines") == f.get("totalLines")
                            and (sp is None or (real_of(fp, cwd) is not None and real_of(sp, cwd) == real_of(fp, cwd))))
                out.append(dict(index=i, pos=upos.get(c["tool_use_id"]), result_pos=rpos.get(c["tool_use_id"]), path=fp, cwd=cwd, content=f["content"] if complete else None, complete=bool(complete), file=path))
    return out

def name_matches(path):
    return bool(NAME_RX.search(os.path.basename(path)))

def is_text_note(path):
    """An established text-note format: .md / .markdown / .txt (any case) or no extension."""
    ext = os.path.splitext(path)[1]
    return ext == "" or ext.lower() in TEXT_EXT

def auto_name(path):
    """Automatic inclusion by NAME: the handoff name rule AND an established text-note format (a code or data file such as test_handoff_flow.py or handoff.json is not a note whatever its name)."""
    return name_matches(path) and is_text_note(path)

TARGET_EXPLANATION = "explicit target: its extension is not an established text-note format (.md, .markdown, .txt or none), so it is never discovered by name; it is handled only because it was selected with --target"

def classify_candidates(items):
    """Split successful Write/Edit into: included_by_name, needs_jev_classify (text files with other names), excluded_non_text, failed."""
    out = {"included_by_name": [], "needs_jev_classify": [], "excluded_non_text": [], "failed": []}
    for it in items:
        if not it["success"]: out["failed"].append(it); continue
        p = it["path"]
        if auto_name(p): out["included_by_name"].append(it)
        elif p.lower().endswith(TEXT_EXT): out["needs_jev_classify"].append(it)
        else: out["excluded_non_text"].append(it)
    return out

def alias_info(path):
    """symlink resolved to real path; alias kept."""
    real = os.path.realpath(path)
    return {"path": path, "real": real, "is_alias": real != os.path.abspath(path), "exists": os.path.exists(real)}

def apply_classification(item, label, confidence):
    """jev_classify outcome handling. label in handoff|non-handoff|manual_review. >0.95 decides; else 'possible_handoff_unconfirmed'."""
    from report import passes
    if label == "handoff" and passes(confidence): return "included"
    if label == "non-handoff" and passes(confidence): return "excluded: non-handoff (confidence %s)" % confidence
    return "possible_handoff_unconfirmed"
