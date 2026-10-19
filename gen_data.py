"""生成智析演示用的合成业务数据（销售 / 投放 / 库存 + 历史报告语料）。

口径说明（面试可讲）：
- 星型模型：fact_sales / fact_ads / fact_inventory 三张事实表 + dim_product 维表。
- 数据量按演示需要缩到 ~26 万行，保持"千万行"的查询形态（全表扫描代价高，必须走索引）。
- 埋了一个真实可归因的坑：上上周 -> 上周，华东区抖音渠道投放花费砍半，销售额同步下滑。
  这样"为什么华东区上周销售额降了"能跑出有意义的归因结论，而不是随机数。
"""
import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import numpy as np

random.seed(20261016)
np.random.seed(20261016)

OUT = Path(__file__).resolve().parent / "data" / "zhixi.db"
OUT.parent.mkdir(parents=True, exist_ok=True)

DATA_START = date(2025, 1, 1)
DATA_END = date(2026, 10, 15)

REGIONS = ["华东", "华南", "华北", "华中", "西南"]
REGION_W = [0.34, 0.24, 0.19, 0.13, 0.10]
CHANNELS = ["天猫", "京东", "抖音", "线下"]
CHANNEL_W = [0.34, 0.27, 0.24, 0.15]
CATEGORIES = {
    "手机数码": (1599, 6999, 0.18),
    "家用电器": (899, 5299, 0.22),
    "美妆个护": (89, 899, 0.35),
    "食品饮料": (19, 299, 0.28),
    "运动户外": (129, 1599, 0.30),
}
WAREHOUSES = {"华东": "上海仓", "华南": "广州仓", "华北": "天津仓", "华中": "武汉仓", "西南": "成都仓"}


def daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def season_factor(d: date) -> float:
    """季节性：618 / 双11 / 年货节 + 周内波动。"""
    f = 1.0
    if d.month == 11 and 1 <= d.day <= 15:
        f *= 2.15
    if d.month == 6 and 10 <= d.day <= 20:
        f *= 1.75
    if d.month == 1 and 10 <= d.day <= 25:
        f *= 1.35
    if d.month == 12 and 10 <= d.day <= 20:
        f *= 1.25
    # 周内：周末略高
    f *= 1.12 if d.weekday() >= 5 else 1.0
    return f


def main() -> None:
    if OUT.exists():
        OUT.unlink()
    con = sqlite3.connect(OUT)
    cur = con.cursor()
    cur.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE dim_product(
            product_id TEXT PRIMARY KEY,
            product_name TEXT,
            category TEXT,
            sub_category TEXT,
            price REAL,
            unit_cost REAL
        );
        CREATE TABLE fact_sales(
            order_id TEXT, order_date TEXT, region TEXT, channel TEXT,
            category TEXT, product_id TEXT, qty INTEGER,
            amount REAL, cost REAL, customer_id TEXT
        );
        CREATE TABLE fact_ads(
            ad_date TEXT, channel TEXT, region TEXT,
            impressions INTEGER, clicks INTEGER, spend REAL
        );
        CREATE TABLE fact_inventory(
            snap_date TEXT, warehouse TEXT, region TEXT, category TEXT,
            product_id TEXT, stock INTEGER, inbound INTEGER, outbound INTEGER
        );
        """
    )

    # ---------- 维表：商品 ----------
    products = []
    pid = 10000
    for cat, (lo, hi, margin) in CATEGORIES.items():
        for i in range(60):
            pid += 1
            price = round(random.uniform(lo, hi), 2)
            products.append(
                (
                    f"P{pid}",
                    f"{cat}-{['Pro', 'Air', 'Max', 'Lite', 'Plus'][i % 5]}{i + 1}",
                    cat,
                    f"{cat}子类{i % 6 + 1}",
                    price,
                    round(price * (1 - margin), 2),
                )
            )
    cur.executemany("INSERT INTO dim_product VALUES (?,?,?,?,?,?)", products)
    prod_by_cat = {}
    for p in products:
        prod_by_cat.setdefault(p[2], []).append(p)

    # ---------- 事实表：销售 ----------
    sales = []
    order_seq = 0
    # 上周华东抖音的投放塌方（埋点，用于归因演示）
    last_week = (date(2026, 10, 5), date(2026, 10, 11))
    for d in daterange(DATA_START, DATA_END):
        base = int(360 * season_factor(d))
        base = int(base * (1 + 0.22 * (d - DATA_START).days / 650))  # 缓慢增长
        # 周内分布
        for _ in range(base):
            region = random.choices(REGIONS, REGION_W)[0]
            channel = random.choices(CHANNELS, CHANNEL_W)[0]
            cat = random.choices(list(CATEGORIES), [0.26, 0.22, 0.20, 0.20, 0.12])[0]
            prod = random.choice(prod_by_cat[cat])
            qty = random.choices([1, 1, 1, 2, 2, 3], [0.42, 0.2, 0.15, 0.13, 0.06, 0.04])[0]
            discount = random.choice([1.0, 0.95, 0.9, 0.85, 0.8])

            # 埋点：华东 + 抖音 + 上周 -> 量跌 45%
            if region == "华东" and channel == "抖音" and last_week[0] <= d <= last_week[1]:
                if random.random() < 0.45:
                    continue

            amount = round(prod[4] * qty * discount, 2)
            cost = round(prod[5] * qty, 2)
            order_seq += 1
            sales.append(
                (
                    f"SO{d.strftime('%Y%m%d')}{order_seq % 99999:05d}",
                    d.isoformat(),
                    region,
                    channel,
                    cat,
                    prod[0],
                    qty,
                    amount,
                    cost,
                    f"C{random.randint(1, 60000)}",
                )
            )
    cur.executemany("INSERT INTO fact_sales VALUES (?,?,?,?,?,?,?,?,?,?)", sales)

    # ---------- 事实表：投放 ----------
    ads = []
    for d in daterange(DATA_START, DATA_END):
        for region in REGIONS:
            for channel in CHANNELS:
                if channel == "线下":
                    continue
                spend = random.uniform(800, 4200) * season_factor(d) * REGION_W[REGIONS.index(region)] * 3
                if region == "华东" and channel == "抖音" and last_week[0] <= d <= last_week[1]:
                    spend *= 0.45  # 投放塌方
                clicks = int(spend / random.uniform(1.2, 3.2))
                ads.append(
                    (
                        d.isoformat(),
                        channel,
                        region,
                        int(clicks * random.uniform(18, 32)),
                        clicks,
                        round(spend, 2),
                    )
                )
    cur.executemany("INSERT INTO fact_ads VALUES (?,?,?,?,?,?)", ads)

    # ---------- 事实表：库存（周快照） ----------
    inv = []
    d = DATA_START
    while d <= DATA_END:
        if d.weekday() == 0:
            for region in REGIONS:
                for cat in CATEGORIES:
                    for prod in random.sample(prod_by_cat[cat], 6):
                        stock = random.randint(40, 900)
                        outb = random.randint(10, 260)
                        inv.append(
                            (
                                d.isoformat(),
                                WAREHOUSES[region],
                                region,
                                cat,
                                prod[0],
                                stock,
                                random.randint(0, 300),
                                outb,
                            )
                        )
        d += timedelta(days=1)
    cur.executemany("INSERT INTO fact_inventory VALUES (?,?,?,?,?,?,?,?)", inv)

    # ---------- 历史报告语料（800+，用于检索工具） ----------
    cur.executescript(
        """
        CREATE TABLE report_corpus(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT, region TEXT, category TEXT, channel TEXT,
            period TEXT, conclusion TEXT, created_at TEXT
        );
        CREATE VIRTUAL TABLE report_corpus_fts USING fts5(title, conclusion, content='report_corpus', content_rowid='id');
        """
    )
    verbs = ["下滑", "增长", "持平", "波动"]
    reasons = ["投放缩减", "季节性回落", "竞品促销", "渠道结构调整", "客单价提升", "库存周转放缓", "新品上量"]
    rows = []
    for i in range(824):
        region = random.choice(REGIONS)
        cat = random.choice(list(CATEGORIES))
        channel = random.choice(CHANNELS)
        period = f"2026-{random.randint(1, 9):02d}"
        verb = random.choice(verbs)
        reason = random.choice(reasons)
        rows.append(
            (
                f"{period} {region}区{cat}销售{verb}归因分析",
                region,
                cat,
                channel,
                period,
                f"{period} {region}区{cat}在{channel}渠道销售额{verb}，主因是{reason}，"
                f"建议对{channel}渠道的{cat}品类做结构优化，并复核投放预算分配。",
                f"{period}-28 10:00:00",
            )
        )
    cur.executemany(
        "INSERT INTO report_corpus(title,region,category,channel,period,conclusion,created_at) VALUES (?,?,?,?,?,?,?)",
        rows,
    )
    cur.executescript(
        """
        INSERT INTO report_corpus_fts(rowid, title, conclusion)
        SELECT id, title, conclusion FROM report_corpus;

        CREATE TABLE user_pref(
            user_id TEXT, pref_key TEXT, pref_value TEXT,
            updated_at TEXT, PRIMARY KEY(user_id, pref_key)
        );
        CREATE TABLE audit_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT, user_id TEXT, question TEXT, sql TEXT,
            rows_returned INTEGER, elapsed_ms INTEGER, status TEXT
        );
        """
    )

    # 索引：千万行级别必须靠这些索引，否则聚合会走全表扫描
    cur.executescript(
        """
        CREATE INDEX idx_sales_date ON fact_sales(order_date);
        CREATE INDEX idx_sales_region_date ON fact_sales(region, order_date);
        CREATE INDEX idx_sales_channel_date ON fact_sales(channel, order_date);
        CREATE INDEX idx_sales_cat_date ON fact_sales(category, order_date);
        CREATE INDEX idx_ads_date_region ON fact_ads(ad_date, region, channel);
        CREATE INDEX idx_inv_date_region ON fact_inventory(snap_date, region, category);
        """
    )
    con.commit()

    n_sales = cur.execute("SELECT COUNT(*) FROM fact_sales").fetchone()[0]
    total = cur.execute("SELECT ROUND(SUM(amount)/1e8, 2) FROM fact_sales").fetchone()[0]
    print(f"fact_sales rows : {n_sales:,}")
    print(f"fact_ads   rows : {len(ads):,}")
    print(f"fact_inventory : {len(inv):,}")
    print(f"report_corpus  : 824")
    print(f"GMV(亿)        : {total}")
    con.close()


if __name__ == "__main__":
    main()
