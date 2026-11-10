"""模型选型探测：对候选模型发同样的“图像 + JSON 输出”请求，记录成功率、时延与输出。

用法：
  .venv\\Scripts\\python tools/probe_models.py --provider openai --models gpt-5.6-luna gpt-6-luna --image data/... --n 2
  .venv\\Scripts\\python tools/probe_models.py --provider deepseek --models deepseek-flash --text-only
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pa.llm import ChatJSON, image_part  # noqa: E402
from pa.runtime.config import load_config, load_env  # noqa: E402
from pa.runtime.factory import provider_cfg  # noqa: E402

VISION_SYS = "你是视觉检查器。只输出 JSON。"
VISION_USER = ('看这张办公室画面，输出 JSON：{"persons": <人数>, "desk_objects": [桌面上能看清的物品英文名, 最多6个], '
               '"activity": "画面中人物主要在做什么（英文短语）"}')
TEXT_SYS = ('You decide whether a proactive assistant should interrupt the user now. Reply JSON only: '
            '{"warranted": 0..1, "mode": "WAIT"|"ASK"|"REMIND", "reason": "..."}')
TEXT_USER = json.dumps({"need": {"kind": "possible_forgotten_item", "mode_cap": "REMIND",
                                 "fire_reason": "person_1.activity: packing → moving_to_exit"},
                        "beliefs": [{"key": "phone_1.location", "value": "ON desk_1", "source": "seen", "status": "holds", "age_s": 0.4},
                                    {"key": "person_1.activity", "value": "moving_to_exit", "source": "seen", "status": "holds"}]},
                       ensure_ascii=False)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    load_env(Path(__file__).resolve().parents[1] / ".env")
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="openai")
    ap.add_argument("--models", nargs="+", required=True)
    ap.add_argument("--image", default=None)
    ap.add_argument("--text-only", action="store_true")
    ap.add_argument("--n", type=int, default=2)
    ap.add_argument("--json-mode", default=None, help="json_object | none（覆盖 provider 配置）")
    ap.add_argument("--max-tokens", type=int, default=400)
    args = ap.parse_args()
    cfg = load_config(None)
    content = None
    if not args.text_only:
        import av
        import numpy as np
        from pa.source import VideoSource
        img = next(VideoSource(args.image, 5, 448, start_s=60, duration_s=1).frames()).img
        content = [{"type": "text", "text": VISION_USER}, image_part(np.ascontiguousarray(img))]
    for m in args.models:
        pc = provider_cfg(cfg, args.provider, m, 60, {"max_completion_tokens": args.max_tokens})
        if args.json_mode:
            pc.json_mode = args.json_mode
        chat = ChatJSON(pc)
        lat, ok, last = [], 0, None
        for _ in range(args.n):
            data, text, ms, err = chat.complete(TEXT_SYS if args.text_only else VISION_SYS,
                                                TEXT_USER if args.text_only else content)
            lat.append(ms)
            ok += err is None
            last = data if err is None else f"ERR {err} | {text[:120]!r}"
        print(f"{m:22s} ok {ok}/{args.n}  latency median {statistics.median(lat):6.0f} ms  max {max(lat):6.0f} ms")
        print(f"    → {json.dumps(last, ensure_ascii=False)[:220] if not isinstance(last, str) else last}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
