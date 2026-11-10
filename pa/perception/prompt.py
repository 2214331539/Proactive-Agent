"""M4 提示词：固定前缀（可命中前缀缓存）+ 状态切片 + 语音 + 帧（设计文档 §3 M4）。"""
from __future__ import annotations

from ..schemas import StateSlice, Utterance, WindowRequest
from ..state.attrs import ACTIVITIES, LOC_RELS

PROMPT_VERSION = "p-2.3"

SYSTEM_PROMPT = f"""你是视频状态追踪器。根据最近几帧画面和语音，输出对“当前状态”的增量补丁，只输出一个 JSON 对象。
规则：
1. 只能引用 STATE / PLACES 中已有的实体 ID；新出现的实体用 new，并给临时 ID（n1, n2 …），同一补丁内可以引用临时 ID。
2. 只报告变化。STATE 中标记 CHECK 的实体，其 location（物品）或 activity（人物）必须在 still / gone / unsure 中给出其一
   （若已变化则用 set / act 代替）：
   still = 该区域清楚可见且仍成立；gone = 区域清楚可见但已不成立；unsure = 看不清或不在画面中。
3. 看不清就用 unsure。不要猜测身份、所有权，不要编造坐标。new 的 cls 用英文小写单数名词（phone, umbrella, keys, cup, door）。
4. 画面中人物框上的标签（P1、P2…）对应 STATE 中带相同标签的 person；新人物 new 时填 label。
5. 词表：
   activity ∈ {sorted(ACTIVITIES)}
   location.rel ∈ {sorted(LOC_RELS)}（HELD_BY / WORN_BY 的 ref 必须是人）
   act.type ∈ [PICK_UP, PUT_DOWN, PUT_INTO, TAKE_OUT, GIVE, MOVING_TOWARD, ENTER, EXIT, OPEN, CLOSE, TURN_ON, TURN_OFF]
   state ∈ [on, off, open, closed]（仅 DEVICE / 门等）
6. 语音中出现请求、承诺、偏好或事实陈述时输出 said，并标注 intent ∈ [request, commitment, preference, info, none]；
   said.speaker 只能填 STATE 中的人物 ID（不确定就省略）。SPEECH 里的 voice:名字 是声音标签：若能从画面确定是哪个人在说话、
   且该人物还没有 voice，输出 {{"op": "link", "person": "person_1", "anchor": "voice:Marry"}}。
7. view.visible_places 只列本窗口清楚可见的地点 ID；view.persons_visible 列画面中的人物 ID。
8. 人物 activity 要综合画面与 SPEECH（含 STATE 之前的对话）：对话表明有人正要出门 / 上班 / 离开（如 let's go、
   go go go、don't be late for work、我得走了），且画面中此人已起身或走动，就设为 packing 或 moving_to_exit，
   src 用 inferred；看到走向门口 / 出口时用 act MOVING_TOWARD。
输出格式：
{{"view": {{"room": null, "visible_places": ["desk_1"], "persons_visible": ["person_1"], "quality": "clear"}},
  "ops": [
    {{"op": "new", "tmp": "n1", "type": "OBJECT", "cls": "phone", "desc": "黑色手机", "at": "desk_1"}},
    {{"op": "set", "key": "n1.location", "value": {{"rel": "ON", "ref": "desk_1"}}, "src": "seen", "ev": ["f_000012"]}},
    {{"op": "act", "type": "PICK_UP", "subj": "person_1", "obj": "backpack_1", "ev": ["f_000046"]}},
    {{"op": "set", "key": "person_1.activity", "value": "packing", "src": "seen"}},
    {{"op": "still", "keys": ["phone_1.location"]}},
    {{"op": "said", "utt_id": "u_0001", "speaker": "person_1", "intent": "info", "content": "我得走了"}}
  ]}}
没有任何变化时：{{"view": {{...}}, "ops": [{{"op": "still", "keys": [...CHECK 条目...]}}]}}"""


def fmt_ms(ms: int) -> str:
    s = ms / 1000
    return f"{int(s // 60):02d}:{s % 60:04.1f}"


def render_slice(sl: StateSlice) -> str:
    lines = [f"STATE v{sl.version}"]
    if not sl.entities:
        lines.append("(空：尚未建立任何实体)")
    for e in sl.entities:
        lab = f" [{e.label}]" if e.label else ""
        voice = f" | voice={e.anchors['voice']}" if e.anchors.get("voice") else ""
        bel = "; ".join(f"{k}={v}" for k, v in e.beliefs.items())
        lines.append(f"{e.id}{lab} {e.type} {e.cls} | {e.desc or '-'}{voice}" + (f" | {bel}" if bel else "")
                     + (" | CHECK" if e.check else ""))
    lines.append("PLACES " + (", ".join(f"{p.id}({p.cls}{'；' + p.desc if p.desc else ''})" for p in sl.places) or "(无)"))
    if sl.open_needs:
        lines.append("OPEN_NEEDS " + ", ".join(sl.open_needs))
    return "\n".join(lines)


def render_user_text(req: WindowRequest, sl: StateSlice, utts: list[Utterance], frame_times: list[int],
                     face_note: str | None = None) -> str:
    parts = [render_slice(sl)]
    if utts:
        parts.append("SPEECH")
        for u in utts:
            who = f"voice:{u.voice_anchor}" if u.voice_anchor else "voice:?"
            if u.person_id:
                who += f"→{u.person_id}"
            parts.append(f"[{fmt_ms(u.start_ms)}–{fmt_ms(u.end_ms)}] {u.utt_id} {who}: “{u.text}”")
    if face_note:
        parts.append(face_note)
    parts.append(f"WINDOW {req.window_id} {fmt_ms(req.start_ms)}–{fmt_ms(req.end_ms)} trigger={req.trigger}")
    parts.append("FRAMES " + "  ".join(f"{fid} ({fmt_ms(t)})" for fid, t in zip(req.frame_ids, frame_times)))
    return "\n".join(parts)
