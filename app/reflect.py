"""自反思节点。

三件硬检查（权重最高，不靠模型自评）：
1. 槽位覆盖：问题里点到的维度、过滤条件、时间，在结论里有没有对应产物；
2. 结果自洽：本期 - 上期 是否等于变化额；各维度贡献之和是否约等于总变化；
3. 数据边界：查询区间是否落在数据范围内，结果是否为空。

模型自评（有 key 时）只作为辅助信号，避免"它总说自己没问题"。
"""
from __future__ import annotations

import re

from pydantic import BaseModel

from .db import data_bounds
from .schemas import Conclusion, Plan, ReflectionResult

TIME_HINTS = ("上周", "本周", "上月", "本月", "昨天", "今天", "近", "月", "季度", "去年", "今年")


class _Check(BaseModel):
    name: str
    passed: bool
    weight: float
    detail: str
    critical: bool = False


def reflect(question: str, plan: Plan, conclusion: Conclusion, rounds: int = 1, bounds: tuple[str, str] | None = None) -> ReflectionResult:
    checks: list[_Check] = []

    # 1) 槽位覆盖
    dims_ok = True
    missing: list[str] = []
    for d in plan.dimensions:
        if d in ("day", "month"):
            ok = len(conclusion.series) > 0
        else:
            ok = bool(conclusion.breakdown_by_dim.get(d)) or bool(conclusion.drivers)
        if not ok:
            missing.append(d)
        dims_ok = dims_ok and ok
    checks.append(
        _Check(
            name="槽位覆盖",
            passed=dims_ok,
            weight=25,
            detail="问题要求的维度都产出了对应结果" if dims_ok else f"维度 {missing} 没有对应产物",
        )
    )

    # 2) 结果非空
    non_empty = conclusion.current not in (0, 0.0, None)
    checks.append(
        _Check(
            name="结果非空",
            passed=non_empty,
            weight=25,
            critical=True,
            detail="聚合结果有值" if non_empty else "聚合结果为 0，可能是过滤条件不成立或该区间无数据",
        )
    )

    # 3) 自洽：本期 - 上期 = 变化额
    consistent = True
    detail = "无对比区间，跳过"
    if conclusion.previous is not None and conclusion.delta is not None:
        expect = conclusion.current - conclusion.previous
        consistent = abs(expect - conclusion.delta) <= max(1.0, abs(expect) * 0.01)
        detail = f"本期 {conclusion.current:.2f} - 上期 {conclusion.previous:.2f} = {expect:.2f}，与记录的变化额 {conclusion.delta:.2f} 一致"
    checks.append(_Check(name="总量自洽", passed=consistent, weight=20, detail=detail))

    # 4) 归因可加性：各成员贡献之和 ≈ 总变化
    if conclusion.drivers and conclusion.delta is not None:
        ssum = sum(d.delta for d in conclusion.drivers)
        ok = abs(ssum) <= abs(conclusion.delta) * 1.05 + 1e-6
        checks.append(
            _Check(
                name="贡献可加性",
                passed=ok,
                weight=15,
                detail=f"Top{len(conclusion.drivers)} 贡献合计 {ssum:,.0f}，总变化 {conclusion.delta:,.0f}",
            )
        )

    # 5) 数据边界（上传数据集用自己的时间范围，不能拿内置模型的边界去卡）
    lo, hi = bounds or data_bounds()
    in_bound = plan.time_range.start >= lo and plan.time_range.end <= hi
    checks.append(
        _Check(
            name="数据边界",
            passed=in_bound,
            weight=10,
            detail=f"查询区间 {plan.time_range.start}~{plan.time_range.end}，数据范围 {lo}~{hi}"
            + ("" if in_bound else "（区间超出数据范围，结果会偏小）"),
        )
    )

    # 6) 时间槽位：问题里有时态词，计划却没锚定
    has_time_hint = any(h in question for h in TIME_HINTS)
    explicit = bool(re.search(r"近\s*\d+\s*天", question)) or plan.time_range.label not in ("近30天",)
    time_ok = (not has_time_hint) or explicit
    checks.append(
        _Check(
            name="时间槽位",
            passed=time_ok,
            weight=5,
            detail="问题中的时间表达已落到明确区间" if time_ok else "问题提到时间但计划退回了默认近30天",
        )
    )

    total_w = sum(c.weight for c in checks)
    got = sum(c.weight for c in checks if c.passed)
    score = round(got / total_w * 100, 1)
    issues = [c.detail for c in checks if not c.passed]
    critical_fail = any(c.critical and not c.passed for c in checks)

    return ReflectionResult(
        passed=(score >= 75 and not critical_fail),
        score=score,
        issues=issues,
        rounds=rounds,
        checks=[c.model_dump() for c in checks],
    )


def suggest_followup(plan: Plan, conclusion: Conclusion) -> Plan | None:
    """不通过时给出"补查"方案：换一个还没拆过的维度再下钻一层。

    注意是**替换**维度而不是追加 —— 追加会让一次查询同时按三个维度分组，
    结果集碎成一把沙子，反而更难看出归因。
    """
    ladder = ["channel", "region", "category"]
    if hasattr(plan, "dataset"):  # 上传数据集：从它自己的分类列里换一个下钻
        from . import datasets

        ds = datasets.get(getattr(plan, "dataset", ""))
        if ds:
            for c in ds.get("dim_cols", []):
                if c not in plan.dimensions:
                    return plan.model_copy(update={"dimensions": [c]})
        return None
    for d in ladder:
        if d not in plan.dimensions:
            return plan.model_copy(update={"dimensions": [d], "intent": "attribution" if plan.compare.start else plan.intent})
    return None


def slot_table(question: str, plan: Plan) -> list[dict[str, str]]:
    """给前端展示"槽位识别"过程。"""
    return [
        {"slot": "时间", "value": f"{plan.time_range.label}（{plan.time_range.start} ~ {plan.time_range.end}）"},
        {"slot": "指标", "value": plan.metric},
        {"slot": "维度", "value": ", ".join(plan.dimensions) or "整体"},
        {"slot": "过滤", "value": ", ".join(f"{f.field}={f.value}" for f in plan.filters) or "无"},
        {"slot": "对比", "value": plan.compare.label},
    ]
