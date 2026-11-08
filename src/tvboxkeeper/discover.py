"""发现：递归展开库中多仓（storeHouse）的子仓。"""

from __future__ import annotations

import asyncio

import httpx

from . import classify
from .fetch import fetch
from .normalize import normalize_url
from .registry import Registry

HINT = "深度限制见 settings.yaml discover.max_depth"


async def expand(
    registry: Registry,
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    settings: dict,
) -> int:
    """把所有 storehouse/mixed 源的子仓登记进库，返回新建数。"""
    cfg = settings["discover"]
    net = settings["network"]
    parents = [
        r for r in registry.sources()
        if r["gen"] < cfg["max_depth"]
        and r["schema_type"] in ("storehouse", "mixed")
        and r["status"] in ("active", "flaky", "new")
    ]
    created_total = 0
    for parent in parents:
        if created_total >= cfg["max_new_per_run"]:
            break
        fr = await fetch(client, sem, parent["url"],
                         max_bytes=net["max_bytes"],
                         timeout=net["timeout"], retries=1)
        if fr.status == 0 or fr.status >= 400:
            continue
        info = classify.parse_config(parent["url"],
                                     classify.decode_content(fr.content))
        for child in info.children:
            if created_total >= cfg["max_new_per_run"]:
                break
            child_url = normalize_url(child["sourceUrl"])
            if not child_url.startswith(("http://", "https://")):
                continue
            _, created = registry.upsert(
                child_url,
                name=child.get("sourceName", ""),
                gen=parent["gen"] + 1,
                origin="storehouse",
                ref=parent["url"],
            )
            if created:
                created_total += 1
    return created_total
