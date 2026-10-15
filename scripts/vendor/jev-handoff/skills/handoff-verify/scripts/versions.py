#!/usr/bin/env python3
"""Version identity and the delivered-version gate (R02). Stdlib only, read-only: it parses Claude Code JSONL transcripts and files and never executes anything it reads.

A handoff VERSION is one successful Write/Edit of a path, reconstructed with slice.reconstruct (Edit on an unknown base / ambiguous Edit stays `content not recoverable`).
`source_session_jsonl` = the transcript where the handoff was written; `calls_session_jsonl` = the transcript holding the real Jev calls. They are the same file for a handoff generated
in the current session (chronology applies: a call verifies a version only after that write and before the next write/edit of the same path) and different files for the retrospective
verification of a saved session (no cross-transcript chronology). A check names the version it evaluates with `version_ref` = {write_tool_use_id, sha256, evaluated_against[, run]} (`run` = the verification run of a session_end evaluation, omissions.source_window); the model never types
those values: it copies them from `versions.py list`. Absent or inconsistent identity is never deduced from the last listed version nor from the call's window.

CLI:
  versions.py list   [--source ID|PATH.jsonl|opencode:ID|opencode-db:/ABS/DB#ID] --file HANDOFF [--evaluated-against prefix|session_end] [--cwd DIR] [--run ID]   exit 0 unambiguous, 3 absent/ambiguous source or unidentifiable path
                     (session_end: each version lists the verification `runs` that follow it; `version_ref.run` is set when exactly one does or when `--run` names one: copy it into every check)
  versions.py status [--session ID|PATH.jsonl|opencode:ID|opencode-db:/ABS/DB#ID] --file HANDOFF --report R.verify.json [--cwd DIR]                  exit 0 verified_version, 2 needs_reverification, 3 unresolved
`verified_version` = the delivered (last) version has a valid report with >= 1 genuinely bound check; it does NOT mean PASS."""
import argparse, datetime, hashlib, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import discover as D, slice as SL, jevref as J, omissions as O

EVALUATED = ("prefix", "session_end")

def sha256_text(t): return hashlib.sha256(t.encode("utf-8")).hexdigest()

def _items(source_jsonl):
    items, _ = D.inventory(source_jsonl, "session")
    return items

def resolve_path(items, path, source_path=None):
    """-> (canonical path | None, how, note). The requested path must be, by canonical identity (realpath), a path written in the source transcript. A basename match is NEVER accepted.
    Retrospective relocation (the saved session wrote /work/x/handoff.md, the copy being verified lives elsewhere) needs an explicit `source_path` = the written path (the report's
    `handoff.source_path`, printed as `path` by `versions.py list`); the caller must additionally verify the copy's bytes against the version hash (bind_versions does)."""
    written = {D.real_of(i["path"], i.get("cwd")) for i in items}
    real = os.path.realpath(path)
    if real in written: return real, "realpath", None
    if isinstance(source_path, str) and source_path:
        sp = os.path.realpath(source_path)
        if sp in written: return sp, "relocated", None
        return None, "none", "handoff.source_path %s was not written in the source session" % source_path
    return None, "none", "%s is not (by canonical path) a file written in the source session%s" % (path, "; written: %s" % sorted(written)[:5] if written else "")

def _ms(ts):
    """Milliseconds since the epoch of an adapter timestamp (YYYY-MM-DDTHH:MM:SS.mmmZ), else None."""
    try: return int(datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=datetime.timezone.utc).timestamp() * 1000 + 0.5)
    except (TypeError, ValueError): return None

def _restores(d, upto):
    """Is the uncertain mutation `d` (an OpenCode diagnostic) demonstrably before a recoverable FULL Write among the mutations `upto` (inventory entries in order)? Only a valid recorded completion time of `d` followed
    STRICTLY by the tool time of that Write counts; message order, message time, equal bytes or a later positioned Write alone do not."""
    done = d.get("completed")
    if not (isinstance(done, int) and not isinstance(done, bool)): return False
    return any(w["op"] == "Write" and not w.get("unrecoverable") and isinstance(w.get("content"), str) and (_ms(w.get("timestamp")) or -1) > done for w in upto)

def _after(d, it):
    """Is the uncertain mutation `d` DEMONSTRABLY created strictly after the mutation `it` (an inventory entry) completed? Only a valid integer creation time of `d` against the valid recorded time of the result of `it`
    counts: such a mutation could not have changed the base of `it`. Everything else (no creation time, an equal or earlier one, no result time) is not evidence."""
    made, done = d.get("created"), _ms(it.get("result_timestamp"))
    return isinstance(made, int) and not isinstance(made, bool) and done is not None and made > done

def unpositioned_reason(v):
    """The blocker text for a version with `unpositioned` set (call ids), or None."""
    if not v.get("unpositioned"): return None
    why = v.get("unpositioned_why") or []
    return ("a successful mutation of the target has no demonstrated position (unpositioned: %s%s): the current delivery cannot be certified until a later full Write with a demonstrated order restores it"
            % (", ".join(v["unpositioned"]), "; " + "; ".join(why) if why else ""))

def mixed_reason(v):
    """The blocker text for a version of a path that other streams of the session also write (`mixed` = their files), or None."""
    if not v.get("mixed"): return None
    return ("the path was written in several streams of the session (also: %s): no common order between independent streams is demonstrated, so the combined history, the last version and the windows that depend on their order are unresolved "
            "(the bytes of each full Write stay)" % ", ".join(os.path.basename(f) for f in v["mixed"]))

_STREAMS = {}
def other_streams(source_jsonl, real):
    """The subagent transcripts of the session `source_jsonl` that also hold a successful Write/Edit of the canonical path `real` (the streams of ONE session whose order against the parent is not demonstrated)."""
    out = []
    for f in D.subagent_files(source_jsonl):
        try: key = D.content_fingerprint(f)
        except OSError: continue
        if key not in _STREAMS: _STREAMS[key] = {D.real_of(i["path"], i.get("cwd")) for i in D.inventory(f, "subagent")[0] if i["success"]} - {None}
        if real in _STREAMS[key]: out.append(f)
    return out

def reconstruct_stream(file, source, real, items=None, reads=None, diagnostics=None, mixed_with=()):
    """The ONE reconstruction of the versions of `real` (canonical path) in ONE transcript `file` of kind `source` ("session" | "subagent"), shared by versions_of, replay_all and prepare.py: the successful Write/Edit of the
    path (inventory; a relative path resolves against the cwd recorded for the call) in order, the demonstrably complete positioned Reads of the same path as refreshing bases (slice.reconstruct) and the OpenCode
    diagnostics that may affect the path. `items` / `reads` / `diagnostics` may be passed to avoid re-reading `file`. `mixed_with` = the other streams of the session that write the same path (`other_streams`).
    UNCERTAINTY is consumed here, for every dependent mutation: an Edit is `content not recoverable` while an OpenCode diagnostic that may affect the path and may have preceded it (not demonstrably created after its result)
    is not restored by a later full Write with a demonstrated order (`unpositioned` of a version still lists every unrestored diagnostic: it blocks the current delivery, not the historical reconstruction),
    and in every Edit of a path that other streams write too (their interleaving is not demonstrated); full Writes keep their bytes. -> [dict(version, write_tool_use_id, uuid, index, pos, result_pos, op, cwd, line, timestamp,
    status, reason, content, sha256, source_file, source_kind, unpositioned [call ids], unpositioned_why, mixed [files])]. Streams are never merged here: a path written in several streams is reconstructed once per stream."""
    if items is None: items = _items_of(file, source)
    if reads is None: reads = D.reads(file)
    if diagnostics is None: diagnostics = D.diagnostics(file)
    its = sorted([i for i in items if D.real_of(i["path"], i.get("cwd")) == real], key=lambda x: x["index"])
    rds = [dict(r, path=real) for r in reads if r["complete"] and isinstance(r["path"], str) and D.real_of(r["path"], r.get("cwd")) == real]
    aff = [d for d in diagnostics if d.get("path") is None or d["path"] == real]; mixed = list(mixed_with or ()); opens, taint, seen = {}, {}, []
    for it in (i for i in its if i["success"]):
        if it["op"] == "Write": seen.append(it)
        opens[it["tool_use_id"]] = [d for d in aff if not _restores(d, seen)]      # the blocker of the CURRENT delivery: every uncertain mutation that no later full Write demonstrably restores
        why, may = [], [d for d in opens[it["tool_use_id"]] if not _after(d, it)]  # the HISTORY: an Edit is only affected by the ones that may have preceded it
        if may: why.append("an OpenCode mutation of the path has no demonstrated position (%s): the base of this Edit may have been changed by it" % ", ".join(d["call_id"] for d in may))
        if mixed: why.append("the path is also written in another stream (%s): the interleaving of independent streams is not demonstrated, so the base of this Edit may have been changed" % ", ".join(os.path.basename(f) for f in mixed))
        if why and it["op"] == "Edit": taint[it["tool_use_id"]] = "; ".join(why)
    vs, op = SL.reconstruct(its, rds, taint), {i["tool_use_id"]: i for i in its}; out = []
    for v in vs:
        it = op[v["tool_use_id"]]; open_ = opens.get(v["tool_use_id"], [])
        out.append(dict(version=v["version"], write_tool_use_id=v["tool_use_id"], uuid=v["uuid"], index=v["index"], pos=it.get("pos"), result_pos=it.get("result_pos"), op=it["op"], cwd=it.get("cwd"), line=it.get("line"),
                        timestamp=v["timestamp"], status=v["status"], reason=v["reason"], content=v["content"], sha256=sha256_text(v["content"]) if v["content"] is not None else None, source_file=file, source_kind=source,
                        unpositioned=[d["call_id"] for d in open_], unpositioned_why=["%s: %s" % (d["call_id"], d["reason"]) for d in open_], mixed=list(mixed)))
    return out

def _items_of(file, source): return D.inventory(file, source)[0]

def versions_of(source_jsonl, path, source_path=None, also=()):
    """-> (versions, canonical_path, how, note). `also` = other representations of the SAME session (a copy or the original of the transcript, `same_session`): the subagent streams that any of them holds make the path `mixed`
    (a copy without the subagent directory must not hide what the original demonstrates). version = dict(version, write_tool_use_id, uuid, index, pos, result_pos, op, status, reason, content, sha256, next_index, next_pos, unpositioned, ...).
    `index` = 1-based ordinal of the write's tool_use among ALL tool_use blocks; `pos`/`result_pos` = positions of the write's tool_use and tool_result on the common event timeline
    (jevref.timeline); `next_pos` = `pos` of the next successful write/edit of the path (None = last). The versions of ONE transcript (`reconstruct_stream`): complete, positioned Reads of the same path refresh the
    base of an Edit; `unpositioned` = the OpenCode mutations without a demonstrated position that may affect the path; `mixed` = the other streams (subagent transcripts of the session) that write the same path, so no order is demonstrated."""
    items = _items(source_jsonl)
    canon, how, note = resolve_path(items, path, source_path)
    if canon is None: return [], None, how, note
    mixed = list(dict.fromkeys(f for src in [source_jsonl] + [a for a in also or () if isinstance(a, str) and a] for f in other_streams(src, canon)))
    out = reconstruct_stream(source_jsonl, "session", canon, items=items, mixed_with=mixed)
    for k, v in enumerate(out): v["next_index"] = out[k + 1]["index"] if k + 1 < len(out) else None; v["next_pos"] = out[k + 1]["pos"] if k + 1 < len(out) else None
    return out, canon, how, (None if out else "no successful Write/Edit of %s in the source session" % os.path.basename(path))

RANK = {"recorded_write": 3, "failed_writes_only": 2, "bash_only": 1, "no_record": 0}

def provenance(source, path, source_path=None, relocate=True):
    """Does the checked session hold a recorded, supported write of `path`? -> dict(state, blocker, path, via, source_path). state: `recorded_write` (a successful Write/Edit of the canonical path; `via` = "direct", or "relocated"
    when `path` is a verified copy of a written note: `source_path` names the written path, given explicitly or, with `relocate`, demonstrated by relocation_candidate), `failed_writes_only`, `bash_only` (created only by a
    shell redirect) or `no_record`. Without a recorded write there is no write identity and no window, so nothing version-dependent can be scheduled: `blocker` says so precisely (None when recorded). Equal paths,
    disk bytes or a text that merely equals the note are never provenance."""
    items, bash = D.inventory(source, "session"); real = os.path.realpath(path)
    mine = [i for i in items if D.real_of(i["path"], i.get("cwd")) == real]
    def rec(via, sp=None): return dict(state="recorded_write", blocker=None, path=real, via=via, source_path=sp)
    if any(i["success"] for i in mine): return rec("direct")
    if isinstance(source_path, str) and source_path:
        sp = os.path.realpath(source_path)
        if any(i["success"] and D.real_of(i["path"], i.get("cwd")) == sp for i in items): return rec("relocated", sp)
    elif relocate and not mine:
        reloc, _ = relocation_candidate(source, path)
        if reloc: return rec("relocated", reloc)
    if mine: state, detail = "failed_writes_only", "only failed Write/Edit calls of this path are recorded"
    elif any(D.real_of(b["path"], b.get("cwd")) == real for b in bash): state, detail = "bash_only", "it was created only through a Bash redirect, which is not a supported write"
    elif other_streams(source, real): state, detail = "no_record", "it was written only in a subagent transcript of the session, which is not a version of this session (prepare and verify that transcript as its own session)"
    else: state, detail = "no_record", "the session never wrote it with a Write/Edit"
    return dict(state=state, path=real, via=None, source_path=None, blocker="%s has no recorded supported Write/Edit in the source session (%s), so no write identity or window exists; a certifiable note needs a recorded supported Write/Edit of the checked session" % (path, detail))

def relocation_candidate(source_jsonl, path):
    """Explicit retrospective relocation for `versions.py list`: the file at `path` is a COPY of a handoff written elsewhere in the source session. -> (written path | None, note). Demonstrated only when
    exactly one written path has the same basename AND the bytes at `path` hash to a recoverable version of that written path. The report must then carry that path as `handoff.source_path`."""
    items = _items(source_jsonl); cand = sorted({D.real_of(i["path"], i.get("cwd")) for i in items if os.path.basename(i["path"]) == os.path.basename(path)} - {None})
    if len(cand) != 1: return None, "no unique written path named %s in the source session (%s)" % (os.path.basename(path), cand)
    try: sha = hashlib.sha256(open(path, "rb").read()).hexdigest()
    except OSError: return None, "the copy %s cannot be read: relocation not verified" % path
    if sha in {v["sha256"] for v in versions_of(source_jsonl, path, cand[0])[0] if v["sha256"]}: return cand[0], None
    return None, "the bytes of %s do not hash to any recoverable version of %s" % (path, cand[0])

def version_ref(v, evaluated_against="session_end"):
    return dict(write_tool_use_id=v["write_tool_use_id"], sha256=v["sha256"], evaluated_against=evaluated_against)

def replay_all(source_jsonl):
    """tool_use_id -> (content | None, sha256 | None) of EVERY successful Write/Edit of the transcript, per path, via versions.reconstruct_stream (the same reconstruction as versions_of and prepare.py:
    one implementation, with the demonstrably complete Reads of the same path as Edit bases)."""
    items = _items(source_jsonl); reads = D.reads(source_jsonl); diags = D.diagnostics(source_jsonl); out = {}
    for real in {D.real_of(i["path"], i.get("cwd")) for i in items} - {None}:
        for v in reconstruct_stream(source_jsonl, "session", real, items=items, reads=reads, diagnostics=diags, mixed_with=other_streams(source_jsonl, real)): out[v["write_tool_use_id"]] = (v["content"], v["sha256"])
    return out

def _identity(path):
    """(session ids named in the transcript metadata, {tool_use_id: (tool name, input hash)}) of a JSONL transcript or stream; ({}, {}) when unreadable."""
    sids, tools = set(), {}
    try:
        for d in D.load_jsonl(path):
            for k in ("sessionId", "session_id"):
                if isinstance(d.get(k), str): sids.add(d[k])
            msg = d.get("message") if isinstance(d.get("message"), dict) else {}
            if d.get("type") == "assistant" and isinstance(msg.get("content"), list):
                for b in msg["content"]:
                    if isinstance(b, dict) and b.get("type") == "tool_use": tools[b["id"]] = (b.get("name"), J.input_hash(b.get("input")))
    except OSError: return set(), {}
    return sids, tools

def same_session(report_source, calls_path, session_id=None):
    """Is the report's `session.jsonl` the CURRENT (calls) transcript? Demonstrated only by (a) the same file (realpath), or (b) a copy/stream of the same session: both files exist, their real
    metadata name a common session id (and `session_id`, when given, is named by the report's transcript) and their tool_use records correspond (same ids with the same tool and input,
    one transcript contained in the other, at least one in common). A path that does not exist, or a file with the same basename/stem but another content, is NOT evidence."""
    if not isinstance(report_source, str) or not report_source or not isinstance(calls_path, str) or not D.source_exists(report_source) or not D.source_exists(calls_path): return False
    if D.canon(report_source) == D.canon(calls_path): return True
    if D.is_selector(report_source) or D.is_selector(calls_path): return False   # an OpenCode selector is the same session only as the canonical-equal selector, never as a Claude JSONL
    sa, ta = _identity(report_source); sb, tb = _identity(calls_path)
    if session_id: sid_ok = session_id in sa and (not sb or session_id in sb)
    else: sid_ok = bool(sa & sb)
    common = set(ta) & set(tb)
    return bool(sid_ok and common and all(ta[i] == tb[i] for i in common) and (set(ta) <= set(tb) or set(tb) <= set(ta)))

def distinct_sessions(a, b):
    """Is it DEMONSTRATED that the transcripts `a` and `b` are two different sessions? Not the mere failure of `same_session` (which is also false when an identity cannot be shown): both must exist and be readable with a recorded
    identity, and either they are two different OpenCode selectors, or both are JSONL transcripts that name session ids (metadata) and tool uses, and no session id is common. An empty, malformed, unreadable, metadata-deficient or mixed
    (selector / JSONL) pair is NOT demonstrated distinct."""
    try:
        if not (isinstance(a, str) and a and isinstance(b, str) and b and D.source_exists(a) and D.source_exists(b)): return False
        if D.canon(a) == D.canon(b): return False
        if D.is_selector(a) or D.is_selector(b): return bool(D.is_selector(a) and D.is_selector(b))
        sa, ta = _identity(a); sb, tb = _identity(b)
        return bool(sa and sb and ta and tb and not (sa & sb))
    except Exception: return False

def validate_ref(ref, vs, call=None, same=False):
    """-> (kind, reason, version). kind: ok | absent | inconsistent | window | unrecoverable | unpositioned | unordered (`bind_versions` adds `run`: the evaluation, i.e. the verification run, is not demonstrated). call = the bound Jev call (dict with pos/result_pos on the transcript's event timeline, same transcript only).
    Window (R02 contract): the call's tool_use comes after the version's write result and the call's RESULT arrives before the next successful write/edit of the same path."""
    if not isinstance(ref, dict): return "absent", "no version_ref: version identity absent", None
    wid, sh, ev = ref.get("write_tool_use_id"), ref.get("sha256"), ref.get("evaluated_against")
    if not isinstance(wid, str) or not isinstance(sh, str) or ev not in EVALUATED: return "inconsistent", "malformed version_ref (write_tool_use_id, sha256, evaluated_against prefix|session_end)", None
    if ref.get("run") is not None and (not isinstance(ref["run"], str) or not ref["run"] or ev != "session_end"): return "inconsistent", "malformed version_ref (run is a non-empty tool_use id and only exists for session_end)", None
    v = next((x for x in vs if x["write_tool_use_id"] == wid), None)
    if v is None: return "inconsistent", "version_ref.write_tool_use_id is not a write of this handoff in the source transcript", None
    if v["status"] != "ok" or v["sha256"] is None: return "unrecoverable", "version content not recoverable (%s)" % v["reason"], v
    if v["sha256"] != sh: return "inconsistent", "version_ref.sha256 differs from the reconstructed version", v
    if same and v.get("unpositioned"): return "unpositioned", unpositioned_reason(v), v   # a mutation of the path without a demonstrated position: no certificate in the current session (a retrospective check is not affected)
    if same and v.get("mixed"): return "unordered", mixed_reason(v), v                    # the path is written in several streams: no demonstrated order, no same-session window
    if same and call is not None:
        cp, cr, start = call.get("pos"), call.get("result_pos"), v.get("result_pos") or v.get("pos")
        if not (isinstance(cp, int) and isinstance(cr, int) and isinstance(start, int) and cp > start and (v.get("next_pos") is None or cr < v["next_pos"])):
            return "window", "call or its result is outside the window of the referenced version (call after its write; result before the next write/edit of the same path)", v
    return "ok", "ok", v

def _holds(recs, tool_use_id):
    """Does the transcript (records) hold a tool_use with this id?"""
    for d in recs or ():
        c = (d.get("message") if isinstance(d, dict) and isinstance(d.get("message"), dict) else {}).get("content")
        if any(isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id") == tool_use_id for b in (c if isinstance(c, list) else [])): return True
    return False

def run_problem(source_jsonl, ref, also=()):
    """-> ("ok", "ok") or ("run", reason): is the EVALUATION a valid version_ref names demonstrated in the transcript? The same window every consumer uses (omissions.source_window): several verification runs after the write
    without `run`, or a `run` that is none of them, make the evaluation invalid (kind `run`): no check about it certifies a delivery. An unresolvable event or an unreadable transcript is not a run problem (only the source is unavailable then).
    `also` = the other representations of the SAME session (a copy or the original of the transcript): the evaluation is judged in EVERY representation that holds the write, so a copy that stops after the first run cannot hide the
    second run of the original (nor the other way round). Other representations only INVALIDATE: their positions are never mixed with those of `source_jsonl` (nothing proves they correspond), the source and the window of the
    check stay those of its own transcript. A run that is NAMED stays valid when some representation demonstrates it and the others do not hold it at all (a shorter copy)."""
    files, seen = [], set()
    for f in [source_jsonl] + [x for x in also or () if isinstance(x, str) and x]:
        try: k = D.canon(f)
        except (OSError, ValueError, TypeError): k = f
        if k not in seen and f: seen.add(k); files.append(f)
    named, bad, skipped, good = ref.get("run") if isinstance(ref, dict) else None, [], [], 0
    for f in files:
        try: w, recs = O.window_for(f, ref["write_tool_use_id"], ref["evaluated_against"], named)
        except (OSError, ValueError, KeyError, TypeError): continue
        if not w: continue
        if w["ambiguous"] and w.get("kind") == "run":
            if isinstance(named, str) and not _holds(recs, named) and len(files) > 1: skipped.append((f, w["ambiguous"])); continue        # a shorter representation that never recorded the named run
            bad.append((f, w["ambiguous"]))
        elif not w["ambiguous"]: good += 1
    if skipped and not good and not bad: bad = skipped          # no representation demonstrates the named run
    if bad: return "run", "evaluation not demonstrated: " + bad[0][1] + ("" if len(files) < 2 else " (judged in %s)" % os.path.basename(bad[0][0]))
    return "ok", "ok"

def bind_versions(checks, bindings, handoff_path, source_jsonl, calls, same, source_path=None, work_locations=(), also=()):
    """-> {check id: dict(kind, ok, reason, version, sha256, write_tool_use_id, text)} for every BOUND check. Unbound checks have no row (they are unresolved already).
    `source_path` (retrospective only, never with `same`): explicit relocation, additionally requires the bytes of the file at `handoff_path` to hash to a recoverable version of the written handoff.
    `work_locations` = the user locations of the report (`doc["work_locations"]`), added to the bases of the omission material exactly as `omissions.py prepare --location` does; one that is invalid or gone makes the material unavailable.
    `also` = the other representations of the same session (see `versions_of`); only used when `same`."""
    loc_why = None
    if work_locations is None: work_locations = ()
    if not isinstance(work_locations, (list, tuple)) or not all(isinstance(x, str) for x in work_locations): loc_why = "work_locations of the report is not a list of directory strings: the complete material is not demonstrated"
    else:
        for x in work_locations:
            w = O.bad_location(x)
            if w: loc_why = "work location %r %s at report time: the complete material is not demonstrated" % (x, "is not an absolute path" if w == "relative" else "does not exist"); break
    cm = {c["tool_use_id"]: c for c in calls}
    vs, note, how, canon = [], None, "none", None
    try:
        vs, canon, how, note = versions_of(source_jsonl, handoff_path, None if same else source_path, also if same else ()) if source_jsonl and D.source_exists(source_jsonl) else ([], None, "none", "source transcript unavailable: provenance not demonstrated")
    except Exception as e:
        note = "source transcript unreadable: %s" % e
    disk = None
    if how == "relocated":
        try: disk = hashlib.sha256(open(handoff_path, "rb").read()).hexdigest()
        except OSError: disk = None
    rows = {}
    for c, b in zip(checks, bindings):
        if not b["bound"]: continue
        if not vs:
            kind, why, v = "inconsistent", note or "no versions of the handoff in the source transcript", None
        else:
            kind, why, v = validate_ref(c.get("version_ref"), vs, cm.get(b["tool_use_id"]), same)
            if kind == "ok" and canon and source_jsonl: kind, why = run_problem(source_jsonl, c.get("version_ref"), also if same else ())
            if kind == "ok" and how == "relocated" and disk not in {x["sha256"] for x in vs if x["sha256"]}: kind, why = "inconsistent", "relocated copy: the bytes at handoff.path do not hash to any recoverable version of the written handoff"
        row = dict(kind=kind, ok=kind == "ok", reason=why, version=v["version"] if v else None, sha256=v["sha256"] if v else None, write_tool_use_id=v["write_tool_use_id"] if v else None,
                   text=v["content"] if kind == "ok" else None, evaluated_against=(c.get("version_ref") or {}).get("evaluated_against") if isinstance(c.get("version_ref"), dict) else None,
                   run=(c.get("version_ref") or {}).get("run") if isinstance(c.get("version_ref"), dict) else None)
        if kind == "ok" and canon and row["evaluated_against"] in EVALUATED:   # R03: what the omission pair is validated against (eligible source of THIS version, canonical material)
            if loc_why: row["omission"] = dict(eligible_source=None, eligible_blocks=None, material=None, material_reason=loc_why)
            else:
              try: row["omission"] = O.context(source_jsonl, canon, v, row["evaluated_against"], O.material_bases(handoff_path, v, work_locations), row["run"])
              except Exception as e: row["omission"] = dict(eligible_source=None, material=None, material_reason="omission context unavailable: %s" % e)
        rows[b["id"]] = row
    return rows

def attach(bindings, rows):
    """Bindings with the version outcome: a bound check without a valid version identity becomes UNRESOLVED (never PASS); findings of a check validate quote_handoff against ITS version text."""
    out = []
    for b in bindings:
        r = rows.get(b["id"])
        if r is None: out.append(b); continue
        b = dict(b, version={k: r[k] for k in ("kind", "ok", "reason", "version", "sha256", "write_tool_use_id", "evaluated_against", "run")})
        if r["ok"]:
            b["version_text"] = r["text"]
            if r.get("omission"): b.update(eligible_source=r["omission"]["eligible_source"], eligible_blocks=r["omission"].get("eligible_blocks"), omission_material=r["omission"]["material"], omission_material_reason=r["omission"]["material_reason"])
        else: b["resolved"] = False
        out.append(b)
    return out

def per_version(checks, bindings, findings, declared_unresolved, declared_versions, omission_contract=False, audit_ctx=None):
    """Status/binding summary per WRITE (not per hash: A -> B -> A has two distinct writes of identical bytes), over the checks explicitly attributed to it by the verified version identity
    (`version.write_tool_use_id` + sha256). -> {write_tool_use_id: dict(sha256, write_tool_use_id, audited_status, binding_summary, evaluations={evaluated_against: dict(audited_status, binding_summary)})}.
    Checks and findings are never shared between writes with the same hash; the evaluations of one write against `prefix` and `session_end` are kept apart (the write-level status covers both).
    A declared version (handoff.versions entry) without checks of its own is not in the result: use `lookup` (UNRESOLVED stub).
    Every status here (the write-level summary AND each prefix / session_end evaluation) goes through the mandatory full-report audit (`audit_ctx`, an audit.Context of the report; audit.certify): a PASS needs a COMPLETE audit of
    the evaluations of that summary, a FAIL keeps its precedence. Without an `audit_ctx` nothing can be audited, so a PASS is capped (audit.no_context)."""
    ok = [b for b in bindings if b.get("version") and b["version"]["ok"]]
    out = {}
    def summary(ids):
        cs = [c for c in checks if c.get("id") in ids]; bs = [b for b in bindings if b["id"] in ids]
        fs = [f for f in findings if isinstance(f, dict) and f.get("check_id") in ids]; un = [u for u in declared_unresolved if isinstance(u, dict) and u.get("check_id") in ids]
        ev = J.audited_status(cs, bs, fs, un, None, omission_contract)
        import audit as A
        ev = A.certify(ev, audit_ctx, list(dict.fromkeys(A.key_of(b["version"]) for b in bs if b.get("bound") and b.get("version") and b["version"].get("ok"))), fs) if audit_ctx is not None else A.no_context(ev)   # each distinct evaluation once: two checks of one evaluation are one evaluation
        return dict(audited_status=ev["status"], binding_summary=dict(checks=ev["checks"], bound=ev["bound"], resolved=ev["resolved"], unbound=ev["unbound"], reasons=ev["reasons"], valid_findings=ev["valid_findings"], audit=ev["audit"]))
    ref_of = {c.get("id"): (c.get("version_ref") if isinstance(c.get("version_ref"), dict) else {}) for c in checks if isinstance(c, dict)}
    for wid in dict.fromkeys(b["version"]["write_tool_use_id"] for b in ok):
        mine = [b for b in ok if b["version"]["write_tool_use_id"] == wid]; ids = {b["id"] for b in mine}
        ev = dict(summary(ids), sha256=mine[0]["version"]["sha256"], write_tool_use_id=wid)
        ev["evaluations"] = {e: summary({i for i in ids if ref_of.get(i, {}).get("evaluated_against") == e}) for e in EVALUATED if any(ref_of.get(i, {}).get("evaluated_against") == e for i in ids)}
        out[wid] = ev
    return out

def write_ids(source_jsonl, path, source_path=None):
    """Ids of every successful write/edit of the handoff in the source transcript (None when it cannot be read)."""
    try: return {v["write_tool_use_id"] for v in versions_of(source_jsonl, path, source_path)[0]} if source_jsonl and D.source_exists(source_jsonl) else None
    except Exception: return None

def lookup(pv, declared, writes=None):
    """The per_version entry of a declared handoff.versions entry: by its `tool_use_id` (the write id); the sha256 fallback is used ONLY when that id is not a known write of the transcript
    (`writes`; a placeholder id) and exactly one write with checks has that hash. A declared real write without checks of its own never borrows the checks of an earlier write with the same bytes.
    -> (entry | None, how)."""
    if not isinstance(declared, dict): return None, "malformed"
    tid = declared.get("tool_use_id")
    if tid in pv: return pv[tid], "write id"
    if writes is not None and tid in writes: return None, "no check of its own (a write with the same bytes does not transfer its checks)"
    cand = [e for e in pv.values() if e["sha256"] == declared.get("sha256")]
    if len(cand) == 1: return cand[0], "unique sha256"
    return None, "ambiguous sha256 (several writes)" if cand else "no check of its own"

STALE_NOTICE = ("Atenție: acest raport este despre versiunea %d (sha %s); nota are acum o versiune mai nouă, %d (sha %s), pe care acest raport nu o acoperă. "
                "Rulează verificarea din nou pe versiunea curentă.")

def stale_notice(stale):
    """The exact notice for a `stale` dict (report_version, report_sha256, newer_version, newer_sha256)."""
    return STALE_NOTICE % (stale["report_version"], stale["report_sha256"][:8], stale["newer_version"], stale["newer_sha256"][:8])

def latest_versions(calls_path, handoff_path, doc, session_id=None):
    """-> (versions of the handoff path, canonical path, reason | None): the AUTHORITATIVE list for the delivered-version gate. It starts from the calls log; when the report names another transcript of the SAME
    session (same_session) that records more writes of the path and the calls log's writes are a prefix of them, that longer list wins: a strict-prefix copy of the transcript must not hide a later write. Whichever
    list is chosen, what ANY representation of the session demonstrates holds for it: the subagent streams of the original make the path `mixed` also when the copy has none (`versions_of(..., also=)`).
    Writes that do not extend each other are not demonstrated to be one session history (reason set)."""
    vs, canon, how, note = versions_of(calls_path, handoff_path)
    src = (doc.get("session") or {}).get("jsonl") if isinstance(doc, dict) and isinstance(doc.get("session"), dict) else None
    if not vs or not isinstance(src, str) or D.canon(src) == D.canon(calls_path) or not same_session(src, calls_path, session_id): return vs, canon, note
    vs, canon, how, note = versions_of(calls_path, handoff_path, None, [src])
    try: sv, scanon, _, _ = versions_of(src, handoff_path, None, [calls_path])
    except Exception: return vs, canon, None
    a, b = [v["write_tool_use_id"] for v in vs], [v["write_tool_use_id"] for v in sv]
    if scanon != canon or not sv: return vs, canon, None
    if b[:len(a)] == a: return (sv if len(b) > len(a) else vs), canon, None
    if a[:len(b)] == b: return vs, canon, None   # the report's transcript is the shorter copy
    return vs, canon, "the writes of the handoff in the calls log and in the report's transcript do not extend each other: one session history is not demonstrated"

def stale_of(doc, vs):
    """-> dict(report_version, report_sha256, newer_version, newer_sha256) when the highest version cited by a valid version_ref of the report's checks is older than the latest write, else None."""
    if not isinstance(doc, dict) or not isinstance(doc.get("checks"), list) or not vs or vs[-1]["status"] != "ok": return None
    cited = [v for c in doc["checks"] if isinstance(c, dict) for k, _, v in [validate_ref(c.get("version_ref"), vs)] if k == "ok"]
    if not cited: return None
    top = max(cited, key=lambda v: v["version"])
    if top["version"] >= vs[-1]["version"]: return None
    return dict(report_version=top["version"], report_sha256=top["sha256"], newer_version=vs[-1]["version"], newer_sha256=vs[-1]["sha256"])

def gate(calls_path, handoff_path, doc, disk_path=None, session_id=None, omission_contract="R04"):
    """The delivered-version gate (current contract R04; "R03" / False (R02) only when passed explicitly for a historical/baseline evaluation; `jevref.contract_of`). -> dict(delivery_state, current_sha256, latest_write_id, report_path?, audited_status, bound_checks_for_version, reasons). Same code for the CLI, the report
    writer and the auditor. `disk_path` = file whose bytes are compared (default: handoff_path)."""
    res = dict(delivery_state="unresolved", current_sha256=None, latest_write_id=None, audited_status=None, audit=None, bound_checks_for_version=0, reasons=[])
    ctx = dict(vs=[])
    def fin(state, why):
        res["delivery_state"] = state; res["reasons"].append(why)
        st = stale_of(doc, ctx["vs"])   # also when the state is unresolved for another reason: a reader must learn that a newer version exists
        if st: res["stale"] = st; res["stale_notice"] = stale_notice(st)
        return res
    try: vs, canon, note = latest_versions(calls_path, handoff_path, doc, session_id)
    except Exception as e: return fin("unresolved", "session transcript unreadable: %s" % e)
    ctx["vs"] = vs
    if not vs: return fin("unresolved", note or "no successful Write/Edit of the handoff in this session")
    last = vs[-1]; res["latest_write_id"] = last["write_tool_use_id"]
    if vs and note: return fin("unresolved", note)
    if last["status"] != "ok": return fin("unresolved", "the last version is not recoverable: %s" % last["reason"])
    if last.get("unpositioned"): return fin("unresolved", unpositioned_reason(last))   # before the byte comparison: equal bytes cannot mask a mutation whose place in the history is unknown
    if last.get("mixed"): return fin("unresolved", mixed_reason(last))                 # several streams write the path: the last version of the combined history is not demonstrated
    dp = disk_path or handoff_path
    try: raw = open(dp, "rb").read()
    except OSError: return fin("unresolved", "the handoff file is absent on disk: %s" % dp)
    res["current_sha256"] = hashlib.sha256(raw).hexdigest()
    if res["current_sha256"] != last["sha256"]: return fin("unresolved", "bytes on disk differ from the last recorded write of this session (external change or unrecorded edit)")
    if not isinstance(doc, dict) or doc.get("schema_version") != "1": return fin("unresolved", "report missing or not schema v1")
    import schema_check as SC   # the structure of the WHOLE report comes before any field of it is read: a structurally invalid report certifies no delivery (an authentic, structurally valid UNRESOLVED report still can)
    problems = SC.validate_report(doc)
    if problems:   # nothing is recomputed from a malformed report (audited_status stays null), but the structural unfinished work is exposed
        res["audit"] = dict(complete=False, reasons=["the report does not match report.schema.json: " + SC.summarize(problems)], reasons_total=1, unfinished=dict(structure=len(problems)), evaluations=0, scope=dict(valid=0, invalid=0))
        return fin("unresolved", "the report is not structurally valid (report.schema.json), so it certifies no delivery: " + SC.summarize(problems))
    h = doc.get("handoff") if isinstance(doc.get("handoff"), dict) else {}
    if not isinstance(h.get("path"), str) or os.path.realpath(h["path"]) != os.path.realpath(handoff_path) or os.path.realpath(handoff_path) != canon: return fin("unresolved", "the report is about another handoff file (canonical path of the report, of the requested file and of the transcript writes must be identical)")
    src = (doc.get("session") or {}).get("jsonl")
    if not same_session(src, calls_path, session_id): return fin("unresolved", "the report's source session is not demonstrated to be the current session (a retrospective report cannot certify a current delivery)")
    calls = J.load_calls(calls_path); checks = doc["checks"]
    r03 = J.contract_of(omission_contract)   # the CURRENT contract is chosen by the evaluator (default R04: strict auxiliary conditions, explicit omission pair with the R04 absence half), never by a marker in the report; R03 / R02 only by an explicit selection (historical evaluation)
    bs = J.bind(checks, calls, r03); rows = bind_versions(checks, bs, handoff_path, calls_path, calls, True, None, doc.get("work_locations"), [src]); bs2 = J.finalize(attach(bs, rows), doc["findings"], None, r03)
    ev = J.audited_status(checks, bs2, doc["findings"], doc.get("unresolved", []), None, r03)
    import audit as A   # the SAME certifying function as the report writer: a PASS needs a complete audit, a validated defect keeps FAIL
    ev = A.certify(ev, A.Context(doc, checks, bs2, calls, calls_path, handoff_path, None, True, True, r03, False, [src]))   # the report's transcript is another representation of this session (same_session above): its registry and ledgers are part of the accounting
    res["audited_status"] = ev["status"]; res["audit"] = ev["audit"]
    if doc.get("status") != ev["status"]: return fin("unresolved", "stored report status %r differs from the recomputed %r (invalid or hand-edited report)" % (doc.get("status"), ev["status"]))
    # Identity validity comes BEFORE the verified_version branch: a valid check must not mask an absent/inconsistent/unrecoverable identity elsewhere in the same report. An unresolved check from
    # confidence/verdict (authentic UNRESOLVED, documents the verification done) is not an identity problem. A version_ref on an unbound check is validated too (it must not be wrong).
    invalid = {b["version"]["reason"] for b in bs2 if b["bound"] and b.get("version") and b["version"]["kind"] in ("absent", "inconsistent", "unrecoverable", "run")}
    for c, b in zip(checks, bs):
        if not b["bound"] and isinstance(c, dict) and c.get("version_ref") is not None:
            k, why, _ = validate_ref(c["version_ref"], vs)
            if k == "ok": k, why = run_problem(calls_path, c["version_ref"], [src])
            if k != "ok": invalid.add("unbound check %s: %s" % (c.get("id"), why))
    if invalid: return fin("unresolved", "invalid version identity in the report: " + "; ".join(sorted(invalid)))
    good = [b for b in bs2 if b["bound"] and b.get("version") and b["version"]["ok"] and b["version"]["write_tool_use_id"] == last["write_tool_use_id"] and b["version"]["sha256"] == last["sha256"]]   # THIS write, not any write with the same bytes
    res["bound_checks_for_version"] = len(good)
    kinds = [(b["version"]["kind"], b["version"]["write_tool_use_id"]) for b in bs2 if b["bound"] and b.get("version")]
    if good and any(k == "window" for k, _ in kinds): return fin("unresolved", "a check of the report cites a call or result outside its version window: the report is inconsistent")
    if good: return fin("verified_version", "the delivered version %s has %d genuinely bound check(s) in a valid report" % (last["sha256"][:8], len(good)))
    if any(k == "window" for k, _ in kinds) or any(k == "ok" and w != last["write_tool_use_id"] for k, w in kinds):
        return fin("needs_reverification", "no genuinely bound check for the delivered version %s: the report verifies an earlier version or cites calls made before the last write/edit" % last["sha256"][:8])
    return fin("unresolved", "no bound check with a demonstrated version identity for the delivered version (identity absent, unrecoverable or no bound check)")

def _resolve(arg, cwd):
    sp, how, amb = D.resolve_session_info(arg, cwd)
    return sp, how, amb

def cmd_list(a):
    sp, how, amb = _resolve(a.source, a.cwd)
    if amb: print(json.dumps({"error": "source session not demonstrated: several recently modified sessions; pass --source ID or PATH.jsonl", "resolution": how})); return 3
    if not sp or not D.source_exists(sp): print(json.dumps({"error": "source session not found", "arg": O.shown(a.source)})); return 3
    path = a.file if os.path.isabs(a.file) else os.path.join(a.cwd or os.getcwd(), a.file)
    prov = provenance(sp, path)   # preflight: no recorded supported write, no write identity or window
    if prov["state"] != "recorded_write": print(json.dumps({"error": prov["blocker"], "provenance": prov["state"], "source_session_jsonl": sp, "resolution": how}, ensure_ascii=False)); return 3
    vs, canon, pathhow, note = versions_of(sp, path)
    reloc = None
    if canon is None:
        reloc, rnote = relocation_candidate(sp, path)
        if reloc: vs, canon, pathhow, note = versions_of(sp, path, reloc)
        else: note = "%s; relocation: %s" % (note, rnote)
    if canon is None or not vs: print(json.dumps({"error": note or "path not identifiable", "source_session_jsonl": sp, "resolution": how})); return 3
    rows, recs, picked = [], (D.load_jsonl(sp) if a.evaluated_against == "session_end" else None), 0
    for v in vs:
        r = dict(version=v["version"], index=v["index"], uuid=v["uuid"], write_tool_use_id=v["write_tool_use_id"], op=v["op"], state=v["status"], sha256=v["sha256"], unpositioned=v["unpositioned"], mixed=v["mixed"])
        if v["status"] == "ok":
            r["version_ref"] = version_ref(v, a.evaluated_against)
            if recs is not None:   # the verification runs that follow this write: one is a demonstrated selection, several need `--run`
                w = O.source_window(recs, v["write_tool_use_id"], "session_end"); ids = [x["id"] for x in w["runs"]]
                if a.run in ids: w = O.source_window(recs, v["write_tool_use_id"], "session_end", a.run)
                r["runs"] = [x["id"] for x in w["runs"]]
                if w["run"]: r["version_ref"]["run"] = w["run"]; picked += 1 if a.run and w["run"] == a.run else 0
                if w["ambiguous"]: r["window"] = w["ambiguous"]
        else: r["reason"] = v["reason"]
        rows.append(r)
    if a.run and recs is not None and not picked: print(json.dumps({"error": "--run %s is not a verification run that starts after any listed version (see `runs` of each version)" % (O.shown(a.run, "tool_use_id") or "(the value is not echoed)"), "source_session_jsonl": sp, "resolution": how}, ensure_ascii=False)); return 3
    if a.run and recs is None: print(json.dumps({"error": "--run only applies to --evaluated-against session_end", "source_session_jsonl": sp, "resolution": how}, ensure_ascii=False)); return 3
    blockers = [x for x in (unpositioned_reason(next((v for v in reversed(vs) if v.get("unpositioned")), {})), mixed_reason(next((v for v in reversed(vs) if v.get("mixed")), {}))) if x]
    runs_note = ("Several verification runs follow a version (`runs`): name the one the check is about with `--run ID` (it is then printed in version_ref.run); without it the run is not demonstrated and the omission pair stays UNRESOLVED. "
                 if any(len(r.get("runs", [])) > 1 and "run" not in r.get("version_ref", {}) for r in rows) else "")
    print(json.dumps(dict(source_session_jsonl=sp, resolution=how, path=canon, path_match=pathhow, handoff_source_path=reloc, evaluated_against=a.evaluated_against, versions=rows, **({"blockers": blockers} if blockers else {}),
                          note=runs_note + ("A version with `unpositioned` or `mixed` set cannot certify the current delivery (see `blockers`). " if blockers else "") + "copy the version_ref object of the version you verify into each check; set handoff.path to `path` and session.jsonl to `source_session_jsonl`; when handoff_source_path is not null (the file is a verified copy of a handoff written elsewhere in that session) set handoff.path to the copy you were given and handoff.source_path to handoff_source_path"), indent=1, ensure_ascii=False)); return 0

def cmd_status(a):
    sp, how, amb = _resolve(a.session, a.cwd)
    def out(d, code): print(json.dumps(d, indent=1, ensure_ascii=False)); return code
    if amb or not sp or not D.source_exists(sp):
        return out(dict(delivery_state="unresolved", current_sha256=None, latest_write_id=None, report_path=O.shown(a.report), audited_status=None, bound_checks_for_version=0,
                        reasons=["current session not demonstrated (ambiguous: pass --session ID or PATH.jsonl)" if amb else "session not found"]), 3)
    path = a.file if os.path.isabs(a.file) else os.path.join(a.cwd or os.getcwd(), a.file)
    try: doc = json.load(open(a.report, encoding="utf-8"))
    except (OSError, ValueError) as e: doc = None
    g = gate(sp, path, doc, disk_path=path); g["report_path"] = O.shown(a.report)
    return out(g, {"verified_version": 0, "needs_reverification": 2}.get(g["delivery_state"], 3))

def build_parser():
    ap = argparse.ArgumentParser(); sub = ap.add_subparsers(dest="cmd", required=True)
    l = sub.add_parser("list"); l.add_argument("--source"); l.add_argument("--file", required=True); l.add_argument("--evaluated-against", choices=EVALUATED, required=True); l.add_argument("--cwd"); l.add_argument("--run")
    s = sub.add_parser("status"); s.add_argument("--session"); s.add_argument("--file", required=True); s.add_argument("--report", required=True); s.add_argument("--cwd")
    return ap

def main():
    a = build_parser().parse_args()
    return cmd_list(a) if a.cmd == "list" else cmd_status(a)

if __name__ == "__main__": sys.exit(main())
