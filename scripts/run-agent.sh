#!/bin/bash
# switchboard — restart the AudioSocket bridge with the venv interpreter,
# the .env sourced into os.environ, and logs captured to agent/bridge.log.
# (The previous process was detached with no log capture.)
set -u
ROOT="$HOME/Sites/switchboard"

# stop the running bridge (any python running bridge.py)
pkill -f "bridge.py" 2>/dev/null || true
sleep 1

cd "$ROOT" || exit 1
set -a
# shellcheck disable=SC1091
. ./.env
set +a

cd agent || exit 1
nohup .venv312/bin/python bridge.py > "$ROOT/agent/bridge.log" 2>&1 < /dev/null &
PID=$!
sleep 3
echo "restarted bridge pid=$PID"
echo "--- pgrep ---"
pgrep -fl "bridge.py"
echo "--- bridge.log (startup) ---"
cat "$ROOT/agent/bridge.log"
