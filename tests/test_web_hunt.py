import httpx

from tvboxkeeper.config import load_settings
from tvboxkeeper.registry import Registry
from tvboxkeeper.web_hunt import WebHunter

PAGE = "http://page.example.com/list"
BODY = """
<a href="http://a.example.com/cfg.json">a</a>
http://b.example.com/tv.txt
http://c.example.com:1935:1935/bad.json
http://a.example.com/cfg.json
"""


def make_registry(tmp_path) -> Registry:
    return Registry(tmp_path / "t.sqlite", tmp_path / "snapshots")


def make_settings(**web) -> dict:
    settings = load_settings(None)
    settings["web"].update({"enabled": True, "pages": [PAGE], **web})
    return settings


def make_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_webhunt_registers_and_dedupes(tmp_path):
    reg = make_registry(tmp_path)
    with make_client(lambda request: httpx.Response(200, text=BODY)) as client:
        stats = WebHunter(client, reg, make_settings()).run()
    assert stats == {"pages": 1, "found": 2, "registered": 2, "failed": 0}
    urls = {r["url"] for r in reg.sources()}
    assert urls == {"http://a.example.com/cfg.json",
                    "http://b.example.com/tv.txt"}
    # 非法端口的地址在登记前就被拦掉
    assert not any("1935:1935" in u for u in urls)


def test_webhunt_disabled_makes_no_requests(tmp_path):
    reg = make_registry(tmp_path)
    called = []

    def handler(request):
        called.append(request)
        return httpx.Response(200, text=BODY)

    with make_client(handler) as client:
        stats = WebHunter(client, reg, make_settings(enabled=False)).run()
    assert stats["pages"] == 0 and stats["registered"] == 0
    assert called == []


def test_webhunt_page_failure_does_not_abort(tmp_path):
    reg = make_registry(tmp_path)

    def handler(request):
        return httpx.Response(500, text="boom")

    with make_client(handler) as client:
        stats = WebHunter(client, reg, make_settings()).run()
    assert stats["failed"] == 1 and stats["registered"] == 0


def test_webhunt_respects_max_new_per_run(tmp_path):
    reg = make_registry(tmp_path)
    with make_client(lambda request: httpx.Response(200, text=BODY)) as client:
        stats = WebHunter(client, reg, make_settings(max_new_per_run=1)).run()
    assert stats["registered"] == 1


def test_webhunt_follows_same_domain_link(tmp_path):
    """depth=1：只跟进同域链接一层，链接页里的配置地址能登记。"""
    reg = make_registry(tmp_path)
    home = "http://page.example.com/home"
    settings = make_settings(pages=[home], depth=1)

    def handler(request):
        url = str(request.url)
        if url == home:
            return httpx.Response(200, text='<a href="/list">more</a>')
        if url.endswith("/list"):
            return httpx.Response(200, text="http://deep.example.com/x.json")
        return httpx.Response(404)

    with make_client(handler) as client:
        stats = WebHunter(client, reg, settings).run()
    assert stats["pages"] == 2 and stats["registered"] == 1
    assert {r["url"] for r in reg.sources()} == {"http://deep.example.com/x.json"}


def test_webhunt_does_not_follow_cross_domain(tmp_path):
    reg = make_registry(tmp_path)
    home = "http://page.example.com/home"
    settings = make_settings(pages=[home], depth=1)

    def handler(request):
        return httpx.Response(
            200, text='<a href="http://other.example.com/list">x</a>')

    with make_client(handler) as client:
        stats = WebHunter(client, reg, settings).run()
    assert stats["pages"] == 1


def test_webhunt_expands_sitemap(tmp_path):
    """sitemap 的 <loc> 作为待扫页面展开，页面内的配置地址正常登记。"""
    reg = make_registry(tmp_path)
    sm = "http://page.example.com/sitemap.xml"
    settings = make_settings(pages=[sm], depth=0)

    def handler(request):
        url = str(request.url)
        if url == sm:
            return httpx.Response(200, text=(
                '<?xml version="1.0"?><urlset><url><loc>'
                'http://page.example.com/p1</loc></url></urlset>'))
        if url.endswith("/p1"):
            return httpx.Response(200, text="http://a.example.com/cfg.json")
        return httpx.Response(404)

    with make_client(handler) as client:
        stats = WebHunter(client, reg, settings).run()
    assert stats["pages"] == 2 and stats["registered"] == 1
