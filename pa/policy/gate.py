"""M9 Policy Gate：发送前的最后复核（设计文档 §3 M9）。纯程序，不调模型。"""
from __future__ import annotations

from ..agenda import dsl
from ..agenda.trigger import TriggerEngine
from ..schemas import MODE_ORDER, Need, ResponseDraft
from ..state.store import StateStore


class PolicyGate:
    def __init__(self, global_min_gap_ms: int = 30000, certain_max_age_ms: int = 10000,
                 permissions: dict | None = None, fire_grace_ms: int = 10000):
        self.fire_grace_ms = fire_grace_ms  # fire 由瞬时信号（如 moving_to_exit）触发，最近 N ms 内为真即可
        self.global_min_gap_ms = global_min_gap_ms
        self.certain_max_age_ms = certain_max_age_ms
        self.permissions = permissions or {"may_ask": True, "may_remind": True}
        self.last_delivery_ms: int | None = None

    def check(self, need: Need, draft: ResponseDraft, store: StateStore, agenda: TriggerEngine,
              now_ms: int) -> tuple[str, str, dict]:
        """返回 (结论, 原因, 检查明细)。结论 ∈ pass | block | downgrade（需要改用 ASK 模板）。"""
        checks: dict = {}
        if need.phase != "deciding":
            return "block", f"phase={need.phase}", checks
        # 与触发引擎同样的语义：空的 fire / resolve 视为“永不成立”
        fire = bool(need.fire) and dsl.evaluate(need.fire, store, need.bindings, now_ms)
        res = bool(need.resolve) and dsl.evaluate(need.resolve, store, need.bindings, now_ms)
        checks.update(fire=fire, resolve=res)
        if res:
            return "block", "already_resolved", checks
        if not fire:
            last = need.fire_last_true_ms
            if last is None or now_ms - last > self.fire_grace_ms:
                return "block", "fire_no_longer_true", checks
            checks["fire"] = f"true {now_ms - last} ms ago (grace {self.fire_grace_ms} ms)"
        cur = agenda.dep_revs(need, store)
        changed = sorted(k for k in set(cur) | set(draft.dep_revs) if cur.get(k) != draft.dep_revs.get(k))
        checks["dep_revs"] = "unchanged" if not changed else {"changed": changed}
        cap = agenda.mode_cap(need, store, now_ms)
        checks["mode_cap"] = cap
        if MODE_ORDER[draft.mode] > MODE_ORDER[cap]:
            return "downgrade", f"mode {draft.mode} > cap {cap}", checks
        for c in draft.claims:
            if c.stated_as != "certain":
                continue
            b = store.beliefs.get(c.key)
            ok = (b is not None and b.status == "holds" and b.source in ("seen", "user_confirmed")
                  and now_ms - b.last_support_ms <= self.certain_max_age_ms)
            if not ok:
                checks["claims"] = f"overstated:{c.key}"
                return "downgrade", "claim_overstated", checks
        checks["claims"] = "ok"
        perm = {"REMIND": "may_remind", "ASK": "may_ask"}[draft.mode]
        if not self.permissions.get(perm, True):
            return "block", f"no_permission:{perm}", checks
        if self.last_delivery_ms is not None and now_ms - self.last_delivery_ms < self.global_min_gap_ms:
            return "block", "global_rate_limit", checks
        if need.reminders_sent >= need.max_reminders:
            return "block", "max_reminders", checks
        checks["cooldown"] = "ok"
        return "pass", "", checks
