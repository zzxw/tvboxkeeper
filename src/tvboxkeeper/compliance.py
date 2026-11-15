"""合规黑名单：扫描配置内容里的敏感关键词，命中即自动封禁。

背景：发布产物在公开仓库，若收录的源内含赌博/色情推广站点，
会有平台合规风险。这里按可配置的关键词表做保守匹配——只匹配
足够具体的词（如 bet365、太阳城），不做泛化猜测，避免误伤。
"""

from __future__ import annotations

from . import classify

# 兜底关键词（settings.yaml compliance.keywords 可覆盖/追加）
DEFAULT_KEYWORDS = [
    "bet365", "hg0088", "hg3088", "hg9088", "js06", "vwin", "fun88",
    "太阳城", "澳门威尼斯", "凯时娱乐", "乐天堂", "六合彩",
    "麻豆传媒", "花季传媒", "十八禁",
]


def keywords_of(settings: dict) -> list[str]:
    cfg = settings.get("compliance") or {}
    if not cfg.get("enabled", True):
        return []
    return [str(k).lower() for k in (cfg.get("keywords") or DEFAULT_KEYWORDS)]


def scan(content: bytes, settings: dict) -> str | None:
    """扫描内容，返回命中的第一个关键词；未命中返回 None。"""
    kws = keywords_of(settings)
    if not kws or not content:
        return None
    text = classify.decode_content(content).lower()
    for kw in kws:
        if kw in text:
            return kw
    return None
