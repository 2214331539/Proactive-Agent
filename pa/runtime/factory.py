"""根据配置构建各个可替换组件（VLM、决策器、草稿器、慢环 LLM）。"""
from __future__ import annotations

from pathlib import Path

from ..decision import JevDecider, LLMDecider, RuleDecider
from ..llm import ChatJSON, ProviderCfg
from ..perception import OpenAICompatVLM, ScriptedVLM
from ..response import LLMDrafter, OppLLMDrafter, TemplateDrafter
from ..storage import PatchCache


def provider_cfg(cfg: dict, provider: str, model: str, timeout_s: float, extra: dict | None = None) -> ProviderCfg:
    p = cfg["providers"].get(provider)
    if p is None:
        raise ValueError(f"未知 provider: {provider}（在 config.providers 里添加）")
    if not model:
        raise ValueError(f"provider={provider} 需要指定 model")
    import os

    base_url = p.get("base_url") or (os.environ.get(p["base_url_env"]) if p.get("base_url_env") else None)
    return ProviderCfg(base_url=base_url, api_key_env=p.get("api_key_env", "OPENAI_API_KEY"), model=model,
                       timeout_s=timeout_s, json_mode=p.get("json_mode", "json_object"), extra=extra or {})


def build_vlm(cfg: dict, scenario: dict | None = None, cache_path: str | Path = "data/cache/patch_cache.sqlite"):
    pc = cfg["perception"]
    if pc["provider"] == "dry":  # 不调模型：只跑门控 / 调度，用来估计窗口频率与成本
        return ScriptedVLM([], None, latency_ms=int(pc.get("dry_latency_ms", 1500)), name="dry")
    if pc["provider"] == "scripted":
        if not scenario:
            raise ValueError("perception.provider=scripted 需要剧本（scenarios/*.yaml）")
        return ScriptedVLM(scenario.get("observations", []), scenario.get("default_view"),
                           latency_ms=scenario.get("vlm_latency_ms", 1100))
    cache = PatchCache(cache_path) if pc.get("cache", True) else None
    return OpenAICompatVLM(provider_cfg(cfg, pc["provider"], pc["model"], pc["timeout_s"], pc.get("extra")), cache=cache)


def build_decider(cfg: dict):
    dc = cfg["decision"]
    if dc["provider"] == "rule":
        return RuleDecider(latency_ms=dc.get("rule_latency_ms", 800))
    if dc["provider"] == "jev":
        return JevDecider(model=dc.get("model") or "jev-latest", timeout_s=dc.get("timeout_s", 2))
    if dc["provider"] == "llm":
        return LLMDecider(ChatJSON(provider_cfg(cfg, dc["llm_provider"], dc["model"], dc.get("timeout_s", 2) * 3,
                                                dc.get("extra"))))
    raise ValueError(f"未知 decision.provider: {dc['provider']}")


def build_drafter(cfg: dict):
    rc = cfg["response"]
    if rc["provider"] == "template":
        return TemplateDrafter()
    return LLMDrafter(ChatJSON(provider_cfg(cfg, rc["llm_provider"], rc["model"], 6, rc.get("extra"))))


def build_pa_parts(cfg: dict, mode: str = "sim") -> dict:
    """主动决策核心的可替换部件：概率模型（C10）、开放发现（C9 ✦）、口语化草稿（C12）、豆包语音。"""
    pc = cfg.get("proactive", {})
    if not pc.get("enabled"):
        return {}
    from ..opportunity import JevProbModel, OpenProposer, ProbModel
    from ..output import DoubaoTTS

    parts: dict = {}
    dc = cfg["decision"]
    if pc.get("model_estimates", True):
        if dc["provider"] == "llm":
            parts["prob"] = ProbModel(ChatJSON(provider_cfg(cfg, dc["llm_provider"], dc["model"], dc.get("timeout_s", 2) * 3,
                                                            dc.get("extra"))))
        elif dc["provider"] == "jev":
            parts["prob"] = JevProbModel(model=dc.get("model") or "jev-latest", timeout_s=dc.get("timeout_s", 3))
    sc = cfg["slow"]
    if pc.get("open", True) and sc.get("provider", "none") != "none":
        parts["opener"] = OpenProposer(ChatJSON(provider_cfg(cfg, sc["llm_provider"], sc["model"], 30, sc.get("extra"))))
    rc = cfg["response"]
    if rc["provider"] == "llm":
        parts["drafter"] = OppLLMDrafter(ChatJSON(provider_cfg(cfg, rc["llm_provider"], rc["model"], 8, rc.get("extra"))))
    tc = cfg.get("tts", {})
    if tc.get("enabled") and mode == "realtime":
        tts = DoubaoTTS(api=tc.get("api", "seed-audio"), api_key_env=tc.get("api_key_env", "DOUBAO_TTS_API_KEY"),
                        voice=tc.get("voice", ""), style=tc.get("style", ""), cache_dir=tc.get("cache_dir", "data/cache/tts"),
                        timeout_s=tc.get("timeout_s", 120), speech_rate=tc.get("speech_rate", 0))
        if tts.available:
            parts["tts"] = tts
    return parts


def build_slow_llm(cfg: dict) -> ChatJSON | None:
    sc = cfg["slow"]
    if sc.get("provider", "none") == "none":
        return None
    return ChatJSON(provider_cfg(cfg, sc["llm_provider"], sc["model"], 30, sc.get("extra")))
