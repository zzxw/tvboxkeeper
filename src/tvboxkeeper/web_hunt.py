"""网页挖源：从指定页面按正则提取配置地址并登记。

安全红线：只做 GET + 纯文本正则提取，绝不执行页面脚本；提取到的地址
必须先过 ``is_fetchable_url`` 才登记，内容是否可用仍交给体检环节判断。
"""

from __future__ import annotations

import re

import httpx

from . import classify
from .normalize import is_fetchable_url, normalize_url


class WebHunter:
    """按 ``settings["web"]`` 抓页面、提地址、登记源。"""

    def __init__(self, client: httpx.Client, registry, settings: dict):
        self.client = client
        self.reg = registry
        self.cfg = settings.get("web") or {}
        # 限制表达式长度：配置写错（超长/灾难性回溯）时不至于拖死进程
        pattern = str(self.cfg.get("pattern") or "")
        self.regex = re.compile(pattern) if 0 < len(pattern) <= 512 else None

    def _match(self, text: str) -> list[str]:
        """取出候选地址；表达式带分组时 findall 返回元组，这里统一摊平。"""
        if self.regex is None:
            return []
        out: list[str] = []
        for item in self.regex.findall(text):
            if isinstance(item, tuple):
                out.append(next((g for g in item if g), ""))
            else:
                out.append(item)
        return out

    def run(self) -> dict:
        """抓取配置里的页面并登记新源，返回本轮统计。"""
        stats = {"pages": 0, "found": 0, "registered": 0, "failed": 0}
        if not self.cfg.get("enabled") or self.regex is None:
            return stats
        max_new = int(self.cfg.get("max_new_per_run", 20))
        max_bytes = int(self.cfg.get("max_bytes", 2_000_000))
        seen: set[str] = set()
        for page in self.cfg.get("pages") or []:
            if stats["registered"] >= max_new:
                break
            if not isinstance(page, str) or not is_fetchable_url(page.strip()):
                continue
            page = page.strip()
            try:
                resp = self.client.get(page)
            except (httpx.HTTPError, httpx.InvalidURL):
                stats["failed"] += 1
                continue
            stats["pages"] += 1
            if resp.status_code >= 400:
                stats["failed"] += 1
                continue
            text = classify.decode_content(resp.content[:max_bytes])
            for raw in self._match(text):
                if stats["registered"] >= max_new:
                    break
                # 行文里的地址常带收尾标点/引号，先剥掉再校验
                candidate = raw.strip().rstrip("),;'\"")
                if not candidate or not is_fetchable_url(candidate):
                    continue
                url = normalize_url(candidate)
                if url in seen:
                    continue
                seen.add(url)
                stats["found"] += 1
                _, created = self.reg.upsert(url, origin="web", ref=page)
                if created:
                    stats["registered"] += 1
        return stats
