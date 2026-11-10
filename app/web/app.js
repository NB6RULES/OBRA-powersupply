/* PSU Studio front end. Talks to Python via pywebview (desktop) or fetch (browser mode). */
"use strict";

// ---------------------------------------------------------------- bridge
const BROWSER = new URLSearchParams(location.search).has("browser");
const isDesktop = () => !BROWSER;
const api = new Proxy({}, {
  get: (_, name) => (...args) => isDesktop()
    ? window.pywebview.api[name](...args)
    : fetch("/api/" + name, { method: "POST", body: JSON.stringify(args) }).then(r => r.json())
});

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const fmt = (x, d) => (x == null || isNaN(x)) ? "—" : Number(x).toFixed(d);
const clamp = (x, a, b) => Math.max(a, Math.min(b, x));
const num = v => { const n = parseFloat(String(v).trim().replace(/\s/g, "")); return isFinite(n) ? n : NaN; };

function toast(msg, kind = "", ms = 3200, action) {
  const t = $("#toast");
  t.className = "toast show " + kind;
  t.textContent = msg;
  if (action) { const a = document.createElement("a"); a.textContent = action.label; a.onclick = action.fn; t.append(a); }
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.className = "toast", ms);
}

// ---------------------------------------------------------------- app state
const S = {
  connected: false, limits: { vmax: 30, imax: 10, pmax: 200, ovp: 31, ocp: 10.1 },
  state: {}, seq: 0, step: 0.01, paused: false,
  live: { t: [], v: [], i: [], p: [] }, win: 30,
  steps: [], sel: new Set(),
  presets: Array(8).fill(null),
  settings: {}, dirty: {},
  seqStatus: { running: false },
};
const MAX_LIVE = 120000;

// ---------------------------------------------------------------- settings
let saveT;
function saveSettings() {
  clearTimeout(saveT);
  saveT = setTimeout(() => {
    S.settings.presets = S.presets;
    S.settings.steps = S.steps;
    S.settings.run = { loops: $("#loops").value, outOn: $("#outOn").checked, outOff: $("#outOff").checked };
    S.settings.autoConn = $("#autoConn").checked;
    S.settings.lastPort = $("#portSel").value;
    api.save_settings(S.settings);
  }, 400);
}

// ---------------------------------------------------------------- charts
const css = getComputedStyle(document.documentElement);
const C = { v: css.getPropertyValue("--v").trim(), i: css.getPropertyValue("--i").trim(), p: css.getPropertyValue("--p").trim() };
const axis = (o = {}) => Object.assign({
  stroke: "#6b7583", grid: { stroke: "#1b2129", width: 1 }, ticks: { stroke: "#1b2129" },
  font: "11px " + css.getPropertyValue("--mono"),
}, o);

function sized(el) { const r = el.getBoundingClientRect(); return { width: Math.max(200, r.width - 12), height: Math.max(140, r.height - 40) }; }

let liveChart, waveChart, genChart;
function makeLive() {
  const el = $("#liveChart");
  liveChart = new uPlot({
    ...sized(el),
    cursor: { drag: { x: true, y: false } },
    scales: { x: { time: false }, V: { range: (u, a, b) => [0, Math.max(1, b * 1.15)] }, A: { range: (u, a, b) => [0, Math.max(0.1, b * 1.15)] }, W: { range: (u, a, b) => [0, Math.max(1, b * 1.15)] } },
    axes: [axis({ values: (u, v) => v.map(x => fmtTime(x)) }), axis({ scale: "V", values: (u, v) => v.map(x => x + " V") }),
           axis({ scale: "A", side: 1, size: 62, grid: { show: false }, values: (u, v) => v.map(x => x + " A") })],
    series: [{ label: "t", value: (u, v) => v == null ? "—" : fmtTime(v) },
      { label: "Voltage", scale: "V", stroke: C.v, width: 1.6, value: (u, v) => fmt(v, 3) + " V" },
      { label: "Current", scale: "A", stroke: C.i, width: 1.6, value: (u, v) => fmt(v, 3) + " A" },
      { label: "Power", scale: "W", stroke: C.p, width: 1.2, dash: [4, 3], value: (u, v) => fmt(v, 2) + " W" }],
  }, [[], [], [], []], el);
}
function fmtTime(s) {
  if (s == null) return "";
  if (s < 120) return (+s.toFixed(1)) + "s";
  const m = Math.floor(s / 60), r = Math.round(s % 60);
  return m + ":" + String(r).padStart(2, "0");
}

function stepSeries(steps) {
  // build a stepped trace: one point per step start + a final point
  const t = [], v = [], i = [];
  let acc = 0;
  steps.forEach(s => { t.push(acc); v.push(s.en ? s.v : null); i.push(s.en ? s.i : null); if (s.en) acc += Math.max(0.05, s.dt); });
  if (steps.length) { t.push(acc); v.push(v[v.length - 1]); i.push(i[i.length - 1]); }
  return [t, v, i];
}
function makeStepChart(el, extraOpts = {}) {
  return new uPlot({
    ...sized(el),
    cursor: { drag: { x: true, y: false } },
    scales: { x: { time: false }, V: { range: (u, a, b) => [Math.min(0, a), Math.max(1, b * 1.1)] }, A: { range: (u, a, b) => [0, Math.max(0.1, b * 1.15)] } },
    axes: [axis({ values: (u, v) => v.map(x => fmtTime(x)) }), axis({ scale: "V", values: (u, v) => v.map(x => x + " V") }),
           axis({ scale: "A", side: 1, size: 62, grid: { show: false }, values: (u, v) => v.map(x => x + " A") })],
    series: [{ label: "t", value: (u, v) => v == null ? "—" : fmtTime(v) },
      { label: "Voltage", scale: "V", stroke: C.v, width: 1.8, fill: "rgba(255,176,32,.07)", paths: uPlot.paths.stepped({ align: 1 }), value: (u, v) => fmt(v, 3) + " V" },
      { label: "Current", scale: "A", stroke: C.i, width: 1.4, paths: uPlot.paths.stepped({ align: 1 }), value: (u, v) => fmt(v, 3) + " A" }],
    plugins: [cursorLinePlugin()],
    ...extraOpts,
  }, [[], [], []], el);
}
// draws the "now playing" line on the waveform preview
function cursorLinePlugin() {
  return { hooks: { draw: [u => {
    if (u._playT == null) return;
    const x = u.valToPos(u._playT, "x", true);
    const ctx = u.ctx; ctx.save(); ctx.strokeStyle = "#fff"; ctx.globalAlpha = .7; ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.moveTo(x, u.bbox.top); ctx.lineTo(x, u.bbox.top + u.bbox.height); ctx.stroke(); ctx.restore();
  }] } };
}
const ro = new ResizeObserver(() => {
  [[liveChart, "#liveChart"], [waveChart, "#waveChart"]].forEach(([c, s]) => { if (c && $(s).offsetParent) c.setSize(sized($(s))); });
});

// ---------------------------------------------------------------- live data
function pushSamples(rows) {
  if (S.paused || !rows.length) return;
  const L = S.live;
  for (const r of rows) {
    if (L.t.length && r[0] < L.t[L.t.length - 1]) { L.t = []; L.v = []; L.i = []; L.p = []; }  // reconnect: time restarted
    L.t.push(r[0]); L.v.push(r[1]); L.i.push(r[2]); L.p.push(r[3]);
  }
  if (L.t.length > MAX_LIVE) { const n = L.t.length - MAX_LIVE; L.t.splice(0, n); L.v.splice(0, n); L.i.splice(0, n); L.p.splice(0, n); }
  drawLive();
}
let liveRaf;
function drawLive() {
  if (liveRaf) return;
  liveRaf = requestAnimationFrame(() => {
    liveRaf = null;
    if (!$("#pane-live").classList.contains("on")) return;
    const L = S.live;
    let from = 0;
    if (S.win > 0 && L.t.length) { const t0 = L.t[L.t.length - 1] - S.win; from = lowerBound(L.t, t0); }
    const sl = a => a.slice(from);
    liveChart.setData([sl(L.t), sl(L.v), sl(L.i), $("#showP").checked ? sl(L.p) : []]);
    stats(sl(L.v), sl(L.i), sl(L.p), sl(L.t));
  });
}
function lowerBound(a, x) { let lo = 0, hi = a.length; while (lo < hi) { const m = (lo + hi) >> 1; if (a[m] < x) lo = m + 1; else hi = m; } return lo; }
function stats(v, i, p, t) {
  if (!v.length) { $("#liveStats").innerHTML = ""; return; }
  const mm = a => { let lo = Infinity, hi = -Infinity, s = 0; for (const x of a) { if (x < lo) lo = x; if (x > hi) hi = x; s += x; } return [lo, hi, s / a.length]; };
  const [vl, vh, va] = mm(v), [il, ih, ia] = mm(i);
  let wh = 0; for (let k = 1; k < t.length; k++) wh += p[k] * (t[k] - t[k - 1]);
  let ah = 0; for (let k = 1; k < t.length; k++) ah += i[k] * (t[k] - t[k - 1]);
  $("#liveStats").innerHTML =
    `<span>V min/avg/max <b>${fmt(vl, 3)} / ${fmt(va, 3)} / ${fmt(vh, 3)}</b></span>` +
    `<span>I min/avg/max <b>${fmt(il, 3)} / ${fmt(ia, 3)} / ${fmt(ih, 3)}</b></span>` +
    `<span>Energy in view <b>${fmt(wh / 3600, 4)} Wh</b> · <b>${fmt(ah / 3.6, 2)} mAh</b></span>` +
    `<span>Points <b>${v.length}</b></span>`;
}

// ---------------------------------------------------------------- polling
let lastMode = "";
async function poll() {
  try {
    const r = await api.get_state(S.seq);
    S.seq = r.seq;
    const wasConnected = S.connected;
    S.connected = r.connected;
    if (r.connected) {
      S.limits = r.limits; S.state = r.state;
      pushSamples(r.samples);
      renderSeq(r.sequencer);
      renderReadouts(r);
      if (!wasConnected) onConnected(r);
    } else { renderSeq(r.sequencer); if (wasConnected) onDisconnected(); }
    renderRec(r.recording);
    $("#statusTxt").textContent = r.error || (r.connected ? `${r.port} · ${shortIdn(r.idn)}` : "Not connected");
    $("#statusTxt").style.color = r.error ? "var(--bad)" : "";
  } catch (e) { /* bridge not ready yet */ }
  setTimeout(poll, 100);
}
function shortIdn(idn) { const p = (idn || "").split(","); return p.length > 1 ? p[1] + (idn.includes("SIM") ? " (sim)" : "") : idn; }

function renderReadouts(r) {
  const st = r.state, on = st.out;
  $("#roV").textContent = fmt(st.v, 3);
  $("#roI").textContent = fmt(st.i, 3);
  $("#roP").textContent = fmt(st.p, 2);
  $(".readouts").classList.toggle("off", !on);
  const m = $("#modeBadge"); m.textContent = st.mode || "—"; m.className = "mode " + (st.mode || "").toLowerCase();
  const ob = $("#outBtn"); ob.classList.toggle("on", !!on); $("#outTxt").textContent = on ? "OUTPUT ON" : "OUTPUT OFF";
  ob.disabled = false;
  // detect protection trip: output went off while mode was active and readings near the limit
  if (lastMode && lastMode !== "OFF" && !on && !S.seqStatus.running && S.userOff !== true && Date.now() - (S.seqEndedAt || 0) > 3000) {
    $("#tripBadge").hidden = false; $("#tripBadge").textContent = "OUTPUT TRIPPED";
    toast("Output switched off by the supply - check OVP/OCP/OTP.", "err", 6000);
  }
  if (on) { $("#tripBadge").hidden = true; S.userOff = false; }
  lastMode = st.mode;
  // setpoints (don't clobber while the user is typing)
  const map = { v: ["#spV", st.v_set, 3], i: ["#spI", st.i_set, 3], ovp: ["#spOVP", st.ovp, 2], ocp: ["#spOCP", st.ocp, 3] };
  for (const k in map) { const [sel, val, d] = map[k]; const el = $(sel); if (document.activeElement !== el && !S.dirty[k]) el.value = fmt(val, d); }
}

function onConnected(r) {
  const L = r.limits;
  $("#modelTag").textContent = modelName(r.idn) + ` · ${L.vmax} V / ${L.imax} A`;
  $("#connectBtn").textContent = "Disconnect";
  $("#connectBtn").classList.remove("primary");
  $("#spV").max = L.vmax; $("#spI").max = L.imax; $("#spOVP").max = L.ovp; $("#spOCP").max = L.ocp;
  const sel = $("#portSel");
  if (![...sel.options].some(o => o.value === r.port)) sel.insertAdjacentHTML("beforeend", `<option value="${r.port}">${r.port}</option>`);
  sel.value = r.port;
  toast("Connected: " + shortIdn(r.idn));
}
function onDisconnected() {
  $("#connectBtn").textContent = "Connect"; $("#connectBtn").classList.add("primary");
  $("#outBtn").disabled = true; $("#outBtn").classList.remove("on"); $("#outTxt").textContent = "OUTPUT OFF";
  $(".readouts").classList.add("off"); $("#modeBadge").textContent = "—"; $("#modeBadge").className = "mode";
  lastMode = "";
  refreshDriver();
}
function modelName(idn) {
  const m = /SPE(\d)(\d\d)/.exec(idn || ""); if (!m) return "Multicomp Pro";
  const key = `${m[1]}${m[2]}`;
  return ({ "305": "MP711127", "310": "MP711128", "610": "MP711129/31", "605": "MP711130" })[key] || "OWON SPE" + key;
}

// ---------------------------------------------------------------- connection & driver
async function refreshPorts() {
  const ports = await api.get_ports();
  const sel = $("#portSel"), prev = sel.value || S.settings.lastPort;
  sel.innerHTML = ports.map(p => `<option value="${p.device}">${p.device === "SIM" ? "Simulator" : p.device}${p.ch340 ? "  ● supply (CH340)" : p.desc && p.device !== "SIM" ? "  " + p.desc.slice(0, 28) : ""}</option>`).join("");
  const ch = ports.find(p => p.ch340);
  sel.value = ch ? ch.device : (ports.some(p => p.device === prev) ? prev : ports[0].device);
  return ports;
}
async function refreshDriver() {
  const d = await api.driver_status();
  const chip = $("#driverChip"), btn = $("#driverBtn");
  chip.className = "chip " + ({ ok: "ok", installed: "warn", missing: "bad", nonwin: "" })[d.state];
  chip.textContent = ({ ok: "USB ✓ supply detected", installed: "Driver OK · plug in supply", missing: "CH340 driver missing", nonwin: "Driver: built-in" })[d.state];
  chip.title = d.msg;
  btn.hidden = !(d.state === "missing" || d.state === "installed");
  btn.textContent = d.state === "installed" ? "Reinstall driver" : "Install driver";
  return d;
}
async function connectToggle() {
  if (S.connected) { S.userOff = true; await api.disconnect(); return; }
  const port = $("#portSel").value;
  $("#connectBtn").disabled = true;
  const r = await api.connect(port);
  $("#connectBtn").disabled = false;
  if (!r.ok) toast(r.msg, "err", 6000); else saveSettings();
}
let autoBusy = false;
async function autoLoop() {
  if ($("#autoConn").checked && !S.connected && !autoBusy) {
    autoBusy = true;
    try {
      const ports = await refreshPorts();
      const d = await refreshDriver();
      if (ports.some(p => p.ch340) || (await api.info()).sim) {
        const r = await api.auto_connect();
        if (!r.ok && d.state === "ok") $("#statusTxt").textContent = r.msg;
      }
    } finally { autoBusy = false; }
  }
  setTimeout(autoLoop, 3000);
}

// ---------------------------------------------------------------- setpoint controls
const SP = {
  v: { el: "#spV", fn: "set_voltage", max: () => S.limits.vmax },
  i: { el: "#spI", fn: "set_current", max: () => S.limits.imax },
  ovp: { el: "#spOVP", fn: "set_ovp", max: () => S.limits.ovp },
  ocp: { el: "#spOCP", fn: "set_ocp", max: () => S.limits.ocp },
};
async function applySP(k) {
  const d = SP[k], el = $(d.el);
  let v = num(el.value);
  if (isNaN(v)) return;
  if (v > d.max()) { toast(`Clamped to ${d.max()} (model limit)`, "", 2000); v = d.max(); }
  S.dirty[k] = false; el.classList.remove("dirty");
  if (!S.connected) { toast("Not connected", "err"); return; }
  const r = await api[d.fn](clamp(v, 0, d.max()));
  if (!r.ok) toast(r.msg, "err");
}
function bindSetpoints() {
  for (const k in SP) {
    const box = $(`.sp[data-k="${k}"]`), el = $(SP[k].el);
    el.addEventListener("input", () => { S.dirty[k] = true; el.classList.add("dirty"); });
    el.addEventListener("keydown", e => { if (e.key === "Enter") { applySP(k); el.blur(); } if (e.key === "Escape") { S.dirty[k] = false; el.classList.remove("dirty"); el.blur(); } });
    el.addEventListener("blur", () => { if (S.dirty[k]) applySP(k); });
    const nudge = dir => { const v = (num(el.value) || 0) + dir * S.step; el.value = clamp(v, 0, SP[k].max()).toFixed(3); S.dirty[k] = true; clearTimeout(el._t); el._t = setTimeout(() => applySP(k), 250); };
    el.addEventListener("wheel", e => { if (document.activeElement === el || e.ctrlKey) { e.preventDefault(); nudge(e.deltaY < 0 ? 1 : -1); } }, { passive: false });
    $$("button", box).forEach(b => b.addEventListener("click", () => nudge(+b.dataset.d)));
  }
  $$(".stepsize button").forEach(b => b.addEventListener("click", () => {
    $$(".stepsize button").forEach(x => x.classList.remove("on")); b.classList.add("on"); S.step = +b.dataset.s;
  }));
  $("#outBtn").addEventListener("click", async () => {
    const on = !S.state.out; S.userOff = !on;
    if (!on && S.seqStatus.running) await api.seq_stop();
    const r = await api.set_output(on); if (!r.ok) toast(r.msg, "err");
  });
  document.addEventListener("keydown", async e => {
    if (e.key === "Escape" && S.connected && !document.querySelector("dialog[open]")) {
      S.userOff = true; await api.seq_stop(); await api.set_output(false); toast("Output OFF");
    }
  });
}

// ---------------------------------------------------------------- presets
function renderPresets() {
  $("#presetGrid").innerHTML = S.presets.map((p, k) => p
    ? `<button class="preset" data-k="${k}"><b>M${k + 1}${p.name ? " · " + esc(p.name) : ""}</b><span class="pv">${fmt(p.v, 2)}V</span> <span class="pi">${fmt(p.i, 2)}A</span></button>`
    : `<button class="preset empty" data-k="${k}"><b>M${k + 1}</b>empty</button>`).join("");
}
function esc(s) { return String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]); }
function bindPresets() {
  const grid = $("#presetGrid");
  grid.addEventListener("click", async e => {
    const b = e.target.closest(".preset"); if (!b) return;
    const p = S.presets[+b.dataset.k];
    if (!p) { toast("Right-click a preset to save the current settings into it"); return; }
    if (!S.connected) { toast("Not connected", "err"); return; }
    const r = await api.apply_preset(p);
    toast(r.ok ? `Loaded M${+b.dataset.k + 1}: ${p.v} V / ${p.i} A` : r.msg, r.ok ? "" : "err");
  });
  grid.addEventListener("contextmenu", e => {
    const b = e.target.closest(".preset"); if (!b) return; e.preventDefault();
    const k = +b.dataset.k, st = S.state;
    if (!S.connected) { toast("Connect first so I can read the current settings", "err"); return; }
    const name = prompt(`Save current settings to M${k + 1}?\n${fmt(st.v_set, 3)} V · ${fmt(st.i_set, 3)} A · OVP ${fmt(st.ovp, 2)} · OCP ${fmt(st.ocp, 3)}\n\nOptional name:`, S.presets[k]?.name || "");
    if (name === null) return;
    S.presets[k] = { name: name.trim(), v: st.v_set, i: st.i_set, ovp: st.ovp, ocp: st.ocp };
    renderPresets(); saveSettings();
  });
}

// ---------------------------------------------------------------- waveform table
function renderTable() {
  const body = $("#waveBody");
  $("#waveEmpty").hidden = S.steps.length > 0;
  const LIMIT = 3000;
  const rows = S.steps.slice(0, LIMIT).map((s, k) => `<tr data-k="${k}" class="${s.en ? "" : "disabled"} ${S.sel.has(k) ? "sel" : ""}">
    <td><input type="checkbox" class="rs" ${S.sel.has(k) ? "checked" : ""}></td><td>${k + 1}</td>
    <td class="cv"><input type="number" data-f="v" step="0.01" value="${+s.v.toFixed(3)}"></td>
    <td class="ci"><input type="number" data-f="i" step="0.001" value="${+s.i.toFixed(3)}"></td>
    <td><input type="number" data-f="dt" step="0.05" min="0.05" value="${+s.dt.toFixed(3)}"></td>
    <td><input type="checkbox" class="en" ${s.en ? "checked" : ""}></td></tr>`);
  if (S.steps.length > LIMIT) rows.push(`<tr><td colspan="6" class="hint" style="text-align:center;padding:10px">… ${S.steps.length - LIMIT} more steps (not shown, still run)</td></tr>`);
  body.innerHTML = rows.join("");
  updateWavePreview();
}
function updateWavePreview() {
  const [t, v, i] = stepSeries(S.steps);
  waveChart.setData([t, v, i]);
  const en = S.steps.filter(s => s.en), total = en.reduce((a, s) => a + Math.max(.05, s.dt), 0);
  const vmax = en.length ? Math.max(...en.map(s => s.v)) : 0, imax = en.length ? Math.max(...en.map(s => s.i)) : 0;
  const fast = en.filter(s => s.dt < 0.05).length;
  const over = en.filter(s => s.v > S.limits.vmax || s.i > S.limits.imax).length;
  $("#waveSum").innerHTML = `<span>Steps <b>${en.length}</b>${en.length !== S.steps.length ? ` / ${S.steps.length}` : ""}</span>
    <span>Duration <b>${fmtTime(total)}</b></span><span>Peak <b>${fmt(vmax, 2)} V</b> · <b>${fmt(imax, 2)} A</b></span>
    ${fast ? `<span style="color:var(--v)">${fast} steps &lt; 50 ms will be stretched</span>` : ""}
    ${over ? `<span style="color:var(--bad)">${over} steps exceed ${S.limits.vmax} V / ${S.limits.imax} A</span>` : ""}`;
  $("#toStep").placeholder = S.steps.length || "end";
}
function bindTable() {
  const body = $("#waveBody");
  body.addEventListener("change", e => {
    const tr = e.target.closest("tr"); if (!tr || tr.dataset.k == null) return;
    const k = +tr.dataset.k, s = S.steps[k];
    if (e.target.classList.contains("en")) { s.en = e.target.checked; tr.classList.toggle("disabled", !s.en); }
    else if (e.target.classList.contains("rs")) { e.target.checked ? S.sel.add(k) : S.sel.delete(k); tr.classList.toggle("sel", e.target.checked); return; }
    else { const f = e.target.dataset.f, v = num(e.target.value); if (!isNaN(v)) s[f] = f === "dt" ? Math.max(0.05, v) : Math.max(0, v); }
    updateWavePreview(); saveSettings();
  });
  body.addEventListener("keydown", e => {   // Enter/arrow navigation like a spreadsheet
    if (!["Enter", "ArrowDown", "ArrowUp"].includes(e.key) || e.target.type !== "number") return;
    const tr = e.target.closest("tr"), f = e.target.dataset.f;
    const next = e.key === "ArrowUp" ? tr.previousElementSibling : tr.nextElementSibling;
    if (next) { e.preventDefault(); const inp = $(`input[data-f="${f}"]`, next); inp && (inp.focus(), inp.select()); }
  });
  $("#selAll").addEventListener("change", e => { S.sel = e.target.checked ? new Set(S.steps.map((_, k) => k)) : new Set(); renderTable(); });
  $("#addRow").addEventListener("click", () => {
    const last = S.steps[S.steps.length - 1] || { v: S.state.v_set || 5, i: S.state.i_set || 1, dt: 1 };
    S.steps.push({ v: last.v, i: last.i, dt: last.dt, en: true }); renderTable(); saveSettings();
    const wrap = $(".wave-table-wrap"); wrap.scrollTop = wrap.scrollHeight;
  });
  $("#delRows").addEventListener("click", () => {
    if (!S.sel.size) { toast("Tick rows on the left to delete them"); return; }
    S.steps = S.steps.filter((_, k) => !S.sel.has(k)); S.sel.clear(); $("#selAll").checked = false; renderTable(); saveSettings();
  });
  $("#clearWave").addEventListener("click", () => {
    if (S.steps.length && !confirm(`Clear all ${S.steps.length} steps?`)) return;
    S.steps = []; S.sel.clear(); renderTable(); saveSettings();
  });
  $("#exportWave").addEventListener("click", () => {
    const csv = "voltage_V,current_A,time_s,enabled\n" + S.steps.map(s => `${s.v},${s.i},${s.dt},${s.en ? 1 : 0}`).join("\n");
    saveText("waveform.csv", csv);
  });
}

// ---------------------------------------------------------------- sequencer
function bindRun() {
  $("#runBtn").addEventListener("click", async () => {
    if (!S.steps.length) { toast("Add some steps first", "err"); return; }
    const from = Math.max(1, parseInt($("#fromStep").value) || 1) - 1;
    const toV = parseInt($("#toStep").value); const to = isNaN(toV) ? null : Math.min(S.steps.length, toV) - 1;
    const opts = { loops: Math.max(0, parseInt($("#loops").value) || 0), start: from, end: to, output_on: $("#outOn").checked, output_off: $("#outOff").checked };
    if (opts.loops === 0 && !confirm("Loops = 0 runs forever until you press Stop (or Esc). Continue?")) return;
    const r = await api.seq_start(S.steps.map(s => ({ ...s })), opts);
    if (!r.ok) toast(r.msg, "err", 5000); else { S.runFrom = from; S.runTo = to; saveSettings(); }
  });
  const stop = async () => { await api.seq_stop(); };
  $("#stopBtn").addEventListener("click", stop);
  $("#seqMiniStop").addEventListener("click", stop);
}
let lastActive = -1;
function renderSeq(st) {
  const was = S.seqStatus.running;
  S.seqStatus = st;
  $("#runBtn").disabled = st.running; $("#stopBtn").disabled = !st.running;
  $("#seqMini").hidden = !st.running;
  if (st.running) {
    const pct = st.total ? clamp(st.elapsed / st.total * 100, 0, 100) : 0;
    $("#seqBar").style.width = pct + "%";
    const loopTxt = st.loops === 0 ? `loop ${st.loop}/∞` : `loop ${st.loop}/${st.loops}`;
    $("#seqTxt").textContent = `Step ${st.index + 1} · ${fmt(st.step_left, 1)} s left · ${loopTxt} · ${fmtTime(st.elapsed)} / ${fmtTime(st.total)}`;
    $("#seqMiniTxt").textContent = `Step ${st.index + 1} · ${loopTxt}`;
    // highlight active row and play-head
    if (st.index !== lastActive) {
      $$("#waveBody tr.active").forEach(tr => tr.classList.remove("active"));
      const tr = $(`#waveBody tr[data-k="${st.index}"]`);
      if (tr) { tr.classList.add("active"); if ($("#pane-wave").classList.contains("on")) tr.scrollIntoView({ block: "nearest" }); }
      lastActive = st.index;
    }
    let tStart = 0; for (let k = 0; k < st.index && k < S.steps.length; k++) if (S.steps[k].en) tStart += Math.max(.05, S.steps[k].dt);
    const cur = S.steps[st.index]; waveChart._playT = tStart + (cur ? Math.max(.05, cur.dt) - st.step_left : 0); waveChart.redraw(false);
  } else if (was) {
    S.seqEndedAt = Date.now();
    $("#seqBar").style.width = st.msg === "Finished" ? "100%" : "0%";
    $("#seqTxt").textContent = st.msg || "Stopped";
    $$("#waveBody tr.active").forEach(tr => tr.classList.remove("active")); lastActive = -1;
    waveChart._playT = null; waveChart.redraw(false);
    toast("Waveform: " + (st.msg || "stopped"));
  }
}

// ---------------------------------------------------------------- import / paste mapper
const ROLES = [["ignore", "— ignore —"], ["time", "Time (absolute)"], ["dur", "Step duration"], ["volt", "Voltage"], ["curr", "Current"], ["en", "Enabled (1/0)"]];
const M = { sheets: [], rows: [], roles: [] };

function parseText(text) {
  const lines = text.replace(/\r/g, "").split("\n").filter(l => l.trim());
  if (!lines.length) return [];
  const sample = lines.slice(0, 20).join("\n");
  let delim = sample.includes("\t") ? "\t" : sample.includes(";") ? ";" : /,/.test(sample) && !/\d,\d+\s*$/m.test(sample.replace(/,/g, "")) ? "," : null;
  if (!delim) delim = sample.includes(",") ? "," : /\s+/;
  const rows = lines.map(l => (delim instanceof RegExp ? l.trim().split(delim) : l.split(delim)).map(c => c.trim()));
  // decimal commas (European Excel) when the delimiter isn't a comma
  if (delim !== ",") for (const r of rows) for (let k = 0; k < r.length; k++) if (/^-?\d+,\d+$/.test(r[k])) r[k] = r[k].replace(",", ".");
  return rows;
}
function isNumRow(r) { return r.some(c => c !== "" && !isNaN(num(c))); }
function guessRoles(rows, header) {
  const ncol = Math.max(...rows.slice(0, 50).map(r => r.length));
  const h = header ? rows[0].map(c => String(c).toLowerCase()) : [];
  const roles = Array(ncol).fill("ignore");
  const used = new Set();
  const pick = (k, role) => { if (!used.has(role)) { roles[k] = role; used.add(role); } };
  for (let k = 0; k < ncol; k++) {
    const s = (h[k] || "").replace(/[_\-()\[\]]/g, " ");
    if (/(^|\b)(dt|dur|duration|step|hold|width|interval)/.test(s)) pick(k, "dur");
    else if (/(^|\b)(t|time|sec|secs|seconds|ms|timestamp)\b/.test(s) || /^time/.test(s)) pick(k, "time");
    else if (/(^|\b)(v|u|volt|volts|voltage|vout|vset)\b/.test(s) || /volt/.test(s)) pick(k, "volt");
    else if (/(^|\b)(i|a|amp|amps|current|iout|iset|ilim)\b/.test(s) || /curr|amp/.test(s)) pick(k, "curr");
    else if (/(^|\b)(en|on|enable|enabled|y\/n)\b/.test(s)) pick(k, "en");
  }
  if (!used.size) { // no header hints: guess by count
    const ord = ncol === 1 ? ["volt"] : ncol === 2 ? ["time", "volt"] : ncol === 3 ? ["time", "volt", "curr"] : ["volt", "curr", "dur", "en"];
    ord.forEach((r, k) => { if (k < ncol) roles[k] = r; });
    // a 3-col table whose first column isn't monotonic is probably V,I,dt (the vendor app's format)
    if (ncol === 3) {
      const col = rows.slice(header ? 1 : 0).map(r => num(r[0])).filter(x => !isNaN(x));
      const mono = col.every((x, k) => k === 0 || x > col[k - 1]);
      if (!mono) { roles[0] = "volt"; roles[1] = "curr"; roles[2] = "dur"; }
    }
  }
  return roles;
}
function openMapper(rows, { paste = false, sheets = null, title = "" } = {}) {
  M.sheets = sheets || [];
  $("#pasteRow").hidden = !paste;
  $("#sheetRow").hidden = !(sheets && sheets.length > 1);
  if (sheets) $("#sheetSel").innerHTML = sheets.map((s, k) => `<option value="${k}">${esc(s.name)} (${s.rows.length} rows)</option>`).join("");
  $("#mapTitle").textContent = title || (paste ? "Paste lookup table" : "Import table");
  setMapperRows(rows);
  const dlg = $("#mapDlg");
  if (!dlg.open) dlg.showModal();
  if (paste) setTimeout(() => $("#pasteArea").focus(), 30);
}
function setMapperRows(rows) {
  M.rows = rows.map(r => r.map(c => typeof c === "number" ? c : String(c ?? "")));
  const header = M.rows.length > 1 && !isNumRow(M.rows[0]) && isNumRow(M.rows[1]);
  $("#hasHeader").checked = header;
  M.roles = M.rows.length ? guessRoles(M.rows, header) : [];
  // unit hints from header text
  if (header) {
    const h = M.rows[0].join(" ").toLowerCase();
    const w = u => new RegExp("(^|[^a-z])" + u + "([^a-z]|$)").test(h);
    $("#tUnit").value = w("ms") || /millisec/.test(h) ? "0.001" : w("min") || /minute/.test(h) ? "60" : "1";
    $("#iUnit").value = w("ma") || /milliamp/.test(h) ? "0.001" : "1";
    $("#vUnit").value = w("mv") || /millivolt/.test(h) ? "0.001" : "1";
  }
  renderMapper();
}
function renderMapper() {
  const ncol = M.roles.length, header = $("#hasHeader").checked;
  const head = `<tr>${M.roles.map((r, k) => `<th><select data-c="${k}" class="r-${r}">${ROLES.map(([v, l]) => `<option value="${v}" ${v === r ? "selected" : ""}>${l}</option>`).join("")}</select></th>`).join("")}</tr>`;
  const body = M.rows.slice(0, 12).map((r, k) => `<tr class="${header && k === 0 ? "hdr" : ""}">${Array.from({ length: ncol }, (_, c) => `<td>${esc(r[c] ?? "")}</td>`).join("")}</tr>`).join("");
  $("#mapPreview").innerHTML = ncol ? head + body + (M.rows.length > 12 ? `<tr><td colspan="${ncol}" class="hint">… ${M.rows.length - 12} more rows</td></tr>` : "") : "";
  validateMapper();
}
function buildSteps() {
  const header = $("#hasHeader").checked;
  const rows = M.rows.slice(header ? 1 : 0);
  const col = role => M.roles.indexOf(role);
  const [ct, cd, cv, ci, ce] = ["time", "dur", "volt", "curr", "en"].map(col);
  const tU = +$("#tUnit").value, vU = +$("#vUnit").value, iU = +$("#iUnit").value;
  const defDt = Math.max(0.05, num($("#defDt").value) || 1), defI = num($("#defI").value) || 0, defV = num($("#defV").value) || 0;
  const warn = [];
  if (cv < 0 && ci < 0) return { err: "Pick at least a Voltage or a Current column." };
  let pts = [];
  for (const r of rows) {
    const g = c => c < 0 ? NaN : num(r[c]);
    const v = cv >= 0 ? g(cv) * vU : defV, i = ci >= 0 ? g(ci) * iU : defI;
    if (isNaN(v) || isNaN(i)) continue;
    const p = { v, i, en: ce >= 0 ? !/^(0|n|no|false|off)$/i.test(String(r[ce]).trim()) : true };
    if (ct >= 0) { p.t = g(ct) * tU; if (isNaN(p.t)) continue; }
    if (cd >= 0) p.dt = g(cd) * tU;
    pts.push(p);
  }
  if (!pts.length) return { err: "No numeric rows found with those columns." };
  let steps;
  if (ct >= 0 && cd < 0) {
    // absolute time LUT -> durations from differences
    pts = pts.filter((p, k) => k === 0 || p.t > pts[k - 1].t);
    const diffs = pts.slice(1).map((p, k) => p.t - pts[k].t);
    const med = diffs.length ? diffs.slice().sort((a, b) => a - b)[diffs.length >> 1] : defDt;
    const lastDt = num($("#lastDt").value) || med || defDt;
    const res = num($("#resample").value) || 0;
    if (res > 0) {
      steps = []; const t0 = pts[0].t, t1 = pts[pts.length - 1].t; let k = 0;
      for (let t = t0; t < t1 + 1e-9; t += res) {
        while (k < pts.length - 2 && pts[k + 1].t <= t) k++;
        const a = pts[k], b = pts[Math.min(k + 1, pts.length - 1)];
        const f = b.t > a.t ? clamp((t - a.t) / (b.t - a.t), 0, 1) : 0;
        steps.push({ v: a.v + (b.v - a.v) * f, i: a.i + (b.i - a.i) * f, dt: res, en: true });
      }
      steps[steps.length - 1].dt = Math.max(res, lastDt);
    } else {
      steps = pts.map((p, k) => ({ v: p.v, i: p.i, dt: k < pts.length - 1 ? pts[k + 1].t - p.t : lastDt, en: p.en }));
    }
    if (pts[0].t > 0.0001) warn.push(`Time starts at ${fmt(pts[0].t, 3)} s; it is shifted to 0.`);
  } else {
    steps = pts.map(p => ({ v: p.v, i: p.i, dt: cd >= 0 && !isNaN(p.dt) ? p.dt : defDt, en: p.en }));
  }
  const fast = steps.filter(s => s.dt < 0.05).length;
  if (fast) warn.push(`${fast} steps are shorter than 50 ms; the supply can't follow that fast, so they'll be stretched to 50 ms. Try "Resample to 0.1 s".`);
  const over = steps.filter(s => s.v > S.limits.vmax || s.i > S.limits.imax).length;
  if (over) warn.push(`${over} rows exceed ${S.limits.vmax} V / ${S.limits.imax} A - fix them before running.`);
  steps.forEach(s => { s.v = Math.round(Math.max(0, s.v) * 1000) / 1000; s.i = Math.round(Math.max(0, s.i) * 1000) / 1000; s.dt = Math.round(Math.max(0.05, s.dt) * 1000) / 1000; });
  return { steps, warn };
}
function validateMapper() {
  const msg = $("#mapMsg");
  if (!M.rows.length) { msg.className = "map-msg"; msg.textContent = $("#pasteRow").hidden ? "No rows in this sheet." : "Paste rows above (Ctrl+V)."; $("#mapOk").disabled = true; return; }
  const r = buildSteps();
  $("#mapOk").disabled = !!r.err;
  if (r.err) { msg.className = "map-msg err"; msg.textContent = r.err; return; }
  const total = r.steps.reduce((a, s) => a + s.dt, 0);
  msg.className = "map-msg " + (r.warn.length ? "warn" : "");
  msg.textContent = `${r.steps.length} steps · ${fmtTime(total)} total. ` + r.warn.join(" ");
  $("#mapOk").textContent = `Import ${r.steps.length} steps`;
}
function bindMapper() {
  $("#mapPreview").addEventListener("change", e => {
    const c = +e.target.dataset.c, role = e.target.value;
    if (role !== "ignore") M.roles = M.roles.map(r => r === role ? "ignore" : r);  // one column per role
    M.roles[c] = role; renderMapper();
  });
  ["#hasHeader", "#tUnit", "#vUnit", "#iUnit", "#defDt", "#defI", "#defV", "#resample", "#lastDt"].forEach(s => $(s).addEventListener("input", () => s === "#hasHeader" ? renderMapper() : validateMapper()));
  $("#pasteArea").addEventListener("input", e => { setMapperRows(parseText(e.target.value)); });
  $("#sheetSel").addEventListener("change", e => setMapperRows(M.sheets[+e.target.value].rows));
  $("#mapDlg").addEventListener("close", () => {
    if ($("#mapDlg").returnValue !== "ok") return;
    const r = buildSteps(); if (r.err) return;
    if ($("#mapMode").value === "append") S.steps.push(...r.steps); else { S.steps = r.steps; S.sel.clear(); }
    renderTable(); saveSettings(); showTab("wave");
    toast(`Imported ${r.steps.length} steps`);
  });
  $("#pasteBtn").addEventListener("click", () => { $("#pasteArea").value = ""; openMapper([], { paste: true }); });
  $("#importBtn").addEventListener("click", () => $("#fileIn").click());
  $("#fileIn").addEventListener("change", async e => {
    const f = e.target.files[0]; e.target.value = ""; if (!f) return;
    await importFile(f);
  });
  // Ctrl+V anywhere on the waveform tab (outside inputs) opens the mapper with clipboard rows
  document.addEventListener("paste", e => {
    if (!$("#pane-wave").classList.contains("on") || document.querySelector("dialog[open]")) return;
    if (["INPUT", "TEXTAREA"].includes(document.activeElement.tagName)) {
      // multi-cell paste into the table -> treat as LUT
      const txt = e.clipboardData.getData("text"); if (!/[\n\t]/.test(txt.trim())) return;
    }
    const txt = e.clipboardData.getData("text"); if (!txt.trim()) return;
    e.preventDefault();
    $("#pasteArea").value = txt; openMapper(parseText(txt), { paste: true });
  });
  // drag & drop files onto the window
  document.addEventListener("dragover", e => e.preventDefault());
  document.addEventListener("drop", async e => { e.preventDefault(); const f = e.dataTransfer.files[0]; if (f) { showTab("wave"); await importFile(f); } });
}
async function importFile(f) {
  if (f.size > 25e6) { toast("File too big (25 MB max)", "err"); return; }
  const b64 = await new Promise((res, rej) => { const r = new FileReader(); r.onload = () => res(String(r.result).split(",")[1] || ""); r.onerror = rej; r.readAsDataURL(f); });
  toast("Reading " + f.name + "…", "", 1500);
  const r = await api.read_table_b64(f.name, b64);
  if (!r.ok) { toast(r.msg, "err", 6000); return; }
  const sheets = r.sheets.filter(s => s.rows.length);
  if (!sheets.length) { toast("That file has no data rows", "err"); return; }
  openMapper(sheets[0].rows, { sheets, title: "Import · " + f.name });
}

// ---------------------------------------------------------------- generator
function genSteps() {
  const shape = $("#gShape").value, lo = num($("#gMin").value) || 0, hi = num($("#gMax").value) || 0;
  const period = Math.max(0.1, num($("#gPeriod").value) || 1), cyc = Math.max(1, parseInt($("#gCycles").value) || 1);
  const n = Math.max(2, parseInt($("#gPts").value) || 2), I = num($("#gI").value) || 0;
  const dt = period / n, out = [];
  for (let c = 0; c < cyc; c++) for (let k = 0; k < n; k++) {
    const x = k / n; let f;
    switch (shape) {
      case "sine": f = 0.5 - 0.5 * Math.cos(2 * Math.PI * x); break;
      case "triangle": f = x < .5 ? 2 * x : 2 - 2 * x; break;
      case "square": f = x < .5 ? 1 : 0; break;
      case "rampup": case "saw": f = n > 1 ? k / (n - 1) : 1; break;
      case "rampdown": f = n > 1 ? 1 - k / (n - 1) : 0; break;
      case "stairs": f = n > 1 ? k / (n - 1) : 1; break;
    }
    out.push({ v: Math.round((lo + (hi - lo) * f) * 1000) / 1000, i: I, dt: Math.round(dt * 1000) / 1000, en: true });
  }
  if (shape === "square") { // merge identical neighbours so a square is 2 steps/cycle
    const m = []; for (const s of out) { const p = m[m.length - 1]; if (p && p.v === s.v && p.i === s.i) p.dt = Math.round((p.dt + s.dt) * 1000) / 1000; else m.push({ ...s }); } return m;
  }
  return out;
}
function bindGen() {
  const upd = () => {
    const s = genSteps(); const [t, v, i] = stepSeries(s); genChart.setData([t, v, i]);
    const dt = s[0]?.dt ?? 0, hi = Math.max(...s.map(x => x.v));
    const m = $("#genMsg");
    m.className = "map-msg " + (dt < 0.05 || hi > S.limits.vmax ? "warn" : "");
    m.textContent = `${s.length} steps of ${fmt(dt, 3)} s` + (dt < 0.05 ? " - too fast, use fewer points per cycle" : "") + (hi > S.limits.vmax ? ` - above ${S.limits.vmax} V` : "");
  };
  $$("#genDlg input, #genDlg select").forEach(e => e.addEventListener("input", upd));
  $("#genBtn").addEventListener("click", () => {
    $("#genDlg").showModal();
    if (!genChart) genChart = makeStepChart($("#genChart"), { legend: { show: false } });
    genChart.setSize(sized($("#genChart"))); upd();
  });
  $("#genDlg").addEventListener("close", () => {
    if ($("#genDlg").returnValue !== "ok") return;
    const s = genSteps();
    if ($("#genMode").value === "append") S.steps.push(...s); else { S.steps = s; S.sel.clear(); }
    renderTable(); saveSettings(); toast(`Inserted ${s.length} steps`);
  });
}

// ---------------------------------------------------------------- console
const hist = []; let hIdx = -1;
async function renderConsole() {
  if (!$("#pane-console").classList.contains("on")) return;
  const log = await api.get_console();
  const out = $("#consoleOut");
  const atBottom = out.scrollTop + out.clientHeight >= out.scrollHeight - 10;
  out.innerHTML = log.map(l => `<div><span class="t">${new Date(l.t * 1000).toLocaleTimeString()}</span><span class="${l.d === ">" ? "o" : "r"}">${l.d} ${esc(l.s)}</span></div>`).join("") || `<span class="hint">Commands you send (and what the app sends when you change settings) show up here.</span>`;
  if (atBottom) out.scrollTop = out.scrollHeight;
}
function bindConsole() {
  $("#consoleForm").addEventListener("submit", async e => {
    e.preventDefault();
    const inp = $("#consoleCmd"), cmd = inp.value.trim(); if (!cmd) return;
    if (!S.connected) { toast("Not connected", "err"); return; }
    hist.unshift(cmd); hIdx = -1; inp.value = "";
    const r = await api.scpi(cmd); if (!r.ok) toast(r.msg, "err");
    renderConsole();
  });
  $("#consoleCmd").addEventListener("keydown", e => {
    if (e.key === "ArrowUp" && hist.length) { hIdx = Math.min(hist.length - 1, hIdx + 1); e.target.value = hist[hIdx]; e.preventDefault(); }
    if (e.key === "ArrowDown") { hIdx = Math.max(-1, hIdx - 1); e.target.value = hIdx < 0 ? "" : hist[hIdx]; e.preventDefault(); }
  });
  setInterval(renderConsole, 700);
}

// ---------------------------------------------------------------- misc UI
function showTab(name) {
  $$(".tab").forEach(t => t.classList.toggle("on", t.dataset.tab === name));
  $$(".pane").forEach(p => p.classList.toggle("on", p.id === "pane-" + name));
  requestAnimationFrame(() => {
    if (name === "live") { liveChart.setSize(sized($("#liveChart"))); drawLive(); }
    if (name === "wave") waveChart.setSize(sized($("#waveChart")));
    if (name === "console") { renderConsole(); $("#consoleCmd").focus(); }
  });
}
async function saveText(name, text) {
  if (isDesktop()) {
    const r = await api.save_text(name, text);
    if (r.ok) toast("Saved " + r.path, "", 4000, { label: "Open folder", fn: () => api.open_folder(r.path) });
    return;
  }
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], { type: "text/csv" })); a.download = name; a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}
function renderRec(rec) {
  const b = $("#recBtn");
  b.classList.toggle("on", rec.on);
  b.lastChild.textContent = rec.on ? ` Recording · ${rec.rows}` : "Record";
}
function bindMisc() {
  $$(".tab").forEach(t => t.addEventListener("click", () => showTab(t.dataset.tab)));
  $$("#winSeg button").forEach(b => b.addEventListener("click", () => {
    $$("#winSeg button").forEach(x => x.classList.remove("on")); b.classList.add("on"); S.win = +b.dataset.w; drawLive();
  }));
  $("#showP").addEventListener("change", drawLive);
  $("#pauseBtn").addEventListener("click", e => { S.paused = !S.paused; e.target.textContent = S.paused ? "Resume" : "Pause"; });
  $("#clearLive").addEventListener("click", () => { S.live = { t: [], v: [], i: [], p: [] }; drawLive(); });
  $("#exportLive").addEventListener("click", () => {
    const L = S.live; let csv = "time_s,voltage_V,current_A,power_W\n";
    for (let k = 0; k < L.t.length; k++) csv += `${L.t[k].toFixed(3)},${L.v[k]},${L.i[k]},${L.p[k]}\n`;
    saveText("live_" + new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-") + ".csv", csv);
  });
  $("#pollSel").addEventListener("change", e => api.set_poll_rate(+e.target.value));
  $("#connectBtn").addEventListener("click", connectToggle);
  $("#refreshPorts").addEventListener("click", async () => { await refreshPorts(); await refreshDriver(); toast("Ports rescanned"); });
  $("#autoConn").addEventListener("change", saveSettings);
  $("#portSel").addEventListener("change", saveSettings);
  ["#loops", "#outOn", "#outOff"].forEach(s => $(s).addEventListener("change", saveSettings));
  $("#driverBtn").addEventListener("click", async () => {
    $("#driverBtn").disabled = true;
    const r = await api.install_driver();
    $("#driverBtn").disabled = false;
    toast(r.msg, r.ok ? "" : "err", 9000);
    // keep checking for the port to appear
    let n = 0; const iv = setInterval(async () => { const d = await refreshDriver(); if (d.state === "ok" || ++n > 40) { clearInterval(iv); if (d.state === "ok") { await refreshPorts(); toast("Supply detected - connecting…"); api.auto_connect(); } } }, 3000);
  });
  $("#recBtn").addEventListener("click", async () => {
    if ($("#recBtn").classList.contains("on")) {
      const r = await api.stop_record();
      if (r.path) toast(`Saved ${r.rows} rows to ${r.path}`, "", 6000, { label: "Open folder", fn: () => api.open_folder(r.path) });
    } else {
      if (!S.connected) { toast("Connect first", "err"); return; }
      const r = await api.start_record(); toast("Recording to " + r.path, "", 4000);
    }
  });
}

// ---------------------------------------------------------------- boot
async function boot() {
  makeLive();
  waveChart = makeStepChart($("#waveChart"));
  ro.observe($("#liveChart")); ro.observe($("#waveChart"));
  bindSetpoints(); bindPresets(); bindTable(); bindRun(); bindMapper(); bindGen(); bindConsole(); bindMisc();

  S.settings = (await api.load_settings()) || {};
  if (Array.isArray(S.settings.presets)) S.presets = S.settings.presets.concat(Array(8).fill(null)).slice(0, 8);
  if (Array.isArray(S.settings.steps)) S.steps = S.settings.steps;
  if (S.settings.run) { $("#loops").value = S.settings.run.loops ?? 1; $("#outOn").checked = S.settings.run.outOn !== false; $("#outOff").checked = S.settings.run.outOff !== false; }
  if (S.settings.autoConn === false) $("#autoConn").checked = false;
  if (!S.settings.presets) { // sensible starter presets
    S.presets = [{ name: "Logic", v: 3.3, i: 0.5, ovp: 3.6, ocp: 0.6 }, { name: "USB", v: 5, i: 1, ovp: 5.5, ocp: 1.2 },
      { name: "12 V", v: 12, i: 2, ovp: 13, ocp: 2.2 }, { name: "Car", v: 13.8, i: 5, ovp: 15, ocp: 5.5 }, null, null, null, null];
  }
  renderPresets(); renderTable(); onDisconnected();
  await refreshPorts(); await refreshDriver();
  poll(); autoLoop();
}
if (BROWSER) boot();
else if (window.pywebview && window.pywebview.api) boot();
else window.addEventListener("pywebviewready", boot, { once: true });
