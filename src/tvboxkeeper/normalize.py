"""URL 规范化与源 ID 计算。"""

from __future__ import annotations

import hashlib
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# 发布跟踪类参数，去掉后视为同一 URL
DROP_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "spm", "from", "share_token",
}


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return url
    parts = urlsplit(url)
    scheme = parts.scheme.lower() or "http"
    query = urlencode(
        [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
         if k.lower() not in DROP_PARAMS]
    )
    return urlunsplit((scheme, parts.netloc, parts.path, query, ""))


def source_id(url: str) -> str:
    return hashlib.sha1(normalize_url(url).encode("utf-8")).hexdigest()[:16]


def guess_name(url: str) -> str:
    """从 URL 猜一个默认名称：去掉 www. 的主机名。"""
    host = urlsplit(normalize_url(url)).netloc.split("@")[-1]
    host = host.split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return host
