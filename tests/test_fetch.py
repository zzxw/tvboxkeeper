import asyncio
import time

import httpx

from tvboxkeeper.fetch import HostLimiter, fetch


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


def _fetch_with(handler, retries=0):
    async def inner():
        async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler)) as client:
            return await fetch(client, asyncio.Semaphore(1),
                               "http://h.example/c.json",
                               max_bytes=1024, timeout=1, retries=retries)
    return asyncio.run(inner())


def test_404_is_not_retried():
    """404/410 重试无意义，只应请求一次。"""
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(404)

    res = _fetch_with(handler, retries=3)
    assert res.status == 404 and res.error == "HTTP 404"
    assert len(calls) == 1


def test_403_is_not_retried():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(403)

    res = _fetch_with(handler, retries=3)
    assert res.status == 403 and len(calls) == 1


def test_429_honors_retry_after_then_recovers():
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, text="ok")

    res = _fetch_with(handler, retries=1)
    assert res.status == 200 and res.content == b"ok"
    assert len(calls) == 2


def test_500_retries_then_reports_http_error():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(500)

    res = _fetch_with(handler, retries=1)
    assert res.status == 500 and res.error == "HTTP 500" and res.content == b""
    assert len(calls) == 2


def test_host_limiter_caps_same_host_concurrency():
    """同一主机的并发不超过配置值，避免把单站打爆。"""

    async def inner():
        gate = HostLimiter(concurrency=2)
        active = peak = 0

        async def job(i):
            nonlocal active, peak
            async with gate.slot(f"http://same.example.com/{i}"):
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.02)
                active -= 1

        await asyncio.gather(*(job(i) for i in range(6)))
        return peak

    assert asyncio.run(inner()) == 2


def test_host_limiter_allows_parallel_across_hosts():
    async def inner():
        gate = HostLimiter(concurrency=1)
        active = peak = 0

        async def job(host):
            nonlocal active, peak
            async with gate.slot(f"http://{host}/x"):
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.02)
                active -= 1

        await asyncio.gather(job("a.example.com"), job("b.example.com"))
        return peak

    assert asyncio.run(inner()) == 2


def test_host_limiter_interval_spaces_requests():
    gate = HostLimiter(concurrency=1, interval=0.05)

    async def inner():
        async def job():
            async with gate.slot("http://a.example.com/x"):
                pass

        start = time.monotonic()
        await job()
        await job()
        return time.monotonic() - start

    assert asyncio.run(inner()) >= 0.045
