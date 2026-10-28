# -*- coding: utf-8 -*-
"""
test_blueprint_editor_e2e.py — BlueprintEditor 真实 UE 端到端测试（P0-2 · T-20260805-UE-FULL-CLOSURE）
=====================================================================================================
需要 UE 编辑器在线 + ue5_bridge（127.0.0.1:8889）运行（全部标 @pytest.mark.ue_online，
不带 --ue-online 时自动跳过）。

覆盖 4 个真实 UE 写入操作：
  1. test_e2e_update_node_param — 修改 BP_HelloMirror「打印字符串」节点 InString
     默认值 → _verify_param 回读验证 → 回滚恢复原值（不污染 BP_HelloMirror）
  2. test_e2e_connect_pins     — 临时蓝图 BP_E2E_Test 连接 打印字符串.then →
     延迟.execute → read_connections 独立验证 → 清理走 fixture teardown
  3. test_e2e_disconnect_pin   — 临时蓝图先连一条线 → 断开 延迟.execute →
     独立验证线已断 → 重连回滚
  4. test_e2e_delete_node      — 临时蓝图删除「延迟」节点 → read_nodes 验证消失
     （临时蓝图每个测试重建，不污染 BP_HelloMirror）

⚠️ 命名坑（T-20260805-UE-FULL-CLOSURE 实测）：
  bridge 的 node_id 参数接受**节点标题（中文）**如 "打印字符串" / "延迟"，
  不是 guid。传 guid 会报"引脚不存在"/"节点未找到"。

⚠️ 临时蓝图构建（实测通过）：
  bridge 的 /build-batch 图形操作（add_event_node/add_function_call_node）因
  模板 API 签名过时（add_event_node 首参应为 graph、add_function_call_node 需
  target_class_name 第 3 参）在真实 UE 上失败 → 本测试用 ue_send 直调
  unreal.BlueprintPythonBridge 搭建/清空临时蓝图（fixture teardown 必清空）。

作者：dev（代码开发 Agent）
日期：2026-08-05
"""

import os
import sys
import uuid
import subprocess
import pytest

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_DIR = os.path.join(os.path.dirname(_THIS_DIR), "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from blueprint_editor import BlueprintEditor  # noqa: E402

# ══════════════════════════════════════════════════════════════
# 常量
# ══════════════════════════════════════════════════════════════

BP_HELLO = "/Game/BP_HelloMirror"
BP_TMP = "/Game/BP_E2E_Test"
TMP_BP_NAME = "BP_E2E_Test"

# ue_send 直调：搭建临时蓝图（clear + 2 函数节点 + compile）
_SETUP_TMP_CODE = r'''
import unreal
pkg = "/Game/BP_E2E_Test"
bp = unreal.EditorAssetLibrary.load_asset(pkg)
if not bp:
    bp = unreal.BlueprintPythonBridge.create_actor_blueprint("/Game/", "BP_E2E_Test")
graph = unreal.BlueprintPythonBridge.get_event_graph(bp)
unreal.BlueprintPythonBridge.clear_graph_nodes(graph)
unreal.BlueprintPythonBridge.add_function_call_node(graph, "PrintString", "/Script/Engine.KismetSystemLibrary", 50, 0)
unreal.BlueprintPythonBridge.add_function_call_node(graph, "Delay", "/Script/Engine.KismetSystemLibrary", 350, 0)
unreal.BlueprintPythonBridge.compile_blueprint(bp)
print("SETUP_OK")
'''

# ue_send 直调：清空临时蓝图（teardown）
_TEARDOWN_TMP_CODE = r'''
import unreal
bp = unreal.EditorAssetLibrary.load_asset("/Game/BP_E2E_Test")
if bp:
    graph = unreal.BlueprintPythonBridge.get_event_graph(bp)
    unreal.BlueprintPythonBridge.clear_graph_nodes(graph)
    unreal.BlueprintPythonBridge.compile_blueprint(bp)
    print("TEARDOWN_OK")
else:
    print("TEARDOWN_NONE")
'''


# ══════════════════════════════════════════════════════════════
# 辅助函数
# ══════════════════════════════════════════════════════════════

def _ue_send(code: str) -> str:
    """通过 ue_send.py 在 UE 内执行 Python 代码（subprocess）。"""
    r = subprocess.run(
        [sys.executable, os.path.join(_SCRIPTS_DIR, "ue_send.py"), code],
        capture_output=True, text=True, timeout=90,
        cwd=_SCRIPTS_DIR,
    )
    return (r.stdout + r.stderr).strip()


def _read_conns(editor: BlueprintEditor, bp_path: str):
    """读取蓝图当前连线（独立验证，不走被测 API 的缓存）。"""
    raw = editor._post("/command", {
        "command": "read_connections",
        "params": {"bp_path": bp_path, "graph_name": "EventGraph"},
        "timeout": 15.0,
    })
    if not raw.get("success") or not (raw.get("result") or {}).get("available"):
        return []
    conns = []
    for g in raw["result"]["graphs"]:
        for c in g.get("connections", []):
            conns.append((c.get("from_node"), c.get("from_pin"),
                          c.get("to_node"), c.get("to_pin")))
    return conns


def _read_titles(editor: BlueprintEditor, bp_path: str):
    """读取蓝图 EventGraph 节点标题列表（独立验证）。"""
    raw = editor._post("/command", {
        "command": "read_nodes",
        "params": {"bp_path": bp_path, "graph_name": "EventGraph"},
        "timeout": 15.0,
    })
    if not raw.get("success") or not (raw.get("result") or {}).get("available"):
        return []
    titles = []
    for g in raw["result"]["graphs"]:
        titles += [n.get("title") for n in g.get("nodes", [])]
    return titles


def _get_pin_value(editor: BlueprintEditor, bp_path: str, node_id: str, pin_name: str):
    """读取引脚当前默认值（独立验证）。"""
    raw = editor._post("/command", {
        "command": "get_pin_default_value",
        "params": {"bp_path": bp_path, "node_id": node_id, "pin_name": pin_name},
        "timeout": 15.0,
    })
    if raw.get("success"):
        return (raw.get("result") or {}).get("value")
    return None


# ══════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════

def _resolve_real_bridge_token():
    """
    直读 %APPDATA%/ue5_bridge/token 文件（绕过 env）。

    ⚠️ 必须绕过 env：e2e 模式下同进程收集的 offline 模块
    （test_blueprint_editor_integration.py / test_add_component_cpp.py 等）
    在 import 时 setenv UE5_BRIDGE_TOKEN=integration-test-token 污染进程 env；
    BlueprintEditor() 自动解析优先读 env → 拿到 offline 测试 token →
    请求真实 UE Bridge 时 403（2026-09-08 功能测试复现，8/23 挂起同源）。
    """
    tok_file = os.path.join(
        os.environ.get("APPDATA", os.path.expanduser("~")),
        "ue5_bridge", "token",
    )
    try:
        with open(tok_file, "r", encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


@pytest.fixture(scope="session")
def e2e_editor():
    """BlueprintEditor 实例（token 直读 token 文件，免疫 offline 模块 env 污染）。"""
    return BlueprintEditor(token=_resolve_real_bridge_token())


@pytest.fixture()
def tmp_bp_ready():
    """
    临时蓝图 BP_E2E_Test 就绪（function scope）：
      setup  — ue_send 直调：clear + 打印字符串/延迟 两个函数节点 + compile
      teardown — ue_send 直调：clear_graph_nodes 清空（不污染下次运行）
    每个测试独立重建，天然隔离、天然回滚。
    """
    setup_out = _ue_send(_SETUP_TMP_CODE)
    assert "SETUP_OK" in setup_out, f"临时蓝图 setup 失败: {setup_out[-300:]}"
    yield
    _ue_send(_TEARDOWN_TMP_CODE)


# ══════════════════════════════════════════════════════════════
# 测试 1：update_node_param（BP_HelloMirror，改 InString + 回滚）
# ══════════════════════════════════════════════════════════════

@pytest.mark.ue_online
def test_e2e_update_node_param(e2e_editor):
    """修改 BP_HelloMirror「打印字符串」节点 InString → 验证 → 回滚。"""
    editor = e2e_editor

    # 前置：节点与引脚存在
    assert "打印字符串" in _read_titles(editor, BP_HELLO), "BP_HelloMirror 缺打印字符串节点"

    old = _get_pin_value(editor, BP_HELLO, "打印字符串", "InString")
    new_val = "E2E_UPDATE_" + uuid.uuid4().hex[:6]

    try:
        res = editor.update_node_param(BP_HELLO, "打印字符串", "InString", new_val)
        assert res.success, f"update_node_param 失败: {res.details}"
        assert res.data.get("verified") is True, f"_verify_param 未确认写入: {res.data}"

        got = _get_pin_value(editor, BP_HELLO, "打印字符串", "InString")
        assert str(got) == new_val, f"回读值不符: {got!r} != {new_val!r}"
    finally:
        # 回滚：恢复原值（不污染 BP_HelloMirror）
        rollback_val = old if old is not None else ""
        rb = editor.update_node_param(BP_HELLO, "打印字符串", "InString", rollback_val)
        assert rb.success, f"回滚失败: {rb.details}"
        final = _get_pin_value(editor, BP_HELLO, "打印字符串", "InString")
        assert str(final) == str(rollback_val), (
            f"回滚后值不符: {final!r} != {rollback_val!r}（BP_HelloMirror 被污染！）"
        )


# ══════════════════════════════════════════════════════════════
# 测试 2：connect_pins（临时蓝图，connect + 独立验证）
# ══════════════════════════════════════════════════════════════

@pytest.mark.ue_online
def test_e2e_connect_pins(e2e_editor, tmp_bp_ready):
    """连接 打印字符串.then → 延迟.execute → read_connections 独立验证生效。"""
    editor = e2e_editor

    # 前置：节点存在
    titles = _read_titles(editor, BP_TMP)
    assert "打印字符串" in titles, f"临时蓝图缺打印字符串: {titles}"
    assert "延迟" in titles, f"临时蓝图缺延迟: {titles}"

    res = editor.connect_pins_by_id(BP_TMP, "打印字符串", "then", "延迟", "execute")
    assert res.success, f"connect_pins_by_id 失败: {res.details}"

    conns = _read_conns(editor, BP_TMP)
    assert ("打印字符串", "then", "延迟", "execute") in conns, (
        f"连线未在拓扑中生效，当前连线: {conns}"
    )


# ══════════════════════════════════════════════════════════════
# 测试 3：disconnect_pin（临时蓝图，先连后断 + 独立验证 + 重连回滚）
# ══════════════════════════════════════════════════════════════

@pytest.mark.ue_online
def test_e2e_disconnect_pin(e2e_editor, tmp_bp_ready):
    """断开 延迟.execute 的全部连线 → 独立验证线已断 → 重连回滚。"""
    editor = e2e_editor

    # 先连一条线
    cres = editor.connect_pins_by_id(BP_TMP, "打印字符串", "then", "延迟", "execute")
    assert cres.success, f"前置连线失败: {cres.details}"
    assert ("打印字符串", "then", "延迟", "execute") in _read_conns(editor, BP_TMP)

    # 断开
    dres = editor.disconnect_pin(BP_TMP, "延迟", "execute")
    assert dres.success, (
        f"disconnect_pin 应真实断开，但失败: {dres.details} "
        f"(errors={dres.errors} warnings={dres.warnings})"
    )

    conns = _read_conns(editor, BP_TMP)
    assert ("打印字符串", "then", "延迟", "execute") not in conns, (
        f"断线后连线仍存在（未真断）: {conns}"
    )

    # 回滚：重连（保持临时蓝图可用状态）
    rres = editor.connect_pins_by_id(BP_TMP, "打印字符串", "then", "延迟", "execute")
    assert rres.success, f"回滚重连失败: {rres.details}"


# ══════════════════════════════════════════════════════════════
# 测试 4：delete_node（临时蓝图，删除非入口节点 + 独立验证）
# ══════════════════════════════════════════════════════════════

@pytest.mark.ue_online
def test_e2e_delete_node(e2e_editor, tmp_bp_ready):
    """删除临时蓝图「延迟」节点 → read_nodes 独立验证节点消失。"""
    editor = e2e_editor

    assert "延迟" in _read_titles(editor, BP_TMP), "前置：临时蓝图应有延迟节点"

    res = editor.delete_node(BP_TMP, "延迟")
    assert res.success, (
        f"delete_node 应真实删除，但失败: {res.details} "
        f"(errors={res.errors})"
    )

    titles = _read_titles(editor, BP_TMP)
    assert "延迟" not in titles, f"删除后节点仍存在: {titles}"
