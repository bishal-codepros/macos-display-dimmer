#!/usr/bin/env python3
"""Unit tests for daemon process matching. No hardware, no spawning.

Two failure modes are in tension and both have actually happened:

  too loose -- an early version matched any command line mentioning the script
               path, and SIGKILLed a `grep` that merely had it as an argument.
  too tight -- matching exact argv elements from `ps` output looks safe, but ps
               joins argv with spaces, so a path containing one shreds into two
               elements and the match can never hit. `dim reset` then cannot
               stop its own daemon and holders accumulate silently.

The space case is the regression these tests exist for.

Run:  python3 tests/test_matching.py
"""
import importlib.util, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("dm", os.path.join(ROOT, "dimmer.py"))
dm = importlib.util.module_from_spec(spec); spec.loader.exec_module(dm)

FAILED = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""))
    if not cond:
        FAILED.append(name)

PY3   = "/opt/homebrew/opt/python@3.14/bin/python3.14"
SPACY = "/Users/x/Projects/Experiment Projects/macos-display-dimmer/dimmer.py"
PLAIN = "/Users/x/bin/dimmer.py"

print("\n=== daemon process-matching tests ===\n")

print("-- the regression: a script path containing a space --")
check("daemon with a space in its path IS matched",
      dm.daemon_line_pid(f"71159 {PY3} {SPACY} --daemon", SPACY) == 71159)
check("pid parsed correctly",
      dm.daemon_line_pid(f"  482 {PY3} {SPACY} --daemon", SPACY) == 482)
check("space-free path still matched",
      dm.daemon_line_pid(f"900 {PY3} {PLAIN} --daemon", PLAIN) == 900)
check("a real venv/framework interpreter path is accepted",
      dm.daemon_line_pid(
          "37243 /opt/homebrew/Cellar/python@3.14/3.14.7/Frameworks/Python.framework"
          f"/Versions/3.14/Resources/Python.app/Contents/MacOS/Python {SPACY} --daemon",
          SPACY) == 37243)

print("\n-- safety: things that merely mention the path must NOT match --")
check("grep with --daemon and the path as args",
      dm.daemon_line_pid(f"555 /usr/bin/grep --daemon {SPACY}", SPACY) is None)
check("the decoy from the hardware suite (sleep)",
      dm.daemon_line_pid(f"556 /bin/sleep 30 --daemon {SPACY}", SPACY) is None)
check("an editor holding the file open",
      dm.daemon_line_pid(f"557 /usr/bin/vim {SPACY}", SPACY) is None)
check("non-python interpreter, right suffix",
      dm.daemon_line_pid(f"558 /usr/bin/vim {SPACY} --daemon", SPACY) is None)
check("a ps that lists it",
      dm.daemon_line_pid(f"559 /bin/ps -ef {SPACY} --daemon x", SPACY) is None)
check("the CLI itself, not the daemon",
      dm.daemon_line_pid(f"560 {PY3} {SPACY} status", SPACY) is None)
check("a DIFFERENT dimmer.py is not ours",
      dm.daemon_line_pid(f"561 {PY3} {PLAIN} --daemon", SPACY) is None)
check("path as a prefix of a longer one",
      dm.daemon_line_pid(f"562 {PY3} {SPACY}.bak --daemon", SPACY) is None)

print("\n-- malformed input --")
check("no pid", dm.daemon_line_pid(f"{PY3} {SPACY} --daemon", SPACY) is None)
check("empty line", dm.daemon_line_pid("", SPACY) is None)
check("pid only", dm.daemon_line_pid("1234", SPACY) is None)
check("truncated command line",
      dm.daemon_line_pid(f"563 {PY3} {SPACY} --dae", SPACY) is None)
check("header row", dm.daemon_line_pid("  PID ARGS", SPACY) is None)

print("\n-- live: daemon_pids() agrees with ps --")
import subprocess
raw = subprocess.run(["ps", "-e", "-ww", "-o", "pid=,args="],
                     capture_output=True, text=True).stdout
expect = {int(l.split()[0]) for l in raw.splitlines()
          if l.strip().endswith(f"{dm.SELF} --daemon")
          and "python" in l.lower() and int(l.split()[0]) != os.getpid()}
check("daemon_pids() == independent scan of ps", set(dm.daemon_pids()) == expect,
      f"got={sorted(dm.daemon_pids())} expected={sorted(expect)}")

print(f"\n=== {'ALL PASS' if not FAILED else str(len(FAILED)) + ' FAILED: ' + ', '.join(FAILED)} ===\n")
sys.exit(1 if FAILED else 0)
