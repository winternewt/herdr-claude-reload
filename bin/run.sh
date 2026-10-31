#!/bin/sh
# run.sh: the entry of the action and of both event hooks. herdr runs an event hook for every pane on the machine, so
# with no request queued this exits at once and starts nothing; with one, it runs bin/reload.py for one pass.
set -u
dir=${HERDR_PLUGIN_CONFIG_DIR:-}
[ -n "$dir" ] || { echo "run.sh: HERDR_PLUGIN_CONFIG_DIR is not set: run by herdr only" >&2; exit 2; }
for f in "$dir"/requests/*.json; do
  [ -e "$f" ] || exit 0
  exec "${CLAUDE_RELOAD_PYTHON:-python3}" "$(dirname "$0")/reload.py"
done
exit 0
