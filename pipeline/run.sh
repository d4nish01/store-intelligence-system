#!/usr/bin/env bash
# pipeline/run.sh
#
# Shell helper: process all stores defined in config/stores.yaml.
# Falls back to hardcoded STORE_001 and STORE_002 if stores.yaml is missing.
#
# Usage:
#   ./pipeline/run.sh
#   ./pipeline/run.sh --demo         # Run all stores in demo mode
#   ./pipeline/run.sh --speed fast   # After processing, replay at max speed

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_ROOT"

DEMO_FLAG=""
REPLAY_SPEED=1

# Parse args
while [[ $# -gt 0 ]]; do
  case $1 in
    --demo)
      DEMO_FLAG="--demo"
      shift ;;
    --speed)
      REPLAY_SPEED="$2"
      shift 2 ;;
    *)
      echo "Unknown argument: $1"
      echo "Usage: ./pipeline/run.sh [--demo] [--speed SPEED]"
      exit 1 ;;
  esac
done

echo "========================================"
echo "  Store Intelligence Pipeline"
echo "  Project root: $PROJECT_ROOT"
echo "  Demo mode: ${DEMO_FLAG:-off}"
echo "========================================"

# ── Discover stores from config/stores.yaml ───────────────────────────────────
STORES_YAML="$PROJECT_ROOT/config/stores.yaml"
STORES=()

if command -v python3 &>/dev/null && [ -f "$STORES_YAML" ]; then
  # Extract store IDs from YAML using Python
  STORE_IDS=$(python3 -c "
import yaml, sys
with open('$STORES_YAML') as f:
    data = yaml.safe_load(f)
stores = data.get('stores', {})
for sid in stores:
    print(sid)
" 2>/dev/null || echo "")
  while IFS= read -r sid; do
    [ -n "$sid" ] && STORES+=("$sid")
  done <<< "$STORE_IDS"
fi

# Fallback: hardcoded store IDs if nothing found from config
if [ ${#STORES[@]} -eq 0 ]; then
  echo "[run.sh] No stores found in config. Using defaults: STORE_001, STORE_002"
  STORES=("STORE_001" "STORE_002")
fi

echo "[run.sh] Stores to process: ${STORES[*]}"
echo ""

FAILED=()
SUCCEEDED=()

for STORE_ID in "${STORES[@]}"; do
  LOWER_ID=$(echo "$STORE_ID" | tr '[:upper:]' '[:lower:]')
  INPUT_DIR="$PROJECT_ROOT/data/raw/$LOWER_ID"
  LAYOUT="$PROJECT_ROOT/config/layouts/$STORE_ID.layout.json"
  OUTPUT="$PROJECT_ROOT/output/events/$STORE_ID.events.jsonl"

  echo "────────────────────────────────────────"
  echo "  Processing: $STORE_ID"
  echo "  Input:      $INPUT_DIR"
  echo "  Layout:     $LAYOUT"
  echo "  Output:     $OUTPUT"
  echo ""

  # Check layout exists (required even in demo mode)
  if [ ! -f "$LAYOUT" ]; then
    echo "[run.sh] WARNING: Layout file missing for $STORE_ID: $LAYOUT"
    echo "         Run: python tools/calibrate_zones.py --store-id $STORE_ID --out $LAYOUT"
    FAILED+=("$STORE_ID (no layout)")
    continue
  fi

  # Build command
  CMD="python3 pipeline/process_store.py \
    --store-id $STORE_ID \
    --input-dir $INPUT_DIR \
    --layout $LAYOUT \
    --out $OUTPUT \
    --config config/stores.yaml \
    --settings config/settings.yaml \
    $DEMO_FLAG"

  echo "[run.sh] Running: $CMD"
  if eval "$CMD"; then
    SUCCEEDED+=("$STORE_ID")
    echo "[run.sh] $STORE_ID: SUCCESS → $OUTPUT"
  else
    FAILED+=("$STORE_ID")
    echo "[run.sh] $STORE_ID: FAILED"
  fi
  echo ""
done

echo "========================================"
echo "  Run Summary"
echo "========================================"
echo "  Succeeded: ${SUCCEEDED[*]:-none}"
echo "  Failed:    ${FAILED[*]:-none}"
echo ""

# ── Optional: replay events to API ───────────────────────────────────────────
API_URL="${API_URL:-http://localhost:8000/events/ingest}"

if [ "$REPLAY_SPEED" != "0" ]; then
  echo "[run.sh] Replaying events to API at $API_URL (speed=$REPLAY_SPEED)..."
  for STORE_ID in "${SUCCEEDED[@]}"; do
    EVENTS_FILE="$PROJECT_ROOT/output/events/$STORE_ID.events.jsonl"
    if [ -f "$EVENTS_FILE" ]; then
      echo "[run.sh] Replaying $STORE_ID..."
      python3 pipeline/replay.py \
        --events "$EVENTS_FILE" \
        --api "$API_URL" \
        --speed "$REPLAY_SPEED" || echo "[run.sh] Replay failed for $STORE_ID (API may not be running)"
    fi
  done
fi

echo "[run.sh] Done."

# Exit with failure if any store failed
[ ${#FAILED[@]} -eq 0 ]
