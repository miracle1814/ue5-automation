"""场景 D 审计：mermaid_render.py 全链路测试

T-20260727-PYTEST 步骤3：pytest 兼容重构。
  - 修复硬编码绝对路径 → 使用相对路径推导
  - 提取独立 test_* 函数供 pytest 收集
  - L2 在线测试用 @pytest.mark.online 标记
  - 保留 if __name__ == "__main__" 独立运行能力

T-20260912-UE5BRIDGE-PYWRAP-L-R4-2（方案 A · 测试侧隔离）：
  - 全部用例统一传 pytest 内置 `tmp_path`，杜绝测试产物落产品目录
    `skills\\ue5-automation\\output\\`（原 `flowchart_*.txt` 污染源）
  - 零产品改动：不改 mermaid_render.py 的默认 output_dir 语义
"""
import sys, json, os
import pytest

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_DIR = os.path.join(os.path.dirname(_THIS_DIR), "scripts")

if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from mermaid_render import MermaidRenderer, MermaidRenderError  # noqa: E402


# ═══════════════════════════════════════════════════════════
# pytest 测试函数（统一传 tmp_path → 输出隔离到临时目录）
# ═══════════════════════════════════════════════════════════

def test_1_normal_ascii(tmp_path):
    """TEST1: 正常 mermaid 代码 ASCII 渲染（allow_external=False → L3 fallback）"""
    r = MermaidRenderer(str(tmp_path))
    result = r.render("flowchart TD\n  A-->B", format="ascii")
    assert result["level"] in (3, 1), f"期望 L3/L1 fallback，实际 L{result['level']}"
    assert result.get("path"), "渲染结果应包含 path"


def test_2_empty_code_raises(tmp_path):
    """TEST2: 空代码 → MermaidRenderError"""
    r = MermaidRenderer(str(tmp_path))
    with pytest.raises(MermaidRenderError):
        r.render("", format="png")


def test_3_png_no_backend_raises(tmp_path):
    """TEST3: PNG 格式无可用后端 → MermaidRenderError（或 L3 ASCII fallback）"""
    r = MermaidRenderer(str(tmp_path))
    try:
        result = r.render("flowchart TD\n  A-->B", format="png")
        # 如果没有抛异常，应走 L3 fallback
        assert result["level"] in (3, 1), f"PNG 无后端应走 fallback，实际 L{result['level']}"
    except MermaidRenderError:
        pass  # 预期行为


def test_4_no_mermaid_header(tmp_path):
    """TEST4: 无 Mermaid 类型头的文本 → render() 验证失败抛 MermaidRenderError"""
    r = MermaidRenderer(str(tmp_path))
    with pytest.raises(MermaidRenderError):
        r.render("hello world\nthis is not mermaid", format="ascii")


def test_5a_validate_valid():
    """TEST5a: validate 合法 mermaid 代码 → True"""
    ok, msg = MermaidRenderer.validate("flowchart TD\nA-->B")
    assert ok, f"合法代码验证应通过: {msg}"


def test_5b_validate_invalid():
    """TEST5b: validate 非法代码 → False"""
    ok, msg = MermaidRenderer.validate("invalid code")
    assert not ok, f"非法代码验证应失败: {msg}"


@pytest.mark.ue_online
def test_6_l2_online(tmp_path):
    """TEST6: L2 在线渲染（allow_external=True）— 需要网络"""
    r = MermaidRenderer(str(tmp_path), allow_external=True)
    result = r.render("flowchart TD\n  A-->B", format="png")
    assert result["level"] in (1, 2), f"在线渲染期望 L1/L2，实际 L{result['level']}"
    assert result.get("path"), "渲染结果应包含 path"


def test_7_non_mermaid_rejected(tmp_path):
    """TEST7: 非 Mermaid 文本 → render() 验证失败抛 MermaidRenderError"""
    r = MermaidRenderer(str(tmp_path))
    with pytest.raises(MermaidRenderError):
        r.render("random text not mermaid at all", format="ascii")


def test_8_batch_render(tmp_path):
    """TEST8: 批量渲染（含空代码，预期 2/3 成功）"""
    r = MermaidRenderer(str(tmp_path))
    diagrams = [
        {"code": "flowchart TD\n  A-->B", "name": "diag1"},
        {"code": "", "name": "empty_diag"},
        {"code": "flowchart LR\n  C-->D", "name": "diag3"},
    ]
    batch_result = r.render_batch(diagrams, format="ascii")
    ok_count = sum(1 for b in batch_result if b["ok"])
    assert ok_count >= 2, f"批量渲染期望 ≥2/3 成功，实际 {ok_count}/3"


# ═══════════════════════════════════════════════════════════
# 独立运行入口
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    import subprocess
    print("=" * 60)
    print("mermaid_render.py 审计（pytest runner）")
    print("=" * 60)
    subprocess.run([sys.executable, "-m", "pytest", __file__, "-v", "--tb=short", "--no-header"])
