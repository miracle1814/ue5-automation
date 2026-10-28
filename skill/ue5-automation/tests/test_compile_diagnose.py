#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_compile_diagnose.py — compile_and_diagnose 结构化编译诊断（离线）
=====================================================================
T-20261024-UE5BRIDGE-COMPILE-DIAGNOSE（纯 Python 侧 · 离线）

背景（规格书：P0-编译报错自动修复闭环设计.md）：
  工具只提供「结构化的错误 + 可定位的锚点」，修复决策交给模型 —— 不搞
  启发式自动修（启发式会制造假成功，与 fail-loud 纪律冲突）。本文件锁定：
  ① 报错行解析器单测（22 条真实风格样本，表驱动）：
     中文/英文引擎、带路径/短名、`_C` 类后缀、函数缺省、节点缺省、
     状态机子图多上下文、无标记降级、多错误混合、重复行去重。
  ② 降级纪律（"Unknown ≠ 失败" 的反面 = "编造 ≠ 诊断"）：
     日志读不到 → errors=[] + warnings 明示原因（绝不编造错误）；
     GUID 映射不到 / 同名标题歧义（PTN-018 教训）→ node_guid=null + warning。
  ③ 规则映射：命中 error_patterns → code / pitfall_ref / fix_hint；
     未命中 → code=CompileError.Unclassified + pitfall_ref/fix_hint=null。
  ④ MCP isError 契约：编译失败 → success=false 且 isError=True
     （`success` 键必须存在的既有约定，v0.20 补丁的延伸回归面）。
  ⑤ 白名单 72→73 / MCP 工具 23→24 的目录计数回归。

层级：离线（UE5_BRIDGE_TEST_MODE=1 + fake_unreal 注入），不依赖 UE。
日志目录用 tempfile 写真实临时文件（fake_unreal.Paths.set_log_dir 注入）。
作者：dev（代码开发 Agent）· 日期：2026-10-24
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

# ── 必须在 import ue5_bridge 之前设置环境变量 ──
os.environ["UE5_BRIDGE_TEST_MODE"] = "1"
os.environ["UE5_BRIDGE_TOKEN"] = "integration-test-token"

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(SKILL_ROOT, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import pytest  # noqa: E402
import ue5_bridge            # noqa: E402  真实模块（测试模式 → fake_unreal 注入）
import fake_unreal as fu     # noqa: E402  test double
from fastapi.testclient import TestClient  # noqa: E402

BP = "/Game/Test/BP_Diag"
TOKEN = "integration-test-token"


@pytest.fixture(autouse=True)
def _pin_token(monkeypatch):
    """token 现读化：不依赖模块导入顺序（T-20261025-UX）。

    本文件靠模块级 env 设 token，若其它测试模块先导入 ue5_bridge（token 已按
    文件值定住），组合跑会 401。autouse patch 让每条用例回到本文件的 token。
    """
    monkeypatch.setattr(ue5_bridge, "_BRIDGE_TOKEN", TOKEN)


# ══════════════════════════════════════════════════════════
#  ① 解析器单测（表驱动 · 22 条真实风格样本）
# ══════════════════════════════════════════════════════════

class TestParseErrorContext(unittest.TestCase):
    """方括号上下文 → (asset, function, node_name) 三元组。

    样本全部对齐两种已知真实格式（任务规格给定）：
      中文引擎： [资产 函数 节点 节点名]
      英文引擎： [/Game/Dir/资产.资产 函数 Node 节点名]
    解析不出 → None（绝不编造）。
    """

    #: (方括号内容, 期望 asset, 期望 function, 期望 node_name, 说明)
    SAMPLES = [
        # ── 中文引擎（资产短名 + "节点" 标记）──
        ("BP_Pickup OnOverlap 节点 Branch",
         "BP_Pickup", "OnOverlap", "Branch",
         "中文引擎标准格式"),
        ("BP_Enemy ReceiveBeginOverlap 节点 Sequence",
         "BP_Enemy", "ReceiveBeginOverlap", "Sequence",
         "中文引擎事件函数上下文"),
        ("BP_Inv OnOverlap 节点 分支",
         "BP_Inv", "OnOverlap", "分支",
         "中文引擎本地化节点标题"),
        ("BP_Pickup OnOverlap 节点 Sequence 2",
         "BP_Pickup", "OnOverlap", "Sequence 2",
         "节点标题含空格 → 标记后整段保留"),
        # ── 英文引擎（带路径资产 + "Node" 标记）──
        ("/Game/Blueprints/BP_Pickup.BP_Pickup OnOverlap Node Branch",
         "/Game/Blueprints/BP_Pickup", "OnOverlap", "Branch",
         "英文引擎带路径（归一化去掉 .ClassName 段）"),
        ("/Game/Chars/BP_Enemy.BP_Enemy_C OnOverlap Node Branch",
         "/Game/Chars/BP_Enemy", "OnOverlap", "Branch",
         "英文引擎带路径 _C 类后缀（.ClassName 段整体剥离）"),
        ("/Game/AI/BP_Bot.BP_Bot ChasePlayer Node Sequence",
         "/Game/AI/BP_Bot", "ChasePlayer", "Sequence",
         "英文引擎自定义函数上下文"),
        # ── 短名原样保留（不做 _C 剥离 —— 资产名本身可能叫 X_C）──
        ("BP_Door_C OnOverlap 节点 Branch",
         "BP_Door_C", "OnOverlap", "Branch",
         "中文引擎短名原样保留（资产名本身可能以 _C 结尾，绝不改写）"),
        # ── 函数名缺省 ──
        ("BP_Pickup 节点 Branch",
         "BP_Pickup", None, "Branch",
         "中文引擎函数缺省"),
        ("/Game/X/BP_A.BP_A Node Branch",
         "/Game/X/BP_A", None, "Branch",
         "英文引擎函数缺省"),
        # ── 节点名缺省 ──
        ("BP_Pickup OnOverlap",
         "BP_Pickup", "OnOverlap", None,
         "节点缺省（仅函数上下文）"),
        ("/Game/X/BP_A.BP_A OnOverlap",
         "/Game/X/BP_A", "OnOverlap", None,
         "英文引擎节点缺省"),
        # ── 仅资产 ──
        ("BP_Pickup",
         "BP_Pickup", None, None,
         "仅资产（类级报错，无函数/节点上下文）"),
        ("/Game/X/BP_A.BP_A",
         "/Game/X/BP_A", None, None,
         "英文引擎仅资产"),
        # ── 无"节点/Node"标记 → 不猜节点（降级纪律）──
        ("BP_Pickup OnOverlap Branch",
         "BP_Pickup", "OnOverlap", None,
         "无标记 3 token：Branch 可能是函数参数上下文也可能是节点名，"
         "无法判定 → 不猜（信息保留在 message 原文）"),
        # ── 状态机子图（标记前多个上下文 token）──
        ("BP_Player StateMove 节点 MoveState",
         "BP_Player", "StateMove", "MoveState",
         "状态机子图：function 取资产后第一个上下文名（字段语义=函数/图上下文）"),
        ("/Game/Player/BP_Bot.BP_Bot ChaseTree Node Attack",
         "/Game/Player/BP_Bot", "ChaseTree", "Attack",
         "行为树/子图多上下文（英文）"),
        # ── 空/畸形上下文 ──
        ("", None, None, None,
         "空上下文 → 全 null"),
        ("   ",
         None, None, None,
         "纯空白 → 全 null"),
        ("/Game/X/BP_A.BP_A_C",
         "/Game/X/BP_A", None, None,
         "英文引擎仅资产带 _C 类后缀"),
    ]

    def test_samples_table(self):
        self.assertGreaterEqual(len(self.SAMPLES), 20, "样本数必须 ≥20（规格书测试计划）")
        for ctx, want_asset, want_fn, want_node, note in self.SAMPLES:
            with self.subTest(note=note, ctx=ctx):
                info = ue5_bridge._parse_bp_error_context(ctx)
                self.assertEqual(info["asset"], want_asset, note)
                self.assertEqual(info["function"], want_fn, note)
                self.assertEqual(info["node_name"], want_node, note)


class TestExtractBlueprintErrors(unittest.TestCase):
    """行级抽取：完整日志行 → 结构化错误列表（不注入 bp → 无 GUID 通道）。"""

    def test_standard_lines_cn_and_en(self):
        lines = [
            "LogBlueprint: Warning: Compile of BP_Pickup failed!",  # 非目标类别 → 忽略
            "LogBlueprintUserMessages: [BP_Pickup OnOverlap 节点 Branch] "
            "Condition 引脚需要 bool 连接",
            "LogBlueprintUserMessages: [/Game/Blueprints/BP_Door.BP_Door "
            "OnOverlap Node Branch] Missing Asset /Game/Mesh/Cube",
        ]
        errors, warnings = ue5_bridge._extract_blueprint_errors(lines, bp=None)
        self.assertEqual(len(errors), 2, "非 LogBlueprintUserMessages 行不入 errors")
        self.assertEqual(errors[0]["asset"], "BP_Pickup")
        self.assertEqual(errors[0]["function"], "OnOverlap")
        self.assertEqual(errors[0]["node_name"], "Branch")
        self.assertEqual(errors[0]["node_guid"], None, "无 bp → GUID 通道不启用 → null")
        self.assertEqual(errors[0]["message"], "Condition 引脚需要 bool 连接")
        self.assertEqual(errors[1]["asset"], "/Game/Blueprints/BP_Door")

    def test_bracket_in_message_not_confused(self):
        # 报错正文里也含 ] —— 正则按首个 ] 截断上下文，正文完整保留
        line = ("LogBlueprintUserMessages: [BP_X OnOverlap 节点 Branch] "
                "Cannot find variable 'Health' (array [int])")
        errors, _ = ue5_bridge._extract_blueprint_errors([line], bp=None)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["message"],
                         "Cannot find variable 'Health' (array [int])")
        self.assertEqual(errors[0]["node_name"], "Branch")

    def test_pin_hint_extraction(self):
        for msg, want in [
            ("Pin 'Condition' must be of type bool", "Condition"),
            ("引脚 '条件' 需要 bool 类型", "条件"),
        ]:
            with self.subTest(msg=msg):
                line = f"LogBlueprintUserMessages: [BP_X OnOverlap 节点 Branch] {msg}"
                errors, _ = ue5_bridge._extract_blueprint_errors([line], bp=None)
                self.assertEqual(errors[0]["pin_hint"], want)

    def test_v58_localized_error_line(self):
        """5.8 变体（T-20261025-VERIFY 真机实锤）：LogBlueprint: Error: [AssetLog]
        路径：[编译器]中文正文 —— 资产取自 [AssetLog]，正文完整保留。"""
        line = ("LogBlueprint: Error: [AssetLog]/Game/Blueprints/BP_DiagTest.BP_DiagTest："
                "[编译器]不能连接引脚  Score  和  In String ：Convert 整数 to 字符串")
        errors, warnings = ue5_bridge._extract_blueprint_errors([line], bp=None)
        self.assertEqual(len(errors), 1, errors)
        e = errors[0]
        self.assertEqual(e["asset"], "/Game/Blueprints/BP_DiagTest")
        self.assertIn("不能连接引脚", e["message"])
        self.assertIn("Convert 整数 to 字符串", e["message"])
        self.assertEqual(e["node_name"], None)   # 行内无节点标记 → 不编造

    def test_v51_assetlog_variant(self):
        """5.1 变体（T-20261025-VERIFY 真机实锤）：[AssetLog] 后带空格、
        资产后挂 "(no disk path found)"、半角冒号、英文 [Compiler] 上下文。"""
        line = ("LogBlueprint: Error: [AssetLog] /Game/Blueprints/BP_DiagTest51."
                "BP_DiagTest51 (no disk path found): [Compiler] 不能连接引脚  "
                "Score  和  In String")
        errors, _ = ue5_bridge._extract_blueprint_errors([line], bp=None)
        self.assertEqual(len(errors), 1, errors)
        e = errors[0]
        self.assertEqual(e["asset"], "/Game/Blueprints/BP_DiagTest51")
        self.assertIn("不能连接引脚", e["message"])
        self.assertEqual(e["node_name"], None)   # [Compiler] 非节点标记 → 不编造

    def test_english_engine_line_variant(self):
        """英文引擎样本（T-20261025-UX）：结构同中文，正文英文；
        正则须同样命中并保留完整 message（海外用户主路径）。"""
        line = ("LogBlueprint: Error: [AssetLog] /Game/Blueprints/BP_En.BP_En: "
                "[Compiler] Cannot connect pins Score and In String: "
                "Convert Integer to String")
        errors, _ = ue5_bridge._extract_blueprint_errors([line], bp=None)
        self.assertEqual(len(errors), 1, errors)
        e = errors[0]
        self.assertEqual(e["asset"], "/Game/Blueprints/BP_En")
        self.assertIn("Cannot connect pins", e["message"])

    def test_no_pin_hint_when_unquoted(self):
        # 无引号的 "Pin Condition" 歧义过大 → 宁缺毋滥
        line = ("LogBlueprintUserMessages: [BP_X OnOverlap 节点 Branch] "
                "Pin Condition input must be bool")
        errors, _ = ue5_bridge._extract_blueprint_errors([line], bp=None)
        self.assertNotIn("pin_hint", errors[0])

    def test_multiple_errors_mixed_engines(self):
        lines = [
            "LogTemp: Warning: unrelated noise",  # 噪声
            "LogBlueprintUserMessages: [BP_A OnOverlap 节点 Branch] err one",
            "LogBlueprintUserMessages: [/Game/X/BP_B.BP_B Tick Node Seq] err two",
            "LogBlueprintUserMessages: [BP_C] err three",
        ]
        errors, _ = ue5_bridge._extract_blueprint_errors(lines, bp=None)
        self.assertEqual([e["asset"] for e in errors], ["BP_A", "/Game/X/BP_B", "BP_C"])
        self.assertEqual(errors[0]["message"], "err one")

    def test_duplicate_lines_dedup(self):
        # 重复编译会把同一报错留在日志尾部多份 → 按整行去重
        line = "LogBlueprintUserMessages: [BP_A OnOverlap 节点 Branch] boom"
        errors, _ = ue5_bridge._extract_blueprint_errors([line, line, line], bp=None)
        self.assertEqual(len(errors), 1)

    def test_empty_and_bracket_only_lines(self):
        lines = [
            "LogBlueprintUserMessages: [BP_A]",                         # 无正文 → 跳过
            "LogBlueprintUserMessages: [BP_A OnOverlap]   ",            # 无正文 → 跳过
            "LogBlueprintUserMessages: [BP_A OnOverlap 节点 Branch] x",  # 带正文 → 保留
        ]
        errors, _ = ue5_bridge._extract_blueprint_errors(lines, bp=None)
        self.assertEqual(len(errors), 1,
                         "仅上下文无正文的行跳过；带正文的保留")
        self.assertEqual(errors[0]["node_name"], "Branch")

    def test_unmatched_pattern_falls_back_to_unclassified(self):
        line = ("LogBlueprintUserMessages: [BP_X OnOverlap 节点 Branch] "
                "some totally unknown compiler text")
        errors, _ = ue5_bridge._extract_blueprint_errors([line], bp=None)
        self.assertEqual(errors[0]["code"], "CompileError.Unclassified")
        self.assertEqual(errors[0]["pitfall_ref"], None)
        self.assertEqual(errors[0]["fix_hint"], None)


# ══════════════════════════════════════════════════════════
#  ②③ 集成：编译 + 诊断（fake 资产 + 真实临时日志文件）
# ══════════════════════════════════════════════════════════

class _Base(unittest.TestCase):
    """公共夹具：fake 蓝图 + TestClient + 可注入日志目录。"""

    def setUp(self):
        fu.reset_fake()
        self.bp = fu.create_test_blueprint(BP)
        self.client = TestClient(ue5_bridge.app)
        self.headers = {"X-Skill-Token": TOKEN}

    def _write_log(self, lines):
        """往临时日志目录写 Project.log（真实文件，经 fake Paths 注入）。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        log_path = os.path.join(tmp.name, "Project.log")
        with open(log_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        fu.Paths.set_log_dir(tmp.name)
        return tmp.name

    def _cmd(self, name, **params):
        return self.client.post(
            "/command", json={"command": name, "params": params},
            headers=self.headers)

    def _mcp_call(self, name, **arguments):
        payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": name, "arguments": arguments}}
        resp = self.client.post("/mcp", json=payload, headers=self.headers)
        return resp.json()["result"]


class TestCompileAndDiagnoseCommand(_Base):
    """/command compile_and_diagnose 返回体与降级纪律。"""

    def test_return_shape_and_success_semantics(self):
        r = self._cmd("compile_and_diagnose", bp_path=BP).json()
        self.assertTrue(r["success"], r)
        result = r["result"]
        # 返回体键契约（success 语义与 compile_blueprint 一致）
        for key in ("success", "compiled", "compile_status",
                    "errors", "warnings", "fix_hint_note"):
            self.assertIn(key, result)
        self.assertTrue(result["success"])
        self.assertTrue(result["compiled"])
        self.assertEqual(result["errors"], [])
        # fake 蓝图 status="Unknown" 可读 → BS_Unknown（真实引擎 status 不可读，
        # 走 compiled 派生 → BS_UpToDate/BS_Error，见 _bp_status_str 注释）
        self.assertEqual(result["compile_status"], "BS_Unknown")

    def test_structured_errors_with_guid_mapping(self):
        graph = self.bp.get_event_graph()
        branch = fu.add_test_node(graph, "Branch")
        fu.add_test_node(graph, "Sequence")
        self._write_log([
            "LogBlueprintUserMessages: [BP_Diag OnOverlap 节点 Branch] bad wire",
            "LogBlueprintUserMessages: [BP_Diag OnOverlap 节点 Sequence] bad wire 2",
        ])
        result = self._cmd("compile_and_diagnose", bp_path=BP).json()["result"]
        self.assertEqual(len(result["errors"]), 2)
        by_name = {e["node_name"]: e for e in result["errors"]}
        # node_guid 口径 = _pywrap_norm_guid（32 位大写无连字符 hex）—— 与
        # get_node_by_guid / read_nodes.guid 直通（增量编辑工具的定位键格式）
        self.assertEqual(by_name["Branch"]["node_guid"],
                         ue5_bridge._pywrap_norm_guid(branch.node_guid),
                         "节点名 → GUID 映射（修复闭环的定位键）")
        self.assertTrue(by_name["Sequence"]["node_guid"])
        # 全部字段键契约
        for e in result["errors"]:
            for key in ("code", "message", "asset", "function", "node_guid",
                        "node_name", "pitfall_ref", "fix_hint"):
                self.assertIn(key, e)

    def test_guid_null_when_node_not_in_graph(self):
        self._write_log([
            "LogBlueprintUserMessages: [BP_Diag OnOverlap 节点 Ghost] boom",
        ])
        result = self._cmd("compile_and_diagnose", bp_path=BP).json()["result"]
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["errors"][0]["node_guid"], None,
                         "图内无此节点 → null（绝不编造 GUID）")
        self.assertEqual(result["errors"][0]["node_name"], "Ghost")

    def test_ambiguous_title_degrades_to_null_with_warning(self):
        # PTN-018 教训：同名节点仅按标题定位不可靠 → 歧义标题整条剔除
        graph = self.bp.get_event_graph()
        fu.add_test_node(graph, "Seq")
        fu.add_test_node(graph, "Seq")  # 同名第二个（异 GUID）
        self._write_log([
            "LogBlueprintUserMessages: [BP_Diag OnOverlap 节点 Seq] boom",
        ])
        result = self._cmd("compile_and_diagnose", bp_path=BP).json()["result"]
        self.assertEqual(result["errors"][0]["node_guid"], None)
        self.assertTrue(any("同名节点标题歧义" in w for w in result["warnings"]),
                        result["warnings"])

    def test_log_unreadable_degrades_to_empty_with_warning(self):
        # 日志目录不存在 → errors=[]（不是编造），warnings 明示原因
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        fu.Paths.set_log_dir(os.path.join(tmp.name, "no_such_dir"))
        result = self._cmd("compile_and_diagnose", bp_path=BP).json()["result"]
        self.assertEqual(result["errors"], [])
        self.assertTrue(any("日志不可读" in w for w in result["warnings"]),
                        result["warnings"])
        # 编译结论不受日志读取影响（fail-loud：不因诊断通道失联改判编译）
        self.assertTrue(result["success"])

    def test_log_dir_without_log_file_also_warns(self):
        # 目录存在但无 .log → 同样必须让调用方知道 errors 为空的原因
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        fu.Paths.set_log_dir(tmp.name)
        result = self._cmd("compile_and_diagnose", bp_path=BP).json()["result"]
        self.assertEqual(result["errors"], [])
        self.assertTrue(any("无 .log 文件" in w for w in result["warnings"]),
                        result["warnings"])

    def test_extraction_independent_of_compile_result(self):
        # 编译失败 + 日志有错误 → errors 尽力给全；编译结论独立成立
        fu.set_compile_result(False)
        graph = self.bp.get_event_graph()
        branch = fu.add_test_node(graph, "Branch")
        self._write_log([
            "LogBlueprintUserMessages: [BP_Diag OnOverlap 节点 Branch] bad wire",
        ])
        result = self._cmd("compile_and_diagnose", bp_path=BP).json()["result"]
        self.assertFalse(result["success"])
        self.assertFalse(result["compiled"])
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["errors"][0]["node_guid"],
                         ue5_bridge._pywrap_norm_guid(branch.node_guid))

    def test_bp_path_required_fail_loud(self):
        data = self._cmd("compile_and_diagnose").json()
        self.assertFalse(data["success"], "缺参必须 fail-loud（可读错误，非假成功）")


class TestRuleMapping(_Base):
    """③ 规则映射：error_patterns 命中 → code/pitfall_ref/fix_hint。"""

    def test_hit_pattern_yields_pitfall_ref_and_fix_hint(self):
        # "Missing Asset" 命中 PTN-010（引用资产丢失）
        self._write_log([
            "LogBlueprintUserMessages: [BP_Diag OnOverlap 节点 Branch] "
            "Missing Asset /Game/Mesh/Cube",
        ])
        result = self._cmd("compile_and_diagnose", bp_path=BP).json()["result"]
        e = result["errors"][0]
        self.assertEqual(e["pitfall_ref"], "PTN-010")
        self.assertEqual(e["code"], "E-BP-REF-NULL-ASSET")
        self.assertTrue(e["fix_hint"])
        self.assertEqual(result["fix_hint_note"], "fix_hint 仅供参考，不代替模型判断")
        # log_matches（公共扫描）同样命中
        self.assertIn("log_matches", result)
        self.assertEqual(result["log_matches"][0]["pattern_id"], "PTN-010")

    def test_circular_dependency_pattern(self):
        self._write_log([
            "LogBlueprintUserMessages: [BP_Diag OnOverlap Node Branch] "
            "cyclic reference between BP_A and BP_B",
        ])
        result = self._cmd("compile_and_diagnose", bp_path=BP).json()["result"]
        e = result["errors"][0]
        self.assertEqual(e["pitfall_ref"], "PTN-012")
        self.assertEqual(e["code"], "E-BP-CIRCULAR-DEPENDENCY")

    def test_no_match_means_null(self):
        self._write_log([
            "LogBlueprintUserMessages: [BP_Diag OnOverlap 节点 Branch] "
            "some totally unknown compiler text",
        ])
        result = self._cmd("compile_and_diagnose", bp_path=BP).json()["result"]
        e = result["errors"][0]
        self.assertEqual(e["code"], "CompileError.Unclassified")
        self.assertEqual(e["pitfall_ref"], None)
        self.assertEqual(e["fix_hint"], None)


class TestMcpContract(_Base):
    """④⑤ MCP 层：isError 契约 + 工具目录计数。"""

    def test_whitelist_count_73(self):
        # 白名单 75→78（T-20261024-UE5BRIDGE-SCREENSHOT 追加截图三命令；
        # 历史沿袭的计数回归，本批在此同步）
        self.assertEqual(len(ue5_bridge._COMMAND_WHITELIST), 101,
                         "v6.5：95 + 多 Agent/AnimBP 6 条（锁×3 + 回执 + AnimBP×2）")
        self.assertIn("compile_and_diagnose", ue5_bridge._COMMAND_WHITELIST)

    def test_mcp_tools_list_24(self):
        payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        tools = self.client.post("/mcp", json=payload,
                                 headers=self.headers).json()["result"]["tools"]
        names = {t["name"] for t in tools}
        # T-20261028-TOOL-RESTORE：默认恢复全量目录（31），能力可发现性优先；
        # 需要收窄的客户端用 UE5_MCP_CORE_TOOLS=1。
        self.assertEqual(len(tools), 34,
                         "tools/list 默认 = 全量 34 工具")
        self.assertIn("ue5_compile_and_diagnose", names)

    def test_compile_fail_is_error_true(self):
        # 编译失败 → success=false → MCP isError=True（v0.20 契约的延伸回归面）
        fu.set_compile_result(False)
        self._write_log([
            "LogBlueprintUserMessages: [BP_Diag OnOverlap 节点 Branch] bad wire",
        ])
        result = self._mcp_call("ue5_compile_and_diagnose", bp_path=BP)
        self.assertTrue(result["isError"], "编译失败必须 isError=True")
        body = json.loads(result["content"][0]["text"])
        self.assertFalse(body["success"])
        self.assertFalse(body["compiled"])
        self.assertEqual(len(body["errors"]), 1)

    def test_compile_ok_is_error_false(self):
        self._write_log([])
        result = self._mcp_call("ue5_compile_and_diagnose", bp_path=BP)
        self.assertFalse(result["isError"])
        body = json.loads(result["content"][0]["text"])
        self.assertTrue(body["success"])

    def test_bridge_unavailable_guid_channel_degrades(self):
        # C++ Bridge 缺失（老 DLL 场景）→ GUID 通道整体降级 + 告警，不阻断诊断
        self._write_log([
            "LogBlueprintUserMessages: [BP_Diag OnOverlap 节点 Branch] boom",
        ])
        with mock.patch.object(ue5_bridge, "BRIDGE_AVAILABLE", False):
            result = self._mcp_call("ue5_compile_and_diagnose", bp_path=BP)
            self.assertFalse(result["isError"])
        body = json.loads(result["content"][0]["text"])
        self.assertEqual(body["errors"][0]["node_guid"], None)
        self.assertTrue(any("GUID 通道不可用" in w for w in body["warnings"]),
                        body["warnings"])

    def test_compile_blueprint_regression_success_key(self):
        # 回归面：公共函数抽取后 compile_blueprint 返回体键不变（v0.20 契约）
        self._write_log([])
        data = self._cmd("compile_blueprint", bp_path=BP).json()
        result = data["result"]
        self.assertTrue(data["success"])
        self.assertIn("success", result)
        self.assertTrue(result["success"])
        self.assertIn("compile_status", result)


if __name__ == "__main__":
    unittest.main()
