"""发布：storeHouse 多仓 JSON + Markdown 体检榜单 + 静态网页。"""

from __future__ import annotations

import html
import json
import time
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

from . import diffing
from .normalize import guess_name

# 状态展示顺序：存活 → 观察中 → 结构/网络异常 → 人工处置
STATUS_ORDER = ("active", "flaky", "anti_crawl", "invalid", "dead", "new", "banned")


def status_breakdown(rows: list[dict]) -> str:
    """完整状态分布：按固定顺序列出非零项，未知状态追加在后，避免统计口径漏项。"""
    counts = Counter(r["status"] for r in rows)
    parts = [f"{s} {counts[s]}" for s in STATUS_ORDER if counts.get(s)]
    parts += [f"{s} {n}" for s, n in sorted(counts.items())
              if s not in STATUS_ORDER and n]
    return " / ".join(parts) or "—"


def pick_sources(rows: list[dict], min_health: float) -> list[dict]:
    """筛出可进入多仓订阅的配置源：active + 达标 + 未封禁 + 非重复 + 非直播。"""
    picked = [
        r for r in rows
        if r.get("status") == "active"
        and r.get("health_score", 0) >= min_health
        and not r.get("banned")
        and not r.get("dup_of")
        and r.get("schema_type") != "live"
    ]
    picked.sort(key=lambda r: (-r.get("starred", 0), -r.get("health_score", 0)))
    return picked


def pick_live_sources(rows: list[dict], min_health: float) -> list[dict]:
    """筛出可进入直播合集的直播源（schema_type=live）。"""
    picked = [
        r for r in rows
        if r.get("schema_type") == "live"
        and r.get("status") == "active"
        and r.get("health_score", 0) >= min_health
        and not r.get("banned")
        and not r.get("dup_of")
    ]
    picked.sort(key=lambda r: (-r.get("starred", 0),
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
    for r in sorted((r for r in rows if not r.get("banned")),
                    key=lambda x: (-x.get("starred", 0), -x.get("health_score", 0))):
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
        lines.append(
            f"| {r['status']}{dup} | {name} | {compat} | {r['sites_count']} | {live_disp} "
            f"| {r['storehouse_count']} | {probe} | {l3} | {r['health_score']} "
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


def webhook_payload(platform: str, text: str) -> dict:
    """按平台构造 webhook 消息体（都是纯文本，国内直连可用）。"""
    plain = text.replace("**", "").replace("`", "").replace("## ", "【").replace("\n## ", "\n【")
    if platform == "feishu":
        return {"msg_type": "text", "content": {"text": plain}}
    # 企业微信 / 钉钉同为 {"msgtype": "text", ...}
    return {"msgtype": "text", "text": {"content": plain}}


_STATUS_CLASS = {
    "active": "ok", "anti_crawl": "warn", "flaky": "warn",
    "invalid": "bad", "dead": "bad", "banned": "off", "new": "off",
}

# 按「可用程度」而非字母序排序：active 优先，死源沉底；未知状态排最后
_STATUS_RANK = {
    "active": 0, "new": 1, "flaky": 2, "anti_crawl": 3,
    "invalid": 4, "dead": 5, "banned": 6,
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
    color = "#4ade80" if rate >= 0.8 else ("#fbbf24" if rate >= 0.5 else "#f87171")
    return (f'<svg width="{width}" height="{height}" aria-hidden="true">'
            f'<polyline fill="none" stroke="{color}" stroke-width="2" '
            f'points="{pts}"/></svg>')


def _rate_color(rate: float) -> str:
    """可用率色档（与内联 sparkline 同口径）。"""
    return "#4ade80" if rate >= 80 else ("#fbbf24" if rate >= 50 else "#f87171")


def _rate_class(rate: float) -> str:
    return "ok" if rate >= 80 else ("warn" if rate >= 50 else "bad")


_TREND_EMPTY = ('<figure class="trend-panel"><div class="chart-empty">'
                '暂无体检记录，明日开始生成趋势</div></figure>')


def _trend_panel(points: list[dict] | None, n_total: int = 0,
                 n_live: int = 0) -> str:
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
    structs = [{"k": "bottom", "v": low, "stroke": "#334155"},
               {"k": "mid", "v": mid, "stroke": "#1f2d45"}]
    if high < 100:      # high==100 时顶线与绘图区上边界重合，不画
        structs.append({"k": "top", "v": high, "stroke": "#1f2d45"})

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
        style = ('stroke="#7d8ea6" stroke-width="1" stroke-opacity="0.45" '
                 'stroke-dasharray="4 3"' if name == "mean" else
                 'stroke="#334155" stroke-width="1" stroke-dasharray="2 4"')
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
        f'<figure class="trend-panel" aria-labelledby="trend-h">'
        f'<div class="trend-top">'
        f'<figcaption><h2 id="trend-h">整体可用率趋势 · '
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
        f'<span class="note">{note}</span></div>'
        f'<table class="sr-only"><caption>近 {n_axis} 天可用率明细</caption>'
        f'<tr><th>日期</th><th>可用率</th><th>体检次数</th></tr>'
        f'{"".join(rows_sr)}</table>'
        f'</figure>')


_SITE_CSS = """\
:root{
  color-scheme:dark;
  --bg:#0f172a; --panel:#131e33; --card:#1e293b; --card-2:#24334a;
  --line:#1c2840; --border:#334155; --border-2:#3d4f6b;
  --text:#e2e8f0; --text-2:#94a3b8; --text-3:#7d8ea6; --text-4:#475569;
  --ok:#4ade80; --ok-bg:#14532d; --warn:#fbbf24; --warn-bg:#451a03;
  --bad:#f87171; --bad-bg:#450a0a; --accent:#3b82f6; --accent-solid:#1d4ed8;
  --tools-h:63px;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
     font:14px/1.6 system-ui,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:1320px;margin:0 auto;padding:32px 40px 64px}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;
         overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
h1{font-size:22px;font-weight:700;color:#f8fafc;margin:0 0 6px;letter-spacing:-.01em}
.meta{font-size:12.5px;color:var(--text-3);margin:0 0 24px}
h2{display:flex;align-items:center;gap:8px;font-size:14px;font-weight:600;
   color:#cbd5e1;margin:40px 0 12px;scroll-margin-top:88px}
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
.card.all b{color:#f8fafc}
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
  background:linear-gradient(90deg,transparent,#334155 45%,#334155 55%,transparent)}
.pt:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.dot{position:absolute;left:50%;top:var(--y);display:block;border-radius:50%;
     border:2px solid currentColor;background:var(--panel);box-sizing:border-box;
     transform:translate(-50%,-50%);
     transition:box-shadow 120ms linear,outline-color 120ms linear}
.dot.s5{width:5px;height:5px} .dot.s7{width:7px;height:7px}
.dot.s9{width:9px;height:9px}
/* 孤岛点：5px 实心灰，与线色的空心点明确区分 */
.dot.isl{width:5px;height:5px;background:#94a3b8;border-color:#94a3b8}
.pt:hover .dot,.pt:focus-visible .dot{box-shadow:0 0 0 4px rgba(148,163,184,.18);
                                      outline:1.5px solid currentColor;outline-offset:1px}
.vlab{position:absolute;left:50%;top:var(--y);transform:translate(-50%,-100%);
      margin-top:-10px;font-size:11px;font-weight:600;color:currentColor;
      font-variant-numeric:tabular-nums;white-space:nowrap;pointer-events:none;
      text-shadow:0 1px 2px #0b1424,0 0 6px #0b1424}
.vlab.vb{transform:translate(-50%,0);margin-top:10px}
/* tooltip 走纯 CSS，不依赖 JS */
.pt::after{content:attr(data-tip);position:absolute;left:50%;top:var(--y);z-index:4;
           transform:translate(-50%,-150%);padding:4px 8px;border-radius:6px;
           background:#0b1220;border:1px solid var(--border-2);color:var(--text);
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
      background:var(--card);color:#cbd5e1;font:inherit;font-size:13px;cursor:pointer;
      transition:background-color 120ms linear,border-color 120ms linear}
.fbtn:hover{border-color:var(--border-2);color:var(--text)}
.fbtn.active{background:var(--accent-solid);border-color:var(--accent-solid);color:#fff}
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
#tbl td{font-size:13px;color:#cbd5e1}
/* `#tbl th,#tbl td{text-align:left}` 特异性高于 `.num`，数字列要带 id 再声明一次
   才真正右对齐；th 的右内边距归零，让「文字 + 箭头」里的文字右缘正好落在
   数字右缘（td 有 12px 内边距，箭头 8px + 间隙 4px 抵掉它）。 */
#tbl .num{text-align:right}
#tbl th.num{padding-right:0}
#tbl td.name{font-size:13.5px;font-weight:500;color:var(--text)}
#tbl tbody tr:hover td{background:#1c2840}
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
     background:var(--accent-solid);color:#fff;font-size:9px;font-weight:700;
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
.url{max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
     font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11.5px;
     color:#64748b}
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
                            border-color:var(--accent-solid);color:#fff}
.pager{display:flex;align-items:center;gap:8px 10px;margin-left:auto;flex-wrap:wrap}
.psize{display:flex;align-items:center;gap:6px;font-size:11.5px;color:var(--text-3)}
.psize select{padding:4px 6px;border-radius:7px;border:1px solid var(--border);
              background:var(--card);color:var(--text);font:inherit;font-size:12px;
              cursor:pointer}
.psize select:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.pinfo{font-size:11.5px;color:var(--text-3);font-variant-numeric:tabular-nums}
.pnums,.pnumlist{display:flex;align-items:center;gap:4px}
.pbtn{min-width:26px;height:26px;padding:0 7px;border-radius:7px;
      border:1px solid var(--border);background:var(--card);color:#cbd5e1;
      font:inherit;font-size:12px;font-variant-numeric:tabular-nums;cursor:pointer;
      transition:background-color 120ms linear,border-color 120ms linear}
.pbtn:hover:not(:disabled){border-color:var(--border-2);color:var(--text)}
.pbtn[aria-current="page"]{background:var(--accent-solid);
                           border-color:var(--accent-solid);color:#fff}
.pbtn:disabled{opacity:.4;cursor:default}
.pbtn:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.pgap{padding:0 1px;font-size:12px;color:var(--text-4)}
/* ---------- 次级表格 ---------- */
table.sub{margin:0;width:100%}
table.sub th{padding:8px 12px;text-align:left;font-size:11.5px;font-weight:600;
             color:var(--text-3);letter-spacing:.05em;
             border-bottom:1px solid var(--border)}
table.sub td{padding:8px 12px;font-size:13px;color:#cbd5e1;
             border-bottom:1px solid var(--line)}
table.sub td.name{font-size:13.5px;font-weight:500;color:var(--text)}
table.sub tbody tr:hover td{background:#1c2840}
table.sub td.none{color:var(--text-3);font-size:12.5px}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;
      color:#cbd5e1}
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
let page = 1;
let size = 20;
let keys = [];                 // 组合排序：靠前的键优先，越靠后越是次级条件
let multiMode = false;         // 触屏没有 Shift 键，用开关把「叠加」变成显式模式
const multiBtn = document.getElementById('multi');

rows.forEach((tr, i) => { tr.dataset.i = i; });   // 稳定末位比较：还原服务端默认序

/* 排序值在构建期写到行上（dataset），运行时零文本解析；空串 = 无数据 */
const SORT_COLS = ['status', 'name', 'compat', 'sites', 'live', 'health',
                   'avail', 'l3'];
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
    const okText = !key || tr.textContent.toLowerCase().includes(key);
    return okKind && okText;
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

document.querySelectorAll('.fbtn').forEach(btn => {
  btn.addEventListener('click', () => {
    kind = btn.dataset.kind;
    document.querySelectorAll('.fbtn').forEach(b =>
      b.classList.toggle('active', b === btn));
    page = 1;
    render();
  });
});

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
        sort_attrs = (
            f' data-status="{_STATUS_RANK.get(r["status"], 9)}"'
            f' data-name="{esc(r.get("name") or "")}"'
            f' data-compat="{esc(r.get("compat") or "")}"'
            f' data-sites="{sv_sites}" data-live="{sv_live}"'
            f' data-health="{score:.2f}" data-avail="{sv_av}"'
            f' data-l3="{sv_l3}"')
        trs.append(
            f'<tr class="{cls}" data-kind="{kind}"{sort_attrs}>'
            f'<td><span class="chip {cls}">{esc(r["status"])}</span>{dup}</td>'
            f'<td class="name">{esc(r.get("name") or "")}</td>'
            f'<td class="c-compat">{esc(r.get("compat") or "—")}</td>'
            f'<td class="c-sites num">{r["sites_count"]}</td>'
            f'<td class="c-live num">{live_disp}</td>'
            f'<td class="c-health num">{score:g}'
            f'<i class="hbar {cls}" style="--w:{width}%"></i></td>'
            f'<td class="c-avail num avail{av_cls}">{esc(str(avail))}</td>'
            f'<td class="c-l3 num">{l3}</td>'
            f'<td class="c-spark spark">{_sparkline(marks)}</td>'
            f'<td class="c-url url" title="{esc(r["url"])}">'
            f'{esc(r["url"][:80])}</td>'
            f'</tr>')

    doc_total = sum(kind_count.values())
    live_n = kind_count.get("live", 0)
    vod_n = kind_count.get("vod", 0)
    lite_note = f" · 精简版 {lite_count} 个源" if lite_count else ""
    trend_block = _trend_panel(trend, doc_total, live_n)

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
                  f'<table class="sub"><tbody><tr><th>主机</th><th>失败次数</th>'
                  f'<th>涉及源</th></tr>{host_rows}</tbody></table>'
                  if host_rows else "")

    fail_rows = "".join(
        f"<tr><td>{esc(f['reason'])}</td><td class='num'>{f['count']}</td>"
        f"<td class='num'>{f['sources']}</td></tr>"
        for f in (failure_stats or [])[:10])
    fail_section = (f'<h2>近 30 天失败原因</h2>'
                    f'<table class="sub"><tbody><tr><th>原因</th><th>次数</th>'
                    f'<th>涉及源</th></tr>{fail_rows}</tbody></table>'
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
<h1>TVBoxKeeper · 源体检看板</h1>
<p class="meta">生成于 {now} · 订阅门槛：健康分 ≥ {min_health} · 订阅源 {duocang_count} 个{lite_note}</p>
<div class="cards">{cards}</div>
{chg_line}{trend_block}
<div class="tools">
<input id="q" type="search" placeholder="筛选名称 / 状态 / URL…" aria-label="按名称或链接筛选源">
<div class="filters" role="group" aria-label="按类型筛选">
<button type="button" class="fbtn active" data-kind="all">全部 <b>{doc_total}</b></button>
<button type="button" class="fbtn" data-kind="live">直播 <b>{live_n}</b></button>
<button type="button" class="fbtn" data-kind="vod">非直播 <b>{vod_n}</b></button>
</div>
<span class="cnt" id="cnt" role="status"></span>
</div>
<table id="tbl">
<caption class="sr-only">源榜单</caption>
<thead><tr>
{_th("status", "状态")}{_th("name", "名称", "s")}{_th("compat", "适配", "s", "c-compat")}
{_th("sites", "站点", "n", "c-sites num")}{_th("live", "直播", "n", "c-live num")}
{_th("health", "健康分", "n", "c-health num")}
{_th("avail", "30天可用", "n", "c-avail num")}{_th("l3", "L3", "n", "c-l3 num")}
<th class="c-spark">近期体检</th><th class="c-url">URL</th>
</tr></thead>
<tbody>
{''.join(trs)}
<tr id="empty" style="display:none"><td colspan="10">{empty_text}</td></tr>
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
