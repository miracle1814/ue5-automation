# -*- coding: utf-8 -*-
"""v6.5 stdio 代理：零第三方 HTTP 依赖（urllib）+ 错误映射。"""
import os
import sys
import urllib.error
import urllib.request

os.environ["UE5_BRIDGE_TEST_MODE"] = "1"
os.environ["UE5_BRIDGE_RECONNECT_S"] = "0"
_SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPTS = os.path.join(_SKILL_ROOT, "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import ue5_mcp_stdio as S  # noqa: E402


class _FakeResp:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_http_resp_json_and_text():
    r = S._HttpResp(200, b'{"ok": true}')
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert r.text == '{"ok": true}'


def test_post_uses_urllib_and_returns_status(monkeypatch):
    old = urllib.request.urlopen

    def fake_urlopen(req, timeout=None):
        assert req.method == "POST"
        return _FakeResp(200, b'{"jsonrpc":"2.0","id":1,"result":{}}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    r = S._post({"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code == 200 and r.json()["result"] == {}


def test_offline_maps_to_minus_32000(monkeypatch):
    monkeypatch.setattr(S, "RECONNECT_S", 0)
    monkeypatch.setattr(S, "_post", lambda payload: (_ for _ in ()).throw(
        urllib.error.URLError(ConnectionRefusedError("refused"))))
    r = S._relay({"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r["error"]["code"] == -32000


def test_timeout_maps_to_minus_32001(monkeypatch):
    monkeypatch.setattr(S, "_post", lambda payload: (_ for _ in ()).throw(
        TimeoutError("slow")))
    r = S._relay({"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r["error"]["code"] == -32001


def test_403_maps_to_minus_32002(monkeypatch):
    monkeypatch.setattr(S, "_post", lambda payload: S._HttpResp(
        403, b'{"detail":"Unauthorized"}'))
    r = S._relay({"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r["error"]["code"] == -32002
