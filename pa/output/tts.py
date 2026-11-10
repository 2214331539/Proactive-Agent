"""语音播报：豆包语音（火山引擎）。合成结果按文本哈希缓存成 mp3，界面通过 /tts/<hash>.mp3 播放。

两种接口（同一个 X-Api-Key）：
- seed-audio：POST /api/v3/tts/create，model=seed-audio-1.0。这是“生成式音频”模型，非流式，实测单句 13–25 s，
  偶尔超过 60 s——所以系统在机会“变热”时就提前合成（见 proactive/core.py），投递时多半已在缓存里。
- tts2：POST /api/v3/tts/unidirectional，X-Api-Resource-Id=seed-tts-2.0，流式，首包约 1 s。
  需要在火山引擎控制台为该 Key 开通“豆包语音合成 2.0”；未开通时返回 45000030 requested resource not granted。
音色 ID 见 docs.volcengine.com/docs/6561/1257544（如 zh_female_xiaohe_uranus_bigtts 小何）。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import uuid
from pathlib import Path

CREATE_URL = "https://openspeech.bytedance.com/api/v3/tts/create"
STREAM_URL = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"


def text_hash(text: str, voice: str = "") -> str:
    return hashlib.sha1(f"{voice}|{text}".encode("utf-8")).hexdigest()[:16]


class DoubaoTTS:
    def __init__(self, api: str = "seed-audio", api_key_env: str = "DOUBAO_TTS_API_KEY", voice: str = "",
                 style: str = "", cache_dir: str | Path = "data/cache/tts", timeout_s: float = 120,
                 sample_rate: int = 24000, speech_rate: int = 0):
        import httpx

        self.api, self.voice, self.style = api, voice, style
        self.key = os.environ.get(api_key_env, "")
        self.cache = Path(cache_dir)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.sample_rate, self.speech_rate = sample_rate, speech_rate
        self.http = httpx.Client(timeout=timeout_s)
        self.name = f"doubao:{api}"

    @property
    def available(self) -> bool:
        return bool(self.key)

    def path_for(self, text: str) -> Path:
        return self.cache / f"{text_hash(text, self.api + self.voice)}.mp3"

    def cached(self, text: str) -> Path | None:
        p = self.path_for(text)
        return p if p.exists() and p.stat().st_size > 0 else None

    def synthesize(self, text: str) -> dict:
        """返回 {"path", "hash", "latency_ms", "cached"} 或 {"error"}。线程安全（每次独立请求）。"""
        p = self.path_for(text)
        if p.exists() and p.stat().st_size > 0:
            return {"path": str(p), "hash": p.stem, "latency_ms": 0, "cached": True}
        t0 = time.perf_counter()
        try:
            audio = self._tts2(text) if self.api == "tts2" else self._seed_audio(text)
        except Exception as e:  # noqa: BLE001
            return {"error": f"{type(e).__name__}: {str(e)[:160]}", "latency_ms": int((time.perf_counter() - t0) * 1000)}
        tmp = p.with_suffix(".part")
        tmp.write_bytes(audio)
        tmp.replace(p)
        return {"path": str(p), "hash": p.stem, "latency_ms": int((time.perf_counter() - t0) * 1000), "cached": False}

    def _seed_audio(self, text: str) -> bytes:
        body = {"model": "seed-audio-1.0", "text_prompt": text,
                "audio_config": {"format": "mp3", "sample_rate": self.sample_rate, "pitch_rate": 0,
                                 "speech_rate": self.speech_rate, "loudness_rate": 0},
                "watermark": {}}
        if self.voice:
            body["references"] = [{"speaker": self.voice}]
            body["text_prompt"] = f"@Audio1 {self.style + '：' if self.style else ''}{text}"
        r = self.http.post(CREATE_URL, json=body, headers={"X-Api-Key": self.key, "X-Api-Request-Id": str(uuid.uuid4())})
        if r.status_code != 200:
            raise RuntimeError(f"http {r.status_code}: {r.text[:160]}")
        data = r.json()
        if not data.get("audio"):
            raise RuntimeError(f"no audio: {str(data)[:160]}")
        return base64.b64decode(data["audio"])

    def _tts2(self, text: str) -> bytes:
        body = {"user": {"uid": "proactive-agent"},
                "req_params": {"text": text, "speaker": self.voice or "zh_female_xiaohe_uranus_bigtts",
                               "audio_params": {"format": "mp3", "sample_rate": self.sample_rate,
                                                "speech_rate": self.speech_rate}}}
        if self.style:
            body["req_params"]["additions"] = json.dumps({"context_texts": [self.style]}, ensure_ascii=False)
        headers = {"X-Api-Key": self.key, "X-Api-Resource-Id": "seed-tts-2.0", "X-Api-Request-Id": str(uuid.uuid4())}
        chunks = []
        with self.http.stream("POST", STREAM_URL, json=body, headers=headers) as r:
            if r.status_code != 200:
                raise RuntimeError(f"http {r.status_code}: {r.read()[:160]!r}")
            for line in r.iter_lines():
                line = line.strip()
                if not line:
                    continue
                msg = json.loads(line)
                code = msg.get("code", 0)
                if msg.get("data"):
                    chunks.append(base64.b64decode(msg["data"]))
                if code == 20000000:
                    break
                if code not in (0, 20000000):
                    raise RuntimeError(f"tts2 code {code}: {msg.get('message')}")
        if not chunks:
            raise RuntimeError("tts2: empty audio")
        return b"".join(chunks)
