"""发现：多仓父源应优先复用快照，避免重复下载。"""

import asyncio
import json

from tvboxkeeper import discover
from tvboxkeeper.config import load_settings
from tvboxkeeper.fetch import FetchResult

PARENT_URL = "http://p.example/list.json"
CONFIG = json.dumps({"storeHouse": [
    {"sourceName": "子1", "sourceUrl": "http://c1.example/a.json"},
    {"sourceName": "子2", "sourceUrl": "http://c2.example/b.json"},
]}, ensure_ascii=False).encode()


class FakeRegistry:
    def __init__(self, rows, snapshot=None):
        self._rows = rows
        self._snapshot = snapshot
        self.created: list[dict] = []

    def sources(self):
        return self._rows

    def latest_snapshot(self, sid):
        return self._snapshot

    def upsert(self, url, name="", gen=0, origin="", ref=""):
        self.created.append({"url": url, "name": name, "gen": gen,
                             "origin": origin, "ref": ref})
        return "id", True


def parent_row():
    return {"id": "p1", "url": PARENT_URL, "name": "父仓", "gen": 0,
            "schema_type": "storehouse", "status": "active"}


def test_expand_reuses_snapshot_without_fetch(monkeypatch):
    calls: list[str] = []

    async def boom(*args, **kwargs):
        calls.append("fetch")
        raise AssertionError("有快照时不应再联网抓父源")

    monkeypatch.setattr(discover, "fetch", boom)
    reg = FakeRegistry([parent_row()], snapshot=(CONFIG, "2026-11-09 00:00:00"))

    created, live = asyncio.run(
        discover.expand(reg, None, None, load_settings(None)))

    assert calls == []
    assert (created, live) == (2, 0)
    assert [c["origin"] for c in reg.created] == ["storehouse", "storehouse"]
    assert all(c["gen"] == 1 for c in reg.created)


def test_expand_fetches_when_snapshot_missing(monkeypatch):
    async def fake_fetch(*args, **kwargs):
        return FetchResult(url=PARENT_URL, status=200, content=CONFIG)

    monkeypatch.setattr(discover, "fetch", fake_fetch)
    reg = FakeRegistry([parent_row()], snapshot=None)

    created, _ = asyncio.run(
        discover.expand(reg, None, None, load_settings(None)))
    assert created == 2


def test_expand_skips_dead_parent(monkeypatch):
    async def fake_fetch(*args, **kwargs):
        return FetchResult(url=PARENT_URL, status=404, error="HTTP 404")

    monkeypatch.setattr(discover, "fetch", fake_fetch)
    reg = FakeRegistry([parent_row()], snapshot=None)

    created, live = asyncio.run(
        discover.expand(reg, None, None, load_settings(None)))
    assert (created, live) == (0, 0) and reg.created == []
