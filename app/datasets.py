"""用户上传数据集的落地层：解析 → 建表 → 字段画像 → 元数据。

设计取舍：
- 文件落 `data/uploads/`，数据入同一个 SQLite（表名 `up_<id>`），这样只读连接、
  参数化 SQL、行数上限这些安全边界对上传数据**同样生效**，不需要第二套通道。
- 字段角色（时间 / 指标 / 维度 / 主键）在入库时推断一次并持久化，规划器直接读，
  避免每次提问都重新猜列含义。
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import time
from pathlib import Path
from typing import Any

import pandas as pd

from . import config
from .db import read_conn, write_conn

UPLOAD_DIR = config.DATA_DIR / "uploads"
META_FILE = config.DATA_DIR / "datasets.json"
MAX_ROWS = 200_000

TIME_HINT = re.compile(r"(date|time|日期|时间|月份|年份|day|dt)", re.I)
ID_HINT = re.compile(r"(^id$|_id$|编号|单号|订单号)", re.I)


def _load_meta() -> dict[str, Any]:
    if META_FILE.exists():
        try:
            return json.loads(META_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"datasets": [], "active": None}


def _save_meta(meta: dict[str, Any]) -> None:
    META_FILE.parent.mkdir(parents=True, exist_ok=True)
    META_FILE.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")


def _safe_col(name: str, idx: int) -> str:
    s = str(name).strip().replace("\ufeff", "")
    s = re.sub(r"[\s\-\/\.]+", "_", s)
    s = re.sub(r"[^\w\u4e00-\u9fa5]", "", s)
    return s or f"col_{idx}"


def _read_any(filename: str, raw: bytes) -> pd.DataFrame:
    low = filename.lower()
    if low.endswith((".xlsx", ".xls")):
        return pd.read_excel(io.BytesIO(raw))
    for enc in ("utf-8-sig", "utf-8", "gb18030", "gbk"):
        try:
            return pd.read_csv(io.BytesIO(raw), encoding=enc, sep=None, engine="python")
        except UnicodeDecodeError:
            continue
        except Exception:
            # 分隔符嗅探失败时退回逗号
            try:
                return pd.read_csv(io.BytesIO(raw), encoding=enc)
            except Exception:
                continue
    raise ValueError("无法解析该文件：请使用 UTF-8/GBK 编码的 CSV 或标准 Excel")


def _infer_role(series: pd.Series, name: str, rows: int) -> tuple[str, str]:
    """返回 (角色, 归一化类型)。角色 ∈ time / metric / dim / id。"""
    nun = int(series.nunique(dropna=True))
    if pd.api.types.is_datetime64_any_dtype(series):
        return "time", "datetime"
    if pd.api.types.is_numeric_dtype(series):
        if ID_HINT.search(name) and nun > rows * 0.8:
            return "id", "number"
        return "metric", "number"
    txt = series.dropna().astype(str).head(50)
    if TIME_HINT.search(name) and len(txt) and txt.str.match(r"^\d{4}[-/]\d{1,2}([-/]\d{1,2})?$").mean() > 0.7:
        return "time", "date"
    if len(txt) and txt.str.match(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}").mean() > 0.85:
        return "time", "date"
    if nun <= 60 or (rows and nun / max(rows, 1) < 0.2):
        return "dim", "text"
    return "id", "text"


def _to_sql_type(dtype: str) -> str:
    return {"number": "REAL", "date": "TEXT", "datetime": "TEXT"}.get(dtype, "TEXT")


def save_upload(filename: str, raw: bytes, source_url: str = "", source_name: str = "") -> dict[str, Any]:
    df = _read_any(filename, raw)
    if df.empty:
        raise ValueError("文件里没有数据行")

    df = df.dropna(axis=1, how="all").head(MAX_ROWS)
    df.columns = [_safe_col(c, i) for i, c in enumerate(df.columns)]
    # 列名去重
    seen: dict[str, int] = {}
    cols = []
    for c in df.columns:
        if c in seen:
            seen[c] += 1
            cols.append(f"{c}_{seen[c]}")
        else:
            seen[c] = 0
            cols.append(c)
    df.columns = cols

    rows = len(df)
    profile = []
    for c in df.columns:
        s = df[c]
        role, kind = _infer_role(s, c, rows)
        sample = [str(v) for v in s.dropna().astype(str).head(4)]
        profile.append(
            {
                "name": c,
                "role": role,
                "type": kind,
                "missing": int(s.isna().sum()),
                "unique": int(s.nunique(dropna=True)),
                "samples": sample,
                # 维度列的候选值留在这里，规划器做等值过滤时不必再查库
                "values": s.dropna().astype(str).value_counts().head(24).index.tolist() if role == "dim" else [],
            }
        )

    # 时间列优先选名字里带 date/日期 的那一个
    time_cols = [p["name"] for p in profile if p["role"] == "time"]
    time_col = next((c for c in time_cols if TIME_HINT.search(c)), time_cols[0] if time_cols else None)
    metric_cols = [p["name"] for p in profile if p["role"] == "metric"]
    dim_cols = [p["name"] for p in profile if p["role"] == "dim"]
    if not metric_cols:
        raise ValueError("没有识别到数值列，无法做聚合分析（请确认表格里有金额/数量这类数字列）")

    # 时间列统一成 YYYY-MM-DD 文本
    if time_col:
        parsed = pd.to_datetime(df[time_col], errors="coerce")
        if parsed.notna().mean() > 0.8:
            df[time_col] = parsed.dt.strftime("%Y-%m-%d")

    dsid = hashlib.md5(f"{filename}{rows}{time.time()}".encode("utf-8")).hexdigest()[:8]
    table = f"up_{dsid}"
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    suffix = Path(filename).suffix.lower() or ".csv"
    (UPLOAD_DIR / f"{dsid}{suffix}").write_bytes(raw)

    col_defs = ", ".join(f'"{p["name"]}" {_to_sql_type(p["type"])}' for p in profile)
    placeholders = ", ".join("?" * len(profile))
    names = [p["name"] for p in profile]
    payload = []
    for rec in df[names].itertuples(index=False, name=None):
        payload.append(tuple(None if (isinstance(v, float) and pd.isna(v)) or v is pd.NaT else v for v in rec))

    with write_conn() as conn:
        conn.execute(f'DROP TABLE IF EXISTS "{table}"')
        conn.execute(f'CREATE TABLE "{table}" ({col_defs})')
        conn.executemany(f'INSERT INTO "{table}" VALUES ({placeholders})', payload)
        conn.execute(f'CREATE INDEX IF NOT EXISTS "idx_{table}_t" ON "{table}" ("{time_col}")') if time_col else None

    meta = _load_meta()
    item = {
        "id": dsid,
        "name": filename,
        "table": table,
        "rows": rows,
        "columns": len(profile),
        "profile": profile,
        "time_col": time_col,
        "metric_cols": metric_cols,
        "dim_cols": dim_cols,
        "file": str(UPLOAD_DIR / f"{dsid}{suffix}").replace("\\", "/"),
        "bytes": len(raw),
        "uploaded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source_url": (source_url or "").strip(),
        "source_name": (source_name or "").strip(),
    }
    meta["datasets"] = [d for d in meta["datasets"] if d["id"] != dsid] + [item]
    meta["active"] = dsid
    _save_meta(meta)
    return item


def list_datasets() -> list[dict[str, Any]]:
    return _load_meta().get("datasets", [])


def get(ds_id: str) -> dict[str, Any] | None:
    return next((d for d in list_datasets() if d["id"] == ds_id), None)


def active() -> dict[str, Any] | None:
    meta = _load_meta()
    return get(meta["active"]) if meta.get("active") else None


def set_active(ds_id: str | None) -> dict[str, Any]:
    meta = _load_meta()
    if ds_id and not any(d["id"] == ds_id for d in meta["datasets"]):
        raise ValueError("数据集不存在")
    meta["active"] = ds_id
    _save_meta(meta)
    return {"active": ds_id}


def drop(ds_id: str) -> None:
    meta = _load_meta()
    item = next((d for d in meta["datasets"] if d["id"] == ds_id), None)
    if not item:
        return
    with write_conn() as conn:
        conn.execute(f'DROP TABLE IF EXISTS "{item["table"]}"')
    p = Path(item["file"])
    if p.exists():
        p.unlink()
    meta["datasets"] = [d for d in meta["datasets"] if d["id"] != ds_id]
    if meta.get("active") == ds_id:
        meta["active"] = meta["datasets"][-1]["id"] if meta["datasets"] else None
    _save_meta(meta)


def value_range(ds: dict[str, Any]) -> tuple[str | None, str | None]:
    """时间列的覆盖范围，用来给相对时间定锚点。"""
    if not ds.get("time_col"):
        return None, None
    with read_conn() as conn:
        row = conn.execute(f'SELECT MIN("{ds["time_col"]}") lo, MAX("{ds["time_col"]}") hi FROM "{ds["table"]}"').fetchone()
    return (row["lo"], row["hi"]) if row else (None, None)
