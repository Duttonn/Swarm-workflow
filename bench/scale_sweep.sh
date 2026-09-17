#!/usr/bin/env bash
# Same brief, same judge, swarm size 1 -> 20. One run at a time: this laptop dies above that.
#
# Usage: bench/scale_sweep.sh prompts/18-secure-server.json 1 2 5 10 20
# Env:   SWARM_RUNNER / SWARM_MODEL / SWARM_WHOLE_FILE_MODEL as usual.
#        SWEEP_PREFLIGHT=0 skips the provider check, SWEEP_PREFLIGHT_TIMEOUT=seconds widens it.
set -u
cd "$(dirname "$0")/.." || exit 1
spec="$1"; shift
name=$(basename "$spec" .json)
export PATH="$PWD/tools/just:$PWD/.venv/Scripts:$PATH"
export PYTHONIOENCODING=utf-8
export SSSF_DB="$PWD/adws/adw_data/sssf.db"
export SSSF_BLUEPRINTS="$PWD/blueprints"
export SWARM_MAX_PARALLEL="${SWARM_MAX_PARALLEL:-4}"
LOGS="$PWD/work/queue-logs/sweep-$name"
mkdir -p "$LOGS"

# Preflight. A free gateway that has gone dark answers every call with exit 1 and no text, and
# the swarm then burns two 15-minute prototype timeouts per size before saying so: the
# 2026-09-14 sweep spent 2h21 producing five failures for one dead provider. One real call per
# distinct model, up front, costs a minute and names the reason.
if [ "${SWEEP_PREFLIGHT:-1}" != "0" ]; then
    for model in $(echo "${SWARM_MODEL:-} ${SWARM_WHOLE_FILE_MODEL:-}" | tr ' ' '\n' | sort -u); do
        [ -n "$model" ] || continue
        log="$LOGS/preflight-$(echo "$model" | tr '/:' '--').log"
        echo "=== preflight ${SWARM_RUNNER:?set SWARM_RUNNER} $model"
        if ! env SWARM_CHECK_TIMEOUT="${SWEEP_PREFLIGHT_TIMEOUT:-420}" python bench/check_runner.py "$SWARM_RUNNER" "$model" >"$log" 2>&1; then
            echo "=== preflight FAILED for $model, sweep aborted"
            tail -3 "$log"
            exit 2
        fi
    done
fi

for size in "$@"; do
    echo "=== size $size starting $(date +%H:%M:%S)"
    SWARM_ROSTER_SIZE="$size" tools/just/just.exe swarm "$spec" > "$LOGS/size-$size.log" 2>&1
    code=$?
    run=$(grep -ao '"run_id": "[a-z0-9]*"' "$LOGS/size-$size.log" | tail -1 | cut -d'"' -f4)
    accepted=$(grep -ao '"accepted": [a-z]*' "$LOGS/size-$size.log" | tail -1 | cut -d' ' -f2)
    echo "=== size $size done exit=$code run=${run:-none} accepted=${accepted:-unknown} $(date +%H:%M:%S)"
done
echo "=== sweep finished"
