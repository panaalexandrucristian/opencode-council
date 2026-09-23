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
| context handover threshold | `handover_at` (0..1], council-wide **and/or per member** | a fraction in (0,1] of the model's context window, **or an absolute token count** (≥ 1000, e.g. `150000`). At the threshold the session writes a handover note and is replaced by a fresh session (same member id, next generation). Set it per member to make one hand over earlier than the rest (`{"id":"A", …, "handover_at":0.3}`); a member without its own value uses the council-wide one. The roster prints the effective threshold per member, and `status` shows each member's context as a percentage of its own threshold |

Then: `council.sh show --config council.json` → paste the roster to the user and get a yes before `start`.
`show` also prints members × rounds × tasks, the high/highest-effort members, and a
config-only cost note with two measured comparisons and cost-reduction levers. Its warning
is advisory; the measurements are not dollar predictions.

### 2. Run

```
council.sh start  --config council.json --run-dir <scratchpad>/council-<name>     # new run dir, must not exist
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

### 3. Tests

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
`scripts/ptools/test_ptools.py` is their unittest suite (156 cases), run automatically by
`scripts/test-completion.sh`.

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
