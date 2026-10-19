"""分析推理师 Agent：把 SQL 结果集算成结构化结论。

只用 Pandas / 纯计算，不做文字包装（那是撰写官的活）。
归因用的是"按维度拆贡献"——把总变化量拆到各成员头上，再按贡献度排序，
这样"为什么降了"能落到具体渠道/区域，而不是一句"整体下滑"。
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from . import sql_engine
from .schemas import METRICS, Conclusion, Driver, Fact, Plan, SeriesPoint, SqlResult, TimeRange

CROSS_METRICS = {"roi", "turnover"}
DIM_CN = {"region": "区域", "channel": "渠道", "category": "品类", "product": "商品", "warehouse": "仓库", "day": "日期", "month": "月份"}


def fmt_money(v: float) -> str:
    a = abs(v)
    if a >= 1e8:
        return f"{v / 1e8:.2f} 亿"
    if a >= 1e4:
        return f"{v / 1e4:.1f} 万"
    return f"{v:,.0f}"


def fmt_value(v: float | None, metric: str) -> str:
    if v is None:
        return "—"
    fmt = METRICS[metric]["fmt"]
    unit = METRICS[metric]["unit"]
    if fmt == "money":
        return f"{fmt_money(v)}{unit}"
    if fmt == "pct":
        return f"{v:.2f} {unit}"
    if fmt == "ratio":
        return f"{v:.2f} {unit}"
    if abs(v) >= 1e4:
        return f"{v / 1e4:.1f} 万 {unit}"
    return f"{v:,.0f} {unit}"


def fmt_delta(v: float, metric: str) -> str:
    """变化额按指标类型给口径：金额用万/亿，比率用百分点。"""
    fmt = METRICS[metric]["fmt"]
    sign = "+" if v >= 0 else ""
    if fmt == "pct":
        return f"{sign}{v:.2f} 个百分点"
    if fmt == "ratio":
        return f"{sign}{v:.2f}"
    return f"{sign}{fmt_money(v)}{METRICS[metric]['unit']}"


def _agg(plan: Plan, dims: list[str], tr: TimeRange, purpose: str = "") -> SqlResult:
    if plan.metric in CROSS_METRICS:
        return sql_engine.run_cross(plan.model_copy(update={"dimensions": dims}), tr, purpose)
    return sql_engine.run_agg(plan, dims, tr, purpose)


def _total(plan: Plan, tr: TimeRange, purpose: str = "") -> tuple[float, SqlResult]:
    if plan.metric in CROSS_METRICS:
        res = sql_engine.run_cross(plan.model_copy(update={"dimensions": []}), tr, purpose)
        val = float(res.rows[0]["metric"]) if res.rows and res.rows[0].get("metric") is not None else 0.0
        return val, res
    res = _agg(plan, [], tr, purpose)
    val = float(res.rows[0]["metric"]) if res.rows and res.rows[0].get("metric") is not None else 0.0
    return val, res


def _frame(rows: list[dict[str, Any]], dim: str) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=[dim, "metric"])
    df["metric"] = pd.to_numeric(df["metric"], errors="coerce").fillna(0.0)
    return df


def _filter_text(plan: Plan) -> str:
    bits = []
    for f in plan.filters:
        if f.field == "region":
            bits.append(f"{f.value}区")
        elif f.field == "channel":
            bits.append(f"{f.value}渠道")
        elif f.field == "category":
            bits.append(f"{f.value}品类")
    return "".join(bits)


def analyze(plan: Plan, seed_cur: SqlResult | None = None, seed_prev: SqlResult | None = None) -> tuple[Conclusion, SqlResult, SqlResult | None, list[SqlResult]]:
    metric_meta = METRICS[plan.metric]
    used: list[SqlResult] = []
    dims = list(plan.dimensions)
    if plan.intent == "trend" and "day" not in dims and "month" not in dims:
        dims = ["day"]
    if plan.intent == "rank" and not dims:
        dims = ["channel"]
    if plan.intent == "attribution" and not dims:
        dims = ["channel"]

    # SQL 节点算过的那两条直接复用，别让同一个数字在证据里出现两次
    if seed_cur is not None and seed_cur.status == "ok" and seed_cur.rows:
        cur_res = seed_cur
        cur_total = float(cur_res.rows[0].get("metric") or 0)
    else:
        cur_total, cur_res = _total(plan, plan.time_range, f"{plan.time_range.label} · {metric_meta['label']} · 总量")
    used.append(cur_res)
    prev_total: float | None = None
    prev_res: SqlResult | None = None
    prev_tr: TimeRange | None = None
    if plan.compare.start and plan.compare.end:
        prev_tr = TimeRange(label=plan.compare.label, start=plan.compare.start, end=plan.compare.end)
        if seed_prev is not None and seed_prev.status == "ok" and seed_prev.rows:
            prev_res = seed_prev
            prev_total = float(prev_res.rows[0].get("metric") or 0)
        else:
            prev_total, prev_res = _total(plan, prev_tr, f"{plan.compare.label} · {metric_meta['label']} · 总量")
        used.append(prev_res)

    delta = None if prev_total is None else cur_total - prev_total
    delta_pct = None
    if prev_total not in (None, 0) and delta is not None:
        delta_pct = delta / abs(prev_total) * 100

    conclusion = Conclusion(
        metric=plan.metric,
        metric_label=metric_meta["label"],
        unit=metric_meta["unit"],
        current=round(cur_total, 4),
        current_display=fmt_value(cur_total, plan.metric),
        previous=None if prev_total is None else round(prev_total, 4),
        delta=None if delta is None else round(delta, 4),
        delta_pct=None if delta_pct is None else round(delta_pct, 2),
    )

    # ---- 逐维度明细（最多两层，供前端切换 / 反思节点核对槽位）----
    for d in dims[:2]:
        res = _agg(plan, [d], plan.time_range, f"{plan.time_range.label} · 按{DIM_CN.get(d, d)}拆分")
        used.append(res)
        if res.status != "ok" or not res.rows:
            continue
        df = _frame(res.rows, d).sort_values("metric", ascending=(plan.intent == "trend"))
        conclusion.breakdown_by_dim[d] = [
            {
                d: str(r[d]),
                "metric": round(float(r["metric"]), 2),
                "display": fmt_value(float(r["metric"]), plan.metric),
            }
            for _, r in df.head(24).iterrows()
        ]
        if "day" in dims and d == "day":
            conclusion.series = [SeriesPoint(x=str(r[d]), y=round(float(r["metric"]), 2)) for _, r in df.iterrows()]

    primary = dims[0] if dims else None
    if primary:
        conclusion.breakdown = conclusion.breakdown_by_dim.get(primary, [])

    # ---- 归因：按维度成员拆贡献 ----
    if plan.intent == "attribution" and prev_tr is not None and primary:
        prev_d = _agg(plan, [primary], prev_tr, f"{plan.compare.label} · 按{DIM_CN.get(primary, primary)}拆分")
        cur_d = _agg(plan, [primary], plan.time_range, f"{plan.time_range.label} · 按{DIM_CN.get(primary, primary)}拆分")
        used += [prev_d, cur_d]
        if cur_d.status == "ok" and prev_d.status == "ok":
            cd = _frame(cur_d.rows, primary).set_index(primary)["metric"]
            pd_ = _frame(prev_d.rows, primary).set_index(primary)["metric"]
            joined = pd.concat([cd.rename("cur"), pd_.rename("prev")], axis=1).fillna(0.0)
            joined["delta"] = joined["cur"] - joined["prev"]
            joined = joined.sort_values("delta")
            # 分母用波动绝对量之和：各成员此消彼长时净变化可能接近 0，
            # 拿净变化当分母会冒出 +330% 这种没法解释的占比。
            total_abs = float(joined["delta"].abs().sum()) or 1.0
            conclusion.breakdown_by_dim[f"{primary}__prev"] = [
                {
                    primary: str(m),
                    "metric": round(float(r["prev"]), 2),
                    "display": fmt_value(float(r["prev"]), plan.metric),
                }
                for m, r in joined.iterrows()
            ]
            for member, row in joined.head(6).iterrows():
                d = float(row["delta"])
                share = d / total_abs * 100
                conclusion.drivers.append(
                    Driver(
                        dimension=primary,
                        member=str(member),
                        delta=round(d, 2),
                        delta_display=fmt_delta(d, plan.metric),
                        share=round(share, 2),
                        note=f"{member} 本期{'下滑' if d < 0 else '增长'} {fmt_delta(abs(d), plan.metric)}，占波动总量 {abs(share):.1f}%",
                    )
                )

            # 投放相关性：同口径（同区域/同品类）对比投放花费变化，归因才有说服力
            cond, params = "", []
            for f in plan.filters:
                if f.field in ("region", "channel"):
                    cond += f" AND {f.field} = ?"
                    params.append(f.value)
            spend_cur = sql_engine.execute(
                f"SELECT channel, SUM(spend) AS s FROM fact_ads WHERE ad_date BETWEEN ? AND ?{cond} GROUP BY channel",
                [plan.time_range.start, plan.time_range.end] + params,
                f"{plan.time_range.label} · 各渠道投放花费（归因交叉验证）",
            )
            spend_prev = sql_engine.execute(
                f"SELECT channel, SUM(spend) AS s FROM fact_ads WHERE ad_date BETWEEN ? AND ?{cond} GROUP BY channel",
                [prev_tr.start, prev_tr.end] + params,
                f"{plan.compare.label} · 各渠道投放花费（归因交叉验证）",
            )
            used += [spend_cur, spend_prev]
            if spend_cur.status == "ok" and spend_prev.status == "ok":
                sc = {r["channel"]: float(r["s"] or 0) for r in spend_cur.rows}
                sp = {r["channel"]: float(r["s"] or 0) for r in spend_prev.rows}
                chan_delta = [(ch, (sc[ch] - sp[ch]) / sp[ch] * 100) for ch in sc if sp.get(ch)]
                chan_delta.sort(key=lambda x: x[1])
                for ch, pct in chan_delta:
                    if pct <= -10:
                        conclusion.caveats.append(
                            f"同口径下 {ch} 渠道投放花费变化 {pct:+.1f}%，与销售下滑方向一致，属于可解释的外部驱动，"
                            "不应全部归因于运营动作。"
                        )

    # ---- 异常检测：日序列 3σ ----
    if conclusion.series and len(conclusion.series) >= 10:
        ys = pd.Series([p.y for p in conclusion.series])
        mu, sd = ys.mean(), ys.std()
        if sd and sd > 0:
            for p in conclusion.series:
                if abs(p.y - mu) > 3 * sd:
                    conclusion.anomalies.append(
                        f"{p.x} 出现异常值 {fmt_value(p.y, plan.metric)}（偏离均值 {abs(p.y - mu) / sd:.1f}σ）"
                    )

    # ---- 图表规格 ----
    if plan.intent == "trend" and conclusion.series:
        conclusion.chart = {
            "type": "line",
            "x": DIM_CN.get(primary or "day", "日期"),
            "y": metric_meta["label"],
            "points": [p.model_dump() for p in conclusion.series],
        }
    elif conclusion.breakdown:
        conclusion.chart = {
            "type": "bar",
            "x": DIM_CN.get(primary or "", primary or ""),
            "y": metric_meta["label"],
            "points": [{"x": b[primary], "y": b["metric"]} for b in conclusion.breakdown[:12]],
        }
    else:
        conclusion.chart = {"type": "kpi", "value": conclusion.current, "label": metric_meta["label"]}

    # ---- 事实清单（撰写官只能引用这里出现过的数字）----
    scope = _filter_text(plan)
    conclusion.facts.append(
        Fact(
            label=f"{plan.time_range.label}{scope}{metric_meta['label']}",
            value=round(cur_total, 2),
            display=fmt_value(cur_total, plan.metric),
            unit=metric_meta["unit"],
            source="fact_sales 聚合（走日期索引）",
        )
    )
    if prev_total is not None:
        conclusion.facts.append(
            Fact(
                label=f"上期（{plan.compare.label}）{metric_meta['label']}",
                value=round(prev_total, 2),
                display=fmt_value(prev_total, plan.metric),
                unit=metric_meta["unit"],
                source="同一套 SQL，换时间窗",
            )
        )
    if delta is not None:
        conclusion.facts.append(Fact(label="变化额", value=round(delta, 2), display=fmt_delta(delta, plan.metric), unit=metric_meta["unit"], source="本期 - 上期"))
    if delta_pct is not None:
        conclusion.facts.append(Fact(label="变化率", value=round(delta_pct, 2), display=f"{delta_pct:+.2f}%", unit="%", source="变化额 / 上期"))
    if conclusion.drivers:
        top = conclusion.drivers[0]
        conclusion.facts.append(
            Fact(label="最大贡献项", value=round(top.delta, 2), display=f"{top.member} {top.delta_display}", unit=metric_meta["unit"], source="维度贡献拆解")
        )

    conclusion.caveats.append(f"统计区间：{plan.time_range.start} ~ {plan.time_range.end}，数据截至该日，之后的日期无数据不代表归零。")

    return conclusion, cur_res, prev_res, used
