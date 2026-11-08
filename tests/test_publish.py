import json

from tvboxkeeper.publish import build_duocang, build_report, previous_urls


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
    report = build_report(rows, set(), set(), "output/多仓.json", 1, 60,
                          failure_stats=[{"reason": "DNS 解析失败",
                                          "count": 5, "sources": 2}])
    assert "93%" in report
    assert "近 30 天失败原因" in report
    assert "DNS 解析失败" in report


def test_previous_urls(tmp_path):
    p = tmp_path / "duo.json"
    p.write_text(json.dumps({"storeHouse": [
        {"sourceName": "x", "sourceUrl": "http://b/c.json"}]},
        ensure_ascii=False), encoding="utf-8")
    assert previous_urls(p) == {"http://b/c.json"}
    assert previous_urls(tmp_path / "missing.json") == set()
