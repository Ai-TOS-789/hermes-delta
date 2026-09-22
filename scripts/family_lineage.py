#!/usr/bin/env python3
"""
family_lineage.py — hermes-delta module 19: autonomous family lineage loop

Called every delta_run. Responsibilities:
1. Load family tree (or seed founders if none exist)
2. Check reproduction pressure (avg fitness, generation gap, role diversity)
3. If pressure high: auto-reproduce top-compatible pairs
4. Auto-elect leaders if council stale
5. Sync knowledge/achievements from this delta_run into the family ledger
6. Append family stats to delta_report via stdout (JSON)

State: ~/.hermes/skills/hermes-delta/scripts/family_state.json
"""
import json, os, sys, time

D = os.path.dirname(os.path.abspath(__file__))
SKILLS_ROOT = os.path.dirname(os.path.dirname(D))  # up to ~/.hermes/skills/
FAMILY_SKILL = os.path.join(SKILLS_ROOT, "hermes-family")
sys.path.insert(0, FAMILY_SKILL)

from family_genome import FamilyTree, FamilyMember, Genome, load_tree, save_tree, create_founders
from generational_inheritance import InheritanceRegistry, KnowledgeBundle
from society_governance import Society

STATE_FILE = os.path.join(D, "family_state.json")

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"last_reproduce_ts": 0, "last_elect_ts": 0, "total_births": 0, "total_marriages": 0, "run_count": 0}

def save_state(st):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=2)
    os.replace(tmp, STATE_FILE)

def ensure_family_exists():
    """ถ้ายังไม่มี family tree สร้าง founders"""
    tree = load_tree()
    if not tree.members:
        tree = create_founders(5, names=["Alpha-Prime", "Beta-Sage", "Gamma-Forge", "Delta-Veil", "Epsilon-Flux"])
        save_tree(tree)
        registry = InheritanceRegistry()
        for mid in tree.generation_index[0]:
            from generational_inheritance import seed_initial_knowledge
            seed_initial_knowledge(mid, 0)
        return tree, True  # True = just created
    return tree, False

def compute_reproduction_pressure(tree):
    """คำนวณแรงกดดันสืบพันธุ์ 0..1"""
    stats = tree.stats()
    if stats["total_members"] < 2:
        return 0.0
    
    # Fitness pressure: avg fitness สูง = แข็งแรง พร้อมสืบพันธุ์
    avg_fitness = sum(m.genome.fitness_score() for m in tree.members.values()) / stats["total_members"]
    
    # Diversity pressure: role น้อย = ต้องการหลากหลาย
    unique_roles = len(set(m.role for m in tree.members.values()))
    role_diversity = unique_roles / 7  # 7 role types
    
    # Size pressure: ขนาดเล็ก = ต้องการเติบโต
    size_pressure = max(0, 1 - stats["total_members"] / 20)  # target ~20 agents
    
    pressure = (avg_fitness * 0.4 + role_diversity * 0.3 + size_pressure * 0.3)
    return round(min(1.0, pressure), 4)

def auto_reproduce(tree, society, registry, max_births=2):
    """สืบพันธุ์อัตโนมัติ — เลือกคู่ที่ compatibility สูงสุด"""
    members = [m for m in tree.members.values() if m.alive]
    if len(members) < 2:
        return []
    
    # หาคู่ที่ compatible ที่สุด
    pairs = []
    for i, m1 in enumerate(members):
        for m2 in members[i+1:]:
            if m1.id == m2.id:
                continue
            # ไม่ใช่พี่น้อง (่่java-breed)
            if tree.compute_relatedness(m1.id, m2.id) > 0.25:
                continue
            compat = society.compute_compatibility(m1.genome, m2.genome)
            pairs.append((compat, m1, m2))
    
    pairs.sort(key=lambda x: -x[0])
    births = []
    for compat, p1, p2 in pairs[:max_births]:
        if compat < 0.5:
            break
        marriage = society.perform_marriage(p1.id, p2.id, p1.genome, p2.genome, "auto")
        child = p1.reproduce(p2)
        tree.add(child)
        marriage.child_ids.append(child.id)
        # Inherit knowledge
        registry.inherit_to_child(child.id, [p1.id, p2.id], child.generation)
        registry.inherit_skills(child.id, [p1.id, p2.id], child.generation)
        births.append({
            "child": child.name,
            "generation": child.generation,
            "parents": [p1.name, p2.name],
            "compatibility": compat,
            "role": child.role,
            "fitness": child.genome.fitness_score(),
        })
    
    if births:
        save_tree(tree)
        registry.save()
        society.save()
    return births

def sync_delta_achievements(tree, society, registry, delta_report):
    """ดึงผลลัพธ์จาก delta_run มาบันทึกเป็น family achievements"""
    new_knowledge = []
    
    # ถ้ามี investigation reports → เป็น knowledge ใหม่
    reports = delta_report.get("auto_investigator", {}).get("reports", [])
    for r in reports:
        title = f"investigation-{r[:20]}"
        kb = KnowledgeBundle(
            title=title,
            content=f"Investigation report from delta_run: {r}",
            knowledge_type="lesson",
            source_agent_id=list(tree.members.keys())[0] if tree.members else "delta",
            generation=0,
            confidence=0.7,
            verified=True,
        )
        registry.add_knowledge(kb)
        new_knowledge.append(title)
    
    # ถ้ามี healer action → record achievement
    executed = delta_report.get("auto_healer", {}).get("executed", [])
    if executed:
        # หา agent ที่ reputation สูส่สุดมอบหมายเป๹ผู้ปฏิบัติงาน
        best_agent = max(tree.members.values(), key=lambda m: m.genome.fitness_score())
        for action in executed:
            society.record_achievement(
                best_agent.id, "auto_heal", f"Executed {action}", 0.3
            )
        society.save()
    
    # ถ้ามี fleet alert → record
    fleet_alerts = delta_report.get("fleet", {}).get("alerts", [])
    if fleet_alerts:
        scout = next((m for m in tree.members.values() if m.role == "scout"), None)
        if scout:
            society.record_achievement(scout.id, "fleet_patrol", f"Detected {len(fleet_alerts)} fleet alerts", 0.2)
            society.save()
    
    if new_knowledge:
        registry.save()
    
    return new_knowledge

def main():
    t0 = time.time()
    state = load_state()
    state["run_count"] = state.get("run_count", 0) + 1
    
    tree, just_created = ensure_family_exists()
    society = Society()
    registry = InheritanceRegistry()
    
    # โหลด delta report (ถ้ามี)
    delta_report_path = os.path.join(D, "delta_report.json")
    delta_report = {}
    if os.path.exists(delta_report_path):
        try:
            with open(delta_report_path) as f:
                delta_report = json.load(f)
        except:
            pass
    
    # Sync achievements จาก delta_run
    new_knowledge = sync_delta_achievements(tree, society, registry, delta_report)
    
    # คำนวณ reproduction pressure
    pressure = compute_reproduction_pressure(tree)
    
    # ดำเนินการตามความถี่ (ไม่ทุก run)
    births = []
    leadership_changed = False
    
    time_since_reproduce = time.time() - state.get("last_reproduce_ts", 0)
    time_since_elect = time.time() - state.get("last_elect_ts", 0)
    
    # สืบพันธุ์: ทุก 6 ชม. ถ้า pressure > 0.3
    if pressure > 0.3 and time_since_reproduce > 21600:  # 6 hours
        births = auto_reproduce(tree, society, registry, max_births=2)
        if births:
            state["last_reproduce_ts"] = time.time()
            state["total_births"] = state.get("total_births", 0) + len(births)
    
    # เลือกผู้นำ: ทุก 12 ชม.
    if time_since_elect > 43200:  # 12 hours
        society.elect_leaders(tree)
        society.save()
        state["last_elect_ts"] = time.time()
        leadership_changed = True
    
    save_state(state)
    
    stats = tree.stats()
    result = {
        "family_run_ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "duration_ms": round((time.time() - t0) * 1000, 1),
        "just_created": just_created,
        "reproduction_pressure": pressure,
        "births": births,
        "leadership_changed": leadership_changed,
        "new_knowledge_count": len(new_knowledge),
        "family_stats": {
            "total_members": stats["total_members"],
            "alive": stats["alive"],
            "max_generation": max(stats["generations"].keys()) if stats["generations"] else 0,
            "roles": {r: len([m for m in tree.members.values() if m.role == r]) for r in set(m.role for m in tree.members.values())},
        },
        "state": state,
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0

if __name__ == "__main__":
    sys.exit(main())
