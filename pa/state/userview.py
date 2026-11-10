"""C6 UserView：每个人“以为”的世界（设计说明 §5.3）。

规则：物品的位置 / 状态变化发生时在场且画面清楚的人算“看到了”；有人在他在场时提到这个物品，算“听到了”；
否则他的认知停在最后一次知道的值。真实值与某人以为的值不同、且这个物品与他有关（他拿过 / 是主人），就是认知差。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..schemas import StateDelta
from .people import pkey, present_keys
from .store import StateStore

ZH_NAMES = {"phone": "手机", "keys": "钥匙", "key": "钥匙", "wallet": "钱包", "umbrella": "伞", "glasses": "眼镜",
            "laptop": "电脑", "bag": "包", "backpack": "背包", "remote": "遥控器", "cup": "杯子", "book": "书",
            "charger": "充电器", "card": "卡", "medicine": "药", "earrings": "耳环", "letter": "信"}


@dataclass
class Gap:
    person: str
    key: str
    believed: object
    actual: object
    since_ms: int
    via: str


class UserView:
    def __init__(self):
        self.known: dict[str, dict[str, tuple[object, int, str]]] = {}
        self.heard: list[tuple[int, str | None, str, list[str]]] = []  # (t, speaker, text, listeners)
        self.handled: dict[str, set[str]] = {}  # 物品 → 拿过它的人

    def observe_delta(self, d: StateDelta, store: StateStore, now_ms: int) -> None:
        witnesses = None
        for ch in d.changes:
            if ch.reason.startswith("tick"):
                continue
            eid, attr = ch.key.split(".", 1)
            e = store.entities.get(eid)
            if e is None or e.type not in ("OBJECT", "DEVICE") or attr not in ("location", "state"):
                continue
            if attr == "location" and isinstance(ch.new, dict) and ch.new.get("rel") in ("HELD_BY", "WORN_BY"):
                h = pkey(store, ch.new.get("ref"))
                if h:
                    self.handled.setdefault(eid, set()).add(h)
            if witnesses is None:
                quality = store.last_view.quality if store.last_view else "clear"
                witnesses = present_keys(store, now_ms, recent_ms=3000) if quality != "poor" else []
            knowers = list(witnesses)
            if isinstance(ch.old, dict) and ch.old.get("rel") in ("HELD_BY", "WORN_BY"):
                prev = pkey(store, ch.old.get("ref"))  # 东西原本在谁手里，多半就是谁放下的
                if prev and prev not in knowers:
                    knowers.append(prev)
            for w in knowers:
                self.known.setdefault(w, {})[ch.key] = (ch.new, now_ms, "seen")

    def observe_utterance(self, speaker: str | None, text: str, store: StateStore, now_ms: int) -> None:
        listeners = present_keys(store, now_ms)
        if speaker and speaker not in listeners:
            listeners.append(speaker)
        self.heard.append((now_ms, speaker, text, listeners))
        self.heard = self.heard[-300:]
        low = (text or "").lower()
        for eid, e in store.entities.items():
            if e.type not in ("OBJECT", "DEVICE") or e.merged_into:
                continue
            names = {e.cls, e.cls + "s", ZH_NAMES.get(e.cls, "")} - {""}
            if any(re.search(rf"\b{re.escape(n)}\b", low) if n.isascii() else n in text for n in names):
                key = f"{eid}.location"
                b = store.beliefs.get(key)
                if b is not None and b.value is not None:
                    for lst in listeners:
                        self.known.setdefault(lst, {})[key] = (b.value, now_ms, "told")

    def believes(self, person: str, key: str) -> tuple[object, int, str] | None:
        return self.known.get(person, {}).get(key)

    def heard_since(self, person: str, pattern: str, since_ms: int) -> bool:
        rx = re.compile(pattern, re.I)
        return any(t >= since_ms and person in lst and rx.search(txt) for t, _, txt, lst in self.heard)

    def gaps(self, store: StateStore, now_ms: int) -> list[Gap]:
        out = []
        for person, kd in self.known.items():
            for key, (val, t, via) in kd.items():
                cur = store.beliefs.get(key)
                if cur is None or cur.value is None or val is None or cur.status not in ("holds", "stale"):
                    continue
                if cur.value == val or cur.source not in ("seen", "user_confirmed"):
                    continue
                # 只看“从一个地方挪到另一个地方”：在人手里的东西转眼就会变，问一句就知道
                if any(isinstance(v, dict) and v.get("rel") in ("HELD_BY", "WORN_BY") for v in (cur.value, val)):
                    continue
                eid = key.split(".", 1)[0]
                e = store.entities.get(eid)
                if e is None or e.merged_into:
                    continue
                owner = store.val(f"{eid}.owner")
                related = person in self.handled.get(eid, set()) or (owner and pkey(store, owner) == person)
                if not related:
                    continue
                if isinstance(cur.value, dict) and cur.value.get("rel") in ("HELD_BY", "WORN_BY") \
                        and pkey(store, cur.value.get("ref")) == person:
                    continue
                out.append(Gap(person, key, val, cur.value, cur.since_ms, via))
        return out
