"""主动决策核心：把 C6–C13、C17 串起来（设计说明 §4.2、§6）。

输入（都来自单写者引擎线程）：
  on_delta      状态变化 → GoalFilter / UserView 更新 → 生成机会 → 估值
  on_utterance  一句话 → 目标证据、可打扰度、认知（谁听到了）、“在找东西” → 估值
  on_need_edge  议程需求（来源①②）的边沿 → 同步成机会；不再直接调用决策模型
  on_tick       每秒：证据衰减、截止风险随时间变化、机会收尾、开放发现
输出：
  value     每个机会的 p q L H、各方式的 U、V(等)、决定（沉默 / 再等等 / 提示音 / 询问 / 提醒）
  delivery  通过频率与负担检查后的主动消息；语音由豆包 TTS 合成（机会变热时就预先合成）
模型只回答概率问题（ProbModel / OpenProposer），开不开口由 ValueEstimator 的确定性规则决定。
"""
from __future__ import annotations

from collections import deque
from concurrent.futures import ThreadPoolExecutor

from ..opportunity import Generators, Opportunity, OpportunityTable
from ..opportunity.model import SOURCES
from ..perception.prompt import fmt_ms
from ..policy.burden import Burden
from ..response import OppTemplateDrafter
from ..response.drafter import zh
from ..state.goals import STAGES, GoalFilter
from ..state.people import ANY, display_name, person_ids, pkey, present_keys, utter_key
from ..state.receptivity import Receptivity
from ..state.userview import UserView
from ..storage.decision_log import DecisionLog
from ..value import MODE_ZH, MODES, ValueEstimator, load_costs

DEC_ZH = {"SILENT": "沉默", "WAIT": "再等等", **MODE_ZH}


class ProactiveCore:
    def __init__(self, cfg: dict, eng, prob=None, opener=None, drafter=None, tts=None):
        pc = cfg.get("proactive", {})
        self.pc, self.eng = pc, eng
        self.costs = load_costs(pc.get("costs"))
        self.est = ValueEstimator(self.costs, eng.permissions)
        self.goals = GoalFilter(pc.get("goal_half_life_s", 60))
        self.recept = Receptivity()
        self.uv = UserView()
        self.table = OpportunityTable()
        self.gen = Generators(self.table, int(pc.get("open_ttl_s", 600) * 1000))
        b = self.costs["burden"]
        self.burden = Burden(b["half_life_s"], b["k"], b["voice_weight"], b["ambient_weight"])
        self.dlog = DecisionLog(eng.log)
        self.prob, self.opener, self.tts = prob, opener, tts
        self.drafter = drafter or OppTemplateDrafter()
        self.template = OppTemplateDrafter()
        self.tts_pool = ThreadPoolExecutor(max_workers=2) if (tts is not None and eng.mode == "realtime") else None
        self.deliveries: list[dict] = []
        self.utts: deque = deque(maxlen=40)
        self.last_emit: dict[str, int] = {}
        self.last_tick_ms = -10**9
        self.last_open_ms = 0
        self.open_inflight = False
        self.open_sig = None
        self.people_sig = None
        self.last_people_ms = -10**9
        self.model_inflight = 0
        self.tts_ready: dict[str, str] = {}
        self.tts_wait: dict[str, list[str]] = {}
        self.tts_inflight: set[str] = set()
        self.n_values = 0

    # ================================================================== 输入
    @property
    def store(self):
        return self.eng.store

    def observe_delta(self, d, t: int) -> None:
        self.goals.observe_delta(d, self.store, t)
        self.uv.observe_delta(d, self.store, t)

    def refresh(self, t: int) -> None:
        self.goals.update(t)
        self._generate(t)
        self._evaluate(t)

    def on_utterance(self, u, t: int) -> None:
        key = utter_key(self.store, u.person_id, u.voice_anchor)
        self.goals.observe_utterance(key, u.text, t)
        self.recept.observe_utterance(key, t)
        self.uv.observe_utterance(key, u.text, self.store, t)
        self.utts.append((u.end_ms, key or "?", u.text))
        self._emit_opps(self.gen.search(key, u.text, self.store, t), t)
        self.goals.update(t)
        self._evaluate(t)

    def on_need_edge(self, e, need, t: int) -> None:
        if e.edge == "fired":  # 议程只负责“时机条件”，是否开口交给价值门控
            need.phase = "armed"

    def on_tick(self, t: int) -> None:
        if t - self.last_tick_ms < 1000:
            return
        self.last_tick_ms = t
        self.goals.update(t)
        self._generate(t)
        self._evaluate(t)
        self._maybe_open(t)
        self._emit_people(t)

    def on_feedback(self, fb: dict, t: int) -> str | None:
        """界面反馈：done（已处理）/ not_needed（不需要）/ wrong（判断错误）。返回关联的 need_id（如有）。"""
        did, key = fb.get("delivery_id"), fb.get("opp_key")
        rec = next((d for d in reversed(self.deliveries) if d["delivery_id"] == did), None) if did else None
        key = key or (rec or {}).get("opp_key")
        opp = self.table.get(key) if key else None
        if opp is None:
            return None
        kind = fb["kind"]
        opp.status = "resolved" if kind == "done" else "suppressed"
        opp.updated_ms = t
        if rec:
            rec["feedback"] = kind
            self.dlog.feedback(rec["delivery_id"], kind)
        self.eng.emit("opp", t, **opp.public())
        return opp.need_id

    # ================================================================== 生成与收尾
    def _generate(self, t: int) -> None:
        res = self.gen.from_needs(self.eng.agenda, self.store, t)
        res += self.gen.goal_items(self.store, self.goals, self.uv, t)
        res += self.gen.belief_gaps(self.store, self.uv, t)
        res += self.gen.forecast(self.store, t)
        self._emit_opps(res, t)
        for o in self.gen.settle(self.store, self.uv, t):
            self._closed(o, t)

    def _emit_opps(self, res, t: int) -> None:
        for opp, new in res:
            if new:
                self.eng.emit("opp", t, **opp.public())
                self.eng._say(t, f"◇ 新机会 [{SOURCES.get(opp.source, opp.source)}] {opp.problem}", "opp")
            elif opp.status != "active":
                self._closed(opp, t)

    def _closed(self, o: Opportunity, t: int) -> None:
        self.eng.gate.clear_focus(o.key)
        self.eng.emit("opp", t, **o.public())
        self.eng._say(t, f"◆ 机会{ {'resolved': '已解决', 'expired': '过期', 'suppressed': '已关闭'}.get(o.status, o.status) }：{o.problem}", "opp")

    # ================================================================== 估值与门控
    def _evaluate(self, t: int) -> None:
        mult = self.burden.multiplier(t)
        for opp in self.table.active():
            keys = [k for k in opp.addressee if k != ANY]
            _, stage = self.goals.leave(keys)
            asking = opp.source == "search" and t - opp.meta.get("asked_ms", -10**9) < 15_000
            hurried = self.goals.hurried(keys, t) or (opp.deadline == "goal:leave_home" and stage >= 2)
            sit = self.recept.situation(keys or [ANY], t, hurried=hurried, asking=asking)
            est = self.est.estimate(opp, goals=self.goals, agenda=self.eng.agenda, situation=sit, uv=self.uv,
                                    now_ms=t, burden_mult=mult, unavailable=self._unavailable(opp, t))
            prev, opp.last_est, opp.last_decision = opp.last_decision, est, est.decision
            hot = est.decision != "SILENT" or (est.u_best > -1.0 and est.H >= 0.25)
            if hot and opp.hot_since_ms is None:
                opp.hot_since_ms = t
                ents = [opp.item] if opp.item else []
                for k in keys:
                    ents += person_ids(self.store, k)
                self.eng.gate.set_focus(opp.key, [e for e in ents if e])
            elif not hot and opp.hot_since_ms is not None and est.u_best < -2.5:
                opp.hot_since_ms = None
                self.eng.gate.clear_focus(opp.key)
            changed = prev != est.decision
            if changed or (t - self.last_emit.get(opp.key, -10**9) >= 5000 and (hot or est.u_best > -1.5)):
                self.last_emit[opp.key] = t
                self.n_values += 1
                self.eng.emit("value", t, key=opp.key, problem=opp.problem, source=opp.source, **est.public())
            if changed:
                self.dlog.write(t, opp, est)
                if prev is not None or est.decision != "SILENT":
                    self.eng._say(t, f"⚖ {opp.problem} → {DEC_ZH[est.decision]}（p={est.p:.2f} q={est.q:.2f} H={est.H:.2f}；"
                                     f"{est.reason}）", "decision")
            if self.prob is not None:
                self._maybe_model(opp, est, hot, t)
            if hot:
                self._prefetch(opp, est, t)
            if est.decision in MODES:
                self._try_speak(opp, est, t)

    def _unavailable(self, opp: Opportunity, t: int) -> set[str]:
        rep, out = self.costs["repeat"], set()
        if opp.ambient_sent >= rep["max_ambient"]:
            out.add("AMBIENT")
        voice_gap = opp.last_voice_ms is not None and t - opp.last_voice_ms < rep["min_gap_same_s"] * 1000
        if opp.reminders >= rep["max_voice"] or voice_gap or (opp.source == "open" and opp.reminders >= 1):
            out |= {"ASK", "REMIND"}
        return out

    def _maybe_model(self, opp: Opportunity, est, hot: bool, t: int) -> None:
        if opp.model_inflight or self.model_inflight >= 2:
            return
        due = opp.model_ms is None or (hot and t - opp.model_ms > 20_000)
        if not due or (est.p < 0.08 and not hot):
            return
        opp.model_inflight = True
        self.model_inflight += 1
        ctx = self._ctx(opp, t, est)
        self.eng._call("pa_prob", t, lambda: self.prob.estimate(ctx), {"key": opp.key},
                       latency_of=lambda r: r.get("latency_ms", 0) if isinstance(r, dict) else 0)

    def on_prob(self, t: int, p: dict) -> None:
        self.model_inflight = max(0, self.model_inflight - 1)
        opp = self.table.get(p["key"])
        if opp is None:
            return
        opp.model_inflight = False
        opp.model_ms = t
        res = p["result"]
        if isinstance(res, Exception) or not isinstance(res, dict) or res.get("error"):
            return
        opp.p_model, opp.q_model, opp.model_why = res["p_cond"], res["q"], res.get("why")
        self.eng.emit("opp", t, **opp.public())
        self.eng._say(t, f"🧠 模型估计 {opp.problem}：p_cond={opp.p_model:.2f} q={opp.q_model:.2f}（{opp.model_why}）", "decision")
        if opp.status == "active":
            self._evaluate(t)

    # ================================================================== 草稿与语音预取
    def _ctx(self, opp: Opportunity, t: int, est=None) -> dict:
        item = None
        if opp.item and opp.item in self.store.entities:
            e = self.store.entities[opp.item]
            b = self.store.beliefs.get(f"{opp.item}.location")
            place = ""
            if b is not None and isinstance(b.value, dict):
                ref = b.value.get("ref") or ""
                rel = b.value.get("rel")
                place = display_name(self.store, pkey(self.store, ref)) + "手里" if rel in ("HELD_BY", "WORN_BY") \
                    else zh(ref.rsplit("_", 1)[0])
            item = {"id": opp.item, "cls": e.cls, "name": zh(e.cls), "place": place,
                    "status": b.status if b else None, "source": b.source if b else None,
                    "age_s": round((t - b.last_support_ms) / 1000, 1) if b else None}
        names = [display_name(self.store, k) for k in opp.addressee] or (opp.meta.get("addressee_names") or [])
        lines = [ln for ln in self.store.describe()
                 if any(ln.startswith(x + " ") for x in ([opp.item] if opp.item else []) + sum((person_ids(self.store, k) for k in opp.addressee), []))]
        return {"t_ms": t, "now": fmt_ms(t),
                "opportunity": {"key": opp.key, "problem": opp.problem, "category": opp.category, "source": opp.source,
                                "deadline": opp.deadline, "addressee_names": names, "item": item, "topic": opp.topic,
                                "evidence": opp.evidence[:4], "reminders_sent": opp.reminders},
                "goals": {k: v for k, v in self.goals.snapshot().items() if k in opp.addressee or k == ANY},
                "situation": est.situation if est else None,
                "recent_speech": [f"{fmt_ms(te)} {k}: {txt}" for te, k, txt in list(self.utts)[-8:]],
                "state": lines[:12]}

    def _prefetch(self, opp: Opportunity, est, t: int) -> None:
        if not self.pc.get("prefetch", True) or opp.draft_inflight:
            return
        mode = "REMIND" if est.best == "REMIND" or est.decision == "WAIT" else est.best
        if mode == "AMBIENT":
            return
        d = opp.draft
        if d is not None and d.mode == mode and t - d.created_ms < 120_000:
            return
        opp.draft_inflight = True
        ctx = self._ctx(opp, t, est)
        self.eng._call("pa_draft", t, lambda: self.drafter.draft(ctx, mode, origin="prefetch"),
                       {"key": opp.key, "purpose": "prefetch"}, latency_of=lambda r: r[1] if isinstance(r, tuple) else 0)

    def on_draft(self, t: int, p: dict) -> None:
        opp = self.table.get(p["key"])
        if opp is None:
            return
        opp.draft_inflight = False
        res = p["result"]
        if isinstance(res, Exception):
            return
        d = res[0]
        d.created_ms = t
        opp.draft = d
        self.eng.emit("draft", t, key=opp.key, mode=d.mode, text=d.text, origin=d.origin, latency_ms=t - p["t_start"])
        if self.tts is not None and d.mode != "AMBIENT":
            self._tts(d.text, t)
        pend = opp.meta.pop("pending_speak", None)
        if pend and opp.status == "active":
            self._deliver(opp, pend, d if d.mode == pend.decision else self.template.draft(self._ctx(opp, t, pend), pend.decision)[0], t)

    def on_speak_timeout(self, t: int, p: dict) -> None:
        opp = self.table.get(p["key"])
        if opp is None or opp.status != "active":
            return
        pend = opp.meta.pop("pending_speak", None)
        if pend is not None:
            d, _ = self.template.draft(self._ctx(opp, t, pend), pend.decision, origin="template_fallback")
            self._deliver(opp, pend, d, t)

    # ================================================================== 投递
    def _try_speak(self, opp: Opportunity, est, t: int) -> None:
        mode, rep, rate = est.decision, self.costs["repeat"], self.costs["rate"]
        if opp.meta.get("pending_speak") is not None:
            return
        if opp.source == "open":
            if opp.p_model is None or opp.reminders >= 1:
                return  # 开放发现先由概率模型复核一次；同一条最多说一次
            last_open = max((d["t_ms"] for d in self.deliveries if d["source"] == "open"), default=None)
            if last_open is not None and t - last_open < int(self.pc.get("open_min_gap_s", 600) * 1000):
                return  # 开放发现全局每 10 分钟最多开口一次
        if mode == "AMBIENT" and opp.ambient_sent >= rep["max_ambient"]:
            return
        if mode != "AMBIENT" and opp.reminders >= rep["max_voice"]:
            return
        if mode == "AMBIENT":  # 提示音之后可以升级成语音；语音之后同一件事至少隔 min_gap_same_s
            if opp.last_delivery_ms is not None and t - opp.last_delivery_ms < rep["min_gap_same_s"] * 1000:
                return
        else:
            if opp.last_voice_ms is not None and t - opp.last_voice_ms < rep["min_gap_same_s"] * 1000:
                return
            if opp.last_delivery_ms is not None and t - opp.last_delivery_ms < 8000:
                return
        wait = self.burden.wait_ms(mode, t, rate["voice_min_gap_s"] * 1000, rate["ambient_min_gap_s"] * 1000)
        if wait > 0 and mode != "AMBIENT" and est.H >= 0.85 and est.u_best >= 1.0:
            # 截止在即、价值明显为正：可以插队，但离上一条语音至少 8 s（累积负担已经计入 C_int）
            wait = self.burden.wait_ms(mode, t, 8000, 0)
        if wait > 0:
            if opp.deferred_until_ms is None or opp.deferred_until_ms < t:
                opp.deferred_until_ms = t + wait
                self.eng.emit("deferred", t, key=opp.key, until_ms=t + wait, mode=mode)
                self.eng._say(t, f"⏳ 频率限制：{opp.problem} 的{MODE_ZH[mode]}最早 {fmt_ms(t + wait)} 再看", "policy")
            return
        d = opp.draft
        if mode == "AMBIENT":
            d, _ = self.template.draft(self._ctx(opp, t, est), "AMBIENT")
        elif d is None or d.mode != mode or t - d.created_ms > 120_000:
            if self.drafter is self.template or isinstance(self.drafter, OppTemplateDrafter):
                d, _ = self.template.draft(self._ctx(opp, t, est), mode)
            else:
                opp.meta["pending_speak"] = est
                if not opp.draft_inflight:
                    opp.draft_inflight = True
                    ctx = self._ctx(opp, t, est)
                    self.eng._call("pa_draft", t, lambda: self.drafter.draft(ctx, mode, origin="on_demand"),
                                   {"key": opp.key, "purpose": "speak"}, latency_of=lambda r: r[1] if isinstance(r, tuple) else 0)
                self.eng._push(t + int(self.pc.get("draft_timeout_ms", 2500)), "pa_speak_timeout", {"key": opp.key})
                return
        self._deliver(opp, est, d, t)

    def _deliver(self, opp: Opportunity, est, d, t: int) -> None:
        mode = est.decision
        max_age = int(self.eng.cfg["policy"]["certain_max_age_s"] * 1000)
        for c in d.claims:
            if c.get("stated_as") != "certain" or c.get("key") not in self.store.beliefs:
                continue  # 只核对状态里有的事实（“泡面好了”这类状态外的话无从核对，不因此降级）
            b = self.store.beliefs.get(c.get("key"))
            if not (b and b.status == "holds" and b.source in ("seen", "user_confirmed") and t - b.last_support_ms <= max_age):
                d, _ = self.template.draft(self._ctx(opp, t, est), "ASK" if mode == "REMIND" else mode, origin="template_fallback")
                mode = d.mode
                break
        did = self.eng._id("dl")
        audio = self._audio_url(d.text) if mode != "AMBIENT" else None
        rec = {"delivery_id": did, "t_ms": t, "opp_key": opp.key, "source": opp.source,
               "source_label": SOURCES.get(opp.source, opp.source), "category": opp.category, "mode": mode,
               "text": d.text, "problem": opp.problem, "addressee": [display_name(self.store, k) for k in opp.addressee],
               "est": est.public(), "draft_origin": d.origin, "hot_since_ms": opp.hot_since_ms,
               "need_id": opp.need_id, "reminder_no": opp.reminders + (0 if mode == "AMBIENT" else 1)}
        self.deliveries.append(rec)
        opp.last_delivery_ms, opp.draft, opp.deferred_until_ms = t, None, None
        if mode == "AMBIENT":
            opp.ambient_sent += 1
        else:
            opp.reminders += 1
            opp.last_voice_ms = t
        self.burden.on_delivery(t, mode)
        self.dlog.write(t, opp, est, delivered=True, delivery_id=did, text=d.text)
        self.eng.log.append("delivery", did, t, t, rec)
        pending = self.tts is not None and mode != "AMBIENT" and audio is None
        self.eng.emit("delivery", t, **rec, audio=audio, tts_pending=pending)
        self.eng._say(t, f"💬 [{MODE_ZH[mode]}] {d.text}（{opp.problem}；{est.reason}）", "delivery")
        if opp.need_id and opp.need_id in self.eng.agenda.needs:
            self.eng.agenda.after_delivery(self.eng.agenda.needs[opp.need_id], t)
        if pending:
            self._tts(d.text, t, did)

    # ================================================================== 语音（豆包 TTS）
    def _audio_url(self, text: str) -> str | None:
        if self.tts is None:
            return None
        p = self.tts.cached(text)
        return f"/tts/{p.name}" if p else None

    def _tts(self, text: str, t: int, delivery_id: str | None = None) -> None:
        if self.tts is None or self.tts_pool is None:
            return
        h = self.tts.path_for(text).stem
        if delivery_id:
            self.tts_wait.setdefault(h, []).append(delivery_id)
        if h in self.tts_ready or self.tts.cached(text):
            self.tts_ready[h] = f"/tts/{h}.mp3"
            self._tts_notify(h, t, 0)
            return
        if h in self.tts_inflight:
            return  # 已在合成，完成后统一通知
        self.tts_inflight.add(h)
        self.eng._call("pa_tts", t, lambda: self.tts.synthesize(text), {"hash": h, "text": text},
                       latency_of=lambda r: 0, pool=self.tts_pool)

    def on_tts(self, t: int, p: dict) -> None:
        res, h = p["result"], p["hash"]
        self.tts_inflight.discard(h)
        if isinstance(res, Exception) or not isinstance(res, dict) or res.get("error"):
            err = str(res) if isinstance(res, Exception) else (res or {}).get("error")
            for did in self.tts_wait.pop(h, []):
                self.eng.emit("tts", t, delivery_id=did, error=err)
            self.eng._say(t, f"⚠ 语音合成失败：{err}", "warn")
            return
        self.tts_ready[h] = f"/tts/{h}.mp3"
        self._tts_notify(h, t, res.get("latency_ms", 0))

    def _tts_notify(self, h: str, t: int, latency_ms: int) -> None:
        for did in self.tts_wait.pop(h, []):
            self.eng.emit("tts", t, delivery_id=did, url=self.tts_ready[h], latency_ms=latency_ms)

    # ================================================================== 开放发现（来源 ✦）
    def _maybe_open(self, t: int) -> None:
        if self.opener is None or self.open_inflight:
            return
        if t - self.last_open_ms < int(self.pc.get("open_every_s", 30) * 1000):
            return
        sig = (len(self.utts) and self.utts[-1][0], self.store.version)
        if sig == self.open_sig:
            return
        self.open_sig, self.last_open_ms, self.open_inflight = sig, t, True
        ctx = {"now": fmt_ms(t), "state": self.store.describe()[:40],
               "recent_speech": [f"{fmt_ms(te)} {k}: {txt}" for te, k, txt in list(self.utts)[-20:]],
               "people_goals": self.goals.snapshot(),
               "active": [{"key": o.key, "problem": o.problem} for o in self.table.active()][:12]}
        self.eng._call("pa_open", t, lambda: self.opener.propose(ctx), {},
                       latency_of=lambda r: r.get("latency_ms", 0) if isinstance(r, dict) else 0)

    def on_open(self, t: int, p: dict) -> None:
        self.open_inflight = False
        res = p["result"]
        if isinstance(res, Exception) or not isinstance(res, dict):
            return
        names = {e.anchors.get("voice"): pkey(self.store, eid) for eid, e in self.store.entities.items() if e.anchors.get("voice")}
        recent = " ".join(txt for _, _, txt in self.utts)
        taken = set()  # 已有机会涉及的物品 / 话题：开放发现换个说法提同一件事不算新机会
        for a in self.table.active():
            for w in (a.topic, self.store.entities[a.item].cls if a.item in self.store.entities else None):
                if w:
                    taken |= {w.lower(), zh(w)}
        out = []
        for o in res.get("opportunities", []):
            if any(w and len(w) >= 1 and w in o["problem"] for w in taken if not w.isascii()) or \
                    any(w in o["problem"].lower() for w in taken if w.isascii() and len(w) > 2):
                continue
            addr = []
            for a in o["addressee"]:
                k = names.get(a) or (pkey(self.store, a) if a in self.store.entities else a)
                if k and k not in addr:
                    addr.append(k)
            grounded = any(ev in self.store.beliefs or (len(ev) > 6 and ev.strip("“”\"' ") in recent) for ev in o["evidence"])
            dl = o["deadline"] if o["deadline"] in ("goal:leave_home", "asap", "none") else "none"
            cat = o["category"] if o["category"] in self.costs["loss"] else "info"
            opp = Opportunity(key=f"open:{o['key']}", source="open", addressee=addr or [ANY], problem=o["problem"],
                              category=cat, deadline=dl, evidence=o["evidence"],
                              factors={"发现时估计": o["p"] if grounded else o["p"] * 0.6}, q_model=o["q"],
                              meta={"grounded": grounded})
            out.append(self.table.upsert(opp, t))
        self._emit_opps(out, t)
        if out:
            self._evaluate(t)

    # ================================================================== 界面：人物面板
    def _emit_people(self, t: int) -> None:
        if t - self.last_people_ms < 2000:
            return
        snap = self.goals.snapshot()
        present = present_keys(self.store, t)
        people = {}
        for k in set(snap) | set(present):
            if k == ANY:
                continue
            sit = self.recept.situation([k], t, hurried=self.goals.hurried([k], t))
            people[k] = {"name": display_name(self.store, k), "present": k in present, **snap.get(k, {"p": 0.12, "stage": 0,
                         "stage_label": "坐着", "cues": []}), "situation": sit.label}
        gaps = [{"person": g.person, "key": g.key} for g in self.uv.gaps(self.store, t)]
        sig = (tuple(sorted((k, round(v["p"], 2), v["stage"], v["situation"], v["present"]) for k, v in people.items())), len(gaps))
        if sig == self.people_sig and t - self.last_people_ms < 10_000:
            return
        self.people_sig, self.last_people_ms = sig, t
        hp, hs = self.goals.household()
        household = {"p": round(hp, 3), "stage": hs, "stage_label": STAGES[hs], "cues": snap.get(ANY, {}).get("cues", [])}
        self.eng.emit("people", t, people=people, household=household, gaps=gaps,
                      burden=round(self.burden.value(t), 2))

    # ================================================================== 收尾
    def summary(self) -> dict:
        opps = list(self.table.opps.values())
        return {"opportunities": [o.public() | {"last_decision": o.last_decision} for o in opps],
                "deliveries": self.deliveries, "n_value_events": self.n_values,
                "by_source": {s: sum(o.source == s for o in opps) for s in SOURCES}}

    def close(self) -> None:
        if self.tts_pool is not None:
            self.tts_pool.shutdown(wait=False, cancel_futures=True)
