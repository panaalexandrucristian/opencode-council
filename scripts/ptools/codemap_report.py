#!/usr/bin/env python3
"""Offline, read-only accounting for a run's code map (stdlib only; no model calls).

Usage: codemap_report.py <run-dir> [--trace FILE]

Everything reported here is counted from what the run actually RECORDED: run-dir/codemap/
index.json (published evidence), events/ (one durable record per capture/validation/publication
operation, each with its own id so two identical validation outcomes stay two events even though
they share one content-addressed snapshot), and deliveries.jsonl (the byte boundaries the
orchestrator recorded at the moment it wrote each locator block, plus the identity of the prompt
it then actually launched). Nothing is inferred by searching for delimiter strings inside task
text, handover notes or relayed author posts, a prompt written but never launched is never counted
as delivered, and records that do not fit what was launched are reported as corrupt rather than
counted.

Two kinds of coverage are kept apart and never added together: CAPTURED coverage is bytes this
map holds and can reproduce; ATTESTED coverage is what an author's report claims to have
inspected. An inspection label, a symbol or a conclusion is a claim, never proof of examination.

Lookup is a read-only service with no built-in log: actual repeated lookup requests and
stale/missing lookup results are UNKNOWN unless an explicit --trace is supplied, and then only as
far as that trace claims to be complete. Byte counts are never converted into token or dollar
figures, and avoided tool calls are never estimated.
"""
import argparse
import json
from pathlib import Path

STATUSES = ("current", "changed", "missing", "unreadable", "unstable")


def load_run(parser):
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--trace", type=Path, default=None)
    args = parser.parse_args()
    try:
        run = args.run_dir
        if not run.is_dir():
            raise ValueError("expected an existing run directory")
        index_path = run / "codemap" / "index.json"
        index = json.loads(index_path.read_bytes()) if index_path.exists() else None
        trace = None
        if args.trace is not None:
            trace = json.loads(args.trace.read_bytes())
        return run, index, trace
    except (OSError, ValueError) as exc:
        parser.error(str(exc))


def _load_json(path):
    try:
        return json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None


def union_bytes(ranges):
    """Union length of byte ranges, overlap counted once."""
    merged = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return sum(b - a for a, b in merged)


def report_index(index, run=None):
    lines = []
    if index is None and run is not None and (run / "codemap" / "index-head.json").exists():
        return ["codemap/index.json is MISSING while codemap/index-head.json still names a published "
                "index snapshot — the accounting records are incomplete/corrupt; nothing below is "
                "counted from them. Run a map operation to recover the index from that snapshot."]
    if index is None:
        lines.append("No codemap/index.json — the code map was never published in this run "
                     "(disabled, or no map-backed step completed).")
        return lines
    sources = index.get("sources", {})
    entries = index.get("entries", {})
    locations = index.get("locations", {})

    # ---- distinct source identity, by the four fields that actually make an identity
    versions = {(e["root"], e["path"], e["resolved_path"], e["source_sha256"]) for e in entries.values()}
    lines.append("Distinct source versions (root+path+resolved_path+digest): {}".format(len(versions)))
    lines.append("Distinct display paths: {} · distinct resolved paths: {} · distinct content digests: {}".format(
        len({v[1] for v in versions}), len({v[2] for v in versions}), len({v[3] for v in versions})))
    lines.append("Stored full-source objects referenced by the index: {} ({} of them line-addressable "
                 "text, the rest binary/NUL-bearing)".format(
                     len(sources), sum(1 for s in sources.values() if s.get("text_safe"))))
    lines.append("Range entries: {}".format(len(entries)))

    # ---- captured vs attested coverage, kept strictly apart
    captured = {}
    attested = {}
    for e in entries.values():
        key = (e["root"], e["path"], e["resolved_path"], e["source_sha256"])
        captured.setdefault(key, []).append(tuple(e["byte_range"]))
        if e.get("reports"):
            attested.setdefault(key, []).append(tuple(e["byte_range"]))
    captured_bytes = sum(union_bytes(r) for r in captured.values())
    attested_bytes = sum(union_bytes(r) for r in attested.values())
    whole_files = sum(s.get("size", 0) for s in sources.values())
    lines.append("CAPTURED coverage (bytes this map holds and can reproduce): {:,} bytes of indexed "
                 "ranges over {} versions; {:,} bytes of whole stored source objects".format(
                     captured_bytes, len(captured), whole_files))
    lines.append("ATTESTED coverage (bytes some author report claims to have inspected): {:,} bytes over "
                 "{} versions — a claim, never proof of examination".format(attested_bytes, len(attested)))

    # ---- reports and the labels attached to them
    bound = sum(len(e.get("reports", [])) for e in entries.values())
    unbound = len(index.get("unbound", []))
    lines.append("Author reports: {} bound (attributed to an entry), {} unbound (digest missing/mismatched, "
                 "invalid range, binary line claim, or capture failed)".format(bound, unbound))
    unbound_reasons = {}
    for u in index.get("unbound", []):
        reason = str(u.get("reason", "?")).split(":")[0]
        unbound_reasons[reason] = unbound_reasons.get(reason, 0) + 1
    lines.append("Unbound reasons: " + (", ".join("{}={}".format(k, v) for k, v in sorted(unbound_reasons.items()))
                                        or "none"))
    labels = {}
    for e in entries.values():
        for r in e.get("reports", []):
            for field in ("inspection", "symbol", "conclusion"):
                if r.get(field):
                    labels[field] = labels.get(field, 0) + 1
            if r.get("inspection"):
                key = "label:" + str(r["inspection"])
                labels[key] = labels.get(key, 0) + 1
    lines.append("Reports carrying inspection/symbol/conclusion claims: inspection={}, symbol={}, "
                 "conclusion={} (all author claims, NOT verification)".format(
                     labels.get("inspection", 0), labels.get("symbol", 0), labels.get("conclusion", 0)))
    per_label = sorted((k[len("label:"):], v) for k, v in labels.items() if k.startswith("label:"))
    lines.append("Distinct inspection labels used: " +
                 (", ".join("{}={}".format(k, v) for k, v in per_label) or "none"))
    readers = {}
    for e in entries.values():
        for r in e.get("reports", []):
            reader = r.get("reader", {})
            readers[(reader.get("member"), reader.get("generation"))] = \
                readers.get((reader.get("member"), reader.get("generation")), 0) + 1
    lines.append("Bound reports per (member, launch generation): " +
                 (", ".join("{}g{}={}".format(m, g, n) for (m, g), n in sorted(readers.items(), key=str))
                  or "none"))

    # ---- per-entry freshness, not per-location
    entry_status = dict.fromkeys(STATUSES, 0)
    unknown = 0
    for e in entries.values():
        loc = locations.get(e["path"])
        if loc is None:
            unknown += 1
            continue
        status = loc.get("status")
        if status == "current" and (loc.get("resolved_path") != e["resolved_path"]
                                    or loc.get("source_sha256") != e["source_sha256"]):
            status = "changed"   # the path is current, but not at the version THIS entry records
        if status in entry_status:
            entry_status[status] += 1
        else:
            unknown += 1
    lines.append("Entry freshness against the last recorded validation: " +
                 ", ".join("{}={}".format(s, entry_status[s]) for s in STATUSES) +
                 ", unknown={}".format(unknown))
    loc_status = {}
    for loc in locations.values():
        loc_status[loc.get("status", "?")] = loc_status.get(loc.get("status", "?"), 0) + 1
    lines.append("Location status per display path: " +
                 (", ".join("{}={}".format(k, v) for k, v in sorted(loc_status.items())) or "no captured locations"))
    lines.append("Accepted (event, position) pairs ingested exactly once: {}".format(
        len(index.get("ingested_keys", []))))
    lines.append("Diagnostics recorded in the published index: {}".format(len(index.get("diagnostics", []))))
    return lines


def report_events(run):
    """Every capture/validation/publication operation the run actually recorded. Events carry their
    own ids, so two identical validation outcomes are two events even though they share one
    content-addressed snapshot."""
    lines = []
    edir = run / "codemap" / "events"
    events = []
    if edir.is_dir():
        for f in sorted(edir.glob("e*.json")):
            ev = _load_json(f)
            if ev:
                events.append(ev)
    if not events:
        return ["No recorded capture/validation events (events/ is empty or absent)."]
    by_kind = {}
    totals = {"bytes_read": 0, "bytes_hashed": 0, "captures": 0, "capture_failures": 0,
              "capture_unstable": 0, "read_attempts": 0}
    guard_status = {}
    source_status = {}
    for ev in events:
        kind = "{}:{}".format(ev.get("kind"), ev.get("mode") or "-")
        by_kind[kind] = by_kind.get(kind, 0) + 1
        for k in totals:
            totals[k] += (ev.get("stats") or {}).get(k, 0)
        if ev.get("guard_status"):
            guard_status[ev["guard_status"]] = guard_status.get(ev["guard_status"], 0) + 1
        for s in ev.get("sources") or []:
            st = s.get("status", "?")
            source_status[st] = source_status.get(st, 0) + 1
    lines.append("Recorded operations: " + ", ".join("{}={}".format(k, v) for k, v in sorted(by_kind.items())))
    lines.append("Bytes actually READ from sources: {:,} · bytes actually HASHED: {:,} (the capture "
                 "contract is two complete reads, each hashed in its own right, so a stable capture "
                 "reads and hashes the file twice)".format(totals["bytes_read"], totals["bytes_hashed"]))
    lines.append("Capture operations: {} · read attempts: {} · definite failures: {} · unstable "
                 "(changed under the reader): {}".format(totals["captures"], totals["read_attempts"],
                                                         totals["capture_failures"], totals["capture_unstable"]))
    lines.append("Guard verdicts across recorded validation events: " +
                 (", ".join("{}={}".format(k, v) for k, v in sorted(guard_status.items())) or "none"))
    lines.append("Per-source observations across all recorded validations: " +
                 (", ".join("{}={}".format(k, v) for k, v in sorted(source_status.items())) or "none"))
    snaps = run / "codemap" / "snapshots"
    n_snaps = len(list(snaps.glob("*.json"))) if snaps.is_dir() else 0
    lines.append("Distinct immutable validation snapshots: {} (fewer than the {} recorded events exactly "
                 "when a validation repeated its outcome byte-for-byte)".format(n_snaps, len(events)))
    repeated = {}
    for ev in events:
        for s in ev.get("sources") or []:
            if s.get("path"):
                repeated[s["path"]] = repeated.get(s["path"], 0) + 1
    lines.append("Recorded repeated re-examinations of the same path (from run data, never a lookup trace): " +
                 (", ".join("{} x{}".format(p, n) for p, n in sorted(repeated.items()) if n > 1)
                  or "none (no path was examined more than once)"))
    # Staging events carry no sources array, so they are invisible to the loop above. Counting
    # them separately keeps logical report ingestion distinct from physical validation work
    # instead of losing it entirely.
    read_reports = sum(ev.get("read_reports", 0) for ev in events if ev.get("kind") == "stage")
    reingests = sum(1 for ev in events if ev.get("kind") == "stage_reingest")
    lines.append("Logical report ingestion, counted apart from physical validation work: {} accepted "
                 "code_reads reports across {} staging events, plus {} re-ingestions of an "
                 "already-staged accepted event (zero capture work, and never re-decided)".format(
                     read_reports, sum(1 for ev in events if ev.get("kind") == "stage"), reingests))
    return lines


def report_locator_delivery(run):
    """Counted ONLY from boundaries the orchestrator recorded at the moment it wrote those bytes,
    and only for prompts it then actually launched. Intentional duplicate deliveries (the same
    block to several members, or again after a retry) are counted honestly, once each."""
    path = run / "codemap" / "deliveries.jsonl"
    if not path.exists():
        return ["Locator delivery: nothing recorded (no map-backed prompt was written in this run)."]
    spans = []
    launched = {}
    bad = 0
    try:
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                bad += 1
                continue
            if not isinstance(rec, dict):
                bad += 1
                continue
            kind = rec.get("kind")
            if kind == "launched":
                if not isinstance(rec.get("prompt"), str) or not isinstance(rec.get("prompt_size"), int):
                    bad += 1
                    continue
                # a prompt written twice (replay) is a distinct delivery of distinct bytes
                launched.setdefault((rec["prompt"], rec.get("attempt")), []).append(rec)
            elif kind == "span":
                if not isinstance(rec.get("length"), int) or not isinstance(rec.get("offset"), int) \
                        or rec["length"] < 0 or rec["offset"] < 0 or not isinstance(rec.get("prompt"), str):
                    bad += 1
                    continue
                spans.append(rec)
            else:
                bad += 1
    except OSError as exc:
        return ["Locator delivery: delivery record unreadable ({}).".format(exc)]

    delivered = 0
    delivered_blocks = 0
    written_only = 0
    written_only_bytes = 0
    out_of_range = 0
    per_prompt = {}
    per_site = {}
    for sp in spans:
        key = (sp["prompt"], sp.get("attempt"))
        records = launched.get(key)
        if not records:
            # written into a prompt file that was never launched: real bytes, but not delivered
            written_only += 1
            written_only_bytes += sp["length"]
            continue
        # validated against the prompt identity recorded AT LAUNCH, so a later replay that
        # overwrote the prompt file cannot make this historical span describe different bytes
        if any(sp["offset"] + sp["length"] <= r["prompt_size"] for r in records):
            delivered += sp["length"]
            delivered_blocks += 1
            per_prompt[sp["prompt"]] = per_prompt.get(sp["prompt"], 0) + 1
            site = sp.get("site", "?")
            per_site[site] = per_site.get(site, 0) + sp["length"]
        else:
            out_of_range += 1
    lines = ["Locator bytes actually DELIVERED (recorded span, inside a prompt that was really "
             "launched): {:,} bytes in {} blocks across {} prompt files".format(
                 delivered, delivered_blocks, len(per_prompt))]
    lines.append("Delivered bytes by site: " +
                 (", ".join("{}={:,}".format(k, v) for k, v in sorted(per_site.items())) or "none"))
    dupes = {k: v for k, v in per_prompt.items() if v > 1}
    if dupes:
        lines.append("Prompts that received more than one locator block (counted, not deduplicated): " +
                     ", ".join("{} x{}".format(k, v) for k, v in sorted(dupes.items())))
    if written_only:
        lines.append("Locator blocks written into a prompt that was never launched: {} ({:,} bytes) — "
                     "written is not delivered, so these are excluded above.".format(
                         written_only, written_only_bytes))
    if out_of_range:
        lines.append("Recorded spans that do not fit the prompt bytes recorded at launch: {} — treated "
                     "as CORRUPT accounting records and excluded, not silently counted.".format(out_of_range))
    if bad:
        lines.append("Unparsable or unrecognised delivery records: {} — the delivery log is "
                     "INCOMPLETE, so the figures above are a lower bound.".format(bad))
    return lines


def normalize_request(request, effective_snapshot=None):
    """The same normalization lookup itself applies, so 'repeated' means what lookup would call
    the same request. The cursor is part of the identity: page 2 of a query is a different
    request from page 1, and collapsing them would report real paging as wasteful repetition."""
    if not isinstance(request, dict):
        return None
    def arr(key):
        v = request.get(key)
        return sorted(v) if isinstance(v, list) and v else None
    return json.dumps({"entry_ids": arr("entry_ids"), "paths": arr("paths"), "symbols": arr("symbols"),
                       "include_evidence": bool(request.get("include_evidence", False)),
                       "include_conclusions": bool(request.get("include_conclusions", False)),
                       "cursor": request.get("cursor"),
                       "snapshot_id": request.get("snapshot_id") or effective_snapshot},
                      sort_keys=True)


def report_trace(trace):
    if trace is None:
        return ["Lookup telemetry: UNKNOWN (lookup is read-only with no built-in log; no --trace was "
                "supplied). Repeated lookups, stale reads and missing selectors cannot be counted."]
    lines = []
    scope = trace.get("scope", "(unspecified)")
    complete = bool(trace.get("complete", False))
    lines.append("Lookup trace scope: {} (complete={})".format(scope, complete))
    requests = trace.get("requests", [])
    if not isinstance(requests, list):
        return lines + ["Trace 'requests' is not an array — nothing countable."]
    seen = {}
    stale = 0
    missing = 0
    unavailable = 0
    malformed = 0
    for r in requests:
        if not isinstance(r, dict):
            malformed += 1
            continue
        result_snapshot = (r.get("result") or {}).get("snapshot_id") if isinstance(r.get("result"), dict) else None
        key = normalize_request(r.get("request"), result_snapshot)
        if key is None:
            malformed += 1
            continue
        seen[key] = seen.get(key, 0) + 1
        result = r.get("result")
        if not isinstance(result, dict):
            continue
        # the real lookup response contract: entries[].freshness.current_at_snapshot, missing[], error
        if result.get("error"):
            unavailable += 1
        for e in result.get("entries") or []:
            if isinstance(e, dict) and not (e.get("freshness") or {}).get("current_at_snapshot", True):
                stale += 1
        if isinstance(result.get("missing"), list):
            missing += len(result["missing"])
    repeated = sum(1 for n in seen.values() if n > 1)
    lines.append("Trace-supplied lookup requests: {} total, {} distinct under lookup's own normalization, "
                 "{} of them repeated".format(len(requests), len(seen), repeated))
    lines.append("Trace-supplied results: {} entries served at a non-current source version (stale), "
                 "{} missing selectors, {} requests answered 'unavailable'".format(stale, missing, unavailable))
    if malformed:
        lines.append("Trace records that could not be interpreted: {} (excluded from every count above).".format(malformed))
    if not complete:
        lines.append("NOTE: the trace does not claim completeness; these counts are a lower bound, not the "
                     "true totals.")
    return lines


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0], usage="%(prog)s <run-dir> [--trace FILE]")
    run, index, trace = load_run(parser)
    print("### Code map (source-attributed evidence coverage)")
    for line in report_index(index, run):
        print(line)
    print()
    for line in report_events(run):
        print(line)
    print()
    for line in report_locator_delivery(run):
        print(line)
    print()
    for line in report_trace(trace):
        print(line)
    print()
    print("Savings are not reported: avoided tool calls, avoided API turns, tokens and money are "
          "unknowable from these records, and this report never converts byte counts into them.")


if __name__ == "__main__":
    main()
