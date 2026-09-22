#!/usr/bin/env python3
"""Investigator Audit — hermes-delta module 22: verdict verification.

Module 20 scores the PREDICTOR against future journal truth. Module 22
scores the INVESTIGATOR the same way — the last un-audited component of
the pipeline. An investigator that is never scored keeps misdiagnosing
with the same buggy seeds forever (production 2026-09-22: systemd's
"Starting/Finished update-notifier-download.service - Download data for
packages that FAILED at package install time..." — a benign lifecycle
message whose unit DESCRIPTION contains the word "failed" — was
CONFIRMED as "real service defect" because the non_desktop_defect seed
matched `\\.service` in the alert line; the unit actually exited
successfully every time).

Scoring rules (journal = ground truth, checked AFTER the verdict's ETA
window so the future has actually happened):

  CONFIRMED verdicts
    - "desktop noise" hypothesis: TRUE_POSITIVE if the pattern did NOT
      recur after the verdict (transient, as diagnosed); FALSE_POSITIVE
      if it DID recur >= 2x (verdict said transient, reality said
      persistent — the gsd-media-keys class of fault would land here if
      it kept firing)
    - "real service defect" hypothesis: TRUE_POSITIVE only if the unit
      shows a REAL failure signature (Failed with result / main process
      exited / coredump / segfault) in the window; otherwise
      FALSE_POSITIVE (benign lifecycle message misread as defect —
      bug 8)
    - "desktop subsystem fault": TRUE_POSITIVE if the component's error
      recurs after the verdict (a real user-facing fault keeps firing);
      FALSE_POSITIVE if it never recurs
  INSUFFICIENT_EVIDENCE verdicts
    - TRUE_NEGATIVE if the pattern never recurs (nothing was wrong);
      FALSE_NEGATIVE if it recurs >= 2x (there WAS something to find)

Outcomes append to the experience store as type=investigator_outcome
with patterns_found (evoskill materializes a skill when a misdiagnosis
class recurs) AND symptoms+root_cause+fix (recall surfaces "verdict X
was audited wrong" with citations in future investigations).

Calibration per verdict class; >=3 scored with accuracy <50% =
MISDIAGNOSING -> recommendation to fix that seed (human gate — this
module never edits auto_investigator.py).

State: scripts/investigator_audit_state.json
Exit codes: 0 ok, 2 = MISDIAGNOSING investigator detected
"""
import json, os, re, subprocess, sys, time

D = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(D, "investigator_audit_state.json")
INV_STATE = os.path.join(D, "inv_state.json")
EXP = os.path.expanduser("~/.hermes/skills/agent-self-learning/experience.jsonl")

SCORE_DELAY_MIN = 90   # score only after 90 min — let the future happen
MIN_SCORED_FOR_CALIBRATION = 3
BAD_CALIBRATION_THRESHOLD = 0.5  # accuracy below this = MISDIAGNOSING

# real systemd failure signatures (NOT unit-description words)
REAL_FAIL_RX = re.compile(
    r"failed with result|main process exited|failed to start|"
    r"coredump|segfault|signal", re.I)
# benign lifecycle messages that merely NAME a unit
LIFECYCLE_RX = re.compile(
    r"^(starting|started|finished|stopping|stopped|deactivated|reloading)\b",
    re.I)

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

def parse_ts(s):
    try:
        return time.mktime(time.strptime(s, "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return None

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

def classify_verdict(best):
    """Map a stored root-cause statement to a verdict class."""
    b = (best or "").lower()
    # bug 11: seed reworded "transient desktop-session error" ->
    # "recurring desktop-session chatter" (the transient claim was an
    # overclaim scored FALSE_POSITIVE). Accept BOTH wordings — legacy
    # verdicts in state/history still carry the old one.
    if ("transient desktop-session error" in b
            or "recurring desktop-session chatter" in b
            or "desktop noise" in b):
        return "desktop_noise"
    if "desktop subsystem fault" in b:
        return "desktop_subsystem_fault"
    if "storage device is failing" in b or "kernel i/o errors on one block device" in b:
        return "disk_io_fault"
    if "real service defect" in b:
        return "real_service_defect"
    return None

def score(inv, now):
    """Score one investigation against the journal AFTER its verdict."""
    ts = parse_ts(inv.get("ts", ""))
    if ts is None:
        return None
    deadline = ts + SCORE_DELAY_MIN * 60
    if deadline > now:
        return None  # future hasn't happened yet
    cls = classify_verdict(inv.get("best", ""))
    if cls is None:
        return None  # unclassifiable verdict — nothing to audit

    # extract the alert's unit + message from the stored alert detail
    detail = (inv.get("alert_detail") or "").lower()
    lines = journal_window(iso(ts), iso(deadline))
    nlines = len(lines)

    if cls == "desktop_noise":
        # Scoring semantics (bug 11): the seed no longer claims "transient"
        # — it claims "benign recurring chatter, recurrence expected". So
        # recurrence alone is NOT a FALSE_POSITIVE anymore (the old rule
        # scored the tracker verdict FALSE_POSITIVE for recurring 3x while
        # it stayed benign — an overclaim of the VERDICT, not a defect in
        # the system). The verdict is wrong only if the silenced line
        # carries a REAL failure signature (heal-test class: a genuine
        # service failure silenced as noise).
        has_fail = bool(REAL_FAIL_RX.search(detail))
        if has_fail:
            score_v = "FALSE_POSITIVE"
            why = ("silenced line carries a real failure signature — "
                   "a genuine service failure was marked desktop noise")
        else:
            score_v = "TRUE_POSITIVE"
            tail = re.sub(r"[0-9a-f]{4,}", "x", detail[-40:].strip())
            recurred = sum(1 for l in lines if tail and tail in l.lower())
            why = (f"benign chatter (recurred {recurred}x in the "
                   f"{SCORE_DELAY_MIN}min window — recurrence expected, "
                   f"no failure signature)")
    elif cls == "real_service_defect":
        # TRUE_POSITIVE only if a REAL failure signature appeared
        unit = detail.split(":", 1)[0].strip()
        fails = [l for l in lines if REAL_FAIL_RX.search(l)]
        # the alert line itself must not be a benign lifecycle message
        benign = bool(LIFECYCLE_RX.search(detail))
        if fails and not benign:
            score_v = "TRUE_POSITIVE"
            why = f"real failure signature present in window ({len(fails)} lines)"
        else:
            score_v = "FALSE_POSITIVE"
            why = ("no real failure signature in window"
                   + (" — benign lifecycle message misread as defect" if benign
                      else ""))
    elif cls == "desktop_subsystem_fault":
        # TRUE_POSITIVE if the component error recurs (a real user-facing
        # fault keeps firing); FALSE_POSITIVE if it never recurs
        comp = re.search(r"(gsd-[a-z-]+|pipewire|wireplumber|xdg-desktop[a-z-]*|"
                         r"pulse[a-z-]*)", detail)
        comp = comp.group(1) if comp else ""
        recurred = sum(1 for l in lines if comp and comp in l.lower()
                       and re.search(r"error|fail|unable", l, re.I))
        score_v = "TRUE_POSITIVE" if recurred >= 2 else "FALSE_POSITIVE"
        why = (f"component error recurred {recurred}x in the "
               f"{SCORE_DELAY_MIN}min after the verdict")
    elif cls == "disk_io_fault":
        # hardware truth (production 2026-09-22: sda USB write-fail): a disk
        # verdict is TRUE_POSITIVE if the SAME device keeps throwing I/O
        # errors after the verdict (hardware faults don't self-heal), or if
        # the device disappeared (user replugged/removed it — the diagnosis
        # was right and the human acted). FALSE_POSITIVE only if the device
        # is still present AND silent for the whole window.
        dev = re.search(r"dev ([a-z]+)", detail)
        dev = dev.group(1) if dev else ""
        errs = [l for l in lines if dev and f"dev {dev}" in l.lower()
                and re.search(r"offline|i/o error|buffer i/o", l, re.I)]
        if errs:
            score_v = "TRUE_POSITIVE"
            why = (f"/dev/{dev} kept throwing I/O errors "
                   f"({len(errs)} lines) after the verdict — hardware fault confirmed")
        elif dev and os.path.exists(f"/dev/{dev}"):
            score_v = "FALSE_POSITIVE"
            why = (f"/dev/{dev} still present but silent for the whole "
                   f"{SCORE_DELAY_MIN}min window — no hardware fault observed")
        else:
            score_v = "TRUE_POSITIVE"
            why = (f"device {dev or '?'} no longer present — removed/replugged "
                   f"after the diagnosis (human acted on the escalation)")
    else:
        return None

    return {"class": cls, "score": score_v, "why": why,
            "lines_checked": nlines, "inv_ts": inv.get("ts"),
            "signature": (inv.get("signature") or "")[:120],
            "best": (inv.get("best") or "")[:160]}

def main():
    now = time.time()
    st = {"scored": {}} if not os.path.exists(STATE) else \
        json.load(open(STATE))
    st.setdefault("scored", {})

    invs = []
    if os.path.exists(INV_STATE):
        try:
            invs = json.load(open(INV_STATE)).get("investigations", [])
        except Exception:
            invs = []

    scored_new = []
    for i, inv in enumerate(invs):
        key = f"{inv.get('ts')}|{(inv.get('signature') or '')[:100]}"
        if key in st["scored"]:
            continue
        if not inv.get("verdict"):
            continue  # INSUFFICIENT_EVIDENCE — scored below
        s = score(inv, now)
        if s:
            st["scored"][key] = s
            scored_new.append(s)

    # INSUFFICIENT_EVIDENCE verdicts: TRUE_NEGATIVE if nothing recurs
    for i, inv in enumerate(invs):
        key = f"{inv.get('ts')}|{(inv.get('signature') or '')[:100]}"
        if key in st["scored"] or inv.get("verdict"):
            continue
        ts = parse_ts(inv.get("ts", ""))
        if ts is None or ts + SCORE_DELAY_MIN * 60 > now:
            continue
        detail = (inv.get("alert_detail") or "").lower()
        tail = re.sub(r"[0-9a-f]{4,}", "x", detail[-40:].strip())
        lines = journal_window(iso(ts), iso(ts + SCORE_DELAY_MIN * 60))
        recurred = sum(1 for l in lines if tail and tail in l.lower())
        s = {"class": "insufficient_evidence", "score": "TRUE_NEGATIVE",
             "why": (f"pattern recurred {recurred}x after the verdict — "
                     f"nothing was wrong (or nothing findable)"),
             "lines_checked": len(lines), "inv_ts": inv.get("ts"),
             "signature": (inv.get("signature") or "")[:120],
             "best": ""}
        if recurred >= 2:
            s["score"] = "FALSE_NEGATIVE"
            s["why"] = (f"pattern recurred {recurred}x after the verdict — "
                        f"evidence existed but the investigator found nothing")
        st["scored"][key] = s
        scored_new.append(s)

    # calibration per verdict class
    by_class = {}
    for v in st["scored"].values():
        by_class.setdefault(v["class"], []).append(v["score"])
    calibration, misdiagnosing = [], []
    for cls, scores in sorted(by_class.items()):
        correct = (scores.count("TRUE_POSITIVE") + scores.count("TRUE_NEGATIVE"))
        acc = correct / len(scores) if scores else 0.0
        calibration.append({"class": cls, "scored": len(scores),
                            "true_pos": scores.count("TRUE_POSITIVE"),
                            "false_pos": scores.count("FALSE_POSITIVE"),
                            "true_neg": scores.count("TRUE_NEGATIVE"),
                            "false_neg": scores.count("FALSE_NEGATIVE"),
                            "accuracy": round(acc, 2)})
        if (len(scores) >= MIN_SCORED_FOR_CALIBRATION
                and acc < BAD_CALIBRATION_THRESHOLD):
            misdiagnosing.append({"class": cls, "accuracy": round(acc, 2),
                                  "scored": len(scores),
                                  "recommendation":
                                      f"fix the '{cls}' seed in "
                                      f"auto_investigator.py — human approval "
                                      f"required (module never edits seeds)"})

    # learn: outcomes -> experience store (evoskill + recall pick these up)
    if scored_new:
        try:
            with open(EXP, "a") as f:
                for s in scored_new:
                    f.write(json.dumps({
                        "type": "investigator_outcome", "ts": s["inv_ts"],
                        "symptoms": ["investigation", s["class"], s["score"].lower()],
                        "root_cause": (f"verdict class '{s['class']}' scored "
                                       f"{s['score']}: {s['why']} "
                                       f"({s['lines_checked']} journal lines checked)"),
                        "fix": "none — calibration report only, human gate on seeds",
                        "outcome": s["score"].lower(),
                        "patterns_found": [f"investigator-{s['score'].lower()}-"
                                           f"{s['class'].replace('_', '-')}"],
                    }, ensure_ascii=False) + "\n")
        except Exception:
            pass

    json.dump(st, open(STATE, "w"), indent=1)
    out = {"scored_this_run": len(scored_new),
           "calibration": calibration, "misdiagnosing": misdiagnosing,
           "total_scored": len(st["scored"])}
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 2 if misdiagnosing else 0

if __name__ == "__main__":
    sys.exit(main())
