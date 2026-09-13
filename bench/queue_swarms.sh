#!/usr/bin/env bash
# Run swarms one after another, never two at once.
#
# Measured the hard way: two 20-agent swarms sharing one API key hit the
# "generativelanguage.googleapis.com/generate_content_paid_tier_input_token_count, limit: 2000000"
# per-minute quota and killed 5 of 10 lost agents. One at a time costs wall-clock, not agents.
#
# Usage: bench/queue_swarms.sh prompts/07-solar-system.json prompts/08-chess-opening.json ...
set -u
cd "$(dirname "$0")/.." || exit 1
export PATH="$PWD/tools/just:$PWD/.venv/Scripts:$PATH"
export PYTHONIOENCODING=utf-8
export SSSF_DB="$PWD/adws/adw_data/sssf.db"
export SSSF_BLUEPRINTS="$PWD/blueprints"
LOGS="${SWARM_QUEUE_LOGS:-$PWD/work/queue-logs}"
mkdir -p "$LOGS"

for spec in "$@"; do
    name=$(basename "$spec" .json)
    echo "=== $name starting $(date +%H:%M:%S)"
    tools/just/just.exe swarm "$spec" > "$LOGS/$name.log" 2>&1
    code=$?
    run=$(grep -ao '"run_id": "[a-z0-9]*"' "$LOGS/$name.log" | tail -1 | cut -d'"' -f4)
    accepted=$(grep -ao '"accepted": [a-z]*' "$LOGS/$name.log" | tail -1 | cut -d' ' -f2)
    echo "=== $name done exit=$code run=${run:-none} accepted=${accepted:-unknown} $(date +%H:%M:%S)"
done
echo "=== queue finished"
