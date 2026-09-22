#!/usr/bin/env python3
"""Healer Audit — hermes-delta module 23: action justification audit.

Module 17 measures whether healer actions SUCCEEDED (executed vs rolled
back). Module 20 audits the predictor. Module 22 audits the investigator.
The healer itself — the component that ACTS on the system — was the last
un-audited piece: nobody ever checked whether an EXECUTED action SHOULD
have been taken. Success != justification.

Production 2026-09-22: 5 MARK_DESKTOP_NOISE actions executed on heal-test
service failures — real failures silenced as desktop noise because they ran
on a premise module 18 later retracted. Module 17 counted them as SUCCESS
(action ran, verify passed). Nobody flagged the premise.

Join discipline (bug 13 class — the healer action ts is NOT the
investigation ts): a healer entry references its investigation by REPORT
FILENAME (entry["investigation"]); inv_state entries carry the same
filename in entry["report"]. Join on that, then reach module 22's scored
state via the investigation's own "{ts}|{signature}" key.

Substance over wording (bug 11 lesson): a desktop-noise verdict scored
FALSE_POSITIVE under the OLD semantics (recurrence vs a "transient"
claim) is a WORDING overclaim, not a classification error — the action
that silenced benign recurring chatter was substantively correct. The
audit flags an action as unjustified only on SUBSTANCE: the silenced
line carries a real failure signature (REAL_FAIL_RX), or the premise was
retracted by belief revision. Never un-silence on a wording overclaim.

Audit rules, in priority order:
  1. EXCLUDED_SELFTEST — action targeted a heal-test* unit (sandbox
     instrumentation; the incident is already handled by modules 18/22
     and the bug-12 known-noise migration — do not double-count it here)
  2. UNJUSTIFIED_RETRACTED_PREMISE — the action's investigation report
     is in module 18's retracted set (the premise is now known wrong)
  3. UNJUSTIFIED_MISDIAGNOSED_PREMISE — substance check: the silenced
     line carries a real failure signature (a genuine service failure
     was marked noise); module 22's FALSE_POSITIVE on the same
     investigation, when present, corroborates
  4. JUSTIFIED — module 22 scored the same investigation TRUE_*
     (cross-validated), or no failure signature was silenced and no
     audit contradicts
  5. PENDING — action younger than the 90-min audit window (re-checked
     next run; never scored on missing evidence)

Guarded remediation (this module's own scope only):
  - UNJUSTIFIED MARK_DESKTOP_NOISE whose signature matches a key in the
    anomaly known-noise list gets UNSILENCED (key removed, pattern can
    alert again) — the rollback edge for suppression actions; a
    suppression without an un-suppress path is a one-way door. A missed
    match is recorded as unsilence_missed (visible, never silent).
  - Other unjustified actions are report-only (human gate).

Outcomes append to the experience store as type=healer_audit_outcome with
patterns_found=["healer_audit-<verdict>-<action>"] (evoskill materializes
a skill when an unjustified action class recurs) AND symptoms+root_cause
(recall surfaces "this action class was audited unjustified" with
citations).

Calibration per action id; >=3 unjustified of one action id = MISFIRING ->
recommendation to demote it from the SAFE allowlist (human gate — this
module never edits auto_healer.py).

State: scripts/healer_audit_state.json
Exit codes: 0 ok, 2 = unjustified actions detected (see report)
"""
import json, os, re, sys, time

D = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(D, "healer_state.jsonl")
STATE = os.path.join(D, "healer_audit_state.json")
INV_STATE = os.path.join(D, "inv_state.json")
IA_STATE = os.path.join(D, "investigator_audit_state.json")
ANOM_STATE = os.path.join(D, "anomaly_state.json")
EXP = os.path.expanduser("~/.hermes/skills/agent-self-learning/experience.jsonl")

SELFTEST_RX = re.compile(r"heal-test", re.I)
MISFIRING_MIN = 3
AUDIT_DELAY_MIN = 90

# real systemd failure signatures (same set as module 22 — duplicated
# deliberately: the audit must not import live pipeline code, a bug in
# module 22's import graph must not take down module 23)
REAL_FAIL_RX = re.compile(
    r"failed with result|main process exited|failed to start|"
    r"coredump|segfault", re.I)


def load_jsonl(p):
    out = []
    if os.path.exists(p):
        for ln in open(p, errors="replace"):
            ln = ln.strip()
            if ln:
                try:
                    out.append(json.loads(ln))
                except Exception:
                    pass
    return out


def load_json(p, default):
    if os.path.exists(p):
        try:
            return json.load(open(p))
        except Exception:
            return default
    return default


def main():
    t0 = time.time()
    entries = load_jsonl(LOG)
    st = load_json(STATE, {"audited": {}})
    st.setdefault("audited", {})

    # ---- premise cross-references (joined by REPORT FILENAME) ----------
    # investigation report basename -> (ts, signature) for module 22 lookup
    inv_by_report = {}
    for inv in load_json(INV_STATE, {}).get("investigations", []):
        rep = os.path.basename(inv.get("report") or "")
        if rep:
            inv_by_report[rep] = (inv.get("ts", ""), inv.get("signature") or "")

    # module 22 verdict audits, keyed "{inv_ts}|{inv_sig[:100]}"
    scored = load_json(IA_STATE, {}).get("scored", {})

    # module 18 retractions: report filenames of retracted beliefs
    retracted_reports = set()
    for e in load_jsonl(EXP):
        if e.get("outcome") == "retracted":
            rep = os.path.basename(e.get("report") or "")
            if rep:
                retracted_reports.add(rep)

    # anomaly known-noise keys (for un-silencing)
    anom = load_json(ANOM_STATE, {})
    known = anom.get("known", {})

    audited_new, unjustified, unsilenced, unsilence_missed = [], [], [], []
    for e in entries:
        key = f"{e.get('ts')}|{(e.get('signature') or '')[:100]}"
        if key in st["audited"]:
            continue  # idempotent — never re-audit the same action
        ts, sig, action = e.get("ts", ""), e.get("signature", ""), e.get("action", "")
        rep = os.path.basename(e.get("investigation") or "")
        inv_ts, inv_sig = inv_by_report.get(rep, ("", ""))
        audit = scored.get(f"{inv_ts}|{inv_sig[:100]}") if inv_ts else None
        real_fail = bool(REAL_FAIL_RX.search(sig))

        # rule 1: self-test instrumentation — excluded from production
        # audit (the incident is owned by modules 18/22 + bug-12 cleanup)
        if SELFTEST_RX.search(sig) or SELFTEST_RX.search(e.get("root_cause") or ""):
            verdict = "EXCLUDED_SELFTEST"
            why = ("action targeted a sandbox test unit (heal-test*) — not "
                   "production signal; premise retraction handled by module 18")
        # rule 2: premise retracted by belief revision
        elif rep and rep in retracted_reports:
            verdict = "UNJUSTIFIED_RETRACTED_PREMISE"
            why = ("the premise (investigation verdict) this action ran on "
                   f"was later retracted by belief revision ({rep})")
        # rule 3: substance — a genuine failure was silenced as noise
        elif real_fail:
            verdict = "UNJUSTIFIED_MISDIAGNOSED_PREMISE"
            why = ("the silenced line carries a real failure signature — "
                   "a genuine service failure was marked desktop noise"
                   + (" (corroborated by investigator-audit FALSE_POSITIVE)"
                      if audit and audit.get("score") == "FALSE_POSITIVE" else ""))
        # rule 4: cross-validated by a TRUE_* verdict audit
        elif audit and str(audit.get("score", "")).startswith("TRUE"):
            verdict = "JUSTIFIED"
            why = f"underlying verdict audited {audit.get('score')} by module 22"
        elif audit and audit.get("score") == "FALSE_POSITIVE":
            # legacy wording overclaim (bug 11): classification was right,
            # the statement overclaimed "transient" — action substantively
            # correct, do NOT un-silence on a wording overclaim
            verdict = "JUSTIFIED"
            why = ("verdict scored FALSE_POSITIVE under legacy transient-"
                   "semantics (wording overclaim, bug 11) — no failure "
                   "signature on the silenced line, action substantively correct")
        else:
            # rule 5: too young to have a verdict audit yet?
            try:
                age_min = (time.time() - time.mktime(time.strptime(
                    ts, "%Y-%m-%dT%H:%M:%S"))) / 60
            except Exception:
                age_min = 1e9
            if age_min < AUDIT_DELAY_MIN:
                verdict = "PENDING"
                why = (f"action is {int(age_min)}min old — verdict audit "
                       f"window ({AUDIT_DELAY_MIN}min) not elapsed yet")
            else:
                verdict = "JUSTIFIED"
                why = ("no failure signature on the silenced line and no "
                       "contradicting verdict audit")

        rec = {"ts": ts, "action": action, "verdict": verdict, "why": why,
               "signature": (sig or "")[:120], "report": rep}
        st["audited"][key] = rec
        audited_new.append(rec)

        if verdict.startswith("UNJUSTIFIED"):
            unjustified.append(rec)
            # guarded remediation: un-silence wrongly-suppressed patterns
            if action == "MARK_DESKTOP_NOISE":
                msg = re.sub(r"^[A-Z_]+:", "", sig)  # strip rule prefix
                unit, _, body = msg.partition(":")
                unit, body = unit.strip(), body.strip()
                hit = [k for k in known if unit and unit in k
                       and body[:25] and body[:25] in k]
                if hit:
                    for k in hit:
                        del known[k]
                        unsilenced.append(k)
                    rec["unsilenced"] = True
                else:
                    unsilence_missed.append(key)
                    rec["unsilence_missed"] = True

    # persist un-silencing back to anomaly state (guarded: only if we
    # actually removed keys)
    if unsilenced:
        anom["known"] = known
        anom.setdefault("migration", []).append({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "bug": "healer-audit-unsilence",
            "removed_known_keys": unsilenced,
        })
        json.dump(anom, open(ANOM_STATE, "w"), indent=1)

    # experience-store feedback (dedup via audited keys above)
    for rec in unjustified:
        with open(EXP, "a") as f:
            f.write(json.dumps({
                "type": "healer_audit_outcome",
                "ts": rec["ts"],
                "patterns_found": [f"healer_audit-{rec['verdict'].lower()}-{rec['action'].lower()}"],
                "symptoms": f"healer action {rec['action']} executed then audited",
                "root_cause": f"{rec['verdict']}: {rec['why']} (signature: {rec['signature'][:80]})",
                "fix": "un-silenced the suppressed pattern" if rec.get("unsilenced")
                       else ("un-silence MISS — pattern not found in known-noise list"
                             if rec.get("unsilence_missed")
                             else "report-only — human review needed"),
            }, ensure_ascii=False) + "\n")

    # calibration per action id
    by_action = {}
    for v in st["audited"].values():
        by_action.setdefault(v.get("action", "?"), []).append(v.get("verdict", "?"))
    calibration, misfiring = [], []
    for action, verdicts in sorted(by_action.items()):
        unj = sum(1 for v in verdicts if v.startswith("UNJUSTIFIED"))
        just = sum(1 for v in verdicts if v == "JUSTIFIED")
        calibration.append({"action": action, "audited": len(verdicts),
                            "justified": just, "unjustified": unj,
                            "pending": verdicts.count("PENDING"),
                            "excluded": verdicts.count("EXCLUDED_SELFTEST")})
        if unj >= MISFIRING_MIN:
            misfiring.append({"action": action, "unjustified": unj,
                              "recommendation": "demote from SAFE allowlist (needs human approval)"})

    json.dump(st, open(STATE, "w"), indent=1)
    out = {"audited_this_run": len(audited_new),
           "audited_total": len(st["audited"]),
           "unjustified": unjustified, "unsilenced": unsilenced,
           "unsilence_missed": unsilence_missed,
           "calibration": calibration, "misfiring": misfiring,
           "duration_s": round(time.time() - t0, 2)}
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 2 if unjustified else 0


if __name__ == "__main__":
    sys.exit(main())
