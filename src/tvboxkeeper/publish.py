"""发布：storeHouse 多仓 JSON + Markdown 体检榜单 + 静态网页。"""

from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import html
import io
import json
import os
import re
import time
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote_plus

from . import classify
from . import diffing
from .normalize import guess_name

# 状态展示顺序：存活 → 慢速 → 观察中 → 结构/网络异常 → 人工处置
STATUS_ORDER = ("active", "slow", "flaky", "anti_crawl", "invalid", "dead",
                "new", "banned")


def status_breakdown(rows: list[dict]) -> str:
    """完整状态分布：按固定顺序列出非零项，未知状态追加在后，避免统计口径漏项。"""
    counts = Counter(r["status"] for r in rows)
    parts = [f"{s} {counts[s]}" for s in STATUS_ORDER if counts.get(s)]
    parts += [f"{s} {n}" for s, n in sorted(counts.items())
              if s not in STATUS_ORDER and n]
    return " / ".join(parts) or "—"


def pick_sources(rows: list[dict], min_health: float) -> list[dict]:
    """筛出可进入多仓订阅的配置源：active/slow + 达标 + 未封禁 + 非重复 + 非直播。

    slow 仍可发布，但排序上整体排在 active 之后（降权不剔除）。
    """
    picked = [
        r for r in rows
        if r.get("status") in ("active", "slow")
        and r.get("health_score", 0) >= min_health
        and not r.get("banned")
        and not r.get("dup_of")
        and r.get("schema_type") != "live"
    ]
    picked.sort(key=lambda r: (-r.get("starred", 0),
                               r.get("status") != "active",
                               -r.get("health_score", 0)))
    return picked


def pick_live_sources(rows: list[dict], min_health: float) -> list[dict]:
    """筛出可进入直播合集的直播源（schema_type=live），slow 同样保留。"""
    picked = [
        r for r in rows
        if r.get("schema_type") == "live"
        and r.get("status") in ("active", "slow")
        and r.get("health_score", 0) >= min_health
        and not r.get("banned")
        and not r.get("dup_of")
    ]
    picked.sort(key=lambda r: (-r.get("starred", 0),
                               r.get("status") != "active",
                               -r.get("live_channels", 0)))
    return picked


def build_duocang(
    rows: list[dict],
    min_health: float,
    mirror_base: str = "",
    mirrored_sids: "set[str] | frozenset[str]" = frozenset(),
    limit: int = 0,
) -> dict:
    """生成 storeHouse 多仓；有镜像的源用镜像地址替代原始 URL。

    limit > 0 时只取健康分最高的前 N 个（精简订阅用）。
    """
    entries = []
    picked = pick_sources(rows, min_health)
    if limit:
        picked = picked[:limit]
    for r in picked:
        url = r["url"]
        if mirror_base and r.get("id") in mirrored_sids:
            url = f"{mirror_base.rstrip('/')}/{r['id']}.json"
        entry = {
            "sourceName": r.get("name") or guess_name(r["url"]),
            "sourceUrl": url,
            "sourceDelay": int(r.get("latency_ms") or 0),  # 最近一次体检延迟 ms
        }
        entries.append(entry)
    return {"storeHouse": entries}


def build_live_config(
    rows: list[dict],
    min_health: float,
    mirror_base: str = "",
    mirrored_sids: "set[str] | frozenset[str]" = frozenset(),
    limit: int = 0,
) -> dict:
    """生成直播合集配置：lives 数组收录健康直播源，可直接作为配置地址导入。"""
    entries = []
    picked = pick_live_sources(rows, min_health)
    if limit:
        picked = picked[:limit]
    for r in picked:
        url = r["url"]
        if mirror_base and r.get("id") in mirrored_sids:
            url = f"{mirror_base.rstrip('/')}/{r['id']}.json"
        entries.append({
            "name": r.get("name") or guess_name(r["url"]),
            "type": 0,
            "url": url,
        })
    return {"lives": entries}


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
    mirror_note: str = "",
    dedupe_groups: list[dict] | None = None,
    live_info: str = "",
    changes: list[dict] | None = None,
    hosts: list[dict] | None = None,
    incremental: str = "",
    lite_info: str = "",
) -> str:
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "# TVBox 源体检报告",
        "",
        f"- 生成时间：{now}",
        f"- 源总数：{len(rows)}（{status_breakdown(rows)}）",
        f"- 订阅门槛：健康分 ≥ {min_health}",
    ]
    if incremental:
        lines.append(f"- 本轮体检口径：{incremental}")
    lines += [
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

    if changes:
        lines += [
            "",
            "## 内容变化（站点级）",
            "",
            "以下源的配置文件内容本轮有变动，明细为站点增删（同 key 视为同一站点）：",
            "",
        ]
        for c in changes[:10]:
            detail = diffing.describe(c.get("change") or {})
            lines.append(f"- `{c['name']}`：{detail or '内容有变动'}")

    lines += [
        "",
        "## 推荐订阅",
        "",
        f"多仓文件：`{duocang_path}`（{duocang_count} 个源），"
        "在 TVBox「配置地址」中填入该文件地址（或其 raw 链接）即可导入。",
    ]
    if mirror_note:
        lines.append(f"- {mirror_note}")
    if live_info:
        lines.append(f"- {live_info}")
    if lite_info:
        lines.append(f"- {lite_info}")
    lines.append("")

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

    if hosts:
        lines += [
            "## 失败主机分布（近 30 天）",
            "",
            "同一主机反复失败通常意味着整站不可用（被墙/换域名），",
            "而非单个源失效——可据此决定是否整批剔除。",
            "",
            "| 主机 | 失败次数 | 涉及源数 |",
            "|---|---|---|",
        ]
        for h in hosts[:8]:
            lines.append(f"| `{h['host']}` | {h['fails']} | {h['sources']} |")
        lines.append("")

    if dedupe_groups:
        lines += [
            "## 同内容镜像归并",
            "",
            "以下源与保留源内容完全相同，已从订阅中剔除（原站恢复差异后会自动回归）：",
            "",
        ]
        for g in dedupe_groups:
            lines.append(
                f"- `{g['merged']}` → 保留 `{g['primary']}`")

    lines += [
        "## 源榜单",
        "",
        "| 状态 | 名称 | 适配 | 站点 | 直播 | 子仓 | 探测 | L3 | 健康分 | 30天可用 | 延迟ms | 最近检查 | URL |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rank_rows(rows):
        name = (r.get("name") or "").replace("|", "\\|")[:28]
        compat = (r.get("compat") or "-").replace("|", "\\|")
        # 直播源在「直播」列显示频道数，配置源显示 lives 数
        live_disp = (r["live_channels"] if r.get("schema_type") == "live"
                     else r["lives_count"])
        probe = (f"{r['sub_ok']}/{r['sub_total']}"
                 if r.get("sub_total") else "-")
        l3 = (f"{r['l3_ok']}/{r['l3_tested']}"
              if r.get("l3_tested") else "-")
        avail = r.get("avail") or "-"
        dup = f" →#{str(r['dup_of'])[:8]}" if r.get("dup_of") else ""
        # 竖线需转义，避免破坏 Markdown 表格；先算好再进 f-string（3.11 不支持反斜杠表达式）
        url_cell = r["url"].replace("|", "\\|")
        lines.append(
            f"| {r['status']}{dup} | {name} | {compat} | {r['sites_count']} | {live_disp} "
            f"| {r['storehouse_count']} | {probe} | {l3} | {r['health_score']} "
            f"| {avail} | {r['latency_ms']} | {r.get('last_checked') or '-'} "
            f"| `{url_cell}` |"
        )
    banned_n = len([r for r in rows if r.get("banned")])
    if banned_n:
        lines += ["", f"> 另有 {banned_n} 个 banned 源未列出。"]
    lines.append("")
    return "\n".join(lines)


_CSV_HEADER = ("状态", "名称", "适配", "站点", "直播", "子仓", "探测", "L3",
               "健康分", "30天可用", "延迟ms", "最近检查", "URL")


def build_stats(
    rows: list[dict],
    duocang_count: int = 0,
    live_count: int = 0,
    added: "set[str] | None" = None,
    removed: "set[str] | None" = None,
    min_health: float = 0,
) -> dict:
    """跑批结构化统计：供看板/监控复用，省得只能翻 Actions 日志。"""
    counts = Counter(r["status"] for r in rows if not r.get("banned"))
    scheme_total = sum(int(r.get("https_sites") or 0)
                       + int(r.get("http_sites") or 0) for r in rows)
    https_total = sum(int(r.get("https_sites") or 0) for r in rows)
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total": len(rows),
        "status": dict(sorted(counts.items())),
        "banned": len([r for r in rows if r.get("banned")]),
        "publishable": duocang_count,
        "live": live_count,
        "min_health": min_health,
        "added": len(added or ()),
        "removed": len(removed or ()),
        "https_ratio": (round(https_total / scheme_total, 4)
                        if scheme_total else None),
    }


def rank_rows(rows: list[dict]) -> list[dict]:
    """报告 / CSV / 看板共用的榜单口径：剔除 banned，置顶优先、健康分倒序。"""
    return sorted((r for r in rows if not r.get("banned")),
                  key=lambda r: (-r.get("starred", 0), -r.get("health_score", 0)))


def build_rank_csv(rows: list[dict]) -> str:
    """源榜单 CSV：行集合/顺序与报告榜单一致，便于 Excel 直接筛选排序。

    带 UTF-8 BOM，双击用 Excel 打开不会按本地代码页乱码；字段一律交给
    csv 模块转义，名称里的逗号/引号/换行不会破表。
    """
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(_CSV_HEADER)
    for r in rank_rows(rows):
        live_disp = (r.get("live_channels") if r.get("schema_type") == "live"
                     else r.get("lives_count"))
        probe = (f"{r['sub_ok']}/{r['sub_total']}" if r.get("sub_total") else "-")
        l3 = (f"{r['l3_ok']}/{r['l3_tested']}" if r.get("l3_tested") else "-")
        writer.writerow([
            r.get("status", ""),
            r.get("name") or "",
            r.get("compat") or "",
            int(r.get("sites_count") or 0),
            "" if live_disp is None else live_disp,
            int(r.get("storehouse_count") or 0),
            probe,
            l3,
            r.get("health_score", 0),
            r.get("avail") or "",
            int(r.get("latency_ms") or 0),
            r.get("last_checked") or "",
            r.get("url", ""),
        ])
    return "\ufeff" + buf.getvalue()


def write_duocang(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def write_mirror(path: Path, content: bytes) -> None:
    """写入源内容镜像（原样字节，保证与最后一次成功快照一致）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def format_diff_text(diff: dict) -> str:
    """把 last_diff 渲染成通知文本（Markdown）。"""
    added = diff.get("added") or []
    removed = diff.get("removed") or []
    lines = [
        f"## TVBox 源更新 · {diff.get('ts', '')}",
        "",
        f"- 订阅源：**{diff.get('count', 0)}** 个",
        f"- 本轮变化：新增 **{len(added)}** / 移除 **{len(removed)}**",
    ]
    for u in added[:10]:
        lines.append(f"  - ➕ `{u}`")
    for u in removed[:10]:
        lines.append(f"  - ➖ `{u}`")
    if len(added) > 10:
        lines.append(f"  - …等 {len(added)} 个")
    return "\n".join(lines) + "\n"


def failure_text() -> str:
    """跑批失败告警文本（在 Actions 失败步骤里调用）。"""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    link = (f"https://github.com/{repo}/actions/runs/{run_id}"
            if repo and run_id else "")
    lines = ["## ⚠️ TVBoxKeeper 跑批失败", ""]
    if link:
        lines.append(f"日志：{link}")
    lines.append("订阅产物可能已过期，请尽快排查。")
    return "\n".join(lines) + "\n"


def should_notify(diff: dict, weekly_fresh: bool, settings: dict) -> bool:
    """无变化且无周报时是否静默（notify.silent_when_unchanged 控制）。"""
    cfg = settings.get("notify") or {}
    if not cfg.get("silent_when_unchanged", True):
        return True
    return bool(diff.get("added") or diff.get("removed") or weekly_fresh)


def webhook_payload(platform: str, text: str) -> dict:
    """按平台构造 webhook 消息体（都是纯文本，国内直连可用）。"""
    plain = text.replace("**", "").replace("`", "").replace("## ", "【").replace("\n## ", "\n【")
    if platform == "feishu":
        return {"msg_type": "text", "content": {"text": plain}}
    if platform == "bark":
        # Bark 走 /push 的 JSON：title/body 分开，device_key 由调用方按 env 补
        return {"title": "TVBoxKeeper", "body": plain}
    # 企业微信 / 钉钉同为 {"msgtype": "text", ...}
    return {"msgtype": "text", "text": {"content": plain}}


_STATUS_CLASS = {
    "active": "ok", "slow": "warn", "anti_crawl": "warn", "flaky": "warn",
    "invalid": "bad", "dead": "bad", "banned": "off", "new": "off",
}

# 按「可用程度」而非字母序排序：active 优先，slow 紧随其后，死源沉底
_STATUS_RANK = {
    "active": 0, "slow": 1, "new": 2, "flaky": 3, "anti_crawl": 4,
    "invalid": 5, "dead": 6, "banned": 7,
}


def _th(key: str, label: str, kind: str = "n", cls: str = "") -> str:
    """可排序表头。整格是一个按钮，方向只由 ``aria-sort`` 表达（箭头由 CSS 画）。

    用真按钮而不是给 ``th`` 挂 click：键盘可聚焦、读屏可播报，且不必手工模拟
    Enter/Space。``data-type`` 让前端知道首点该从升序还是降序起（文本 A→Z、
    数值大→小）。
    """
    attr_cls = f' class="{cls}"' if cls else ""
    return (f'<th{attr_cls} data-sort="{key}" data-type="{kind}" '
            f'data-label="{label}" aria-sort="none">'
            f'<button type="button" class="thb">{label}'
            f'<span class="arw" aria-hidden="true"></span>'
            f'<span class="ord" aria-hidden="true"></span></button></th>')


def site_kind(row: dict) -> str:
    """看板筛选口径：纯直播配置源记 ``live``，其余记 ``vod``。

    判定依据是 schema_type —— classify 只把「sites 为空、仅提供 lives/channels」
    的源标记为 live。TVBox 单仓里附带的 ``lives`` 字段属于「点播源顺带直播」，
    主体仍是点播站点，因此算非直播，否则按「含直播内容」筛选会命中几乎所有源，
    筛选就失去意义。
    """
    return "live" if row.get("schema_type") == "live" else "vod"


def _sparkline(marks: list[int], width: int = 120, height: int = 22) -> str:
    """最近 N 次体检的可用性折线（内联 SVG）。"""
    if not marks:
        return ""
    n = len(marks)
    step = width / max(1, n - 1)
    pts = " ".join(
        f"{i * step:.1f},{(height - 3) - m * (height - 6):.1f}"
        for i, m in enumerate(marks))
    rate = sum(marks) / n
    color = "var(--ok)" if rate >= 0.8 else ("var(--warn)" if rate >= 0.5 else "var(--bad)")
    return (f'<svg width="{width}" height="{height}" aria-hidden="true">'
            f'<polyline fill="none" stroke="{color}" stroke-width="2" '
            f'points="{pts}"/></svg>')


def _rate_color(rate: float) -> str:
    """可用率色档（与内联 sparkline 同口径）。"""
    return "var(--ok)" if rate >= 80 else ("var(--warn)" if rate >= 50 else "var(--bad)")


def _rate_class(rate: float) -> str:
    return "ok" if rate >= 80 else ("warn" if rate >= 50 else "bad")


_TREND_EMPTY = ('<figure class="trend-panel"><div class="chart-empty">'
                '暂无体检记录，明日开始生成趋势</div></figure>')


def _trend_figure(points: list[dict] | None, n_total: int = 0,
                  n_live: int = 0, attrs: str = "", note_ext: str = "") -> str:
    """整体可用率趋势面板（几何层走 SVG，文字层走 HTML）。

    关键取舍：SVG 用 ``viewBox="0 0 1000 200"`` + ``preserveAspectRatio="none"``
    把几何拉满容器，所以**任何带文字的元素都不能放进这个 SVG**（会被非等比拉糊）。
    轴名、Y/X 刻度、数值标签、tooltip、数据点热区全部是 HTML + 百分比绝对定位，
    px 尺寸不随容器缩放。

    两个计数器不可混用：
    - ``n_axis`` 日期跨度 —— 决定 x 坐标 / 刻度 / 热区宽度 / 「近 N 天」；
    - ``n_data`` 有效点数 —— 决定拥挤度（dot 尺寸、标签开关）、色档、均值、空态。
    缺日在时间轴上必须留空（线性），但不应污染统计。
    """
    clean = sorted((p for p in (points or []) if p.get("date")),
                   key=lambda p: p["date"])
    if not clean:
        return _TREND_EMPTY
    first = date.fromisoformat(clean[0]["date"])
    last = date.fromisoformat(clean[-1]["date"])
    n_axis = (last - first).days + 1
    by_date = {p["date"]: p for p in clean}
    seq = []
    for i in range(n_axis):
        d = first + timedelta(days=i)
        seq.append((d, by_date.get(d.isoformat())))
    valid = [(d, p) for d, p in seq if p and p.get("rate") is not None]
    n_data = len(valid)
    if not n_data:
        return _TREND_EMPTY

    rates = [p["rate"] for _, p in valid]
    lo_v, hi_v = min(rates), max(rates)
    rng = hi_v - lo_v
    single = n_data == 1
    flat = n_data > 1 and rng == 0
    # zoomed 与 axis_span 是两个正交概念，不能共用一个变量：
    # - zoomed   仅 ``10 <= rng < 20`` 为真，控制「轴已放大」徽标（语义是「我放大了比例」）
    # - axis_span 域非 0–100 即为真，控制轴名后缀 ``· 轴 low–high``
    # 全平只满足 axis_span：持平不是放大，徽标会让人以为有起伏
    zoomed = False
    if single:
        # 单点没有起伏概念：固定 0–100，保住「45% 处于什么绝对水平」的判断力
        low, high = 0, 100
    elif flat:
        # 全平是真实信息：收窄到 ±10 让平坦线离开边界；域变了靠轴名字段解释，
        # 但不能给「轴已放大」徽标 —— 持平不是放大，徽标会让人以为有起伏
        low, high = max(0, int(lo_v) - 10), min(100, int(hi_v) + 10)
    elif 10 <= rng < 20:
        # 仅此区间值得放大：<10 收益小且跨天不可比，>=20 的 0–100 域已足够
        low = max(0, (int(lo_v) // 10) * 10 - 10)
        high = min(100, (int(hi_v) // 10 + 1) * 10 + 10)
        if high - low < 20:            # 域至少 20 宽，防过度放大与除零
            mid_v = (low + high) // 2
            low, high = max(0, mid_v - 10), min(100, mid_v + 10)
        zoomed = True
    else:
        low, high = 0, 100
    span = high - low
    axis_span = low != 0 or high != 100
    mid = (low + high) // 2

    mean = sum(rates) / n_data
    color = _rate_color(mean)
    cur, start = rates[-1], rates[0]
    diff = cur - start

    def x_pct(off: int) -> float:
        return 50.0 if n_axis <= 1 else 1 + off * 98 / (n_axis - 1)

    def y_pct(v: float) -> float:
        return (high - v) / span * 100

    def cx(off: int) -> float:
        return 10 + (off * 980 / (n_axis - 1) if n_axis > 1 else 490)

    def cy(v: float) -> float:
        return 200 * (high - v) / span

    offs = [(d - first).days for d, _ in valid]
    # 按连续段切分：只有相邻有效点相差正好 1 天才连线，缺口处断线不留色带
    segs: list[list[int]] = []
    for i in range(n_data):
        if i and offs[i] - offs[i - 1] == 1:
            segs[-1].append(i)
        else:
            segs.append([i])
    islands = set()
    if n_data > 1:      # 单点不按孤岛画（它是唯一的点，用常规 9px 空心点）
        for i in range(n_data):
            prev_gap = (offs[i] - offs[i - 1]) if i else None
            next_gap = (offs[i + 1] - offs[i]) if i + 1 < n_data else None
            if prev_gap != 1 and next_gap != 1:
                islands.add(i)

    # ---- SVG：只画几何，每条 stroke 都加 non-scaling-stroke ----
    nss = 'vector-effect="non-scaling-stroke"'

    def py(v: float) -> float:
        """可用率 → viewBox 纵向位置（0 顶 / 200 底），用于重合判定。"""
        return 200 * (high - v) / span

    # 语义线：单点无起伏、全平时均值与数据线重合，两种情况都不画均值线；
    # 全平也不画 50/80 阈值线 —— 持平场景阈值没有参考价值，且全平值恰在
    # 阈值附近时会与数据线近距并行（<12px）被误读成画歪的双线
    sem: list[tuple[str, float]] = []
    if not single and not flat:
        sem.append(("mean", mean))
    if not single and not flat and low + 3 <= 80 <= high - 3:
        sem.append(("t80", 80))
    if (not single and not flat and n_data <= 3
            and low + 3 <= 50 <= high - 3):
        sem.append(("t50", 50))
    structs = [{"k": "bottom", "v": low, "stroke": "var(--border)"},
               {"k": "mid", "v": mid, "stroke": "var(--grid)"}]
    if high < 100:      # high==100 时顶线与绘图区上边界重合，不画
        structs.append({"k": "top", "v": high, "stroke": "var(--grid)"})

    # 重合抑制一：语义线压在结构线上（<2px）→ 删语义线，实线更利读数
    kept_sem = [s for s in sem
                if all(abs(py(s[1]) - py(t["v"])) >= 2 for t in structs)]
    # 语义线彼此极近 → 阈值线让位均值线（均值有自己的数值出处）
    uniq: list[tuple[str, float]] = []
    for name, v in kept_sem:
        if any(abs(py(v) - py(v2)) < 2 for _, v2 in uniq):
            continue
        uniq.append((name, v))
    kept_sem = uniq
    # 重合抑制二：结构线距语义线 2–12px → 删结构线，避免读成「画歪的双线」；
    # 结构线的 Y 刻度文字不受影响（刻度不依赖线存在）
    kept_struct = [t for t in structs
                   if not any(2 <= abs(py(t["v"]) - py(v)) < 12
                              for _, v in kept_sem)]

    grid = []
    for t in kept_struct:
        y = min(199.5, max(0.5, py(t["v"])))
        grid.append(f'<line x1="0" y1="{y:.1f}" x2="1000" y2="{y:.1f}" '
                    f'stroke="{t["stroke"]}" stroke-width="1" {nss}/>')
    for name, v in kept_sem:
        style = ('stroke="var(--text-3)" stroke-width="1" stroke-opacity="0.45" '
                 'stroke-dasharray="4 3"' if name == "mean" else
                 'stroke="var(--border)" stroke-width="1" stroke-dasharray="2 4"')
        grid.append(f'<line x1="0" y1="{py(v):.1f}" x2="1000" y2="{py(v):.1f}" '
                    f'{style} {nss}/>')

    areas, lines = [], []
    for seg in segs:
        if len(seg) < 2:
            continue
        xy = [(cx(offs[i]), cy(valid[i][1]["rate"])) for i in seg]
        if not flat:    # 全平不收窄域内色带（零起伏的面积没有信息量）
            path = ("M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in xy)
                    + f" L {xy[-1][0]:.1f},200 L {xy[0][0]:.1f},200 Z")
            areas.append(f'<path d="{path}" fill="url(#tg-trend)" stroke="none"/>')
        lines.append(
            f'<polyline fill="none" stroke="{color}" stroke-width="2.5" '
            f'stroke-linecap="round" stroke-linejoin="round" {nss} '
            f'points="{" ".join(f"{x:.1f},{y:.1f}" for x, y in xy)}"/>')

    svg = (
        f'<svg class="geo" viewBox="0 0 1000 200" width="100%" height="100%" '
        f'preserveAspectRatio="none" aria-hidden="true" focusable="false">'
        f'<defs><linearGradient id="tg-trend" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0" stop-color="{color}" stop-opacity="0.26"/>'
        f'<stop offset="1" stop-color="{color}" stop-opacity="0"/>'
        f'</linearGradient></defs>'
        f'{"".join(grid)}{"".join(areas)}{"".join(lines)}</svg>')

    # ---- HTML 层：数据点热区 + 常驻数值标签 + CSS tooltip ----
    dot = 9 if n_data <= 2 else (7 if n_data <= 7 else 5)
    # n_axis==1 时没有「相邻间距」，热区退化为固定宽度（避免 min(32px, inf%)）
    hot_w = "32px" if n_axis <= 1 else f"min(32px,{98 / (n_axis - 1):.2f}%)"
    if flat:
        labeled = {n_data - 1}          # 全平只标最后一个点，否则一排放同一个数字
    elif n_data <= 5:
        labeled = set(range(n_data))
    else:
        labeled = {rates.index(max(rates)), rates.index(min(rates))}
    hotspots = []
    for i, (d, p) in enumerate(valid):
        rate = p["rate"]
        x, y = x_pct(offs[i]), y_pct(rate)
        align = "l" if x < 12 else ("r" if x > 88 else "c")
        side = "above" if y >= 22 else "below"
        tip = (f'{d.isoformat()} · 可用率 {rate}% · '
               f'{int(p.get("total") or 0)} 次体检')
        lab = ""
        if i in labeled:
            flip = " vb" if y < 15 else ""
            lab = f'<span class="vlab{flip}">{rate}</span>'
        if i in islands:
            mark, cls = '<i class="dot isl"></i>', "pt isl"
        else:
            mark, cls = f'<i class="dot s{dot}"></i>', "pt"
        hotspots.append(
            f'<button type="button" class="{cls}" '
            f'style="left:{x:.2f}%;--y:{y:.2f}%;width:{hot_w}" '
            f'data-tip="{tip}" data-align="{align}" data-side="{side}" '
            f'aria-label="{d.isoformat()} 可用率 {rate}%">'
            f'{mark}{lab}</button>')

    # ---- 坐标轴 ----
    # 域不是 0–100 时把值域写进轴名：右侧徽标离轴太远，视线不经过
    axis_name = "可用率 %"
    if axis_span:
        axis_name = f"可用率 % · 轴 {low}–{high}"
    yticks = "".join(
        f'<span style="top:{top:.1f}%">{v}</span>'
        for v, top in ((high, 0.0), (mid, 50.0), (low, 100.0)))
    xticks = []
    if n_axis > 1:                      # 单点没有时间轴可刻度
        n_tick = min(n_axis, 15)        # 总刻度上限 15
        if n_tick == n_axis:
            tick_idx = list(range(n_axis))
        else:
            tick_idx = sorted(
                {round(i * (n_axis - 1) / (n_tick - 1)) for i in range(n_tick)})
        # 主刻度恒定 ≤7 个且含首尾：<900px 隐藏 .p1 后最多 7 个标签。
        # 但 7 个主刻度在窄屏可能贴住：320px 时绘图区约 200px，日期标签 26.4px
        # 需 ≥30px 间距，即相邻主刻度至少隔 1.4 天。n_axis 在 10–30 时按等分
        # 取整会出现只隔 1 天的一对（实测最近间距 -4.6px ~ 1.6px），
        # 故给主刻度再分一层 .p0b（按序号隔一取一），≤420px 时隐藏，
        # 剩 4 个日期在 320px 下仍有 30px 以上间距。
        n_p0 = min(len(tick_idx), 7)
        if n_p0 > 1:
            p0_order = sorted(
                {tick_idx[round(i * (len(tick_idx) - 1) / (n_p0 - 1))]
                 for i in range(n_p0)})
        else:
            p0_order = list(tick_idx)
        need = 30 * (n_axis - 1) / 196       # 320px 下相邻标签所需天数间隔
        gaps = [b - a for a, b in zip(p0_order, p0_order[1:])]
        crowded = bool(gaps) and min(gaps) < need
        primary = set(p0_order)
        extra = set(p0_order[1::2]) if crowded else set()
        for i in tick_idx:
            cls = "p0 p0b" if i in extra else ("p0" if i in primary else "p1")
            label = (first + timedelta(days=i)).strftime("%m-%d")
            xticks.append(
                f'<span class="{cls}" style="left:{x_pct(i):.2f}%">{label}</span>')

    # ---- 头部统计 ----
    cur_stat = (f'<div class="stat"><b class="{_rate_class(cur)}">{cur}%</b>'
                f'<span>当前</span></div>')
    if single:
        # 单点没有均值/环比可言，整项隐藏而不是显示「—」
        stats = cur_stat
    else:
        if flat:
            delta = '<b class="flat">持平</b>'
        elif diff > 0:
            delta = f'<b class="ok">▲ {abs(diff):.1f}pt</b>'
        elif diff < 0:
            delta = f'<b class="bad">▼ {abs(diff):.1f}pt</b>'
        else:
            delta = '<b class="flat">＝ 0.0pt</b>'
        stats = (cur_stat
                 + f'<div class="stat"><b>{mean:.1f}%</b><span>均值</span></div>'
                 + f'<div class="stat">{delta}<span>较首日</span></div>')
    badge = (f'<em class="badge">轴已放大 {low}–{high}%</em>' if zoomed else "")

    # ---- 脚注：左 = 口径，右 = 数据完整性 ----
    lead = f"全量 {n_total} 个源" + (f"（含直播 {n_live}）" if n_live else "")
    lead += " · 不受下方筛选影响"
    miss = n_axis - n_data
    if single:
        note = "仅有 1 天数据，明日开始显示趋势"
    elif flat:
        note = f"可用率持平（近 {n_axis} 天均为 {rates[0]}%）"
    else:
        note = f"有记录 {n_data} 天" + (f" · 缺 {miss} 天" if miss > 0 else "")

    # ---- 无障碍明细表（行不带 class，避免干扰主表行数统计） ----
    rows_sr = []
    for d, p in seq:
        if p is None or p.get("rate") is None:
            rows_sr.append(f'<tr><td>{d.isoformat()}</td><td>—</td><td>—</td></tr>')
        else:
            rows_sr.append(
                f'<tr><td>{d.isoformat()}</td><td>{p["rate"]}%</td>'
                f'<td>{int(p.get("total") or 0)}</td></tr>')

    return (
        f'<figure class="trend-panel"{attrs} aria-label="可用率趋势图">'
        f'<div class="trend-top">'
        f'<figcaption><h2>整体可用率趋势 · '
        f'{clean[0]["date"][5:]} → {clean[-1]["date"][5:]}</h2>'
        f'<ul class="legend"><li class="ok">≥80% 健康</li>'
        f'<li class="warn">50–79% 观察</li>'
        f'<li class="bad">&lt;50% 异常</li></ul></figcaption>'
        f'<div class="chart-head">{stats}{badge}</div>'
        f'</div>'
        f'<div class="chart-plot">'
        f'<div class="ylab">{axis_name}</div>'
        f'<div class="yaxis">{yticks}</div>'
        f'<div class="plot" style="color:{color}">{svg}{"".join(hotspots)}</div>'
        f'<div class="xaxis">{"".join(xticks)}</div>'
        f'<div class="xname">日期（MM-DD）</div>'
        f'</div>'
        f'<div class="pfoot"><span class="lead">{lead}</span>'
        f'<span class="note">{note}'
        f'{f" · {note_ext}" if note_ext else ""}</span>'
        f'<table class="sr-only"><caption>近 {n_axis} 天可用率明细</caption>'
        f'<tr><th>日期</th><th>可用率</th><th>体检次数</th></tr>'
        f'{"".join(rows_sr)}</table>'
        f'</figure>')




def _weekly_aggregate(points: list[dict]) -> tuple[list[dict], str]:
    """全部视图超 60 天时按 ISO 周聚合，热区/刻度密度才有意义。"""
    buckets: dict[str, dict] = {}
    for p in points:
        d = date.fromisoformat(p["date"])
        iso = d.isocalendar()
        key = f"{iso[0]}-W{iso[1]:02d}"
        b = buckets.setdefault(key, {"date": p["date"], "total": 0, "ok": 0})
        if p["date"] > b["date"]:
            b["date"] = p["date"]
        b["total"] += int(p.get("total") or 0)
        b["ok"] += int(p.get("ok") or 0)
    out = [{"date": b["date"], "total": b["total"], "ok": b["ok"],
            "rate": round(b["ok"] * 100 / b["total"]) if b["total"] else 0}
           for b in buckets.values()]
    return out, f"跨度超 60 天，已按周聚合（{len(out)} 周）"


def _trend_panel(points: list[dict] | None, n_total: int = 0,
                 n_live: int = 0) -> str:
    """趋势入口：按最近一周 / 最近一月 / 全部各渲染一份服务端图，按钮切换。

    - 每份图与旧版单视图完全同构（自适应缩放/热区/标签全部保留）；
    - 全部视图超 60 天按周聚合，超 365 天的旧数据直接不展示（成本换可读）；
    - 只有一个可用视图时输出与旧版完全一致（无按钮）。
    """
    clean = sorted((p for p in (points or []) if p.get("date")),
                   key=lambda p: p["date"])
    if not clean:
        return _TREND_EMPTY
    today = date.fromisoformat(clean[-1]["date"])
    week = [p for p in clean
            if date.fromisoformat(p["date"]) >= today - timedelta(days=6)]
    month = [p for p in clean
             if date.fromisoformat(p["date"]) >= today - timedelta(days=29)]
    all_pts = [p for p in clean
               if date.fromisoformat(p["date"]) >= today - timedelta(days=364)]
    agg_note = ""
    if len(all_pts) > 60:
        all_pts, agg_note = _weekly_aggregate(all_pts)
    views: list[tuple[str, str, list[dict], str]] = []
    seen: set[tuple[str, str, int]] = set()
    for key, label, pts in (("week", "最近一周", week),
                            ("month", "最近一月", month),
                            ("all", "全部", all_pts)):
        if not pts:
            continue
        sig = (pts[0]["date"], pts[-1]["date"], len(pts))
        if sig in seen:
            continue
        seen.add(sig)
        views.append((key, label, pts, agg_note if key == "all" else ""))
    if len(views) == 1:
        return _trend_figure(views[0][2], n_total, n_live)
    active = ("month" if any(k == "month" for k, *_ in views)
              else views[-1][0])
    btns = "".join(
        f'<button type="button" class="trbtn{" active" if k == active else ""}" '
        f'data-range="{k}" role="tab" '
        f'aria-selected="{"true" if k == active else "false"}">{label}</button>'
        for k, label, _p, _n in views)
    # 激活视图排 DOM 首位：读屏/测试取第一个 figure 就是默认展示的那份
    ordered = sorted(views, key=lambda v: v[0] != active)
    figs = "".join(
        _trend_figure(p, n_total, n_live,
                      attrs=f' data-range="{k}"'
                      f'{"" if k == active else " hidden"}',
                      note_ext=note)
        for k, _label, p, note in ordered)
    return (f'<div class="trange" role="tablist" '
            f'aria-label="趋势时间范围切换">{btns}</div>{figs}')


_SITE_CSS = """\
:root{
  color-scheme:dark;
  --bg:#0f172a; --panel:#131e33; --card:#1e293b; --card-2:#24334a;
  --line:#1c2840; --border:#334155; --border-2:#3d4f6b;
  --text:#e2e8f0; --text-2:#94a3b8; --text-3:#7d8ea6; --text-4:#475569;
  --ok:#4ade80; --ok-bg:#14532d; --warn:#fbbf24; --warn-bg:#451a03;
  --bad:#f87171; --bad-bg:#450a0a; --accent:#3b82f6; --accent-solid:#1d4ed8;
    --accent-soft:#93c5fd; --accent-soft-2:#bfdbfe; --link:#60a5fa;
  --text-hi:#cbd5e1; --title:#f8fafc; --inv:#ffffff;
  --bg-2:#0b1220; --card-3:#273449; --grid:#1f2d45;
  --tools-h:63px;
}
html[data-theme="light"]{
  color-scheme:light;
  --bg:#f5f7fb; --bg-2:#eef1f8; --panel:#ffffff; --card:#ffffff; --card-2:#f1f4f9;
  --line:#e3e8f0; --border:#d7dde8; --border-2:#c4cdda;
  --text:#1f2937; --text-2:#5b6474; --text-3:#6b7280; --text-4:#98a1b0;
  --ok:#15803d; --ok-bg:#dcfce7; --warn:#b45309; --warn-bg:#fef3c7;
  --bad:#dc2626; --bad-bg:#fee2e2; --accent:#2563eb; --accent-solid:#1d4ed8;
  --accent-soft:#2563eb; --accent-soft-2:#1d4ed8; --link:#2563eb;
  --text-hi:#111827; --title:#0f172a; --inv:#ffffff;
  --bg-2:#eef1f8; --card-3:#e7ecf5; --grid:#dde3ee;
}
*{box-sizing:border-box}
/* ---------- 顶栏与主题切换 ---------- */
.head{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}
.tbtn{flex:none;border:1px solid var(--border);background:var(--card);
  color:var(--text-2);border-radius:8px;padding:6px 10px;font-size:14px;
  cursor:pointer;line-height:1}
.tbtn:hover{background:var(--card-3);color:var(--text)}
body{margin:0;background:var(--bg);color:var(--text);
     font:14px/1.6 system-ui,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:1320px;margin:0 auto;padding:32px 40px 64px}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;
         overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
h1{font-size:22px;font-weight:700;color:var(--title);margin:0 0 6px;letter-spacing:-.01em}
.meta{font-size:12.5px;color:var(--text-3);margin:0 0 24px}
h2{display:flex;align-items:center;gap:8px;font-size:14px;font-weight:600;
   color:var(--text-hi);margin:40px 0 12px;scroll-margin-top:88px}
h2::before{content:"";flex:none;width:3px;height:14px;border-radius:2px;
           background:var(--border)}
/* 状态卡片：彩色只落在数字上，不铺底色 */
.cards{display:flex;gap:12px;flex-wrap:wrap;margin:0}
.card{display:flex;flex-direction:column;gap:2px;min-width:104px;
      padding:14px 18px;border-radius:12px;background:var(--card);
      border:1px solid var(--card-2)}
.card b{font-size:26px;font-weight:700;line-height:1.2;
        font-variant-numeric:tabular-nums;color:var(--text)}
.card span{font-size:11.5px;font-weight:500;color:var(--text-2)}
.card.all{min-width:132px;border-left:3px solid var(--accent)}
.card.all b{color:var(--title)}
.card.ok b{color:var(--ok)} .card.warn b{color:var(--warn)}
.card.bad b{color:var(--bad)} .card.off b{color:var(--text-2)}
.chg-line{font-size:12.5px;color:var(--text-3);margin:16px 0 0}
/* ---------- 趋势面板：几何层 SVG + 文字层 HTML ---------- */
.trend-panel{margin:0;padding:18px 20px 14px;border-radius:14px;
             background:var(--panel);border:1px solid var(--line)}
.cards + .trend-panel{margin-top:32px}
.chg-line + .trend-panel{margin-top:18px}
.trend-top{display:flex;justify-content:space-between;align-items:flex-start;
           gap:24px;flex-wrap:wrap}
.trend-panel h2{margin:0;font-size:14px;scroll-margin-top:0}
.trend-panel h2::before{background:var(--accent)}
.trend-panel .legend{margin:5px 0 0}
.chart-head{display:flex;align-items:flex-end;gap:18px;flex-wrap:wrap}
.chart-head .stat{display:flex;flex-direction:column;align-items:flex-end;gap:1px}
.chart-head .stat b{font-size:17px;font-weight:700;line-height:1.2;
                    color:var(--text);font-variant-numeric:tabular-nums}
.chart-head .stat b.ok{color:var(--ok)}
.chart-head .stat b.warn{color:var(--warn)}
.chart-head .stat b.bad{color:var(--bad)}
.chart-head .stat b.flat{color:var(--text-2)}
.chart-head .stat span{font-size:10.5px;color:var(--text-3)}
.badge{align-self:center;font-style:normal;font-size:10.5px;color:var(--text-2);
       background:var(--card);border:1px solid var(--border);border-radius:99px;
       padding:2px 9px;white-space:nowrap}
.chart-plot{display:grid;grid-template-columns:46px minmax(0,1fr);
            grid-template-rows:auto auto auto auto;margin:16px 0 0}
.ylab{grid-column:2;grid-row:1;font-size:10.5px;color:var(--text-3);margin-bottom:10px}
.yaxis{grid-column:1;grid-row:2;position:relative}
.yaxis span{position:absolute;right:8px;transform:translateY(-50%);white-space:nowrap;
            font-size:11px;font-weight:500;color:var(--text-3);
            font-variant-numeric:tabular-nums}
.plot{grid-column:2;grid-row:2;position:relative;height:210px}
.xaxis{grid-column:2;grid-row:3;position:relative;height:18px;margin-top:14px}
.xaxis span{position:absolute;top:0;transform:translateX(-50%);white-space:nowrap;
            font-size:10.5px;color:var(--text-3);font-variant-numeric:tabular-nums}
.xname{grid-column:2;grid-row:4;text-align:right;font-size:10.5px;
       color:var(--text-3);margin-top:2px}
/* 几何层拉满容器；非等比缩放靠 non-scaling-stroke 保住描边粗细 */
.plot .geo{position:absolute;inset:0;display:block;width:100%;height:100%}
.pt{position:absolute;top:0;bottom:0;transform:translateX(-50%);margin:0;padding:0;
    background:none;border:0;color:inherit;cursor:pointer;
    -webkit-appearance:none;appearance:none;transition:background-color 120ms linear}
.pt:hover,.pt:focus-visible{
  background:linear-gradient(90deg,transparent,var(--border) 45%,var(--border) 55%,transparent)}
.pt:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.dot{position:absolute;left:50%;top:var(--y);display:block;border-radius:50%;
     border:2px solid currentColor;background:var(--panel);box-sizing:border-box;
     transform:translate(-50%,-50%);
     transition:box-shadow 120ms linear,outline-color 120ms linear}
.dot.s5{width:5px;height:5px} .dot.s7{width:7px;height:7px}
.dot.s9{width:9px;height:9px}
/* 孤岛点：5px 实心灰，与线色的空心点明确区分 */
.dot.isl{width:5px;height:5px;background:var(--text-2);border-color:var(--text-2)}
.pt:hover .dot,.pt:focus-visible .dot{box-shadow:0 0 0 4px rgba(148,163,184,.18);
                                      outline:1.5px solid currentColor;outline-offset:1px}
.vlab{position:absolute;left:50%;top:var(--y);transform:translate(-50%,-100%);
      margin-top:-10px;font-size:11px;font-weight:600;color:currentColor;
      font-variant-numeric:tabular-nums;white-space:nowrap;pointer-events:none;
      text-shadow:0 1px 2px rgba(2,6,23,.55),0 0 6px rgba(2,6,23,.55)}
.vlab.vb{transform:translate(-50%,0);margin-top:10px}
/* tooltip 走纯 CSS，不依赖 JS */
.pt::after{content:attr(data-tip);position:absolute;left:50%;top:var(--y);z-index:4;
           transform:translate(-50%,-150%);padding:4px 8px;border-radius:6px;
           background:var(--bg-2);border:1px solid var(--border-2);color:var(--text);
           font-size:11px;font-weight:500;white-space:nowrap;
           box-shadow:0 6px 16px rgba(0,0,0,.5);
           opacity:0;pointer-events:none;transition:opacity 120ms linear}
.pt:hover::after,.pt:focus-visible::after{opacity:1}
.pt[data-align="l"]::after{transform:translate(-14%,-150%)}
.pt[data-align="r"]::after{transform:translate(-86%,-150%)}
.pt[data-side="below"]::after{transform:translate(-50%,46%)}
.pt[data-side="below"][data-align="l"]::after{transform:translate(-14%,46%)}
.pt[data-side="below"][data-align="r"]::after{transform:translate(-86%,46%)}
.pfoot{display:flex;justify-content:space-between;align-items:center;gap:16px;
       flex-wrap:wrap;margin-top:14px;padding-top:10px;
       border-top:1px solid var(--line)}
.lead{font-size:11.5px;color:var(--text-3)}
.legend{display:flex;gap:14px;list-style:none;margin:0;padding:0}
.legend li{display:flex;align-items:center;gap:6px;font-size:11px;color:var(--text-3)}
.legend li::before{content:"";width:6px;height:6px;border-radius:50%;background:currentColor}
.legend li.ok::before{background:var(--ok)} .legend li.warn::before{background:var(--warn)}
.legend li.bad::before{background:var(--bad)}
.note{font-size:11.5px;color:var(--text-3);margin-left:auto}
.chart-empty{display:flex;align-items:center;justify-content:center;height:160px;
             border:1px dashed var(--border);border-radius:10px;
             font-size:12.5px;color:var(--text-3)}
/* ---------- 工具条：吸顶，表头让位 ---------- */
.tools{position:sticky;top:0;z-index:5;display:flex;gap:10px;align-items:center;
       flex-wrap:wrap;padding:12px 0;margin:32px 0 20px;background:var(--bg);
       box-shadow:0 1px 0 var(--line)}
#q{width:280px;padding:8px 12px;border-radius:8px;border:1px solid var(--border);
   background:var(--card);color:var(--text);font:inherit;font-size:13px}
#q::placeholder{color:var(--text-3)}
#q:focus-visible{outline:2px solid var(--accent);outline-offset:2px;
                 border-color:var(--accent)}
.filters{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.fbtn{padding:7px 14px;border-radius:8px;border:1px solid var(--border);
      background:var(--card);color:var(--text-hi);font:inherit;font-size:13px;cursor:pointer;
      transition:background-color 120ms linear,border-color 120ms linear}
.fbtn:hover{border-color:var(--border-2);color:var(--text)}
.fbtn.active{background:var(--accent-solid);border-color:var(--accent-solid);color:var(--inv)}
.fbtn b{font-variant-numeric:tabular-nums;font-weight:600;opacity:.85}
.fbtn:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.cnt{margin-left:auto;font-size:12px;color:var(--text-3);
     font-variant-numeric:tabular-nums}
/* ---------- 表格 ---------- */
table{border-collapse:collapse;width:100%}
#tbl{margin:0}
#tbl th,#tbl td{padding:8px 12px;text-align:left;vertical-align:middle;
                border-bottom:1px solid var(--line)}
#tbl th{position:sticky;top:var(--tools-h);z-index:2;background:var(--bg);
        border-bottom:0;box-shadow:inset 0 -1px 0 var(--border);
        font-size:11.5px;font-weight:600;color:var(--text-3);
        letter-spacing:.05em;white-space:nowrap}
#tbl td{font-size:13px;color:var(--text-hi)}
/* `#tbl th,#tbl td{text-align:left}` 特异性高于 `.num`，数字列要带 id 再声明一次
   才真正右对齐；th 的右内边距归零，让「文字 + 箭头」里的文字右缘正好落在
   数字右缘（td 有 12px 内边距，箭头 8px + 间隙 4px 抵掉它）。 */
#tbl .num{text-align:right}
#tbl th.num{padding-right:0}
#tbl td.name{font-size:13.5px;font-weight:500;color:var(--text)}
#tbl tbody tr:hover td{background:var(--line)}
#tbl tbody tr:hover td.url{color:var(--text-2)}
#tbl tr.bad td:first-child{box-shadow:inset 3px 0 0 var(--bad)}
#tbl tr.warn td:first-child{box-shadow:inset 3px 0 0 var(--warn)}
/* ---------- 表头排序：整格可点，箭头固定 8px，不挤压列宽 ---------- */
#tbl th.sorted{color:var(--text)}
.thb{display:inline-flex;align-items:center;gap:4px;margin:-2px 0;padding:2px 0;
     border:0;background:none;color:inherit;font:inherit;letter-spacing:inherit;
     cursor:pointer;white-space:nowrap}
.thb:hover{color:var(--text)}
.thb:focus-visible{outline:2px solid var(--accent);outline-offset:3px;border-radius:4px}
.arw{display:inline-block;width:8px;font-size:9px;line-height:1;text-align:center;
     color:var(--text-3);opacity:.6}
.thb:hover .arw{opacity:1}
th[aria-sort="none"] .arw::before{content:"\\2195"}      /* ↕ */
th[aria-sort="ascending"] .arw::before{content:"\\25B2"}  /* ▲ */
th[aria-sort="descending"] .arw::before{content:"\\25BC"} /* ▼ */
th[aria-sort="ascending"] .arw,th[aria-sort="descending"] .arw{
  color:var(--accent);opacity:1}
/* 组合排序的优先级序号：只有叠了第二列才出现，单列排序不打扰视线 */
.ord{display:none;min-width:13px;height:13px;padding:0 3px;border-radius:99px;
     background:var(--accent-solid);color:var(--inv);font-size:9px;font-weight:700;
     line-height:13px;text-align:center;font-variant-numeric:tabular-nums}
.ord.on{display:inline-block}
.num{text-align:right;font-variant-numeric:tabular-nums}
.spark{text-align:center} .spark svg{display:block;margin:0 auto}
.chip{display:inline-flex;align-items:center;gap:5px;padding:2px 9px 2px 7px;
      border-radius:99px;font-size:11.5px;font-weight:600;
      background:var(--card-2);color:var(--text-2)}
.chip::before{content:"";flex:none;width:5px;height:5px;border-radius:50%;
              background:currentColor}
.chip.ok{background:var(--ok-bg);color:var(--ok)}
.chip.warn{background:var(--warn-bg);color:var(--warn)}
.chip.bad{background:var(--bad-bg);color:var(--bad)}
.dup{font-size:11px;color:var(--text-3);margin-left:4px}
.hbar{display:block;width:80px;height:3px;margin:4px 0 0 auto;border-radius:2px;
      background:linear-gradient(90deg,currentColor var(--w),var(--card-2) var(--w))}
.hbar.ok{color:var(--ok)} .hbar.warn{color:var(--warn)}
.hbar.bad{color:var(--bad)} .hbar.off{color:var(--text-4)}
.avail.a-hi{color:var(--ok)} .avail.a-lo{color:var(--bad)}
.url{max-width:340px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11.5px;
     color:var(--text-4)}
#empty td{text-align:center;padding:32px;font-size:12.5px;color:var(--text-3)}
/* ---------- 表尾：排序摘要 + 分页 ---------- */
.tfoot{display:flex;align-items:center;justify-content:space-between;gap:10px 14px;
       flex-wrap:wrap;margin:0;padding:12px 0 0;border-top:1px solid var(--line)}
.sortrow{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.sortinfo{display:flex;align-items:center;gap:6px;flex-wrap:wrap;
          font-size:11.5px;color:var(--text-3)}
.sortinfo b{font-weight:600;color:var(--text-2);font-variant-numeric:tabular-nums}
.clr{margin-left:2px;padding:2px 9px;border-radius:6px;border:1px solid var(--border);
     background:var(--card);color:var(--text-2);font:inherit;font-size:11px;
     cursor:pointer;transition:border-color 120ms linear,color 120ms linear}
.clr:hover{border-color:var(--border-2);color:var(--text)}
.clr:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
#multi[aria-pressed="true"]{background:var(--accent-solid);
                            border-color:var(--accent-solid);color:var(--inv)}
.pager{display:flex;align-items:center;gap:8px 10px;margin-left:auto;flex-wrap:wrap}
.psize{display:flex;align-items:center;gap:6px;font-size:11.5px;color:var(--text-3)}
.psize select{padding:4px 6px;border-radius:7px;border:1px solid var(--border);
              background:var(--card);color:var(--text);font:inherit;font-size:12px;
              cursor:pointer}
.psize select:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.pinfo{font-size:11.5px;color:var(--text-3);font-variant-numeric:tabular-nums}
.pnums,.pnumlist{display:flex;align-items:center;gap:4px}
.pbtn{min-width:26px;height:26px;padding:0 7px;border-radius:7px;
      border:1px solid var(--border);background:var(--card);color:var(--text-hi);
      font:inherit;font-size:12px;font-variant-numeric:tabular-nums;cursor:pointer;
      transition:background-color 120ms linear,border-color 120ms linear}
.pbtn:hover:not(:disabled){border-color:var(--border-2);color:var(--text)}
.pbtn[aria-current="page"]{background:var(--accent-solid);
                           border-color:var(--accent-solid);color:var(--inv)}
.pbtn:disabled{opacity:.4;cursor:default}
.pbtn:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.pgap{padding:0 1px;font-size:12px;color:var(--text-4)}
/* ---------- 次级表格 ---------- */
table.sub{margin:0;width:100%}
table.sub th{padding:8px 12px;text-align:left;font-size:11.5px;font-weight:600;
             color:var(--text-3);letter-spacing:.05em;
             border-bottom:1px solid var(--border)}
table.sub td{padding:8px 12px;font-size:13px;color:var(--text-hi);
             border-bottom:1px solid var(--line)}
table.sub td.name{font-size:13.5px;font-weight:500;color:var(--text)}
table.sub tbody tr:hover td{background:var(--line)}
table.sub td.none{color:var(--text-3);font-size:12.5px}
/* 数字列的表头跟着数据一起右对齐，文字右缘落在数字右缘上 */
table.sub th.num{text-align:right}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;
      color:var(--text-hi)}
footer{margin-top:40px;padding-top:16px;border-top:1px solid var(--line);
       font-size:11.5px;color:var(--text-3)}
/* ---------- 响应式：列按优先级逐级隐藏 ---------- */
@media (min-width:1600px){.wrap{max-width:1480px}}
@media (max-width:1320px){
  .c-compat,.c-l3{display:none}
  /* 窄屏只留两项：当前值给结论，「较首日」给方向；均值让位——图上均值虚线
     已标出它的位置，而「变好还是变差」无法从图上直接读出。 */
  .chart-head .stat:nth-child(2){display:none}
}
/* URL 的 nowrap 会把表格 min-content 顶到 950px 以上；窄屏这列既不可点也读不全
   （title 提示在触屏无意义），整列让位给名称列，避免整页横向滚动。 */
@media (max-width:1200px){
  .c-url{display:none}
  #tbl .name{overflow-wrap:anywhere}
}
@media (max-width:900px){
  /* 窄屏工具条会折成 2–3 行，高度随内容（计数文字长度）变化，表头吸顶的
     top 偏移无法用静态变量对齐，会与工具条错位；此处取消表头吸顶，
     把竖向空间让给行内容，工具条吸顶保留。 */
  #tbl th{position:static}
  .c-avail,.c-sites{display:none}
  .plot{height:160px}
  .xaxis .p1{display:none}
  h2{scroll-margin-top:124px}
}
@media (max-width:600px){
  .c-spark{display:none}
  .plot{height:136px}
  .wrap{padding:20px 16px 48px}
  #q{width:100%}
  h1{font-size:19px}
  .cards{gap:8px}
  .card{min-width:92px;padding:12px 14px}
  .card b{font-size:22px}
  .hbar{width:44px}
  .chip{padding:2px 7px 2px 6px}
  /* 名称列给足宽度：短名（kstore合集 / 二月红·直播 / noimank多仓 / gh-proxy.com
     实测 67–88px）在 128px 列里都是一行。overflow-wrap 仍保留 anywhere ——
     真实数据里有 ~204px 的不可断长名（URL 兜底名），只有它能压住 min-content，
     否则 320–430px 会出现整页横向滚动。 */
  #tbl .name{min-width:128px}
  #tbl .c-live,#tbl .c-health{width:56px;padding-left:6px;padding-right:6px}
  #tbl .c-https{width:58px;padding-left:6px;padding-right:6px}
  .xaxis span{font-size:9.5px}
  /* 表尾竖排：排序摘要一行、分页一行，避免与计数文字挤成 3 行 */
  .tfoot{flex-direction:column;align-items:flex-start;gap:10px}
  .pager{width:100%;margin-left:0;justify-content:space-between}
}
@media (max-width:420px){
  /* 7 个主刻度在 ≤420px 时相邻两枚会贴住（实测 320px 最近间距 -4.6px），
     隔一隐藏后剩 4 个，间距回到 60px 以上。 */
  .xaxis .p0b{display:none}
}
@media (max-width:344px){
  #tbl .c-live{display:none}      /* 320px 只剩状态/名称/健康分，保不横滚 */
}
@media (prefers-reduced-motion:reduce){
  *{transition:none!important;animation:none!important}
}
/* ---------- 趋势时间范围切换 ---------- */
.trange{display:flex;gap:8px;margin:0 0 10px}
.trbtn{border:1px solid var(--border);background:var(--card);color:var(--text-2);
  border-radius:8px;padding:5px 14px;font-size:12.5px;cursor:pointer}
.trbtn:hover{background:var(--card-3);color:var(--text)}
.trbtn.active{background:var(--accent-solid);border-color:var(--accent-solid);
  color:var(--inv);font-weight:600}
figure.trend-panel[hidden]{display:none}
/* ---------- 复制链接按钮：截断的 URL 悬浮可见但复制不了，按钮补上这一步 ---------- */
td.c-url{white-space:nowrap}
.url .url-text{display:inline-block;vertical-align:middle;max-width:230px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.copy-btn{border:1px solid var(--border);background:var(--card);color:var(--accent-soft);
  border-radius:6px;padding:2px 8px;font-size:11.5px;line-height:1.4;
  cursor:pointer;vertical-align:middle;margin-left:6px}
.copy-btn:hover{background:var(--card-3);color:var(--accent-soft-2);border-color:var(--text-4)}
.copy-btn.copied{border-color:var(--ok-bg);background:var(--ok-bg);color:var(--ok)}
/* ---------- 订阅地址卡片 ---------- */
.sub-panel{background:var(--bg-2);border:1px solid var(--card);border-radius:12px;
  padding:12px 16px;margin:0 0 18px}
.sub-panel h3{margin:0 0 8px;font-size:13px;color:var(--text-2);font-weight:600}
.sub-item{display:flex;align-items:center;gap:10px;padding:4px 0}
.sub-item .sub-name{flex:none;width:76px;color:var(--text);font-size:13px}
.sub-item .sub-url{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
  font-size:11.5px;color:var(--text-4)}
@media (max-width:640px){.sub-item .sub-name{width:58px}}
"""

_SITE_JS = """\
const tbody = document.querySelector('#tbl tbody');
const rows = Array.from(tbody.querySelectorAll('tr')).filter(tr => tr.id !== 'empty');
const cnt = document.getElementById('cnt');
const q = document.getElementById('q');
const emptyRow = document.getElementById('empty');
const psize = document.getElementById('psize');
const pinfo = document.getElementById('pinfo');
const pnums = document.getElementById('pnums');
const pprev = document.getElementById('pprev');
const pnext = document.getElementById('pnext');
const sortinfo = document.getElementById('sortinfo');
const heads = Array.from(document.querySelectorAll('th[data-sort]'));
const COL = {};
heads.forEach(th => { COL[th.dataset.sort] = th.dataset.label; });

let kind = 'all';
let slowOnly = false;          // 仅看 slow：降权但仍可发布的慢源
let page = 1;
let size = 20;
let keys = [];                 // 组合排序：靠前的键优先，越靠后越是次级条件
let multiMode = false;         // 触屏没有 Shift 键，用开关把「叠加」变成显式模式
const multiBtn = document.getElementById('multi');

rows.forEach((tr, i) => { tr.dataset.i = i; });   // 稳定末位比较：还原服务端默认序

/* 排序值在构建期写到行上（dataset），运行时零文本解析；空串 = 无数据 */
const SORT_COLS = ['status', 'name', 'compat', 'sites', 'live', 'health',
                   'avail', 'https', 'l3'];
const DATA = new Map();
function dataOf(tr) {
  let o = DATA.get(tr);
  if (o) return o;
  o = { i: +tr.dataset.i };
  SORT_COLS.forEach(k => {
    const raw = tr.dataset[k];
    o[k] = (raw === undefined || raw === '') ? null
      : (/^-?\\d+(?:\\.\\d+)?$/.test(raw) ? +raw : raw);
  });
  DATA.set(tr, o);
  return o;
}

function cmpVal(a, b) {
  if (typeof a === 'number' && typeof b === 'number') return a - b;
  return String(a).localeCompare(String(b), 'zh-CN', { numeric: true });
}

function cmp(ta, tb) {
  const A = dataOf(ta), B = dataOf(tb);
  for (const { k, dir } of keys) {
    const av = A[k], bv = B[k];
    if (av === null && bv === null) continue;
    if (av === null) return 1;          // 缺数据恒排队尾，不随升降序翻面
    if (bv === null) return -1;
    const c = cmpVal(av, bv);
    if (c) return c > 0 ? dir : -dir;
  }
  return A.i - B.i;
}

function pageList(cur, pages) {
  if (pages <= 7) {
    return Array.from({ length: pages }, (_, i) => i + 1);
  }
  const out = [1];
  const lo = Math.max(2, cur - 2), hi = Math.min(pages - 1, cur + 2);
  if (lo > 2) out.push('…');
  for (let i = lo; i <= hi; i++) out.push(i);
  if (hi < pages - 1) out.push('…');
  out.push(pages);
  return out;
}

function syncHead() {
  heads.forEach(th => {
    const i = keys.findIndex(x => x.k === th.dataset.sort);
    const on = i >= 0;
    th.setAttribute('aria-sort',
      on ? (keys[i].dir > 0 ? 'ascending' : 'descending') : 'none');
    th.classList.toggle('sorted', on);
    const b = th.querySelector('.ord');
    b.textContent = (on && keys.length > 1) ? String(i + 1) : '';
    b.classList.toggle('on', on && keys.length > 1);
  });
  if (!keys.length) {
    sortinfo.textContent = '默认按置顶与健康分排列 · 点击表头排序';
    return;
  }
  const txt = keys.map(x => COL[x.k] + (x.dir > 0 ? ' ↑' : ' ↓')).join(' › ');
  sortinfo.innerHTML = '排序：<b>' + txt + '</b>'
    + '<button type="button" class="clr" id="sreset">清除</button>';
  document.getElementById('sreset').addEventListener('click', () => {
    keys = []; page = 1; syncHead(); render();
  });
}

function render() {
  const key = q.value.trim().toLowerCase();
  const list = rows.filter(tr => {
    const okKind = kind === 'all' || tr.dataset.kind === kind;
    // 状态排序值 1 = slow（见 _STATUS_RANK），复用行上已有的排序属性
    const okSlow = !slowOnly || tr.dataset.status === '1';
    const okText = !key || tr.textContent.toLowerCase().includes(key);
    return okKind && okSlow && okText;
  });
  list.sort(cmp);

  const total = list.length;
  const per = size || total || 1;
  const pages = Math.max(1, Math.ceil(total / per));
  page = Math.min(Math.max(1, page), pages);
  const start = (page - 1) * per;
  const view = size ? list.slice(start, start + size) : list;

  const keep = new Set(view);
  rows.forEach(tr => { if (!keep.has(tr)) tr.style.display = 'none'; });
  // 依次插到空态行之前：可见行在 DOM 里连续且顺序即排序结果
  view.forEach(tr => { tr.style.display = ''; tbody.insertBefore(tr, emptyRow); });
  emptyRow.style.display = total ? 'none' : '';

  const filtered = total !== rows.length;
  cnt.textContent = (filtered ? '符合 ' + total + ' / ' + rows.length : '共 ' + total + ' 个源')
    + (size && total > size ? ' · 第 ' + (start + 1) + '–' + (start + view.length) + ' 条' : '');

  pinfo.textContent = pages > 1 ? page + ' / ' + pages + ' 页' : '';
  pprev.disabled = page <= 1;
  pnext.disabled = page >= pages;
  pnums.innerHTML = pages > 1 ? pageList(page, pages).map(n =>
    n === '…' ? '<span class="pgap">…</span>'
      : '<button type="button" class="pbtn"' + (n === page ? ' aria-current="page"' : '')
        + ' data-p="' + n + '" aria-label="第 ' + n + ' 页">' + n + '</button>').join('') : '';
}

heads.forEach(th => {
  th.addEventListener('click', ev => {
    const k = th.dataset.sort;
    const i = keys.findIndex(x => x.k === k);
    const shift = ev.shiftKey || ev.metaKey || ev.ctrlKey;
    const multi = shift || multiMode;
    if (i >= 0 && !multi) {
      keys[i].dir = -keys[i].dir;                 // 同列再点 = 翻转方向
    } else if (i >= 0) {
      keys.splice(i, 1);                          // 叠加模式下点已参与的列 = 撤掉
    } else if (multi) {
      keys.push({ k: k, dir: th.dataset.type === 's' ? 1 : -1 });
    } else {
      // 单列：文本从升序起（A→Z），数值从降序起（大→小），符合看榜直觉
      keys = [{ k: k, dir: th.dataset.type === 's' ? 1 : -1 }];
    }
    page = 1;
    syncHead();
    render();
  });
});

document.querySelectorAll('.fbtn[data-kind]').forEach(btn => {
  btn.addEventListener('click', () => {
    kind = btn.dataset.kind;
    document.querySelectorAll('.fbtn[data-kind]').forEach(b =>
      b.classList.toggle('active', b === btn));
    page = 1;
    render();
  });
});

/* slow 快捷筛选：与类型筛选叠加，不改变 kind */
const slowBtn = document.getElementById('slowonly');
if (slowBtn) {
  slowBtn.addEventListener('click', () => {
    slowOnly = !slowOnly;
    slowBtn.classList.toggle('active', slowOnly);
    slowBtn.setAttribute('aria-pressed', slowOnly ? 'true' : 'false');
    page = 1;
    render();
  });
}

q.addEventListener('input', () => { page = 1; render(); });

multiBtn.addEventListener('click', () => {
  multiMode = !multiMode;
  multiBtn.setAttribute('aria-pressed', String(multiMode));
  multiBtn.classList.toggle('on', multiMode);
});

pprev.addEventListener('click', () => { page -= 1; render(); });
pnext.addEventListener('click', () => { page += 1; render(); });
pnums.addEventListener('click', ev => {
  const btn = ev.target.closest('button[data-p]');
  if (!btn) return;
  page = +btn.dataset.p;
  render();
});

psize.addEventListener('change', () => {
  size = +psize.value;
  page = 1;
  try { localStorage.setItem('tvk.psize', String(size)); } catch (e) {}
  render();
});

/* 每页条数记住上次选择：翻页是习惯，不该每次重新设 */
(function () {
  let v = null;
  try { v = localStorage.getItem('tvk.psize'); } catch (e) {}
  if (v !== null && Array.from(psize.options).some(o => o.value === v)) {
    size = +v;
    psize.value = v;
  }
})();

syncHead();
render();

/* ---------- 主题切换 + 趋势范围切换 ---------- */
(function () {
  const root = document.documentElement;
  let t = null;
  try { t = localStorage.getItem('tvk.theme'); } catch (e) {}
  if (t === 'light' || t === 'dark') { root.setAttribute('data-theme', t); }
  const btn = document.getElementById('theme-toggle');
  if (btn) {
    const paint = () => {
      btn.textContent = root.getAttribute('data-theme') === 'light' ? '🌙' : '☀️';
    };
    paint();
    btn.addEventListener('click', () => {
      const next = root.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
      root.setAttribute('data-theme', next);
      try { localStorage.setItem('tvk.theme', next); } catch (e) {}
      paint();
    });
  }
  document.addEventListener('click', (ev) => {
    const tb = ev.target.closest('.trbtn');
    if (!tb) return;
    document.querySelectorAll('.trbtn').forEach(b => {
      const on = b === tb;
      b.classList.toggle('active', on);
      b.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    document.querySelectorAll('figure.trend-panel[data-range]').forEach(f => {
      f.hidden = f.getAttribute('data-range') !== tb.getAttribute('data-range');
    });
  });
})();

/* ---------- 复制链接：事件委托，分页/筛选后新增的按钮同样生效 ---------- */
async function tvkCopy(t) {
  try { await navigator.clipboard.writeText(t); return true; } catch (e) {}
  /* 非 HTTPS（如本地 file:// 预览）走降级方案 */
  try {
    const ta = document.createElement('textarea');
    ta.value = t; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.focus(); ta.select();
    const ok = document.execCommand('copy');
    ta.remove(); return ok;
  } catch (e) { return false; }
}
document.addEventListener('click', async (ev) => {
  const btn = ev.target.closest('.copy-btn');
  if (!btn) return;
  ev.preventDefault();
  const ok = await tvkCopy(btn.getAttribute('data-url') || '');
  const old = btn.textContent;
  btn.textContent = ok ? '已复制' : '失败';
  btn.classList.toggle('copied', ok);
  clearTimeout(btn._t);
  btn._t = setTimeout(() => {
    btn.textContent = old; btn.classList.remove('copied');
  }, 1200);
});
"""


def build_site(
    rows: list[dict],
    histories: dict[str, list[dict]],
    failure_stats: list[dict] | None,
    duocang_count: int,
    min_health: float,
    trend: list[dict] | None = None,
    hosts: list[dict] | None = None,
    changes: list[dict] | None = None,
    lite_count: int = 0,
    detail_links: dict | None = None,
    sub_urls: dict | None = None,
) -> str:
    """生成自包含的静态目录页（无外部依赖，可直接挂 GitHub Pages）。"""
    now = time.strftime("%Y-%m-%d %H:%M")
    esc = html.escape
    by_status = Counter(r["status"] for r in rows)
    card_specs = [("总数", len(rows), "all")] + [
        (status, by_status[status], _STATUS_CLASS.get(status, "off"))
        for status in STATUS_ORDER if by_status.get(status)
    ]
    cards = "".join(
        f'<div class="card {cls}"><b>{n}</b><span>{esc(label)}</span></div>'
        for label, n, cls in card_specs)

    trs = []
    kind_count: Counter[str] = Counter()
    for r in sorted((x for x in rows if not x.get("banned")),
                    key=lambda x: (-x.get("starred", 0), -x.get("health_score", 0))):
        marks = [h.get("ok", 0) for h in histories.get(r["id"], [])]
        cls = _STATUS_CLASS.get(r["status"], "off")
        kind = site_kind(r)
        kind_count[kind] += 1
        l3 = f"{r['l3_ok']}/{r['l3_tested']}" if r.get("l3_tested") else "—"
        live_disp = (r["live_channels"] if r.get("schema_type") == "live"
                     else r["lives_count"])
        score = float(r.get("health_score") or 0)
        width = max(0, min(100, int(round(score))))
        # 30 天可用率按阈值上色；数值保持原文（可能带 %，也可能是 —）
        avail_raw = r.get("avail")
        av_num = None
        if isinstance(avail_raw, (int, float)):
            av_num, avail = float(avail_raw), f"{avail_raw}%"
        elif isinstance(avail_raw, str) and avail_raw.strip() not in ("", "—", "-"):
            avail = avail_raw
            try:
                av_num = float(avail_raw.strip().rstrip("%"))
            except ValueError:
                av_num = None
        else:
            avail = "—"
        if av_num is None:
            av_cls = ""
        else:
            av_cls = " a-hi" if av_num >= 95 else (" a-lo" if av_num < 80 else "")
        dup = (f' <span class="dup">归并#{esc(str(r["dup_of"])[:8])}</span>'
               if r.get("dup_of") else "")
        # 排序用的原始值一次性挂在行上：前端不必读文本猜类型，空串统一按「无数据」
        sv_sites = int(r.get("sites_count") or 0)
        try:
            sv_live = "" if live_disp is None else str(int(live_disp))
        except (TypeError, ValueError):
            sv_live = ""
        sv_l3 = (f"{float(r['l3_ok']) / float(r['l3_tested']):.4f}"
                 if r.get("l3_tested") else "")
        sv_av = "" if av_num is None else f"{av_num:.2f}"
        # HTTPS 覆盖率（0~1）；分母为 0 时留空，排序时按「无数据」处理
        scheme_total = int(r.get("https_sites") or 0) + int(r.get("http_sites") or 0)
        https_num = int(r.get("https_sites") or 0)
        https_disp = (f"{https_num * 100 // scheme_total}%"
                      if scheme_total else "—")
        sv_https = f"{https_num / scheme_total:.4f}" if scheme_total else ""
        sort_attrs = (
            f' data-status="{_STATUS_RANK.get(r["status"], 9)}"'
            f' data-name="{esc(r.get("name") or "")}"'
            f' data-compat="{esc(r.get("compat") or "")}"'
            f' data-sites="{sv_sites}" data-live="{sv_live}"'
            f' data-health="{score:.2f}" data-avail="{sv_av}"'
            f' data-l3="{sv_l3}" data-https="{sv_https}"')
        name_cell = esc(r.get("name") or "")
        link = (detail_links or {}).get(r["id"])
        if link:
            name_cell = f'<a href="{esc(link)}">{name_cell}</a>'
        trs.append(
            f'<tr class="{cls}" data-kind="{kind}"{sort_attrs}>'
            f'<td><span class="chip {cls}">{esc(r["status"])}</span>{dup}</td>'
            f'<td class="name">{name_cell}</td>'
            f'<td class="c-compat">{esc(r.get("compat") or "—")}</td>'
            f'<td class="c-sites num">{r["sites_count"]}</td>'
            f'<td class="c-live num">{live_disp}</td>'
            f'<td class="c-health num">{score:g}'
            f'<i class="hbar {cls}" style="--w:{width}%"></i></td>'
            f'<td class="c-avail num avail{av_cls}">{esc(str(avail))}</td>'
            f'<td class="c-https num" title="站点/解析/直播/spider 的 https 占比">'
            f'{https_disp}</td>'
            f'<td class="c-l3 num">{l3}</td>'
            f'<td class="c-spark spark">{_sparkline(marks)}</td>'
            f'<td class="c-url url" title="{esc(r["url"])}">'
            f'<span class="url-text">{esc(r["url"][:80])}</span>'
            f'<button type="button" class="copy-btn" data-url="{esc(r["url"])}" '
            f'aria-label="复制完整链接">复制</button></td>'
            f'</tr>')

    doc_total = sum(kind_count.values())
    live_n = kind_count.get("live", 0)
    vod_n = kind_count.get("vod", 0)
    slow_n = by_status.get("slow", 0)
    # 榜单 CSV 与站点同一父目录（output/），文件名含中文需 URL 编码
    csv_href = quote_plus("榜单.csv").replace("+", "%20")
    lite_note = f" · 精简版 {lite_count} 个源" if lite_count else ""
    trend_block = _trend_panel(trend, doc_total, live_n)

    # 订阅地址卡片：把可直接导入的链接摆到最显眼处，一键复制
    sub_panel = ""
    if sub_urls:
        sub_items = "".join(
            f'<div class="sub-item"><span class="sub-name">{esc(k)}</span>'
            f'<code class="sub-url" title="{esc(v)}">{esc(v)}</code>'
            f'<button type="button" class="copy-btn" data-url="{esc(v)}" '
            f'aria-label="复制{k}订阅地址">复制</button></div>'
            for k, v in sub_urls.items())
        sub_panel = (f'<div class="sub-panel"><h3>订阅地址 · 点击「复制」即可导入 '
                     f'TVBox / 影视仓</h3>{sub_items}</div>')

    # 内容变化摘要：让「本轮是否值得点开明细」一眼可见
    chg_line = ""
    if changes:
        sum_a = sum(int((c.get("change") or {}).get("a") or 0) for c in changes)
        sum_r = sum(int((c.get("change") or {}).get("r") or 0) for c in changes)
        chg_line = (f'<p class="chg-line">本轮内容变化：{len(changes)} 个源共 '
                    f'+{sum_a} / −{sum_r} 个站点 · '
                    f'<a href="#changes">查看明细</a></p>')

    chg_rows = "".join(
        f"<tr><td class='name'>{esc(c['name'])}</td>"
        f"<td>{esc(diffing.describe(c.get('change') or {}))}</td></tr>"
        for c in (changes or [])[:6])
    if not chg_rows:
        chg_rows = '<tr><td colspan="2" class="none">本轮无内容变化</td></tr>'
    chg_block = (f'<h2 id="changes">本轮内容变化 · 站点级</h2>'
                 f'<table class="sub"><tbody><tr><th>源</th><th>变化</th></tr>'
                 f'{chg_rows}</tbody></table>')

    host_rows = "".join(
        f"<tr><td class='mono'>{esc(h['host'])}</td>"
        f"<td class='num'>{h['fails']}</td><td class='num'>{h['sources']}</td></tr>"
        for h in (hosts or [])[:8])
    host_block = (f'<h2>失败主机分布 · 近 30 天</h2>'
                  f'<table class="sub"><tbody><tr><th>主机</th>'
                  f"<th class='num'>失败次数</th>"
                  f"<th class='num'>涉及源</th></tr>{host_rows}</tbody></table>"
                  if host_rows else "")

    fail_rows = "".join(
        f"<tr><td>{esc(f['reason'])}</td><td class='num'>{f['count']}</td>"
        f"<td class='num'>{f['sources']}</td></tr>"
        for f in (failure_stats or [])[:10])
    fail_section = (f'<h2>近 30 天失败原因</h2>'
                    f'<table class="sub"><tbody><tr><th>原因</th>'
                    f"<th class='num'>次数</th>"
                    f"<th class='num'>涉及源</th></tr>{fail_rows}</tbody></table>"
                    if fail_rows else "")

    # 搜索无命中时的空态行：全表无数据与「筛掉了」要给出不同文案
    empty_text = ("没有匹配的源，试试清空关键词" if trs else "暂无源记录")

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TVBoxKeeper · 源体检看板</title>
<style>
{_SITE_CSS}</style></head><body>
<div class="wrap">
<div class="head">
<h1>TVBoxKeeper · 源体检看板</h1>
<button type="button" id="theme-toggle" class="tbtn"
        aria-label="切换亮色/暗色主题">☀️</button>
</div>
<p class="meta">生成于 {now} · 订阅门槛：健康分 ≥ {min_health} · 订阅源 {duocang_count} 个{lite_note} · <a class="dl" href="../{csv_href}" download>下载榜单 CSV</a></p>
<div class="cards">{cards}</div>
{sub_panel}{chg_line}{trend_block}
<div class="tools">
<input id="q" type="search" placeholder="筛选名称 / 状态 / URL…" aria-label="按名称或链接筛选源">
<div class="filters" role="group" aria-label="按类型筛选">
<button type="button" class="fbtn active" data-kind="all">全部 <b>{doc_total}</b></button>
<button type="button" class="fbtn" data-kind="live">直播 <b>{live_n}</b></button>
<button type="button" class="fbtn" data-kind="vod">非直播 <b>{vod_n}</b></button>
<button type="button" class="fbtn" id="slowonly" aria-pressed="false">仅看 slow <b>{slow_n}</b></button>
</div>
<span class="cnt" id="cnt" role="status"></span>
</div>
<table id="tbl">
<caption class="sr-only">源榜单</caption>
<thead><tr>
{_th("status", "状态")}{_th("name", "名称", "s")}{_th("compat", "适配", "s", "c-compat")}
{_th("sites", "站点", "n", "c-sites num")}{_th("live", "直播", "n", "c-live num")}
{_th("health", "健康分", "n", "c-health num")}
{_th("avail", "30天可用", "n", "c-avail num")}{_th("https", "HTTPS", "n", "c-https num")}
{_th("l3", "L3", "n", "c-l3 num")}
<th class="c-spark">近期体检</th><th class="c-url">URL</th>
</tr></thead>
<tbody>
{''.join(trs)}
<tr id="empty" style="display:none"><td colspan="11">{empty_text}</td></tr>
</tbody>
</table>
<div class="tfoot">
<div class="sortrow">
<div class="sortinfo" id="sortinfo" aria-live="polite">默认按置顶与健康分排列</div>
<button type="button" class="clr" id="multi" aria-pressed="false"
 title="开启后点击表头会追加排序条件；桌面端也可按住 Shift 直接叠加">叠加排序</button>
</div>
<nav class="pager" aria-label="表格分页">
<label class="psize">每页<select id="psize" aria-label="每页显示条数">
<option value="20" selected>20</option><option value="50">50</option>
<option value="100">100</option><option value="0">全部</option>
</select>条</label>
<span class="pinfo" id="pinfo"></span>
<div class="pnums">
<button type="button" class="pbtn" id="pprev" aria-label="上一页">‹</button>
<span class="pnumlist" id="pnums"></span>
<button type="button" class="pbtn" id="pnext" aria-label="下一页">›</button>
</div>
</nav>
</div>
{chg_block}
{host_block}
{fail_section}
<footer>TVBoxKeeper 自动生成 · 内容仅为配置可达性元数据，不含任何音视频</footer>
</div>
<script>
{_SITE_JS}</script>
</body></html>"""


# ---------- 超级直播列表 ----------

# 央视频道套数别名（长 key 先替换，避免"十一套"里的"一套"误替换）
_SUITE_MAP = {
    "十七套": "17", "十六套": "16", "十五套": "15", "十四套": "14",
    "十三套": "13", "十二套": "12", "十一套": "11", "十套": "10",
    "九套": "9", "八套": "8", "七套": "7", "六套": "6", "五套": "5",
    "四套": "4", "三套": "3", "二套": "2", "一套": "1",
}


def channel_key(name: str) -> str:
    """频道名归一键：CCTV-1/央视一套/CCTV1综合 → CCTV1。

    规则：中央/央视→CCTV，N套→N，+→P，去掉常见频道描述词与分隔符；
    中文频道名（如 东方卫视）保留原文，仅去分隔符。
    """
    n = name.strip().upper()
    n = n.replace("中央", "CCTV").replace("央視", "CCTV").replace("央视", "CCTV")
    for cn, num in sorted(_SUITE_MAP.items(), key=lambda kv: -len(kv[0])):
        if cn in n:
            n = n.replace(cn, num)
    n = n.replace("+", "P").replace("＋", "P")
    # 去掉 CCTV 系常见描述词（卫视不能去，它是真正频道名的一部分）
    n = re.sub(r"综合|财经|体育|电影|电视剧|戏曲|科教|少儿|音乐|纪实|农业"
               r"|军事|国防|女性时尚|风云|频道|高清|超清|标清|蓝光|iptv",
               "", n)
    # 只保留字母数字与汉字，去掉分隔符（-、·、空格、全角符号等）
    n = re.sub(r"[^A-Z0-9\u4e00-\u9fff]", "", n)
    return n


def _parse_live_channels(text: str) -> list[tuple[str, str, str]]:
    """完整解析直播列表 → [(分组, 频道名, url)]；m3u 归入"其他"组。"""
    out: list[tuple[str, str, str]] = []
    group = "其他"
    is_m3u = text.lstrip().startswith("#EXTM3U")
    pending_name = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.endswith("#genre#"):
            group = line[:-len("#genre#")].strip().rstrip(",").strip() or "其他"
            continue
        if line.startswith("#EXTINF"):
            pending_name = line.split(",", 1)[1].strip() if "," in line else ""
            continue
        if line.startswith("#"):
            continue
        if "," in line:
            cname, url = line.split(",", 1)[0].strip(), line.split(",", 1)[1].strip()
        elif is_m3u:
            cname, url = pending_name or "直播", line.strip()
        else:
            continue
        pending_name = ""
        if url.startswith(("http://", "https://", "rtmp://", "rtsp://")):
            out.append((group, cname, url))
    return out


def build_super_live(
    sources: list[tuple[str, bytes]],
    max_urls: int = 4,
    dead_urls: "set[str] | frozenset[str]" = frozenset(),
) -> tuple[str, dict]:
    """合并多个直播源为超级列表。

    同名频道（归一键相同）合并为一行，多线路用 # 互备（TVBox 原生支持），
    央视/卫视/其他分组排序。dead_urls 是窗口内测活判死的线路（livetest
    产出），构建时剔除——线路全死的频道整体去掉。返回 (txt 内容, 统计)。
    """
    groups: dict[str, dict[str, dict]] = {}
    group_order: list[str] = []
    stats = {"sources": len(sources), "in_channels": 0, "channels": 0,
             "urls": 0, "dead_dropped": 0}

    for name, content in sources:
        text = classify.decode_content(content)
        for group, cname, url in _parse_live_channels(text):
            stats["in_channels"] += 1
            if group not in groups:
                groups[group] = {}
                group_order.append(group)
            key = channel_key(cname) or cname.strip()
            entry = groups[group].setdefault(key, {"name": cname, "urls": []})
            for u in url.split("#"):
                u = u.strip()
                if not u or u in entry["urls"] or len(entry["urls"]) >= max_urls:
                    continue
                if u in dead_urls:
                    stats["dead_dropped"] += 1
                    continue
                entry["urls"].append(u)

    def group_rank(g: str) -> tuple:
        if "CCTV" in g.upper() or "央" in g:
            return (0, group_order.index(g))
        if "卫视" in g:
            return (1, group_order.index(g))
        return (2, group_order.index(g))

    lines: list[str] = []
    for g in sorted(groups, key=group_rank):
        alive = [e for e in groups[g].values() if e["urls"]]
        if not alive:
            continue  # 组内频道线路全被剔除则整组不输出
        lines.append(f"{g},#genre#")
        for entry in alive:
            stats["channels"] += 1
            stats["urls"] += len(entry["urls"])
            lines.append(f"{entry['name']},{'#'.join(entry['urls'])}")
    if not lines:
        return "", stats
    return "\n".join(lines) + "\n", stats


# ---------- 通知增强 ----------


def dingtalk_sign(url: str, secret: str) -> str:
    """钉钉机器人"加签"安全模式：HMAC-SHA256 时间戳签名追加到 URL。"""
    ts = str(round(time.time() * 1000))
    digest = hmac.new(secret.encode(), f"{ts}\n{secret}".encode(),
                      hashlib.sha256).digest()
    sign = base64.b64encode(digest).decode()
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}timestamp={ts}&sign={quote_plus(sign)}"


# ---------- 单源详情页 ----------


# 亮/暗主题 token 与切换脚本（详情页用；看板的 _SITE_CSS 内嵌同一套值）
_THEME_ROOT = """
:root{
  color-scheme:dark;
  --bg:#0f172a; --panel:#131e33; --card:#1e293b; --card-2:#24334a;
  --line:#1c2840; --border:#334155; --border-2:#3d4f6b;
  --text:#e2e8f0; --text-2:#94a3b8; --text-3:#7d8ea6; --text-4:#475569;
  --ok:#4ade80; --ok-bg:#14532d; --warn:#fbbf24; --warn-bg:#451a03;
  --bad:#f87171; --bad-bg:#450a0a; --accent:#3b82f6; --accent-solid:#1d4ed8;
  --accent-soft:#93c5fd; --accent-soft-2:#bfdbfe; --link:#60a5fa;
  --text-hi:#cbd5e1; --title:#f8fafc; --inv:#ffffff;
  --bg-2:#0b1220; --card-3:#273449; --grid:#1f2d45;
}
html[data-theme="light"]{
  color-scheme:light;
  --bg:#f5f7fb; --bg-2:#eef1f8; --panel:#ffffff; --card:#ffffff; --card-2:#f1f4f9;
  --line:#e3e8f0; --border:#d7dde8; --border-2:#c4cdda;
  --text:#1f2937; --text-2:#5b6474; --text-3:#6b7280; --text-4:#98a1b0;
  --ok:#15803d; --ok-bg:#dcfce7; --warn:#b45309; --warn-bg:#fef3c7;
  --bad:#dc2626; --bad-bg:#fee2e2; --accent:#2563eb; --accent-solid:#1d4ed8;
  --accent-soft:#2563eb; --accent-soft-2:#1d4ed8; --link:#2563eb;
  --text-hi:#111827; --title:#0f172a; --inv:#ffffff;
  --bg-2:#eef1f8; --card-3:#e7ecf5; --grid:#dde3ee;
}
"""

_THEME_JS = """
(function () {
  const root = document.documentElement;
  let t = null;
  try { t = localStorage.getItem('tvk.theme'); } catch (e) {}
  if (t === 'light' || t === 'dark') { root.setAttribute('data-theme', t); }
  const btn = document.getElementById('theme-toggle');
  if (btn) {
    const paint = () => {
      btn.textContent = root.getAttribute('data-theme') === 'light' ? '🌙' : '☀️';
    };
    paint();
    btn.addEventListener('click', () => {
      const next = root.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
      root.setAttribute('data-theme', next);
      try { localStorage.setItem('tvk.theme', next); } catch (e) {}
      paint();
    });
  }
})();
"""


def build_source_detail(row: dict, history: list[dict]) -> str:
    """生成单源详情页：元信息 + 体检历史 + 内容变更记录。"""
    detail_theme = _THEME_ROOT
    theme_js = _THEME_JS
    esc = html.escape
    hist_rows = "".join(
        f"<tr class='{'ok' if h['ok'] else 'bad'}'><td>{esc(h['ts'])}</td>"
        f"<td>{esc(h['level'])}</td><td>{'OK' if h['ok'] else 'FAIL'}</td>"
        f"<td class='num'>{h['latency_ms']}</td>"
        f"<td>{esc(str(h['parsed'].get('schema') or '-'))}</td>"
        f"<td>{esc(str(h['parsed'].get('error') or ''))}</td></tr>"
        for h in reversed(history))
    changes = "".join(
        f"<li>{esc(h['ts'])}："
        f"+{len((h['parsed'].get('chg') or {}).get('added') or [])} / "
        f"-{len((h['parsed'].get('chg') or {}).get('removed') or [])} 站点</li>"
        for h in history if h["parsed"].get("chg"))
    https_n = int(row.get("https_sites") or 0)
    http_n = int(row.get("http_sites") or 0)
    scheme_total = https_n + http_n
    # 分母只含 http(s) 站点；全是相对路径/csp 站点时展示占位符
    https_cov = (f"{https_n}/{scheme_total}（{https_n * 100 // scheme_total}%）"
                 if scheme_total else "—")
    meta = "".join(
        f"<tr><th>{esc(label)}</th><td>{esc(str(value))}</td></tr>"
        for label, value in [
            ("状态", row.get("status", "")),
            ("健康分", row.get("health_score", "")),
            ("适配", row.get("compat") or "—"),
            ("30天可用", row.get("avail") or "—"),
            ("站点/直播/子仓",
             f"{row.get('sites_count', 0)} / {row.get('lives_count', 0)} / "
             f"{row.get('storehouse_count', 0)}"),
            ("HTTPS 覆盖率", https_cov),
            ("延迟", f"{row.get('latency_ms', 0)} ms"),
            ("最近检查", row.get("last_checked") or "—"),
            ("URL", row.get("url", "")),
        ])
    change_html = f"<h2>内容变更</h2><ul>{changes}</ul>" if changes else ""
    hist_html = (f"<h2>体检历史</h2><table><tr><th>时间</th><th>档位</th>"
                 f"<th>结果</th><th>延迟</th><th>类型</th><th>备注</th></tr>"
                 f"{hist_rows}</table>" if hist_rows else "<p>暂无体检记录。</p>")
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(row.get('name') or '')} · 详情</title>
<style>
{detail_theme}
body {{ margin: 0; padding: 24px; font: 14px/1.6 system-ui, "Segoe UI", "Microsoft YaHei", sans-serif;
       background: var(--bg); color: var(--text); }}
h1 {{ font-size: 18px; }} h2 {{ font-size: 15px; color: var(--text-2); }}
a {{ color: var(--link); }}
.tbtn {{ float: right; border: 1px solid var(--border); background: var(--card);
        color: var(--text-2); border-radius: 8px; padding: 4px 10px;
        font-size: 13px; cursor: pointer; }}
.tbtn:hover {{ background: var(--card-3); color: var(--text); }}
table {{ border-collapse: collapse; width: 100%; margin-bottom: 20px; }}
th, td {{ padding: 5px 10px; border-bottom: 1px solid var(--card); text-align: left; }}
th {{ color: var(--text-2); font-weight: 500; }}
tr.ok td:nth-child(3) {{ color: var(--ok); }}
tr.bad td:nth-child(3) {{ color: var(--bad); }}
.num {{ font-variant-numeric: tabular-nums; }}
.meta td {{ color: var(--text-hi); }}
</style></head><body>
<h1><a href="index.html">← 返回看板</a> · {esc(row.get('name') or '')}
<button type="button" id="theme-toggle" class="tbtn"
        aria-label="切换亮色/暗色主题">☀️</button></h1>
<table class="meta">{meta}</table>
{change_html}
{hist_html}
<script>{theme_js}</script>
</body></html>"""


# ---------- 周报 ----------


def build_weekly_markdown(
    breakdown: str,
    daily: list[dict],
    failure_stats: list[dict],
    changes: list[dict],
    host_failures: list[dict],
) -> str:
    """每周日生成：状态分布 / 7 天可用率趋势 / 内容变化 / 失败原因与主机。"""
    now = time.strftime("%Y-%m-%d")
    lines = [f"# TVBox 源周报 · {now}", "",
             f"- 当前状态分布：{breakdown}", ""]
    if daily:
        last7 = daily[-7:]
        avg = round(sum(d["rate"] for d in last7) / len(last7))
        trend = " | ".join(f"{d['date'][5:]} {d['rate']}%" for d in last7)
        lines += ["## 近 7 天整体可用率", "", trend, "",
                  f"- 7 日平均：**{avg}%**（共体检 {sum(d['total'] for d in last7)} 次）", ""]
    if changes:
        lines += ["## 本周内容变化（Top）", ""]
        for c in changes[:8]:
            chg = c.get("change") or {}
            lines.append(f"- {c['name']}："
                         f"+{len(chg.get('added') or [])} / "
                         f"-{len(chg.get('removed') or [])} 站点")
        lines.append("")
    if failure_stats:
        lines += ["## 近 7 天失败原因", ""]
        for f in failure_stats[:5]:
            lines.append(f"- {f['reason']}：{f['count']} 次 / {f['sources']} 个源")
        lines.append("")
    if host_failures:
        lines += ["## 失败集中主机", ""]
        for h in host_failures[:5]:
            lines.append(f"- {h['host']}：{h['fails']} 次 / {h['sources']} 个源"
                         + ("（整站疑似不可用）" if h["sources"] >= 3 else ""))
        lines.append("")
    return "\n".join(lines) + "\n"
