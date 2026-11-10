"""回放：优先读取 ui_events.jsonl；旧运行没有它时，从 log.sqlite（事件溯源日志）重建界面事件。"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from ..state.attrs import render_value

_LINE = re.compile(r"^(\S+)(?: \[(\w+)\])? (PERSON|OBJECT|PLACE|DEVICE) (\S+)(?: \| ([^|]*))?")


def load_events(run_dir: Path) -> list[dict]:
    ui = run_dir / "ui_events.jsonl"
    if ui.exists() and ui.stat().st_size > 0:
        evs = [json.loads(line) for line in ui.read_text(encoding="utf-8").splitlines() if line.strip()]
        if any(e.get("ev") not in ("start", "log") for e in evs):
            return evs
    return from_log(run_dir)


def from_log(run_dir: Path) -> list[dict]:
    db = run_dir / "log.sqlite"
    if not db.exists():
        return []
    con = sqlite3.connect(str(db))
    rows = con.execute("SELECT kind, msg_id, t_event_ms, t_avail_ms, payload_json FROM log ORDER BY log_seq").fetchall()
    final: dict[str, dict] = {}
    s = run_dir / "summary.json"
    if s.exists():
        for line in json.loads(s.read_text(encoding="utf-8")).get("final_state", []):
            m = _LINE.match(line)
            if m:
                final[m.group(1)] = {"id": m.group(1), "label": m.group(2), "type": m.group(3), "cls": m.group(4),
                                     "desc": (m.group(5) or "").strip()}
    counters: dict[str, int] = {}
    evs: list[dict] = [{"t": 0, "ev": "start", "mode": "replay", "live": False}]
    for kind, mid, te, ta, pj in rows:
        p = json.loads(pj)
        if kind == "utterance":
            evs.append({"t": ta, "ev": "utterance", "utt_id": mid, "speaker": p.get("voice_anchor") or p.get("person_id"),
                        "text": p.get("text"), "start_ms": p.get("start_ms"), "end_ms": p.get("end_ms")})
        elif kind == "window":
            evs.append({"t": ta, "ev": "window", "window_id": mid, "trigger": p.get("trigger"), "frames": [],
                        "start_ms": p.get("start_ms"), "end_ms": p.get("end_ms")})
        elif kind in ("patch", "patch_failed") and p.get("source") == "vlm":
            evs.append({"t": ta, "ev": "patch", "window_id": p.get("window_id"), "latency_ms": p.get("latency_ms"),
                        "failed": kind == "patch_failed", "n_ops": len(p.get("ops", [])),
                        "n_rejected": len(p.get("rejected", []))})
            # 重建实体创建：同类 ID 按提交顺序递增分配；被拒绝或被并发去重绑定的 new 不占号
            rejected_tmps = {r.get("op", {}).get("tmp") for r in p.get("rejected", []) if isinstance(r.get("op"), dict)}
            dedup = {i.get("tmp") for i in p.get("ignored", []) if i.get("reason") == "dedup_concurrent"}
            for op in p.get("ops", []):
                if op.get("op") != "new" or op.get("tmp") in rejected_tmps or op.get("tmp") in dedup:
                    continue
                cls = re.sub(r"[^a-z0-9]+", "_", (op.get("cls") or ("person" if op.get("type") == "PERSON" else "obj")).lower()).strip("_")
                counters[cls] = counters.get(cls, 0) + 1
                eid = f"{cls}_{counters[cls]}"
                info = final.get(eid, {})
                evs.append({"t": ta, "ev": "entity", "id": eid, "type": op.get("type"), "cls": cls,
                            "desc": op.get("desc") or info.get("desc", ""), "label": op.get("label") or info.get("label")})
        elif kind == "delta":
            evs.append({"t": ta, "ev": "delta", "version": p.get("version_to"), "changes": [
                {"key": c["key"], "old": render_value(c.get("old")), "new": render_value(c.get("new")),
                 "status": c.get("new_status"), "source": c.get("source"), "reason": c.get("reason")}
                for c in p.get("changes", [])]})
        elif kind == "need_edge":
            d = p.get("detail", {})
            meta = d.get("meta") or {}
            evs.append({"t": ta, "ev": "need", "need_id": p["need_id"], "kind": p["kind"], "edge": p["edge"],
                        "phase": d.get("phase"), "bindings": d.get("bindings", {}), "summary": meta.get("summary"),
                        "topic": meta.get("topic")})
        elif kind == "need_compile" and p.get("spec"):
            sp = p["spec"]
            evs.append({"t": ta, "ev": "compile", "utt": p["said"].get("content"), "speaker": p["said"].get("speaker"),
                        "summary": sp.get("summary"), "topic": sp.get("topic"), "latency_ms": ta - te})
        elif kind == "draft" and p.get("origin") == "prefetch":
            evs.append({"t": ta, "ev": "draft", "need_id": p.get("need_id"), "mode": p.get("mode"), "text": p.get("text")})
        elif kind == "decision":
            evs.append({"t": ta, "ev": "decision", "need_id": p.get("need_id"), "warranted": p.get("warranted"),
                        "mode": p.get("mode"), "applied": p.get("applied_mode"), "reason": p.get("reason"),
                        "notes": p.get("rule_notes"), "latency_ms": p.get("latency_ms"), "model": p.get("model")})
        elif kind == "delivery":
            evs.append({"t": ta, "ev": "delivery", "delivery_id": p.get("delivery_id"), "need_id": p.get("need_id"),
                        "kind": p.get("kind"), "mode": p.get("mode"), "text": p.get("text"), "fire_t": p.get("fire_t_ms")})
        elif kind == "blocked":
            evs.append({"t": ta, "ev": "blocked", "need_id": p.get("need_id"), "reason": p.get("reason")})
        elif kind == "deferred":
            evs.append({"t": ta, "ev": "deferred", "need_id": p.get("need_id"), "until_ms": p.get("until_ms")})
        elif kind == "episode":
            evs.append({"t": ta, "ev": "episode", "episode_id": mid, "text": p.get("text")})
    # 用需求编译结果补全 need / decision / delivery 的摘要
    summaries: dict[str, str] = {}
    for e in evs:
        if e["ev"] == "need" and e.get("summary"):
            summaries[e["need_id"]] = e["summary"]
    last_compile = None
    for e in evs:
        if e["ev"] == "compile":
            last_compile = e
        if e["ev"] == "need" and e["kind"] == "user_request" and not e.get("summary") and last_compile:
            summaries.setdefault(e["need_id"], last_compile.get("summary"))
            e["summary"] = summaries[e["need_id"]]
            e["topic"] = last_compile.get("topic")
        if e["ev"] in ("decision", "delivery") and e.get("need_id") in summaries:
            e.setdefault("summary", summaries[e["need_id"]])
    evs.sort(key=lambda e: e["t"])
    return evs
