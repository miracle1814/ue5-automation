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
import time

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

BRIDGE_URL = os.environ.get("UE5_BRIDGE_URL", "http://127.0.0.1:8889/mcp")
SERVER_NAME = "ue5-automation"
SERVER_VERSION = "6.5.0"
SUPPORTED_PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")
DEFAULT_PROTOCOL_VERSION = "2025-06-18"
RELAY_TIMEOUT = (10, 600)  # 连接快速失败 / 读超时容忍长编译
# 编辑器重启保活（v4.9）：连接被拒时等待桥恢复的秒数；0 = 关闭（离线测试用）
RECONNECT_S = float(os.environ.get("UE5_BRIDGE_RECONNECT_S", "30"))


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
    explicit = os.environ.get("UE5_BRIDGE_TOKEN_FILE", "").strip()
    if explicit:
        try:
            with open(explicit, "r", encoding="utf-8") as f:
                tok = f.read().strip()
                if tok:
                    return tok
        except OSError:
            return ""
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
        # 与桥内 /mcp 的 tools/list 同口径（按 profile 裁剪；UE 离线时也
        # 不会返回与在线不一致的全量目录）。
        return _bridge._mcp_tool_defs_core()
    except Exception as e:  # pragma: no cover - 防御
        _log("tool catalog unavailable:", repr(e))
        return []


class _HttpResp:
    """极简 HTTP 响应（urllib 结果 → requests 风格接口）。"""

    def __init__(self, status_code, body):
        self.status_code = int(status_code)
        self._body = body or b""

    def json(self):
        return json.loads(self._body.decode("utf-8"))

    @property
    def text(self):
        return self._body.decode("utf-8", errors="replace")


def _post(payload):
    """零第三方依赖的 HTTP POST（标准库 urllib）。"""
    import urllib.error
    import urllib.request
    data = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    req = urllib.request.Request(
        BRIDGE_URL, data=data, method="POST",
        headers={"X-Skill-Token": _TOKEN or "",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=RELAY_TIMEOUT[1]) as resp:
            return _HttpResp(getattr(resp, "status", 200), resp.read())
    except urllib.error.HTTPError as e:
        return _HttpResp(e.code, e.read())


def _health_url():
    return BRIDGE_URL.rsplit("/mcp", 1)[0] + "/health"


def _wait_for_bridge(deadline):
    """等桥恢复（编辑器重启/桥自启）；期间重读 token（重启可能重新生成）。"""
    global _TOKEN
    import urllib.request
    while time.time() < deadline:
        fresh = _load_token()
        if fresh:
            _TOKEN = fresh
        try:
            req = urllib.request.Request(
                _health_url(), method="GET",
                headers={"X-Skill-Token": _TOKEN or ""})
            with urllib.request.urlopen(req, timeout=2) as r:
                if int(getattr(r, "status", 200)) == 200:
                    return True
        except Exception:
            pass
        time.sleep(1.0)
    return False


def _is_timeout_error(exc) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    return isinstance(getattr(exc, "reason", None), TimeoutError)


def _relay(payload):
    """把 JSON-RPC 请求原样转发给 UE 内桥；编辑器重启时等待恢复再重试一次。"""
    import urllib.error
    try:
        r = _post(payload)
    except urllib.error.URLError as e:
        if _is_timeout_error(e):
            return {"jsonrpc": "2.0", "id": payload.get("id"),
                    "error": {"code": -32001, "message":
                              "UE 桥响应超时（编辑器可能卡住或有模态对话框）。"}}
        if RECONNECT_S > 0:
            _log("bridge offline; waiting up to %.0fs for editor restart..."
                 % RECONNECT_S)
            if _wait_for_bridge(time.time() + RECONNECT_S):
                try:
                    r = _post(payload)
                except urllib.error.URLError as e2:
                    if _is_timeout_error(e2):
                        return {"jsonrpc": "2.0", "id": payload.get("id"),
                                "error": {"code": -32001, "message":
                                          "UE 桥响应超时（编辑器可能卡住或有模态对话框）。"}}
                    r = None
            else:
                r = None
        else:
            r = None
        if r is None:
            return {"jsonrpc": "2.0", "id": payload.get("id"),
                    "error": {"code": -32000, "message":
                              "UE 编辑器离线：无法连接 ue5_bridge（127.0.0.1:8889）。"
                              "已等待 %.0fs 未见恢复；请打开 UE 编辑器并确认 bridge 已启动"
                              "（UE5_BRIDGE_RECONNECT_S 可调等待时长）。" % RECONNECT_S}}
    except TimeoutError:
        return {"jsonrpc": "2.0", "id": payload.get("id"),
                "error": {"code": -32001, "message":
                          "UE 桥响应超时（编辑器可能卡住或有模态对话框）。"}}
    if r.status_code == 403:
        return {"jsonrpc": "2.0", "id": payload.get("id"),
                "error": {"code": -32002, "message":
                          "token 不匹配：读 %APPDATA%/ue5_bridge/token 或设 "
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
