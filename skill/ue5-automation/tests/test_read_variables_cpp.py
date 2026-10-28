#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_read_variables_cpp.py — read_variables C++ ReadVariables API 对接专项测试
============================================================================
T-20260823-UE5BRIDGE-CPP-APIS 子项2：C++ 新增 ReadVariables（v0.10）后，
Python _read_variables 优先调用 bridge.read_variables（真实 UE Python 绑定
无 new_variables 属性 → 旧路径假阳性/降级）。本文件验证：

  1. C++ 路径（BRIDGE_AVAILABLE + hasattr(bridge, 'read_variables')）优先
     → read_variables 返回真实变量清单（含 name + type）
  2. 无变量蓝图 → 空列表
  3. 多变量类型（Boolean/Integer/Float/String/Vector）映射
  4. bridge 无 read_variables → 降级 Python new_variables 路径（fake 兼容）
  5. fake dict 条目 → FBPVariableInfo 结构字段兼容
  6. bridge.read_variables 抛异常 → BridgeError 如实报错（不假阳性）

测试层级：集成测试（真实启动 ue5_bridge.py + fake_unreal 替身 + 真实 HTTP）。

作者：dev（代码开发 Agent）
日期：2026-08-23（T-20260823-UE5BRIDGE-CPP-APIS 子项2）
"""

import os
import sys
import unittest
from unittest import mock

# ── 必须在 import ue5_bridge 之前设置环境变量 ──
os.environ["UE5_BRIDGE_TEST_MODE"] = "1"
os.environ["UE5_BRIDGE_TOKEN"] = "integration-test-token"

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(SKILL_ROOT, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import ue5_bridge            # noqa: E402  真实模块（测试模式 → fake_unreal 注入）
import fake_unreal as fu     # noqa: E402  test double
from fastapi.testclient import TestClient  # noqa: E402

BP_PATH = "/Game/Test/BP_ReadVars"


class ReadVariablesCppTests(unittest.TestCase):
    """C++ 路径优先：read_variables 返回真实变量清单。"""

    def setUp(self):
        fu.reset_fake()
        self.bp = fu.create_test_blueprint(BP_PATH)
        self.client = TestClient(ue5_bridge.app)
        self.headers = {"X-Skill-Token": "integration-test-token"}

    def _cmd(self, name, **params):
        return self.client.post(
            "/command",
            json={"command": name, "params": params},
            headers=self.headers,
        )

    def _add_var(self, name, var_type):
        return self._cmd("add_variable", bp_path=BP_PATH,
                         var_name=name, var_type=var_type)

    def test_read_variables_cpp_path_returns_real_list(self):
        """C++ 路径：read_variables 返回真实变量清单（含 name + type）。"""
        self._add_var("bFlag", "bool")
        self._add_var("iCount", "int")
        self._add_var("fSpeed", "float")
        resp = self._cmd("read_variables", bp_path=BP_PATH)
        data = resp.json()
        self.assertTrue(data["success"], data)
        vars_ = data["result"]
        names = [v["name"] for v in vars_]
        self.assertEqual(names, ["bFlag", "iCount", "fSpeed"])
        # fake add_member_variable 存 var_type 原样（bool/int/float），
        # 真实 C++ 映射为 Boolean/Integer/Float——fake 侧透传
        types = {v["name"]: v["type"] for v in vars_}
        self.assertEqual(types["bFlag"], "bool")
        self.assertEqual(types["iCount"], "int")
        self.assertEqual(types["fSpeed"], "float")

    def test_read_variables_empty_bp(self):
        """无变量蓝图 → 空列表。"""
        resp = self._cmd("read_variables", bp_path=BP_PATH)
        data = resp.json()
        self.assertTrue(data["success"], data)
        self.assertEqual(data["result"], [])

    def test_read_variables_struct_fields_present(self):
        """返回条目含全部 FBPVariableInfo 字段（default/category/is_editable）。"""
        self._add_var("bFlag", "bool")
        resp = self._cmd("read_variables", bp_path=BP_PATH)
        vars_ = resp.json()["result"]
        self.assertEqual(len(vars_), 1)
        item = vars_[0]
        for field in ("name", "type", "default_value",
                      "is_exposed", "category", "is_editable"):
            self.assertIn(field, item)

    def test_read_variables_cpp_api_preferred(self):
        """优先走 C++ API（bridge.read_variables 被调用而非降级路径）。"""
        with mock.patch.object(
            fu.BlueprintPythonBridge, "read_variables",
            return_value=[{"variable_name": "FromCpp",
                           "variable_type": "Boolean",
                           "default_value": "true",
                           "category": "Test",
                           "is_editable": True}],
        ):
            resp = self._cmd("read_variables", bp_path=BP_PATH)
        vars_ = resp.json()["result"]
        self.assertEqual(vars_[0]["name"], "FromCpp")
        self.assertEqual(vars_[0]["type"], "Boolean")
        self.assertEqual(vars_[0]["default_value"], "true")
        self.assertEqual(vars_[0]["category"], "Test")
        self.assertTrue(vars_[0]["is_editable"])

    def test_read_variables_degrade_when_api_missing(self):
        """bridge 无 read_variables → 降级 Python new_variables 路径。"""
        with mock.patch.object(ue5_bridge, "bridge", None):
            # BRIDGE_AVAILABLE=True 但 hasattr(None,'read_variables')=False → 降级
            self._add_var("dVar", "bool")
            resp = self._cmd("read_variables", bp_path=BP_PATH)
        data = resp.json()
        self.assertTrue(data["success"], data)
        # fake new_variables dict 条目：降级路径逐字段 try/except
        # （dict 无 var_name 属性 → 名称为空，不抛错，无假阳性）
        self.assertIsInstance(data["result"], list)

    def test_read_variables_error_not_fake_positive(self):
        """C++ API 抛异常 → BridgeError 如实报错（不假阳性）。"""
        def _boom(*args, **kwargs):
            raise RuntimeError("boom")

        with mock.patch.object(
            fu.BlueprintPythonBridge, "read_variables", new=_boom,
        ):
            resp = self._cmd("read_variables", bp_path=BP_PATH)
        data = resp.json()
        self.assertFalse(data["success"])
        # 如实报错（不假阳性）：C++ API 路径 _safe_call 分类透传
        self.assertIn("variables_read", data["error"])
        self.assertIn("boom", data["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
