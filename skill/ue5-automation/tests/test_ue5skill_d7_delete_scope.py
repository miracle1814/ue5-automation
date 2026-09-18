#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_ue5skill_d7_delete_scope.py — D-7 保存作用域收窄 · 离线回归
================================================================================
任务：T-20260917-D7-DELETE-SCOPE-FIX（maintainer派单 · 雨点儿拍板「肯定需要修复」）

缺陷（D-7）：`_delete_asset_cascade()` 删除成功后调全工程脏包保存
（save_dirty_packages 双 True）→ 用户「删一个临时资产」会**静默提交**所有未保存
的关卡 / 资产。实测 2026-09-17 13:00:08 连带写了 ElevatorDemo.umap + 3 件既有资产。

覆盖判据：
  J1 作用域实证 —— 正常删除路径**不再**全量保存；只对「相关包」（引用者包）调用
     已白名单化验证的 `EditorAssetLibrary.save_asset(path, only_if_is_dirty=True)`；
     并附**源码级守卫**：非注释代码中不得再出现全量保存 API。
  J2 防复活不回归 —— ① 删前不保存（09-13 v2 语义）由**事件顺序断言**保住；
     ② 被删包**自身**不进保存列表（保存它只可能写回磁盘 = 复活）；
     ③ 既有 test_delete_compile_fix.py 全绿（同套件运行）。
  J5 fail-loud 不丢 —— 删除失败路径行为逐字不变 + 该路径**零保存**。

层级：离线（UE5_BRIDGE_TEST_MODE=1 + fake unreal 注入），不依赖 UE。
作者：dev（代码开发 Agent）· 日期：2026-09-17
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

import ue5_bridge                              # noqa: E402
from fastapi.testclient import TestClient      # noqa: E402

BP = "/Game/Blueprints/BP_D7_Tmp"
MAP_PKG = "/Game/Maps/LevelA"


# ══════════════════════════════════════════════════════════
#  替身（全部记录**有序事件**，用于断言「删前不保存」）
# ══════════════════════════════════════════════════════════

class _Events:
    def __init__(self):
        self.log = []

    def add(self, s):
        self.log.append(s)
        return s

    def seq(self):
        return list(self.log)


class _StubEAL:
    """EditorAssetLibrary 替身（记录 save_asset 调用 · 可注入失败）。"""

    def __init__(self, ev, alive=True, absent_after_n_deletes=1,
                 with_save_api=True, save_raises=None, kwarg_unsupported=False):
        self.ev = ev
        self.alive = alive
        self.n = absent_after_n_deletes
        self.delete_calls = 0
        self.save_attempts = []          # [(path, kwargs)] —— 记录**尝试**
        self.save_calls = []             # [path] —— 记录**成功返回 True** 的
        self._save_raises = save_raises
        self._kwarg_unsupported = kwarg_unsupported
        if not with_save_api:
            self.save_asset = None       # hasattr True / callable False

    def does_asset_exist(self, path):
        return self.alive

    def load_asset(self, path):
        return object()

    def delete_asset(self, path):
        self.delete_calls += 1
        self.ev.add("delete#%d" % self.delete_calls)
        if self.n is not None and self.delete_calls >= self.n:
            self.alive = False
        return True

    def save_asset(self, path, **kw):
        self.save_attempts.append((path, dict(kw)))
        self.ev.add("save_asset:%s" % path)
        if self._save_raises is not None:
            raise self._save_raises
        return True

    def _save_asset_no_kwarg(self, path):     # 旧绑定：不支持关键字参数
        self.save_attempts.append((path, {}))
        self.ev.add("save_asset_nokw:%s" % path)
        return True


class _StubRegistry:
    def __init__(self, refs, ev):
        self.refs = refs
        self.ev = ev
        self.queried = []

    def get_referencers(self, path, opts=None):
        self.ev.add("get_referencers:%s" % path)
        self.queried.append(path)
        return list(self.refs)


class _StubUnreal:
    """fake unreal 模块面（含 EditorLoadingAndSavingUtils **探针**）。"""

    def __init__(self, eal, refs=None, with_registry=True, ev=None):
        self.ev = ev or _Events()
        self.EditorAssetLibrary = eal
        self.SystemLibrary = types.SimpleNamespace(collect_garbage=lambda: None)
        self.AssetEditorSubsystem = None        # close_all_editors 走 hasattr 守卫
        self.AssetRegistryDependencyOptions = type("_Opts", (), {})
        if with_registry:
            reg = _StubRegistry(refs or [], self.ev)
            self.AssetRegistryHelpers = types.SimpleNamespace(
                get_asset_registry=lambda: reg)
        else:
            self.AssetRegistryHelpers = None

        # 🔴 D-7 探针：全量保存 API 必须**永不被调用**
        self.save_dirty_calls = []

        class _ELASU:
            def save_dirty_packages(_self, *a, **kw):
                self.save_dirty_calls.append((a, kw))
                self.ev.add("save_dirty_packages(!!)")
                return True

        self.EditorLoadingAndSavingUtils = _ELASU()


def _run_inline(fn, *a, **k):
    """GT 同步替身：丢弃 timeout 等调度参数，直接内联执行。"""
    return fn()


class _Base(unittest.TestCase):
    def setUp(self):
        self._orig_sync = ue5_bridge._run_on_game_thread_sync
        ue5_bridge._run_on_game_thread_sync = _run_inline
        self._orig_unreal = ue5_bridge.unreal

    def tearDown(self):
        ue5_bridge._run_on_game_thread_sync = self._orig_sync
        ue5_bridge.unreal = self._orig_unreal

    def _wire(self, eal, refs=None, with_registry=True, ev=None):
        # ⚠️ 共享同一份有序事件日志：EAL 与 unreal 替身必须记进同一序列，
        #    否则「删前不保存」的顺序断言无从成立。
        ev = ev or getattr(eal, "ev", None) or _Events()
        eal.ev = ev
        u = _StubUnreal(eal, refs=refs, with_registry=with_registry, ev=ev)
        ue5_bridge.unreal = u
        return u


# ══════════════════════════════════════════════════════════
#  J1 · 作用域实证
# ══════════════════════════════════════════════════════════

class ScopeJ1Tests(_Base):

    def test_no_global_save_on_success_path(self):
        """🔴 J1 核心：正常删除路径**零**全量保存（旧实现恒调 (True, True)）。"""
        eal = _StubEAL(_Events(), alive=True, absent_after_n_deletes=1)
        u = self._wire(eal)
        out = ue5_bridge._delete_asset_cascade(BP)
        self.assertTrue(out["success"], out)
        self.assertEqual(u.save_dirty_calls, [],
                         "⛔ 不得再调用全量脏包保存")
        self.assertNotIn("save_dirty_packages(!!)", u.ev.seq())

    def test_only_related_packages_saved(self):
        """J1：只对「引用者包」调用 save_asset（路径字符串 · only_if_is_dirty=True）。"""
        eal = _StubEAL(_Events(), alive=True, absent_after_n_deletes=1)
        u = self._wire(eal, refs=["/Game/Maps/LevelA.LevelA"])
        out = ue5_bridge._delete_asset_cascade(BP)
        self.assertTrue(out["success"], out)
        self.assertEqual(out["save_scope"], "related_packages")
        self.assertEqual(out["saved_related_packages"], [MAP_PKG])
        self.assertEqual(eal.save_attempts, [(MAP_PKG, {"only_if_is_dirty": True})])
        self.assertEqual(u.save_dirty_calls, [])

    def test_deleted_package_itself_never_saved(self):
        """🔴 J2：被删包自身不得进保存列表（保存它 = 写回磁盘 = 复活）。"""
        eal = _StubEAL(_Events(), alive=True, absent_after_n_deletes=1)
        # 注册表误把被删包自己列为引用者 → 实现必须过滤掉
        u = self._wire(eal, refs=[BP + ".BP_D7_Tmp", "/Game/Maps/LevelA.LevelA"])
        out = ue5_bridge._delete_asset_cascade(BP)
        self.assertEqual(out["saved_related_packages"], [MAP_PKG])
        self.assertNotIn(BP, [p for p, _ in eal.save_attempts])

    def test_skip_reason_when_no_referencers(self):
        """J1：零引用者 → **不调用任何保存** + 显式 save_skipped_reason。"""
        eal = _StubEAL(_Events(), alive=True, absent_after_n_deletes=1)
        u = self._wire(eal, refs=[])
        out = ue5_bridge._delete_asset_cascade(BP)
        self.assertTrue(out["success"], out)
        self.assertEqual(out["save_skipped_reason"], "no_related_packages")
        self.assertEqual(out["saved_related_packages"], [])
        self.assertEqual(eal.save_attempts, [])
        self.assertEqual(u.save_dirty_calls, [])

    def test_skip_reason_when_registry_unavailable(self):
        """J1：注册表不可用 → 无从定位相关包 → 不全局保存 + 诚实原因。"""
        eal = _StubEAL(_Events(), alive=True, absent_after_n_deletes=1)
        u = self._wire(eal, with_registry=False)
        out = ue5_bridge._delete_asset_cascade(BP)
        self.assertTrue(out["success"], out)
        self.assertEqual(out["save_skipped_reason"], "referencers_api_unavailable")
        self.assertEqual(u.save_dirty_calls, [])
        self.assertIn("save_skipped: referencers_api_unavailable", out["trail"])

    def test_skip_reason_when_save_asset_api_missing(self):
        """J1：save_asset 缺失 → 跳过（⛔ 绝不退化到全量保存）+ 诚实原因。"""
        eal = _StubEAL(_Events(), alive=True, absent_after_n_deletes=1,
                       with_save_api=False)
        u = self._wire(eal, refs=["/Game/Maps/LevelA.LevelA"])
        out = ue5_bridge._delete_asset_cascade(BP)
        self.assertTrue(out["success"], out)
        self.assertEqual(out["save_skipped_reason"], "save_asset_api_unavailable")
        self.assertEqual(u.save_dirty_calls, [])

    def test_save_asset_error_is_recorded_not_swallowed(self):
        """J1/诚实性：save_asset 抛错 → 记 trail（⛔ 不吞异常）+ 不全局保存。"""
        eal = _StubEAL(_Events(), alive=True, absent_after_n_deletes=1,
                       save_raises=RuntimeError("disk full"))
        u = self._wire(eal, refs=["/Game/Maps/LevelA.LevelA"])
        out = ue5_bridge._delete_asset_cascade(BP)
        self.assertTrue(out["success"], out)          # 删除本身成功
        self.assertEqual(out["save_skipped_reason"], "related_packages_not_dirty")
        self.assertTrue(any("save_related_err" in t for t in out["trail"]), out["trail"])
        self.assertEqual(u.save_dirty_calls, [])

    def test_no_unguarded_degrade_on_typeerror(self):
        """J1：save_asset 不支持关键字参数 → ⛔ 不退化为「无条件落盘」重试。

        注：关键字绑定失败发生在**进入函数体之前** → 替身无法记录调用；
        故以 trail 中 `save_related_typeerr` **恰好一条** 证明「只尝试一次、无降级重试」。
        """
        class _EAL(_StubEAL):
            save_asset = _StubEAL._save_asset_no_kwarg

        eal = _EAL(_Events(), alive=True, absent_after_n_deletes=1)
        u = self._wire(eal, refs=["/Game/Maps/LevelA.LevelA"])
        out = ue5_bridge._delete_asset_cascade(BP)
        self.assertEqual(sum("save_related_typeerr" in t for t in out["trail"]), 1)
        self.assertEqual(out["save_skipped_reason"], "related_packages_not_dirty")
        self.assertEqual(u.save_dirty_calls, [])

    def test_command_layer_exposes_scope_fields(self):
        """/command delete_asset 层透出作用域字段（可观测性）。"""
        client = TestClient(ue5_bridge.app)
        eal = _StubEAL(_Events(), alive=True, absent_after_n_deletes=1)
        self._wire(eal, refs=["/Game/Maps/LevelA.LevelA"])
        data = client.post("/command",
                           json={"command": "delete_asset",
                                 "params": {"asset_path": BP}},
                           headers={"X-Skill-Token": "integration-test-token"}).json()
        self.assertTrue(data["success"], data)
        res = data["result"]
        self.assertEqual(res["save_scope"], "related_packages")
        self.assertEqual(res["saved_related_packages"], [MAP_PKG])


# ══════════════════════════════════════════════════════════
#  J2 · 防复活不回归（事件顺序 + 幂等路径）
# ══════════════════════════════════════════════════════════

class AntiResurrectJ2Tests(_Base):

    def test_no_save_before_delete(self):
        """🔴 J2：09-13 v2 语义（删前不保存，防内存态资产落盘复活）由顺序断言保住。"""
        eal = _StubEAL(_Events(), alive=True, absent_after_n_deletes=1)
        u = self._wire(eal, refs=["/Game/Maps/LevelA.LevelA"])
        ue5_bridge._delete_asset_cascade(BP)
        seq = u.ev.seq()
        first_delete = next(i for i, s in enumerate(seq) if s.startswith("delete#"))
        first_save = next((i for i, s in enumerate(seq)
                           if s.startswith("save_asset")), None)
        self.assertIsNotNone(first_save, seq)
        self.assertLess(first_delete, first_save,
                        "⛔ 保存必须发生在删除成功**之后**")

    def test_referencers_collected_before_delete(self):
        """J2：相关包必须在删除**前**采集（删后注册表项消失 → 无从定位）。"""
        eal = _StubEAL(_Events(), alive=True, absent_after_n_deletes=1)
        u = self._wire(eal, refs=["/Game/Maps/LevelA.LevelA"])
        ue5_bridge._delete_asset_cascade(BP)
        seq = u.ev.seq()
        q = next(i for i, s in enumerate(seq) if s.startswith("get_referencers"))
        d = next(i for i, s in enumerate(seq) if s.startswith("delete#"))
        self.assertLess(q, d)

    def test_already_absent_is_idempotent_and_saves_nothing(self):
        """J2：已不存在（幂等成功）路径不采集、不保存。"""
        eal = _StubEAL(_Events(), alive=True, absent_after_n_deletes=0)
        eal.alive = False
        u = self._wire(eal, refs=["/Game/Maps/LevelA.LevelA"])
        out = ue5_bridge._delete_asset_cascade(BP)
        self.assertTrue(out["already_absent"], out)
        self.assertEqual(out["attempts"], 0)
        self.assertEqual(eal.save_attempts, [])
        self.assertNotIn("save_scope", out)          # 未走删除分支 → 无该字段
        self.assertEqual(u.save_dirty_calls, [])


# ══════════════════════════════════════════════════════════
#  J5 · fail-loud 逐字不变 + 零保存
# ══════════════════════════════════════════════════════════

class FailLoudJ5Tests(_Base):

    def test_fail_loud_unchanged_and_no_save(self):
        eal = _StubEAL(_Events(), alive=True, absent_after_n_deletes=None)
        # ⚠️ 与 test_delete_compile_fix.py 的用例同构：替身**无** AssetRegistryHelpers
        #   → engine_reason 逐字为 asset_still_exists_after_delete（J5 不回归口径）
        u = self._wire(eal, with_registry=False)
        out = ue5_bridge._delete_asset_cascade(BP)
        self.assertFalse(out["success"], out)
        self.assertFalse(out["deleted"])
        self.assertEqual(out["attempts"], 3)
        self.assertEqual(out["error"], "asset still exists after delete: %s" % BP)
        self.assertEqual(out["engine_reason"], "asset_still_exists_after_delete")
        self.assertIsNone(out["referencers"])
        self.assertIs(out["attempted_with_python_hold"], False)
        self.assertTrue(out["trail"])
        # 🔴 J5 附加：失败路径**零保存**（删不掉就不该落任何盘）
        self.assertEqual(eal.save_attempts, [])
        self.assertEqual(u.save_dirty_calls, [])

    def test_retry_hard_cap_still_three(self):
        eal = _StubEAL(_Events(), alive=True, absent_after_n_deletes=None)
        self._wire(eal)
        out = ue5_bridge._delete_asset_cascade(BP, max_attempts=99)
        self.assertLessEqual(out["attempts"], 3)


# ══════════════════════════════════════════════════════════
#  源码级守卫（缺陷写法不得复活）
# ══════════════════════════════════════════════════════════

class SourceGuardTests(unittest.TestCase):

    @staticmethod
    def _code_only():
        src = io.open(ue5_bridge.__file__, encoding="utf-8").read()
        return "\n".join(l for l in src.splitlines()
                         if not l.lstrip().startswith("#"))

    def test_no_global_save_in_code(self):
        """🔴 非注释代码中不得出现全量脏包保存 API（注释可留历史说明）。"""
        code = self._code_only()
        self.assertNotIn("save_dirty_packages", code)
        self.assertNotIn("EditorLoadingAndSavingUtils", code)

    def test_scoped_save_present(self):
        src = io.open(ue5_bridge.__file__, encoding="utf-8").read()
        self.assertIn("_save_related", src)
        self.assertIn("save_skipped_reason", src)
        self.assertIn("saved_related_packages", src)

    def test_save_asset_uses_dirty_guard(self):
        src = io.open(ue5_bridge.__file__, encoding="utf-8").read()
        self.assertIn("only_if_is_dirty=True", src)


if __name__ == "__main__":
    unittest.main()
