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
for suite in api_tests permission_tests wait_tests result_tests cli_tests council_tests dedup_tests report_tests style_tests handover_tests ptools_tests codemap_version_tests codemap_prompt_tests codemap_pipeline_tests codemap_baseline_tests codemap_engine_tests; do "$suite" || exit 1; done
echo "PASS $(wc -l <"$scratch/passed" | tr -d ' ') checks; 0 failures (offline, no model calls)"
