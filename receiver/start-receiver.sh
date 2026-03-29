#!/bin/bash
# KevinStream Receiver - Mac/Linux launcher
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python3 "$SCRIPT_DIR/receive.py" "$@"
