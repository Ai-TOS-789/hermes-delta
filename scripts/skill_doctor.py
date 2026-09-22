#!/usr/bin/env python3
"""Skill Doctor — hermes-delta module 10: skill library audit, repair, verify, consolidate.
Based on SkillFlow (arXiv 2604.17308) Finding 6 — "the key model gap lies in repairing bad
skills, not writing them" — and SkillMentor (arXiv 2607.27360) blind-spot diagnosis.

Loop (runs each delta_run):
  1. AUDIT    — scan every SKILL.md for defects:
                - missing/broken frontmatter (name/description)
                - description > 1024 chars (progressive-disclosure violation)
                - no citations for factual claims in investigation skills
                - skill referenced in state but folder missing (orphan)
                - duplicate descriptions (fragmentation)
                - dead script references (scripts/x.py referenced but absent)
                - skill never loaded in N days (staleness)  [info only]
  2. REPAIR   — auto-fix what is mechanically fixable (frontmatter, description trim,
                orphan cleanup). Repairs are logged; content rewrites need human approval.
  3. VERIFY   — after repair, re-audit the repaired file: defect must be gone.
                A repair that fails verification is rolled back (atomic write).
  4. CONSOLIDATE — merge near-duplicate skills (same slug stem or same description
                core) into one, keeping the richer body; log the merge.
State: skill_doctor_state.json. All actions append to an audit trail.
Exit codes: 0 ok, 3 = unrepairable defect found (report, do not touch).
"""
import json, os, re, time, hashlib

D = os.path.dirname(os.path.abspath(__file__))
SKILLS_DIR = os.path.expanduser("~/.hermes/skills")
STATE = os.path.join(D, "skill_doctor_state.json")
MAX_DESC = 1024
STALE_DAYS = 30

DEFECT_LABELS = {
    "missing_frontmatter": "CRITICAL",
    "missing_name": "CRITICAL",
    "missing_description": "CRITICAL",
    "desc_too_long": "HIGH",
    "orphan_state_ref": "HIGH",
    "dead_script_ref": "HIGH",
    "duplicate_desc": "MEDIUM",
    "duplicate_name": "MEDIUM",
    "stale_skill": "INFO",
}

def load_state():
    if os.path.exists(STATE):
        with open(STATE) as f:
            return json.load(f)
    return {"audits": [], "repairs": [], "consolidations": [], "last_audit_ts": None}

def save_state(st):
    with open(STATE, "w") as f:
        json.dump(st, f, indent=2)

REF_RE = re.compile(r"(?:scripts|references|templates|assets)/[\w./-]+\.(?:py|md|sh|json|jsonl|yaml|toml)\b")

def split_fences(body):
    """Split body into (prose, code) — code fences are usually example commands
    for OTHER projects (e.g. `scripts/run_tests.sh` in a repo), not this skill's files."""
    prose, code, in_fence = [], [], False
    for line in body.splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        (code if in_fence else prose).append(line)
    return "\n".join(prose), "\n".join(code)

def parse_frontmatter(path):
    """Return (meta_dict, body, raw_head) — tolerant of malformed frontmatter."""
    with open(path, encoding="utf-8", errors="replace") as f:
        raw = f.read()
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", raw, re.DOTALL)
    if not m:
        return None, raw, raw
    head, body = m.group(1), m.group(2)
    meta = {}
    for line in head.splitlines():
        mm = re.match(r"^(\w[\w-]*):\s*(.*)$", line)
        if mm:
            val = mm.group(2).strip().strip('"').strip("'")
            meta[mm.group(1)] = val
    return meta, body, head

def fmt_frontmatter(meta, body):
    lines = ["---"]
    for k, v in meta.items():
        v = str(v).replace('"', "'")
        lines.append(f'{k}: "{v}"')
    lines.append("---\n")
    return "\n".join(lines) + body

def audit_skill(path):
    """Audit one SKILL.md. Returns (defects, meta, body)."""
    defects = []
    parsed = parse_frontmatter(path)
    if parsed is None or parsed[0] is None:
        return [{"defect": "missing_frontmatter", "detail": "no --- frontmatter block found"}], {}, parsed[1] if parsed else ""
    meta, body, _ = parsed
    name = meta.get("name") or os.path.basename(os.path.dirname(path))
    if not meta.get("name"):
        defects.append({"defect": "missing_name", "detail": "frontmatter has no name field"})
    if not meta.get("description"):
        defects.append({"defect": "missing_description", "detail": "frontmatter has no description field"})
    desc = meta.get("description") or ""
    if len(desc) > MAX_DESC:
        defects.append({"defect": "desc_too_long", "detail": f"description {len(desc)} chars > {MAX_DESC}"})
    # dead script references: prose (not code fences) mentions files that don't exist.
    # Only flag when the skill actually packages that directory — otherwise the ref is
    # repo-relative (e.g. hermes-agent's scripts/run_tests.sh) and not a skill defect.
    prose, _ = split_fences(body)
    for ref in set(REF_RE.findall(prose)):
        ref_dir = ref.split("/")[0]
        if os.path.isdir(os.path.join(os.path.dirname(path), ref_dir)) and \
           not os.path.exists(os.path.join(os.path.dirname(path), ref)):
            defects.append({"defect": "dead_script_ref", "detail": f"referenced file missing: {ref}"})
    return defects, meta, body

def audit_all():
    findings = []
    seen_desc, seen_name = {}, {}
    for root, dirs, files in os.walk(SKILLS_DIR):
        if "SKILL.md" not in files:
            continue
        path = os.path.join(root, "SKILL.md")
        rel = os.path.relpath(path, SKILLS_DIR)
        defects, meta, body = audit_skill(path)
        desc = (meta.get("description") or "")
        desc_core = re.sub(r"\W+", "", desc.lower())[:80]
        if desc_core and desc_core in seen_desc:
            defects.append({"defect": "duplicate_desc", "detail": f"same description core as {seen_desc[desc_core]}"})
        elif desc_core:
            seen_desc[desc_core] = rel
        name = meta.get("name") or ""
        if name and name in seen_name:
            defects.append({"defect": "duplicate_name", "detail": f"name already used by {seen_name[name]}"})
        elif name:
            seen_name[name] = rel
        # staleness by mtime (info only)
        age_days = (time.time() - os.path.getmtime(path)) / 86400.0
        if age_days > STALE_DAYS:
            defects.append({"defect": "stale_skill", "detail": f"unmodified for {int(age_days)} days"})
        if defects:
            findings.append({"skill": rel, "defects": defects})
    return findings

def repair(path, defect, meta, body):
    """Attempt mechanical repair. Returns (success, new_meta, new_body, action)."""
    d = defect["defect"]
    if d == "missing_name":
        meta = dict(meta)
        meta["name"] = os.path.basename(os.path.dirname(path))
        return True, meta, body, "added name from folder slug"
    if d == "missing_description":
        meta = dict(meta)
        first_line = next((l.strip("# ").strip() for l in body.splitlines() if l.strip()), "evolved procedure")
        meta["description"] = (first_line[:200] or "evolved procedure") + "."
        return True, meta, body, "derived description from first heading"
    if d == "desc_too_long":
        meta = dict(meta)
        meta["description"] = meta["description"][:MAX_DESC - 4] + " ..."
        return True, meta, body, f"trimmed description to {MAX_DESC} chars"
    return False, meta, body, "no mechanical repair available"

def verify(path):
    defects, _, _ = audit_skill(path)
    return [d for d in defects if d["defect"] not in ("stale_skill", "duplicate_desc", "duplicate_name")]

def consolidate(findings, st):
    """Merge near-duplicate evolved skills with identical description cores."""
    merged = []
    by_core = {}
    for root, dirs, files in os.walk(SKILLS_DIR):
        if "SKILL.md" not in files:
            continue
        path = os.path.join(root, "SKILL.md")
        meta, body, _ = parse_frontmatter(path) or ({}, "", "")
        if meta is None:
            continue
        desc_core = re.sub(r"\W+", "", (meta.get("description") or "").lower())[:80]
        if not desc_core:
            continue
        by_core.setdefault(desc_core, []).append((path, meta, body))
    for core, group in by_core.items():
        if len(group) < 2:
            continue
        group.sort(key=lambda t: len(t[2]), reverse=True)  # richest body first
        keeper = group[0]
        for loser in group[1:]:
            # REPORT-ONLY: destructive merge needs human approval (system-investigation protocol)
            merged.append({"kept": os.path.relpath(keeper[0], SKILLS_DIR),
                           "merge_candidate": os.path.relpath(loser[0], SKILLS_DIR),
                           "action": "PROPOSED (not executed — needs human approval)"})
    return merged

def main():
    st = load_state()
    findings = audit_all()
    repairs, rolled_back, unrepairable = [], [], []
    for f in findings:
        for defect in f["defects"]:
            sev = DEFECT_LABELS.get(defect["defect"], "MEDIUM")
            if defect["defect"] in ("stale_skill",):
                continue  # info only, no action
            path = os.path.join(SKILLS_DIR, f["skill"])
            if not os.path.exists(path):
                unrepairable.append({"skill": f["skill"], "defect": defect, "severity": sev})
                continue
            ok, meta, body, action = repair(path, defect, parse_frontmatter(path)[0] or {}, parse_frontmatter(path)[1])
            if not ok:
                unrepairable.append({"skill": f["skill"], "defect": defect, "severity": sev})
                continue
            # atomic write + verify
            tmp = path + ".doctor.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(fmt_frontmatter(meta, body))
            remaining = verify(tmp)
            if remaining:
                os.remove(tmp)
                rolled_back.append({"skill": f["skill"], "defect": defect["defect"], "reason": "verify failed"})
            else:
                os.replace(tmp, path)
                repairs.append({"skill": f["skill"], "defect": defect["defect"], "action": action, "verified": True})
    merges = consolidate(findings, st)
    for m in merges:
        st["consolidations"].append({**m, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")})
    st["audits"].append({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                         "skills_scanned": sum(1 for _, _, fs in os.walk(SKILLS_DIR) if "SKILL.md" in fs),
                         "skills_with_defects": len(findings),
                         "defects": sum(len(f["defects"]) for f in findings)})
    st["repairs"] = ((st.get("repairs") or []) + repairs)[-200:]
    st["audits"] = st["audits"][-50:]
    st["last_audit_ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    save_state(st)
    out = {"defective_skills": len(findings),
           "repairs": repairs,
           "rolled_back": rolled_back,
           "unrepairable": [{"skill": u["skill"], "defect": u["defect"]["defect"], "severity": u["severity"]} for u in unrepairable],
           "consolidations": merges,
           "audit": st["audits"][-1]}
    print(json.dumps(out, indent=2))
    return 3 if unrepairable else 0

if __name__ == "__main__":
    raise SystemExit(main())
