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

# `command -v python3` is not enough: on a clean macOS /usr/bin/python3 is a
# stub that only prompts to install Command Line Tools. Actually run it.
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
    echo "error: a runnable python3 (3.8+) is required." >&2
    echo "  On a fresh macOS, /usr/bin/python3 is only a stub until the Command" >&2
    echo "  Line Tools are installed. Fix with either:" >&2
    echo "      xcode-select --install" >&2
    echo "      brew install python" >&2
    exit 1
fi

if ! python3 -c 'import venv' 2>/dev/null; then
    echo "error: the python3 'venv' module is missing; cannot build the daemon venv." >&2
    exit 1
fi

echo "==> installing dimmer.py + ddc.py -> $BIN/"
mkdir -p "$BIN" "$CFG" "$HOME/Library/LaunchAgents"
install -m 755 "$SRC/dimmer.py" "$BIN/dimmer.py"
# ddc.py is imported by dimmer.py, never executed, so it is not +x. It must
# land in the SAME directory: dimmer.py adds its own directory to sys.path and
# imports `ddc` from there.
install -m 644 "$SRC/ddc.py" "$BIN/ddc.py"

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

# Shell alias. Pick the rc file for the user's ACTUAL shell and create it if
# absent -- a fresh macOS account often has no ~/.zshrc at all, and the old
# "only if the file already exists" loop silently installed no alias and said
# nothing about it. macOS bash login shells read .bash_profile, not .bashrc.
SHELL_NAME="$(basename "${SHELL:-/bin/zsh}")"
case "$SHELL_NAME" in
    zsh)
        RC="$HOME/.zshrc"
        ALIAS_LINE="alias dim='python3 \$HOME/bin/dimmer.py'"
        ;;
    bash)
        if [ -f "$HOME/.bashrc" ]; then RC="$HOME/.bashrc"; else RC="$HOME/.bash_profile"; fi
        ALIAS_LINE="alias dim='python3 \$HOME/bin/dimmer.py'"
        ;;
    fish)
        RC="$HOME/.config/fish/config.fish"
        mkdir -p "$(dirname "$RC")"
        ALIAS_LINE="alias dim='python3 \$HOME/bin/dimmer.py'"
        ;;
    *)
        RC=""
        ;;
esac

if [ -z "$RC" ]; then
    echo "==> unrecognised shell '$SHELL_NAME'; add this alias yourself:"
    echo "      dim -> python3 \$HOME/bin/dimmer.py"
elif grep -q "alias dim=" "$RC" 2>/dev/null; then
    echo "==> alias already present in $RC"
else
    touch "$RC"
    printf '\n%s\n' "$ALIAS_LINE" >> "$RC"
    echo "==> added alias to $RC"
fi

cat <<'DONE'

Installed.

  dim 60        set the external display to 60%
  dim reset     restore full brightness, stop the daemon
  dim status    show requested level vs the gamma read back
  dim probe     show which backend each display uses, and why

Open a new shell (or `source ~/.zshrc`) to pick up the alias.

Run `dim probe` first. Where a monitor answers on DDC/CI this drives its real
backlight and no daemon is needed. Where it doesn't, it falls back to dimming
on the GPU, which is not the same thing -- see the README.
DONE
