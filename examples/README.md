# Example: a 2-member council builds `hello.sh` — and asks before it assumes

A real run (output trimmed only for paths). One Claude Code session (Sonnet, read-only) and one
OpenCode session (Kimi K3, the executor) get a deliberately vague build task:

> Creează în directorul de lucru un script shell `hello.sh` care afișează un salut.

Files in this directory:

| file | what |
|---|---|
| [`council.json`](council.json) | the roster + task (set `dir` to an absolute path of a scratch git repo before running) |
| [`answers.json`](answers.json) | the answers given to the council's questions |
| [`transcript.md`](transcript.md) | the full transcript the run produced |

## 1. Check the roster (nothing runs yet)

```
$ scripts/council.sh show --config examples/council.json
COUNCIL ROSTER
  sessions: 2 total = 1 OpenCode + 1 Claude Code
  id   kind      model                              effort   mode   agent/perm   ctx-window
  A    claude    sonnet                             medium   read   plan         (from first reply)
  B    opencode  kimi-code-plan-global/k3           high     edit   build        1048k
  executor (only member allowed to edit files): B
  dir: /path/to/project
  max_rounds/task: 4 · timeout/call: 600s · handover at 50% context · claude max_turns: 30
  tasks:
    hello [build]: Creează în directorul de lucru un script shell `hello.sh` care afișează un salut.
config OK: examples/council.json
```

## 2. Start — the council refuses to guess

Both members find that the task leaves the greeting text, language, shell and permissions open.
Instead of picking "reasonable defaults" they vote `question`; the run pauses with exit code **4**:

```
council: run dir: /path/to/run
council: ==== task hello (1/1) phase plan round 1 ====
council: 11:46:14 hello r1 member A: question · ctx 3% [33886/1000000] · session tokens 98777
council: 11:46:17 hello r1 member B: question · ctx 0% [9693/1048576] · session tokens 27251
council: QUESTIONS FOR THE USER (task hello) — answer them and run: council.sh resume --run-dir /path/to/run --answers answers.json
  [hello/r1/A/q1] member A: Ce text exact trebuie afișat de salut (ex: "Hello, World!", "Salut!", "Bună ziua!" sau alt text specific)?
  [hello/r1/A/q2] member A: În ce limbă trebuie să fie salutul: română sau engleză?
  [hello/r1/A/q3] member A: Ce shebang/shell trebuie folosit: #!/bin/sh (POSIX) sau #!/bin/bash?
  [hello/r1/A/q4] member A: Trebuie ca hello.sh să fie făcut executabil (chmod +x) ca parte a livrării, sau e suficient fișierul cu shebang, rulat cu `bash hello.sh`?
  [hello/r1/B/q5] member B: Ce text exact să afișeze hello.sh (ex. «Salut, lume!» sau «Hello, World!») și în ce limbă?
  [hello/r1/B/q6] member B: Să fac fișierul executabil (chmod +x) sau doar să îl creez?
/path/to/run/questions.json
```

`questions.json` holds the same items as `[{id, member, question}]`.

## 3. Answer and resume

```
$ cat examples/answers.json
{
  "hello/r1/A/q1": "Textul exact: Salut, consiliu!",
  "hello/r1/A/q2": "Română.",
  "hello/r1/A/q3": "#!/bin/sh (POSIX).",
  "hello/r1/A/q4": "Da, executabil (chmod +x).",
  "hello/r1/B/q5": "Textul exact: Salut, consiliu! — în română.",
  "hello/r1/B/q6": "Da, chmod +x."
}
```

```
$ scripts/council.sh resume --run-dir /path/to/run --answers examples/answers.json
council: answers recorded — re-running task hello phase plan round 1 with the answers (round budget not consumed)
council: ==== task hello (1/1) phase plan round 1 ====
council: 11:47:36 hello r1 member A: propose · ctx 3% [37815/1000000] · session tokens 172207
council: 11:47:42 hello r1 member B: propose · ctx 1% [12669/1048576] · session tokens 51028
council: 11:47:49 hello r2 member A: agree · ctx 4% [40532/1000000] · session tokens 212739
council: 11:47:51 hello r2 member B: agree · ctx 1% [14841/1048576] · session tokens 65869
council: task hello: CONSENSUS in round 2 on candidate hello-c1
council: 11:48:44 hello exec1 member B: done · ctx 1% [17350/1048576] · session tokens 132296
council: 11:48:56 hello x1 member A: agree · ctx 4% [43272/1000000] · session tokens 298827
council: 11:49:12 hello x1 member B: agree · ctx 1% [19316/1048576] · session tokens 170415
council: task hello: implementation RATIFIED in ratification round 1
council: done — transcript: /path/to/run/transcript.md
/path/to/run/transcript.md
```

What happened, round by round (see [`transcript.md`](transcript.md)):

- **r1** — with the answers relayed verbatim, both members list the open items as settled by
  `task` / `dir` / `user` and post complete plans.
- **r2** — A's plan is the frozen candidate; A and B both vote `agree` → **consensus**.
- **exec1** — B (the only member in `edit` mode) creates `hello.sh`, runs `chmod +x`, `sh -n`,
  executes it, and reports the real output.
- **x1** — A verifies the file on disk, both vote `agree` → **ratified**. Exit code **0**.

The result on disk:

```
$ cat /path/to/project/hello.sh
#!/bin/sh
echo "Salut, consiliu!"
$ /path/to/project/hello.sh
Salut, consiliu!
```

## 4. Where things are

```
$ scripts/council.sh status --run-dir /path/to/run
run: /path/to/run · status: done · task hello (1/1 done) · phase plan · round 1/4
  member A g1 claude sonnet [medium] session 796b37ed-… · ctx 43272/1000000 (4%) · session tokens 298827 · cost 0.1978 · calls 5 · retired 0
  member B g1 opencode kimi-code-plan-global/k3 [high] session ses_f3cdb69c… · ctx 19316/1048576 (1%) · session tokens 170415 · cost 0 · calls 6 · retired 0
  task hello: ratified (2 rounds)
  transcript: /path/to/run/transcript.md
```

Every member's context use is measured after each call; had one crossed `handover_at` (50%), the
session would have written a handover note and been replaced by a fresh session (`g2`) — the
`retired` column and the transcript log record that.

## From Claude Code

The same thing driven by the skill instead of by hand:

```
/opencode vreau un consiliu: 2 sesiuni — Claude Sonnet (medium, read-only) și OpenCode Kimi K3
(high, executor) — pe /path/to/project, task: creează hello.sh care afișează un salut.
```

Claude writes `council.json`, shows you the roster, waits for your yes, runs `start` in the
background, relays the council's questions to you one by one, writes `answers.json`, resumes, and
reports the outcome, the diff and the token/context summary.
