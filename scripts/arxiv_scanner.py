#!/usr/bin/env python3
"""ArXiv Scanner — hermes-delta module 13: autonomous literature radar.
Each delta_run: query arXiv for standing research interests (self-improving agents,
skill learning, autonomous debugging, failure forecasting). Dedupes against state,
records new papers as [arxiv:ID] evidence lines. Feeds the knowledge graph.
State: scripts/arxiv_state.json. Exit 0 always (new papers are opportunity, not anomaly)."""
import json, os, re, sys, time, urllib.request, urllib.parse
import xml.etree.ElementTree as ET

D = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(D, "arxiv_state.json")
NS = {'a': 'http://www.w3.org/2005/Atom'}
# standing interests — precise title-scoped queries (arXiv all: is too loose, verified 2026-09-22)
INTERESTS = [
    'ti:"self-improving agent"',
    'ti:"skill learning" AND ti:agent',
    'ti:"self-evolving" AND ti:agent',
    'abs:"failure prediction" AND cat:cs.AI',
    'abs:"root cause" AND abs:LLM',
]
MAX_PER_QUERY = 5

def load_state():
    if os.path.exists(STATE):
        with open(STATE) as f:
            return json.load(f)
    return {"seen": {}, "papers": []}

def save_state(st):
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, STATE)

def fetch(query):
    # query already carries its own field prefix (ti:/abs:/cat:) — no all: wrapper
    params = urllib.parse.urlencode({
        "search_query": query, "max_results": str(MAX_PER_QUERY),
        "sortBy": "submittedDate", "sortOrder": "descending"})
    url = "https://export.arxiv.org/api/query?" + params
    req = urllib.request.Request(url, headers={"User-Agent": "HermesDelta/13.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return ET.fromstring(r.read())
    except Exception as e:
        return {"error": str(e)}

def parse_entries(root):
    out = []
    if not isinstance(root, ET.Element):
        return out
    for e in root.findall('a:entry', NS):
        aid = (e.findtext('a:id', '', NS) or '').rsplit('/', 1)[-1]
        aid = re.sub(r'v\d+$', '', aid)
        title = re.sub(r'\s+', ' ', e.findtext('a:title', '', NS)).strip()
        pub = e.findtext('a:published', '', NS)[:10]
        if aid and title:
            out.append({"id": aid, "title": title, "published": pub})
    return out

def relevant(title, interest):
    """arXiv all: search is loose (OR-ish). Require >=2 significant interest
    words in the title to cut false positives like 'Affine Volterra covariance
    processes' matching 'agent skill learning memory'."""
    STOP = {"llm", "agent", "learning", "with", "for", "of", "and", "using"}
    words = {w for w in re.findall(r"[a-z]+", interest.lower()) if len(w) > 2 and w not in STOP}
    t = title.lower()
    hits = sum(1 for w in words if w in t)
    return hits >= min(2, len(words)) if words else False

def main():
    st = load_state()
    new_papers, errors = [], []
    for q in INTERESTS:
        root = fetch(q)
        for p in parse_entries(root):
            if p["id"] not in st["seen"]:
                # mark seen regardless of relevance (skip forever), but only
                # surface papers that actually match the interest
                st["seen"][p["id"]] = {"title": p["title"], "first_seen": time.strftime("%Y-%m-%d")}
                if relevant(p["title"], q):
                    p["interest"] = q
                    new_papers.append(p)
        if isinstance(root, dict):
            errors.append(f"{q}: {root.get('error')}")
        time.sleep(1)  # arXiv API etiquette
    st["papers"] = (new_papers + st["papers"])[:200]
    st["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    save_state(st)
    out = {"queries": len(INTERESTS), "new_papers": len(new_papers),
           "total_seen": len(st["seen"]),
           "evidence": [f'[arxiv:{p["id"]}] "{p["title"]}" ({p["published"]}) <- {p["interest"]}'
                        for p in new_papers[:10]],
           "errors": errors}
    print(json.dumps(out, indent=2))
    return 0

if __name__ == "__main__":
    sys.exit(main())
