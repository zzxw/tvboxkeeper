"""配置解析：识别 TVBox 单仓 / storeHouse 多仓结构。

安全红线：这里只做纯 JSON 文本解析，任何字段都不会被动态执行。
"""

from __future__ import annotations

import json
import re
from urllib.parse import urljoin

from .models import SchemaInfo

_SCHEME_RE = re.compile(r"^https?://", re.IGNORECASE)
# 真正的 TVBox 配置不可能是 HTML；返回 HTML 基本就是反爬/落地页
_HTML_RE = re.compile(r"<\s*(!doctype|html|body)", re.IGNORECASE)


def looks_like_html(text: str) -> bool:
    return bool(_HTML_RE.search(text[:2048]))


def decode_content(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def _extract_json(text: str):
    """容忍 BOM/前后杂文本/JSONP 包裹：找到第一段可解析的 JSON。"""
    try:
        return json.loads(text)
    except ValueError:
        pass
    decoder = json.JSONDecoder()
    for match in re.finditer(r"[{\[]", text):
        try:
            obj, _ = decoder.raw_decode(text[match.start():])
        except ValueError:
            continue
        if isinstance(obj, (dict, list)):
            return obj
    raise ValueError("no embedded JSON object found")


def _count(entries) -> int:
    return sum(1 for e in entries if isinstance(e, dict)) if isinstance(entries, list) else 0


def _resolve_url(value: str, base: str) -> str | None:
    value = value.strip()
    if not value or value.startswith("proxy://"):
        return None
    if _SCHEME_RE.match(value):
        return value
    if value.startswith(("./", "/")) and base:
        return urljoin(base, value)
    return None


def _spider_urls(spider, base: str) -> list[str]:
    """spider 字段形如 "url;md5;hash"，也可能是数组或相对路径。"""
    urls: list[str] = []
    items = spider if isinstance(spider, list) else [spider]
    for item in items:
        if not isinstance(item, str):
            continue
        resolved = _resolve_url(item.split(";")[0], base)
        if resolved:
            urls.append(resolved)
    return urls


def _live_urls(lives) -> list[str]:
    """lives[].url 可能用 ; 分隔多个地址。"""
    urls: list[str] = []
    if not isinstance(lives, list):
        return urls
    for life in lives:
        if not isinstance(life, dict):
            continue
        raw = life.get("url")
        if not isinstance(raw, str):
            continue
        for part in raw.split(";"):
            resolved = _resolve_url(part, "")
            if resolved:
                urls.append(resolved)
    return urls


def parse_config(url: str, text: str) -> SchemaInfo:
    """把抓到的文本解析为 SchemaInfo，解析失败返回 schema_type=invalid。"""
    try:
        data = _extract_json(text)
    except ValueError:
        if looks_like_html(text):
            return SchemaInfo(schema_type="anti_crawl")
        return SchemaInfo(schema_type="invalid")
    if not isinstance(data, dict):
        return SchemaInfo(schema_type="invalid")

    info = SchemaInfo()
    info.sites_count = _count(data.get("sites"))
    info.parses_count = _count(data.get("parses"))
    info.lives_count = _count(data.get("lives"))

    storehouse = data.get("storeHouse")
    if isinstance(storehouse, list):
        for entry in storehouse:
            if isinstance(entry, dict) and isinstance(entry.get("sourceUrl"), str) and entry["sourceUrl"].strip():
                info.children.append({
                    "sourceName": str(entry.get("sourceName") or "").strip(),
                    "sourceUrl": entry["sourceUrl"].strip(),
                })

    # 另一种主流多仓格式：顶层 "urls": [{"url": ..., "name": ...}]
    urls_multi = data.get("urls")
    if isinstance(urls_multi, list):
        for entry in urls_multi:
            if not isinstance(entry, dict):
                continue
            src_url = entry.get("url") or entry.get("sourceUrl")
            if isinstance(src_url, str) and src_url.strip():
                info.children.append({
                    "sourceName": str(entry.get("name")
                                      or entry.get("sourceName") or "").strip(),
                    "sourceUrl": src_url.strip(),
                })

    info.storehouse_count = len(info.children)

    has_structure = bool(info.sites_count or info.parses_count
                         or info.lives_count or info.storehouse_count
                         or data.get("spider"))
    if not has_structure:
        return SchemaInfo(schema_type="invalid")

    if info.storehouse_count and (info.sites_count or info.lives_count):
        info.schema_type = "mixed"
    elif info.storehouse_count:
        info.schema_type = "storehouse"
    else:
        info.schema_type = "tvbox"

    # L3 抽测候选：type 0/1 站点的 http(s) CMS api
    for site in data.get("sites") or []:
        if (isinstance(site, dict) and site.get("type") in (0, 1)
                and isinstance(site.get("api"), str)
                and _SCHEME_RE.match(site["api"])):
            info.site_apis.append(site["api"])
    info.site_apis = info.site_apis[:10]

    # 子资源探测清单：jar 优先，其次直播、壁纸
    for sp in _spider_urls(data.get("spider"), url):
        info.probes.append(("jar", sp))
    for lv in _live_urls(data.get("lives")):
        info.probes.append(("live", lv))
    wallpaper = _resolve_url(str(data.get("wallpaper") or ""), url)
    if wallpaper:
        info.probes.append(("wallpaper", wallpaper))
    return info
