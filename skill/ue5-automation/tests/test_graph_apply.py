#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_graph_apply.py — 整图声明式写入（search_blueprint_nodes + apply_graph）
=====================================================================
T-20261024-UE5BRIDGE-GRAPH-APPLY（纯 Python 侧 · 离线）

背景（规格书：P1-整图声明式写入设计.md · UEBox 透析）：
  UEBox 删除了全部逐节点写图工具，第一条规则是 "Never build a graph one
  node at a time"（逐节点改图是上下文与轮次黑洞）。本文件锁定「查一批 →
  写一整张」主路径的契约，覆盖规格书测试计划全部 8 组：
  ① search_nodes      —— 事件/函数/流程控制命中；limit 截断；大小写不敏感；
                          引脚签名完整（可给签名的源必须给全；引擎库候选
                          如实缺省 + pins_note，绝不假装）
  ② apply_graph 正路径 —— 空图一次性建 3 节点 2 边；verified=true；编译状态回读
  ③ replace 守卫       —— 非空图不带 replace → 拒绝 + 返回图摘要；带 → 成功
  ④ 原子性            —— 中途连线失败 / 建节点失败 → 事务回滚 → 图与之前一致（读回断言）
  ⑤ 编译中守卫         —— bBeingCompiled → 409 语义错误，不做任何修改
  ⑥ 读回验证          —— 引擎悄悄多插隐式转换节点 → warning 而非失败（R1 口径）
  ⑦ MCP 契约          —— success 键存在；isError 行为与蓝图族一致；工具描述
                          交叉引用（UEBox 教训：改描述不同步 → 模型照旧文调错工具）
  ⑧ 布局确定性         —— 相同 spec 两次 apply → 节点坐标确定一致

设计决定（详见实现注释）：
  · GraphSpec 解析容错 = 宽容形态（缺省 edges/pin_defaults、kind 别名）、
    严格语义（id 唯一无点号、edge 引用必须已声明、未知 kind/target fail-loud）。
  · 事务边界 = 守卫/清图（失败不改动）→ 快照 → 建图连线设值（失败即回滚）
    → 提交 → 事务外编译 → 读回验证（不一致 fail-loud 报告）。
  · 回滚 = 显式追踪 + 逆序 remove_node + 复读确认（对齐 /build-batch 的
    「失败回滚已建资产」模式）。

层级：离线（UE5_BRIDGE_TEST_MODE=1 + fake_unreal 注入），不依赖 UE。
作者：dev（代码开发 Agent）· 日期：2026-10-24
"""

import json
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

TOKEN = "integration-test-token"

#: 规格书示例 spec（正路径 / 布局 / MCP 契约共用）：3 节点 2 边 1 默认值
SPEC_MAIN = {
    "graph": "EventGraph",
    "nodes": [
        {"id": "n1", "kind": "event", "target": "BeginPlay"},
        {"id": "n2", "kind": "flow", "target": "Branch"},
        {"id": "n3", "kind": "function", "target": "PrintString",
         "class": "KismetSystemLibrary"},
    ],
    "edges": [
        {"from": "n1.then", "to": "n2.execute"},
        {"from": "n2.then", "to": "n3.execute"},
    ],
    "pin_defaults": {"n3.InString": "Hello"},
}


def _make_spec(nodes, edges=None, defaults=None, graph="EventGraph"):
    return {"graph": graph, "nodes": nodes, "edges": edges or [],
            "pin_defaults": defaults or {}}


class _Base(unittest.TestCase):
    """公共夹具：fake 蓝图 + TestClient。"""

    def setUp(self):
        fu.reset_fake()
        self.client = TestClient(ue5_bridge.app)
        self.headers = {"X-Skill-Token": TOKEN}

    def _new_bp(self, path="/Game/Test/BP_GraphApply"):
        return fu.create_test_blueprint(path)

    def _cmd(self, name, **params):
        return self.client.post(
            "/command", json={"command": name, "params": params},
            headers=self.headers)

    def _mcp_call(self, name, **arguments):
        payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": name, "arguments": arguments}}
        resp = self.client.post("/mcp", json=payload, headers=self.headers)
        return resp.json()["result"]

    def _apply(self, bp_path, spec, replace=False):
        # T-20261027-GUARD：replace=true 属破坏性操作，需显式 confirm
        return self._cmd("apply_graph", bp_path=bp_path,
                         graph_spec=spec, replace=replace,
                         confirm=True).json()["result"]

    def _titles(self, bp, graph_name="EventGraph"):
        g = bp.graphs[graph_name]
        return [n.node_title for n in g.get_nodes()]


# ══════════════════════════════════════════════════════════
#  ① search_nodes：命中 / 截断 / 大小写 / 引脚签名完整性
# ══════════════════════════════════════════════════════════

class TestSearchBlueprintNodes(_Base):
    """① 只读搜索：多源命中 + 截断 + 签名完整性。"""

    def test_search_hits_event_function_flow(self):
        """事件目录 / 流程控制目录 / 引擎函数候选 / 图内已有节点 / 变量全命中。"""
        bp = self._new_bp()
        # 图内已有节点（带真实引脚面）
        g = bp.get_event_graph()
        fu.add_test_node(g, "PrintString",
                         pins=[("execute", "EGPD_Input", "exec"),
                               ("InString", "EGPD_Input", "string"),
                               ("then", "EGPD_Output", "exec")])
        # 蓝图变量（get/set 候选源）
        fu.BlueprintPythonBridge.add_member_variable(bp, "Health", "float")
        # 事件命中（Receive 前缀 + 友好名 Tick 都应命中 ReceiveTick）
        r = self._cmd("search_blueprint_nodes", bp_path="/Game/Test/BP_GraphApply",
                      query="receivebegin").json()["result"]
        self.assertTrue(r["success"], r)
        hits = {(e["kind"], e["target"]) for e in r["results"]}
        self.assertIn(("event", "ReceiveBeginPlay"), hits, hits)
        # 流程控制命中
        r = self._cmd("search_blueprint_nodes", bp_path="/Game/Test/BP_GraphApply",
                      query="foreach").json()["result"]
        self.assertTrue(any(e["kind"] == "flow" and e["target"] == "ForEachLoop"
                            for e in r["results"]), r["results"])
        # 函数候选命中（图内已有 + 引擎库）
        r = self._cmd("search_blueprint_nodes", bp_path="/Game/Test/BP_GraphApply",
                      query="printstring").json()["result"]
        sources = {(e["source"], e["target"]) for e in r["results"]}
        self.assertIn(("graph", "PrintString"), sources, sources)
        self.assertIn(("engine_class", "PrintString"), sources, sources)
        # 变量 get/set 双候选
        r = self._cmd("search_blueprint_nodes", bp_path="/Game/Test/BP_GraphApply",
                      query="health").json()["result"]
        kinds = {e["kind"] for e in r["results"]}
        self.assertEqual(kinds, {"variable_get", "variable_set"}, r["results"])

    def test_search_case_insensitive_and_friendly_alias(self):
        """大小写不敏感 + 事件友好名（Tick）命中标准名（ReceiveTick）。"""
        self._new_bp()
        for q in ("TICK", "Tick", "tIcK"):
            r = self._cmd("search_blueprint_nodes",
                          bp_path="/Game/Test/BP_GraphApply",
                          query=q).json()["result"]
            self.assertTrue(any(e["target"] == "ReceiveTick"
                                for e in r["results"]), (q, r["results"]))

    def test_search_pin_signature_complete(self):
        """可给签名的源 → 每条引脚五字段齐全；引擎库候选如实缺省 + 说明。"""
        bp = self._new_bp()
        g = bp.get_event_graph()
        # ⚠️ add_test_node 的 pins 只吃 (name, direction, category) 三元组
        #（4 元素会被忽略），默认值需建后补（fake 引脚 default_value 直改）
        delay = fu.add_test_node(g, "Delay",
                                 pins=[("execute", "EGPD_Input", "exec"),
                                       ("Duration", "EGPD_Input", "float"),
                                       ("then", "EGPD_Output", "exec")])
        delay.find_pin("Duration").default_value = "0.2"
        r = self._cmd("search_blueprint_nodes", bp_path="/Game/Test/BP_GraphApply",
                      query="delay").json()["result"]
        graph_hits = [e for e in r["results"] if e["source"] == "graph"]
        self.assertEqual(len(graph_hits), 1, r["results"])
        pins = graph_hits[0]["pins"]
        self.assertEqual(len(pins), 3, pins)
        for p in pins:
            for key in ("name", "type", "direction", "is_delegate",
                        "default_hint"):
                self.assertIn(key, p, p)
        self.assertEqual(pins[1]["type"], "float")
        self.assertEqual(pins[1]["default_hint"], "0.2")
        # 引擎库候选：签名如实空 + 显式说明（绝不假装给全）
        eng_hits = [e for e in r["results"] if e["source"] == "engine_class"]
        self.assertTrue(eng_hits, r["results"])
        for e in eng_hits:
            self.assertEqual(e["pins"], [])
            self.assertTrue(e.get("pins_note"), e)
            self.assertEqual(e["class"], "KismetSystemLibrary")
        # 流程控制目录条目签名完整（Branch 四引脚）
        r = self._cmd("search_blueprint_nodes", bp_path="/Game/Test/BP_GraphApply",
                      query="branch").json()["result"]
        flow = [e for e in r["results"] if e["source"] == "flow_catalog"][0]
        self.assertEqual({p["name"] for p in flow["pins"]},
                         {"execute", "Condition", "then", "else"})

    def test_search_own_function_graph_signature(self):
        """蓝图自有函数图 → source=own_function，入口参数引脚聚合。"""
        bp = self._new_bp()
        fgraph = bp.add_graph(fu.FakeGraph("MyFunc"))
        fu.BlueprintPythonBridge.add_function_pin_entry(fgraph, "InVal",
                                                        "float", True)
        r = self._cmd("search_blueprint_nodes", bp_path="/Game/Test/BP_GraphApply",
                      query="myfunc").json()["result"]
        own = [e for e in r["results"] if e["source"] == "own_function"]
        self.assertEqual(len(own), 1, r["results"])
        self.assertEqual(own[0]["kind"], "function")
        self.assertEqual(own[0]["class"], "BP_GraphApply")
        self.assertTrue(any(p["name"] == "InVal" for p in own[0]["pins"]),
                        own[0]["pins"])

    def test_search_limit_truncation_and_cap(self):
        """limit 截断（truncated 标记）+ 上限 50 收敛（warning 显式）。"""
        self._new_bp()
        r = self._cmd("search_blueprint_nodes", bp_path="/Game/Test/BP_GraphApply",
                      query="", limit=3).json()["result"]
        self.assertEqual(r["returned"], 3)
        self.assertTrue(r["truncated"])
        self.assertEqual(len(r["results"]), 3)
        self.assertEqual(r["total_matches"], 25)
        # 25 = 事件目录 8 + 流程目录 9 + 引擎候选 8
        #   （父类 Actor×3 + KismetSystemLibrary×3 + GameplayStatics×2，fake 面）
        # 空 query = 全量浏览；上限收敛（999 → 50 + warning）
        # 上限收敛（999 → 50 + warning）
        r = self._cmd("search_blueprint_nodes", bp_path="/Game/Test/BP_GraphApply",
                      query="", limit=999).json()["result"]
        self.assertEqual(r["returned"], 25)
        self.assertTrue(any("已收敛" in w for w in r["warnings"]), r["warnings"])

    def test_search_is_readonly(self):
        """只读纪律：搜索前后图节点数不变。"""
        bp = self._new_bp()
        before = len(bp.get_event_graph().get_nodes())
        self._cmd("search_blueprint_nodes", bp_path="/Game/Test/BP_GraphApply",
                  query="print").json()
        self.assertEqual(len(bp.get_event_graph().get_nodes()), before)

    def test_search_bp_path_required_fail_loud(self):
        data = self._cmd("search_blueprint_nodes").json()
        self.assertFalse(data["success"], "缺参必须 fail-loud")


# ══════════════════════════════════════════════════════════
#  ② apply_graph 正路径
# ══════════════════════════════════════════════════════════

class TestApplyGraphHappyPath(_Base):
    """② 正路径：空图一次性建 3 节点 2 边 + verified + 编译状态回读。"""

    def test_apply_builds_full_graph_atomically(self):
        bp = self._new_bp()
        r = self._apply("/Game/Test/BP_GraphApply", SPEC_MAIN)
        # 返回体键契约（success 语义与全族一致；verified 是一等信号）
        for key in ("success", "compiled", "compile_status", "verified",
                    "nodes_applied", "edges_applied", "errors", "warnings",
                    "node_guids"):
            self.assertIn(key, r, r)
        self.assertTrue(r["success"], r)
        self.assertTrue(r["compiled"])
        self.assertTrue(r["verified"])
        self.assertEqual(r["nodes_applied"], 3)
        self.assertEqual(r["edges_applied"], 2)
        self.assertEqual(r["compile_status"], "BS_Unknown")  # fake 蓝图状态可读
        # 读回断言（不采信自报）：节点真的在图里
        titles = self._titles(bp)
        self.assertEqual(len(titles), 3, titles)
        # GUID 锚点：可直接喂增量编辑工具（node_guids 与图内 GUID 同口径）
        g = bp.get_event_graph()
        guids = {ue5_bridge._pywrap_norm_guid(n.node_guid) for n in g.get_nodes()}
        self.assertEqual(set(r["node_guids"].values()), guids)

    def test_apply_wires_connections_and_defaults(self):
        """连线拓扑与 pin_defaults 真实落图（fake 蓝图层面断言，不采信返回体）。"""
        bp = self._new_bp()
        self._apply("/Game/Test/BP_GraphApply", SPEC_MAIN)
        g = bp.get_event_graph()
        conns = {(c.from_node_title, c.from_pin_name,
                  c.to_node_title, c.to_pin_name)
                 for c in fu.BlueprintPythonBridge.get_node_connections(g)}
        self.assertIn(("ReceiveBeginPlay", "then", "K2Node_IfThenElse", "execute"),
                      conns, conns)
        self.assertIn(("K2Node_IfThenElse", "then", "PrintString", "execute"),
                      conns, conns)
        printer = next(n for n in g.get_nodes() if n.node_title == "PrintString")
        self.assertEqual(printer.find_pin("InString").default_value, "Hello")

    def test_apply_event_alias_and_custom_event(self):
        """事件友好名（BeginPlay）确定性归一；非标准名 = 自定义事件。"""
        bp = self._new_bp()
        spec = _make_spec([
            {"id": "a", "kind": "event", "target": "BeginPlay"},
            {"id": "b", "kind": "event", "target": "OnFoo"},
        ])
        r = self._apply("/Game/Test/BP_GraphApply", spec)
        self.assertTrue(r["success"], r)
        titles = set(self._titles(bp))
        self.assertIn("ReceiveBeginPlay", titles)
        # 自定义事件标题形如 "<name>\n自定义事件"（对齐 add_custom_event_node）
        self.assertTrue(any(t.startswith("OnFoo") for t in titles), titles)

    def test_apply_variable_get_set(self):
        """variable_get/set：变量存在 → 建节点；不存在 → fail-loud + 回滚。"""
        bp = self._new_bp()
        fu.BlueprintPythonBridge.add_member_variable(bp, "Health", "float")
        spec = _make_spec([
            {"id": "v", "kind": "get", "target": "Health"},   # kind 别名
            {"id": "s", "kind": "set", "target": "Health"},
        ])
        r = self._apply("/Game/Test/BP_GraphApply", spec)
        self.assertTrue(r["success"], r)
        classes = {n.node_class for n in bp.get_event_graph().get_nodes()}
        self.assertEqual(classes, {"K2Node_VariableGet", "K2Node_VariableSet"})

    def test_apply_unknown_variable_fails_loud_on_fresh_graph(self):
        """不存在的变量 → fail-loud（绝不静默建空节点）+ 事务回滚。

        用全新蓝图（守卫次序：非空图拒绝先于节点解析 —— 上一个用例 apply
        过的蓝图会先撞 replace 守卫，那是 ③ 组语义，不属本组）。
        """
        self._new_bp("/Game/Test/BP_VarMiss")
        bad = _make_spec([{"id": "x", "kind": "variable_get", "target": "Mana"}])
        r = self._apply("/Game/Test/BP_VarMiss", bad)
        self.assertFalse(r["success"], r)
        self.assertEqual(r.get("category"), "param_type")
        self.assertIn("不存在变量", r["error"])
        self.assertTrue(r["rollback"]["clean"])
        self.assertEqual(
            len(fu.get_asset("/Game/Test/BP_VarMiss").get_event_graph().get_nodes()),
            0)

    def test_apply_function_without_class_uses_own_graph(self):
        """function 无 class：同名函数图存在 → 归到本蓝图；否则 fail-loud。"""
        bp = self._new_bp()
        bp.add_graph(fu.FakeGraph("MyFunc"))
        spec = _make_spec([{"id": "f", "kind": "function", "target": "MyFunc"}])
        r = self._apply("/Game/Test/BP_GraphApply", spec)
        self.assertTrue(r["success"], r)
        # 无同名图且无 class → 可读错误（指明加 class / 用 类:函数 形态）
        # （用新蓝图：上一段 apply 后图非空，会先撞 replace 守卫 —— ③ 组语义）
        self._new_bp("/Game/Test/BP_NoOwnFn")
        spec2 = _make_spec([{"id": "f", "kind": "function",
                             "target": "NoSuchFn"}])
        r = self._apply("/Game/Test/BP_NoOwnFn", spec2)
        self.assertFalse(r["success"])
        self.assertIn("class", r["error"])

    def test_apply_function_k2_candidate_retry_warns(self):
        """K2_ 候选重试（与 add_function_node 同款确定性归一）→ 显式告警。"""
        bp = self._new_bp()
        spec = _make_spec([{"id": "f", "kind": "function",
                            "target": "K2_PrintString",
                            "class": "KismetSystemLibrary"}])
        r = self._apply("/Game/Test/BP_GraphApply", spec)
        self.assertTrue(r["success"], r)
        self.assertTrue(any("候选" in w for w in r["warnings"]), r["warnings"])


# ══════════════════════════════════════════════════════════
#  ③ replace 守卫
# ══════════════════════════════════════════════════════════

class TestReplaceGuard(_Base):
    """③ 整图写入是替换语义 + 明确守卫，不是静默合并。"""

    def test_non_empty_graph_rejected_with_summary(self):
        """非空图不带 replace → 拒绝 + 返回当前图摘要（先 read 再决定）。"""
        bp = self._new_bp()
        fu.add_test_node(bp.get_event_graph(), "OldLogic")
        r = self._apply("/Game/Test/BP_GraphApply", SPEC_MAIN)
        self.assertFalse(r["success"], r)
        self.assertEqual(r.get("category"), "graph_not_empty")
        summary = r["graph_summary"]
        self.assertEqual(summary["node_count"], 1)
        self.assertIn("OldLogic", summary["node_titles"])
        # 图未被改动
        self.assertEqual(self._titles(bp), ["OldLogic"])

    def test_replace_true_clears_then_applies(self):
        bp = self._new_bp()
        g = bp.get_event_graph()
        fu.add_test_node(g, "OldA")
        fu.add_test_node(g, "OldB")
        r = self._apply("/Game/Test/BP_GraphApply", SPEC_MAIN, replace=True)
        self.assertTrue(r["success"], r)
        self.assertTrue(any("replace=true 已清空 2 个旧节点" in w
                            for w in r["warnings"]), r["warnings"])
        self.assertEqual(len(self._titles(bp)), 3)

    def test_function_graph_replace_preserves_structural_nodes(self):
        """非 EventGraph：replace 清图保留 FunctionEntry/Result（删掉就废了）。"""
        bp = self._new_bp()
        fgraph = bp.add_graph(fu.FakeGraph("MyFunc"))
        fu.BlueprintPythonBridge.add_function_pin_entry(fgraph, "InVal",
                                                        "float", True)
        fu.add_test_node(fgraph, "JunkNode")
        spec = _make_spec([{"id": "f", "kind": "function", "target": "MyFunc"}],
                          graph="MyFunc")
        r = self._apply("/Game/Test/BP_GraphApply", spec, replace=True)
        self.assertTrue(r["success"], r)
        kept = self._titles(bp, "MyFunc")
        self.assertIn("K2Node_FunctionEntry", kept, kept)
        self.assertNotIn("JunkNode", kept)

    def test_function_graph_empty_check_ignores_structural(self):
        """函数图只有入口/出口 → 视为空图（无需 replace 即可写）。"""
        bp = self._new_bp()
        fgraph = bp.add_graph(fu.FakeGraph("MyFunc"))
        fu.BlueprintPythonBridge.add_function_pin_entry(fgraph, "InVal",
                                                        "float", True)
        spec = _make_spec([{"id": "f", "kind": "function", "target": "MyFunc"}],
                          graph="MyFunc")
        r = self._apply("/Game/Test/BP_GraphApply", spec)
        self.assertTrue(r["success"], r)


# ══════════════════════════════════════════════════════════
#  ④ 原子性：中途失败 → 事务回滚 → 图与之前一致
# ══════════════════════════════════════════════════════════

class TestAtomicRollback(_Base):
    """④ 事务边界：守卫/解析失败零改动；建图连线失败 → 逆序回滚 + 复读确认。"""

    def test_bad_spec_never_touches_graph(self):
        """解析失败（非法 kind / 未声明 id / id 含点）→ fail-loud + 零改动。"""
        bp = self._new_bp()
        bad_specs = [
            _make_spec([{"id": "a", "kind": "wat", "target": "X"}]),
            _make_spec([{"id": "a.b", "kind": "flow", "target": "Branch"}]),
            _make_spec([{"id": "a", "kind": "flow", "target": "Branch"}],
                       edges=[{"from": "ghost.then", "to": "a.execute"}]),
            _make_spec([{"id": "a", "kind": "flow", "target": "NoSuchMacro"}]),
            _make_spec([{"id": "a", "kind": "flow", "target": ""}]),
        ]
        for i, spec in enumerate(bad_specs):
            with self.subTest(i=i, spec=spec):
                r = self._apply("/Game/Test/BP_GraphApply", spec)
                self.assertFalse(r["success"], r)
                self.assertEqual(r.get("category"), "param_type", r)
                self.assertEqual(len(bp.get_event_graph().get_nodes()), 0,
                                 "解析失败不得改动图")
        # graph_spec 缺参（None）同样 fail-loud
        data = self._cmd("apply_graph", bp_path="/Game/Test/BP_GraphApply").json()
        self.assertFalse(data["success"])

    def test_connect_failure_rolls_back_everything(self):
        """连线失败（引脚不存在）→ 回滚 → 图与之前一致（读回断言）。"""
        bp = self._new_bp()
        bad = _make_spec(
            [{"id": "n1", "kind": "event", "target": "BeginPlay"},
             {"id": "n2", "kind": "function", "target": "PrintString",
              "class": "KismetSystemLibrary"}],
            edges=[{"from": "n1.then", "to": "n2.execute"},
                   {"from": "n2.then", "to": "n1.NoSuchPin"}])
        r = self._apply("/Game/Test/BP_GraphApply", bad)
        self.assertFalse(r["success"], r)
        self.assertEqual(r.get("category"), "pin_failure")
        rb = r["rollback"]
        self.assertTrue(rb["attempted"])
        self.assertEqual(sorted(rb["removed"]), ["n1", "n2"], rb)
        self.assertTrue(rb["clean"], rb)
        # 读回断言：图与之前一致（空）
        self.assertEqual(len(bp.get_event_graph().get_nodes()), 0)
        self.assertEqual(len(bp.get_event_graph().get_nodes()), 0)

    def test_node_ctor_failure_rolls_back(self):
        """建节点失败（DLL 返回 None 注入）→ 已建的兄弟节点一并回滚。"""
        bp = self._new_bp()
        fu.BlueprintPythonBridge.L2_FAIL_NODE_CTOR = "add_function_call_node"
        try:
            r = self._apply("/Game/Test/BP_GraphApply", SPEC_MAIN)
            self.assertFalse(r["success"], r)
            self.assertTrue(r["rollback"]["clean"], r["rollback"])
            self.assertEqual(len(bp.get_event_graph().get_nodes()), 0)
        finally:
            fu.BlueprintPythonBridge.L2_FAIL_NODE_CTOR = None

    def test_pin_default_failure_rolls_back(self):
        """pin_defaults 引脚不存在 → 回滚（设值发生在连线之后，仍在事务内）。"""
        bp = self._new_bp()
        bad = _make_spec(
            [{"id": "n1", "kind": "event", "target": "BeginPlay"}],
            defaults={"n1.NoSuchPin": "1"})
        r = self._apply("/Game/Test/BP_GraphApply", bad)
        self.assertFalse(r["success"], r)
        self.assertIn("设置引脚默认值失败", r["error"])
        self.assertTrue(r["rollback"]["clean"])
        self.assertEqual(len(bp.get_event_graph().get_nodes()), 0)

    def test_partial_then_second_apply_succeeds(self):
        """失败后图状态干净 → 同蓝图可立即重试（回滚不留残迹）。"""
        bp = self._new_bp()
        bad = _make_spec(
            [{"id": "n1", "kind": "event", "target": "BeginPlay"},
             {"id": "n2", "kind": "function", "target": "PrintString",
              "class": "KismetSystemLibrary"}],
            edges=[{"from": "n1.then", "to": "n2.BadPin"}])
        r = self._apply("/Game/Test/BP_GraphApply", bad)
        self.assertFalse(r["success"])
        r = self._apply("/Game/Test/BP_GraphApply", SPEC_MAIN)
        self.assertTrue(r["success"], r)


# ══════════════════════════════════════════════════════════
#  ⑤ 编译中守卫（409 语义）
# ══════════════════════════════════════════════════════════

class TestBeingCompiledGuard(_Base):
    """⑤ bBeingCompiled → 409 Conflict 语义错误，不做任何修改。"""

    def test_compiling_blueprint_rejected_with_409_semantics(self):
        bp = self._new_bp()
        fu.set_being_compiled(True)
        try:
            r = self._apply("/Game/Test/BP_GraphApply", SPEC_MAIN)
            self.assertFalse(r["success"], r)
            self.assertEqual(r.get("category"), "graph_conflict")
            self.assertIn("409", r["error"], "错误文本须点明 409 语义")
            self.assertIn("bBeingCompiled", r["error"])
            # 不做任何修改
            self.assertEqual(len(bp.get_event_graph().get_nodes()), 0)
        finally:
            fu.set_being_compiled(False)

    def test_replace_does_not_bypass_compiling_guard(self):
        """守卫先于 replace 清图（编译中清图同样是破坏）。"""
        bp = self._new_bp()
        fu.add_test_node(bp.get_event_graph(), "Keep")
        fu.set_being_compiled(True)
        try:
            r = self._apply("/Game/Test/BP_GraphApply", SPEC_MAIN, replace=True)
            self.assertFalse(r["success"])
            self.assertEqual(r.get("category"), "graph_conflict")
            self.assertEqual(self._titles(bp), ["Keep"])
        finally:
            fu.set_being_compiled(False)


# ══════════════════════════════════════════════════════════
#  ⑥ 读回验证：隐式转换节点 → warning 而非失败
# ══════════════════════════════════════════════════════════

class TestReadbackVerification(_Base):
    """⑥ verified 语义对齐材质族 v0.20 口径：非致命告警不拉低 verified。"""

    def test_engine_inserted_converter_is_warning_not_failure(self):
        """引擎悄悄多插隐式转换节点 → warning + verified=true。"""
        bp = self._new_bp()
        fu.set_graph_apply_insert_converter(True)
        try:
            r = self._apply("/Game/Test/BP_GraphApply", SPEC_MAIN)
            self.assertTrue(r["success"], r)
            self.assertTrue(r["verified"], "R1 口径：多出转换节点不算失败")
            self.assertTrue(
                any("隐式转换" in w and "不判失败" in w for w in r["warnings"]),
                r["warnings"])
            # 转换节点真的在图里（5 = 3 spec 节点 + 2 转换节点；
            # fake 注入口按每次成功连线各插一个，模拟引擎逐连线插入的行为）
            self.assertEqual(len(bp.get_event_graph().get_nodes()), 5)
        finally:
            fu.set_graph_apply_insert_converter(False)

    def test_duplicate_titles_degrade_edge_verification(self):
        """同名节点 → 拓扑（按标题）比对降级为引脚级 + 显式告警（宁缺毋滥）。"""
        bp = self._new_bp()
        spec = _make_spec(
            [{"id": "a", "kind": "function", "target": "PrintString",
              "class": "KismetSystemLibrary"},
             {"id": "b", "kind": "function", "target": "PrintString",
              "class": "KismetSystemLibrary"}],
            edges=[{"from": "a.then", "to": "b.execute"}])
        r = self._apply("/Game/Test/BP_GraphApply", spec)
        self.assertTrue(r["success"], r)
        self.assertTrue(any("同名节点" in w for w in r["warnings"]),
                        r["warnings"])


# ══════════════════════════════════════════════════════════
#  ⑦ MCP 契约：success 键 + isError 行为 + 目录计数 + 交叉引用
# ══════════════════════════════════════════════════════════

class TestMcpContract(_Base):
    """⑦ MCP 层：isError 契约 + 工具目录计数 + 描述交叉引用。"""

    def test_whitelist_count_75(self):
        self.assertEqual(len(ue5_bridge._COMMAND_WHITELIST), 101,
                         "v6.5：95 + 多 Agent/AnimBP 6 条")
        for cmd in ("search_blueprint_nodes", "apply_graph"):
            self.assertIn(cmd, ue5_bridge._COMMAND_WHITELIST)

    def test_mcp_tools_list_26(self):
        payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        tools = self.client.post("/mcp", json=payload,
                                 headers=self.headers).json()["result"]["tools"]
        names = {t["name"] for t in tools}
        # T-20261028-TOOL-RESTORE：默认全量 34
        self.assertEqual(len(tools), 34, "tools/list 默认全量 34")
        self.assertIn("ue5_search_blueprint_nodes", names)
        self.assertIn("ue5_apply_blueprint_graph", names)
        self.assertIn("ue5_screenshot", names)

    def test_tool_descriptions_cross_reference(self):
        """UEBox 教训：新增/改名工具必须互相交叉引用，否则模型照旧文调错工具。"""
        payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        tools = {t["name"]: t["description"]
                 for t in self.client.post(
                     "/mcp", json=payload,
                     headers=self.headers).json()["result"]["tools"]}
        self.assertIn("ue5_apply_blueprint_graph",
                      tools["ue5_search_blueprint_nodes"])
        self.assertIn("ue5_search_blueprint_nodes",
                      tools["ue5_apply_blueprint_graph"])
        self.assertIn("ue5_connect_pins",
                      tools["ue5_apply_blueprint_graph"],
                      "逐节点工具（手术式微调）须在整图工具描述中被引用")

    def test_apply_success_is_error_false_and_success_key_present(self):
        bp = self._new_bp()
        result = self._mcp_call("ue5_apply_blueprint_graph",
                                bp_path="/Game/Test/BP_GraphApply",
                                graph_spec=SPEC_MAIN)
        self.assertFalse(result["isError"])
        body = json.loads(result["content"][0]["text"])
        # success 键必须存在（_mcp_handle_one 的 isError 判据依赖）
        self.assertIn("success", body)
        self.assertTrue(body["success"])
        self.assertTrue(body["verified"])

    def test_apply_compile_fail_is_error_true(self):
        """编译失败 → success=false → isError=True（蓝图族同款行为）。"""
        self._new_bp()
        fu.set_compile_result(False)
        try:
            result = self._mcp_call("ue5_apply_blueprint_graph",
                                    bp_path="/Game/Test/BP_GraphApply",
                                    graph_spec=SPEC_MAIN)
            self.assertTrue(result["isError"], "编译失败必须 isError=True")
            body = json.loads(result["content"][0]["text"])
            self.assertFalse(body["success"])
            self.assertFalse(body["compiled"])
            self.assertTrue(body["verified"])  # 图写对了，只是编译不过
            self.assertTrue(body["error"])     # 指向 §3.6 修复循环
        finally:
            fu.set_compile_result(True)

    def test_apply_guard_failure_is_error_true(self):
        """守卫拒绝（409 / 非空图）→ isError=True。"""
        bp = self._new_bp()
        fu.add_test_node(bp.get_event_graph(), "Old")
        result = self._mcp_call("ue5_apply_blueprint_graph",
                                bp_path="/Game/Test/BP_GraphApply",
                                graph_spec=SPEC_MAIN)
        self.assertTrue(result["isError"])

    def test_search_via_mcp(self):
        self._new_bp()
        result = self._mcp_call("ue5_search_blueprint_nodes",
                                bp_path="/Game/Test/BP_GraphApply",
                                query="branch")
        self.assertFalse(result["isError"])
        body = json.loads(result["content"][0]["text"])
        self.assertTrue(body["success"])
        self.assertTrue(any(e["target"] == "Branch" for e in body["results"]))


# ══════════════════════════════════════════════════════════
#  ⑧ 布局确定性
# ══════════════════════════════════════════════════════════

class TestLayoutDeterminism(_Base):
    """⑧ 相同 spec 两次 apply → 节点坐标确定一致（拓扑序网格，纯 Python）。"""

    def test_same_spec_same_coordinates(self):
        pos_maps = []
        for path in ("/Game/Test/BP_LayA", "/Game/Test/BP_LayB"):
            self._new_bp(path)
            r = self._apply(path, SPEC_MAIN)
            self.assertTrue(r["success"], r)
            bp = fu.get_asset(path)
            # 按 spec 声明序取坐标（fake 建序 = spec 序）
            g = bp.get_event_graph()
            pos_maps.append([(n.node_title, n.pos_x, n.pos_y)
                             for n in g.get_nodes()])
        self.assertEqual(pos_maps[0], pos_maps[1])

    def test_layout_is_topological_grid(self):
        """事件（层 0）→ Branch（层 1）→ PrintString（层 2）: 列 = 300 × 层。"""
        bp = self._new_bp()
        self._apply("/Game/Test/BP_GraphApply", SPEC_MAIN)
        g = bp.get_event_graph()
        pos = {n.node_title: (n.pos_x, n.pos_y) for n in g.get_nodes()}
        self.assertEqual(pos["ReceiveBeginPlay"], (0, 0))
        self.assertEqual(pos["K2Node_IfThenElse"], (300, 0))
        self.assertEqual(pos["PrintString"], (600, 0))

    def test_explicit_pos_overrides_layout(self):
        """显式 pos 的节点跳过布局（模型自定坐标优先）。"""
        bp = self._new_bp()
        spec = _make_spec([
            {"id": "a", "kind": "event", "target": "BeginPlay", "pos": [42, 84]},
            {"id": "b", "kind": "flow", "target": "Branch"},
        ], edges=[{"from": "a.then", "to": "b.execute"}])
        r = self._apply("/Game/Test/BP_GraphApply", spec)
        self.assertTrue(r["success"], r)
        pos = {n.node_title: (n.pos_x, n.pos_y)
               for n in bp.get_event_graph().get_nodes()}
        self.assertEqual(pos["ReceiveBeginPlay"], (42, 84))
        self.assertEqual(pos["K2Node_IfThenElse"], (300, 0))  # 未指定的照常布局

    def test_layout_pure_python_no_elkjs(self):
        """许可边界（规格书）：布局不得引入 elkjs（EPL-2.0）。"""
        self.assertNotIn("elkjs", sys.modules)


if __name__ == "__main__":
    unittest.main(verbosity=2)
