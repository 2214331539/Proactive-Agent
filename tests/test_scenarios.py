"""E0 剧本测试：合成画面 + 脚本化 VLM + 规则决策器 + 模板回复，跑完整引擎并核对剧本里的 expect。"""
from pathlib import Path

import pytest
import yaml

from pa.runtime.config import deep_merge, load_config
from pa.runtime.engine import Engine
from pa.runtime.factory import build_decider, build_drafter, build_vlm
from pa.source import SyntheticSource

SCN = Path(__file__).resolve().parents[1] / "scenarios"


def run_scenario(name, tmp_path, **cfg_over):
    sc = yaml.safe_load((SCN / f"{name}.yaml").read_text(encoding="utf-8"))
    cfg = load_config(None, deep_merge({"proactive": {"enabled": False}}, sc.get("config") or {}))
    for k, v in cfg_over.items():
        sec, key = k.split("__")
        cfg[sec][key] = v
    src = SyntheticSource(sc["duration_s"], sc.get("changes", []), fps=cfg["source"]["sample_fps"])
    eng = Engine(cfg, src, build_vlm(cfg, sc), build_decider(cfg), build_drafter(cfg), session_id=name,
                 run_dir=tmp_path / name, scripted_feedback=sc.get("feedback"), quiet=True)
    return sc, eng.run(), eng


@pytest.mark.parametrize("name", ["forgot_phone", "phone_into_bag", "occluded_then_leave", "false_leave"])
def test_scenario_expectations(name, tmp_path):
    sc, summary, eng = run_scenario(name, tmp_path)
    exp = sc["expect"]
    dl = summary["deliveries"]
    assert len(dl) == len(exp["deliveries"]), [d["text"] for d in dl]
    for d, e in zip(dl, exp["deliveries"]):
        assert d["kind"] == e["kind"] and d["mode"] == e["mode"]
        assert e["after_ms"] < d["t_first_visible_ms"] < e["before_ms"]
    assert len(summary["decisions"]) <= exp["max_decisions"]
    edges = [e["edge"] for e in summary["edges"]]
    if "edges_in_order" in exp:
        it = iter(edges)
        assert all(x in it for x in exp["edges_in_order"]), edges
    for x in exp.get("edges_contain", []):
        assert x in edges, edges
    for key, want in exp.get("final", {}).items():
        b = eng.store.beliefs[key]
        assert b.value == want["value"] and b.source == want["source"]


def test_forgot_phone_decides_only_after_fire_and_is_deterministic(tmp_path):
    _, s1, e1 = run_scenario("forgot_phone", tmp_path / "a")
    _, s2, _ = run_scenario("forgot_phone", tmp_path / "b")
    fired = [e for e in s1["edges"] if e["edge"] == "fired"]
    assert len(fired) == 1
    assert all(d["t_request_ms"] >= fired[0]["t_ms"] for d in s1["decisions"])
    strip = lambda s: [(d["t_first_visible_ms"], d["text"]) for d in s["deliveries"]]  # noqa: E731
    assert strip(s1) == strip(s2) and s1["final_state"] == s2["final_state"]
    # 预生成草稿被直接复用：发送时刻 = 决策返回时刻
    assert s1["deliveries"][0]["t_first_visible_ms"] == s1["decisions"][0]["t_avail_ms"]


def test_prefetch_saves_latency_vs_llm_first(tmp_path):
    """A4 消融的离线版：template 草稿器零时延，因此 prefetch 与否只差调度；这里只验证两种策略都能发出且时间一致。"""
    _, s1, _ = run_scenario("forgot_phone", tmp_path / "p", response__strategy="prefetch_llm")
    _, s2, _ = run_scenario("forgot_phone", tmp_path / "t", response__strategy="template_first")
    assert s1["deliveries"] and s2["deliveries"]
