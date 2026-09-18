# -*- coding: utf-8 -*-
"""
T-20260909-SKILL-GEN-C-INIT-CONVERGE-REMAIN · 阶段④：single_agent fixture 实测
==============================================================================
发布门禁：覆盖 init.py 的 single_agent 确定性指派路径。

被测语义（薄执行器 v1.1 · 人工确认门）：
  - 探测到 ≤1 个 enabled agent  → detection.mode = "single_agent"
  - assign_roles(mode="single_agent") → 宿主全角色 all_in_one，
    role_source = deterministic（无选择空间，确定性指派）
  - _decide_status(mode="single_agent") → status = confirmed（生效稿）

测试不依赖真实 agent API（monkeypatch api_list_agents → None），
走 init.py 的 agent_json_scan 降级分支（COPAW_WORKSPACE 指向临时目录），
与 run() 的 phase1→assign_roles→_decide_status 同款调用链一致。
"""
import json
import os
import sys

import pytest

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_DIR = os.path.join(os.path.dirname(_THIS_DIR), "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import init  # noqa: E402


# ═══════════════════════════════════════════════════════════
# fixtures
# ═══════════════════════════════════════════════════════════

@pytest.fixture
def single_agent_workspace(tmp_path, monkeypatch):
    """
    构造单 agent 环境（真实文件系统）：

      {tmp}/workspaces/ws1/agent.json   ← 唯一 enabled agent
      COPAW_WORKSPACE={tmp}/workspaces
      api_list_agents → None（强制 L1 失败 → agent_json_scan 分支）

    返回 ws1 的 agent.json dict + ws 路径。
    """
    ws_root = tmp_path / "workspaces"
    ws1 = ws_root / "ws1"
    ws1.mkdir(parents=True, exist_ok=True)

    aj = {
        "id": "host1",
        "name": "HostOne",
        "description": "single host only",
        "workspace_dir": str(ws1),
        "enabled": True,
        "startup_status": "running",
        "backend": "test",
        "active_model": {"provider_id": "test", "model": "m"},
        "tools": {"builtin_tools": {
            "execute_shell_command": {"enabled": True},
            "read_file": {"enabled": True},
            "write_file": {"enabled": True},
            "chat_with_agent": {"enabled": True},
        }},
        "system_prompt_files": [],
    }
    (ws1 / "agent.json").write_text(
        json.dumps(aj, ensure_ascii=False), encoding="utf-8")

    monkeypatch.setenv("COPAW_WORKSPACE", str(ws_root))
    monkeypatch.setattr(init, "api_list_agents", lambda *a, **k: None)
    return {"agent_json": aj, "ws1": str(ws1)}


# ═══════════════════════════════════════════════════════════
# 测试：detect_agents 单 agent 判定
# ═══════════════════════════════════════════════════════════

def test_detect_agents_single_agent_mode(single_agent_workspace):
    """探测：单 enabled agent → mode=single_agent（走 agent_json_scan）。"""
    meta, records = init.detect_agents()

    assert meta["mode"] == "single_agent", \
        f"期望 mode=single_agent，实际 {meta['mode']}"
    assert meta["agents_found"] == 1, meta["agents_found"]
    assert meta["agents_enabled"] == 1, meta["agents_enabled"]
    assert "agent_json_scan" in meta["probe_sources"], meta["probe_sources"]
    assert len(records) == 1, len(records)
    assert records[0]["agent_id"] == "host1"


# ═══════════════════════════════════════════════════════════
# 测试：assign_roles 确定性指派 all_in_one
# ═══════════════════════════════════════════════════════════

def test_assign_roles_single_agent_all_in_one(single_agent_workspace):
    """single_agent 模式 → 宿主全角色 all_in_one + deterministic。"""
    meta, records = init.detect_agents()
    role_map, out_records = init.assign_roles(records, {}, mode=meta["mode"])

    # 5 个关键角色全部指回宿主
    expect_roles = ("orchestrator", "ue_executor", "screenshot_ocr",
                    "report_writer", "archivist")
    for role in expect_roles:
        assert role_map.get(role) == "host1", \
            f"期望 {role}=host1，实际 {role_map.get(role)}"

    host = out_records[0]
    assert host["role"] == "all_in_one", host["role"]
    assert host["role_source"] == init.SRC_DETERMINISTIC, host["role_source"]
    assert host["enabled"] is True


# ═══════════════════════════════════════════════════════════
# 测试：_decide_status 单 agent → confirmed（生效稿）
# ═══════════════════════════════════════════════════════════

def test_decide_status_single_agent_confirmed(single_agent_workspace):
    """single_agent 确定性指派 → status=confirmed，无人工通道也可生效。"""
    meta, records = init.detect_agents()
    role_map, out_records = init.assign_roles(records, {}, mode=meta["mode"])

    # 与 run() 相同：从 records 汇总 role_sources
    by_id = {r["agent_id"]: r for r in out_records}
    role_sources = {
        role: by_id.get(aid, {}).get("role_source", init.SRC_AUTO)
        for role, aid in role_map.items()
    }
    status, reason = init._decide_status(
        meta["mode"], role_sources, manual_channels=False)

    assert status == init.STATUS_CONFIRMED, \
        f"期望 confirmed，实际 {status}: {reason}"
    assert "single_agent" in reason
    # deterministic 来源，无 auto 残留
    assert set(role_sources.values()) == {init.SRC_DETERMINISTIC}, role_sources


# ═══════════════════════════════════════════════════════════
# 测试：端到端链（detect → assign → decide，run() phase1/5 同款）
# ═══════════════════════════════════════════════════════════

def test_single_agent_full_chain(single_agent_workspace):
    """确定性指派路径端到端：host1 五角色 + confirmed + 无 unspecified。"""
    meta, records = init.detect_agents()
    role_map, out_records = init.assign_roles(records, {}, mode=meta["mode"])

    assert meta["mode"] == "single_agent"
    assert set(role_map.values()) == {"host1"}, role_map
    # all_in_one 宿主应有完整边界与可执行命令
    assert out_records[0]["boundaries"], "all_in_one 边界不应为空"
    assert out_records[0]["exec_commands"], "all_in_one exec_commands 不应为空"
    assert out_records[0]["default_behavior"] == \
        init.ROLE_DEFAULT_BEHAVIOR["all_in_one"]
