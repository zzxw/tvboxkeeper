"""TLS 策略：默认校验证书，仅证书链失败时按源降级复检。"""

import asyncio
import json

import httpx

from tvboxkeeper import cli as cli_mod
from tvboxkeeper.config import load_settings
from tvboxkeeper.fetch import is_cert_error
from tvboxkeeper.registry import Registry

SETTINGS = load_settings(None)
# 模块级抓一份原始类：monkeypatch 改的是 httpx.AsyncClient 本身（共享模块对象），
# 不预先保存会递归调回工厂自己
_REAL_CLIENT = httpx.AsyncClient
URL = "http://tls.example/c.json"
CERT_ERR = ("ConnectError: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify "
            "failed: unable to get local issuer certificate (_ssl.c:1010)")
HANDSHAKE_ERR = ("ConnectError: [SSL: SSLV3_ALERT_HANDSHAKE_FAILURE] sslv3 alert "
                 "handshake failure (_ssl.c:1010)")
BODY = b'{"sites": [{"key": "a", "api": "http://api.example/tv"}]}'


def test_is_cert_error_matches_only_verification_failures():
    assert is_cert_error(CERT_ERR)
    assert is_cert_error("self-signed certificate in certificate chain")
    assert is_cert_error("hostname 'a.com' doesn't match 'b.com'")
    # 握手失败/超时关掉校验也连不上，不算可降级
    assert not is_cert_error(HANDSHAKE_ERR)
    assert not is_cert_error("ConnectTimeout: timed out")
    assert not is_cert_error("")


class _Factory:
    """把 AsyncClient 换成按 verify 选择 transport 的真客户端，并记录调用。"""

    def __init__(self, handlers: dict[bool, object]):
        self.handlers = handlers
        self.calls: list[bool] = []

    def __call__(self, **kwargs):
        verify = kwargs.pop("verify", True)
        self.calls.append(verify)
        handler = self.handlers[bool(verify)]
        return _REAL_CLIENT(
            transport=httpx.MockTransport(handler), **kwargs)


def _run(tmp_path, monkeypatch, secure_handler, insecure_handler=None):
    reg = Registry(tmp_path / "data" / "tvbox.sqlite",
                   tmp_path / "data" / "snapshots")
    reg.upsert(URL)
    factory = _Factory({True: secure_handler,
                        False: insecure_handler or secure_handler})
    monkeypatch.setattr(cli_mod.httpx, "AsyncClient", factory)
    outcomes = asyncio.run(
        cli_mod._run_checks(reg, SETTINGS, [{"url": URL}]))
    return reg, outcomes, factory.calls


def test_cert_failure_retries_insecure_and_flags(tmp_path, monkeypatch):
    def denied(request):
        raise httpx.ConnectError(CERT_ERR)

    def allowed(request):
        return httpx.Response(200, content=BODY)

    reg, outcomes, calls = _run(tmp_path, monkeypatch, denied, allowed)

    assert calls == [True, False], "应先校验抓取，失败后仅降级一次"
    out = outcomes[0]
    assert out.ok and out.tls_insecure
    detail = json.loads(reg.conn.execute(
        "SELECT detail FROM check_log ORDER BY rowid DESC LIMIT 1"
    ).fetchone()["detail"])
    assert detail["tls"] == 1
    assert reg.get_by_url(URL)["status"] == "active"


def test_handshake_failure_does_not_downgrade(tmp_path, monkeypatch):
    def denied(request):
        raise httpx.ConnectError(HANDSHAKE_ERR)

    _, outcomes, calls = _run(tmp_path, monkeypatch, denied)
    assert calls == [True], "握手失败不应触发不校验重试"
    assert not outcomes[0].ok and not outcomes[0].tls_insecure


def test_verify_ssl_false_skips_secure_pass(tmp_path, monkeypatch):
    settings = load_settings(None)
    settings["network"]["verify_ssl"] = False
    reg = Registry(tmp_path / "data" / "tvbox.sqlite",
                   tmp_path / "data" / "snapshots")
    reg.upsert(URL)
    factory = _Factory({False: lambda r: httpx.Response(200, content=BODY)})
    monkeypatch.setattr(cli_mod.httpx, "AsyncClient", factory)
    outcomes = asyncio.run(
        cli_mod._run_checks(reg, settings, [{"url": URL}]))
    assert factory.calls == [False] and outcomes[0].ok
