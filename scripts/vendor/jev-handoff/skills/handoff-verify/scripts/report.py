"""Report core: strict Jev threshold, status precedence, retry policy, report writing, patch approval/backup.
Stdlib only. Never calls Jev (Claude Code calls MCP directly)."""
import datetime, hashlib, json, math, os, re, shutil, stat, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import advice, audit, jevref, schema_check, scope, versions
import schema_check as SC

THRESHOLD = 0.95
SCHEMA_VERSION = "1"

def passes(confidence):
    """A probability (finite number in [0, 1], jevref.is_probability) strictly > 0.95, no rounding. None/NaN/infinity/out-of-range/bool/str never pass."""
    return jevref.is_probability(confidence) and confidence > THRESHOLD

def check_status(check):
    """check: {kind: 'defect'|'obligation', verdict_ok: bool, confidence: float|None, error: str|None, aux_ok: bool=True}
    returns 'confirmed' | 'cleared' | 'unresolved'.
    A defect is confirmed only when the defect verdict holds with confidence > threshold and auxiliary conditions are satisfied.
    An obligation is cleared only when satisfied with confidence > threshold."""
    if check.get("error") or not passes(check.get("confidence")) or not check.get("aux_ok", True) or check.get("bound") is False:
        return "unresolved"   # bound=False: no demonstrable Jev call/result behind the check (R01: PASS is impossible with an unbound check)
    if check["kind"] == "defect":
        return "confirmed" if check["verdict_ok"] else "cleared"
    return "cleared" if check["verdict_ok"] else "confirmed"

def overall_status(checks):
    """FAIL if any confirmed defect (unresolved kept & counted); else UNRESOLVED if any unresolved; else PASS (also requires >=1 check)."""
    st = [check_status(c) for c in checks]
    unresolved = st.count("unresolved")
    confirmed = st.count("confirmed")
    if confirmed:
        status = "FAIL"
    elif unresolved or not checks:
        status = "UNRESOLVED"
    else:
        status = "PASS"
    return {"status": status, "confirmed": confirmed, "unresolved": unresolved, "cleared": st.count("cleared"), "total": len(st)}

TRANSPORT_ERRORS = {"transport", "invalid_response"}

def _version_ref(binding):
    """The version of the note the writer demonstrated for a binding, as a version_ref ({write_tool_use_id, sha256, evaluated_against[, run]}); None when no identity was demonstrated."""
    v = binding.get("version")
    return audit.dict_of(audit.key_of(v)) if isinstance(v, dict) and v.get("ok") else None

def attach_advice(checks, bindings, calls, findings, ctx=None):
    """Informative only (never read back, never changes a status): every bound verify/gate check that is not resolved gets `advice` = advice.advise of its REAL result
    (what to try in the single re-verification that retry_decision allows). The two checks of an omission pair get none (their `unsupported` half is expected). A declared `advice` is dropped.
    The two halves of an omission pair that no finding links (R04) get the stage-aware advice of advice.omission_advise instead of the generic one.
    With the audit context `ctx` (the writer), the advice also carries `evidence_request` (advice.evidence_request), bound to the version the writer demonstrated for the check and to the authorizations of the registry
    of the session; a history that cannot be read is a lost history, which refuses a new allowance."""
    pair = {f.get("check_id") for f in findings if isinstance(f, dict) and f.get("type") == "lost_detail" and isinstance(f.get("check_id"), str)}
    pair |= {f["omission_ref"].get("source_check_id") for f in findings if isinstance(f, dict) and isinstance(f.get("omission_ref"), dict) and isinstance(f["omission_ref"].get("source_check_id"), str)}
    by_id = {c["tool_use_id"]: c for c in calls}; history = False      # the registry is read once, and only when a check needs a request
    for c, b in zip(checks, bindings):
        if not isinstance(c, dict): continue
        c.pop("advice", None)
        if not b.get("bound") or b.get("resolved") or b.get("tool") not in ("verify", "gate") or jevref.check_id(c) in pair: continue
        call = by_id.get(b["tool_use_id"]); e = next((x for x in jevref.results_of(call)[0] if x["index"] == b["result_index"]), None) if call else None
        ok = bool(b.get("omission_material")) and audit.O.material_matches(b.get("call_evidence_raw") or [], b["omission_material"])      # the call's evidence IS the complete canonical material of the version
        v = b.get("version"); reg = False                                   # the bare claim is a detail registered as a candidate of this very evaluation (what the ABSENCE call of an omission asks)
        if ctx is not None and isinstance(v, dict) and v.get("ok") and isinstance(b.get("claim_key"), str):
            k, dsha = audit.key_of(v), audit.detail_sha(b["claim_key"]); reg = any(x["detail_sha256"] == dsha and audit.applicable(x, k) for x in ctx.candidates())
        stage = advice.stage_of(b.get("claim_key"), b.get("tool"), ok, reg)  # R04: the two halves of an omission pair have their own generator (the generic advice is wrong for them)
        a = (advice.omission_advise(stage, e, call, ok) if stage else advice.advise(e, call)) if e else None
        if a:
            if ctx is not None and a["code"] != "absence_awaits_source":     # a first SOURCE call is not a re-verification: no request
                if history is False: hist, why = ctx.evidence_history(); history = None if why else hist
                a["evidence_request"] = advice.evidence_request(e, call, a, history, _version_ref(b), stage)
            c["advice"] = a

def retry_decision(error_kind, attempts_identical, low_confidence=False, new_evidence=False, reverifications=0):
    """One identical retry only for transport/invalid_response; low confidence: at most one re-verification with NEW documented evidence;
    never an identical re-call for a nicer score. Returns 'retry_identical' | 'reverify_new_evidence' | 'stop_unresolved'."""
    if error_kind in TRANSPORT_ERRORS:
        return "retry_identical" if attempts_identical < 1 else "stop_unresolved"
    if low_confidence:
        return "reverify_new_evidence" if (new_evidence and reverifications < 1) else "stop_unresolved"
    return "stop_unresolved"

def input_hash(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

def confidence_or_null(v):
    return v if jevref.is_probability(v) else None   # a malformed confidence is null, never clamped or rounded

def cost_field(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else "unavailable"

def short_hash(path, extra=0):
    return hashlib.sha256(os.path.realpath(path).encode()).hexdigest()[:8 + extra]

def report_names(handoff_path, existing=()):
    base = os.path.basename(handoff_path)
    extra = 0
    while True:
        stem = "%s-%s" % (base, short_hash(handoff_path, extra))
        if not any(e.startswith(stem) and os.path.realpath(e) != os.path.realpath(handoff_path) for e in existing) or extra > 8:
            return stem + ".verify.md", stem + ".verify.json"
        extra += 1

def new_run_dir(session_cwd, session_id, now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    run_id = now.strftime("%Y%m%dT%H%M%S%fZ")
    d = os.path.join(session_cwd, ".handoff-verify", session_id, run_id)
    os.makedirs(d, exist_ok=False)  # unique run id: never overwrite
    return d, run_id

STATUS_LINE = re.compile(r"^\W*(Stare|Stat|Status|Estado)\b", re.I)
# The MAIN status token of a status line: the one that directly follows the label (Stare|Stat|Status|Estado), allowing markdown emphasis, a colon or a dash between them (`Stare: **PASS**`, `**Stare:** PASS`, `- Status — FAIL`).
# Anything after it (a warning, a condition, an example, another status word) is never touched; a line without such a token is left unchanged.
STATUS_TOKEN = re.compile(r"^(\W*(?:Stare|Stat|Status|Estado)\b[\s*_`:=\-\u2013\u2014]*)(PASS|FAIL|UNRESOLVED)(?![^\W_])", re.I)
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.*)$")
VERSION_HEADING = re.compile(r"\b(versiun\w*|versi[oó]n\w*|version\w*|v\d+)\b", re.I)   # a heading of a per-version section ("Versiunea 2", "Version 2", "v3"): the statuses under it are those of that version, not the report's
EXAMPLE_HEADING = re.compile(r"\b(exempl\w*|example\w*|ejempl\w*)\b", re.I)     # a heading of an example section ("Exemplu", "Exemple de stare", "Example", "Ejemplo"): a status under it is an illustration, not the report's
# What may precede the label on the MAIN status line (the rule, documented in reference/report.md, "Output"): at most three spaces of indentation, an optional heading marker (`## `), an optional list marker (`- `, `* `, `+ `) and markdown emphasis (`*`, `_`, `~`).
# Anything else before the label makes the line a quotation or a literal: a quotation mark (`"Stare: PASS"`, `'Stare: PASS'`, curly or guillemet quotes), a blockquote `>`, an opening backtick (inline code), a table cell `|`, an HTML comment `<!--`,
# and four or more spaces / a tab of indentation (an indented code block).
MAIN_LEAD = re.compile(r"^ {0,3}(?![ \t])(?:#{1,6}[ \t]+)?(?:[-*+][ \t]+)?[*_~ \t]*$")

def session_cwd_of(run_dir):
    """The cwd of the CURRENT Claude session = the part before /.handoff-verify/ in the run dir (else the process cwd). NOT the cwd of the verified transcript."""
    marker = os.sep + ".handoff-verify" + os.sep
    return run_dir.split(marker)[0] if marker in run_dir else os.getcwd()

def _dict(x): return x if isinstance(x, dict) else {}
def _list(x): return x if isinstance(x, list) else []

def bind_report(doc, calls_jsonl=None, run_dir=None, extra_paths=(), require_version_identity=False, handoff_path=None, session_id=None, omission_contract="R04"):
    """Bind every check of `doc` against the Jev calls logged in the CURRENT session's JSONL (never the verified fixture's transcript) and recompute the status.
    Returns the new doc (status_claimed kept when the status changes). Without a readable calls log every check stays unbound -> UNRESOLVED.
    R02 (`require_version_identity`, True in write_report): every bound check must carry a version_ref whose identity is verified against the source transcript named by `session.jsonl`
    (shared/versions.py); a bound check without a valid version identity is unresolved, findings validate quote_handoff against their check's own version, status/binding_summary are also
    computed per version, and for a handoff written in the current session a `delivery` block records the gate state (the gate itself is `versions.py status`)."""
    doc = dict(doc) if isinstance(doc, dict) else {}
    # The structure comes first: nothing of a model-written report is touched before it is known to be usable. A list that is not a list is empty; a record that does not match its item schema (a check whose id is a list, a finding whose type is an object,
    # a version whose tool_use_id is a list, ...) is NOT USABLE: an empty record (an unbound check, a finding that confirms nothing) is evaluated in its place, so nothing is hashed, indexed or compared on a value of the wrong kind. The problem itself is
    # never dropped: the audit (audit.assess, with the same schema_check validator as the gate) counts every structural problem of the INPUT and a PASS cannot survive it, while a validated defect among the usable records keeps FAIL.
    claimed = doc.get("status")   # the author's claim; it becomes `status_claimed` only when it is a status
    for k in SC.WRITER_OWNED:     # the writer's properties are the writer's: nothing the author put there is carried into the output (it would be an unvalidated value in a schema-constrained field)
        if k != "status": doc.pop(k, None)
    raw_checks, raw_findings, raw_unresolved = _list(doc.get("checks")), _list(doc.get("findings")), _list(doc.get("unresolved"))
    ok_checks = SC.usable(raw_checks, SC.item_schema("checks"), SC.RECORD_WRITER_OWNED["checks"])
    checks = [dict(c) if c is not None else {} for c in ok_checks]; out_checks = [dict(c) if isinstance(c, dict) else {} for c in raw_checks]   # evaluated records / the records the report keeps
    findings = [f if f is not None else {} for f in SC.usable(raw_findings, SC.item_schema("findings"))]; unresolved = [u if u is not None else {} for u in SC.usable(raw_unresolved, SC.item_schema("unresolved"))]
    session_o, handoff_o = _dict(doc.get("session")), _dict(doc.get("handoff")); wloc = doc.get("work_locations") if isinstance(doc.get("work_locations"), list) and all(isinstance(x, str) for x in doc["work_locations"]) else None
    calls, note, cpath = [], None, None
    try:
        path = calls_jsonl
        if not path:
            import discover as D
            path, how, amb = D.resolve_session_info(None, session_cwd_of(run_dir or os.getcwd()))
            if amb: path, note = None, "current session not demonstrated (ambiguous)"
            elif not path: note = "current session log not found"
        if path: calls = jevref.load_calls(path); cpath = path
    except Exception as e:
        note = "calls log unreadable: %s" % e
    ctr = jevref.contract_of(omission_contract); bs = jevref.bind(checks, calls, ctr); vnote = None
    htxt = jevref.resolve_handoff_text(handoff_o, extra_paths)[0]
    if require_version_identity:
        h = handoff_o
        hp = h.get("path") if isinstance(h.get("path"), str) else handoff_path
        src = session_o.get("jsonl"); same = bool(cpath) and versions.same_session(src, cpath, session_id)
        rows = versions.bind_versions(checks, bs, hp or "", (cpath if same else src) if isinstance(src, str) else None, calls, same, h.get("source_path") if isinstance(h.get("source_path"), str) else None, wloc, [src] if isinstance(src, str) else ())   # the report's transcript is another representation of the same session when `same`: what its subagent streams demonstrate holds too
        bs = versions.attach(bs, rows)
        bs = jevref.finalize(bs, findings, htxt, ctr)   # R04: the absence half of a valid pair is resolved once the identity and the material are attached
        vnote = dict(same_session=same, identity_ok=sum(1 for r in rows.values() if r["ok"]), identity_failed=sum(1 for r in rows.values() if not r["ok"]), reasons=sorted({r["reason"] for r in rows.values() if not r["ok"]}), unpositioned=sum(1 for r in rows.values() if r["kind"] == "unpositioned"),
                     unordered=sum(1 for r in rows.values() if r["kind"] == "unordered"))
        sfile = (cpath if same else src) if isinstance(src, str) else None   # provenance is re-derived here, whether or not the report has checks: a note without a recorded supported write can never be certified
        try:
            import discover as D
            prov = versions.provenance(sfile, hp, h.get("source_path") if isinstance(h.get("source_path"), str) else None, relocate=False) if sfile and hp and D.source_exists(sfile) else None
        except Exception as e: prov = None
        if prov: vnote["provenance"] = prov
    for c, o, b in zip(checks, out_checks, bs): o["binding"] = dict({k: b[k] for k in ("bound", "reason", "resolved", "aux_ok")}, **({"contract_reason": b["contract_reason"]} if b.get("contract_reason") else {}))   # contract_reason: the real verdict is outside its tool's contract (bound, recorded, never resolved)
    src = session_o.get("jsonl")
    h = handoff_o; hp = h.get("path") if isinstance(h.get("path"), str) else handoff_path
    asrc = ((cpath if vnote["same_session"] else src) if vnote is not None else src) if isinstance(src, str) else None   # the transcript the audit derives its expected chunks from (the one the version identity is judged in)
    also = [src] if vnote is not None and vnote["same_session"] and isinstance(src, str) and src else []   # the report's transcript is another representation of the SAME session (demonstrated): its candidate registry and its ledgers are part of the accounting too
    actx = audit.Context(doc, checks, bs, calls, asrc, hp, h.get("source_path") if isinstance(h.get("source_path"), str) else None, vnote is not None, bool(vnote and vnote["same_session"]), ctr, True, also)
    attach_advice(checks, bs, calls, findings, actx)
    requests = []
    for c, o in zip(checks, out_checks):
        o.pop("advice", None)
        if "advice" in c: o["advice"] = c["advice"]
        r = (c.get("advice") or {}).get("evidence_request")
        if r and jevref.check_id(c): requests.append(dict(check_id=jevref.check_id(c), request_id=r["request_id"], state=r["state"], remedy=r["remedy"], stop_reason=r["stop_reason"]))
    ev = jevref.audited_status(checks, bs, findings, unresolved, htxt, ctr)
    if vnote is not None and vnote.get("provenance") and vnote["provenance"]["state"] != "recorded_write":
        ev["reasons"].append(vnote["provenance"]["blocker"])   # exposed even without checks; a PASS cannot survive it
        if ev["status"] == "PASS": ev["status"] = "UNRESOLVED"
    if "scope_exclusions" in doc:   # R05: every declared out-of-scope exclusion is re-derived from the real jev_classify call (audit.Context.scope_state = scope.validate_exclusions with the real source, the canonical scope, the known writes and the evaluation of the report); the audit below turns an invalid one into an incomplete audit
        sa = actx.scope_state()
        doc["scope_audit"] = dict(threshold=scope.THRESHOLD, valid=sa["valid"], invalid=sa["invalid"], reasons=sa["reasons"])
    ev = audit.certify(ev, actx, None, findings)   # MANDATORY full-report audit: a PASS needs a complete audit (structure, scope accounting, chunk review, nine categories, every registered candidate); FAIL keeps its precedence
    valid_claim = claimed in STATUSES
    if "status" in doc and not valid_claim: ev["reasons"].append("the declared status is not PASS, FAIL or UNRESOLVED: it is ignored and not kept (the status is the recomputed one)")
    doc["status_claimed"] = claimed if valid_claim else None
    if valid_claim and claimed == ev["status"]: doc.pop("status_claimed")
    if ctr != "R02": doc["omission_contract"] = ctr   # informative only (never read): the contract the writer applied (strict auxiliary conditions and the explicit omission pair; the gate and the auditors recompute under their own contract)
    doc.update(checks=out_checks, evidence_requests=requests, blockers=audit.blockers(ev["audit"].get("unfinished")), status=ev["status"], jev_ref_version="1", binding_summary=dict(checks=ev["checks"], bound=ev["bound"], resolved=ev["resolved"], unbound=ev["unbound"], reasons=ev["reasons"], calls_log_note=note, jev_calls_in_log=len(calls), audit=ev["audit"]))
    if vnote is not None:
        doc["binding_summary"]["version_identity"] = vnote
        h = dict(handoff_o); declared = _list(h.get("versions"))
        ok_versions = SC.usable(declared, SC.item_schema("handoff", "versions"), SC.RECORD_WRITER_OWNED["versions"])
        pv = versions.per_version(checks, bs, findings, unresolved, [v for v in ok_versions if v is not None], omission_contract, actx)
        wids = versions.write_ids((cpath if same else src) if isinstance(src, str) else None, hp or "", h.get("source_path") if isinstance(h.get("source_path"), str) else None)
        def attach_pv(v, usable):
            if not usable: return v      # a version record that does not match its schema is kept as it is (a structural problem the audit counts), never looked up
            e, how = versions.lookup(pv, v, wids)
            if e is not None: return dict(v, audited_status=e["audited_status"], binding_summary=e["binding_summary"], evaluations=e["evaluations"], attributed_write=e["write_tool_use_id"], attribution=how)
            return dict(v, audited_status="UNRESOLVED", binding_summary=dict(checks=0, bound=0, resolved=0, unbound=0, reasons=[how], valid_findings=0), evaluations={}, attribution=how)
        if "versions" in h: h["versions"] = [attach_pv(v, u is not None) for v, u in zip(declared, ok_versions)] if isinstance(h["versions"], list) else h["versions"]
        if isinstance(doc.get("handoff"), dict): doc["handoff"] = h
        if vnote["same_session"] and cpath:
            g = versions.gate(cpath, h.get("path") or handoff_path or "", dict(doc), disk_path=handoff_path or h.get("path"), session_id=session_id, omission_contract=ctr)
            doc["delivery"] = {k: g[k] for k in ("delivery_state", "current_sha256", "latest_write_id", "bound_checks_for_version", "reasons")}
            if g.get("stale"): doc["delivery"]["stale"] = g["stale"]   # the report is about a version that is not the latest write of the path (the exact notice is derived from it by render_md / versions.py status)
    return seal(doc)

STATUSES = ("PASS", "FAIL", "UNRESOLVED")

def seal(doc):
    """The last check of the writer: the report it emits is validated against the schema as a whole. A PASS that is not structurally valid as emitted (a value the writer itself put in the wrong place) is never certified: it becomes UNRESOLVED
    with the problems counted in the audit and a delivery that is not verified. (It cannot be reached by an input problem: those are counted by the audit already; it keeps a writer defect from certifying malformed output.)"""
    problems = SC.validate_report(doc)
    if not problems or doc.get("status") != "PASS": return doc
    au = doc["binding_summary"]["audit"]; au["complete"] = False; au["unfinished"] = dict(au.get("unfinished", {}), structure=au.get("unfinished", {}).get("structure", 0) + len(problems)); doc["blockers"] = audit.blockers(au["unfinished"])
    why = "the report as emitted does not match report.schema.json: " + SC.summarize(problems); au["reasons"] = ([why] + au["reasons"])[:5]; au["reasons_total"] += 1
    doc["binding_summary"]["reasons"] = doc["binding_summary"]["reasons"] + ["audit incomplete: " + why]
    if doc.get("status_claimed", "absent") == "absent": doc["status_claimed"] = "PASS"
    doc["status"] = "UNRESOLVED"
    if isinstance(doc.get("delivery"), dict): doc["delivery"] = dict(doc["delivery"], delivery_state="unresolved", reasons=list(doc["delivery"].get("reasons", [])) + [why])
    return doc

def rewrite_status_lines(md_text, final):
    """The Markdown with the MAIN status of the report rewritten to `final` (see STATUS_TOKEN): the first status line of the report that is the report's own, and only that one. THE RULE: a status line is the report's own only when
    (1) the label is at the start of the line, preceded by nothing but up to three spaces, a heading marker, a list marker and markdown emphasis (MAIN_LEAD) and directly followed by the status token (STATUS_TOKEN); (2) it is outside a fenced
    code block; and (3) it is not under the heading of a per-version section ("Versiunea 2") nor of an example section ("Exemplu", "Example"), up to the next heading of the same or a higher level. So these are never touched: a line inside a fence,
    a blockquote (`> Stare: ...`), a quotation (`"Stare: PASS"`, `'Stare: PASS'`), inline code (`Stare: PASS` quoted), an indented code line (four or more spaces, or a tab), a table cell, an HTML comment, a status under a per-version or an example
    heading, every later status line of the document, and every line without the token right after the label. The line endings of the text are kept."""
    out, fence, done, skip = [], None, False, []   # skip = the levels of the headings of the per-version / example sections we are in (nested sections are kept apart)
    for l in md_text.splitlines():
        m = FENCE.match(l)
        if fence:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) and not l.strip().strip(fence[0]): fence = None
        elif m: fence = m.group(1)
        else:
            hd = HEADING.match(l); special = False
            if hd:
                lvl = len(hd.group(1)); special = bool(VERSION_HEADING.search(hd.group(2)) or EXAMPLE_HEADING.search(hd.group(2)))
                while skip and skip[-1] >= lvl: skip.pop()      # a heading of the same or a higher level ends the sections it closes
                if special: skip.append(lvl)
            t = STATUS_TOKEN.match(l)
            if not done and not skip and t and t.group(2) in STATUSES and MAIN_LEAD.match(re.match(r"\W*", l).group(0)) and not special:
                l = t.group(1) + final + l[t.end():]; done = True
        out.append(l)
    return "\n".join(out) + ("\n" if md_text.endswith("\n") else "")

def render_md(md_text, doc):
    """Keep the Markdown consistent with the final JSON status: the main status token of each status line is rewritten (and nothing else of the line), and a notice is prepended when the model's status was overridden."""
    final = doc["status"]
    out = rewrite_status_lines(md_text, final)
    if "status_claimed" in doc:
        out = "> Stare finală (recalculată de report.py din legăturile verificare→apel Jev): **%s**; declarată inițial: %s. Motiv: %s.\n\n" % (final, doc["status_claimed"] or "lipsă sau nevalidă", "; ".join(doc["binding_summary"]["reasons"]) or "n/a") + out
    stale = (doc.get("delivery") or {}).get("stale")
    if stale: out = versions.stale_notice(stale) + "\n\n" + out
    return out

def write_report(run_dir, handoff_path, doc, md_text, existing=(), calls_jsonl=None, require_version_identity=True, omission_contract="R04"):
    """Writes <name>.verify.md/.json. Checks are bound to the real Jev calls of the current session (see jevref.py) and the status is recomputed: PASS is impossible
    with an unbound mandatory check. `calls_jsonl` = explicit path of the current session log (else resolved with discover.py; ambiguity => unbound)."""
    md, js = report_names(handoff_path, existing)
    doc = bind_report(dict(doc if isinstance(doc, dict) else {}, schema_version=SCHEMA_VERSION), calls_jsonl, run_dir, [handoff_path], require_version_identity, handoff_path, None, omission_contract)
    json.dump(doc, open(os.path.join(run_dir, js), "w"), indent=1, ensure_ascii=False)
    open(os.path.join(run_dir, md), "w").write(render_md(md_text, doc))
    return md, js

def sha256_file(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()

def _exclusive_backup(path, data, src):
    """Create `<path>.bak-<UTC>` (then `-N`) EXCLUSIVELY (O_EXCL: an existing backup is never overwritten, and there is no exists-then-copy race) holding exactly `data` (the bytes whose hash was verified), then copy the metadata
    of `src` as shutil.copy2 does (mode, times, flags). -> the backup path."""
    n = 0; base = path
    while True:
        try: fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            n += 1; path = "%s-%d" % (base, n); continue
        break
    try:
        with os.fdopen(fd, "wb") as f: f.write(data)
        shutil.copystat(src, path)
    except BaseException:
        try: os.unlink(path)    # only this operation's own, just created backup
        except OSError: pass
        raise
    return path

def _write_all(fd, data):
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        if n <= 0: raise OSError("short write")
        view = view[n:]

def backup_only(path, expected_sha256, approved, now=None):
    """The backup half of apply_patch and nothing else (orchestrate.py edit-preflight): only after explicit per-file approval, and only while the file still has the verified hash, create the exclusive, non-overwriting backup `<path>.bak-<UTC>` (-N on a collision) holding
    the verified bytes and their metadata. The note is NOT replaced: the writer makes the change with a recorded Edit. No approval or a hash mismatch changes nothing and creates no backup; the hash is checked again after the backup (a change in between is refused, the backup stays)."""
    if approved is not True: raise PermissionError("backup not approved for %s" % path)
    real = os.path.realpath(path)
    with open(real, "rb") as f: original = f.read()
    if hashlib.sha256(original).hexdigest() != expected_sha256: raise RuntimeError("file changed since verification: hash mismatch")
    now = now or datetime.datetime.now(datetime.timezone.utc)
    bak = _exclusive_backup("%s.bak-%s" % (path, now.strftime("%Y%m%dT%H%M%SZ")), original, real)
    if sha256_file(real) != expected_sha256: raise RuntimeError("file changed during the backup: hash mismatch")
    return bak

def apply_patch(path, expected_sha256, new_content, approved, now=None):
    """Apply a patch to ONE file only after explicit per-file approval, with hash check and a non-overwriting backup <file>.bak-<UTC> (-N on a collision, created exclusively, holding the verified bytes and their metadata).
    The new content is written as explicit UTF-8 bytes to a uniquely created temporary file next to the CANONICAL target (os.path.realpath(path): same filesystem; a symlink alias stays an alias), flushed, fsynced and closed with
    the mode of the target, and moved over the canonical target with os.replace. IMMEDIATELY before the replace both the canonical resolution/identity of `path` and the expected hash are checked again: an alias retargeted to another
    file (even one with equal bytes), a replaced target or changed bytes refuse the patch. Any failure (encoding, write, close, replace, retargeting, a changed hash) leaves the target as it was and removes only this operation's own
    temporary file; the backup, once made, stays; an external update is never overwritten or restored. No approval or an initial hash mismatch changes nothing and creates no backup.
    Limits (not handled): the replacement breaks hardlinks to the target; owner and extended attributes are not carried over."""
    if approved is not True:
        raise PermissionError("patch not approved for %s" % path)
    real = os.path.realpath(path); st0 = os.stat(real)
    with open(real, "rb") as f: original = f.read()
    if hashlib.sha256(original).hexdigest() != expected_sha256:
        raise RuntimeError("file changed since verification: hash mismatch")
    data = new_content.encode("utf-8")   # explicit bytes, whatever the locale; a content that cannot be encoded fails here, before anything is created
    now = now or datetime.datetime.now(datetime.timezone.utc)
    bak = _exclusive_backup("%s.bak-%s" % (path, now.strftime("%Y%m%dT%H%M%SZ")), original, real)
    fd, tmp = tempfile.mkstemp(prefix=".apply-patch-", suffix=".tmp", dir=os.path.dirname(real))
    try:
        try:
            _write_all(fd, data); os.fchmod(fd, stat.S_IMODE(st0.st_mode)); os.fsync(fd)
        except BaseException:
            try: os.close(fd)
            except OSError: pass
            raise
        os.close(fd)
        if os.path.realpath(path) != real: raise RuntimeError("the path no longer resolves to the file that was verified: refused")
        st1 = os.stat(real)
        if (st1.st_dev, st1.st_ino) != (st0.st_dev, st0.st_ino): raise RuntimeError("the target is no longer the file that was verified: refused")
        if sha256_file(real) != expected_sha256: raise RuntimeError("file changed during the patch: hash mismatch")
        os.replace(tmp, real)
    except BaseException:
        try: os.unlink(tmp)    # this operation's own temporary file only; the target is never restored over a concurrent update
        except OSError: pass
        raise
    return bak
