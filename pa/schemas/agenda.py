"""需求议程（设计文档 §5）。条件使用 JSON DSL，见 pa/agenda/dsl.py。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

NeedPhase = Literal[
    "draft", "idle", "armed", "fired", "deciding", "awaiting_feedback",
    "resolved", "dismissed", "expired",
]
TERMINAL_PHASES = {"resolved", "dismissed", "expired"}
EdgeKind = Literal["instantiated", "armed", "disarmed", "fired", "resolved", "dismissed", "expired", "rearmed"]


class Need(BaseModel):
    need_id: str
    kind: str
    origin: Literal["template", "speech", "consolidator"] = "template"
    bindings: dict[str, str]
    arm: dict = Field(default_factory=dict)
    fire: dict = Field(default_factory=dict)
    resolve: dict = Field(default_factory=dict)
    dismiss: dict | None = None
    mode_rule: list[dict] = Field(default_factory=list)
    deps: list[str] = Field(default_factory=list)
    dep_attrs: list[str] = Field(default_factory=list)
    phase: NeedPhase = "idle"
    phase_since_ms: int = 0
    cooldown_until_ms: int = 0
    redecide_after_ms: int = 0
    fire_latched: bool = False
    fire_last_true_ms: int | None = None  # 最近一次 fire 为真的时刻（瞬时信号的宽限）
    arm_false_since_ms: int | None = None
    reminders_sent: int = 0
    max_reminders: int = 2
    cooldown_s: float = 120
    gen: int = 0  # 每次取消预取 / 状态重置时 +1，用来丢弃过期的异步结果
    prefetch: dict | None = None
    edges: list[str] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)  # 语音需求：topic / summary / 来源语句


class NeedEdge(BaseModel):
    edge_id: str
    need_id: str
    kind: str
    edge: EdgeKind
    state_version: int
    t_ms: int
    caused_by: str
    dep_revs: dict[str, int] = Field(default_factory=dict)
    detail: dict[str, Any] = Field(default_factory=dict)
