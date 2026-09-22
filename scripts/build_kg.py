#!/usr/bin/env python3
"""Knowledge Graph Builder — hermes-delta module 8.
Merges experience store + evoskill state + pf history into a causal graph
(root cause -> component -> log -> fix), renders SVG via GraphViz.
State: kg_state.json. Output: kg_latest.svg next to this script."""
import json, os, subprocess, time, hashlib

D = os.path.dirname(os.path.abspath(__file__))
EXP = os.path.expanduser("~/.hermes/skills/agent-self-learning/experience.jsonl")
EVOSTATE = os.path.join(D, "evoskill_state.json")
PF = os.path.join(D, "pf_state.jsonl")
STATE = os.path.join(D, "kg_state.json")
SVG = os.path.join(D, "kg_latest.svg")

def load_jsonl(p):
    if not os.path.exists(p):
        return []
    with open(p) as f:
        return [json.loads(l) for l in f if l.strip()]

def load_json(p, default):
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return default

def nid(label, prefix):
    return prefix + hashlib.md5(label.encode()).hexdigest()[:8]

def main():
    exp = load_jsonl(EXP)
    evo = load_json(EVOSTATE, {"materialized": {}, "rejected": []})
    pf = load_jsonl(PF)

    nodes, edges = {}, set()
    def node(n, label, shape, color):
        nodes[n] = (label, shape, color)
    def edge(a, b, label=""):
        edges.add((a, b, label))

    # layer 1: patterns (root causes) from experience store
    pat_count = {}
    for e in exp:
        skill = e.get("skill", "?")
        sk = nid(skill, "skill_")
        node(sk, skill, "box", "#4a9eff")
        for p in e.get("patterns_found", []):
            pn = nid(p, "pat_")
            pat_count[p] = pat_count.get(p, 0) + 1
            node(pn, f"{p}\\n({pat_count[p]}x)", "ellipse", "#f59e0b")
            edge(pn, sk, "found via")
            if e.get("outcome") == "failure":
                fn = nid("failure:" + p, "fail_")
                node(fn, "outcome: failure", "octagon", "#ef4444")
                edge(pn, fn, "led to")
    # layer 2: materialized evolved skills (fixes)
    for p, m in evo.get("materialized", {}).items():
        pn = nid(p, "pat_")
        ev = nid(m["skill"], "evo_")
        node(ev, m["skill"], "hexagon", "#22c55e")
        edge(pn, ev, f"materialized {m['occurrences']}x")
    # layer 3: rejected patterns (not retained — audit trail); rejected may be dict or legacy list
    rejected = evo.get("rejected", {})
    if isinstance(rejected, dict):
        rejected = [dict(v, pattern=k) if isinstance(v, dict) else {"pattern": k} for k, v in rejected.items()]
    for r in rejected:
        rn = nid("rej:" + r["pattern"], "rej_")
        node(rn, r["pattern"] + "\\n(rejected)", "plaintext", "#6b7280")
    # layer 4: predictive metrics
    if pf:
        last = pf[-1]
        mn = nid("metrics", "met_")
        node(mn, f"mem {last['mem_pct']}% / disk {last['disk_pct']}%\\nerr {last['err_now']}/10min", "note", "#a78bfa")
        for p in pat_count:
            edge(nid(p, "pat_"), mn, "monitored for")

    # layer 5: healer actions -> outcomes (module 16/17 feedback edge made
    # visible: which remedy works, which one keeps rolling back)
    hlog = load_jsonl(os.path.join(D, "healer_state.jsonl"))
    act_stats = {}
    for h in hlog:
        a = h.get("action", "?")
        s = act_stats.setdefault(a, {"executed": 0, "rolled_back": 0,
                                     "apply_failed": 0, "other": 0})
        st = h.get("status", "?")
        if st == "EXECUTED":
            s["executed"] += 1
        elif st == "ROLLED_BACK":
            s["rolled_back"] += 1
        elif st == "APPLY_FAILED":
            s["apply_failed"] += 1
        else:
            s["other"] += 1
    for a, s in act_stats.items():
        an = nid("action:" + a, "act_")
        total = sum(s.values())
        ok = s["executed"]
        color = "#22c55e" if ok and ok == total else ("#ef4444" if s["rolled_back"] else "#f59e0b")
        node(an, f"{a}\\n{ok}/{total} ok", "doublecircle", color)
        # edge from the alert rule that triggered the action
        for h in hlog:
            if h.get("action") == a:
                rn = nid("alert:" + h.get("rule", "?"), "alr_")
                node(rn, h.get("rule", "?"), "ellipse", "#38bdf8")
                edge(rn, an, "healed by")
                break
        # edge to the evolved skill if the healer outcome materialized one
        evo_a = evo.get("materialized", {})
        for p, m in evo_a.items():
            if a.lower().replace("_", "-") in p:
                ev = nid(m["skill"], "evo_")
                edge(an, ev, "learned into")

    lines = ["digraph kg {", '  rankdir=LR;', '  bgcolor="#111827";',
             '  node [fontname="Helvetica" fontcolor="#e5e7eb" color="#374151" style=filled fillcolor="#1f2937"];',
             '  edge [color="#4b5563" fontcolor="#9ca3af" fontname="Helvetica" fontsize=10];']
    for n, (label, shape, color) in nodes.items():
        lines.append(f'  {n} [label="{label}" shape={shape} color="{color}"];')
    for a, b, lbl in edges:
        lines.append(f'  {a} -> {b} [label="{lbl}"];')
    lines.append("  labelloc=t; label=\"Hermes Delta Knowledge Graph — root cause → skill → fix\"; }")
    dot_src = "\n".join(lines)
    src_path = os.path.join(D, "kg_latest.dot")
    with open(src_path, "w") as f:
        f.write(dot_src)
    r = subprocess.run(["dot", "-Tsvg", src_path, "-o", SVG], capture_output=True, text=True)
    if r.returncode != 0:
        print(json.dumps({"error": r.stderr})); return 1
    st = load_json(STATE, {"builds": []})
    st["builds"].append({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                         "nodes": len(nodes), "edges": len(edges),
                         "patterns": len(pat_count), "evolved": len(evo.get("materialized", {}))})
    st["builds"] = st["builds"][-50:]
    with open(STATE, "w") as f:
        json.dump(st, f, indent=2)
    print(json.dumps({"nodes": len(nodes), "edges": len(edges),
                      "patterns": len(pat_count),
                      "evolved_fixes": len(evo.get("materialized", {})),
                      "rejected_patterns": len(rejected),
                      "svg": SVG, "svg_bytes": os.path.getsize(SVG)}, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
