"""
Windows self-test for the updater swap (run by .github/workflows/updater-selftest.yml).
Built as a one-file windowed exe exactly like PSU Studio. First run "downloads" a payload
exe through Updater._install (file:// URL), which must rename itself, put the payload in
place and start it. The second run logs success and cleans up the .old file.
"""
import os
import sys
import threading
import time

import updater

exe = sys.executable
LOG = os.path.join(os.path.dirname(exe), "log.txt")


def w(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%H:%M:%S')} pid={os.getpid()} {msg}\n")


def arg(name):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else None


if "--after-update" in sys.argv:
    t0 = time.time()
    updater.wait_for_pid(arg("--wait-pid"))
    w(f"second run ok, waited {time.time() - t0:.1f}s for old copy, meipass={getattr(sys, '_MEIPASS', '')}")
    updater.cleanup_leftovers()
    time.sleep(8)
    w(f"leftover .old exists: {os.path.exists(exe + '.old')}")
    sys.exit(0)

w("first run")
u = updater.Updater()
u.latest = dict(build=999, download="file:///" + arg("--payload").replace("\\", "/"))
done = threading.Event()
r = u.install(done.set)
w(f"install() -> {r}")
if not done.wait(60):
    w(f"FAILED: never got ready to quit, error={u.error!r}")
    sys.exit(1)
w(f"ready to quit, error={u.error!r}")
time.sleep(1.5)   # like the real app closing its window
os._exit(0)
