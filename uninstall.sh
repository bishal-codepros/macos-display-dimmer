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

# Remove the alias we added. Note rc files are often symlinks into a dotfiles
# repo, so resolve the real path -- `sed -i` refuses to edit a symlink. Only an
# exact match on the line we wrote is removed; a hand-customised alias is left
# alone and reported, and nothing else in the file is touched.
python3 - "$HOME/.zshrc" "$HOME/.bashrc" <<'ALIAS_EOF'
import os, re, sys

PATTERN = re.compile(r"^alias dim='python3 (\$HOME|~)/bin/dimmer\.py'$")
for raw in sys.argv[1:]:
    if not os.path.exists(raw):
        continue
    path = os.path.realpath(raw)          # follow dotfiles symlinks
    try:
        lines = open(path).readlines()
    except OSError as e:
        print(f"==> could not read {path}: {e}")
        continue
    kept, removed, custom = [], 0, []
    for line in lines:
        stripped = line.rstrip("\n")
        if PATTERN.match(stripped.strip()):
            removed += 1
            continue
        if stripped.strip().startswith("alias dim="):
            custom.append(stripped.strip())
        kept.append(line)
    if removed:
        while kept and kept[-1].strip() == "":
            kept.pop()
        if kept:
            kept[-1] = kept[-1].rstrip("\n") + "\n"
        open(path, "w").writelines(kept)
        print(f"==> removed alias from {path}")
    if custom:
        print(f"==> left a customised alias in {path}: {custom[0]}")
ALIAS_EOF

cat <<'DONE'

Uninstalled. Open a new shell to drop the `dim` alias from your session.

DONE
