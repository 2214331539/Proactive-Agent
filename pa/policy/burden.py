"""C13 负担管理：累积打扰负担与频率限制（设计说明 §5.5）。

每次投递 X ← X·0.5^(Δt/半衰期) + 权重（语音 1、提示音 0.3），之后的打扰代价 C_int × (1 + k·X)，
避免短时间内连续打扰；语音之间至少隔 voice_min_gap_s，同一机会最多提醒 max_voice 次。
"""
from __future__ import annotations


class Burden:
    def __init__(self, half_life_s: float = 600, k: float = 0.4, voice_weight: float = 1.0, ambient_weight: float = 0.3):
        self.half_ms = half_life_s * 1000
        self.k, self.vw, self.aw = k, voice_weight, ambient_weight
        self.x = 0.0
        self.t = 0
        self.last_voice_ms: int | None = None
        self.last_ambient_ms: int | None = None

    def value(self, now_ms: int) -> float:
        return self.x * 0.5 ** (max(0, now_ms - self.t) / self.half_ms)

    def multiplier(self, now_ms: int) -> float:
        return 1 + self.k * self.value(now_ms)

    def on_delivery(self, now_ms: int, mode: str) -> None:
        self.x = self.value(now_ms) + (self.aw if mode == "AMBIENT" else self.vw)
        self.t = now_ms
        if mode == "AMBIENT":
            self.last_ambient_ms = now_ms
        else:
            self.last_voice_ms = now_ms

    def wait_ms(self, mode: str, now_ms: int, voice_gap_ms: int, ambient_gap_ms: int) -> int:
        """距离允许下一次同类投递还要等多久（0 表示可以）。"""
        last, gap = (self.last_ambient_ms, ambient_gap_ms) if mode == "AMBIENT" else (self.last_voice_ms, voice_gap_ms)
        if last is None:
            return 0
        return max(0, last + gap - now_ms)
