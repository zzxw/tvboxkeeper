from tvboxkeeper.normalize import (guess_name, is_fetchable_url, normalize_url,
                                   source_id)


def test_normalize_strips_utm_and_fragment():
    assert (normalize_url("http://example.com/a.json?utm_source=x&id=1#frag")
            == "http://example.com/a.json?id=1")


def test_normalize_strips_whitespace():
    assert normalize_url("  https://a.b/c.json \n") == "https://a.b/c.json"


def test_source_id_stable():
    assert (source_id("http://example.com/a.json?utm_source=x")
            == source_id("http://example.com/a.json"))


def test_guess_name():
    assert guess_name("https://www.example.com:8080/x.json") == "example.com"


def test_is_fetchable_url_rejects_bad_authority():
    assert is_fetchable_url("http://h.example:8080/a.json")
    assert is_fetchable_url("http://[::1]:8080/a.json")     # IPv6 带方括号
    assert not is_fetchable_url("http://h.example:1935:1935/a.json")  # 重复端口
    assert not is_fetchable_url("http://h.example:abc/a.json")        # 非数字端口
    assert not is_fetchable_url("ftp://h.example/a.json")
    assert not is_fetchable_url("")
