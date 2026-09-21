#!/usr/bin/env bash
# oc.sh — thin CLI over the OpenCode v2 HTTP API, used by the Claude Code "opencode" skill.
#
# Talks to the OpenCode *background service* (the same server `opencode` TUI and
# `opencode api` use). If the service is not running it is started with
# `opencode service start`. Auth + URL are read from the service state file.
#
# Override the target server (no auto-start) with:
#   OPENCODE_URL=http://127.0.0.1:4096 [OPENCODE_PASSWORD=... OPENCODE_USERNAME=opencode]
#
# Exit codes: 0 ok · 1 error · 2 wait timeout (session still running) · 3 blocked on permission request
set -o pipefail

STATE_FILE="${XDG_STATE_HOME:-$HOME/.local/state}/opencode/service.json"
DEFAULT_DIR="${CLAUDE_PROJECT_DIR:-$PWD}"
WAIT_TIMEOUT="${OPENCODE_WAIT_TIMEOUT:-600}"
API_TIMEOUT="${OPENCODE_API_TIMEOUT:-30}"

BASE=""; AUTH=(); STARTED=false

die()  { echo "oc: $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "missing dependency: $1"; }
need curl; need jq

usage() {
  cat <<'USAGE'
usage: oc.sh <command> [options]

server
  ensure                         make sure the OpenCode service is running (start it if not); prints {url,version,pid,started}
  status                         print server info, or "stopped" (exit 1); never starts it
  stop                           stop the background service (opencode service stop)

discovery
  models [FILTER] [--dir D]      enabled models as "provider/id<TAB>name" (default model marked with *)
  agents [--dir D]               primary agents (id, mode, description)
  sessions [--dir D] [--limit N] recent sessions for a directory

sessions
  new [--dir D] [--model P/ID] [--variant V] [--agent A] [--title T] [--ask] [--allow A[:R]]... [--deny A[:R]]...
                                 create a session, print its id
  prompt SID TEXT|- [--file F] [--no-wait] [--timeout S] [--ask]     send a prompt (TEXT, "-" = stdin, or --file), wait, print result
  run TEXT|- [new options] [--file F] [--timeout S]                  new + prompt + wait + result in one go (session id on stderr)
  wait SID [--timeout S] [--ask]                                     wait until the session is idle (handles permission requests)
  result SID                                                         text of the last assistant turn (+ errors/outcome)
  messages SID [--limit N] [--raw]                                   compact transcript (or raw JSON)
  diff SID [--patch]                                                 files changed in the last turn (with patches)
  permissions SID                                                    pending permission requests
  reply SID PER_ID once|always|reject [MESSAGE]                      answer a permission request
  interrupt SID                                                      stop the running turn
  api METHOD PATH [JSON]                                             raw request against the server (e.g. api GET /api/agent)

options
  --dir D      project directory the session works in (default: $CLAUDE_PROJECT_DIR or cwd)
  --model P/ID model ref, e.g. opencode/muse-spark-1.3-contributor-free (see: oc.sh models)
  --variant V  model variant = effort/thinking level (per model, e.g. low|medium|high|xhigh|max; see variants in oc.sh api GET /api/model)
  --ask        do NOT auto-approve: sessions keep OpenCode's default ask rules and a pending request stops wait/prompt with exit 3
               (default is allow-all: new/run put an allow-all ruleset on the session, wait/prompt reply "once"; env OPENCODE_AUTO=false flips the default)
  --allow A[:R]  / --deny A[:R]   extra per-session permission rule (new/run), e.g. --deny bash:rm* --allow external_directory
               actions: read edit glob grep list bash task external_directory webfetch websearch skill lsp ... (* = any)
  --timeout S  max seconds to wait for a turn (default $OPENCODE_WAIT_TIMEOUT or 600)
USAGE
}

# ---------------------------------------------------------------- server ----
load_state() {
  [ -f "$STATE_FILE" ] || return 1
  BASE=$(jq -r '.url // empty' "$STATE_FILE" 2>/dev/null)
  local pw; pw=$(jq -r '.password // empty' "$STATE_FILE" 2>/dev/null)
  [ -n "$BASE" ] || return 1
  AUTH=(-u "${OPENCODE_USERNAME:-opencode}:$pw")
}

LAST_CODE=""
healthy() {
  LAST_CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "${AUTH[@]}" "$1/api/info" 2>/dev/null)
  [ "$LAST_CODE" = "200" ]
}

# resolve [nostart]: sets BASE/AUTH, starting the managed service unless "nostart".
resolve() {
  if [ -n "$OPENCODE_URL" ]; then
    BASE="${OPENCODE_URL%/}"
    [ -n "$OPENCODE_PASSWORD" ] && AUTH=(-u "${OPENCODE_USERNAME:-opencode}:$OPENCODE_PASSWORD")
    if ! healthy "$BASE"; then
      [ "$LAST_CODE" = "401" ] && die "server $BASE rejected the credentials (HTTP 401): set OPENCODE_PASSWORD (printed by 'opencode serve' at startup) and, if not 'opencode', OPENCODE_USERNAME"
      die "server $BASE is not reachable (HTTP ${LAST_CODE:-000}); OPENCODE_URL is set, so it is not auto-started"
    fi
    return 0
  fi
  if load_state && healthy "$BASE"; then return 0; fi
  [ "$LAST_CODE" = "401" ] && die "service at $BASE answered HTTP 401 with the password from $STATE_FILE; try: opencode service restart"
  [ "$1" = "nostart" ] && return 1
  need opencode
  echo "oc: OpenCode service is not running — starting it (opencode service start)..." >&2
  local out; out=$(opencode service start 2>&1) || die "'opencode service start' failed: $out"
  local i
  for i in $(seq 1 30); do
    if load_state && healthy "$BASE"; then STARTED=true; return 0; fi
    sleep 1
  done
  die "OpenCode service did not become healthy within 30s (try: opencode service status / opencode service restart)"
}

# api METHOD PATH [JSON_BODY] -> prints response body; non-zero on HTTP >= 300
api() {
  local method=$1 path=$2 body=$3 out code
  out=$(mktemp)
  if [ -n "$body" ]; then
    code=$(curl -s -o "$out" -w '%{http_code}' --max-time "$API_TIMEOUT" "${AUTH[@]}" -X "$method" "$BASE$path" \
           -H 'Content-Type: application/json' --data-binary "$body")
  else
    code=$(curl -s -o "$out" -w '%{http_code}' --max-time "$API_TIMEOUT" "${AUTH[@]}" -X "$method" "$BASE$path")
  fi
  cat "$out"; rm -f "$out"
  case "$code" in
    2*) return 0 ;;
    *)  echo "" >&2; echo "oc: HTTP $code for $method $path" >&2; return 1 ;;
  esac
}

loc_q() { printf 'location%%5Bdirectory%%5D=%s' "$(jq -rn --arg d "$1" '$d|@uri')"; }
abs()   { case "$1" in /*) printf '%s' "$1" ;; *) printf '%s' "$PWD/${1#./}" ;; esac; }

# --------------------------------------------------------------- helpers ----
# Permissions default to allow-all (user preference): sessions get an allow-all ruleset and pending
# requests are answered "once". Opt out per call with --ask, or globally with OPENCODE_AUTO=false.
AUTO="${OPENCODE_AUTO:-true}"
DIR="$DEFAULT_DIR"; MODEL=""; VARIANT=""; AGENT=""; TITLE=""; NOWAIT=false; FILE=""; LIMIT=""; RAW=false; PATCH=false; RULES="[]"
POS=()
parse_opts() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --dir|--model|--variant|--agent|--title|--file|--timeout|--limit|--allow|--deny)
                 [ $# -ge 2 ] || die "missing value for $1" ;;
    esac
    case "$1" in
      --dir)     DIR=$(abs "$2"); shift 2 ;;
      --model)   MODEL=$2; shift 2 ;;
      --variant) VARIANT=$2; shift 2 ;;
      --agent)   AGENT=$2; shift 2 ;;
      --title)   TITLE=$2; shift 2 ;;
      --file)    FILE=$2; shift 2 ;;
      --timeout) WAIT_TIMEOUT=$2; shift 2 ;;
      --limit)   LIMIT=$2; shift 2 ;;
      --auto)    AUTO=true; shift ;;
      --ask)     AUTO=false; shift ;;
      --allow|--deny)
                 # --allow ACTION[:RESOURCE]  e.g. --allow read:*.env  --allow external_directory  --deny bash:rm*
                 RULES=$(jq -c --arg e "${1#--}" --arg r "$2" '. + [{action:($r|split(":")[0]), resource:(($r|split(":")[1:]|join(":")) | if .=="" then "*" else . end), effect:$e}]' <<<"$RULES")
                 shift 2 ;;
      --no-wait) NOWAIT=true; shift ;;
      --raw)     RAW=true; shift ;;
      --patch)   PATCH=true; shift ;;
      -h|--help) usage; exit 0 ;;
      --*)       die "unknown option: $1" ;;
      *)         POS+=("$1"); shift ;;
    esac
  done
}

read_text() {  # $1 = positional text ("-" = stdin); --file wins
  if [ -n "$FILE" ]; then cat "$FILE"
  elif [ "$1" = "-" ] || [ -z "$1" ]; then cat
  else printf '%s' "$1"; fi
}

check_sid() { case "$1" in ses*) ;; *) die "session id expected (ses_...), got: '$1'" ;; esac; }

model_json() {  # provider/id -> Model.Ref json (or empty)
  [ -z "$MODEL" ] && return
  case "$MODEL" in */*) ;; *) die "--model must be provider/id (see: oc.sh models)";; esac
  jq -cn --arg m "$MODEL" --arg v "$VARIANT" '{providerID:($m|split("/")[0]), id:($m|split("/")[1:]|join("/"))} + (if $v != "" then {variant:$v} else {} end)'
}

create_session() {
  local body
  body=$(jq -cn --arg dir "$DIR" --arg title "$TITLE" --arg agent "$AGENT" --argjson model "${MODEL_JSON:-null}" --argjson auto "$AUTO" --argjson rules "$RULES" '
    ((if $auto then [{action:"*",resource:"*",effect:"allow"}] else [] end) + $rules) as $perms
    | {location:{directory:$dir}}
    + (if $title != "" then {title:$title} else {} end)
    + (if $agent != "" then {agent:$agent} else {} end)
    + (if $model != null then {model:$model} else {} end)
    + (if ($perms|length) > 0 then {permissions:$perms} else {} end)')
  api POST /api/session "$body" | jq -r '.data.id'
}

send_prompt() {  # sid text
  local body; body=$(jq -cn --arg t "$2" '{text:$t}')
  api POST "/api/session/$1/prompt" "$body" | jq -r '.data.id'
}

last_type() { api GET "/api/session/$1/message?order=desc&limit=1" | jq -r '.data[0].type // "none"'; }

handle_permissions() {  # sid -> 0 none pending / 0 auto-approved / 3 pending and not auto
  local sid=$1 pend
  pend=$(api GET "/api/session/$sid/permission" | jq -c '.data // []') || return 1
  [ "$pend" = "[]" ] && return 0
  if [ "$AUTO" = true ]; then
    echo "$pend" | jq -r '.[] | "\(.id)\t\(.action) \(.resources|join(", "))"' | while IFS=$'\t' read -r pid what; do
      echo "oc: auto-approving permission $pid ($what)" >&2
      api POST "/api/session/$sid/permission/$pid/reply" '{"decision":"once"}' >/dev/null
    done
    return 0
  fi
  echo "oc: session $sid is BLOCKED on permission request(s):" >&2
  echo "$pend" | jq -r '.[] | "  \(.id)\t\(.action)\t\(.resources|join(", "))\(if .message then "\t"+.message else "" end)"' >&2
  echo "oc: answer with:  oc.sh reply $sid <per_id> once|always|reject   then  oc.sh wait $sid   (or: oc.sh wait $sid --auto)" >&2
  return 3
}

wait_idle() {  # sid ; returns 0 idle, 2 timeout, 3 blocked on permission, 1 error
  local sid=$1 deadline code rc chunk settle=0
  deadline=$(( $(date +%s) + WAIT_TIMEOUT ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    handle_permissions "$sid"; rc=$?
    [ $rc -ne 0 ] && return $rc
    chunk=$(( deadline - $(date +%s) )); [ $chunk -gt 10 ] && chunk=10; [ $chunk -lt 1 ] && chunk=1
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time "$chunk" "${AUTH[@]}" -X POST "$BASE/api/experimental/session/$sid/wait")
    case "$code" in
      204|200)
        # idle according to the server; make sure the turn actually got recorded (idle marker is last)
        if [ "$(last_type "$sid")" = "idle" ] || [ $settle -ge 5 ]; then return 0; fi
        settle=$((settle+1)); sleep 1 ;;
      000) ;;   # curl timed out: loop, re-check permissions
      *) echo "oc: wait returned HTTP $code" >&2; return 1 ;;
    esac
  done
  echo "oc: timed out after ${WAIT_TIMEOUT}s; session $sid is still running (oc.sh wait $sid | oc.sh interrupt $sid)" >&2
  return 2
}

show_result() {  # sid -> text of the last turn
  api GET "/api/session/$1/message?order=desc&limit=40" | jq -r '
    .data as $m
    | ($m | map(.type=="user") | index(true)) as $u
    | ($m | if $u == null then . else .[:$u] end | reverse) as $turn
    | ($turn | map(select(.type=="assistant") | .content[]? | select(.type=="text") | .text) | join("\n")) as $text
    | ($turn | map(select(.type=="assistant" and .error != null) | .error.message)) as $errs
    | ($turn | map(select(.type=="idle") | .outcome) | last) as $outcome
    | (if $text == "" then "(no text output in this turn)" else $text end),
      (if ($errs|length) > 0 then "\n[error] " + ($errs|join("; ")) else empty end),
      (if $outcome != null and $outcome != "succeeded" then "\n[outcome] " + $outcome else empty end)'
}

# -------------------------------------------------------------- commands ----
cmd=$1; shift 2>/dev/null
[ -z "$cmd" ] && { usage; exit 1; }
parse_opts "$@"
set -- "${POS[@]}"

case "$cmd" in
  -h|--help|help) usage ;;

  ensure)
    resolve
    api GET /api/info | jq -c --argjson s "$STARTED" '{url:.urls[0], version, pid, started:$s}' ;;

  status)
    if resolve nostart; then api GET /api/info | jq -c '{url:.urls[0], version, pid}'
    else echo "stopped"; exit 1; fi ;;

  stop)
    [ -n "$OPENCODE_URL" ] && die "OPENCODE_URL is set; refusing to stop a server I do not manage"
    need opencode; opencode service stop ;;

  models)
    resolve
    local_def=$(api GET "/api/model/default?$(loc_q "$DIR")" | jq -r '.data | if . then .providerID+"/"+.id else "" end')
    api GET "/api/model?$(loc_q "$DIR")" | jq -r --arg f "${1:-}" --arg def "$local_def" '
      .data[] | select(.enabled)
      | select($f == "" or ((.providerID+"/"+.id+" "+.name) | test($f; "i")))
      | (.providerID+"/"+.id) as $ref
      | "\($ref)\t\(.name)\(if $ref == $def then "\t*default" else "" end)"' ;;

  agents)
    resolve
    api GET "/api/agent?$(loc_q "$DIR")" | jq -r '.data[] | select(.hidden|not) | "\(.id)\t\(.mode)\t\(.description // "")"' ;;

  sessions)
    resolve
    api GET "/api/session?directory=$(jq -rn --arg d "$DIR" '$d|@uri')&limit=${LIMIT:-20}&order=desc" \
      | jq -r '.data[] | "\(.id)\t\(.time.updated/1000|todate)\t\(.outcome // "-")\t\(.title // "")"' ;;

  new)
    resolve; MODEL_JSON=$(model_json) || exit 1
    create_session ;;

  prompt)
    sid=$1; check_sid "$sid"; resolve
    text=$(read_text "$2"); [ -n "$text" ] || die "empty prompt"
    send_prompt "$sid" "$text" >/dev/null || die "prompt was not accepted by session $sid"
    [ "$NOWAIT" = true ] && { echo "$sid"; exit 0; }
    wait_idle "$sid"; rc=$?
    [ $rc -eq 0 ] && show_result "$sid"
    exit $rc ;;

  run)
    resolve; MODEL_JSON=$(model_json) || exit 1
    text=$(read_text "$1"); [ -n "$text" ] || die "empty prompt"
    sid=$(create_session) || exit 1
    echo "oc: session $sid (dir: $DIR${MODEL:+, model: $MODEL}${VARIANT:+ ($VARIANT)})" >&2
    send_prompt "$sid" "$text" >/dev/null || die "prompt was not accepted by session $sid"
    [ "$NOWAIT" = true ] && { echo "$sid"; exit 0; }
    wait_idle "$sid"; rc=$?
    [ $rc -eq 0 ] && show_result "$sid"
    exit $rc ;;

  wait)
    sid=$1; check_sid "$sid"; resolve
    wait_idle "$sid"; rc=$?
    [ $rc -eq 0 ] && echo "idle"
    exit $rc ;;

  result)
    sid=$1; check_sid "$sid"; resolve
    show_result "$sid" ;;

  messages)
    sid=$1; check_sid "$sid"; resolve
    out=$(api GET "/api/session/$sid/message?order=asc&limit=${LIMIT:-200}") || exit 1
    if [ "$RAW" = true ]; then echo "$out" | jq .; exit 0; fi
    echo "$out" | jq -r '.data[] |
      if .type == "user" then "[user] " + .text
      elif .type == "assistant" then (.content[] |
          if .type == "text" then "[assistant] " + .text
          elif .type == "tool" then "[tool] " + .name + " (" + .state.status + ") " + ((.state.input // {}) | tojson | .[0:160])
          else empty end)
      elif .type == "idle" then "[idle] " + .outcome
      elif .type == "system" then "[system] (" + (.text|length|tostring) + " chars)"
      else "[" + .type + "]" end' ;;

  diff)
    sid=$1; check_sid "$sid"; resolve
    out=$(api GET "/api/session/$sid/diff") || exit 1
    if [ "$(echo "$out" | jq '.data|length')" = "0" ]; then
      # empty diff: either the turn changed nothing (snapshots exist) or no snapshot was ever recorded
      # (e.g. directory not recognised as a git repo when the session started) -> fall back to git status
      if [ "$(api GET "/api/session/$sid/message?type=assistant&limit=200" | jq '[.data[] | select(.snapshot != null)] | length')" != "0" ]; then
        echo "(no file changes in the last turn)"; exit 0
      fi
      sdir=$(api GET "/api/session/$sid" | jq -r '.data.location.directory')
      echo "(no turn snapshot for this session; git working-tree changes in $sdir:)"
      api GET "/api/vcs/status?$(loc_q "$sdir")" | jq -r 'if (.data|length)==0 then "(clean)" else .data[] | "\(.status)\t\(.file)\t+\(.additions) -\(.deletions)" end'
      [ "$PATCH" = true ] && git -C "$sdir" diff 2>/dev/null
      exit 0
    fi
    if [ "$PATCH" = true ]; then echo "$out" | jq -r '.data[] | "### \(.status) \(.file) (+\(.additions) -\(.deletions))\n\(.patch)"'
    else echo "$out" | jq -r '.data[] | "\(.status)\t\(.file)\t+\(.additions) -\(.deletions)"'; fi ;;

  permissions)
    sid=$1; check_sid "$sid"; resolve
    api GET "/api/session/$sid/permission" | jq -r 'if (.data|length)==0 then "(none pending)" else .data[] | "\(.id)\t\(.action)\t\(.resources|join(", "))" end' ;;

  reply)
    sid=$1; pid=$2; dec=$3; msg=$4; check_sid "$sid"; resolve
    case "$dec" in once|always|reject) ;; *) die "decision must be once|always|reject" ;; esac
    api POST "/api/session/$sid/permission/$pid/reply" "$(jq -cn --arg d "$dec" --arg m "$msg" '{decision:$d} + (if $m != "" then {message:$m} else {} end)')" >/dev/null && echo "replied $dec" ;;

  interrupt)
    sid=$1; check_sid "$sid"; resolve
    api POST "/api/session/$sid/interrupt" | jq -c . ;;

  api)
    resolve
    [ -n "$1" ] && [ -n "$2" ] || die "usage: oc.sh api METHOD PATH [JSON]"
    out=$(api "$1" "$2" "$3"); rc=$?
    echo "$out" | jq . 2>/dev/null || echo "$out"
    exit $rc ;;

  *) die "unknown command: $cmd (see oc.sh --help)" ;;
esac
