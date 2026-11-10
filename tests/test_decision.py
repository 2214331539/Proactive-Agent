"""M7 / M9：决策规则、Jev 请求与响应合同（按 docs.typesafe.ai 核实的字段，用 MockTransport 模拟）、模型不可用时的降级。"""
import json

import httpx

from pa.decision import JevDecider, apply_rules
from pa.schemas import Decision

CTX = {"context_id": "ctx_0007", "need_id": "need_1", "state_version": 4, "t_ms": 12300,
       "need": {"kind": "possible_forgotten_item", "mode_cap": "ASK", "bindings": {}}, "beliefs": [],
       "dep_revs": {"phone_1.location": 1}}


def dec(**kw):
    return Decision(decision_id="d", context_id="c", need_id="n", model="m", **kw)


def test_threshold_cap_and_permissions():
    assert apply_rules(dec(status="ok", warranted=0.5, mode="REMIND"), "REMIND", {}, 0.7).applied_mode == "WAIT"
    d = apply_rules(dec(status="ok", warranted=0.9, mode="REMIND"), "ASK", {}, 0.7)
    assert d.applied_mode == "ASK" and "downgrade" in d.rule_notes[0]  # 只降不升
    assert apply_rules(dec(status="ok", warranted=0.9, mode="ASK"), "REMIND", {}, 0.7).applied_mode == "ASK"
    assert apply_rules(dec(status="ok", warranted=0.9, mode="REMIND"), "REMIND", {"may_remind": False}, 0.7).applied_mode == "ASK"
    assert apply_rules(dec(status="unavailable"), "REMIND", {}, 0.7).applied_mode == "DECISION_UNAVAILABLE"


def test_jev_request_and_response_contract(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    seen = {}

    def handler(req: httpx.Request):
        seen["auth"] = req.headers["authorization"]
        seen["url"] = str(req.url)
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={
            "model": "jev-1.14.0",
            "answers": {"warranted": {"type": "noul", "noul": 0.86},
                        "mode": {"type": "choice", "choice": "REMIND", "confidence": 0.7,
                                 "probabilities": {"WAIT": 0.08, "ASK": 0.21, "REMIND": 0.71}}},
            "usage": {"input_tokens": 420, "output_tokens": 4}})

    d = JevDecider(model="jev-latest", transport=httpx.MockTransport(handler)).decide(CTX)
    assert seen["url"] == "https://api.typesafe.ai/v1/systemone" and seen["auth"] == "Bearer test-key"
    q = seen["body"]["questions"]
    assert q["warranted"]["type"] == "noul" and q["mode"]["type"] == "choice"
    assert set(q["mode"]["criteria"]) == {"WAIT", "ASK", "REMIND"}
    assert "dep_revs" not in seen["body"]["state"]
    assert d.status == "ok" and d.warranted == 0.86 and d.mode == "REMIND" and d.model == "jev:jev-1.14.0"
    assert apply_rules(d, CTX["need"]["mode_cap"], {}, 0.7).applied_mode == "ASK"


def test_jev_http_error_is_unavailable_not_wait(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    d = JevDecider(transport=httpx.MockTransport(lambda r: httpx.Response(529, text="overloaded"))).decide(CTX)
    assert d.status == "unavailable" and "529" in d.reason
