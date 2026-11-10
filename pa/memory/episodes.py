"""M13 长期记忆：SQLite FTS5 情节库。检索只返回 t_avail <= now 的情节（防止未来信息泄漏）。"""
from __future__ import annotations

import sqlite3

from ..schemas import Episode


class EpisodeStore:
    def __init__(self, path: str = ":memory:"):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        try:
            self.conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS episodes USING fts5(episode_id UNINDEXED, "
                              "session_id UNINDEXED, t_start_ms UNINDEXED, t_end_ms UNINDEXED, t_avail_ms UNINDEXED, "
                              "entity_ids, text)")
            self.fts = True
        except sqlite3.OperationalError:  # 少数 Python 发行版的 sqlite 没编进 FTS5
            self.conn.execute("CREATE TABLE IF NOT EXISTS episodes(episode_id, session_id, t_start_ms, t_end_ms, "
                              "t_avail_ms, entity_ids, text)")
            self.fts = False

    def add(self, ep: Episode) -> None:
        self.conn.execute("INSERT INTO episodes VALUES(?,?,?,?,?,?,?)",
                          (ep.episode_id, ep.session_id, ep.t_start_ms, ep.t_end_ms, ep.t_avail_ms,
                           " ".join(ep.entity_ids), ep.text))

    def history(self, entity_ids: list[str], before_ms: int, k: int = 3) -> list[dict]:
        rows = self.conn.execute("SELECT episode_id, t_start_ms, t_end_ms, t_avail_ms, entity_ids, text FROM episodes")\
            .fetchall()
        hits = []
        for r in rows:
            if int(r[3]) > before_ms:
                continue  # 双时间约束：事后才写入的情节不可见
            ents = set(str(r[4]).split())
            score = len(ents & set(entity_ids))
            if score:
                hits.append((score, int(r[2]), {"ref": r[0], "t_start_ms": int(r[1]), "t_end_ms": int(r[2]), "text": r[5]}))
        hits.sort(key=lambda x: (-x[0], -x[1]))
        return [h[2] for h in hits[:k]]

    def search(self, query: str, before_ms: int, k: int = 5) -> list[dict]:
        if self.fts:
            q = " OR ".join(f'"{w}"' for w in query.replace('"', " ").split() if w)
            if not q:
                return []
            rows = self.conn.execute("SELECT episode_id, t_start_ms, t_end_ms, t_avail_ms, text FROM episodes "
                                     "WHERE episodes MATCH ? ORDER BY rank", (q,)).fetchall()
        else:
            rows = self.conn.execute("SELECT episode_id, t_start_ms, t_end_ms, t_avail_ms, text FROM episodes").fetchall()
        out = [{"ref": r[0], "t_start_ms": int(r[1]), "t_end_ms": int(r[2]), "text": r[4]} for r in rows if int(r[3]) <= before_ms]
        return out[:k]

    def all_before(self, before_ms: int) -> list[dict]:
        rows = self.conn.execute("SELECT episode_id, t_start_ms, t_end_ms, t_avail_ms, text FROM episodes").fetchall()
        return sorted(({"ref": r[0], "t_start_ms": int(r[1]), "t_end_ms": int(r[2]), "text": r[4]}
                       for r in rows if int(r[3]) <= before_ms), key=lambda d: d["t_start_ms"])
