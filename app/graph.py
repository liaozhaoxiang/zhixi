"""LangGraph 状态图编排：主管 -> 工具路由 -> SQL 工程师 -> 分析推理师 -> 自反思 -> 撰写官。

为什么用状态图而不是线性 AgentExecutor：
- 有明确的**循环**：反思不通过要回到 SQL 工程师补查（最多 3 轮）
- 有**条件分支**：槽位不全直接走澄清，不浪费一次取数
- 每一步状态都能落检查点，断点续跑 / 人工介入才有可能

这里做了双实现：装了 langgraph 就用官方 StateGraph（默认情况）；
没装则退化成行为一致的原生状态机，保证演示环境永远能跑。
"""
from __future__ import annotations

import operator
import time
from typing import Annotated, Any, TypedDict

from . import config, datasets, generic, memory, planner, reflect, reporter, scope, sql_engine, tools
from .analyst import analyze
from .schemas import METRICS, Conclusion, Plan, ReflectionResult, ReportResult, TimeRange

try:  # pragma: no cover
    from langgraph.graph import END, StateGraph

    LANGGRAPH_OK = True
except Exception:  # pragma: no cover
    LANGGRAPH_OK = False


class State(TypedDict, total=False):
    question: str
    user_id: str
    mode: str  # "model" = 内置星型模型 / "dataset" = 用户上传表
    plan: Plan
    tool_names: list[str]
    probe: dict[str, Any]
    sql_result: dict[str, Any]
    prev_result: dict[str, Any] | None
    conclusion: Conclusion
    reflection: ReflectionResult
    report: ReportResult
    rounds: int
    clarify: str | None
    out_of_scope: bool
    trace: Annotated[list[dict[str, Any]], operator.add]
    evidence: Annotated[list[dict[str, Any]], operator.add]


def _log(node: str, text: str, kind: str = "info") -> dict[str, Any]:
    return {"node": node, "text": text, "kind": kind, "ts": round(time.time(), 3)}


def _next_eid(state: State) -> int:
    return len(state.get("evidence", [])) + 1


def _evidence(
    res,
    role: str = "query",
    eid: str | None = None,
    src: tuple[str, str] | None = None,
) -> dict[str, Any]:
    d = res.model_dump() if hasattr(res, "model_dump") else dict(res)
    d["role"] = role
    d["evidence_id"] = eid or d.get("evidence_id") or ""
    if src:
        d["source_label"], d["source_url"] = src
    return d


def _src_of(state: State) -> tuple[str, str]:
    """每条证据都带「数据从哪来」。上传文件给文件名+可选链接；内置库明示合成，不冒充真实来源。"""
    if state.get("mode") == "dataset":
        ds = datasets.get(getattr(state.get("plan"), "dataset", "")) or {}
        rows = f"{ds.get('rows', 0):,} 行"
        label = f"{ds['source_name']} · {rows}" if ds.get("source_name") else f"上传文件 {ds.get('name', '?')} · {rows}"
        return label, ds.get("source_url", "")
    return "内置演示库 zhixi.db · gen_data.py 合成数据（非真实网站来源）", ""


# --------------------------------------------------------------------------
# 节点
# --------------------------------------------------------------------------


def _plan_for_refusal(q: str) -> Plan:
    """拒答 / 闲聊时也要给撰写官一个结构，但这个 plan 不会被拿去取数。

    不直接复用 planner.plan 的结果，是因为它会把「红烧肉」硬塞进 gmv 口径，
    虽然不执行 SQL，但字段会漏到前端槽位面板里，看起来像真的规划过。
    """
    from .db import data_bounds

    lo, hi = data_bounds()
    return Plan(intent="summary", metric="gmv", time_range=TimeRange(label="—", start=lo, end=hi))


def node_supervisor(state: State) -> dict[str, Any]:
    q = state["question"]
    uid = state.get("user_id", "demo")

    # ---------------- 范围闸门：先判断这句话是不是在问数据 ----------------
    # 放在最前面，是为了不浪费一次取数 + 一次反思重试，
    # 也是为了避免输出「数据真实但答非所问」的结论。
    active_ds = datasets.active()
    verdict, scope_msg = scope.check(q, active_ds)
    if verdict != "ok":
        trace = [
            _log("supervisor", f"收到问题：{q}"),
            _log(
                "supervisor",
                "范围判定：超出数据分析范围，不进入取数链路" if verdict == "out_of_scope" else "范围判定：招呼语 / 能力询问，直接应答",
                "warn",
            ),
            _log("supervisor", "跳过 规划 → 取数 → 反思，未消耗查询资源", "muted"),
        ]
        return {
            "plan": _plan_for_refusal(q),
            "mode": "dataset" if active_ds else "model",
            "tool_names": [],
            "clarify": scope_msg,
            "out_of_scope": True,
            "trace": trace,
            "rounds": 0,
        }

    # ---------------- 上传数据集分支 ----------------
    ds = active_ds
    if ds:
        plan = generic.build_plan(q, ds)
        trace = [
            _log("supervisor", f"收到问题：{q}"),
            _log("supervisor", f"数据源：用户上传「{ds['name']}」{ds['rows']:,} 行 / {ds['columns']} 列（表 {ds['table']}）", "muted"),
            _log(
                "supervisor",
                "字段画像：时间列 "
                + (ds["time_col"] or "无")
                + "｜数值列 "
                + (", ".join(ds["metric_cols"][:4]) or "无")
                + "｜分类列 "
                + (", ".join(ds["dim_cols"][:4]) or "无"),
                "muted",
            ),
            _log(
                "supervisor",
                f"选口径：{plan.metric_label}（{plan.agg} on {plan.metric_col}）｜维度 {plan.dim_col or '不分组'}｜意图 {plan.intent}",
            ),
            _log(
                "supervisor",
                f"时间锚定：{plan.time_range.label} {plan.time_range.start} ~ {plan.time_range.end}｜对比：{plan.compare.label}",
            ),
            _log("supervisor", f"过滤条件：{', '.join(f'{f.field}={f.value}' for f in plan.filters) or '无'}"),
        ]
        tool_names: list[str] = []
        if plan.clarify:
            trace.append(_log("supervisor", "槽位不足，转澄清流程", "warn"))
        return {"plan": plan, "mode": "dataset", "tool_names": tool_names, "clarify": plan.clarify, "trace": trace, "rounds": 0}

    # ---------------- 内置星型模型分支 ----------------
    prefs = memory.get_prefs(uid)
    plan = planner.plan(q, prefs)
    trace = [_log("supervisor", f"收到问题：{q}")]
    if prefs:
        trace.append(_log("supervisor", f"命中长期偏好：{', '.join(f'{k}={v}' for k, v in prefs.items())}"))
    trace.append(_log("supervisor", f"规划器：{plan.planner}｜意图 {plan.intent}｜指标 {plan.metric}｜维度 {plan.dimensions or ['整体']}"))
    trace.append(_log("supervisor", f"时间锚定：{plan.time_range.label} {plan.time_range.start} ~ {plan.time_range.end}｜对比：{plan.compare.label}"))
    tool_names = tools.recommend_tools(plan)
    trace.append(_log("supervisor", f"工具路由：候选集 {len(tool_names)} 个 -> {', '.join(tool_names[:5])}"))
    if plan.clarify:
        trace.append(_log("supervisor", "槽位不足，转澄清流程", "warn"))
        return {"plan": plan, "mode": "model", "tool_names": tool_names, "clarify": plan.clarify, "trace": trace, "rounds": 0}
    return {"plan": plan, "mode": "model", "tool_names": tool_names, "clarify": None, "trace": trace, "rounds": 0}


def node_tools(state: State) -> dict[str, Any]:
    plan = state["plan"]
    tr = plan.time_range
    args = {"start": tr.start, "end": tr.end, "top_n": 5, "query": "销售" if plan.metric == "gmv" else plan.metric}
    trace = []
    called = []
    for name in state.get("tool_names", [])[:2]:
        res = tools.run_tool(name, args)
        called.append({"tool": name, "ok": "error" not in res})
        trace.append(_log("tools", f"调用 {name} -> {'ok' if 'error' not in res else res.get('error')}"))
    return {"trace": trace, "sql_result": {"tools_called": called}}


def node_probe(state: State) -> dict[str, Any]:
    """数据摸底：取数之前先确认"表在不在、有多大、覆盖到哪天、字段长什么样"。

    这一步不是为了放慢速度，而是真实的前置校验——计划区间如果超出数据边界，
    这里有义务在写 SQL 之前就喊出来，否则报告会给出一个"看起来很正常"的 0。
    """
    from . import db as _db
    from .schemas import METRICS as _M

    plan = state["plan"]
    tr = plan.time_range
    if state.get("mode") == "dataset":
        return _probe_dataset(state, plan)
    if plan.metric == "roi":
        tables = ["fact_sales", "fact_ads"]
    elif plan.metric == "turnover":
        tables = ["fact_inventory"]
    else:
        t = _M.get(plan.metric, {}).get("table", "fact_sales")
        tables = [t if t in ("fact_sales", "fact_ads", "fact_inventory") else "fact_sales"]

    trace = [_log("probe", f"取数前摸底：计划涉及 {', '.join(tables)}")]
    evidence: list[dict[str, Any]] = []
    eid = _next_eid(state)
    summary: list[dict[str, Any]] = []

    for t in tables:
        col = sql_engine.TABLE_TIME_COL.get(t) or "rowid"
        res = sql_engine.execute(
            f"SELECT * FROM {t} ORDER BY {col} DESC LIMIT 5",
            purpose=f"数据摸底 · 抽样 {t} 最新 5 行",
        )
        if res.status != "ok":
            trace.append(_log("probe", f"{t} 抽样失败：{res.error}", "error"))
            continue
        res.evidence_id = f"S{eid}"
        eid += 1
        evidence.append(_evidence(res, role="probe", src=_src_of(state)))
        meta = (res.tables_meta or [{}])[0]
        lo = _db.scalar(f"SELECT MIN({col}) FROM {t}") if col != "rowid" else None
        hi = _db.scalar(f"SELECT MAX({col}) FROM {t}") if col != "rowid" else None
        summary.append({"table": t, "rows": meta.get("rows"), "time_col": meta.get("time_col"), "from": lo, "to": hi, "columns": res.columns})
        trace.append(
            _log(
                "probe",
                f"{t}：{meta.get('rows'):,} 行 · 时间列 {meta.get('time_col')}（{lo} ~ {hi}）· {len(res.columns)} 个字段",
            )
        )
        trace.append(_log("probe", f"字段口径：{', '.join(res.columns[:10])}{' …' if len(res.columns) > 10 else ''}", "muted"))

    if summary:
        lo0 = min((s["from"] for s in summary if s["from"]), default=None)
        hi0 = max((s["to"] for s in summary if s["to"]), default=None)
        if lo0 and hi0:
            inside = tr.start >= str(lo0) and tr.end <= str(hi0)
            if inside:
                trace.append(_log("probe", f"计划区间 {tr.start} ~ {tr.end} 落在数据覆盖范围内 ✓", "ok"))
            else:
                trace.append(
                    _log(
                        "probe",
                        f"计划区间 {tr.start} ~ {tr.end} 超出数据边界（{lo0} ~ {hi0}），区间外无数据不等于为 0",
                        "warn",
                    )
                )

    return {"trace": trace, "probe": {"tables": summary, "range": [tr.start, tr.end]}, "evidence": evidence}


def _probe_dataset(state: State, plan) -> dict[str, Any]:
    """上传表的摸底：确认行数、时间覆盖、抽样看真实值，而不是直接开始聚合。"""
    ds = datasets.get(getattr(plan, "dataset", "")) or {}
    trace = [_log("probe", f"取数前摸底：上传表 {plan.table}（{ds.get('rows', 0):,} 行 / {ds.get('columns', 0)} 列）")]
    order = f' ORDER BY "{plan.time_col}" DESC' if plan.time_col else ""
    res = sql_engine.execute(
        f'SELECT * FROM "{plan.table}"{order} LIMIT 5',
        purpose=f"数据摸底 · 抽样 {plan.table} 最新 5 行",
    )
    evidence: list[dict[str, Any]] = []
    if res.status != "ok":
        trace.append(_log("probe", f"抽样失败：{res.error}", "error"))
        return {"trace": trace, "probe": {"tables": []}, "evidence": evidence}

    res.evidence_id = f"S{_next_eid(state)}"
    evidence.append(_evidence(res, role="probe", src=_src_of(state)))
    trace.append(_log("probe", f"字段（{len(res.columns)}）：{', '.join(res.columns[:12])}{' …' if len(res.columns) > 12 else ''}"))
    if res.sample:
        first = res.sample[0]
        preview = "，".join(f"{k}={v}" for k, v in list(first.items())[:6])
        trace.append(_log("probe", f"抽样首行：{preview}", "muted"))
    lo, hi = datasets.value_range(ds)
    if lo and hi:
        inside = plan.time_range.start >= str(lo)[:10] and plan.time_range.end <= str(hi)[:10]
        trace.append(
            _log(
                "probe",
                f"数据覆盖 {lo} ~ {hi}；计划区间 {plan.time_range.start} ~ {plan.time_range.end} " + ("在其内 ✓" if inside else "超出边界，会偏小"),
                "ok" if inside else "warn",
            )
        )
    return {
        "trace": trace,
        "probe": {"tables": [{"table": plan.table, "rows": ds.get("rows"), "time_col": plan.time_col, "from": lo, "to": hi}], "range": [plan.time_range.start, plan.time_range.end]},
        "evidence": evidence,
    }


def node_sql(state: State) -> dict[str, Any]:
    plan = state["plan"]
    tr = plan.time_range
    trace = []
    eid = _next_eid(state)
    if state.get("mode") == "dataset":
        cur = generic.sql_total(plan, tr)
    else:
        cur = sql_engine.run_agg(plan, [], tr, f"{tr.label} · {METRICS[plan.metric]['label']} · 总量") if plan.metric not in {"roi", "turnover"} else sql_engine.run_cross(plan, tr)
    cur.evidence_id = f"S{eid}"
    evidence = [_evidence(cur, role="query", src=_src_of(state))]
    trace.append(_log("sql", f"生成 SQL（{len(cur.sql)} 字符，{len(cur.params)} 个绑定参数）→ 记作证据 {cur.evidence_id}"))
    if cur.status != "ok":
        trace.append(_log("sql", f"执行失败：{cur.error}，交给重试边", "error"))
        return {"sql_result": cur.model_dump(), "trace": trace, "evidence": evidence}
    trace.append(
        _log(
            "sql",
            f"命中表 {', '.join(cur.tables)}｜返回 {cur.row_count} 行｜{cur.elapsed_ms} ms｜扫描计划：{(cur.query_plan or ['—'])[0]}",
        )
    )

    prev_dump = None
    if plan.compare.start and plan.compare.end:
        prev_tr = TimeRange(label=plan.compare.label, start=plan.compare.start, end=plan.compare.end)
        if state.get("mode") == "dataset":
            prev = generic.sql_total(plan, prev_tr)
        else:
            prev = (
                sql_engine.run_agg(plan, [], prev_tr, f"{prev_tr.label} · {METRICS[plan.metric]['label']} · 总量")
                if plan.metric not in {"roi", "turnover"}
                else sql_engine.run_cross(plan, prev_tr)
            )
        prev.evidence_id = f"S{eid + 1}"
        prev_dump = prev.model_dump()
        evidence.append(_evidence(prev, role="query", src=_src_of(state)))
        trace.append(_log("sql", f"对比区间 {prev_tr.start} ~ {prev_tr.end} 同口径复算：{prev.row_count} 行 → 证据 {prev.evidence_id}"))

    memory.audit(state.get("user_id", "demo"), state["question"], cur.sql, cur.row_count, cur.elapsed_ms, cur.status)
    return {"sql_result": cur.model_dump(), "prev_result": prev_dump, "trace": trace, "evidence": evidence}


def node_analyst(state: State) -> dict[str, Any]:
    plan = state["plan"]
    from .schemas import SqlResult as _SR

    seed_cur = _SR.model_validate(state["sql_result"]) if state.get("sql_result") else None
    seed_prev = _SR.model_validate(state["prev_result"]) if state.get("prev_result") else None
    if state.get("mode") == "dataset":
        conclusion, cur, prev, used = generic.analyze_dataset(plan, seed_cur, seed_prev)
    else:
        conclusion, cur, prev, used = analyze(plan, seed_cur, seed_prev)
    trace = [
        _log("analyst", f"当前值 {conclusion.metric_label} = {conclusion.current:,.2f}（来自证据 {cur.evidence_id or 'S?'}）"),
    ]
    if conclusion.delta_pct is not None:
        trace.append(_log("analyst", f"环比 {conclusion.delta_pct:+.2f}%（变化额 {conclusion.delta:+,.2f}）"))
    if conclusion.drivers:
        trace.append(
            _log(
                "analyst",
                f"归因拆解完成：Top1 = {conclusion.drivers[0].member}（{conclusion.drivers[0].delta_display}，占 {conclusion.drivers[0].share:+.1f}%）",
            )
        )
    if conclusion.anomalies:
        trace.append(_log("analyst", f"异常检测：{len(conclusion.anomalies)} 个 3σ 异常点", "warn"))

    # 分析阶段新产生的查询（去重掉 SQL 节点已经记录过的那两条）
    seen = {state.get("sql_result", {}).get("sql"), (state.get("prev_result") or {}).get("sql")}
    evidence: list[dict[str, Any]] = []
    eid = _next_eid(state)
    for r in used:
        if r.sql in seen or r.status != "ok":
            continue
        seen.add(r.sql)
        r.evidence_id = f"S{eid}"
        eid += 1
        evidence.append(_evidence(r, role="query", src=_src_of(state)))
    if evidence:
        trace.append(_log("analyst", f"归因过程中新增 {len(evidence)} 条交叉验证查询：" + "、".join(e["evidence_id"] for e in evidence)))

    # 把结论里的数字挂到证据编号上，报告里的角标才有落点
    cur_id = state.get("sql_result", {}).get("evidence_id") or ""
    prev_id = (state.get("prev_result") or {}).get("evidence_id") or ""
    for i, f in enumerate(conclusion.facts):
        if i == 0 and cur_id:
            f.source = cur_id
        elif "上期" in f.label and prev_id:
            f.source = prev_id
        elif f.source and not f.source.startswith("S"):
            f.source = f"{f.source}（派生自 {cur_id}）" if cur_id else f.source

    return {"conclusion": conclusion, "trace": trace, "evidence": evidence}


def node_reflector(state: State) -> dict[str, Any]:
    plan = state["plan"]
    rounds = int(state.get("rounds", 0)) + 1
    bounds = None
    if state.get("mode") == "dataset":
        ds = datasets.get(getattr(plan, "dataset", ""))
        if ds:
            lo, hi = datasets.value_range(ds)
            if lo and hi:
                bounds = (str(lo)[:10], str(hi)[:10])
    res = reflect.reflect(state["question"], plan, state["conclusion"], rounds, bounds)
    trace = [_log("reflector", f"第 {rounds} 轮自检：{res.score} 分，{'通过' if res.passed else '不通过'}")]
    for c in res.checks:
        trace.append(_log("reflector", f"  [{ '✓' if c['passed'] else '✗' }] {c['name']}：{c['detail']}", "ok" if c["passed"] else "warn"))
    update: dict[str, Any] = {"reflection": res, "rounds": rounds, "trace": trace}
    if not res.passed and rounds < config.MAX_REFLECT_ROUNDS:
        nxt = reflect.suggest_followup(plan, state["conclusion"])
        if nxt is not None:
            update["plan"] = nxt
            trace.append(_log("reflector", f"触发补查：维度扩展为 {nxt.dimensions}", "warn"))
    return update


def node_writer(state: State) -> dict[str, Any]:
    if state.get("out_of_scope"):
        c = Conclusion(
            headline="",
            metric="gmv",
            metric_label="—",
            unit="",
            current=0.0,
            current_display="—",
            caveats=["未取数：本条问题不在数据分析范围内"],
        )
        rep = ReportResult(markdown=state["clarify"] or "", grounded=True)
        return {
            "conclusion": c,
            "report": rep,
            "out_of_scope": True,
            "trace": [_log("writer", "输出能力边界说明，未生成任何数字，无需溯源校验", "warn")],
        }
    if state.get("clarify"):
        plan = state["plan"]
        c = Conclusion(headline=state["clarify"], metric=plan.metric, metric_label=plan.metric, caveats=["槽位不足，未取数"])
        rep = reporter.write_report(plan, c, state.get("evidence"))
        return {"conclusion": c, "report": rep, "trace": [_log("writer", "输出澄清反问，未消耗取数资源", "warn")]}
    rep = reporter.write_report(state["plan"], state["conclusion"], state.get("evidence"))
    trace = [_log("writer", f"报告生成 {len(rep.markdown)} 字符，{len(state.get('evidence', []))} 条证据可回溯")]
    trace.append(_log("writer", "数字溯源校验：全部命中结论对象" if rep.grounded else f"拦截无出处数字 {len(rep.ungrounded_numbers)} 个", "ok" if rep.grounded else "error"))
    return {"report": rep, "trace": trace}


# --------------------------------------------------------------------------
# 路由
# --------------------------------------------------------------------------


def route_after_supervisor(state: State) -> str:
    return "writer" if state.get("clarify") else "probe"


def route_after_reflect(state: State) -> str:
    rf = state.get("reflection")
    if rf and not rf.passed and rf.rounds < config.MAX_REFLECT_ROUNDS:
        return "retry"
    return "writer"


def build():
    g = StateGraph(State)
    g.add_node("supervisor", node_supervisor)
    g.add_node("probe", node_probe)
    g.add_node("tools", node_tools)
    g.add_node("sql", node_sql)
    g.add_node("analyst", node_analyst)
    g.add_node("reflector", node_reflector)
    g.add_node("writer", node_writer)
    g.set_entry_point("supervisor")
    g.add_conditional_edges("supervisor", route_after_supervisor, {"probe": "probe", "writer": "writer"})
    g.add_edge("probe", "tools")
    g.add_edge("tools", "sql")
    g.add_edge("sql", "analyst")
    g.add_edge("analyst", "reflector")
    g.add_conditional_edges("reflector", route_after_reflect, {"retry": "sql", "writer": "writer"})
    g.add_edge("writer", END)
    return g.compile()


_GRAPH = build() if LANGGRAPH_OK else None


def _native_order(state: State) -> list[str]:
    """没有 langgraph 时用的等价调度（同一套节点，同一条边）。"""
    order = ["supervisor"]
    if state.get("clarify"):
        return order + ["writer"]
    order += ["probe", "tools", "sql", "analyst", "reflector"]
    while True:
        st = {"reflection": state.get("reflection"), "clarify": state.get("clarify")}
        if route_after_reflect(st) == "retry":
            order += ["sql", "analyst", "reflector"]
        else:
            break
    return order + ["writer"]


ACCUM = ("trace", "evidence")


def _merge(acc: dict[str, Any], upd: dict[str, Any]) -> None:
    """trace / evidence 是累加字段（对应 State 里的 Annotated reducer）。"""
    for k, v in upd.items():
        if k in ACCUM:
            acc[k] = list(acc.get(k, [])) + list(v or [])
        else:
            acc[k] = v


def stream_events(question: str, user_id: str = "demo"):
    """逐节点产出事件，供 SSE 推给前端做实时流水线动画。"""
    state: State = {"question": question, "user_id": user_id, "trace": [], "evidence": [], "rounds": 0}
    t0 = time.perf_counter()
    acc: dict[str, Any] = dict(state)
    if _GRAPH is not None:
        for chunk in _GRAPH.stream(state, stream_mode="updates"):
            for node, upd in chunk.items():
                if not isinstance(upd, dict):
                    continue
                _merge(acc, upd)
                yield {"node": node, "update": _jsonable(upd)}
    else:
        order = _native_order(acc)
        i = 0
        while i < len(order) and i < 12:
            name = order[i]
            fn = {
                "supervisor": node_supervisor,
                "probe": node_probe,
                "tools": node_tools,
                "sql": node_sql,
                "analyst": node_analyst,
                "reflector": node_reflector,
                "writer": node_writer,
            }[name]
            upd = fn(acc)
            _merge(acc, upd)
            yield {"node": name, "update": _jsonable(upd)}
            order = _native_order(acc)
            i += 1

    yield {
        "node": "__done__",
        "update": {
            "elapsed_ms": int((time.perf_counter() - t0) * 1000),
            "engine": "langgraph" if LANGGRAPH_OK else "native",
            "result": _jsonable(
                {
                    "plan": acc.get("plan"),
                    "probe": acc.get("probe"),
                    "sql_result": acc.get("sql_result"),
                    "prev_result": acc.get("prev_result"),
                    "conclusion": acc.get("conclusion"),
                    "reflection": acc.get("reflection"),
                    "report": acc.get("report"),
                    "evidence": acc.get("evidence", []),
                    "rounds": acc.get("rounds", 0),
                    "out_of_scope": bool(acc.get("out_of_scope")),
                    "trace": acc.get("trace", []),
                }
            ),
        },
    }


def _jsonable(obj: Any) -> Any:
    from pydantic import BaseModel

    if isinstance(obj, BaseModel):
        return obj.model_dump()
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    return obj


def run_once(question: str, user_id: str = "demo") -> dict[str, Any]:
    """同步跑完整条链路，返回最终状态。"""
    state: State = {"question": question, "user_id": user_id, "trace": [], "evidence": [], "rounds": 0}
    t0 = time.perf_counter()
    if _GRAPH is not None:
        final: dict[str, Any] = dict(state)
        for chunk in _GRAPH.stream(state, stream_mode="updates"):
            for _node, upd in chunk.items():
                if not isinstance(upd, dict):
                    continue
                _merge(final, upd)
        out = final
    else:
        out = dict(state)
        i = 0
        guard = 0
        while i < len(_native_order(out)) and guard < 12:
            guard += 1
            name = _native_order(out)[i]
            fn = {
                "supervisor": node_supervisor,
                "probe": node_probe,
                "tools": node_tools,
                "sql": node_sql,
                "analyst": node_analyst,
                "reflector": node_reflector,
                "writer": node_writer,
            }[name]
            _merge(out, fn(out))
            i += 1
    out["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
    out["engine"] = "langgraph" if LANGGRAPH_OK else "native"
    return out
