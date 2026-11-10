"""C9 机会生成器：六个来源统一产出 Opportunity（设计说明 §2.3）。

① request   语音编译出的用户请求（含“N 分钟后提醒我”）——来自议程里的 speech Need
② template  风险模板（忘带物品、电器没关）——来自议程里的 template Need
③ belief_gap 认知差：物品被别人挪了，经手过它的人不知道（UserView）
⑤ goal      目标推断：某人要出门 → 他经手过的随身物品不在身上（GoalFilter）
⑤ search    有人在找东西（“where is my phone”），而状态里知道它在哪
⑥ forecast  风险预测：危险电器开着、屋里没人
✦ open      开放发现：受约束的 LLM 每隔一段时间提出 0–2 个候选（opportunity/llm.py）
④ expectation（习惯 / 流程违背）需要长期习惯数据，暂未实现。
"""
from __future__ import annotations

import json
import re

from ..agenda.trigger import TriggerEngine
from ..response.drafter import zh
from ..schemas.agenda import TERMINAL_PHASES
from ..state.goals import GoalFilter
from ..state.people import ANY, display_name, person_ids, pkey, present_keys
from ..state.store import StateStore
from ..state.userview import UserView
from .model import Opportunity, OpportunityTable

ESSENTIAL = {"phone", "keys", "key", "wallet", "purse", "badge", "card", "glasses", "umbrella", "laptop", "charger",
             "passport", "medicine"}
SAFETY_DEV = {"stove", "oven", "iron", "heater", "gas"}
LEAVE_WORDS = ("moving_to_exit", "leaving", "\"left\"")
SEARCH_RX = re.compile(r"\bwhere (?:is|are|'s|did i put|did you put) (?:my|the|your|our) ([a-z ]{2,20}?)\b(?:\?|$|\.| now| again)"
                       r"|\b(?:can'?t|cannot) find (?:my|the) ([a-z ]{2,20})|我的(\S{1,4})(?:在哪|呢|去哪)", re.I)


def _place(store: StateStore, item: str) -> str:
    b = store.beliefs.get(f"{item}.location")
    if b is None or not isinstance(b.value, dict):
        return ""
    ref = b.value.get("ref") or ""
    if b.value.get("rel") in ("HELD_BY", "WORN_BY"):
        return display_name(store, pkey(store, ref) or ref) + "手里"
    return zh(ref.rsplit("_", 1)[0]) if ref else ""


def _held_by(store: StateStore, item: str, keys: list[str]) -> bool:
    for k in keys:
        for pid in person_ids(store, k) or [k]:
            if store.with_person(item, pid):
                return True
    return False


def _loc_factor(store: StateStore, item: str) -> float:
    """物品确实不在人身上（还在原处）的概率。"""
    b = store.beliefs.get(f"{item}.location")
    if b is None or b.value is None:
        return 0.5
    if isinstance(b.value, dict) and b.value.get("rel") in ("HELD_BY", "WORN_BY"):
        return 0.2  # 在别人手里：多半会递过去，或者本来就是那个人的
    if b.status == "holds" and b.source in ("seen", "user_confirmed"):
        return 0.95
    return 0.7 if b.status == "stale" else 0.5


def _state_factor(store: StateStore, dev: str) -> float:
    b = store.beliefs.get(f"{dev}.state")
    if b is None or b.value != "on":
        return 0.3
    return 0.85 if b.status == "holds" and b.source in ("seen", "user_confirmed") else 0.5


def _persons(store: StateStore, binds: dict) -> list[str]:
    out = []
    for v in binds.values():
        e = store.entities.get(v)
        if e is not None and e.type == "PERSON":
            k = pkey(store, v)
            if k and k not in out:
                out.append(k)
    return out


class Generators:
    def __init__(self, table: OpportunityTable, open_ttl_ms: int = 600_000):
        self.table = table
        self.open_ttl_ms = open_ttl_ms

    # ------------------------------------------------------------ ①② 议程需求
    def from_needs(self, agenda: TriggerEngine, store: StateStore, now_ms: int) -> list[tuple[Opportunity, bool]]:
        out = []
        for need in agenda.needs.values():
            b = need.bindings
            item = b.get("item")
            if need.kind == "possible_forgotten_item":
                person = pkey(store, b.get("person"))
                key = f"forget:{item}:{person}"
            elif need.kind == "appliance_left_on":
                key = f"appliance:{item}"
            else:  # 按需求 ID：同一件事的几句话合并后话题会被改写，键不能跟着变
                key = f"request:{need.need_id}"
            opp = self.table.get(key)
            if need.phase in TERMINAL_PHASES:
                if opp is not None and opp.status == "active" and opp.need_id == need.need_id:
                    opp.status = {"resolved": "resolved", "dismissed": "suppressed"}.get(need.phase, "expired")
                    opp.updated_ms = now_ms
                    out.append((opp, False))
                continue
            if need.kind == "possible_forgotten_item":
                cls = store.entities[item].cls if item in store.entities else item
                person = pkey(store, b.get("person"))
                o = Opportunity(key=key, source="template", addressee=[person],
                                problem=f"{display_name(store, person)} 要出门，{zh(cls)}还在{_place(store, item) or '原处'}",
                                category="essential", deadline="goal:leave_home", item=item, need_id=need.need_id,
                                evidence=[f"{item}.location", f"held_before({item},{person})"],
                                factors={"不在身上": _loc_factor(store, item), "需要它": 0.9})
            elif need.kind == "appliance_left_on":
                cls = store.entities[item].cls if item in store.entities else item
                o = Opportunity(key=key, source="template", addressee=_persons(store, b) or [ANY],
                                problem=f"{zh(cls)}还开着，有人要离开", category="safety" if cls in SAFETY_DEV else "convenience",
                                deadline="goal:leave_home", item=item, need_id=need.need_id, evidence=[f"{item}.state"],
                                factors={"确实开着": _state_factor(store, item), "无人照看会出事": 0.3})
            else:
                due = need.meta.get("due_ms")
                fire_txt = json.dumps(need.fire, ensure_ascii=False)
                if due:
                    deadline, factors = f"time:{int(due)}", {"请求明确": 0.95}
                elif any(w in fire_txt for w in LEAVE_WORDS):
                    deadline, factors = "goal:leave_home", {"出门时真会忘/需要": 0.6}
                elif need.fire:
                    deadline, factors = "trigger", {"请求明确": 0.8}
                else:  # 没有可判定的时机（如“10 点的会”这类绝对时间）：先记住，不主动开口
                    deadline, factors = "none", {"请求明确": 0.8}
                names = [str(a) for a in (need.meta.get("addressee") or []) if a]
                # 字幕里的说话人名字比画面实体的声音锚点可靠（第一人称视角下 link 常绑错人）
                o = Opportunity(key=key, source="request", addressee=names or _persons(store, b) or [ANY],
                                problem=need.meta.get("summary") or need.meta.get("topic") or need.kind,
                                category="essential", deadline=deadline, item=item, topic=need.meta.get("topic"),
                                need_id=need.need_id, evidence=[f"“{need.meta.get('source_utt', '')}”"], factors=factors,
                                meta={"to_assistant": bool(need.meta.get("to_assistant")), "said_ms": need.meta.get("said_ms")})
            out.append(self.table.upsert(o, now_ms))
        return out

    # ------------------------------------------------------------ ⑤ 目标推断
    def goal_items(self, store: StateStore, goals: GoalFilter, uv: UserView, now_ms: int) -> list[tuple[Opportunity, bool]]:
        out = []
        for key, st in goals.people.items():
            if key == ANY or st.p < 0.3:
                continue
            for eid, e in store.entities.items():
                if e.type != "OBJECT" or e.merged_into or e.cls not in ESSENTIAL:
                    continue
                handled = key in uv.handled.get(eid, set()) or pkey(store, e.last_holder) == key
                owner = store.val(f"{eid}.owner")
                if not (handled or (owner and pkey(store, owner) == key)):
                    continue
                loc = store.beliefs.get(f"{eid}.location")
                if loc is None or _held_by(store, eid, [key]):
                    continue
                if isinstance(loc.value, dict) and loc.value.get("rel") in ("HELD_BY", "WORN_BY"):
                    continue  # 在别人手里：不算落下
                o = Opportunity(key=f"forget:{eid}:{key}", source="goal", addressee=[key],
                                problem=f"{display_name(store, key)} 要出门，{zh(e.cls)}还在{_place(store, eid) or '原处'}",
                                category="essential", deadline="goal:leave_home", item=eid,
                                evidence=[f"{eid}.location", f"{key} 经手过 {eid}"],
                                factors={"不在身上": _loc_factor(store, eid), "需要它": 0.85 if owner else 0.7})
                out.append(self.table.upsert(o, now_ms))
        return out

    # ------------------------------------------------------------ ③ 认知差
    def belief_gaps(self, store: StateStore, uv: UserView, now_ms: int) -> list[tuple[Opportunity, bool]]:
        out = []
        for g in uv.gaps(store, now_ms):
            eid = g.key.split(".", 1)[0]
            e = store.entities[eid]
            def where(v):
                if not isinstance(v, dict):
                    return str(v)
                if v.get("rel") in ("HELD_BY", "WORN_BY"):
                    return display_name(store, pkey(store, v.get("ref")) or "") + "手里"
                return zh(v.get("ref", "").rsplit("_", 1)[0])
            o = Opportunity(key=f"gap:{eid}:{g.person}", source="belief_gap", addressee=[g.person],
                            problem=f"{zh(e.cls)}被挪到了{where(g.actual)}，{display_name(store, g.person)}可能还以为在{where(g.believed)}",
                            category="essential" if e.cls in ESSENTIAL else "convenience",
                            deadline="goal:leave_home" if e.cls in ESSENTIAL else "need", eta_kind="locate", item=eid,
                            evidence=[g.key, f"{g.person} 上次{'看到' if g.via == 'seen' else '听说'}它在{where(g.believed)}"],
                            factors={"他不知道": 0.9, "会用到": 0.6 if e.cls in ESSENTIAL else 0.35})
            out.append(self.table.upsert(o, now_ms))
        return out

    # ------------------------------------------------------------ ⑤ 正在找东西
    def search(self, speaker: str | None, text: str, store: StateStore, now_ms: int) -> list[tuple[Opportunity, bool]]:
        m = SEARCH_RX.search(text or "")
        if not m:
            return []
        word = next(g for g in m.groups() if g).strip().lower()
        word = re.sub(r"\b(the|my|your)\b", "", word).strip()
        out = []
        for eid, e in store.entities.items():
            if e.type not in ("OBJECT", "DEVICE") or e.merged_into:
                continue
            names = {e.cls, e.cls + "s", zh(e.cls)}
            if not any(n and (n in word or word in n) for n in names):
                continue
            b = store.beliefs.get(f"{eid}.location")
            if b is None or b.value is None:
                continue
            who = speaker or ANY
            o = Opportunity(key=f"search:{eid}:{who}", source="search", addressee=[who],
                            problem=f"{display_name(store, who)}在找{zh(e.cls)}，它在{_place(store, eid) or '某处'}",
                            category="convenience", deadline="asap", eta_kind="locate", item=eid,
                            evidence=[f"“{text[:60]}”", f"{eid}.location"],
                            factors={"位置可靠": _loc_factor(store, eid), "确实在找": 0.9},
                            meta={"asked_ms": now_ms, "asker": who})
            out.append(self.table.upsert(o, now_ms))
        return out

    # ------------------------------------------------------------ ⑥ 风险预测
    def forecast(self, store: StateStore, now_ms: int, absent_ms: int = 90_000) -> list[tuple[Opportunity, bool]]:
        out = []
        if present_keys(store, now_ms, recent_ms=absent_ms):
            return out
        for eid, e in store.entities.items():
            if e.type != "DEVICE" or e.merged_into or e.cls not in SAFETY_DEV:
                continue
            b = store.beliefs.get(f"{eid}.state")
            if b is None or b.value != "on" or b.status not in ("holds", "stale"):
                continue
            o = Opportunity(key=f"appliance:{eid}", source="forecast", addressee=[ANY],
                            problem=f"{zh(e.cls)}开着，屋里已经 {absent_ms // 60000} 分钟没人",
                            category="safety", deadline="asap", item=eid, evidence=[f"{eid}.state"],
                            factors={"确实开着": 0.85 if b.status == "holds" else 0.5, "无人照看会出事": 0.3})
            out.append(self.table.upsert(o, now_ms))
        return out

    # ------------------------------------------------------------ 收尾：解决 / 过期
    def settle(self, store: StateStore, uv: UserView, now_ms: int) -> list[Opportunity]:
        changed = []
        gap_keys = {f"gap:{g.key.split('.', 1)[0]}:{g.person}" for g in uv.gaps(store, now_ms)}
        for o in self.table.active():
            new = None
            if o.item and o.item not in store.entities:
                new = "expired"
            elif o.item and store.entities[o.item].merged_into:
                new = "expired"
            elif o.key.startswith(("forget:", "gap:", "search:")) and o.item and \
                    _held_by(store, o.item, [k for k in o.addressee if k != ANY]):
                new = "resolved"
            elif o.source == "belief_gap" and o.key not in gap_keys and not o.need_id:
                new = "resolved"
            elif o.source == "search" and now_ms - o.meta.get("asked_ms", now_ms) > 90_000:
                new = "expired"
            elif o.key.startswith("appliance:") and store.val(f"{o.item}.state") in ("off", "closed"):
                new = "resolved"
            elif o.source == "open" and now_ms - o.updated_ms > self.open_ttl_ms:
                new = "expired"
            elif o.deadline.startswith("time:") and o.reminders and now_ms - (o.last_delivery_ms or 0) > 300_000:
                new = "resolved"
            if new:
                o.status, o.updated_ms = new, now_ms
                changed.append(o)
        return changed
