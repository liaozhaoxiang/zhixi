"""Pydantic 契约层。

面试口径：所有工具入参、Agent 之间的数据交换都用强类型 Schema，
非法参数在"进入工具执行之前"就被拦下，而不是带着错误打到数据库。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

# --------------------------------------------------------------------------
# 指标字典：指标名 -> SQL 表达式 / 单位 / 是否跨表
# 只允许白名单内的指标参与拼接，杜绝把用户输入当 SQL
# --------------------------------------------------------------------------
METRICS: dict[str, dict[str, Any]] = {
    "gmv": {"label": "销售额", "expr": "SUM(amount)", "unit": "元", "table": "fact_sales", "fmt": "money"},
    "orders": {"label": "订单量", "expr": "COUNT(DISTINCT order_id)", "unit": "单", "table": "fact_sales", "fmt": "int"},
    "aov": {
        "label": "客单价",
        "expr": "ROUND(SUM(amount) * 1.0 / COUNT(DISTINCT order_id), 2)",
        "unit": "元",
        "table": "fact_sales",
        "fmt": "money",
    },
    "profit": {"label": "毛利", "expr": "SUM(amount - cost)", "unit": "元", "table": "fact_sales", "fmt": "money"},
    "margin": {
        "label": "毛利率",
        "expr": "ROUND((SUM(amount) - SUM(cost)) * 100.0 / SUM(amount), 2)",
        "unit": "%",
        "table": "fact_sales",
        "fmt": "pct",
    },
    "qty": {"label": "销量", "expr": "SUM(qty)", "unit": "件", "table": "fact_sales", "fmt": "int"},
    "customers": {
        "label": "成交客户数",
        "expr": "COUNT(DISTINCT customer_id)",
        "unit": "人",
        "table": "fact_sales",
        "fmt": "int",
    },
    "spend": {"label": "投放花费", "expr": "SUM(spend)", "unit": "元", "table": "fact_ads", "fmt": "money"},
    "clicks": {"label": "点击量", "expr": "SUM(clicks)", "unit": "次", "table": "fact_ads", "fmt": "int"},
    "roi": {"label": "投放 ROI", "expr": "NULL", "unit": "倍", "table": "cross", "fmt": "ratio"},
    "stock": {"label": "库存量", "expr": "SUM(stock)", "unit": "件", "table": "fact_inventory", "fmt": "int"},
    "turnover": {"label": "库存周转天数", "expr": "NULL", "unit": "天", "table": "cross", "fmt": "ratio"},
}

DIMENSIONS: dict[str, dict[str, str]] = {
    "region": {"label": "区域", "field": "region", "table": "fact_sales"},
    "channel": {"label": "渠道", "field": "channel", "table": "fact_sales"},
    "category": {"label": "品类", "field": "category", "table": "fact_sales"},
    "product": {"label": "商品", "field": "product_id", "table": "fact_sales"},
    "month": {"label": "月份", "field": "substr(order_date,1,7)", "table": "fact_sales"},
    "day": {"label": "日期", "field": "order_date", "table": "fact_sales"},
    "warehouse": {"label": "仓库", "field": "warehouse", "table": "fact_inventory"},
}

FILTER_FIELDS = {"region", "channel", "category", "product_id", "warehouse"}


class TimeRange(BaseModel):
    label: str = "全部区间"
    start: str
    end: str


class CompareSpec(BaseModel):
    type: Literal["none", "wow", "mom", "yoy", "prev_period"] = "none"
    label: str = "无对比"
    start: str | None = None
    end: str | None = None


class FilterItem(BaseModel):
    field: str
    op: Literal["=", "in", "like"] = "="
    value: Any

    @field_validator("field")
    @classmethod
    def _whitelist(cls, v: str) -> str:
        if v not in FILTER_FIELDS:
            raise ValueError(f"非法的过滤字段: {v}")
        return v


class Plan(BaseModel):
    """主管 Agent 拆解出来的执行计划——子 Agent 只认这个结构，不认自然语言。"""

    intent: Literal["summary", "trend", "rank", "attribution", "compare"] = "summary"
    metric: str = "gmv"
    dimensions: list[str] = Field(default_factory=list)
    filters: list[FilterItem] = Field(default_factory=list)
    time_range: TimeRange
    compare: CompareSpec = Field(default_factory=CompareSpec)
    top_n: int | None = None
    order: Literal["desc", "asc"] = "desc"
    need_ads: bool = False
    need_inventory: bool = False
    clarify: str | None = None
    planner: str = "rule"

    @field_validator("metric")
    @classmethod
    def _metric_whitelist(cls, v: str) -> str:
        if v not in METRICS:
            raise ValueError(f"未知指标: {v}")
        return v

    @field_validator("dimensions")
    @classmethod
    def _dim_whitelist(cls, v: list[str]) -> list[str]:
        for d in v:
            if d not in DIMENSIONS:
                raise ValueError(f"未知维度: {d}")
        return v


class SqlResult(BaseModel):
    """一次取数的完整档案：SQL 本身 + 它落在哪张表 + 扫描计划 + 样本行。

    前端「数据来源」面板就是消费这个对象——每个进报告的数字都必须能指回某条 SqlResult。
    """

    sql: str
    params: list[Any] = Field(default_factory=list)
    columns: list[str] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    row_count: int = 0
    elapsed_ms: int = 0
    status: Literal["ok", "error", "rejected"] = "ok"
    error: str | None = None
    # ---- 溯源信息 ----
    evidence_id: str = ""  # S1 / S2 ...
    purpose: str = ""  # 这条查询在整条链路里干什么
    tables: list[str] = Field(default_factory=list)
    tables_meta: list[dict[str, Any]] = Field(default_factory=list)  # [{table, rows, time_col}]
    sample: list[dict[str, Any]] = Field(default_factory=list)  # 前 N 行原始结果
    query_plan: list[str] = Field(default_factory=list)  # EXPLAIN QUERY PLAN
    source_file: str = ""  # data/zhixi.db
    duration_note: str = ""
    source_label: str = ""  # 数据来源说明（如「上传文件 sales.csv · 1,024 行」）
    source_url: str = ""  # 可点击的来源链接；内置合成数据为空


class Fact(BaseModel):
    label: str
    value: float
    display: str
    unit: str = ""
    source: str = ""


class Driver(BaseModel):
    dimension: str
    member: str
    delta: float
    delta_display: str
    share: float
    note: str = ""


class SeriesPoint(BaseModel):
    x: str
    y: float


class Conclusion(BaseModel):
    """分析推理师的产出：结构化结论对象。撰写官只消费它，拿不到原始数据。"""

    headline: str = ""
    metric: str = "gmv"
    metric_label: str = "销售额"
    unit: str = "元"
    current: float = 0.0
    current_display: str = ""
    previous: float | None = None
    delta: float | None = None
    delta_pct: float | None = None
    facts: list[Fact] = Field(default_factory=list)
    drivers: list[Driver] = Field(default_factory=list)
    series: list[SeriesPoint] = Field(default_factory=list)
    breakdown: list[dict[str, Any]] = Field(default_factory=list)
    breakdown_by_dim: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    chart: dict[str, Any] = Field(default_factory=dict)
    anomalies: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)


class ReflectionResult(BaseModel):
    passed: bool
    score: float
    issues: list[str] = Field(default_factory=list)
    rounds: int = 1
    checks: list[dict[str, Any]] = Field(default_factory=list)


class ReportResult(BaseModel):
    markdown: str
    grounded: bool
    ungrounded_numbers: list[str] = Field(default_factory=list)
    checks: list[dict[str, Any]] = Field(default_factory=list)
