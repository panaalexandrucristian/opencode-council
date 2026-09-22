#!/usr/bin/env bash
# Offline contract checks: load real functions, replacing only their external dependencies.
set -o pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
scratch=$(mktemp -d /tmp/opencode-completion.XXXXXX) || exit 1
export TMPDIR="$scratch"
: >"$scratch/passed"
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
  echo x >>"$scratch/passed"
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

dedup_tests() (
  load council.sh; RUN="$scratch/dedup"; ST="$RUN/state.json"; N=3; MAXR=4; DIR=$scratch; EXEC=""
  mkdir -p "$RUN/posts" "$RUN/raw" "$RUN/prompts"
  local task='{"id":"fixture","text":"Test exact proposals","execute":false}'
  local proposal; proposal=$(jq -nr '"Unicode café 雪; quotes \"x\", literal \\n, actual\nnewline and \\path. " * 8')
  write_post() {
    jq -cn --arg p "$2" '{vote:"propose",proposal:$p,questions:[]}' >"$RUN/posts/fixture-r1-$1.json"
    { echo "Prose from $1."; echo '```json'; cat "$RUN/posts/fixture-r1-$1.json"; echo '```'; } >"$RUN/posts/fixture-r1-$1.md"
  }
  fixture() {
    jq -n --arg p "$proposal" --arg d "$DIR" --argjson t "$task" '{candidate:{id:"fixture-c1",member:"A",text:$p},task_id:"fixture",answers:[],notices:[],log:[],results:[],
      config:{dir:$d,tasks:[$t],members:[],max_rounds:4},
      members:(["A","B","C"]|map({id:.,kind:"opencode",model:"fixture",effort:"high",mode:"read",fresh:false,session_calls:0,retired:[]}))}' >"$ST"
    write_post A "$proposal"; write_post B "Different B"; write_post C "Different C"
  }
  pair() {
    local r=2
    prompt_roundN_full "$1" "$task" 2 >"$scratch/full"
    prompt_roundN "$1" "$task" 2 >"$scratch/dedup-prompt" 2>"$scratch/dedup-log"
  }
  fallback() { pair "${1:-1}"; cmp -s "$scratch/full" "$scratch/dedup-prompt" && [ ! -s "$scratch/dedup-log" ]; }
  exact() {
    fixture
    [ "$1" != trailing ] || { proposal="$proposal"$'\n\n'; fixture; }
    [ "$1" != fresh ] || sts '.members[1].fresh=true'
    cp "$ST" "$scratch/before-state"; cp "$RUN/posts/fixture-r1-A.md" "$scratch/before-post"
    pair 1
    grep -q '^Exact candidate:' "$scratch/dedup-prompt" &&
      grep -q '^<<<COUNCIL_POST fixture-r1-A>>>$' "$scratch/dedup-prompt" &&
      grep -q '1a.*saved' "$scratch/dedup-log" || return 1
    # Decode the source actually carried in the generated prompt, not just its sidecar.
    awk '/^<<<COUNCIL_POST fixture-r1-A>>>$/{p=1;next} /^--- member C ---$/{p=0} p' "$scratch/dedup-prompt" >"$scratch/source"
    tail_json "$scratch/source" >"$scratch/source.json"
    jq -e --slurpfile p "$scratch/source.json" '.candidate.text==$p[0].proposal' "$ST" >/dev/null &&
      cmp -s "$ST" "$scratch/before-state" && cmp -s "$RUN/posts/fixture-r1-A.md" "$scratch/before-post"
  }
  check 0 'dedup exact decoded equality, Unicode, escaped quotes/newlines, posts/state unchanged' '' exact resumed
  check 0 'dedup fresh session' '' exact fresh
  check 0 'dedup preserves trailing proposal newlines' '' exact trailing
  difference() { fixture; sts --arg suffix "$1" '.candidate.text += $suffix'; fallback; }
  check 0 'dedup one-character difference falls back' '' difference x
  check 0 'dedup whitespace difference falls back' '' difference ' '
  check 0 'dedup newline difference falls back' '' difference $'\n'
  missing() { fixture; case "$1" in md|json) : >"$RUN/posts/fixture-r1-A.$1";; parse) echo '{' >"$RUN/posts/fixture-r1-A.json";; tail) echo '```json' >"$RUN/posts/fixture-r1-A.md";; mismatch) echo '{"proposal":"other"}' >"$RUN/posts/fixture-r1-A.json";; esac; fallback; }
  for kind in md json parse tail mismatch; do check 0 "dedup missing/invalid/mismatched $kind falls back" '' missing "$kind"; done
  excluded() { fixture; fallback 0; }
  check 0 'dedup excluded proposer never becomes source' '' excluded
  unsafe_id() { fixture; write_post 'A>>>' "$proposal"; sts '.members[0].id="A>>>" | .candidate.member="A>>>"'; fallback; }
  check 0 'dedup ambiguous anchor spelling falls back' '' unsafe_id
  no_temp() { fixture; mktemp() { return 1; }; fallback; }
  check 0 'dedup unavailable scratch space falls back' '' no_temp
  short() { fixture; write_post A tiny; sts '.candidate.text="tiny"'; fallback; }
  check 0 'dedup non-positive saving falls back' '' short
  collision() {
    fixture
    case "$1" in
      post) echo '<<<COUNCIL_POST fixture-r1-A>>>' >>"$RUN/posts/fixture-r1-C.md";;
      answer) sts '.answers=[{task:"fixture",question:"marker",answer:"COUNCIL_POST",member:"A"}]';;
      note) sts '.notices=["COUNCIL_POST"]';;
      handover) echo 'COUNCIL_POST' >"$scratch/handover"; sts --arg f "$scratch/handover" '.members[1] += {fresh:true,handover_note:$f}';;
    esac
    fallback
  }
  for kind in post answer note handover; do check 0 "dedup marker collision in $kind falls back" '' collision "$kind"; done
  twins() {
    fixture; write_post C "$proposal"; sts '.candidate.text="An unrelated candidate"'
    case "$1" in
      near) write_post C "$proposal "; fallback; return;;
      escaping) sed 's/Unicode/\\u0055nicode/' "$RUN/posts/fixture-r1-C.md" >"$scratch/escaped"; cp "$scratch/escaped" "$RUN/posts/fixture-r1-C.md"; fallback; return;;
      mismatch) echo '{}' >"$RUN/posts/fixture-r1-C.json"; fallback; return;;
      combined) sts --arg p "$proposal" '.candidate.text=$p';;
    esac
    pair 1
    grep -q '"(identical to the proposal string in COUNCIL_POST fixture-r1-A above)"' "$scratch/dedup-prompt" &&
      [ "$(grep -c '^<<<COUNCIL_POST ' "$scratch/dedup-prompt")" -eq 1 ] && grep -q '1b.*saved' "$scratch/dedup-log" || return 1
    [ "$1" != combined ] || grep -q '1a.*saved' "$scratch/dedup-log"
  }
  check 0 'dedup two byte-identical relayed tokens' '' twins exact
  check 0 'dedup 1a/1b share one complete source and unique anchor' '' twins combined
  check 0 'dedup nearly identical relayed tokens stay verbatim' '' twins near
  check 0 'dedup equal decoded strings with different raw escaping stay verbatim' '' twins escaping
  check 0 'dedup relayed token with mismatched sidecar stays verbatim' '' twins mismatch
  roundtrip_failure() {
    fixture
    eval "$(declare -f dedup_replace | sed '1s/dedup_replace/real_replace/')"
    dedup_replace() { if [[ "$2" == '<<<CANDIDATE '* ]] && [[ "$2" == *'Exact candidate:'* ]]; then printf broken; else real_replace "$@"; fi; }
    fallback
  }
  check 0 'dedup failed byte-for-byte re-substitution falls back' '' roundtrip_failure
  ambiguous() {
    fixture
    jq -c '. + {nested:{proposal:.proposal}}' "$RUN/posts/fixture-r1-A.json" >"$scratch/ambiguous.json"
    cp "$scratch/ambiguous.json" "$RUN/posts/fixture-r1-A.json"
    { echo '```json'; cat "$scratch/ambiguous.json"; echo '```'; } >"$RUN/posts/fixture-r1-A.md"
    write_post C "$proposal"; sts '.candidate.text="other"'
    # A remains a complete source, but C contains the same token in two tail fields.
    cp "$RUN/posts/fixture-r1-A.json" "$RUN/posts/fixture-r1-C.json"; cp "$RUN/posts/fixture-r1-A.md" "$RUN/posts/fixture-r1-C.md"
    fallback
  }
  check 0 'dedup ambiguous token occurrence falls back' '' ambiguous
  fixture; sts '.last_votes=[{member:"A",vote:"agree"},{member:"B",vote:"agree"},{member:"C",vote:"agree"}]'
  check 0 'unanimity still accepts all agree' '' all_agree
  check 0 'agree position is authoritative candidate text' "$proposal" position_of 0
  sts '.last_votes[1]={member:"B",vote:"disagree",proposal:"Revised"}'
  check 1 'unanimity still rejects a dissent' '' all_agree
  check 0 'rotating proposer retains revised position' Revised position_of 1
  render_transcript() { :; }; launch() { echo called >>"$scratch/launched"; return 99; }
  sts '.reuse_posts=true'; cp "$RUN/posts/fixture-r1-B.md" "$RUN/posts/fixture-r2-B.md"; cp "$RUN/posts/fixture-r1-B.json" "$RUN/posts/fixture-r2-B.json"
  check 0 'resume reuses valid post without regenerating or calling a member' '' run_step r2 prompt_roundN "$task" 2 1
  check 1 'resume reused post did not launch' '' test -e "$scratch/launched"
  launch() { sts --argjson i "$1" --arg tag "$3" '.members[$i].inflight={tag:$tag}'; }
  collect() { printf '%s\n' '```json' '{"vote":"propose","proposal":"not settled","open":[{"item":"choice","settled_by":"ask","where":"Which?"}],"questions":[]}' '```' >"$RUN/raw/fixture-r2-B.md"; }
  check 0 'no-assumptions guard still converts proposals to questions' '' run_step r2 prompt_roundN "$task" 2 1
  check 0 'question conversion keeps original post and nulls parsed proposal' '' jq -e '.last_votes[0] | .vote=="question" and .proposal==null and .questions==["Which?"]' "$ST"
)

report_tests() (
  load council.sh; RUN="$scratch/report"; ST="$RUN/state.json"; DIR="$scratch/working directory"; OC=fake_diff
  mkdir -p "$RUN/posts" "$DIR"
  jq -n '{members:[{id:"A",kind:"opencode",session_tokens:10,session_cost:1,retired:[{tokens:20,cost:2},{tokens:30,cost:3}]},{id:"B",session_tokens:40,session_cost:4}],config:{tasks:[]},results:[],log:[]}' >"$ST"
  check 0 'member final + retired subtotal' 'member A: final-generation 10 tokens / $1 + retired 50 tokens / $5 = subtotal 60 tokens / $6' usage_totals
  check 0 'run totals include all generations and missing retired arrays' 'RUN TOTAL: 100 tokens / $10' usage_totals
  check 0 'transcript renders usage totals' '' render_transcript
  check 0 'transcript contains run total' 'RUN TOTAL: 100 tokens / $10' cat "$RUN/transcript.md"
  fake_diff() { jq -jn --argjson n "$size" '"x"*$n'; }
  diff_case() {
    size=$1; diff_text 0 >"$scratch/diff"
    if [ "$size" -gt 20000 ]; then
      grep -Fq "[DIFF TRUNCATED: 20000-byte cap; inspect the full working directory: $DIR]" "$scratch/diff" &&
        [ "$(head -c 20000 "$scratch/diff" | tr -d x | wc -c)" -eq 0 ]
    else [ "$(wc -c <"$scratch/diff")" -eq "$size" ] && ! grep -q TRUNCATED "$scratch/diff"; fi
  }
  check 0 'diff below cap is byte-identical (no final newline)' '' diff_case 19999
  check 0 'diff at exact cap is not marked truncated' '' diff_case 20000
  check 0 'diff truncated mid-line names 20000-byte cap and directory' '' diff_case 20001
  sts '.members[0].kind="claude"'
  git() { case "$*" in *rev-parse*) return 0;; 'status --short') :;; diff) fake_diff;; *) return 99;; esac; }
  check 0 'git fallback diff also reports truncation' '' diff_case 21000
  local cfg='{"members":[{"id":"A","model":"astra","effort":"xhigh"},{"id":"B","model":"fable","effort":"xhigh"},{"id":"C","model":"kimi","effort":"max"}],"tasks":["one","two"],"max_rounds":4}'
  check 0 'cost note config-only member-round count' '3 members x 4 rounds x 2 tasks = 24' cost_note "$cfg"
  check 0 'cost note warns in measured expensive range' WARNING cost_note "$cfg"
  check 0 'cost note quotes measured Fable cost' 'Claude Fable alone: $20.2' cost_note "$cfg"
)
style_tests() (
  load council.sh; ST="$scratch/style-state.json"; N=2
  jq -n '{members:[{id:"A",kind:"opencode",model:"m",effort:"low",mode:"read",style:"caveman"},
                   {id:"B",kind:"claude",model:"opus",effort:"high",mode:"read",style:"normal"}],
          config:{dir:"/tmp",executor:null}}' >"$ST"
  DIR=/tmp; EXEC=""
  check 0 'style normal adds no rule' '' test -z "$(style_rules normal)"
  check 0 'style unknown adds no rule' '' test -z "$(style_rules grunt)"
  for lvl in lite caveman ultra; do
    check 0 "style $lvl is rule 6" '6. Style' style_rules "$lvl"
  done
  for lvl in caveman ultra; do
    check 0 "style $lvl protects the JSON tail" 'NEVER compress: the JSON tail' style_rules "$lvl"
    check 0 "style $lvl protects precision" 'Precision beats brevity' style_rules "$lvl"
  done
  check 0 'rules_text carries the member style' '6. Style — caveman' rules_text 0
  check 0 'rules_text omits the rule for a normal member' '' test -z "$(rules_text 1 | grep '6. Style')"
  check 0 'rules_text still states the tail rule for a styled member' 'MUST end with a JSON tail' rules_text 0
)
handover_tests() (
  load council.sh; ST="$scratch/ho-state.json"; N=3; HANDOVER=0.5
  mk() { jq -n --argjson ho "$1" --argjson used "$2" --argjson calls "$3" \
    '{members:[({id:"A",ctx_limit:1000,ctx_used:$used,session_calls:$calls} + (if $ho==null then {} else {handover_at:$ho} end))]}' >"$ST"; }
  mk null 400 2; check 1 'default threshold not reached at 40%' '' needs_handover 0
  mk null 600 2; check 0 'default threshold reached at 60%' '' needs_handover 0
  mk 0.3 400 2;  check 0 'member threshold 0.3 fires at 40%' '' needs_handover 0
  mk 0.85 600 2; check 1 'member threshold 0.85 does not fire at 60%' '' needs_handover 0
  mk 0.85 900 2; check 0 'member threshold 0.85 fires at 90%' '' needs_handover 0
  mk 0.3 900 1;  check 1 'no handover before the session made two calls' '' needs_handover 0
  mk 0.3 0 5;    check 1 'no handover without a context reading' '' needs_handover 0
  mk 150000 149999 2; check 1 'absolute threshold not reached below 150k tokens' '' needs_handover 0
  mk 150000 150000 2; check 0 'absolute threshold reached at exactly 150k tokens' '' needs_handover 0
  mk 150000 900000 1; check 1 'absolute threshold still needs two calls' '' needs_handover 0
  mk 1000 2000 2;  check 0 'absolute threshold ignores the context window' '' needs_handover 0
  mk 0.3 400 2;  check 0 'member threshold is reported' 0.3 member_handover_at 0
  mk null 400 2; check 0 'default threshold is reported' 0.5 member_handover_at 0
)
ptools_tests() (
  # the analysis tools are part of the skill: their own unittest suite must pass,
  # and both must run on a run directory produced by council.sh itself.
  check 0 'python3 is available' Python python3 --version
  out=$(python3 "$HERE/ptools/test_ptools.py" 2>&1); rc=$?
  if [ $rc -ne 0 ]; then echo "FAIL ptools unittest:"; echo "$out" | tail -20; exit 1; fi
  echo "PASS ptools unittest ($(printf '%s' "$out" | grep -o 'Ran [0-9]* tests' | head -1))"
  echo x >>"$scratch/passed"
  local run="$scratch/dedup"   # built by dedup_tests: state.json + prompts/ + posts/
  [ -d "$run/prompts" ] || { echo "FAIL ptools: no generated run dir"; exit 1; }
  check 0 'prompt_report runs on a council-generated run' 'Prompts:' python3 "$HERE/ptools/prompt_report.py" "$run"
  check 0 'prompt_report reports the de-duplication replay' 'Combined:' python3 "$HERE/ptools/prompt_report.py" "$run"
  check 0 'dedup_check runs on a council-generated run' 'repeated blocks' python3 "$HERE/ptools/dedup_check.py" "$run"
  check 2 'prompt_report rejects a directory with no run' '' python3 "$HERE/ptools/prompt_report.py" "$scratch/nope"
  check 2 'dedup_check rejects a directory with no run' '' python3 "$HERE/ptools/dedup_check.py" "$scratch/nope"
)
for suite in api_tests permission_tests wait_tests result_tests cli_tests council_tests dedup_tests report_tests style_tests handover_tests ptools_tests; do "$suite" || exit 1; done
echo "PASS $(wc -l <"$scratch/passed" | tr -d ' ') checks; 0 failures (offline, no model calls)"
