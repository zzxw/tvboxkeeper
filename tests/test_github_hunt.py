import httpx

from tvboxkeeper.config import load_settings
from tvboxkeeper.github_hunt import Hunter
from tvboxkeeper.registry import Registry

SETTINGS = load_settings(None)
SETTINGS["github"]["search_interval"] = 0

REPO_SEARCH = {"items": [{
    "full_name": "o/r1", "default_branch": "main",
    "html_url": "https://github.com/o/r1", "stargazers_count": 5,
}]}

CONFIG_URL = "https://raw.githubusercontent.com/o/r1/main/config.json"


def make_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def handler(request) -> httpx.Response:
    url = str(request.url)
    if "api.github.com/search/repositories" in url:
        return httpx.Response(200, json=REPO_SEARCH)
    if url == CONFIG_URL:
        return httpx.Response(200, json={"sites": [{"key": "a", "type": 0}]})
    return httpx.Response(404)


def test_hunt_registers_valid_configs(tmp_path):
    reg = Registry(tmp_path / "t.sqlite")
    hunter = Hunter(make_client(handler), reg, SETTINGS)
    stats = hunter.hunt()
    assert stats["repos"] == 1
    assert stats["registered"] == 1
    assert reg.get_by_url(CONFIG_URL) is not None


def test_hunt_skips_recent_hunted_repos(tmp_path):
    reg = Registry(tmp_path / "t.sqlite")
    Hunter(make_client(handler), reg, SETTINGS).hunt()
    second = Hunter(make_client(handler), reg, SETTINGS).hunt()
    assert second["repos"] == 0  # rehunt_days 内不重复探测


def test_hunt_ignores_invalid_files(tmp_path):
    reg = Registry(tmp_path / "t.sqlite")

    def bad_handler(request) -> httpx.Response:
        url = str(request.url)
        if "search/repositories" in url:
            return httpx.Response(200, json=REPO_SEARCH)
        if url == CONFIG_URL:
            return httpx.Response(200, text="<html>反爬页</html>")
        return httpx.Response(404)

    stats = Hunter(make_client(bad_handler), reg, SETTINGS).hunt()
    assert stats["registered"] == 0
    assert reg.get_by_url(CONFIG_URL) is None


def test_hunt_rate_limited_search(tmp_path):
    reg = Registry(tmp_path / "t.sqlite")

    def limited_handler(request) -> httpx.Response:
        return httpx.Response(403, json={"message": "rate limit"})

    stats = Hunter(make_client(limited_handler), reg, SETTINGS).hunt()
    assert stats["repos"] == 0
