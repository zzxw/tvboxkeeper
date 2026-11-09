"""配置加载：返回的配置必须与 DEFAULTS 完全隔离。"""

from pathlib import Path

from tvboxkeeper.config import DEFAULTS, load_settings


def test_settings_instances_are_isolated():
    a = load_settings(None)
    b = load_settings(None)
    a["validate"]["level"] = "L3"
    a["network"]["user_agents"].append("custom")
    assert b["validate"]["level"] == "L2"
    assert "custom" not in b["network"]["user_agents"]


def test_defaults_not_mutated():
    before = DEFAULTS["validate"]["level"]
    settings = load_settings(None)
    settings["validate"]["level"] = "L3"
    settings["network"]["user_agents"].clear()
    assert DEFAULTS["validate"]["level"] == before
    assert DEFAULTS["network"]["user_agents"]


def test_yaml_overrides_without_leaking(tmp_path: Path):
    cfg = tmp_path / "settings.yaml"
    cfg.write_text("validate:\n  level: L3\n", encoding="utf-8")
    settings = load_settings(cfg)
    assert settings["validate"]["level"] == "L3"
    assert settings["validate"]["dead_after_failures"] == 3  # 未覆盖项保留
    assert load_settings(None)["validate"]["level"] == "L2"
