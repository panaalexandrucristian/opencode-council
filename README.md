# opencode-council

A [Claude Code](https://claude.com/claude-code) skill that drives [OpenCode](https://opencode.ai) through its HTTP API — and runs a **cross-CLI council**: OpenCode sessions and Claude Code CLI sessions, each on a model and effort you choose, that discuss a list of tasks, ask you instead of assuming, and must reach a unanimous consensus.

## What is inside

| file | purpose |
|---|---|
| `SKILL.md` | the skill Claude Code loads (`/opencode …`): workflow, permissions, the council checklist |
| `scripts/oc.sh` | thin bash + curl + jq CLI over the OpenCode v2 background service (sessions, prompts, wait, diff, models, `--variant` = effort) |
| `scripts/council.sh` | the council orchestrator (see below) |
| `scripts/ptools/` | optional, offline Python 3 stdlib prompt-byte report and same-prompt duplicate audit |
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
- **Tests.** `bash scripts/test-completion.sh` — 1280 offline checks (no network, no model calls), including
  `scripts/ptools/test_ptools.py` (192 Python standard-library unittest cases for the analysis tools).
  Run `/bin/bash -n` separately on each changed shell script after any change.

Exit codes: `0` all tasks reached consensus · `1` config error · `2` a member failed twice (checkpointed, `resume`) · `4` questions pending · `5` some task unresolved.

## Known gaps / future tests

Tracked limitations of the map pre-pass and the tests not yet written:

- **Links changed during the mapper run.** The boundary comes from a scan just before the session and a rescan after it. A symlink created or retargeted by another process while the mapper runs is not blocked when it is accessed. The rescan discards the map when the final set differs, but it cannot undo a read, and it cannot see a link that was created and removed again between the two scans. The mapper itself cannot create links (edit and shell are denied).
- **OpenCode's built-in search filters.** OpenCode's own `grep` and `glob` always pass `--glob=!**/.git/**` to ripgrep, `glob` skips dot-paths unless the mapper passes `hidden: true`, and ripgrep honours `.gitignore`. The orchestrator adds no exclusion of its own, and `read` reaches `.git`, but these built-in skips remain. OpenCode is not modified.
- **Directory aliases are blocked as a whole.** An in-project directory symlink whose target contains an escaping link is denied entirely, so its other in-project content is reachable only through the real path.
- **Case-folded denies over-match.** On a filesystem that ignores letter case, OpenCode's case-sensitive matcher would let a case variant of an escaping link (`ESCAPE.PY` for `escape.py`) through, so the per-link denies are case-folded (`?` per ASCII letter, `*` per non-ASCII character). They can also block unrelated in-project names of the same shape, which the mapper then cannot read.
- **Hard links** cannot be told apart from ordinary files by path, so a hard link to an outside file is neither blocked nor detected.
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

## `oc.sh` on its own

```bash
scripts/oc.sh ensure                                   # start/check the OpenCode service
scripts/oc.sh models [filter]                          # enabled models (provider/id)
scripts/oc.sh run "task" --dir /abs/project --model openai/gpt-6-astra --variant high
scripts/oc.sh prompt ses_… "follow-up" ; scripts/oc.sh diff ses_… --patch ; scripts/oc.sh messages ses_…
```

## License

[MIT](LICENSE)
