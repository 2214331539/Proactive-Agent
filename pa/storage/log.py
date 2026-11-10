"""M11 Event Log：SQLite WAL。所有消息追加写入，可用于恢复、重放与评测（设计文档 §8.2）。"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS sessions(
  session_id TEXT PRIMARY KEY, source TEXT NOT NULL, mode TEXT NOT NULL, camera_mode TEXT NOT NULL,
  started_wall TEXT NOT NULL, config_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS log(
  log_seq INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, kind TEXT NOT NULL,
  msg_id TEXT NOT NULL UNIQUE, causation_id TEXT, t_event_ms INTEGER NOT NULL, t_avail_ms INTEGER NOT NULL,
  payload_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_log_kind_t ON log(session_id, kind, t_avail_ms);
CREATE INDEX IF NOT EXISTS idx_log_cause ON log(causation_id);
CREATE TABLE IF NOT EXISTS snapshots(
  session_id TEXT NOT NULL, version INTEGER NOT NULL, log_seq INTEGER NOT NULL, t_ms INTEGER NOT NULL,
  state_json TEXT NOT NULL, agenda_json TEXT NOT NULL, PRIMARY KEY(session_id, version));
CREATE TABLE IF NOT EXISTS traces(
  msg_id TEXT NOT NULL, span TEXT NOT NULL, t_start_wall REAL, t_end_wall REAL, t_ms INTEGER, attrs_json TEXT);
"""


def _dump(x: Any) -> str:
    if hasattr(x, "model_dump"):
        x = x.model_dump()
    return json.dumps(x, ensure_ascii=False, default=str)


class EventLog:
    def __init__(self, path: str | Path, session_id: str):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.executescript(SCHEMA)
        self.session_id = session_id
        self._lock = threading.Lock()
        self._n = 0

    def start(self, source: str, mode: str, camera_mode: str, config: dict, wall: str) -> None:
        with self._lock:
            self.conn.execute("INSERT OR REPLACE INTO sessions VALUES(?,?,?,?,?,?)",
                              (self.session_id, source, mode, camera_mode, wall, _dump(config)))

    def append(self, kind: str, msg_id: str, t_event_ms: int, t_avail_ms: int, payload: Any,
               causation_id: str | None = None) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO log(session_id,kind,msg_id,causation_id,t_event_ms,t_avail_ms,payload_json)"
                " VALUES(?,?,?,?,?,?,?)",
                (self.session_id, kind, msg_id, causation_id, int(t_event_ms), int(t_avail_ms), _dump(payload)))
            self._n += 1
            if self._n % 200 == 0:
                self.conn.commit()

    def snapshot(self, version: int, t_ms: int, state: dict, agenda: dict) -> None:
        with self._lock:
            seq = self.conn.execute("SELECT COALESCE(MAX(log_seq),0) FROM log").fetchone()[0]
            self.conn.execute("INSERT OR REPLACE INTO snapshots VALUES(?,?,?,?,?,?)",
                              (self.session_id, version, seq, t_ms, _dump(state), _dump(agenda)))

    def trace(self, msg_id: str, span: str, t0: float, t1: float, t_ms: int, attrs: dict | None = None) -> None:
        with self._lock:
            self.conn.execute("INSERT INTO traces VALUES(?,?,?,?,?,?)", (msg_id, span, t0, t1, t_ms, _dump(attrs or {})))

    def query(self, kind: str | None = None) -> list[dict]:
        sql = "SELECT log_seq,kind,msg_id,causation_id,t_event_ms,t_avail_ms,payload_json FROM log WHERE session_id=?"
        args: list[Any] = [self.session_id]
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        rows = self.conn.execute(sql + " ORDER BY log_seq", args).fetchall()
        return [{"log_seq": r[0], "kind": r[1], "msg_id": r[2], "causation_id": r[3], "t_event_ms": r[4],
                 "t_avail_ms": r[5], "payload": json.loads(r[6])} for r in rows]

    def close(self) -> None:
        with self._lock:
            self.conn.commit()
            self.conn.close()


class PatchCache:
    """VLM 响应缓存：上游完全相同时直接重放，免去重复调用（跨运行共享）。"""

    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.execute("CREATE TABLE IF NOT EXISTS patch_cache(cache_key TEXT PRIMARY KEY, model TEXT, "
                          "prompt_version TEXT, response_json TEXT, latency_ms INTEGER)")
        self._lock = threading.Lock()

    def get(self, key: str) -> dict | None:
        with self._lock:
            r = self.conn.execute("SELECT response_json FROM patch_cache WHERE cache_key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else None

    def put(self, key: str, value: dict, model: str, prompt_version: str) -> None:
        with self._lock:
            self.conn.execute("INSERT OR REPLACE INTO patch_cache VALUES(?,?,?,?,?)",
                              (key, model, prompt_version, json.dumps(value, ensure_ascii=False), value.get("latency_ms", 0)))
            self.conn.commit()
