"""运行时引擎：离散事件调度，把 M1–M13 串起来（设计文档 §7）。

两种时钟：
- sim（replay_sim）：模型调用真实执行（或命中缓存 / 脚本），但其“完成时刻” = 发起时刻 + 实测时延；
  工作池排队同样被模拟。决策只能看到 t_avail <= now 的信息，与 1× 回放可见性一致。
- realtime（replay_1x / live）：按墙钟推进，模型调用在线程池中执行，完成时刻 = 实际完成时间。
状态只在本线程（单写者）中修改。
"""
from __future__ import annotations

import base64
import heapq
import io
import itertools
import json
import queue
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

from ..agenda import TriggerEngine, load_templates
from ..decision import apply_rules, build_context
from ..gate import FrameGate, GateCfg
from ..memory import EpisodeStore
from ..perception.prompt import PROMPT_VERSION, fmt_ms
from ..policy import PolicyGate
from ..response import LLMDrafter, TemplateDrafter
from ..schemas import (
    ConfirmOp, Decision, Delivery, NeedEdge, NeedFeedbackOp, NeedUpsertOp, StateDelta, StatePatch, Utterance,
    ViewInfo, WindowRequest, parse_ops,
)
from ..slow import Consolidator
from ..state import Reducer, Sequencer, StateStore, Validator, build_attrs
from ..state.attrs import render_value
from ..storage import EventLog

INF = float("inf")


@dataclass
class _Call:
    kind: str
    t_start: int
    payload: dict
    future: Future


class Engine:
    def __init__(self, cfg: dict, source, vlm, decider, drafter, *, session_id: str, run_dir: str | Path,
                 mode: str = "sim", utterances: list[Utterance] | None = None, asr=None, faces=None,
                 slow_llm=None, scripted_feedback: list[dict] | None = None,
                 probes: list[tuple[int, str]] | None = None, on_probe: Callable | None = None,
                 speed: float = 1.0, quiet: bool = False, max_ms: int | None = None,
                 on_event: Callable[[dict], None] | None = None, ui_thumbs: bool = False, ui_frames: bool = False,
                 pa_parts: dict | None = None):
        self.cfg, self.mode, self.quiet, self.speed, self.max_ms = cfg, mode, quiet, speed, max_ms
        self.source, self.vlm, self.decider, self.drafter = source, vlm, decider, drafter
        self.asr, self.faces = asr, faces
        self.session_id = session_id
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)

        attrs = build_attrs(cfg["state"].get("ttl_s"))
        self.store = StateStore(cfg["session"].get("camera_mode", "fixed"))
        self.validator, self.reducer = Validator(attrs), Reducer(attrs)
        self.sequencer = Sequencer()
        ac = cfg["agenda"]
        self.agenda = TriggerEngine(load_templates(ac.get("templates")), int(ac["disarm_debounce_s"] * 1000),
                                    int(ac["redecide_min_s"] * 1000))
        g = cfg["gate"]
        self.gate = FrameGate(GateCfg(**{k: g[k] for k in g if k in GateCfg.__dataclass_fields__},
                                      fps=cfg["source"]["sample_fps"]))
        pc = cfg["policy"]
        self.permissions = cfg["session"].get("permissions", {"may_ask": True, "may_remind": True})
        self.policy = PolicyGate(int(pc["global_min_gap_s"] * 1000), int(pc["certain_max_age_s"] * 1000), self.permissions)
        self.episodes = EpisodeStore()
        self.consolidator = Consolidator(session_id, int(cfg["slow"]["every_s"] * 1000), llm=slow_llm)
        self.template = TemplateDrafter()
        self.log = EventLog(self.run_dir / "log.sqlite", session_id)

        self.workers = int(cfg["perception"].get("workers", 3))
        self.worker_free = [0] * self.workers
        self.pool = ThreadPoolExecutor(max_workers=self.workers + 8) if mode == "realtime" else None
        self.inflight: list[_Call] = []
        self.waiting: list[WindowRequest] = []

        self.heap: list = []
        self._seq = itertools.count()
        self._ids = itertools.count(1)
        self.frame_idx = 0
        self.utts = sorted(utterances or [], key=lambda u: u.t_avail_ms)
        self.utt_by_id: dict[str, Utterance] = {}
        self.slices: dict[int, set[str]] = {}
        self.pending_dec: dict[str, str | None] = {}  # need_id -> 等待现场草稿的 decision_id
        self.lag_ema: float | None = None  # 感知滞后（t_avail - t_event）的指数平均，仅统计用
        self.commits: list[tuple[int, int]] = []  # 已提交感知补丁的 (slice_version, t_event)
        self.deltas: list[StateDelta] = []
        self.edges: list[NeedEdge] = []
        self.decisions: list[Decision] = []
        self.deliveries: list[Delivery] = []
        self.blocked: list[dict] = []
        self.vlm_calls: list[dict] = []
        self.timeline: list[dict] = []
        self.on_probe = on_probe
        for fb in scripted_feedback or []:
            self._push(fb["t_ms"], "feedback", fb)
        for t, pid in probes or []:
            self._push(t, "probe", {"probe_id": pid})
        self.last_snapshot_version = 0
        self.source_done = False
        self.t_now = 0
        self._wall0 = None
        # 可视化界面：结构化事件、外部反馈、停止
        self.on_event = on_event
        self.ui_thumbs, self.ui_frames = ui_thumbs, ui_frames
        self.ext_q: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self._ui_file = open(self.run_dir / "ui_events.jsonl", "w", encoding="utf-8")
        self._last_frame_emit = -10_000
        # 主动决策核心（机会 → 价值 → 门控）；关闭时退回旧的“需求触发 → 决策模型”路径
        self.pa = None
        if cfg.get("proactive", {}).get("enabled"):
            from ..proactive import ProactiveCore
            self.pa = ProactiveCore(cfg, self, **(pa_parts or {}))

    # ================================================================== 主循环
    def run(self) -> dict:
        self.log.start(getattr(self.source, "path", type(self.source).__name__), self.mode,
                       self.store.camera_mode, self.cfg, time.strftime("%Y-%m-%dT%H:%M:%S"))
        if getattr(self.source, "live", False):
            return self._run_live()
        frames: Iterator = iter(self.source.frames())
        audio: Iterator = iter(self.source.audio(self.cfg["speech"].get("chunk_s", 8))) if self.asr else iter(())
        nf, na = next(frames, None), next(audio, None)
        ui = 0
        self._push(self.cfg["state"]["tick_ms"], "tick", {})
        self._wall0 = time.perf_counter()
        self.emit("start", 0, mode=self.mode, live=False)
        while True:
            if self.stop_event.is_set():
                break
            self._poll_calls()
            if self.mode == "realtime":
                self._drain_ext()
            cands = [
                (nf.pts_ms if nf else INF, 0, "frame"),
                (na.end_ms if na else INF, 1, "audio"),
                (self.utts[ui].t_avail_ms if ui < len(self.utts) else INF, 2, "utt"),
                (self.heap[0][0] if self.heap else INF, 3, "event"),
            ]
            if self.max_ms is not None:
                cands = [c if c[0] <= self.max_ms else (INF, c[1], c[2]) for c in cands]
            t_next, _, kind = min(cands)
            if t_next == INF:
                if self.inflight:
                    time.sleep(0.005)
                    continue
                break
            if self.mode == "realtime":
                now = self._wall_ms()
                if t_next > now:
                    time.sleep(min(0.01, (t_next - now) / 1000))
                    continue
            self.t_now = int(t_next)
            if kind == "frame":
                self._on_frame(nf)
                nf = next(frames, None)
                if nf is None:
                    self.source_done = True
            elif kind == "audio":
                self._on_audio(na)
                na = next(audio, None)
            elif kind == "utt":
                self._on_utt(self.utts[ui])
                ui += 1
            else:
                t, _, ek, payload = heapq.heappop(self.heap)
                getattr(self, f"_ev_{ek}")(int(t), payload)
        return self._finish()

    def _run_live(self) -> dict:
        """实时流：画面按到达时刻进入，模型调用在线程池并行，所有事件按墙钟触发。"""
        assert self.mode == "realtime", "实时流只能用 realtime 模式"
        self._wall0 = time.perf_counter()
        self.source.start(self._wall0)
        self._push(self.cfg["state"]["tick_ms"], "tick", {})
        ui = 0
        last_frame_wall = time.perf_counter()
        self.emit("start", 0, mode=self.mode, live=True)
        while True:
            if self.stop_event.is_set():
                break
            self._poll_calls()
            self._drain_ext()
            items = self.source.drain()
            for item in items:
                self.t_now = item.pts_ms
                self._on_frame(item)
            if items:
                last_frame_wall = time.perf_counter()
            now = self._wall_ms()
            while ui < len(self.utts) and self.utts[ui].t_avail_ms <= now:
                self.t_now = self.utts[ui].t_avail_ms
                self._on_utt(self.utts[ui])
                ui += 1
            while self.heap and self.heap[0][0] <= now:
                t, _, ek, payload = heapq.heappop(self.heap)
                self.t_now = int(t)
                getattr(self, f"_ev_{ek}")(int(t), payload)
            if self.source.done and not self.source_done:
                self.source_done = True
                self._say(now, f"⏹ 视频流结束（解码 {self.source.n_decoded} 帧，丢弃 {self.source.n_dropped}）"
                               + (f"：{self.source.error}" if self.source.error else ""), "info")
            if self.source_done and not self.inflight and not any(k != "tick" for _, _, k, _ in self.heap):
                break
            if self.max_ms is not None and now > self.max_ms:
                break
            if not self.source.done and time.perf_counter() - last_frame_wall > 60:
                self._say(now, "⚠ 60 s 没有收到画面，感知中断", "warn")
                last_frame_wall = time.perf_counter()
            time.sleep(0.005)
        return self._finish()

    # ---------------- 可视化事件
    def emit(self, ev_type: str, t: int, **data) -> None:
        ev = {"t": int(t), "ev": ev_type, **data}
        try:
            self._ui_file.write(json.dumps(ev, ensure_ascii=False, default=str) + "\n")
            now = time.perf_counter()
            if now - getattr(self, "_ui_flush", 0) > 2:  # 运行中途回放 / 接入也能看到完整事件
                self._ui_file.flush()
                self._ui_flush = now
        except ValueError:  # 文件已关闭
            pass
        if self.on_event is not None:
            try:
                self.on_event(ev)
            except Exception:  # noqa: BLE001  界面出错不影响系统
                pass

    @staticmethod
    def _thumb(img, width: int = 220, quality: int = 70) -> str:
        from PIL import Image

        im = Image.fromarray(img)
        h = int(im.height * width / im.width)
        buf = io.BytesIO()
        im.resize((width, h)).save(buf, format="JPEG", quality=quality)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()

    def inject_feedback(self, need_id: str | None, kind: str, delivery_id: str | None = None) -> None:
        """线程安全：界面上的“已处理 / 不需要 / 判断错误”。"""
        self.ext_q.put(("feedback", {"need_id": need_id, "kind": kind, "delivery_id": delivery_id}))

    def stop(self) -> None:
        self.stop_event.set()

    def _drain_ext(self) -> None:
        while True:
            try:
                kind, payload = self.ext_q.get_nowait()
            except queue.Empty:
                return
            self._push(self._wall_ms(), kind, payload)

    def _wall_ms(self) -> int:
        return int((time.perf_counter() - self._wall0) * 1000 * self.speed)

    def _push(self, t: int, kind: str, payload: Any) -> None:
        heapq.heappush(self.heap, (int(t), next(self._seq), kind, payload))

    def _id(self, prefix: str) -> str:
        return f"{prefix}_{next(self._ids):04d}"

    def _say(self, t: int, what: str, cat: str = "info") -> None:
        self.timeline.append({"t_ms": t, "cat": cat, "text": what})
        self.emit("log", t, cat=cat, text=what)
        if not self.quiet:
            print(f"[{fmt_ms(t)}] {what}", flush=True)

    # ---------------- 模型调用：sim 下同步执行、按实测时延排程；realtime 下进线程池
    def _call(self, kind: str, t_start: int, fn: Callable[[], Any], payload: dict, latency_of: Callable[[Any], int],
              pool: ThreadPoolExecutor | None = None) -> None:
        if self.mode == "sim":
            res = fn()
            self._push(t_start + max(0, int(latency_of(res))), kind, {**payload, "result": res, "t_start": t_start})
        else:
            def runner():
                t0 = time.perf_counter()
                res = fn()
                # 脚本化 / 规则组件的“名义时延”在实时模式下真实等待，真实模型的时延≈已耗时，不会额外等待
                nominal = max(0, int(latency_of(res))) / 1000 / self.speed if not isinstance(res, Exception) else 0
                rest = nominal - (time.perf_counter() - t0)
                if rest > 0:
                    time.sleep(rest)
                return res
            self.inflight.append(_Call(kind, t_start, payload, (pool or self.pool).submit(runner)))

    def _poll_calls(self) -> None:
        if not self.inflight:
            return
        now = self._wall_ms()
        done = [c for c in self.inflight if c.future.done()]
        for c in done:
            self.inflight.remove(c)
            try:
                res = c.future.result()
            except Exception as e:  # noqa: BLE001
                res = e
            self._push(max(now, c.t_start), c.kind, {**c.payload, "result": res, "t_start": c.t_start})

    # ================================================================== 感知
    def _on_frame(self, item) -> None:
        fid = f"f_{self.frame_idx:06d}"
        self.frame_idx += 1
        if self.ui_frames and item.pts_ms - self._last_frame_emit >= 400:
            self._last_frame_emit = item.pts_ms
            self.emit("frame", item.pts_ms, img=self._thumb(item.img, 480, 75))
        for req in self.gate.push(fid, item.pts_ms, item.img):
            self._dispatch(req, item.pts_ms)

    def _on_audio(self, chunk) -> None:
        def run():
            return self.asr.transcribe_chunk(chunk.start_ms, chunk.end_ms, chunk.pcm)
        self._call("asr", chunk.end_ms, run, {}, latency_of=lambda r: r[1])

    def _ev_asr(self, t: int, p: dict) -> None:
        res = p["result"]
        if isinstance(res, Exception):
            return
        for u in res[0]:
            self._on_utt(u.model_copy(update={"t_avail_ms": t}))

    def _on_utt(self, u: Utterance) -> None:
        self.utt_by_id[u.utt_id] = u
        self.gate.on_utterance(u.utt_id, u.t_avail_ms)
        self.log.append("utterance", u.utt_id, u.end_ms, u.t_avail_ms, u)
        self.emit("utterance", u.t_avail_ms, utt_id=u.utt_id, speaker=u.voice_anchor or u.person_id, text=u.text,
                  start_ms=u.start_ms, end_ms=u.end_ms)
        if self.consolidator.llm is not None:  # M12：逐句判断是否包含需要日后提醒的请求 / 叮嘱
            said = {"utt_id": u.utt_id, "speaker": u.person_id or u.voice_anchor, "content": u.text}
            lines, ctx = self.store.describe(), self._recent_speech(u.t_avail_ms)[:-1]
            self._call("speech_scan", u.t_avail_ms, lambda: self.consolidator.compile_need(said, lines, ctx),
                       {"said": said}, latency_of=lambda r: r[1])
        if self.pa is not None:
            self.pa.on_utterance(u, u.t_avail_ms)

    def _ev_speech_scan(self, t: int, p: dict) -> None:
        res = p["result"]
        spec = None if isinstance(res, Exception) else res[0]
        self.log.append("need_compile", self._id("compile"), p["t_start"], t, {"said": p["said"], "spec": spec})
        if spec:
            spec["source_utt"] = p["said"]["content"]
            spec["said_ms"] = p["t_start"]
            try:
                if spec.get("due_in_s"):
                    spec["due_ms"] = int(p["t_start"] + float(spec["due_in_s"]) * 1000)
            except (TypeError, ValueError):
                spec.pop("due_in_s", None)
            self._say(t, f"🧩 语音需求编译：“{p['said']['content']}” → {spec.get('summary') or spec.get('topic')}", "slow")
            self.emit("compile", t, utt=p["said"]["content"], speaker=p["said"].get("speaker"),
                      summary=spec.get("summary"), topic=spec.get("topic"), latency_ms=t - p["t_start"])
            self._ev_need_upsert(t, {"spec": spec})

    def _free_worker(self, t: int) -> int | None:
        if self.mode == "realtime":
            return 0 if sum(c.kind == "patch" for c in self.inflight) < self.workers else None
        for i, ft in enumerate(self.worker_free):
            if ft <= t:
                return i
        return None

    def _busy(self, t: int) -> int:
        if self.mode == "realtime":
            return sum(c.kind == "patch" for c in self.inflight)
        return sum(ft > t for ft in self.worker_free)

    def _dispatch(self, req: WindowRequest, t: int) -> None:
        # 背压（设计文档 §3 M2）：还没有任何状态时只保留一个在途请求，避免并发窗口各自“新建”同一批实体；
        # 工作池满时丢弃心跳窗口（下一个窗口会覆盖这段时间）
        busy = self._busy(t)
        if req.trigger in ("heartbeat", "focus", "initial") and (
                busy >= self.workers or (self.store.version == 0 and busy)):
            self.log.append("window_dropped", req.window_id, req.end_ms, t, {"trigger": req.trigger, "busy": busy})
            return
        w = self._free_worker(t)
        if w is None:
            if self.mode == "sim":
                self._push(min(self.worker_free), "dispatch", {"req": req})
            else:
                self.waiting.append(req)
            return
        sl = self.store.slice_for(t, focus=req.focus_entities,
                                  open_needs=[f"{n.need_id}:{n.kind}" for n in self.agenda.active_needs()])
        self.slices[req.seq] = set(sl.check_keys)
        pairs = [(fid, self.gate.get(fid)) for fid in req.frame_ids]
        pairs = [(fid, img) for fid, img in pairs if img is not None]
        fids, frames = [p[0] for p in pairs], [p[1] for p in pairs]
        times = [self.gate.pts(f) for f in fids]
        voice2p = {e.anchors["voice"]: e.id for e in self.store.entities.values() if e.anchors.get("voice")}
        utts = [self.utt_by_id[u] for u in req.utt_ids if u in self.utt_by_id]
        utts = [u.model_copy(update={"person_id": voice2p.get(u.voice_anchor)}) if u.voice_anchor else u for u in utts]
        face_note = None
        if self.faces is not None and frames:
            from ..speech.faces import draw_labels
            boxes = self.faces.process(fids, frames)
            by_frame: dict[str, list] = {}
            for b in boxes:
                by_frame.setdefault(b.frame_id, []).append(b)
            frames = [draw_labels(img, by_frame.get(fid, [])) for fid, img in zip(fids, frames)]
            if boxes:
                lab2p = {e.anchors.get("face"): e.id for e in self.store.entities.values() if e.anchors.get("face")}
                face_note = "FACES " + ", ".join(sorted({f"{b.label}={lab2p.get(b.face_anchor, '(new)')}" for b in boxes}))
        req = req.model_copy(update={"frame_ids": fids})
        self.log.append("window", req.window_id, req.end_ms, t, {**req.model_dump(), "slice_version": sl.version})
        self.emit("window", t, window_id=req.window_id, trigger=req.trigger, start_ms=req.start_ms, end_ms=req.end_ms,
                  frame_times=times, frames=[self._thumb(f) for f in frames] if self.ui_thumbs else [],
                  n_utts=len(utts))
        if self.mode == "sim":
            self.worker_free[w] = t  # 占位，下面用真实时延更新
        fn = lambda: self.vlm.propose(req, sl, frames, times, utts, face_note)  # noqa: E731

        def latency(res):
            lat = res.latency_ms if not isinstance(res, Exception) else 0
            self.worker_free[w] = t + lat
            return lat
        self._call("patch", t, fn, {"req": req, "slice_version": sl.version}, latency)

    def _ev_dispatch(self, t: int, p: dict) -> None:
        self._dispatch(p["req"], t)

    def _ev_patch(self, t: int, p: dict) -> None:
        req: WindowRequest = p["req"]
        res = p["result"]
        if self.waiting:
            self._dispatch(self.waiting.pop(0), t)
        failed = isinstance(res, Exception) or res.error is not None or not isinstance(res.raw, dict)
        raw = {} if failed else res.raw
        ops, rejected = parse_ops(raw.get("ops") if isinstance(raw.get("ops"), list) else [])
        view = None
        if isinstance(raw.get("view"), dict):
            try:
                view = ViewInfo(**{k: v for k, v in raw["view"].items() if v is not None or k == "persons_visible"})
            except Exception:  # noqa: BLE001
                view = None
        err = (str(res) if isinstance(res, Exception) else res.error) if failed else None
        patch = StatePatch(patch_id=f"patch_{req.seq:04d}", source="vlm", window_id=req.window_id, seq=req.seq,
                           slice_version=p["slice_version"], model=getattr(res, "model", None),
                           prompt_version=PROMPT_VERSION, t_event_ms=req.end_ms, view=view, ops=ops, rejected=rejected,
                           latency_ms=getattr(res, "latency_ms", None), failed=failed,
                           raw=(getattr(res, "text", "") or "")[:2000] if failed else None)
        self.vlm_calls.append({"window_id": req.window_id, "trigger": req.trigger, "t_request": req.t_request_ms,
                               "t_done": t, "latency_ms": patch.latency_ms, "failed": failed, "error": err,
                               "cached": getattr(res, "cached", False), "n_ops": len(ops), "n_rejected": len(rejected)})
        self.emit("patch", t, window_id=req.window_id, latency_ms=patch.latency_ms, failed=failed, n_ops=len(ops),
                  n_rejected=len(rejected), error=err)
        if failed:
            self._say(t, f"⚠ {req.window_id} 感知失败：{err}", "warn")
        for ready in self.sequencer.push(patch, t):
            self._commit(ready, t)

    # ================================================================== 状态提交（单写者）
    def _commit(self, patch: StatePatch, t: int) -> None:
        check = self.slices.pop(patch.seq, set()) if patch.source == "vlm" else set()
        if patch.source == "vlm" and not patch.failed:
            lag = t - patch.t_event_ms
            self.lag_ema = lag if self.lag_ema is None else 0.7 * self.lag_ema + 0.3 * lag
            self.commits.append((patch.slice_version or 0, patch.t_event_ms))
            self.commits = self.commits[-400:]
        ok_ops, rejected = self.validator.check(patch, self.store, check)
        delta, ctx = self.reducer.apply(ok_ops, patch, self.store, t)
        self.log.append("patch_failed" if patch.failed else "patch", patch.patch_id, patch.t_event_ms, t,
                        {**patch.model_dump(), "rejected": rejected, "ignored": ctx.ignored}, causation_id=patch.window_id)
        if rejected:
            self.log.append("patch_rejected", f"{patch.patch_id}_rej", patch.t_event_ms, t, rejected, patch.patch_id)
        for c in ctx.created:
            self.emit("entity", t, **c)
        edges: list[NeedEdge] = []
        if delta is not None:
            self._on_delta(delta)
            if self.pa is not None:
                self.pa.observe_delta(delta, t)
            edges += self.agenda.on_delta(delta, self.store, t)
        for op in ctx.other_ops:
            if isinstance(op, NeedFeedbackOp):
                edges += self.agenda.on_feedback(op.need_id, op.kind, self.store, t)
            elif isinstance(op, NeedUpsertOp):
                edges += self.agenda.upsert_need(op.need, self.store, t, patch.patch_id)
        for said in ctx.said:
            self._say(t, f"🗣 {said.speaker or '?'}：{said.content}（intent={said.intent}）", "speech")
            self.emit("said", t, speaker=said.speaker, content=said.content, intent=said.intent)
            # 语音需求不在这里编译：VLM 的 intent 标签不可靠（实测把“别忘了带伞”标成 info），改为逐句扫描原始转写
        for e in edges:
            self._on_edge(e, t)
        if self.pa is not None and (delta is not None or edges):
            self.pa.refresh(t)

    def _on_delta(self, d: StateDelta) -> None:
        self.deltas.append(d)
        self.log.append("delta", d.delta_id, d.t_event_ms, d.t_avail_ms, d, d.caused_by)
        self.emit("delta", d.t_avail_ms, version=d.version_to, changes=[
            {"key": c.key, "old": render_value(c.old), "new": render_value(c.new), "status": c.new_status,
             "source": c.source, "reason": c.reason} for c in d.changes])
        for ch in d.changes:
            if ch.reason.startswith("tick"):
                continue
            src = "" if ch.source == ch.old_source else f" [{ch.source}]"
            self._say(d.t_avail_ms, f"v{d.version_to} {ch.key}: {render_value(ch.old)} → {render_value(ch.new)} "
                                    f"({ch.new_status}){src} ← {ch.reason}", "state")
        if self.store.version - self.last_snapshot_version >= self.cfg["state"]["snapshot_every_versions"]:
            self.log.snapshot(self.store.version, d.t_avail_ms, self.store.snapshot(), self._agenda_dump())
            self.last_snapshot_version = self.store.version

    def _agenda_dump(self) -> dict:
        return {nid: n.model_dump(exclude={"prefetch"}) for nid, n in self.agenda.needs.items()}

    def _ev_need_upsert(self, t: int, p: dict) -> None:
        patch = StatePatch(patch_id=self._id("patch_slow"), source="consolidator", t_event_ms=t,
                           ops=[NeedUpsertOp(need=p["spec"])])
        self._commit(patch, t)

    def _ev_tick(self, t: int, p: dict) -> None:
        delta = self.reducer.tick(self.store, t, watermark_ms=self._watermark)
        edges = []
        if delta is not None:
            self._on_delta(delta)
            if self.pa is not None:
                self.pa.observe_delta(delta, t)
            edges += self.agenda.on_delta(delta, self.store, t)
        edges += self.agenda.on_tick(self.store, t)
        for e in edges:
            self._on_edge(e, t)
        if self.pa is not None:
            self.pa.on_tick(t)
        for ready in self.sequencer.release(t):
            self._commit(ready, t)
        if self.consolidator.due(t):
            # 情节 = 状态变化 + 原始语句（转写在 t_avail 之后才算可用）
            speech = [{"t_ms": u.t_avail_ms, "speaker": u.person_id or u.voice_anchor, "content": u.text}
                      for u in self.utt_by_id.values()]
            ep, lat = self.consolidator.summarize(self.deltas, speech, t)
            if ep is not None:
                self._push(ep.t_avail_ms, "episode", {"ep": ep})
        busy = any(k != "tick" for _, _, k, _ in self.heap) or self.inflight
        if not self.source_done or busy:
            self._push(t + self.cfg["state"]["tick_ms"], "tick", {})

    def _watermark(self, support_version: int) -> int | None:
        ts = [te for sv, te in self.commits if sv >= support_version]
        return max(ts) if ts else None

    def _ev_episode(self, t: int, p: dict) -> None:
        ep = p["ep"]
        self.episodes.add(ep)
        self.log.append("episode", ep.episode_id, ep.t_end_ms, t, ep)
        self._say(t, f"📚 {ep.episode_id} 写入长期记忆（{fmt_ms(ep.t_start_ms)}–{fmt_ms(ep.t_end_ms)}）", "slow")
        self.emit("episode", t, episode_id=ep.episode_id, text=ep.text)

    # ================================================================== 议程 / 调度（M6）
    def _recent_speech(self, t: int) -> list[str]:
        us = sorted((u for u in self.utt_by_id.values() if u.t_avail_ms <= t), key=lambda u: u.end_ms)[-6:]
        return [f"{fmt_ms(u.end_ms)} {u.person_id or u.voice_anchor or '?'}: {u.text}" for u in us]

    def _fire_reason(self, e: NeedEdge) -> str | None:
        d = next((d for d in reversed(self.deltas) if d.delta_id == e.caused_by), None)
        if d is None:
            return None
        need = self.agenda.needs[e.need_id]
        keys = set(need.deps) | set(e.dep_revs)
        parts = [f"{c.key}: {render_value(c.old)} → {render_value(c.new)}" for c in d.changes if c.key in keys]
        return "; ".join(parts) or None

    def _on_edge(self, e: NeedEdge, t: int) -> None:
        self.edges.append(e)
        self.log.append("need_edge", e.edge_id, t, t, e, e.caused_by)
        need = self.agenda.needs[e.need_id]
        self._say(t, f"⚑ {need.need_id}({need.kind} {need.bindings}) → {e.edge}", "agenda")
        self.emit("need", t, need_id=need.need_id, kind=need.kind, edge=e.edge, phase=need.phase,
                  bindings=need.bindings, summary=need.meta.get("summary"), topic=need.meta.get("topic"))
        if self.pa is not None:  # 新路径：需求只是机会的来源之一，是否开口由价值门控决定
            self.pa.on_need_edge(e, need, t)
            return
        rc = self.cfg["response"]
        if e.edge in ("armed", "rearmed"):
            self.gate.set_focus(need.need_id, list(need.bindings.values()))
            history = self.episodes.history(list(need.bindings.values()), before_ms=t, k=3)
            ctx = build_context(need, self.store, self.agenda, self.deltas, history, t, self._id("ctx"),
                                self.cfg["session"]["role"], self.permissions, speech=self._recent_speech(t))
            need.prefetch = {"history": history, "ctx": ctx, "draft": None}
            if rc["strategy"] == "prefetch_llm":
                mode = ctx["need"]["mode_cap"]
                gen = need.gen
                self._call("draft", t, lambda: self.drafter.draft(ctx, mode, self._id("dr"), origin="prefetch"),
                           {"need_id": need.need_id, "gen": gen, "purpose": "prefetch"}, latency_of=lambda r: r[1])
        elif e.edge == "disarmed":
            self.gate.clear_focus(need.need_id)
            need.prefetch, need.gen = None, need.gen + 1
        elif e.edge == "fired":
            self.agenda.mark_deciding(need, t)
            self.gate.set_focus(need.need_id, list(need.bindings.values()))
            history = (need.prefetch or {}).get("history") or self.episodes.history(list(need.bindings.values()), t, 3)
            ctx = build_context(need, self.store, self.agenda, self.deltas, history, t, self._id("ctx"),
                                self.cfg["session"]["role"], self.permissions, fire_reason=self._fire_reason(e),
                                speech=self._recent_speech(t))
            need.prefetch = {**(need.prefetch or {}), "ctx": ctx, "history": history}
            gen = need.gen
            self.log.append("decision_request", f"req_{ctx['context_id']}", t, t, ctx, e.edge_id)
            self._call("decision", t, lambda: self.decider.decide(ctx),
                       {"need_id": need.need_id, "gen": gen, "ctx": ctx},
                       latency_of=lambda r: getattr(r, "latency_ms", 0))
        elif e.edge in ("resolved", "dismissed", "expired"):
            self.gate.clear_focus(need.need_id)
            need.prefetch = None

    def _ev_draft(self, t: int, p: dict) -> None:
        need = self.agenda.needs[p["need_id"]]
        res = p["result"]
        if isinstance(res, Exception) or need.gen != p["gen"]:
            return
        draft = res[0]
        self.log.append("draft", draft.draft_id, p["t_start"], t, draft, need.need_id)
        if p["purpose"] == "prefetch":
            if need.prefetch is not None:
                need.prefetch["draft"] = draft
                self._say(t, f"✎ 预生成草稿 {draft.draft_id}（{draft.mode}）：{draft.text}", "response")
                self.emit("draft", t, need_id=need.need_id, mode=draft.mode, text=draft.text, latency_ms=t - p["t_start"])
        elif need.phase == "deciding" and not p.get("superseded"):
            self._deliver(need, p["decision"], draft, t)

    def _ev_draft_timeout(self, t: int, p: dict) -> None:
        need = self.agenda.needs[p["need_id"]]
        if need.phase == "deciding" and need.gen == p["gen"] and p["decision_id"] == self.pending_dec.get(need.need_id):
            d, _ = self.template.draft(p["ctx"], p["mode"], self._id("dr"), origin="template_fallback")
            self._deliver(need, p["decision"], d, t)

    def _ev_decision(self, t: int, p: dict) -> None:
        need = self.agenda.needs[p["need_id"]]
        ctx = p["ctx"]
        res = p["result"]
        if isinstance(res, Exception):
            res = Decision(decision_id=f"dec_{ctx['context_id'].split('_', 1)[1]}", context_id=ctx["context_id"],
                           need_id=need.need_id, model=getattr(self.decider, "name", "?"), status="unavailable",
                           reason=str(res)[:200])
        dec = apply_rules(res, ctx["need"]["mode_cap"], self.permissions, self.cfg["decision"]["warranted_threshold"])
        dec = dec.model_copy(update={"t_request_ms": p["t_start"], "t_avail_ms": t})
        self.decisions.append(dec)
        self.log.append("decision", dec.decision_id, p["t_start"], t, dec, ctx["context_id"])
        self.emit("decision", t, need_id=need.need_id, kind=need.kind, summary=need.meta.get("summary"),
                  bindings=need.bindings, warranted=dec.warranted, mode=dec.mode, applied=dec.applied_mode,
                  reason=dec.reason, notes=dec.rule_notes, latency_ms=t - p["t_start"], model=dec.model)
        w = "—" if dec.warranted is None else f"{dec.warranted:.2f}"
        self._say(t, f"⚖ {self.decider.name}: warranted={w} mode={dec.mode} → {dec.applied_mode}"
                     + (f"（{'; '.join(dec.rule_notes)}）" if dec.rule_notes else ""), "decision")
        if need.gen != p["gen"] or need.phase != "deciding":
            self.log.append("decision_discarded", f"{dec.decision_id}_x", t, t, {"phase": need.phase}, dec.decision_id)
            return
        mode = dec.applied_mode
        if mode == "DECISION_UNAVAILABLE":
            if self.cfg["decision"].get("on_unavailable") == "rule_ask" and ctx["need"]["mode_cap"] in ("ASK", "REMIND"):
                mode = "ASK"
            else:
                self.agenda.after_wait(need, t)
                return
        if mode == "WAIT":
            self.agenda.after_wait(need, t)
            return
        rc = self.cfg["response"]
        draft = (need.prefetch or {}).get("draft")
        if draft is not None and draft.mode == mode and self._claims_fresh(draft):
            self._deliver(need, dec, draft, t)
            return
        if rc["strategy"] == "template_first" or isinstance(self.drafter, TemplateDrafter):
            d, _ = self.template.draft(ctx, mode, self._id("dr"))
            self._deliver(need, dec, d, t)
            return
        self.pending_dec[need.need_id] = dec.decision_id
        gen = need.gen
        self._push(t + rc["ttft_fallback_ms"], "draft_timeout",
                   {"need_id": need.need_id, "gen": gen, "decision": dec, "decision_id": dec.decision_id,
                    "ctx": ctx, "mode": mode})
        self._call("draft", t, lambda: self.drafter.draft(ctx, mode, self._id("dr")),
                   {"need_id": need.need_id, "gen": gen, "purpose": "delivery", "decision": dec},
                   latency_of=lambda r: r[1])

    def _ev_deliver_retry(self, t: int, p: dict) -> None:
        need = self.agenda.needs[p["need_id"]]
        if need.gen != p["gen"] or need.phase != "deciding":
            return
        self._retrying = True
        try:
            self._deliver(need, p["decision"], p["draft"], t)
        finally:
            self._retrying = False

    def _claims_fresh(self, draft) -> bool:
        for c in draft.claims:
            b = self.store.beliefs.get(c.key)
            if b is None or b.rev != draft.dep_revs.get(c.key, -1):
                return False
        return True

    def _deliver(self, need, dec: Decision, draft, t: int) -> None:
        verdict, reason, checks = self.policy.check(need, draft, self.store, self.agenda, t)
        if verdict == "downgrade":
            draft, _ = self.template.draft(need.prefetch["ctx"], "ASK", self._id("dr"), origin="template_fallback")
            verdict, reason, checks = self.policy.check(need, draft, self.store, self.agenda, t)
        if verdict == "block" and reason == "global_rate_limit" and not getattr(self, "_retrying", False):
            when = self.policy.last_delivery_ms + self.policy.global_min_gap_ms + 10
            self.log.append("deferred", self._id("dfr"), t, t, {"need_id": need.need_id, "until_ms": when}, dec.decision_id)
            self._say(t, f"⏳ 限频：{need.need_id} 的消息延后到 {fmt_ms(when)} 复核发送", "policy")
            self.emit("deferred", t, need_id=need.need_id, until_ms=when)
            self._push(when, "deliver_retry", {"need_id": need.need_id, "gen": need.gen, "decision": dec, "draft": draft})
            return
        if verdict != "pass":
            self.blocked.append({"need_id": need.need_id, "decision_id": dec.decision_id, "reason": reason, "t_ms": t})
            self.log.append("blocked", self._id("blk"), t, t, {"need_id": need.need_id, "reason": reason, "checks": checks},
                            dec.decision_id)
            self._say(t, f"⛔ Policy Gate 拦截：{reason}", "policy")
            self.emit("blocked", t, need_id=need.need_id, reason=reason)
            if need.phase == "deciding":
                self.agenda.after_wait(need, t)
            return
        fire_t = next((e.t_ms for e in reversed(self.edges) if e.need_id == need.need_id and e.edge == "fired"), t)
        dl = Delivery(delivery_id=self._id("dl"), need_id=need.need_id, kind=need.kind, decision_id=dec.decision_id,
                      draft_id=draft.draft_id, mode=draft.mode, text=draft.text, t_first_visible_ms=t, fire_t_ms=fire_t,
                      checks=checks)
        self.policy.last_delivery_ms = t
        self.agenda.after_delivery(need, t)
        self.pending_dec[need.need_id] = None
        self.deliveries.append(dl)
        self.log.append("delivery", dl.delivery_id, t, t, dl, dec.decision_id)
        self._say(t, f"💬 [{dl.mode}] {dl.text}", "delivery")
        self.emit("delivery", t, delivery_id=dl.delivery_id, need_id=need.need_id, kind=need.kind, mode=dl.mode,
                  text=dl.text, summary=need.meta.get("summary"), warranted=dec.warranted, reason=dec.reason,
                  fire_t=fire_t)

    # ================================================================== 反馈 / 探针
    def _ev_feedback(self, t: int, fb: dict) -> None:
        if self.pa is not None and (fb.get("delivery_id") or fb.get("opp_key")):
            nid = self.pa.on_feedback(fb, t)
            self._say(t, f"👆 用户反馈 {fb['kind']}（{fb.get('delivery_id') or fb.get('opp_key')}）", "feedback")
            self.emit("feedback", t, delivery_id=fb.get("delivery_id"), need_id=nid, fb=fb["kind"])
            if nid:
                self._commit(StatePatch(patch_id=self._id("patch_user"), source="user", t_event_ms=t,
                                        ops=[NeedFeedbackOp(need_id=nid, kind=fb["kind"])]), t)
            return
        nid = fb.get("need_id")
        if nid is None:
            dl = next((d for d in reversed(self.deliveries) if d.kind == fb.get("need_kind", d.kind)), None)
            nid = dl.need_id if dl else None
        if nid is None:
            return
        ops: list[Any] = [NeedFeedbackOp(need_id=nid, kind=fb["kind"])]
        if fb.get("confirm"):
            ops.insert(0, ConfirmOp(key=fb["confirm"]["key"], value=fb["confirm"]["value"]))
        self._say(t, f"👆 用户反馈 {fb['kind']}（{nid}）", "feedback")
        self.emit("feedback", t, need_id=nid, fb=fb["kind"])
        self._commit(StatePatch(patch_id=self._id("patch_user"), source="user", t_event_ms=t, ops=ops), t)

    # ---------------- 主动决策核心的异步结果
    def _ev_pa_prob(self, t: int, p: dict) -> None:
        self.pa.on_prob(t, p)

    def _ev_pa_draft(self, t: int, p: dict) -> None:
        self.pa.on_draft(t, p)

    def _ev_pa_open(self, t: int, p: dict) -> None:
        self.pa.on_open(t, p)

    def _ev_pa_tts(self, t: int, p: dict) -> None:
        self.pa.on_tts(t, p)

    def _ev_pa_speak_timeout(self, t: int, p: dict) -> None:
        self.pa.on_speak_timeout(t, p)

    def _ev_probe(self, t: int, p: dict) -> None:
        if self.on_probe:
            self.on_probe(p["probe_id"], self.store.copy(), self.episodes.all_before(t), t)

    # ================================================================== 收尾
    def _finish(self) -> dict:
        if self.pool:
            self.pool.shutdown(wait=False, cancel_futures=True)
        self.log.snapshot(self.store.version, self.t_now, self.store.snapshot(), self._agenda_dump())
        lat = sorted(c["latency_ms"] for c in self.vlm_calls if c["latency_ms"] is not None and not c["failed"])
        pct = lambda q: lat[min(len(lat) - 1, int(q * len(lat)))] if lat else None  # noqa: E731
        summary = {
            "session_id": self.session_id, "mode": self.mode, "t_end_ms": self.t_now,
            "state_version": self.store.version, "n_entities": len(self.store.entities),
            "vlm": {"calls": len(self.vlm_calls), "failed": sum(c["failed"] for c in self.vlm_calls),
                    "cached": sum(c["cached"] for c in self.vlm_calls), "p50_ms": pct(0.5), "p95_ms": pct(0.95),
                    "ops": sum(c["n_ops"] for c in self.vlm_calls), "rejected": sum(c["n_rejected"] for c in self.vlm_calls)},
            "decisions": [d.model_dump() for d in self.decisions],
            "deliveries": self.pa.deliveries if self.pa is not None else [d.model_dump() for d in self.deliveries],
            "blocked": self.blocked,
            "edges": [e.model_dump(exclude={"dep_revs"}) for e in self.edges],
            "final_state": self.store.describe(),
        }
        if self.pa is not None:
            summary["proactive"] = self.pa.summary()
            self.pa.close()
        (self.run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str),
                                                   encoding="utf-8")
        with open(self.run_dir / "timeline.jsonl", "w", encoding="utf-8") as f:
            for row in self.timeline:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.emit("end", self.t_now, vlm=summary["vlm"], n_deliveries=len(summary["deliveries"]),
                  n_decisions=len(self.decisions), stopped=self.stop_event.is_set())
        self._ui_file.close()
        self.log.close()
        return summary
