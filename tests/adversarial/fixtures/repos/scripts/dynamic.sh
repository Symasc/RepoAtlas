#!/usr/bin/env bash
# GROUND TRUTH: every target in this file is computed. NONE may resolve.

BASE=/opt/tools
NAME=runner.sh
SCRIPT="$BASE/$NAME"
"$SCRIPT"

source "$CONFIGURED_PATH"
python3 "$PYTHON_TARGET"
bash "${HELPER_SCRIPT}"

CMD=$(cat /etc/service/command)
$CMD --flag
