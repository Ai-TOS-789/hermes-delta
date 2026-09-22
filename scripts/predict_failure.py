#!/usr/bin/env python3
"""Predictive Failure Forecasting — trend + error-rate engine (hermes-delta module 6).
Run periodically (cron). Accumulates samples in state file, forecasts OOM/disk-full/crash.
Emit prediction when P > 0.3 (per episodic-prediction skill thresholds)."""
import json, os, subprocess, time, sys

STATE = os.path.expanduser("~/.hermes/skills/hermes-delta/scripts/pf_state.jsonl")
HORIZON_MIN = 60
WEIGHTS = {"trend": 0.55, "pattern": 0.45}  # trend + threshold-pattern blend

def sh(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=15).stdout.strip()
    except Exception:
        return ""

def sample():
    free = sh("free -b")
    lines = free.splitlines()
    tot = used = 0
    if len(lines) > 1:
        parts = lines[1].split()
        tot, used = int(parts[1]), int(parts[2])
    mem_pct = round(100.0 * used / tot, 2) if tot else 0.0
    df = sh("df --output=pcent /").splitlines()[-1].strip().rstrip('%')
    disk_pct = float(df) if df.replace('.', '').isdigit() else 0.0
    load1 = 0.0
    try:
        load1 = os.getloadavg()[0]
    except Exception:
        pass
    # journal error rate: errors now vs previous window (per 10 min)
    now_err = sh("journalctl --since '-10 min' -p err --no-pager 2>/dev/null | wc -l")
    prev_err = sh("journalctl --since '-20 min' --until '-10 min' -p err --no-pager 2>/dev/null | wc -l")
    return {"ts": time.time(), "mem_pct": mem_pct, "disk_pct": disk_pct, "load1": round(load1, 2),
            "err_now": int(now_err) if now_err.isdigit() else 0,
            "err_prev": int(prev_err) if prev_err.isdigit() else 0}

def load_history():
    rows = []
    if os.path.exists(STATE):
        with open(STATE) as f:
            rows = [json.loads(l) for l in f if l.strip()]
    return rows

def slope(series):
    n = len(series)
    if n < 3:
        return 0.0
    xs = list(range(n))
    mx, my = sum(xs) / n, sum(series) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, series))
    den = sum((x - mx) ** 2 for x in xs)
    return num / den if den else 0.0

def predict(rows):
    preds = []
    win = rows[-20:]
    mem_sl = slope([r["mem_pct"] for r in win])          # %/sample
    disk_sl = slope([r["disk_pct"] for r in win])
    interval = (win[-1]["ts"] - win[0]["ts"]) / max(1, len(win) - 1) / 60.0  # min/sample
    for metric, val, sl, cap, name in (("mem", win[-1]["mem_pct"], mem_sl, 95.0, "OOM Kill"),
                                        ("disk", win[-1]["disk_pct"], disk_sl, 98.0, "Disk Full")):
        p_pattern = 1.0 if val >= 90 else (0.6 if val >= 80 else 0.15 if val >= 65 else 0.02)
        if sl > 0 and interval > 0:
            eta = (cap - val) / (sl / interval)  # minutes to cap
            p_trend = 0.85 if 0 < eta <= HORIZON_MIN else (0.4 if eta <= HORIZON_MIN * 4 else 0.05)
        else:
            eta, p_trend = None, 0.05
        p = round(min(1.0, p_pattern * WEIGHTS["pattern"] + p_trend * WEIGHTS["trend"]), 2)
        if p > 0.3:
            preds.append({"failure": name, "probability": p,
                          "eta_minutes": round(eta) if eta else None,
                          "current": val, "slope_per_hr": round(sl * 60 / interval, 2) if interval else 0})
    err_ratio = (win[-1]["err_now"] + 1) / (win[-1]["err_prev"] + 1)
    if win[-1]["err_now"] >= 10 or err_ratio >= 2.5:
        preds.append({"failure": "Error-burst crash risk", "probability": round(min(1.0, 0.3 + 0.2 * err_ratio), 2),
                      "eta_minutes": 15, "current": win[-1]["err_now"], "slope_per_hr": None})
    return preds

def main():
    s = sample()
    rows = load_history() + [s]
    with open(STATE, "a") as f:
        f.write(json.dumps(s) + "\n")
    preds = predict(rows) if len(rows) >= 3 else []
    out = {"sampled": s, "n_samples": len(rows), "predictions": preds,
           "auto_investigate": any(p["probability"] > 0.7 for p in preds)}
    print(json.dumps(out, indent=2))
    return 0 if not out["auto_investigate"] else 2

if __name__ == "__main__":
    sys.exit(main())
