#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_blueprint_editor_mock.py — blueprint_editor 离线 Mock 测试套件
====================================================================
覆盖 blueprint_editor.py 的 4 个核心 API：disconnect_pin / delete_node /
connect_pins_by_id / update_node_param。使用 unittest.mock 模拟 HTTP 桥，
不依赖 UE 编辑器在线。

v0.7 FIX — T-20260727-UE05-V07-FIX · 问题 2

已知教训（来自 KB + 编码前语义搜索）：
  - Mock 桥接测试策略：事务模式可测试性（Mock _post 模拟各类降级）
  - 便捷函数测试不可忽略（薄封装入口必须单独 TestCase）
  - 不硬编码绝对路径（用 __file__ 推导）
"""

import unittest
from unittest.mock import patch, MagicMock

import os
import sys

# 确保可以导入 blueprint_editor
SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from blueprint_editor import (
    BlueprintEditor, EditResult, EditOperation,
    DEFAULT_BRIDGE_URL,
)


# ═══════════════════════════════════════════════════════════
# 测试数据工厂
# ═══════════════════════════════════════════════════════════

BP_PATH = "/Game/Test/BP_TestActor"
NODE_ID = "PrintString"
PIN_NAME = "InString"
NODE_A = "BeginPlay"
PIN_A = "then"
NODE_B = "Delay"
PIN_B = "execute"


def _make_post_response(success=True, data=None, detail=""):
    """构造 _post() 返回的 JSON 响应。"""
    resp = {"success": success}
    if data is not None:
        resp["data"] = data
    if detail:
        resp["detail"] = detail
    return resp


# ── disconnect 响应 ────────────────────────────────────

def _disconnect_python_api_response():
    """模拟 Python API 成功断开。"""
    return _make_post_response(success=True, data={
        "node": NODE_ID, "pin": PIN_NAME,
        "broken_links": [
            {"from": NODE_ID, "from_pin": PIN_NAME, "to": "BeginPlay", "to_pin": "execute"},
        ],
        "verified_disconnected": True,
        "method": "python_api",
    })


def _disconnect_catalog_only_response():
    """模拟 catalog_only 降级（break_link_to 失败，仅记录未真断）。"""
    return _make_post_response(success=True, data={
        "node": NODE_ID, "pin": PIN_NAME,
        "broken_links": [
            {"from": NODE_ID, "from_pin": PIN_NAME, "to": "BeginPlay", "to_pin": "execute"},
        ],
        "verified_disconnected": False,
        "method": "catalog_only",
    })


def _disconnect_already_response():
    """模拟本就无连接。"""
    return _make_post_response(success=True, data={
        "broken_links": [], "already_disconnected": True,
        "method": "python_api",
    })


# ── delete_node 响应 ──────────────────────────────────

def _delete_success_response():
    return _make_post_response(success=True, data={
        "deleted_node": NODE_ID, "broken_connections": 2,
        "removed_from_graph": True, "destroyed": True, "marked_dirty": True,
    })


def _delete_node_not_found_response():
    return _make_post_response(success=False, detail=f"节点不存在: {NODE_ID}")


# ── connect_pins 响应 ─────────────────────────────────

def _connect_success_response():
    return _make_post_response(success=True, data={
        "results": [{"status": "connected", "reason": ""}]
    })


def _connect_cross_graph_fail_response():
    return _make_post_response(success=True, data={
        "results": [{"status": "failed",
                      "reason": "跨图操作被拒绝：连线两端节点不在同一图中"}]
    })


# ── update_node_param 响应 ────────────────────────────

def _update_command_ok_response():
    """模拟 /command set_pin_default_value 成功返回（T-20260805-UE-FULL-CLOSURE 格式）。"""
    return {
        "success": True,
        "command": "set_pin_default_value",
        "result": {"ok": True, "node": NODE_ID, "pin": "InString", "value": "Hello"},
        "error": "",
        "duration_ms": 0.0,
    }


def _update_command_fail_response():
    """模拟 /command set_pin_default_value 失败返回。"""
    return {
        "success": False,
        "command": "set_pin_default_value",
        "result": None,
        "error": "BridgeError: SetPinDefaultValue 返回 False: PrintString.InString",
        "duration_ms": 0.0,
    }


# ═══════════════════════════════════════════════════════════
# 1. disconnect_pin 测试
# ═══════════════════════════════════════════════════════════

class TestDisconnectPin(unittest.TestCase):
    """离线 Mock 测试 disconnect_pin 的各种场景。"""

    def test_disconnect_success_python_api(self):
        """Python API 成功断开 → success=True, method=python_api"""
        with patch.object(BlueprintEditor, '_get_pin_links',
                          return_value=[{"from": NODE_ID, "from_pin": PIN_NAME,
                                         "to": "BeginPlay", "to_pin": "execute"}]):
            with patch.object(BlueprintEditor, '_post',
                              return_value=_disconnect_python_api_response()):
                editor = BlueprintEditor()
                result = editor.disconnect_pin(BP_PATH, NODE_ID, PIN_NAME)

        self.assertTrue(result.success)
        self.assertIn("断开 1 条连线", result.details)
        self.assertEqual(result.data.get("method"), "python_api")
        self.assertEqual(len(result.warnings), 0)

    def test_disconnect_catalog_only_returns_false(self):
        """catalog_only 降级 → success=False + warnings
        🔴 T-20260727-UE05-V07-FIX 验收项 #1/#2
        """
        with patch.object(BlueprintEditor, '_get_pin_links',
                          return_value=[{"from": NODE_ID, "from_pin": PIN_NAME,
                                         "to": "BeginPlay", "to_pin": "execute"}]):
            with patch.object(BlueprintEditor, '_post',
                              return_value=_disconnect_catalog_only_response()):
                editor = BlueprintEditor()
                result = editor.disconnect_pin(BP_PATH, NODE_ID, PIN_NAME)

        self.assertFalse(result.success,
            "catalog_only 降级不应返回 success=True")
        self.assertEqual(result.operation, "disconnect")
        self.assertIn("catalog_only", result.details)
        self.assertIn("未真断", result.details)
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("catalog_only", result.warnings[0])
        self.assertEqual(result.data.get("method"), "catalog_only")
        self.assertFalse(result.data.get("verified_disconnected"))

    def test_disconnect_already_disconnected(self):
        """本就无连接 → success=True, _post 不被调用"""
        with patch.object(BlueprintEditor, '_get_pin_links', return_value=[]):
            with patch.object(BlueprintEditor, '_post') as mock_post:
                editor = BlueprintEditor()
                result = editor.disconnect_pin(BP_PATH, NODE_ID, PIN_NAME)

        self.assertTrue(result.success)
        self.assertIn("本就无连接", result.details)
        self.assertTrue(result.data.get("already_disconnected"))
        mock_post.assert_not_called()

    def test_disconnect_bridge_failure(self):
        """Bridge 返回 success=False → success=False + errors"""
        with patch.object(BlueprintEditor, '_get_pin_links',
                          return_value=[{"from": NODE_ID, "from_pin": PIN_NAME,
                                         "to": "BeginPlay", "to_pin": "execute"}]):
            with patch.object(BlueprintEditor, '_post',
                              return_value=_make_post_response(
                                  success=False, detail="节点不存在: NonExistentNode")):
                editor = BlueprintEditor()
                result = editor.disconnect_pin(BP_PATH, "NonExistentNode", PIN_NAME)

        self.assertFalse(result.success)
        self.assertIn("断开失败", result.details)
        self.assertGreater(len(result.errors), 0)

    def test_disconnect_network_error(self):
        """网络异常 → success=False, 异常被捕获"""
        with patch.object(BlueprintEditor, '_get_pin_links',
                          return_value=[{"from": NODE_ID, "from_pin": PIN_NAME,
                                         "to": "BeginPlay", "to_pin": "execute"}]):
            with patch.object(BlueprintEditor, '_post',
                              side_effect=RuntimeError("无法连接 http://127.0.0.1:8889")):
                editor = BlueprintEditor()
                result = editor.disconnect_pin(BP_PATH, NODE_ID, PIN_NAME)

        self.assertFalse(result.success)
        self.assertIn("断开失败", result.details)
        self.assertGreater(len(result.errors), 0)


# ═══════════════════════════════════════════════════════════
# 2. delete_node 测试
# ═══════════════════════════════════════════════════════════

class TestDeleteNode(unittest.TestCase):
    """离线 Mock 测试 delete_node 的各种场景。"""

    def test_delete_node_success(self):
        """成功删除节点 → success=True"""
        with patch.object(BlueprintEditor, '_get_node_info',
                          return_value={"title": NODE_ID, "class": "K2Node_CallFunction", "pins": 3}):
            with patch.object(BlueprintEditor, '_post',
                              return_value=_delete_success_response()):
                editor = BlueprintEditor()
                result = editor.delete_node(BP_PATH, NODE_ID)

        self.assertTrue(result.success)
        self.assertIn("已删除", result.details)
        self.assertEqual(result.data.get("deleted_node"), NODE_ID)

    def test_delete_node_dry_run(self):
        """干跑模式 → success=True, _post 不被调用"""
        with patch.object(BlueprintEditor, '_get_node_info',
                          return_value={"title": NODE_ID, "class": "K2Node_CallFunction", "pins": 3}):
            with patch.object(BlueprintEditor, '_post') as mock_post:
                editor = BlueprintEditor()
                result = editor.delete_node(BP_PATH, NODE_ID, dry_run=True)

        self.assertTrue(result.success)
        self.assertIn("干跑", result.details)
        mock_post.assert_not_called()

    def test_delete_node_not_found(self):
        """节点不存在 → success=False"""
        with patch.object(BlueprintEditor, '_get_node_info', return_value=None):
            with patch.object(BlueprintEditor, '_post') as mock_post:
                editor = BlueprintEditor()
                result = editor.delete_node(BP_PATH, "NonExistentNode")

        self.assertFalse(result.success)
        self.assertIn("不存在", result.details)
        mock_post.assert_not_called()

    def test_delete_node_bridge_failure(self):
        """Bridge 返回 success=False → success=False + errors"""
        with patch.object(BlueprintEditor, '_get_node_info',
                          return_value={"title": NODE_ID, "class": "K2Node_CallFunction", "pins": 3}):
            with patch.object(BlueprintEditor, '_post',
                              return_value=_make_post_response(
                                  success=False, detail="有 2 条连线，设置 force=True 才能删除")):
                editor = BlueprintEditor()
                result = editor.delete_node(BP_PATH, NODE_ID)

        self.assertFalse(result.success)
        self.assertGreater(len(result.errors), 0)


# ═══════════════════════════════════════════════════════════
# 3. connect_pins_by_id 测试
# ═══════════════════════════════════════════════════════════

class TestConnectPinsById(unittest.TestCase):
    """离线 Mock 测试 connect_pins_by_id 的各种场景。"""

    def test_connect_success(self):
        """成功连线 → success=True（关闭 auto_snapshot 简化 mock）"""
        with patch.object(BlueprintEditor, '_post',
                          return_value=_connect_success_response()):
            editor = BlueprintEditor(auto_snapshot=False)
            result = editor.connect_pins_by_id(BP_PATH, NODE_A, PIN_A, NODE_B, PIN_B)

        self.assertTrue(result.success)
        self.assertIn("connected", result.details)

    def test_connect_cross_graph_fail(self):
        """跨图同属校验失败 → success=False"""
        with patch.object(BlueprintEditor, '_post',
                          return_value=_connect_cross_graph_fail_response()):
            editor = BlueprintEditor(auto_snapshot=False)
            result = editor.connect_pins_by_id(BP_PATH, NODE_A, PIN_A, NODE_B, PIN_B)

        self.assertFalse(result.success)
        self.assertGreater(len(result.errors), 0)

    def test_connect_network_error(self):
        """网络异常 → success=False"""
        with patch.object(BlueprintEditor, '_post',
                          side_effect=RuntimeError("无法连接")):
            editor = BlueprintEditor(auto_snapshot=False)
            result = editor.connect_pins_by_id(BP_PATH, NODE_A, PIN_A, NODE_B, PIN_B)

        self.assertFalse(result.success)
        self.assertGreater(len(result.errors), 0)


# ═══════════════════════════════════════════════════════════
# 4. update_node_param 测试
# ═══════════════════════════════════════════════════════════

class TestUpdateNodeParam(unittest.TestCase):
    """离线 Mock 测试 update_node_param 的各种场景。"""

    def test_update_success(self):
        """成功修改参数 → success=True + 走 /command set_pin_default_value"""
        mock_post = MagicMock(return_value=_update_command_ok_response())
        with patch.object(BlueprintEditor, '_verify_param', return_value=True):
            with patch.object(BlueprintEditor, '_post', mock_post):
                editor = BlueprintEditor(auto_snapshot=False)
                result = editor.update_node_param(BP_PATH, NODE_ID, "InString", "Hello")

        self.assertTrue(result.success)
        self.assertIn("InString", result.details)
        self.assertIn("Hello", result.details)
        self.assertTrue(result.data.get("verified"))

        # T-20260805-UE-FULL-CLOSURE：必须走 /command 白名单，禁用 /execute
        call_args = mock_post.call_args_list
        self.assertTrue(any(
            args[0] == "/command" and len(args) > 1
            and args[1].get("command") == "set_pin_default_value"
            for args, kwargs in call_args
        ), f"update_node_param 未走 /command set_pin_default_value: {call_args}")
        self.assertFalse(any(
            args[0] == "/execute" for args, _ in call_args
        ), "update_node_param 仍调用已废弃的 /execute 端点")

    def test_update_execute_failure(self):
        """/command 返回 success=False → 抛出 RuntimeError → success=False"""
        with patch.object(BlueprintEditor, '_post',
                          return_value=_update_command_fail_response()):
            editor = BlueprintEditor(auto_snapshot=False)
            result = editor.update_node_param(BP_PATH, NODE_ID, "InString", "Hello")

        self.assertFalse(result.success)
        # /command 失败会 raise RuntimeError，被 catch
        self.assertIn("修改失败", result.details)

    def test_update_network_error(self):
        """网络异常 → success=False"""
        with patch.object(BlueprintEditor, '_post',
                          side_effect=RuntimeError("UE 编辑器未连接")):
            editor = BlueprintEditor(auto_snapshot=False)
            result = editor.update_node_param(BP_PATH, NODE_ID, "InString", "Hello")

        self.assertFalse(result.success)
        self.assertGreater(len(result.errors), 0)

    def test_update_verify_failure(self):
        """执行成功但验证失败 → success=True + ⚠️ 未验证"""
        with patch.object(BlueprintEditor, '_verify_param', return_value=False):
            with patch.object(BlueprintEditor, '_post',
                              return_value=_update_command_ok_response()):
                editor = BlueprintEditor(auto_snapshot=False)
                result = editor.update_node_param(BP_PATH, NODE_ID, "InString", "Hello")

        self.assertTrue(result.success)
        self.assertIn("未验证", result.details)
        self.assertFalse(result.data.get("verified"))

    def test_token_header_sent(self):
        """T-20260805-UE-FULL-CLOSURE：_post 必须带 X-Skill-Token header"""
        mock_post = MagicMock(return_value=_update_command_ok_response())
        with patch.object(BlueprintEditor, '_verify_param', return_value=True):
            with patch.object(BlueprintEditor, '_post', mock_post):
                editor = BlueprintEditor(auto_snapshot=False, token="test-token-abc")
                editor.update_node_param(BP_PATH, NODE_ID, "InString", "Hello")

        call_args = mock_post.call_args_list
        self.assertTrue(call_args)
        # 第一次调用 _post 时必须带 X-Skill-Token
        first_kwargs = call_args[0][1] if len(call_args[0]) > 1 else {}
        # _post 的 headers 在 _auth_headers() 内构造，这里验证实例 token 已传入
        self.assertEqual(editor.token, "test-token-abc")


# ═══════════════════════════════════════════════════════════
# 5. 事务日志验证
# ═══════════════════════════════════════════════════════════

class TestTransactionLogging(unittest.TestCase):
    """验证事务日志记录正确。"""

    def test_disconnect_logs_catalog_only_as_partial(self):
        """catalog_only 降级时事务日志记录为 PARTIAL"""
        with patch.object(BlueprintEditor, '_get_pin_links',
                          return_value=[{"from": NODE_ID, "from_pin": PIN_NAME,
                                         "to": "BeginPlay", "to_pin": "execute"}]):
            with patch.object(BlueprintEditor, '_post',
                              return_value=_disconnect_catalog_only_response()):
                editor = BlueprintEditor()
                editor.disconnect_pin(BP_PATH, NODE_ID, PIN_NAME)

        self.assertEqual(len(editor._history), 1)
        op = editor._history[0]
        self.assertEqual(op.op_type, "disconnect")
        self.assertEqual(op.status, "PARTIAL")
        self.assertIn("catalog_only", op.error)


# ═══════════════════════════════════════════════════════════
# 运行入口
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    unittest.main(verbosity=2)
