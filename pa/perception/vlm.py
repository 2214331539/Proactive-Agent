"""M4 Perception：状态条件化的 VLM 调用。提供脚本化实现（离线测试）和 OpenAI 兼容实现（真实模型）。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np

from ..llm import ChatJSON, ProviderCfg, image_part
from ..schemas import StateSlice, Utterance, WindowRequest
from .prompt import PROMPT_VERSION, SYSTEM_PROMPT, render_user_text


@dataclass
class VLMResult:
    raw: dict | None
    text: str
    latency_ms: int
    error: str | None
    model: str
    cached: bool = False


class VLM(Protocol):
    name: str

    def propose(self, req: WindowRequest, sl: StateSlice, frames: list[np.ndarray], frame_times: list[int],
                utts: list[Utterance], face_note: str | None = None) -> VLMResult: ...


class ScriptedVLM:
    """按剧本返回补丁，模拟“一个完美但有延迟的 VLM”。用于 E0 逻辑测试和演示，不代表真实感知能力。

    剧本条目：{"t_ms": 2000, "ops": [...], "view": {...只作用于该窗口...}, "set_view": {...此后持续生效...}}。
    覆盖条目时间的第一个窗口（start < t_ms <= end）返回这些 op；CHECK 条目若所在地点在本窗口可见则补 still。
    """

    def __init__(self, observations: list[dict], default_view: dict | None = None, latency_ms: int = 1100,
                 name: str = "scripted"):
        self.obs = sorted(observations, key=lambda o: o["t_ms"])
        self.default_view = dict(default_view or {})
        self.latency_ms = latency_ms
        self.name = name

    def propose(self, req, sl, frames, frame_times, utts, face_note=None) -> VLMResult:
        ops: list[dict] = []
        once: dict = {}
        for o in self.obs:
            if req.start_ms < o["t_ms"] <= req.end_ms:
                ops += [dict(op) for op in o.get("ops", [])]
                if "set_view" in o:
                    self.default_view = {**self.default_view, **o["set_view"]}
                if "view" in o:
                    once = {**once, **o["view"]}
        view = {**self.default_view, **once}
        mentioned = {op.get("key") for op in ops if "key" in op}
        for op in ops:
            mentioned.update(op.get("keys", []))
            if op.get("op") == "act" and op.get("type") in ("PICK_UP", "PUT_DOWN", "PUT_INTO", "TAKE_OUT", "GIVE"):
                mentioned.add(f"{op['obj']}.location")
        visible = set(view.get("visible_places", []))
        persons = set(view.get("persons_visible") or [])
        still, unsure = [], []
        for key in sl.check_keys:
            if key in mentioned:
                continue
            eid, attr = key.split(".", 1)
            ent = next((e for e in sl.entities if e.id == eid), None)
            if ent is None:
                continue
            if attr == "activity":
                ok = eid in persons
            else:
                ref = ent.beliefs.get("location", "").split(" (")[0].split(" ")[-1]
                ok = ref in visible and view.get("quality", "clear") != "poor"
            (still if ok else unsure).append(key)
        if still:
            ops.append({"op": "still", "keys": still})
        if unsure:
            ops.append({"op": "unsure", "keys": unsure, "reason": "out_of_view"})
        raw = {"view": view or None, "ops": ops}
        return VLMResult(raw=raw, text=json.dumps(raw, ensure_ascii=False), latency_ms=self.latency_ms,
                         error=None, model=self.name)


class OpenAICompatVLM:
    """任何 OpenAI 兼容端点：OpenAI、DashScope(Qwen-VL)、Gemini 兼容层、本地 Ollama / vLLM。"""

    def __init__(self, cfg: ProviderCfg, cache=None):
        self.chat = ChatJSON(cfg)
        self.name = cfg.model
        self.cache = cache

    def propose(self, req, sl, frames, frame_times, utts, face_note=None) -> VLMResult:
        user_text = render_user_text(req, sl, utts, frame_times, face_note)
        key = None
        if self.cache is not None:
            h = hashlib.sha1()
            h.update(f"{self.name}|{PROMPT_VERSION}|{user_text}".encode())
            for f in frames:
                h.update(np.ascontiguousarray(f[::8, ::8]).tobytes())
            key = h.hexdigest()
            hit = self.cache.get(key)
            if hit is not None:
                return VLMResult(raw=hit["raw"], text=hit["text"], latency_ms=hit["latency_ms"], error=None,
                                 model=self.name, cached=True)
        content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
        for fid, t, img in zip(req.frame_ids, frame_times, frames):
            content.append({"type": "text", "text": f"{fid}"})
            content.append(image_part(img))
        data, text, latency, err = self.chat.complete(SYSTEM_PROMPT, content)
        if err is None and not isinstance(data, dict):
            err, data = "not_object", None
        if err is None and self.cache is not None:
            self.cache.put(key, {"raw": data, "text": text, "latency_ms": latency}, model=self.name,
                           prompt_version=PROMPT_VERSION)
        return VLMResult(raw=data, text=text, latency_ms=latency, error=err, model=self.name)
