# -*- coding: utf-8 -*-
"""
test_cpp_apis_online.py — CPP-APIS（read_variables / add_component）真实 UE 在线回归
====================================================================================
T-20260908-CPPAPIS-CLOSEOUT 阶段③：把 2026-09-08 手动在线验证（T-20260823-
UE5BRIDGE-CPP-APIS 闭环）固化为 ue_online 自动用例。目标：将来 DLL/模板改动后
能自动回归这 2 个 C++ API 的在线路径。

  - test_online_read_variables_health_maxhealth — read_variables 返回真实变量清单
    （9/08 实测证据：临时蓝图返回 Health/MaxHealth 真实清单，Python
    new_variables 假阳性/降级消除）
  - test_online_add_component_boxextent_spawn    — add_component + properties →
    C++ SetComponentProperty BoxExtent=[200,100,50] → spawn 实例实测 Extent
    精确生效（9/08 决定性证据：CPP_Box Extent = {x:200, y:100, z:50}）

模式与 test_blueprint_editor_e2e.py 完全一致：
  - 全部标 @pytest.mark.ue_online → 不带 --ue-online 时自动 skip（conftest.py），
    离线无需 mock 即可收集通过（真实 UE 跑法见下）
  - token 直读 %APPDATA%/ue5_bridge/token（_resolve_real_bridge_token）——免疫
    offline 模块 import 期 setenv UE5_BRIDGE_TOKEN 污染进程 env 导致的 HTTP 403
    （2026-09-08 根因修复，见 KB 2026-09-08-UE5在线测试-离线setenv污染进程
    env-e2e直读token文件免疫模式.md）

真实 UE 跑法（需 UE 编辑器在线 + ue5_bridge 127.0.0.1:8889 + DLL >= 437,248B
含 ReadVariables / SetComponentProperty / OutError 补丁，8/23 已部署）：
  run_tests.bat e2e --ue-online      （全 e2e 批，本文件 2 用例一并回归）
  run_tests.bat online               （phase2 + e2e + 本文件在线用例）

临时蓝图：/Game/BP_CPPOnline_RV（变量读）与 /Game/BP_CPPOnline_Comp（组件+spawn）
——每个测试独立重建 + teardown delete_asset 清理（不污染，与 9/08 清理
BP_CPPOnline_20260908 同口径）。

作者：dev（代码开发 Agent）
日期：2026-09-08（T-20260908-CPPAPIS-CLOSEOUT 阶段③）
"""

import os
import re
import sys
import subprocess
import pytest

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_DIR = os.path.join(os.path.dirname(_THIS_DIR), "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from blueprint_editor import BlueprintEditor  # noqa: E402

BP_RV = "/Game/BP_CPPOnline_RV"
BP_COMP = "/Game/BP_CPPOnline_Comp"


def _resolve_real_bridge_token():
    """
    直读 %APPDATA%/ue5_bridge/token 文件（绕过 env）。

    与 test_blueprint_editor_e2e.py 同款（T-20260908 复制）：
    offline 测试模块在 import 时 setenv UE5_BRIDGE_TOKEN 污染 pytest 进程 env；
    BlueprintEditor() 自动解析优先读 env → 拿到 offline 测试 token → 请求真实
    UE Bridge 时 HTTP 403（2026-09-08 功能测试复现并修复）。
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


def _ue_send(code: str) -> str:
    """通过 ue_send.py 在 UE 内执行 Python 代码（subprocess）。"""
    r = subprocess.run(
        [sys.executable, os.path.join(_SCRIPTS_DIR, "ue_send.py"), code],
        capture_output=True, text=True, timeout=120,
        cwd=_SCRIPTS_DIR,
    )
    return (r.stdout + r.stderr).strip()


# ue_send 直调：搭建临时蓝图（clear + 2 个 Float 变量 + compile）
_SETUP_RV_CODE = r'''
import unreal
pkg = "/Game/BP_CPPOnline_RV"
bp = unreal.EditorAssetLibrary.load_asset(pkg)
if not bp:
    bp = unreal.BlueprintPythonBridge.create_actor_blueprint("/Game/", "BP_CPPOnline_RV")
graph = unreal.BlueprintPythonBridge.get_event_graph(bp)
unreal.BlueprintPythonBridge.clear_graph_nodes(graph)
for v in ("Health", "MaxHealth"):
    if not unreal.BlueprintPythonBridge.add_member_variable(bp, v, "Float"):
        print("ADD_VAR_FAIL", v)
unreal.BlueprintPythonBridge.compile_blueprint(bp)
print("SETUP_RV_OK")
'''

# ue_send 直调：搭建临时蓝图（clear + compile，组件由被测 API 添加）
_SETUP_COMP_CODE = r'''
import unreal
pkg = "/Game/BP_CPPOnline_Comp"
bp = unreal.EditorAssetLibrary.load_asset(pkg)
if not bp:
    bp = unreal.BlueprintPythonBridge.create_actor_blueprint("/Game/", "BP_CPPOnline_Comp")
graph = unreal.BlueprintPythonBridge.get_event_graph(bp)
unreal.BlueprintPythonBridge.clear_graph_nodes(graph)
unreal.BlueprintPythonBridge.compile_blueprint(bp)
print("SETUP_COMP_OK")
'''

# ue_send 直调：删除临时蓝图（teardown，不污染）
_TEARDOWN_CODE = r'''
import unreal
for pkg in ("/Game/BP_CPPOnline_RV", "/Game/BP_CPPOnline_Comp"):
    if unreal.EditorAssetLibrary.delete_asset(pkg):
        print("TEARDOWN_DEL_OK", pkg)
    else:
        print("TEARDOWN_DEL_FAIL", pkg)
'''

# ue_send 直调：spawn 临时蓝图实例并读取 BoxComponent Extent（独立验证）
_SPAWN_CODE = r'''
import unreal
bp = unreal.EditorAssetLibrary.load_asset("/Game/BP_CPPOnline_Comp")
cls = bp.generated_class()
if not cls:
    print("NO_GENERATED_CLASS")
else:
    actor = unreal.EditorLevelLibrary.spawn_actor_from_class(cls, unreal.Vector(0, 0, 0))
    if not actor:
        print("SPAWN_FAIL")
    else:
        try:
            comps = actor.get_components_by_class(unreal.BoxComponent)
            if not comps:
                print("NO_BOX_COMP")
            else:
                ext = comps[0].get_editor_property("box_extent")
                print("EXTENT_OK X={0} Y={1} Z={2}".format(ext.x, ext.y, ext.z))
        finally:
            unreal.EditorLevelLibrary.destroy_actor(actor)
'''


@pytest.fixture(scope="session")
def online_editor():
    """BlueprintEditor 实例（token 直读 token 文件，免疫 offline env 污染）。"""
    return BlueprintEditor(token=_resolve_real_bridge_token())


def _setup_or_raise(tag: str, code: str):
    out = _ue_send(code)
    assert tag in out, f"{tag} 失败: {out[-300:]}"


@pytest.fixture()
def tmp_bp_rv():
    """临时蓝图 BP_CPPOnline_RV（Health/MaxHealth 变量）就绪 + teardown 删除。"""
    _setup_or_raise("SETUP_RV_OK", _SETUP_RV_CODE)
    yield
    _ue_send(_TEARDOWN_CODE)


@pytest.fixture()
def tmp_bp_comp():
    """临时蓝图 BP_CPPOnline_Comp 就绪 + teardown 删除。"""
    _setup_or_raise("SETUP_COMP_OK", _SETUP_COMP_CODE)
    yield
    _ue_send(_TEARDOWN_CODE)


def _parse_extent(out: str):
    """从 'EXTENT_OK X=200.000000 Y=100.000000 Z=50.000000' 解析 (x, y, z)。"""
    m = re.search(r"EXTENT_OK X=([\d.]+) Y=([\d.]+) Z=([\d.]+)", out)
    if not m:
        raise AssertionError(f"未解析到 EXTENT_OK，ue_send 输出: {out[-400:]}")
    return float(m.group(1)), float(m.group(2)), float(m.group(3))


# ══════════════════════════════════════════════════════════════
# 用例 1：read_variables 在线路径（真实变量清单）
# ══════════════════════════════════════════════════════════════

@pytest.mark.ue_online
def test_online_read_variables_health_maxhealth(online_editor, tmp_bp_rv):
    """C++ ReadVariables 在线返回 Health/MaxHealth 真实清单（假阳性消除）。"""
    editor = online_editor
    res = editor._post("/command", {
        "command": "read_variables",
        "params": {"bp_path": BP_RV},
        "timeout": 15.0,
    })
    assert res.get("success"), f"read_variables 在线失败: {res}"
    vars_ = res.get("result") or []
    names = [v.get("name") for v in vars_]
    assert "Health" in names, f"变量清单缺 Health: {names}"
    assert "MaxHealth" in names, f"变量清单缺 MaxHealth: {names}"
    # 类型应为 C++ 映射后的真实类型（Float 类）
    by_name = {v.get("name"): v.get("type") for v in vars_}
    assert by_name.get("Health") in ("Float", "Real", "float"), by_name


# ══════════════════════════════════════════════════════════════
# 用例 2：add_component + SetComponentProperty 在线路径（spawn 实测）
# ══════════════════════════════════════════════════════════════

@pytest.mark.ue_online
def test_online_add_component_boxextent_spawn(online_editor, tmp_bp_comp):
    """add_component BoxExtent=[200,100,50] → SetComponentProperty → spawn 精确生效。"""
    editor = online_editor

    # add_component + properties（走 C++ AddComponentToBlueprint + SetComponentProperty）
    res = editor._post("/command", {
        "command": "add_component",
        "params": {
            "bp_path": BP_COMP,
            "comp_name": "CPP_Box",
            "comp_class": "BoxComponent",
            "properties": {"BoxExtent": [200, 100, 50]},
        },
        "timeout": 30.0,
    })
    assert res.get("success"), (
        f"add_component 在线失败（SetComponentProperty 不应 fail-loud）: {res}"
    )
    assert res.get("result", {}).get("added") == "CPP_Box", res

    # 独立验证：spawn 实例实测组件模板 Extent（与传入属性精确一致）
    out = _ue_send(_SPAWN_CODE)
    assert "NO_GENERATED_CLASS" not in out and "SPAWN_FAIL" not in out, (
        f"spawn 失败: {out[-300:]}"
    )
    x, y, z = _parse_extent(out)
    assert abs(x - 200.0) < 1.0, f"BoxExtent.X 不符: {x} (期望 200)"
    assert abs(y - 100.0) < 1.0, f"BoxExtent.Y 不符: {y} (期望 100)"
    assert abs(z - 50.0) < 1.0, f"BoxExtent.Z 不符: {z} (期望 50)"
