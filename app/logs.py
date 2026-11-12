"""
Saved logs: list the CSVs in the logs folder, read one back for the viewer,
and keep a small index.json with a friendly title and the result of
battery-charge logs.

The viewer gets every number worked out from the full file (duration, peaks,
Ah, Wh) but only a thinned-out copy of the points, so a 14-hour charge still
opens instantly.
"""
import csv
import io
import json
import os
import re
import time

MAX_POINTS = 6000
INDEX = "index.json"


# ---------------------------------------------------------------- index
def _index_path(folder):
    return os.path.join(folder, INDEX)


def read_index(folder):
    try:
        with open(_index_path(folder), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_index(folder, idx):
    tmp = _index_path(folder) + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(idx, f, indent=1)
        os.replace(tmp, _index_path(folder))
    except OSError:
        pass


def update_index(folder, name, **info):
    idx = read_index(folder)
    idx.setdefault(name, {}).update({k: v for k, v in info.items() if v is not None})
    _write_index(folder, idx)


def drop_from_index(folder, name):
    idx = read_index(folder)
    if idx.pop(name, None) is not None:
        _write_index(folder, idx)


# ---------------------------------------------------------------- listing
def _last_line(path):
    """Last non-empty line of a file without reading the whole thing."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        back = min(size, 4096)
        f.seek(size - back)
        lines = [ln for ln in f.read().split(b"\n") if ln.strip()]
    return lines[-1].decode("utf-8", "replace") if lines else ""


def _quick_span(path):
    """(start timestamp text, duration s) from the first and last rows, or (None, None)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            f.readline()
            first = f.readline()
        last = _last_line(path)
        a, b = first.split(","), last.split(",")
        return a[0], max(0.0, float(b[1]) - float(a[1]))
    except (OSError, ValueError, IndexError):
        return None, None


def list_logs(folder, recording_path=""):
    if not os.path.isdir(folder):
        return []
    idx = read_index(folder)
    out = []
    for name in os.listdir(folder):
        if not name.lower().endswith(".csv"):
            continue
        path = os.path.join(folder, name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        started, dur = _quick_span(path)
        info = idx.get(name, {})
        kind = info.get("kind") or ("charge" if name.startswith("charge_") else "record")
        out.append(dict(
            name=name, size=st.st_size, mtime=st.st_mtime, started=started, duration=dur,
            title=info.get("title") or _title_from_name(name), kind=kind,
            result=info.get("result"), msg=info.get("msg"),
            recording=bool(recording_path) and os.path.abspath(path) == os.path.abspath(recording_path),
        ))
    out.sort(key=lambda r: r["mtime"], reverse=True)
    return out


def _title_from_name(name):
    base = os.path.splitext(name)[0]
    m = re.match(r"^(log|charge)_(\d{8})_(\d{6})_?(.*)$", base)
    if not m:
        return base
    kind, label = m.group(1), m.group(4).replace("-", " ").strip()
    if kind == "charge":
        return "Charge" + (f": {label}" if label else "")
    return label or "Recording"


def safe_name(text):
    s = re.sub(r"[^A-Za-z0-9.]+", "-", text).strip("-")
    return s[:40]


# ---------------------------------------------------------------- reading
def _find_cols(header):
    h = [c.strip().lower() for c in header]

    def pick(*keys, avoid=("set",)):
        # try the most specific key first across all columns, then the looser ones
        for k in keys:
            for n, c in enumerate(h):
                if k in c and not any(a in c and a not in k for a in avoid + ("stamp",)):
                    return n
        return None

    return dict(
        stamp=pick("timestamp", "date", avoid=()),
        t=pick("time_s", "time", "sec", "elapsed", avoid=("timestamp", "stamp")),
        v=pick("voltage", "volt", "_v", avoid=("set",)),
        i=pick("current", "curr", "amp", "_a", avoid=("set",)),
        p=pick("power", "watt", avoid=("set",)),
        sv=pick("set_v", avoid=()),
        si=pick("set_a", "set_i", avoid=()),
    )


def parse(text, name="log.csv"):
    """Parse a log CSV (ours, or anything with time/voltage/current columns)."""
    rd = csv.reader(io.StringIO(text))
    header = None
    for row in rd:
        if row and any(c.strip() for c in row):
            header = row
            break
    if not header:
        raise ValueError("The file is empty.")
    cols = _find_cols(header)
    if cols["v"] is None and cols["i"] is None:
        raise ValueError("Couldn't find voltage or current columns in this file.")

    def f(row, k):
        n = cols[k]
        if n is None or n >= len(row):
            return None
        try:
            return float(row[n])
        except ValueError:
            return None

    T, V, I, P, SV, SI = [], [], [], [], [], []
    first_stamp = last_stamp = None
    for n, row in enumerate(rd):
        if not row:
            continue
        v, i = f(row, "v"), f(row, "i")
        if v is None and i is None:
            continue
        t = f(row, "t")
        t = float(n) if t is None else t
        p = f(row, "p")
        if p is None and v is not None and i is not None:
            p = v * i
        T.append(t); V.append(v or 0.0); I.append(i or 0.0); P.append(p or 0.0)
        SV.append(f(row, "sv")); SI.append(f(row, "si"))
        if cols["stamp"] is not None and cols["stamp"] < len(row):
            if first_stamp is None:
                first_stamp = row[cols["stamp"]]
            last_stamp = row[cols["stamp"]]
    if not T:
        raise ValueError("No readings found in this file.")
    t0 = T[0]
    T = [t - t0 for t in T]

    # totals from every sample (trapezoid rule), ignoring gaps over 10 s (paused / reconnects)
    ah = wh = on_s = 0.0
    for k in range(1, len(T)):
        dt = T[k] - T[k - 1]
        if 0 < dt < 10:
            ah += (I[k] + I[k - 1]) / 2 * dt / 3600
            wh += (P[k] + P[k - 1]) / 2 * dt / 3600
            if I[k] > 0.001:
                on_s += dt
    pk = lambda a: max(range(len(a)), key=a.__getitem__)  # noqa: E731
    iv, ii, ip = pk(V), pk(I), pk(P)
    on = [k for k in range(len(I)) if I[k] > 0.001]
    stats = dict(
        rows=len(T), duration=T[-1], started=first_stamp, ended=last_stamp,
        v_max=V[iv], v_max_t=T[iv], v_min=min(V), v_end=V[-1],
        i_max=I[ii], i_max_t=T[ii], i_avg=(sum(I[k] for k in on) / len(on)) if on else 0.0,
        p_max=P[ip], p_max_t=T[ip], ah=ah, wh=wh, on_s=on_s,
        rate=(len(T) - 1) / T[-1] if T[-1] > 0 else 0.0,
    )
    keep = _thin(I, V, MAX_POINTS)
    return dict(name=name, stats=stats, has_set=any(x is not None for x in SV),
                t=[round(T[k], 3) for k in keep], v=[V[k] for k in keep],
                i=[I[k] for k in keep], p=[P[k] for k in keep],
                sv=[SV[k] for k in keep], si=[SI[k] for k in keep])


def _thin(I, V, limit):
    """Indices to keep: per bucket, the highs and lows of current and voltage, so spikes survive."""
    n = len(I)
    if n <= limit:
        return list(range(n))
    buckets = limit // 4
    size = n / buckets
    keep = {0, n - 1}
    for b in range(buckets):
        lo, hi = int(b * size), min(n, int((b + 1) * size))
        if lo >= hi:
            continue
        rng = range(lo, hi)
        keep.update((min(rng, key=I.__getitem__), max(rng, key=I.__getitem__),
                     min(rng, key=V.__getitem__), max(rng, key=V.__getitem__)))
    return sorted(keep)


def load_file(path):
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        text = f.read()
    return parse(text, os.path.basename(path))


def stamp_now():
    return time.strftime("%Y%m%d_%H%M%S")
