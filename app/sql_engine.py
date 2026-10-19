"""SQL 工程师 Agent 的产出层：Plan -> 参数化 SQL -> 只读执行。

三层防护落在代码上，不是写在提示词里：
1) 生成即校验：字段只能来自白名单，值一律走 ? 占位符，不拼字符串；
2) 执行即兜底：连接是 mode=ro + query_only=1，超时 + 行数上限；
3) 失败即回退：异常原样回吐，由状态图的重试边去找下一个维度补查。
"""
from __future__ import annotations

import re
import time
from typing import Any

from . import config
from .db import read_conn
from .schemas import METRICS, Plan, SqlResult, TimeRange

FORBIDDEN = re.compile(r"\b(insert|update|delete|drop|alter|attach|detach|pragma|create|replace)\b", re.I)
TABLE_REF = re.compile(r'\b(?:from|join)\s+"?([A-Za-z_\u4e00-\u9fa5][A-Za-z0-9_\u4e00-\u9fa5]*)"?', re.I)
_ROW_CACHE: dict[str, int] = {}

TABLE_TIME_COL = {"fact_sales": "order_date", "fact_ads": "ad_date", "fact_inventory": "snap_date"}
TABLE_DIMS = {
    "fact_sales": {"region", "channel", "category", "product", "month", "day"},
    "fact_ads": {"region", "channel", "month", "day"},
    "fact_inventory": {"region", "category", "warehouse", "month", "day"},
}
DIM_SQL = {
    "region": "region",
    "channel": "channel",
    "category": "category",
    "product": "product_id",
    "warehouse": "warehouse",
    "month": "{t}.substr({time},1,7)",
    "day": "{t}.{time}",
}


def _dim_expr(dim: str, table: str) -> str:
    raw = DIM_SQL[dim]
    return raw.format(t=table, time=TABLE_TIME_COL[table]) if "{" in raw else raw


def _table_for(plan: Plan) -> str:
    return METRICS[plan.metric]["table"] if METRICS[plan.metric]["table"] in TABLE_TIME_COL else "fact_sales"


def build_agg_sql(plan: Plan, dims: list[str], tr: TimeRange) -> tuple[str, list[Any]]:
    table = _table_for(plan)
    metric = METRICS[plan.metric]
    col = TABLE_TIME_COL[table]
    dims = [d for d in dims if d in TABLE_DIMS[table]]

    select_dims = [f"{_dim_expr(d, table)} AS {d}" for d in dims]
    select_dims.append(f"{metric['expr']} AS metric")
    where = [f"{col} BETWEEN ? AND ?"]
    params: list[Any] = [tr.start, tr.end]

    for f in plan.filters:
        if f.field in {"region", "channel", "category", "warehouse"} and (table != "fact_ads" or f.field != "category"):
            where.append(f"{f.field} = ?")
            params.append(f.value)

    sql = f"SELECT {', '.join(select_dims)} FROM {table} WHERE {' AND '.join(where)}"
    if dims:
        sql += " GROUP BY " + ", ".join(_dim_expr(d, table) for d in dims)
        if plan.intent == "trend":
            # 时间序列必须按时间排，按指标大小排出来的曲线是乱序的
            time_dim = next((d for d in dims if d in ("day", "month")), dims[0])
            sql += f" ORDER BY {_dim_expr(time_dim, table)} ASC"
        else:
            order = "ASC" if plan.order == "asc" else "DESC"
            sql += f" ORDER BY metric {order}"
        sql += f" LIMIT {int(plan.top_n) if plan.top_n else config.SQL_MAX_ROWS}"
    return sql, params


def guard(sql: str) -> str | None:
    body = sql.strip().rstrip(";")
    if not re.match(r"(?is)^(select|with)\b", body):
        return "只允许执行 SELECT 查询"
    if FORBIDDEN.search(body):
        return "SQL 中出现非查询语句"
    if ";" in body:
        return "不允许多语句执行"
    return None


def tables_of(sql: str) -> list[str]:
    """从 SQL 里抽出物理表名（只用于展示溯源，不做权限判断）。"""
    seen: list[str] = []
    for m in TABLE_REF.finditer(sql):
        t = m.group(1)
        if t.upper() in {"SELECT"} or t in seen:
            continue
        seen.append(t)
    return seen


def _table_rows(table: str, conn) -> int | None:
    if table in _ROW_CACHE:
        return _ROW_CACHE[table]
    try:
        n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        cnt = int(list(n.values())[0]) if isinstance(n, dict) else int(n[0])
        _ROW_CACHE[table] = cnt
        return cnt
    except Exception:
        return None


def time_col_of(table: str, conn) -> str | None:
    """猜一张表的时间列：优先 *date / *time / dt。"""
    try:
        cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    except Exception:
        return None
    for cand in ("order_date", "ad_date", "snap_date", "date", "dt", "day", "created_at", "time", "datetime"):
        if cand in cols:
            return cand
    for c in cols:
        if "date" in c.lower() or "time" in c.lower():
            return c
    return None


def describe(sql: str, rows: list[dict[str, Any]], elapsed: int) -> dict[str, Any]:
    """补齐溯源信息：涉及哪些表、每张表多大、走没走索引、结果长什么样。"""
    from .config import DB_PATH

    tables = tables_of(sql)
    meta: list[dict[str, Any]] = []
    plan_lines: list[str] = []
    with read_conn() as conn:
        for t in tables:
            info = conn.execute(f"PRAGMA table_info({t})").fetchall()
            meta.append({"table": t, "rows": _table_rows(t, conn), "columns": len(info), "time_col": time_col_of(t, conn)})
        try:
            for r in conn.execute("EXPLAIN QUERY PLAN " + sql, ()):
                plan_lines.append(str(list(r.values())[-1]))
        except Exception:
            pass
    return {
        "tables": tables,
        "tables_meta": meta,
        "sample": rows[:8],
        "query_plan": plan_lines[:6],
        "source_file": str(DB_PATH).replace("\\", "/"),
        "duration_note": f"返回 {len(rows)} 行，耗时 {elapsed} ms",
    }


def execute(sql: str, params: list[Any] | tuple[Any, ...] = (), purpose: str = "") -> SqlResult:
    problem = guard(sql)
    if problem:
        return SqlResult(sql=sql, status="rejected", error=problem, purpose=purpose)
    t0 = time.perf_counter()
    try:
        with read_conn() as conn:
            conn.set_progress_handler(lambda: 0, 10000)
            cur = conn.execute(sql, params)
            rows = cur.fetchall()
            cols = [c[0] for c in cur.description] if cur.description else []
        elapsed = int((time.perf_counter() - t0) * 1000)
        rows = rows[: config.SQL_MAX_ROWS]
        trace = describe(sql, rows, elapsed)
        return SqlResult(
            sql=sql,
            params=list(params),
            columns=cols,
            rows=rows,
            row_count=len(rows),
            elapsed_ms=elapsed,
            purpose=purpose,
            **trace,
        )
    except Exception as exc:  # 报错原样回吐，交给重试边
        return SqlResult(sql=sql, params=list(params), status="error", error=str(exc), elapsed_ms=int((time.perf_counter() - t0) * 1000), purpose=purpose)


def run_agg(plan: Plan, dims: list[str], tr: TimeRange, purpose: str = "") -> SqlResult:
    sql, params = build_agg_sql(plan, dims, tr)
    if not purpose:
        label = METRICS.get(plan.metric, {}).get("label", plan.metric)
        purpose = f"{tr.label} · {label}" + (f" · 按{dims[0]}拆分" if dims else " · 总量")
    return execute(sql, params, purpose)


def run_cross(plan: Plan, tr: TimeRange, purpose: str = "") -> SqlResult:
    """跨表指标：ROI = 销售额 / 投放花费；库存周转天数 = 平均库存 / 日均出库。"""
    if plan.metric == "roi":
        dim = "channel" if "channel" in plan.dimensions else ("region" if "region" in plan.dimensions else None)
        region = next((f.value for f in plan.filters if f.field == "region"), None)
        g = f"{dim}, " if dim else ""
        grp = f" GROUP BY {dim}" if dim else ""
        cond = " AND region = ?" if (dim == "region" and region) else ""
        params_s: list[Any] = [tr.start, tr.end] + ([region] if cond else [])
        sql = (
            "WITH s AS (SELECT {g}SUM(amount) AS gmv FROM fact_sales "
            "WHERE order_date BETWEEN ? AND ?{cond}{grp}), "
            "a AS (SELECT {g}SUM(spend) AS spend FROM fact_ads "
            "WHERE ad_date BETWEEN ? AND ?{cond}{grp}) "
            "SELECT {sdim}ROUND(s.gmv / NULLIF(a.spend, 0), 2) AS metric, "
            "ROUND(s.gmv, 2) AS gmv, ROUND(a.spend, 2) AS spend FROM s JOIN a {on} "
            "ORDER BY metric DESC LIMIT {lim}"
        ).format(
            g=g,
            cond=cond,
            grp=grp,
            sdim=f"s.{dim}, " if dim else "",
            on=f"ON s.{dim} = a.{dim}" if dim else "ON 1=1",
            lim=int(plan.top_n or 20),
        )
        return execute(sql, params_s + params_s, purpose or f"{tr.label} · 投放ROI（销售/投放两表对齐）")

    # 库存周转天数
    dim = "category" if "category" in plan.dimensions else None
    g = f"{dim}, " if dim else ""
    grp = f" GROUP BY {dim}" if dim else ""
    sql = (
        "SELECT {g}ROUND(AVG(stock) / NULLIF(AVG(outbound), 0), 2) AS metric "
        "FROM fact_inventory WHERE snap_date BETWEEN ? AND ?{grp} "
        "ORDER BY metric DESC LIMIT {lim}"
    ).format(g=g, grp=grp, lim=int(plan.top_n or 20))
    return execute(sql, [tr.start, tr.end], purpose or f"{tr.label} · 库存周转天数")
