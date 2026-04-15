#!/bin/bash
# ssbatch - Smart sbatch: submit a job and automatically tail its output
# Usage: ssbatch [sbatch-args...] script.sh [script-args...]

set -euo pipefail

POLL_INTERVAL_MIN=2    # initial seconds between squeue checks
POLL_INTERVAL_MAX=30   # cap on backed-off poll interval (HPC etiquette)
FILE_POLL_INTERVAL=2   # seconds between output file existence checks
TAIL_RETRY_DELAY=3     # seconds to wait before retrying tail after failure
TAIL_MAX_RETRIES=10    # max times to retry tail -f if it fails

# --- Colors (if terminal supports it) ---
if [[ -t 1 ]]; then
    RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
    CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'
else
    RED=''; GREEN=''; YELLOW=''; CYAN=''; BOLD=''; RESET=''
fi

info()  { echo -e "${CYAN}[ssbatch]${RESET} $*"; }
warn()  { echo -e "${YELLOW}[ssbatch]${RESET} $*"; }
error() { echo -e "${RED}[ssbatch]${RESET} $*" >&2; }
ok()    { echo -e "${GREEN}[ssbatch]${RESET} $*"; }

# --- Argument check ---
if [[ $# -lt 1 ]]; then
    echo "Usage: ssbatch [sbatch-args...] script.sh [script-args...]"
    echo "Submits a SLURM job and automatically tails its output."
    exit 1
fi

# --- Find the script file among arguments (for output file detection) ---
# We pass everything to sbatch as-is, but also try to find which arg is the
# script so we can parse #SBATCH directives from it.
SCRIPT_FILE=""
for arg in "$@"; do
    if [[ -f "$arg" && "$arg" != -* ]]; then
        SCRIPT_FILE="$arg"
        break
    fi
done

# --- Resolve expected output file from script's #SBATCH directives ---
resolve_output_file() {
    local job_id="$1"
    local outfile=""

    # Check command-line args first: -o or --output
    local prev=""
    for arg in "$@"; do
        shift  # we won't use positional args after this
        if [[ "$prev" == "-o" || "$prev" == "--output" ]]; then
            outfile="$arg"
            break
        fi
        case "$arg" in
            --output=*) outfile="${arg#--output=}"; break ;;
            -o=*)       outfile="${arg#-o=}"; break ;;
        esac
        prev="$arg"
    done

    # If not on command line, check the script file
    if [[ -z "$outfile" && -n "$SCRIPT_FILE" ]]; then
        # Match #SBATCH --output=... or #SBATCH -o ...
        outfile=$(grep -E '^\s*#SBATCH\s+(--output[= ]|-o[= ])' "$SCRIPT_FILE" 2>/dev/null \
            | tail -1 \
            | sed -E 's/^\s*#SBATCH\s+(--output[= ]|-o[= ])\s*//' \
            | sed -E 's/\s*$//')
    fi

    # Default if nothing found
    if [[ -z "$outfile" ]]; then
        outfile="slurm-%j.out"
    fi

    # Replace SLURM filename patterns with actual values
    outfile="${outfile//%j/$job_id}"
    outfile="${outfile//%J/$job_id}"
    # %x = job name - try to resolve it
    if [[ "$outfile" == *%x* ]]; then
        local jobname
        jobname=$(squeue -j "$job_id" -h -o "%j" 2>/dev/null || echo "")
        if [[ -n "$jobname" ]]; then
            outfile="${outfile//%x/$jobname}"
        fi
    fi

    echo "$outfile"
}

# --- Submit the job ---
info "Submitting: sbatch $*"

SBATCH_OUTPUT=$(sbatch "$@" 2>&1) || {
    error "sbatch failed with exit code $?"
    error "Output: $SBATCH_OUTPUT"
    exit 1
}

# --- Parse job ID ---
JOB_ID=$(echo "$SBATCH_OUTPUT" | grep -oP 'Submitted batch job \K[0-9]+' || true)

if [[ -z "$JOB_ID" ]]; then
    error "Could not parse job ID from sbatch output:"
    error "$SBATCH_OUTPUT"
    exit 1
fi

ok "Submitted batch job ${BOLD}$JOB_ID${RESET}"

# --- Resolve output file path ---
OUTPUT_FILE=$(resolve_output_file "$JOB_ID" "$@")
info "Expected output file: ${BOLD}$OUTPUT_FILE${RESET}"

# --- Wait for job to start (or finish) and output file to appear ---
wait_for_output_file() {
    local elapsed=0
    local poll_interval=$POLL_INTERVAL_MIN

    while [[ ! -e "$OUTPUT_FILE" ]]; do
        # Check if the job still exists / hasn't failed before producing output
        local state
        state=$(squeue -j "$JOB_ID" -h -o "%T" 2>/dev/null || echo "")

        if [[ -z "$state" ]]; then
            # Job no longer in squeue — check sacct for final state
            local final_state
            final_state=$(sacct -j "$JOB_ID" -n -o State -X 2>/dev/null \
                | head -1 | tr -d ' ' || echo "UNKNOWN")

            if [[ ! -e "$OUTPUT_FILE" ]]; then
                # Give the filesystem a moment to catch up
                sleep "$FILE_POLL_INTERVAL"
                if [[ ! -e "$OUTPUT_FILE" ]]; then
                    warn "Job finished (state: $final_state) but output file not found."
                    warn "Check: sacct -j $JOB_ID --format=JobID,State,ExitCode,Elapsed"
                    exit 1
                fi
            fi
            break
        fi

        case "$state" in
            PENDING)
                info "Job $JOB_ID is PENDING... (${elapsed}s elapsed)" ;;
            CONFIGURING|REQUEUED)
                info "Job $JOB_ID is $state... (${elapsed}s elapsed)" ;;
            RUNNING)
                info "Job $JOB_ID is RUNNING, waiting for output file..." ;;
            *)
                info "Job $JOB_ID state: $state (${elapsed}s elapsed)" ;;
        esac

        sleep "$poll_interval"
        elapsed=$((elapsed + poll_interval))
        # Exponential backoff to be polite to the scheduler — no overall timeout.
        if (( poll_interval < POLL_INTERVAL_MAX )); then
            poll_interval=$((poll_interval * 2))
            (( poll_interval > POLL_INTERVAL_MAX )) && poll_interval=$POLL_INTERVAL_MAX
        fi
    done
}

info "Waiting for output file to appear..."
wait_for_output_file

ok "Output file found: $OUTPUT_FILE"

# --- Tail with retries (filesystem can be slow) ---
tail_with_retries() {
    local retries=0

    while (( retries < TAIL_MAX_RETRIES )); do
        # Brief pause to let the filesystem settle
        if (( retries > 0 )); then
            warn "tail failed (attempt $retries/$TAIL_MAX_RETRIES), retrying in ${TAIL_RETRY_DELAY}s..."
            sleep "$TAIL_RETRY_DELAY"
        fi

        # Verify the file still exists before tailing
        if [[ ! -e "$OUTPUT_FILE" ]]; then
            warn "Output file disappeared — filesystem hiccup? Waiting..."
            sleep "$TAIL_RETRY_DELAY"
            retries=$((retries + 1))
            continue
        fi

        ok "Tailing output (Ctrl+C to stop):"
        echo "─────────────────────────────────────────────"

        # tail -f: --retry handles the file being temporarily inaccessible
        # +1 = start from the beginning of the file
        if tail --retry -f -n +1 "$OUTPUT_FILE" 2>/dev/null; then
            # tail exited cleanly (file was removed / renamed)
            break
        elif tail -f -n +1 "$OUTPUT_FILE" 2>/dev/null; then
            # fallback without --retry (not all systems support it)
            break
        else
            retries=$((retries + 1))
        fi
    done

    if (( retries >= TAIL_MAX_RETRIES )); then
        error "Failed to tail output file after $TAIL_MAX_RETRIES attempts."
        warn "You can try manually: tail -f $OUTPUT_FILE"
        exit 1
    fi
}

tail_with_retries
