"""场景 D 审计：analyzer.py + blueprint_reader.py 报告输出格式
用 tests/ 下的 mock 数据喂给分析引擎，验证输出链路。

T-20260727-PYTEST 步骤3：pytest 兼容重构。
  - 修复硬编码绝对路径 → 使用相对路径推导
  - 提取独立 test_* 函数供 pytest 收集
  - 保留 if __name__ == "__main__" 独立运行能力
"""
import sys, json, os

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_DIR = os.path.join(os.path.dirname(_THIS_DIR), "scripts")
_MOCK_PATH = os.path.join(_THIS_DIR, "mock_reader_output.json")

# conftest.py 自动注入，此处为兜底
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from analyzer import ProjectAnalyzer, analyze_from_reader_output  # noqa: E402


# ═══════════════════════════════════════════════════════════
# pytest 测试函数
# ═══════════════════════════════════════════════════════════

def test_a1_feed_and_analyze():
    """A1: feed_from_json + analyze 不崩溃，返回正确蓝图数"""
    analyzer = ProjectAnalyzer()
    analyzer.feed_from_json(_MOCK_PATH)
    result = analyzer.analyze("/Game/TestProject", depth=2)
    assert result.total_blueprints == 6, f"期望 6 蓝图，实际 {result.total_blueprints}"
    assert result.total_nodes == 14, f"期望 14 节点，实际 {result.total_nodes}"


def test_a2_to_dict_to_json():
    """A2: to_dict / to_json 序列化不崩溃"""
    analyzer = ProjectAnalyzer()
    analyzer.feed_from_json(_MOCK_PATH)
    analyzer.analyze("/Game/TestProject", depth=2)
    d = analyzer.to_dict()
    j = analyzer.to_json()
    assert len(d) > 0, "to_dict 返回空 dict"
    assert len(j) > 0, "to_json 返回空字符串"


def test_a3_analyze_from_reader_output():
    """A3: analyze_from_reader_output 便捷函数，depth=1 返回正确蓝图数"""
    result = analyze_from_reader_output(_MOCK_PATH, "/Game/TestProject", depth=1)
    assert result.total_blueprints == 6, f"depth=1 期望 6，实际 {result.total_blueprints}"


def test_a4_to_markdown_summary():
    """A4: to_markdown_summary 输出含关键标记 '蓝图分析报告'"""
    from blueprint_reader import BlueprintReader, BlueprintAnalysis, BlueprintBasicInfo  # noqa: E402

    analysis = BlueprintAnalysis(
        basic_info=BlueprintBasicInfo(
            name="BP_TestMock",
            asset_path="/Game/Test/BP_TestMock",
            parent_class="Actor",
            blueprint_type="Blueprint",
        ),
    )
    reader = BlueprintReader(mode="remote")
    md = reader.to_markdown_summary(analysis)
    assert "蓝图分析报告" in md, f"to_markdown_summary 缺失'蓝图分析报告'，前100字: {md[:100]}"
    assert len(md) > 0


def test_a5_to_dict_structure():
    """A5: to_dict 结果包含全部 10 个预期字段"""
    analyzer = ProjectAnalyzer()
    analyzer.feed_from_json(_MOCK_PATH)
    analyzer.analyze("/Game/TestProject", depth=2)
    d = analyzer.to_dict()

    expected_keys = [
        "project_name", "project_root", "analyzed_at", "summary",
        "distribution", "top_largest", "circular_dependencies",
        "coupling", "interface_usage", "deep_dives",
    ]
    for key in expected_keys:
        assert key in d, f"to_dict 缺少字段: {key}"


# ═══════════════════════════════════════════════════════════
# 独立运行入口（保持向后兼容）
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    import subprocess
    print("=" * 60)
    print("analyzer.py / blueprint_reader.py 报告输出审计（pytest runner）")
    print("=" * 60)
    subprocess.run([sys.executable, "-m", "pytest", __file__, "-v", "--tb=short", "--no-header"])
