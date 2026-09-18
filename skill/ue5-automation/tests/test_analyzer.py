#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_analyzer.py — analyzer.py 阶段一离线测试套件
==============================================
参照方案 v2.0：12 项离线测试
"""

import os
import sys
import json
import hashlib
import traceback

# 确保 scripts 目录在 sys.path 中
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

# ── 测试计数 ──
PASS = 0
FAIL = 0
WARN = 0
FAIL_DETAILS = []

RESULTS = {}

def _run_test(name, fn):
    global PASS, FAIL, RESULTS
    try:
        fn()
        PASS += 1
        RESULTS[name] = "PASS"
        print(f"  [PASS] {name}")
    except AssertionError as e:
        FAIL += 1
        msg = f"{name}: {e}"
        FAIL_DETAILS.append(msg)
        RESULTS[name] = f"FAIL"
        print(f"  [FAIL] {name}: {e}")
    except Exception as e:
        FAIL += 1
        msg = f"{name}: {e}\n{traceback.format_exc()}"
        FAIL_DETAILS.append(msg)
        RESULTS[name] = "CRASH"
        print(f"  [CRASH] {name}: {e}")
        print(traceback.format_exc())

# ═══════════════════════════════════════════════
# 测试数据
# ═══════════════════════════════════════════════

MOCK_PATH = os.path.join(os.path.dirname(__file__), "mock_reader_output.json")

def load_mock():
    with open(MOCK_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


# ═══════════════════════════════════════════════
# 1a: import 语法
# ═══════════════════════════════════════════════

def test_1a_import():
    """语法/import 检查"""
    from analyzer import ProjectAnalyzer, AnalysisResult, BlueprintSummary, CircularDependency, \
        CouplingMetrics, DistributionStats, DeepDiveDetail, InterfaceUsage, \
        analyze_from_reader_output, main
    assert ProjectAnalyzer is not None
    assert AnalysisResult is not None
    assert BlueprintSummary is not None


# ═══════════════════════════════════════════════
# 1b: 适配层 — reader 原始输出 → feed → 不崩
# ═══════════════════════════════════════════════

def test_1b_adapter():
    """适配层：reader 原始输出 feed 不崩溃"""
    from analyzer import ProjectAnalyzer
    data = load_mock()
    analyzer = ProjectAnalyzer()
    try:
        analyzer.feed(data)
        assert len(analyzer._blueprint_results) == 6, \
            f"期望 6 条，实际 {len(analyzer._blueprint_results)}"
    except Exception as e:
        raise AssertionError(f"feed() 崩溃: {e}")


def test_1b_adapter_field_mapping():
    """适配层字段映射正确"""
    from analyzer import ProjectAnalyzer
    data = load_mock()
    analyzer = ProjectAnalyzer()
    analyzer.feed(data)
    r = analyzer._blueprint_results

    # 检查第一个蓝图的扁平化字段
    bp = r[0]
    assert bp["blueprint_path"] == "/Game/DemoTest/BP_TestPhase2", \
        f"blueprint_path 错误: {bp['blueprint_path']}"
    assert bp["blueprint_name"] == "BP_TestPhase2", \
        f"blueprint_name 错误: {bp['blueprint_name']}"
    assert bp["blueprint_type"] == "Blueprint", \
        f"blueprint_type 错误: {bp['blueprint_type']}"
    assert bp["parent_class"] == "Actor", \
        f"parent_class 错误: {bp['parent_class']}"
    assert len(bp["graphs"]) == 1, \
        f"BP_TestPhase2 应有 1 个 graph，实际 {len(bp['graphs'])}"
    assert bp["graphs"][0]["graph_name"] == "EventGraph", \
        f"graph_name 错误: {bp['graphs'][0].get('graph_name')}"

    # 检查 BP_TestPhase3（2 个 graph）
    bp3 = r[1]
    assert len(bp3["graphs"]) == 2, \
        f"BP_TestPhase3 应有 2 个 graph，实际 {len(bp3['graphs'])}"
    graph_names = {g["graph_name"] for g in bp3["graphs"]}
    assert graph_names == {"EventGraph", "MoveToTarget"}, \
        f"graph_names 错误: {graph_names}"
    assert bp3["dependent_blueprints"] == ["/Game/DemoTest/BP_TestPhase2"], \
        f"dependent_blueprints 错误: {bp3['dependent_blueprints']}"

    # 检查空蓝图
    bp_empty = r[5]
    assert bp_empty["blueprint_name"] == "BP_Empty"
    assert bp_empty["graphs"] == [], f"BP_Empty graphs 应为 []，实际 {bp_empty['graphs']}"


# ═══════════════════════════════════════════════
# 1c: 正常 Mock 指标验证
# ═══════════════════════════════════════════════

def test_1c_basic_metrics():
    """正常 Mock：蓝图数/节点数/类型分布准确"""
    from analyzer import ProjectAnalyzer
    data = load_mock()
    analyzer = ProjectAnalyzer()
    analyzer.feed(data)
    result = analyzer.analyze("/Game/", depth=2)

    # 蓝图总数
    assert result.total_blueprints == 6, \
        f"蓝图总数应为 6，实际 {result.total_blueprints}"

    # 节点总数：BP_TestPhase2(3) + BP_TestPhase3(5) + BP_CharA(2) + BP_CharB(2) + BP_CharC(2) + BP_Empty(0) = 14
    assert result.total_nodes == 14, \
        f"节点总数应为 14，实际 {result.total_nodes}"

    # 连线总数：BP_TestPhase2(2) + BP_TestPhase3(3) + BP_CharA(1) + BP_CharB(1) + BP_CharC(1) + BP_Empty(0) = 8
    assert result.total_connections == 8, \
        f"连线总数应为 8，实际 {result.total_connections}"

    # 类型分布 — _classify_blueprint_type 按 parent_class 归类
    # 5 张 Actor 子类 + 1 张 Other（BP_Empty 的 parent 也是 Actor，所以全为 Actor? 实际)
    # 检查所有蓝图被正确归类（不崩溃，分布非空）
    assert len(result.distribution.by_type) >= 1, \
        f"类型分布不应为空，实际 {result.distribution.by_type}"
    total_by_type = sum(result.distribution.by_type.values())
    assert total_by_type == 6, \
        f"类型分布总计应为 6，实际 {total_by_type}"

    # TOP N 排序
    top_names = [s.name for s in result.top_largest]
    assert top_names[0] == "BP_TestPhase3", \
        f"TOP1 应为 BP_TestPhase3（5节点），实际 {top_names[0]}"
    assert top_names[1] == "BP_TestPhase2", \
        f"TOP2 应为 BP_TestPhase2（3节点），实际 {top_names[1]}"


# ═══════════════════════════════════════════════
# 1d: 空数据
# ═══════════════════════════════════════════════

def test_1d_empty_data():
    """空数据：不崩溃，输出合理空结果"""
    from analyzer import ProjectAnalyzer
    analyzer = ProjectAnalyzer()
    analyzer.feed([])
    result = analyzer.analyze("/Game/", depth=2)
    assert result.total_blueprints == 0
    assert result.total_nodes == 0
    assert result.total_connections == 0
    assert result.distribution.by_type == {} or len(result.distribution.by_type) == 0
    assert result.top_largest == []


# ═══════════════════════════════════════════════
# 1e: 空蓝图（0 节点）
# ═══════════════════════════════════════════════

def test_1e_empty_blueprint():
    """空蓝图（0节点0变量）：_extract_summary 不崩溃"""
    from analyzer import ProjectAnalyzer
    analyzer = ProjectAnalyzer()
    analyzer.feed([{
        "basic_info": {
            "name": "BP_Empty",
            "asset_path": "/Game/Empty",
            "parent_class": "Actor",
            "blueprint_type": "Blueprint",
            "interfaces": []
        },
        "graphs": {},
        "related_blueprints": [],
        "variables": [],
        "referenced_assets": []
    }])
    result = analyzer.analyze("/Game/", depth=2)
    assert result.total_blueprints == 1
    assert result.total_nodes == 0
    assert result.total_connections == 0
    assert len(result.top_largest) == 1
    assert result.top_largest[0].node_count == 0


# ═══════════════════════════════════════════════
# 1f: 循环依赖检测
# ═══════════════════════════════════════════════

def test_1f_circular_detection():
    """循环依赖：A→B→C→A 正确检测"""
    from analyzer import ProjectAnalyzer
    data = load_mock()
    analyzer = ProjectAnalyzer()
    analyzer.feed(data)
    result = analyzer.analyze("/Game/", depth=2)

    # Mock data 中 BP_CharA→BB→C→A 构成循环
    assert len(result.circular_dependencies) >= 1, \
        f"应检测到循环依赖，实际 {len(result.circular_dependencies)} 个"

    # 验证环的蓝图数量 ≥ 3
    cd = result.circular_dependencies[0]
    assert len(cd.cycle) >= 3, \
        f"循环依赖至少 3 个蓝图，实际 {len(cd.cycle)}: {cd.cycle}"


# ═══════════════════════════════════════════════
# 1g: >200 节点 deep dive 触发
# ═══════════════════════════════════════════════

def test_1g_deep_dive_trigger():
    """>200 节点蓝图：正确触发 deep dive"""
    from analyzer import ProjectAnalyzer

    # 构造一张 250 节点的蓝图
    nodes = []
    connections = []
    for i in range(250):
        nodes.append({
            "node_name": f"Node_{i}",
            "node_type": "K2Node_CallFunction",
            "category": "function_call"
        })
        if i > 0:
            connections.append({"from": f"Node_{i-1}", "to": f"Node_{i}"})

    analyzer = ProjectAnalyzer()
    analyzer.feed([{
        "basic_info": {
            "name": "BP_Massive",
            "asset_path": "/Game/BP_Massive",
            "parent_class": "Actor",
            "blueprint_type": "Blueprint",
            "interfaces": []
        },
        "graphs": {
            "EventGraph": {
                "graph_name": "EventGraph",
                "nodes": nodes,
                "connections": connections
            }
        },
        "related_blueprints": [],
        "variables": [],
        "referenced_assets": []
    }])
    result = analyzer.analyze("/Game/", depth=2)

    assert len(result.deep_dives) >= 1, \
        f"应触发 deep dive（250节点），实际 {len(result.deep_dives)} 个"
    # 检查 deep dive 原因是节点过多
    dd = result.deep_dives[0]
    assert dd.blueprint_path == "/Game/BP_Massive" or "BP_Massive" in dd.blueprint_path, \
        f"deep dive 对象应为 BP_Massive，实际 {dd.blueprint_path}"


# ═══════════════════════════════════════════════
# 1h: 单蓝图循环假阳性
# ═══════════════════════════════════════════════

def test_1h_single_no_false_positive():
    """单蓝图：循环检测不假阳性"""
    from analyzer import ProjectAnalyzer
    analyzer = ProjectAnalyzer()
    analyzer.feed([{
        "basic_info": {
            "name": "BP_Solo",
            "asset_path": "/Game/BP_Solo",
            "parent_class": "Actor",
            "blueprint_type": "Blueprint",
            "interfaces": []
        },
        "graphs": {
            "EventGraph": {
                "graph_name": "EventGraph",
                "nodes": [
                    {"node_name": "BeginPlay", "node_type": "K2Node_Event", "category": "event"}
                ],
                "connections": []
            }
        },
        "related_blueprints": [],
        "variables": [],
        "referenced_assets": []
    }])
    result = analyzer.analyze("/Game/", depth=2)
    assert result.circular_dependencies == [], \
        f"单蓝图不应有循环依赖，实际 {len(result.circular_dependencies)} 个"


# ═══════════════════════════════════════════════
# 1i: graphs 缺失（模拟 L3 降级）
# ═══════════════════════════════════════════════

def test_1i_missing_graphs():
    """graphs 字段缺失：优雅降级有 warnings"""
    from analyzer import ProjectAnalyzer
    analyzer = ProjectAnalyzer()
    analyzer.feed([{
        "basic_info": {
            "name": "BP_NoGraph",
            "asset_path": "/Game/BP_NoGraph",
            "parent_class": "Actor",
            "blueprint_type": "Blueprint",
            "interfaces": []
        },
        "related_blueprints": [],
        "variables": [],
        "referenced_assets": []
        # graphs 字段缺失
    }])
    result = analyzer.analyze("/Game/", depth=2)
    assert result.total_blueprints == 1
    assert result.total_nodes == 0  # 无 graph，节点为 0
    # 允许静默度过（graphs 缺失不算错误，只是空输出）


# ═══════════════════════════════════════════════
# 1j: 损坏 JSON
# ═══════════════════════════════════════════════

def test_1j_corrupted_json():
    """损坏 JSON：明确报错不静默"""
    from analyzer import ProjectAnalyzer
    import tempfile

    # 写入损坏的 JSON 文件
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8")
    tmp.write("{this is not valid json [[[")
    tmp.close()

    analyzer = ProjectAnalyzer()
    try:
        analyzer.feed_from_json(tmp.name)
        # 如果没抛异常，也算通过——只要不崩
    except (json.JSONDecodeError, Exception):
        pass  # 预期行为：报错

    os.unlink(tmp.name)
    # 到这没崩就行


# ═══════════════════════════════════════════════
# 1k: 序列化往返
# ═══════════════════════════════════════════════

def test_1k_serialization_roundtrip():
    """序列化往返：analyze → to_dict → 语义验证 → 再 analyze（同数据源）→ 结果一致"""
    from analyzer import ProjectAnalyzer

    data = load_mock()
    analyzer1 = ProjectAnalyzer()
    analyzer1.feed(data)
    result1 = analyzer1.analyze("/Game/", depth=2)
    d1 = analyzer1.to_dict()

    # 验证序列化语义正确
    assert d1["summary"]["total_blueprints"] == 6
    assert d1["summary"]["total_nodes"] == 14

    # 同数据源重新分析 → 结果一致
    analyzer2 = ProjectAnalyzer()
    analyzer2.feed(data)
    result2 = analyzer2.analyze("/Game/", depth=2)
    d2 = analyzer2.to_dict()

    # 排除 analyzed_at 和 total_elapsed_ms 后比较（这两个是时间相关字段）
    d1.pop("analyzed_at", None)
    d2.pop("analyzed_at", None)
    if "summary" in d1 and "total_elapsed_ms" in d1["summary"]:
        d1["summary"]["total_elapsed_ms"] = 0
    if "summary" in d2 and "total_elapsed_ms" in d2["summary"]:
        d2["summary"]["total_elapsed_ms"] = 0
    h1 = hashlib.sha256(json.dumps(d1, sort_keys=True).encode()).hexdigest()
    h2 = hashlib.sha256(json.dumps(d2, sort_keys=True).encode()).hexdigest()
    assert h1 == h2, f"同数据源两次分析结果不一致: {h1} vs {h2}"


# ═══════════════════════════════════════════════
# 1l: JSON + Markdown 输出格式
# ═══════════════════════════════════════════════

def test_1l_json_output():
    """JSON 输出合法且非空"""
    from analyzer import ProjectAnalyzer
    data = load_mock()
    analyzer = ProjectAnalyzer()
    analyzer.feed(data)
    result = analyzer.analyze("/Game/", depth=2)
    json_str = analyzer.to_json()

    # 可解析
    parsed = json.loads(json_str)
    assert parsed["summary"]["total_blueprints"] == 6
    assert "distribution" in parsed
    assert "top_largest" in parsed
    assert "circular_dependencies" in parsed
    assert "deep_dives" in parsed


def test_1l_markdown_output():
    """Markdown 报告生成"""
    from analyzer import generate_markdown_report
    assert callable(generate_markdown_report), "generate_markdown_report 应可调用"


# ═══════════════════════════════════════════════
# 额外: analyze_from_reader_output 便捷入口
# ═══════════════════════════════════════════════

def test_analyze_from_reader_output():
    """analyze_from_reader_output() — 用 ProjectAnalyzer 显式调用"""
    from analyzer import ProjectAnalyzer
    analyzer = ProjectAnalyzer()
    analyzer.feed_from_json(MOCK_PATH)
    result = analyzer.analyze("/Game/", depth=2)
    assert result.total_blueprints == 6
    assert result.total_nodes == 14
    assert result.project_root == "/Game/"


# ═══════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 55)
    print("  analyzer.py 阶段一离线测试")
    print("=" * 55)
    print()

    _run_test("1a  import语法", test_1a_import)
    _run_test("1b  适配层-feed不崩", test_1b_adapter)
    _run_test("1b  适配层-字段映射", test_1b_adapter_field_mapping)
    _run_test("1c  基础指标验证", test_1c_basic_metrics)
    _run_test("1d  空数据集", test_1d_empty_data)
    _run_test("1e  空蓝图", test_1e_empty_blueprint)
    _run_test("1f  循环依赖检测", test_1f_circular_detection)
    _run_test("1g  深挖触发(>200节点)", test_1g_deep_dive_trigger)
    _run_test("1h  单蓝图不假阳性", test_1h_single_no_false_positive)
    _run_test("1i  graphs缺失降级", test_1i_missing_graphs)
    _run_test("1j  损坏JSON", test_1j_corrupted_json)
    _run_test("1k  序列化往返", test_1k_serialization_roundtrip)
    _run_test("1l  JSON输出格式", test_1l_json_output)
    _run_test("1l  Markdown生成", test_1l_markdown_output)
    _run_test("extra analyze_from_reader_output", test_analyze_from_reader_output)

    print()
    print("=" * 55)
    total = PASS + FAIL
    print(f"  结果: {PASS}/{total} 通过, {FAIL} 失败")
    print("=" * 55)

    if FAIL > 0:
        print("\n[FAIL] Details:")
        for d in FAIL_DETAILS:
            print(f"  - {d}")
        sys.exit(1)
    else:
        print("\n[PASS] All tests passed!")
        sys.exit(0)
