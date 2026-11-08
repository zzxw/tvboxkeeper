from pathlib import Path

from tvboxkeeper.config import load_settings
from tvboxkeeper.models import CheckOutcome, ProbeResult, SchemaInfo
from tvboxkeeper.registry import Registry

SETTINGS = load_settings(None)


def make_outcome(url="http://e.com/a.json", ok=True, schema="tvbox",
                 probes=(), content=b'{"sites": []}') -> CheckOutcome:
    if schema == "invalid":
        info = SchemaInfo(schema_type="invalid")
    elif schema == "anti_crawl":
        info = SchemaInfo(schema_type="anti_crawl")
    else:
        info = SchemaInfo(schema_type=schema, sites_count=10, lives_count=2)
    return CheckOutcome(
        url=url, ok=ok, status=200 if ok else 404, latency_ms=120,
        error="" if ok else "HTTP 404", schema=info,
        probes=list(probes), content=content,
    )


def make_registry(tmp_path: Path) -> Registry:
    return Registry(tmp_path / "t.sqlite", tmp_path / "snapshots")


def test_upsert_dedup(tmp_path):
    reg = make_registry(tmp_path)
    sid1, created1 = reg.upsert("http://e.com/a.json")
    sid2, created2 = reg.upsert("http://e.com/a.json?utm_source=x")
    assert created1 and not created2 and sid1 == sid2


def test_status_transitions(tmp_path):
    reg = make_registry(tmp_path)
    reg.upsert("http://e.com/a.json", name="A")
    # 1-2 次失败：flaky
    reg.record(make_outcome(ok=False), SETTINGS)
    assert reg.get_by_url("http://e.com/a.json")["status"] == "flaky"
    reg.record(make_outcome(ok=False), SETTINGS)
    assert reg.get_by_url("http://e.com/a.json")["status"] == "flaky"
    # 第 3 次失败：dead（dead_after_failures=3）
    reg.record(make_outcome(ok=False), SETTINGS)
    assert reg.get_by_url("http://e.com/a.json")["status"] == "dead"
    # 成功后复活
    reg.record(make_outcome(ok=True), SETTINGS)
    row = reg.get_by_url("http://e.com/a.json")
    assert row["status"] == "active" and row["streak_fail"] == 0


def test_invalid_reachable(tmp_path):
    reg = make_registry(tmp_path)
    reg.upsert("http://e.com/b.json")
    reg.record(make_outcome(url="http://e.com/b.json", ok=False,
                            schema="invalid"), SETTINGS)
    assert reg.get_by_url("http://e.com/b.json")["status"] == "invalid"


def test_score_bounds(tmp_path):
    reg = make_registry(tmp_path)
    reg.upsert("http://e.com/ok.json")
    probes = [ProbeResult("http://e.com/jar.jar", "jar", 200, 50),
              ProbeResult("http://e.com/live.txt", "live", 0, 0, "timeout")]
    reg.record(make_outcome(url="http://e.com/ok.json", probes=probes), SETTINGS)
    score = reg.get_by_url("http://e.com/ok.json")["health_score"]
    assert 0 < score <= 100
    assert reg.get_by_url("http://e.com/ok.json")["sub_ok"] == 1


def test_snapshot_saved_and_pruned(tmp_path):
    reg = make_registry(tmp_path)
    reg.upsert("http://e.com/s.json")
    settings = load_settings(None)
    settings["snapshot"]["keep"] = 2
    for i in range(3):
        reg.record(make_outcome(url="http://e.com/s.json",
                                content=f'{{"n": {i}}}'.encode()), settings)
    d = tmp_path / "snapshots"
    files = list(d.rglob("*.json.gz"))
    assert len(files) == 2


def test_banned_excluded(tmp_path):
    reg = make_registry(tmp_path)
    reg.upsert("http://e.com/ban.json", banned=True)
    rows = reg.sources()
    assert all(r["banned"] == 0 for r in rows)
    reg.record(make_outcome(url="http://e.com/ban.json", ok=False), SETTINGS)
    assert reg.get_by_url("http://e.com/ban.json")["status"] == "banned"


def test_availability_and_failure_reasons(tmp_path):
    import pytest
    reg = make_registry(tmp_path)
    reg.upsert("http://e.com/h.json", name="H")
    for ok in (True, True, False):
        reg.record(make_outcome(url="http://e.com/h.json", ok=ok), SETTINGS)
    sid = reg.get_by_url("http://e.com/h.json")["id"]
    assert reg.availability(sid) == pytest.approx(66.7, abs=0.1)
    assert reg.availability("nonexistent") is None

    reasons = reg.failure_reasons(days=30)
    assert reasons and reasons[0]["reason"] == "HTTP 404"
    assert reasons[0]["count"] == 1 and reasons[0]["sources"] == 1


def test_history_order(tmp_path):
    reg = make_registry(tmp_path)
    reg.upsert("http://e.com/h2.json")
    for ok in (True, False, True):
        reg.record(make_outcome(url="http://e.com/h2.json", ok=ok), SETTINGS)
    hist = reg.history(reg.get_by_url("http://e.com/h2.json")["id"])
    assert [h["ok"] for h in hist] == [1, 0, 1]  # 旧 → 新


def test_hunted_recently(tmp_path):
    reg = make_registry(tmp_path)
    assert not reg.hunted_recently("o/r1", 7)
    reg.note_hunt_batch(["o/r1"])
    assert reg.hunted_recently("o/r1", 7)
    assert not reg.hunted_recently("o/r2", 7)


def test_anti_crawl_grace(tmp_path):
    reg = make_registry(tmp_path)
    reg.upsert("http://e.com/ac.json")
    url = "http://e.com/ac.json"
    # 连续 3 次反爬页：达到普通判死线但仍在宽限内
    for _ in range(3):
        reg.record(make_outcome(url=url, ok=False, schema="anti_crawl"), SETTINGS)
    assert reg.get_by_url(url)["status"] == "anti_crawl"
    # 达到 3×3=9 次：判死
    for _ in range(6):
        reg.record(make_outcome(url=url, ok=False, schema="anti_crawl"), SETTINGS)
    assert reg.get_by_url(url)["status"] == "dead"


def test_latest_snapshot_roundtrip(tmp_path):
    reg = make_registry(tmp_path)
    reg.upsert("http://e.com/snap.json")
    url = "http://e.com/snap.json"
    assert reg.latest_snapshot(reg.get_by_url(url)["id"]) is None
    payload = b'{"sites": [{"name": "x"}]}'
    reg.record(make_outcome(url=url, content=payload), SETTINGS)
    got = reg.latest_snapshot(reg.get_by_url(url)["id"])
    assert got is not None and got[0] == payload and got[1]


def test_dedupe_groups_same_content(tmp_path):
    from tvboxkeeper.publish import pick_sources
    reg = make_registry(tmp_path)
    reg.upsert("http://e.com/d1.json", name="D1")
    reg.upsert("http://e.com/d2.json", name="D2")
    for u in ("http://e.com/d1.json", "http://e.com/d2.json"):
        reg.record(make_outcome(url=u, ok=True, content=b'{"same": 1}'), SETTINGS)
    merged = reg.dedupe()
    assert len(merged) == 1
    rows = {r["url"]: r for r in reg.sources()}
    d1, d2 = rows["http://e.com/d1.json"], rows["http://e.com/d2.json"]
    assert (d1["dup_of"] == d2["id"]) or (d2["dup_of"] == d1["id"])
    picked = [r["url"] for r in pick_sources(list(rows.values()), 0)]
    assert len(picked) == 1  # 重复源不进订阅


def test_dedupe_resets_when_content_diverges(tmp_path):
    reg = make_registry(tmp_path)
    reg.upsert("http://e.com/d1.json", name="D1")
    reg.upsert("http://e.com/d2.json", name="D2")
    for u in ("http://e.com/d1.json", "http://e.com/d2.json"):
        reg.record(make_outcome(url=u, ok=True, content=b'{"same": 1}'), SETTINGS)
    reg.dedupe()
    # 内容分叉后应自动解除归并
    reg.record(make_outcome(url="http://e.com/d2.json", ok=True,
                            content=b'{"different": 2}'), SETTINGS)
    merged = reg.dedupe()
    assert merged == []
    rows = {r["url"]: r for r in reg.sources()}
    assert rows["http://e.com/d2.json"]["dup_of"] == ""


def test_compat_persisted(tmp_path):
    reg = make_registry(tmp_path)
    reg.upsert("http://e.com/comp.json", name="C")
    outcome = make_outcome(url="http://e.com/comp.json")
    outcome.schema = SchemaInfo(schema_type="tvbox", sites_count=2,
                                lives_count=1, compat="偏影视仓")
    reg.record(outcome, SETTINGS)
    assert reg.get_by_url("http://e.com/comp.json")["compat"] == "偏影视仓"


def test_reason_of_variants():
    from tvboxkeeper.registry import reason_of
    assert reason_of({"error": "响应为空（疑似反爬或接口异常）"}) == "空响应/疑似反爬"
    assert reason_of({"error": "HTTP 404 Not Found"}) == "HTTP 404 Not"
    assert reason_of({}) == "未知"


def test_cleanup_purges_old_data(tmp_path):
    reg = make_registry(tmp_path)
    reg.upsert("http://e.com/c1.json")
    reg.record(make_outcome(url="http://e.com/c1.json"), SETTINGS)
    reg.note_hunt_batch(["o/old"])
    reg.conn.execute("UPDATE check_log SET ts='2020-01-01 00:00:00'")
    reg.conn.execute("UPDATE origin SET ts='2020-01-01 00:00:00'")
    reg.conn.commit()
    stats = reg.cleanup(SETTINGS)
    assert stats["check_log"] >= 1 and stats["origin"] >= 1
    assert reg.conn.execute(
        "SELECT COUNT(*) AS n FROM check_log").fetchone()["n"] == 0
    # 清理后再 VACUUM 不报错，库仍可用
    reg.record(make_outcome(url="http://e.com/c1.json"), SETTINGS)
    assert reg.get_by_url("http://e.com/c1.json")["status"] == "active"
