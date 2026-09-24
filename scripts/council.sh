#!/usr/bin/env bash
# council.sh — a cross-CLI "council": N persistent sessions (OpenCode via oc.sh and/or Claude Code CLI
# via `claude -p --resume`) work through a list of tasks and must reach an explicit, unanimous
# consensus on each one. The orchestrator relays the members' posts to each other every round.
#
#   council.sh show   --config F                      validate the config and print the roster (no sessions created)
#   council.sh start  --config F --run-dir D          create the sessions and run all tasks (D must not exist)
#   council.sh status --run-dir D                     where the run is: task, round, member context/tokens, pending questions
#   council.sh report --run-dir D                     token report for the run: prompt bytes by section, de-duplication
#                                                     replay and the remaining verbatim duplication (scripts/ptools, offline)
#   council.sh resume --run-dir D [--answers F|--answer TEXT] [--confirm-mapper FILE] [--map-decision FILE]
#                     [--replace ID=kind:model:effort]         swap a member's model/session (e.g. its provider ran out of quota):
#                                                              C=claude:sonnet:xhigh or C=opencode:google/gemini-3.8-flash:high
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
#   "members": [ {"id":"A","kind":"opencode","model":"openai/gpt-6-astra","effort":"high","mode":"read","handover_at":0.35},
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
# handover_at is the council-wide default; a member may set its own handover_at to hand over earlier or later.
set -o pipefail

HERE=$(cd "$(dirname "$0")" && pwd); OC="$HERE/oc.sh"
[ -x "$OC" ] || { echo "council: oc.sh not found next to this script" >&2; exit 1; }
command -v jq >/dev/null || { echo "council: jq is required" >&2; exit 1; }
command -v python3 >/dev/null || { echo "council: python3 is required (scripts/ptools: token report and duplicate audit)" >&2; exit 1; }
PTOOLS="$HERE/ptools"
for t in prompt_report.py dedup_check.py; do
  [ -f "$PTOOLS/$t" ] || { echo "council: missing $PTOOLS/$t" >&2; exit 1; }
done
# council_codemap.py/codemap_report.py are NOT hard startup requirements: an old run directory
# predating the code map (no state.codemap_version) must keep working even if a partial/minimal
# install is missing them. Every call site below already degrades gracefully on a nonzero/failed
# python3 invocation (map delivery disabled for that attempt, or the report section marked
# unavailable) — exactly what a missing-file invocation naturally produces.
CM="$HERE/council_codemap.py"

die() { echo "council: $*" >&2; exit 1; }
log() { echo "council: $*" >&2; }
now() { date +%H:%M:%S; }
CMD=""; CONFIG=""; RUN=""; ANSWERS_FILE=""; ANSWER_TEXT=""; CONFIRM_MAPPER_FILE=""; MAP_DECISION_FILE=""; REPLACE=()
while [ $# -gt 0 ]; do
  case "$1" in
    --config|--run-dir|--answers|--answer|--replace|--confirm-mapper|--map-decision) [ $# -ge 2 ] || die "missing value for $1" ;;
  esac
  case "$1" in
    --replace) REPLACE+=("$2"); shift 2 ;;
    --config)  CONFIG=$2; shift 2 ;;
    --run-dir) RUN=$2; shift 2 ;;
    --answers) ANSWERS_FILE=$2; shift 2 ;;
    --answer)  ANSWER_TEXT=$2; shift 2 ;;
    --confirm-mapper) CONFIRM_MAPPER_FILE=$2; shift 2 ;;
    --map-decision) MAP_DECISION_FILE=$2; shift 2 ;;
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
N=0; DIR=""; MAXR=0; TIMEOUT=600; HANDOVER=0.5; MAXT=30; EXEC=""; CODEMAP=0; MAP_PREPASS=0
load_state() {
  ST="$RUN/state.json"; [ -f "$ST" ] || die "no state in $RUN (not a council run dir)"
  N=$(st '.members|length'); DIR=$(st '.config.dir'); MAXR=$(st '.config.max_rounds'); TIMEOUT=$(st '.config.timeout_s')
  HANDOVER=$(st '.config.handover_at'); MAXT=$(st '.config.max_turns // 30'); EXEC=$(st '.config.executor // ""')
  MAP_PREPASS=0
  if [ "$(st '.map_prepass_version // 0')" = 1 ] && [ "$(st '.config.map_code // false')" = true ]; then MAP_PREPASS=1; fi
  codemap_check_version
}

# ---------------------------------------------------------------- codemap ----
# state.codemap_version=1 is stamped on every new run; no opt-in field. Absent -> every
# map-related state mutation/prompt addition/report addition/post interpretation is disabled.
# An unsupported present version disables new exposure (diagnostic only); a pending map-backed
# attempt whose freshness still needs checking is checkpointed rather than having its guard
# bypassed. See SKILL.md for the whole-attempt-replay trade-off this guard accepts.
codemap_check_version() {
  CODEMAP=0
  local v; v=$(st '.codemap_version // empty')
  [ -n "$v" ] || return 0
  if [ "$v" = "1" ]; then CODEMAP=1; return 0; fi
  log "codemap: unsupported state.codemap_version=$v — new map exposure disabled"
  local pend; pend=$(st '.codemap_pending // empty')
  if [ -n "$pend" ]; then
    sts '.status="failed"'
    log "codemap: a pending attempt has map-backed evidence whose freshness still needs checking under an unsupported version — checkpointed (its votes are preserved); run: council.sh resume --run-dir $RUN"
    exit 2
  fi
}
codemap_is_deliberation() { [ "$1" = prompt_round1 ] || [ "$1" = prompt_roundN ]; }
CODEMAP_STEP=0   # this attempt's map delivery: 0 disabled (absent version, non-deliberation step, or prepare failure) / 1 enabled
codemap_pending_attempt() { stj '.codemap_pending.attempt // empty' | tr -d '"'; }
codemap_archive_posts() {  # tid tag -> archives the COMPLETE posts (Markdown + JSON tail + attribution)
  # inside the attempt's own private area before a replay can purge or overwrite them.
  local tid=$1 tag=$2 aid dest f
  aid=$(codemap_pending_attempt); [ -n "$aid" ] || aid="unknown-$tid-$tag"
  ls "$RUN"/posts/"$tid-$tag-"* >/dev/null 2>&1 || return 0
  dest="$RUN/codemap/attempts/$aid/archived-posts/$(date +%s)-$$"
  mkdir -p "$dest" || codemap_checkpoint "cannot create the archive directory $dest — refusing to purge $tid/$tag's posts before they are safely archived"
  # The archive must succeed BEFORE anything is deleted: an archive that silently failed would
  # turn "preserved for inspection" into data loss at exactly the moment it matters.
  cp "$RUN"/posts/"$tid-$tag-"* "$dest"/ \
    || codemap_checkpoint "could not archive $tid/$tag's posts into $dest — refusing to purge them"
  for f in "$RUN"/posts/"$tid-$tag-"*.json; do
    [ -e "$f" ] || continue
    local mem; mem=$(basename "$f" .json); mem=${mem##*-}
    # Attribution comes from the ACCEPTED EVENT this post's exact bytes were staged as — the
    # durable record that already names the launch that produced them. Never from the member's
    # generation as it reads at archival time, and never from "the" launch record: a member may
    # have been launched more than once in one attempt, and guessing between those launches is
    # exactly how a replacement's reply would be relabelled as the session it replaced.
    local sha ameta assoc aeid lr gen=null lseq=null lid=null eid=null known=false
    sha=$(shasum -a 256 <"$f" | cut -d' ' -f1)
    # The association written at acceptance names the ONE event these exact bytes were accepted
    # as. Member plus digest is not an identity — two launches of the same member can produce
    # byte-identical tails — so the association is resolved and validated, never searched for.
    assoc="$RUN/codemap/attempts/$aid/post-assoc/$(basename "$f").assoc.json"
    ameta=""
    if [ -s "$assoc" ]; then
      aeid=$(jq -r --arg m "$mem" --arg t "$tid" --arg s "$tag" --arg a "$aid" --arg sha "$sha" \
             'select(.member==$m and .task==$t and .step==$s and .attempt==$a and .accepted_sha256==$sha)
              | .event_id // empty' "$assoc" 2>/dev/null)
      if [ -n "$aeid" ] && [ -s "$RUN/codemap/attempts/$aid/accepted/$aeid.meta.json" ]; then
        # the named event must itself agree about who and what it is, or it speaks for nothing
        ameta=$(jq -c --arg m "$mem" --arg sha "$sha" --arg e "$aeid" \
                'select(.member==$m and .accepted_sha256==$sha and .event_id==$e)' \
                "$RUN/codemap/attempts/$aid/accepted/$aeid.meta.json" 2>/dev/null)
      fi
    fi
    if [ -n "$ameta" ]; then
      gen=$(jq -c '.generation // null' <<<"$ameta"); lseq=$(jq -c '.launch_seq // null' <<<"$ameta")
      lid=$(jq -c '.launch_id // null' <<<"$ameta"); eid=$(jq -c '.event_id // null' <<<"$ameta")
      known=$(jq -c 'if .attributed then true else false end' <<<"$ameta")
    else
      # No exact association (never staged, or the association does not match these bytes): only
      # an unambiguous single launch can speak for the post. Anything else stays unattributed —
      # an ambiguous match is not provenance.
      local n; n=$(ls "$RUN/codemap/attempts/$aid/launches" 2>/dev/null | grep -c "^$mem\(-s[0-9]*\)\{0,1\}\.json$")
      if [ "$n" = 1 ]; then
        lr=$(ls "$RUN/codemap/attempts/$aid/launches"/$mem*.json 2>/dev/null | head -1)
        if [ -s "$lr" ]; then
          gen=$(jq -c '.generation // null' "$lr"); lseq=$(jq -c '.launch_seq // null' "$lr")
          lid=$(jq -c '.launch_id // null' "$lr"); known=true
        fi
      fi
    fi
    jq -n --arg m "$mem" --arg t "$tid" --arg s "$tag" --arg a "$aid" --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      --arg sha "$sha" --argjson g "$gen" --argjson q "$lseq" --argjson lid "$lid" --argjson eid "$eid" \
      --argjson known "$known" \
      '{member:$m,task:$t,step:$s,attempt:$a,archived_at:$ts,generation:$g,launch_seq:$q,
        launch_id:$lid,event_id:$eid,accepted_sha256:$sha,
        attribution_from_launch_record:$known,marker:"stale_or_ineligible"}' \
      >"$dest/$mem.attribution.json" \
      || codemap_checkpoint "could not write the archive attribution for $mem in $tid/$tag"
  done
  # a durable marker so the archive is never mistaken for reusable material
  jq -n --arg t "$tid" --arg s "$tag" --arg a "$aid" --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    '{task:$t,step:$s,attempt:$a,archived_at:$ts,status:"stale_or_ineligible",reusable:false}' >"$dest/MARKER.json" \
    || codemap_checkpoint "could not write the durable archive marker for $tid/$tag"
}
codemap_checkpoint() {  # message -> preserve everything, stop before any decision is made
  sts '.status="failed"'
  log "codemap: $1; run: council.sh resume --run-dir $RUN"
  render_transcript 2>/dev/null || true
  exit 2
}
codemap_prepare() {  # tid tag raw(0/1) -> sets CODEMAP_STEP; writes the locator cache file on success
  local tid=$1 tag=$2 raw=$3 out rc pend
  CODEMAP_STEP=0
  pend=$(stj '.codemap_pending // empty')
  if [ -n "$pend" ] && [ "$pend" != null ] && [ "$(jq -r .task <<<"$pend")" = "$tid" ] && [ "$(jq -r .step <<<"$pend")" = "$tag" ]; then
    # An attempt for this exact task/step is already open (resume of a partial step) — reuse it,
    # never re-prepare. A missing locator cache here means an already-open evidentiary commitment
    # (a real attempt, with its own guard list) was lost, not that the map is simply absent —
    # checkpoint rather than silently downgrading this attempt to "map delivery disabled".
    if [ -s "$RUN/codemap/.locator-$tid-$tag.md" ]; then CODEMAP_STEP=1; return 0; fi
    codemap_checkpoint "the locator cache for the already-open attempt $tid/$tag is missing — checkpointed (its freshness obligation is unresolved, not silently bypassed)"
  fi
  local extra=(); [ "$raw" = 1 ] && extra=(--raw)
  out=$(python3 "$CM" validate --run-dir "$RUN" --dir "$DIR" --task "$tid" --step "$tag" --mode begin "${extra[@]}" 2>&1); rc=$?
  if [ $rc -ne 0 ]; then log "codemap: view preparation failed for $tid/$tag — running this attempt with map delivery disabled: $out"; return 0; fi
  # The pending record carries the attempt, view and guard identity plus the exact source versions
  # this attempt exposed, so the obligation survives even if a derived map file is later lost.
  sts --arg t "$tid" --arg s "$tag" --argjson b "$out" \
    '.codemap_pending={task:$t,step:$s,attempt:$b.attempt,view_snapshot_id:$b.snapshot_id,
                       guard_snapshot_id:$b.guard_snapshot_id,exposed_sources:$b.exposed_sources}'
  if python3 "$CM" locator --run-dir "$RUN" --task "$tid" --step "$tag" >"$RUN/codemap/.locator-$tid-$tag.md" 2>"$RUN/codemap/.locator-$tid-$tag.err"; then
    CODEMAP_STEP=1
  else
    # Nothing was ever exposed, so there is no freshness obligation to owe. Clear the pending
    # record completely: leaving it set would block this step's own later partial-step resume (and
    # could leak a phantom obligation into another step) for an attempt that delivered no map
    # bytes at all.
    sts '.codemap_pending=null'
    log "codemap: locator failed for $tid/$tag before any map evidence was exposed — this attempt runs entirely map-disabled, with no freshness obligation: $(cat "$RUN/codemap/.locator-$tid-$tag.err" 2>/dev/null)"
  fi
}
SCAN_SHAPE='type=="object" and (.links|type=="array") and (.case_insensitive|type=="boolean")'   # scan-escapes output
map_key() { printf '%s' "$1" | shasum -a 256 | cut -c1-16; }
map_prepass_enabled() { [ "$MAP_PREPASS" = 1 ]; }
map_finalize_failure() {  # terminal optimization failure still gets complete durable accounting artifacts
  local tid=$1 dir=$2 reason=$3 outcome=${4:-unavailable} sid usage model effort
  mkdir -p "$dir" || { log "cannot create mapper failure archive $dir"; return 1; }
  sid=$(jq -r --arg t "$tid" '.map_prepasses[$t].session // empty' "$ST")
  model=$(st '.config.map_prepass.model // "google/gemini-3.8-flash"'); effort=$(st '.config.map_prepass.effort // "medium"')
  usage='{}'
  if [ -n "$sid" ]; then
    [ -s "$dir/session.json" ] || "$OC" api GET "/api/session/$sid" >"$dir/session.json" 2>"$dir/session.err" || true
    [ -s "$dir/tool-records.json" ] || "$OC" messages "$sid" --raw >"$dir/tool-records.json" 2>"$dir/tools.err" || true
    usage=$(jq -c '.data | {input:(.tokens.input // null),cache_read:(.tokens.cache.read // null),cache_write:(.tokens.cache.write // null),output:(.tokens.output // null),reasoning:(.tokens.reasoning // null),cost:(.cost // null)}' "$dir/session.json" 2>/dev/null)
    [ -n "$usage" ] || usage='{}'
  fi
  [ -f "$dir/response.txt" ] || : >"$dir/response.txt"
  [ -f "$dir/tool-records.json" ] || echo '[]' >"$dir/tool-records.json"
  [ -f "$dir/candidates.json" ] || echo '[]' >"$dir/candidates.json"
  [ -f "$dir/capture-results.json" ] || echo '{"results":[]}' >"$dir/capture-results.json"
  local input_digest selector_digest failure_captures failure_index
  failure_index="$RUN/codemap/index.json"
  if [ ! -s "$failure_index" ]; then printf '{"entries":{}}\n' >"$dir/failure-empty-index.json"; failure_index="$dir/failure-empty-index.json"; fi
  failure_captures=$(jq -cn --slurpfile cap "$dir/capture-results.json" --slurpfile idx "$failure_index" '
    [$cap[0].results[]? | . as $r | (if (($r.entry_id|type)=="string" and ($idx[0].entries|type)=="object") then ($idx[0].entries[$r.entry_id] // {}) else {} end) as $e |
      {path:$r.path,status:$r.status,reason:$r.reason,entry_id:$r.entry_id,resolved_path:$e.resolved_path,
       source_sha256:$e.source_sha256,byte_range:$e.byte_range,lines:$e.lines}]' 2>"$dir/failure-coverage.err") || {
         failure_captures='[]'; reason="$reason; captured-version assembly failed: $(cat "$dir/failure-coverage.err")"
      }
  # A later failure does not erase a usable captured subset: that outcome is partial, not unavailable.
  if [ "$outcome" = unavailable ] && jq -e 'any(.[]; .status=="ok")' <<<"$failure_captures" >/dev/null 2>&1; then outcome=partial; fi
  input_digest=$(jq -r --arg t "$tid" '.map_prepasses[$t].input_fingerprint // "unknown"' "$ST")
  selector_digest=$(shasum -a 256 <"$dir/candidates.json" | cut -d' ' -f1)
  local boundary; boundary=$(jq -c --arg t "$tid" '.map_prepasses[$t].boundary // null' "$ST" 2>/dev/null) || boundary=null
  [ -n "$boundary" ] || boundary=null
  jq -n --arg p "$tid" --arg d "$input_digest" --arg sd "$selector_digest" --arg r "$reason" --arg m "$model" --arg e "$effort" \
    --argjson c "$failure_captures" --argjson b "$boundary" \
    '{schema_version:1,parent_id:$p,map_seed_id:$d,mapping_input_digest:$d,selector_artifact_digest:$sd,
      captured_versions_ranges:$c,capture_statuses:$c,
      exclusions:[],boundary:$b,resource_schema_failures:[$r],
      mapper_unresolved:{attribution:{kind:"opencode",model:$m,effort:$e},items:[]},
      telemetry_complete:false,tool_records_complete:false,coverage:"unknown"}' >"$dir/coverage.json.tmp" || return 1
  mv "$dir/coverage.json.tmp" "$dir/coverage.json" || return 1
  # Preserve any validated mapper unresolved targets when failure happens after validation.
  if [ -s "$dir/validation.json" ]; then
    local unresolved; unresolved=$(jq -c '.result.unresolved // []' "$dir/validation.json" 2>/dev/null) || unresolved='[]'
    [ -n "$unresolved" ] || unresolved='[]'
    jq --argjson u "$unresolved" '.mapper_unresolved.items=$u' "$dir/coverage.json" >"$dir/coverage.json.tmp" && mv "$dir/coverage.json.tmp" "$dir/coverage.json" || return 1
  fi
  sts --arg t "$tid" --arg r "$reason" --arg s "$outcome" --arg dir "$dir" --argjson u "$usage" \
    '.map_prepasses[$t].status=$s | .map_prepasses[$t].failure=$r | .map_prepasses[$t].usage=$u
     | .map_prepasses[$t].artifacts={directory:$dir,coverage:($dir+"/coverage.json"),response:($dir+"/response.txt"),selectors:($dir+"/candidates.json"),session:($dir+"/session.json"),tools:($dir+"/tool-records.json")}
     | .map_prepasses[$t].finished_at=(now|floor)'
}
map_prepass_run() {  # original task only; durable dispatch state prevents duplicate paid launches
  local task=$1 tid key dir entry status sid prompt raw parsed errors maxbytes model effort timeout digest cmdrc answers recovered=0 result_tmp
  tid=$(jq -r .id <<<"$task"); key=$(map_key "$tid"); dir="$RUN/map/$key"; entry=$(jq -c --arg t "$tid" '.map_prepasses[$t] // {}' "$ST")
  mkdir -p "$dir" || { log "cannot create mapper artifact directory $dir"; return 1; }
  status=$(jq -r '.status // "new"' <<<"$entry")
  case "$status" in complete|partial|unavailable) return 0;; dispatching|dispatched)
    sid=$(jq -r '.session // empty' <<<"$entry")
    if [ -z "$sid" ]; then map_finalize_failure "$tid" "$dir" "dispatch state has no session; not relaunched" || return 1; return 0; else
      "$OC" wait "$sid" --timeout 1 --ask >/dev/null 2>"$dir/resume-wait.err"; cmdrc=$?
      if [ "$cmdrc" -eq 0 ]; then
        result_tmp="$dir/response.txt.recovery-$$.tmp"
        if ! "$OC" result "$sid" >"$result_tmp" 2>"$dir/result-recovery.err"; then
          [ -s "$result_tmp" ] && mv "$result_tmp" "$dir/response-retrieval-partial.txt"
          map_finalize_failure "$tid" "$dir" "completed mapper result could not be retrieved" || return 1; return 0
        fi
        mv "$result_tmp" "$dir/response.txt" || { log "could not publish recovered mapper response"; return 1; }
        status=dispatched; recovered=1
      else
        [ "$cmdrc" -eq 2 ] && "$OC" interrupt "$sid" >/dev/null 2>&1 || true
        map_finalize_failure "$tid" "$dir" "incomplete or uncertain dispatch (wait exit $cmdrc); not relaunched" || return 1; return 0
      fi
    fi
  esac
  mkdir -p "$dir" || return 1
  model=$(st '.config.map_prepass.model'); effort=$(st '.config.map_prepass.effort'); timeout=$(st '.config.map_prepass.timeout_s // 120'); maxbytes=$(st '.config.map_prepass.max_output_bytes // 65536')
  if [ "$status" != dispatched ]; then
    answers=$(jq -c --arg t "$tid" '[.answers[]? | select(.task==$t)]' "$ST")
    local mapper_kind; mapper_kind=$(st '.config.map_prepass.kind // "opencode"')
    jq -n --arg id "$tid" --arg text "$(jq -r .text <<<"$task")" --arg root "$DIR" --arg model "$model" --arg effort "$effort" --argjson answers "$answers" --argjson original "$task" \
      --arg kind "$mapper_kind" --argjson timeout "$timeout" --argjson maxbytes "$maxbytes" \
      '{task_id:$id,task_text:$text,original_task:$original,answers:$answers,root:$root,mapper:{kind:$kind,model:$model,effort:$effort,timeout_s:$timeout,max_output_bytes:$maxbytes},exclusions:[]}' >"$dir/input.json"
    digest=$(shasum -a 256 <"$dir/input.json" | cut -d' ' -f1)
    sts --arg t "$tid" --arg d "$digest" --arg dir "$dir" --arg now "$(date +%s)" \
      '.map_prepasses[$t]={status:"preparing",input_fingerprint:$d,artifact_dir:$dir,started_at:($now|tonumber),usage:null,coverage:"unknown"}' || { log "could not persist mapper preparation checkpoint"; return 1; }
    # Project boundary before mapper access. OpenCode checks paths lexically (it never resolves a
    # symlink), so the orchestrator scans the whole project for in-project symlinks that resolve
    # outside it (and for directory aliases leading to one) and denies them by name. grep/glob are
    # authorized by their search PATTERN and then search the directory named by their path argument
    # (a symlink is followed there), so no rule can keep them off an escaping link: when any exists,
    # they are denied for this session only. The baseline is persisted before any session exists.
    local scan escaping boundary_reason
    if ! scan=$(python3 "$HERE/council_map_prepass.py" scan-escapes --root "$DIR" 2>"$dir/escape-scan.err") || ! jq -e "$SCAN_SHAPE" <<<"$scan" >/dev/null 2>&1; then
      map_finalize_failure "$tid" "$dir" "symlink boundary scan failed before dispatch: $(cat "$dir/escape-scan.err")" || return 1; return 0
    fi
    printf '%s\n' "$scan" >"$dir/escape-scan.json" || { map_finalize_failure "$tid" "$dir" "symlink boundary baseline could not be written" || return 1; return 0; }
    escaping=$(jq '.links|length' <<<"$scan")
    boundary_reason=""
    [ "$escaping" -gt 0 ] && boundary_reason="grep/glob denied for this mapper session: $escaping in-project symlink(s) resolve outside the project or lead to one: $(jq -r '[.links[].path]|join(", ")' <<<"$scan")"
    [ "$escaping" -gt 0 ] && [ "$(jq -r .case_insensitive <<<"$scan")" = true ] && boundary_reason="$boundary_reason; the filesystem ignores letter case, so their read denies are case-folded and may also block same-shaped in-project names"
    sts --arg t "$tid" --argjson scan "$scan" --arg r "$boundary_reason" \
      '.map_prepasses[$t].boundary={blocked_symlinks:$scan.links,case_insensitive:$scan.case_insensitive,search_denied:($scan.links|length>0),reason:(if $r=="" then null else $r end)}' || { log "could not persist mapper boundary baseline"; return 1; }
    local boundary_line="Boundary: only paths inside the project root are accessible; in-project symlinks that resolve outside it are blocked."
    [ "$escaping" -gt 0 ] && boundary_line="$boundary_line grep and glob are unavailable in this session because such symlinks exist; use read to list directories and read files."
    cat >"$dir/instruction.md" <<EOF
Locate candidate source, test, configuration, and documentation regions relevant to this task. Return locations and unresolved search targets only. Do not explain code behavior, diagnose, propose implementation, invent requirements, decide a split, or claim exhaustive coverage. Check named task locations and look for relevant callers, callees, tests, and configuration. Return candidates:[{path,lines?}], unresolved:[{target,reason}], and stopped_reason. Paths are project-relative; lines are inclusive and 1-based. Omit lines for a whole-file selector. Selectors are hypotheses, not verified relevance.

Project root: $DIR
$boundary_line
Task:
$(jq -r .text <<<"$task")
User answers:
$(jq -r --arg t "$tid" --arg p "$(jq -r '.map_parent // .id' <<<"$task")" '.answers[]? | select(.task==$t or .task==$p) | "Q: \(.question)\nA: \(.answer)"' "$ST")
EOF
    # OpenCode evaluates the LAST rule whose action and resource wildcard-match (unmatched = ask), and
    # names in-project files by their lexical project-relative path; outside paths are absolute and
    # also need external_directory. The whole project is readable and searchable, repository
    # metadata and the run directory included; the escaping links found above are denied after the
    # allows (relative and absolute forms, the link and everything beneath it), and grep/glob are
    # denied for the whole session when any exists. OpenCode's matcher is case-sensitive, so on a
    # filesystem that ignores case (e.g. APFS) the scan's case-folded pattern (`?` per ASCII letter,
    # `*` per non-ASCII character) is denied too; the absolute prefix stays literal, since a
    # case-variant absolute path is not lexically inside and already needs external_directory.
    local -a mapper_rules=(--ask --deny '*' --deny edit --deny bash --deny shell --deny task --deny subagent --deny external_directory
      --deny webfetch --deny websearch --deny skill
      --allow 'read:*' --allow 'list:*' --deny 'read:..' --deny 'read:../*')
    if [ "$escaping" -gt 0 ]; then
      mapper_rules+=(--deny grep --deny glob)
      local link
      while IFS= read -r -d '' link; do
        mapper_rules+=(--deny "read:$link" --deny "read:$link/*" --deny "read:$DIR/$link" --deny "read:$DIR/$link/*"
          --deny "list:$link" --deny "list:$link/*" --deny "list:$DIR/$link" --deny "list:$DIR/$link/*")
      done < <(jq -j '.links[] | (.path, (select(.pattern != .path) | .pattern)) + "\u0000"' <<<"$scan")
    else
      mapper_rules+=(--allow grep --allow glob)
    fi
    sid=$("$OC" new --dir "$DIR" --model "$model" --variant "$effort" --agent plan --title "Council map pre-pass" "${mapper_rules[@]}") || {
        map_finalize_failure "$tid" "$dir" "mapper session creation failed" || return 1; return 0; }
    if ! sts --arg t "$tid" --arg s "$sid" '.map_prepasses[$t].session=$s | .map_prepasses[$t].status="created"'; then
      "$OC" interrupt "$sid" >/dev/null 2>&1 || true
      log "could not persist mapper session identity; no prompt dispatched"
      return 1
    fi
    if ! sts --arg t "$tid" '.map_prepasses[$t].status="dispatching"'; then
      "$OC" interrupt "$sid" >/dev/null 2>&1 || true
      log "could not persist mapper dispatch checkpoint; no prompt dispatched"
      return 1
    fi
    if ! "$OC" prompt "$sid" --file "$dir/instruction.md" --no-wait --ask >/dev/null 2>"$dir/dispatch.err"; then
      "$OC" interrupt "$sid" >/dev/null 2>&1 || true
      map_finalize_failure "$tid" "$dir" "mapper prompt dispatch failed" || return 1; return 0
    fi
    sts --arg t "$tid" '.map_prepasses[$t].status="dispatched"' || { log "mapper prompt may be dispatched; durable state remains dispatching for recovery"; return 1; }
  fi
  sid=$(jq -r --arg t "$tid" '.map_prepasses[$t].session' "$ST")
  if [ "$recovered" -eq 0 ]; then
    "$OC" wait "$sid" --timeout "$timeout" --ask >/dev/null 2>"$dir/wait.err"; cmdrc=$?
    if [ "$cmdrc" -ne 0 ]; then
      [ "$cmdrc" -eq 2 ] && "$OC" interrupt "$sid" >/dev/null 2>&1 || true
      map_finalize_failure "$tid" "$dir" "timeout or blocked mapper permission (wait exit $cmdrc); no retry" || return 1; return 0
    fi
    result_tmp="$dir/response.txt.result-$$.tmp"
    if ! "$OC" result "$sid" >"$result_tmp" 2>"$dir/result.err"; then
      [ -s "$result_tmp" ] && mv "$result_tmp" "$dir/response-retrieval-partial.txt"
      map_finalize_failure "$tid" "$dir" "mapper result retrieval failed" || return 1; return 0
    fi
    mv "$result_tmp" "$dir/response.txt" || { log "could not publish mapper response"; return 1; }
  fi
  "$OC" api GET "/api/session/$sid" >"$dir/session.json" 2>"$dir/session.err" || true
  "$OC" messages "$sid" --raw >"$dir/tool-records.json" 2>"$dir/tools.err" || true
  local usage; usage=$(jq -c '.data | {input:(.tokens.input // null),cache_read:(.tokens.cache.read // null),cache_write:(.tokens.cache.write // null),output:(.tokens.output // null),reasoning:(.tokens.reasoning // null),cost:(.cost // null)}' "$dir/session.json" 2>/dev/null)
  [ -n "$usage" ] || usage='{}'
  raw="$dir/response.txt"
  # Links changed by another process during the run cannot be blocked by rules; they are detected:
  # the persisted pre-dispatch baseline must equal a fresh scan, or the map is discarded.
  local baseline rescan
  baseline=$(jq -c --arg t "$tid" '.map_prepasses[$t].boundary | select(.blocked_symlinks != null) | {links:.blocked_symlinks,case_insensitive}' "$ST" 2>/dev/null)
  if [ -z "$baseline" ]; then
    map_finalize_failure "$tid" "$dir" "symlink boundary baseline missing; map discarded" || return 1; return 0
  fi
  if ! rescan=$(python3 "$HERE/council_map_prepass.py" scan-escapes --root "$DIR" 2>"$dir/escape-rescan.err") || ! jq -e "$SCAN_SHAPE" <<<"$rescan" >/dev/null 2>&1; then
    map_finalize_failure "$tid" "$dir" "symlink boundary rescan failed after the mapper run; map discarded: $(cat "$dir/escape-rescan.err")" || return 1; return 0
  fi
  printf '%s\n' "$rescan" >"$dir/escape-rescan.json"
  if ! jq -e --argjson a "$baseline" --argjson b "$rescan" -n '$a==$b' >/dev/null; then  # links, targets, patterns and case mode
    map_finalize_failure "$tid" "$dir" "escaping symlink set changed during the mapper run; map discarded" || return 1; return 0
  fi
  if ! python3 "$HERE/council_map_prepass.py" "$raw" --max-output-bytes "$maxbytes" --root "$DIR" --run-dir "$RUN" --selectors "$dir/candidates.json" >"$dir/validation.json" 2>"$dir/validation.err"; then
    [ -s "$dir/validation.json" ] || { map_finalize_failure "$tid" "$dir" "mapper validation helper failed" || return 1; return 0; }
  fi
  parsed=$(jq -c '.result' "$dir/validation.json" 2>/dev/null); errors=$(jq -c '.errors // []' "$dir/validation.json" 2>/dev/null); [ -n "$parsed" ] || { parsed='{"status":"unavailable","coverage":"unknown","candidates":[]}'; errors='["validation failed"]'; }
  jq -n --argjson r "$parsed" '$r.candidates' >"$dir/captures.json"
  if [ "$(jq -r '.candidates|length' <<<"$parsed")" -gt 0 ]; then
    if ! python3 "$CM" ingest --run-dir "$RUN" --dir "$DIR" --capture-json "$dir/captures.json" --capture-lines >"$dir/capture-results.json" 2>"$dir/capture.err"; then
      map_finalize_failure "$tid" "$dir" "codemap ingestion failed" || return 1; return 0
    fi
  else echo '{"results":[]}' >"$dir/capture-results.json"; fi
  local index_for_coverage="$RUN/codemap/index.json"
  if [ ! -s "$index_for_coverage" ]; then printf '{"entries":{}}\n' >"$dir/empty-index.json"; index_for_coverage="$dir/empty-index.json"; fi
  local captured; captured=$(jq -cn --slurpfile cap "$dir/capture-results.json" --slurpfile idx "$index_for_coverage" '
    [$cap[0].results[]? | . as $r | (if (($r.entry_id|type)=="string" and ($idx[0].entries|type)=="object") then ($idx[0].entries[$r.entry_id] // {}) else {} end) as $e |
      {path:$r.path,status:$r.status,reason:$r.reason,entry_id:$r.entry_id,
       resolved_path:$e.resolved_path,source_sha256:$e.source_sha256,byte_range:$e.byte_range,lines:$e.lines}]' 2>"$dir/coverage-assembly.err") || {
      map_finalize_failure "$tid" "$dir" "coverage assembly failed: $(cat "$dir/coverage-assembly.err")" || return 1; return 0;
    }
  jq -n --arg p "$tid" --arg d "$(jq -r --arg t "$tid" '.map_prepasses[$t].input_fingerprint' "$ST")" \
    --arg sd "$(shasum -a 256 <"$dir/candidates.json" | cut -d' ' -f1)" \
    --argjson c "$captured" --argjson e "${errors:-[]}" --argjson r "$parsed" \
    --arg m "$model" --arg effort "$effort" --argjson b "$(jq -c --arg t "$tid" '.map_prepasses[$t].boundary // null' "$ST")" \
    '{schema_version:1,parent_id:$p,map_seed_id:$d,mapping_input_digest:$d,selector_artifact_digest:$sd,
      captured_versions_ranges:$c,capture_statuses:$c,
      exclusions:[],boundary:$b,resource_schema_failures:$e,
      mapper_unresolved:{attribution:{kind:"opencode",model:$m,effort:$effort},items:($r.unresolved // [])},
      tool_records_complete:false,telemetry_complete:false,coverage:"unknown"}' >"$dir/coverage.json.tmp" || { map_finalize_failure "$tid" "$dir" "coverage artifact write failed" || return 1; return 0; }
  mv "$dir/coverage.json.tmp" "$dir/coverage.json" || { map_finalize_failure "$tid" "$dir" "coverage artifact publication failed" || return 1; return 0; }
  status=$(jq -r '.status' <<<"$parsed")
  local capture_total capture_ok
  capture_total=$(jq -r '.results|length' "$dir/capture-results.json" 2>/dev/null) || capture_total=0
  capture_ok=$(jq -r '[.results[]? | select(.status=="ok")]|length' "$dir/capture-results.json" 2>/dev/null) || capture_ok=0
  if [ "$capture_total" -gt 0 ] && [ "$capture_ok" -eq 0 ]; then status=unavailable
  elif [ "$capture_ok" -gt 0 ] && [ "$capture_ok" -lt "$capture_total" ]; then status=partial
  elif jq -e 'any(.results[]?; .status!="ok")' "$dir/capture-results.json" >/dev/null 2>&1 && [ "$status" = ok ]; then status=partial; fi
  [ "$status" = ok ] && status=complete
  [ "$status" = "partial" ] && parsed=$(jq -c '.status="partial"' <<<"$parsed")
  sts --arg t "$tid" --arg s "$status" --arg dir "$dir" --argjson result "$parsed" --argjson u "$usage" \
    '.map_prepasses[$t].status=$s | .map_prepasses[$t].artifacts={directory:$dir,coverage:($dir+"/coverage.json"),response:($dir+"/response.txt"),selectors:($dir+"/candidates.json"),session:($dir+"/session.json"),tools:($dir+"/tool-records.json")} | .map_prepasses[$t].mapper_result=$result | .map_prepasses[$t].usage=$u | .map_prepasses[$t].finished_at=(now|floor)'
}
map_prepass_review() {  # pause once per original task until explicit user decision
  local task=$1 tid key dir reviewed locator seed_id; tid=$(jq -r .id <<<"$task"); key=$(map_key "$tid"); dir="$RUN/map/$key"
  reviewed=$(jq -r --arg t "$tid" '.map_prepasses[$t].reviewed // false' "$ST")
  [ "$reviewed" = true ] && return 0
  mkdir -p "$dir" || return 1
  locator="$dir/seed-locator.md"
  if ! python3 "$CM" seed-locator --run-dir "$RUN" --coverage "$dir/coverage.json" >"$locator" 2>"$dir/seed-locator.err"; then
    echo "Complete map locator unavailable: $(cat "$dir/seed-locator.err")" >"$locator"
  fi
  seed_id=$(sed -n 's/^snapshot_id: //p' "$locator" | head -1)
  local contract_seed_id; contract_seed_id=$(jq -r '.map_seed_id // "unknown"' "$dir/coverage.json" 2>/dev/null)
  sts --arg t "$tid" --arg loc "$locator" --arg sid "$seed_id" \
    '.map_prepasses[$t].seed_locator=$loc | .map_prepasses[$t].seed_snapshot_id=$sid | .map_prepasses[$t].scope_applicability="unknown"'
  echo "Map review for task $tid: status=$(jq -r --arg t "$tid" '.map_prepasses[$t].status' "$ST"), coverage=unknown"
  echo "Split contract map_seed_id (copy this exact value into contract JSON): $contract_seed_id"
  echo "Lookup snapshot_id (locator identity; not the contract map_seed_id): ${seed_id:-unavailable}"
  [ -s "$dir/candidates.json" ] && jq -r '.[] | "  \(.path)\(if .lines then ":\(.lines[0])-\(.lines[1])" else " (whole file)" end)"' "$dir/candidates.json"
  [ -s "$dir/coverage.json" ] && { echo "Capture statuses:"; jq -r '.capture_statuses[]? | "  \(.path // "?"): \(.status // "unknown")\(.reason // "" | if .=="" then "" else " — "+. end)"' "$dir/coverage.json"; echo "Exclusions:"; jq -r '.exclusions[]? | "  "+.' "$dir/coverage.json"; echo "Project boundary:"; jq -r 'if .boundary==null then "  unknown (no boundary scan recorded)" elif .boundary.search_denied then "  "+.boundary.reason else "  no in-project symlink resolves outside the project; grep/glob allowed" end' "$dir/coverage.json"; }
  [ -s "$dir/coverage.json" ] && { echo "Mapper schema/resource failures:"; jq -r '.resource_schema_failures[]? | "  "+.' "$dir/coverage.json"; echo "Unresolved targets (mapper-attributed):"; jq -r '.mapper_unresolved.items[]? | "  \(.target): \(.reason)"' "$dir/coverage.json"; }
  echo "Complete-map locator (entries are enumerable and retrievable with its lookup command):"
  cat "$locator"
  echo "Map artifacts: $dir (see coverage.json; selectors are hypotheses, not verified relevance)."
  sts --arg t "$tid" '.task_id=$t | .phase="map_review" | .status="questions" | .map_prepasses[$t].review_started_at=(now|floor) | .pending_questions=[{id:"map-decision",task:$t,member:"orchestrator",question:"Review the complete map and provide resume --map-decision FILE with action keep or split and a standalone contract."}]'
  jq '.pending_questions' "$ST" >"$RUN/questions.json"; render_transcript
  echo "council: map review pending; run council.sh resume --run-dir $RUN --map-decision FILE" >&2
  exit 4
}
validate_child_launch() {  # contract prerequisite gate before every model launch, including handover
  [ "$MAP_PREPASS" = 1 ] || return 0
  local t child parent key contract writes
  t=$(stj ".config.tasks[$(st .task_idx)]"); child=$(jq -r '.id' <<<"$t"); parent=$(jq -r '.map_parent // empty' <<<"$t")
  local authorized; authorized=$(stj '.map_prepass_authorized // null')
  if [ "$authorized" = null ] || [ "$(jq -r '.map_prepass_pending // null' "$ST")" != null ] || [ "$(st .phase)" = mapper_confirm ] || [ "$(st .phase)" = map_review ]; then
    log "mapper authorization or map review is pending; refusing launch for $child"
    return 1
  fi
  if ! jq -e '.map_prepass_authorized as $a | ($a.kind==(.config.map_prepass.kind // "opencode") and $a.model==(.config.map_prepass.model // "google/gemini-3.8-flash") and $a.effort==(.config.map_prepass.effort // "medium"))' "$ST" >/dev/null 2>&1; then
    log "authorized mapper tuple differs from frozen run settings; refusing inference"
    return 1
  fi
  if [ -z "$parent" ]; then
    [ "$(jq -r --arg p "$child" '.map_prepasses[$p].reviewed // false' "$ST")" = true ] || {
      log "original task $child has no completed map review; refusing launch"; return 1;
    }
    return 0
  fi
  local approval identity_file expected_digest actual_digest seed
  approval=$(jq -c --arg p "$parent" '.map_prepasses[$p].approved_contract // null' "$ST")
  [ "$approval" != null ] || { checkpoint_split_invalid "$child" "approved contract metadata is missing"; return 1; }
  key=$(map_key "$parent"); contract=$(jq -r '.contract_file // empty' <<<"$approval")
  [ -n "$contract" ] || contract="$RUN/map/$key/approved-contract.json"
  if [ ! -r "$contract" ] || [ ! -s "$contract" ]; then
    checkpoint_split_invalid "$child" "approved split contract for parent $parent is missing or unreadable"
    return 1
  fi
  expected_digest=$(jq -r '.digest // empty' <<<"$approval"); actual_digest=$(shasum -a 256 <"$contract" | cut -d' ' -f1)
  [ -n "$expected_digest" ] && [ "$expected_digest" = "$actual_digest" ] || { checkpoint_split_invalid "$child" "approved contract digest mismatch"; return 1; }
  [ "$(jq -r '.contract_identity // empty' <<<"$t")" = "$expected_digest" ] || {
    checkpoint_split_invalid "$child" "child task identity does not match the approved contract"; return 1;
  }
  [ "$(jq -r '.parent_id // empty' <<<"$approval")" = "$parent" ] || { checkpoint_split_invalid "$child" "approved contract parent identity mismatch"; return 1; }
  seed=$(jq -r --arg p "$parent" '(.map_prepasses[$p].coverage|objects|.map_seed_id) // (.map_prepasses[$p].mapper_result|objects|.map_seed_id) // empty' "$ST")
  [ -n "$seed" ] || seed=$(jq -r '.map_seed_id // empty' "$RUN/map/$key/coverage.json")
  [ "$(jq -r '.seed_id // empty' <<<"$approval")" = "$seed" ] || { checkpoint_split_invalid "$child" "approved map seed identity mismatch"; return 1; }
  identity_file=$(jq -r '.identity_file // empty' <<<"$approval")
  [ -r "$identity_file" ] || { checkpoint_split_invalid "$child" "approved baseline identity file is missing or unreadable"; return 1; }
  local expected_identity_digest actual_identity_digest
  expected_identity_digest=$(jq -r '.identity_digest // empty' <<<"$approval")
  actual_identity_digest=$(shasum -a 256 <"$identity_file" | cut -d' ' -f1)
  [ -n "$expected_identity_digest" ] && [ "$expected_identity_digest" = "$actual_identity_digest" ] || { checkpoint_split_invalid "$child" "approved baseline identity digest mismatch"; return 1; }
  if ! jq -e --arg c "$child" '.subtasks|any(.[]; .id==$c)' "$contract" >/dev/null 2>&1; then
    checkpoint_split_invalid "$child" "child $child is absent from the approved contract for parent $parent"
    return 1
  fi
  local entered; entered=$(jq -r --arg p "$parent" --arg c "$child" --arg d "$expected_digest" \
    '(.map_prepasses[$p].execution_started[$c] // null) as $e | if (($e|type)=="object" and $e.contract_digest==$d) then "true" else "false" end' "$ST")
  writes=false; [ "$entered" = false ] && case "$(st .phase)" in plan|exec) writes=true;; esac
  local args=(--root "$DIR" --contract "$contract" --baseline-only --child "$child" --identity-file "$identity_file")
  [ "$writes" = true ] && args+=(--include-writes)
  [ "$entered" = false ] && { [ "$(st .phase)" = plan ] || [ "$(st .phase)" = exec ]; } && args+=(--include-creates)
  [ "$entered" = true ] && args+=(--allow-authorized-changes)
  if ! python3 "$HERE/council_splitcheck.py" "${args[@]}" >"$RUN/map/$key/child-$(map_key "$child")-gate.out" 2>"$RUN/map/$key/child-$(map_key "$child")-gate.err"; then
    checkpoint_split_invalid "$child" "$(cat "$RUN/map/$key/child-$(map_key "$child")-gate.err")"
    return 1
  fi
}
checkpoint_split_invalid() {
  local child=$1 error=$2
  sts --arg t "$child" --arg e "$error" \
    '.status="questions" | .phase="split_invalid" | .pending_questions=[{id:("split-invalid/"+$t),task:$t,member:"orchestrator",question:("Approved split contract/baseline is invalid: "+$e+". Resolve it with the user before continuing.")}]'
  jq '.pending_questions' "$ST" >"$RUN/questions.json"
  render_transcript
}
append_historical_map_locator() {  # prompt file/task id; informational snapshot only, no freshness guard
  [ "$MAP_PREPASS" = 1 ] || return 0
  local pf=$1 tid=$2 t parent loc
  t=$(jq -c --arg t "$tid" '[.config.tasks[]|select(.id==$t)][0] // {}' "$ST")
  parent=$(jq -r '.map_parent // .id // empty' <<<"$t"); [ -n "$parent" ] || return 0
  local histdir snapshot
  histdir="$RUN/codemap/historical-locators"; mkdir -p "$histdir"
  loc="$histdir/$(map_key "$parent")-latest.md"
  # Freeze the latest completed whole-index view now, so execution and later rounds can
  # cite post-deliberation captures without turning the seed snapshot into a live claim.
  python3 "$CM" seed-locator --run-dir "$RUN" --coverage "$RUN/map/$(map_key "$parent")/coverage.json" >"$loc.tmp" 2>"$loc.err" || {
    loc=$(jq -r --arg p "$parent" '.map_prepasses[$p].seed_locator // empty' "$ST")
    [ -s "$loc" ] || return 0
  }
  if [ -s "$loc.tmp" ]; then mv "$loc.tmp" "$loc"; fi
  snapshot=$(sed -n 's/^snapshot_id: //p' "$loc" | head -1)
  [ -n "$snapshot" ] && sts --arg p "$parent" --arg sid "$snapshot" --arg loc "$loc" \
    '.map_prepasses[$p].historical_snapshot_id=$sid | .map_prepasses[$p].historical_locator=$loc | .map_prepasses[$p].historical_snapshot_at=(now|floor)'
  [ -s "$loc" ] || return 0
  { echo; echo "=== HISTORICAL CODE MAP (as of the completed pre-pass snapshot; not a live-source claim) ==="; cat "$loc"; echo "Live inspection is required for current-source claims. No deliberation freshness guard is applied to executor edits."; echo "=== END HISTORICAL CODE MAP ==="; } >>"$pf"
}
apply_mapper_confirmation() {
  [ -n "$CONFIRM_MAPPER_FILE" ] && [ -f "$CONFIRM_MAPPER_FILE" ] || { log "mapper confirmation needs --confirm-mapper FILE"; exit 4; }
  [ "$(st .phase)" = mapper_confirm ] && [ "$(st .status)" = questions ] || { log "there is no pending mapper confirmation"; exit 4; }
  local pending got; pending=$(stj '.map_prepass_pending')
  jq -e 'type=="object" and (keys|sort)==["effort","kind","model"] and all(.[];type=="string")' "$CONFIRM_MAPPER_FILE" >/dev/null 2>&1 || { log "confirmation file must contain exactly string fields {kind,model,effort}"; exit 4; }
  got=$(jq -c 'select(type=="object" and (.kind|type)=="string" and (.model|type)=="string" and (.effort|type)=="string") | {kind,model,effort}' "$CONFIRM_MAPPER_FILE" 2>/dev/null) || { log "confirmation file must contain {kind,model,effort}"; exit 4; }
  [ -n "$got" ] || { log "confirmation file must contain {kind,model,effort}"; exit 4; }
  jq -e --argjson g "$got" '(.map_prepass_pending|{kind,model,effort})==$g' "$ST" >/dev/null || { log "mapper confirmation does not exactly match the pending proposal"; exit 4; }
  local kind model effort; kind=$(jq -r .kind <<<"$got"); model=$(jq -r .model <<<"$got"); effort=$(jq -r .effort <<<"$got")
  [ "$kind" = opencode ] || { log "mapper kind must be opencode"; exit 4; }
  local cfg; cfg=$(stj '.config | .map_prepass += {kind:"opencode"}')
  cfg=$(jq -c --arg m "$model" --arg e "$effort" '.map_prepass.model=$m | .map_prepass.effort=$e' <<<"$cfg")
  check_mapper_available "$cfg"
  sts --arg m "$model" --arg e "$effort" '.config.map_prepass.kind="opencode" | .config.map_prepass.model=$m | .config.map_prepass.effort=$e | .map_prepass_authorized={kind:"opencode",model:$m,effort:$e,source:"user-confirmed"} | .map_prepass_pending=null | .status="running" | .phase="plan" | .pending_questions=null | .last_votes=[] | .reuse_posts=false' || { log "could not persist mapper authorization; confirmation checkpoint is retained"; exit 4; }
  rm -f "$RUN/questions.json"
}
archive_superseded_contract() {  # parent old-child-ids old-contract-identity -> retire stale work before state publication
  local parent=$1 oldids=$2 identity=$3 base dest id file aid attempt_dir archive_leaf
  SUPERSEDED_ARCHIVE=""
  base="$RUN/map/$(map_key "$parent")/superseded"
  dest="$base/$identity"
  if [ -e "$dest" ]; then
    # Never overwrite a prior archive for a reused contract digest.
    dest=$(mktemp -d "$base/$identity.XXXXXX") || { log "cannot allocate non-overwriting superseded archive"; return 1; }
  fi
  mkdir -p "$dest/prompts" "$dest/posts" "$dest/raw" || { log "cannot create superseded-contract archive $dest"; return 1; }
  for id in $(jq -r '.[]?' <<<"$oldids"); do
    for file in "$RUN"/prompts/"$id"-*; do [ -f "$file" ] || continue; mv "$file" "$dest/prompts/" || { log "cannot retire superseded prompt $file"; return 1; }; done
    for file in "$RUN"/posts/"$id"-*; do [ -f "$file" ] || continue; mv "$file" "$dest/posts/" || { log "cannot retire superseded post $file"; return 1; }; done
    for file in "$RUN"/raw/"$id"-*; do [ -f "$file" ] || continue; mv "$file" "$dest/raw/" || { log "cannot retire superseded raw response $file"; return 1; }; done
  done
  aid=$(stj '.codemap_pending.attempt // empty' | tr -d '"')
  if [ -n "$aid" ]; then
    attempt_dir="$RUN/codemap/attempts/$aid"
    if [ -d "$attempt_dir" ]; then
      mkdir -p "$dest/codemap-attempt" || return 1
       cp -R -n "$attempt_dir" "$dest/codemap-attempt/" || { log "cannot archive superseded code-map attempt $aid"; return 1; }
    fi
  fi
  jq -n --arg p "$parent" --arg identity "$identity" --arg ids "$oldids" --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
     '{parent:$p,old_contract_identity:$identity,child_ids:($ids|fromjson),archived_at:$ts,reusable:false}' >"$dest/MARKER.json.tmp" &&
    mv "$dest/MARKER.json.tmp" "$dest/MARKER.json" || { log "cannot finalize superseded-contract archive $dest"; return 1; }
  SUPERSEDED_ARCHIVE=$dest
}
apply_map_decision() {
  [ -n "$MAP_DECISION_FILE" ] && [ -f "$MAP_DECISION_FILE" ] || { log "map review needs --map-decision FILE"; exit 4; }
  [ "$(st .phase)" = map_review -o "$(st .phase)" = split_invalid ] && [ "$(st .status)" = questions ] || { log "there is no pending map review or split-resolution checkpoint"; exit 4; }
  local decision action parent idx phase task original
  phase=$(st .phase); task=$(st .task_id)
  jq -e 'type=="object" and ((.action=="keep" and (keys|sort)==["action"]) or (.action=="split" and (keys|sort)==["action","contract_file"] and (.contract_file|type)=="string" and (.contract_file|length)>0))' "$MAP_DECISION_FILE" >/dev/null 2>&1 || { log "map decision must be exactly {action:keep} or {action:split,contract_file:string}"; exit 4; }
  decision=$(jq -c . "$MAP_DECISION_FILE" 2>/dev/null) || { log "map decision must be valid JSON"; exit 4; }
  action=$(jq -r '.action // empty' <<<"$decision")
  parent=$(jq -r --arg t "$task" '.config.tasks[]|select(.id==$t)|(.map_parent // .id)' "$ST")
  idx=$(jq -r --arg p "$parent" '[.config.tasks|to_entries[]|select(.value.id==$p or .value.map_parent==$p)|.key]|min' "$ST")
  [ "$idx" != null ] || { log "cannot locate parent task $parent for map decision"; exit 4; }
  local mapdir; mapdir="$RUN/map/$(map_key "$parent")"; mkdir -p "$mapdir" || { log "cannot create map-decision archive"; exit 4; }
  cp "$MAP_DECISION_FILE" "$mapdir/decision.json.tmp" && mv "$mapdir/decision.json.tmp" "$mapdir/decision.json" || { log "cannot archive exact map decision"; exit 4; }
  case "$action" in
     keep)
       if [ "$phase" = split_invalid ]; then
         original=$(jq -c '.original_task' "$mapdir/input.json")
         [ -n "$original" ] && [ "$original" != null ] || { log "archived original task is missing; cannot resolve split by keeping parent"; exit 4; }
         local keep_oldids keep_identity
         keep_oldids=$(jq -c --arg p "$parent" '[.config.tasks[]|select(.map_parent==$p)|.id]' "$ST") || { log "cannot identify superseded split children"; exit 4; }
         keep_identity=$(jq -r --arg p "$parent" '.map_prepasses[$p].approved_contract.digest // "unapproved"' "$ST")
         archive_superseded_contract "$parent" "$keep_oldids" "$keep_identity" || exit 4
          sts --arg p "$parent" --arg f "$MAP_DECISION_FILE" --arg identity "$keep_identity" --arg archive "${SUPERSEDED_ARCHIVE:-}" --argjson i "$idx" --argjson o "$original" \
           '(.config.tasks|map(select(.map_parent==$p)|.id)) as $oldids
             | ([.results[]? as $r | select(($oldids|index($r.task))!=null) | $r]) as $superseded
             | if ($superseded|length)>0 then .result_history=((.result_history // [])+[{reason:"split_invalid resolved by keeping parent",contract_identity:$identity,approved_contract:.map_prepasses[$p].approved_contract,baseline_identities:(.map_prepasses[$p].approved_contract.identity_file // null),archive:$archive,results:$superseded,archived_at:(now|floor)}]) else . end
            | .config.tasks=(.config.tasks[:$i]+[$o]+[.config.tasks[$i:][]|select(.id!=$p and .map_parent!=$p)]) | .task_idx=$i | .task_id=$p
            | .results=[.results[]? as $r|select(($oldids|index($r.task))==null)|$r]
             | .map_prepasses[$p].execution_started={}
             | .map_prepasses[$p].reviewed=true | .map_prepasses[$p].decision={action:"keep",file:$f} | .pending_questions=null | .codemap_pending=null | .status="running" | .phase="plan" | .round=1 | .candidate=null | .voted_candidate=null | .fixes=[] | .last_votes=[] | .reuse_posts=false | .members=(.members // [] | map(.inflight=null))' || { log "could not commit keep resolution; archive is preserved and checkpoint remains"; exit 4; }
      else
         sts --arg p "$parent" --arg f "$MAP_DECISION_FILE" '.map_prepasses[$p].reviewed=true | .map_prepasses[$p].decision={action:"keep",file:$f} | .pending_questions=null | .status="running" | .phase="plan" | .last_votes=[] | .reuse_posts=false' || { log "could not persist map keep decision; review checkpoint is retained"; exit 4; }
      fi
      ;;
    split)
      local contract; contract=$(jq -r '.contract_file // empty' <<<"$decision")
      [ -n "$contract" ] || { log "split decision requires contract_file"; exit 4; }
      case "$contract" in /*) ;; *) contract="$(cd "$(dirname "$MAP_DECISION_FILE")" && pwd)/$contract" ;; esac
      [ -f "$contract" ] || { log "contract file not found: $contract"; exit 4; }
        local key cdir seed oldids; key=$(map_key "$parent"); cdir="$RUN/map/$key"; seed=$(jq -r '.map_seed_id // .snapshot_id // .mapping_input_digest // "unknown"' "$cdir/coverage.json" 2>/dev/null)
       mkdir -p "$cdir" || { log "cannot create split archive"; exit 4; }
       # Copy the submitted bytes once to a private candidate. All validation, digesting,
       # child expansion, and eventual approval use this same immutable artifact.
        local candidate candidate_digest identities identity_digest collision_rc submission_key old_identity
       candidate=$(mktemp "$cdir/submitted-contract.XXXXXX") || { log "cannot allocate submitted-contract archive"; exit 4; }
       submission_key=${candidate##*/}
       cp "$contract" "$candidate" || { log "cannot archive submitted contract"; exit 4; }
       candidate_digest=$(shasum -a 256 <"$candidate" | cut -d' ' -f1)
       identities="$cdir/baseline-identities-$submission_key.json"
       oldids=$(jq -c --arg p "$parent" '[.config.tasks[]|select(.map_parent==$p)|.id]' "$ST") || { log "cannot identify prior split children"; exit 4; }
       python3 "$HERE/council_splitcheck.py" --root "$DIR" --contract "$candidate" --parent-id "$parent" --map-seed-id "$seed" --identity-output "$identities.tmp" >"$cdir/splitcheck-$candidate_digest.out" 2>"$cdir/splitcheck-$candidate_digest.err"
       if [ $? -ne 0 ]; then
         cat "$cdir/splitcheck-$candidate_digest.err" >&2
         [ "$phase" = split_invalid ] || sts '.status="questions" | .phase="map_review"'
         jq '.pending_questions' "$ST" >"$RUN/questions.json"
         exit 4
       fi
       if jq -e --arg p "$parent" --slurpfile st "$ST" '[ $st[0].config.tasks[]|select(.id!=$p and .map_parent!=$p)|.id ] as $existing | any(.subtasks[];.id as $id|($existing|index($id))!=null)' "$candidate" >/dev/null 2>&1; then
         log "split child id collides with an existing task id"; [ "$phase" = split_invalid ] || sts '.status="questions" | .phase="map_review"'; exit 4
       else collision_rc=$?; [ "$collision_rc" -eq 1 ] || { log "could not validate split task-id collisions"; exit 4; }
       fi
       if jq -e --argjson oldids "$oldids" --slurpfile st "$ST" '[.subtasks[].id] as $ids | [$st[0].results[]? as $r | select(($oldids|index($r.task))==null) | $r.task] as $done | any($ids[]; . as $id | ($done|index($id))!=null)' "$candidate" >/dev/null 2>&1; then
         log "split child id collides with an existing result"; [ "$phase" = split_invalid ] || sts '.status="questions" | .phase="map_review"'; exit 4
       else collision_rc=$?; [ "$collision_rc" -eq 1 ] || { log "could not validate split result-id collisions"; exit 4; }
       fi
       local build_rc
       jq -e 'any(.subtasks[];.execute==true)' "$candidate" >/dev/null 2>&1; build_rc=$?
       if [ "$build_rc" -eq 0 ] && [ -z "$EXEC" ]; then
         log "split contains execute:true children but no executor is configured"; [ "$phase" = split_invalid ] || sts '.status="questions" | .phase="map_review"'; exit 4
       elif [ "$build_rc" -ne 0 ] && [ "$build_rc" -ne 1 ]; then
         log "could not validate split execution requirements"; exit 4
       fi
       if ! jq -e . "$candidate" >/dev/null 2>&1; then log "could not parse archived split contract"; exit 4; fi
        local children; children=$(jq -c --arg p "$parent" --arg d "$candidate_digest" --slurpfile state "$ST" '[.subtasks[] | . + {map_parent:$p,map_contract_id:.id,contract_identity:$d,inherited_answers:([$state[0].answers[]? | select(.task==$p)])}]' "$candidate") || { log "cannot expand archived split subtasks"; exit 4; }
       [ "$(jq 'length' <<<"$children")" -gt 0 ] || { log "split contract has no subtasks"; exit 4; }
       [ -s "$identities.tmp" ] || { log "split checker did not produce baseline identities"; exit 4; }
        mv "$identities.tmp" "$identities" || { log "cannot finalize baseline identity archive"; exit 4; }
        identity_digest=$(shasum -a 256 <"$identities" | cut -d' ' -f1)
         old_identity=$(jq -r --arg p "$parent" '.map_prepasses[$p].approved_contract.digest // "unapproved"' "$ST")
         if [ "$(jq 'length' <<<"$oldids")" -gt 0 ]; then archive_superseded_contract "$parent" "$oldids" "$old_identity" || exit 4; fi
         sts --arg p "$parent" --argjson i "$idx" --argjson children "$children" --arg f "$candidate" --arg digest "$candidate_digest" --arg old_identity "$old_identity" --arg archive "${SUPERSEDED_ARCHIVE:-}" --arg seed "$seed" --arg identities "$identities" --arg identity_digest "$identity_digest" --argjson oldids "$oldids" \
          '([.results[]? as $r | select(($oldids|index($r.task))!=null) | $r]) as $superseded
           | if ($superseded|length)>0 then .result_history=((.result_history // [])+[{reason:"split contract replaced",contract_identity:$old_identity,approved_contract:.map_prepasses[$p].approved_contract,baseline_identities:(.map_prepasses[$p].approved_contract.identity_file // null),archive:$archive,results:$superseded,archived_at:(now|floor)}]) else . end
          | .config.tasks = (.config.tasks[:$i] + $children + [.config.tasks[$i:][]|select(.id!=$p and .map_parent!=$p)]) | .task_idx=$i | .task_id=$p
          | .results=[.results[]? as $r|select(($oldids|index($r.task))==null)|$r]
           | .map_prepasses[$p].execution_started={}
           | .map_prepasses[$p].approved_contract={digest:$digest,seed_id:$seed,parent_id:$p,contract_file:$f,identity_file:$identities,identity_digest:$identity_digest}
           | .map_prepasses[$p].reviewed=true | .map_prepasses[$p].decision={action:"split",contract:$f,children:$children}
           | .pending_questions=null | .codemap_pending=null | .status="running" | .phase="plan" | .round=1 | .candidate=null | .voted_candidate=null | .fixes=[] | .last_votes=[] | .reuse_posts=false | .members=(.members // [] | map(.inflight=null))' || { log "could not commit validated split contract; archive is preserved and checkpoint remains"; exit 4; }
      ;;
    *) die "map decision action must be keep or split" ;;
  esac
  if jq -e --arg p "$parent" '.map_prepasses[$p].review_started_at != null' "$ST" >/dev/null 2>&1; then
    sts --arg p "$parent" '.map_prepasses[$p].review_finished_at=(now|floor) | .map_prepasses[$p].review_seconds=(.map_prepasses[$p].review_finished_at-.map_prepasses[$p].review_started_at)' || { log "could not persist map review completion"; exit 4; }
  fi
  rm -f "$RUN/questions.json"
  render_transcript
}
codemap_append_locator() {  # prompt-file tid tag site -> append the locator AND record its span
  # The orchestrator writes these bytes itself, so it knows their exact offset and length without
  # ever searching the generated prompt for marker text. Author posts, task text, stored handover
  # notes and relayed replies can contain "=== CODE MAP ===" all they like: none of it is counted,
  # because none of it was written here.
  [ "$CODEMAP_STEP" = 1 ] || return 0
  local pf=$1 tid=$2 tag=$3 site=$4 loc="$RUN/codemap/.locator-$2-$3.md" off len
  [ -s "$loc" ] || return 0
  [ -e "$pf" ] || : >"$pf"
  off=$(wc -c <"$pf" | tr -d ' ')
  len=$(wc -c <"$loc" | tr -d ' ')
  cat "$loc" >>"$pf" || codemap_checkpoint "could not deliver the code map locator into $pf"
  mkdir -p "$RUN/codemap"
  jq -n --arg p "$(basename "$pf")" --arg s "$site" --argjson o "$off" --argjson l "$len" \
     --arg a "$(codemap_pending_attempt)" --arg t "$tid" --arg g "$tag" \
     '{kind:"span",prompt:$p,site:$s,offset:$o,length:$l,attempt:$a,task:$t,step:$g}' -c \
     >>"$RUN/codemap/deliveries.jsonl" \
     || codemap_checkpoint "could not record the locator delivery boundary for $pf"
}
codemap_record_launched() {  # prompt-file tid tag -> the prompt was actually DELIVERED, and these
  # were its exact bytes. A later replay that overwrites the prompt file cannot make this record
  # point at different bytes, and a prompt written but never launched is never counted as delivered.
  [ "$CODEMAP_STEP" = 1 ] || return 0
  local pf=$1
  [ -s "$pf" ] || return 0
  jq -n --arg p "$(basename "$pf")" --arg a "$(codemap_pending_attempt)" \
     --arg h "$(shasum -a 256 <"$pf" | cut -d' ' -f1)" --argjson n "$(wc -c <"$pf" | tr -d ' ')" \
     --arg t "$2" --arg g "$3" \
     '{kind:"launched",prompt:$p,attempt:$a,prompt_sha256:$h,prompt_size:$n,task:$t,step:$g}' -c \
     >>"$RUN/codemap/deliveries.jsonl" || true
}
codemap_record_launch() {  # idx tid tag launch_gen -> prelaunch, orchestration-owned attribution;
  # sets CODEMAP_LAUNCH_ID to the identity of THIS launch. Every genuinely new launch of a member
  # inside one attempt — a replacement, a handover, a re-dispatched retry — gets its own immutable
  # record, so a reply is always bound to the session that actually produced it. Earlier records
  # are never rewritten, and reusing an already accepted checkpoint post allocates no launch at all
  # (run_step skips this call entirely on the reuse path).
  CODEMAP_LAUNCH_ID=""
  [ "$CODEMAP_STEP" = 1 ] || return 0
  local i=$1 tid=$2 tag=$3 lgen=$4 aid d f m seq lid
  aid=$(codemap_pending_attempt); [ -n "$aid" ] || return 0
  m=$(mid $i)
  d="$RUN/codemap/attempts/$aid/launches"; mkdir -p "$d"
  seq=$(( $(ls "$d" 2>/dev/null | wc -l) + 1 ))
  # the member's first launch in the attempt keeps the member's own name as its launch id
  lid=$m; [ "$seq" -gt 1 ] && lid="$m-s$seq"
  f="$d/$lid.json"
  [ -e "$f" ] && { CODEMAP_LAUNCH_ID=$lid; return 0; }   # immutable: never rewritten in place
  # Checked BEFORE the member is launched: without this record staging refuses to authenticate the
  # reply at all, so launching anyway would produce an accepted post that can never be attributed.
  jq -n --arg m "$m" --argjson g "$lgen" --arg t "$tid" --arg s "$tag" --arg a "$aid" \
     --arg lid "$lid" --argjson ts "$(date +%s)" --argjson seq "$seq" \
     '{member:$m,generation:$g,task:$t,step:$s,attempt:$a,launched_at:$ts,launch_seq:$seq,launch_id:$lid}' >"$f" \
    || codemap_checkpoint "could not record pre-launch attribution for member $m in $tid/$tag — refusing to launch a reply that could never be attributed"
  [ -s "$f" ] || codemap_checkpoint "the pre-launch attribution record for member $m in $tid/$tag is empty — refusing to launch"
  CODEMAP_LAUNCH_ID=$lid
}
codemap_schema_text() {
  [ "$CODEMAP_STEP" = 1 ] || return 0
  cat <<'TXT'
If the code map locator appears above, you may OPTIONALLY add "code_reads" to your JSON tail: an array of {"path":"<repo-relative path>","lines":[first,last] (1-based inclusive; omit for a whole-file claim),"observed_sha256":"<sha256 of the exact bytes you believe you read>","symbol":"<optional>","conclusion":"<optional short claim>","inspection":"<optional note>"}. code_reads is optional; omitting it means unknown coverage. symbol and conclusion are author claims, not verification by the map; observed_sha256 describes the bytes you actually inspected — a mismatch with the map's current capture leaves the claim unbound.
TXT
}
codemap_stage_accepted() {  # idx tid tag postjson launch_gen launch_id -> the accepted reply is
  # bound to the orchestration-owned record of the launch that produced it (the caller's arrays),
  # never a live re-read of state, so a handover/replace that launched the member again after this
  # reply was launched cannot relabel either reply.
  [ "$CODEMAP_STEP" = 1 ] || return 0
  local i=$1 tid=$2 tag=$3 pj=$4 lgen=$5 lid=${6:-} out rc
  local -a sel=(); [ -n "$lid" ] && sel=(--launch-id "$lid")
  out=$(python3 "$CM" ingest --run-dir "$RUN" --dir "$DIR" --task "$tid" --step "$tag" --mode stage \
        --member "$(mid $i)" --generation "$lgen" "${sel[@]}" --post-json "$pj" 2>&1); rc=$?
  [ $rc -eq 0 ] || log "codemap: staging member $(mid $i)'s optional reports failed (post's vote is unaffected): $out"
}
codemap_finish_step() {  # tid tag -> 0 normal, 1 stale (caller must fail the step; posts already purged)
  [ "$CODEMAP_STEP" = 1 ] || return 0
  local tid=$1 tag=$2 out status rc
  out=$(python3 "$CM" validate --run-dir "$RUN" --dir "$DIR" --task "$tid" --step "$tag" --mode end 2>&1); rc=$?
  # A guard that cannot be evaluated is NOT a pass. Accepted posts, staging, the attempt identity
  # and the unresolved obligation are all preserved, and the run checkpoints before this step's
  # posts can be used for candidate selection or consensus.
  if [ $rc -ne 0 ]; then
    codemap_checkpoint "the end-of-step freshness check for $tid/$tag could not be completed, so its guard is unverified — checkpointed with every accepted post and staged report preserved: $out"
  fi
  status=$(jq -r .guard_status <<<"$out" 2>/dev/null)
  case "$status" in
    current)
      out=$(python3 "$CM" ingest --run-dir "$RUN" --dir "$DIR" --task "$tid" --step "$tag" --mode publish 2>&1); rc=$?
      case "$( [ $rc -eq 0 ] && jq -r '.status // "error"' <<<"$out" 2>/dev/null || echo error)" in
        published|already_published) sts '.codemap_pending=null' ;;
        *) # Everything is retryable and nothing is lost — but the run must NOT advance past this
           # barrier while a publication is outstanding, or the next preparation would overwrite
           # the pending record that is the only thing remembering the work.
           codemap_checkpoint "publication of $tid/$tag's staged reports did not complete — its votes are unaffected and every staged report is preserved, but the run holds at this barrier rather than advancing with an outstanding publication: $out" ;;
      esac
      return 0 ;;
    changed)
      log "codemap: a source exposed as current in $tid/$tag's frozen view changed during the round — checkpointing and replaying this round (its posts are not reused)"
      codemap_archive_posts "$tid" "$tag"
      rm -f "$RUN"/posts/"$tid-$tag-"*
      sts '.codemap_pending=null'
      return 1 ;;
    *)
      codemap_checkpoint "$tid/$tag's freshness guard could not be verified (pending_verification) — its votes are preserved and NOT classified as stale, but they are not used for a decision until the obligation is resolved"
      ;;
  esac
}
codemap_resume_check() {  # called before an ordinary resume reuses posts of the in-progress step
  [ "$CODEMAP" = 1 ] || return 0
  local pend; pend=$(stj '.codemap_pending // empty'); [ -n "$pend" ] && [ "$pend" != null ] || return 0
  local tid tag out status
  tid=$(jq -r .task <<<"$pend"); tag=$(jq -r .step <<<"$pend")
  out=$(python3 "$CM" validate --run-dir "$RUN" --dir "$DIR" --task "$tid" --step "$tag" --mode resume 2>&1) || {
    codemap_checkpoint "the resume freshness re-check for $tid/$tag could not be completed — its posts are preserved and NOT classified as stale, but they are not reused until the guard is verified: $out"; }
  status=$(jq -r .guard_status <<<"$out" 2>/dev/null)
  if [ "$status" = pending_verification ]; then
    codemap_checkpoint "$tid/$tag's guard is still unverifiable on resume — its posts are preserved and NOT reused until the obligation is resolved"
  fi
  if [ "$status" = changed ]; then
    log "codemap: a guarded source changed while checkpointed — purging $tid/$tag's posts so they are not reused on resume"
    codemap_archive_posts "$tid" "$tag"
    rm -f "$RUN"/posts/"$tid-$tag-"*
    sts '.codemap_pending=null'
  elif [ "$status" = current ]; then
    # Unchanged — and that is ALL this re-check establishes. The obligation is not discharged here
    # and nothing is published here, whatever the attempt's recorded barrier says: the orchestration
    # decision for this step has not been made yet (these posts are about to be reused by run_step),
    # so the only safe state is the one we already have. Publishing and clearing codemap_pending now
    # would let run_step open a brand-new attempt with a brand-new guard list while reusing these
    # posts — exactly the hole through which a source changed after this re-check reaches consensus
    # unnoticed. The attempt, its frozen view, its original exposed-source guard and its
    # accepted-event attribution all stay in place; run_step reuses that same attempt and
    # codemap_finish_step re-verifies that same original guard at the decision barrier, which is
    # also where an outstanding publication (a publish that failed before the checkpoint) is retried.
    log "codemap: $tid/$tag's guarded sources are unchanged on resume — its posts are reusable, but the same attempt, view and guard are kept until the end-of-step decision barrier re-verifies them"
  fi
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
    (.map_prepass | if type=="object" then . else {} end) as $mp |
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
    + (if (.handover_at|type)=="number" and ((.handover_at>0 and .handover_at<=1) or (.handover_at>=1000 and .handover_at==(.handover_at|floor))) then [] else ["handover_at must be a fraction in (0,1] of the model context window, or an absolute token count >= 1000 (e.g. 150000)"] end)
    + (if has("map_code") and (.map_code|type)!="boolean" then ["map_code must be a boolean"] else [] end)
    + (if has("map_prepass") and (.map_prepass|type)!="object" then ["map_prepass must be an object containing mapper settings"] else [] end)
    + (if ($mp|has("kind")) and (($mp.kind|type)!="string" or $mp.kind!="opencode") then ["map_prepass.kind must be opencode"] else [] end)
    + (if ($mp|has("model")) and (($mp.model|type)!="string" or ($mp.model|contains("/")|not)) then ["map_prepass.model must be provider/id"] else [] end)
    + (if ($mp|has("effort")) and ($mp.effort|type)!="string" then ["map_prepass.effort must be a string"] else [] end)
    + (if ($mp|has("timeout_s")) then (if ($mp.timeout_s|type)=="number" and $mp.timeout_s>0 and ($mp.timeout_s|floor)==$mp.timeout_s then [] else ["map_prepass.timeout_s must be a positive integer"] end) else [] end)
    + (if ($mp|has("max_output_bytes")) then (if ($mp.max_output_bytes|type)=="number" and $mp.max_output_bytes>0 and ($mp.max_output_bytes|floor)==$mp.max_output_bytes then [] else ["map_prepass.max_output_bytes must be a positive integer"] end) else [] end)
    + [ .tasks[]? | select((type=="string" and length>0) or (type=="object" and (.text|type)=="string") | not) | "each task must be a string or {id,text,execute}" ]
    | .[]' "$1" | sed 's/^/  /')
  [ -z "$err" ] || { echo "council: invalid config $1:" >&2; echo "$err" >&2; exit 1; }
  local d; d=$(jq -r .dir "$1"); [ -d "$d" ] || die "dir does not exist: $d"
  # claude effort values; opencode efforts are checked against the model's variants below
  err=$(jq -r '.members[] | select(.kind=="claude") | select(.effort|IN("low","medium","high","xhigh","max")|not) | "  member \(.id): claude effort must be low|medium|high|xhigh|max"' "$1")
  [ -z "$err" ] || { echo "council: invalid config:" >&2; echo "$err" >&2; exit 1; }
  # optional per-member handover threshold (overrides the council-wide handover_at)
  err=$(jq -r '.members[] | select(has("handover_at")) | select(((.handover_at|type)=="number" and ((.handover_at>0 and .handover_at<=1) or (.handover_at>=1000 and .handover_at==(.handover_at|floor))))|not) | "  member \(.id): handover_at must be a fraction in (0,1] or an absolute token count >= 1000"' "$1")
  [ -z "$err" ] || { echo "council: invalid config:" >&2; echo "$err" >&2; exit 1; }
  # optional prose-compression style (see style_rules): normal (default) | lite | caveman | ultra
  err=$(jq -r '(if (.style // "normal")|IN("normal","lite","caveman","ultra")|not then "  style must be normal|lite|caveman|ultra" else empty end),
               (.members[] | select((.style // "normal")|IN("normal","lite","caveman","ultra")|not) | "  member \(.id): style must be normal|lite|caveman|ultra")' "$1")
  [ -z "$err" ] || { echo "council: invalid config:" >&2; echo "$err" >&2; exit 1; }
  # normalise: tasks -> {id,text,execute}; members -> +agent/permission_mode
  jq '.map_code = (.map_code // false)
      | .map_prepass = (.map_prepass // {})
      | .tasks |= [to_entries[] | (if (.value|type)=="string" then {id:("t"+((.key+1)|tostring)), text:.value, execute:false}
                                  else ({id:(.value.id // ("t"+((.key+1)|tostring))), text:.value.text, execute:(.value.execute==true)} +
                                        (.value | with_entries(select(.key|IN("acceptance","map_parent","map_contract_id","contract_identity","parent_id","map_seed_id","inherited_answers"))))) end)]
      | .max_turns = (.max_turns // 30)
      | .style = (.style // "normal")
      | .members |= [ .[] | {style: $s, handover_at: $h} + . + (if .kind=="opencode" then {agent:(if .mode=="edit" then "build" else "plan" end)}
                                 else {permission_mode:(if .mode=="edit" then "acceptEdits" else "plan" end)} end) ]' --arg s "$(jq -r '.style // "normal"' "$1")" --argjson h "$(jq -r '.handover_at' "$1")" "$1"
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
  printf '  %-4s %-9s %-34s %-8s %-6s %-12s %-8s %-9s %s\n' id kind model effort mode agent/perm style handover ctx-window
  jq -r --argjson d "$(jq -r '.handover_at' <<<"$cfg")" '.members[] | "\(.id)\t\(.kind)\t\(.model)\t\(.effort)\t\(.mode)\t\(.agent // .permission_mode)\t\(.style // "normal")\t\((.handover_at // $d) as $h | if $h > 1 then (($h/1000|floor)|tostring)+"k tok" else (($h*100|floor)|tostring)+"%" end)"' <<<"$cfg" | while IFS=$'\t' read -r id kind model effort mode ag sty ho; do
    local l; l=$(printf '%s\n' "$lim" | awk -F'\t' -v i="$id" '$1==i{print $2}')
    [ -n "$l" ] && l="$((l/1000))k" || l="(from first reply)"
    printf '  %-4s %-9s %-34s %-8s %-6s %-12s %-8s %-9s %s\n' "$id" "$kind" "$model" "$effort" "$mode" "$ag" "$sty" "$ho" "$l"
  done
  echo "  executor (only member allowed to edit files): $(jq -r '.executor // "none — read-only council"' <<<"$cfg")"
  echo "  dir: $(jq -r .dir <<<"$cfg")"
  if [ "$(jq -r '.map_code' <<<"$cfg")" = true ]; then
    echo "  map_code: true (one run-wide mapping choice)"
    echo "  mapper: $(jq -r '"opencode / " + (.map_prepass.model // "google/gemini-3.8-flash") + " / " + (.map_prepass.effort // "medium") + (if (.map_prepass.model and .map_prepass.effort) then " (configured)" else " (proposal; explicit confirmation required)" end)' <<<"$cfg")"
    echo "  mapper limits: $(jq -r '.map_prepass.timeout_s // 120' <<<"$cfg")s, $(jq -r '.map_prepass.max_output_bytes // 65536' <<<"$cfg") response bytes"
  fi
  echo "  max_rounds/task: $(jq -r .max_rounds <<<"$cfg") · timeout/call: $(jq -r .timeout_s <<<"$cfg")s · handover at $(jq -r '.handover_at as $h | if $h > 1 then ($h|tostring)+" tokens" else (($h*100|floor)|tostring)+"% of context" end' <<<"$cfg") (council default; per-member values in the table) · claude max_turns: $(jq -r .max_turns <<<"$cfg")"
  echo "  tasks:"; jq -r '.tasks[] | "    \(.id)\(if .execute then " [build]" else "" end): \(.text|gsub("\n";" ")|.[0:110])"' <<<"$cfg"
}

mapper_tuple() {  # config JSON -> proposed/configured tuple JSON
  jq -c '{kind:(.map_prepass.kind // "opencode"),model:(.map_prepass.model // "google/gemini-3.8-flash"),effort:(.map_prepass.effort // "medium"),timeout_s:(.map_prepass.timeout_s // 120),max_output_bytes:(.map_prepass.max_output_bytes // 65536)}' <<<"$1"
}
ensure_mapper_authorized() {  # recover a crash after enabled state creation, before any inference
  [ "$MAP_PREPASS" = 1 ] || return 0
  local auth cfg model effort tuple
  auth=$(stj '.map_prepass_authorized // null')
  cfg=$(stj '.config')
  model=$(jq -r '.config.map_prepass.model // empty' "$ST")
  effort=$(jq -r '.config.map_prepass.effort // empty' "$ST")
  if [ "$(jq -r '.phase' "$ST")" = mapper_confirm ] && [ "$(jq -r '.map_prepass_pending // null' "$ST")" != null ]; then return 0; fi
  if [ "$auth" != null ] && jq -e --argjson a "$auth" '.config as $c | $a.kind==($c.map_prepass.kind // "opencode") and $a.model==($c.map_prepass.model // "google/gemini-3.8-flash") and $a.effort==($c.map_prepass.effort // "medium") and ($a.source=="config" or $a.source=="user-confirmed")' "$ST" >/dev/null 2>&1; then return 0; fi
  if [ -n "$model" ] && [ -n "$effort" ]; then
    check_mapper_available "$cfg"
    sts '.map_prepass_authorized={kind:(.config.map_prepass.kind // "opencode"),model:.config.map_prepass.model,effort:.config.map_prepass.effort,source:"config"} | .map_prepass_pending=null' || { log "could not persist configured mapper authorization; refusing inference"; exit 4; }
    return 0
  fi
  tuple=$(mapper_tuple "$cfg")
  sts --argjson p "$tuple" '.map_prepass_pending=$p | .status="questions" | .phase="mapper_confirm" | .pending_questions=[{id:"mapper-confirmation",task:null,member:"orchestrator",question:("Explicitly confirm mapper "+$p.kind+" "+$p.model+" ["+$p.effort+"] by resuming with --confirm-mapper FILE containing the exact {kind,model,effort} tuple.")}]' || { log "could not persist mapper proposal; refusing inference"; exit 4; }
  jq '.pending_questions' "$ST" >"$RUN/questions.json" || { log "could not persist mapper confirmation question"; exit 4; }
  render_transcript
  log "enabled code-map run has no durable mapper authorization; confirmation is required before inference (proposed $model $effort)"
  exit 4
}
check_mapper_available() {  # config JSON -> validates configured/recommended model metadata without inference
  local cfg=$1 tuple model effort models m variants
  [ "$(jq -r '.map_code' <<<"$cfg")" = true ] || return 0
  tuple=$(mapper_tuple "$cfg"); model=$(jq -r .model <<<"$tuple"); effort=$(jq -r .effort <<<"$tuple")
  models=$("$OC" api GET "/api/model?location%5Bdirectory%5D=$(jq -rn --arg d "$(jq -r .dir <<<"$cfg")" '$d|@uri')") || die "cannot list models to validate mapper"
  m=$(jq -c --arg m "$model" '.data[] | select(.enabled and (.providerID+"/"+.id)==$m)' <<<"$models")
  [ -n "$m" ] || die "mapper model not enabled: $model (configure map_prepass.model explicitly; no substitution)"
  variants=$(jq -r '[.variants[]?.id]|join("|")' <<<"$m")
  if [ -n "$variants" ]; then jq -e --arg e "$effort" '[.variants[].id]|index($e)' <<<"$m" >/dev/null || die "mapper effort '$effort' is not a variant of $model (valid: $variants)"
  else [ "$effort" = default ] || die "mapper model $model has no variants; use effort default"; fi
}

cost_note() {  # config only; measured comparisons, never a dollar forecast or a gate
  jq -r '
    (.members|length) as $n | (.tasks|length) as $t |
    [.members[] | select(.effort|IN("high","xhigh","max")) | "\(.id) \(.model) [\(.effort)]"] as $high |
    "COST NOTE: \($n) members x \(.max_rounds) rounds x \($t) tasks = \($n*.max_rounds*$t) deliberation member-rounds (execution, retries and handovers can add calls).",
    "  High-cost efforts (high/xhigh/max): \(if ($high|length)>0 then $high|join(", ") else "none" end).",
    (if $n>=3 and .max_rounds>=4 and ($high|length)>=2 then
      "  WARNING: this configuration is in the size/effort range of expensive measured runs; task breadth also matters."
     else empty end),
    "  Similar runs measured: 3 members (Astra xhigh + Claude Fable xhigh + Kimi max), 2 broad tasks, max_rounds 4: 27.9M tokens / ~$17.6.",
    "  Similar runs measured: 3-member research council (Astra xhigh + Fable xhigh + Gemini 3.8 Flash high), 1 broad task, max_rounds 4: 11.3M tokens / ~$21.4; Claude Fable alone: $20.2.",
    "  These are measurements, not predictions. Levers: fewer members, lower effort, narrower tasks, fewer rounds."
  ' <<<"$1"
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
  validate_child_launch || { [ "$(st .phase)" = split_invalid ] && exit 4; return 1; }
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
    "$OC" result "$sid" >"$out" 2>&1; local result_rc=$?
    if [ $result_rc -ne 0 ]; then
      local why; why=$(grep -m1 -E '^\[(error|outcome)\]' "$out" | cut -c1-300)
      log "member $id: result failed (exit $result_rc)${why:+ — $why}"
      [ $rc -ne 0 ] || rc=1
    fi
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
# Prose compression, modelled on the "caveman" skill (github.com/juliusbrussee/caveman): drop throat-clearing,
# keep substance. It applies ONLY to the prose a member writes for the other members — never to the JSON tail's
# proposal/report (that text is voted on, ratified and implemented, so it stays complete), and never to quoted
# code, paths, commands, errors or numbers. Measured expectation: the JetBrains lab found ~8.5% fewer output
# tokens on real agentic tasks (the skill's own README claims ~50% on output-only evals); in this council the
# JSON tail is 74-99% of a post, so treat single-digit percent as the honest expectation.
style_rules() {  # member style -> extra rule text (empty for "normal")
  case "$1" in
    lite) cat <<'TXT'
6. Style — lite: write your prose for the other members without throat-clearing. No preamble, no restating the task, no summary of what you are about to say, no closing pleasantries. Full sentences are fine; just drop the filler.
TXT
;;
    caveman) cat <<'TXT'
6. Style — caveman: compress the prose you write for the other members. Drop articles, pleasantries, hedging and transitions; use fragments and lists; one line per point; no preamble and no recap. Keep every technical fact.
   NEVER compress: the JSON tail (its "proposal"/"report" must stay complete, unambiguous English — it is what gets voted on and implemented), quoted code, file paths, commands, error messages, numbers, names, and anything you quote from the task, the working directory or the user's answers. Precision beats brevity: if compressing a sentence could change its meaning, write it out.
TXT
;;
    ultra) cat <<'TXT'
6. Style — ultra: telegraphic prose. Fragments only, one line per point, symbols over words (-> = becomes, != = not). No articles, no hedging, no preamble, no recap.
   NEVER compress: the JSON tail (its "proposal"/"report" must stay complete, unambiguous English — it is what gets voted on and implemented), quoted code, file paths, commands, error messages, numbers, names, and anything you quote from the task, the working directory or the user's answers. Precision beats brevity: if compressing a sentence could change its meaning, write it out.
TXT
;;
  esac
}

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
$(style_rules "$(mget $1 style)")
TXT
}
task_header() { local t=$1 r=$2; echo "=== TASK $(jq -r .id <<<"$t") — round $r of $MAXR ==="; }
task_text()   { jq -r '.text' <<<"$1"; }
task_inherited_answers() { jq -r '(.inherited_answers // []) | map("- Q: \(.question)\n  A: \(.answer)") | if length==0 then "" else "Original parent-task user answers (verbatim):\n" + join("\n") + "\n" end' <<<"$1"; }
task_intro()  { local t=$1; task_text "$t"; jq -e '.execute' <<<"$t" >/dev/null && echo "
(This is a BUILD task: the council first agrees on a PLAN — concrete files, changes, verification commands. Then executor $EXEC implements the plan, and the council ratifies the actual result.)"
  local inherited; inherited=$(task_inherited_answers "$t"); [ -z "$inherited" ] || printf '\n%s' "$inherited"
  jq -e '(.map_parent != null) and ((.acceptance // [])|length>0)' <<<"$t" >/dev/null 2>&1 && { echo; echo "Approved acceptance specifications:"; jq '.acceptance' <<<"$t"; }
}

prompt_round1() {  # idx task
  local tid; tid=$(jq -r .id <<<"$2")
  local schema; schema=$(codemap_schema_text); [ -n "$schema" ] && schema="$schema"$'\n'
  cat <<TXT
$(task_header "$2" 1)
$(task_intro "$2")
$(answers_block)
Your job this round: (1) list EVERY choice the task leaves open and every fact you need, and for each say what settles it: "task" (quote it), "dir" (path you verified), "user" (an answer from the user, quoted) or "ask" (the user must decide); (2) if any item is "ask", vote "question" (or vote "propose" — the orchestrator turns "ask" items into questions anyway); otherwise give your complete proposed $( jq -e '.execute' <<<"$2" >/dev/null && echo plan || echo answer ). Other members do the same; next round you will see their proposals.
${schema}JSON tail — exactly one of:
\`\`\`json
{"vote": "propose", "open": [{"item": "<open choice or needed fact>", "settled_by": "task|dir|user|ask", "where": "<quote from the task / file path / the user's answer, or the exact question for the user>"}], "proposal": "<your complete $( jq -e '.execute' <<<"$2" >/dev/null && echo plan || echo answer )>", "questions": []}
\`\`\`
\`\`\`json
{"vote": "question", "questions": ["<precise question for the user>"], "proposal": null}
\`\`\`
TXT
}
prompt_roundN_full() {  # lossless fallback; also the baseline for the round-trip check
  local i=$1 t=$2 r=$3 prev=$((r-1)) tid; tid=$(jq -r .id <<<"$t")
  local schema; schema=$(codemap_schema_text); [ -n "$schema" ] && schema="$schema"$'\n'
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
${schema}JSON tail — exactly one of:
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

# A literal, unique substitution. ENVIRON avoids awk -v escape interpretation. Callers
# append a sentinel when capturing stdout so bash never strips meaningful final newlines.
dedup_replace() {
  COUNCIL_TEXT=$1 COUNCIL_OLD=$2 COUNCIL_NEW=$3 LC_ALL=C awk 'BEGIN {
    s=ENVIRON["COUNCIL_TEXT"]; old=ENVIRON["COUNCIL_OLD"]; new=ENVIRON["COUNCIL_NEW"];
    p=index(s,old); if (!length(old) || !p || index(substr(s,p+length(old)),old)) exit 1;
    printf "%s%s%s",substr(s,1,p-1),new,substr(s,p+length(old));
  }'
}

dedup_source() {  # md, stored json -> DEDUP_TAIL and DEDUP_TOKEN, or failure
  local md=$1 stored=$2 parsed
  [ -s "$md" ] && [ -s "$stored" ] || return 1
  # Shell/ENVIRON cannot carry NUL. Reject it rather than silently dropping bytes.
  jq -en --rawfile p "$md" '$p|contains("\u0000")|not' >/dev/null 2>&1 || return 1
  parsed=$(tail_json "$md") || return 1
  jq -e -s 'length==2 and .[0]==.[1] and (.[0].proposal|type)=="string"
    and (.[0].proposal|contains("\u0000")|not)' <(printf '%s' "$parsed") "$stored" >/dev/null 2>&1 || return 1
  DEDUP_TAIL=$(awk 'BEGIN{b=0;last=""} /^[[:space:]]*```[[:space:]]*[Jj][Ss][Oo][Nn][[:space:]]*$/{b=1;buf="";next}
    /^[[:space:]]*```[[:space:]]*$/{if(b){b=0;last=buf};next} {if(b)buf=buf $0 "\n"} END{printf "%s.",last}' "$md")
  DEDUP_TAIL=${DEDUP_TAIL%.}
  # Locate a single top-level, literally-spelled proposal key, scanning JSON strings
  # without decoding their escapes. Unusual/duplicate/escaped keys conservatively fall back.
  DEDUP_TOKEN=$(COUNCIL_TAIL=$DEDUP_TAIL LC_ALL=C awk 'BEGIN {
    s=ENVIRON["COUNCIL_TAIL"]; depth=0; count=0;
    for (i=1;i<=length(s);i++) {
      c=substr(s,i,1);
      if (c=="\"") {
        start=i++; while (i<=length(s)) { c=substr(s,i,1); if(c=="\\") i+=2; else if(c=="\"") break; else i++; }
        token=substr(s,start,i-start+1); j=i+1; while(substr(s,j,1) ~ /[ \t\r\n]/ && j<=length(s)) j++;
        if(depth==1 && substr(s,j,1)==":") {
          if(index(token,"\\")) exit 1;
          key=token; if(key=="\"proposal\"") count++;
        } else if(depth==1 && key=="\"proposal\"") { value=token; key=""; }
      } else if(c=="{" || c=="[") depth++; else if(c=="}" || c=="]") depth--;
    }
    if(count!=1 || !length(value)) exit 1; printf "%s",value;
  }') || return 1
  jq -en --argjson token "$DEDUP_TOKEN" --argjson p "$parsed" '$token==$p.proposal' >/dev/null 2>&1
}

prompt_roundN() (  # idx task round; all edits are prompt-local, never stored posts/state
  local i=$1 t=$2 r=$3 prev=$(($3-1)) tid tmp original work restored x hn
  local j k source=-1 candidate ref anchor saving total=0
  local ids=() posts=() tails=() tokens=() anchors=() changed=() oldblocks=() newblocks=() messages=()
  local LC_ALL=C; export LC_ALL
  tid=$(jq -r .id <<<"$t")
  tmp=$(mktemp -d "${TMPDIR:-/tmp}/council-dedup.XXXXXX") || { prompt_roundN_full "$@"; return; }
  trap 'rm -rf "$tmp"' EXIT
  prompt_roundN_full "$@" >"$tmp/original"
  # Include first-contact material in the collision check, even though run_step emits
  # it before this body. Any pre-existing use of the reference namespace disables edits.
  hn=$(mget "$i" handover_note)
  if grep -Fq COUNCIL_POST "$tmp/original" ||
     jq -e 'any(..|strings; contains("COUNCIL_POST"))' "$ST" >/dev/null 2>&1 ||
     { [ "$(mget "$i" fresh)" = true ] && [ -f "$hn" ] && grep -Fq COUNCIL_POST "$hn"; }; then
    cat "$tmp/original"; return
  fi
  original=$(cat "$tmp/original"; printf .); original=${original%.}; work=$original
  for j in $(seq 0 $((N-1))); do
    [ "$j" = "$i" ] && continue
    ids[$j]="$tid-r$prev-$(mid "$j")"
    case "${ids[$j]}" in *[!a-zA-Z0-9._-]*) continue ;; esac  # unambiguous anchor spelling
    if dedup_source "$RUN/posts/${ids[$j]}.md" "$RUN/posts/${ids[$j]}.json"; then
      posts[$j]=$(cat "$RUN/posts/${ids[$j]}.md"; printf .); posts[$j]=${posts[$j]%.}
      tails[$j]=$DEDUP_TAIL; tokens[$j]=$DEDUP_TOKEN
    fi
  done
  candidate=$(st .candidate.text)
  for j in $(seq 0 $((N-1))); do
    [ -n "${tokens[$j]:-}" ] || continue
    jq -e --slurpfile p "$RUN/posts/${ids[$j]}.json" '.candidate.text==$p[0].proposal' "$ST" >/dev/null 2>&1 || continue
    ref="Exact candidate: JSON-decode the 'proposal' string in COUNCIL_POST ${ids[$j]} below."
    anchor="<<<COUNCIL_POST ${ids[$j]}>>>"$'\n'
    saving=$((${#candidate}-${#ref}-${#anchor})); [ "$saving" -gt 0 ] || continue
    source=$j; anchors[$j]=$anchor; total=$((total+saving))
    messages+=("1a candidate $(st .candidate.id) -> ${ids[$j]} for member $(mid "$i"): saved $saving bytes")
    break
  done
  for j in $(seq 0 $((N-1))); do
    [ -n "${tokens[$j]:-}" ] && [ "$j" -ne "$source" ] || continue
    for ((k=0;k<j;k++)); do
      [ -n "${tokens[$k]:-}" ] && [ -z "${changed[$k]:-}" ] || continue
      [ "${tokens[$j]}" = "${tokens[$k]}" ] || continue
      local replacement tail post back
      replacement=$(jq -cn --arg id "${ids[$k]}" '"(identical to the proposal string in COUNCIL_POST " + $id + " above)"')
      anchor="<<<COUNCIL_POST ${ids[$k]}>>>"$'\n'
      saving=$((${#tokens[$j]}-${#replacement})); [ -n "${anchors[$k]:-}" ] || saving=$((saving-${#anchor}))
      [ "$saving" -gt 0 ] || continue
      tail=$(dedup_replace "${tails[$j]}" "${tokens[$j]}" "$replacement" && printf .) || continue; tail=${tail%.}
      back=$(dedup_replace "$tail" "$replacement" "${tokens[$j]}" && printf .) || continue; back=${back%.}
      [ "$back" = "${tails[$j]}" ] || continue
      # Ensure the located token really is the top-level proposal, not other prose/data.
      jq -en --argjson old "${tails[$j]}" --argjson new "$tail" --argjson ref "$replacement" '$new==($old|.proposal=$ref)' >/dev/null 2>&1 || continue
      post=$(dedup_replace "${posts[$j]}" "${tails[$j]}" "$tail" && printf .) || continue; post=${post%.}
      back=$(dedup_replace "$post" "$tail" "${tails[$j]}" && printf .) || continue; back=${back%.}
      [ "$back" = "${posts[$j]}" ] || continue
      changed[$j]=$post; anchors[$k]=$anchor; total=$((total+saving))
      messages+=("1b ${ids[$j]} proposal -> ${ids[$k]} for member $(mid "$i"): saved $saving bytes")
      break
    done
  done
  if [ "$source" -ge 0 ]; then
    oldblocks+=("<<<CANDIDATE $(st .candidate.id)"$'\n'"$candidate"$'\n'">>>")
    newblocks+=("<<<CANDIDATE $(st .candidate.id)"$'\n'"Exact candidate: JSON-decode the 'proposal' string in COUNCIL_POST ${ids[$source]} below."$'\n'">>>")
  fi
  for j in $(seq 0 $((N-1))); do
    [ -n "${anchors[$j]:-}${changed[$j]:-}" ] || continue
    oldblocks+=("--- member $(mid "$j") ---"$'\n'"${posts[$j]}"$'\n')
    newblocks+=("--- member $(mid "$j") ---"$'\n'"${anchors[$j]:-}${changed[$j]:-${posts[$j]}}"$'\n')
  done
  for ((j=0;j<${#oldblocks[@]};j++)); do
    x=$(dedup_replace "$work" "${oldblocks[$j]}" "${newblocks[$j]}" && printf .) || { cat "$tmp/original"; return; }; work=${x%.}
  done
  restored=$work
  for ((j=${#oldblocks[@]}-1;j>=0;j--)); do
    x=$(dedup_replace "$restored" "${newblocks[$j]}" "${oldblocks[$j]}" && printf .) || { cat "$tmp/original"; return; }; restored=${x%.}
  done
  # Byte-for-byte re-substitution proof, and a strictly positive net saving, before send.
  printf '%s' "$restored" >"$tmp/restored"
  if [ "$total" -le 0 ] || [ "$((${#original}-${#work}))" -ne "$total" ] || ! cmp -s "$tmp/original" "$tmp/restored"; then
    cat "$tmp/original"; return
  fi
  for x in "${messages[@]}"; do log "prompt dedup $tid-r$r: $x"; done
  printf '%s' "$work"
)
prompt_exec() {  # idx task round
  cat <<TXT
=== TASK $(jq -r .id <<<"$2") — EXECUTION ===
$(answers_block)
The council reached consensus on this plan:
<<<PLAN
$(st '.results[-1].text')
>>>
TXT
  local inherited; inherited=$(task_inherited_answers "$2"); [ -z "$inherited" ] || { echo; printf '%s\n' "$inherited"; }  # keep the last answer off the next line
  if jq -e '(.map_parent != null) and ((.acceptance // [])|length>0)' <<<"$2" >/dev/null 2>&1; then
    echo; echo "Approved child acceptance specifications:"; jq '.acceptance' <<<"$2"
  fi
  cat <<TXT
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
notices_block() {  # one-off notes from the orchestrator (e.g. a member was replaced), cleared after the step
  local n; n=$(jq -r '(.notices // []) | if length==0 then "" else "Notes from the orchestrator:\n" + (map("- " + .)|join("\n")) + "\n" end' "$ST")
  [ -n "$n" ] && printf '%s\n' "$n"
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
member_handover_at() { st ".members[$1].handover_at // $HANDOVER"; }
# handover_at <= 1 is a fraction of the model's context window; > 1 is an absolute token count.
needs_handover() { st ".members[$1] | (.handover_at // $HANDOVER) as \$h
  | ((.session_calls // 0) >= 2 and (.ctx_used // 0) > 0
     and (if \$h > 1 then (.ctx_used >= \$h) else ((.ctx_limit // 0) > 0 and (.ctx_used / .ctx_limit) >= \$h) end))" | grep -q true; }

do_handover() {  # idx -> old session writes a note; new session created; note stored for the next prompt
  local i=$1 id; id=$(mid $i)
  log "member $id: context $(st ".members[$i].ctx_used // 0") tokens ($(ctx_pct $i)%) reached its $(st ".members[$i] | (.handover_at // $HANDOVER) as \$h | if \$h > 1 then (\$h|tostring)+\" tokens\" else ((\$h*100|floor)|tostring)+\"%\" end") handover threshold — new session"
  local pf="$RUN/prompts/handover-$id-g$(mget $i gen).md"; prompt_handover $i >"$pf"
  append_historical_map_locator "$pf" "$(st .task_id)"
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
  validate_child_launch || { [ "$(st .phase)" = split_invalid ] && exit 4; return 1; }
  local idxs; if [ -n "$only" ]; then idxs=$only; else idxs=$(seq 0 $((N-1))); fi
  local -a launch_gen=()   # generation recorded at launch time, for attribution (see codemap_stage_accepted)
  local -a launch_id=()    # the orchestration-owned identity of the launch that produced the reply
  CODEMAP_STEP=0
  if [ "$CODEMAP" = 1 ] && codemap_is_deliberation "$gen"; then
    codemap_prepare "$tid" "$tag" "$( [ "$gen" = prompt_round1 ] && echo 1 || echo 0 )"
  fi
  for i in $idxs; do
    needs_handover $i && do_handover $i
    pf="$RUN/prompts/$tid-$tag-$(mid $i).md"
    if [ "$(st '.reuse_posts // false')" = true ] && post_valid "$RUN/posts/$tid-$tag-$(mid $i).json" && [ -s "$RUN/posts/$tid-$tag-$(mid $i).md" ]; then
      log "member $(mid $i): reusing its $tag post from the checkpoint (not re-run)"; sts --argjson i $i '.members[$i].inflight={tag:"reuse"}'; continue
    fi
    launch_gen[$i]=$(mget $i gen)
    codemap_record_launch $i "$tid" "$tag" "${launch_gen[$i]}"; launch_id[$i]=$CODEMAP_LAUNCH_ID
    # The prompt is assembled in place rather than in one redirected block, so the orchestrator
    # knows the exact byte offset of every locator block IT writes (see codemap_append_locator).
    # The resulting bytes are identical to the single-block form: the locator sat at the start of
    # the generator's output and inside the handover section, which is exactly where it goes here.
    : >"$pf"
    if [ "$(mget $i fresh)" = true ]; then
      { rules_text $i; echo; } >>"$pf"
      local hn; hn=$(mget $i handover_note)
      if [ "$hn" != null ] && [ -n "$hn" ]; then
        { echo "Handover note from your predecessor session (same member id):"; echo "<<<HANDOVER"; cat "$hn"; } >>"$pf"
        codemap_append_locator "$pf" "$tid" "$tag" handover
        { echo ">>>"; echo; } >>"$pf"
      fi
    fi
    notices_block >>"$pf"
    codemap_append_locator "$pf" "$tid" "$tag" prompt
    case "$gen" in prompt_round1|prompt_roundN) ;; *) append_historical_map_locator "$pf" "$tid" ;; esac
    $gen $i "$t" "$r" >>"$pf"
    launch $i "$pf" "$tid-$tag" || { log "member $(mid $i): launch failed"; return 1; }
    codemap_record_launched "$pf" "$tid" "$tag"
  done
  local fail=0
  for i in $idxs; do
    local id; id=$(mid $i); local ok=0 attempt
    if [ "$(st ".members[$i].inflight.tag")" = reuse ]; then sts --argjson i $i '.members[$i].inflight=null'; continue; fi
    for attempt in 1 2; do
      if collect $i && tail_json "$RUN/raw/$tid-$tag-$id.md" >"$RUN/posts/$tid-$tag-$id.json" && jq -e '(.vote|IN("propose","agree","disagree","question","done")) and
                 (if .vote=="propose" or .vote=="disagree" then ((.proposal|type)=="string" and (.proposal|length)>0)
                  elif .vote=="done" then ((.report|type)=="string" and (.report|length)>0)
                  elif .vote=="question" then ((.questions|type)=="array" and (.questions|length)>0) else true end)' "$RUN/posts/$tid-$tag-$id.json" >/dev/null; then ok=1; break; fi
      # A re-dispatched retry is a genuinely new launch: it gets its own immutable record, so the
      # reply it produces is bound to it and not to the launch whose reply was unusable.
      [ $attempt -eq 1 ] && { log "member $id: no valid JSON tail / call failed — retrying once"; prompt_retry >"$RUN/prompts/$tid-$tag-$id-retry.md"; launch_gen[$i]=$(mget $i gen); codemap_record_launch $i "$tid" "$tag" "${launch_gen[$i]}"; launch_id[$i]=$CODEMAP_LAUNCH_ID; launch $i "$RUN/prompts/$tid-$tag-$id-retry.md" "$tid-$tag" || break; }
    done
    sts --argjson i $i '.members[$i].fresh=false | .members[$i].handover_note=null'
    if [ $ok -eq 1 ]; then
      cp "$RUN/raw/$tid-$tag-$id.md" "$RUN/posts/$tid-$tag-$id.md"
      # no-assumptions guard: an open item not settled by the task or the working directory is a question, whatever the vote says
      if jq -e '(.vote=="propose" or .vote=="disagree") and any((.open // [])[]; (.settled_by|ascii_downcase|IN("task","dir","user"))|not)' "$RUN/posts/$tid-$tag-$id.json" >/dev/null 2>&1; then
        jq -c '.questions = ((.questions // []) + [ .open[] | select((.settled_by|ascii_downcase|IN("task","dir","user"))|not) | (.where // .item) ]) | .vote="question" | .proposal=null' "$RUN/posts/$tid-$tag-$id.json" >"$RUN/posts/$tid-$tag-$id.json.tmp" && mv "$RUN/posts/$tid-$tag-$id.json.tmp" "$RUN/posts/$tid-$tag-$id.json"
        log "member $id: proposal had open items not settled by task/dir — converted to questions for the user"
      fi
      codemap_stage_accepted $i "$tid" "$tag" "$RUN/posts/$tid-$tag-$id.json" "${launch_gen[$i]}" "${launch_id[$i]}"
    else fail=1; log "member $id: failed twice in step $tag"; fi
    local vote=FAILED; [ $ok -eq 1 ] && vote=$(jq -r '.vote' "$RUN/posts/$tid-$tag-$id.json")
    local cu cl stt; cu=$(st ".members[$i].ctx_used // 0"); cl=$(st ".members[$i].ctx_limit // \"?\""); stt=$(st ".members[$i].session_tokens // 0")
    log "$(now) $tid $tag member $id: $vote · ctx $(ctx_pct $i)% [$cu/$cl] · session tokens $stt"
  done
  if [ "$fail" -eq 0 ] && [ "$CODEMAP_STEP" = 1 ]; then
    codemap_finish_step "$tid" "$tag" || fail=1
  fi
  # collect votes from the post tails
  local votes="[]"; for i in $idxs; do local id; id=$(mid $i); [ -f "$RUN/posts/$tid-$tag-$id.json" ] && votes=$(jq -c --arg m "$id" --slurpfile p "$RUN/posts/$tid-$tag-$id.json" '. + [ $p[0] + {member:$m} ]' <<<"$votes"); done
  sts --argjson v "$votes" '.last_votes=$v | .notices=[] | .reuse_posts=false'
  render_transcript
  return $fail
}

post_valid() {  # json tail file -> 0 if it parses and has a usable vote
  [ -s "$1" ] && jq -e '(.vote|IN("propose","agree","disagree","question","done"))' "$1" >/dev/null 2>&1
}

replace_member() {  # "ID=kind:model:effort" -> new session for that member, handover note built from its own posts
  local spec=$1 id kind model effort i
  id=${spec%%=*}; local rest=${spec#*=}; kind=${rest%%:*}; rest=${rest#*:}; effort=${rest##*:}; model=${rest%:*}
  [ -n "$id" ] && [ -n "$kind" ] && [ -n "$model" ] && [ -n "$effort" ] && [ "$model" != "$effort" ] || die "--replace expects ID=kind:model:effort (got: $spec)"
  i=""; local j; for j in $(seq 0 $((N-1))); do [ "$(mid $j)" = "$id" ] && i=$j; done; [ -n "$i" ] || die "--replace: no member $id"
  local mode; mode=$(mget $i mode); local ctx=null extra
  case "$kind" in
    claude)   echo "$effort" | grep -qxE 'low|medium|high|xhigh|max' || die "--replace: claude effort must be low|medium|high|xhigh|max"
              extra=$(jq -cn --arg pm "$( [ "$mode" = edit ] && echo acceptEdits || echo plan )" '{permission_mode:$pm, agent:null}') ;;
    opencode) local mj; mj=$("$OC" api GET "/api/model?location%5Bdirectory%5D=$(jq -rn --arg d "$DIR" '$d|@uri')" | jq -c --arg m "$model" '.data[] | select(.enabled and (.providerID+"/"+.id)==$m)')
              [ -n "$mj" ] || die "--replace: OpenCode model not enabled: $model"
              jq -e --arg e "$effort" '([.variants[]?.id] | index($e)) != null or ($e=="default" and ([.variants[]?]|length)==0)' <<<"$mj" >/dev/null || die "--replace: effort '$effort' is not a variant of $model (valid: $(jq -r '[.variants[]?.id]|join("|")' <<<"$mj"))"
              ctx=$(jq '.limit.context' <<<"$mj"); extra=$(jq -cn --arg a "$( [ "$mode" = edit ] && echo build || echo plan )" '{agent:$a, permission_mode:null}') ;;
    *) die "--replace: kind must be opencode|claude" ;;
  esac
  local note="$RUN/raw/replace-$id-g$(mget $i gen).md"
  { echo "Your predecessor session as member $id ($(mget $i kind) $(mget $i model), effort $(mget $i effort)) could not continue (provider failure or replacement by the user) and could not write a handover note. Below are ALL the posts it made in this run, oldest first — they are your positions so far; continue from them."
    local f; for f in $(ls -tr "$RUN/posts" 2>/dev/null | grep -- "-$id\.md$"); do echo; echo "--- post ${f%.md} ---"; cat "$RUN/posts/$f"; done; } >"$note"
  local old; old=$(mget $i session)
  sts --argjson i $i --arg kind "$kind" --arg model "$model" --arg effort "$effort" --argjson ctx "$ctx" --argjson extra "$extra" --arg note "$note" --arg old "$old" --arg now "$(now)" '
    .members[$i] |= (.retired += [{session:.session, gen:.gen, tokens:.session_tokens, cost:.session_cost, model:(.kind+" "+.model), reason:"replaced"}]
                     | .gen+=1 | .session=null | .fresh=true | .handover_note=$note | .ctx_used=0 | .ctx_limit=$ctx | .session_tokens=0 | .session_cost=0 | .session_calls=0
                     | .kind=$kind | .model=$model | .effort=$effort | . + $extra | del(.[] | nulls))
    | .config.members[$i] |= (. + {kind:$kind, model:$model, effort:$effort} + $extra | del(.[] | nulls))
    | .notices += ["member \(.members[$i].id) is now \($kind) \($model) (effort \($effort)); its previous session could not continue. It has its earlier posts."]
    | .log += ["\($now) member \(.members[$i].id) g\(.members[$i].gen): replaced \($old) with \($kind) \($model) [\($effort)]"]'
  log "member $id: replaced with $kind $model [$effort] (previous session $old retired; note built from $(grep -c '^--- post' "$note") earlier posts)"
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
      sts '.voted_candidate=.candidate'   # keep the candidate this round actually voted on (for an unresolved outcome)
      sts --arg tid "$tid" --argjson r $r --arg m "$(mid $p)" --arg txt "$(position_of $p)" '.candidate={id:($tid+"-c"+($r|tostring)), member:$m, text:$txt}'
    fi
    r=$((r+1))
  done
  # exhausted: record unresolved with the candidate of the LAST VOTE (not the next rotating proposal) and every dissent
  sts --arg tid "$tid" --argjson r "$MAXR" '(.voted_candidate // .candidate) as $c | .results += [{task:$tid, outcome:"unresolved", text:$c.text, candidate:$c.id, rounds:$r, dissent:[.last_votes[] | select(.vote!="agree") | {member, reason, proposal}]}] | .round=1 | .voted_candidate=null'
  log "task $tid: UNRESOLVED after $MAXR rounds"; return 1
}

executor_idx() { local i; for i in $(seq 0 $((N-1))); do [ "$(mid $i)" = "$EXEC" ] && { echo $i; return; }; done; }
diff_text() {  # executor idx -> diff of the working dir as seen by the executor's tool
  local i=$1; if [ "$(mget $i kind)" = opencode ]; then "$OC" diff "$(mget $i session)" --patch 2>/dev/null | cap_diff
  elif git -C "$DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then (cd "$DIR" && git status --short && git diff) | cap_diff
  else echo "(dir is not a git repository — no diff available; rely on the report and inspect the files)"; fi
}
cap_diff() (  # consume the stream, preserve head's byte cap, make truncation explicit
  local tmp; tmp=$(mktemp "${TMPDIR:-/tmp}/council-diff.XXXXXX") || return 1
  trap 'rm -f "$tmp"' EXIT
  cat >"$tmp" || return 1
  head -c 20000 "$tmp"
  if [ "$(wc -c <"$tmp")" -gt 20000 ]; then
    printf '\n[DIFF TRUNCATED: 20000-byte cap; inspect the full working directory: %s]\n' "$DIR"
  fi
)
execute_and_ratify() {  # task json; assumes .results[-1] is the consensus plan; returns 0 ratified / 1 unresolved
  local t=$1 tid ei r; tid=$(jq -r .id <<<"$t"); ei=$(executor_idx)
  r=$(st .round)
  if [ "$(st .phase)" = "plan" ]; then
    validate_child_launch || { [ "$(st .phase)" = split_invalid ] && exit 4; return 1; }
    if [ -n "$(jq -r '.map_parent // empty' <<<"$t")" ]; then
      local child_parent contract_digest
      child_parent=$(jq -r .map_parent <<<"$t")
      contract_digest=$(jq -r --arg p "$child_parent" '.map_prepasses[$p].approved_contract.digest // empty' "$ST")
      [ -n "$contract_digest" ] || { log "cannot bind execution entry to an approved contract"; exit 4; }
      sts --arg p "$child_parent" --arg c "$tid" --arg d "$contract_digest" '.map_prepasses[$p].execution_started[$c]={contract_digest:$d,started_at:(now|floor)} | .phase="exec"' || { log "could not persist authorized execution entry"; exit 4; }
    else sts '.phase="exec"'; fi
  fi
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
    nt=$(st '.config.tasks|length')
    local t; t=$(stj ".config.tasks[$ti]"); local tid parent; tid=$(jq -r .id <<<"$t"); parent=$(jq -r '.map_parent // .id' <<<"$t")
    sts --argjson ti $ti --arg tid "$tid" '.task_idx=$ti | .task_id=$tid | .status="running"'
    log "==== task $tid ($((ti+1))/$nt) phase $(st .phase) round $(st .round) ===="
    if map_prepass_enabled; then
      if [ "$parent" = "$tid" ]; then
        local reviewed mapstatus; reviewed=$(jq -r --arg p "$parent" '.map_prepasses[$p].reviewed // false' "$ST")
        mapstatus=$(jq -r --arg p "$parent" '.map_prepasses[$p].status // "new"' "$ST")
        if [ "$reviewed" != true ]; then
          if [ "$mapstatus" = new ] || [ "$mapstatus" = preparing ] || [ "$mapstatus" = created ] || [ "$mapstatus" = dispatching ] || [ "$mapstatus" = dispatched ]; then
            sts '.phase="map"'
            map_prepass_run "$t" || { sts '.status="failed"'; log "map pre-pass failed operationally; checkpointed"; exit 2; }
          fi
          sts '.phase="map_review"'
          map_prepass_review "$t"
        fi
      else
        # Contract-validated children share the original task's complete map and never launch a mapper.
        local reviewed; reviewed=$(jq -r --arg p "$parent" '.map_prepasses[$p].reviewed // false' "$ST")
        [ "$reviewed" = true ] || { log "child $tid has no approved parent map review"; exit 2; }
      fi
      if [ "$parent" != "$tid" ]; then
        validate_child_launch || { [ "$(st .phase)" = split_invalid ] && exit 4; exit 2; }
      fi
    fi
    case "$(st .phase)" in
      plan)
        if deliberate "$t"; then
          if [ "$(jq -r '.execute // false' <<<"$t")" = true ]; then execute_and_ratify "$t" || unresolved=1; fi
        else unresolved=1; fi
        ;;
      exec|ratify)
        if [ "$(jq -r '.execute // false' <<<"$t")" = true ]; then execute_and_ratify "$t" || unresolved=1
        else
          log "task $tid is execute:false; refusing to enter execution/ratification phase"
          sts '.status="questions" | .phase="plan" | .pending_questions=[{id:"invalid-execution-phase",task:.task_id,member:"orchestrator",question:"This task is execute:false but state requests execution. Resolve the checkpoint before continuing."}]'
          jq '.pending_questions' "$ST" >"$RUN/questions.json"; render_transcript; exit 4
        fi
        ;;
      *)
        log "unsupported task phase $(st .phase); refusing inference"
        exit 4
        ;;
    esac
    ti=$((ti+1)); sts --argjson ti $ti '.task_idx=$ti | .round=1 | .phase="plan" | .candidate=null | .last_votes=[] | .fixes=null'
  done
  sts '.status="done"'; render_transcript
  log "done — transcript: $RUN/transcript.md"; echo "$RUN/transcript.md"
  [ $unresolved -eq 0 ] && exit 0 || exit 5
}

# --------------------------------------------------------------- report ----
# Offline token accounting for a finished (or in-progress) run. No model calls, no network.
token_report() {  # prints the report; returns non-zero only if both tools fail
  local rc=0
  echo "### Prompt bytes by section and de-duplication replay"; echo
  echo '```'
  python3 "$PTOOLS/prompt_report.py" "$RUN" 2>&1 || rc=1
  echo '```'; echo
  echo "### Verbatim duplication still present inside single prompts (>= 128 bytes)"; echo
  echo '```'
  python3 "$PTOOLS/dedup_check.py" "$RUN" 2>&1 | tail -40 || rc=1
  echo '```'
  if [ "$(st '.codemap_version // empty')" = "1" ]; then
    echo; echo "### Code map"; echo; echo '```'
    python3 "$PTOOLS/codemap_report.py" "$RUN" 2>&1 || echo "(the code map report is unavailable: see above)"
    echo '```'
  fi
  if [ "$(st '.map_prepass_version // empty')" = "1" ]; then
    echo; echo "### Map pre-pass"; echo; echo '```'
    python3 "$PTOOLS/codemap_report.py" "$RUN" --prepass 2>&1 || echo "(map pre-pass accounting is unavailable)"
    echo '```'
  fi
  return $rc
}

# ------------------------------------------------------------ transcript ----
usage_totals() {  # latest generation plus ALL retired generations; no API calls
  st '
    [.members[] | {id, final_tokens:(.session_tokens // 0), final_cost:(.session_cost // 0),
      retired_tokens:([.retired[]? | .tokens // 0]|add // 0),
      retired_cost:([.retired[]? | .cost // 0]|add // 0)}
      | . + {tokens:(.final_tokens+.retired_tokens), cost:(.final_cost+.retired_cost)}] as $m |
    ($m[] | "  member \(.id): final-generation \(.final_tokens) tokens / $\(.final_cost) + retired \(.retired_tokens) tokens / $\(.retired_cost) = subtotal \(.tokens) tokens / $\(.cost)"),
    (if .map_prepass_version==1 then "MEMBER SUBTOTAL: \($m|map(.tokens)|add // 0) tokens / $\($m|map(.cost)|add // 0) (all members, all generations)"
     else "RUN TOTAL: \($m|map(.tokens)|add // 0) tokens / $\($m|map(.cost)|add // 0) (all members, all generations)" end)
  '
  if [ "$(st '.map_prepass_version // empty')" = "1" ]; then
    st '
      . as $state |
      ([.map_prepasses|to_entries[] | {session:(.value.session // ("task:"+.key)),usage:(.value.usage // {})}] | unique_by(.session)) as $m |
      [$m[].usage | [.input,.cache_read,.cache_write,.output,.reasoning][] | select(type=="number" and (isinfinite|not) and (isnan|not))] as $tokens |
      [$m[].usage.cost | select(type=="number" and (isinfinite|not) and (isnan|not))] as $costs |
      [$m[] | select((.usage.input|type)!="number" or (.usage.cache_read|type)!="number" or (.usage.cache_write|type)!="number" or (.usage.output|type)!="number" or (.usage.reasoning|type)!="number") | .session] as $unknown |
      [$m[] | select((.usage.cost|type)!="number") | .session] as $unknown_cost |
      [ $state.members[] | (.session_tokens // 0) + ([.retired[]? | .tokens // 0]|add // 0)] as $member_tokens |
      [ $state.members[] | (.session_cost // 0) + ([.retired[]? | .cost // 0]|add // 0)] as $member_costs |
      "MAP PRE-PASS SUBTOTAL: \($tokens|add // 0) known tokens / \(if ($costs|length)>0 then "$\($costs|add)" else "UNKNOWN" end) known cost across \($m|length) unique sessions; token components unknown for: \(if ($unknown|length)>0 then ($unknown|join(",")) else "none" end); cost unknown for: \(if ($unknown_cost|length)>0 then ($unknown_cost|join(",")) else "none" end)",
      "RUN TOTAL (known components): \(($member_tokens|add // 0)+($tokens|add // 0)) tokens / $\(($member_costs|add // 0)+($costs|add // 0)); mapper telemetry unknowns retained, no estimates"
    '
  fi
}
render_transcript() {
  {
    echo "# Council run — $(st .started) — $RUN"; echo
    echo "status: **$(st .status)** · task $(st '.task_id // "-"') · phase $(st .phase) · round $(st .round)"; echo
    echo "## Roster"; echo; echo '```'; print_roster "$(stj .config)" "$(st '.members[] | "\(.id)\t\(.ctx_limit // "")"')"; echo '```'; echo
    echo "## Members — context and tokens"; echo
    echo "| member | generation | session | context used | session tokens | cost | calls | retired sessions |"; echo "|---|---|---|---|---|---|---|---|"
    st '.members[] | "| \(.id) | g\(.gen) | `\(.session // "-")` | \(.ctx_used // 0) / \(.ctx_limit // "?") (\(if (.ctx_limit//0)>0 then ((.ctx_used//0)/.ctx_limit*100|floor) else 0 end)%) | \(.session_tokens // 0) | \(.session_cost // 0 | .*10000|round/10000) | \(.calls // 0) | \((.retired // []) | map("\(.session) (\(.tokens) tok)") | join(", ")) |"'
    echo
    echo '```'; usage_totals; echo '```'; echo
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
    if [ "$(st .status)" = done ] && [ -d "$RUN/prompts" ]; then
      echo "## Token report"; echo; token_report || echo "(the report tools reported an error; see above)"; echo
    fi
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
    check_mapper_available "$cfg"
    print_roster "$cfg" "$lim"; cost_note "$cfg"; echo "config OK: $CONFIG" ;;

  start)
    [ -n "$CONFIG" ] && [ -n "$RUN" ] || die "start needs --config F --run-dir D"
    RUN=$(abs "$RUN"); [ -e "$RUN" ] && die "run dir exists: $RUN (use resume, or a new dir)"
    cfg=$(validate_config "$CONFIG") || exit 1
    "$OC" ensure >/dev/null || exit 1
    lim=$(check_opencode_models "$cfg") || exit 1
    check_mapper_available "$cfg"
    # Ownership: the leaf is created here (mkdir without -p fails if it appeared meanwhile), so a
    # failed initial state write may remove it and the identical start can simply be rerun.
    mkdir -p "$(dirname "$RUN")" && mkdir "$RUN" 2>/dev/null || die "cannot create run dir (it must not exist): $RUN"
    mkdir -p "$RUN/prompts" "$RUN/posts" "$RUN/raw"; cp "$CONFIG" "$RUN/config.json"
    if ! jq -n --argjson cfg "$cfg" --arg lim "$lim" --arg run "$RUN" --arg ts "$(date '+%Y-%m-%d %H:%M:%S')" --argjson cum "$(claude_cumulative)" '
      ($lim | split("\n") | map(select(length>0) | split("\t") | {key:.[0], value:(.[1]|tonumber)}) | from_entries) as $L |
      ({config:$cfg, run_dir:$run, started:$ts, status:"created", cl_cumulative:$cum, task_idx:0, task_id:null, round:1, phase:"plan", candidate:null, last_votes:[],
        codemap_version:1, codemap_pending:null,
        answers:[], pending_questions:null, notices:[], reuse_posts:false, results:[], log:[],
        members:[ $cfg.members[] | . + {session:null, gen:1, fresh:true, handover_note:null, ctx_used:0, ctx_limit:($L[.id] // null), session_tokens:0, session_cost:0, calls:0, session_calls:0, retired:[], inflight:null} ]}
        + (if $cfg.map_code then {map_prepass_version:1,map_prepasses:{}} else {} end))' >"$RUN/state.json.tmp" || ! mv "$RUN/state.json.tmp" "$RUN/state.json"; then
      rm -rf -- "$RUN"; log "could not persist initial run state; no model call made"; exit 1
    fi
    load_state
    if [ "$(jq -r '.map_code' <<<"$cfg")" = true ]; then
       sts '.map_prepass_version=1 | .map_prepasses={}' || { log "could not persist map-prepass version marker"; exit 1; }
      if [ -z "$(jq -r '.config.map_prepass.model // empty' "$ST")" ] || [ -z "$(jq -r '.config.map_prepass.effort // empty' "$ST")" ]; then
        proposed=$(mapper_tuple "$cfg")
         sts --argjson p "$proposed" '.map_prepass_pending=$p | .status="questions" | .phase="mapper_confirm" | .pending_questions=[{id:"mapper-confirmation",task:null,member:"orchestrator",question:("Explicitly confirm mapper "+$p.kind+" "+$p.model+" ["+$p.effort+"] by resuming with --confirm-mapper FILE containing the exact {kind,model,effort} tuple.")}]' || { log "could not persist mapper proposal; no model call made"; exit 1; }
         jq '.pending_questions' "$ST" >"$RUN/questions.json" || { log "could not persist mapper confirmation question"; exit 1; }
        render_transcript
        echo "council: mapper confirmation required before any model call; proposed $(jq -r '.kind+" "+.model+" ["+.effort+"]' <<<"$proposed")" >&2
        exit 4
      else
         sts '.map_prepass_authorized={kind:(.config.map_prepass.kind // "opencode"),model:.config.map_prepass.model,effort:.config.map_prepass.effort,source:"config"}' || { log "could not persist configured mapper authorization; no model call made"; exit 1; }
      fi
    fi
    load_state
    print_roster "$cfg" "$lim" >&2; log "run dir: $RUN"
    run_tasks ;;

  status)
    [ -n "$RUN" ] || die "status needs --run-dir D"; RUN=$(abs "$RUN"); load_state
    echo "run: $RUN · status: $(st .status) · task $(st '.task_id // "-"') ($(st .task_idx)/$(st '.config.tasks|length') done) · phase $(st .phase) · round $(st .round)/$MAXR"
    st '.members[] | "  member \(.id) g\(.gen) \(.kind) \(.model) [\(.effort)] session \(.session // "-") · ctx \(.ctx_used // 0)/\(.ctx_limit // "?") (\(if (.ctx_limit//0)>0 then ((.ctx_used//0)/.ctx_limit*100|floor) else 0 end)%; handover at \((.handover_at // 0.5) as $h | if $h > 1 then ($h|tostring)+" tok" else (($h*100|floor)|tostring)+"%" end))) · session tokens \(.session_tokens // 0) · cost \(.session_cost // 0) · calls \(.calls // 0) · retired \((.retired//[])|length)"'
    usage_totals
    st '.results[] | "  task \(.task): \(.outcome) (\(.rounds) rounds)"'
    jq -r '.pending_questions[]? | "  PENDING [\(.id)] member \(.member): \(.question)"' "$ST"
    echo "  transcript: $RUN/transcript.md" ;;

  report)
    [ -n "$RUN" ] || die "report needs --run-dir D"; RUN=$(abs "$RUN"); load_state
    echo "run: $RUN"; token_report; exit $? ;;

  resume)
    [ -n "$RUN" ] || die "resume needs --run-dir D"; RUN=$(abs "$RUN"); load_state
    "$OC" ensure >/dev/null || exit 1
    ensure_mapper_authorized
    status=$(st .status)
    case "$status" in
      questions)
        if [ "$(st .phase)" = mapper_confirm ]; then
          [ -z "$ANSWERS_FILE$ANSWER_TEXT$MAP_DECISION_FILE" ] || { log "mapper consent must use --confirm-mapper FILE, not --answer/--answers"; exit 4; }
          [ -n "$CONFIRM_MAPPER_FILE" ] || { log "mapper confirmation needs --confirm-mapper FILE"; exit 4; }
          apply_mapper_confirmation
        elif [ "$(st .phase)" = map_review ]; then
          [ -z "$ANSWERS_FILE$ANSWER_TEXT$CONFIRM_MAPPER_FILE" ] || { log "map review requires --map-decision FILE"; exit 4; }
          [ -n "$MAP_DECISION_FILE" ] || { log "map review requires --map-decision FILE"; exit 4; }
          apply_map_decision
        elif [ "$(st .phase)" = split_invalid ]; then
          [ -z "$ANSWERS_FILE$ANSWER_TEXT$CONFIRM_MAPPER_FILE" ] || { log "split resolution requires --map-decision FILE"; exit 4; }
          [ -n "$MAP_DECISION_FILE" ] || { log "split resolution requires --map-decision FILE (choose keep or a replacement split contract)"; exit 4; }
          apply_map_decision
        else
        pend=$(stj '.pending_questions')
        answer_mode="text"; ans='{}'
        if [ -n "$ANSWERS_FILE" ]; then
          [ -f "$ANSWERS_FILE" ] || die "answers file not found: $ANSWERS_FILE"
          # accepted: {"<qid>":"answer",...}  or  [{"id":"<qid>","answer":"..."}]
          ans=$(jq -c 'if type=="array" then map({key:.id, value:.answer}) | from_entries else . end' "$ANSWERS_FILE") || die "answers must be JSON: {qid: answer} or [{id, answer}]"
          missing=$(jq -r --argjson a "$ans" '.[] | select($a[.id]==null) | .id' <<<"$pend")
          [ -z "$missing" ] || die "no answer for: $(echo $missing) — every pending question needs an answer (or use --answer TEXT for one answer to all)"
          answer_mode="object"
        elif [ -n "$ANSWER_TEXT" ]; then
          answer_mode="text"
        else die "pending questions — pass --answers answers.json ({qid: answer}) or --answer TEXT (same answer to all). See $RUN/questions.json"; fi
        answer_parents=$(jq -c '[.pending_questions[]?.task as $t | .config.tasks[] | select(.id==$t) | (.map_parent // .id)] | unique' "$ST") || { log "could not identify tasks associated with pending answers"; exit 4; }
        # Commit answers, scope invalidation, and checkpoint clearing atomically. In particular,
        # do not remove the durable question file or purge reusable posts if this write fails.
        if [ "$CODEMAP" = 1 ]; then
          if ! sts --arg mode "$answer_mode" --argjson a "$ans" --arg text "$ANSWER_TEXT" --argjson ps "$answer_parents" \
            'if $mode=="object" then .answers += [.pending_questions[] | . + {answer:$a[.id]}] else .answers += [.pending_questions[] | . + {answer:$text}] end
             | (if .map_prepass_version==1 then .map_prepasses |= with_entries(. as $entry | if ($ps|index($entry.key))!=null then .value.scope_applicability="unknown after scope-changing answers" else . end) else . end)
             | .codemap_pending=null | .pending_questions=null | .status="running" | .last_votes=[] | .reuse_posts=false'; then
            log "could not persist answers; pending checkpoint and posts are preserved"; exit 4
          fi
        else
          if ! sts --arg mode "$answer_mode" --argjson a "$ans" --arg text "$ANSWER_TEXT" \
            'if $mode=="object" then .answers += [.pending_questions[] | . + {answer:$a[.id]}] else .answers += [.pending_questions[] | . + {answer:$text}] end
             | .pending_questions=null | .status="running" | .last_votes=[] | .reuse_posts=false'; then
            log "could not persist answers; pending checkpoint and posts are preserved"; exit 4
          fi
        fi
        rm -f "$RUN/questions.json"
        # the round is redone with the answers: discard the posts of that step so nobody's earlier post is reused
        case "$(st .phase)" in exec) stag="exec$(st .round)" ;; ratify) stag="x$(st .round)" ;; *) stag="r$(st .round)" ;; esac
        rm -f "$RUN"/posts/"$(st .task_id)-$stag-"*
        log "answers recorded — re-running task $(st .task_id) phase $(st .phase) round $(st .round) with the answers (round budget not consumed)$( [ "$CODEMAP" = 1 ] && echo "; a new code map attempt begins for this step" )"
        fi
        ;;
      failed|running|created)
        codemap_resume_check
        log "resuming task $(st '.task_id // "-"') phase $(st .phase) round $(st .round) — members with a valid post in that round are not re-run"
        sts '.status="running" | .last_votes=[] | .reuse_posts=true | .members |= map(.inflight=null)' ;;
      done) die "this run is finished (see $RUN/transcript.md)" ;;
      *) die "unknown status: $status" ;;
    esac
    [ -z "$CONFIRM_MAPPER_FILE$MAP_DECISION_FILE" ] || [ "$status" = questions ] || { log "confirmation/decision file supplied without a pending checkpoint"; exit 4; }
    for spec in "${REPLACE[@]:-}"; do [ -n "$spec" ] && replace_member "$spec"; done
    load_state
    render_transcript
    run_tasks ;;

  *) die "unknown command: $CMD (show|start|status|report|resume)" ;;
esac
