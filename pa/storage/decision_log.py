"""C17 决策日志：每次价值评估的全部中间量（含沉默与等待）与事后结果（设计说明 §5.6）。

与事件日志共用 log.sqlite。是以后校准概率、学习代价、离线评估（倾向分）和人工核验的前提。
"""
from __future__ import annotations

import json

from .log import EventLog

SCHEMA = """
CREATE TABLE IF NOT EXISTS value_decisions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, t_ms INTEGER NOT NULL, opp_key TEXT NOT NULL,
  source TEXT, category TEXT, p REAL, q REAL, L REAL, H REAL, utility_json TEXT, v_wait REAL,
  decision TEXT NOT NULL, reason TEXT, propensity REAL, delivered INTEGER DEFAULT 0, delivery_id TEXT,
  text TEXT, feedback TEXT, estimate_json TEXT);
CREATE INDEX IF NOT EXISTS idx_vd_opp ON value_decisions(session_id, opp_key, t_ms);
"""


class DecisionLog:
    def __init__(self, log: EventLog):
        self.log = log
        with log._lock:
            log.conn.executescript(SCHEMA)

    def write(self, t_ms: int, opp, est, delivered: bool = False, delivery_id: str | None = None,
              text: str | None = None) -> None:
        pub = est.public()
        with self.log._lock:
            self.log.conn.execute(
                "INSERT INTO value_decisions(session_id,t_ms,opp_key,source,category,p,q,L,H,utility_json,v_wait,decision,"
                "reason,propensity,delivered,delivery_id,text,estimate_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (self.log.session_id, int(t_ms), opp.key, opp.source, opp.category, est.p, est.q, est.L, est.H,
                 json.dumps(pub["U"]), est.v_wait, est.decision, est.reason, 1.0, int(delivered), delivery_id, text,
                 json.dumps(pub, ensure_ascii=False)))

    def feedback(self, delivery_id: str, kind: str) -> None:
        with self.log._lock:
            self.log.conn.execute("UPDATE value_decisions SET feedback=? WHERE delivery_id=?", (kind, delivery_id))
