"""CLI 端到端：发布产物应完整反映归并与镜像口径。"""

from typer.testing import CliRunner

from tvboxkeeper.cli import app
from tvboxkeeper.config import load_settings
from tvboxkeeper.models import CheckOutcome, SchemaInfo
from tvboxkeeper.registry import Registry

SETTINGS = load_settings(None)


def _seed(tmp_path, payload=b'{"sites": [{"key": "a"}, {"key": "b"}]}'):
    reg = Registry(tmp_path / "data" / "tvbox.sqlite",
                   tmp_path / "data" / "snapshots")
    for name, url in (("主源", "http://a.example/c.json"),
                      ("镜像", "http://b.example/c.json")):
        reg.upsert(url, name=name)
        reg.record(CheckOutcome(
            url=url, ok=True, status=200, latency_ms=100,
            schema=SchemaInfo(schema_type="tvbox", sites_count=2),
            content=payload), SETTINGS)
    reg.close()


def test_publish_report_lists_dedupe_groups(tmp_path, monkeypatch):
    monkeypatch.setenv("TVBOX_ROOT", str(tmp_path))
    (tmp_path / "config").mkdir()
    _seed(tmp_path)

    result = CliRunner().invoke(app, ["publish"])
    assert result.exit_code == 0, result.output

    report = (tmp_path / "output" / "报告.md").read_text(encoding="utf-8")
    assert "同内容镜像归并" in report
    section = report.split("## 同内容镜像归并", 1)[1].split("## 源榜单", 1)[0]
    assert "→ 保留" in section
    assert "http://b.example/c.json" in section
