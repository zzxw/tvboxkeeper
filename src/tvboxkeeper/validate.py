"""体检流程：L1（可达+解析）与 L2（子资源探测）。"""

from __future__ import annotations

import asyncio

import httpx

from . import classify
from .fetch import fetch
from .models import CheckOutcome, ProbeResult


def _content_ok(kind: str, body: bytes, status: int) -> bool:
    """子资源内容特征校验：防止 200 状态但实际是 HTML 错误页的误判。"""
    if not (200 <= status < 400):
        return False
    if kind == "jar":
        return body[:2] == b"PK"  # jar = zip
    if kind == "live":
        low = body.lower()
        return bool(body) and (b"#genre#" in low or b"#extm3u" in low
                               or b"http" in low or b"rtsp" in low)
    return True  # wallpaper 等只看状态


async def _probe(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    url: str,
    kind: str,
    settings: dict,
) -> ProbeResult:
    v = settings["validate"]
    fr = await fetch(
        client, sem, url,
        max_bytes=65536,
        timeout=v["probe_timeout"],
        retries=1,
        probe_bytes=4096,
    )
    return ProbeResult(url=url, kind=kind, status=fr.status,
                       latency_ms=fr.latency_ms, error=fr.error,
                       content_ok=_content_ok(kind, fr.content, fr.status))


async def _probe_site_api(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    api: str,
    settings: dict,
) -> tuple[int, str]:
    """L3：请求 CMS 列表接口 ?ac=list，按返回内容宽松判定可用。"""
    l3 = settings["validate"].get("l3") or {}
    url = api + ("&" if "?" in api else "?") + "ac=list"
    fr = await fetch(client, sem, url, max_bytes=65536,
                     timeout=l3.get("timeout", 8), retries=0)
    body = fr.content
    ok = (200 <= fr.status < 400
          and (b'"list"' in body or b'"code"' in body or b"<list" in body.lower()))
    return (1 if ok else 0), url


async def check_source(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    url: str,
    settings: dict,
) -> CheckOutcome:
    """体检单个源：L1 必做，L2 按档位开关。"""
    net = settings["network"]
    fr = await fetch(
        client, sem, url,
        max_bytes=net["max_bytes"],
        timeout=net["timeout"],
        retries=net["retries"],
    )
    out = CheckOutcome(url=url, status=fr.status,
                       latency_ms=fr.latency_ms, error=fr.error)
    if fr.status == 0 or fr.status >= 400:
        out.error = fr.error or f"HTTP {fr.status}"
        return out

    text = classify.decode_content(fr.content)
    info = classify.parse_config(url, text)
    out.schema = info
    out.content = fr.content
    if info.schema_type in ("invalid", "anti_crawl", "empty"):
        out.error = {
            "anti_crawl": "HTML 反爬页，疑似仍存活，已放宽观察",
            "empty": "响应为空（疑似反爬或接口异常）",
        }.get(info.schema_type, "可达但解析不出 TVBox 配置结构")
        return out

    out.ok = True
    level = settings["validate"]["level"].upper()
    if level in ("L2", "L3"):
        probes = info.probes[: settings["validate"]["max_probes"]]
        if probes:
            out.probes = list(await asyncio.gather(
                *(_probe(client, sem, u, kind, settings) for kind, u in probes)
            ))
    if level == "L3":
        l3 = settings["validate"].get("l3") or {}
        apis = info.site_apis[: l3.get("sample_sites", 3)]
        if apis:
            results = await asyncio.gather(
                *(_probe_site_api(client, sem, a, settings) for a in apis))
            out.l3_tested = len(results)
            out.l3_ok = sum(ok for ok, _ in results)
    return out
