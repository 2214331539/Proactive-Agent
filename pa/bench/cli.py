"""pa bench 子命令：

  pa bench m3-list                       # 列出 robot 视频、QA 数与“状态类”问题数
  pa bench m3-download office_05         # 下载视频 + 字幕（HF；国内可加 --endpoint https://hf-mirror.com）
  pa bench m3-probes office_05 --set perception.provider=openai --set perception.model=... \
        --answer openai:gpt-5-mini --judge openai:gpt-4o-2024-11-20
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import m3bench


def _llm(cfg: dict, spec: str | None):
    if not spec:
        return None
    from ..llm import ChatJSON
    from ..runtime.factory import provider_cfg

    prov, model = spec.split(":", 1)
    return ChatJSON(provider_cfg(cfg, prov, model, 60))


def cmd_list(args) -> int:
    rows = m3bench.stats(args.subset)
    print(f"{'video':18s} {'QA':>4s} {'state':>6s} {'最后提问(s)':>10s}")
    for r in rows:
        print(f"{r['video']:18s} {r['qa']:4d} {r['state_qa']:6d} {r['last_q_s']:10d}")
    print(f"共 {len(rows)} 段，QA {sum(r['qa'] for r in rows)}，状态类 {sum(r['state_qa'] for r in rows)}")
    return 0


def cmd_download(args) -> int:
    m3bench.download(args.videos, args.out, args.endpoint, method=args.method, proxy=args.proxy,
                     subtitles_only=args.subs_only)
    return 0


def cmd_probes(args) -> int:
    from ..runtime.config import load_config, set_path
    from ..runtime.engine import Engine
    from ..runtime.factory import build_decider, build_drafter, build_slow_llm, build_vlm
    from ..source import VideoSource
    from ..speech import SrtUtterances

    cfg = load_config(args.config)
    for s in args.set:
        k, v = s.split("=", 1)
        set_path(cfg, k, v)
    cfg["session"]["camera_mode"] = "egocentric"
    ann = m3bench.load("robot")[args.video]
    qas = m3bench.select_probes(ann["qa_list"], only_state=not args.all_questions)
    dur_ms = None if args.duration is None else int(args.duration * 1000)
    probes = []
    for qa in qas:
        t = m3bench.probe_time_ms(qa)
        if dur_ms is None or t <= dur_ms:
            probes.append((t, qa["question_id"]))
    by_id = {qa["question_id"]: qa for qa in qas}
    print(f"{args.video}: 选中 {len(probes)} 个探针（状态类={not args.all_questions}，截止 {args.duration or '全片'} s）")
    if not probes:
        return 0
    data_dir = Path(args.data_dir)
    video = data_dir / "videos" / "robot" / f"{args.video}.mp4"
    srt = data_dir / "subtitles" / "robot" / f"{args.video}.srt"
    if not video.exists():
        print(f"缺少视频 {video}，先运行：pa bench m3-download {args.video}")
        return 1
    src = VideoSource(str(video), cfg["source"]["sample_fps"], cfg["source"]["short_side"], 0.0, args.duration)
    utts = SrtUtterances(srt, latency_ms=cfg["speech"]["srt_latency_ms"]).all(dur_ms) if srt.exists() else None
    records: list[dict] = []

    def on_probe(pid, store, episodes, t):
        qa = by_id[pid]
        records.append({"question_id": pid, "question": qa["question"], "answer": qa["answer"], "type": qa["type"],
                        "timestamp": qa["timestamp"], "probe_t_s": t / 1000, "state": store.describe(),
                        "episodes": [e["text"] for e in episodes][-20:]})

    run_dir = Path(args.out) / f"m3probe_{args.video}_{time.strftime('%Y%m%d_%H%M%S')}"
    eng = Engine(cfg, src, build_vlm(cfg), build_decider(cfg), build_drafter(cfg), session_id=args.video,
                 run_dir=run_dir, mode=args.mode, utterances=utts, slow_llm=build_slow_llm(cfg), probes=probes,
                 on_probe=on_probe, quiet=args.quiet, max_ms=dur_ms)
    eng.run()
    results = m3bench.answer_and_judge(records, _llm(cfg, args.answer), _llm(cfg, args.judge))
    out = run_dir / "probes.jsonl"
    with open(out, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    judged = [r for r in results if "correct" in r]
    if judged:
        acc = sum(r["correct"] for r in judged) / len(judged)
        print(f"状态探针准确率 {acc:.3f}（{sum(r['correct'] for r in judged)}/{len(judged)}）")
    else:
        print("未配置 --answer/--judge：只导出了每个探针时刻的状态快照，可人工检查。")
    print(f"结果：{out}")
    return 0


def cmd_scan(args) -> int:
    from .m3bench import scan_proactive

    for r in scan_proactive()[:args.top]:
        print(f"{r['video']:<18} {r['duration_s'] // 60:>3} 分钟  线索分 {r['score']:>4}  {r['counts']}")
        for t, k, x in r["examples"][:4]:
            print(f"      {t // 60:02d}:{t % 60:02d} {k:<8} {x}")
    return 0


def register(sub) -> None:
    b = sub.add_parser("bench", help="数据集 / 基准工具")
    bs = b.add_subparsers(dest="bench_cmd", required=True)
    p = bs.add_parser("m3-list")
    p.add_argument("--subset", default="robot")
    p.set_defaults(fn=cmd_list)
    p = bs.add_parser("m3-download")
    p.add_argument("videos", nargs="+")
    p.add_argument("--out", default="data/m3bench")
    p.add_argument("--endpoint", default=None, help="如 https://hf-mirror.com（默认读 HF_ENDPOINT）")
    p.add_argument("--method", choices=["http", "hub"], default="http", help="http=断点续传直连（推荐）")
    p.add_argument("--proxy", default=None, help="如 http://127.0.0.1:7890")
    p.add_argument("--subs-only", action="store_true", help="只下载字幕（几十 KB，用于 m3-scan 挑视频）")
    p.set_defaults(fn=cmd_download)
    p = bs.add_parser("m3-scan", help="按字幕里的主动性线索给 robot 视频排序（先下载字幕：m3-download --subs-only）")
    p.add_argument("--top", type=int, default=15)
    p.set_defaults(fn=cmd_scan)
    p = bs.add_parser("m3-probes")
    p.add_argument("video")
    p.add_argument("--duration", type=float, default=None, help="只处理前 N 秒（探针也只取这段内的）")
    p.add_argument("--all-questions", action="store_true", help="不只选状态类问题")
    p.add_argument("--answer", default=None, help="作答模型 provider:model")
    p.add_argument("--judge", default=None, help="判分模型 provider:model（M3-Agent 用 gpt-4o-2024-11-20）")
    p.add_argument("--data-dir", default="data/m3bench")
    p.add_argument("--config", default=None)
    p.add_argument("--set", action="append", default=[])
    p.add_argument("--out", default="runs")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--mode", choices=["sim", "realtime"], default="sim",
                   help="慢速 API（单次 >5 s）下 realtime 更快：调用并行、按 1× 墙钟推进")
    p.set_defaults(fn=cmd_probes)
