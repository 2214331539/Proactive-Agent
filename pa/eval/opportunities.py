"""C18 评测：主动性表现的事后结算（设计说明 §3.6）。

两种口径：
1. 人工核验（验收主线）：界面上对每条主动消息标“有用 / 没必要 / 判断错误”，并在“该说没说”的时刻标“漏报”。
   有用 +L−C_int，没必要 −C_int，判断错误 −C_int−C_FA；完美助理 = 所有有用与漏报的机会都准时说一次。
   PG_human = NPV / NPV_oracle。
2. 标注窗口（可复现）：data/annotations/<video>.json 标出每个机会的有效窗口、损失与标签
   （must_fire / can_skip / must_skip），按窗口匹配投递，算 NPV、PG、FAH、及时命中率、提前量。
"""
from __future__ import annotations

import json
from pathlib import Path

C_INT = {"AMBIENT": 0.1, "ASK": 1.5, "REMIND": 1.0}
C_FA = {"AMBIENT": 0.2, "ASK": 1.0, "REMIND": 1.5}
DEFAULT_L = 8.0
VERDICTS = {"useful": "有用", "unnecessary": "没必要", "wrong": "判断错误", "missed": "漏报"}


def load_run(run_dir: str | Path) -> dict:
    d = Path(run_dir)
    events = []
    ui = d / "ui_events.jsonl"
    if ui.exists():
        events = [json.loads(x) for x in ui.read_text(encoding="utf-8").splitlines() if x.strip()]
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8")) if (d / "meta.json").exists() else {}
    verdicts = load_verdicts(d)
    audio = {e["delivery_id"]: e.get("url") for e in events if e.get("ev") == "tts" and e.get("delivery_id")}
    dls = []
    for e in events:
        if e.get("ev") == "delivery" and e.get("opp_key"):
            dls.append({**e, "audio": e.get("audio") or audio.get(e["delivery_id"])})
    t_end = max([e["t"] for e in events] or [0])
    return {"dir": str(d), "run": d.name, "meta": meta, "events": events, "deliveries": dls, "verdicts": verdicts,
            "t_end_ms": t_end}


def load_verdicts(run_dir: Path) -> list[dict]:
    p = Path(run_dir) / "verdicts.jsonl"
    if not p.exists():
        return []
    rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    latest: dict[str, dict] = {}
    for r in rows:  # 同一条消息以最后一次核验为准
        k = r.get("delivery_id") or f"miss_{r.get('t_ms')}"
        latest[k] = r
    return [r for r in latest.values() if r.get("verdict") != "clear"]


def human_stats(deliveries: list[dict], verdicts: list[dict], hours: float) -> dict:
    by_dl = {v["delivery_id"]: v for v in verdicts if v.get("delivery_id")}
    missed = [v for v in verdicts if v.get("verdict") == "missed"]
    npv = oracle = 0.0
    counts = {k: 0 for k in VERDICTS}
    voice = [d for d in deliveries if d.get("mode") != "AMBIENT"]
    for d in deliveries:
        v = by_dl.get(d["delivery_id"])
        mode = d.get("mode", "REMIND")
        L = float((d.get("est") or {}).get("L") or DEFAULT_L)
        if v is None:
            continue
        counts[v["verdict"]] = counts.get(v["verdict"], 0) + 1
        if v["verdict"] == "useful":
            npv += L - C_INT[mode]
            oracle += L - C_INT[mode]
        elif v["verdict"] == "unnecessary":
            npv -= C_INT[mode]
        elif v["verdict"] == "wrong":
            npv -= C_INT[mode] + C_FA[mode]
    for m in missed:
        oracle += float(m.get("L") or DEFAULT_L) - 1.0
    counts["missed"] = len(missed)
    judged = counts["useful"] + counts["unnecessary"] + counts["wrong"]
    return {"n_deliveries": len(deliveries), "n_voice": len(voice), "n_ambient": len(deliveries) - len(voice),
            "per_hour": round(len(voice) / hours, 2) if hours > 0 else None, "judged": judged, "counts": counts,
            "precision": round(counts["useful"] / judged, 3) if judged else None,
            "false_alarm_per_hour": round((counts["unnecessary"] + counts["wrong"]) / hours, 2) if hours > 0 else None,
            "npv": round(npv, 2), "npv_oracle": round(oracle, 2),
            "pg_human": round(npv / oracle, 3) if oracle > 0 else None}


def annotated_stats(deliveries: list[dict], ann: dict, start_s: float, hours: float, grace_s: float = 20) -> dict:
    """按标注窗口结算。投递时刻换算成视频时间：start_s + t_ms/1000。"""
    opps = ann.get("opportunities", [])
    used: set[str] = set()
    npv = 0.0
    hits, leads, rows = 0, [], []
    for o in opps:
        lo, hi, L = o["t_earliest"], o["t_latest"], float(o.get("L", DEFAULT_L))
        kws = [k.lower() for k in o.get("keywords", [])]
        cand = [d for d in deliveries if d["delivery_id"] not in used and lo <= start_s + d["t_ms"] / 1000 <= hi + grace_s
                and (not kws or any(k in (d.get("text", "") + d.get("problem", "")).lower() for k in kws))]
        # 同一机会优先算语音（提示音只响一声，不传达内容）
        d = min(cand, key=lambda x: (x.get("mode") == "AMBIENT", x["t_ms"])) if cand else None
        if d is not None:
            used.add(d["delivery_id"])
            tv = start_s + d["t_ms"] / 1000
            w = 1.0 if tv <= hi else max(0.0, 1 - (tv - hi) / grace_s)
            if o.get("label", "must_fire") == "must_fire":
                npv += L * w
                hits += 1
                leads.append(round(hi - tv, 1))
            elif o.get("label") == "must_skip":
                npv -= C_FA[d.get("mode", "REMIND")]
        rows.append({"id": o.get("id"), "label": o.get("label", "must_fire"), "window": [lo, hi],
                     "hit": d["delivery_id"] if d else None, "t_video": round(start_s + d["t_ms"] / 1000, 1) if d else None})
    fa = 0
    for d in deliveries:
        npv -= C_INT.get(d.get("mode", "REMIND"), 1.0)
        if d["delivery_id"] not in used:
            npv -= C_FA.get(d.get("mode", "REMIND"), 1.5)
            fa += d.get("mode") != "AMBIENT"
    must = [o for o in opps if o.get("label", "must_fire") == "must_fire"]
    oracle = sum(float(o.get("L", DEFAULT_L)) - 1.0 for o in must)
    return {"npv": round(npv, 2), "npv_oracle": round(oracle, 2), "pg": round(npv / oracle, 3) if oracle > 0 else None,
            "recall": round(hits / len(must), 3) if must else None, "fah": round(fa / hours, 2) if hours > 0 else None,
            "lead_time_s": sorted(leads), "opportunities": rows}


def evaluate(run_dir: str | Path, ann_path: str | Path | None = None) -> dict:
    r = load_run(run_dir)
    hours = max(r["t_end_ms"], 1) / 3_600_000
    out = {"run": r["run"], "title": r["meta"].get("title"), "duration_min": round(r["t_end_ms"] / 60000, 1),
           "human": human_stats(r["deliveries"], r["verdicts"], hours),
           "by_source": {}, "by_mode": {}}
    for d in r["deliveries"]:
        out["by_source"][d.get("source_label", d.get("source"))] = out["by_source"].get(d.get("source_label", d.get("source")), 0) + 1
        out["by_mode"][d.get("mode")] = out["by_mode"].get(d.get("mode"), 0) + 1
    if ann_path is None and r["meta"].get("video_id"):
        cand = Path("data/annotations") / f"{r['meta']['video_id']}.json"
        ann_path = cand if cand.exists() else None
    if ann_path:
        ann = json.loads(Path(ann_path).read_text(encoding="utf-8"))
        out["annotated"] = annotated_stats(r["deliveries"], ann, float(r["meta"].get("start") or 0), hours)
    return out
