#!/usr/bin/env python3
"""Offline prompt-byte accounting and conservative measure-1 replay (stdlib only).

Usage: prompt_report.py <run-dir>
Reads prompts/*.md, posts/*.{md,json}, and state.json; never changes the run.
Sections partition every byte: candidate/plan includes its fences; relayed posts
include their heading; rules and replacement/handover notes include their framing;
answers include their heading; task text is the configured text only. Everything
else (including retries and handover requests) is instructions. These are UTF-8
file bytes, not tokens, filesystem allocation, or predicted dollars.
"""

import argparse
from collections import Counter
import json
from pathlib import Path
import re


SECTIONS = ("relayed posts", "candidate", "replacement notes", "answers",
            "rules", "instructions", "task text")
ANSWERS = b"Answers from the user to the council's questions (verbatim, authoritative):\n"


def load_run(parser):
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args()
    try:
        state = json.loads((args.run_dir / "state.json").read_bytes())
        prompts = sorted((args.run_dir / "prompts").glob("*.md"))
        if not prompts or not isinstance(state.get("config", {}).get("tasks"), list):
            raise ValueError("expected state.json with config.tasks and prompts/*.md")
        return args.run_dir, state, [(p, p.read_bytes()) for p in prompts]
    except (OSError, ValueError, AttributeError) as exc:
        parser.error(str(exc))


def layout(data):
    """Find the active task after first-contact material, never inside a handover."""
    start = 0
    note = data.find(b"Handover note from your predecessor session (same member id):\n")
    if note >= 0:
        end = re.search(rb"\n>>>\n\n(?=Notes from the orchestrator:|=== TASK )", data[note:])
        if end:
            start = note + end.end()
    header = re.search(rb"^=== TASK (.*?) \xe2\x80\x94 (.*?) ===\n", data[start:], re.M)
    if not header:
        return None
    begin, end = start + header.start(), start + header.end()
    task, phase = header.group(1).decode(), header.group(2).decode()
    candidate = re.search(rb"^<<<(?:CANDIDATE [^\n]+|PLAN)\n(.*?)\n>>>\n", data[end:], re.M | re.S)
    cand = None if not candidate else (end + candidate.start(), end + candidate.end(),
                                      end + candidate.start(1), end + candidate.end(1))
    return task, phase, begin, end, cand


def section_bytes(data, state):
    labels = bytearray([SECTIONS.index("instructions")]) * len(data)

    def mark(name, start, end):
        labels[start:end] = bytes([SECTIONS.index(name)]) * (end - start)

    info = layout(data)
    if info:
        task, phase, start, end, cand = info
        prefix = data[:start]
        rules_end = prefix.find(b"Nothing may follow the JSON tail.\n")
        if data.startswith(b"You are member ") and rules_end >= 0:
            rules_end += len(b"Nothing may follow the JSON tail.\n")
            mark("rules", 0, rules_end)
        for marker in (b"Handover note from your predecessor session (same member id):\n",
                       b"Notes from the orchestrator:\n"):
            pos = prefix.find(marker)
            if pos >= 0:
                mark("replacement notes", pos, start)
                break
        if cand:
            mark("candidate", cand[0], cand[1])
        body_start = cand[1] if cand else end
        # Final instructions follow all relayed posts/answers. rfind avoids member
        # quotations of those same instructions earlier in their posts.
        instruction = max(data.rfind(b"\nYour job this round:"),
                          data.rfind(b'\nVote "agree" if the implementation'))
        if instruction >= body_start:
            instruction += 1  # The preceding newline belongs to the section before it.
        if instruction < body_start:
            instruction = data.find(b"The council reached consensus on this plan:\n", end)
        if instruction < end:
            instruction = len(data)
        # Answers must be an authoritative prefix of state.answers immediately
        # before the final instructions. Headings quoted inside proposals/posts
        # are member text, not orchestrator sections.
        answer, block = -1, ANSWERS
        for a in state.get("answers", []):
            if a.get("task") != task:
                continue
            fields = [a.get(k) for k in ("member", "question", "answer")]
            fields = [v if isinstance(v, str) else json.dumps(v, ensure_ascii=False) for v in fields]
            block += ("- Q (member {}): {}\n  A: {}\n".format(*fields)).encode()
            if data[end:instruction].endswith(block):
                answer = instruction - len(block)
        if 0 <= answer < instruction:
            mark("answers", answer, instruction)
        relays = data.find(b"Posts of the other members ", body_start)
        if 0 <= relays < instruction:
            mark("relayed posts", relays, answer if answer > relays else instruction)
        if phase.startswith("round 1 "):
            for t in state["config"]["tasks"]:
                if isinstance(t, dict) and t.get("id") == task:
                    text = t["text"].encode()
                    if data[end:end + len(text)] == text:
                        mark("task text", end, end + len(text))
    counts = Counter(labels)
    return Counter({s: counts[i] for i, s in enumerate(SECTIONS)})


def proposal_source(md, stored):
    """Validate the original fenced tail against the sidecar; retain its raw token.

    Walk top-level JSON keys with JSONDecoder.raw_decode to avoid regex matching
    inside quoted prose. Like the shell, reject escaped/duplicate proposal keys.
    """
    if b"\0" in md:
        return None
    buf, tail = None, None
    for line in md.splitlines(keepends=True):
        if re.fullmatch(rb"\s*```\s*json\s*", line, re.I):
            buf = b""
        elif re.fullmatch(rb"\s*```\s*", line):
            if buf is not None:
                tail, buf = buf, None
        elif buf is not None:
            buf += line
    if tail is None:
        return None
    parsed = json.loads(tail)
    if parsed != stored or not isinstance(parsed.get("proposal"), str) or "\0" in parsed["proposal"]:
        return None
    text, decoder = tail.decode(), json.JSONDecoder()
    pos = len(text) - len(text.lstrip()) + 1
    tokens = []
    while True:
        while pos < len(text) and text[pos].isspace():
            pos += 1
        if text[pos] == "}":
            break
        key, stop = decoder.raw_decode(text, pos)
        if "\\" in text[pos:stop]:
            return None
        pos = stop
        while text[pos].isspace():
            pos += 1
        if text[pos] != ":":
            return None
        pos += 1
        while text[pos].isspace():
            pos += 1
        value, stop = decoder.raw_decode(text, pos)
        if key == "proposal":
            tokens.append(text[pos:stop].encode())
        pos = stop
        while text[pos].isspace():
            pos += 1
        if text[pos] == "}":
            break
        if text[pos] != ",":
            return None
        pos += 1
    return (tail, tokens[0], parsed["proposal"].encode()) if len(tokens) == 1 else None


def replace_once(text, old, new):
    if not old or text.count(old) != 1:
        raise ValueError("missing or ambiguous replacement")
    return text.replace(old, new, 1)


def measure_one(data, name, run, state):
    """Replay only validated sources actually present in this prompt. No model calls.

    Use matching authoritative state when available, including trailing newlines
    that bash hides when rendering. Otherwise historical candidates come from the
    prompt: a conservative lower bound when their original state is unavailable.
    Existing references are not re-counted.
    """
    info = layout(data)
    match = re.fullmatch(r"(.*)-r([0-9]+)-(.*)\.md", name)
    if not info or not match or int(match[2]) < 2 or not info[4] or b"COUNCIL_POST" in data:
        return data, []
    task, round_n, recipient = match[1], int(match[2]), match[3]
    cand = info[4]
    body = data[cand[2]:cand[3]]
    candidate_value = body
    current = state.get("candidate") or {}
    marker = b"<<<CANDIDATE " + str(current.get("id", "")).encode() + b"\n"
    if data[cand[0]:cand[2]] == marker and isinstance(current.get("text"), str):
        value = current["text"].encode()
        if value.rstrip(b"\n") == body:  # Match the renderer, not proposal equality.
            candidate_value = value
    peers = []
    for member in state.get("members", []):
        if member["id"] == recipient:
            continue
        pid = "{}-r{}-{}".format(task, round_n - 1, member["id"])
        if not re.fullmatch(r"[a-zA-Z0-9._-]+", pid):
            continue
        try:
            md = (run / "posts" / (pid + ".md")).read_bytes()
            stored = json.loads((run / "posts" / (pid + ".json")).read_bytes())
            source = proposal_source(md, stored)
            block = b"--- member " + member["id"].encode() + b" ---\n" + md + b"\n"
            if source and data[cand[1]:].count(block) == 1:
                peers.append(dict(id=pid, md=md, block=block, source=source, anchor=b"", changed=None))
        except (OSError, ValueError, AttributeError, IndexError):
            continue
    changes, events, source_id = [], [], None
    for p in peers:
        anchor = ("<<<COUNCIL_POST " + p["id"] + ">>>\n").encode()
        ref = ("Exact candidate: JSON-decode the 'proposal' string in COUNCIL_POST " + p["id"] + " below.").encode()
        saving = len(body) - len(ref) - len(anchor)
        if candidate_value == p["source"][2] and saving > 0:
            changes.append((data[cand[0]:cand[1]], data[cand[0]:cand[2]] + ref + data[cand[3]:cand[1]]))
            p["anchor"], source_id = anchor, p["id"]
            events.append(("1a", saving, len(body)))
            break
    for j, p in enumerate(peers):
        if p["id"] == source_id:
            continue
        tail, token, _ = p["source"]
        for first in peers[:j]:
            if first["changed"] is not None or token != first["source"][1]:
                continue
            ref = json.dumps("(identical to the proposal string in COUNCIL_POST " + first["id"] + " above)").encode()
            anchor = ("<<<COUNCIL_POST " + first["id"] + ">>>\n").encode()
            saving = len(token) - len(ref) - (0 if first["anchor"] else len(anchor))
            if saving <= 0:
                continue
            try:
                edited_tail = replace_once(tail, token, ref)
                expected = json.loads(tail)
                expected["proposal"] = json.loads(ref)
                if json.loads(edited_tail) != expected or replace_once(edited_tail, ref, token) != tail:
                    continue
                edited = replace_once(p["md"], tail, edited_tail)
                if replace_once(edited, edited_tail, tail) != p["md"]:
                    continue
            except ValueError:
                continue
            p["changed"], first["anchor"] = edited, anchor
            events.append(("1b", saving, len(token)))
            break
    for p in peers:
        if p["anchor"] or p["changed"] is not None:
            heading = p["block"][:len(p["block"]) - len(p["md"]) - 1]
            changes.append((p["block"], heading + p["anchor"] + (p["changed"] if p["changed"] is not None else p["md"]) + b"\n"))
    try:
        after = data
        for old, new in changes:
            after = replace_once(after, old, new)
        restored = after
        for old, new in reversed(changes):
            restored = replace_once(restored, new, old)
        if restored == data and len(data) - len(after) == sum(e[1] for e in events):
            return after, events
    except ValueError:
        pass
    return data, []


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0], usage="%(prog)s <run-dir>")
    run, state, prompts = load_run(parser)
    totals, measures, rows = Counter(), Counter(), []
    for path, data in prompts:
        totals.update(section_bytes(data, state))
        after, events = measure_one(data, path.name, run, state)
        if events:
            rows.append((path.name, len(data), len(after), events))
        for measure, saving, removed in events:
            measures[measure] += saving
            measures[measure + " removed"] += removed
    size = sum(len(data) for _, data in prompts)
    print("Prompts: {:,} bytes in {} files".format(size, len(prompts)))
    print("Section bytes (disjoint; framing conventions in script docstring):")
    for section in SECTIONS:
        print("  {:19s} {:>10,}  {:6.2f}%".format(section, totals[section], totals[section] * 100 / size if size else 0))
    print("Biggest prompts:")
    for path, data in sorted(prompts, key=lambda p: (-len(p[1]), p[0].name))[:12]:
        print("  {}: {:,} bytes".format(path.name, len(data)))
    print("Measure 1 replay (only same-prompt, validated sources; net bytes):")
    for name, before, after, events in rows:
        print("  {}: {:,} -> {:,}; saved {:,} ({})".format(name, before, after, before - after, "+".join(e[0] for e in events)))
    for measure in ("1a", "1b"):
        print("  {}: {:,} removed - {:,} reference/anchor bytes = {:,} saved".format(measure, measures[measure + " removed"], measures[measure + " removed"] - measures[measure], measures[measure]))
    saved = measures["1a"] + measures["1b"]
    print("Combined: {:,} / {:,} bytes = {:.2f}% (prompt bytes, not tokens)".format(saved, size, saved * 100 / size if size else 0))


if __name__ == "__main__":
    main()
