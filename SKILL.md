---
name: opencode
description: Delegate coding tasks to OpenCode (the local AI coding agent, v2) through its HTTP API. Checks that the OpenCode background service is running and starts it automatically if not. Use when the user asks to run/delegate something in or with OpenCode, to use another model via OpenCode, to list OpenCode models/agents/sessions, or to continue/inspect an OpenCode session. Also runs a cross-CLI "council" (consiliu): OpenCode and Claude Code CLI sessions on chosen models/efforts that discuss a task list, ask the user instead of assuming, and must reach unanimous consensus (scripts/council.sh).
argument-hint: [task to delegate to OpenCode]
allowed-tools: Bash(${CLAUDE_SKILL_DIR}/scripts/oc.sh *), Bash(${CLAUDE_SKILL_DIR}/scripts/council.sh *)
---

# OpenCode via HTTP API

Everything goes through `${CLAUDE_SKILL_DIR}/scripts/oc.sh` (bash + curl + jq); `scripts/council.sh` builds a cross-CLI council (OpenCode + Claude Code sessions) on top of it (see "Council" below). It talks to the
OpenCode **background service** (the same server the `opencode` TUI uses): it reads the URL and
password from `~/.local/state/opencode/service.json`, checks `GET /api/info`, and if the service is
not running it starts it with `opencode service start` and waits until it is healthy. You never need
to start the server yourself. Full endpoint notes: [reference.md](reference.md).

## Workflow

1. `oc.sh ensure` — prints `{"url","version","pid","started"}`. Run it once at the start (every other
   command also ensures the server, so this is mostly to report the state to the user).
2. Pick the project directory: always pass `--dir <absolute path>` (default is `$CLAUDE_PROJECT_DIR`
   or the cwd). The OpenCode session works *inside that directory* — files it edits land there.
3. Pick a model only if the user asked for one or the task benefits from it: `oc.sh models [filter]`
   lists `provider/id<TAB>name` (default marked `*default`). Pass it as `--model provider/id`.
4. Delegate:
   - one-shot: `oc.sh run "<task>" --dir D [--model P/ID] [--agent build|plan] [--title T]`
     → creates a session, sends the prompt, waits for the turn to finish, prints the assistant's
     final text. The session id is printed on stderr (`oc: session ses_...`) — keep it for follow-ups.
   - long prompts: write them to a file and use `oc.sh run - --file /path/prompt.md ...`, or pipe on stdin with `-`.
   - follow-up in the same session: `oc.sh prompt <ses_id> "<text>"`.
5. Inspect the outcome before reporting back:
   - `oc.sh diff <ses_id> [--patch]` — files changed (falls back to git working-tree status of the session dir).
   - `oc.sh messages <ses_id>` — compact transcript (tool calls included) when you need to know *what* it did.
   - `oc.sh result <ses_id>` — re-print the last answer.
6. Report to the user: what OpenCode did, the files it changed, and anything it flagged. Verify the
   changes yourself (read the diff / run tests) — treat OpenCode's output like a colleague's PR.

If the user gave arguments to this skill, treat `$ARGUMENTS` as the task and start at step 1.

## Permissions

**User preference: always allow.** Every session `oc.sh` creates gets an allow-all ruleset, and
`wait`/`prompt` answer `once` to any request that still shows up — so OpenCode never blocks waiting
for approval. Do not pass `--ask` unless the user explicitly asks for interactive approval
(then `run`/`prompt`/`wait` exit **3** with the pending `per_...` ids; answer with
`oc.sh reply <ses_id> <per_id> once|always|reject` and `oc.sh wait <ses_id>`).

Because everything is allowed, you are the safety net: keep tasks scoped to the project directory,
be explicit in prompts about what OpenCode may not touch, and review `oc.sh diff` before reporting.
For a specific restriction use `--deny ACTION[:RESOURCE]` on `new`/`run` (it wins over the
wildcard), e.g. `--deny 'bash:rm*'` or `--deny 'external_directory'`. Actions: `read edit glob grep
list bash task external_directory webfetch websearch skill lsp` (`*` = any).

## Exit codes and waiting

`0` ok · `1` error (message on stderr) · `2` wait timeout — the session is still running: call
`oc.sh wait <ses_id>` again (default timeout 600 s, `--timeout S` to change) or `oc.sh interrupt <ses_id>`
· `3` blocked on a permission request (only with `--ask`, see above).

`prompt`, `run` (when waiting), and `result` preserve available output but return **1** for
transport/JSON errors, assistant errors, or a turn without a succeeded idle outcome. `wait`
returns **0** only after observing an idle marker; deadline expiry remains **2**, and permission
blocking remains **3**.

## Other commands

| command | purpose |
|---|---|
| `oc.sh status` | server info, or `stopped` (exit 1); never starts the service |
| `oc.sh stop` | `opencode service stop` |
| `oc.sh agents [--dir D]` | agents available (`build` = default, `plan` = read-only) |
| `oc.sh sessions [--dir D] [--limit N]` | recent sessions for a directory (id, updated, outcome, title) |
| `oc.sh new [--dir D] [--model] [--agent] [--title] [--deny A:R]` | create a session without prompting (prints `ses_...`) |
| `oc.sh prompt SID TEXT --no-wait` | fire-and-forget; later `oc.sh wait SID` + `oc.sh result SID` |
| `oc.sh permissions SID` | pending permission requests (only relevant with `--ask`) |
| `oc.sh interrupt SID` | stop the running turn |
| `oc.sh api METHOD /api/... [JSON]` | raw request (see reference.md) |

## Council (cross-CLI multi-model consensus)

`scripts/council.sh` runs a **council**: N persistent sessions — OpenCode sessions (any enabled
model) and/or Claude Code CLI sessions (`claude -p … --resume`) — that work through a list of tasks,
see each other's posts every round (relayed by the orchestrator, by name), and must reach an
explicit **unanimous** consensus per task. Use it when the user asks for a "council"/"consiliu",
a multi-model decision, or several agents that must agree.

### 1. Settle the configuration with the user FIRST — never assume it

Everything below is a required field of `council.json`; `council.sh` refuses a config with a
missing field, and the roster is printed before any session exists. If the user did not state a
value, **ask** (one AskUserQuestion with the open points; propose concrete options, e.g. from
`oc.sh models`). Do not fill in silently.

| decide | field | notes |
|---|---|---|
| how many sessions, how many OpenCode vs Claude Code | `members[]` (`kind`: `opencode` \| `claude`) | ≥ 2 members, unique ids (A, B, C…) |
| which model each one uses | `members[].model` | OpenCode: `provider/id` from `oc.sh models`; Claude: `opus`, `sonnet`, `haiku`, `fable` or a full id |
| what effort each one uses | `members[].effort` | OpenCode = the model's **variant** (validated live: Astra low\|medium\|high\|xhigh\|max, Gemini low\|medium\|high, Kimi K3 low\|high\|max; models without variants take `"default"`); Claude = `--effort` low\|medium\|high\|xhigh\|max |
| who may edit files | `executor` + that member's `mode: "edit"` | exactly one executor or `null` (read-only council). read → OpenCode agent `plan` / Claude `--permission-mode plan`; edit → `build` / `acceptEdits` |
| project directory | `dir` | absolute; the sessions inspect/edit it |
| the tasks | `tasks[]` | strings, or `{"id","text","execute":true}` for a build task (plan → executor implements → council ratifies the diff) |
| bounds | `max_rounds` (2..10), `timeout_s` per call, `max_turns` for Claude members | |
| prose style (optional) | `style` (top level and/or per member) | `normal` (default), `lite`, `caveman`, `ultra` — compresses only the prose a member writes for the others, modelled on the [caveman skill](https://github.com/juliusbrussee/caveman). The JSON tail's `proposal`/`report`, quoted code, paths, commands, errors and numbers are never compressed. Honest expectation: single-digit % of output tokens here (the JSON tail is 74–99% of a post); the JetBrains lab measured ~8.5% on real agentic tasks |
| context handover threshold | `handover_at` (0..1], council-wide **and/or per member** | at ≥ this fraction of the model's context window the session writes a handover note and is replaced by a fresh session (same member id, next generation). Set it per member to make one hand over earlier than the rest (`{"id":"A", …, "handover_at":0.3}`); a member without its own value uses the council-wide one. The roster prints the effective threshold per member, and `status` shows each member's context as a percentage of its own threshold |

Then: `council.sh show --config council.json` → paste the roster to the user and get a yes before `start`.
`show` also prints members × rounds × tasks, the high/highest-effort members, and a
config-only cost note with two measured comparisons and cost-reduction levers. Its warning
is advisory; the measurements are not dollar predictions.

### 2. Run

```
council.sh start  --config council.json --run-dir <scratchpad>/council-<name>     # new run dir, must not exist
council.sh status --run-dir D                                                     # task/round, per-member context %, tokens, cost, pending questions
council.sh resume --run-dir D --answers answers.json | --answer "text"            # after exit 4 (questions) or exit 2 (failure)
council.sh resume --run-dir D --replace C=claude:sonnet:xhigh                     # swap a member's model/session (repeatable)
```

`resume` re-runs only what is missing: a member that already has a valid post for the current
round is reused, not called again. `--replace ID=kind:model:effort` (`kind` = `opencode` \| `claude`)
gives a member a fresh session on another model — use it when its provider fails or runs out of
quota. The new session keeps the member's id and mode, and its first prompt carries a handover note
built from that member's own earlier posts, so it continues from its positions. The replaced
session is retired (visible in `status`/transcript as an earlier generation).

Runs take minutes (rounds × slowest member): start it in the background and read the log; keep
`--run-dir` in the scratchpad. Exit codes: **0** all tasks reached consensus · **1** config error ·
**2** a member failed twice (checkpointed — inspect `D/raw/*.err`, fix, `resume`) · **4** the
council has questions for the user · **5** finished but a task is `unresolved`/`unratified`.

**Exit 4 — no assumptions.** Members must list every choice the task leaves open with what settles
it (`task` / `dir` / `user` answer / `ask`); anything not settled by the task, the working directory
or an earlier answer is turned into a question by the orchestrator, even if the member tried to
propose a default (tested: for "a script that prints a greeting" the council asks for the exact text,
language, shell and permissions instead of picking them). Read `D/questions.json`
(`[{id, member, question}]`), ask the user (AskUserQuestion, one question per item, verbatim — do not
answer on the user's behalf), write `answers.json` as `{"<id>": "<answer>"}` covering every id (or
`--answer TEXT` for one answer to all), and `resume`. The answers are relayed verbatim to every member
and the round is re-run without consuming the round budget.

**Context handover.** After every call the member's context use is measured (OpenCode: tokens of the
last assistant message vs the model's context limit; Claude: `usage` of the last iteration vs
`modelUsage.contextWindow`). At ≥ `handover_at` (after at least 2 calls on that session) the session
writes a handover note, a fresh session is created for the same member id (generation +1), and the
note + council rules are prepended to its first prompt. `status` and the transcript show per member:
generation, context %, session tokens, cost, calls, retired sessions. Both include each
member's final-generation + retired subtotal and a **RUN TOTAL** across all generations.

**Lossless prompt de-duplication.** In voting rounds, an exactly matching candidate or a
byte-identical proposal token can refer to a uniquely anchored peer post in the same prompt.
The source stays complete; exact comparisons and a byte-for-byte reconstruction check guard
every substitution, with full-text fallback on ambiguity. Substitutions go through the run log.
Votes still target the authoritative candidate in state. Capped diffs end with a visible
`DIFF TRUNCATED` line naming the 20000-byte cap and the directory to inspect.

### 3. Tests

`scripts/test-completion.sh` is an offline contract suite (no network, no model calls): it
loads the real functions from both scripts and stubs only curl/api/adapters, covering transport and
HTTP failures, permission-reply propagation, `wait_idle` completion, `show_result` validation,
`prompt`/`run` status propagation, failed council turns, exact prompt references/fallbacks,
post reuse, the question guard, diff truncation, and run totals. Run it plus **separate**
`/bin/bash -n` invocations for `oc.sh`, `council.sh`, and `test-completion.sh` after changes.

### 4. Report

The transcript is `D/transcript.md`: roster, per-member context/tokens/cost/generations, per task
the outcome (`consensus` / `ratified` / `unresolved` / `unratified`), the agreed text, every post
by round and member, dissent, Q&A, and the log (handovers included). Report to the user: the outcome
and agreed text per task, who dissented and why if unresolved, files changed for build tasks (verify
them yourself with `oc.sh diff <ses>` / `git diff`), and the token/context summary. Session ids are
in the roster table — any member can be continued with `oc.sh prompt <ses_id>` / `claude -p --resume <uuid>`.

Optional analysis tools (Python 3 standard library only; no pip, network, or model calls):

```bash
python3 scripts/ptools/prompt_report.py D  # section bytes, largest prompts, measure 1a/1b replay savings
python3 scripts/ptools/dedup_check.py D    # verbatim repeated blocks >=128 bytes within each prompt
```

Both are read-only and support `-h`. `prompt_report.py` documents its section boundaries in
its docstring; byte counts are not token counts. The council never invokes these helpers:
the skill works identically without Python or with `scripts/ptools/` absent.

## Targeting another server

Set `OPENCODE_URL` (and `OPENCODE_PASSWORD`, `OPENCODE_USERNAME` if not `opencode`) to talk to a
standalone `opencode serve --port N` or a remote instance instead of the local background service.
With `OPENCODE_URL` set the script never starts or stops anything. `opencode serve` prints its
password at startup (`server password ...`); `--port`/`--hostname`/`--cors` configure it.

## Troubleshooting

- `ensure` fails to start the service: run `opencode service status` / `opencode service restart`;
  a port clash is fixed with `opencode service set port <port>` (managed default port is 49374).
- HTTP 401 from the managed service: the state file password is stale → `opencode service restart`.
- Model errors (`[error] ...` in the result): check `oc.sh models` — only enabled providers work;
  `opencode auth` manages credentials.
- Empty `diff`: the session's directory was not recognised as a git repo when the session started;
  the command already falls back to `git` status of that directory.
