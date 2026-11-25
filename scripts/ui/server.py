#!/usr/bin/env python3
"""Read-only live view of one council run.

    python3 -I scripts/ui/server.py RUN_DIR [--port 8765]

Serves index.html and /api/state, built from the files council.sh writes in RUN_DIR (state.json, config.json, posts/,
handoffs/, raw/handover-*.md) and from the log next to it (RUN_DIR.out, when the run was started with its output sent
there). Listens on 127.0.0.1 only, answers only GET, and writes nothing: every question the council asks is still
answered with `council.sh resume`. Python 3 standard library only.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
POST_CAP = 30000      # characters of one post, one agreed text or one implementation report sent to the page
REASON_CAP = 4000     # one dissenting proposal
POSTS_MAX = 60        # newest posts sent per poll (the rest stay on disk)
LOG_LINES = 60        # last lines of RUN_DIR.out
LOG_TAIL_BYTES = 65536
EVENT_LINES = 40      # last lines of state.log
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
FINAL_OUTCOMES = {"ratified", "unratified", "unresolved"}  # results[].outcome that end a task of either kind
# what `council.sh resume` needs while a run waits for the user, by state.phase (resume: "questions" branch)
ANSWER_WITH = {"mapper_confirm": "confirm-mapper", "map_review": "map-decision", "split_invalid": "map-decision"}
TASK_TAG = re.compile(r"^(?P<task>.+)-(?P<tag>(?:exec|r|x)\d+)$")  # run_step tags: r<N>, exec<N>, x<N>
POST_FILE = re.compile(r"^(?P<stem>.+)\.(?P<ext>md|json)$")
HANDOFF_DIR = re.compile(r"^handoff-[0-9a-f]{8}-\d{8}-(?P<member>.+)-g(?P<gen>\d+)$")  # handoffs/<this>/HANDOFF.md
OWN_POSTS_NOTE = re.compile(r"^handover-g(?P<gen>\d+)-(?P<member>.+)\.md$")             # raw/: the handoff could not be built
REPLACE_NOTE = re.compile(r"^replace-(?P<member>.+)-g(?P<gen>\d+)\.md$")                 # raw/: resume --replace


def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def clip(value, cap=POST_CAP):
    text = value if isinstance(value, str) else ""
    if len(text) > cap:
        return text[:cap] + f"\n[... {len(text) - cap} more characters, see the files in the run directory]"
    return text


def read_text(path, cap=None):
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return clip(text, cap) if cap is not None else text


def tail_lines(path, count=LOG_LINES, max_bytes=LOG_TAIL_BYTES):
    """The last lines of a log that may be megabytes long: only its last max_bytes are read."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            data = fh.read()
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]  # the first line was cut in the middle
    return lines[-count:]


def ere_escape(text):
    return re.sub(r"([.^$*+?()\[\]{}|\\])", r"\\\1", text)


def alive(run):
    """True while a council.sh start/resume process names this run directory, False when none does, None when that
    cannot be told. A heuristic on command lines: it needs the run directory given to council.sh as an absolute path,
    spelled as the server was given it or with its symlinks resolved (a relative --run-dir is not matched)."""
    spellings = sorted({str(run), os.path.realpath(run)})
    pattern = r"council\.sh (start|resume) .*--run-dir (" + "|".join(ere_escape(s) for s in spellings) + ")/?( |$)"
    try:
        rc = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True, timeout=5).returncode
    except (OSError, subprocess.SubprocessError):
        return None
    return {0: True, 1: False}.get(rc)


def split_post(name, member_ids):
    """posts/<task>-<tag>-<member>.<md|json> -> (task, tag, member, ext). Task and member ids may contain hyphens, so the
    member is matched against the ids the run has (longest first) and the tag against run_step's shapes."""
    m = POST_FILE.match(name)
    if not m:
        return None
    stem, ext = m["stem"], m["ext"]
    for member in sorted(member_ids, key=len, reverse=True):
        if stem.endswith("-" + member):
            t = TASK_TAG.match(stem[: -len(member) - 1])
            if t:
                return t["task"], t["tag"], member, ext
    return None


def posts_of(run, member_ids):
    folder = run / "posts"
    found = {}
    try:
        names = list(folder.iterdir())
    except OSError:
        return [], 0
    for path in names:
        parts = split_post(path.name, member_ids)
        if not parts:
            continue
        task, tag, member, ext = parts
        entry = found.setdefault((task, tag, member), {"task": task, "tag": tag, "member": member, "mtime": 0, "text": "", "vote": None})
        try:
            entry["mtime"] = max(entry["mtime"], path.stat().st_mtime)
        except OSError:
            continue
        if ext == "md":
            entry["text"] = read_text(path, POST_CAP)
        else:
            tail = read_json(path, None)
            entry["vote"] = tail.get("vote") if isinstance(tail, dict) else None
    ordered = sorted(found.values(), key=lambda e: e["mtime"], reverse=True)
    return ordered[:POSTS_MAX], len(ordered)


def handovers_of(run, member_ids):
    """What the run left for the successor sessions: a compact handoff folder (council.sh handoff_build) or, when the
    handoff could not be built or a member was replaced, a note made of the member's own posts in raw/."""
    found = []
    try:
        for path in (run / "handoffs").iterdir():
            m = HANDOFF_DIR.match(path.name)
            if m and path.is_dir():
                found.append({"member": m["member"], "gen": int(m["gen"]), "kind": "handoff", "name": path.name, "mtime": path.stat().st_mtime})
    except OSError:
        pass
    try:
        for path in (run / "raw").iterdir():
            for pattern, kind in ((OWN_POSTS_NOTE, "own-posts note"), (REPLACE_NOTE, "replacement note")):
                m = pattern.match(path.name)
                if m and m["member"] in member_ids:  # raw/ also holds every member's raw replies: only known members count
                    found.append({"member": m["member"], "gen": int(m["gen"]), "kind": kind, "name": path.name, "mtime": path.stat().st_mtime})
                    break
    except OSError:
        pass
    return sorted(found, key=lambda h: (h["mtime"], h["name"]))


def normalised_tasks(config):
    tasks = []
    for i, t in enumerate(config.get("tasks") or []):
        if isinstance(t, str):
            t = {"text": t}
        if not isinstance(t, dict):
            continue
        tasks.append({"id": t.get("id") or f"t{i + 1}", "text": t.get("text") or "", "execute": t.get("execute") is True})
    return tasks


def result_view(res):
    dissent = [{"member": d.get("member"), "reason": clip(d.get("reason"), REASON_CAP), "proposal": clip(d.get("proposal"), REASON_CAP)}
               for d in (res.get("dissent") or []) if isinstance(d, dict)]
    return {"outcome": res.get("outcome"), "rounds": res.get("rounds"), "ratify_rounds": res.get("ratify_rounds"),
            "candidate": res.get("candidate"), "text": clip(res.get("text")), "implementation": clip(res.get("implementation")),
            "dissent": dissent}


def task_views(config, state):
    """One entry per task: its text and, once council.sh recorded one, its result. state.results holds one object per
    finished deliberation, {task, outcome: consensus|unresolved, text, rounds, candidate[, dissent]}; for a build task
    council.sh updates that same object in place after implementation, adding outcome ratified|unratified,
    implementation, ratify_rounds[, dissent]. So "consensus" on a build task means the plan is agreed, not that the task
    is done."""
    results = [r for r in (state.get("results") or []) if isinstance(r, dict)]
    current = state.get("task_id")
    views = []
    for t in normalised_tasks(config):
        mine = [r for r in results if r.get("task") == t["id"]]
        res = result_view(mine[-1]) if mine else None
        outcome = res["outcome"] if res else None
        if outcome in FINAL_OUTCOMES or (outcome == "consensus" and not t["execute"]):
            stage = "done"
        elif outcome == "consensus":
            stage = "executing"
        else:
            stage = "current" if t["id"] == current else "pending"
        views.append({"id": t["id"], "text": t["text"][:400], "execute": t["execute"], "stage": stage, "result": res})
    return views


def build(run):
    run = Path(run)
    state = read_json(run / "state.json", {})
    if not isinstance(state, dict):
        state = {}
    config = state.get("config") or read_json(run / "config.json", {})
    if not isinstance(config, dict):
        config = {}
    cfg_members = {m.get("id"): m for m in config.get("members") or [] if isinstance(m, dict)}
    members, total_tokens, total_cost = [], 0, 0.0
    for m in state.get("members") or []:
        if not isinstance(m, dict):
            continue
        retired = [r for r in (m.get("retired") or []) if isinstance(r, dict)]
        tokens = (m.get("session_tokens") or 0) + sum(r.get("tokens") or 0 for r in retired)
        cost = (m.get("session_cost") or 0) + sum(r.get("cost") or 0 for r in retired)
        total_tokens += tokens
        total_cost += cost
        inflight = m.get("inflight") if isinstance(m.get("inflight"), dict) else {}
        members.append({
            "id": m.get("id"), "kind": m.get("kind"), "model": m.get("model"), "mode": m.get("mode"),
            "effort": m.get("effort") or cfg_members.get(m.get("id"), {}).get("effort"),
            "executor": bool(config.get("executor")) and m.get("id") == config.get("executor"),
            "gen": m.get("gen"), "session": m.get("session"), "ctx_used": m.get("ctx_used") or 0, "ctx_limit": m.get("ctx_limit") or 0,
            "handover_at": m.get("handover_at") or config.get("handover_at"),
            "session_tokens": m.get("session_tokens") or 0, "session_cost": m.get("session_cost") or 0,
            "calls": m.get("calls") or 0, "retired": len(retired), "tokens": tokens, "cost": cost, "inflight": inflight.get("tag"),
        })
    member_ids = [m["id"] for m in members if isinstance(m["id"], str) and m["id"]]
    posts, posts_total = posts_of(run, member_ids)
    votes = [{"member": v.get("member"), "vote": v.get("vote")} for v in (state.get("last_votes") or []) if isinstance(v, dict)]
    candidate = state.get("candidate") if isinstance(state.get("candidate"), dict) else None
    phase = state.get("phase")
    return {
        "now": time.time(), "run": str(run), "alive": alive(run),
        "status": state.get("status"), "phase": phase, "task_id": state.get("task_id"), "task_idx": state.get("task_idx"),
        "round": state.get("round"), "max_rounds": config.get("max_rounds"), "started": state.get("started"),
        "tasks": task_views(config, state), "members": members,
        "total_tokens": total_tokens, "total_cost": total_cost,
        "totals_exclude_mapper": state.get("map_prepass_version") == 1 and config.get("map_code") is True,
        "candidate": {"id": candidate.get("id"), "member": candidate.get("member")} if candidate else None, "last_votes": votes,
        "pending_questions": [q for q in (state.get("pending_questions") or []) if isinstance(q, dict)],
        "answer_with": ANSWER_WITH.get(phase, "answer"),
        "notices": [n for n in (state.get("notices") or []) if isinstance(n, str)],
        "log": [e for e in (state.get("log") or []) if isinstance(e, str)][-EVENT_LINES:],
        "posts": posts, "posts_total": posts_total, "handovers": handovers_of(run, member_ids),
        "out": tail_lines(Path(f"{run}.out")),
    }


class Handler(BaseHTTPRequestHandler):
    """GET only: any other method gets the base class's 501, so there is nothing here that changes a file or a run."""
    run = None

    def _send(self, code, body, ctype):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _host_ok(self):
        # a page on another site that DNS-rebinds its name to 127.0.0.1 reaches this port with its own name in Host
        host = self.headers.get("Host", "")
        name = host[: host.index("]") + 1] if host.startswith("[") and "]" in host else host.split(":")[0]
        return host == "" or name in ALLOWED_HOSTS

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, "forbidden host", "text/plain; charset=utf-8")
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/state":
            self._send(200, json.dumps(build(self.run)), "application/json; charset=utf-8")
        else:
            self._send(404, "not found", "text/plain; charset=utf-8")

    def log_message(self, *args):
        pass


def make_server(run, port):
    handler = type("RunHandler", (Handler,), {"run": Path(run)})
    return ThreadingHTTPServer(("127.0.0.1", port), handler)


def main():
    ap = argparse.ArgumentParser(description="Read-only live view of a council run (127.0.0.1 only).")
    ap.add_argument("run_dir")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    run = Path(args.run_dir).absolute()  # not resolve(): alive() matches the path as council.sh was given it
    if not (run / "state.json").is_file():
        sys.exit(f"not a council run dir: {run}")
    server = make_server(run, args.port)
    print(f"council view of {run}: http://127.0.0.1:{server.server_address[1]}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
