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


def test_report_status_breakdown_complete():
    """状态分布必须覆盖 anti_crawl / banned，否则总数对不上。"""
    rows = [
        make_row("活", "http://a/1", status="active"),
        make_row("反爬", "http://b/1", status="anti_crawl"),
        make_row("封禁", "http://c/1", status="banned", banned=1),
    ]
    report = build_report(rows, set(), set(), "output/多仓.json", 1, 60)
    header = next(line for line in report.splitlines()
                  if line.startswith("- 源总数"))
    assert "总数：3" in header
    assert "anti_crawl 1" in header and "banned 1" in header


def test_site_cards_cover_banned():
    rows = [make_row("封", "http://c/1", status="banned", banned=1)]
    out = build_site(rows, {}, [], duocang_count=0, min_health=60)
    assert "<span>banned</span>" in out


def test_duocang_limit_for_lite():
    data = build_duocang(ROWS, min_health=60, limit=1)
    assert len(data["storeHouse"]) == 1
    # 置顶优先（starred 排序在健康分之前）
    assert data["storeHouse"][0]["sourceName"] == "置顶"


def test_report_renders_change_hosts_and_lite():
    rows = ROWS + [dict(make_row("站点甲", "http://f/c.json"),
                        id="f" * 16, dup_of="", l3_tested=0, l3_ok=0)]
    report = build_report(
        rows, set(), set(), "output/多仓.json", 3, 60,
        failure_stats=[{"reason": "超时", "count": 5, "sources": 2}],
        changes=[{"name": "站点甲", "url": "http://f/c.json",
                  "change": {"a": 1, "r": 1, "add": ["新站"], "del": ["旧站"]}}],
        hosts=[{"host": "gh-proxy.com", "fails": 24, "sources": 5}],
        incremental="增量体检，30 个源内容未变跳过子资源探测",
        lite_info="精简版：`output/多仓-lite.json`（8 个源）",
    )
    assert "## 内容变化（站点级）" in report
    assert "新增 1 个（新站）" in report and "移除 1 个（旧站）" in report
    assert "## 失败主机分布（近 30 天）" in report
    assert "`gh-proxy.com` | 24 | 5" in report
    assert "本轮体检口径：增量体检" in report
    assert "精简版：`output/多仓-lite.json`" in report


def test_site_includes_trend_and_hosts():
    rows = [dict(make_row("站点A", "http://b/c.json"),
                 id="aaa111", dup_of="", l3_tested=0, l3_ok=0)]
    trend = [{"date": "2026-11-07", "total": 10, "ok": 8, "rate": 80},
             {"date": "2026-11-08", "total": 10, "ok": 9, "rate": 90},
             {"date": "2026-11-09", "total": 12, "ok": 6, "rate": 50}]
    html = build_site(rows, {}, None, 1, 60, trend=trend,
                      hosts=[{"host": "gh-proxy.com", "fails": 24, "sources": 5}],
                      lite_count=1)
    assert "整体可用率趋势 · 近 3 天" in html
    assert 'viewBox="0 0 660 150"' in html and "polyline" in html
    assert "失败主机分布" in html and "gh-proxy.com" in html
    assert "精简版 1 个源" in html
    # 单点无法成线时不渲染图表
    short = build_site(rows, {}, None, 1, 60, trend=trend[:1])
    assert "整体可用率趋势" not in short


def test_site_kind_live_vs_vod():
    from tvboxkeeper.publish import site_kind

    assert site_kind({"schema_type": "live"}) == "live"
    assert site_kind({"schema_type": "tvbox"}) == "vod"
    assert site_kind({"schema_type": "mixed"}) == "vod"
    assert site_kind({"schema_type": "storehouse"}) == "vod"
    # 未解析出结构（flaky/dead 常见空 schema_type）按非直播归类
    assert site_kind({"schema_type": ""}) == "vod"
    assert site_kind({}) == "vod"
    # 点播源内附带的 lives 字段不算「直播接口」，否则筛选几乎命中全部源
    assert site_kind({"schema_type": "tvbox", "lives_count": 17}) == "vod"


def test_site_has_kind_filter_buttons_and_row_marks():
    base = [dict(r, id=f"r{i}", dup_of="", l3_tested=0, l3_ok=0)
            for i, r in enumerate(ROWS)]
    rows = base + [
        dict(make_row("直播A", "http://l1/c.txt", sites=0, lives=0),
             id="l1" * 8, schema_type="live", live_channels=120,
             dup_of="", l3_tested=0, l3_ok=0),
        dict(make_row("直播B", "http://l2/c.txt", sites=0, lives=0),
             id="l2" * 8, schema_type="live", live_channels=30,
             dup_of="", l3_tested=0, l3_ok=0),
    ]
    html = build_site(rows, {}, None, 1, 60)
    # 封禁源不渲染：5 + 2 - 1 = 6
    assert 'data-kind="all">全部 <b>6</b>' in html
    assert 'data-kind="live">直播 <b>2</b>' in html
    assert 'data-kind="vod">非直播 <b>4</b>' in html
    # 2 个直播行 + 1 个直播按钮；4 个非直播行 + 1 个按钮
    assert html.count('data-kind="live"') == 3
    assert html.count('data-kind="vod"') == 5
    # 默认选中「全部」，筛选为纯前端逻辑
    assert 'class="fbtn active" data-kind="all"' in html
    assert "tr.dataset.kind === kind" in html
    assert 'id="cnt"' in html
    # 三个按钮的计数必须等于实际渲染行数
    assert html.count('<tr class=') == 6
