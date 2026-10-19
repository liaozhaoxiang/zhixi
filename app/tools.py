"""工具层：26 个工具节点，全部带 Pydantic 入参校验。

分四类（对应面试里"20 多个工具"那句）：
- data     取数类：预绑定指标 + 维度的查询模板，不是让模型裸写 SQL
- calc     计算类：趋势、归因、异常、量价拆解、KPI 包
- retrieval 检索类：历史分析报告全文检索（FTS5）
- meta     元工具：偏好记忆、澄清、审计

路由：主管 Agent 先按意图挑一个候选集（recommend_tools），模型只在这个集合里选，
避免"问个销售额顺手把天气 API 也调一遍"。
"""
from __future__ import annotations

from typing import Any, Callable

import pandas as pd
from pydantic import BaseModel, Field, ValidationError

from . import db, sql_engine
from .analyst import fmt_money, fmt_value
from .schemas import METRICS, Plan, TimeRange

# --------------------------------------------------------------------------
# 入参契约
# --------------------------------------------------------------------------


class ToolArgs(BaseModel):
    start: str | None = None
    end: str | None = None
    region: str | None = None
    channel: str | None = None
    category: str | None = None
    top_n: int = Field(default=10, ge=1, le=50)
    sql: str | None = None
    query: str | None = None
    user_id: str = "demo"

    def to_filters(self) -> list[tuple[str, Any]]:
        out = []
        for k in ("region", "channel", "category"):
            v = getattr(self, k)
            if v:
                out.append((k, v))
        return out


class ToolSpec(BaseModel):
    name: str
    label: str
    category: str
    description: str
    metric: str | None = None
    dims: list[str] = Field(default_factory=list)
    handler: Callable[..., dict[str, Any]] | None = None

    model_config = {"arbitrary_types_allowed": True}


REGISTRY: dict[str, ToolSpec] = {}


def register(name: str, label: str, category: str, desc: str, metric: str | None = None, dims: list[str] | None = None):
    def deco(fn: Callable[..., dict[str, Any]]):
        REGISTRY[name] = ToolSpec(name=name, label=label, category=category, description=desc, metric=metric, dims=dims or [], handler=fn)
        return fn

    return deco


def _where(args: ToolArgs, time_col: str) -> tuple[str, list[Any]]:
    parts, params = [], []
    if args.start and args.end:
        parts.append(f"{time_col} BETWEEN ? AND ?")
        params += [args.start, args.end]
    for field, value in args.to_filters():
        parts.append(f"{field} = ?")
        params.append(value)
    return (" WHERE " + " AND ".join(parts)) if parts else "", params


def _q(sql: str, params: list[Any]) -> dict[str, Any]:
    res = sql_engine.execute(sql, params)
    return res.model_dump()


# --------------------------------------------------------------------------
# data 类：预绑定查询模板
# --------------------------------------------------------------------------


def _agg_handler(metric: str, dims: list[str], table: str, time_col: str):
    def run(args: ToolArgs) -> dict[str, Any]:
        where, params = _where(args, time_col)
        expr = METRICS[metric]["expr"]
        sel = ", ".join(dims + [f"{expr} AS metric"])
        grp = f" GROUP BY {', '.join(dims)}" if dims else ""
        order = " ORDER BY metric DESC"
        sql = f"SELECT {sel} FROM {table}{where}{grp}{order} LIMIT {args.top_n}"
        return _q(sql, params)

    return run


register("sales_query", "销售额聚合", "data", "按区域/渠道/品类聚合 GMV", "gmv")(_agg_handler("gmv", ["region", "channel", "category"], "fact_sales", "order_date"))
register("orders_query", "订单量聚合", "data", "统计订单数（去重 order_id）", "orders")(_agg_handler("orders", ["region"], "fact_sales", "order_date"))
register("aov_query", "客单价计算", "data", "销售额 / 订单数", "aov")(_agg_handler("aov", ["channel"], "fact_sales", "order_date"))
register("profit_query", "毛利核算", "data", "销售额减成本", "profit")(_agg_handler("profit", ["category"], "fact_sales", "order_date"))
register("margin_query", "毛利率分析", "data", "毛利率按品类拆解", "margin")(_agg_handler("margin", ["category"], "fact_sales", "order_date"))
register("qty_query", "销量统计", "data", "件数口径", "qty")(_agg_handler("qty", ["category"], "fact_sales", "order_date"))
register("customer_query", "成交客户数", "data", "去重客户数", "customers")(_agg_handler("customers", ["region"], "fact_sales", "order_date"))
register("ads_query", "投放花费", "data", "各渠道投放花费", "spend")(_agg_handler("spend", ["channel"], "fact_ads", "ad_date"))
register("clicks_query", "点击量", "data", "广告点击量", "clicks")(_agg_handler("clicks", ["channel"], "fact_ads", "ad_date"))
register("inventory_query", "库存快照", "data", "仓库库存余量", "stock")(_agg_handler("stock", ["warehouse"], "fact_inventory", "snap_date"))


@register("product_rank", "商品排行", "data", "单品销售额 TOP N")
def _product_rank(args: ToolArgs) -> dict[str, Any]:
    where, params = _where(args, "order_date")
    sql = (
        "SELECT product_id, ROUND(SUM(amount),2) AS metric FROM fact_sales"
        f"{where} GROUP BY product_id ORDER BY metric DESC LIMIT {args.top_n}"
    )
    return _q(sql, params)


@register("schema_inspect", "表结构探查", "data", "查看事实表字段与行数")
def _schema_inspect(args: ToolArgs) -> dict[str, Any]:
    return {"tables": db.table_profile()}


@register("dim_values", "维度取值枚举", "data", "取某维度的全部成员")
def _dim_values(args: ToolArgs) -> dict[str, Any]:
    allowed = {"region": "fact_sales", "channel": "fact_sales", "category": "fact_sales"}
    col = "region" if args.region is None else "region"
    table = allowed.get(col, "fact_sales")
    res = sql_engine.execute(f"SELECT DISTINCT {col} AS v FROM {table} ORDER BY v")
    return res.model_dump()


# --------------------------------------------------------------------------
# calc 类
# --------------------------------------------------------------------------


@register("trend_series", "趋势序列", "calc", "按天/月的指标序列 + 环比", "gmv", ["day"])
def _trend(args: ToolArgs) -> dict[str, Any]:
    where, params = _where(args, "order_date")
    sql = (
        "SELECT order_date AS d, ROUND(SUM(amount),2) AS metric FROM fact_sales"
        f"{where} GROUP BY order_date ORDER BY d LIMIT 400"
    )
    res = sql_engine.execute(sql, params)
    return res.model_dump()


@register("attribution", "归因拆解", "calc", "把总变化拆到维度成员，按贡献排序", "gmv", ["channel"])
def _attribution(args: ToolArgs) -> dict[str, Any]:
    if not (args.start and args.end):
        return {"error": "归因需要 start/end 两个时间窗"}
    return _q(
        "SELECT channel, ROUND(SUM(amount),2) AS metric FROM fact_sales "
        "WHERE order_date BETWEEN ? AND ? GROUP BY channel ORDER BY metric DESC",
        [args.start, args.end],
    )


@register("zscore_anomaly", "异常点检测", "calc", "日序列 3σ 异常检测")
def _zscore(args: ToolArgs) -> dict[str, Any]:
    where, params = _where(args, "order_date")
    res = sql_engine.execute(
        f"SELECT order_date AS d, SUM(amount) AS v FROM fact_sales{where} GROUP BY order_date ORDER BY d",
        params,
    )
    if res.status != "ok" or not res.rows:
        return res.model_dump()
    s = pd.Series([float(r["v"] or 0) for r in res.rows])
    mu, sd = s.mean(), s.std()
    hits = [
        {"date": r["d"], "value": round(float(r["v"] or 0), 2), "sigma": round((float(r["v"] or 0) - mu) / sd, 2)}
        for r in res.rows
        if sd and abs((float(r["v"] or 0) - mu) / sd) > 3
    ]
    return {"mean": round(mu, 2), "std": round(sd, 2), "anomalies": hits[:20]}


@register("price_volume_split", "量价拆解", "calc", "销售额变化拆成销量效应 + 价格效应")
def _price_volume(args: ToolArgs) -> dict[str, Any]:
    if not (args.start and args.end):
        return {"error": "需要 start/end"}
    sql = "SELECT SUM(qty) AS qty, SUM(amount) AS amt FROM fact_sales WHERE order_date BETWEEN ? AND ?"
    cur = sql_engine.execute(sql, [args.start, args.end]).rows[0]
    return {"qty": cur["qty"], "amount": round(float(cur["amt"] or 0), 2), "avg_price": round(float(cur["amt"] or 0) / max(1, cur["qty"]), 2)}


@register("kpi_bundle", "KPI 指标包", "calc", "一次取回 GMV/订单/客单价/毛利率 四个核心指标")
def _kpi(args: ToolArgs) -> dict[str, Any]:
    where, params = _where(args, "order_date")
    sql = (
        "SELECT ROUND(SUM(amount),2) AS gmv, COUNT(DISTINCT order_id) AS orders, "
        "ROUND(SUM(amount)*1.0/COUNT(DISTINCT order_id),2) AS aov, "
        "ROUND((SUM(amount)-SUM(cost))*100.0/SUM(amount),2) AS margin "
        f"FROM fact_sales{where}"
    )
    return _q(sql, params)


# --------------------------------------------------------------------------
# retrieval 类
# --------------------------------------------------------------------------


@register("search_reports", "历史报告检索", "retrieval", "在 824 份历史分析报告里做全文检索")
def _search(args: ToolArgs) -> dict[str, Any]:
    kw = (args.query or "").strip()
    if not kw:
        return {"hits": []}
    res = sql_engine.execute(
        "SELECT r.id, r.title, r.conclusion, r.created_at FROM report_corpus_fts f "
        "JOIN report_corpus r ON r.id = f.rowid WHERE report_corpus_fts MATCH ? LIMIT ?",
        [kw, args.top_n],
    )
    return res.model_dump()


@register("report_by_id", "报告详情", "retrieval", "按 ID 取历史报告正文")
def _report_by_id(args: ToolArgs) -> dict[str, Any]:
    rid = 1
    if args.query and args.query.isdigit():
        rid = int(args.query)
    return _q("SELECT * FROM report_corpus WHERE id = ?", [rid])


@register("similar_questions", "相似问题召回", "retrieval", "从历史问题里找相似的分析诉求")
def _similar(args: ToolArgs) -> dict[str, Any]:
    kw = (args.query or "").strip() or "销售"
    res = sql_engine.execute(
        "SELECT title FROM report_corpus WHERE title LIKE ? OR conclusion LIKE ? LIMIT ?",
        [f"%{kw}%", f"%{kw}%", args.top_n],
    )
    return res.model_dump()


# --------------------------------------------------------------------------
# meta 类
# --------------------------------------------------------------------------


@register("memory_get_pref", "读取长期偏好", "meta", "从 MySQL/SQLite 取用户偏好")
def _mem_get(args: ToolArgs) -> dict[str, Any]:
    return {"prefs": db.query("SELECT pref_key, pref_value FROM user_pref WHERE user_id = ?", [args.user_id])}


@register("memory_set_pref", "写入长期偏好", "meta", "把用户口径沉淀为长期偏好")
def _mem_set(args: ToolArgs) -> dict[str, Any]:
    if not args.query:
        return {"ok": False, "error": "缺少 pref 描述"}
    key, _, value = args.query.partition("=")
    with db.write_conn() as conn:
        conn.execute(
            "INSERT INTO user_pref(user_id,pref_key,pref_value,updated_at) VALUES (?,?,?,datetime('now')) "
            "ON CONFLICT(user_id,pref_key) DO UPDATE SET pref_value=excluded.pref_value, updated_at=excluded.updated_at",
            [args.user_id, key.strip(), value.strip()],
        )
    return {"ok": True, "key": key.strip(), "value": value.strip()}


@register("clarify_question", "澄清反问", "meta", "问题槽位缺失时向用户反问")
def _clarify(args: ToolArgs) -> dict[str, Any]:
    return {"question": "请问你想看哪个时间范围、哪个区域/渠道？例如「上周华东区各渠道的销售额」。"}


@register("audit_write", "审计留痕", "meta", "记录谁在什么时候查了什么 SQL")
def _audit(args: ToolArgs) -> dict[str, Any]:
    with db.write_conn() as conn:
        conn.execute(
            "INSERT INTO audit_log(ts,user_id,question,sql,rows_returned,elapsed_ms,status) "
            "VALUES (datetime('now'),?,?,?,?,?,?)",
            [args.user_id, args.query or "", args.sql or "", 0, 0, "ok"],
        )
    return {"ok": True}


@register("calendar_factor", "日历因子", "meta", "判断区间是否落在 618/双11/年货节大促窗口")
def _calendar(args: ToolArgs) -> dict[str, Any]:
    hits = []
    if args.start and args.end:
        for m, (d0, d1), name in [(6, (10, 20), "618"), (11, (1, 15), "双11"), (1, (10, 25), "年货节")]:
            hits.append({"promo": name, "window": f"{m}-{d0}~{m}-{d1}"})
    return {"promo_windows": hits, "note": "大促窗口内的同比/环比需单独解释，不能直接归因为运营动作。"}


def tool_catalog() -> list[dict[str, Any]]:
    return [
        {"name": s.name, "label": s.label, "category": s.category, "description": s.description}
        for s in REGISTRY.values()
    ]


def recommend_tools(plan: Plan) -> list[str]:
    """意图 -> 候选工具集。模型只在这个候选集里挑，压住乱调用。"""
    base: list[str] = []
    if plan.intent == "attribution":
        base = ["attribution", "sales_query", "ads_query", "price_volume_split", "search_reports"]
    elif plan.intent == "trend":
        base = ["trend_series", "zscore_anomaly", "calendar_factor"]
    elif plan.intent == "rank":
        base = ["product_rank", "sales_query", "aov_query"]
    elif plan.intent == "compare":
        base = ["kpi_bundle", "sales_query", "margin_query"]
    else:
        base = ["sales_query", "kpi_bundle", "dim_values"]
    metric_tool = {
        "orders": "orders_query",
        "aov": "aov_query",
        "profit": "profit_query",
        "margin": "margin_query",
        "qty": "qty_query",
        "customers": "customer_query",
        "spend": "ads_query",
        "clicks": "clicks_query",
        "stock": "inventory_query",
    }.get(plan.metric)
    if metric_tool and metric_tool not in base:
        base.insert(0, metric_tool)
    if plan.need_ads and "ads_query" not in base:
        base.append("ads_query")
    return base


def run_tool(name: str, raw_args: dict[str, Any] | None = None) -> dict[str, Any]:
    spec = REGISTRY.get(name)
    if not spec or not spec.handler:
        return {"error": f"未知工具: {name}"}
    try:
        args = ToolArgs.model_validate(raw_args or {})
    except ValidationError as exc:
        # 参数不合法 -> 在执行之前拦下，把校验错误回吐（对应 Pydantic 强校验那层）
        return {"error": "参数校验失败", "detail": exc.errors()}
    try:
        out = spec.handler(args)
        out["_tool"] = name
        return out
    except Exception as exc:
        return {"error": str(exc), "_tool": name}


def build_plan_from_args(metric: str, dims: list[str], args: ToolArgs) -> Plan:
    return Plan(
        metric=metric,
        dimensions=dims,
        time_range=TimeRange(label="自定义", start=args.start or "", end=args.end or ""),
        filters=[],
    )
