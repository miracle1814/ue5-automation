# -*- coding: utf-8 -*-
"""v6.3 变更回执 + 资产锁契约（离线）。"""
import hashlib
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


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    B._ASSET_LOCKS.clear()
    B._CONTENT_DIR_CACHE["path"] = str(tmp_path / "Content")
    tj.reset_for_tests()
    monkeypatch.setattr(B, "_TASK_JOURNAL_AVAILABLE", True)
    monkeypatch.setattr(B, "_task_journal", tj)
    yield
    B._ASSET_LOCKS.clear()
    tj.reset_for_tests()


def _write_asset(tmp_path, pkg, data=b"AAA"):
    rel = pkg[len("/Game/"):]
    p = tmp_path / "Content" / (rel + ".uasset")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def test_asset_stat_and_receipt_saved(tmp_path):
    p = _write_asset(tmp_path, "/Game/A/BP_X", b"AAA")
    before = B._asset_stat("/Game/A/BP_X")
    assert before["exists"] and before["sha1"] == hashlib.sha1(b"AAA").hexdigest()
    ctx = B._receipt_begin("save_asset", {"asset_path": "/Game/A/BP_X"})
    p.write_bytes(b"BBB")
    result = B._receipt_finish(ctx, {"success": True, "verified": True})
    r = result["receipt"]
    assert r["command"] == "save_asset"
    assert r["saved"] is True
    assert r["before"]["sha1"] != r["after"]["sha1"]
    assert r["verified"] is True


def test_receipt_without_asset_still_structured():
    ctx = B._receipt_begin("apply_graph", {"bp_path": "/Game/No/BP"})
    result = B._receipt_finish(ctx, {"success": True, "verified": True,
                                     "nodes_applied": 3, "edges_applied": 2})
    r = result["receipt"]
    assert r["asset"] == "/Game/No/BP"
    assert r["saved"] is False and r["nodes_applied"] == 3


def test_lock_conflict_between_tasks(monkeypatch, tmp_path):
    _write_asset(tmp_path, "/Game/A/BP_X")
    monkeypatch.setattr(tj, "active_task_id", lambda: "T-A")
    B._asset_lock_acquire("/Game/A/BP_X")
    monkeypatch.setattr(tj, "active_task_id", lambda: "T-B")
    with pytest.raises(B.BridgeError) as ei:
        B._asset_lock_acquire("/Game/A/BP_X")
    assert ei.value.category == "asset_locked"
    # 同一 owner 可重入
    monkeypatch.setattr(tj, "active_task_id", lambda: "T-A")
    B._asset_lock_acquire("/Game/A/BP_X")


def test_lock_released_on_task_end(monkeypatch, tmp_path):
    _write_asset(tmp_path, "/Game/A/BP_X")
    tj.task_begin({"goal": "g"})
    B._asset_lock_acquire("/Game/A/BP_X")
    assert "/Game/A/BP_X" in B._ASSET_LOCKS
    B._cmd_task_end({"status": "success"})
    assert "/Game/A/BP_X" not in B._ASSET_LOCKS


def test_adhoc_does_not_take_locks(tmp_path):
    """无任务（adhoc）不加锁：单进程由 GT 串行化；锁只服务多任务。"""
    _write_asset(tmp_path, "/Game/A/BP_X")
    assert tj.active_task_id() == ""
    ctx = B._receipt_begin("save_asset", {"asset_path": "/Game/A/BP_X"})
    assert ctx and "/Game/A/BP_X" not in B._ASSET_LOCKS
    B._receipt_finish(ctx, {"success": True})


def test_journal_receipts_capped_and_listed(tmp_path):
    tj.task_begin({"goal": "g"})
    for i in range(105):
        tj.note_receipt({"id": str(i), "command": "save_asset"})
    active = tj.task_list()["active"]
    assert len(active["receipts"]) == 100
    assert active["receipts"][-1]["id"] == "104"
