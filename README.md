# macos-display-dimmer

A tiny CLI to dim an external display on macOS when the monitor refuses to talk
DDC/CI.

```console
$ dim 40
external display -> 40%

$ dim status
daemon  : running pid 7913
level   : 40% (requested)
gamma   : display 2 reports 40%   <- read back from CoreGraphics
fade    : 300 ms, smoothstep
external: [2]
```

Brightness changes fade over 300 ms instead of snapping. The level survives
display sleep, wake, resolution changes, unplug/replug and reboot.

## Read this before installing

**This does not touch your backlight.** It rescales the display's gamma ramp on
the GPU, before pixels leave the Mac. The monitor is never consulted. Which
means:

- The backlight still runs at full power. No energy saved, no less heat.
- White gets dimmer but the panel's black floor doesn't move, so the measured
  contrast ratio drops as you dim.
- You're compressing 8-bit values into a narrower range, so expect some banding
  in gradients below roughly 20%.
- It cannot touch hue, saturation, colour presets or input source. Those live in
  the monitor and need DDC/CI.

If your monitor *does* support DDC/CI, **use [`m1ddc`](https://github.com/waydabber/m1ddc)
or [Lunar](https://lunar.fyi/) instead** — they drive the real backlight, the
setting persists in the monitor's own memory, and no background process is
needed. This tool is strictly the fallback for when that doesn't work.

## When you actually need this

DDC/CI rides an I²C channel multiplexed onto the DisplayPort AUX lines. Per the
[ddcutil Type-C notes](https://www.ddcutil.com/typec/), USB-C to a Type-C or
DisplayPort input on the monitor should just work. HDMI is where it breaks: a
USB-C connector can only emit DisplayPort, never TMDS, so reaching an HDMI input
*always* requires active protocol conversion — and cheap converters forward EDID
and video while silently dropping I²C traffic to address `0x37`, where the
monitor control commands live.

Diagnose it in one command:

```console
$ m1ddc display list
$ m1ddc display 2 max luminance
-128
```

`-128` is `0x80` read as a signed byte — the signature of an I²C transaction
nobody answered. If you get that consistently, no software can fix it; the
packets die inside the cable or the monitor doesn't implement DDC/CI. That's the
case this tool exists for.

Born from exactly that: a 27" HDMI-only panel behind a USB-C→HDMI converter that
forwards EDID at `0x50` but nothing at `0x37`.

## Requirements

- **macOS 11 Big Sur or later.** `launchctl bootstrap gui/` needs 10.11+; the
  rest is older still. Developed and tested on macOS 26.6.2, Apple Silicon
  (`arm64`). **Intel is untested** — the CoreGraphics gamma API is
  architecture-independent and pyobjc ships `x86_64` wheels, so it should work,
  but nobody has run it. Report back if you try.
- **A runnable `python3`, 3.8 or newer.** On a clean macOS, `/usr/bin/python3`
  is only a stub that prompts to install the Command Line Tools — `install.sh`
  actually executes python to catch this rather than just checking the path. Fix
  with `xcode-select --install` or `brew install python`.
- **Network access on first install**, to `pip install pyobjc-framework-Cocoa`
  into an isolated venv at `~/.config/dimmer/venv`. Nothing is installed into
  your system or Homebrew Python.
- **An external display.** Nothing here touches the built-in panel — macOS
  already controls that backlight properly.

### Shells

`install.sh` adds a `dim` alias to the rc file for your actual `$SHELL`,
creating it if needed:

| Shell | File |
|---|---|
| zsh (macOS default) | `~/.zshrc` |
| bash | `~/.bashrc` if present, else `~/.bash_profile` (what macOS login shells read) |
| fish | `~/.config/fish/config.fish` |

Anything else and it prints the alias for you to add by hand. `uninstall.sh`
removes it from all four locations, resolving symlinks first — rc files are
often symlinked into a dotfiles repo, and `sed -i` refuses to edit a symlink.

The alias keeps `$HOME` unexpanded on purpose: single quotes stop it expanding
when the alias is *defined*, and the shell expands it when the alias is *used*,
so the same line works for any user.

The scripts are `bash` but only use constructs valid in **bash 3.2**, which is
what macOS ships — no bash-4 syntax, no `readlink -f` (absent before 12.3).

## Install

```bash
git clone git@github.com:bishal-codepros/macos-display-dimmer.git
cd macos-display-dimmer
./install.sh
```

Then open a new shell.

## Usage

```bash
dim 60        # set to 60% (fades over 300 ms)
dim 5         # minimum
dim 100       # full
dim reset     # fade back to 100%, stop the daemon
dim status    # requested level vs actual gamma readback
```

Levels are clamped to 5–100 so you can't blank the screen and lose the ability
to see the terminal you'd fix it from.

The fade duration is fixed — 300 ms with smoothstep easing, in the range desktop
OSes use. There is deliberately no speed flag. Change `TRANSITION` in
`dimmer.py` if you disagree.

## Uninstall

```bash
./uninstall.sh
```

## How it works

`CGSetDisplayTransferByFormula` rescales the display's 1024-entry gamma lookup
table. With `min=0, max=v, gamma=1` the transfer function collapses to
`output = v × input` — a linear scale of every channel, applied identically to
R, G and B so colour stays neutral.

The awkward part is that **CoreGraphics scopes a gamma ramp to the process that
set it and reverts on exit.** A one-shot command genuinely cannot work; macOS
undoes it microseconds later. macOS also resets the ramp on login, on display
sleep and wake, and on any resolution change. So a resident holder is a platform
constraint, not a design preference.

It's built to cost nothing while idle:

- **Main thread** parks in `NSApp.run()` waiting for screen-change notifications.
- **Reader thread** blocks on a FIFO at `~/.config/dimmer/ctl` for commands.
- **Animator thread** blocks on an `Event` until there's a fade to run.

No timers. Nothing polls. Measured **0.03 s of CPU across ten level changes plus
25 s idle** — the fades don't even register at `ps`'s centisecond resolution.

`dim 60` writes the level and pokes the FIFO; the running daemon retunes without
respawning.

## Notes for anyone hacking on this

Four constraints that cost real debugging time. All verified, not guessed.

**1. `CFRunLoopRun()` does not pump AppKit's event loop.** Screen-change
notifications are never delivered from it.
`CGDisplayRegisterReconfigurationCallback` is worse: it registers with `rc=0`
and is *also* never delivered in an unbundled Python process. The only thing
that works is pyobjc's `NSApp.run()` with an observer on
`NSApplicationDidChangeScreenParametersNotification`.

**2. Python signal handlers never run while the main thread is inside that
loop.** The interpreter never regains control, so `SystemExit` raised in a
handler is never processed — the daemon silently ignores `SIGTERM` and becomes
unkillable, and signal-based IPC does nothing at all. Hence the FIFO, and
`SIGTERM` left at `SIG_DFL` so the kernel can always kill it.

**3. `CGGetActiveDisplayList` excludes *sleeping* displays.** Use
`CGGetOnlineDisplayList`. With the active list the tool goes blind every time
the screen sleeps and reports "no external display found".

**4. macOS reinstalls its own ramp as a display re-settles, and the
notification fires more than once per event.** A single immediate re-apply loses
the race, so `_schedule_reassert()` re-applies at +0.2/+0.8/+2.0/+4.0 s,
debounced. Bounded and event-triggered — still not a poll loop.

### Verify outcomes, not proxies

Every one of those bugs initially passed review because a *proxy* looked
healthy: the PID was stable, CPU stayed at zero, the API returned `rc=0`. None
of that proves a pixel changed. `CGGetDisplayTransferByFormula` reads the ramp
back, and that's what `dim status` prints. If requested and readback ever
disagree, something is broken:

```
level   : 45% (requested)
gamma   : display 2 reports 100%     <- mismatch: bug
```

## Tests

```bash
python3 tests/test_dimmer.py
```

20 assertions, every brightness check against the readback. Requires an external
display; skips cleanly without one.

Covers level application, live-daemon retune, rapid-fire supersede, FIFO-loss
recovery, `SIGTERM` killability, gamma restore on process death, and display
reconfiguration. That last one is exercised without touching hardware by
switching the display mode with `CGDisplaySetDisplayMode` and switching back —
which fires the same notification path as a physical unplug/replug.

The suite also asserts a safety property worth keeping: daemon discovery matches
on **exact argv elements**, never a substring of the command line. An earlier
version matched any process merely mentioning the script path — and SIGKILLed a
`grep`.

## Troubleshooting

**`dim 60` says "no external display found"** — check `dim status`. If the
display shows `(asleep)` it's fine; if nothing is listed, macOS doesn't see the
display at all.

**Requested and readback disagree** — something reset the ramp and the daemon
didn't notice. `dim <level>` re-applies immediately. `/tmp/dimmer.log` records
every notification and re-assert.

**Level lost after reboot** — the launchd agent should restore it:
```bash
launchctl print gui/$(id -u)/com.dimmer.agent | head
launchctl kickstart -k gui/$(id -u)/com.dimmer.agent
```

**Two daemons** — shouldn't happen, but `dim reset` sweeps all of them, with a
`SIGKILL` fallback for survivors.

## Limitations

- Applies one level to **all** external displays; no per-display control.
- Not on PyPI or Homebrew. Clone and run `install.sh`.
- Tested on one machine, one monitor, one macOS version. Issues welcome.

## License

MIT — see [LICENSE](LICENSE).
