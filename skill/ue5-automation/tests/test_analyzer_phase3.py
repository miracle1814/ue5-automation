"""Phase 3: 回归 — 同一输入 3 次执行验证输出一致性

T-20260727-PYTEST 步骤3：pytest 兼容重构。
  - 提取 test_phase3_* 函数
  - 修复 import 路径（conftest.py 自动注入 scripts/）
  - 保留 if __name__ == "__main__" 独立运行能力
"""
import sys, os, json, hashlib

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_DIR = os.path.join(os.path.dirname(_THIS_DIR), "scripts")

if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from analyzer import ProjectAnalyzer  # noqa: E402

_MOCK = os.path.join(_THIS_DIR, "mock_reader_output.json")


# ═══════════════════════════════════════════════════════════
# 辅助
# ═══════════════════════════════════════════════════════════

def _run_analyze():
    """运行一次完整分析，返回 to_dict（不含时间字段）。"""
    with open(_MOCK, "r", encoding="utf-8") as f:
        data = json.load(f)
    a = ProjectAnalyzer()
    a.feed(data)
    a.analyze("/Game/", depth=2)
    d = a.to_dict()
    d.pop("analyzed_at", None)
    # 归一化时间字段，避免因 cold start 导致哈希不一致
    if "summary" in d and "total_elapsed_ms" in d["summary"]:
        d["summary"]["total_elapsed_ms"] = 0
    return d


def _hash_dict(d):
    return hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest()


# ═══════════════════════════════════════════════════════════
# pytest 测试函数
# ═══════════════════════════════════════════════════════════

def test_phase3a_consistency_3_runs():
    """Phase 3a: 同一输入 3 次执行输出 SHA256 完全一致"""
    hashes = [_hash_dict(_run_analyze()) for _ in range(3)]
    unique = set(hashes)
    assert len(unique) == 1, (
        f"3 次运行结果不一致！\n"
        f"  Run 1: {hashes[0][:16]}...\n"
        f"  Run 2: {hashes[1][:16]}...\n"
        f"  Run 3: {hashes[2][:16]}..."
    )


def test_phase3b_different_order_consistency():
    """Phase 3b: 不同 feed 顺序核心指标一致"""
    # 正常顺序
    d_forward = _run_analyze()

    # 反转顺序
    with open(_MOCK, "r", encoding="utf-8") as f:
        data = json.load(f)
    a = ProjectAnalyzer()
    a.feed(list(reversed(data)))
    a.analyze("/Game/", depth=2)
    d_rev = a.to_dict()
    d_rev.pop("analyzed_at", None)

    # 核心指标应一致
    assert d_forward["summary"]["total_blueprints"] == d_rev["summary"]["total_blueprints"], (
        f"蓝图数不一致: forward={d_forward['summary']['total_blueprints']}, "
        f"reversed={d_rev['summary']['total_blueprints']}"
    )
    assert d_forward["summary"]["total_nodes"] == d_rev["summary"]["total_nodes"], (
        f"节点数不一致: forward={d_forward['summary']['total_nodes']}, "
        f"reversed={d_rev['summary']['total_nodes']}"
    )
    assert d_forward["summary"]["total_blueprints"] == 6
    assert d_forward["summary"]["total_nodes"] == 14


def test_phase3_forward_baseline():
    """Phase 3 基线：正常顺序下蓝图数和节点数符合预期"""
    d = _run_analyze()
    assert d["summary"]["total_blueprints"] == 6, f"期望 6 蓝图，实际 {d['summary']['total_blueprints']}"
    assert d["summary"]["total_nodes"] == 14, f"期望 14 节点，实际 {d['summary']['total_nodes']}"


# ═══════════════════════════════════════════════════════════
# 独立运行入口
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    import subprocess
    print("=" * 50)
    print("Phase 3: 回归一致性测试（pytest runner）")
    print("=" * 50)
    subprocess.run([sys.executable, "-m", "pytest", __file__, "-v", "--tb=short", "--no-header"])
