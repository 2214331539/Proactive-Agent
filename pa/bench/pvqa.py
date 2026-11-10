"""ProactiveVideoQA（github.com/yellow-binary-tree/ProactiveVideoQA，HF wangyueqian/ProactiveVideoQA）输出格式转换。

官方评测读取 MMDuet 风格的 pred.jsonl：{"question_id": ..., "model_response_list": [{"time": 秒, "content": "..."}]}，
再用 pauc/judge_llm_scores_openai.py（GPT-4.1 Batch API）打分，得到 PAUC。
注意：该基准的问题由用户在 t=0 提出、是开放式的；要用本系统回答，需要“用户问题 → 观察型需求（query need）”适配，
本仓库尚未实现（见 README 的里程碑）。这里先提供格式导出，便于对接。
"""
from __future__ import annotations

import json
from pathlib import Path


def export(question_id: str, summary: dict, out_path: str | Path, append: bool = True) -> None:
    rows = [{"time": round(d["t_first_visible_ms"] / 1000, 2), "content": d["text"]} for d in summary["deliveries"]]
    with open(out_path, "a" if append else "w", encoding="utf-8") as f:
        f.write(json.dumps({"question_id": question_id, "model_response_list": rows}, ensure_ascii=False) + "\n")
