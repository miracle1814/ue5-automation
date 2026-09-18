# -*- coding: utf-8 -*-
"""
test_v3_fusion.py — v3.0 融合升级回归（T-20260918-V3-MCP-FUSION）
=================================================================
离线覆盖：
  · MCP 协议层：initialize 握手 / tools/list / tools/call / 通知 202 /
    鉴权（X-Skill-Token 与 Bearer）/ 未知方法与未知工具
  · 新能力命令族：editor_batch（fail-loud 中止语义）/ logs_read（尾部+游标）
  · 白名单扩展：50 → 59 条 + 新命令可路由
  · REST 别名：/logs /editor/batch /pie/state（负路径 fail-loud）
"""

import io
import json
import os
import sys
import tempfile

os.environ["UE5_BRIDGE_TEST_MODE"] = "1"
os.environ["UE5_BRIDGE_TOKEN"] = "integration-test-token"  # 全套件统一约定

_SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPTS_DIR = os.path.join(_SKILL_ROOT, "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import ue5_bridge  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(ue5_bridge.app)
# 全量套件中 ue5_bridge 可能已被先序测试模块导入（其 UE5_BRIDGE_TOKEN 生效）
# → 从模块读**实际** token，免疫导入顺序；Bearer 测试用同一值。
TOKEN = {"X-Skill-Token": ue5_bridge._BRIDGE_TOKEN}
BEARER = {"Authorization": "Bearer " + ue5_bridge._BRIDGE_TOKEN}

V3_NEW_COMMANDS = [
    "pie_start", "pie_stop", "pie_state",
    "find_actors", "spawn_actor", "set_actor_transform", "get_actor_properties",
    "logs_read", "editor_batch",
]


def _rpc(method, params=None, _id=1, headers=None):
    body = {"jsonrpc": "2.0", "id": _id, "method": method}
    if params is not None:
        body["params"] = params
    return client.post("/mcp", json=body, headers=headers or TOKEN)


def _call_tool(name, arguments=None):
    return _rpc("tools/call", {"name": name, "arguments": arguments or {}})


def _tool_text(resp):
    """从 tools/call 响应里取 text 并解析 JSON。"""
    data = resp.json()
    return json.loads(data["result"]["content"][0]["text"])


# ═══════════════════════════════════════════════════════════
# 白名单扩展
# ═══════════════════════════════════════════════════════════

class TestWhitelistExpansion:
    def test_new_total_is_72(self):
        assert len(ue5_bridge._COMMAND_WHITELIST) == 72

    def test_new_commands_present(self):
        for c in V3_NEW_COMMANDS:
            assert c in ue5_bridge._COMMAND_WHITELIST, c

    def test_docstring_count_matches(self):
        doc = ue5_bridge.execute_command.__doc__ or ""
        assert ("%d 条" % len(ue5_bridge._COMMAND_WHITELIST)) in doc


# ═══════════════════════════════════════════════════════════
# MCP 协议层
# ═══════════════════════════════════════════════════════════

class TestMcpProtocol:
    def test_initialize_echoes_supported_version(self):
        r = _rpc("initialize", {"protocolVersion": "2025-03-26",
                                "capabilities": {}, "clientInfo": {"name": "t"}})
        assert r.status_code == 200
        body = r.json()
        assert body["result"]["protocolVersion"] == "2025-03-26"
        assert "tools" in body["result"]["capabilities"]
        assert body["result"]["serverInfo"]["name"] == "ue5-automation-bridge"

    def test_initialize_unknown_version_falls_back(self):
        r = _rpc("initialize", {"protocolVersion": "1999-01-01"})
        assert r.json()["result"]["protocolVersion"] in ue5_bridge._MCP_PROTOCOLS

    def test_session_header_present(self):
        r = _rpc("initialize", {"protocolVersion": "2025-03-26"})
        assert r.headers.get("mcp-session-id")

    def test_notification_returns_202(self):
        r = client.post("/mcp", json={"jsonrpc": "2.0",
                                      "method": "notifications/initialized"},
                        headers=TOKEN)
        assert r.status_code == 202

    def test_tools_list_has_key_tools(self):
        r = _rpc("tools/list", {})
        names = [t["name"] for t in r.json()["result"]["tools"]]
        for must in ("ue5_health", "ue5_build_blueprint", "ue5_connect_pins",
                     "ue5_pie_state", "ue5_logs", "ue5_batch", "ue5_command"):
            assert must in names, must
        # 每个工具都有 inputSchema
        for t in r.json()["result"]["tools"]:
            assert t.get("inputSchema", {}).get("type") == "object"

    def test_tools_list_command_description_contains_full_whitelist(self):
        r = _rpc("tools/list", {})
        cmd_tool = [t for t in r.json()["result"]["tools"]
                    if t["name"] == "ue5_command"][0]
        for c in V3_NEW_COMMANDS:
            assert c in cmd_tool["description"], c

    def test_tools_call_health(self):
        r = _call_tool("ue5_health")
        body = r.json()
        assert body["result"]["isError"] is False
        payload = json.loads(body["result"]["content"][0]["text"])
        assert isinstance(payload, dict) and payload  # fake 环境返回引擎信息字典

    def test_tools_call_unknown_tool(self):
        r = _call_tool("no_such_tool")
        body = r.json()
        assert "error" in body and body["error"]["code"] == -32602

    def test_unknown_method(self):
        r = _rpc("nonsense/method", {})
        assert r.json()["error"]["code"] == -32601

    def test_ping(self):
        assert _rpc("ping", {}).json()["result"] == {}

    def test_resources_and_prompts_empty(self):
        assert _rpc("resources/list", {}).json()["result"]["resources"] == []
        assert _rpc("prompts/list", {}).json()["result"]["prompts"] == []


class TestMcpAuth:
    def test_missing_token_403(self):
        r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                                      "method": "tools/list"})
        assert r.status_code == 403

    def test_wrong_token_403(self):
        r = _rpc("tools/list", {}, headers={"X-Skill-Token": "definitely-wrong-token-000"})
        assert r.status_code == 403

    def test_query_param_token_accepted(self):
        """不支持自定义 header 的 MCP 客户端兜底通道（?token=）"""
        r = client.post("/mcp?token=" + ue5_bridge._BRIDGE_TOKEN,
                        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert r.status_code == 200 and "result" in r.json()

    def test_wrong_query_token_403(self):
        r = client.post("/mcp?token=definitely-wrong",
                        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert r.status_code == 403

    def test_bearer_token_accepted(self):
        r = _rpc("tools/list", {}, headers=BEARER)
        assert r.status_code == 200 and "result" in r.json()


class TestMcpCommandPassthrough:
    def test_command_health_ok(self):
        res = _tool_text(_call_tool("ue5_command", {"command": "health"}))
        assert res["success"] is True

    def test_command_unknown_fail_loud(self):
        r = _call_tool("ue5_command", {"command": "drop_database"})
        body = r.json()
        assert body["result"]["isError"] is True
        txt = body["result"]["content"][0]["text"]
        assert "Unknown command" in txt

    def test_batch_tool_stops_on_failure(self):
        res = _tool_text(_call_tool("ue5_batch", {
            "steps": [
                {"command": "health"},
                {"command": "drop_database"},
                {"command": "health"},
            ]}))
        assert res["success"] is False
        assert res["stopped_early"] is True
        assert res["completed"] == 1
        assert len(res["steps"]) == 2


# ═══════════════════════════════════════════════════════════
# editor_batch（命令层直测）
# ═══════════════════════════════════════════════════════════

class TestEditorBatch:
    def test_success_sequence(self):
        out = ue5_bridge._cmd_editor_batch(
            [{"command": "health"}, {"command": "pie_state"}], True)
        # pie_state 在 fake 环境 fail-loud（无 EditorLevelLibrary）→ 预期失败中止
        assert out["_fail_loud"] is True
        assert out["steps"][0]["success"] is True

    def test_unknown_stops(self):
        out = ue5_bridge._cmd_editor_batch(
            [{"command": "nope"}, {"command": "health"}], True)
        assert out["completed"] == 0 and out["stopped_early"] is True

    def test_continue_on_error(self):
        out = ue5_bridge._cmd_editor_batch(
            [{"command": "nope"}, {"command": "health"}], False)
        assert out["completed"] == 1 and out["stopped_early"] is False
        assert out["success"] is False

    def test_step_cap(self):
        steps = [{"command": "health"}] * 201
        try:
            ue5_bridge._cmd_editor_batch(steps, True)
            raise AssertionError("应当拒绝超过 200 步")
        except ue5_bridge.BridgeError:
            pass


# ═══════════════════════════════════════════════════════════
# logs_read
# ═══════════════════════════════════════════════════════════

class TestLogsRead:
    def _mk_log(self, d, n=10):
        p = os.path.join(d, "dev-project.log")
        lines = ["[2026.09.18-10.00.%02d:000][%03d]LogTemp: line %d"
                 % (i, i, i) for i in range(n)]
        with io.open(p, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return p

    def test_tail_mode(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._mk_log(d)
            out = ue5_bridge._cmd_logs_read(lines=3, log_file=p)
            data = out["data"]
            assert data["tail_mode"] is True
            assert data["count"] == 3
            assert "line 9" in data["lines"][-1]
            assert data["next_cursor"] == data["size"]

    def test_cursor_incremental(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._mk_log(d)
            first = ue5_bridge._cmd_logs_read(lines=100, log_file=p)["data"]
            cur = first["next_cursor"]
            with io.open(p, "a", encoding="utf-8") as f:
                f.write("[2026.09.18-10.01.00:000][100]LogTemp: NEW LINE\n")
            second = ue5_bridge._cmd_logs_read(cursor=cur, log_file=p)["data"]
            assert second["tail_mode"] is False
            assert any("NEW LINE" in l for l in second["lines"])

    def test_category_and_pattern_filter(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._mk_log(d)
            out = ue5_bridge._cmd_logs_read(lines=100, category="LogTemp",
                                            pattern=r"line [45]$", log_file=p)
            assert out["data"]["count"] == 2

    def test_missing_file_fail_loud(self):
        try:
            ue5_bridge._cmd_logs_read(log_file=os.path.join(
                tempfile.gettempdir(), "definitely_missing_ue_log_xyz.log"))
            raise AssertionError("应当 fail-loud")
        except ue5_bridge.BridgeError:
            pass

    def test_bad_pattern_fail_loud(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._mk_log(d)
            try:
                ue5_bridge._cmd_logs_read(pattern="([unclosed", log_file=p)
                raise AssertionError("应当 fail-loud")
            except ue5_bridge.BridgeError:
                pass


# ═══════════════════════════════════════════════════════════
# REST 别名 + PIE 负路径
# ═══════════════════════════════════════════════════════════

class TestRestAliases:
    def test_logs_endpoint(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.log")
            with io.open(p, "w", encoding="utf-8") as f:
                f.write("hello\n")
            r = client.post("/logs", json={"lines": 10, "log_file": p},
                            headers=TOKEN)
            body = r.json()
            assert r.status_code == 200 and body["success"] is True
            assert body["data"]["count"] == 1

    def test_logs_requires_token(self):
        assert client.post("/logs", json={}).status_code == 403

    def test_editor_batch_endpoint(self):
        r = client.post("/editor/batch",
                        json={"steps": [{"command": "health"}]}, headers=TOKEN)
        assert r.json()["success"] is True

    def test_pie_state_reports_not_playing_in_fake_env(self):
        """v3.0 语义：状态查询不报错 —— 无 PIE 世界即 is_playing=False + probe_notes。"""
        r = client.post("/pie/state", headers=TOKEN)
        body = r.json()
        assert r.status_code == 200
        assert body["success"] is True
        assert body["data"]["is_playing"] is False
        assert body["data"]["probe_notes"]

    def test_pie_start_rejects_bad_mode(self):
        r = client.post("/pie/start", json={"mode": "bogus"}, headers=TOKEN)
        body = r.json()
        assert body["success"] is False
        assert "mode" in body.get("error", "")

    def test_pie_set_transform_requires_actor_name(self):
        r = client.post("/pie/set_transform", json={"location": [1, 2, 3]},
                        headers=TOKEN)
        assert r.json()["success"] is False

    def test_find_actors_world_pie_without_pie(self):
        r = client.post("/command", json={"command": "find_actors",
                                          "params": {"world": "pie"}},
                        headers=TOKEN)
        body = r.json()
        assert body["success"] is False  # fake 环境无 PIE 世界 → fail-loud

# ═══════════════════════════════════════════════════════════
# 材质 / UMG 命令族（v3.0）
# ═══════════════════════════════════════════════════════════

MAT_COMMANDS = [
    "material_create", "material_add_expression", "material_connect_expressions",
    "material_connect_property", "material_set_expression_property",
    "material_compile", "material_read", "material_instance_create",
    "material_instance_set_parameter",
]
WIDGET_COMMANDS = ["widget_create", "widget_add_child",
                   "widget_set_property", "widget_read_tree"]


class TestMaterialWidgetRouting:
    def test_material_commands_registered(self):
        for c in MAT_COMMANDS:
            assert c in ue5_bridge._COMMAND_WHITELIST, c

    def test_widget_commands_registered(self):
        for c in WIDGET_COMMANDS:
            assert c in ue5_bridge._COMMAND_WHITELIST, c

    def test_widget_read_tree_fail_loud_without_bridge(self):
        """fake 环境无 BlueprintPythonBridge → 必须可读 fail-loud（不假成功）。"""
        r = client.post("/command", json={
            "command": "widget_read_tree",
            "params": {"widget_path": "/Game/X/WBP_X"}}, headers=TOKEN)
        body = r.json()
        assert body["success"] is False
        err = body.get("error", "")
        assert ("BlueprintPythonBridge" in err) or ("UMG API" in err), err

    def test_material_read_fail_loud_in_fake_env(self):
        r = client.post("/command", json={
            "command": "material_read",
            "params": {"material_path": "/Game/X/M_X"}}, headers=TOKEN)
        body = r.json()
        assert body["success"] is False

    def test_widget_add_child_unknown_class_fail_loud_later(self):
        """参数校验前置：widget_class 为空 → BridgeError（早于任何副作用）。"""
        r = client.post("/command", json={
            "command": "widget_add_child",
            "params": {"widget_path": "/Game/X/WBP_X", "widget_class": "",
                       "widget_name": "W"}}, headers=TOKEN)
        body = r.json()
        assert body["success"] is False

# ═══════════════════════════════════════════════════════════
# v3.0.1：回读值比对助手（_value_matches）
# ═══════════════════════════════════════════════════════════

class TestValueMatches:
    def test_exact(self):
        assert ue5_bridge._value_matches("abc", "abc") is True

    def test_float_tolerance(self):
        assert ue5_bridge._value_matches("0.35", "0.3499999940395355") is True

    def test_struct_export_format(self):
        assert ue5_bridge._value_matches(
            "(R=0.1,G=0.2,B=0.3,A=1.0)",
            "(SpecifiedColor=(R=0.100000,G=0.200000,B=0.300000,A=1.000000))",
            ) is True

    def test_mismatch_detected(self):
        assert ue5_bridge._value_matches(
            "(R=0.1,G=0.2,B=0.3,A=1.0)",
            "(SpecifiedColor=(R=1.000000,G=1.000000,B=1.000000,A=1.000000))",
            ) is False

    def test_none_is_mismatch(self):
        assert ue5_bridge._value_matches("x", None) is False
