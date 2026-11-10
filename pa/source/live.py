"""M1 实时流源：udp:// rtsp:// rtmp:// http:// srt:// 或本机摄像头。

后台线程持续解码，帧时间戳 = 到达时的墙钟（会话开始后的毫秒），而不是文件里的 PTS：
系统看到的是“此刻到达的画面”，不能提前读、也不能回看未来。主循环用 drain() 非阻塞地取帧。
"""
from __future__ import annotations

import queue
import threading
import time

from .video import AudioChunk, FrameItem  # noqa: F401

LIVE_PREFIXES = ("udp://", "rtsp://", "rtmp://", "http://", "https://", "srt://", "tcp://")


def is_live_url(s: str) -> bool:
    return s.startswith(LIVE_PREFIXES) or s.startswith("video=")


class LiveSource:
    live = True

    def __init__(self, url: str, sample_fps: float = 5.0, short_side: int = 448, open_timeout_s: float = 30.0,
                 read_timeout_s: float = 10.0, fmt: str | None = None):
        self.path = url
        self.sample_fps = sample_fps
        self.short_side = short_side
        self.open_timeout_s = open_timeout_s
        self.read_timeout_s = read_timeout_s
        self.fmt = fmt or ("dshow" if url.startswith("video=") else None)
        self.q: queue.Queue[FrameItem] = queue.Queue(maxsize=600)
        self.done = False
        self.error: str | None = None
        self.n_decoded = 0
        self.n_dropped = 0
        self._thread: threading.Thread | None = None

    def start(self, wall0: float) -> None:
        self._thread = threading.Thread(target=self._reader, args=(wall0,), daemon=True)
        self._thread.start()

    def _reader(self, wall0: float) -> None:
        import av

        try:
            c = av.open(self.path, format=self.fmt, timeout=(self.open_timeout_s, self.read_timeout_s))
            vs = c.streams.video[0]
            vs.thread_type = "AUTO"
            next_ms = 0.0
            size = None
            for frame in c.decode(vs):
                self.n_decoded += 1
                t_ms = (time.perf_counter() - wall0) * 1000
                if t_ms < next_ms:
                    continue
                next_ms = max(next_ms + 1000 / self.sample_fps, t_ms)
                if size is None:
                    w, h = frame.width, frame.height
                    s = min(1.0, self.short_side / min(w, h))
                    size = (int(round(w * s)) // 2 * 2, int(round(h * s)) // 2 * 2)
                img = frame.to_ndarray(format="rgb24", width=size[0], height=size[1])
                try:
                    self.q.put_nowait(FrameItem(pts_ms=int(t_ms), img=img))
                except queue.Full:  # 主循环跟不上时丢最旧的画面，而不是阻塞解码
                    self.n_dropped += 1
            c.close()
        except Exception as e:  # noqa: BLE001
            self.error = f"{type(e).__name__}: {e}"
        finally:
            self.done = True

    def drain(self) -> list[FrameItem]:
        out = []
        while True:
            try:
                out.append(self.q.get_nowait())
            except queue.Empty:
                return out

    def frames(self):
        raise RuntimeError("LiveSource 只能在 realtime 实时循环中使用（Engine 会调用 drain()）")

    def audio(self, chunk_s: float = 8.0, sr: int = 16000):
        return iter(())

    def info(self) -> dict:
        return {"duration_s": None, "width": None, "height": None, "fps": None, "has_audio": None, "live": True}
