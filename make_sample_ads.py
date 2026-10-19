"""造一份"广告投放日报"测试表：结构、列名、业务域都跟门店销售表完全不同。

目的是验证通用引擎不是围着某一张表写死的：
- 列名带前后空格（真实系统导出的样子），入库前会被清洗
- 有一整列全空（备注），入库时整列丢弃
- 有一个高基数列（计划名称），应该被判成标识而不是维度
- 时间是不是 datetime 类型都无所谓，靠列名 + 值格式判断

埋的信号：
1. 6 月信息流-短视频 预算 +45%，但成交金额 -39%（转化崩了）→ 归因题
2. 5/1-5/3 品牌专区大促，单日花费暴涨 → 趋势图有尖峰
3. 联盟推广在西南：花钱不少、几乎零成交 → 停投建议
4. 6 月整体：花费↑、成交金额↓ → "花更多钱赚更少"的核心矛盾
"""
from __future__ import annotations

import random
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

OUT = Path(__file__).resolve().parent / "samples" / "线上广告投放日报_2026年1-6月.csv"
random.seed(20260619)

CHANNELS = {
    # 渠道: (单次点击成本, 点击率, 转化率, 客单价, 单日单区域基础预算)
    "搜索广告":     (2.60, 0.042, 0.038, 430, 1700),
    "信息流-短视频": (1.25, 0.026, 0.011, 268, 1350),
    "品牌专区":     (4.20, 0.019, 0.031, 540, 620),
    "联盟推广":     (0.65, 0.013, 0.004, 185, 450),
    "内容种草":     (0.90, 0.031, 0.016, 320, 380),
}
REGIONS = {"华东": 1.35, "华南": 1.15, "华北": 1.00, "华中": 0.86, "西南": 0.62}
# 西南的联盟推广：预算照投，转化几乎为零（典型的"渠道水土不服"）
BAD_CELL = ("联盟推广", "西南")

d0, d1 = date(2026, 1, 1), date(2026, 6, 30)
rows = []
day = d0
month_idx = 0
while day <= d1:
    if day.month != month_idx:
        month_idx = day.month
    # 季节性：预算逐月 +2%，客单价逐月 +0.4%
    month_budget = 1 + 0.02 * (day.month - 1)
    month_aov = 1 + 0.004 * (day.month - 1)
    weekend = 1.08 if day.weekday() >= 5 else 1.0

    for ch, (cpc, ctr, cvr, aov, base_spend) in CHANNELS.items():
        for rg, rf in REGIONS.items():
            spend = base_spend * rf * month_budget * weekend * random.uniform(0.88, 1.12)
            c_cpc, c_ctr, c_cvr, c_aov = cpc, ctr, cvr, aov * month_aov

            # 信号 1：6 月整体提前加投 3.5%，信息流-短视频在这个基础上再猛加到 +45%，
            # 但转化率腰斩 —— "花更多钱、赚更少钱"的典型。
            if day.month == 6:
                spend *= 1.035
                if ch == "信息流-短视频":
                    spend *= 1.40
                    c_cvr *= 0.40
            # 信号 2：三八节品牌大促（3 天集中曝光），转化率不升反降
            if ch == "品牌专区" and date(2026, 3, 8) <= day <= date(2026, 3, 10):
                spend *= 9.0
                c_cvr *= 0.92
            # 信号 3：西南的联盟推广，钱照花，几乎不转化
            if (ch, rg) == BAD_CELL:
                spend *= 1.5
                c_cvr *= 0.12

            clicks = max(1, int(spend / c_cpc * random.uniform(0.92, 1.08)))
            impressions = int(clicks / c_ctr * random.uniform(0.95, 1.05))
            orders = max(0, int(round(clicks * c_cvr * random.uniform(0.85, 1.15))))
            gmv = orders * c_aov * random.uniform(0.9, 1.12)

            rows.append(
                {
                    "报表日期": day.isoformat(),
                    "广告渠道": ch,
                    "投放区域": rg,
                    "计划名称": f"{ch[:2]}-{rg}-{abs(hash((ch, rg))) % 900 + 100}",
                    "曝光量": impressions,
                    "点击量": clicks,
                    "投放花费": round(spend, 2),
                    "成交订单数": orders,
                    "成交金额": round(gmv, 2),
                    "备注": "",  # 全空列，测试自动丢弃
                }
            )
    day += timedelta(days=1)

df = pd.DataFrame(rows)
df = df.sort_values(["报表日期", "广告渠道", "投放区域"]).reset_index(drop=True)
# 真实导出必然带的流水号：每行唯一，用来验证「标识列不会被拿去分组」
df.insert(0, "投放流水号", [f"AD{d.replace('-', '')}{i + 1:06d}" for i, d in enumerate(df["报表日期"])])
# 故意把列名弄脏：导出系统常见的"前后空格"
df.columns = [f" {c} " if c in ("广告渠道", "投放区域", "投放花费", "成交金额") else c for c in df.columns]
OUT.parent.mkdir(parents=True, exist_ok=True)
df.to_csv(OUT, index=False, encoding="utf-8-sig")

print(f"写出 {OUT}")
print(f"行数 {len(df):,}  列数 {df.shape[1]}")
print(df.head(3).to_string())
print("\n--- 关键校验（按渠道汇总成交金额）---")
g = df.groupby(" 广告渠道 ")[" 成交金额 "].sum().sort_values(ascending=False)
print((g / 1e4).round(1).to_string())
print("\n--- 6 月 vs 5 月 ---")
df["_m"] = df["报表日期"].str[:7]
for m in ("2026-05", "2026-06"):
    sub = df[df["_m"] == m]
    print(m, "花费", round(sub[" 投放花费 "].sum() / 1e4, 1), "万 / 成交", round(sub[" 成交金额 "].sum() / 1e4, 1), "万")
sub5 = df[df["_m"] == "2026-05"].groupby(" 广告渠道 ")[[" 投放花费 ", " 成交金额 "]].sum()
sub6 = df[df["_m"] == "2026-06"].groupby(" 广告渠道 ")[[" 投放花费 ", " 成交金额 "]].sum()
print("\n渠道维度 6月 vs 5月（万元）")
print(pd.DataFrame({"花费5": sub5[" 投放花费 "] / 1e4, "花费6": sub6[" 投放花费 "] / 1e4,
                    "成交5": sub5[" 成交金额 "] / 1e4, "成交6": sub6[" 成交金额 "] / 1e4}).round(1).to_string())
