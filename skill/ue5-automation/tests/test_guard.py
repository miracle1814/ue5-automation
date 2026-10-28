# -*- coding: utf-8 -*-
"""调用守卫测试（T-20261027-GUARD）：高危确认 + 死循环保护。

纯逻辑测试：直接测 `_guard_call` / `_note_call_result` / `_save_dirty_assets`，
不触达真实编辑器（TEST_MODE + fake_unreal）。
"""
import os
import sys

os.environ["UE5_BRIDGE_TEST_MODE"] = "1"
os.environ["UE5_BRIDGE_TOKEN"] = "guard-test-token"

_SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPTS = os.path.join(_SKILL_ROOT, "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import pytest  # noqa: E402

import ue5_bridge as b  # noqa: E402


def setup_function(_):
    b._LOOP_STREAK.clear()


class TestDestructiveConfirm:

    def test_delete_asset_without_confirm_is_blocked(self):
        with pytest.raises(b.BridgeError) as e:
            b._guard_call("ue5_delete_asset", {"asset_path": "/Game/BP_X"})
        assert "confirm" in str(e.value)

    def test_delete_asset_with_confirm_passes(self):
        key = b._guard_call("ue5_delete_asset",
                            {"asset_path": "/Game/BP_X", "confirm": True})
        assert key.startswith("ue5_delete_asset|")

    def test_delete_node_without_confirm_is_blocked(self):
        with pytest.raises(b.BridgeError):
            b._guard_call("ue5_delete_node",
                          {"blueprint_path": "/Game/BP_X", "node_id": "n1"})

    def test_execute_command_delete_asset_blocked(self):
        """透传壳里调 delete_asset 同样要 confirm。"""
        with pytest.raises(b.BridgeError):
            b._guard_call("ue5_execute_command",
                          {"command": "delete_asset",
                           "params": {"asset_path": "/Game/BP_X"}})

    def test_apply_replace_requires_confirm(self):
        with pytest.raises(b.BridgeError) as e:
            b._guard_call("ue5_apply_blueprint_graph",
                          {"bp_path": "/Game/BP_X", "replace": True})
        assert "confirm" in str(e.value)

    def test_apply_non_replace_needs_no_confirm(self):
        key = b._guard_call("ue5_apply_blueprint_graph",
                            {"bp_path": "/Game/BP_X", "replace": False})
        assert key

    def test_safe_tool_unaffected(self):
        assert b._guard_call("ue5_search_blueprint_nodes", {"bp_path": "/Game/X"})
        assert b._guard_call("ue5_screenshot", {"source": "editor_viewport"})


class TestLoopGuard:

    def test_same_call_fails_twice_then_blocked(self):
        args = {"bp_path": "/Game/Nope"}
        key1 = b._guard_call("ue5_compile_and_diagnose", args)
        b._note_call_result(key1, False)
        key2 = b._guard_call("ue5_compile_and_diagnose", args)
        b._note_call_result(key2, False)
        # 编译类放宽到 5 次：第 2 次失败后仍允许
        key3 = b._guard_call("ue5_compile_and_diagnose", args)
        assert key3 == key1

    def test_non_volatile_tool_blocked_after_two(self):
        args = {"bp_path": "/Game/Nope"}
        for _ in range(2):
            b._note_call_result(
                b._guard_call("ue5_apply_blueprint_graph", args), False)
        with pytest.raises(b.BridgeError) as e:
            b._guard_call("ue5_apply_blueprint_graph", args)
        assert "连续失败" in str(e.value)

    def test_success_resets_all_streaks(self):
        args = {"bp_path": "/Game/Nope"}
        for _ in range(2):
            b._note_call_result(
                b._guard_call("ue5_apply_blueprint_graph", args), False)
        # 任何一次成功清零
        b._note_call_result(
            b._guard_call("ue5_search_blueprint_nodes", {"bp_path": "/Game/A"}), True)
        key = b._guard_call("ue5_apply_blueprint_graph", args)
        assert key   # 不再被拦

    def test_different_args_not_shared(self):
        for _ in range(2):
            b._note_call_result(
                b._guard_call("ue5_apply_blueprint_graph",
                              {"bp_path": "/Game/A"}), False)
        # 换参数 → 不共享计数
        assert b._guard_call("ue5_apply_blueprint_graph", {"bp_path": "/Game/B"})

    def test_time_window_resets_streak(self, monkeypatch):
        args = {"bp_path": "/Game/Nope"}
        key = b._guard_call("ue5_apply_blueprint_graph", args)
        for _ in range(2):
            b._note_call_result(
                b._guard_call("ue5_apply_blueprint_graph", args), False)
        # 假装已经过去 10 分钟 → 窗口过期，重新放行
        real_time = b.time.time
        monkeypatch.setattr(b.time, "time", lambda: real_time() + 600)
        assert b._guard_call("ue5_apply_blueprint_graph", args)


class TestAutoSaveField:

    def test_task_end_records_auto_save(self):
        import task_journal as tj
        tj.reset_for_tests()
        tj.task_begin({"goal": "检查门蓝图能不能编译"})
        out = tj.task_end({"status": "success",
                           "_auto_save": {"saved": ["/Game/BP_Door"],
                                          "clean": [], "failed": [],
                                          "missing": [], "assets": ["/Game/BP_Door"]}})
        assert out["task"]["auto_save"]["saved"] == ["/Game/BP_Door"]
        tj.reset_for_tests()

    def test_touched_assets_collects_paths(self):
        import task_journal as tj
        tj.reset_for_tests()
        tj.task_begin({"goal": "A", "steps": [{"name": "写", "hint": "apply_blueprint_graph"}]})
        tj.note_command("ue5_apply_blueprint_graph",
                        {"bp_path": "/Game/Blueprints/BP_X"}, {"verified": True})
        paths = tj.task_touched_assets()
        assert "/Game/Blueprints/BP_X" in paths
        tj.reset_for_tests()
