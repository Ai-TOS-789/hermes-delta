#!/usr/bin/env python3
"""Delta Run — hermes-delta module 9: unified pipeline runner.
One shot: sample+predict (6) -> evoskill discovery (7) -> knowledge graph (8).
Auto-escalates: if P(failure) > 0.7, captures artifacts for investigation.
Exit codes: 0 ok, 2 = anomaly detected (auto-investigate flag)."""
import json, os, subprocess, sys, time

D = os.path.dirname(os.path.abspath(__file__))
REPORT = os.path.join(D, "delta_report.json")

def run(script):
    r = subprocess.run([sys.executable, os.path.join(D, script)],
                       capture_output=True, text=True, timeout=120)
    try:
        return json.loads(r.stdout), r.returncode
    except Exception:
        return {"error": r.stderr.strip()[:400]}, r.returncode

def main():
    t0 = time.time()
    pf, pf_rc = run("predict_failure.py")
    ev, _ = run("evoskill.py")
    sd, sd_rc = run("skill_doctor.py")
    rc, _ = run("root_cause_recall.py")  # module 11: rebuild symptom index
    fleet_data, fleet_rc = run("fleet_sentinel.py")  # module 12: fleet device health
    ax, _ = run("arxiv_scanner.py")  # module 13: literature radar
    an, an_rc = run("anomaly_watch.py")  # module 14: journal anomaly scan
    ai, ai_rc = run("auto_investigator.py")  # module 15: auto-investigate alerts
    ah, ah_rc = run("auto_healer.py")  # module 16: guarded auto-remediation
    hf, hf_rc = run("healer_feedback.py")  # module 17: heal->learn feedback loop
    br, br_rc = run("belief_revision.py")  # module 18: retract beliefs learned wrong
    pa, pa_rc = run("prediction_audit.py")  # module 20: score past forecasts vs truth
    ia, ia_rc = run("investigator_audit.py")  # module 22: score past verdicts vs truth
    # module 23 runs AFTER 18+22 so it audits against THIS run's retractions
    # and verdict scores (running it earlier = 1-run-stale premises, same
    # lag class as the module-15 alert-source bug)
    ha, ha_rc = run("healer_audit.py")  # module 23: audit executed actions' justification
    family_data, family_rc = run("family_lineage.py")  # module 19: autonomous family lineage
    # re-run AFTER belief revision so the recall index is rebuilt without
    # retracted entries (quarantine), and the KG reflects revised beliefs
    rc, _ = run("root_cause_recall.py")
    kg, _ = run("build_kg.py")
    preds = pf.get("predictions", []) if isinstance(pf, dict) else []
    anomaly = pf.get("auto_investigate", False) if isinstance(pf, dict) else False
    # BUG 15 (2026-09-22): the exit code below used ONLY the predictor's
    # flag while DISK_IO HIGH alerts were live (an_rc == 2) and the
    # investigator had confirmed a CRITICAL root cause (ai_rc == 2) — the
    # run exited 0 and the cron digest read "nothing happened" while a disk
    # was failing. Exit 2 must reflect ANY component that demands attention.
    anomaly = bool(anomaly or an_rc == 2 or ai_rc == 2)
    report = {
        "run_ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "duration_s": round(time.time() - t0, 1),
        "predict": {"samples": pf.get("n_samples") if isinstance(pf, dict) else None,
                    "predictions": preds, "anomaly": anomaly},
        "evoskill": {"materialized_total": ev.get("materialized_total") if isinstance(ev, dict) else None,
                     "decisions": [d for d in (ev.get("decisions", []) if isinstance(ev, dict) else [])
                                   if isinstance(d, dict) and d.get("decision") != "NOT_RETAINED"]},
        "kg": {"nodes": kg.get("nodes") if isinstance(kg, dict) else None,
               "edges": kg.get("edges") if isinstance(kg, dict) else None,
               "svg": kg.get("svg") if isinstance(kg, dict) else None},
        "skill_doctor": {"defective_skills": sd.get("defective_skills") if isinstance(sd, dict) else None,
                         "repairs": sd.get("repairs", []) if isinstance(sd, dict) else [],
                         "unrepairable": sd.get("unrepairable", []) if isinstance(sd, dict) else [],
                         "consolidations_proposed": sd.get("consolidations", []) if isinstance(sd, dict) else []},
        "recall_index_tokens": rc.get("tokens") if isinstance(rc, dict) else None,
        "arxiv": {"new_papers": ax.get("new_papers") if isinstance(ax, dict) else None,
                  "total_seen": ax.get("total_seen") if isinstance(ax, dict) else None,
                  "evidence": ax.get("evidence", [])[:5] if isinstance(ax, dict) else []},
        "fleet": {"rounds": fleet_data.get("rounds") if isinstance(fleet_data, dict) else None,
                  "alerts": fleet_data.get("alerts", []) if isinstance(fleet_data, dict) else [],
                  "evidence": fleet_data.get("evidence", []) if isinstance(fleet_data, dict) else []},
        "anomaly": {"window_min": an.get("window_min") if isinstance(an, dict) else None,
                    "lines_scanned": an.get("lines_scanned") if isinstance(an, dict) else None,
                    "error_rate_per_min": an.get("error_rate_per_min") if isinstance(an, dict) else None,
                    "known_patterns": an.get("known_patterns") if isinstance(an, dict) else None,
                    "alerts": an.get("alerts", [])[:8] if isinstance(an, dict) else []},
        "auto_investigator": {"ran": ai.get("ran") if isinstance(ai, dict) else None,
                              "reports": ai.get("reports", []) if isinstance(ai, dict) else [],
                              "critical_root_cause": ai.get("critical_root_cause") if isinstance(ai, dict) else None,
                              "total_investigations": ai.get("total_investigations") if isinstance(ai, dict) else None},
        "auto_healer": {"actions_attempted": ah.get("actions_attempted") if isinstance(ah, dict) else None,
                        "executed": ah.get("executed", []) if isinstance(ah, dict) else [],
                        "rolled_back": ah.get("rolled_back", []) if isinstance(ah, dict) else [],
                        "escalated": ah.get("escalated", []) if isinstance(ah, dict) else []},
        "healer_feedback": {"log_entries": hf.get("log_entries") if isinstance(hf, dict) else None,
                            "new_feedback": hf.get("new_feedback") if isinstance(hf, dict) else None,
                            "already_processed": hf.get("already_processed") if isinstance(hf, dict) else None,
                            "actions": hf.get("actions", []) if isinstance(hf, dict) else [],
                            "unreliable": hf.get("unreliable", []) if isinstance(hf, dict) else []},
        "healer_audit": {"audited_this_run": ha.get("audited_this_run") if isinstance(ha, dict) else None,
                         "audited_total": ha.get("audited_total") if isinstance(ha, dict) else None,
                         "unjustified": ha.get("unjustified", []) if isinstance(ha, dict) else [],
                         "unsilenced": ha.get("unsilenced", []) if isinstance(ha, dict) else [],
                         "calibration": ha.get("calibration", []) if isinstance(ha, dict) else [],
                         "misfiring": ha.get("misfiring", []) if isinstance(ha, dict) else []},
        "belief_revision": {"retracted": br.get("retracted", []) if isinstance(br, dict) else [],
                            "held": len(br.get("held", [])) if isinstance(br, dict) else 0,
                            "citation_rotted": br.get("citation_rotted", []) if isinstance(br, dict) else []},
        "prediction_audit": {"scored_this_run": pa.get("scored_this_run") if isinstance(pa, dict) else None,
                             "calibration": pa.get("calibration", []) if isinstance(pa, dict) else [],
                             "over_firing": pa.get("over_firing", []) if isinstance(pa, dict) else []},
        "investigator_audit": {"scored_this_run": ia.get("scored_this_run") if isinstance(ia, dict) else None,
                               "calibration": ia.get("calibration", []) if isinstance(ia, dict) else [],
                               "misdiagnosing": ia.get("misdiagnosing", []) if isinstance(ia, dict) else []},
        "family": family_data if isinstance(family_data, dict) else {},
    }
    if anomaly:
        report["escalation"] = "P>0.7 — artifacts captured, system-investigation protocol should run"
    if fleet_rc == 2:
        report["escalation"] = (report.get("escalation", "") +
                                " fleet_sentinel: CRITICAL/HIGH device alert — see fleet_state.jsonl").strip()
    if an_rc == 2:
        report["escalation"] = (report.get("escalation", "") +
                                " anomaly_watch: CRITICAL/HIGH journal alert — see anomaly_state.json / artifacts/").strip()
    if ai_rc == 2:
        report["escalation"] = (report.get("escalation", "") +
                                " auto_investigator: CRITICAL root cause found — see investigations/").strip()
    if ah_rc == 3:
        report["escalation"] = (report.get("escalation", "") +
                                " auto_healer: ROLLBACK occurred — healing failed, human review needed").strip()
    if hf_rc == 2:
        report["escalation"] = (report.get("escalation", "") +
                                " healer_feedback: UNRELIABLE action detected — playbook review needed").strip()
    if ha_rc == 2:
        report["escalation"] = (report.get("escalation", "") +
                                " healer_audit: UNJUSTIFIED actions detected — premises were wrong, see healer_audit.unjustified").strip()
    if br_rc == 2:
        report["escalation"] = (report.get("escalation", "") +
                                " belief_revision: beliefs retracted — past learning was wrong, see belief_revision_state.json").strip()
    if pa_rc == 2:
        report["escalation"] = (report.get("escalation", "") +
                                " prediction_audit: OVER_FIRING forecaster detected — calibration broken, weight change needs human approval").strip()
    if ia_rc == 2:
        report["escalation"] = (report.get("escalation", "") +
                                " investigator_audit: MISDIAGNOSING verdict class detected — seed fix needs human approval (see investigator_audit.calibration)").strip()
    if sd_rc == 3:
        report["escalation"] = (report.get("escalation", "") +
                                " skill_doctor: unrepairable defects found — see skill_doctor_state.json").strip()
    with open(REPORT, "w") as f:
        json.dump(report, f, indent=2)
    # append to history so evoskill can mine REAL recurring outcomes across runs
    try:
        with open(os.path.join(D, "delta_report_history.jsonl"), "a") as f:
            f.write(json.dumps(report) + "\n")
    except Exception:
        pass
    # module 21 runs AFTER the history append so it diffs THIS run vs the
    # previous one (running it before the append made it compare the two
    # OLDEST-pending runs — same lag-1 bug class as module 15's original
    # alert source, caught by testing 2026-09-22)
    dc, dc_rc = run("delta_compare.py")
    if dc_rc == 2:
        report["delta_compare"] = {"drifts": dc.get("drifts", []) if isinstance(dc, dict) else [],
                                   "evidence": dc.get("evidence", []) if isinstance(dc, dict) else []}
        report["escalation"] = (report.get("escalation", "") +
                                " delta_compare: behavioral drift between runs — see delta_compare.evidence in this report").strip()
        with open(REPORT, "w") as f:
            json.dump(report, f, indent=2)
        try:
            with open(os.path.join(D, "delta_report_history.jsonl")) as f:
                hist = f.readlines()
            hist[-1] = json.dumps(report) + "\n"
            with open(os.path.join(D, "delta_report_history.jsonl"), "w") as f:
                f.writelines(hist)
        except Exception:
            pass
    print(json.dumps(report, indent=2))
    return 2 if anomaly else 0

if __name__ == "__main__":
    raise SystemExit(main())
