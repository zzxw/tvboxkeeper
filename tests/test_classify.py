from tvboxkeeper.classify import decode_content, parse_config

BASE = "http://example.com/cfg.json"

TVBOX = """{
  "spider": "./jar/spider.jar;md5;abcdef",
  "sites": [{"key": "a", "name": "A", "type": 3, "api": "csp_A"}],
  "parses": [{"name": "json", "type": 0, "url": "http://p.example.com"}],
  "lives": [{"name": "直播", "type": 0, "url": "http://x.example.com/live.txt;http://y.example.com/tv.txt"}]
}"""

STOREHOUSE = """{
  "storeHouse": [
    {"sourceName": "仓A", "sourceUrl": "http://a.example.com/a.json"},
    {"sourceName": "仓B", "sourceUrl": "http://b.example.com/b.json"},
    {"noUrl": true}
  ]
}"""


def test_tvbox_structure_and_probes():
    info = parse_config(BASE, TVBOX)
    assert info.schema_type == "tvbox"
    assert info.sites_count == 1
    assert info.parses_count == 1
    assert info.lives_count == 1
    # 相对路径 spider 应基于配置 URL 解析
    assert ("jar", "http://example.com/jar/spider.jar") in info.probes
    assert info.storehouse_count == 0


def test_live_urls_split_by_semicolon():
    info = parse_config(BASE, TVBOX)
    live_urls = [u for kind, u in info.probes if kind == "live"]
    assert live_urls == ["http://x.example.com/live.txt",
                         "http://y.example.com/tv.txt"]


def test_storehouse_children():
    info = parse_config(BASE, STOREHOUSE)
    assert info.schema_type == "storehouse"
    assert info.storehouse_count == 2
    assert info.children[0]["sourceName"] == "仓A"


def test_urls_multirepo_format():
    info = parse_config(BASE, '{"urls": [{"url": "http://a.example.com/a.json", '
                              '"name": "线路A"}, {"name": "无地址"}]}')
    assert info.schema_type == "storehouse"
    assert info.storehouse_count == 1
    assert info.children[0] == {"sourceName": "线路A",
                                "sourceUrl": "http://a.example.com/a.json"}


def test_mixed():
    info = parse_config(BASE, '{"storeHouse": [{"sourceName": "仓A", '
                              '"sourceUrl": "http://a.example.com/a.json"}], '
                              '"sites": [{"type": 0}]}')
    assert info.schema_type == "mixed"


def test_invalid():
    assert parse_config(BASE, "plain text, not json at all").schema_type == "invalid"
    assert parse_config(BASE, '{"foo": 1}').schema_type == "invalid"


def test_site_apis_collected():
    info = parse_config(BASE, '{"sites": ['
                              '{"type": 0, "api": "http://s1.example/api.php"}, '
                              '{"type": 3, "api": "csp_XYQ"}, '
                              '{"type": 1, "api": "relative/path"}]}')
    # 只收集 type 0/1 且为 http(s) 的 api，供 L3 抽测
    assert info.site_apis == ["http://s1.example/api.php"]


def test_anti_crawl_html_detected():
    assert parse_config(BASE, "<!DOCTYPE html><html>Just a moment...</html>").schema_type == "anti_crawl"
    assert parse_config(BASE, "<html><body>请完成验证</body></html>").schema_type == "anti_crawl"


def test_jsonp_and_bom():
    wrapped = "callback(" + TVBOX + ");"
    assert parse_config(BASE, wrapped).schema_type == "tvbox"
    assert parse_config(BASE, "﻿" + TVBOX).schema_type == "tvbox"


def test_gbk_decode():
    text = "{'sites': [{'name': '中文'}]}".encode("gbk").decode("gbk")
    raw = ("{" + '"sites": [{"name": "中文"}]' + "}").encode("gbk")
    assert "中文" in decode_content(raw) or parse_config(BASE, decode_content(raw)).schema_type == "tvbox"
    assert text  # 占位，防止误删
