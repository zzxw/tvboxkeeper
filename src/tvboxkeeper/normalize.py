"""URL 规范化与源 ID 计算。"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# 发布跟踪类参数，去掉后视为同一 URL
DROP_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "spm", "from", "share_token",
}

# 源配置里常见的坏写法：https://https://xxx → 压缩成单个协议头
_DUP_SCHEME_RE = re.compile(r"^(https?://)+", re.IGNORECASE)


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return url
    url = _DUP_SCHEME_RE.sub(lambda m: m.group(1), url)
    parts = urlsplit(url)
    scheme = parts.scheme.lower() or "http"
    query = urlencode(
        [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
         if k.lower() not in DROP_PARAMS]
    )
    return urlunsplit((scheme, parts.netloc, parts.path, query, ""))


def is_fetchable_url(url: str) -> bool:
    """判断 URL 能否被 httpx 直接请求（http/https + 合法主机与端口）。

    httpx 对 `host:1935:1935` 这类重复冒号的 authority 会抛 InvalidURL，
    而 InvalidURL 不是 HTTPError 子类——不在这里拦掉，脏数据会一路传到
    探测层并中断整批体检。
    """
    try:
        parts = urlsplit(url or "")
        _ = parts.port          # 访问即校验：端口非数字/越界会抛 ValueError
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.hostname)


def source_id(url: str) -> str:
    return hashlib.sha1(normalize_url(url).encode("utf-8")).hexdigest()[:16]


def guess_name(url: str) -> str:
    """从 URL 猜一个默认名称：去掉 www. 的主机名。"""
    host = urlsplit(normalize_url(url)).netloc.split("@")[-1]
    host = host.split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return host
