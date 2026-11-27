#!/usr/bin/env bash
# Opt-in integration tests against the real OpenCode service and model.
set -eo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
OC="$HERE/oc.sh"
MODEL=openai/gpt-6.1-terra
VARIANT=medium
HOST_SESSION_ID=${OPENCODE_SESSION_ID:-ses_conncil_test_host}
export COUNCIL_TELEMETRY=0
unset CLAUDECODE OPENCODE_SESSION_ID OPENCODE_AUTO

cleanup() {
  touch "$PROJECT/release"
  local sid
  if [ -f "$PROJECT/run/state.json" ]; then
    while IFS= read -r sid; do
      IDS+=("$sid")
    done < <(jq -r '(.members[]?.session, .members[]?.retired[]?.session, .map_prepasses[]?.session)
      | strings | select(startswith("ses_"))' "$PROJECT/run/state.json")
  fi
  for sid in "${IDS[@]}"; do
    "$OC" interrupt "$sid" >/dev/null 2>&1 || true
    "$OC" api PATCH "/api/session/$sid" '{"archived":true}' >/dev/null || true
  done
  rm -r -- "$PROJECT"
}

fail() { echo "FAIL: $*" >&2; exit 1; }

new_session() {
  local sid
  sid=$("$OC" new --dir "$PROJECT" --model "$MODEL" --variant "$VARIANT" --title "conncil live test: $1" --ask)
  [[ "$sid" == ses_* ]] || fail "invalid fixture session ID: $sid"
  IDS+=("$sid")
  SID=$sid
}

finished_session() {
  new_session "$1"
  "$OC" prompt "$SID" 'Reply with exactly the word READY and use no tools.' --ask --timeout 90 >/dev/null
}

busy_session() {
  local name=$1 deadline=$((SECONDS+60))
  new_session "$name"
  "$OC" prompt "$SID" --no-wait --ask "Use the shell tool to run this exact command in the current project directory: touch $name.ready; while [ ! -f release ]; do sleep 0.2; done
After the command exits, reply READY." >/dev/null
  until [ -f "$PROJECT/$name.ready" ]; do
    [ "$SECONDS" -lt "$deadline" ] || fail "model did not start the controlled shell command: $SID"
    sleep 0.2
  done
  "$OC" api GET /api/session/active | jq -e --arg sid "$SID" '.data | has($sid)' >/dev/null \
    || fail "fixture session is not busy: $SID"
}

blocked_session() {
  local deadline=$((SECONDS+60))
  new_session permission-blocker
  printf 'test fixture\n' >"$PROJECT/permission-probe.txt"
  "$OC" api PATCH "/api/session/$SID" '{"permissions":[{"action":"read","resource":"*","effect":"ask"}]}'
  "$OC" prompt "$SID" --no-wait --ask 'Use the read tool to read permission-probe.txt, then reply READY.' >/dev/null
  until "$OC" api GET "/api/session/$SID/permission" | jq -e '.data | length > 0' >/dev/null; do
    [ "$SECONDS" -lt "$deadline" ] || fail "model did not request permission: $SID"
    sleep 0.2
  done
}

assert_wait() {
  local want_rc=$1 want_output=$2 actual rc=0
  shift 2
  actual=$("$OC" wait "$@") || rc=$?
  if [ "$rc" -ne "$want_rc" ] || [ "$actual" != "$want_output" ]; then
    printf 'Expected wait exit %s and stdout:\n%s\nGot exit %s and stdout:\n%s\n' "$want_rc" "$want_output" "$rc" "$actual" >&2
    exit 1
  fi
}

idle_sessions() {
  local first second
  finished_session first; first=$SID
  finished_session second; second=$SID
  assert_wait 0 idle "$first" --ask
  ( TIMEFORMAT='Wait for both idle sessions: %R s'
    time assert_wait 0 "$(printf '%s\tidle\n%s\tidle' "$first" "$second")" "$first" "$second" --ask --timeout 5
  )
}

common_deadline() {
  local first second start
  busy_session first-busy; first=$SID
  busy_session second-busy; second=$SID
  start=$SECONDS
  assert_wait 2 "$(printf '%s\ttimeout\n%s\ttimeout' "$first" "$second")" "$first" "$second" --ask --timeout 5
  [ "$((SECONDS-start))" -lt 8 ] || fail 'wait used more than one timeout budget'
}

idle_after_deadline() {
  local first second
  finished_session already-done; second=$SID
  busy_session deadline-blocker; first=$SID
  assert_wait 2 "$(printf '%s\ttimeout\n%s\tidle' "$first" "$second")" "$first" "$second" --ask --timeout 2
}

blocked_over_timeout() {
  local first second third
  finished_session finished; third=$SID
  blocked_session; second=$SID
  busy_session timeout-blocker; first=$SID
  assert_wait 3 "$(printf '%s\ttimeout\n%s\tblocked\n%s\tidle' "$first" "$second" "$third")" \
    "$first" "$second" "$third" --ask --timeout 2
}

error_over_blocked() {
  local first third missing=ses_conncil_test_missing
  finished_session finished; third=$SID
  blocked_session; first=$SID
  assert_wait 1 "$(printf '%s\tblocked\n%s\terror\n%s\tidle' "$first" "$missing" "$third")" \
    "$first" "$missing" "$third" --ask --timeout 5
}

start_council() {
  local map=$1
  mkdir "$PROJECT/source"
  printf '# Fixture\n\nThe application is called Pebble.\n' >"$PROJECT/source/README.md"
  jq -n --arg dir "$PROJECT/source" --arg model "$MODEL" --arg effort "$VARIANT" --argjson map "$map" '
    {dir:$dir, executor:null, max_rounds:2, timeout_s:120, style:"normal", map_code:$map,
     map_prepass:{kind:"opencode", model:$model, effort:$effort},
     tasks:["Read README.md and report the application name. Do not edit files."],
     members:["A","B"] | map({id:., kind:"opencode", model:$model, effort:$effort, mode:"read"})}
  ' >"$PROJECT/council.json"
  council_rc=0
  OPENCODE_SESSION_ID="$HOST_SESSION_ID" "$HERE/council.sh" start --config "$PROJECT/council.json" --run-dir "$PROJECT/run" \
    >"$PROJECT/council.log" 2>&1 || council_rc=$?
}

council_members() {
  local sid
  start_council false
  if ! jq -e 'all(.members[]; (.session // "" | startswith("ses_")) and .calls >= 1)' "$PROJECT/run/state.json" >/dev/null; then
    cat "$PROJECT/council.log" >&2
    fail "council members did not complete a turn (council exit $council_rc)"
  fi
  for sid in $(jq -r '.members[].session' "$PROJECT/run/state.json"); do
    "$OC" result "$sid" >/dev/null || fail "council member did not produce a successful turn: $sid"
  done
}

council_mapper() {
  local response
  start_council true
  response=$(jq -er '.map_prepasses.t1 | select(.session // "" | startswith("ses_")) | .artifacts.response // empty' "$PROJECT/run/state.json") || response=""
  if [ "$council_rc" -ne 4 ] || [ -z "$response" ] || [ ! -s "$response" ]; then
    cat "$PROJECT/council.log" >&2
    fail "council mapper did not produce a response from a session (council exit $council_rc)"
  fi
}

run_case() (
  PROJECT=$(mktemp -d "${TMPDIR:-/tmp}/conncil-live.XXXXXX")
  PROJECT=$(cd "$PROJECT" && pwd -P)
  IDS=()
  trap cleanup EXIT
  "$1"
)

[ "$#" -gt 0 ] || set -- idle_sessions common_deadline idle_after_deadline blocked_over_timeout error_over_blocked council_members council_mapper
for test_case in "$@"; do
  case "$test_case" in
    idle_sessions|common_deadline|idle_after_deadline|blocked_over_timeout|error_over_blocked|council_members|council_mapper) run_case "$test_case" ;;
    *) fail "unknown test: $test_case" ;;
  esac
  printf 'PASS %s\n' "$test_case"
done
