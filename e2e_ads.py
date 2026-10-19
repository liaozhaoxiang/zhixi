"""端到端验证：新上传的广告投放表在通用引擎下能不能正常回答。"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8010"


def op(path: str, data: dict | None = None, timeout: int = 120):
    # 本地服务必须绕开系统代理，否则 Clash 的失效代理会让请求直接失败
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    if data is None:
        req = urllib.request.Request(BASE + path)
    else:
        req = urllib.request.Request(BASE + path, data=json.dumps(data).encode(), headers={"Content-Type": "application/json"})
    with opener.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


QS = [
    "2026年6月成交金额是多少？和5月比怎么样",
    "为什么6月成交金额下降了",
    "各广告渠道的投放花费排行",
    "2026年成交金额趋势",
    "2026年曝光量最高的是哪个渠道",
]

for q in QS:
    out = op("/api/analyze", {"question": q})
    plan = out.get("plan") or {}
    conc = out.get("conclusion") or {}
    md = ((out.get("report") or {}).get("markdown") or "").strip()
    print("=" * 78)
    print("问：", q)
    print(f"  意图={plan.get('intent')} 指标={plan.get('metric_col')} 聚合={plan.get('agg')} 维度={plan.get('dimensions')} "
          f"区间={plan.get('time_range', {}).get('label')} 对比={plan.get('compare', {}).get('label')}")
    print(f"  总量={conc.get('current_display')} 上期={conc.get('previous') and round(conc['previous'], 2)} "
          f"变化={conc.get('delta_pct') and round(conc['delta_pct'], 2)}%")
    print("  驱动:", [(d.get('name'), round(d.get('delta') or 0, 1), d.get('share')) for d in (conc.get('drivers') or [])][:4])
    print("  报告首行:", md.split("\n")[0][:180])
    print("  facts:", len(conc.get("facts") or []), "| 图表:",
          ((out.get("report") or {}).get("chart") or {}).get("type"),
          len(((out.get("report") or {}).get("chart") or {}).get("points") or []))
