"""HTTP 抓取：限速并发由调用方的 Semaphore 控制，失败自动重试。

安全约定：抓到的内容一律按不可信数据处理，只返回原始字节，
解析交给 classify（纯 JSON 解析，绝不执行任何动态代码）。
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx


@dataclass
class FetchResult:
    url: str
    status: int = 0
    content: bytes = b""
    latency_ms: int = 0
    error: str = ""


# 只匹配「证书链校验失败」这一类可降级错误。
# 刻意不含 handshake failure / 协议版本不符：那是服务端 TLS 栈过旧，
# 关掉校验也照样连不上，降级只会掩盖真实原因。
CERT_VERIFY_HINTS = (
    "certificate_verify_failed",
    "certificate verify failed",
    "unable to get local issuer certificate",
    "self-signed certificate",
    "self signed certificate",
    "certificate has expired",
    "hostname mismatch",
    "ip address mismatch",
    "doesn't match",
)


def is_cert_error(error: str) -> bool:
    """判断错误信息是否为证书校验失败（可安全降级复检的那一类）。"""
    low = (error or "").lower()
    return any(hint in low for hint in CERT_VERIFY_HINTS)


def make_headers(user_agent: str) -> dict[str, str]:
    return {
        "User-Agent": user_agent,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }


class HostLimiter:
    """按主机名限制并发与请求间隔，避免同一站点被打爆（误判 dead / 封 IP）。

    只约束调度，不改变单次请求的超时与重试语义。interval 是同一主机
    两次请求开始之间的最小间隔（秒），0 表示不额外等待。
    """

    def __init__(self, concurrency: int = 2, interval: float = 0.0):
        self.concurrency = max(1, int(concurrency))
        self.interval = max(0.0, float(interval))
        self._sems: dict[str, asyncio.Semaphore] = {}
        self._last: dict[str, float] = {}

    def _sem(self, host: str) -> asyncio.Semaphore:
        sem = self._sems.get(host)
        if sem is None:
            sem = self._sems[host] = asyncio.Semaphore(self.concurrency)
        return sem

    @asynccontextmanager
    async def slot(self, url: str):
        """占用某主机的请求名额：同主机并发不超过 concurrency。"""
        host = urlsplit(url).hostname or ""
        async with self._sem(host):
            if self.interval:
                wait = self._last.get(host, 0.0) + self.interval - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last[host] = time.monotonic()
            yield


# 重试策略：404/410 及其余 4xx 重试无意义（配置/权限/反爬），直接返回；
# 429 单独按 Retry-After 等待；408/425/5xx 与网络错误走退避重试。
_RETRY_STATUS = frozenset({408, 425})
MAX_RETRY_AFTER = 10.0


def _retry_after(value: str | None) -> float | None:
    """解析 Retry-After 的秒数写法；HTTP-date 或非法值返回 None。"""
    try:
        seconds = float((value or "").strip())
    except ValueError:
        return None
    return min(max(seconds, 0.0), MAX_RETRY_AFTER)


async def fetch(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    url: str,
    *,
    max_bytes: int,
    timeout: float,
    retries: int,
    probe_bytes: int = 0,
) -> FetchResult:
    """抓取 URL。

    probe_bytes > 0 时携带 Range 头只取前 N 字节（子资源探测用），
    并始终限制读取量，避免下载大文件。
    """
    start = time.monotonic()
    headers = {"Range": f"bytes=0-{probe_bytes - 1}"} if probe_bytes else None
    last_error = "unknown"
    last_status = 0

    for attempt in range(1, retries + 2):
        attempt_start = time.monotonic()  # 延迟只统计本次请求，不含重试等待
        wait: float | None = None
        try:
            async with sem:
                async with client.stream(
                    "GET", url, headers=headers, timeout=timeout
                ) as resp:
                    status = last_status = resp.status_code
                    latency = int((time.monotonic() - attempt_start) * 1000)
                    if (400 <= status < 500 and status != 429
                            and status not in _RETRY_STATUS):
                        # 404/410 及 400/401/403 等：重试不会改变结果，省配额
                        return FetchResult(url=url, status=status,
                                           latency_ms=latency,
                                           error=f"HTTP {status}")
                    if status == 429 or status >= 500 or status in _RETRY_STATUS:
                        # 错误体没有分析价值，不下载；429 尊重服务端 Retry-After
                        last_error = f"HTTP {status}"
                        if status == 429:
                            wait = _retry_after(resp.headers.get("Retry-After"))
                    else:
                        buf = bytearray()
                        async for chunk in resp.aiter_bytes(65536):
                            buf.extend(chunk)
                            if len(buf) >= max_bytes:
                                break
                        return FetchResult(
                            url=url,
                            status=status,
                            content=bytes(buf[:max_bytes]),
                            latency_ms=latency,
                            error="" if status < 400 else f"HTTP {status}",
                        )
        except httpx.InvalidURL as exc:
            # URL 本身非法（如 host:1935:1935）：重试没有意义，直接判失败。
            # InvalidURL 不是 HTTPError 子类，必须单独捕获，否则单条脏 URL
            # 会让整个 asyncio.gather 抛异常、中断整批跑批。
            return FetchResult(url=url, status=0,
                               latency_ms=int((time.monotonic() - start) * 1000),
                               error=f"{type(exc).__name__}: {exc}")
        except (httpx.HTTPError, OSError, asyncio.TimeoutError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            last_status = 0

        if attempt > retries:
            break
        await asyncio.sleep(wait if wait is not None else min(1.0 * attempt, 3.0))

    return FetchResult(url=url, status=last_status,
                       latency_ms=int((time.monotonic() - start) * 1000),
                       error=last_error)
