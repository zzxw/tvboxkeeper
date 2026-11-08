"""发布：storeHouse 多仓 JSON + Markdown 体检榜单 + 静态网页。"""

from __future__ import annotations

import html
import json
import time
from collections import Counter
from pathlib import Path

from .normalize import guess_name


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
) -> dict:
    """生成 storeHouse 多仓；有镜像的源用镜像地址替代原始 URL。"""
    entries = []
    for r in pick_sources(rows, min_health):
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
) -> dict:
    """生成直播合集配置：lives 数组收录健康直播源，可直接作为配置地址导入。"""
    entries = []
    for r in pick_live_sources(rows, min_health):
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
    ]
    if mirror_note:
        lines.append(f"- {mirror_note}")
    if live_info:
        lines.append(f"- {live_info}")
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


def build_site(
    rows: list[dict],
    histories: dict[str, list[dict]],
    failure_stats: list[dict] | None,
    duocang_count: int,
    min_health: float,
) -> str:
    """生成自包含的静态目录页（无外部依赖，可直接挂 GitHub Pages）。"""
    now = time.strftime("%Y-%m-%d %H:%M")
    esc = html.escape
    by_status = Counter(r["status"] for r in rows)
    cards = "".join(
        f'<div class="card {cls}"><b>{n}</b><span>{label}</span></div>'
        for label, n, cls in [
            ("总数", len(rows), "off"),
            ("active", by_status.get("active", 0), "ok"),
            ("anti_crawl", by_status.get("anti_crawl", 0), "warn"),
            ("flaky", by_status.get("flaky", 0), "warn"),
            ("invalid", by_status.get("invalid", 0), "bad"),
            ("dead", by_status.get("dead", 0), "bad"),
        ])

    trs = []
    for r in sorted((x for x in rows if not x.get("banned")),
                    key=lambda x: (-x.get("starred", 0), -x.get("health_score", 0))):
        marks = [h.get("ok", 0) for h in histories.get(r["id"], [])]
        cls = _STATUS_CLASS.get(r["status"], "off")
        l3 = f"{r['l3_ok']}/{r['l3_tested']}" if r.get("l3_tested") else "—"
        avail = r.get("avail") or "—"
        live_disp = (r["live_channels"] if r.get("schema_type") == "live"
                     else r["lives_count"])
        dup = (f' <span class="dup">归并#{esc(str(r["dup_of"])[:8])}</span>'
               if r.get("dup_of") else "")
        trs.append(
            f'<tr class="{cls}">'
            f'<td><span class="chip {cls}">{esc(r["status"])}</span>{dup}</td>'
            f'<td class="name">{esc(r.get("name") or "")}</td>'
            f'<td>{esc(r.get("compat") or "—")}</td>'
            f'<td class="num">{r["sites_count"]}</td>'
            f'<td class="num">{live_disp}</td>'
            f'<td class="num">{r["health_score"]}</td>'
            f'<td class="num">{avail}</td>'
            f'<td class="num">{l3}</td>'
            f'<td class="spark">{_sparkline(marks)}</td>'
            f'<td class="url" title="{esc(r["url"])}">{esc(r["url"][:80])}</td>'
            f'</tr>')

    fail_rows = "".join(
        f"<tr><td>{esc(f['reason'])}</td><td class='num'>{f['count']}</td>"
        f"<td class='num'>{f['sources']}</td></tr>"
        for f in (failure_stats or [])[:10])
    fail_section = (f'<h2>近 30 天失败原因</h2><table id="fails"><tr>'
                    f'<th>原因</th><th>次数</th><th>涉及源</th></tr>{fail_rows}</table>'
                    if fail_rows else "")

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TVBoxKeeper · 源体检看板</title>
<style>
:root {{ color-scheme: dark; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; padding: 24px; font: 14px/1.6 system-ui, "Segoe UI", "Microsoft YaHei", sans-serif;
       background: #0f172a; color: #e2e8f0; }}
h1 {{ font-size: 20px; margin: 0 0 4px; }}
.meta {{ color: #64748b; margin-bottom: 16px; }}
.cards {{ display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 20px; }}
.card {{ background: #1e293b; border-radius: 10px; padding: 10px 18px; text-align: center; }}
.card b {{ display: block; font-size: 22px; }}
.card span {{ color: #94a3b8; font-size: 12px; }}
.card.ok b {{ color: #4ade80; }} .card.warn b {{ color: #fbbf24; }}
.card.bad b {{ color: #f87171; }} .card.off b {{ color: #94a3b8; }}
input {{ background: #1e293b; border: 1px solid #334155; color: #e2e8f0;
         border-radius: 8px; padding: 8px 12px; width: 280px; margin-bottom: 12px; }}
table {{ border-collapse: collapse; width: 100%; margin-bottom: 24px; }}
th, td {{ padding: 6px 10px; border-bottom: 1px solid #1e293b; text-align: left; }}
th {{ color: #94a3b8; font-weight: 500; font-size: 12px; }}
tr:hover {{ background: #1e293b; }}
.chip {{ display: inline-block; padding: 1px 8px; border-radius: 99px; font-size: 12px; }}
.chip.ok {{ background: #14532d; color: #4ade80; }}
.chip.warn {{ background: #451a03; color: #fbbf24; }}
.chip.bad {{ background: #450a0a; color: #f87171; }}
.chip.off {{ background: #1e293b; color: #94a3b8; }}
.dup {{ color: #64748b; font-size: 11px; }}
.num {{ font-variant-numeric: tabular-nums; }}
.url {{ color: #64748b; font-family: ui-monospace, monospace; font-size: 12px;
        max-width: 320px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.spark svg {{ display: block; }}
footer {{ color: #475569; font-size: 12px; }}
</style></head><body>
<h1>TVBoxKeeper · 源体检看板</h1>
<div class="meta">生成于 {now} · 订阅门槛：健康分 ≥ {min_health} · 订阅源 {duocang_count} 个</div>
<div class="cards">{cards}</div>
<input id="q" placeholder="筛选名称 / 状态 / URL…">
<table id="tbl">
<tr><th>状态</th><th>名称</th><th>适配</th><th>站点</th><th>直播</th><th>健康分</th>
<th>30天可用</th><th>L3</th><th>近期体检</th><th>URL</th></tr>
{''.join(trs)}
</table>
{fail_section}
<footer>TVBoxKeeper 自动生成 · 内容仅为配置可达性元数据，不含任何音视频</footer>
<script>
document.getElementById('q').addEventListener('input', e => {{
  const q = e.target.value.toLowerCase();
  document.querySelectorAll('#tbl tr').forEach((tr, i) => {{
    if (!i) return;
    tr.style.display = tr.textContent.toLowerCase().includes(q) ? '' : 'none';
  }});
}});
</script>
</body></html>"""
