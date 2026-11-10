"""可视化界面的视频源：内置演示 + 所有已下载的 M3-Bench robot 视频（可任选起点与时长）。

视频需先用 `pa bench m3-download <id>` 下载（见 README）。浏览器预览：H.264 原片直接播放（支持 Range 拖动）；
HEVC（如 living_room_06）多数浏览器放不了，后台转一份 H.264 预览片段，转好之前界面显示 AI 看到的帧。
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
M3 = ROOT / "data" / "m3bench"
CLIPS = ROOT / "data" / "ui_clips"
ANN = ROOT / "data" / "annotations"
CFG = "config/deepseek.yaml"

PRESETS: list[dict] = [
    {
        "id": "lr06_full",
        "title": "living_room_06 全片 · 16 分钟（长视频验收）",
        "desc": "M3-Bench 真实第一人称视频全片。三人聊瑜伽、约晚饭；13:57 Jack 提醒“别忘了带伞”，14:32 两人匆忙准备出门，"
                "14:52 Jack 才发现她们忘了伞。看系统在整段长视频里什么时候沉默、什么时候等待、什么时候开口。",
        "video_id": "living_room_06", "start": 0, "duration": 970, "egocentric": True,
        "markers": [{"t": 837.3, "label": "Jack：别忘了带伞", "kind": "gt"},
                    {"t": 872.0, "label": "两人准备出门", "kind": "gt"},
                    {"t": 892.6, "label": "Jack 发现忘带伞（截止）", "kind": "deadline"}],
    },
    {
        "id": "lr06_umbrella",
        "title": "living_room_06 · 雨伞片段（13:30 起 90 s）",
        "desc": "短片段快速验证：Jack 在 28 s 说“别忘了带伞”，82 s 才发现她们忘带。看系统能否在她们出门时主动提醒。",
        "video_id": "living_room_06", "start": 810, "duration": 90, "egocentric": True,
        "markers": [{"t": 27.3, "label": "Jack：别忘了带伞", "kind": "gt"},
                    {"t": 62.0, "label": "两人准备出门", "kind": "gt"},
                    {"t": 82.6, "label": "Jack 发现忘带伞（截止）", "kind": "deadline"}],
    },
    {
        "id": "forgot_phone",
        "title": "合成剧本 · 忘带手机（离线，无需 Key）",
        "desc": "20 秒剧情：放下手机 → 拿起背包 → 走向门口 → 系统提醒 → 取回。脚本化感知 + 模板回复，离线看清整条链路。",
        "scenario": "scenarios/forgot_phone.yaml", "duration": 20,
        "markers": [{"t": 2.0, "label": "放下手机", "kind": "gt"}, {"t": 9.0, "label": "拿起背包", "kind": "gt"},
                    {"t": 11.0, "label": "走向门口", "kind": "gt"}, {"t": 15.0, "label": "取回手机", "kind": "gt"}],
    },
]

_probe_cache: dict[str, dict] = {}
_clip_jobs: dict[str, threading.Thread] = {}


def video_path(vid: str) -> Path:
    return M3 / "videos" / "robot" / f"{vid}.mp4"


def srt_path(vid: str) -> Path:
    return M3 / "subtitles" / "robot" / f"{vid}.srt"


def probe(vid: str) -> dict:
    """时长与编码（缓存）。"""
    if vid in _probe_cache:
        return _probe_cache[vid]
    p = video_path(vid)
    info = {"duration": None, "codec": None}
    if p.exists():
        try:
            out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                  "stream=codec_name:format=duration", "-of", "json", str(p)],
                                 capture_output=True, text=True, timeout=30).stdout
            j = json.loads(out)
            info = {"duration": float(j["format"]["duration"]), "codec": j["streams"][0]["codec_name"]}
        except Exception:  # noqa: BLE001
            pass
    _probe_cache[vid] = info
    return info


def downloaded() -> list[str]:
    d = M3 / "videos" / "robot"
    return sorted(p.stem for p in d.glob("*.mp4")) if d.exists() else []


def clip_path(vid: str, start: float, duration: float) -> Path:
    return CLIPS / f"{vid}_{int(start)}_{int(duration)}.mp4"


def clip_ready(vid: str, start: float, duration: float) -> bool:
    c = clip_path(vid, start, duration)
    return c.exists() and c.stat().st_size > 0


def ensure_clip(vid: str, start: float, duration: float) -> Path | None:
    """转出 H.264 预览片段（时间轴从 0 开始）；阻塞调用。"""
    src = video_path(vid)
    if not src.exists():
        return None
    out = clip_path(vid, start, duration)
    if out.exists() and out.stat().st_size > 0:
        return out
    CLIPS.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".part.mp4")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", str(start), "-t", str(duration), "-i", str(src),
                    "-vf", "scale=-2:540", "-c:v", "libx264", "-preset", "veryfast", "-crf", "25", "-c:a", "aac",
                    "-b:a", "128k", "-movflags", "+faststart", str(tmp)], check=True)
    tmp.replace(out)
    return out


def ensure_clip_bg(vid: str, start: float, duration: float) -> None:
    key = clip_path(vid, start, duration).name
    job = _clip_jobs.get(key)
    if (job is not None and job.is_alive()) or clip_ready(vid, start, duration):
        return

    def run():
        try:
            ensure_clip(vid, start, duration)
        except Exception as e:  # noqa: BLE001
            print("预览片段生成失败", key, e)
    _clip_jobs[key] = t = threading.Thread(target=run, daemon=True)
    t.start()


def view_of(vid: str, start: float, duration: float) -> tuple[str | None, float, str]:
    """浏览器怎么播：返回 (video_url, view_start, 说明)。"""
    if clip_ready(vid, start, duration):
        return f"/clips/{clip_path(vid, start, duration).name}", 0.0, "预览片段"
    if probe(vid)["codec"] == "h264":
        return f"/media/videos/robot/{vid}.mp4", float(start), "原片"
    return None, 0.0, "HEVC 原片浏览器放不了，预览片段转码中，先显示 AI 看到的帧"


def annotation_markers(vid: str, start: float) -> list[dict]:
    p = ANN / f"{vid}.json"
    if not p.exists():
        return []
    out = []
    for o in json.loads(p.read_text(encoding="utf-8")).get("opportunities", []):
        if o.get("label", "must_fire") == "must_fire" and o["t_latest"] >= start:
            out.append({"t": o["t_latest"] - start, "label": (o.get("desc") or o.get("id", ""))[:16] + "（截止）",
                        "kind": "deadline"})
    return out


def m3_spec(vid: str, start: float = 0, duration: float | None = None, title: str | None = None) -> dict:
    info = probe(vid)
    dur = duration or (info["duration"] - start if info["duration"] else 600)
    return {"id": f"m3:{vid}:{int(start)}:{int(dur)}", "video_id": vid, "start": float(start), "duration": float(dur),
            "egocentric": True,
            "title": title or f"{vid} · {int(start) // 60}:{int(start) % 60:02d} 起 {dur / 60:.0f} 分钟",
            "desc": "M3-Bench robot 真实第一人称视频（字幕作理想 ASR）。", "markers": annotation_markers(vid, start)}


def all_presets() -> list[dict]:
    out = [dict(p) for p in PRESETS]
    have = {p.get("video_id") for p in PRESETS if p.get("start", 0) == 0}
    for vid in downloaded():
        if vid not in have:
            out.append(m3_spec(vid))
    return out


def resolve(spec: dict) -> dict | None:
    """把 {preset} 或 {video, start, duration} 解析成完整规格。"""
    if spec.get("preset"):
        p = next((dict(p) for p in all_presets() if p["id"] == spec["preset"]), None)
        if p is None and str(spec["preset"]).startswith("m3:"):
            _, vid, s, d = spec["preset"].split(":")
            p = m3_spec(vid, float(s), float(d))
        return p
    if spec.get("video"):
        return m3_spec(spec["video"], float(spec.get("start") or 0),
                       float(spec["duration"]) if spec.get("duration") else None)
    return None


def status(p: dict) -> dict:
    out = dict(p)
    ok, why = True, ""
    if p.get("video_id"):
        vid = p["video_id"]
        if not video_path(vid).exists():
            ok, why = False, f"缺少视频：先运行 pa bench m3-download {vid} --proxy http://127.0.0.1:7890"
        elif not os.environ.get("DEEPSEEK_API_KEY"):
            ok, why = False, ".env 中缺少 DEEPSEEK_API_KEY"
        out["config"] = p.get("config", CFG)
        out["srt"] = str(srt_path(vid)) if srt_path(vid).exists() else None
        url, vs, note = view_of(vid, p["start"], p["duration"]) if ok else (None, 0.0, "")
        out.update(video_url=url, view_start=vs, view_note=note, codec=probe(vid)["codec"])
    else:
        out.update(video_url=None, view_start=0, view_note="合成画面")
    out["available"], out["reason"] = ok, why
    return out
