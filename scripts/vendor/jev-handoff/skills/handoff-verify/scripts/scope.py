#!/usr/bin/env python3
"""Session scope filter (R05). Stdlib only, read-only: it parses Claude Code JSONL transcripts (or an OpenCode selector) and never executes anything it reads.

The SCOPE of a session is the user's own requests in it (prompts, trimmed; slash commands with arguments as `/name args`; question/answer pairs of the model's questions), in order, up to the start of the evaluated verification run
(`omissions.source_window`: the only run after the evaluated write, or the one the evaluation names; never the last by default; harness notices, slash commands without arguments and the wording around question answers are not requests). Before the omission checks, every candidate detail is classified by ONE `jev_classify` call per batch of at most 64 candidates, whose purpose, class catalog and context are the
canonical ones printed by `prepare` (never typed by the model). A candidate is EXCLUDED as out of scope only when the real result is `out_of_scope` with a confidence STRICTLY above 0.99 and the decision explicitly
`auto`; every other candidate (in_scope, manual_review, <= 0.99, review, error) goes through the omission checks as before. Exclusions are recorded in the report as `scope_exclusions` and re-derived by
`validate_exclusions` (report.py): an exclusion that cannot be demonstrated makes the report UNRESOLVED, never PASS.

CLI: scope.py prepare --source ID|PATH.jsonl|opencode:ID|opencode-db:/ABS/DB#ID (--detail TEXT ... | --spec FILE.json) [--cwd DIR] [--write-id ID --evaluated-against prefix|session_end [--run ID]]
  FILE.json = a list of detail strings (or {"details": [...]}). Prints {ok, scope_sha256, requests, evaluation, payloads: [{purpose, classes, context, items}]}; exit 3 when the scope or a detail is not usable (nothing is excluded then).
  The scope is the one of ONE evaluation: the window of the write (`--write-id` + `--evaluated-against`, plus `--run` when several verification runs follow the write) that the candidate details are checked against; without a
  write it is the window of the whole session (exit 3 when several runs make it ambiguous). `evaluation` is printed to be copied into every exclusion made from these payloads (`scope_exclusions[].evaluation`); an exclusion without it, in a report about versions, is re-derived in the evaluation of the report (`report_evaluation`)."""
import argparse, hashlib, json, math, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sanitize as S

THRESHOLD = 0.99           # strictly above; 0.99 itself keeps the detail
OUT_CLASS = "out_of_scope"
MAX_ITEMS = 64             # jev_classify maxItems
MAX_DETAIL = 2000          # jev_classify truncates item text at 2000 characters: a longer detail could be judged on a truncated text
MAX_CONTEXT = 100000       # a larger scope is not sent: nothing is excluded
PURPOSE = "Decide, for each candidate detail extracted from a coding-session transcript, whether it is within the scope of the session, defined only by the user's requests quoted in the context."
CLASSES = [
    {"id": "in_scope", "description": "The detail concerns what the user asked for in the quoted requests: the requested change, its design, its tests, its commit or push, the constraints and decisions about it, or problems met while doing it. When in doubt between in_scope and out_of_scope, prefer in_scope or manual_review."},
    {"id": "out_of_scope", "description": "The detail is unrelated to every quoted user request: another project, another task the user did not ask for in this session, personal or environment information with no bearing on the requested work, or tool chatter. Choose it only when no quoted request concerns the detail at all."},
    {"id": "manual_review", "description": "It is unclear whether the detail concerns the quoted requests, or it is related to them only indirectly."},
]
_SKIP = ("<local-command-", "<task-notification>", "<bash-input>", "<bash-stdout>", "<bash-stderr>", "[Request interrupted by user")
_CMD = re.compile(r"<command-name>\s*(/?[^<\s]+)\s*</command-name>")
_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.S)
_ANSWER = re.compile(r'"((?:[^"\\]|\\.)*)"="((?:[^"\\]|\\.)*)"')   # AskUserQuestion result: "question"="answer", ...

def wsnorm(s): return " ".join(str(s if s is not None else "").split())
def sha256_text(t): return hashlib.sha256(t.encode("utf-8")).hexdigest()

def _prompt_text(t):
    """The request carried by one user text, or None: harness notices are skipped; a slash command becomes `/name args` (a command without arguments, such as /clear, carries no request)."""
    if not isinstance(t, str) or not t.strip() or t.lstrip().startswith(_SKIP): return None
    m = _CMD.search(t)
    if m:
        a = _ARGS.search(t); args = a.group(1).strip() if a else ""
        if not args: return None
        name = m.group(1) if m.group(1).startswith("/") else "/" + m.group(1)
        return name + " " + args
    return t.strip()

def user_requests(records, write_id=None, evaluated_against="session_end", run=None):
    """The user's requests in order (each text trimmed; a slash command as `/name args`), strictly before the shared source window's limit (`omissions.source_window`: the start of the last verification run, after
    the evaluated write when one is given; None = the whole session): user prompts that are not harness notices, meta records or compaction summaries, slash-command arguments, and only the question/answer pairs
    of AskUserQuestion results ("Q: ..\nA: .."). Prompts between runs are kept; skill/Jev material and the spans of earlier runs are not. Every tool_result is counted on the timeline, also inside a record that
    is skipped, so the limit is the position `omissions.eligible_blocks` uses. -> [text]."""
    import omissions as O
    w = O.source_window(records, write_id, evaluated_against, run); limit, gone = w["limit"], w["excluded"]; n, ask, out = 0, set(), []
    for i, d in enumerate(records):
        if limit is not None and n >= limit: break
        msg = d.get("message") if isinstance(d.get("message"), dict) else {}; c = msg.get("content"); t = d.get("type")
        if t == "assistant" and isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    n += 1
                    if b.get("name") == "AskUserQuestion": ask.add(b.get("id"))
            continue
        if t != "user": continue
        skip = bool(d.get("isMeta") or d.get("isCompactSummary"))
        if skip:
            n += sum(1 for b in c if isinstance(b, dict) and b.get("type") == "tool_result") if isinstance(c, list) else 0
            continue
        if isinstance(c, str):
            x = _prompt_text(c)
            if x and (i, 0) not in gone: out.append(x)
            continue
        for j, b in enumerate(c if isinstance(c, list) else []):
            if not isinstance(b, dict): continue
            if b.get("type") == "tool_result":
                n += 1
                if b.get("tool_use_id") in ask and (limit is None or n < limit) and (i, j) not in gone:
                    r = b.get("content"); r = r if isinstance(r, str) else "".join(y.get("text", "") for y in r if isinstance(y, dict)) if isinstance(r, list) else ""
                    qa = _ANSWER.findall(r)   # only the user's answers, never the harness wording around them (a rejected question has none)
                    if qa: out.append("\n".join("Q: %s\nA: %s" % (q, x) for q, x in qa))
            elif b.get("type") == "text" and (i, j) not in gone:
                x = _prompt_text(b.get("text"))
                if x: out.append(x)
    return out

def canonical_context(requests):
    out = "USER REQUESTS OF THE SESSION (verbatim, in order):\n"
    for i, r in enumerate(requests, 1): out += "=== REQUEST %d ===\n%s\n" % (i, r)
    return out + "=== END USER REQUESTS ==="

_MEMO = {}
def scope_of(source, write_id=None, evaluated_against="session_end", run=None):
    """-> (context | None, requests, reason) for a source session in ONE window (omissions.source_window): the evaluated write, its mode and, for session_end, the evaluated run (the only one after the write, or the
    named one; several without a name are ambiguous: no scope). Without a write the window is the whole session. Cached by transcript identity AND window."""
    import discover as D, omissions as O
    try: key = (D.content_fingerprint(source), write_id, evaluated_against, run)
    except OSError: return None, [], "source transcript unreadable"
    if key not in _MEMO:
        recs = D.load_jsonl(source); w = O.source_window(recs, write_id, evaluated_against, run)
        if w["ambiguous"]: _MEMO[key] = (None, [], w["ambiguous"])
        else:
            reqs = user_requests(recs, write_id, evaluated_against, run)
            ctx = canonical_context(reqs)
            dep = [i for i, r in enumerate(reqs, 1) if S.altered(r)["redaction_dependent"]]
            why = "no user request found before the verification activity" if not reqs else ("the user requests hold secrets or redaction markers (request %s): what they hide is not demonstrated, so nothing is excluded" % ", ".join(map(str, dep)) if dep else "scope too large (%d characters > %d): nothing is excluded" % (len(ctx), MAX_CONTEXT) if len(ctx) > MAX_CONTEXT else None)
            _MEMO[key] = (None if why else ctx, reqs, why)
    ctx, reqs, why = _MEMO[key]
    return ctx, list(reqs), why   # a copy: a caller cannot corrupt the cached scope

def payloads(context, details):
    """The canonical jev_classify inputs: batches of at most 64 items, ids d1..dN over all batches, item text = the whitespace-normalized detail."""
    items = [{"id": "d%d" % (i + 1), "text": wsnorm(x)} for i, x in enumerate(details)]
    return [dict(purpose=PURPOSE, classes=CLASSES, context=context, items=items[k:k + MAX_ITEMS]) for k in range(0, len(items), MAX_ITEMS)]

def _strict(v): return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > THRESHOLD

def evaluation_of(e, default=None):
    """-> ((write id | None, evaluated_against, run | None), None) or (None, reason): the evaluation an exclusion names (`evaluation` = {write_tool_use_id, evaluated_against[, run]}). Without `evaluation`:
    `default` = None is the window of the whole session (no write; a report that has no version evaluation), ("evaluation", (write, mode, run)) is the evaluation of the report (see `report_evaluation`), and
    ("undemonstrated", reason) leaves the exclusion without one."""
    ev = e.get("evaluation")
    if ev is None:
        if default is None: return (None, "session_end", None), None
        return (default[1], None) if default[0] == "evaluation" else (None, default[1])
    if not isinstance(ev, dict) or set(ev) - {"write_tool_use_id", "evaluated_against", "run"}: return None, "malformed evaluation (write_tool_use_id, evaluated_against[, run])"
    wid, ea, run = ev.get("write_tool_use_id"), ev.get("evaluated_against"), ev.get("run")
    if not isinstance(wid, str) or not wid or ea not in ("prefix", "session_end") or (run is not None and (not isinstance(run, str) or not run or ea != "session_end")): return None, "malformed evaluation (write_tool_use_id, evaluated_against prefix|session_end, run only for session_end)"
    return (wid, ea, run), None

def report_evaluation(checks):
    """The evaluation of a report about versions, for the exclusions that name none: the distinct (write_tool_use_id, evaluated_against, run) the version_refs of its checks name. -> None (no check names one: the
    global scope of the session), ("evaluation", key) (exactly one) or ("undemonstrated", reason) (several: an exclusion must name its own)."""
    keys = []
    for c in checks if isinstance(checks, list) else []:
        r = c.get("version_ref") if isinstance(c, dict) and isinstance(c.get("version_ref"), dict) else None
        if r and isinstance(r.get("write_tool_use_id"), str) and r["write_tool_use_id"] and r.get("evaluated_against") in ("prefix", "session_end"):
            k = (r["write_tool_use_id"], r["evaluated_against"], r.get("run") if isinstance(r.get("run"), str) and r.get("run") else None)
            if k not in keys: keys.append(k)
    if not keys: return None
    return ("evaluation", keys[0]) if len(keys) == 1 else ("undemonstrated", "the report names several evaluations (%d) and the exclusion names none: its own evaluation is not demonstrated (copy `evaluation` from scope.py prepare)" % len(keys))

def validate_exclusions(exclusions, calls, source, findings=(), writes=None, default=None):
    """Re-derive every declared exclusion. -> dict(valid, invalid, reasons=[{index, detail, reason}]). An exclusion {detail, jev_ref {tool_use_id, result_index, key}, classification, confidence} is VALID only if:
    the call is a real, error-free jev_classify call of the current session; its purpose, classes and context are exactly the canonical ones for the source session's scope; the result at result_index carries the id
    `key` of the input item at that index, whose text equals the whitespace-normalized detail; the real classification is out_of_scope with a finite confidence STRICTLY above 0.99, equal to the declared one, and the
    decision is explicitly `auto`; no other exclusion uses the same (call, result); no lost_detail finding is about the same detail. The scope is re-derived in the evaluation the exclusion NAMES (`evaluation`, see
    `evaluation_of`: its own write, mode and run), so a report with several evaluations validates each exclusion on its own window and a later write or run never reinterprets an earlier exclusion; `writes` (the ids of the
    handoff's writes, when known) must contain the named write. `default` = what an exclusion without `evaluation` means (`evaluation_of`; `report_evaluation` of the checks for a report about versions)."""
    import jevref as J
    ex = exclusions if isinstance(exclusions, list) else None
    if ex is None: return dict(valid=0, invalid=1, reasons=[dict(index=None, detail=None, reason="scope_exclusions is not a list")])
    if not ex: return dict(valid=0, invalid=0, reasons=[])
    by_id = {c["tool_use_id"]: c for c in calls}; used = set(); bad = []
    scopes = {}
    def scope_for(key):
        if key not in scopes: scopes[key] = scope_of(source, *key) if isinstance(source, str) else (None, [], "report names no source session")
        return scopes[key]
    lost = {wsnorm((f.get("omission_ref") or {}).get("detail")) for f in findings if isinstance(f, dict) and f.get("type") == "lost_detail" and isinstance(f.get("omission_ref"), dict)}
    for i, e in enumerate(ex):
        def no(r): bad.append(dict(index=i, detail=S.sanitize(e["detail"])[0] if isinstance(e, dict) and isinstance(e.get("detail"), str) else None, reason=r))   # the diagnostic never echoes a credential; the validation uses the original detail
        if not isinstance(e, dict) or not isinstance(e.get("detail"), str) or not wsnorm(e["detail"]): no("malformed exclusion (detail)"); continue
        if S.altered(wsnorm(e["detail"]))["redaction_dependent"]: no("the detail holds a secret or a redaction marker: a redaction-dependent exclusion cannot be certified (what is hidden is not demonstrated)"); continue
        ref = e.get("jev_ref") if isinstance(e.get("jev_ref"), dict) else {}
        tid, ri, key = ref.get("tool_use_id"), ref.get("result_index"), ref.get("key")
        if not isinstance(tid, str) or not isinstance(ri, int) or isinstance(ri, bool) or ri < 0 or not isinstance(key, str): no("malformed jev_ref"); continue
        call = by_id.get(tid)
        if call is None: no("tool_use_id not found among the session's Jev calls"); continue
        if call["tool"] != "classify": no("the call is not jev_classify"); continue
        evk, bad_ev = evaluation_of(e, default)
        if evk is None: no(bad_ev); continue
        if writes is not None and evk[0] is not None and evk[0] not in writes: no("the evaluation names a write that is not a version of the handoff"); continue
        ctx, _, why = scope_for(evk)
        if ctx is None: no("scope not demonstrated: %s" % why); continue
        inp = call["input"]
        if set(inp) - {"purpose", "classes", "context", "items"}: no("the call carries other arguments (%s): the decision thresholds must be Jev's defaults" % ", ".join(sorted(set(inp) - {"purpose", "classes", "context", "items"}))); continue
        if inp.get("purpose") != PURPOSE or inp.get("classes") != CLASSES: no("purpose or classes differ from the canonical ones"); continue
        if inp.get("context") != ctx: no("context is not the canonical scope of the source session in the named evaluation (its user requests, as scope.py extracts them)"); continue
        ents, w = J.results_of(call)
        if w: no(w); continue
        r = next((x for x in ents if x["index"] == ri), None)
        if r is None: no("result_index %d does not exist" % ri); continue
        if r.get("mismatch"): no(r["mismatch"]); continue
        if r["key"] != key: no("key differs from the input item id at that index"); continue
        items = inp.get("items") if isinstance(inp.get("items"), list) else []
        if ri >= len(items) or not isinstance(items[ri], dict) or items[ri].get("text") != wsnorm(e["detail"]): no("the classified item text is not the whitespace-normalized detail"); continue
        if r["verdict"] != OUT_CLASS: no("real classification is %r, not out_of_scope" % r["verdict"]); continue
        if not _strict(r["confidence"]): no("real confidence %r is not strictly above %s" % (r["confidence"], THRESHOLD)); continue
        dc = e.get("confidence")
        if isinstance(dc, bool) or not isinstance(dc, (int, float)) or dc != r["confidence"] or e.get("classification") != r["verdict"]: no("declared classification/confidence differ from the real result (the confidence must be the same number)"); continue
        if r.get("action") != "auto": no("the decision is not explicitly auto"); continue
        if (tid, ri) in used: no("(call, result) already used by another exclusion"); continue
        if wsnorm(e["detail"]) in lost: no("a lost_detail finding is about the same detail"); continue
        used.add((tid, ri))
    return dict(valid=len(ex) - len(bad), invalid=len(bad), reasons=bad)

def cmd_prepare(a):
    import discover as D
    def fail(*r): print(json.dumps(dict(ok=False, reasons=[S.sanitize(str(x))[0] for x in r]), indent=1, ensure_ascii=False)); return 3   # a diagnostic never echoes a credential (an unreadable spec's file name included)
    sp, how, amb = D.resolve_session_info(a.source, a.cwd)
    if amb: return fail("source session not demonstrated: several recently modified sessions; pass --source ID or PATH.jsonl")
    if not sp or not D.source_exists(sp): return fail("source session not found")
    import omissions as O
    if O.identity_dependent(sp, D.canon(sp)): return fail("the source session path holds a secret or a redaction marker: an identity that needs redaction is not used, so no scope is prepared (the value is not echoed)")   # the identity is printed below: never altered, never echoed
    if O.identity_dependent(a.write_id, a.run, role=S.ID_ROLE): return fail("the write id or the run id holds a secret or a redaction marker: an identity that needs redaction is not used, so no scope is prepared (the value is not echoed)")
    details = list(a.detail or [])
    if a.spec:
        try: s = json.load(sys.stdin) if a.spec == "-" else json.load(open(a.spec, encoding="utf-8"))   # `--spec -`: the JSON comes from stdin, no temporary file
        except (OSError, ValueError) as e: return fail("spec unreadable: %s" % ("stdin is not valid JSON" if a.spec == "-" else e))
        s = s.get("details") if isinstance(s, dict) else s
        if not isinstance(s, list) or not all(isinstance(x, str) for x in s): return fail('spec must be a list of strings (or {"details": [...]})')
        details += s
    if not details: return fail("no detail given")
    for i, x in enumerate(details):
        if not wsnorm(x): return fail("detail %d is empty" % i)
        if S.altered(wsnorm(x))["redaction_dependent"]: return fail("detail %d holds a secret or a redaction marker: a redaction-dependent detail is not classified (nothing is excluded; the value is not echoed)" % i)
        if len(wsnorm(x)) > MAX_DETAIL: return fail("detail %d is longer than %d characters (jev_classify would judge a truncated text)" % (i, MAX_DETAIL))
    if bool(a.write_id) != bool(a.evaluated_against): return fail("--write-id and --evaluated-against go together (the scope is the one of ONE evaluation)")
    if a.run and not a.write_id: return fail("--run needs --write-id and --evaluated-against session_end")
    ctx, reqs, why = scope_of(sp, a.write_id, a.evaluated_against or "session_end", a.run)
    if ctx is None: return fail(why)
    w = O.source_window(D.load_jsonl(sp), a.write_id, a.evaluated_against or "session_end", a.run)
    if O.identity_dependent(w["run"], role=S.ID_ROLE): return fail("the selected verification run id holds a secret or a redaction marker: an identity that needs redaction is not used, so no scope is prepared (the value is not echoed)")
    evaluation = dict(write_tool_use_id=a.write_id, evaluated_against=a.evaluated_against, **({"run": w["run"]} if a.evaluated_against == "session_end" and w["run"] else {})) if a.write_id else None
    print(json.dumps(dict(ok=True, source=sp, scope_sha256=sha256_text(ctx), requests=len(reqs), threshold=THRESHOLD, evaluation=evaluation, payloads=payloads(ctx, details),
                          note="Call jev_classify once per payload, with purpose, classes, context and items copied verbatim (add nothing else). A detail is excluded only when its result is out_of_scope with confidence strictly above 0.99 and decision auto: record it in scope_exclusions {detail, jev_ref {tool_use_id, result_index, key = the item id}, classification, confidence, evaluation (copy the `evaluation` printed here)} and run no omission check for it. Every other detail goes through the omission checks as before."), indent=1, ensure_ascii=False))
    return 0

def main():
    ap = argparse.ArgumentParser(); sub = ap.add_subparsers(dest="cmd", required=True); p = sub.add_parser("prepare")
    p.add_argument("--source"); p.add_argument("--detail", action="append"); p.add_argument("--spec"); p.add_argument("--cwd"); p.add_argument("--write-id", dest="write_id"); p.add_argument("--evaluated-against", choices=("prefix", "session_end"), dest="evaluated_against"); p.add_argument("--run")
    a = ap.parse_args()
    return cmd_prepare(a)

if __name__ == "__main__": sys.exit(main())
