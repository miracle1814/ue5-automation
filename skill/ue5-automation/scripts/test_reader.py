#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_reader.py — blueprint_reader 离线测试套件
================================================
覆盖 blueprint_reader.py 全部公开 API，共 13 项测试。
独立运行：python test_reader.py

已知教训（来自 KB + 编码前语义搜索）：
  - 不硬编码绝对路径（用 __file__ 推导）
  - 写入后必须验证文件完整性
  - 不使用 rm/del 清理（测试用 tempfile 自动回收）
"""

import json
import os
import sys
import io
import tempfile
import subprocess
import unittest
from unittest.mock import patch, MagicMock, PropertyMock
from pathlib import Path

# 确保可以导入 blueprint_reader（与 test_reader.py 同级）
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from blueprint_reader import (
    BlueprintReader, BlueprintAnalysis, BlueprintBasicInfo,
    PinInfo, NodeInfo, ConnectionInfo, GraphAnalysis,
    BlueprintVariable, AssetReference, DegradeLevel,
    analyze_blueprint, quick_inspect,
)


# ═══════════════════════════════════════════════════════════
# 测试数据工厂
# ═══════════════════════════════════════════════════════════

def _make_l1_ok_result():
    """构造一个完整的 L1 OK 探针返回。"""
    return {
        "ok": True,
        "level": "L1",
        "basic_info": {
            "name": "BP_TestActor",
            "asset_path": "/Game/Test/BP_TestActor",
            "parent_class": "Actor",
            "blueprint_type": "Blueprint",
            "interfaces": [],
            "node_count_total": 3,
            "graph_count": 1,
        },
        "variables": [
            {"var_name": "Health", "var_type": "Float", "category": "Combat",
             "default_value": "100.0", "is_exposed": True, "is_array": False},
            {"var_name": "Name", "var_type": "String", "category": "Default",
             "default_value": "", "is_exposed": True, "is_array": False},
        ],
        "graphs": {
            "EventGraph": {
                "graph_name": "EventGraph",
                "node_count": 3,
                "nodes": [
                    {
                        "title": "BeginPlay",
                        "node_class": "K2Node_Event",
                        "node_guid": "abc-001",
                        "is_event": True,
                        "is_pure": False,
                        "pins": [
                            {"name": "then", "direction": "Output", "pin_type": "exec",
                             "is_connected": True, "is_exec": True, "default_value": ""},
                        ],
                    },
                    {
                        "title": "PrintString",
                        "node_class": "K2Node_CallFunction",
                        "node_guid": "abc-002",
                        "is_event": False,
                        "is_pure": False,
                        "pins": [
                            {"name": "execute", "direction": "Input", "pin_type": "exec",
                             "is_connected": True, "is_exec": True, "default_value": ""},
                            {"name": "InString", "direction": "Input", "pin_type": "string",
                             "is_connected": False, "is_exec": False, "default_value": "Hello"},
                        ],
                    },
                    {
                        "title": "GetPlayerController",
                        "node_class": "K2Node_CallFunction",
                        "node_guid": "abc-003",
                        "is_event": False,
                        "is_pure": True,
                        "pins": [
                            {"name": "ReturnValue", "direction": "Output", "pin_type": "object",
                             "is_connected": True, "is_exec": False, "default_value": ""},
                        ],
                    },
                ],
            }
        },
        "connections": [
            {"from_node": "BeginPlay", "from_pin": "then", "from_guid": "abc-001",
             "to_node": "PrintString", "to_pin": "execute", "to_guid": "abc-002"},
        ],
        "errors": [],
        "warnings": [],
    }


def _make_minimal_l3_result():
    """构造一个最小 L3 探针返回。"""
    return {
        "ok": True,
        "level": "L3",
        "basic_info": {
            "name": "BP_Minimal",
            "asset_path": "/Game/Test/BP_Minimal",
            "parent_class": "Object",
            "blueprint_type": "Blueprint",
            "interfaces": [],
            "node_count_total": 0,
            "graph_count": 0,
        },
        "variables": [],
        "graphs": {},
        "connections": [],
        "errors": [],
        "warnings": [],
    }


# ═══════════════════════════════════════════════════════════
# 测试用例
# ═══════════════════════════════════════════════════════════

class TestBlueprintReader(unittest.TestCase):
    """blueprint_reader.py 离线测试套件 — 13 项"""

    def setUp(self):
        """每个测试前创建 BlueprintReader 实例。"""
        self.reader = BlueprintReader(mode="remote")
        self.reader.ue_send_path = os.path.join(SCRIPT_DIR, "ue_send.py")

    # ── 测试 1：analyze() 基本调用 ──

    def test_01_analyze_basic(self):
        """analyze() — 基本调用，mock _run_probe 返回 L1 OK。"""
        mock_result = _make_l1_ok_result()
        with patch.object(self.reader, '_run_probe', return_value=mock_result):
            analysis = self.reader.analyze("/Game/Test/BP_TestActor")

        self.assertIsInstance(analysis, BlueprintAnalysis)
        self.assertEqual(analysis.basic_info.name, "BP_TestActor")
        self.assertEqual(analysis.degrade_level, DegradeLevel.L1_NATIVE)
        self.assertEqual(len(analysis.variables), 2)
        self.assertEqual(analysis.basic_info.node_count_total, 3)
        self.assertEqual(len(analysis.graphs), 1)
        self.assertIn("EventGraph", analysis.graphs)
        self.assertEqual(len(analysis.connections), 1)
        self.assertEqual(analysis.connections[0].from_node, "BeginPlay")

    # ── 测试 2：_parse_probe_output 解析 __JSON_RESULT__ 标记 ──

    def test_02_parse_probe_output(self):
        """_parse_probe_output() — 正确解析 __JSON_RESULT__ ... __JSON_END__ 标记。"""
        reader = BlueprintReader(mode="embedded")

        # 正常情况
        output = 'some log\n__JSON_RESULT__{"ok": true, "level": "L1"}__JSON_END__\nmore log'
        result = reader._parse_probe_output(output)
        self.assertIsNotNone(result)
        self.assertTrue(result["ok"])
        self.assertEqual(result["level"], "L1")

        # 无标记 → None
        self.assertIsNone(reader._parse_probe_output("no markers here"))

        # 只有开始标记 → None
        self.assertIsNone(reader._parse_probe_output("__JSON_RESULT__incomplete"))

        # 无效 JSON → None
        self.assertIsNone(reader._parse_probe_output("__JSON_RESULT__not json__JSON_END__"))

        # 空 JSON 对象
        result = reader._parse_probe_output("__JSON_RESULT__{}__JSON_END__")
        self.assertEqual(result, {})

    # ── 测试 3：_exec_embedded mock（sys.stdout 劫持验证） ──

    def test_03_exec_embedded(self):
        """_exec_embedded() — sys.stdout 劫持 + JSON 输出捕获。"""
        reader = BlueprintReader(mode="embedded")
        # 构造一个不依赖 unreal 的最小探针代码
        code = (
            "import json\n"
            "result = {'ok': True, 'from': 'embedded_test', 'value': 42}\n"
            "print('__JSON_RESULT__' + json.dumps(result) + '__JSON_END__')\n"
        )
        result = reader._exec_embedded(code)
        self.assertIsNotNone(result)
        self.assertTrue(result["ok"])
        self.assertEqual(result["from"], "embedded_test")
        self.assertEqual(result["value"], 42)

    def test_03b_exec_embedded_exception(self):
        """_exec_embedded() — 异常捕获，不再静默吞噬。"""
        reader = BlueprintReader(mode="embedded")
        code = "raise RuntimeError('test_error_xyz')"
        result = reader._exec_embedded(code)
        self.assertFalse(result["ok"])
        self.assertIn("RuntimeError", result["errors"][0])
        self.assertIn("test_error_xyz", result["errors"][0])

    def test_03c_exec_embedded_no_output(self):
        """_exec_embedded() — 无 stdout 输出时返回默认错误。"""
        reader = BlueprintReader(mode="embedded")
        code = "x = 1 + 1"  # 不打印任何东西
        result = reader._exec_embedded(code)
        self.assertFalse(result["ok"])
        self.assertIn("无输出", result["errors"][0])

    # ── 测试 4：_exec_remote mock（subprocess.run 验证） ──

    def test_04_exec_remote(self):
        """_exec_remote() — mock subprocess.run，验证参数传递。"""
        reader = BlueprintReader(mode="remote")
        reader.ue_send_path = os.path.join(SCRIPT_DIR, "ue_send.py")

        mock_output = '__JSON_RESULT__{"ok": true, "remote": true}__JSON_END__'
        mock_process = MagicMock()
        mock_process.stdout = mock_output
        mock_process.stderr = ""
        mock_process.returncode = 0

        with patch('blueprint_reader.subprocess.run', return_value=mock_process) as mock_run:
            code = "print('test')"
            result = reader._exec_remote(code)

        # 验证结果正确
        self.assertIsNotNone(result)
        self.assertTrue(result["ok"])
        self.assertTrue(result["remote"])

        # 验证 subprocess.run 被调用且参数正确
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        self.assertIn("ue_send.py", call_args[1])  # sys.executable
        self.assertIn("exec(open(", call_args[2])  # 执行的代码

    def test_04b_exec_remote_timeout(self):
        """_exec_remote() — 超时返回 None。"""
        reader = BlueprintReader(mode="remote")
        reader.ue_send_path = os.path.join(SCRIPT_DIR, "ue_send.py")

        with patch('blueprint_reader.subprocess.run', side_effect=subprocess.TimeoutExpired("cmd", 30)):
            result = reader._exec_remote("print('slow')")
        self.assertIsNone(result)

    # ── 测试 5：BlueprintBasicInfo 数据完整性 ──

    def test_05_blueprint_basic_info(self):
        """BlueprintBasicInfo — 所有字段默认值与赋值。"""
        bi = BlueprintBasicInfo(
            name="BP_Test",
            asset_path="/Game/BP_Test",
            parent_class="Actor",
        )
        self.assertEqual(bi.name, "BP_Test")
        self.assertEqual(bi.asset_path, "/Game/BP_Test")
        self.assertEqual(bi.parent_class, "Actor")
        self.assertEqual(bi.blueprint_type, "")          # 默认值
        self.assertEqual(bi.interfaces, [])               # 默认值
        self.assertEqual(bi.node_count_total, 0)          # 默认值
        self.assertEqual(bi.graph_count, 0)               # 默认值

        # 完整赋值
        bi2 = BlueprintBasicInfo(
            name="BP_Widget",
            asset_path="/Game/UI/WBP_Main",
            parent_class="UserWidget",
            blueprint_type="WidgetBlueprint",
            interfaces=["BI_Interactable"],
            last_modified="2026-07-24",
            node_count_total=42,
            graph_count=3,
        )
        self.assertEqual(bi2.blueprint_type, "WidgetBlueprint")
        self.assertEqual(bi2.interfaces, ["BI_Interactable"])
        self.assertEqual(bi2.node_count_total, 42)

    # ── 测试 6：PinInfo / NodeInfo / ConnectionInfo 数据类 ──

    def test_06_pin_node_connection(self):
        """PinInfo / NodeInfo / ConnectionInfo — 字段读写与默认值。"""
        # PinInfo
        pin = PinInfo(name="Execute", direction="Input", pin_type="exec", is_exec=True)
        self.assertEqual(pin.name, "Execute")
        self.assertTrue(pin.is_exec)
        self.assertFalse(pin.is_connected)  # 默认值

        pin2 = PinInfo(name="ReturnValue", direction="Output", is_connected=True, default_value="42")
        self.assertEqual(pin2.direction, "Output")
        self.assertEqual(pin2.default_value, "42")

        # NodeInfo
        node = NodeInfo(
            title="BeginPlay",
            node_class="K2Node_Event",
            is_event=True,
            pins=[pin, pin2],
        )
        self.assertEqual(node.title, "BeginPlay")
        self.assertTrue(node.is_event)
        self.assertFalse(node.is_pure)
        self.assertEqual(len(node.pins), 2)
        self.assertEqual(node.function_name, "")  # 默认值

        # ConnectionInfo
        conn = ConnectionInfo(
            from_node="BeginPlay",
            from_pin="then",
            to_node="PrintString",
            to_pin="execute",
        )
        self.assertEqual(conn.from_node, "BeginPlay")
        self.assertEqual(conn.to_node, "PrintString")

    # ── 测试 7：GraphAnalysis 嵌套结构 ──

    def test_07_graph_analysis(self):
        """GraphAnalysis — 嵌套 NodeInfo / PinInfo 结构。"""
        pins = [
            PinInfo(name="then", direction="Output", pin_type="exec", is_exec=True),
            PinInfo(name="InString", direction="Input", pin_type="string"),
        ]
        nodes = [
            NodeInfo(title="BeginPlay", node_class="K2Node_Event", pins=pins[:1]),
            NodeInfo(title="PrintString", node_class="K2Node_CallFunction", pins=pins[1:]),
        ]
        graph = GraphAnalysis(
            graph_name="EventGraph",
            graph_type="EventGraph",
            node_count=2,
            nodes=nodes,
            connections=[
                ConnectionInfo(from_node="BeginPlay", from_pin="then",
                               to_node="PrintString", to_pin="execute"),
            ],
        )
        self.assertEqual(graph.graph_name, "EventGraph")
        self.assertEqual(graph.node_count, 2)
        self.assertEqual(len(graph.nodes), 2)
        self.assertEqual(len(graph.connections), 1)

        # 验证嵌套引脚
        self.assertEqual(graph.nodes[0].pins[0].name, "then")
        self.assertTrue(graph.nodes[0].pins[0].is_exec)

    # ── 测试 8：BlueprintVariable 数据类 ──

    def test_08_blueprint_variable(self):
        """BlueprintVariable — 变量字段与默认值。"""
        v = BlueprintVariable(
            var_name="Health",
            var_type="Float",
            default_value="100.0",
            category="Combat",
        )
        self.assertEqual(v.var_name, "Health")
        self.assertEqual(v.var_type, "Float")
        self.assertTrue(v.is_exposed)     # 默认值
        self.assertFalse(v.is_array)      # 默认值
        self.assertTrue(v.is_editable)    # 默认值

        # 数组变量
        v2 = BlueprintVariable(
            var_name="Items",
            var_type="Array",
            is_array=True,
            is_exposed=False,
        )
        self.assertTrue(v2.is_array)
        self.assertFalse(v2.is_exposed)

    # ── 测试 9：AssetReference 数据类 ──

    def test_09_asset_reference(self):
        """AssetReference — 引用资产字段。"""
        ref = AssetReference(
            asset_path="/Game/Blueprints/BP_Enemy",
            asset_type="CastTo",
            source_node="CastToEnemy",
        )
        self.assertEqual(ref.asset_path, "/Game/Blueprints/BP_Enemy")
        self.assertEqual(ref.asset_type, "CastTo")
        self.assertFalse(ref.is_suspicious)   # 默认值
        self.assertEqual(ref.note, "")          # 默认值

        # 可疑引用
        ref2 = AssetReference(
            asset_path="/Engine/SomethingSuspicious",
            asset_type="CastTo",
            source_node="CastNode",
            is_suspicious=True,
            note="路径指向引擎内部资产",
        )
        self.assertTrue(ref2.is_suspicious)
        self.assertIn("引擎", ref2.note)

    # ── 测试 10：三级降级链路（L1→L2→L3 fallback） ──

    def test_10_degradation_L1_to_L2(self):
        """三级降级 — L1 失败 → L2 成功。"""
        self.reader.mode = "remote"
        l1_fail = {"ok": False, "level": "L1", "errors": ["L1 failed"], "basic_info": {}}
        l2_ok = {
            "ok": True, "level": "L2",
            "basic_info": {"name": "BP_L2", "asset_path": "/Game/BP_L2",
                           "parent_class": "Actor", "blueprint_type": "Blueprint",
                           "interfaces": [], "node_count_total": 5, "graph_count": 1},
            "variables": [], "graphs": {"EventGraph": {"nodes": []}}, "connections": [], "errors": [], "warnings": [],
        }

        with patch.object(self.reader, '_run_probe', side_effect=[l1_fail, l2_ok]):
            analysis = self.reader.analyze("/Game/BP_L2")

        self.assertEqual(analysis.degrade_level, DegradeLevel.L2_BRIDGE)
        self.assertEqual(analysis.basic_info.name, "BP_L2")

    def test_10b_degradation_full_fallback_to_L3(self):
        """三级降级 — L1 + L2 失败 → L3 成功。"""
        self.reader.mode = "remote"
        l1_fail = {"ok": False, "level": "L1", "errors": ["L1 failed"], "basic_info": {}}
        l2_fail = {"ok": False, "level": "L2", "errors": ["L2 failed"], "basic_info": {}}
        l3_min = _make_minimal_l3_result()

        with patch.object(self.reader, '_run_probe', side_effect=[l1_fail, l2_fail, l3_min]):
            analysis = self.reader.analyze("/Game/BP_Minimal")

        self.assertEqual(analysis.degrade_level, DegradeLevel.L3_MINIMAL)
        self.assertEqual(analysis.basic_info.name, "BP_Minimal")
        self.assertIn("L1_NATIVE", analysis.warnings[0] if analysis.warnings else "")

    def test_10c_degradation_all_fail(self):
        """三级降级 — 三级全部失败。"""
        self.reader.mode = "remote"
        all_fail = {"ok": False, "level": "L3", "errors": ["All failed"], "basic_info": {}}

        with patch.object(self.reader, '_run_probe', side_effect=[all_fail, all_fail, all_fail]):
            analysis = self.reader.analyze("/Game/BP_Gone")

        # 应该包含三级全部失败的错误
        self.assertGreaterEqual(len(analysis.errors), 3)
        self.assertIn("三级降级全部失败", " ".join(analysis.errors))

    # ── 测试 11：save_report() 文件写入 ──

    def test_11_save_report(self):
        """save_report() — 验证文件写入与完整性。"""
        result = _make_l1_ok_result()
        with patch.object(self.reader, '_run_probe', return_value=result):
            analysis = self.reader.analyze("/Game/Test/BP_TestActor")

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = os.path.join(tmpdir, "test_report.md")
            saved = self.reader.save_report(analysis, output_path)

            # 验证返回路径
            self.assertEqual(saved, output_path)

            # 验证文件存在且非空
            self.assertTrue(os.path.isfile(output_path))
            with open(output_path, "r", encoding="utf-8") as f:
                content = f.read()
            self.assertIn("蓝图分析报告", content)
            self.assertIn("BP_TestActor", content)
            self.assertIn("## 1. 基本信息", content)
            self.assertIn("## 2. 变量清单", content)
            self.assertIn("Health", content)

            # 验证 JSON 文件也被写入
            json_path = output_path.replace(".md", ".json")
            self.assertTrue(os.path.isfile(json_path))
            with open(json_path, "r", encoding="utf-8") as f:
                json_content = json.load(f)
            self.assertIn("basic_info", json_content)

    # ── 测试 12：to_markdown_summary() 输出 ──

    def test_12_to_markdown_summary(self):
        """to_markdown_summary() — Markdown 摘要输出验证。"""
        result = _make_l1_ok_result()
        with patch.object(self.reader, '_run_probe', return_value=result):
            analysis = self.reader.analyze("/Game/Test/BP_TestActor")

        md = self.reader.to_markdown_summary(analysis)

        # 核心内容检查
        self.assertIn("# 蓝图分析报告", md)
        self.assertIn("BP_TestActor", md)
        self.assertIn("## 1. 基本信息", md)
        self.assertIn("## 2. 变量清单", md)
        self.assertIn("## 3. 节点摘要", md)
        self.assertIn("## 连线拓扑", md)
        self.assertIn("Actor", md)                       # parent_class
        self.assertIn("Health", md)                      # variable
        self.assertIn("Float", md)                       # var type
        self.assertIn("BeginPlay", md)                   # node
        self.assertIn("EventGraph", md)                  # graph name

        # 降级层级标注
        self.assertIn(DegradeLevel.L1_NATIVE.name, md)

        # 连线表格
        self.assertIn("PrintString", md)

    def test_12b_to_markdown_empty(self):
        """to_markdown_summary() — 空蓝图（无变量、无节点）。"""
        analysis = BlueprintAnalysis(
            basic_info=BlueprintBasicInfo(
                name="BP_Empty",
                asset_path="/Game/BP_Empty",
                parent_class="Object",
            ),
            degrade_level=DegradeLevel.L3_MINIMAL,
        )
        md = self.reader.to_markdown_summary(analysis)
        self.assertIn("BP_Empty", md)
        self.assertIn("*(无变量)*", md)
        self.assertIn("L3_MINIMAL", md)

    # ── 测试 13：空蓝图 / 损坏数据边界情况 ──

    def test_13_empty_graph_corner_case(self):
        """边界情况 — 空 graphs、空 variables、空 connections。"""
        empty_result = {
            "ok": True,
            "level": "L1",
            "basic_info": {
                "name": "BP_Shell",
                "asset_path": "/Game/BP_Shell",
                "parent_class": "Actor",
                "blueprint_type": "Blueprint",
                "interfaces": [],
                "node_count_total": 0,
                "graph_count": 0,
            },
            "variables": [],
            "graphs": {},
            "connections": [],
            "errors": [],
            "warnings": [],
        }

        with patch.object(self.reader, '_run_probe', return_value=empty_result):
            analysis = self.reader.analyze("/Game/BP_Shell")

        self.assertEqual(analysis.basic_info.name, "BP_Shell")
        self.assertEqual(len(analysis.variables), 0)
        self.assertEqual(len(analysis.graphs), 0)
        self.assertEqual(len(analysis.connections), 0)
        self.assertEqual(analysis.basic_info.node_count_total, 0)

        # to_markdown_summary 不应崩溃
        md = self.reader.to_markdown_summary(analysis)
        self.assertIsInstance(md, str)

        # to_json / to_dict 不应崩溃
        j = analysis.to_json()
        self.assertIn("BP_Shell", j)

    def test_13b_missing_basic_info_fields(self):
        """边界情况 — basic_info 缺少部分字段。"""
        partial_result = {
            "ok": True,
            "level": "L1",
            "basic_info": {
                # 只有 name，缺少其他字段
                "name": "BP_Partial",
            },
            "variables": [{"var_name": "X", "var_type": "Int"}],  # 缺少部分字段
            "graphs": {
                "EventGraph": {
                    "graph_name": "EventGraph",
                    "node_count": 1,
                    "nodes": [{
                        "title": "Tick",
                        "node_class": "K2Node_Event",
                        # 缺少 node_guid, pins 等
                    }],
                }
            },
            "connections": [{
                "from_node": "Tick",
                "from_pin": "then",
                # 缺少其他字段
            }],
            "errors": [],
            "warnings": [],
        }

        with patch.object(self.reader, '_run_probe', return_value=partial_result):
            analysis = self.reader.analyze("/Game/BP_Partial")

        # 不应崩溃，缺失字段使用默认值
        self.assertEqual(analysis.basic_info.name, "BP_Partial")
        self.assertEqual(analysis.basic_info.parent_class, "Unknown")  # 默认值
        self.assertEqual(len(analysis.variables), 1)
        self.assertEqual(analysis.variables[0].var_name, "X")
        self.assertEqual(analysis.variables[0].category, "Default")  # 默认值

        graph = analysis.graphs.get("EventGraph")
        self.assertIsNotNone(graph)
        self.assertEqual(graph.nodes[0].title, "Tick")
        self.assertEqual(len(graph.nodes[0].pins), 0)  # 缺少 pins 时为空列表

    def test_13c_none_probe_return(self):
        """边界情况 — _run_probe 返回 None（探针无响应）。"""
        self.reader.mode = "remote"

        with patch.object(self.reader, '_run_probe', return_value=None):
            analysis = self.reader.analyze("/Game/BP_Ghost")

        # 应该记录错误
        self.assertGreater(len(analysis.errors), 0)
        self.assertIn("无响应", " ".join(analysis.errors))

    def test_13d_to_json_serialization(self):
        """边界情况 — to_json() 包含特殊字符。"""
        analysis = BlueprintAnalysis(
            basic_info=BlueprintBasicInfo(
                name='BP_"Special"',
                asset_path="/Game/Test/BP_Special",
                parent_class="Actor",
            ),
            variables=[
                BlueprintVariable(var_name="Name", var_type="String",
                                  default_value='<div class="test">'),
                BlueprintVariable(var_name="中文变量", var_type="Int", default_value="值"),
            ],
        )
        j = analysis.to_json()
        parsed = json.loads(j)
        self.assertEqual(parsed["basic_info"]["name"], 'BP_"Special"')
        self.assertEqual(parsed["variables"][0]["default_value"], '<div class="test">')
        self.assertEqual(parsed["variables"][1]["var_name"], "中文变量")


# ═══════════════════════════════════════════════════════════
# 便捷函数测试
# ═══════════════════════════════════════════════════════════

class TestConvenienceFunctions(unittest.TestCase):
    """便捷函数测试"""

    def test_analyze_blueprint_function(self):
        """analyze_blueprint() 便捷函数。"""
        mock_result = _make_l1_ok_result()
        with patch('blueprint_reader.BlueprintReader._run_probe', return_value=mock_result):
            analysis = analyze_blueprint("/Game/Test/BP_Test", mode="embedded")
        self.assertIsInstance(analysis, BlueprintAnalysis)
        self.assertEqual(analysis.basic_info.name, "BP_TestActor")

    def test_quick_inspect_function(self):
        """quick_inspect() 便捷函数返回 Markdown 字符串。"""
        mock_result = _make_l1_ok_result()
        with patch('blueprint_reader.BlueprintReader._run_probe', return_value=mock_result):
            md = quick_inspect("/Game/Test/BP_Test", mode="embedded")
        self.assertIsInstance(md, str)
        self.assertIn("蓝图分析报告", md)


# ═══════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    # 自定义 runner，输出 PASS/FAIL 计数
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])

    # 使用 TextTestRunner 但要捕获结果以便最终统计
    runner = unittest.TextTestRunner(verbosity=2, stream=sys.stdout)
    result = runner.run(suite)

    # 最终摘要
    total = result.testsRun
    failed = len(result.failures) + len(result.errors)
    passed = total - failed

    print()
    print("=" * 50)
    print(f"  RESULTS: {passed} PASS / {failed} FAIL / {total} TOTAL")
    if failed == 0:
        print("  ALL TESTS PASSED [OK]")
    else:
        print("  SOME TESTS FAILED [FAIL]")
    print("=" * 50)

    sys.exit(0 if failed == 0 else 1)
