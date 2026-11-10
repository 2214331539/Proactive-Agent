"""OpenAI 兼容接口的薄封装：OpenAI、DashScope(Qwen-VL)、Gemini 兼容端点、本地 Ollama / vLLM 都走这里。"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np


@dataclass
class ProviderCfg:
    base_url: str | None = None
    api_key_env: str = "OPENAI_API_KEY"
    model: str = ""
    timeout_s: float = 10.0
    json_mode: str = "json_object"  # json_object | none
    extra: dict[str, Any] = field(default_factory=dict)  # 透传给 chat.completions.create，例如 max_completion_tokens


def parse_json_loose(text: str | None) -> Any:
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", t, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


def image_part(img: np.ndarray, quality: int = 80) -> dict:
    from PIL import Image

    buf = io.BytesIO()
    Image.fromarray(img).save(buf, format="JPEG", quality=quality)
    b64 = base64.b64encode(buf.getvalue()).decode()
    return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}


class ChatJSON:
    """返回 (dict | None, 原始文本, 时延 ms, 错误信息)。不重试：热路径上失败就让下一个窗口补上。"""

    def __init__(self, cfg: ProviderCfg):
        from openai import OpenAI

        self.cfg = cfg
        key = os.environ.get(cfg.api_key_env) or "EMPTY"
        self.client = OpenAI(base_url=cfg.base_url or None, api_key=key, timeout=cfg.timeout_s, max_retries=0)

    @property
    def model(self) -> str:
        return self.cfg.model

    def complete(self, system: str, user: str | list[dict]) -> tuple[Any, str, int, str | None]:
        kwargs: dict[str, Any] = dict(model=self.cfg.model, messages=[
            {"role": "system", "content": system}, {"role": "user", "content": user}], **self.cfg.extra)
        if self.cfg.json_mode == "json_object":
            kwargs["response_format"] = {"type": "json_object"}
        t0 = time.perf_counter()
        try:
            resp = self.client.chat.completions.create(**kwargs)
            text = resp.choices[0].message.content or ""
            err = None
        except Exception as e:  # noqa: BLE001  网络 / 配额 / 超时都在这里归一
            text, err = "", f"{type(e).__name__}: {str(e)[:200]}"
        latency = int((time.perf_counter() - t0) * 1000)
        data = parse_json_loose(text) if err is None else None
        if err is None and data is None:
            err = "json_parse_failed"
        return data, text, latency, err
