"""通用数据集分析：把"上传的任意一张表"变成可分析对象。

和内置星型模型（METRICS 白名单）不同，这里的指标/维度是**运行时从字段画像里选**的：
- 聚合函数、指标列、维度列、过滤条件全部先落成 DatasetPlan 这个强类型对象，
  再拼参数化 SQL —— 列名只从画像里取，问句里的字一个都不进 SQL。
- 生成不了的（比如表里根本没有数值列、维度全被过滤掉）就返回 clarify，不硬猜。
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field

from . import datasets, nlu, sql_engine
from .schemas import CompareSpec, Conclusion, Driver, Fact, SeriesPoint, SqlResult, TimeRange

AGG_WORDS: list[tuple[tuple[str, ...], str]] = [
    (("平均", "均值", "人均", "均价"), "avg"),
    (("最多", "最大", "最高", "峰值"), "max"),
    (("最少", "最小", "最低", "谷值"), "min"),
    (("去重", "不同", "几种", "多少个"), "count_distinct"),
    (("多少条", "条数", "记录数", "行数", "笔数"), "count"),
]

AGG_LABEL = {"sum": "合计", "avg": "平均", "max": "最大", "min": "最小", "count": "记录数", "count_distinct": "去重数"}
MONEY_HINT = re.compile(r"(额|金额|营收|销售额|收入|费用|成本|花费|预算|price|amount|revenue|cost|spend|gmv)", re.I)
PCT_HINT = re.compile(r"(率|占比|比例|percent|rate|ratio)", re.I)


class DsFilter(BaseModel):
    field: str
    op: Literal["=", "in"] = "="
    value: Any


class DatasetPlan(BaseModel):
    """针对上传表生成的执行计划。字段名刻意与 Plan 对齐，好让反思/撰写节点直接复用。"""

    dataset: str
    table: str
    intent: Literal["summary", "trend", "rank", "attribution", "compare"] = "summary"
    agg: Literal["sum", "avg", "max", "min", "count", "count_distinct"] = "sum"
    metric_col: str = ""
    metric: str = "metric"
    metric_label: str = "指标"
    unit: str = ""
    fmt: Literal["money", "num", "pct"] = "num"
    dimensions: list[str] = Field(default_factory=list)
    filters: list[DsFilter] = Field(default_factory=list)
    time_range: TimeRange
    compare: CompareSpec = Field(default_factory=CompareSpec)
    top_n: int | None = None
    order: Literal["desc", "asc"] = "desc"
    clarify: str | None = None
    planner: str = "rule(dataset)"
    time_col: str | None = None
    grain: Literal["day", "week", "month"] | None = None  # 时间粒度（趋势题用）

    @property
    def dim_col(self) -> str | None:
        return self.dimensions[0] if self.dimensions else None

    def model_copy(self, *, update: dict[str, Any] | None = None, deep: bool = False):  # type: ignore[override]
        return super().model_copy(update=update, deep=deep)


def fmt_of(col: str) -> tuple[str, str]:
    if PCT_HINT.search(col):
        return "pct", "%"
    if MONEY_HINT.search(col):
        return "money", "元"
    return "num", ""


def fmt_number(v: float, fmt: str, unit: str = "") -> str:
    if v is None:
        return "—"
    if fmt == "pct":
        return f"{v:.2f}%"
    if fmt == "money":
        a = abs(v)
        if a >= 1e8:
            return f"{v / 1e8:.2f} 亿{unit}"
        if a >= 1e4:
            return f"{v / 1e4:.1f} 万{unit}"
        return f"{v:,.0f} {unit}".strip()
    a = abs(v)
    if a >= 1e4:
        return f"{v / 1e4:.1f} 万{unit}"
    return f"{v:,.2f}".rstrip("0").rstrip(".") + (f" {unit}" if unit else "")


TIME_HINT = re.compile(
    r"(昨天|今天|本周|上周|上上周|本月|上月|上个月|这个月|最近|过去|去年|今年|前年|\d+\s*(?:天|日|周|个?月|年)|(?:\d{1,2})月\d{1,2}|全部|所有数据|全量|整体|历史)"
)


def _pick_agg(q: str) -> str:
    for words, agg in AGG_WORDS:
        if any(w in q for w in words):
            return agg
    return "sum"


def _pick_metric(q: str, ds: dict[str, Any]) -> str:
    cols = ds["metric_cols"]
    # 1) 问句里点名了列名
    for c in sorted(cols, key=len, reverse=True):
        if c.lower() in q:
            return c
    # 2) 金额类列优先（用户说"销售额/收入"大概率指金额列，而不是恰好排第一的数量列）
    money = [c for c in cols if MONEY_HINT.search(c)]
    if money:
        return money[0]
    return cols[0]


def _pick_dim(q: str, ds: dict[str, Any], intent: str) -> str | None:
    dims = ds["dim_cols"]
    # 1) 问句里直接出现列名
    for c in sorted(dims, key=len, reverse=True):
        if c.lower() in q:
            return c
    # 2) "各X / 按X / 每个X / 分X" 后面那个词
    for m in re.finditer(r"(?:各|按|每个|分|每)([\u4e00-\u9fa5a-zA-Z_]{1,8})", q):
        seg = m.group(1)
        for c in dims:
            if seg in c or c in seg:
                return c
    # 3) 趋势看时间列，排行看第一个分类列
    if intent == "trend" and ds.get("time_col"):
        return ds["time_col"]
    if intent in ("rank", "attribution") and dims:
        return dims[0]
    return None


def _pick_filters(q: str, ds: dict[str, Any], dim: str | None) -> list[DsFilter]:
    """问句里出现某个分类列的具体值（"华东""线上"）就做等值过滤。

    注意：**不排除当前分组维度** —— "为什么华东的销售额变化了"必须同时
    GROUP BY 区域 和 WHERE 区域=华东，否则归因归的是全国。
    """
    out: list[DsFilter] = []
    for p in ds["profile"]:
        if p["role"] != "dim":
            continue
        for v in p.get("values", [])[:24]:
            sv = str(v)
            if len(sv) >= 2 and sv.lower() in q:
                out.append(DsFilter(field=p["name"], value=v))
                break
    # 「深圳」和「深圳南山店」会同时命中（城市 + 门店），拼进标题就成了"深圳深圳南山店"。
    # 命中了更具体的成员，就丢掉被它包含的那个粗粒度过滤条件。
    if len(out) > 1:
        out = [
            f
            for f in out
            if not any(
                g is not f and f.field != g.field and str(f.value) in str(g.value) for g in out
            )
        ]
    return out


def build_plan(question: str, ds: dict[str, Any]) -> DatasetPlan:
    q = (question or "").strip().lower()
    lo, hi = datasets.value_range(ds)
    anchor = date.fromisoformat(str(hi)[:10]) if hi else date.today()
    # 问句里没有时间词时，默认查**全区间**而不是近30天——
    # 用户文件的时间跨度是任意的，拿"近30天"去套只会得到一个对不上文件的数字。
    if TIME_HINT.search(q):
        tr, cmp = nlu.resolve_time(q, anchor)
    else:
        full = lo and hi
        if full:
            tr = TimeRange(label="全部区间", start=str(lo)[:10], end=str(hi)[:10])
            # 全区间也能比：对齐全区的上一段做环比 / 去年同期做同比
            if "同比" in q:
                cs, ce = nlu._yoy(date.fromisoformat(str(lo)[:10]), date.fromisoformat(str(hi)[:10]))
                cmp = CompareSpec(type="yoy", label="同比", start=cs.isoformat(), end=ce.isoformat())
            elif "环比" in q:
                cs, ce = nlu._prev_period(date.fromisoformat(str(lo)[:10]), date.fromisoformat(str(hi)[:10]))
                cmp = CompareSpec(type="prev_period", label="环比上期", start=cs.isoformat(), end=ce.isoformat())
            else:
                cmp = CompareSpec()
        else:
            tr, cmp = nlu.resolve_time(q, anchor)
    if lo and hi and any(w in q for w in ("全部", "所有数据", "全量", "整体时间", "整个区间")):
        tr = TimeRange(label="全部区间", start=str(lo)[:10], end=str(hi)[:10])
    # 解析出的区间裁剪到数据实际覆盖范围（问"2026年"而数据只到 10 月时，不要拿空区间去算）
    if lo and hi and tr.label != "全部区间":
        s, e = max(tr.start, str(lo)[:10]), min(tr.end, str(hi)[:10])
        if s <= e:
            tr = TimeRange(label=tr.label, start=s, end=e)

    intent = "summary"
    if any(w in q for w in ("为什么", "为啥", "原因", "归因", "怎么会")):
        intent = "attribution"
    elif any(w in q for w in ("趋势", "走势", "变化", "每天", "每日", "曲线", "按月", "每月")):
        intent = "trend"
    elif any(w in q for w in ("排行", "排名", "top", "前几", "最高", "最低", "最差")):
        intent = "rank"
    elif any(w in q for w in ("对比", "相比", "vs")):
        intent = "compare"

    metric_col = _pick_metric(q, ds)
    fmt, unit = fmt_of(metric_col)
    agg = _pick_agg(q)
    # 「哪个品类销售额最高」里的"最高"是排序词，不是聚合函数：
    # rank 意图下仍按 sum 汇总各成员，再挑最大的那个；否则会算成全表的 MAX(单行销售额)。
    if intent == "rank" and agg in ("max", "min") and not any(
        w in q for w in ("平均", "均值", "人均", "均价")
    ):
        agg = "sum"
    if agg in ("count", "count_distinct"):
        fmt, unit = "num", "条" if agg == "count" else "个"
    dim = _pick_dim(q, ds, intent)
    if intent == "trend" and ds.get("time_col"):
        dim = ds["time_col"]
    if intent == "attribution" and cmp.type == "none":
        if tr.label == "全部区间" and hi:
            # 全区间问"为什么降"：拿最后一个自然月对比上一个月。
            # 否则会去比「数据开始之前」那一段（必然空），只能答"无法计算"。
            last = date.fromisoformat(str(hi)[:10])
            s = last.replace(day=1)
            e = last
            tr = TimeRange(label=f"{last.year}年{last.month}月", start=s.isoformat(), end=e.isoformat())
        else:
            s = date.fromisoformat(tr.start)
            e = date.fromisoformat(tr.end)
        cs, ce = nlu._prev_window(s, e)
        cmp = CompareSpec(type="prev_period", label="环比上期", start=cs.isoformat(), end=ce.isoformat())

    filters = _pick_filters(q, ds, dim)
    # 归因要"往下拆一层"：如果已经把某个维度钉死成过滤条件（如 区域=华东），
    # 再按同一个维度分组只会剩一个成员，归因就没有意义了 —— 换一个维度交叉。
    if intent == "attribution" and dim and any(f.field == dim for f in filters):
        for c in ds["dim_cols"]:
            if c != dim and not any(f.field == c for f in filters):
                dim = c
                break

    top_n = None
    if m := re.search(r"(?:前|top)\s*(\d+)", q):
        top_n = max(1, min(30, int(m.group(1))))
    elif intent == "rank":
        top_n = 10

    label = f"{AGG_LABEL[agg]}{metric_col}"
    clarify = None
    if not ds["metric_cols"]:
        clarify = "这张表里没有识别到数值列，只能做计数统计，要不要换个文件？"

    grain = None
    if re.search(r"按周|每周|周度|分周|以周", q):
        grain = "week"
    elif re.search(r"按天|每天|每日|日度", q):
        grain = "day"
    elif re.search(r"按月|每月|月度|分月", q):
        grain = "month"
    # "按周看销售额"这类问法没写"趋势"，但粒度词本身就说明要看时间序列
    if grain and intent == "summary":
        intent = "trend"
        if ds.get("time_col"):
            dim = ds["time_col"]

    plan = DatasetPlan(
        dataset=ds["id"],
        table=ds["table"],
        intent=intent,
        agg=agg,  # type: ignore[arg-type]
        metric_col=metric_col,
        metric_label=label,
        unit=unit,
        fmt=fmt,  # type: ignore[arg-type]
        dimensions=[dim] if dim else [],
        filters=filters,
        time_range=tr,
        compare=cmp,
        top_n=top_n,
        clarify=clarify,
        time_col=ds.get("time_col"),
        grain=grain,  # type: ignore[arg-type]
    )
    # 归因题不能"抓到哪个维度就拆哪个"：挑波动最集中的那个维度，它才最接近可行动的原因
    if plan.intent == "attribution" and plan.compare.start and len(ds["dim_cols"]) > 1:
        cands = [d for d in ds["dim_cols"] if not any(f.field == d for f in filters)]
        if len(cands) > 1:
            best, best_score = None, -1.0
            for cand in cands:
                score = _dim_concentration(plan, cand)
                if score > best_score:
                    best, best_score = cand, score
            if best and best != dim:
                plan = plan.model_copy(update={"dimensions": [best]})
    return plan


# --------------------------------------------------------------------------
# SQL 生成
# --------------------------------------------------------------------------


def _agg_expr(plan: DatasetPlan) -> str:
    col = f'"{plan.metric_col}"'
    if plan.agg == "count":
        return "COUNT(*)"
    if plan.agg == "count_distinct":
        return f"COUNT(DISTINCT {col})"
    return f"ROUND({plan.agg.upper()}({col}), 2)"


def _dim_expr(plan: DatasetPlan, dim: str) -> str:
    if plan.intent == "trend" and plan.time_col and dim == plan.time_col:
        grain = plan.grain
        if grain is None:
            if "月" in plan.time_range.label or plan.time_range.label in ("上月", "本月"):
                grain = "month"
            else:
                # 区间超过 45 天自动按月，否则按天，避免折线被 90 个点撑爆
                try:
                    d0 = date.fromisoformat(plan.time_range.start)
                    d1 = date.fromisoformat(plan.time_range.end)
                    grain = "month" if (d1 - d0).days > 45 else "day"
                except Exception:
                    grain = "day"
        if grain == "week":
            return f"strftime('%Y-W%W', \"{dim}\")"
        if grain == "month":
            return f'substr("{dim}",1,7)'
        return f'"{dim}"'
    return f'"{dim}"'


def _where(plan: DatasetPlan, tr: TimeRange, with_time: bool = True) -> tuple[list[str], list[Any]]:
    w: list[str] = []
    p: list[Any] = []
    if with_time and plan.time_col:
        w.append(f'"{plan.time_col}" BETWEEN ? AND ?')
        p += [tr.start, tr.end]
    for f in plan.filters:
        w.append(f'"{f.field}" = ?')
        p.append(f.value)
    return w, p


def sql_total(plan: DatasetPlan, tr: TimeRange) -> SqlResult:
    w, p = _where(plan, tr)
    sql = f'SELECT {_agg_expr(plan)} AS metric FROM "{plan.table}"' + (" WHERE " + " AND ".join(w) if w else "")
    return sql_engine.execute(sql, p, f"{tr.label} · {plan.metric_label} · 全表聚合")


def sql_by_dim(plan: DatasetPlan, dim: str, tr: TimeRange, limit: int = 40) -> SqlResult:
    w, p = _where(plan, tr)
    dexpr = _dim_expr(plan, dim)
    order = "ASC" if plan.intent == "trend" else "DESC"
    sql = (
        f'SELECT {dexpr} AS dim, {_agg_expr(plan)} AS metric FROM "{plan.table}"'
        + (" WHERE " + " AND ".join(w) if w else "")
        + f" GROUP BY {dexpr} ORDER BY {'dim' if plan.intent == 'trend' else 'metric'} {order} LIMIT {int(limit)}"
    )
    return sql_engine.execute(sql, p, f"{tr.label} · {plan.metric_label} · 按{dim}拆分")


def _dim_concentration(plan: DatasetPlan, dim: str) -> float:
    """某个维度"能解释多少变化"的集中度：最大成员波动 / 全部成员波动绝对值之和。

    归因题要的是可行动的原因。城市维度只能说明"深圳降了"（地点），渠道维度能说明
    "外卖塌了"（原因）。集中度越高，说明这个维度的波动越由单一成员主导，越接近真因。
    """
    prev_tr = TimeRange(label=plan.compare.label or "上期", start=plan.compare.start or "", end=plan.compare.end or "")
    if not prev_tr.start or not prev_tr.end:
        return 0.0
    cur = sql_by_dim(plan, dim, plan.time_range, limit=200)
    prev = sql_by_dim(plan, dim, prev_tr, limit=200)
    cm = {_dim_label(r.get("dim")): _num(r.get("metric")) for r in cur.rows}
    pm = {_dim_label(r.get("dim")): _num(r.get("metric")) for r in prev.rows}
    members = set(cm) | set(pm)
    if len(members) < 2:
        return 0.0  # 只有一两个成员的维度说明不了结构，不算「解释力」
    deltas = [cm.get(k, 0.0) - pm.get(k, 0.0) for k in members]
    total_abs = sum(abs(v) for v in deltas) or 1.0
    return max((abs(v) for v in deltas), default=0.0) / total_abs


# --------------------------------------------------------------------------
# 分析
# --------------------------------------------------------------------------


def _num(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _dim_label(v: Any) -> str:
    """维度成员的空值统一显示成「未标注」，别把 None/nan 直接甩给用户看。"""
    if v is None:
        return "未标注"
    s = str(v).strip()
    return "未标注" if s in ("", "None", "nan", "NaN", "null", "NULL") else s


def _display(plan: DatasetPlan, v: float) -> str:
    return fmt_number(v, plan.fmt, plan.unit)


def analyze_dataset(
    plan: DatasetPlan,
    seed_cur: SqlResult | None = None,
    seed_prev: SqlResult | None = None,
) -> tuple[Conclusion, SqlResult, SqlResult | None, list[SqlResult]]:
    used: list[SqlResult] = []
    cur_res = seed_cur if (seed_cur and seed_cur.status == "ok" and seed_cur.rows) else sql_total(plan, plan.time_range)
    used.append(cur_res)
    cur_total = _num(cur_res.rows[0].get("metric")) if cur_res.rows else 0.0

    prev_res: SqlResult | None = None
    prev_total: float | None = None
    prev_tr: TimeRange | None = None
    if plan.compare.start and plan.compare.end:
        prev_tr = TimeRange(label=plan.compare.label, start=plan.compare.start, end=plan.compare.end)
        prev_res = seed_prev if (seed_prev and seed_prev.status == "ok" and seed_prev.rows) else sql_total(plan, prev_tr)
        used.append(prev_res)
        prev_total = _num(prev_res.rows[0].get("metric")) if prev_res.rows else 0.0

    delta = None if prev_total is None else cur_total - prev_total
    delta_pct = None
    if prev_total not in (None, 0) and delta is not None:
        delta_pct = delta / abs(prev_total) * 100

    c = Conclusion(
        metric="metric",
        metric_label=plan.metric_label,
        unit=plan.unit,
        current=round(cur_total, 4),
        current_display=_display(plan, cur_total),
        previous=None if prev_total is None else round(prev_total, 4),
        delta=None if delta is None else round(delta, 4),
        delta_pct=None if delta_pct is None else round(delta_pct, 2),
    )

    dim = plan.dim_col
    if dim:
        res = sql_by_dim(plan, dim, plan.time_range)
        used.append(res)
        rows = [r for r in res.rows if r.get("metric") is not None]
        rows.sort(key=lambda r: (str(r["dim"]) if plan.intent == "trend" else -_num(r["metric"])))
        c.breakdown_by_dim[dim] = [
            {dim: _dim_label(r["dim"]), "metric": round(_num(r["metric"]), 2), "display": _display(plan, _num(r["metric"]))} for r in rows[:30]
        ]
        c.breakdown = c.breakdown_by_dim[dim]
        if plan.intent == "trend":
            c.series = [SeriesPoint(x=_dim_label(r["dim"]), y=round(_num(r["metric"]), 2)) for r in rows]

        if plan.intent == "attribution" and prev_tr is not None:
            prev_d = sql_by_dim(plan, dim, prev_tr)
            used.append(prev_d)
            cur_map = {_dim_label(r["dim"]): _num(r["metric"]) for r in res.rows}
            prev_map = {_dim_label(r["dim"]): _num(r["metric"]) for r in prev_d.rows}
            # 上期分成员值也落进结论对象，报告的拆解表才能给出本期/上期两列（数字仍全部可溯源）
            c.breakdown_by_dim[f"{dim}__prev"] = [
                {dim: k, "metric": round(prev_map.get(k, 0.0), 2), "display": _display(plan, prev_map.get(k, 0.0))}
                for k in sorted(set(cur_map) | set(prev_map), key=lambda k: -(cur_map.get(k, 0.0)))
            ]
            joined = [(k, cur_map.get(k, 0.0) - prev_map.get(k, 0.0)) for k in set(cur_map) | set(prev_map)]
            joined.sort(key=lambda x: x[1])
            # 分母用「波动总量」而不是净变化：各成员此消彼长时净值可能接近 0，
            # 拿净值当分母会算出 300% 这种没法看的占比。
            total_abs = sum(abs(v) for _, v in joined) or 1.0
            for member, d in joined[:6]:
                share = d / total_abs * 100
                c.drivers.append(
                    Driver(
                        dimension=dim,
                        member=member,
                        delta=round(d, 2),
                        delta_display=("+" if d >= 0 else "") + _display(plan, d),
                        share=round(share, 2),
                        note=f"{member} 本期{'增长' if d >= 0 else '下滑'} {_display(plan, abs(d))}，占波动总量 {abs(share):.1f}%",
                    )
                )

    # 图表
    if plan.intent == "trend" and c.series:
        c.chart = {"type": "line", "x": dim or "时间", "y": plan.metric_label, "points": [p.model_dump() for p in c.series]}
    elif c.breakdown:
        c.chart = {
            "type": "bar",
            "x": dim or "维度",
            "y": plan.metric_label,
            "points": [{"x": b[dim], "y": b["metric"]} for b in c.breakdown[:12]],
        }
    elif c.previous is not None and c.previous:
        # 只有环比、没有维度可拆：给「本期 vs 上期」双柱，别只丢一个大数字
        c.chart = {
            "type": "compare",
            "x": "对比",
            "y": plan.metric_label,
            "points": [{"x": "本期", "y": c.current}, {"x": "上期", "y": c.previous}],
        }
    else:
        c.chart = {"type": "kpi", "value": c.current, "label": plan.metric_label}

    scope = "".join(f"{f.value}" for f in plan.filters)
    c.facts.append(
        Fact(
            label=f"{plan.time_range.label}{scope}{plan.metric_label}",
            value=round(cur_total, 2),
            display=_display(plan, cur_total),
            unit=plan.unit,
            source="",
        )
    )
    if prev_total is not None:
        c.facts.append(Fact(label=f"上期（{plan.compare.label}）{plan.metric_label}", value=round(prev_total, 2), display=_display(plan, prev_total), unit=plan.unit, source=""))
    if delta is not None:
        c.facts.append(Fact(label="变化额", value=round(delta, 2), display=("+" if delta >= 0 else "") + _display(plan, delta), unit=plan.unit, source="本期 - 上期"))
    if delta_pct is not None:
        c.facts.append(Fact(label="变化率", value=round(delta_pct, 2), display=f"{delta_pct:+.2f}%", unit="%", source="变化额 / 上期"))
    if c.drivers:
        top = c.drivers[0]
        c.facts.append(Fact(label="最大贡献项", value=round(top.delta, 2), display=f"{top.member} {top.delta_display}", unit=plan.unit, source="维度贡献拆解"))

    c.caveats.append(f"统计区间：{plan.time_range.start} ~ {plan.time_range.end}（数据来源：用户上传表 {plan.table}）")
    if prev_total == 0 and plan.compare.start:
        c.caveats.append(f"对比区间 {plan.compare.start} ~ {plan.compare.end} 在上传文件中没有记录，环比无法计算。")
    dsmeta = next((d for d in datasets.list_datasets() if plan.dataset in (d["id"], d["table"])), {})
    dsname = dsmeta.get("name") or plan.dataset
    c.caveats.append(f"聚合口径：{AGG_LABEL[plan.agg]}({plan.metric_col})，数据集《{dsname}》，字段含义按数据列名推断。")
    return c, cur_res, prev_res, used
