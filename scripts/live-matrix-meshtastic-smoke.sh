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
#   2. Collects inspect-first JSON artifacts after the run finishes.
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
  --storage-path PATH    Database path for medre inspect commands.
                          If omitted, the script attempts to read
                          path from the [storage] section of the config file.
  --event-id     ID      Write four separate inspect-*.json artifacts after
                          the run completes: inspect-event.json,
                          inspect-timeline.json, inspect-evidence.json,
                          inspect-recovery.json. Requires a valid
                          --storage-path (or config-derived path).
  --help                 Show this help message and exit.

Artifacts produced in OUTPUT_DIR:
  medre.log              Full stderr+stdout from the medre run.
  snapshot.json          Database snapshot written on shutdown.
  config.redacted        Copy of config with access_token masked.
  inspect-event.json     (if --event-id) Event details from medre inspect.
  inspect-timeline.json  (if --event-id) Timeline view from medre inspect.
  inspect-evidence.json  (if --event-id) Evidence bundle from medre inspect.
  inspect-recovery.json  (if --event-id) Recovery info from medre inspect.
  receipts.json          (if no --event-id) Receipts dump from storage backend.
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
	--help | -h)
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
if [[ -z ${CONFIG} ]]; then
	echo "ERROR: --config is required." >&2
	usage >&2
	exit 1
fi

if [[ ! -f ${CONFIG} ]]; then
	echo "ERROR: Config file not found: ${CONFIG}" >&2
	exit 1
fi

# ---- Attempt to extract storage path from config if not provided -----------
# Parses the TOML [storage] section to find `path = "..."`.
if [[ -z ${STORAGE_PATH} ]]; then
	in_storage_section=false
	while IFS= read -r line; do
		# Detect section headers: [storage] or [storage.xxx]
		if [[ ${line} =~ ^[[:space:]]*\[storage\] ]]; then
			in_storage_section=true
			continue
		fi
		# Any other section header exits the [storage] scope
		if [[ ${line} =~ ^[[:space:]]*\[ ]]; then
			if ${in_storage_section}; then
				in_storage_section=false
			fi
			continue
		fi
		# Inside [storage]: look for path = "..." or path = ...
		if ${in_storage_section} && [[ ${line} =~ ^[[:space:]]*path[[:space:]]*= ]]; then
			# Extract value after '=', strip quotes and surrounding whitespace
			raw_val="${line#*=}"
			raw_val="${raw_val#"${raw_val%%[![:space:]]*}"}" # trim leading whitespace
			raw_val="${raw_val%"${raw_val##*[![:space:]]}"}" # trim trailing whitespace
			raw_val="${raw_val#\"}"                          # strip leading double quote
			raw_val="${raw_val%\"}"                          # strip trailing double quote
			raw_val="${raw_val#\'}"                          # strip leading single quote
			raw_val="${raw_val%\'}"                          # strip trailing single quote
			# Check for XDG placeholder {state}
			if [[ ${raw_val} == *'{state}'* ]]; then
				echo "ERROR: Config [storage] path contains unresolved XDG placeholder '{state}':" >&2
				echo "       ${raw_val}" >&2
				echo "       This requires Python-level resolution. Pass --storage-path to provide" >&2
				echo "       the resolved database path explicitly." >&2
				exit 1
			fi
			STORAGE_PATH="${raw_val}"
			break
		fi
	done <"${CONFIG}"

	# Validate: if we got a path, check the file exists (warn but don't fail)
	if [[ -n ${STORAGE_PATH} && ! -f ${STORAGE_PATH} ]]; then
		echo "WARNING: Resolved storage path does not exist: ${STORAGE_PATH}" >&2
		echo "         Inspect-first collection may fail." >&2
	fi
fi

# ---- Create output directory (idempotent) ----------------------------------
mkdir -p "${OUTPUT_DIR}"

echo "=== medre live smoke capture ==="
echo "Config:      ${CONFIG}"
echo "Output dir:  ${OUTPUT_DIR}"
if [[ -n ${STORAGE_PATH} ]]; then
	echo "Storage DB:  ${STORAGE_PATH}"
else
	echo "Storage DB:  (not specified — receipts collection will be skipped)"
fi
echo ""

# ---- Step 1: Run medre with snapshot-on-shutdown ---------------------------
echo "--- Running medre (output logged to ${OUTPUT_DIR}/medre.log) ---"
echo "    Press Ctrl+C to trigger shutdown snapshot."
echo ""

medre run \
	--config "${CONFIG}" \
	--snapshot-on-shutdown "${OUTPUT_DIR}/snapshot.json" \
	2>&1 | tee "${OUTPUT_DIR}/medre.log" || true

echo ""
echo "--- medre run exited. Collecting artifacts... ---"

# ---- Step 2: Inspect-first workflow ----------------------------------------
if [[ -n ${EVENT_ID} && -n ${STORAGE_PATH} ]]; then
	echo "  -> inspect-event.json  (event: ${EVENT_ID})"
	medre inspect event "${EVENT_ID}" \
		--storage-path "${STORAGE_PATH}" \
		>"${OUTPUT_DIR}/inspect-event.json" 2>/dev/null ||
		echo "     WARNING: inspect event failed (event may not exist or db unavailable)."

	echo "  -> inspect-timeline.json  (event: ${EVENT_ID})"
	medre inspect event "${EVENT_ID}" \
		--storage-path "${STORAGE_PATH}" \
		--timeline \
		>"${OUTPUT_DIR}/inspect-timeline.json" 2>/dev/null ||
		echo "     WARNING: inspect timeline failed."

	echo "  -> inspect-evidence.json  (event: ${EVENT_ID})"
	medre inspect event "${EVENT_ID}" \
		--storage-path "${STORAGE_PATH}" \
		--evidence \
		>"${OUTPUT_DIR}/inspect-evidence.json" 2>/dev/null ||
		echo "     WARNING: inspect evidence failed."

	echo "  -> inspect-recovery.json  (event: ${EVENT_ID})"
	medre inspect event "${EVENT_ID}" \
		--storage-path "${STORAGE_PATH}" \
		--recovery \
		>"${OUTPUT_DIR}/inspect-recovery.json" 2>/dev/null ||
		echo "     WARNING: inspect recovery failed."
elif [[ -z ${EVENT_ID} && -n ${STORAGE_PATH} ]]; then
	echo "  -> receipts.json"
	medre inspect receipts \
		--storage-path "${STORAGE_PATH}" \
		>"${OUTPUT_DIR}/receipts.json" 2>/dev/null ||
		echo "     WARNING: receipts collection failed (db may not exist yet)."
	echo "     Tip: re-run with --event-id <ID> for full inspect-first investigation."
else
	echo "  -> receipts.json  SKIPPED (no --storage-path and none found in config)"
	echo "     Tip: re-run with --storage-path <PATH> and --event-id <ID> for full inspect-first investigation."
fi

# ---- Step 4: Redacted config copy ------------------------------------------
# Replace any access_token value with "***" to avoid leaking secrets.
# Matches TOML pattern:  access_token = "any-token-here"
echo "  -> config.redacted"
sed -E 's/(access_token\s*=\s*").*"/\1***"/' "${CONFIG}" >"${OUTPUT_DIR}/config.redacted"

# ---- Step 5: Summary -------------------------------------------------------
echo ""
echo "=== Artifact summary ==="

for artifact in medre.log snapshot.json config.redacted inspect-event.json inspect-timeline.json inspect-evidence.json inspect-recovery.json receipts.json; do
	path="${OUTPUT_DIR}/${artifact}"
	if [[ -f ${path} ]]; then
		size=$(stat --format='%s' "${path}" 2>/dev/null || stat -f '%z' "${path}" 2>/dev/null || echo "?")
		echo "  [OK]   ${artifact}  (${size} bytes)"
	else
		echo "  [MISS] ${artifact}"
	fi
done

echo ""
echo "=== Done. Artifacts are in ${OUTPUT_DIR} ==="
