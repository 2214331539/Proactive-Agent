"""命令行入口：

  pa scenario scenarios/forgot_phone.yaml            # 离线剧本（脚本化 VLM），E0 逻辑验证
  pa run --video x.mp4 --set perception.provider=openai --set perception.model=gpt-5-mini
  pa bench ...                                       # 数据集工具，见 pa/bench/cli.py
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import yaml

from .config import load_config, load_env, set_path
from .engine import Engine
from .factory import build_decider, build_drafter, build_pa_parts, build_slow_llm, build_vlm


def _apply_sets(cfg: dict, sets: list[str]) -> None:
    for s in sets or []:
        k, v = s.split("=", 1)
        set_path(cfg, k, v)


def _print_summary(summary: dict, run_dir: Path) -> None:
    v = summary["vlm"]
    print("\n==== 运行结束 ====")
    print(f"状态版本 v{summary['state_version']}，实体 {summary['n_entities']} 个；VLM 调用 {v['calls']} 次"
          f"（失败 {v['failed']}，缓存命中 {v['cached']}，P50 {v['p50_ms']} ms，P95 {v['p95_ms']} ms）")
    print(f"决策调用 {len(summary['decisions'])} 次，发出 {len(summary['deliveries'])} 条，被拦截 {len(summary['blocked'])} 条")
    for d in summary["deliveries"]:
        if "t_ms" in d:  # 主动决策核心
            e = d.get("est") or {}
            print(f"  {d['t_ms'] / 1000:7.1f}s [{d['mode']}] {d['text']}  ← {d['problem']}（{e.get('reason', '')}）")
        else:
            print(f"  {d['t_first_visible_ms'] / 1000:7.1f}s [{d['mode']}] {d['text']}  (fired@{d['fire_t_ms'] / 1000:.1f}s)")
    print("最终状态：")
    for line in summary["final_state"]:
        print("  " + line)
    print(f"产物：{run_dir}（log.sqlite / summary.json / timeline.jsonl）")


def cmd_scenario(args) -> int:
    from ..source import SyntheticSource

    sc = yaml.safe_load(Path(args.scenario).read_text(encoding="utf-8"))
    cfg = load_config(args.config, sc.get("config"))
    _apply_sets(cfg, args.set)
    run_dir = Path(args.out) / f"{sc.get('name', 'scenario')}_{time.strftime('%Y%m%d_%H%M%S')}"
    src = SyntheticSource(sc["duration_s"], sc.get("changes", []), fps=cfg["source"]["sample_fps"])
    eng = Engine(cfg, src, build_vlm(cfg, sc), build_decider(cfg), build_drafter(cfg), session_id=sc.get("name", "scn"),
                 run_dir=run_dir, mode=args.mode, scripted_feedback=sc.get("feedback"), slow_llm=build_slow_llm(cfg),
                 pa_parts=build_pa_parts(cfg, args.mode),
                 speed=args.speed, quiet=args.quiet)
    summary = eng.run()
    _print_summary(summary, run_dir)
    return 0


def cmd_run(args) -> int:
    from ..source import VideoSource
    from ..source.live import LiveSource, is_live_url
    from ..speech import SrtUtterances

    cfg = load_config(args.config)
    _apply_sets(cfg, args.set)
    if args.egocentric:
        cfg["session"]["camera_mode"] = "egocentric"
    live = args.live or is_live_url(args.video)
    if live:
        args.mode = "realtime"
        src = LiveSource(args.video, cfg["source"]["sample_fps"], cfg["source"]["short_side"])
        print(f"实时流：{args.video}（帧时间戳 = 到达墙钟；realtime 模式）")
    else:
        src = VideoSource(args.video, cfg["source"]["sample_fps"], cfg["source"]["short_side"], args.start, args.duration)
        info = src.info()
        print(f"视频：{args.video} 时长 {info['duration_s']}s {info['width']}x{info['height']} 音频={info['has_audio']}")
    utts, asr, faces = None, None, None
    sp = cfg["speech"]
    if args.srt:
        offset = args.srt_offset if args.srt_offset is not None else args.start
        utts = SrtUtterances(args.srt, offset_s=offset, latency_ms=sp["srt_latency_ms"]).all(
            None if args.duration is None else int(args.duration * 1000))
        print(f"字幕语句 {len(utts)} 条（作为理想 ASR，可用时刻 = 结束 + {sp['srt_latency_ms']} ms）")
    elif sp["provider"] == "whisper":
        from ..speech import WhisperUtterances
        asr = WhisperUtterances(sp["whisper_model"])
    if cfg["faces"]["enabled"]:
        from ..speech.faces import FaceAnchors
        faces = FaceAnchors()
    name = "live" if live else Path(args.video).stem
    run_dir = Path(args.out) / f"{name}_{int(args.start)}s_{time.strftime('%Y%m%d_%H%M%S')}"
    if not live:  # 供界面回放（带视频与标注竖线）与 pa eval 使用
        import json
        from ..ui.presets import annotation_markers
        run_dir.mkdir(parents=True, exist_ok=True)
        dur = args.duration or src.info()["duration_s"] - args.start
        (run_dir / "meta.json").write_text(json.dumps({
            "id": f"cli:{name}", "title": f"{name} · {int(args.start) // 60}:{int(args.start) % 60:02d} 起 {dur / 60:.1f} 分钟（命令行）",
            "video_id": name, "start": args.start, "duration": dur, "markers": annotation_markers(name, args.start),
            "config": args.config, "proactive": bool(cfg.get("proactive", {}).get("enabled")), "mode": args.mode},
            ensure_ascii=False, indent=2), encoding="utf-8")
    eng = Engine(cfg, src, build_vlm(cfg), build_decider(cfg), build_drafter(cfg), session_id=name,
                 run_dir=run_dir, mode=args.mode, utterances=utts, asr=asr, faces=faces, slow_llm=build_slow_llm(cfg),
                 pa_parts=build_pa_parts(cfg, args.mode),
                 speed=args.speed, quiet=args.quiet)
    summary = eng.run()
    _print_summary(summary, run_dir)
    return 0


def cmd_eval(args) -> int:
    import json

    from ..eval import evaluate

    r = evaluate(args.run_dir, args.ann)
    h = r["human"]
    print(f"{r['title'] or r['run']} · {r['duration_min']} 分钟")
    print(f"  主动消息 {h['n_deliveries']} 条（语音 {h['n_voice']}，提示音 {h['n_ambient']}），语音 {h['per_hour']} 条/小时；"
          f"来源 {r['by_source']}")
    print(f"  人工核验 {h['judged']}/{h['n_deliveries']}：{h['counts']}；有用率 {h['precision']}；"
          f"误报 {h['false_alarm_per_hour']} 次/小时；NPV {h['npv']} / 完美 {h['npv_oracle']} → PG_human {h['pg_human']}")
    if "annotated" in r:
        a = r["annotated"]
        print(f"  标注窗口：PG {a['pg']}（NPV {a['npv']} / {a['npv_oracle']}），及时命中率 {a['recall']}，FAH {a['fah']}，"
              f"提前量 {a['lead_time_s']} s")
        for o in a["opportunities"]:
            print(f"    - {o['id']} [{o['label']}] 窗口 {o['window']} → {'命中 ' + str(o['t_video']) + ' s' if o['hit'] else '未命中'}")
    out = Path(args.run_dir) / "eval.json"
    out.write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  详细结果：{out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # Windows 控制台中文 / emoji
    except Exception:  # noqa: BLE001
        pass
    load_env()
    ap = argparse.ArgumentParser(prog="pa", description="Proactive Video Agent V2")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--config", default=None, help="额外 YAML 配置，与 pa/config/default.yaml 合并")
        p.add_argument("--set", action="append", default=[], help="覆盖配置，如 perception.provider=openai")
        p.add_argument("--mode", choices=["sim", "realtime"], default="sim")
        p.add_argument("--speed", type=float, default=1.0, help="realtime 模式的播放倍速")
        p.add_argument("--out", default="runs")
        p.add_argument("--quiet", action="store_true")

    p = sub.add_parser("scenario", help="运行离线剧本（合成画面 + 脚本化 VLM）")
    p.add_argument("scenario")
    common(p)
    p.set_defaults(fn=cmd_scenario)

    p = sub.add_parser("run", help="在真实视频上运行")
    p.add_argument("--video", required=True)
    p.add_argument("--start", type=float, default=0.0)
    p.add_argument("--duration", type=float, default=None)
    p.add_argument("--srt", default=None, help="字幕文件（M3-Bench robot 自带），作为理想 ASR")
    p.add_argument("--egocentric", action="store_true", help="第一人称移动视角（M3-Bench robot）")
    p.add_argument("--live", action="store_true", help="把 --video 当实时流读（udp/rtsp/http 地址会自动识别）")
    p.add_argument("--srt-offset", type=float, default=None, help="字幕时间轴偏移（秒）；实时推流从第 N 秒开始时填 N")
    common(p)
    p.set_defaults(fn=cmd_run)

    from ..bench.cli import register as register_bench
    register_bench(sub)

    p = sub.add_parser("eval", help="主动性结算：人工核验（verdicts.jsonl）+ 标注窗口（data/annotations/<video>.json）")
    p.add_argument("run_dir")
    p.add_argument("--ann", default=None, help="标注文件；默认按 meta.json 的 video_id 查 data/annotations/")
    p.set_defaults(fn=cmd_eval)

    p = sub.add_parser("ui", help="可视化界面：实时运行 / 回放（浏览器打开 http://127.0.0.1:8765）")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.set_defaults(fn=lambda a: __import__("pa.ui.server", fromlist=["serve"]).serve(a.host, a.port) or 0)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
