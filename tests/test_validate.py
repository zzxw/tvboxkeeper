import asyncio
import hashlib
import time

import httpx

from tvboxkeeper.config import load_settings
from tvboxkeeper.validate import _content_ok, check_source, should_skip_probes

CONFIG_URL = "http://e.com/c.json"
API_URL = "http://s.example/api.php"

SETTINGS = load_settings(None)


def test_content_ok_channel():
    assert _content_ok("channel", b"#EXTM3U\nhttp://x/1.ts", 200)
    assert _content_ok("channel", b"\x00\x00\x00\x18ftypisom", 200)  # 二进制流
    assert not _content_ok("channel", b"<html>err</html>", 200)  # 200 但非流
    assert not _content_ok("channel", b"", 200)


def test_content_ok_jar_magic():
    assert _content_ok("jar", b"PK\x03\x04rest-of-zip", 200)
    assert not _content_ok("jar", b"<html>error page</html>", 200)  # 200 但不是 jar
    assert not _content_ok("jar", b"PK\x03\x04", 500)


def test_content_ok_live_features():
    assert _content_ok("live", "央视频道,#genre#\nCCTV1,http://x".encode("utf-8"), 200)
    assert _content_ok("live", b"#EXTM3U\nhttp://x/live.m3u8", 200)
    assert not _content_ok("live", b"<html></html>", 200)  # 200 但不是直播列表
    assert not _content_ok("live", b"", 200)


def _handler(request) -> httpx.Response:
    url = str(request.url)
    if url == CONFIG_URL:
        return httpx.Response(200, json={
            "sites": [{"type": 0, "api": API_URL}]})
    if url.startswith(API_URL):
        return httpx.Response(200, json={"code": 1, "list": []})
    return httpx.Response(404)


def _run_check(settings):
    async def inner():
        async with httpx.AsyncClient(
                transport=httpx.MockTransport(_handler)) as client:
            sem = asyncio.Semaphore(4)
            return await check_source(client, sem, CONFIG_URL, settings)
    return asyncio.run(inner())


def test_l2_no_site_sampling():
    out = _run_check(SETTINGS)  # level=L2
    assert out.ok
    assert out.l3_tested == 0


def test_l3_site_sampling():
    settings = load_settings(None)
    settings["validate"]["level"] = "L3"
    out = _run_check(settings)
    assert out.ok
    assert out.l3_tested == 1 and out.l3_ok == 1


def test_l3_site_fail_counted():
    settings = load_settings(None)
    settings["validate"]["level"] = "L3"

    def dead_api(request) -> httpx.Response:
        if str(request.url) == CONFIG_URL:
            return httpx.Response(200, json={
                "sites": [{"type": 0, "api": API_URL}]})
        return httpx.Response(200, text="<html>挂了</html>")

    async def inner():
        async with httpx.AsyncClient(
                transport=httpx.MockTransport(dead_api)) as client:
            sem = asyncio.Semaphore(4)
            return await check_source(client, sem, CONFIG_URL, settings)

    out = asyncio.run(inner())
    assert out.l3_tested == 1 and out.l3_ok == 0
    assert out.level == "L3"  # 实际做了 L3 抽测，不能被降级记成 L2


def test_level_reflects_executed_depth():
    """配置无子资源时 L2 也只剩 L1 的实际动作；有子资源才记 L2。"""
    assert _run_check(SETTINGS).level == "L1"  # 样例配置无 jar/直播

    live_url = "http://l.example/live.txt"

    def handler(request) -> httpx.Response:
        if str(request.url) == CONFIG_URL:
            return httpx.Response(200, json={
                "sites": [], "lives": [{"url": live_url}]})
        return httpx.Response(200, text="央视,#genre#\nCCTV1,http://l/1.ts")

    async def inner():
        async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler)) as client:
            return await check_source(
                client, asyncio.Semaphore(4), CONFIG_URL, SETTINGS)

    out = asyncio.run(inner())
    assert out.probes and out.level == "L2"


BODY = ('{"spider": "http://e.com/spider.jar", '
        '"sites": [{"type": 0, "api": "http://s.example/api.php"}]}').encode()
JAR = "http://e.com/spider.jar"


def _jar_handler(request) -> httpx.Response:
    url = str(request.url)
    if url == CONFIG_URL:
        return httpx.Response(200, content=BODY)
    if url == JAR:
        return httpx.Response(200, content=b"PK\x03\x04jar")
    return httpx.Response(200, json={"code": 1, "list": []})


def _check(settings, handler=_jar_handler, **kwargs):
    async def inner():
        async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler)) as client:
            return await check_source(client, asyncio.Semaphore(4), CONFIG_URL,
                                      settings, **kwargs)
    return asyncio.run(inner())


def test_should_skip_probes_window():
    settings = load_settings(None)
    md5 = hashlib.md5(BODY).hexdigest()
    fresh = time.strftime("%Y-%m-%d %H:%M:%S")
    assert should_skip_probes({"content_md5": md5, "probe_ts": fresh},
                              BODY, settings)
    old = time.strftime("%Y-%m-%d %H:%M:%S",
                        time.localtime(time.time() - 10 * 86400))
    assert not should_skip_probes({"content_md5": md5, "probe_ts": old},
                                  BODY, settings)
    assert not should_skip_probes({"content_md5": "deadbeef",
                                   "probe_ts": fresh}, BODY, settings)
    assert not should_skip_probes(None, BODY, settings)
    off = load_settings(None)
    off["validate"]["incremental"]["enabled"] = False
    assert not should_skip_probes({"content_md5": md5, "probe_ts": fresh},
                                  BODY, off)


def test_incremental_skips_probe_when_content_unchanged():
    settings = load_settings(None)
    md5 = hashlib.md5(BODY).hexdigest()
    prev = {"content_md5": md5,
            "probe_ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    out = _check(settings, prev=prev)
    assert out.ok and out.skipped_probes and not out.probes
    assert out.level == "L1"  # 没有真正探测，不应记为 L2


def test_incremental_does_not_skip_l3():
    settings = load_settings(None)
    md5 = hashlib.md5(BODY).hexdigest()
    prev = {"content_md5": md5,
            "probe_ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    out = _check(settings, level="L3", prev=prev)
    assert not out.skipped_probes and out.l3_tested >= 1


def test_l3_targets_respects_quota_and_health():
    from tvboxkeeper.cli import l3_targets

    settings = load_settings(None)
    rows = [
        {"url": f"http://s{i}.com/c.json", "status": "active", "banned": 0,
         "dup_of": "", "schema_type": "tvbox", "health_score": 90 + i}
        for i in range(8)
    ] + [{"url": "http://low.com/c.json", "status": "active", "banned": 0,
          "dup_of": "", "schema_type": "tvbox", "health_score": 50}]
    picked = l3_targets(rows, settings)
    assert len(picked) == settings["validate"]["l3"]["daily_quota"] == 5
    assert "http://low.com/c.json" not in picked
    # 关闭后不再抽样
    settings["validate"]["l3"]["enabled"] = False
    assert l3_targets(rows, settings) == set()
