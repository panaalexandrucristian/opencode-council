#!/usr/bin/env python3
"""Resumable obligation ledger of the omission audit (stdlib only, read-only on everything but the ledger file; no Jev call). It records PROGRESS, never acceptance: one row per candidate lost detail (an OBLIGATION), the references to the
real Jev calls made for it, and nothing else. The audit stays complete only when every obligation of all nine categories of useful detail is resolved by the report; the ledger never says so (`coverage.complete` is always false here).

Rows are never dropped, replaced or merged: `omissions.py prepare-batch --ledger FILE` adds the rows of a batch (an existing row keeps its stages), `omissions.py ledger record` attaches the tool_use id of a real call to a row, and
`omissions.py ledger resume` RE-DERIVES every row from the files as they are now (the same preparation as `prepare`) and re-reads every recorded call in the session that holds the Jev calls:
 - an obligation is `valid` only while the identity it was prepared with still holds (the sanitized eligible source of the evaluation, the write id and its sha256, the evaluation mode and run, the material, the ordered references
   with their content hashes, the passage, the detail and the quote); otherwise it is `invalidated` with the reasons, and what was recorded for it is reported stale (never reused, never deleted);
 - a stage (a recorded call) is `bound` only if the call is a real, error-free jev_verify of the session whose single claim is the canonical claim, whose evidence is exactly the canonical material (absence) or the printed passage (source),
   and whose position lies in the window of the version (versions.validate_ref, the same rule as the report); a ledger-declared verdict or status is never read. Two writes with the same hash are two obligations, they never share a call;
 - one recorded call belongs to ONE evaluation (write, version, mode, run): the same call recorded for obligations of different evaluations is stale for all of them, history kept; obligations of the same evaluation share it.
   The retained reference of an INVALIDATED row still counts as a claimant: a call does not migrate to the newer write or evaluation because the earlier row became invalid, unless the chronology of the same session
   demonstrates the owner (another write of the same note has another window);
 - the ledger file obeys the secret policy of stdout: an identity field the policy would alter (a location, the source or note path, an alias, an id) is withheld (hash only, `args.withheld`) and the row is unavailable, unreplayable;
   a stage id (tool_use id) the policy would alter is never recorded, and a ledger that holds one is refused (exit 3, untouched, never echoed);
 - a malformed or incompatible ledger is refused (exit 3), nothing is written over it.
The next step it prints is the scheduling aid of omissions.plan_absence / plan_source, never a status."""
import hashlib, json, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SCHEMA = "handoff-verify-ledger"
VERSION = 1
STAGES = ("absence", "source")
COVERAGE_NOTE = "Progress is not completeness: the audit still requires all nine categories of useful detail and every obligation resolved by the report. A valid obligation or a bound stage confirms nothing; only the report's own validation does."

class LedgerError(Exception): pass

def _sha(x): return hashlib.sha256(x.encode("utf-8")).hexdigest()
def _canon_json(x): return json.dumps(x, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

def empty(): return dict(schema=SCHEMA, version=VERSION, audit=dict(categories_required=9, note=COVERAGE_NOTE), obligations=[])

def _str(x): return isinstance(x, str)
def _opt_str(x): return x is None or isinstance(x, str)
def _strs(x): return isinstance(x, list) and all(isinstance(y, str) for y in x)
_ARGS = dict(source=_opt_str, file=_str, write_id=_str, evaluated_against=lambda x: x in ("prefix", "session_end"), run=_opt_str, cwd=_opt_str, location=_strs, detail=_opt_str, quote=_opt_str)
_IDENTITY = dict(detail_sha256=_str, quote_sha256=_str, write_id=_str, evaluated_against=lambda x: x in ("prefix", "session_end"), run=_opt_str, source=_opt_str, file=_str, available=lambda x: isinstance(x, bool))
_READY = dict(version_sha256=_str, evaluated_sha256=_str, material_sha256=_str, passage_sha256=_str)
_REFERENCE = dict(ref=_str, path=_str, sha256=_str, resolution=_str)

def _shape(where, d, spec, path):
    """Every field of `spec` must be present with its type (a missing field is as malformed as a mistyped one); the diagnostic names the field, never its value."""
    for k, ok in spec.items():
        if k not in d or not ok(d[k]): raise LedgerError("ledger %s is malformed: %s.%s is missing or has the wrong type: refused, nothing is written over it" % (path, where, k))

def validate(doc, path="<ledger>"):
    """Reject a document that is not a complete ledger: LedgerError naming the field. Used by load, and by save before anything is written."""
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA or doc.get("version") != VERSION or not isinstance(doc.get("obligations"), list) or not isinstance(doc.get("audit"), dict): raise LedgerError("ledger %s is not a %s v%d document" % (path, SCHEMA, VERSION))
    seen = set()
    for n, r in enumerate(doc["obligations"]):
        w = "obligation %d" % n
        if not isinstance(r, dict) or not _str(r.get("id")) or r.get("state") not in ("prepared", "unavailable") or r["id"] in seen: raise LedgerError("ledger %s holds a malformed or duplicated obligation (%s): refused" % (path, w))
        if not isinstance(r.get("args"), dict) or not isinstance(r.get("identity"), dict) or not isinstance(r.get("stages"), dict) or not _strs(r.get("reasons")): raise LedgerError("ledger %s is malformed: %s lacks args, identity, stages or reasons of the right type: refused" % (path, w))
        _shape(w + ".args", r["args"], _ARGS, path); _shape(w + ".identity", r["identity"], _IDENTITY, path)
        if "withheld" in r["args"] and (not _strs(r["args"]["withheld"]) or not r["args"]["withheld"] or r["state"] != "unavailable"): raise LedgerError("ledger %s is malformed: %s.args.withheld must list the withheld fields of an unavailable obligation: refused" % (path, w))
        if r["identity"]["available"] != (r["state"] == "prepared"): raise LedgerError("ledger %s is malformed: %s state and identity.available disagree: refused" % (path, w))
        if r["identity"]["available"]:
            _shape(w + ".identity", r["identity"], _READY, path)
            refs = r["identity"].get("references")
            if not isinstance(refs, list) or not all(isinstance(x, dict) for x in refs): raise LedgerError("ledger %s is malformed: %s.identity.references is missing or has the wrong type: refused" % (path, w))
            for x in refs: _shape(w + ".identity.references", x, _REFERENCE, path)
        if r["id"] != obligation_id(r["identity"]): raise LedgerError("ledger %s is malformed: %s id is not the hash of its identity: refused" % (path, w))
        if set(r["stages"]) - set(STAGES): raise LedgerError("ledger %s is malformed: %s holds an unknown stage: refused" % (path, w))
        for st in r["stages"].values():
            if not isinstance(st, list) or not all(isinstance(x, dict) and _str(x.get("tool_use_id")) and x["tool_use_id"] for x in st): raise LedgerError("ledger %s holds a malformed stage (%s): refused" % (path, w))
            if any(_unsafe(x["tool_use_id"], "tool_use_id") for x in st): raise LedgerError("ledger %s holds a stage id that needs redaction (%s): refused, nothing is written over it and the value is not echoed" % (path, w))
        seen.add(r["id"])

def load(path, must_exist=True):
    """-> the ledger document; LedgerError when it is missing (must_exist), unreadable, truncated or incompatible."""
    if not os.path.exists(path):
        if must_exist: raise LedgerError("ledger %s does not exist" % path)
        return empty()
    try: doc = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError) as e: raise LedgerError("ledger %s is unreadable or truncated (%s): refused, nothing is written over it" % (path, e.__class__.__name__))
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA or doc.get("version") != VERSION or not isinstance(doc.get("obligations"), list): raise LedgerError("ledger %s is not a %s v%d document" % (path, SCHEMA, VERSION))
    validate(doc, path)
    return doc

def save(path, doc):
    """Atomic write (temporary file in the same directory, then rename): a reader never sees half a ledger. Refuses to write a document that is not a complete ledger, and fewer obligations than the file on disk holds."""
    validate(doc, path)
    if os.path.exists(path):
        old = load(path)
        if {r["id"] for r in old["obligations"]} - {r["id"] for r in doc["obligations"]}: raise LedgerError("refused: the new ledger would drop obligations of %s" % path)
    d = os.path.dirname(os.path.abspath(path)); fd, tmp = tempfile.mkstemp(prefix=".ledger-", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f: json.dump(doc, f, indent=1, ensure_ascii=False); f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try: os.unlink(tmp)
        except OSError: pass
        raise

def identity_of(a, obj, extra):
    """The identity of one prepared candidate -> dict. `a` = the prepare arguments, `obj` = the object `prepare` printed (ok or not), `extra` = what the preparation reports for the ledger (source, note_real, evaluated_sha256).
    A candidate that is not ready has a smaller identity (what is known without the material); the detail and the quote enter as hashes only (they may be refused because of what they hold)."""
    import omissions as O
    base = dict(detail_sha256=_sha(O.wsnorm(a.detail)), quote_sha256=_sha(a.source_quote or ""), write_id=a.write_id, evaluated_against=a.evaluated_against, run=getattr(a, "run", None),
                source=extra.get("source") or a.source, file=extra.get("note_real") or os.path.realpath(a.file if os.path.isabs(a.file) else os.path.join(a.cwd or os.getcwd(), a.file)))
    if not obj.get("ok"): return dict(base, available=False)
    ref = obj["version_ref"]
    return dict(base, available=True, version_sha256=ref["sha256"], run=ref.get("run"), evaluated_sha256=extra["evaluated_sha256"], material_sha256=_sha(obj["material"]), passage_sha256=_sha(obj["source_passage"]),
                references=[dict(ref=m["ref"], path=m["path"], sha256=m["sha256"], resolution=m["resolution"]) for m in obj["material_manifest"]])

def obligation_id(identity): return _sha(_canon_json(identity))

def args_of(a):
    """The arguments `resume` re-prepares with (only what prepare was given; a detail or quote that a redaction made unusable is not stored in clear)."""
    import omissions as O
    clean = lambda t: t if not O.redaction_dependent(t) else None
    return dict(source=a.source, file=a.file, write_id=a.write_id, evaluated_against=a.evaluated_against, run=getattr(a, "run", None), cwd=a.cwd, location=list(getattr(a, "location", None) or []),
                detail=clean(O.wsnorm(a.detail)), quote=clean(a.source_quote))

def add_rows(doc, rows):
    """Add the rows of a batch (obligations not yet in the ledger); a row already there keeps its stages, an unavailable one refreshes its reasons. Nothing is removed. -> (added, kept)."""
    by = {r["id"]: r for r in doc["obligations"]}; added = kept = 0
    for r in rows:
        if r["id"] in by:
            kept += 1
            if by[r["id"]]["state"] == "unavailable" and r["state"] == "unavailable": by[r["id"]]["reasons"] = r["reasons"]
        else: doc["obligations"].append(r); by[r["id"]] = r; added += 1
    return added, kept

def _unsafe(v, role=None):
    """Would the shared policy alter this identity string (a secret in a path, a location, an id) or does it hold a redaction marker? Such a value is never stored in the ledger. `role` = the id role of a write / run / stage id
    (a recognized credential-free recorded id is kept byte-for-byte); the default is the strict policy."""
    import omissions as O
    return isinstance(v, str) and O.identity_dependent(v, role=role)

_ROLE = dict(write_id="tool_use_id", run="tool_use_id")   # the id roles of an argument / identity field; every other field keeps the strict policy

def _withheld(v): return "withheld:sha256:" + hashlib.sha256(v.encode("utf-8")).hexdigest()   # hash only: not a path, never resolved, never replayed

def _guard(args, ident):
    """The ledger file obeys the policy of stdout: an identity field that the shared policy would alter is WITHHELD (replaced by its hash, listed in `args.withheld`) and the row can only be unavailable (it cannot be replayed).
    -> (args, identity, withheld fields)."""
    args, ident, held = dict(args), dict(ident), []
    for k in ("source", "file", "cwd", "write_id", "run"):
        if _unsafe(args.get(k), _ROLE.get(k)): held.append("args." + k); args[k] = _withheld(args[k])
    args["location"] = [(_withheld(x) if _unsafe(x) else x) for x in args.get("location") or []]
    held += ["args.location[%d]" % i for i, x in enumerate((args.get("location") or [])) if x.startswith("withheld:sha256:")]
    for k in ("source", "file", "write_id", "run"):
        if _unsafe(ident.get(k), _ROLE.get(k)): held.append("identity." + k); ident[k] = _withheld(ident[k])
    if not held and ident.get("available") and any(_unsafe(x.get("path")) or _unsafe(x.get("ref")) or _unsafe(x.get("resolution")) for x in ident.get("references") or []): held.append("identity.references")
    return args, ident, held

UNREPLAYABLE = "unreplayable: an identity field of this candidate holds a secret or a redaction marker; it is withheld (hash only) and never stored, so this obligation cannot be re-prepared from the ledger"

def row_of(a, obj, extra):
    import omissions as O
    ident = identity_of(a, obj, extra); args, ident, held = _guard(args_of(a), ident)
    reasons = [] if obj.get("ok") else [O.S.sanitize(x)[0] for x in (obj.get("reasons") or [])]
    if held:
        ident = {k: ident[k] for k in ("detail_sha256", "quote_sha256", "write_id", "evaluated_against", "run", "source", "file")}; ident["available"] = False
        args["withheld"] = held; reasons = [UNREPLAYABLE] + reasons
    return dict(id=obligation_id(ident), state="prepared" if ident["available"] else "unavailable", args=args, identity=ident, reasons=reasons, stages={})

def record(doc, oid, stage, tool_use_id):
    """Attach the tool_use id of a real call to the row `oid` (nothing about its result is stored: resume re-reads the call)."""
    if stage not in STAGES: raise LedgerError("stage must be one of %s" % list(STAGES))
    if not isinstance(tool_use_id, str) or not tool_use_id.strip(): raise LedgerError("tool_use id is empty")
    if _unsafe(tool_use_id, "tool_use_id"): raise LedgerError("the tool_use id holds a secret or a redaction marker: an identity that needs redaction is never stored (refused; the value is not echoed)")
    r = next((x for x in doc["obligations"] if x["id"] == oid), None)
    if r is None: raise LedgerError("no obligation with this id in the ledger")
    if r["state"] != "prepared": raise LedgerError("this obligation is unavailable: no call can be recorded for it")
    st = r["stages"].setdefault(stage, [])
    if not any(x["tool_use_id"] == tool_use_id for x in st): st.append(dict(tool_use_id=tool_use_id))
    return r

def _diff(old, new):
    why = []
    for k, label in (("evaluated_sha256", "the evaluated source (sanitized eligible records of the window) changed"), ("version_sha256", "the version of the note changed"), ("write_id", "the write changed"),
                     ("evaluated_against", "the evaluation mode changed"), ("run", "the evaluated verification run changed"), ("material_sha256", "the complete material changed"), ("passage_sha256", "the source passage changed"),
                     ("references", "a reference changed, moved, disappeared or resolves elsewhere"), ("source", "the source session changed"), ("file", "the note path changed")):
        if old.get(k) != new.get(k): why.append(label)
    return why or ["the identity of the obligation changed"]

def _result(call, claim):
    """The REAL result of a single-claim jev_verify call, read with the rules of the report binding (jevref.results_of): -> (observed dict | None, reason | None). The result must sit at index 0 and carry exactly the
    claim of the input (no mismatch, no other result), with a verdict and a probability: anything else is not a result of THIS claim and is never counted. `observed` also holds the strict auxiliary verdict of the report."""
    import jevref as J
    ents, why = J.results_of(call)
    if why or not ents: return None, "the response has no identifiable result for the claim (%s)" % (why or "no results")
    if len(ents) != 1 or ents[0]["index"] != 0: return None, "the response holds %d results for a single claim: only result 0 is the claim's" % len(ents)
    e = ents[0]
    if e.get("mismatch"): return None, "the result is not the result of the canonical claim (%s)" % e["mismatch"]
    if e["key"] != claim: return None, "the result is not the result of the canonical claim"
    if not isinstance(e["verdict"], str) or not e["verdict"].strip(): return None, "the result carries no verdict"
    if e["confidence"] is None: return None, "the result carries no usable confidence (a probability in [0, 1])"
    return dict(verdict=e["verdict"], confidence=e["confidence"], action=e.get("action"), same_subject=e.get("same_subject"), aux_ok=J.aux_ok(e, call, True), strict_confidence=J.strict_pass(e["confidence"])), None

def check_stage(stage, tid, obj, calls_by_id, vs, same, source, sess=None):
    """Re-read one recorded call -> dict(stage, tool_use_id, state bound|pending|stale|missing, reason, observed). Nothing the ledger holds is believed. `vs` = the versions on the timeline of the transcript that HOLDS the call
    (`sess`; the source itself when it is the same file), `same` = the session of the calls is demonstrated to be the evaluated one (versions.same_session); the evaluation is judged in every representation (versions.run_problem)."""
    import omissions as O, versions as V
    out = dict(stage=stage, tool_use_id=tid, state="stale", reason=None, observed=None)
    call = calls_by_id.get(tid)
    if call is None: out.update(state="missing", reason="no Jev call with this tool_use id in the session"); return out
    claim, ev = (obj["absence_claim"], obj["material"]) if stage == "absence" else (obj["source_claim"], obj["source_passage"])
    if call["tool"] != "verify": out["reason"] = "the call is not jev_verify"
    elif not call["has_result"]: out.update(state="pending", reason="the call has no result yet")
    elif call["is_error"]: out["reason"] = "the call has no usable result"
    elif (call["input"].get("claims") if isinstance(call["input"].get("claims"), list) else None) != [claim]: out["reason"] = "the call's claims are not exactly [the canonical %s claim]" % stage
    elif "".join(O.evidence_raw(call["input"])) != ev: out["reason"] = "the evidence of the call is not exactly the canonical %s" % ("material" if stage == "absence" else "source passage")
    else:
        observed, why = _result(call, claim)
        if observed is None: out["reason"] = why; return out
        kind, why, _ = V.validate_ref(obj["version_ref"], vs, call, same)
        if kind == "ok": kind, why = V.run_problem(sess or source, obj["version_ref"], also=[source] if same and sess else ())
        if kind != "ok": out["reason"] = "the call is outside the binding window or the evaluation is not demonstrated: %s" % why
        else: out.update(state="bound", reason="bound", observed=observed)
    return out

def _evaluation(identity):
    """What a call is evidence OF: the evaluated source, the write, the version, the mode and the run (the detail and the quote only choose the claim and the passage). Two obligations that agree on all of it share a call."""
    return tuple(identity.get(k) for k in ("source", "file", "write_id", "version_sha256", "evaluated_sha256", "evaluated_against", "run"))

def _next(stages):
    """The scheduling aid (never a status): what the audit of a valid obligation needs next, read from the observed results of the bound stages (the strict rules of omissions.plan_absence and of the report: a SOURCE
    result counts only when it is `verified` with confidence > 0.95 and the strict auxiliary conditions hold)."""
    import jevref as J, omissions as O
    bound = lambda k: [s for s in stages if s["stage"] == k and s["state"] == "bound" and s.get("observed")]
    ab = bound("absence")
    if not ab: return "call_absence"
    plans = [O.plan_absence(dict(bound=True, version_ok=True, material_complete=True, verdict=o["verdict"], confidence=o["confidence"], action=o["action"], aux_ok=o["aux_ok"])) for o in (s["observed"] for s in ab)]
    if any(p["disposition"] == "present_resolved" for p in plans): return "no_source_call"
    if not any(p["disposition"] == "request_source" for p in plans): return "unresolved"
    src = bound("source")
    if not src: return "call_source"
    strict = lambda o: str(o["verdict"]).lower() == "verified" and o["strict_confidence"] and o["aux_ok"] is True
    return "ready_for_the_report" if any(strict(s["observed"]) for s in src) else "unresolved"

def resume(doc, session=None, cwd=None):
    """Re-derive every obligation and re-read every recorded call. -> dict(ok, obligations [dict(id, state valid|invalidated|unavailable, reasons, stages, next)], coverage). Never changes the ledger. One resume reads each
    transcript (and each stored OpenCode session) as ONE snapshot: every row is judged against the same bytes."""
    import discover as D
    with D.fingerprint_scope(): return _resume(doc, session, cwd)

def _resume(doc, session=None, cwd=None):
    import argparse, discover as D, jevref as J, omissions as O, versions as V
    out, calls_cache, owners, readable = [], {}, {}, set()
    for r in doc["obligations"]:
        a = r["args"]; row = dict(id=r["id"], state=None, reasons=[], stages=[], next=None)
        if a.get("withheld"):
            row.update(state="unavailable", reasons=[UNREPLAYABLE]); out.append(row); continue
        if a.get("detail") is None or a.get("quote") is None:
            row.update(state="unavailable", reasons=["the detail or the quote holds a secret or a redaction marker: it was never stored in clear and cannot be re-derived"]); out.append(row); continue
        ns = argparse.Namespace(source=a["source"], file=a["file"], write_id=a["write_id"], evaluated_against=a["evaluated_against"], run=a.get("run"), cwd=a.get("cwd") or cwd, location=a.get("location") or [], detail=a["detail"], source_quote=a["quote"])
        extra = {}; obj, code = O.prepare_one(ns, extra)
        if r["state"] == "unavailable":
            if code == 0: row.update(state="invalidated", reasons=["it was unavailable and can be prepared now: prepare it again (a new obligation); this row is kept as history"])
            else: row.update(state="unavailable", reasons=list(obj.get("reasons") or []))
            out.append(row); continue
        if code != 0:
            row.update(state="invalidated", reasons=["no longer preparable: " + "; ".join(obj.get("reasons") or ["unknown"])])
        else:
            ident = identity_of(ns, obj, extra)
            if obligation_id(ident) != r["id"]: row.update(state="invalidated", reasons=_diff(r["identity"], ident))
            else: row["state"] = "valid"
        sp = extra.get("source") or D.resolve_session_info(a["source"], ns.cwd)[0]
        sess, _, amb = D.resolve_session_info(session, ns.cwd) if session else (sp, None, False)
        if row["state"] == "valid" and (not sess or amb or not D.source_exists(sess)): row["reasons"].append("the session with the Jev calls is not available: every recorded call is unverified")
        calls = None
        if row["state"] == "valid" and sess and not amb and D.source_exists(sess):
            ck = D.canon(sess)
            if ck not in calls_cache:
                try: calls_cache[ck] = {c["tool_use_id"]: c for c in J.load_calls(sess)}
                except OSError: calls_cache[ck] = None
            calls = calls_cache[ck]
        vs = same = None
        if calls is not None:
            prov, vs, canon, note, reloc, rnote = O._session_prep(extra["source"], os.path.join(ns.cwd or os.getcwd(), a["file"]) if not os.path.isabs(a["file"]) else a["file"]); same = V.same_session(extra["source"], sess)
            if same and D.canon(sess) != D.canon(extra["source"]):   # positions are judged on the timeline of the transcript that holds the calls, never mixed with those of another representation
                vs, c2, _, n2 = V.versions_of(sess, extra["note_real"], also=[extra["source"]])
                if c2 is None: vs = []
        for stage in STAGES:
            for st in r["stages"].get(stage, []):
                if row["state"] == "valid" and calls is not None:
                    row["stages"].append(check_stage(stage, st["tool_use_id"], obj, calls, vs, same, extra["source"], sess))
                    if row["stages"][-1]["state"] == "bound": owners.setdefault((D.canon(sess), stage, st["tool_use_id"]), []).append(dict(ev=_evaluation(r["identity"]), st=row["stages"][-1], same=bool(same)))
                else:
                    row["stages"].append(dict(stage=stage, tool_use_id=st["tool_use_id"], state="stale", reason="the obligation is %s: what was recorded for it is not reused" % row["state"] if row["state"] != "valid" else "the calls could not be re-read", observed=None))
                    if row["state"] == "invalidated" and sess and not amb and D.source_exists(sess):   # the retained reference of an invalidated evaluation still names that evaluation as a claimant of the call
                        owners.setdefault((D.canon(sess), stage, st["tool_use_id"]), []).append(dict(ev=_evaluation(r["identity"]), st=None, same=None))
        if calls is not None: readable.add(row["id"])
        out.append(row)
    for owner in owners.values():   # one recorded call (and its result) belongs to ONE evaluation
        n = len({o["ev"] for o in owner})
        if n < 2: continue
        for o in owner:
            if o["st"] is None: continue
            # an invalidated claimant is excluded only when the chronology of the same session demonstrates the owner: another WRITE of the same note has a different window, so the call lies in ONE of them
            rivals = [x for x in owner if x["ev"] != o["ev"] and not (x["st"] is None and o["same"] and x["ev"][:2] == o["ev"][:2] and x["ev"][2] != o["ev"][2])]
            if rivals: o["st"].update(state="stale", reason="the same call is recorded for obligations of different writes, modes or runs (%d evaluations%s): which one its result belongs to is not demonstrated, so it is not usable for any of them" % (n, ", some of them no longer valid" if any(x["st"] is None for x in rivals) else ""), observed=None)
    for row in out:
        if row["state"] == "valid": row["next"] = _next(row["stages"]) if row["id"] in readable else "call_absence"
    n = lambda s: sum(1 for x in out if x["state"] == s)
    return dict(ok=True, obligations=out, coverage=dict(categories_required=9, obligations=len(out), valid=n("valid"), invalidated=n("invalidated"), unavailable=n("unavailable"), complete=False, note=COVERAGE_NOTE))
