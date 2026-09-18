#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_pywrap_h1h2.py — H-1 / H-2 / M-1 / M-2 修复的离线回归
==========================================================================
T-20260910-UE5BRIDGE-PYWRAP-H1H2-FIX（S1 离线补充用例）

设计要点：**离线复现在线根因**
  · H-1 在线根因：DLL 建节点函数返回的裸 `unreal.K2Node` 在 UE 5.1 Python 下
    `NodeGuid` / `NodeTitle` 不可达（无 BlueprintReadOnly）→ 恒 ''
    → 本文件用 `_NakedNode`（无任何属性）模拟该返回值，断言接口**仍返回真 GUID**
      （真 GUID 只能由 `list_nodes()` 的 FBPNodeInfo 快照差集取得）。
  · M-2 在线根因：`list_pins(BPNodeInfo)` 被 UE 报
    `Cannot nativize 'BPNodeInfo' as 'Node'`
    → 本文件给 `list_pins` 注入「非 FakeNode 即抛」的严格替身，断言回读仍为 True。
  · H-2 在线根因：DLL「先插节点再返回 None」→ 本文件注入该副作用，断言
    残留节点被删除且节点数回落；删不掉时必须 fail-loud 报「图已污染」。
  · M-1：三条守卫的**负向实证**（注入错误 → 必须 raise）。

作者：dev（代码开发 Agent）  日期：2026-09-10
"""

import os
import sys
import unittest
from unittest import mock

os.environ["UE5_BRIDGE_TEST_MODE"] = "1"
os.environ["UE5_BRIDGE_TOKEN"] = "integration-test-token"

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(SKILL_ROOT, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import ue5_bridge            # noqa: E402
import fake_unreal as fu     # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

BP_PATH = "/Game/Test/BP_PyWrapH1H2"
FN_GRAPH = "H1Fn"


class _NakedNode:
    """模拟 UE 5.1 Python 下的裸 `unreal.K2Node`：node_guid/node_title 全不可达。"""

    __slots__ = ()


class _Base(unittest.TestCase):

    def setUp(self):
        fu.reset_fake()
        self.bp = fu.create_test_blueprint(BP_PATH)
        self.bp.add_graph(fu.FakeGraph(FN_GRAPH))
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
        self.assertFalse(data["success"], data)
        self.assertIn("BridgeError", data["error"])
        return data["error"]

    def _graph(self, name="EventGraph"):
        return self.bp.graphs[name]

    def _naked_adder(self, real_fn, drop_at=(0, 0)):
        """返回 side_effect：真落图（模拟 DLL）+ 返回裸对象（模拟 UE 包装）。"""
        def _se(graph, cls, x, y):
            node = real_fn(graph, cls, drop_at[0], drop_at[1])
            # 让差集定位拿到真实 GUID 的同时，返回对象完全不可读
            return _NakedNode() if node is not None else None
        return _se


# ══════════════════════════════════════════════════════════
# ① H-1 · 裸返回对象下 6 条建节点接口仍取到真 GUID
# ══════════════════════════════════════════════════════════

class H1GuidContractTests(_Base):

    def test_h1_01_create_node_by_class_guid_from_snapshot_diff(self):
        real = fu.BlueprintPythonBridge.add_node_by_class
        with mock.patch.object(fu.BlueprintPythonBridge, "add_node_by_class",
                               side_effect=self._naked_adder(real, (300, 400))):
            res = self._ok("create_node_by_class", bp_path=BP_PATH,
                           graph_name="EventGraph",
                           node_class="K2Node_IfThenElse", node_pos=[300, 400])
        self.assertTrue(res["node_id"], "H-1：裸返回对象下 node_id 仍须非空")
        node = self._graph().get_nodes()[0]
        self.assertEqual(res["node_id"],
                         ue5_bridge._pywrap_norm_guid(node.node_guid))
        self.assertEqual(res["pos"], [300, 400])

    def test_h1_03_get_node_by_guid_title_class_from_list_nodes(self):
        guid = self._ok("create_node_by_class", bp_path=BP_PATH,
                        graph_name="EventGraph",
                        node_class="K2Node_IfThenElse",
                        node_pos=[10, 10])["node_id"]

        orig_get = fu.BlueprintPythonBridge.get_node_by_guid

        def _se(graph, g):
            real = orig_get(graph, g)
            return _NakedNode() if real is not None else None

        with mock.patch.object(fu.BlueprintPythonBridge, "get_node_by_guid",
                               side_effect=_se):
            res = self._ok("get_node_by_guid", bp_path=BP_PATH,
                           node_guid=guid)
        self.assertEqual(res["node_id"], guid)
        self.assertEqual(res["title"], "K2Node_IfThenElse")
        self.assertEqual(res["node_class"], "K2Node_IfThenElse")

    def test_h1_04_add_variable_node_guid(self):
        def _se(graph, var_name, is_setter, x, y):
            n = fu.FakeNode(str(var_name), "K2Node_VariableSet", x, y)
            n.add_pin(str(var_name))
            graph.add_node(n)
            return _NakedNode()
        with mock.patch.object(fu.BlueprintPythonBridge, "add_variable_node",
                               side_effect=_se):
            res = self._ok("add_variable_node", bp_path=BP_PATH,
                           var_name="H1Var", b_is_setter=True,
                           node_pos=[0, 480])
        self.assertTrue(res["node_id"], "H-1：变量节点 node_id 须非空")

    def test_h1_05_add_custom_event_node_title_and_guid(self):
        def _se(graph, name, x, y, sig=""):
            n = fu.FakeNode("%s\n自定义事件" % name, "K2Node_CustomEvent", x, y)
            graph.add_node(n)
            return _NakedNode()
        with mock.patch.object(fu.BlueprintPythonBridge, "add_custom_event_node",
                               side_effect=_se):
            res = self._ok("add_custom_event_node", bp_path=BP_PATH,
                           event_name="H1Evt", node_pos=[0, 300])
        self.assertTrue(res["node_id"], "H-1：自定义事件 node_id 须非空")
        self.assertIn("H1Evt", res["title"],
                      "M-1：标题守卫依赖的真标题须来自 BPNodeInfo")

    def test_h1_06_add_input_key_event_node_guid(self):
        def _se(graph, key_name, x, y):
            n = fu.FakeNode(str(key_name), "K2Node_InputKey", x, y)
            graph.add_node(n)
            return _NakedNode()
        with mock.patch.object(fu.BlueprintPythonBridge,
                               "add_input_key_event_node", side_effect=_se):
            res = self._ok("add_input_key_event_node", bp_path=BP_PATH,
                           key_name="W", node_pos=[0, 900])
        self.assertTrue(res["node_id"], "H-1：按键事件 node_id 须非空")

    def test_h1_08_add_timeline_node_guid(self):
        def _se(graph, name, x, y):
            n = fu.FakeNode(str(name), "K2Node_Timeline", x, y)
            for p in fu.BlueprintPythonBridge.TIMELINE_PINS:
                n.add_pin(p)
            graph.add_node(n)
            return _NakedNode()
        with mock.patch.object(fu.BlueprintPythonBridge, "add_timeline_node",
                               side_effect=_se):
            res = self._ok("add_timeline_node", bp_path=BP_PATH,
                           timeline_name="H1TL", node_pos=[0, 1100])
        self.assertTrue(res["node_id"], "H-1：Timeline node_id 须非空")

    def test_h1_09_add_comment_node_guid_and_title(self):
        def _se(graph, text, x, y, w, h):
            n = fu.FakeNode(str(text), "EdGraphNode_Comment", x, y)
            graph.add_node(n)
            return _NakedNode()
        with mock.patch.object(fu.BlueprintPythonBridge, "add_comment_node",
                               side_effect=_se):
            res = self._ok("add_comment_node", bp_path=BP_PATH,
                           text="H1 注释框", node_pos=[-400, -300])
        self.assertTrue(res["node_id"], "H-1：注释节点 node_id 须非空")
        self.assertIn("H1 注释框", res["title"],
                      "M-1：D-2 哨兵依赖的真标题须来自 BPNodeInfo")


# ══════════════════════════════════════════════════════════
# ② M-2 · add_function_pin 回读改走真实节点对象
# ══════════════════════════════════════════════════════════

class M2ReadbackTests(_Base):

    def test_m2_function_pin_readback_true_under_strict_list_pins(self):
        orig = fu.BlueprintPythonBridge.list_pins

        def _strict(node):
            if not isinstance(node, fu.FakeNode):
                raise TypeError("NativizeProperty: Cannot nativize "
                                "'BPNodeInfo' as 'Node'")
            return orig(node)

        with mock.patch.object(fu.BlueprintPythonBridge, "list_pins",
                               side_effect=_strict):
            res = self._ok("add_function_pin", bp_path=BP_PATH,
                           graph_name=FN_GRAPH, pin_name="H1In",
                           pin_type="int", b_is_input=True)
        self.assertTrue(res["readback"], res)
        self.assertEqual(res["warnings"], [], res)


# ══════════════════════════════════════════════════════════
# ③ M-1 · 三条守卫的负向实证（注入错误 → 必须 raise）
# ══════════════════════════════════════════════════════════

class M1GuardNegativeTests(_Base):

    def test_guard_create_node_position_mismatch_raises(self):
        real = fu.BlueprintPythonBridge.add_node_by_class
        with mock.patch.object(fu.BlueprintPythonBridge, "add_node_by_class",
                               side_effect=self._naked_adder(real, (7, 9))):
            err = self._fail("create_node_by_class", bp_path=BP_PATH,
                             graph_name="EventGraph",
                             node_class="K2Node_IfThenElse",
                             node_pos=[300, 400])
        self.assertIn("位置回读不一致", err)

    def test_guard_custom_event_title_mismatch_raises(self):
        def _se(graph, name, x, y, sig=""):
            n = fu.FakeNode("WRONG_TITLE", "K2Node_CustomEvent", x, y)
            graph.add_node(n)
            return n
        with mock.patch.object(fu.BlueprintPythonBridge, "add_custom_event_node",
                               side_effect=_se):
            err = self._fail("add_custom_event_node", bp_path=BP_PATH,
                             event_name="H1Evt")
        self.assertIn("标题回读不符", err)

    def test_guard_comment_d2_sentinel_raises(self):
        fu.set_comment_text_drop(True)     # 复现 D-2：文本未写入
        err = self._fail("add_comment_node", bp_path=BP_PATH, text="H1 注释框")
        self.assertIn("注释文本未写入", err)


# ══════════════════════════════════════════════════════════
# ④ H-2 · 失败分支残留节点自动清理 + 删不掉时 fail-loud
# ══════════════════════════════════════════════════════════

class H2ResidueCleanupTests(_Base):

    @staticmethod
    def _polluting_side_effect(graph, key_name, x, y):
        """复现 DLL 行为：先插非法节点，再返回 None（失败）。"""
        n = fu.FakeNode(str(key_name), "K2Node_InputKey", x, y)
        n.add_pin("Pressed")
        n.add_pin("Released")
        n.add_pin("Key")
        graph.add_node(n)
        return None

    def test_h2_residue_deleted_and_node_count_restored(self):
        graph = self._graph()
        before = len(graph.get_nodes())
        with mock.patch.object(fu.BlueprintPythonBridge,
                               "add_input_key_event_node",
                               side_effect=self._polluting_side_effect):
            err = self._fail("add_input_key_event_node", bp_path=BP_PATH,
                             key_name="NotAKeyXYZ", node_pos=[0, 1480])
        self.assertIn("返回 None", err)
        self.assertIn("已自动清理残留节点", err)
        self.assertEqual(len(graph.get_nodes()), before,
                         "H-2 验收：图节点数必须与调用前一致")

    def test_h2_cleanup_failure_fails_loud_with_pollution_flag(self):
        graph = self._graph()
        with mock.patch.object(fu.BlueprintPythonBridge,
                               "add_input_key_event_node",
                               side_effect=self._polluting_side_effect):
            with mock.patch.object(fu.BlueprintPythonBridge, "remove_node",
                                   return_value=False):
                err = self._fail("add_input_key_event_node", bp_path=BP_PATH,
                                 key_name="NotAKeyXYZ", node_pos=[0, 1480])
        self.assertIn("图已污染", err)
        self.assertIn("NotAKeyXYZ", err, "污染时必须给出残留节点标识（不可静默）")
        self.assertEqual(len(graph.get_nodes()), 1, "本例故意保留残留以验证 fail-loud")

    def test_h2_no_residue_keeps_plain_error(self):
        """无残留（合法失败）→ 不应出现「已自动清理」噪音。"""
        with mock.patch.object(fu.BlueprintPythonBridge,
                               "add_input_key_event_node", return_value=None):
            err = self._fail("add_input_key_event_node", bp_path=BP_PATH,
                             key_name="NotAKey")
        self.assertIn("返回 None", err)
        self.assertNotIn("已自动清理残留节点", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
