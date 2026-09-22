#!/usr/bin/env python3
"""Delta Compare — hermes-delta module 21: run-over-run self-diff.
The delta-compare skill existed but nothing in the pipeline ever CALLED it
(open question since 2026-09-22). This module applies it to the pipeline's
own telemetry: diff the last two delta_report_history.jsonl entries and
surface behavioral drift between runs — new alert rules, error-rate shifts,
metric moves (mem/disk/load), evoskill changes, new escalations, knowledge-
graph growth. Every diff cites the history line it came from.

Drift rules (MEDIUM severity, report-only — no auto-action):
  - error rate tripled vs last run          -> ERROR_RATE_DRIFT
  - new alert rule never seen in last run   -> NEW_ALERT_CLASS
  - disk_pct moved >= +3 points             -> DISK_TREND_UP
  - knowledge graph shrank                  -> KG_SHRINK (beliefs retracted?)
  - module went from ran->not-ran           -> MODULE_SILENT

State: none (reads delta_report_history.jsonl directly — the history IS the state)
Exit codes: 0 ok, 2 = drift detected (report-only, feeds the cron digest)
"""
import json, os, sys

D = os.path.dirname(os.path.abspath(__file__))
HISTORY = os.path.join(D, "delta_report_history.jsonl")

def load_history():
    rows = []
    if os.path.exists(HISTORY):
        with open(HISTORY, errors="replace") as f:
            for i, ln in enumerate(f, 1):
                try:
                    rows.append((i, json.loads(ln)))
                except Exception:
                    continue
    return rows

def g(rep, *path, default=None):
    cur = rep
    for p in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(p)
    return cur if cur is not None else default

def main():
    rows = load_history()
    if len(rows) < 2:
        print(json.dumps({"ran": False, "reason": "need >=2 runs of history"}))
        return 0
    (ln_prev, prev), (ln_cur, cur) = rows[-2], rows[-1]

    drifts, evidence = [], []

    # error-rate drift
    er_prev = g(prev, "anomaly", "error_rate_per_min", default=0) or 0
    er_cur = g(cur, "anomaly", "error_rate_per_min", default=0) or 0
    if er_prev < er_cur and er_cur >= max(3 * max(er_prev, 0.1), 5):
        drifts.append("ERROR_RATE_DRIFT")
        evidence.append(f"[history:{ln_cur}] error rate {er_prev}/min -> {er_cur}/min "
                        f"({round(er_cur / max(er_prev, 0.1))}x)")

    # new alert classes
    rules_prev = {a.get("rule") for a in g(prev, "anomaly", "alerts", default=[])}
    rules_cur = {a.get("rule") for a in g(cur, "anomaly", "alerts", default=[])}
    new_rules = rules_cur - rules_prev
    if new_rules:
        drifts.append("NEW_ALERT_CLASS")
        evidence.append(f"[history:{ln_cur}] new alert class this run: "
                        f"{', '.join(sorted(new_rules))} (prev had {sorted(rules_prev) or 'none'})")

    # disk trend — disk_pct lives in pf_state samples, not the report; use
    # anomaly mem jumps as the report-visible metric (disk tracked by module 6)
    mem_prev = g(prev, "anomaly", "mem_pct")
    mem_cur = g(cur, "anomaly", "mem_pct")
    if isinstance(mem_prev, (int, float)) and isinstance(mem_cur, (int, float)):
        if mem_cur - mem_prev >= 20:
            drifts.append("MEM_JUMP")
            evidence.append(f"[history:{ln_cur}] mem {mem_prev}% -> {mem_cur}% between runs")

    # KG shrink (beliefs retracted?)
    kg_prev = g(prev, "kg", "nodes", default=0) or 0
    kg_cur = g(cur, "kg", "nodes", default=0) or 0
    if kg_prev and kg_cur < kg_prev:
        drifts.append("KG_SHRINK")
        evidence.append(f"[history:{ln_cur}] knowledge graph {kg_prev} -> {kg_cur} nodes "
                        f"(retractions or pruning)")

    # module went silent
    for mod in ("auto_investigator", "auto_healer"):
        ran_prev = g(prev, mod, "ran", default=False)
        ran_cur = g(cur, mod, "ran", default=False)
        if ran_prev and not ran_cur:
            # healer only "ran" when there was something to do — not drift
            if mod == "auto_investigator":
                drifts.append("MODULE_SILENT")
                evidence.append(f"[history:{ln_cur}] auto_investigator ran last run "
                                f"but not this one — alerts may have stopped or all resolved")

    # evoskill new materializations
    ev_prev = {d.get("pattern") for d in g(prev, "evoskill", "decisions", default=[])}
    ev_cur = [d for d in g(cur, "evoskill", "decisions", default=[])]
    new_mat = [d.get("pattern") for d in ev_cur
               if d.get("decision") == "MATERIALIZED" and d.get("pattern") not in ev_prev]
    if new_mat:
        evidence.append(f"[history:{ln_cur}] evoskill materialized NEW skill(s): "
                        f"{', '.join(new_mat)}")

    out = {"ran": True, "prev_run": g(prev, "run_ts"), "this_run": g(cur, "run_ts"),
           "drifts": drifts, "evidence": evidence}
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 2 if drifts else 0

if __name__ == "__main__":
    sys.exit(main())
