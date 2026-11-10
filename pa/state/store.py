"""StateStore：内存中的当前信念（设计文档 §4.9）。只有 StateActor 能写它；其他模块只读快照。"""
from __future__ import annotations

import copy
import re
from typing import Any, Iterable

from ..schemas import Belief, Entity, SliceEntity, StateSlice, ViewInfo
from .attrs import render_value


class StateStore:
    def __init__(self, camera_mode: str = "fixed"):
        self.camera_mode = camera_mode
        self.version = 0
        self.entities: dict[str, Entity] = {}
        self.beliefs: dict[str, Belief] = {}
        self.place_last_visible: dict[str, int] = {}
        self.last_view: ViewInfo | None = None
        self.last_view_ms = -1
        self.counters: dict[str, int] = {}
        self.utterances: list[dict] = []

    # ---------- 基本读写 ----------
    def alloc_id(self, cls: str) -> str:
        base = re.sub(r"[^a-z0-9]+", "_", cls.lower()).strip("_") or "obj"
        n = self.counters.get(base, 0) + 1
        self.counters[base] = n
        return f"{base}_{n}"

    def etype(self, eid: str | None) -> str | None:
        e = self.entities.get(eid) if eid else None
        return e.type if e else None

    def belief(self, key: str) -> Belief | None:
        return self.beliefs.get(key)

    def val(self, key: str) -> Any:
        b = self.beliefs.get(key)
        return None if b is None else b.value

    # ---------- 派生查询 ----------
    def objects_at(self, place: str) -> list[str]:
        out = []
        for key, b in self.beliefs.items():
            if key.endswith(".location") and isinstance(b.value, dict) and b.value.get("ref") == place:
                out.append(key.split(".", 1)[0])
        return out

    def with_person(self, x: str, p: str, depth: int = 3) -> bool:
        """沿 location 的 INSIDE / ON 链向上，若到达 HELD_BY / WORN_BY p 则为真（§4.3）。"""
        cur = x
        for _ in range(depth + 1):
            b = self.beliefs.get(f"{cur}.location")
            if b is None or b.value is None or b.status == "unknown":
                return False
            rel, ref = b.value["rel"], b.value["ref"]
            if rel in ("HELD_BY", "WORN_BY"):
                return ref == p
            if rel in ("INSIDE", "ON"):
                cur = ref
                continue
            return False
        return False

    def present(self, p: str) -> bool:
        if p not in self.entities:
            return False
        return self.val(f"{p}.presence") != "left"

    def render_belief(self, b: Belief) -> str:
        return f"{render_value(b.value)} ({b.source}, {b.status})"

    # ---------- 快照 ----------
    def snapshot(self) -> dict:
        return {
            "version": self.version,
            "camera_mode": self.camera_mode,
            "entities": {k: e.model_dump() for k, e in self.entities.items()},
            "beliefs": {k: b.model_dump() for k, b in self.beliefs.items()},
            "place_last_visible": dict(self.place_last_visible),
            "counters": dict(self.counters),
            "last_view": self.last_view.model_dump() if self.last_view else None,
            "last_view_ms": self.last_view_ms,
        }

    @classmethod
    def from_snapshot(cls, snap: dict) -> "StateStore":
        s = cls(snap.get("camera_mode", "fixed"))
        s.version = snap["version"]
        s.entities = {k: Entity(**v) for k, v in snap["entities"].items()}
        s.beliefs = {k: Belief(**v) for k, v in snap["beliefs"].items()}
        s.place_last_visible = dict(snap["place_last_visible"])
        s.counters = dict(snap["counters"])
        s.last_view = ViewInfo(**snap["last_view"]) if snap.get("last_view") else None
        s.last_view_ms = snap.get("last_view_ms", -1)
        return s

    def copy(self) -> "StateStore":
        return copy.deepcopy(self)

    def describe(self) -> list[str]:
        """供状态探针 / 调试使用的可读文本。"""
        lines = []
        for eid, e in sorted(self.entities.items()):
            if e.merged_into:
                continue
            attrs = [f"{k.split('.', 1)[1]}={self.render_belief(b)}" for k, b in sorted(self.beliefs.items())
                     if k.startswith(eid + ".")]
            lab = f" [{e.label}]" if e.label else ""
            desc = f" | {e.desc}" if e.desc else ""
            lines.append(f"{eid}{lab} {e.type} {e.cls}{desc}" + (" | " + "; ".join(attrs) if attrs else ""))
        return lines

    # ---------- 状态切片（发给 VLM） ----------
    def slice_for(self, now_ms: int, focus: Iterable[str] = (), max_entities: int = 20,
                  max_places: int = 10, recent_ms: int = 8000, open_needs: Iterable[str] = ()) -> StateSlice:
        visible_places = set(self.last_view.visible_places) if self.last_view else set()
        order: list[str] = []

        def add(eid: str):
            e = self.entities.get(eid)
            if e and not e.merged_into and e.type != "PLACE" and eid not in order:
                order.append(eid)

        for eid in focus:
            add(eid)
        for eid, e in self.entities.items():
            if e.type == "PERSON" and self.present(eid):
                add(eid)
        for eid, e in sorted(self.entities.items(), key=lambda kv: -kv[1].last_visible_ms):
            if e.last_visible_ms >= now_ms - recent_ms:
                add(eid)
        for place in visible_places:
            for eid in self.objects_at(place):
                add(eid)
        order = order[:max_entities]

        check_keys: list[str] = []
        ents: list[SliceEntity] = []
        for eid in order:
            e = self.entities[eid]
            bel = {k.split(".", 1)[1]: self.render_belief(b) for k, b in self.beliefs.items() if k.startswith(eid + ".")}
            chk = False
            loc = self.beliefs.get(f"{eid}.location")
            if loc and isinstance(loc.value, dict) and loc.value.get("ref") in visible_places and loc.status in ("holds", "stale"):
                chk = True
                check_keys.append(loc.key)
            act = self.beliefs.get(f"{eid}.activity")  # 可见人物的瞬时属性也要逐窗确认，否则会被 TTL 误判为 unknown
            if e.type == "PERSON" and act and act.status == "holds" and self.val(f"{eid}.presence") == "in_view":
                chk = True
                check_keys.append(act.key)
            ents.append(SliceEntity(id=eid, type=e.type, cls=e.cls, desc=e.desc, label=e.label,
                                    anchors={k: v for k, v in e.anchors.items() if k == "voice"}, beliefs=bel, check=chk))
        places = [SliceEntity(id=eid, type=e.type, cls=e.cls, desc=e.desc,
                              beliefs={k.split(".", 1)[1]: self.render_belief(b) for k, b in self.beliefs.items()
                                       if k.startswith(eid + ".")})
                  for eid, e in sorted(self.entities.items(), key=lambda kv: -self.place_last_visible.get(kv[0], -1))
                  if e.type == "PLACE" and not e.merged_into][:max_places]
        return StateSlice(version=self.version, t_ms=now_ms, entities=ents, places=places,
                          check_keys=check_keys, open_needs=list(open_needs))
