#!/usr/bin/env python3
"""Belief Revision — hermes-delta module 18: unlearning what was learned wrong.

The pipeline learns from every investigation (modules 15→17), but until now
there was NO way to un-learn. If a root cause was confirmed under a buggy
rule (production 2026-09-22: NEW_PATTERN seeds matched desktop-noise regexes
against the whole journal window, so real systemd failures — heal-test.service
— were "confirmed" as desktop noise), those wrong entries stay in the
experience store FOREVER and recall keeps re-proposing them. A self-improving
system that can only add beliefs and never revise them is not self-improving
— it is self-poisoning.

This module closes that loop, belief-revision style (AGM-style contraction):

  RE-VERIFY  every confirmed root_cause entry in the experience store is
             re-tested against the CURRENT rule set. Two re-check paths:
               a) entries with inline evidence (new schema): re-run the
                  alert-line gate directly — the desktop-noise pattern must
                  appear in the alert line itself
               b) legacy entries (no inline evidence): re-check via the
                  report file if it still exists; if pruned AND the entry
                  cites desktop-noise for a .service failure, flag as
                  UNVERIFIABLE (citation rotted — can't re-check, mark)
  CONTRACT   entries whose re-verification CONTRADICTS the stored verdict
             get entry retracted: outcome flipped to "retracted",
             root_cause prefixed "[RETRACTED <ts> ...]", and a retraction
             record appended to the store so recall/evoskill see the
             revision (never silently deleted — audit trail preserved)
  QUARANTINE retracted statements are quarantined in recall: the recall
             index rebuild (module 11) skips entries with
             outcome == "retracted" — the poison no longer propagates

Also repairs citation rot: entries whose report file no longer exists are
marked "citation_rotted" (kept — they may still be correct, just unverifiable
via file; inline evidence from now on prevents new rot).

State: belief_revision_state.json
Exit codes: 0 ok, 2 = beliefs retracted (learning was wrong — see report)
"""
import json, os, re, sys, time

D = os.path.dirname(os.path.abspath(__file__))
EXP = os.path.expanduser("~/.hermes/skills/agent-self-learning/experience.jsonl")
INVDIR = os.path.join(D, "investigations")
STATE = os.path.join(D, "belief_revision_state.json")

# desktop-noise components — must appear in the ALERT LINE for a desktop-noise
# verdict to hold (mirrors auto_investigator's alert_line_only gate)
DESKTOP_RX = re.compile(r"gvfs|tracker|gnome-shell|colord|pipewire", re.I)
SERVICE_FAIL_RX = re.compile(
    r"\.service|failed with result|main process exited|exit-code|signal|coredump|segfault", re.I)
DESKTOP_NOISE_RX = re.compile(r"transient desktop-session error|desktop noise|gvfs/tracker", re.I)

def load_jsonl(p):
    out = []
    if os.path.exists(p):
        for ln in open(p, errors="replace"):
            ln = ln.strip()
            if ln:
                try:
                    out.append(json.loads(ln))
                except json.JSONDecodeError:
                    out.append({"type": "parse_error", "raw": ln[:100]})
    return out

def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")

def main():
    entries = load_jsonl(EXP)
    ts = now()
    retracted, unverifiable, citation_rotted, repaired = [], [], [], []

    for idx, e in enumerate(entries):
        if e.get("type") != "root_cause" or e.get("outcome") != "confirmed":
            continue
        rc = e.get("root_cause", "")
        if not DESKTOP_NOISE_RX.search(rc):
            continue  # only desktop-noise beliefs are re-checkable today

        alert = (e.get("alert_detail") or "").lower()
        inline = e.get("evidence_inline") or []

        # ---- path (a): inline evidence — re-run the alert-line gate
        if alert:
            desktop_in_alert = bool(DESKTOP_RX.search(alert))
            service_fail_in_alert = bool(SERVICE_FAIL_RX.search(alert))
            if service_fail_in_alert and not desktop_in_alert:
                # CONTRADICTION: a real service failure was confirmed as
                # desktop noise — retract (the exact production bug)
                entries[idx]["outcome"] = "retracted"
                entries[idx]["root_cause"] = (
                    f"[RETRACTED {ts}: service-failure alert misdiagnosed as "
                    f"desktop noise under the pre-gate rule — window-wide "
                    f"regex match false-confirmed it] " + rc[:400])
                entries[idx]["retraction_reason"] = (
                    "alert line shows a service failure with no desktop "
                    "component; the desktop-noise verdict cannot hold")
                retracted.append({"line": idx + 1, "ts": e.get("ts"),
                                  "alert": alert[:120]})
            elif desktop_in_alert:
                repaired.append({"line": idx + 1, "ts": e.get("ts"),
                                 "note": "re-verified: desktop component in alert line — belief holds"})
            # else: no signal either way — leave as-is (no evidence to flip on)

        # ---- path (b): legacy entry, no inline evidence
        else:
            rep = e.get("report")
            rep_ok = False
            if rep:
                rp = os.path.join(INVDIR, os.path.basename(rep))
                if os.path.exists(rp):
                    text = open(rp, errors="replace").read()
                    # re-check: does the report's own ALERT line contain a
                    # desktop component? If it shows a .service failure with
                    # no desktop words, the belief was wrong.
                    m = re.search(r"ALERT:\s*(.+)", text)
                    alert_line = (m.group(1) if m else text[:300]).lower()
                    if SERVICE_FAIL_RX.search(alert_line) and not DESKTOP_RX.search(alert_line):
                        entries[idx]["outcome"] = "retracted"
                        entries[idx]["root_cause"] = (
                            f"[RETRACTED {ts}: misdiagnosed under pre-gate rule "
                            f"(re-verified via report file)] " + rc[:400])
                        entries[idx]["retraction_reason"] = (
                            "report's alert line shows a service failure, not desktop noise")
                        retracted.append({"line": idx + 1, "ts": e.get("ts"),
                                          "alert": alert_line[:120], "via": "report"})
                        rep_ok = True  # we did re-verify (and retracted)
                    else:
                        rep_ok = True
                        repaired.append({"line": idx + 1, "ts": e.get("ts"),
                                         "note": "re-verified via report — belief holds"})
            if not rep_ok:
                # ---- path (c): report pruned — cross-reference the healer
                # audit log, which stores the exact alert SIGNATURE for each
                # investigation it acted on (module 16 blast-radius record)
                healed = False
                if rep:
                    base = os.path.basename(rep)
                    for h in load_jsonl(os.path.join(D, "healer_state.jsonl")):
                        if h.get("investigation") == base:
                            sig = (h.get("signature") or "").lower()
                            # signature format: "RULE:?: <alert detail>"
                            detail = sig.split(":", 2)[-1] if sig.count(":") >= 2 else sig
                            if SERVICE_FAIL_RX.search(detail) and not DESKTOP_RX.search(detail):
                                entries[idx]["outcome"] = "retracted"
                                entries[idx]["root_cause"] = (
                                    f"[RETRACTED {ts}: misdiagnosed under pre-gate rule "
                                    f"(re-verified via healer audit signature)] " + rc[:400])
                                entries[idx]["retraction_reason"] = (
                                    "healer audit log's alert signature shows a service "
                                    "failure, not desktop noise")
                                retracted.append({"line": idx + 1, "ts": e.get("ts"),
                                                  "alert": detail[:120], "via": "healer-log"})
                            else:
                                repaired.append({"line": idx + 1, "ts": e.get("ts"),
                                                 "note": "re-verified via healer audit — belief holds"})
                            healed = True
                            break
                if not healed:
                    # no report, no healer record → cannot re-verify at all
                    entries[idx]["citation_status"] = "citation_rotted"
                    citation_rotted.append({"line": idx + 1, "ts": e.get("ts")})

    # persist the revised store (atomic-ish: write temp then replace)
    if retracted or citation_rotted:
        tmp = EXP + ".tmp"
        with open(tmp, "w") as f:
            for e in entries:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        os.replace(tmp, EXP)

    # append retraction records so evoskill/recall see the revision event
    if retracted:
        with open(EXP, "a") as f:
            for r in retracted:
                f.write(json.dumps({
                    "type": "belief_revision", "ts": ts,
                    "action": "retract",
                    "retracted_line": r["line"],
                    "retracted_ts": r["ts"],
                    "patterns_found": ["belief-revision-retracted-desktop-noise"],
                    "symptoms": ["NEW_PATTERN", "misdiagnosis", "desktop-noise"],
                    "root_cause": ("investigation rule matched desktop-noise regex against "
                                   "the whole journal window instead of the alert line — "
                                   "real service failures were confirmed as noise"),
                    "fix": "alert-line gating (auto_investigator gate=alert_line_only)",
                    "outcome": "retracted",
                }, ensure_ascii=False) + "\n")

    st = json.load(open(STATE)) if os.path.exists(STATE) else {"revisions": []}
    st["revisions"].append({
        "ts": ts, "checked": sum(1 for e in entries if e.get("type") == "root_cause"),
        "retracted": len(retracted), "held": len(repaired),
        "citation_rotted": len(citation_rotted)})
    st["revisions"] = st["revisions"][-100:]
    json.dump(st, open(STATE, "w"), indent=1)

    print(json.dumps({
        "ts": ts,
        "retracted": retracted,
        "held": repaired,
        "citation_rotted": citation_rotted,
    }, indent=2, ensure_ascii=False))
    return 2 if retracted else 0

if __name__ == "__main__":
    sys.exit(main())
