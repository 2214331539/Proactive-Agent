"""触发引擎：模板实例化、条件评估、边沿检测、Need 状态机（设计文档 §5.4）。在 State Actor 的同一步内同步运行。"""
from __future__ import annotations

import itertools
import re
from pathlib import Path
from typing import Any

import yaml

from ..schemas import TERMINAL_PHASES, Need, NeedEdge, StateDelta
from ..state.store import StateStore
from . import dsl

DEFAULT_TEMPLATES = Path(__file__).with_name("templates.yaml")
ACTIVE = {"idle", "armed", "fired", "deciding", "awaiting_feedback"}


def _cls_match(cls: str, allowed: list[str]) -> bool:
    """容忍复数与下划线写法：umbrellas / mobile_phone → umbrella / phone。"""
    c = cls.lower().strip()
    forms = {c, c[:-1] if c.endswith("s") and not c.endswith("ss") else c, c.split("_")[-1]}
    return any(f in allowed for f in forms)


def load_templates(path: str | Path | None = None) -> list[dict]:
    with open(path or DEFAULT_TEMPLATES, encoding="utf-8") as f:
        return yaml.safe_load(f)


class TriggerEngine:
    def __init__(self, templates: list[dict], disarm_debounce_ms: int = 3000, redecide_min_ms: int = 5000,
                 awaiting_timeout_ms: int = 60000):
        self.templates = templates
        self.needs: dict[str, Need] = {}
        self.dep_index: dict[str, set[str]] = {}
        self.attr_index: dict[str, set[str]] = {}
        self.last_eval: dict[str, tuple[bool, bool, bool]] = {}
        self.inst_last: dict[tuple, bool] = {}
        self.prefs: list[dict] = []  # 用户偏好：{"kind","item","until_ms"}
        self.disarm_debounce_ms = disarm_debounce_ms
        self.redecide_min_ms = redecide_min_ms
        self.awaiting_timeout_ms = awaiting_timeout_ms
        self._n_need = 0
        self._n_edge = 0

    # ------------------------------------------------------------ 公共接口
    def active_needs(self) -> list[Need]:
        return [n for n in self.needs.values() if n.phase in ACTIVE]

    def on_delta(self, delta: StateDelta | None, store: StateStore, now_ms: int) -> list[NeedEdge]:
        edges: list[NeedEdge] = []
        caused = delta.delta_id if delta else f"t_{now_ms}"
        edges += self._instantiate(store, now_ms, caused)
        affected: set[str] = set()
        if delta:
            for ch in delta.changes:
                affected |= self.dep_index.get(ch.key, set())
                affected |= self.attr_index.get(ch.key.split(".", 1)[1], set())
        for nid in sorted(affected):
            edges += self._evaluate(self.needs[nid], store, now_ms, caused)
        return edges

    def on_tick(self, store: StateStore, now_ms: int) -> list[NeedEdge]:
        edges: list[NeedEdge] = []
        for need in list(self.needs.values()):
            if need.phase in ("armed", "idle", "awaiting_feedback"):
                edges += self._evaluate(need, store, now_ms, f"tick_{now_ms}")
        return edges

    def mode_cap(self, need: Need, store: StateStore, now_ms: int) -> str:
        for rule in need.mode_rule:
            if dsl.evaluate(rule.get("when") or {}, store, need.bindings, now_ms):
                return rule["cap"]
        return "ASK"

    def dep_revs(self, need: Need, store: StateStore) -> dict[str, int]:
        keys = set(need.deps)
        for attr in need.dep_attrs:  # with() 依赖：目标物品及其容器链
            if attr == "location":
                cur = need.bindings.get("item")
                for _ in range(4):
                    b = store.beliefs.get(f"{cur}.location")
                    if b is None:
                        break
                    keys.add(b.key)
                    cur = b.value.get("ref") if isinstance(b.value, dict) else None
                    if not cur:
                        break
        return {k: store.beliefs[k].rev for k in sorted(keys) if k in store.beliefs}

    # Scheduler 回调：决策 / 发送 / 反馈后推进状态机
    def mark_deciding(self, need: Need, now_ms: int) -> None:
        self._set_phase(need, "deciding", now_ms)

    def after_wait(self, need: Need, now_ms: int) -> None:
        need.redecide_after_ms = now_ms + self.redecide_min_ms
        need.fire_latched = False
        self._set_phase(need, "armed", now_ms)

    def after_delivery(self, need: Need, now_ms: int) -> None:
        need.reminders_sent += 1
        need.cooldown_until_ms = now_ms + int(need.cooldown_s * 1000)
        self._set_phase(need, "awaiting_feedback", now_ms)

    def on_feedback(self, need_id: str, kind: str, store: StateStore, now_ms: int) -> list[NeedEdge]:
        need = self.needs.get(need_id)
        if need is None or need.phase in TERMINAL_PHASES:
            return []
        if kind == "done":
            return [self._terminal(need, "resolved", store, now_ms, "feedback:done")]
        if kind == "not_needed":
            self.prefs.append({"kind": need.kind, "item": need.bindings.get("item"), "until_ms": now_ms + 2 * 3600 * 1000})
        return [self._terminal(need, "dismissed", store, now_ms, f"feedback:{kind}")]

    def upsert_need(self, spec: dict, store: StateStore, now_ms: int, caused_by: str) -> list[NeedEdge]:
        """慢环编译出的需求（来源 speech / consolidator），先校验条件再生效。"""
        errs = sum((dsl.validate_cond(spec.get(k)) for k in ("arm", "fire", "resolve")), [])
        if errs:
            return []
        binds = {k: v for k, v in (spec.get("bindings") or {}).items() if v in store.entities}
        meta = {k: spec[k] for k in ("topic", "summary", "source_utt", "due_ms", "to_assistant", "addressee", "said_ms") if spec.get(k)}
        def words(topic) -> set[str]:  # 单复数视为同一个词；去掉泛词
            ws = {w.rstrip("s") for w in re.split(r"[\s_\-]+", str(topic or "").lower()) if w}
            return ws - {"reminder", "remind", "the", "a", "an", "to", "for", "of", "at", "wait", "three", "minute", ""}

        topic_words = words(meta.get("topic"))
        explicit = re.compile(r"remind|提醒|don'?t forget|别忘", re.I)
        for n in self.active_needs():  # 同一话题已有活跃的语音需求时不重复建
            if n.origin == "template":
                continue
            if meta.get("topic") and n.meta.get("topic") == meta.get("topic"):
                return []
            # 同一件事被连续几句话说出来（“别忘了开会”“10 点”“推迟一小时”）：对象相同、60 s 内、话题有重叠 → 合并
            same_who = bool(set(n.bindings.values()) & set(binds.values())) or bool(
                set(n.meta.get("addressee") or []) & set(meta.get("addressee") or []))
            close = abs(int(n.meta.get("said_ms", -10**9)) - int(meta.get("said_ms", 0))) < 60_000
            overlap = bool(topic_words & words(n.meta.get("topic")))
            same_due = bool(meta.get("due_ms") and n.meta.get("due_ms")
                            and abs(int(meta["due_ms"]) - int(n.meta["due_ms"])) < 30_000)
            if (same_who and close and overlap) or same_due or (overlap and same_who):
                if meta.get("due_ms") and not n.meta.get("due_ms"):
                    n.meta["due_ms"] = meta["due_ms"]
                # 保留更明确的那句（对助理说的“提醒我…”，或描述更具体的）
                new_explicit = bool(explicit.search(str(meta.get("source_utt") or "")))
                old_explicit = bool(explicit.search(str(n.meta.get("source_utt") or "")))
                if (new_explicit and not old_explicit) or (meta.get("to_assistant") and not n.meta.get("to_assistant")) or \
                        (new_explicit == old_explicit and
                         len(str(meta.get("summary") or "")) > len(str(n.meta.get("summary") or "")) + 4):
                    for k in ("summary", "topic", "source_utt", "to_assistant", "addressee"):
                        if meta.get(k):
                            n.meta[k] = meta[k]
                n.meta.setdefault("merged", []).append(meta.get("source_utt"))
                return []
        need = self._new_need(spec.get("kind") or "user_request", binds, spec,
                              origin=spec.get("origin", "speech"), now_ms=now_ms)
        need.meta = meta
        return [self._edge(need, "instantiated", store, now_ms, caused_by)] + self._evaluate(need, store, now_ms, caused_by)

    # ------------------------------------------------------------ 内部
    def _instantiate(self, store: StateStore, now_ms: int, caused: str) -> list[NeedEdge]:
        edges: list[NeedEdge] = []
        for tpl in self.templates:
            pools = []
            for pname, pspec in tpl["params"].items():
                cands = [eid for eid, e in sorted(store.entities.items())
                         if e.type == pspec["type"] and not e.merged_into
                         and (not pspec.get("cls_in") or _cls_match(e.cls, pspec["cls_in"]))]
                pools.append([(pname, c) for c in cands])
            for combo in itertools.product(*pools):
                binds = dict(combo)
                key = (tpl["kind"], tuple(sorted(binds.items())))
                ok = dsl.evaluate(tpl["instantiate"], store, binds, now_ms)
                prev = self.inst_last.get(key, False)
                self.inst_last[key] = ok
                if not ok or prev:
                    continue  # 只在条件上升沿实例化
                if any(n.kind == tpl["kind"] and n.bindings == binds and n.phase in ACTIVE for n in self.needs.values()):
                    continue
                if any(p["kind"] == tpl["kind"] and p["item"] == binds.get("item") and p["until_ms"] > now_ms
                       for p in self.prefs):
                    continue
                need = self._new_need(tpl["kind"], binds, tpl, origin="template", now_ms=now_ms)
                edges.append(self._edge(need, "instantiated", store, now_ms, caused))
                edges += self._evaluate(need, store, now_ms, caused)
        return edges

    def _new_need(self, kind: str, binds: dict, spec: dict, origin: str, now_ms: int) -> Need:
        self._n_need += 1
        need = Need(need_id=f"need_{self._n_need}", kind=kind, origin=origin, bindings=binds,
                    arm=spec.get("arm") or {}, fire=spec.get("fire") or {}, resolve=spec.get("resolve") or {},
                    dismiss=spec.get("dismiss"), mode_rule=spec.get("mode_rule") or [],
                    cooldown_s=spec.get("cooldown_s", 120), max_reminders=spec.get("max_reminders", 2),
                    phase="idle", phase_since_ms=now_ms)
        keys, attrs = set(), set()
        for c in (need.arm, need.fire, need.resolve, need.dismiss):
            k, a = dsl.deps(c, binds)
            keys |= k
            attrs |= a
        for rule in need.mode_rule:
            k, a = dsl.deps(rule.get("when"), binds)
            keys |= k
            attrs |= a
        need.deps, need.dep_attrs = sorted(keys), sorted(attrs)
        self.needs[need.need_id] = need
        for k in keys:
            self.dep_index.setdefault(k, set()).add(need.need_id)
        for a in attrs:
            self.attr_index.setdefault(a, set()).add(need.need_id)
        return need

    def _evaluate(self, need: Need, store: StateStore, now_ms: int, caused: str) -> list[NeedEdge]:
        if need.phase in TERMINAL_PHASES or need.phase == "draft":
            return []
        b = need.bindings
        if any(store.entities.get(eid) is None or store.entities[eid].merged_into for eid in b.values()):
            return [self._terminal(need, "expired", store, now_ms, caused)]
        arm = dsl.evaluate(need.arm, store, b, now_ms)
        # 空的 fire / resolve 视为“永不成立”（编译出的需求可能没有可判定的完成条件，只能靠用户反馈关闭）
        fire = bool(need.fire) and dsl.evaluate(need.fire, store, b, now_ms)
        res = bool(need.resolve) and dsl.evaluate(need.resolve, store, b, now_ms)
        self.last_eval[need.need_id] = (arm, fire, res)
        if fire:
            need.fire_last_true_ms = now_ms
        if res:
            return [self._terminal(need, "resolved", store, now_ms, caused)]
        if need.dismiss and dsl.evaluate(need.dismiss, store, b, now_ms) and need.phase in ("idle", "armed"):
            return [self._terminal(need, "dismissed", store, now_ms, caused)]
        if not fire:
            need.fire_latched = False
        edges: list[NeedEdge] = []
        if need.phase in ("idle", "armed") and fire and not need.fire_latched \
                and now_ms >= need.cooldown_until_ms and now_ms >= need.redecide_after_ms \
                and need.reminders_sent < need.max_reminders:
            need.fire_latched = True
            self._set_phase(need, "fired", now_ms)
            return [self._edge(need, "fired", store, now_ms, caused)]
        if need.phase == "idle" and arm:
            need.arm_false_since_ms = None
            self._set_phase(need, "armed", now_ms)
            edges.append(self._edge(need, "armed", store, now_ms, caused))
        elif need.phase == "armed" and not arm and not fire:
            if need.arm_false_since_ms is None:
                need.arm_false_since_ms = now_ms
            elif now_ms - need.arm_false_since_ms >= self.disarm_debounce_ms:
                need.arm_false_since_ms = None
                self._set_phase(need, "idle", now_ms)
                edges.append(self._edge(need, "disarmed", store, now_ms, caused))
        elif need.phase == "armed" and arm:
            need.arm_false_since_ms = None
        elif need.phase == "awaiting_feedback" and fire and now_ms - need.phase_since_ms >= self.awaiting_timeout_ms \
                and need.reminders_sent < need.max_reminders:
            need.fire_latched = False
            need.cooldown_until_ms = 0
            self._set_phase(need, "armed", now_ms)
            edges.append(self._edge(need, "rearmed", store, now_ms, caused))
        return edges

    def _terminal(self, need: Need, phase: str, store: StateStore, now_ms: int, caused: str) -> NeedEdge:
        self._set_phase(need, phase, now_ms)
        need.gen += 1
        return self._edge(need, phase, store, now_ms, caused)

    def _set_phase(self, need: Need, phase: str, now_ms: int) -> None:
        if need.phase != phase:
            need.phase, need.phase_since_ms = phase, now_ms

    def _edge(self, need: Need, edge: str, store: StateStore, now_ms: int, caused: str, **detail: Any) -> NeedEdge:
        self._n_edge += 1
        e = NeedEdge(edge_id=f"edge_{self._n_edge:04d}", need_id=need.need_id, kind=need.kind, edge=edge,
                     state_version=store.version, t_ms=now_ms, caused_by=caused,
                     dep_revs=self.dep_revs(need, store),
                     detail={"bindings": need.bindings, "phase": need.phase, "meta": need.meta, **detail})
        need.edges.append(e.edge_id)
        return e
