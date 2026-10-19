"""全局配置。所有可调项都从环境变量读，方便演示时切换模型 / 数据库。"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = Path(os.getenv("ZHIXI_DB", DATA_DIR / "zhixi.db"))
WEB_DIR = BASE_DIR / "web"

HOST = os.getenv("ZHIXI_HOST", "127.0.0.1")
PORT = int(os.getenv("ZHIXI_PORT", "8010"))

# 接上 Qwen / DeepSeek / 任何 OpenAI 兼容端点后，主管 Agent 的规划由模型完成；
# 没有 key 时自动退回本地规则规划器（离线演示 / 面试现场不怕断网）。
LLM_BASE = os.getenv("ZHIXI_LLM_BASE", "https://dashscope.aliyuncs.com/compatible-mode/v1")
LLM_KEY = os.getenv("ZHIXI_LLM_KEY", "")
LLM_MODEL = os.getenv("ZHIXI_LLM_MODEL", "qwen-plus")
LLM_TIMEOUT = float(os.getenv("ZHIXI_LLM_TIMEOUT", "20"))

# 短期记忆：有 Redis 就用 Redis，没有就用进程内字典（面试演示默认后者）
REDIS_URL = os.getenv("ZHIXI_REDIS_URL", "")

MAX_REFLECT_ROUNDS = 3
SQL_TIMEOUT_SECONDS = 8
SQL_MAX_ROWS = 5000
LLM_ENABLED = bool(LLM_KEY)


def model_label() -> str:
    return f"{LLM_MODEL}（在线）" if LLM_ENABLED else "本地规则规划器（离线）"
