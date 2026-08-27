#!/usr/bin/env python3
"""Persistent GPU-side brightness for displays that don't honour DDC/CI.

CoreGraphics ties a gamma ramp to the process that set it, so a resident
holder is unavoidable. Design constraints learned the hard way:

  * The main thread must sit in NSApp.run() to receive screen-change
    notifications. CFRunLoopRun() does not pump AppKit's event loop, so
    nothing is delivered; CGDisplayRegisterReconfigurationCallback registers
    with rc=0 but is likewise never delivered in an unbundled Python process.
    Both were verified against a real reconfiguration.
  * While the main thread is inside that loop the interpreter never regains
    control, so Python-level signal handlers NEVER RUN -- SIGTERM and SIGHUP
    are silently swallowed and the daemon becomes unkillable.
  * So control messages arrive over a FIFO read by a worker thread
    (blocking read, 0% CPU), and SIGTERM is left at SIG_DFL so the kernel
    can always kill us.
  * No timers anywhere. Nothing polls.
"""
import ctypes, ctypes.util, os, signal, stat, subprocess, sys, threading, time

CFG   = os.path.expanduser("~/.config/dimmer")
LEVEL = os.path.join(CFG, "level")
PID   = os.path.join(CFG, "pid")
FIFO  = os.path.join(CFG, "ctl")
SELF  = os.path.realpath(__file__)
# ddc.py sits beside this script; the installer copies both into ~/bin. Our own
# directory is added explicitly because the test suite loads dimmer.py by path
# through importlib, which -- unlike running it as a script -- does not put the
# containing directory on sys.path.
sys.path.insert(0, os.path.dirname(SELF))
try:
    import ddc as _ddc
except Exception:
    _ddc = None                 # gamma-only; every DDC path below degrades to a no-op
# The daemon needs pyobjc (AppKit) for screen-change notifications; the CLI
# does not, so only the daemon must run under this interpreter.
VENV_PY = os.path.join(CFG, "venv", "bin", "python")
os.makedirs(CFG, exist_ok=True)

TRANSITION = 0.30      # seconds; in the range every desktop OS uses
FPS        = 60

cg = ctypes.CDLL(ctypes.util.find_library("ApplicationServices"))
cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
f  = ctypes.c_float
Pf = ctypes.POINTER(f)
cg.CGSetDisplayTransferByFormula.argtypes = [ctypes.c_uint32] + [f]*9
cg.CGGetDisplayTransferByFormula.argtypes = [ctypes.c_uint32] + [Pf]*9
cg.CGGetOnlineDisplayList.argtypes = [ctypes.c_uint32,
    ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32)]
cg.CGDisplayIsBuiltin.argtypes = [ctypes.c_uint32]
cg.CGDisplayIsAsleep.argtypes  = [ctypes.c_uint32]

LOG = "/tmp/dimmer.log"
def log(msg):
    try:
        with open(LOG, "a") as fh:
            fh.write(f"{time.strftime('%H:%M:%S')} [{os.getpid()}] {msg}\n")
    except Exception:
        pass

def externals():
    """ONLINE, not ACTIVE. CGGetActiveDisplayList excludes sleeping displays,
    so with the active list the tool went blind every time the screen slept:
    `dim 45` answered "no external display found" and the level was lost.
    A sleeping display is still attached and still owns a gamma ramp."""
    arr = (ctypes.c_uint32 * 16)(); n = ctypes.c_uint32()
    cg.CGGetOnlineDisplayList(16, arr, ctypes.byref(n))
    return [d for d in list(arr)[:n.value] if not cg.CGDisplayIsBuiltin(d)]

# --- backend selection -------------------------------------------------------
# Two ways to dim an external display, in order of preference:
#   DDC/CI  -- drives the real backlight, the monitor stores the level in its
#              own NVRAM, and NO resident process is needed.
#   gamma   -- rescales the GPU transfer table; needs this daemon alive because
#              CoreGraphics scopes a ramp to the process that set it.
# A display gets gamma only when DDC cannot drive it. Mixed setups work: each
# display is handled by whichever backend it actually supports.
_ddc_ids   = None                  # display IDs on hardware backlight; None = unknown
_ddc_lock  = threading.Lock()

def ddc_capable_ids(refresh=False):
    """CG display IDs whose backlight we can really drive. Opens no lasting
    handles. Cheap after the first call -- ddc.probe() caches its verdict
    against the monitor's EDID fingerprint, so a swapped monitor re-probes and
    the same monitor never does."""
    if _ddc is None:
        return set()
    found, avs = set(), _ddc.av_displays()
    try:
        for did in externals():
            m = _ddc.match_cg_display(did, avs)
            if m is not None and _ddc.probe(m, refresh=refresh).get("capable"):
                found.add(did)
    except Exception:
        return set()                       # never let a DDC fault break gamma
    finally:
        for x in avs:
            x.close()
    return found

def gamma_targets(refresh=False):
    """Externals that need gamma: everything DDC cannot drive.

    The animator calls this on every frame of a fade, so the DDC verdict is
    memoised rather than re-probed. _schedule_reassert() refreshes it on a
    screen-parameter change, which is the only time the answer can move."""
    global _ddc_ids
    with _ddc_lock:
        if _ddc_ids is None or refresh:
            _ddc_ids = ddc_capable_ids(refresh=refresh)
        skip = _ddc_ids
    return [d for d in externals() if d not in skip]

def apply_ddc(pct):
    """Set the real backlight wherever it works. Returns the IDs handled."""
    if _ddc is None:
        return set()
    done, avs = set(), _ddc.av_displays()
    try:
        for did in externals():
            m = _ddc.match_cg_display(did, avs)
            if m is None:
                continue
            p = _ddc.probe(m)
            if p.get("capable") and m.set_brightness(pct, maximum=p.get("max")):
                done.add(did)
    except Exception:
        pass                               # fall through to gamma for everything
    finally:
        for x in avs:
            x.close()
    return done

def set_gamma(pct):
    v = max(0.05, min(1.0, pct / 100.0))
    for d in gamma_targets():
        cg.CGSetDisplayTransferByFormula(d, f(0),f(v),f(1), f(0),f(v),f(1), f(0),f(v),f(1))

def get_gamma(d):
    """Read the ramp back -- the only honest way to prove a change landed."""
    vals = [f() for _ in range(9)]
    cg.CGGetDisplayTransferByFormula(d, *[ctypes.byref(x) for x in vals])
    return round(vals[1].value * 100, 1)          # redMax, as a percentage

def read_level():
    try:    return float(open(LEVEL).read().strip())
    except: return 100.0

# --- daemon discovery: every daemon, not just the pidfile's ------------------
def daemon_line_pid(line, self_path=SELF):
    """pid if this `ps -o pid=,args=` line is one of our daemons, else None.

    Pure, so it can be tested without spawning anything.

    Matching a loose substring is dangerous: any process whose command line
    merely mentions this path (a grep, an editor, a ps) would match and get
    SIGKILLed. An earlier version did exactly that.

    But argv cannot be recovered by splitting either. ps joins argv with spaces
    and gives no way back to the original element boundaries, so a script path
    containing a space -- `~/Experiment Projects/...` -- shreds into two
    elements and an exact-element match can NEVER hit. That silently disabled
    kill_all(): `dim reset` could not stop its own daemon, and holders piled up
    one per invocation, each fighting the others over the gamma table.

    So anchor on the exact ` <SELF> --daemon` suffix. Spaces survive, and the
    safety property holds: a grep or editor merely mentioning the path does not
    end with it."""
    head, _, rest = line.strip().partition(" ")
    try:
        pid = int(head)
    except ValueError:
        return None
    suffix = f" {self_path} --daemon"
    if not rest.endswith(suffix):
        return None
    interp = rest[:-len(suffix)]
    if "python" not in os.path.basename(interp).lower():
        return None                        # not an interpreter -> not ours
    return pid


def daemon_pids():
    """Every daemon of ours that is currently running, not just the pidfile's.

    -ww stops ps truncating the command line, which would otherwise chop the
    ` --daemon` suffix off a long interpreter path and hide the process."""
    out = subprocess.run(["ps", "-e", "-ww", "-o", "pid=,args="],
                         capture_output=True, text=True).stdout
    me = os.getpid()
    pids = []
    for line in out.splitlines():
        pid = daemon_line_pid(line)
        if pid is not None and pid != me:
            pids.append(pid)
    return pids

def kill_all():
    """SIGTERM, then SIGKILL anything that survives. Older builds ignored
    SIGTERM entirely, so the fallback is not optional."""
    targets = daemon_pids()
    for p in targets:
        try: os.kill(p, signal.SIGTERM)
        except ProcessLookupError: pass
    if targets:
        time.sleep(0.4)
        for p in targets:
            try:
                os.kill(p, 0); os.kill(p, signal.SIGKILL)
            except (ProcessLookupError, PermissionError): pass
        time.sleep(0.2)
    return targets

# --- animation ---------------------------------------------------------------
_st   = {"current": 100.0, "target": 100.0, "gen": 0}
_lock = threading.Lock()
_wake = threading.Event()

def _ease(t):
    """Smoothstep: gentle departure and arrival, full use of the duration."""
    return t * t * (3.0 - 2.0 * t)

def request(target):
    with _lock:
        _st["target"] = target; _st["gen"] += 1
    _wake.set()

def reassert():
    with _lock:
        t = _st["target"]; _st["current"] = t
    set_gamma(t)

_reassert_lock   = threading.Lock()
_reassert_active = False

def _schedule_reassert():
    """Re-apply after a screen-parameter change. Two things make a single
    immediate write insufficient: the notification fires more than once per
    event, and macOS installs its own default ramp as the display settles --
    last writer wins. So re-assert a bounded handful of times, debounced.
    Event-triggered and finite; never a poll loop."""
    global _reassert_active
    with _reassert_lock:
        if _reassert_active:
            return                       # already covering this event
        _reassert_active = True

    def worker():
        global _reassert_active
        try:
            gamma_targets(refresh=True)   # a swapped monitor can change backend
            for delay in (0.2, 0.8, 2.0, 4.0):
                time.sleep(delay)
                reassert()
                log(f"reassert(+{delay}s) target={read_level()} "
                    f"readback={[get_gamma(d) for d in gamma_targets()]}")
        finally:
            with _reassert_lock:
                _reassert_active = False
    threading.Thread(target=worker, daemon=True).start()

def _animator():
    while True:
        _wake.wait(); _wake.clear()                  # parked: 0% CPU
        with _lock:
            gen, start, end = _st["gen"], _st["current"], _st["target"]
        if abs(end - start) < 0.05:
            set_gamma(end)
            with _lock: _st["current"] = end
            continue
        steps = max(1, int(TRANSITION * FPS)); t0 = time.perf_counter()
        for i in range(1, steps + 1):
            with _lock:
                if _st["gen"] != gen: break          # superseded; restart
            cur = start + (end - start) * _ease(i / steps)
            set_gamma(cur)
            with _lock: _st["current"] = cur
            slack = t0 + (i / steps) * TRANSITION - time.perf_counter()
            if slack > 0: time.sleep(slack)
        else:
            set_gamma(end)
            with _lock: _st["current"] = end

# --- control channel ---------------------------------------------------------
def ensure_fifo():
    """The channel must be a real FIFO. If it is missing, open() would raise
    forever; if it is a regular file, open() returns EOF instantly and the
    read loop spins at full tilt. Both are silent busy-loops -- so repair the
    node rather than tolerating either."""
    if os.path.exists(FIFO) and not stat.S_ISFIFO(os.stat(FIFO).st_mode):
        os.remove(FIFO)
    if not os.path.exists(FIFO):
        os.mkfifo(FIFO, 0o600)

def _reader():
    """Blocking FIFO reads. Needs no interpreter attention, so it works even
    though the main thread is parked forever inside NSApp.run().

    No branch here may spin: every failure path either repairs the node or
    gives up and exits, with bounded backoff in between.
    """
    fails = 0
    while True:
        try:
            ensure_fifo()
            with open(FIFO, "r") as fh:              # blocks until a writer opens
                for line in fh:
                    msg = line.strip().split()
                    if not msg: continue
                    if msg[0] == "level" and len(msg) > 1:
                        request(float(msg[1]))
                    elif msg[0] == "quit":
                        request(100.0)
                        time.sleep(TRANSITION + 0.1)  # let the fade finish
                        os._exit(0)                   # immediate; no unwinding
            fails = 0                                 # a clean cycle resets
        except Exception:
            fails += 1
            if fails >= 5:
                os._exit(1)          # unrecoverable: exit rather than spin.
                                     # macOS restores gamma on process death.
            time.sleep(0.5 * fails)  # bounded backoff, never a tight loop

def send(msg):
    """Write a control message. False means nobody is listening."""
    try:
        fd = os.open(FIFO, os.O_WRONLY | os.O_NONBLOCK)   # ENXIO if no reader
    except OSError:
        return False
    try:
        os.write(fd, (msg + "\n").encode()); return True
    finally:
        os.close(fd)

# --- daemon ------------------------------------------------------------------
def run_daemon():
    signal.signal(signal.SIGTERM, signal.SIG_DFL)   # let the kernel kill us
    signal.signal(signal.SIGHUP,  signal.SIG_IGN)
    open(PID, "w").write(str(os.getpid()))

    ensure_fifo()

    threading.Thread(target=_animator, daemon=True).start()
    threading.Thread(target=_reader,   daemon=True).start()
    request(read_level())

    # AppKit, imported here rather than at module scope: only the daemon needs
    # pyobjc, and the CLI runs under the system interpreter which lacks it.
    import AppKit, objc

    class _Obs(AppKit.NSObject):
        def screensChanged_(self, note):
            log(f"screens changed -> {len(AppKit.NSScreen.screens())} screen(s)")
            _schedule_reassert()

    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyProhibited)
    obs = _Obs.alloc().init()
    globals()["_obs_ref"] = obs                     # keep alive
    AppKit.NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
        obs, objc.selector(_Obs.screensChanged_, signature=b"v@:@"),
        AppKit.NSApplicationDidChangeScreenParametersNotification, None)
    log(f"daemon start: observer registered, externals={externals()}")
    # NSApp.run(), NOT CFRunLoopRun(): CFRunLoopRun does not pump AppKit's
    # event loop, so screen-parameter notifications are never delivered.
    # CGDisplayRegisterReconfigurationCallback registers fine (rc=0) but is
    # likewise never delivered in an unbundled Python process -- verified.
    app.run()
    log("NSApp.run() RETURNED -- event loop died")

def stop():
    if not send("quit"):
        kill_all()
    else:
        time.sleep(TRANSITION + 0.35)
        kill_all()                                  # belt and braces
    for x in (PID, LEVEL, FIFO):
        try: os.remove(x)
        except FileNotFoundError: pass
    cg.CGDisplayRestoreColorSyncSettings()

# --- diagnostics -------------------------------------------------------------
def probe_report(refresh=False):
    """Walk the low-level display path and report what is actually true.

    Exists because every layer of this stack reports success while doing
    nothing: IOAVServiceWriteI2C returns 0 against a display that ignores the
    write, and a stub responder returns a well-formed empty reply. The only
    honest evidence is parsed data, so that is what this prints."""
    print(f"IOAVService     : {'available' if (_ddc and _ddc.AVAILABLE) else 'MISSING'}")
    if _ddc is None:
        print("ddc.py not importable -- gamma is the only backend")
        return 1
    if not _ddc.AVAILABLE:
        print("this macOS build does not export the private I2C symbols")
        return 1
    avs = _ddc.av_displays()
    print(f"external AV svcs: {len(avs)}   (DCPAVServiceProxy, Location=External)")
    ext = externals()
    if not avs or not ext:
        print("no external display attached, or the DCP exposes no I2C endpoint")
        for x in avs:
            x.close()
        return 1
    rc = 1
    try:
        for did in ext:
            m = _ddc.match_cg_display(did, avs)
            print(f"\ndisplay {did}{' (asleep)' if cg.CGDisplayIsAsleep(did) else ''}")
            if m is None or not m.info:
                print("  EDID        : unreadable -- I2C is not reaching this display")
                print("  backend     : gamma")
                continue
            i = m.info
            print(f"  EDID        : {i['name'] or '?'}  mfg={i['mfg']} "
                  f"product=0x{i['product']:04x} serial=0x{i['serial']:08x}")
            p = _ddc.probe(m, refresh=refresh)
            print(f"  DDC/CI 0x37 : {'YES' if p.get('capable') else 'no'} -- {p['reason']}")
            osv = _ddc.os_can_change_brightness(did)
            print(f"  macOS says  : CanChangeBrightness={osv}"
                  + ("   <- DISAGREES with the probe above" 
                     if osv is not None and osv != bool(p.get('capable')) else ""))
            if p.get("capable"):
                rc = 0
                print(f"  brightness  : {m.get_brightness()}%  "
                      f"(VCP 0x10, max {p.get('max')})")
                print("  backend     : hardware backlight -- no daemon needed")
            else:
                print("  backend     : gamma -- software dimming, daemon required")
    finally:
        for x in avs:
            x.close()
    return rc


if __name__ == "__main__":
    a = sys.argv[1:]
    if a and a[0] == "--daemon":
        run_daemon(); sys.exit(0)

    if a and a[0] == "probe":
        sys.exit(probe_report(refresh=("-r" in a or "--refresh" in a)))

    if not a or a[0] in ("status", "-h", "--help"):
        live = daemon_pids()
        print(f"daemon  : {'running pid ' + str(live[0]) if live else 'not running'}")
        if len(live) > 1: print(f"  WARNING: {len(live)} daemons running: {live}")
        print(f"level   : {read_level():g}% (requested)")
        hw = ddc_capable_ids()
        for d in externals():
            zzz = " (asleep)" if cg.CGDisplayIsAsleep(d) else ""
            if d in hw:
                print(f"backlight: display {d} on DDC/CI{zzz}   <- hardware, monitor stores the level")
            else:
                print(f"gamma   : display {d} reports {get_gamma(d):g}%{zzz}   <- read back from CoreGraphics")
        print(f"fade    : {TRANSITION*1000:.0f} ms, smoothstep (gamma only)")
        print(f"external: {externals() or 'none'}")
        print("\nusage: dim <5-100> | dim reset | dim status | dim probe")
        sys.exit(0)

    if a[0] in ("reset", "off", "stop"):
        apply_ddc(100.0)                            # real backlight back to full
        stop(); print("faded back to 100% (daemon stopped)"); sys.exit(0)

    try: pct = max(5.0, min(100.0, float(a[0])))
    except ValueError: sys.exit(f"bad level: {a[0]}")
    if not externals(): sys.exit("no external display found")
    open(LEVEL, "w").write(str(pct))

    handled    = apply_ddc(pct)                     # hardware backlight first
    need_gamma = [d for d in externals() if d not in handled]

    if need_gamma:
        if not send(f"level {pct}"):                # no daemon listening
            kill_all()                              # clear any zombie holders
            subprocess.Popen(["nohup", VENV_PY if os.path.exists(VENV_PY) else sys.executable, SELF, "--daemon"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL, start_new_session=True)
            time.sleep(0.6)
    elif daemon_pids():
        # Every display is on its own backlight now, so the gamma holder has
        # nothing left to hold -- and a stale ramp would darken a display the
        # monitor is already dimming. Retire it.
        if not send("quit"):
            kill_all()

    if handled and need_gamma:
        print(f"external display -> {pct:g}%  "
              f"({len(handled)} backlight, {len(need_gamma)} gamma)")
    elif handled:
        print(f"external display -> {pct:g}%  (hardware backlight)")
    else:
        print(f"external display -> {pct:g}%")
