#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_pywrap_l2.py — PATCH-A/B 剩余 21 项接口 L2 Python 封装离线单测
================================================================
T-20260912-UE5BRIDGE-PYWRAP-L2-IMPL（纯 Python 侧 · 离线）

覆盖 21 条白名单命令（D3 §5.2 扣除 L1 已封装 11 项后的精确差集）：
  引脚/属性(4)：add_pin_to_node / remove_pin_from_node /
                try_node_add_input_pin / set_node_property
  节点构造(14)：add_cast_node / add_class_cast_node / add_macro_node /
                add_switch_node / add_struct_node / add_enum_literal_node /
                add_composite_node / add_math_expression_node /
                add_async_action_node / add_create_delegate_node /
                add_actor_bound_event_node / add_input_axis_event_node /
                add_input_action_event_node / add_component_bound_event_node
  蓝图/委托(3)：remove_component / add_implemented_interface /
                add_event_dispatcher

测试层级：集成测试（真实启动 ue5_bridge.py + fake_unreal 替身 + 真实 HTTP），
          不依赖 UE（不需要真实 DLL / 编辑器）。

每项至少：① 正路径 1 条（断言真实返回结构 node_id / pins / 回读）
          ② fail-loud 负路径 1 条（底层 None/False → success=false，杜绝假阳性）

作者：dev（代码开发 Agent）
日期：2026-09-12
"""

import io
import os
import sys
import unittest

# ── 必须在 import ue5_bridge 之前设置环境变量（对齐 test_pywrap_l1.py） ──
os.environ["UE5_BRIDGE_TEST_MODE"] = "1"
os.environ["UE5_BRIDGE_TOKEN"] = "integration-test-token"

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(SKILL_ROOT, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import ue5_bridge            # noqa: E402  真实模块（测试模式 → fake_unreal 注入）
import fake_unreal as fu     # noqa: E402  test double
from fastapi.testclient import TestClient  # noqa: E402

BP_PATH = "/Game/Test/BP_PyWrapL2"
FN_GRAPH = "BRFuncL2"

L2_COMMANDS = [
    # 引脚 / 属性
    "add_pin_to_node", "remove_pin_from_node", "try_node_add_input_pin",
    "set_node_property",
    # 节点构造
    "add_cast_node", "add_class_cast_node", "add_macro_node",
    "add_switch_node", "add_struct_node", "add_enum_literal_node",
    "add_composite_node", "add_math_expression_node", "add_async_action_node",
    "add_create_delegate_node", "add_actor_bound_event_node",
    "add_input_axis_event_node", "add_input_action_event_node",
    "add_component_bound_event_node",
    # 蓝图 / 委托
    "remove_component", "add_implemented_interface", "add_event_dispatcher",
]

HANDLER_MAP = {
    "add_pin_to_node": "_cmd_add_pin_to_node",
    "remove_pin_from_node": "_cmd_remove_pin_from_node",
    "try_node_add_input_pin": "_cmd_try_node_add_input_pin",
    "set_node_property": "_cmd_set_node_property",
    "add_cast_node": "_cmd_add_cast_node",
    "add_class_cast_node": "_cmd_add_class_cast_node",
    "add_macro_node": "_cmd_add_macro_node",
    "add_switch_node": "_cmd_add_switch_node",
    "add_struct_node": "_cmd_add_struct_node",
    "add_enum_literal_node": "_cmd_add_enum_literal_node",
    "add_composite_node": "_cmd_add_composite_node",
    "add_math_expression_node": "_cmd_add_math_expression_node",
    "add_async_action_node": "_cmd_add_async_action_node",
    "add_create_delegate_node": "_cmd_add_create_delegate_node",
    "add_actor_bound_event_node": "_cmd_add_actor_bound_event_node",
    "add_input_axis_event_node": "_cmd_add_input_axis_event_node",
    "add_input_action_event_node": "_cmd_add_input_action_event_node",
    "add_component_bound_event_node": "_cmd_add_component_bound_event_node",
    "remove_component": "_cmd_remove_component",
    "add_implemented_interface": "_cmd_add_implemented_interface",
    "add_event_dispatcher": "_cmd_add_event_dispatcher",
}


class _Base(unittest.TestCase):
    """公共夹具：fake 资产 + 双图（EventGraph + 函数图）+ TestClient。"""

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

    def _mk(self, name, **params):
        """建节点 → 返回 node_id（供引脚/属性类命令使用）。"""
        res = self._ok(name, **params)
        self.assertTrue(res.get("node_id"), res)
        return res["node_id"]

    def _plain_node(self, graph="EventGraph"):
        return self._mk("create_node_by_class", bp_path=BP_PATH,
                        graph_name=graph, node_class="K2Node_IfThenElse",
                        node_pos=[100, 200])

    def _switch_node(self, graph="EventGraph"):
        return self._mk("add_switch_node", bp_path=BP_PATH, graph_name=graph,
                        type_kind="int", node_pos=[300, 400])


# ══════════════════════════════════════════════════════════
# ① 白名单 ↔ handler 双侧同步（21 项全量）
# ══════════════════════════════════════════════════════════

class WhitelistSyncTests(_Base):

    def test_all_l2_commands_registered(self):
        for cmd in L2_COMMANDS:
            self.assertIn(cmd, ue5_bridge._COMMAND_WHITELIST, cmd)

    def test_every_l2_command_has_existing_handler(self):
        for cmd, hname in HANDLER_MAP.items():
            fn = getattr(ue5_bridge, hname, None)
            self.assertIsNotNone(fn, f"{hname} 缺失（白名单已有 {cmd}）")
            self.assertTrue(callable(fn), hname)

    def test_no_orphan_l2_handler(self):
        """反向：21 个 handler 必须都被白名单 lambda 引用（漏白名单=Unknown）。"""
        src = io.open(ue5_bridge.__file__, encoding="utf-8").read()
        i = src.index("_COMMAND_WHITELIST = {")
        j = src.index('@app.post("/command"')
        wl_src = src[i:j]
        for cmd, hname in HANDLER_MAP.items():
            self.assertIn(hname, wl_src, f"{hname} 未挂到白名单（{cmd}）")
            names = ue5_bridge._COMMAND_WHITELIST[cmd].__code__.co_names
            self.assertIn(hname, names, f"{cmd} 的 lambda 未引用 {hname}")

    def test_whitelist_total_50(self):
        """L1 后 26 + L2 新增 21 = 47 + save_asset（T-20260913-UE5BRIDGE-SAVE-ASSET）
        + delete_asset（T-20260913-UE5BRIDGE-DELETE-COMPILE-FIX · D-1）
        + unpin_console_refs（T-20260914-UE5BRIDGE-BUILD-HOLD-FIX · 修法3）
        = 50（实测枚举报数）。"""
        # v3.0：50 → 72（+PIE×3 + Actor×4 + logs + batch + 材质9 + UMG×4）
        self.assertEqual(len(ue5_bridge._COMMAND_WHITELIST), 72)

    def test_l2_commands_are_l1_disjoint(self):
        """L2 与 L1 无交集（防重复登记覆盖）。"""
        l1 = ["create_node_by_class", "set_node_position", "get_node_by_guid",
              "add_variable_node", "add_custom_event_node",
              "add_input_key_event_node", "add_function_pin",
              "add_local_variable", "add_timeline_node", "add_comment_node",
              "set_class_default"]
        self.assertEqual(sorted(set(l1) & set(L2_COMMANDS)), [])

    def test_docstring_lists_l2_commands(self):
        doc = ue5_bridge.execute_command.__doc__ or ""
        for cmd in L2_COMMANDS:
            self.assertIn(cmd, doc, cmd)
        # v2.20：条数断言改为动态取实际白名单长度（旧断言硬编码
        #   "49 条"，白名单已扩到 50 条时测试反而报错）——
        #   同时验证 docstring 计数与 _COMMAND_WHITELIST 不脱同。
        self.assertIn(("%d \u6761" % len(ue5_bridge._COMMAND_WHITELIST)), doc)

    def test_unknown_command_still_rejected(self):
        resp = self._cmd("not_a_real_l2_command", bp_path=BP_PATH)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Unknown command", resp.json()["detail"])


# ══════════════════════════════════════════════════════════
# ② B1 · 引脚 / 属性（4 项）· 正路径 + fail-loud
# ══════════════════════════════════════════════════════════

class B1PinPropertyTests(_Base):

    def test_add_pin_to_node_happy(self):
        nid = self._plain_node()
        res = self._ok("add_pin_to_node", bp_path=BP_PATH,
                       graph_name="EventGraph", node_guid=nid,
                       direction="input", pin_type="bool", pin_name="NewPin")
        self.assertEqual(res["out_pin_name"], "NewPin")
        self.assertIn("NewPin", res["pins"])
        self.assertEqual(res["direction"], "input")

    def test_add_pin_to_node_duplicate_fail_loud(self):
        """重名幂等语义：同名引脚已存在 → 第二次必失败（禁静默双建）。"""
        nid = self._plain_node()
        self._ok("add_pin_to_node", bp_path=BP_PATH, graph_name="EventGraph",
                 node_guid=nid, direction="input", pin_type="bool",
                 pin_name="Dup")
        err = self._fail("add_pin_to_node", bp_path=BP_PATH,
                         graph_name="EventGraph", node_guid=nid,
                         direction="input", pin_type="bool", pin_name="Dup")
        self.assertIn("禁静默双建", err)

    def test_add_pin_to_node_dll_fail_loud(self):
        nid = self._plain_node()
        fu.set_l2_fail_pin_add(True)
        self._fail("add_pin_to_node", bp_path=BP_PATH, graph_name="EventGraph",
                   node_guid=nid, direction="output", pin_type="float",
                   pin_name="P")

    def test_add_pin_to_node_bad_direction_fail_loud(self):
        nid = self._plain_node()
        err = self._fail("add_pin_to_node", bp_path=BP_PATH,
                         graph_name="EventGraph", node_guid=nid,
                         direction="sideways", pin_type="bool", pin_name="P")
        self.assertIn("direction", err)

    def test_remove_pin_from_node_happy(self):
        nid = self._plain_node()
        res = self._ok("remove_pin_from_node", bp_path=BP_PATH,
                       graph_name="EventGraph", node_guid=nid,
                       pin_name="Condition")
        self.assertTrue(res["removed"])
        self.assertNotIn("Condition", res["pins"])

    def test_remove_pin_from_node_unknown_fail_loud(self):
        nid = self._plain_node()
        err = self._fail("remove_pin_from_node", bp_path=BP_PATH,
                         graph_name="EventGraph", node_guid=nid,
                         pin_name="NotARealPin")
        self.assertIn("NotARealPin", err)

    def test_try_node_add_input_pin_happy(self):
        nid = self._switch_node()
        res = self._ok("try_node_add_input_pin", bp_path=BP_PATH,
                       graph_name="EventGraph", node_guid=nid)
        self.assertTrue(res["added"])
        self.assertGreater(res["pin_count"], res["before_count"])

    def test_try_node_add_input_pin_unsupported_fail_loud(self):
        nid = self._plain_node()   # IfThenElse 未实现 AddPin 接口
        err = self._fail("try_node_add_input_pin", bp_path=BP_PATH,
                         graph_name="EventGraph", node_guid=nid)
        self.assertIn("IK2Node_AddPinInterface", err)

    def test_try_node_add_input_pin_strict_noop_fail_loud(self):
        nid = self._switch_node()
        fu.set_l2_try_add_pin_noop(True)
        err = self._fail("try_node_add_input_pin", bp_path=BP_PATH,
                         graph_name="EventGraph", node_guid=nid)
        self.assertIn("引脚数未增加", err)

    def test_try_node_add_input_pin_non_strict_warns(self):
        nid = self._switch_node()
        fu.set_l2_try_add_pin_noop(True)
        res = self._ok("try_node_add_input_pin", bp_path=BP_PATH,
                       graph_name="EventGraph", node_guid=nid, strict=False)
        self.assertFalse(res["added"])
        self.assertTrue(any("strict=false" in w for w in res["warnings"]))

    def test_set_node_property_happy(self):
        nid = self._plain_node()
        res = self._ok("set_node_property", bp_path=BP_PATH,
                       graph_name="EventGraph", node_guid=nid,
                       property_name="EnabledState", value="Disabled")
        self.assertTrue(res["matched"])
        self.assertEqual(str(res["readback"]), "Disabled")

    def test_set_node_property_dll_fail_loud(self):
        nid = self._plain_node()
        fu.set_l2_fail_node_prop(True)
        self._fail("set_node_property", bp_path=BP_PATH,
                   graph_name="EventGraph", node_guid=nid,
                   property_name="EnabledState", value="Disabled")

    def test_set_node_property_readback_mismatch_fail_loud(self):
        """DLL 返 True 但值未落盘 → 回读不一致 → fail-loud（杜绝假阳性）。"""
        nid = self._plain_node()
        fu.set_l2_drop_node_prop(True)
        err = self._fail("set_node_property", bp_path=BP_PATH,
                         graph_name="EventGraph", node_guid=nid,
                         property_name="EnabledState", value="Disabled")
        self.assertIn("回读不一致", err)


# ══════════════════════════════════════════════════════════
# ③ B2 · 节点构造 7 项 · 正路径 + fail-loud
# ══════════════════════════════════════════════════════════

class B2NodeCtorTests(_Base):

    def test_add_cast_node_happy(self):
        res = self._ok("add_cast_node", bp_path=BP_PATH,
                       graph_name="EventGraph",
                       target_class_name="/Script/Engine.Actor",
                       node_pos=[10, 20])
        self.assertTrue(res["node_id"])
        self.assertEqual(res["title"], "Cast To Actor")
        self.assertIn("AsObject", res["pins"])

    def test_add_cast_node_empty_fail_loud(self):
        err = self._fail("add_cast_node", bp_path=BP_PATH,
                         graph_name="EventGraph", target_class_name="")
        self.assertIn("target_class_name", err)

    def test_add_cast_node_ctor_none_fail_loud(self):
        fu.set_l2_fail_node_ctor("add_cast_node")
        self._fail("add_cast_node", bp_path=BP_PATH, graph_name="EventGraph",
                   target_class_name="/Script/Engine.Actor")

    def test_add_class_cast_node_happy(self):
        res = self._ok("add_class_cast_node", bp_path=BP_PATH,
                       graph_name="EventGraph",
                       target_class_name="/Script/Engine.Actor",
                       node_pos=[10, 20])
        self.assertTrue(res["node_id"])
        self.assertIn("Class", res["title"])

    def test_add_class_cast_node_empty_fail_loud(self):
        self._fail("add_class_cast_node", bp_path=BP_PATH,
                   graph_name="EventGraph", target_class_name="")

    def test_add_macro_node_happy(self):
        res = self._ok("add_macro_node", bp_path=BP_PATH,
                       graph_name="EventGraph", macro_name="ForEachLoop",
                       node_pos=[10, 20])
        self.assertTrue(res["node_id"])
        self.assertEqual(res["title"], "ForEachLoop")
        self.assertIn("LoopBody", res["pins"])

    def test_add_macro_node_unknown_fail_loud(self):
        """未知宏名 → C++ nullptr → 统一清理出口 fail-loud。"""
        err = self._fail("add_macro_node", bp_path=BP_PATH,
                         graph_name="EventGraph", macro_name="NoSuchMacro")
        self.assertIn("AddMacroNode", err)

    def test_add_switch_node_happy(self):
        res = self._ok("add_switch_node", bp_path=BP_PATH,
                       graph_name="EventGraph", type_kind="int",
                       node_pos=[10, 20])
        self.assertTrue(res["node_id"])
        self.assertEqual(res["title"], "Switch on Int")

    def test_add_switch_node_enum_happy(self):
        res = self._ok("add_switch_node", bp_path=BP_PATH,
                       graph_name="EventGraph", type_kind="enum",
                       enum_or_type_name="EMyEnum", node_pos=[10, 20])
        self.assertEqual(res["title"], "Switch on EMyEnum")

    def test_add_switch_node_bad_kind_fail_loud(self):
        err = self._fail("add_switch_node", bp_path=BP_PATH,
                         graph_name="EventGraph", type_kind="bogus")
        self.assertIn("type_kind", err)

    def test_add_switch_node_enum_missing_name_fail_loud(self):
        err = self._fail("add_switch_node", bp_path=BP_PATH,
                         graph_name="EventGraph", type_kind="enum",
                         enum_or_type_name="")
        self.assertIn("enum_or_type_name", err)

    def test_add_struct_node_break_happy(self):
        res = self._ok("add_struct_node", bp_path=BP_PATH,
                       graph_name="EventGraph", struct_name="Vector",
                       b_is_break=True, node_pos=[10, 20])
        self.assertEqual(res["title"], "Break Vector")

    def test_add_struct_node_make_happy(self):
        res = self._ok("add_struct_node", bp_path=BP_PATH,
                       graph_name="EventGraph", struct_name="Vector",
                       b_is_break=False, node_pos=[10, 20])
        self.assertEqual(res["title"], "Make Vector")

    def test_add_struct_node_empty_fail_loud(self):
        self._fail("add_struct_node", bp_path=BP_PATH,
                   graph_name="EventGraph", struct_name="")

    def test_add_enum_literal_node_happy(self):
        res = self._ok("add_enum_literal_node", bp_path=BP_PATH,
                       graph_name="EventGraph", enum_name="EMyEnum",
                       value_name="NewEnumerator0", node_pos=[10, 20])
        self.assertEqual(res["title"], "EMyEnum")

    def test_add_enum_literal_node_empty_fail_loud(self):
        self._fail("add_enum_literal_node", bp_path=BP_PATH,
                   graph_name="EventGraph", enum_name="")

    def test_add_composite_node_happy(self):
        res = self._ok("add_composite_node", bp_path=BP_PATH,
                       graph_name="EventGraph", node_pos=[10, 20])
        self.assertEqual(res["title"], "Composite")

    def test_add_composite_node_ctor_none_fail_loud(self):
        fu.set_l2_fail_node_ctor("add_composite_node")
        self._fail("add_composite_node", bp_path=BP_PATH,
                   graph_name="EventGraph", node_pos=[10, 20])

    def test_ctor_failure_leaves_no_residue(self):
        """建节点失败 → 统一清理出口：图内节点数不得增加（防 ghost 残留）。"""
        before = len(fu.BlueprintPythonBridge.list_nodes(
            self.bp.get_event_graph()))
        fu.set_l2_fail_node_ctor("add_macro_node")
        self._fail("add_macro_node", bp_path=BP_PATH,
                   graph_name="EventGraph", macro_name="ForEachLoop")
        after = len(fu.BlueprintPythonBridge.list_nodes(
            self.bp.get_event_graph()))
        self.assertEqual(before, after)


# ══════════════════════════════════════════════════════════
# ④ B3 · 节点构造 7 项 · 正路径 + fail-loud
# ══════════════════════════════════════════════════════════

class B3NodeCtorTests(_Base):

    def test_add_math_expression_node_happy(self):
        res = self._ok("add_math_expression_node", bp_path=BP_PATH,
                       graph_name="EventGraph", expression="(A+B)*C",
                       node_pos=[10, 20])
        self.assertTrue(res["node_id"])
        self.assertIn("(A+B)*C", res["title"])

    def test_add_math_expression_node_empty_fail_loud(self):
        self._fail("add_math_expression_node", bp_path=BP_PATH,
                   graph_name="EventGraph", expression="")

    def test_add_async_action_node_happy(self):
        res = self._ok("add_async_action_node", bp_path=BP_PATH,
                       graph_name="EventGraph",
                       factory_function_name="Delay",
                       factory_class_name="/Script/Engine.KismetSystemLibrary",
                       activate_function_name="", node_pos=[10, 20])
        self.assertTrue(res["node_id"])
        self.assertIn("Async Delay", res["title"])

    def test_add_async_action_node_missing_class_fail_loud(self):
        err = self._fail("add_async_action_node", bp_path=BP_PATH,
                         graph_name="EventGraph",
                         factory_function_name="Delay",
                         factory_class_name="")
        self.assertIn("factory_class_name", err)

    def test_add_create_delegate_node_happy(self):
        res = self._ok("add_create_delegate_node", bp_path=BP_PATH,
                       graph_name="EventGraph", function_name="OnDone",
                       node_pos=[10, 20])
        self.assertIn("OnDone", res["title"])

    def test_add_create_delegate_node_empty_fail_loud(self):
        self._fail("add_create_delegate_node", bp_path=BP_PATH,
                   graph_name="EventGraph", function_name="")

    def test_add_actor_bound_event_node_happy(self):
        res = self._ok("add_actor_bound_event_node", bp_path=BP_PATH,
                       graph_name="EventGraph", actor_name="BP_Chair_1",
                       delegate_name="OnClicked", node_pos=[10, 20])
        self.assertTrue(res["node_id"])
        self.assertIn("OnClicked", res["title"])

    def test_add_actor_bound_event_node_empty_delegate_fail_loud(self):
        self._fail("add_actor_bound_event_node", bp_path=BP_PATH,
                   graph_name="EventGraph", actor_name="BP_Chair_1",
                   delegate_name="")

    def test_add_input_axis_event_node_happy(self):
        res = self._ok("add_input_axis_event_node", bp_path=BP_PATH,
                       graph_name="EventGraph", axis_name="MoveForward",
                       b_is_key_axis=False, node_pos=[10, 20])
        self.assertIn("MoveForward", res["title"])

    def test_add_input_axis_event_node_key_axis_happy(self):
        res = self._ok("add_input_axis_event_node", bp_path=BP_PATH,
                       graph_name="EventGraph", axis_name="W",
                       b_is_key_axis=True, node_pos=[10, 20])
        infos = fu.BlueprintPythonBridge.list_nodes(
            self.bp.get_event_graph())
        classes = [getattr(n, "node_class", "") for n in infos]
        self.assertIn("K2Node_InputAxisKeyEvent", classes)

    def test_add_input_axis_event_node_empty_fail_loud(self):
        self._fail("add_input_axis_event_node", bp_path=BP_PATH,
                   graph_name="EventGraph", axis_name="")

    def test_add_input_action_event_node_happy(self):
        res = self._ok("add_input_action_event_node", bp_path=BP_PATH,
                       graph_name="EventGraph", action_name="Jump",
                       node_pos=[10, 20])
        self.assertIn("Jump", res["title"])

    def test_add_input_action_event_node_empty_fail_loud(self):
        self._fail("add_input_action_event_node", bp_path=BP_PATH,
                   graph_name="EventGraph", action_name="")

    def test_add_component_bound_event_node_happy(self):
        self._ok("add_component", bp_path=BP_PATH, comp_name="MyComp",
                 comp_class="/Script/Engine.StaticMeshComponent")
        res = self._ok("add_component_bound_event_node", bp_path=BP_PATH,
                       graph_name="EventGraph", component_name="MyComp",
                       delegate_name="OnClicked", node_pos=[10, 20])
        self.assertTrue(res["node_id"])
        self.assertIn("MyComp", res["title"])

    def test_add_component_bound_event_node_scs_missing_fail_loud(self):
        """SCS 未加组件（D-1 家族）→ nullptr → fail-loud，不把断言留给调用方。"""
        err = self._fail("add_component_bound_event_node", bp_path=BP_PATH,
                         graph_name="EventGraph", component_name="Ghost",
                         delegate_name="OnClicked", node_pos=[10, 20])
        self.assertIn("AddComponentBoundEventNode", err)

    def test_ctor_guid_resolvable_via_get_node_by_guid(self):
        """建节点后回读契约：node_id 必须能经 get_node_by_guid 命中（M-2）。"""
        res = self._ok("add_cast_node", bp_path=BP_PATH,
                       graph_name="EventGraph",
                       target_class_name="/Script/Engine.Actor",
                       node_pos=[77, 88])
        got = self._ok("get_node_by_guid", bp_path=BP_PATH,
                       graph_name="EventGraph", node_guid=res["node_id"])
        self.assertTrue(got.get("node_id") or got.get("guid") or got)

    def test_read_nodes_guid_normalized_and_chainable(self):
        """C-① 在线缺口回归守卫（T-20260912-...-L2-ONLINE）。

        修复前 `_read_l2_nodes` 用 `str(BPNodeInfo.node_guid)`：真实 UE 上得到
        `<Struct 'Guid' (0x内存地址) {}>`（内存地址 repr），既不是 32 位 hex，
        也无法喂给 get_node_by_guid → 便利通道 read_nodes → get_node_by_guid 断链。
        本用例锁定：guid 必须归一化为 32 位 hex，且可直接直通 get_node_by_guid。
        """
        self._ok("add_cast_node", bp_path=BP_PATH, graph_name="EventGraph",
                 target_class_name="/Script/Engine.Actor", node_pos=[3, 4])
        res = self._ok("read_nodes", bp_path=BP_PATH)
        guids = [n.get("guid") or ""
                 for g in res.get("graphs", []) for n in g.get("nodes", [])]
        self.assertTrue(guids, "read_nodes 应至少回读 1 个节点")
        for gid in guids:
            self.assertEqual(len(gid), 32, f"guid 非 32 位 hex: {gid!r}")
            self.assertTrue(all(c in "0123456789ABCDEF" for c in gid.upper()),
                            f"guid 含非 hex 字符: {gid!r}")
        # 直通验证：read_nodes 的 guid 必须能直接被 get_node_by_guid 命中
        got = self._ok("get_node_by_guid", bp_path=BP_PATH,
                       graph_name="EventGraph", node_guid=guids[0])
        self.assertTrue(got)


# ══════════════════════════════════════════════════════════
# ⑤ B4 · 蓝图 / 委托 3 项 · 正路径 + fail-loud
# ══════════════════════════════════════════════════════════

class B4BlueprintDelegateTests(_Base):

    def test_remove_component_happy(self):
        self._ok("add_component", bp_path=BP_PATH, comp_name="ToRemove",
                 comp_class="/Script/Engine.StaticMeshComponent")
        res = self._ok("remove_component", bp_path=BP_PATH,
                       comp_name="ToRemove")
        self.assertTrue(res["removed"])
        self.assertNotIn("ToRemove", res["components"])

    def test_remove_component_missing_fail_loud(self):
        err = self._fail("remove_component", bp_path=BP_PATH,
                         comp_name="NotThere")
        self.assertIn("NotThere", err)

    def test_remove_component_empty_fail_loud(self):
        self._fail("remove_component", bp_path=BP_PATH, comp_name="")

    def test_add_implemented_interface_happy(self):
        res = self._ok("add_implemented_interface", bp_path=BP_PATH,
                       interface_class_path="/Game/Test/BPI_Foo.BPI_Foo_C")
        self.assertTrue(res["readback"])
        self.assertIn("BPI_Foo", res["interfaces"])

    def test_add_implemented_interface_duplicate_fail_loud(self):
        self._ok("add_implemented_interface", bp_path=BP_PATH,
                 interface_class_path="/Game/Test/BPI_Foo.BPI_Foo_C")
        self._fail("add_implemented_interface", bp_path=BP_PATH,
                   interface_class_path="/Game/Test/BPI_Foo.BPI_Foo_C")

    def test_add_implemented_interface_empty_fail_loud(self):
        self._fail("add_implemented_interface", bp_path=BP_PATH,
                   interface_class_path="")

    def test_add_event_dispatcher_happy(self):
        res = self._ok("add_event_dispatcher", bp_path=BP_PATH,
                       delegate_name="OnMyEvent")
        self.assertTrue(res["readback"])
        self.assertIn("OnMyEvent", res["delegate_graphs"])

    def test_add_event_dispatcher_duplicate_fail_loud(self):
        """重名语义：同名分发器已存在 → C++ false → None → fail-loud。"""
        self._ok("add_event_dispatcher", bp_path=BP_PATH,
                 delegate_name="OnMyEvent")
        self._fail("add_event_dispatcher", bp_path=BP_PATH,
                   delegate_name="OnMyEvent")

    def test_add_event_dispatcher_empty_fail_loud(self):
        self._fail("add_event_dispatcher", bp_path=BP_PATH, delegate_name="")
