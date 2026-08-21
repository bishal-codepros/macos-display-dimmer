#!/usr/bin/env bash
#
# Remove dimmer: restore full brightness, stop and unregister the agent,
# delete the script, venv and state. The shell alias line is left for you
# to remove by hand.
#
set -uo pipefail

BIN="$HOME/bin"
CFG="$HOME/.config/dimmer"
LABEL="com.dimmer.agent"
UID_NUM="$(id -u)"

echo "==> restoring full brightness and stopping the daemon"
if [[ -x "$BIN/dimmer.py" ]]; then
    python3 "$BIN/dimmer.py" reset 2>/dev/null || true
fi

for legacy in "$LABEL" "com.bishal.dimmer"; do
    launchctl bootout "gui/$UID_NUM/$legacy" 2>/dev/null || true
    rm -f "$HOME/Library/LaunchAgents/$legacy.plist"
done
echo "==> launchd agent removed"

# Belt and braces before deleting the script: an older build ignored SIGTERM
# entirely, so sweep for survivors. Uses dimmer's own exact-argv matcher --
# deliberately NOT `pkill -f`, which matches any command line merely
# mentioning the path (a grep, an editor) and would SIGKILL it.
if [[ -f "$BIN/dimmer.py" ]]; then
    python3 - "$BIN/dimmer.py" <<'SWEEP' 2>/dev/null || true
import importlib.util, sys
spec = importlib.util.spec_from_file_location("dm", sys.argv[1])
dm = importlib.util.module_from_spec(spec); spec.loader.exec_module(dm)
killed = dm.kill_all()
print(f"==> swept {len(killed)} stray daemon(s)" if killed else "==> no strays")
SWEEP
fi

rm -f "$BIN/dimmer.py"
rm -rf "$CFG"
rm -f /tmp/dimmer.log /tmp/dimmer.out /tmp/dimmer.err
echo "==> script, venv and state removed"

cat <<'DONE'

Uninstalled. Remove the alias line yourself if you want it gone:

  grep -n "alias dim=" ~/.zshrc ~/.bashrc 2>/dev/null

DONE
