"""源库：SQLite 存储、状态流转、健康分、内容快照。"""

from __future__ import annotations

import gzip
import hashlib
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
  l3_tested        INTEGER DEFAULT 0,
  l3_ok            INTEGER DEFAULT 0,
  live_channels    INTEGER DEFAULT 0,
  live_groups      INTEGER DEFAULT 0,
  compat           TEXT DEFAULT '',
  content_md5      TEXT DEFAULT '',
  dup_of           TEXT DEFAULT '',
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
CREATE INDEX IF NOT EXISTS idx_check_log_ts ON check_log(ts);
CREATE INDEX IF NOT EXISTS idx_snapshot_source ON snapshot(source_id);
"""


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def reason_of(detail: dict) -> str:
    """把 check_log.detail 里的错误归一成可统计的原因。"""
    err = str(detail.get("error") or "")
    if err.startswith("HTTP "):
        return err[:12]
    if err.startswith("响应为空"):
        return "空响应/疑似反爬"
    if "getaddrinfo" in err:
        return "DNS 解析失败"
    if "Timeout" in err or "timed out" in err:
        return "超时"
    if "ConnectError" in err or "RemoteProtocolError" in err or "ReadError" in err:
        return "连接失败/被重置"
    if "SSL" in err or "CERTIFICATE" in err:
        return "SSL 证书错误"
    if err.startswith("可达但解析不出"):
        return "结构无效/反爬页"
    if "HTML 反爬页" in err:
        return "HTML 反爬页（疑似存活）"
    return err[:24] or "未知"


class Registry:
    def __init__(self, db_path: Path, snap_root: Path | None = None):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self._migrate()
        self.conn.commit()
        self.snap_root = Path(snap_root) if snap_root else db_path.parent / "snapshots"

    def _migrate(self) -> None:
        """为旧库补齐新增列。"""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(source)")}
        for col, ddl in (
            ("l3_tested", "INTEGER DEFAULT 0"),
            ("l3_ok", "INTEGER DEFAULT 0"),
            ("live_channels", "INTEGER DEFAULT 0"),
            ("live_groups", "INTEGER DEFAULT 0"),
            ("compat", "TEXT DEFAULT ''"),
            ("content_md5", "TEXT DEFAULT ''"),
            ("dup_of", "TEXT DEFAULT ''"),
        ):
            if col not in cols:
                self.conn.execute(f"ALTER TABLE source ADD COLUMN {col} {ddl}")

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

    # ---------- 挖源记录（GitHub hunt 去重用） ----------

    def note_hunt_batch(self, repo_full_names: list[str]) -> None:
        now = _now()
        self.conn.executemany(
            "INSERT INTO origin(source_id, channel, ref, ts) VALUES('','github_repo',?,?)",
            [(ref, now) for ref in repo_full_names],
        )
        self.conn.commit()

    def hunted_recently(self, repo_full_name: str, days: int) -> bool:
        cutoff = time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(time.time() - days * 86400))
        row = self.conn.execute(
            "SELECT 1 FROM origin WHERE channel='github_repo' AND ref=? "
            "AND ts>=? LIMIT 1",
            (repo_full_name, cutoff),
        ).fetchone()
        return bool(row)

    # ---------- 历史与统计 ----------

    def history(self, sid: str, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            "SELECT ts, level, ok, latency_ms, detail FROM check_log "
            "WHERE source_id=? ORDER BY rowid DESC LIMIT ?",
            (sid, limit),
        ).fetchall()
        out = []
        for r in reversed(rows):
            d = dict(r)
            try:
                d["parsed"] = json.loads(d.pop("detail") or "{}")
            except ValueError:
                d["parsed"] = {}
            out.append(d)
        return out

    def availability(self, sid: str, days: int = 30) -> float | None:
        """近 N 天体检成功率（%），无记录返回 None。"""
        cutoff = time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(time.time() - days * 86400))
        row = self.conn.execute(
            "SELECT COUNT(*) AS n, SUM(ok) AS ok_n FROM check_log "
            "WHERE source_id=? AND ts>=?",
            (sid, cutoff),
        ).fetchone()
        n = row["n"] or 0
        if n == 0:
            return None
        return (row["ok_n"] or 0) / n * 100

    def failure_reasons(self, days: int = 30) -> list[dict]:
        """聚合近 N 天失败原因：[{reason, count, sources}]。"""
        cutoff = time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(time.time() - days * 86400))
        rows = self.conn.execute(
            "SELECT source_id, detail FROM check_log WHERE ok=0 AND ts>=?",
            (cutoff,),
        ).fetchall()
        agg: dict[str, dict] = {}
        for r in rows:
            try:
                parsed = json.loads(r["detail"] or "{}")
            except ValueError:
                parsed = {}
            reason = reason_of(parsed)
            entry = agg.setdefault(reason, {"reason": reason, "count": 0, "sids": set()})
            entry["count"] += 1
            entry["sids"].add(r["source_id"])
        out = sorted(
            ({"reason": e["reason"], "count": e["count"],
              "sources": len(e["sids"])} for e in agg.values()),
            key=lambda x: -x["count"],
        )
        return out

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
        elif schema_type in ("invalid", "empty") and outcome.status > 0:
            status = "invalid"
        elif schema_type == "anti_crawl":
            # 反爬页可能是活的：放宽判死时限（grace 倍数 × 判死线）
            grace = (settings["validate"].get("anti_crawl_grace", 3)
                     * settings["validate"]["dead_after_failures"])
            status = "dead" if streak_fail >= grace else "anti_crawl"
        elif streak_fail >= settings["validate"]["dead_after_failures"]:
            status = "dead"
        else:
            status = "flaky"

        sub_total = len(outcome.probes)
        sub_ok = sum(1 for p in outcome.probes if p.ok)
        # 优先用体检实际执行的档位；旧调用方未填时按探测结果回退推断
        level = outcome.level or ("L2" if sub_total else "L1")
        detail = json.dumps(
            {
                "http": outcome.status,
                "schema": schema_type,
                "error": outcome.error,
                "subs": [f"{p.kind}:{p.status}" for p in outcome.probes],
                "l3": [outcome.l3_tested, outcome.l3_ok],
                # 该源本次是按非校验证书模式抓的（证书链不完整），便于事后审计
                "tls": 1 if outcome.tls_insecure else 0,
            },
            ensure_ascii=False,
        )
        self.conn.execute(
            "INSERT INTO check_log(source_id, ts, level, ok, latency_ms, detail) "
            "VALUES(?,?,?,?,?,?)",
            (sid, now, level, 1 if ok else 0, outcome.latency_ms, detail),
        )
        score = self._score(sid, ok, sub_ok, sub_total, schema, settings)
        content_md5 = hashlib.md5(outcome.content).hexdigest() if ok and outcome.content else ""
        self.conn.execute(
            "UPDATE source SET schema_type=?, status=?, last_checked=?, "
            "last_ok=CASE WHEN ? THEN ? ELSE last_ok END, "
            "streak_ok=?, streak_fail=?, latency_ms=?, sites_count=?, "
            "parses_count=?, lives_count=?, storehouse_count=?, sub_ok=?, "
            "sub_total=?, l3_tested=?, l3_ok=?, live_channels=?, "
            "live_groups=?, compat=?, "
            "content_md5=CASE WHEN ? THEN ? ELSE content_md5 END, "
            "dup_of='', health_score=? WHERE id=?",
            (schema_type, status, now, 1 if ok else 0, now,
             streak_ok, streak_fail, outcome.latency_ms,
             schema.sites_count if schema else 0,
             schema.parses_count if schema else 0,
             schema.lives_count if schema else 0,
             schema.storehouse_count if schema else 0,
             sub_ok, sub_total, outcome.l3_tested, outcome.l3_ok,
             schema.live_channels if schema else 0,
             schema.live_groups if schema else 0,
             schema.compat if schema else "",
             1 if ok else 0, content_md5, score, sid),
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
        elif schema.schema_type == "live":
            richness = min(1.0, schema.live_channels / 300)
        else:
            richness = min(1.0, schema.sites_count / 40 * 0.7 + schema.lives_count / 5 * 0.3)
        score = 100 * (w["success"] * rate + w["subs"] * sub_ratio + w["richness"] * richness)
        if not ok_now:
            score = min(score, 20)
        return round(score, 1)

    # ---------- 生命周期 ----------

    def cleanup(self, settings: dict) -> dict:
        """按保留期清理 check_log / origin / snapshot，并 VACUUM 瘦身。"""
        cfg = settings.get("cleanup") or {}
        now = time.time()

        def cutoff(days: int) -> str:
            return time.strftime("%Y-%m-%d %H:%M:%S",
                                 time.localtime(now - days * 86400))

        stats = {}
        stats["check_log"] = self.conn.execute(
            "DELETE FROM check_log WHERE ts < ?",
            (cutoff(cfg.get("check_log_days", 90)),)).rowcount
        stats["origin"] = self.conn.execute(
            "DELETE FROM origin WHERE channel='github_repo' AND ts < ?",
            (cutoff(cfg.get("origin_days", 90)),)).rowcount
        old = self.conn.execute(
            "SELECT gzip_path FROM snapshot WHERE ts < ?",
            (cutoff(cfg.get("snapshot_days", 90)),)).fetchall()
        for r in old:
            try:
                Path(r["gzip_path"]).unlink(missing_ok=True)
            except OSError:
                pass
        stats["snapshot"] = len(old)
        self.conn.execute("DELETE FROM snapshot WHERE ts < ?",
                          (cutoff(cfg.get("snapshot_days", 90)),))
        self.conn.commit()
        self.conn.execute("VACUUM")
        return stats

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

    # ---------- 同内容去重 ----------

    def dedupe(self) -> list[dict]:
        """按最近内容 md5 归并同内容源：保留最优，其余标记 dup_of。

        每次全量重算（内容变化会自动解除归并）。返回归并组信息。
        """
        self.conn.execute("UPDATE source SET dup_of=''")
        rows = [dict(r) for r in self.conn.execute(
            "SELECT id, url, name, starred, health_score, content_md5 "
            "FROM source WHERE banned=0 AND status='active' "
            "AND content_md5 IS NOT NULL AND content_md5!=''")]
        groups: dict[str, list[dict]] = {}
        for r in rows:
            groups.setdefault(r["content_md5"], []).append(r)
        merged: list[dict] = []
        for grp in groups.values():
            if len(grp) < 2:
                continue
            grp.sort(key=lambda r: (-r["starred"], -r["health_score"]))
            primary, rest = grp[0], grp[1:]
            for r in rest:
                self.conn.execute(
                    "UPDATE source SET dup_of=? WHERE id=?",
                    (primary["id"], r["id"]))
                merged.append({
                    "primary_name": primary["name"] or primary["url"],
                    "primary": primary["url"],
                    "merged_name": r["name"] or r["url"],
                    "merged": r["url"],
                })
        self.conn.commit()
        return merged

    def latest_snapshot(self, sid: str) -> tuple[bytes, str] | None:
        """取最近一次内容快照，返回 (内容, 时间戳)；没有则 None。"""
        row = self.conn.execute(
            "SELECT ts, gzip_path FROM snapshot WHERE source_id=? "
            "ORDER BY rowid DESC LIMIT 1",
            (sid,),
        ).fetchone()
        if not row:
            return None
        try:
            with gzip.open(row["gzip_path"], "rb") as fh:
                return fh.read(), row["ts"]
        except OSError:
            return None

    def close(self) -> None:
        self.conn.close()
