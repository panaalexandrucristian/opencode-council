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
| who may edit files | `executor` + that member's `mode: "edit"` | exactly one executor or `null` (read-only council). All members run with permissions bypassed (OpenCode: allow-all ruleset + `--auto`; Claude: `--permission-mode bypassPermissions`). read → OpenCode agent `plan` / Claude without Edit/Write/MultiEdit/NotebookEdit; edit → `build` / all tools |
| project directory | `dir` | absolute; the sessions inspect/edit it |
| the tasks | `tasks[]` | strings, or `{"id","text","execute":true}` for a build task (plan → executor implements → council ratifies the diff) |
| bounds | `max_rounds` (2..10), `timeout_s` per call, `max_turns` for Claude members | |
| prose style (optional) | `style` (top level and/or per member) | `normal` (default), `lite`, `caveman`, `ultra` — compresses only the prose a member writes for the others, modelled on the [caveman skill](https://github.com/juliusbrussee/caveman). The JSON tail's `proposal`/`report`, quoted code, paths, commands, errors and numbers are never compressed. Honest expectation: single-digit % of output tokens here (the JSON tail is 74–99% of a post); the JetBrains lab measured ~8.5% on real agentic tasks |
| context handover threshold | `handover_at` (0..1], council-wide **and/or per member** | a fraction in (0,1] of the model's context window, **or an absolute token count** (≥ 1000, e.g. `150000`). At the threshold the session writes a handover note and is replaced by a fresh session (same member id, next generation). Set it per member to make one hand over earlier than the rest (`{"id":"A", …, "handover_at":0.3}`); a member without its own value uses the council-wide one. The roster prints the effective threshold per member, and `status` shows each member's context as a percentage of its own threshold |
| whether to seed the code map | `map_code` (one run-wide boolean) | **Ask the user once before writing the config:** “Map the code for this council?” Record true/false. It applies to every task. Hand-written configs that omit it are accepted as false; there is no prompt or pause. |

Then: `council.sh show --config council.json` → paste the roster to the user and get a yes before `start`.
`show` also prints members × rounds × tasks, the high/highest-effort members, and a
config-only cost note with two measured comparisons and cost-reduction levers. Its warning
is advisory; the measurements are not dollar predictions.

**Choosing models: measured (2026-10-05).** When proposing a roster, start cheap, because the
cheapest council solved hard but well-specified tasks perfectly. The test council was Claude Sonnet
at `low` effort as executor plus `openai/gpt-6-luna-fast` at `low`, with `max_rounds` 3 and
`map_code` false. Each task was graded against hidden tests that the members never saw:

- **Spreadsheet engine:** about 1300 lines built from a strict spec (parser, coercions, static
  cycles, inserting and deleting rows/columns with formula rewriting, a 5000-cell chain).
  Result: 54/54 hidden tests and 300/300 random-operation fuzz sequences matching a reference
  implementation. 8 min, 2.3M tokens, about $1.34 list price.
- **Four algorithmic problems (contest level ~2000–2400, Python under time limits):** 100%.
  3 min, about $0.40. On one problem Sonnet `low` found a better algorithm than the planned one.
- **A real change in this repository (release 0.11.1, orphaned Claude calls):** process groups,
  signal traps, a launch-window race and per-attempt raw files in bash. The council started from
  0.11.0 with the release notes and the test interface. Result: the official `orphan_tests` passed
  26/26 with 0 regressions in 1107 other checks. About 70 min, 8.3M tokens, about $2.45. The cheap
  reviewer (Luna Fast `low`) missed the JSON tail 4 times (each fixed by the automatic retry) and
  once asked to leave Plan mode.
- **No leaks:** no access to the hidden tests appeared in any transcript.

So for clear, well-specified coding tasks, including real multi-file changes in this repo, do not default to Opus or to more members. One
cheap pair is already at the ceiling, and a stronger or larger council only adds cost.

**Not yet measured:**
- Whether Opus, or several Sonnets standing in for one Opus, helps on underspecified work, work
  in a large unfamiliar codebase, or problems at the frontier of model ability. Escalate there,
  and ideally measure it the same way: hidden tests plus a cheap pilot first.
- These results are one run each (n=1).

**Pricing note (OpenCode's GitHub Copilot prices):** `gpt-5.6-terra` costs $2/$12 per million
input/output tokens and `gpt-6.1-sol` costs $2/$10, so Terra is older but not cheaper. The cheap
OpenAI option is `gpt-6-luna` at $0.1/$0.5.

When `map_code` is true, every original task receives a bounded read-only navigation pre-pass and a user map-review checkpoint. Mapper settings may be supplied in `map_prepass` (`kind`, `model`, `effort`, `timeout_s`, `max_output_bytes`). If model or effort is omitted, `start` proposes OpenCode `google/gemini-3.8-flash` at medium effort and pauses before any inference call. Confirm with `resume --run-dir D --confirm-mapper FILE`, where FILE contains the exact `{"kind":"opencode","model":"google/gemini-3.8-flash","effort":"medium"}` tuple (or the displayed proposal). A config-specified complete mapper pair needs no confirmation. The mapper prompt asks for exactly one JSON object (`scripts/council.sh:642`). When strict parsing fails, a narrow syntax repair runs outside string literals: it quotes bare keys, removes a trailing comma after a value, inserts a missing comma between the three known top-level fields, and adds missing outer braces. It never changes a value. Ambiguous text stays unavailable, and duplicate keys and NaN/Infinity are rejected (`_repair_json_syntax`, `_unique_keys`, `_reject_constant`: `scripts/council_map_prepass.py:22-81,98-105`). Each repair is listed in `result.repairs`, `validation.err` and the map review (`council_map_prepass.py:153,313`; `scripts/council.sh:738,800`). After mapping, inspect the displayed complete-map locator and use `resume --run-dir D --map-decision FILE`; FILE is `{"action":"keep"}` or `{"action":"split","contract_file":"path/to/contract.json"}`. Splits are checked offline against declared baseline files before any further model call. The map remains incomplete, and members must inspect raw evidence and independently check task scope.

The split contract file path is relative to the directory containing the map-decision JSON. A version-1 contract names `parent_id` and the exact contract `map_seed_id` printed at map review from `coverage.json`; the locator's `snapshot_id` is a distinct lookup identity and must not be substituted. It then supplies non-empty `subtasks`; every child declares unique `id`, complete `text`, boolean `execute`, and arrays `requires`, `modifies`, `deletes`, `creates`, `acceptance`, and `unresolved`. Existing-file declarations bind to `{ "path": "...", "sha256": "<64 lowercase hex>" }`; created paths must be absent. Each acceptance item requires a unique `id`, `description`, non-empty `argv`, integer `expected_exit`, and its own baseline/output paths. Shared reads are valid; sibling-output dependencies and overlapping writes are not. The validator is offline and failure returns to map review (exit 4); correct the contract or explicitly keep the parent task.

Mapper permission rules allow `read`, `grep` and `glob` everywhere inside the project, including repository metadata (`.git`/`.hg`/`.svn`) and an in-project run directory; the orchestrator excludes nothing (`exclusions: []`). Parent paths and outside paths (`external_directory`) are denied, and the session remains ask-before-permission and deny-by-default. Because OpenCode never resolves symlinks, the orchestrator scans the project before dispatch. Every in-project symlink that resolves outside the project, and every directory alias leading to one, is denied for read by name. OpenCode's matcher is case-sensitive, so on a filesystem that ignores letter case (probed, recorded as `boundary.case_insensitive`) these denies are also emitted case-folded (`?` per ASCII letter, `*` per non-ASCII character) and may block same-shaped in-project names. When any exists, `grep`/`glob` are denied for that session only, since they are authorized by pattern and follow a symlinked search path; the reason is in `state.json`, `coverage.json` and the map review. An incomplete scan means no dispatch (unavailable). A rescan after the run, on resume too, discards the map if the escaping-link records changed. Capture still rejects escaping selectors. Links changed by another process during the run are detected, not blocked (README: Known gaps / future tests). Timeout, blocked permission, invalid/oversized response, and uncertain interrupted work are retained as unavailable/partial artifacts; uncertain dispatches are never automatically relaunched. The map is historical navigation evidence, not exhaustive coverage. Executor edits do not refresh it, and current-source conclusions require live inspection. Usage/capture telemetry may be incomplete and must remain labelled unknown where unavailable.

Replacing an approved split or keeping its parent after split validation fails archives superseded prompts, posts, raw outputs, map attempts and outcomes under the old contract identity before publishing the replacement state. Both paths reset planning to round 1 and clear stale candidate/vote/reuse state. Archival or publication failures leave a checkpoint; resume must not reuse superseded posts.

### 2. Run

```
council.sh start  --config council.json --run-dir ~/dev/council-runs/<date>/council-<name>     # new run dir, must not exist
council.sh status --run-dir D                                                     # task/round, per-member context %, tokens, cost, pending questions
council.sh report --run-dir D                                                     # token report: prompt bytes by section, de-duplication replay, duplication left
council.sh resume --run-dir D --answers answers.json | --answer "text"            # after exit 4 (questions) or exit 2 (failure)
council.sh resume --run-dir D --replace C=claude:sonnet:xhigh                     # swap a member's model/session (repeatable)
```

`resume` re-runs only what is missing: a member that already has a valid post for the current
round is reused, not called again. `--replace ID=kind:model:effort` (`kind` = `opencode` \| `claude`)
gives a member a fresh session on another model — use it when its provider fails or runs out of
quota. The new session keeps the member's id and mode, and its first prompt carries a handover note
built from that member's own earlier posts, so it continues from its positions. The replaced
session is retired (visible in `status`/transcript as an earlier generation).

Runs take minutes (rounds × slowest member): start it detached (below) and read the log; keep
`--run-dir` under `~/dev/council-runs/<date>/`, not in the scratchpad: the Claude Code scratchpad
lives under `/private/tmp`, which macOS clears on reboot (a reboot mid-run on 2026-09-23 lost a
run's state). Exit codes: **0** all tasks reached consensus · **1** config error ·
**2** a member failed twice (checkpointed — inspect `D/raw/*.err`, fix, `resume`) · **4** the
council has questions for the user · **5** finished but a task is `unresolved`/`unratified`.

**Claude member calls.** Each `claude -p` call runs as the leader of its own process group (a
`python3` `os.setsid` exec wrapper, `scripts/council.sh:1431`), and every attempt writes its own
`D/raw/<tag>-<id>-a<N>.json` (`:1100`). `kill_group` stops the whole group: SIGTERM, up to 10 s,
SIGKILL, then up to 10 s until no live process is left (`:1034-1037`). It runs on timeout, after a
call that exited but left processes in its group, on TERM/INT/HUP to `start`/`resume` (the trap is
installed only there, `:1069-1074`, `:1957`, `:2011`), on a failed launch or handover (`:1612`,
`:1637`), and on `resume` for calls left in flight. If a group survives SIGKILL, the step is not
retried and the run stops with the failed checkpoint, exit 2 (`:1138`, `:1651`, `:1739`;
`trap_failed` `:1064-1067`). Limit: processes that moved into another session (Claude Code's Bash
tool starts its commands detached) stop only if claude stops them (`:1022-1023`). Measured with
Claude Code 2.1.286: claude stops them within 1 s of SIGTERM, but they survive if claude is
SIGKILLed; check `ps -A -o pid,ppid,command` for leftovers (README 0.11.1).

**Launching a run.** A council started as a background job of the Claude session dies with that
session. Launch it detached instead: double fork + `os.setsid`, so it ends up with PPID 1 in its own
session. `nohup` alone only sets SIGHUP to be ignored (`man nohup`). Run it under `caffeinate -i`,
because Mac idle sleep caused WebSocket timeouts. Keep the run log in the run dir's sibling
`<run-dir>.out` file. Watch the WHOLE log, not a narrow grep, and relay every event to the user.
The wrapper below was verified on 5/5 launches (detached, PPID 1, under `caffeinate -i`); for
resume, use the same wrapper with `resume --run-dir <run-dir> ...` as the arguments.

```
python3 - <<EOF
import os,sys
if os.fork(): sys.exit(0)
os.setsid()
if os.fork(): os._exit(0)
out=open("<run-dir>.out","ab")
os.dup2(out.fileno(),1); os.dup2(out.fileno(),2)
os.dup2(os.open("/dev/null",os.O_RDONLY),0)
os.execvp("caffeinate",["caffeinate","-i","<skill-dir>/scripts/council.sh","start","--config","<config>","--run-dir","<run-dir>"])
EOF
```

**Exit 4 — no assumptions.** Members must list every choice the task leaves open with what settles
it (`task` / `dir` / `user` answer / `ask`); anything not settled by the task, the working directory
or an earlier answer is turned into a question by the orchestrator, even if the member tried to
propose a default (tested: for "a script that prints a greeting" the council asks for the exact text,
language, shell and permissions instead of picking them). Read `D/questions.json`
(`[{id, member, question}]`), ask the user (AskUserQuestion, one question per item, verbatim — do not
answer on the user's behalf), write `answers.json` as `{"<id>": "<answer>"}` covering every id (or
`--answer TEXT` for one answer to all), and `resume`. The answers are relayed verbatim to every member
and the round is re-run without consuming the round budget.
For two or more pending questions of one task, `--answer TEXT` records one explicitly shared answer with all question IDs in `ids` and all original questions labelled inside `question`. The answer applies to every listed question. `id` remains the first ID; `member` lists distinct originating members in first-appearance order. Do not assume one `.answers[]` entry means one original question or that its `member` names one council member. Single-question answers and `--answers FILE` are unchanged. Older stored records are not migrated, and placeholder strings are never interpreted or recovered.

**Context handover.** After every call the member's context use is measured (OpenCode: tokens of the
last assistant message vs the model's context limit; Claude: `usage` of the last iteration vs
`modelUsage.contextWindow`). At ≥ `handover_at` (after at least 2 calls on that session) the session
writes a handover note, a fresh session is created for the same member id (generation +1), and the
note + council rules are prepended to its first prompt. `status` and the transcript show per member:
generation, context %, session tokens, cost, calls, retired sessions. Both include each
member's final-generation + retired subtotal and a **RUN TOTAL** across all generations.

**Handover gate (optional).** With `COUNCIL_HANDOVER_GATE=1` in the orchestrator's environment (it works on
`start` and on `resume` of an existing run), the delivered handover note waits for an outside review before the
successor reads it: `do_handover` writes `<note>.pending`, logs `handover gate: review <note>; remove
<note>.pending to release`, and polls until the marker is removed (`handover_gate`, `scripts/council.sh:1834-1850`). A replacement note (`resume --replace`) waits the same way.
The successor reads the note by path when its prompt is built, so a reviewer may correct the file in place before
releasing it. Without the gate the successor's prompt is written 0-1 s after the note (measured on 5 handovers), too
soon for any review. The wait is bounded by `COUNCIL_HANDOVER_GATE_TIMEOUT` seconds (default 3600); an unreleased
note is never delivered: the calls still in flight are stopped and the run stops with the failed checkpoint
(exit 2) before the old session is retired, so `resume` redoes the handover.

The handover prompt asks for the deliverable, half-finished work and mistakes not to repeat.

**Lossless prompt de-duplication.** In voting rounds, an exactly matching candidate or a
byte-identical proposal token can refer to a uniquely anchored peer post in the same prompt.
The source stays complete; exact comparisons and a byte-for-byte reconstruction check guard
every substitution, with full-text fallback on ambiguity. Substitutions go through the run log.
Votes still target the authoritative candidate in state. Capped diffs end with a visible
`DIFF TRUNCATED` line naming the 20000-byte cap and the directory to inspect.

**Code map (attributed evidence, avoiding re-reading the same files over and over).** Every new
run stamps `state.codemap_version=1` automatically (no config field; an older run without it runs
exactly as before). `scripts/council_codemap.py` (Python 3 stdlib only) keeps a content-addressed,
append-only map of `config.dir`'s files under `D/codemap/`: captured full-file bytes stored as
labelled base64 and addressed by SHA-256 (`sources/`, text and binary alike, with a tombstone for
any object recorded as lost so it is never recreated from later bytes under its old digest), the
recorded identities that actually held those bytes (`versions/` — root, display path and resolved
path, which is what a historical claim must bind against, and what keeps "some file somewhere had
these bytes" from being mistaken for evidence about *this* path), immutable
per-validation freshness records addressed by their own exact-byte digest and re-verified on every
read (`snapshots/`), one durable record per capture/validation/publication operation (`events/`),
the current published index of byte-range entries (`index.json`, mirrored to an immutable
content-addressed `index-snapshots/` entry named by `index-head.json` — the only thing a lost
index is ever recovered from, never posts and never today's source bytes), and a private per-attempt
staging/archive area (`attempts/`) — this is the map's own storage, not a generic object store.
The root is always `config.dir` from the run's own state: an orchestrator-supplied `--dir` is
verified against it, never trusted on its own. A member may optionally
add `code_reads` to its JSON tail: `{"path","lines":[first,last],"observed_sha256","symbol",
"conclusion","inspection"}`, all optional beyond `path`; omitting `code_reads` entirely means
unknown coverage. `symbol`/`conclusion`/`inspection` are the author's own claims, never proof of
examination — the locator delivered before every round-1/round-N prompt says this verbatim, and a
malformed optional item is recorded as a diagnostic without ever touching the post's vote or
triggering a retry. Round 1 of every task (even a later task in the same run) only ever sees a
raw-only, claim-free projection — identities, byte ranges, hashes, freshness — so independent
first-round reasoning is preserved; later rounds see attributed reports published by completed
earlier steps. A newly accepted member's claims are staged privately and never exposed to a peer
in the same still-in-progress step.

Before a step's outcome can affect consensus, a conservative round-level freshness guard
re-captures (by full content, never size/mtime) every source version the frozen view exposed as
current. Each capture is two complete reads, each fenced by before/after descriptor-identity and
resolved-path checks and hashed in its own right; a file modified while it is being read is never
accepted. A rejected read still counts as work done: the bytes it read, and — if it reached EOF
before the checks rejected it — the bytes it hashed, so an unstable source is never reported as
having cost nothing. That holds for **every** failure path, including one raised inside a helper
that knows nothing of the read (a symlink that escapes the root only after the read finished, a
re-resolution that itself fails, or a failure closing the descriptor, which happens past every
inner handler), because the counters belong to the read operation and are attached at a single
boundary rather than at each raise site — and an operational filesystem error never leaves that
boundary raw, it becomes the ordinary retry-worthy "unreadable" carrying those counters. What did
not happen is still never counted: a capture that failed before its hash reports zero bytes hashed. Only a demonstrated identity or path change counts against a vote — a parent directory
that merely became unreadable is "we could not check", not "it changed". An unrelated new capture never invalidates it; a whole-file edit, deletion, or symlink
retarget/escape of a guarded source does — even if the specific claimed excerpt was untouched. On
a confirmed change the attempt is marked with a distinct "stale" status, its posts are purged so
resume cannot reuse them, and the run checkpoints through the existing exit 2 path — the round is
replayed from scratch (same round/candidate, round budget not consumed) rather than the run trying
to guess which member's vote actually relied on the changed source. **Trade-off, accepted
deliberately:** without per-vote reliance declarations there is no sound way to tell which member
ignored a changed source, so the whole attempt replays even if only one claim was affected — a
false positive (an unrelated member re-answering) is preferred over a false negative (trusting a
vote that may have depended on stale evidence). If the guard itself cannot be evaluated (storage
broken, a capture unreadable/unstable) the accepted votes are preserved and the run checkpoints
pending verification — never classified as stale just because the map failed. "Checkpoint" here
means the run stops at exit 2 *before* the step's posts can be used for candidate selection,
consensus or reuse: an unverifiable guard is never treated as a pass. Everything is preserved —
accepted posts, staged reports, the attempt identity and its unresolved guard obligation (recorded
in `state.codemap_pending` together with the attempt, view and guard ids and the exact source
versions that attempt exposed). A resume re-verifies. Unchanged does **not** release the obligation, and the resume re-check
publishes nothing: the same attempt, frozen view, original exposed-source guard and accepted-event
attribution are all kept, and the step's own end-of-step barrier — reached again by the resumed
round, with those posts reused — is the only place the guard is discharged. That is also where an
outstanding publication (one that failed before the checkpoint) is retried. So a source that
changes *after* an unchanged re-check is still caught before the decision, because the decision is
made behind the *original* guard and never behind a fresh one captured after the change. An end
validation that could not verify the guard records no completed-step barrier at all. If publication
itself does not complete, the run holds at the same barrier rather than advancing with the work
outstanding. A stale verdict
is terminal — restoring the original bytes afterwards never revives the attempt, because nothing
can establish what the members read in between — and it archives the complete original posts (Markdown, accepted JSON tail
and attribution) inside the attempt's private area behind a durable ineligible marker, then replays
the same round and candidate.

Attribution is orchestration-owned and written **before** a member is launched, and every
genuinely new launch — a replacement, a handover, a re-dispatched retry — has its **own immutable
record** inside the attempt, never a rewrite of an earlier one. An accepted reply is bound to the
record of the launch that produced it, so neither an already accepted reply nor a replacement's
new one is ever re-labelled as the other, and reusing a checkpointed post allocates no launch and
keeps its original binding. Acceptance also writes an orchestration-owned **association** from the
live post file to the one accepted event those exact bytes were accepted as — its event id, launch
and attempt. Archives *resolve* that association, never the generation as it reads at archival
time and never a search for a record matching the member and the tail digest: the same member can
produce byte-identical tails from two launches, so that pair names no single event. Where no exact
association and no unambiguous single launch can speak for a post, the archive preserves it without
claiming provenance. Without a record for the launch being staged an accepted event is preserved in
full but stays **unattributed**: the caller's own word is not authentication, no other launch record
may stand in for the missing one, and none of its claims become bound findings. The same holds when
a member was launched more than once in an attempt and nothing identifies which launch produced the
reply — an ambiguous attribution is refused rather than guessed. Staging is exactly-once as well as publication: re-ingesting the same accepted
event preserves its original binding decisions verbatim, so a source that changed since acceptance
can never turn an unmatched report into a bound claim. Publication is exactly-once: each report is keyed by its accepted event plus its array
position, so replaying or republishing an attempt never duplicates reports, unbound records or
diagnostics, and publication itself requires both the recorded completed-step barrier and a passing
end-of-step guard. Captured bytes are stored at the moment of capture and never re-derived from the
live file at publication time; a stored object that is missing or fails its digest is reported as
missing evidence, never repaired from today's bytes. A published entry is labelled `current`, and
carries the validation's snapshot id, only when that validation actually re-captured **this** source
version and found it unchanged — same root, same display path, same resolved path, same digest.
Anything else is `historical` or `unverified` with no snapshot id: a matching path is not coverage,
and neither are equal bytes behind another real file. The end validation is also the last word on
what each display path holds, so an observation staged earlier in the step never overwrites it.
While a step is owed a decision, **every**
lookup route serves that one frozen view: naming another snapshot or another task/step returns an
explicit `unavailable_for_this_step` rather than widening what the step may read.

#### Telemetry (content-free, local, on by default)

`start` and `resume` record what happened — never what was said — so councils can be compared and improved.
`show`, `status` and `report` record nothing. Nothing is ever sent anywhere: no network.

- **Where.** Each run writes `D/telemetry.jsonl` (0600). On **every** exit of `start`/`resume` — 0, 1 once the run
  directory exists, 2, 4, 5, or a TERM/INT/HUP signal (143/130/129) — the events not yet exported, plus one
  cumulative `run_summary` snapshot, are appended in one locked write to the portable ledger
  `~/.council-telemetry/<host-id>.jsonl` (directory 0700, file 0600; override with `COUNCIL_TELEMETRY_DIR`). The
  byte offset already exported is kept in `D/telemetry.shipped`; `state.json` holds only `.telemetry {run, inv, seq}`,
  and each event's `seq` is reserved there (an atomic replace through an exclusively created temporary file) before
  its line is written, so an interrupted write never lets a later invocation reuse it. A destination reached through
  any symlink — whoever owns it, `/tmp` and `/var` included, and also when a `..` follows it — a symlinked or
  non-regular cursor or `state.json`, or a non-regular file is refused before anything is exported or replaced;
  existing telemetry files and directories lose group/other access, missing ledger parents are created 0700, and
  `state.json` keeps its own permissions. The export re-validates each unexported line and ships only records the
  schema accepts for this run (anything else is skipped, never copied), as does every reader.
  Copy the ledger folder from several Macs into one place and report over all of them: each file is one Mac.
- **Host id.** The first 16 hex characters of `sha256("council-telemetry-v1:" + IOPlatformUUID)` (from
  `ioreg`, bounded to 5 s). Without a platform UUID: a random 128-bit seed in `~/.config/council-telemetry/host-seed`
  (0600, outside the ledger folder) hashed the same way; failing both, `unknown`. The UUID, the seed, the host name
  and the user name are never written to telemetry.
- **What.** One JSON object per line, `schema` 1, with `ev`, `host`, `run` (a random uuid4 hex, the same across
  resumes), `inv` (the recorded invocation, 1..n), `seq` (per run, continued across invocations; each summary has
  its own) and `ts` (UTC epoch seconds). Events: `run_start`/`run_end` (command, status, skill/Claude/OpenCode
  versions the run already measured, configuration dimensions, exit code), `member`, `call_start`/`call_end`
  (member index, kind, mode, executor flag, model/effort, task index, split-child flag, step
  plan/exec/ratify/handover/map, round, generation, attempt — every launch of the member in that step and round
  over the whole run, across tasks, retries, resumes and generations; the mapper counts per task — outcome
  ok/failed/killed, one failure class, duration, provider duration, the observed exit codes (Claude `exit_code`,
  OpenCode `wait_exit_code`/`result_exit_code`), tokens {input, output, cache_read, cache_write, reasoning, total},
  cost, context),
  `retry`, `timeout`, `kill_group` (stopped/survived/refused/unknown — never a pid), `handover` (threshold or
  replace, context before the note, the note call's outcome), `gate`
  (wait and released/timeout/disabled), `questions` (counts and source member/mapper/contract at every exit-4
  checkpoint), `votes` (accepted tails per round, replays flagged), `outcome`
  (consensus/unresolved/ratified/unratified), `mapper_start` (the mapper operation's own identity) and one terminal
  `mapper` per operation (its `stage` prepare/session/dispatch/wait/recover/validate/capture/done, status
  complete/partial/unavailable/aborted, counts, linked to its operation and its call), `mapper_review`, `split_failed`,
  `reused`, `replace`,
  `stale_replay`; `run_summary` only in the ledger.
- **Never.** Member ids and task ids (indices instead), prompts, task text, posts, notes, answers, paths, project
  names, session ids, host or user names, provider error text. A model or effort outside
  `^[A-Za-z0-9._/:@~-]{1,100}$` is recorded as `other`. Failure classes `quota`/`auth` come only from the
  provider's own error evidence (OpenCode's structured message errors and adapter stderr; the structured errors and
  outcome of the very page `oc.sh result` fetched, which it hands over on a separate internal channel only when that
  fetch succeeded and the page was valid, for members and the mapper alike — never parsed out of the printed reply,
  never inferred from another page's reply, never a stale, unreadable or unmarked channel; Claude `.result` when
  `is_error`, CLI stderr) — never from a member's or the mapper's reply — and only the class name is kept. Without such
  evidence a failed call stays `cli_error`.
- **Unknown is not zero.** A value the run did not observe is `null`. OpenCode token components need a complete
  turn in the message page the run already fetched; per-call totals and costs are deltas only from a trusted
  baseline (a new session, or a sample taken earlier in the same invocation and generation) — the first call after
  a resume is unknown. Durations are launch to observed completion, so they include collection delay; Claude's
  own `duration_ms` is kept separately. A call interrupted by a crash or signal stays incomplete, never invented: a
  leftover process stopped on resume closes its call only when its launch key (a salted hash of the recorded
  process group and start time, kept by a `launched` event — never the pid) and generation identify exactly one
  recorded launch, and a recovered mapper reply only when its session's key matches one recorded dispatch; time
  proximity never links. Summaries count calls whose outcome is unknown (`outcome_unknown`) and question
  checkpoints whose count is unknown (`questions_unknown`) explicitly; `questions` sums only the known counts.
- **Resumes.** A run started without telemetry (opted out, or older than 0.12.0) records from its first enabled
  resume with `history: partial`. Every resumed segment carries `gap: true`: an opted-out invocation leaves no
  trace, so continuity is not guaranteed — it is not proof that calls are missing. Nothing is backfilled; events
  recorded but not yet exported are exported by the next enabled invocation.
- **Signals while arming.** A TERM/INT/HUP that arrives while telemetry obtains its identity (up to the 5 s
  hardware probe) is held until the identity and invocation exist, then delivered to the command's usual handling,
  so the exit status is unchanged and the run is still exported exactly once. This holds for a signal sent to the
  whole process group too (a terminal's Ctrl-C or hang-up): the arming helpers ignore those signals while they are
  held, the probe runs in its own session, and a helper's capture shell that the signal ended before the helper
  started is rerun, at most twice; a helper that may have run is never rerun.
- **Opt out.** `COUNCIL_TELEMETRY=0` (exactly `0`) disables everything for that invocation: no identity probe, no
  seed, no files, no `state.json` change. The offline report is unaffected.
- **Failures.** The first telemetry failure prints once `council: telemetry: write failed (<errno name>); telemetry
  off for this invocation` (a held ledger lock waits 5 s and reports `ETIMEDOUT`) and telemetry stays off until the
  next invocation, which exports what was missed. Telemetry never changes a status or an exit code.
- **Report.** `python3 scripts/ptools/telemetry_report.py [INPUT ...]` — see "Changing the skill".

### 3. Token report

`scripts/ptools/` is part of the skill, not an extra: `python3` is required and `council.sh` refuses to
start without it. Every finished run's transcript ends with a **Token report** — prompt bytes by section,
the de-duplication replay and the verbatim duplication still present — and `council.sh report --run-dir D`
prints the same report for any run at any time (offline, no model calls). On a run with the code
map enabled, both the transcript's Token report and `council.sh report` append a **Code map**
section from `scripts/ptools/codemap_report.py`. Everything there is counted from what the run
actually recorded — `index.json`, the durable per-operation records in `codemap/events/`, and the
locator byte boundaries the orchestrator recorded in `codemap/deliveries.jsonl` at the moment it
wrote each block, together with the identity (digest and size) of the prompt it then actually
launched. Nothing is inferred by searching a generated prompt for delimiter text, so marker text
inside a task, a stored handover note or a relayed author post is never counted; a prompt written
but never launched is reported as written, not delivered; a span that does not fit the prompt
recorded at launch is reported as a corrupt accounting record rather than counted; and intentional
duplicate deliveries are counted honestly. It reports distinct source identity by root/path/resolved_path/digest,
CAPTURED coverage (bytes the map holds and can reproduce) strictly apart from ATTESTED coverage
(what an author claims to have inspected), inspection-label counts (never presented as
verification), per-entry current/changed/missing/unreadable/unstable counts, and bytes actually
READ versus actually HASHED (the capture contract is two complete reads, each with its own full-file
hash, so the two counts move together on every successful capture and diverge exactly where a read
was aborted before EOF and therefore never hashed). Snapshots are
content-addressed, so two identical validation outcomes share one snapshot; events are not, so they
remain two countable events. Lookup is strictly read-only with no built-in log, so repeated-lookup
and stale/missing-lookup counts are unknown unless an explicit `--trace FILE` is supplied (and then
only as far as that trace claims completeness); trace repetition uses lookup's own request
normalisation and stale/missing counts come from the real response contract. Byte counts are never
converted into token or dollar figures and savings are never estimated.

### 4. Report

The transcript is `D/transcript.md`: roster, per-member context/tokens/cost/generations, per task
the outcome (`consensus` / `ratified` / `unresolved` / `unratified`), the agreed text, every post
by round and member, dissent, Q&A, and the log (handovers included). Report to the user: the outcome
and agreed text per task, who dissented and why if unresolved, files changed for build tasks (verify
them yourself with `oc.sh diff <ses>` / `git diff`), and the token/context summary. Session ids are
in the roster table — any member can be continued with `oc.sh prompt <ses_id>` / `claude -p --resume <uuid>`.

Optional analysis tools (Python 3 standard library only; no pip, network, or model calls):

```bash
python3 scripts/ptools/prompt_report.py D   # section bytes, largest prompts, measure 1a/1b replay savings
python3 scripts/ptools/dedup_check.py D     # verbatim repeated blocks >=128 bytes within each prompt
python3 scripts/ptools/codemap_report.py D  # code map: coverage, bound/unbound reports, freshness-guard status
```

All three are read-only and support `-h`. `prompt_report.py` documents its section boundaries in
its docstring; byte counts are not token counts. All three are invoked by the council's own
**Token report**: `prompt_report.py` and `dedup_check.py` on every finished run, and
`codemap_report.py` additionally when `state.codemap_version=1`. `prompt_report.py` and
`dedup_check.py` are hard startup requirements (`council.sh` refuses to start without them);
`codemap_report.py` is not — if it is missing, the report says the code map section is unavailable
and keeps its established exit behaviour, never failing the report. `scripts/council_codemap.py` is the code map itself
(`ingest`/`validate`/`lookup`/`locator`), and `council.sh` invokes it directly in every
deliberation round once `state.codemap_version=1` (stamped on every new run). Neither file is a
hard startup requirement: a run directory that predates the code map (no `state.codemap_version`)
keeps working with them absent, and on a supported-version run a helper that cannot be invoked
before any exposure disables map delivery for that whole attempt with a diagnostic — while an
attempt that has ALREADY exposed map-backed evidence checkpoints instead of silently dropping its
freshness obligation.

Per-session token report (`session_report.py`, same constraints: Python 3 standard library, read-only, `-h`):

```bash
python3 scripts/ptools/session_report.py --run-dir D [--projects DIR]                       # every member session of a run
python3 scripts/ptools/session_report.py --run-dir D --fetch-opencode [--oc scripts/oc.sh]  # + OpenCode messages via oc.sh
python3 scripts/ptools/session_report.py --session FILE.jsonl [FILE.jsonl ...]              # any Claude Code transcript(s)
```

Defaults: `--projects` `~/.claude/projects`, `--oc` `<script dir>/../oc.sh` (it only selects the adapter), `--chars-per-token` 3.2
(finite, > 0), `--top` 15 (>= 1); `--fetch-opencode` is off. It is **local by default**: without `--fetch-opencode` no subprocess is started at all (`--oc` only selects the adapter), and OpenCode
generations show the totals recorded in `state.json` with the per-call breakdown `unknown (not fetched; use --fetch-opencode)`.
`--fetch-opencode` (needs `--run-dir`) runs `oc.sh status`, then pages `GET /api/session/<id>/message` (first request
`order=asc&limit=200`, then cursor-only requests; at most 1000 pages per session; each `oc.sh` call and its `curl`/`jq` children are
killed after 120 s), so it also needs bash, curl and jq. The connection is pinned so `oc.sh` can never auto-start the service: a preset
non-empty `OPENCODE_URL` is used as is (with `OPENCODE_PASSWORD`/`OPENCODE_USERNAME` unchanged); otherwise `url` and `password` come from
`${XDG_STATE_HOME:-~/.local/state}/opencode/service.json`, and an empty, `null` or absent password removes any inherited
`OPENCODE_PASSWORD`. A missing or invalid state file (also a url or password with a lone surrogate) makes the OpenCode sessions `unavailable` with nothing spawned; credentials are never
printed. A session id or a next cursor that cannot be written as UTF-8 (a lone surrogate) is never requested or replaced: that generation is `unavailable (session id is not valid UTF-8 ...; not requested)` with its recorded totals kept, or that session's pagination stops `PARTIAL` with everything already read kept. The report opens with a Coverage section (per session: `complete` | `PARTIAL` | `unavailable` | `ambiguous` | `conflict` |
`unknown (not fetched)`, and the kind/model/effort of each generation with the source of that identity: `state.json` and its log, and
the one exception that an ambiguous generation with exactly one `<projects>/*/<sid>.jsonl` is read as Claude, its model shown only when
every `message.model` in it is equal; `PARTIAL` when a line of it cannot be parsed; the lookup is literal, so `*`, `?` and `[` in the session id or in `--projects` are plain characters and an id with a `/` names no transcript of that shape). A transcript that cannot be read is `unavailable (transcript unreadable: ...)` and the run still
reports. Exit 0 whenever a report is printed, also a PARTIAL one; exit 2 for a usage error (also a `state.json` with a member id, a member session id or a retired-entry
session id that is not a string or `null`, or a `retired` that is not a list or `null`) or when no session can be used. Missing values are
`unknown`, never 0, and a total with unknown components reads `<sum> (known components: k; unknown components: u; conflicting components: c)`
(the conflicting part is omitted when c is 0; a conflicting component is not counted as unknown). OpenCode includes its reported reasoning
once, and OpenCode calls that precede the first user message form their own segment, attributed `unknown`. A session id recorded by several
generations or by a member and the map pre-pass is counted once: identical records count once, differing known records are an excluded
`conflict`, and a component (tokens or cost) that is unknown for the member is filled from the mapper record, labelled `from mapper record`
(a mapper field that is unknown stays an unknown component); call numbers start at 0 (large cache writes, context drops); costs are only the recorded ones (a recorded 0 is labelled, since the orchestrator also writes 0
when no cost was reported); the split over items is a `heuristic estimate (character-based)`; task/round/phase are attributed only from a
byte-for-byte match with one of the member's own prompt files, otherwise `unknown`; a member with no id (absent or `null`) is attributed
`unknown` (provenance `no member id in state.json`), since its own prompt files cannot be identified. A `state.json` entry (a member, or a `retired` entry) that is not an object is listed in Coverage as `state.json: members[i] is not an object (ignored; ...)`, counted in the `PARTIAL:` line (`N state entries unusable`) and as two unknown components of the run totals, never silently dropped. The report is UTF-8 whatever the locale or `PYTHONIOENCODING` (a lone surrogate is written as `\udXXX`), and text from a transcript, `state.json` or an adapter (ids, model, effort, tool names and targets, message ids, an adapter's error line) is shown escaped (C0/C1 controls, U+2028/2029, lone surrogates as `\xNN`/`\uNNNN`, a backslash doubled, a pipe as `\|`), so it cannot forge a report line or end a table cell; the escaping happens only where text is printed, so ids from `state.json` are looked up, compared and requested exactly as recorded (a transcript file, an OpenCode request, a prompt file, a log line), and a first prompt with a lone surrogate is attributed `unknown` with that reason instead of being compared.

Build/test runner with a capped summary (`run_check.py`, Python 3 standard library, POSIX only, `-h`; it **executes** the command and **writes**
files, so unlike the other tools it is not read-only; use a `--log-dir` outside the project, `.gitignore` covers `*.log` only and
`diff_text` (`scripts/council.sh:2056-2057`) puts `git status --short` into the ratification diff when the executor is not an OpenCode member):

```bash
python3 scripts/ptools/run_check.py --log-dir D [--fallback-dir F] [--reports GLOB]... [--max-bytes N] [--timeout SECONDS] -- COMMAND [ARG...]
python3 scripts/ptools/run_check.py --log-dir /tmp/checks -- sh -c 'pytest --junitxml="$RUN_CHECK_REPORT_DIR/r.xml"'
```

`--log-dir` is required (no default; relative paths are taken against the cwd). `--max-bytes` defaults to 4000; the WHOLE stdout, final newline
included, stays within it, `omitted:` is always the last line and counts what is missing, and a value below the computed minimum (it grows with the
length of the log directory path; printed on refusal) is refused with exit 2 before anything is created; below 512 is a usage error.
`--timeout` (finite, > 0) stops the command's process group (SIGTERM, 5 s, SIGKILL). The command runs as argv without a shell, with the caller's
cwd and environment plus `RUN_CHECK_REPORT_DIR`, stdin `/dev/null`. Exit codes: the command's own (`COMMAND_EXIT rc`), 128+n for `SIGNAL n`, 124
`TIMEOUT (TIMED OUT ...)`, 130/143/129 when the wrapper itself got SIGINT/SIGTERM/SIGHUP, 125/126/127 `WRAPPER_ERROR` (log not creatable, not
executable, not found; the command did not run), 2 usage. The log is the raw OS-merged byte stream, lossless or explicitly `capture: INCOMPLETE`
(lines end only at LF: `sed -n 'A,Bp' LOG` agrees with the numbers, total = `wc -l`, or +1 without a final LF; `log continued:` lines name the
continuation files to read in order (a continuation on an unknown filesystem is still used); storage errors block the command instead of dropping output, `storage: STALLED_ON_STORAGE`). Failing tests
come from JUnit XML (`$RUN_CHECK_REPORT_DIR` = produced by this invocation, `--reports` globs are expanded by a lazy walker (entry by entry, in file system order, a capped source reads no further) that reports every failing listing or inspection, streams each one to the index and lets `omitted:` count the warning lines that do not fit, and are snapshotted before launch: `changed during this
run; concurrent writers not excluded`, `stale, ignored`, `removed` (only when the absence is established; a snapshot error stays a `PROVENANCE_ERROR` even if the file vanishes); `PROVENANCE_ERROR`/`REPORT_ERROR` lines and `reports not examined: N+` (N is a
lower bound of candidate occurrences; caps `MAX_REPORT_PATHS` 10000, `MAX_UNEXAMINED_COUNT` 1000000, `MAX_MARKUP_TOKEN_BYTES` 8388608 per markup
token, `DEDUP_CAP` 200000 are module constants; `dedup capped: ...` when later duplicates may be listed twice; a partial XML result says `(partial: n
failures kept)`; an unreadable entry of the private report directory is a `REPORT_ERROR` and the entries after it are still read), identified by `(classname, name, file)`, and from strict whole-line unittest/pytest text patterns labelled `text-heuristic` (a copy of each line has one trailing CR and every SGR colour sequence (at most 256 parameter characters) removed before it is matched;
the raw log, the bodies and the excerpts keep the bytes; OSC/hyperlink, cursor-movement and CR-only progress lines are not interpreted and stay declared; the unittest `subTest`
suffix ` (i=0)`, ` [why]`, ` [why] (s='a (b) c')` or ` (<subtest>)` is kept verbatim in `subtest` and every subTest failure is an occurrence of its own, numbered in a table of at most `DEDUP_CAP` identities: a later one has `occurrence` null and an `occurrences_capped` record and `WARNING` say so); a text
hit merges into a JUnit failure only when classname and name are exactly equal (file too when both carry one) and exactly one matches (and never after the
dedup cap), otherwise it stays a separate failing test (also when its identity equals a JUnit one); `FAILED (failures=..)`, `FAILED build`, `FAIL <message>`, indented lines and pytest `ERROR` lines are not recognised (the runner-count `WARNING` counts pytest `N failed` and `N error(s)`, and says when JUnit failures that the log does not corroborate hide a shortfall). Every
identity is in `<stem>.failures.jsonl` (complete message and streamed body records; for a text failure the index also holds its block: `body` records keyed by `block`, a `text_block` record with the log lines and the byte range, the message as `message`, `message_chars` and `message_more` continuation `message` records, the same fields on its `observation` and `duplicate` records; a unittest block runs from the FAIL/ERROR header to the next `=====` line, the `-----` line before `Ran N tests in`, another header or the end of the log, and its message is the last exception report; a pytest block is a `___ name ___` block of the FAILURES section, matched to a FAILED line only after the whole log was seen and only when the name is unique in both directions, otherwise recorded `unmatched` with a `coverage` record and a counted `WARNING` (the pending blocks and FAILED lines are fixed-size records up to `DEDUP_CAP`, then `text_block_association_capped`, and a name that recurs beyond the cap is kept in a fixed-size filter so that no block is attached to a name that is not unique); the detail lines of a text block carry their log line number; a `testsuite` that declares more failures or errors than it lists gives `WARNING: REPORT_MISMATCH`; text of the command or of a report is shown escaped with every literal backslash doubled (also the numbered excerpt lines, whose cuts count bytes); if stdout cannot take the summary the exit code stays the command's own and stderr gets one line saying so followed by the status, `log:`, `capture:`, `CAPTURE_PENDING:` and `failures index:` lines of the summary, claiming nothing that was not checked; a log segment that is shorter than its recorded length when the analysis reads it is never taken for a complete log (`CAPTURE_INCOMPLETE`, a `coverage` record `analysis_read_failed`, the summary-error form, `complete` false in a worker's `complete.json`); the file always ends on a complete record, a missing `end` record proves it incomplete, and a write error reads `failures index: INCOMPLETE (<ERRNO>; N records written, M not written)`; the worker removes a `final-summary.txt` or `complete.json` it could not write completely, and its final index replays the pre-launch discovery records from the foreground index by byte range, complete records only, with a `records_not_copied` record when it could not read them all or the foreground index never wrote them). The identities are listed while they fit in `--max-bytes` (only the first 200 failing tests get a detail block) and `omitted:` counts every warning not shown. If a
`setsid` descendant still holds the pipe 2 s after the command group ended the summary says `CAPTURE_PENDING` (prefix counts only; it names the fallback directory when there is one) and a
background worker writes `<stem>.final-failures.jsonl`, `<stem>.final-summary.txt` and, last, `<stem>.complete.json` at EOF: the final summary is the
authoritative one and a missing `complete.json` never means success. If summary production itself fails the exit code is kept and the output is
line 1, `log:`, `SUMMARY ERROR: ...` and `omitted: summary not produced; ...` (`unknown` for totals not established). Limits: FIFO stdout, `setsid`
descendants escape the group cleanup (the worker lives until EOF), bytes buffered in a killed process may be lost, a SIGKILLed wrapper leaves a
partial log, under a finite hard `RLIMIT_FSIZE` the index itself can be `INCOMPLETE`, a same-volume fallback cannot help when the volume is full.

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

## Changing the skill

`scripts/ptools/test_ptools.py` is the unittest suite of the `scripts/ptools/` tools (779 cases, including the telemetry writer/report), run automatically by
`scripts/test-completion.sh`.

`scripts/ptools/telemetry_report.py [INPUT ...]` reports offline over telemetry (read-only, standard library).
INPUT is a ledger file, a directory (its immediate `*.jsonl`) or a run directory (its `telemetry.jsonl`); the default
is `$COUNCIL_TELEMETRY_DIR`, else `~/.council-telemetry`; symlinks are skipped and counted, never followed; `-h` is
the only option. It prints plain-text sections per host and for all hosts: coverage, calls by model/kind with the
failure rate ((failed + killed) / all launches, retries included, incomplete calls unknown), failure classes,
timeouts and kill results, retries, durations by step (nearest-rank median/p90), costs per council and per step
(known subtotal + calls with unknown cost), handovers, gate waits, the mapper (complete and
complete+partial rates, repair rate), questions, votes and outcomes, and the latest summary per run (never added to
the event totals); then diagnostics. Records are deduplicated by (host, run, seq), so a run directory and its
ledger can be given together; records that conflict under one identity are excluded and counted; malformed or
truncated lines, other schemas, unknown events and invalid values are skipped and counted, never printed. Exit 0
with a report, 2 with no readable input.

`scripts/test-completion.sh` is an offline contract suite (no network, no model calls): it
loads the real functions from both scripts and stubs only curl/api/adapters, covering transport and
HTTP failures, permission-reply propagation, `wait_idle` completion, `show_result` validation,
`prompt`/`run` status propagation, failed council turns, exact prompt references/fallbacks,
post reuse, the question guard, diff truncation, run totals, and the code map's version gating,
prompt delivery, staging/publish pipeline, freshness-guard replay, resume re-verification,
handover framing, pre-launch attribution, recorded locator-delivery boundaries, the exit-2
checkpoint on an unverifiable guard, and the stale archive/replay path. It also pins the COMPLETE
execution, ratification and fix deliveries — fresh-session rules and predecessor-handover framing
included — to SHA-256 baselines recovered from the pre-change commit `d62a356`, asserted with the
map both enabled and disabled, so map content cannot leak into a non-deliberation prompt. Run it plus **separate** `/bin/bash -n` invocations for `oc.sh`, `council.sh`,
and `test-completion.sh` after changes.
