from tvboxkeeper.normalize import guess_name, normalize_url, source_id


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
