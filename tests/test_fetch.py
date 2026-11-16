import asyncio

import httpx

from tvboxkeeper.fetch import fetch


def _no_request(request):  # pragma: no cover - 走到这里说明 URL 没被拦下
    raise AssertionError(f"不应发起请求：{request.url}")


def test_invalid_port_does_not_raise():
    """重复端口的 URL 只会得到失败结果，不能让整批 asyncio.gather 抛异常。"""

    async def inner():
        async with httpx.AsyncClient(
                transport=httpx.MockTransport(_no_request)) as client:
            return await fetch(client, asyncio.Semaphore(1),
                               "http://h.example:1935:1935/c.json",
                               max_bytes=1024, timeout=1, retries=2)

    res = asyncio.run(inner())
    assert res.status == 0
    assert res.error.startswith("InvalidURL")
    assert res.content == b""


def test_valid_url_still_fetches():
    async def inner():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda r: httpx.Response(200, text="ok"))) as client:
            return await fetch(client, asyncio.Semaphore(1),
                               "http://h.example:8080/c.json",
                               max_bytes=1024, timeout=1, retries=0)

    res = asyncio.run(inner())
    assert res.status == 200
    assert res.content == b"ok"
