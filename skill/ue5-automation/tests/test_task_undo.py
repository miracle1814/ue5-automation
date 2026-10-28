#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_task_undo.py — 任务级撤销（T-20261027-TASK-UNDO）契约测试
=============================================================
C++ 侧（BlueprintTransaction.cpp）需要真机验证；本文件锁定 Python 侧的
可离线验证的契约：

  ① 写命令包装：_dispatch_with_tx 只在事务命令/SDK 可用时包事务；
     Begin 失败（用户事务在场）→ 原样执行且不 End；异常路径必须 End。
  ② 断线/删节点等 MCP 直连写路径有事务别名，别名目标必须在事务命令集合内。
  ③ agent_undo / agent_undo_status：JSON 解析、参数校验、0 步撤销 fail-loud。
  ④ task_journal.note_rollback：回滚记录进任务账本（面板"已回滚 N 步"）。
  ⑤ /ui/api/task 带 undo 字段 + 2s 缓存（观测面不被 GT 排队拖死）。
  ⑥ 三个分派入口都确实走了 _dispatch_with_tx（源码级回归，防回退）。
"""

import inspect
import json
import os
import sys

os.environ["UE5_BRIDGE_TEST_MODE"] = "1"

_SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPTS_DIR = os.path.join(_SKILL_ROOT, "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import pytest  # noqa: E402
import ue5_bridge as B  # noqa: E402
import task_journal as tj  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

_TOKEN = "task-undo-test-token"
client = TestClient(B.app)


class FakeBridge:
    """最小事务桥替身：记录调用顺序，返回可配置结果。"""

    def __init__(self, begin_result=True):
        self.calls = []
        self.begin_result = begin_result
        self.status_json = json.dumps({
            "ok": True, "active": False, "depth": 0, "undo_count": 3,
            "agent_undoable": 2, "next_title": "[agent] connect_pins BP_X",
            "next_is_agent": True,
        })
        self.undo_json = json.dumps({
            "ok": True, "undone": 1, "titles": ["[agent] connect_pins BP_X"],
            "stopped_reason": "", "undo_count_before": 3, "undo_count_after": 2,
        })

    def begin_agent_transaction(self, title):
        self.calls.append(("begin", title))
        return self.begin_result

    def end_agent_transaction(self):
        self.calls.append(("end",))
        return True

    def cancel_agent_transaction(self):
        self.calls.append(("cancel",))
        return True

    def get_agent_undo_status(self):
        self.calls.append(("status",))
        return self.status_json

    def undo_agent_transactions(self, n):
        self.calls.append(("undo", n))
        return self.undo_json


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setattr(B, "_BRIDGE_TOKEN", _TOKEN)
    monkeypatch.setattr(B, "_LOOP_STREAK", {})
    # 离线测试没有 GT worker：置 stopped → _gt_run_handler 内联执行（不等队列）
    monkeypatch.setattr(B, "_GT_WORKER_STOPPED", True)
    B._UNDO_STATUS_CACHE["ts"] = 0.0
    B._UNDO_STATUS_CACHE["data"] = None
    tj.reset_for_tests()
    yield
    tj.reset_for_tests()


@pytest.fixture
def fake_bridge(monkeypatch):
    fb = FakeBridge()
    monkeypatch.setattr(B, "bridge", fb)
    monkeypatch.setattr(B, "BRIDGE_AVAILABLE", True)
    monkeypatch.setattr(B, "_TEST_MODE", False)   # _agent_tx_available 需要
    monkeypatch.setattr(B, "_AGENT_TX_ENABLED", True)
    return fb


# ══════════════════════════════════════════════════════════
#  ① 写命令包装
# ══════════════════════════════════════════════════════════

def test_transactional_command_wrapped(fake_bridge):
    out = B._dispatch_with_tx(
        "connect_pins", lambda **kw: {"ok": True}, bp_path="/Game/BP_X")
    assert out == {"ok": True}
    assert [c[0] for c in fake_bridge.calls] == ["begin", "end"]
    assert fake_bridge.calls[0][1] == "connect_pins BP_X"


def test_readonly_command_not_wrapped(fake_bridge):
    B._dispatch_with_tx("health", lambda **kw: {"ok": True})
    assert fake_bridge.calls == []


def test_begin_refused_runs_without_end(fake_bridge):
    """用户事务在场 → Begin 返回 False：照常执行，绝不 End 别人的事务。"""
    fake_bridge.begin_result = False
    out = B._dispatch_with_tx(
        "set_pin_default_value", lambda **kw: {"ok": True}, bp_path="/Game/BP_X")
    assert out == {"ok": True}
    assert [c[0] for c in fake_bridge.calls] == ["begin"]


def test_exception_still_ends_transaction(fake_bridge):
    def boom(**kw):
        raise RuntimeError("engine said no")

    with pytest.raises(RuntimeError):
        B._dispatch_with_tx("add_variable", boom, bp_path="/Game/BP_X")
    assert [c[0] for c in fake_bridge.calls] == ["begin", "end"]


def test_nested_writes_are_balanced(fake_bridge):
    def inner(**kw):
        return B._dispatch_with_tx("add_component", lambda **kw2: 1,
                                   bp_path="/Game/BP_X")

    B._dispatch_with_tx("add_variable", inner, bp_path="/Game/BP_X")
    kinds = [c[0] for c in fake_bridge.calls]
    assert kinds == ["begin", "begin", "end", "end"]


def test_old_dll_degrades_to_passthrough(monkeypatch):
    class OldBridge:
        pass

    monkeypatch.setattr(B, "bridge", OldBridge())
    monkeypatch.setattr(B, "BRIDGE_AVAILABLE", True)
    monkeypatch.setattr(B, "_TEST_MODE", False)
    out = B._dispatch_with_tx("connect_pins", lambda **kw: {"ok": 1})
    assert out == {"ok": 1}


def test_env_switch_disables_wrapping(fake_bridge, monkeypatch):
    monkeypatch.setattr(B, "_AGENT_TX_ENABLED", False)
    B._dispatch_with_tx("connect_pins", lambda **kw: {"ok": 1})
    assert fake_bridge.calls == []


# ══════════════════════════════════════════════════════════
#  ② MCP 直连写路径别名
# ══════════════════════════════════════════════════════════

def test_mcp_alias_targets_are_transactional():
    for tool, cmd in B._MCP_TX_ALIAS.items():
        assert cmd in B._TRANSACTIONAL_COMMANDS, (tool, cmd)


def test_mcp_write_tools_covered():
    for tool in ("ue5_connect_pins", "ue5_disconnect_pin", "ue5_delete_node"):
        assert tool in B._MCP_TX_ALIAS


def test_apply_graph_not_double_wrapped():
    """apply_graph 自己在写入段开事务（编译必须在事务外）→ 不进通用包装集合。"""
    assert "apply_graph" not in B._TRANSACTIONAL_COMMANDS


# ══════════════════════════════════════════════════════════
#  ③ 撤销命令
# ══════════════════════════════════════════════════════════

def test_undo_status_parses(fake_bridge):
    d = B._cmd_agent_undo_status()
    assert d["ok"] is True and d["agent_undoable"] == 2
    assert d["next_is_agent"] is True
    assert ("status",) in fake_bridge.calls


def test_undo_status_requires_v45(monkeypatch):
    monkeypatch.setattr(B, "bridge", object())
    monkeypatch.setattr(B, "_TEST_MODE", False)
    with pytest.raises(B.BridgeError) as ei:
        B._cmd_agent_undo_status()
    assert ei.value.category == "bridge_unavailable"


def test_undo_returns_titles(fake_bridge):
    d = B._cmd_agent_undo(3)
    assert d["undone"] == 1
    assert d["titles"] == ["[agent] connect_pins BP_X"]
    assert d["success"] is True
    assert ("undo", 3) in fake_bridge.calls


def test_undo_zero_is_fail_loud(fake_bridge):
    fake_bridge.undo_json = json.dumps({
        "ok": True, "undone": 0,
        "stopped_reason": "下一个撤销步不是 agent 事务（\"Move Actor\"）",
    })
    d = B._cmd_agent_undo(1)
    assert d["success"] is False and d["_fail_loud"] is True
    assert "Move Actor" in d["error"]


@pytest.mark.parametrize("bad", [0, -2])
def test_undo_rejects_nonpositive(fake_bridge, bad):
    with pytest.raises(B.BridgeError) as ei:
        B._cmd_agent_undo(bad)
    assert ei.value.category == "param_type"
    assert fake_bridge.calls == []


def test_undo_rejects_absurd_count(fake_bridge):
    with pytest.raises(B.BridgeError):
        B._cmd_agent_undo(501)


def test_undo_engine_error_is_bridge_error(fake_bridge):
    fake_bridge.undo_json = json.dumps({"ok": False, "error": "事务正在进行中"})
    with pytest.raises(B.BridgeError) as ei:
        B._cmd_agent_undo(1)
    assert "事务正在进行中" in str(ei.value)


def test_whitelist_has_undo_commands():
    assert "agent_undo" in B._COMMAND_WHITELIST
    assert "agent_undo_status" in B._COMMAND_WHITELIST


# ══════════════════════════════════════════════════════════
#  ④ 任务账本回滚记录
# ══════════════════════════════════════════════════════════

def test_note_rollback_records_active_task():
    tj.task_begin({"goal": "g"})
    tj.note_rollback(2, ["[agent] a", "[agent] b"], "")
    active = tj.task_list()["active"]
    assert len(active["rollbacks"]) == 1
    assert active["rollbacks"][0]["undone"] == 2
    assert any("已回滚 2 步" in n["note"] for n in active["notes"])
    tj.task_end({"status": "success"})


def test_note_rollback_without_task_is_noop():
    tj.note_rollback(1, ["[agent] x"], "")
    assert tj.task_list()["active"] is None


# ══════════════════════════════════════════════════════════
#  ⑤ /ui/api/task 撤销状态 + 缓存
# ══════════════════════════════════════════════════════════

def test_ui_task_includes_undo(fake_bridge):
    r = client.get("/ui/api/task", headers={"X-Skill-Token": _TOKEN})
    assert r.status_code == 200
    body = r.json()
    assert body["undo"]["ok"] is True
    assert body["undo"]["agent_undoable"] == 2


def test_ui_task_undo_is_cached(fake_bridge):
    client.get("/ui/api/task", headers={"X-Skill-Token": _TOKEN})
    client.get("/ui/api/task", headers={"X-Skill-Token": _TOKEN})
    assert [c[0] for c in fake_bridge.calls].count("status") == 1


def test_ui_task_undo_failure_is_soft(monkeypatch):
    """桥不可用 → undo 字段 ok:false，任务数据照常返回（观测面 fail-open）。"""
    monkeypatch.setattr(B, "bridge", object())
    monkeypatch.setattr(B, "BRIDGE_AVAILABLE", True)
    monkeypatch.setattr(B, "_TEST_MODE", False)
    r = client.get("/ui/api/task", headers={"X-Skill-Token": _TOKEN})
    assert r.status_code == 200
    assert r.json()["undo"]["ok"] is False


# ══════════════════════════════════════════════════════════
#  ⑥ 源码级回归：分派入口不许绕过事务包装
# ══════════════════════════════════════════════════════════

def test_command_endpoint_uses_tx_dispatch():
    src = inspect.getsource(B.execute_command)
    assert "_dispatch_with_tx" in src


def test_editor_batch_uses_tx_dispatch():
    src = inspect.getsource(B._cmd_editor_batch)
    assert "_dispatch_with_tx" in src


def test_mcp_command_impl_uses_tx_dispatch():
    src = inspect.getsource(B._mcp_impl_command)
    assert "_dispatch_with_tx" in src


def test_mcp_tools_call_uses_tx_alias():
    src = inspect.getsource(B._mcp_handle_one)
    assert "_MCP_TX_ALIAS" in src
