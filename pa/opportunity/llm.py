"""模型负责“估计”，规则负责“选择”（设计说明 §5.4 C10）。

- ProbModel：对一个机会回答两个类型化概率问题——p_cond（假设截止事件发生，问题真的会发生的概率）与
  q（不提醒也会自己处理的概率）。DeepSeek 实现；Jev（TypeSafe System One）实现走同一接口，用 noul 题型。
- OpenProposer：受约束的开放发现（来源 ✦）。每隔一段时间看一眼状态和最近对话，提出 0–2 个候选机会；
  是否开口仍由价值门控决定，类别为 info 的机会损失只有 1 分，基本不会开口。
"""
from __future__ import annotations

import json
import os
import time

from ..llm import ChatJSON

PROB_SYSTEM = """你在为一个家庭主动助理估计概率。输入是一个“机会”（某人可能遇到的问题）、当前状态、最近对话和人物目标估计。
回答两个概率（0–1，两位小数）：
- p_cond：假设截止事件真的发生（deadline=goal:leave_home 就假设他们确实要出门；time:... 就假设时间到了），
  这个问题真的会发生、助理提醒确实有用的概率。证据薄弱、只是闲聊提到、或已经处理过时要给低值。
- q：不提醒的情况下，当事人自己会想起并处理的概率。
注意：别人口头提醒过 ≠ 已经处理；人在赶时间、聊天时很容易把几分钟前听到的叮嘱忘掉。只有看到实际行动
（如已经拿起伞、已经关掉）才算处理。对着助理说的“提醒我…”，没有别人会替它提醒，q 应很低。
只输出 JSON：{"p_cond": 0.0, "q": 0.0, "why": "一句中文理由"}"""

OPEN_SYSTEM = """你是家庭主动助理的“机会发现”模块。根据当前状态、最近对话和人物目标估计，找出接下来几分钟内某人可能遇到麻烦、
而助理及时提醒能帮上忙的情况：答应要做 / 要提醒的事、可能忘带的东西、物品被挪而本人不知道、安全隐患、约好的时间到了等。
规则：
- 绝大多数时候没有这样的情况，返回空列表。闲聊里提到的喜好、观点不是机会。
- 大家正在说、显然已经知道的事不是机会（例如他们已经在说要迟到了，就不要再提醒“快点出发”）；
  只提他们可能没意识到、或过一会儿会忘的事。
- “可能忘带 / 忘收”类只在有人明确准备出门或离开（people_goals 里出门概率高，或对话里说要走）时才提；
  桌上放着东西本身不是机会。
- 不要重复 active 里已有的机会（换个说法也算重复）。
- evidence 必须引用输入里真实出现的状态键或对话原句。
- category：safety（人身 / 财产安全）| essential（出门必需品、吃药、约定）| convenience（省事）| info（信息）。
- deadline：goal:leave_home（某人出门前）| asap（现在）| none（没有明确时限）。
输出 JSON：{"opportunities": [{"key": "英文短标识", "addressee": ["人名或实体ID"], "problem": "一句中文",
 "category": "...", "deadline": "...", "evidence": ["..."], "p": 0.0, "q": 0.0}]}，最多 2 个。"""

JEV_URL = "https://api.typesafe.ai/v1/systemone"


class ProbModel:
    def __init__(self, chat: ChatJSON):
        self.chat = chat
        self.name = f"llm:{chat.model}"

    def estimate(self, ctx: dict) -> dict:
        data, text, latency, err = self.chat.complete(PROB_SYSTEM, json.dumps(ctx, ensure_ascii=False))
        if err or not isinstance(data, dict):
            return {"error": err or text[:120], "latency_ms": latency}
        try:
            p = max(0.0, min(1.0, float(data.get("p_cond"))))
            q = max(0.0, min(1.0, float(data.get("q"))))
        except (TypeError, ValueError):
            return {"error": "invalid", "latency_ms": latency}
        return {"p_cond": p, "q": q, "why": str(data.get("why", ""))[:120], "latency_ms": latency, "model": self.name}


class JevProbModel:
    """TypeSafe System One：两个 noul 问题。接口同 decision.JevDecider（docs.typesafe.ai/api/system-one）。"""

    def __init__(self, model: str = "jev-latest", api_key_env: str = "TYPESAFE_API_KEY", timeout_s: float = 3.0):
        import httpx

        self.model, self.name = model, f"jev:{model}"
        self.key = os.environ.get(api_key_env, "")
        self.http = httpx.Client(timeout=timeout_s)

    def estimate(self, ctx: dict) -> dict:
        body = {"model": self.model, "state": ctx, "questions": {
            "p_cond": {"type": "noul", "instructions": "Assuming the deadline event happens, how likely is it that the "
                                                         "problem really occurs and a reminder would help?"},
            "q": {"type": "noul", "instructions": "How likely is it that the person handles it without a reminder?"}}}
        t0 = time.perf_counter()
        try:
            r = self.http.post(JEV_URL, json=body, headers={"Authorization": f"Bearer {self.key}"})
            lat = int((time.perf_counter() - t0) * 1000)
            if r.status_code != 200:
                return {"error": f"http {r.status_code}", "latency_ms": lat}
            ans = r.json()["answers"]
            return {"p_cond": float(ans["p_cond"]["noul"]), "q": float(ans["q"]["noul"]), "why": "jev",
                    "latency_ms": lat, "model": self.name}
        except Exception as e:  # noqa: BLE001
            return {"error": f"{type(e).__name__}: {str(e)[:100]}", "latency_ms": int((time.perf_counter() - t0) * 1000)}


class OpenProposer:
    def __init__(self, chat: ChatJSON):
        self.chat = chat
        self.name = f"llm:{chat.model}"

    def propose(self, ctx: dict) -> dict:
        data, text, latency, err = self.chat.complete(OPEN_SYSTEM, json.dumps(ctx, ensure_ascii=False))
        if err or not isinstance(data, dict):
            return {"error": err or text[:120], "latency_ms": latency, "opportunities": []}
        out = []
        for o in (data.get("opportunities") or [])[:2]:
            if not isinstance(o, dict) or not o.get("problem"):
                continue
            try:
                out.append({"key": str(o.get("key") or "open")[:40], "addressee": [str(a) for a in (o.get("addressee") or [])][:3],
                            "problem": str(o["problem"])[:80], "category": str(o.get("category", "info")),
                            "deadline": str(o.get("deadline", "none")), "evidence": [str(x)[:80] for x in (o.get("evidence") or [])][:3],
                            "p": max(0.0, min(1.0, float(o.get("p", 0.3)))), "q": max(0.0, min(1.0, float(o.get("q", 0.3))))})
            except (TypeError, ValueError):
                continue
        return {"opportunities": out, "latency_ms": latency}
