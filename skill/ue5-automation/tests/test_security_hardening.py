# -*- coding: utf-8 -*-
"""v6.4 安全硬化：路径逃逸 / 输出白名单 / CSV 上限 / 日志字面量。"""
import os
import sys

os.environ["UE5_BRIDGE_TEST_MODE"] = "1"
_SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPTS = os.path.join(_SKILL_ROOT, "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import pytest  # noqa: E402
import ue5_bridge as B  # noqa: E402


def test_safe_game_pkg_rejects_traversal():
    assert B._safe_game_pkg("/Game/A/BP_X") == "/Game/A/BP_X"
    for bad in ("/Game/../Secret", "/Game/A/./B", "/Game/A\\B",
                "/Engine/BasicShapes/Cube", "Game/A/B", ""):
        assert B._safe_game_pkg(bad) == "", bad


def test_asset_stat_rejects_escape():
    assert B._asset_stat("/Game/../etc/passwd")["exists"] is False
    assert B._asset_stat("/Game/../etc/passwd").get("error") == "unsafe_path"


def test_snapshot_rejects_escape():
    assert B._snapshot_asset_file("/Game/../BP_X", "deleted") == {}


def test_batch_rejects_traversal():
    with pytest.raises(B.BridgeError):
        B._cmd_batch_asset_ops(
            [{"from": "/Game/A/../B", "to": "/Game/C/D"}], dry_run=True)


def test_delete_asset_rejects_non_game():
    with pytest.raises(B.BridgeError) as ei:
        B._cmd_delete_asset("/Engine/BasicShapes/Cube")
    assert ei.value.category == "param_type"


def test_datatable_csv_size_cap():
    with pytest.raises(B.BridgeError) as ei:
        B._cmd_datatable_write_csv("/Game/T/DT_X", "x" * (5 * 1024 * 1024 + 1),
                                   confirm=True)
    assert ei.value.category == "param_type"


def test_scenario_junit_path_escape_rejected(monkeypatch):
    monkeypatch.setattr(B, "_GT_TASK_RUNNING", False)
    with pytest.raises(B.BridgeError) as ei:
        B._cmd_verify_scenario(
            "x", [{"type": "wait", "seconds": 0}], start_pie=False,
            junit_path=r"C:\Windows\evil.xml")
    assert ei.value.category == "path_escape"


def test_scenario_log_pattern_is_literal(monkeypatch):
    seen = {}

    def logs(*a, **k):
        seen["pattern"] = a[2]
        return {"success": True, "data": {"count": 1}}

    monkeypatch.setattr(B, "_cmd_logs_read", logs)
    monkeypatch.setattr(B, "_GT_TASK_RUNNING", False)
    r = B._cmd_verify_scenario(
        "x", [{"type": "log_contains", "pattern": "a.b", "timeout": 0.1}],
        start_pie=False, junit_path=os.path.join(
            os.environ.get("TEMP", "."), "ue5_bridge",
            "scenario_regex_test.xml"))
    assert r["verified"] is True
    assert seen["pattern"] == r"a\.b", seen


def test_batch_acquires_locks_for_all_items(monkeypatch):
    B._ASSET_LOCKS.clear()
    monkeypatch.setattr(B, "_lock_owner", lambda: "T-BATCH")
    ctx = B._receipt_begin("batch_asset_ops", {"items": [
        {"from": "/Game/A/BP_1", "to": "/Game/B/BP_1"},
        {"from": "/Game/A/BP_2", "to": "/Game/B/BP_2"}]})
    assert set(ctx["assets"]) == {"/Game/A/BP_1", "/Game/B/BP_1",
                                  "/Game/A/BP_2", "/Game/B/BP_2"}
    assert len(B._ASSET_LOCKS) == 4
    B._release_locks("T-BATCH")


def test_logs_read_rejects_outside_roots(tmp_path):
    """logs_read 的 log_file 只允许项目日志/临时目录，禁止读 token 等任意文件。"""
    secret = r"C:\Windows\win.ini"
    with pytest.raises(B.BridgeError):
        B._cmd_logs_read(log_file=secret)


def test_logs_regex_redos_rejected():
    with pytest.raises(B.BridgeError):
        B._cmd_logs_read(pattern=r"(a+)+$")
