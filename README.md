# macos-display-dimmer

Dim an external display on macOS. Drives the real backlight over DDC/CI where
the monitor answers, falls back to GPU gamma where it doesn't. Picked per
display, automatically.

## Requirements

- macOS 11+, Apple Silicon (DDC/CI backend uses `IOAVService*`; on Intel
  everything falls back to gamma)
- `python3` 3.11+ with the `venv` module
- An external display
- `pyobjc-framework-Cocoa` — gamma daemon only, see `requirements.txt`

## Setup

```bash
mkdir -p ~/bin ~/.config/dimmer ~/Library/LaunchAgents
install -m 755 dimmer.py ~/bin/dimmer.py
install -m 644 ddc.py    ~/bin/ddc.py      # imported by dimmer.py, same dir required

python3 -m venv ~/.config/dimmer/venv
~/.config/dimmer/venv/bin/pip install -r requirements.txt
```

Alias:

```bash
echo "alias dim='python3 \$HOME/bin/dimmer.py'" >> ~/.zshrc
```

launchd agent (restores the saved level at login; gamma backend only):

```bash
cat > ~/Library/LaunchAgents/com.dimmer.agent.plist <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.dimmer.agent</string>
    <key>ProgramArguments</key>
    <array>
        <string>$HOME/.config/dimmer/venv/bin/python</string>
        <string>$HOME/bin/dimmer.py</string>
        <string>--daemon</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
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
PLIST

plutil -lint ~/Library/LaunchAgents/com.dimmer.agent.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.dimmer.agent.plist
```

`KeepAlive` must stay `false`. With `true`, launchd resurrects the daemon
seconds after `dim reset`.

Reinstalling over an existing setup: run `python3 ~/bin/dimmer.py reset` and
`launchctl bootout gui/$(id -u)/com.dimmer.agent` first, or two daemons fight
over the gamma table.

## Usage

```bash
dim 60        # set to 60%
dim reset     # back to 100%, stop the daemon
dim status    # requested level vs gamma readback
dim probe     # which backend per display, and why (-r re-probes, ignoring cache)
```

Levels clamp to 5–100.

## Settings

| Where | What |
|---|---|
| `TRANSITION` in `dimmer.py` | fade duration, default `0.30` s (gamma only) |
| `FPS` in `dimmer.py` | fade frame rate, default `60` |
| `CACHE_VERSION` in `ddc.py` | bump to invalidate every cached DDC capability |

## Paths

| Path | What |
|---|---|
| `~/bin/dimmer.py`, `~/bin/ddc.py` | the scripts |
| `~/.config/dimmer/venv/` | venv holding pyobjc |
| `~/.config/dimmer/level` | saved level; deleted by `dim reset` |
| `~/.config/dimmer/ddc.json` | cached DDC capability, keyed by EDID fingerprint |
| `~/.config/dimmer/pid` | running daemon pid |
| `~/.config/dimmer/ctl` | FIFO the CLI uses to talk to the daemon |
| `~/Library/LaunchAgents/com.dimmer.agent.plist` | launchd agent |
| `/tmp/dimmer.{log,out,err}` | daemon log, launchd stdio |

Nothing system-wide, no `sudo`, system and Homebrew Python untouched.

## Uninstall

```bash
python3 ~/bin/dimmer.py reset
launchctl bootout gui/$(id -u)/com.dimmer.agent
rm -f ~/Library/LaunchAgents/com.dimmer.agent.plist
rm -f ~/bin/dimmer.py ~/bin/ddc.py
rm -rf ~/.config/dimmer
rm -f /tmp/dimmer.log /tmp/dimmer.out /tmp/dimmer.err
```

Then delete the `dim` alias line from your rc file.

## Tests

```bash
python3 tests/test_ddc.py        # DDC/CI protocol: framing, replies, EDID
python3 tests/test_matching.py   # daemon process matching
python3 tests/test_dimmer.py     # real hardware; skips without an external display
```

## Gamma backend caveats

Not a backlight. Backlight stays at full power, black floor doesn't move so
contrast drops as you dim, banding below ~20%, and it cannot touch hue,
saturation, colour presets or input source. CoreGraphics scopes a gamma ramp to
the process that set it, so the daemon must stay resident. DDC/CI has none of
these limits and needs no daemon.

## License

MIT. See [LICENSE](LICENSE).
