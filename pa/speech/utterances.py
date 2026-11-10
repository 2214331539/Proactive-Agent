"""M3 语音：把音频变成带时间戳的语句。

- SrtUtterances：读取字幕（M3-Bench robot 自带 subtitles/robot/*.srt），作为“理想 ASR”。语句在 end + 时延 后才可用。
- WhisperUtterances：faster-whisper 按块流式转写（因果：只用已到达的音频块）。可选依赖 `pip install faster-whisper`。
说话人分离未实现：M3-Agent 的 voice_processing 依赖 Gemini 做 ASR+分离，ERes2NetV2 需要 3D-Speaker 的 speakerlab，
这里暂不引入；speaker 由 VLM 结合画面在 said op 中给出。
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from ..schemas import Utterance

_TS = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)")


def _ms(h, m, s, f) -> int:
    return ((int(h) * 60 + int(m)) * 60 + int(s)) * 1000 + int(f.ljust(3, "0")[:3])


def parse_srt(path: str | Path) -> list[tuple[int, int, str]]:
    text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    out = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        for i, ln in enumerate(lines):
            m = _TS.search(ln)
            if m:
                g = m.groups()
                body = " ".join(lines[i + 1:])
                if body:
                    out.append((_ms(*g[:4]), _ms(*g[4:]), body))
                break
    return out


_SPK = re.compile(r"^\s*([^:：]{1,24})[:：]\s*(.+)$")


class SrtUtterances:
    """M3-Bench 字幕形如 "Marry:the cappuccino today is very delicious"，冒号前的名字作为说话人标签。"""

    def __init__(self, path: str | Path, offset_s: float = 0.0, latency_ms: int = 500):
        self.items = parse_srt(path)
        self.offset_ms = int(offset_s * 1000)
        self.latency_ms = latency_ms

    def all(self, duration_ms: int | None = None) -> list[Utterance]:
        out = []
        for i, (s, e, text) in enumerate(self.items):
            s2, e2 = s - self.offset_ms, e - self.offset_ms
            if e2 <= 0 or (duration_ms is not None and s2 > duration_ms):
                continue
            m = _SPK.match(text)
            speaker, body = (m.group(1).strip(), m.group(2).strip()) if m else (None, text)
            out.append(Utterance(utt_id=f"u_{i:04d}", start_ms=max(0, s2), end_ms=e2, text=body,
                                 voice_anchor=speaker, t_avail_ms=e2 + self.latency_ms))
        return out


class WhisperUtterances:
    def __init__(self, model_size: str = "small", device: str = "auto", compute_type: str = "default",
                 language: str | None = None):
        from faster_whisper import WhisperModel

        self.model = WhisperModel(model_size, device=device, compute_type=compute_type)
        self.language = language
        self.n = 0

    def transcribe_chunk(self, start_ms: int, end_ms: int, pcm) -> tuple[list[Utterance], int]:
        t0 = time.perf_counter()
        segs, _ = self.model.transcribe(pcm, language=self.language, vad_filter=True, beam_size=1)
        segs = list(segs)
        latency = int((time.perf_counter() - t0) * 1000)
        out = []
        for s in segs:
            text = s.text.strip()
            if not text:
                continue
            out.append(Utterance(utt_id=f"u_{self.n:04d}", start_ms=start_ms + int(s.start * 1000),
                                 end_ms=start_ms + int(s.end * 1000), text=text, t_avail_ms=end_ms + latency,
                                 asr_conf=float(getattr(s, "avg_logprob", 0.0))))
            self.n += 1
        return out, latency
