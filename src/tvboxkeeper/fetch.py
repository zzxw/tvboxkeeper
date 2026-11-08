"""HTTP 抓取：限速并发由调用方的 Semaphore 控制，失败自动重试。

安全约定：抓到的内容一律按不可信数据处理，只返回原始字节，
解析交给 classify（纯 JSON 解析，绝不执行任何动态代码）。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import httpx


@dataclass
class FetchResult:
    url: str
    status: int = 0
    content: bytes = b""
    latency_ms: int = 0
    error: str = ""


def make_headers(user_agent: str) -> dict[str, str]:
    return {
        "User-Agent": user_agent,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }


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

    for attempt in range(1, retries + 2):
        try:
            async with sem:
                async with client.stream(
                    "GET", url, headers=headers, timeout=timeout
                ) as resp:
                    buf = bytearray()
                    async for chunk in resp.aiter_bytes(65536):
                        buf.extend(chunk)
                        if len(buf) >= max_bytes:
                            break
                    latency = int((time.monotonic() - start) * 1000)
                    status = resp.status_code
                    return FetchResult(
                        url=url,
                        status=status,
                        content=bytes(buf[:max_bytes]),
                        latency_ms=latency,
                        error="" if status < 400 else f"HTTP {status}",
                    )
        except (httpx.HTTPError, OSError, asyncio.TimeoutError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt > retries:
                return FetchResult(
                    url=url,
                    status=0,
                    latency_ms=int((time.monotonic() - start) * 1000),
                    error=last_error,
                )
            await asyncio.sleep(min(1.0 * attempt, 3.0))

    return FetchResult(url=url, error=last_error)  # pragma: no cover
