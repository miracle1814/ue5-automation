#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_delete_compile_fix.py — D-1/D-2 离线回归（T-20260913-UE5BRIDGE-DELETE-COMPILE-FIX）
================================================================================
覆盖：
  D-1 资产级联删除
    · 三级路径：delete_loaded_asset 命中 / fallback delete_asset 命中 / 已不存在
    · fail-loud：3 轮后仍存在 → success=False + error="asset still exists after delete: …"
    · 重试上限硬约束（max_attempts 传入 99 → 实际 ≤3）
    · /command delete_asset 层 success=false 传播（opt-in `_fail_loud`）
    · 回归面：未标 `_fail_loud` 的既有命令行为不变
  D-2 compile_status
    · _bp_status_normalize 全枚举映射（int / 枚举名 / 非法值）
    · _bp_status_str 多路回退 + 派生 + 兜底 'unknown'（⛔ 永不 ''/None）
    · 源码级回归守卫：禁止 `str(bp.status)` / `hasattr(bp, 'status')` 复活

层级：离线（UE5_BRIDGE_TEST_MODE=1 + fake_unreal 注入），不依赖 UE。
作者：dev（代码开发 Agent）· 日期：2026-09-13
"""
import io
import os
import sys
import types
import unittest

os.environ["UE5_BRIDGE_TEST_MODE"] = "1"
os.environ["UE5_BRIDGE_TOKEN"] = "integration-test-token"

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(SKILL_ROOT, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import ue5_bridge            # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

BP = "/Game/Blueprints/BP_TmpDel"


# ══════════════════════════════════════════════════════════
#  替身
# ══════════════════════════════════════════════════════════

class _StubEAL:
    """可控 EditorAssetLibrary 替身。

    absent_after_n_deletes: 第 N 次删除调用后资产「消失」；None = 永不存在。
    with_loaded_delete: 是否暴露 delete_loaded_asset。
    """

    def __init__(self, alive=True, absent_after_n_deletes=None,
                 with_loaded_delete=True):
        self.alive = alive
        self.n = absent_after_n_deletes
        self.delete_calls = 0
        self.trail = []
        if not with_loaded_delete:
            self.delete_loaded_asset = None  # hasattr → True 但 callable → False

    def does_asset_exist(self, path):
        self.trail.append("does_asset_exist")
        return self.alive

    def load_asset(self, path):
        self.trail.append("load_asset")
        return object()

    def _on_delete(self):
        self.delete_calls += 1
        self.trail.append("delete#%d" % self.delete_calls)
        if self.n is not None and self.delete_calls >= self.n:
            self.alive = False
        return True

    def delete_loaded_asset(self, obj):
        return self._on_delete()

    def delete_asset(self, path):
        return self._on_delete()


class _StubUnreal:
    def __init__(self, eal, with_gc=False):
        self.EditorAssetLibrary = eal
        self.SystemLibrary = types.SimpleNamespace(
            collect_garbage=(lambda: None) if with_gc else None)
        # close_all_editors_for_asset 故意缺失 → 走 hasattr 守卫分支
        self.AssetEditorSubsystem = None
        self.EditorLoadingAndSavingUtils = None


def _run_inline(fn, *a, **k):
    """GT 同步替身：丢弃 timeout 等调度参数，直接内联执行。"""
    return fn()


class _Base(unittest.TestCase):
    def setUp(self):
        self._orig_sync = ue5_bridge._run_on_game_thread_sync
        ue5_bridge._run_on_game_thread_sync = _run_inline   # GT 同步 → 内联
        self._orig_unreal = ue5_bridge.unreal

    def tearDown(self):
        ue5_bridge._run_on_game_thread_sync = self._orig_sync
        ue5_bridge.unreal = self._orig_unreal

    def _wire(self, eal, with_gc=False):
        ue5_bridge.unreal = _StubUnreal(eal, with_gc)


# ══════════════════════════════════════════════════════════
#  D-1 · 级联删除
# ══════════════════════════════════════════════════════════

class CascadeDeleteTests(_Base):

    def test_absent_asset_is_idempotent_success(self):
        eal = _StubEAL(alive=False)
        self._wire(eal)
        out = ue5_bridge._delete_asset_cascade(BP)
        self.assertTrue(out["success"], out)
        self.assertTrue(out["already_absent"])
        self.assertEqual(out["attempts"], 0)

    def test_loaded_delete_path_succeeds_first_attempt(self):
        eal = _StubEAL(alive=True, absent_after_n_deletes=1)
        self._wire(eal)
        out = ue5_bridge._delete_asset_cascade(BP)
        self.assertTrue(out["success"], out)
        self.assertEqual(out["attempts"], 1, out)
        self.assertIn("delete#1", eal.trail)

    def test_fallback_delete_asset_path(self):
        """delete_loaded_asset 不可用 → fallback delete_asset 仍能成功。"""
        eal = _StubEAL(alive=True, absent_after_n_deletes=1,
                       with_loaded_delete=False)
        self._wire(eal)
        out = ue5_bridge._delete_asset_cascade(BP)
        self.assertTrue(out["success"], out)
        self.assertEqual(out["attempts"], 1, out)

    def test_fail_loud_when_still_exists(self):
        """🔴 D-1 判据：3 轮后仍存在 → success=False + 明确 error（⛔ 禁假成功）。"""
        eal = _StubEAL(alive=True, absent_after_n_deletes=None)
        self._wire(eal)
        out = ue5_bridge._delete_asset_cascade(BP)
        self.assertFalse(out["success"], out)
        self.assertFalse(out["deleted"])
        self.assertEqual(out["attempts"], 3, out)
        self.assertEqual(out["error"],
                         "asset still exists after delete: %s" % BP)
        self.assertTrue(out["trail"])

    def test_retry_hard_cap_is_three(self):
        """重试上限硬约束：传入 99 → 实际 ≤3。"""
        eal = _StubEAL(alive=True)
        self._wire(eal)
        out = ue5_bridge._delete_asset_cascade(BP, max_attempts=99)
        self.assertLessEqual(out["attempts"], 3)
        self.assertEqual(out["attempts"], 3)

    def test_bad_path_rejected(self):
        eal = _StubEAL()
        self._wire(eal)
        with self.assertRaises(ue5_bridge.BridgeError):
            ue5_bridge._delete_asset_cascade("Blueprints/BP_TmpDel")


# ══════════════════════════════════════════════════════════
#  F1/F4 · 删除路径禁止持有 Python 包装器（T-20260913-UE5BRIDGE-DELETE-F1F4-FIX）
# ══════════════════════════════════════════════════════════

class F1NoPythonWrapperHoldTests(_Base):
    """F1 判据：删除路径**不得持有 Python 包装器**。

    根因（H1 高置信）：旧实现 `_obj = eal.load_asset(pkg_path)` 与
    `delete_loaded_asset(_obj)` 同帧 → 引擎判「被 root 对象 GCObjectReferencer_0
    外部引用」→ `ObjectTools::DeleteSingleObject` return false。
    F1：删除调用**只传路径**（`delete_asset(path)`），不残留 `unreal.Object`。
    """

    def test_delete_path_does_not_load_asset(self):
        """正常删除链中 `load_asset` **零调用**（删除不靠对象包装器）。"""
        eal = _StubEAL(alive=True, absent_after_n_deletes=1)  # 暴露 delete_loaded_asset
        self._wire(eal)
        out = ue5_bridge._delete_asset_cascade(BP)
        self.assertTrue(out["success"], out)
        self.assertNotIn("load_asset", eal.trail)          # 🔴 F1 核心判据
        self.assertIn("delete#1", eal.trail)

    def test_delete_loaded_asset_never_invoked(self):
        """即便替身暴露 `delete_loaded_asset`，F1 后也**不得**被调用（不传 obj）。"""
        class _Rec(_StubEAL):
            def __init__(self, **kw):
                super().__init__(**kw)
                self.loaded_calls = 0

            def delete_loaded_asset(self, obj):
                self.loaded_calls += 1
                return self._on_delete()

        eal = _Rec(alive=True, absent_after_n_deletes=1)
        self._wire(eal)
        out = ue5_bridge._delete_asset_cascade(BP)
        self.assertTrue(out["success"], out)
        self.assertEqual(eal.loaded_calls, 0)               # 🔴 F1：不经 obj 删除

    def test_fail_loud_carries_engine_diag(self):
        """F4：fail-loud 返回带 engine_reason / referencers / attempted_with_python_hold。"""
        eal = _StubEAL(alive=True, absent_after_n_deletes=None)
        self._wire(eal)
        out = ue5_bridge._delete_asset_cascade(BP)
        self.assertFalse(out["success"], out)
        # 自证式诊断：F1 后恒 False（删除调用不再持有 Python 包装器）
        self.assertIs(out["attempted_with_python_hold"], False)
        self.assertIn("engine_reason", out)
        self.assertEqual(out["engine_reason"], "asset_still_exists_after_delete")
        # 替身无 AssetRegistryHelpers → referencers 优雅降级为 None（不抛）
        self.assertIsNone(out["referencers"])


# ══════════════════════════════════════════════════════════
#  D-1 · /command 层 fail-loud 传播
# ══════════════════════════════════════════════════════════

class DeleteCommandTests(_Base):

    def setUp(self):
        super().setUp()
        self.client = TestClient(ue5_bridge.app)
        self.headers = {"X-Skill-Token": "integration-test-token"}

    def _cmd(self, name, **params):
        return self.client.post(
            "/command", json={"command": name, "params": params},
            headers=self.headers).json()

    def test_delete_asset_registered(self):
        self.assertIn("delete_asset", ue5_bridge._COMMAND_WHITELIST)

    def test_command_success_path(self):
        self._wire(_StubEAL(alive=True, absent_after_n_deletes=1))
        data = self._cmd("delete_asset", asset_path=BP)
        self.assertTrue(data["success"], data)
        self.assertTrue(data["result"]["deleted"])

    def test_command_fail_loud_path(self):
        """🔴 失败时必须 success=false（旧实现丢弃返回值 → 假成功）。"""
        self._wire(_StubEAL(alive=True, absent_after_n_deletes=None))
        data = self._cmd("delete_asset", asset_path=BP)
        self.assertFalse(data["success"], data)
        self.assertIn("asset still exists after delete", data["error"])
        self.assertEqual(data["result"]["attempts"], 3)

    def test_other_commands_unaffected(self):
        """回归面：未标 _fail_loud 的既有命令行为不变（health 仍 success=true）。"""
        data = self._cmd("health")
        self.assertTrue(data["success"], data)


# ══════════════════════════════════════════════════════════
#  D-2 · 编译状态
# ══════════════════════════════════════════════════════════

class StatusNormalizeTests(unittest.TestCase):

    def test_index_mapping(self):
        m = {0: "BS_Unknown", 1: "BS_Dirty", 2: "BS_Error",
             3: "BS_UpToDate", 4: "BS_UpToDateWithWarnings",
             5: "BS_BeingCreated"}
        for k, v in m.items():
            self.assertEqual(ue5_bridge._bp_status_normalize(k), v)

    def test_enum_name_mapping(self):
        self.assertEqual(
            ue5_bridge._bp_status_normalize("EBlueprintStatus.BS_UpToDate"),
            "BS_UpToDate")
        self.assertEqual(
            ue5_bridge._bp_status_normalize("BS_UP_TO_DATE_WITH_WARNINGS"),
            "BS_UpToDateWithWarnings")
        self.assertEqual(
            ue5_bridge._bp_status_normalize("EBlueprintStatus.BS_Error"),
            "BS_Error")

    def test_invalid_returns_none(self):
        for bad in (None, True, False, "", "garbage", object()):
            self.assertIsNone(ue5_bridge._bp_status_normalize(bad), bad)


class _BPStub:
    """get_editor_property 抛异常（复刻 UE5.1 'protected' 实测）的蓝图替身。"""

    def __init__(self, attr=None, gep_raises=True):
        self._attr = attr
        self._gep_raises = gep_raises
        if attr is not None:
            self.status = attr      # 复刻「属性可读」的 UE 版本路径

    def get_editor_property(self, name):
        if self._gep_raises:
            raise RuntimeError(
                "Blueprint: Property 'Status' for attribute 'status' on "
                "'Blueprint' is protected and cannot be read")
        return self._attr


class StatusStrTests(unittest.TestCase):

    def test_derived_from_compile_true(self):
        self.assertEqual(
            ue5_bridge._bp_status_str(_BPStub(), compiled=True), "BS_UpToDate")

    def test_derived_from_compile_false(self):
        self.assertEqual(
            ue5_bridge._bp_status_str(_BPStub(), compiled=False), "BS_Error")

    def test_unknown_when_nothing_readable(self):
        self.assertEqual(ue5_bridge._bp_status_str(_BPStub()), "unknown")

    def test_attr_fallback_used_when_gep_protected(self):
        self.assertEqual(
            ue5_bridge._bp_status_str(_BPStub(attr=2)), "BS_Error")

    def test_never_empty_or_none(self):
        """🔴 D-2 判据：⛔ 不得返回 '' / None（回读 null/空 = 本缺陷）。"""
        for kw in ({}, {"compiled": True}, {"compiled": False}):
            v = ue5_bridge._bp_status_str(_BPStub(), **kw)
            self.assertIsInstance(v, str, (v, kw))
            self.assertTrue(v, (v, kw))
            self.assertNotIn(v.lower(), ("none", "null"), (v, kw))


class SourceRegressionGuard(unittest.TestCase):
    """源码级守卫：缺陷写法不得复活。"""

    def test_no_legacy_status_read(self):
        src = io.open(ue5_bridge.__file__, encoding="utf-8").read()
        self.assertNotIn("str(bp.status)", src)
        self.assertNotIn("hasattr(bp, 'status')", src)
        self.assertNotIn('hasattr(bp, "status")', src)

    def test_default_compile_status_not_empty(self):
        src = io.open(ue5_bridge.__file__, encoding="utf-8").read()
        self.assertNotIn('"compile_status": ""', src)

    def test_rollback_uses_cascade(self):
        src = io.open(ue5_bridge.__file__, encoding="utf-8").read()
        self.assertNotIn(
            "_run_on_game_thread_sync(unreal.EditorAssetLibrary.delete_asset, path)",
            src)
        self.assertIn("_delete_asset_cascade(path)", src)


if __name__ == "__main__":
    unittest.main()
