#!/usr/bin/env python3
"""Where do the tokens of council runs go? (stdlib only; local by default; read-only; no model calls; no prices)

By provider, step, round and category, over one or more runs.

usage: token_breakdown.py [-h] [--projects DIR] [--oc PATH] [--fetch-opencode] [--chars-per-token X]
                          [--top N] [--beyond N] RUN_DIR [RUN_DIR ...]

Every run is read through session_report.py, used as a module: the same generations, provider counters,
segments and task/round/phase attribution, so a run's numbers agree with its session_report report. Only
--fetch-opencode contacts OpenCode (through oc.sh, which is never allowed to start the service); without it
an OpenCode session has no per-call data and is listed in Coverage as not complete. Provider counters are
exact; the split over categories, the harness prefix and the size of file reads are session_report's
heuristic estimate (character-based). An unknown value is counted apart, never as 0. There are no prices
or weights: tokens and API calls are what the providers reported. The skill version comes only from the
run_start records that council_telemetry.py accepts. Exit 0 when a report is printed (also a partial one),
2 for a usage error or when no run can be read.
"""
import argparse
import bisect
import collections
import math
import os
import re
import sys

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.join(HERE, "..")]
import session_report as sr  # noqa: E402  (no import-time side effects)
import council_telemetry as schema  # noqa: E402  (shared registry; no import-time side effects)

STEPS = ("plan", "ratify", "exec", "handover", "retry", "unknown")
READ_TOOLS = ("tool:Read", "tool:read")
ROUND_RE = re.compile(r"round \d+ of \d+")
CLAUDE_CLI = "claude CLI"


class Refused(Exception):
    pass


class Collect:
    """An argparse stand-in for session_report.read_state: one refused run is reported, the others still are."""

    def error(self, msg):
        raise Refused(msg)


class Run:
    def __init__(self, path):
        self.path, self.label = path, sr.disp(path)
        self.error = self.state = self.analysis = self.versions = None
        self.gens = []


class Seg:
    """One segment (council call) of a session, with what the report groups it by."""

    def __init__(self, run, g, gen, s, j, rng, turns, c0, tokc, at):
        self.run, self.g, self.gen, self.s, self.j, self.rng = run, g, gen, s, j, rng
        self.turns, self.c0, self.tokc, self.at = turns, c0, tokc, at
        self.provider, self.step = provider_of(g), step_of(at)


def step_of(at):
    """The telemetry step of a segment, named as council.sh names it (prompt_exec and prompt_fix -> exec,
    prompt_ratify -> ratify, every other council prompt -> plan), from the attributed phase; a retry keeps
    its own bucket, since its prompt names no phase, and an unattributed segment is unknown."""
    task, rnd, phase, prov = at
    if " (retry)" in prov:
        return "retry"
    if phase == "handover request":
        return "handover"
    if phase in ("EXECUTION", "FIXES REQUESTED BY THE COUNCIL"):
        return "exec"
    if phase.startswith("RATIFICATION ") and ROUND_RE.fullmatch(phase[len("RATIFICATION "):]):
        return "ratify"
    if ROUND_RE.fullmatch(phase):
        return "plan"
    return "unknown"


def provider_of(g):
    """'claude CLI' for a Claude Code member, else the provider part of the recorded OpenCode model."""
    ident = g.identity
    kind = ident.kind if ident is not None and ident.kind else g.kind
    if kind == "claude":
        return CLAUDE_CLI
    model = ident.model if ident is not None else None
    if isinstance(model, str) and "/" in model:
        return sr.disp(model.split("/", 1)[0])
    return "unknown"


def skill_versions(run):
    """The distinct skill_version values of the run_start records the telemetry registry accepts; None
    when the run has no readable telemetry file."""
    try:
        with open(os.path.join(run, schema.EVENTS_FILE), "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    found = []
    for raw in data.split(b"\n"):
        if not raw.strip():
            continue
        try:
            rec = schema.loads(raw.decode("utf-8"))
            schema.validate(rec)
        except Exception:   # a record the registry refuses is not evidence of a version
            continue
        v = rec.get("skill_version")
        if rec.get("ev") == "run_start" and v and v not in found:
            found.append(v)
    return found


def version_text(found):
    if found is None:
        return "unknown (no readable %s)" % schema.EVENTS_FILE
    return ", ".join(sr.disp(v) for v in found) or "unknown (no accepted run_start with a version)"


def gen_numbers(state):
    """session id -> recorded generation, the first record of an id winning (session_report reads it once)."""
    out = {}
    for m in state.get("members", []):
        if not isinstance(m, dict):
            continue
        for rec in [x for x in m.get("retired") or [] if isinstance(x, dict)] + [m]:
            sid, gen = rec.get("session"), rec.get("gen")
            if isinstance(sid, str) and sid not in out and isinstance(gen, int) and not isinstance(gen, bool):
                out[sid] = gen
    return out


def norm_path(p, root):
    """A read target relative to the run's project directory when it lies under it, so the absolute and the
    relative spelling of one file are counted together; any other path is only normalized."""
    if not p:
        return p
    if not root:
        return os.path.normpath(p)
    q = os.path.normpath(p if os.path.isabs(p) else os.path.join(root, p))
    r = os.path.normpath(root)
    return os.path.relpath(q, r) if q == r or q.startswith(r + os.sep) else q


def read_run(path, a):
    r = Run(path)
    try:
        r.state = sr.read_state(Collect(), path)
    except Refused as e:
        r.error = str(e)
        return r
    r.gens = sr.gens_from_run(r.state, a.projects, a.oc, a.fetch_opencode)
    r.analysis = sr.compute(r.gens, a.chars_per_token, path)
    r.versions = skill_versions(path)
    return r


def segments(r):
    g_of = {id(g.session): g for g in r.gens if g.session}
    gens = gen_numbers(r.state)
    out = []
    for tokc, lab, j, turns, c0, c1, at, s in r.analysis.segs:
        g = g_of[id(s)]
        out.append(Seg(r, g, gens.get(g.sid), s, j, sr.segment_ranges(s)[j - 1], turns, c0, tokc, at))
    return out


def add(acc, tokc):
    for i in range(4):
        acc[i] += tokc[i]


def nearest_rank(values, p):
    s = sorted(values)
    return s[max(0, math.ceil(p * len(s)) - 1)]


def spread(values):
    """[median, p90, max] by nearest rank over the known values, and the number of unknown ones."""
    known = [v for v in values if v is not None]
    if not known:
        return ["unknown"] * 3 + [str(len(values))]
    return [sr.fmt(nearest_rank(known, 0.5)), sr.fmt(nearest_rank(known, 0.9)), sr.fmt(max(known)),
            str(len(values) - len(known))]


def table(header, rows):
    if not rows:
        print("(none)\n")
        return
    print("| " + " | ".join(header) + " |")
    print("|" + "---|" * len(header))
    for row in rows:
        print("| " + " | ".join(str(c) for c in row) + " |")
    print()


def share(part, whole):
    return "%.1f%%" % (100.0 * part / whole) if whole else "n/a"


def grouped(segs, key):
    """key -> [segments, API calls, consumed]."""
    acc = collections.OrderedDict()
    for sg in segs:
        row = acc.setdefault(key(sg), [0, 0, [0, 0, 0, 0]])
        row[0] += 1
        row[1] += sg.turns
        add(row[2], sg.tokc)
    return acc


def step_rank(step):
    return STEPS.index(step)


def print_runs(runs):
    print("## Runs\n")
    rows = []
    for r in runs:
        if r.error is not None:
            rows.append([r.label, "n/a", "not read: " + sr.disp(r.error), "", "", "", ""])
            continue
        sessions = [s for s, res in r.analysis.sessions]
        tokc = [0, 0, 0, 0]
        for s in sessions:
            add(tokc, sr.consumed(s.kind, s.calls))
        unknown = sum(1 for sg in r.analysis.segs if sg[6][0] == "unknown")
        rows.append([r.label, version_text(r.versions), "%d of %d" % (sum(g.status == "complete" for g in r.gens), len(r.gens)),
                     len(r.analysis.segs), sum(len(s.calls) for s in sessions), sr.qualified(tokc), unknown])
    table(["run", "skill version", "sessions complete", "council calls (segments)", "API calls",
           "tokens consumed (provider counters)", "segments with task unknown"], rows)


def print_coverage(runs):
    print("## Coverage\n")
    lines = []
    for r in runs:
        if r.error is not None:
            lines.append("- %s: not read (%s)" % (r.label, sr.disp(r.error)))
            continue
        for g in r.gens:
            if g.status != "complete":
                lines.append("- %s: %s: %s %s%s" % (r.label, g.label, g.source, g.status_text(), " - " + g.detail if g.detail else ""))
    print("\n".join(lines) if lines else "Every session of every run read is complete.")
    print()


def print_providers(runs):
    print("## By provider (provider counters)\n")
    recorded, sessions = collections.Counter(), collections.OrderedDict()
    for r in runs:
        if r.error is not None:
            continue
        for g in r.gens:
            recorded[provider_of(g)] += 1
            if g.session:
                sessions.setdefault(provider_of(g), []).append(g.session)
    rows = []
    for p in sorted(recorded):
        ss = sessions.get(p, [])
        calls = [c for s in ss for c in s.calls]
        oc_calls = [c for s in ss if s.kind == "opencode" for c in s.calls]
        tokc = [0, 0, 0, 0]
        for s in ss:
            add(tokc, sr.consumed(s.kind, s.calls))
        rows.append([p, "%d of %d" % (len(ss), recorded[p]), sum(len(s.segments) for s in ss), len(calls),
                     sr.column(calls, "inp"), sr.column(calls, "cr"), sr.column(calls, "cw"), sr.column(calls, "out"),
                     sr.column(oc_calls, "reasoning") if oc_calls else "n/a (Claude usage has no reasoning count)",
                     sr.qualified(tokc)])
    table(["provider", "sessions read", "council calls", "API calls", "input (new)", "cache read", "cache write", "output",
           "reasoning (reported)", "tokens consumed"], rows)


def print_steps(segs):
    print("## By step\n")
    print("Step as council.sh names it in telemetry (execution and fixes are exec), from the attributed phase; a retry "
          "prompt names no phase and has its own row; a segment without attribution is unknown.\n")
    known = sum(sg.tokc[0] for sg in segs)
    acc = grouped(segs, lambda sg: sg.step)
    table(["step", "segments", "API calls", "tokens consumed", "share of known tokens"],
          [[k, n, calls, sr.qualified(t), share(t[0], known)] for k, (n, calls, t) in sorted(acc.items(), key=lambda kv: step_rank(kv[0]))])
    print("### By step and provider\n")
    acc = grouped(segs, lambda sg: (sg.step, sg.provider))
    table(["step", "provider", "segments", "API calls", "tokens consumed"],
          [[k[0], k[1], n, calls, sr.qualified(t)] for k, (n, calls, t) in sorted(acc.items(), key=lambda kv: (step_rank(kv[0][0]), kv[0][1]))])


def print_rounds(segs):
    print("## By round (deliberation and ratification)\n")
    rounds = [sg for sg in segs if sg.step in ("plan", "ratify") and sg.at[1].isdigit()]
    acc = grouped(rounds, lambda sg: (sg.step, int(sg.at[1]), sg.provider))
    table(["step", "round", "provider", "segments", "API calls", "tokens consumed"],
          [[k[0], k[1], k[2], n, calls, sr.qualified(t)] for k, (n, calls, t) in sorted(acc.items(), key=lambda kv: (step_rank(kv[0][0]), kv[0][1], kv[0][2]))])


def print_context(segs):
    print("## Context at the start of a council call (new + cache read + cache write of its first API call)\n")
    acc = collections.OrderedDict()
    for sg in segs:
        acc.setdefault((sg.step, sg.provider), []).append(sg.c0)
    table(["step", "provider", "segments", "median", "p90", "max", "unknown"],
          [[k[0], k[1], len(v)] + spread(v) for k, v in sorted(acc.items(), key=lambda kv: (step_rank(kv[0][0]), kv[0][1]))])


def print_turns(segs, beyond):
    print("## API calls per council call\n")
    print("Tokens after call %d: what the API calls after the first %d of a council call consumed (provider counters).\n"
          % (beyond, beyond))
    acc = collections.OrderedDict()
    for sg in sorted(segs, key=lambda sg: sg.provider):
        row = acc.setdefault(sg.provider, [[], 0, [0, 0, 0, 0]])
        row[0].append(sg.turns)
        if sg.turns > beyond:
            row[1] += 1
            add(row[2], sr.consumed(sg.s.kind, sg.s.calls[sg.rng[0] + beyond:sg.rng[1]]))
    table(["provider", "segments", "median", "p90", "max", "segments over %d calls" % beyond, "tokens after call %d" % beyond],
          [[p, len(t)] + spread(t)[:3] + [n, sr.qualified(tok) if n else "0"] for p, (t, n, tok) in acc.items()])


def print_categories(runs):
    print("## Where the input tokens go - heuristic estimate (character-based; session_report's split, all runs)\n")
    cat = collections.Counter()
    for r in runs:
        if r.error is None:
            cat.update(r.analysis.all_cat)
    whole = sum(cat.values())
    table(["category", "tokens", "share"], [[k, sr.fmt(v), share(v, whole)] for k, v in cat.most_common()])
    print("### Harness prefix per API call - heuristic estimate (character-based)\n")
    acc = collections.OrderedDict()
    for r in runs:
        if r.error is not None:
            continue
        g_of = {id(g.session): g for g in r.gens if g.session}
        for s, res in r.analysis.sessions:
            if res[3]:
                acc.setdefault(provider_of(g_of[id(s)]), []).append(res[3])
    table(["provider", "sessions", "median", "max"], [[p, len(v)] + [spread(v)[0], spread(v)[2]] for p, v in sorted(acc.items())])


def item_segment(starts, start):
    """1-based segment of an item: the one holding the call that produced it (the call before `start`)."""
    return bisect.bisect_right(starts, max(start - 1, 0))


def print_tools(runs):
    print("## Tools called\n")
    acc = collections.Counter()
    for r in runs:
        if r.error is not None:
            continue
        g_of = {id(g.session): g for g in r.gens if g.session}
        for s, res in r.analysis.sessions:
            for it in s.items:
                if it[1].startswith("tool:"):
                    acc[(provider_of(g_of[id(s)]), it[1][5:])] += 1
    table(["provider", "tool", "calls"], [[p, t, n] for (p, t), n in sorted(acc.items(), key=lambda kv: (kv[0][0], -kv[1], kv[0][1]))])


def reads(r, segs):
    """(segment or None, member, normalized path, chars) of every file read of the run."""
    root = r.state.get("config", {}).get("dir") if isinstance(r.state.get("config"), dict) else None
    root = root if isinstance(root, str) and os.path.isabs(root) else None
    by_seg = {(id(sg.s), sg.j): sg for sg in segs if sg.run is r}
    out = []
    g_of = {id(g.session): g for g in r.gens if g.session}
    for s, res in r.analysis.sessions:
        starts = [x for x, y in sr.segment_ranges(s)]
        for it in s.items:
            if it[1] in READ_TOOLS:
                out.append((by_seg.get((id(s), item_segment(starts, it[0]))), g_of[id(s)], norm_path(it[2], root), it[3]))
    return out


def print_reads(runs, segs, cpt, top):
    print("## Files read more than once in the same session (paths relative to the run's project directory)\n")
    rows, extra, files = [], 0.0, 0
    shared = collections.OrderedDict()
    for r in runs:
        if r.error is not None:
            continue
        per = collections.OrderedDict()
        for sg, g, path, chars in reads(r, segs):
            v = per.setdefault((g.label, path), [0, 0])
            v[0] += 1
            v[1] += chars
            if sg is not None and sg.step == "plan" and sg.at[1] == "1":
                w = shared.setdefault((r.label, sr.disp(sg.at[0]), path), [set(), 0, 0])
                w[0].add(g.member)
                w[1] += 1
                w[2] += chars
        for (label, path), (n, chars) in per.items():
            if n > 1:
                files += 1
                extra += chars * (n - 1) / n / cpt
                rows.append((n, chars, r.label, label, path))
    print("Files read more than once in one session: %d; reads after the first ~ %s tokens at first reading "
          "(heuristic estimate; before re-sending).\n" % (files, sr.fmt(extra)))
    table(["run", "session", "file", "reads", "chars read"],
          [[run, label, "`%s`" % path.replace("|", "/"), n, sr.fmt(chars)] for n, chars, run, label, path in sorted(rows, key=lambda x: (-x[0], -x[1]))[:top]])
    print("## Files read by several members in round 1 of a task\n")
    many = [(len(m), n, chars, run, task, path) for (run, task, path), (m, n, chars) in shared.items() if len(m) > 1]
    print("Files read by two or more members in round 1: %d; their reads: %d; chars read: %s.\n"
          % (len(many), sum(x[1] for x in many), sr.fmt(sum(x[2] for x in many))))
    table(["run", "task", "file", "members", "reads", "chars read"],
          [[run, task, "`%s`" % path.replace("|", "/"), m, n, sr.fmt(chars)] for m, n, chars, run, task, path in sorted(many, key=lambda x: (-x[0], -x[1], -x[2]))[:top]])


def at_cells(at):
    return [sr.disp(v) for v in at[:3]]


def print_sessions(segs, top):
    print("## New sessions after a handover (generation 2 and later): their first council call\n")
    first = [sg for sg in segs if sg.gen is not None and sg.gen >= 2 and sg.j == 1]
    table(["run", "session", "provider", "context at start", "API calls", "tokens consumed", "task", "round", "phase"],
          [[sg.run.label, sg.g.label, sg.provider, "unknown" if sg.c0 is None else sr.fmt(sg.c0), sg.turns, sr.qualified(sg.tokc)]
           + at_cells(sg.at) for sg in sorted(first, key=lambda sg: -sg.tokc[0])[:top]])
    print("## Task boundaries inside a session (first council call of a task after another task)\n")
    rows, last = [], {}
    for sg in segs:   # already in run, session and segment order
        task = sg.at[0]
        if task == "unknown":
            continue
        prev = last.get(id(sg.s))
        if prev is not None and prev != task:
            rows.append([sg.run.label, sg.g.label, sr.disp(task), sg.j, "unknown" if sg.c0 is None else sr.fmt(sg.c0)])
        last[id(sg.s)] = task
    table(["run", "session", "task", "segment #", "context at start"], rows[:top])
    print("## Retries\n")
    retries = [sg for sg in segs if sg.step == "retry"]
    tokc = [0, 0, 0, 0]
    for sg in retries:
        add(tokc, sg.tokc)
    print("Retry council calls: %d; API calls: %d; tokens consumed: %s.\n"
          % (len(retries), sum(sg.turns for sg in retries), sr.qualified(tokc) if retries else "0"))
    table(["run", "session", "segment #", "API calls", "tokens consumed", "provenance"],
          [[sg.run.label, sg.g.label, sg.j, sg.turns, sr.qualified(sg.tokc), sg.at[3]] for sg in sorted(retries, key=lambda sg: -sg.tokc[0])[:top]])


def report(runs, a):
    ok = [r for r in runs if r.error is None]
    segs = [sg for r in ok for sg in segments(r)]
    print("# Token breakdown - %d run(s)\n" % len(runs))
    print("Provider counters are exact; categories, the harness prefix and file-read sizes are session_report's heuristic "
          "estimate (character-based). Unknown values are counted apart, never as 0. No prices or weights.\n")
    print_runs(runs)
    print_coverage(runs)
    print_providers(ok)
    print_steps(segs)
    print_rounds(segs)
    print_context(segs)
    print_turns(segs, a.beyond)
    print_categories(ok)
    print_tools(ok)
    print_reads(ok, segs, a.chars_per_token, a.top)
    print_sessions(segs, a.top)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0],
                                 epilog="Local by default: without --fetch-opencode no subprocess is started at all.")
    ap.add_argument("runs", nargs="+", metavar="RUN_DIR", help="council run directories (state.json, prompts/)")
    ap.add_argument("--projects", default=os.path.join(os.path.expanduser("~"), ".claude", "projects"),
                    help="where Claude transcripts live (default ~/.claude/projects)")
    ap.add_argument("--oc", default=os.path.join(HERE, "..", "oc.sh"),
                    help="the oc.sh adapter; only selects it, collection needs --fetch-opencode")
    ap.add_argument("--fetch-opencode", action="store_true",
                    help="collect OpenCode messages through oc.sh (never starts the service)")
    ap.add_argument("--chars-per-token", type=float, default=3.2, help="finite and > 0 (default 3.2)")
    ap.add_argument("--top", type=int, default=15, help="rows per long table, >= 1 (default 15)")
    ap.add_argument("--beyond", type=int, default=20,
                    help="API calls of a council call after which its tokens are also counted apart, >= 1 (default 20)")
    a = ap.parse_args(argv)
    try:                # the report is UTF-8 whatever the locale is
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    except (AttributeError, ValueError, OSError):
        pass
    if not math.isfinite(a.chars_per_token) or a.chars_per_token <= 0:
        ap.error("--chars-per-token must be a finite number > 0")
    if a.top < 1:
        ap.error("--top must be >= 1")
    if a.beyond < 1:
        ap.error("--beyond must be >= 1")
    runs = [read_run(p, a) for p in a.runs]
    if all(r.error is not None for r in runs):
        ap.exit(2, "".join("%s: error: %s: %s\n" % (ap.prog, r.label, sr.disp(r.error)) for r in runs))
    report(runs, a)


if __name__ == "__main__":
    main()
