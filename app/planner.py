"""主管 Agent 的规划器。

在线：走 OpenAI 兼容端点（Qwen / DeepSeek / 内网 vLLM 都行），要求模型只吐 JSON，
再用 Pydantic 做一次强校验；校验失败把错误回吐给模型重试一次，仍失败就直接降级到规则规划器。
离线：nlu.parse_plan 兜底。

这一层的设计目标是"永不返回 500"——面试演示最怕的就是模型抽风把整条链路搞挂。
"""
from __future__ import annotations

import json
import urllib.request
from datetime import date

from pydantic import ValidationError

from . import config, nlu
from .db import data_bounds
from .schemas import DIMENSIONS, METRICS, Plan

SYSTEM_PROMPT = """你是企业数据智能分析系统「智析」的主管 Agent。
把用户的中文问题拆解成一份可执行的 JSON 执行计划，只输出 JSON，不要任何解释文字。

JSON 结构：
{
  "intent": "summary | trend | rank | attribution | compare",
  "metric": "<指标 key>",
  "dimensions": ["<维度 key>", ...],
  "filters": [{"field": "<字段>", "op": "=", "value": "<值>"}],
  "time_range": {"label": "<中文标签>", "start": "YYYY-MM-DD", "end": "YYYY-MM-DD"},
  "compare": {"type": "none|wow|mom|yoy|prev_period", "label": "<中文标签>", "start": "YYYY-MM-DD", "end": "YYYY-MM-DD"},
  "top_n": null,
  "order": "desc",
  "need_ads": false,
  "need_inventory": false
}

可用指标：{metrics}
可用维度：{dims}
可用过滤字段：region(华东/华南/华北/华中/西南) / channel(天猫/京东/抖音/线下) / category / product_id / warehouse

规则：
1. "为什么/原因/归因"这类问题 intent=attribution，必须给出 compare 区间（一般为紧邻的上一个等长周期）。
2. 提到趋势/走势/每天，intent=trend 且 dimensions 含 "day"。
3. 提到排名/TOP/前N，intent=rank，top_n 填数字。
4. 数据最后一天是 {anchor}，所有相对时间（上周、上月、近7天）都以它为锚，不要用系统当前日期。
5. 时间区间必须是 YYYY-MM-DD 的 ISO 格式。
"""


def _post(messages: list[dict[str, str]]) -> str:
    body = json.dumps(
        {"model": config.LLM_MODEL, "messages": messages, "temperature": 0.1, "response_format": {"type": "json_object"}},
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{config.LLM_BASE.rstrip('/')}/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {config.LLM_KEY}"},
    )
    with urllib.request.urlopen(req, timeout=config.LLM_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


def plan(question: str, prefs: dict[str, str] | None = None) -> Plan:
    if not config.LLM_ENABLED:
        return nlu.parse_plan(question, prefs)

    _, hi = data_bounds()
    anchor = date.fromisoformat(hi).isoformat()
    sys_msg = SYSTEM_PROMPT.format(
        metrics=", ".join(f"{k}({v['label']})" for k, v in METRICS.items()),
        dims=", ".join(f"{k}({v['label']})" for k, v in DIMENSIONS.items()),
        anchor=anchor,
    )
    messages = [{"role": "system", "content": sys_msg}, {"role": "user", "content": question}]
    for attempt in range(2):
        try:
            raw = _post(messages)
            raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            payload = json.loads(raw)
            payload["planner"] = config.LLM_MODEL
            return Plan.model_validate(payload)
        except (ValidationError, json.JSONDecodeError, KeyError) as exc:
            if attempt == 0:
                messages.append({"role": "assistant", "content": raw if "raw" in dir() else "{}"})
                messages.append({"role": "user", "content": f"上一次输出没通过 Schema 校验：{exc}. 请只输出修正后的 JSON。"})
            else:
                break
        except Exception:  # 网络/超时/额度问题：直接降级，不让主链路失败
            break
    fallback = nlu.parse_plan(question, prefs)
    fallback.planner = "rule(fallback)"
    return fallback
