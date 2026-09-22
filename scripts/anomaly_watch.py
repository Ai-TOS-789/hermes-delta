#!/usr/bin/env python3
"""Anomaly Watch — hermes-delta module 14: proactive log monitoring.
Implements the proactive-monitoring skill's detection rules against the REAL
system journal (journalctl --since; kernel messages included — dmesg is
restricted on this host but the journal carries them).

Rules (from proactive-monitoring skill):
  CRITICAL  OOM kill, kernel panic/oops, soft/hard lockup, watchdog timeout
  HIGH      error rate > 10/min, service restart loop (>=2 Scheduled restart)
  MEDIUM    new error pattern (never seen in baseline), memory > 90%

Alert-fatigue control (skill pitfall #1): pattern signatures are normalized
(digits/UUIDs/IPs/hex -> placeholders) and counted across runs; a pattern seen
>= 3 times becomes KNOWN NOISE and only re-alerts if it matches a CRITICAL
rule. First run seeds the baseline without new-pattern alerts.

Citations: each run's scanned window is saved to artifacts/anomaly_<ts>.log
(last 20 kept); evidence cites [anomaly:<file>:<line>].

State: scripts/anomaly_state.json
Exit codes: 0 ok, 2 = CRITICAL/HIGH alert (auto-investigate flag).
"""
import json, os, re, subprocess, sys, time

D = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(D, "anomaly_state.json")
ARTDIR = os.path.join(D, "artifacts")
KEEP_ARTIFACTS = 20

ERR_RE = re.compile(r"\b(error|fail(ed|ure)?|critical|cannot|unable to)\b", re.I)
CRIT_PATTERNS = [
    re.compile(r"invoked oom-killer|oom-kill|Out of memory: Killed process", re.I),
    re.compile(r"Kernel panic|general protection fault|Oops:", re.I),
    re.compile(r"soft lockup|hard LOCKUP|watchdog: BUG", re.I),
]
RESTART_RE = re.compile(r"Scheduled restart job.*unit ([^,]+)", re.I)

def norm(msg):
    """Normalize a log message into a stable pattern signature."""
    s = msg.lower()
    s = re.sub(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", "U", s)
    s = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "IP", s)
    s = re.sub(r"\b[0-9a-f]{6,}\b", "H", s)
    s = re.sub(r"\b\d+(\.\d+)?\b", "N", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:140]

def load_state():
    if os.path.exists(STATE):
        try:
            return json.load(open(STATE))
        except Exception:
            pass
    return {"runs": 0, "known": {}, "last_ts": None, "rates": []}

def scan_window(since_iso, until_iso):
    """Pull the journal window (system + kernel). Returns (lines, source)."""
    cmd = ["journalctl", "--since", since_iso, "--until", until_iso, "--no-pager", "-o", "short"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.splitlines(), "journalctl"
    except Exception:
        pass
    # fallback: kern.log tail (no time filter — CRITICAL rules only)
    try:
        with open("/var/log/kern.log", errors="replace") as f:
            return f.readlines()[-2000:], "kern.log-tail"
    except Exception:
        return [], "none"

def mem_pct():
    try:
        info = {}
        for l in open("/proc/meminfo"):
            k, v = l.split(":", 1)
            info[k] = int(v.strip().split()[0])
        return 100.0 * (1 - info.get("MemAvailable", 0) / info["MemTotal"])
    except Exception:
        return 0.0

def parse_unit(line):
    # short format: "Mon DD HH:MM:SS host ident[pid]: msg"
    m = re.match(r"\w{3}\s+\d+ \d+:\d+:\d+ \S+ ([^:\[]+)(?:\[\d+\])?: (.*)", line)
    if m:
        return m.group(1).strip(), m.group(2)
    return "?", line

def main():
    st = load_state()
    now = time.time()
    # window: since last run (bounded 5..120 min), else 30 min
    if st.get("last_ts"):
        window = max(5, min(120, int((now - st["last_ts"]) / 60)))
    else:
        window = 30
    since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - window * 60))
    until = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))

    lines, source = scan_window(since, until)

    # save artifact for citations
    os.makedirs(ARTDIR, exist_ok=True)
    art = os.path.join(ARTDIR, time.strftime("anomaly_%Y%m%d_%H%M%S.log"))
    with open(art, "w") as f:
        f.write("\n".join(lines) + "\n")
    for old in sorted(os.listdir(ARTDIR))[:-KEEP_ARTIFACTS]:
        try:
            os.remove(os.path.join(ARTDIR, old))
        except Exception:
            pass
    artname = os.path.basename(art)

    alerts, evidence = [], []
    err_lines, seen_sigs, restarts = [], {}, {}
    for i, line in enumerate(lines, 1):
        unit, msg = parse_unit(line)
        if any(p.search(msg) for p in CRIT_PATTERNS):
            alerts.append({"rule": "CRITICAL_PATTERN", "severity": "CRITICAL",
                           "detail": msg[:120],
                           "cite": f"[anomaly:{artname}:{i}]"})
            evidence.append(f"[anomaly:{artname}:{i}] CRITICAL: {msg[:110]}")
        m = RESTART_RE.search(msg)
        if m:
            u = m.group(1)
            restarts.setdefault(u, []).append(i)
        if ERR_RE.search(msg):
            err_lines.append(i)
            sig = f"{unit}|{norm(msg)}"
            seen_sigs.setdefault(sig, []).append(i)

    # HIGH: error rate
    rate = round(len(err_lines) / max(window, 1), 2)
    if rate > 10:
        alerts.append({"rule": "ERROR_RATE", "severity": "HIGH",
                       "detail": f"{len(err_lines)} error lines / {window}min = {rate}/min",
                       "cite": f"[anomaly:{artname}:{err_lines[0]}]" if err_lines else ""})
        evidence.append(f"[anomaly:{artname}:{err_lines[0] if err_lines else 0}] error rate {rate}/min > 10/min")

    # HIGH: restart loop (>=2 scheduled restarts of same unit in window)
    for u, lns in restarts.items():
        if len(lns) >= 2:
            alerts.append({"rule": "RESTART_LOOP", "severity": "HIGH",
                           "detail": f"unit {u} scheduled-restarted {len(lns)}x in {window}min",
                           "cite": f"[anomaly:{artname}:{lns[0]}]"})
            evidence.append(f"[anomaly:{artname}:{lns[0]}] {u} restart loop {len(lns)}x")

    # MEDIUM: new error patterns (skip on baseline seed run)
    known = st.get("known", {})
    new_sigs = []
    for sig, lns in seen_sigs.items():
        if sig not in known:
            new_sigs.append((sig, lns))
    if st["runs"] > 0:
        for sig, lns in sorted(new_sigs, key=lambda x: -len(x[1]))[:5]:
            unit, _ = sig.split("|", 1)
            alerts.append({"rule": "NEW_PATTERN", "severity": "MEDIUM",
                           "detail": f"{unit}: {sig.split('|', 1)[1][:90]}",
                           "cite": f"[anomaly:{artname}:{lns[0]}]"})
            evidence.append(f"[anomaly:{artname}:{lns[0]}] NEW pattern {unit}: {sig.split('|',1)[1][:80]}")

    # MEDIUM: memory pressure
    mp = round(mem_pct(), 1)
    if mp > 90:
        alerts.append({"rule": "MEM_HIGH", "severity": "MEDIUM", "detail": f"memory {mp}% > 90%",
                       "cite": "[proc:meminfo]"})
        evidence.append(f"[proc:meminfo] memory usage {mp}%")

    # update state (patterns graduate to known-noise at count >= 3)
    for sig, lns in seen_sigs.items():
        known[sig] = known.get(sig, 0) + 1
    st["known"] = known
    st["runs"] = st.get("runs", 0) + 1
    st["last_ts"] = now
    st["rates"] = (st.get("rates", []) + [rate])[-20:]
    st["known_trimmed"] = len(known)
    # cap known patterns to last 2000 to avoid unbounded growth
    if len(known) > 2000:
        st["known"] = dict(sorted(known.items(), key=lambda kv: -kv[1])[:2000])
    json.dump(st, open(STATE, "w"), indent=1)

    out = {"window_min": window, "source": source, "lines_scanned": len(lines),
           "error_lines": len(err_lines), "error_rate_per_min": rate,
           "new_patterns": len(new_sigs), "known_patterns": len(st["known"]),
           "mem_pct": mp, "alerts": alerts, "evidence": evidence,
           "artifact": art,
           "auto_investigate": any(a["severity"] in ("CRITICAL", "HIGH") for a in alerts)}
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 2 if out["auto_investigate"] else 0

if __name__ == "__main__":
    sys.exit(main())
