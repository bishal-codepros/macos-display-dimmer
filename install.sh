#!/usr/bin/env bash
#
# Install dimmer: copy the script, build a venv with pyobjc, register a
# launchd agent, and add a `dim` shell alias.
#
set -euo pipefail

BIN="$HOME/bin"
CFG="$HOME/.config/dimmer"
VENV="$CFG/venv"
LABEL="com.dimmer.agent"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UID_NUM="$(id -u)"

if [[ "$(uname)" != "Darwin" ]]; then
    echo "error: macOS only (this drives CoreGraphics gamma tables)" >&2
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "error: python3 not found" >&2
    exit 1
fi

echo "==> installing dimmer.py -> $BIN/dimmer.py"
mkdir -p "$BIN" "$CFG" "$HOME/Library/LaunchAgents"
install -m 755 "$SRC/dimmer.py" "$BIN/dimmer.py"

# The daemon needs pyobjc for AppKit screen-change notifications. It lives in
# its own venv so we never touch the system or Homebrew site-packages.
echo "==> building venv at $VENV (pyobjc-framework-Cocoa)"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet pyobjc-framework-Cocoa
"$VENV/bin/python" -c "import AppKit" || {
    echo "error: pyobjc did not import; the daemon cannot detect display changes" >&2
    exit 1
}

# Sweep any daemon left over from a previous install BEFORE registering the
# new agent, or you end up with two holders fighting over the gamma table.
# `reset` uses dimmer's own exact-argv process matcher plus a SIGKILL fallback,
# which is safer than a `pkill -f` substring match.
echo "==> clearing any existing daemon"
python3 "$BIN/dimmer.py" reset >/dev/null 2>&1 || true

# Retire any agent from an earlier install, including the pre-rename label.
for legacy in "$LABEL" "com.bishal.dimmer"; do
    launchctl bootout "gui/$UID_NUM/$legacy" 2>/dev/null || true
    [[ "$legacy" != "$LABEL" ]] && rm -f "$HOME/Library/LaunchAgents/$legacy.plist"
done

echo "==> writing launchd agent $LABEL"
cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$VENV/bin/python</string>
        <string>$BIN/dimmer.py</string>
        <string>--daemon</string>
    </array>
    <!-- Start at login so a saved level is restored. -->
    <key>RunAtLoad</key>
    <true/>
    <!-- KeepAlive MUST stay false. With true, launchd would resurrect the
         daemon seconds after \`dim reset\`, overriding an explicit "off". -->
    <key>KeepAlive</key>
    <false/>
    <key>ProcessType</key>
    <string>Background</string>
    <key>StandardOutPath</key>
    <string>/tmp/dimmer.out</string>
    <key>StandardErrorPath</key>
    <string>/tmp/dimmer.err</string>
</dict>
</plist>
PLIST_EOF

plutil -lint "$PLIST"
launchctl bootstrap "gui/$UID_NUM" "$PLIST"

# Shell alias, for whichever rc file exists.
ALIAS="alias dim='python3 \$HOME/bin/dimmer.py'"
for rc in "$HOME/.zshrc" "$HOME/.bashrc"; do
    [[ -f "$rc" ]] || continue
    if grep -q "alias dim=" "$rc"; then
        echo "==> alias already present in $rc"
    else
        printf '\n%s\n' "$ALIAS" >> "$rc"
        echo "==> added alias to $rc"
    fi
done

cat <<'DONE'

Installed.

  dim 60        set the external display to 60%
  dim reset     restore full brightness, stop the daemon
  dim status    show requested level vs the gamma read back

Open a new shell (or `source ~/.zshrc`) to pick up the alias.

Note: this dims on the GPU, not the backlight. See the README for what that
does and does not mean.
DONE
