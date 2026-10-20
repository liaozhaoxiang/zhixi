"""问题范围判定：在取数之前先判断这句话是不是在问数据。

为什么需要它：
过去「你好」「红烧肉怎么做」这类输入会一路走完 规划 → 取数 → 反思 → 撰写，
最后输出一张「合计投放花费 459.2 万元」的 KPI 卡。数据是真的，但答非所问，
比直接说「我不会」更糟：用户会以为系统在回答他的问题。

判定分三层：
1. 有业务信号（指标词 / 维度词 / 时间词 + 操作词 / 数据集列名）→ 放行；
2. 没有业务信号，但像招呼语或能力询问 → 走闲聊应答（不取数）；
3. 其余 → 超出范围，直接说明能力边界。

宁可放行也别误杀：业务信号词表故意给得很宽。
"""
from __future__ import annotations

import re

from .nlu import DIM_WORDS, METRIC_WORDS

_GREET = re.compile(
    r"^\s*(你好|您好|hi|hello|嗨|哈喽|在吗|在么|早|早上好|下午好|晚上好|谢谢|多谢|感谢|再见|拜拜|辛苦了)"
)
_SELF = re.compile(r"(你是谁|你叫什么|你能做什么|你能干嘛|你会什么|怎么用|如何使用|使用说明|帮助|help|功能)")
_TIME = re.compile(
    r"(\d+\s*月|20\d\d|今年|去年|前年|去年同|本月|上月|下月|本周|这周|上周|上上周|昨天|今天|前天|"
    r"最近|近\s*\d+|最近\s*\d+|季度|上半年|下半年|全年|半年|同比|环比|月初|月末)"
)
# 操作词：单独出现不足以判定业务问题，需要搭配指标/维度/时间
_OP = re.compile(
    r"(多少|几[个万千百]|最高|最低|最大|最小|平均|合计|总计|汇总|总量|总额|排名|排行|前\s*\d+|top|"
    r"对比|比较|趋势|走势|变化|波动|占比|分布|构成|结构|拆解|下钻|明细|列表|"
    r"分析|看看|看一下|算一下|统计|汇总|预测|预估|达成|完成率|增长|下降|上升|下滑|跌|涨|异常|原因|为什么)",
)
_OFFTOPIC = re.compile(
    r"(天气|气温|下雨|股票|大盘|彩票|笑话|诗|歌词|作文|翻译|代码|python|java|"
    r"红烧肉|菜谱|做饭|减肥|健身|恋爱|失恋|电影|小说|游戏|旅游|机票|酒店|快递|"
    r"算数|多少加多少|等于几)"
)

# 业务名词：宁可放宽也不能误杀。词表里的每一项都是"看到它就几乎可以肯定在问经营数据"
_BIZ_NOUN = re.compile(
    r"(数据|报表|销售|销量|投放|广告|订单|客户|用户|会员|新客|老客|库存|利润|毛利|成本|费用|"
    r"渠道|区域|大区|地区|门店|仓库|品类|类目|商品|单品|sku|转化|退款|退货|复购|留存|流失|"
    r"曝光|点击|流量|访客|uv|pv|gmv|roi|roas|客单|营收|收入|业绩|预算|周转|履约|发货|时效|"
    r"排期|活动|促销|大促|节日|信息流|短视频|直播|搜索|联盟|品牌|站内|站外|达人|素材|"
    r"618|双11|双十一|618|三八|年货节)"
)


def _has_domain_signal(q: str, ds: dict | None) -> bool:
    low = q.lower()

    if any(w in q for w, _ in METRIC_WORDS):
        return True
    if any(w in q for w, _ in DIM_WORDS):
        return True
    if ds:
        for col in (ds.get("metric_cols") or []) + (ds.get("dim_cols") or []):
            name = str(col)
            if name and name in q:
                return True
    if _TIME.search(q) and _OP.search(q):
        return True
    if _BIZ_NOUN.search(low):
        return True
    return False


def check(question: str, ds: dict | None = None) -> tuple[str, str | None]:
    """返回 (verdict, message)。verdict ∈ ok / smalltalk / out_of_scope。"""
    q = (question or "").strip()
    if not q:
        return "out_of_scope", "请输入一个和数据相关的问题，例如「6 月各渠道销售额是多少」。"

    if _has_domain_signal(q, ds):
        return "ok", None

    if _GREET.match(q) and len(q) <= 16:
        return (
            "smalltalk",
            "你好，我是智析——一个面向经营数据的分析 Agent。\n\n"
            "你可以直接问我数据问题，例如：\n\n"
            "- 6 月销售额是多少\n"
            "- 各渠道投放 ROI 排名\n"
            "- 西南大区为什么零转化\n"
            "- 最近三个月 GMV 趋势",
        )

    if _SELF.search(q):
        return (
            "smalltalk",
            "我负责把经营数据问题拆成取数计划、执行 SQL、做归因分析，并输出带出处的报告。\n\n"
            "能回答的是「数据怎么样」这类问题：销售额、订单、投放、库存、转化、区域/渠道/品类维度、"
            "时间对比与归因。与数据无关的问题我不处理，避免给出没有依据的结论。",
        )

    if _OFFTOPIC.search(q):
        reason = "这个问题与经营数据无关"
    else:
        reason = "我没有从中识别到可查询的指标或维度"

    return (
        "out_of_scope",
        f"{reason}，所以不做分析——凭空给个数字比不回答更糟。\n\n"
        "当前数据源能回答的示例：\n\n"
        "- 6 月销售额是多少 / 各渠道投放 ROI 排名\n"
        "- 最近三个月 GMV 趋势 / 同比下降多少\n"
        "- 西南大区为什么零转化 / 哪个品类毛利最高\n\n"
        "补充一个指标（销售额、订单量、投放花费、转化率、库存……）或一个维度"
        "（区域、渠道、品类、商品、月份），我就能开工。",
    )
