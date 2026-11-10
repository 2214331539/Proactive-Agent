"""M2 Frame Gate：帧缓冲 + 变化检测 + 窗口组装（设计文档 §3 M2）。纯程序。"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

import numpy as np

from ..schemas import WindowRequest


@dataclass
class GateCfg:
    theta_change: float = 0.08  # 与上一窗口末帧的平均灰度差（0..1），需按数据标定
    theta_settle: float = 0.02  # 相邻帧差低于它视为“画面稳定”
    settle_ms: int = 350
    max_settle_ms: int = 1500
    heartbeat_s: float = 4.0
    focus_heartbeat_s: float = 1.5
    min_gap_ms: int = 800
    initial_ms: int = 400
    frames_per_window: int = 3
    buffer_s: float = 10.0
    fps: float = 5.0


@dataclass
class _Frame:
    frame_id: str
    pts_ms: int
    img: np.ndarray
    small: np.ndarray
    step_diff: float


def _small(img: np.ndarray) -> np.ndarray:
    g = img.mean(axis=2) if img.ndim == 3 else img.astype(np.float32)
    h, w = g.shape
    ys = (np.arange(64) * h // 64).astype(int)
    xs = (np.arange(64) * w // 64).astype(int)
    return g[np.ix_(ys, xs)].astype(np.float32) / 255.0


class FrameGate:
    def __init__(self, cfg: GateCfg):
        self.cfg = cfg
        self.buf: deque[_Frame] = deque(maxlen=max(4, int(cfg.buffer_s * cfg.fps)))
        self.ref: np.ndarray | None = None  # 上一个窗口末帧
        self.last_emit_ms: int | None = None
        self.last_end_ms = 0
        self.seq = 0
        self.pending_since: int | None = None
        self.stable_since: int | None = None
        self.pending_utts: list[tuple[str, int]] = []  # (utt_id, end_ms)
        self.focus: dict[str, list[str]] = {}  # need_id -> 关注实体
        self.frames_by_id: dict[str, _Frame] = {}

    # ---------- 外部输入 ----------
    def set_focus(self, need_id: str, entities: list[str]) -> None:
        self.focus[need_id] = entities

    def clear_focus(self, need_id: str) -> None:
        self.focus.pop(need_id, None)

    def on_utterance(self, utt_id: str, end_ms: int) -> None:
        self.pending_utts.append((utt_id, end_ms))

    def get(self, frame_id: str) -> np.ndarray | None:
        f = self.frames_by_id.get(frame_id)
        return None if f is None else f.img

    def pts(self, frame_id: str) -> int:
        return self.frames_by_id[frame_id].pts_ms

    # ---------- 主逻辑 ----------
    def push(self, frame_id: str, pts_ms: int, img: np.ndarray) -> list[WindowRequest]:
        small = _small(img)
        prev = self.buf[-1].small if self.buf else small
        f = _Frame(frame_id, pts_ms, img, small, float(np.abs(small - prev).mean()))
        if len(self.buf) == self.buf.maxlen:
            self.frames_by_id.pop(self.buf[0].frame_id, None)
        self.buf.append(f)
        self.frames_by_id[frame_id] = f
        now = pts_ms
        if self.ref is None:
            self.ref = small
        change = float(np.abs(small - self.ref).mean())

        # 变化检测：超过阈值 → 等画面稳定 settle_ms（动作做完）再发；最长等 max_settle_ms
        if change > self.cfg.theta_change and self.pending_since is None:
            self.pending_since = now
        if f.step_diff < self.cfg.theta_settle:
            self.stable_since = self.stable_since if self.stable_since is not None else now
        else:
            self.stable_since = None

        can_emit = self.last_emit_ms is None or now - self.last_emit_ms >= self.cfg.min_gap_ms
        if not can_emit:
            return []
        if self.last_emit_ms is None and now >= self.cfg.initial_ms:
            return [self._emit(now, "initial")]
        if self.pending_since is not None:
            settled = self.stable_since is not None and now - self.stable_since >= self.cfg.settle_ms
            if settled or now - self.pending_since >= self.cfg.max_settle_ms:
                return [self._emit(now, "change")]
        if self.pending_utts and any(end <= now for _, end in self.pending_utts):
            return [self._emit(now, "utterance")]
        hb = self.cfg.focus_heartbeat_s if self.focus else self.cfg.heartbeat_s
        if self.last_emit_ms is not None and now - self.last_emit_ms >= hb * 1000:
            return [self._emit(now, "focus" if self.focus else "heartbeat")]
        return []

    def _emit(self, now: int, trigger: str) -> WindowRequest:
        frames = [f for f in self.buf if self.last_end_ms < f.pts_ms <= now] or [self.buf[-1]]
        k = self.cfg.frames_per_window
        if len(frames) > k:  # 均匀取 k 帧，必含末帧
            idx = sorted({int(round(i * (len(frames) - 1) / (k - 1))) for i in range(k)})
            frames = [frames[i] for i in idx]
        utts = [u for u, end in self.pending_utts if end <= now]
        self.pending_utts = [(u, end) for u, end in self.pending_utts if end > now]
        req = WindowRequest(window_id=f"w_{self.seq:03d}", seq=self.seq, start_ms=self.last_end_ms, end_ms=now,
                            t_request_ms=now, trigger=trigger, frame_ids=[f.frame_id for f in frames], utt_ids=utts,
                            focus_entities=sorted({e for ents in self.focus.values() for e in ents}))
        self.seq += 1
        self.last_emit_ms = now
        self.last_end_ms = now
        self.ref = self.buf[-1].small
        self.pending_since = None
        return req
