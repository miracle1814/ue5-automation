"""Phase 2: UE 在线 — 用 subprocess 调 ue_send 捕获完整输出

T-20260727-PYTEST 步骤3：pytest 兼容重构。
  - @pytest.mark.online 标记（conftest 自动 skip 无 UE 环境）
  - 提取 test_phase2_* 函数
  - 修复硬编码路径 → 相对路径推导
  - 保留 if __name__ == "__main__" 独立运行能力
"""
import sys, os, json, subprocess
import pytest

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_DIR = os.path.join(os.path.dirname(_THIS_DIR), "scripts")
_UE_SEND = os.path.join(_SCRIPTS_DIR, "ue_send.py")

if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

# UE 端探针代码（T-20260805-UE-E2E-FIX 修订）
# 原逻辑用 bp.ubergraph_pages / function_graphs / macro_graphs 拿 graphs，
# 这些 UE5.1 Python API 拿不到 → graphs 恒空 + break 在 if 外导致第一次循环必 break。
# 修订：Bridge API 优先（unreal.BlueprintPythonBridge），BlueprintEditorLibrary 兜底。
_UE_CODE = '''
import unreal, json

bp_path = "/Game/BP_HelloMirror"
bp = unreal.EditorAssetLibrary.load_asset(bp_path)

try:
    parent_name = str(bp.parent_class.get_name())
except:
    try:
        parent_name = str(bp.get_editor_property("parent_class").get_name())
    except:
        parent_name = "Actor"

result = {
    "basic_info": {
        "name": bp.get_name(),
        "asset_path": bp_path,
        "parent_class": parent_name,
        "blueprint_type": str(bp.get_class().get_name()),
        "interfaces": []
    },
    "graphs": {},
    "related_blueprints": [],
    "variables": [],
    "referenced_assets": []
}

# ── bridge 真实加载验证（T-20260805-UE-E2E-FIX）──
bridge_loaded = False
bridge_module = "none"
try:
    import BlueprintPythonBridge as BPB
    bridge_loaded = True
    bridge_module = str(getattr(BPB, "__file__", "BlueprintPythonBridge(import)"))
except Exception:
    try:
        _b = unreal.BlueprintPythonBridge
        _ = dir(_b)
        bridge_loaded = True
        bridge_module = "unreal.BlueprintPythonBridge"
    except Exception:
        bridge_loaded = False
result["bridge_loaded"] = bridge_loaded
result["bridge_module"] = bridge_module

# ── 图表提取：Bridge API 优先 ──
graphs = []
try:
    graphs = list(unreal.BlueprintPythonBridge.get_all_graphs(bp))
except Exception:
    graphs = []

# ── 兜底 1：BlueprintEditorLibrary（反射拿函数图）──
if not graphs:
    try:
        eg = unreal.BlueprintEditorLibrary.find_event_graph(bp)
        if eg:
            graphs.append(eg)
        try:
            fnames = [n for n in dir(bp) if n.startswith("K2Node")]
        except Exception:
            fnames = []
        for fname in fnames:
            try:
                g = unreal.BlueprintEditorLibrary.find_graph(bp, fname)
                if g:
                    graphs.append(g)
            except Exception:
                pass
    except Exception:
        pass

# ── 兜底 2：原生属性（修复原 break 缺陷：if 内 break）──
if not graphs:
    for attr in ["ubergraph_pages", "function_graphs", "macro_graphs"]:
        try:
            gs = getattr(bp, attr, None)
            if gs:
                graphs = list(gs)
                break
        except Exception:
            continue

for g in graphs:
    gname = g.get_name()
    graph_data = {"graph_name": gname, "nodes": [], "connections": []}

    # 节点提取：Bridge list_nodes 优先
    node_infos = []
    try:
        node_infos = list(unreal.BlueprintPythonBridge.list_nodes(g))
    except Exception:
        node_infos = []

    if node_infos:
        for ni in node_infos:
            try:
                title = str(ni.node_title)
            except:
                title = str(getattr(ni, "get_name", lambda: "node")())
            try:
                node_class = str(ni.node_class)
            except:
                node_class = "unknown"
            graph_data["nodes"].append({
                "node_name": title,
                "node_type": node_class,
                "category": "unknown"
            })
    else:
        # 原生 g.nodes 兜底
        for node in getattr(g, "nodes", []) or []:
            try:
                title = str(node.get_node_title())
            except:
                title = str(node.get_name())
            try:
                node_class = str(node.get_class().get_name())
            except:
                node_class = "unknown"
            graph_data["nodes"].append({
                "node_name": title,
                "node_type": node_class,
                "category": "unknown"
            })

    result["graphs"][gname] = graph_data

print("__JSON_START__")
print(json.dumps(result, ensure_ascii=False, default=str))
print("__JSON_END__")
'''


# ═══════════════════════════════════════════════════════════
# pytest 测试函数
# ═══════════════════════════════════════════════════════════

@pytest.mark.ue_online
def test_phase2a_extract_blueprint():
    """Phase 2a: 通过 ue_send 提取 BP_HelloMirror 蓝图数据"""
    r = subprocess.run(
        [sys.executable, _UE_SEND, _UE_CODE],
        capture_output=True, text=True, timeout=60,
        cwd=_SCRIPTS_DIR,
    )
    raw = r.stdout + r.stderr

    assert "__JSON_START__" in raw, f"未在输出中找到 __JSON_START__ 标记\n前500字: {raw[:500]}"

    json_str = raw.split("__JSON_START__")[1].split("__JSON_END__")[0].strip()
    bp_data = json.loads(json_str)

    bi = bp_data["basic_info"]
    graphs = bp_data.get("graphs", {})
    total_nodes = sum(len(g.get("nodes", [])) for g in graphs.values())

    print(f"\nBlueprint: {bi['name']}")
    print(f"Parent: {bi['parent_class']}")
    print(f"Type: {bi['blueprint_type']}")
    print(f"Graphs: {list(graphs.keys())} ({total_nodes} nodes)")

    assert bi["name"] == "BP_HelloMirror", f"蓝图名称应为 BP_HelloMirror，实际 {bi['name']}"

    # 保存结果供后续分析
    out_path = os.path.join(_THIS_DIR, "bp_hellomirror.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(bp_data, f, ensure_ascii=False, indent=2, default=str)


@pytest.mark.ue_online
def test_phase2b_feed_into_analyzer():
    """Phase 2b: 将提取的蓝图数据喂给 analyzer"""
    from analyzer import ProjectAnalyzer  # noqa: E402

    # 读取 phase2a 保存的结果
    data_path = os.path.join(_THIS_DIR, "bp_hellomirror.json")
    if not os.path.exists(data_path):
        # 如果 phase2a 未运行，先运行一次
        test_phase2a_extract_blueprint()

    with open(data_path, "r", encoding="utf-8") as f:
        bp_data = json.load(f)

    analyzer = ProjectAnalyzer()
    analyzer.feed([bp_data])
    result = analyzer.analyze("/Game/", depth=2)

    print(f"Blueprints: {result.total_blueprints}")
    print(f"Nodes: {result.total_nodes}")
    print(f"Connections: {result.total_connections}")

    assert result.total_blueprints == 1, f"期望 1 蓝图，实际 {result.total_blueprints}"


@pytest.mark.ue_online
def test_phase2d_json_output():
    """Phase 2d: analyzer JSON 输出结构完整"""
    from analyzer import ProjectAnalyzer  # noqa: E402

    data_path = os.path.join(_THIS_DIR, "bp_hellomirror.json")
    if not os.path.exists(data_path):
        test_phase2a_extract_blueprint()

    with open(data_path, "r", encoding="utf-8") as f:
        bp_data = json.load(f)

    analyzer = ProjectAnalyzer()
    analyzer.feed([bp_data])
    analyzer.analyze("/Game/", depth=2)

    json_out = analyzer.to_json()
    parsed = json.loads(json_out)

    assert parsed["summary"]["total_blueprints"] == 1
    for key in ["distribution", "top_largest", "circular_dependencies", "deep_dives"]:
        assert key in parsed, f"JSON 输出缺少字段: {key}"

    # 保存结果
    out_json = os.path.join(_THIS_DIR, "analyzer_live_result.json")
    with open(out_json, "w", encoding="utf-8") as f:
        f.write(json_out)


# ═══════════════════════════════════════════════════════════
# 独立运行入口
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("Phase 2: UE 在线测试（需要 UE 编辑器运行中）")
    print("=" * 50)

    # 直接以脚本方式运行 phase2a（兼容旧行为）
    test_phase2a_extract_blueprint()

    print("\n" + "=" * 50)
    print("[2b] Feed into Analyzer")
    print("=" * 50)
    test_phase2b_feed_into_analyzer()

    print("\n" + "=" * 50)
    print("[2d] JSON output")
    print("=" * 50)
    test_phase2d_json_output()

    print("\n" + "=" * 50)
    print("[PASS] Phase 2 complete!")
    print("=" * 50)
