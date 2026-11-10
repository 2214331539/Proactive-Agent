"""M12 慢环：把一段时间内的状态变化和语句整理成情节，写入长期记忆（设计文档 §3 M12）。

默认用确定性模板生成摘要（离线可用）；配置了 LLM 时改用模型摘要。语音需求编译接口 compile_need() 需要 LLM。
"""
from __future__ import annotations

import json

from ..llm import ChatJSON
from ..perception.prompt import fmt_ms
from ..schemas import Episode, StateDelta
from ..state.attrs import render_value

SUMMARY_SYSTEM = ("你把一段视频时间内的结构化状态变化和语音整理成一段简短的中文情节摘要，必须保留实体 ID（如 phone_1）"
                  "和时间（MM:SS）。只陈述给出的事实，不推测。输出 JSON：{\"text\": \"...\"}")

COMPILE_SYSTEM = """你把视频里有人说出的请求 / 叮嘱 / 承诺（如“别忘了带伞”“出门前提醒我关灯”）编译成一个“待办需求”，
让助理在合适时刻主动提醒。输入：utterance（说了什么、谁说的）与 state（当前实体，人物带 voice=名字）。

规则：
- bindings 只放 state 里已有的实体 ID，用变量名作键（如 "p1": "person_2"）。被提醒的人优先绑定；
  提到的物品如果还不在 state 里，不要编造 ID，把它写进 topic（英文小写，如 "umbrella"）。
- 条件语法（JSON DSL）：{"all":[...]} / {"any":[...]} / {"not":...} / {"path":"$p1.activity","in":[...]} /
  {"path":"$p1.presence","eq":"left"} / {"fn":"with","args":["$item","$p1"]}。比较符 eq ne in nin。
- 可用属性：activity ∈ [sitting_working, standing, walking, packing, moving_to_exit, leaving, entering, eating,
  cooking, cleaning, talking, resting, other]；presence ∈ [in_view, out_of_view, left]；location / state。
- “出门 / 离开”类请求：arm = 相关人物 activity ∈ [standing, walking, packing]；
  fire = 任一相关人物 activity ∈ [packing, moving_to_exit, leaving] 或 presence = left；
  fire 里必须再加兜底 {"fn":"any_person","args":["activity",["packing","moving_to_exit","leaving"]]}
  （第一人称移动视角下同一个人可能被识别成多个 ID，绑定的 ID 未必会更新）。
- resolve 只有在能用 state 判断“已完成”时才写（例如物品已 with 到人身上）；否则写 {}（表示只能靠用户反馈关闭）。
- mode_rule：[{"when": {}, "cap": "REMIND"}]。
示例：Jack(person_1) 对 Sophie(person_2)、Lily(person_3) 说 “Don't forget your umbrellas today!”，state 里没有雨伞 →
{"kind":"user_request","topic":"umbrella","summary":"提醒 Sophie 和 Lily 出门带伞（Jack 说今天可能下雨）",
 "bindings":{"p1":"person_2","p2":"person_3"},
 "arm":{"any":[{"path":"$p1.activity","in":["standing","walking","packing"]},{"path":"$p2.activity","in":["standing","walking","packing"]}]},
 "fire":{"any":[{"path":"$p1.activity","in":["packing","moving_to_exit","leaving"]},{"path":"$p1.presence","eq":"left"},
               {"path":"$p2.activity","in":["packing","moving_to_exit","leaving"]},{"path":"$p2.presence","eq":"left"},
               {"fn":"any_person","args":["activity",["packing","moving_to_exit","leaving"]]}]},
 "resolve":{},"mode_rule":[{"when":{},"cap":"REMIND"}]}
- addressee：被提醒的人的名字（按说话人名字写，如 ["Jack"]；“remind me” 就是说话人自己）。
- 对助理 / 机器人本身提的请求（“robot, remind me…”“提醒我…”）加 "to_assistant": true。
- 相对时间的定时提醒（“in three minutes”“五分钟后”“半小时后”）写 "due_in_s": 秒数，此时 arm / fire 可以写 {}；
  绝对时间（“at two pm”“明天早上”）无法换算成视频时间，写进 summary，不写 due_in_s。
- 让助理做体力活（拿东西、关电视、收拾）不是提醒需求，输出 {"kind": null}。
- 当下的催促（“快点”“快走”“别迟到”“hurry up”）说完马上就会被执行，不需要日后提醒，输出 {"kind": null}。
输入里的 recent_speech 是前几句对话，帮助理解指代。绝大多数闲聊不需要提醒：只有当这句话包含需要日后提醒的内容时
才编译，否则输出 {"kind": null}。只输出 JSON。"""


class Consolidator:
    def __init__(self, session_id: str, every_ms: int = 30000, llm: ChatJSON | None = None, rule_latency_ms: int = 0):
        self.session_id = session_id
        self.every_ms = every_ms
        self.llm = llm
        self.rule_latency_ms = rule_latency_ms
        self.last_ms = 0
        self.n = 0

    def due(self, now_ms: int) -> bool:
        return now_ms - self.last_ms >= self.every_ms

    def summarize(self, deltas: list[StateDelta], utterances: list[dict], now_ms: int) -> tuple[Episode | None, int]:
        """返回 (情节, 生成耗时 ms)。情节的 t_avail = now + 耗时。"""
        t0, self.last_ms = self.last_ms, now_ms
        lines, ents = [], set()
        for d in deltas:
            if not (t0 < d.t_event_ms <= now_ms):
                continue
            for ch in d.changes:
                if ch.reason.startswith("tick"):
                    continue
                ents.add(ch.key.split(".", 1)[0])
                lines.append(f"{fmt_ms(d.t_event_ms)} {ch.key}: {render_value(ch.old)} → {render_value(ch.new)} ({ch.new_status})")
        for u in utterances:
            if t0 < u["t_ms"] <= now_ms:
                lines.append(f"{fmt_ms(u['t_ms'])} {u.get('speaker') or '?'} 说：{u['content']}")
                if u.get("speaker"):
                    ents.add(u["speaker"])
        if not lines:
            return None, 0
        text, latency = "；".join(lines), self.rule_latency_ms
        if self.llm is not None:
            data, _, latency, err = self.llm.complete(SUMMARY_SYSTEM, "\n".join(lines))
            if err is None and isinstance(data, dict) and data.get("text"):
                text = str(data["text"])
        self.n += 1
        ep = Episode(episode_id=f"ep_{self.n:03d}", session_id=self.session_id, t_start_ms=t0, t_end_ms=now_ms,
                     t_avail_ms=now_ms + latency, entity_ids=sorted(ents), text=text)
        return ep, latency

    def compile_need(self, said: dict, state_lines: list[str], recent_speech: list[str] | None = None) -> tuple[dict | None, int]:
        if self.llm is None:
            return None, 0
        user = json.dumps({"utterance": said, "recent_speech": recent_speech or [], "state": state_lines}, ensure_ascii=False)
        data, _, latency, err = self.llm.complete(COMPILE_SYSTEM, user)
        if err or not isinstance(data, dict) or not data.get("kind"):
            return None, latency
        data["origin"] = "speech"
        return data, latency
