"""人物身份的统一键：画面里的实体（person_3）和字幕里的说话人（Sophie）对上号后用同一个键。

VLM 用 link 把人物和声音锚点绑定后，键就是说话人名字；没绑定的人物用实体 ID。
第一人称视角下同一个人可能被识别成多个实体，所以目标、可打扰度等还维护一个“全屋”聚合键 "*"。
"""
from __future__ import annotations

import re

from .store import StateStore

ANY = "*"


def pkey(store: StateStore, eid: str | None) -> str | None:
    if not eid:
        return None
    e = store.entities.get(eid)
    if e is None:
        return eid
    if e.merged_into:
        return pkey(store, e.merged_into)
    return e.anchors.get("voice") or eid


def person_ids(store: StateStore, key: str) -> list[str]:
    """一个人物键对应的所有实体 ID（未合并的）。"""
    return [eid for eid, e in store.entities.items()
            if e.type == "PERSON" and not e.merged_into and (eid == key or e.anchors.get("voice") == key)]


def utter_key(store: StateStore, person_id: str | None, voice: str | None) -> str | None:
    if person_id:
        return pkey(store, person_id)
    return voice or None


def present_keys(store: StateStore, now_ms: int, recent_ms: int = 8000) -> list[str]:
    """此刻在场（画面里或刚刚还在）的人物键。"""
    out = []
    for eid, e in store.entities.items():
        if e.type != "PERSON" or e.merged_into:
            continue
        pres = store.val(f"{eid}.presence")
        if pres == "in_view" or (pres != "left" and now_ms - e.last_visible_ms <= recent_ms):
            k = pkey(store, eid)
            if k and k not in out:
                out.append(k)
    return out


def display_name(store: StateStore, key: str) -> str:
    if key == ANY:
        return "大家"
    if key in store.entities:
        e = store.entities[key]
        label = e.label if e.label and re.fullmatch(r"P\d+", e.label) else None  # VLM 偶尔把临时 ID（n2）写进 label
        return e.anchors.get("voice") or label or key
    return key
