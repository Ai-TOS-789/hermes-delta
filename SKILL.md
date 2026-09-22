---
name: hermes-delta
description: "Autonomous investigation agent with self-improvement."
version: 0.1.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [hermes-delta, autonomous, investigation, self-improving, logcat-alternative]
    related_skills: [system-investigation, delta-compare, hypothesis-tracker, self-correction-loop, proactive-monitoring]
---

# Hermes Delta — Autonomous Investigation Agent

The full Hermes + Logcat convergence: an autonomous investigation engine that learns, remembers, and runs independently. Not a supervised tool — a self-improving agent that debugs, correlates, and fixes across the full stack.

## What Makes Hermes Delta Different from logcat.ai

| Capability | logcat.ai | Hermes Delta |
|------------|-----------|---------------|
| **Self-improving** | No — each investigation is isolated | Yes — learns from every session via skills |
| **Persistent memory** | No — no cross-investigation memory | Yes — remembers root-cause patterns across sessions |
| **Multi-platform** | Web console only | TUI, desktop, Telegram, Discord, Slack, email |
| **Provider-agnostic** | Frontier models only | 20+ providers, swap mid-workflow |
| **Autonomous operation** | Human must trigger | Cron, proactive loop, auto-investigate |
| **General-purpose** | OS-layer only | Code, research, write, debug, investigate |
| **Cross-session learning** | No | Yes — pattern library + experience store |
| **Multi-agent swarm** | No | Yes — delegate to parallel agents |
| **Remediation** | Proposes patch | Proposes + tests + reports |
| **Knowledge graph** | No | Yes — root cause → component → log → fix |

## The 5 Core Modules

### 1. System Investigation (`system-investigation`)
Ingest → Hypothesize → Test → Self-correct → Report. Citation discipline on every claim.

### 6. Predictive Failure Forecasting (`scripts/predict_failure.py`)
Samples mem/disk/load/error-rate every run, linear-regression trend + threshold blend, emits P(failure) with ETA ≤ 60 min. Auto-investigate when P > 0.7. State: `scripts/pf_state.jsonl`.

### 7. EvoSkill — Automated Skill Discovery (`scripts/evoskill.py`)
Analyzes the agent-self-learning experience store for recurring failure patterns (≥2 occurrences = held-out validation). Materializes them as skill folders under `~/.hermes/skills/evolved/` — retained only on recurrence. State: `scripts/evoskill_state.json`. Based on arXiv 2603.02766.

### 8. Knowledge Graph Builder (`scripts/build_kg.py`)
Merges experience store + evoskill state + pf history into a causal graph (root cause → skill → fix → monitored metrics). Renders dark-themed SVG via GraphViz: `scripts/kg_latest.svg`. State: `scripts/kg_state.json`.

### 9. Delta Run — Unified Pipeline (`scripts/delta_run.py`)
One-shot runner: predict (6) → evoskill (7) → knowledge graph (8) → skill doctor (10). Writes `scripts/delta_report.json`. Exit code 2 = anomaly (P>0.7) → run system-investigation protocol. Exit code 3 = unrepairable skill defects.

### 10. Skill Doctor — Library Audit & Repair (`scripts/skill_doctor.py`)
Closes the gap SkillFlow (arXiv 2604.17308) Finding 6 identified: "the key model gap lies in *repairing* bad skills, not writing them." Every delta_run:
1. **AUDIT** all skills — frontmatter defects, oversized descriptions, dead script refs (only when the skill packages that directory), duplicates, staleness
2. **REPAIR** mechanical defects (atomic write + re-verify; failed repair rolls back)
3. **VERIFY** — repaired file must pass re-audit or the repair is reverted
4. **CONSOLIDATE** — propose merges of duplicate-description skills (report-only; destructive merge needs human approval)
State: `scripts/skill_doctor_state.json`. Exit 3 = unrepairable defect (reported, never silently touched).

### 11. Root-Cause Recall — Symptom-Indexed Retrieval (`scripts/root_cause_recall.py`)
The retrieval half of cross-session learning: when a new investigation starts, query past root causes by symptom words. Inverted index (sub-token + hyphenated forms, idf-weighted) over the experience store + delta_report history; every hit carries citations (`file:line`). Rebuilt each delta_run. Query: `python3 root_cause_recall.py "async race crash"` → ranked patterns with citations and past outcomes. Empty result = pattern never seen (honest negative, no fabrication). Citation-rotted beliefs (module 18: report pruned, no inline evidence, no healer record) are NOT quarantined but DEMOTED — their outcome is annotated `unverified(citation-rotted)` so investigations weigh them as hints, not confirmed knowledge; auto_investigator ranks verified recalls above unverified ones. State: `scripts/recall_index.json`.

### 12. Fleet Sentinel — Remote Device Health (`scripts/fleet_sentinel.py`)
Probes the HSAES fleet (PI4, TizenRT, NAS326 on 192.168.31.0/24) every delta_run: ICMP median-of-3 latency, TCP port reachability, flapping detection, latency-slope degradation forecast. Emits `[fleet:DEVICE]` evidence lines for the investigation protocol. Exit 2 = CRITICAL/HIGH alert (device down or flapping) → auto-investigate. State: `scripts/fleet_state.jsonl`. Lessons baked in: embedded devices' single-ping latency is power-save jitter (use median-of-3); verify which TCP services actually run before assuming a closed port = defect (TizenRT exposes none — ICMP-only monitoring).

### 13. ArXiv Scanner — Autonomous Literature Radar (`scripts/arxiv_scanner.py`)
The "find what you don't have" module: every delta_run, queries arXiv for standing research interests (self-improving agents, skill learning, self-evolving agents, failure prediction, LLM root-cause analysis). Dedupes via `arxiv_state.json`, emits `[arxiv:ID]` evidence lines for the report and feeds the knowledge graph. Lessons baked in: arXiv `all:` search is OR-loose (surfaced "Affine Volterra covariance processes" for interest "agent skill learning memory") — use precise `ti:`/`abs:` queries AND a title relevance gate (≥2 significant interest words). State: `scripts/arxiv_state.json`.

### 14. Anomaly Watch — Proactive Journal Monitor (`scripts/anomaly_watch.py`)
Implements the proactive-monitoring skill's detection table against the real system journal: CRITICAL (OOM kill / kernel panic / lockup / watchdog), HIGH (error rate >10/min, service restart loops), MEDIUM (new error pattern, mem >90%). Every delta_run scans the window since the last run, saves the raw window to `scripts/artifacts/anomaly_<ts>.log` (last 20 kept) so every alert carries a `[anomaly:<file>:<line>]` citation. Alert-fatigue control: message signatures are normalized (numbers/UUIDs/IPs/hex → placeholders; 3 distinct IPs of "connection refused" = 1 pattern) and a pattern seen ≥3 runs is demoted to known noise — only CRITICAL rules re-alert on known noise. First run seeds the baseline silently. State: `scripts/anomaly_state.json`. Exit 2 = CRITICAL/HIGH → auto-investigate. Kernel messages come from journalctl (dmesg needs root here); kern.log tail is the fallback source.

### 15. Auto-Investigator — Alert→Action Closure (`scripts/auto_investigator.py`)
Closes the gap the pipeline had since module 14: alerts fired but nothing investigated. When anomaly_watch flags CRITICAL/HIGH — or MEDIUM NEW_PATTERN per the proactive-monitoring SLA (within 15 min) — this module runs the full system-investigation protocol automatically: INGEST (captures journal window, mem/swap/load, top-RSS processes, failed units), HYPOTHESES (2-5 candidates from a rule table keyed by alert rule + root-cause recall from module 11's index — cross-session learning), EVIDENCE (each hypothesis declares its own confirm/disconfirm regexes tested against the captured artifact), SELF-CORRECTION (CONFIRMED/PARTIAL/REJECTED/INSUFFICIENT per the self-correction-loop skill's verdict rules; failed hypotheses recorded, not hidden), REPORT (`investigations/inv_<ts>_<rule>_<seq>.md`, every claim cited `[artifact:line]`, proposed fix gated on human approval). Runs offline <5s, no LLM calls. Outcomes append to the experience store so evoskill + recall learn from every investigation. Same-alert dedup: an *resolved* signature is never re-investigated; an *unresolved* one may be re-investigated with fixed rules (max 2 attempts — a botched investigation must not be frozen, same principle as module 18). Exit 2 = CRITICAL root cause CONFIRMED → escalate. Lessons baked in: multiple alerts in one second must get unique filenames (sequence suffix — first version overwrote 5 reports into 1 file); recall-candidate verification must match keywords against the ALERT LINE ITSELF, not the whole journal window (window-wide matching always finds desktop words like gvfs/gnome on a desktop machine and falsely CONFIRMS unrelated recalled patterns — found by testing, fixed with alert-line keyword gating + stopword filter); alerts must be read from THIS run's scan (anomaly_alerts.json handoff), not delta_report.json which delta_run writes only at pipeline end (every investigation used to lag one run behind its evidence window and examined a mismatched artifact); the evidence window must be the artifact the alert's own `cite` names, not the newest file; journalctl month tokens are locale-dependent (`\w{3}` never matched Thai "ก.ย." — every unit parsed as "?", zero attribution); gated hypotheses settle BOTH confirm and disconfirm on the alert line — a window-wide disconfirm scan rejects every non-desktop hypothesis on a desktop machine (gnome-shell exists in every window); recalled statements must be de-prefixed before re-prefixing or "Recalled: **Recalled:" compounds each generation.

### 16. Auto-Healer — Guarded Auto-Remediation (`scripts/auto_healer.py`)
Closes the remediation gap module 15 left open ("fix needs human approval — always"). GuardedAct-style (arXiv 2609.11264 — blast-radius-aware guarded remediation, 87.4% recovery / −79.7% collateral damage): every CONFIRMED investigation verdict is mapped to a candidate action from a FIXED playbook (never free-form), then runs a guarded transaction — PRE (action allowlisted + preconditions hold + target unit appears in the investigation's OWN evidence — refuses to guess units not diagnosed, blast-radius control), APPLY, VERIFY (post-condition check), and auto-ROLLBACK to the snapshot if verification fails. Only SAFE actions auto-execute (restart failed *user* units, reset-failed, mark desktop noise known-noise); everything riskier is escalated for human approval — the human gate survives for risky actions. One action per alert signature (no healing loops). Action log: `healer_state.jsonl` (append-only audit trail). Exit 2 = action attempted, 3 = ROLLBACK occurred (healing failed, human needed). Lessons baked in: `grep -q active` substring-matches "inactive" — post-condition checks must use exact match (`grep -qx active`); first live test proved a rollback-destined unit EXECUTED on the substring bug.

### 17. Healer Feedback — Heal→Learn Loop (`scripts/healer_feedback.py`)
Closes the gap module 16 left open: the audit trail was write-only. If an action rolls back every run, the pipeline would retry forever without ever learning. This module is the missing feedback edge: reads every `healer_state.jsonl` entry, appends NEW outcomes to the agent-self-learning experience store as `type=healer_outcome` entries carrying BOTH schemas — `patterns_found=["healer-<status>-<action>"]` (so evoskill materializes a skill when a failing action recurs ≥2x: the system learns which remedies DON'T work) and `symptoms`+`root_cause`+`fix` (so root-cause recall surfaces "action X was tried and rolled back" with citations in future investigations). Aggregates per-action success rate over all history; ≥3 attempts with 0% success = UNRELIABLE → recommend demoting from the SAFE allowlist (report-only — playbook changes keep the human gate). Dedup via (ts, signature, action) keys in `healer_feedback_state.json` — re-runs never inflate evoskill counts. Exit 2 = UNRELIABLE action detected. Verified live: 10 real log entries fed back, recall now answers "MARK_DESKTOP_NOISE -> EXECUTED" with citations, evoskill materialized `evolved-healer-executed-mark_desktop_noise`, and a sandboxed 3x-rollback test correctly flagged RESTART_FAILED_USER_UNIT as UNRELIABLE with exit 2.

### 18. Belief Revision — Unlearning What Was Learned Wrong (`scripts/belief_revision.py`)
The gap nobody notices until it bites: modules 15→17 could only ADD beliefs. When a root cause was confirmed under a buggy rule (production 2026-09-22: NEW_PATTERN seeds matched desktop-noise regexes against the whole journal window, so real systemd failures — `heal-test.service` — were "confirmed" as desktop noise and the healer marked them known-noise), the wrong entries stayed in the experience store FOREVER and recall kept re-proposing them. A self-improving system that can only add beliefs and never revise them is not self-improving — it is self-poisoning. Module 18 is the AGM-style contraction edge: RE-VERIFY every confirmed root_cause against the CURRENT rule set (three paths: inline evidence on the entry, the report file if still present, the healer audit log's alert signature as last resort), CONTRACT contradicted entries (outcome flipped to `retracted`, root_cause prefixed `[RETRACTED <ts>...]` — never silently deleted, audit trail preserved), and QUARANTINE retracted statements so the recall index rebuild skips them (the poison stops propagating). Also detects citation rot (entry's report pruned + no inline evidence → marked `citation_rotted`); new entries now embed `alert_detail` + `evidence_inline` so future re-verification never depends on a prunable file. Verified live: 5 poison entries (heal-test.service misdiagnoses) retracted via healer-log cross-reference, 5 correct desktop-noise beliefs held, 10 citation-rotted entries marked, recall no longer surfaces the poison. Exit 2 = beliefs retracted. Lesson baked in: the fix for the original bug (alert-line gating in module 15) is necessary but NOT sufficient — the store must also be purged of beliefs formed under the buggy rule, or recall resurrects them.

### 2. Delta Compare (`delta-compare`)
Compare logs across versions, devices, builds. Timeline alignment + behavioral diff.

### 3. Hypothesis Tracker (`hypothesis-tracker`)
Track every hypothesis: PENDING → TESTING → CONFIRMED/REJECTED/REVISED. Failed hypotheses surfaced, not hidden.

### 4. Self-Correction Loop (`self-correction-loop`)
Evidence contradicts → revise hypothesis. Max 3 rounds, max 5 hypotheses. No infinite loops.

### 5. Proactive Monitoring (`proactive-monitoring`)
Tail logs, detect anomalies, auto-investigate. 24/7 without human trigger.

## The Full Pipeline

```
┌─────────────────────────────────────────────────────┐
│  Hermes Delta — Full Autonomous Pipeline             │
├─────────────────────────────────────────────────────┤
│                                                     │
│  ┌─────────────────────────────────────────────┐   │
│  │  Proactive Monitor (24/7)                   │   │
│  │  journalctl -f, dmesg -w, custom logs       │   │
│  └─────────────────┬───────────────────────────┘   │
│                    ↓                                │
│  ┌─────────────────────────────────────────────┐   │
│  │  Anomaly Detector                           │   │
│  │  OOM, panic, error spike, new pattern        │   │
│  └─────────────────┬───────────────────────────┘   │
│                    ↓                                │
│  ┌─────────────────────────────────────────────┐   │
│  │  Investigation Engine                       │   │
│  │  Ingest → Hypothesize → Test → Self-correct  │   │
│  │  → Report (cited)                           │   │
│  └─────────────────┬───────────────────────────┘   │
│                    ↓                                │
│  ┌─────────────────────────────────────────────┐   │
│  │  Delta Compare (optional)                   │   │
│  │  Version A vs B, device A vs B               │   │
│  └─────────────────┬───────────────────────────┘   │
│                    ↓                                │
│  ┌─────────────────────────────────────────────┐   │
│  │  Learning & Memory                          │   │
│  │  Store pattern → recall next time           │   │
│  │  Cross-session improvement                  │   │
│  └─────────────────┬───────────────────────────┘   │
│                    ↓                                │
│  ┌─────────────────────────────────────────────┐   │
│  │  Report + Notify                            │   │
│  │  Cited root cause + failed hypotheses       │   │
│  │  Human approves fix                         │   │
│  └─────────────────────────────────────────────┘   │
│                                                     │
└─────────────────────────────────────────────────────┘
```

## Supervised Alternative Full

Hermes Delta is NOT a supervised tool that waits for commands. It is:

1. **Autonomous** — runs 24/7 via cron, proactive monitoring, self-triggered investigations
2. **Self-improving** — every investigation makes the next one faster via pattern memory
3. **Self-correcting** — evidence-driven hypothesis revision, no human needed for debugging logic
4. **Self-documenting** — every claim cited, every failure recorded, full audit trail
5. **Self-scaling** — delegate to parallel agents for multi-device investigations

## Getting Started

```bash
# 1. Run a one-shot investigation
hermes chat -q 'Investigate why the system crashed last night'

# 2. Start proactive monitoring
hermes chat -q 'Start monitoring system logs for anomalies'

# 3. Compare two log files
hermes chat -q 'Compare dmesg from boot A and boot B'

# 4. Check investigation history
hermes chat -q 'What root causes have we found this week?'
```

## Pitfalls

1. **Overconfidence** — always cite, never claim without evidence
2. **Confirmation bias** — actively seek disconfirming evidence
3. **Alert fatigue** — tune anomaly thresholds
4. **Investigation cost** — only investigate when anomaly confirmed
5. **Memory bloat** — prune stale patterns regularly
6. **Audit false positives** — when scanning skills for dead file refs: exclude code fences (example commands belong to OTHER projects), require the skill to actually package the referenced directory, and mind regex extension boundaries (.jsonl ≠ .json). First skill_doctor run flagged 27 defects; verification showed 19 were false positives.

## Verification Checklist

- [ ] All 5 modules loaded and linked
- [ ] Proactive monitoring active
- [ ] Anomaly detection rules configured
- [ ] Investigation protocol follows 5 phases
- [ ] Every claim cited
- [ ] Failed hypotheses recorded
- [ ] Learning loop active
- [ ] Human approval gate for fixes
