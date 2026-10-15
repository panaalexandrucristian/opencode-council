#!/usr/bin/env python3
"""Structural validation of a handoff-verify report against scripts/report.schema.json (stdlib only; no network, no Jev call). ONE validator for the report writer (report.bind_report: before any field of a model-written report
is touched), the delivery gate (versions.gate: before it reads the report) and the audit (audit.assess: the structure of the report is part of the audit), so a malformed report is judged the same way everywhere.
It implements exactly the keywords the schema file uses: type (a name or a list of names; `integer` and `number` never accept a bool, `number` never a NaN or an infinity), const, enum, required, properties, additionalProperties
(a schema), items, minItems, maxItems, uniqueItems, minimum, maximum, pattern and a local $ref ("#/$defs/name"). A keyword it does not know is reported, never ignored. Unknown properties are allowed (the schema does not forbid them).
-> list of problems "path: what is wrong" (empty = structurally valid); it never raises on any JSON-like value."""
import json, math, os, re

SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "report.schema.json")
KNOWN = {"$schema", "$defs", "$ref", "title", "description", "type", "const", "enum", "required", "properties", "additionalProperties", "items", "minItems", "maxItems", "uniqueItems", "minimum", "maximum", "pattern"}
_SCHEMA = None

def schema():
    global _SCHEMA
    if _SCHEMA is None:
        with open(SCHEMA_PATH, encoding="utf-8") as f: _SCHEMA = json.load(f)
    return _SCHEMA

def _is_type(v, t):
    if t == "object": return isinstance(v, dict)
    if t == "array": return isinstance(v, list)
    if t == "string": return isinstance(v, str)
    if t == "boolean": return isinstance(v, bool)
    if t == "null": return v is None
    if t == "integer": return isinstance(v, int) and not isinstance(v, bool)
    if t == "number": return isinstance(v, (int, float)) and not isinstance(v, bool) and not (isinstance(v, float) and (math.isnan(v) or math.isinf(v)))
    return False

def _same(a, b):
    """JSON equality that does not confuse True with 1 or 1.0 with True."""
    if isinstance(a, bool) or isinstance(b, bool): return isinstance(a, bool) and isinstance(b, bool) and a == b
    return a == b

def _canon(v): return json.dumps(v, sort_keys=True, ensure_ascii=False, default=str)

def validate(value, node=None, root=None, path="$", out=None, depth=0):
    """Validate `value` against the schema node `node` (the whole report schema by default). -> list of problems."""
    out = [] if out is None else out
    root = root if root is not None else schema(); node = root if node is None else node
    if depth > 40: out.append("%s: nested too deeply" % path); return out
    if not isinstance(node, dict): return out
    for kw in node:
        if kw not in KNOWN: out.append("%s: the schema keyword %r is not supported by the validator" % (path, kw))
    if "$ref" in node:
        ref = node["$ref"]; target = root
        for part in (ref[2:].split("/") if isinstance(ref, str) and ref.startswith("#/") else [None]):
            target = target.get(part) if isinstance(target, dict) else None
        if not isinstance(target, dict): out.append("%s: unresolvable $ref %r" % (path, ref)); return out
        return validate(value, target, root, path, out, depth + 1)
    t = node.get("type")
    if t is not None:
        names = t if isinstance(t, list) else [t]
        if not any(_is_type(value, n) for n in names): out.append("%s: expected %s, got %s" % (path, " or ".join(map(str, names)), type(value).__name__ if value is not None else "null")); return out
    if "const" in node and not _same(value, node["const"]): out.append("%s: must be %s" % (path, json.dumps(node["const"])))
    if "enum" in node and not any(_same(value, e) for e in node["enum"]): out.append("%s: must be one of %s" % (path, ", ".join(json.dumps(e) for e in node["enum"])))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in node and value < node["minimum"]: out.append("%s: below the minimum %s" % (path, node["minimum"]))
        if "maximum" in node and value > node["maximum"]: out.append("%s: above the maximum %s" % (path, node["maximum"]))
    if isinstance(value, str) and "pattern" in node and not re.search(node["pattern"], value): out.append("%s: does not match %s" % (path, node["pattern"]))
    if isinstance(value, dict):
        for r in node.get("required", []):
            if r not in value: out.append("%s: required property %r is missing" % (path, r))
        props = node.get("properties", {}); extra = node.get("additionalProperties")
        for k, v in value.items():
            if k in props: validate(v, props[k], root, "%s.%s" % (path, k), out, depth + 1)
            elif isinstance(extra, dict): validate(v, extra, root, "%s.%s" % (path, k), out, depth + 1)
    if isinstance(value, list):
        if "minItems" in node and len(value) < node["minItems"]: out.append("%s: fewer than %d items" % (path, node["minItems"]))
        if "maxItems" in node and len(value) > node["maxItems"]: out.append("%s: more than %d items" % (path, node["maxItems"]))
        if node.get("uniqueItems") and len({_canon(x) for x in value}) != len(value): out.append("%s: items are not unique" % path)
        if isinstance(node.get("items"), dict):
            for i, v in enumerate(value): validate(v, node["items"], root, "%s[%d]" % (path, i), out, depth + 1)
    return out

# The properties the REPORT WRITER owns: it sets them itself (report.bind_report), so a model-written report is not required to carry them and whatever it holds there is replaced (never carried into the output).
WRITER_OWNED = ("status", "status_claimed", "binding_summary", "scope_audit", "delivery", "jev_ref_version", "omission_contract", "evidence_requests", "blockers")
# The properties of the records of a list that the writer also sets on every record it emits (a binding, an advice, the per-version summary): not judged on a model-written record either.
RECORD_WRITER_OWNED = dict(checks=("binding", "advice"), versions=("audited_status", "binding_summary", "evaluations", "attributed_write", "attribution"))

def validate_report(doc, writer_input=False):
    """-> problems of a report (empty = structurally valid). With `writer_input` the writer-owned properties (WRITER_OWNED, and per record RECORD_WRITER_OWNED: a check's binding / advice, a version row's summary fields) are not judged: they are the writer's to set and nothing the author put there is carried into the report (an author's `status` that is not a
    status is ignored by the writer, never kept as `status_claimed`). Everything else, including `schema_version` (the writer stamps v1 before it validates), is judged exactly as the schema says."""
    if not isinstance(doc, dict): return ["$: the report is not an object"]
    d = doc
    if writer_input:
        d = {k: v for k, v in doc.items() if k not in WRITER_OWNED}; d["status"] = "UNRESOLVED"
        def bare(records, names): return [({k: v for k, v in r.items() if k not in names} if isinstance(r, dict) else r) for r in records] if isinstance(records, list) else records
        if "checks" in d: d["checks"] = bare(d["checks"], RECORD_WRITER_OWNED["checks"])      # the per-record properties of the writer are not judged either (the same exclusions as `usable`)
        h = d.get("handoff")
        if isinstance(h, dict) and "versions" in h: d["handoff"] = dict(h, versions=bare(h["versions"], RECORD_WRITER_OWNED["versions"]))
    return validate(d)

def usable(records, node, strip=()):
    """-> one entry per record of a list: the record itself when it is an object that matches the item schema `node` (the properties in `strip` are the writer's and are not judged), else None. A record that is None here is NOT safe
    to read: the report writer evaluates an empty record in its place, and the structural problem it is stays counted by `validate_report` (it blocks a PASS; a confirmed defect keeps FAIL)."""
    return [r if isinstance(r, dict) and not validate({k: v for k, v in r.items() if k not in strip}, node) else None for r in (records if isinstance(records, list) else [])]

def item_schema(*path):
    """The schema of the items of a list property of the report: item_schema("checks"), item_schema("handoff", "versions")."""
    node = schema()
    for p in path: node = node["properties"][p]
    return node["items"]

def summarize(problems, cap=3):
    """The compact reason for a list of problems (never echoes more than `cap` of them)."""
    if not problems: return ""
    head = "; ".join(problems[:cap])
    return head + (" (+%d more)" % (len(problems) - cap) if len(problems) > cap else "")
