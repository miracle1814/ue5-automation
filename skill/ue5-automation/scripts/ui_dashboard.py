#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ui_dashboard.py — 管理端可视化仪表盘数据管道（/ui 只读观测面）
==============================================================
T-20261024-UE5BRIDGE-DASHBOARD · P1 规格书《管理端仪表盘设计》

职责边界（观测面绝不侵入业务）：
  · 本模块是 ue5_bridge 的**附属数据管道**：事件环形缓冲 + JSONL 会话落盘 +
    图纸快照缓存 + 报告/截图扫描。全部只读、全部 fail-open（唯一例外声明见
    `record_event` 与 ue5_bridge.add_trace_id 内注释）。
  · 本模块**不 import ue5_bridge**（单向依赖：bridge → 本模块，反向 import
    会成环）；因此 GraphSpec kind 映射在 `_kind_of_class` 内自带一份与
    ue5_bridge._graph_apply_kind_of_class 同口径的轻量副本（8 行，改动需同步）。
  · 不新增 MCP 工具：本模块只服务于 HTTP /ui* 面，tools/list 不变（26 个）。

事件模型（JSONL 每行一条，字段契约 —— 规格书）：
    {ts, trace_id, route, method, status, latency_ms, tool_name?, category}

  · category 映射表（route + tool_name → 六类，页面任务流水按此着色/分组）：
    preflight(健康/诊断) / read(读) / write(写) / compile(编译) /
    verify(读回验证) / save(保存)；未命中 → other。
    映射表是**显式字典**而非启发式：宁可 other 也不猜（fail-loud 同源纪律
    在观测面的投影 —— 分类错了比不分类更误导）。
  · tool_name 由 MCP 处理器（/mcp 端点）挂钩写入 request.state，中间件收尾时
    读取 —— 刻意不在中间件里读请求体（流式 body 只能读一次，读了就毁）。

【刻意的 fail-open 设计 —— 对 fail-loud 纪律的明确例外】
  本仓库纪律是 fail-loud（绝不静默吞错），但事件记录是唯一例外：
  观测面失效（磁盘满 / JSONL 不可写 / 缓冲异常）**绝不许**阻塞命令主路径 ——
  一个"为了让人类看见"的旁路把命令弄挂，是本末倒置；agent 的写操作可信度
  由返回体 verified/warnings 承担，不依赖仪表盘。
  该例外只覆盖**事件记录与快照缓存**两处；/ui* 只读端点本身照常 fail-loud
  （查不到就 404，不假装成功）。

Python 3.9+ 兼容（UE5.1 内嵌 Python 3.9 也要能跑）：无 match、无 `X | Y` 注解。
"""

import io
import json
import os
import time
import threading
import zipfile
from collections import deque
from datetime import datetime

# ══════════════════════════════════════════════════════════
#  配置常量
# ══════════════════════════════════════════════════════════

#: 内存事件环形缓冲上限（规格书：1000 条）
EVENT_BUFFER_CAP = 1000

#: 写操作验证卡片环形缓冲上限（验证墙数据源；内存态，不入 JSONL）
VERDICT_BUFFER_CAP = 100

#: 图纸快照 LRU 缓存容量（最近 N=8 个资产）
GRAPH_SNAPSHOT_CAP = 8

#: 任务流水六类映射 —— route 级（MCP 命中 tool 级优先）
ROUTE_CATEGORY = {
    # preflight：健康与诊断
    "/health": "preflight", "/diag": "preflight",
    # read：只读面
    "/read": "read", "/logs": "read",
    "/pie/state": "read", "/pie/actors": "read", "/pie/get_property": "read",
    # write：写通道（/command 是白名单命令通道，历史上以写为主；v1 不解析
    # 其请求体取命令名 —— 中间件不读 body 是铁律，命令名粒度由 /mcp 面覆盖）
    "/command": "write", "/build": "write", "/build-batch": "write",
    "/connect": "write", "/disconnect": "write", "/delete_node": "write",
    "/pie/start": "write", "/pie/stop": "write", "/pie/set_transform": "write",
    "/editor/batch": "write",
}

#: 任务流水六类映射 —— MCP 工具级（/mcp 面的 tool_name）
TOOL_CATEGORY = {
    # preflight
    "ue5_check_health": "preflight", "ue5_run_diagnostics": "preflight",
    # read
    "ue5_read_blueprint": "read", "ue5_read_nodes": "read",
    "ue5_read_connections": "read", "ue5_search_blueprint_nodes": "read",
    "ue5_list_assets": "read", "ue5_read_logs": "read",
    "ue5_read_pie_state": "read", "ue5_read_pie_property": "read",
    "ue5_list_pie_actors": "read",
    # write
    "ue5_build_blueprint": "write", "ue5_build_batch": "write",
    "ue5_connect_pins": "write", "ue5_disconnect_pin": "write",
    "ue5_delete_node": "write", "ue5_set_pie_transform": "write",
    "ue5_run_batch": "write", "ue5_execute_command": "write",
    # compile（编译与结构化诊断是一族）
    "ue5_compile_blueprint": "compile", "ue5_compile_and_diagnose": "compile",
    # verify（整图写入 = 写 + 读回验证一体；验证墙的主数据源）
    "ue5_apply_blueprint_graph": "verify",
    # save
    "ue5_save_asset": "save",
}

#: 工具名批量合并上限（批量 JSON-RPC 数组可能携带多工具，防异常长字符串）
_TOOL_NAME_CAP = 120

_lock = threading.Lock()
_event_seq = 0            # 单调递增事件 id（增量轮询游标）
_events = deque(maxlen=EVENT_BUFFER_CAP)
_verdict_seq = 0          # 验证卡片游标（与事件游标独立）
_verdicts = deque(maxlen=VERDICT_BUFFER_CAP)
_graph_snapshots = {}     # asset → snapshot（OrderedDict 语义用手工淘汰模拟 3.9 兼容）
_graph_order = []         # LRU 访问序（最新在尾）

# ── JSONL 会话目录 ──
# 默认 %APPDATA%/ue5_bridge/sessions（无 APPDATA 的平台回退 ~/.ue5_bridge/sessions）；
# 环境变量 UE5_BRIDGE_SESSIONS_DIR 最高优先（离线测试注入 tmp 目录用）；
# UE5_BRIDGE_TEST_MODE=1（离线测试套件）且未显式指定 → 临时目录，不污染真实 APPDATA。
def _sessions_dir():
    explicit = os.environ.get("UE5_BRIDGE_SESSIONS_DIR")
    if explicit:
        return explicit
    if os.environ.get("UE5_BRIDGE_TEST_MODE") == "1":
        import tempfile
        return os.path.join(tempfile.gettempdir(), "ue5_bridge_test_sessions")
    return os.path.join(os.environ.get("APPDATA", os.path.expanduser("~/.ue5_bridge")),
                        "ue5_bridge", "sessions")


# ══════════════════════════════════════════════════════════
#  分类映射
# ══════════════════════════════════════════════════════════

def categorize(route: str, tool_name: str = "") -> str:
    """route + tool_name → 六类之一（未命中 → other，绝不猜）。

    tool 级优先于 route 级：一条 /mcp 请求里真正干活的是工具，不是路由。
    """
    if tool_name:
        cat = TOOL_CATEGORY.get(tool_name)
        if cat:
            return cat
    return ROUTE_CATEGORY.get(route, "other")


# ══════════════════════════════════════════════════════════
#  tool_name 挂钩（MCP 处理器 → 中间件的传递通道）
# ══════════════════════════════════════════════════════════

def tool_names_from_mcp_payload(payload) -> str:
    """从 /mcp 请求体提取 tool_name（MCP 处理器内调用，非中间件）。

    · 单条 {"method":"tools/call","params":{"name":...}} → name
    · 批量数组 → 逗号合并（截断到 120 字符）
    · 其它 method（initialize/tools/list…）→ ""（协议噪音，不冒充工具调用）
    任何形态异常都按 "" 处理 —— 这是观测面，不是校验面（校验在 _mcp_handle_one）。
    """
    try:
        items = payload if isinstance(payload, list) else [payload]
        names = []
        for item in items:
            if not isinstance(item, dict):
                continue
            if str(item.get("method") or "") != "tools/call":
                continue
            params = item.get("params") or {}
            name = str(params.get("name") or "").strip()
            if name:
                names.append(name)
        joined = ",".join(names)[:_TOOL_NAME_CAP]
        return joined
    except Exception:
        return ""


def note_tool_name(request, names: str) -> None:
    """MCP 处理器把 tool_name 记到 request.state（中间件收尾时读取）。

    request.state 底层是 scope 共享字典，BaseHTTPMiddleware 与端点可见同一份。
    全程吞异常：拿不到 tool_name 的事件照发（tool_name 为空），不许影响响应。
    """
    try:
        if names:
            request.state.ui_tool_name = str(names)
    except Exception:
        pass  # fail-open：观测面永不阻塞（例外声明见模块 docstring）


def tool_name_of(request) -> str:
    """中间件侧读取 tool_name（读不到 → ""）。"""
    try:
        return str(getattr(request.state, "ui_tool_name", "") or "")
    except Exception:
        return ""


# ══════════════════════════════════════════════════════════
#  事件管道：环形缓冲 + JSONL 会话落盘
# ══════════════════════════════════════════════════════════

def record_event(route: str, method: str, status, latency_ms: float,
                 trace_id: str = "", tool_name: str = "", error: bool = False):
    """记录一条请求事件：内存环形缓冲 + 追加写 JSONL 会话文件。

    ★ fail-open 边界（对 fail-loud 纪律的唯一例外，理由见模块 docstring）：
      本函数内部任何异常都吞掉 —— JSONL 写失败不拖垮缓冲，缓冲异常不拖垮
      调用方（ue5_bridge.add_trace_id 中间件另有外层兜底 try/except）。

    · /ui* 自访问不入流：页面 500ms 轮询若也被记录，事件流会被自己灌满
      （自激反馈），且对"agent 干了什么"零信息量。
    · 事件 id 单调递增，是 /ui/api/events 增量轮询的游标。
    """
    try:
        route = str(route or "")
        if route.startswith("/ui"):
            return None  # 仪表盘自访问不入流（理由见上）
        if route in ("/health",):
            # /health 是面板 5s 连接探测（真机实测：事件流被它刷屏、统计虚增），
            # 对「agent 干了什么」零信息量 → 不入流（同 /ui 口径）
            return None
        global _event_seq
        event = {
            "id": 0,  # 占位，持锁后回填（保证 id 顺序与缓冲顺序一致）
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "trace_id": str(trace_id or ""),
            "route": route,
            "method": str(method or ""),
            "status": int(status) if status is not None else 0,
            "latency_ms": round(float(latency_ms or 0.0), 1),
            "tool_name": str(tool_name or ""),
            "category": categorize(route, str(tool_name or "")),
        }
        if error:
            event["error"] = True
        with _lock:
            _event_seq += 1
            event["id"] = _event_seq
            _events.append(event)
        _append_session_jsonl(event)
        return event
    except Exception:
        # fail-open：观测面失效绝不阻塞命令主路径（本文件唯一的刻意例外）
        return None


def events_since(since: int = 0, limit: int = 500):
    """增量读取：id > since 的事件（旧→新），limit 封顶。

    返回 (events, last_id)。last_id 是缓冲内最大 id（无新事件时也返回，
    页面把它当作下一轮 since，避免缓冲淘汰后游标回退）。
    """
    try:
        with _lock:
            events = [e for e in _events if e["id"] > int(since or 0)]
            last_id = _event_seq
        if limit and int(limit) > 0:
            events = events[-int(limit):]
        return events, last_id
    except Exception:
        return [], int(since or 0)


def _append_session_jsonl(event: dict) -> None:
    """追加一条事件到当日 JSONL 会话文件（%APPDATA%/ue5_bridge/sessions/YYYYMMDD.jsonl）。

    · 每次打开-追加-关闭：量级是"人看仪表盘"级别（每秒几条），不值得常驻
      句柄；换来的是不持有文件锁、外部可随时拷走审计。
    · 写失败（磁盘满/权限）静默放弃 —— 缓冲里仍有这份事件，页面不缺数据；
      这是 fail-open 例外在文件层的具体化。
    """
    d = _sessions_dir()
    date_tag = datetime.now().strftime("%Y%m%d")
    path = os.path.join(d, date_tag + ".jsonl")
    try:
        os.makedirs(d, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass  # fail-open（理由见模块 docstring；缓冲兜底）


# ══════════════════════════════════════════════════════════
#  验证墙数据：写操作验证卡片（内存环形，不入 JSONL）
# ══════════════════════════════════════════════════════════

def record_verdict(tool: str, asset: str, result: dict) -> None:
    """从写操作返回体抽 verified/warnings/errors 存成验证卡片。

    fail-open 同事件管道；卡片只在内存（v1 审计以 JSONL 事件 + 返回体为准）。
    """
    try:
        if not isinstance(result, dict):
            return
        global _verdict_seq
        warnings = result.get("warnings") or []
        errors = result.get("errors") or []
        verdict = {
            "id": 0,
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "tool": str(tool or ""),
            "asset": str(asset or ""),
            "ok": result.get("success", result.get("ok", True)) is not False,
            "verified": bool(result.get("verified", False)),
            # 列表截断：页面只是卡片流，洪水条目没有观看价值（审计走导出件）
            "warnings": [str(w) for w in warnings][:20],
            "errors": [str(e) for e in errors][:20],
            "compile_status": str(result.get("compile_status", "") or ""),
        }
        with _lock:
            _verdict_seq += 1
            verdict["id"] = _verdict_seq
            _verdicts.append(verdict)
    except Exception:
        pass  # fail-open


def verdicts_since(since: int = 0, limit: int = 100):
    """增量读取验证卡片（语义同 events_since）。"""
    try:
        with _lock:
            items = [v for v in _verdicts if v["id"] > int(since or 0)]
            last_id = _verdict_seq
        if limit and int(limit) > 0:
            items = items[-int(limit):]
        return items, last_id
    except Exception:
        return [], int(since or 0)


# ══════════════════════════════════════════════════════════
#  图纸快照缓存（最近 N=8 个资产的 LRU）
# ══════════════════════════════════════════════════════════

def _kind_of_class(node_class: str) -> str:
    """节点类 → GraphSpec kind（与 ue5_bridge._graph_apply_kind_of_class 同口径）。

    独立副本的理由见模块 docstring（本模块不许 import ue5_bridge，防环）。
    """
    ncls = str(node_class or "")
    if ncls.startswith("K2Node_Event") or ncls.startswith("K2Node_CustomEvent"):
        return "event"
    if ncls.startswith("K2Node_CallFunction"):
        return "function"
    if ncls.startswith("K2Node_MacroInstance"):
        return "flow"
    if ncls.startswith("K2Node_VariableGet"):
        return "variable_get"
    if ncls.startswith("K2Node_VariableSet"):
        return "variable_set"
    return "other"


def put_graph_snapshot(asset: str, snapshot: dict) -> None:
    """写入/覆盖某资产的图纸快照（LRU，容量 GRAPH_SNAPSHOT_CAP）。

    fail-open；同名资产覆盖（页面永远看"最近一次"快照，历史走导出件）。
    """
    try:
        asset = str(asset or "").strip()
        if not asset or not isinstance(snapshot, dict):
            return
        snapshot["asset"] = asset
        snapshot["ts"] = datetime.now().isoformat(timespec="milliseconds")
        with _lock:
            if asset in _graph_snapshots:
                _graph_order.remove(asset)
            _graph_snapshots[asset] = snapshot
            _graph_order.append(asset)
            while len(_graph_order) > GRAPH_SNAPSHOT_CAP:
                old = _graph_order.pop(0)
                _graph_snapshots.pop(old, None)
    except Exception:
        pass  # fail-open


def get_graph_snapshot(asset: str = ""):
    """取快照：asset 为空 → 最近一个；命中则刷新 LRU；未命中 → None。"""
    try:
        with _lock:
            asset = str(asset or "").strip()
            if not asset:
                if not _graph_order:
                    return None
                asset = _graph_order[-1]
            snap = _graph_snapshots.get(asset)
            if snap is not None:
                # 刷新 LRU 访问序
                if asset in _graph_order:
                    _graph_order.remove(asset)
                _graph_order.append(asset)
            return snap
    except Exception:
        return None


def snapshot_assets():
    """缓存中的资产清单（旧→新）。"""
    try:
        with _lock:
            return list(_graph_order)
    except Exception:
        return []


def _snap_from_l2(bp_path: str, l2: dict, graph_name: str = "") -> dict:
    """reader L2 返回体 → 统一快照形态。

    统一形态（页面只认这一种，不管数据来自 reader 还是 GraphSpec）：
      nodes: [{id, title, kind, pos:[x,y]|None, guid}]
      edges: [{from:[node_id, pin], to:[node_id, pin]}]
      meta:  {source, graph_name, verified, compile_errors[], node_guids{}}
    reader 连线以标题为锚（from_guid 是 title→guid 反查所得），节点 id 取
    guid（缺失回退标题）；连线端点在两者之间归一到节点 id。
    """
    graphs = (l2 or {}).get("graphs") or []
    if graph_name:
        graphs = [g for g in graphs if g.get("graph_name") == graph_name] or graphs
    # 多图时取节点最多的（主图 EventGraph 几乎总是最大；避免 v1 做多图切换）
    g = max(graphs, key=lambda x: len(x.get("nodes") or []), default=None)
    if not g:
        return {"source": "reader", "graph_name": graph_name or "",
                "nodes": [], "edges": [], "meta": {"source": "reader"}}
    nodes, title2id = [], {}
    for n in (g.get("nodes") or []):
        title = str(n.get("title") or "?")
        guid = str(n.get("guid") or "")
        nid = guid or title
        title2id.setdefault(title, nid)
        pos = n.get("pos")
        nodes.append({
            "id": nid,
            "title": title,
            "kind": _kind_of_class(n.get("class")),
            "pos": [int(pos[0]), int(pos[1])] if pos and len(pos) == 2 else None,
            "guid": guid,
        })
    ids = {n["id"] for n in nodes}
    edges = []
    for c in (g.get("connections") or []):
        f = str(c.get("from_guid") or "") or title2id.get(str(c.get("from_node")), "")
        t = str(c.get("to_guid") or "") or title2id.get(str(c.get("to_node")), "")
        if f in ids and t in ids:
            edges.append({"from": [f, str(c.get("from_pin") or "")],
                          "to": [t, str(c.get("to_pin") or "")]})
    return {"source": "reader", "graph_name": str(g.get("graph_name") or ""),
            "nodes": nodes, "edges": edges,
            "meta": {"source": "reader", "verified": None,
                     "compile_errors": [], "node_guids": {}}}


def _snap_from_apply(bp_path: str, graph_spec: dict, result: dict) -> dict:
    """apply_graph 入参 GraphSpec + 返回体 → 统一快照形态。

    GraphSpec 是"模型声明的目标图"，带 spec id / kind / pos / node_guids ——
    是仪表盘图纸视图最高保真的数据源（比 reader 读取多出 kind 与意图）。
    """
    spec = graph_spec if isinstance(graph_spec, dict) else {}
    spec_nodes = spec.get("nodes") or []
    node_guids = (result or {}).get("node_guids") or {}
    nodes = []
    for n in spec_nodes:
        if not isinstance(n, dict):
            continue
        nid = str(n.get("id") or "")
        if not nid:
            continue
        pos = n.get("pos")
        nodes.append({
            "id": nid,
            "title": str(n.get("target") or nid),
            "kind": str(n.get("kind") or "other"),
            "pos": [int(pos[0]), int(pos[1])] if pos and len(pos) == 2 else None,
            "guid": str(node_guids.get(nid) or ""),
        })
    ids = {n["id"] for n in nodes}
    edges = []
    for e in (spec.get("edges") or []):
        if not isinstance(e, dict):
            continue
        try:
            f_id, f_pin = str(e.get("from") or "").split(".", 1)
            t_id, t_pin = str(e.get("to") or "").split(".", 1)
        except ValueError:
            continue  # 形态非法的边不渲染（语义校验在 _graph_spec_parse）
        if f_id in ids and t_id in ids:
            edges.append({"from": [f_id, f_pin], "to": [t_id, t_pin]})
    # 结构化编译错误（带 node_guid）→ 红标钉数据源
    compile_errors = []
    for err in ((result or {}).get("errors") or []):
        if isinstance(err, dict) and err.get("node_guid"):
            compile_errors.append(err)
    return {"source": "graph_spec", "graph_name": str(spec.get("graph") or "EventGraph"),
            "nodes": nodes, "edges": edges,
            "meta": {"source": "graph_spec",
                     "verified": bool((result or {}).get("verified", False)),
                     "compile_errors": compile_errors,
                     "node_guids": {str(k): str(v) for k, v in node_guids.items()}}}


def _observe_compile_result(bp_path: str, result: dict) -> None:
    """compile 族结果 → 给该资产已有快照更新编译错误钉（没有快照则跳过）。

    编译错误结构：{node_guid, node_name, message, fix_hint, ...}
    （_extract_blueprint_errors 契约）；node_guid 为空的条目钉不到节点上，
    页面在图例区以列表兜底展示。
    """
    snap = get_graph_snapshot(bp_path)
    if not snap or not isinstance(result, dict):
        return
    errors = [e for e in (result.get("errors") or [])
              if isinstance(e, dict) and (e.get("node_guid") or e.get("node_name"))]
    snap["meta"]["compile_errors"] = errors
    snap["meta"]["compiled"] = bool(result.get("success", result.get("compiled", True)))


def observe_tool_result(name: str, args: dict, result) -> None:
    """MCP 工具结果观测挂钩（_mcp_handle_one 的 tools/call 分支内调用）。

    覆盖面：read 族（reader L2 快照）、apply（GraphSpec 快照）、compile 族
    （错误钉更新）、写族（验证卡片）。全部 fail-open（外层还有一道兜底）。
    """
    try:
        if not isinstance(args, dict):
            args = {}
        bp = str(args.get("blueprint_path") or args.get("bp_path") or args.get("asset_path") or "")
        if name in ("ue5_read_blueprint",):
            l2 = (result or {}).get("l2") if isinstance(result, dict) else None
            if isinstance(l2, dict) and l2.get("available"):
                put_graph_snapshot(bp, _snap_from_l2(bp, l2))
        elif name in ("ue5_read_nodes", "ue5_read_connections"):
            if isinstance(result, dict) and result.get("available"):
                put_graph_snapshot(bp, _snap_from_l2(bp, result))
        elif name == "ue5_apply_blueprint_graph":
            if isinstance(result, dict):
                put_graph_snapshot(bp, _snap_from_apply(bp, args.get("graph_spec"), result))
                record_verdict(name, bp, result)
        elif name in ("ue5_compile_blueprint", "ue5_compile_and_diagnose"):
            _observe_compile_result(bp, result if isinstance(result, dict) else {})
            record_verdict(name, bp, result if isinstance(result, dict) else {})
        elif name in ("ue5_build_blueprint", "ue5_build_batch", "ue5_connect_pins",
                      "ue5_disconnect_pin", "ue5_delete_node", "ue5_save_asset"):
            record_verdict(name, bp, result if isinstance(result, dict) else {})
    except Exception:
        pass  # fail-open（观测面例外声明见模块 docstring）


def observe_read_result(bp_path: str, level: str, result: dict) -> None:
    """HTTP /read 端点观测挂钩（blueprint_reader 宿主 CLI 的数据源）。

    只有 L2/full 且 l2.available 才有图可存；其余静默跳过。
    """
    try:
        l2 = (result or {}).get("l2")
        if isinstance(l2, dict) and l2.get("available") and str(level).upper() in ("L2", "FULL"):
            put_graph_snapshot(str(bp_path or ""), _snap_from_l2(str(bp_path or ""), l2))
    except Exception:
        pass  # fail-open


# ══════════════════════════════════════════════════════════
#  报告扫描 + 审计导出 zip
# ══════════════════════════════════════════════════════════

def _skill_output_dir() -> str:
    """skill 输出目录（mermaid 渲染件 / reader 报告落这里）。"""
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output")


_REPORT_EXTS = (".md", ".json", ".png", ".svg")


def list_reports():
    """扫描报告文件（skill output/ 目录，按 mtime 新→旧）。"""
    out = []
    try:
        d = _skill_output_dir()
        if os.path.isdir(d):
            for name in os.listdir(d):
                p = os.path.join(d, name)
                if os.path.isfile(p) and os.path.splitext(name)[1].lower() in _REPORT_EXTS:
                    st = os.stat(p)
                    out.append({"name": name, "path": p, "size": st.st_size,
                                "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")})
    except OSError:
        pass  # fail-open：报告目录不可读 → 空列表（页面显示"暂无报告"）
    out.sort(key=lambda r: r["mtime"], reverse=True)
    return out


def list_sessions():
    """扫描 JSONL 会话文件（sessions 目录，新→旧）。"""
    out = []
    try:
        d = _sessions_dir()
        if os.path.isdir(d):
            for name in sorted(os.listdir(d), reverse=True):
                p = os.path.join(d, name)
                if os.path.isfile(p) and name.endswith(".jsonl"):
                    st = os.stat(p)
                    out.append({"name": name, "path": p, "size": st.st_size,
                                "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")})
    except OSError:
        pass  # fail-open
    return out


def build_audit_zip() -> bytes:
    """审计导出包：全部 JSONL 会话 + 报告文件打成一个 zip（内存构建）。

    名字里带"审计"的原因（README 定位兑现）：这是"可验证自动化"的实物凭证
    —— agent 每一步请求的原始事件流 + 当时产出的报告，一起带走。
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for item in list_sessions():
            try:
                z.write(item["path"], "sessions/" + item["name"])
            except OSError:
                continue  # 导出瞬间文件被删 → 跳过（不炸整个 zip）
        for item in list_reports():
            try:
                z.write(item["path"], "reports/" + item["name"])
            except OSError:
                continue
        # 清单：让审计包自解释（谁、何时、打包了什么）
        manifest = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "tool": "ue5-automation /ui 审计导出（T-20261024-UE5BRIDGE-DASHBOARD）",
            "sessions": [i["name"] for i in list_sessions()],
            "reports": [i["name"] for i in list_reports()],
            "event_note": "事件字段契约: {ts, trace_id, route, method, status, latency_ms, tool_name, category}",
        }
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    return buf.getvalue()


# ══════════════════════════════════════════════════════════
#  画面面板：最新截图（截图闭环就绪前的只读探测）
# ══════════════════════════════════════════════════════════

# 画面面板数据源目录（ue5_bridge.start() 在 GameThread 上注入 —— unreal.Paths
# 只能 GT 调：HTTP 线程连 get_engine_version 都抛异常（真机实录 2026-10-24），
# 而本模块不许反向 import bridge（会成环）→ 由 bridge 启动时注入，fail-open）。
_INJECTED_SCREENSHOTS_DIR = ""


def set_screenshots_dir(path) -> None:
    """bridge 侧 GT 注入口（非 UE 环境/单测不调用）。"""
    global _INJECTED_SCREENSHOTS_DIR
    _INJECTED_SCREENSHOTS_DIR = str(path or "")


def _screenshots_dir() -> str:
    """<工程>/Saved/Screenshots/ue5-automation/（截图闭环约定的落盘目录）。"""
    if _INJECTED_SCREENSHOTS_DIR:
        return _INJECTED_SCREENSHOTS_DIR
    try:
        import unreal  # 延迟导入：本模块可能在非 UE 进程里被单测
        # ⚠️ unreal.Paths.* 返回 "../../../ProjName/" 形态的相对路径（相对引擎 Binaries），
        # os.path.isdir 按进程 CWD 解析必然失败。必须 convert_relative_path_to_full。
        # （此分支仅兜底：正常路径是 start() 的 GT 注入。）
        saved = unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_saved_dir())
        return os.path.join(str(saved), "Screenshots", "ue5-automation")
    except Exception:
        return ""  # 读不到 → 调用方返回占位（不报错，规格书要求）


def latest_screenshot() -> str:
    """最新截图路径（无 → ""）。目录约定见 README 截图闭环章节（规划中）。"""
    items = screenshots_list(limit=1)
    return items[0]["path"] if items else ""


def screenshots_list(limit: int = 24) -> list:
    """截图目录清单（新→旧，胶片条数据源；fail-open 返回 []）。"""
    d = _screenshots_dir()
    if not d or not os.path.isdir(d):
        return []
    try:
        items = []
        for name in os.listdir(d):
            p = os.path.join(d, name)
            if os.path.isfile(p) and name.lower().endswith(".png"):
                try:
                    st = os.stat(p)
                    items.append({"name": name, "path": p,
                                  "mtime": int(st.st_mtime),
                                  "size": st.st_size})
                except OSError:
                    continue
        items.sort(key=lambda x: x["mtime"], reverse=True)
        return items[:max(1, int(limit))]
    except OSError:
        return []  # fail-open


def stats_summary() -> dict:
    """调用统计聚合（图表数据源）：按工具名 / 按分类聚合调用数、错误数、均耗时。

    纯内存聚合（环形缓冲，含本次会话全部事件）；fail-open 返回空骨架。
    """
    try:
        with _lock:
            events = list(_events)
    except Exception:
        events = []
    by_tool, by_cat = {}, {}
    for ev in events:
        tool = ev.get("tool_name") or ev.get("route") or "?"
        cat = ev.get("category") or "other"
        err = 1 if ev.get("error") else 0
        lat = ev.get("latency_ms") or 0.0
        for bucket, key in ((by_tool, tool), (by_cat, cat)):
            b = bucket.setdefault(key, {"calls": 0, "errors": 0, "total_ms": 0.0})
            b["calls"] += 1
            b["errors"] += err
            b["total_ms"] += lat
    def _rows(bucket):
        rows = [{"key": k, "calls": v["calls"], "errors": v["errors"],
                 "avg_ms": round(v["total_ms"] / v["calls"], 1)}
                for k, v in bucket.items()]
        rows.sort(key=lambda r: r["calls"], reverse=True)
        return rows[:15]
    total_calls = sum(r["calls"] for r in by_tool.values())
    total_errors = sum(r["errors"] for r in by_tool.values())
    return {"total_calls": total_calls, "total_errors": total_errors,
            "by_tool": _rows(by_tool), "by_category": _rows(by_cat)}


# ══════════════════════════════════════════════════════════
#  测试辅助（生产代码不调用）
# ══════════════════════════════════════════════════════════

def reset_for_tests() -> None:
    """清空全部内存态（环形缓冲/快照/游标）。测试隔离用。"""
    global _event_seq, _verdict_seq
    with _lock:
        _event_seq = 0
        _verdict_seq = 0
        _events.clear()
        _verdicts.clear()
        _graph_snapshots.clear()
        _graph_order.clear()
