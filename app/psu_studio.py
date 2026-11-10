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

import device as dev_mod  # noqa: E402
import driver  # noqa: E402
import importers  # noqa: E402
import updater  # noqa: E402

WEB = os.path.join(HERE, "web")
DOCS = os.path.join(os.path.expanduser("~"), "Documents", "PSU Studio")
SETTINGS = os.path.join(DOCS, "settings.json")
VERSION = "1.0.0"


class Api:
    def __init__(self, sim=False):
        self._dev = dev_mod.Device()
        self._seq = dev_mod.Sequencer(self._dev)
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
            return self.connect(self._dev.link.port)
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
        self.stop_record()
        self._dev.disconnect()
        return dict(ok=True)

    # ---- control --------------------------------------------------------
    def _do(self, fn, *a):
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
        return self._do(self._dev.set_output, bool(on))

    def apply_preset(self, p):
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
        )

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
        os.makedirs(os.path.join(DOCS, "logs"), exist_ok=True)
        path = os.path.join(DOCS, "logs", time.strftime("log_%Y%m%d_%H%M%S.csv"))
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
        return self._upd.check(force=bool(force))

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
        try:
            if self._dev.state.get("out") and self._seq.status.get("running"):
                self._dev.set_output(False)
        except Exception:
            pass
        self.disconnect()


# ---------------------------------------------------------------------------
# Browser fallback: tiny local server, POST /api/<method> with JSON args list
# ---------------------------------------------------------------------------
def run_browser(api, port=8765):
    class H(SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=WEB, **k)

        def log_message(self, *a):
            pass

        def do_POST(self):
            if not self.path.startswith("/api/"):
                self.send_error(404)
                return
            name = self.path[5:]
            n = int(self.headers.get("Content-Length", 0))
            args = json.loads(self.rfile.read(n) or b"[]")
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

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    url = f"http://127.0.0.1:{port}/index.html?browser=1"
    print("PSU Studio running at", url, "(Ctrl+C to quit)")
    if "--no-open" not in sys.argv:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        api.shutdown()


def main():
    sim = "--sim" in sys.argv
    api = Api(sim=sim)
    if "--browser" in sys.argv:
        run_browser(api)
        return
    try:
        import webview
    except ImportError:
        print("pywebview not installed - falling back to browser mode.")
        run_browser(api)
        return
    win = webview.create_window(
        "PSU Studio", os.path.join(WEB, "index.html"), js_api=api,
        width=1360, height=880, min_size=(1000, 680), background_color="#0d0f12")
    api._window = win
    win.events.closing += lambda: api.shutdown()
    webview.start(debug="--debug" in sys.argv)


if __name__ == "__main__":
    main()
