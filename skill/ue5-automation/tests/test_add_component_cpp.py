#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_add_component_cpp.py — add_component C++ SCS API 对接专项测试
================================================================
T-20260822-ADDCOMPONENT-CPP：C++ 新增 AddComponentToBlueprint（v0.9）后，
Python _cmd_add_component 优先调用 bridge.add_component_to_blueprint，
失败降级 _add_component_unreal。本文件验证：

  1. C++ 路径（BRIDGE_AVAILABLE + hasattr）优先 → 组件注册到 bp.components
  2. 真实 UE 反射返回值 str-or-None 语义（T-20260822-ADDCOMPONENT-ONLINE
     阶段1实测）：''=成功 / None=失败 / 非空str=失败且值为错误文本
  3. 短名 / 全路径两种 ComponentClass 写法
  4. 重名组件 → BridgeError（严格拒绝，错误文本透传）
  5. properties → T-20260823-UE5BRIDGE-CPP-APIS 子项1：添加成功后逐属性调用
     bridge.set_component_property（C++ SetComponentProperty v0.10），
     合法属性成功设置 / 非法属性名报错（取代裁决 C fail-loud）
  6. bridge 返回非空错误文本 → BridgeError 透传
  7. bridge 不可用 → 降级 _add_component_unreal（fake 无 SCS → 明确报错）

测试层级：集成测试（真实启动 ue5_bridge.py + fake_unreal 替身 + 真实 HTTP）。

作者：dev（代码开发 Agent）
日期：2026-08-23（T-20260823-UE5BRIDGE-CPP-APIS 子项1 更新：
     properties fail-loud → C++ SetComponentProperty 真实现；新增
     属性成功设置 + 非法属性名报错 + FVector 值格式用例）
"""

import os
import sys
import unittest
from unittest import mock

# ── 必须在 import ue5_bridge 之前设置环境变量 ──
# ⚠️ 与 test_blueprint_editor_integration.py 使用同一 token：pytest 同进程
# 共享 env，ue5_bridge 模块 import 时锁定 _EXPECTED_TOKEN，任何文件先导入
# 都会决定全进程 token；值不一致 → 其他文件 403。
os.environ["UE5_BRIDGE_TEST_MODE"] = "1"
os.environ["UE5_BRIDGE_TOKEN"] = "integration-test-token"

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(SKILL_ROOT, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import ue5_bridge            # noqa: E402  真实模块（测试模式 → fake_unreal 注入）
import fake_unreal as fu     # noqa: E402  test double
from fastapi.testclient import TestClient  # noqa: E402

BP_PATH = "/Game/Test/BP_AddComp"


class AddComponentCppTests(unittest.TestCase):
    """C++ 路径优先：add_component_to_blueprint。"""

    def setUp(self):
        fu.reset_fake()
        self.bp = fu.create_test_blueprint(BP_PATH)
        self.client = TestClient(ue5_bridge.app)
        # 硬编码固定 token（与模块锁定值一致，防其他测试文件覆盖 env）
        self.headers = {"X-Skill-Token": "integration-test-token"}

    def _cmd(self, name, **params):
        return self.client.post(
            "/command",
            json={"command": name, "params": params},
            headers=self.headers,
        )

    def test_cpp_path_add_component_success(self):
        """C++ 路径：添加 StaticMeshComponent 成功，组件注册到 bp.components。"""
        resp = self._cmd("add_component", bp_path=BP_PATH,
                         comp_name="MeshComp", comp_class="StaticMeshComponent")
        data = resp.json()
        self.assertTrue(data["success"], data)
        self.assertEqual(data["result"]["added"], "MeshComp")
        self.assertEqual(data["result"]["class"], "StaticMeshComponent")
        self.assertEqual(len(self.bp.components), 1)
        self.assertEqual(self.bp.components[0].get_name(), "MeshComp")
        self.assertEqual(self.bp.components[0].component_class, "StaticMeshComponent")

    def test_cpp_path_class_short_and_full_path(self):
        """短名与全路径两种 ComponentClass 写法均支持。"""
        for cls in ("SceneComponent", "/Script/Engine.SceneComponent"):
            fu.reset_fake()
            self.bp = fu.create_test_blueprint(BP_PATH)
            name = "Comp_" + cls.replace("/", "_").replace(".", "_")
            resp = self._cmd("add_component", bp_path=BP_PATH,
                             comp_name=name, comp_class=cls)
            data = resp.json()
            self.assertTrue(data["success"], data)
            self.assertEqual(len(self.bp.components), 1)

    def test_cpp_path_duplicate_component_fails(self):
        """重名组件 → success=False + 错误文本透传（严格拒绝）。"""
        self._cmd("add_component", bp_path=BP_PATH,
                  comp_name="MeshComp", comp_class="StaticMeshComponent")
        resp = self._cmd("add_component", bp_path=BP_PATH,
                         comp_name="MeshComp", comp_class="StaticMeshComponent")
        data = resp.json()
        self.assertFalse(data["success"])
        self.assertIn("组件添加失败", data["error"])
        self.assertIn("already exists", data["error"])

    def test_cpp_path_properties_success(self):
        """properties → C++ SetComponentProperty 成功设置（取代 fail-loud）。"""
        resp = self._cmd("add_component", bp_path=BP_PATH,
                         comp_name="MeshComp", comp_class="StaticMeshComponent",
                         properties={"Mobility": "Movable"})
        data = resp.json()
        self.assertTrue(data["success"], data)
        self.assertEqual(len(self.bp.components), 1)
        # 属性已设置到组件模板（fake set_editor_property 存 _props）
        comp = self.bp.components[0]
        self.assertEqual(comp.get_editor_property("Mobility"), "Movable")

    def test_cpp_path_properties_vector_value(self):
        """properties 含 FVector 值（list → 空格分隔字符串）。"""
        resp = self._cmd("add_component", bp_path=BP_PATH,
                         comp_name="BoxComp", comp_class="BoxComponent",
                         properties={"BoxExtent": [100, 200, 50]})
        data = resp.json()
        self.assertTrue(data["success"], data)
        comp = self.bp.components[0]
        self.assertEqual(comp.get_editor_property("BoxExtent"), "100 200 50")

    def test_cpp_path_properties_invalid_property_fails(self):
        """非法属性名 → 添加失败 + 错误文本透传（非静默）。"""
        resp = self._cmd("add_component", bp_path=BP_PATH,
                         comp_name="MeshComp", comp_class="StaticMeshComponent",
                         properties={"NotARealProperty": "x"})
        data = resp.json()
        self.assertFalse(data["success"])
        self.assertIn("设置组件属性失败", data["error"])
        self.assertIn("NotARealProperty", data["error"])

    def test_cpp_path_properties_fail_fast_on_add_error(self):
        """属性设置失败时组件已添加但命令失败——错误可诊断、不静默。"""
        with mock.patch.object(
            fu.BlueprintPythonBridge, "set_component_property",
            return_value=None,  # bool=false 类 → Python None → 失败无文本
        ):
            resp = self._cmd("add_component", bp_path=BP_PATH,
                             comp_name="MeshComp", comp_class="StaticMeshComponent",
                             properties={"Mobility": "Movable"})
        data = resp.json()
        self.assertFalse(data["success"])
        self.assertIn("设置组件属性失败: MeshComp.Mobility", data["error"])
        # 组件本身已添加（副作用已发生，错误可诊断）
        self.assertEqual(len(self.bp.components), 1)

    def test_cpp_path_error_text_propagates(self):
        """bridge 返回非空错误文本 → BridgeError 透传（err 文本）。"""
        with mock.patch.object(
            fu.BlueprintPythonBridge, "add_component_to_blueprint",
            return_value="boom",
        ):
            resp = self._cmd("add_component", bp_path=BP_PATH,
                             comp_name="MeshComp", comp_class="StaticMeshComponent")
        data = resp.json()
        self.assertFalse(data["success"])
        self.assertIn("组件添加失败: boom", data["error"])

    def test_cpp_path_str_none_semantics(self):
        """真实 UE 反射语义三用例：''=成功 / None=失败 / 非空str=失败err。"""
        # ① '' 空字符串 → 成功
        with mock.patch.object(
            fu.BlueprintPythonBridge, "add_component_to_blueprint",
            return_value="",
        ):
            resp = self._cmd("add_component", bp_path=BP_PATH,
                             comp_name="MeshComp", comp_class="StaticMeshComponent")
        self.assertTrue(resp.json()["success"], resp.text)

        # ② None → 失败（OutError 未写 → 错误文本用 comp_class 兜底）
        with mock.patch.object(
            fu.BlueprintPythonBridge, "add_component_to_blueprint",
            return_value=None,
        ):
            resp = self._cmd("add_component", bp_path=BP_PATH,
                             comp_name="MeshComp", comp_class="StaticMeshComponent")
        data = resp.json()
        self.assertFalse(data["success"])
        self.assertIn("组件添加失败: StaticMeshComponent", data["error"])

        # ③ 非空 str → 失败且值为错误文本
        with mock.patch.object(
            fu.BlueprintPythonBridge, "add_component_to_blueprint",
            return_value="Class not found: Foo",
        ):
            resp = self._cmd("add_component", bp_path=BP_PATH,
                             comp_name="MeshComp", comp_class="StaticMeshComponent")
        data = resp.json()
        self.assertFalse(data["success"])
        self.assertIn("组件添加失败: Class not found: Foo", data["error"])

    def test_legacy_degrade_when_bridge_method_missing(self):
        """bridge 不可用（None）→ 降级 _add_component_unreal。"""
        with mock.patch.object(ue5_bridge, "bridge", None):
            # BRIDGE_AVAILABLE=True 但 hasattr(None, ...)=False → 走降级；
            # fake BlueprintEditorLibrary.get_blueprint_simple_construction_script
            # 返回 None → 明确报错「无法获取 SCS」
            resp = self._cmd("add_component", bp_path=BP_PATH,
                             comp_name="MeshComp", comp_class="StaticMeshComponent")
        data = resp.json()
        self.assertFalse(data["success"])
        self.assertIn("无法获取 SCS", data["error"])

    def test_not_supported_marker_updated_to_supported(self):
        """execute_command docstring 标注已从 not-supported 更新为 supported。"""
        doc = ue5_bridge.execute_command.__doc__ or ""
        self.assertIn("add_component: ✅ supported", doc)
        self.assertNotIn("add_component: ⚠️ not-supported", doc)


if __name__ == "__main__":
    unittest.main(verbosity=2)
