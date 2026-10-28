# -*- coding: utf-8 -*-
"""任务日志（task_journal）+ 面板端点测试（T-20261024-UI-V2）。

覆盖：
  ① task_journal 单元：schema fail-loud / 单任务纪律 / 步骤定位 / 复盘统计
  ② 白名单命令 + MCP 工具：ue5_task_begin/step/end/list 全链路（TestClient）
  ③ /ui/api/task 端点（含 503 降级）
  ④ /ui/api/stats 聚合 + /ui/api/screenshots 胶片条 + screenshot?name= 约束

测试方式：真实 ue5_bridge + TestClient（同 test_dashboard.py 模式），
任务日志落盘重定向到临时目录（不污染 %APPDATA%）。
"""

import json
import os
import sys
import tempfile

# ── 必须在 import ue5_bridge 之前设置环境变量 ──
os.environ["UE5_BRIDGE_TEST_MODE"] = "1"

_SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPTS_DIR = os.path.join(_SKILL_ROOT, "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import pytest  # noqa: E402
import task_journal as tj  # noqa: E402
import ue5_bridge  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

_TJ_TOKEN = "task-journal-test-token"
client = TestClient(ue5_bridge.app)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """每个用例：内存态清空 + 落盘重定向 tmp + 固定 token。"""
    tj.reset_for_tests()
    monkeypatch.setattr(tj, "_dir", str(tmp_path))
    monkeypatch.setattr(ue5_bridge, "_BRIDGE_TOKEN", _TJ_TOKEN)
    yield
    tj.reset_for_tests()


def _h():
    return {"X-Skill-Token": _TJ_TOKEN}


def _cmd(name, **params):
    r = client.post("/command", json={"command": name, "params": params},
                    headers=_h())
    assert r.status_code == 200, r.text
    return r.json()


# ══════════════════════════════════════════════════════════
#  ① task_journal 单元
# ══════════════════════════════════════════════════════════

class TestJournalUnit:

    def test_begin_requires_goal(self):
        with pytest.raises(ValueError, match="goal"):
            tj.task_begin({})

    def test_begin_happy_path_with_steps(self):
        out = tj.task_begin({
            "goal": "给 BP_Door 加自动开门",
            "asset": "/Game/BP/BP_Door",
            "understanding": "碰撞触发+Timeline 旋转",
            "approach": "Overlap 事件驱动",
            "steps": ["读图", {"name": "写图", "hint": "apply_graph"}],
        })
        t = out["task"]
        assert t["status"] == "running"
        assert [s["id"] for s in t["steps"]] == [1, 2]
        assert t["steps"][1]["hint"] == "apply_graph"

    def test_single_active_task_discipline(self):
        tj.task_begin({"goal": "A"})
        with pytest.raises(ValueError, match="已有任务在跑"):
            tj.task_begin({"goal": "B"})

    def test_step_requires_plan_member(self):
        tj.task_begin({"goal": "A", "steps": ["读", "写"]})
        with pytest.raises(ValueError, match="不在计划里"):
            tj.task_step({"step_id": 99})
        tj.task_step({"step_id": 1, "status": "done", "note": "读完了"})
        lst = tj.task_list()
        assert lst["active"]["steps"][0]["status"] == "done"

    def test_step_without_step_id_needs_note(self):
        tj.task_begin({"goal": "A"})
        with pytest.raises(ValueError, match="step_id"):
            tj.task_step({})
        r = tj.task_step({"note": "阶段注记"})
        assert r["noted"] is True

    def test_step_status_enum(self):
        tj.task_begin({"goal": "A", "steps": ["x"]})
        with pytest.raises(ValueError, match="status"):
            tj.task_step({"step_id": 1, "status": "magic"})

    def test_end_computes_summary_fields(self):
        tj.task_begin({"goal": "A", "steps": ["x", "y"]})
        tj.task_step({"step_id": 1, "status": "done"})
        tj.task_step({"step_id": 2, "status": "failed", "note": "引脚名编造"})
        out = tj.task_end({"status": "success", "summary": "实现 X",
                           "lessons": "先 search 再写"})
        t = out["task"]
        assert t["result_status"] == "success"
        assert t["failed_steps"] == ["y"]
        assert t["duration_s"] is not None
        # 收尾后无 active，可开新任务
        assert tj.task_list()["active"] is None
        tj.task_begin({"goal": "B"})  # 不抛 = 串行纪律解除

    def test_end_status_enum(self):
        tj.task_begin({"goal": "A"})
        with pytest.raises(ValueError, match="status"):
            tj.task_end({"status": "ok"})

    def test_error_nudge(self):
        tj.task_begin({"goal": "A"})
        tj.task_error_nudge({"note": "add_node 对 K2Node_SwitchInteger 无效"})
        lst = tj.task_list()
        assert lst["active"]["notes"][-1]["error"] is True

    def test_persist_fail_open(self, monkeypatch):
        """落盘目录不可写 → 不抛（fail-open），命令照常成功。"""
        tj.task_begin({"goal": "A"})
        monkeypatch.setattr(tj, "_dir", "Z:\\definitely\\not\\writable\\路径")
        r = tj.task_step({"note": "落盘坏了也要能记"})
        assert r["noted"] is True


class TestEvidenceAndAutoProgress:
    """T-20261025-UX：自动进度注记 + success 声明需证据（防 agent 谎报）。"""

    def test_auto_progress_lands_in_running_step(self):
        tj.task_begin({"goal": "A", "steps": ["写图", "验证"]})
        tj.task_step({"step_id": 1, "status": "running"})
        tj.note_tool_use("ue5_apply_blueprint_graph", ok=True, latency_ms=140)
        t = tj.task_list()["active"]
        log = t["steps"][0]["log"]
        auto = [x for x in log if x.get("auto")]
        assert auto and "整图写入" in auto[-1]["note"]
        assert t["evidence_counts"]["apply"] == 1

    def test_non_key_tool_noted_but_no_evidence(self):
        tj.task_begin({"goal": "A", "steps": ["x"]})
        tj.task_step({"step_id": 1, "status": "running"})
        tj.note_tool_use("ue5_task_list", ok=True)
        assert tj.task_list()["active"].get("evidence_counts") in (None, {})

    def test_success_without_evidence_is_flagged(self):
        tj.task_begin({"goal": "A", "steps": ["x"]})
        out = tj.task_end({"status": "success", "summary": "我说成功了"})
        assert out["task"]["unverified"] is True
        assert any("未检测到验证证据" in w for w in out["warnings"])

    def test_success_with_compile_evidence_is_clean(self):
        tj.task_begin({"goal": "A", "steps": ["x"]})
        tj.note_tool_use("ue5_compile_and_diagnose", ok=True)
        out = tj.task_end({"status": "success"})
        assert not out["task"].get("unverified")
        assert "warnings" not in out

    def test_success_with_only_screenshot_warns(self):
        tj.task_begin({"goal": "A"})
        tj.note_tool_use("ue5_screenshot", ok=True)
        out = tj.task_end({"status": "success"})
        assert not out["task"].get("unverified")
        assert any("缺少编译/写入证据" in w for w in out["warnings"])

    def test_end_auto_closes_hanging_steps(self):
        """收尾封口（T-20261025-UX）：结束任务不该留 running/pending 悬挂步骤。"""
        tj.task_begin({"goal": "A", "steps": ["一", "二", "三"]})
        tj.task_step({"step_id": 1, "status": "running"})
        out = tj.task_end({"status": "success"})
        steps = out["task"]["steps"]
        assert all(x["status"] == "done" for x in steps), steps
        assert out["task"]["auto_closed_steps"] == 3
        assert all(x.get("auto_closed") for x in steps)

    def test_command_routed_by_hint(self):
        """命令按计划步骤 hint 归位（回归：曾丢失导致命令全落任务级）。"""
        tj.task_begin({"goal": "A", "steps": [
            {"name": "查", "hint": "search_blueprint_nodes"},
            {"name": "编译", "hint": "compile_and_diagnose"}]})
        tj.note_command("ue5_search_blueprint_nodes", {"bp_path": "/Game/A"}, {})
        tj.note_command("ue5_compile_and_diagnose", {"bp_path": "/Game/A"},
                        {"compiled": True})
        active = tj.task_list()["active"]
        s0, s1 = active["steps"]
        assert len(s0.get("commands") or []) == 1, s0.get("commands")
        assert s0["commands"][0]["tool"] == "ue5_search_blueprint_nodes"
        assert len(s1.get("commands") or []) == 1, s1.get("commands")
        assert s1["commands"][0]["tool"] == "ue5_compile_and_diagnose"
        assert not (active.get("commands") or []), "不应落到任务级"

    def test_deliverables_explicit(self):
        """交付物：agent 显式提供 → 原样入库。"""
        tj.task_begin({"goal": "A"})
        out = tj.task_end({
            "status": "success",
            "deliverables": [
                {"kind": "蓝图资产", "path": "/Game/Blueprints/BP_X",
                 "action": "created", "note": "新建并在 BeginPlay 打印"},
                {"kind": "文件", "path": "docs/report.md", "action": "modified"}],
            "how_to_verify": "1. 打开 BP_X\n2. 点 Compile 确认无错"})
        t = out["task"]
        assert len(t["deliverables"]) == 2
        assert not t.get("deliverables_inferred")
        assert "打开 BP_X" in t["how_to_verify"]

    def test_deliverables_inferred_from_commands(self):
        """交付物：agent 没填 → 从命令流水自动推断（资产去重 + 截图单列）。"""
        tj.task_begin({"goal": "A"})
        tj.note_command("ue5_apply_blueprint_graph",
                        {"bp_path": "/Game/Blueprints/BP_Y"},
                        {"verified": True, "compiled": True})
        tj.note_command("ue5_screenshot", {"source": "editor_viewport"},
                        {"saved_path": "/x/Saved/Screenshots/ue5-automation/s.png",
                         "width": 100, "height": 80})
        out = tj.task_end({"status": "success"})
        t = out["task"]
        assert t.get("deliverables_inferred") is True
        kinds = {(d["kind"], d["action"]) for d in t["deliverables"]}
        assert ("蓝图资产", "modified") in kinds
        assert ("截图", "evidence") in kinds
        asset = [d for d in t["deliverables"] if d["kind"] == "蓝图资产"][0]
        assert "读回验证通过" in asset["note"] and "编译通过" in asset["note"]

    def test_deliverables_dedup(self):
        """同一资产多次写入 → 交付物只出现一次。"""
        tj.task_begin({"goal": "A"})
        for _ in range(3):
            tj.note_command("ue5_apply_blueprint_graph",
                            {"bp_path": "/Game/BP_Z"}, {"verified": True})
        out = tj.task_end({"status": "success"})
        assets = [d for d in out["task"]["deliverables"] if d["kind"] == "蓝图资产"]
        assert len(assets) == 1

    def test_failed_status_needs_no_evidence(self):
        tj.task_begin({"goal": "A"})
        out = tj.task_end({"status": "failed"})
        assert not out["task"].get("unverified")
        assert "warnings" not in out


# ══════════════════════════════════════════════════════════
#  ② 白名单命令 + MCP 工具全链路
# ══════════════════════════════════════════════════════════

class TestCommandAndMcpSurface:

    def test_whitelist_roundtrip(self):
        r = _cmd("task_begin", goal="任务 X", steps=["一步"])
        assert r["success"] is True
        _cmd("task_step", step_id=1, status="running", note="开始定位")
        r = _cmd("task_step", step_id=1, status="done", note="定位完成")
        assert r["result"]["data"]["step_status"] == "done"
        # 过程日志：同一步骤的多次上报逐条追加（执行脉络的中间过程）
        lst = _cmd("task_list")["result"]["data"]
        log = lst["active"]["steps"][0]["log"]
        assert len(log) == 2 and log[0]["note"] == "开始定位"
        r = _cmd("task_end", status="success", summary="完了", lessons="学到了")
        assert r["result"]["data"]["task"]["result_status"] == "success"

    def test_whitelist_fail_loud_on_bad_schema(self):
        r = _cmd("task_begin")  # 缺 goal
        assert r["success"] is False and "goal" in r["error"]

    def test_mcp_tools_listed(self):
        payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        tools = client.post("/mcp", json=payload, headers=_h()).json()["result"]["tools"]
        names = {t["name"] for t in tools}
        # T-20261027-SLIM：默认只暴露核心工具；task_list 属扩展（走 execute_command）
        assert {"ue5_task_begin", "ue5_task_step", "ue5_task_end"} <= names

    def test_mcp_call_roundtrip(self):
        payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "ue5_task_begin",
            "arguments": {"goal": "MCP 任务", "steps": ["a", "b"]}}}
        body = client.post("/mcp", json=payload, headers=_h()).json()["result"]
        assert not body.get("isError")
        meta = json.loads(body["content"][0]["text"])
        assert meta["data"]["task"]["goal"] == "MCP 任务"

    def test_mcp_step_unknown_id_is_error(self):
        client.post("/command", json={"command": "task_begin",
                                      "params": {"goal": "A", "steps": ["x"]}},
                    headers=_h())
        payload = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "ue5_task_step", "arguments": {"step_id": 7}}}
        body = client.post("/mcp", json=payload, headers=_h()).json()["result"]
        assert body.get("isError") is True
        assert "不在计划里" in body["content"][0]["text"]


# ══════════════════════════════════════════════════════════
#  ③ /ui/api/task 端点
# ══════════════════════════════════════════════════════════

class TestJournalIsolation:

    def test_test_mode_does_not_write_production(self):
        """v6.0.3：测试期间账本目录绝不指向生产目录（autouse fixture 已注入 tmp）。"""
        prod = os.path.join(os.environ.get("APPDATA", ""), "ue5_bridge", "tasks")
        assert os.path.abspath(tj._dir) != os.path.abspath(prod)


class TestTaskIdUniqueness:

    def test_begin_skips_ids_already_in_history(self):
        """旧版本同日重启会产生重复 ID；新任务必须顺延，不能再撞。"""
        tj.reset_for_tests()
        tid = "%s-01" % tj._day_stamp()
        tj._history.append({"task_id": tid, "goal": "legacy"})
        tj._seq = 0
        task = tj.task_begin({"goal": "new"})["task"]
        assert task["task_id"] != tid
        tj.reset_for_tests()


class TestUiTaskEndpoint:

    def test_empty_state(self):
        r = client.get("/ui/api/task", headers=_h())
        assert r.status_code == 200
        body = r.json()
        assert body["active"] is None
        assert body["history"] == []
        # T-20261027-TASK-UNDO：撤销状态是附加字段（离线无事务桥 → ok:false）
        assert "undo" in body and body["undo"]["ok"] is False

    def test_active_and_history(self):
        _cmd("task_begin", goal="T1", steps=["s1"])
        _cmd("task_end", status="success")
        _cmd("task_begin", goal="T2")
        r = client.get("/ui/api/task", headers=_h()).json()
        assert r["active"]["goal"] == "T2"
        assert [t["goal"] for t in r["history"]] == ["T1"]

    def test_auth_required(self):
        assert client.get("/ui/api/task").status_code == 401


# ══════════════════════════════════════════════════════════
#  ④ /ui/api/stats + 截图清单/单图约束
# ══════════════════════════════════════════════════════════

class TestUiStatsAndShots:

    def test_stats_shape_after_requests(self):
        client.get("/health", headers=_h())
        r = client.get("/ui/api/stats", headers=_h())
        assert r.status_code == 200
        d = r.json()
        assert d["total_calls"] >= 1
        assert isinstance(d["by_tool"], list) and d["by_tool"]
        row = d["by_tool"][0]
        assert {"key", "calls", "errors", "avg_ms"} <= set(row)

    def test_stats_empty_skeleton(self, monkeypatch):
        import ui_dashboard as ud
        monkeypatch.setattr(ud, "_events", [])
        d = client.get("/ui/api/stats", headers=_h()).json()
        assert d["total_calls"] == 0 and d["by_tool"] == []

    def test_screenshots_list_and_named_fetch(self, tmp_path, monkeypatch):
        import ui_dashboard as ud
        shots = tmp_path / "shots"
        shots.mkdir()
        (shots / "a.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
        (shots / "b.png").write_bytes(b"\x89PNG\r\n\x1a\nfake2")
        monkeypatch.setattr(ud, "_INJECTED_SCREENSHOTS_DIR", str(shots))
        d = client.get("/ui/api/screenshots", headers=_h()).json()
        assert {x["name"] for x in d["items"]} == {"a.png", "b.png"}  # mtime 同秒，不锁序
        # 按名取图 OK
        r = client.get("/ui/api/screenshot",
                       params={"name": "a.png"}, headers=_h())
        assert r.status_code == 200 and r.content.startswith(b"\x89PNG")
        # 路径逃逸拒绝（basename 剥离后找不到 → 404，绝不出目录）
        r = client.get("/ui/api/screenshot",
                       params={"name": "..%2F..%2Fsecret.png"}, headers=_h())
        assert r.status_code == 404
