import asyncio

import httpx

from tvboxkeeper.config import load_settings
from tvboxkeeper.validate import _content_ok, check_source

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
