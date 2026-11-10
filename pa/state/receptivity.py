"""C8 Receptivity：此刻打扰的代价高不高（设计说明 §5.3）。

说话中（本人 4 s 内说过话，或屋里 2.5 s 内有人说话）→ 打扰代价 ×2；赶时间 → 自己想起来的概率 q 低；
刚刚自己问了相关问题的人是“想听”的，不算打扰。
"""
from __future__ import annotations

from dataclasses import dataclass

from .people import ANY


@dataclass
class Situation:
    label: str
    talking: bool
    hurried: bool
    asking: bool
    key: str  # costs.yaml 里 c_int 的情境键：normal | talking

    def as_dict(self) -> dict:
        return {"label": self.label, "talking": self.talking, "hurried": self.hurried, "asking": self.asking}


class Receptivity:
    def __init__(self, own_talk_ms: int = 4000, room_talk_ms: int = 2500):
        self.own_talk_ms, self.room_talk_ms = own_talk_ms, room_talk_ms
        self.last_spoke: dict[str, int] = {}

    def observe_utterance(self, key: str | None, end_ms: int) -> None:
        self.last_spoke[ANY] = end_ms
        if key:
            self.last_spoke[key] = end_ms

    def situation(self, keys: list[str], now_ms: int, hurried: bool, asking: bool = False) -> Situation:
        named = [k for k in keys if k != ANY]
        own = any(now_ms - self.last_spoke.get(k, -10**9) < self.own_talk_ms for k in named)
        # 对象明确时只看对象本人在不在说话（别人在旁边催促不算对象“正忙着聊天”）；对象不明确时看整个屋子
        room = not named and now_ms - self.last_spoke.get(ANY, -10**9) < self.room_talk_ms
        talking = (own or room) and not asking
        if talking:
            label = "说话中" + (" · 赶时间" if hurried else "")
        elif asking:
            label = "正在问"
        elif hurried:
            label = "赶时间 · 安静"
        else:
            label = "空闲"
        return Situation(label, talking, hurried, asking, "talking" if talking else "normal")
