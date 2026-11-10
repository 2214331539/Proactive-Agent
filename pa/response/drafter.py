"""M8 Response：模板（离线 / 兜底）与 LLM 草稿（设计文档 §3 M8、§6.3）。"""
from __future__ import annotations

import json
from dataclasses import dataclass

from ..llm import ChatJSON
from ..schemas import Claim, ResponseDraft

ZH = {"phone": "手机", "keys": "钥匙", "key": "钥匙", "wallet": "钱包", "glasses": "眼镜", "badge": "工牌",
      "umbrella": "伞", "laptop": "笔记本电脑", "card": "卡", "purse": "钱包", "desk": "桌子", "table": "桌子",
      "counter": "台面", "shelf": "架子", "sofa": "沙发", "bed": "床", "chair": "椅子", "cabinet": "柜子",
      "stove": "炉灶", "iron": "熨斗", "oven": "烤箱", "heater": "取暖器", "kettle": "水壶", "tap": "水龙头",
      "faucet": "水龙头", "light": "灯", "backpack": "背包", "bag": "包", "drawer": "抽屉", "umbrellas": "伞"}


def zh(cls: str) -> str:
    return ZH.get(cls, cls)


def _names(ctx: dict) -> tuple[str, str, str]:
    descs = {b["key"].split(".")[0]: b["value"] for b in ctx["beliefs"] if b["key"].endswith(".desc")}
    item = ctx["need"]["bindings"].get("item", "")
    item_name = zh(descs.get(item, item).split(" ")[0])
    loc = next((b for b in ctx["beliefs"] if b["key"] == f"{item}.location"), None)
    place = ""
    if loc and loc["value"] not in ("?", None):
        ref = loc["value"].split(" ")[-1]
        place = zh(ref.rsplit("_", 1)[0])
    return item, item_name, place


class TemplateDrafter:
    def __init__(self):
        self.name = "template"

    def draft(self, ctx: dict, mode: str, draft_id: str, origin: str = "on_demand") -> tuple[ResponseDraft, int]:
        item, item_name, place = _names(ctx)
        kind = ctx["need"]["kind"]
        where = f"{place}上" if place else "原处"
        if ctx["need"].get("topic") and not item:  # 语音编译出的需求：物品不在状态里，按话题提醒
            name = zh(ctx["need"]["topic"])
            text = f"别忘了带{name}。" if mode == "REMIND" else f"要带上{name}吗？"
            return ResponseDraft(draft_id=draft_id, need_id=ctx["need_id"], mode=mode, text=text, claims=[],
                                 context_version=ctx["state_version"], dep_revs=ctx["dep_revs"], model=self.name,
                                 origin=origin, created_t_ms=ctx["t_ms"]), 0
        if kind == "appliance_left_on":
            text = f"{item_name}好像还开着，出门前要关掉吗？" if mode == "REMIND" else f"{item_name}是不是还开着？"
            key = f"{item}.state"
        else:
            text = f"你的{item_name}好像还在{where}，记得带上。" if mode == "REMIND" else f"{item_name}是不是还在{where}？出门前要带上吗？"
            key = f"{item}.location"
        claim = Claim(key=key, value=None, stated_as="probable" if mode == "REMIND" else "question")
        return ResponseDraft(draft_id=draft_id, need_id=ctx["need_id"], mode=mode, text=text, claims=[claim],
                             context_version=ctx["state_version"], dep_revs=ctx["dep_revs"], model=self.name,
                             origin=origin, created_t_ms=ctx["t_ms"]), 0


class LLMDrafter:
    SYSTEM = ("你为一个主动式助理写一句中文提醒或询问（不超过 30 个字）。输入是 DecisionContext 与模式 mode。"
              "只围绕需求涉及的物品 / 设备（need.bindings.item）及其位置或状态来说，不要描述人物当前在做什么"
              "（人物动作很快会变，草稿可能是提前生成的）。"
              "status 不是 holds 或 source 不是 seen/user_confirmed 的信念，不能说成确定的事实，用“好像 / 是不是”。"
              "mode=ASK 时必须是问句。claims 只列你断言的槽位键。输出 JSON：{\"text\": \"...\", "
              "\"claims\": [{\"key\": \"phone_1.location\", \"stated_as\": \"certain|probable|question\"}]}")

    def __init__(self, chat: ChatJSON):
        self.chat = chat
        self.name = f"llm:{chat.model}"
        self.fallback = TemplateDrafter()

    def draft(self, ctx: dict, mode: str, draft_id: str, origin: str = "on_demand") -> tuple[ResponseDraft, int]:
        data, _, latency, err = self.chat.complete(self.SYSTEM, json.dumps({"mode": mode, "context": ctx}, ensure_ascii=False))
        if err or not isinstance(data, dict) or not data.get("text"):
            d, _ = self.fallback.draft(ctx, mode, draft_id, origin="template_fallback")
            return d, latency
        claims = []
        for c in data.get("claims") or []:
            try:
                claims.append(Claim(key=c["key"], stated_as=c.get("stated_as", "probable")))
            except Exception:  # noqa: BLE001
                continue
        return ResponseDraft(draft_id=draft_id, need_id=ctx["need_id"], mode=mode, text=str(data["text"])[:120],
                             claims=claims, context_version=ctx["state_version"], dep_revs=ctx["dep_revs"],
                             model=self.name, origin=origin, latency_ms=latency, created_t_ms=ctx["t_ms"]), latency


# ---------------------------------------------------------------- 面向机会（Opportunity）的草稿：口语化，适合语音播报
@dataclass
class OppDraft:
    text: str
    mode: str
    claims: list[dict]
    origin: str
    model: str
    latency_ms: int = 0
    created_ms: int = 0


def _opp_names(ctx: dict) -> tuple[str, str, str]:
    o = ctx["opportunity"]
    who = "、".join(n for n in o.get("addressee_names") or [] if n and n != "大家")
    item = (o.get("item") or {}).get("name") or zh(o.get("topic") or "")
    place = (o.get("item") or {}).get("place") or ""
    return who, item, place


class OppTemplateDrafter:
    name = "template"

    def draft(self, ctx: dict, mode: str, origin: str = "template") -> tuple[OppDraft, int]:
        o = ctx["opportunity"]
        who, item, place = _opp_names(ctx)
        hi = f"{who}，" if who else ""
        key = o.get("key", "")
        src = o.get("source")
        if mode == "AMBIENT":
            short = o["problem"].split("（")[0].split("；")[0].removeprefix("提醒").strip()
            if item and not item.isascii():
                text = f"{item}还在{place}" if place else f"别忘了{item}"
            else:  # 话题是没翻译的英文词时，用机会描述的前半句
                text = short[:16] or "提醒一下"
        elif key.startswith("search:"):
            text = f"{item}在{place}哦。" if place else f"{item}好像在附近，我帮你留意着。"
        elif src == "belief_gap":
            text = f"{hi}{item}刚被挪到{place}了。" if mode == "REMIND" else f"{hi}你在找{item}吗？它在{place}。"
        elif key.startswith("appliance:"):
            text = f"{item}好像还开着，要不要关一下？"
        elif key.startswith("forget:"):
            text = f"{hi}{item}还在{place}，记得带上哦。" if mode == "REMIND" else f"{hi}{item}带了吗？好像还在{place}。"
        elif o.get("deadline", "").startswith("time:"):
            short = o["problem"].split("（")[0].rstrip("。")
            text = f"{hi}时间到了，{short}。"
        elif item and not item.isascii() and o.get("deadline") == "goal:leave_home":
            text = f"{hi}别忘了带{item}哦。" if mode == "REMIND" else f"{hi}{item}带上了吗？"
        else:
            text = f"{hi}提醒一下：{o['problem']}" if mode == "REMIND" else f"{hi}{o['problem']}，需要我提醒吗？"
        claims = [{"key": f"{(o.get('item') or {}).get('id')}.location", "stated_as": "probable"}] if (o.get("item") or {}).get("id") else []
        return OppDraft(text=text, mode=mode, claims=claims, origin=origin, model=self.name, created_ms=ctx["t_ms"]), 0


class OppLLMDrafter:
    SYSTEM = ("你是家里的语音助理，现在要主动对家人说一句话，会用自然的人声播出来。写一句口语化的中文："
              "像家人之间的提醒一样亲切、简短（不超过 26 个字），不要像机器播报，不要用“检测到 / 系统 / 用户”这类词。"
              "有名字就称呼名字。mode=REMIND 直接提醒；mode=ASK 用问句确认；mode=AMBIENT 写 10 个字以内的提示卡标题。"
              "只说机会涉及的事；item.status 不是 holds 或 item.source 不是 seen/user_confirmed 的事实要说“好像 / 是不是”；"
              "不要描述对方此刻正在做什么（这句话可能是提前写好的）。"
              "输出 JSON：{\"text\": \"...\", \"claims\": [{\"key\": \"umbrella_1.location\", \"stated_as\": \"certain|probable|question\"}]}")

    def __init__(self, chat):
        self.chat = chat
        self.name = f"llm:{chat.model}"
        self.fallback = OppTemplateDrafter()

    def draft(self, ctx: dict, mode: str, origin: str = "prefetch") -> tuple[OppDraft, int]:
        data, _, latency, err = self.chat.complete(self.SYSTEM, json.dumps({"mode": mode, **ctx}, ensure_ascii=False))
        if err or not isinstance(data, dict) or not data.get("text"):
            d, _ = self.fallback.draft(ctx, mode, origin="template_fallback")
            d.latency_ms = latency
            return d, latency
        claims = [c for c in (data.get("claims") or []) if isinstance(c, dict) and c.get("key")]
        return OppDraft(text=str(data["text"]).strip()[:60], mode=mode, claims=claims, origin=origin, model=self.name,
                        latency_ms=latency, created_ms=ctx["t_ms"]), latency
