#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# live-matrix-meshtastic-smoke.sh
#
# Optional operator convenience script for capturing live smoke-test artifacts.
# NOT a medre subcommand — run this manually when performing live integration
# tests against a real Matrix + Meshtastic environment.
#
# What it does:
#   1. Runs `medre run` with snapshot-on-shutdown, capturing all output.
#   2. Collects receipts and evidence JSON after the run finishes.
#   3. Produces a redacted copy of the config (access tokens masked).
#   4. Prints a summary of every artifact captured.
#
# Idempotent: safe to re-run; existing output files are overwritten.
#
# Usage:
#   ./scripts/live-matrix-meshtastic-smoke.sh --config ./my-config.toml
#   ./scripts/live-matrix-meshtastic-smoke.sh --config ./cfg.toml --output-dir /tmp/smoke-run-2
#   ./scripts/live-matrix-meshtastic-smoke.sh --config ./cfg.toml --storage-path /data/medre.db
# ---------------------------------------------------------------------------

set -euo pipefail

# ---- Defaults --------------------------------------------------------------
OUTPUT_DIR="/tmp/medre-live-smoke"
CONFIG=""
STORAGE_PATH=""
EVENT_ID=""

# ---- Usage -----------------------------------------------------------------
usage() {
  cat <<'EOF'
Usage: live-matrix-meshtastic-smoke.sh [OPTIONS]

Capture live smoke-test artifacts from a medre run.

Options:
  --config       PATH    (required) Path to medre TOML config file.
  --output-dir   PATH    Directory for captured artifacts.
                          Default: /tmp/medre-live-smoke
  --storage-path PATH    Database path for `medre inspect receipts`.
                          If omitted, the script attempts to read
                          path from the [storage] section of the config file.
  --event-id     ID      Run `medre inspect event` after the run completes.
                          Requires a valid --storage-path (or config-derived
                          path). Output is saved to inspect-event.txt.
  --help                 Show this help message and exit.

Artifacts produced in OUTPUT_DIR:
  medre.log          Full stderr+stdout from the medre run.
  snapshot.json      Database snapshot written on shutdown.
  receipts.json      Receipts dump from the storage backend.
  evidence.json      Evidence bundle in JSON format.
  config.redacted    Copy of config with access_token masked.
  inspect-event.txt  (if --event-id is provided) Full inspect-first output.
EOF
}

# ---- Argument parsing ------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)
      CONFIG="$2"
      shift 2
      ;;
    --output-dir)
      OUTPUT_DIR="$2"
      shift 2
      ;;
    --storage-path)
      STORAGE_PATH="$2"
      shift 2
      ;;
    --event-id)
      EVENT_ID="$2"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: Unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

# ---- Validate required args ------------------------------------------------
if [[ -z "$CONFIG" ]]; then
  echo "ERROR: --config is required." >&2
  usage >&2
  exit 1
fi

if [[ ! -f "$CONFIG" ]]; then
  echo "ERROR: Config file not found: $CONFIG" >&2
  exit 1
fi

# ---- Attempt to extract storage path from config if not provided -----------
# Parses the TOML [storage] section to find `path = "..."`.
# Falls back to a flat `storage_path = "..."` key for legacy configs.
if [[ -z "$STORAGE_PATH" ]]; then
  in_storage_section=false
  while IFS= read -r line; do
    # Detect section headers: [storage] or [storage.xxx]
    if [[ "$line" =~ ^[[:space:]]*\[storage\] ]]; then
      in_storage_section=true
      continue
    fi
    # Any other section header exits the [storage] scope
    if [[ "$line" =~ ^[[:space:]]*\[ ]]; then
      if $in_storage_section; then
        in_storage_section=false
      fi
      continue
    fi
    # Inside [storage]: look for path = "..." or path = ...
    if $in_storage_section && [[ "$line" =~ ^[[:space:]]*path[[:space:]]*= ]]; then
      # Extract value after '=', strip quotes and surrounding whitespace
      raw_val="${line#*=}"
      raw_val="${raw_val#"${raw_val%%[![:space:]]*}"}"  # trim leading whitespace
      raw_val="${raw_val%"${raw_val##*[![:space:]]}"}"  # trim trailing whitespace
      raw_val="${raw_val#\"}"   # strip leading double quote
      raw_val="${raw_val%\"}"   # strip trailing double quote
      raw_val="${raw_val#\'}"   # strip leading single quote
      raw_val="${raw_val%\'}"   # strip trailing single quote
      # Check for XDG placeholder {state}
      if [[ "$raw_val" == *'{state}'* ]]; then
        echo "WARNING: Config [storage] path contains XDG placeholder '{state}':" >&2
        echo "         $raw_val" >&2
        echo "         This requires Python-level resolution. Use --storage-path to" >&2
        echo "         provide the resolved database path explicitly." >&2
        raw_val=""
      fi
      STORAGE_PATH="$raw_val"
      break
    fi
  done < "$CONFIG"

  # Fallback: try legacy flat key  storage_path = "..."
  if [[ -z "$STORAGE_PATH" ]]; then
    STORAGE_PATH=$(grep -E '^\s*storage_path\s*=' "$CONFIG" 2>/dev/null \
      | awk -F'=' '{for(i=2;i<=NF;i++){gsub(/["'"'"' ]/,"",$i); if($i!=""){print $i; break}}}' \
      || true)
  fi

  # Validate: if we got a path, check the file exists (warn but don't fail)
  if [[ -n "$STORAGE_PATH" && ! -f "$STORAGE_PATH" ]]; then
    echo "WARNING: Resolved storage path does not exist: $STORAGE_PATH" >&2
    echo "         Receipt and evidence collection may fail." >&2
  fi
fi

# ---- Create output directory (idempotent) ----------------------------------
mkdir -p "$OUTPUT_DIR"

echo "=== medre live smoke capture ==="
echo "Config:      $CONFIG"
echo "Output dir:  $OUTPUT_DIR"
if [[ -n "$STORAGE_PATH" ]]; then
  echo "Storage DB:  $STORAGE_PATH"
else
  echo "Storage DB:  (not specified — receipts collection will be skipped)"
fi
echo ""

# ---- Step 1: Run medre with snapshot-on-shutdown ---------------------------
echo "--- Running medre (output logged to $OUTPUT_DIR/medre.log) ---"
echo "    Press Ctrl+C to trigger shutdown snapshot."
echo ""

medre run \
  --config "$CONFIG" \
  --snapshot-on-shutdown "$OUTPUT_DIR/snapshot.json" \
  2>&1 | tee "$OUTPUT_DIR/medre.log" || true

echo ""
echo "--- medre run exited. Collecting artifacts... ---"

# ---- Step 2: Collect receipts (requires storage path) ----------------------
if [[ -n "$STORAGE_PATH" ]]; then
  echo "  -> receipts.json"
  medre inspect receipts \
    --storage-path "$STORAGE_PATH" \
    > "$OUTPUT_DIR/receipts.json" 2>/dev/null \
    || echo "     WARNING: receipts collection failed (db may not exist yet)."
else
  echo "  -> receipts.json  SKIPPED (no --storage-path and none found in config)"
fi

# ---- Step 3: Collect evidence bundle ---------------------------------------
echo "  -> evidence.json"
medre evidence \
  --config "$CONFIG" \
  --json \
  > "$OUTPUT_DIR/evidence.json" 2>/dev/null \
  || echo "     WARNING: evidence collection failed."

# ---- Step 4: Inspect-first investigation (if --event-id provided) ----------
if [[ -n "$EVENT_ID" ]]; then
  if [[ -n "$STORAGE_PATH" ]]; then
    echo "  -> inspect-event.txt  (event: $EVENT_ID)"
    medre inspect event "$EVENT_ID" \
      --storage-path "$STORAGE_PATH" \
      --timeline --evidence --recovery \
      > "$OUTPUT_DIR/inspect-event.txt" 2>&1 \
      || echo "     WARNING: inspect event failed (event may not exist or db unavailable)."
  else
    echo "  -> inspect-event.txt  SKIPPED (--event-id provided but no storage path available)"
  fi
else
  echo "  -> inspect-event.txt  SKIPPED (no --event-id provided)"
  echo "     Tip: pass --event-id <ID> to run full inspect-first investigation after the run."
fi

# ---- Step 5: Redacted config copy ------------------------------------------
# Replace any access_token value with "***" to avoid leaking secrets.
# Matches TOML pattern:  access_token = "any-token-here"
echo "  -> config.redacted"
sed -E 's/(access_token\s*=\s*").*"/\1***"/' "$CONFIG" > "$OUTPUT_DIR/config.redacted"

# ---- Step 6: Summary -------------------------------------------------------
echo ""
echo "=== Artifact summary ==="

for artifact in medre.log snapshot.json receipts.json evidence.json config.redacted inspect-event.txt; do
  path="$OUTPUT_DIR/$artifact"
  if [[ -f "$path" ]]; then
    size=$(stat --format='%s' "$path" 2>/dev/null || stat -f '%z' "$path" 2>/dev/null || echo "?")
    echo "  [OK]   $artifact  ($size bytes)"
  else
    echo "  [MISS] $artifact"
  fi
done

echo ""
echo "=== Done. Artifacts are in $OUTPUT_DIR ==="
