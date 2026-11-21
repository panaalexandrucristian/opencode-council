#!/usr/bin/env python3
"""Offline tests of scripts/ui/server.py against a synthetic run directory.

The fixture follows the shapes scripts/council.sh writes (start: state.json members and config; run_step: posts/<task>-<tag>-<member>.md
and .json; deliberate and execute_and_ratify: results; handoff_build and handover_finish: handoffs/ and raw/handover-g<N>-<id>.md;
pause_for_questions: pending_questions). No council, OpenCode or network is needed. Standard library only:
    python3 scripts/ui/test_ui.py
"""
import hashlib
import http.client
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

UI = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("council_ui_server", UI / "server.py")
S = importlib.util.module_from_spec(spec)
spec.loader.exec_module(S)

TASKS = [
    {"id": "t1", "text": "Question one", "execute": False},
    {"id": "t2", "text": "Build the thing", "execute": True},
    {"id": "t3", "text": "Later question", "execute": False},
    {"id": "my-task", "text": "A task whose id has a hyphen", "execute": False},
]


def member(mid, kind, model, **extra):
    base = {"id": mid, "kind": kind, "model": model, "effort": "high", "mode": "read", "style": "normal", "handover_at": 0.5,
            "session": "ses_" + mid, "gen": 1, "fresh": False, "handover_note": None, "ctx_used": 0, "ctx_limit": 1000000,
            "session_tokens": 0, "session_cost": 0, "calls": 0, "session_calls": 0, "retired": [], "inflight": None}
    base.update(extra)
    return base


def make_state():
    members = [
        member("A", "opencode", "claude-agent/opus", effort="xhigh", agent="plan", gen=2, ctx_used=250000, session_tokens=900, calls=9,
               session_calls=4, retired=[{"session": "ses_a1", "gen": 1, "tokens": 4000, "cost": 0}], inflight={"tag": "t2-exec1"}),
        member("gpt-5", "opencode", "openai/gpt-6-astra", mode="edit", agent="build", handover_at=150000, session_tokens=70, session_cost=1.25,
               retired=[{"session": "ses_g0", "gen": 1, "tokens": 30, "cost": 0.75, "model": "claude x", "reason": "replaced"}]),
        member("C", "claude", "sonnet", effort="medium", permission_mode="plan", session="9f1c-uuid", ctx_limit=None, session_cost=0.5,
               inflight={"tag": "t2-exec1", "pid": 1, "pgid": 1, "started": "x", "raw": "y"}),
    ]
    config = {"dir": "/p", "max_rounds": 4, "timeout_s": 600, "handover_at": 0.5, "max_turns": 30, "executor": "gpt-5",
              "map_code": False, "map_prepass": {}, "style": "normal", "tasks": TASKS,
              "members": [{k: v for k, v in m.items() if k in ("id", "kind", "model", "effort", "mode")} for m in members]}
    return {
        "config": config, "run_dir": "/r", "started": "2026-11-21 12:00:00", "status": "running", "cl_cumulative": True,
        "task_idx": 1, "task_id": "t2", "round": 1, "phase": "exec", "candidate": {"id": "t2-c1", "member": "A", "text": "plan"},
        "last_votes": [{"vote": "agree", "member": "A"}, {"vote": "disagree", "reason": "r", "proposal": "p", "member": "gpt-5"}],
        "codemap_version": 1, "codemap_pending": None, "answers": [], "pending_questions": None, "notices": ["member C is now claude sonnet"],
        "reuse_posts": False,
        "results": [
            {"task": "t1", "outcome": "consensus", "text": "agreed one", "rounds": 2, "candidate": "t1-c1"},
            {"task": "t2", "outcome": "consensus", "text": "agreed plan", "rounds": 3, "candidate": "t2-c2"},
        ],
        "log": ["12:00:01 member A g2: handover from ses_a1"], "members": members,
    }


def write_post(run, name, text=None, tail=None, mtime=None):
    for ext, body in (("md", text), ("json", None if tail is None else json.dumps(tail))):
        if body is None:
            continue
        path = run / "posts" / f"{name}.{ext}"
        path.write_text(body, encoding="utf-8")
        if mtime is not None:
            os.utime(path, (mtime, mtime))


def make_run(base, state=None):
    run = Path(base) / "run.1+x"
    for sub in ("posts", "raw", "handoffs/handoff-0a1b2c3d-20261121-A-g1/blobs"):
        (run / sub).mkdir(parents=True)
    (run / "handoffs/handoff-0a1b2c3d-20261121-A-g1/HANDOFF.md").write_text("# handoff", encoding="utf-8")
    state = state or make_state()
    (run / "state.json").write_text(json.dumps(state), encoding="utf-8")
    (run / "config.json").write_text(json.dumps({"dir": "/p", "tasks": ["Question one"]}), encoding="utf-8")
    write_post(run, "t1-r1-A", "A proposes", {"vote": "propose", "proposal": "x"}, mtime=1000)
    write_post(run, "t1-r1-gpt-5", "gpt-5 proposes", {"vote": "propose", "proposal": "y"}, mtime=1001)
    write_post(run, "my-task-r2-gpt-5", None, {"vote": "agree"}, mtime=1002)
    write_post(run, "t2-exec1-gpt-5", "did it", {"vote": "done", "report": "r"}, mtime=1003)
    write_post(run, "t2-x1-A", "looks right", {"vote": "agree"}, mtime=1004)
    write_post(run, "t2-r3-C", "claude post", {"vote": "question", "questions": ["q?"]}, mtime=1005)
    (run / "posts" / "t1-r1-ghost.md").write_text("not a member", encoding="utf-8")
    (run / "posts" / "notes.txt").write_text("not a post", encoding="utf-8")
    (run / "posts" / "t1-r1-A.json.tmp").write_text("{}", encoding="utf-8")
    return run


def save_state(run, state):
    (run / "state.json").write_text(json.dumps(state), encoding="utf-8")


def snapshot(root):
    seen = {}
    for path in sorted(Path(root).rglob("*")):
        st = path.stat()
        seen[str(path)] = (st.st_size, st.st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "")
    return seen


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ui-test.")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.state = make_state()
        self.run = make_run(self.tmp, self.state)
        patcher = mock.patch.object(S, "alive", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def build(self):
        return S.build(self.run)

    def task(self, tid):
        return next(t for t in self.build()["tasks"] if t["id"] == tid)


class BuildTests(Fixture):
    def test_run_fields(self):
        b = self.build()
        self.assertEqual((b["status"], b["phase"], b["task_id"], b["task_idx"], b["round"], b["max_rounds"], b["started"]),
                         ("running", "exec", "t2", 1, 1, 4, "2026-11-21 12:00:00"))
        self.assertEqual(b["run"], str(self.run))
        self.assertIsNone(b["alive"])
        self.assertEqual(b["notices"], ["member C is now claude sonnet"])
        self.assertEqual(b["log"], ["12:00:01 member A g2: handover from ses_a1"])
        self.assertEqual(b["candidate"], {"id": "t2-c1", "member": "A"})
        self.assertEqual(b["last_votes"], [{"member": "A", "vote": "agree"}, {"member": "gpt-5", "vote": "disagree"}])

    def test_members_and_totals(self):
        b = self.build()
        a, g, c = b["members"]
        self.assertEqual((a["id"], a["kind"], a["model"], a["effort"], a["gen"], a["inflight"]), ("A", "opencode", "claude-agent/opus", "xhigh", 2, "t2-exec1"))
        self.assertEqual((a["session_tokens"], a["tokens"], a["retired"], a["calls"], a["ctx_used"], a["ctx_limit"]), (900, 4900, 1, 9, 250000, 1000000))
        self.assertEqual((g["tokens"], g["cost"], g["executor"], g["mode"], g["handover_at"], g["inflight"]), (100, 2.0, True, "edit", 150000, None))
        self.assertEqual((c["ctx_limit"], c["cost"], c["executor"], c["inflight"]), (0, 0.5, False, "t2-exec1"))  # a claude member's window is null until its first reply
        self.assertEqual(b["total_tokens"], 4900 + 100)
        self.assertAlmostEqual(b["total_cost"], 2.5)
        self.assertFalse(b["totals_exclude_mapper"])

    def test_totals_say_when_mapper_calls_are_not_in_them(self):
        self.state["map_prepass_version"] = 1
        self.state["config"]["map_code"] = True
        save_state(self.run, self.state)
        self.assertTrue(self.build()["totals_exclude_mapper"])

    def test_task_stages(self):
        self.assertEqual([t["stage"] for t in self.build()["tasks"]], ["done", "executing", "pending", "pending"])
        t1 = self.task("t1")
        self.assertEqual((t1["execute"], t1["result"]["outcome"], t1["result"]["rounds"], t1["result"]["text"]), (False, "consensus", 2, "agreed one"))
        self.assertEqual(self.task("t2")["result"]["outcome"], "consensus")  # a build task's plan is agreed, the task is not done
        self.assertIsNone(self.task("t3")["result"])

    def test_current_task_without_result(self):
        self.state["task_id"], self.state["phase"] = "t3", "plan"
        save_state(self.run, self.state)
        self.assertEqual([t["stage"] for t in self.build()["tasks"]], ["done", "executing", "current", "pending"])

    def test_ratified_build_task(self):
        self.state["results"][1].update({"outcome": "ratified", "implementation": "REPORT BY EXECUTOR gpt-5:\nok\n\nDIFF:\n+x", "ratify_rounds": 2})
        save_state(self.run, self.state)
        t2 = self.task("t2")
        self.assertEqual(t2["stage"], "done")
        self.assertEqual((t2["result"]["outcome"], t2["result"]["ratify_rounds"], t2["result"]["rounds"]), ("ratified", 2, 3))
        self.assertIn("DIFF:", t2["result"]["implementation"])

    def test_unratified_build_task_is_done_with_its_dissent(self):
        self.state["results"][1].update({"outcome": "unratified", "implementation": "impl", "dissent": [{"member": "A", "reason": "wrong", "proposal": "fix"}]})
        save_state(self.run, self.state)
        t2 = self.task("t2")
        self.assertEqual((t2["stage"], t2["result"]["outcome"]), ("done", "unratified"))
        self.assertEqual(t2["result"]["dissent"], [{"member": "A", "reason": "wrong", "proposal": "fix"}])

    def test_unresolved_task_keeps_every_dissent(self):
        self.state["results"].append({"task": "my-task", "outcome": "unresolved", "text": "last candidate", "candidate": "my-task-c3", "rounds": 4,
                                      "dissent": [{"member": "A", "reason": "r1", "proposal": "p1"}, {"member": "C", "reason": None, "proposal": None}]})
        save_state(self.run, self.state)
        res = self.task("my-task")["result"]
        self.assertEqual((self.task("my-task")["stage"], res["outcome"], res["rounds"], res["candidate"]), ("done", "unresolved", 4, "my-task-c3"))
        self.assertEqual([d["member"] for d in res["dissent"]], ["A", "C"])
        self.assertEqual(res["dissent"][1], {"member": "C", "reason": "", "proposal": ""})

    def test_the_last_result_of_a_task_wins(self):
        self.state["results"].append({"task": "t1", "outcome": "unresolved", "text": "later", "rounds": 4, "candidate": "t1-c3"})
        save_state(self.run, self.state)
        self.assertEqual(self.task("t1")["result"]["outcome"], "unresolved")

    def test_long_texts_are_capped(self):
        self.state["results"][0]["text"] = "x" * (S.POST_CAP + 50)
        save_state(self.run, self.state)
        text = self.task("t1")["result"]["text"]
        self.assertTrue(text.startswith("x" * S.POST_CAP) and "50 more characters" in text)
        self.assertLess(len(text), S.POST_CAP + 200)

    def test_task_text_is_clipped_for_the_card(self):
        self.state["config"]["tasks"][0]["text"] = "y" * 900
        save_state(self.run, self.state)
        self.assertEqual(len(self.task("t1")["text"]), 400)

    def test_pending_questions_and_how_to_answer(self):
        qs = [{"id": "t3/r1/A/q1", "task": "t3", "member": "A", "question": "Which one?"}]
        self.state.update(pending_questions=qs, status="questions", phase="plan")
        save_state(self.run, self.state)
        b = self.build()
        self.assertEqual((b["pending_questions"], b["answer_with"]), (qs, "answer"))
        for phase, flag in (("mapper_confirm", "confirm-mapper"), ("map_review", "map-decision"), ("split_invalid", "map-decision"), ("exec", "answer")):
            self.state["phase"] = phase
            save_state(self.run, self.state)
            self.assertEqual(self.build()["answer_with"], flag, phase)
        self.state["pending_questions"] = None
        save_state(self.run, self.state)
        self.assertEqual(self.build()["pending_questions"], [])

    def test_events_are_capped_and_only_strings(self):
        self.state["log"] = [f"line {i}" for i in range(100)]
        self.state["notices"] = ["ok", 5, None]
        save_state(self.run, self.state)
        b = self.build()
        self.assertEqual((len(b["log"]), b["log"][-1], b["notices"]), (S.EVENT_LINES, "line 99", ["ok"]))

    def test_config_json_is_the_fallback_and_string_tasks_get_ids(self):
        save_state(self.run, {"status": "created"})
        tasks = self.build()["tasks"]
        self.assertEqual([(t["id"], t["text"], t["execute"], t["stage"]) for t in tasks], [("t1", "Question one", False, "pending")])

    def test_unreadable_or_odd_state_does_not_raise(self):
        for body in ("not json", "[]", "null", "{}", '{"members": [1, null], "results": "x", "config": []}'):
            (self.run / "state.json").write_text(body, encoding="utf-8")
            b = self.build()
            self.assertEqual((b["members"], b["total_tokens"]), ([], 0), body)
        (self.run / "state.json").unlink()
        self.assertEqual(self.build()["status"], None)

    def test_a_member_without_numbers_counts_zero(self):
        self.state["members"][1].update(ctx_used=None, ctx_limit=None, session_tokens=None, session_cost=None, calls=None, retired=None)
        save_state(self.run, self.state)
        g = self.build()["members"][1]
        self.assertEqual((g["ctx_used"], g["ctx_limit"], g["tokens"], g["cost"], g["calls"], g["retired"]), (0, 0, 0, 0, 0, 0))

    def test_build_writes_nothing(self):
        before = snapshot(self.tmp)
        self.build()
        self.assertEqual(snapshot(self.tmp), before)


class PostTests(Fixture):
    def test_posts_are_parsed_with_hyphenated_ids_and_newest_first(self):
        b = self.build()
        keys = [(p["task"], p["tag"], p["member"]) for p in b["posts"]]
        self.assertEqual(keys, [("t2", "r3", "C"), ("t2", "x1", "A"), ("t2", "exec1", "gpt-5"), ("my-task", "r2", "gpt-5"),
                                ("t1", "r1", "gpt-5"), ("t1", "r1", "A")])
        self.assertEqual(b["posts_total"], 6)

    def test_post_text_and_vote(self):
        posts = {(p["task"], p["tag"], p["member"]): p for p in self.build()["posts"]}
        p = posts[("t1", "r1", "A")]
        self.assertEqual((p["text"], p["vote"], p["mtime"]), ("A proposes", "propose", 1000))
        self.assertEqual(posts[("t2", "exec1", "gpt-5")]["vote"], "done")
        self.assertEqual(posts[("t2", "r3", "C")]["vote"], "question")
        only_json = posts[("my-task", "r2", "gpt-5")]
        self.assertEqual((only_json["text"], only_json["vote"]), ("", "agree"))

    def test_files_that_are_not_posts_of_a_member_are_skipped(self):
        names = {p["member"] for p in self.build()["posts"]}
        self.assertNotIn("ghost", names)
        self.assertEqual(len(self.build()["posts"]), 6)

    def test_a_broken_json_tail_leaves_the_vote_empty(self):
        (self.run / "posts" / "t1-r1-A.json").write_text("{not json", encoding="utf-8")
        p = next(p for p in self.build()["posts"] if (p["task"], p["member"]) == ("t1", "A"))
        self.assertIsNone(p["vote"])

    def test_a_post_is_capped(self):
        (self.run / "posts" / "t1-r1-A.md").write_text("z" * (S.POST_CAP + 7), encoding="utf-8")
        p = next(p for p in self.build()["posts"] if (p["task"], p["member"]) == ("t1", "A"))
        self.assertTrue("7 more characters" in p["text"] and len(p["text"]) < S.POST_CAP + 200)

    def test_only_the_newest_posts_are_sent(self):
        for i in range(S.POSTS_MAX + 10):
            write_post(self.run, f"t3-r{i + 1}-A", f"post {i}", {"vote": "agree"}, mtime=5000 + i)
        b = self.build()
        self.assertEqual((len(b["posts"]), b["posts_total"]), (S.POSTS_MAX, S.POSTS_MAX + 10 + 6))
        self.assertEqual(b["posts"][0]["tag"], f"r{S.POSTS_MAX + 10}")

    def test_split_post_without_members_finds_nothing(self):
        self.assertIsNone(S.split_post("t1-r1-A.md", []))
        self.assertEqual(S.split_post("t1-r1-A.md", ["A"]), ("t1", "r1", "A", "md"))
        self.assertEqual(S.split_post("my-task-exec2-a-B.json", ["B", "a-B"]), ("my-task", "exec2", "a-B", "json"))
        self.assertIsNone(S.split_post("t1-q1-A.md", ["A"]))  # not a run_step tag
        # both readings are well formed: member B of task "t-r1" in tag x1, or member x1-B of task "t" in tag r1; the longest member id wins
        self.assertEqual(S.split_post("t-r1-x1-B.md", ["B", "x1-B"]), ("t", "r1", "x1-B", "md"))
        self.assertEqual(S.split_post("t-r1-x1-B.md", ["B"]), ("t-r1", "x1", "B", "md"))


class HandoverTests(Fixture):
    def test_handoff_folder_and_fallback_notes(self):
        (self.run / "raw" / "handover-g1-gpt-5.md").write_text("note", encoding="utf-8")
        (self.run / "raw" / "replace-C-g1.md").write_text("note", encoding="utf-8")
        (self.run / "raw" / "handover-r1-A.md").write_text("a raw reply of a task named handover", encoding="utf-8")
        (self.run / "raw" / "handover-g1-nobody.md").write_text("unknown member", encoding="utf-8")
        (self.run / "raw" / "handoff-A-g1.err").write_text("", encoding="utf-8")
        found = {(h["member"], h["gen"], h["kind"]) for h in self.build()["handovers"]}
        self.assertEqual(found, {("A", 1, "handoff"), ("gpt-5", 1, "own-posts note"), ("C", 1, "replacement note")})

    def test_handoff_folder_of_a_hyphenated_member(self):
        (self.run / "handoffs" / "handoff-deadbeef-20261121-gpt-5-g2").mkdir()
        found = [(h["member"], h["gen"]) for h in self.build()["handovers"] if h["kind"] == "handoff"]
        self.assertIn(("gpt-5", 2), found)

    def test_a_run_without_handoffs(self):
        shutil.rmtree(self.run / "handoffs")
        shutil.rmtree(self.run / "raw")
        self.assertEqual(self.build()["handovers"], [])


class LogTests(Fixture):
    def out(self):
        return Path(f"{self.run}.out")

    def test_missing_out_file(self):
        self.assertEqual(self.build()["out"], [])

    def test_last_lines_only(self):
        self.out().write_text("\n".join(f"line {i}" for i in range(200)) + "\n", encoding="utf-8")
        out = self.build()["out"]
        self.assertEqual((len(out), out[0], out[-1]), (S.LOG_LINES, f"line {200 - S.LOG_LINES}", "line 199"))

    def test_a_big_log_is_read_from_its_end_and_its_cut_first_line_is_dropped(self):
        self.out().write_text("".join(f"row {i:07d} " + "x" * 40 + "\n" for i in range(5000)), encoding="utf-8")  # about 260 KB
        self.assertGreater(self.out().stat().st_size, S.LOG_TAIL_BYTES * 3)
        out = self.build()["out"]
        self.assertEqual(len(out), S.LOG_LINES)
        self.assertTrue(out[-1].startswith("row 0004999"))
        self.assertTrue(all(re.match(r"row \d{7} x{40}$", line) for line in out))

    def test_a_short_log_keeps_its_first_line(self):
        self.out().write_text("first\nsecond\n", encoding="utf-8")
        self.assertEqual(self.build()["out"], ["first", "second"])


class AliveTests(unittest.TestCase):
    def run_with(self, rc=0, exc=None):
        with mock.patch.object(S.subprocess, "run", side_effect=exc, return_value=mock.Mock(returncode=rc)) as run:
            result = S.alive(Path("/runs/a.b+c (1)"))
        return result, run.call_args[0][0]

    def test_pattern_names_start_and_resume_and_escapes_the_path(self):
        _, args = self.run_with()
        self.assertEqual(args[:2], ["pgrep", "-f"])
        pattern = args[2]
        self.assertTrue(pattern.startswith(r"council\.sh (start|resume) .*--run-dir ("))
        self.assertIn(r"/runs/a\.b\+c \(1\)", pattern)
        self.assertTrue(re.search(pattern, "bash /x/council.sh start --config c.json --run-dir /runs/a.b+c (1)"))
        self.assertTrue(re.search(pattern, "bash /x/council.sh resume --run-dir /runs/a.b+c (1)/ --answers a.json"))
        self.assertFalse(re.search(pattern, "bash /x/council.sh status --run-dir /runs/a.b+c (1)"))
        self.assertFalse(re.search(pattern, "bash /x/council.sh resume --run-dir /runs/aXb+c (1)"))
        self.assertFalse(re.search(pattern, "bash /x/council.sh resume --run-dir /runs/a.b+c (1)2"))

    def test_results(self):
        self.assertIs(self.run_with(0)[0], True)
        self.assertIs(self.run_with(1)[0], False)
        self.assertIsNone(self.run_with(2)[0])
        self.assertIsNone(self.run_with(exc=FileNotFoundError())[0])
        self.assertIsNone(self.run_with(exc=subprocess.TimeoutExpired("pgrep", 5))[0])

    @unittest.skipUnless(shutil.which("pgrep"), "pgrep is not installed")
    def test_against_real_processes(self):
        tmp = tempfile.mkdtemp(prefix="ui-alive.")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        script = Path(tmp) / "council.sh"
        script.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
        script.chmod(0o755)
        run, other = Path(tmp) / "run.1+x", Path(tmp) / "other"
        procs = []

        def spawn(*argv):
            p = subprocess.Popen([str(script), *argv], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            procs.append(p)
            return p

        def stop(p):
            os.killpg(p.pid, signal.SIGKILL)
            p.wait()

        self.addCleanup(lambda: [stop(p) for p in procs if p.poll() is None])
        self.assertIs(S.alive(run), False)
        status = spawn("status", "--run-dir", str(run))
        self.assertIs(S.alive(run), False)  # a status call is not the run
        stop(status)
        started = spawn("start", "--config", "c.json", "--run-dir", str(run))
        self.assertIs(S.alive(run), True)
        self.assertIs(S.alive(other), False)
        stop(started)
        self.assertIs(S.alive(run), False)
        resumed = spawn("resume", "--run-dir", str(run), "--answer", "x")
        self.assertIs(S.alive(run), True)
        stop(resumed)


class PageContractTests(Fixture):
    """Every field index.html reads from the state it polls is one build() produces."""

    def setUp(self):
        super().setUp()
        self.state["pending_questions"] = [{"id": "q", "task": "t2", "member": "A", "question": "?"}]
        self.state["results"][1].update({"outcome": "unratified", "implementation": "i", "dissent": [{"member": "A", "reason": "r", "proposal": "p"}]})
        save_state(self.run, self.state)
        self.html = (UI / "index.html").read_text(encoding="utf-8")
        self.script = self.html[self.html.index("<script>"):]
        self.b = self.build()

    def fields(self, var):
        return set(re.findall(r"(?<![\w.$])" + var + r"\.([A-Za-z_]+)", self.script))

    def test_every_field_the_page_reads_exists(self):
        b = self.b
        wanted = {
            "s": set(b), "m": set(b["members"][0]), "t": set(b["tasks"][0]), "r": set(b["tasks"][1]["result"]), "p": set(b["posts"][0]),
            "h": set(b["handovers"][0]), "v": set(b["last_votes"][0]), "q": set(b["pending_questions"][0]),
            "x": set(b["tasks"][1]["result"]["dissent"][0]),
        }
        # members of the JS and DOM API that share a variable name with a state object
        dom = {"length", "split", "append", "style", "map", "filter", "forEach", "textContent", "hidden", "className", "open", "replaceChildren"}
        for var, keys in wanted.items():
            used = self.fields(var) - dom
            self.assertFalse(used - keys, f"index.html reads {var}.{sorted(used - keys)} but build() does not produce it")
            self.assertTrue(used, f"the page reads no field through {var}: the check is not looking at it")

    def test_the_page_never_builds_html_from_run_text(self):
        for unsafe in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
            self.assertNotIn(unsafe, self.html)

    def test_the_page_polls_the_state_endpoint(self):
        self.assertIn('fetch("/api/state"', self.html)
        self.assertIn("setInterval(tick, 3000)", self.html)

    def test_every_status_and_outcome_the_run_can_have_has_a_chip(self):
        for status in ("running", "questions", "failed", "done", "created"):
            self.assertRegex(self.script, status + r": \[")
        for outcome in ("consensus", "ratified", "unresolved", "unratified"):
            self.assertIn(outcome + ':', self.script)


class HttpTests(Fixture):
    def setUp(self):
        super().setUp()
        self.server = S.make_server(self.run, 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def request(self, method, path, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, headers={"Host": host} if host else {})
            r = conn.getresponse()
            return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
        finally:
            conn.close()

    def test_binds_loopback_only(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    def test_index(self):
        for path in ("/", "/index.html", "/?x=1"):
            status, headers, body = self.request("GET", path)
            self.assertEqual(status, 200, path)
            self.assertTrue(headers["content-type"].startswith("text/html"))
            self.assertEqual(body, (UI / "index.html").read_bytes())
            self.assertEqual((headers["cache-control"], headers["x-content-type-options"]), ("no-store", "nosniff"))

    def test_state(self):
        status, headers, body = self.request("GET", "/api/state?t=1")
        self.assertEqual(status, 200)
        self.assertTrue(headers["content-type"].startswith("application/json"))
        state = json.loads(body)
        self.assertEqual([m["id"] for m in state["members"]], ["A", "gpt-5", "C"])
        self.assertEqual([t["stage"] for t in state["tasks"]], ["done", "executing", "pending", "pending"])
        self.assertEqual(state["run"], str(self.run))

    def test_state_follows_the_run_while_it_changes(self):
        self.state["status"], self.state["round"] = "failed", 3
        save_state(self.run, self.state)
        state = json.loads(self.request("GET", "/api/state")[2])
        self.assertEqual((state["status"], state["round"]), ("failed", 3))

    def test_anything_else_is_404(self):
        for path in ("/nope", "/state.json", "/../state.json", "/api", "/api/state/", "/posts/t1-r1-A.md", "/api/../state.json"):
            self.assertEqual(self.request("GET", path)[0], 404, path)

    def test_only_get_is_served(self):
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            self.assertEqual(self.request(method, "/api/state")[0], 501, method)

    def test_foreign_host_names_are_refused(self):
        for host in ("evil.example", "evil.example:%d" % self.port, "127.0.0.1.evil.example", "localhost.evil.example:80", "[::2]:80"):
            self.assertEqual(self.request("GET", "/api/state", host)[0], 403, host)
            self.assertEqual(self.request("GET", "/", host)[0], 403, host)

    def test_loopback_host_names_are_served(self):
        for host in ("127.0.0.1:%d" % self.port, "localhost:%d" % self.port, "localhost", "[::1]:%d" % self.port, "127.0.0.1:9999"):
            self.assertEqual(self.request("GET", "/api/state", host)[0], 200, host)

    def test_serving_writes_nothing(self):
        before = snapshot(self.tmp)
        for path in ("/", "/api/state", "/nope"):
            self.request("GET", path)
        for method in ("POST", "PUT", "DELETE"):
            self.request(method, "/api/state")
        self.assertEqual(snapshot(self.tmp), before)


class CommandLineTests(unittest.TestCase):
    def script(self, *args, **kw):
        return subprocess.run([sys.executable, "-I", str(UI / "server.py"), *args], capture_output=True, text=True, timeout=30, **kw)

    def test_help(self):
        r = self.script("--help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("--port", r.stdout)

    def test_not_a_run_dir(self):
        tmp = tempfile.mkdtemp(prefix="ui-cli.")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        r = self.script(tmp)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not a council run dir", r.stderr)

    def test_serves_in_isolated_mode(self):
        tmp = tempfile.mkdtemp(prefix="ui-cli.")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        run = make_run(tmp)
        proc = subprocess.Popen([sys.executable, "-I", str(UI / "server.py"), str(run), "--port", "0"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (proc.kill(), proc.wait(), proc.stdout.close(), proc.stderr.close()))
        line = proc.stdout.readline()
        m = re.search(r"http://127\.0\.0\.1:(\d+)/", line)
        self.assertTrue(m, line)
        conn = http.client.HTTPConnection("127.0.0.1", int(m[1]), timeout=10)
        conn.request("GET", "/api/state")
        r = conn.getresponse()
        self.assertEqual(r.status, 200)
        self.assertEqual(json.loads(r.read())["status"], "running")
        conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=1)
