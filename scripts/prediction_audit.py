#!/usr/bin/env python3
"""Prediction Audit — hermes-delta module 20: forecast verification.
The pipeline PREDICTS (module 6) but nothing ever checked whether the
predictions came TRUE. A forecaster that is never scored keeps forecasting
with the same broken weights forever. This module closes that loop:

  For each past prediction whose ETA window has now ELAPSED:
    - score HIT if the predicted failure signature appeared in the journal
      within ETA (or the smoothed metric actually reached the threshold),
    - score MISS otherwise,
    - score FALSE_ALARM if it fired but nothing happened.

  Outcomes append to the agent-self-learning experience store (type
  prediction_outcome) so evoskill materializes a skill when a failure mode
  of the FORECASTER recurs (e.g. "error-burst risk over-fires on desktop
  chatter") — the system learns about its own forecasting blind spots.

  Calibration report: per failure-type hit rate over all history; a type
  with >=3 scored outcomes and hit rate < 40% is flagged OVER_FIRING and
  the recommended weight adjustment is reported (human gate — the module
  never edits predict_failure.py itself).

State: scripts/prediction_audit_state.json (scored prediction ids)
Exit codes: 0 ok, 2 = OVER_FIRING forecaster detected (calibration broken)
"""
import json, os, re, subprocess, time, sys

D = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(D, "prediction_audit_state.json")
HISTORY = os.path.join(D, "delta_report_history.jsonl")
EXP = os.path.expanduser("~/.hermes/skills/agent-self-learning/experience.jsonl")

# failure type -> journal signature that would prove it TRUE
TRUTH_SIGNATURES = {
    "OOM Kill": r"invoked oom-killer|oom-kill|Out of memory: Killed process",
    "Disk Full": r"No space left on device|Read-only file system",
    "Error-burst crash risk": r"segfault|coredump|general protection fault|"
                              r"Kernel panic|watchdog: BUG",
}

MIN_SCORED_FOR_CALIBRATION = 3
BAD_CALIBRATION_THRESHOLD = 0.4  # hit rate below this = OVER_FIRING

# Calibration epoch (bug 16, 2026-09-22): the error-burst rule was re-weighted
# at 19:25 (>=20 AND ratio>=2.0, replay: 0 false alarms over 50 samples). An
# audit that averages over ALL history never clears: the 9 pre-fix FALSE_ALARMs
# keep the hit rate at 0.0 forever and every delta_run re-escalates a defect
# that no longer exists (run 26 fired OVER_FIRING while the fixed forecaster
# produced 0 predictions). Calibration must judge the CURRENT forecaster —
# only predictions made AFTER the epoch count toward OVER_FIRING. Pre-epoch
# scores stay in state (audit trail) and in a legacy bucket for reference.
CALIBRATION_EPOCH = "2026-09-22T19:25:00"

def journal_window(since_iso, until_iso):
    try:
        r = subprocess.run(["journalctl", "--since", since_iso, "--until",
                            until_iso, "--no-pager", "-o", "short"],
                           capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            return r.stdout.splitlines()
    except Exception:
        pass
    return []

def iso(ts):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))

def main():
    now = time.time()
    st = {"scored": {}} if not os.path.exists(STATE) else \
        (json.load(open(STATE)) if os.path.exists(STATE) else {"scored": {}})
    st.setdefault("scored", {})

    scored_new, hits, misses, false_alarms = [], [], [], []
    if os.path.exists(HISTORY):
        with open(HISTORY) as f:
            for ln in f:
                try:
                    r = json.loads(ln)
                except Exception:
                    continue
                run_ts = r.get("run_ts", "")
                try:
                    run_epoch = time.mktime(time.strptime(run_ts, "%Y-%m-%dT%H:%M:%S"))
                except Exception:
                    continue
                for p in r.get("predict", {}).get("predictions", []):
                    pid = f"{run_ts}|{p.get('failure')}|{p.get('probability')}"
                    if pid in st["scored"]:
                        continue
                    eta = p.get("eta_minutes") or 15
                    deadline = run_epoch + eta * 60
                    if deadline > now:
                        continue  # ETA window not elapsed yet — cannot score
                    sig = TRUTH_SIGNATURES.get(p.get("failure", ""), None)
                    if not sig:
                        continue  # unknown failure type — needs a signature added
                    lines = journal_window(iso(run_epoch), iso(min(deadline, now)))
                    truth = any(re.search(sig, l, re.I) for l in lines)
                    score = "HIT" if truth else "MISS"
                    if not truth:
                        score = "FALSE_ALARM" if p.get("probability", 0) >= 0.7 else "MISS"
                    st["scored"][pid] = {"score": score, "failure": p.get("failure"),
                                         "prob": p.get("probability"), "run_ts": run_ts,
                                         "eta": eta, "lines_checked": len(lines)}
                    (hits if score == "HIT" else
                     false_alarms if score == "FALSE_ALARM" else misses).append(pid)
                    scored_new.append(st["scored"][pid])

    # calibration per failure type — CURRENT forecaster only (bug 16):
    # predictions made after CALIBRATION_EPOCH. Pre-epoch scores are kept in
    # a legacy bucket (audit trail, never deleted) but do not gate exit code.
    epoch = time.mktime(time.strptime(CALIBRATION_EPOCH, "%Y-%m-%dT%H:%M:%S"))
    by_type, legacy_by_type = {}, {}
    for v in st["scored"].values():
        try:
            v_epoch = time.mktime(time.strptime(v["run_ts"], "%Y-%m-%dT%H:%M:%S"))
        except Exception:
            v_epoch = 0.0
        bucket = by_type if v_epoch >= epoch else legacy_by_type
        bucket.setdefault(v["failure"], []).append(v["score"])
    calibration, over_firing = [], []
    for ftype, scores in sorted(by_type.items()):
        hit_rate = scores.count("HIT") / len(scores) if scores else 0.0
        legacy = legacy_by_type.get(ftype, [])
        calibration.append({"failure": ftype, "scored": len(scores),
                            "hits": scores.count("HIT"),
                            "misses": scores.count("MISS"),
                            "false_alarms": scores.count("FALSE_ALARM"),
                            "hit_rate": round(hit_rate, 2),
                            "legacy_pre_epoch": {"scored": len(legacy),
                                                 "false_alarms": legacy.count("FALSE_ALARM")}})
        if (len(scores) >= MIN_SCORED_FOR_CALIBRATION
                and hit_rate < BAD_CALIBRATION_THRESHOLD):
            over_firing.append({"failure": ftype, "hit_rate": round(hit_rate, 2),
                                "scored": len(scores),
                                "recommendation":
                                    f"lower pattern weight or raise threshold for "
                                    f"'{ftype}' — human approval required to edit "
                                    f"predict_failure.py"})

    # learn: outcomes -> experience store (evoskill + recall pick these up)
    if scored_new:
        try:
            with open(EXP, "a") as f:
                for s in scored_new:
                    f.write(json.dumps({
                        "type": "prediction_outcome", "ts": s["run_ts"],
                        "symptoms": ["prediction", s["failure"], s["score"].lower()],
                        "root_cause": (f"forecast '{s['failure']}' scored {s['score']} "
                                       f"(p={s['prob']}, eta={s['eta']}min, "
                                       f"{s['lines_checked']} journal lines checked)"),
                        "fix": "none — calibration report only, human gate on weights",
                        "outcome": s["score"].lower(),
                        "patterns_found": [f"prediction-{s['score'].lower()}-"
                                           f"{(s['failure'] or 'unknown').lower().replace(' ', '-')}"],
                    }, ensure_ascii=False) + "\n")
        except Exception:
            pass

    json.dump(st, open(STATE, "w"), indent=1)
    out = {"scored_this_run": len(scored_new), "hits": len(hits),
           "misses": len(misses), "false_alarms": len(false_alarms),
           "calibration": calibration, "over_firing": over_firing,
           "total_scored": len(st["scored"])}
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 2 if over_firing else 0

if __name__ == "__main__":
    sys.exit(main())
