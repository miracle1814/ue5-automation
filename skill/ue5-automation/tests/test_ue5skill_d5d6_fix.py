#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_ue5skill_d5d6_fix.py — T-20260917-D5D6-CONTRACT-FIX 离线回归
==================================================================
主题：UE5 skill 契约/易用性缺陷 2 条修复（来源：maintainer 2026-09-17 活体 UE 实证）

  D-5  `/build` 的 `events` 字段名契约与实现不一致
       文档/runner docstring 写 `name`，模板实际只认 `event_name`
       → 照文档写 = 「`add_event:`（空名）+ `add_event_node returned None`」
         （报错指不到根因字段）
       ue5_bridge.py（`_normalize_build_events` + `/build` 接线）
       + game_thread_build.py.tmpl（双层兼容 + fail-loud）
       + ue5_runner.py docstring + docs/SOP-PIPELINE.md（文档对齐）

  D-6  `add_function_node` 不回传 node GUID
       → 同名节点场景只能「回读全图 + 按坐标反查 GUID」绕路
       ue5_bridge.py `_cmd_add_function_node`（新增 `node_id` + 保留 `added_node`）

判据（逐条对应测试方法）：
  R5-1 传 `name` → 能建出节点（向后兼容）
  R5-2 传 `event_name` → 能建出节点（现行字段不回退）
  R5-3 两者都给且值不同 → 取 `event_name` + 回传 warning
  R5-4 两者都缺/为空 → fail-loud，点名 `events[i]` + 回传实收 keys
  R5-5 注释 / runner docstring / SOP 文档三处对齐
  R5-6 单测覆盖 R5-1 ~ R5-4（本文件）
  R6-1 返回体新增 `node_id`（32 位 hex）
  R6-2 保留 `added_node`（向后兼容）
  R6-3 复用 `_pywrap_node_guid` / R6-4 裸节点 GUID 不可达 → 回读兜底 / fail-loud 标注
  R6-5 同类命令核对结论（`add_event_node` 不在 /command 白名单）
  R6-6 单测覆盖 R6-1 / R6-2 / R6-4（本文件）

设计要点（离线复现，沿用 test_ue5skill_d1d4_fix.py 范式）：
  · 模板侧 `_run_template`（unreal ← fake_unreal）
  · fake_unreal 无 `add_event_node` / `add_function_call_node` → 本文件在类属性上挂
    **最小替身**（setUp 挂 / tearDown 还原，不污染其它测试模块）
  · 不修改任何生产代码之外的文件；无网络、无 UE 依赖

作者：dev（代码开发 Agent）  日期：2026-09-17
"""

import io
import json
import os
import sys
import tempfile
import types
import unittest

os.environ["UE5_BRIDGE_TEST_MODE"] = "1"
os.environ["UE5_BRIDGE_TOKEN"] = "integration-test-token"

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(SKILL_ROOT, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import ue5_bridge            # noqa: E402
import fake_unreal as fu     # noqa: E402

BP_PATH = "/Game/Test/BP_D5D6"
HEX = "0123456789ABCDEF"


# ══════════════════════════════════════════════════════════
# 离线执行 GameThread 模板（unreal ← fake_unreal）
# ══════════════════════════════════════════════════════════

def _run_template(tmpl_name, spec):
    """渲染并离线执行模板（等价 GameThread 注入），返回结果 JSON dict。"""
    with io.open(os.path.join(SCRIPTS_DIR, tmpl_name), "r",
                 encoding="utf-8") as f:
        code = f.read()
    tmpdir = tempfile.mkdtemp(prefix="d5d6_")
    spec_file = os.path.join(tmpdir, "spec.json")
    result_file = os.path.join(tmpdir, "result.json")
    with io.open(spec_file, "w", encoding="utf-8") as f:
        json.dump(spec, f, ensure_ascii=False)
    code = (code.replace("{SPEC_FILE}", repr(spec_file))
                .replace("{RESULT_FILE}", repr(result_file)))
    _prev = sys.modules.get("unreal")
    sys.modules["unreal"] = fu
    try:
        exec(compile(code, tmpl_name, "exec"), {"__name__": "__d5d6__"})
    finally:
        if _prev is None:
            sys.modules.pop("unreal", None)
        else:
            sys.modules["unreal"] = _prev
    with io.open(result_file, "r", encoding="utf-8") as f:
        return json.load(f)


# ══════════════════════════════════════════════════════════
# DLL 方法替身（fake_unreal 未提供）
# ══════════════════════════════════════════════════════════

def _fake_add_event_node(graph, event_name, pos_x=0, pos_y=0):
    """模拟 C++ `AddEventNode(graph, name, x, y)`：返回带标题的 K2Node。"""
    return fu.add_test_node(graph, str(event_name), node_class="K2Node_Event",
                            x=pos_x, y=pos_y)


class _BareK2Node(object):
    """模拟 UE5.1 **裸 K2Node**：`node_title` 可读，`node_guid` 不可达（H-1 坑）。"""

    def __init__(self, title):
        self.node_title = title

    @property
    def node_guid(self):
        raise AttributeError("node_guid 在裸 K2Node 上不可达（H-1）")


def _make_add_call_node(node_factory):
    """造 `add_function_call_node` 替身（node_factory(graph, x, y) → 节点）。"""
    def _stub(graph, function_name, target_class_name, pos_x=0, pos_y=0):
        return node_factory(graph, pos_x, pos_y)
    return _stub


def _run_inline(fn, *a, **k):
    """GT 同步替身：丢弃调度用 `timeout`，其余参数原样内联执行。"""
    k.pop("timeout", None)
    return fn(*a, **k)


class _FakeRequest(object):
    """最小 Request 替身（仅需 `headers`，供 `_verify_token` 用）。"""

    def __init__(self, token="integration-test-token"):
        self.headers = {"x-skill-token": token}
        self.state = types.SimpleNamespace(trace_id="d5d6-test")


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
        fu.BlueprintPythonBridge.add_event_node = staticmethod(
            _fake_add_event_node)

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

    def _event_step(self, res, step_name):
        for s in res["steps"]:
            if s["step"] == step_name:
                return s
        raise AssertionError("no step %r in %s" % (step_name, res["steps"]))


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
# D-5 · 模板层（直接写 spec 文件 → 双层兼容）
# ══════════════════════════════════════════════════════════

class D5TemplateCompatTests(_Base):

    def test_r5_1_alias_name_still_builds_node(self):
        """R5-1：仅 `name`（历史文档写法）→ **必须能建出节点**，不得空名 + None。"""
        res = self._build([{"name": "ReceiveActorEndOverlap", "pos": [768, 0]}])

        st = self._event_step(res, "add_event:ReceiveActorEndOverlap")
        self.assertEqual(st["status"], "ok", st)
        self.assertTrue(res["success"], res)
        self.assertIn("ReceiveActorEndOverlap",
                      [n.node_title for n in self.graph.get_nodes()])

    def test_r5_2_primary_event_name_still_builds_node(self):
        """R5-2：仅 `event_name`（现行生效字段）→ 行为不回退。"""
        res = self._build([{"event_name": "ReceiveActorEndOverlap"}])

        st = self._event_step(res, "add_event:ReceiveActorEndOverlap")
        self.assertEqual(st["status"], "ok", st)
        self.assertTrue(res["success"], res)

    def test_r5_3_conflict_takes_event_name_and_warns(self):
        """R5-3：两者都给且值不同 → 取 `event_name` + **回传 warning**（禁静默择一）。"""
        res = self._build([{"name": "ReceiveBeginPlay",
                            "event_name": "ReceiveActorEndOverlap"}])

        st = self._event_step(res, "add_event:ReceiveActorEndOverlap")
        self.assertEqual(st["status"], "ok", st)
        self.assertIn("ReceiveActorEndOverlap",
                      [n.node_title for n in self.graph.get_nodes()])
        self.assertNotIn("ReceiveBeginPlay",
                         [n.node_title for n in self.graph.get_nodes()])
        warns = res.get("warnings") or []
        self.assertTrue(any("【D-5】" in w for w in warns), warns)
        self.assertTrue(any("值不同" in w for w in warns), warns)

    def test_r5_4_missing_name_fail_loud_with_index_and_keys(self):
        """R5-4：两者都缺 → fail-loud：点名 `events[i]` + 实收 keys（**不静默**）。"""
        res = self._build([{"pos": [0, 0]}])

        self.assertFalse(res["success"], res)
        st = self._event_step(res, "add_event:[events[0]]")
        self.assertEqual(st["status"], "error", st)
        self.assertIn("events[0]", st["error"])
        self.assertIn("缺少事件名", st["error"])
        self.assertIn("event_name", st["error"])
        self.assertIn("keys", st["error"])
        self.assertIn("pos", st["error"])          # 实收 keys 内容
        # 绝不落到旧报错（指不到根因字段）
        self.assertNotIn("returned None", st["error"])

    def test_r5_4b_blank_strings_are_treated_as_missing(self):
        """R5-4：空串 / 纯空白同样按「缺名」处理（不得静默建空名节点）。"""
        res = self._build([{"event_name": "", "name": "   "}])

        self.assertFalse(res["success"], res)
        st = self._event_step(res, "add_event:[events[0]]")
        self.assertIn("缺少事件名", st["error"])

    def test_r5_4c_second_item_index_is_reported(self):
        """R5-4：索引必须指向**真正出错的项**（第 2 项 → `events[1]`）。"""
        res = self._build([{"event_name": "ReceiveBeginPlay"}, {"pos": [0, 0]}])

        st = self._event_step(res, "add_event:[events[1]]")
        self.assertIn("events[1]", st["error"])
        self.assertEqual(
            self._event_step(res, "add_event:ReceiveBeginPlay")["status"], "ok")

    def test_r5_4d_no_bare_empty_event_step(self):
        """R5-4 反例守卫：**禁止**再出现 `add_event:`（空事件名）这种无信息步骤。"""
        res = self._build([{"pos": [0, 0]}])
        self.assertNotIn("add_event:", [s["step"] for s in res["steps"]])

    def test_r5_4e_non_dict_item_fail_loud(self):
        """R5-4 边界：非对象项 → 可读 fail-loud（不得裸 AttributeError）。"""
        res = self._build(["ReceiveBeginPlay"])

        st = self._event_step(res, "add_event:[events[0]]")
        self.assertEqual(st["status"], "error", st)
        self.assertIn("类型错误", st["error"])


# ══════════════════════════════════════════════════════════
# D-5 · bridge 归一化层 + /build 接线
# ══════════════════════════════════════════════════════════

class D5BridgeNormalizeTests(_BridgeBase):

    def test_r5_1_alias_name_normalized_not_rejected(self):
        ev, warns = ue5_bridge._normalize_build_events(
            [{"name": "ReceiveActorEndOverlap", "pos": [768, 0]}])
        self.assertEqual(ev[0]["event_name"], "ReceiveActorEndOverlap")
        self.assertEqual(ev[0]["name"], "ReceiveActorEndOverlap")
        self.assertTrue(any("【D-5】" in w for w in warns), warns)

    def test_r5_2_primary_name_unchanged(self):
        ev, warns = ue5_bridge._normalize_build_events(
            [{"event_name": "ReceiveActorEndOverlap"}])
        self.assertEqual(ev[0]["event_name"], "ReceiveActorEndOverlap")
        self.assertEqual(ev[0]["pos"], [0, 0])      # 缺省位置补齐
        self.assertEqual(warns, [])

    def test_r5_3_conflict_warns_and_takes_primary(self):
        ev, warns = ue5_bridge._normalize_build_events(
            [{"name": "ReceiveBeginPlay",
              "event_name": "ReceiveActorEndOverlap"}])
        self.assertEqual(ev[0]["event_name"], "ReceiveActorEndOverlap")
        self.assertEqual(ev[0]["name"], "ReceiveActorEndOverlap")  # 别名镜像
        self.assertTrue(any("值不同" in w for w in warns), warns)

    def test_r5_4_missing_name_raises_bridge_error(self):
        with self.assertRaises(ue5_bridge.BridgeError) as cm:
            ue5_bridge._normalize_build_events([{"pos": [0, 0]}])
        msg = str(cm.exception)
        self.assertIn("events[0]", msg)
        self.assertIn("缺少事件名", msg)
        self.assertIn("keys", msg)
        self.assertEqual(cm.exception.category, "param_type")

    def test_r5_4b_non_dict_raises_bridge_error(self):
        with self.assertRaises(ue5_bridge.BridgeError) as cm:
            ue5_bridge._normalize_build_events(["ReceiveBeginPlay"])
        self.assertIn("类型错误", str(cm.exception))

    def test_r5_4c_empty_events_passthrough(self):
        ev, warns = ue5_bridge._normalize_build_events([])
        self.assertEqual((ev, warns), ([], []))

    def test_r5_4d_build_endpoint_fail_loud_before_side_effect(self):
        """`/build` 接线：非法 events → 结构化失败，且**早于任何副作用**（steps 空）。"""
        req = ue5_bridge.BuildRequest(
            blueprint_name="BP_D5D6_FAIL", events=[{"pos": [1, 2]}])
        res = ue5_bridge.build_blueprint(req, _FakeRequest())

        self.assertFalse(res["success"], res)
        self.assertIn("events[0]", res["error"])
        self.assertIn("缺少事件名", res["error"])
        self.assertIn("keys", res["error"])
        self.assertEqual(res["steps"], [], "非法 spec 不得留下半成品资产")
        self.assertEqual(res.get("category"), "param_type", res)
        self.assertNotIn("add_event_node returned None", res["error"])

    def test_r5_1b_build_endpoint_alias_name_builds_node(self):
        """`/build` 端到端（fake UE · GT 内联）：传 `name` → **节点真建出** + warning 回传。

        修复前复现：模板侧只认 `event_name` → step 为 `add_event:`（空名）
        + `add_event_node returned None`。
        """
        _prev_unreal = sys.modules.get("unreal")
        sys.modules["unreal"] = fu
        _orig_gt = getattr(
            fu.BlueprintPythonBridge, "execute_python_on_game_thread", None)

        def _exec_gt(code_pos):
            exec(compile(code_pos, "<gt-build-d5d6>", "exec"),
                 {"__name__": "__d5d6_gt__"})

        fu.BlueprintPythonBridge.execute_python_on_game_thread = staticmethod(
            _exec_gt)
        try:
            req = ue5_bridge.BuildRequest(
                blueprint_name="BP_D5D6_ALIAS",
                package_path="/Game/Blueprints/",
                events=[{"name": "ReceiveActorEndOverlap", "pos": [768, 0]}],
                compile_after=False,
            )
            res = ue5_bridge.build_blueprint(req, _FakeRequest())
        finally:
            if _orig_gt is None:
                try:
                    del fu.BlueprintPythonBridge.execute_python_on_game_thread
                except AttributeError:
                    pass
            else:
                fu.BlueprintPythonBridge.execute_python_on_game_thread = _orig_gt
            if _prev_unreal is not None:
                sys.modules["unreal"] = _prev_unreal

        self.assertTrue(res["success"], res)
        steps = [s for s in res["steps"] if s["step"].startswith("add_event:")]
        self.assertEqual(len(steps), 1, res["steps"])
        self.assertEqual(steps[0]["step"], "add_event:ReceiveActorEndOverlap")
        self.assertEqual(steps[0]["status"], "ok", steps[0])
        self.assertNotIn("returned None", json.dumps(res, ensure_ascii=False))
        # 节点真落在图上（不是「步骤说 ok」）
        bp = fu.EditorAssetLibrary.load_asset("/Game/Blueprints/BP_D5D6_ALIAS")
        self.assertIsNotNone(bp, "蓝图资产未创建")
        titles = [n.node_title for n in bp.graphs["EventGraph"].get_nodes()]
        self.assertIn("ReceiveActorEndOverlap", titles, titles)
        # R5-1 别名可用 + D-5 告警回传（不阻断）
        self.assertTrue(any("【D-5】" in w for w in (res.get("warnings") or [])),
                        res.get("warnings"))


# ══════════════════════════════════════════════════════════
# D-6 · add_function_node 回传 node_id
# ══════════════════════════════════════════════════════════

class D6NodeIdTests(_BridgeBase):

    def setUp(self):
        super().setUp()
        self._orig_afcn = getattr(
            fu.BlueprintPythonBridge, "add_function_call_node", None)

    def tearDown(self):
        if self._orig_afcn is None:
            try:
                del fu.BlueprintPythonBridge.add_function_call_node
            except AttributeError:
                pass
        else:
            fu.BlueprintPythonBridge.add_function_call_node = self._orig_afcn
        super().tearDown()

    def _patch(self, stub):
        fu.BlueprintPythonBridge.add_function_call_node = staticmethod(stub)

    @staticmethod
    def _is_guid(s):
        s = str(s or "")
        return len(s) == 32 and all(c in HEX for c in s.upper())

    # ── R6-1 / R6-2 ─────────────────────────────────────────

    def test_r6_1_and_r6_2_return_carries_node_id_and_keeps_added_node(self):
        """R6-1 + R6-2：新增 32 位 hex `node_id`，`added_node` 保留（不改名不删除）。"""
        created = {}

        def _stub(graph, function_name, target_class_name, pos_x=0, pos_y=0):
            n = fu.add_test_node(graph, "打印字符串", x=pos_x, y=pos_y)
            created["node"] = n
            return n

        self._patch(_make_add_call_node(lambda g, x, y: _stub(g, "", "", x, y)))
        out = ue5_bridge._cmd_add_function_node(
            BP_PATH, "EventGraph",
            "/Script/Engine.KismetSystemLibrary:PrintString", [10, 20])

        self.assertIn("added_node", out, out)               # R6-2 保留
        self.assertEqual(out["added_node"], "打印字符串", out)
        self.assertIn("node_id", out, out)                  # R6-1 新增
        self.assertTrue(self._is_guid(out["node_id"]), out)
        self.assertEqual(out["node_id"],
                         ue5_bridge._pywrap_norm_guid(created["node"].node_guid))
        self.assertEqual(out["graph"], "EventGraph")
        self.assertIn("warnings", out, out)

    def test_r6_1b_node_id_is_usable_as_pin_anchor(self):
        """R6-1：`node_id` 必须是**可直接喂** `set_pin_default_value` 的真 GUID。"""
        def _stub(graph, function_name, target_class_name, pos_x=0, pos_y=0):
            return fu.add_test_node(
                graph, "设置Actor在游戏中隐藏", x=pos_x, y=pos_y,
                pins=[("InString", "EGPD_Input", "bool")])

        self._patch(_stub)
        out = ue5_bridge._cmd_add_function_node(
            BP_PATH, "EventGraph",
            "/Script/Engine.Actor:SetActorHiddenInGame")
        pin_res = ue5_bridge._cmd_set_pin_default_value(
            BP_PATH, out["node_id"], "InString", "true")
        self.assertTrue(pin_res.get("ok"), pin_res)

    # ── R6-4 ────────────────────────────────────────────────

    def test_r6_4_bare_node_guid_unreachable_falls_back_to_readback(self):
        """R6-4：裸节点 `node_guid` 不可达 → 回读兜底取真 GUID（不返回空串）。"""
        def _stub(graph, function_name, target_class_name, pos_x=0, pos_y=0):
            info = fu.add_test_node(graph, "打印字符串", x=pos_x, y=pos_y)
            self.assertEqual(len(ue5_bridge._pywrap_norm_guid(info.node_guid)), 32)
            return _BareK2Node("打印字符串")

        self._patch(_stub)
        out = ue5_bridge._cmd_add_function_node(
            BP_PATH, "EventGraph",
            "/Script/Engine.KismetSystemLibrary:PrintString", [30, 40])

        self.assertTrue(self._is_guid(out["node_id"]), out)
        self.assertTrue(any("回读" in w for w in out["warnings"]), out["warnings"])

    def test_r6_4b_unreachable_and_unresolvable_fail_loud(self):
        """R6-4：回读也取不到 → `node_id == ""` + `warnings` **显式标注**（禁静默空串）。"""
        self._patch(_make_add_call_node(
            lambda g, x, y: _BareK2Node("不存在的节点")))
        out = ue5_bridge._cmd_add_function_node(
            BP_PATH, "EventGraph",
            "/Script/Engine.KismetSystemLibrary:PrintString")

        self.assertEqual(out["node_id"], "", out)
        self.assertTrue(out["warnings"], "空 node_id 必须带 warnings（禁不吭声）")
        self.assertTrue(any("node_id" in w for w in out["warnings"]),
                        out["warnings"])

    def test_r6_4c_ambiguous_readback_does_not_guess(self):
        """R6-4：回读出现**同名多节点**且无位置可消歧 → 不猜（空 + 歧义告警）。"""
        def _stub(graph, function_name, target_class_name, pos_x=0, pos_y=0):
            fu.add_test_node(graph, "打印字符串", x=0, y=0)
            fu.add_test_node(graph, "打印字符串", x=999, y=999)
            return _BareK2Node("打印字符串")

        self._patch(_stub)
        out = ue5_bridge._cmd_add_function_node(
            BP_PATH, "EventGraph",
            "/Script/Engine.KismetSystemLibrary:PrintString")

        self.assertEqual(out["node_id"], "", out)
        self.assertTrue(any("歧义" in w for w in out["warnings"]),
                        out["warnings"])

    def test_r6_4d_recover_helper_reuses_existing_primitives(self):
        """R6-3：兜底复用现成原语（`_pywrap_node_guid` 失败才回读）。"""
        node = fu.add_test_node(self.graph, "打印字符串", x=1, y=2)
        self.assertTrue(self._is_guid(ue5_bridge._pywrap_node_guid(node)))
        guid, warns = ue5_bridge._pywrap_recover_node_guid(
            self.graph, _BareK2Node("打印字符串"))
        self.assertEqual(guid, ue5_bridge._pywrap_norm_guid(node.node_guid))
        self.assertEqual(warns, [])

    # ── R6-5（核对结论，不强制改）────────────────────────────

    def test_r6_5_add_event_node_is_not_a_whitelisted_command(self):
        """R6-5：`add_event_node` 不在 /command 白名单（仅模板直调 DLL）→ 无返回体可缺。"""
        self.assertNotIn("add_event_node", ue5_bridge._COMMAND_WHITELIST)
        self.assertIn("add_function_node", ue5_bridge._COMMAND_WHITELIST)


# ══════════════════════════════════════════════════════════
# R5-5 · 三处文档对齐（注释 / runner docstring / SOP）
# ══════════════════════════════════════════════════════════

class D5DocAlignmentTests(unittest.TestCase):

    def _read(self, *parts):
        with io.open(os.path.join(SKILL_ROOT, *parts), "r",
                     encoding="utf-8") as f:
            return f.read()

    def test_r5_5_bridge_field_comment_documents_both_names(self):
        src = self._read("scripts", "ue5_bridge.py")
        self.assertIn("event_name, name(兼容别名)", src)
        self.assertNotIn("# [{name, pos:[x,y]}]", src)

    def test_r5_5_runner_docstring_uses_event_name(self):
        src = self._read("scripts", "ue5_runner.py")
        self.assertIn('"events": [{"event_name": "EventBeginPlay"', src)
        self.assertNotIn('"events": [{"name": "EventBeginPlay"}]', src)
        self.assertIn("兼容别名", src)

    def test_r5_5_sop_no_longer_flags_inconsistency(self):
        src = self._read("docs", "SOP-PIPELINE.md")
        self.assertNotIn("按文档写会静默取到空名", src)
        self.assertIn("已对齐", src)
        self.assertIn("event_name", src)
        self.assertIn("兼容别名", src)

    def test_r5_5_build_endpoint_docstring_documents_contract(self):
        doc = ue5_bridge.build_blueprint.__doc__ or ""
        self.assertIn("event_name", doc)
        self.assertIn("name", doc)


if __name__ == "__main__":
    unittest.main(verbosity=2)
