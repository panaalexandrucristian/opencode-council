#!/usr/bin/env python3
"""Replay the handoff-test integration over a corpus of real handovers (offline, stdlib only).

  handoff_replay.py <corpus-dir> [--kit DIR] [--evidence DIR]

<corpus-dir>/index.json lists {file, source, project_dir, has_python_block}. Member notes
(project_dir set) go through the council path exactly as council.sh runs it (handoff_test.check_member:
section 4 disabled, --fix, mapped back onto the original bytes), each on its own copy. Session
handovers (project_dir null) go through the `council.sh handoff-test` path: the kit unchanged, no
--fix, with a disposable copy placed in the original source directory so the handoff's directory
and the kit's config discovery are the original ones; the copy is removed afterwards. The corpus is
never modified (checked by digest). A python block is never executed: every file is checked with
the kit's own block regex first, and a session document containing one is reported, not run.

Prints a per-file table and member/session aggregates next to <corpus-dir>/baseline-noconfig.tsv.
Exit 0 when every member note was tested and every delivered note equals the original with exactly
the kit's authorized repair (spans from its own plan_edits/banner rule, recorded per note) applied,
outside the gap checklist; 1 otherwise; 2 when no kit is found or the corpus is unreadable.
"""
import collections
import hashlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("handoff_test", HERE / "handoff_test.py")
ht = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ht)
BUDGET = 60  # the council's default handoff_timeout_s


def digest(root):
    h = hashlib.sha256()
    for p in sorted(Path(root).rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(root)).encode())
            h.update(p.read_bytes())
    return h.hexdigest()


def run_member(entry, corpus, work, kit):
    src = corpus / entry["file"]
    name = Path(entry["source"]).name  # handover-g<N>-<ID>.md, as council.sh names it
    raw = work / "raw"
    raw.mkdir(parents=True)
    note = raw / name
    shutil.copyfile(src, note)
    run_dir = str(Path(entry["source"]).parent.parent)
    res = ht.check_member(str(note), entry["project_dir"], run_dir if os.path.isdir(run_dir) else None,
                          str(work / "handoff"), kit_arg=kit, deadline=time.time() + BUDGET)
    staged = res.get("artifacts", {}).get("stage")
    sec4_blocks = None
    if staged and os.path.isfile(staged):
        sec4_blocks = len(ht.KIT_BLOCK.findall(Path(staged).read_text("utf-8", "surrogateescape").replace("\r\n", "\n")))
    orig = note.read_bytes()
    delivered = Path(res["delivered"]).read_bytes()
    rules = ht.kit_rules(res["kit"]["dir"]) if res.get("kit") and res["kit"].get("dir") else None
    auth = (res.get("fix") or {}).get("authorization")
    if delivered == orig:
        kept = True
    elif rules and auth:  # re-verified against the recorded authorized spans, never against token shapes
        kept = ht.preserved(ht.decode(orig), ht.decode(delivered), rules, auth)[0]
    else:
        kept = False
    return {"entry": entry, "res": res, "raw_unchanged": src.read_bytes() == orig,
            "staged_blocks": sec4_blocks, "outside_kept": kept}


def run_session(entry, corpus, kit, n):
    src = corpus / entry["file"]
    text = src.read_text("utf-8", "surrogateescape").replace("\r\n", "\n")
    out = {"entry": entry, "status": None, "reason": None, "check": None, "blocks": len(ht.KIT_BLOCK.findall(text))}
    if out["blocks"]:
        out.update(status="error", reason="contains a python block; not run (the replay never executes one)")
        return out
    home = Path(entry["source"]).parent
    copy = home / (".handoff-replay-%d-%d-%s" % (os.getpid(), n, Path(entry["source"]).name))
    try:
        shutil.copyfile(src, copy)
    except OSError as e:
        out.update(status="error", reason="cannot stage a copy in the original directory %s: %s" % (home, e))
        return out
    try:
        rc, so, se = ht.run(["bash", os.path.join(kit, "handoff-test.sh"), str(copy)], time.time() + BUDGET)
        if rc == 3:
            out.update(status="error", reason="kit exit 3: %s" % (se.strip().splitlines() or ["?"])[-1])
        else:
            out.update(status="tested", check=ht.parse(so, rc))
    except ht.Failure as e:
        out.update(status="error", reason=str(e))
    finally:
        try:
            copy.unlink()
        except OSError as e:
            out["reason"] = (out["reason"] or "") + " (could not remove staged copy %s: %s)" % (copy, e)
    return out


def chk(c):
    return "-" if not c else "%d/%d" % (c["raw_rc"], c["effective_rc"])


def secs(c):
    if not c:
        return "-"
    return " ".join("%s:%dF%dG" % (n, s["FAIL"], s["GAP"]) for n, s in sorted(c["sections"].items()))


def baseline(corpus):
    rows = []
    p = corpus / "baseline-noconfig.tsv"
    if p.is_file():
        for line in p.read_text("utf-8").splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                rows.append({"rc": int(parts[0]), "result": parts[1], "source": parts[2]})
    return rows


def main(argv):
    kit_arg, evidence, pos = None, None, []
    while argv:
        a = argv.pop(0)
        if a in ("--kit", "--evidence") and argv:
            v = argv.pop(0)
            if a == "--kit":
                kit_arg = v
            else:
                evidence = v
        elif a.startswith("-"):
            print(__doc__, file=sys.stderr)
            return 2
        else:
            pos.append(a)
    if len(pos) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    corpus = Path(pos[0]).resolve()
    try:
        index = json.loads((corpus / "index.json").read_text("utf-8"))
    except (OSError, ValueError) as e:
        print("handoff_replay: cannot read %s/index.json: %s" % (corpus, e), file=sys.stderr)
        return 2
    kit, source, why = ht.locate(kit_arg)
    if not kit:
        print("handoff_replay: %s" % why, file=sys.stderr)
        return 2
    before_digest = digest(corpus)
    print("kit: %s (source %s, revision %s)" % (kit, source, ht.revision(kit)))
    print("corpus: %s — %d index entries (%d member notes, %d session handovers)" % (
        corpus, len(index), sum(1 for e in index if e["project_dir"]), sum(1 for e in index if not e["project_dir"])))
    dup = sorted(f for f, c in collections.Counter(e["file"] for e in index).items() if c > 1)
    print("distinct corpus files: %d%s" % (len({e["file"] for e in index}), "" if not dup else
          " — several index entries share one corpus file (each entry is replayed with its own source/run dir): " + ", ".join(dup)))
    members, sessions = [], []
    with tempfile.TemporaryDirectory(prefix="handoff-replay.") as tmp:
        for n, entry in enumerate(index):
            if entry["project_dir"]:
                members.append(run_member(entry, corpus, Path(tmp) / ("%02d" % n), kit_arg or kit))
            else:
                sessions.append(run_session(entry, corpus, kit, n))
        if evidence:
            ev = Path(evidence)
            ev.mkdir(parents=True, exist_ok=True)
            for n, m in enumerate(members, 1):
                d = ev / "member-notes" / ("%02d-%s" % (n, Path(m["entry"]["file"]).stem))
                if d.exists():
                    shutil.rmtree(d)
                shutil.copytree(m["res"]["artifacts"]["dir"], d)
            (ev / "replay.json").write_text(json.dumps({"members": members, "sessions": sessions}, indent=1,
                                                       ensure_ascii=False, default=str) + "\n", "utf-8")
    after_digest = digest(corpus)

    print()
    print("MEMBER NOTES — council path (section 4 %s; --fix; delivered = original bytes + mapped repairs)" % ht.SECTION4)
    print("before/after = kit exit / effective exit (section-4 gap excluded); sections = FAIL/GAP counts per section")
    print("| # | file | status | before | after | sections before | --fix changed | sec4 blocks staged | sec4 entries | only authorized bytes changed | raw copy unchanged | FP |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for n, m in enumerate(members, 1):
        r = m["res"]
        fx = r.get("fix") or {}
        changed = "; ".join((fx.get("kit_changes") or []) + (["sec4 entry removed"] if fx.get("section4_items_removed") else [])) or "-"
        sec4 = ",".join((r.get("before") or {}).get("section4_entries") or []) or "-"
        print("| %d | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %d |" % (
            n, Path(m["entry"]["file"]).name, r["status"] + (" (%s)" % r["reason"] if r["status"] in ("error", "timeout", "skipped") else ""),
            chk(r.get("before")), chk(r.get("after")), secs(r.get("before")), changed,
            m["staged_blocks"], sec4, "yes" if m["outside_kept"] else "NO", "yes" if m["raw_unchanged"] else "NO",
            len(r.get("false_positives") or [])))
    print()
    print("SESSION HANDOVERS — `council.sh handoff-test` path (kit unchanged, no --fix; after = unchanged/check-only)")
    print("| # | file | status | kit exit | result | sections | python blocks |")
    print("|---|---|---|---|---|---|---|")
    for n, s in enumerate(sessions, 1):
        c = s["check"]
        print("| %d | %s | %s | %s | %s | %s | %d |" % (n, Path(s["entry"]["file"]).name,
              s["status"] + (" (%s)" % s["reason"] if s["reason"] else ""), c["raw_rc"] if c else "-",
              c["result_line"] if c else "-", secs(c), s["blocks"]))

    tested = [m for m in members if m["res"]["status"] in ("passed", "failed", "fixed")]
    untested = [m for m in members if m not in tested]
    rc_b = collections.Counter(m["res"]["before"]["raw_rc"] for m in tested)
    eff_b = collections.Counter(m["res"]["before"]["effective_rc"] for m in tested)
    eff_a = collections.Counter((m["res"]["after"] or m["res"]["before"])["effective_rc"] for m in tested)
    gaps_b = collections.Counter(g for m in tested for g in m["res"]["before"]["gaps"])
    sec4 = collections.Counter(g for m in tested for g in m["res"]["before"]["section4_gaps"])
    unres = sum(1 for m in tested for f in m["res"]["before"]["failures"] if f.startswith("unresolvable: "))
    fps = collections.Counter(f["kind"] for m in tested for f in m["res"]["false_positives"])
    status = collections.Counter(m["res"]["status"] for m in members)
    executed = sum(1 for m in members if "ok" in ((m["res"].get("before") or {}).get("section4_entries") or [])
                   or "FAIL" in ((m["res"].get("before") or {}).get("section4_entries") or []))
    kept = sum(1 for m in members if m["outside_kept"])
    fmt = lambda c: ", ".join("exit %s: %d" % (k, c[k]) for k in sorted(c)) or "-"
    print()
    print("AGGREGATE — member notes")
    print("  member notes in index: %d · tested: %d · not tested: %d%s" % (
        len(members), len(tested), len(untested),
        "" if not untested else " (" + "; ".join("%s: %s" % (Path(m["entry"]["file"]).name, m["res"]["reason"]) for m in untested) + ")"))
    print("  status: %s" % ", ".join("%s %d" % kv for kv in sorted(status.items())))
    print("  kit exit before (section 4 disabled): %s" % fmt(rc_b))
    print("  effective exit before: %s" % fmt(eff_b))
    print("  effective exit after --fix: %s" % fmt(eff_a))
    print("  section 4 executed: %d of %d (python blocks in staged copies: %d; section-4 result in every tested note: %s)" % (
        executed, len(members), sum(m["staged_blocks"] or 0 for m in members),
        ", ".join(sorted(set(",".join((m["res"]["before"] or {}).get("section4_entries") or []) for m in tested)))))
    print("  section-4 gaps excluded from the verdict: %s" % (dict(sec4) or "-"))
    auths = [(m["res"].get("fix") or {}).get("authorization") for m in members]
    print("  delivered notes = original + exactly the authorized repair (outside the gap checklist): %d of %d" % (kept, len(members)))
    print("  authorized repair spans (kit plan_edits / banner rule): %d fact edit(s), %d banner(s) in %d planned note(s)" % (
        sum(len(a["edits"]) for a in auths if a), sum(1 for a in auths if a and a["banner"]), sum(1 for a in auths if a)))
    print("  delivered: fixed note %d, original %d" % (sum(1 for m in members if m["res"]["fixed"]),
                                                       sum(1 for m in members if not m["res"]["fixed"])))
    print("  'unresolvable' path failures before: %d (probable kit false positives: %s)" % (
        unres, ", ".join("%s %d" % kv for kv in sorted(fps.items())) or "none"))
    print("  coverage gaps before (sections 1-3, 5): %s" % ", ".join("%s %d" % kv for kv in gaps_b.most_common()))
    base = baseline(corpus)
    bc = collections.Counter(r["rc"] for r in base)
    print()
    print("BASELINE — %s/baseline-noconfig.tsv (kit, no config, section 4 enabled, run from a copy directory)" % corpus)
    print("  rows: %d · %s" % (len(base), fmt(bc)))
    print("  task statement: 27 tested, 17 exit 1, 10 exit 2, 0 exit 0; 179 unresolvable; gaps: what is half-finished 26,"
          " mistakes not to repeat 26, the deliverable 25, runnable verification block 27")
    print("  denominators differ: the baseline tested %d files, this replay %d member notes + %d session handovers;"
          " the baseline had no repo_root and counted the section-4 gap" % (len(base), len(tested), len(sessions)))
    ts = [s for s in sessions if s["check"]]
    print()
    print("AGGREGATE — session handovers: %d tested, %d not tested; kit exit: %s; python blocks found: %d (none executed by the replay)" % (
        len(ts), len(sessions) - len(ts), fmt(collections.Counter(s["check"]["raw_rc"] for s in ts)), sum(s["blocks"] for s in sessions)))
    print()
    print("corpus unchanged: %s (sha256 before %s…, after %s…)" % ("yes" if before_digest == after_digest else "NO",
                                                                   before_digest[:12], after_digest[:12]))
    ok = not untested and kept == len(members) and before_digest == after_digest and executed == 0
    print("replay verdict: %s" % ("OK" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
