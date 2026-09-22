#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ue5-automation stdio 代理 —— MCP stdio 客户端 ↔ UE 编辑器内 HTTP 桥 的传输翻译层。

为什么需要它：
  · ue5-automation 的 MCP 服务器（/mcp）运行在 UE 编辑器进程内，只支持 HTTP + token 头；
    Claude Desktop 等 stdio 优先的客户端无法直接连（不支持自定义 HTTP 头）。
  · Glama 等目录的自检要求"服务器能启动并响应 introspection"——本代理启动零依赖 UE。

职责划分：
  · initialize / tools/list / ping / resources / prompts —— 本地应答
    （工具目录复用 ue5_bridge._mcp_tool_defs()，与真服务器单一来源同步）；
  · tools/call 及其他请求 —— 原样转发到 UE 内桥（默认 http://127.0.0.1:8889/mcp），
    UE 离线时返回可读的 JSON-RPC 错误（绝不假装成功）。

协议：stdio 上的 newline-delimited JSON-RPC 2.0（MCP stdio transport）。
日志一律走 stderr（stdout 只承载协议消息）。

用法（Claude Desktop / 任意 stdio 客户端）：
    {
      "mcpServers": {
        "ue5-automation": {
          "command": "python",
          "args": ["<仓库>/skill/ue5-automation/scripts/ue5_mcp_stdio.py"]
        }
      }
    }

鉴权：token 取 UE5_BRIDGE_TOKEN 环境变量，否则读 %APPDATA%\\ue5_bridge\\token
（与 ue5_bridge 完全同口径）。
"""
import json
import os
import sys

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

BRIDGE_URL = os.environ.get("UE5_BRIDGE_URL", "http://127.0.0.1:8889/mcp")
SERVER_NAME = "ue5-automation"
SERVER_VERSION = "3.3"
SUPPORTED_PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")
DEFAULT_PROTOCOL_VERSION = "2025-06-18"
RELAY_TIMEOUT = (10, 600)  # 连接快速失败 / 读超时容忍长编译


def _log(*args):
    print("[ue5-mcp-stdio]", *args, file=sys.stderr, flush=True)


# stdout 编码钉死 UTF-8：Windows CI/控制台默认代码页（如 cp1252）会把
# 工具描述里的中文写出非 UTF-8 字节，污染 stdio 协议流。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass


def _load_token():
    env = os.environ.get("UE5_BRIDGE_TOKEN")
    if env:
        return env
    for base in (os.environ.get("APPDATA"), os.path.expanduser("~")):
        if not base:
            continue
        p = os.path.join(base, "ue5_bridge", "token")
        try:
            with open(p, "r", encoding="utf-8") as f:
                tok = f.read().strip()
                if tok:
                    return tok
        except OSError:
            continue
    return ""


_TOKEN = _load_token()

# 导入 ue5_bridge 仅为取静态工具目录（单一来源）；TEST_MODE 保证无 UE 可导入。
# 导入期 print 一律改道 stderr，避免污染 stdout 协议流。
_real_stdout = sys.stdout
sys.stdout = sys.stderr
try:
    os.environ.setdefault("UE5_BRIDGE_TEST_MODE", "1")
    import ue5_bridge as _bridge  # noqa: E402
finally:
    sys.stdout = _real_stdout


def _tool_catalog():
    try:
        return _bridge._mcp_tool_defs()
    except Exception as e:  # pragma: no cover - 防御
        _log("tool catalog unavailable:", repr(e))
        return []


def _relay(payload):
    """把 JSON-RPC 请求原样转发给 UE 内桥；离线返回可读错误。"""
    import requests
    headers = {"X-Skill-Token": _TOKEN, "Content-Type": "application/json"}
    try:
        r = requests.post(BRIDGE_URL, json=payload, headers=headers,
                          timeout=RELAY_TIMEOUT)
    except requests.exceptions.ConnectionError:
        return {"jsonrpc": "2.0", "id": payload.get("id"),
                "error": {"code": -32000, "message":
                          "UE 编辑器离线：无法连接 ue5_bridge（127.0.0.1:8889）。"
                          "请打开 UE 编辑器并确认 bridge 已启动（README 部署章节）。"}}
    except requests.exceptions.Timeout:
        return {"jsonrpc": "2.0", "id": payload.get("id"),
                "error": {"code": -32001, "message":
                          "UE 桥响应超时（编辑器可能卡住或有模态对话框）。"}}
    if r.status_code == 403:
        return {"jsonrpc": "2.0", "id": payload.get("id"),
                "error": {"code": -32002, "message":
                          "token 不匹配：读 %APPDATA%\\ue5_bridge\\token 或设 "
                          "UE5_BRIDGE_TOKEN（stdio 端与桥必须一致）。"}}
    try:
        return r.json()
    except ValueError:
        return {"jsonrpc": "2.0", "id": payload.get("id"),
                "error": {"code": -32603,
                          "message": f"桥返回非 JSON（HTTP {r.status_code}）"}}


def _handle(msg):
    """处理单条 JSON-RPC 消息；通知返回 None，请求返回响应 dict。"""
    method = msg.get("method", "")
    has_id = "id" in msg
    params = msg.get("params") or {}

    if method.startswith("notifications/"):
        return None

    if method == "initialize":
        asked = params.get("protocolVersion")
        version = asked if asked in SUPPORTED_PROTOCOL_VERSIONS \
            else DEFAULT_PROTOCOL_VERSION
        return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": "UE5 blueprint automation. Tool calls are forwarded "
                            "to the in-editor bridge — start Unreal Editor with "
                            "the bridge installed before calling tools.",
        }}

    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {}}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg.get("id"),
                "result": {"tools": _tool_catalog()}}

    if method in ("resources/list", "prompts/list"):
        key = "resources" if method == "resources/list" else "prompts"
        return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {key: []}}

    # 其余（含 tools/call）→ 转发
    resp = _relay(msg)
    if has_id and "id" not in resp:
        resp["id"] = msg.get("id")
    return resp


def _main():
    _log(f"proxy up → bridge {BRIDGE_URL} (token "
         f"{'configured' if _TOKEN else 'NOT SET'})")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError as e:
            print(json.dumps({"jsonrpc": "2.0", "id": None, "error": {
                "code": -32700, "message": f"parse error: {e}"}}), flush=True)
            continue
        batch = msg if isinstance(msg, list) else [msg]
        out = []
        for m in batch:
            if not isinstance(m, dict):
                continue
            resp = _handle(m)
            if resp is not None:
                out.append(resp)
        if out:
            # ensure_ascii=True：协议流恒为纯 ASCII（非 ASCII 转义为 \uXXXX），
            # 对 stdio 编码差异完全免疫。
            print(json.dumps(out if isinstance(msg, list) else out[0],
                             ensure_ascii=True), flush=True)


if __name__ == "__main__":
    try:
        _main()
    except KeyboardInterrupt:
        pass
