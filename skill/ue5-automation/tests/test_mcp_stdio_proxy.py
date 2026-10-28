# -*- coding: utf-8 -*-
"""ue5_mcp_stdio 离线单测：stdio 代理的握手 / 工具目录 / 离线转发错误路径。

覆盖（Glama 目录自检的同款判据）：
  · initialize     → protocolVersion + serverInfo
  · tools/list     → 静态目录（与 ue5_bridge._mcp_tool_defs 单一来源一致，≥23 工具）
  · tools/call     → UE 离线（指向不可达端口）→ JSON-RPC error，绝不假成功
"""
import json
import os
import subprocess
import sys

import pytest

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "scripts")
PROXY = os.path.join(SCRIPTS, "ue5_mcp_stdio.py")


def _spawn(env_extra):
    env = dict(os.environ)
    env.update(env_extra)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.Popen(
        [sys.executable, PROXY], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env=env, text=True, encoding="utf-8", bufsize=1)


def _roundtrip(proc, msgs, expect_n):
    for m in msgs:
        proc.stdin.write(json.dumps(m) + "\n")
    proc.stdin.flush()
    out = []
    while len(out) < expect_n:
        line = proc.stdout.readline()
        assert line.strip(), "代理进程提前退出（stdout 无响应）"
        out.append(json.loads(line))
    return out


def test_initialize_and_tools_list_offline():
    proc = _spawn({"UE5_BRIDGE_URL": "http://127.0.0.1:1/mcp",
                   "UE5_BRIDGE_RECONNECT_S": "0"})
    try:
        init, tools = _roundtrip(proc, [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18",
                        "capabilities": {}, "clientInfo": {"name": "t"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ], expect_n=2)

        r = init["result"]
        assert r["protocolVersion"] == "2025-06-18"
        assert r["serverInfo"]["name"] == "ue5-automation"
        assert "tools" in r["capabilities"]

        catalog = tools["result"]["tools"]
        names = {t["name"] for t in catalog}
        assert len(catalog) >= 23
        assert {"ue5_check_health", "ue5_build_blueprint", "ue5_start_pie",
                "ue5_execute_command"} <= names
        assert all("inputSchema" in t for t in catalog)
    finally:
        proc.kill()


def test_tools_call_offline_returns_clean_error():
    """UE 离线（不可达端口）→ 可读 JSON-RPC 错误，绝不假成功。"""
    proc = _spawn({"UE5_BRIDGE_URL": "http://127.0.0.1:1/mcp",
                   "UE5_BRIDGE_TOKEN": "guard-token",
                   "UE5_BRIDGE_RECONNECT_S": "0"})
    try:
        (resp,) = _roundtrip(proc, [
            {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
             "params": {"name": "ue5_check_health", "arguments": {}}},
        ], expect_n=1)
        assert resp["id"] == 7
        assert resp["error"]["code"] == -32000
        assert "离线" in resp["error"]["message"] or "offline" in resp["error"]["message"].lower()
    finally:
        proc.kill()


def test_ping_and_unknown_method():
    proc = _spawn({"UE5_BRIDGE_URL": "http://127.0.0.1:1/mcp",
                   "UE5_BRIDGE_RECONNECT_S": "0"})
    try:
        ping, unknown, res = _roundtrip(proc, [
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            {"jsonrpc": "2.0", "id": 2, "method": "no/such/method"},
            {"jsonrpc": "2.0", "id": 3, "method": "resources/list"},
        ], expect_n=3)
        assert ping["result"] == {}
        assert unknown["error"]["code"] != 0
        assert res["result"]["resources"] == []
    finally:
        proc.kill()


def test_reconnects_after_editor_restart():
    """v4.9 会话保活：桥启动前发请求 → 代理等待并重试，编辑器重启后自动恢复。"""
    import socket
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, obj):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            self._send(200, {"status": "ok"})

        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
            self._send(200, {"jsonrpc": "2.0", "id": req.get("id"),
                             "result": {"ok": True, "recovered": True}})

        def log_message(self, *a):
            pass

    proc = _spawn({"UE5_BRIDGE_URL": "http://127.0.0.1:%d/mcp" % port,
                   "UE5_BRIDGE_RECONNECT_S": "8"})
    httpd = {}

    def start_server():
        time.sleep(1.2)  # 模拟编辑器重启窗口：请求先到、桥后起
        httpd["s"] = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        httpd["s"].serve_forever()

    th = threading.Thread(target=start_server, daemon=True)
    th.start()
    try:
        (resp,) = _roundtrip(proc, [
            {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
             "params": {"name": "ue5_check_health", "arguments": {}}},
        ], expect_n=1)
        assert resp["result"]["recovered"] is True
    finally:
        proc.kill()
        httpd.get("s") and httpd["s"].shutdown()
