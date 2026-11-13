"""
PSU Studio - a nicer control app for the Multicomp Pro MP7111xx (OWON SPE) supplies.

  python psu_studio.py            desktop window (pywebview / WebView2)
  python psu_studio.py --browser  same UI in your browser (fallback / dev)
  add --sim to either to use the built-in simulator, no hardware needed
"""
import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

HERE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import charger as chg_mod  # noqa: E402
import device as dev_mod  # noqa: E402
import driver  # noqa: E402
import importers  # noqa: E402
import logs as logs_mod  # noqa: E402
import updater  # noqa: E402

WEB = os.path.join(HERE, "web")
DOCS = os.path.join(os.path.expanduser("~"), "Documents", "PSU Studio")
SETTINGS = os.path.join(DOCS, "settings.json")
LOGS = os.path.join(DOCS, "logs")
VERSION = "1.2.1"


class Api:
    def __init__(self, sim=False):
        self._dev = dev_mod.Device()
        self._seq = dev_mod.Sequencer(self._dev)
        self._chg = chg_mod.Charger(self._dev)
        self._chg.on_end = self._charge_ended
        self._chg_log = None      # path of the log a charge opened for itself
        self._sim = sim
        self._upd = updater.Updater()
        self._window = None

    # ---- discovery / driver ----------------------------------------
    def get_ports(self):
        ports = dev_mod.list_ports()
        ports.append(dict(device="SIM", desc="Built-in simulator (no hardware)", ch340=False))
        return ports

    def driver_status(self):
        return driver.status()

    def install_driver(self):
        return driver.install()

    def auto_connect(self):
        """Pick the first CH340 port, or the simulator in --sim mode."""
        if self._dev.link.is_open:
            # already connected (e.g. the window was reloaded): keep the link, so a running
            # charge or waveform isn't interrupted by a reconnect
            d = self._dev
            return dict(ok=True, idn=d.idn, port=d.link.port, limits=d.limits)
        if self._sim:
            return self.connect("SIM")
        for p in dev_mod.list_ports():
            if p["ch340"]:
                try:
                    return self.connect(p["device"])
                except Exception:
                    continue
        return dict(ok=False, msg="No supply found. Check USB, power and driver.")

    # ---- connection ---------------------------------------------------
    def connect(self, port):
        try:
            idn = self._dev.connect(port)
            return dict(ok=True, idn=idn, port=port, limits=self._dev.limits)
        except Exception as e:
            return dict(ok=False, msg=str(e))

    def disconnect(self):
        self._seq.stop()
        self._chg.stop("Disconnected")
        self.stop_record()
        self._dev.disconnect()
        return dict(ok=True)

    # ---- control --------------------------------------------------------
    def _do(self, fn, *a):
        if self._chg.running:
            return dict(ok=False, msg="The battery charger is in control. Stop the charge first.")
        try:
            return dict(ok=True, value=fn(*a))
        except Exception as e:
            return dict(ok=False, msg=str(e))

    def set_voltage(self, v):
        return self._do(self._dev.set_voltage, v)

    def set_current(self, i):
        return self._do(self._dev.set_current, i)

    def set_ovp(self, v):
        return self._do(self._dev.set_ovp, v)

    def set_ocp(self, i):
        return self._do(self._dev.set_ocp, i)

    def set_output(self, on):
        if self._chg.running:
            self._chg.stop()
            if on:
                return dict(ok=False, msg="Charge stopped. Press the output button again to switch it on.")
        return self._do(self._dev.set_output, bool(on))

    def apply_preset(self, p):
        if self._chg.running:
            return dict(ok=False, msg="The battery charger is in control. Stop the charge first.")
        try:
            self._dev.set_output(False) if p.get("off_first") else None
            self._dev.set_ocp(p["ocp"])
            self._dev.set_ovp(p["ovp"])
            self._dev.set_current(p["i"])
            self._dev.set_voltage(p["v"])
            return dict(ok=True)
        except Exception as e:
            return dict(ok=False, msg=str(e))

    def set_poll_rate(self, hz):
        self._dev.poll_hz = max(1, min(20, int(hz)))
        return dict(ok=True, value=self._dev.poll_hz)

    def get_state(self, seq=0):
        d = self._dev
        data, new_seq = d.samples_since(int(seq or 0))
        rec = d.recorder
        return dict(
            connected=d.link.is_open, port=d.link.port, idn=d.idn, t0=d.t0,
            limits=d.limits, state=d.state, error=d.error,
            seq=new_seq, samples=data, sequencer=self._seq.status,
            recording=dict(on=bool(rec), path=rec.path if rec else "", rows=rec.rows if rec else 0),
            meter=d.meter.status(), poll_hz=d.poll_hz, charger=self._chg.status,
        )

    # ---- battery charger --------------------------------------------------
    def charge_options(self):
        return chg_mod.options()

    def charge_plan(self, cfg):
        return chg_mod.make_plan(cfg or {}, self._dev.limits)

    def charge_start(self, cfg):
        if not self._dev.link.is_open:
            return dict(ok=False, msg="Connect to the supply first.")
        self._seq.stop()
        try:
            self._chg.start(cfg or {})
        except Exception as e:
            return dict(ok=False, msg=str(e))
        # every charge is saved to the logs folder (once a second), unless you're already recording
        plan = self._chg.plan
        if not self._dev.recorder:
            try:
                os.makedirs(LOGS, exist_ok=True)
                name = f"charge_{logs_mod.stamp_now()}_{logs_mod.safe_name(plan['label'])}.csv"
                path = os.path.join(LOGS, name)
                self._dev.recorder = dev_mod.Recorder(path, self._dev.t0, every=1.0)
                self._chg_log = path
            except OSError:
                self._chg_log = None
        target = self._chg_log or (self._dev.recorder.path if self._dev.recorder else None)
        if target and os.path.dirname(os.path.abspath(target)) == os.path.abspath(LOGS):
            desc = plan["label"] + (f", {plan['cap']:g} Ah, {plan['mode']}" if plan.get("cap") else "")
            logs_mod.update_index(LOGS, os.path.basename(target), kind="charge",
                                  title=f"Charge: {desc}", result="running", msg="")
        self._chg_target = target
        return dict(ok=True, log=os.path.basename(target) if target else "")

    def _charge_ended(self, st):
        if self._chg_log and self._dev.recorder and self._dev.recorder.path == self._chg_log:
            rec, self._dev.recorder = self._dev.recorder, None
            rec.close()
        self._chg_log = None
        t = getattr(self, "_chg_target", None)
        if t and os.path.dirname(os.path.abspath(t)) == os.path.abspath(LOGS):
            logs_mod.update_index(LOGS, os.path.basename(t), result=st.get("kind"), msg=st.get("msg"))

    # ---- saved logs -----------------------------------------------------
    def logs_list(self):
        rec = self._dev.recorder
        return dict(ok=True, folder=LOGS, logs=logs_mod.list_logs(LOGS, rec.path if rec else ""))

    def _log_path(self, name):
        name = os.path.basename(str(name or ""))
        path = os.path.join(LOGS, name)
        if not name.lower().endswith(".csv") or not os.path.isfile(path):
            raise ValueError("That log isn't there any more.")
        return path

    def log_load(self, name):
        try:
            if self._dev.recorder:
                self._dev.recorder.f.flush()
            out = logs_mod.load_file(self._log_path(name))
            info = logs_mod.read_index(LOGS).get(os.path.basename(name), {})
            out.update(title=info.get("title"), result=info.get("result"), msg=info.get("msg"))
            return dict(ok=True, **out)
        except Exception as e:
            return dict(ok=False, msg=f"Couldn't open the log: {e}")

    def log_load_text(self, name, text):
        """A CSV picked from anywhere (browser mode sends the text)."""
        try:
            return dict(ok=True, external=True, **logs_mod.parse(text, os.path.basename(name)))
        except Exception as e:
            return dict(ok=False, msg=f"Couldn't read {name}: {e}")

    def log_pick(self):
        """Desktop: pick any CSV with the Windows open dialog."""
        if self._window is None:
            return dict(ok=False, msg="browser")
        import webview
        res = self._window.create_file_dialog(webview.OPEN_DIALOG, directory=LOGS if os.path.isdir(LOGS) else DOCS,
                                              file_types=("CSV files (*.csv)", "All files (*.*)"))
        if not res:
            return dict(ok=False, msg="cancelled")
        path = res if isinstance(res, str) else res[0]
        try:
            out = logs_mod.load_file(path)
            ext = os.path.dirname(os.path.abspath(path)) != os.path.abspath(LOGS)
            return dict(ok=True, external=ext, path=path, **out)
        except Exception as e:
            return dict(ok=False, msg=f"Couldn't read {os.path.basename(path)}: {e}")

    def log_delete(self, name):
        try:
            path = self._log_path(name)
            rec = self._dev.recorder
            if rec and os.path.abspath(rec.path) == os.path.abspath(path):
                return dict(ok=False, msg="That log is still being recorded. Stop it first.")
            os.remove(path)
            logs_mod.drop_from_index(LOGS, os.path.basename(path))
            return dict(ok=True)
        except Exception as e:
            return dict(ok=False, msg=str(e))

    def log_rename(self, name, title):
        try:
            path = self._log_path(name)
            logs_mod.update_index(LOGS, os.path.basename(path), title=str(title).strip()[:80] or None)
            return dict(ok=True)
        except Exception as e:
            return dict(ok=False, msg=str(e))

    def charge_stop(self):
        self._chg.stop()
        return dict(ok=True)

    # ---- power meter ----------------------------------------------------
    def meter_enable(self, on):
        self._dev.meter.set_enabled(on)
        return self._dev.meter.status()

    def meter_reset(self):
        self._dev.meter.reset()
        return self._dev.meter.status()

    # ---- console --------------------------------------------------------
    def scpi(self, cmd):
        cmd = (cmd or "").strip()
        if not cmd:
            return dict(ok=False)
        try:
            if cmd.endswith("?"):
                return dict(ok=True, reply=self._dev.link.query(cmd))
            self._dev.link.write(cmd)
            return dict(ok=True, reply="")
        except Exception as e:
            return dict(ok=False, msg=str(e))

    def get_console(self):
        return [dict(t=t, d=d, s=s) for (t, d, s) in list(self._dev.link.log)[-200:]]

    # ---- sequencer ------------------------------------------------------
    def seq_start(self, steps, opts):
        if not self._dev.link.is_open:
            return dict(ok=False, msg="Connect to the supply first.")
        if self._chg.running:
            return dict(ok=False, msg="A battery charge is running. Stop it first.")
        lim = self._dev.limits
        bad = [k for k, s in enumerate(steps) if s.get("en", True) and (
            float(s["v"]) > lim["vmax"] or float(s["i"]) > lim["imax"] or float(s["v"]) < 0 or float(s["i"]) < 0)]
        if bad:
            return dict(ok=False, msg=f"Step {bad[0] + 1} is outside {lim['vmax']} V / {lim['imax']} A.")
        for n, s in enumerate(steps):
            s["n"] = n
        try:
            self._seq.start(steps, loops=opts.get("loops", 1),
                           start=int(opts.get("start", 0)),
                           end=opts.get("end"),
                           output_on=opts.get("output_on", True),
                           output_off_at_end=opts.get("output_off", True))
            return dict(ok=True)
        except Exception as e:
            return dict(ok=False, msg=str(e))

    def seq_stop(self):
        self._seq.stop()
        return dict(ok=True)

    # ---- files ----------------------------------------------------------
    def read_table_b64(self, name, b64):
        """UI sends the picked file as base64; returns sheets of raw cells."""
        suffix = os.path.splitext(name)[1] or ".csv"
        fd, path = tempfile.mkstemp(suffix=suffix)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(base64.b64decode(b64))
            return dict(ok=True, **importers.read_table(path, name))
        except Exception as e:
            return dict(ok=False, msg=f"Couldn't read {name}: {e}")
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    def _save_path(self, default_name):
        os.makedirs(DOCS, exist_ok=True)
        if self._window is None:
            return os.path.join(DOCS, default_name)
        import webview
        ext = os.path.splitext(default_name)[1].lower()
        types = {".csv": ("CSV files (*.csv)",), ".jpg": ("JPEG images (*.jpg;*.jpeg)",)}.get(ext, ())
        res = self._window.create_file_dialog(
            webview.SAVE_DIALOG, directory=DOCS, save_filename=default_name, file_types=types)
        if not res:
            return None
        return res if isinstance(res, str) else res[0]

    def save_text(self, default_name, text):
        path = self._save_path(default_name)
        if not path:
            return dict(ok=False, msg="cancelled")
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        return dict(ok=True, path=path)

    def save_binary(self, default_name, b64):
        """Used for chart JPEGs: the page sends a base64 data payload."""
        path = self._save_path(default_name)
        if not path:
            return dict(ok=False, msg="cancelled")
        with open(path, "wb") as f:
            f.write(base64.b64decode(b64))
        return dict(ok=True, path=path)

    def start_record(self):
        if self._dev.recorder:
            return dict(ok=True, path=self._dev.recorder.path)
        os.makedirs(LOGS, exist_ok=True)
        path = os.path.join(LOGS, time.strftime("log_%Y%m%d_%H%M%S.csv"))
        self._dev.recorder = dev_mod.Recorder(path, self._dev.t0)
        return dict(ok=True, path=path)

    def stop_record(self):
        rec, self._dev.recorder = self._dev.recorder, None
        if rec:
            rec.close()
            return dict(ok=True, path=rec.path, rows=rec.rows)
        return dict(ok=True)

    def open_folder(self, path=None):
        target = path or DOCS
        if os.path.isfile(target):
            target = os.path.dirname(target)
        os.makedirs(target, exist_ok=True)
        if os.name == "nt":
            os.startfile(target)  # noqa
        elif sys.platform == "darwin":
            subprocess.Popen(["open", target])
        else:
            subprocess.Popen(["xdg-open", target])
        return dict(ok=True)

    # ---- settings -------------------------------------------------------
    def load_settings(self):
        try:
            with open(SETTINGS, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def save_settings(self, data):
        os.makedirs(DOCS, exist_ok=True)
        tmp = SETTINGS + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, SETTINGS)
        return dict(ok=True)

    def info(self):
        b = self._upd.info
        return dict(version=VERSION, build=b["build"], sha=b["sha"], sim=self._sim, docs=DOCS, platform=sys.platform)

    # ---- updates --------------------------------------------------------
    def check_update(self, force=False):
        return dict(self._upd.check(force=bool(force)), version=VERSION,
                    just_updated="--after-update" in sys.argv)

    def update_status(self):
        return self._upd.status()

    def open_url(self, url):
        if not str(url).startswith("https://github.com/"):
            return dict(ok=False)
        webbrowser.open(url)
        return dict(ok=True)

    def install_update(self):
        def quit_app():
            try:
                self.shutdown()
            finally:
                if self._window is not None:
                    self._window.destroy()
                else:
                    os._exit(0)
        return self._upd.install(quit_app)

    def shutdown(self):
        self._chg.stop("App closed")
        try:
            if self._dev.state.get("out") and self._seq.status.get("running"):
                self._dev.set_output(False)
        except Exception:
            pass
        self.disconnect()


# ---------------------------------------------------------------------------
# Browser fallback: tiny local server, POST /api/<method> with JSON args list
# ---------------------------------------------------------------------------
APP_BROWSERS = ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
                "microsoft-edge", "microsoft-edge-stable", "brave-browser", "vivaldi"]


def _already_running(port):
    """True if another PSU Studio already owns the port."""
    import urllib.request
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/info", data=b"[]", method="POST")
        with urllib.request.urlopen(req, timeout=1.5) as r:
            return "version" in json.load(r)
    except Exception:
        return False


def _open_window(url):
    """
    Open the UI. Prefer a Chromium-family browser in --app mode with its own
    profile: that gives a real app window, and the process exits when the
    window closes so we know to quit. Falls back to the default browser.
    Returns the Popen of the app window, or None.
    """
    import shutil
    if "--no-app" not in sys.argv:
        for name in APP_BROWSERS:
            exe = shutil.which(name)
            if not exe:
                continue
            prof = os.path.join(os.path.expanduser("~"), ".config", "psu-studio", "window")
            os.makedirs(prof, exist_ok=True)
            try:
                return subprocess.Popen(
                    [exe, f"--app={url}", f"--user-data-dir={prof}", "--no-first-run",
                     "--no-default-browser-check", "--window-size=1360,880", "--class=PSU Studio"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            except Exception:
                continue
    webbrowser.open(url)
    return None


def run_browser(api, port=8765):
    last_call = [0.0]
    bye_at = [0.0]

    class H(SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=WEB, **k)

        def log_message(self, *a):
            pass

        def end_headers(self):
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

        def do_POST(self):
            if not self.path.startswith("/api/"):
                self.send_error(404)
                return
            name = self.path[5:]
            n = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(n)
            if name == "bye":  # page closed (sendBeacon); quit unless it comes back
                bye_at[0] = time.time()
                self.send_response(204)
                self.end_headers()
                return
            last_call[0] = time.time()
            args = json.loads(raw or b"[]")
            fn = getattr(api, name, None)
            if not fn or name.startswith("_"):
                self.send_error(404)
                return
            try:
                out = fn(*args)
            except Exception as e:
                out = dict(ok=False, msg=str(e))
            body = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    url = f"http://127.0.0.1:{port}/index.html?browser=1"
    # bind, retrying for a while (after a self-update the old copy may still be exiting)
    srv = None
    deadline = time.time() + (15 if "--after-update" in sys.argv else 2)
    while srv is None:
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), H)
        except OSError:
            if _already_running(port):
                print("PSU Studio is already running - opening it.")
                _open_window(url)
                return
            if time.time() > deadline:
                raise
            time.sleep(0.5)
    srv.daemon_threads = True
    print("PSU Studio running at", url, "(Ctrl+C to quit)")

    def quit_later(why):
        print("Closing:", why)
        threading.Thread(target=srv.shutdown, daemon=True).start()

    win = [None]
    opened_at = time.time()
    if "--no-open" not in sys.argv:
        win[0] = _open_window(url)

    def lifecycle():
        fallback_pending = False
        while True:
            time.sleep(1)
            w = win[0]
            if w is not None and w.poll() is not None:
                if time.time() - opened_at < 5:
                    # exited at once: Chrome handed off to an instance already running with this
                    # profile, or couldn't start. Keep serving; open the default browser if no page shows up.
                    win[0] = None
                    fallback_pending = True
                else:
                    return quit_later("window closed")
            if fallback_pending and time.time() - opened_at > 7:
                fallback_pending = False
                if last_call[0] == 0:
                    webbrowser.open(url)
            # page sent "bye" and nothing has called in since (a reload calls back within a second)
            if bye_at[0] and bye_at[0] >= last_call[0] and time.time() - bye_at[0] > 8:
                return quit_later("page closed")
    if "--no-open" not in sys.argv or "--after-update" in sys.argv:
        threading.Thread(target=lifecycle, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        api.shutdown()


def _arg(name):
    try:
        return sys.argv[sys.argv.index(name) + 1]
    except (ValueError, IndexError):
        return None


def main():
    sim = "--sim" in sys.argv
    if "--wait-pid" in sys.argv:
        updater.wait_for_pid(_arg("--wait-pid"))
    updater.cleanup_leftovers()
    api = Api(sim=sim)
    # Linux/macOS builds don't bundle a GUI toolkit: serve the UI to a browser window instead
    if "--browser" in sys.argv or (sys.platform != "win32" and "--webview" not in sys.argv):
        run_browser(api)
        return
    try:
        import webview
    except ImportError:
        print("pywebview not installed - falling back to browser mode.")
        run_browser(api)
        return
    win = webview.create_window(
        f"PSU Studio {VERSION}", os.path.join(WEB, "index.html"), js_api=api,
        width=1360, height=880, min_size=(1000, 680), background_color="#0d0f12")
    api._window = win
    win.events.closing += lambda: api.shutdown()
    webview.start(debug="--debug" in sys.argv)
    # window gone: make sure the process really ends (a background thread must not keep
    # it alive, or the next start / update would find it still running)
    try:
        api.shutdown()
    except Exception:
        pass
    os._exit(0)


if __name__ == "__main__":
    main()
