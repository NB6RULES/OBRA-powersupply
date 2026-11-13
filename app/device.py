"""
Serial / SCPI link to the Multicomp Pro SPE series (MP711127..MP711131).
These are rebadged OWON SPE supplies: CH340 USB-serial, 115200 8N1,
commands end with \\n, replies end with \\r\\n. No list mode over SCPI,
so sequences are run PC-side by the Sequencer below.
"""
import random
import re
import threading
import time
from collections import deque

try:
    import serial
    import serial.tools.list_ports
except ImportError:  # pragma: no cover
    serial = None

CH34X_VID = 0x1A86
CH34X_PIDS = {0x7523, 0x7522, 0x5523, 0x55D4, 0x55D3}

# Known limits by model. Anything unknown falls back to MP711128.
MODELS = {
    "MP711127": dict(vmax=30, imax=5, pmax=150, ovp=31, ocp=5.1),
    "MP711128": dict(vmax=30, imax=10, pmax=200, ovp=31, ocp=10.1),
    "MP711129": dict(vmax=60, imax=10, pmax=200, ovp=61, ocp=10.1),
    "MP711130": dict(vmax=60, imax=5, pmax=300, ovp=61, ocp=5.1),
    "MP711131": dict(vmax=60, imax=10, pmax=300, ovp=61, ocp=10.1),
}
DEFAULT_MODEL = "MP711128"


def guess_limits(idn: str):
    """Map an OWON IDN string (e.g. 'OWON,SPE3103,...') to limits."""
    m = re.search(r"SPE(\d)(\d)(\d)(\d)", idn or "")
    if not m:
        return dict(MODELS[DEFAULT_MODEL])
    # OWON naming: SPE<V/10><amps:2 digits><rev>, e.g. SPE3103 = 30 V / 10 A
    vmax = int(m.group(1)) * 10
    imax = int(m.group(2) + m.group(3))
    for lim in MODELS.values():
        if lim["vmax"] == vmax and lim["imax"] == imax:
            return dict(lim)
    return dict(MODELS[DEFAULT_MODEL])


def list_ports():
    out = []
    if serial is None:
        return out
    for p in serial.tools.list_ports.comports():
        is_ch = p.vid == CH34X_VID
        out.append(dict(device=p.device, desc=p.description or "",
                        ch340=bool(is_ch), vid=p.vid, pid=p.pid))
    out.sort(key=lambda d: (not d["ch340"], d["device"]))
    return out


class SimBattery:
    """
    Very rough battery for the simulator: open-circuit voltage from state of
    charge, plus internal resistance. Time runs `speed` times faster so a
    charge finishes in minutes instead of hours.
    """
    # per-cell OCV vs state of charge
    CURVES = {
        "lead": [(0, 1.80), (0.2, 1.95), (0.5, 2.05), (0.8, 2.15), (0.95, 2.30), (1.0, 2.43)],
        "gel": [(0, 1.80), (0.2, 1.95), (0.5, 2.05), (0.8, 2.15), (0.95, 2.28), (1.0, 2.38)],
        "lfp": [(0, 2.60), (0.08, 3.15), (0.5, 3.28), (0.9, 3.35), (0.97, 3.45), (1.0, 3.62)],
        "liion": [(0, 3.05), (0.1, 3.50), (0.5, 3.72), (0.85, 4.00), (0.95, 4.12), (1.0, 4.21)],
    }
    R_CELL = {"lead": 0.012, "gel": 0.015, "lfp": 0.01, "liion": 0.04}

    def __init__(self, chem, cells, cap_ah, soc=0.25, speed=60):
        self.chem = chem if chem in self.CURVES else "lead"
        self.cells = max(1, cells)
        self.cap = max(0.05, cap_ah)
        self.soc = soc
        self.speed = speed
        self.r = self.R_CELL[self.chem] * self.cells * (10 / max(1.0, self.cap)) ** 0.5
        self._t = time.monotonic()

    def ocv(self):
        pts = self.CURVES[self.chem]
        s = min(1.0, max(0.0, self.soc))
        for (s0, v0), (s1, v1) in zip(pts, pts[1:]):
            if s <= s1:
                return self.cells * (v0 + (v1 - v0) * (s - s0) / (s1 - s0))
        return self.cells * pts[-1][1]

    def advance(self, i):
        now = time.monotonic()
        dt, self._t = min(1.0, now - self._t), now
        self.soc = min(1.0, self.soc + i * dt * self.speed / 3600 / self.cap * 0.95)


class SimPSU:
    """Pretend supply so the UI can be used/tested without hardware."""

    def __init__(self):
        self.v_set, self.i_set = 5.0, 1.0
        self.ovp, self.ocp = 31.0, 10.1
        self.out = False
        self.load_ohms = 4.7
        self.trip = ""
        self.battery = None
        self.speed = 60  # simulated battery time runs this much faster

    def attach_battery(self, plan):
        """Called by the charger: put a part-charged battery on the terminals."""
        chem, cells = plan["chem"], plan["cells"]
        if chem == "custom":  # guess a lead-acid bank from the charge voltage
            chem, cells = "lead", max(1, round(plan["v_charge"] / 2.4))
        self.battery = SimBattery(chem, cells, plan.get("cap") or 10, speed=self.speed)

    def detach_battery(self):
        self.battery = None

    def query(self, cmd):
        c = cmd.strip().upper()
        if c == "*IDN?":
            return "OWON,SPE3103,SIM00001,FV:V5.2.0 (SIMULATOR)"
        if c in ("VOLT?",):
            return f"{self.v_set:.3f}"
        if c in ("CURR?",):
            return f"{self.i_set:.3f}"
        if c == "VOLT:LIM?":
            return f"{self.ovp:.3f}"
        if c == "CURR:LIM?":
            return f"{self.ocp:.3f}"
        if c == "OUTP?":
            return "ON" if self.out else "OFF"
        v, i, mode = self._measure()
        if c == "MEAS:VOLT?":
            return f"{v:.3f}"
        if c == "MEAS:CURR?":
            return f"{i:.3f}"
        if c == "MEAS:POW?":
            return f"{v * i:.3f}"
        if c == "MEAS:ALL?":
            return f"{v:.3f},{i:.3f}"
        if c == "MEAS:ALL:INFO?":
            return f"{v:.3f},{i:.3f},{v*i:.3f},0,0,0,{mode}"
        return "ERR"

    def _measure(self):
        b = self.battery
        if b is not None:
            return self._measure_battery(b)
        if not self.out:
            return 0.0, 0.0, 0
        load = self.load_ohms * (1 + 0.02 * random.random())
        i = self.v_set / load
        if i > self.i_set:
            i = self.i_set
            v = i * load
            mode = 2
        else:
            v = self.v_set
            mode = 1
        v += random.gauss(0, 0.003)
        i += random.gauss(0, 0.002)
        if v > self.ovp or i > self.ocp:
            self.out = False
            self.trip = "OVP" if v > self.ovp else "OCP"
        return max(v, 0), max(i, 0), mode

    def _measure_battery(self, b):
        ocv = b.ocv()
        if not self.out:
            b.advance(0)
            return ocv + random.gauss(0, 0.002), 0.0, 0
        want = (self.v_set - ocv) / b.r
        if want <= 0:          # battery above the setpoint: supply can't sink current
            i, v, mode = 0.0, ocv, 1
        elif want > self.i_set:
            i, v, mode = self.i_set, ocv + self.i_set * b.r, 2
        else:
            i, v, mode = want, self.v_set, 1
        b.advance(i)
        v += random.gauss(0, 0.002)
        i = max(0.0, i + random.gauss(0, 0.0008))
        if v > self.ovp or i > self.ocp:
            self.out = False
            self.trip = "OVP" if v > self.ovp else "OCP"
        return max(v, 0), i, mode

    def write(self, cmd):
        c = cmd.strip().upper()
        try:
            if c.startswith("VOLT:LIM "):
                self.ovp = float(c.split()[1])
            elif c.startswith("CURR:LIM "):
                self.ocp = float(c.split()[1])
            elif c.startswith("VOLT "):
                self.v_set = float(c.split()[1])
            elif c.startswith("CURR "):
                self.i_set = float(c.split()[1])
            elif c.startswith("OUTP "):
                self.out = c.split()[1] in ("ON", "1")
                if self.out:
                    self.trip = ""
        except (ValueError, IndexError):
            pass

    def close(self):
        pass


class Link:
    """Thread-safe SCPI transport over pyserial (or the simulator)."""

    def __init__(self):
        self.lock = threading.RLock()
        self.ser = None
        self.sim = None
        self.port = None
        self.log = deque(maxlen=400)  # SCPI console history
        self.empty_streak = 0      # replies in a row that came back empty
        self.recovering = False    # connection lost, Device is trying to get it back

    @property
    def is_open(self):
        return self.ser is not None or self.sim is not None or self.recovering

    def reopen(self, port):
        """Drop the (probably dead) handle and open the port again, keeping our place."""
        with self.lock:
            try:
                if self.ser:
                    self.ser.close()
            except Exception:
                pass
            self.ser = None
            if port == "SIM":
                self.sim = self.sim or SimPSU()
            else:
                self.ser = serial.Serial(port, 115200, bytesize=8, parity="N", stopbits=1,
                                         timeout=0.5, write_timeout=0.5)
                time.sleep(0.15)
                self.ser.reset_input_buffer()
            self.port = port
            self.empty_streak = 0

    def drop_handle(self):
        with self.lock:
            try:
                if self.ser:
                    self.ser.close()
            except Exception:
                pass
            self.ser = None

    def open(self, port, baud=115200):
        self.close()
        with self.lock:
            if port == "SIM":
                self.sim = SimPSU()
            else:
                self.ser = serial.Serial(port, baud, bytesize=8, parity="N",
                                         stopbits=1, timeout=0.5,
                                         write_timeout=0.5)
                time.sleep(0.15)
                self.ser.reset_input_buffer()
            self.port = port

    def close(self):
        with self.lock:
            try:
                if self.ser:
                    self.ser.close()
            except Exception:
                pass
            self.ser = None
            self.sim = None
            self.port = None
            self.recovering = False

    def _note(self, direction, text):
        self.log.append((time.time(), direction, text))

    def write(self, cmd, log=True):
        with self.lock:
            if log:
                self._note(">", cmd)
            if self.sim:
                self.sim.write(cmd)
                return
            if not self.ser:
                raise IOError("Not connected")
            self.ser.write((cmd + "\n").encode("ascii"))
            self.ser.flush()
            time.sleep(0.02)  # supply needs a breath between commands

    def query(self, cmd, log=True):
        with self.lock:
            if log:
                self._note(">", cmd)
            if self.sim:
                time.sleep(0.005)
                r = self.sim.query(cmd)
            else:
                if not self.ser:
                    raise IOError("Not connected")
                self.ser.reset_input_buffer()
                self.ser.write((cmd + "\n").encode("ascii"))
                self.ser.flush()
                r = self.ser.readline().decode("ascii", "replace").strip()
                self.empty_streak = 0 if r else self.empty_streak + 1
            if log:
                self._note("<", r if r else "(no reply)")
            return r


def _f(s, default=0.0):
    try:
        return float(str(s).strip().split()[0].rstrip("VAW"))
    except (ValueError, IndexError):
        return default


class PowerMeter:
    """
    Peak / average power tracker. Fed from the poll loop, so it keeps counting
    while the chart is paused. Only counts samples taken while the output is on,
    and averages are time-weighted (energy / on-time), so uneven polling or a
    stalled read doesn't skew them.
    """
    MAX_GAP = 1.0  # s; longer gaps (comms hiccup, output was off) aren't integrated

    def __init__(self):
        self.lock = threading.Lock()
        self.enabled = False
        self.reset()

    def reset(self):
        with self.lock:
            self.peak_p = self.peak_v = self.peak_i = 0.0
            self.peak_at = 0.0          # wall clock (time.time()) of peak power
            self.max_i = 0.0
            self.energy_j = 0.0
            self.charge_c = 0.0
            self.on_time = 0.0
            self.samples = 0
            self.started = time.time()
            self.peak_count = 0         # bumps on every new peak, UI uses it to flash
            self._last = None           # (wall t, p, i) of previous on-sample

    def set_enabled(self, on):
        with self.lock:
            self.enabled = bool(on)
            self._last = None

    def feed(self, t_wall, v, i, p, out):
        with self.lock:
            if not self.enabled or not out:
                self._last = None
                return
            if self._last is not None:
                dt = t_wall - self._last[0]
                if 0 < dt <= self.MAX_GAP:
                    self.energy_j += 0.5 * (p + self._last[1]) * dt
                    self.charge_c += 0.5 * (i + self._last[2]) * dt
                    self.on_time += dt
            self._last = (t_wall, p, i)
            self.samples += 1
            if p > self.peak_p:
                self.peak_p, self.peak_v, self.peak_i, self.peak_at = p, v, i, t_wall
                self.peak_count += 1
            if i > self.max_i:
                self.max_i = i

    def status(self):
        with self.lock:
            avg_p = self.energy_j / self.on_time if self.on_time > 0 else (self.peak_p if self.samples else 0.0)
            avg_i = self.charge_c / self.on_time if self.on_time > 0 else (self.max_i if self.samples else 0.0)
            return dict(enabled=self.enabled, samples=self.samples,
                        peak_p=self.peak_p, peak_v=self.peak_v, peak_i=self.peak_i, peak_at=self.peak_at,
                        peak_count=self.peak_count, max_i=self.max_i,
                        avg_p=avg_p, avg_i=avg_i, energy_wh=self.energy_j / 3600,
                        charge_mah=self.charge_c / 3.6, on_time=self.on_time, started=self.started)


class Device:
    """High level supply control + measurement poller."""

    def __init__(self):
        self.link = Link()
        self.idn = ""
        self.limits = dict(MODELS[DEFAULT_MODEL])
        self.state = dict(v=0.0, i=0.0, p=0.0, out=False, mode="—",
                          v_set=0.0, i_set=0.0, ovp=0.0, ocp=0.0)
        self.samples = deque(maxlen=200000)  # (t, v, i, p, vset, iset)
        self.sample_seq = 0
        self.t0 = time.time()
        self.poll_hz = 8
        self.error = ""
        self._stop = threading.Event()
        self._poller = None
        self._info_ok = None  # does MEAS:ALL:INFO? work on this firmware
        self.recorder = None
        self.paused = False
        self.meter = PowerMeter()
        self.reconnecting = False
        self.reconnects = 0          # how many times the link was recovered (UI shows a toast)
        self.off_on_reconnect = False  # switch the output off when the link comes back (charger asks)

    # -- connection -------------------------------------------------
    def connect(self, port):
        self.disconnect()
        self.link.open(port)
        self.idn = self.link.query("*IDN?")
        if not self.idn:
            self.link.close()
            raise IOError(f"No reply from {port}. Is it the power supply and is it switched on?")
        try:
            self.link.write("SYST:REM")
        except Exception:
            pass
        self.limits = guess_limits(self.idn)
        self._info_ok = None
        self.read_setpoints()
        self.t0 = time.time()
        self.samples.clear()
        self.sample_seq = 0
        self._stop.clear()
        self._poller = threading.Thread(target=self._poll_loop, daemon=True)
        self._poller.start()
        return self.idn

    def disconnect(self):
        self._stop.set()
        if self._poller:
            self._poller.join(timeout=2)
        self._poller = None
        if self.link.is_open:
            try:
                self.link.write("SYST:LOC")
            except Exception:
                pass
        self.link.close()
        self.idn = ""
        self.state.update(out=False, mode="—")

    # -- setpoints -----------------------------------------------------
    def read_setpoints(self):
        L = self.link
        self.state["v_set"] = _f(L.query("VOLT?"))
        self.state["i_set"] = _f(L.query("CURR?"))
        self.state["ovp"] = _f(L.query("VOLT:LIM?"))
        self.state["ocp"] = _f(L.query("CURR:LIM?"))
        self.state["out"] = L.query("OUTP?").upper() in ("ON", "1")

    def _clamp(self, v, lo, hi):
        return max(lo, min(hi, float(v)))

    def set_voltage(self, v, log=True):
        v = self._clamp(v, 0, self.limits["vmax"])
        self.link.write(f"VOLT {v:.3f}", log=log)
        self.state["v_set"] = v
        return v

    def set_current(self, i, log=True):
        i = self._clamp(i, 0, self.limits["imax"])
        self.link.write(f"CURR {i:.3f}", log=log)
        self.state["i_set"] = i
        return i

    def set_ovp(self, v):
        v = self._clamp(v, 0, self.limits["ovp"])
        self.link.write(f"VOLT:LIM {v:.3f}")
        self.state["ovp"] = v
        return v

    def set_ocp(self, i):
        i = self._clamp(i, 0, self.limits["ocp"])
        self.link.write(f"CURR:LIM {i:.3f}")
        self.state["ocp"] = i
        return i

    def set_output(self, on):
        self.link.write("OUTP ON" if on else "OUTP OFF")
        self.state["out"] = bool(on)

    # -- polling -------------------------------------------------------
    def _measure(self):
        L = self.link
        mode = None
        if self._info_ok is not False:
            r = L.query("MEAS:ALL:INFO?", log=False)
            parts = r.split(",")
            if len(parts) >= 7:
                self._info_ok = True
                v, i, p = _f(parts[0]), _f(parts[1]), _f(parts[2])
                mode = {"0": "OFF", "1": "CV", "2": "CC", "3": "FAULT"}.get(parts[6].strip(), "—")
                return v, i, p, mode
            self._info_ok = False
        r = L.query("MEAS:ALL?", log=False)
        parts = r.split(",")
        if len(parts) >= 2:
            v, i = _f(parts[0]), _f(parts[1])
        else:
            v = _f(L.query("MEAS:VOLT?", log=False))
            i = _f(L.query("MEAS:CURR?", log=False))
        return v, i, v * i, mode

    # -- recovering a lost link (laptop sleep, USB reset, cable wiggle) --------------
    def _recover(self, why):
        """Keep reopening the port until the supply answers again. Runs on the poll thread."""
        port, idn = self.link.port, self.idn
        self.reconnecting = True
        self.link.recovering = True
        self.error = "Lost contact with the supply. Reconnecting…"
        self.link._note("!", f"lost contact ({why}), reconnecting")
        attempt = 0
        while not self._stop.is_set():
            attempt += 1
            self.link.drop_handle()
            # same port first; from the 4th try also look at the other ports (it may come back
            # under a different COM number), only accepting one that gives the same *IDN?
            candidates = [port]
            if attempt > 3 and port != "SIM":
                try:
                    candidates += [p["device"] for p in list_ports() if p["device"] != port]
                except Exception:
                    pass
            for cand in candidates:
                if self._stop.is_set():
                    return
                try:
                    self.link.reopen(cand)
                    got = self.link.query("*IDN?")
                    if got and (cand == port or got == idn):
                        self._back(cand, got)
                        return
                except Exception:
                    pass
                self.link.drop_handle()
            self.error = f"Lost contact with the supply. Reconnecting… (try {attempt})"
            self._stop.wait(2.0)

    def _back(self, port, idn):
        self.idn = idn
        try:
            self.link.write("SYST:REM")
        except Exception:
            pass
        if self.off_on_reconnect:
            try:
                self.link.write("OUTP OFF")
            except Exception:
                pass
            self.off_on_reconnect = False
        try:
            self.read_setpoints()
        except Exception:
            pass
        self.link.recovering = False
        self.reconnecting = False
        self.reconnects += 1
        self.error = ""
        self.link._note("!", f"reconnected on {port}")

    def _poll_loop(self):
        tick = 0
        fails = 0
        while not self._stop.is_set():
            start = time.time()
            if not self.paused:
                try:
                    if self.link.empty_streak >= 6:
                        raise IOError("the supply stopped replying")
                    v, i, p, mode = self._measure()
                    tick += 1
                    if tick % 8 == 0:  # occasionally re-read output state
                        self.state["out"] = self.link.query("OUTP?", log=False).upper() in ("ON", "1")
                    if mode is None:
                        if not self.state["out"]:
                            mode = "OFF"
                        elif self.state["i_set"] and i >= self.state["i_set"] * 0.985:
                            mode = "CC"
                        else:
                            mode = "CV"
                    self.state.update(v=v, i=i, p=p, mode=mode)
                    now = time.time()
                    self.meter.feed(now, v, i, p, mode != "OFF" and self.state["out"])
                    t = now - self.t0
                    row = (t, v, i, p, self.state["v_set"], self.state["i_set"])
                    self.samples.append(row)
                    self.sample_seq += 1
                    rec = self.recorder
                    if rec:
                        rec.write(row)
                    self.error = ""
                    fails = 0
                except Exception as e:  # cable pulled, laptop slept, USB reset
                    fails += 1
                    self.error = f"Comms error: {e}"
                    if fails >= 3 and not self._stop.is_set():
                        self._recover(str(e))
                        fails = 0
                        tick = 0
                    else:
                        self._stop.wait(0.5)
            dt = 1.0 / max(1, self.poll_hz) - (time.time() - start)
            if dt > 0:
                self._stop.wait(dt)

    def samples_since(self, seq):
        """Return samples newer than seq (a running counter)."""
        n_new = self.sample_seq - seq
        if n_new <= 0:
            return [], self.sample_seq
        n_new = min(n_new, len(self.samples), 5000)
        data = list(self.samples)[-n_new:]
        return data, self.sample_seq


class Recorder:
    def __init__(self, path, t0=0.0, every=0.0):
        self.path = path
        self.t0 = t0
        self.every = every      # seconds between saved rows (0 = every reading)
        self._last = -1e9
        self.f = open(path, "w", encoding="utf-8", newline="")
        self.f.write("timestamp,time_s,voltage_V,current_A,power_W,set_V,set_A\n")
        self.rows = 0

    def write(self, row):
        if row[0] - self._last < self.every:
            return
        self._last = row[0]
        try:
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.t0 + row[0]))
            ms = int(((self.t0 + row[0]) % 1) * 1000)
            self.f.write(f"{ts}.{ms:03d}," + ",".join(f"{x:.4f}" for x in row) + "\n")
            self.rows += 1
            if self.rows % 20 == 0:
                self.f.flush()
        except ValueError:  # closed between check and write
            pass

    def close(self):
        try:
            self.f.close()
        except Exception:
            pass


class Sequencer:
    """
    Runs a list of steps PC-side: each step = (V, I, seconds, enabled).
    Timing uses a monotonic deadline so drift does not accumulate.
    """
    MIN_STEP = 0.05

    def __init__(self, dev: Device):
        self.dev = dev
        self.thread = None
        self.stop_evt = threading.Event()
        self.status = dict(running=False, index=-1, loop=0, loops=1,
                           step_left=0.0, elapsed=0.0, total=0.0, msg="")

    def start(self, steps, loops=1, start=0, end=None, output_on=True,
              output_off_at_end=True):
        self.stop()
        steps = [s for s in steps[start:(end + 1 if end is not None else None)]
                 if s.get("en", True)]
        if not steps:
            raise ValueError("No enabled steps to run.")
        for s in steps:
            s["dt"] = max(self.MIN_STEP, float(s["dt"]))
        total = sum(s["dt"] for s in steps)
        self.stop_evt.clear()
        self.status.update(running=True, index=0, loop=1, loops=int(loops),
                           total=total, elapsed=0.0, msg="")
        self.thread = threading.Thread(
            target=self._run, args=(steps, int(loops), output_on, output_off_at_end),
            daemon=True)
        self.thread.start()

    def _run(self, steps, loops, output_on, output_off_at_end):
        dev = self.dev
        t_start = time.monotonic()
        try:
            loop = 0
            last_v = last_i = None
            while loops == 0 or loop < loops:
                loop += 1
                self.status["loop"] = loop
                t_loop = time.monotonic()
                deadline = t_loop
                for idx, s in enumerate(steps):
                    if self.stop_evt.is_set():
                        return
                    self.status["index"] = s.get("n", idx)
                    if s["i"] != last_i:
                        dev.set_current(s["i"], log=False)
                        last_i = s["i"]
                    if s["v"] != last_v:
                        dev.set_voltage(s["v"], log=False)
                        last_v = s["v"]
                    if output_on and loop == 1 and idx == 0 and not dev.state["out"]:
                        dev.set_output(True)
                    deadline += s["dt"]
                    while True:
                        now = time.monotonic()
                        left = deadline - now
                        self.status["step_left"] = max(0, left)
                        self.status["elapsed"] = (now - t_loop)
                        if left <= 0 or self.stop_evt.wait(min(left, 0.02)):
                            break
            self.status["msg"] = "Finished"
        except Exception as e:
            self.status["msg"] = f"Stopped: {e}"
        finally:
            if output_off_at_end:
                try:
                    dev.set_output(False)
                except Exception:
                    pass
            self.status["running"] = False
            self.status["index"] = -1
            self.status["run_time"] = time.monotonic() - t_start

    def stop(self):
        if self.thread and self.thread.is_alive():
            self.stop_evt.set()
            self.thread.join(timeout=2)
            self.status["msg"] = "Stopped by user"
        self.status["running"] = False
