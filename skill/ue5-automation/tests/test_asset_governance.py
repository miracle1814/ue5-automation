# -*- coding: utf-8 -*-
"""资产治理（v5.0）：批量重命名/搬迁 + 任务级资产回滚。

锁定：
  ① dry-run 默认只出计划、不改动；有问题时 fail-loud；
  ② 真跑必须 confirm=true；逐条回读验证；
  ③ 任务账本记录 created/deleted/snapshots；
  ④ rollback：删除新建资产 + 从快照恢复被删资产；无快照则如实失败；
  ⑤ 破坏性守卫：batch 真跑/回滚必须 confirm，dry-run 豁免。
"""
import json
import os
import sys

os.environ["UE5_BRIDGE_TEST_MODE"] = "1"

_SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPTS = os.path.join(_SKILL_ROOT, "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import pytest  # noqa: E402
import ue5_bridge as B  # noqa: E402
import task_journal as tj  # noqa: E402
import fake_unreal as fu  # noqa: E402


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    fu.reset_fake()
    tj.reset_for_tests()
    monkeypatch.setattr(B, "_BRIDGE_TOKEN", "asset-test-token")
    monkeypatch.setattr(B, "_LOOP_STREAK", {})
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    fu.Paths.set_project_dir(str(tmp_path / "proj"))
    yield
    tj.reset_for_tests()


# ══════════════════════════════════════════════════════════
#  ① 批量资产操作
# ══════════════════════════════════════════════════════════

def test_batch_dry_run_plans_without_changes():
    fu.create_test_blueprint("/Game/A/BP_X")
    r = B._cmd_batch_asset_ops(
        [{"from": "/Game/A/BP_X", "to": "/Game/B/BP_X"}], dry_run=True)
    assert r["success"] is True and r["dry_run"] is True
    assert r["plan"][0]["state"] == "ok"
    # 什么都没动
    assert fu.EditorAssetLibrary.does_asset_exist("/Game/A/BP_X")
    assert not fu.EditorAssetLibrary.does_asset_exist("/Game/B/BP_X")


def test_batch_dry_run_reports_conflicts():
    fu.create_test_blueprint("/Game/A/BP_X")
    fu.create_test_blueprint("/Game/B/BP_X")
    r = B._cmd_batch_asset_ops(
        [{"from": "/Game/A/BP_X", "to": "/Game/B/BP_X"}], dry_run=True)
    assert r["success"] is False and r["_fail_loud"] is True
    assert "conflict_target_exists" in r["errors"][0]


def test_batch_real_run_requires_confirm():
    fu.create_test_blueprint("/Game/A/BP_X")
    with pytest.raises(B.BridgeError) as ei:
        B._cmd_batch_asset_ops(
            [{"from": "/Game/A/BP_X", "to": "/Game/B/BP_X"}],
            dry_run=False, confirm=False)
    assert ei.value.category == "destructive_guard"


def test_batch_real_run_renames_and_verifies():
    fu.create_test_blueprint("/Game/A/BP_X")
    r = B._cmd_batch_asset_ops(
        [{"from": "/Game/A/BP_X", "to": "/Game/B/BP_X"}],
        dry_run=False, confirm=True)
    assert r["success"] is True and len(r["renamed"]) == 1
    assert fu.EditorAssetLibrary.does_asset_exist("/Game/B/BP_X")
    assert not fu.EditorAssetLibrary.does_asset_exist("/Game/A/BP_X")


def test_batch_guard_allows_dry_run_without_confirm():
    key = B._guard_call("ue5_execute_command",
                        {"command": "batch_asset_ops",
                         "params": {"items": []}})
    assert key
    with pytest.raises(B.BridgeError):
        B._guard_call("ue5_execute_command",
                      {"command": "batch_asset_ops",
                       "params": {"items": [], "dry_run": False}})


# ══════════════════════════════════════════════════════════
#  ② 任务账本
# ══════════════════════════════════════════════════════════

def test_ledger_tracks_created_and_deleted():
    tj.task_begin({"goal": "g"})
    tj.note_created_asset("/Game/A/BP_New")
    tj.note_deleted_asset("/Game/A/BP_Old", {"asset": "/Game/A/BP_Old",
                                             "snapshot": "C:/snap/x.uasset",
                                             "bytes": 12})
    led = tj.task_asset_ledger()
    assert led["created_assets"] == ["/Game/A/BP_New"]
    assert led["deleted_assets"] == ["/Game/A/BP_Old"]
    assert led["asset_snapshots"][0]["snapshot"] == "C:/snap/x.uasset"
    tj.task_end({"status": "success"})
    # 结束后仍可查
    led2 = tj.task_asset_ledger(led["task_id"])
    assert led2["created_assets"] == ["/Game/A/BP_New"]


def test_ledger_unknown_task_errors():
    with pytest.raises(ValueError):
        tj.task_asset_ledger("20990101-99")


# ══════════════════════════════════════════════════════════
#  ③ 回滚
# ══════════════════════════════════════════════════════════

def test_rollback_requires_confirm():
    with pytest.raises(B.BridgeError) as ei:
        B._cmd_rollback_task_assets()
    assert ei.value.category == "destructive_guard"


def test_rollback_deletes_created_assets(monkeypatch):
    tj.task_begin({"goal": "g"})
    tj.note_created_asset("/Game/A/BP_New")
    fu.create_test_blueprint("/Game/A/BP_New")
    monkeypatch.setattr(B, "_delete_asset_cascade",
                        lambda p, n=1: {"success": True, "deleted": True})
    r = B._cmd_rollback_task_assets(confirm=True)
    assert r["success"] is True
    assert r["created_removed"][0]["status"] == "deleted"


def test_rollback_restores_deleted_from_snapshot(tmp_path):
    # 准备：项目 Content 下的原文件“已被删”，快照文件还在
    content = tmp_path / "proj" / "Content"
    (content / "A").mkdir(parents=True)
    snap = tmp_path / "snap.uasset"
    snap.write_bytes(b"UEASSET")
    tj.task_begin({"goal": "g"})
    tj.note_deleted_asset("/Game/A/BP_Old", {"asset": "/Game/A/BP_Old",
                                             "snapshot": str(snap), "bytes": 7})
    # fake 资产表里补一条，让 reload_asset 返回 True
    fu.create_test_blueprint("/Game/A/BP_Old")
    r = B._cmd_rollback_task_assets(confirm=True)
    assert r["success"] is True, r
    target = content / "A" / "BP_Old.uasset"
    assert target.is_file() and target.read_bytes() == b"UEASSET"
    assert r["deleted_restored"][0]["reloaded"] is True


def test_rollback_reports_missing_snapshot():
    tj.task_begin({"goal": "g"})
    tj.note_deleted_asset("/Game/A/BP_Old", {})
    r = B._cmd_rollback_task_assets(confirm=True)
    assert r["success"] is False and r["_fail_loud"] is True
    assert "无删除前快照" in r["failed"][0]["error"]


def test_snapshot_helper_copies_file(tmp_path):
    content = tmp_path / "proj" / "Content"
    (content / "A").mkdir(parents=True)
    src = content / "A" / "BP_X.uasset"
    src.write_bytes(b"DATA")
    tj.task_begin({"goal": "g"})
    info = B._snapshot_asset_file("/Game/A/BP_X", "deleted")
    assert info.get("snapshot") and os.path.isfile(info["snapshot"])
    assert open(info["snapshot"], "rb").read() == b"DATA"
    assert info["bytes"] == 4


# ══════════════════════════════════════════════════════════
#  ④ 命令注册
# ══════════════════════════════════════════════════════════

def test_asset_commands_registered():
    for c in ("batch_asset_ops", "task_assets", "rollback_task_assets"):
        assert c in B._COMMAND_WHITELIST, c
