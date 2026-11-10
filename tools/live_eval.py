"""实时流验收：把一段视频用 ffmpeg 实时推流，系统在线处理，统计主动消息是否在截止时刻前发出。

例（living_room_06 雨伞，13:30 起推流 90 s，Jack 在 14:52 = 流内 82 s 自己发现忘带伞）：
  .venv\Scripts\python tools/live_eval.py --video data/m3bench/videos/robot/living_room_06.mp4 \
      --srt data/m3bench/subtitles/robot/living_room_06.srt --start 810 --duration 90 \
      --keyword 伞 --keyword umbrella --deadline 82 --runs 3 --config config/deepseek.yaml
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--srt")
    ap.add_argument("--start", type=float, required=True)
    ap.add_argument("--duration", type=float, required=True)
    ap.add_argument("--keyword", action="append", required=True, help="命中目标的关键词（任一）")
    ap.add_argument("--deadline", type=float, required=True, help="流内秒数：在此之前发出才算及时")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--config", default="config/deepseek.yaml")
    ap.add_argument("--port", type=int, default=23100)
    args = ap.parse_args()
    rows = []
    for i in range(args.runs):
        port = args.port + i
        ff = subprocess.Popen(["ffmpeg", "-loglevel", "error", "-re", "-ss", str(args.start), "-t", str(args.duration),
                               "-i", args.video, "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-an",
                               "-f", "mpegts", "-listen", "1", f"http://127.0.0.1:{port}"])
        time.sleep(1.0)
        before = set(p.name for p in (ROOT / "runs").glob("live_*"))
        cmd = [sys.executable, "-m", "pa", "run", "--video", f"http://127.0.0.1:{port}", "--egocentric",
               "--config", args.config, "--out", "runs", "--quiet"]
        if args.srt:
            cmd += ["--srt", args.srt, "--srt-offset", str(args.start)]
        subprocess.run(cmd, cwd=ROOT, capture_output=True, timeout=args.duration * 4 + 120)
        ff.kill()
        run_dir = sorted(set(p.name for p in (ROOT / "runs").glob("live_*")) - before)[-1]
        s = json.loads((ROOT / "runs" / run_dir / "summary.json").read_text(encoding="utf-8"))
        hits = [d for d in s["deliveries"] if any(k in d["text"] for k in args.keyword)]
        others = [d for d in s["deliveries"] if d not in hits]
        first = min((d["t_first_visible_ms"] / 1000 for d in hits), default=None)
        timely = first is not None and first <= args.deadline
        row = {"run": i + 1, "dir": run_dir, "hit_s": first, "timely": timely,
               "lead_s": round(args.deadline - first, 1) if first is not None else None,
               "hit_text": hits[0]["text"] if hits else None, "other_deliveries": [d["text"] for d in others],
               "decisions": len(s["decisions"]), "blocked": [b["reason"] for b in s["blocked"]],
               "vlm_p50_ms": s["vlm"]["p50_ms"], "vlm_p95_ms": s["vlm"]["p95_ms"]}
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    n = len(rows)
    print(f"\n及时命中 {sum(r['timely'] for r in rows)}/{n}；命中但晚 {sum(r['hit_s'] is not None and not r['timely'] for r in rows)}/{n}；"
          f"其他（可能误报）消息 {sum(len(r['other_deliveries']) for r in rows)} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
