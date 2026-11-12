"""
Update check against the GitHub releases of NB6RULES/OBRA-powersupply.

CI writes build_info.json ({"build": <run number>, "sha": ...}) next to this
file before packaging, and tags each release "build-<run number>". The app
compares its own build number with the latest release tag.

On the packaged Windows exe, install() downloads the new exe next to the
current one and hands off to a tiny batch script that waits for this
process to exit, swaps the files and relaunches.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

REPO = "NB6RULES/OBRA-powersupply"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES = f"https://github.com/{REPO}/releases/latest"
ASSET = {"win32": "PSU-Studio.exe", "linux": "PSU-Studio-linux-x86_64"}.get(
    "linux" if sys.platform.startswith("linux") else sys.platform, "PSU-Studio.exe")
HERE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def build_info():
    try:
        with open(os.path.join(HERE, "build_info.json"), encoding="utf-8") as f:
            d = json.load(f)
            return dict(build=int(d.get("build", 0)), sha=str(d.get("sha", ""))[:7])
    except Exception:
        return dict(build=0, sha="")  # running from source


def is_frozen_exe():
    return bool(getattr(sys, "frozen", False)) and (os.name == "nt" or sys.platform.startswith("linux"))


def _get(url, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": "PSU-Studio-updater",
                                               "Accept": "application/vnd.github+json"})
    return urllib.request.urlopen(req, timeout=timeout)


class Updater:
    def __init__(self):
        self.info = build_info()
        self.latest = None   # dict(build, url, notes, published, download)
        self.error = ""
        self.checked_at = 0
        self.progress = None  # 0..1 while downloading
        self.lock = threading.Lock()

    def check(self, force=False):
        if not force and self.latest and time.time() - self.checked_at < 600:
            return self.status()
        try:
            with _get(API) as r:
                d = json.load(r)
            m = re.search(r"(\d+)", d.get("tag_name", ""))
            dl = next((a["browser_download_url"] for a in d.get("assets", []) if a["name"] == ASSET), None)
            self.latest = dict(build=int(m.group(1)) if m else 0, url=d.get("html_url", RELEASES),
                               notes=(d.get("body") or "").strip()[:500],
                               published=d.get("published_at", ""), download=dl)
            self.error = ""
        except Exception as e:
            self.error = f"Couldn't check for updates: {e}"
        self.checked_at = time.time()
        return self.status()

    def status(self):
        cur = self.info["build"]
        lat = self.latest["build"] if self.latest else 0
        return dict(current=cur, sha=self.info["sha"], dev=cur == 0,
                    latest=self.latest, available=bool(cur and lat > cur),
                    can_install=is_frozen_exe() and bool(self.latest and self.latest.get("download")),
                    progress=self.progress, error=self.error)

    # ---- self-update (packaged exe only) ---------------------------------
    def install(self, on_ready_to_quit):
        if not is_frozen_exe():
            return dict(ok=False, msg="Self-update only works in the packaged PSU-Studio.exe.")
        if not (self.latest and self.latest.get("download")):
            return dict(ok=False, msg="No download found on the latest release.")
        if not self.lock.acquire(blocking=False):
            return dict(ok=False, msg="Already downloading.")
        threading.Thread(target=self._install, args=(on_ready_to_quit,), daemon=True).start()
        return dict(ok=True, msg="Downloading update...")

    def _install(self, on_ready_to_quit):
        try:
            exe = sys.executable
            new = exe + ".new"
            self.progress = 0.0
            with _get(self.latest["download"], timeout=30) as r, open(new, "wb") as f:
                total = int(r.headers.get("Content-Length") or 0)
                got = 0
                while True:
                    chunk = r.read(256 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    if total:
                        self.progress = got / total
            if os.path.getsize(new) < 1_000_000:
                raise IOError("download looks incomplete")
            if os.name != "nt":
                # Linux: a running binary can be replaced in place; start the new one
                # (it waits for the port) and let this one quit.
                os.chmod(new, 0o755)
                os.replace(new, exe)
                subprocess.Popen([exe, "--no-open", "--after-update"], start_new_session=True,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.progress = 1.0
                on_ready_to_quit()
                return
            bat = os.path.join(tempfile.gettempdir(), "psu_studio_update.bat")
            with open(bat, "w", encoding="ascii", errors="replace") as f:
                f.write(f"""@echo off
:wait
tasklist /FI "PID eq {os.getpid()}" | find "{os.getpid()}" >nul && (timeout /t 1 /nobreak >nul & goto wait)
move /y "{new}" "{exe}" >nul || (echo Update failed: could not replace the exe & pause & exit /b 1)
start "" "{exe}"
del "%~f0"
""")
            subprocess.Popen(["cmd", "/c", bat], creationflags=0x08000000)  # CREATE_NO_WINDOW
            self.progress = 1.0
            on_ready_to_quit()
        except PermissionError:
            self.error = "No permission to replace the exe here. Download it manually instead."
            self.progress = None
        except Exception as e:
            self.error = f"Update failed: {e}"
            self.progress = None
        finally:
            self.lock.release()
