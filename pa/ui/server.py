"""可视化界面服务端：`pa ui` → http://127.0.0.1:8765

- POST /api/start {preset} 或 {video, start, duration}：后台线程实时运行引擎（realtime，可选倍速），事件经 SSE 推给浏览器
- GET  /api/stream?since=N：SSE 事件流（晚加入的页面会先收到历史事件）
- POST /api/feedback {delivery_id | need_id, kind}：主动消息下的“已处理 / 不需要 / 判断错误”回到系统
- POST /api/verdict {run, delivery_id?, t_ms, verdict}：人工核验（有用 / 没必要 / 判断错误 / 漏报），写 runs/<run>/verdicts.jsonl
- GET  /api/review/{run}：主动消息清单 + 核验结果 + 统计（PG_human 等）
- GET  /api/runs、/api/replay/{run}：历史运行回放
- /media/...：数据集视频（支持 Range）；/clips/...：H.264 预览片段；/tts/...：豆包语音合成的 mp3
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..runtime.config import load_config, load_env
from . import presets as P
from .replay import load_events

ROOT = P.ROOT
RUNS = ROOT / "runs"
STATIC = Path(__file__).with_name("static")
TTS_DIR = ROOT / "data" / "cache" / "tts"

app = FastAPI(title="Proactive Agent UI")
app.mount("/media", StaticFiles(directory=str(P.M3), check_dir=False), name="media")
P.CLIPS.mkdir(parents=True, exist_ok=True)
TTS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/clips", StaticFiles(directory=str(P.CLIPS)), name="clips")
app.mount("/tts", StaticFiles(directory=str(TTS_DIR)), name="tts")


def _prepare_clips() -> None:
    for p in P.PRESETS:
        if p.get("video_id") and P.video_path(p["video_id"]).exists() and P.probe(p["video_id"])["codec"] != "h264":
            try:
                P.ensure_clip(p["video_id"], p["start"], p["duration"])
            except Exception as e:  # noqa: BLE001
                print("预览片段生成失败", p["id"], e)


threading.Thread(target=_prepare_clips, daemon=True).start()


class Session:
    def __init__(self, run_dir: Path, meta: dict):
        self.run_dir, self.meta = run_dir, meta
        self.events: list[dict] = []
        self.subs: set[asyncio.Queue] = set()
        self.lock = threading.Lock()
        self.engine = None
        self.thread: threading.Thread | None = None
        self.done = False
        self.error: str | None = None
        self.loop: asyncio.AbstractEventLoop | None = None

    def publish(self, ev: dict) -> None:
        with self.lock:
            ev = {**ev, "i": len(self.events)}
            self.events.append(ev)
            subs = list(self.subs)
        for q in subs:
            if self.loop is not None:
                self.loop.call_soon_threadsafe(q.put_nowait, ev)


CURRENT: Session | None = None


class StartReq(BaseModel):
    preset: str | None = None
    video: str | None = None
    start: float | None = None
    duration: float | None = None
    speed: float = 1.0
    tts: bool = True


class FeedbackReq(BaseModel):
    need_id: str | None = None
    delivery_id: str | None = None
    kind: str


class VerdictReq(BaseModel):
    run: str
    delivery_id: str | None = None
    t_ms: int
    verdict: str  # useful | unnecessary | wrong | missed | clear
    note: str | None = None
    L: float | None = None


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/api/presets")
def presets():
    load_env(ROOT / ".env")
    return [P.status(p) for p in P.all_presets()]


@app.get("/api/videos")
def videos():
    """已下载的 M3-Bench robot 视频 + 字幕里的主动性线索（帮助挑选验收片段）。"""
    from ..bench.m3bench import scan_proactive

    scan = {r["video"]: r for r in scan_proactive(P.M3 / "subtitles" / "robot")}
    out = []
    for vid in P.downloaded():
        info = P.probe(vid)
        s = scan.get(vid, {})
        out.append({"video": vid, "duration": info["duration"], "codec": info["codec"], "srt": P.srt_path(vid).exists(),
                    "score": s.get("score"), "counts": s.get("counts"),
                    "examples": [{"t": t, "kind": k, "text": x} for t, k, x in s.get("examples", [])]})
    return out


@app.get("/api/tts_status")
def tts_status():
    load_env(ROOT / ".env")
    cfg = load_config(str(ROOT / P.CFG))
    tc = cfg.get("tts", {})
    return {"key": bool(os.environ.get(tc.get("api_key_env", "DOUBAO_TTS_API_KEY"))), "enabled": bool(tc.get("enabled")),
            "api": tc.get("api"), "voice": tc.get("voice")}


def _build_engine(p: dict, sess: Session, speed: float, tts_on: bool):
    from ..runtime.engine import Engine
    from ..runtime.factory import build_decider, build_drafter, build_pa_parts, build_slow_llm, build_vlm
    from ..source import SyntheticSource, VideoSource
    from ..speech import SrtUtterances

    if p.get("scenario"):
        import yaml
        sc = yaml.safe_load((ROOT / p["scenario"]).read_text(encoding="utf-8"))
        cfg = load_config(None, sc.get("config"))
        src = SyntheticSource(sc["duration_s"], sc.get("changes", []), fps=cfg["source"]["sample_fps"])
        return Engine(cfg, src, build_vlm(cfg, sc), build_decider(cfg), build_drafter(cfg), session_id=p["id"],
                      run_dir=sess.run_dir, mode="realtime", quiet=True, on_event=sess.publish, ui_frames=True,
                      ui_thumbs=True, speed=speed, pa_parts=build_pa_parts(cfg, "realtime"))
    cfg = load_config(str(ROOT / p.get("config", P.CFG)))
    if p.get("egocentric"):
        cfg["session"]["camera_mode"] = "egocentric"
    cfg.setdefault("tts", {})["enabled"] = bool(tts_on and cfg["tts"].get("enabled", True))
    vid = p["video_id"]
    src = VideoSource(str(P.video_path(vid)), cfg["source"]["sample_fps"], cfg["source"]["short_side"],
                      p["start"], p["duration"])
    utts = SrtUtterances(P.srt_path(vid), offset_s=p["start"], latency_ms=cfg["speech"]["srt_latency_ms"]).all(
        int(p["duration"] * 1000)) if P.srt_path(vid).exists() else None
    return Engine(cfg, src, build_vlm(cfg, cache_path=ROOT / "data/cache/patch_cache.sqlite"), build_decider(cfg),
                  build_drafter(cfg), session_id=str(p["id"]).replace(":", "_"), run_dir=sess.run_dir, mode="realtime",
                  utterances=utts, slow_llm=build_slow_llm(cfg), quiet=True, on_event=sess.publish, ui_thumbs=True,
                  ui_frames=sess.meta.get("video_url") is None, speed=speed,
                  pa_parts=build_pa_parts(cfg, "realtime"))


@app.post("/api/start")
async def start(req: StartReq):
    global CURRENT
    load_env(ROOT / ".env")
    p = P.resolve(req.model_dump())
    if p is None:
        raise HTTPException(404, "unknown preset / video")
    st = P.status(p)
    if not st["available"]:
        raise HTTPException(400, st["reason"])
    if p.get("video_id") and st["video_url"] is None:
        P.ensure_clip_bg(p["video_id"], p["start"], p["duration"])
    if CURRENT is not None and not CURRENT.done and CURRENT.engine is not None:
        CURRENT.engine.stop()
    slug = str(p["id"]).replace(":", "_")
    run_dir = RUNS / f"ui_{slug}_{time.strftime('%Y%m%d_%H%M%S')}"
    run_dir.mkdir(parents=True, exist_ok=True)
    speed = max(0.5, min(3.0, req.speed or 1.0))
    meta = {k: st.get(k) for k in ("id", "title", "desc", "video_id", "video_url", "view_start", "view_note", "start",
                                   "duration", "markers", "config")}
    meta.update(speed=speed, started=time.strftime("%Y-%m-%d %H:%M:%S"))
    sess = Session(run_dir, meta)
    sess.loop = asyncio.get_running_loop()
    sess.engine = _build_engine(p, sess, speed, req.tts)
    meta["tts"] = sess.engine.pa.tts.name if (sess.engine.pa and sess.engine.pa.tts) else None
    meta["proactive"] = sess.engine.pa is not None
    (run_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def run():
        try:
            sess.engine.run()
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            sess.error = f"{type(e).__name__}: {e}"
            sess.publish({"t": sess.engine.t_now, "ev": "error", "text": sess.error})
        finally:
            sess.done = True

    sess.thread = threading.Thread(target=run, daemon=True)
    CURRENT = sess
    sess.thread.start()
    return {"run": run_dir.name, "meta": meta}


@app.get("/api/current")
def current():
    """正在运行（或最近一次）的界面会话：供刷新后的页面接入。"""
    if CURRENT is None:
        raise HTTPException(404, "没有会话")
    return {"run": CURRENT.run_dir.relative_to(RUNS).as_posix(), "meta": CURRENT.meta, "done": CURRENT.done,
            "t_now": CURRENT.engine.t_now if CURRENT.engine is not None else 0, "n_events": len(CURRENT.events)}


@app.post("/api/stop")
def stop():
    if CURRENT is not None and CURRENT.engine is not None:
        CURRENT.engine.stop()
    return {"ok": True}


@app.post("/api/feedback")
def feedback(req: FeedbackReq):
    if CURRENT is None or CURRENT.engine is None or CURRENT.done:
        raise HTTPException(400, "没有正在运行的会话")
    CURRENT.engine.inject_feedback(req.need_id, req.kind, delivery_id=req.delivery_id)
    return {"ok": True}


def _run_dir(run: str) -> Path:
    d = (RUNS / run).resolve()
    if RUNS.resolve() not in d.parents or not d.exists():
        raise HTTPException(404, "no such run")
    return d


@app.post("/api/verdict")
def verdict(req: VerdictReq):
    d = _run_dir(req.run)
    row = {**req.model_dump(), "wall": time.strftime("%Y-%m-%d %H:%M:%S")}
    with open(d / "verdicts.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    if CURRENT is not None and CURRENT.run_dir.resolve() == d:
        CURRENT.publish({"t": req.t_ms, "ev": "verdict", **row})
    return {"ok": True}


@app.get("/api/review/{run:path}")
def review(run: str):
    from ..eval import evaluate, load_run

    d = _run_dir(run)
    r = load_run(d)
    return {"run": run, "meta": r["meta"], "deliveries": r["deliveries"], "verdicts": r["verdicts"],
            "stats": evaluate(d)}


@app.get("/api/stream")
async def stream(since: int = 0):
    sess = CURRENT
    if sess is None:
        raise HTTPException(404, "没有会话")
    q: asyncio.Queue = asyncio.Queue()
    with sess.lock:
        backlog = sess.events[since:]
        sess.subs.add(q)

    async def gen():
        try:
            for ev in backlog:
                yield f"data: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n"
            if sess.done and backlog and backlog[-1].get("ev") in ("end", "error"):
                return
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield f"data: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n"
                if ev.get("ev") in ("end", "error"):
                    return
        finally:
            sess.subs.discard(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/runs")
def runs():
    out = []
    dirs = [d for d in list(RUNS.glob("*")) + list(RUNS.glob("*/*")) if d.is_dir() and (d / "log.sqlite").exists()]
    for d in sorted(dirs, key=lambda x: x.stat().st_mtime, reverse=True):
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8")) if (d / "meta.json").exists() else {}
        n_dl = None
        if (d / "summary.json").exists():
            try:
                n_dl = len(json.loads((d / "summary.json").read_text(encoding="utf-8")).get("deliveries", []))
            except Exception:  # noqa: BLE001
                pass
        n_v = sum(1 for _ in open(d / "verdicts.jsonl", encoding="utf-8")) if (d / "verdicts.jsonl").exists() else 0
        out.append({"run": d.relative_to(RUNS).as_posix(), "title": meta.get("title"),
                    "has_video": bool(meta.get("video_id") or meta.get("video_url")),
                    "deliveries": n_dl, "verdicts": n_v, "proactive": meta.get("proactive"),
                    "duration": meta.get("duration"), "mtime": d.stat().st_mtime})
    return out[:120]


@app.get("/api/replay/{run:path}")
def replay(run: str):
    from ..eval.opportunities import load_verdicts

    d = _run_dir(run)
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8")) if (d / "meta.json").exists() else {}
    vid = meta.get("video_id")
    if vid is None and meta.get("id") in ("lr06_umbrella", "office05"):  # 旧版运行的 meta 没有 video_id
        vid = {"lr06_umbrella": "living_room_06", "office05": "office_05"}[meta["id"]]
    if vid and meta.get("duration") is not None:
        url, vs, note = P.view_of(vid, float(meta.get("start") or 0), float(meta["duration"]))
        if url is None:
            P.ensure_clip_bg(vid, float(meta.get("start") or 0), float(meta["duration"]))
        meta.update(video_url=url, view_start=vs, view_note=note, video_id=vid)
    if not meta.get("duration") and (d / "summary.json").exists():
        meta["duration"] = json.loads((d / "summary.json").read_text(encoding="utf-8")).get("t_end_ms", 0) / 1000
    return {"run": run, "meta": meta, "events": load_events(d), "verdicts": load_verdicts(d)}


def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    import uvicorn

    load_env(ROOT / ".env")
    print(f"可视化界面：http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning")
