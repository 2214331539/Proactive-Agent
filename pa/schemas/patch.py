"""StatePatch：VLM / 用户 / 慢环 提出的状态补丁（设计文档 §4.5）。"""
from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from .state import EntityType

ActType = Literal[
    "PICK_UP", "PUT_DOWN", "PUT_INTO", "TAKE_OUT", "GIVE", "MOVING_TOWARD",
    "ENTER", "EXIT", "OPEN", "CLOSE", "TURN_ON", "TURN_OFF",
]
Intent = Literal["request", "commitment", "preference", "info", "none"]


class ViewInfo(BaseModel):
    room: str | None = None
    visible_places: list[str] = Field(default_factory=list)
    persons_visible: list[str] | None = None  # None 表示模型没有报告
    quality: Literal["clear", "partial", "poor"] = "clear"


class NewOp(BaseModel):
    op: Literal["new"] = "new"
    tmp: str
    type: EntityType
    cls: str | None = None  # 真实 VLM 常在 new PERSON 时省略 cls；Validator 会补默认值
    desc: str = ""
    at: str | None = None
    label: str | None = None


class SetOp(BaseModel):
    op: Literal["set"] = "set"
    key: str
    value: Any = None
    src: Literal["seen", "heard", "inferred"] = "seen"
    ev: list[str] = Field(default_factory=list)


class ActOp(BaseModel):
    op: Literal["act"] = "act"
    type: ActType
    subj: str
    obj: str
    target: str | None = None
    ev: list[str] = Field(default_factory=list)


class StillOp(BaseModel):
    op: Literal["still"] = "still"
    keys: list[str]


class GoneOp(BaseModel):
    op: Literal["gone"] = "gone"
    keys: list[str]
    ev: list[str] = Field(default_factory=list)


class UnsureOp(BaseModel):
    op: Literal["unsure"] = "unsure"
    keys: list[str]
    reason: str = "ambiguous"


class SaidOp(BaseModel):
    op: Literal["said"] = "said"
    utt_id: str | None = None
    speaker: str | None = None
    intent: Intent = "none"
    content: str
    about: list[str] = Field(default_factory=list)
    due: dict | None = None


class LinkOp(BaseModel):
    op: Literal["link"] = "link"
    person: str
    anchor: str


class ConfirmOp(BaseModel):
    op: Literal["confirm"] = "confirm"
    key: str
    value: Any = None


class NeedFeedbackOp(BaseModel):
    op: Literal["need_feedback"] = "need_feedback"
    need_id: str
    kind: Literal["done", "not_needed", "wrong"]


class NeedUpsertOp(BaseModel):
    op: Literal["need_upsert"] = "need_upsert"
    need: dict


class MergeOp(BaseModel):
    op: Literal["merge"] = "merge"
    keep: str
    drop: str
    reason: str = ""


Op = Annotated[
    Union[NewOp, SetOp, ActOp, StillOp, GoneOp, UnsureOp, SaidOp, LinkOp,
          ConfirmOp, NeedFeedbackOp, NeedUpsertOp, MergeOp],
    Field(discriminator="op"),
]

ALLOWED_OPS: dict[str, set[str]] = {
    "vlm": {"new", "set", "act", "still", "gone", "unsure", "said", "link"},
    "user": {"confirm", "need_feedback"},
    "consolidator": {"need_upsert", "merge"},
}

_OP_ADAPTER = TypeAdapter(Op)


def parse_ops(raw_ops: list[Any] | None) -> tuple[list[Any], list[dict]]:
    """逐条解析 op；单条不合法只拒绝该条，不连累整个补丁。"""
    ops: list[Any] = []
    rejected: list[dict] = []
    for i, raw in enumerate(raw_ops or []):
        try:
            ops.append(_OP_ADAPTER.validate_python(raw))
        except ValidationError as e:
            rejected.append({"index": i, "op": raw, "reason": "schema", "detail": str(e)[:300]})
    return ops, rejected


class StatePatch(BaseModel):
    patch_id: str
    source: Literal["vlm", "user", "consolidator"]
    window_id: str | None = None
    seq: int | None = None
    slice_version: int | None = None
    model: str | None = None
    prompt_version: str | None = None
    t_event_ms: int
    view: ViewInfo | None = None
    ops: list[Op] = Field(default_factory=list)
    rejected: list[dict] = Field(default_factory=list)
    latency_ms: int | None = None
    failed: bool = False  # 调用失败或输出无法解析：只用于让 Sequencer 放行
    raw: str | None = None
