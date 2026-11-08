import json

from tvboxkeeper.publish import (build_duocang, build_live_config, build_report,
                                 build_site, format_diff_text, pick_live_sources,
                                 pick_sources, previous_urls, webhook_payload)


def make_row(name, url, status="active", health=80.0, starred=0,
             sites=10, lives=2, storehouse=0, banned=0, sub_ok=0, sub_total=0):
    return {
        "name": name, "url": url, "status": status, "health_score": health,
        "starred": starred, "sites_count": sites, "lives_count": lives,
        "storehouse_count": storehouse, "banned": banned,
        "sub_ok": sub_ok, "sub_total": sub_total,
        "latency_ms": 100, "last_checked": "2026-11-08 12:00:00",
    }


ROWS = [
    make_row("低分", "http://a/c.json", health=30),
    make_row("正常", "http://b/c.json", health=90),
    make_row("置顶", "http://c/c.json", health=70, starred=1),
    make_row("死源", "http://d/c.json", status="dead", health=5),
    make_row("封禁", "http://e/c.json", health=99, banned=1),
]


def test_duocang_filters_and_sorts():
    data = build_duocang(ROWS, min_health=60)
    urls = [e["sourceUrl"] for e in data["storeHouse"]]
    assert urls == ["http://c/c.json", "http://b/c.json"]  # 置顶在前
    assert "storeHouse" in data


def test_duocang_names():
    data = build_duocang(ROWS, min_health=60)
    names = [e["sourceName"] for e in data["storeHouse"]]
    assert names == ["置顶", "正常"]


def test_duocang_includes_delay():
    data = build_duocang(ROWS, min_health=60)
    delays = [e["sourceDelay"] for e in data["storeHouse"]]
    assert delays == [100, 100]  # make_row 默认 latency_ms=100
    assert all(isinstance(d, int) for d in delays)


def test_duocang_with_mirror():
    rows = [
        dict(make_row("有镜像", "http://b/c.json", health=90), id="aaa111"),
        dict(make_row("无快照", "http://c/c.json", health=85), id="bbb222"),
    ]
    data = build_duocang(rows, 60, mirror_base="https://mirror.example/mirrors",
                         mirrored_sids={"aaa111"})
    entries = {e["sourceName"]: e["sourceUrl"] for e in data["storeHouse"]}
    assert entries["有镜像"] == "https://mirror.example/mirrors/aaa111.json"
    assert entries["无快照"] == "http://c/c.json"  # 没快照回落原始地址


def test_report_contains_diff_and_table():
    report = build_report(ROWS, added={"http://new/c.json"},
                          removed={"http://old/c.json"},
                          duocang_path="output/多仓.json", duocang_count=2,
                          min_health=60)
    assert "新增 1 个" in report and "移除 1 个" in report
    assert "| 状态 |" in report
    assert "http://b/c.json" in report
    # banned 源不在榜单里，但有备注
    assert "封禁" not in report.split("## 源榜单")[1].split(">")[0]
    assert "banned" in report


def test_report_with_avail_and_failures():
    rows = [make_row("可用", "http://b/c.json")]
    rows[0]["avail"] = "93%"
    rows[0]["compat"] = "通用"
    report = build_report(rows, set(), set(), "output/多仓.json", 1, 60,
                          failure_stats=[{"reason": "DNS 解析失败",
                                          "count": 5, "sources": 2}])
    assert "93%" in report
    assert "近 30 天失败原因" in report
    assert "DNS 解析失败" in report
    assert "| 适配 |" in report and "通用" in report


def test_site_shows_compat():
    rows = [dict(make_row("站点A", "http://b/c.json"),
                 id="aaa111", l3_tested=0, l3_ok=0, dup_of="", compat="偏影视仓")]
    out = build_site(rows, {}, [], duocang_count=1, min_health=60)
    assert "偏影视仓" in out and "适配" in out


def test_previous_urls(tmp_path):
    p = tmp_path / "duo.json"
    p.write_text(json.dumps({"storeHouse": [
        {"sourceName": "x", "sourceUrl": "http://b/c.json"}]},
        ensure_ascii=False), encoding="utf-8")
    assert previous_urls(p) == {"http://b/c.json"}
    assert previous_urls(tmp_path / "missing.json") == set()


def test_format_diff_text():
    text = format_diff_text({"ts": "2026-11-08 12:00:00", "count": 3,
                             "added": ["http://a/c.json"], "removed": []})
    assert "新增 **1**" in text and "➕" in text and "http://a/c.json" in text


def test_live_pick_and_duocang_exclusion():
    rows = [
        dict(make_row("配置", "http://b/c.json", health=90),
             id="c1", schema_type="tvbox", dup_of=""),
        dict(make_row("直播A", "http://l/a.txt", health=90),
             id="l1", schema_type="live", dup_of="", live_channels=100),
        dict(make_row("直播B", "http://l/b.txt", health=70),
             id="l2", schema_type="live", dup_of="", live_channels=200),
    ]
    # 直播源不进多仓
    assert [r["url"] for r in pick_sources(rows, 60)] == ["http://b/c.json"]
    # 直播合集按频道数优先
    assert ([r["url"] for r in pick_live_sources(rows, 60)]
            == ["http://l/b.txt", "http://l/a.txt"])
    data = build_live_config(rows, 60)
    assert data["lives"][0] == {"name": "直播B", "type": 0,
                                "url": "http://l/b.txt"}


def test_webhook_payload_platforms():
    text = "## TVBox 源更新\n- 新增 **2**"
    feishu = webhook_payload("feishu", text)
    assert feishu["msg_type"] == "text"
    assert "**" not in feishu["content"]["text"]  # webhook 走纯文本
    assert "TVBox 源更新" in feishu["content"]["text"]
    ding = webhook_payload("dingtalk", text)
    assert ding["msgtype"] == "text" and "**" not in ding["text"]["content"]
    wecom = webhook_payload("wecom", text)
    assert wecom["msgtype"] == "text"


def test_build_site_html():
    rows = [dict(make_row("站点A", "http://b/c.json"),
                 id="aaa111", l3_tested=0, l3_ok=0, dup_of="")]
    out = build_site(rows, {"aaa111": [{"ok": 1}, {"ok": 0}]},
                     [{"reason": "超时", "count": 2, "sources": 1}],
                     duocang_count=1, min_health=60)
    assert "站点A" in out and "近 30 天失败原因" in out
    assert "<svg" in out and "站点A".replace("站点A", "站点A") in out
    assert out.strip().endswith("</html>")
