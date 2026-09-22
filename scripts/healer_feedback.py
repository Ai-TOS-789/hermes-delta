#!/usr/bin/env python3
"""Healer Feedback — hermes-delta module 17: closes the heal→learn loop.

Module 16 (auto_healer) writes an append-only audit trail but nothing reads
it — if an action rolls back every time it runs, the pipeline never learns
and keeps retrying forever. This module is the missing feedback edge:

  COLLECT   read every entry in healer_state.jsonl (the audit trail)
  AGGREGATE per action_id: executed / rolled_back / apply_failed / skipped
            counts + success rate over ALL history
  FEEDBACK  append NEW outcomes to the agent-self-learning experience store
            as type=healer_outcome entries carrying BOTH schemas:
              - patterns_found=["healer-<status>-<action_id>"]  -> evoskill
                (module 7) materializes a skill when a failing action
                recurs >=2x: the system literally learns which remedies
                don't work
              - symptoms + root_cause + fix                     -> recall
                (module 11) indexes them, so a future investigation of a
                similar alert recalls "action X was tried and rolled back"
                with a citation
  REPORT    effectiveness per action. An action with >=3 attempts and 0%
            success is UNRELIABLE — recommended for demotion from the SAFE
            allowlist (report-only: playbook changes need human approval,
            same gate as module 16 keeps for risky actions)

Dedup: processed entries tracked by (ts, signature, action) in
healer_feedback_state.json — the same outcome is never appended twice, so
re-running the pipeline doesn't inflate evoskill counts.
State: healer_feedback_state.json
Exit codes: 0 ok, 2 = unreliable action detected (playbook review needed)
"""
import json, os, sys, time

D = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(D, "healer_state.jsonl")
STATE = os.path.join(D, "healer_feedback_state.json")
EXP = os.path.expanduser("~/.hermes/skills/agent-self-learning/experience.jsonl")

# statuses that count as a real attempt (precheck skips are not attempts)
ATTEMPT_STATUSES = ("EXECUTED", "ROLLED_BACK", "APPLY_FAILED")
UNRELIABLE_MIN_ATTEMPTS = 3

def load_jsonl(p):
    if not os.path.exists(p):
        return []
    out = []
    for line in open(p, errors="replace"):
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out

def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")

def main():
    entries = load_jsonl(LOG)

    # ---- 1) dedup: which log entries were already fed back
    state = {"processed": [], "ts": now()}
    if os.path.exists(STATE):
        try:
            state = json.load(open(STATE))
            state.setdefault("processed", [])
        except Exception:
            pass
    seen = set(map(tuple, state["processed"]))

    # ---- 2) append NEW outcomes to the experience store (both schemas)
    new_entries, already = [], 0
    for e in entries:
        key = [str(e.get("ts", "")), str(e.get("signature", "")), str(e.get("action", ""))]
        if tuple(key) in seen:
            already += 1
            continue
        status = e.get("status", "?")
        action = e.get("action", "?")
        entry = {
            "type": "healer_outcome",
            "ts": e.get("ts", now()),
            "action": action,
            "status": status,
            "unit": e.get("unit"),
            # schema A: evoskill mines patterns_found (>=2x -> materialize skill)
            "patterns_found": [f"healer-{status.lower().replace('_', '-')}-{action.lower()}"],
            # schema B: recall indexes symptoms + root_cause (+ fix as outcome)
            "symptoms": [e.get("rule", "?"), status, action],
            "root_cause": (e.get("root_cause") or "")[:200],
            "fix": f"{action} -> {status}",
            "investigation": e.get("investigation"),
        }
        new_entries.append(entry)
        state["processed"].append(key)
        seen.add(tuple(key))

    if new_entries:
        with open(EXP, "a") as f:
            for e in new_entries:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")

    # ---- 3) aggregate effectiveness over ALL history (not just new)
    stats = {}
    for e in entries:
        a = e.get("action", "?")
        s = stats.setdefault(a, {"executed": 0, "rolled_back": 0, "apply_failed": 0,
                                 "skipped": 0, "escalated": 0})
        st = e.get("status", "?")
        if st == "EXECUTED":
            s["executed"] += 1
        elif st == "ROLLED_BACK":
            s["rolled_back"] += 1
        elif st == "APPLY_FAILED":
            s["apply_failed"] += 1
        elif st.startswith("SKIPPED"):
            s["skipped"] += 1
        else:
            s["escalated"] += 1

    actions = []
    unreliable = []
    for a, s in sorted(stats.items()):
        attempts = s["executed"] + s["rolled_back"] + s["apply_failed"]
        rate = round(s["executed"] / attempts, 2) if attempts else None
        rec = None
        if attempts >= UNRELIABLE_MIN_ATTEMPTS and s["executed"] == 0:
            rec = "UNRELIABLE — recommend demoting from SAFE allowlist (human approval)"
            unreliable.append(a)
        elif attempts >= UNRELIABLE_MIN_ATTEMPTS and rate is not None and rate < 0.5:
            rec = "FLAKY — review playbook pre/verify conditions (human approval)"
        actions.append({"action": a, **s, "attempts": attempts,
                        "success_rate": rate, "recommendation": rec})

    state["ts"] = now()
    # keep the processed list bounded (log is append-only; old keys stay valid)
    state["processed"] = state["processed"][-5000:]
    with open(STATE, "w") as f:
        json.dump(state, f, indent=1)

    out = {
        "ts": now(),
        "log_entries": len(entries),
        "new_feedback": len(new_entries),
        "already_processed": already,
        "actions": actions,
        "unreliable": unreliable,
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 2 if unreliable else 0

if __name__ == "__main__":
    sys.exit(main())
