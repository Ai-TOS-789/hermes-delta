#!/usr/bin/env python3
"""Regression suite for hermes-delta investigation pipeline.
Replays every PRODUCTION case that was ever misdiagnosed, against the fixed
code. Run: python3 test_delta.py  -> exit 0 = all pass, 1 = failures.
Each case cites the production incident it guards against.
"""
import json, os, re, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from anomaly_watch import parse_unit as parse14
from auto_investigator import hypotheses_for, test, parse_unit as parse15
from auto_healer import playbook_for

D = os.path.dirname(os.path.abspath(__file__))
FAILS = []

def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), name, ("— " + detail if detail and not cond else ""))
    if not cond:
        FAILS.append(name)

def investigate(alert, artifact):
    lines = open(os.path.join(D, artifact), errors="replace").read().splitlines()
    cap = {"artifact": "artifacts/" + os.path.basename(artifact), "lines": lines,
           "mem_pct": "10.0", "swap_pct": "0.0", "load": "0.5", "top_mem": "", "failed_units": ""}
    cands = hypotheses_for(alert, cap)
    return [test(h, cap["lines"], cap) for h in cands]

def first_confirmed(results):
    c = [r for r in results if r["verdict"] == "CONFIRMED"]
    return c[0]["statement"] if c else None

# ---- bug 3/6: Thai-locale month attribution (module 14 + 15) --------------
for fn, tag in ((parse14, "module14"), (parse15, "module15")):
    u, _ = fn("ก.ย. 22 14:18:26 master-ai gsd-media-keys[1900]: unable to get default sink")
    check(f"[{tag}] Thai month 'ก.ย.' attributes unit", u == "gsd-media-keys", f"got {u!r}")
u, _ = parse14("Sep 22 14:18:26 master-ai systemd[1]: Started session")
check("[module14] ASCII month still parses", u == "systemd", f"got {u!r}")

# ---- bug 2: alert must be investigated against ITS OWN cited window -------
r = investigate({"rule": "ERROR_RATE", "severity": "HIGH",
                 "detail": "130 error lines / 8min = 16.25/min",
                 "cite": "[anomaly:anomaly_20260922_134336.log:1]"},
                "artifacts/anomaly_20260922_134336.log")
c = first_confirmed(r)
check("[bug2] brave flood CONFIRMED single-subsystem",
      c is not None and "single subsystem is flooding" in c, str(c)[:80])

# ---- bug 4: gated hypotheses settle on the alert line, not the window -----
r = investigate({"rule": "NEW_PATTERN", "severity": "MEDIUM",
                 "detail": "gsd-media-keys: ก.ย. N N:N:N master-ai gsd-media-keys[N]: unable to get default sink",
                 "cite": "[anomaly:anomaly_20260922_142225.log:9]"},
                "artifacts/anomaly_20260922_142225.log")
c = first_confirmed(r)
check("[bug7-seed] gsd-media-keys -> desktop subsystem fault (was INSUFFICIENT)",
      c is not None and "desktop subsystem fault" in c, str(c)[:80])

# ---- widened regex: xdg-desktop-portal (journal truncates ident to 15ch) --
r = investigate({"rule": "NEW_PATTERN", "severity": "MEDIUM",
                 "detail": "xdg-desktop-por: failed to measure available space: error getting filesystem info for /media/aorus/bootforg",
                 "cite": "[anomaly:anomaly_20260922_151826.log:197]"},
                "artifacts/anomaly_20260922_151826.log")
c = first_confirmed(r)
check("[seed-wide] xdg-desktop-por -> desktop subsystem fault",
      c is not None and "desktop subsystem fault" in c, str(c)[:80])

# ---- module 18 regression: real service defect must NOT be desktop noise --
r = investigate({"rule": "NEW_PATTERN", "severity": "MEDIUM",
                 "detail": "heal-test.service: Failed with result exit-code",
                 "cite": "[anomaly:x:1]"},
                "artifacts/anomaly_20260922_134336.log")
c = first_confirmed(r)
check("[mod18] heal-test.service -> real service defect (not noise)",
      c is not None and "real service defect" in c, str(c)[:80])

# ---- healer KEYMAP: desktop-subsystem-fault statement must NOT map to noise
pb, key = playbook_for("A desktop subsystem fault affecting user sessions "
                       "(gsd-*/pipewire/wireplumber component error, not session chatter)")
check("[healer] subsystem-fault statement maps to NO auto-action (escalates)",
      key is None, f"mapped to {key!r}")

# ---- healer post-condition discipline (module 16 lesson) ------------------
check("[healer-doc] grep -qx active (exact match) still in playbook source",
      "grep -qx active" in open(os.path.join(D, "auto_healer.py")).read())

# ---- state migration integrity: no '?|' signatures remain -----------------
st = json.load(open(os.path.join(D, "anomaly_state.json")))
bad = [k for k in st.get("known", {}) if k.startswith("?|")]
check("[migration] zero unattributed signatures in known baseline",
      len(bad) == 0, f"{len(bad)} remain")

print()
print("RESULT:", "ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
