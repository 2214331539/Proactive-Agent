"""属性注册表与取值规范化（设计文档 §4.3）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

LOC_RELS = {"ON", "INSIDE", "NEAR", "HELD_BY", "WORN_BY", "IN_ROOM"}
ACTIVITIES = {
    "sitting_working", "standing", "walking", "packing", "moving_to_exit", "leaving",
    "entering", "eating", "cooking", "cleaning", "talking", "resting", "other",
}
EXIT_CLASSES = {"door", "exit", "entrance", "gate", "doorway"}
SURFACE_RELS_FOR_AT = "ON"


@dataclass
class AttrSpec:
    applies: set[str]
    kind: str  # loc | enum | ref
    dynamics: str  # static | inertial | transient
    values: set[str] = field(default_factory=set)
    ref_types: set[str] = field(default_factory=set)
    ttl_ms: int | None = None
    expire_to: str | None = None  # transient 过期后转成的值（如 presence → out_of_view）
    sources: set[str] | None = None  # 允许写入的来源；None 表示不限


def build_attrs(ttl_s: dict[str, float] | None = None) -> dict[str, AttrSpec]:
    ttl = {"activity": 8, "near": 5, "presence": 6, **(ttl_s or {})}
    return {
        "location": AttrSpec({"OBJECT", "DEVICE"}, "loc", "inertial"),
        "state": AttrSpec({"DEVICE", "PLACE"}, "enum", "inertial", values={"on", "off", "open", "closed"}),
        "activity": AttrSpec({"PERSON"}, "enum", "transient", values=ACTIVITIES, ttl_ms=int(ttl["activity"] * 1000)),
        "presence": AttrSpec({"PERSON"}, "enum", "transient", values={"in_view", "out_of_view", "left"},
                             ttl_ms=int(ttl["presence"] * 1000), expire_to="out_of_view"),
        "near": AttrSpec({"PERSON"}, "ref", "transient", ref_types={"PLACE", "OBJECT", "DEVICE"},
                         ttl_ms=int(ttl["near"] * 1000)),
        "room": AttrSpec({"PERSON", "OBJECT", "DEVICE"}, "ref", "inertial", ref_types={"PLACE"}),
        "owner": AttrSpec({"OBJECT", "DEVICE"}, "ref", "static", ref_types={"PERSON"},
                          sources={"heard", "user_confirmed"}),
    }


def split_key(key: str) -> tuple[str, str] | None:
    if not isinstance(key, str) or "." not in key:
        return None
    ent, attr = key.split(".", 1)
    return (ent, attr) if ent and attr else None


def normalize_value(spec: AttrSpec, value: Any, etype_of: Callable[[str], str | None]) -> tuple[bool, Any, str]:
    """把模型输出规范成内部取值；失败时返回 (False, None, 原因)。etype_of(实体ID) 返回实体类型或 None。"""
    if value is None:
        return True, None, ""
    if spec.kind == "loc":
        if isinstance(value, str):
            parts = value.replace(":", " ").split()
            value = {"rel": parts[0], "ref": parts[1]} if len(parts) == 2 else None
        if not isinstance(value, dict):
            return False, None, "loc_format"
        rel = str(value.get("rel", "")).upper()
        ref = value.get("ref")
        if rel not in LOC_RELS:
            return False, None, f"bad_rel:{rel}"
        ref_type = etype_of(ref) if isinstance(ref, str) else None
        if ref_type is None:
            return False, None, f"unknown_ref:{ref}"
        if rel in ("HELD_BY", "WORN_BY") and ref_type != "PERSON":
            return False, None, "held_by_non_person"
        return True, {"rel": rel, "ref": ref}, ""
    if spec.kind == "enum":
        v = str(value).strip().lower()
        if v in spec.values:
            return True, v, ""
        if "other" in spec.values:
            return True, "other", f"mapped_to_other:{v}"
        return False, None, f"bad_enum:{v}"
    if spec.kind == "ref":
        ref_type = etype_of(value) if isinstance(value, str) else None
        if ref_type is None:
            return False, None, f"unknown_ref:{value}"
        if spec.ref_types and ref_type not in spec.ref_types:
            return False, None, f"ref_type:{ref_type}"
        return True, value, ""
    return False, None, "unknown_kind"


def render_value(v: Any) -> str:
    if v is None:
        return "?"
    if isinstance(v, dict) and "rel" in v:
        return f"{v['rel']} {v['ref']}"
    if isinstance(v, list):
        return ",".join(map(str, v))
    return str(v)
