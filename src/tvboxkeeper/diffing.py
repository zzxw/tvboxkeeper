"""内容级变更：比较同一源两次配置的站点构成。"""

from __future__ import annotations

from .classify import decode_content, site_labels

# 报告里单源最多列出的站点名，避免一个源刷屏
MAX_LABELS = 6


def content_change(old: bytes, new: bytes, limit: int = MAX_LABELS) -> dict:
    """比较两次快照的站点集合，返回新增/移除摘要；无变化返回空 dict。

    以 key/name/api 作站点标识（同 key 视为同一站点，改 api 也算变更）。
    直播类配置没有 sites 字段，恒返回空——它们的变更由 md5 体现。
    """
    old_set = set(site_labels(decode_content(old)))
    new_set = set(site_labels(decode_content(new)))
    added, removed = new_set - old_set, old_set - new_set
    if not added and not removed:
        return {}
    return {
        "a": len(added),
        "r": len(removed),
        "add": sorted(added)[:limit],
        "del": sorted(removed)[:limit],
    }


def describe(change: dict) -> str:
    """把变更摘要渲染成一行中文，供报告/看板复用。"""
    if not change:
        return ""
    parts = []
    if change.get("a"):
        names = "、".join(change.get("add") or [])
        parts.append(f"新增 {change['a']} 个" + (f"（{names}）" if names else ""))
    if change.get("r"):
        names = "、".join(change.get("del") or [])
        parts.append(f"移除 {change['r']} 个" + (f"（{names}）" if names else ""))
    return "；".join(parts)
