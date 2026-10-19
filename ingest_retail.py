"""灌入一份真实公开数据集：UCI Machine Learning Repository · Online Retail。

这是机器学习领域最经典的公开电商数据集之一：一家英国在线零售商
2010-12-01 ~ 2011-12-09 的真实交易流水，共 541,909 行。来源链接真实可点击。

清洗口径（面试可讲）：
- 剔除取消单（InvoiceNo 以 C 开头）与退货（Quantity<=0）；
- 剔除单价<=0 的行（赠品/调整项，不构成销售额）；
- 销售额 = 数量 × 单价，逐行计算；
- datasets.py 的 MAX_ROWS=200_000 是上传安全上限，为不破坏它，
  从最近月份往前凑一个 <= 190,000 行的连续自然月窗口（月边界干净，环比好算）。
"""
from __future__ import annotations

import io
from pathlib import Path

import pandas as pd

from app import datasets

XLSX = Path(__file__).resolve().parent / "uci_tmp" / "Online Retail.xlsx"
SOURCE_NAME = "UCI Machine Learning Repository · Online Retail（英国在线零售商真实交易流水）"
SOURCE_URL = "https://archive.ics.uci.edu/dataset/352/online+retail"
BUDGET_ROWS = 190_000


def main() -> None:
    print("reading xlsx ...")
    df = pd.read_excel(XLSX)
    print(f"raw rows: {len(df)}")

    inv = df["InvoiceNo"].astype(str).str.strip()
    df = df[~inv.str.upper().str.startswith("C")]
    df = df[(df["Quantity"] > 0) & (df["UnitPrice"] > 0)]
    df = df[df["Description"].notna()]
    print(f"after clean: {len(df)}")

    out = pd.DataFrame(
        {
            "下单时间": df["InvoiceDate"].dt.strftime("%Y-%m-%d %H:%M:%S"),
            "国家": df["Country"].astype(str).str.strip(),
            "商品名称": df["Description"].astype(str).str.strip(),
            "数量": df["Quantity"].astype(float),
            "单价": df["UnitPrice"].astype(float),
            "销售额": (df["Quantity"] * df["UnitPrice"]).round(2),
        }
    )

    out["月"] = out["下单时间"].str[:7]
    monthly = out["月"].value_counts().sort_index()
    picked: list[str] = []
    total = 0
    for m in reversed(monthly.index):
        if total + int(monthly[m]) > BUDGET_ROWS:
            break
        picked.append(m)
        total += int(monthly[m])
    picked = sorted(picked)
    print(f"picked months: {picked} (rows={total})")

    sub = out[out["月"].isin(picked)].drop(columns="月").reset_index(drop=True)
    buf = io.BytesIO()
    sub.to_csv(buf, index=False, encoding="utf-8-sig")
    name = f"英国在线零售真实交易_{picked[0]}_至_{picked[-1]}.csv"
    item = datasets.save_upload(
        name,
        buf.getvalue(),
        source_url=SOURCE_URL,
        source_name=SOURCE_NAME,
    )
    print(f"ingested: {item['name']} | table={item['table']} | rows={item['rows']}")

    # 清掉压测残留的假来源数据集（example.com 占位链接）
    fake = [d["id"] for d in datasets.list_datasets() if "example.com" in (d.get("source_url") or "")]
    for ds_id in fake:
        datasets.drop(ds_id)
        print(f"dropped fake dataset: {ds_id}")

    # 真实交易数据设为激活，报告的「数据来源」从此有真链接
    datasets.set_active(item["id"])
    print("active:", item["id"])
    for d in datasets.list_datasets():
        print(f"  - {d['id']} {d['name']} rows={d['rows']} src={d.get('source_url') or '(无)'}")


if __name__ == "__main__":
    main()
