"""源库：SQLite 存储、状态流转、健康分、内容快照。"""

from __future__ import annotations

import gzip
import json
import sqlite3
import time
from pathlib import Path

from .models import CheckOutcome, SchemaInfo
from .normalize import guess_name, normalize_url, source_id

_SCHEMA = """
CREATE TABLE IF NOT EXISTS source (
  id               TEXT PRIMARY KEY,
  url              TEXT NOT NULL UNIQUE,
  name             TEXT DEFAULT '',
  gen              INTEGER DEFAULT 0,
  schema_type      TEXT DEFAULT '',
  status           TEXT DEFAULT 'new',
  first_seen       TEXT DEFAULT '',
  last_ok          TEXT DEFAULT '',
  last_checked     TEXT DEFAULT '',
  streak_ok        INTEGER DEFAULT 0,
  streak_fail      INTEGER DEFAULT 0,
  latency_ms       INTEGER DEFAULT 0,
  sites_count      INTEGER DEFAULT 0,
  parses_count     INTEGER DEFAULT 0,
  lives_count      INTEGER DEFAULT 0,
  storehouse_count INTEGER DEFAULT 0,
  sub_ok           INTEGER DEFAULT 0,
  sub_total        INTEGER DEFAULT 0,
  health_score     REAL DEFAULT 0,
  starred          INTEGER DEFAULT 0,
  banned           INTEGER DEFAULT 0,
  notes            TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS check_log (
  rowid_seq  INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id  TEXT NOT NULL,
  ts         TEXT NOT NULL,
  level      TEXT NOT NULL,
  ok         INTEGER NOT NULL,
  latency_ms INTEGER DEFAULT 0,
  detail     TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS snapshot (
  source_id   TEXT NOT NULL,
  ts          TEXT NOT NULL,
  content_md5 TEXT NOT NULL,
  gzip_path   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS origin (
  source_id TEXT NOT NULL,
  channel   TEXT NOT NULL,
  ref       TEXT DEFAULT '',
  ts        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_check_log_source ON check_log(source_id);
CREATE INDEX IF NOT EXISTS idx_snapshot_source ON snapshot(source_id);
"""


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class Registry:
    def __init__(self, db_path: Path, snap_root: Path | None = None):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()
        self.snap_root = Path(snap_root) if snap_root else db_path.parent / "snapshots"

    # ---------- 登记 ----------

    def upsert(
        self,
        url: str,
        name: str = "",
        gen: int = 0,
        origin: str = "manual",
        ref: str = "",
        starred: bool | None = None,
        banned: bool | None = None,
    ) -> tuple[str, bool]:
        """登记/更新源，返回 (source_id, 是否新建)。"""
        url = normalize_url(url)
        sid = source_id(url)
        now = _now()
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO source(id, url, name, gen, first_seen) "
            "VALUES(?,?,?,?,?)",
            (sid, url, name or guess_name(url), gen, now),
        )
        created = cur.rowcount > 0
        if name:
            self.conn.execute("UPDATE source SET name=? WHERE id=?", (name, sid))
        if starred is not None:
            self.conn.execute("UPDATE source SET starred=? WHERE id=?",
                              (1 if starred else 0, sid))
        if banned is not None:
            self.conn.execute(
                "UPDATE source SET banned=?, status=CASE WHEN ? THEN 'banned' ELSE status END "
                "WHERE id=?",
                (1 if banned else 0, 1 if banned else 0, sid))
        self.conn.execute(
            "INSERT INTO origin(source_id, channel, ref, ts) VALUES(?,?,?,?)",
            (sid, origin, ref, now))
        self.conn.commit()
        return sid, created

    def get(self, sid: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM source WHERE id=?", (sid,)).fetchone()
        return dict(row) if row else None

    def get_by_url(self, url: str) -> dict | None:
        return self.get(source_id(normalize_url(url)))

    def sources(self, status: str = "", include_banned: bool = False) -> list[dict]:
        sql = "SELECT * FROM source WHERE 1=1"
        params: list = []
        if status:
            sql += " AND status=?"
            params.append(status)
        if not include_banned:
            sql += " AND banned=0"
        sql += " ORDER BY health_score DESC, url"
        return [dict(r) for r in self.conn.execute(sql, params)]

    def set_flag(self, sid: str, field: str, value) -> None:
        assert field in ("starred", "banned", "notes", "name")
        self.conn.execute(f"UPDATE source SET {field}=? WHERE id=?", (value, sid))
        if field == "banned" and value:
            self.conn.execute(
                "UPDATE source SET status='banned' WHERE id=?", (sid,))
        self.conn.commit()

    # ---------- 体检记录 ----------

    def record(self, outcome: CheckOutcome, settings: dict) -> None:
        """记录一次体检结果：写 check_log、更新状态与健康分、存快照。"""
        url = normalize_url(outcome.url)
        sid = source_id(url)
        if self.get(sid) is None:
            self.upsert(outcome.url)
        row = self.get(sid)
        now = _now()
        ok = bool(outcome.ok)
        schema = outcome.schema
        schema_type = schema.schema_type if schema else ""

        streak_ok = row["streak_ok"] + 1 if ok else 0
        streak_fail = 0 if ok else row["streak_fail"] + 1
        if row["banned"]:
            status = "banned"
        elif ok:
            status = "active"
        elif schema_type == "invalid" and outcome.status > 0:
            status = "invalid"
        elif streak_fail >= settings["validate"]["dead_after_failures"]:
            status = "dead"
        else:
            status = "flaky"

        sub_total = len(outcome.probes)
        sub_ok = sum(1 for p in outcome.probes if p.ok)
        level = "L2" if sub_total else "L1"
        detail = json.dumps(
            {
                "http": outcome.status,
                "schema": schema_type,
                "error": outcome.error,
                "subs": [f"{p.kind}:{p.status}" for p in outcome.probes],
            },
            ensure_ascii=False,
        )
        self.conn.execute(
            "INSERT INTO check_log(source_id, ts, level, ok, latency_ms, detail) "
            "VALUES(?,?,?,?,?,?)",
            (sid, now, level, 1 if ok else 0, outcome.latency_ms, detail),
        )
        score = self._score(sid, ok, sub_ok, sub_total, schema, settings)
        self.conn.execute(
            "UPDATE source SET schema_type=?, status=?, last_checked=?, "
            "last_ok=CASE WHEN ? THEN ? ELSE last_ok END, "
            "streak_ok=?, streak_fail=?, latency_ms=?, sites_count=?, "
            "parses_count=?, lives_count=?, storehouse_count=?, sub_ok=?, "
            "sub_total=?, health_score=? WHERE id=?",
            (schema_type, status, now, 1 if ok else 0, now,
             streak_ok, streak_fail, outcome.latency_ms,
             schema.sites_count if schema else 0,
             schema.parses_count if schema else 0,
             schema.lives_count if schema else 0,
             schema.storehouse_count if schema else 0,
             sub_ok, sub_total, score, sid),
        )
        self.conn.commit()
        if ok and outcome.content:
            self._save_snapshot(sid, outcome.content,
                                settings["snapshot"]["keep"])

    def _recent_rate(self, sid: str, n: int = 10) -> float:
        rows = self.conn.execute(
            "SELECT ok FROM check_log WHERE source_id=? ORDER BY rowid DESC LIMIT ?",
            (sid, n),
        ).fetchall()
        if not rows:
            return 0.0
        return sum(r["ok"] for r in rows) / len(rows)

    def _score(
        self,
        sid: str,
        ok_now: bool,
        sub_ok: int,
        sub_total: int,
        schema: SchemaInfo | None,
        settings: dict,
    ) -> float:
        w = settings["score"]["weights"]
        rate = self._recent_rate(sid)
        sub_ratio = (sub_ok / sub_total) if sub_total else 1.0
        if schema is None or schema.schema_type in ("storehouse", "mixed"):
            richness = min(1.0, (schema.storehouse_count if schema else 0) / 10)
        else:
            richness = min(1.0, schema.sites_count / 40 * 0.7 + schema.lives_count / 5 * 0.3)
        score = 100 * (w["success"] * rate + w["subs"] * sub_ratio + w["richness"] * richness)
        if not ok_now:
            score = min(score, 20)
        return round(score, 1)

    # ---------- 快照 ----------

    def _save_snapshot(self, sid: str, content: bytes, keep: int) -> None:
        d = self.snap_root / sid
        d.mkdir(parents=True, exist_ok=True)
        ts = str(time.time_ns())
        path = d / f"{ts}.json.gz"
        with gzip.open(path, "wb") as fh:
            fh.write(content)
        import hashlib
        self.conn.execute(
            "INSERT INTO snapshot(source_id, ts, content_md5, gzip_path) VALUES(?,?,?,?)",
            (sid, _now(), hashlib.md5(content).hexdigest(), str(path)),
        )
        self.conn.commit()
        for old in sorted(d.glob("*.json.gz"))[:-keep] if keep else []:
            old.unlink()

    def close(self) -> None:
        self.conn.close()
