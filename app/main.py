"""FastAPI 入口：同步接口 + SSE 流式接口 + 元数据接口。"""
from __future__ import annotations

import json
import time
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from . import config, datasets, db, graph, memory, reflect, tools
from .schemas import DIMENSIONS, METRICS

app = FastAPI(title="智析 · 企业数据智能分析 Agent", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory=str(config.WEB_DIR)), name="static")


class AskBody(BaseModel):
    question: str = Field(min_length=1, max_length=200)
    user_id: str = "demo"


@app.get("/")
def index():
    return FileResponse(config.WEB_DIR / "index.html")


@app.get("/api/health")
def health():
    lo, hi = db.data_bounds()
    return {
        "ok": True,
        "model": config.model_label(),
        "engine": "langgraph" if graph.LANGGRAPH_OK else "native",
        "memory": memory.backend_label(),
        "data": {"from": lo, "to": hi},
        "llm": config.LLM_ENABLED,
    }


@app.get("/api/meta")
def meta():
    lo, hi = db.data_bounds()
    ds = datasets.active()
    return {
        "model": config.model_label(),
        "engine": "langgraph" if graph.LANGGRAPH_OK else "native",
        "memory": memory.backend_label(),
        "data_bounds": {"from": lo, "to": hi},
        "tables": db.table_profile(),
        "metrics": [{"key": k, "label": v["label"], "unit": v["unit"], "table": v["table"]} for k, v in METRICS.items()],
        "dimensions": [{"key": k, "label": v["label"]} for k, v in DIMENSIONS.items()],
        "tools": tools.tool_catalog(),
        "max_rounds": config.MAX_REFLECT_ROUNDS,
        "active_dataset": ds,
        "datasets": [{k: d[k] for k in ("id", "name", "rows", "columns", "uploaded_at")} for d in datasets.list_datasets()],
    }


@app.get("/api/datasets")
def list_datasets():
    return {"items": datasets.list_datasets(), "active": (datasets.active() or {}).get("id")}


@app.post("/api/upload")
async def upload(file: UploadFile = File(...), source_url: str = Form(""), source_name: str = Form("")):
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "空文件")
    if len(raw) > 40 * 1024 * 1024:
        raise HTTPException(413, "文件超过 40MB，请先裁剪到需要分析的列")
    try:
        item = await run_in_threadpool(
            datasets.save_upload,
            file.filename or "upload.csv",
            raw,
            source_url or "",
            source_name or (file.filename or "upload.csv"),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, f"解析失败：{exc}") from exc
    return item


@app.post("/api/datasets/{ds_id}/activate")
def activate_dataset(ds_id: str):
    try:
        return datasets.set_active(None if ds_id == "none" else ds_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.delete("/api/datasets/{ds_id}")
def delete_dataset(ds_id: str):
    datasets.drop(ds_id)
    return {"ok": True, "active": (datasets.active() or {}).get("id")}


def _prefer_col(cols: list[str], keys: tuple[str, ...]) -> str | None:
    """示例问题要像业务同事说的，别把「数量」这种兜底列顶上去。"""
    for k in keys:
        for c in cols:
            if k in str(c):
                return c
    return cols[0] if cols else None


@app.get("/api/examples")
def examples():
    ds = datasets.active()
    if ds:
        m = _prefer_col(
            ds.get("metric_cols") or [],
            ("销售额", "销售金额", "金额", "收入", "营业额", "毛利"),
        )
        dim = _prefer_col(
            ds.get("dim_cols") or [],
            ("品类", "类目", "渠道", "区域", "城市", "门店", "商品"),
        )
        if not m:
            return {"examples": EXAMPLES, "dataset": ds["name"]}
        items: list[str] = []
        if dim:
            items.append(f"各{dim}的{m}排行")
            items.append(f"哪个{dim}{m}最高")
            items.append(f"为什么{dim}的{m}降了")
        items.append(f"最近30天{m}趋势")
        return {"examples": items[:4], "dataset": ds["name"]}
    return {"examples": EXAMPLES}


EXAMPLES = [
    "为什么华东区上周销售额降了",
    "上周各渠道销售额排行",
    "近30天销售额趋势",
    "上个月各品类毛利率对比",
    "近30天各渠道投放ROI",
    "上周抖音渠道的客单价是多少",
    "上上周各区域销售额",
    "近30天库存周转天数按品类",
]


@app.post("/api/analyze")
def analyze(body: AskBody):
    out = graph.run_once(body.question, body.user_id)
    memory.push_turn(body.user_id, body.question, {"plan": str(out.get("plan")), "score": getattr(out.get("reflection"), "score", None)})
    return graph._jsonable(
        {
            "question": body.question,
            "plan": out.get("plan"),
            "mode": out.get("mode"),
            "probe": out.get("probe"),
            "evidence": out.get("evidence"),
            "tool_names": out.get("tool_names"),
            "sql_result": out.get("sql_result"),
            "prev_result": out.get("prev_result"),
            "conclusion": out.get("conclusion"),
            "reflection": out.get("reflection"),
            "report": out.get("report"),
            "rounds": out.get("rounds"),
            "out_of_scope": bool(out.get("out_of_scope")),
            "trace": out.get("trace"),
            "elapsed_ms": out.get("elapsed_ms"),
        }
    )


@app.get("/api/analyze/stream")
def analyze_stream(question: str, user_id: str = "demo"):
    if not question.strip():
        raise HTTPException(400, "question 不能为空")

    def gen():
        t0 = time.perf_counter()
        try:
            for ev in graph.stream_events(question, user_id):
                node = ev["node"]
                kind = "done" if node == "__done__" else "node"
                payload = json.dumps({"type": kind, "node": node, "data": ev["update"]}, ensure_ascii=False)
                yield f"data: {payload}\n\n"
            memory.push_turn(user_id, question, {"ts": time.time()})
            yield "data: {\"type\": \"end\"}\n\n"
        except Exception as exc:  # 任何异常都要让前端看到，而不是静默断开
            yield f'data: {json.dumps({"type": "error", "message": str(exc)}, ensure_ascii=False)}\n\n'

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/audit")
def audit(limit: int = 12):
    return {"items": memory.recent_audit(limit)}


@app.get("/api/memory/{user_id}")
def get_memory(user_id: str):
    return {"prefs": memory.get_prefs(user_id), "turns": memory.recent_turns(user_id), "backend": memory.backend_label()}


class PrefBody(BaseModel):
    key: str
    value: str


@app.post("/api/memory/{user_id}")
def set_memory(user_id: str, body: PrefBody):
    memory.set_pref(user_id, body.key, body.value)
    return {"ok": True, "prefs": memory.get_prefs(user_id)}


@app.get("/api/slots")
def slots(question: str):
    ds = datasets.active()
    if ds:
        from . import generic

        plan = generic.build_plan(question, ds)
    else:
        plan = graph.planner.plan(question, memory.get_prefs("demo"))
    return {"plan": plan.model_dump(), "slots": reflect.slot_table(question, plan)}
