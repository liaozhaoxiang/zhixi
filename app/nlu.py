"""本地规则规划器：把中文问题解析成 Plan。

定位：**离线兜底**。有 LLM 时由 planner.py 走模型规划，这套规则只在没有 key
（面试现场断网、公司内网）时接管，保证主链路永远能跑通。
解析不出来的部分不硬猜 —— 填 clarify 字段，让主管 Agent 反问用户。
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from . import datasets
from .db import data_bounds
from .schemas import CompareSpec, FilterItem, Plan, TimeRange

REGIONS = ["华东", "华南", "华北", "华中", "西南"]
CHANNELS = ["天猫", "京东", "抖音", "线下"]
CATEGORIES = ["手机数码", "家用电器", "美妆个护", "食品饮料", "运动户外"]

METRIC_WORDS: list[tuple[str, str]] = [
    ("毛利率", "margin"),
    ("利润率", "margin"),
    ("客单价", "aov"),
    ("单均", "aov"),
    ("投入产出", "roi"),
    ("投产比", "roi"),
    ("roi", "roi"),
    ("周转", "turnover"),
    ("库存", "stock"),
    ("投放费用", "spend"),
    ("投放花费", "spend"),
    ("广告费", "spend"),
    ("投放", "spend"),
    ("预算", "spend"),
    ("花费", "spend"),
    ("点击量", "clicks"),
    ("点击", "clicks"),
    ("客户数", "customers"),
    ("买家数", "customers"),
    ("用户数", "customers"),
    ("订单量", "orders"),
    ("订单数", "orders"),
    ("单量", "orders"),
    ("销量", "qty"),
    ("件数", "qty"),
    ("毛利", "profit"),
    ("利润", "profit"),
    ("gmv", "gmv"),
    ("销售额", "gmv"),
    ("营业额", "gmv"),
    ("营收", "gmv"),
    ("收入", "gmv"),
    ("业绩", "gmv"),
]

DIM_WORDS: list[tuple[str, str]] = [
    ("大区", "region"),
    ("区域", "region"),
    ("地区", "region"),
    ("渠道", "channel"),
    ("品类", "category"),
    ("类目", "category"),
    ("商品", "product"),
    ("单品", "product"),
    ("sku", "product"),
    ("仓库", "warehouse"),
    ("月度", "month"),
    ("每月", "month"),
    ("按月", "month"),
    ("每天", "day"),
    ("每日", "day"),
    ("按天", "day"),
]


def _month_range(year: int, month: int) -> tuple[date, date]:
    start = date(year, month, 1)
    nxt = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    return start, nxt - timedelta(days=1)


def _shift_months(d: date, delta: int) -> date:
    y, m = d.year, d.month + delta
    while m <= 0:
        y, m = y - 1, m + 12
    while m > 12:
        y, m = y + 1, m - 12
    return date(y, m, 1)


def _prev_period(start: date, end: date) -> tuple[date, date]:
    days = (end - start).days + 1
    return start - timedelta(days=days), start - timedelta(days=1)


def _prev_window(start: date, end: date) -> tuple[date, date]:
    """环比对比区间：整自然月对齐上一个自然月，其余情况等长前移。

    7 月（31 天）如果按"等长 31 天"往前截，会截到 5/31~6/30，把 5 月末的高峰带进来，
    算出来的环比跟"6 月"对不上，业务上也没法解释。
    """
    if start.day == 1 and (end + timedelta(days=1)).day == 1:
        y, m = _shift_months(start, -1).timetuple()[:2]
        return _month_range(y, m)
    return _prev_period(start, end)


def _yoy(start: date, end: date) -> tuple[date, date]:
    try:
        return start.replace(year=start.year - 1), end.replace(year=end.year - 1)
    except ValueError:  # 2/29
        return start.replace(year=start.year - 1, day=28), end.replace(year=end.year - 1, day=28)


def resolve_time(q: str, anchor: date | None = None) -> tuple[TimeRange, CompareSpec]:
    if anchor is None:
        # 锚点优先用当前激活数据集的时间覆盖，这样「上个月/上周」对上传数据同样成立；
        # 没有激活数据集时才退回内置库边界
        ds = datasets.active()
        if ds:
            lo, hi = datasets.value_range(ds)
            anchor = date.fromisoformat(hi) if hi else date.today()
        else:
            _, hi = data_bounds()
            anchor = date.fromisoformat(hi)

    this_week_start = anchor - timedelta(days=anchor.weekday())
    last_week_start = this_week_start - timedelta(days=7)

    def week(s: date, label: str) -> TimeRange:
        return TimeRange(label=label, start=s.isoformat(), end=(s + timedelta(days=6)).isoformat())

    tr: TimeRange | None = None
    if "上上周" in q or "前一周" in q:
        tr = week(last_week_start - timedelta(days=7), "上上周")
    elif "上周" in q or "上个星期" in q:
        tr = week(last_week_start, "上周")
    elif "本周" in q or "这周" in q or "这个星期" in q:
        tr = TimeRange(label="本周", start=this_week_start.isoformat(), end=anchor.isoformat())
    elif "昨天" in q:
        tr = TimeRange(label="昨天", start=(anchor - timedelta(days=1)).isoformat(), end=(anchor - timedelta(days=1)).isoformat())
    elif "今天" in q:
        tr = TimeRange(label="今天", start=anchor.isoformat(), end=anchor.isoformat())
    elif "上个月" in q or "上月" in q:
        s, e = _month_range(*_shift_months(anchor.replace(day=1), -1).timetuple()[:2])
        tr = TimeRange(label="上月", start=s.isoformat(), end=e.isoformat())
    elif "本月" in q or "这个月" in q:
        s, _ = _month_range(anchor.year, anchor.month)
        tr = TimeRange(label="本月", start=s.isoformat(), end=anchor.isoformat())
    elif m := re.search(r"近\s*(\d+)\s*天", q):
        n = max(1, min(180, int(m.group(1))))
        tr = TimeRange(label=f"近{n}天", start=(anchor - timedelta(days=n - 1)).isoformat(), end=anchor.isoformat())
    elif "近三个月" in q or "近3个月" in q:
        tr = TimeRange(label="近三个月", start=_shift_months(anchor.replace(day=1), -3).isoformat(), end=anchor.isoformat())
    elif "去年" in q:
        s, e = date(anchor.year - 1, 1, 1), date(anchor.year - 1, 12, 31)
        tr = TimeRange(label="去年", start=s.isoformat(), end=e.isoformat())
    elif "今年" in q:
        tr = TimeRange(label="今年", start=date(anchor.year, 1, 1).isoformat(), end=anchor.isoformat())
    elif m := re.search(r"(20\d{2})\s*年(?:(\d{1,2})\s*月(?:(\d{1,2})\s*[日号])?)?", q):
        # 「2026年」「2026年9月」「2026年8月18日」这类显式日期表达
        year = int(m.group(1))
        if m.group(2):
            month = min(12, max(1, int(m.group(2))))
            if m.group(3) and 1 <= int(m.group(3)) <= 31:
                try:
                    day = date(year, month, int(m.group(3)))
                    tr = TimeRange(label=f"{year}年{month}月{day.day}日", start=day.isoformat(), end=day.isoformat())
                except ValueError:
                    s, e = _month_range(year, month)
                    tr = TimeRange(label=f"{year}年{month}月", start=s.isoformat(), end=e.isoformat())
            else:
                s, e = _month_range(year, month)
                tr = TimeRange(label=f"{year}年{month}月", start=s.isoformat(), end=e.isoformat())
        else:
            tr = TimeRange(label=f"{year}年", start=date(year, 1, 1).isoformat(), end=date(year, 12, 31).isoformat())
    elif m := re.search(r"(\d{1,2})\s*月(?:(\d{1,2})\s*[日号])?", q):
        month = int(m.group(1))
        if 1 <= month <= 12:
            year = anchor.year if month <= anchor.month else anchor.year - 1
            if m.group(2) and 1 <= int(m.group(2)) <= 31:
                try:
                    day = date(year, month, int(m.group(2)))
                    tr = TimeRange(label=f"{year}年{month}月{day.day}日", start=day.isoformat(), end=day.isoformat())
                except ValueError:
                    s, e = _month_range(year, month)
                    tr = TimeRange(label=f"{year}年{month}月", start=s.isoformat(), end=e.isoformat())
            else:
                s, e = _month_range(year, month)
                tr = TimeRange(label=f"{year}年{month}月", start=s.isoformat(), end=e.isoformat())

    if tr is None:
        tr = TimeRange(label="近30天", start=(anchor - timedelta(days=29)).isoformat(), end=anchor.isoformat())

    s, e = date.fromisoformat(tr.start), date.fromisoformat(tr.end)
    cmp = CompareSpec()
    # 「和10月比」「跟2011年10月相比」「比2011年10月增长」这类显式指定对比期的句式，优先级最高。
    # 只在主期表达之后的部分里找对比期，避免把主期自己（"11月比…"）误认成对比目标。
    prim = re.search(r"(?:20\d{2}\s*年)?\d{1,2}\s*月", q)
    tail = q[prim.end():] if prim else q
    m2 = re.search(r"[和跟与较比]\s*(?:(20\d{2})\s*年)?(\d{1,2})\s*月", tail) or re.search(
        r"(?:(20\d{2})\s*年)?(\d{1,2})\s*月(?:份)?\s*(?:相比|比起来|比较|比)", tail
    )
    if m2 and (not m2.group(2) or 1 <= int(m2.group(2)) <= 12):
        cm = int(m2.group(2))
        # 未写年份时：对比月在本期之前→同年，否则取上一年（最近一次出现的那个月）
        cy = int(m2.group(1)) if m2.group(1) else (s.year if cm < s.month else s.year - 1)
        cs, ce = _month_range(cy, cm)
        cmp = CompareSpec(type="prev_period", label=f"{cy}年{cm}月", start=cs.isoformat(), end=ce.isoformat())
    elif "同比" in q:
        cs, ce = _yoy(s, e)
        cmp = CompareSpec(type="yoy", label="同比", start=cs.isoformat(), end=ce.isoformat())
    elif "环比" in q or "对比" in q or "相比" in q or "比上" in q:
        cs, ce = _prev_window(s, e)
        cmp = CompareSpec(type="prev_period", label="环比", start=cs.isoformat(), end=ce.isoformat())
    elif tr.label == "上周":
        cs, ce = _prev_window(s, e)
        cmp = CompareSpec(type="wow", label="对比上上周", start=cs.isoformat(), end=ce.isoformat())
    elif tr.label == "上月":
        py, pm = _shift_months(s, -1).timetuple()[:2]
        cs, ce = _month_range(py, pm)
        cmp = CompareSpec(type="mom", label="前一个月", start=cs.isoformat(), end=ce.isoformat())
    return tr, cmp


def parse_plan(question: str, prefs: dict[str, str] | None = None) -> Plan:
    q = (question or "").strip().lower()
    prefs = prefs or {}
    tr, cmp = resolve_time(q)

    metric = "gmv"
    for word, key in METRIC_WORDS:
        if word in q:
            metric = key
            break

    dims: list[str] = []
    for word, key in DIM_WORDS:
        if word in q and key not in dims:
            dims.append(key)

    intent = "summary"
    if any(w in q for w in ("为什么", "为啥", "原因", "归因", "怎么会", "分析下", "分析一下")):
        intent = "attribution"
    elif any(w in q for w in ("趋势", "走势", "变化", "每天", "每日", "曲线")):
        intent = "trend"
    elif any(w in q for w in ("排行", "排名", "top", "前几", "最高", "最好", "最差", "top")):
        intent = "rank"
    elif any(w in q for w in ("对比", "相比", " vs", "比上")):
        intent = "compare"

    if intent == "attribution" and cmp.type == "none":
        s, e = date.fromisoformat(tr.start), date.fromisoformat(tr.end)
        cs, ce = _prev_window(s, e)
        cmp = CompareSpec(type="prev_period", label="环比上期", start=cs.isoformat(), end=ce.isoformat())
    if intent == "trend" and "day" not in dims and "month" not in dims:
        dims = ["day"]
    if intent == "attribution" and not dims:
        dims = ["channel", "region"]

    filters: list[FilterItem] = []
    for r in REGIONS:
        if r in q:
            filters.append(FilterItem(field="region", value=r))
    for c in CHANNELS:
        if c in q:
            filters.append(FilterItem(field="channel", value=c))
    for c in CATEGORIES:
        if c in q:
            filters.append(FilterItem(field="category", value=c))

    # 长期偏好兜底：用户上次说过"我只看华东"，这次没提就沿用
    if prefs and not any(f.field == "region" for f in filters) and prefs.get("region"):
        filters.append(FilterItem(field="region", value=prefs["region"]))

    top_n = None
    if m := re.search(r"(?:前|top)\s*(\d+)", q):
        top_n = max(1, min(20, int(m.group(1))))
    elif intent == "rank":
        top_n = 5

    need_ads = metric in {"spend", "roi", "clicks"} or "投放" in q or "roi" in q
    need_inventory = metric in {"stock", "turnover"} or "库存" in q or "周转" in q

    clarify = None
    if len(q) < 4:
        clarify = "能说得再具体一点吗？比如「上周华东区各渠道的销售额」。"

    return Plan(
        intent=intent,
        metric=metric,
        dimensions=dims,
        filters=filters,
        time_range=tr,
        compare=cmp,
        top_n=top_n,
        need_ads=need_ads,
        need_inventory=need_inventory,
        clarify=clarify,
        planner="rule",
    )
