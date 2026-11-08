"""体检流程：L1（可达+解析）与 L2（子资源探测）。"""

from __future__ import annotations

import asyncio

import httpx

from . import classify
from .fetch import fetch
from .models import CheckOutcome, ProbeResult


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
                       latency_ms=fr.latency_ms, error=fr.error)


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
    if info.schema_type in ("invalid", "anti_crawl"):
        out.error = ("HTML 反爬页，疑似仍存活，已放宽观察"
                     if info.schema_type == "anti_crawl"
                     else "可达但解析不出 TVBox 配置结构")
        return out

    out.ok = True
    if settings["validate"]["level"].upper() == "L2":
        probes = info.probes[: settings["validate"]["max_probes"]]
        if probes:
            out.probes = list(await asyncio.gather(
                *(_probe(client, sem, u, kind, settings) for kind, u in probes)
            ))
    return out
