#!/usr/bin/env python3
"""EvoSkill — Automated Skill Discovery (module 7)
Analyzes agent-self-learning experience store for recurring failure patterns
(>=2 occurrences = held-out validation). Materializes them as skill folders
under ~/.hermes/skills/evolved/. Retained only on recurrence.

Also ingests REAL experience from delta_report.json history (failed
hypotheses, anomaly investigations) — not just the demo store.

State: scripts/evoskill_state.json
"""
import json, os, time, hashlib
from collections import Counter
from pathlib import Path

HERE = Path(__file__).parent
STATE = HERE / "evoskill_state.json"
EXP_STORE = Path.home() / ".hermes/skills/agent-self-learning/experience.jsonl"
EVOLVED_DIR = Path.home() / ".hermes/skills/evolved"
REPORT = HERE / "delta_report.json"
REPORT_HISTORY = HERE / "delta_report_history.jsonl"

def load_jsonl(p):
    if not p.exists(): return []
    out = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if line:
            try: out.append(json.loads(line))
            except json.JSONDecodeError: pass
    return out

def main():
    # 1) Load experience store
    exp = load_jsonl(EXP_STORE)
    # 2) Load real delta_run history (failed hypotheses, anomalies)
    hist = load_jsonl(REPORT_HISTORY)
    real_patterns = []
    for h in hist:
        # anomalies and failed hypotheses are the real "failure patterns"
        if h.get("predict", {}).get("anomaly"):
            real_patterns.append("predicted-anomaly")
        sd = h.get("skill_doctor", {})
        if sd.get("unrepairable"):
            real_patterns.append("unrepairable-skill-defect")

    # 3) Count pattern occurrences (>=2 to materialize)
    counts = Counter()
    for e in exp:
        for p in e.get("patterns_found", []):
            counts[p] += 1
    for p in real_patterns:
        counts[p] += 1

    # 4) Load state (migrate legacy shapes: rejected may be a list)
    state = {"materialized": {}, "rejected": {}, "log": []}
    if STATE.exists():
        try:
            loaded = json.loads(STATE.read_text())
            if isinstance(loaded.get("materialized"), dict):
                state["materialized"] = loaded["materialized"]
            if isinstance(loaded.get("rejected"), dict):
                state["rejected"] = loaded["rejected"]
            elif isinstance(loaded.get("rejected"), list):
                # legacy list of {pattern, ...} entries
                for item in loaded["rejected"]:
                    if isinstance(item, dict) and "pattern" in item:
                        state["rejected"][item["pattern"]] = item
            if isinstance(loaded.get("log"), list):
                state["log"] = loaded["log"]
        except Exception:
            pass

    decisions = []
    newly = []
    for pattern, n in counts.items():
        if n >= 2:
            if pattern in state["materialized"]:
                decisions.append({"pattern": pattern, "decision": "ALREADY_MATERIALIZED"})
                continue
            # Materialize as evolved skill
            name = "evolved-" + pattern
            d = EVOLVED_DIR / name
            d.mkdir(parents=True, exist_ok=True)
            skill_md = f"""---
name: {name}
description: "Evolved from recurring pattern '{pattern}' (seen {n}x). Auto-materialized by EvoSkill."
version: 0.1.0
author: Hermes Delta EvoSkill
license: MIT
metadata:
  hermes:
    tags: [evolved, {pattern}]
    evolved_from: {pattern}
    occurrences: {n}
---

# {name}

Auto-evolved skill from recurring failure pattern **{pattern}** — observed {n} times in the experience store.

## When to Use
- Pattern '{pattern}' detected during debugging/review/research

## Core Lesson
Recurring pattern detected {n}x. When this pattern appears, check for it deliberately before concluding.
"""
            (d / "SKILL.md").write_text(skill_md)
            state["materialized"][pattern] = {"skill": name, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "occurrences": n}
            decisions.append({"pattern": pattern, "decision": "MATERIALIZED", "skill": name})
            newly.append(name)
        else:
            if pattern in state["rejected"]:
                decisions.append({"pattern": pattern, "v": n, "decision": "STILL_BELOW_THRESHOLD"})
            else:
                state["rejected"][pattern] = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "occurrences": n}
                decisions.append({"pattern": pattern, "v": n, "decision": "REJECTED_SINGLE_OCCURRENCE"})

    STATE.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    print(json.dumps({"materialized_total": len(state["materialized"]), "newly_materialized": newly, "decisions": decisions}, indent=2, ensure_ascii=False))

if __name__ == "__main__":
    main()
