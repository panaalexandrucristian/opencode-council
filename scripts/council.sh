#!/usr/bin/env bash
# council.sh — a cross-CLI "council": N persistent sessions (OpenCode via oc.sh and/or Claude Code CLI
# via `claude -p --resume`) work through a list of tasks and must reach an explicit, unanimous
# consensus on each one. The orchestrator relays the members' posts to each other every round.
#
#   council.sh show   --config F                      validate the config and print the roster (no sessions created)
#   council.sh start  --config F --run-dir D          create the sessions and run all tasks (D must not exist)
#   council.sh status --run-dir D                     where the run is: task, round, member context/tokens, pending questions
#   council.sh resume --run-dir D [--answers F|--answer TEXT]   continue after exit 4 (questions) or exit 2 (member failure)
#
# Exit codes: 0 every task reached consensus · 1 usage/config error · 2 a member failed twice (checkpointed; resume
#             re-runs the round) · 4 questions for the user are pending (see D/questions.json) · 5 finished, but at
#             least one task is unresolved (max_rounds reached without unanimity)
#
# Config (JSON, every field explicit — no hidden defaults, see SKILL.md "Council"):
# {
#   "dir": "/abs/project", "max_rounds": 4, "timeout_s": 600, "handover_at": 0.5, "max_turns": 30,
#   "executor": "A" | null,
#   "tasks": ["question", {"id":"t2","text":"...","execute":true}],
#   "members": [ {"id":"A","kind":"opencode","model":"openai/gpt-6-astra","effort":"high","mode":"read"},
#                {"id":"B","kind":"claude","model":"sonnet","effort":"high","mode":"read"} ]
# }
# mode read -> opencode agent "plan" / claude --permission-mode plan; mode edit -> "build" / acceptEdits.
# Only the executor may be mode edit. effort -> OpenCode model variant / claude --effort.
#
# Protocol: round 1 every member posts a complete proposal. From round 2 the candidate is the current position of
# a rotating proposer (member (r-2) mod N); everyone sees the others' previous posts and votes agree/disagree
# (with a complete revised proposal) or asks questions. Unanimous "agree" = consensus. Build tasks: consensus on a
# plan -> executor implements -> ratification rounds on the report + diff. Any question pauses the run (exit 4).
# Context: after every call the member's context usage is measured; at >= handover_at of the model's window the
# session writes a handover note and is replaced by a fresh session (same member id, next generation).
set -o pipefail

HERE=$(cd "$(dirname "$0")" && pwd); OC="$HERE/oc.sh"
[ -x "$OC" ] || { echo "council: oc.sh not found next to this script" >&2; exit 1; }
command -v jq >/dev/null || { echo "council: jq is required" >&2; exit 1; }

die() { echo "council: $*" >&2; exit 1; }
log() { echo "council: $*" >&2; }
now() { date +%H:%M:%S; }
CMD=""; CONFIG=""; RUN=""; ANSWERS_FILE=""; ANSWER_TEXT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --config|--run-dir|--answers|--answer) [ $# -ge 2 ] || die "missing value for $1" ;;
  esac
  case "$1" in
    --config)  CONFIG=$2; shift 2 ;;
    --run-dir) RUN=$2; shift 2 ;;
    --answers) ANSWERS_FILE=$2; shift 2 ;;
    --answer)  ANSWER_TEXT=$2; shift 2 ;;
    -h|--help) sed -n '2,32p' "$0"; exit 0 ;;
    --*)       die "unknown option: $1" ;;
    *)         [ -z "$CMD" ] && CMD=$1 || die "unexpected argument: $1"; shift ;;
  esac
done
[ -n "$CMD" ] || { sed -n '2,12p' "$0"; exit 1; }
abs() { case "$1" in /*) printf '%s' "$1" ;; *) printf '%s' "$PWD/${1#./}" ;; esac; }

# Claude Code >= 2.1.277 reports a resumed session's CUMULATIVE spend on every result (total_cost_usd, modelUsage);
# earlier versions report per-call figures. Detect once so session totals are neither double-counted nor under-counted.
claude_cumulative() {
  local v; v=$(claude --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1); [ -n "$v" ] || { echo false; return; }
  printf '%s\n%s\n' "2.1.277" "$v" | sort -t. -k1,1n -k2,2n -k3,3n | head -1 | grep -qx "2.1.277" && echo true || echo false
}

# ------------------------------------------------------------------ state ----
# Everything lives in $RUN: config.json, state.json, prompts/, posts/, raw/, questions.json, transcript.md
ST=""
st()  { jq -r "$1" "$ST"; }                                   # read
stj() { jq -c "$1" "$ST"; }                                   # read json
sts() { jq "$@" "$ST" >"$ST.tmp" && mv "$ST.tmp" "$ST"; }     # update (atomic)
N=0; DIR=""; MAXR=0; TIMEOUT=600; HANDOVER=0.5; MAXT=30; EXEC=""
load_state() {
  ST="$RUN/state.json"; [ -f "$ST" ] || die "no state in $RUN (not a council run dir)"
  N=$(st '.members|length'); DIR=$(st '.config.dir'); MAXR=$(st '.config.max_rounds'); TIMEOUT=$(st '.config.timeout_s')
  HANDOVER=$(st '.config.handover_at'); MAXT=$(st '.config.max_turns // 30'); EXEC=$(st '.config.executor // ""')
}
mid()  { st ".members[$1].id"; }
mget() { st ".members[$1].$2"; }

# ----------------------------------------------------------------- config ----
validate_config() {  # $1 = config file -> prints normalised config json
  [ -f "$1" ] || die "config not found: $1"
  jq -e . "$1" >/dev/null 2>&1 || die "config is not valid JSON: $1"
  local err
  err=$(jq -r '
    (.executor) as $ex |
    (if $ex == null then [ .members[]? | select(.mode=="edit") | "member \(.id) has mode edit but executor is null" ]
     else (if ([.members[]? | select(.id==$ex)]|length)==1 then [] else ["executor must be one of the member ids"] end)
          + [ .members[]? | select(.id==$ex and .mode!="edit") | "executor \(.id) must have mode edit" ]
          + [ .members[]? | select(.id!=$ex and .mode=="edit") | "member \(.id) has mode edit but is not the executor" ] end)
    + [ .tasks[]? | select(type=="object" and .execute==true and $ex==null) | "task \(.id // .text[0:40]) has execute:true but no executor is configured" ]
    | .[]' "$1" 2>/dev/null | sed 's/^/  /'; jq -r '
    def need(f; msg): if (has(f) | not) then "missing field: " + f + " (" + msg + ")" else empty end;
    [ need("dir"; "absolute project directory"), need("tasks"; "array of tasks"), need("members"; "array of {id,kind,model,effort,mode}"),
      need("executor"; "member id allowed to edit files, or null"), need("max_rounds"; "e.g. 4"), need("timeout_s"; "e.g. 600"),
      need("handover_at"; "e.g. 0.5") ]
    + (if (.dir|type)=="string" and (.dir|startswith("/")) then [] else ["dir must be an absolute path"] end)
    + (if (.tasks|type)=="array" and (.tasks|length)>0 then [] else ["tasks must be a non-empty array"] end)
    + (if (.members|type)=="array" and (.members|length)>=2 then [] else ["members must be an array of at least 2"] end)
    + [ .members[]? | select((.id|type)!="string" or (.id|length)==0) | "every member needs a string id" ]
    + [ .members[]? | select(.kind!="opencode" and .kind!="claude") | "member \(.id): kind must be opencode|claude" ]
    + [ .members[]? | select((.model|type)!="string") | "member \(.id): model is required" ]
    + [ .members[]? | select((.effort|type)!="string") | "member \(.id): effort is required" ]
    + [ .members[]? | select(.mode!="read" and .mode!="edit") | "member \(.id): mode must be read|edit" ]
    + (if ([.members[]?.id]|unique|length) == ([.members[]?.id]|length) then [] else ["member ids must be unique"] end)
    + (if (.max_rounds|type)=="number" and .max_rounds>=2 and .max_rounds<=10 then [] else ["max_rounds must be 2..10 (round 1 = proposals, consensus needs at least one voting round)"] end)
    + (if (.timeout_s|type)=="number" and .timeout_s>=30 then [] else ["timeout_s must be >= 30"] end)
    + (if (.handover_at|type)=="number" and .handover_at>0 and .handover_at<=1 then [] else ["handover_at must be in (0,1]"] end)
    + [ .tasks[]? | select((type=="string" and length>0) or (type=="object" and (.text|type)=="string") | not) | "each task must be a string or {id,text,execute}" ]
    | .[]' "$1" | sed 's/^/  /')
  [ -z "$err" ] || { echo "council: invalid config $1:" >&2; echo "$err" >&2; exit 1; }
  local d; d=$(jq -r .dir "$1"); [ -d "$d" ] || die "dir does not exist: $d"
  # claude effort values; opencode efforts are checked against the model's variants below
  err=$(jq -r '.members[] | select(.kind=="claude") | select(.effort|IN("low","medium","high","xhigh","max")|not) | "  member \(.id): claude effort must be low|medium|high|xhigh|max"' "$1")
  [ -z "$err" ] || { echo "council: invalid config:" >&2; echo "$err" >&2; exit 1; }
  # normalise: tasks -> {id,text,execute}; members -> +agent/permission_mode
  jq '.tasks |= [to_entries[] | (if (.value|type)=="string" then {id:("t"+((.key+1)|tostring)), text:.value, execute:false}
                                  else {id:(.value.id // ("t"+((.key+1)|tostring))), text:.value.text, execute:(.value.execute==true)} end)]
      | .max_turns = (.max_turns // 30)
      | .members |= [ .[] | . + (if .kind=="opencode" then {agent:(if .mode=="edit" then "build" else "plan" end)}
                                 else {permission_mode:(if .mode=="edit" then "acceptEdits" else "plan" end)} end) ]' "$1"
}

check_opencode_models() {  # $1 = normalised config json -> validates model + variant against the live server; prints ctx limits json
  local models; models=$("$OC" api GET "/api/model?location%5Bdirectory%5D=$(jq -rn --arg d "$(jq -r .dir <<<"$1")" '$d|@uri')") || die "cannot list OpenCode models"
  jq -r '.members[] | select(.kind=="opencode") | "\(.id)\t\(.model)\t\(.effort)"' <<<"$1" | while IFS=$'\t' read -r id model effort; do
    local m; m=$(jq -c --arg m "$model" '.data[] | select(.enabled and (.providerID+"/"+.id)==$m)' <<<"$models")
    if [ -z "$m" ]; then  # a directory the server has not seen yet can answer before its config is loaded: retry once
      sleep 2; models=$("$OC" api GET "/api/model?location%5Bdirectory%5D=$(jq -rn --arg d "$(jq -r .dir <<<"$1")" '$d|@uri')")
      m=$(jq -c --arg m "$model" '.data[] | select(.enabled and (.providerID+"/"+.id)==$m)' <<<"$models")
    fi
    [ -n "$m" ] || die "member $id: OpenCode model not enabled: $model (see oc.sh models)"
    local vs; vs=$(jq -r '[.variants[]?.id] | join("|")' <<<"$m")
    if [ -n "$vs" ]; then jq -e --arg e "$effort" '[.variants[].id] | index($e)' <<<"$m" >/dev/null || die "member $id: effort '$effort' is not a variant of $model (valid: $vs)"
    else [ "$effort" = "default" ] || die "member $id: $model has no variants; use effort \"default\""; fi
    echo "$id	$(jq -r '.limit.context' <<<"$m")"
  done
}

print_roster() {  # $1 = normalised config json, $2 = ctx-limit tsv (id<TAB>limit) or empty
  local cfg=$1 lim=$2
  echo "COUNCIL ROSTER"
  echo "  sessions: $(jq -r '.members|length' <<<"$cfg") total = $(jq -r '[.members[]|select(.kind=="opencode")]|length' <<<"$cfg") OpenCode + $(jq -r '[.members[]|select(.kind=="claude")]|length' <<<"$cfg") Claude Code"
  printf '  %-4s %-9s %-34s %-8s %-6s %-12s %s\n' id kind model effort mode agent/perm ctx-window
  jq -r '.members[] | "\(.id)\t\(.kind)\t\(.model)\t\(.effort)\t\(.mode)\t\(.agent // .permission_mode)"' <<<"$cfg" | while IFS=$'\t' read -r id kind model effort mode ag; do
    local l; l=$(printf '%s\n' "$lim" | awk -F'\t' -v i="$id" '$1==i{print $2}')
    [ -n "$l" ] && l="$((l/1000))k" || l="(from first reply)"
    printf '  %-4s %-9s %-34s %-8s %-6s %-12s %s\n' "$id" "$kind" "$model" "$effort" "$mode" "$ag" "$l"
  done
  echo "  executor (only member allowed to edit files): $(jq -r '.executor // "none — read-only council"' <<<"$cfg")"
  echo "  dir: $(jq -r .dir <<<"$cfg")"
  echo "  max_rounds/task: $(jq -r .max_rounds <<<"$cfg") · timeout/call: $(jq -r .timeout_s <<<"$cfg")s · handover at $(jq -r '.handover_at*100|floor' <<<"$cfg")% context · claude max_turns: $(jq -r .max_turns <<<"$cfg")"
  echo "  tasks:"; jq -r '.tasks[] | "    \(.id)\(if .execute then " [build]" else "" end): \(.text|gsub("\n";" ")|.[0:110])"' <<<"$cfg"
}

# --------------------------------------------------------------- adapters ----
# member call = launch (async) + collect (blocking). Raw reply -> $RUN/raw/<tag>-<id>.md ; tokens -> state.
oc_new_session() {  # idx -> prints ses_...
  local i=$1 sid; sid=$("$OC" new --dir "$DIR" --model "$(mget $i model)" --variant "$(mget $i effort)" --agent "$(mget $i agent)" \
        --title "Council $(mid $i) g$(mget $i gen) ($(mget $i model))") || return 1
  printf '%s' "$sid"
}
cl_new_session() { uuidgen | tr 'A-Z' 'a-z'; }

launch() {  # idx promptfile tag  -> starts the call; state gets .members[i].inflight
  local i=$1 pf=$2 tag=$3 kind sid
  kind=$(mget $i kind); sid=$(mget $i session)
  if [ "$kind" = opencode ]; then
    if [ "$sid" = null ] || [ -z "$sid" ]; then sid=$(oc_new_session $i) || return 1; sts --argjson i $i --arg s "$sid" '.members[$i].session=$s'; fi
    "$OC" prompt "$sid" --file "$pf" --no-wait >/dev/null || return 1
    sts --argjson i $i --arg t "$tag" '.members[$i].inflight={tag:$t}'
  else
    local args=(-p --output-format json --model "$(mget $i model)" --effort "$(mget $i effort)" --permission-mode "$(mget $i permission_mode)" --max-turns "$MAXT")
    if [ "$sid" = null ] || [ -z "$sid" ]; then sid=$(cl_new_session); args+=(--session-id "$sid"); sts --argjson i $i --arg s "$sid" '.members[$i].session=$s|.members[$i].cl_started=false'
    else args+=(--resume "$sid"); fi
    if [ "$(mget $i mode)" = read ]; then args+=(--disallowedTools "Edit Write MultiEdit NotebookEdit")
    else args+=(--allowedTools "Bash Edit Write MultiEdit NotebookEdit"); fi   # headless: nobody can answer a permission prompt
    local out="$RUN/raw/$tag-$(mid $i).json"
    ( cd "$DIR" && env -u CLAUDE_EFFORT claude "${args[@]}" <"$pf" >"$out" 2>"$out.err" ) &
    sts --argjson i $i --arg t "$tag" --argjson p $! '.members[$i].inflight={tag:$t,pid:$p}'
  fi
}

collect() {  # idx -> raw text in $RUN/raw/<tag>-<id>.md ; returns 0 ok / 1 failed
  local i=$1 kind sid tag id out rc=0
  kind=$(mget $i kind); sid=$(mget $i session); id=$(mid $i); tag=$(st ".members[$i].inflight.tag")
  out="$RUN/raw/$tag-$id.md"
  if [ "$kind" = opencode ]; then
    "$OC" wait "$sid" --timeout "$TIMEOUT" >/dev/null 2>"$RUN/raw/$tag-$id.err"; rc=$?
    if [ $rc -eq 2 ]; then "$OC" interrupt "$sid" >/dev/null 2>&1; log "member $id: timeout after ${TIMEOUT}s (interrupted)"; fi
    "$OC" result "$sid" >"$out" 2>&1
    grep -q '^\[error\]' "$out" && { log "member $id: $(grep '^\[error\]' "$out" | head -1)"; rc=1; }
    # tokens: last assistant message of this turn = context in use; session totals = consumed
    local msg; msg=$("$OC" api GET "/api/session/$sid/message?order=desc&limit=40" 2>/dev/null)
    local ctx; ctx=$(jq '[.data[] | select(.type=="assistant")][0].tokens | (.input + .output + (.reasoning//0) + .cache.read + .cache.write)' <<<"$msg" 2>/dev/null)
    local tot; tot=$("$OC" api GET "/api/session/$sid" 2>/dev/null | jq -c '.data | {t:(.tokens | .input + .output + (.reasoning//0) + .cache.read + .cache.write), c:(.cost//0)}')
    [ -n "$tot" ] || tot='{"t":0,"c":0}'
    [ -n "$ctx" ] && [ "$ctx" != null ] && sts --argjson i $i --argjson c "$ctx" --argjson t "$tot" \
      '.members[$i] |= (.ctx_used=$c | .session_tokens=$t.t | .session_cost=$t.c | .calls+=1 | .session_calls+=1)'
  else
    local pid; pid=$(st ".members[$i].inflight.pid"); local j="$RUN/raw/$tag-$id.json"
    local deadline=$(( $(date +%s) + TIMEOUT ))
    while kill -0 "$pid" 2>/dev/null; do
      if [ "$(date +%s)" -ge "$deadline" ]; then kill "$pid" 2>/dev/null; sleep 1; kill -9 "$pid" 2>/dev/null; log "member $id: timeout after ${TIMEOUT}s (killed)"; rc=1; break; fi
      sleep 2
    done
    wait "$pid" 2>/dev/null; local prc=$?
    if ! jq -e '.type=="result"' "$j" >/dev/null 2>&1; then log "member $id: claude exit $prc: $(head -c 300 "$j.err" "$j" 2>/dev/null | tr '\n' ' ')"; rc=1
    elif jq -e '.is_error==true' "$j" >/dev/null; then log "member $id: claude error: $(jq -r '.result' "$j" | head -c 300)"; rc=1; fi
    jq -r '.result // ""' "$j" >"$out" 2>/dev/null
    sts --argjson i $i '.members[$i].cl_started=true'
    local u; u=$(jq -c '{ctx:((.usage.iterations // [.usage] | last) | (.input_tokens + .cache_read_input_tokens + .cache_creation_input_tokens + .output_tokens)),
                        tot:(.usage | .input_tokens + .cache_read_input_tokens + .cache_creation_input_tokens + .output_tokens),
                        cum:([(.modelUsage // {})[] | .inputTokens + .cacheReadInputTokens + .cacheCreationInputTokens + .outputTokens] | add // 0),
                        lim:((.modelUsage // {}) | to_entries | max_by(.value.inputTokens + .value.cacheReadInputTokens + .value.cacheCreationInputTokens) | .value.contextWindow),
                        cost:(.total_cost_usd // 0)}' "$j" 2>/dev/null)
    [ -n "$u" ] && sts --argjson i $i --argjson u "$u" --argjson cum "$(st '.cl_cumulative // false')" \
      '.members[$i] |= (.ctx_used=$u.ctx | .calls+=1 | .session_calls+=1 | (if $u.lim then .ctx_limit=$u.lim else . end)
        | (if $cum then .session_tokens=$u.cum | .session_cost=$u.cost          # >= 2.1.277: result already carries the whole session
           else .session_tokens+=$u.tot | .session_cost+=$u.cost end))'
  fi
  sts --argjson i $i '.members[$i].inflight=null'
  return $rc
}

# last fenced ```json block of a post -> compact json on stdout (exit 1 if none/invalid)
tail_json() {
  awk 'BEGIN{b=0;last=""} /^[[:space:]]*```[[:space:]]*[Jj][Ss][Oo][Nn][[:space:]]*$/{b=1;buf="";next}
       /^[[:space:]]*```[[:space:]]*$/{if(b){b=0;last=buf};next} {if(b)buf=buf $0 "\n"} END{printf "%s",last}' "$1" | jq -c . 2>/dev/null
}

# ---------------------------------------------------------------- prompts ----
roster_lines() { st '.members[] | "  - member \(.id): \(.kind) \(.model), effort \(.effort), \(if .mode=="edit" then "may edit files (executor)" else "read-only" end)"'; }
rules_text() {  # sent on a session's first contact (and after handover)
  cat <<TXT
You are member $(mid $1) of a council of $N AI agents. The council receives tasks, discusses them through an orchestrator that relays every member's post to the others each round, and must reach an explicit, unanimous consensus on each task. Members:
$(roster_lines)
Working directory: $DIR (inspect it with your tools whenever a claim can be verified there; cite paths).
Executor: $( [ -n "$EXEC" ] && echo "member $EXEC is the only member allowed to modify files; everyone else is read-only." || echo "none — this council is read-only." )

Rules:
1. NEVER assume. Before proposing, list every choice the task leaves open (exact wording/text, names, language, formats, edge-case behaviour, tools/versions) and every fact you need. For each one: if the working directory settles it, verify it there and cite the path; otherwise it is a question for the user — vote "question" with ALL your questions in one post. Do NOT pick a "reasonable default" for anything the user might care about — triviality is not a reason to skip the question (for a hello-world, the greeting text is a question). A made-up choice is a failure, a precise question is not. Every open item you list must be marked settled_by "task" (quote the task), "dir" (cite the path), "user" (quote the user's answer given to the council) or "ask" — anything marked "ask" is sent to the user automatically, and a proposal that still contains "ask" items is not accepted as a proposal.
2. Verify before you claim. Check the code/files; say what you verified and what you could not.
3. Engage with the other members by name: say what you agree/disagree with and why. Change your mind when they are right.
4. Answer in the language of the task. Be concrete and complete; no padding.
5. Every post MUST end with a JSON tail — one fenced \`\`\`json block as the LAST thing in your reply. The orchestrator parses only that block, so the "proposal" field must contain your COMPLETE proposed answer (self-contained text; the prose above it is for the other members). Nothing may follow the JSON tail.
TXT
}
task_header() { local t=$1 r=$2; echo "=== TASK $(jq -r .id <<<"$t") — round $r of $MAXR ==="; }
task_text()   { jq -r '.text' <<<"$1"; }
task_intro()  { local t=$1; task_text "$t"; jq -e '.execute' <<<"$t" >/dev/null && echo "
(This is a BUILD task: the council first agrees on a PLAN — concrete files, changes, verification commands. Then executor $EXEC implements the plan, and the council ratifies the actual result.)"; }

prompt_round1() {  # idx task
  cat <<TXT
$(task_header "$2" 1)
$(task_intro "$2")
$(answers_block)
Your job this round: (1) list EVERY choice the task leaves open and every fact you need, and for each say what settles it: "task" (quote it), "dir" (path you verified), "user" (an answer from the user, quoted) or "ask" (the user must decide); (2) if any item is "ask", vote "question" (or vote "propose" — the orchestrator turns "ask" items into questions anyway); otherwise give your complete proposed $( jq -e '.execute' <<<"$2" >/dev/null && echo plan || echo answer ). Other members do the same; next round you will see their proposals.
JSON tail — exactly one of:
\`\`\`json
{"vote": "propose", "open": [{"item": "<open choice or needed fact>", "settled_by": "task|dir|user|ask", "where": "<quote from the task / file path / the user's answer, or the exact question for the user>"}], "proposal": "<your complete $( jq -e '.execute' <<<"$2" >/dev/null && echo plan || echo answer )>", "questions": []}
\`\`\`
\`\`\`json
{"vote": "question", "questions": ["<precise question for the user>"], "proposal": null}
\`\`\`
TXT
}
prompt_roundN() {  # idx task round -> uses state.candidate and posts of round-1
  local i=$1 t=$2 r=$3 prev=$((r-1)) tid; tid=$(jq -r .id <<<"$t")
  cat <<TXT
$(task_header "$t" "$r")
Candidate for this round — proposed by member $(st .candidate.member). Vote on this EXACT text:
<<<CANDIDATE $(st .candidate.id)
$(st .candidate.text)
>>>

Posts of the other members in round $prev:
TXT
  local j; for j in $(seq 0 $((N-1))); do
    [ "$j" = "$i" ] && continue
    printf -- '--- member %s ---\n' "$(mid $j)"; cat "$RUN/posts/$tid-r$prev-$(mid $j).md" 2>/dev/null || echo "(no post)"; echo
  done
  answers_block
  cat <<TXT
Your job this round: reply to the other members where you disagree (by name), then vote on the candidate.
- If the candidate is acceptable to you AS WRITTEN: vote "agree" (no proposal needed). Consensus needs every member to agree.
- Otherwise vote "disagree" and give a COMPLETE revised proposal (not a diff of the candidate — the full text), so it can become the next candidate.
- If you still lack information: vote "question".
JSON tail — exactly one of:
\`\`\`json
{"vote": "agree", "reason": "<one line>", "proposal": null, "questions": []}
\`\`\`
\`\`\`json
{"vote": "disagree", "reason": "<what is wrong with the candidate>", "proposal": "<complete revised text>", "questions": []}
\`\`\`
\`\`\`json
{"vote": "question", "questions": ["<precise question for the user>"], "proposal": null}
\`\`\`
TXT
}
prompt_exec() {  # idx task round
  cat <<TXT
=== TASK $(jq -r .id <<<"$2") — EXECUTION ===
$(answers_block)
The council reached consensus on this plan:
<<<PLAN
$(st '.results[-1].text')
>>>
You are the executor. Implement EXACTLY this plan in $DIR now — nothing more, nothing less. Run the verification the plan specifies. Do not ask the council; if you are blocked by missing information, stop and vote "question".
Then report: files changed (paths), commands run, verification evidence (actual output), and anything that deviated from the plan and why.
JSON tail — exactly one of:
\`\`\`json
{"vote": "done", "report": "<complete report as described>", "questions": []}
\`\`\`
\`\`\`json
{"vote": "question", "questions": ["<precise question for the user>"], "report": null}
\`\`\`
TXT
}
prompt_ratify() {  # idx task round
  local i=$1 t=$2 r=$3 tid; tid=$(jq -r .id <<<"$t")
  cat <<TXT
=== TASK $tid — RATIFICATION round $r of $MAXR ===
Executor $EXEC implemented the agreed plan. Verify the result in $DIR yourself (read the files, run read-only checks). Candidate = the implementation as it now exists on disk, described by this report and diff:
<<<CANDIDATE $(st .candidate.id)
$(st .candidate.text)
>>>
TXT
  if [ "$r" -gt 1 ]; then echo "Posts of the other members in the previous ratification round:"; local j; for j in $(seq 0 $((N-1))); do [ "$j" = "$i" ] && continue; printf -- '--- member %s ---\n' "$(mid $j)"; cat "$RUN/posts/$tid-x$((r-1))-$(mid $j).md" 2>/dev/null || echo "(no post)"; echo; done; fi
  answers_block
  cat <<TXT
Vote "agree" if the implementation satisfies the plan and the task. Vote "disagree" with a proposal listing the CONCRETE fixes the executor must apply (file, change, why). Vote "question" if you need the user.
JSON tail — exactly one of:
\`\`\`json
{"vote": "agree", "reason": "<one line>", "proposal": null, "questions": []}
\`\`\`
\`\`\`json
{"vote": "disagree", "reason": "<what is wrong>", "proposal": "<numbered list of concrete fixes>", "questions": []}
\`\`\`
\`\`\`json
{"vote": "question", "questions": ["<precise question for the user>"], "proposal": null}
\`\`\`
TXT
}
prompt_fix() {  # idx task round -> fixes collected from disagree votes
  cat <<TXT
=== TASK $(jq -r .id <<<"$2") — FIXES REQUESTED BY THE COUNCIL ===
$(answers_block)
The council did not ratify your implementation. Apply these fixes in $DIR (only these), re-run the verification, and report as before:
$(jq -r '.fixes[]? | "--- from member \(.member): \(.reason // "")\n\(.proposal // "")\n"' "$ST")
JSON tail — exactly one of:
\`\`\`json
{"vote": "done", "report": "<complete report: files changed, commands, verification output>", "questions": []}
\`\`\`
\`\`\`json
{"vote": "question", "questions": ["<precise question for the user>"], "report": null}
\`\`\`
TXT
}
answers_block() {  # user answers relevant to the current task (all of them, verbatim)
  local a; a=$(jq -r --arg t "$(st '.task_id')" '[.answers[]? | select(.task==$t)] | if length==0 then "" else "Answers from the user to the council'"'"'s questions (verbatim, authoritative):\n" + (map("- Q (member \(.member)): \(.question)\n  A: \(.answer)")|join("\n")) + "\n" end' "$ST")
  [ -n "$a" ] && printf '%s\n' "$a"
}
prompt_retry() { echo "Your previous reply could not be used: it did not end with a valid fenced \`\`\`json tail matching the required schema (or the call failed). Re-send your COMPLETE post for the current round now, ending with the JSON tail. Do not refer to your previous reply."; }
prompt_handover() {
  cat <<TXT
=== HANDOVER ===
Your session has used $(st ".members[$1].ctx_used") of $(st ".members[$1].ctx_limit") context tokens and will be replaced by a fresh session that continues as member $(mid $1). Write a complete handover note for your successor, who has NONE of your history: (1) the tasks so far and their outcomes; (2) the current task, the current candidate and your position on it with reasoning; (3) facts you verified (with file paths) and things you could not verify; (4) the other members' positions and where you disagree; (5) open items, pending questions and answers from the user. Self-contained plain text, no JSON tail needed.
TXT
}

# ------------------------------------------------------------- engine ----
ctx_pct() { st ".members[$1] | if (.ctx_limit // 0) > 0 and (.ctx_used // 0) > 0 then ((.ctx_used / .ctx_limit) * 100 | floor) else 0 end"; }
needs_handover() { st ".members[$1] | ((.session_calls // 0) >= 2 and (.ctx_limit // 0) > 0 and (.ctx_used // 0) > 0 and (.ctx_used / .ctx_limit) >= $HANDOVER)" | grep -q true; }

do_handover() {  # idx -> old session writes a note; new session created; note stored for the next prompt
  local i=$1 id; id=$(mid $i)
  log "member $id: context $(ctx_pct $i)% >= $(st ".config.handover_at*100|floor")% — handover to a new session"
  local pf="$RUN/prompts/handover-$id-g$(mget $i gen).md"; prompt_handover $i >"$pf"
  launch $i "$pf" "handover-g$(mget $i gen)" && collect $i
  local note="$RUN/raw/handover-g$(mget $i gen)-$id.md"; [ -s "$note" ] || echo "(the previous session produced no handover note)" >"$note"
  local old; old=$(mget $i session)
  sts --argjson i $i --arg note "$note" --arg old "$old" \
    '.members[$i] |= (.retired += [{session:.session, gen:.gen, tokens:.session_tokens, cost:.session_cost}] | .gen+=1 | .session=null | .fresh=true | .handover_note=$note | .ctx_used=0 | .session_tokens=0 | .session_cost=0 | .session_calls=0)
     | .log += ["\($now) member \(.members[$i].id) g\(.members[$i].gen): handover from \($old)"]' --arg now "$(now)"
  render_transcript
}

# run one deliberation step for all members: $1 = tag (r<N> / x<N> / exec), $2 = prompt generator (name), $3 = task json
# writes posts/<tid>-<tag>-<id>.md and .json (tail); sets .last_votes; returns 0 ok / 1 member failure
run_step() {
  local tag=$1 gen=$2 t=$3 tid i pf r=${4:-1} only=${5:-}
  tid=$(jq -r .id <<<"$t"); mkdir -p "$RUN/prompts" "$RUN/posts" "$RUN/raw"
  local idxs; if [ -n "$only" ]; then idxs=$only; else idxs=$(seq 0 $((N-1))); fi
  for i in $idxs; do
    needs_handover $i && do_handover $i
    pf="$RUN/prompts/$tid-$tag-$(mid $i).md"
    { if [ "$(mget $i fresh)" = true ]; then rules_text $i; echo
        local hn; hn=$(mget $i handover_note); [ "$hn" != null ] && [ -n "$hn" ] && { echo "Handover note from your predecessor session (same member id):"; echo "<<<HANDOVER"; cat "$hn"; echo ">>>"; echo; }; fi
      $gen $i "$t" "$r"; } >"$pf"
    launch $i "$pf" "$tid-$tag" || { log "member $(mid $i): launch failed"; return 1; }
  done
  local fail=0
  for i in $idxs; do
    local id; id=$(mid $i); local ok=0 attempt
    for attempt in 1 2; do
      if collect $i && tail_json "$RUN/raw/$tid-$tag-$id.md" >"$RUN/posts/$tid-$tag-$id.json" && jq -e '(.vote|IN("propose","agree","disagree","question","done")) and
                 (if .vote=="propose" or .vote=="disagree" then ((.proposal|type)=="string" and (.proposal|length)>0)
                  elif .vote=="done" then ((.report|type)=="string" and (.report|length)>0)
                  elif .vote=="question" then ((.questions|type)=="array" and (.questions|length)>0) else true end)' "$RUN/posts/$tid-$tag-$id.json" >/dev/null; then ok=1; break; fi
      [ $attempt -eq 1 ] && { log "member $id: no valid JSON tail / call failed — retrying once"; prompt_retry >"$RUN/prompts/$tid-$tag-$id-retry.md"; launch $i "$RUN/prompts/$tid-$tag-$id-retry.md" "$tid-$tag" || break; }
    done
    sts --argjson i $i '.members[$i].fresh=false | .members[$i].handover_note=null'
    if [ $ok -eq 1 ]; then
      cp "$RUN/raw/$tid-$tag-$id.md" "$RUN/posts/$tid-$tag-$id.md"
      # no-assumptions guard: an open item not settled by the task or the working directory is a question, whatever the vote says
      if jq -e '(.vote=="propose" or .vote=="disagree") and any((.open // [])[]; (.settled_by|ascii_downcase|IN("task","dir","user"))|not)' "$RUN/posts/$tid-$tag-$id.json" >/dev/null 2>&1; then
        jq -c '.questions = ((.questions // []) + [ .open[] | select((.settled_by|ascii_downcase|IN("task","dir","user"))|not) | (.where // .item) ]) | .vote="question" | .proposal=null' "$RUN/posts/$tid-$tag-$id.json" >"$RUN/posts/$tid-$tag-$id.json.tmp" && mv "$RUN/posts/$tid-$tag-$id.json.tmp" "$RUN/posts/$tid-$tag-$id.json"
        log "member $id: proposal had open items not settled by task/dir — converted to questions for the user"
      fi
    else fail=1; log "member $id: failed twice in step $tag"; fi
    local vote=FAILED; [ $ok -eq 1 ] && vote=$(jq -r '.vote' "$RUN/posts/$tid-$tag-$id.json")
    local cu cl stt; cu=$(st ".members[$i].ctx_used // 0"); cl=$(st ".members[$i].ctx_limit // \"?\""); stt=$(st ".members[$i].session_tokens // 0")
    log "$(now) $tid $tag member $id: $vote · ctx $(ctx_pct $i)% [$cu/$cl] · session tokens $stt"
  done
  # collect votes from the post tails
  local votes="[]"; for i in $idxs; do local id; id=$(mid $i); [ -f "$RUN/posts/$tid-$tag-$id.json" ] && votes=$(jq -c --arg m "$id" --slurpfile p "$RUN/posts/$tid-$tag-$id.json" '. + [ $p[0] + {member:$m} ]' <<<"$votes"); done
  sts --argjson v "$votes" '.last_votes=$v'
  render_transcript
  return $fail
}

pause_for_questions() {  # -> writes questions.json, exit 4
  local tid; tid=$(st .task_id)
  jq -c --arg t "$tid" --arg r "$(st .round)" '[.last_votes[] | select(.vote=="question") | .member as $m | (.questions // [])[] | {id:($t+"/r"+$r+"/"+$m), task:$t, member:$m, question:.}]
      | to_entries | map(.value + {id:(.value.id+"/q"+((.key+1)|tostring))})' "$ST" >"$RUN/questions.json"
  sts --slurpfile q "$RUN/questions.json" '.pending_questions=$q[0] | .status="questions"'
  render_transcript
  echo "council: QUESTIONS FOR THE USER (task $tid) — answer them and run: council.sh resume --run-dir $RUN --answers answers.json" >&2
  jq -r '.[] | "  [\(.id)] member \(.member): \(.question)"' "$RUN/questions.json" >&2
  echo "$RUN/questions.json"; exit 4
}
has_questions() { jq -e 'any(.last_votes[]; .vote=="question")' "$ST" >/dev/null; }
all_agree()     { jq -e --argjson n "$N" '(.last_votes|length)==$n and all(.last_votes[]; .vote=="agree")' "$ST" >/dev/null; }

# current position of member idx after a round: its proposal if it proposed/disagreed, else the candidate it agreed to
position_of() { local id; id=$(mid $1); jq -r --arg m "$id" '(.last_votes[] | select(.member==$m)) as $v | if ($v.proposal // "") != "" then $v.proposal else .candidate.text end' "$ST"; }

deliberate() {  # task json -> sets .results[-1] {task, outcome, text, rounds}; returns 0 consensus / 1 unresolved (exit 2/4 inside)
  local t=$1 tid r; tid=$(jq -r .id <<<"$t")
  r=$(st .round)
  while [ "$r" -le "$MAXR" ]; do
    sts --argjson r $r '.round=$r'
    if [ "$r" -eq 1 ]; then
      run_step "r1" prompt_round1 "$t" 1 || { sts '.status="failed"'; log "checkpointed; fix the cause and run: council.sh resume --run-dir $RUN"; exit 2; }
      has_questions && pause_for_questions
      # candidate for round 2: proposer = member 0
      sts --arg tid "$tid" --arg m "$(mid 0)" --arg txt "$(position_of 0)" '.candidate={id:($tid+"-c1"), member:$m, text:$txt}'
    else
      run_step "r$r" prompt_roundN "$t" $r || { sts '.status="failed"'; exit 2; }
      has_questions && pause_for_questions
      if all_agree; then
        sts --arg tid "$tid" --argjson r $r '.results += [{task:$tid, outcome:"consensus", text:.candidate.text, rounds:$r, candidate:.candidate.id}] | .round=1'
        log "task $tid: CONSENSUS in round $r on candidate $(st .candidate.id)"; return 0
      fi
      local p=$(( (r-1) % N ))   # rotating proposer for the next candidate
      sts --arg tid "$tid" --argjson r $r --arg m "$(mid $p)" --arg txt "$(position_of $p)" '.candidate={id:($tid+"-c"+($r|tostring)), member:$m, text:$txt}'
    fi
    r=$((r+1))
  done
  # round 1 only (max_rounds=1) or exhausted: record unresolved with everyone's last position
  sts --arg tid "$tid" --argjson r "$MAXR" '.results += [{task:$tid, outcome:"unresolved", text:.candidate.text, rounds:$r, dissent:[.last_votes[] | select(.vote!="agree") | {member, reason, proposal}]}] | .round=1'
  log "task $tid: UNRESOLVED after $MAXR rounds"; return 1
}

executor_idx() { local i; for i in $(seq 0 $((N-1))); do [ "$(mid $i)" = "$EXEC" ] && { echo $i; return; }; done; }
diff_text() {  # executor idx -> diff of the working dir as seen by the executor's tool
  local i=$1; if [ "$(mget $i kind)" = opencode ]; then "$OC" diff "$(mget $i session)" --patch 2>/dev/null | head -c 20000
  elif git -C "$DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then (cd "$DIR" && git status --short && git diff) | head -c 20000
  else echo "(dir is not a git repository — no diff available; rely on the report and inspect the files)"; fi
}
execute_and_ratify() {  # task json; assumes .results[-1] is the consensus plan; returns 0 ratified / 1 unresolved
  local t=$1 tid ei r; tid=$(jq -r .id <<<"$t"); ei=$(executor_idx)
  r=$(st .round)
  if [ "$(st .phase)" = "plan" ]; then sts '.phase="exec"'; fi
  while [ "$r" -le "$MAXR" ]; do
    sts --argjson r $r '.round=$r'
    if [ "$(st .phase)" = "exec" ]; then
      local gen; if [ "$r" -eq 1 ]; then gen=prompt_exec; else gen=prompt_fix; fi
      run_step "exec$r" $gen "$t" $r "$ei" || { sts '.status="failed"'; exit 2; }
      has_questions && pause_for_questions
      sts --arg tid "$tid" --argjson r $r --arg m "$EXEC" --arg rep "$(jq -r '.last_votes[0].report // ""' "$ST")" --arg d "$(diff_text $ei)" \
        '.candidate={id:($tid+"-impl"+($r|tostring)), member:$m, text:("REPORT BY EXECUTOR " + $m + ":\n" + $rep + "\n\nDIFF:\n" + $d)} | .phase="ratify"'
    fi
    run_step "x$r" prompt_ratify "$t" $r || { sts '.status="failed"'; exit 2; }
    has_questions && pause_for_questions
    if all_agree; then
      sts --arg tid "$tid" --argjson r $r '.results[-1] += {outcome:"ratified", implementation:.candidate.text, ratify_rounds:$r} | .round=1 | .phase="plan"'
      log "task $tid: implementation RATIFIED in ratification round $r"; return 0
    fi
    sts '.fixes=[.last_votes[] | select(.vote=="disagree")] | .phase="exec"'; r=$((r+1))
  done
  sts --arg tid "$tid" '.results[-1] += {outcome:"unratified", implementation:.candidate.text, dissent:[.last_votes[] | select(.vote!="agree") | {member, reason, proposal}]} | .round=1 | .phase="plan"'
  log "task $tid: implementation NOT ratified after $MAXR rounds"; return 1
}

run_tasks() {  # from state.task_idx onward
  local nt ti unresolved=0; nt=$(st '.config.tasks|length'); ti=$(st .task_idx)
  while [ "$ti" -lt "$nt" ]; do
    local t; t=$(stj ".config.tasks[$ti]"); local tid; tid=$(jq -r .id <<<"$t")
    sts --argjson ti $ti --arg tid "$tid" '.task_idx=$ti | .task_id=$tid | .status="running"'
    log "==== task $tid ($((ti+1))/$nt) phase $(st .phase) round $(st .round) ===="
    if [ "$(st .phase)" = "plan" ]; then
      if deliberate "$t"; then
        if jq -e '.execute' <<<"$t" >/dev/null; then execute_and_ratify "$t" || unresolved=1; fi
      else unresolved=1; fi
    else execute_and_ratify "$t" || unresolved=1; fi
    ti=$((ti+1)); sts --argjson ti $ti '.task_idx=$ti | .round=1 | .phase="plan" | .candidate=null | .last_votes=[] | .fixes=null'
  done
  sts '.status="done"'; render_transcript
  log "done — transcript: $RUN/transcript.md"; echo "$RUN/transcript.md"
  [ $unresolved -eq 0 ] && exit 0 || exit 5
}

# ------------------------------------------------------------ transcript ----
render_transcript() {
  {
    echo "# Council run — $(st .started) — $RUN"; echo
    echo "status: **$(st .status)** · task $(st '.task_id // "-"') · phase $(st .phase) · round $(st .round)"; echo
    echo "## Roster"; echo; echo '```'; print_roster "$(stj .config)" "$(st '.members[] | "\(.id)\t\(.ctx_limit // "")"')"; echo '```'; echo
    echo "## Members — context and tokens"; echo
    echo "| member | generation | session | context used | session tokens | cost | calls | retired sessions |"; echo "|---|---|---|---|---|---|---|---|"
    st '.members[] | "| \(.id) | g\(.gen) | `\(.session // "-")` | \(.ctx_used // 0) / \(.ctx_limit // "?") (\(if (.ctx_limit//0)>0 then ((.ctx_used//0)/.ctx_limit*100|floor) else 0 end)%) | \(.session_tokens // 0) | \(.session_cost // 0 | .*10000|round/10000) | \(.calls // 0) | \((.retired // []) | map("\(.session) (\(.tokens) tok)") | join(", ")) |"'
    echo
    local nt ti; nt=$(st '.config.tasks|length')
    for ti in $(seq 0 $((nt-1))); do
      local t tid; t=$(stj ".config.tasks[$ti]"); tid=$(jq -r .id <<<"$t")
      echo "## Task $tid$(jq -r 'if .execute then " [build]" else "" end' <<<"$t")"; echo; task_text "$t"; echo
      local res; res=$(jq -c --arg t "$tid" '[.results[]? | select(.task==$t)] | last // empty' "$ST")
      if [ -n "$res" ]; then
        echo "**Outcome: $(jq -r .outcome <<<"$res")** ($(jq -r '.rounds' <<<"$res") rounds$(jq -r 'if .ratify_rounds then ", \(.ratify_rounds) ratification rounds" else "" end' <<<"$res"))"; echo
        echo "### Agreed text"; echo; jq -r .text <<<"$res"; echo
        jq -e '.implementation' <<<"$res" >/dev/null 2>&1 && { echo "### Implementation (as ratified/last)"; echo; jq -r .implementation <<<"$res"; echo; }
        jq -e '.dissent' <<<"$res" >/dev/null 2>&1 && { echo "### Dissent"; echo; jq -r '.dissent[] | "- **\(.member)**: \(.reason // "")\n\n\(.proposal // "")\n"' <<<"$res"; }
      fi
      local f; for f in $(ls -tr "$RUN/posts" 2>/dev/null | grep "^$tid-" | grep '\.md$'); do
        local tag=${f#$tid-}; tag=${tag%.md}; local step=${tag%-*} mem=${tag##*-}
        echo "### $step — member $mem ($(jq -r '.vote // "?"' "$RUN/posts/${f%.md}.json" 2>/dev/null))"; echo; cat "$RUN/posts/$f"; echo
      done
    done
    local qa; qa=$(jq -r '.answers[]? | "- [\(.id)] member \(.member): \(.question)\n  → \(.answer)"' "$ST"); [ -n "$qa" ] && { echo "## Questions and answers"; echo; echo "$qa"; echo; }
    local pq; pq=$(jq -r '.pending_questions[]? | "- [\(.id)] member \(.member): \(.question)"' "$ST"); [ -n "$pq" ] && { echo "## PENDING questions for the user"; echo; echo "$pq"; echo; }
    echo "## Log"; echo; st '.log[]? | "- " + .'
  } >"$RUN/transcript.md.tmp" 2>/dev/null && mv "$RUN/transcript.md.tmp" "$RUN/transcript.md"
}

# ------------------------------------------------------------- commands ----
case "$CMD" in
  show)
    [ -n "$CONFIG" ] || die "show needs --config F"
    cfg=$(validate_config "$CONFIG") || exit 1
    "$OC" ensure >/dev/null || exit 1
    lim=$(check_opencode_models "$cfg") || exit 1
    print_roster "$cfg" "$lim"; echo "config OK: $CONFIG" ;;

  start)
    [ -n "$CONFIG" ] && [ -n "$RUN" ] || die "start needs --config F --run-dir D"
    RUN=$(abs "$RUN"); [ -e "$RUN" ] && die "run dir exists: $RUN (use resume, or a new dir)"
    cfg=$(validate_config "$CONFIG") || exit 1
    "$OC" ensure >/dev/null || exit 1
    lim=$(check_opencode_models "$cfg") || exit 1
    mkdir -p "$RUN/prompts" "$RUN/posts" "$RUN/raw"; cp "$CONFIG" "$RUN/config.json"
    jq -n --argjson cfg "$cfg" --arg lim "$lim" --arg run "$RUN" --arg ts "$(date '+%Y-%m-%d %H:%M:%S')" --argjson cum "$(claude_cumulative)" '
      ($lim | split("\n") | map(select(length>0) | split("\t") | {key:.[0], value:(.[1]|tonumber)}) | from_entries) as $L |
      {config:$cfg, run_dir:$run, started:$ts, status:"created", cl_cumulative:$cum, task_idx:0, task_id:null, round:1, phase:"plan", candidate:null, last_votes:[],
       answers:[], pending_questions:null, results:[], log:[],
       members:[ $cfg.members[] | . + {session:null, gen:1, fresh:true, handover_note:null, ctx_used:0, ctx_limit:($L[.id] // null), session_tokens:0, session_cost:0, calls:0, session_calls:0, retired:[], inflight:null} ]}' >"$RUN/state.json"
    load_state
    print_roster "$cfg" "$lim" >&2; log "run dir: $RUN"
    run_tasks ;;

  status)
    [ -n "$RUN" ] || die "status needs --run-dir D"; RUN=$(abs "$RUN"); load_state
    echo "run: $RUN · status: $(st .status) · task $(st '.task_id // "-"') ($(st .task_idx)/$(st '.config.tasks|length') done) · phase $(st .phase) · round $(st .round)/$MAXR"
    st '.members[] | "  member \(.id) g\(.gen) \(.kind) \(.model) [\(.effort)] session \(.session // "-") · ctx \(.ctx_used // 0)/\(.ctx_limit // "?") (\(if (.ctx_limit//0)>0 then ((.ctx_used//0)/.ctx_limit*100|floor) else 0 end)%) · session tokens \(.session_tokens // 0) · cost \(.session_cost // 0) · calls \(.calls // 0) · retired \((.retired//[])|length)"'
    st '.results[] | "  task \(.task): \(.outcome) (\(.rounds) rounds)"'
    jq -r '.pending_questions[]? | "  PENDING [\(.id)] member \(.member): \(.question)"' "$ST"
    echo "  transcript: $RUN/transcript.md" ;;

  resume)
    [ -n "$RUN" ] || die "resume needs --run-dir D"; RUN=$(abs "$RUN"); load_state
    "$OC" ensure >/dev/null || exit 1
    status=$(st .status)
    case "$status" in
      questions)
        pend=$(stj '.pending_questions')
        if [ -n "$ANSWERS_FILE" ]; then
          [ -f "$ANSWERS_FILE" ] || die "answers file not found: $ANSWERS_FILE"
          # accepted: {"<qid>":"answer",...}  or  [{"id":"<qid>","answer":"..."}]
          ans=$(jq -c 'if type=="array" then map({key:.id, value:.answer}) | from_entries else . end' "$ANSWERS_FILE") || die "answers must be JSON: {qid: answer} or [{id, answer}]"
          missing=$(jq -r --argjson a "$ans" '.[] | select($a[.id]==null) | .id' <<<"$pend")
          [ -z "$missing" ] || die "no answer for: $(echo $missing) — every pending question needs an answer (or use --answer TEXT for one answer to all)"
          sts --argjson a "$ans" '.answers += [ .pending_questions[] | . + {answer:$a[.id]} ]'
        elif [ -n "$ANSWER_TEXT" ]; then
          sts --arg a "$ANSWER_TEXT" '.answers += [ .pending_questions[] | . + {answer:$a} ]'
        else die "pending questions — pass --answers answers.json ({qid: answer}) or --answer TEXT (same answer to all). See $RUN/questions.json"; fi
        sts '.pending_questions=null | .status="running" | .last_votes=[]'; rm -f "$RUN/questions.json"
        log "answers recorded — re-running task $(st .task_id) phase $(st .phase) round $(st .round) with the answers (round budget not consumed)" ;;
      failed|running|created)
        log "resuming task $(st '.task_id // "-"') phase $(st .phase) round $(st .round) (the interrupted round is re-run)"
        sts '.status="running" | .last_votes=[] | .members |= map(.inflight=null)' ;;
      done) die "this run is finished (see $RUN/transcript.md)" ;;
      *) die "unknown status: $status" ;;
    esac
    run_tasks ;;

  *) die "unknown command: $CMD (show|start|status|resume)" ;;
esac
