"""内容级变更：站点增删识别。"""

import json

from tvboxkeeper.diffing import content_change, describe


def _cfg(sites) -> bytes:
    return json.dumps({"sites": sites}, ensure_ascii=False).encode()


def test_detects_added_and_removed_sites():
    old = _cfg([{"key": "a"}, {"key": "b"}, {"key": "c"}])
    new = _cfg([{"key": "b"}, {"key": "c"}, {"key": "d"}])
    change = content_change(old, new)
    assert change["a"] == 1 and change["r"] == 1
    assert change["add"] == ["d"] and change["del"] == ["a"]


def test_no_change_returns_empty():
    cfg = _cfg([{"key": "a"}])
    assert content_change(cfg, cfg) == {}
    # 顺序变化不算变更（按集合比较）
    assert content_change(_cfg([{"key": "a"}, {"key": "b"}]),
                          _cfg([{"key": "b"}, {"key": "a"}])) == {}


def test_site_label_fallback_to_name_and_api():
    old = _cfg([{"name": "站点甲"}, {"key": "k1"}])
    new = _cfg([{"name": "站点甲"}, {"key": "k1"}, {"api": "http://x.com/api"}])
    change = content_change(old, new)
    assert change["add"] == ["http://x.com/api"]


def test_live_text_has_no_site_change():
    assert content_change(b"g1,#genre#\nCCTV1,http://a/1.m3u8",
                          b"g1,#genre#\nCCTV2,http://a/2.m3u8") == {}


def test_describe_renders_chinese():
    text = describe({"a": 2, "r": 1, "add": ["x", "y"], "del": ["z"]})
    assert "新增 2 个（x、y）" in text and "移除 1 个（z）" in text
    assert describe({}) == ""
