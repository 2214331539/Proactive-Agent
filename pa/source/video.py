"""M1 Source：PyAV 解码视频 / 音频（文件、RTSP），以及离线测试用的合成画面源。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

import numpy as np


@dataclass
class FrameItem:
    pts_ms: int
    img: np.ndarray  # RGB uint8


@dataclass
class AudioChunk:
    start_ms: int
    end_ms: int
    pcm: np.ndarray  # float32 mono 16 kHz


class VideoSource:
    """按 sample_fps 抽帧，缩放到短边 short_side。start_s / duration_s 用于截取长视频的一段。"""

    def __init__(self, path: str, sample_fps: float = 5.0, short_side: int = 448, start_s: float = 0.0,
                 duration_s: float | None = None):
        self.path = path
        self.sample_fps = sample_fps
        self.short_side = short_side
        self.start_s = start_s
        self.duration_s = duration_s

    def info(self) -> dict:
        import av

        with av.open(self.path) as c:
            v = c.streams.video[0]
            dur = float(c.duration / 1e6) if c.duration else (float(v.duration * v.time_base) if v.duration else None)
            return {"duration_s": dur, "width": v.width, "height": v.height,
                    "fps": float(v.average_rate) if v.average_rate else None, "has_audio": bool(c.streams.audio)}

    def frames(self) -> Iterator[FrameItem]:
        import av

        end_s = None if self.duration_s is None else self.start_s + self.duration_s
        with av.open(self.path) as c:
            vs = c.streams.video[0]
            vs.thread_type = "AUTO"
            if self.start_s > 0:
                c.seek(int(self.start_s * 1_000_000), any_frame=False, backward=True)
            w, h = vs.width, vs.height
            scale = self.short_side / min(w, h)
            ow, oh = (int(round(w * scale)) // 2 * 2, int(round(h * scale)) // 2 * 2) if scale < 1 else (w, h)
            next_t = self.start_s
            for frame in c.decode(vs):
                if frame.time is None:
                    continue
                t = float(frame.time)
                if t < next_t - 1e-6:
                    continue
                if end_s is not None and t > end_s:
                    break
                next_t = max(next_t + 1.0 / self.sample_fps, t)
                img = frame.to_ndarray(format="rgb24", width=ow, height=oh)
                yield FrameItem(pts_ms=int(round((t - self.start_s) * 1000)), img=img)

    def audio(self, chunk_s: float = 8.0, sr: int = 16000) -> Iterator[AudioChunk]:
        import av

        end_s = None if self.duration_s is None else self.start_s + self.duration_s
        with av.open(self.path) as c:
            if not c.streams.audio:
                return
            a = c.streams.audio[0]
            if self.start_s > 0:
                c.seek(int(self.start_s * 1_000_000), any_frame=False, backward=True)
            rs = av.AudioResampler(format="s16", layout="mono", rate=sr)
            buf: list[np.ndarray] = []
            buf_start: float | None = None
            n = 0
            for frame in c.decode(a):
                if frame.time is None:
                    continue
                t = float(frame.time)
                if t < self.start_s:
                    continue
                if end_s is not None and t > end_s:
                    break
                for rf in rs.resample(frame):
                    arr = rf.to_ndarray().reshape(-1).astype(np.float32) / 32768.0
                    if buf_start is None:
                        buf_start = t - self.start_s
                    buf.append(arr)
                    n += len(arr)
                    if n >= chunk_s * sr:
                        pcm = np.concatenate(buf)
                        yield AudioChunk(int(buf_start * 1000), int((buf_start + len(pcm) / sr) * 1000), pcm)
                        buf, n, buf_start = [], 0, None
            if buf:
                pcm = np.concatenate(buf)
                yield AudioChunk(int(buf_start * 1000), int((buf_start + len(pcm) / sr) * 1000), pcm)


class SyntheticSource:
    """离线测试用：灰色背景上按剧本出现 / 移动色块，让 Gate 的帧差在剧本事件时刻自然触发。"""

    def __init__(self, duration_s: float, changes: list[dict], fps: float = 5.0, size: tuple[int, int] = (252, 448)):
        self.duration_s = duration_s
        self.changes = sorted(changes, key=lambda c: c["t_ms"])
        self.fps = fps
        self.size = size

    def frames(self) -> Iterator[FrameItem]:
        h, w = self.size
        canvas = np.full((h, w, 3), 128, np.uint8)
        idx = 0
        n = int(self.duration_s * self.fps)
        for i in range(n + 1):
            t = int(round(i * 1000 / self.fps))
            while idx < len(self.changes) and self.changes[idx]["t_ms"] <= t:
                x0, y0, x1, y1 = self.changes[idx].get("box", [10, 10, 60, 60])
                canvas[y0:y1, x0:x1] = self.changes[idx].get("color", [200, 60, 60])
                idx += 1
            yield FrameItem(pts_ms=t, img=canvas.copy())

    def audio(self, chunk_s: float = 8.0, sr: int = 16000) -> Iterator[AudioChunk]:
        return iter(())
