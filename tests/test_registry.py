from pathlib import Path

from tvboxkeeper.config import load_settings
from tvboxkeeper.models import CheckOutcome, ProbeResult, SchemaInfo
from tvboxkeeper.registry import Registry

SETTINGS = load_settings(None)


def make_outcome(url="http://e.com/a.json", ok=True, schema="tvbox",
                 probes=(), content=b'{"sites": []}') -> CheckOutcome:
    if schema == "invalid":
        info = SchemaInfo(schema_type="invalid")
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
