"""C7 GoalFilter：估计每个人当前在做什么、做到哪一步（设计说明 §5.3）。

目前只建模家庭场景里最关键的目标“出门”（leave_home）：多条证据（动作标签、拿起随身物品、走向门口、离开画面、
“走吧 / 要迟到了”这类话）累加成对数几率，随时间衰减。单帧标签翻转只让概率小幅波动，不会让判断翻转——
实测漏报正是因为 VLM 的 “leaving” 标签不稳定。

阶段：0 坐着 / 1 起身准备 / 2 走向门口 / 3 已出门；截止风险 H 由阶段给出（costs.yaml: hazard）。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from ..schemas import StateDelta
from .people import ANY, pkey
from .store import StateStore

STAGES = ["坐着", "起身准备", "走向门口", "已出门"]
STAGE_KEYS = ["idle", "preparing", "heading_out", "left"]

# activity → (证据权重, 该证据支持的阶段)
ACT_EVIDENCE = {
    "packing": (1.3, 1), "standing": (0.35, 1), "walking": (0.4, 1),
    "moving_to_exit": (2.2, 2), "leaving": (2.6, 2),
    "entering": (-2.2, 0), "sitting_working": (-0.9, 0), "resting": (-0.9, 0), "eating": (-0.7, 0),
    "cooking": (-0.6, 0), "cleaning": (-0.3, 0),
}
CARRY = {"bag", "backpack", "handbag", "purse", "coat", "jacket", "keys", "key", "umbrella", "shoes", "suitcase",
         "briefcase", "hat", "scarf"}
EXIT_CLS = {"door", "exit", "entrance", "gate", "doorway", "hallway"}
# (正则, 权重, 支持的阶段, 是否说的是自己)：“马上就走”类直接支持“走向门口”，“准备出门”类支持“起身准备”。
# 说自己的（“我走了”“let's go”）记在说话人身上并共享到全屋；催别人的（“go go go”“快走”）只记全屋，
# 否则催促者自己会被当成要出门。
LEAVE_SPEECH = [
    (re.compile(r"\b(bye|goodbye|see you|i'?m (leaving|off|going out)|we'?re (leaving|off)|heading out|"
                r"got to go|gotta go|have to go now|leaving now|i'?ll be back)\b", re.I), 1.8, 2, True),
    (re.compile(r"(我?走了|走啦|我先走|拜拜|再见|出发了|我出门了)"), 1.8, 2, True),
    (re.compile(r"\b(go go|go now|get going|off you go)\b|快走|快出门", re.I), 1.8, 2, False),
    (re.compile(r"\b(let'?s go|i have to go|i need to go|go to (work|school|the office|the park|the gym)|get ready|"
                r"i'?m (late|running late)|we'?re late|time to go|we should go)\b", re.I), 1.4, 1, True),
    (re.compile(r"(我要出门|上班去|去上班|来不及了|快迟到|准备走|该走了|出发)"), 1.4, 1, True),
]
HURRY_SPEECH = re.compile(r"\b(hurry|quick(ly)?|late|rush|asap)\b|快点|赶紧|来不及|迟到|快走", re.I)
PRIOR = -2.0


@dataclass
class _Ev:
    t: int
    w: float
    stage: int
    label: str
    kind: str = "act"  # act 动作标签 | speech 话语 | carry 拿起随身物品 | near 靠近出口 | presence 离开画面


@dataclass
class GoalState:
    evidence: list[_Ev] = field(default_factory=list)
    left_ms: int | None = None
    p: float = 0.12
    stage: int = 0
    cues: list[str] = field(default_factory=list)


class GoalFilter:
    def __init__(self, half_life_s: float = 60.0, stage_half_life_s: float = 40.0):
        self.half = half_life_s * 1000
        self.stage_half = stage_half_life_s * 1000
        self.people: dict[str, GoalState] = {}
        self.hurry_ms: dict[str, int] = {}

    def _st(self, key: str) -> GoalState:
        return self.people.setdefault(key, GoalState())

    def _add(self, key: str, t: int, w: float, stage: int, label: str, shared: bool = False, kind: str = "act") -> None:
        """shared=True 的证据（语音、离开画面）同时记入全屋键 "*"：“走吧”往往说的是一群人；
        动作标签只记在本人身上——把多人的“坐着 / 休息”加在一起会把全屋概率压到 0。"""
        for k in ({key, ANY} if shared else {key}):
            st = self._st(k)
            st.evidence.append(_Ev(t, w, stage, label, kind))
            st.evidence = [e for e in st.evidence if t - e.t < 6 * self.half][-40:]

    # ------------------------------------------------------------ 证据
    def observe_delta(self, d: StateDelta, store: StateStore, now_ms: int) -> None:
        for ch in d.changes:
            if ch.reason.startswith("tick"):
                continue
            eid, attr = ch.key.split(".", 1)
            e = store.entities.get(eid)
            if e is None:
                continue
            if e.type == "PERSON":
                key = pkey(store, eid)
                if attr == "activity" and ch.new in ACT_EVIDENCE:
                    w, stage = ACT_EVIDENCE[ch.new]
                    self._add(key, now_ms, w, stage, f"activity={ch.new}")
                elif attr == "presence" and ch.new == "left":
                    self._add(key, now_ms, 2.4, 3, "presence=left", shared=True, kind="presence")
                    for k in (key, ANY):
                        self._st(k).left_ms = now_ms
                elif attr == "presence" and ch.new == "in_view" and ch.old == "left":
                    self._add(key, now_ms, -1.5, 0, "回到画面")
                    self._st(key).left_ms = None
                elif attr == "near" and isinstance(ch.new, str):
                    ref = store.entities.get(ch.new)
                    if ref is not None and ref.cls in EXIT_CLS:
                        self._add(key, now_ms, 1.4, 2, f"靠近 {ch.new}", kind="near")
            elif attr == "location" and isinstance(ch.new, dict) and ch.new.get("rel") in ("HELD_BY", "WORN_BY"):
                if e.cls in CARRY:
                    self._add(pkey(store, ch.new.get("ref")), now_ms, 0.8, 1, f"拿起 {e.cls}", kind="carry")

    def observe_utterance(self, key: str | None, text: str, now_ms: int) -> None:
        hit = next(((w, st, own) for rx, w, st, own in LEAVE_SPEECH if rx.search(text or "")), None)
        if hit:
            self._add(key if (hit[2] and key) else ANY, now_ms, hit[0], hit[1], f"说“{text[:24]}”", shared=True,
                      kind="speech")
        if HURRY_SPEECH.search(text or ""):
            self._add(ANY, now_ms, 0.6, 1, "赶时间", shared=True, kind="speech")
            self.hurry_ms[ANY] = now_ms

    # ------------------------------------------------------------ 推断
    def update(self, now_ms: int) -> None:
        for st in self.people.values():
            logit, best_stage, cues, n_pos = PRIOR, 0, [], 0
            kinds, kinds2 = set(), set()
            for e in st.evidence:
                dt = max(0, now_ms - e.t)
                logit += e.w * 0.5 ** (dt / self.half)
                if e.w > 0 and e.w * 0.5 ** (dt / self.stage_half) >= 0.3:
                    best_stage = max(best_stage, e.stage)
                    n_pos += e.stage >= 1
                    kinds.add(e.kind)
                    if e.stage >= 2:
                        kinds2.add(e.kind)
            st.p = 1 / (1 + math.exp(-logit))
            if st.left_ms is not None and now_ms - st.left_ms < 10 * 60_000:
                best_stage = 3
            # “走向门口”要跨来源佐证：一句“马上就走”，或动作标签 + 至少一种别的证据（拿随身物品 / 靠近门口 / 离开画面）。
            # 第一人称视频里人一走出画面就常被标成 leaving，单靠动作标签会把“去厨房”当成出门。
            corroborated = bool(kinds2 & {"speech", "near", "presence"}) or (bool(kinds2) and bool(kinds - {"act"}))
            if best_stage == 2 and (st.p < 0.4 or n_pos < 2 or not corroborated):
                best_stage = 1
            st.stage = best_stage
            st.cues = [e.label for e in sorted(st.evidence, key=lambda e: -e.t)[:3]]

    def leave(self, keys: list[str]) -> tuple[float, int]:
        """一组人里最可能出门的那个人的 (概率, 阶段)；绑定的 ID 没有证据时退回全屋聚合 × 0.9。"""
        best = (0.0, 0)
        for k in keys:
            st = self.people.get(k)
            if st and st.evidence and (st.p, st.stage) > best:
                best = (st.p, st.stage)
        agg = self.household()
        if agg[0] * 0.9 > best[0]:
            best = (agg[0] * 0.9, max(agg[1], best[1]))
        return best if best[0] else (0.12, 0)

    def household(self) -> tuple[float, int]:
        """全屋：任何一个人要出门的概率（各人取最大，再与共享的语音证据取最大）。"""
        cands = [(st.p, st.stage) for st in self.people.values() if st.evidence]
        return max(cands) if cands else (0.0, 0)

    def hurried(self, keys: list[str], now_ms: int, window_ms: int = 120_000) -> bool:
        return any(now_ms - self.hurry_ms.get(k, -10**9) < window_ms for k in [*keys, ANY])

    def snapshot(self) -> dict:
        return {k: {"p": round(st.p, 3), "stage": st.stage, "stage_label": STAGES[st.stage], "cues": st.cues}
                for k, st in self.people.items() if st.evidence}
