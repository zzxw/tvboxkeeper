"""CLI 入口。在仓库根目录运行（数据/产物路径相对于当前目录）。"""

from __future__ import annotations

import asyncio
import os
import random
import time
from pathlib import Path

import httpx
import typer

from . import discover as discover_mod
from . import publish as publish_mod
from . import validate as validate_mod
from .config import load_seeds, load_settings
from .fetch import make_headers
from .normalize import normalize_url
from .registry import Registry

app = typer.Typer(
    help="TVBox 源维护工具：发现、体检、评分、发布",
    no_args_is_help=True,
    add_completion=False,
)


def _root() -> Path:
    return Path(os.environ.get("TVBOX_ROOT", Path.cwd()))


def _open() -> tuple[Path, dict, Registry]:
    root = _root()
    settings = load_settings(root / "config" / "settings.yaml")
    reg = Registry(root / "data" / "tvbox.sqlite", root / "data" / "snapshots")
    return root, settings, reg


def _fmt(name: str, width: int) -> str:
    """中文名按显示宽度对齐（中文算 2 列）。"""
    visible = sum(2 if ord(ch) > 127 else 1 for ch in name)
    return name + " " * max(1, width - visible) if visible < width else name


@app.command()
def init() -> None:
    """初始化 data/ output/ 目录与数据库。"""
    root, _, reg = _open()
    (root / "output").mkdir(parents=True, exist_ok=True)
    reg.close()
    typer.echo("已初始化：data/tvbox.sqlite、data/snapshots/、output/")


@app.command(name="import")
def import_seeds(
    file: Path = typer.Option(None, "--file", help="种子文件，默认 config/sources.yaml"),
) -> None:
    """把种子清单登记进源库。"""
    root, _, reg = _open()
    seeds = load_seeds(file or root / "config" / "sources.yaml")
    created = 0
    for seed in seeds:
        _, is_new = reg.upsert(
            seed["url"],
            name=seed.get("name", ""),
            origin="manual",
            starred=seed.get("starred"),
            banned=seed.get("banned"),
        )
        created += int(is_new)
    typer.echo(f"种子源 {len(seeds)} 个，其中新建 {created} 个。")
    reg.close()


@app.command()
def add(
    url: str,
    name: str = typer.Option("", "--name"),
    starred: bool = typer.Option(False, "--starred", help="置顶推荐"),
) -> None:
    """手工添加一个源。"""
    _, _, reg = _open()
    sid, created = reg.upsert(url, name=name, origin="manual", starred=starred or None)
    typer.echo(f"{'新增' if created else '已存在'}：{sid} {normalize_url(url)}")
    reg.close()


@app.command()
def star(
    url_or_id: str,
    off: bool = typer.Option(False, "--off"),
) -> None:
    """把源标记/取消为置顶推荐。"""
    _, _, reg = _open()
    row = reg.get(url_or_id) or reg.get_by_url(url_or_id)
    if not row:
        typer.echo("找不到该源。")
        raise typer.Exit(1)
    reg.set_flag(row["id"], "starred", 0 if off else 1)
    typer.echo(f"{'已取消' if off else '已置顶'}：{row['name']}")


@app.command()
def ban(
    url_or_id: str,
    off: bool = typer.Option(False, "--off"),
) -> None:
    """封禁/解封一个源（封禁后不参与体检与发布）。"""
    _, _, reg = _open()
    row = reg.get(url_or_id) or reg.get_by_url(url_or_id)
    if not row:
        typer.echo("找不到该源。")
        raise typer.Exit(1)
    if off:
        reg.set_flag(row["id"], "banned", 0)
        reg.set_flag(row["id"], "notes", row.get("notes") or "")
        reg.conn.execute("UPDATE source SET status='new' WHERE id=?", (row["id"],))
        reg.conn.commit()
        typer.echo(f"已解封：{row['name']}")
    else:
        reg.set_flag(row["id"], "banned", 1)
        typer.echo(f"已封禁：{row['name']}")
    reg.close()


async def _run_checks(reg: Registry, settings: dict, rows: list[dict]) -> list:
    net = settings["network"]
    headers = make_headers(random.choice(net["user_agents"]))
    verify = net.get("verify_ssl", True)
    async with httpx.AsyncClient(
        timeout=net["timeout"], follow_redirects=True, headers=headers,
        verify=verify,
    ) as client:
        sem = asyncio.Semaphore(net["concurrency"])
        outcomes = await asyncio.gather(*[
            validate_mod.check_source(client, sem, r["url"], settings)
            for r in rows
        ])
    for outcome in outcomes:
        reg.record(outcome, settings)
    return list(outcomes)


@app.command()
def check(
    level: str = typer.Option("", "--level", help="覆盖验证档位：L1 / L2"),
    limit: int = typer.Option(0, "--limit", help="最多检查 N 个（0=全部）"),
    only: str = typer.Option("", "--only", help="只检查包含该关键字的 URL"),
) -> None:
    """全量体检源库。"""
    _, settings, reg = _open()
    if level:
        settings["validate"]["level"] = level.upper()
    rows = reg.sources()
    if only:
        rows = [r for r in rows if only in r["url"]]
    if limit:
        rows = rows[:limit]
    if not rows:
        typer.echo("源库为空，先运行 `tvboxkeeper import` 或 `tvboxkeeper add`。")
        reg.close()
        raise typer.Exit()
    typer.echo(f"开始体检 {len(rows)} 个源（档位 {settings['validate']['level']}）...")
    start = time.monotonic()
    outcomes = asyncio.run(_run_checks(reg, settings, rows))
    by = {r["url"]: r for r in rows}
    ok_n = 0
    for o in outcomes:
        row = by.get(o.url, {})
        info = o.schema
        subs = f"{sum(1 for p in o.probes if p.ok)}/{len(o.probes)}" if o.probes else "-"
        mark = "OK  " if o.ok else "FAIL"
        ok_n += int(o.ok)
        typer.echo(
            f"[{mark}] {_fmt(row.get('name', ''), 16)} "
            f"{o.url[:60]:60s} {o.latency_ms:>5}ms "
            f"schema={getattr(info, 'schema_type', '-'):<10} "
            f"sites={getattr(info, 'sites_count', 0):<4} "
            f"subs={subs} {('err=' + o.error) if o.error else ''}"
        )
    typer.echo(
        f"\n完成：{ok_n}/{len(outcomes)} 个源可用，"
        f"耗时 {time.monotonic() - start:.1f}s。"
        "运行 `tvboxkeeper publish` 生成产物。"
    )
    reg.close()


@app.command()
def discover() -> None:
    """展开库中多仓（storeHouse）的子仓并登记。"""
    _, settings, reg = _open()
    net = settings["network"]
    headers = make_headers(random.choice(net["user_agents"]))

    async def _run() -> int:
        async with httpx.AsyncClient(
            timeout=net["timeout"], follow_redirects=True, headers=headers,
            verify=net.get("verify_ssl", True),
        ) as client:
            sem = asyncio.Semaphore(net["concurrency"])
            return await discover_mod.expand(reg, client, sem, settings)

    created = asyncio.run(_run())
    typer.echo(f"discover 完成：新登记 {created} 个子仓源（{discover_mod.HINT}）。")
    reg.close()


@app.command()
def publish() -> None:
    """生成多仓订阅 JSON 与 Markdown 体检报告。"""
    root, settings, reg = _open()
    rows = reg.sources(include_banned=True)
    pub = settings["publish"]
    duo_path = root / pub["duocang"]
    prev = publish_mod.previous_urls(duo_path)
    data = publish_mod.build_duocang(rows, pub["min_health"])
    publish_mod.write_duocang(duo_path, data)
    cur = {e["sourceUrl"] for e in data["storeHouse"]}
    added, removed = cur - prev, prev - cur
    report = publish_mod.build_report(
        rows, added, removed, pub["duocang"], len(data["storeHouse"]),
        pub["min_health"],
    )
    report_path = root / pub["report"]
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")
    typer.echo(
        f"已发布：{pub['duocang']}（{len(data['storeHouse'])} 个源）、"
        f"{pub['report']}（diff: +{len(added)} / -{len(removed)}）"
    )
    reg.close()


@app.command(name="list")
def list_cmd(
    status: str = typer.Option("", "--status", help="按状态过滤"),
    all_: bool = typer.Option(False, "--all", help="包含 banned"),
) -> None:
    """查看源库。"""
    _, _, reg = _open()
    rows = reg.sources(status=status, include_banned=all_)
    typer.echo(f"{'id':16s} {'状态':<10} {'健康':>5}  {'名称':<20} URL")
    for r in rows:
        typer.echo(
            f"{r['id']:16s} {r['status']:<10} {r['health_score']:>5.1f}  "
            f"{_fmt(r['name'], 20)} {r['url'][:70]}"
        )
    typer.echo(f"\n共 {len(rows)} 个源。")
    reg.close()


if __name__ == "__main__":
    app()
