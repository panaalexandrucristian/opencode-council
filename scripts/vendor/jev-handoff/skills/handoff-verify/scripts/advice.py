#!/usr/bin/env python3
"""What to try first for a jev_verify / jev_gate claim that did not pass (deterministic hint, never a verdict; stdlib only, read-only).

The rules come from the real history of this machine (918 Claude Code and OpenCode sessions, 2026-10-07): 966 jev_verify results were not `verified` above 0.95;
of the 110 real cases later re-asked in a similar form and passed, 62 kept the same claim and changed the evidence (43 with > 20% more evidence) and 48 narrowed
the claim (32 with shorter, more focused evidence); `contradicted` results were real wrong facts (test counts, versions, inverted decisions), fixed by correcting the claim.
Backtest of these rules (836 non-lab sessions, 785 results that did not pass, 63 later re-asked in a similar form and passed): after add_direct_evidence the claim was kept with new
evidence 29 times and changed 28 times, so for low confidence the history does not tell which works (the hint asks for both when the claim is compound); none of the 76 fix_fact claims passed later.

The advice never changes a status and never relaxes report.retry_decision: at most one re-verification, only with NEW evidence or a corrected claim, never an identical call.

Each advice of the CLI also carries `evidence_request`: what is missing before that single re-verification (remedy, state, sources, rounds used). It only DESCRIBES the request; collecting, asking, authorizing and
recording are other steps, and `advise` (the report path) does not carry it. The identity of a request is the call result it refers to plus the stage and the version, never the check id or the bytes of an input.

The two halves of an omission pair (R04) have their own generator, `omission_advise`, because the generic advice is wrong for them: the ABSENCE half takes the complete canonical material and nothing else, and the SOURCE half takes exactly one
eligible record. A qualified `unsupported` ABSENCE is the EXPECTED half (next: the SOURCE call, a first call and no request); one that does not qualify takes no new evidence (blocked); an unqualified SOURCE may be re-asked once with another record of
the session. The log alone recognizes a SOURCE call (its canonical prefix); only the report writer recognizes an ABSENCE call (the canonical material, or a registered candidate), so the CLI keeps the generic advice for it.

CLI: advice.py --session ID|PATH.jsonl|opencode:ID|opencode-db:/ABS/DB#ID [--cwd DIR] [--registry PATH] [--version-ref-file FILE.json]   -> JSON with every verify/gate result that did not pass and its advice.
--registry reads the evidence_history of a candidate registry (read-only); an unreadable or malformed one refuses a new allowance (state blocked, stop_reason history_unavailable)."""
import argparse, json, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import audit as A, jevref as J

CODES = ("protocol_fields_absent", "evidence_off_subject", "fix_fact", "recheck_fact", "remove_unsupported_detail", "add_source_passage", "add_direct_evidence", "aux_condition_failed", "result_mismatch", "no_result",
         "absence_awaits_source", "absence_material_unchanged", "absence_material_not_canonical", "source_not_qualified")      # the last four: the two halves of an omission pair (omission_advise)
HINTS = {
    "protocol_fields_absent": "The recorded response does not contain %s, which the strict conditions need (same_subject on each result, subject_at at the top level of the response): this response cannot pass whatever the evidence, so more evidence will not help. Do not spend the single re-verification on it; the check stays UNRESOLVED. A future run needs a server whose recorded verify/gate responses carry these fields (an ABSENCE check of an omission pair does not need them).",
    "evidence_off_subject": "Jev judged the evidence to be about another subject (same_subject below subject_at): give the exact passage about this claim's subject.",
    "fix_fact": "The evidence contradicts the claim with high confidence: correct the fact from the source; more evidence of the same kind will not help.",
    "recheck_fact": "The evidence leans against the claim, below the threshold: compare the claim with the source; if it is wrong, correct it, if it is right, add the exact passage that states it.",
    "remove_unsupported_detail": "These details of the claim do not appear in the evidence: remove them from the claim or add the passage that states them.",
    "add_source_passage": "The evidence does not state the claim: add the exact source passage (command output, file lines, transcript quote) that states it.",
    "add_direct_evidence": "The claim is supported below the threshold: add the direct evidence for it (the exact output or lines), not a summary.",
    "aux_condition_failed": "Jev did not let the verdict stand on its own: the action is review, or same_subject/subject_at is missing. Give evidence about exactly this claim; a server that returns no same_subject/subject_at cannot pass the strict conditions.",
    "result_mismatch": "The result does not carry the claim that was sent (%s): no advice can be read from it; the check stays unresolved. Re-verify only with a corrected call that carries this claim.",
    "no_result": "No usable result: only a transport or invalid_response error allows one identical retry; otherwise the check stays unresolved.",
    "absence_awaits_source": "The ABSENCE half of an omission came back unsupported above the threshold with action auto: the note does not state the detail, which is the expected result of this half. If the SOURCE call has not been made yet, make it now (omissions.py prepare prints the claim and the passage): it is a first call, not a re-verification, and the retry advice for an unsupported claim does not apply to this half.",
    "absence_material_unchanged": "The ABSENCE result does not qualify (a real unsupported needs confidence above 0.95 and action explicitly auto). Its evidence is the complete canonical material of the note and nothing may be added to it (no neighbours, no free text, no other file): the same claim with the same material is not new evidence, so there is no re-verification and the omission stays UNRESOLVED. It needs no SOURCE call.",
    "absence_material_not_canonical": "The ABSENCE call did not use the complete canonical material that omissions.py prepare prints, so it proves nothing about the note, and the result does not qualify. A documentary correction cannot make the real call valid; the omission stays UNRESOLVED and needs no SOURCE call.",
    "source_not_qualified": "The SOURCE result does not qualify (verified above 0.95 with the strict auxiliary conditions). The single re-verification may use another eligible record of the session that states the same detail: give the exact quote and send the WHOLE passage of that record as the only evidence, never neighbouring records joined together, never a file and never an answer typed in the chat.",
}
VERSION_NOTE = ("No --version-ref-file: the requests are not bound to a version and their ids change once one is given; evidence.py authorize needs the ids of the bound requests, so give the same --version-ref-file "
                "(the target.version_ref of the request in the report) to advice.py, collect, authorize and check.")
CAPABILITY_NOTE = ("What the recorded verify/gate responses of this session show, per tool and envelope shape: the state (absent / null / invalid / finite) of same_subject on each result and of subject_at at the top level. Every response is judged on its own fields: "
                   "strict acceptance needs both finite at those locations, a response without them cannot be accepted whatever the evidence, and a later response that carries them is judged normally. Future runs need a server whose recorded responses carry these fields; "
                   "this skill installs or restarts nothing, retrofits no old result and leaves the retry rule unchanged.")
SPLIT_HINT = " It joins several facts: in the same single re-verification, split it into one claim per fact, each with only its own evidence."
# identifiers that must appear in the evidence when the claim states them: numbers, versions, hashes, paths, file names, code names
DETAIL = re.compile(r"`[^`]+`|[\w./~:-]*\d[\w./:-]*|[\w-]+(?:/[\w.-]+)+|\w+\.(?:md|txt|json|py|sh|yml|yaml|toml|js|mjs|ts)\b|\b\w+_\w+\b")
JOIN = re.compile(r";|\b(?:and|but|while|whereas|și|iar|dar|însă)\b", re.I)

def split_parts(claim):
    """The parts a compound claim is split into (see `compound`): the pieces of three or more words between `;` and the conjunctions, stripped of surrounding punctuation. Meaningful when `compound(claim)` is true."""
    return [p.strip(" ,;:") for p in JOIN.split(claim or "") if len(p.split()) >= 3]

def passed(entry, call):
    """The claim holds: `verified`, strictly > 0.95, with the strict auxiliary conditions (a confirmed `contradicted` is a claim that failed, not a pass)."""
    return str(entry.get("verdict")).lower() == "verified" and J.strict_pass(entry.get("confidence")) and J.aux_ok(entry, call, True)

def evidence_text(call):
    return " ".join(J.call_evidence(call))

def unsupported_details(claim, evidence):
    """Claim details (see DETAIL) that the normalized evidence does not contain, in claim order, without repeats."""
    ev, out = J._norm(evidence), []
    for m in DETAIL.findall(claim or ""):
        d = m.strip("`").rstrip(".,:;)").lower()
        if len(d) > 1 and d not in ev and d not in out: out.append(d)
    return out

def compound(claim):
    """True when the claim joins at least two facts (split on ; and the conjunctions only, never on a bare comma) of three or more words each."""
    parts = [p for p in JOIN.split(claim or "") if len(p.split()) >= 3]
    return len(parts) >= 2

FIELD_WHERE = (("same_subject", "same_subject (on the result)"), ("subject_at", "subject_at (at the top level)"))

def field_states(entry, call):
    """-> {same_subject, subject_at}: absent / null / invalid / finite as the REAL recorded response has them (jevref.field_state)."""
    return dict(same_subject=entry.get("same_subject_state") or J.field_state(entry, "same_subject"), subject_at=entry.get("subject_at_state") or J.field_state(call.get("parsed"), "subject_at"))

def causes_of(entry, call, fields):
    """Every coexisting reason why the result did not pass, in a FIXED order (the primary code is chosen by `advise`, by its precedence): protocol_fields_absent, subject_fields_null_or_invalid, evidence_off_subject,
    action_not_auto, verdict_contradicted | verdict_unsupported, confidence_not_above_threshold."""
    sa, ss, out = (call.get("parsed") or {}).get("subject_at"), entry.get("same_subject"), []
    if "absent" in fields.values(): out.append("protocol_fields_absent")
    if any(v in ("null", "invalid") for v in fields.values()): out.append("subject_fields_null_or_invalid")
    if J._num(ss) and J._num(sa) and ss < sa: out.append("evidence_off_subject")
    if entry.get("action") not in (None, "auto"): out.append("action_not_auto")
    verdict = str(entry.get("verdict")).lower()
    if verdict in ("contradicted", "unsupported"): out.append("verdict_" + verdict)
    if not J.strict_pass(entry.get("confidence")): out.append("confidence_not_above_threshold")
    return out

def advise(entry, call):
    """entry: a jevref.results_of entry of a verify/gate call; -> {code, hint[, details][, split], fields, causes[, evidence_retry]}, or None when the result passed. `fields` = the recorded state of same_subject / subject_at
    (absent|null|invalid|finite), `causes` = every coexisting reason in a fixed order. protocol_fields_absent (a usable, correctly identified result whose response lacks a field the strict conditions need) comes before the
    evidence-focused codes and recommends NO evidence retry (`evidence_retry` false)."""
    if entry.get("mismatch"): return dict(code="result_mismatch", hint=HINTS["result_mismatch"] % entry["mismatch"])
    if call.get("is_error") or entry.get("verdict") is None or entry.get("confidence") is None:
        return dict(code="no_result", hint=HINTS["no_result"])
    if passed(entry, call): return None
    fields = field_states(entry, call)
    miss = [w for k, w in FIELD_WHERE if fields[k] == "absent"]
    a = dict(code="protocol_fields_absent", hint=HINTS["protocol_fields_absent"] % " and ".join(miss), evidence_retry=False) if miss else _evidence_advice(entry, call)
    return dict(a, fields=fields, causes=causes_of(entry, call, fields))

def _evidence_advice(entry, call):
    """The established evidence-focused advice (precedence unchanged)."""
    sa, ss = (call.get("parsed") or {}).get("subject_at"), entry.get("same_subject")
    verdict = str(entry["verdict"]).lower()
    if J._num(ss) and J._num(sa) and ss < sa: code = "evidence_off_subject"
    elif verdict == "contradicted": code = "fix_fact" if J.strict_pass(entry["confidence"]) else "recheck_fact"
    elif verdict == "unsupported":
        miss = unsupported_details(entry.get("key"), evidence_text(call))
        if miss: return dict(code="remove_unsupported_detail", hint=HINTS["remove_unsupported_detail"], details=miss)
        code = "add_source_passage"
    elif verdict == "verified" and J.strict_pass(entry["confidence"]): code = "aux_condition_failed"
    elif compound(entry.get("key")): return dict(code="add_direct_evidence", hint=HINTS["add_direct_evidence"] + SPLIT_HINT, split=True)
    else: code = "add_direct_evidence"
    return dict(code=code, hint=HINTS[code])

def stage_of(claim, tool, material_ok=False, registered=False):
    """The R04 stage of a verify check, from facts only: `source` when the claim starts with the canonical SOURCE prefix, `absence` when the claim starts with the (R03) ABSENCE prefix, the call's evidence is exactly the complete canonical
    material of the note version (`material_ok`), or the bare claim is a detail registered as a candidate of that evaluation (`registered`; both are known to the report writer only); None otherwise. The claim decides first. A gate call is never
    one of the two halves."""
    if tool != "verify" or not isinstance(claim, str): return None
    import omissions as O
    c = O.wsnorm(claim)
    if c.startswith(O.SOURCE_PREFIX.strip()): return "source"
    return "absence" if material_ok or registered or c.startswith(O.ABSENCE_PREFIX.strip()) else None

def qualified_absence(entry, call):
    """The absence half of a valid pair: a real `unsupported` above the threshold with action EXPLICITLY auto (same_subject is not required for this half, and only for it), on an error-free jev_verify call."""
    return (call.get("tool") == "verify" and not call.get("is_error") and str(entry.get("verdict")).lower() == "unsupported" and J.strict_pass(entry.get("confidence")) and entry.get("action") == "auto")

def omission_advise(stage, entry, call, material_ok=True):
    """The distinct generator for the two halves of an omission pair (R04); -> {code, hint, stage, fields, causes[, evidence_retry]} or None when the result needs nothing. A qualified unsupported ABSENCE is the EXPECTED half: the
    next step is the SOURCE call (not a re-verification). An ABSENCE that does not qualify takes no new evidence (its evidence is the complete canonical material), and an unqualified SOURCE may be re-asked once with another eligible
    record. Pure; it never changes a status, and the pair exception (the checks of a finding-linked pair get no advice) is applied by the report writer before this is reached."""
    fields = field_states(entry, call); causes = causes_of(entry, call, fields)
    if stage == "absence":
        code = "absence_awaits_source" if qualified_absence(entry, call) else ("absence_material_unchanged" if material_ok else "absence_material_not_canonical")
        return dict(code=code, hint=HINTS[code], stage=stage, evidence_retry=False, fields=fields, causes=causes)
    if stage == "source":
        if passed(entry, call): return None
        return dict(code="source_not_qualified", hint=HINTS["source_not_qualified"], stage=stage, fields=fields, causes=causes)
    raise ValueError("unknown omission stage %r" % (stage,))

ROUNDS_LIMIT = 1                      # one re-verification per request (the user's rule per_check_only), never a wave
SOURCES = ("session_transcript", "admitted_local_file")   # where new evidence may come from: never an answer typed in the chat
# primary advice code -> (remedy, allowed sources, missing artifacts). Only codes that advice.py itself produces; the identity, material and audit families belong to the later steps.
REMEDY = {
    "add_direct_evidence": ("semantic_evidence", SOURCES, ("direct_output_or_lines",)),
    "add_source_passage": ("semantic_evidence", SOURCES, ("source_passage",)),
    "evidence_off_subject": ("semantic_evidence", SOURCES, ("passage_about_claim_subject",)),
    "aux_condition_failed": ("semantic_evidence", SOURCES, ("passage_about_exact_claim",)),
    "remove_unsupported_detail": ("claim_correction", (), ()),
    "recheck_fact": ("claim_correction", (), ()),
    "fix_fact": ("claim_correction", (), ()),
    "result_mismatch": ("claim_correction", (), ("corrected_call_with_claim",)),
    "protocol_fields_absent": ("protocol_blocker", (), ()),
    "no_result": ("transport_retry", (), ()),
    "absence_material_unchanged": ("protocol_blocker", (), ()),        # the ABSENCE half takes the complete canonical material and nothing else: no new evidence exists
    "absence_material_not_canonical": ("protocol_blocker", (), ()),
    "source_not_qualified": ("semantic_evidence", ("session_transcript",), ("another_eligible_record_stating_the_detail",)),     # one other eligible record of the session, never a file
}

def _jev_ref(entry, call):
    """The result a request refers to; a call without a usable result (or a result without a text key) is the whole call, with index and key null."""
    if entry is not None and type(entry.get("index")) is int and isinstance(entry.get("key"), str): return dict(tool_use_id=call["tool_use_id"], result_index=entry["index"], key=entry["key"])
    return dict(tool_use_id=call["tool_use_id"], result_index=None, key=None)

def evidence_request(entry, call, adv, history=(), version_ref=None, stage=None):
    """-> the evidence_request of one result that did not pass (`adv` = advise(entry, call), or the call-level no_result advice with entry None). Pure: reads nothing, calls nothing.
    state: collect_local (the first thing to do is look for the missing material locally), blocked (no evidence can help, or the history is lost) or exhausted (the single re-verification of this request is already
    authorized or recorded in `history`). `history` = the evidence_history of the registry; None = lost or unreadable, which refuses a new allowance. The request id comes from the target only."""
    if A.key_from_dict(version_ref) is None: raise ValueError("version_ref is not a version identity")
    code, causes = adv["code"], adv.get("causes", [])
    remedy, sources, missing = REMEDY[code]
    stop = {"protocol_fields_absent": "protocol_fields_absent", "no_result": "no_usable_result", "absence_material_unchanged": "absence_material_unchanged", "absence_material_not_canonical": "absence_material_not_canonical"}.get(code)
    if stop is None and remedy == "semantic_evidence" and "subject_fields_null_or_invalid" in causes:      # more text cannot repair a response whose subject fields are null or invalid
        remedy, sources, missing, stop = "protocol_blocker", (), (), "subject_fields_null_or_invalid"
    target = dict(jev_ref=_jev_ref(entry, call), version_ref=version_ref, stage=stage or call["tool"])      # `absence` / `source` for the two halves of an omission pair (omission_advise), else the tool
    rid = A.evidence_request_id(target)
    called = target["jev_ref"]["tool_use_id"]      # a result of a call that is itself the RECORDED re-verification of another request is exhausted: asking again under the id of the re-verification would reset the allowance
    chained = history is not None and any(isinstance(h, dict) and h.get("state") == "recorded" and isinstance(h.get("recorded_ref"), dict) and h["recorded_ref"].get("tool_use_id") == called for h in history)
    used = 0 if history is None else (ROUNDS_LIMIT if chained else min(ROUNDS_LIMIT, sum(1 for h in history if isinstance(h, dict) and h.get("request_id") == rid)))
    if stop: state = "blocked"
    elif history is None: state, stop = "blocked", "history_unavailable"
    elif chained: state, stop = "exhausted", "result_of_a_reverification"
    elif used >= ROUNDS_LIMIT: state, stop = "exhausted", "reverification_used"
    else: state = "collect_local"
    return dict(schema_version=1, request_id=rid, reason_codes=[code] + [x for x in causes if x != code], target=target, state=state, remedy=remedy, allowed_sources=list(sources),
                missing_artifacts=list(missing) + ["detail:" + d for d in adv.get("details", [])], question_ro=None, rounds_used=used, rounds_limit=ROUNDS_LIMIT,
                retry_remaining=0 if state != "collect_local" else ROUNDS_LIMIT - used, stop_reason=stop)

def session_advice(calls, history=(), version_ref=None):
    out = []
    for c in calls:
        if c["tool"] not in ("verify", "gate"): continue
        ents, why = J.results_of(c)
        if why:
            a = dict(code="no_result", hint=HINTS["no_result"])
            out.append(dict(tool_use_id=c["tool_use_id"], tool=c["tool"], result_index=None, claim=None, verdict=None, confidence=None, advice=dict(a, evidence_request=evidence_request(None, c, a, history, version_ref)))); continue
        for e in ents:
            stage = "source" if stage_of(e["key"], c["tool"]) == "source" else None      # the log alone can tell a SOURCE call (canonical prefix); only the report can tell an ABSENCE call (it knows the canonical material)
            a = omission_advise(stage, e, c, False) if stage else advise(e, c)
            if a: out.append(dict(tool_use_id=c["tool_use_id"], tool=c["tool"], result_index=e["index"], claim=e["key"], verdict=e["verdict"], confidence=e["confidence"], advice=dict(a, evidence_request=evidence_request(e, c, a, history, version_ref, stage))))
    return out

def load_history(registry_path):
    """-> (history, consulted, error). No path: no history is consulted ((), False, None). A readable registry: its evidence_history. An unreadable or malformed one: history None, which refuses a new allowance."""
    if not registry_path: return (), False, None
    doc, why = A.load_registry(registry_path)
    return (None, False, why) if doc is None else (doc["evidence_history"], True, None)

def load_version_ref(path):
    """-> (version_ref | None, error | None) from a JSON file {write_tool_use_id, sha256, evaluated_against[, run]}; no path = no version (None, None): the requests are then not bound to a version."""
    if not path: return None, None
    try: d = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError) as e: return None, "version_ref file unreadable (%s)" % e.__class__.__name__
    if not isinstance(d, dict) or A.key_from_dict(d) is None: return None, "version_ref file is not a version identity (write_tool_use_id, sha256, evaluated_against[, run])"
    return d, None

def build_parser():
    ap = argparse.ArgumentParser(); ap.add_argument("--session"); ap.add_argument("--cwd"); ap.add_argument("--registry"); ap.add_argument("--version-ref-file")
    return ap

def main():
    a = build_parser().parse_args()
    import discover as D
    sp, how, amb = D.resolve_session_info(a.session, a.cwd)
    if amb: print(json.dumps({"error": "current session not demonstrated: several recently modified sessions; pass --session ID or PATH.jsonl", "resolution": how})); return 3
    if not sp or not D.source_exists(sp): print(json.dumps({"error": "session not found", "arg": a.session})); return 2
    calls = J.load_calls(sp)
    vref, bad = load_version_ref(a.version_ref_file)
    if bad: print(json.dumps({"error": bad})); return 2
    history, consulted, why = load_history(a.registry)
    extra = dict(history_error=why) if why else {}
    print(json.dumps(dict(session=sp, resolution=how, rule="at most one re-verification, with new evidence or a corrected claim; never an identical call", history_consulted=consulted, version_bound=vref is not None, **({} if vref is not None else {"version_note": VERSION_NOTE}), **extra, results=session_advice(calls, history, vref),
                          capabilities=J.capabilities(calls), capability_note=CAPABILITY_NOTE, gate_summaries=[dict(tool_use_id=c["tool_use_id"], **J.gate_summary(c)) for c in calls if c["tool"] == "gate"]), indent=1, ensure_ascii=False)); return 0

if __name__ == "__main__": sys.exit(main())
