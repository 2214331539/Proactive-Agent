"""C10 价值估计 + C11 价值门控（设计说明 §3.2、§5.4）。

U(m)  = p·(1−q)·η_m·L − (1−p)·C_FA(m) − C_int(m)
V(等) = (1−H) · max(0, 等一会儿之后最好的开口价值)
决定：U(m*) ≤ 0 → 沉默；U(m*) ≤ V(等) → 再等等；否则用 m* 开口。

p 由结构化因子相乘（生成器给出的条件概率 × GoalFilter 的出门概率 / 时机条件），有模型估计时与模型的条件概率
取几何平均；q 由情境先验（Receptivity）与模型估计平均。所有中间量都进 ValueEstimate，写入决策日志。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..opportunity.model import Opportunity
from ..state.goals import STAGES
from ..state.people import ANY

MODES = ("AMBIENT", "ASK", "REMIND")
MODE_ZH = {"AMBIENT": "提示音", "ASK": "询问", "REMIND": "提醒"}
COSTS_PATH = Path(__file__).with_name("costs.yaml")


def load_costs(path: str | Path | None = None) -> dict:
    return yaml.safe_load(Path(path or COSTS_PATH).read_text(encoding="utf-8"))


@dataclass
class ValueEstimate:
    p: float
    q: float
    L: float
    H: float
    p_parts: dict[str, float]
    q_why: str
    situation: str
    stage: str | None
    eta: dict[str, float]
    c_int: dict[str, float]
    c_fa: dict[str, float]
    utility: dict[str, float]
    v_wait: float
    best: str
    decision: str  # SILENT | WAIT | AMBIENT | ASK | REMIND
    reason: str
    notes: list[str] = field(default_factory=list)

    @property
    def u_best(self) -> float:
        return self.utility[self.best]

    def public(self) -> dict:
        r = lambda x: round(float(x), 3)  # noqa: E731
        return {"p": r(self.p), "q": r(self.q), "L": self.L, "H": r(self.H), "p_parts": {k: r(v) for k, v in self.p_parts.items()},
                "q_why": self.q_why, "situation": self.situation, "stage": self.stage,
                "U": {m: r(u) for m, u in self.utility.items()}, "V": r(self.v_wait), "best": self.best,
                "decision": self.decision, "reason": self.reason, "notes": self.notes}


class ValueEstimator:
    def __init__(self, costs: dict | None = None, permissions: dict | None = None):
        self.c = costs or load_costs()
        self.permissions = permissions or {"may_ask": True, "may_remind": True}

    # ------------------------------------------------------------ p 与 H
    def _p_h(self, opp: Opportunity, goals, agenda, now_ms: int) -> tuple[float, dict, float, str | None, list[str]]:
        hz, notes = self.c["hazard"], []
        cond = math.prod(opp.factors.values()) if opp.factors else 0.5
        if opp.p_model is not None:
            cond = math.sqrt(max(cond, 1e-6) * opp.p_model) if opp.factors else opp.p_model
        parts = dict(opp.factors)
        if opp.p_model is not None:
            parts["模型估计"] = opp.p_model
        if opp.source == "open":  # 开放发现多是推测：打五折，并且要等模型单独估过概率才可能开口（见 core._try_speak）
            parts["开放发现折扣"] = 0.5
            cond *= 0.5
        stage = None
        dl = opp.deadline
        if dl == "goal:leave_home":
            keys = [k for k in opp.addressee if k != ANY]
            p_leave, st = goals.leave(keys)
            parts = {"会出门": p_leave, **parts}
            H, stage = hz["goal"][st], STAGES[st]
            p = p_leave * cond
        elif dl.startswith("time:"):
            due = int(dl.split(":", 1)[1])
            left = (due - now_ms) / 1000
            if left <= hz["time_window_s"]:
                H = 0.9
            else:
                H = 0.3 if left <= 90 else 0.05
                notes.append(f"距约定时间还有 {int(left)} s")
            p = cond
        elif dl == "trigger":
            need = agenda.needs.get(opp.need_id) if opp.need_id else None
            arm, fire, _ = agenda.last_eval.get(opp.need_id, (False, False, False)) if need else (False, False, False)
            fire_recent = bool(need and need.fire_last_true_ms is not None and now_ms - need.fire_last_true_ms <= 10_000)
            H = hz["trigger_fired"] if fire_recent else hz["trigger_armed"] if arm else hz["trigger_idle"]
            t = 1.0 if fire_recent else 0.6 if arm else 0.3
            parts = {"时机条件": t, **parts}
            p = t * cond
        else:
            H = hz.get(dl, hz["none"])
            p = cond
        return max(0.0, min(1.0, p)), parts, H, stage, notes

    # ------------------------------------------------------------ 主入口
    def estimate(self, opp: Opportunity, *, goals, agenda, situation, uv, now_ms: int, burden_mult: float = 1.0,
                 unavailable: set[str] | frozenset = frozenset()) -> ValueEstimate:
        """unavailable：此刻用不了的方式（提示音已经响过、语音已达上限或刚说过），门控只在剩下的方式里选。"""
        p, parts, H, stage, notes = self._p_h(opp, goals, agenda, now_ms)
        cq = self.c["q"]
        if opp.meta.get("to_assistant"):
            q_ctx, q_why = cq["to_assistant"], "请求是对助理说的"
        elif situation.hurried:
            q_ctx, q_why = cq["hurried"], "赶时间，不太会自己想起"
        else:
            q_ctx, q_why = cq["normal"], "一般情况"
        if opp.item and uv is not None:
            for k in opp.addressee:
                seen = uv.believes(k, f"{opp.item}.location")
                if seen and seen[2] == "told" and now_ms - seen[1] < 120_000:
                    q_ctx, q_why = max(q_ctx, cq["told_recently"]), "刚刚有人提到过"
        q = q_ctx if opp.q_model is None else (q_ctx + opp.q_model) / 2
        said = opp.meta.get("said_ms")
        if said is not None and not opp.meta.get("to_assistant") and opp.source == "request":
            fresh = 0.8 * 0.5 ** (max(0, now_ms - said) / 20_000)  # 刚刚有人当面说过：对方自己记得，立刻复述没有价值
            if fresh > q:
                q, q_why = fresh, f"{(now_ms - said) / 1000:.0f} s 前刚有人当面说过"
        if opp.last_voice_ms is not None and now_ms - opp.last_voice_ms < 120_000:
            q, q_why = max(q, 0.6), "刚提醒过"
        L = float(self.c["loss"].get(opp.category, self.c["loss"]["info"]))
        if opp.source == "open" and L > self.c["loss"]["essential"]:
            L, notes = self.c["loss"]["essential"], notes + ["开放发现是推测，损失最多按必需品计（真正的安全隐患由风险预测 / 模板负责）"]
        rep = self.c["repeat"]
        repeat_mult = rep["second_multiplier"] if opp.reminders >= 1 else 1.0
        eta, c_int, c_fa, U = {}, {}, {}, {}
        for m in self._modes():
            if m in unavailable:
                continue
            if m == "AMBIENT" and opp.source == "open":
                continue  # 开放发现多是推测：响一声不传达内容，只能用语音说清楚（门槛自然更高）
            mc = self.c["modes"][m]
            eta[m] = mc["eta"][opp.eta_kind]
            if m == "AMBIENT" and opp.last_voice_ms is not None and now_ms - opp.last_voice_ms < 120_000:
                eta[m] = 0.0  # 刚用语音说过，再响一声提示音没有增量
            c_int[m] = mc["c_int"][situation.key] * burden_mult * (repeat_mult if m != "AMBIENT" else 1.0)
            c_fa[m] = mc["c_fa"]
            U[m] = p * (1 - q) * eta[m] * L - (1 - p) * c_fa[m] - c_int[m]
        # 等一会儿：截止前还有时间时，再看一眼会更有把握，对方也可能空下来
        gain = self.c["wait"]["info_gain_goal" if opp.deadline == "goal:leave_home" else "info_gain_other"]
        p_later = p + (1 - p) * gain * (1 - H)
        later = max(p_later * (1 - q) * eta[m] * L - (1 - p_later) * c_fa[m]
                    - self.c["modes"][m]["c_int"]["normal"] * burden_mult * (repeat_mult if m != "AMBIENT" else 1.0)
                    for m in U)
        V = (1 - H) * max(0.0, later)
        if not U:  # 所有方式都用不了（已提醒到上限）
            return ValueEstimate(p=p, q=q, L=L, H=H, p_parts=parts, q_why=q_why, situation=situation.label, stage=stage,
                                 eta={}, c_int={}, c_fa={}, utility={"REMIND": -9.0}, v_wait=V, best="REMIND",
                                 decision="SILENT", reason="已经提醒过，不再重复", notes=notes)
        best = max(U, key=U.get)
        ub = U[best]
        not_yet = opp.deadline.startswith("time:") and H < 0.9
        if ub <= 0:
            decision, reason = "SILENT", f"最高的{MODE_ZH[best]}也只有 {ub:+.2f}，不值得开口"
        elif not_yet:
            decision, reason = "WAIT", f"{MODE_ZH[best]} {ub:+.2f}，但还没到约定时间"
        elif ub <= V + self.c.get("min_margin", 0.0) or ub <= self.c.get("min_margin", 0.0):
            decision, reason = "WAIT", (f"{MODE_ZH[best]} {ub:+.2f}，低于等待价值 {V:.2f}" if ub <= V
                                        else f"{MODE_ZH[best]} {ub:+.2f}，只比等待价值 {V:.2f} 高一点，再看看")
        else:
            decision, reason = best, f"{MODE_ZH[best]} {ub:+.2f} 最高，且高于等待价值 {V:.2f}"
        return ValueEstimate(p=p, q=q, L=L, H=H, p_parts=parts, q_why=q_why, situation=situation.label, stage=stage,
                             eta=eta, c_int=c_int, c_fa=c_fa, utility=U, v_wait=V, best=best, decision=decision,
                             reason=reason, notes=notes)

    def _modes(self) -> list[str]:
        out = ["AMBIENT"]
        if self.permissions.get("may_ask", True):
            out.append("ASK")
        if self.permissions.get("may_remind", True):
            out.append("REMIND")
        return out
