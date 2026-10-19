"""把伪造的门店明细传进去，按 6 个问题跑一遍，把报告落到文件里人工核对。"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from pathlib import Path

import requests

BASE = "http://127.0.0.1:8010"
CSV = Path("samples/连锁门店销售明细_2026年1-9月.csv")
SRC_NAME = "模拟数据 · 云栖咖啡连锁 12 店销售明细（脚本合成，非真实业务）"

QUESTIONS = [
    "2026年7月总销售额多少？和6月比怎么样",
    "为什么华南区7月销售额下降了",
    "2026年8月各品类销售额排行",
    "华东区2026年8月哪个门店卖得最好",
    "2026年8月毛利是多少",
    "2026年销售额趋势",
]


def upload() -> dict:
    # 已经传过就复用（避免每跑一次多一份重复数据集）
    meta = requests.get(BASE + "/api/datasets", timeout=30).json()
    for d in meta["items"]:
        if d["name"] == CSV.name:
            requests.post(BASE + f"/api/datasets/{d['id']}/activate", timeout=30)
            return d
    with CSV.open("rb") as f:
        r = requests.post(
            BASE + "/api/upload",
            files={"file": (CSV.name, f, "text/csv")},
            data={"source_name": SRC_NAME},
            timeout=300,
        )
    r.raise_for_status()
    return r.json()


def ask(q: str) -> tuple[str, list[dict]]:
    url = BASE + "/api/analyze/stream?question=" + urllib.parse.quote(q)
    events = []
    with urllib.request.urlopen(url, timeout=300) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("data:"):
                try:
                    events.append(json.loads(line[5:].strip()))
                except Exception:
                    pass
    report = ""
    for ev in events:
        d = ev.get("data") or {}
        if isinstance(d, dict) and d.get("report"):
            report = d["report"].get("markdown", "") if isinstance(d["report"], dict) else str(d["report"])
    return report, events


def main() -> None:
    item = upload()
    print("uploaded:", item["id"], item["rows"], "rows |", item["table"])
    print("profile roles:", [(p["name"], p["role"]) for p in item["profile"]])

    out = [f"dataset={item['id']} rows={item['rows']}"]
    for q in QUESTIONS:
        rep, _ = ask(q)
        out.append("\n" + "=" * 70 + f"\nQ: {q}\n{'-' * 70}\n{rep}")
        print("done:", q, "| len", len(rep))

    Path("logs/e2e_sample.log").write_text("\n".join(out), encoding="utf-8")
    print("written logs/e2e_sample.log")


if __name__ == "__main__":
    main()
