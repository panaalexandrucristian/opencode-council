#!/usr/bin/env bash
# Offline contract checks: load real functions, replacing only their external dependencies.
set -o pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
scratch=$(mktemp -d /tmp/opencode-completion.XXXXXX) || exit 1
export TMPDIR="$scratch"
trap '[ "$BASH_SUBSHELL" -ne 0 ] || rm -rf "$scratch"' EXIT
curl() { echo "unexpected network call" >&2; return 99; }
load() { eval "$(sed '/^# .* commands /,$d' "$HERE/$1")"; }
check() {
  local want=$1 name=$2 text=$3 got; shift 3
  ("$@") >"$scratch/out" 2>"$scratch/err"; got=$?
  if [ "$got" -ne "$want" ] || [[ "$(cat "$scratch/out")" != *"$text"* ]]; then
    echo "FAIL $name: exit $got, wanted $want; expected text: $text"; cat "$scratch/out" "$scratch/err"; exit 1
  fi
  echo "PASS $name"
}
api_tests() (
  load oc.sh
  curl() { local out; while [ $# -gt 0 ]; do if [ "$1" = -o ]; then out=$2; shift 2; else shift; fi; done; printf '{}' >"$out"; printf '%s' "$http"; return "$transport"; }
  for method in GET POST; do for spec in '200 28 1' '200 0 0' '503 0 1'; do
    set -- $spec; http=$1; transport=$2; body=""; [ "$method" = POST ] && body='{}'
    check "$3" "api $method HTTP=$http curl=$transport" '{}' api "$method" /fixture "$body"
  done; done
  http=200; transport=0
  (mktemp() { return 1; }; check 1 'api mktemp failure' '' api GET /fixture) || exit 1
  (cat() { return 1; }; check 1 'api response-read failure' '' api GET /fixture) || exit 1
)
permission_tests() (
  load oc.sh; AUTO=true; reply_rc=1
  permission_body='{"data":[{"id":"first","action":"read","resources":[]},{"id":"second","action":"read","resources":[]}]}'
  api() { if [ "$1" = GET ]; then printf '%s' "$permission_body"; else echo "$2" >>"$scratch/replies"; case "$2" in *first*) return "$reply_rc";; esac; fi; }
  check 1 'permissions first reply fails' '' handle_permissions ses_fixture
  check 1 'permissions second reply not attempted' '' grep -q second "$scratch/replies"
  reply_rc=0; check 0 'permissions approved' '' handle_permissions ses_fixture
  AUTO=false; check 3 'permissions ask' '' handle_permissions ses_fixture
  AUTO=true; permission_body='{"data":[]}'; check 0 'permissions none' '' handle_permissions ses_fixture
  permission_body='{"data":[{"id":"bad","resources":null}]}'; check 1 'permissions formatting failure' '' handle_permissions ses_fixture
)
wait_tests() (
  load oc.sh; WAIT_TIMEOUT=8
  date() { cat "$scratch/clock"; }; sleep() { local n; read -r n <"$scratch/clock"; echo "$((n+1))" >"$scratch/clock"; }
  handle_permissions() { return 0; }; last_type() { echo "$last"; return "$lookup"; }
  curl() { printf x >>"$scratch/polls"; printf 200; if [ "$mode" = slice ] && [ "$(wc -c <"$scratch/polls")" -eq 1 ]; then sleep 1; return 28; fi; return "$transport"; }
  run_wait() { echo 0 >"$scratch/clock"; : >"$scratch/polls"; wait_idle ses_fixture; }
  for spec in 'missing 0 none 0 2 8' 'lookup 0 idle 1 1 1' 'idle 0 idle 0 0 1' 'slice 0 idle 0 0 2' 'transport 7 idle 0 1 1'; do
    set -- $spec; mode=$1; transport=$2; last=$3; lookup=$4
    check "$5" "wait $mode" '' run_wait
    check 0 "wait $mode observed $6 polls" '' test "$(wc -c <"$scratch/polls")" -eq "$6"
  done
)
result_tests() (
  load oc.sh; api_rc=0; api() { printf '%s' "$body"; return "$api_rc"; }
  response() { jq -cn --arg o "$1" --arg t "$2" --argjson e "${3:-null}" '{data:[(if $o=="missing" then empty else {type:"idle",outcome:$o} end),{type:"assistant",content:[{type:"text",text:$t}],error:$e},{type:"user"}]}'; }
  for outcome in succeeded failed interrupted unknown missing; do
    body=$(response "$outcome" reply); want=1; text=$'reply\n\n[outcome] '; [ "$outcome" = missing ] && outcome=unavailable
    text="$text$outcome"; if [ "$outcome" = succeeded ]; then want=0; text=reply; fi
    check "$want" "result $outcome preserves text" "$text" show_result ses_fixture
  done
  body=$(response succeeded ''); check 0 'result empty text' '(no text output in this turn)' show_result ses_fixture
  for error in '{"message":"problem"}' '{}'; do body=$(response succeeded reply "$error"); check 1 "result assistant error $error" $'reply\n\n[error] ' show_result ses_fixture; done
  for body in '' '{' '{}' '[]' '{"data":{}}'; do check 1 "result malformed [$body]" '' show_result ses_fixture; done
  body='{"data":[]}'; check 1 'result empty messages' '[outcome] unavailable' show_result ses_fixture
  body="$(response succeeded reply)$(response succeeded reply)"; check 1 'result multiple objects' '' show_result ses_fixture
  body=$(response succeeded reply); api_rc=1; check 1 'result fetch failure preserves text' reply show_result ses_fixture
)
cli_tests() (
  load oc.sh; resolve() { :; }; send_prompt() { :; }; create_session() { echo ses_fixture; }
  wait_idle() { return "$wait_rc"; }; show_result() { echo reply; return "$result_rc"; }
  cli() { local cmd=$1; shift; if [ "$cmd" = prompt ]; then set -- "$cmd" ses_fixture payload "$@"; else set -- "$cmd" payload "$@"; fi; eval "$(sed -n '/^# .* commands /,$p' "$HERE/oc.sh")"; }
  for cmd in prompt run; do
    for spec in '0 1 1' '2 0 2' '3 0 3' '0 0 0'; do set -- $spec; wait_rc=$1; result_rc=$2; check "$3" "$cmd wait=$wait_rc result=$result_rc" '' cli "$cmd"; done
    wait_rc=3; result_rc=1; check 0 "$cmd no-wait" ses_fixture cli "$cmd" --no-wait
  done
)
council_tests() (
  load council.sh; RUN="$scratch/council"; ST="$RUN/state.json"; N=1; OC=fake_oc
  mkdir -p "$RUN/raw" "$RUN/posts" "$RUN/prompts"
  jq -n '{members:[{id:"A",kind:"opencode",session:"ses_fixture",fresh:false,calls:0,session_calls:0}],last_votes:[]}' >"$ST"
  render_transcript() { :; }; fixture_prompt() { echo fixture; }; wait_rc=0; result_rc=1
  post=$'```json\n{"vote":"agree","questions":[]}\n```'; output="$post"$'\n[outcome] failed'
  fake_oc() { case "$1" in wait) return "$wait_rc";; result) printf '%s\n' "$output"; return "$result_rc";; api) if [[ "$3" == *'/message?'* ]]; then echo '{"data":[]}'; else echo '{"data":{"tokens":{"input":1}}}'; fi;; prompt|interrupt) return 0;; *) return 99;; esac; }
  check 1 'council rejects failed result with agree tail' '' run_step r2 fixture_prompt '{"id":"fixture"}' 2
  check 0 'council retry gate accepts no votes' '' jq -e '.last_votes==[] and .members[0].calls==2' "$ST"
  check 0 'council retains failed raw output' '[outcome] failed' cat "$RUN/raw/fixture-r2-A.md"
  result_rc=0; output=$'[error] literal assistant text\n'"$post"
  check 0 'council accepts successful literal error text' '' run_step r2 fixture_prompt '{"id":"fixture"}' 2
  check 0 'council accepted the successful vote' '' jq -e '.last_votes[0].vote=="agree"' "$ST"
  result_rc=1; for wait_rc in 2 3; do sts '.members[0].inflight={tag:"wait"}'; check "$wait_rc" "council preserves wait=$wait_rc" '' collect 0; done
)
for suite in api_tests permission_tests wait_tests result_tests cli_tests council_tests; do "$suite" || exit 1; done
