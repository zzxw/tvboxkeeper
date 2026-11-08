"""发布：storeHouse 多仓 JSON + Markdown 体检榜单。"""

from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path

from .normalize import guess_name


def build_duocang(rows: list[dict], min_health: float) -> dict:
    """只收录健康分达标的 active 源，starred 优先、健康分降序。"""
    picked = [
        r for r in rows
        if r.get("status") == "active"
        and r.get("health_score", 0) >= min_health
        and not r.get("banned")
    ]
    picked.sort(key=lambda r: (-r.get("starred", 0), -r.get("health_score", 0)))
    return {
        "storeHouse": [
            {
                "sourceName": r.get("name") or guess_name(r["url"]),
                "sourceUrl": r["url"],
            }
            for r in picked
        ]
    }


def previous_urls(path: Path) -> set[str]:
    """读取上次发布的多仓，用于 diff。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {e.get("sourceUrl", "") for e in data.get("storeHouse", [])}
    except (OSError, ValueError):
        return set()


def build_report(
    rows: list[dict],
    added: set[str],
    removed: set[str],
    duocang_path: str,
    duocang_count: int,
    min_health: float,
    failure_stats: list[dict] | None = None,
) -> str:
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    by_status = Counter(r["status"] for r in rows)
    lines = [
        "# TVBox 源体检报告",
        "",
        f"- 生成时间：{now}",
        f"- 源总数：{len(rows)}"
        f"（active {by_status.get('active', 0)}"
        f" / flaky {by_status.get('flaky', 0)}"
        f" / dead {by_status.get('dead', 0)}"
        f" / invalid {by_status.get('invalid', 0)}"
        f" / new {by_status.get('new', 0)}）",
        f"- 订阅门槛：健康分 ≥ {min_health}",
        "",
        "## 本轮变化（相对上次发布的多仓）",
        "",
    ]
    if added:
        lines.append(f"- 新增 {len(added)} 个：")
        lines += [f"  - `{u}`" for u in sorted(added)]
    if removed:
        lines.append(f"- 移除 {len(removed)} 个：")
        lines += [f"  - `{u}`" for u in sorted(removed)]
    if not added and not removed:
        lines.append("- 无变化")

    lines += [
        "",
        "## 推荐订阅",
        "",
        f"多仓文件：`{duocang_path}`（{duocang_count} 个源），"
        "在 TVBox「配置地址」中填入该文件地址（或其 raw 链接）即可导入。",
        "",
    ]

    if failure_stats:
        lines += [
            "## 近 30 天失败原因",
            "",
            "| 原因 | 次数 | 涉及源数 |",
            "|---|---|---|",
        ]
        for f in failure_stats[:10]:
            lines.append(f"| {f['reason']} | {f['count']} | {f['sources']} |")
        lines.append("")

    lines += [
        "## 源榜单",
        "",
        "| 状态 | 名称 | 站点 | 直播 | 子仓 | 探测 | 健康分 | 30天可用 | 延迟ms | 最近检查 | URL |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in sorted((r for r in rows if not r.get("banned")),
                    key=lambda x: (-x.get("starred", 0), -x.get("health_score", 0))):
        name = (r.get("name") or "").replace("|", "\\|")[:28]
        probe = (f"{r['sub_ok']}/{r['sub_total']}"
                 if r.get("sub_total") else "-")
        avail = r.get("avail") or "-"
        lines.append(
            f"| {r['status']} | {name} | {r['sites_count']} | {r['lives_count']} "
            f"| {r['storehouse_count']} | {probe} | {r['health_score']} "
            f"| {avail} | {r['latency_ms']} | {r.get('last_checked') or '-'} "
            f"| `{r['url'].replace('|', '\\|')}` |"
        )
    banned_n = len([r for r in rows if r.get("banned")])
    if banned_n:
        lines += ["", f"> 另有 {banned_n} 个 banned 源未列出。"]
    lines.append("")
    return "\n".join(lines)


def write_duocang(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
