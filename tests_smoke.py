import json
import sys
import traceback

from app import graph

Q = [
    "为什么华东区上周销售额降了",
    "上周各渠道销售额排行",
    "近30天销售额趋势",
    "上个月各品类毛利率对比",
    "近30天各渠道投放ROI",
    "上周抖音渠道的客单价是多少",
    "近30天库存周转天数按品类",
    "为什么上个月美妆个护销售额降了",
]

out = []
for q in Q:
    try:
        r = graph.run_once(q, "demo")
        c = r.get("conclusion")
        rf = r.get("reflection")
        rep = r.get("report")
        out.append(
            {
                "q": q,
                "engine": r.get("engine"),
                "plan": json.loads(r["plan"].model_dump_json()) if r.get("plan") else None,
                "sql": (r.get("sql_result") or {}).get("sql"),
                "rows": (r.get("sql_result") or {}).get("row_count"),
                "status": (r.get("sql_result") or {}).get("status"),
                "current": getattr(c, "current", None),
                "delta_pct": getattr(c, "delta_pct", None),
                "drivers": [d.model_dump() for d in getattr(c, "drivers", [])][:3],
                "score": getattr(rf, "score", None),
                "passed": getattr(rf, "passed", None),
                "checks": getattr(rf, "checks", []),
                "grounded": getattr(rep, "grounded", None),
                "ungrounded": getattr(rep, "ungrounded_numbers", None),
                "report": getattr(rep, "markdown", "")[:600],
                "elapsed_ms": r.get("elapsed_ms"),
            }
        )
    except Exception:
        out.append({"q": q, "error": traceback.format_exc()})

with open("D:/zhixi/_smoke.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)

print("done", len(out))
sys.exit(0)
