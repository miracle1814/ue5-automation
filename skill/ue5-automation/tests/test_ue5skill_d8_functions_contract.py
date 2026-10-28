#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_ue5skill_d8_functions_contract.py — T-20260917-D8-FUNCTIONS-CONTRACT 离线回归
====================================================================================
主题：`/build` 的 `functions[]` 契约与实现不一致（与 D-5 `events[]` **完全同型**）

  修前故障链（maintainer 2026-09-17 实测落准）：
    · 实现读取（`game_thread_build.py.tmpl` L307-309） = `function_name` / `function_path`
    · 文档写的（`ue5_runner.py` L410 docstring）       = `name` / `target_class`
    · bridge 侧（`ue5_bridge.py` L5580 `/build` · L7278 `/build-batch`）**原样透传**
    → 第三方照文档写：`fn_name` 空 + `fn_path` 空 → 走 tmpl `else` 分支
      → `add_function_node(graph, '')` → 返回 None
      → step `add_function:`（**空函数名**）+ error「returned None…常见原因 ①②③④」
      → **四条提示全指向错方向**（真实原因 = 键名写错），第三方无从排查。

判据（逐条对应测试方法）：
  K2 传 `name` 别名            → 建出**非空函数名**节点（不再落 returned None）
  K3 传 `function_name` 主字段 → 可用（现行字段不回退）
  K4 两者冲突                  → 取 `function_name` + 回传 `warnings`
  K5 两者皆空                  → fail-loud：点名 `functions[i]` + 实收 keys
  K1 归一化函数 + 两处接线（`/build` · `/build-batch`）实测存在
  K4b/K2b `target_class` 推导 `function_path`（来源标注）/ 仅 path 反推函数名
  K7/K8 文档对齐（SOP-PIPELINE.md · ue5_runner.py docstring · /build docstring）

设计：沿用 test_ue5skill_d5d6_fix.py 范式（模板离线执行 + fake_unreal 替身），
      无网络、无 UE 依赖，不触碰生产代码以外的东西。

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

BP_PATH = "/Game/Test/BP_D8"


# ══════════════════════════════════════════════════════════
# 离线执行 GameThread 模板（unreal ← fake_unreal）
# ══════════════════════════════════════════════════════════

def _run_template(tmpl_name, spec):
    """渲染并离线执行模板（等价 GameThread 注入），返回结果 JSON dict。"""
    with io.open(os.path.join(SCRIPTS_DIR, tmpl_name), "r",
                 encoding="utf-8") as f:
        code = f.read()
    tmpdir = tempfile.mkdtemp(prefix="d8_")
    spec_file = os.path.join(tmpdir, "spec.json")
    result_file = os.path.join(tmpdir, "result.json")
    with io.open(spec_file, "w", encoding="utf-8") as f:
        json.dump(spec, f, ensure_ascii=False)
    code = (code.replace("{SPEC_FILE}", repr(spec_file))
                .replace("{RESULT_FILE}", repr(result_file)))
    _prev = sys.modules.get("unreal")
    sys.modules["unreal"] = fu
    try:
        exec(compile(code, tmpl_name, "exec"), {"__name__": "__d8__"})
    finally:
        if _prev is None:
            sys.modules.pop("unreal", None)
        else:
            sys.modules["unreal"] = _prev
    with io.open(result_file, "r", encoding="utf-8") as f:
        return json.load(f)


# ══════════════════════════════════════════════════════════
# DLL 方法替身（fake_unreal 未提供 add_function_node /
# add_function_call_node → 本文件在类属性上挂最小替身，
# setUp 挂 / tearDown 还原，不污染其它测试模块）
# ══════════════════════════════════════════════════════════

def _fake_add_function_call_node(graph, function_name, target_class_name,
                                 pos_x=0, pos_y=0):
    """模拟 C++ `AddFunctionCallNode(graph, fn_name, class_name, x, y)`。

    关键：节点标题 = **传入的函数名**（修前传空串 → 标题为空 → 无法验收）。
    """
    return fu.add_test_node(graph, str(function_name),
                            node_class="K2Node_CallFunction",
                            x=pos_x, y=pos_y)


def _fake_add_function_node(graph, function_name):
    """模拟 C++ `AddFunctionNode(graph, fn_name)`（无 class 拆分的老签名）。"""
    return fu.add_test_node(graph, str(function_name),
                            node_class="K2Node_CallFunction")


def _run_inline(fn, *a, **k):
    """GT 同步替身：丢弃调度用 `timeout`，其余参数原样内联执行。"""
    k.pop("timeout", None)
    return fn(*a, **k)


class _FakeRequest(object):
    """最小 Request 替身（仅需 `headers`，供 `_verify_token` 用）。"""

    def __init__(self, token="integration-test-token"):
        self.headers = {"x-skill-token": token}
        self.state = types.SimpleNamespace(trace_id="d8-test")


# ══════════════════════════════════════════════════════════
# 基类
# ══════════════════════════════════════════════════════════

class _Base(unittest.TestCase):

    def setUp(self):
        fu.reset_fake()
        self.bp = fu.create_test_blueprint(BP_PATH)
        self.graph = self.bp.graphs["EventGraph"]
        self._orig_afn = getattr(fu.BlueprintPythonBridge,
                                 "add_function_node", None)
        self._orig_afcn = getattr(fu.BlueprintPythonBridge,
                                  "add_function_call_node", None)
        fu.BlueprintPythonBridge.add_function_node = staticmethod(
            _fake_add_function_node)
        fu.BlueprintPythonBridge.add_function_call_node = staticmethod(
            _fake_add_function_call_node)

    def tearDown(self):
        for _name, _orig in (("add_function_node", self._orig_afn),
                             ("add_function_call_node", self._orig_afcn)):
            if _orig is None:
                try:
                    delattr(fu.BlueprintPythonBridge, _name)
                except AttributeError:
                    pass
            else:
                setattr(fu.BlueprintPythonBridge, _name, _orig)

    def _build(self, functions):
        return _run_template("game_thread_build.py.tmpl", {
            "blueprint_path": BP_PATH,
            "events": [],
            "functions": functions,
            "connections": [],
            "compile_after": False,
        })

    def _fn_step(self, res, step_name):
        for s in res["steps"]:
            if s["step"] == step_name:
                return s
        raise AssertionError("no step %r in %s" % (step_name, res["steps"]))

    def _titles(self):
        return [n.node_title for n in self.graph.get_nodes()]


class _BridgeBase(_Base):
    """需要调用 /build 端点的用例：GT 同步 → 内联。"""

    def setUp(self):
        super().setUp()
        self._orig_sync = ue5_bridge._run_on_game_thread_sync
        ue5_bridge._run_on_game_thread_sync = _run_inline

    def tearDown(self):
        ue5_bridge._run_on_game_thread_sync = self._orig_sync
        super().tearDown()


# ══════════════════════════════════════════════════════════
# K2 / K3 / K4 / K5 · 模板层（直接写 spec 文件 → 双层兼容）
# ══════════════════════════════════════════════════════════

class D8TemplateCompatTests(_Base):

    def test_k2_alias_name_builds_nonempty_function_node(self):
        """K2：`name` 别名 + `target_class` → **非空函数名节点**（不落 returned None）。

        修复前复现：fn_name='' + fn_path='' → step `add_function:`（空名）
        + `add_function_node returned None`。
        """
        res = self._build([{"name": "PrintString",
                            "target_class": "/Script/Engine.KismetSystemLibrary"}])

        st = self._fn_step(res, "add_function:PrintString")
        self.assertEqual(st["status"], "ok", st)
        self.assertTrue(res["success"], res)
        self.assertIn("PrintString", self._titles(), self._titles())
        self.assertNotIn("returned None", json.dumps(res, ensure_ascii=False))

    def test_k2b_alias_without_target_class_still_builds_node(self):
        """K2 边界：只给 `name`（无 path / 无 target_class）→ 走 add_function_node 分支。"""
        res = self._build([{"name": "PrintString", "pos": [10, 20]}])

        st = self._fn_step(res, "add_function:PrintString")
        self.assertEqual(st["status"], "ok", st)
        self.assertIn("PrintString", self._titles(), self._titles())

    def test_k3_primary_function_name_works(self):
        """K3：`function_name`（主字段）+ `function_path` → 行为不回退。"""
        res = self._build([{
            "function_name": "PrintString",
            "function_path": "/Script/Engine.KismetSystemLibrary:PrintString"}])

        st = self._fn_step(res, "add_function:PrintString")
        self.assertEqual(st["status"], "ok", st)
        self.assertIn("PrintString", self._titles(), self._titles())

    def test_k4_conflict_takes_primary_and_warns(self):
        """K4：两者都给且值不同 → 取 `function_name` + **回传 warning**（禁静默择一）。"""
        res = self._build([{"function_name": "PrintString", "name": "Delay",
                            "target_class": "/Script/Engine.KismetSystemLibrary"}])

        st = self._fn_step(res, "add_function:PrintString")
        self.assertEqual(st["status"], "ok", st)
        titles = self._titles()
        self.assertIn("PrintString", titles, titles)
        self.assertNotIn("Delay", titles, titles)
        warns = res.get("warnings") or []
        self.assertTrue(any("【D-8】" in w for w in warns), warns)
        self.assertTrue(any("值不同" in w for w in warns), warns)

    def test_k5_missing_name_fail_loud_with_index_and_keys(self):
        """K5：两者皆空 → fail-loud：点名 `functions[0]` + 实收 keys（**不静默**）。"""
        res = self._build([{"pos": [0, 0]}])

        self.assertFalse(res["success"], res)
        st = self._fn_step(res, "add_function:[functions[0]]")
        self.assertEqual(st["status"], "error", st)
        self.assertIn("functions[0]", st["error"])
        self.assertIn("缺少函数名", st["error"])
        self.assertIn("keys", st["error"])
        self.assertIn("pos", st["error"])            # 实收 keys 内容
        # 绝不落到旧报错（四条「常见原因」全指向错方向）
        self.assertNotIn("returned None", st["error"])

    def test_k5b_blank_strings_are_treated_as_missing(self):
        """K5：空串 / 纯空白同样按「缺名」处理（不得静默建空名节点）。"""
        res = self._build([{"function_name": "", "name": "   "}])

        self.assertFalse(res["success"], res)
        st = self._fn_step(res, "add_function:[functions[0]]")
        self.assertIn("缺少函数名", st["error"])

    def test_k5c_second_item_index_is_reported(self):
        """K5：索引必须指向**真正出错的项**（第 2 项 → `functions[1]`）。"""
        res = self._build([{"function_name": "PrintString"}, {"pos": [0, 0]}])

        st = self._fn_step(res, "add_function:[functions[1]]")
        self.assertIn("functions[1]", st["error"])
        self.assertEqual(
            self._fn_step(res, "add_function:PrintString")["status"], "ok")

    def test_k5d_no_bare_empty_function_step(self):
        """K5 反例守卫：**禁止**再出现 `add_function:`（空函数名）这种无信息步骤。"""
        res = self._build([{"pos": [0, 0]}])
        self.assertNotIn("add_function:", [s["step"] for s in res["steps"]])

    def test_k5e_non_dict_item_fail_loud(self):
        """K5 边界：非对象项 → 可读 fail-loud（不得裸 AttributeError）。"""
        res = self._build(["PrintString"])

        st = self._fn_step(res, "add_function:[functions[0]]")
        self.assertEqual(st["status"], "error", st)
        self.assertIn("类型错误", st["error"])

    def test_k2c_path_only_derives_function_name(self):
        """K2 边界：只给 `function_path` → 由末段反推函数名（step 名非空）。"""
        res = self._build([{
            "function_path": "/Script/Engine.KismetSystemLibrary:PrintString"}])

        st = self._fn_step(res, "add_function:PrintString")
        self.assertEqual(st["status"], "ok", st)


# ══════════════════════════════════════════════════════════
# K1 / K2 / K3 / K4 · bridge 归一化层
# ══════════════════════════════════════════════════════════

class D8BridgeNormalizeTests(unittest.TestCase):

    def test_k3_primary_fields_pass_through(self):
        """K3：`function_name` + `function_path` → 原样可用，无告警，`pos` 缺省补齐。"""
        out, warns = ue5_bridge._normalize_build_functions([{
            "function_name": "PrintString",
            "function_path": "/Script/Engine.KismetSystemLibrary:PrintString"}])

        self.assertEqual(out[0]["function_name"], "PrintString")
        self.assertEqual(out[0]["name"], "PrintString")          # 别名镜像
        self.assertEqual(out[0]["function_path"],
                         "/Script/Engine.KismetSystemLibrary:PrintString")
        self.assertEqual(out[0]["pos"], [0, 0])
        self.assertEqual(warns, [])

    def test_k2_alias_name_is_accepted_and_path_derived(self):
        """K2：仅 `name` 别名 + `target_class` → 可用 + 推导出等价 `function_path`。"""
        out, warns = ue5_bridge._normalize_build_functions([{
            "name": "PrintString",
            "target_class": "/Script/Engine.KismetSystemLibrary",
            "pos": [100, 200]}])

        self.assertEqual(out[0]["function_name"], "PrintString")
        self.assertEqual(out[0]["name"], "PrintString")
        # K2b：`target_class` 推导等价路径（`<class>:<fn>`）+ 来源标注
        self.assertEqual(out[0]["function_path"],
                         "/Script/Engine.KismetSystemLibrary:PrintString")
        self.assertEqual(out[0]["function_path_source"],
                         "derived_from_target_class")
        self.assertTrue(any("【D-8】" in w for w in warns), warns)
        self.assertTrue(any("推导" in w for w in warns), warns)

    def test_k2b_path_only_derives_function_name(self):
        """K2b 反向：只给 `function_path` → 由末段推导函数名 + 告警标注来源。"""
        out, warns = ue5_bridge._normalize_build_functions([{
            "function_path": "/Script/Engine.KismetSystemLibrary:PrintString"}])

        self.assertEqual(out[0]["function_name"], "PrintString")
        self.assertEqual(out[0]["function_name_source"],
                         "derived_from_function_path")
        self.assertTrue(any("推导" in w for w in warns), warns)

    def test_k4_conflict_takes_primary_and_returns_warning(self):
        """K4：主字段与别名都给且值不同 → 取主字段 + **回传 warning**（禁静默择一）。"""
        out, warns = ue5_bridge._normalize_build_functions([{
            "function_name": "PrintString", "name": "Delay",
            "target_class": "/Script/Engine.KismetSystemLibrary"}])

        self.assertEqual(out[0]["function_name"], "PrintString")
        self.assertEqual(out[0]["name"], "PrintString")
        # 取主字段 → 推导路径也用主字段（绝不混用别名）
        self.assertEqual(out[0]["function_path"],
                         "/Script/Engine.KismetSystemLibrary:PrintString")
        self.assertTrue(any("值不同" in w for w in warns), warns)

    def test_k4b_alias_illegal_target_class_conflict_not_relevant_but_path_wins(self):
        """K4 边界：显式 `function_path` 优先于 `target_class` 推导（不覆盖用户显式值）。"""
        out, warns = ue5_bridge._normalize_build_functions([{
            "name": "PrintString", "target_class": "/Script/Engine.Actor",
            "function_path": "/Script/Engine.KismetSystemLibrary:PrintString"}])

        self.assertEqual(out[0]["function_path"],
                         "/Script/Engine.KismetSystemLibrary:PrintString")
        self.assertEqual(out[0]["function_path_source"], "explicit")

    def test_k5_missing_name_raises_bridge_error_with_index_and_keys(self):
        with self.assertRaises(ue5_bridge.BridgeError) as cm:
            ue5_bridge._normalize_build_functions([{"pos": [0, 0]}])
        msg = str(cm.exception)
        self.assertIn("functions[0]", msg)
        self.assertIn("缺少函数名", msg)
        self.assertIn("keys", msg)
        self.assertIn("pos", msg)                 # 实收 keys 内容
        self.assertEqual(cm.exception.category, "param_type")

    def test_k5b_blank_primary_and_alias_fail_loud(self):
        """K5：主字段与别名都是空串/纯空白 → 同样 fail-loud（不得静默建空名节点）。"""
        with self.assertRaises(ue5_bridge.BridgeError) as cm:
            ue5_bridge._normalize_build_functions(
                [{"function_name": "", "name": "   "}])
        self.assertIn("function_name", str(cm.exception))

    def test_k5c_index_points_to_offending_item(self):
        with self.assertRaises(ue5_bridge.BridgeError) as cm:
            ue5_bridge._normalize_build_functions(
                [{"function_name": "PrintString"}, {"target_class": "/Script/Engine.Actor"}])
        self.assertIn("functions[1]", str(cm.exception))

    def test_k5d_non_dict_raises_bridge_error(self):
        with self.assertRaises(ue5_bridge.BridgeError) as cm:
            ue5_bridge._normalize_build_functions(["PrintString"])
        self.assertIn("类型错误", str(cm.exception))
        self.assertEqual(cm.exception.category, "param_type")

    def test_k5e_empty_list_passthrough(self):
        self.assertEqual(ue5_bridge._normalize_build_functions([]), ([], []))

    def test_k5f_none_passthrough(self):
        self.assertEqual(ue5_bridge._normalize_build_functions(None), ([], []))

    def test_k1_normalizer_defined_and_wired_at_both_transmission_points(self):
        """K1：归一化函数已定义，且 `/build`（L5580 透传点）与
        `/build-batch`（L7278 透传点）**两处**均已接线。"""
        with io.open(os.path.join(SCRIPTS_DIR, "ue5_bridge.py"), "r",
                     encoding="utf-8") as f:
            src = f.read()

        self.assertIn("def _normalize_build_functions(functions)", src)
        self.assertIn("_normalize_build_functions(req.functions)", src)    # /build
        self.assertIn("_normalize_build_functions(spec.functions)", src)   # /build-batch
        self.assertGreaterEqual(src.count("_normalize_build_functions("), 3,
                                "期望 def + 两处接线 ≥ 3 次出现")


# ══════════════════════════════════════════════════════════
# K2 / K5 · 端点层（/build 端到端 · /build-batch 接线）
# ══════════════════════════════════════════════════════════

class D8EndpointTests(_BridgeBase):

    def test_k2_alias_end_to_end_build_endpoint(self):
        """K2 端到端：传 `name` 别名 + `target_class` → **节点真建出** + D-8 告警回传。"""
        _prev_unreal = sys.modules.get("unreal")
        sys.modules["unreal"] = fu
        _orig_gt = getattr(fu.BlueprintPythonBridge,
                           "execute_python_on_game_thread", None)

        def _exec_gt(code_pos):
            exec(compile(code_pos, "<gt-build-d8>", "exec"),
                 {"__name__": "__d8_gt__"})

        fu.BlueprintPythonBridge.execute_python_on_game_thread = staticmethod(
            _exec_gt)
        try:
            req = ue5_bridge.BuildRequest(
                blueprint_name="BP_D8_ALIAS",
                package_path="/Game/Blueprints/",
                functions=[{"name": "PrintString",
                            "target_class": "/Script/Engine.KismetSystemLibrary",
                            "pos": [100, 200]}],
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
        steps = [s for s in res["steps"] if s["step"].startswith("add_function:")]
        self.assertEqual(len(steps), 1, res["steps"])
        self.assertEqual(steps[0]["step"], "add_function:PrintString")
        self.assertEqual(steps[0]["status"], "ok", steps[0])
        self.assertNotIn("returned None",
                         json.dumps(res, ensure_ascii=False, default=str))
        # 节点真落在图上（不是「步骤说 ok」）
        bp = fu.EditorAssetLibrary.load_asset("/Game/Blueprints/BP_D8_ALIAS")
        self.assertIsNotNone(bp, "蓝图资产未创建")
        titles = [n.node_title for n in bp.graphs["EventGraph"].get_nodes()]
        self.assertIn("PrintString", titles, titles)
        self.assertTrue(any("【D-8】" in w for w in (res.get("warnings") or [])),
                        res.get("warnings"))

    def test_k5_build_endpoint_fail_loud_before_side_effect(self):
        """K5 接线：非法 functions → 结构化失败，且**早于任何副作用**（steps 空）。"""
        req = ue5_bridge.BuildRequest(
            blueprint_name="BP_D8_FAIL", functions=[{"pos": [1, 2]}])
        res = ue5_bridge.build_blueprint(req, _FakeRequest())

        self.assertFalse(res["success"], res)
        self.assertIn("functions[0]", res["error"])
        self.assertIn("缺少函数名", res["error"])
        self.assertIn("keys", res["error"])
        self.assertEqual(res["steps"], [], "非法 spec 不得留下半成品资产")
        self.assertEqual(res.get("category"), "param_type", res)
        self.assertNotIn("add_function_node returned None", res["error"])

    def test_k1_build_batch_wiring_normalizes_functions(self):
        """K1 接线：`/build-batch` 同样走归一化 → 非法 functions 在**建资产前**即失败。"""
        req = ue5_bridge.BuildBatchRequest(blueprints=[
            ue5_bridge.BuildBatchSpec(blueprint_name="BP_D8_BATCH",
                                      functions=[{"pos": [0, 0]}])])
        res = ue5_bridge.build_batch(req, _FakeRequest())

        self.assertFalse(res["success"], res)
        self.assertIn("functions[0]", res["error"])
        self.assertIn("缺少函数名", res["error"])
        self.assertEqual(res["created_count"], 0, res)


# ══════════════════════════════════════════════════════════
# K7 / K8 · 文档与模板对齐（SOP / runner docstring / 模板双层）
# ══════════════════════════════════════════════════════════

class D8DocAlignmentTests(unittest.TestCase):

    def _read(self, *parts):
        with io.open(os.path.join(SKILL_ROOT, *parts), "r",
                     encoding="utf-8") as f:
            return f.read()

    def test_k8_template_reads_both_keys(self):
        """K8：模板不再单层读 `fn.get('function_name','')`，且覆盖 别名 / 推导。"""
        src = self._read("scripts", "game_thread_build.py.tmpl")
        self.assertNotIn("fn.get('function_name', '')", src,
                         "旧单层读法必须已被双层兼容替换")
        self.assertIn("fn.get('function_name')", src)
        self.assertIn("fn.get('name')", src)
        self.assertIn("target_class", src)          # 由 target_class 推导路径
        self.assertIn("【D-8】", src)                 # 冲突告警
        self.assertIn("缺少函数名", src)              # fail-loud

    def test_k7_sop_functions_row_aligned(self):
        src = self._read("docs", "SOP-PIPELINE.md")
        self.assertNotIn("🔴 **不一致**", src)
        self.assertIn("D-8", src)
        self.assertIn("T-20260917-D8-FUNCTIONS-CONTRACT", src)
        self.assertIn("已对齐", src)
        self.assertIn("_normalize_build_functions", src)

    def test_k7_runner_docstring_uses_primary_field(self):
        src = self._read("scripts", "ue5_runner.py")
        self.assertIn('"functions": [{"function_name": "PrintString"', src)
        self.assertNotIn('"functions": [{"name": "PrintString"', src)
        self.assertIn("兼容别名", src)

    def test_k7_build_endpoint_docstring_documents_contract(self):
        doc = ue5_bridge.build_blueprint.__doc__ or ""
        self.assertIn("functions[]", doc)
        self.assertIn("function_name", doc)
        self.assertIn("function_path", doc)
        self.assertIn("D-8", doc)
        self.assertIn("functions[i]", doc)

    def test_k7_scripts_readme_example_uses_primary_fields(self):
        src = self._read("scripts", "README.md")
        self.assertIn('"function_name": "PrintString"', src)
        self.assertIn('"event_name": "EventBeginPlay"', src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
