"""
Battery charger: turns the bench supply into a staged CC/CV charger.

The supply already limits voltage and current. This module picks the right
limits for the battery, then watches the measured current to move between
stages:

  pre-charge  gentle current until a flat battery comes up to a safe voltage
  bulk        constant current (supply in CC) until the charge voltage is reached
  absorb      constant voltage (supply in CV) while the current tapers off
  float       lead-acid only: hold at a lower voltage, safe to leave connected
  done        lithium: output off once the current has tapered

Safety stops: overall timer, pre-charge timer, voltage above target, no current
flowing (no battery), the supply turning its own output off (OVP/OCP trip),
and lost comms. Every stop turns the output off.
"""
import threading
import time

from device import _f

# Per-cell values. Sources: typical manufacturer charging guides (Yuasa / CSB for
# lead-acid, cell datasheets for Li-ion and LiFePO4). The battery's own datasheet wins.
CHEMS = {
    "lead": dict(
        name="Lead-acid (flooded / AGM)", short="Lead-acid",
        v_charge=2.40, v_float=2.275, v_pre=1.75, v_dead=1.50, v_high=2.60,
        rate=dict(normal=0.10, fast=0.25), i_pre_c=0.05, term_c=0.02,
        absorb_max_h=4.0, term_hold_s=60, eff=1.25,
        sizes=[(3, "6 V"), (6, "12 V"), (12, "24 V")],
        size_hint={3: "small 6 V SLA", 6: "car, bike, UPS, alarm", 12: "truck, mobility, 2× 12 V"},
    ),
    "gel": dict(
        name="Lead-acid gel", short="Gel",
        v_charge=2.35, v_float=2.25, v_pre=1.75, v_dead=1.50, v_high=2.60,
        rate=dict(normal=0.10, fast=0.20), i_pre_c=0.05, term_c=0.02,
        absorb_max_h=4.0, term_hold_s=60, eff=1.25,
        sizes=[(3, "6 V"), (6, "12 V"), (12, "24 V")],
        size_hint={3: "6 V gel", 6: "12 V gel, leisure", 12: "24 V gel"},
    ),
    "lfp": dict(
        name="LiFePO4 (lithium iron phosphate)", short="LiFePO4",
        v_charge=3.60, v_float=None, v_pre=2.50, v_dead=2.00, v_high=3.75,
        rate=dict(normal=0.30, fast=0.50), i_pre_c=0.10, term_c=0.05,
        absorb_max_h=2.0, term_hold_s=30, eff=1.05,
        sizes=[(1, "3.2 V"), (2, "6.4 V"), (4, "12.8 V"), (8, "25.6 V")],
        size_hint={1: "1 cell", 2: "2 cells", 4: "\"12 V\" lithium, 4 cells", 8: "\"24 V\" lithium, 8 cells"},
    ),
    "liion": dict(
        name="Li-ion (18650, 21700, packs)", short="Li-ion",
        v_charge=4.20, v_float=None, v_pre=3.00, v_dead=2.50, v_high=4.30,
        rate=dict(normal=0.50, fast=1.00), i_pre_c=0.10, term_c=0.05,
        absorb_max_h=2.0, term_hold_s=30, eff=1.05,
        sizes=[(1, "3.7 V"), (2, "7.4 V"), (3, "11.1 V"), (4, "14.8 V"),
               (5, "18.5 V"), (6, "22.2 V"), (7, "25.9 V")],
        size_hint={1: "1 cell", 2: "2 cells", 3: "3 cells", 4: "4 cells",
                   5: "5 cells", 6: "6 cells", 7: "7 cells"},
    ),
}

PRE_MAX_S = 30 * 60        # give up if a flat battery won't come up in 30 min
STAGES = ["pre", "bulk", "absorb", "float", "done"]
STAGE_NAMES = dict(pre="Waking up", bulk="Bulk charge", absorb="Topping up", float="Holding (float)",
                   done="Full", fault="Stopped (fault)", stopped="Stopped", idle="Ready")


def options():
    """What the UI shows in its drop-downs."""
    out = []
    for key, c in CHEMS.items():
        out.append(dict(key=key, name=c["name"], short=c["short"], float=c["v_float"] is not None,
                        rates=c["rate"],
                        sizes=[dict(cells=n, label=lab, hint=c["size_hint"].get(n, ""),
                                    v_charge=round(n * c["v_charge"], 2),
                                    v_float=round(n * c["v_float"], 2) if c["v_float"] else None)
                               for n, lab in c["sizes"]]))
    out.append(dict(key="custom", name="Custom (type your own voltages)", short="Custom", float=True,
                    rates={}, sizes=[]))
    return out


def _r(x, d=3):
    return round(float(x), d)


def make_plan(cfg, limits):
    """
    cfg: chem, cells, cap (Ah), mode (normal|fast), hold (bool, lead-acid float),
         i_override (A, optional), custom: {v_charge, v_hold, i, i_term}
    Returns dict(ok=True, plan=...) or dict(ok=False, msg=...).
    """
    chem = cfg.get("chem")
    mode = "fast" if cfg.get("mode") == "fast" else "normal"
    notes = []
    try:
        cap = float(cfg.get("cap") or 0)
    except (TypeError, ValueError):
        cap = 0.0
    vmax, imax = float(limits["vmax"]), float(limits["imax"])
    pmax = float(limits.get("pmax") or vmax * imax)

    if chem == "custom":
        cu = cfg.get("custom") or {}
        try:
            v_charge = float(cu.get("v_charge") or 0)
            v_hold = float(cu.get("v_hold") or 0)
            i_chg = float(cu.get("i") or 0)
            i_term = float(cu.get("i_term") or 0)
        except (TypeError, ValueError):
            return dict(ok=False, msg="Fill in the custom voltages and current as numbers.")
        if v_charge <= 0:
            return dict(ok=False, msg="Type a charge voltage.")
        if i_chg <= 0:
            return dict(ok=False, msg="Type a charge current.")
        if v_hold < 0 or v_hold > v_charge:
            return dict(ok=False, msg="Hold voltage must be between 0 and the charge voltage.")
        if i_term <= 0:
            i_term = max(0.01, i_chg * 0.05)
        plan = dict(chem="custom", label=f"Custom {v_charge:g} V", cells=0, cap=cap, mode="custom",
                    v_charge=v_charge, v_float=v_hold or None, v_pre=0.0, v_dead=0.0,
                    v_high=v_charge * 1.05, i_charge=i_chg, i_pre=0.0, i_term=i_term,
                    absorb_max_s=4 * 3600, term_hold_s=60, eff=1.2)
        if v_hold and abs(v_hold - v_charge) < 1e-6:
            plan["absorb_max_s"] = 0  # same voltage both stages: straight to "hold" once in CV
            notes.append("Charge and hold voltages are the same: it charges, then keeps holding that voltage.")
    else:
        c = CHEMS.get(chem)
        if not c:
            return dict(ok=False, msg="Pick a battery type.")
        try:
            cells = int(cfg.get("cells") or 0)
        except (TypeError, ValueError):
            cells = 0
        size = dict(c["sizes"]).get(cells)
        if not size:
            return dict(ok=False, msg="Pick the battery voltage.")
        if cap <= 0:
            return dict(ok=False, msg="Type the battery capacity (the Ah number on the label).")
        if cap >= 1000:
            return dict(ok=False, msg="That capacity looks too big. Use Ah, not mAh (2000 mAh = 2 Ah).")
        hold = c["v_float"] is not None and cfg.get("hold", True) is not False
        i_chg = cap * c["rate"][mode]
        try:
            ov = float(cfg.get("i_override") or 0)
        except (TypeError, ValueError):
            ov = 0.0
        if ov > 0:
            if ov > cap * c["rate"]["fast"] * 1.01:
                notes.append(f"{ov:g} A is more than the usual fast rate for this battery "
                             f"({cap * c['rate']['fast']:.2f} A). Only use it if the battery's datasheet allows it.")
            i_chg = ov
        plan = dict(chem=chem, label=f"{size} {c['short']}", cells=cells, cap=cap, mode=mode,
                    v_charge=cells * c["v_charge"], v_float=cells * c["v_float"] if hold else None,
                    v_pre=cells * c["v_pre"], v_dead=cells * c["v_dead"], v_high=cells * c["v_high"],
                    i_charge=i_chg, i_pre=max(0.01, cap * c["i_pre_c"]), i_term=max(0.01, cap * c["term_c"]),
                    absorb_max_s=c["absorb_max_h"] * 3600, term_hold_s=c["term_hold_s"], eff=c["eff"])
        plan["i_pre"] = min(plan["i_pre"], i_chg)
        if chem in ("lfp", "liion") and mode == "fast" and ov <= 0:
            notes.append("Fast charge: check the battery's datasheet allows "
                         f"{c['rate']['fast']:g}C ({i_chg:.2f} A).")

    # fit the supply
    if plan["v_charge"] > vmax - 0.1:
        return dict(ok=False, msg=f"This needs {plan['v_charge']:.2f} V but the supply only goes to {vmax:g} V.")
    if plan["v_charge"] + 0.3 > float(limits["ovp"]):
        return dict(ok=False, msg=f"Charge voltage {plan['v_charge']:.2f} V is too close to the supply's {limits['ovp']} V protection limit.")
    i_lim = min(imax, pmax * 0.95 / plan["v_charge"])
    if plan["i_charge"] > i_lim:
        notes.append(f"Current limited to {i_lim:.2f} A by the supply (wanted {plan['i_charge']:.2f} A), so it takes longer.")
        plan["i_charge"] = i_lim
    if plan["i_charge"] < 0.005:
        return dict(ok=False, msg="Charge current is too small for the supply to control.")
    plan["i_term"] = min(plan["i_term"], plan["i_charge"] * 0.5)
    if plan["i_pre"]:
        plan["i_pre"] = min(plan["i_pre"], plan["i_charge"])

    plan["ovp"] = min(float(limits["ovp"]), plan["v_charge"] + max(0.5, 0.04 * plan["v_charge"]))
    plan["ocp"] = min(float(limits["ocp"]), plan["i_charge"] * 1.25 + 0.1)
    if cap > 0:
        hrs = cap / plan["i_charge"] * plan["eff"] + plan["absorb_max_s"] / 3600 * 0.5
        plan["est_h"] = hrs
        plan["total_max_s"] = (cap / plan["i_charge"] * plan["eff"] * 1.5 + 3) * 3600
    else:
        plan["est_h"] = None
        plan["total_max_s"] = 24 * 3600
    plan["notes"] = notes
    for k in ("v_charge", "v_pre", "v_dead", "v_high", "ovp"):
        plan[k] = _r(plan[k], 3)
    if plan["v_float"]:
        plan["v_float"] = _r(plan["v_float"], 3)
    for k in ("i_charge", "i_pre", "i_term", "ocp"):
        plan[k] = _r(plan[k], 3)
    return dict(ok=True, plan=plan)


class Charger:
    TICK = 0.5

    def __init__(self, dev):
        self.dev = dev
        self.thread = None
        self.stop_evt = threading.Event()
        self.lock = threading.Lock()
        self.plan = None
        self.on_end = None      # called with the final status when a charge finishes
        self._idle()

    def _idle(self):
        self.status = dict(running=False, stage="idle", kind="", msg="", plan=None, started=0.0, elapsed=0.0,
                           stage_started=0.0, ah=0.0, wh=0.0, v=0.0, i=0.0, target_v=0.0, target_i=0.0,
                           taper=0.0, events=[], sim_speed=1)

    def _event(self, text):
        ev = self.status["events"]
        ev.append([time.time(), text])
        del ev[:-60]

    @property
    def running(self):
        return bool(self.thread and self.thread.is_alive())

    # ------------------------------------------------------------------
    def start(self, cfg):
        if self.running:
            raise RuntimeError("A charge is already running.")
        r = make_plan(cfg, self.dev.limits)
        if not r["ok"]:
            raise ValueError(r["msg"])
        plan = r["plan"]
        dev = self.dev
        sim = dev.link.sim
        if sim is not None:
            sim.attach_battery(plan)

        # what does the battery read before we start? (works when the output stage
        # lets the terminals float; reads ~0 on supplies that disconnect them)
        if dev.state.get("out"):
            dev.set_output(False)
            time.sleep(0.3)
        v_open = 0.0
        try:
            v_open = _f(dev.link.query("MEAS:VOLT?"))
        except Exception:
            pass
        if v_open > 0.5 and plan["chem"] != "custom":
            if v_open > plan["v_high"]:
                self._detach()
                raise ValueError(f"The battery reads {v_open:.2f} V, which is higher than a {plan['label']} "
                                 f"battery should ever be. Check you picked the right voltage.")
            if v_open < plan["v_dead"]:
                self._detach()
                low = "may be damaged. Don't charge a lithium battery that has sat this flat" if plan["chem"] in ("lfp", "liion") \
                    else "is very badly flat"
                raise ValueError(f"The battery reads {v_open:.2f} V, too low for a {plan['label']} battery. "
                                 f"Either the wrong voltage is picked or the battery {low}.")
        elif v_open > 0.5 and v_open > plan["v_charge"] * 1.05:
            self._detach()
            raise ValueError(f"The battery reads {v_open:.2f} V, above your {plan['v_charge']:g} V charge voltage.")

        self.plan = plan
        self.stop_evt.clear()
        self._idle()
        now = time.time()
        self.status.update(running=True, plan=plan, started=now, stage_started=now,
                           sim_speed=getattr(sim, "speed", 1) if sim is not None else 1)
        self._event(f"Start: {plan['label']}" + (f", {plan['cap']:g} Ah, {plan['mode']}" if plan['cap'] else ""))
        if v_open > 0.5:
            self._event(f"Battery reads {v_open:.2f} V before charging")
        self._saved = dict(v=dev.state.get("v_set", 0), i=dev.state.get("i_set", 0),
                           ovp=dev.state.get("ovp", 0), ocp=dev.state.get("ocp", 0))
        self.thread = threading.Thread(target=self._run, args=(plan,), daemon=True)
        self.thread.start()

    def stop(self, msg="Stopped by you"):
        if self.running:
            self._stop_msg = msg
            self.stop_evt.set()
            self.thread.join(timeout=3)

    # ------------------------------------------------------------------
    def _detach(self):
        sim = self.dev.link.sim
        if sim is not None:
            sim.detach_battery()

    def _avg(self, window=2.0):
        """Average V and I over the last `window` seconds of polled samples."""
        dev = self.dev
        rows = []
        try:
            last_t = dev.samples[-1][0]
        except IndexError:
            return dev.state.get("v", 0.0), dev.state.get("i", 0.0)
        for row in reversed(dev.samples):
            if row[0] < last_t - window:
                break
            rows.append(row)
            if len(rows) > 400:
                break
        if not rows:
            return dev.state.get("v", 0.0), dev.state.get("i", 0.0)
        return sum(r[1] for r in rows) / len(rows), sum(r[2] for r in rows) / len(rows)

    def _set(self, v=None, i=None):
        if i is not None:
            self.dev.set_current(i)
            self.status["target_i"] = i
        if v is not None:
            self.dev.set_voltage(v)
            self.status["target_v"] = v

    def _stage(self, name, msg=""):
        self.status["stage"] = name
        self.status["stage_started"] = time.time()
        self._event(STAGE_NAMES.get(name, name) + (f": {msg}" if msg else ""))

    def _run(self, plan):
        dev, st = self.dev, self.status
        self._stop_msg = "Stopped by you"
        end_kind, end_msg = "stopped", ""
        try:
            dev.set_ocp(plan["ocp"])
            dev.set_ovp(plan["ovp"])
            first_i = plan["i_pre"] if plan["i_pre"] else plan["i_charge"]
            self._set(v=plan["v_charge"], i=first_i)
            dev.set_output(True)
            self._stage("pre" if plan["i_pre"] else "bulk",
                        f"{first_i:.3f} A up to {plan['v_charge']:.2f} V")

            t0 = time.monotonic()
            t_on = t0
            last = t0
            below_since = None
            over_since = None
            err_since = None
            scale = st["sim_speed"]
            v_ceiling = max(plan["v_charge"], plan["v_float"] or 0)

            while not self.stop_evt.wait(self.TICK):
                now = time.monotonic()
                dt = now - last
                last = now
                stage = st["stage"]
                t_stage = time.time() - st["stage_started"]

                # comms
                if dev.error:
                    err_since = err_since or now
                    if now - err_since > 5:
                        end_kind, end_msg = "fault", "Lost contact with the supply. Output switched off."
                        break
                    continue
                err_since = None

                # supply turned its own output off (OVP / OCP / OTP trip, front-panel button)
                if not dev.state.get("out") and now - t_on > 2.5:
                    end_kind = "fault"
                    end_msg = ("The supply switched its output off (protection trip or front-panel button). "
                               "If it says OVP, the battery may be a higher voltage than you picked.")
                    break

                v, i = self._avg(2.0)
                i_now = max(0.0, dev.state.get("i", 0.0))
                st["v"], st["i"] = v, i
                st["ah"] += i_now * dt * scale / 3600
                st["wh"] += i_now * dev.state.get("v", 0.0) * dt * scale / 3600
                st["elapsed"] = time.time() - st["started"]

                # voltage above what we're charging to: something else is driving the terminals
                if v > v_ceiling + max(0.15, 0.02 * v_ceiling):
                    over_since = over_since or now
                    if now - over_since > 3:
                        end_kind = "fault"
                        end_msg = (f"The battery reads {v:.2f} V, above the {v_ceiling:.2f} V it should be charged to. "
                                   "Check you picked the right battery voltage.")
                        break
                else:
                    over_since = None

                # nothing connected: voltage sits at the setpoint and no current flows
                if stage in ("pre", "bulk") and 4 < now - t0 < 20 and i < 0.004 \
                        and abs(v - st["target_v"]) < 0.02 * st["target_v"] + 0.05:
                    end_kind = "fault"
                    end_msg = ("No current is flowing: no battery connected, a loose clip, "
                               "or the battery is already completely full.")
                    break

                # overall timer for the charging stages
                if stage in ("pre", "bulk", "absorb") and now - t0 > plan["total_max_s"]:
                    end_kind = "fault"
                    end_msg = (f"Still charging after {(now - t0) / 3600:.1f} h, much longer than expected. "
                               "Stopped for safety: the battery may be faulty, or the Ah figure is too low.")
                    break

                in_cv = dev.state.get("mode") == "CV" or (
                    dev.state.get("mode") not in ("CC",) and v >= st["target_v"] * 0.995
                    and i < st["target_i"] * 0.95)

                if stage == "pre":
                    if t_stage > 3 and v >= plan["v_pre"]:
                        self._set(i=plan["i_charge"])
                        self._stage("bulk", f"{v:.2f} V, now {plan['i_charge']:.3f} A")
                    elif t_stage > PRE_MAX_S:
                        end_kind = "fault"
                        end_msg = (f"The battery stayed below {plan['v_pre']:.2f} V after 30 min of gentle charging. "
                                   "It is probably damaged. Don't use it.")
                        break
                elif stage == "bulk":
                    if t_stage > 3 and in_cv:
                        self._stage("absorb", f"reached {st['target_v']:.2f} V, current now tapering")
                elif stage == "absorb":
                    span = max(1e-6, st["target_i"] - plan["i_term"])
                    st["taper"] = max(0.0, min(1.0, (st["target_i"] - i) / span))
                    full = False
                    if i < plan["i_term"]:
                        below_since = below_since or now
                        if now - below_since >= plan["term_hold_s"]:
                            full = True
                            why = f"current fell below {plan['i_term']:.3f} A"
                    else:
                        below_since = None
                    if not full and t_stage > plan["absorb_max_s"] and in_cv:
                        full, why = True, "time limit for this stage reached"
                    if full:
                        if plan["v_float"]:
                            self._set(v=plan["v_float"])
                            self._stage("float", f"{why}. Holding {plan['v_float']:.2f} V")
                            st["taper"] = 1.0
                        else:
                            st["taper"] = 1.0
                            end_kind, end_msg = "ok", f"Charged ({why}). Output switched off."
                            st["stage"] = "done"
                            break
                elif stage == "float":
                    pass  # holds until stopped; nothing to decide
            else:
                end_kind, end_msg = "stopped", self._stop_msg
                if st["stage"] == "float":
                    end_kind, end_msg = "ok", "Stopped while holding. The battery is full."
                    st["stage"] = "done"
        except Exception as e:  # noqa: BLE001
            end_kind, end_msg = "fault", f"Stopped: {e}"
        finally:
            try:
                dev.set_output(False)
            except Exception:
                pass
            # put the user's own setpoints back (output is off, so this is harmless)
            try:
                s = self._saved
                dev.set_ocp(s["ocp"]) if s["ocp"] else None
                dev.set_ovp(s["ovp"]) if s["ovp"] else None
                dev.set_current(s["i"])
                dev.set_voltage(s["v"])
            except Exception:
                pass
            self._detach()
            if end_kind == "fault":
                st["stage"] = "fault"
            elif st["stage"] != "done":
                st["stage"] = "stopped"
            st["kind"] = end_kind
            st["msg"] = end_msg
            st["elapsed"] = time.time() - st["started"]
            self._event(end_msg)
            st["running"] = False
            if self.on_end:
                try:
                    self.on_end(st)
                except Exception:
                    pass
