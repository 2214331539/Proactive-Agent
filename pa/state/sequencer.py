"""按窗口序号顺序放行 VLM 补丁（设计文档 §3 M5）。"""
from __future__ import annotations

from ..schemas import StatePatch


class Sequencer:
    def __init__(self, max_wait_ms: int = 5000):
        self.next_seq = 0
        self.pending: dict[int, tuple[StatePatch, int]] = {}
        self.max_wait_ms = max_wait_ms

    def push(self, patch: StatePatch, now_ms: int) -> list[StatePatch]:
        assert patch.seq is not None
        if patch.seq < self.next_seq:
            return []  # 已被跳过的序号迟到：丢弃（日志里仍有记录）
        self.pending[patch.seq] = (patch, now_ms)
        return self.release(now_ms)

    def release(self, now_ms: int) -> list[StatePatch]:
        out: list[StatePatch] = []
        while True:
            if self.next_seq in self.pending:
                out.append(self.pending.pop(self.next_seq)[0])
                self.next_seq += 1
                continue
            # 缺失的序号最多等待 max_wait_ms
            if self.pending and min(t for _, t in self.pending.values()) + self.max_wait_ms <= now_ms:
                self.next_seq = min(self.pending)
                continue
            break
        return out
