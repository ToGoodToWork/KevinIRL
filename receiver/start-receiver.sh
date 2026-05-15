#!/bin/bash
# KevinStream Receiver - Mac/Linux launcher.
# Prompts for the SRT passphrase on first run and persists it to
# ~/.kevinstream/passphrase (mode 600). Set KEVINSTREAM_PASSPHRASE in the env
# to override the saved file. Pass --passphrase on the CLI to override both.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="${HOME}/.kevinstream"
PASSPHRASE_FILE="${CONFIG_DIR}/passphrase"

# If the caller already supplied --passphrase, just forward everything along.
if printf '%s\n' "$@" | grep -q -E '^--passphrase($|=)'; then
    exec python3 "$SCRIPT_DIR/receive.py" "$@"
fi

# Or use the env var if present.
PASS="${KEVINSTREAM_PASSPHRASE:-}"

# Otherwise: read from the saved file, or prompt + save.
if [ -z "$PASS" ] && [ -f "$PASSPHRASE_FILE" ]; then
    PASS="$(cat "$PASSPHRASE_FILE")"
fi

if [ -z "$PASS" ]; then
    echo "First-run setup: enter the SRT passphrase from the Pi (must match SRT_PASSPHRASE in stream.conf)."
    while true; do
        read -rsp "Passphrase (min 10 chars): " PASS
        echo
        if [ -z "$PASS" ]; then
            echo "  ERROR: empty passphrase not allowed."
            continue
        fi
        if [ ${#PASS} -lt 10 ]; then
            echo "  ERROR: too short (${#PASS} chars), minimum 10."
            PASS=""
            continue
        fi
        break
    done
    mkdir -p "$CONFIG_DIR"
    umask 077
    printf '%s' "$PASS" > "$PASSPHRASE_FILE"
    chmod 600 "$PASSPHRASE_FILE"
    echo "Saved to $PASSPHRASE_FILE (mode 600). Delete this file to be re-prompted."
fi

exec python3 "$SCRIPT_DIR/receive.py" --passphrase "$PASS" "$@"
