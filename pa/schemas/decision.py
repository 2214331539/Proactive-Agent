"""决策与响应合同（设计文档 §6）。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Mode = Literal["WAIT", "ASK", "REMIND"]
MODE_ORDER = {"WAIT": 0, "ASK": 1, "REMIND": 2}


class Decision(BaseModel):
    decision_id: str
    context_id: str
    need_id: str
    model: str
    status: Literal["ok", "unavailable", "invalid"] = "ok"
    warranted: float | None = None
    mode: Mode | None = None
    mode_probs: dict[str, float] = Field(default_factory=dict)
    applied_mode: Literal["WAIT", "ASK", "REMIND", "DECISION_UNAVAILABLE"] = "WAIT"
    rule_notes: list[str] = Field(default_factory=list)
    reason: str | None = None
    latency_ms: int = 0
    t_request_ms: int = 0
    t_avail_ms: int = 0


class Claim(BaseModel):
    key: str
    value: Any = None
    stated_as: Literal["certain", "probable", "question"] = "probable"


class ResponseDraft(BaseModel):
    draft_id: str
    need_id: str
    mode: Literal["ASK", "REMIND"]
    text: str
    claims: list[Claim] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    context_version: int
    dep_revs: dict[str, int] = Field(default_factory=dict)
    model: str
    origin: Literal["prefetch", "on_demand", "template_fallback"] = "on_demand"
    latency_ms: int = 0
    created_t_ms: int = 0


class Delivery(BaseModel):
    delivery_id: str
    need_id: str
    kind: str
    decision_id: str
    draft_id: str
    mode: str
    text: str
    t_first_visible_ms: int
    fire_t_ms: int
    checks: dict[str, Any] = Field(default_factory=dict)
    feedback_options: list[str] = Field(default_factory=lambda: ["已拿到", "不需要", "不是我的"])


class Blocked(BaseModel):
    need_id: str
    decision_id: str
    reason: str
    t_ms: int


class Feedback(BaseModel):
    feedback_id: str
    delivery_id: str | None = None
    need_id: str
    kind: Literal["done", "not_needed", "wrong"]
    t_ms: int
