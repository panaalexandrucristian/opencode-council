#!/usr/bin/env bash
# Offline contract checks: load real functions, replacing only their external dependencies.
set -o pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
scratch=$(mktemp -d /tmp/opencode-completion.XXXXXX) || exit 1
export TMPDIR="$scratch"
export PYTHONDONTWRITEBYTECODE=1  # every python3 invocation below must not dirty tracked ptools/__pycache__
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
codemap_version_tests() (
  load council.sh; RUN="$scratch/cmver"; ST="$RUN/state.json"; DIR="$scratch"; N=1
  mkdir -p "$RUN"
  jq -n '{codemap_pending:null}' >"$ST"
  codemap_check_version
  check 0 'absent codemap_version disables the map' '' test "$CODEMAP" = 0
  sts '.codemap_version=1'
  codemap_check_version
  check 0 'codemap_version=1 enables the map' '' test "$CODEMAP" = 1
  sts '.codemap_version=2 | .codemap_pending=null'
  codemap_check_version; local rc=$?
  check 0 'unsupported version without a pending attempt does not checkpoint' '' test "$rc" -eq 0
  check 0 'unsupported version leaves the map disabled' '' test "$CODEMAP" = 0
  sts '.codemap_pending={task:"t1",step:"r1"}'
  check 2 'unsupported version with a pending map-backed attempt checkpoints via exit 2' '' codemap_check_version
)
codemap_prompt_tests() (
  load council.sh; RUN="$scratch/cmprompt"; ST="$RUN/state.json"; N=2; MAXR=4
  mkdir -p "$RUN/posts" "$RUN/codemap"
  local task='{"id":"t1","text":"Do X","execute":false}'
  jq -n --argjson t "$task" '{candidate:{id:"t1-c1",member:"A",text:"CAND"},answers:[],config:{tasks:[$t],max_rounds:4},members:[{id:"A"},{id:"B"}]}' >"$ST"
  not_contains() { ! grep -q "$2" "$1"; }
  CODEMAP_STEP=0
  prompt_round1 0 "$task" >"$scratch/r1-disabled"
  check 0 'round1 prompt has no code map text when the step is disabled' '' not_contains "$scratch/r1-disabled" 'CODE MAP'
  check 0 'round1 prompt has no code_reads schema text when disabled' '' not_contains "$scratch/r1-disabled" 'code_reads'
  check 0 'round1 prompt starts with the task header when disabled (no stray leading blank line)' '=== TASK t1' head -1 "$scratch/r1-disabled"
  printf '=== CODE MAP ===\nfixture locator\n=== END CODE MAP ===\n' >"$RUN/codemap/.locator-t1-r1.md"
  CODEMAP_STEP=1
  # The generator NEVER emits the locator; the orchestrator appends it and records the span. This
  # is the real delivery path, so the test exercises it exactly as run_step does.
  prompt_round1 0 "$task" >"$scratch/r1-gen-only"
  check 0 'the round1 generator itself emits no code map bytes' '' not_contains "$scratch/r1-gen-only" 'CODE MAP'
  check 0 'round1 prompt carries the code_reads schema when enabled' 'code_reads is optional' cat "$scratch/r1-gen-only"
  : >"$scratch/r1-enabled"
  codemap_append_locator "$scratch/r1-enabled" t1 r1 prompt
  prompt_round1 0 "$task" >>"$scratch/r1-enabled"
  check 0 'round1 prompt carries the locator when the step is enabled' 'fixture locator' cat "$scratch/r1-enabled"
  check 0 'the locator is the first thing in the delivered round1 prompt' '=== CODE MAP ===' head -1 "$scratch/r1-enabled"
  check 0 'the recorded span is the real offset and length of the appended block' \
    '0 '"$(wc -c <"$RUN/codemap/.locator-t1-r1.md" | tr -d ' ')" \
    jq -r 'select(.kind=="span")|"\(.offset) \(.length)"' "$RUN/codemap/deliveries.jsonl"
  check 0 'the recorded span really does delimit the locator bytes in the delivered prompt' \
    '=== CODE MAP ===' head -1 "$scratch/r1-enabled"
  # author text that merely CONTAINS the marker is never recorded as a delivery
  : >"$RUN/codemap/deliveries.jsonl"
  { printf 'A member wrote: === CODE MAP === in their post\n'; } >"$scratch/r1-impostor"
  prompt_round1 0 "$task" >>"$scratch/r1-impostor"
  check 0 'marker text inside relayed author prose records no delivery at all' '' \
    test ! -s "$RUN/codemap/deliveries.jsonl"
  printf '=== CODE MAP ===\nfixture locator r2\n=== END CODE MAP ===\n' >"$RUN/codemap/.locator-t1-r2.md"
  CODEMAP_STEP=0
  prompt_roundN_full 0 "$task" 2 >"$scratch/rN-disabled"
  check 0 'roundN prompt has no code map text when disabled' '' not_contains "$scratch/rN-disabled" 'CODE MAP'
  CODEMAP_STEP=1
  : >"$scratch/rN-enabled"
  codemap_append_locator "$scratch/rN-enabled" t1 r2 prompt
  prompt_roundN_full 0 "$task" 2 >>"$scratch/rN-enabled"
  check 0 'roundN prompt carries the locator when enabled' 'fixture locator r2' cat "$scratch/rN-enabled"
)
codemap_pipeline_tests() (
  load council.sh; RUN="$scratch/cmpipe"; ST="$RUN/state.json"; N=1; MAXR=4; EXEC=""
  not_contains() { ! grep -q "$2" "$1"; }
  mkdir -p "$RUN/posts" "$RUN/raw" "$RUN/prompts"
  DIR="$scratch/proj"; mkdir -p "$DIR/src"; printf 'one\ntwo\n' >"$DIR/src/a.py"
  local sha; sha=$(python3 -c "import hashlib;print(hashlib.sha256(open('$DIR/src/a.py','rb').read()).hexdigest())")
  local task='{"id":"t1","text":"Do X","execute":false}'
  reset_state() {
    jq -n --arg d "$DIR" --argjson t "$task" '{codemap_version:1,codemap_pending:null,candidate:null,answers:[],notices:[],log:[],
       results:[],reuse_posts:false,round:1,
       config:{dir:$d,tasks:[$t],max_rounds:4},
       members:[{id:"A",kind:"opencode",model:"m",effort:"h",mode:"read",fresh:false,session_calls:0,retired:[],gen:1}]}' >"$ST"
    codemap_check_version
  }
  reset_state
  render_transcript() { :; }
  local tail; tail=$(jq -cn --arg sha "$sha" '{vote:"propose",proposal:"x",questions:[],code_reads:[{path:"src/a.py",lines:[1,1],observed_sha256:$sha,symbol:"foo",conclusion:"looks fine"}]}')
  launch() { :; }
  collect() { printf '%s\n' 'Prose.' '```json' "$tail" '```' >"$RUN/raw/t1-r1-A.md"; return 0; }
  check 0 'a round-1 deliberation step with code_reads succeeds' '' run_step r1 prompt_round1 "$task" 1
  check 0 'the accepted code_reads report is published into the index' '' jq -e '.entries|length==1' "$RUN/codemap/index.json"
  check 0 'the published report carries the symbol claim' foo jq -r '[.entries[].reports[].symbol]|.[0]' "$RUN/codemap/index.json"
  check 0 'codemap_pending is cleared after a clean publish' 'null' st '.codemap_pending'
  check 0 'the accepted vote itself is unaffected by the optional report' propose jq -r '.last_votes[0].vote' "$ST"

  # malformed code_reads item must never change the vote or retry
  reset_state
  local bad_tail; bad_tail=$(jq -cn '{vote:"agree",questions:[],code_reads:[{path:"src/a.py",bogus_key:1}]}')
  collect() { printf '%s\n' 'Prose.' '```json' "$bad_tail" '```' >"$RUN/raw/t1-r1-A.md"; return 0; }
  check 0 'a malformed code_reads item does not fail the step' '' run_step r1 prompt_round1 "$task" 1
  check 0 'the vote is unaffected by a malformed optional item' agree jq -r '.last_votes[0].vote' "$ST"

  # freshness guard: a guarded source mutated after round-1 publish must fail round 2 (stale) and purge its posts
  reset_state
  collect() { printf '%s\n' 'Prose.' '```json' "$tail" '```' >"$RUN/raw/t1-r1-A.md"; return 0; }
  run_step r1 prompt_round1 "$task" 1 >/dev/null
  sts --arg m A --arg tid t1 '.candidate={id:"t1-c1",member:$m,text:"CAND"}'
  codemap_prepare t1 r2 0   # freeze the guard on today's a.py before it changes
  printf 'one\nCHANGED\n' >"$DIR/src/a.py"
  collect() { printf '%s\n' 'Prose.' '```json' '{"vote":"agree","questions":[]}' '```' >"$RUN/raw/t1-r2-A.md"; return 0; }
  check 1 'a guarded source changing mid-round fails the step (stale)' '' run_step r2 prompt_roundN "$task" 2
  check 0 'the stale round purges its own posts so they are never reused' '' test ! -e "$RUN/posts/t1-r2-A.md"
  check 0 'codemap_pending is cleared after stale handling' 'null' st '.codemap_pending'

  # resume: an already-checkpointed step must be re-verified and its posts purged if the guard changed meanwhile
  reset_state
  # restore the exact bytes the member's observed_sha256 names, so its claim binds to the LIVE
  # version and the round-2 guard actually has an exposed current source to watch
  printf 'one\ntwo\n' >"$DIR/src/a.py"
  collect() { printf '%s\n' 'Prose.' '```json' "$tail" '```' >"$RUN/raw/t1-r1-A.md"; return 0; }
  run_step r1 prompt_round1 "$task" 1 >/dev/null
  sts '.candidate={id:"t1-c1",member:"A",text:"CAND"}'
  codemap_prepare t1 r2 0
  : >"$RUN/posts/t1-r2-A.md"; echo '{"vote":"agree","questions":[]}' >"$RUN/posts/t1-r2-A.json"
  printf 'one\nCHANGED AGAIN\n' >"$DIR/src/a.py"
  codemap_resume_check
  check 0 'resuming re-verifies the guard and purges posts if it changed' '' test ! -e "$RUN/posts/t1-r2-A.md"
  printf 'one\nCHANGED AGAIN\n' >"$DIR/src/a.py"  # unchanged since the previous check
  : >"$RUN/posts/t1-r2-A.md"; echo '{"vote":"agree","questions":[]}' >"$RUN/posts/t1-r2-A.json"
  codemap_prepare t1 r2 0
  codemap_resume_check
  check 0 'resuming keeps posts when the guard is unchanged' '' test -e "$RUN/posts/t1-r2-A.md"

  # handover: original note bytes are preserved on disk; the delivered prompt adds the locator, never mutating the file
  reset_state
  echo "Original note text." >"$scratch/note.md"
  sts --arg n "$scratch/note.md" '.members[0].fresh=true | .members[0].handover_note=$n'
  collect() { printf '%s\n' 'Prose.' '```json' '{"vote":"agree","questions":[]}' '```' >"$RUN/raw/t1-r1-A.md"; return 0; }
  run_step r1 prompt_round1 "$task" 1 >/dev/null
  check 0 'the delivered prompt renders the original handover note bytes' 'Original note text.' cat "$RUN/prompts/t1-r1-A.md"
  check 0 'the delivered prompt embeds the locator inside the handover section' 'CODE MAP' cat "$RUN/prompts/t1-r1-A.md"
  check 0 'the stored handover note file is never mutated (still exactly its original line)' '1 '"$scratch/note.md" wc -l "$scratch/note.md"
  check 0 'the stored handover note file gained no code map bytes on disk' '' not_contains "$scratch/note.md" 'CODE MAP'
)
# Byte-exact baselines for the deliveries the code map must never touch. The digests below were
# recovered from the immutable pre-change commit d62a356 with exactly the fixture built here (the
# only environment-dependent text, the scratch path, is normalised away). They cover the COMPLETE
# execution / ratification / fix prompt as delivered to a FRESH session — i.e. including the
# rules block and the predecessor-handover framing — with the map enabled and disabled.
BASELINE_EXEC=27a50eabb90cb446c025a420020d097459acbe52a036276ec154f9503a9bd71c
BASELINE_RATIFY=5102c70d74878f06e2f9a048d48a8037eaeb128382613b1e68898573d8f9b7e1
BASELINE_FIX=6e9542d66c9b572460123caa3b486e5a841f4a28c96f25d92168ebcfb049c4ac
prompt_baseline_digests() (  # map(0|1) -> "<tag> <sha256>" per non-deliberation delivery
  local map=$1 base="$scratch/baseline-$1"
  load council.sh
  RUN="$base/run"; ST="$RUN/state.json"; N=2; MAXR=4; EXEC=A; DIR="$base/proj"
  mkdir -p "$RUN/posts" "$RUN/raw" "$RUN/prompts" "$RUN/codemap" "$DIR"
  local task='{"id":"t1","text":"Do X","execute":true}'
  jq -n --argjson t "$task" --arg d "$DIR" '{
    codemap_version:1, codemap_pending:null, status:"running", task_id:"t1", task_idx:0, round:1,
    phase:"exec", started:"S", run_dir:"R",
    candidate:{id:"t1-c1",member:"A",text:"CANDIDATE BODY"},
    answers:[{id:"t1/r1/A/q1",task:"t1",member:"A",question:"Q?",answer:"A!"}],
    notices:["a notice"], log:[], results:[{task:"t1",outcome:"consensus",rounds:2,text:"PLAN BODY"}],
    fixes:[{member:"B",reason:"because",proposal:"fix this"}],
    reuse_posts:false, config:{dir:$d,tasks:[$t],max_rounds:4,executor:"A"},
    members:[{id:"A",kind:"claude",model:"m",effort:"high",mode:"edit",fresh:true,handover_note:null,
              gen:1,session_calls:0,retired:[],ctx_used:0,ctx_limit:1000,style:null},
             {id:"B",kind:"opencode",model:"p/m2",effort:"high",mode:"read",fresh:false,handover_note:null,
              gen:1,session_calls:0,retired:[],ctx_used:0,ctx_limit:1000,style:null}]}' >"$ST"
  printf 'Predecessor note body.\n' >"$base/note.md"
  render_transcript() { :; }
  launch() { :; }
  collect() { local i=$1; printf '%s\n' 'Prose.' '```json' '{"vote":"done","report":"r","questions":[]}' '```' >"$RUN/raw/t1-$TAG-$(mid $i).md"; return 0; }
  needs_handover() { return 1; }
  # Really turn the map on: loading council.sh resets CODEMAP to 0, so without this the
  # "map enabled" half of the baseline would be testing the disabled path twice over.
  if [ "$map" = 1 ]; then
    codemap_check_version
    [ "$CODEMAP" = 1 ] || { echo "FAIL baseline fixture: CODEMAP is $CODEMAP, expected 1"; echo x >>"$scratch/failed"; }
    # a locator cache exists for these steps: if any non-deliberation delivery ever consulted it,
    # the digest would change
    for s in exec x1 fix1; do printf '=== CODE MAP ===\nfixture\n=== END CODE MAP ===\n' >"$RUN/codemap/.locator-t1-$s.md"; done
  else
    CODEMAP=0
    [ "$CODEMAP" = 0 ] || { echo "FAIL baseline fixture: CODEMAP is $CODEMAP, expected 0"; echo x >>"$scratch/failed"; }
  fi
  local spec
  for spec in "exec:prompt_exec" "x1:prompt_ratify" "fix1:prompt_fix"; do
    TAG=${spec%%:*}
    sts --arg n "$base/note.md" '.members[0].fresh=true | .members[0].handover_note=$n | .notices=["a notice"] | .reuse_posts=false'
    run_step "$TAG" "${spec#*:}" "$task" 1 0 >/dev/null 2>&1
    printf '%s %s\n' "$TAG" "$(sed "s|$base|@SCRATCH@|g" "$RUN/prompts/t1-$TAG-A.md" | shasum -a 256 | cut -d' ' -f1)"
  done
)
codemap_baseline_tests() (
  local off on
  off=$(prompt_baseline_digests 0); on=$(prompt_baseline_digests 1)
  digest() { awk -v t="$2" '$1==t{print $2}' <<<"$1"; }
  check 0 'execution delivery is byte-identical to the d62a356 baseline (map disabled)' "$BASELINE_EXEC" digest "$off" exec
  check 0 'ratification delivery is byte-identical to the d62a356 baseline (map disabled)' "$BASELINE_RATIFY" digest "$off" x1
  check 0 'fix delivery is byte-identical to the d62a356 baseline (map disabled)' "$BASELINE_FIX" digest "$off" fix1
  check 0 'execution delivery is byte-identical to the baseline with the map ENABLED' "$BASELINE_EXEC" digest "$on" exec
  check 0 'ratification delivery is byte-identical to the baseline with the map ENABLED' "$BASELINE_RATIFY" digest "$on" x1
  check 0 'fix delivery is byte-identical to the baseline with the map ENABLED' "$BASELINE_FIX" digest "$on" fix1
  check 0 'enabling the map changes no non-deliberation delivery at all' '' test "$off" = "$on"
  # the same fixture, regenerated from the immutable commit itself, must produce those digests
  if git -C "$HERE/.." rev-parse --verify -q d62a356 >/dev/null 2>&1; then
    git -C "$HERE/.." show d62a356:scripts/council.sh >"$scratch/d62a356-council.sh"
    cp "$HERE/oc.sh" "$scratch/oc.sh"; ln -sfn "$HERE/ptools" "$scratch/ptools"
    local recovered
    recovered=$( HERE_OVERRIDE=1; load() { eval "$(sed '/^# .* commands /,$d' "$scratch/d62a356-council.sh")"; }
                 prompt_baseline_digests 0 )
    check 0 'the embedded digests really are the pre-change d62a356 bytes' "$BASELINE_EXEC" digest "$recovered" exec
  else
    echo "PASS (skipped) d62a356 is not present in this clone; the embedded digests stand alone"
    echo x >>"$scratch/passed"
  fi
)
codemap_engine_tests() (
  load council.sh; RUN="$scratch/cmeng"; ST="$RUN/state.json"; N=1; MAXR=4; EXEC=""
  mkdir -p "$RUN/posts" "$RUN/raw" "$RUN/prompts"
  DIR="$scratch/eng"; mkdir -p "$DIR/src"; printf 'one\ntwo\n' >"$DIR/src/a.py"
  local sha; sha=$(python3 -c "import hashlib;print(hashlib.sha256(open('$DIR/src/a.py','rb').read()).hexdigest())")
  local task='{"id":"t1","text":"Do X","execute":false}'
  reset_state() {
    jq -n --arg d "$DIR" --argjson t "$task" '{codemap_version:1,codemap_pending:null,candidate:null,answers:[],notices:[],log:[],
       results:[],reuse_posts:false,round:1,status:"running",
       config:{dir:$d,tasks:[$t],max_rounds:4},
       members:[{id:"A",kind:"opencode",model:"m",effort:"h",mode:"read",fresh:false,session_calls:0,retired:[],gen:1}]}' >"$ST"
    codemap_check_version
  }
  render_transcript() { :; }
  launch() { :; }
  local tail; tail=$(jq -cn --arg sha "$sha" '{vote:"propose",proposal:"x",questions:[],code_reads:[{path:"src/a.py",lines:[1,1],observed_sha256:$sha,symbol:"foo"}]}')
  collect() { printf '%s\n' 'Prose.' '```json' "$tail" '```' >"$RUN/raw/t1-r1-A.md"; return 0; }

  # attribution is recorded BEFORE the member is launched, and survives a later handover
  reset_state
  run_step r1 prompt_round1 "$task" 1 >/dev/null
  local aid; aid=$(stj '.codemap_pending.attempt // empty' | tr -d '"')
  check 0 'the pending record carries the attempt identity' '' test -n "$(ls -d "$RUN"/codemap/attempts/t1-r1-a* 2>/dev/null)"
  local ldir; ldir=$(ls -d "$RUN"/codemap/attempts/t1-r1-a*/launches 2>/dev/null | head -1)
  check 0 'a pre-launch attribution record exists for the member' 1 jq -r .generation "$ldir/A.json"
  check 0 'the pre-launch record names the attempt it belongs to' t1 jq -r .task "$ldir/A.json"
  check 0 'the published report is attributed to the launch generation' 1 jq -r '[.entries[].reports[].reader.generation]|.[0]' "$RUN/codemap/index.json"
  check 0 'locator delivery boundaries are recorded, not searched for later' 't1-r1-A.md' cat "$RUN/codemap/deliveries.jsonl"

  # an unverifiable guard checkpoints instead of quietly passing
  reset_state
  run_step r1 prompt_round1 "$task" 1 >/dev/null
  sts '.candidate={id:"t1-c1",member:"A",text:"CAND"}'
  codemap_prepare t1 r2 0
  collect() { printf '%s\n' 'Prose.' '```json' '{"vote":"agree","questions":[]}' '```' >"$RUN/raw/t1-r2-A.md"; return 0; }
  # make the guarded source unreadable-but-present: a directory in its place is not "missing"
  chmod 000 "$DIR/src/a.py"
  check 2 'an unverifiable freshness guard checkpoints via exit 2 instead of passing' '' run_step r2 prompt_roundN "$task" 2
  chmod 644 "$DIR/src/a.py"
  check 0 'the checkpointed attempt keeps its posts (they are preserved, not stale)' '' test -e "$RUN/posts/t1-r2-A.md"
  check 0 'the unresolved obligation is still recorded in state' t1 jq -r '.codemap_pending.task' "$ST"
  check 0 'the run is left checkpointed, not advanced' failed st '.status'

  # once the guard verifies unchanged, the very same posts are reused and the step publishes
  codemap_resume_check
  check 0 'an unchanged recheck keeps the posts for reuse' '' test -e "$RUN/posts/t1-r2-A.md"

  # a stale replay archives the complete original posts before purging them
  reset_state
  collect() { printf '%s\n' 'Prose.' '```json' "$tail" '```' >"$RUN/raw/t1-r1-A.md"; return 0; }
  run_step r1 prompt_round1 "$task" 1 >/dev/null
  sts '.candidate={id:"t1-c1",member:"A",text:"CAND"}'
  codemap_prepare t1 r3 0
  local aid3; aid3=$(codemap_pending_attempt)
  : >"$RUN/posts/t1-r3-A.md"; printf 'ORIGINAL POST BODY\n' >"$RUN/posts/t1-r3-A.md"
  echo '{"vote":"agree","questions":[]}' >"$RUN/posts/t1-r3-A.json"
  printf 'one\nCHANGED\n' >"$DIR/src/a.py"
  codemap_resume_check
  check 0 'the stale replay purged the live posts' '' test ! -e "$RUN/posts/t1-r3-A.md"
  check 0 'the complete original post is archived in the private attempt area' 'ORIGINAL POST BODY' cat "$RUN/codemap/attempts/$aid3"/archived-posts/*/t1-r3-A.md
  check 0 'the archived accepted JSON tail is kept too' agree jq -r .vote "$RUN/codemap/attempts/$aid3"/archived-posts/*/t1-r3-A.json
  check 0 'the archive carries a durable ineligible marker' stale_or_ineligible jq -r .status "$RUN/codemap/attempts/$aid3"/archived-posts/*/MARKER.json
  check 0 'the round and candidate are untouched by the replay' 't1-c1' st '.candidate.id'
  check 0 'the round number is not advanced by the replay' 1 st '.round'

  # ---- A's reproduction: an unchanged resume recheck must NOT release the obligation, or a
  # source that changes afterwards reaches consensus behind a brand-new, empty guard.
  reset_state
  collect() { printf '%s\n' 'Prose.' '```json' "$tail" '```' >"$RUN/raw/t1-r1-A.md"; return 0; }
  printf 'one\ntwo\n' >"$DIR/src/a.py"
  run_step r1 prompt_round1 "$task" 1 >/dev/null
  sts '.candidate={id:"t1-c1",member:"A",text:"CAND"}'
  codemap_prepare t1 r4 0
  local aid4; aid4=$(codemap_pending_attempt)
  printf 'ORIGINAL\n' >"$RUN/posts/t1-r4-A.md"; echo '{"vote":"agree","questions":[]}' >"$RUN/posts/t1-r4-A.json"
  codemap_resume_check                       # unchanged: the step is NOT complete yet
  check 0 'an unchanged resume recheck keeps the SAME attempt, not a fresh one' "$aid4" codemap_pending_attempt
  check 0 'the obligation is still owed after an unchanged recheck' t1 jq -r '.codemap_pending.task' "$ST"
  check 0 'the original guard list is still the one on disk' '' test -s "$RUN/codemap/attempts/$aid4/guard.json"
  # the source now changes, AFTER the unchanged recheck but BEFORE the decision
  printf 'one\nCHANGED AFTER THE RECHECK\n' >"$DIR/src/a.py"
  CODEMAP_STEP=1
  check 1 'a change after an unchanged recheck is still caught at the decision barrier' '' codemap_finish_step t1 r4
  check 0 'its posts are purged rather than reaching consensus' '' test ! -e "$RUN/posts/t1-r4-A.md"

  # ---- stale is terminal: restoring the original bytes never revives the attempt
  check 0 'the stale attempt is marked stale' stale jq -r .status "$RUN/codemap/attempts/$aid4/meta.json"
  printf 'one\ntwo\n' >"$DIR/src/a.py"          # the exact original bytes are back
  local reout; reout=$(python3 "$CM" validate --run-dir "$RUN" --dir "$DIR" --task t1 --step r4 --mode resume 2>&1)
  check 0 'a restored source does not turn an already-stale attempt back into guard_ok' changed jq -r .guard_status <<<"$reout"
  check 0 'the terminal verdict is reported as terminal' true jq -r .terminal_stale <<<"$reout"
  check 0 'the attempt stays stale on disk' stale jq -r .status "$RUN/codemap/attempts/$aid4/meta.json"
  check 0 'a stale attempt never publishes' stale jq -r .status \
    <(python3 "$CM" ingest --run-dir "$RUN" --dir "$DIR" --task t1 --step r4 --mode publish 2>&1)

  # ---- a publication that does not complete holds the run at the barrier
  reset_state
  printf 'one\ntwo\n' >"$DIR/src/a.py"
  run_step r1 prompt_round1 "$task" 1 >/dev/null
  sts '.candidate={id:"t1-c1",member:"A",text:"CAND"}'
  codemap_prepare t1 r5 0
  printf 'ORIGINAL\n' >"$RUN/posts/t1-r5-A.md"; echo '{"vote":"agree","questions":[]}' >"$RUN/posts/t1-r5-A.json"
  CODEMAP_STEP=1
  export CM_REAL="$CM"; CM="$scratch/broken-cm.py"
  cat >"$CM" <<'PYX'
import sys, subprocess, os
if "--mode" in sys.argv and "publish" in sys.argv:
    sys.stderr.write("simulated publication failure\n"); sys.exit(1)
sys.exit(subprocess.run([sys.executable, os.environ["CM_REAL"]] + sys.argv[1:]).returncode)
PYX
  check 2 'a publication that does not complete checkpoints instead of advancing the run' '' codemap_finish_step t1 r5
  CM="$CM_REAL"
  check 0 'the staged work is retained after a failed publication' t1 jq -r '.codemap_pending.task' "$ST"
  check 0 'the posts survive a failed publication' '' test -e "$RUN/posts/t1-r5-A.md"
  # recovery: the outstanding publication is retried at the DECISION barrier, never at the resume
  # re-check — releasing the obligation there would hand the reused posts a brand-new guard.
  codemap_resume_check
  check 0 'a resume re-check does not release the obligation of a failed publication' t1 jq -r '.codemap_pending.task' "$ST"
  CODEMAP_STEP=1
  check 0 'the decision barrier completes the outstanding publication' '' codemap_finish_step t1 r5
  check 0 'and only then is the obligation released' null jq -r '.codemap_pending // "null"' "$ST"
  check 0 'the recovered publication really did publish' published \
    jq -r .status "$RUN/codemap/attempts/$(ls -t "$RUN/codemap/attempts" | grep '^t1-r5-a' | head -1)/meta.json"

  # ---- A's full sequence: a REAL end-of-step checkpoint, an unchanged resume, then a change
  # before the decision. The votes of the original attempt must never be decided behind a new guard.
  reset_state
  printf 'one\ntwo\n' >"$DIR/src/a.py"
  collect() { printf '%s\n' 'Prose.' '```json' "$tail" '```' >"$RUN/raw/t1-r1-A.md"; return 0; }
  run_step r1 prompt_round1 "$task" 1 >/dev/null
  sts '.candidate={id:"t1-c1",member:"A",text:"CAND"}'
  # the guarded source becomes unreadable WHILE the round is in flight, so the end-of-step guard
  # genuinely cannot be evaluated — the view was frozen and delivered before that happened
  collect() { printf '%s\n' 'Prose.' '```json' '{"vote":"agree","questions":[]}' '```' >"$RUN/raw/t1-r6-A.md"
              chmod 000 "$DIR/src/a.py"; return 0; }
  check 2 'the real end-of-step validation checkpoints on an unverifiable guard' '' run_step r6 prompt_roundN "$task" 2
  chmod 644 "$DIR/src/a.py"
  local aid8; aid8=$(codemap_pending_attempt)
  check 0 'the checkpointed step still owes its obligation' t1 jq -r '.codemap_pending.task' "$ST"
  codemap_resume_check               # readable again and unchanged
  check 0 'the ORIGINAL attempt survives the unchanged resume re-check' "$aid8" codemap_pending_attempt
  check 0 'the unchanged re-check publishes nothing' '' test "$(jq -r .status "$RUN/codemap/attempts/$aid8/meta.json")" != published
  printf 'one\nCHANGED BEFORE THE DECISION\n' >"$DIR/src/a.py"
  sts '.reuse_posts=true'
  check 1 'the resumed step reuses the same attempt, and its own guard catches the change' '' \
    run_step r6 prompt_roundN "$task" 2
  check 0 'run_step never opened a second attempt for that step' 1 \
    sh -c 'ls -d "$1"/codemap/attempts/t1-r6-a* | wc -l | tr -d " "' _ "$RUN"
  check 0 'the reused votes are purged rather than reaching consensus' '' test ! -e "$RUN/posts/t1-r6-A.md"
  check 0 'and no vote survives into the decision' 0 jq -r '.last_votes|length' "$ST"
  check 0 'the original attempt is the one marked stale' stale jq -r .status "$RUN/codemap/attempts/$aid8/meta.json"

  # ---- the archive attribution comes from the LAUNCH record, not the current generation
  reset_state
  run_step r1 prompt_round1 "$task" 1 >/dev/null
  local aid6; aid6=$(basename "$(ls -dt "$RUN"/codemap/attempts/t1-r1-a* | head -1)")
  sts --arg a "$aid6" '.codemap_pending={task:"t1",step:"r1",attempt:$a}|.members[0].gen=7'                        # a handover bumped the generation since the launch
  printf 'BODY\n' >"$RUN/posts/t1-r1-A.md"
  codemap_archive_posts t1 r1
  check 0 'the archive attributes the post to the generation it was LAUNCHED with' 1 \
    jq -r .generation "$RUN/codemap/attempts/$aid6"/archived-posts/*/A.attribution.json
  check 0 'the archive records that the attribution came from a real launch record' true \
    jq -r .attribution_from_launch_record "$RUN/codemap/attempts/$aid6"/archived-posts/*/A.attribution.json

  # ---- a REPLACEMENT launched inside one attempt: its reply is its own, not the old session's.
  # The member is launched at generation 1 and never produces an accepted reply; it is replaced,
  # and the new session is genuinely launched again inside the SAME attempt.
  reset_state
  codemap_prepare t1 r7 0
  check 0 'the replacement scenario runs with the map enabled' 1 echo "$CODEMAP_STEP"
  local aidr; aidr=$(codemap_pending_attempt)
  codemap_record_launch 0 t1 r7 1;                      local lid1=$CODEMAP_LAUNCH_ID
  sts '.members[0].gen=2'                               # replaced: a new session, same attempt
  codemap_record_launch 0 t1 r7 2;                      local lid2=$CODEMAP_LAUNCH_ID
  printf '%s\n' "$tail" >"$RUN/posts/t1-r7-A.json"; printf 'BODY\n' >"$RUN/posts/t1-r7-A.md"
  codemap_stage_accepted 0 t1 r7 "$RUN/posts/t1-r7-A.json" 2 "$lid2"
  check 0 "the replacement's accepted reply is staged as generation 2" 2 \
    sh -c 'jq -r .attribution.generation "$1"/codemap/attempts/"$2"/staged/A-*.json' _ "$RUN" "$aidr"
  check 0 'the generation-1 launch record is still there, unchanged' 1 \
    jq -r .generation "$RUN/codemap/attempts/$aidr/launches/A.json"
  check 0 'the replacement launch has its OWN immutable record' 2 \
    sh -c 'jq -r .generation "$1/codemap/attempts/$2/launches/$3.json"' _ "$RUN" "$aidr" "$lid2"
  check 0 'and the two launches are distinct identities' '' test "$lid1" != "$lid2"
  check 0 'its bound report is attributed to generation 2' 2 \
    sh -c 'jq -r "[.entries[].reports[].reader.generation]|.[0]" "$1"/codemap/attempts/"$2"/staged/A-*.json' _ "$RUN" "$aidr"
  check 0 'the durable acceptance metadata names generation 2' 2 \
    sh -c 'cat "$1"/codemap/attempts/"$2"/accepted/*.meta.json | jq -s -r "map(select(.member==\"A\"))|.[0].generation"' _ "$RUN" "$aidr"
  check 0 'and names the launch that produced it' "$lid2" \
    sh -c 'cat "$1"/codemap/attempts/"$2"/accepted/*.meta.json | jq -s -r "map(select(.member==\"A\"))|.[0].launch_id"' _ "$RUN" "$aidr"
  # archival binds to that accepted event even after the member's generation moves on again
  sts '.members[0].gen=9'
  codemap_archive_posts t1 r7
  check 0 'the archive attributes the post to the accepted event, not the current generation' 2 \
    jq -r .generation "$RUN/codemap/attempts/$aidr"/archived-posts/*/A.attribution.json
  check 0 'the archive names the launch the accepted event was bound to' "$lid2" \
    jq -r .launch_id "$RUN/codemap/attempts/$aidr"/archived-posts/*/A.attribution.json

  # ---- byte-identical tails from two launches of one member. Both accepted events carry the SAME
  # member and the SAME accepted_sha256, so "the first metadata record matching member+digest" is a
  # coin toss between generation 1 and generation 2. Only the association written at acceptance
  # says which event the live post actually is.
  reset_state
  codemap_prepare t1 r8 0
  local aidt; aidt=$(codemap_pending_attempt)
  codemap_record_launch 0 t1 r8 1;                       local tl1=$CODEMAP_LAUNCH_ID
  sts '.members[0].gen=2'
  codemap_record_launch 0 t1 r8 2;                       local tl2=$CODEMAP_LAUNCH_ID
  printf '%s\n' "$tail" >"$RUN/posts/t1-r8-A.json"; printf 'BODY\n' >"$RUN/posts/t1-r8-A.md"
  codemap_stage_accepted 0 t1 r8 "$RUN/posts/t1-r8-A.json" 1 "$tl1"
  codemap_stage_accepted 0 t1 r8 "$RUN/posts/t1-r8-A.json" 2 "$tl2"
  check 0 'the identical tails are two accepted events under one digest' 2 \
    sh -c 'ls "$1/codemap/attempts/$2/accepted"/*.meta.json | wc -l | tr -d " "' _ "$RUN" "$aidt"
  # Make the live post belong to whichever event does NOT sort first, so a first-match selection
  # provably names the wrong generation whatever order the event ids happen to hash into.
  local tsfirst tgen tlid teid ASSOC="$RUN/codemap/attempts/$aidt/post-assoc/t1-r8-A.json.assoc.json"
  tsfirst=$(jq -r .generation "$(ls "$RUN/codemap/attempts/$aidt/accepted"/*.meta.json | head -1)")
  if [ "$tsfirst" = 1 ]; then tgen=2; tlid=$tl2; else tgen=1; tlid=$tl1; fi
  codemap_stage_accepted 0 t1 r8 "$RUN/posts/t1-r8-A.json" "$tgen" "$tlid"   # the live post's event
  teid=$(jq -r '.event_id // empty' "$ASSOC" 2>/dev/null)
  # the member moves on; the post is REUSED from the checkpoint, so it is never staged again
  sts '.members[0].gen=9|.reuse_posts=true'
  codemap_archive_posts t1 r8
  check 0 'the archive resolves the association instead of the first member+digest match' "$tgen" \
    jq -r .generation "$RUN/codemap/attempts/$aidt"/archived-posts/*/A.attribution.json
  check 0 'and the launch that produced it' "$tlid" \
    jq -r .launch_id "$RUN/codemap/attempts/$aidt"/archived-posts/*/A.attribution.json
  check 0 'and that exact accepted event, which is NOT the one that sorts first' "$teid" \
    sh -c 'test -s "$2" && test "$(jq -r .event_id "$2")" != "$(jq -r .event_id "$(ls "$3"/*.meta.json | head -1)")" && jq -r .event_id "$1"/archived-posts/*/A.attribution.json' \
      _ "$RUN/codemap/attempts/$aidt" "$ASSOC" "$RUN/codemap/attempts/$aidt/accepted"
  check 0 'the reused post kept its original association untouched' "$teid" jq -r .event_id "$ASSOC"
  # without an exact association, an ambiguous member+digest match is not provenance
  rm -rf "$RUN/codemap/attempts/$aidt/archived-posts" "$ASSOC"
  codemap_archive_posts t1 r8
  check 0 'with no association and two launches the archive claims no generation' null \
    jq -r .generation "$RUN/codemap/attempts/$aidt"/archived-posts/*/A.attribution.json
  check 0 'and says plainly that nothing authenticated it' false \
    jq -r .attribution_from_launch_record "$RUN/codemap/attempts/$aidt"/archived-posts/*/A.attribution.json
  check 0 'while the post itself is preserved in full' 1 \
    sh -c 'ls "$1/codemap/attempts/$2"/archived-posts/*/t1-r8-A.md | wc -l | tr -d " "' _ "$RUN" "$aidt"

  # ---- an archive that cannot be written stops the run before anything is purged
  reset_state
  run_step r1 prompt_round1 "$task" 1 >/dev/null
  local aid7; aid7=$(basename "$(ls -dt "$RUN"/codemap/attempts/t1-r1-a* | head -1)")
  sts --arg a "$aid7" '.codemap_pending={task:"t1",step:"r1",attempt:$a}'
  printf 'MUST SURVIVE\n' >"$RUN/posts/t1-r1-A.md"
  mkdir -p "$RUN/codemap/attempts/$aid7/archived-posts"
  chmod 500 "$RUN/codemap/attempts/$aid7/archived-posts"
  check 2 'an archive that cannot be written checkpoints instead of purging the originals' '' codemap_archive_posts t1 r1
  chmod 755 "$RUN/codemap/attempts/$aid7/archived-posts"
  check 0 'the original post was never deleted' 'MUST SURVIVE' cat "$RUN/posts/t1-r1-A.md"

  # ---- a locator failure before any exposure leaves no dangling obligation
  reset_state
  export CM_REAL2="$CM"; CM="$scratch/nolocator-cm.py"
  cat >"$CM" <<'PYX'
import sys, subprocess, os
if sys.argv[1] == "locator":
    sys.stderr.write("simulated locator failure\n"); sys.exit(1)
sys.exit(subprocess.run([sys.executable, os.environ["CM_REAL2"]] + sys.argv[1:]).returncode)
PYX
  codemap_prepare t1 r9 0
  check 0 'a locator failure before exposure disables map delivery for the attempt' 0 echo "$CODEMAP_STEP"
  check 0 'and leaves NO pending freshness obligation behind' null jq -r '.codemap_pending // "null"' "$ST"
  CM="$CM_REAL2"
)
map_prepass_lifecycle_tests() (
  load council.sh
  eval "$(declare -f map_prepass_run | sed 's/map_prepass_run/map_prepass_real/g')"
  RUN="$scratch/map-run"; ST="$RUN/state.json"; mkdir -p "$RUN/prompts" "$RUN/posts" "$RUN/raw"
  # Authorization must remain a durable precondition even when its state write fails.
  auth_run="$scratch/auth-write-fail-run"; mkdir -p "$auth_run"; RUN="$auth_run"; ST="$RUN/state.json"
  jq -n '{config:{dir:"/tmp",map_code:true,map_prepass:{}},map_prepass_version:1,map_prepass_pending:null,status:"running",phase:"plan",pending_questions:null}' >"$ST"
  MAP_PREPASS=1; check_mapper_available() { :; }; sts() { return 1; }
  check 4 'T2 failed proposal persistence stops at the authorization boundary' '' ensure_mapper_authorized
  check 0 'T2 failed proposal write does not record an authorization or pending checkpoint' true jq -e '.map_prepass_authorized==null and .map_prepass_pending==null' "$ST"
  jq -n '{config:{dir:"/tmp",map_code:true,map_prepass:{model:"google/gemini-3.8-flash",effort:"medium"}},map_prepass_version:1,map_prepass_authorized:null,map_prepass_pending:null,status:"running",phase:"plan",pending_questions:null}' >"$ST"
  check 4 'T2 failed configured-authorization persistence blocks inference' '' ensure_mapper_authorized
  check 0 'T2 configured mapper is not authorized after failed persistence' true jq -e '.map_prepass_authorized==null' "$ST"
  jq -n '{config:{dir:"/tmp",map_code:true,map_prepass:{model:"google/gemini-3.8-flash",effort:"medium"}},map_prepass_version:1,map_prepass_authorized:null,map_prepass_pending:{kind:"opencode",model:"google/gemini-3.8-flash",effort:"medium"},status:"questions",phase:"mapper_confirm",pending_questions:[{id:"mapper-confirmation"}]}' >"$ST"
  CONFIRM_MAPPER_FILE="$scratch/auth-confirm.json"; printf '{"kind":"opencode","model":"google/gemini-3.8-flash","effort":"medium"}\n' >"$CONFIRM_MAPPER_FILE"
  check 4 'T2 failed explicit-confirmation persistence blocks inference' '' apply_mapper_confirmation
  check 0 'T2 failed confirmation write leaves pending proposal and no authorization' true jq -e '.map_prepass_pending.model=="google/gemini-3.8-flash" and .map_prepass_authorized==null and .phase=="mapper_confirm"' "$ST"
  sts() { jq "$@" "$ST" >"$ST.tmp" && mv "$ST.tmp" "$ST"; }
  counter="$scratch/map-launches"; : >"$counter"
  jq -n --arg d "$scratch" '{config:{dir:$d,max_rounds:2,timeout_s:30,handover_at:0.5,max_turns:3,executor:null,
    map_code:true,map_prepass:{kind:"opencode",model:"google/gemini-3.8-flash",effort:"medium"},
    tasks:[{id:"one",text:"task one",execute:false},{id:"two",text:"task two",execute:false}]},
    map_prepass_version:1,map_prepasses:{},map_prepass_authorized:{source:"config"},members:[],answers:[],results:[],task_idx:0,round:1,phase:"plan",status:"running"}' >"$ST"
  MAP_PREPASS=1; N=0; DIR=$scratch; EXEC=""; TIMEOUT=30; MAXR=2; HANDOVER=.5; MAXT=3
  map_prepass_run() { local t key; t=$(jq -r .id <<<"$1"); key=$(map_key "$t"); echo "$t" >>"$counter"; mkdir -p "$RUN/map/$key"; jq -n --arg t "$t" '{schema_version:1,parent_id:$t,map_seed_id:("seed-"+$t),coverage:"unknown",capture_statuses:[]}' >"$RUN/map/$key/coverage.json"; sts --arg t "$t" --arg d "$RUN/map/$key" '.map_prepasses[$t]={status:"complete",artifacts:{directory:$d,coverage:($d+"/coverage.json")}}'; }
  deliberations="$scratch/deliberations"; : >"$deliberations"
  deliberate() { echo "$(jq -r .id <<<"$1"):$(st .phase)" >>"$deliberations"; return 0; }
  render_transcript() { :; }
  run_enabled() { run_tasks; }
  check 4 'T1 real mapper review pauses before the first deliberation and displays contract seed' 'Split contract map_seed_id (copy this exact value into contract JSON): seed-one' run_enabled
  check 0 'T1 first original task is mapped once before map review' '' test "$(wc -l <"$counter" | tr -d ' ')" -eq 1
  check 0 'T1 production map review stores a usable seed locator' true jq -r '.map_prepasses.one.seed_snapshot_id|length>0' "$ST"
  check 0 'T1 map review starts with neutral unknown scope applicability' unknown jq -r '.map_prepasses.one.scope_applicability' "$ST"
  echo '{"action":"keep"}' >"$scratch/keep-one.json"; MAP_DECISION_FILE="$scratch/keep-one.json"
  check 0 'production keep decision returns original task to plan' '' apply_map_decision
  check 4 'keep proceeds through deliberation then pauses at the next original task review' 'Complete-map locator' run_enabled
  check 0 'keep did not bypass first-task deliberation' 'one:plan' grep -F 'one:plan' "$deliberations"
  check 0 'both original tasks have one pre-pass each' '' test "$(wc -l <"$counter" | tr -d ' ')" -eq 2
  echo '{"action":"keep"}' >"$scratch/keep-two.json"; MAP_DECISION_FILE="$scratch/keep-two.json"
  check 0 'second production keep accepts map review' '' apply_map_decision
  check 0 'second keep allows the remaining task to finish' '' run_enabled
  check 0 'T1 every original task deliberated in plan' '' test "$(wc -l <"$deliberations" | tr -d ' ')" -eq 2
  : >"$counter"
  sts '.config.map_code=false'; MAP_PREPASS=0; sts '.task_idx=0 | .status="running"'
  map_prepass_run() { echo called >>"$counter"; return 0; }
  run_disabled() { run_tasks; }
  check 0 'T1 disabled run continues through the ordinary task path' '' run_disabled
  check 0 'T1 disabled run launches no mapper' '' test "$(wc -l <"$counter" | tr -d ' ')" -eq 0
  # Compare real prompt generators against the fixed pre-feature council script.
  baseline_prompt_fixture() (
    local script_file="$1" dest="$2" map_choice="$3"
    local functions="$dest/functions.sh"
    set -- prompt-fixture
    sed '/^# .* commands /,$d' "$script_file" >"$functions" || exit 2
    eval "$(cat "$functions")"
    RUN="$scratch/prompt-fixture"; ST="$RUN/state.json"; mkdir -p "$RUN/prompts" "$RUN/posts" "$RUN/raw"
    jq -n --arg choice "$map_choice" '{started:"fixture",task_id:"t1",phase:"plan",round:1,candidate:{text:"consensus plan"},fixes:[],
      config:({dir:"/tmp",executor:"A",handover_at:0.5,tasks:[{id:"t1",text:"fixture task",execute:true}]} + (if $choice=="false" then {map_code:false} else {} end)),
       members:[{id:"A",kind:"opencode",model:"p/m",effort:"medium",mode:"edit",agent:"build",session:null,gen:1,fresh:false}],
      answers:[],notices:[],results:[{task:"t1",text:"fixture proposal"}],last_votes:[],codemap_version:1}' >"$ST"
    CODEMAP=0; MAP_PREPASS=0; DIR=/tmp; EXEC=A; MAXR=2; HANDOVER=0.5; MAXT=30
    local t='{"id":"t1","text":"fixture task","execute":true}'
    prompt_round1 0 "$t" 1 >"$dest/round1"
    prompt_exec 0 "$t" 1 >"$dest/exec"
    prompt_ratify 0 "$t" 1 >"$dest/ratify"
    prompt_fix 0 "$t" 2 >"$dest/fix"
    prompt_handover 0 >"$dest/handover"
  )
  git show 97f4c70:scripts/council.sh >"$scratch/baseline-council.sh" || exit 1
  for map_choice in false omitted; do
    mkdir -p "$scratch/baseline-prompts-$map_choice" "$scratch/current-prompts-$map_choice"
    baseline_prompt_fixture "$scratch/baseline-council.sh" "$scratch/baseline-prompts-$map_choice" "$map_choice"
    baseline_prompt_fixture "$HERE/council.sh" "$scratch/current-prompts-$map_choice" "$map_choice"
    for part in round1 exec ratify fix handover; do check 0 "T1 $map_choice $part prompt bytes match 97f4c70" '' cmp -s "$scratch/baseline-prompts-$map_choice/$part" "$scratch/current-prompts-$map_choice/$part"; done
  done
  check 0 'T1 omitted map_code remains absent from state' null jq -r '.map_prepass_version // "null"' "$scratch/prompt-fixture/state.json"
  check 0 'T1 disabled and omitted fixture launch no mapper' '' test "$(wc -l <"$counter" | tr -d ' ')" -eq 0

  # Mapper authorization is a separate durable gate; confirmation uses the exact proposed tuple.
  jq -n '{config:{dir:"/tmp",map_code:true,map_prepass:{timeout_s:120,max_output_bytes:65536}},map_prepass_pending:{kind:"opencode",model:"google/gemini-3.8-flash",effort:"medium"},phase:"mapper_confirm",status:"questions",pending_questions:[]}' >"$ST"
  jq -n '{kind:"opencode",model:"google/gemini-3.8-flash",effort:"medium"}' >"$scratch/confirm.json"
  check_mapper_available() { echo checked >>"$scratch/mapper-metadata"; }
  MAP_PREPASS=0; CONFIRM_MAPPER_FILE="$scratch/confirm.json"
  check 0 'T2 no mapper authorized before confirmation' false jq -r '.map_prepass_authorized.source == "user-confirmed"' "$ST"
  check 0 'T2 exact confirmation accepted and recorded' '' apply_mapper_confirmation
  check 0 'T2 confirmed model and effort are preserved' true jq -r '.map_prepass_authorized.model=="google/gemini-3.8-flash" and .map_prepass_authorized.effort=="medium"' "$ST"

  # An old state—even one whose config carries a new-looking switch—must not enable a pre-pass.
  jq -n '{config:{dir:"/tmp",map_code:true,members:[],max_rounds:2,timeout_s:30,handover_at:0.5},status:"running"}' >"$ST"
  RUN="$RUN"; load_state
  check 0 'T3 old run without pre-pass version remains unmapped' '' test "$MAP_PREPASS" -eq 0

  # Config omission is accepted as false; explicit non-booleans are rejected at validation.
  cfgfile="$scratch/map-config.json"
  jq -n --arg d "$scratch" '{dir:$d,max_rounds:2,timeout_s:30,handover_at:0.5,executor:null,
    tasks:["read this"],members:[{id:"A",kind:"opencode",model:"test/a",effort:"high",mode:"read"},
    {id:"B",kind:"opencode",model:"test/b",effort:"high",mode:"read"}]}' >"$cfgfile"
  normalized=$(validate_config "$cfgfile") || exit 1
  check 0 'T1 config with omitted map_code normalizes to false' false jq -r '.map_code' <<<"$normalized"
  jq '.map_code="yes"' "$cfgfile" >"$scratch/invalid-map-config.json"
  invalid_config() { validate_config "$1" 2>&1; }
  check 1 'map_code rejects non-boolean values' 'map_code must be a boolean' invalid_config "$scratch/invalid-map-config.json"

  # T2 dispatch through the actual command entry point with a counting fake OpenCode adapter.
  dispatch_project="$scratch/dispatch-project"; mkdir -p "$dispatch_project"
  dispatch_home="$scratch/dispatch-home"; mkdir -p "$dispatch_home/ptools"
  cp "$HERE/council.sh" "$HERE/council_codemap.py" "$HERE/council_map_prepass.py" "$HERE/council_splitcheck.py" "$dispatch_home/"
  cp -R "$HERE/ptools/." "$dispatch_home/ptools/"
  dispatch_script="$dispatch_home/council.sh"
  dispatch_oc="$dispatch_home/oc.sh"; FAKE_MAP_ARGS="$scratch/dispatch-args"; FAKE_MAP_CALLS="$scratch/dispatch-calls"
  FAKE_MAP_MODELS="$scratch/dispatch-models.json"; FAKE_MAP_RESPONSE="$scratch/dispatch-response.json"
  printf '{"data":[{"enabled":true,"providerID":"google","id":"gemini-3.8-flash","variants":[{"id":"medium"},{"id":"high"}],"limit":{"context":100000}}]}\n' >"$FAKE_MAP_MODELS"
  printf '{"candidates":[],"unresolved":[],"stopped_reason":"done"}\n' >"$FAKE_MAP_RESPONSE"
  cat >"$dispatch_oc" <<'SH'
#!/bin/sh
cmd=$1; shift
case "$cmd" in
  ensure) exit 0 ;;
  api) case "$2" in /api/model*) cat "$FAKE_MAP_MODELS" ;; *) echo '{"data":{"tokens":{"input":1,"output":1,"reasoning":0,"cache":{"read":0,"write":0}},"cost":0}}' ;; esac ;;
  new) echo "new $*" >>"$FAKE_MAP_ARGS"; echo ses_dispatch ;;
  prompt) echo prompt >>"$FAKE_MAP_CALLS" ;;
  wait) echo idle ;;
  result) echo "result $*" >>"$FAKE_MAP_ARGS"; if [ -n "$FAKE_MAP_RESULT_COUNT" ]; then n=0; [ ! -f "$FAKE_MAP_RESULT_COUNT" ] || n=$(cat "$FAKE_MAP_RESULT_COUNT"); n=$((n+1)); echo "$n" >"$FAKE_MAP_RESULT_COUNT"; [ "$n" -eq 1 ] || { echo 'second retrieval forbidden' >&2; exit 91; }; fi; cat "$FAKE_MAP_RESPONSE" ;;
  messages) echo '{"data":[]}' ;;
  interrupt) exit 0 ;;
  *) echo "unexpected adapter call: $cmd $*" >&2; exit 90 ;;
esac
SH
  chmod +x "$dispatch_oc"; export FAKE_MAP_ARGS FAKE_MAP_CALLS FAKE_MAP_MODELS FAKE_MAP_RESPONSE
  make_dispatch_config() {
    local config_path=$1 mapper=$2 effort=${3:-medium}
    jq -n --arg d "$dispatch_project" --arg mapper "$mapper" --arg effort "$effort" \
      '{dir:$d,max_rounds:2,timeout_s:30,handover_at:0.5,max_turns:3,executor:null,map_code:true,tasks:["dispatch task"],members:[
        {id:"A",kind:"claude",model:"test/a",effort:"high",mode:"read"},
        {id:"B",kind:"claude",model:"test/b",effort:"high",mode:"read"}]} +
       (if $mapper=="complete" then {map_prepass:{kind:"opencode",model:"google/gemini-3.8-flash",effort:$effort}} else {} end)' >"$config_path"
  }
  dispatch_run="$scratch/dispatch-confirm-run"; dispatch_cfg="$scratch/dispatch-confirm-config.json"
  make_dispatch_config "$dispatch_cfg" omitted
  : >"$FAKE_MAP_ARGS"; : >"$FAKE_MAP_CALLS"
  start_dispatch() { "$dispatch_script" start --config "$dispatch_cfg" --run-dir "$dispatch_run" 2>&1; }
  check 4 'T2 real start pauses when mapper settings need confirmation' 'mapper confirmation' start_dispatch
  check 0 'T2 confirmation question records the proposed tuple' true jq -e '.[0].id=="mapper-confirmation"' "$dispatch_run/questions.json"
  check 0 'T2 proposed default tuple is persisted' true jq -e '.map_prepass_pending=={kind:"opencode",model:"google/gemini-3.8-flash",effort:"medium",timeout_s:120,max_output_bytes:65536}' "$dispatch_run/state.json"
  check 0 'T2 missing mapper settings make zero inference calls' '' test ! -s "$FAKE_MAP_CALLS"
  for partial in model-only effort-only; do
    partial_cfg="$scratch/dispatch-$partial.json"; partial_run="$scratch/dispatch-$partial-run"
    if [ "$partial" = model-only ]; then jq '.map_prepass={kind:"opencode",model:"google/gemini-3.8-flash"}' "$dispatch_cfg" >"$partial_cfg"
    else jq '.map_prepass={kind:"opencode",effort:"high"}' "$dispatch_cfg" >"$partial_cfg"; fi
    partial_start() { "$dispatch_script" start --config "$partial_cfg" --run-dir "$partial_run" 2>&1; }
    : >"$FAKE_MAP_CALLS"
    check 4 "T2 $partial mapper tuple pauses for missing field" 'mapper confirmation' partial_start
    if [ "$partial" = model-only ]; then
      check 0 'T2 model-only partial config proposes default effort' medium jq -r '.map_prepass_pending.effort' "$partial_run/state.json"
    else
      check 0 'T2 effort-only partial config proposes default model' google/gemini-3.8-flash jq -r '.map_prepass_pending.model' "$partial_run/state.json"
    fi
    check 0 "T2 $partial configuration dispatches no inference" '' test ! -s "$FAKE_MAP_CALLS"
  done
  before_confirm=$(shasum -a 256 <"$dispatch_run/state.json" | cut -d' ' -f1)
  dispatch_state_hash() { shasum -a 256 <"$dispatch_run/state.json" | cut -d' ' -f1; }
  generic_answer() { "$dispatch_script" resume --run-dir "$dispatch_run" --answer "yes" 2>&1; }
  check 4 'T2 generic answer cannot authorize mapper' 'use --confirm-mapper' generic_answer
  after_answer=$(shasum -a 256 <"$dispatch_run/state.json" | cut -d' ' -f1)
  check 0 'T2 generic-answer rejection leaves state unchanged' "$before_confirm" dispatch_state_hash
  for bad in malformed extra mismatch stale; do
    case "$bad" in
      malformed) printf '{broken\n' >"$scratch/confirm-$bad.json" ;;
      extra) printf '{"kind":"opencode","model":"google/gemini-3.8-flash","effort":"medium","other":true}\n' >"$scratch/confirm-$bad.json" ;;
      mismatch) printf '{"kind":"opencode","model":"google/other","effort":"medium"}\n' >"$scratch/confirm-$bad.json" ;;
      stale) printf '{"kind":"opencode","model":"google/gemini-3.8-flash","effort":"high"}\n' >"$scratch/confirm-$bad.json" ;;
    esac
    bad_confirmation() { "$dispatch_script" resume --run-dir "$dispatch_run" --confirm-mapper "$scratch/confirm-$bad.json" 2>&1; }
    case "$bad" in malformed|extra) expected='confirmation file must contain exactly' ;; mismatch|stale) expected='does not exactly match' ;; esac
    check 4 "T2 $bad mapper confirmation is rejected" "$expected" bad_confirmation
    check 0 "T2 $bad confirmation leaves checkpoint state unchanged" "$before_confirm" dispatch_state_hash
    check 0 "T2 $bad confirmation performs no inference" '' test ! -s "$FAKE_MAP_CALLS"
  done
  printf '{"kind":"opencode","model":"google/gemini-3.8-flash","effort":"medium"}\n' >"$scratch/confirm-good.json"
  good_confirmation() { "$dispatch_script" resume --run-dir "$dispatch_run" --confirm-mapper "$scratch/confirm-good.json" 2>&1; }
  check 4 'T2 exact tuple confirms and proceeds to map review without deliberating first' 'map review pending' good_confirmation
  check 0 'T2 exact tuple authorization is durably recorded' true jq -e '.map_prepass_authorized.model=="google/gemini-3.8-flash" and .map_prepass_authorized.effort=="medium" and .map_prepass_authorized.source=="user-confirmed"' "$dispatch_run/state.json"
  check 0 'T2 mapper session launches with the confirmed model and variant' '--model google/gemini-3.8-flash --variant medium' cat "$FAKE_MAP_ARGS"

  crash_run="$scratch/crash-before-auth"; mkdir -p "$crash_run"
  jq -n --arg d "$dispatch_project" '{config:{dir:$d,max_rounds:2,timeout_s:30,handover_at:0.5,max_turns:3,executor:null,map_code:true,map_prepass:{},tasks:[{id:"t1",text:"recovered task",execute:false}],members:[]},codemap_version:1,map_prepass_version:1,map_prepasses:{},status:"running",phase:"plan",task_idx:0,task_id:null,round:1,answers:[],results:[],members:[],pending_questions:null}' >"$crash_run/state.json"
  : >"$FAKE_MAP_ARGS"; : >"$FAKE_MAP_CALLS"
  crash_resume() { "$dispatch_script" resume --run-dir "$crash_run" 2>&1; }
  check 4 'T2 enabled crash state without authorization recovers into confirmation' 'confirmation is required before inference' crash_resume
  check 0 'T2 crash recovery creates a durable confirmation question' true jq -e '.phase=="mapper_confirm" and .map_prepass_pending.model=="google/gemini-3.8-flash"' "$crash_run/state.json"
  check 0 'T2 crash recovery launches no inference' '' test ! -s "$FAKE_MAP_CALLS"

  configured_recovery="$scratch/configured-auth-recovery"; mkdir -p "$configured_recovery"
  jq -n --arg d "$dispatch_project" '{config:{dir:$d,max_rounds:2,timeout_s:30,handover_at:0.5,max_turns:3,executor:null,map_code:true,map_prepass:{kind:"opencode",model:"google/gemini-3.8-flash",effort:"medium"},tasks:[{id:"t1",text:"recover configured authorization",execute:false}],members:[]},codemap_version:1,map_prepass_version:1,map_prepasses:{},status:"running",phase:"plan",task_idx:0,task_id:null,round:1,answers:[],results:[],pending_questions:null}' >"$configured_recovery/state.json"
  : >"$FAKE_MAP_ARGS"; : >"$FAKE_MAP_CALLS"
  configured_recovery_resume() { "$dispatch_script" resume --run-dir "$configured_recovery" 2>&1; }
  check 4 'T2 configured mapper authorization recovers without a pause' 'map review pending' configured_recovery_resume
  check 0 'T2 configured authorization recovery is durably recorded' true jq -e '.map_prepass_authorized.source=="config"' "$configured_recovery/state.json"
  check 0 'T2 authorization recovery launches configured mapper once' '--model google/gemini-3.8-flash --variant medium' cat "$FAKE_MAP_ARGS"

  dispatch_configured="$scratch/dispatch-configured-run"; configured_cfg="$scratch/dispatch-configured.json"
  make_dispatch_config "$configured_cfg" complete high
  printf '{"data":[{"enabled":true,"providerID":"google","id":"gemini-3.8-flash","variants":[{"id":"medium"}],"limit":{"context":100000}}]}\n' >"$FAKE_MAP_MODELS"
  : >"$FAKE_MAP_ARGS"; : >"$FAKE_MAP_CALLS"
  configured_start() { "$dispatch_script" start --config "$configured_cfg" --run-dir "$dispatch_configured" 2>&1; }
  check 1 'T2 unavailable mapper variant fails precisely without substitution' 'effort '\''high'\'' is not a variant' configured_start
  check 0 'T2 unavailable variant has no inference fallback' '' test ! -s "$FAKE_MAP_CALLS"
  unavailable_model_run="$scratch/unavailable-model-run"
  printf '{"data":[]}' >"$FAKE_MAP_MODELS"; : >"$FAKE_MAP_CALLS"
  unavailable_model_start() { "$dispatch_script" start --config "$configured_cfg" --run-dir "$unavailable_model_run" 2>&1; }
  check 1 'T2 unavailable mapper model fails distinctly from unavailable variant' 'mapper model not enabled' unavailable_model_start
  check 0 'T2 unavailable model makes no inference calls' '' test ! -s "$FAKE_MAP_CALLS"
  printf '{"data":[{"enabled":true,"providerID":"google","id":"gemini-3.8-flash","variants":[{"id":"high"}],"limit":{"context":100000}}]}\n' >"$FAKE_MAP_MODELS"
  check 4 'T2 fully configured mapper starts without confirmation pause' 'map review pending' configured_start
  check 0 'T2 configured mapper launches exact config model and variant' '--model google/gemini-3.8-flash --variant high' cat "$FAKE_MAP_ARGS"

  # T4: actual resume --map-decision rejects a sibling-created prerequisite before inference.
  split_resume_run="$scratch/real-split-rejection"; split_parent="parent-task"; split_key=$(printf '%s' "$split_parent" | shasum -a 256 | cut -c1-16)
  mkdir -p "$split_resume_run/map/$split_key"
  split_root="$scratch/real-split-project"; mkdir -p "$split_root"; printf 'baseline\n' >"$split_root/base.txt"
  split_seed="seed-from-review"
  printf '{"map_seed_id":"%s","coverage":"unknown"}\n' "$split_seed" >"$split_resume_run/map/$split_key/coverage.json"
  jq -n --arg p "$split_parent" --arg seed "$split_seed" --arg missing "$split_root/generated.txt" \
    '{schema_version:1,parent_id:$p,map_seed_id:$seed,subtasks:[
      {id:"A",text:"consume generated file",execute:false,requires:[{path:"generated.txt",sha256:"0000000000000000000000000000000000000000000000000000000000000000"}],modifies:[],deletes:[],creates:[],acceptance:[],unresolved:[]},
      {id:"B",text:"create generated file",execute:false,requires:[],modifies:[],deletes:[],creates:["generated.txt"],acceptance:[],unresolved:[]}]}' >"$scratch/real-sibling-dependency.json"
  printf '{"action":"split","contract_file":"real-sibling-dependency.json"}\n' >"$scratch/real-split-decision.json"
  jq -n --arg d "$split_root" --arg p "$split_parent" '{config:{dir:$d,max_rounds:2,timeout_s:30,handover_at:0.5,max_turns:3,executor:null,map_code:true,map_prepass:{kind:"opencode",model:"google/gemini-3.8-flash",effort:"medium"},tasks:[{id:$p,text:"original split task",execute:false}]},codemap_version:1,map_prepass_version:1,map_prepass_authorized:{kind:"opencode",model:"google/gemini-3.8-flash",effort:"medium",source:"config"},map_prepasses:{($p):{status:"complete",reviewed:false}},task_id:$p,task_idx:0,round:1,phase:"map_review",status:"questions",members:[],answers:[],results:[{task:"earlier",outcome:"consensus",text:"keep this"}],pending_questions:[{id:"map-decision",task:$p,member:"orchestrator",question:"review"}],notices:[],last_votes:[]}' >"$split_resume_run/state.json"
  : >"$FAKE_MAP_ARGS"; : >"$FAKE_MAP_CALLS"
  real_split_rejection() { "$dispatch_script" resume --run-dir "$split_resume_run" --map-decision "$scratch/real-split-decision.json" 2>&1; }
  check 4 'T4 real split resume rejects sibling-created prerequisite' 'subtask A requires generated.txt created by sibling B' real_split_rejection
  check 0 'T4 rejected split remains at map-review with original task/result intact' true jq -e '.phase=="map_review" and (.config.tasks|length)==1 and .results[0].text=="keep this"' "$split_resume_run/state.json"
  check 0 'T4 rejected split makes zero inference calls' '' test ! -s "$FAKE_MAP_CALLS"

  # Map review is resumable and an explicit keep decision closes the checkpoint without a round.
  RUN="$scratch/review-run"; ST="$RUN/state.json"; mkdir -p "$RUN/map/$(map_key parent)"
  jq -n '{config:{dir:"/tmp",tasks:[{id:"parent",text:"original",execute:false}]},task_id:"parent",task_idx:0,
    phase:"map_review",status:"questions",map_prepasses:{parent:{status:"unavailable"}},pending_questions:[]}' >"$ST"
  echo '{"action":"keep"}' >"$scratch/keep.json"; MAP_DECISION_FILE="$scratch/keep.json"; render_transcript() { :; }
  check 0 'map decision keep is accepted at the review checkpoint' '' apply_map_decision
  check 0 'map review is durably closed with user keep decision' true jq -r '.map_prepasses.parent.reviewed and .map_prepasses.parent.decision.action=="keep"' "$ST"

  # Replacement transaction: same-parent child IDs are reusable, but unrelated task/result
  # identities remain reserved; unrelated results are preserved and replaced child outcomes archived.
  project_root="$scratch/split-project"; mkdir -p "$project_root"
  RUN="$scratch/split-replace-run"; ST="$RUN/state.json"; mkdir -p "$RUN/map/$(map_key parent)"
  printf '{"map_seed_id":"seed-input"}\n' >"$RUN/map/$(map_key parent)/coverage.json"
  printf '{"original_task":{"id":"parent","text":"original parent","execute":false}}\n' >"$RUN/map/$(map_key parent)/input.json"
  printf '{"schema_version":1,"parent_id":"parent","map_seed_id":"seed-input","subtasks":[{"id":"child-a","text":"corrected child","execute":false,"requires":[],"modifies":[],"deletes":[],"creates":[],"acceptance":[],"unresolved":[]}]}\n' >"$scratch/replacement-contract.json"
  printf '{"action":"split","contract_file":"replacement-contract.json"}\n' >"$scratch/replacement-decision.json"
  jq -n --arg p "$project_root" --arg old "$RUN/map/$(map_key parent)/old-contract.json" --arg ident "$RUN/map/$(map_key parent)/old-identities.json" \
    '{config:{dir:$p,tasks:[{id:"done",text:"earlier task",execute:false},{id:"parent",text:"original parent",execute:false},{id:"child-a",map_parent:"parent",text:"old child",execute:false}]},
      task_id:"parent",task_idx:1,phase:"split_invalid",status:"questions",round:3,candidate:{id:"old-candidate",text:"superseded proposal"},voted_candidate:{id:"old-vote",text:"superseded vote"},fixes:[{member:"A"}],codemap_pending:{attempt:"old-attempt"},members:[{id:"A",inflight:{tag:"old"}},{id:"B",inflight:{tag:"old"}}],pending_questions:[],answers:[],last_votes:[{member:"A",vote:"disagree"}],reuse_posts:true,
      results:[{task:"done",outcome:"consensus",text:"earlier result",candidate:"done-c1"},{task:"child-a",outcome:"ratified",text:"old outcome",implementation:"old implementation"}],
      map_prepasses:{parent:{status:"complete",reviewed:true,approved_contract:{digest:"old-digest",contract_file:$old,identity_file:$ident},execution_started:{"child-a":{contract_digest:"old-digest"}}}}}' >"$ST"
  echo old-approved-contract >"$RUN/map/$(map_key parent)/old-contract.json"; echo old-identities >"$RUN/map/$(map_key parent)/old-identities.json"
  mkdir -p "$RUN/prompts" "$RUN/posts" "$RUN/raw"
  echo 'old prompt' >"$RUN/prompts/child-a-r2-A.md"; echo 'old post' >"$RUN/posts/child-a-r2-A.md"; echo '{"vote":"agree"}' >"$RUN/posts/child-a-r2-A.json"; echo 'old raw' >"$RUN/raw/child-a-r2-A.md"
  # Old-contract round-1 posts under the reused child ID: an ordinary resume must never reuse them.
  for m in A B; do echo 'old contract r1 post' >"$RUN/posts/child-a-r1-$m.md"; echo '{"vote":"propose","proposal":"stale old-contract plan","questions":[]}' >"$RUN/posts/child-a-r1-$m.json"; done
  MAP_DECISION_FILE="$scratch/replacement-decision.json"; render_transcript() { :; }
  real_mv=$(command -v mv); archive_failbin="$scratch/archive-fail-bin"; mkdir -p "$archive_failbin"
  cat >"$archive_failbin/mv" <<'SH'
#!/bin/sh
case "$*" in *state.json) exit 1;; esac
exec "$REAL_MV" "$@"
SH
  chmod +x "$archive_failbin/mv"; export REAL_MV="$real_mv"
  original_path=$PATH; PATH="$archive_failbin:$PATH"
  check 4 'split replacement state-publication failure remains checkpointed after archiving' '' apply_map_decision
  PATH=$original_path
  check 0 'failed split replacement preserves old checkpoint and completed result' 'split_invalid' jq -c '{phase,approved:.map_prepasses.parent.approved_contract.digest,results}' "$ST"
  check 0 'failed split replacement has already archived stale posts safely' true sh -c 'test -s "$1/map/$(printf parent|shasum -a 256|cut -c1-16)/superseded/old-digest/posts/child-a-r2-A.md" && echo true' _ "$RUN"
  check 0 'split replacement accepts its own prior child id' '' apply_map_decision
  check 0 'split replacement preserves unrelated completed result as a result record' true jq -e '.results==[{task:"done",outcome:"consensus",text:"earlier result",candidate:"done-c1"}] and (.results[0]|has("config")|not)' "$ST"
  check 0 'split replacement archives superseded child outcome' true jq -e '.result_history[-1].results[0].implementation=="old implementation"' "$ST"
  check 0 'split replacement history keeps the superseded contract identity' old-digest jq -r '.result_history[-1].contract_identity' "$ST"
  check 0 'split replacement history preserves old approval and baseline identity metadata' true jq -e '.result_history[-1].approved_contract.digest=="old-digest" and .result_history[-1].baseline_identities==.result_history[-1].approved_contract.identity_file' "$ST"
  check 0 'split replacement resets prior execution authorization' true jq -e '.map_prepasses.parent.execution_started=={}' "$ST"
  check 0 'split replacement resets round and superseded candidate/vote/fixes' true jq -e '.round==1 and .candidate==null and .voted_candidate==null and .fixes==[] and .last_votes==[] and .reuse_posts==false' "$ST"
  check 0 'split replacement clears pending codemap and in-flight launch bookkeeping' true jq -e '.codemap_pending==null and .members[0].inflight==null' "$ST"
  N=2; MAXR=2
  replacement_task='{"id":"child-a","text":"corrected child","execute":false}'
  CODEMAP=0; MAP_PREPASS=0; DIR="$project_root"; EXEC=""
  real_launch_collect=$(declare -f launch collect)
  launch() { local i=$1 pf=$2 tag=$3; printf '%s\n' "$i:$tag:$pf" >>"$RUN/fresh-launches"; sts --argjson i "$i" --arg t "$tag" '.members[$i].inflight={tag:$t}'; }
  collect() { local i=$1 id tag; id=$(mid "$i"); tag=$(st ".members[$i].inflight.tag"); printf '%s\n' 'Fresh replacement plan.' '```json' '{"vote":"propose","proposal":"corrected plan","questions":[]}' '```' >"$RUN/raw/$tag-$id.md"; }
  # Ordinary resume after a post-commit failure sets reuse_posts=true (council.sh resume path).
  sts '.status="running" | .last_votes=[] | .reuse_posts=true | .members |= map(.inflight=null)'
  replacement_resume_step() { run_step r1 prompt_round1 "$replacement_task" 1 >"$scratch/replacement-resume.log" 2>&1; }
  check 0 'replacement executes the production run_step path with fresh member launches' '' replacement_resume_step
  check 0 'resume after replacement never reuses superseded r1 posts' '' sh -c 'test -f "$1" && ! grep -q "reusing its r1 post" "$1"' _ "$scratch/replacement-resume.log"
  check 0 'resume after replacement launches every member fresh' 2 sh -c 'grep -c ":child-a-r1:" "$1"' _ "$RUN/fresh-launches"
  check 0 'superseded r1 posts exist only in the old-contract archive' true sh -c 'a="$1/map/$(printf parent|shasum -a 256|cut -c1-16)/superseded/old-digest/posts"; grep -q "old contract r1 post" "$a/child-a-r1-A.md" && grep -q "old contract r1 post" "$a/child-a-r1-B.md" && ! grep -q "stale old-contract" "$1/posts/child-a-r1-A.json" && echo true' _ "$RUN"
  check 0 'replacement regenerates round-1 prompt for member A' 'round 1' grep -i 'round 1' "$RUN/prompts/child-a-r1-A.md"
  check 0 'replacement regenerates round-1 prompt for member B' 'round 1' grep -i 'round 1' "$RUN/prompts/child-a-r1-B.md"
  check 0 'replacement fresh prompts carry corrected contract task text' 'corrected child' grep -F 'corrected child' "$RUN/prompts/child-a-r1-A.md"
  check 0 'replacement fresh posts agree on one exact candidate' true jq -e '.last_votes|length==2 and all(.[];.vote=="propose" and .proposal=="corrected plan")' "$ST"
  check 0 'replacement first-round prompts exclude the superseded candidate' '' sh -c '! grep -q "superseded proposal" "$1" "$2"' _ "$RUN/prompts/child-a-r1-A.md" "$RUN/prompts/child-a-r1-B.md"
  check 0 'split replacement archives old prompts, posts, raw output, and old contract identity' true sh -c 'test -s "$1/map/$(printf parent|shasum -a 256|cut -c1-16)/superseded/old-digest/prompts/child-a-r2-A.md" && test -s "$1/map/$(printf parent|shasum -a 256|cut -c1-16)/superseded/old-digest/posts/child-a-r2-A.json" && test -s "$1/map/$(printf parent|shasum -a 256|cut -c1-16)/superseded/old-digest/raw/child-a-r2-A.md" && echo true' _ "$RUN"
  check 0 'replacement retires superseded live post and raw names before fresh prompts' true sh -c '! test -e "$1/posts/child-a-r2-A.md" && ! test -e "$1/posts/child-a-r2-A.json" && ! test -e "$1/raw/child-a-r2-A.md" && test -s "$1/prompts/child-a-r1-A.md" && echo true' _ "$RUN"
  check 0 'split replacement binds child identity to the new contract digest' true jq -e '.config.tasks[1].contract_identity==.map_prepasses.parent.approved_contract.digest' "$ST"
  check 0 'prior approved contract artifact remains preserved on disk' old-approved-contract grep -F old-approved-contract "$RUN/map/$(map_key parent)/old-contract.json"
  eval "$real_launch_collect"

  # Reusing an old child ID is legal, but a different task's completed result remains reserved.
  RUN="$scratch/split-result-collision-run"; ST="$RUN/state.json"; mkdir -p "$RUN/map/$(map_key parent)"
  printf '{"map_seed_id":"seed-input"}\n' >"$RUN/map/$(map_key parent)/coverage.json"
  printf '{"schema_version":1,"parent_id":"parent","map_seed_id":"seed-input","subtasks":[{"id":"done","text":"collision","execute":false,"requires":[],"modifies":[],"deletes":[],"creates":[],"acceptance":[],"unresolved":[]}]}\n' >"$scratch/result-collision-contract.json"
  printf '{"action":"split","contract_file":"result-collision-contract.json"}\n' >"$scratch/result-collision-decision.json"
  jq -n --arg d "$project_root" '{config:{dir:$d,tasks:[{id:"parent",text:"parent",execute:false}]},task_id:"parent",task_idx:0,phase:"map_review",status:"questions",pending_questions:[{id:"map-decision"}],results:[{task:"done",outcome:"consensus",text:"already completed"}],map_prepasses:{parent:{reviewed:false}}}' >"$ST"
  MAP_DECISION_FILE="$scratch/result-collision-decision.json"
  result_collision() { apply_map_decision 2>&1; }
  check 4 'split rejects unrelated completed-result ID collision' 'split child id collides with an existing result' result_collision
  check 0 'result collision leaves map-review tasks and completed result intact' true jq -e '.phase=="map_review" and .config.tasks==[{id:"parent",text:"parent",execute:false}] and .results[0].text=="already completed"' "$ST"

  # Invalid replacement leaves the approved children/results untouched and can then resolve by keep.
  RUN="$scratch/split-keep-run"; ST="$RUN/state.json"; mkdir -p "$RUN/map/$(map_key parent)"
  printf '{"original_task":{"id":"parent","text":"original parent","execute":false}}\n' >"$RUN/map/$(map_key parent)/input.json"
  printf '{"map_seed_id":"seed-input"}\n' >"$RUN/map/$(map_key parent)/coverage.json"
  printf '{"schema_version":1,"parent_id":"parent","map_seed_id":"seed-input","subtasks":[]}\n' >"$scratch/invalid-replacement.json"
  printf '{"action":"split","contract_file":"invalid-replacement.json"}\n' >"$scratch/invalid-replacement-decision.json"
  jq -n --arg d "$project_root" '{config:{dir:$d,tasks:[{id:"parent",text:"parent",execute:false},{id:"child-a",map_parent:"parent",text:"existing",execute:false}]},task_id:"parent",task_idx:0,round:4,candidate:{text:"old candidate"},voted_candidate:{text:"old vote"},fixes:[{member:"A"}],codemap_pending:{attempt:"old-keep-attempt"},phase:"split_invalid",status:"questions",pending_questions:[{id:"split",task:"parent"}],last_votes:[{member:"A",vote:"disagree"}],reuse_posts:true,results:[{task:"child-a",outcome:"consensus",text:"retained"}],map_prepasses:{parent:{reviewed:true,approved_contract:{digest:"kept-digest"}}}}' >"$ST"
  mkdir -p "$RUN/posts" "$RUN/prompts" "$RUN/raw"
  echo 'keep stale post' >"$RUN/posts/child-a-r4-A.md"; echo 'keep stale prompt' >"$RUN/prompts/child-a-r4-A.md"
  echo 'keep stale r1 post' >"$RUN/posts/child-a-r1-A.md"; echo '{"vote":"propose","proposal":"stale","questions":[]}' >"$RUN/posts/child-a-r1-A.json"
  MAP_DECISION_FILE="$scratch/invalid-replacement-decision.json"
  invalid_replacement() { apply_map_decision 2>&1; }
  check 4 'invalid same-id replacement stays at split-resolution checkpoint' 'subtasks must be a non-empty array' invalid_replacement
  check 0 'invalid replacement preserves task list and results' true jq -e '.phase=="split_invalid" and .config.tasks[1].id=="child-a" and .results[0].text=="retained" and .map_prepasses.parent.approved_contract.digest=="kept-digest"' "$ST"
  printf '{"action":"keep"}\n' >"$scratch/keep-resolution.json"; MAP_DECISION_FILE="$scratch/keep-resolution.json"
  check 0 'split_invalid resolves with keep after rejected replacement' '' apply_map_decision
  check 0 'keep archives child result history and restores parent task' true jq -e '.config.tasks[0].id=="parent" and .results==[] and .result_history[-1].results[0].text=="retained"' "$ST"
  check 0 'keep resolution resets the superseded deliberation checkpoint' true jq -e '.round==1 and .candidate==null and .voted_candidate==null and .fixes==[] and .reuse_posts==false and .codemap_pending==null' "$ST"
  check 0 'keep history records old contract identity and approval metadata' true jq -e '.result_history[-1].contract_identity=="kept-digest" and .result_history[-1].approved_contract.digest=="kept-digest"' "$ST"
  keep_archive="$RUN/map/$(map_key parent)/superseded/kept-digest/posts/child-a-r4-A.md"
  check 0 'keep resolution archives stale child posts before restoring the parent' true sh -c 'test -s "$1" && echo true' _ "$keep_archive"
  check 0 'keep resolution retires stale child r1 posts from reusable live names' true sh -c 'a="$1/map/$(printf parent|shasum -a 256|cut -c1-16)/superseded/kept-digest/posts"; test -s "$a/child-a-r1-A.json" && ! test -e "$1/posts/child-a-r1-A.json" && ! test -e "$1/posts/child-a-r1-A.md" && echo true' _ "$RUN"

  # Child entry validates approved contract/task identity and untouched prerequisites before launch.
  gate_root="$scratch/gate-project"; mkdir -p "$gate_root"; printf 'baseline\n' >"$gate_root/input.txt"; printf 'immutable prerequisite\n' >"$gate_root/keep.txt"
  baseline_sha=$(shasum -a 256 <"$gate_root/input.txt" | cut -d' ' -f1); keep_sha=$(shasum -a 256 <"$gate_root/keep.txt" | cut -d' ' -f1)
  RUN="$scratch/gate-run"; ST="$RUN/state.json"; mkdir -p "$RUN/map/$(map_key parent)"
  gate_contract="$RUN/map/$(map_key parent)/contract.json"
  jq -n --arg sha "$baseline_sha" --arg keep "$keep_sha" '{schema_version:1,parent_id:"parent",map_seed_id:"seed",subtasks:[{id:"child-a",text:"modify the project",execute:true,requires:[{path:"input.txt",sha256:$sha},{path:"keep.txt",sha256:$keep}],modifies:[{path:"input.txt",sha256:$sha}],deletes:[],creates:["output.txt"],acceptance:[],unresolved:[]}]}' >"$gate_contract"
  gate_identities="$RUN/map/$(map_key parent)/identities.json"
  check 0 'split child approval fixture validates and captures identities' '' python3 "$HERE/council_splitcheck.py" --root "$gate_root" --contract "$gate_contract" --parent-id parent --map-seed-id seed --identity-output "$gate_identities"
  gate_digest=$(shasum -a 256 <"$gate_contract" | cut -d' ' -f1); gate_identity_digest=$(shasum -a 256 <"$gate_identities" | cut -d' ' -f1)
  jq -n --arg d "$gate_root" --arg c "$gate_contract" --arg idf "$gate_identities" --arg cd "$gate_digest" --arg id "$gate_identity_digest" --arg sha "$baseline_sha" --arg keep "$keep_sha" '{config:{dir:$d,tasks:[{id:"child-a",map_parent:"parent",contract_identity:$cd,text:"modify the project",execute:true,requires:[{path:"input.txt",sha256:$sha},{path:"keep.txt",sha256:$keep}],modifies:[{path:"input.txt",sha256:$sha}],deletes:[],creates:["output.txt"],acceptance:[],unresolved:[]}]},task_id:"child-a",task_idx:0,phase:"plan",status:"running",members:[{id:"A",kind:"opencode",model:"test/model",effort:"high",agent:"build",session:null,gen:1,inflight:null}],map_prepass_version:1,map_prepass_authorized:{kind:"opencode",model:"google/gemini-3.8-flash",effort:"medium"},map_prepasses:{parent:{reviewed:true,coverage:{map_seed_id:"seed"},approved_contract:{digest:$cd,parent_id:"parent",seed_id:"seed",contract_file:$c,identity_file:$idf,identity_digest:$id},execution_started:{}}}}' >"$ST"
  DIR="$gate_root"; MAP_PREPASS=1; N=1; EXEC=A; render_transcript() { :; }
  check 0 'child gate accepts unchanged approved baseline before execution' '' validate_child_launch
  sts --arg p parent --arg c child-a --arg d "$gate_digest" '.map_prepasses[$p].execution_started[$c]={contract_digest:$d,started_at:(now|floor)} | .phase="ratify"'
  printf 'executor changed authorized file\n' >"$gate_root/input.txt"; printf 'executor created authorized output\n' >"$gate_root/output.txt"
  check 0 'entered execution permits declared modifications and creates during ratification' '' validate_child_launch
  gate_launch_calls="$scratch/gate-launch-calls"; : >"$gate_launch_calls"
  cat >"$scratch/gate-oc" <<'SH'
#!/bin/sh
echo "$*" >>"$GATE_LAUNCH_CALLS"
exit 90
SH
  chmod +x "$scratch/gate-oc"; OC="$scratch/gate-oc"; export GATE_LAUNCH_CALLS="$gate_launch_calls"
  printf 'changed after approval\n' >"$gate_root/keep.txt"
  gate_launch() { launch 0 "$RUN/prompts/gate.md" gate 2>&1; }
  check 4 'baseline drift blocks child launch before inference' '' gate_launch
  check 0 'baseline drift checkpoint persists split_invalid' split_invalid jq -r '.phase' "$ST"
  check 0 'baseline drift checkpoint names changed baseline in its diagnostic' baseline jq -r '.pending_questions[0].question' "$ST"
  check 0 'baseline drift gate prevents all adapter calls' '' test ! -s "$gate_launch_calls"
  gate_handover() { do_handover 0 2>&1; }
  check 4 'baseline drift also blocks the handover launch path' 'context' gate_handover
  check 0 'handover baseline gate makes zero adapter calls' '' test ! -s "$gate_launch_calls"

  # Exercise the real resume dispatcher for mapped parents, inherited child scope, and both legacy formats.
  fake_resume_oc="$scratch/fake-resume-oc"; resume_calls="$scratch/resume-inference-calls"
  cat >"$fake_resume_oc" <<'SH'
#!/bin/sh
case "$1" in
  ensure) exit 0 ;;
  *) echo "$*" >>"$RESUME_CALLS"; exit 91 ;;
esac
SH
  chmod +x "$fake_resume_oc"; export RESUME_CALLS="$resume_calls"; : >"$resume_calls"
  make_answer_run() {
    local target=$1 type=$2 run_dir="$scratch/resume-$1-$2"
    mkdir -p "$run_dir/posts"; RUN="$run_dir"; ST="$RUN/state.json"
    if [ "$type" = mapped ]; then
      jq -n --arg task "$target" --arg d "$scratch" '{config:{dir:$d,max_rounds:2,timeout_s:30,handover_at:0.5,max_turns:3,executor:null,map_code:true,map_prepass:{kind:"opencode",model:"google/gemini-3.8-flash",effort:"medium"},tasks:[{id:$task,map_parent:(if $task=="child-a" then "parent" else null end),text:"pending mapped question",execute:false}]},codemap_version:1,codemap_pending:{task:$task},map_prepass_version:1,map_prepass_authorized:{kind:"opencode",model:"google/gemini-3.8-flash",effort:"medium",source:"config"},map_prepasses:{parent:{scope_applicability:"known"},($task):{scope_applicability:"known"}},task_id:$task,task_idx:1,round:1,phase:"plan",status:"questions",members:[],answers:[],results:[],pending_questions:[{id:"q1",task:$task,member:"A",question:"clarify scope"}],notices:[],last_votes:[]}' >"$ST"
    else
      jq -n --arg task "$target" --arg d "$scratch" --argjson cv "$([ "$type" = codemap ] && echo 1 || echo null)" '({config:{dir:$d,max_rounds:2,timeout_s:30,handover_at:0.5,max_turns:3,executor:null,tasks:[{id:$task,text:"legacy pending question",execute:false}],members:[{id:"A",kind:"claude",model:"old/model",effort:"high",mode:"read",permission_mode:"plan",gen:1,session:null,session_tokens:0,session_cost:0,retired:[],calls:0,session_calls:0,ctx_used:0,ctx_limit:10000},{id:"B",kind:"claude",model:"old/other",effort:"high",mode:"read",permission_mode:"plan",gen:1,session:null,session_tokens:0,session_cost:0,retired:[],calls:0,session_calls:0,ctx_used:0,ctx_limit:10000}]},task_id:$task,task_idx:1,round:1,phase:"plan",status:"questions",members:[{id:"A",kind:"claude",model:"old/model",effort:"high",mode:"read",permission_mode:"plan",gen:1,session:null,session_tokens:0,session_cost:0,retired:[],calls:0,session_calls:0,ctx_used:0,ctx_limit:10000},{id:"B",kind:"claude",model:"old/other",effort:"high",mode:"read",permission_mode:"plan",gen:1,session:null,session_tokens:0,session_cost:0,retired:[],calls:0,session_calls:0,ctx_used:0,ctx_limit:10000}],answers:[],results:[],pending_questions:[{id:"q1",task:$task,member:"A",question:"legacy clarify"}],notices:[],last_votes:[]} + (if $cv==1 then {codemap_version:1} else {} end))' >"$ST"
    fi
    printf '{"pending":true}\n' >"$RUN/questions.json"
  }
  resume_real_answer() { OC="$fake_resume_oc" "$HERE/council.sh" resume --run-dir "$RUN" --answer "scope answer"; }
  make_answer_run mapped mapped
  check 0 'mapped original task answer resolves through real resume dispatch' '' resume_real_answer
  check 0 'mapped original answer is stored once and scope becomes unknown' true jq -e '.answers|length==1' "$ST"
  check 0 'mapped original seed applicability is invalidated after answer' 'unknown after scope-changing answers' jq -r '.map_prepasses.mapped.scope_applicability' "$ST"
  check 0 'mapped answer clears pending state only after persistence' null jq -r '.pending_questions // "null"' "$ST"
  printf '{"q1":"inherited scope answer"}\n' >"$scratch/inherited-answers.json"
  make_answer_run child-a mapped
  check 0 'split child --answers resolves through real resume dispatch' '' env OC="$fake_resume_oc" "$HERE/council.sh" resume --run-dir "$RUN" --answers "$scratch/inherited-answers.json"
  check 0 'split child answer invalidates parent seed applicability' 'unknown after scope-changing answers' jq -r '.map_prepasses.parent.scope_applicability' "$ST"
  for legacy in codemap no-codemap; do
    make_answer_run legacy "$legacy"
    check 0 "legacy $legacy answer resolves through real resume dispatch" '' resume_real_answer
    check 0 "legacy $legacy answer is stored exactly once" true jq -e '.answers|length==1' "$ST"
    check 0 "legacy $legacy adds no map pre-pass fields" true jq -e '([keys[]|select(startswith("map_"))]|length)==0' "$ST"
    make_answer_run legacy "$legacy"
    legacy_replace() { "$dispatch_script" resume --run-dir "$RUN" --answer "replacement resolution" --replace A=claude:sonnet:high 2>&1; }
    check 0 "legacy $legacy replacement resumes without mapper calls" '' legacy_replace
    check 0 "legacy $legacy replacement adds no map pre-pass state" true jq -e '([keys[]|select(startswith("map_"))]|length)==0' "$ST"
    check 0 "legacy $legacy replacement reaches the expected new member generation" sonnet jq -r '.members[0].model' "$ST"
  done
  check 0 'resume answer paths issue no inference calls' '' test ! -s "$resume_calls"

  # A failed atomic state replacement must retain the question checkpoint/posts and never continue.
  make_answer_run mapped mapped
  printf 'keep this post\n' >"$RUN/posts/mapped-r1-A.md"
  real_mv=$(command -v mv); failbin="$scratch/fail-mv-bin"; mkdir -p "$failbin"
  cat >"$failbin/mv" <<'SH'
#!/bin/sh
case "$*" in *state.json) exit 1 ;; esac
exec "$REAL_MV" "$@"
SH
  chmod +x "$failbin/mv"; export REAL_MV="$real_mv"
  failed_answer_write() { env PATH="$failbin:$PATH" OC="$fake_resume_oc" "$HERE/council.sh" resume --run-dir "$RUN" --answer "do not continue" 2>&1; }
  check 4 'failed answer state write exits at the durable checkpoint' 'could not persist answers' failed_answer_write
  check 0 'failed answer write preserves pending state and durable question file' '' test -s "$RUN/questions.json"
  check 0 'failed answer write leaves answers and pending state unchanged' true jq -e '.answers==[] and .pending_questions[0].id=="q1"' "$ST"
  check 0 'failed answer write preserves posts and performs no inference' 'keep this post' grep -F 'keep this post' "$RUN/posts/mapped-r1-A.md"
  check 0 'failed answer write made no inference calls' '' test ! -s "$resume_calls"

  # A completed mapper response is captured by the orchestrator once and reused on resume.
  project="$scratch/map-project"; mkdir -p "$project/src"; printf 'first\nsecond\n' >"$project/src/a.py"
  project_ci=$(python3 "$HERE/council_map_prepass.py" scan-escapes --root "$project" | jq -c .case_insensitive)   # the probe's answer for this filesystem, as dispatch persists it
  mapper_run="$scratch/mapper-run"; mkdir -p "$mapper_run"; RUN="$mapper_run"; ST="$RUN/state.json"
  cat >"$scratch/fake-map-oc" <<'SH'
#!/bin/bash
cmd=$1; shift
case "$cmd" in
  ensure) exit 0 ;;
  new) echo "$@" >>"$FAKE_MAP_ARGS"; echo ses_map_fixture ;;
  prompt) echo prompt >>"$FAKE_MAP_CALLS"; echo "prompt-args $@" >>"$FAKE_MAP_ARGS" ;;
  wait) echo "wait-args $@" >>"$FAKE_MAP_ARGS"; [ "$FAKE_MAP_WAIT" = timeout ] && exit 2; [ "$FAKE_MAP_WAIT" = blocked ] && exit 3; echo idle ;;
  result) echo "result $*" >>"$FAKE_MAP_ARGS"; if [ -n "$FAKE_MAP_RESULT_COUNT" ]; then n=0; [ ! -f "$FAKE_MAP_RESULT_COUNT" ] || n=$(cat "$FAKE_MAP_RESULT_COUNT"); n=$((n+1)); echo "$n" >"$FAKE_MAP_RESULT_COUNT"; [ "$n" -eq 1 ] || { echo 'second retrieval forbidden' >&2; exit 91; }; fi; cat "$FAKE_MAP_RESPONSE" ;;
  interrupt) echo interrupt >>"$FAKE_MAP_CALLS" ;;
  api) case "$2" in /api/model*) cat "$FAKE_MAP_MODELS" ;; *) echo '{"data":{"tokens":{"input":1,"output":1,"reasoning":0,"cache":{"read":0,"write":0}},"cost":0}}' ;; esac ;;
  messages) echo '{"data":[]}' ;;
  *) exit 90 ;;
esac
SH
  chmod +x "$scratch/fake-map-oc"; OC="$scratch/fake-map-oc"; FAKE_MAP_CALLS="$scratch/fake-map-calls"; FAKE_MAP_ARGS="$scratch/fake-map-args"; export FAKE_MAP_CALLS FAKE_MAP_ARGS
  FAKE_MAP_WAIT=idle; export FAKE_MAP_WAIT
  FAKE_MAP_MODELS="$scratch/map-models.json"; printf '{"data":[{"enabled":true,"providerID":"google","id":"gemini-3.8-flash","variants":[{"id":"medium"},{"id":"high"}],"limit":{"context":100000}}]}\n' >"$FAKE_MAP_MODELS"; export FAKE_MAP_MODELS
  printf '{"candidates":[{"path":"src/a.py","lines":[2,2]}],"unresolved":[],"stopped_reason":"done"}\n' >"$scratch/map-response.json"
  FAKE_MAP_RESPONSE="$scratch/map-response.json"; export FAKE_MAP_RESPONSE
  jq -n --arg d "$project" '{config:{dir:$d,map_code:true,map_prepass:{model:"google/gemini-3.8-flash",effort:"medium",timeout_s:120,max_output_bytes:65536}},map_prepass_version:1,map_prepass_authorized:{source:"config"},map_prepasses:{}}' >"$ST"
  MAP_PREPASS=1; DIR="$project"; CM="$HERE/council_codemap.py"
  task='{"id":"mapped","text":"inspect src/a.py","execute":false}'
  map_prepass_resume_count() { map_prepass_real "$task"; wc -l <"$FAKE_MAP_CALLS" | tr -d ' '; }
  check 0 'T6 valid mapper output is captured through the existing orchestrator ingestion' '' map_prepass_real "$task"
  check 0 'T6 mapper state records terminal completion' complete jq -r '.map_prepasses.mapped.status' "$ST"
  check 0 'T6 ingestion reports no operational capture error' '' cat "$RUN/map/$(map_key mapped)/capture.err"
  check 0 'T6 line selector seeds only its selected source line' '[2,2]' jq -c '[.entries[].lines] | first' "$RUN/codemap/index.json"
  check 0 'T6 coverage has one captured source' true jq -r '(.captured_versions_ranges|length)==1' "$RUN/map/$(map_key mapped)/coverage.json"
  check 0 'T6 coverage records captured entry id and digest' true jq -r '.captured_versions_ranges[0].entry_id!=null and .captured_versions_ranges[0].source_sha256!=null' "$RUN/map/$(map_key mapped)/coverage.json"
  real_source=$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$DIR/src/a.py")
  check 0 'T6 coverage records resolved path and byte range' true jq -r --arg d "$real_source" '.captured_versions_ranges[0].resolved_path==$d and .captured_versions_ranges[0].byte_range!=null' "$RUN/map/$(map_key mapped)/coverage.json"
  check 0 'T6 coverage records line range and capture status' true jq -r '.captured_versions_ranges[0].lines==[2,2] and .capture_statuses[0].status!=null' "$RUN/map/$(map_key mapped)/coverage.json"
  check 0 'T6 completed mapper status is reusable without a paid relaunch' 1 map_prepass_resume_count

  # A failed selector has no entry_id; it must not erase successful captures or unresolved claims.
  partial_run="$scratch/partial-capture-run"; mkdir -p "$partial_run"; RUN="$partial_run"; ST="$RUN/state.json"
  jq -n --arg d "$project" '{config:{dir:$d,map_code:true,map_prepass:{model:"google/gemini-3.8-flash",effort:"medium",timeout_s:120,max_output_bytes:65536}},map_prepass_version:1,map_prepass_authorized:{source:"config"},map_prepasses:{}}' >"$ST"
  : >"$FAKE_MAP_CALLS"; FAKE_MAP_WAIT=idle
  printf '{"candidates":[{"path":"src/a.py","lines":[1,1]},{"path":"src/a.py","lines":[99,99]}],"unresolved":[{"target":"named helper","reason":"not found"}],"stopped_reason":"searched"}\n' >"$FAKE_MAP_RESPONSE"
  partial_task='{"id":"partial","text":"inspect src/a.py","execute":false}'
  check 0 'T6 mixed valid and failed production captures complete without jq null-index failure' '' map_prepass_real "$partial_task"
  check 0 'T6 successful subset with a range failure is classified partial' partial jq -r '.map_prepasses.partial.status' "$ST"
  check 0 'T6 partial coverage retains successful digest and source range' true jq -e '.captured_versions_ranges|length==2 and any(.[]; .status=="ok" and .entry_id!=null and .source_sha256!=null)' "$RUN/map/$(map_key partial)/coverage.json"
  check 0 'T6 partial coverage preserves failed selector status and reason' true jq -e 'any(.capture_statuses[]; .status=="error" and .reason!=null)' "$RUN/map/$(map_key partial)/coverage.json"
  check 0 'T6 partial coverage preserves mapper-attributed unresolved target' 'named helper' jq -r '.mapper_unresolved.items[0].target' "$RUN/map/$(map_key partial)/coverage.json"
  check 0 'T6 partial map remains unknown coverage and retains raw response' true jq -e '.coverage=="unknown"' "$RUN/map/$(map_key partial)/coverage.json"
  check 0 'T6 partial response bytes remain archived' 'not found' grep -F 'not found' "$RUN/map/$(map_key partial)/response.txt"
  check 0 'T6 partial capture issues one prompt only' 1 grep -c '^prompt$' "$FAKE_MAP_CALLS"

  all_failed_run="$scratch/all-failed-capture-run"; mkdir -p "$all_failed_run"; RUN="$all_failed_run"; ST="$RUN/state.json"
  jq -n --arg d "$project" '{config:{dir:$d,map_code:true,map_prepass:{model:"google/gemini-3.8-flash",effort:"medium",timeout_s:120,max_output_bytes:65536}},map_prepass_version:1,map_prepass_authorized:{source:"config"},map_prepasses:{}}' >"$ST"
  : >"$FAKE_MAP_CALLS"; printf '{"candidates":[{"path":"src/a.py","lines":[99,99]}],"unresolved":[],"stopped_reason":"searched"}\n' >"$FAKE_MAP_RESPONSE"
  all_failed_task='{"id":"all-failed","text":"inspect src/a.py","execute":false}'
  check 0 'T6 all-failed production capture finalizes without jq null-index failure' '' map_prepass_real "$all_failed_task"
  check 0 'T6 zero successful captures is unavailable' unavailable jq -r '.map_prepasses["all-failed"].status' "$ST"
  check 0 'T6 all-failed coverage retains per-item error with unknown coverage' true jq -e '.coverage=="unknown" and any(.capture_statuses[]; .status=="error" and .entry_id==null)' "$RUN/map/$(map_key all-failed)/coverage.json"
  check 0 'T6 all-failed response is not retried' 1 grep -c '^prompt$' "$FAKE_MAP_CALLS"

  failed_finalize_run="$scratch/failed-finalize-run"; mkdir -p "$failed_finalize_run"; RUN="$failed_finalize_run"; ST="$RUN/state.json"
  jq -n --arg d "$project" '{config:{dir:$d,map_code:true,map_prepass:{model:"google/gemini-3.8-flash",effort:"medium",timeout_s:120,max_output_bytes:65536}},map_prepass_version:1,map_prepass_authorized:{source:"config"},map_prepasses:{"finalize-error":{status:"dispatching",input_fingerprint:"fingerprint"}}}' >"$ST"
  failure_dir="$RUN/map/$(map_key finalize-error)"; mkdir -p "$failure_dir"; printf '{"results":[{"path":"src/a.py","status":"error","reason":"out of range"}]}\n' >"$failure_dir/capture-results.json"
  printf '{"result":{"unresolved":[{"target":"missing caller","reason":"not found"}]}}\n' >"$failure_dir/validation.json"
  : >"$FAKE_MAP_CALLS"
  finalize_task='{"id":"finalize-error","text":"recover failed capture","execute":false}'
  check 0 'failure finalization handles null entry_id without jq failure' '' map_prepass_real "$finalize_task"
  check 0 'failure finalization preserves per-item capture error and unresolved target' true jq -e '.capture_statuses[0].reason=="out of range" and .mapper_unresolved.items[0].target=="missing caller"' "$failure_dir/coverage.json"
  check 0 'failure finalization keeps coverage unknown and does not redispatch' true jq -e '.coverage=="unknown"' "$failure_dir/coverage.json"
  check 0 'failure finalization never sends a second mapper prompt' '' sh -c '! grep -q "^prompt$" "$1"' _ "$FAKE_MAP_CALLS"

  # A completed recovered response is processed directly, without a second result request.
  recovered_run="$scratch/recovered-complete-run"; mkdir -p "$recovered_run"; RUN="$recovered_run"; ST="$RUN/state.json"
  jq -n --arg d "$project" --argjson ci "$project_ci" '{config:{dir:$d,map_code:true,map_prepass:{model:"google/gemini-3.8-flash",effort:"medium",timeout_s:120,max_output_bytes:65536}},map_prepass_version:1,map_prepass_authorized:{source:"config"},map_prepasses:{recovered:{status:"dispatched",session:"ses_completed",input_fingerprint:"fingerprint",boundary:{blocked_symlinks:[],case_insensitive:$ci,search_denied:false,reason:null}}},answers:[]}' >"$ST"
  : >"$FAKE_MAP_CALLS"; : >"$FAKE_MAP_ARGS"; recovery_count="$scratch/recovered-result-count"; rm -f "$recovery_count"; FAKE_MAP_RESULT_COUNT="$recovery_count"; export FAKE_MAP_RESULT_COUNT
  printf '{"candidates":[],"unresolved":[{"target":"recovered marker","reason":"kept"}],"stopped_reason":"done"}\n' >"$FAKE_MAP_RESPONSE"
  recovered_task='{"id":"recovered","text":"recover completed dispatch","execute":false}'
  check 0 'T6 recovered completed mapper result processes successfully' '' map_prepass_real "$recovered_task"
  check 0 'T6 recovered response is retrieved exactly once' 1 cat "$recovery_count"
  check 0 'T6 recovered response bytes remain published' 'recovered marker' grep -F 'recovered marker' "$RUN/map/$(map_key recovered)/response.txt"
  check 0 'T6 recovered unresolved item survives response processing' 'recovered marker' jq -r '.map_prepasses.recovered.mapper_result.unresolved[0].target' "$ST"
  check 0 'T6 repeated resume is terminal and reusable' '' map_prepass_real "$recovered_task"
  check 0 'T6 repeated resume keeps the original recovered bytes' 'recovered marker' grep -F 'recovered marker' "$RUN/map/$(map_key recovered)/response.txt"
  # D2: a recovered completed session without a persisted boundary baseline cannot be re-verified: discarded.
  RUN="$scratch/recovered-no-baseline-run"; mkdir -p "$RUN"; ST="$RUN/state.json"
  jq -n --arg d "$project" --argjson ci "$project_ci" '{config:{dir:$d,map_code:true,map_prepass:{model:"google/gemini-3.8-flash",effort:"medium",timeout_s:120,max_output_bytes:65536}},map_prepass_version:1,map_prepass_authorized:{source:"config"},map_prepasses:{recovered:{status:"dispatched",session:"ses_completed",input_fingerprint:"fingerprint"}},answers:[]}' >"$ST"
  rm -f "$recovery_count"; : >"$FAKE_MAP_CALLS"
  check 0 'D2 recovered session without a boundary baseline finalizes' '' map_prepass_real "$recovered_task"
  check 0 'D2 recovered session without a boundary baseline is discarded as unavailable' 'unavailable symlink boundary baseline missing; map discarded' jq -r '"\(.map_prepasses.recovered.status) \(.map_prepasses.recovered.failure)"' "$ST"
  check 0 'D2 recovered session without a boundary baseline captured nothing' true jq -e '.capture_statuses==[]' "$RUN/map/$(map_key recovered)/coverage.json"
  # A failing recovery retrieval must not truncate a response already published for this attempt.
  RUN="$scratch/recovered-failed-retrieval-run"; mkdir -p "$RUN/map/$(map_key recovered)"; ST="$RUN/state.json"
  jq -n --arg d "$project" --argjson ci "$project_ci" '{config:{dir:$d,map_code:true,map_prepass:{model:"google/gemini-3.8-flash",effort:"medium",timeout_s:120,max_output_bytes:65536}},map_prepass_version:1,map_prepass_authorized:{source:"config"},map_prepasses:{recovered:{status:"dispatched",session:"ses_completed",input_fingerprint:"fingerprint"}},answers:[]}' >"$ST"
  printf 'previously published response bytes\n' >"$RUN/map/$(map_key recovered)/response.txt"
  echo 1 >"$recovery_count"; : >"$FAKE_MAP_CALLS"
  check 0 'T6 failed recovery retrieval finalizes without relaunch' '' map_prepass_real "$recovered_task"
  check 0 'T6 failed recovery retrieval is recorded as unavailable' unavailable jq -r '.map_prepasses.recovered.status' "$ST"
  check 0 'T6 failed recovery retrieval keeps the published response intact' 'previously published response bytes' cat "$RUN/map/$(map_key recovered)/response.txt"
  check 0 'T6 failed recovery retrieval issues no prompt' '' sh -c '! grep -q prompt "$1"' _ "$FAKE_MAP_CALLS"
  unset FAKE_MAP_RESULT_COUNT
  check 0 'T6 completed recovery never dispatched a prompt' '' sh -c '! grep -q "^prompt$" "$1"' _ "$FAKE_MAP_CALLS"
  unset FAKE_MAP_RESULT_COUNT

  # Dispatch authorization must fail closed if its durable checkpoint cannot be written.
  dispatch_fail_run="$scratch/dispatch-checkpoint-fail-run"; mkdir -p "$dispatch_fail_run"; RUN="$dispatch_fail_run"; ST="$RUN/state.json"
  jq -n --arg d "$project" '{config:{dir:$d,map_code:true,map_prepass:{model:"google/gemini-3.8-flash",effort:"medium",timeout_s:120,max_output_bytes:65536}},map_prepass_version:1,map_prepass_authorized:{source:"config"},map_prepasses:{}}' >"$ST"
  eval "$(declare -f sts | sed 's/^sts ()/sts_real ()/')"
  sts() { case "$*" in *'status="dispatching"'*) return 1;; esac; sts_real "$@"; }
  : >"$FAKE_MAP_CALLS"; printf '{"candidates":[],"unresolved":[],"stopped_reason":"done"}\n' >"$FAKE_MAP_RESPONSE"
  dispatch_fail_task='{"id":"dispatch-fail","text":"test failed checkpoint","execute":false}'
  check 1 'mapper dispatch checkpoint write failure interrupts before prompt' '' map_prepass_real "$dispatch_fail_task"
  check 0 'dispatch checkpoint write failure makes zero prompt calls' '' sh -c '! grep -q "^prompt$" "$1"' _ "$FAKE_MAP_CALLS"
  sts() { sts_real "$@"; }

  # Timeout and malformed responses are terminal optimization failures, not council failures/retries.
  timeout_run="$scratch/timeout-run"; mkdir -p "$timeout_run"; RUN="$timeout_run"; ST="$RUN/state.json"
  jq -n --arg d "$project" '{config:{dir:$d,map_code:true,map_prepass:{model:"google/gemini-3.8-flash",effort:"medium",timeout_s:120,max_output_bytes:65536}},map_prepass_version:1,map_prepass_authorized:{source:"config"},map_prepasses:{}}' >"$ST"
  : >"$FAKE_MAP_CALLS"; FAKE_MAP_WAIT=timeout
  timed_task='{"id":"timeout","text":"inspect src/a.py","execute":false}'
  check 0 'T6 mapper timeout is unavailable and task-level call returns normally' '' map_prepass_real "$timed_task"
  check 0 'T6 timeout status is explicitly unavailable' unavailable jq -r '.map_prepasses.timeout.status' "$ST"
  check 0 'T6 timeout interrupts the one dispatched turn' '' grep -q '^interrupt$' "$FAKE_MAP_CALLS"
  FAKE_MAP_WAIT=idle; export FAKE_MAP_WAIT
  map_timeout_resume_count() { map_prepass_real "$timed_task"; grep -c '^prompt$' "$FAKE_MAP_CALLS"; }
  check 0 'T6 timed-out pass is not automatically retried on resume' 1 map_timeout_resume_count

  malformed_run="$scratch/malformed-run"; mkdir -p "$malformed_run"; RUN="$malformed_run"; ST="$RUN/state.json"
  jq -n --arg d "$project" '{config:{dir:$d,map_code:true,map_prepass:{model:"google/gemini-3.8-flash",effort:"medium",timeout_s:120,max_output_bytes:65536}},map_prepass_version:1,map_prepass_authorized:{source:"config"},map_prepasses:{}}' >"$ST"
  : >"$FAKE_MAP_CALLS"; printf 'not json\n' >"$FAKE_MAP_RESPONSE"; FAKE_MAP_WAIT=idle
  malformed_task='{"id":"malformed","text":"inspect src/a.py","execute":false}'
  check 0 'T6 malformed response does not fail the ordinary council task path' '' map_prepass_real "$malformed_task"
  check 0 'T6 malformed response is unavailable with unknown coverage' true jq -r '.map_prepasses.malformed.status=="unavailable" and .map_prepasses.malformed.mapper_result.coverage=="unknown"' "$ST"
  check 0 'T6 malformed response does not trigger a second mapper call' 1 grep -c '^prompt$' "$FAKE_MAP_CALLS"

  recovery_run="$scratch/recovery-run"; mkdir -p "$recovery_run"; RUN="$recovery_run"; ST="$RUN/state.json"
  jq -n --arg d "$project" '{config:{dir:$d,map_code:true,map_prepass:{model:"google/gemini-3.8-flash",effort:"medium",timeout_s:120,max_output_bytes:65536}},map_prepass_version:1,map_prepass_authorized:{source:"config"},map_prepasses:{interrupted:{status:"dispatching"}},answers:[]}' >"$ST"
  : >"$FAKE_MAP_CALLS"; : >"$FAKE_MAP_ARGS"
  interrupted_task='{"id":"interrupted","text":"recover interrupted dispatch","execute":false}'
  check 0 'T6 dispatching-without-session recovery becomes unavailable' '' map_prepass_real "$interrupted_task"
  check 0 'T6 uncertain dispatch recovery is terminal and writes coverage' true jq -e '.map_prepasses.interrupted.status=="unavailable" and .map_prepasses.interrupted.artifacts.coverage!=null' "$ST"
  check 0 'T6 dispatching recovery does not launch a second prompt' '' test ! -s "$FAKE_MAP_CALLS"

  jq -n --arg d "$project" --argjson ci "$project_ci" '{config:{dir:$d,map_code:true,map_prepass:{model:"google/gemini-3.8-flash",effort:"medium",timeout_s:120,max_output_bytes:65536}},map_prepass_version:1,map_prepass_authorized:{source:"config"},map_prepasses:{recovered:{status:"dispatched",session:"ses_completed",boundary:{blocked_symlinks:[],case_insensitive:$ci,search_denied:false,reason:null}}},answers:[]}' >"$ST"
  : >"$FAKE_MAP_CALLS"; : >"$FAKE_MAP_ARGS"; FAKE_MAP_WAIT=idle; export FAKE_MAP_WAIT
  printf '{"candidates":[],"unresolved":[],"stopped_reason":"done"}\n' >"$FAKE_MAP_RESPONSE"
  recovered_task='{"id":"recovered","text":"recover completed dispatch","execute":false}'
  check 0 'T6 dispatched recovery fixture begins in dispatched state' dispatched jq -r '.map_prepasses.recovered.status' "$ST"
  check 0 'T6 dispatched completed session recovers through result without relaunch' '' map_prepass_real "$recovered_task"
  check 0 'T6 dispatched recovery wait adapter diagnostic' '' cat "$RUN/map/$(map_key recovered)/resume-wait.err"
  check 0 'T6 recovered result reaches a terminal mapper record' unavailable jq -r '.map_prepasses.recovered.status' "$ST"
  check 0 'T6 dispatched recovery sends no second prompt' '' test ! -s "$FAKE_MAP_CALLS"
  check 0 'T6 dispatched recovery adapter wait was observed' 'wait-args ses_completed' grep -F 'wait-args ses_completed' "$FAKE_MAP_ARGS"
  check 0 'T6 dispatched recovery terminal status retained' unavailable jq -r '.map_prepasses.recovered.status' "$ST"
  check 0 'T6 dispatched recovery has no failure reason' none jq -r '.map_prepasses.recovered.failure // "none"' "$ST"

  # Runtime empty, oversized and blocked-permission outcomes all finalize once without retries.
  for outcome in empty oversized blocked; do
    outcome_run="$scratch/$outcome-run"; mkdir -p "$outcome_run"; RUN="$outcome_run"; ST="$RUN/state.json"
    jq -n --arg d "$project" '{config:{dir:$d,map_code:true,map_prepass:{model:"google/gemini-3.8-flash",effort:"medium",timeout_s:120,max_output_bytes:65536}},map_prepass_version:1,map_prepass_authorized:{kind:"opencode",model:"google/gemini-3.8-flash",effort:"medium",source:"config"},map_prepasses:{}}' >"$ST"
    : >"$FAKE_MAP_CALLS"; FAKE_MAP_WAIT=idle
    if [ "$outcome" = empty ]; then : >"$FAKE_MAP_RESPONSE"
    elif [ "$outcome" = oversized ]; then python3 -c 'import sys; sys.stdout.write("x"*65537)' >"$FAKE_MAP_RESPONSE"
    else printf '{"candidates":[],"unresolved":[],"stopped_reason":"done"}\n' >"$FAKE_MAP_RESPONSE"; FAKE_MAP_WAIT=blocked; fi
    export FAKE_MAP_WAIT
    outcome_task="{\"id\":\"$outcome\",\"text\":\"map $outcome response\",\"execute\":false}"
    check 0 "T6 $outcome mapper result is a non-fatal task outcome" '' map_prepass_real "$outcome_task"
    check 0 "T6 $outcome is unavailable with unknown coverage" true jq -r --arg t "$outcome" '.map_prepasses[$t].status=="unavailable" and (.map_prepasses[$t].artifacts.coverage|type)=="string"' "$ST"
    check 0 "T6 $outcome dispatches exactly one prompt" 1 grep -c '^prompt$' "$FAKE_MAP_CALLS"
    if [ "$outcome" = blocked ]; then check 0 'T6 blocked permission is not interrupted or retried' 1 grep -c '^prompt$' "$FAKE_MAP_CALLS"; fi
  done
  check 0 'T6 mapper session setup carries --ask' '' grep -q -- '--ask' "$FAKE_MAP_ARGS"
  check 0 'T6 mapper session setup carries deny rules' '' grep -q -- '--deny' "$FAKE_MAP_ARGS"

  # Mapping remains opt-in-per-run and applies even to docs-only work and empty directories.
  for kind in docs-only empty-project; do
    enabled_root="$scratch/$kind"; mkdir -p "$enabled_root"
    [ "$kind" != docs-only ] || printf '# Documentation only\n' >"$enabled_root/README.md"
    enabled_run="$enabled_root/.council-run"; mkdir -p "$enabled_run"; RUN="$enabled_run"; ST="$RUN/state.json"; DIR="$enabled_root"
    jq -n --arg d "$enabled_root" '{config:{dir:$d,map_code:true,map_prepass:{model:"google/gemini-3.8-flash",effort:"medium",timeout_s:120,max_output_bytes:65536}},map_prepass_version:1,map_prepass_authorized:{kind:"opencode",model:"google/gemini-3.8-flash",effort:"medium",source:"config"},map_prepasses:{}}' >"$ST"
    : >"$FAKE_MAP_CALLS"; : >"$FAKE_MAP_ARGS"
    printf '{"candidates":[{"path":"missing.py"}],"unresolved":[],"stopped_reason":"done"}\n' >"$FAKE_MAP_RESPONSE"
    enabled_task="{\"id\":\"$kind\",\"text\":\"map $kind task\",\"execute\":false}"
    check 0 "T1 $kind task actually invokes mapper" '' map_prepass_real "$enabled_task"
    check 0 "T1 $kind remains configured enabled" true jq -r '.config.map_code and .map_prepass_version==1' "$ST"
    check 0 "T1 $kind records unavailable mapper output" unavailable jq -r --arg t "$kind" '.map_prepasses[$t].status' "$ST"
    check 0 "T1 $kind dispatches one mapper session and one prompt" 1 grep -c '^--dir ' "$FAKE_MAP_ARGS"
    coverage_file=$(jq -r --arg t "$kind" '.map_prepasses[$t].artifacts.coverage' "$ST")
    check 0 "T1 $kind does not claim exhaustive coverage" unknown jq -r '.coverage' "$coverage_file"
    jq --arg t "$kind" '.config.tasks=[{id:$t,text:("continue "+$t),execute:false}] | .task_idx=0 | .phase="map" | .status="running" | .map_prepasses[$t].reviewed=false' "$ST" >"$ST.tmp" && mv "$ST.tmp" "$ST"
    map_prepass_review() { local tid; tid=$(jq -r .id <<<"$1"); sts --arg t "$tid" '.map_prepasses[$t].reviewed=true | .phase="plan"'; }
    deliberate() { echo "$kind:$(st .phase)" >>"$scratch/enabled-continuation"; return 0; }
    check 0 "T1 $kind continues to ordinary deliberation after map review" '' run_tasks
    check 0 "T1 $kind reaches plan deliberation" "$kind:plan" grep -F "$kind:plan" "$scratch/enabled-continuation"
  done
  check 0 'T6 mapper discovery allows project-relative reads and denies parent paths' '' sh -c 'grep -Fq -- "--allow read:*" "$1" && grep -Fq -- "--deny read:../*" "$1"' _ "$FAKE_MAP_ARGS"
  check 0 'D1 mapper discovery no longer denies repository metadata reads' '' sh -c '! grep -Eq -- "--deny read:(\*/)?\.(git|hg|svn)" "$1"' _ "$FAKE_MAP_ARGS"
  check 0 'D1 mapper discovery no longer denies the run directory' '' sh -c '! grep -Fq -- "--deny read:$2" "$1"' _ "$FAKE_MAP_ARGS" "$RUN"
  check 0 'D1 mapper discovery allows grep and glob when no symlink escapes the project' '' sh -c 'grep -Fq -- "--allow grep" "$1" && grep -Fq -- "--allow glob" "$1" && ! grep -Eq -- "--deny (grep|glob)( |$)" "$1"' _ "$FAKE_MAP_ARGS"
  check 0 'T6 mapper rules do not silently deny ordinary hidden project sources' '' sh -c '! grep -Fq -- "$1" "$2"' _ "--deny read:$DIR/.hidden/**" "$FAKE_MAP_ARGS"
  check 0 'T6 mapper prompt carries --ask' '' grep -E 'prompt-args .*--ask' "$FAKE_MAP_ARGS"
  check 0 'T6 mapper wait carries --ask' '' grep -E 'wait-args .*--ask' "$FAKE_MAP_ARGS"
)
# Deterministic offline OpenCode adapter used by the real-process suites: council.sh always calls
# "$HERE/oc.sh", so each suite runs a scratch copy of scripts/ whose oc.sh is this file.
write_fake_oc() {
  cat >"$1" <<'SH'
#!/bin/bash
# Deterministic offline OpenCode adapter: replies depend only on the delivered prompt bytes.
F=${FAKE_OC_DIR:?}; cmd=$1; shift
echo "$cmd $*" >>"$F/calls.log"
case "$cmd" in
  ensure) exit 0 ;;
  new) n=$(( $(cat "$F/sessions" 2>/dev/null || echo 0) + 1 )); echo $n >"$F/sessions"; printf '%s\0' "$@" >"$F/new-args-$n"; echo "ses_fake$n" ;;
  prompt) sid=$1; while [ $# -gt 0 ]; do [ "$1" = --file ] && cp "$2" "$F/$sid.prompt"; shift; done ;;
  interrupt) exit 0 ;;
  wait) [ -f "$F/$1.prompt" ] || exit 1  # a session that was never prompted has no completed reply
    [ -f "$F/mapper-timeout" ] && grep -q 'Locate candidate source' "$F/$1.prompt" 2>/dev/null && exit 2; exit 0 ;;
  result) p="$F/$1.prompt"
    [ -x "$F/result-hook" ] && "$F/result-hook" "$p"
    cr=""; [ -s "$F/code-reads" ] && grep -Eq -- "$(cat "$F/code-reads-pattern")" "$p" && cr=",\"code_reads\":$(cat "$F/code-reads")"
    if [ -s "$F/fail-results" ] && ! grep -q '=== HANDOVER ===' "$p" && grep -Eq -- "$(cat "$F/fail-pattern" 2>/dev/null || echo .)" "$p"; then n=$(cat "$F/fail-results"); echo $((n-1)) >"$F/fail-results"; [ "$n" -gt 0 ] && { echo "[error] injected failure"; exit 1; }; fi
    if grep -q 'Locate candidate source' "$p"; then cat "$F/mapper-response"
    elif grep -q '=== HANDOVER ===' "$p"; then echo "Handover note for the successor."
    elif grep -q -- '— EXECUTION ===' "$p"; then printf 'Executed.\n```json\n{"vote":"done","report":"did it","questions":[]}\n```\n'
    elif grep -q -- 'FIXES REQUESTED' "$p"; then printf 'Fixed.\n```json\n{"vote":"done","report":"fixed","questions":[]}\n```\n'
    elif grep -q 'RATIFICATION' "$p" && [ -f "$F/disagree-once" ] && grep -q "$(cat "$F/disagree-once")" "$p"; then rm -f "$F/disagree-once"; printf 'Dissent.\n```json\n{"vote":"disagree","reason":"needs a fix","proposal":"1. apply the fix","questions":[]}\n```\n'
    elif grep -q 'RATIFICATION' "$p"; then printf 'Ratified.\n```json\n{"vote":"agree","reason":"ok","proposal":null,"questions":[]}\n```\n'
    elif grep -q 'round 1 of' "$p" && [ -f "$F/ask-once" ] && grep -q -- "$( [ -s "$F/ask-once" ] && cat "$F/ask-once" || echo 'second task')" "$p"; then rm -f "$F/ask-once"; printf 'Need input.\n```json\n{"vote":"question","questions":["Which greeting?"],"proposal":null}\n```\n'
    elif grep -q 'round 1 of' "$p"; then printf 'Plan.\n```json\n{"vote":"propose","proposal":"Deterministic plan.","questions":[]%s}\n```\n' "$cr"
    else printf 'Agree.\n```json\n{"vote":"agree","reason":"same","proposal":null,"questions":[]%s}\n```\n' "$cr"; fi ;;
  api) case "$2" in
      /api/model*) echo '{"data":[{"enabled":true,"providerID":"p","id":"m","variants":[{"id":"medium"}],"limit":{"context":10000}},{"enabled":true,"providerID":"google","id":"gemini-3.8-flash","variants":[{"id":"medium"}],"limit":{"context":10000}}]}' ;;
      */message*) echo '{"data":[{"type":"assistant","tokens":{"input":6000,"output":10,"reasoning":0,"cache":{"read":0,"write":0}}}]}' ;;
      /api/session/*) sid=${2#/api/session/}
        if grep -q 'Locate candidate source' "$F/$sid.prompt" 2>/dev/null && [ -f "$F/mapper-session-usage" ]; then
          [ -s "$F/mapper-session-usage" ] || exit 1; cat "$F/mapper-session-usage"; exit 0; fi
        echo '{"data":{"tokens":{"input":6000,"output":10,"reasoning":0,"cache":{"read":0,"write":0}},"cost":0.01}}' ;;
      *) echo '{"data":{"tokens":{"input":6000,"output":10,"reasoning":0,"cache":{"read":0,"write":0}},"cost":0.01}}' ;;
    esac ;;
  messages) echo '{"data":[]}' ;;
  *) exit 90 ;;
esac
SH
  chmod +x "$1"
}
# T1/T3 differential: genuine 97f4c70 runs driven by real council.sh processes through the same
# deterministic offline adapter, then compared file-for-file with the current script. Both
# implementations run from one fixed path under one frozen clock (a PATH date shim for the shell and
# a sitecustomize time.time for the Python tools), so every byte is compared exactly, with no
# timestamp normalisation.
legacy_differential_tests() (
  D="$scratch/legacy-diff"; mkdir -p "$D/base" "$D/proj"
  git -C "$HERE/.." archive 97f4c70 scripts | tar -x -C "$D/base" || exit 1
  mkdir -p "$D/cur"; cp -R "$HERE" "$D/cur/" || exit 1
  write_fake_oc "$D/fake-oc"
  mkdir -p "$D/bin"; real_date=$(command -v date)
  # Frozen clock: every `date` call of either implementation sees the same instant.
  printf '#!/bin/sh\nif "%s" -r 0 >/dev/null 2>&1; then exec "%s" -r 1700000000 "$@"; else exec "%s" -d @1700000000 "$@"; fi\n' "$real_date" "$real_date" "$real_date" >"$D/bin/date"
  chmod +x "$D/bin/date"
  # The same frozen instant for the Python map tools (time.time), via sitecustomize on PYTHONPATH.
  mkdir -p "$D/pyclock"; printf 'import time\ntime.time = lambda: 1700000000.0\n' >"$D/pyclock/sitecustomize.py"
  chmod +x "$D/fake-oc"; cp "$D/fake-oc" "$D/base/scripts/oc.sh"; cp "$D/fake-oc" "$D/cur/scripts/oc.sh"
  cat >"$D/cmp.py" <<'PY2'
import os,sys
def files(root):
    out={}
    for d,_,fs in os.walk(root):
        for f in fs:
            p=os.path.join(d,f); out[os.path.relpath(p,root)]=p
    return out
a,b=files(sys.argv[1]),files(sys.argv[2]); bad=0
for k in sorted(set(a)|set(b)):
    if k not in a or k not in b: print('ONLY', 'base' if k in a else 'cur', k); bad=1; continue
    if open(a[k],'rb').read()!=open(b[k],'rb').read(): print('DIFF',k); bad=1
print('compared %d files' % len(a)); sys.exit(bad)
PY2
  # run_impl IMPL LABEL SNAPSHOT-RUN SNAPSHOT-ADAPTER -- steps separated by '::' (each: council.sh args)
  run_impl() {
    local impl=$1 out=$2 srun=$3 sfo=$4; shift 5
    rm -rf "$D/impl" "$D/run" "$D/fo"; mkdir -p "$D/impl"; cp -R "$D/$impl/scripts" "$D/impl/"
    [ -z "$srun" ] || cp -Rp "$srun" "$D/run"; cp -Rp "$sfo" "$D/fo"; : >"$D/fo/calls.log"
    local n=0 args=()
    while :; do
      if [ $# -eq 0 ] || [ "$1" = :: ]; then
        n=$((n+1)); ( cd "$D" && PATH="$D/bin:$PATH" PYTHONPATH="$D/pyclock" FAKE_OC_DIR="$D/fo" bash "$D/impl/scripts/council.sh" "${args[@]}" >"$out.step$n.out" 2>"$out.step$n.err"; echo $? >"$out.step$n.rc" )
        args=(); [ $# -eq 0 ] && break; shift; continue
      fi
      [ "$1" = @FAIL2 ] && { echo 2 >"$D/fo/fail-results"; shift; continue; }
      args+=("$1"); shift
    done
    ( cd "$D" && PATH="$D/bin:$PATH" PYTHONPATH="$D/pyclock" FAKE_OC_DIR="$D/fo" bash "$D/impl/scripts/council.sh" status --run-dir "$D/run" >"$out.status" 2>&1 )
    ( cd "$D" && PATH="$D/bin:$PATH" PYTHONPATH="$D/pyclock" FAKE_OC_DIR="$D/fo" bash "$D/impl/scripts/council.sh" report --run-dir "$D/run" >"$out.report" 2>&1 )
    rm -rf "$out.run" "$out.fo"; mv "$D/run" "$out.run"; mv "$D/fo" "$out.fo"
  }
  same_outputs() {  # label -> compare every artifact and every step's exit/stdout/stderr of the two implementations
    local b="$D/out-$1-base" c="$D/out-$1-cur" n=1
    python3 "$D/cmp.py" "$b.run" "$c.run" || return 1
    cmp "$b.status" "$c.status" && cmp "$b.report" "$c.report" && cmp "$b.fo/calls.log" "$c.fo/calls.log" || return 1
    while [ -f "$b.step$n.rc" ]; do
      cmp "$b.step$n.rc" "$c.step$n.rc" && cmp "$b.step$n.out" "$c.step$n.out" && cmp "$b.step$n.err" "$c.step$n.err" || return 1
      n=$((n+1))
    done
  }
  diff_case() {  # label snapshot-run snapshot-adapter -- steps
    local label=$1 srun=$2 sfo=$3; shift 3
    run_impl base "$D/out-$label-base" "$srun" "$sfo" "$@"
    run_impl cur "$D/out-$label-cur" "$srun" "$sfo" "$@"
    if [ -n "${NEW_RUN_CONFIG:-}" ]; then
      # A new run records its normalised choice; that is the only permitted state difference.
      rm -rf "$D/out-$label-cur.orig-run"; cp -Rp "$D/out-$label-cur.run" "$D/out-$label-cur.orig-run"
      jq -e '.config.map_code==false and .config.map_prepass=={}' "$D/out-$label-cur.run/state.json" >/dev/null || return 1
      local side; for side in base cur; do
        jq '.config|=del(.map_code,.map_prepass)' "$D/out-$label-$side.run/state.json" >"$D/s.tmp" && mv "$D/s.tmp" "$D/out-$label-$side.run/state.json"
      done
    fi
    same_outputs "$label"
  }
  jq -n --arg d "$D/proj" '{dir:$d,max_rounds:2,timeout_s:30,handover_at:0.5,executor:"A",members:[{id:"A",kind:"opencode",model:"p/m",effort:"medium",mode:"edit"},{id:"B",kind:"opencode",model:"p/m",effort:"medium",mode:"read"}],tasks:[{text:"first task",execute:true},{text:"second task"}]}' >"$D/cfg-omitted.json"
  jq '.map_code=false' "$D/cfg-omitted.json" >"$D/cfg-false.json"
  mkdir -p "$D/fo0"; touch "$D/fo0/ask-once"
  start_case() { NEW_RUN_CONFIG=1 diff_case "$@"; }
  # Real start from config: omitted and explicit-false map_code both follow the 97f4c70 path.
  for choice in omitted false; do
    check 0 "T1 map_code $choice: real start is byte-identical to 97f4c70 apart from the recorded map_code=false (prompts, posts, state, transcript, status, report, adapter calls)" 'compared' start_case "start-$choice" "" "$D/fo0" -- start --config "$D/cfg-$choice.json" --run-dir "$D/run"
    check 0 "T1 map_code $choice: start pauses at the real remaining-task question" 4 cat "$D/out-start-$choice-cur.step1.rc"
    check 0 "T1 map_code $choice: no mapper session or pre-pass state" '' sh -c '! grep -q "map pre-pass" "$1" && jq -e "([keys[]|select(startswith(\"map_prepass\"))]|length)==0" "$2" >/dev/null' _ "$D/out-start-$choice-cur.fo/calls.log" "$D/out-start-$choice-cur.run/state.json"
  done
  # The genuine 97f4c70 checkpoint: task t2 of 2 active (task_idx=1), handovers already done.
  legacy="$D/out-start-omitted-base"
  check 0 'T3 legacy fixture is a genuine 97f4c70 checkpoint inside an active remaining task' true jq -e '.task_idx==1 and (.config.tasks|length)==2 and .status=="questions" and .codemap_version==1 and ([.members[].gen]|max)>1' "$legacy.run/state.json"
  mkdir -p "$D/legacy-nocm"; cp -Rp "$legacy.run" "$D/legacy-nocm/run"
  jq 'del(.codemap_version, .codemap_pending)' "$D/legacy-nocm/run/state.json" >"$D/s.tmp" && mv "$D/s.tmp" "$D/legacy-nocm/run/state.json"
  printf '{"t2/r1/A/q1":"Hello"}\n' >"$D/answers.json"
  for form in codemap nocodemap; do
    if [ "$form" = codemap ]; then snap="$legacy.run"; else snap="$D/legacy-nocm/run"; fi
    check 0 "T3 legacy $form --answer resume is byte-identical to 97f4c70" compared diff_case "$form-answer" "$snap" "$legacy.fo" -- resume --run-dir "$D/run" --answer Hello
    check 0 "T3 legacy $form --answer resume performs a real handover into a fresh generation" '' test -s "$D/out-$form-answer-cur.run/prompts/handover-A-g3.md"
    check 0 "T3 legacy $form --answer resume finishes the remaining task" 0 cat "$D/out-$form-answer-cur.step1.rc"
    check 0 "T3 legacy $form --answers resume is byte-identical to 97f4c70" compared diff_case "$form-answers" "$snap" "$legacy.fo" -- resume --run-dir "$D/run" --answers "$D/answers.json"
    check 0 "T3 legacy $form --replace resume is byte-identical to 97f4c70" compared diff_case "$form-replace" "$snap" "$legacy.fo" -- resume --run-dir "$D/run" --answer Hello --replace B=opencode:p/m:medium
    check 0 "T3 legacy $form interruption (member fails twice, exit 2) then plain resume is byte-identical" compared diff_case "$form-interrupt" "$snap" "$legacy.fo" -- @FAIL2 resume --run-dir "$D/run" --answer Hello :: resume --run-dir "$D/run"
    check 0 "T3 legacy $form interruption really stopped with exit 2" 2 cat "$D/out-$form-interrupt-cur.step1.rc"
    check 0 "T3 legacy $form plain resume reused the surviving member post" 'reusing its' cat "$D/out-$form-interrupt-cur.step2.err"
    for label in answer answers replace interrupt; do
      check 0 "T3 legacy $form $label: zero mapper calls and no pre-pass state" '' sh -c '! grep -q "map pre-pass" "$1" && jq -e "([keys[]|select(startswith(\"map_prepass\"))]|length)==0" "$2" >/dev/null' _ "$D/out-$form-$label-cur.fo/calls.log" "$D/out-$form-$label-cur.run/state.json"
    done
  done
  check 0 'T3 legacy no-codemap form stays without codemap_version after resume' null jq -r '.codemap_version // "null"' "$D/out-nocodemap-answer-cur.run/state.json"
  # Current-format checkpoints created with map_code false/omitted resume exactly like 97f4c70 did.
  for choice in omitted false; do
    snap="$D/out-start-$choice-cur.orig-run"; sfo="$D/out-start-$choice-cur.fo"
    check 0 "T1 current map_code $choice checkpoint records map_code false before resume" true jq -e '.config.map_code==false and .config.map_prepass=={} and (has("map_prepass_version")|not)' "$snap/state.json"
    check 0 "T1 current map_code $choice checkpoint: --answer resume is byte-identical to 97f4c70" compared diff_case "cur-$choice-answer" "$snap" "$sfo" -- resume --run-dir "$D/run" --answer Hello
    check 0 "T1 current map_code $choice checkpoint: interruption then plain resume is byte-identical" compared diff_case "cur-$choice-interrupt" "$snap" "$sfo" -- @FAIL2 resume --run-dir "$D/run" --answer Hello :: resume --run-dir "$D/run"
    for label in answer interrupt; do
      check 0 "T1 current map_code $choice $label: zero mapper calls, no pre-pass state, map_code stays false" '' sh -c '! grep -q "map pre-pass" "$1" && jq -e "([keys[]|select(startswith(\"map_prepass\"))]|length)==0 and .config.map_code==false" "$2" >/dev/null' _ "$D/out-cur-$choice-$label-cur.fo/calls.log" "$D/out-cur-$choice-$label-cur.run/state.json"
    done
  done
)
# Mapped lifecycle through real council.sh processes (start / resume --map-decision / resume) with
# the offline adapter: map review, split approval by the printed contract map_seed_id, children
# through planning, execution, dissent/fix, ratification and handover, delivered-locator lookups,
# baseline-drift gating, and replacement/keep with superseded-post retirement across resumes.
mapped_lifecycle_process_tests() (
  D="$scratch/mapped-life"; mkdir -p "$D/impl" "$D/proj/src"
  cp -R "$HERE" "$D/impl/" || exit 1; write_fake_oc "$D/impl/scripts/oc.sh"
  printf 'alpha_body_line_one\nalpha_body_line_two\nalpha_body_line_three\n' >"$D/proj/src/a.py"
  printf 'beta_body_only_line\n' >"$D/proj/src/b.py"; printf 'shared prerequisite\n' >"$D/proj/keep.txt"
  mkdir -p "$D/fo"; printf '{"candidates":[{"path":"src/a.py","lines":[1,2]},{"path":"src/b.py"}],"unresolved":[{"target":"config","reason":"not found"}],"stopped_reason":"done"}\n' >"$D/fo/mapper-response"
  jq -n --arg d "$D/proj" '{dir:$d,max_rounds:2,timeout_s:30,handover_at:0.5,executor:"A",map_code:true,map_prepass:{kind:"opencode",model:"p/m",effort:"medium"},members:[{id:"A",kind:"opencode",model:"p/m",effort:"medium",mode:"edit"},{id:"B",kind:"opencode",model:"p/m",effort:"medium",mode:"read"}],tasks:[{id:"parent",text:"change a and b",execute:true},{id:"later",text:"a later original task"}]}' >"$D/cfg.json"
  R="$D/run"; mk=$(printf parent | shasum -a 256 | cut -c1-16)
  step() { local label=$1; shift; ( cd "$D" && FAKE_OC_DIR="$D/fo" bash "$D/impl/scripts/council.sh" "$@" >"$D/$label.out" 2>"$D/$label.err" ); }
  mapper_sessions() { grep -c 'Council map pre-pass' "$D/fo/calls.log"; }
  check 4 'real mapped start pauses at map review before any member inference' '' step start start --config "$D/cfg.json" --run-dir "$R"
  check 0 'mapped start dispatched exactly one mapper session' 1 mapper_sessions
  check 0 'mapped start launched no member session before review' 1 grep -c '^new ' "$D/fo/calls.log"
  check 0 'map review prints the selector list (regression: unterminated jq string)' 'src/a.py:1-2' cat "$D/start.out"
  check 0 'map review prints per-capture statuses (regression: unterminated jq string)' 'src/b.py: ok' cat "$D/start.out"
  check 0 'map review prints mapper-attributed unresolved targets' 'config: not found' cat "$D/start.out"
  check 0 'map review emits no jq errors' '' sh -c '! grep -q "jq: error" "$1"' _ "$D/start.err"
  seed=$(sed -n 's/^Split contract map_seed_id (copy this exact value into contract JSON): //p' "$D/start.out")
  check 0 'contract map_seed_id parsed from real review output equals coverage.json' "$seed" jq -r .map_seed_id "$R/map/$mk/coverage.json"
  sa=$(shasum -a 256 <"$D/proj/src/a.py" | cut -d' ' -f1); sb=$(shasum -a 256 <"$D/proj/src/b.py" | cut -d' ' -f1); sk=$(shasum -a 256 <"$D/proj/keep.txt" | cut -d' ' -f1)
  jq -n --arg seed "$seed" --arg sa "$sa" --arg sb "$sb" --arg sk "$sk" '{schema_version:1,parent_id:"parent",map_seed_id:$seed,subtasks:[
      {id:"child-a",text:"change a (contract one)",execute:true,requires:[{path:"src/a.py",sha256:$sa},{path:"keep.txt",sha256:$sk}],modifies:[{path:"src/a.py",sha256:$sa}],deletes:[],creates:[],acceptance:[],unresolved:[]},
      {id:"child-b",text:"change b (contract one)",execute:true,requires:[{path:"src/b.py",sha256:$sb},{path:"keep.txt",sha256:$sk}],modifies:[{path:"src/b.py",sha256:$sb}],deletes:[],creates:[],acceptance:[],unresolved:[]}]}' >"$D/contract1.json"
  printf '{"action":"split","contract_file":"contract1.json"}\n' >"$D/decision1.json"
  # child-a planning completes round 1, then member A fails twice in round 2 -> exit 2 checkpoint.
  echo 2 >"$D/fo/fail-results"; echo 'round 2 of|could not be used' >"$D/fo/fail-pattern"
  check 2 'split approval by the printed seed starts child planning; injected round-2 failure checkpoints' '' step split1 resume --run-dir "$R" --map-decision "$D/decision1.json"
  rm -f "$D/fo/fail-results" "$D/fo/fail-pattern"
  check 0 'children reuse the parent map: still exactly one mapper session' 1 mapper_sessions
  check 0 'child-a round-1 posts of contract one exist' 'contract one' cat "$R/prompts/child-a-r1-A.md"
  # External drift of an untouched prerequisite blocks the next launch before inference.
  calls_before=$(grep -c '^prompt ' "$D/fo/calls.log")
  printf 'drifted externally\n' >"$D/proj/keep.txt"
  check 4 'resume after baseline drift stops at split_invalid' '' step drift resume --run-dir "$R"
  check 0 'baseline drift diagnostic names the changed prerequisite' 'keep.txt' jq -r '.pending_questions[0].question' "$R/state.json"
  check 0 'baseline drift made zero inference calls' "$calls_before" grep -c '^prompt ' "$D/fo/calls.log"
  # Replacement contract reuses child-a; old round-1/2 posts must be retired, never reused.
  sk2=$(shasum -a 256 <"$D/proj/keep.txt" | cut -d' ' -f1)
  jq --arg sk "$sk2" '.subtasks|=map(.requires|=map(if .path=="keep.txt" then .sha256=$sk else . end)) | .subtasks[0].text="change a (contract two)" | .subtasks[1].text="change b (contract two)"' "$D/contract1.json" >"$D/contract2.json"
  printf '{"action":"split","contract_file":"contract2.json"}\n' >"$D/decision2.json"
  old_digest=$(jq -r '.map_prepasses.parent.approved_contract.digest' "$R/state.json")
  mkdir -p "$D/failbin"; real_mv=$(command -v mv)
  cat >"$D/failbin/mv" <<'SH'
#!/bin/sh
# Fail only the state publication that follows a completed superseded-contract archive.
case "$*" in *state.json) ls "$ARCHIVE_DIR"/*/MARKER.json >/dev/null 2>&1 && exit 1;; esac
exec "$REAL_MV" "$@"
SH
  chmod +x "$D/failbin/mv"
  pubfail() { ( cd "$D" && PATH="$D/failbin:$PATH" REAL_MV="$real_mv" ARCHIVE_DIR="$R/map/$mk/superseded" FAKE_OC_DIR="$D/fo" bash "$D/impl/scripts/council.sh" resume --run-dir "$R" --map-decision "$D/decision2.json" >"$D/pubfail.out" 2>"$D/pubfail.err" ); }
  prompts_before=$(grep -c '^prompt ' "$D/fo/calls.log")
  check 4 'state publication failure after archival stays checkpointed (exit 4)' '' pubfail
  check 0 'failed publication keeps the old approval and split_invalid checkpoint' true jq -e --arg o "$old_digest" '.phase=="split_invalid" and .map_prepasses.parent.approved_contract.digest==$o' "$R/state.json"
  check 0 'failed publication already retired the old posts from reusable names' true sh -c '! test -e "$1/posts/child-a-r1-A.md" && test -s "$1/map/$2/superseded/$3/posts/child-a-r1-A.md" && echo true' _ "$R" "$mk" "$old_digest"
  check 0 'failed publication made zero inference calls' "$prompts_before" grep -c '^prompt ' "$D/fo/calls.log"
  echo 2 >"$D/fo/fail-results"; echo 'contract two|could not be used' >"$D/fo/fail-pattern"
  check 2 'replacement publishes, then the first fresh member fails before round 1 completes' '' step replace resume --run-dir "$R" --map-decision "$D/decision2.json"
  rm -f "$D/fo/fail-results" "$D/fo/fail-pattern"
  check 0 'superseded round-1 and round-2 posts live only under the old contract identity' true sh -c 'a="$1/map/$2/superseded/$3/posts"; test -s "$a/child-a-r1-A.md" && test -s "$a/child-a-r1-B.md" && test -s "$a/child-a-r2-B.md" && ! test -e "$1/posts/child-a-r2-B.md" && echo true' _ "$R" "$mk" "$old_digest"
  echo 'TASK child-a — RATIFICATION' >"$D/fo/disagree-once"
  echo 2 >"$D/fo/fail-results"; echo 'TASK child-b — RATIFICATION|could not be used' >"$D/fo/fail-pattern"
  check 2 'ordinary resume runs the replacement contract until child-b ratification is interrupted' '' step resume1 resume --run-dir "$R"
  rm -f "$D/fo/fail-results" "$D/fo/fail-pattern"
  check 0 'ordinary resume never reuses a superseded contract-one post' '' sh -c '! grep "reusing its" "$1" | grep -v "member B"' _ "$D/resume1.err"
  check 0 'the only reused post is member B contract-two r1 post produced after publication' 'child-a r1 member B: propose' cat "$D/replace.err"
  check 0 'every member received a fresh contract-two round-1 prompt' true sh -c 'grep -q "contract two" "$1/prompts/child-a-r1-A.md" && grep -q "contract two" "$1/prompts/child-a-r1-B.md" && ! grep -q "contract one" "$1/posts/child-a-r1-A.md" && echo true' _ "$R"
  check 0 'replacement child-a is ratified and every child is bound to the new contract digest' true jq -e '[.results[]|select(.task=="child-a")|.outcome]==["ratified"] and ([.config.tasks[]|select(.map_parent=="parent")|.contract_identity]|unique)==[.map_prepasses.parent.approved_contract.digest]' "$R/state.json"
  check 0 'superseded archive marker records the old contract identity, not the new one' true jq -e --arg o "$old_digest" --slurpfile st "$R/state.json" '.old_contract_identity==$o and $o!=$st[0].map_prepasses.parent.approved_contract.digest and .reusable==false' "$R/map/$mk/superseded/$old_digest/MARKER.json"
  check 0 'ratification dissent drove a real fix round (exec2 delivers the fix prompt)' 'FIXES REQUESTED' cat "$R/prompts/child-a-exec2-A.md"
  check 0 'members were handed over to fresh generations during the lifecycle' '' sh -c 'ls "$1"/prompts/handover-* >/dev/null 2>&1' _ "$R"
  # T5: every expected child/member/stage delivery and every handover must exist and carry a
  # locator; a missing prompt or snapshot_id fails rather than being skipped. Each delivered
  # snapshot is looked up with the real council_codemap.py and must enumerate both the task's own
  # region and the sibling region; no captured or uncaptured source line may appear in a prompt.
  cat >"$D/snapcheck.py" <<'PY2'
import json,subprocess,sys
cm,run,pf,stage=sys.argv[1:5]
snaps=sorted({l.split(": ",1)[1].strip() for l in open(pf,encoding="utf-8") if l.startswith("snapshot_id: ")})
if not snaps: print("no snapshot_id in",pf); sys.exit(1)
for snap in snaps:
    p=subprocess.run([sys.executable,cm,"lookup","--run-dir",run,"--snapshot",snap],input=json.dumps({"paths":["src/a.py","src/b.py"],"include_evidence":True}),capture_output=True,text=True)
    if p.returncode: print("lookup failed",snap,p.stderr); sys.exit(1)
    ents=json.loads(p.stdout)["entries"]
    got={(e["path"],tuple(e["lines"] or [])) for e in ents}
    if ("src/a.py",(1,2)) not in got or not any(e["path"]=="src/b.py" for e in ents): print("own/sibling region missing",snap,got); sys.exit(1)
    if stage=="r1" and any(e.get("reports") for e in ents): print("round-1 view is not raw-only",snap); sys.exit(1)
print("true")
PY2
  expected="child-a-r1-A child-a-r1-B child-a-r2-A child-a-r2-B child-a-exec1-A child-a-x1-A child-a-x1-B child-a-exec2-A child-a-x2-A child-a-x2-B child-b-r1-A child-b-r1-B child-b-r2-A child-b-r2-B child-b-exec1-A child-b-x1-A child-b-x1-B"
  handovers=$(cd "$R/prompts" && ls handover-*.md 2>/dev/null | sed 's/\.md$//' | tr '\n' ' ')
  check 0 'handovers were delivered to both members during the child lifecycle' true sh -c 'ls "$1"/prompts/handover-A-* >/dev/null 2>&1 && ls "$1"/prompts/handover-B-* >/dev/null 2>&1 && echo true' _ "$R"
  deliveries=0
  for name in $expected $handovers; do
    pf="$R/prompts/$name.md"; deliveries=$((deliveries+1))
    case "$name" in *-r1-*) stage=r1;; *-r2-*) stage=r2;; handover-*) stage=handover;; *) stage=historical;; esac
    check 0 "T5 expected delivery $name exists and every delivered snapshot resolves both own and sibling regions" true python3 "$D/snapcheck.py" "$D/impl/scripts/council_codemap.py" "$R" "$pf" "$stage"
    check 0 "T5 delivered prompt $name embeds neither captured nor uncaptured source lines" '' sh -c '! grep -q "alpha_body_line_one\|alpha_body_line_two\|alpha_body_line_three\|beta_body_only_line" "$1"' _ "$pf"
    case "$stage" in
      historical|handover) check 0 "T5 $name carries the historical framing and the live-inspection instruction" true sh -c 'grep -q "^=== HISTORICAL CODE MAP (as of the completed pre-pass snapshot; not a live-source claim) ===$" "$1" && grep -q "^Live inspection is required for current-source claims" "$1" && echo true' _ "$pf" ;;
      *) check 0 "T5 deliberation prompt $name is not framed as a historical map" '' sh -c '! grep -q "HISTORICAL CODE MAP" "$1"' _ "$pf" ;;
    esac
  done
  check 0 'T5 every expected child delivery plus every handover was checked' '' test "$deliveries" -ge 19
  # Ratification-round keep: drift during child-b ratification, then restore the parent task.
  new_digest=$(jq -r '.map_prepasses.parent.approved_contract.digest' "$R/state.json")
  printf 'drifted during ratification\n' >"$D/proj/keep.txt"
  check 4 'resume at the interrupted ratification detects prerequisite drift' '' step drift2 resume --run-dir "$R"
  check 0 'ratification-round drift is a split_invalid checkpoint' split_invalid jq -r .phase "$R/state.json"
  printf '{"action":"keep"}\n' >"$D/keep.json"
  check 4 'keep restores the parent, plans it fresh, and reaches the later map review' '' step keep resume --run-dir "$R" --map-decision "$D/keep.json"
  check 0 'keep archived ratified child outcomes under the replaced contract identity' true jq -e --arg d "$new_digest" '.result_history[-1].contract_identity==$d and ([.result_history[-1].results[].task]|index("child-a"))!=null' "$R/state.json"
  check 0 'keep re-planned the parent from round 1 with fresh prompts for every member' true sh -c 'test -s "$1/prompts/parent-r1-A.md" && test -s "$1/prompts/parent-r1-B.md" && ! grep -q "reusing its" "$2" && echo true' _ "$R" "$D/keep.err"
  check 0 'kept parent reached a ratified result' ratified jq -r '[.results[]|select(.task=="parent")][0].outcome' "$R/state.json"
  check 0 'interrupted child-b ratification prompts and surviving post are archived under the replaced identity' true sh -c 'a="$1/map/$2/superseded/$3"; test -s "$a/prompts/child-b-x1-A.md" && test -s "$a/posts/child-b-x1-B.md" && ! test -e "$4/posts/child-b-x1-B.md" && echo true' _ "$R" "$mk" "$new_digest" "$R"
  check 0 'later task still has exactly its own single pre-pass' 2 mapper_sessions
  printf '{"action":"keep"}\n' >"$D/keep-later.json"
  check 0 'ordinary keep continuation finishes the later original task' '' step later resume --run-dir "$R" --map-decision "$D/keep-later.json"
  check 0 'run completes with every remaining task resolved' true jq -e '.status=="done" and ([.results[].task]|index("later"))!=null' "$R/state.json"
  check 0 'no further mapper sessions after the last review' 2 mapper_sessions
  check 0 '4(d) children reuse the parent mapper session: status counts 2 unique mapper sessions for 2 original tasks' 'across 2 unique sessions' sh -c 'cd "$1" && bash "$1/impl/scripts/council.sh" status --run-dir "$2"' _ "$D" "$R"
  check 0 '4(d) the report agrees on 2 unique mapper sessions' '2 unique session(s)' sh -c 'cd "$1" && bash "$1/impl/scripts/council.sh" report --run-dir "$2"' _ "$D" "$R"
  check 0 'the mapped lifecycle emitted no jq errors (regression: .coverage string indexing)' '' sh -c '! cat "$1"/*.err | grep -q "jq: error"' _ "$D"
)
# T1 inherited parent answers through real council.sh processes: the user's settled parent answers
# are copied into every split child and delivered verbatim in the children's deliberation and
# execution prompts, across an interrupted child round and its resume, while the parent history stays.
# No CLI path records a parent answer before the split decision (map review precedes deliberation),
# so the fixture stores the parent answer records in the exact form resume --answers stores them.
inherited_answers_process_tests() (
  D="$scratch/inherited"; mkdir -p "$D/impl" "$D/proj/src"
  cp -R "$HERE" "$D/impl/" || exit 1; write_fake_oc "$D/impl/scripts/oc.sh"
  printf 'alpha\n' >"$D/proj/src/a.py"; printf 'beta\n' >"$D/proj/src/b.py"
  mkdir -p "$D/fo"; printf '%s\n' '{"candidates":[{"path":"src/a.py"},{"path":"src/b.py"}],"unresolved":[],"stopped_reason":"done"}' >"$D/fo/mapper-response"
  jq -n --arg d "$D/proj" '{dir:$d,max_rounds:2,timeout_s:30,handover_at:0.5,executor:"A",map_code:true,map_prepass:{kind:"opencode",model:"p/m",effort:"medium"},members:[{id:"A",kind:"opencode",model:"p/m",effort:"medium",mode:"edit"},{id:"B",kind:"opencode",model:"p/m",effort:"medium",mode:"read"}],tasks:[{id:"parent",text:"change a and b",execute:true}]}' >"$D/cfg.json"
  R="$D/run"
  step() { local label=$1; shift; ( cd "$D" && FAKE_OC_DIR="$D/fo" bash "$D/impl/scripts/council.sh" "$@" >"$D/$label.out" 2>"$D/$label.err" ); }
  check 4 'T1 inherited answers: real mapped start pauses at map review' '' step start start --config "$D/cfg.json" --run-dir "$R"
  jq -n '[{id:"parent/r1/A/q1",task:"parent",member:"A",question:"Which layout should the change follow?",answer:"Use the \"v2\" layout — păstrează diacriticele; keep `src/` as is"},
          {id:"parent/r1/B/q1",task:"parent",member:"B",question:"Scope of b?",answer:"Only the first line.\nNothing else."}]' >"$D/parent-answers.json"
  jq --slurpfile a "$D/parent-answers.json" '.answers += $a[0]' "$R/state.json" >"$R/state.json.t" && mv "$R/state.json.t" "$R/state.json"
  seed=$(sed -n 's/^Split contract map_seed_id (copy this exact value into contract JSON): //p' "$D/start.out")
  sa=$(shasum -a 256 <"$D/proj/src/a.py" | cut -d' ' -f1); sb=$(shasum -a 256 <"$D/proj/src/b.py" | cut -d' ' -f1)
  jq -n --arg seed "$seed" --arg sa "$sa" --arg sb "$sb" '{schema_version:1,parent_id:"parent",map_seed_id:$seed,subtasks:[
      {id:"child-a",text:"change a",execute:true,requires:[{path:"src/a.py",sha256:$sa}],modifies:[{path:"src/a.py",sha256:$sa}],deletes:[],creates:[],acceptance:[],unresolved:[]},
      {id:"child-b",text:"change b",execute:true,requires:[{path:"src/b.py",sha256:$sb}],modifies:[{path:"src/b.py",sha256:$sb}],deletes:[],creates:[],acceptance:[],unresolved:[]}]}' >"$D/contract.json"
  printf '{"action":"split","contract_file":"contract.json"}\n' >"$D/decision.json"
  echo 2 >"$D/fo/fail-results"; echo 'round 2 of|could not be used' >"$D/fo/fail-pattern"
  check 2 'T1 inherited answers: split approval starts child-a, whose round 2 is interrupted' '' step split resume --run-dir "$R" --map-decision "$D/decision.json"
  rm -f "$D/fo/fail-results" "$D/fo/fail-pattern"
  check 0 'T1 inherited answers: every child carries exactly the settled parent answer records' true jq -e --slurpfile a "$D/parent-answers.json" '[.config.tasks[]|select(.map_parent=="parent")] as $c | ($c|length)==2 and all($c[]; .inherited_answers==$a[0])' "$R/state.json"
  check 0 'T1 inherited answers: resume after the interruption completes both children' '' step resume resume --run-dir "$R"
  check 0 'T1 inherited answers: both children are ratified' true jq -e '.status=="done" and ([.results[]|select(.outcome=="ratified")|.task]|sort)==["child-a","child-b"]' "$R/state.json"
  check 0 'T1 inherited answers: the parent answer history is preserved unchanged and attributed to the parent' true jq -e --slurpfile a "$D/parent-answers.json" '[.answers[]|select(.task=="parent")]==$a[0]' "$R/state.json"
  check 0 'T1 inherited answers: the mapper was never relaunched' 1 grep -c 'Council map pre-pass' "$D/fo/calls.log"
  cat >"$D/verbatim.py" <<'PY2'
import json,sys
answers=json.load(open(sys.argv[1]))
block="Original parent-task user answers (verbatim):\n"+"\n".join("- Q: {}\n  A: {}".format(a["question"],a["answer"]) for a in answers)+"\n"
missing=[p for p in sys.argv[2:] if block not in open(p,encoding="utf-8").read()]
print("missing in: "+" ".join(missing) if missing else "true")
PY2
  # The task statement (and with it the inherited answers) is delivered in round 1 and at execution;
  # later rounds carry only the candidate and posts. child-a-exec1 and all of child-b are delivered by
  # the resumed process.
  prompts="child-a-r1-A child-a-r1-B child-a-exec1-A child-b-r1-A child-b-r1-B child-b-exec1-A"
  set -- ; for n in $prompts; do set -- "$@" "$R/prompts/$n.md"; done
  check 0 'T1 inherited answers: every child round-1 and execution prompt (the resumed process included) carries the exact parent Q/A text' true python3 "$D/verbatim.py" "$D/parent-answers.json" "$@"
  check 0 'T1 inherited answers: the execution prompt keeps the last answer on its own line' true sh -c 'grep -qx "Nothing else." "$1" && echo true' _ "$R/prompts/child-a-exec1-A.md"
)
# T6 response matrix through real processes: every mapper outcome is classified with unknown
# coverage, keeps its raw response, is never retried, and the task continues after review.
mapper_response_process_tests() (
  D="$scratch/mapper-matrix"; mkdir -p "$D/impl" "$D/proj/src"
  cp -R "$HERE" "$D/impl/" || exit 1; write_fake_oc "$D/impl/scripts/oc.sh"
  printf 'one\ntwo\nthree\n' >"$D/proj/src/a.py"
  valid='{"candidates":[{"path":"src/a.py","lines":[1,2]}],"unresolved":[],"stopped_reason":"done"}'
  for variant in valid partial empty malformed oversized timeout; do
    F="$D/fo-$variant"; R="$D/run-$variant"; mkdir -p "$F"; maxb=65536
    case "$variant" in
      valid) printf '%s\n' "$valid" >"$F/mapper-response" ;;
      partial) printf '%s\n' '{"candidates":[{"path":"src/a.py","lines":[1,2]},{"path":"src/a.py","lines":[50,60]}],"unresolved":[],"stopped_reason":"done"}' >"$F/mapper-response" ;;
      empty) : >"$F/mapper-response" ;;
      malformed) printf 'this is not json\n' >"$F/mapper-response" ;;
      oversized) printf '%s\n' "$valid" >"$F/mapper-response"; maxb=16 ;;
      timeout) printf '%s\n' "$valid" >"$F/mapper-response"; touch "$F/mapper-timeout" ;;
    esac
    jq -n --arg d "$D/proj" --argjson mb "$maxb" '{dir:$d,max_rounds:2,timeout_s:30,handover_at:0.5,executor:null,map_code:true,map_prepass:{kind:"opencode",model:"p/m",effort:"medium",max_output_bytes:$mb},members:[{id:"A",kind:"opencode",model:"p/m",effort:"medium",mode:"read"},{id:"B",kind:"opencode",model:"p/m",effort:"medium",mode:"read"}],tasks:[{id:"only",text:"investigate a"}]}' >"$D/cfg-$variant.json"
    step() { local label=$1; shift; ( cd "$D" && FAKE_OC_DIR="$F" bash "$D/impl/scripts/council.sh" "$@" >"$D/$label.out" 2>"$D/$label.err" ); }
    check 4 "T6 $variant mapper response reaches map review" '' step "$variant-start" start --config "$D/cfg-$variant.json" --run-dir "$R"
    case "$variant" in valid) want=complete;; partial) want=partial;; *) want=unavailable;; esac
    check 0 "T6 $variant mapper response is classified $want" "$want" jq -r '.map_prepasses.only.status' "$R/state.json"
    check 0 "T6 $variant coverage stays unknown" unknown jq -r '.coverage' "$R/map/$(printf only | shasum -a 256 | cut -c1-16)/coverage.json"
    [ "$variant" = timeout ] || check 0 "T6 $variant raw mapper response bytes are retained" '' cmp "$F/mapper-response" "$R/map/$(printf only | shasum -a 256 | cut -c1-16)/response.txt"
    [ "$variant" != timeout ] || check 0 "T6 timeout interrupts the mapper session" '' grep -q '^interrupt ses_fake1' "$F/calls.log"
    check 0 "T6 $variant exactly one mapper prompt, no retry" 1 sh -c 'grep "^prompt ses_fake1 " "$1" | wc -l | tr -d " "' _ "$F/calls.log"
    check 0 "T6 $variant usage retained with explicit fields" true jq -e '.map_prepasses.only.usage|type=="object"' "$R/state.json"
    printf '{"action":"keep"}\n' >"$D/keep.json"
    check 0 "T6 $variant task continues to consensus after keep" '' step "$variant-keep" resume --run-dir "$R" --map-decision "$D/keep.json"
    check 0 "T6 $variant run completes" done jq -r .status "$R/state.json"
    check 0 "T6 $variant never relaunches the mapper" 1 grep -c 'Council map pre-pass' "$F/calls.log"
  done
)
# T2/T6 durability through real council.sh processes: a PATH mv shim fails (before) or completes
# and then SIGKILLs the orchestrator (after) the first state publication matching a jq predicate,
# i.e. each authorization/dispatch boundary. Ordinary resumes must never infer before durable
# authorization, never prompt after a pre-dispatch persistence failure, and never prompt the
# mapper more than once.
write_fault_bin() {  # dir -> mv and python3 shims driven by FAIL_* environment variables
  mkdir -p "$1"
  cat >"$1/mv" <<SH
#!/bin/bash
REAL=$(command -v mv)
SH
  cat >>"$1/mv" <<'SH'
dst=""; for a; do dst=$a; done; src=""; for a; do case $a in -*) ;; *) src=$a; break;; esac; done
if [ -n "${FAIL_STATE_JQ:-}" ] && [ ! -e "$FAIL_FLAG" ]; then
  case "$dst" in */state.json)
    if jq -e "$FAIL_STATE_JQ" "$src" >/dev/null 2>&1; then
      seen=$(cat "$FAIL_FLAG.seen" 2>/dev/null || echo 0); echo $((seen+1)) >"$FAIL_FLAG.seen"
      [ "$seen" -lt "${FAIL_STATE_SKIP:-0}" ] && exec "$REAL" "$@"
      : >"$FAIL_FLAG"
      if [ "$FAIL_MODE" = after ]; then "$REAL" "$@" && kill -KILL $PPID; exit 0; fi
      exit 1
    fi ;;
  esac
fi
if [ -n "${FAIL_MV_GLOB:-}" ]; then
  case "$dst" in $FAIL_MV_GLOB)
    n=$(cat "$FAIL_FLAG.mv" 2>/dev/null || echo 0)
    if [ "$n" -lt "${FAIL_MV_COUNT:-1}" ]; then echo $((n+1)) >"$FAIL_FLAG.mv"; exit 1; fi ;;
  esac
fi
exec "$REAL" "$@"
SH
  cat >"$1/python3" <<SH
#!/bin/bash
REAL=$(command -v python3)
SH
  cat >>"$1/python3" <<'SH'
if [ -n "${FAIL_PY_MATCH:-}" ] && [ ! -e "$FAIL_FLAG.py" ]; then
  case " $* " in *$FAIL_PY_MATCH*) : >"$FAIL_FLAG.py"; echo "injected helper failure" >&2; exit 1;; esac
fi
if [ -n "${MUTATE_AFTER_PY_MATCH:-}" ] && [ ! -e "$FAIL_FLAG.mut" ]; then
  case " $* " in *$MUTATE_AFTER_PY_MATCH*) "$REAL" "$@"; rc=$?; : >"$FAIL_FLAG.mut"; sh -c "$MUTATE_CMD"; exit $rc;; esac
fi
exec "$REAL" "$@"
SH
  chmod +x "$1/mv" "$1/python3"
}
prepass_fault_process_tests() (
  D="$scratch/prepass-faults"; mkdir -p "$D/impl" "$D/proj/src"
  cp -R "$HERE" "$D/impl/" || exit 1; write_fake_oc "$D/impl/scripts/oc.sh"; write_fault_bin "$D/bin"
  printf 'one\ntwo\nthree\n' >"$D/proj/src/a.py"
  printf '%s\n' '{"candidates":[{"path":"src/a.py","lines":[1,2]}],"unresolved":[{"target":"config","reason":"not found"}],"stopped_reason":"done"}' >"$D/mapper-response"
  base='{max_rounds:2,timeout_s:30,handover_at:0.5,executor:null,map_code:true,members:[{id:"A",kind:"opencode",model:"p/m",effort:"medium",mode:"read"},{id:"B",kind:"opencode",model:"p/m",effort:"medium",mode:"read"}],tasks:[{id:"only",text:"investigate a"}]}'
  jq -n --arg d "$D/proj" "$base"' + {dir:$d,map_prepass:{kind:"opencode",model:"p/m",effort:"medium"}}' >"$D/cfg.json"
  jq -n --arg d "$D/proj" "$base"' + {dir:$d}' >"$D/cfg-proposal.json"
  printf '{"kind":"opencode","model":"google/gemini-3.8-flash","effort":"medium"}\n' >"$D/confirm.json"
  printf '{"action":"keep"}\n' >"$D/keep.json"; printf '{"x":"y"}\n' >"$D/answers.json"
  mk=$(printf only | shasum -a 256 | cut -c1-16)
  fcase() { C="$D/case-$1"; F="$C/fo"; R="$C/run"; rm -rf "$C"; mkdir -p "$F"; cp "$D/mapper-response" "$F/"; }
  # run LABEL [VAR=value ...] -- council.sh args   (fault variables apply to this process only)
  run() {
    local label=$1; shift; local -a envs=()
    while [ "$1" != -- ]; do envs+=("$1"); shift; done; shift
    ( cd "$D" && env PATH="$D/bin:$PATH" FAKE_OC_DIR="$F" FAIL_FLAG="$C/flag" "${envs[@]}" bash "$D/impl/scripts/council.sh" "$@" >"$C/$label.out" 2>"$C/$label.err" )
  }
  mprompts() { grep -c '^prompt ses_fake[0-9]* --file .*/map/' "$F/calls.log" 2>/dev/null || echo 0; }
  anyprompts() { grep -c '^prompt ' "$F/calls.log" 2>/dev/null || echo 0; }
  news() { grep -c '^new ' "$F/calls.log" 2>/dev/null || echo 0; }
  finish() {  # label -> map review reached; keep; run completes; mapper never re-prompted
    check 0 "$1: map review is displayed with unknown coverage" 'Map review for task only: status=' cat "$C/review.out"
    check 0 "$1: keep continues the task to completion" '' run keep -- resume --run-dir "$R" --map-decision "$D/keep.json"
    check 0 "$1: run completes" done jq -r .status "$R/state.json"
    check 0 "$1: exactly one mapper prompt across every process" 1 mprompts
  }
  # ---- T2 boundary: the INITIAL state write (generation with partial output, and publication) ----
  mkdir -p "$D/jqbin"; printf '#!/bin/bash\nREAL=%s\n' "$(command -v jq)" >"$D/jqbin/jq"
  cat >>"$D/jqbin/jq" <<'SH'
# Fail the initial run-state generation once, after emitting a truncated document.
case " $* " in *'cl_cumulative:$cum'*) if [ ! -e "$FAIL_FLAG.jq" ]; then : >"$FAIL_FLAG.jq"; printf '{"config":'; exit 1; fi;; esac
exec "$REAL" "$@"
SH
  chmod +x "$D/jqbin/jq"
  for variant in generation publication; do
    fcase "initial-$variant"
    case $variant in
      generation) fault=("PATH=$D/jqbin:$D/bin:$PATH") ;;
      publication) fault=('FAIL_MV_GLOB=*/state.json' FAIL_MV_COUNT=1) ;;
    esac
    check 1 "T2 initial state $variant fault: start exits 1" '' run start "${fault[@]}" -- start --config "$D/cfg.json" --run-dir "$R"
    check 0 "T2 initial state $variant fault: exact diagnostic" 'council: could not persist initial run state; no model call made' cat "$C/start.err"
    check 0 "T2 initial state $variant fault: zero sessions and zero prompts" 00 sh -c 'printf %s%s "$(grep -c "^new " "$1")" "$(grep -c "^prompt " "$1")"' _ "$F/calls.log"
    check 0 "T2 initial state $variant fault: the run directory this start created is removed (never half-started)" '' test ! -e "$R"
    check 4 "T2 initial state $variant fault: the identical start command reruns cleanly to map review" '' run review -- start --config "$D/cfg.json" --run-dir "$R"
    check 0 "T2 initial state $variant fault: the rerun published a complete state" true jq -e '.map_prepass_version==1 and .map_prepass_authorized.source=="config" and .phase=="map_review"' "$R/state.json"
    check 0 "T2 initial state $variant fault: no temporary state file is left" '' test ! -e "$R/state.json.tmp"
    finish "T2 initial state $variant fault"
  done
  check 1 'T2 an existing run directory is still refused by start' '' run exists -- start --config "$D/cfg.json" --run-dir "$R"
  check 0 'T2 the refusal names the existing directory and leaves its state untouched' 'run dir exists' sh -c 'cat "$1"; jq -e ".status==\"done\"" "$2" >/dev/null' _ "$C/exists.err" "$R/state.json"
  # ---- boundary: initial version-marker publication (configured mapper) ----
  for mode in before after; do
    fcase "marker-$mode"; want=1; [ $mode = after ] && want=137
    check $want "T2 version marker ($mode): publication fault stops start" '' run start FAIL_MODE=$mode FAIL_STATE_SKIP=1 'FAIL_STATE_JQ=.map_prepass_version==1 and .map_prepass_authorized==null and .map_prepass_pending==null' -- start --config "$D/cfg.json" --run-dir "$R"
    [ $mode = before ] && check 0 'T2 version marker (before): the fault hit that exact publication' 'could not persist map-prepass version marker' cat "$C/start.err"
    check 0 "T2 version marker ($mode): zero sessions and zero prompts" 00 sh -c 'printf %s%s "$(grep -c "^new " "$1")" "$(grep -c "^prompt " "$1")"' _ "$F/calls.log"
    check 4 "T2 version marker ($mode): ordinary resume authorizes from config and reaches map review" '' run review -- resume --run-dir "$R"
    check 0 "T2 version marker ($mode): durable config authorization precedes the only mapper prompt" config jq -r .map_prepass_authorized.source "$R/state.json"
    finish "T2 version marker ($mode)"
  done
  # ---- boundary: pending proposal (mapper model/effort omitted) ----
  for mode in before after; do
    fcase "proposal-$mode"; want=1; [ $mode = after ] && want=137
    check $want "T2 pending proposal ($mode): publication fault stops start" '' run start FAIL_MODE=$mode 'FAIL_STATE_JQ=.map_prepass_pending!=null' -- start --config "$D/cfg-proposal.json" --run-dir "$R"
    [ $mode = before ] && check 0 'T2 pending proposal (before): the fault hit that exact publication' 'could not persist mapper proposal; no model call made' cat "$C/start.err"
    check 0 "T2 pending proposal ($mode): no session, no prompt" 00 sh -c 'printf %s%s "$(grep -c "^new " "$1")" "$(grep -c "^prompt " "$1")"' _ "$F/calls.log"
    check 4 "T2 pending proposal ($mode): plain resume re-proposes and waits for explicit confirmation" '' run resume1 -- resume --run-dir "$R"
    check 0 "T2 pending proposal ($mode): proposal is durable and nothing is authorized" true jq -e '.phase=="mapper_confirm" and .map_prepass_pending.model=="google/gemini-3.8-flash" and .map_prepass_authorized==null' "$R/state.json"
    check 4 "T2 pending proposal ($mode): generic --answers is rejected before authorization" '' run answers -- resume --run-dir "$R" --answers "$D/answers.json"
    check 0 "T2 pending proposal ($mode): the --answers rejection names the confirmation path" 'must use --confirm-mapper' cat "$C/answers.err"
    check 4 "T2 pending proposal ($mode): generic --answer is rejected before authorization" '' run answer -- resume --run-dir "$R" --answer yes
    check 0 "T2 pending proposal ($mode): still zero sessions and zero prompts" 00 sh -c 'printf %s%s "$(grep -c "^new " "$1")" "$(grep -c "^prompt " "$1")"' _ "$F/calls.log"
    check 4 "T2 pending proposal ($mode): explicit confirmation runs the mapper to review" '' run review -- resume --run-dir "$R" --confirm-mapper "$D/confirm.json"
    finish "T2 pending proposal ($mode)"
  done
  # ---- boundary: configured authorization ----
  for mode in before after; do
    fcase "cfgauth-$mode"; want=1; [ $mode = after ] && want=137
    check $want "T2 configured authorization ($mode): publication fault stops start" '' run start FAIL_MODE=$mode 'FAIL_STATE_JQ=.map_prepass_authorized.source=="config"' -- start --config "$D/cfg.json" --run-dir "$R"
    [ $mode = before ] && check 0 'T2 configured authorization (before): the fault hit that exact publication' 'could not persist configured mapper authorization; no model call made' cat "$C/start.err"
    check 0 "T2 configured authorization ($mode): zero sessions and zero prompts" 00 sh -c 'printf %s%s "$(grep -c "^new " "$1")" "$(grep -c "^prompt " "$1")"' _ "$F/calls.log"
    check 4 "T2 configured authorization ($mode): ordinary resume reaches map review" '' run review -- resume --run-dir "$R"
    finish "T2 configured authorization ($mode)"
  done
  # ---- boundary: explicit confirmation ----
  for mode in before after; do
    fcase "confirm-$mode"; want=4; [ $mode = after ] && want=137
    check 4 "T2 explicit confirmation ($mode): start proposes and pauses" '' run start -- start --config "$D/cfg-proposal.json" --run-dir "$R"
    check $want "T2 explicit confirmation ($mode): authorization publication fault" '' run confirm FAIL_MODE=$mode 'FAIL_STATE_JQ=.map_prepass_authorized.source=="user-confirmed"' -- resume --run-dir "$R" --confirm-mapper "$D/confirm.json"
    [ $mode = before ] && check 0 'T2 explicit confirmation (before): the fault hit that exact publication' 'could not persist mapper authorization; confirmation checkpoint is retained' cat "$C/confirm.err"
    check 0 "T2 explicit confirmation ($mode): zero sessions and zero prompts" 00 sh -c 'printf %s%s "$(grep -c "^new " "$1")" "$(grep -c "^prompt " "$1")"' _ "$F/calls.log"
    if [ $mode = before ]; then
      check 0 "T2 explicit confirmation (before): the pending proposal is retained" true jq -e '.phase=="mapper_confirm" and .map_prepass_pending!=null and .map_prepass_authorized==null' "$R/state.json"
      check 4 "T2 explicit confirmation (before): confirming again runs the mapper to review" '' run review -- resume --run-dir "$R" --confirm-mapper "$D/confirm.json"
    else
      check 4 "T2 explicit confirmation (after): ordinary resume uses the durable authorization" '' run review -- resume --run-dir "$R"
    fi
    finish "T2 explicit confirmation ($mode)"
  done
  # ---- boundaries inside map_prepass_run ----
  for boundary in preparing created dispatching dispatched complete; do
    for mode in before after; do
      fcase "$boundary-$mode"
      case "$boundary-$mode" in *-after) want=137;; preparing-before|created-before|dispatching-before|dispatched-before|complete-before) want=2;; esac
      check $want "T2 $boundary checkpoint ($mode): fault stops the first process" '' run start FAIL_MODE=$mode "FAIL_STATE_JQ=any(.map_prepasses[]?; .status==\"$boundary\")" -- start --config "$D/cfg.json" --run-dir "$R"
      if [ $mode = before ]; then
        case "$boundary" in preparing) msg='could not persist mapper preparation checkpoint';; created) msg='could not persist mapper session identity; no prompt dispatched';;
          dispatching) msg='could not persist mapper dispatch checkpoint; no prompt dispatched';; dispatched) msg='mapper prompt may be dispatched; durable state remains dispatching';; complete) msg='map pre-pass failed operationally; checkpointed';; esac
        check 0 "T2 $boundary checkpoint (before): the fault hit that exact publication" "$msg" cat "$C/start.err"
      else
        check 0 "T2 $boundary checkpoint (after): the killed process left that exact durable state" "$boundary" jq -r .map_prepasses.only.status "$R/state.json"
      fi
      case "$boundary" in preparing|created|dispatching) pre=0;; *) pre=1;; esac
      [ "$boundary-$mode" = dispatching-after ] && pre=0
      check 0 "T2 $boundary checkpoint ($mode): mapper prompts so far = $pre" $pre mprompts
      check 0 "T2 $boundary checkpoint ($mode): no member session was launched" 0 sh -c 'grep "^prompt " "$1" | grep -v "/map/" | wc -l | tr -d " "' _ "$F/calls.log"
      [ "$boundary-$mode" = created-before ] && check 0 'T2 created (before): the session whose identity could not be persisted is interrupted unprompted' '' grep -q '^interrupt ses_fake1' "$F/calls.log"
      check 4 "T2 $boundary checkpoint ($mode): plain resume reaches map review" '' run review -- resume --run-dir "$R"
      case "$boundary-$mode" in
        dispatching-after)
          check 0 'T2 uncertain dispatch (never prompted) is unavailable and not relaunched' unavailable jq -r .map_prepasses.only.status "$R/state.json"
          check 0 'T2 uncertain dispatch failure names the no-relaunch rule' 'not relaunched' jq -r .map_prepasses.only.failure "$R/state.json"
          check 0 'T2 uncertain dispatch: zero mapper prompts after resume' 0 mprompts
          check 0 'T2 uncertain dispatch: keep continues to completion' '' run keep -- resume --run-dir "$R" --map-decision "$D/keep.json"
          check 0 'T2 uncertain dispatch: zero mapper prompts at completion' 0 mprompts ;;
        *)
          check 0 "T2 $boundary checkpoint ($mode): resumed mapper result is complete" complete jq -r .map_prepasses.only.status "$R/state.json"
          check 0 "T2 $boundary checkpoint ($mode): raw response retained byte-for-byte" '' cmp "$D/mapper-response" "$R/map/$mk/response.txt"
          finish "T2 $boundary checkpoint ($mode)" ;;
      esac
    done
  done
  # Interrupted response: dispatched, orchestrator killed, the session then times out on recovery.
  fcase interrupted
  check 137 'T6 interrupted response: orchestrator killed after the dispatched checkpoint' '' run start FAIL_MODE=after 'FAIL_STATE_JQ=any(.map_prepasses[]?; .status=="dispatched")' -- start --config "$D/cfg.json" --run-dir "$R"
  touch "$F/mapper-timeout"
  check 4 'T6 interrupted response: resume reaches review' '' run review -- resume --run-dir "$R"
  check 0 'T6 interrupted response: unavailable, never relaunched' unavailable jq -r .map_prepasses.only.status "$R/state.json"
  check 0 'T6 interrupted response: the running session is interrupted' '' grep -q '^interrupt ses_fake1' "$F/calls.log"
  check 0 'T6 interrupted response: still exactly one mapper prompt' 1 mprompts
  check 0 'T6 interrupted response: keep continues to completion' '' run keep -- resume --run-dir "$R" --map-decision "$D/keep.json"
  check 0 'T6 interrupted response: exactly one mapper prompt at completion' 1 mprompts
  # ---- failures after useful output exists ----
  known='{"input":6000,"cache_read":0,"cache_write":0,"output":10,"reasoning":0,"cost":0.01}'
  after_output() {  # label want-status
    check 0 "T6 $1: classified $2 with coverage unknown" "$2 unknown" sh -c 'printf "%s %s" "$(jq -r .map_prepasses.only.status "$1")" "$(jq -r .coverage "$2")"' _ "$R/state.json" "$R/map/$mk/coverage.json"
    check 0 "T6 $1: known usage components are retained" true jq -e --argjson k "$known" '.map_prepasses.only.usage==$k' "$R/state.json"
    check 0 "T6 $1: mapper-attributed unresolved claim is preserved" config jq -r '.mapper_unresolved.items[0].target' "$R/map/$mk/coverage.json"
  }
  fcase retrieval; echo 1 >"$F/fail-results"; echo 'Locate candidate source' >"$F/fail-pattern"
  check 4 'T6 retrieval failure after the mapper answered reaches review' '' run review -- start --config "$D/cfg.json" --run-dir "$R"
  check 0 'T6 retrieval failure: classified unavailable with coverage unknown' 'unavailable unknown' sh -c 'printf "%s %s" "$(jq -r .map_prepasses.only.status "$1")" "$(jq -r .coverage "$2")"' _ "$R/state.json" "$R/map/$mk/coverage.json"
  check 0 'T6 retrieval failure: partial retrieval bytes are retained separately' 'injected failure' cat "$R/map/$mk/response-retrieval-partial.txt"
  check 0 'T6 retrieval failure: known usage components are retained' true jq -e --argjson k "$known" '.map_prepasses.only.usage==$k' "$R/state.json"
  finish 'T6 retrieval failure'
  fcase validation
  check 4 'T6 validation helper failure reaches review' '' run review 'FAIL_PY_MATCH=response.txt --max-output-bytes' -- start --config "$D/cfg.json" --run-dir "$R"
  check 0 'T6 validation helper failure: classified unavailable with coverage unknown' 'unavailable unknown' sh -c 'printf "%s %s" "$(jq -r .map_prepasses.only.status "$1")" "$(jq -r .coverage "$2")"' _ "$R/state.json" "$R/map/$mk/coverage.json"
  check 0 'T6 validation helper failure: raw response retained byte-for-byte' '' cmp "$D/mapper-response" "$R/map/$mk/response.txt"
  check 0 'T6 validation helper failure: known usage components are retained' true jq -e --argjson k "$known" '.map_prepasses.only.usage==$k' "$R/state.json"
  finish 'T6 validation helper failure'
  fcase ingestion
  check 4 'T6 ingestion failure reaches review' '' run review 'FAIL_PY_MATCH=council_codemap.py ingest' -- start --config "$D/cfg.json" --run-dir "$R"
  after_output 'ingestion failure' unavailable
  check 0 'T6 ingestion failure: raw response retained byte-for-byte' '' cmp "$D/mapper-response" "$R/map/$mk/response.txt"
  finish 'T6 ingestion failure'
  fcase coverage
  check 4 'T6 coverage publication failure after successful captures reaches review' '' run review 'FAIL_MV_GLOB=*/coverage.json' FAIL_MV_COUNT=1 -- start --config "$D/cfg.json" --run-dir "$R"
  after_output 'coverage publication failure after a usable capture' partial
  check 0 'T6 coverage publication failure: the successful capture survives in coverage' 'src/a.py ok' jq -r '.capture_statuses[]|"\(.path) \(.status)"' "$R/map/$mk/coverage.json"
  check 0 'T6 coverage publication failure: the failure is recorded' 'coverage artifact publication failed' jq -r .map_prepasses.only.failure "$R/state.json"
  finish 'T6 coverage publication failure'
  fcase terminal
  check 2 'T6 finalization that cannot persist checkpoints (exit 2)' '' run first 'FAIL_MV_GLOB=*/coverage.json' FAIL_MV_COUNT=99 -- start --config "$D/cfg.json" --run-dir "$R"
  check 0 'T6 unpersistable finalization: state keeps the recoverable dispatched checkpoint' 'failed dispatched' jq -r '"\(.status) \(.map_prepasses.only.status)"' "$R/state.json"
  check 4 'T6 unpersistable finalization: plain resume recovers without a new prompt' '' run review -- resume --run-dir "$R"
  check 0 'T6 unpersistable finalization: recovered result is complete' complete jq -r .map_prepasses.only.status "$R/state.json"
  finish 'T6 unpersistable finalization'
  # ---- 4(d) accounting: complete, failed, recovered, orphan-session and unknown-usage sessions ----
  for u in partial none; do
    fcase "usage-$u"
    if [ $u = partial ]; then echo '{"data":{"tokens":{"input":100}}}' >"$F/mapper-session-usage"; else : >"$F/mapper-session-usage"; fi
    check 4 "4(d) mapper usage $u: start reaches review" '' run review -- start --config "$D/cfg.json" --run-dir "$R"
    finish "4(d) mapper usage $u"
  done
  acct() {  # case known-tokens cost-text unknown-token-sessions unknown-cost-sessions report-cost
    local C="$D/case-$1" label="4(d) accounting $1"
    ( cd "$D" && bash "$D/impl/scripts/council.sh" status --run-dir "$C/run" >"$C/acct.status" 2>&1 )
    ( cd "$D" && bash "$D/impl/scripts/council.sh" report --run-dir "$C/run" >"$C/acct.report" 2>&1 )
    local line="MAP PRE-PASS SUBTOTAL: $2 known tokens / $3 known cost across 1 unique sessions; token components unknown for: $4; cost unknown for: $5"
    check 0 "$label: status counts the mapper session once with explicit unknowns" "$line" cat "$C/acct.status"
    check 0 "$label: transcript carries the identical subtotal" '' grep -qF -- "$line" "$C/run/transcript.md"
    check 0 "$label: report agrees on known mapper tokens and the single session" "Map pre-pass subtotal: $2 known tokens across 1 unique session(s); cost_usd=$6 known components; token usage unknown for $4; cost unknown for $5" cat "$C/acct.report"
    check 0 "$label: status, transcript and report RUN TOTAL tokens agree" true sh -c 'a=$(sed -n "s/^RUN TOTAL (known components): \([0-9]*\) tokens.*/\1/p" "$1"); b=$(sed -n "s/^RUN TOTAL (known components): \([0-9]*\) tokens.*/\1/p" "$2" | head -1); c=$(sed -n "s/^RUN TOTAL (known components): \([0-9]*\) known tokens.*/\1/p" "$3" | head -1); [ -n "$a" ] && [ "$a" = "$b" ] && [ "$a" = "$c" ] && echo true' _ "$C/acct.status" "$C/run/transcript.md" "$C/acct.report"
    check 0 "$label: mapper elapsed and user review overhead are measured" true sh -c 'grep -Eq "elapsed=[0-9]+s" "$1" && grep -Eq "user review time: [0-9]+s" "$1" && echo true' _ "$C/acct.report"
  }
  acct cfgauth-before 6010 '$0.01' none none 0.01
  acct dispatched-after 6010 '$0.01' none none 0.01
  acct retrieval 6010 '$0.01' none none 0.01
  acct created-before 6010 '$0.01' none none 0.01
  acct usage-partial 100 UNKNOWN ses_fake1 ses_fake1 UNKNOWN
  acct usage-none 0 UNKNOWN ses_fake1 ses_fake1 UNKNOWN
  check 0 '4(d) partial usage keeps the known component and marks the rest unknown' true jq -e '.map_prepasses.only.usage=={"input":100,"cache_read":null,"cache_write":null,"output":null,"reasoning":null,"cost":null}' "$D/case-usage-partial/run/state.json"
  check 0 '4(d) partial usage: report lists every unknown field' 'usage_fields_known=input, usage_fields_unknown=cache_read,cache_write,output,reasoning,cost' cat "$D/case-usage-partial/acct.report"
  check 0 '4(d) unreadable session telemetry leaves every usage field unknown, not zero' 'usage values: UNKNOWN' cat "$D/case-usage-none/acct.report"
  check 0 '4(d) the orphan session whose identity was never persisted was never prompted' 0 sh -c 'grep -c "^prompt ses_fake1 " "$1" || true' _ "$D/case-created-before/fo/calls.log"
)
# T4 through real council.sh processes: offline contract rejection at approval, deterministic
# mutation between approval and launch inside the approving process, tampering with every approved
# identity, sibling-modified/created prerequisites produced by an executor, gate-before-handover
# ordering, and legitimate executor modifications/creates/deletes that must still pass.
split_gate_process_tests() (
  D="$scratch/split-gate"; P="$D/proj"; mkdir -p "$D/impl" "$P/src"
  cp -R "$HERE" "$D/impl/" || exit 1; write_fake_oc "$D/impl/scripts/oc.sh"; write_fault_bin "$D/bin"
  reset_proj() {  # rewrite in place: baseline identities include the file identity, not only bytes
    mkdir -p "$P/src"; rm -f "$P/src/new.py" "$P/src/gen.py"
    printf 'alpha\n' >"$P/src/a.py"; printf 'beta\n' >"$P/src/b.py"; printf 'obsolete\n' >"$P/src/old.py"; printf 'shared\n' >"$P/keep.txt"
  }
  reset_proj
  mkdir -p "$D/fo0"; printf '%s\n' '{"candidates":[{"path":"src/a.py"},{"path":"src/b.py"}],"unresolved":[],"stopped_reason":"done"}' >"$D/fo0/mapper-response"
  jq -n --arg d "$P" '{dir:$d,max_rounds:2,timeout_s:30,handover_at:0.5,executor:"A",map_code:true,map_prepass:{kind:"opencode",model:"p/m",effort:"medium"},members:[{id:"A",kind:"opencode",model:"p/m",effort:"medium",mode:"edit"},{id:"B",kind:"opencode",model:"p/m",effort:"medium",mode:"read"}],tasks:[{id:"parent",text:"change a and b",execute:true}]}' >"$D/cfg.json"
  R="$D/run"; F="$D/fo"; C="$D"
  run() {
    local label=$1; shift; local -a envs=()
    while [ "$1" != -- ]; do envs+=("$1"); shift; done; shift
    ( cd "$D" && env PATH="$D/bin:$PATH" FAKE_OC_DIR="$F" FAIL_FLAG="$D/flag-$label" "${envs[@]}" bash "$D/impl/scripts/council.sh" "$@" >"$D/$label.out" 2>"$D/$label.err" )
  }
  prompts() { grep -c '^prompt ' "$F/calls.log"; }
  save() { rm -rf "$D/snap-$1"; mkdir -p "$D/snap-$1"; cp -Rp "$R" "$D/snap-$1/run"; cp -Rp "$F" "$D/snap-$1/fo"; }
  restore() { rm -rf "$R" "$F" "$D"/flag-*; reset_proj; cp -Rp "$D/snap-$1/run" "$R"; cp -Rp "$D/snap-$1/fo" "$F"; }
  cp -Rp "$D/fo0" "$F"
  check 4 'T4 gate fixture: real mapped start pauses at map review' '' run start -- start --config "$D/cfg.json" --run-dir "$R"
  seed=$(sed -n 's/^Split contract map_seed_id (copy this exact value into contract JSON): //p' "$D/start.out")
  save review
  sha() { shasum -a 256 <"$P/$1" | cut -d' ' -f1; }
  sa=$(sha src/a.py); sb=$(sha src/b.py); so=$(sha src/old.py); sk=$(sha keep.txt); zero=$(printf '%064d' 0)
  # contract NAME child-a-json child-b-json
  contract() { jq -n --arg seed "$seed" --argjson a "$2" --argjson b "$3" '{schema_version:1,parent_id:"parent",map_seed_id:$seed,subtasks:[$a,$b]}' >"$D/$1.json"; printf '{"action":"split","contract_file":"%s.json"}\n' "$1" >"$D/decide-$1.json"; }
  child() {  # id requires modifies deletes creates
    jq -n --arg id "$1" --argjson r "$2" --argjson m "$3" --argjson d "$4" --argjson c "$5" '{id:$id,text:("do "+$id),execute:true,requires:$r,modifies:$m,deletes:$d,creates:$c,acceptance:[],unresolved:[]}'
  }
  f() { printf '{"path":"%s","sha256":"%s"}' "$1" "$2"; }
  ca=$(child child-a "[$(f src/a.py "$sa")]" "[$(f src/a.py "$sa")]" "[$(f src/old.py "$so")]" '["src/new.py"]')
  cb=$(child child-b "[$(f src/b.py "$sb"),$(f keep.txt "$sk")]" "[$(f src/b.py "$sb")]" '[]' '["src/gen.py"]')
  contract good "$ca" "$cb"
  # ---- rejected at approval, before any member inference ----
  reject() {  # label contract-name expected-diagnostic
    restore review; local before; before=$(prompts)
    check 4 "T4 $1: approval is rejected offline (exit 4)" '' run "rej-$2" -- resume --run-dir "$R" --map-decision "$D/decide-$2.json"
    check 0 "T4 $1: precise diagnostic" "$3" cat "$D/rej-$2.err"
    check 0 "T4 $1: stays at map review with no approved contract" true jq -e '.phase=="map_review" and .status=="questions" and .map_prepasses.parent.approved_contract==null' "$R/state.json"
    check 0 "T4 $1: zero member inference" "$before" prompts
  }
  contract sibmod "$ca" "$(child child-b "[$(f src/a.py "$sa")]" "[$(f src/b.py "$sb")]" '[]' '[]')"
  reject 'sibling-modified prerequisite (child-b reads what child-a modifies)' sibmod 'cross-child read/write or write/write overlap'
  contract sibcreate "$ca" "$(child child-b "[$(f src/new.py "$zero")]" "[$(f src/b.py "$sb")]" '[]' '[]')"
  reject 'sibling-created prerequisite (child-b requires what child-a creates)' sibcreate 'subtask child-b requires src/new.py created by sibling child-a'
  contract cycle "$(child child-a "[$(f src/y.py "$zero")]" '[]' '[]' '["src/x.py"]')" "$(child child-b "[$(f src/x.py "$zero")]" '[]' '[]' '["src/y.py"]')"
  reject 'dependency cycle between siblings' cycle 'subtask child-b requires src/x.py created by sibling child-a'
  check 0 'T4 dependency cycle: the reverse edge is reported too' 'subtask child-a requires src/y.py created by sibling child-b' cat "$D/rej-cycle.err"
  contract missing "$(child child-a "[$(f src/unmapped.py "$sa")]" '[]' '[]' '[]')" "$cb"
  reject 'declared prerequisite absent from the project and from the incomplete map' missing 'src/unmapped.py'
  contract stale "$(child child-a "[$(f src/a.py "$zero")]" "[$(f src/a.py "$sa")]" '[]' '[]')" "$cb"
  reject 'declared baseline digest differs from the file' stale 'src/a.py'
  jq '.map_seed_id="not-the-reviewed-seed"' "$D/good.json" >"$D/wrongseed.json"; printf '{"action":"split","contract_file":"wrongseed.json"}\n' >"$D/decide-wrongseed.json"
  reject 'contract names a different map seed' wrongseed 'map_seed_id does not match the reviewed map'
  # ---- deterministic mutation between approval and launch, inside the approving process ----
  restore review; before=$(prompts)
  check 4 'T4 approval-to-launch mutation: the launch gate stops the approving process' '' run mutate MUTATE_AFTER_PY_MATCH=--identity-output "MUTATE_CMD=printf drift >>'$P/keep.txt'" -- resume --run-dir "$R" --map-decision "$D/decide-good.json"
  check 0 'T4 approval-to-launch mutation: the mutation ran after approval validated' '' test -e "$D/flag-mutate.mut"
  check 0 'T4 approval-to-launch mutation: split_invalid names the drifted prerequisite' 'keep.txt' jq -r 'select(.phase=="split_invalid")|.pending_questions[0].question' "$R/state.json"
  check 0 'T4 approval-to-launch mutation: zero member inference' "$before" prompts
  # ---- approved checkpoint, then tampering with every approved identity ----
  restore review
  echo 2 >"$F/fail-results"; echo 'TASK child-a|could not be used' >"$F/fail-pattern"
  check 2 'T4 tamper fixture: approved split checkpoints after a member failure' '' run approve -- resume --run-dir "$R" --map-decision "$D/decide-good.json"
  rm -f "$F/fail-results" "$F/fail-pattern"; save approved
  tamper() {  # label expected-diagnostic shell-snippet
    restore approved; local before; before=$(prompts)
    ( cd "$D" && R="$R" P="$P" sh -c "$3" ) || { echo "tamper setup failed: $1"; exit 1; }
    check 4 "T4 tamper $1: ordinary resume stops at the launch gate" '' run "tamper" -- resume --run-dir "$R"
    check 0 "T4 tamper $1: split_invalid with a precise diagnostic" "$2" jq -r 'select(.phase=="split_invalid")|.pending_questions[0].question' "$R/state.json"
    check 0 "T4 tamper $1: zero inference" "$before" prompts
  }
  st_edit() { printf 'jq %s "$R/state.json" >"$R/s.tmp" && mv "$R/s.tmp" "$R/state.json"' "$1"; }
  tamper 'archived contract bytes' 'approved contract digest mismatch' 'printf " " >>"$(jq -r .map_prepasses.parent.approved_contract.contract_file "$R/state.json")"'
  tamper 'approved seed identity' 'approved map seed identity mismatch' "$(st_edit "'.map_prepasses.parent.approved_contract.seed_id=\"forged\"'")"
  tamper 'approved parent identity' 'approved contract parent identity mismatch' "$(st_edit "'.map_prepasses.parent.approved_contract.parent_id=\"other\"'")"
  tamper 'child task contract identity' 'child task identity does not match the approved contract' "$(st_edit "'.config.tasks[.task_idx].contract_identity=\"forged\"'")"
  tamper 'child id absent from the contract' 'child child-z is absent from the approved contract' "$(st_edit "'.config.tasks[.task_idx].id=\"child-z\" | .task_id=\"child-z\"'")"
  tamper 'baseline identity file' 'approved baseline identity digest mismatch' 'printf " " >>"$(jq -r .map_prepasses.parent.approved_contract.identity_file "$R/state.json")"'
  tamper 'untouched prerequisite deleted' 'subtask child-a requires src/a.py: missing' 'rm -f "$P/src/a.py"'
  # An unreadable prerequisite is caught even earlier: the code map's resume freshness re-check
  # cannot verify the source, so the run checkpoints (exit 2) before the launch gate or any inference.
  restore approved; before=$(prompts); chmod 000 "$P/src/a.py"
  check 2 'T4 unreadable prerequisite: resume checkpoints before any launch' '' run unreadable -- resume --run-dir "$R"
  check 0 'T4 unreadable prerequisite: the unverifiable guard is named' 'still unverifiable on resume' cat "$D/unreadable.err"
  check 0 'T4 unreadable prerequisite: zero inference' "$before" prompts
  chmod 644 "$P/src/a.py"
  # Gate ordering: a member due for handover is not handed over (no handover inference) when the gate fails.
  tamper 'drift while a member is due for handover' 'src/a.py' "printf drift >>\"\$P/src/a.py\"; $(st_edit "'.members[1].session_calls=5 | .members[1].ctx_used=9000 | .members[1].ctx_limit=10000'")"
  check 0 'T4 gate runs before handover: no handover prompt was written' '' sh -c '! ls "$1"/prompts/handover-* >/dev/null 2>&1' _ "$R"
  # ---- legitimate executor modifications, creates and deletes pass; sibling-caused drift does not ----
  cat >"$D/exec-hook" <<'SH'
#!/bin/sh
grep -q 'child-a — EXECUTION' "$1" || exit 0
[ -e "$FAKE_OC_DIR/exec-done" ] && exit 0; : >"$FAKE_OC_DIR/exec-done"
printf 'alpha edited\n' >"$PROJ/src/a.py"; printf 'new\n' >"$PROJ/src/new.py"; rm -f "$PROJ/src/old.py"
case "$EXTRA" in keep) printf 'sibling wrote this\n' >>"$PROJ/keep.txt";; gen) printf 'sibling created this\n' >"$PROJ/src/gen.py";; esac
SH
  chmod +x "$D/exec-hook"
  for extra in none keep gen; do
    restore review; cp "$D/exec-hook" "$F/result-hook"
    if [ $extra = none ]; then
      check 0 'T4 declared executor modify/create/delete: the split runs to completion' '' run legit PROJ="$P" EXTRA=none -- resume --run-dir "$R" --map-decision "$D/decide-good.json"
      check 0 'T4 declared executor writes: both children ratified, parent split done' true jq -e '.status=="done" and ([.results[]|select(.outcome=="ratified")|.task]|sort)==["child-a","child-b"]' "$R/state.json"
      check 0 'T4 declared executor writes really happened' true sh -c 'grep -q edited "$1/src/a.py" && test -e "$1/src/new.py" && ! test -e "$1/src/old.py" && echo true' _ "$P"
    else
      case $extra in keep) what='sibling-modified prerequisite written by child-a executor'; diag='keep.txt';; gen) what='sibling-created path written by child-a executor'; diag='src/gen.py';; esac
      check 4 "T4 $what: child-b launch is refused" '' run "sib-$extra" PROJ="$P" EXTRA=$extra -- resume --run-dir "$R" --map-decision "$D/decide-good.json"
      check 0 "T4 $what: child-a was ratified before the refusal" ratified jq -r '[.results[]|select(.task=="child-a")][0].outcome' "$R/state.json"
      check 0 "T4 $what: split_invalid names the path" "$diag" jq -r 'select(.phase=="split_invalid" and .task_id=="child-b")|.pending_questions[0].question' "$R/state.json"
      check 0 "T4 $what: no child-b prompt was delivered" '' sh -c '! ls "$1"/prompts/child-b-* >/dev/null 2>&1' _ "$R"
    fi
  done
  # ---- ratification-round replacement with an archival interruption, then a clean replacement ----
  restore review
  echo 2 >"$F/fail-results"; echo 'TASK child-b — RATIFICATION|could not be used' >"$F/fail-pattern"
  check 2 'T4 ratification-round fixture: child-b ratification is interrupted' '' run ratfix -- resume --run-dir "$R" --map-decision "$D/decide-good.json"
  rm -f "$F/fail-results" "$F/fail-pattern"
  old_digest=$(jq -r .map_prepasses.parent.approved_contract.digest "$R/state.json")
  printf 'drift during ratification\n' >"$P/keep.txt"
  check 4 'T4 ratification-round drift reaches split_invalid' '' run ratdrift -- resume --run-dir "$R"
  sk2=$(sha keep.txt)
  jq --arg sk "$sk2" '.subtasks|=map(.text=(.text+" (contract two)") | .requires|=map(if .path=="keep.txt" then .sha256=$sk else . end))' "$D/good.json" >"$D/two.json"
  printf '{"action":"split","contract_file":"two.json"}\n' >"$D/decide-two.json"
  before=$(prompts)
  check 4 'T4 archival interrupted mid-way during ratification-round replacement stays checkpointed' '' run archfail 'FAIL_MV_GLOB=*/superseded/*/posts/' FAIL_MV_COUNT=1 -- resume --run-dir "$R" --map-decision "$D/decide-two.json"
  check 0 'T4 archival interruption: the old approval and split_invalid checkpoint are retained' true jq -e --arg o "$old_digest" '.phase=="split_invalid" and .map_prepasses.parent.approved_contract.digest==$o' "$R/state.json"
  check 0 'T4 archival interruption: the failure is reported' 'cannot retire superseded post' cat "$D/archfail.err"
  check 0 'T4 archival interruption: zero inference' "$before" prompts
  check 0 'T4 clean replacement after the archival interruption runs to completion' '' run replace2 -- resume --run-dir "$R" --map-decision "$D/decide-two.json"
  check 0 'T4 replacement: no post of the superseded contract was reused' '' sh -c '! grep -q "reusing its" "$1"' _ "$D/replace2.err"
  check 0 'T4 replacement: every member received a fresh contract-two round-1 prompt for both children' true sh -c 'for c in child-a child-b; do for m in A B; do grep -q "contract two" "$1/prompts/$c-r1-$m.md" || exit 1; done; done; echo true' _ "$R"
  check 0 'T4 replacement: ratified outcomes of the old contract are history under the old identity' true jq -e --arg o "$old_digest" '.result_history[-1].contract_identity==$o and ([.result_history[-1].results[].task]|index("child-a"))!=null and .status=="done"' "$R/state.json"
  check 0 'T4 replacement: the interrupted ratification post of contract one is archived under the old identity' true sh -c 'ls "$1"/map/*/superseded/"$2"*/posts/child-b-x1-B.md >/dev/null 2>&1 && echo true' _ "$R" "$old_digest"
  check 0 'T4 replacement: both children of contract two are ratified' true jq -e --arg o "$old_digest" '([.results[]|select(.outcome=="ratified")|.task]|sort)==["child-a","child-b"] and .map_prepasses.parent.approved_contract.digest!=$o' "$R/state.json"
)
# T5 through real council.sh processes: a capture made after the seed (a member's code_reads)
# appears only in later snapshots; earlier delivered snapshots stay immutable; seed entries keep
# reports=[] while the later member report is preserved; the pre-pass freshness guard handles edits
# outside an excerpt, deletion, symlink retargeting, failed verification and stale replay; executor
# writes are never guarded; a scope-changing answer keeps the seed with applicability unknown.
mapped_snapshot_process_tests() (
  D="$scratch/mapped-snap"; P="$D/proj"; mkdir -p "$D/impl" "$P/src"
  cp -R "$HERE" "$D/impl/" || exit 1; write_fake_oc "$D/impl/scripts/oc.sh"
  CM="$D/impl/scripts/council_codemap.py"
  reset_proj() {
    mkdir -p "$P/src"; chmod 644 "$P/src/a.py" 2>/dev/null
    printf 'one\ntwo\nthree\n' >"$P/src/a.py"; printf 'beta\n' >"$P/src/b.py"; printf 'later\n' >"$P/src/later.py"
    printf 'real one\n' >"$P/src/real1.py"; printf 'real two\n' >"$P/src/real2.py"; ln -sfn real1.py "$P/src/link.py"
  }
  reset_proj
  mkdir -p "$D/fo0"; printf '%s\n' '{"candidates":[{"path":"src/a.py","lines":[1,2]},{"path":"src/b.py"},{"path":"src/link.py"}],"unresolved":[],"stopped_reason":"done"}' >"$D/fo0/mapper-response"
  jq -n --arg d "$P" '{dir:$d,max_rounds:3,timeout_s:30,handover_at:0.5,executor:"A",map_code:true,map_prepass:{kind:"opencode",model:"p/m",effort:"medium"},members:[{id:"A",kind:"opencode",model:"p/m",effort:"medium",mode:"edit"},{id:"B",kind:"opencode",model:"p/m",effort:"medium",mode:"read"}],tasks:[{id:"parent",text:"change a and b",execute:true}]}' >"$D/cfg.json"
  printf '{"action":"keep"}\n' >"$D/keep.json"
  R="$D/run"; F="$D/fo"
  run() { local label=$1; shift; ( cd "$D" && FAKE_OC_DIR="$F" bash "$D/impl/scripts/council.sh" "$@" >"$D/$label.out" 2>"$D/$label.err" ); }
  save() { rm -rf "$D/snap-$1"; mkdir -p "$D/snap-$1"; cp -Rp "$R" "$D/snap-$1/run"; cp -Rp "$F" "$D/snap-$1/fo"; }
  restore() { rm -rf "$R" "$F"; reset_proj; cp -Rp "$D/snap-$1/run" "$R"; cp -Rp "$D/snap-$1/fo" "$F"; }
  snap_of() { sed -n 's/^snapshot_id: //p' "$1" | head -1; }
  lookup_all() { printf '{"include_evidence":false}' | python3 "$CM" lookup --run-dir "$R" --snapshot "$1"; }
  cp -Rp "$D/fo0" "$F"
  check 4 'T5 snapshot fixture: mapped start pauses at review' '' run start start --config "$D/cfg.json" --run-dir "$R"
  save review
  # ---- a later capture, snapshot immutability, and report preservation (own two-task run) ----
  R="$D/run-reports"; F="$D/fo-reports"; cp -Rp "$D/fo0" "$F"
  jq '.tasks+=[{id:"next",text:"a next original task"}]' "$D/cfg.json" >"$D/cfg-two.json"
  check 4 'T5 two-task fixture: mapped start pauses at the first review' '' run rstart start --config "$D/cfg-two.json" --run-dir "$R"
  printf '[{"path":"src/later.py","observed_sha256":"%s","conclusion":"later capture by a member"}]\n' "$(shasum -a 256 <"$P/src/later.py" | cut -d' ' -f1)" >"$F/code-reads"
  echo 'round 2 of' >"$F/code-reads-pattern"
  check 4 'T5 keep continuation with a member code_reads report reaches the next task review' '' run rkeep resume --run-dir "$R" --map-decision "$D/keep.json"
  check 0 'T5 the next original task completes after its own keep' '' run rkeep2 resume --run-dir "$R" --map-decision "$D/keep.json"
  r1=$(snap_of "$R/prompts/parent-r1-A.md"); r2=$(snap_of "$R/prompts/parent-r2-A.md"); ex=$(snap_of "$R/prompts/parent-exec1-A.md"); n2=$(snap_of "$R/prompts/next-r2-A.md")
  check 0 'T5 round-1, round-2, execution and later-task round-2 prompts each delivered a snapshot' true sh -c '[ -n "$1" ] && [ -n "$2" ] && [ -n "$3" ] && [ -n "$4" ] && echo true' _ "$r1" "$r2" "$ex" "$n2"
  lookup_all "$r1" >"$D/r1.json"; lookup_all "$r2" >"$D/r2.json"; lookup_all "$ex" >"$D/ex.json"; lookup_all "$n2" >"$D/n2.json"
  check 0 'T5 the round-1 snapshot holds only seed entries, all raw-only (reports=[])' true jq -e '([.entries[].path]|sort)==["src/a.py","src/b.py","src/link.py"] and all(.entries[]; .reports==[])' "$D/r1.json"
  check 0 'T5 the round-2 snapshot (before the report was published) has no later capture' true jq -e '([.entries[].path]|index("src/later.py"))==null' "$D/r2.json"
  check 0 'T5 the historical execution snapshot contains the later capture' true jq -e '([.entries[].path]|index("src/later.py"))!=null' "$D/ex.json"
  check 0 'T5 the historical execution snapshot is a raw-only projection (no author reports)' true jq -e 'all(.entries[]; .reports==[])' "$D/ex.json"
  check 0 'T5 a later deliberation snapshot preserves the optional member reports on the later capture' true jq -e '[.entries[]|select(.path=="src/later.py")|(.reports|length)>0]==[true]' "$D/n2.json"
  check 0 'T5 seed entries keep reports=[] in the later deliberation snapshot' true jq -e 'all(.entries[]|select(.path!="src/later.py"); .reports==[])' "$D/n2.json"
  check 0 'T5 the index keeps the attributed member reports for the later capture' true jq -e '[.entries[]|select(.path=="src/later.py")|.reports[].reader|select(.task=="parent" and .step=="r2")|.member]|sort==["A","B"]' "$R/codemap/index.json"
  check 0 'T5 earlier snapshots are immutable: re-looking up round 1 after later publication is byte-identical' '' sh -c 'printf "{\"include_evidence\":false}" | python3 "$1" lookup --run-dir "$2" --snapshot "$3" | cmp - "$4"' _ "$CM" "$R" "$r1" "$D/r1.json"
  check 0 'T5 the execution prompt frames its snapshot as historical' 'HISTORICAL CODE MAP' cat "$R/prompts/parent-exec1-A.md"
  R="$D/run"; F="$D/fo"
  # ---- freshness guard during a deliberation round ----
  fresh() {  # label hook-command expected-log
    restore review
    printf '#!/bin/sh\ngrep -q "round 2 of" "$1" || exit 0\n[ -e "$FAKE_OC_DIR/hooked" ] && exit 0; : >"$FAKE_OC_DIR/hooked"\n%s\n' "$2" >"$F/result-hook"; chmod +x "$F/result-hook"
    local rc; run "fresh-$1" resume --run-dir "$R" --map-decision "$D/keep.json"; rc=$?
    check 0 "T5 freshness $1: the in-round source change is detected" "$3" cat "$D/fresh-$1.err"
    check 0 "T5 freshness $1: the round did not reach a decision on stale evidence (checkpoint)" 2 echo "$rc"
    chmod 644 "$P/src/a.py" 2>/dev/null
    check 0 "T5 freshness $1: ordinary resume replays the round and completes" '' run "fresh-$1-resume" resume --run-dir "$R"
    if [ "${4:-stale}" = stale ]; then
      check 0 "T5 freshness $1: no stale round-2 post is reused on replay" '' sh -c '! grep -q "reusing its r2 post" "$1"' _ "$D/fresh-$1-resume.err"
    else  # an unverifiable guard is not a stale verdict: the preserved votes are used once verified
      check 0 "T5 freshness $1: preserved round-2 votes are reused after verification succeeds" 'reusing its r2 post' cat "$D/fresh-$1-resume.err"
    fi
    check 0 "T5 freshness $1: run done" done jq -r .status "$R/state.json"
  }
  fresh 'edit outside the captured excerpt' "printf 'four\\\\n' >>'$P/src/a.py'" 'changed during the round'
  fresh 'deletion of a captured source' "rm -f '$P/src/b.py'" 'changed during the round'
  fresh 'symlink retarget' "ln -sfn real2.py '$P/src/link.py'" 'changed during the round'
  fresh 'failed verification (unreadable source)' "chmod 000 '$P/src/a.py'" 'pending_verification' preserved
  # ---- stale terminal replay: a checkpointed round whose guarded source changes before resume ----
  restore review
  echo 2 >"$F/fail-results"; echo 'round 2 of|could not be used' >"$F/fail-pattern"
  check 2 'T5 stale replay fixture: round 2 is interrupted with a surviving post' '' run stale1 resume --run-dir "$R" --map-decision "$D/keep.json"
  rm -f "$F/fail-results" "$F/fail-pattern"
  check 0 'T5 stale replay fixture: a round-2 post survived the interruption' '' sh -c 'ls "$1"/posts/parent-r2-*.md >/dev/null 2>&1' _ "$R"
  printf 'four\n' >>"$P/src/a.py"
  check 0 'T5 stale replay: resume after a guarded change completes' '' run stale2 resume --run-dir "$R"
  check 0 'T5 stale replay: the change while checkpointed purges the stale posts' 'changed while checkpointed' cat "$D/stale2.err"
  check 0 'T5 stale replay: no stale round-2 post is reused' '' sh -c '! grep -q "reusing its r2 post" "$1"' _ "$D/stale2.err"
  # ---- executor writes are never guarded ----
  restore review
  printf '#!/bin/sh\ngrep -q -- "— EXECUTION ===" "$1" || exit 0\nprintf "executor edit\\n" >>"%s"\n' "$P/src/a.py" >"$F/result-hook"; chmod +x "$F/result-hook"
  check 0 'T5 executor edits of a mapped source do not trip any freshness guard' '' run execw resume --run-dir "$R" --map-decision "$D/keep.json"
  check 0 'T5 executor edit really happened and no freshness checkpoint was logged' true sh -c 'grep -q "executor edit" "$1" && ! grep -q "changed during the round\|pending_verification\|changed while checkpointed" "$2" && echo true' _ "$P/src/a.py" "$D/execw.err"
  # ---- scope-changing answer: the seed is retained with applicability unknown ----
  restore review; printf 'change a and b' >"$F/ask-once"
  seed_snap=$(jq -r .map_prepasses.parent.seed_snapshot_id "$R/state.json")
  check 4 'T5 a member question after keep pauses for the user' '' run ask resume --run-dir "$R" --map-decision "$D/keep.json"
  check 0 'T5 answer continues the kept task to completion' '' run answer resume --run-dir "$R" --answer 'Use the v2 layout'
  check 0 'T5 scope-changing answer marks applicability unknown and keeps the seed' true jq -e --arg s "$seed_snap" '.map_prepasses.parent.scope_applicability=="unknown after scope-changing answers" and .map_prepasses.parent.seed_snapshot_id==$s and .map_prepasses.parent.status=="complete"' "$R/state.json"
  check 0 'T5 scope-changing answer never remaps' 1 grep -c 'Council map pre-pass' "$F/calls.log"
  check 0 'T5 the answer reaches subsequent member work' 'Use the v2 layout' cat "$R/prompts/parent-r1-B.md"
)
# Section (d) permission matrix at the adapter boundary: the exact mapper arguments emitted by a real
# council.sh start are fed to the REAL oc.sh option parser and session-body builder, and the
# resulting ruleset is evaluated with OpenCode's own semantics (verified in the installed OpenCode
# build: the last rule whose action AND resource wildcard-match wins, default "ask"; `*` matches any
# characters including "/"; in-project file resources are project-relative, outside paths are
# absolute and additionally need external_directory "<dir>/*"; grep/glob are authorized by their
# search pattern, shell/subagent are the command/delegation actions).
mapper_permission_boundary_tests() (
  load oc.sh
  D="$scratch/perm-boundary"; mkdir -p "$D/impl" "$D/outside/sub" "$D/outside2"
  cp -R "$HERE" "$D/impl/" || exit 1; write_fake_oc "$D/impl/scripts/oc.sh"; write_fault_bin "$D/bin"
  printf 'secret\n' >"$D/outside/secret.py"; printf 'deeper secret\n' >"$D/outside/sub/s.py"; printf 'other secret\n' >"$D/outside2/secret.py"
  mk=$(printf only | shasum -a 256 | cut -c1-16)
  cat >"$D/evaluate.py" <<'PY2'
import json,re,sys
def match(value,pattern):  # OpenCode's wildcard matcher, transcribed
    v=value.replace("\\","/"); p=re.sub(r'[.+^${}()|[\]\\]',lambda m:"\\"+m.group(0),pattern.replace("\\","/"))
    p=p.replace("*",".*").replace("?",".")
    if p.endswith(" .*"): p=p[:-3]+"( .*)?"
    return re.fullmatch(p,v,re.S) is not None
def evaluate(action,resource,rules):  # last matching rule wins; unmatched -> ask
    hit=[r for r in rules if match(action,r["action"]) and match(resource,r["resource"])]
    return hit[-1]["effect"] if hit else "ask"
rules=json.load(open(sys.argv[1]))["permissions"]
for line in sys.stdin:
    if not line.strip(): continue
    want,*reqs=line.rstrip("\n").split("\t")
    got=[evaluate(*r.split(" ",1),rules) for r in reqs]
    eff="deny" if "deny" in got else ("ask" if "ask" in got else "allow")
    print(("ok " if eff==want else "WRONG ")+want+" got "+eff+" for "+" + ".join(reqs))
PY2
  # pstart LABEL PROJECT [VAR=value ...] -> real mapped start (run dir inside the project), its own adapter dir
  pstart() {
    local label=$1 proj=$2; shift 2
    F="$D/fo-$label"; R="$proj/.council-run"; rm -rf "$F"; mkdir -p "$F"; cp "$D/mapper-response" "$F/mapper-response"
    jq -n --arg d "$proj" '{dir:$d,max_rounds:2,timeout_s:30,handover_at:0.5,executor:null,map_code:true,map_prepass:{kind:"opencode",model:"p/m",effort:"medium"},members:[{id:"A",kind:"opencode",model:"p/m",effort:"medium",mode:"read"},{id:"B",kind:"opencode",model:"p/m",effort:"medium",mode:"read"}],tasks:[{id:"only",text:"investigate"}]}' >"$D/cfg-$label.json"
    ( cd "$D" && env PATH="$D/bin:$PATH" FAKE_OC_DIR="$F" FAIL_FLAG="$D/flag-$label" "$@" bash "$D/impl/scripts/council.sh" start --config "$D/cfg-$label.json" --run-dir "$R" >"$D/$label.out" 2>"$D/$label.err" )
  }
  # body LABEL -> the REAL oc.sh option parser and session-body builder applied to the emitted mapper arguments
  body() {
    args=(); while IFS= read -r -d '' a; do args+=("$a"); done <"$D/fo-$1/new-args-1"
    RULES="[]"; AUTO=true; parse_opts "${args[@]}"; MODEL_JSON=$(model_json)
    api() { [ "$1" = POST ] && printf '%s' "$3" >"$D/$label-body.json"; echo '{"data":{"id":"ses_x"}}'; }
    label=$1; create_session
  }
  matrix() {  # label tsv-file -> one check per request
    python3 "$D/evaluate.py" "$D/$1-body.json" <"$2" >"$2.out"
    while IFS= read -r line; do
      check 0 "permission boundary ($1): ${line#* }" '' sh -c 'case "$1" in ok\ *) exit 0;; *) exit 1;; esac' _ "$line"
    done <"$2.out"
    check 0 "permission boundary ($1): every request in the matrix was evaluated" "$(grep -c . "$2")" sh -c 'grep -c . "$1"' _ "$2.out"
    check 0 "permission boundary ($1): no mapper request falls through to an interactive ask" '' sh -c '! grep -q " got ask " "$1"' _ "$2.out"
  }
  common_denies() {
    printf 'deny\tread ../sibling/file.py\n'; printf 'deny\tread ..\n'
    printf 'deny\texternal_directory %s/*\tread %s/secret.py\n' "$D/outside" "$D/outside"
    printf 'deny\texternal_directory /etc/*\tread /etc/passwd\n'
    printf 'deny\tedit src/a.py\n'; printf 'deny\tshell ls\n'; printf 'deny\tbash ls\n'
    printf 'deny\tsubagent explore\n'; printf 'deny\ttask explore\n'
    printf 'deny\twebfetch https://example.com\n'; printf 'deny\twebsearch query\n'; printf 'deny\tskill any\n'
    printf 'deny\tquestion *\n'; printf 'deny\tdoom_loop *\n'
  }
  # ================= D1: a project with no escaping symlink =================
  P="$D/clean"; mkdir -p "$P/src" "$P/.hidden" "$P/.git" "$P/.hg/store" "$P/.svn" "$P/vendor/lib/.git"
  printf 'one\n' >"$P/src/a.py"; printf 'k: v\n' >"$P/.hidden/conf.yaml"; printf '[core]\n' >"$P/.git/config"
  printf 'hg\n' >"$P/.hg/store/data"; printf 'svn\n' >"$P/.svn/entries"; printf 'nested\n' >"$P/vendor/lib/.git/config"
  ln -s src/a.py "$P/inlink.py"; ln -s src "$P/srcalias"
  printf '%s\n' '{"candidates":[{"path":"src/a.py"},{"path":".hidden/conf.yaml"},{"path":".git/config"},{"path":".hg/store/data"},{"path":".council-run/config.json"},{"path":"inlink.py"}],"unresolved":[],"stopped_reason":"done"}' >"$D/mapper-response"
  check 4 'D1 permission boundary: real mapped start of a clean project (run dir inside it) reaches review' '' pstart clean "$P"
  check 0 'D1 permission boundary: the first session is the mapper session' 'Council map pre-pass' sh -c 'tr "\0" "\n" <"$1"' _ "$D/fo-clean/new-args-1"
  check 0 'D1 permission boundary: the real oc.sh builds the session body from the emitted arguments' ses_x body clean
  check 0 'D1 permission boundary: --ask removes the allow-all ruleset (no leading * allow)' true jq -e '.permissions[0]!={action:"*",resource:"*",effect:"allow"} and ([.permissions[]|select(.action=="*" and .resource=="*")|.effect]|first)=="deny"' "$D/clean-body.json"
  {
    printf 'allow\tread src/a.py\n'; printf 'allow\tread .\n'; printf 'allow\tread src\n'
    printf 'allow\tread .hidden/conf.yaml\n'; printf 'allow\tread .github/workflows/ci.yml\n'; printf 'allow\tread src/.gitignore\n'
    printf 'allow\tread .git\n'; printf 'allow\tread .git/config\n'; printf 'allow\tread .git/HEAD\n'; printf 'allow\tread vendor/lib/.git/config\n'
    printf 'allow\tread .hg/store/data\n'; printf 'allow\tread .svn/entries\n'
    printf 'allow\tread .council-run\n'; printf 'allow\tread .council-run/state.json\n'; printf 'allow\tread .council-run/posts/only-r1-A.md\n'
    printf 'allow\tread inlink.py\n'; printf 'allow\tread srcalias/a.py\n'
    printf 'allow\tgrep def main\n'; printf 'allow\tglob **/*.py\n'; printf 'allow\tgrep [core]\n'; printf 'allow\tglob .git/**\n'
    common_denies
  } >"$D/clean.tsv"
  matrix clean "$D/clean.tsv"
  cov="$P/.council-run/map/$mk/coverage.json"
  check 0 'D1 VCS metadata selected by the mapper is captured' 'ok ok' sh -c 'printf "%s %s" "$(jq -r ".capture_statuses[]|select(.path==\".git/config\")|.status" "$1")" "$(jq -r ".capture_statuses[]|select(.path==\".hg/store/data\")|.status" "$1")"' _ "$cov"
  check 0 'D1 a run-directory file selected by the mapper is captured' ok jq -r '.capture_statuses[]|select(.path==".council-run/config.json")|.status' "$cov"
  check 0 'D1 an ordinary hidden project source and an in-project symlink are captured' 'ok ok' sh -c 'printf "%s %s" "$(jq -r ".capture_statuses[]|select(.path==\".hidden/conf.yaml\")|.status" "$1")" "$(jq -r ".capture_statuses[]|select(.path==\"inlink.py\")|.status" "$1")"' _ "$cov"
  check 0 'D1 coverage and mapper input record no orchestrator exclusions' true sh -c 'jq -e ".exclusions==[]" "$1" >/dev/null && jq -e ".exclusions==[]" "$2" >/dev/null && echo true' _ "$cov" "$P/.council-run/map/$mk/input.json"
  check 0 'D1 the clean boundary is recorded (no blocked symlink, search allowed)' true jq -e '.boundary.blocked_symlinks==[] and .boundary.search_denied==false and .boundary.reason==null' "$cov"
  check 0 'D1 the boundary records the probed filesystem case mode' "$(python3 "$HERE/council_map_prepass.py" scan-escapes --root "$P" | jq -c .case_insensitive)" jq -c '.boundary.case_insensitive' "$cov"
  check 0 'D1 the mapper prompt no longer excludes metadata or the run directory' '' sh -c '! grep -q "Excluded from discovery" "$1" && grep -q "^Boundary: only paths inside the project root" "$1" && ! grep -q "grep and glob are unavailable" "$1"' _ "$P/.council-run/map/$mk/instruction.md"
  check 0 'D1 map review reports that grep/glob are allowed' 'grep/glob allowed' cat "$D/clean.out"
  # ================= D2: escaping links are blocked before mapper access =================
  P="$D/esc"; mkdir -p "$P/src" "$P/nested/inner" "$P/.git"
  printf 'one\n' >"$P/src/a.py"; printf 'fine\n' >"$P/nested/other.txt"; printf '[core]\n' >"$P/.git/config"
  ln -s "$D/outside/secret.py" "$P/escape.py"                    # file link out
  ln -s "$D/outside" "$P/outdir"                                  # directory link out
  ln -s "$D/outside/secret.py" "$P/nested/inner/chain2"; ln -s nested/inner/chain2 "$P/chain1"   # chained escape
  ln -s "$D/outside/sub" "$P/nested/deep"; ln -s nested "$P/alias"                              # escape reached through an in-project alias
  ln -s "$D/outside/missing.py" "$P/dangling.py"                  # unresolvable target outside
  ln -s src/a.py "$P/good.py"                                     # in-project link stays readable
  printf '%s\n' '{"candidates":[{"path":"src/a.py"},{"path":"escape.py"},{"path":"good.py"}],"unresolved":[],"stopped_reason":"done"}' >"$D/mapper-response"
  check 4 'D2 permission boundary: real mapped start of a project with escaping links reaches review' '' pstart esc "$P"
  check 0 'D2 the mapper is still dispatched exactly once' 1 grep -c '^prompt ses_fake1 ' "$D/fo-esc/calls.log"
  check 0 'D2 the real oc.sh builds the session body' ses_x body esc
  # independent of the product: does this filesystem resolve a case variant of escape.py to the same link?
  esc_ci=$(python3 -c 'import os,sys; a,b=(os.path.join(sys.argv[1],n) for n in ("escape.py","ESCAPE.PY")); print("true" if os.path.lexists(b) and os.path.samestat(os.lstat(a),os.lstat(b)) else "false")' "$P")
  check 0 'D2 the boundary records the filesystem case mode' "$esc_ci" jq -r '.map_prepasses.only.boundary.case_insensitive' "$P/.council-run/state.json"
  {
    printf 'deny\tread escape.py\n'; printf 'deny\tread %s/escape.py\n' "$P"
    printf 'deny\tread outdir\n'; printf 'deny\tread outdir/secret.py\n'; printf 'deny\tread outdir/sub/s.py\n'; printf 'deny\tread %s/outdir/secret.py\n' "$P"
    printf 'deny\tread chain1\n'; printf 'deny\tread nested/inner/chain2\n'
    printf 'deny\tread nested/deep\n'; printf 'deny\tread nested/deep/s.py\n'; printf 'deny\tread alias/deep/s.py\n'; printf 'deny\tread alias/deep\n'
    printf 'deny\tread dangling.py\n'
    printf 'deny\tgrep secret\n'; printf 'deny\tglob **/*.py\n'
    printf 'allow\tread src/a.py\n'; printf 'allow\tread good.py\n'; printf 'allow\tread .git/config\n'; printf 'allow\tread .\n'
    printf 'allow\tread docs/guide.md\n'                          # a non-escaping name of a different shape
    if [ "$esc_ci" = true ]; then
      # Case-insensitive filesystem (APFS): OpenCode's matcher is case-sensitive, so every case variant
      # of an escaping link reaches the outside file unless the denies are case-folded.
      printf 'deny\tread ESCAPE.PY\n'; printf 'deny\tread Escape.py\n'; printf 'deny\tread %s/ESCAPE.PY\n' "$P"
      printf 'deny\tread OUTDIR/secret.py\n'; printf 'deny\tread Outdir\n'; printf 'deny\tread outDir/sub/S.py\n'
      printf 'deny\tread ALIAS/deep/s.py\n'; printf 'deny\tread alias/DEEP/s.py\n'; printf 'deny\tread Nested/Deep/s.py\n'
      printf 'deny\tread CHAIN1\n'; printf 'deny\tread Dangling.PY\n'
      printf 'deny\texternal_directory %s/*\tread %s/escape.py\n' "$(printf %s "$P" | tr a-z A-Z)" "$(printf %s "$P" | tr a-z A-Z)"
      # the documented over-match: a same-shaped in-project name (6 letters, like "outdir") is blocked too
      printf 'deny\tread nested/other.txt\n'
    else
      printf 'allow\tread nested\n'; printf 'allow\tread nested/other.txt\n'
    fi
    common_denies
  } >"$D/esc.tsv"
  matrix esc "$D/esc.tsv"
  cov="$P/.council-run/map/$mk/coverage.json"
  check 0 'D2 the scan records every escaping link and the alias leading to one' 'alias chain1 dangling.py escape.py nested/deep nested/inner/chain2 outdir' sh -c 'jq -r "[.boundary.blocked_symlinks[].path]|join(\" \")" "$1"' _ "$cov"
  check 0 'D2 the scan never blocks an in-project link that stays inside' true jq -e '[.boundary.blocked_symlinks[].path]|index("good.py")==null' "$cov"
  check 0 'D2 search denial and its reason are recorded in state' true jq -e '.map_prepasses.only.boundary.search_denied==true and (.map_prepasses.only.boundary.reason|test("grep/glob denied for this mapper session: 7 in-project symlink"))' "$P/.council-run/state.json"
  check 0 'D2 the same reason is recorded in coverage' true jq -e --slurpfile st "$P/.council-run/state.json" '.boundary.reason==$st[0].map_prepasses.only.boundary.reason' "$cov"
  check 0 'D2 map review shows the grep/glob denial reason' 'grep/glob denied for this mapper session: 7 in-project symlink(s)' cat "$D/esc.out"
  if [ "$esc_ci" = true ]; then
    check 0 'D2 map review says the denies are case-folded on this case-insensitive filesystem' 'their read denies are case-folded and may also block same-shaped in-project names' cat "$D/esc.out"
  fi
  check 0 'D2 the mapper prompt says grep and glob are unavailable' 'grep and glob are unavailable in this session' cat "$P/.council-run/map/$mk/instruction.md"
  check 0 'D2 an escaping selector is still rejected at capture (second check)' true jq -e '[.capture_statuses[]|select(.path=="escape.py")]==[] and ([.resource_schema_failures[]|tostring|select(test("escape.py"))]|length)>0' "$cov"
  check 0 'D2 ordinary and inside-link selectors are captured' 'ok ok' sh -c 'printf "%s %s" "$(jq -r ".capture_statuses[]|select(.path==\"src/a.py\")|.status" "$1")" "$(jq -r ".capture_statuses[]|select(.path==\"good.py\")|.status" "$1")"' _ "$cov"
  check 0 'D2 the mapper prompt was sent with --ask' '' grep -Eq '^prompt ses_fake1 .*--ask' "$D/fo-esc/calls.log"
  check 0 'D2 the mapper wait was run with --ask' '' grep -Eq '^wait ses_fake1 .*--ask' "$D/fo-esc/calls.log"
  ask_blocks() { AUTO=true; parse_opts --ask; api() { case "$1" in GET) echo '{"data":[{"id":"per1","action":"read","resources":["x"]}]}';; POST) echo "$2" >>"$D/replies";; esac; }; handle_permissions ses_x; }
  check 3 'permission boundary: with --ask a pending request blocks (exit 3) instead of being approved' '' ask_blocks
  check 0 'permission boundary: with --ask no permission reply was sent' '' sh -c '! test -e "$1"' _ "$D/replies"
  printf '{"action":"keep"}\n' >"$D/keep.json"
  keep() { ( cd "$D" && FAKE_OC_DIR="$D/fo-$1" bash "$D/impl/scripts/council.sh" resume --run-dir "$2/.council-run" --map-decision "$D/keep.json" >"$D/$1-keep.out" 2>"$D/$1-keep.err" ); }
  check 0 'D2 the task continues to completion after keep' '' keep esc "$P"
  # ================= D2: an incomplete scan never dispatches the mapper =================
  P="$D/unscannable"; mkdir -p "$P/src" "$P/locked"; printf 'one\n' >"$P/src/a.py"; chmod 000 "$P/locked"
  printf '%s\n' '{"candidates":[{"path":"src/a.py"}],"unresolved":[],"stopped_reason":"done"}' >"$D/mapper-response"
  check 4 'D2 scan failure: start reaches map review' '' pstart unscannable "$P"
  chmod 755 "$P/locked"
  check 0 'D2 scan failure: pre-pass is unavailable with the precise reason' 'unavailable symlink boundary scan failed before dispatch' sh -c 'jq -r "\"\(.map_prepasses.only.status) \(.map_prepasses.only.failure)\"" "$1" | cut -c1-60' _ "$P/.council-run/state.json"
  check 0 'D2 scan failure: zero mapper sessions and zero prompts' 00 sh -c 'printf %s%s "$(grep -c "^new " "$1")" "$(grep -c "^prompt " "$1")"' _ "$D/fo-unscannable/calls.log"
  check 0 'D2 scan failure: the task continues to completion after keep' '' keep unscannable "$P"
  check 0 'D2 scan failure: run done' done jq -r .status "$P/.council-run/state.json"
  # ================= D2: links changed during the mapper run discard the map =================
  change_case() {  # label hook-shell -- extra env for the first process
    local label=$1 hook=$2; shift 2
    P="$D/$label"; rm -rf "$P"; mkdir -p "$P/src"; printf 'one\n' >"$P/src/a.py"; ln -s "$D/outside/secret.py" "$P/escape.py"
    printf '%s\n' '{"candidates":[{"path":"src/a.py"}],"unresolved":[],"stopped_reason":"done"}' >"$D/mapper-response"
    printf '#!/bin/sh\ngrep -q "Locate candidate source" "$1" || exit 0\n%s\n' "$hook" >"$D/hook-$label"; chmod +x "$D/hook-$label"
  }
  for variant in created retargeted; do
    case $variant in created) hook="ln -s '$D/outside2/secret.py' '$D/rescan-$variant/late.py'";; retargeted) hook="ln -sfn '$D/outside2/secret.py' '$D/rescan-$variant/escape.py'";; esac
    change_case "rescan-$variant" "$hook"
    F="$D/fo-rescan-$variant"; rm -rf "$F"; mkdir -p "$F"; cp "$D/hook-rescan-$variant" "$F/result-hook"
    cp "$D/mapper-response" "$F/mapper-response"
    jq -n --arg d "$P" '{dir:$d,max_rounds:2,timeout_s:30,handover_at:0.5,executor:null,map_code:true,map_prepass:{kind:"opencode",model:"p/m",effort:"medium"},members:[{id:"A",kind:"opencode",model:"p/m",effort:"medium",mode:"read"},{id:"B",kind:"opencode",model:"p/m",effort:"medium",mode:"read"}],tasks:[{id:"only",text:"investigate"}]}' >"$D/cfg-rescan-$variant.json"
    check 4 "D2 link $variant during the mapper run: start reaches map review" '' sh -c 'cd "$1" && FAKE_OC_DIR="$2" bash "$1/impl/scripts/council.sh" start --config "$3" --run-dir "$4/.council-run" >"$1/rescan-$5.out" 2>"$1/rescan-$5.err"' _ "$D" "$F" "$D/cfg-rescan-$variant.json" "$P" "$variant"
    check 0 "D2 link $variant during the mapper run: the map is discarded as unavailable" 'unavailable escaping symlink set changed during the mapper run; map discarded' jq -r '"\(.map_prepasses.only.status) \(.map_prepasses.only.failure)"' "$P/.council-run/state.json"
    check 0 "D2 link $variant during the mapper run: nothing was captured" true jq -e '.capture_statuses==[]' "$P/.council-run/map/$mk/coverage.json"
    check 0 "D2 link $variant during the mapper run: the boundary baseline survives finalization" true jq -e '.boundary.search_denied==true and ([.boundary.blocked_symlinks[].path]==["escape.py"])' "$P/.council-run/map/$mk/coverage.json"
    check 0 "D2 link $variant during the mapper run: map review shows the discard reason" 'map discarded' cat "$D/rescan-$variant.out"
  done
  # Recovered completed session: the orchestrator dies after dispatch; a link appears before resume.
  change_case rescan-recovered ":"
  check 137 'D2 recovered session: orchestrator killed after the dispatched checkpoint' '' pstart rescan-recovered "$P" FAIL_MODE=after 'FAIL_STATE_JQ=any(.map_prepasses[]?; .status=="dispatched")'
  ln -s "$D/outside2/secret.py" "$P/late.py"
  check 4 'D2 recovered session: resume recovers the completed mapper result and reaches review' '' sh -c 'cd "$1" && FAKE_OC_DIR="$2" bash "$1/impl/scripts/council.sh" resume --run-dir "$3/.council-run" >"$1/recovered.out" 2>"$1/recovered.err"' _ "$D" "$D/fo-rescan-recovered" "$P"
  check 0 'D2 recovered session: the persisted baseline detects the change and discards the map' 'unavailable escaping symlink set changed during the mapper run; map discarded' jq -r '"\(.map_prepasses.only.status) \(.map_prepasses.only.failure)"' "$P/.council-run/state.json"
  check 0 'D2 recovered session: nothing was captured and the mapper was prompted once' 'true 1' sh -c 'printf "%s %s" "$(jq -e ".capture_statuses==[]" "$1")" "$(grep -c "^prompt ses_fake1 " "$2")"' _ "$P/.council-run/map/$mk/coverage.json" "$D/fo-rescan-recovered/calls.log"
  check 0 'D2 recovered session: the task continues to completion after keep' '' keep rescan-recovered "$P"
)
for suite in api_tests permission_tests wait_tests result_tests cli_tests council_tests dedup_tests report_tests style_tests handover_tests ptools_tests codemap_version_tests codemap_prompt_tests codemap_pipeline_tests codemap_baseline_tests codemap_engine_tests map_prepass_lifecycle_tests legacy_differential_tests mapped_lifecycle_process_tests inherited_answers_process_tests mapper_response_process_tests prepass_fault_process_tests split_gate_process_tests mapped_snapshot_process_tests mapper_permission_boundary_tests; do "$suite" || exit 1; done
echo "PASS $(wc -l <"$scratch/passed" | tr -d ' ') checks; 0 failures (offline, no model calls)"
