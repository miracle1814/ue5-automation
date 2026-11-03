# -*- coding: utf-8 -*-
"""Skill 自检脚本：health 归一化与重试/错误结构回归。"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from bridge_client import BridgeClient, BridgeError, normalize_health  # noqa: E402


def test_normalize_health_nested_ue_info():
    h = normalize_health({
        "status": "ok",
        "bridge_version": "0.5.0",
        "ue_info": {
            "bridge_loaded": True,
            "engine_version": "5.1.1",
            "project_name": "TP_BlankBP",
            "project_dir": "C:/P/",
        },
    })
    assert h["status"] == "ok"
    assert h["bridge_loaded"] is True
    assert h["bridge_version"] == "0.5.0"
    assert h["engine_version"] == "5.1.1"
    assert h["project_name"] == "TP_BlankBP"
    assert h["project_dir"] == "C:/P/"


def test_normalize_health_top_level_command_result():
    h = normalize_health({
        "engine_version": "5.1.1",
        "project_name": "TP_BlankBP",
        "project_dir": "C:/P/",
        "bridge_loaded": True,
    })
    assert h["status"] == "ok"
    assert h["bridge_loaded"] is True
    assert h["engine_version"] == "5.1.1"


def test_normalize_health_accepts_full_command_envelope():
    h = normalize_health({
        "success": True,
        "result": {
            "engine_version": "5.1.1",
            "bridge_loaded": True,
        },
    })
    assert h["bridge_loaded"] is True
    assert h["engine_version"] == "5.1.1"


def test_normalize_health_missing_fields_are_falsey():
    h = normalize_health({})
    assert h["status"] == ""
    assert h["bridge_loaded"] is False
    assert h["engine_version"] == ""


class _FlakyHandler(BaseHTTPRequestHandler):
    count = 0

    def do_GET(self):
        type(self).count += 1
        if type(self).count <= 2:
            self.send_response(500)
            self.end_headers()
            return
        body = json.dumps({
            "status": "ok",
            "bridge_version": "0.5.0",
            "ue_info": {"bridge_loaded": True, "engine_version": "5.1.1"},
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class _AuthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(401)
        self.end_headers()

    def log_message(self, *args):
        pass


def _serve(handler):
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def test_client_retries_http_500_then_succeeds():
    _FlakyHandler.count = 0
    server = _serve(_FlakyHandler)
    try:
        client = BridgeClient(port=server.server_address[1], token="test",
                              retries=2, backoff=0.01, timeout=2.0)
        h = client.health()
        assert h["bridge_loaded"] is True
        assert _FlakyHandler.count == 3
    finally:
        server.shutdown()
        server.server_close()


def test_client_auth_error_is_structured_and_not_retried():
    server = _serve(_AuthHandler)
    try:
        client = BridgeClient(port=server.server_address[1], token="bad",
                              retries=3, backoff=0.01, timeout=2.0)
        with pytest.raises(BridgeError) as exc:
            client.health()
        err = exc.value
        assert err.code == "auth_error"
        assert err.http_status == 401
        assert err.hint
    finally:
        server.shutdown()
        server.server_close()
