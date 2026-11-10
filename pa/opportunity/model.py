"""C9 机会（Opportunity）：主动决策的基本单位（设计说明 §2.2）。

“某个人，在某件事上，可能会遇到某种不好的后果；系统有一定证据；如果要帮，最晚要在某个时刻之前帮。”
机会只是候选：发现时宁可多报，开不开口由价值门控（C11）决定。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SOURCES = {
    "request": "① 用户请求",
    "template": "② 风险模板",
    "belief_gap": "③ 认知差",
    "expectation": "④ 预期违背",
    "goal": "⑤ 目标推断",
    "forecast": "⑥ 风险预测",
    "search": "⑤ 正在找东西",
    "open": "✦ 开放发现",
}
TERMINAL = {"resolved", "expired", "suppressed"}


@dataclass
class Opportunity:
    key: str
    source: str
    addressee: list[str]
    problem: str
    category: str = "essential"  # safety | essential | convenience | info → costs.yaml loss
    evidence: list[str] = field(default_factory=list)
    deadline: str = "none"  # goal:leave_home | time:<ms> | trigger | asap | none
    eta_kind: str = "action"  # action（让人去做）| locate（告诉位置）
    item: str | None = None
    topic: str | None = None
    need_id: str | None = None
    created_ms: int = 0
    updated_ms: int = 0
    status: str = "active"
    sources: set[str] = field(default_factory=set)
    # 结构化概率因子（生成器给出；p = 各因子相乘，见 value/estimator.py）
    factors: dict[str, float] = field(default_factory=dict)
    # 模型估计（DeepSeek / Jev 回答类型化概率问题，异步刷新）
    p_model: float | None = None
    q_model: float | None = None
    model_why: str | None = None
    model_ms: int | None = None
    model_inflight: bool = False
    # 投递记录
    reminders: int = 0
    ambient_sent: int = 0
    last_delivery_ms: int | None = None
    last_voice_ms: int | None = None
    hot_since_ms: int | None = None
    last_decision: str | None = None
    last_est: Any = None
    draft: Any = None
    draft_inflight: bool = False
    deferred_until_ms: int | None = None
    meta: dict = field(default_factory=dict)

    def public(self) -> dict:
        return {"key": self.key, "source": self.source, "source_label": SOURCES.get(self.source, self.source),
                "sources": sorted(self.sources or {self.source}), "addressee": self.addressee, "problem": self.problem,
                "category": self.category, "deadline": self.deadline, "item": self.item, "topic": self.topic,
                "need_id": self.need_id, "status": self.status, "evidence": self.evidence[:4],
                "reminders": self.reminders, "p_model": self.p_model, "q_model": self.q_model,
                "model_why": self.model_why, "created_ms": self.created_ms}


class OpportunityTable:
    def __init__(self, max_active: int = 40):
        self.opps: dict[str, Opportunity] = {}
        self.max_active = max_active

    def get(self, key: str) -> Opportunity | None:
        return self.opps.get(key)

    def active(self) -> list[Opportunity]:
        return [o for o in self.opps.values() if o.status == "active"]

    def upsert(self, opp: Opportunity, now_ms: int) -> tuple[Opportunity, bool]:
        """返回 (机会, 是否新建)。同键的机会合并来源与证据，因子取最新。"""
        cur = self.opps.get(opp.key)
        if cur is None or cur.status in TERMINAL and now_ms - cur.updated_ms > 120_000 and cur.status != "suppressed":
            opp.created_ms = opp.updated_ms = now_ms
            opp.sources = {opp.source}
            self.opps[opp.key] = opp
            self._trim()
            return opp, True
        if cur.status in TERMINAL:
            return cur, False
        cur.sources.add(opp.source)
        cur.factors.update(opp.factors)
        if opp.source == cur.source and opp.problem != cur.problem:
            cur.problem = opp.problem  # 需求被合并 / 改写后，描述跟着更新
        cur.meta.update({k: v for k, v in opp.meta.items() if v is not None})
        cur.evidence = list(dict.fromkeys(opp.evidence + cur.evidence))[:8]
        cur.updated_ms = now_ms
        if opp.need_id and not cur.need_id:
            cur.need_id = opp.need_id
        if opp.deadline != "none" and cur.deadline in ("none", "trigger"):
            cur.deadline = opp.deadline
        for k in ("item", "topic"):
            if getattr(cur, k) is None and getattr(opp, k) is not None:
                setattr(cur, k, getattr(opp, k))
        return cur, False

    def _trim(self) -> None:
        act = sorted(self.active(), key=lambda o: o.updated_ms)
        for o in act[:max(0, len(act) - self.max_active)]:
            o.status = "expired"
