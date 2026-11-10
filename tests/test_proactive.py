"""主动决策核心（机会 → 价值 → 门控）的离线测试：合成剧本 + 脚本化 VLM + 模板草稿，不调任何模型。"""
from pathlib import Path

import pytest
import yaml

from pa.opportunity.model import Opportunity
from pa.runtime.config import load_config
from pa.runtime.engine import Engine
from pa.runtime.factory import build_decider, build_drafter, build_pa_parts, build_vlm
from pa.source import SyntheticSource
from pa.state.goals import GoalFilter
from pa.state.receptivity import Situation
from pa.value import ValueEstimator

SCN = Path(__file__).resolve().parents[1] / "scenarios"


def run(name, tmp_path):
    sc = yaml.safe_load((SCN / f"{name}.yaml").read_text(encoding="utf-8"))
    cfg = load_config(None, sc.get("config"))
    assert cfg["proactive"]["enabled"]
    src = SyntheticSource(sc["duration_s"], sc.get("changes", []), fps=cfg["source"]["sample_fps"])
    eng = Engine(cfg, src, build_vlm(cfg, sc), build_decider(cfg), build_drafter(cfg), session_id=name,
                 run_dir=tmp_path / name, quiet=True, pa_parts=build_pa_parts(cfg, "sim"))
    return eng.run(), eng


def test_forgot_phone_reminds_before_leaving(tmp_path):
    s, eng = run("forgot_phone", tmp_path)
    dl = [d for d in s["deliveries"] if d["mode"] != "AMBIENT"]
    assert len(dl) == 1, [(d["t_ms"], d["text"]) for d in s["deliveries"]]
    d = dl[0]
    assert d["opp_key"].startswith("forget:phone_1") and d["mode"] == "REMIND"
    assert 11000 <= d["t_ms"] <= 15000
    assert d["est"]["U"]["REMIND"] > d["est"]["V"] > -1
    ops = {o["key"]: o for o in s["proactive"]["opportunities"]}
    assert ops[d["opp_key"]]["status"] == "resolved"  # 15 s 取回手机后关闭


@pytest.mark.parametrize("name", ["false_leave", "phone_into_bag"])
def test_no_reminder_when_not_needed(name, tmp_path):
    s, _ = run(name, tmp_path)
    assert [d for d in s["deliveries"] if d["mode"] != "AMBIENT"] == []


def test_decision_log_records_silence(tmp_path):
    s, eng = run("forgot_phone", tmp_path)
    import sqlite3
    conn = sqlite3.connect(tmp_path / "forgot_phone" / "log.sqlite")
    decs = [r[0] for r in conn.execute("SELECT decision FROM value_decisions ORDER BY t_ms")]
    assert "REMIND" in decs and decs[0] in ("SILENT", "WAIT")  # 先沉默 / 等待，再提醒


def _est(p_leave_evidence, talking=False, hurried=False):
    g = GoalFilter()
    for i, (lab, w, st) in enumerate(p_leave_evidence):
        g._add("Sophie", 1000 * i, w, st, lab, kind="speech" if lab.startswith("说") else "act")
    g.update(1000 * len(p_leave_evidence))
    opp = Opportunity(key="request:umbrella", source="request", addressee=["Sophie"], problem="带伞",
                      deadline="goal:leave_home", factors={"出门时真会忘/需要": 0.6})
    sit = Situation("x", talking, hurried, False, "talking" if talking else "normal")
    return ValueEstimator().estimate(opp, goals=g, agenda=None, situation=sit, uv=None, now_ms=1000 * len(p_leave_evidence))


def test_lone_activity_label_is_not_heading_out():
    """第一人称视频里一帧 leaving 标签 + walking 不足以判定“走向门口”（需要话语 / 拿随身物品等跨来源佐证）。"""
    g = GoalFilter()
    g._add("p3", 0, 0.4, 1, "activity=walking")
    g._add("p3", 1000, 2.6, 2, "activity=leaving")
    g.update(2000)
    assert g.people["p3"].stage == 1
    g._add("p3", 2500, 0.8, 1, "拿起 bag", kind="carry")
    g.update(3000)
    assert g.people["p3"].stage == 2


def test_value_rule_silent_wait_speak():
    early = _est([])  # 坐着聊天
    assert early.decision == "SILENT"
    prep = _est([("standing", 0.35, 1), ("说“let's go get ready”", 1.4, 1)], talking=True)
    assert prep.decision in ("WAIT", "SILENT") and prep.v_wait > 0
    door = _est([("standing", 0.35, 1), ("说“let's go”", 1.4, 1), ("activity=moving_to_exit", 2.2, 2)], hurried=True)
    assert door.decision == "REMIND", door.public()
    assert door.H >= 0.85 and door.u_best > door.v_wait


def test_timed_request_waits_until_due():
    """“robot, remind me in three minutes …”：到点前再等等，到点提醒；没人会替助理提醒，q 很低。"""
    g = GoalFilter()
    due = 180_000
    opp = Opportunity(key="request:noodles", source="request", addressee=["Jack"], problem="三分钟后提醒 Jack 泡面好了",
                      deadline=f"time:{due}", factors={"请求明确": 0.95}, meta={"to_assistant": True, "said_ms": 0})
    sit = Situation("空闲", False, False, False, "normal")
    est = ValueEstimator()
    before = est.estimate(opp, goals=g, agenda=None, situation=sit, uv=None, now_ms=60_000)
    assert before.decision == "WAIT" and before.q <= 0.15
    assert est.estimate(opp, goals=g, agenda=None, situation=sit, uv=None, now_ms=due - 5_000).decision == "WAIT"
    at = est.estimate(opp, goals=g, agenda=None, situation=sit, uv=None, now_ms=due)
    assert at.decision == "REMIND", at.public()


def test_consecutive_requests_merge():
    """同一件事被连续几句话说出来，只生成一个需求；后一句带来的相对时间并入前一个。"""
    from pa.agenda.trigger import TriggerEngine
    from pa.schemas import Entity
    from pa.state.store import StateStore

    store = StateStore()
    store.entities["person_1"] = Entity(id="person_1", type="PERSON", cls="person")
    ag = TriggerEngine([])
    base = {"kind": "user_request", "bindings": {"p1": "person_1"}, "arm": {}, "fire": {}, "resolve": {}}
    ag.upsert_need({**base, "topic": "client meeting", "said_ms": 10_000}, store, 10_000, "c1")
    ag.upsert_need({**base, "topic": "meeting", "said_ms": 12_000}, store, 12_000, "c2")
    ag.upsert_need({**base, "topic": "meeting reminder", "said_ms": 30_000, "due_ms": 3_630_000}, store, 30_000, "c3")
    needs = ag.active_needs()
    assert len(needs) == 1 and needs[0].meta["due_ms"] == 3_630_000
    ag.upsert_need({**base, "topic": "umbrella", "said_ms": 40_000}, store, 40_000, "c4")
    assert len(ag.active_needs()) == 2


def test_explicit_request_replaces_vague_one():
    from pa.agenda.trigger import TriggerEngine
    from pa.schemas import Entity
    from pa.state.store import StateStore

    store = StateStore()
    store.entities["person_1"] = Entity(id="person_1", type="PERSON", cls="person")
    ag = TriggerEngine([])
    base = {"kind": "user_request", "bindings": {"p1": "person_1"}, "arm": {}, "fire": {}, "resolve": {}, "addressee": ["Jack"]}
    ag.upsert_need({**base, "topic": "wait_for_three_minutes", "summary": "三分钟后提醒 Jack（Jack 说 Wait for three minutes.）",
                    "source_utt": "Wait for three minutes.", "said_ms": 0, "due_ms": 180_000}, store, 0, "c1")
    ag.upsert_need({**base, "topic": "instant noodles", "summary": "三分钟后提醒 Jack 方便面好了",
                    "source_utt": "Hey robot remind me in three minutes that the instant noodles is ready",
                    "said_ms": 2_000, "due_ms": 182_000}, store, 2_000, "c2")
    (n,) = ag.active_needs()
    assert "方便面" in n.meta["summary"] and n.meta["due_ms"] == 180_000
    ag.upsert_need({**base, "topic": "umbrella", "said_ms": 300_000}, store, 300_000, "c3")
    ag.upsert_need({**base, "topic": "umbrellas", "said_ms": 600_000}, store, 600_000, "c4")
    assert len(ag.active_needs()) == 2
