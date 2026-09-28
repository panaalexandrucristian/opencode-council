#!/usr/bin/env python3
"""Run the external handoff-test-kit on handover documents (stdlib only; the kit is never copied).

  handoff_test.py member NOTE --project-dir D --run-dir R --out-dir O [--kind handover|replacement]
                  [--check-only] [--kit DIR] [--kit-field DIR] [--user-config C] [--deadline EPOCH]
      A council member note (council.sh do_handover / replace_member). The kit runs on a copy in O
      from which every `python3 - <<'PY' ... PY` block is removed, so section 4 never executes; a
      section-4 gap does not count ("not applicable to member notes"). A failing note is repaired
      with the kit's --fix, the section-4 checklist entry is dropped, the repair is mapped back onto
      the ORIGINAL bytes (python blocks restored byte-for-byte) and only authorized spans may differ.
      Writes O/<stem>.result.json (also printed). Exit 0 whenever a result was recorded.

  handoff_test.py session [--fix] [--config c.json] [--kit DIR] [--timeout S] FILE
      A session handover (council.sh handoff-test). The kit runs unchanged, section 4 included, and
      its exit code 0/1/2/3 is passed through; 3 on a usage error or timeout; 4 when no kit is found.

Kit lookup: --kit > council.json handoff_kit > $HANDOFF_TEST_KIT > /Users/apana/dev/handoff-test-kit
(used only if it exists). A directory is a kit only with handoff-test.sh and handoff-fix.py; an
explicitly set source that is invalid is a named skip and never falls back to a later one.
"""
import datetime
import difflib
import json
import os
import re
import runpy
import signal
import subprocess
import sys
import time

DEFAULT_KIT = "/Users/apana/dev/handoff-test-kit"
SECTION4 = "not applicable to member notes"
KIT_BLOCK = re.compile(r"python3 - <<'PY'\n(.*?)\nPY$", re.S | re.M)       # the kit's own (handoff-test.sh `block`)
STRIP = re.compile(r"python3 - <<'PY'\r?\n.*?\r?\nPY(?=\r?\n|\Z)", re.S)  # the same block, CRLF-aware, on the original
HEADER = re.compile(r"^== ([1-5])\. .* ==$")
ENTRY = re.compile(r"^  (ok|FAIL|skip|info|GAP) +(.*)$")
RESULT = re.compile(r"^result: (\d+) ok · (\d+) fact failures · (\d+) coverage gaps · (\d+) skipped$")
FIX_START = "== repairing derivable facts =="
RECHECK = "== re-verifying =="
FP_ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\S*\s+\S")
FP_GIT = re.compile(r"^ ?(?:[MADRCU]{1,2}|\?\?|!!) +\S")


class Failure(Exception):
    """An operational failure: the original note is delivered and the reason is recorded."""


class Timeout(Failure):
    pass


# ------------------------------------------------------------------ kit lookup ----
def locate(kit_arg=None, field=None, env=None, default=DEFAULT_KIT):
    """-> (dir or None, source, reason). field=None means council.json has no handoff_kit."""
    env = os.environ if env is None else env
    if kit_arg is not None:
        source, path = "--kit", kit_arg
    elif field is not None:
        source, path = "council.json handoff_kit", field
    elif "HANDOFF_TEST_KIT" in env:
        source, path = "HANDOFF_TEST_KIT", env["HANDOFF_TEST_KIT"]
    elif os.path.isdir(default):
        source, path = "default", default
    else:
        return None, "none", ("kit not found: handoff_kit is not set, HANDOFF_TEST_KIT is not set and the "
                              "default %s does not exist" % default)
    if not path:
        return None, source, "kit not found: %s is set but empty (no fallback)" % source
    if not os.path.isdir(path):
        return None, source, "kit not found: %s=%s is not a directory (no fallback)" % (source, path)
    missing = [f for f in ("handoff-test.sh", "handoff-fix.py") if not os.path.isfile(os.path.join(path, f))]
    if missing:
        return None, source, "kit not found: %s=%s lacks %s (no fallback)" % (source, path, " and ".join(missing))
    return os.path.abspath(path), source, None


def revision(kit):
    try:
        r = subprocess.run(["git", "-C", kit, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def kit_rules(kit):
    """The kit's own span rules (gap_spans, SHA, BANNER), loaded like handoff-test.sh does."""
    try:
        f = runpy.run_path(os.path.join(kit, "handoff-fix.py"))
        return {k: f[k] for k in ("gap_spans", "SHA", "BANNER", "GAP_HEAD")}
    except (OSError, SyntaxError, KeyError, SystemExit, Exception) as e:  # any revision may differ
        raise Failure("cannot load the kit's span rules from handoff-fix.py: %s" % e)


# ------------------------------------------------------------- process runs ----
_CHILD = []


def _on_term(signum, frame):
    for p in _CHILD:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
    sys.exit(128 + signum)


def run(cmd, deadline=None, env=None, cwd=None):
    """-> (rc, stdout, stderr). The child runs in its own process group, killed whole on timeout."""
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                         start_new_session=True, env=env, cwd=cwd)
    _CHILD.append(p)
    try:
        left = None if deadline is None else max(0.1, deadline - time.time())
        out, err = p.communicate(timeout=left)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        p.communicate()
        raise Timeout("timeout: the handoff test exceeded its budget (process group killed)")
    finally:
        _CHILD.remove(p)
    return p.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def kit_env():
    env = dict(os.environ)
    env.pop("HANDOFF_TEST_CONFIG", None)  # the integration always names its config explicitly
    return env


# ------------------------------------------------------------------ parsing ----
def parse(text, rc=None):
    """One check's output -> dict; raises Failure if it does not match the kit's output contract.
    rc=None: a check embedded in --fix output, whose own exit code is not observable."""
    if rc is not None and rc not in (0, 1, 2):
        raise Failure("unrecognised kit output: exit code %s is outside 0..3" % rc)
    sections, order, cur, result = {}, [], None, None
    for line in text.splitlines():
        m = HEADER.match(line)
        if m:
            cur = m.group(1)
            if cur in sections:
                raise Failure("unrecognised kit output: section %s appears twice" % cur)
            order.append(cur)
            sections[cur] = []
            continue
        m = RESULT.match(line)
        if m:
            if result is not None:
                raise Failure("unrecognised kit output: more than one result line")
            result = (line, tuple(int(g) for g in m.groups()))
            cur = None
            continue
        m = ENTRY.match(line)
        if m and cur is not None:
            sections[cur].append((m.group(1), m.group(2)))
    if order != ["1", "2", "3", "4", "5"]:
        raise Failure("unrecognised kit output: sections %s, expected == 1. to == 5." % (order or "none"))
    if result is None:
        raise Failure("unrecognised kit output: no 'result:' line")
    count = lambda k: sum(1 for s in sections.values() for e in s if e[0] == k)
    if (count("ok"), count("FAIL"), count("GAP"), count("skip")) != result[1]:
        raise Failure("unrecognised kit output: result line %r disagrees with the listed entries" % result[0])
    want = 1 if count("FAIL") else 2 if count("GAP") else 0
    if rc is not None and rc != want:
        raise Failure("unrecognised kit output: exit %d but its result line implies %d" % (rc, want))
    eff_gaps = [msg for n, s in sections.items() if n != "4" for k, msg in s if k == "GAP"]
    return {
        "raw_rc": want if rc is None else rc,
        "effective_rc": 1 if count("FAIL") else 2 if eff_gaps else 0,
        "result_line": result[0],
        "sections": {n: {k: sum(1 for e in s if e[0] == k) for k in ("ok", "FAIL", "GAP", "skip")} for n, s in sections.items()},
        "failures": [msg for s in sections.values() for k, msg in s if k == "FAIL"],
        "gaps": eff_gaps,
        "section4_gaps": [msg for k, msg in sections["4"] if k == "GAP"],
        "section4_entries": [k for k, _ in sections["4"]],
    }


def split_fix(text):
    """--fix output -> (first check, fixer output, recheck) or raises Failure."""
    if FIX_START not in text:
        raise Failure("unrecognised kit output: --fix printed no '%s'" % FIX_START)
    first, rest = text.split(FIX_START, 1)
    if RECHECK not in rest:
        raise Failure("the kit's fixer did not complete (no '%s')" % RECHECK)
    fixer, again = rest.split(RECHECK, 1)
    return first, fixer, again


def false_positives(failures):
    out = []
    for msg in failures:
        if not msg.startswith("unresolvable: "):
            continue
        p = msg[len("unresolvable: "):]
        if FP_ENV.match(p):
            out.append({"kind": "environment assignment + command", "path": p})
        elif FP_GIT.match(p):
            out.append({"kind": "git-status prefix", "path": p})
    return out


# ------------------------------------------------------------ bytes and spans ----
def decode(b):
    return b.decode("utf-8", "surrogateescape")


def encode(s):
    return s.encode("utf-8", "surrogateescape")


def strip_blocks(text):
    """-> (stripped, keep) where keep[i] is the original offset of stripped[i]. Raises Failure when a
    block the kit could run survives (never hand the kit something it could execute)."""
    cur, keep = text, list(range(len(text)))
    while True:
        m = STRIP.search(cur)
        if not m:
            break
        cur, keep = cur[:m.start()] + cur[m.end():], keep[:m.start()] + keep[m.end():]
    if KIT_BLOCK.search(cur.replace("\r\n", "\n")):
        raise Failure("a python block could not be removed from the staged copy; the kit was not run")
    return cur, keep


def removed_spans(n, keep):
    spans, prev = [], 0
    for k in keep + [n]:
        if k > prev:
            spans.append((prev, k))
        prev = k + 1
    return spans


def line_bounds(s):
    out, pos = [], 0
    while pos < len(s):
        nl = s.find("\n", pos)
        end = len(s) if nl < 0 else nl + 1
        out.append((pos, end))
        pos = end
    return out


def map_back(orig, stripped, keep, fixed):
    """Apply the stripped->fixed edits to the original, python blocks restored where they were.
    Raises Failure when an edit touches a removed block (the mapping would not be exact)."""
    if fixed == stripped:
        return orig
    gone = removed_spans(len(orig), keep)
    if not gone:
        return fixed
    if not stripped:
        raise Failure("cannot map the repair back: the note consists only of python blocks")
    a, b = line_bounds(stripped), line_bounds(fixed)
    sm = difflib.SequenceMatcher(None, [stripped[x:y] for x, y in a], [fixed[x:y] for x, y in b], autojunk=False)

    def span(i1, i2):  # stripped lines -> original bytes, blocks attached to the line that follows them
        s, e = a[i1][0], a[i2 - 1][1]
        return (0 if s == 0 else keep[s - 1] + 1), (len(orig) if e == len(stripped) else keep[e - 1] + 1)

    out = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        new = fixed[b[j1][0]:b[j2 - 1][1]] if j2 > j1 else ""
        if i2 > i1:
            s, e = span(i1, i2)
            if tag == "equal":
                out.append(orig[s:e])
                continue
            if any(x < e and s < y for x, y in gone):
                raise Failure("cannot map the repair back exactly: the kit changed text next to a removed python block")
        out.append(new)
    result = "".join(out)
    again, kept = strip_blocks(result)
    if again != fixed or [orig[x:y] for x, y in gone] != [result[x:y] for x, y in removed_spans(len(result), kept)]:
        raise Failure("cannot map the repair back exactly: restored note does not reproduce the kit's repair")
    return result


def drop_items(s, items, rules):
    """Remove the given '- [ ] ' entries from the kit's generated checklist; a checklist left with no
    entry is removed with the single line ending in front of it (the kit's own removal rule)."""
    if not items:
        return s, 0
    dropped = 0
    for a, b in reversed(rules["gap_spans"](s)):
        block = s[a:b]
        kept, entries = [], 0
        for x, y in line_bounds(block):
            ln = block[x:y]
            body = ln.rstrip("\r\n")
            if body.startswith("- [ ] ") and body[len("- [ ] "):] in items:
                dropped += 1
                continue
            entries += body.startswith("- [ ] ")
            kept.append(ln)
        if entries:
            s = s[:a] + "".join(kept) + s[b:]
            continue
        cut = a
        if a >= 2 and s[a - 2:a] == "\r\n":
            cut = a - 2
        elif a >= 1 and s[a - 1] == "\n":
            cut = a - 1
        s = s[:cut] + s[b:]
    return s, dropped


def outside(s, rules):
    """The text outside the kit's generated checklists (each with the one line ending before it)."""
    out, pos = [], 0
    for a, b in rules["gap_spans"](s):
        cut = a
        if a - 2 >= pos and s[a - 2:a] == "\r\n":
            cut = a - 2
        elif a - 1 >= pos and s[a - 1] == "\n":
            cut = a - 1
        out.append(s[pos:cut])
        pos = b
    out.append(s[pos:])
    return "".join(out)


# Asks the kit's own fixer, with the same config and the same staged copy, which spans it may repair
# (plan_edits: SHA tokens on lines naming the configured branch/MR that are real commits, version
# claims captured by a selected probe) and whether its banner precondition (the branch merged, no
# banner yet) holds. Run as a child so the kit's git calls sit inside the budget and its process group.
PLAN = r"""
import json, os, runpy, sys
kit, cfg, stage = sys.argv[1:4]
k = runpy.run_path(os.path.join(kit, "handoff-fix.py"))
try:
    with open(stage, "rb") as f:
        s = f.read().decode("utf-8", "surrogateescape")
    c, d = k["load_config"](stage, cfg)
    root, ext, probes = k["validate"](c, d, os.path.abspath(stage))
    facts = k["facts"](root, ext, probes)
    edits, view = k["plan_edits"](s, facts)
except k["Error"] as e:
    sys.stderr.write("%s\n" % e)
    sys.exit(3)
json.dump({"edits": [[a, b, new, why] for a, b, new, why in edits],
           "banner": bool(facts.get("merged")) and k["MARK"] not in view}, sys.stdout)
"""


def plan_fix(kit, cfg, stage, deadline):
    """-> {"edits": [[a, b, new, why]], "banner": bool} in staged coordinates, from the kit's fact rules."""
    rc, so, se = run([sys.executable, "-c", PLAN, kit, cfg, stage], deadline, kit_env())
    if rc == 3:
        raise Failure("the kit's fixer refused to plan a repair: %s" % (se.strip().splitlines() or ["?"])[-1])
    try:
        plan = json.loads(so) if rc == 0 else None
        ok = (isinstance(plan, dict) and isinstance(plan.get("banner"), bool) and isinstance(plan.get("edits"), list)
              and all(isinstance(e, list) and len(e) == 4 and type(e[0]) is int and type(e[1]) is int
                      and isinstance(e[2], str) for e in plan["edits"]))
    except ValueError:
        ok = False
    if not ok:
        raise Failure("unrecognised kit fixer interface: cannot derive the authorized repair spans (%s)"
                      % ((se.strip().splitlines() or ["exit %s" % rc])[-1]))
    return plan


def authorize(orig, stripped, keep, plan, rules):
    """The kit's planned repair, in staged AND original coordinates: exact spans, old and new values,
    and the banner insertion point only when the kit's banner precondition holds."""
    edits = []
    for a, b, new, why in plan["edits"]:
        if not 0 <= a < b <= len(stripped):
            raise Failure("unrecognised kit fixer interface: repair span %d..%d is outside the note" % (a, b))
        oa, ob = keep[a], keep[b - 1] + 1
        if ob - oa != b - a or orig[oa:ob] != stripped[a:b]:
            raise Failure("cannot map the repair back exactly: repair span %d..%d crosses a removed python block" % (a, b))
        edits.append({"stage": [a, b], "original": [oa, ob], "old": stripped[a:b], "new": new, "why": why})
    banner = None
    if plan["banner"]:
        m = re.match(r"# [^\r\n]*\r?\n\r?\n", stripped)  # where handoff-fix.py inserts it
        at = m.end() if m else 0
        nl = "\r\n" if re.match(r"[^\n]*\r\n", stripped) else "\n"
        banner = {"stage": at, "original": 0 if at == 0 else keep[at - 1] + 1, "text": rules["BANNER"] + nl + nl}
    return {"edits": edits, "banner": banner}


def apply(text, auth, coord):
    """text with exactly the authorized repair applied (coord: "stage" or "original")."""
    ops = [(e[coord][0], 1, e[coord][1], e["new"]) for e in auth["edits"]]
    if auth["banner"]:
        ops.append((auth["banner"][coord], 0, auth["banner"][coord], auth["banner"]["text"]))
    for a, _, b, new in sorted(ops, reverse=True):  # at one offset the edit first, then the banner before it
        text = text[:a] + new + text[b:]
    return text


def preserved(before, after, rules, auth=None, coord="original"):
    """-> (ok, authorized edits). Outside the kit's generated checklist, after must equal before with
    exactly the authorized repair applied (auth; None = nothing authorized): every other byte, every
    other SHA-, version- or number-shaped token included, must be identical."""
    auth = auth or {"edits": [], "banner": None}
    if outside(apply(before, auth, coord), rules) != outside(after, rules):
        return False, []
    edits = ["%s -> %s at %s %d..%d (%s)" % (e["old"], e["new"], coord, e[coord][0], e[coord][1], e["why"])
             for e in auth["edits"]]
    if auth["banner"]:
        edits.append("banner inserted at %s %d" % (coord, auth["banner"][coord]))
    return True, edits


# --------------------------------------------------------------- member notes ----
def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def stage_config(out, stem, project_dir, run_dir, user_config):
    if user_config:
        p = user_config if os.path.isabs(user_config) else os.path.join(project_dir, user_config)
        try:
            with open(p, encoding="utf-8") as f:
                cfg = json.load(f)
        except (OSError, ValueError) as e:
            raise Failure("handoff_config invalid: %s (%s)" % (p, e))
        if not isinstance(cfg, dict):
            raise Failure("handoff_config invalid: %s is not a JSON object" % p)
        root = cfg.get("repo_root")
        if root is None:
            cfg["repo_root"] = project_dir
        elif isinstance(root, str) and root and not os.path.isabs(os.path.expanduser(root)):
            cfg["repo_root"] = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(p)), root))
    else:
        cfg = {"repo_root": project_dir, "path_bases": [run_dir] if run_dir else []}
    path = os.path.join(out, stem + ".config.json")
    write_json(path, cfg)
    return path


def check_member(note, project_dir, run_dir, out, kind="handover", fix=True, kit_arg=None, kit_field=None,
                 user_config=None, deadline=None, env=None):
    t0 = time.time()
    stem = os.path.splitext(os.path.basename(note))[0]
    os.makedirs(out, exist_ok=True)
    res = {"version": 1, "kind": kind, "note": note, "delivered": note, "fixed": False, "status": None,
           "reason": None, "section4": SECTION4, "kit": None, "config": None, "blocks_removed": 0,
           "blocks_restored": 0, "before": None, "fix": None, "after": None, "false_positives": [],
           "artifacts": {"dir": out}}
    try:
        if kit_field and not os.path.isabs(kit_field):  # integration-owned relative paths: relative to config.dir
            kit_field = os.path.join(project_dir, kit_field)
        kit, source, why = locate(kit_arg, kit_field, env)
        res["kit"] = {"dir": kit, "source": source, "revision": revision(kit) if kit else None}
        if not kit:
            res.update(status="skipped", reason=why)
            return finish(res, out, stem, t0)
        with open(note, "rb") as f:
            raw = f.read()
        orig = decode(raw)
        stripped, keep = strip_blocks(orig)
        res["blocks_removed"] = len(removed_spans(len(orig), keep))
        stage = os.path.join(out, stem + ".stage.md")
        with open(stage, "wb") as f:
            f.write(encode(stripped))
        res["artifacts"]["stage"] = stage
        cfg = stage_config(out, stem, project_dir, run_dir, user_config)
        res["config"] = cfg
        script = os.path.join(kit, "handoff-test.sh")
        rc, so, se = run(["bash", script, "--config", cfg, stage], deadline, kit_env())
        save(out, stem + ".check", so, se, res)
        exit3(rc, se)
        before = parse(so, rc)
        res["before"] = before
        res["false_positives"] = false_positives(before["failures"])
        if before["effective_rc"] == 0:
            res["status"] = "passed"
            return finish(res, out, stem, t0)
        if not fix:
            res["status"] = "failed"
            res["reason"] = "check only (replacement notes are never altered)"
            return finish(res, out, stem, t0)
        rules = kit_rules(kit)
        auth = authorize(orig, stripped, keep, plan_fix(kit, cfg, stage, deadline), rules)
        rc, so, se = run(["bash", script, "--fix", "--config", cfg, stage], deadline, kit_env())
        save(out, stem + ".fix", so, se, res)
        exit3(rc, se)
        baks = [os.path.join(out, n) for n in os.listdir(out) if n.startswith(stem + ".stage.md.bak-")]
        for p in baks:
            os.remove(p)
        first, fixer, again = split_fix(so)
        parse(first)  # the first half must be a well-formed check too
        after = parse(again, rc)
        with open(stage, "rb") as f:
            fixed_stage = decode(f.read())
        normal, dropped = drop_items(fixed_stage, set(before["section4_gaps"]), rules)
        res["fix"] = {"attempted": True, "kit_changes": [l.strip()[2:] for l in fixer.splitlines() if l.strip().startswith("- ")],
                      "kit_backups_deleted": len(baks), "section4_items_removed": dropped, "changed": False,
                      "authorization": auth, "authorized_edits": [], "outside_preserved": None, "diff": None}
        res["after"] = after
        if not preserved(stripped, fixed_stage, rules, auth, "stage")[0]:
            res["fix"]["outside_preserved"] = False
            raise Failure("the kit's --fix changed bytes outside the kit's authorized repair spans and gap checklist; not delivered")
        delivered = map_back(orig, stripped, keep, normal)
        ok, edits = preserved(orig, delivered, rules, auth)
        res["fix"]["outside_preserved"] = ok
        res["fix"]["authorized_edits"] = edits
        if not ok:
            raise Failure("the repair changed bytes outside the kit's authorized repair spans and gap checklist; not delivered")
        res["blocks_restored"] = len(removed_spans(len(delivered), strip_blocks(delivered)[1]))
        if delivered == orig:
            res["status"] = "failed"
            res["reason"] = "the kit's --fix changed nothing deliverable"
            return finish(res, out, stem, t0)
        fixed_path = os.path.join(out, stem + ".fixed.md")
        with open(fixed_path, "wb") as f:
            f.write(encode(delivered))
        diff = os.path.join(out, stem + ".fix.diff")
        with open(diff, "w", encoding="utf-8", errors="surrogateescape") as f:
            f.writelines(difflib.unified_diff(orig.splitlines(True), delivered.splitlines(True), note, fixed_path))
        res["fix"].update(changed=True, diff=diff)
        res.update(status="fixed", fixed=True, delivered=fixed_path)
    except Timeout as e:
        res.update(status="timeout", reason=str(e), fixed=False, delivered=note)
    except Failure as e:
        res.update(status="error", reason=str(e), fixed=False, delivered=note)
    except (OSError, UnicodeError) as e:
        res.update(status="error", reason="operational error: %s" % e, fixed=False, delivered=note)
    return finish(res, out, stem, t0)


def exit3(rc, se):
    if rc == 3:
        raise Failure("kit exit 3: %s" % (se.strip().splitlines() or ["(no message on stderr)"])[-1])
    if rc not in (0, 1, 2):
        raise Failure("unrecognised kit output: exit code %s is outside 0..3" % rc)


def save(out, base, so, se, res):
    for ext, data in (("out", so), ("err", se)):
        p = os.path.join(out, "%s.%s" % (base, ext))
        with open(p, "w", encoding="utf-8") as f:
            f.write(data)
        res["artifacts"][base.rsplit(".", 1)[-1] + "_" + ext] = p


def finish(res, out, stem, t0):
    res["elapsed_s"] = round(time.time() - t0, 3)
    path = os.path.join(out, stem + ".result.json")
    res["artifacts"]["result"] = path
    write_json(path, res)
    return res


# ------------------------------------------------------------------ session ----
def session(argv):
    fix, config, kit_arg, timeout, file = False, None, None, None, None
    usage = "usage: council.sh handoff-test [--fix] [--config c.json] [--kit DIR] [--timeout S] FILE"
    while argv:
        a = argv.pop(0)
        if a == "--fix":
            fix = True
        elif a in ("--config", "--kit", "--timeout"):
            if not argv or not argv[0]:
                print("handoff-test: %s needs a value\n%s" % (a, usage), file=sys.stderr)
                return 3
            v = argv.pop(0)
            if a == "--config":
                config = v
            elif a == "--kit":
                kit_arg = v
            else:
                if not re.fullmatch(r"[1-9][0-9]*", v):
                    print("handoff-test: --timeout must be a positive integer (seconds)", file=sys.stderr)
                    return 3
                timeout = int(v)
        elif a in ("-h", "--help"):
            print(usage)
            return 0
        elif a.startswith("-"):
            print("handoff-test: unknown option: %s\n%s" % (a, usage), file=sys.stderr)
            return 3
        elif file is None:
            file = a
        else:
            print("handoff-test: only one FILE may be given\n%s" % usage, file=sys.stderr)
            return 3
    if file is None:
        print(usage, file=sys.stderr)
        return 3
    kit, source, why = locate(kit_arg)
    if not kit:
        print("handoff-test: skipped — %s" % why, file=sys.stderr)
        return 4
    cmd = ["bash", os.path.join(kit, "handoff-test.sh")] + (["--fix"] if fix else []) + (["--config", config] if config else []) + [file]
    if timeout is None:
        return subprocess.call(cmd)
    p = subprocess.Popen(cmd, start_new_session=True)
    try:
        return p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        p.wait()
        print("handoff-test: timeout after %ss (the kit's process group was killed)" % timeout, file=sys.stderr)
        return 3


def member(argv):
    opts = {"kind": "handover", "fix": True, "kit_arg": None, "kit_field": None, "user_config": None, "deadline": None}
    pos = []
    names = {"--project-dir": "project_dir", "--run-dir": "run_dir", "--out-dir": "out", "--kind": "kind",
             "--kit": "kit_arg", "--kit-field": "kit_field", "--user-config": "user_config", "--deadline": "deadline"}
    while argv:
        a = argv.pop(0)
        if a == "--check-only":
            opts["fix"] = False
        elif a in names:
            if not argv:
                print("handoff_test.py: %s needs a value" % a, file=sys.stderr)
                return 3
            opts[names[a]] = argv.pop(0)
        else:
            pos.append(a)
    if len(pos) != 1 or not all(k in opts for k in ("project_dir", "out")):
        print("usage: handoff_test.py member NOTE --project-dir D --run-dir R --out-dir O [...]", file=sys.stderr)
        return 3
    if opts["deadline"] is not None:
        opts["deadline"] = float(opts["deadline"])
    signal.signal(signal.SIGTERM, _on_term)
    res = check_member(pos[0], opts.pop("project_dir"), opts.pop("run_dir", None), opts.pop("out"), **opts)
    json.dump(res, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    cmd, rest = (sys.argv[1], sys.argv[2:]) if len(sys.argv) > 1 else (None, [])
    if cmd == "session":
        sys.exit(session(rest))
    if cmd == "member":
        sys.exit(member(rest))
    print(__doc__, file=sys.stderr)
    sys.exit(3)
