from tvboxkeeper import compliance
from tvboxkeeper.config import load_settings
from tvboxkeeper.models import CheckOutcome, SchemaInfo
from tvboxkeeper.registry import Registry

SETTINGS = load_settings(None)


def test_scan_hit_and_miss():
    assert compliance.scan(b'{"sites": [{"api": "http://bet365.example/x"}]}',
                           SETTINGS) == "bet365"
    assert compliance.scan(b'{"sites": [{"api": "http://clean.example/x"}]}',
                           SETTINGS) is None
    assert compliance.scan(b"", SETTINGS) is None


def test_scan_disabled():
    settings = load_settings(None)
    settings["compliance"]["enabled"] = False
    assert compliance.scan(b"http://bet365.example", settings) is None


def test_custom_keywords_override():
    settings = load_settings(None)
    settings["compliance"]["keywords"] = ["自定义词"]
    assert compliance.scan("包含自定义词的内容".encode(), settings) == "自定义词"
    assert compliance.scan(b"http://bet365.example", settings) is None


def make_outcome(url="http://e.com/x.json", ok=True, content=b"{}"):
    return CheckOutcome(url=url, ok=ok, status=200, latency_ms=100,
                        schema=SchemaInfo(schema_type="tvbox", sites_count=2),
                        content=content)


def test_registry_auto_ban(tmp_path):
    reg = Registry(tmp_path / "t.sqlite", tmp_path / "snapshots")
    reg.upsert("http://e.com/bad.json", name="B")
    reg.record(make_outcome(
        url="http://e.com/bad.json",
        content=b'{"sites": [{"api": "http://hg0088.example"}]}'), SETTINGS)
    row = reg.get_by_url("http://e.com/bad.json")
    assert row["status"] == "banned" and row["banned"] == 1
    assert "hg0088" in (row["notes"] or "")
    # 封禁源不进发布清单
    assert reg.sources() == []
