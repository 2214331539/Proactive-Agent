"""M7 决策（设计文档 §3 M7、§6.2）。

- JevDecider：TypeSafe System One（POST https://api.typesafe.ai/v1/systemone，Bearer $TYPESAFE_API_KEY），
  一次调用两个问题：warranted(noul) + mode(choice)。接口按官方文档核实（docs.typesafe.ai/api/system-one）。
- LLMDecider：任意 OpenAI 兼容模型，输出同样的字段，作为对照或替代。
- RuleDecider：离线基线（不调模型）。只用于跑通流程与消融对照，不代表“智能决策”。
所有决策器的输出都经过 apply_rules()：阈值、mode_cap 只降不升、权限。
"""
from __future__ import annotations

import json
import os
import time
from typing import Protocol

from ..llm import ChatJSON
from ..schemas import MODE_ORDER, Decision

JEV_URL = "https://api.typesafe.ai/v1/systemone"
MODE_CRITERIA = {
    "WAIT": "Keep observing; evidence or benefit is insufficient, or interrupting now costs more than it helps.",
    "ASK": "Clarify a meaningful uncertainty with a short question, without asserting it as fact.",
    "REMIND": "Deliver a grounded, timely reminder; the key facts are directly observed and current.",
}
WARRANTED_INSTR = ("Is an intervention warranted right now, given the observed state, its uncertainty "
                   "(status/source/age of each belief), the time pressure and the cost of interrupting the user?")
MODE_INSTR = "Choose the appropriate action under the stated permissions and mode_cap."


class Decider(Protocol):
    name: str

    def decide(self, ctx: dict) -> Decision: ...


def _base(ctx: dict, model: str) -> dict:
    return {"decision_id": f"dec_{ctx['context_id'].split('_', 1)[1]}", "context_id": ctx["context_id"],
            "need_id": ctx["need_id"], "model": model}


class RuleDecider:
    """离线基线：目标信念亲眼所见且新鲜 → mode_cap；否则 ASK。warranted 是固定值，不是概率。"""

    def __init__(self, latency_ms: int = 800):
        self.name = "rule-baseline"
        self.latency_ms = latency_ms

    def decide(self, ctx: dict) -> Decision:
        cap = ctx["need"]["mode_cap"]
        return Decision(**_base(ctx, self.name), status="ok", warranted=0.9, mode=cap,
                        mode_probs={cap: 1.0}, reason="rule: mode = mode_cap", latency_ms=self.latency_ms)


class LLMDecider:
    SYSTEM = ("You decide whether a proactive assistant should interrupt the user right now. You receive a JSON "
              "DecisionContext built from a video-derived belief state. Beliefs with status stale/unknown are NOT facts. "
              "If need.kind is user_request, someone explicitly asked for this reminder (see need.summary); when its "
              "trigger fires, reminding is the expected behaviour unless there is evidence it is already done — an earlier "
              "verbal reminder by a person does NOT count as done. "
              "Respect permissions and never choose a mode above need.mode_cap. Reply with JSON only: "
              '{"warranted": <probability 0..1 that intervening now is worth it>, "mode": "WAIT"|"ASK"|"REMIND", '
              '"reason": "<one short sentence>"}.\nMode criteria: ' + json.dumps(MODE_CRITERIA))

    def __init__(self, chat: ChatJSON):
        self.chat = chat
        self.name = f"llm:{chat.model}"

    def decide(self, ctx: dict) -> Decision:
        data, text, latency, err = self.chat.complete(self.SYSTEM, json.dumps(ctx, ensure_ascii=False))
        base = _base(ctx, self.name)
        if err or not isinstance(data, dict):
            return Decision(**base, status="unavailable", reason=err, latency_ms=latency)
        mode = str(data.get("mode", "")).upper()
        try:
            w = float(data.get("warranted"))
        except (TypeError, ValueError):
            w = None
        if mode not in MODE_ORDER or w is None:
            return Decision(**base, status="invalid", reason=text[:200], latency_ms=latency)
        return Decision(**base, status="ok", warranted=max(0.0, min(1.0, w)), mode=mode,
                        reason=str(data.get("reason", ""))[:300], latency_ms=latency)


class JevDecider:
    def __init__(self, model: str = "jev-latest", api_key_env: str = "TYPESAFE_API_KEY", timeout_s: float = 2.0,
                 base_url: str = JEV_URL, transport=None):
        import httpx

        self.model = model
        self.name = f"jev:{model}"
        self.url = base_url
        self.timeout_s = timeout_s
        self.key = os.environ.get(api_key_env, "")
        self.http = httpx.Client(timeout=timeout_s, transport=transport)

    def decide(self, ctx: dict) -> Decision:
        state = {k: v for k, v in ctx.items() if k not in ("dep_revs",)}
        body = {"model": self.model, "state": state, "questions": {
            "warranted": {"type": "noul", "instructions": WARRANTED_INSTR},
            "mode": {"type": "choice", "instructions": MODE_INSTR, "criteria": MODE_CRITERIA},
        }}
        base = _base(ctx, self.name)
        t0 = time.perf_counter()
        try:
            r = self.http.post(self.url, json=body, headers={"Authorization": f"Bearer {self.key}"})
            latency = int((time.perf_counter() - t0) * 1000)
            if r.status_code != 200:
                return Decision(**base, status="unavailable", reason=f"http {r.status_code}: {r.text[:160]}", latency_ms=latency)
            data = r.json()
            ans = data["answers"]
            w = float(ans["warranted"]["noul"])
            m = ans["mode"]
            return Decision(**{**base, "model": f"jev:{data.get('model', self.model)}"}, status="ok", warranted=w,
                            mode=m["choice"], mode_probs={k: float(v) for k, v in (m.get("probabilities") or {}).items()},
                            reason=f"confidence={m.get('confidence')}", latency_ms=latency)
        except Exception as e:  # noqa: BLE001
            return Decision(**base, status="unavailable", reason=f"{type(e).__name__}: {str(e)[:160]}",
                            latency_ms=int((time.perf_counter() - t0) * 1000))


def apply_rules(dec: Decision, mode_cap: str, permissions: dict, threshold: float) -> Decision:
    """程序规则：阈值；只降级不升级；权限。warranted/mode_probs 是决策输出，不是“事实概率”。"""
    notes: list[str] = []
    if dec.status != "ok":
        applied = "DECISION_UNAVAILABLE"
    elif dec.warranted is not None and dec.warranted < threshold:
        applied = "WAIT"
        notes.append(f"warranted {dec.warranted:.2f} < {threshold}")
    elif dec.mode == "WAIT" or dec.mode is None:
        applied = "WAIT"
    else:
        applied = dec.mode
        if MODE_ORDER[applied] > MODE_ORDER.get(mode_cap, 1):
            notes.append(f"downgrade {applied}→{mode_cap} (mode_cap)")
            applied = mode_cap
        if applied == "REMIND" and not permissions.get("may_remind", True):
            applied = "ASK" if permissions.get("may_ask", True) else "WAIT"
            notes.append("no may_remind")
        if applied == "ASK" and not permissions.get("may_ask", True):
            applied = "WAIT"
            notes.append("no may_ask")
    return dec.model_copy(update={"applied_mode": applied, "rule_notes": notes})
