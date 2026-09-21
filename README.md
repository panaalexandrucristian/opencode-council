# opencode-council

A [Claude Code](https://claude.com/claude-code) skill that drives [OpenCode](https://opencode.ai) through its HTTP API — and runs a **cross-CLI council**: OpenCode sessions and Claude Code CLI sessions, each on a model and effort you choose, that discuss a list of tasks, ask you instead of assuming, and must reach a unanimous consensus.

## What is inside

| file | purpose |
|---|---|
| `SKILL.md` | the skill Claude Code loads (`/opencode …`): workflow, permissions, the council checklist |
| `scripts/oc.sh` | thin bash + curl + jq CLI over the OpenCode v2 background service (sessions, prompts, wait, diff, models, `--variant` = effort) |
| `scripts/council.sh` | the council orchestrator (see below) |
| `reference.md` | OpenCode HTTP endpoint notes |

## Install

```bash
git clone https://github.com/panaalexandrucristian/opencode-council.git ~/.claude/skills/opencode
```

Requirements: `opencode` (v2, with at least one provider authenticated), `claude` (Claude Code CLI, for Claude council members), `bash`, `curl`, `jq`. The scripts start the OpenCode background service themselves when it is not running.

## The council

```bash
scripts/council.sh show   --config council.json              # validate + print the roster (no sessions created)
scripts/council.sh start  --config council.json --run-dir D  # run all tasks
scripts/council.sh status --run-dir D                        # task/round, per-member context %, tokens, cost
scripts/council.sh resume --run-dir D --answers answers.json # continue after the council asked questions
```

`council.json` — every field is explicit, nothing is defaulted silently:

```json
{
  "dir": "/abs/project", "max_rounds": 4, "timeout_s": 600, "handover_at": 0.5, "max_turns": 30,
  "executor": "C",
  "tasks": [
    "What does scripts/oc.sh status print when the service is stopped? Cite the lines.",
    {"id": "hello", "text": "Create hello.sh that prints a greeting, plus a test.", "execute": true}
  ],
  "members": [
    {"id": "A", "kind": "opencode", "model": "openai/gpt-6-astra",           "effort": "high",   "mode": "read"},
    {"id": "B", "kind": "opencode", "model": "google/gemini-3.1-pro-preview", "effort": "high",   "mode": "read"},
    {"id": "C", "kind": "claude",   "model": "sonnet",                        "effort": "medium", "mode": "edit"}
  ]
}
```

How it works:

- **Roster first.** `show` prints how many sessions there are, how many OpenCode vs Claude Code, each member's model, effort (validated against the model's variants), mode and context window — before anything runs.
- **Members talk to each other.** Every round the orchestrator relays the other members' posts (by name) into each session. Posts are prose plus a mandatory JSON tail the orchestrator parses.
- **Unanimous consensus.** Round 1 = proposals; then a rotating proposer's position is the frozen candidate and everyone votes `agree` / `disagree` (with a complete revised proposal). All `agree` = consensus. `max_rounds` reached = `unresolved` (exit 5), dissent preserved — never a forced verdict.
- **Build tasks.** Consensus on a plan → the single `executor` implements it → the council ratifies the report + diff (fix rounds if needed).
- **No assumptions.** Each member must list every choice the task leaves open and what settles it (`task` / `dir` / `user` / `ask`). Anything not settled by the task, the working directory or an earlier answer becomes a question for the user: the run pauses (exit 4, `questions.json`), you answer, `resume` re-runs the round.
- **Context handover.** After every call the member's context use is measured against the model's window; at `handover_at` the session writes a handover note and is replaced by a fresh session (same member, next generation) that starts from the note.
- **Transcript.** `D/transcript.md`: roster, per-member tokens/context/cost/generations, every post by task and round, outcomes, Q&A, log.

Exit codes: `0` all tasks reached consensus · `1` config error · `2` a member failed twice (checkpointed, `resume`) · `4` questions pending · `5` some task unresolved.

## Example

[`examples/`](examples/) is a real run: a 2-member council (Claude Sonnet read-only + OpenCode Kimi K3
as executor) gets "create `hello.sh` that prints a greeting", **asks** for the exact text, language,
shell and permissions instead of assuming them, then agrees a plan, implements it and ratifies the
result. It contains the `council.json`, the `answers.json`, the console output of `show` / `start` /
`resume`, and the full [`transcript.md`](examples/transcript.md).

## `oc.sh` on its own

```bash
scripts/oc.sh ensure                                   # start/check the OpenCode service
scripts/oc.sh models [filter]                          # enabled models (provider/id)
scripts/oc.sh run "task" --dir /abs/project --model openai/gpt-6-astra --variant high
scripts/oc.sh prompt ses_… "follow-up" ; scripts/oc.sh diff ses_… --patch ; scripts/oc.sh messages ses_…
```
