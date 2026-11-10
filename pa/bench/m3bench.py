"""M3-Bench（ByteDance-Seed/M3-Bench，CC BY-NC-SA 4.0）接入：下载、选题、状态探针评测。

探针时刻与 M3-Agent 的信息截止对齐：probe_t = (before_clip + 1) × 30 s，即提问前最后一个完整 30 秒片段的末尾
（M3-Agent 的检索只用 clip ≤ before_clip 的记忆）。判分沿用 M3-Agent control.py 实际使用的
prompt_agent_verify_answer_referencing（GPT-4o，"yes" in 回复 视为正确）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

REPO = "ByteDance-Seed/M3-Bench"
ANN_DIR = Path("data/m3bench/annotations")

# 原文取自 m3-agent/mmagent/prompts.py:992（Apache-2.0）
JUDGE_PROMPT = """You are provided with a question, a ground truth answer, and an answer from an agent model. Your task is to determine whether the ground truth answer can be logically inferred from the agent's answer, in the context of the question.

Do not directly compare the surface forms of the agent answer and the ground truth answer. Instead, assess whether the meaning expressed by the agent answer supports or implies the ground truth answer. If the ground truth can be reasonably derived from the agent answer, return "Yes". If it cannot, return "No".

Important notes:
\t•\tDo not require exact wording or matching structure.
\t•\tSemantic inference is sufficient, as long as the agent answer entails or implies the meaning of the ground truth answer, given the question.
\t•\tOnly return "Yes" or "No", with no additional explanation or formatting.

Input fields:
\t•\tquestion: the question asked
\t•\tground_truth_answer: the correct answer
\t•\tagent_answer: the model's answer to be evaluated

Now evaluate the following input:

Input:
\t•\tquestion: {question}
\t•\tground_truth_answer: {ground_truth_answer}
\t•\tagent_answer: {agent_answer}

Output ('Yes' or 'No'):"""

ANSWER_SYSTEM = ("你根据一个视频助理在“此刻”的结构化记忆回答问题。只能使用给出的 STATE（当前信念，含来源与状态）"
                 "和 EPISODES（历史情节）。status 为 stale/unknown 的信念不是确定事实。无法从记忆中回答时如实说不知道。"
                 "用英文简短作答，输出 JSON：{\"answer\": \"...\"}")

STATE_Q = re.compile(r"^\s*(where\b|which (room|place|drawer|shelf|table)\b|what is (on|in|inside|under)\b|"
                     r"who (is holding|has|took|picked)\b|what did .* (put|place|leave)\b)", re.I)


def parse_ts(ts: str) -> int:
    parts = [int(p) for p in ts.strip().split(":")]
    sec = parts[-1] + 60 * parts[-2] + (3600 * parts[-3] if len(parts) > 2 else 0)
    return sec * 1000


def probe_time_ms(qa: dict) -> int:
    return (int(qa["before_clip"]) + 1) * 30000


def load(subset: str = "robot") -> dict:
    return json.loads((ANN_DIR / f"{subset}.json").read_text(encoding="utf-8"))


def select_probes(qa_list: list[dict], only_state: bool = True) -> list[dict]:
    return [qa for qa in qa_list if not only_state or STATE_Q.search(qa["question"])]


def stats(subset: str = "robot") -> list[dict]:
    ann = load(subset)
    rows = []
    for vid, v in sorted(ann.items()):
        qas = v["qa_list"]
        last = max((parse_ts(q["timestamp"]) for q in qas if "timestamp" in q), default=0)
        rows.append({"video": vid, "qa": len(qas), "state_qa": len(select_probes(qas)), "last_q_s": last // 1000})
    return rows


def http_download(url: str, dest: Path, proxy: str | None = None, chunk: int = 1 << 20) -> Path:
    """普通 HTTP 断点续传下载（huggingface_hub 2.x 的 Xet 通道在部分网络下会卡住，这里绕开它）。"""
    import httpx

    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    have = part.stat().st_size if part.exists() else 0
    headers = {"Range": f"bytes={have}-"} if have else {}
    with httpx.stream("GET", url, headers=headers, follow_redirects=True, timeout=60, proxy=proxy) as r:
        if r.status_code == 416:  # 已经下完
            part.rename(dest)
            return dest
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0)) + have
        mode = "ab" if have and r.status_code == 206 else "wb"
        done = have if mode == "ab" else 0
        last = 0
        with open(part, mode) as f:
            for b in r.iter_bytes(chunk):
                f.write(b)
                done += len(b)
                if total and done - last > 50 * chunk:
                    print(f"    {dest.name}: {done / 1e6:.0f}/{total / 1e6:.0f} MB", flush=True)
                    last = done
    part.replace(dest)
    return dest


def download(video_ids: list[str], out_dir: str = "data/m3bench", endpoint: str | None = None,
             method: str = "http", proxy: str | None = None, subtitles_only: bool = False) -> list[Path]:
    """下载 robot 视频与字幕（单个视频 0.3–3.4 GB）。

    method=http：直接请求 {endpoint}/datasets/{REPO}/resolve/main/<file>，支持断点续传与代理（推荐）；
    method=hub：huggingface_hub.hf_hub_download。国内网络可用 endpoint=https://hf-mirror.com 或本地代理。
    """
    import os

    endpoint = (endpoint or os.environ.get("HF_ENDPOINT") or "https://huggingface.co").rstrip("/")
    got = []
    for vid in video_ids:
        files = [f"subtitles/robot/{vid}.srt"] if subtitles_only else [f"videos/robot/{vid}.mp4", f"subtitles/robot/{vid}.srt"]
        for fn in files:
            dest = Path(out_dir) / fn
            if dest.exists():
                print(f"  已存在：{dest}")
                got.append(dest)
                continue
            if method == "hub":
                from huggingface_hub import hf_hub_download
                p = Path(hf_hub_download(REPO, fn, repo_type="dataset", local_dir=out_dir, endpoint=endpoint))
            else:
                p = http_download(f"{endpoint}/datasets/{REPO}/resolve/main/{fn}", dest, proxy=proxy)
            print(f"  ✓ {p}")
            got.append(p)
    return got


def answer_and_judge(probes: list[dict], answer_llm, judge_llm) -> list[dict]:
    out = []
    for pr in probes:
        rec = dict(pr)
        if answer_llm is not None:
            user = json.dumps({"question": pr["question"], "now": pr["probe_t_s"], "STATE": pr["state"],
                               "EPISODES": pr["episodes"]}, ensure_ascii=False)
            data, text, lat, err = answer_llm.complete(ANSWER_SYSTEM, user)
            rec["agent_answer"] = (data or {}).get("answer") if isinstance(data, dict) else None
            rec["answer_error"] = err
        if judge_llm is not None and rec.get("agent_answer"):
            prompt = JUDGE_PROMPT.format(question=pr["question"], ground_truth_answer=pr["answer"],
                                         agent_answer=rec["agent_answer"])
            from ..llm import ChatJSON  # noqa: F401  类型提示
            judge_llm.cfg.json_mode = "none"
            _, text, _, err = judge_llm.complete("You are a strict grader.", prompt)
            rec["judge_raw"] = text if not err else err
            rec["correct"] = "yes" in (text or "").lower()
        elif judge_llm is not None:
            rec["correct"] = False  # 空答案按 M3-Agent 规则记为错误
        out.append(rec)
    return out


# ---------------------------------------------------------------- 主动性线索扫描（挑选验收视频）
PROACTIVE_CUES = {
    "reminder": r"\b(remind|don'?t forget|do not forget|remember to|make sure (you|to)|bring your|take your)\b",
    "forgot": r"\b(forgot|forget|left (it|my|your|the)|leave (it|my|your))\b",
    "search": r"\b(where (is|are|did|'?s) (my|the|your)|can'?t find|have you seen|looking for)\b",
    "leave": r"\b(let'?s go|i'?m leaving|have to go|gotta go|go to work|be late|hurry|bye|see you)\b",
    "robot": r"\b(robot|robots)\b",
    "items": r"\b(umbrella|keys?|phone|wallet|glasses|medicine|pills?|badge|charger|laptop|passport|card)\b",
    "safety": r"\b(stove|oven|iron|gas|fire|burn|turn (it )?off|switch off|water (is )?(running|boiling))\b",
}
CUE_WEIGHT = {"reminder": 3, "forgot": 3, "search": 2, "leave": 1, "robot": 2, "items": 1, "safety": 2}


def scan_proactive(srt_dir: str | Path = "data/m3bench/subtitles/robot") -> list[dict]:
    """按字幕里的主动性线索给每个视频打分：叮嘱 / 忘记 / 找东西 / 出门 / 对机器人说话 / 随身物品 / 安全。"""
    rx = {k: re.compile(v, re.I) for k, v in PROACTIVE_CUES.items()}
    ts = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->")
    out = []
    for p in sorted(Path(srt_dir).glob("*.srt")):
        text = p.read_text(encoding="utf-8", errors="ignore")
        last = 0
        hits: dict[str, list[tuple[int, str]]] = {k: [] for k in rx}
        cur_t = 0
        for line in text.splitlines():
            m = ts.search(line)
            if m:
                h, mi, s, _ = (int(x) for x in m.groups())
                cur_t = h * 3600 + mi * 60 + s
                last = max(last, cur_t)
                continue
            for k, r in rx.items():
                if r.search(line):
                    hits[k].append((cur_t, line.strip()[:90]))
        score = sum(CUE_WEIGHT[k] * len(v) for k, v in hits.items())
        out.append({"video": p.stem, "duration_s": last, "score": score,
                    "counts": {k: len(v) for k, v in hits.items()},
                    "examples": sorted([(t, k, s) for k, v in hits.items() if k in ("reminder", "forgot", "search", "safety")
                                        for t, s in v])[:8]})
    return sorted(out, key=lambda r: -r["score"])
