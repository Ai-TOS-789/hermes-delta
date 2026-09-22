#!/usr/bin/env python3
"""Fleet Sentinel — hermes-delta module 12: remote device health monitoring.
Probes fleet devices (ICMP latency, TCP port reachability), tracks per-device
history, forecasts degradation via latency slope (same linear-regression
approach as predict_failure.py). Emits cited evidence lines for the
system-investigation protocol.

State: scripts/fleet_state.jsonl  (one line per probe round)
Exit codes: 0 ok, 2 = device down / degraded (auto-investigate flag).
"""
import json, os, socket, subprocess, sys, time

D = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(D, "fleet_state.jsonl")
HORIZON_MIN = 60
# Fleet from HSAES config (subnet 192.168.31.0/24) — extend as devices change.
# TizenRT: ICMP-only — verified 2026-09-22: host alive (RST on all TCP ports,
# 0% loss, ARP REACHABLE) but no TCP services; single-ping latency is
# power-save jitter (4-77ms, mdev 28.8), NOT degradation.
FLEET = {
    "PI4":    {"ip": "192.168.31.59",  "ports": [22]},
    "TizenRT": {"ip": "192.168.31.23",  "ports": []},
    "NAS326": {"ip": "192.168.31.236", "ports": [22, 445, 5000]},
}

def ping_ms(ip):
    """Return (up, median_latency_ms) — median of 3 probes to filter
    embedded power-save jitter (TizenRT single-ping varies 4-77ms)."""
    lats = []
    for _ in range(3):
        try:
            r = subprocess.run(["ping", "-c", "1", "-W", "2", ip],
                               capture_output=True, text=True, timeout=5)
            if r.returncode != 0:
                continue
            for tok in r.stdout.split():
                if "time=" in tok:
                    lats.append(float(tok.split("=")[1]))
        except Exception:
            pass
    if not lats:
        # all 3 probes lost -> only then declare unreachable
        return False, None
    lats.sort()
    return True, lats[len(lats) // 2]

def port_open(ip, port, timeout=2.0):
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except Exception:
        return False

def slope(series):
    n = len(series)
    if n < 3:
        return 0.0
    xs = list(range(n))
    mx, my = sum(xs) / n, sum(series) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, series))
    den = sum((x - mx) ** 2 for x, xs2 in zip(xs, xs))
    return num / den if den else 0.0

def load_history():
    rows = []
    if os.path.exists(STATE):
        with open(STATE) as f:
            rows = [json.loads(l) for l in f if l.strip()]
    return rows

def main():
    now = time.time()
    probe = {"ts": now, "devices": {}}
    for name, cfg in FLEET.items():
        up, lat = ping_ms(cfg["ip"])
        ports = {p: port_open(cfg["ip"], p) for p in cfg["ports"]}
        probe["devices"][name] = {"ip": cfg["ip"], "up": up,
                                  "latency_ms": lat, "ports": ports}
    with open(STATE, "a") as f:
        f.write(json.dumps(probe) + "\n")

    rows = load_history()
    alerts, evidence = [], []
    for name, cfg in FLEET.items():
        hist = [r["devices"][name] for r in rows if name in r.get("devices", {})]
        cur = probe["devices"][name]
        # 1) hard-down: ICMP fails now
        if not cur["up"]:
            alerts.append({"device": name, "issue": "ICMP_DOWN",
                           "severity": "CRITICAL"})
            evidence.append(f"[fleet:{name}] ICMP probe failed at {time.strftime('%H:%M:%S')}")
            continue
        evidence.append(f"[fleet:{name}] up, latency={cur['latency_ms']}ms, ports={cur['ports']}")
        # 2) flapping: up/down transitions in last 10 rounds
        ups = [1 if h["up"] else 0 for h in hist[-10:]]
        if len(ups) >= 5 and 0 in ups and 1 in ups:
            alerts.append({"device": name, "issue": "FLAPPING",
                           "severity": "HIGH", "transitions": ups.count(0)})
            evidence.append(f"[fleet:{name}] flapping: {ups.count(0)}/{len(ups)} rounds down in last {len(ups)}")
        # 3) latency degradation trend (last 20 samples, ms slope)
        lats = [h["latency_ms"] for h in hist[-20:] if h.get("latency_ms")]
        if len(lats) >= 5:
            sl = slope(lats)
            interval = (hist[-1]["ts"] - hist[-20]["ts"]) / max(1, len(hist[-20:]) - 1) / 60.0 if len(hist) >= 20 else 1.0
            if sl > 0.5:  # >0.5 ms/sample growth = degradation
                alerts.append({"device": name, "issue": "LATENCY_DEGRADATION",
                               "severity": "MEDIUM", "slope_ms_per_hr": round(sl * 60 / max(interval, 0.1), 1)})
                evidence.append(f"[fleet:{name}] latency slope +{round(sl,2)}ms/sample over {len(lats)} samples")
        # 4) closed service port while host up
        dead_ports = [p for p, ok in cur["ports"].items() if not ok]
        if dead_ports:
            alerts.append({"device": name, "issue": "PORT_CLOSED",
                           "severity": "MEDIUM", "ports": dead_ports})
            evidence.append(f"[fleet:{name}] host up but ports {dead_ports} unreachable")

    out = {"rounds": len(rows), "devices": probe["devices"], "alerts": alerts,
           "evidence": evidence,
           "auto_investigate": any(a["severity"] in ("CRITICAL", "HIGH") for a in alerts)}
    print(json.dumps(out, indent=2))
    return 2 if out["auto_investigate"] else 0

if __name__ == "__main__":
    sys.exit(main())
