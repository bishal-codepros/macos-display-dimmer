# macos-display-dimmer

A tiny CLI to dim an external display on macOS. It drives the monitor's real
backlight over DDC/CI where that works, and falls back to GPU gamma dimming
where it doesn't — automatically, per display.

```console
$ dim 40
external display -> 40%

$ dim status
daemon  : running pid 7913
level   : 40% (requested)
gamma   : display 2 reports 40%   <- read back from CoreGraphics
fade    : 300 ms, smoothstep (gamma only)
external: [2]

usage: dim <5-100> | dim reset | dim status | dim probe
```

`dim probe` walks the low-level display path and reports which backend you are
on and why:

```console
$ dim probe
IOAVService     : available
external AV svcs: 1   (DCPAVServiceProxy, Location=External)

display 2
  EDID        : E2711F  mfg=HKC product=0x2792 serial=0x00000001
  DDC/CI 0x37 : no -- stub responder: valid DDC/CI Null Message to every request
  macOS says  : CanChangeBrightness=False
  backend     : gamma -- software dimming, daemon required
```

On the gamma backend, changes fade over 300 ms instead of snapping, and the
level survives display sleep, wake, resolution changes, unplug/replug and
reboot.

## Two backends, picked automatically

**DDC/CI — the real backlight.** If the monitor answers on the I²C control
channel, `dim` sets VCP code `0x10` and the panel dims for real. The monitor
stores the level in its own NVRAM, so nothing needs to stay resident: no
daemon, no launchd agent, no CPU. `dim` takes this path whenever it can.

**Gamma — the fallback.** When the monitor doesn't answer, `dim` rescales the
display's gamma ramp on the GPU instead, before pixels leave the Mac. That
works on anything, but it is not a backlight and you should know what you get:

- The backlight still runs at full power. No energy saved, no less heat.
- White gets dimmer but the panel's black floor doesn't move, so the measured
  contrast ratio drops as you dim.
- You're compressing 8-bit values into a narrower range, so expect some banding
  in gradients below roughly 20%.
- It cannot touch hue, saturation, colour presets or input source. Those live in
  the monitor and need DDC/CI.
- CoreGraphics scopes a gamma ramp to the process that set it, so a resident
  daemon is unavoidable. See [How it works](#how-it-works).

Mixed setups work: each display gets whichever backend it actually supports.
`dim probe` tells you which.

If all your monitors support DDC/CI, [`m1ddc`](https://github.com/waydabber/m1ddc)
and [Lunar](https://lunar.fyi/) are mature tools that do the same thing with far
more features. This project's reason to exist is the fallback.

## When you actually need this

DDC/CI rides an I²C channel multiplexed onto the DisplayPort AUX lines. Per the
[ddcutil Type-C notes](https://www.ddcutil.com/typec/), USB-C to a Type-C or
DisplayPort input on the monitor should just work. HDMI is where it breaks: a
USB-C connector can only emit DisplayPort, never TMDS, so reaching an HDMI input
*always* requires active protocol conversion — and the converter decides what
becomes of the I²C traffic carrying the monitor control commands.

Born from exactly that: a 27" HDMI-only panel (HKC E2711F) behind a USB-C→HDMI
converter. Run `dim probe` and it reports:

```
  EDID        : E2711F  mfg=HKC product=0x2792 serial=0x00000001
  DDC/CI 0x37 : no -- stub responder: valid DDC/CI Null Message to every request
```

EDID reads back perfectly from `0x50`, so I²C does reach the far end. But every
request to `0x37` — every VCP code, the capabilities request `0xF3`, the
identification request `0xF1`, at settle times from 50 ms to 800 ms — returns
the same three bytes:

```
6e 80 be
```

That is a **DDC/CI Null Message**: source address `0x6E`, a length byte of
`0x80` meaning zero payload, and checksum `0x50 ^ 0x6E ^ 0x80 = 0xBE`, which is
correct. Something at `0x37` is parsing the request and deliberately answering
"nothing." A stub responder, not a control channel.

This is worth dwelling on, because it is indistinguishable from success unless
you inspect the payload. `IOAVServiceWriteI2C` returns `0`. `IOAVServiceReadI2C`
returns `0`. The reply is correctly framed and correctly checksummed. Only the
length field gives it away.

It also explains the `-128` that `m1ddc` reports on this link, which reads like
"nobody answered" but isn't:

```console
$ m1ddc display 2 max luminance
-128
```

`-128` is `0x80` as a signed byte — and `0x80` is byte 1 of `6e 80 be`, the null
message's length field. The transaction *was* answered. The answer was empty.

macOS agrees independently: `DisplayServicesCanChangeBrightness` returns `False`
for this display and `True` for the built-in panel, which is why the Displays
pane offers no brightness slider for it.

If `dim probe` shows the same, no software can fix it — the control channel
genuinely is not there. Check your monitor's OSD menu for a `DDC/CI` toggle
first, since some panels ship with it off; after that it is a hardware question,
and a different adapter (USB-C→DisplayPort direct, or another HDMI converter) is
the only thing that might change the answer.

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
  your system or Homebrew Python. The DDC/CI backend needs no dependencies at
  all — it is `ctypes` against IOKit. pyobjc is only for the gamma daemon's
  screen-change notifications.
- **Apple Silicon, for the DDC/CI backend.** It reaches the DCP through
  `IOAVService*`, which does not exist on Intel Macs — those expose the older
  `IOFramebuffer` I²C API instead, which this tool does not implement. On Intel
  the probe finds no AV services and everything falls back to gamma.
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

Then open a new shell (or `source` your rc file) to pick up the `dim` alias.

### What it puts on your machine

| Path | What |
|---|---|
| `~/bin/dimmer.py` | the script (a **copy** — see below) |
| `~/bin/ddc.py` | the DDC/CI module, imported by `dimmer.py` from the same directory |
| `~/.config/dimmer/venv/` | isolated venv holding pyobjc |
| `~/.config/dimmer/level` | the saved level; deleted by `dim reset` |
| `~/.config/dimmer/ddc.json` | cached per-monitor DDC capability, keyed by EDID fingerprint |
| `~/.config/dimmer/pid` | the running daemon's pid |
| `~/.config/dimmer/ctl` | FIFO the CLI uses to talk to the daemon |
| `~/Library/LaunchAgents/com.dimmer.agent.plist` | starts the daemon at login |
| your shell rc file | the `dim` alias |
| `/tmp/dimmer.{log,out,err}` | daemon log and launchd stdio |

Nothing is installed system-wide, nothing needs `sudo`, and your system and
Homebrew Python are left alone.

`install.sh` is safe to re-run: it stops any existing daemon first (a leftover
holder from an earlier install otherwise fights the new one over the gamma
table) and retires the old launchd agent before registering the new one.

> **Editing the code:** `~/bin/dimmer.py` is a copy made by the installer. Edit
> the repo and re-run `./install.sh`; changes made directly to `~/bin` are
> silently overwritten on the next install.

> **If your rc file is a symlink** into a dotfiles repo — common — the alias is
> appended to the real file, so it shows up as an uncommitted change in that
> repo. Expected, and `uninstall.sh` cleans it back out.

## Usage

```bash
dim 60        # set to 60% (fades over 300 ms)
dim 5         # minimum
dim 100       # full
dim reset     # back to 100%, stop the daemon
dim status    # requested level vs actual gamma readback
dim probe     # which backend, and why (add -r to re-probe, ignoring the cache)
```

Levels are clamped to 5–100 so you can't blank the screen and lose the ability
to see the terminal you'd fix it from.

The fade duration is fixed — 300 ms with smoothstep easing, in the range desktop
OSes use. There is deliberately no speed flag. Change `TRANSITION` in
`dimmer.py` if you disagree. Fading applies to the gamma backend only; a DDC
write is a single command and the monitor does its own ramping.

## Uninstall

```bash
./uninstall.sh
```

Reverses everything above: fades the display back to 100%, stops the daemon,
unregisters and deletes the launchd agent, sweeps any stray daemon a previous
build may have left behind, removes both scripts, the venv, state and logs, and
deletes the `dim` alias from your rc file.

The alias removal resolves symlinks first (`sed -i` refuses to edit one) and
matches only the exact line `install.sh` wrote — if you customised your `dim`
alias it is left alone and reported rather than clobbered.

Open a new shell afterwards to drop `dim` from the current session.

## How it works

### DDC/CI backend

On Apple Silicon there is no `/dev/i2c` and no `IOFramebuffer`. The DisplayPort
link is owned by the **DCP**, a coprocessor running its own firmware, and every
I²C transaction is an RPC to it through three private IOKit symbols:
`IOAVServiceCreateWithService`, `IOAVServiceReadI2C`, `IOAVServiceWriteI2C`.
The endpoints appear in the IORegistry as `DCPAVServiceProxy` nodes with
`Location` = `Embedded` (the built-in panel, never touched) or `External`.

If you are coming from Linux, this is the part that differs most: there the I²C
master is in-kernel and exposed as a device node, so `ddcutil` opens
`/dev/i2c-N` and writes to it. Here the master lives in coprocessor firmware and
userland only gets to send it messages. Asahi Linux reverse-engineered the same
protocol in `drivers/gpu/drm/apple/` if you want to see it from the other side.

`ddc.py` enumerates those endpoints, reads EDID from `0x50`, and speaks MCCS
over `0x37`: `0x01` to get a VCP feature, `0x03` to set one, `0x10` for
luminance.

Displays are paired to `CGDirectDisplayID`s by **EDID identity** —
`CGDisplayVendorNumber` / `ModelNumber` / `SerialNumber` are exactly EDID bytes
8–15 — rather than by enumeration order, which is what makes some tools mix up
which monitor they are talking to on multi-monitor setups.

Capability is decided by parsed, checksum-valid, **non-empty** data, never by a
return code, and cached against the monitor's EDID fingerprint so a swapped
monitor re-probes and the same monitor never does.

### Gamma backend

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
of that proves a pixel changed.

The DDC/CI work produced the sharpest example yet. Probing `0x37` on a dead
link, `IOAVServiceWriteI2C` returns `0`, `IOAVServiceReadI2C` returns `0`, and
the reply is correctly framed with a valid checksum. Four independent
success-shaped signals. The payload length is zero, and that single field is
the entire difference between a working backlight and none at all. So
`ddc.probe()` requires parsed, non-empty data and treats every return code as
meaningless. `CGGetDisplayTransferByFormula` reads the ramp
back, and that's what `dim status` prints. If requested and readback ever
disagree, something is broken:

```
level   : 45% (requested)
gamma   : display 2 reports 100%     <- mismatch: bug
```

## Tests

Two suites need no hardware and run anywhere:

```bash
python3 tests/test_ddc.py        # DDC/CI protocol: framing, replies, EDID
python3 tests/test_matching.py   # daemon process matching
```

`test_ddc.py` pins the wire format against golden packets and a real 128-byte
EDID block captured from hardware, so it tests the protocol rather than the
implementation that produced it. The assertion that earns its keep is that a
**Null Message is rejected**: `6e 80 be` is correctly framed, correctly
checksummed and completely empty, and `IOAVServiceReadI2C` returns `0` for it.
Accept it and the tool reports working brightness control over a link that has
none.

`test_matching.py` covers both ways daemon discovery has gone wrong. Too loose:
an early version matched any command line merely mentioning the script path, and
SIGKILLed a `grep`. Too tight: matching exact argv elements split out of `ps`
output looks safe, but `ps` joins argv with spaces, so a script path *containing*
a space shreds into two elements and the match can never hit — which silently
disabled `kill_all()`, so `dim reset` could not stop its own daemon and holders
accumulated one per invocation. Matching now anchors on the exact
` <path> --daemon` suffix, which survives spaces and still rejects a `grep`.

The third suite drives real hardware:

```bash
python3 tests/test_dimmer.py
```

Every brightness check is against the gamma readback. Requires an external
display; skips cleanly without one. Covers backend partitioning, level
application, live-daemon retune, rapid-fire supersede, FIFO-loss recovery,
`SIGTERM` killability, gamma restore on process death, and display
reconfiguration. That last one is exercised without touching hardware by
switching the display mode with `CGDisplaySetDisplayMode` and switching back —
which fires the same notification path as a physical unplug/replug.

## Troubleshooting

**Which backend am I on?** — `dim probe`. It reports the AV service count, the
EDID it read, what `0x37` answered, and macOS's own
`DisplayServicesCanChangeBrightness` verdict as an independent cross-check. Add
`-r` to ignore the cached capability and re-probe the hardware.

**`dim probe` says DDC/CI works but brightness doesn't move** — the monitor is
answering `0x10` but ignoring writes. Some panels only accept VCP writes when
their OSD menu is closed. `dim probe -r` after closing it.

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

- Applies one level to **all** external displays; no per-display control. The
  *backend* is chosen per display, but the level is global.
- Only VCP `0x10` (luminance) is driven. Contrast, input source and colour
  presets are reachable over the same channel but are not exposed.
- Not on PyPI or Homebrew. Clone and run `install.sh`.
- Tested on one machine, one monitor, one macOS version. Issues welcome.

## Contributing

Cloning may hand you an **HTTPS** remote even if you cloned with `git@` — some
git configs rewrite URLs, and HTTPS pushes fail without a credential helper.
If you plan to push:

```bash
git remote set-url origin git@github.com:bishal-codepros/macos-display-dimmer.git
```

Run `python3 tests/test_dimmer.py` before opening a PR. It needs an external
display attached and briefly changes its resolution to exercise the
reconfiguration path.

## License

MIT — see [LICENSE](LICENSE).
