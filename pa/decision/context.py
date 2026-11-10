"""DecisionContext 组装（设计文档 §6.1）。M7 与 M8 共用同一份快照。"""
from __future__ import annotations

from ..agenda.trigger import TriggerEngine
from ..perception.prompt import fmt_ms
from ..schemas import Need, StateDelta
from ..state.attrs import render_value
from ..state.store import StateStore

DEFAULT_ROLE = "个人助理：尽量少打扰，只在可能造成实际损失时提醒"


def build_context(need: Need, store: StateStore, agenda: TriggerEngine, recent: list[StateDelta], history: list[dict],
                  now_ms: int, ctx_id: str, role: str = DEFAULT_ROLE, permissions: dict | None = None,
                  fire_reason: str | None = None, speech: list[str] | None = None) -> dict:
    dep_revs = agenda.dep_revs(need, store)
    ents = set(need.bindings.values())
    keys = set(dep_revs)
    beliefs = []
    for k in sorted(keys):
        b = store.beliefs.get(k)
        if b is None:
            continue
        item = {"key": k, "value": render_value(b.value), "source": b.source, "status": b.status,
                "age_s": round((now_ms - b.last_support_ms) / 1000, 1)}
        if b.last_seen and b.last_seen.get("value") != b.value:
            item["last_seen"] = f"{render_value(b.last_seen['value'])} @{fmt_ms(b.last_seen['t_ms'])}"
        beliefs.append(item)
    for eid in ents:  # 绑定实体的描述，给模型看“是什么”
        e = store.entities.get(eid)
        if e:
            beliefs.append({"key": f"{eid}.desc", "value": f"{e.cls} {e.desc}".strip()})
    changes = []
    for d in recent[-12:]:
        for ch in d.changes:
            if ch.key in keys or ch.key.split(".", 1)[0] in ents:
                changes.append(f"{fmt_ms(d.t_event_ms)} {ch.key}: {render_value(ch.old)} → {render_value(ch.new)}")
    return {
        "context_id": ctx_id,
        "need_id": need.need_id,
        "state_version": store.version,
        "t_ms": now_ms,
        "role": role,
        "permissions": permissions or {"may_ask": True, "may_remind": True},
        "need": {"kind": need.kind, "bindings": need.bindings, "fire_reason": fire_reason,
                 "reminders_sent": need.reminders_sent, "mode_cap": agenda.mode_cap(need, store, now_ms),
                 **({"topic": need.meta["topic"]} if need.meta.get("topic") else {}),
                 **({"summary": need.meta["summary"]} if need.meta.get("summary") else {})},
        "recent_speech": (speech or [])[-6:],
        "beliefs": beliefs,
        "recent_changes": changes[-8:],
        "history": [{"text": h["text"], "ref": h["ref"]} for h in history],
        "dep_revs": dep_revs,
    }
