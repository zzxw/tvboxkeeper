"""网页挖源：从指定页面按正则提取配置地址并登记。

安全红线：只做 GET + 纯文本正则提取，绝不执行页面脚本；提取到的地址
必须先过 ``is_fetchable_url`` 才登记，内容是否可用仍交给体检环节判断。
"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit

import httpx

from . import classify
from .normalize import is_fetchable_url, normalize_url

# 页面内链接：只取 href 属性，交给 urljoin 解析相对地址
_HREF_RE = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
# sitemap 的 <loc> 条目
_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)


class WebHunter:
    """按 ``settings["web"]`` 抓页面、提地址、登记源。"""

    def __init__(self, client: httpx.Client, registry, settings: dict):
        self.client = client
        self.reg = registry
        self.cfg = settings.get("web") or {}
        # 限制表达式长度：配置写错（超长/灾难性回溯）时不至于拖死进程
        pattern = str(self.cfg.get("pattern") or "")
        self.regex = re.compile(pattern) if 0 < len(pattern) <= 512 else None
        # depth=0 只扫配置页；depth=1 再跟进同域链接一层
        self.depth = max(0, int(self.cfg.get("depth", 0)))
        self.max_pages = max(1, int(self.cfg.get("max_pages_per_run", 20)))

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

    def _links(self, text: str, base: str, sitemap: bool) -> list[str]:
        """页面内可跟进的链接：sitemap 取 <loc>，其余取同域 href。"""
        if sitemap:
            return [urljoin(base, loc) for loc in _LOC_RE.findall(text)]
        host = urlsplit(base).hostname or ""
        out: list[str] = []
        for href in _HREF_RE.findall(text):
            href = href.strip()
            if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue
            target = urljoin(base, href).split("#")[0]
            # 只跟同域：避免把「挖源」退化成全网爬虫
            if (urlsplit(target).hostname or "") != host:
                continue
            if is_fetchable_url(target):
                out.append(target)
        return out

    @staticmethod
    def _is_sitemap(page: str, text: str) -> bool:
        low = page.lower()
        if low.endswith((".xml", ".xml.gz")) or "sitemap" in low:
            return True
        head = text[:2000].lower()
        return "<urlset" in head or "<sitemapindex" in head

    def run(self) -> dict:
        """抓取配置里的页面（可跟进一层同域链接）并登记新源，返回本轮统计。"""
        stats = {"pages": 0, "found": 0, "registered": 0, "failed": 0}
        if not self.cfg.get("enabled") or self.regex is None:
            return stats
        max_new = int(self.cfg.get("max_new_per_run", 20))
        max_bytes = int(self.cfg.get("max_bytes", 2_000_000))
        seen: set[str] = set()
        visited: set[str] = set()
        queue: list[tuple[str, int]] = []
        for page in self.cfg.get("pages") or []:
            if not isinstance(page, str) or not is_fetchable_url(page.strip()):
                continue
            queue.append((page.strip(), 0))

        while queue and stats["pages"] < self.max_pages:
            if stats["registered"] >= max_new:
                break
            page, level = queue.pop(0)
            if page in visited:
                continue
            visited.add(page)
            try:
                resp = self.client.get(page)
            except (httpx.HTTPError, httpx.InvalidURL):
                stats["failed"] += 1
                continue
            if resp.status_code >= 400:
                stats["failed"] += 1
                continue
            stats["pages"] += 1
            text = classify.decode_content(resp.content[:max_bytes])
            sitemap = self._is_sitemap(page, text)
            if sitemap:
                # sitemap 自身不含配置地址，只把 <loc> 当作待扫页面（层级不变）
                for link in self._links(text, page, sitemap=True):
                    if is_fetchable_url(link) and link not in visited:
                        queue.append((link, level))
                continue

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

            if level < self.depth:
                for link in self._links(text, page, sitemap=False):
                    if link not in visited:
                        queue.append((link, level + 1))
        return stats
