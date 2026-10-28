#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_titlegate_guid.py — T-20260912-UE5BRIDGE-TITLEGATE-FIX 离线回归
==========================================================================
主题：**同名节点定位：GUID 优先 → 歧义 fail-loud**（补齐到剩余全部标题定位调用点）

覆盖改造点：
  F-1  /connect 主路径          game_thread_connect.py.tmpl  — 同名歧义 fail-loud + 零连线
  F-2  /build 连接路径          game_thread_build.py.tmpl    — 同名歧义 fail-loud + 零连线
  F-3  /build 事件存在性判定    game_thread_build.py.tmpl    — 同名歧义 → 报错（不静默跳过）
  F-4  _find_graph_of_node      ue5_bridge.py               — 跨图同名 → fail-loud
  F-5  _pywrap_find_peer_in_graph / _delete_node_on_game_thread — 统一 strict 语义
  F-6  _pywrap_verify_pin_disconnected — 第 4 重校验统一走 BPNodeInfo（K2Node → TypeError）

设计要点（离线复现）：fake_unreal.FakeGraph 新增 `_all` 全量节点表 → 支持
「同 title 异 GUID」场景（旧桩标题字典互相覆盖，同名节点直接丢失，无法复现歧义）。

作者：dev（代码开发 Agent）  日期：2026-09-12
"""

import os
import sys
import json
import tempfile
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

BP_PATH = "/Game/Test/BP_TitleGate"


# ══════════════════════════════════════════════════════════
# 离线执行 GameThread 模板（unreal ← fake_unreal）
# ══════════════════════════════════════════════════════════

def _run_template(tmpl_name, spec):
    """渲染并离线执行模板（等价 GameThread 注入），返回结果 JSON dict。"""
    with open(os.path.join(SCRIPTS_DIR, tmpl_name), "r", encoding="utf-8") as f:
        code = f.read()
    tmpdir = tempfile.mkdtemp(prefix="titlegate_")
    spec_file = os.path.join(tmpdir, "spec.json")
    result_file = os.path.join(tmpdir, "result.json")
    with open(spec_file, "w", encoding="utf-8") as f:
        json.dump(spec, f, ensure_ascii=False)
    code = (code.replace("{SPEC_FILE}", repr(spec_file))
                .replace("{RESULT_FILE}", repr(result_file)))
    _prev = sys.modules.get("unreal")
    sys.modules["unreal"] = fu          # 模板内 `import unreal` → fake 替身
    try:
        exec(compile(code, tmpl_name, "exec"), {"__name__": "__titlegate__"})
    finally:
        if _prev is None:
            sys.modules.pop("unreal", None)
        else:
            sys.modules["unreal"] = _prev
    with open(result_file, "r", encoding="utf-8") as f:
        return json.load(f)


class _FakeInfo:
    """模拟真实 UE `list_nodes` 返回的 FBPNodeInfo（**不含** K2Node 方法）。

    F-6 用例用它区分「传 BPNodeInfo（合法）」与「传 K2Node（TypeError）」。
    """

    def __init__(self, node, pins):
        self.node_title = node.node_title
        self.node_guid = node.node_guid
        self.node_class = node.node_class
        self.pos_x = node.pos_x
        self.pos_y = node.pos_y
        self._pins = pins


class _FakePinInfo:
    def __init__(self, pin_name, is_connected):
        self.pin_name = pin_name
        self.is_connected = is_connected


class _Base(unittest.TestCase):

    def setUp(self):
        fu.reset_fake()
        self.bp = fu.create_test_blueprint(BP_PATH)
        self.graph = self.bp.graphs["EventGraph"]

    def _links(self):
        return fu.BlueprintPythonBridge.get_node_connections(self.graph)


# ══════════════════════════════════════════════════════════
# F-1 · /connect 模板
# ══════════════════════════════════════════════════════════

class F1ConnectTemplateTests(_Base):

    def test_f1_ambiguous_title_fail_loud_and_no_link(self):
        """同名（同 title 异 GUID）→ 必须 fail-loud，且**不产生任何连线**。"""
        n1 = fu.add_test_node(self.graph, "PrintString",
                              pins=[("then", "EGPD_Output", "exec")])
        fu.add_test_node(self.graph, "PrintString",
                         pins=[("then", "EGPD_Output", "exec")])
        fu.add_test_node(self.graph, "Target",
                         pins=[("execute", "EGPD_Input", "exec")])

        res = _run_template("game_thread_connect.py.tmpl", {
            "blueprint_path": BP_PATH,
            "connections": [{"src_node": "PrintString", "src_pin": "then",
                             "dst_node": "Target", "dst_pin": "execute"}],
        })

        self.assertFalse(res["success"], res)
        self.assertEqual(self._links(), [], "同名歧义下不得产生任何连线")
        err = res["results"][0]["error"]
        self.assertIn("歧义", err)
        self.assertIn(n1.node_guid, err, "错误信息须回传全部同名节点 GUID 清单")

    def test_f1_guid_input_targets_exact_node(self):
        """GUID 入参 → 同名场景下精确定位目标节点（不受同名干扰）。"""
        n1 = fu.add_test_node(self.graph, "PrintString",
                              pins=[("then", "EGPD_Output", "exec")])
        n2 = fu.add_test_node(self.graph, "PrintString",
                              pins=[("then", "EGPD_Output", "exec")])
        tgt = fu.add_test_node(self.graph, "Target",
                               pins=[("execute", "EGPD_Input", "exec")])

        res = _run_template("game_thread_connect.py.tmpl", {
            "blueprint_path": BP_PATH,
            "connections": [{"src_node": n2.node_guid, "src_pin": "then",
                             "dst_node": "Target", "dst_pin": "execute"}],
        })

        self.assertTrue(res["success"], res)
        self.assertEqual(res["results"][0]["status"], "connected")
        self.assertEqual(n1.find_pin("then").linked_to, [])
        self.assertEqual(n2.find_pin("then").linked_to, [tgt.find_pin("execute")])

    def test_f1_unique_title_still_works(self):
        """唯一标题 → 既有行为不变（不新增失败面）。"""
        fu.add_test_node(self.graph, "PrintString",
                         pins=[("then", "EGPD_Output", "exec")])
        fu.add_test_node(self.graph, "Target",
                         pins=[("execute", "EGPD_Input", "exec")])
        res = _run_template("game_thread_connect.py.tmpl", {
            "blueprint_path": BP_PATH,
            "connections": [{"src_node": "PrintString", "src_pin": "then",
                             "dst_node": "Target", "dst_pin": "execute"}],
        })
        self.assertTrue(res["success"], res)
        self.assertEqual(len(self._links()), 1)


# ══════════════════════════════════════════════════════════
# F-2 / F-3 · /build 模板
# ══════════════════════════════════════════════════════════

class F2BuildConnectTemplateTests(_Base):

    def test_f2_ambiguous_title_fail_loud_and_no_link(self):
        n1 = fu.add_test_node(self.graph, "PrintString",
                              pins=[("then", "EGPD_Output", "exec")])
        fu.add_test_node(self.graph, "PrintString",
                         pins=[("then", "EGPD_Output", "exec")])
        fu.add_test_node(self.graph, "Target",
                         pins=[("execute", "EGPD_Input", "exec")])

        res = _run_template("game_thread_build.py.tmpl", {
            "blueprint_path": BP_PATH,
            "events": [], "functions": [],
            "connections": [{"src_node": "PrintString", "src_pin": "then",
                             "dst_node": "Target", "dst_pin": "execute"}],
            "compile_after": False,
        })

        self.assertFalse(res["success"], res)
        self.assertEqual(self._links(), [], "同名歧义下不得产生任何连线")
        step = [s for s in res["steps"] if s["step"].startswith("connect:")][0]
        self.assertEqual(step["status"], "error")
        self.assertIn("歧义", step["error"])
        self.assertIn(n1.node_guid, step["error"])


class F3BuildExistenceTests(_Base):

    def test_f3_ambiguous_event_title_fail_loud_not_silent_skip(self):
        """F-3：同名事件标题 → 报错（**不静默跳过**、不误判「已存在」）。"""
        fu.add_test_node(self.graph, "事件开始运行", node_class="K2Node_Event")
        fu.add_test_node(self.graph, "事件开始运行", node_class="K2Node_Event")

        res = _run_template("game_thread_build.py.tmpl", {
            "blueprint_path": BP_PATH,
            "events": [{"event_name": "BeginPlay"}],
            "functions": [], "connections": [], "compile_after": False,
        })

        self.assertFalse(res["success"], res)
        st = [s for s in res["steps"] if s["step"].startswith("add_event:")][0]
        self.assertEqual(st["status"], "error")
        self.assertIn("歧义", st["error"])
        # 既没静默跳过、也没新增节点（节点数仍为 2）
        self.assertEqual(len(self.graph.get_nodes()), 2)

    def test_f3_unique_localized_event_still_reports_exists(self):
        """唯一命中（本地化标题）→ 仍按既有语义判定「已存在」，不重复创建。"""
        fu.add_test_node(self.graph, "事件开始运行", node_class="K2Node_Event")
        res = _run_template("game_thread_build.py.tmpl", {
            "blueprint_path": BP_PATH,
            "events": [{"event_name": "BeginPlay"}],
            "functions": [], "connections": [], "compile_after": False,
        })
        self.assertTrue(res["success"], res)
        st = [s for s in res["steps"] if s["step"].startswith("add_event:")][0]
        self.assertEqual(st["status"], "exists")
        self.assertEqual(len(self.graph.get_nodes()), 1)


# ══════════════════════════════════════════════════════════
# F-4 · _find_graph_of_node（跨图同名 → fail-loud）
# ══════════════════════════════════════════════════════════

class F4FindGraphOfNodeTests(_Base):

    def test_f4_cross_graph_same_title_fail_loud(self):
        g2 = fu.FakeGraph("FuncGraph")
        self.bp.add_graph(g2)
        n1 = fu.add_test_node(self.graph, "NodeX")
        n2 = fu.add_test_node(g2, "NodeX")
        with self.assertRaises(ue5_bridge.BridgeError) as cm:
            ue5_bridge._find_graph_of_node(self.bp, "NodeX")
        self.assertEqual(cm.exception.category, "node_ambiguous")
        self.assertIn("歧义", str(cm.exception))
        _msg = str(cm.exception)
        self.assertIn(ue5_bridge._pywrap_norm_guid(n1.node_guid), _msg)
        self.assertIn(ue5_bridge._pywrap_norm_guid(n2.node_guid), _msg)

    def test_f4_unique_title_returns_its_graph(self):
        fu.add_test_node(self.graph, "SoloNode")
        self.assertIs(ue5_bridge._find_graph_of_node(self.bp, "SoloNode"), self.graph)

    def test_f4_guid_input_returns_graph(self):
        n = fu.add_test_node(self.graph, "GuidNode")
        self.assertIs(ue5_bridge._find_graph_of_node(self.bp, n.node_guid), self.graph)

    def test_f4_missing_title_returns_none(self):
        self.assertIsNone(ue5_bridge._find_graph_of_node(self.bp, "NoSuchNode"))


# ══════════════════════════════════════════════════════════
# F-5 · 对端定位 / 删除路径统一 strict
# ══════════════════════════════════════════════════════════

class F5StrictPeerTests(_Base):

    def test_f5_peer_ambiguous_title_fail_loud(self):
        fu.add_test_node(self.graph, "Peer")
        fu.add_test_node(self.graph, "Peer")
        node, err = ue5_bridge._pywrap_find_peer_in_graph(self.graph, "Peer")
        self.assertIsNone(node)
        self.assertIn("歧义", err)

    def test_f5_peer_unique_title_returns_node(self):
        n = fu.add_test_node(self.graph, "Peer")
        node, err = ue5_bridge._pywrap_find_peer_in_graph(self.graph, "Peer")
        self.assertIs(node, n)
        self.assertEqual(err, "")

    def test_f5_peer_missing_returns_none_no_err(self):
        node, err = ue5_bridge._pywrap_find_peer_in_graph(self.graph, "Nope")
        self.assertIsNone(node)
        self.assertEqual(err, "")


# ══════════════════════════════════════════════════════════
# F-6 · 第 4 重校验统一走 BPNodeInfo
# ══════════════════════════════════════════════════════════

class F6VerifyPinDisconnectedTests(_Base):

    def test_f6_connected_returns_false_disconnected_returns_true(self):
        a = fu.add_test_node(self.graph, "A", pins=[("then", "EGPD_Output", "exec")])
        b = fu.add_test_node(self.graph, "B", pins=[("execute", "EGPD_Input", "exec")])
        fu.connect_pins_by_name(self.graph, "A", "then", "B", "execute")
        self.assertIs(
            ue5_bridge._pywrap_verify_pin_disconnected(self.graph, "A", "then"), False)
        a.find_pin("then").break_link_to(b.find_pin("execute"))
        self.assertIs(
            ue5_bridge._pywrap_verify_pin_disconnected(self.graph, "A", "then"), True)

    def test_f6_receives_bpnodeinfo_not_k2node(self):
        """断言第 4 重校验传的是 BPNodeInfo（K2Node 会 TypeError → 被旧代码吞掉）。"""
        a = fu.add_test_node(self.graph, "A", pins=[("then", "EGPD_Output", "exec")])
        infos = [_FakeInfo(a, [_FakePinInfo("then", False)])]

        seen = []

        def _list_nodes(graph):
            return list(infos)

        def _get_pin_infos(graph, node):
            seen.append(node)
            if isinstance(node, fu.FakeNode):
                # 真实 C++ 签名 GetPinInfos(UEdGraph*, const FBPNodeInfo&)：
                #   传 K2Node → TypeError（见 _cmd_set_pin_default_value 头注实测）
                raise TypeError("Cannot natively convert parameter 'node'")
            return list(getattr(node, "_pins", []))

        with mock.patch.object(fu.BlueprintPythonBridge, "list_nodes",
                               side_effect=_list_nodes), \
             mock.patch.object(fu.BlueprintPythonBridge, "get_pin_infos",
                               side_effect=_get_pin_infos):
            verdict = ue5_bridge._pywrap_verify_pin_disconnected(
                self.graph, "A", "then")

        self.assertEqual(len(seen), 1)
        self.assertNotIsInstance(seen[0], fu.FakeNode,
                                 "必须传 BPNodeInfo 回读结构体，不得传 K2Node")
        self.assertIs(verdict, True)

    def test_f6_typeerror_channel_does_not_silently_degrade(self):
        """校验通道 TypeError → 返回 None（不静默吞成 False/catalog_only）。"""
        a = fu.add_test_node(self.graph, "A", pins=[("then", "EGPD_Output", "exec")])
        with mock.patch.object(fu.BlueprintPythonBridge, "get_pin_infos",
                               side_effect=TypeError("Cannot natively convert 'node'")):
            self.assertIsNone(
                ue5_bridge._pywrap_verify_pin_disconnected(self.graph, "A", "then"))

    def test_f6_ambiguous_title_in_readback_returns_none(self):
        a = fu.add_test_node(self.graph, "A", pins=[("then", "EGPD_Output", "exec")])
        b = fu.add_test_node(self.graph, "A", pins=[("then", "EGPD_Output", "exec")])
        self.assertIsNone(
            ue5_bridge._pywrap_verify_pin_disconnected(self.graph, "A", "then"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
