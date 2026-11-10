"""Validator：补丁进入 Reducer 前的校验（设计文档 §4.6）。不合法的 op 进隔离区，不影响其他 op。"""
from __future__ import annotations

from typing import Any

from ..schemas import (
    ALLOWED_OPS, ActOp, ConfirmOp, GoneOp, LinkOp, NewOp, SaidOp, SetOp, StatePatch, StillOp, UnsureOp,
)
from .attrs import AttrSpec, normalize_value, split_key
from .store import StateStore

# 这些动作会改变 obj.location，因此算作“回答了 obj.location 的 CHECK”
LOCATION_ACTS = {"PICK_UP", "PUT_DOWN", "PUT_INTO", "TAKE_OUT", "GIVE"}


class Validator:
    def __init__(self, attrs: dict[str, AttrSpec]):
        self.attrs = attrs

    def check(self, patch: StatePatch, store: StateStore, check_keys: set[str] | None = None) -> tuple[list[Any], list[dict]]:
        allowed = ALLOWED_OPS[patch.source]
        tmp_types: dict[str, str] = {op.tmp: op.type for op in patch.ops if isinstance(op, NewOp)}

        def etype_of(eid: str | None) -> str | None:
            if eid is None:
                return None
            return tmp_types.get(eid) or store.etype(eid)

        ok: list[Any] = []
        rejected: list[dict] = list(patch.rejected)

        def reject(op, reason):
            rejected.append({"op": op.model_dump() if hasattr(op, "model_dump") else op, "reason": reason})

        mentioned: set[str] = set()
        for op in patch.ops:
            if op.op not in allowed:
                reject(op, f"op_not_allowed_for_{patch.source}")
                continue
            if isinstance(op, NewOp):
                if not op.cls:
                    if op.type != "PERSON":
                        reject(op, "cls_missing")
                        continue
                    op = op.model_copy(update={"cls": "person"})
                if op.at is not None and etype_of(op.at) is None:
                    op = op.model_copy(update={"at": None})  # 地点不认识就丢掉 at，实体照常创建
                ok.append(op)
            elif isinstance(op, (SetOp, ConfirmOp)):
                kv = split_key(op.key)
                if kv is None:
                    reject(op, "bad_key")
                    continue
                ent, attr = kv
                et = etype_of(ent)
                spec = self.attrs.get(attr)
                if et is None:
                    reject(op, f"unknown_entity:{ent}")
                    continue
                if spec is None or et not in spec.applies:
                    reject(op, f"attr_not_applicable:{attr}/{et}")
                    continue
                src = "user_confirmed" if isinstance(op, ConfirmOp) else op.src
                if spec.sources is not None and src not in spec.sources:
                    reject(op, f"source_not_allowed:{src}")
                    continue
                good, value, why = normalize_value(spec, op.value, etype_of)
                if not good:
                    reject(op, why)
                    continue
                ok.append(op.model_copy(update={"value": value}))
                mentioned.add(op.key)
            elif isinstance(op, ActOp):
                if etype_of(op.subj) != "PERSON":
                    reject(op, f"subj_not_person:{op.subj}")
                    continue
                if etype_of(op.obj) is None:
                    reject(op, f"unknown_obj:{op.obj}")
                    continue
                if op.type in ("PUT_INTO", "GIVE") and etype_of(op.target) is None:
                    reject(op, "target_required")
                    continue
                if op.type == "GIVE" and etype_of(op.target) != "PERSON":
                    reject(op, "give_target_not_person")
                    continue
                if op.target is not None and etype_of(op.target) is None:
                    op = op.model_copy(update={"target": None})
                ok.append(op)
                if op.type in LOCATION_ACTS:
                    mentioned.add(f"{op.obj}.location")
            elif isinstance(op, (StillOp, GoneOp, UnsureOp)):
                keys = [k for k in op.keys if k in store.beliefs]
                if not keys:
                    reject(op, "no_known_keys")
                    continue
                ok.append(op.model_copy(update={"keys": keys}))
                mentioned.update(keys)
            elif isinstance(op, LinkOp):
                if etype_of(op.person) != "PERSON":
                    reject(op, "link_person_unknown")
                    continue
                ok.append(op)
            elif isinstance(op, SaidOp):
                if op.speaker is not None and etype_of(op.speaker) != "PERSON":
                    op = op.model_copy(update={"speaker": None})
                ok.append(op)
            else:  # need_feedback / need_upsert / merge：交给下游处理
                ok.append(op)

        # CHECK 完整性：漏报的条目补 unsure(omitted)，既不当“仍在”也不当“已不在”
        if patch.source == "vlm" and check_keys:
            missing = sorted(k for k in check_keys if k not in mentioned and k in store.beliefs)
            if missing:
                ok.append(UnsureOp(keys=missing, reason="omitted"))
        return ok, rejected
