"""配置加载：settings.yaml 全局参数 + sources.yaml 人工种子源。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "network": {
        "timeout": 10,
        "retries": 2,
        "concurrency": 8,
        "max_bytes": 5_242_880,
        "verify_ssl": True,
        "user_agents": [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "okhttp/4.10.0",
        ],
    },
    "validate": {
        "level": "L2",
        "dead_after_failures": 3,
        "max_probes": 12,
        "probe_timeout": 8,
    },
    "score": {"weights": {"success": 0.5, "subs": 0.3, "richness": 0.2}},
    "publish": {
        "min_health": 60,
        "duocang": "output/多仓.json",
        "report": "output/报告.md",
    },
    "snapshot": {"keep": 10},
    "discover": {"max_depth": 2, "max_new_per_run": 100},
}


def _deep_merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_settings(path: Path | None) -> dict:
    """加载 settings.yaml，缺省字段回落到 DEFAULTS。"""
    data = {}
    if path and path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return _deep_merge(DEFAULTS, data)


def load_seeds(path: Path) -> list[dict]:
    """读取 sources.yaml 种子清单，忽略缺 url 的条目。"""
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    seeds = []
    for item in data.get("sources") or []:
        if isinstance(item, dict) and item.get("url"):
            seeds.append(item)
        elif isinstance(item, str):  # 允许只写 url 字符串
            seeds.append({"url": item})
    return seeds
