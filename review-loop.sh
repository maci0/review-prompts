#!/bin/bash

set -uo pipefail

LOOP_COUNT=0
REVIEW_COUNT=0
FAIL_COUNT=0
FAILURES=()
STOPPING=false
SCRIPT_START=$SECONDS

declare -A TOOL_RUNS=([claude]=0 [gemini]=0 [codex]=0)
declare -A TOOL_FAILS=([claude]=0 [gemini]=0 [codex]=0)
declare -A TOOL_TIMEOUTS=([claude]=0 [gemini]=0 [codex]=0)

CURRENT_PID=""
INTERRUPT_COUNT=0

trap 'handle_sigint' INT TERM

handle_sigint() {
  ((INTERRUPT_COUNT++))
  STOPPING=true
  echo ""
  if [[ -n "$CURRENT_PID" ]] && kill -0 "$CURRENT_PID" 2>/dev/null; then
    if (( INTERRUPT_COUNT == 1 )); then
      log "Signal received — terminating current review (pid $CURRENT_PID). Ctrl+C again for KILL."
      kill -TERM "$CURRENT_PID" 2>/dev/null
      pkill -TERM -P "$CURRENT_PID" 2>/dev/null
    else
      log "Force-killing pid $CURRENT_PID..."
      kill -KILL "$CURRENT_PID" 2>/dev/null
      pkill -KILL -P "$CURRENT_PID" 2>/dev/null
    fi
  else
    log "Signal received — stopping..."
  fi
}

log() {
  printf "[%s] %s\n" "$(date +%H:%M:%S)" "$*"
}

usage() {
  cat <<'EOF'
Usage: review-loop.sh <claude|gemini|codex|mixed> [options]

Options:
  --dir DIR             cd into DIR before running (default: cwd)
  --once                run a single loop and exit
  --max-loops N         stop after N loops (integer)
  --timeout DUR         per-review timeout, e.g. 30m, 1h (default: 30m)
  --log FILE            tee output to FILE
  --prompt-dir DIR      directory of *-review.md files (default: ~/prompts)
  --reviews LIST        comma-separated subset to run (e.g. code-review,sec-review)
  --exclude LIST        comma-separated reviews to skip
  --dry-run             print planned schedule and exit
  -h, --help            show this help
EOF
  exit 1
}

TOOL="${1:-}"
[[ -z "$TOOL" ]] && usage
shift

WORK_DIR=""
ONCE=false
MAX_LOOPS=0
PER_REVIEW_TIMEOUT="30m"
LOG_FILE=""
PROMPT_DIR="$HOME/prompts"
INCLUDE_LIST=""
EXCLUDE_LIST=""
DRY_RUN=false

while (( $# > 0 )); do
  case "$1" in
    --dir)         WORK_DIR="$2"; shift 2 ;;
    --once)        ONCE=true; shift ;;
    --max-loops)   MAX_LOOPS="$2"; shift 2 ;;
    --timeout)     PER_REVIEW_TIMEOUT="$2"; shift 2 ;;
    --log)         LOG_FILE="$2"; shift 2 ;;
    --prompt-dir)  PROMPT_DIR="$2"; shift 2 ;;
    --reviews)     INCLUDE_LIST="$2"; shift 2 ;;
    --exclude)     EXCLUDE_LIST="$2"; shift 2 ;;
    --dry-run)     DRY_RUN=true; shift ;;
    -h|--help)     usage ;;
    *)             echo "Unknown arg: $1"; usage ;;
  esac
done

if [[ "$TOOL" != "claude" && "$TOOL" != "gemini" && "$TOOL" != "codex" && "$TOOL" != "mixed" ]]; then
  usage
fi

if ! [[ "$MAX_LOOPS" =~ ^[0-9]+$ ]]; then
  echo "Invalid --max-loops: $MAX_LOOPS (must be integer)"
  exit 1
fi

if ! [[ "$PER_REVIEW_TIMEOUT" =~ ^[0-9]+[smhd]?$ ]]; then
  echo "Invalid --timeout: $PER_REVIEW_TIMEOUT (e.g. 30m, 1h, 90s)"
  exit 1
fi

check_tool() {
  command -v "$1" >/dev/null 2>&1 || { echo "Required tool not found in PATH: $1"; exit 1; }
}

case "$TOOL" in
  claude|gemini|codex) check_tool "$TOOL" ;;
  mixed)               check_tool claude; check_tool gemini; check_tool codex ;;
esac

check_tool timeout
check_tool flock

if [[ -n "$WORK_DIR" ]]; then
  cd "$WORK_DIR" || { echo "Cannot cd to $WORK_DIR"; exit 1; }
fi

if [[ ! -d "$PROMPT_DIR" ]]; then
  echo "Prompt directory not found: $PROMPT_DIR"
  exit 1
fi

LOCK_FILE="$PWD/.review-loop.lock"
exec 200>"$LOCK_FILE"
if ! flock -n 200; then
  echo "Another review-loop appears to be running in $PWD (lock: $LOCK_FILE)"
  exit 1
fi

if [[ -n "$LOG_FILE" ]]; then
  exec > >(tee -a "$LOG_FILE") 2>&1
fi

REVIEWS=(
  code-review
  doc-review
  cli-review
  perf-review
  arch-review
  sec-review
  test-review
  ux-review
  api-review
  infra-review
  o11y-review
  deps-review
  db-review
)

validate_filter() {
  local arg_name="$1"
  local list="$2"
  [[ -z "$list" ]] && return 0
  local IFS=','
  read -ra items <<< "$list"
  for item in "${items[@]}"; do
    local found=false
    for r in "${REVIEWS[@]}"; do
      if [[ "$r" == "$item" ]]; then found=true; break; fi
    done
    if ! $found; then
      echo "Unknown review in $arg_name: '$item'"
      echo "Valid reviews: ${REVIEWS[*]}"
      exit 1
    fi
  done
}

validate_filter "--reviews" "$INCLUDE_LIST"
validate_filter "--exclude" "$EXCLUDE_LIST"

apply_filters() {
  local result=()
  for r in "${REVIEWS[@]}"; do
    if [[ -n "$INCLUDE_LIST" ]] && [[ ",$INCLUDE_LIST," != *",$r,"* ]]; then
      continue
    fi
    if [[ -n "$EXCLUDE_LIST" ]] && [[ ",$EXCLUDE_LIST," == *",$r,"* ]]; then
      continue
    fi
    result+=("$r")
  done
  REVIEWS=("${result[@]}")
}

apply_filters

if (( ${#REVIEWS[@]} == 0 )); then
  echo "No reviews remain after filtering."
  exit 1
fi

PROMPT_HEADER="MODE: AUTO_FIX — apply fixes directly to files. Do NOT write a report."

PROMPT_SUFFIX="

---

OVERRIDE: Ignore any 'Output format', 'Executive Summary', or report-style sections in the instructions above. Do NOT write a report.

Operating mode: apply fixes directly to files, autonomously.

Rules:
- First check if this review makes sense for this codebase. If not, exit immediately without changes.
- Make the smallest reversible diff possible.
- Fix at most ~10 highest-value issues this pass. Stop after that.
- Do not delete tests.
- Do not change public APIs or exported symbols.
- Skip anything uncertain. Never ask questions.
- Never modify files in: node_modules, vendor, dist, build, .next, target, .git, generated/auto-generated files, lockfiles (package-lock.json, yarn.lock, pnpm-lock.yaml, Cargo.lock, go.sum, etc.).
- If a single fix would change more than 300 lines in one file, skip it.
- Do not commit anything to git. Do not run git commit, git push, or git tag.
- After edits, if the project has a lint, typecheck, or test command configured (package.json scripts, Makefile, justfile, pyproject.toml, etc.), run the relevant one. If it fails due to your changes, revert them.
- At the end, print a brief 1-line summary per changed file in the form: 'PATH — what was done'. If nothing changed, print 'No changes.'"

fmt_duration() {
  local secs=$1
  printf "%dm%02ds" $((secs / 60)) $((secs % 60))
}

pick_tool() {
  if [[ "$TOOL" == "mixed" ]]; then
    local tools=(claude gemini codex)
    echo "${tools[$(( RANDOM % 3 ))]}"
  else
    echo "$TOOL"
  fi
}

shuffle_array() {
  local i j n temp
  n=${#REVIEWS[@]}
  for (( i = n - 1; i > 0; i-- )); do
    j=$(( RANDOM % (i + 1) ))
    temp="${REVIEWS[$i]}"
    REVIEWS[$i]="${REVIEWS[$j]}"
    REVIEWS[$j]="$temp"
  done
}

run_review() {
  local review="$1"
  local tool
  tool=$(pick_tool)
  local prompt_file="${PROMPT_DIR}/${review}.md"

  if [[ ! -f "$prompt_file" ]]; then
    log "Missing prompt file: $prompt_file — skipping"
    return 0
  fi

  local prompt
  prompt="${PROMPT_HEADER}

$(cat "$prompt_file")${PROMPT_SUFFIX}"

  local start=$SECONDS
  log "Running ${review} with ${tool} (timeout ${PER_REVIEW_TIMEOUT})"
  ((TOOL_RUNS[$tool]++))

  local cmd=()
  case "$tool" in
    claude) cmd=(timeout --foreground "$PER_REVIEW_TIMEOUT" claude --dangerously-skip-permissions -p "$prompt") ;;
    gemini) cmd=(timeout --foreground "$PER_REVIEW_TIMEOUT" gemini -y -p "$prompt") ;;
    codex)  cmd=(timeout --foreground "$PER_REVIEW_TIMEOUT" codex exec --dangerously-bypass-approvals-and-sandbox "$prompt") ;;
  esac

  "${cmd[@]}" &
  CURRENT_PID=$!
  local rc=0
  while kill -0 "$CURRENT_PID" 2>/dev/null; do
    wait "$CURRENT_PID" 2>/dev/null
    rc=$?
  done
  CURRENT_PID=""
  INTERRUPT_COUNT=0

  local elapsed=$(( SECONDS - start ))

  if (( rc == 130 || rc == 143 )); then
    log "Interrupted: ${review} (${tool}) after $(fmt_duration $elapsed)"
    STOPPING=true
  elif (( rc == 124 )); then
    log "TIMEOUT: ${review} (${tool}) after ${PER_REVIEW_TIMEOUT}"
    ((FAIL_COUNT++))
    ((TOOL_TIMEOUTS[$tool]++))
    FAILURES+=("loop $((LOOP_COUNT+1)): ${review} (${tool}) — timeout")
  elif (( rc != 0 )); then
    log "FAILED: ${review} (${tool}) after $(fmt_duration $elapsed) — exit code ${rc}"
    ((FAIL_COUNT++))
    ((TOOL_FAILS[$tool]++))
    FAILURES+=("loop $((LOOP_COUNT+1)): ${review} (${tool}) — exit ${rc}")
  else
    log "Done: ${review} (${tool}) in $(fmt_duration $elapsed)"
  fi
}

print_stats() {
  local total_elapsed=$(( SECONDS - SCRIPT_START ))
  echo ""
  echo "=== Review loop stopped ==="
  echo "Completed loops: ${LOOP_COUNT}"
  echo "Total reviews run: ${REVIEW_COUNT}"
  echo "Failures: ${FAIL_COUNT}"
  echo "Total time: $(fmt_duration $total_elapsed)"

  if [[ "$TOOL" == "mixed" ]]; then
    echo ""
    echo "Per-tool stats:"
    for t in claude gemini codex; do
      printf "  %-7s runs=%d  fails=%d  timeouts=%d\n" \
        "$t" "${TOOL_RUNS[$t]}" "${TOOL_FAILS[$t]}" "${TOOL_TIMEOUTS[$t]}"
    done
  fi

  if (( ${#FAILURES[@]} > 0 )); then
    echo ""
    echo "Failed reviews:"
    for f in "${FAILURES[@]}"; do
      echo "  - $f"
    done
  fi
}

if $DRY_RUN; then
  echo "DRY RUN — planned schedule for one loop:"
  shuffle_array
  for review in "${REVIEWS[@]}"; do
    tool=$(pick_tool)
    printf "  %-15s → %s\n" "$review" "$tool"
  done
  echo ""
  echo "Reviews per loop: ${#REVIEWS[@]}"
  echo "Tool: $TOOL  |  timeout: $PER_REVIEW_TIMEOUT  |  prompt-dir: $PROMPT_DIR"
  echo "Loop limit: $( $ONCE && echo 1 || (( MAX_LOOPS > 0 )) && echo "$MAX_LOOPS" || echo "infinite")"
  exit 0
fi

while true; do
  shuffle_array
  loop_start=$SECONDS
  for review in "${REVIEWS[@]}"; do
    if $STOPPING; then
      print_stats
      exit 0
    fi
    run_review "$review"
    ((REVIEW_COUNT++))
  done
  ((LOOP_COUNT++))
  loop_elapsed=$(( SECONDS - loop_start ))
  echo ""
  log "=== Loop ${LOOP_COUNT} complete in $(fmt_duration $loop_elapsed) (${REVIEW_COUNT} reviews, ${FAIL_COUNT} failures) ==="
  echo ""

  if $ONCE; then break; fi
  if (( MAX_LOOPS > 0 && LOOP_COUNT >= MAX_LOOPS )); then break; fi
done

print_stats
