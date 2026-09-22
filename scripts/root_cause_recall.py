#!/usr/bin/env python3
"""Root-Cause Recall — hermes-delta module 11.
Symptom-indexed retrieval of past root causes across sessions.
Index: symptom tokens -> {pattern, skill, outcome, citations} from the
experience store + delta_report history. Query: python3 root_cause_recall.py "symptom words..."
Returns ranked matches with citations (file:line). This is the retrieval
half of the 'cross-session learning' claim — nothing else in the pipeline
recalls past root causes when a new investigation starts.
State: recall_index.json (rebuilt each delta_run; cheap)."""
import json, os, re, sys, time
from collections import defaultdict

D = os.path.dirname(os.path.abspath(__file__))
EXP = os.path.expanduser("~/.hermes/skills/agent-self-learning/experience.jsonl")
HIST = os.path.join(D, "delta_report_history.jsonl")
INDEX = os.path.join(D, "recall_index.json")

STOP = set("the a an of in on for to with and or is was were be been at by from this that it its as".split())

def load_jsonl(p):
    if not os.path.exists(p):
        return []
    out = []
    with open(p) as f:
        for i, l in enumerate(f, 1):
            l = l.strip()
            if not l:
                continue
            try:
                out.append((i, json.loads(l)))
            except json.JSONDecodeError:
                pass
    return out

def tokens(text):
    # sub-tokens (split hyphens) + full hyphenated forms, so "async race" matches
    # "async-race-condition" and exact queries match too
    words = re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", text.lower())
    out = set()
    for w in words:
        if w in STOP or len(w) < 3:
            continue
        out.add(w)
        for part in w.split("-"):
            if len(part) >= 3 and part not in STOP:
                out.add(part)
    return out

def build():
    """Rebuild the symptom index. Every entry keeps its citation."""
    index = defaultdict(list)  # token -> [(pattern, citation, outcome)]
    for ln, e in load_jsonl(EXP):
        cite = f"experience.jsonl:{ln}"
        # module 18 quarantine: retracted beliefs never enter the recall
        # index — a retracted root cause must not be re-proposed
        if e.get("outcome") == "retracted" or str(e.get("root_cause", "")).startswith("[RETRACTED"):
            continue
        # schema A: patterns_found/skill/task_type (self-learning entries)
        toks = tokens(" ".join(e.get("patterns_found", [])) + " " + e.get("skill", "") + " " + e.get("task_type", "") + " " + " ".join(e.get("corrections", [])))
        for p in e.get("patterns_found", []):
            for t in tokens(p + " " + e.get("skill", "")):
                index[t].append((p, cite, e.get("outcome", "?")))
        # schema B: type=root_cause entries (symptoms + root_cause + fix)
        if e.get("type") == "root_cause" or "symptoms" in e:
            symptoms = e.get("symptoms", [])
            rc = e.get("root_cause", "")
            for t in tokens(" ".join(symptoms) + " " + rc):
                index[t].append((rc[:80], cite, e.get("fix", "?")))
    for ln, h in load_jsonl(HIST):
        cite = f"delta_report_history.jsonl:{ln}"
        if h.get("predict", {}).get("anomaly"):
            for t in tokens("predicted anomaly failure memory disk error"):
                index[t].append(("predicted-anomaly", cite, "anomaly"))
        for d in h.get("evoskill", {}).get("decisions", []):
            if d.get("decision") == "ALREADY_MATERIALIZED":
                for t in tokens(d["pattern"]):
                    index[t].append((d["pattern"], cite, "recurrent"))
    out = {t: v for t, v in index.items()}
    with open(INDEX, "w") as f:
        json.dump({"built": time.strftime("%Y-%m-%dT%H:%M:%S"), "tokens": len(out), "index": out}, f)
    return out

def query(q, index):
    qt = tokens(q)
    scores = defaultdict(lambda: {"score": 0.0, "cites": set(), "outcomes": []})
    for t in qt:
        for pat, cite, outcome in index.get(t, []):
            s = scores[pat]
            s["score"] += 1.0 / len(index[t]) ** 0.5  # idf-ish: rare tokens weigh more
            s["cites"].add(cite)
            s["outcomes"].append(outcome)
    ranked = sorted(scores.items(), key=lambda kv: -kv[1]["score"])[:5]
    return [{"pattern": p, "score": round(v["score"], 3),
             "citations": sorted(v["cites"])[:3],
             "seen_outcomes": sorted(set(v["outcomes"]))} for p, v in ranked]

def main():
    if len(sys.argv) > 1 and sys.argv[1] != "--build":
        # query mode: use existing index if fresh, else rebuild
        index = json.load(open(INDEX))["index"] if os.path.exists(INDEX) else build()
        print(json.dumps(query(" ".join(sys.argv[1:]), index), indent=2, ensure_ascii=False))
        return 0
    index = build()
    print(json.dumps({"tokens": len(index), "index": INDEX}, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
