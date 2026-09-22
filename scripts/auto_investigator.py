#!/usr/bin/env python3
"""Auto-Investigator — hermes-delta module 15: closes the alert->action gap.
When anomaly_watch exits 2 (CRITICAL/HIGH) — or MEDIUM NEW_PATTERN per the
proactive-monitoring skill's SLA (investigate within 15 min) — this module
runs the system-investigation protocol AUTOMATICALLY:

  Phase 1 INGEST    capture system state (journal window, mem, load, units)
  Phase 2 HYPOTHESES generate 2-5 candidates from alert + recall of past
                    root causes (module 11 index — cross-session learning)
  Phase 3 EVIDENCE  grep the captured artifact for confirming/disconfirming
                    lines (each hypothesis declares its own patterns)
  Phase 4 SELF-CORRECT  CONFIRMED / REJECTED / PARTIAL per evidence rules
  Phase 5 REPORT    cited report -> investigations/inv_<ts>.md + history

No LLM calls: hypotheses are seeded from a rule table keyed by alert rule
(OOM -> memory-leak candidates, RESTART_LOOP -> crash-loop candidates, ...)
and enriched by root-cause recall, so it runs offline in <5s. Every claim
carries a [source:line] citation. Failed hypotheses are recorded, not hidden.

State: investigations/ (reports), inv_state.json (history index)
Exit codes: 0 ran (or nothing to do), 2 = investigation found CRITICAL root cause
"""
import json, os, re, subprocess, sys, time

D = os.path.dirname(os.path.abspath(__file__))
INVDIR = os.path.join(D, "investigations")
STATE = os.path.join(D, "inv_state.json")
REPORT = os.path.join(D, "delta_report.json")
RECALL_INDEX = os.path.join(D, "recall_index.json")
EXP = os.path.expanduser("~/.hermes/skills/agent-self-learning/experience.jsonl")

MAX_HYPOTHESES = 5          # self-correction-loop skill limit
MAX_ROUNDS = 3              # ditto

# ---------------------------------------------------------------- Phase 1: ingest
def sh(cmd, timeout=30):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""

def capture(alerts, window_min, alert=None):
    """Snapshot system state per the proactive-monitoring protocol.

    Evidence discipline (lesson 2026-09-22, production miss): read the
    artifact the ALERT ITSELF cites — `ls -t | head -1` grabs the CURRENT
    run's artifact, which by investigator time is a *different* journal
    window (the ERROR_RATE 16.25/min alert was investigated against a
    0-error window and wrongly closed as "insufficient evidence").
    """
    art_path = None
    # 1st choice: the artifact named in this alert's own citation
    cite = (alert or {}).get("cite", "")
    m = re.search(r"anomaly_(\d{8}_\d{6})\.log", cite)
    if m:
        cand = os.path.join(D, "artifacts", "anomaly_%s.log" % m.group(1))
        if os.path.isfile(cand):
            art_path = cand
    # 2nd choice: newest artifact (legacy behavior)
    if not art_path:
        art = sh("ls -t %s 2>/dev/null | head -1" % os.path.join(D, "artifacts"))
        if art and art != "investigations":
            cand = os.path.join(D, "artifacts", art)
            if os.path.isfile(cand):
                art_path = cand
    lines = []
    if art_path:
        lines = open(art_path, errors="replace").read().splitlines()
    return {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "artifact": "artifacts/" + os.path.basename(art_path) if art_path else None,
        "lines": lines,
        "mem_pct": sh("free | awk '/Mem:/{printf \"%.1f\", $3/$2*100}'"),
        "swap_pct": sh("free | awk '/Swap:/{if($2>0) printf \"%.1f\", $3/$2*100; else print \"n/a\"}'"),
        "load": sh("cat /proc/loadavg | cut -d' ' -f1-3"),
        "top_mem": sh("ps -eo comm,rss,pid --sort=-rss | head -6"),
        "failed_units": sh("systemctl --failed --no-legend | head -10"),
        "journal_tail": sh("journalctl -n 30 --no-pager -o short 2>/dev/null | tail -30"),
    }

def parse_unit(line):
    # journalctl short format: "<month> <day> <time> <host> <ident>[pid]: msg"
    # \w{3} only matches ASCII month abbreviations; on a Thai-locale host the
    # journal prints "ก.ย." — those lines parsed as unit "?" (zero attribution
    # on a 130-line error window; production 2026-09-22). Match any non-space
    # month token instead.
    m = re.match(r"\S+\s+\d+ \d+:\d+:\d+ \S+ ([^:\[\s]+)(?:\[\d+\])?: (.*)", line)
    return (m.group(1).strip(), m.group(2)) if m else ("?", line)

# ------------------------------------------------------- Phase 2: hypothesis seeds
# Rule table: alert rule -> candidate hypotheses (statement, layer,
# confirm pattern, disconfirm pattern). Recall adds cross-session candidates.
SEEDS = {
    "OOM": [
        ("A process leaked memory until the kernel OOM-killer reclaimed it",
         "kernel", r"invoked oom-killer|Out of memory: Killed process\s+(\S+)",
         r""),
    ],
    "OOM_KILLED": [
        ("The OOM victim itself was the memory hog (killed pid == top-RSS process)",
         "app", r"Out of memory: Killed process\s+\d+\s+\((\S+)\)",
         r""),
    ],
    "RESTART_LOOP": [
        ("The unit crashes on start (exit code / signal in journal before each restart)",
         "app", r"(?:Failed with result|Main process exited).{0,80}(signal|exit-code|start-limit)",
         r"", None),
        ("The unit is being stopped and started by config/timer, not crashing",
         "config", r"Started|Stopped.*(timer|cron|scheduled)", r"Failed with result", None),
        ("The unit's config changed on disk and systemd is running a stale copy (needs reload)",
         "config", r"changed on disk|daemon-reload", r"", None),
    ],
    "ERROR_RATE": [
        ("A single subsystem is flooding errors (top error unit > 50% of error lines)",
         "app", r"", r""),
    ],
    "NEW_PATTERN": [
        # alert_line_only: the desktop-noise regex must match the ALERT LINE
        # ITSELF, not the journal window — on a desktop machine the window
        # always contains gvfs/tracker lines, which false-confirmed desktop
        # noise for NON-desktop failures (production 2026-09-22: heal-test
        # .service failures were diagnosed as desktop noise). Same lesson
        # already applied to recall candidates; now the rule-table seeds too.
        # Wording discipline (bug 11, production 2026-09-22): do NOT claim
        # "transient" — tracker/gvfs chatter on this desktop machine RECURS
        # (module 22 scored that claim FALSE_POSITIVE when the pattern
        # recurred 3x). Claim only what the evidence shows: benign recurring
        # chatter that needs no action. The word "transient" is also a healer
        # KEYMAP trigger substring ("transient desktop-session error") —
        # keep the statement free of it OR keep the KEYMAP regex in sync.
        ("The new pattern is recurring desktop-session chatter (gvfs/tracker/gnome noise — benign on this desktop, recurrence expected)",
         "app", r"gvfs|tracker|gnome-shell|colord|pipewire", r"", "alert_line_only"),
        ("The new pattern indicates a real service defect (non-desktop unit)",
         "app", r"", r"gvfs|tracker|gnome-shell|colord|pipewire", "non_desktop_defect"),
        # desktop-SUBSYSTEM fault, not noise: gsd-* / pipewire / wireplumber
        # components ARE desktop components but their errors are real user-
        # facing faults (no audio sink). Distinct from gvfs/tracker chatter.
        # Production 2026-09-22: gsd-media-keys "unable to get default sink"
        # was closed INSUFFICIENT_EVIDENCE because no seed covered this class.
        # Wording discipline: must NOT contain "gvfs/tracker" or "transient
        # desktop-session error" — the healer KEYMAP maps those substrings to
        # MARK_DESKTOP_NOISE and would silence a real fault as noise.
        ("A desktop subsystem fault affecting user sessions (gsd-*/pipewire/wireplumber component error, not session chatter)",
         "app", r"gsd-|pipewire|wireplumber|pulse|xdg-desktop", r"gvfs|tracker", "desktop_subsystem_fault"),
    ],
    "MEM_HIGH": [
        ("Memory pressure from a single dominant process (top RSS > 30% of RAM)",
         "app", r"", r"", None),
    ],
}

def recall(query_words):
    """Query module 11's index for past root causes (cross-session learning)."""
    try:
        idx = json.load(open(RECALL_INDEX)).get("index", {})
        sys.path.insert(0, D)
        from root_cause_recall import tokens, query
        return query(query_words, idx)
    except Exception:
        return []

def hypotheses_for(alert, cap):
    """Generate 2-5 candidates for one alert (seeds + recall enrichment)."""
    rule = alert.get("rule", "")
    cands = []
    for seed in SEEDS.get(rule, []):
        stmt, layer, cpat, dpat = seed[0], seed[1], seed[2], seed[3]
        mode = seed[4] if len(seed) > 4 else None
        cands.append({"statement": stmt, "layer": layer,
                      "confirm_re": cpat, "disconfirm_re": dpat,
                      "source": "rule-table", "gate": mode,
                      "alert_detail": alert.get("detail", "")})
    # recall: past root causes matching the alert's symptom words.
    # Unverified (citation-rotted) recalls are demoted below verified ones —
    # they are hints, not confirmed knowledge.
    words = f"{rule} {alert.get('detail','')}"
    recalled = recall(words)
    verified = [r for r in recalled
                if not any("unverified" in o for o in r.get("seen_outcomes", []))]
    for r in (verified + [r for r in recalled if r not in verified])[:2]:
        # strip any inherited prefix — stored statements already carry
        # "Recalled:"/"**" from past generations and re-prefixing compounds
        # ("Recalled: **Recalled: **..." observed in production 2026-09-22)
        stmt = re.sub(r"^(\**recalled:\s*)+", "", r["pattern"][:120], flags=re.I)
        cands.append({"statement": f"Recalled: {stmt}",
                      "layer": "recall", "confirm_re": "", "disconfirm_re": "",
                      "source": "root-cause-recall",
                      "citations": r.get("citations", []),
                      "seen_outcomes": r.get("seen_outcomes", []),
                      "alert_detail": alert.get("detail", "")})
    return cands[:MAX_HYPOTHESES]

# ----------------------------------------------------- Phase 3/4: evidence + verdict
def test(h, lines, cap):
    """Test one hypothesis against captured evidence. Returns verdict dict."""
    conf, disconf = [], []
    cre = h.get("confirm_re") or ""
    dre = h.get("disconfirm_re") or ""
    art = cap["artifact"] or "artifacts/none"
    gate = h.get("gate")
    alert_line = (h.get("alert_detail") or "").lower()
    if gate == "alert_line_only":
        # desktop-noise detection must match the ALERT LINE ITSELF — the
        # journal window on a desktop machine always contains gvfs/tracker
        # lines, which false-confirmed noise for real service failures.
        if cre and re.search(cre, alert_line, re.I):
            conf.append(f"[alert-line] pattern matched alert itself: "
                        f"{alert_line[:110]}")
        elif cre:
            disconf.append(f"[alert-line] pattern NOT in the alert line — "
                           f"window-wide match would be a false positive here")
    elif gate == "desktop_subsystem_fault":
        # gsd-*/pipewire/wireplumber in the ALERT LINE confirm a desktop-
        # subsystem fault; gvfs/tracker in the ALERT LINE disconfirm (that
        # is desktop chatter, not a subsystem fault). Both directions on
        # the alert line itself — never the window (bug 4 lesson).
        if dre and re.search(dre, alert_line, re.I):
            disconf.append(f"[alert-line] gvfs/tracker chatter in the alert "
                           f"itself, not a subsystem fault: {alert_line[:110]}")
        elif cre and re.search(cre, alert_line, re.I):
            conf.append(f"[alert-line] desktop-subsystem component in the alert "
                        f"itself: {alert_line[:110]}")
    elif gate == "non_desktop_defect":
        # converse: desktop words in the ALERT LINE disconfirm "real defect";
        # a REAL failure signature in the alert line (not a mere unit NAME —
        # bug 8, production 2026-09-22: "Starting/Finished update-notifier-
        # download.service - ... packages that failed at package install
        # time..." is a benign lifecycle message whose unit DESCRIPTION
        # contains "failed"; `\.service` matched and it was CONFIRMED as a
        # "real service defect" while the unit exited successfully every
        # time. The confirm regex must match FAILURE SIGNATURES, and a
        # lifecycle-verb prefix actively disconfirms.)
        if dre and re.search(dre, alert_line, re.I):
            disconf.append(f"[alert-line] desktop component named in the alert "
                           f"itself: {alert_line[:110]}")
        elif re.search(r":\s*(starting|started|finished|stopping|stopped|"
                       r"deactivated|reloading)\b", alert_line):
            disconf.append(f"[alert-line] benign systemd lifecycle message "
                           f"(unit description wording, not a failure): "
                           f"{alert_line[:110]}")
        elif re.search(r"failed with result|main process exited|"
                       r"failed to start|exit-code|coredump|segfault",
                       alert_line):
            conf.append(f"[alert-line] service-failure signature in the alert "
                        f"itself, no desktop component: {alert_line[:110]}")
    elif cre:
        for i, ln in enumerate(lines, 1):
            if re.search(cre, ln):
                conf.append(f"[{art}:{i}] {ln.strip()[:110]}")
                if len(conf) >= 3:
                    break
    # window-wide disconfirm scan ONLY for ungated hypotheses. Gated ones
    # (alert_line_only / non_desktop_defect) settle both directions on the
    # alert line itself — a window-wide scan here would disconfirm EVERY
    # non-desktop hypothesis on a desktop machine (gnome-shell lines exist
    # in every window; production 2026-09-22: gsd-media-keys alert wrongly
    # REJECTED as "not a real defect" by unrelated desktop noise).
    if dre and not gate:
        for i, ln in enumerate(lines, 1):
            if re.search(dre, ln):
                disconf.append(f"[{art}:{i}] {ln.strip()[:110]}")
                if len(disconf) >= 3:
                    break

    statement = h["statement"]
    # analytic hypotheses (no regex) use captured state
    if statement.startswith("A single subsystem is flooding"):
        unit_counts = {}
        for ln in lines:
            u, m = parse_unit(ln)
            if re.search(r"\b(error|fail)", m, re.I):
                unit_counts[u] = unit_counts.get(u, 0) + 1
        total = sum(unit_counts.values()) or 1
        top = max(unit_counts.items(), key=lambda kv: kv[1]) if unit_counts else ("?", 0)
        if top[1] / total > 0.5 and top[1] >= 5:
            conf.append(f"[{art}] top error unit {top[0]} = {top[1]}/{total} error lines "
                        f"({100*top[1]//total}%) — single-subsystem flood")
        else:
            disconf.append(f"[{art}] errors spread across {len(unit_counts)} units "
                           f"(top {top[0]} {top[1]}/{total}) — no single-subsystem flood")
    elif statement.startswith("Memory pressure from a single dominant"):
        try:
            mem_pct = float(cap["mem_pct"] or 0)
        except ValueError:
            mem_pct = 0.0
        top = (cap["top_mem"] or "").splitlines()
        if mem_pct > 60 and len(top) > 1:
            conf.append(f"[proc:ps] mem {mem_pct}% top RSS: {top[1][:100]}")
        else:
            disconf.append(f"[proc:ps] mem {mem_pct}% — no single-process pressure")

    if h.get("source") == "root-cause-recall":
        # recall candidates: confirmed only if the past pattern's symptom words
        # appear in the ALERT LINE ITSELF, not the whole window. Testing showed
        # window-wide matching always finds desktop words (gvfs/gnome) on a
        # desktop machine and confirms unrelated recalled patterns.
        STOP = {"recalled", "pattern", "error", "transient", "session", "window",
                "lines", "match", "keywords", "supported", "cited", "evidence",
                "found", "cause", "root", "best", "indicates", "real", "service",
                "defect", "non", "desktop"}
        kw = [w for w in re.findall(r"[a-z]{4,}", h["statement"].lower())
              if w not in STOP][:6]
        alert_line = h.get("alert_detail", "").lower()
        hits = sum(1 for k in kw if k in alert_line) if kw else 0
        if hits >= 2:
            conf.append(f"[alert:{kw[:4]}] {hits}/{len(kw)} recalled-pattern keywords "
                        f"appear in the alert line itself")
        else:
            disconf.append(f"[alert:{kw[:4]}] only {hits}/{len(kw)} keywords in the "
                           f"alert line — recalled pattern does not repeat here")

    if conf and not disconf:
        verdict = "CONFIRMED"
    elif conf and disconf:
        verdict = "PARTIAL"
    elif disconf and not conf:
        verdict = "REJECTED"
    else:
        verdict = "INSUFFICIENT_EVIDENCE"
    return {"statement": statement, "layer": h.get("layer"),
            "source": h.get("source"), "verdict": verdict,
            "confirming": conf, "disconfirming": disconf,
            "citations": h.get("citations", [])}

# --------------------------------------------------------------- Phase 5: report
def render(alert, cap, results, verdict_summary):
    art = cap["artifact"] or "artifacts/none"
    L = []
    L.append(f"# Auto-Investigation — {alert['rule']} ({alert['severity']})")
    L.append(f"*{cap['ts']} — hermes-delta module 15, triggered by anomaly_watch*")
    L.append("")
    L.append(f"**Alert:** {alert['rule']} — {alert.get('detail','')[:160]}")
    L.append(f"**Cite:** {alert.get('cite','')}")
    L.append(f"**System:** mem {cap['mem_pct']}% | swap {cap['swap_pct']}% | load {cap['load']}")
    L.append(f"**Failed units:** {cap['failed_units'] or 'none'}")
    L.append("")
    L.append("## Root Cause (best-supported hypothesis)")
    L.append(verdict_summary or "_No hypothesis reached CONFIRMED — insufficient evidence._")
    L.append("")
    L.append("## Evidence")
    for r in results:
        tag = {"CONFIRMED": "✅", "PARTIAL": "⚠️", "REJECTED": "❌",
               "INSUFFICIENT_EVIDENCE": "❓"}.get(r["verdict"], "?")
        L.append(f"### {tag} {r['verdict']} — {r['statement']}")
        L.append(f"*layer: {r['layer']} | source: {r['source']}*")
        for c in r["confirming"]:
            L.append(f"- CONFIRMING: {c}")
        for c in r["disconfirming"]:
            L.append(f"- DISCONFIRMING: {c}")
        if not r["confirming"] and not r["disconfirming"]:
            L.append("- (no matching evidence in window)")
        if r.get("citations"):
            L.append(f"- past citations: {', '.join(r['citations'][:2])}")
        L.append("")
    L.append("## Proposed Fix")
    L.append("_Human approval required before any change (skill safety rule)._")
    L.append("")
    L.append(f"*Captured artifact: {art} ({len(cap['lines'])} lines) — kept for re-analysis.*")
    return "\n".join(L)

def load_state():
    if os.path.exists(STATE):
        try:
            return json.load(open(STATE))
        except Exception:
            pass
    return {"investigations": []}

def main():
    # trigger: THIS run's anomaly alerts (anomaly_alerts.json, written by
    # anomaly_watch in the same pipeline pass). The old path read
    # delta_report.json — which delta_run writes only at the END of the
    # pipeline — so every investigation ran one run late, against a stale
    # alert and a mismatched evidence window.
    ALERTS_FILE = os.path.join(D, "anomaly_alerts.json")
    if not os.path.exists(ALERTS_FILE):
        print(json.dumps({"ran": False, "reason": "no anomaly_alerts.json"}))
        return 0
    try:
        an = json.load(open(ALERTS_FILE))
    except Exception:
        print(json.dumps({"ran": False, "reason": "unreadable anomaly_alerts.json"}))
        return 0
    alerts = an.get("alerts", [])
    if not alerts:
        print(json.dumps({"ran": False, "reason": "no alerts in this run's scan"}))
        return 0

    st = load_state()
    done_sigs = {i.get("signature") for i in st["investigations"]
                 if i.get("verdict")}          # resolved — never re-investigate
    # unresolved (no-verdict) alerts MAY be re-investigated with fixed rules —
    # same principle as module 18 belief revision: a botched investigation
    # (e.g. wrong artifact window, pre-fix) must not be frozen forever.
    # Cap at 2 attempts per signature to prevent infinite re-tries.
    from collections import Counter
    attempts = Counter(i.get("signature") for i in st["investigations"])

    ran, critical_found, reports = False, False, []
    for alert in alerts:
        sev = alert.get("severity", "")
        rule = alert.get("rule", "")
        sig = f"{rule}:{alert.get('detail','')[:100]}"
        if sev not in ("CRITICAL", "HIGH") and rule != "NEW_PATTERN":
            continue  # only rule-tabled alerts are auto-investigable
        if sig in done_sigs:
            continue  # already resolved — no re-investigation spam
        if attempts[sig] >= 2:
            continue  # unresolved but already tried twice — needs a human
        ran = True

        cap = capture(alerts, 0, alert=alert)
        cands = hypotheses_for(alert, cap)
        results = [test(h, cap["lines"], cap) for h in cands]

        confirmed = [r for r in results if r["verdict"] == "CONFIRMED"]
        partial = [r for r in results if r["verdict"] == "PARTIAL"]
        if confirmed:
            best = confirmed[0]
            summary = (f"**{best['statement']}** (layer {best['layer']}, "
                       f"source {best['source']}) — supported by {len(best['confirming'])} "
                       f"cited evidence lines, no disconfirming evidence found.")
        elif partial:
            best = partial[0]
            summary = (f"**{best['statement']}** — PARTIAL: supporting evidence exists "
                       f"but contradicted by {len(best['disconfirming'])} line(s); "
                       f"scope narrowed, needs human eyes.")
        else:
            summary = None

        os.makedirs(INVDIR, exist_ok=True)
        seq = sum(1 for i in st["investigations"]
                  if i.get("ts", "").startswith(cap["ts"][:16]))  # unique per minute
        fname = time.strftime("inv_%Y%m%d_%H%M%S") + f"_{rule.lower()}_{seq}.md"
        fpath = os.path.join(INVDIR, fname)
        with open(fpath, "w") as f:
            f.write(render(alert, cap, results, summary))
        reports.append(fname)

        st["investigations"].append({
            "ts": cap["ts"], "rule": rule, "severity": sev, "signature": sig,
            "verdict": summary is not None, "best": (summary or "")[:200],
            "report": fname,
            # keep the alert + key evidence on the entry itself so later
            # belief revision can re-verify WITHOUT the report file
            "alert_detail": alert.get("detail", "")[:300],
            "evidence_inline": [c for r in results if r["verdict"] == "CONFIRMED"
                                for c in r["confirming"][:2]][:5],
        })
        if sev == "CRITICAL" and summary:
            critical_found = True

    # cap history at 100 entries
    st["investigations"] = st["investigations"][-100:]
    json.dump(st, open(STATE, "w"), indent=1)

    # learn: append confirmed/partial outcomes to the experience store so
    # evoskill + recall pick them up next run (cross-session learning loop)
    if ran:
        try:
            with open(EXP, "a") as f:
                for i in st["investigations"][-len(reports):]:
                    f.write(json.dumps({
                        "type": "root_cause", "ts": i["ts"],
                        "symptoms": [i["rule"], i["severity"]],
                        # alert line embedded INLINE — the entry stays
                        # verifiable even if the report file is pruned
                        # (lesson from 2026-09-22: 25/43 entries had
                        # dangling citations after report cleanup)
                        "alert_detail": (i.get("alert_detail") or "")[:300],
                        "evidence_inline": (i.get("evidence_inline") or [])[:5],
                        "root_cause": i["best"] or "insufficient evidence",
                        "fix": "none — human approval gate",
                        "outcome": "confirmed" if i["verdict"] else "insufficient-evidence",
                        "report": os.path.join("investigations", i["report"]),
                    }, ensure_ascii=False) + "\n")
        except Exception:
            pass

    out = {"ran": ran, "reports": reports,
           "critical_root_cause": critical_found,
           "total_investigations": len(st["investigations"])}
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 2 if critical_found else 0

if __name__ == "__main__":
    sys.exit(main())
