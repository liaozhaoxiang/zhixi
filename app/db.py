"""数据库访问层。

两条连接通道：
- 查询通道：`mode=ro` 只读打开 + `PRAGMA query_only=1`，从驱动层就把写操作堵死。
  这是"SQL 生成错了也只能查不能改"的底线，不是靠提示词约束模型。
- 元数据通道：读写，只给偏好记忆和审计日志用。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator

from .config import DB_PATH


def _row_factory(cursor: sqlite3.Cursor, row: tuple) -> dict[str, Any]:
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}


@contextmanager
def read_conn() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True, timeout=5)
    conn.row_factory = _row_factory
    conn.execute("PRAGMA query_only=1")
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def write_conn() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH.as_posix(), timeout=5)
    conn.row_factory = _row_factory
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def query(sql: str, params: list[Any] | tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with read_conn() as conn:
        cur = conn.execute(sql, params)
        return cur.fetchall()


def scalar(sql: str, params: list[Any] | tuple[Any, ...] = ()) -> Any:
    rows = query(sql, params)
    if not rows:
        return None
    return list(rows[0].values())[0]


def data_bounds() -> tuple[str, str]:
    """数据的时间边界，用来把"上周/上月"锚定到真实数据，而不是系统当前时间。"""
    row = query("SELECT MIN(order_date) AS lo, MAX(order_date) AS hi FROM fact_sales")[0]
    return row["lo"], row["hi"]


def table_profile() -> list[dict[str, Any]]:
    """给前端"数据资产"面板用：每张表的行数和字段数。"""
    out = []
    for t in ("fact_sales", "fact_ads", "fact_inventory", "report_corpus", "dim_product"):
        rows = scalar(f"SELECT COUNT(*) FROM {t}")
        cols = len(query(f"PRAGMA table_info({t})"))
        out.append({"table": t, "rows": rows, "columns": cols})
    return out
