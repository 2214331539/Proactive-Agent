"""运行模式：realtime（线程池 + 墙钟）与 sim 结果一致；真实模型不可用时感知降级、不崩溃。"""
from pathlib import Path

import yaml

from pa.runtime.config import load_config

LEGACY = {"proactive": {"enabled": False}}
from pa.runtime.engine import Engine
from pa.runtime.factory import build_decider, build_drafter, build_vlm
from pa.source import SyntheticSource

SCN = Path(__file__).resolve().parents[1] / "scenarios"


def test_realtime_mode_matches_sim_outcome(tmp_path):
    sc = yaml.safe_load((SCN / "forgot_phone.yaml").read_text(encoding="utf-8"))
    cfg = load_config(None, LEGACY)
    src = SyntheticSource(sc["duration_s"], sc["changes"], fps=5)
    eng = Engine(cfg, src, build_vlm(cfg, sc), build_decider(cfg), build_drafter(cfg), session_id="rt",
                 run_dir=tmp_path, mode="realtime", speed=8.0, scripted_feedback=sc["feedback"], quiet=True)
    s = eng.run()
    assert len(s["deliveries"]) == 1 and s["deliveries"][0]["mode"] == "REMIND"
    assert 11000 < s["deliveries"][0]["t_first_visible_ms"] < 16000


def test_unreachable_model_degrades_gracefully(tmp_path):
    cfg = load_config(None, LEGACY)
    cfg["providers"]["dead"] = {"base_url": "http://127.0.0.1:9/v1", "api_key_env": "NOPE"}
    cfg["perception"].update(provider="dead", model="x", timeout_s=0.5, cache=False)
    src = SyntheticSource(6, [{"t_ms": 2000, "box": [0, 0, 224, 126], "color": [255, 255, 255]}], fps=5)
    eng = Engine(cfg, src, build_vlm(cfg), build_decider(cfg), build_drafter(cfg), session_id="dead",
                 run_dir=tmp_path, quiet=True)
    s = eng.run()
    assert s["vlm"]["calls"] >= 2 and s["vlm"]["failed"] == s["vlm"]["calls"]
    assert s["state_version"] == 0 and not s["deliveries"]
