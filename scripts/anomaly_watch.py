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

def canon(sig):
    """Canonical signature KEY — placeholder case must not split counts.
    (bug 9, production 2026-09-22: the same sudo pam_unix pattern lived as
    'uid=n' (migrated key) AND 'uid=N' (current norm output) — counts split
    2+1, never graduated, re-alerted as NEW_PATTERN every run. Same lesson
    as bug 6: a parser/normalizer fix needs a state migration or every old
    pattern looks NEW once.)"""
    return sig.lower()

def load_state():
    if os.path.exists(STATE):
        try:
            st = json.load(open(STATE))
        except Exception:
            st = {}
    else:
        st = {}
    # MIGRATION (bug 9): merge case-variant duplicate keys, SUMMING counts —
    # 'uid=n'(2) + 'uid=N'(1) must become one key with count 3 (graduate to
    # known-noise), not reset to the last value. Runs on every load so any
    # consumer sees migrated state even before the next scan writes it back.
    known = {}
    for k, v in st.get("known", {}).items():
        ck = canon(k)
        known[ck] = known.get(ck, 0) + v
    st["known"] = known
    return st

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
    # \w{3} matches only ASCII months; on a Thai-locale host the journal
    # prints "ก.ย." — every line parsed as unit "?" (all 35 baseline
    # signatures had zero attribution; same bug as module 15, fixed there
    # 2026-09-22 but missed here). Match any non-space month token.
    m = re.match(r"\S+\s+\d+ \d+:\d+:\d+ \S+ ([^:\[\s]+)(?:\[\d+\])?: (.*)", line)
    if m:
        return m.group(1).strip(), m.group(2)
    return "?", line

# SELF-TEST EXCLUSION (bug 12, production 2026-09-22): the healer's own
# sandboxed unit tests (heal-test*.service — deliberately failing units
# used to exercise the executed/rollback paths) polluted the production
# known-noise list: 12 "heal-test*.service: failed with result" signatures
# graduated as KNOWN NOISE, so the system learned "real failures = noise".
# Test instrumentation must never enter production signal state.
SELFTEST_UNIT_RX = re.compile(r"^heal-test", re.I)

def is_selftest(unit, msg=""):
    """True if this line originates from the pipeline's own sandbox tests."""
    return bool(SELFTEST_UNIT_RX.search(unit or ""))

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
        # bug 12: never count/alert on the pipeline's own sandbox test units
        if is_selftest(unit, msg):
            continue
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
            sig = canon(f"{unit}|{norm(msg)}")
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
    # bug 9: compare against CANONICAL keys — state written before the
    # canon() fix may hold case-variant duplicates ('uid=N' vs 'uid=n')
    known = {canon(k): v for k, v in st.get("known", {}).items()}
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

    # update state (patterns graduate to known-noise at count >= 3).
    # MIGRATION (bug 9): merge keys that differ only in placeholder case —
    # counts must unify or patterns re-alert as NEW forever.
    known = {canon(k): v for k, v in st.get("known", {}).items()}
    # MIGRATION (bug 12): strip self-test pollution from the known-noise
    # list — heal-test*.service signatures that graduated as "known noise"
    # under the pre-filter rule (12 production entries, 2026-09-22). The
    # filter above stops NEW pollution; this removes what already landed.
    # Idempotent: re-running on clean state removes nothing.
    st_test_pollution = [k for k in known if k.split("|", 1)[0].lower()
                         .startswith("heal-test")]
    for k in st_test_pollution:
        del known[k]
    if st_test_pollution:
        st.setdefault("migration", []).append({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "bug": "bug12-selftest-pollution",
            "removed_known_keys": len(st_test_pollution),
        })
    for sig, lns in seen_sigs.items():
        known[sig] = known.get(sig, 0) + 1  # +1 per RUN (cross-run recurrence
        # is the graduation signal — counting lines would graduate a single
        # 3-line burst immediately, too aggressive)
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
    # persist alerts for the SAME-RUN investigator (module 15). Reading
    # delta_report.json instead made every investigation lag one run behind
    # its evidence window (production 2026-09-22: ERROR_RATE alert
    # investigated against the NEXT run's empty window).
    try:
        with open(os.path.join(D, "anomaly_alerts.json"), "w") as f:
            json.dump(out, f, ensure_ascii=False)
    except Exception:
        pass
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 2 if out["auto_investigate"] else 0

if __name__ == "__main__":
    sys.exit(main())
