"""生成一份"看起来像真公司导出"的连锁咖啡门店销售明细，用于压力测试。

埋了 4 个只有认真做分析才能挖出来的信号（也是验收标准答案）：
1. 2026-07 起华南区「外卖」渠道订单断崖式消失（模拟外卖平台补贴退坡、配送费上调），
   华南 7 月销售额逆季节性下跌约 25%，而其他区域同期在涨 —— 归因题答案：主因是外卖渠道。
2. 2026-08-18 深圳南山店团购大单（当日销售额约为常态 8 倍）—— 异常点。
3. 季节性：劳动节（5/1~5/5）+60%、周末 +25%、夏季现制饮品 +20%。
4. 约 0.8% 退货行（数量与销售额为负）—— 数据质量噪声。

输出：samples/连锁门店销售明细_2026年1-9月.csv（UTF-8-SIG）与同名 .xlsx
"""
from __future__ import annotations

import random
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

random.seed(20261018)

OUT_DIR = Path(__file__).resolve().parent / "samples"
OUT_DIR.mkdir(parents=True, exist_ok=True)

START, END = date(2026, 1, 1), date(2026, 9, 30)

NETWORK = {
    "华东": {"上海": ["上海静安店", "上海徐汇店"], "杭州": ["杭州西湖店"]},
    "华南": {"深圳": ["深圳南山店", "深圳福田店"], "广州": ["广州天河店"]},
    "华北": {"北京": ["北京国贸店", "北京三里屯店"]},
    "华中": {"武汉": ["武汉光谷店"], "长沙": ["长沙五一店"]},
    "西南": {"成都": ["成都春熙店"], "重庆": ["重庆解放碑店"]},
}

CHANNELS = [("门店自提", 0.45), ("外卖", 0.35), ("小程序", 0.20)]
CATEGORIES = {
    "现制饮品": ((18, 38), 0.60, 0.62),
    "烘焙": ((12, 28), 0.20, 0.55),
    "周边": ((39, 99), 0.10, 0.45),
    "咖啡豆": ((58, 128), 0.10, 0.38),
}
PRODUCTS = {
    "现制饮品": ["生椰拿铁", "美式咖啡", "抹茶拿铁", "香草冷萃", "杨枝甘露", "茉莉奶绿"],
    "烘焙": ["芝士蛋糕", "可颂", "提拉米苏", "贝果"],
    "周边": ["随行杯", "帆布袋", "挂耳礼盒"],
    "咖啡豆": ["埃塞俄比亚豆", "哥伦比亚豆", "意式拼配豆"],
}

stores = [(region, city, s) for region, cities in NETWORK.items() for city, names in cities.items() for s in names]


def seasonal(d: date, category: str) -> float:
    f = 1.0
    if d.weekday() >= 5:
        f *= 1.25
    if date(2026, 5, 1) <= d <= date(2026, 5, 5):
        f *= 1.6
    if d.month in (6, 7, 8):
        f *= 1.20 if category == "现制饮品" else 1.05
    if d.month in (1, 2):
        f *= 1.10 if category == "烘焙" else 0.95
    return f


def pick(weighted):
    r = random.random()
    acc = 0.0
    for item, w in weighted:
        acc += w
        if r <= acc:
            return item
    return weighted[-1][0]


rows = []
seq = 0
d = START
while d <= END:
    ds = d.isoformat()
    for region, city, store in stores:
        n_lines = 50 if d.weekday() < 5 else 62
        n_lines = int(n_lines * random.uniform(0.9, 1.1))
        for _ in range(n_lines):
            category = pick([(c, v[1]) for c, v in CATEGORIES.items()])
            product = random.choice(PRODUCTS[category])
            channel = pick(CHANNELS)
            lo, hi = CATEGORIES[category][0]
            unit = round(random.uniform(lo, hi), 1)

            qty = random.choices([1, 2, 3, 4, 6, 8], weights=[0.40, 0.28, 0.16, 0.08, 0.05, 0.03])[0]
            qty = max(1, int(round(qty * seasonal(d, category))))

            # 信号1：华南区外卖渠道 7 月起大面积流失（平台补贴退坡 → 配送费上涨）
            if region == "华南" and channel == "外卖" and d >= date(2026, 7, 1):
                if random.random() < 0.65:
                    continue          # 订单直接消失
                qty = max(1, int(qty * 0.6))  # 剩下的也缩量

            # 信号2：8/18 深圳南山店团购大单
            if store == "深圳南山店" and d == date(2026, 8, 18):
                qty *= 8

            # 信号4：退货
            if random.random() < 0.008:
                qty = -qty

            amount = round(qty * unit, 2)
            margin = CATEGORIES[category][2] * random.uniform(0.92, 1.08)
            cost = round(amount * (1 - margin), 2)

            seq += 1
            rows.append(
                {
                    "订单号": f"SO{ds.replace('-', '')[2:]}{seq:06d}",
                    "日期": ds,
                    "区域": region,
                    "城市": city,
                    "门店": store,
                    "渠道": channel,
                    "品类": category,
                    "商品": product,
                    "数量": qty,
                    "单价": unit,
                    "销售额": amount,
                    "成本": cost,
                    "毛利": round(amount - cost, 2),
                }
            )
    d += timedelta(days=1)

df = pd.DataFrame(rows)
# 真实导出常见的缺失：少量门店字段为空
mask = df.sample(frac=0.003, random_state=7).index
df.loc[mask, "门店"] = None

csv_path = OUT_DIR / "连锁门店销售明细_2026年1-9月.csv"
xlsx_path = OUT_DIR / "连锁门店销售明细_2026年1-9月.xlsx"
df.to_csv(csv_path, index=False, encoding="utf-8-sig")
df.to_excel(xlsx_path, index=False)
print(f"rows={len(df)} cols={len(df.columns)}")
print("csv :", csv_path)
print("xlsx:", xlsx_path)
