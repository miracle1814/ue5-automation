#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_build_blueprint.py — POST /build（创建模式）回归测试
=========================================================
T-20260909-SKILL-DEFECT-FIX · P1-1 缺口修复。

背景：`ue5_runner.build(spec)` → `POST /build` 是「自动生成蓝图资产」的**唯一
入口**，2026-09-09 上线能力评估实测发现该路径此前**零自动化回归覆盖**（离线
137 passed 全部集中在读/改/诊断/评估四模式）。本文件补齐。

测试层级
--------
* 离线（默认 13 条）：`UE5_BRIDGE_TEST_MODE=1` + `fake_unreal` 替身 +
  FastAPI `TestClient`（走完整中间件栈：认证 / 路由 / 异常处理）。
* 在线（1 条）：`@pytest.mark.ue_online`，真实 UE + `ue5_runner.build` 端到端，
  不带 `--ue-online` 时自动 skip（conftest.py）。

离线覆盖点
----------
  1. 最小创建（compile_after=False）→ success + create_asset 步骤 + 资产存在
  2. 变量写入 → steps 含 add_variable:* + fake 蓝图变量表真实变更
  3. 组件添加 → steps 含 add_component:* + fake 蓝图组件表真实变更
  4. 组件属性 → 属性真实落到组件模板
  5. 接口添加 → steps 含 add_interface:*
  6. 重名蓝图 → success=False + category=asset_create + suggestion 透传
  7. compile_after=True → GameThread 结果文件合并（成功路径）
  8. compile_after=True 且 GameThread 无响应 → partial_success 超时语义
     （须带图操作 · 修法2 后纯编译不进 GT）
  8b. 纯编译（无图操作）→ 进程内编译，零 GT 注入（修法2 回归锚点）
  9. 缺 Token → HTTP 403
 10. 错 Token → HTTP 403
 11. UE API 抛异常 → 错误分类透传（category=variable_add）
 12. 请求默认值（package_path=/Game/Blueprints/ · parent_class=Actor ·
     compile_after=True）

已知教训（编码前检索）
----------------------
  * 必须在 `import ue5_bridge` **之前**设置 `UE5_BRIDGE_TEST_MODE` /
    `UE5_BRIDGE_TOKEN`（模块 import 时锁定 token，见 test_add_component_cpp.py
    头部注记）。
  * `fake_unreal.BlueprintEditorLibrary.get_blueprint_simple_construction_script`
    返回 `None`——**刻意对齐真实 UE5.1 行为**（SimpleConstructionScript 未向
    Python 暴露，见 KB `UE5_Python_API_组件添加限制_SCS未暴露.md`）。因此创建
    模式的组件路径若走 legacy `_add_component_unreal` 必然失败。

作者：maintainer（default · T-20260909-SKILL-DEFECT-FIX）
日期：2026-09-09
"""

import ast
import json
import os
import re
import sys
import unittest
from unittest import mock

# ── 必须在 import ue5_bridge 之前设置（模块 import 时锁定 token）──
os.environ["UE5_BRIDGE_TEST_MODE"] = "1"
os.environ["UE5_BRIDGE_TOKEN"] = "integration-test-token"

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(SKILL_ROOT, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import pytest                        # noqa: E402
import ue5_bridge                    # noqa: E402  真实模块（TEST_MODE → fake_unreal）
import fake_unreal as fu             # noqa: E402  test double
from fastapi.testclient import TestClient  # noqa: E402

TOKEN = "integration-test-token"
DEFAULT_PKG = "/Game/Blueprints/"


# ══════════════════════════════════════════════════════════════
#  离线：POST /build 语义回归
# ══════════════════════════════════════════════════════════════

class BuildBlueprintOfflineTests(unittest.TestCase):
    """创建模式离线回归（fake_unreal + TestClient）。"""

    def setUp(self):
        fu.reset_fake()
        self.client = TestClient(ue5_bridge.app)
        self.headers = {"X-Skill-Token": TOKEN}

    # ── 工具 ──────────────────────────────────────────────
    def _build(self, name, headers=None, **kw):
        payload = {"blueprint_name": name}
        payload.update(kw)
        return self.client.post("/build", json=payload,
                                headers=self.headers if headers is None else headers)

    def _steps(self, data):
        return [s["step"] for s in data.get("steps", [])]

    # ── 1. 最小创建 ───────────────────────────────────────
    def test_minimal_build_without_compile(self):
        """compile_after=False → 只创建资产，不触发 GameThread 路径。"""
        r = self._build("BP_Min", compile_after=False)
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertTrue(d["success"], d)
        self.assertIn("create_asset", self._steps(d))
        self.assertTrue(
            fu.EditorAssetLibrary.does_asset_exist(DEFAULT_PKG + "BP_Min"))

    # ── 2. 变量 ───────────────────────────────────────────
    def test_build_variables_registered(self):
        """变量写入 → steps 齐全 + fake 蓝图变量表真实变更。"""
        r = self._build("BP_Vars", compile_after=False,
                        variables=[{"name": "Health", "type": "float"},
                                   {"name": "IsDead", "type": "bool"}])
        d = r.json()
        self.assertTrue(d["success"], d)
        steps = self._steps(d)
        self.assertIn("add_variable:Health", steps)
        self.assertIn("add_variable:IsDead", steps)
        bp = fu.EditorAssetLibrary.load_asset(DEFAULT_PKG + "BP_Vars")
        self.assertIsNotNone(bp)
        names = [v["var_name"] for v in bp.new_variables]
        self.assertEqual(names, ["Health", "IsDead"])

    # ── 3. 组件 ───────────────────────────────────────────
    def test_build_components_registered(self):
        """组件添加 → steps 齐全 + fake 蓝图组件表真实变更。

        🔴 回归锚点：创建模式的组件路径必须与 /command add_component 一致，
        走 C++ SCS API（v0.9+）；legacy `_add_component_unreal` 依赖 UE5.1 未向
        Python 暴露的 SimpleConstructionScript → 真实 UE 必然失败。
        """
        r = self._build("BP_Comps", compile_after=False,
                        components=[{"name": "MeshComp",
                                     "component_class": "StaticMeshComponent"}])
        d = r.json()
        self.assertTrue(d["success"], d)
        self.assertIn("add_component:MeshComp", self._steps(d))
        bp = fu.EditorAssetLibrary.load_asset(DEFAULT_PKG + "BP_Comps")
        self.assertIsNotNone(bp)
        self.assertEqual(len(bp.components), 1)
        self.assertEqual(bp.components[0].get_name(), "MeshComp")
        self.assertEqual(bp.components[0].component_class,
                         "StaticMeshComponent")

    # ── 4. 组件属性 ───────────────────────────────────────
    def test_build_component_properties_applied(self):
        """组件属性 → 真实落到组件模板（C++ SetComponentProperty 语义）。"""
        r = self._build("BP_CompProps", compile_after=False,
                        components=[{"name": "BoxComp",
                                     "component_class": "BoxComponent",
                                     "properties": {"BoxExtent": [200, 100, 50]}}])
        d = r.json()
        self.assertTrue(d["success"], d)
        bp = fu.EditorAssetLibrary.load_asset(DEFAULT_PKG + "BP_CompProps")
        self.assertEqual(len(bp.components), 1)
        self.assertEqual(bp.components[0].get_editor_property("BoxExtent"),
                         "200 100 50")

    # ── 5. 接口 ───────────────────────────────────────────
    def test_build_interfaces_registered(self):
        """接口添加 → steps 含 add_interface:*。"""
        r = self._build("BP_Iface", compile_after=False,
                        interfaces=["IInteractable"])
        d = r.json()
        self.assertTrue(d["success"], d)
        self.assertIn("add_interface:IInteractable", self._steps(d))

    # ── 6. 重名蓝图 ───────────────────────────────────────
    def test_duplicate_blueprint_rejected(self):
        """重名蓝图 → success=False + category=asset_create + suggestion。"""
        fu.create_test_blueprint(DEFAULT_PKG + "BP_Dup")
        r = self._build("BP_Dup", compile_after=False)
        d = r.json()
        self.assertFalse(d["success"])
        self.assertEqual(d.get("category"), "asset_create")
        self.assertIn("蓝图已存在", d["error"])
        self.assertIn("建议", d.get("suggestion", "") + "建议")  # suggestion 非空
        self.assertTrue(d.get("suggestion"), d)

    # ── 7. compile_after=True 成功路径（GameThread 结果合并）──
    def test_compile_after_merges_gamthread_result(self):
        """GameThread 写出结果文件 → 步骤合并 + success 透传。"""
        def _writer(code):
            m = re.search(r"_result_file = (.+)", code)
            self.assertIsNotNone(m, "GameThread 模板未包含 _result_file")
            path = ast.literal_eval(m.group(1))
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"success": True,
                           "steps": [{"step": "compile", "status": "ok"}]}, f)

        with mock.patch.object(fu.BlueprintPythonBridge,
                               "execute_python_on_game_thread",
                               side_effect=_writer):
            r = self._build("BP_GT", compile_after=True)
        d = r.json()
        self.assertTrue(d["success"], d)
        self.assertIn("compile", self._steps(d))

    # ── 8. compile_after=True 超时（GameThread 无响应）────
    def test_compile_after_gamthread_timeout_partial_success(self):
        """GameThread 无响应 → partial_success 语义 + 剩余步骤清单。

        ⚠️ 前提（T-20260914-UE5BRIDGE-BUILD-HOLD-FIX · 修法2）：自 2026-09-14 起
        仅「真有图形操作」（events/functions/connections 非空）才进 GT 路径；纯编译
        改走进程内（in_process(修法2)）。故本用例**必须带一项最小图操作**，否则永远
        不进 GT、覆盖不到超时语义（2026-09-14 三项既有失败之一即此前提失效）。
        """
        # fake 的 execute_python_on_game_thread 是 no-op（不写结果文件）
        # → 轮询 30s 后走超时分支；patch time.sleep 让 300 次轮询瞬时完成。
        with mock.patch.object(ue5_bridge.time, "sleep", lambda *_: None):
            r = self._build("BP_GTT", compile_after=True,
                            events=[{"name": "OnGTProbe", "pos": [0, 0]}])
        d = r.json()
        self.assertFalse(d["success"])
        self.assertTrue(d.get("partial_success"), d)
        self.assertEqual(d.get("blueprint_path"), DEFAULT_PKG + "BP_GTT")
        self.assertIn("create_asset", d.get("steps_completed", []))
        self.assertIn("compile", d.get("steps_remaining", []))
        self.assertIn("GameThread 执行超时", d["error"])

    # ── 8b. 修法2：纯编译不进 GT（零 PyConsoleGlobalDict 注入）──
    def test_pure_compile_skips_gamthread_path(self):
        """无 events/functions/connections + compile_after=True → 进程内编译。

        回归锚点（T-20260914-UE5BRIDGE-BUILD-HOLD-FIX · 修法2）：纯编译**不得**调用
        execute_python_on_game_thread —— GT 注入会写持久 PyConsoleGlobalDict → 持有
        资产 → 后续 delete 恒 False（BUILD-HOLD-FIX 根因）。
        """
        with mock.patch.object(fu.BlueprintPythonBridge,
                               "execute_python_on_game_thread") as m_gt:
            r = self._build("BP_PureCompile", compile_after=True)
        d = r.json()
        self.assertTrue(d["success"], d)
        self.assertEqual(m_gt.call_count, 0,
                         "纯编译不应注入 GT（修法2 零注入）")
        step = [s for s in d.get("steps", []) if s.get("step") == "compile"]
        self.assertTrue(step, d)
        self.assertEqual(step[0].get("path"), "in_process(修法2)", d)

    # ── 9/10. Token 认证 ──────────────────────────────────
    def test_missing_token_403(self):
        """无 X-Skill-Token → 403。"""
        r = self._build("BP_NoTok", headers={}, compile_after=False)
        self.assertEqual(r.status_code, 403, r.text)

    def test_wrong_token_403(self):
        """错误 X-Skill-Token → 403。"""
        r = self._build("BP_BadTok",
                        headers={"X-Skill-Token": "wrong-token"},
                        compile_after=False)
        self.assertEqual(r.status_code, 403, r.text)

    # ── 11. UE API 异常 → 错误分类透传 ────────────────────
    def test_ue_api_exception_propagates_category(self):
        """bridge.add_member_variable 抛异常 → category=variable_add。"""
        with mock.patch.object(fu.BlueprintPythonBridge, "add_member_variable",
                               side_effect=RuntimeError("boom")):
            r = self._build("BP_Err", compile_after=False,
                            variables=[{"name": "X", "type": "float"}])
        d = r.json()
        self.assertFalse(d["success"])
        self.assertEqual(d.get("category"), "variable_add")
        self.assertIn("boom", d["error"])

    # ── 12. 请求默认值 ────────────────────────────────────
    def test_request_defaults(self):
        """默认 package_path=/Game/Blueprints/ · parent_class=Actor ·
        compile_after=True。"""
        defaults = ue5_bridge.BuildRequest(blueprint_name="BP_Defaults")
        self.assertEqual(defaults.package_path, DEFAULT_PKG)
        self.assertEqual(defaults.parent_class, "Actor")
        self.assertTrue(defaults.compile_after)
        self.assertEqual(defaults.variables, [])
        self.assertEqual(defaults.components, [])


# ══════════════════════════════════════════════════════════════
#  在线：真实 UE 端到端（ue5_runner.build → POST /build）
# ══════════════════════════════════════════════════════════════

BP_ONLINE = "/Game/BP_BuildOnline_Test"
TMP_NAME = "BP_BuildOnline_Test"


def _resolve_real_bridge_token():
    """直读 %APPDATA%/ue5_bridge/token（绕开 offline 模块 setenv 污染）。"""
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
    """通过 ue_send.py 在 UE 内执行 Python 代码（独立验证 / 清理用）。"""
    import subprocess
    # ⚠️ 修复 T-20260909-BRIDGE-ALIVE-TOKEN：Windows 下 text=True 默认按 GBK 解码子进程输出，
    # UE 侧中文输出（如「GameThread tick worker 已启动」）会触发
    # PytestUnhandledThreadExceptionWarning: UnicodeDecodeError。显式 UTF-8 + replace 兜底。
    r = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS_DIR, "ue_send.py"), code],
        capture_output=True, text=True, timeout=120, cwd=SCRIPTS_DIR,
        encoding="utf-8", errors="replace",
    )
    return (r.stdout + r.stderr).strip()


_TEARDOWN_CODE = r'''
import unreal
if unreal.EditorAssetLibrary.delete_asset("/Game/BP_BuildOnline_Test"):
    print("TEARDOWN_DEL_OK")
else:
    print("TEARDOWN_DEL_FAIL")
'''

_VERIFY_CODE = r'''
import unreal
bp = unreal.EditorAssetLibrary.load_asset("/Game/BP_BuildOnline_Test")
if not bp:
    print("VERIFY_NO_BP")
else:
    vs = unreal.BlueprintPythonBridge.read_variables(bp)
    # ⚠️ 修复 T-20260909-BRIDGE-ALIVE-TOKEN：直接 Python API 返回 BPVariableInfo 结构体
    # （属性 variable_name / variable_type），不是 HTTP /command 路径的 dict，
    # 旧写法 v.get("variable_name") 必抛 AttributeError。
    names = sorted(getattr(v, "variable_name", "") for v in vs)
    comps = [c.get_name() for c in bp.simple_construction_script.get_all_nodes()] \
        if hasattr(bp, "simple_construction_script") else []
    print("VERIFY_OK VARS=" + ",".join(names))
'''


@pytest.fixture()
def tmp_bp_build():
    """在线用例前置：确保临时蓝图不存在；teardown 删除（不污染工程）。"""
    _ue_send(_TEARDOWN_CODE)
    yield
    _ue_send(_TEARDOWN_CODE)


@pytest.mark.ue_online
def test_online_build_creates_blueprint_with_component(tmp_bp_build):
    """真实 UE：ue5_runner.build 创建蓝图 + 变量 + 组件 → 独立验证 + 清理。"""
    from ue5_runner import UE5Runner  # noqa: E402  延迟导入（离线无需）

    runner = UE5Runner(http_token=_resolve_real_bridge_token())
    res = runner.build({
        "blueprint_name": TMP_NAME,
        "package_path": "/Game/",
        "parent_class": "Actor",
        "variables": [{"name": "Health", "type": "float"}],
        "components": [{"name": "BoxComp", "component_class": "BoxComponent",
                        "properties": {"BoxExtent": [200, 100, 50]}}],
        "compile_after": True,
    })
    assert res.get("success"), f"/build 在线失败: {res}"
    steps = [s.get("step") for s in res.get("steps", [])]
    assert "create_asset" in steps, steps
    assert "add_variable:Health" in steps, steps
    assert "add_component:BoxComp" in steps, steps

    # 独立验证（不采信 /build 自报）：资产存在 + 变量真实写入
    out = _ue_send(_VERIFY_CODE)
    assert "VERIFY_OK" in out, f"独立验证失败: {out[-400:]}"
    assert "Health" in out, f"变量未真实写入: {out[-400:]}"


if __name__ == "__main__":
    unittest.main(verbosity=2)
