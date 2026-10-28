#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_ue5skill_d1d4_fix.py — T-20260916-UE5AUTO-HALLBEACON-D1D4-FIX 离线回归
==========================================================================
主题：UE5 skill 蓝图缺陷 4 条修复（来源：T-20260916-UE5AUTO-HALLBEACON P5 验证报告 §3）

  D-1  /build 事件存在性判定拿「全量本地化标题」当候选 → 误判 exists 静默漏建
       game_thread_build.py.tmpl（`_event_title_candidates` 构造段）
  D-2  set_pin_default_value / get_pin_default_value 仅标题定位 → 同名节点无法分别设值
       ue5_bridge.py
  D-3  add_function_node 返回 None 时无原因、不 fail-loud（返回假阳性「成功」）
       ue5_bridge.py + game_thread_build.py.tmpl
  D-4  白名单 unpin_console_refs 无默认参数 → 裸 TypeError（且无文档）
       ue5_bridge.py

设计要点（离线复现）：
  · 模板侧沿用 test_titlegate_guid.py 的 `_run_template`（unreal ← fake_unreal）
  · fake_unreal 无 `add_event_node` / `add_function_call_node` 两个 DLL 方法 →
    本测试在类属性上挂**最小替身**（setUp 挂 / tearDown 还原，不污染其它测试模块）
  · 本文件不修改任何生产代码之外的文件

作者：dev（代码开发 Agent）  日期：2026-09-16
"""

import os
import sys
import json
import tempfile
import unittest

os.environ["UE5_BRIDGE_TEST_MODE"] = "1"
os.environ["UE5_BRIDGE_TOKEN"] = "integration-test-token"

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(SKILL_ROOT, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import ue5_bridge            # noqa: E402
import fake_unreal as fu     # noqa: E402

BP_PATH = "/Game/Test/BP_D1D4"


# ══════════════════════════════════════════════════════════
# 离线执行 GameThread 模板（unreal ← fake_unreal）
# ══════════════════════════════════════════════════════════

def _run_template(tmpl_name, spec):
    """渲染并离线执行模板（等价 GameThread 注入），返回结果 JSON dict。"""
    with open(os.path.join(SCRIPTS_DIR, tmpl_name), "r", encoding="utf-8") as f:
        code = f.read()
    tmpdir = tempfile.mkdtemp(prefix="d1d4_")
    spec_file = os.path.join(tmpdir, "spec.json")
    result_file = os.path.join(tmpdir, "result.json")
    with open(spec_file, "w", encoding="utf-8") as f:
        json.dump(spec, f, ensure_ascii=False)
    code = (code.replace("{SPEC_FILE}", repr(spec_file))
                .replace("{RESULT_FILE}", repr(result_file)))
    _prev = sys.modules.get("unreal")
    sys.modules["unreal"] = fu          # 模板内 `import unreal` → fake 替身
    try:
        exec(compile(code, tmpl_name, "exec"), {"__name__": "__d1d4__"})
    finally:
        if _prev is None:
            sys.modules.pop("unreal", None)
        else:
            sys.modules["unreal"] = _prev
    with open(result_file, "r", encoding="utf-8") as f:
        return json.load(f)


# ══════════════════════════════════════════════════════════
# DLL 方法替身（fake_unreal 未提供）
# ══════════════════════════════════════════════════════════

#: 模拟 UE 中文环境的本地化节点标题
_LOCALIZED_MOCK = {
    "beginplay": "事件开始运行",
    "actorbegingoverlap": "事件Actor开始重叠",
    "actorendoverlap": "事件Actor结束重叠",
    "tick": "事件Tick",
}


def _ev_key(name):
    """事件名归一化（去 Receive/Event 前缀 + 去分隔符 + 小写）。"""
    s = str(name or "").strip().lower().replace("_", "").replace(" ", "")
    if s.startswith("receive"):
        s = s[len("receive"):]
    return s


def _fake_add_event_node(graph, event_name, pos_x=0, pos_y=0):
    """模拟 C++ `AddEventNode(graph, name, x, y)`。

    · `BAD_` 前缀 → 返回 None（模拟引擎不认识的事件名，用于 fail-loud 断言）
    · 其余 → 建 K2Node_Event，标题按本地化映射给出（对齐 UE 中文环境）
    """
    if str(event_name).startswith("BAD_"):
        return None
    title = _LOCALIZED_MOCK.get(_ev_key(event_name), str(event_name))
    return fu.add_test_node(graph, title, node_class="K2Node_Event", x=pos_x, y=pos_y)


def _fn_add_call_node_none(*a, **k):
    """模拟 DLL 建函数节点失败（函数名/类名不匹配）→ 返回 None。"""
    return None


def _run_inline(fn, *a, **k):
    """GT 同步替身：丢弃调度参数，直接内联执行。"""
    return fn()


# ══════════════════════════════════════════════════════════
# 基类
# ══════════════════════════════════════════════════════════

class _Base(unittest.TestCase):

    def setUp(self):
        fu.reset_fake()
        self.bp = fu.create_test_blueprint(BP_PATH)
        self.graph = self.bp.graphs["EventGraph"]
        self._orig_add_event = getattr(
            fu.BlueprintPythonBridge, "add_event_node", None)
        fu.BlueprintPythonBridge.add_event_node = staticmethod(_fake_add_event_node)

    def tearDown(self):
        if self._orig_add_event is None:
            try:
                del fu.BlueprintPythonBridge.add_event_node
            except AttributeError:
                pass
        else:
            fu.BlueprintPythonBridge.add_event_node = self._orig_add_event

    def _build(self, events, functions=None):
        return _run_template("game_thread_build.py.tmpl", {
            "blueprint_path": BP_PATH,
            "events": events,
            "functions": functions or [],
            "connections": [],
            "compile_after": False,
        })

    def _event_step(self, res, ev_name):
        for s in res["steps"]:
            if s["step"] == "add_event:" + ev_name:
                return s
        raise AssertionError("no add_event step: %s / %s" % (ev_name, res["steps"]))

    def _node_titles(self):
        return [n.node_title for n in self.graph.get_nodes()]


class _BridgeBase(_Base):
    """需要调用 ue5_bridge._cmd_* 的用例：GT 同步 → 内联。"""

    def setUp(self):
        super().setUp()
        self._orig_sync = ue5_bridge._run_on_game_thread_sync
        ue5_bridge._run_on_game_thread_sync = _run_inline

    def tearDown(self):
        ue5_bridge._run_on_game_thread_sync = self._orig_sync
        super().tearDown()


# ══════════════════════════════════════════════════════════
# D-1 · /build 事件存在性判定（语义绑定）
# ══════════════════════════════════════════════════════════

class D1EventExistenceTests(_Base):

    def test_d1_a_endoverlap_not_misjudged_exists_by_beginoverlap_title(self):
        """修复前复现：图内只有「事件Actor开始重叠」→ 建 ReceiveActorEndOverlap
        曾被误判 exists 而**静默漏建**（候选列表无条件塞入了全部本地化标题）。"""
        fu.add_test_node(self.graph, "事件Actor开始重叠", node_class="K2Node_Event")
        res = self._build([{"event_name": "ReceiveActorEndOverlap"}])

        st = self._event_step(res, "ReceiveActorEndOverlap")
        self.assertNotEqual(st["status"], "exists",
                            "不得因「无关事件」的本地化标题命中而误判已存在: %s" % st)
        self.assertEqual(st["status"], "ok", st)
        self.assertEqual(len(self.graph.get_nodes()), 2, self._node_titles())

    def test_d1_b_unrelated_tick_title_is_not_a_candidate(self):
        """修复前复现：图内只有「事件Tick」→ 建 EndOverlap 曾误判 exists。"""
        fu.add_test_node(self.graph, "事件Tick", node_class="K2Node_Event")
        res = self._build([{"event_name": "ReceiveActorEndOverlap"}])

        st = self._event_step(res, "ReceiveActorEndOverlap")
        self.assertEqual(st["status"], "ok", st)
        self.assertIn("事件Actor结束重叠", self._node_titles())

    def test_d1_c_localized_candidate_still_semantically_bound(self):
        """向后兼容：ReceiveBeginPlay ↔「事件开始运行」仍判定 exists（不重复创建）。"""
        fu.add_test_node(self.graph, "事件开始运行", node_class="K2Node_Event")
        res = self._build([{"event_name": "ReceiveBeginPlay"}])

        st = self._event_step(res, "ReceiveBeginPlay")
        self.assertEqual(st["status"], "exists", st)
        self.assertEqual(len(self.graph.get_nodes()), 1, self._node_titles())

    def test_d1_d_endoverlap_localized_map_gap_filled(self):
        """修复前复现（map 缺口）：图内已有「事件Actor结束重叠」→ 旧候选表无该键
        → 既查不到又去重失败 → **重复创建同名事件节点**（编译期 Status=2 风险）。"""
        fu.add_test_node(self.graph, "事件Actor结束重叠", node_class="K2Node_Event")
        res = self._build([{"event_name": "ReceiveActorEndOverlap"}])

        st = self._event_step(res, "ReceiveActorEndOverlap")
        self.assertEqual(st["status"], "exists", st)
        self.assertEqual(len(self.graph.get_nodes()), 1, self._node_titles())

    def test_d1_e_exists_step_carries_matched_evidence(self):
        """可审计：判定 exists 时必须回传命中的真实标题（杜绝再次静默误判）。"""
        fu.add_test_node(self.graph, "事件Actor开始重叠", node_class="K2Node_Event")
        res = self._build([{"event_name": "ReceiveActorBeginOverlap"}])

        st = self._event_step(res, "ReceiveActorBeginOverlap")
        self.assertEqual(st["status"], "exists", st)
        self.assertEqual(st.get("matched_title"), "事件Actor开始重叠", st)

    def test_d1_f_create_none_still_fail_loud(self):
        """既有 fail-loud 语义不回退：引擎不识别的事件名 → error + success=false。"""
        res = self._build([{"event_name": "BAD_NoSuchEvent"}])
        st = self._event_step(res, "BAD_NoSuchEvent")
        self.assertEqual(st["status"], "error", st)
        self.assertFalse(res["success"])


# ══════════════════════════════════════════════════════════
# D-2 · set/get_pin_default_value 支持 GUID 定位
# ══════════════════════════════════════════════════════════

class D2PinGuidTests(_BridgeBase):

    def _two_same_title(self):
        a = fu.add_test_node(self.graph, "设置Actor在游戏中隐藏",
                             pins=[("InString", "EGPD_Input", "bool")])
        b = fu.add_test_node(self.graph, "设置Actor在游戏中隐藏",
                             pins=[("InString", "EGPD_Input", "bool")])
        return a, b

    def test_d2_set_by_guid_targets_exact_node(self):
        """修复前复现：同名 3 节点无法分别设值 —— GUID 入参曾直接「节点未找到」。"""
        a, b = self._two_same_title()
        out = ue5_bridge._cmd_set_pin_default_value(
            BP_PATH, a.node_guid, "InString", "true")
        self.assertTrue(out.get("ok"), out)
        self.assertEqual(a.find_pin("InString").default_value, "true")
        self.assertEqual(b.find_pin("InString").default_value, "",
                         "同名兄弟节点不得被牵连（GUID 全局唯一）")

    def test_d2_get_by_guid_reads_exact_node(self):
        a, b = self._two_same_title()
        ue5_bridge._cmd_set_pin_default_value(BP_PATH, a.node_guid, "InString", "true")
        out = ue5_bridge._cmd_get_pin_default_value(BP_PATH, b.node_guid, "InString")
        self.assertEqual(out.get("value"), "", out)
        out2 = ue5_bridge._cmd_get_pin_default_value(BP_PATH, a.node_guid, "InString")
        self.assertEqual(out2.get("value"), "true", out2)

    def test_d2_title_path_stays_backward_compatible(self):
        """标题定位保持向后兼容（唯一标题 → 既有行为不变）。"""
        fu.add_test_node(self.graph, "打印字符串",
                         pins=[("InString", "EGPD_Input", "string")])
        out = ue5_bridge._cmd_set_pin_default_value(
            BP_PATH, "打印字符串", "InString", "hi")
        self.assertTrue(out.get("ok"), out)

    def test_d2_unknown_guid_fail_loud(self):
        """GUID 未命中 → 必须报「GUID」语义的可读错误（不伪装成标题未找到）。"""
        with self.assertRaises(ue5_bridge.BridgeError) as cm:
            ue5_bridge._cmd_set_pin_default_value(
                BP_PATH, "0" * 32, "InString", "true")
        self.assertIn("GUID", str(cm.exception))


# ══════════════════════════════════════════════════════════
# D-3 · add_function_node 返回 None → fail-loud + 候选诊断
# ══════════════════════════════════════════════════════════

class D3AddFunctionNodeTests(_BridgeBase):

    def setUp(self):
        super().setUp()
        self._orig_afcn = getattr(
            fu.BlueprintPythonBridge, "add_function_call_node", None)
        fu.BlueprintPythonBridge.add_function_call_node = staticmethod(
            _fn_add_call_node_none)

    def tearDown(self):
        if self._orig_afcn is None:
            try:
                del fu.BlueprintPythonBridge.add_function_call_node
            except AttributeError:
                pass
        else:
            fu.BlueprintPythonBridge.add_function_call_node = self._orig_afcn
        super().tearDown()

    def test_d3_bridge_none_result_fail_loud_with_candidates(self):
        """修复前复现：返回 `{"added_node": ...}` 的**假阳性成功**（无 error 字段）。"""
        with self.assertRaises(ue5_bridge.BridgeError) as cm:
            ue5_bridge._cmd_add_function_node(
                BP_PATH, "EventGraph",
                "/Script/Engine.Actor:K2_SetActorHiddenInGame")
        msg = str(cm.exception)
        self.assertIn("候选", msg, msg)
        self.assertIn("K2_", msg, msg)
        self.assertIn("SetActorHiddenInGame", msg, msg)

    def test_d3_tmpl_none_result_fail_loud_with_candidates(self):
        """模板 /build 路径同样给出候选诊断（不再裸报 returned None）。"""
        res = self._build([], functions=[{
            "function_name": "K2_SetActorHiddenInGame",
            "function_path": "/Script/Engine.Actor:K2_SetActorHiddenInGame",
        }])
        st = [s for s in res["steps"] if s["step"].startswith("add_function:")][0]
        self.assertEqual(st["status"], "error", st)
        self.assertFalse(res["success"])
        self.assertIn("候选", st["error"], st)
        self.assertIn("SetActorHiddenInGame", st["error"], st)

    def test_d3_bad_function_path_still_fail_loud(self):
        """既有守卫不回退：function_path 缺 ':' → 可读错误。"""
        with self.assertRaises(ue5_bridge.BridgeError) as cm:
            ue5_bridge._cmd_add_function_node(BP_PATH, "EventGraph", "NoColonHere")
        self.assertIn("function_path", str(cm.exception))


# ══════════════════════════════════════════════════════════
# D-4 · 白名单 unpin_console_refs 缺参 → 可读错误（非裸 TypeError）
# ══════════════════════════════════════════════════════════

class D4WhitelistSignatureTests(_BridgeBase):

    def test_d4_no_arg_call_is_readable_error_not_typeerror(self):
        """修复前复现：`{"command":"unpin_console_refs","params":{}}`
        → 裸 `TypeError: missing 1 required positional argument: 'asset_path'`。"""
        out = ue5_bridge._COMMAND_WHITELIST["unpin_console_refs"]()
        self.assertIsInstance(out, dict, out)
        self.assertFalse(out.get("success"), out)
        self.assertIn("asset_path", out.get("error", ""), out)

    def test_d4_blank_asset_path_error_is_actionable(self):
        out = ue5_bridge._COMMAND_WHITELIST["unpin_console_refs"](asset_path="")
        self.assertFalse(out.get("success"), out)
        self.assertIn("asset_path", out["error"])

    def test_d4_docstring_documents_asset_path(self):
        """文档补齐：命令 docstring 必须说明 asset_path 的语义与用法。"""
        doc = ue5_bridge._cmd_unpin_console_refs.__doc__ or ""
        self.assertIn("asset_path", doc)
        self.assertIn("/Game/", doc)

    def test_d4b_set_pin_default_value_empty_params_readable_error(self):
        """同族签名缺陷：set_pin_default_value 缺参 → BridgeError（非 TypeError）。"""
        with self.assertRaises(ue5_bridge.BridgeError) as cm:
            ue5_bridge._COMMAND_WHITELIST["set_pin_default_value"](bp_path=BP_PATH)
        self.assertIn("node_id", str(cm.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
