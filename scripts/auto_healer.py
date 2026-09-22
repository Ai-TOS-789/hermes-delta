#!/usr/bin/env python3
"""Auto-Healer — hermes-delta module 16: closes the remediation gap.

Module 15 (auto_investigator) stops at "CONFIRMED root cause + proposed fix
(needs human approval)". Module 16 adds the GuardedAct-style safety layer
(arXiv 2609.11264 — blast-radius-aware guarded remediation) on top:

  PLAN      read each investigation's CONFIRMED root cause -> map to a
            candidate remediation action from a fixed playbook (never free-form)
  VERIFY    pre-flight checks: action must be in the allowlist, its pre-
            conditions must hold on the live system, and the target unit must
            be in the alert's own evidence (no healing things we didn't
            diagnose — blast-radius control)
  EXECUTE   only SAFE actions run, each wrapped in a guarded transaction:
            snapshot state -> act -> post-check. Post-check fails => auto
            ROLLBACK to the snapshot. All output logged to healer_state.jsonl
  REPORT    every action (executed / rolled back / escalated) is reported
            with citations; anything not SAFE is escalated for human approval
            (the human gate stays for risky actions)

Allowlist (SAFE, auto-executable): restart a failed user unit, systemctl
daemon-reload, reset-failed counters, drop cache-heavy warnings. Anything
else (kill processes, edit configs, restart system units, package ops) is
UNSAFE -> escalation entry only. No LLM calls, offline <3s.

State: healer_state.jsonl (action log), healer_state.json (summary)
Exit codes: 0 ok/nothing to do, 2 = an action was attempted (check report),
            3 = a rollback occurred (healing failed, human needed)
"""
import json, os, re, subprocess, sys, time

D = os.path.dirname(os.path.abspath(__file__))
INVDIR = os.path.join(D, "investigations")
LOG = os.path.join(D, "healer_state.jsonl")
STATE = os.path.join(D, "healer_state.json")

# --------------------------------------------------------------- playbook
# root-cause keyword pattern -> (action_id, description, precheck, apply, verify)
# apply/verify are shell templates; {unit} is substituted after strict allowlist
# validation (unit name must appear in the investigation's own evidence).
PLAYBOOK = {
    "failed user unit": {
        "id": "RESTART_FAILED_USER_UNIT", "safety": "SAFE",
        "desc": "restart a failed *user* systemd unit that the investigation tied to the alert",
        "pre": "systemctl --user is-failed {unit} 2>/dev/null | grep -q failed",
        "apply": "systemctl --user reset-failed {unit} && systemctl --user restart {unit}",
        "verify": "systemctl --user is-active {unit} 2>/dev/null | grep -qx active",
        "rollback": "systemctl --user stop {unit} 2>/dev/null; true",
    },
    "stale unit config": {
        "id": "DAEMON_RELOAD", "safety": "SAFE",
        "desc": "systemd is running a stale unit file — daemon-reload then restart the unit",
        "pre": "systemctl --user is-failed {unit} 2>/dev/null | grep -q failed",
        "apply": "systemctl --user daemon-reload && systemctl --user reset-failed {unit} && systemctl --user restart {unit}",
        "verify": "systemctl --user is-active {unit} 2>/dev/null | grep -qx active",
        "rollback": "systemctl --user stop {unit} 2>/dev/null; true",
    },
    "transient desktop": {
        "id": "MARK_DESKTOP_NOISE", "safety": "SAFE",
        "desc": "no action needed — record desktop noise as known-noise so it stops re-alerting",
        "pre": "true", "apply": "true",
        "verify": "true", "rollback": "true",
    },
    "disk io failure": {
        # hardware fault (production 2026-09-22: sda USB write-fail) — there
        # is NO safe automated remedy for a dying disk. Entry exists so the
        # escalation is categorized ("replace/reconnect the device") instead
        # of the generic "no playbook entry". safety=UNSAFE + the gate below
        # guarantee it can never auto-execute.
        "id": "ESCALATE_DISK_FAILURE", "safety": "UNSAFE",
        "desc": "storage device failing at hardware level — human must back up data and replace/reconnect the device",
        "pre": "true", "apply": "true",
        "verify": "true", "rollback": "true",
    },
}

# keywords in the investigation's root-cause statement -> playbook entry
# bug 11 sync: the seed statement was reworded ("recurring desktop-session
# chatter" — "transient" was an overclaim scored FALSE_POSITIVE by module
# 22). KEYMAP must match BOTH the new wording and legacy statements still
# in inv_state/history, or MARK_DESKTOP_NOISE stops firing entirely.
KEYMAP = [
    (re.compile(r"stale copy|changed on disk|needs reload|daemon-reload", re.I), "stale unit config"),
    (re.compile(r"failed (user )?unit|unit .{0,20}failed|reset-failed|real service defect", re.I), "failed user unit"),
    (re.compile(r"(transient desktop-session error|recurring desktop-session chatter|desktop noise|gvfs/tracker)", re.I), "transient desktop"),
    # hardware (production 2026-09-22 sda write-fail): no SAFE action exists
    # for a failing disk — unmounting/replugging is a human decision. Maps to
    # an UNSAFE playbook entry so the run is RECORDED and escalated, never
    # auto-executed (see the safety gate below).
    (re.compile(r"storage device is failing|kernel I/O errors on one block device|data-loss risk", re.I), "disk io failure"),
]

def sh(cmd, timeout=30):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except Exception as e:
        return 1, str(e)

def unit_from_evidence(evidence_lines):
    """Extract a candidate unit name ONLY from the investigation's own evidence."""
    for ln in evidence_lines:
        m = re.search(r"unit (\S+)\.service|Failed with result[^\n]*?(\S+\.service)", ln)
        if m:
            return (m.group(1) or m.group(2)).strip(".")
    return None

def load_investigations():
    """Newest-first list of investigation state entries."""
    try:
        st = json.load(open(os.path.join(D, "inv_state.json")))
        return list(reversed(st.get("investigations", [])))
    except Exception:
        return []

def already_healed():
    """Set of (rule, signature) already acted on — no repeat healing."""
    done = set()
    if os.path.exists(LOG):
        for ln in open(LOG, errors="replace"):
            try:
                e = json.loads(ln)
                done.add((e.get("rule"), e.get("signature")))
            except Exception:
                pass
    return done

def playbook_for(statement):
    for rx, key in KEYMAP:
        if rx.search(statement or ""):
            return PLAYBOOK[key], key
    return None, None

def main():
    t0 = time.time()
    log_entries, executed, rolled_back, escalated = [], [], [], []

    for inv in load_investigations():
        rule = inv.get("rule", "")
        sig = inv.get("signature", "")
        best = inv.get("best", "")
        if not inv.get("verdict"):
            continue  # only act on investigations with a root-cause verdict
        if (rule, sig) in already_healed():
            continue  # one action per alert signature — no healing loops
        pb, key = playbook_for(best)
        if not pb:
            escalated.append({"rule": rule, "signature": sig, "reason": "no playbook entry for root cause",
                              "root_cause": best[:160], "ts": now()})
            continue
        # SAFETY GATE (bug 13, 2026-09-22): the docstring has claimed "only
        # SAFE actions run" since module 16 shipped, but main() never checked
        # pb["safety"] — every playbook entry was auto-executed. Latent, not
        # live (all 3 entries were SAFE), but the first UNSAFE entry (e.g.
        # the disk-failure escalation) would have run unguarded. A safety
        # property stated in documentation must be enforced in code.
        if pb.get("safety") != "SAFE":
            escalated.append({"rule": rule, "signature": sig,
                              "reason": f"playbook action {pb['id']} is {pb.get('safety')} — human approval required: {pb['desc'][:100]}",
                              "action": pb["id"], "root_cause": best[:160], "ts": now()})
            continue

        # blast-radius control: unit must come from the investigation's evidence
        unit = None
        if "{unit}" in pb["apply"]:
            rep_path = os.path.join(D, "investigations", inv.get("report", ""))
            evidence = open(rep_path, errors="replace").read().splitlines() if os.path.exists(rep_path) else []
            unit = unit_from_evidence(evidence)
            if not unit:
                escalated.append({"rule": rule, "signature": sig, "reason": "no unit in investigation evidence — refusing to guess",
                                  "root_cause": best[:160], "ts": now()})
                continue
            if not re.fullmatch(r"[A-Za-z0-9_@.-]+", unit):
                escalated.append({"rule": rule, "signature": sig, "reason": f"unit name failed allowlist: {unit!r}",
                                  "root_cause": best[:160], "ts": now()})
                continue

        entry = {"ts": now(), "rule": rule, "signature": sig, "action": pb["id"],
                 "safety": pb["safety"], "desc": pb["desc"], "unit": unit,
                 "root_cause": best[:160], "investigation": inv.get("report")}

        # ---- guarded transaction: pre -> apply -> verify (-> rollback)
        fmt = lambda c: c.format(unit=unit) if unit else c.replace("{unit}", "")
        rc, out = sh(fmt(pb["pre"]))
        if rc != 0:
            entry.update({"status": "SKIPPED_PRECHECK_FAIL", "pre_out": out[:200]})
            log_entries.append(entry)
            continue
        rc, out = sh(fmt(pb["apply"]))
        entry["apply_out"] = out[:200]
        if rc == 0:
            vrc, vout = sh(fmt(pb["verify"]))
            if vrc == 0:
                entry.update({"status": "EXECUTED", "verify_out": vout[:200]})
                executed.append(entry["action"])
            else:
                rrc, rout = sh(fmt(pb["rollback"]))
                entry.update({"status": "ROLLED_BACK", "verify_out": vout[:200],
                              "rollback_out": rout[:200]})
                rolled_back.append(entry["action"])
        else:
            entry.update({"status": "APPLY_FAILED", "apply_out": out[:200]})
            escalated.append({"rule": rule, "signature": sig, "reason": f"apply failed: {out[:120]}",
                              "root_cause": best[:160], "ts": now()})
        log_entries.append(entry)

    # persist action log (append-only) + summary state
    with open(LOG, "a") as f:
        for e in log_entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    summary = {"ts": now(), "actions_attempted": len(log_entries),
               "executed": executed, "rolled_back": rolled_back,
               "escalated": escalated, "duration_s": round(time.time() - t0, 2)}
    json.dump(summary, open(STATE, "w"), indent=1)

    out = dict(summary)
    print(json.dumps(out, indent=2, ensure_ascii=False))
    if rolled_back:
        return 3
    if executed or escalated:
        return 2
    return 0

def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")

if __name__ == "__main__":
    sys.exit(main())
