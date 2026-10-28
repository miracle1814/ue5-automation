#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_dashboard.py — 管理端仪表盘（/ui 只读观测面）契约测试
==========================================================
T-20261024-UE5BRIDGE-DASHBOARD · P1 规格书《管理端仪表盘设计》§2.4

覆盖面（与规格书实现清单一一对应）：
  ① 事件管道：正常记录 / 异常吞掉不阻塞（fail-open）/ 环形缓冲上限 /
     JSONL 会话落盘格式 / /ui 自访问不入流（防轮询自激）
  ② API：401 无 token、?token= 查询参数通过、增量游标、graph 缓存（LRU N=8）、
     审计 zip 导出、画面占位（截图闭环未就绪 → 404 而非报错）
  ③ 只读性：遍历 app.routes 断言 /ui* 全部 GET（无 POST/PUT/DELETE/PATCH）
  ④ 页面契约：index.html 关键 DOM id 存在（轻量字符串断言，不上前端测试框架）
  ⑤ MCP 面不受影响：tools/list 保持 27 个（仪表盘不新增 MCP 工具；
    27 = 26 + T-20261024-UE5BRIDGE-SCREENSHOT 的 ue5_screenshot）

测试方式：真实 ue5_bridge 模块 + 真实 FastAPI TestClient（走完整中间件栈），
UE 侧 fake_unreal 替身 —— 与 test_token_auth.py 同模式。
"""

import io
import json
import os
import sys
import zipfile

# ── 必须在 import ue5_bridge 之前设置环境变量（同 test_token_auth.py 教训）──
os.environ["UE5_BRIDGE_TEST_MODE"] = "1"

_SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPTS_DIR = os.path.join(_SKILL_ROOT, "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import pytest  # noqa: E402
import ui_dashboard as ud  # noqa: E402  观测面数据管道（单测直连）
import ue5_bridge  # noqa: E402  真实模块（测试模式 → fake_unreal 注入）
from fastapi.testclient import TestClient  # noqa: E402

# 不依赖 import 顺序的 token：动态改 _BRIDGE_TOKEN（_verify_token 请求时现读）
_DASH_TOKEN = "dashboard-test-token"

client = TestClient(ue5_bridge.app)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """每个用例：清空观测面内存态 + 固定 token + JSONL 落临时目录。"""
    ud.reset_for_tests()
    monkeypatch.setattr(ue5_bridge, "_BRIDGE_TOKEN", _DASH_TOKEN)
    yield


def _h():
    return {"X-Skill-Token": _DASH_TOKEN}


# ══════════════════════════════════════════════════════════
#  ① 事件管道
# ══════════════════════════════════════════════════════════

class TestEventPipeline:
    """中间件事件记录：正常 / fail-open / 环形缓冲 / JSONL / 自访问过滤。"""

    REQUIRED_KEYS = {"ts", "trace_id", "route", "method", "status", "latency_ms"}

    def test_middleware_records_normal_request(self):
        """/command 请求 → 环形缓冲出现一条字段齐全的事件。

        （T-20261025-VERIFY 起 /health 是面板 5s 探测，已不入流 —— 本用例改用
        /command 打点；/health 不入流由 test_health_probe_not_recorded 锁定。）"""
        r = client.post("/command", json={"command": "task_list", "params": {}},
                        headers=_h())
        assert r.status_code == 200, r.text
        events, last_id = ud.events_since(0)
        assert events, "事件缓冲不应为空"
        ev = [e for e in events if e["route"] == "/command"][-1]
        assert self.REQUIRED_KEYS <= set(ev.keys()), f"字段缺失: {ev.keys()}"
        assert ev["method"] == "POST"
        assert ev["status"] == 200
        assert ev["trace_id"] == r.headers.get("x-trace-id"), \
            "事件 trace_id 应与响应头 X-Trace-Id 一致（同一咽喉分配）"
        assert ev["latency_ms"] >= 0
        assert last_id == ev["id"]

    def test_health_probe_not_recorded(self):
        """/health 是面板 5s 连接探测 → 不入事件流（防刷屏/统计虚增）。"""
        assert client.get("/health", headers=_h()).status_code == 200
        events, _ = ud.events_since(0)
        assert not any(e["route"] == "/health" for e in events), \
            "/health 探测不应入事件流"

    def test_failing_recorder_does_not_block(self, monkeypatch):
        """注入必然抛错的记录器 → 命令仍成功（fail-open 契约，规格书硬要求）。"""
        def _boom(*a, **kw):
            raise RuntimeError("注入的记录器爆炸（观测面失效模拟）")
        monkeypatch.setattr(ud, "record_event", _boom)
        r = client.get("/health", headers=_h())
        assert r.status_code == 200, \
            "事件记录异常绝不允许阻塞命令主路径（fail-open 唯一例外声明处）"
        r2 = client.post("/mcp", headers=_h(),
                         json={"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}})
        assert r2.status_code == 200

    def test_ring_buffer_cap_1000(self):
        """环形缓冲上限 1000：写入 1005 条 → 只留最近 1000 条，id 连续。"""
        for i in range(1005):
            ud.record_event(route="/command", method="POST", status=200,
                            latency_ms=1.0, trace_id=f"t{i}")
        events, last_id = ud.events_since(0, limit=10 ** 9)
        assert len(events) == 1000, "环形缓冲上限应为 1000"
        ids = [e["id"] for e in events]
        assert ids == list(range(ids[0], ids[0] + 1000)), "保留的应是最新的连续 1000 条"
        assert last_id == 1005

    def test_jsonl_session_file_format(self, monkeypatch, tmp_path):
        """JSONL 落盘：每日一文件、每行一条 JSON、字段契约齐全。"""
        monkeypatch.setenv("UE5_BRIDGE_SESSIONS_DIR", str(tmp_path))
        ud.record_event(route="/command", method="POST", status=200,
                        latency_ms=1.5, trace_id="abc")
        ud.record_event(route="/mcp", method="POST", status=200,
                        latency_ms=2.5, trace_id="def", tool_name="ue5_check_health")
        files = list(tmp_path.glob("*.jsonl"))
        assert len(files) == 1, "应落一个当日会话文件（YYYYMMDD.jsonl）"
        assert len(files[0].name) == len("YYYYMMDD") + len(".jsonl")
        lines = files[0].read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2, "两次记录 → 两行（追加写）"
        for line in lines:
            obj = json.loads(line)
            assert self.REQUIRED_KEYS <= set(obj.keys()), f"JSONL 字段缺失: {obj.keys()}"
        assert json.loads(lines[1])["tool_name"] == "ue5_check_health"

    def test_ui_self_traffic_not_recorded(self):
        """/ui* 自访问不入流：页面轮询不能把事件流灌满自己（防自激）。"""
        before = ud.events_since(0)[1]
        client.get("/ui", params={"token": _DASH_TOKEN})
        client.get("/ui/api/events", headers=_h())
        client.get("/ui/api/reports", headers=_h())
        assert ud.events_since(0)[1] == before, "/ui* 请求不应产生事件"

    def test_tool_name_via_mcp_hook(self):
        """tool_name 由 /mcp 处理器挂钩取得（非中间件读 body）→ 事件带工具名。"""
        client.post("/mcp", headers=_h(), json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "ue5_check_health", "arguments": {}}})
        events, _ = ud.events_since(0)
        ev = [e for e in events if e["route"] == "/mcp"][-1]
        assert ev["tool_name"] == "ue5_check_health"
        assert ev["category"] == "preflight"

    def test_categorize_mapping_table(self):
        """六类映射表：tool 级优先于 route 级；未命中 → other（绝不猜）。"""
        assert ud.categorize("/health") == "preflight"
        assert ud.categorize("/read") == "read"
        assert ud.categorize("/build") == "write"
        assert ud.categorize("/mcp", "ue5_compile_and_diagnose") == "compile"
        assert ud.categorize("/mcp", "ue5_apply_blueprint_graph") == "verify"
        assert ud.categorize("/mcp", "ue5_save_asset") == "save"
        assert ud.categorize("/mcp", "ue5_read_nodes") == "read"
        assert ud.categorize("/mcp", "ue5_build_blueprint") == "write"
        # tool 级覆盖 route 级（一条 /mcp 里真正干活的是工具）
        assert ud.categorize("/mcp", "ue5_check_health") == "preflight"
        # 未命中 → other
        assert ud.categorize("/mcp", "ue5_unknown_tool") == "other"
        assert ud.categorize("/nowhere") == "other"

    def test_tool_names_from_mcp_payload(self):
        """payload 解析：单条 / 批量合并 / 非 tools/call → 空串（协议噪音）。"""
        single = {"method": "tools/call",
                  "params": {"name": "ue5_read_nodes", "arguments": {}}}
        assert ud.tool_names_from_mcp_payload(single) == "ue5_read_nodes"
        batch = [single, {"method": "tools/call",
                          "params": {"name": "ue5_save_asset", "arguments": {}}},
                 {"method": "initialize", "params": {}}]
        assert ud.tool_names_from_mcp_payload(batch) == "ue5_read_nodes,ue5_save_asset"
        assert ud.tool_names_from_mcp_payload({"method": "tools/list"}) == ""
        assert ud.tool_names_from_mcp_payload(None) == ""
        assert ud.tool_names_from_mcp_payload("garbage") == ""


# ══════════════════════════════════════════════════════════
#  ② 只读 API
# ══════════════════════════════════════════════════════════

class TestUiApi:
    """token 通道 / 增量游标 / graph 缓存 / 审计导出 / 画面占位。"""

    def test_no_token_401(self):
        """v6.0.1：/ui 静态壳公开（无数据）；所有数据接口无 token → 401。"""
        shell = client.get("/ui")
        assert shell.status_code == 200
        assert "auth-banner" in shell.text
        for path in ("/ui/api/events", "/ui/api/graph",
                     "/ui/api/reports", "/ui/api/reports/export",
                     "/ui/api/screenshot", "/ui/api/task"):
            r = client.get(path)
            assert r.status_code == 401, f"{path} 无 token 应 401，实得 {r.status_code}"

    def test_wrong_token_401(self):
        r = client.get("/ui/api/events", headers={"X-Skill-Token": "wrong-token"})
        assert r.status_code == 401

    def test_query_token_accepted(self):
        """?token= 查询参数放行（/ui* 专用放宽：浏览器无法配自定义 header）。"""
        r = client.get("/ui", params={"token": _DASH_TOKEN})
        assert r.status_code == 200
        r = client.get("/ui/api/events", params={"token": _DASH_TOKEN})
        assert r.status_code == 200

    def test_bearer_token_accepted(self):
        r = client.get("/ui/api/events", headers={"Authorization": f"Bearer {_DASH_TOKEN}"})
        assert r.status_code == 200

    def test_events_incremental_cursor(self):
        """since=<id> 只回增量；last_id 无新事件也不回退（防缓冲淘汰游标回退）。"""
        client.post("/command", json={"command": "task_list", "params": {}},
                    headers=_h())
        events, last_id = ud.events_since(0)
        assert events
        # since=last_id → 无增量
        r = client.get(f"/ui/api/events?since={last_id}", headers=_h())
        body = r.json()
        assert body["events"] == []
        assert body["last_id"] == last_id
        # 新事件 → since=last_id 之后可见且只含新事件
        client.post("/command", json={"command": "task_list", "params": {}},
                    headers=_h())
        r2 = client.get(f"/ui/api/events?since={last_id}", headers=_h())
        new_events = r2.json()["events"]
        assert new_events and all(e["id"] > last_id for e in new_events)

    def test_graph_cache_lru_cap_8(self):
        """快照缓存：容量 8、LRU 淘汰最旧、asset 命中 / 缺省回最近。"""
        for i in range(9):
            ud.put_graph_snapshot(f"/Game/BP_{i}", {"nodes": [], "edges": [],
                                                    "meta": {}, "graph_name": "g"})
        assert len(ud.snapshot_assets()) == 8, "缓存应保留最近 8 个资产"
        assert "/Game/BP_0" not in ud.snapshot_assets(), "最旧应被淘汰"
        assert ud.snapshot_assets()[-1] == "/Game/BP_8", "最新在尾"
        # 命中刷新 LRU：访问 BP_1 后写入新资产，淘汰的应是 BP_2 而非 BP_1
        assert ud.get_graph_snapshot("/Game/BP_1") is not None
        ud.put_graph_snapshot("/Game/BP_new", {"nodes": [], "edges": [], "meta": {}})
        assert "/Game/BP_1" in ud.snapshot_assets()
        assert "/Game/BP_2" not in ud.snapshot_assets()
        # 缺省 → 最近更新的
        assert ud.get_graph_snapshot()["asset"] == "/Game/BP_new"

    def test_graph_api_hit_and_miss(self):
        """/ui/api/graph：命中返回归一快照；未命中 404 + 缓存资产清单。"""
        ud.observe_tool_result("ue5_apply_blueprint_graph", {
            "blueprint_path": "/Game/BP_Demo",
            "graph_spec": {"graph": "EventGraph",
                           "nodes": [{"id": "e1", "kind": "event", "target": "BeginPlay",
                                      "pos": [0, 0]},
                                     {"id": "p1", "kind": "function",
                                      "target": "PrintString"}],
                           "edges": [{"from": "e1.then", "to": "p1.execute"}]},
        }, {"success": True, "verified": True, "nodes_applied": 2, "edges_applied": 1,
            "node_guids": {"e1": "G1", "p1": "G2"}, "errors": [], "warnings": []})
        # 命中（显式 asset）
        r = client.get("/ui/api/graph", params={"asset": "/Game/BP_Demo", "token": _DASH_TOKEN})
        assert r.status_code == 200
        body = r.json()
        assert body["found"] is True
        snap = body["snapshot"]
        assert snap["source"] == "graph_spec"
        assert [n["id"] for n in snap["nodes"]] == ["e1", "p1"]
        assert snap["edges"] == [{"from": ["e1", "then"], "to": ["p1", "execute"]}]
        assert snap["meta"]["node_guids"] == {"e1": "G1", "p1": "G2"}
        # 缺省 asset → 最近一个
        r2 = client.get("/ui/api/graph", params={"token": _DASH_TOKEN})
        assert r2.json()["snapshot"]["asset"] == "/Game/BP_Demo"
        # 未命中 → 404 + 清单（fail-loud：不假装成功）
        r3 = client.get("/ui/api/graph", params={"asset": "/Game/Nope", "token": _DASH_TOKEN})
        assert r3.status_code == 404
        assert r3.json()["found"] is False

    def test_graph_reader_l2_normalization(self):
        """reader L2 → 归一快照：节点 id 取 guid（回退标题）、连线按 guid 归一。"""
        l2 = {"available": True, "graphs": [{
            "graph_name": "EventGraph",
            "nodes": [
                {"title": "事件开始运行", "class": "K2Node_Event",
                 "guid": "AAA", "pos": [0, 0]},
                {"title": "打印字符串", "class": "K2Node_CallFunction",
                 "guid": "BBB", "pos": [300, 0]},
            ],
            "connections": [
                {"from_node": "事件开始运行", "from_pin": "then",
                 "to_node": "打印字符串", "to_pin": "execute",
                 "from_guid": "AAA", "to_guid": "BBB"},
            ]}]}
        snap = ud._snap_from_l2("/Game/BP_R", l2)
        assert [n["id"] for n in snap["nodes"]] == ["AAA", "BBB"]
        assert snap["nodes"][1]["kind"] == "function"
        assert snap["edges"] == [{"from": ["AAA", "then"], "to": ["BBB", "execute"]}]
        # /read 端点挂钩 → 快照进缓存（blueprint_reader 宿主 CLI 的数据源）
        ud.observe_read_result("/Game/BP_R", "full",
                               {"success": True, "level": "full", "l2": l2})
        assert ud.get_graph_snapshot("/Game/BP_R") is not None

    def test_compile_error_pinned_to_snapshot(self):
        """compile 族结果 → 已有快照更新编译错误钉（node_guid 定位）。"""
        ud.put_graph_snapshot("/Game/BP_C", {"nodes": [{"id": "n1", "title": "打印字符串",
                                                        "kind": "function", "pos": [0, 0],
                                                        "guid": "CCC"}],
                                             "edges": [], "meta": {"node_guids": {}}})
        ud.observe_tool_result("ue5_compile_and_diagnose", {"bp_path": "/Game/BP_C"}, {
            "success": False, "compiled": False, "compile_status": "BS_Error",
            "errors": [{"code": "CompileError.Unclassified", "message": "类型不匹配",
                        "node_guid": "CCC", "node_name": "打印字符串",
                        "fix_hint": "检查引脚类型"}],
            "warnings": []})
        snap = ud.get_graph_snapshot("/Game/BP_C")
        assert snap["meta"]["compile_errors"][0]["node_guid"] == "CCC"
        # 验证卡片同步产生（编译失败 → ok=False）
        verdicts, _ = ud.verdicts_since(0)
        assert verdicts and verdicts[-1]["ok"] is False

    def test_verdicts_from_write_tools(self):
        """写工具 → 验证卡片：verified/warnings/errors 抽取 + 失败标记。"""
        ud.observe_tool_result("ue5_apply_blueprint_graph",
                               {"blueprint_path": "/Game/BP_V", "graph_spec": None},
                               {"success": True, "verified": True, "ok": True,
                                "warnings": ["隐式转换节点 1 个"], "errors": []})
        verdicts, _ = ud.verdicts_since(0)
        assert verdicts[-1]["verified"] is True
        assert verdicts[-1]["ok"] is True
        assert verdicts[-1]["warnings"] == ["隐式转换节点 1 个"]
        # 失败写操作
        ud.observe_tool_result("ue5_connect_pins",
                               {"blueprint_path": "/Game/BP_V"},
                               {"ok": False, "error": "引脚不存在"})
        verdicts2, _ = ud.verdicts_since(0)
        assert verdicts2[-1]["ok"] is False
        # 非写工具不产生卡片
        n_before = len(ud.verdicts_since(0)[0])
        ud.observe_tool_result("ue5_read_blueprint", {"blueprint_path": "/x"}, {})
        assert len(ud.verdicts_since(0)[0]) == n_before

    def test_raising_tool_still_yields_verdict(self):
        """工具实现 raise（409 守卫 / 资产不存在）→ 失败卡片照样产生。

        "失败置顶红色"的前提是失败可见 —— raise 路径若漏记，验证墙只会报喜。
        """
        # /mcp 真实调用一个必然 raise 的工具（蓝图不存在 → BridgeError）
        r = client.post("/mcp", headers=_h(), json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "ue5_apply_blueprint_graph",
                       "arguments": {"bp_path": "/Game/BP_Definitely_Missing",
                                     "graph_spec": {"nodes": [], "edges": []}}}})
        assert r.status_code == 200
        assert r.json()["result"]["isError"] is True
        verdicts, _ = ud.verdicts_since(0)
        assert verdicts, "raise 路径必须也产生验证卡片（失败可见性）"
        last = verdicts[-1]
        assert last["ok"] is False and last["verified"] is False
        assert last["tool"] == "ue5_apply_blueprint_graph"
        assert last["errors"], "失败原因应进卡片"

    def test_reports_export_zip(self, monkeypatch, tmp_path):
        """审计导出：zip 可打开、含 manifest.json、内容含会话/报告。"""
        monkeypatch.setenv("UE5_BRIDGE_SESSIONS_DIR", str(tmp_path))
        ud.record_event(route="/command", method="POST", status=200,
                        latency_ms=1.0, trace_id="zz")
        r = client.get("/ui/api/reports/export", headers=_h())
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/zip")
        assert "attachment" in r.headers.get("content-disposition", "")
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            names = z.namelist()
            assert "manifest.json" in names
            assert any(n.startswith("sessions/") and n.endswith(".jsonl") for n in names)
            manifest = json.loads(z.read("manifest.json"))
            assert manifest["event_note"].startswith("事件字段契约")
        # 报告列表端点
        r2 = client.get("/ui/api/reports", headers=_h())
        assert r2.status_code == 200
        body = r2.json()
        assert "reports" in body and "sessions" in body
        assert any(s["name"].endswith(".jsonl") for s in body["sessions"])

    def test_screenshot_placeholder_404(self, monkeypatch):
        """画面面板：截图闭环未就绪 → 404 + 占位说明（读不到不报错）。"""
        monkeypatch.setattr(ud, "_screenshots_dir", lambda: "")
        r = client.get("/ui/api/screenshot", headers=_h())
        assert r.status_code == 404
        assert "截图闭环" in r.json()["detail"]


# ══════════════════════════════════════════════════════════
#  ③ 只读性 + MCP 面不受影响
# ══════════════════════════════════════════════════════════

class TestReadOnlyContract:
    """/ui* 永久只读（规格书边界：v1 不做任何写操作）。"""

    def test_ui_routes_are_get_only(self):
        """遍历 app.routes：/ui* 全部 GET（无 POST/PUT/DELETE/PATCH）。"""
        ui_routes = [r for r in ue5_bridge.app.routes
                     if getattr(r, "path", "").startswith("/ui")]
        assert ui_routes, "/ui 路由应已注册"
        for r in ui_routes:
            methods = getattr(r, "methods", set()) or set()
            assert methods <= {"GET", "HEAD"}, \
                f"{r.path} 注册了非只读方法 {methods} —— 违反 /ui 永久只读边界"
            assert r.path in {"/ui", "/ui/api/events", "/ui/api/graph",
                              "/ui/api/reports", "/ui/api/reports/export",
                              "/ui/api/screenshot", "/ui/api/screenshots",
                              "/ui/api/task", "/ui/api/stats",
                              "/ui/api/mcp-config", "/ui/api/scenarios"}, \
                f"意外的新 /ui 路由: {r.path}（白名单外新增需过规格书评审）"

    def test_mcp_tools_count_locked(self):
        """MCP 工具计数锁定（31 = 27 + T-20261024-UI-V2 的 task 四工具）。"""
        payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        tools = client.post("/mcp", json=payload, headers=_h()).json()["result"]["tools"]
        # T-20261028-TOOL-RESTORE：默认全量 34（按调用日志裁剪是统计错误）；
        # UE5_MCP_CORE_TOOLS=1 时才收窄到 16。
        assert len(tools) == 34, "tools/list 默认应为全量 34"

    def test_mcp_core_tools_opt_in(self, monkeypatch):
        """收窄档改为显式 opt-in：UE5_MCP_CORE_TOOLS=1 → 16 个。"""
        monkeypatch.setenv("UE5_MCP_CORE_TOOLS", "1")
        assert len(ue5_bridge._mcp_tool_defs_core()) == 18

    def test_mcp_gateway_opt_in(self, monkeypatch):
        """v6.5：网关档 12 个入口（补读/诊断/场景/任务收尾），能力仍可发现。"""
        monkeypatch.setenv("UE5_MCP_GATEWAY", "1")
        names = {t["name"] for t in ue5_bridge._mcp_tool_defs_core()}
        assert names == {
            "ue5_check_health", "ue5_tool_search", "ue5_execute_command",
            "ue5_read_blueprint", "ue5_search_blueprint_nodes",
            "ue5_apply_blueprint_graph", "ue5_compile_and_diagnose",
            "ue5_screenshot", "ue5_verify_behavior", "ue5_verify_scenario",
            "ue5_task_begin", "ue5_task_end"}


# ══════════════════════════════════════════════════════════
#  ④ 页面契约（关键 DOM id 字符串断言）
# ══════════════════════════════════════════════════════════

class TestPageContract:
    """单文件页面契约：关键 DOM id / 交互钩子存在（轻量字符串断言）。

    v3（T-20261025-UI-V3）任务档案模型（用户《可视管理需求.txt》落地）：
    KPI 条 + 任务选择器 + 单页档案（①需求确认 ②实现思路 ③执行脉络
    ④复盘）；截图对比降级为步骤内 evidence.pair；统计降级进调试抽屉。
    """

    REQUIRED_IDS = (
        # 左侧导航栏（v3.3：用户确认的布局）
        "token-input", "conn-status", "nav-taskboard", "nav-debug",
        "task-list", "side-kpi",
        # KPI 条（用户 txt：总任务数、成功率、完成时间）
        "kpi-total", "kpi-rate", "kpi-running", "kpi-duration",
        # 任务档案四段
        "panel-dossier", "dossier-status", "dossier-goal", "dossier-req",
        "dossier-approach", "dossier-steps", "dossier-retro",
        # 调试视图（v3.3：人话面板——桥状态/使用摘要/最近错误）
        "view-debug", "dbg-engine", "dbg-project", "dbg-uptime", "dbg-calls",
        "call-summary", "recent-errors", "event-list",
    )

    def _page(self) -> str:
        r = client.get("/ui", params={"token": _DASH_TOKEN})
        assert r.status_code == 200
        return r.text

    def test_api_token_uses_header_not_url(self):
        """v6.4.1：数据请求用 X-Skill-Token 头，token 不进 API URL。"""
        page = self._page()
        assert 'headers["X-Skill-Token"] = TOKEN' in page
        assert 'fetch(path + (path.indexOf("?")' not in page

    def test_task_selection_uses_unique_key_and_delegation(self):
        """v6.0.1：历史 ID 可重复 → UI 用 ID+开始时间 作唯一键；点击用事件委托。"""
        page = self._page()
        assert "function taskKey(" in page
        assert "viewTaskKey" in page
        assert 'data-key="' in page
        assert '$("task-list").addEventListener("click"' in page
        assert 'viewTaskKey = item.getAttribute("data-key")' in page
        # 逐项监听会在 1s 轮询重建时丢点击 —— 不应回退到该写法
        assert "item.addEventListener(\"click\"" not in page

    def test_page_served_as_html(self):
        page = self._page()
        assert page.lstrip().startswith("<!DOCTYPE html>")
        assert 'lang="zh-CN"' in page

    def test_no_cross_line_regex(self):
        """回归（T-20261026）：<script> 内不能出现跨行正则字面量。

        一个跨行正则会让整个脚本解析失败（TOKEN/pollTask 全 undefined、
        面板空白）—— 真机踩中过一次。纯字符扫描，避开转义/正则干扰；
        排除注释与闭合标签产生的 // 与 </ 误报。
        """
        page = self._page()
        js = page[page.index("<script>"):page.rindex("</script>")]
        nl = chr(10)
        bad = []
        for k in range(len(js) - 1):
            if js[k] == "/" and js[k + 1] == nl:
                if k == 0 or js[k - 1] not in "</*":
                    bad.append(k)
        assert not bad, "疑似跨行正则（会整块失效）@script 偏移 " + str(bad[:3])

    def test_required_dom_ids(self):
        page = self._page()
        for dom_id in self.REQUIRED_IDS:
            assert f'id="{dom_id}"' in page, f"页面缺少契约 DOM id: {dom_id}"

    def test_page_wiring(self):
        """轮询端点 / token 记忆 / 白底主题 / 档案四段 的接线存在。"""
        page = self._page()
        assert "/ui/api/task" in page
        assert "/ui/api/stats" in page
        assert "/ui/api/screenshot?name=" in page or "shotUrl" in page
        assert "localStorage" in page, "token 应 localStorage 记住"
        assert "URLSearchParams(location.search)" in page, "?token= 查询参数解析（B6 修复契约）"
        assert "location.host" in page, "连接状态显示真实端口（勿硬编码）"
        assert "XMLHttpRequest" not in page and "fetch(" in page
        # 白底主题（用户指定 2026-10-24）：背景必须是浅色（#f 开头的亮色值；
        # 不锁具体色值 —— 视觉精修会微调 hex，浅色语义不变）
        assert "--bg:#f" in page
        # v4 语义锚点（T-20261025-UI-V4）：原话对照 / 命令流水 / 阶段轨道 / 复盘
        for anchor in ("需求来源", "你的原话", "我理解成", "命令流水",
                       "UE 内动作", "复盘", "stage-rail"):
            assert anchor in page, f"缺少 v4 语义锚点: {anchor}"
