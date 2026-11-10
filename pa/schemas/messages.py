"""跨模块消息（设计文档 §2.4、§3）。图像数组不进入 Pydantic 模型，由 FrameGate 的缓冲持有。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class FrameRef(BaseModel):
    frame_id: str
    pts_ms: int
    t_avail_ms: int
    change_score: float = 0.0
    path: str | None = None


class WindowRequest(BaseModel):
    window_id: str
    seq: int
    start_ms: int
    end_ms: int
    t_request_ms: int
    trigger: Literal["change", "utterance", "heartbeat", "focus", "initial"]
    frame_ids: list[str]
    utt_ids: list[str] = Field(default_factory=list)
    focus_entities: list[str] = Field(default_factory=list)


class Utterance(BaseModel):
    utt_id: str
    start_ms: int
    end_ms: int
    text: str
    t_avail_ms: int
    voice_anchor: str | None = None
    person_id: str | None = None
    asr_conf: float | None = None


class FaceBox(BaseModel):
    frame_id: str
    bbox: tuple[int, int, int, int]
    face_anchor: str
    label: str  # "P1"
    score: float = 0.0


class SliceEntity(BaseModel):
    id: str
    type: str
    cls: str
    desc: str = ""
    label: str | None = None
    anchors: dict[str, str] = Field(default_factory=dict)
    beliefs: dict[str, str] = Field(default_factory=dict)
    check: bool = False


class StateSlice(BaseModel):
    version: int
    t_ms: int
    entities: list[SliceEntity]
    places: list[SliceEntity]
    check_keys: list[str] = Field(default_factory=list)
    open_needs: list[str] = Field(default_factory=list)


class Episode(BaseModel):
    episode_id: str
    session_id: str
    t_start_ms: int
    t_end_ms: int
    t_avail_ms: int
    entity_ids: list[str]
    text: str
