"""GitHub 挖源：搜索仓库/代码 → 探测常见配置文件名 → 登记进源库。

限速与合规：
- 未带 token 时 Search API 限 10 次/分钟，搜索调用之间强制间隔；
- raw.githubusercontent.com 的文件抓取不走 API 配额，但受单轮探测上限约束；
- 绝不执行下载内容，只做纯 JSON 解析，验证通过才登记。
"""

from __future__ import annotations

import time

import httpx

from . import classify
from .models import SchemaInfo
from .normalize import normalize_url

API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"

VALID_SCHEMAS = ("tvbox", "storehouse", "mixed")


class Hunter:
    def __init__(self, client: httpx.Client, registry, settings: dict, token: str = ""):
        self.client = client
        self.reg = registry
        self.cfg = settings["github"]
        self.token = token
        self.fetched = 0
        self.notes: list[str] = []

    def _log(self, msg: str) -> None:
        self.notes.append(msg)

    # ---------- 搜索 ----------

    def _search(self, path: str, params: dict) -> dict | None:
        resp = self.client.get(f"{API}{path}", params=params)
        if resp.status_code in (403, 429):
            self._log("GitHub 搜索触发限流，跳过后续搜索（建议配置 token）")
            return None
        if resp.status_code != 200:
            self._log(f"GitHub 搜索异常 HTTP {resp.status_code}")
            return None
        return resp.json()

    def _raw_base(self) -> str:
        """raw 地址前缀，可配镜像加速（国内直连 raw.githubusercontent.com 常不通）。"""
        mirror = (self.cfg.get("raw_mirror") or "").strip().rstrip("/")
        return f"{mirror}/{RAW}" if mirror else RAW

    def search_repos(self) -> list[dict]:
        repos: dict[str, dict] = {}
        for i, query in enumerate(self.cfg["repo_queries"]):
            if i:
                time.sleep(self.cfg["search_interval"])
            payload = self._search("/search/repositories", {
                "q": query, "sort": "updated", "per_page": 20,
            })
            if payload is None:
                break
            for item in payload.get("items") or []:
                full = item.get("full_name")
                if full:
                    repos[full] = {
                        "full_name": full,
                        "branch": item.get("default_branch") or "main",
                    }
        return list(repos.values())[: self.cfg["max_repos_per_run"]]

    def code_candidates(self) -> list[str]:
        """需要 token 的代码搜索：直接命中包含 storeHouse 的 JSON 文件。"""
        if not (self.token and self.cfg.get("code_query")):
            return []
        payload = self._search("/search/code", {
            "q": self.cfg["code_query"], "per_page": 20,
        })
        if payload is None:
            return []
        out = []
        for item in payload.get("items") or []:
            html_url = item.get("html_url") or ""
            # https://github.com/o/r/blob/branch/path -> raw 地址
            if "/blob/" in html_url:
                out.append(html_url.replace(
                    "https://github.com/", f"{self._raw_base()}/", 1
                ).replace("/blob/", "/", 1))
        return out

    # ---------- 探测与登记 ----------

    def _try_url(self, url: str) -> SchemaInfo | None:
        if self.fetched >= self.cfg["max_fetches_per_run"]:
            return None
        self.fetched += 1
        try:
            resp = self.client.get(url)
        except httpx.HTTPError:
            return None
        if resp.status_code >= 400 or not resp.content:
            return None
        info = classify.parse_config(url, classify.decode_content(resp.content))
        return info if info.schema_type in VALID_SCHEMAS else None

    def hunt(self) -> dict:
        stats = {"repos": 0, "probes": 0, "registered": 0}
        hunted: list[str] = []

        for repo in self.search_repos():
            full = repo["full_name"]
            if self.reg.hunted_recently(full, self.cfg["rehunt_days"]):
                continue
            hunted.append(full)
            stats["repos"] += 1
            for fname in self.cfg["candidate_files"]:
                url = f"{self._raw_base()}/{full}/{repo['branch']}/{fname}"
                info = self._try_url(url)
                if info is None:
                    continue
                _, created = self.reg.upsert(
                    normalize_url(url), origin="github", ref=full)
                stats["registered"] += int(created)

        self.reg.note_hunt_batch(hunted)

        for raw_url in self.code_candidates():
            info = self._try_url(raw_url)
            if info is None:
                continue
            _, created = self.reg.upsert(
                normalize_url(raw_url), origin="github_code", ref=raw_url)
            stats["registered"] += int(created)

        stats["probes"] = self.fetched
        return stats
