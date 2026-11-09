"""CLI 端到端：发布产物应完整反映归并与镜像口径。"""

import json

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


def test_publish_mirror_note_splits_config_and_live(tmp_path, monkeypatch):
    """镜像比例必须分配置/直播统计，避免与多仓条数口径混淆。"""
    monkeypatch.setenv("TVBOX_ROOT", str(tmp_path))
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.yaml").write_text(
        'publish:\n  mirror_base: "https://mirror.example/mirrors"\n',
        encoding="utf-8")
    _seed(tmp_path)

    result = CliRunner().invoke(app, ["publish"])
    assert result.exit_code == 0, result.output

    report = (tmp_path / "output" / "报告.md").read_text(encoding="utf-8")
    assert "配置 1/1" in report
    assert "直播 0/0" not in report  # 没有直播源时不渲染空分组

    duo = json.loads(
        (tmp_path / "output" / "多仓.json").read_text(encoding="utf-8"))
    assert duo["storeHouse"][0]["sourceUrl"].startswith(
        "https://mirror.example/mirrors/")


def test_publish_keeps_mirrors_when_snapshot_unreadable(tmp_path, monkeypatch):
    """快照读不到（换机器/快照目录未同步）时，不得删掉线上可用的镜像文件。"""
    monkeypatch.setenv("TVBOX_ROOT", str(tmp_path))
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.yaml").write_text(
        'publish:\n  mirror_base: "https://mirror.example/mirrors"\n',
        encoding="utf-8")
    _seed(tmp_path)

    assert CliRunner().invoke(app, ["publish"]).exit_code == 0
    mirror_dir = tmp_path / "output" / "mirrors"
    before = sorted(p.name for p in mirror_dir.glob("*.json")
                    if p.name != "index.json")
    assert before, "首次发布应产出镜像"

    # 快照目录被清空 = 库里 gzip_path 全部指向不存在的文件
    for f in (tmp_path / "data" / "snapshots").rglob("*.gz"):
        f.unlink()

    assert CliRunner().invoke(app, ["publish"]).exit_code == 0
    after = sorted(p.name for p in mirror_dir.glob("*.json")
                   if p.name != "index.json")
    assert after == before, "镜像文件不应因读不到快照被删除"

    duo = json.loads(
        (tmp_path / "output" / "多仓.json").read_text(encoding="utf-8"))
    assert duo["storeHouse"]
    assert all(e["sourceUrl"].startswith("https://mirror.example/mirrors/")
               for e in duo["storeHouse"]), "订阅仍应指向镜像而非回退原站"


def test_publish_writes_lite_subscription(tmp_path, monkeypatch):
    """精简版订阅需按 lite 配置的条数上限生成，且报告说明两档差异。"""
    monkeypatch.setenv("TVBOX_ROOT", str(tmp_path))
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.yaml").write_text(
        """publish:
  lite:
    max_sources: 1
    max_live: 1
""",
        encoding="utf-8")
    _seed(tmp_path)

    assert CliRunner().invoke(app, ["publish"]).exit_code == 0
    lite = json.loads(
        (tmp_path / "output" / "多仓-lite.json").read_text(encoding="utf-8"))
    assert len(lite["storeHouse"]) == 1
    lite_live = json.loads(
        (tmp_path / "output" / "直播-lite.json").read_text(encoding="utf-8"))
    assert lite_live["lives"] == []

    report = (tmp_path / "output" / "报告.md").read_text(encoding="utf-8")
    assert "精简版" in report and "多仓-lite.json" in report
    site = (tmp_path / "output" / "site" / "index.html").read_text(
        encoding="utf-8")
    assert "精简版 1 个源" in site
