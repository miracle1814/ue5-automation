#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ue5-automation 本机 bridge 客户端（doctor / smoke 共用）。

只做两件事，避免两个自检脚本各自猜 bridge 返回结构：

1. 把 ``/health`` 的嵌套字段（``ue_info``）与 ``/command health`` 的顶层字段
   归一化成同一份结构；
2. 对连接失败和 HTTP 5xx 做有限重试，并把错误整理成
   ``code / message / http_status / hint``。

只依赖 Python 标准库。
"""
import json
import os
import socket
import time
import urllib.error
import urllib.request


class BridgeError(RuntimeError):
    """带错误码与排查提示的 bridge 调用错误。"""

    def __init__(self, message, code="bridge_error", http_status=None, hint=""):
        super().__init__(message)
        self.message = str(message)
        self.code = code
        self.http_status = http_status
        self.hint = hint

    def to_dict(self):
        out = {"code": self.code, "message": self.message}
        if self.http_status is not None:
            out["http_status"] = self.http_status
        if self.hint:
            out["hint"] = self.hint
        return out


def read_token(explicit=None):
    """按 CLI 参数 → 环境变量 → %APPDATA%\\ue5_bridge\\token 读取 token。"""
    if explicit:
        return explicit.strip()
    env = os.environ.get("UE5_BRIDGE_TOKEN", "").strip()
    if env:
        return env
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    path = os.path.join(appdata, "ue5_bridge", "token")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _as_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "ok")
    return bool(value)


def _first(payload, ue_info, key):
    for src in (payload, ue_info):
        value = src.get(key)
        if value not in (None, ""):
            return value
    return ""


def normalize_health(payload):
    """两种 health 返回 → 同一结构。

    - ``GET /health``：``status`` / ``bridge_version`` 在顶层，
      ``bridge_loaded`` / ``engine_version`` 在 ``ue_info``。
    - ``POST /command {"command":"health"}``：字段都在 ``result`` 顶层。
    """
    if not isinstance(payload, dict):
        return {}
    # 兼容误传完整 /command 信封的情况
    if isinstance(payload.get("result"), dict) and "ue_info" not in payload:
        payload = payload["result"]
    ue_info = payload.get("ue_info")
    if not isinstance(ue_info, dict):
        ue_info = {}

    bridge_loaded = _as_bool(_first(payload, ue_info, "bridge_loaded"))
    status = payload.get("status") or ("ok" if bridge_loaded else "")
    return {
        "status": str(status),
        "bridge_version": str(_first(payload, ue_info, "bridge_version")),
        "bridge_loaded": bridge_loaded,
        "engine_version": str(_first(payload, ue_info, "engine_version")),
        "project_name": str(_first(payload, ue_info, "project_name")),
        "project_dir": str(_first(payload, ue_info, "project_dir")),
        "uptime_seconds": payload.get("uptime_seconds", 0),
        "request_count": payload.get("request_count", 0),
    }


class BridgeClient(object):
    """本机 bridge 的最小客户端，带有限重试。"""

    def __init__(self, host="127.0.0.1", port=8889, token="", timeout=8.0,
                 retries=2, backoff=0.4):
        self.host = host
        self.port = int(port)
        self.token = token
        self.timeout = float(timeout)
        self.retries = max(0, int(retries))
        self.backoff = max(0.0, float(backoff))

    def _url(self, path):
        return "http://%s:%d%s" % (self.host, self.port, path)

    def _request(self, method, path, payload=None):
        headers = {
            "X-Skill-Token": self.token,
            "Accept": "application/json",
        }
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"

        last_error = None
        for attempt in range(self.retries + 1):
            req = urllib.request.Request(
                self._url(path), data=body, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode("utf-8", "replace")
                    status = resp.getcode()
            except urllib.error.HTTPError as e:
                raw = e.read().decode("utf-8", "replace")
                if e.code in (401, 403):
                    raise BridgeError(
                        "鉴权失败（HTTP %s）" % e.code, "auth_error", e.code,
                        "检查 %APPDATA%\\ue5_bridge\\token 或 --token 是否与编辑器一致")
                if e.code >= 500 and attempt < self.retries:
                    last_error = e
                    time.sleep(self.backoff * (2 ** attempt))
                    continue
                raise BridgeError(
                    "HTTP %s: %s" % (e.code, raw[:200]), "http_error", e.code,
                    "先跑 doctor.py 确认 bridge 状态；500 可稍后重试")
            except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as e:
                last_error = e
                if attempt < self.retries:
                    time.sleep(self.backoff * (2 ** attempt))
                    continue
                raise BridgeError(
                    "无法连接 bridge %s:%d（%s）" % (self.host, self.port, e),
                    "network_error", None,
                    "确认 UE 编辑器已启动、端口未被占用，且服务监听 127.0.0.1")
            try:
                return json.loads(raw or "{}")
            except json.JSONDecodeError:
                raise BridgeError(
                    "bridge 返回非 JSON: %s" % raw[:200], "invalid_json", status,
                    "确认该端口是 ue5-automation，而不是其他本地服务")

        raise BridgeError(
            "bridge 请求失败：%s" % last_error, "network_error", None,
            "确认 UE 编辑器已启动并监听 127.0.0.1")

    def health(self):
        """读取 /health 并归一化。"""
        return normalize_health(self._request("GET", "/health"))

    def call(self, command, params=None):
        """调用 /command；返回 result（health 自动归一化）。"""
        data = self._request("POST", "/command", {
            "command": command,
            "params": params or {},
        })
        if not data.get("success"):
            raise BridgeError(
                data.get("error") or "command failed: %s" % command,
                "command_failed", None,
                "查看 bridge 日志；必要时先跑 doctor.py 确认连接")
        result = data.get("result") or {}
        if command == "health":
            return normalize_health(result)
        return result
