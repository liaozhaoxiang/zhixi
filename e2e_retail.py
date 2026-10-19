"""端到端验证：走 SSE 全链路问两个问题，把最终报告落到文件里人工核对。"""
import json
import sys
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8010"
QUESTIONS = [
    "2011年11月总销售额多少？和10月比怎么样",
    "2011年11月哪个国家卖得最好",
    "上个月销售额多少，环比呢",
]

out_path = sys.argv[1] if len(sys.argv) > 1 else "logs/e2e_retail.log"
lines = []
for q in QUESTIONS:
    url = BASE + "/api/analyze/stream?question=" + urllib.parse.quote(q)
    lines.append(f"===== Q: {q}")
    events = []
    with urllib.request.urlopen(url, timeout=180) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").rstrip("\n")
            if line.startswith("data:"):
                try:
                    ev = json.loads(line[5:].strip())
                    ev["_raw"] = line[5:].strip()
                    events.append(ev)
                except Exception:
                    pass
    final = None
    for ev in events:
        d = ev.get("data") or {}
        if isinstance(d, dict) and d.get("report"):
            final = d
        if ev.get("type") == "error":
            lines.append(f"EVENT-ERROR: {ev.get('message')}")
    if final:
        rep = final["report"]
        lines.append(f"has_report={bool(rep)} len={len(rep)}")
        lines.append(rep)
    else:
        lines.append("NO FINAL EVENT; last 5 events:")
        lines.extend(e["_raw"] for e in events[-5:])

with open(out_path, "w", encoding="utf-8") as f:
    f.write("\n".join(str(x) for x in lines))
print("written", out_path)
