#!/usr/bin/env python3
"""Exact binding of report checks to real Jev tool calls (jev_ref v1). Stdlib only, read-only: it parses a Claude Code JSONL (session log or stream-json) and never
executes anything it reads.

A check is BOUND only when its `jev_ref` = {tool_use_id, result_index, key} names a real, non-error Jev call of the same tool, the selected result exists, the result
belongs to the exact input element named by `key` (per-tool adapter, never a recursive search), verdict and confidence are exactly equal, and the (call, result) pair is not
used by another check of the same report. Text overlap, uniqueness and "same confidence" are never evidence. Unknown response shapes stay unbound.
Binding and resolution are distinct: a bound check is RESOLVED only if the strict threshold (> 0.95) and the auxiliary Jev conditions also hold.

CLI: jevref.py list [--session ID|PATH.jsonl|opencode:ID|opencode-db:/ABS/DB#ID] [--cwd DIR]   -> JSON with every Jev call of the session: tool_use_id, tool, per result index/key/verdict/confidence."""
import argparse, hashlib, io, json, math, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import opencode as O

PREFIX = ("mcp__jev__", "mcp__plugin_jev_jev__")   # direct MCP config, or the server shipped by the jev Claude Code plugin
VERDICT_KEYS = ("verdict", "relation", "classification", "decision", "class", "label", "choice")   # jev_classify returns `classification` (and `decision` = auto/review, which is the action)

def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and not (isinstance(v, float) and math.isnan(v))

def is_probability(v):
    """A probability: an int or float (not a bool, not a string), finite and within [0, 1]. The domain test that comes BEFORE the strict threshold: a malformed confidence is unusable, never clamped or rounded.
    (`_num` stays the looser test of the auxiliary numbers same_subject / subject_at.)"""
    return _num(v) and 0 <= v <= 1 and math.isfinite(v)   # bounds first: an int beyond float range is out of domain, never an OverflowError (NaN is already excluded by _num)

def strict_pass(confidence):
    """A probability strictly > 0.95, no rounding (same rule as report.passes)."""
    return is_probability(confidence) and confidence > 0.95

CONTRACTS = ("R02", "R03", "R04")
def contract_of(x):
    """The omission contract an evaluator applies, normalized: "R02" (baseline: legacy auxiliary rules, no omission pair), "R03" (strict auxiliary conditions, omission pair with the absence claim `neither states nor implies`,
    `verified`), "R04" (strict auxiliary conditions, omission pair whose absence half is the bare detail with a real `unsupported`; see `_validate_omission`). The legacy booleans keep their meaning of the callers written
    before R04 (False = R02, True = R03); a string is matched exactly after strip/upper (never by truthiness); anything else, including None, raises: the evaluator is always chosen explicitly."""
    if x is False: return "R02"
    if x is True: return "R03"
    if isinstance(x, str) and x.strip().upper() in CONTRACTS: return x.strip().upper()
    raise ValueError("unknown omission contract %r (expected R02, R03 or R04)" % (x,))

def norm_tool(name):
    n = (name or "").split("__")[-1]
    return n[4:] if n.startswith("jev_") else n

def input_hash(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

def _text_of(content):
    if isinstance(content, list): return "".join(x.get("text", "") for x in content if isinstance(x, dict) and x.get("type") == "text")
    return content if isinstance(content, str) else ""

def timeline(records):
    """Common event timeline of a transcript: every tool_use block and every tool_result block gets the next position (1-based, in record order). -> (use_pos, result_pos) dicts by tool_use_id.
    The same counting is used by discover.inventory and calls_from_records, so a write's position and a Jev call's use/result positions are comparable (R02 window check)."""
    use_pos, res_pos, n = {}, {}, 0
    for d in records:
        msg = d.get("message") if isinstance(d.get("message"), dict) else {}; c = msg.get("content")
        if d.get("type") == "assistant" and isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "tool_use": n += 1; use_pos.setdefault(b["id"], n)
        elif d.get("type") == "user" and isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "tool_result": n += 1; res_pos.setdefault(b.get("tool_use_id"), n)
    return use_pos, res_pos

def calls_from_records(records):
    """records: parsed JSONL objects (session log or stream-json). Returns every mcp__jev__* tool_use correlated BY ID with its tool_result, in call order.
    `seq` = position among ALL tool_use blocks (the same index tests/common.py:calls_of gives); `pos`/`result_pos` = positions of the call and of its result on the common
    tool_use/tool_result timeline (see `timeline`), None for a missing result."""
    uses, res, order, seq = {}, {}, [], 0
    use_pos, res_pos = timeline(records)
    for d in records:
        t = d.get("type"); msg = d.get("message") if isinstance(d.get("message"), dict) else {}
        c = msg.get("content")
        if t == "assistant" and isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    uses[b["id"]] = dict(name=b.get("name", ""), input=b.get("input"), seq=seq, uuid=d.get("uuid")); order.append(b["id"]); seq += 1
        elif t == "user" and isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "tool_result": res[b.get("tool_use_id")] = dict(content=b.get("content"), is_error=bool(b.get("is_error")))
    out = []
    for i in order:
        u = uses[i]
        if not u["name"].startswith(PREFIX): continue
        r = res.get(i); raw = _text_of(r["content"]) if r else ""
        try: parsed = json.loads(raw) if raw else None
        except Exception: parsed = None
        out.append(dict(tool_use_id=i, tool=norm_tool(u["name"]), seq=u["seq"], pos=use_pos.get(i), result_pos=res_pos.get(i), uuid=u["uuid"], input=u["input"] if isinstance(u["input"], dict) else {}, input_hash=input_hash(u["input"]),
                        has_result=r is not None, is_error=(r is None) or r["is_error"] or not isinstance(parsed, dict), parsed=parsed if isinstance(parsed, dict) else None,
                        raw_sha256=hashlib.sha256(raw.encode()).hexdigest() if raw else None))
    return out

_SNAP = None   # while a snapshot scope is open: canonical path -> (sha256 of its bytes, the bytes) -- one capture per file, shared by its digest and by every reader

class file_snapshots:
    """`with file_snapshots():` -> every file read through `read_snapshot` is captured once inside the block (nested blocks share the outer capture); the capture lives only as long as the outermost block."""
    def __enter__(self):
        global _SNAP; self.prev = _SNAP
        if _SNAP is None: _SNAP = {}
        return self
    def __exit__(self, *a):
        global _SNAP; _SNAP = self.prev

def read_snapshot(path):
    """-> (canonical path, sha256 hex of the bytes, the bytes). Inside a snapshot scope the file is read once and every caller gets that capture (so a digest and the records parsed from it always describe the same bytes);
    outside a scope each call reads the file as it is. A failed read (OSError) is never captured."""
    real = os.path.realpath(path)
    if _SNAP is not None and real in _SNAP: return (real,) + _SNAP[real]
    with open(real, "rb") as f: data = f.read()
    sha = hashlib.sha256(data).hexdigest()
    if _SNAP is not None: _SNAP[real] = (sha, data)
    return real, sha, data

def text_lines(data):
    """The lines of captured bytes exactly as `open(path, encoding="utf-8", errors="replace")` yields them (universal newlines)."""
    return io.TextIOWrapper(io.BytesIO(data), encoding="utf-8", errors="replace")

def load_calls(path):
    if O.is_selector(path): return calls_from_records(O.records(path))   # explicit OpenCode selector (see opencode.py)
    recs = []
    for l in text_lines(read_snapshot(path)[2]):
        l = l.strip()
        if l.startswith("{"):
            try: recs.append(json.loads(l))
            except Exception: pass
    return calls_from_records(recs)

def field_state(container, key):
    """What a recorded response says about one protocol field: "absent" (no such key, or no object to hold it), "null" (the key is there with null), "finite" (a finite number, not a bool) or "invalid" (anything else:
    bool, string, NaN, infinity, list, ...). Kept apart from the normalized values so that a structural incompatibility (`absent`) is not confused with a present-but-unusable value."""
    if not isinstance(container, dict) or key not in container: return "absent"
    v = container[key]
    return "null" if v is None else ("finite" if _finite(v) else "invalid")

def _entry(index, key, r, kind):
    verdict = next((r[k] for k in VERDICT_KEYS if isinstance(r.get(k), str)), None)
    action = r.get("action", r.get("decision"))
    return dict(index=index, key=key, verdict=verdict, confidence=r.get("confidence") if is_probability(r.get("confidence")) else None, action=action,
                same_subject=r.get("same_subject") if _num(r.get("same_subject")) else None, kind=kind, same_subject_state=field_state(r, "same_subject"))

def _claims_like(call):
    """jev_verify / jev_gate: results[k] must carry claim == input.claims[k] (exact string). The results sit at the top level (`flat`, jev_verify and the historical gate form) or, for jev_gate ONLY, under
    `verification.results` (`nested`, the envelope the inspected gate implementations return); a gate response holding both is `ambiguous` and, like any other form, an unknown shape. No recursive search."""
    claims, shape, p = call["input"].get("claims"), envelope_shape(call), call["parsed"] or {}
    res = p["verification"]["results"] if shape == "nested" else (p.get("results") if shape == "flat" else None)
    if shape == "ambiguous": return [], "unknown response shape (gate: results and verification.results both present)"
    if not isinstance(claims, list) or not isinstance(res, list): return [], "unknown response shape (claims/results)"
    out = []
    for k, r in enumerate(res):
        if not isinstance(r, dict): continue
        ok = k < len(claims) and isinstance(claims[k], str) and r.get("claim") == claims[k]
        e = _entry(k, claims[k] if k < len(claims) and isinstance(claims[k], str) else None, r, "claim")
        if not ok: e["mismatch"] = "result %d does not carry the claim at input.claims[%d]" % (k, k)
        out.append(e)
    return out, None

def compare_key(inp, aspect_index=None):
    asp = inp.get("aspects") if isinstance(inp.get("aspects"), list) else []
    sel = asp if aspect_index is None else asp[aspect_index:aspect_index + 1]
    return json.dumps({"passage_a": inp.get("passage_a"), "passage_b": inp.get("passage_b"), "aspects": sel}, sort_keys=True, ensure_ascii=False)

def _compare(call):
    """jev_compare: result_index 0 = `overall` (key = both passages + all aspects); result_index 1+k = aspects[k] whose `aspect` equals input.aspects[k]."""
    inp, p = call["input"], call["parsed"] or {}
    if not isinstance(inp.get("passage_a"), str) or not isinstance(inp.get("passage_b"), str) or not isinstance(p.get("overall"), dict): return [], "unknown response shape (compare)"
    out = [_entry(0, compare_key(inp), p["overall"], "overall")]
    asp, res = inp.get("aspects") if isinstance(inp.get("aspects"), list) else [], p.get("aspects") if isinstance(p.get("aspects"), list) else []
    for k, r in enumerate(res):
        if not isinstance(r, dict): continue
        ok = k < len(asp) and isinstance(asp[k], str) and r.get("aspect") == asp[k]
        e = _entry(1 + k, compare_key(inp, k) if k < len(asp) else None, r, "aspect")
        if not ok: e["mismatch"] = "result aspect %d does not equal input.aspects[%d]" % (k, k)
        out.append(e)
    return out, None

def _by_id(field):
    def f(call):
        """jev_classify / jev_extract: ONLY the form results[k].id == input.<items|fields>[k].id is accepted; any other shape stays unbound.
        jev_classify (observed live, R05): results[k] = {id, classification, probabilities, confidence, margin, top_probability, decision: auto|review}; `classification` is the verdict (VERDICT_KEYS)
        and `decision` the action (_entry). The jev_extract form is not observed: it is the strict rule, not a measured contract."""
        items, res = call["input"].get(field), (call["parsed"] or {}).get("results")
        if not isinstance(items, list) or not isinstance(res, list): return [], "unknown response shape (%s/results)" % field
        out = []
        for k, r in enumerate(res):
            if not isinstance(r, dict): continue
            iid = items[k].get("id") if k < len(items) and isinstance(items[k], dict) else None
            e = _entry(k, iid if isinstance(iid, str) else None, r, field)
            if not (isinstance(iid, str) and r.get("id") == iid): e["mismatch"] = "result %d id does not equal input.%s[%d].id" % (k, field, k)
            out.append(e)
        return out, None
    return f

VERDICT_DOMAIN = {"verify": ("verified", "contradicted", "unsupported"), "gate": ("verified", "contradicted", "unsupported")}
# The successful verdicts of the tools whose contract is a closed verdict domain (reference/jev-and-audit.md, "Jev tool contracts"), applied PER TOOL: a bound result whose verdict is outside its tool's contract is recorded faithfully (bound, with
# `contract_reason`) but never resolved. jev_verify also returns `unknown` (an invalid_response shape) and a stray label such as `supported` is not a verdict at all: neither can resolve a check, hold a gate or PASS.
# NO global three-verdict rule: jev_compare relations (same_fact, contradicts, different_facts) and jev_classify / jev_extract labels (the caller's own class or field ids: `out_of_scope`, `handoff`, ...) keep their own contracts.

def verdict_contract(tool, verdict):
    """-> None when `verdict` is a verdict of the tool's contract (or the tool has no closed verdict domain), else the reason it is not."""
    dom = VERDICT_DOMAIN.get(tool)
    if dom is None or (isinstance(verdict, str) and verdict.lower() in dom): return None
    return "verdict %r is outside the jev_%s contract (%s)" % (verdict, tool, " | ".join(dom))

ADAPTERS = {"verify": _claims_like, "gate": _claims_like, "compare": _compare, "classify": _by_id("items"), "extract": _by_id("fields")}

def results_of(call):
    """-> (entries, reason). entries: per result {index, key, verdict, confidence, action, same_subject, [mismatch]}; reason set when no result can be identified."""
    if call["is_error"] or call["parsed"] is None: return [], "call has no usable result (error/missing/unparseable)"
    ad = ADAPTERS.get(call["tool"])
    if not ad: return [], "no adapter for tool %s" % call["tool"]
    ents, why = ad(call); sa = field_state(call["parsed"], "subject_at")   # subject_at lives at the top level of the response
    for e in ents: e["subject_at_state"] = sa
    return ents, why

def envelope_shape(call):
    """The explicit envelope shape of a recorded jev_verify / jev_gate response: "flat" (top-level `results`), "nested" (jev_gate only: `verification.results`), "ambiguous" (a gate response with both) or "unknown"."""
    p = call.get("parsed")
    if call.get("is_error") or not isinstance(p, dict): return "unknown"
    flat = isinstance(p.get("results"), list)
    if call.get("tool") == "gate":
        v = p.get("verification"); nested = isinstance(v, dict) and isinstance(v.get("results"), list)
        if "results" in p and isinstance(v, dict) and "results" in v: return "ambiguous"   # both KEYS present, whatever their value types: never choose one location
        if nested: return "nested"
    return "flat" if flat else "unknown"

def gate_summary(call):
    """A separate OBSERVATION of one recorded jev_gate response: the patch review, the aggregate action and the claim results apart. -> dict(tool, shape, claims, results, matched, strict_resolved, review{action, status, reasons},
    aggregate{action, truncated, reasons}, verification_action, holds, reasons). `holds` is true only when ALL hold: a known unambiguous shape; a nonempty exact claim/result correspondence (as many results as claims, each carrying
    its claim at its index); every claim `verified` > 0.95 under the strict auxiliary conditions (action auto, finite same_subject >= finite subject_at); a usable review (an object with action `auto`, no
    `incomplete_context`, and no `status` or `ok`: the real contract omits `status` on success and marks a failed one `invalid_response`); `truncated` false; aggregate `action` auto; and no observed non-auto verification action.
    Passing claims never imply an accepted patch. It certifies no version binding and changes no report status."""
    p = call.get("parsed") if isinstance(call.get("parsed"), dict) else {}
    out = dict(tool=call.get("tool"), shape=envelope_shape(call) if call.get("tool") == "gate" else "unknown", claims=None, results=None, matched=0, strict_resolved=0, review=dict(action=None, status=None, reasons=[]),
               aggregate=dict(action=None, truncated=None, reasons=[]), verification_action=None, holds=False, reasons=[])
    why = out["reasons"]
    if call.get("tool") != "gate": why.append("not a jev_gate call"); return out
    if out["shape"] not in ("flat", "nested"): why.append("unknown or ambiguous response shape"); return out
    claims, ents = call["input"].get("claims") if isinstance(call.get("input"), dict) else None, results_of(call)[0]
    res = p["verification"]["results"] if out["shape"] == "nested" else p["results"]
    out["claims"], out["results"] = (len(claims) if isinstance(claims, list) else None), len(res)
    out["matched"] = sum(1 for e in ents if not e.get("mismatch"))
    out["strict_resolved"] = sum(1 for e in ents if not e.get("mismatch") and str(e.get("verdict")).lower() == "verified" and strict_pass(e.get("confidence")) and aux_ok(e, call, True))
    rv = p.get("review") if isinstance(p.get("review"), dict) else None
    if rv is not None: out["review"] = dict(action=rv.get("action"), status=rv.get("status"), reasons=list(rv.get("reason_codes")) if isinstance(rv.get("reason_codes"), list) else [])
    v = p.get("verification") if isinstance(p.get("verification"), dict) else {}
    out["aggregate"] = dict(action=p.get("action"), truncated=p.get("truncated"), reasons=list(p.get("reason_codes")) if isinstance(p.get("reason_codes"), list) else [])
    out["verification_action"] = v.get("action")   # an observed verification action is kept in either shape
    if not out["claims"] or out["results"] != out["claims"] or len(ents) != out["results"] or out["matched"] != out["results"]: why.append("claim/result correspondence is not complete and exact")
    if out["strict_resolved"] != out["claims"]: why.append("not every claim is verified > 0.95 under the strict auxiliary conditions")
    r = out["review"]
    if rv is None or r["action"] not in ("auto", "review", "escalate"): why.append("no usable patch review")
    else:
        if r["action"] != "auto": why.append("patch review action is %s" % r["action"])
        if r["status"] not in (None, "ok"): why.append("patch review status is %s" % r["status"])
        if "incomplete_context" in r["reasons"]: why.append("patch review context is incomplete")
    if out["aggregate"]["truncated"] is not False: why.append("response is truncated or not demonstrably complete")
    if out["aggregate"]["action"] != "auto": why.append("aggregate action is %s" % out["aggregate"]["action"])
    if out["verification_action"] not in (None, "auto"): why.append("verification action is %s" % out["verification_action"])
    out["holds"] = not why
    return out

def capabilities(calls):
    """What the RECORDED verify / gate responses show, one row per (tool, explicit envelope shape, state of same_subject, state of subject_at) with `results` (result count) and `calls` (distinct calls): states are
    absent / null / invalid / finite (`field_state`); an unknown shape is a row with null states and 0 results. Each response stands on its own fields: nothing is attributed from a package version, grouped or voted, and an earlier
    response never decides a later one. Error and unparseable calls are not observations. -> list sorted by (tool, shape, states)."""
    obs = {}
    for c in calls:
        if c.get("tool") not in ("verify", "gate") or c.get("is_error") or c.get("parsed") is None: continue
        shape = envelope_shape(c); ents = results_of(c)[0] if shape != "unknown" else []
        for key in ({(c["tool"], shape, e["same_subject_state"], e["subject_at_state"]) for e in ents} or {(c["tool"], shape, None, None)}):
            o = obs.setdefault(key, [0, set()]); o[1].add(c["tool_use_id"])
            if key[2] is not None: o[0] += sum(1 for e in ents if (e["same_subject_state"], e["subject_at_state"]) == key[2:])
    return [dict(tool=k[0], shape=k[1], same_subject=k[2], subject_at=k[3], results=v[0], calls=len(v[1])) for k, v in sorted(obs.items(), key=lambda kv: tuple(x or "" for x in kv[0]))]

def _finite(v): return _num(v) and (isinstance(v, int) or math.isfinite(v))   # an int is always finite (and math.isfinite overflows beyond float range)

def aux_ok(entry, call, strict=False):
    """Jev auxiliary conditions of the REAL result: action must be auto (absent = auto), and same_subject >= subject_at. Legacy rules (strict=False, unchanged from audit v3): the comparison is made only when both
    numbers exist, and an `unsupported` verdict is accepted whatever its same_subject (executor assumption of D4, a documented limit). R03 (strict=True): that exception is CLOSED (no verdict has a free pass) and for
    jev_verify/jev_gate both same_subject and subject_at must be finite numbers (not bool) with same_subject >= subject_at, otherwise the check is unresolved; other tools keep the comparison-when-both-exist rule."""
    if entry.get("action") not in (None, "auto"): return False
    sa, ss = (call["parsed"] or {}).get("subject_at"), entry.get("same_subject")
    if strict:
        if call.get("tool") in ("verify", "gate"): return bool(_finite(ss) and _finite(sa) and ss >= sa)
        return not (_num(ss) and _num(sa) and ss < sa)
    if str(entry.get("verdict")).lower() == "unsupported": return True
    return not (_num(ss) and _num(sa) and ss < sa)

def _norm(s): return " ".join(str(s or "").lower().split())

def call_evidence(call):
    """Normalized evidence texts the REAL call was given (verify/gate `evidence[].text`, compare passages, gate diff): what a finding's source quote must be found in."""
    inp, out = call.get("input") or {}, []
    ev = inp.get("evidence")
    for x in ev if isinstance(ev, list) else ([ev] if isinstance(ev, (str, dict)) else []): out.append(_norm(x.get("text") if isinstance(x, dict) else x))   # jev_verify accepts one string, one item or a list of items
    for k in ("passage_a", "passage_b", "diff"):
        if isinstance(inp.get(k), str): out.append(_norm(inp[k]))
    return [t for t in out if t]

def quote_evidence_raw(call):
    """The texts a finding's `quote_source` may be found in, each ENTRY unmodified and apart: the evidence entries of verify/gate (`omissions.evidence_raw`), then compare `passage_a` / `passage_b` and the gate `diff`. Case, whitespace
    and newlines are exactly as the call was given them; a quote must lie inside ONE entry, never across two."""
    import omissions as O
    inp = call.get("input") or {}
    return O.evidence_raw(inp) + [inp[k] for k in ("passage_a", "passage_b", "diff") if isinstance(inp.get(k), str)]

def call_evidence_raw(call):
    """The evidence texts the REAL call was given, in order and unmodified (R03: the absence call must have received the canonical material)."""
    import omissions as O
    return O.evidence_raw(call.get("input"))

def check_id(c):
    """The id of a check when it is a string, else None: an id that is not a string (a list, an object) is never hashed or used as a key, whatever the report holds there."""
    i = c.get("id") if isinstance(c, dict) else None
    return i if isinstance(i, str) else None

def duplicate_ids(checks):
    seen, dup = set(), set()
    for c in checks:
        i = check_id(c)
        if i in seen: dup.add(i)
        seen.add(i)
    return dup

def bind(checks, calls, strict_aux=False):
    """-> one dict per check, in order: {id, bound, reason, tool_use_id, result_index, input_hash, aux_ok, resolved}. A `binding` declared inside the report is never read.
    `strict_aux` is the contract selector (`contract_of`): False/"R02" = legacy auxiliary rules, True/"R03"/"R04" = strict. The R04 exception for the absence half of a valid omission pair is NOT applied here (the binding
    records the real result faithfully): `finalize` applies it after the version identity and the material are attached."""
    by_id = {c["tool_use_id"]: c for c in calls}; used = set(); dup = duplicate_ids(checks); out = []; ctr = contract_of(strict_aux); strict_aux = ctr != "R02"
    for c in checks:
        c = c if isinstance(c, dict) else {}
        row = dict(id=check_id(c), bound=False, reason=None, tool_use_id=None, result_index=None, input_hash=None, aux_ok=False, resolved=False, contract_reason=None)
        def no(why): row["reason"] = why; out.append(row)
        ref = c.get("jev_ref")
        if check_id(c) in dup: no("duplicate check id"); continue
        if not isinstance(ref, dict): no("no jev_ref"); continue
        tid, ri, key = ref.get("tool_use_id"), ref.get("result_index"), ref.get("key")
        if not isinstance(tid, str) or not isinstance(ri, int) or isinstance(ri, bool) or ri < 0 or not isinstance(key, str): no("malformed jev_ref"); continue
        call = by_id.get(tid)
        if call is None: no("tool_use_id not found among the session's Jev calls"); continue
        if norm_tool(c.get("tool")) != call["tool"]: no("check.tool %s != call tool %s" % (c.get("tool"), call["tool"])); continue
        ents, why = results_of(call)
        if why: no(why); continue
        e = next((x for x in ents if x["index"] == ri), None)
        if e is None: no("result_index %d does not exist" % ri); continue
        if e.get("mismatch"): no(e["mismatch"]); continue
        if e["key"] != key: no("key differs from the input element at that index"); continue
        if (tid, ri) in used: no("(call, result) already bound to another check"); continue
        conf = c.get("confidence")
        if not _num(conf) or e["confidence"] is None or conf != e["confidence"]: no("confidence differs from the real result"); continue
        if not isinstance(c.get("verdict"), str) or e["verdict"] != c["verdict"]: no("verdict differs from the real result"); continue
        used.add((tid, ri)); a = aux_ok(e, call, strict_aux); outside = verdict_contract(call["tool"], e["verdict"])
        # R04: an `unsupported` result is never resolved here whatever its same_subject; only `finalize` can resolve it, and only as the ABSENCE half of a fully valid pair
        r04_unsup = ctr == "R04" and str(e["verdict"]).lower() == "unsupported"
        row.update(bound=True, reason="bound", tool_use_id=tid, result_index=ri, input_hash=call["input_hash"], aux_ok=a, resolved=bool(a and strict_pass(e["confidence"]) and not c.get("error") and not r04_unsup and outside is None), contract_reason=outside,
                   real_verdict=e["verdict"], real_confidence=e["confidence"], real_action=e.get("action"), real_same_subject=e.get("same_subject"), tool=call["tool"], check_error=bool(c.get("error")), claim_key=key, call_evidence=call_evidence(call), call_evidence_raw=call_evidence_raw(call), call_quote_evidence=quote_evidence_raw(call))
        out.append(row)
    return out

FACT_DEFECTS = {"wrong_fact", "stale_state", "dead_path"}   # need a real `contradicted` result
FINDING_TYPES = FACT_DEFECTS | {"lost_detail"}               # lost_detail needs a real `unsupported` result (absence probe)

def resolve_handoff_text(handoff, extra_paths=(), texts=()):
    """-> (text | None, provenance). The text of the handoff VERSION the report declares: a candidate file (the report's `handoff.path`, then `extra_paths`, e.g. a fixture copy; or an in-memory reconstructed text in `texts`) is accepted
    only if the sha256 of its bytes equals one of the report's declared `handoff.versions[].sha256`. A file with the same basename but another hash is NEVER substituted; no declared hash,
    no candidate or no match -> (None, reason): findings that depend on a handoff passage are then unsupported."""
    import hashlib
    h = handoff if isinstance(handoff, dict) else {}
    want = {v.get("sha256") for v in (h.get("versions") if isinstance(h.get("versions"), list) else []) if isinstance(v, dict) and isinstance(v.get("sha256"), str)}
    if not want: return None, "report declares no version sha256: exact handoff version not demonstrable"
    tried = []
    for tx in texts or []:   # in-memory reconstructed versions (e.g. replayed Write/Edit): same sha256 rule
        if isinstance(tx, str) and hashlib.sha256(tx.encode("utf-8")).hexdigest() in want: return tx, "sha256 %s verified on a reconstructed version" % hashlib.sha256(tx.encode("utf-8")).hexdigest()[:8]
    for cand in [h.get("path")] + list(extra_paths or []):
        if not isinstance(cand, str) or cand in tried: continue
        tried.append(cand)
        try:
            if not os.path.isfile(cand): continue
            raw = open(cand, "rb").read()
        except OSError: continue
        sha = hashlib.sha256(raw).hexdigest()
        if sha in want: return raw.decode("utf-8", errors="replace"), "sha256 %s verified on %s" % (sha[:8], cand)
    return None, "no readable file with a declared version sha256 (tried %d path(s)); handoff version not recoverable" % len(tried)

def validate_finding(f, bindings_by_id, handoff_text=None, omission_contract=False):
    """The ONE finding validator (audited_status, report writer, audit_run, audit_trigger, score). -> (confirmed: bool, reason: str).
    A finding is CONFIRMED only if ALL hold: structure valid; its check_id names a BOUND and RESOLVED check (aux ok, real confidence > 0.95); its own confidence is a number strictly > 0.95
    and EQUAL to the real result's confidence; the real verdict fits the declared defect (wrong_fact/stale_state/dead_path <- `contradicted`; lost_detail <- `unsupported`; `verified`
    can never confirm a defect); and it carries an explicit `claim` equal (whitespace-normalized) to the claim identified by the check's jev_ref, its quote_handoff is in the text of the handoff version the report declares (sha256-verified; unavailable text, or a missing passage, never confirms; lost_detail may lack quote_handoff), and its source quote is demonstrably part of the evidence the real call received (link finding <-> call; for lost_detail the quote must be in a bound, resolved `verified` source-side check of the same report, because the absence probe itself only receives the handoff). Otherwise it is UNSUPPORTED (never FAIL).
    The explicit `claim` equality IS verified; only the semantic relation between the claim and the quoted passages (quote_handoff / quote_source) beyond the contract is NOT verified: stated limit.
    R02: a binding that carries `version_text` (the sha256-verified snapshot of the version its check evaluates, shared/versions.py) validates quote_handoff against THAT text.
    R03 (`omission_contract`): a `lost_detail` is confirmed only by the explicit PAIR of `_validate_omission` (source support + explicit absence on the same write, canonical claims, exact material); `unsupported`
    never confirms any finding. R04: the same pair, whose absence half is the bare detail with a real `unsupported` (> 0.95, explicit action auto, same_subject not required, this half only); an `unsupported` on
    any other claim or finding type never confirms anything. `omission_contract` is the contract selector of `contract_of`."""
    if not isinstance(f, dict): return False, "malformed finding"
    ctr = contract_of(omission_contract)
    t, cid, conf = f.get("type"), f.get("check_id"), f.get("confidence")
    if not isinstance(t, str) or t not in FINDING_TYPES or not isinstance(cid, str): return False, "malformed finding (type/check_id)"
    for k in ("quote_handoff", "quote_source", "uuid"):
        if k in f and f[k] is not None and not isinstance(f[k], str): return False, "malformed finding (%s)" % k
    b = bindings_by_id.get(cid)
    if not b or not b.get("bound"): return False, "check unbound or missing"
    r04_lost = ctr == "R04" and t == "lost_detail"   # R04: the absence check of a pair is judged by the pair validator itself (explicit unsupported/auto conditions), not by the general auxiliary filter
    if not b.get("resolved") and not r04_lost: return False, "check bound but unresolved (<= 0.95, aux filter or error)"
    if not strict_pass(conf): return False, "finding confidence is not a number > 0.95"
    if conf != b.get("real_confidence"): return False, "finding confidence differs from the real result"
    want = "verified" if (t == "lost_detail" and ctr == "R03") else ("unsupported" if t == "lost_detail" else "contradicted")
    if not r04_lost and str(b.get("real_verdict")).lower() != want: return False, "real verdict %r cannot confirm %s (needs %r)" % (b.get("real_verdict"), t, want)
    # explicit finding <-> claim correspondence: the finding must carry the exact claim it is about (`claim`) and it must equal the claim of the call result named by the check's jev_ref
    # (whitespace-normalized equality only; no lexical overlap, no uniqueness, no fallback to check_id alone)
    if not isinstance(f.get("claim"), str) or not f["claim"].strip(): return False, "finding carries no explicit `claim`: link to the verified claim not demonstrable"
    if " ".join(f["claim"].split()) != " ".join(str(b.get("claim_key") or "").split()): return False, "finding `claim` differs from the claim identified by the check's jev_ref"
    if b.get("version_text") is not None: handoff_text = b["version_text"]   # R02: the snapshot of the check's own version (identity verified by versions.py)
    qh = f.get("quote_handoff"); has_qh = isinstance(qh, str) and bool(qh.strip())
    if t != "lost_detail" and not has_qh: return False, "finding depends on a handoff passage but has no quote_handoff"
    if has_qh:   # a passage-dependent finding needs the demonstrated handoff version: unavailable text or a missing passage never confirms (lost_detail may legitimately have no quote_handoff)
        if handoff_text is None: return False, "handoff version text not available/demonstrated (sha256): quote_handoff cannot be checked"
        if qh not in handoff_text: return False, "quote_handoff not found in the handoff text of the reported version"   # exact: case, whitespace and newlines as in the demonstrated version
    if ctr in ("R03", "R04") and t == "lost_detail": return _validate_omission(f, b, bindings_by_id, ctr)
    q = f.get("quote_source")   # exact (case, whitespace, newlines) and inside ONE raw entry of the evidence the call was given
    if not isinstance(q, str) or not q.strip(): return False, "no source quote to link the finding to the call's evidence"
    if t == "lost_detail":
        # the absence probe's evidence is the HANDOFF, so the source quote lives in the report's source-side probe: a bound, resolved `verified` check whose call evidence holds the quote
        if not any(x.get("resolved") and str(x.get("real_verdict")).lower() == "verified" and any(q in e for e in x.get("call_quote_evidence") or []) for x in bindings_by_id.values()):
            return False, "no bound, resolved `verified` source-side check whose call evidence contains the source quote"
    elif not any(q in e for e in b.get("call_quote_evidence") or []): return False, "source quote not found in the evidence given to the call"
    return True, "confirmed"

def _validate_omission(f, a, by, contract="R03"):
    """R03: the pair that confirms a lost_detail. `a` = the ABSENCE binding (finding.check_id); `by` = all bindings. Everything is re-derived (nothing declared by the model is believed):
    omission_ref {detail, source_check_id}; two DIFFERENT checks, both bound, resolved (strict aux), verdict `verified`, confidence > 0.95; the claims are exactly the canonical claims of that detail
    (shared/omissions.py); both checks evaluate the same write (write_tool_use_id + sha256 + evaluated_against validated by versions.py); the finding's source quote is EXACTLY inside one record of the eligible source of that version (strict prefix before the write for `prefix`; before the verification activity for `session_end`; Jev calls are never source) and the whole evidence of the source call equals that passage; the evidence given to the absence call equals the canonical material of the version (exact text, order, delimiters).
    contract R04: the ABSENCE half is instead a bound, non-error, verify check whose claim is the bare normalized detail, with the real verdict `unsupported`, a finite confidence > 0.95 and an EXPLICIT action `auto`; same_subject
    / subject_at are not required for that half (this is the only place the exception exists: the general auxiliary filter is unchanged and an `unsupported` anywhere else confirms nothing).
    -> (confirmed, reason)."""
    import omissions as O
    contract = contract_of(contract)
    if contract == "R02": return False, "the R02 evaluator has no omission pair"
    ref = f.get("omission_ref")
    if not isinstance(ref, dict) or not isinstance(ref.get("detail"), str) or not O.wsnorm(ref["detail"]) or not isinstance(ref.get("source_check_id"), str): return False, "lost_detail without a valid omission_ref {detail, source_check_id}"
    if O.redaction_dependent(ref["detail"]): return False, "the detail holds a secret or a redaction marker: a redaction-dependent detail cannot be certified (what is hidden is not demonstrated)"
    s = by.get(ref["source_check_id"])
    if not s or not s.get("bound"): return False, "omission_ref.source_check_id names no bound check"
    if s["id"] == a["id"]: return False, "the same check cannot be both the source support and the absence probe"
    if not s.get("resolved") or str(s.get("real_verdict")).lower() != "verified" or not strict_pass(s.get("real_confidence")): return False, "source check is not resolved/verified with confidence > 0.95 (strict auxiliary conditions)"
    sc, ac = O.claims(ref["detail"], contract)
    if O.wsnorm(s.get("claim_key")) != sc: return False, "source check claim is not the canonical source claim of the detail"
    if O.wsnorm(a.get("claim_key")) != ac: return False, "absence check claim is not the canonical absence claim of the detail"
    if contract == "R04":
        if a.get("tool") != "verify" or a.get("check_error"): return False, "the absence check is not an error-free jev_verify check"
        if str(a.get("real_verdict")).lower() != "unsupported": return False, "the absence check verdict is %r: only `unsupported` (the note does not state the detail) can confirm an omission (verified/contradicted: the detail is present or contradicted)" % a.get("real_verdict")
        if not strict_pass(a.get("real_confidence")): return False, "the absence check confidence is not a finite number in [0, 1] and > 0.95"
        if a.get("real_action") != "auto": return False, "the absence check action is not explicitly `auto`"
    elif not a.get("resolved") or str(a.get("real_verdict")).lower() != "verified" or not strict_pass(a.get("real_confidence")): return False, "absence check is not resolved/verified with confidence > 0.95 (strict auxiliary conditions)"
    va, vs_ = a.get("version"), s.get("version")
    if not (va and va.get("ok") and vs_ and vs_.get("ok")): return False, "version identity of the pair is not demonstrated (version_ref validated by versions.py is required on both checks)"
    if (va["write_tool_use_id"], va["sha256"], va.get("evaluated_against"), va.get("run")) != (vs_["write_tool_use_id"], vs_["sha256"], vs_.get("evaluated_against"), vs_.get("run")): return False, "the two checks do not evaluate the same write, hash, evaluated_against and verification run"
    q = f.get("quote_source")
    if not isinstance(q, str) or not q.strip(): return False, "no exact source quote"
    if O.redaction_dependent(q): return False, "the source quote holds a secret or a redaction marker: a redaction-dependent quote cannot be certified (what is hidden is not demonstrated)"
    passage = O.passage_of(s.get("eligible_blocks"), q) if isinstance(s.get("eligible_blocks"), list) else None
    if passage is None: return False, "the source quote is not exactly (case, whitespace, newlines) inside ONE record of the eligible source of the evaluated version (before the write for `prefix`; before the verification activity for `session_end`; Jev calls are never source)"
    if O.redaction_dependent(passage): return False, "the eligible passage that holds the quote contains redacted content: what it hides is not demonstrated"
    if not O.passage_matches(s.get("call_evidence_raw") or [], passage): return False, "the evidence of the source call is not exactly the eligible passage that holds the quote (no extra or missing content)"
    if not O.material_matches(a.get("call_evidence_raw") or [], a.get("omission_material")): return False, "the evidence of the absence call is not the complete canonical material of the version (%s)" % (a.get("omission_material_reason") or "text, order or delimiters differ")
    return True, "confirmed"

def finalize(bindings, findings, handoff_text=None, omission_contract=False):
    """R04 exception, applied AFTER the version identity and the material are attached (versions.attach): the ABSENCE check of a fully valid omission pair (the finding passes `validate_finding`, i.e. a valid linked
    SOURCE check on the same write, canonical claims, exact material, a real `unsupported` > 0.95 with explicit action auto) is marked resolved although its same_subject is below subject_at. Nothing else is touched:
    no other check, finding type or verdict gets the exception; idempotent; the other contracts return the bindings unchanged. -> bindings (new list)."""
    if contract_of(omission_contract) != "R04": return bindings
    by = {b["id"]: b for b in bindings}; out = dict(by)
    for f in findings if isinstance(findings, (list, tuple)) else []:
        if isinstance(f, dict) and f.get("type") == "lost_detail" and isinstance(f.get("check_id"), str) and validate_finding(f, by, handoff_text, "R04")[0]:
            a = by[f["check_id"]]; out[a["id"]] = dict(a, resolved=True, aux_ok=True, absence_exception=True)
    return [out.get(b["id"], b) if by.get(b["id"]) is b else b for b in bindings]

def audited_status(checks, bindings, findings, declared_unresolved=(), handoff_text=None, omission_contract=False):
    """The BINDING-LEVEL status evaluator, NON-CERTIFYING (`certifying` False in the result): it judges only the checks, bindings and findings it is given. A PASS from here certifies nothing about the review behind the report;
    the report writer, the delivery gate and the per-version evaluation never use it alone: they pass its result through audit.certify (the mandatory full-report audit), which turns a PASS without a complete audit into UNRESOLVED.
    FAIL only if at least one finding passes validate_finding. Otherwise UNRESOLVED when any check is unbound/unresolved/duplicated, a declared unresolved entry exists, a finding is
    unsupported, or there are zero checks. Otherwise PASS (never with an unbound check)."""
    bindings = finalize(bindings, findings, handoff_text, omission_contract)
    by = {b["id"]: b for b in bindings}; resolved = {b["id"] for b in bindings if b["resolved"]}
    verdicts = [validate_finding(f, by, handoff_text, omission_contract) for f in findings]
    valid = [f for f, (ok, _) in zip(findings, verdicts) if ok]; invalid_findings = [f for f, (ok, _) in zip(findings, verdicts) if not ok]
    unbound = [b["id"] for b in bindings if not b["bound"]]
    unresolved_checks = [b["id"] for b in bindings if b["bound"] and not b["resolved"]]
    reasons = []
    if valid: status = "FAIL"
    else:
        if unbound: reasons.append("unbound checks: %d" % len(unbound))
        if unresolved_checks: reasons.append("bound but unresolved (<= 0.95, aux filter or error): %d" % len(unresolved_checks))
        outside = [b["id"] for b in bindings if b.get("bound") and b.get("contract_reason")]
        if outside: reasons.append("verdict outside the tool contract: %d" % len(outside))
        if declared_unresolved: reasons.append("declared unresolved entries: %d" % len(declared_unresolved))
        if invalid_findings: reasons.append("unsupported findings: %d" % len(invalid_findings))
        if not checks: reasons.append("zero checks")
        status = "UNRESOLVED" if reasons else "PASS"
    return dict(status=status, certifying=False, reasons=reasons, valid_findings=len(valid), invalid_findings=len(invalid_findings), finding_verdicts=[dict(confirmed=ok, reason=r) for ok, r in verdicts],
                checks=len(checks), bound=len(bindings) - len(unbound), resolved=len(resolved), unbound=len(unbound))

def listing(calls):
    out = []
    for c in calls:
        ents, why = results_of(c)
        out.append(dict(tool_use_id=c["tool_use_id"], tool=c["tool"], is_error=c["is_error"], input_hash=c["input_hash"], note=why,
                        results=[{k: e[k] for k in ("index", "key", "verdict", "confidence", "action", "same_subject")} | ({"mismatch": e["mismatch"]} if e.get("mismatch") else {}) for e in ents]))
    return out

def build_parser():
    ap = argparse.ArgumentParser(); sub = ap.add_subparsers(dest="cmd", required=True)
    l = sub.add_parser("list"); l.add_argument("--session"); l.add_argument("--cwd")
    return ap

def main():
    a = build_parser().parse_args()
    import discover as D
    sp, how, amb = D.resolve_session_info(a.session, a.cwd)
    if amb: print(json.dumps({"error": "current session not demonstrated: several recently modified sessions; pass --session ID or PATH.jsonl", "resolution": how})); return 3
    if not sp or not D.source_exists(sp): print(json.dumps({"error": "session not found", "arg": a.session})); return 2
    print(json.dumps(dict(session=sp, resolution=how, calls=listing(load_calls(sp))), indent=1, ensure_ascii=False)); return 0

if __name__ == "__main__": sys.exit(main())
