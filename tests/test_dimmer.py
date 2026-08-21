#!/usr/bin/env python3
"""Regression tests for dimmer.py.

Every brightness assertion reads the ramp BACK from CoreGraphics. Asserting on
the PID or on CPU usage is exactly what let a completely non-functional
level-change path pass as "working" -- never trust anything but the readback.

Requires an external display connected.
Run from the repo root:  python3 tests/test_dimmer.py

Resolves dimmer.py from $DIMMER_PATH, else the repo checkout, else ~/bin.
"""
import ctypes.util, importlib.util, os, signal, subprocess, sys, time

def _find_dimmer():
    env = os.environ.get("DIMMER_PATH")
    if env and os.path.exists(env):
        return env
    here = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dimmer.py")
    if os.path.exists(here):
        return here
    installed = os.path.expanduser("~/bin/dimmer.py")
    if os.path.exists(installed):
        return installed
    sys.exit("cannot locate dimmer.py; set DIMMER_PATH")

DIM  = _find_dimmer()
spec = importlib.util.spec_from_file_location("dm", DIM)
dm   = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dm)

FADE   = dm.TRANSITION + 0.9      # generous settle window
FAILED = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""))
    if not cond:
        FAILED.append(name)


def dim(*args):
    return subprocess.run([sys.executable, DIM, *map(str, args)],
                          capture_output=True, text=True).stdout.strip()


def gamma():
    ext = dm.externals()
    return dm.get_gamma(ext[0]) if ext else None


def near(a, b, tol=1.0):
    return a is not None and abs(a - b) <= tol


# ---------------------------------------------------------------- preflight
if not dm.externals():
    sys.exit("SKIP: no external display connected; these tests need one.")

subprocess.run(["caffeinate", "-u", "-t", "2"], capture_output=True)
time.sleep(1.5)

print("\n=== dimmer.py regression tests ===\n")
print("-- clean slate --")
dim("reset")
time.sleep(0.5)
check("no daemons after reset", len(dm.daemon_pids()) == 0, f"{dm.daemon_pids()}")
check("gamma restored to 100%", near(gamma(), 100.0), f"readback={gamma()}")

# ------------------------------------------------------- level application
print("\n-- level application (readback, not PID) --")
dim(40)
time.sleep(FADE)
g = gamma()
check("dim 40 -> gamma 40%", near(g, 40.0), f"readback={g}")
pids = dm.daemon_pids()
check("exactly one daemon spawned", len(pids) == 1, f"{pids}")
first_pid = pids[0] if pids else None

# This is the regression that shipped broken: retuning a LIVE daemon.
# Signal-based IPC never ran while the main thread sat in CFRunLoopRun, so the
# level file updated, the PID persisted, and the gamma never moved.
print("\n-- live-daemon retune (the regression) --")
for want in (85, 15, 60):
    dim(want)
    time.sleep(FADE)
    g = gamma()
    check(f"dim {want} -> gamma {want}%", near(g, want), f"readback={g}")
pids = dm.daemon_pids()
check("still exactly one daemon", len(pids) == 1, f"{pids}")
check("same daemon reused (no respawn)", pids == [first_pid], f"{pids} vs {[first_pid]}")

# ------------------------------------------------------------ interruption
print("\n-- rapid fire / supersede --")
for v in (100, 30, 90, 20, 75, 45):
    dim(v)
time.sleep(FADE + 0.5)
g = gamma()
check("lands on final value (45%)", near(g, 45.0, 2.0), f"readback={g}")
check("no daemon storm", len(dm.daemon_pids()) == 1, f"{dm.daemon_pids()}")

# ---------------------------------------------------------- process safety
print("\n-- process-matching safety --")
decoy = subprocess.Popen(["sleep", "30", "--daemon", DIM],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(0.4)
found = dm.daemon_pids()
check("decoy with --daemon + path in argv is NOT matched",
      decoy.pid not in found, f"decoy={decoy.pid} matched={found}")
decoy.kill()
decoy.wait()

# ------------------------------------------------------- FIFO loss recovery
print("\n-- FIFO loss recovery --")
# Deleting the FIFO leaves the daemon's reader blocked on the old inode, so it
# cannot be reached. The client must notice (ENXIO/ENOENT on send), clear the
# stale holder and spawn a fresh daemon. Verify recovery, not immortality.
try:
    os.remove(dm.FIFO)
except FileNotFoundError:
    pass
dim(70)
time.sleep(FADE + 0.5)
g = gamma()
check("recovers after FIFO deletion", near(g, 70.0), f"readback={g}")
check("exactly one daemon after recovery", len(dm.daemon_pids()) == 1, f"{dm.daemon_pids()}")


# ------------------------------------------------- display reconfiguration
print("\n-- display reconfiguration (screen-parameter change) --")
# Was untestable until a programmatic trigger existed. macOS installs its own
# default ramp when the display re-settles, so the daemon must notice and
# re-assert. This is the bug that survived to production: CFRunLoopRun never
# pumped AppKit's loop, so the notification was never delivered and the level
# was silently lost on every unplug/replug or resolution change.
def switch_mode_and_back():
    import ctypes
    cgl = ctypes.CDLL(ctypes.util.find_library('ApplicationServices'))
    cfl = ctypes.CDLL(ctypes.util.find_library('CoreFoundation'))
    vp = ctypes.c_void_p
    cgl.CGDisplayCopyAllDisplayModes.argtypes = [ctypes.c_uint32, vp]
    cgl.CGDisplayCopyAllDisplayModes.restype = vp
    cgl.CGDisplayCopyDisplayMode.argtypes = [ctypes.c_uint32]
    cgl.CGDisplayCopyDisplayMode.restype = vp
    cgl.CGDisplaySetDisplayMode.argtypes = [ctypes.c_uint32, vp, vp]
    cgl.CGDisplaySetDisplayMode.restype = ctypes.c_int32
    cgl.CGDisplayModeGetWidth.argtypes = [vp]; cgl.CGDisplayModeGetWidth.restype = ctypes.c_size_t
    cgl.CGDisplayModeGetHeight.argtypes = [vp]; cgl.CGDisplayModeGetHeight.restype = ctypes.c_size_t
    cfl.CFArrayGetCount.argtypes = [vp]; cfl.CFArrayGetCount.restype = ctypes.c_long
    cfl.CFArrayGetValueAtIndex.argtypes = [vp, ctypes.c_long]
    cfl.CFArrayGetValueAtIndex.restype = vp
    d = dm.externals()[0]
    cur = cgl.CGDisplayCopyDisplayMode(d)
    cw, ch = cgl.CGDisplayModeGetWidth(cur), cgl.CGDisplayModeGetHeight(cur)
    modes = cgl.CGDisplayCopyAllDisplayModes(d, None)
    for i in range(cfl.CFArrayGetCount(modes)):
        m = cfl.CFArrayGetValueAtIndex(modes, i)
        w, h = cgl.CGDisplayModeGetWidth(m), cgl.CGDisplayModeGetHeight(m)
        if (w, h) != (cw, ch) and w >= 1280:
            cgl.CGDisplaySetDisplayMode(d, m, None); time.sleep(2.5)
            cgl.CGDisplaySetDisplayMode(d, cur, None); time.sleep(2.5)
            return True
    return False

dim(55)
time.sleep(FADE)
if not near(gamma(), 55.0):
    check("precondition: level 55 applied", False, f"readback={gamma()}")
elif not switch_mode_and_back():
    print("  SKIP  display reconfiguration (no alternate mode available)")
else:
    time.sleep(6.0)            # bounded reasserts run out to +7s
    g = gamma()
    check("level survives a reconfiguration", near(g, 55.0, 2.0), f"readback={g}")
    check("daemon survived", len(dm.daemon_pids()) == 1, f"{dm.daemon_pids()}")

# ------------------------------------------------------------- killability
print("\n-- killability (SIGTERM must not be swallowed) --")
pids = dm.daemon_pids()
if pids:
    p = pids[0]
    os.kill(p, signal.SIGTERM)
    time.sleep(0.8)
    alive = True
    try:
        os.kill(p, 0)
    except ProcessLookupError:
        alive = False
    check("daemon exits on SIGTERM", not alive, f"pid={p}")
    time.sleep(0.4)
    check("gamma auto-restored on death", near(gamma(), 100.0), f"readback={gamma()}")
else:
    check("daemon present to kill", False, "none running")

# ----------------------------------------------------------------- teardown
print("\n-- teardown --")
dim("reset")
time.sleep(0.6)
check("no daemons after final reset", len(dm.daemon_pids()) == 0, f"{dm.daemon_pids()}")
check("gamma at 100%", near(gamma(), 100.0), f"readback={gamma()}")

print(f"\n=== {'ALL PASS' if not FAILED else str(len(FAILED)) + ' FAILED: ' + ', '.join(FAILED)} ===\n")
sys.exit(1 if FAILED else 0)
