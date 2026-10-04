#!/usr/bin/env python3
"""Offline report over council telemetry (read-only; no network, no model calls, no writes).

usage: telemetry_report.py [-h] [INPUT ...]

INPUT is a ledger file, a directory (its immediate *.jsonl files) or a council run directory (its
telemetry.jsonl). Default: $COUNCIL_TELEMETRY_DIR, else ~/.council-telemetry. Symlinks are never followed.
Records are deduplicated by (host, run, seq); records that conflict under one identity are excluded and
counted; the latest run_summary per run is shown but never added to the event totals. Unknown values are
counted separately, never as zero. Exit 0 when a report is printed, 2 when there is no readable input.
"""
import argparse
import json
import math
import os
import stat
import sys
from collections import Counter, defaultdict

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import council_telemetry as schema  # noqa: E402  (shared registry; no import-time side effects)

STEPS = ("plan", "exec", "ratify", "handover", "map", "replace")
CLASSES = schema.CLASSES
MAX_NUMBER = 2 ** 53 - 1


def nearest_rank(values, p):
    """Nearest-rank percentile over known values: sorted value at index ceil(p*n)-1; None when empty."""
    if not values:
        return None
    s = sorted(values)
    return s[max(0, math.ceil(p * len(s)) - 1)]


def pct(n, d):
    return "%.2f%%" % (100.0 * n / d) if d else "n/a"


def secs(v):
    return "unknown" if v is None else "%ds" % v


def unk(v):
    return "unknown" if v is None else v


def usd(v):
    return "$%.6f" % v


class Diagnostics:
    def __init__(self):
        self.c = Counter()


def symlinked_parent(path):
    """True when a directory on the way to path, as written (never lexically normalized), is a symlink, whoever
    owns it (as the writer refuses it); the final component is checked by the caller."""
    parts = [c for c in os.path.join(os.getcwd(), path).split(os.sep)[:-1] if c and c != "."]
    cur = os.sep
    for c in parts:
        cur = os.path.join(cur, c)
        st = os.lstat(cur)
        if stat.S_ISLNK(st.st_mode):
            return True
    return False


def discover(paths, diag):
    """-> list of readable files; symlinks (final or parent) and unreadable inputs are counted, never followed."""
    files = []
    for p in paths:
        try:
            if symlinked_parent(p):
                diag.c["symlinks"] += 1
                continue
            st = os.lstat(p)
        except OSError:
            diag.c["unreadable"] += 1
            continue
        if stat.S_ISLNK(st.st_mode):
            diag.c["symlinks"] += 1
            continue
        if stat.S_ISREG(st.st_mode):
            files.append(p)
            continue
        if not stat.S_ISDIR(st.st_mode):
            diag.c["unreadable"] += 1
            continue
        events = os.path.join(p, schema.EVENTS_FILE)
        if os.path.lexists(events) or os.path.lexists(os.path.join(p, "state.json")):
            names = [schema.EVENTS_FILE] if os.path.lexists(events) else []
        else:
            try:
                names = sorted(n for n in os.listdir(p) if n.endswith(".jsonl"))
            except OSError:
                diag.c["unreadable"] += 1
                continue
        for n in names:
            f = os.path.join(p, n)
            try:
                fst = os.lstat(f)
            except OSError:
                diag.c["unreadable"] += 1
                continue
            if stat.S_ISLNK(fst.st_mode):
                diag.c["symlinks"] += 1
            elif stat.S_ISREG(fst.st_mode):
                files.append(f)
    return files


def bounded(v):
    """Numbers stay within the exactly representable integer range; nested objects are checked too."""
    if isinstance(v, dict):
        return all(bounded(x) for x in v.values())
    if isinstance(v, bool) or v is None or isinstance(v, str):
        return True
    if isinstance(v, int):
        return -MAX_NUMBER <= v <= MAX_NUMBER
    if isinstance(v, float):
        return math.isfinite(v) and -MAX_NUMBER <= v <= MAX_NUMBER
    return False


def check(rec):
    """schema.validate plus the report's own totality guards (exact integer schema, string ev, bounded numbers)."""
    if not isinstance(rec, dict):
        raise schema.Invalid("invalid")
    if type(rec.get("schema")) is not int or rec.get("schema") != schema.SCHEMA:
        raise schema.Invalid("schema")
    if not isinstance(rec.get("ev"), str):
        raise schema.Invalid("event")
    schema.validate(rec)
    if not bounded(rec):
        raise schema.Invalid("invalid")


def read_records(files, diag):
    """-> (records by identity, number of files read). Identity = (host, run, seq) per record kind."""
    seen, conflicts, read = {}, set(), 0
    for f in files:
        try:
            fd = os.open(f, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as fh:
                data = fh.read()
        except OSError:
            diag.c["unreadable"] += 1
            continue
        read += 1
        for raw in data.split(b"\n"):
            if not raw.strip():
                continue
            try:
                rec = schema.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError, RecursionError):
                diag.c["malformed"] += 1
                continue
            try:
                check(rec)
                key = (rec["host"], rec["run"], rec["seq"])
                hash(key)
            except schema.Invalid as exc:
                diag.c[exc.args[0] if exc.args and exc.args[0] in ("schema", "event") else "invalid"] += 1
                continue
            except Exception:  # validation must never traceback the report
                diag.c["invalid"] += 1
                continue
            if key in conflicts:
                diag.c["conflicting"] += 1
            elif key in seen:
                if seen[key] == rec:
                    diag.c["duplicates"] += 1
                else:
                    conflicts.add(key)
                    diag.c["conflicting"] += 2
                    del seen[key]
            else:
                seen[key] = rec
    return list(seen.values()), read


def section(out, title, lines):
    out.append(title)
    out.extend("  " + l for l in (lines or ["(none)"]))


def report_block(records, label):
    """One host (or all hosts): every section, computed only from event records."""
    out = ["=== %s ===" % label]
    events = [r for r in records if r["ev"] != "run_summary"]
    summaries = [r for r in records if r["ev"] == "run_summary"]
    runs_events = {(r["host"], r["run"]) for r in events}
    runs_all = runs_events | {(r["host"], r["run"]) for r in summaries}
    by = lambda r: (r["host"], r["run"])
    latest = {}
    for s in summaries:
        k = by(s)
        if k not in latest or (s["inv"], s["seq"]) > (latest[k]["inv"], latest[k]["seq"]):
            latest[k] = s
    starts_rs = [r for r in events if r["ev"] == "run_start"]
    # history known only from a summary (its run_start not collected) still counts
    partial = ({by(r) for r in starts_rs if r.get("history") == "partial"}
               | {k for k, s in latest.items() if s.get("history") == "partial"})
    gaps = (sum(1 for r in starts_rs if r.get("gap") is True)
            + sum(1 for k, s in latest.items() if k not in runs_events and s.get("gap") is True))
    invocations = {(r["host"], r["run"], r["inv"]) for r in records}
    section(out, "Coverage", ["runs: %d (history partial: %d; gap-flagged segments: %d; summary-only runs: %d)"
                              % (len(runs_all), len(partial), gaps, len(runs_all - runs_events)),
                              "invocations: %d" % len(invocations), "events: %d" % len(events)])
    starts = {(r["host"], r["run"], r["seq"]): r for r in events if r["ev"] == "call_start"}
    ends = {}
    for r in events:
        if r["ev"] == "call_end" and (r["host"], r["run"], r.get("call_seq")) in starts:
            ends[(r["host"], r["run"], r["call_seq"])] = r
    groups = defaultdict(list)
    for k, s in starts.items():
        groups[(s.get("kind") or "unknown", s.get("model") or "unknown")].append(k)

    def call_line(name, keys):
        n = len(keys)
        oc = Counter(ends[k].get("outcome") for k in keys if k in ends)
        unknown = n - oc["ok"] - oc["failed"] - oc["killed"]  # no call_end, or an end with a null outcome
        return "%s: launches %d; ok %d; failed %d; killed %d; unknown %d; failure rate %s" % (
            name, n, oc["ok"], oc["failed"], oc["killed"], unknown, pct(oc["failed"] + oc["killed"], n))
    lines = [call_line("%s %s" % g, groups[g]) for g in sorted(groups)]
    if starts:
        lines.append(call_line("all", list(starts)))
    section(out, "Calls by model/kind", lines)
    n = len(starts)
    fc = Counter(e.get("failure_class") for e in ends.values() if e.get("failure_class"))
    section(out, "Failure classes (rate over all launches)", ["%s: %d (%s)" % (c, fc[c], pct(fc[c], n)) for c in sorted(fc)])
    kills = Counter(r.get("result") or "unknown" for r in events if r["ev"] == "kill_group")
    section(out, "Timeouts and kill results", ["timeouts: %d" % sum(1 for r in events if r["ev"] == "timeout"),
                                               "kill_group: stopped %d; survived %d; refused %d; unknown %d"
                                               % (kills["stopped"], kills["survived"], kills["refused"], kills["unknown"])])
    retries = Counter(r.get("failure_class") or "unknown" for r in events if r["ev"] == "retry")
    section(out, "Retries", ["retries: %d%s" % (sum(retries.values()), " (%s)" % ", ".join("%s %d" % (c, retries[c]) for c in sorted(retries)) if retries else "")])
    lines = []
    for step in STEPS:
        keys = [k for k, s in starts.items() if s.get("step") == step]
        if not keys:
            continue
        known = [ends[k]["duration_s"] for k in keys if k in ends and ends[k].get("duration_s") is not None]
        lines.append("%s: known %d; median %s; p90 %s; unknown %d" % (step, len(known), secs(nearest_rank(known, 0.5)),
                                                                     secs(nearest_rank(known, 0.9)), len(keys) - len(known)))
    section(out, "Durations by step (seconds, nearest rank; observed launch to completion, includes collection delay)", lines)

    def cost_line(name, keys):
        known = [ends[k]["cost_usd"] for k in keys if k in ends and ends[k].get("cost_usd") is not None]
        return "%s: %s known + %d calls unknown" % (name, usd(sum(known)), len(keys) - len(known))
    lines = []
    for run in sorted({by(s) for s in starts.values()}):
        lines.append(cost_line("council %s" % run[1][:8], [k for k in starts if k[:2] == run]))
    for step in STEPS:
        keys = [k for k, s in starts.items() if s.get("step") == step]
        if keys:
            lines.append(cost_line("step %s" % step, keys))
    section(out, "Costs (USD; known subtotal + calls with unknown cost)", lines)
    ho = Counter(r.get("type") for r in events if r["ev"] == "handover" and r.get("stage") == "start")
    section(out, "Handovers", ["handovers: threshold %d; replace %d" % (ho["threshold"], ho["replace"])])
    gates = [r for r in events if r["ev"] == "gate"]
    gr = Counter(r.get("result") for r in gates)
    timed = [r for r in gates if r.get("result") in ("released", "timeout")]
    waits = [r["wait_s"] for r in timed if r.get("wait_s") is not None]
    section(out, "Gate waits", ["released %d; timeout %d; disabled %d; interrupted %d; unknown result %d; wait median %s; p90 %s; unknown waits %d"
                                % (gr["released"], gr["timeout"], gr["disabled"], sum(1 for r in gates if r.get("interrupted")),
                                   sum(1 for r in gates if r.get("result") is None and not r.get("interrupted")),
                                   secs(nearest_rank(waits, 0.5)), secs(nearest_rank(waits, 0.9)), len(timed) - len(waits))])
    maps = [r for r in events if r["ev"] == "mapper"]
    ms = Counter(r.get("status") for r in maps)
    rep = [r.get("repaired") for r in maps if r.get("repaired") is not None]
    map_starts = {(r["host"], r["run"], r["seq"]) for r in events if r["ev"] == "mapper_start"}
    linked = {(r["host"], r["run"], r.get("op_seq")) for r in maps if r.get("op_seq") is not None}
    section(out, "Mapper", ["operations: %d; complete %s; complete+partial %s; unknown status %d; repair rate %s (unknown repair status %d)"
                            % (len(maps), pct(ms["complete"], len(maps)), pct(ms["complete"] + ms["partial"], len(maps)),
                               ms[None], pct(sum(1 for x in rep if x), len(rep)), len(maps) - len(rep)),
                            "started %d; incomplete %d; aborted %d" % (len(map_starts), len(map_starts - linked), ms["aborted"])])
    qs = [r for r in events if r["ev"] == "questions"]
    qsum = lambda src: sum(r["count"] for r in qs if r.get("source") == src and r.get("count") is not None)
    votes = [r for r in events if r["ev"] == "votes"]
    vkeys = ("vote_propose", "vote_agree", "vote_disagree", "vote_question", "vote_done", "failed_members", "converted")
    vsum = lambda k: sum(r[k] for r in votes if r.get(k) is not None)  # known subtotal; nulls counted below
    oc = Counter(r.get("milestone") for r in events if r["ev"] == "outcome")
    section(out, "Questions, votes and outcomes", [
        "questions: member %d; mapper %d; contract %d; unknown counts %d" % (qsum("member"), qsum("mapper"), qsum("contract"),
                                                                            sum(1 for r in qs if r.get("count") is None)),
        "votes: propose %d; agree %d; disagree %d; question %d; done %d; failed members %d; converted %d; replays %d; tallies with unknown counts %d"
        % (vsum("vote_propose"), vsum("vote_agree"), vsum("vote_disagree"), vsum("vote_question"), vsum("vote_done"),
           vsum("failed_members"), vsum("converted"), sum(1 for r in votes if r.get("replay")),
           sum(1 for r in votes if any(r.get(k) is None for k in vkeys))),
        "outcomes: consensus %d; unresolved %d; ratified %d; unratified %d" % (oc["consensus"], oc["unresolved"], oc["ratified"], oc["unratified"])])
    section(out, "Run summaries (latest snapshot per run; never added to event totals)", [
        "run %s: inv %d; exit %s; status %s; calls %s (outcome unknown %s); questions %s (unknown counts %s)%s"
        % (k[1][:8], s["inv"], s.get("exit_code"), s.get("status"), s.get("calls"), unk(s.get("outcome_unknown")),
           unk(s.get("questions")), unk(s.get("questions_unknown")), "" if k in runs_events else " (summary only)")
        for k, s in sorted(latest.items())])
    return out


def main(argv=None):
    p = argparse.ArgumentParser(prog="telemetry_report.py", description="Offline report over council telemetry (read-only).",
                                epilog="Default input: $COUNCIL_TELEMETRY_DIR, else ~/.council-telemetry. Exit 0 report, 2 no readable input.")
    p.add_argument("inputs", nargs="*", help="ledger files, directories (immediate *.jsonl) or run directories (telemetry.jsonl)")
    a = p.parse_args(argv)
    inputs = a.inputs or [os.environ.get("COUNCIL_TELEMETRY_DIR") or os.path.join(os.path.expanduser("~"), ".council-telemetry")]
    diag = Diagnostics()
    files = discover(inputs, diag)
    records, read = read_records(files, diag)
    if read == 0:
        print("telemetry_report: no readable input", file=sys.stderr)
        return 2
    out = ["Council telemetry report (content-free; observed durations include collection delay)",
           "inputs: %d file(s) read" % read, ""]
    for host in sorted({r["host"] for r in records}):
        out.extend(report_block([r for r in records if r["host"] == host], "Host " + host))
        out.append("")
    out.extend(report_block(records, "All hosts"))
    out.append("")
    c = diag.c
    out.append("=== Diagnostics ===")
    out.extend("  " + l for l in ["malformed or truncated lines: %d" % c["malformed"], "unsupported schema: %d" % c["schema"],
                                  "unknown event types: %d" % c["event"], "invalid values: %d" % c["invalid"],
                                  "duplicate records (identical, counted once): %d" % c["duplicates"],
                                  "conflicting records (same host, run and seq; excluded): %d" % c["conflicting"],
                                  "skipped symlinks: %d" % c["symlinks"], "unreadable inputs: %d" % c["unreadable"]])
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
