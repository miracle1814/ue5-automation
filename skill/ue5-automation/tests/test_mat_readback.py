#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_mat_readback.py — 材质族/控件族回读验证 + MCP isError 契约（离线）
=====================================================================
T-20260923-V331-MAT-READBACK（纯 Python 侧 · 离线）

背景（对照 18 个竞品的源码审计结论）：
  · 蓝图族已验证「引擎 API 的 bool 返回值会说谎」（P01/P04：`TryCreateConnection`
    是可行性验证 API，类型不匹配时建隐式 Cast 节点仍返回 True）→ 现有
    `_connect_with_triple_check` 四重验证。
  · 材质侧 `connect_material_expressions` / `connect_material_property` 是**同类
    API**，原先只判返回值 → 同一类假阳性无防护。本文件锁定修复后的行为。
  · 材质族原先在替身里完全没有建模（`grep -ci material fake_unreal.py` = 0）
    → 无任何离线用例，这是「验证密度低于蓝图族」的直接原因。

覆盖：
  ① 材质回读正路径（add_expression / connect_expressions / connect_property）
  ② 材质回读负路径（API 报成功但未生效 → fail-loud，杜绝假阳性）
  ③ 隐式转换节点检测（引擎插入转换表达式 → warnings 上报）
  ④ 回读通道不可用 → 告警降级不阻断（对齐 R1 策略）
  ⑤ material_compile 的 None 漏判修复（原 `is False` 会把未编译报成成功）
  ⑥ compile_blueprint 返回体带 success → MCP 层 isError 正确置位
  ⑦ widget_add_child 回读（报成功但未落树 → fail-loud）
  ⑧ MCP 层 is_err 同时认 success 与 ok 两种成功键

作者：dev（代码开发 Agent）
日期：2026-09-23
"""

import os
import sys
import unittest

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

MAT_PATH = "/Game/Test/M_Readback"
WBP_PATH = "/Game/Test/WBP_Readback"


class _Base(unittest.TestCase):
    """公共夹具：fake 材质/控件资产 + TestClient。"""

    def setUp(self):
        fu.reset_fake()
        self.mat = fu.create_test_material(MAT_PATH)
        self.wbp = fu.create_test_widget(WBP_PATH)
        self.client = TestClient(ue5_bridge.app)
        self.headers = {"X-Skill-Token": "integration-test-token"}

    def _cmd(self, name, **params):
        return self.client.post(
            "/command", json={"command": name, "params": params},
            headers=self.headers)

    def _ok(self, name, **params):
        data = self._cmd(name, **params).json()
        self.assertTrue(data["success"], data)
        return data["result"]

    def _fail(self, name, **params):
        data = self._cmd(name, **params).json()
        self.assertFalse(data["success"], "期望 fail-loud，实际成功: %s" % data)
        return data

    def _add(self, cls_name="Multiply", **kw):
        """加一个表达式并返回其名字。"""
        r = self._ok("material_add_expression", material_path=MAT_PATH,
                     expression_class=cls_name, **kw)
        return r["data"]["name"]


# ══════════════════════════════════════════════════════════
#  ① 材质回读正路径
# ══════════════════════════════════════════════════════════

class TestMaterialReadbackPositive(_Base):

    def test_add_expression_lands_in_material(self):
        """正路径：表达式创建后出现在材质表达式清单中。"""
        name = self._add("Multiply")
        self.assertTrue(name)
        r = self._ok("material_read", material_path=MAT_PATH)
        names = [e["name"] for e in r["data"]["expressions"]]
        self.assertIn(name, names)

    def test_add_expression_no_warning_on_clean_path(self):
        """正路径：回读成功时不产生告警。"""
        r = self._ok("material_add_expression", material_path=MAT_PATH,
                     expression_class="Multiply")
        self.assertEqual(r["data"]["warnings"], [])

    def test_connect_expressions_verified(self):
        """正路径：连线后回读比对通过（verified=True、无告警）。"""
        a = self._add("Multiply")
        b = self._add("Add")
        r = self._ok("material_connect_expressions", material_path=MAT_PATH,
                     from_name=a, to_name=b, to_input="A")
        self.assertTrue(r["data"]["connected"])
        self.assertTrue(r["data"]["verified"], r["data"])
        self.assertEqual(r["data"]["warnings"], [])

    def test_connect_property_verified(self):
        """正路径：连到材质主属性后回读比对通过。"""
        a = self._add("Constant")
        r = self._ok("material_connect_property", material_path=MAT_PATH,
                     from_name=a, property_name="base_color")
        self.assertTrue(r["data"]["connected"])
        self.assertTrue(r["data"]["verified"], r["data"])

    def test_connect_expressions_default_input(self):
        """正路径：to_input 空串 = 默认第一引脚，回读仍能比对。"""
        a = self._add("Multiply")
        b = self._add("Add")
        r = self._ok("material_connect_expressions", material_path=MAT_PATH,
                     from_name=a, to_name=b)
        self.assertTrue(r["data"]["verified"], r["data"])


# ══════════════════════════════════════════════════════════
#  ② 材质回读负路径（核心：杜绝 P04 类假阳性）
# ══════════════════════════════════════════════════════════

class TestMaterialReadbackFailLoud(_Base):

    def test_connect_expressions_false_positive_detected(self):
        """P04 类比：API 返回 True 但未建立连接 → 回读不一致 fail-loud。"""
        a = self._add("Multiply")
        b = self._add("Add")
        fu.set_mat_connect_false_positive(True)
        data = self._fail("material_connect_expressions", material_path=MAT_PATH,
                          from_name=a, to_name=b, to_input="A")
        self.assertIn("回读不一致", data["error"])
        self.assertIn(a, data["error"])

    def test_connect_property_false_positive_detected(self):
        """P04 类比（主属性）：API 返回 True 但未建立连接 → fail-loud。"""
        a = self._add("Constant")
        fu.set_mat_connect_false_positive(True)
        data = self._fail("material_connect_property", material_path=MAT_PATH,
                          from_name=a, property_name="base_color")
        self.assertIn("回读不一致", data["error"])

    def test_add_expression_drop_detected(self):
        """API 返回对象但未落图 → 回读不一致 fail-loud。"""
        fu.set_mat_add_expression_drop(True)
        data = self._fail("material_add_expression", material_path=MAT_PATH,
                          expression_class="Multiply")
        self.assertIn("未出现在材质表达式列表", data["error"])

    def test_false_positive_not_silently_reported_as_success(self):
        """回归护栏：假阳性路径绝不能返回 success=true。"""
        a = self._add("Multiply")
        b = self._add("Add")
        fu.set_mat_connect_false_positive(True)
        data = self._cmd("material_connect_expressions",
                         material_path=MAT_PATH, from_name=a, to_name=b,
                         to_input="A").json()
        self.assertFalse(data["success"])


# ══════════════════════════════════════════════════════════
#  ③ 隐式转换节点检测
# ══════════════════════════════════════════════════════════

class TestMaterialImplicitConverter(_Base):

    def test_implicit_converter_reported_as_warning(self):
        """引擎插入转换表达式 → 连线仍成功，warnings 上报（不阻断）。

        语义边界（v3.3.1 审计自查修正）：`verified` **只回答「回读比对是否执行且
        通过」**，不因这项非致命线索变 False —— 连线本身确实已按引脚比对通过。
        """
        a = self._add("Multiply")
        b = self._add("Add")
        fu.set_mat_insert_converter(True)
        r = self._ok("material_connect_expressions", material_path=MAT_PATH,
                     from_name=a, to_name=b, to_input="A")
        self.assertTrue(r["data"]["connected"])
        joined = " ".join(r["data"]["warnings"])
        self.assertIn("隐式类型转换", joined)
        # 告警存在，但比对确实做了 → verified 仍应为 True
        self.assertTrue(r["data"]["verified"], r["data"])


# ══════════════════════════════════════════════════════════
#  ④ 回读通道不可用 → 告警降级（对齐 R1 策略）
# ══════════════════════════════════════════════════════════

class TestMaterialReadbackDegrade(_Base):

    def test_add_expression_degrades_to_warning(self):
        """回读通道不可用 → 命令仍成功，但告警说明未确认落图。"""
        fu.set_mat_readback_unavailable(True)
        r = self._ok("material_add_expression", material_path=MAT_PATH,
                     expression_class="Multiply")
        joined = " ".join(r["data"]["warnings"])
        self.assertIn("回读不可用", joined)

    def test_connect_expressions_degrades_to_warning(self):
        """引脚级回读通道不可用 → 连线不因读不到而误判失败（避免假阴性）。"""
        a = self._add("Multiply")
        b = self._add("Add")
        fu.set_mat_verify_unavailable(True)
        r = self._ok("material_connect_expressions", material_path=MAT_PATH,
                     from_name=a, to_name=b, to_input="A")
        self.assertTrue(r["data"]["connected"])
        self.assertFalse(r["data"]["verified"])
        self.assertTrue(r["data"]["warnings"])

    def test_connect_property_degrades_to_warning(self):
        """主属性回读通道不可用 → 命令仍成功，verified=False + 告警。"""
        a = self._add("Constant")
        fu.set_mat_verify_unavailable(True)
        r = self._ok("material_connect_property", material_path=MAT_PATH,
                     from_name=a, property_name="base_color")
        self.assertTrue(r["data"]["connected"])
        self.assertFalse(r["data"]["verified"])
        self.assertTrue(r["data"]["warnings"])

    def test_verified_means_comparison_ran_not_no_warnings(self):
        """语义边界回归：verified 只表示「比对执行且通过」，与告警数量无关。

        构造「比对通过 + 存在非致命告警」的场景，断言 verified 为 True。
        """
        a = self._add("Multiply")
        b = self._add("Add")
        fu.set_mat_insert_converter(True)      # 制造非致命告警
        r = self._ok("material_connect_expressions", material_path=MAT_PATH,
                     from_name=a, to_name=b, to_input="A")
        self.assertTrue(r["data"]["warnings"], "本用例需要至少一条告警")
        self.assertTrue(r["data"]["verified"], "比对已执行且通过，verified 应为 True")


# ══════════════════════════════════════════════════════════
#  ⑤ material_compile：None 漏判修复
# ══════════════════════════════════════════════════════════

class TestMaterialCompileResult(_Base):

    def test_compile_true_passes(self):
        r = self._ok("material_compile", material_path=MAT_PATH)
        self.assertTrue(r["data"]["compiled"])

    def test_compile_false_fail_loud(self):
        fu.set_mat_compile_result(False)
        data = self._fail("material_compile", material_path=MAT_PATH)
        self.assertIn("未返回 True", data["error"])

    def test_compile_none_fail_loud(self):
        """核心回归：None 不再被 `is False` 放过（原实现会报 compiled:True）。"""
        fu.set_mat_compile_result(None)
        data = self._fail("material_compile", material_path=MAT_PATH)
        self.assertIn("未返回 True", data["error"])
        self.assertIn("None", data["error"])


# ══════════════════════════════════════════════════════════
#  ⑥ compile_blueprint 带 success → MCP isError 正确
# ══════════════════════════════════════════════════════════

class TestCompileBlueprintSuccessKey(_Base):

    def test_compile_blueprint_result_has_success_key(self):
        """回归护栏：返回体必须带 success，否则 MCP 层无法置 isError。"""
        bp = fu.create_test_blueprint("/Game/Test/BP_CompileKey")
        r = self._ok("compile_blueprint", bp_path="/Game/Test/BP_CompileKey")
        self.assertIn("success", r)
        self.assertIn("compiled", r)
        self.assertEqual(r["success"], r["compiled"])

    def test_compile_blueprint_mcp_iserror_true_on_failure(self):
        """编译失败 → MCP 客户端必须收到 isError:true（原实现是 false）。"""
        bp = fu.create_test_blueprint("/Game/Test/BP_CompileFail")
        # 注入编译失败：让 bridge.compile_blueprint 返回 False
        real = fu.BlueprintPythonBridge.compile_blueprint

        def _false(bp_):
            return False

        fu.BlueprintPythonBridge.compile_blueprint = staticmethod(_false)
        try:
            resp = self.client.post("/mcp", json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "ue5_compile_blueprint",
                           "arguments": {"bp_path": "/Game/Test/BP_CompileFail"}}},
                headers=self.headers).json()
        finally:
            fu.BlueprintPythonBridge.compile_blueprint = real
        self.assertTrue(resp["result"]["isError"], resp)

    def test_compile_blueprint_mcp_iserror_false_on_success(self):
        fu.create_test_blueprint("/Game/Test/BP_CompileOK")
        resp = self.client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "ue5_compile_blueprint",
                       "arguments": {"bp_path": "/Game/Test/BP_CompileOK"}}},
            headers=self.headers).json()
        self.assertFalse(resp["result"]["isError"], resp)


# ══════════════════════════════════════════════════════════
#  ⑦ widget_add_child 回读
# ══════════════════════════════════════════════════════════

class TestWidgetAddChildReadback(_Base):

    def test_add_child_verified(self):
        r = self._ok("widget_add_child", widget_path=WBP_PATH,
                     widget_class="/Script/UMG.CanvasPanel",
                     widget_name="RootCanvas")
        self.assertTrue(r["data"]["verified"], r["data"])

    def test_add_child_drop_detected(self):
        """报成功但未落树 → 回读不一致 fail-loud。"""
        fu.set_mat_widget_add_drop(True)
        data = self._fail("widget_add_child", widget_path=WBP_PATH,
                          widget_class="/Script/UMG.CanvasPanel",
                          widget_name="Ghost")
        self.assertIn("未出现在控件树", data["error"])

    def test_add_child_readback_unavailable_degrades(self):
        """回读通道不可用 → 告警降级，不误判失败。"""
        fu.set_mat_readback_unavailable(True)
        r = self._ok("widget_add_child", widget_path=WBP_PATH,
                     widget_class="/Script/UMG.CanvasPanel",
                     widget_name="NoReadback")
        self.assertFalse(r["data"]["verified"])
        self.assertTrue(r["data"]["warnings"])


# ══════════════════════════════════════════════════════════
#  ⑧ MCP 层 is_err 同时认 success 与 ok
# ══════════════════════════════════════════════════════════

class TestMcpIsErrorContract(_Base):

    def test_ok_false_also_sets_iserror(self):
        """契约显式化：只有 ok:False 的返回体也要置 isError。"""
        real = ue5_bridge._mcp_impl_dispatch

        def _stub(name, args):
            if name == "ue5_check_health":
                return lambda a: {"ok": False, "reason": "injected"}
            return real(name, args)

        ue5_bridge._mcp_impl_dispatch = _stub
        try:
            resp = self.client.post("/mcp", json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "ue5_check_health", "arguments": {}}},
                headers=self.headers).json()
        finally:
            ue5_bridge._mcp_impl_dispatch = real
        self.assertTrue(resp["result"]["isError"], resp)

    def test_mcp_still_passes_through_when_no_success_or_ok(self):
        """无 success / ok 键的返回体不受影响（向后兼容）。"""
        resp = self.client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "ue5_check_health", "arguments": {}}},
            headers=self.headers).json()
        self.assertFalse(resp["result"]["isError"], resp)


if __name__ == "__main__":
    unittest.main(verbosity=2)
