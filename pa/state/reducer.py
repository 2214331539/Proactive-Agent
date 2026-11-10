"""Reducer：确定性地把补丁归约为状态变化（设计文档 §4.6–4.8）。唯一修改 StateStore 的地方。"""
from __future__ import annotations

from typing import Any

from ..schemas import (
    SOURCE_RANK, ActOp, Belief, ConfirmOp, Entity, GoneOp, LinkOp, MergeOp, NewOp, SaidOp, SetOp,
    SlotChange, StateDelta, StatePatch, StillOp, UnsureOp,
)
from .attrs import EXIT_CLASSES, AttrSpec, split_key
from .store import StateStore


class _Ctx:
    def __init__(self, t: int, caused_by: str):
        self.t = t
        self.caused_by = caused_by
        self.changes: list[SlotChange] = []
        self.ignored: list[dict] = []
        self.said: list[SaidOp] = []
        self.other_ops: list[Any] = []  # need_feedback / need_upsert 交给议程
        self.touched: set[str] = set()  # 本补丁中被刷新或写入的槽位
        self.created: list[dict] = []  # 本补丁新建的实体（tmp → id）


class Reducer:
    def __init__(self, attrs: dict[str, AttrSpec]):
        self.attrs = attrs

    # ------------------------------------------------------------------ 主入口
    def apply(self, ops: list[Any], patch: StatePatch, store: StateStore, now_ms: int) -> tuple[StateDelta | None, _Ctx]:
        ctx = _Ctx(patch.t_event_ms, patch.patch_id)
        tmap: dict[str, str] = {}
        # 1) 先创建实体，后续 op 才能引用
        bound: set[str] = set()
        for op in ops:
            if isinstance(op, NewOp):
                dup = self._concurrent_duplicate(store, op, patch, bound)
                if dup is not None:  # VLM 发起时看不到该实体（它在切片之后才被创建）→ 视为同一个
                    tmap[op.tmp] = dup
                    bound.add(dup)
                    ctx.ignored.append({"tmp": op.tmp, "reason": "dedup_concurrent", "bound_to": dup})
                    continue
                eid = store.alloc_id(op.cls)
                tmap[op.tmp] = eid
                bound.add(eid)
                store.entities[eid] = Entity(id=eid, type=op.type, cls=op.cls.lower(), desc=op.desc,
                                             label=op.label, first_seen_ms=ctx.t, last_visible_ms=ctx.t, n_observed=1,
                                             created_version=store.version + 1)
                if op.label:
                    store.entities[eid].anchors.setdefault("label", op.label)
                ctx.created.append({"tmp": op.tmp, "id": eid, "type": op.type, "cls": op.cls.lower(),
                                    "desc": op.desc, "label": op.label})
        R = lambda x: tmap.get(x, x) if isinstance(x, str) else x  # noqa: E731

        def rkey(key: str) -> str:
            kv = split_key(key)
            return f"{R(kv[0])}.{kv[1]}" if kv else key

        def rval(v: Any) -> Any:
            if isinstance(v, dict) and "ref" in v:
                return {**v, "ref": R(v["ref"])}
            return R(v) if isinstance(v, str) else v

        for op in ops:
            if isinstance(op, NewOp) and op.at:
                at, eid = R(op.at), tmap[op.tmp]
                if at not in store.entities or at == eid:
                    continue
                if store.entities[eid].type in ("OBJECT", "DEVICE"):
                    if store.beliefs.get(f"{eid}.location") is None:
                        self._write(ctx, store, f"{eid}.location", {"rel": "ON", "ref": at}, "seen", [], "new:at")
                elif store.entities[at].type == "PLACE":
                    store.entities[eid].room = store.entities[eid].room or at

        # 2) 视野信息
        if patch.view is not None:
            self._apply_view(ctx, store, patch, R)

        # 3) 其余 op 按顺序处理
        for op in ops:
            if isinstance(op, NewOp):
                continue
            if isinstance(op, SetOp):
                self._write(ctx, store, rkey(op.key), rval(op.value), op.src, op.ev, "set")
                self._mark_visible(store, rkey(op.key).split(".", 1)[0], ctx.t, op.src)
            elif isinstance(op, ConfirmOp):
                self._write(ctx, store, rkey(op.key), rval(op.value), "user_confirmed", [], "user:confirm")
            elif isinstance(op, ActOp):
                self._apply_act(ctx, store, op, R)
            elif isinstance(op, StillOp):
                for k in op.keys:
                    self._still(ctx, store, rkey(k))
            elif isinstance(op, GoneOp):
                for k in op.keys:
                    self._gone(ctx, store, rkey(k), op.ev)
            elif isinstance(op, UnsureOp):
                pass  # 不改值：遮挡 ≠ 被拿走
            elif isinstance(op, SaidOp):
                op2 = op.model_copy(update={"speaker": R(op.speaker), "about": [R(a) for a in op.about]})
                store.utterances.append({"t_ms": ctx.t, **op2.model_dump()})
                ctx.said.append(op2)
            elif isinstance(op, LinkOp):
                ent = store.entities.get(R(op.person))
                if ent:
                    a = op.anchor.strip()
                    if a.startswith("voice:"):
                        ent.anchors["voice"] = a[len("voice:"):].strip()
                    elif a.startswith("face_"):
                        ent.anchors["face"] = a
                    else:  # 字幕里的说话人名字（如 Marry）也按声音标签处理
                        ent.anchors["voice"] = a
            elif isinstance(op, MergeOp):
                self._merge(ctx, store, R(op.keep), R(op.drop))
            else:
                ctx.other_ops.append(op)

        # 4) 交互机会规则（§4.7）
        self._interaction_rule(ctx, store, patch)
        return self._finish(ctx, store, now_ms), ctx

    @staticmethod
    def _concurrent_duplicate(store: StateStore, op: NewOp, patch: StatePatch, bound: set[str]) -> str | None:
        """并发创建去重：同类型同类别、且在本补丁的切片版本之后才创建的实体，VLM 不可能知道，按一对一绑定。"""
        if patch.source != "vlm" or patch.slice_version is None:
            return None
        cands = [e for e in store.entities.values()
                 if e.type == op.type and e.cls == (op.cls or "").lower() and not e.merged_into
                 and e.created_version > patch.slice_version and e.id not in bound]
        if op.label:
            cands = [e for e in cands if e.label == op.label] or cands
        return min(cands, key=lambda e: e.created_version).id if cands else None

    def tick(self, store: StateStore, now_ms: int, watermark_ms=None,
             hard_grace_ms: int = 60000) -> StateDelta | None:
        """瞬时属性过期（§4.6 Tick）。

        “TTL 内没有新证据”按感知水位线判断：watermark_ms = 已提交的感知窗口覆盖到的最晚视频时刻。
        只有当已经处理过的观察（而不是还在路上的观察）在 TTL 之后仍没有支持它时才过期；
        感知完全停滞时，用 now - last_support > TTL + hard_grace 兜底。不给 watermark 时退化为按 now 计。
        watermark_ms 也可以是函数 f(support_version) -> 视频时刻 | None：只统计切片版本 ≥ support_version 的观察，
        因为更早发出的窗口根本看不到这条信念，不能拿它们“没支持”来判过期。
        """
        ctx = _Ctx(now_ms, f"tick_{now_ms}")
        for key, b in list(store.beliefs.items()):
            if b.dynamics != "transient" or b.status != "holds":
                continue
            spec = self.attrs.get(key.split(".", 1)[1])
            if not spec or spec.ttl_ms is None:
                continue
            wm = watermark_ms(b.support_version) if callable(watermark_ms) else watermark_ms
            observed_age = (wm if wm is not None else (now_ms if watermark_ms is None else b.last_support_ms)) - b.last_support_ms
            if observed_age <= spec.ttl_ms and now_ms - b.last_support_ms <= spec.ttl_ms + hard_grace_ms:
                continue
            old, old_status, old_source = b.value, b.status, b.source
            if spec.expire_to is not None:
                if b.value == spec.expire_to:
                    continue
                b.value, b.source, b.since_ms = spec.expire_to, "inferred", now_ms
            else:
                b.value, b.status = None, "unknown"
            b.rev += 1
            ctx.changes.append(SlotChange(key=key, old=old, new=b.value, old_status=old_status, new_status=b.status,
                                          old_source=old_source, source=b.source, reason="tick:ttl", rev=b.rev))
        return self._finish(ctx, store, now_ms)

    # ------------------------------------------------------------------ 内部
    def _finish(self, ctx: _Ctx, store: StateStore, now_ms: int) -> StateDelta | None:
        if not ctx.changes:
            return None
        v0 = store.version
        store.version += 1
        return StateDelta(delta_id=f"delta_{store.version:04d}", version_from=v0, version_to=store.version,
                          t_event_ms=ctx.t, t_avail_ms=now_ms, caused_by=ctx.caused_by, changes=ctx.changes)

    def _write(self, ctx: _Ctx, store: StateStore, key: str, value: Any, src: str, ev: list[str], reason: str,
               status: str = "holds") -> None:
        kv = split_key(key)
        if kv is None or kv[0] not in store.entities:
            ctx.ignored.append({"key": key, "reason": "unknown_entity"})
            return
        spec = self.attrs[kv[1]]
        if value is None:
            status = "unknown"
        b = store.beliefs.get(key)
        t = ctx.t
        if b is not None:
            cur_rank, new_rank = SOURCE_RANK[b.source], SOURCE_RANK[src]
            if t < b.last_support_ms and new_rank <= cur_rank:  # 时间守卫：旧观察不覆盖新观察
                ctx.ignored.append({"key": key, "reason": "stale_op"})
                return
            if src == "heard" and b.source in ("seen", "user_confirmed") and b.last_support_ms >= t and b.value != value:
                b.contra.append(f"{ctx.caused_by}:{value}")
                ctx.ignored.append({"key": key, "reason": "heard_vs_seen"})
                return
            old, old_status, old_source = b.value, b.status, b.source
        else:
            b = Belief(key=key, dynamics=spec.dynamics, since_ms=t, last_support_ms=t, source=src)
            old, old_status, old_source = None, None, None
        changed_value = old != value
        b.value, b.source, b.status = value, src, status
        b.last_support_ms = max(b.last_support_ms, t)
        b.support_version = store.version + 1
        b.stale_reason = None
        if changed_value:
            b.since_ms = t
        if src in ("seen", "user_confirmed") and value is not None:
            b.last_seen = {"value": value, "t_ms": t, "evidence": list(ev)}
        if ev:
            b.evidence = list(ev)[-5:]
        store.beliefs[key] = b
        ctx.touched.add(key)
        if kv[1] == "location" and isinstance(value, dict) and value.get("rel") in ("HELD_BY", "WORN_BY"):
            store.entities[kv[0]].last_holder = value["ref"]
        if changed_value or old_status != status or old_source != src:
            b.rev += 1
            ctx.changes.append(SlotChange(key=key, old=old, new=value, old_status=old_status, new_status=status,
                                          old_source=old_source, source=src, reason=reason, rev=b.rev))

    def _still(self, ctx: _Ctx, store: StateStore, key: str) -> None:
        b = store.beliefs.get(key)
        if b is None or b.value is None:
            return
        if ctx.t >= b.last_support_ms:
            b.last_support_ms = ctx.t
            b.support_version = store.version + 1
            if b.source in ("seen", "user_confirmed"):
                b.last_seen = {"value": b.value, "t_ms": ctx.t, "evidence": b.last_seen.get("evidence", []) if b.last_seen else []}
        ctx.touched.add(key)
        self._mark_visible(store, key.split(".", 1)[0], ctx.t, "seen")
        if b.status == "stale":
            old_status = b.status
            b.status, b.stale_reason = "holds", None
            b.rev += 1
            ctx.changes.append(SlotChange(key=key, old=b.value, new=b.value, old_status=old_status, new_status="holds",
                                          old_source=b.source, source=b.source, reason="still:reobserved", rev=b.rev))

    def _gone(self, ctx: _Ctx, store: StateStore, key: str, ev: list[str]) -> None:
        b = store.beliefs.get(key)
        if b is None or b.status == "unknown":
            return
        old, old_status = b.value, b.status
        b.value, b.status, b.since_ms = None, "unknown", ctx.t
        b.last_support_ms = max(b.last_support_ms, ctx.t)
        b.contra.append(f"{ctx.caused_by}:gone")
        b.rev += 1
        ctx.touched.add(key)
        ctx.changes.append(SlotChange(key=key, old=old, new=None, old_status=old_status, new_status="unknown",
                                      old_source=b.source, source=b.source, reason="gone", rev=b.rev))

    def _mark_visible(self, store: StateStore, eid: str, t: int, src: str) -> None:
        e = store.entities.get(eid)
        if e and src == "seen":
            e.last_visible_ms = max(e.last_visible_ms, t)
            e.n_observed += 1
            if e.identity == "tentative" and e.n_observed >= 3 and not e.maybe_same:
                e.identity = "confirmed"

    def _apply_view(self, ctx: _Ctx, store: StateStore, patch: StatePatch, R) -> None:
        view = patch.view.model_copy(update={
            "visible_places": [R(p) for p in patch.view.visible_places if R(p) in store.entities],
            "persons_visible": None if patch.view.persons_visible is None else [R(p) for p in patch.view.persons_visible],
            "room": R(patch.view.room) if patch.view.room else None,
        })
        store.last_view, store.last_view_ms = view, ctx.t
        for p in view.visible_places:
            store.place_last_visible[p] = ctx.t
            self._mark_visible(store, p, ctx.t, "seen")
        if view.persons_visible is not None:
            for pid, e in store.entities.items():
                if e.type != "PERSON":
                    continue
                if pid in view.persons_visible:
                    self._write(ctx, store, f"{pid}.presence", "in_view", "seen", [], "view")
                    self._mark_visible(store, pid, ctx.t, "seen")
                    if view.room and store.etype(view.room) == "PLACE":
                        self._write(ctx, store, f"{pid}.room", view.room, "seen", [], "view")
                elif store.val(f"{pid}.presence") == "in_view":
                    self._write(ctx, store, f"{pid}.presence", "out_of_view", "seen", [], "view")

    def _apply_act(self, ctx: _Ctx, store: StateStore, op: ActOp, R) -> None:
        p, x, tgt = R(op.subj), R(op.obj), R(op.target)
        reason = f"act:{op.type}"
        if op.type in ("PICK_UP", "TAKE_OUT", "PUT_DOWN", "PUT_INTO") and x in store.entities:
            store.entities[x].last_holder = p
        elif op.type == "GIVE" and x in store.entities:
            store.entities[x].last_holder = tgt
        self._mark_visible(store, p, ctx.t, "seen")
        if op.type in ("PICK_UP", "TAKE_OUT"):
            self._write(ctx, store, f"{x}.location", {"rel": "HELD_BY", "ref": p}, "seen", op.ev, reason)
        elif op.type == "PUT_DOWN":
            val = {"rel": "ON", "ref": tgt} if tgt else None
            self._write(ctx, store, f"{x}.location", val, "seen", op.ev, reason)
        elif op.type == "PUT_INTO":
            self._write(ctx, store, f"{x}.location", {"rel": "INSIDE", "ref": tgt}, "seen", op.ev, reason)
        elif op.type == "GIVE":
            self._write(ctx, store, f"{x}.location", {"rel": "HELD_BY", "ref": tgt}, "seen", op.ev, reason)
        elif op.type == "MOVING_TOWARD":
            ent = store.entities.get(x)
            act = "moving_to_exit" if ent and ent.cls in EXIT_CLASSES else "walking"
            self._write(ctx, store, f"{p}.activity", act, "seen", op.ev, reason)
            if ent and ent.type in ("PLACE", "OBJECT", "DEVICE"):
                self._write(ctx, store, f"{p}.near", x, "seen", op.ev, reason)
        elif op.type == "ENTER":
            self._write(ctx, store, f"{p}.presence", "in_view", "seen", op.ev, reason)
            self._write(ctx, store, f"{p}.activity", "entering", "seen", op.ev, reason)
            if store.etype(x) == "PLACE":
                self._write(ctx, store, f"{p}.room", x, "seen", op.ev, reason)
        elif op.type == "EXIT":
            self._write(ctx, store, f"{p}.activity", "leaving", "seen", op.ev, reason)
            self._write(ctx, store, f"{p}.presence", "left", "seen", op.ev, reason)
        elif op.type in ("OPEN", "CLOSE", "TURN_ON", "TURN_OFF"):
            val = {"OPEN": "open", "CLOSE": "closed", "TURN_ON": "on", "TURN_OFF": "off"}[op.type]
            spec = self.attrs["state"]
            if store.etype(x) in spec.applies:
                self._write(ctx, store, f"{x}.state", val, "seen", op.ev, reason)
        if op.type != "MOVING_TOWARD":
            self._mark_visible(store, x, ctx.t, "seen")

    def _interaction_rule(self, ctx: _Ctx, store: StateStore, patch: StatePatch) -> None:
        """有人在看不见的时候靠近过地点 X：X 上未被本补丁确认的物品转为 stale。"""
        view = store.last_view if patch.view is not None else None
        visible = set(view.visible_places) if view else set()
        poor = bool(view and view.quality == "poor")
        for key in list(ctx.touched):
            if not key.endswith(".near"):
                continue
            place = store.val(key)
            if not place or (place in visible and not poor):
                continue
            for obj in store.objects_at(place):
                lk = f"{obj}.location"
                b = store.beliefs.get(lk)
                if b is None or b.status != "holds" or lk in ctx.touched:
                    continue
                b.status, b.stale_reason = "stale", "interaction_unobserved"
                b.rev += 1
                ctx.changes.append(SlotChange(key=lk, old=b.value, new=b.value, old_status="holds", new_status="stale",
                                              old_source=b.source, source=b.source,
                                              reason="rule:interaction_unobserved", rev=b.rev))

    def _merge(self, ctx: _Ctx, store: StateStore, keep: str, drop: str) -> None:
        if keep not in store.entities or drop not in store.entities or keep == drop:
            return
        store.entities[drop].merged_into = keep
        for key, b in list(store.beliefs.items()):
            if key.startswith(drop + "."):
                attr = key.split(".", 1)[1]
                kk = f"{keep}.{attr}"
                if kk not in store.beliefs:
                    self._write(ctx, store, kk, b.value, b.source, b.evidence, "merge")
