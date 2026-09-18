#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_pywrap_l1.py — PATCH-A/B 新接口 L1 Python 封装离线单测
================================================================
T-20260910-UE5BRIDGE-PYWRAP-L1-IMPL（S1 纯 Python）

覆盖 11 条白名单命令（= 评估单 §3.1 的 L1 十项，L1-7 拆 2 条 API）：
    create_node_by_class / set_node_position / get_node_by_guid /
    add_variable_node / add_custom_event_node / add_input_key_event_node /
    add_function_pin / add_local_variable / add_timeline_node /
    add_comment_node / set_class_default

测试层级：集成测试（真实启动 ue5_bridge.py + fake_unreal 替身 + 真实 HTTP），
          不依赖 UE（不需要真实 DLL / 编辑器）。

三类断言：
  ① 白名单 ↔ handler 双侧同步（漏一侧 = Unknown command）
  ② Happy path：真实返回结构（node_id / pins / readback）
  ③ fail-loud：底层 None/False → success=false（**杜绝假阳性 success**），
     含故障注入（变量引脚丢弃 / timeline 引脚不足 / 注释文本丢失 D-2）

作者：dev（代码开发 Agent）
日期：2026-09-10
"""

import io
import os
import sys
import unittest
from unittest import mock

# ── 必须在 import ue5_bridge 之前设置环境变量（对齐 test_add_component_cpp.py） ──
os.environ["UE5_BRIDGE_TEST_MODE"] = "1"
os.environ["UE5_BRIDGE_TOKEN"] = "integration-test-token"

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(SKILL_ROOT, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import ue5_bridge            # noqa: E402  真实模块（测试模式 → fake_unreal 注入）
import fake_unreal as fu     # noqa: E402  test double
from fastapi.testclient import TestClient  # noqa: E402

BP_PATH = "/Game/Test/BP_PyWrap"
FN_GRAPH = "BRFunc"

NEW_COMMANDS = [
    "create_node_by_class", "set_node_position", "get_node_by_guid",
    "add_variable_node", "add_custom_event_node", "add_input_key_event_node",
    "add_function_pin", "add_local_variable", "add_timeline_node",
    "add_comment_node", "set_class_default",
    "delete_asset",  # T-20260913-UE5BRIDGE-DELETE-COMPILE-FIX · D-1
]

HANDLER_MAP = {
    "create_node_by_class": "_cmd_create_node_by_class",
    "set_node_position": "_cmd_set_node_position",
    "get_node_by_guid": "_cmd_get_node_by_guid",
    "add_variable_node": "_cmd_add_variable_node",
    "add_custom_event_node": "_cmd_add_custom_event_node",
    "add_input_key_event_node": "_cmd_add_input_key_event_node",
    "add_function_pin": "_cmd_add_function_pin",
    "add_local_variable": "_cmd_add_local_variable",
    "add_timeline_node": "_cmd_add_timeline_node",
    "add_comment_node": "_cmd_add_comment_node",
    "set_class_default": "_cmd_set_class_default",
    "delete_asset": "_cmd_delete_asset",
}


class _Base(unittest.TestCase):
    """公共夹具：fake 资产 + 双图（EventGraph + 函数图 BRFunc）+ TestClient。"""

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

    def _new_node(self, cls="K2Node_IfThenElse", graph="EventGraph"):
        return self._ok("create_node_by_class", bp_path=BP_PATH,
                        graph_name=graph, node_class=cls, node_pos=[100, 200])


# ══════════════════════════════════════════════════════════
# ① 白名单 ↔ handler 双侧同步
# ══════════════════════════════════════════════════════════

class WhitelistSyncTests(_Base):

    def test_all_new_commands_registered(self):
        for cmd in NEW_COMMANDS:
            self.assertIn(cmd, ue5_bridge._COMMAND_WHITELIST, cmd)

    def test_every_new_command_has_existing_handler(self):
        """白名单每条 → handler 真实存在（漏 handler = 调用 NameError）。"""
        for cmd, hname in HANDLER_MAP.items():
            fn = getattr(ue5_bridge, hname, None)
            self.assertIsNotNone(fn, f"{hname} 缺失（白名单已有 {cmd}）")
            self.assertTrue(callable(fn), hname)

    def test_no_orphan_handler(self):
        """反向校验：11 个新 handler 必须都在白名单里被引用（漏白名单 = Unknown command）。

        双重校验：① 源码级 grep（白名单块文本含 handler 名）
                  ② 运行时级：lambda 的 co_names 必须解析到真实 handler
        """
        src = io.open(ue5_bridge.__file__, encoding="utf-8").read()
        i = src.index("_COMMAND_WHITELIST = {")
        j = src.index('@app.post("/command"')
        wl_src = src[i:j]
        for cmd, hname in HANDLER_MAP.items():
            self.assertIn(hname, wl_src, f"{hname} 未挂到白名单（{cmd}）")
            names = ue5_bridge._COMMAND_WHITELIST[cmd].__code__.co_names
            self.assertIn(hname, names, f"{cmd} 的 lambda 未引用 {hname}")

    def test_whitelist_total_50(self):
        # L2 全量封装（T-20260912-UE5BRIDGE-PYWRAP-L2-IMPL）后 26 + 21 = 47 条
        # + save_asset（T-20260913-UE5BRIDGE-SAVE-ASSET）= 48 条
        # + delete_asset（T-20260913-UE5BRIDGE-DELETE-COMPILE-FIX · D-1）= 49 条
        # + unpin_console_refs（T-20260914-UE5BRIDGE-BUILD-HOLD-FIX · 修法3）= 50 条
        # v3.0：50 → 72（+PIE×3 + Actor×4 + logs + batch + 材质9 + UMG×4）
        self.assertEqual(len(ue5_bridge._COMMAND_WHITELIST), 72)

    def test_docstring_lists_new_commands(self):
        doc = ue5_bridge.execute_command.__doc__ or ""
        for cmd in NEW_COMMANDS:
            self.assertIn(cmd, doc, cmd)

    def test_unknown_command_still_rejected(self):
        resp = self._cmd("not_a_real_command", bp_path=BP_PATH)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Unknown command", resp.json()["detail"])


# ══════════════════════════════════════════════════════════
# ② Happy path（11 条命令逐条）
# ══════════════════════════════════════════════════════════

class HappyPathTests(_Base):

    def test_01_create_node_by_class(self):
        res = self._new_node()
        self.assertTrue(res["ok"])
        self.assertTrue(res["node_id"], "GUID 锚点不可为空")
        self.assertEqual(res["pos"], [100, 200])
        self.assertIn("Condition", res["pins"])
        self.assertEqual(res["graph"], "EventGraph")
        # fake 侧确证节点已落图
        self.assertEqual(len(self.bp.graphs["EventGraph"].get_nodes()), 1)

    def test_01b_create_node_full_path_class(self):
        res = self._ok("create_node_by_class", bp_path=BP_PATH,
                       node_class="/Script/BlueprintGraph.K2Node_CallFunction")
        self.assertTrue(res["node_id"])

    def test_02_set_node_position_readback(self):
        guid = self._new_node()["node_id"]
        res = self._ok("set_node_position", bp_path=BP_PATH,
                       node_guid=guid, x=640, y=320)
        self.assertEqual(res["pos"], [640, 320])
        self.assertEqual(res["readback"], [640, 320])

    def test_03_get_node_by_guid(self):
        guid = self._new_node()["node_id"]
        res = self._ok("get_node_by_guid", bp_path=BP_PATH, node_guid=guid)
        self.assertEqual(res["node_id"], guid)
        self.assertEqual(res["node_class"], "K2Node_IfThenElse")
        self.assertTrue(res["pins"])

    def test_04_add_variable_node(self):
        res = self._ok("add_variable_node", bp_path=BP_PATH, var_name="BRVar",
                       b_is_setter=True, node_pos=[0, 480])
        self.assertTrue(res["node_id"])
        self.assertIn("BRVar", res["pins"])
        self.assertIn("Output_Get", res["pins"])
        self.assertTrue(res["b_is_setter"])

    def test_04b_add_variable_node_getter(self):
        res = self._ok("add_variable_node", bp_path=BP_PATH, var_name="BRVar",
                       b_is_setter=False)
        self.assertFalse(res["b_is_setter"])
        self.assertIn("BRVar", res["pins"])

    def test_05_add_custom_event_node_title_readback(self):
        res = self._ok("add_custom_event_node", bp_path=BP_PATH,
                       event_name="BREvent1", node_pos=[0, 0])
        self.assertTrue(res["node_id"])
        self.assertIn("BREvent1", res["title"])
        self.assertTrue(res["pins"])

    def test_06_add_input_key_event_node(self):
        res = self._ok("add_input_key_event_node", bp_path=BP_PATH,
                       key_name="W", node_pos=[0, 240])
        self.assertTrue(res["node_id"])
        self.assertEqual(res["key_name"], "W")
        self.assertTrue(res["pins"])

    def test_07a_add_function_pin_readback(self):
        res = self._ok("add_function_pin", bp_path=BP_PATH,
                       graph_name=FN_GRAPH, pin_name="BRIn",
                       pin_type="int", b_is_input=True)
        self.assertTrue(res["ok"])
        self.assertTrue(res["readback"], res)
        self.assertEqual(res["graph"], FN_GRAPH)

    def test_07b_add_local_variable_readback(self):
        res = self._ok("add_local_variable", bp_path=BP_PATH,
                       graph_name=FN_GRAPH, var_name="BRLocal",
                       var_type="float")
        self.assertTrue(res["ok"])
        self.assertTrue(res["readback"], res)

    def test_08_add_timeline_node_10_pins(self):
        res = self._ok("add_timeline_node", bp_path=BP_PATH,
                       timeline_name="BRTimeline", node_pos=[0, 720])
        self.assertTrue(res["node_id"])
        self.assertEqual(res["pin_count"], 10, res["pins"])
        for pin in ("Play", "Update", "Finished", "Direction"):
            self.assertIn(pin, res["pins"])

    def test_09_add_comment_node_text_readback(self):
        res = self._ok("add_comment_node", bp_path=BP_PATH,
                       text="BR 注释", node_pos=[-200, -200],
                       width=500, height=300)
        self.assertTrue(res["node_id"])
        self.assertIn("BR 注释", res["title"])
        self.assertEqual(res["size"], [500, 300])

    def test_10_set_class_default_cdo_readback(self):
        res = self._ok("set_class_default", bp_path=BP_PATH,
                       property_name="InitialLifeSpan", value="123.0")
        self.assertTrue(res["matched"], res)
        self.assertEqual(res["readback"], "123.0")
        # fake 侧确证 CDO 已写入 snake_case 属性
        cdo = self.bp.generated_class().get_default_object()
        self.assertEqual(cdo.get_editor_property("initial_life_span"), "123.0")


# ══════════════════════════════════════════════════════════
# ③ fail-loud（禁止假阳性 success）
# ══════════════════════════════════════════════════════════

class FailLoudTests(_Base):

    def test_create_node_none_raises(self):
        with mock.patch.object(fu.BlueprintPythonBridge, "add_node_by_class",
                               return_value=None):
            err = self._fail("create_node_by_class", bp_path=BP_PATH,
                             node_class="K2Node_IfThenElse")
        self.assertIn("返回 None", err)

    def test_set_node_position_false_raises(self):
        guid = self._new_node()["node_id"]
        with mock.patch.object(fu.BlueprintPythonBridge, "set_node_position",
                               return_value=False):
            err = self._fail("set_node_position", bp_path=BP_PATH,
                             node_guid=guid, x=1, y=2)
        self.assertIn("SetNodePosition 返回", err)

    def test_set_node_position_bad_guid_raises(self):
        err = self._fail("set_node_position", bp_path=BP_PATH,
                         node_guid="DEADBEEFDEADBEEFDEADBEEFDEADBEEF",
                         x=1, y=2)
        self.assertIn("节点未找到", err)

    def test_get_node_by_guid_missing_raises(self):
        err = self._fail("get_node_by_guid", bp_path=BP_PATH,
                         node_guid="DEADBEEFDEADBEEFDEADBEEFDEADBEEF")
        self.assertIn("节点未找到", err)

    def test_add_variable_node_none_raises(self):
        with mock.patch.object(fu.BlueprintPythonBridge, "add_variable_node",
                               return_value=None):
            err = self._fail("add_variable_node", bp_path=BP_PATH,
                             var_name="BRVar")
        self.assertIn("返回 None", err)

    def test_add_variable_node_missing_pin_raises(self):
        """故障注入：变量不存在 → 静默空节点（E1 类）→ 必须 fail-loud。"""
        fu.set_variable_pins_drop(True)
        err = self._fail("add_variable_node", bp_path=BP_PATH, var_name="BRVar")
        self.assertIn("缺失变量引脚", err)

    def test_add_custom_event_none_raises(self):
        with mock.patch.object(fu.BlueprintPythonBridge, "add_custom_event_node",
                               return_value=None):
            err = self._fail("add_custom_event_node", bp_path=BP_PATH,
                             event_name="E")
        self.assertIn("返回 None", err)

    def test_add_input_key_event_none_raises(self):
        with mock.patch.object(
                fu.BlueprintPythonBridge, "add_input_key_event_node",
                return_value=None):
            err = self._fail("add_input_key_event_node", bp_path=BP_PATH,
                             key_name="NotAKey")
        self.assertIn("返回 None", err)

    def test_add_timeline_none_raises(self):
        with mock.patch.object(fu.BlueprintPythonBridge, "add_timeline_node",
                               return_value=None):
            err = self._fail("add_timeline_node", bp_path=BP_PATH,
                             timeline_name="T")
        self.assertIn("返回 None", err)

    def test_add_timeline_insufficient_pins_raises(self):
        """故障注入：timeline 引脚不足 → fail-loud（不放过退化节点）。"""
        fu.set_timeline_pin_limit(2)
        err = self._fail("add_timeline_node", bp_path=BP_PATH,
                         timeline_name="T")
        self.assertIn("引脚数不足", err)

    def test_add_timeline_strict_pin_count(self):
        """契约要求 10 引脚：8 引脚 → strict_pins=True 直接 raise；=false 降级告警。"""
        fu.set_timeline_pin_limit(8)
        err = self._fail("add_timeline_node", bp_path=BP_PATH,
                         timeline_name="T")
        self.assertIn("契约要求 10", err)
        res = self._ok("add_timeline_node", bp_path=BP_PATH,
                       timeline_name="T", strict_pins=False)
        self.assertEqual(res["pin_count"], 8)
        self.assertTrue(any("strict_pins=false" in w for w in res["warnings"]),
                        res["warnings"])

    def test_add_comment_none_raises(self):
        with mock.patch.object(fu.BlueprintPythonBridge, "add_comment_node",
                               return_value=None):
            err = self._fail("add_comment_node", bp_path=BP_PATH, text="X")
        self.assertIn("返回 None", err)

    def test_add_comment_d2_text_drop_raises(self):
        """故障注入：复现 D-2（注释文本不写入）→ 标题回读校验必须 fail-loud。"""
        fu.set_comment_text_drop(True)
        err = self._fail("add_comment_node", bp_path=BP_PATH, text="BR 注释")
        self.assertIn("注释文本未写入", err)

    def test_set_class_default_false_raises(self):
        with mock.patch.object(fu.BlueprintPythonBridge, "set_class_default",
                               return_value=False):
            err = self._fail("set_class_default", bp_path=BP_PATH,
                             property_name="InitialLifeSpan", value="1.0")
        self.assertIn("SetClassDefault 返回", err)

    def test_add_function_pin_false_raises(self):
        with mock.patch.object(fu.BlueprintPythonBridge,
                               "add_function_pin_entry", return_value=False):
            err = self._fail("add_function_pin", bp_path=BP_PATH,
                             graph_name=FN_GRAPH, pin_name="P", pin_type="int")
        self.assertIn("AddFunctionPinEntry 返回", err)

    def test_add_local_variable_false_raises(self):
        with mock.patch.object(fu.BlueprintPythonBridge, "add_local_variable",
                               return_value=False):
            err = self._fail("add_local_variable", bp_path=BP_PATH,
                             graph_name=FN_GRAPH, var_name="L",
                             var_type="float")
        self.assertIn("AddLocalVariable 返回", err)

    def test_param_validation_empty_values(self):
        """参数空值 / 类型错误 → param_type 类 fail-loud（不打到 DLL）。"""
        self.assertIn("不可为空", self._fail("create_node_by_class",
                                             bp_path=BP_PATH, node_class=""))
        self.assertIn("不可为空", self._fail("add_comment_node",
                                             bp_path=BP_PATH, text=""))
        self.assertIn("必须为字符串", self._fail("get_node_by_guid",
                                              bp_path=BP_PATH, node_guid=123))

    def test_function_pin_requires_function_graph(self):
        """L1-7 硬约束：EventGraph 无 FunctionEntry → 参数校验直接拒绝。"""
        err = self._fail("add_function_pin", bp_path=BP_PATH,
                         graph_name="EventGraph", pin_name="P",
                         pin_type="int")
        self.assertIn("不支持函数参数", err)
        err2 = self._fail("add_local_variable", bp_path=BP_PATH,
                          graph_name="", var_name="L", var_type="float")
        self.assertIn("graph_name 必填", err2)

    def test_graph_not_found_includes_available(self):
        err = self._fail("create_node_by_class", bp_path=BP_PATH,
                         graph_name="NoSuchGraph",
                         node_class="K2Node_IfThenElse")
        self.assertIn("图表未找到", err)
        self.assertIn(FN_GRAPH, err)   # 错误信息含可用图名（可诊断）

    def test_blueprint_not_found_raises(self):
        err = self._fail("get_node_by_guid", bp_path="/Game/No/Such_BP",
                         node_guid="AABB")
        self.assertIn("蓝图不存在", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
