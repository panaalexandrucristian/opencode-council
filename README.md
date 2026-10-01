# opencode-council

A [Claude Code](https://claude.com/claude-code) skill that drives [OpenCode](https://opencode.ai) through its HTTP API — and runs a **cross-CLI council**: OpenCode sessions and Claude Code CLI sessions, each on a model and effort you choose, that discuss a list of tasks, ask you instead of assuming, and must reach a unanimous consensus.

## What is inside

| file | purpose |
|---|---|
| `SKILL.md` | the skill Claude Code loads (`/opencode …`): workflow, permissions, the council checklist |
| `scripts/oc.sh` | thin bash + curl + jq CLI over the OpenCode v2 background service (sessions, prompts, wait, diff, models, `--variant` = effort) |
| `scripts/council.sh` | the council orchestrator (see below) |
| `scripts/ptools/` | optional, offline Python 3 stdlib prompt-byte report and same-prompt duplicate audit; `session_report.py` (per-session token report, local by default); `handoff_test.py` (the adapter to the external handoff-test-kit) and `handoff_replay.py` (its replay over a corpus of real handovers); `run_check.py` (runs a build or test command with the full log on disk and a capped summary on stdout; unlike the others it executes commands and writes artifacts, so it is not read-only) |
| `reference.md` | OpenCode HTTP endpoint notes |
| `.claude-plugin/` | plugin + marketplace manifests, so the repo installs with `/plugin install` |
| `examples/` | a real end-to-end run (config, answers, console output, transcript) |

## Install

### 1. Prerequisites

| tool | why | check |
|---|---|---|
| [OpenCode](https://opencode.ai) **v2** (`opencode`) | runs the OpenCode sessions / background service | `opencode --version` → `v2.x` |
| [Claude Code](https://claude.com/claude-code) (`claude`) | loads the skill; also runs Claude council members via `claude -p` | `claude --version` |
| `bash` (3.2+ is fine, macOS default works), `curl`, `jq` | the scripts | `jq --version` |
| `python3` (macOS ships one; standard library only) | `scripts/ptools/` — the token report and duplicate audit the council runs on every finished run | `python3 --version` |

Authenticate at least one OpenCode provider (each council member's model must belong to an enabled provider):

```bash
opencode auth login        # e.g. OpenAI (ChatGPT/Codex), Google, Kimi, Moonshot …
```

### 2. Install the skill

**Option A — as a Claude Code plugin (recommended).** The repository is its own plugin marketplace, so two commands inside Claude Code install it, and `/plugin` keeps it updated:

```
/plugin marketplace add panaalexandrucristian/opencode-council
/plugin install opencode-council@opencode-council
```

(or from a terminal: `claude plugin marketplace add panaalexandrucristian/opencode-council` and `claude plugin install opencode-council@opencode-council`; add `--scope project` to install for one repository only). Plugin skills are namespaced, so the skill is invoked as **`/opencode-council:opencode …`**. To update later: `claude plugin marketplace update opencode-council && claude plugin update opencode-council@opencode-council`, then restart Claude Code.

**Option B — with the `skills` CLI** ([vercel-labs/skills](https://github.com/vercel-labs/skills), works for Claude Code and other agents; discovers the `SKILL.md` at the repo root):

```bash
npx skills add panaalexandrucristian/opencode-council -g -a claude-code   # -g = ~/.claude/skills, omit for ./.claude/skills
```

**Option C — plain `git clone`** as a user skill (available in every project) or a project skill:

```bash
git clone https://github.com/panaalexandrucristian/opencode-council.git ~/.claude/skills/opencode      # user skill
git clone https://github.com/panaalexandrucristian/opencode-council.git .claude/skills/opencode        # project skill
```

With B or C the skill is invoked as `/opencode …` — the name comes from the `name: opencode` line in `SKILL.md`, not from the folder. Nothing else to configure: the scripts read the OpenCode service URL/password from `~/.local/state/opencode/service.json` and start the service (`opencode service start`) when it is not running.

### 3. Verify

```bash
~/.claude/skills/opencode/scripts/oc.sh ensure          # {"url":"http://127.0.0.1:49374","version":"2.0.11",...}
~/.claude/skills/opencode/scripts/oc.sh models          # enabled models as provider/id — pick council members from here
claude -p "reply OK" --model sonnet --output-format json | jq .result   # only needed for Claude council members
```

Then, in Claude Code:

```
/opencode list the models available in OpenCode
/opencode vreau un consiliu: 2 OpenCode (Astra high, Gemini 3.1 Pro high) + 1 Claude (Sonnet medium, executor) pe acest proiect …
```

(`/opencode-council:opencode …` when installed as a plugin.)

`SKILL.md` lists `scripts/oc.sh` and `scripts/council.sh` under `allowed-tools`, so Claude Code should not prompt for them while the skill is active; if your setup still prompts, allow the two commands once (or add them to `permissions.allow` in `~/.claude/settings.json`).

### 4. Update

Plugin: `claude plugin marketplace update opencode-council && claude plugin update opencode-council@opencode-council` (restart Claude Code to apply). `skills` CLI: re-run `npx skills add …`. Git clone: `git -C ~/.claude/skills/opencode pull`.

## The council

```bash
scripts/council.sh show   --config council.json              # validate + print the roster (no sessions created)
scripts/council.sh start  --config council.json --run-dir D  # run all tasks
scripts/council.sh status --run-dir D                        # task/round, per-member context %, tokens, cost
council.sh report --run-dir D                        # token report (prompt bytes by section, de-duplication, duplication left)
scripts/council.sh resume --run-dir D --answers answers.json # continue after the council asked questions
council.sh resume --run-dir D --replace C=claude:sonnet:xhigh   # give a member a fresh session on another model
```

`council.json` — every field is explicit, nothing is defaulted silently:

```json
{
  "dir": "/abs/project", "max_rounds": 4, "timeout_s": 600, "handover_at": 0.5, "max_turns": 30,
  "map_code": false,
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

Before writing a council config, ask once: **“Map the code for this council?”** Record the answer in the run-wide `map_code` boolean. `true` maps every task; `false` or an omitted field preserves the current path without mapping or a confirmation pause. Optional mapper settings live in `map_prepass`, for example `{"kind":"opencode","model":"google/gemini-3.8-flash","effort":"medium","timeout_s":120,"max_output_bytes":65536}`. If model or effort is missing while `map_code` is true, `start` proposes the missing value(s) and stops before any inference call. Confirm the exact displayed tuple with `resume --run-dir D --confirm-mapper FILE`, where FILE is JSON with `kind`, `model`, and `effort`. After each original task's map, review the complete locator and provide `resume --run-dir D --map-decision FILE` with `{"action":"keep"}` or `{"action":"split","contract_file":"..."}`. User-authored split contracts are validated offline before any following model call.

Split contract paths are resolved relative to the directory containing the map-decision JSON. The archived contract is validated and then used as the source for child tasks. Its schema is `schema_version: 1`, `parent_id`, the exact **contract `map_seed_id` printed in map review** (from `coverage.json`), and a non-empty `subtasks` array. The locator's `snapshot_id` is a separate lookup identity; do not substitute it for `map_seed_id`. Each child has a unique `id`, complete `text`, boolean `execute`, arrays `requires`/`modifies`/`deletes`/`creates`/`acceptance`/`unresolved`. Baseline path declarations use `{ "path": "src/file.py", "sha256": "<64 lowercase hex>" }`; `creates` contains absent project-relative paths. Acceptance entries contain a unique `id`, `description`, non-empty `argv`, integer `expected_exit`, and `requires` paths limited to the child's baseline inputs and own outputs. Shared read-only prerequisites are permitted; sibling output dependencies and overlapping writes are rejected. Invalid contracts stop at the map-review checkpoint (exit 4) for correction or `keep`.

Minimal executable contract shape (replace the digest with the actual baseline SHA-256, and copy the contract `map_seed_id` printed at review; configure an executor because both children execute):

```json
{
  "schema_version": 1,
  "parent_id": "implement-feature",
  "map_seed_id": "<reviewed-contract-map-seed-id>",
  "subtasks": [
    {
      "id": "update-source",
      "text": "Update the source behavior and its focused test.",
      "execute": true,
      "requires": [{"path": "src/app.py", "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}],
      "modifies": [{"path": "src/app.py", "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}],
      "deletes": [], "creates": [], "unresolved": [],
      "acceptance": [{"id": "focused-test", "description": "Focused test passes", "argv": ["python3", "-m", "unittest", "tests.test_app"], "expected_exit": 0, "requires": ["src/app.py"]}]
    },
    {
      "id": "write-docs",
      "text": "Document the feature using the current source as read-only context.",
      "execute": true,
      "requires": [{"path": "docs/style-guide.md", "sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}],
      "modifies": [], "deletes": [], "creates": ["docs/feature.md"], "unresolved": [], "acceptance": []
    }
  ]
}
```

Mapper calls are read-only, ask before permission, and deny everything by default. Inside the project the mapper may `read` (including directory listings), `grep` and `glob` everywhere: the orchestrator excludes nothing, so repository metadata (`.git`, `.hg`, `.svn`) and an in-project run directory are readable, searchable and capturable like any other path (`exclusions` in `input.json` and `coverage.json` is `[]`). Parent paths (`..`) are denied, and outside paths are denied through `external_directory`. OpenCode checks paths lexically and never resolves a symlink, so the orchestrator enforces the project boundary itself before the mapper session exists. It scans the whole project, metadata and run directory included, without following links. It records every in-project symlink whose resolved target lies outside the project, and every in-project directory symlink whose target contains such a link. It denies read of each recorded link and of everything beneath it, in relative and absolute form. OpenCode's permission matcher is case-sensitive, so when the scan's probe finds that the project filesystem ignores letter case (for example default APFS), each recorded link is also denied in a case-folded form: every ASCII letter becomes `?` and every non-ASCII character `*`, which also covers Unicode normalization variants. These folded denies may also block in-project names of the same shape (the same length, with non-letters in the same places), for example a six-letter directory when `outdir` escapes; the probe result is recorded as `boundary.case_insensitive`, and a change of it at rescan discards the map. `grep` and `glob` are authorized by their search pattern, and they search the directory named by their path argument, following a symlink there, so no rule can keep them off an escaping link. When the scan finds any, `grep` and `glob` are denied for that mapper session only; the reason is stored in `state.json` (`map_prepasses.<task>.boundary`) and `coverage.json` (`boundary`) and shown at map review, and the mapper still runs with `read`. If the scan cannot complete (for example an unreadable directory), the mapper is not dispatched and the pre-pass is unavailable. After the mapper finishes, a completed session recovered on `resume` included, the project is scanned again. If the escaping-link records (paths and resolved targets) differ from the persisted baseline, or the rescan fails, the map is discarded as unavailable with the reason recorded. Capture still resolves every selector and rejects any that leaves the project. These are scan-time protections, not access-time isolation: see [Known gaps / future tests](#known-gaps--future-tests). A timeout, blocked permission, malformed/oversized response, or uncertain interrupted dispatch is recorded as partial/unavailable; uncertain work is not relaunched automatically. The map is a navigation aid, never exhaustive. Seed evidence remains historical after executor edits; current-source claims still require inspection. Usage and capture telemetry can be incomplete and are labelled unknown rather than priced or treated as savings.

When a previously approved split is replaced or resolved with `keep`, superseded prompts, posts, raw outputs, evidence and results are archived under the old contract identity before the new state is published. The transition starts at round 1 and does not reuse old posts, even if a child ID is reused. If archival or state publication fails, the run stays checkpointed.

How it works:

- **Roster first.** `show` prints how many sessions there are, how many OpenCode vs Claude Code, each member's model, effort (validated against the model's variants), mode and context window — before anything runs.
- **Members talk to each other.** Every round the orchestrator relays the other members' posts (by name) into each session. Posts are prose plus a mandatory JSON tail the orchestrator parses.
- **Exact same-prompt references.** Voting prompts de-duplicate only identical candidate/proposal text against a complete, uniquely anchored peer post in that prompt. Byte-for-byte reconstruction checks and full-text fallback protect identity; stored posts and voting state stay authoritative. Every substitution is logged.
- **Unanimous consensus.** Round 1 = proposals; then a rotating proposer's position is the frozen candidate and everyone votes `agree` / `disagree` (with a complete revised proposal). All `agree` = consensus. `max_rounds` reached = `unresolved` (exit 5), dissent preserved — never a forced verdict.
- **Build tasks.** Consensus on a plan → the single `executor` implements it → the council ratifies the report + diff (fix rounds if needed).
- **No assumptions.** Each member must list every choice the task leaves open and what settles it (`task` / `dir` / `user` / `ask`). Anything not settled by the task, the working directory or an earlier answer becomes a question for the user: the run pauses (exit 4, `questions.json`), you answer, `resume` re-runs the round.
- **Context handover.** After every call the member's context use is measured against the model's window; at `handover_at` — a fraction of the model's window, or an absolute token count such as `150000` — the session writes a handover note and is replaced by a fresh session (same member, next generation) that starts from the note. `handover_at` is a council-wide default that any member can override with its own value, so a member on a small context window (or an expensive one) hands over earlier.
- **Transcript.** `D/transcript.md`: roster, per-member tokens/context/cost/generations, every post by task and round, outcomes, Q&A, log.
- **Visible truncation.** Diffs retain their 20000-byte cap; truncated output ends with the cap and the working directory to inspect.

- **Resume is cheap and swappable.** `resume` re-runs only what is missing — a member that already
  has a valid post for the current round is reused. `--replace ID=kind:model:effort` moves a member
  to a fresh session on another model (its provider failed or ran out of quota); the new session
  keeps the member's id and gets a handover note built from that member's own earlier posts.
- **Prose style (optional).** `"style": "normal" | "lite" | "caveman" | "ultra"`, top level and/or per member,
  compresses the prose members write for each other (after the [caveman skill](https://github.com/juliusbrussee/caveman)).
  The JSON tail's `proposal`/`report` — the text that is voted on and implemented — plus quoted code, paths,
  commands, errors and numbers are never compressed. Expect single-digit % of output tokens in this council,
  not the headline figures: the JSON tail is 74–99% of a post.
- **Token report.** Every finished run's transcript ends with a Token report (prompt bytes by section,
  the de-duplication replay, and the verbatim duplication still present); `council.sh report --run-dir D`
  prints it for any run at any time. It comes from `scripts/ptools/` — required, not optional — which runs
  offline with no model calls.
- **Tests.** `bash scripts/test-completion.sh` — 1395 offline checks (no network, no model calls), including
  `scripts/ptools/test_ptools.py` (746 Python standard-library unittest cases for the analysis tools and the handoff-test adapter).
  Run `/bin/bash -n` separately on each changed shell script after any change.

Exit codes: `0` all tasks reached consensus · `1` config error · `2` a member failed twice (checkpointed, `resume`) · `4` questions pending · `5` some task unresolved.

## Handoff tests (external handoff-test-kit)

Handover documents are tested with the external [handoff-test-kit](https://github.com/panaalexandrucristian/handoff-test-kit).
The kit is a dependency, not part of this repository: it is never copied, vendored or added as a submodule, and
its code is never changed from here. Any revision is accepted; its output is checked against the kit's contract
(`== 1.` … `== 5.` section headers, one `result:` line whose counts match the entries, exit 0..3) and anything
else is an operational error, never a pass. The kit revision (`git rev-parse HEAD`, if available) is recorded
with every result.

**Where the kit is looked for.** `council.json` field `handoff_kit` (a kit directory; relative = relative to
`dir`) > environment variable `HANDOFF_TEST_KIT` (directory) > `/Users/apana/dev/handoff-test-kit`, used only
if it exists. A directory is a kit only if it holds both `handoff-test.sh` and `handoff-fix.py`. An explicitly
set field or variable that is invalid gives a named skip (`kit not found: …`) and never falls back to a later
source. No kit found is a named skip, never a pass.

**Member handover notes (automatic).** After `do_handover` writes `D/raw/handover-g<N>-<ID>.md` and before the
successor sees it, the orchestrator runs `scripts/ptools/handoff_test.py`:

- The raw note stays byte-identical. The kit runs on a copy, `D/handoff/handover-g<N>-<ID>.stage.md`, with a
  generated config (`D/handoff/…config.json`): `repo_root` = `dir`, `path_bases` = [`D`], the kit's generic
  topics, no assertions/external/version probes. Optional `handoff_config` (a path to your own kit config,
  relative = relative to `dir`) **replaces** the generated config; if it has no `repo_root`, `dir` is used, and
  a relative `repo_root` is resolved from that config file's directory, as the kit does. `HANDOFF_TEST_CONFIG`
  is not passed to the kit: the config is always named explicitly.
- **Section 4 is not applicable to member notes.** Section 4 *executes* the first `python3 - <<'PY' … PY` block of
  a document. Every such block is removed from the copy (the copy is re-checked with the kit's own regex before
  the kit runs), so nothing from a member's note is ever executed. The section-4 gap does not count toward the
  note's verdict: the result records the kit's raw exit and an *effective* exit without it.
- If the effective verdict is 1 (facts drifted) or 2 (coverage gap), the kit runs with `--fix` on the copy.
  The section-4 entry is removed from the generated `## ⚠️ Gaps flagged by the self-test` checklist (a checklist
  left empty is removed), the repair is mapped back onto the ORIGINAL bytes — python blocks restored
  byte-for-byte where they were — and the result must equal the original with exactly the repair the kit
  authorizes, outside its checklist. Before `--fix` runs, the adapter asks the kit's own fixer
  (`handoff-fix.py`: `load_config`, `validate`, `facts`, `plan_edits`, with the same config and copy, inside the
  budget) which spans it may rewrite and with which values (a SHA token on a line naming the configured
  branch/MR that is a real commit, a version captured by a selected probe), and whether its banner precondition
  holds (branch merged, no banner yet). Those spans, mapped to original offsets, and the banner are recorded in
  `result.json` (`fix.authorization`); any other change, including a same-shaped SHA, version or number
  anywhere else, is refused. With the generated config nothing is authorized but the checklist. The successor then receives
  `D/handoff/handover-g<N>-<ID>.fixed.md`, even when findings remain after the repair (they need a human; the
  checklist says so). A repair that cannot be mapped back exactly, or that touches any other byte, is refused and
  the original is delivered.
- `D/handoff/` keeps, per note, the kit's stdout/stderr for the check and the fix, the unified diff of the
  delivered repair and `<stem>.result.json`. The kit's `.bak-*` file (a copy of the staged copy) is deleted.
- One budget, `handoff_timeout_s` (default 60 s), covers check + `--fix` + recheck, enforced with the same
  deadline-and-kill loop the orchestrator uses for a Claude member call; the kit runs in its own process group,
  killed as a whole.
- **It never blocks the council.** No kit, an invalid setting, kit exit 3, unrecognised output, a refused
  repair, a timeout or a crash of the adapter: the reason is logged and the original note is delivered.
- Every result goes to `state.json` (`handoff_tests[]`, with task, member and generation), to the run log and to
  a **Handoff tests** table in `D/transcript.md` and in `council.sh report`. Paths the kit could not resolve that
  look like a command with an environment assignment (`NAME=value cmd …`) or a git-status line (`M `, `A `,
  `??` …) are listed there as **probable kit false positives**; the verdict is not changed.
- Replacement notes (`D/raw/replace-<ID>-g<N>.md`, from `resume --replace`) are tested check-only: never
  repaired, never altered; the result is logged so their gaps are visible.

The handover prompt now also asks for (6) the deliverable, (7) what is half-finished or left running and
(8) mistakes not to repeat. In the replay corpus (29 member notes from this machine) those were the most
often missing topics (baseline: 25, 26 and 26 notes). The kit's coverage check is a regex: it proves a topic is
mentioned, not that it is answered.

**Session handovers (on request).**

```bash
scripts/council.sh handoff-test [--fix] [--config c.json] [--kit DIR] [--timeout S] HANDOVER.md
```

runs the kit unchanged — section 4 included, so only on a document you trust — and passes its exit code
through: 0 clean · 1 facts drifted · 2 coverage gap · 3 usage/config/operational error (also a `--timeout`
expiry). `--fix` is passed only when you give it. Without `--config` the kit's own config discovery applies.
No kit found: the reason on stderr and exit **4**, never 0. No timeout unless `--timeout S`.

**Replay over real handovers.**

```bash
python3 scripts/ptools/handoff_replay.py <corpus-dir> [--kit DIR] [--evidence DIR]
```

runs the integration on every file listed in `<corpus-dir>/index.json` (`file`, `source`, `project_dir`,
`has_python_block`), each on its own copy; the corpus is never modified (checked by digest). Member notes go
through the council path (`repo_root` = the note's `project_dir`, `path_bases` = its original run directory if
it still exists, section 4 disabled, `--fix`). Session handovers (`project_dir` null) go through the
`handoff-test` path — kit unchanged, no `--fix` — on a disposable copy placed in the original source directory
(so the kit's config discovery and relative paths are the original ones) and removed afterwards. A document with
a python block is reported, never run. It prints a per-file table and member/session aggregates next to the
corpus's `baseline-noconfig.tsv`, and exits 0 only when every member note was tested, no block was executed and
every delivered note is the original plus exactly its recorded authorized repair; 2 when no kit is found.

## Release notes

- **0.11.1** — a Claude member call that hits `timeout_s` no longer keeps running orphaned. Each `claude -p` call
  runs as the leader of its own process group (a `python3` `os.setsid` exec wrapper), and one helper stops the
  whole group: SIGTERM, up to 10 s, SIGKILL, then up to 10 s until no live process is left. It runs on timeout,
  after a call that exited but left processes in its group, on SIGTERM/SIGINT/SIGHUP to `start`/`resume`, on a
  failed launch or handover, and on `resume` for calls a previous orchestrator left in flight. Every attempt
  writes its own `raw/<tag>-<id>-a<N>.json`, so a retry can no longer share a file with the call it replaces. If
  a group survives SIGKILL, the step is not retried and the run stops with the failed checkpoint (exit 2).
  Limit: Claude Code's Bash tool starts its commands in their own session. Measured with Claude Code 2.1.286,
  claude stops them itself within 1 s of SIGTERM (foreground and `run_in_background`), but if claude ignores
  SIGTERM for 10 s and is killed, those commands survive; check `ps -A -o pid,ppid,command` for leftovers. A
  0.11.0 in-flight record (pid only) cannot be verified on resume and is only reported. New `orphan_tests`
  suite (26 checks); test-completion.sh: 1395 checks.
- **0.11.0** — two new analysis tools: `scripts/ptools/session_report.py` (where a council run's tokens and cost
  go, per member session and per task/phase; local by default, `--fetch-opencode` for OpenCode sessions;
  unknown and conflicting values are counted, never guessed) and `scripts/ptools/run_check.py` (runs a command,
  keeps the full log byte-exact with a verified sha256, and prints a bounded summary of the failing tests from
  JUnit reports or strict unittest/pytest text recognition, with a complete failures index; every omission is
  marked and counted). Both were hardened by an adversarial test campaign; the suite is now 746 unittest cases.
- **0.10.0** — handover notes are tested with the external handoff-test-kit before the successor reads them
  (section 4 disabled for member notes, failing notes repaired with the kit's `--fix` and mapped back onto the
  original bytes, never blocking the council); `council.sh handoff-test` for session handovers; a corpus replay;
  the handover prompt asks for the deliverable, half-finished work and mistakes not to repeat.

## Known gaps / future tests

Tracked limitations of the map pre-pass and the tests not yet written:

- **Links changed during the mapper run.** The boundary comes from a scan just before the session and a rescan after it. A symlink created or retargeted by another process while the mapper runs is not blocked when it is accessed. The rescan discards the map when the final set differs, but it cannot undo a read, and it cannot see a link that was created and removed again between the two scans. The mapper itself cannot create links (edit and shell are denied).
- **OpenCode's built-in search filters.** OpenCode's own `grep` and `glob` always pass `--glob=!**/.git/**` to ripgrep, `glob` skips dot-paths unless the mapper passes `hidden: true`, and ripgrep honours `.gitignore`. The orchestrator adds no exclusion of its own, and `read` reaches `.git`, but these built-in skips remain. OpenCode is not modified.
- **Directory aliases are blocked as a whole.** An in-project directory symlink whose target contains an escaping link is denied entirely, so its other in-project content is reachable only through the real path.
- **Case-folded denies over-match.** On a filesystem that ignores letter case, OpenCode's case-sensitive matcher would let a case variant of an escaping link (`ESCAPE.PY` for `escape.py`) through, so the per-link denies are case-folded (`?` per ASCII letter, `*` per non-ASCII character). They can also block unrelated in-project names of the same shape, which the mapper then cannot read.
- **Hard links** cannot be told apart from ordinary files by path, so a hard link to an outside file is neither blocked nor detected.
- **Handoff tests — coverage is a regex.** The kit's section 5 proves that a topic is mentioned, not that it is
  answered; the appended checklist only says what is missing.
- **Handoff tests — kit false positives.** The kit reads some inline code as paths (commands with an environment
  assignment, git-status lines). They are listed as probable false positives and not filtered; an upstream issue
  for the kit is drafted, not filed.
- **Handoff tests — what `--fix` can repair.** With the generated config there are no fact probes, so `--fix`
  only adds the gap checklist; SHA/version repairs need a `handoff_config` with `external`/`version_probes`. A
  git fetch the kit starts for such a config runs in its own session and is bounded by the kit (15 s), not by
  `handoff_timeout_s`'s process-group kill; it can run twice (once when the authorized spans are planned, once
  in `--fix`). If the facts change between the two (the branch moves), the repair no longer matches the plan and
  is refused (original delivered).
- **Handoff tests — contract drift.** A future kit revision that changes its output format or fixer interface
  is detected by the contract checks and reported as an operational error (original note delivered), not
  adapted to.
- **Future tests:** the exhaustive alias/hard-link permutation matrix; fault injection at every persistence boundary not yet covered (the initial state write, the version marker, the authorization and the mapper checkpoints are covered); byte-level parity of the reports.

## Example

[`examples/`](examples/) is a real run: a 2-member council (Claude Sonnet read-only + OpenCode Kimi K3
as executor) gets "create `hello.sh` that prints a greeting", **asks** for the exact text, language,
shell and permissions instead of assuming them, then agrees a plan, implements it and ratifies the
result. It contains the `council.json`, the `answers.json`, the console output of `show` / `start` /
`resume`, and the full [`transcript.md`](examples/transcript.md).

## Cost and billing

Council members are billed exactly like any other Claude Code / OpenCode session — headless mode is not priced differently:

- **Claude members** (`claude -p … --resume`) use whatever `claude` is logged in with. On a claude.ai subscription (Pro/Max/Team/Enterprise) they are included in the plan and draw from the same 5-hour/weekly windows as your interactive session; the `cost` figures in `status` and the transcript are then only local list-price estimates. With an API key they are billed per token at list price. `council.sh` never uses `--bare` (which would ignore the subscription login and require `ANTHROPIC_API_KEY`).
- **OpenCode members** are billed by the provider behind the model (OpenAI, Google, Kimi plan, …); `cost` comes from OpenCode's own accounting (0 on flat-rate plans).
- N members = N full agent contexts per round, so a council uses a multiple of what one session would. Effort (`variant` / `--effort`) is the main lever.
- Token/cost accounting follows the Claude Code version: from v2.1.277 a resumed session reports its cumulative spend, so `council.sh` reads the latest result instead of summing (detected at `start`, stored as `cl_cumulative` in `state.json`).
- `status` and the transcript show final-generation + retired subtotals per member and one **RUN TOTAL**, including handovers and replacements.
- `show` adds an advisory cost note computed from the config: members × rounds × tasks, high-cost efforts, two measured similar runs, and levers (fewer members, lower effort, narrower tasks, fewer rounds). It never predicts a dollar bill or blocks a run.

Optional read-only analysis (Python 3 standard library, no pip or model calls):

```bash
python3 scripts/ptools/prompt_report.py D  # section bytes, biggest prompts, exact measure-1 savings
python3 scripts/ptools/dedup_check.py D    # repeated verbatim blocks >=128 bytes in each prompt
```

Both support `-h`. These tools are independent of the shell runtime; Python and `scripts/ptools/`
are optional. Prompt bytes measure file content, not token usage or billing.

Per-session token report, `session_report.py` (also Python 3 standard library only, read-only, `-h`):

```bash
python3 scripts/ptools/session_report.py --run-dir D [--projects DIR]                       # every member session of a run
python3 scripts/ptools/session_report.py --run-dir D --fetch-opencode [--oc scripts/oc.sh]  # + OpenCode messages via oc.sh
python3 scripts/ptools/session_report.py --session FILE.jsonl [FILE.jsonl ...]              # any Claude Code transcript(s)
```

- **Options and defaults.** `--projects` (default `~/.claude/projects`, where the Claude transcripts live), `--oc` (default: the
  `oc.sh` next to `scripts/`, i.e. `<script dir>/../oc.sh`; it only selects the adapter), `--chars-per-token` (default 3.2; finite and
  greater than 0), `--top` (default 15; rows per table, at least 1), `--fetch-opencode` (off by default).
- **Local by default.** Without `--fetch-opencode` no subprocess is started at all, not even `oc.sh status`
  (`--oc` only selects the adapter). OpenCode generations then show the totals recorded in `state.json`, and their
  per-call breakdown is `unknown (not fetched; use --fetch-opencode)`.
- **`--fetch-opencode`** (needs `--run-dir`) runs `oc.sh status` first, then pages
  `GET /api/session/<id>/message`: the first request is `order=asc&limit=200` (PAGE_LIMIT), every later one carries only the
  URL-encoded cursor, at most 1000 pages per session (MAX_PAGES); every `oc.sh` call, with its `curl`/`jq` children, is killed
  after 120 s (OC_TIMEOUT_SECONDS). These are module constants, not options. The collection uses `oc.sh`, so it needs bash, curl and jq.
  Nothing that cannot be written as UTF-8 is ever requested, replaced or encoded with a substitute: a session id with a lone surrogate (a JSON `\udXXX` escape in
  `state.json`) is not requested, its generation is `unavailable (session id is not valid UTF-8 (a lone surrogate); not requested)` with its recorded totals kept, and the other generations are
  still fetched and reported; a next cursor with a lone surrogate stops that session's pagination `PARTIAL` (`page N: next cursor is not valid UTF-8 (a lone surrogate); not requested`) with every message
  and usage value of the pages already read kept.
- **Pinned connection, no auto-start.** A preset non-empty `OPENCODE_URL` (with `OPENCODE_PASSWORD`/`OPENCODE_USERNAME`) is passed
  through unchanged and the state file is not read. Otherwise `${XDG_STATE_HOME:-~/.local/state}/opencode/service.json` supplies
  `url` (a non-empty string) and `password` (a string, `null` or absent); an **empty, `null` or absent password removes any inherited
  `OPENCODE_PASSWORD`** from the child environment. A missing, unreadable or non-JSON file, or one without a url, leaves the OpenCode
  sessions `unavailable (service state not found; not started)`; wrong field types give `unavailable (service state invalid; not started)`,
  and so does a url or password that holds a lone surrogate (`service state invalid (url or password is not valid UTF-8: a lone surrogate); not started`).
  In all these cases nothing is spawned. Credentials are never printed.
- **Exit codes.** 0 whenever a report is printed, also a `PARTIAL` one; 2 for a usage error, a missing or invalid run dir or
  `state.json` (also a member id, a member session id or a retired-entry session id that is not a string or `null`, or a `retired`
  that is not a list or `null`), an unreadable `--session` file, or no usable session.
- **Coverage first.** The report opens with one line per session: source and status (`complete`, `PARTIAL`, `unavailable`,
  `ambiguous`, `conflict`, `unknown (not fetched)`), plus each generation's kind, model, effort and where that identity came from.
  Identity comes from `state.json` and its log and stays `ambiguous` when the evidence is missing or contradictory. The one
  exception: an ambiguous generation whose session id has exactly one `<projects>/*/<sid>.jsonl` file is read as a Claude session
  (source `local transcript`; `PARTIAL` when a line of it cannot be parsed), and its model is shown only when every `message.model` in that transcript is equal, otherwise `unknown`.
  The lookup is literal: only the project directory level is a wildcard, so `*`, `?` and `[` in the session id or in `--projects` are plain characters (an id `*` adopts no other transcript, `odd[1]` is
  the file `odd[1].jsonl` and never `odd1.jsonl`, a `--projects` directory named `p[1]` is that directory), and a session id that contains a `/` names no transcript of that shape.
  A transcript that cannot be read (permissions, a directory with that name) leaves that generation `unavailable (transcript unreadable: ...)`
  with its recorded totals, and the run still reports. A missing value is `unknown`, never 0; a total with unknown components reads
  `<sum> (known components: k; unknown components: u; conflicting components: c)` (the conflicting part is omitted when c is 0, and a
  conflicting component is not counted as unknown). Consumed-token totals (segments, task and phase table) are the known counters plus
  that account (OpenCode adds its reported reasoning once; OpenCode calls that precede the first user message form their own segment,
  attributed `unknown`). A session id recorded by several generations, or by a member and the map pre-pass, is counted once: identical
  records count once, differing known records are an excluded `conflict`, and a component (tokens or cost) that is unknown for the member
  is filled from the mapper record, labelled `from mapper record` (a mapper field that is unknown stays an unknown component).
- **A state entry that is not an object** (an element of `members`, or of a member's `retired`) is never silently dropped: Coverage lists
  `state.json: members[i] is not an object (ignored; its recorded tokens and cost are unknown and are in no total)` (the first 50, then a count), the `PARTIAL:` line counts
  `N state entries unusable`, and each such entry is two unknown components (tokens and cost) of the run totals.
- **Safe, UTF-8 output.** The report is written as UTF-8 whatever the locale or `PYTHONIOENCODING`; a lone surrogate from a JSON `\udXXX` escape is written as that visible escape,
  it never stops the report. Text that comes from a transcript, `state.json` or an adapter (member and session ids, model and effort, tool names and targets, message ids, unlisted
  line or message types, the first line of an adapter's error) is shown escaped: C0 controls, DEL, C1, U+2028/2029 and lone surrogates become `\xNN` or `\uNNNN`, a backslash is doubled and a pipe
  becomes `\|`, so it can neither forge a report line nor end a table cell (the transcript and the state file keep the exact text). A tool name that is not a string is shown as its text.
  Escaping happens only where text is printed: ids and names from `state.json` are never altered, so a transcript `<projects>/*/<sid>.jsonl`, an OpenCode request (the id URL-encoded, `ses|x` is
  requested as `ses%7Cx`), a member's prompt files, the handover and replacement log lines and the mapper records are all looked up and compared with the exact id. A first prompt that holds a lone
  surrogate has no bytes to compare: its segment is attributed `unknown` (`first prompt is not valid UTF-8 (a lone surrogate); not compared with any prompt file`) and the usage accounting is unaffected.
- **Costs and limits.** Only recorded costs are shown: once per generation (a recorded 0 is labelled, because the orchestrator also
  writes 0 when no cost was reported), per mapper session, and per OpenCode message; a cumulative cost is never repeated per call or
  added to per-message costs. The split of the input side over items is a `heuristic estimate (character-based)`. Task, round and phase
  are attributed only when a segment's first prompt equals, byte for byte, one of the member's own files in `prompts/`; otherwise `unknown`;
  a member with no id (absent or `null`) is attributed `unknown` (provenance `no member id in state.json`), since its own prompt files
  cannot be identified. A context drop or a large cache write is reported without a cause; call numbers start at 0 (the first call of the session).

Run a build or test command with the full log on disk and only a short, capped summary on stdout, `run_check.py` (Python 3 standard
library only, POSIX only, `-h`). Unlike the tools above it **executes** the command and **writes** files, so it is not read-only:

```bash
python3 scripts/ptools/run_check.py --log-dir D [--fallback-dir F] [--reports GLOB]... [--max-bytes N] [--timeout SECONDS] -- COMMAND [ARG...]
python3 scripts/ptools/run_check.py --log-dir /tmp/checks --reports '**/build/test-results/**/*.xml' -- ./gradlew test
python3 scripts/ptools/run_check.py --log-dir /tmp/checks -- sh -c 'pytest --junitxml="$RUN_CHECK_REPORT_DIR/r.xml"'
```

- **Options and defaults.** `--log-dir` is required: no default and no environment fallback (a relative path is taken against the caller's
  cwd, the directory is created when missing). `--fallback-dir` is optional (created and probed at startup): continuation files go there,
  and so do the worker's final files if the log directory stops accepting them. `--reports` is a repeatable recursive glob (`**`), relative to the
  caller's cwd. `--max-bytes` defaults to 4000; below 512 is a usage error. `--timeout` is optional, a finite number above 0. The literal `--`
  before the command is mandatory, the command must not be empty, and option abbreviations are rejected. The command runs as argv (no
  shell) with the caller's cwd and environment plus `RUN_CHECK_REPORT_DIR`, stdin `/dev/null`, in its own process group. A usage error exits 2
  with the message on stderr only: stdout is empty, nothing is created and the command does not run. Use a `--log-dir` outside the project
  (or in an ignored directory): `.gitignore` ignores `*.log` but neither the failures index nor the report directory, and
  `scripts/council.sh:1655-1657` puts `git status --short` into the ratification diff.
- **The whole stdout is capped.** The final newline included, stdout is at most `--max-bytes` bytes of UTF-8 whatever the locale or
  `PYTHONIOENCODING`, and it never splits a code point. The mandatory lines are budgeted **before launch** in their worst case (every
  optional line present, 20-digit counters, the real absolute paths, 80-byte reasons, the pending line with its `--fallback-dir` disclosure, the final and emergency variants): when
  `--max-bytes` is smaller the run is refused with exit 2 and `... below the minimum of N bytes ...` on stderr, and nothing is created. That
  minimum grows with the length of the `--log-dir` path (and with `--fallback-dir`, every `--reports` glob and the command name), so a very long
  log directory needs a larger `--max-bytes`; the number is printed on refusal and no maximum path length is promised. As a rule of thumb
  it is about 3.2 KB plus nine times the absolute `--log-dir` path length (up to nine path-bearing lines can coexist), so the default 4000 suits
  ordinary paths. Text of the command or of a report is shown escaped: invalid UTF-8 bytes, C0 controls except TAB, DEL, C1, U+2028/2029 and lone surrogates
  become `\xNN` or `\uNNNN` and every literal backslash is doubled, so a literal backslash-x1b and a real ESC byte never look alike (the log and the index keep the
  exact text; the numbered body lines of an excerpt too, and their cut at 240 bytes counts the bytes of the log). If stdout cannot take the summary (closed, a broken pipe, a full
  device) the exit code stays the command's own and nothing fails a second time at shutdown; stderr is then the caller's only result: one line says that the summary was not delivered,
  followed by the result lines of the summary itself (line 1 with the status and exit code, and the `log:`, `capture:`, `CAPTURE_PENDING:`, `failures index:` and `SUMMARY ERROR:` lines it has),
  which say exactly what was and was not produced (for example `log: none (not created)` and `capture: none (command did not run)` after a refusal, `capture: INCOMPLETE ...`, the worker and
  its final files); no completeness is claimed that was not checked.
- **What it prints**, in this order, each line only when it applies: (1) `<STATUS> · exit code <N>[ · CAPTURE_PENDING][ · CAPTURE_INCOMPLETE]
  · <D>s · <L> log lines · <B> log bytes`; (2) `log: <absolute path>` (or `none (not created)`); (3) `capture: VERIFIED|INCOMPLETE|PENDING|none ...`;
  (4) one `log continued:` line per continuation file; (5) `storage: STALLED_ON_STORAGE ...`; (6) the `CAPTURE_PENDING:` line;
  (7) `failures index: <path>` (or `INCOMPLETE (<reason>)`); (8) the `reports not examined: N+ (...)` lines and `dedup capped: ...`;
  (9) `WARNING:` lines; (10) `reports: ...`; (11) `failing tests: T identified (junit J, text-heuristic H)`, `none identified` or
  `failing tests not identified`; (12) `#n <classname>.<name> (<file>) [<source>]` for as many complete identities as fit (a prefix, in order:
  the byte budget limits it, not the 200 excerpts), then `[... M more failing tests not listed; full list in the failures index]`;
  (13) `detail #n:` blocks for the first 200 failing tests only (message up to 300 characters and up to
  12 non-blank body lines at 240 bytes, each block whole or not at all; the body lines of a text block start with their log line number); (14) when the run failed and nothing was identified: `error lines
  (n of M):` (the first 20 lines matching `error|fail|failed|exception|panic|fatal|e: `) and `last lines (n of L):` (the last 40), each row cut at 240 bytes
  with `[...+N bytes]`, or `unwritten tail (...)` instead of the last lines when bytes were not written; (15) **always last** `omitted:`.
  Optional pieces are added greedily in that priority while the exact byte count still fits (memory holds only what could still fit: the
  identity rows and the warnings are kept while they fit in `--max-bytes`, the rest is counted), and `omitted:` counts what is missing:
  `K of T failing tests not listed; D of T failure details not shown; W warning lines not shown; H more error lines not shown; X of L log lines
  not shown` (also on success, where no log line is shown); `C lines or messages cut; P lines only partially examined`, or `omitted: nothing`.
  If producing the summary itself fails after the command ran, the exit code is kept and only line 1, `log:`, `SUMMARY ERROR: <type>:
  <reason>` and `omitted: summary not produced; N failing tests not listed; N failure details not shown; N warning lines not shown; N log
  lines not shown; read the log and the failures index` are printed; a total that was not established before the failure reads `unknown`.
  The analysis never takes a shorter log for a complete one: when a log segment holds fewer bytes than the capture recorded for it (the file changed after the read-back
  said `VERIFIED`) or cannot be read, the capture is marked `CAPTURE_INCOMPLETE`, a `coverage` record `analysis_read_failed` (`reason`, `segment`, `recorded_bytes`,
  `read_bytes`) is written after the `capture` record that said `readback` OK, no `end` record follows, the output is the summary-error form above (`SUMMARY ERROR: ShortRead: ...`;
  no `capture: VERIFIED` line), the exit code is the command's own, and the `complete.json` of a worker says `complete` false with the reason in `incomplete`.
- **Exit codes and status words.** `COMMAND_EXIT <rc>`: the command's own code, kept even when reports, the index, the verification or the
  summary fail (a command that exits 127 itself is `COMMAND_EXIT 127`); `SIGNAL <n> (<NAME>)`: killed by signal n, exit 128+n;
  `SIGNAL <n> (<NAME>) wrapper interrupted, command process group terminated`: the wrapper got SIGINT, SIGTERM or SIGHUP, exit 130, 143 or 129;
  `TIMEOUT (TIMED OUT after <T>s, command process group terminated)`: exit 124; `WRAPPER_ERROR (...)`: 125 the log directory, the fallback directory or one of the
  stem's files cannot be created, 126 the command is not executable, 127 not found (the command did not run, `capture: none (command did not run)`; the empty log
  stays); 2 usage error. Timeout and wrapper signals send SIGTERM to the command's process group, wait 5 s, then SIGKILL; processes still
  in the group after a normal exit are stopped the same way and reported as a `WARNING`.
- **Files**, all created exclusively next to the log (`stem` = `<YYYYmmdd-HHMMSS>-<microseconds>-<wrapper pid>-<command name>`, `-1` ... `-99` on a
  collision): `<stem>.log` (the raw bytes, no header), `<stem>.log.2` ... `.log.4` (continuations), `<stem>.failures.jsonl` (the index),
  `<stem>.reports/` (the private report directory, removed when empty; left to the background worker while a capture is pending), and after a hand-off `<stem>.final-failures.jsonl`,
  `<stem>.final-summary.txt` and, written last, `<stem>.complete.json`.
- **The log is lossless or says otherwise.** The log is the OS-merged stdout+stderr byte stream of the command (its stdout is a FIFO, not a tty):
  no decoding, nothing added. Lines end **only** at LF bytes: line N is what `sed -n 'Np' LOG` prints and a range is `sed -n 'A,Bp' LOG`; the
  total equals `wc -l`, or `wc -l` + 1 when the last line has no LF. Every write goes through one seam that continues partial writes and
  retries EINTR, and the exact accounting (written, unwritten, two SHA-256 states) is kept. A per-file size limit (`EFBIG`) opens the next
  continuation file (in `--fallback-dir` when given; at most 4 files in all; read them in order, `log continued:` says where each starts and, from the
  paths, else from the open descriptors, whether it is on the log's filesystem: `yes`, `no` or `unknown`; a continuation that was opened is used even when
  that cannot be established). Any
  other write error (ENOSPC, EDQUOT, EIO) blocks the command on the full pipe instead of dropping output and retries with a backoff (`storage:
  STALLED_ON_STORAGE`); after 10 s the log continues in `--fallback-dir` if one was given. A write that makes progress (even a partial one) ends the
  continuous stall and the next failure starts a new one with a fresh backoff; the statistics are cumulative. A stall is bounded by the absolute deadline `launch +
  --timeout` when there is one (progress never extends it), otherwise by 120 s without progress; then the capture is `INCOMPLETE`: the log stays a contiguous prefix, the pipe is still drained
  and counted, the last 64 KiB of unwritten bytes go to the index and their last lines to the summary, and the analysis says it covers only the
  written prefix. At the end every file is fsynced and read back; only then does `capture: VERIFIED <B> bytes, sha256 <hex> (fsync + read-back)` appear.
- **Detached writers: `CAPTURE_PENDING`.** If a process that left the command's group (for example after `setsid`) still holds the output pipe 2 s after the
  group ended, the wrapper summarises exactly the written prefix (line 1 and the `capture:` line say so, counts are "so far", `failing tests:
  T identified so far`) and forks a background worker (`background worker pid P`). The worker detaches, keeps the same capture engine, and at EOF
  writes `<stem>.final-failures.jsonl`, `<stem>.final-summary.txt` (line 1 marked `FINAL`, same cap) and, **last**, `<stem>.complete.json`
  (`complete` true|false, both SHA-256, segments, unwritten bytes, stall counts, the outcome of each final write, and `artifacts` naming
  the paths that were actually written, also when they are in `--fallback-dir`). The `CAPTURE_PENDING:` line names the three final files in the log directory and,
  when `--fallback-dir` is given, the fallback directory where the same file names are used if the log directory stops accepting them. Every persisted path
  is absolute (a relative `--log-dir` or `--fallback-dir` is taken against the cwd). The final summary is the
  authoritative result and the foreground one is provisional; a missing `complete.json` never means success (the worker was killed, or the
  detached process is still running: the worker lives as long as the pipe is held). The worker's storage-stall budget is the same absolute
  deadline `launch + --timeout` (a stall that begins after it is abandoned at once); without `--timeout` it is 120 s per stall.
- **Failing tests from JUnit XML.** The command writes reports into `$RUN_CHECK_REPORT_DIR` (produced by this invocation), or a `--reports` glob names
  reports written anywhere. A glob is expanded by the tool's own walker with the rules of `glob.glob(recursive=True)` (a wildcard never matches a leading dot unless its
  component starts with one, `**` follows symlinks but never re-enters a directory it is inside of) except that every failing
  listing or inspection is reported and not dropped as `glob` does, and that it is lazy: a directory is read entry by entry in the order the file
  system lists it (not sorted; the admitted reports are processed in sorted order), nothing is collected, a source that reached its cap reads no
  further; the result is de-duplicated by real path and **snapshotted before launch** (size,
  mtime, streamed SHA-256); after the run a new or content-changed file is `changed during this run; concurrent writers not excluded`, an unchanged
  one is `stale, ignored` (and, when only its mtime changed, also counted `rewritten with identical content, failures NOT listed`), a vanished one is
  `removed` (only when its absence is established: ENOENT or ENOTDIR; any other inspection failure is a `PROVENANCE_ERROR`), a glob that matches nothing gives a
  `WARNING`, and the private directory (symlinks are not followed) wins over a glob. Reports are read at
  any exit code; exit 0 with failing tests gives `WARNING: command exited 0 but reports list N failing tests`. The `reports:` line reads `<C> changed
  during this run (concurrent writers not excluded), <P> produced by this invocation, stale <S>, removed <R>, errors <E>` (printed whenever
  `--reports` is used, a private report exists or an error was counted). Every discovery failure is written to the failures index when it happens
  (a `coverage` record `discovery_error` with its source, path, pass and errno) and counted; the summary keeps only the `WARNING` lines that can still
  fit in `--max-bytes` and `omitted:` counts the others, while the sources that had a failure are remembered apart from that, so limiting what is shown
  never turns an uncertain path into a current report. A distinct failure is counted once (a digest of source, path and errno is remembered for at
  most `MAX_REPORT_PATHS` of them per pass; beyond that a `WARNING` and a `discovery_errors_dedup_capped` record say that a repeated failure is counted
  again). An entry of the private report directory whose type cannot be inspected is a `REPORT_ERROR` naming that entry, and the entries after it are
  still read; a symlink status that cannot be read is a discovery failure too, and the entry is then walked as if it were a symlink. The final index of
  a background worker gets the pre-launch discovery records copied from the foreground index: only the byte range those records occupy is read, one
  record at a time (a record longer than 64 KiB is not read), and each one must be a complete line-feed-terminated JSON object. A `records_not_copied`
  coverage record (`from`, `range`, `copied_records`, `reason`) follows the records that were copied when the file cannot be opened, ends before the range
  does, holds an unterminated, malformed or non-object record, or a record that crosses the end of the range. The diagnostics that the foreground index never
  wrote (a write error such as `EFBIG` stopped it; only their count and the first error are kept) are announced by a `records_not_copied` record too, with
  `expected_records`, `persisted_records`, `missing_records` and the storage error, so a healthy final index cannot hide them; the `errors` count always
  includes them. A testcase with a
  `failure` or `error` child is ONE failing test, identified by `(classname, name, file)`; duplicates across reports count once (identities are kept per source, so a
  text hit never becomes the duplicate of a JUnit failure); skipped, rerun and passing cases are only counted. Reports are parsed with expat in 64 KiB chunks behind a byte pre-scan, so a failure body is streamed into the
  index in ordered `body` records (at most 16384 characters each) and never held in memory; only an excerpt is (first 300 message characters, 12
  body lines, the first 200 failing tests). Report problems are never silent: `WARNING: PROVENANCE_ERROR: <path>: <ERRNO>` when a report could
  not be snapshotted (the error is kept even if the command deletes the file or the post-run cap does not reach it), hashed or listed, or a directory listing
  or entry inspection failed before the launch (the failing path is named, the whole glob counts as incomplete; after the run the same failure is a `REPORT_ERROR`) (or `pre-launch discovery incomplete; current-run provenance unknown` when a path absent from a capped
  or failed snapshot cannot be proven new), `WARNING: REPORT_ERROR: <path>: <ERRNO NAME>` when it cannot be read and `...: malformed XML ...`, `not
  a JUnit report (root element x)` or `markup token over 8388608 bytes at byte offset O` when it cannot be parsed (`(partial: n failures kept)` when
  failures before the error were kept). A `testsuite` that declares more `failures` or `errors` than its own testcases list (a suite without nested suites; root and aggregate
  attributes are ignored) gives `WARNING: REPORT_MISMATCH: <path>: testsuite <name> declares failures=F errors=E, lists failures=f errors=e` and a `report_declared_mismatch` coverage record. A failed report is never counted as changed, stale or produced. The byte pre-scan reads UTF-8, ASCII and ISO-8859-x/windows-125x; UTF-16/UTF-32
  and other declared encodings, also names Python has no codec for, are a `REPORT_ERROR` (`encoding not supported by the markup pre-scan`); a codec error raised
  while parsing is `codec error: <type>: <reason>`, and one bad report never stops the others or the log analysis. The module constants `MAX_REPORT_PATHS`
  (10000 retained real paths per discovery pass, shared by the private directory and every glob), `MAX_UNEXAMINED_COUNT` (1000000),
  `MAX_MARKUP_TOKEN_BYTES` (8388608 bytes for one tag, comment, processing instruction or declaration; character data and CDATA are not limited)
  and `DEDUP_CAP` (200000 identity digests) are not options. Beyond the path cap the candidates are only counted: `reports not examined: N+
  (glob G)` or `(report dir D)`, where N is a **lower bound of candidate occurrences** (not unique reports; the counts of the passes are summed,
  the separate pass counts are in the index) and the discovery of that source stops at `MAX_UNEXAMINED_COUNT`; a discovery cap never turns an
  unexamined path into a claim that it was removed. After the dedup cap, later failures are still emitted and `dedup capped: later duplicates may
  be listed twice` says so.
- **Failing tests from the text, labelled `text-heuristic`.** Only whole lines match (at most 64 KiB), after one trailing CR and every SGR
  colour sequence (`ESC [`, at most 256 digits, `;` or `:`, then `m`; a longer run is no colour sequence and stays) have been removed **from a copy of the line**: the log, the index bodies and the summary
  excerpts keep the original bytes. OSC/hyperlink sequences, cursor-movement and erase sequences and progress that is only overwritten with CR are
  not interpreted: such lines stay unrecognised and are declared as before (`failing tests not identified`, the error lines, the runner-count
  `WARNING`). Recognised: `^(FAIL|ERROR): (\S+) \(([^()\s]+)\)( [(\[].*[)\]])?$` (unittest: `mod.Class.test` gives classname `mod.Class`, name `test`; the optional
  suffix is what unittest prints for a `subTest` and is kept verbatim in the `subtest` field and shown after the identity: ` (i=0)`, ` [why]`,
  ` [why] (s='a (b) c')`, ` (<subtest>)`) or `^FAILED (\S+::\S.*?)( - .*)?$` (pytest: `a/b/test_x.py::C::t[p]` gives file `a/b/test_x.py`, classname
  `a.b.test_x.C`, name `t[p]`). Every subTest failure of the log is an occurrence of its own (its `occurrence` among those with the same identity and suffix), so an
  identical suffix twice is two failing tests and agrees with `FAILED (failures=2)`; an ordinary failure line twice is still one failing test and a `duplicate`
  record. The table of occurrence numbers holds at most `DEDUP_CAP` distinct identities with their suffix: a later distinct one has `occurrence` null (it is still counted, indexed and never
  merged with another), and one `occurrences_capped` coverage record and a `WARNING` say so. Not recognised: `FAILED (failures=1, ...)`, `FAILED build`, `FAIL <message>` lines of scripts, indented or prefixed lines and pytest `ERROR <nodeid>` and collection
  errors (use JUnit for those; the runner-count `WARNING` counts pytest `N failed` and `N error(s)`). A text hit is **merged** into a JUnit
  failure only when the classname AND the name are exactly equal and, when both carry a file, the file is equal, and only when exactly one
  JUnit failure matches (`[junit, also in log]`, every observation in the index, with its `subtest`, `occurrence` and block); otherwise it stays a separate failing test (so a
  report with only `a.C2::t` and the log line `FAILED a.py::C1::t` give two failing tests), and a hit that matches several JUnit failures keeps all of them and adds a
  `WARNING`. `WARNING: runner reported R failing tests, identified I` appears only when I < R (unittest `FAILED (failures=.., errors=..)`,
  pytest `N failed, M error`; each subTest failure counts as one identified); when JUnit failures that the log does not corroborate hide a shortfall, `WARNING: runner
  reported R failing tests, the log identifies L; J JUnit failing tests are not corroborated by the log` says so. Text hits are found at any exit code (a `WARNING` at exit 0).
  An identity is compared as an unambiguous JSON encoding of its parts (a NUL or any other character in a name can never make two identities equal).
- **Text blocks: the message and the complete body of a text failure.** The index holds, for every recognised failure of the log, the block of log
  lines that describes it, streamed in `body` records of at most 16384 characters (invalid UTF-8 is kept reversibly as lone surrogates, CR and SGR bytes are verbatim; a
  line beyond 64 KiB is streamed whole and can never act as a header or a separator). A **unittest block** starts at the FAIL/ERROR header line and
  ends before the first of: a line of three or more `=`; a line of three or more `-` that is followed by `Ran N test(s) in `; another recognised header; the end of
  the log (`ended_by` is `separator`, `summary`, `next_header`, `eof`, or `prefix_end` when the analysed stream is only a prefix: a `CAPTURE_PENDING` foreground summary or an
  incomplete capture; the final analysis of a worker re-reads the whole log). The dash line directly after the header is its underline and belongs to the block; any
  other dash-only line stays in the body. The message is the last exception report: from the first column-0 line after the frames of the last `Traceback (most recent call last):`
  to the last non-blank line of the block (`message_from` `traceback`); without a traceback it is the block text after the underline (`block_text`); a block without an underline, a
  header-only block or an unreadable region has no message (`none`) and only the complete body. A **pytest block** is the text between a `___ name ___` line and the next such line or
  the next `=== ... ===` banner, inside a `=== FAILURES ===` section (blocks of an ERRORS section are not captured); its message is the pytest `E` lines (`E` and up to three spaces
  removed, the source indentation kept, joined with LF, `message_from` `e_lines`), else the short-summary message (`short_summary`). The message is streamed too: the `failure`
  record's `message` is its first 16384 characters, `message_chars` is its exact length, and `message_more` continuation `message` records (`block`, `seq` from 1) hold the rest, so
  `message` plus the continuation records in `seq` order is the complete message; an `observation` or `duplicate` record of a text failure carries the same `message`, `message_from`,
  `message_chars` and `message_more` (the text of its own block, never the JUnit message), so its complete message is rebuilt the same way from its `block`. A pytest block is matched to a FAILED line **only after every FAILED line and block of the log has been
  seen**, by name (what follows the first `::` of the node id with `::` turned into `.`, parameters from the first `[` kept exactly) and only when the name is unique in both directions;
  the matched failure gets `block`, `log_lines` = the block and `summary_line` = the FAILED line. Two blocks or two FAILED lines with one name attach nothing: each block is recorded
  `unmatched` (`coverage` `text_block_unmatched`, reason `ambiguous_name` or `no_failed_line`, with its complete body), each ambiguous FAILED line has a `text_block_ambiguous` record, and counted
  `WARNING` lines say how many. The pytest blocks and FAILED lines that wait for each other are kept as a fixed number of integers each (no text) up to `DEDUP_CAP`; beyond it one
  `text_block_association_capped` record and a `WARNING` say that later ones are not matched: a block is then recorded `unmatched` (reason `block_cap`, body kept) and a FAILED line is registered
  at once without a block. A name that occurs beyond the cap is remembered in a filter of fixed size (64 KiB): a retained block or FAILED line whose name may also occur beyond the cap is never attached
  (`text_block_ambiguous` with `beyond_cap` true, the block `unmatched` with reason `ambiguous_name`); a false positive of the filter costs an association, it can never attach a wrong one. Every block ends in one `text_block` record (`block`, `runner`, `header`, `log_lines`, byte offsets `bytes` in the stream, `ended_by`, `body_chunks`, `body_chars`,
  `outcome` failure|observation|duplicate|unmatched, `failure`, `failed_line`, `read_error`); the body of a block is its `body` records with that `block` in `seq` order and equals the log
  bytes it names. The summary detail of a text failure shows the message (300 characters) and the first 12 non-blank body lines, each with its log line number; a shown log line is counted as shown
  in `omitted:` (once, also when it is an error line). If a block cannot be read back (an I/O error) the failure is still listed, a `text_block_unread` record and a `WARNING` say so.
- **The failures index** `<stem>.failures.jsonl` is JSON Lines (ASCII), exclusive create, with `header`, `capture`, `unwritten_tail`, `report`, `failure`
  (complete message), `node`, `body`, `message`, `text_block`, `duplicate`, `observation`, `coverage` and `end` records (a JUnit `failure` and `node` record also carry the
  `error_type` attribute of the node; a `body` record of a JUnit failure has `id` and `node`, one of a text failure has `block`); a missing `end` record proves an incomplete
  file. The file always ends on a complete record: a write error (a per-file size limit, a full disk) marks it `failures index: INCOMPLETE
  (<ERRNO NAME>; N records written, M not written)` without touching the log or the exit code, a record that could only be written in part is cut
  off again, and `; last record torn` says so if even that failed (a SIGKILLed wrapper can still leave a torn last line, and the missing `end`
  record then proves the file incomplete). The worker never leaves a torn `final-summary.txt` or `complete.json`: a file it could not write
  completely is removed (the next directory is tried) and the missing artifact, with the error in `complete.json` when that could be written,
  is the answer.
- **Limits.** Python 3 standard library and POSIX only. The command's stdout is a FIFO. Text recognition is anchored whole-line matching: a header that spans lines
  (a subTest description with a line break), OSC/cursor/CR-only sequences and pytest `ERROR` lines are not identified (declared, never silent); a unittest block without a
  delimiter runs to the end of the log, so a failure followed by unrelated output shows that output as part of its block and message (`ended_by: eof`); two pytest blocks or FAILED lines with one
  name (the same test name in two modules) are never guessed apart. A JUnit `system-out`/`system-err` is not indexed (it stays in the report file). A `setsid` descendant escapes the process-group
  cleanup; the worker lives as long as it holds the pipe. Bytes buffered in a killed process may never be written; a SIGKILLed wrapper leaves a partial log. Under a finite hard
  `RLIMIT_FSIZE` the single-file failures index can itself be `INCOMPLETE` (the log rolls over into continuation files and stays verified).
  A fallback directory on the same volume cannot help when the whole volume is full. Nothing is ever written outside `--log-dir` and
  `--fallback-dir`; no `council.sh` or prompt change is involved.

## `oc.sh` on its own

```bash
scripts/oc.sh ensure                                   # start/check the OpenCode service
scripts/oc.sh models [filter]                          # enabled models (provider/id)
scripts/oc.sh run "task" --dir /abs/project --model openai/gpt-6-astra --variant high
scripts/oc.sh prompt ses_… "follow-up" ; scripts/oc.sh diff ses_… --patch ; scripts/oc.sh messages ses_…
```

## License

[MIT](LICENSE)
