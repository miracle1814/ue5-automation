#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_token_auth.py — ue5_bridge 4 核心 API 的 Token 认证契约测试
==================================================================
T-20260805-UE-FULL-CLOSURE P1-2

验证 ue5_bridge 的 4 个核心写入/执行端点对错误/缺失 Token 一律返回 403：
  - /command            （update_node_param 的白名单命令通道）
  - /connect            （connect_pins_by_id）
  - /disconnect         （disconnect_pin）
  - /delete_node        （delete_node）

测试方式：真实 ue5_bridge 模块 + 真实 FastAPI 应用 + 真实 HTTP 客户端
（TestClient 走完整中间件栈），UE 侧用 fake_unreal test double —— 与
test_blueprint_editor_integration.py 同模式；Token 认证逻辑 100% 真实执行。

已知教训（KB 语义搜索）：
  - token 校验必须在 HTTP 层验证（_verify_token 读 header）
  - 集成测试必须在 import ue5_bridge 前设置 UE5_BRIDGE_TEST_MODE
"""

import os
import sys

# ── 必须在 import ue5_bridge 之前设置环境变量 ──
os.environ["UE5_BRIDGE_TEST_MODE"] = "1"
os.environ["UE5_BRIDGE_TOKEN"] = "token-auth-test-token"

_SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPTS_DIR = os.path.join(_SKILL_ROOT, "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import ue5_bridge  # noqa: E402  真实模块（测试模式 → fake_unreal 注入）
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(ue5_bridge.app)

# 4 个核心 API 对应端点 + 合法请求体（即使 token 错误，路由应先拒绝）
_APIS = [
    ("/command", {"command": "health", "params": {}}),
    ("/connect", {
        "blueprint_path": "/Game/Test/BP_TestActor",
        "connections": [{"src_node": "BeginPlay", "src_pin": "then",
                         "dst_node": "PrintString", "dst_pin": "execute"}],
    }),
    ("/disconnect", {
        "blueprint_path": "/Game/Test/BP_TestActor",
        "node_id": "BeginPlay",
        "pin_name": "then",
    }),
    ("/delete_node", {
        "blueprint_path": "/Game/Test/BP_TestActor",
        "node_id": "PrintString",
        "dry_run": True,
    }),
]

_WRONG_TOKEN = {"X-Skill-Token": "definitely-wrong-token-000"}


def test_4_apis_with_wrong_token():
    """4 个 API 带错误 token → 全部 403"""
    for path, payload in _APIS:
        resp = client.post(path, json=payload, headers=_WRONG_TOKEN)
        assert resp.status_code == 403, \
            f"{path} 带错误 token 应返回 403，实际 {resp.status_code}: {resp.text[:200]}"
        assert "Unauthorized" in resp.text or "token" in resp.text.lower(), \
            f"{path} 403 响应应含明确错误信息"


def test_4_apis_with_no_token():
    """4 个 API 无 token header → 全部 403"""
    for path, payload in _APIS:
        resp = client.post(path, json=payload)
        assert resp.status_code == 403, \
            f"{path} 无 token 应返回 403，实际 {resp.status_code}: {resp.text[:200]}"
        assert "Unauthorized" in resp.text or "token" in resp.text.lower(), \
            f"{path} 403 响应应含明确错误信息"
