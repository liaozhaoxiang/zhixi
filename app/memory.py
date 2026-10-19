"""分层记忆。

- 短期：会话上下文（这一轮问了什么、走到哪一步）。有 Redis 用 Redis，没有退化为进程内字典。
- 长期：用户口径偏好（默认看哪个区域、含税不含税、日维度还是月维度），落 SQLite 的 user_pref 表。
  生命周期不同，所以存储选型不同 —— 这是面试里必须讲清的点。
"""
from __future__ import annotations

import json
import time
from collections import defaultdict, deque
from typing import Any

from . import config, db

_LOCAL: dict[str, deque] = defaultdict(lambda: deque(maxlen=20))
_REDIS = None
if config.REDIS_URL:
    try:  # pragma: no cover - 演示环境默认没有 Redis
        import redis  # type: ignore

        _REDIS = redis.Redis.from_url(config.REDIS_URL, decode_responses=True)
        _REDIS.ping()
    except Exception:
        _REDIS = None


def backend_label() -> str:
    return "Redis" if _REDIS is not None else "进程内（Redis 未连接）"


def push_turn(user_id: str, question: str, payload: dict[str, Any]) -> None:
    item = json.dumps({"q": question, "ts": time.time(), "payload": payload}, ensure_ascii=False)
    if _REDIS is not None:
        _REDIS.lpush(f"zhixi:sess:{user_id}", item)
        _REDIS.ltrim(f"zhixi:sess:{user_id}", 0, 19)
    else:
        _LOCAL[user_id].appendleft(item)


def recent_turns(user_id: str, limit: int = 6) -> list[dict[str, Any]]:
    if _REDIS is not None:
        raw = _REDIS.lrange(f"zhixi:sess:{user_id}", 0, limit - 1)
    else:
        raw = list(_LOCAL[user_id])[:limit]
    out = []
    for r in raw:
        try:
            out.append(json.loads(r))
        except json.JSONDecodeError:
            continue
    return out


def get_prefs(user_id: str) -> dict[str, str]:
    rows = db.query("SELECT pref_key, pref_value FROM user_pref WHERE user_id = ?", [user_id])
    return {r["pref_key"]: r["pref_value"] for r in rows}


def set_pref(user_id: str, key: str, value: str) -> None:
    with db.write_conn() as conn:
        conn.execute(
            "INSERT INTO user_pref(user_id,pref_key,pref_value,updated_at) VALUES (?,?,?,datetime('now')) "
            "ON CONFLICT(user_id,pref_key) DO UPDATE SET pref_value=excluded.pref_value, updated_at=excluded.updated_at",
            [user_id, key, value],
        )


def audit(user_id: str, question: str, sql: str, rows: int, elapsed_ms: int, status: str) -> None:
    with db.write_conn() as conn:
        conn.execute(
            "INSERT INTO audit_log(ts,user_id,question,sql,rows_returned,elapsed_ms,status) VALUES (datetime('now'),?,?,?,?,?,?)",
            [user_id, question, sql, rows, elapsed_ms, status],
        )


def recent_audit(limit: int = 12) -> list[dict[str, Any]]:
    return db.query("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", [limit])
