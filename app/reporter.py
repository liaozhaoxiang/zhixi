"""报告撰写官 Agent。

它拿不到原始数据，只能消费分析推理师产出的结构化 Conclusion。
最后一道闸：报告里出现的每个数字都必须能在结论对象里找到出处，找不到就拦下来。

2026-10-18 重写：
- 结构从「罗列 fact」升级为 分析报告：核心结论段 + 关键数据表 + 结构拆解（含占比）
  + 趋势要点 + 具体建议 + 来源清单。
- 新增「数据来源」一节：逐条证据给出来源名与可点击链接（上传文件可带真实 URL；
  内置合成数据明确标注，不冒充真实网站来源）。
- 派生数字（占比、成员变化额）在生成时同步登记进溯源白名单，保证闸门不被自己绕过。
"""
from __future__ import annotations

import re

from .analyst import fmt_money, fmt_value
from .schemas import Conclusion, Plan, ReportResult

DIM_LABEL = {
    "channel": "渠道",
    "region": "区域",
    "category": "品类",
    "product": "商品",
    "warehouse": "仓库",
    "day": "日期",
    "month": "月份",
}


# --------------------------------------------------------------------------
# 格式化工具（内置指标走 METRICS，上传数据集走 plan 自带口径）
# --------------------------------------------------------------------------


def _fmt(v: float, plan: Plan, c: Conclusion) -> str:
    try:
        return fmt_value(v, plan.metric)
    except KeyError:  # 上传数据集的动态指标
        if hasattr(plan, "fmt"):
            from .generic import fmt_number

            return fmt_number(v, plan.fmt, plan.unit)  # type: ignore[attr-defined]
        return f"{v:,.2f} {c.unit}".strip()


def _fmt_signed(v: float, plan: Plan, c: Conclusion) -> str:
    return ("+" if v >= 0 else "") + _fmt(v, plan, c)


def _fmt_pct(v: float) -> str:
    return f"{v:+.2f}%" if v is not None else "—"


def _cur_text(c: Conclusion) -> str:
    if c.current_display:
        return c.current_display
    return f"{c.current:,.2f} {c.unit}".strip()


def _scope(plan: Plan) -> str:
    bits = []
    for f in getattr(plan, "filters", []):
        bits.append(str(f.value))
    return "".join(bits)


def _dim_cn(dim: str) -> str:
    return DIM_LABEL.get(dim, dim)


def _headline(plan: Plan, c: Conclusion) -> str:
    m = c.metric_label
    scope = _scope(plan)
    if c.previous == 0 and c.delta is not None:
        return f"{plan.time_range.label}{scope}{m}为 {_cur_text(c)}；指定的对比区间（{_get_compare_label(plan)}）里没有数据，环比无法计算。"
    # 趋势题：别只报一个总数，直接给峰值/谷值和它们出现的时点
    series = getattr(c, "series", None) or []
    if getattr(plan, "intent", "") == "trend" and len(series) >= 3:
        hi = max(series, key=lambda p: p.y)
        lo = min(series, key=lambda p: p.y)
        return (
            f"{plan.time_range.label}{scope}{m}峰值在 {hi.x}（{_fmt(hi.y, plan, c)}），"
            f"谷值在 {lo.x}（{_fmt(lo.y, plan, c)}），合计 {_cur_text(c)}。"
        )
    if getattr(plan, "intent", "") == "rank" and getattr(c, "breakdown", None):
        # 排行题的标题必须写"谁第一"，只给一个总数等于没回答
        dim = getattr(plan, "dim_col", "") or ""
        top = max(c.breakdown, key=lambda b: b.get("metric") or 0)
        name = top.get(dim) or top.get("dim") or "第一名"
        val = top.get("display") or ""
        if c.current:
            return (
                f"{plan.time_range.label}{scope}{m} {_cur_text(c)}，"
                f"{_dim_cn(dim)}「{name}」最高为 {val}（占 {top.get('metric', 0) / c.current * 100:.1f}%）。"
            )
        return f"{plan.time_range.label}{scope}{m}，{_dim_cn(dim)}「{name}」最高为 {val}。"
    if c.delta_pct is None:
        return f"{plan.time_range.label}{scope}{m}为 {_cur_text(c)}。"
    direction = "增长" if (c.delta_pct or 0) >= 0 else "下滑"
    line = f"{plan.time_range.label}{scope}{m} {_cur_text(c)}，环比{direction} {abs(c.delta_pct):.2f}%"
    if c.drivers:
        # 标题引「绝对变化最大」的成员：涨的场景引最负的那个没有意义
        top = max(c.drivers, key=lambda d: abs(d.delta))
        line += f"，最大影响来自{_dim_cn(top.dimension)}「{top.member}」"
    return line + "。"


def _cite(fact_source: str) -> str:
    """把证据编号渲染成溯源角标，前端会把它变成可点击的锚点。"""
    src = (fact_source or "").split("（")[0].strip()
    return f" ⟦{src}⟧" if src.startswith("S") else ""


def _ev_id(evidence: list[dict] | None, contains: tuple[str, ...] = (), excludes: tuple[str, ...] = ()) -> str:
    """按 purpose 关键字找证据编号，让表格里的数字也能挂上溯源角标。"""
    for e in evidence or []:
        purpose = f"{e.get('purpose') or ''} {e.get('role') or ''}"
        if all(k in purpose for k in contains) and not any(k in purpose for k in excludes):
            eid = e.get("evidence_id") or ""
            if eid.startswith("S"):
                return eid
    return ""


def _cite_ev(eid: str) -> str:
    return f" ⟦{eid}⟧" if eid else ""


# --------------------------------------------------------------------------
# 报告正文
# --------------------------------------------------------------------------


def write_report(plan: Plan, c: Conclusion, evidence: list[dict] | None = None) -> ReportResult:
    md: list[str] = []
    extra_allowed: set[float] = set()  # 报告里现算的派生数字，全部登记进白名单

    ev = evidence or []
    cur_ev = _ev_id(ev, contains=("全表聚合",), excludes=("环比",))
    prev_ev = _ev_id(ev, contains=("环比",))
    dim_ev = _ev_id(ev, contains=("拆分",))
    if not cur_ev:  # 趋势题没有「全表聚合」，退回第一条 query
        first_q = next((e for e in ev if (e.get("role") or "") == "query"), None)
        cur_ev = (first_q or {}).get("evidence_id", "") or ""

    head = _headline(plan, c).rstrip("。")
    md.append(f"### {head}{_cite(c.facts[0].source if c.facts else '')}。")

    # ---- 核心结论：一段话说完，全部带数字 ----
    core: list[str] = []
    if c.previous == 0 and c.delta is not None:
        core.append(f"本期{c.metric_label} {_cur_text(c)}{_cite_ev(cur_ev)}；对比区间（{_get_compare_label(plan)}）在本数据集中没有记录，环比无法计算，建议扩大区间或检查数据覆盖。")
    elif c.delta_pct is not None:
        d = "增长" if (c.delta_pct or 0) >= 0 else "下滑"
        core.append(
            f"本期{c.metric_label} {_cur_text(c)}{_cite_ev(cur_ev)}，较{_get_compare_label(plan)}{d} "
            f"{abs(c.delta_pct):.2f}%（变化 {_fmt_signed(c.delta or 0, plan, c)}{_cite_ev(prev_ev)}）。"
        )
    series = getattr(c, "series", None) or []
    if c.delta_pct is None:
        if getattr(plan, "intent", "") == "trend" and len(series) >= 3:
            core.append(
                f"区间内共 {len(series)} 个时点，峰值 {_fmt(max(series, key=lambda p: p.y).y, plan, c)}，"
                f"谷值 {_fmt(min(series, key=lambda p: p.y).y, plan, c)}，合计 {_cur_text(c)}。"
            )
        elif getattr(c, "breakdown", None):
            core.append(f"本期{c.metric_label} {_cur_text(c)}，下面按维度拆开看。")
        else:
            core.append(f"本期{c.metric_label} {_cur_text(c)}。")
    if c.drivers:
        neg = [d for d in c.drivers if d.delta < 0]
        pos = [d for d in c.drivers if d.delta > 0]
        # 只讲够分量的成员：占比不到 10% 的属于噪声，写进结论反而稀释重点
        if neg and abs(neg[0].share) >= 10:
            top = neg[0]
            core.append(f"主要拖累是{_dim_cn(top.dimension)}「{top.member}」：变化 {top.delta_display}，占总波动的 {abs(top.share):.1f}%{_cite_ev(dim_ev)}。")
        if pos and abs(pos[0].share) >= 10 and (not neg or abs(pos[0].delta) >= abs(neg[-1].delta) * 0.5):
            top = pos[0]
            core.append(f"主要拉动是{_dim_cn(top.dimension)}「{top.member}」：变化 {top.delta_display}，占总波动的 {abs(top.share):.1f}%{_cite_ev(dim_ev)}。")
    md.append("\n**核心结论**")
    md.append("".join(core))

    # ---- 关键数据表 ----
    if c.previous is not None:
        md.append("\n**关键数据**\n")
        md.append("| 指标 | 本期 | 上期 | 变化 | 变化率 |")
        md.append("| --- | --- | --- | --- | --- |")
        md.append(
            f"| {c.metric_label} | {_cur_text(c)}{_cite_ev(cur_ev)} | {_fmt(c.previous, plan, c)}{_cite_ev(prev_ev)} "
            f"| {_fmt_signed(c.delta or 0, plan, c)} | {_fmt_pct(c.delta_pct)}{_cite_ev(cur_ev)}{_cite_ev(prev_ev)} |"
        )
    else:
        md.append("\n**关键数据**")
        for f in c.facts[:4]:
            md.append(f"- {f.label}：**{f.display}**{_cite(f.source)}")

    # ---- 结构拆解 / 归因表 ----
    if c.drivers:
        dim = c.drivers[0].dimension
        dim_cn = _dim_cn(dim)
        cur_rows = {str(b.get(dim)): b for b in c.breakdown or []}
        prev_rows = {str(b.get(dim)): b for b in (c.breakdown_by_dim or {}).get(f"{dim}__prev", [])}
        md.append(f"\n**{dim_cn}拆解**（按变化额排序，最多 6 项）\n")
        md.append(f"| {dim_cn} | 本期 | 上期 | 变化额 | 占波动 |")
        md.append("| --- | --- | --- | --- | --- |")
        for d in c.drivers[:6]:
            cur_v = cur_rows.get(d.member, {}).get("metric")
            prev_v = prev_rows.get(d.member, {}).get("metric")
            cur_txt = _fmt(cur_v, plan, c) if cur_v is not None else "—"
            prev_txt = _fmt(prev_v, plan, c) if prev_v is not None else "—"
            if cur_v is not None and prev_v is not None:
                extra_allowed.add(round(float(cur_v) - float(prev_v), 2))
            md.append(f"| {d.member} | {cur_txt} | {prev_txt} | {d.delta_display}{_cite_ev(dim_ev)} | {d.share:+.1f}% |")
    elif c.breakdown:
        dim_key = next((k for k in c.breakdown[0] if k not in ("metric", "display")), "维度")
        dim_cn = _dim_cn(dim_key)
        total = c.current or sum(float(b.get("metric", 0)) for b in c.breakdown) or 1.0
        md.append(f"\n**{dim_cn}分布**（前 {min(len(c.breakdown), 8)} 项）\n")
        md.append(f"| {dim_cn} | {c.metric_label} | 占总量 |")
        md.append("| --- | --- | --- |")
        for b in c.breakdown[:8]:
            mv = float(b.get("metric", 0))
            share = mv / total * 100
            extra_allowed.add(round(share, 2))
            extra_allowed.add(round(mv, 2))
            md.append(f"| {b.get(dim_key)} | {b.get('display') or mv}{_cite_ev(dim_ev)} | {share:.1f}% |")

    # ---- 趋势要点 ----
    if c.series and len(c.series) >= 3:
        peak = max(c.series, key=lambda p: p.y)
        trough = min(c.series, key=lambda p: p.y)
        md.append("\n**趋势要点**")
        md.append(f"- 峰值出现在 {peak.x}（{_fmt(peak.y, plan, c)}）；谷值在 {trough.x}（{_fmt(trough.y, plan, c)}）{_cite_ev(dim_ev)}。")
        md.append(f"- 期初 {c.series[0].x} 为 {_fmt(c.series[0].y, plan, c)}，期末 {c.series[-1].x} 为 {_fmt(c.series[-1].y, plan, c)}。")
        if c.anomalies:
            md.append(f"- 检出 {len(c.anomalies)} 个 3σ 异常点，见下方明细。")

    if c.anomalies:
        md.append("\n**异常点**")
        for s in c.anomalies[:3]:
            md.append(f"- {s}")

    # ---- 建议：必须落到成员名 + 数字，不给空话 ----
    md.append("\n**建议**")
    tips: list[str] = []
    if c.drivers:
        neg = [d for d in c.drivers if d.delta < 0]
        pos = [d for d in c.drivers if d.delta > 0]
        if neg and abs(neg[0].share) >= 10:
            top = neg[0]
            tips.append(f"先查「{top.member}」：本次 {_fmt_signed(top.delta, plan, c)}、占波动 {abs(top.share):.1f}%{_cite_ev(dim_ev)}，是第一优先级；把它的量价/投入变化拆到周，两周内给出结论。")
        if pos and abs(pos[0].share) >= 10:
            top = pos[0]
            tips.append(f"「{top.member}」贡献了 {abs(top.share):.1f}% 的正向波动（{_fmt_signed(top.delta, plan, c)}），值得把它的做法整理成可复制动作，在下个周期做小范围放量验证。")
    if c.delta_pct is not None and c.delta_pct < -5:
        extra_allowed.add(5.0)
        tips.append(f"整体下滑 {abs(c.delta_pct):.1f}% 已超过 5% 的预警线，建议对该指标开日级监控，把发现问题的时间点从事后复盘前移到当天。")
    if not tips:
        tips.append("本期波动有限，建议把注意力放在结构性指标（结构占比变化、异常点）上，避免只盯总量。")
    for t in tips[:3]:
        md.append(f"- {t}")

    # ---- 口径与边界 ----
    md.append("\n**口径与边界**")
    for s in c.caveats:
        if "数据来源" not in s:
            md.append(f"- {s}")

    # ---- 数据来源（可点击链接）----
    md.extend(_sources_md(evidence))

    text = "\n".join(md)
    allowed = _allowed_numbers(c) | extra_allowed
    ungrounded = _check_grounding(_strip_for_grounding(text), allowed)
    return ReportResult(
        markdown=text,
        grounded=not ungrounded,
        ungrounded_numbers=ungrounded,
        checks=[
            {"name": "数字溯源", "passed": not ungrounded, "detail": "报告中的数字全部可在结论对象中找到出处" if not ungrounded else f"发现 {len(ungrounded)} 个无出处数字：{', '.join(ungrounded[:5])}"},
            {"name": "口径标注", "passed": bool(c.caveats), "detail": "已标注统计区间与数据边界"},
            {"name": "来源标注", "passed": bool(evidence), "detail": f"{len(evidence or [])} 条取数记录在「数据来源」中逐条列出"},
        ],
    )


def _get_compare_label(plan: Plan) -> str:
    label = getattr(plan.compare, "label", "") if plan.compare else ""
    return (label or "上期").replace("环比", "")


def _sources_md(evidence: list[dict] | None) -> list[str]:
    if not evidence:
        return []
    md: list[str] = ["\n**数据来源**"]
    seen: set[str] = set()
    for e in evidence:
        eid = str(e.get("evidence_id") or "")
        if not eid or eid in seen:
            continue
        seen.add(eid)
        label = e.get("source_label") or "、".join(e.get("tables") or []) or "内置演示库"
        url = str(e.get("source_url") or "")
        purpose = str(e.get("purpose") or "取数")
        if url and re.match(r"^https?://", url):
            md.append(f"- ⟦{eid}⟧ [{label}]({url}) · {purpose} · 返回 {e.get('row_count', 0)} 行")
        else:
            md.append(f"- ⟦{eid}⟧ {label} · {purpose} · 返回 {e.get('row_count', 0)} 行")
    md.append("- 提示：报告中的数字角标可点击跳转到对应取数记录（SQL、参数、扫描计划、样本行）。")
    return md


def _strip_for_grounding(text: str) -> str:
    """溯源闸门只校验分析数字；来源链接/URL/文件名里的数字不参与校验。"""
    t = re.sub(r"\*\*数据来源\*\*[\s\S]*?(?=\n###|\Z)", "", text)
    t = re.sub(r"\]\([^)]*\)", "]( )", t)  # 链接 URL 里可能有数字
    return re.sub(r"⟦[^⟧]*⟧", " ", t)


def _allowed_numbers(c: Conclusion) -> set[float]:
    vals: list[float] = []
    for f in c.facts:
        vals.append(abs(f.value))
    if c.current:
        vals.append(abs(c.current))
    if c.previous:
        vals.append(abs(c.previous))
    if c.delta is not None:
        vals.append(abs(c.delta))
    if c.delta_pct is not None:
        vals.append(abs(c.delta_pct))
    for d in c.drivers:
        vals.append(abs(d.delta))
        vals.append(abs(d.share))
    for group in (c.breakdown, *[v for v in (c.breakdown_by_dim or {}).values()]):
        for b in group:
            vals.append(abs(float(b.get("metric", 0))))
    for p in c.series:
        vals.append(abs(p.y))

    out: set[float] = set()
    for v in vals:
        for scaled in (v, v / 1e4, v / 1e8, v / 1e6):
            out.add(round(scaled, 2))

    # 归因附注 / 异常点里的数字也是查询产物（投放变化率等），一并纳入出处白名单
    for text in list(c.caveats) + list(c.anomalies):
        for m in re.finditer(r"(\d+(?:\.\d+)?)\s*(亿|万|%)?", text):
            try:
                v2 = float(m.group(1))
            except ValueError:
                continue
            unit = m.group(2) or ""
            out.add(round(v2, 2))
            if unit == "万":
                out.add(round(v2 / 1e4, 2))
            elif unit == "亿":
                out.add(round(v2 / 1e8, 2))
    return out


COUNT_UNITS = ("天", "个", "轮", "项", "次", "人", "件", "单", "行", "条")


def _check_grounding(text: str, allowed: set[float]) -> list[str]:
    bad: list[str] = []
    # 先把日期剔掉，否则 2026-10-05 会被拆成三个"无出处数字"
    clean = re.sub(r"\d{4}-\d{2}-\d{2}", " ", text)
    clean = re.sub(r"\d{4}\s*年", " ", clean)
    for m in re.finditer(r"(\d+(?:,\d{3})*(?:\.\d+)?)\s*(亿|万|%|个百分点|天|个|轮|项|次)?", clean):
        raw = m.group(1).replace(",", "")
        try:
            v = float(raw)
        except ValueError:
            continue
        unit = m.group(2) or ""
        if unit in COUNT_UNITS:
            continue
        cand = {"": [v], "万": [v / 1e4], "亿": [v / 1e8], "%": [v], "个百分点": [v]}[unit]
        hit = any(any(abs(round(x, 2) - a) <= max(0.06, abs(a) * 0.006) for a in allowed) for x in cand)
        if not hit:
            if v <= 10 and not unit:  # 序号/小计数不参与校验
                continue
            bad.append(m.group(0).strip())
    return bad[:8]
