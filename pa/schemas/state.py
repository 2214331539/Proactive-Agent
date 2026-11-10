"""FastState 数据结构：实体、信念槽、状态变化（设计文档 §4）。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

EntityType = Literal["PERSON", "OBJECT", "PLACE", "DEVICE"]
Source = Literal["seen", "heard", "inferred", "user_confirmed", "consolidator"]
Status = Literal["holds", "stale", "unknown", "conflicted"]
Dynamics = Literal["static", "inertial", "transient"]

# 冲突时的来源优先级（§4.8）
SOURCE_RANK: dict[str, int] = {"user_confirmed": 4, "seen": 3, "heard": 2, "inferred": 1, "consolidator": 1}


class Entity(BaseModel):
    id: str
    type: EntityType
    cls: str
    desc: str = ""
    anchors: dict[str, str] = Field(default_factory=dict)  # {"face": "face_3", "voice": "voice_2"}
    label: str | None = None  # 画面上的 P1 / P2
    identity: Literal["confirmed", "tentative"] = "tentative"
    maybe_same: list[str] = Field(default_factory=list)
    room: str | None = None
    first_seen_ms: int = 0
    last_visible_ms: int = 0
    n_observed: int = 0
    merged_into: str | None = None
    created_version: int = 0  # 创建时的状态版本，用于“并发创建去重”
    last_holder: str | None = None  # 最近一次拿过 / 放下 / 放入它的人（物品与人的相关性）


class Belief(BaseModel):
    key: str  # "phone_1.location"
    value: Any = None  # {"rel": "ON", "ref": "desk_1"}；未知时为 None
    source: Source = "seen"
    status: Status = "holds"
    since_ms: int = 0
    last_support_ms: int = 0
    last_seen: dict | None = None  # 历史观察，永不清除
    evidence: list[str] = Field(default_factory=list)
    contra: list[str] = Field(default_factory=list)
    rev: int = 0
    dynamics: Dynamics = "inertial"
    stale_reason: str | None = None
    support_version: int = 0  # 最后一次被支持时的状态版本；切片早于它的观察不能用来判它过期


class SlotChange(BaseModel):
    key: str
    old: Any = None
    new: Any = None
    old_status: str | None = None
    new_status: str
    old_source: str | None = None
    source: str
    reason: str
    rev: int


class StateDelta(BaseModel):
    delta_id: str
    version_from: int
    version_to: int
    t_event_ms: int
    t_avail_ms: int
    caused_by: str
    changes: list[SlotChange]
